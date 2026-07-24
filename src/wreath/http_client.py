"""Lifespan-managed dependency-free outbound HTTP/1.1 client.

The public policy and pool remain in Python. Byte codecs start with the pure
reference implementation and are the parity contract for the optional native
client protocol.
"""

from __future__ import annotations  # noqa: I001 -- Ruff misorders the local codec facade

import asyncio
import ipaddress
import os
import random
import socket
import ssl
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import SplitResult, urljoin, urlsplit

from . import _client_codec

try:  # accelerated transport-facing stream; optional like every native piece
    from wreath._native._client import Http1ClientStream as _NativeClientStream
except ImportError:  # pragma: no cover - pure tier
    _NativeClientStream = None
if os.environ.get("WREATH_CLIENT_NATIVE_STREAM") == "0":
    _NativeClientStream = None
from time import monotonic as _monotonic
from time import monotonic_ns as _monotonic_ns

# Reuse the native token-bucket that backs the inbound rate-limiter (no new C):
# the outbound client throttle shares the exact same primitive.
from ._native import _core as _native_core

if _native_core is not None and hasattr(_native_core, "TokenBucket"):
    _TokenBucket: Any = _native_core.TokenBucket
else:  # pragma: no cover - exercised under WREATH_PURE=1
    from ._pure.ratelimit import TokenBucket as _TokenBucket

from ._flight_markers import (
    CAP_OUTBOUND_REQUEST as _CAP_OUTBOUND_REQUEST,
)
from ._flight_markers import (
    CAP_OUTBOUND_RESPONSE as _CAP_OUTBOUND_RESPONSE,
)
from ._flight_markers import (
    COV_EXTERNAL as _COV_EXTERNAL,
)
from ._flight_markers import (
    PH_HTTP_CLIENT as _PH_HTTP_CLIENT,
)
from ._flight_markers import (
    capture_marker as _capture_marker,
)
from ._flight_markers import (
    phase_marker as _phase_marker,
)

type _AddressInfo = tuple[int, int, int, str, tuple[object, ...]]

_HAPPY_EYEBALLS_DELAY = 0.25


async def _timed(pending: Any, deadline_seconds: float) -> bytes:
    """Await a stream read under a timeout, skipping wait_for entirely when
    the read already resolved.

    The native client stream returns buffered reads synchronously as `bytes`
    (no future, no await) and only allocates a loop future when it must wait;
    a timeout that cannot fire is not worth wait_for's Timeout context, timer
    handle, and cancellation bookkeeping per read. The asyncio-streams
    fallback always passes coroutines through to wait_for unchanged.
    """
    if type(pending) is bytes:
        return pending
    if isinstance(pending, asyncio.Future) and pending.done():
        return cast(bytes, pending.result())
    return cast(bytes, await asyncio.wait_for(pending, deadline_seconds))


class ClientError(Exception):
    """Base class for outbound client failures."""


class ClientClosed(ClientError):
    pass


class PoolTimeout(ClientError):
    pass


class ConnectError(ClientError):
    pass


class DNSFailure(ConnectError):
    pass


class TLSFailure(ConnectError):
    pass


class RequestTimeout(ClientError):
    pass


class ResponseTimeout(ClientError):
    pass


class ProtocolError(ClientError):
    pass


class _TransportError(ProtocolError):
    """A transient connection failure during an HTTP exchange."""


class ResponseTooLarge(ClientError):
    pass


class RedirectError(ClientError):
    pass


class DestinationRejected(ClientError):
    pass


class ProxyError(ClientError):
    pass


@dataclass(frozen=True, slots=True)
class ClientLimits:
    max_connections: int = 20
    max_keepalive_connections: int = 10
    max_waiters: int = 100
    max_request_header_bytes: int = 32 * 1024
    max_response_header_bytes: int = 32 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    read_high_water: int = 256 * 1024
    dns_cache_ttl: float = 30.0

    def __post_init__(self) -> None:
        values = (
            self.max_connections,
            self.max_keepalive_connections,
            self.max_waiters,
            self.max_request_header_bytes,
            self.max_response_header_bytes,
            self.max_response_bytes,
            self.read_high_water,
        )
        if any(value <= 0 for value in values):
            raise ValueError("client limits must be positive")
        if self.dns_cache_ttl < 0:
            raise ValueError("DNS cache TTL cannot be negative")
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")


@dataclass(frozen=True, slots=True)
class ClientTimeout:
    pool: float = 1.0
    connect: float = 5.0
    tls: float = 5.0
    response_headers: float = 10.0
    response_body: float = 30.0
    total: float | None = 45.0

    def __post_init__(self) -> None:
        values = (self.pool, self.connect, self.tls, self.response_headers, self.response_body)
        if any(value <= 0 for value in values) or (self.total is not None and self.total <= 0):
            raise ValueError("client timeouts must be positive")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 1
    idempotent_only: bool = True
    statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
    # Exponential backoff between attempts: min(base * 2**attempt, cap), with
    # optional bounded jitter and Retry-After honouring on 429/503.
    backoff_base: float = 0.05
    backoff_cap: float = 1.0
    jitter: bool = True
    respect_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.attempts <= 0:
            raise ValueError("retry attempts must be positive")
        if self.backoff_base <= 0 or self.backoff_cap <= 0:
            raise ValueError("retry backoff_base and backoff_cap must be positive")


@dataclass(frozen=True, slots=True)
class RatePolicy:
    """Client-side outbound throttle: one continuous token bucket shared by every
    request from this client (``rate`` tokens/sec, bursting to ``capacity``),
    reusing the same native ``TokenBucket`` as the inbound rate-limiter. When a
    token is not yet available the request parks — up to ``max_wait`` — rather
    than being rejected. ``enabled=False`` (default) skips throttling entirely.

    TODO(pure-twin): none needed — TokenBucket already has a native/pure twin
    selected above; this policy is pure-Python glue.
    """

    enabled: bool = False
    capacity: float = 0.0
    rate: float = 0.0
    max_wait: float = 30.0

    def __post_init__(self) -> None:
        if self.enabled:
            if self.rate <= 0 or self.capacity <= 0:
                raise ValueError("an enabled RatePolicy needs positive rate and capacity")
            if self.max_wait <= 0:
                raise ValueError("RatePolicy max_wait must be positive")


_DEFAULT_RATE = RatePolicy()


def _parse_retry_after(raw: bytes | None) -> float | None:
    """Parse a ``Retry-After`` header. Only the delta-seconds form is honoured;
    the HTTP-date form falls back to normal backoff (returns None)."""
    if raw is None:
        return None
    try:
        seconds = int(raw.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        return None
    return float(seconds) if seconds >= 0 else None


@dataclass(frozen=True, slots=True)
class RedirectPolicy:
    enabled: bool = False
    max_hops: int = 0
    allow_cross_origin: bool = False

    def __post_init__(self) -> None:
        if self.max_hops < 0:
            raise ValueError("redirect max_hops cannot be negative")
        if self.enabled and self.max_hops == 0:
            raise ValueError("enabled redirects require max_hops")


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    schemes: frozenset[str] = frozenset({"http", "https"})
    hosts: tuple[str, ...] = ()
    ports: frozenset[int] = frozenset()
    allow_private: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False

    def validate_url(self, parsed: SplitResult) -> None:
        if parsed.scheme not in self.schemes:
            raise DestinationRejected(f"scheme {parsed.scheme!r} is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise DestinationRejected("URL credentials are not allowed")
        hostname = parsed.hostname
        if hostname is None:
            raise DestinationRejected("destination host is required")
        normalized = hostname.encode("idna").decode("ascii").lower()
        if self.hosts and not any(_host_matches(normalized, pattern) for pattern in self.hosts):
            raise DestinationRejected("destination host is not allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.ports and port not in self.ports:
            raise DestinationRejected("destination port is not allowed")

    def validate_address(self, value: str) -> None:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if address.is_loopback and not self.allow_loopback:
            raise DestinationRejected("loopback destination address is not allowed")
        if address.is_link_local and not self.allow_link_local:
            raise DestinationRejected("link-local destination address is not allowed")
        if address.is_private and not address.is_loopback and not self.allow_private:
            raise DestinationRejected("private destination address is not allowed")
        if address.is_unspecified or address.is_multicast:
            raise DestinationRejected("special destination address is not allowed")
        if not address.is_global and not (
            (address.is_loopback and self.allow_loopback)
            or (address.is_link_local and self.allow_link_local)
            or (address.is_private and self.allow_private)
        ):
            raise DestinationRejected("non-global destination address is not allowed")


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    active: int
    idle: int
    waiters: int
    requests: int
    reused: int


@dataclass(frozen=True, slots=True)
class ClientResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    http_version: str
    reason: bytes = b""

    def header(self, name: bytes) -> bytes | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key == lowered:
                return value
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise ClientError(f"HTTP response status {self.status}")


class _NativeStreamWriter:
    """StreamWriter surface over a transport + native client stream.

    The native stream protocol owns backpressure futures (`_drain`,
    `_wait_closed`); writes go straight to the transport, which on the metal
    loop means the retained zero-copy send queue.
    """

    __slots__ = ("_transport", "_protocol")

    def __init__(self, transport: asyncio.BaseTransport, protocol: Any) -> None:
        self._transport = transport
        self._protocol = protocol

    def write(self, data: bytes) -> None:
        self._transport.write(data)  # type: ignore[attr-defined]

    async def drain(self) -> None:
        waiter = self._protocol._drain()
        if waiter is not None:
            await waiter

    def close(self) -> None:
        self._transport.close()

    def is_closing(self) -> bool:
        return self._transport.is_closing()

    async def wait_closed(self) -> None:
        await self._protocol._wait_closed()


@dataclass(slots=True, eq=False)
class _Connection:
    reader: Any  # asyncio.StreamReader or native Http1ClientStream
    writer: Any  # asyncio.StreamWriter or _NativeStreamWriter


_IDEMPOTENT = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PUT", "TRACE"})
_DEFAULT_LIMITS = ClientLimits()
_DEFAULT_TIMEOUT = ClientTimeout()
_DEFAULT_RETRY = RetryPolicy()
_DEFAULT_REDIRECT = RedirectPolicy()
_DEFAULT_DESTINATION = DestinationPolicy()


def _host_matches(host: str, pattern: str) -> bool:
    normalized = pattern.encode("idna").decode("ascii").lower()
    if normalized.startswith("*."):
        suffix = normalized[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == normalized


class HTTPClient:
    """One bounded pool for a configured HTTP origin."""

    __slots__ = (
        "_active",
        "_base_path",
        "_condition",
        "_destination",
        "_dns_addresses",
        "_dns_expires_at",
        "_dns_lock",
        "_flight_dep_id",
        "_host",
        "_idle",
        "_limits",
        "_name",
        "_open",
        "_port",
        "_rate",
        "_rate_bucket",
        "_redirect",
        "_requests",
        "_retry",
        "_reused",
        "_scheme",
        "_ssl_context",
        "_started",
        "_timeout",
        "_waiters",
    )

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        limits: ClientLimits = _DEFAULT_LIMITS,
        timeout: ClientTimeout = _DEFAULT_TIMEOUT,
        retry: RetryPolicy = _DEFAULT_RETRY,
        redirect: RedirectPolicy = _DEFAULT_REDIRECT,
        destination: DestinationPolicy = _DEFAULT_DESTINATION,
        rate: RatePolicy = _DEFAULT_RATE,
    ) -> None:
        if not name:
            raise ValueError("HTTP client name cannot be empty")
        parsed = urlsplit(base_url)
        destination.validate_url(parsed)
        if parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain a query or fragment")
        assert parsed.hostname is not None
        self._name = name
        self._scheme = parsed.scheme
        self._host = parsed.hostname.encode("idna").decode("ascii").lower()
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._limits = limits
        self._timeout = timeout
        self._retry = retry
        self._redirect = redirect
        self._destination = destination
        self._rate = rate
        # One shared bucket per client (single key); reuses the native TokenBucket.
        self._rate_bucket = (
            _TokenBucket(capacity=rate.capacity, rate=rate.rate, max_entries=1)
            if rate.enabled
            else None
        )
        self._condition = asyncio.Condition()
        self._dns_lock = asyncio.Lock()
        # Metadata-image ID for phase attribution; stamped by the app when the
        # flight recorder joins live objects to the image (0 = unattributed).
        self._flight_dep_id = 0
        self._dns_addresses: tuple[_AddressInfo, ...] = ()
        self._dns_expires_at = 0.0
        self._ssl_context: ssl.SSLContext | None = None
        self._active: set[_Connection] = set()
        self._idle: list[_Connection] = []
        self._open = 0
        self._waiters = 0
        self._requests = 0
        self._reused = 0
        self._started = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def started(self) -> bool:
        return self._started

    async def __aenter__(self) -> HTTPClient:
        await self.start()
        return self

    async def __aexit__(self, *_error: object) -> None:
        await self.close()

    async def start(self) -> None:
        async with self._condition:
            if self._started:
                return
            self._started = True
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            if not self._started and not self._idle and not self._active:
                return
            self._started = False
            idle = self._idle
            self._idle = []
            self._open -= len(idle)
            self._condition.notify_all()
        for connection in idle:
            connection.writer.close()
        for connection in idle:
            try:
                await connection.writer.wait_closed()
            except (ConnectionError, OSError):
                pass

        drain_timeout = self._timeout.total or self._timeout.response_body
        try:
            async with asyncio.timeout(drain_timeout):
                async with self._condition:
                    await self._condition.wait_for(lambda: not self._active)
        except TimeoutError:
            async with self._condition:
                active = tuple(self._active)
            for connection in active:
                connection.writer.close()
            for connection in active:
                try:
                    await connection.writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    def snapshot(self) -> ClientSnapshot:
        return ClientSnapshot(
            active=len(self._active),
            idle=len(self._idle),
            waiters=self._waiters,
            requests=self._requests,
            reused=self._reused,
        )

    async def get(
        self, target: str, *, headers: tuple[tuple[bytes, bytes], ...] = ()
    ) -> ClientResponse:
        return await self.request("GET", target, headers=headers)

    async def post(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        return await self.request(
            "POST", target, headers=headers, body=body, idempotency_key=idempotency_key
        )

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        # Armed-request outbound phase; every other request pays exactly the
        # ContextVar read. A finally records failed and timed-out calls too —
        # their duration is precisely what the Inspector wants to see.
        marker = _phase_marker.get(None)
        if marker is None:
            return await self._request_timed(
                method, target, headers=headers, body=body,
                idempotency_key=idempotency_key,
            )
        # Forensic dependency capture rides inside the phase gate (Detailed-armed
        # requests only) and fires only when a Forensic arm bound the capturer.
        # The outbound request body is captured before the call; the response
        # body in the finally, so a failed/timed-out call still records what it
        # sent. Both are redacted natively per the arm's dependency disposition.
        capture = _capture_marker.get(None)
        if capture is not None and body:
            capture(_CAP_OUTBOUND_REQUEST, bytes(body))
        start = _monotonic_ns()
        response = None
        try:
            response = await self._request_timed(
                method, target, headers=headers, body=body,
                idempotency_key=idempotency_key,
            )
            return response
        finally:
            marker(_PH_HTTP_CLIENT, self._flight_dep_id, _COV_EXTERNAL,
                   _monotonic_ns() - start)
            if capture is not None and response is not None and response.body:
                capture(_CAP_OUTBOUND_RESPONSE, response.body)

    async def _request_timed(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes | bytearray | memoryview,
        idempotency_key: str | None,
    ) -> ClientResponse:
        if self._timeout.total is None:
            return await self._request_flow(
                method,
                target,
                headers=headers,
                body=body,
                idempotency_key=idempotency_key,
            )
        try:
            async with asyncio.timeout(self._timeout.total):
                return await self._request_flow(
                    method,
                    target,
                    headers=headers,
                    body=body,
                    idempotency_key=idempotency_key,
                )
        except TimeoutError as error:
            raise RequestTimeout("outbound request exceeded total timeout") from error

    async def _request_flow(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes | bytearray | memoryview,
        idempotency_key: str | None,
    ) -> ClientResponse:
        if not self._started:
            raise ClientClosed(f"HTTP client {self._name!r} is not started")
        request_headers = headers
        if idempotency_key is not None:
            request_headers = (
                *request_headers,
                (b"idempotency-key", idempotency_key.encode("ascii")),
            )
        current_target = self._request_target(target).decode("ascii")
        method_upper = method.upper()
        payload = bytes(body)
        redirects = 0
        while True:
            request_bytes = _client_codec.serialize_request(
                method_upper,
                current_target.encode("ascii"),
                self._authority().encode("ascii"),
                headers=request_headers,
                body=payload,
            )
            if len(request_bytes) - len(payload) > self._limits.max_request_header_bytes:
                raise ClientError("request headers exceed configured limit")
            response = await self._send_with_retries(
                method_upper,
                request_bytes,
                idempotency_key=idempotency_key,
            )
            if not self._redirect.enabled or response.status not in {
                301,
                302,
                303,
                307,
                308,
            }:
                return response
            location = response.header(b"location")
            if location is None:
                return response
            if redirects >= self._redirect.max_hops:
                raise RedirectError("outbound redirect limit exceeded")
            redirects += 1
            current_target = self._redirect_target(current_target, location)
            if response.status == 303:
                method_upper = "GET"
                payload = b""
            elif response.status in (301, 302) and method_upper not in ("GET", "HEAD"):
                raise RedirectError("redirect would require rewriting a non-idempotent method")

    async def _throttle(self) -> None:
        """Park until the outbound token bucket admits this request (bounded by
        ``RatePolicy.max_wait``). No-op unless rate limiting is enabled."""
        bucket = self._rate_bucket
        if bucket is None:
            return
        deadline = _monotonic() + self._rate.max_wait
        while True:
            now = _monotonic()
            wait = float(bucket.acquire("client", now, 1.0))
            if wait <= 0.0:
                return
            if now + wait > deadline:
                raise ClientError("outbound rate limit exceeded max_wait")
            await asyncio.sleep(wait)

    def _retry_delay(self, attempt: int, response: ClientResponse | None) -> float:
        """Backoff before the next attempt: honour Retry-After on the response
        when present, else exponential backoff with optional bounded jitter."""
        policy = self._retry
        if policy.respect_retry_after and response is not None:
            after = _parse_retry_after(response.header(b"retry-after"))
            if after is not None:
                # Clamp an absurd server value so one bad header can't hang us.
                return min(after, policy.backoff_cap * 16)
        delay = min(policy.backoff_base * (2**attempt), policy.backoff_cap)
        if policy.jitter:
            delay *= 0.5 + random.random() * 0.5  # jitter within [0.5x, 1.0x]
        return delay

    async def _send_with_retries(
        self,
        method: str,
        request_bytes: bytes,
        *,
        idempotency_key: str | None,
    ) -> ClientResponse:
        retryable = (
            not self._retry.idempotent_only
            or method in _IDEMPOTENT
            or idempotency_key is not None
        )
        attempts = self._retry.attempts if retryable else 1
        last_error: ClientError | None = None
        for attempt in range(attempts):
            await self._throttle()  # each attempt spends a token
            retry_response: ClientResponse | None = None
            try:
                response = await self._request_once(method, request_bytes)
            except (ConnectError, DNSFailure, ResponseTimeout, _TransportError) as error:
                last_error = error
                if attempt + 1 == attempts:
                    raise
            except ClientError:
                raise
            else:
                if response.status not in self._retry.statuses or attempt + 1 == attempts:
                    return response
                retry_response = response
            await asyncio.sleep(self._retry_delay(attempt, retry_response))
        assert last_error is not None
        raise last_error

    def _request_target(self, target: str) -> bytes:
        if not target.startswith("/") or target.startswith("//"):
            raise ValueError("request target must be origin-relative")
        combined = f"{self._base_path}{target}" if self._base_path else target
        try:
            return combined.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("request target must be ASCII/percent-encoded") from error

    def _redirect_target(self, current: str, location: bytes) -> str:
        try:
            value = location.decode("ascii")
        except UnicodeDecodeError as error:
            raise RedirectError("redirect location must be ASCII/percent-encoded") from error
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            self._destination.validate_url(parsed)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            same_origin = (
                parsed.scheme == self._scheme
                and parsed.hostname is not None
                and parsed.hostname.encode("idna").decode("ascii").lower() == self._host
                and port == self._port
            )
            if not same_origin:
                if not self._redirect.allow_cross_origin:
                    raise RedirectError("cross-origin redirect is not allowed")
                raise RedirectError(
                    "cross-origin redirects require a separately configured client"
                )
            target = parsed.path or "/"
            return f"{target}?{parsed.query}" if parsed.query else target
        joined = urljoin(current, value)
        relative = urlsplit(joined)
        target = relative.path or "/"
        return f"{target}?{relative.query}" if relative.query else target

    def _authority(self) -> str:
        default = (self._scheme == "http" and self._port == 80) or (
            self._scheme == "https" and self._port == 443
        )
        host = f"[{self._host}]" if ":" in self._host else self._host
        return host if default else f"{host}:{self._port}"

    async def _request_once(self, method: str, request: bytes) -> ClientResponse:
        connection = await self._acquire()
        reusable = False
        try:
            connection.writer.write(request)
            await connection.writer.drain()
            self._requests += 1
            response, reusable = await self._read_response(connection.reader, method)
            return response
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as error:
            raise _TransportError("connection failed during HTTP exchange") from error
        finally:
            await self._release(connection, reusable)

    async def _acquire(self) -> _Connection:
        create = False
        async with self._condition:
            if not self._started:
                raise ClientClosed(f"HTTP client {self._name!r} is not started")
            while True:
                while self._idle:
                    connection = self._idle.pop()
                    if not connection.writer.is_closing() and not connection.reader.at_eof():
                        self._reused += 1
                        self._active.add(connection)
                        return connection
                    connection.writer.close()
                    self._open -= 1
                if self._open < self._limits.max_connections:
                    self._open += 1
                    create = True
                    break
                if self._waiters >= self._limits.max_waiters:
                    raise PoolTimeout("HTTP client waiter limit reached")
                self._waiters += 1
                try:
                    await asyncio.wait_for(self._condition.wait(), self._timeout.pool)
                except TimeoutError as error:
                    raise PoolTimeout("timed out waiting for an HTTP connection") from error
                finally:
                    self._waiters -= 1
                if not self._started:
                    raise ClientClosed(f"HTTP client {self._name!r} is not started")
        assert create
        try:
            connection = await self._connect()
        except BaseException:
            async with self._condition:
                self._open -= 1
                self._condition.notify(1)
            raise
        async with self._condition:
            if self._started:
                self._active.add(connection)
                return connection
            self._open -= 1
            self._condition.notify_all()
        connection.writer.close()
        try:
            await connection.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        raise ClientClosed(f"HTTP client {self._name!r} closed while connecting")

    async def _resolve(self) -> tuple[_AddressInfo, ...]:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._dns_addresses and now < self._dns_expires_at:
            return self._dns_addresses
        async with self._dns_lock:
            now = loop.time()
            if self._dns_addresses and now < self._dns_expires_at:
                return self._dns_addresses
            try:
                resolved = await asyncio.wait_for(
                    loop.getaddrinfo(
                        self._host,
                        self._port,
                        type=socket.SOCK_STREAM,
                        proto=socket.IPPROTO_TCP,
                    ),
                    self._timeout.connect,
                )
            except TimeoutError as error:
                raise DNSFailure("destination resolution timed out") from error
            except OSError as error:
                raise DNSFailure("destination resolution failed") from error
            addresses = tuple(cast(list[_AddressInfo], resolved))
            if not addresses:
                raise DNSFailure("destination resolved to no addresses")
            for family, _socket_type, _protocol, _canonical, sockaddr in addresses:
                self._destination.validate_address(self._address(family, sockaddr))
            self._dns_addresses = addresses
            self._dns_expires_at = now + self._limits.dns_cache_ttl
            return addresses

    @staticmethod
    def _address(family: int, sockaddr: tuple[object, ...]) -> str:
        address = cast(str, sockaddr[0])
        if family == socket.AF_INET6 and len(sockaddr) >= 4:
            scope_id = cast(int, sockaddr[3])
            if scope_id and "%" not in address:
                return f"{address}%{scope_id}"
        return address

    async def _open_address(
        self,
        address_info: _AddressInfo,
        delay: float,
        ssl_context: ssl.SSLContext | None,
    ) -> _Connection:
        if delay:
            await asyncio.sleep(delay)
        family, _socket_type, _protocol, _canonical, sockaddr = address_info
        timeout = self._timeout.connect + (
            self._timeout.tls if ssl_context is not None else 0
        )
        if _NativeClientStream is not None:
            # Native stream: C-owned receive buffer, StreamReader-shaped
            # awaitable reads, and the stream-fusion C API -- on a metal loop
            # wire bytes never cross into Python per read.
            protocol = _NativeClientStream(limit=self._limits.read_high_water)
            loop = asyncio.get_running_loop()
            transport, _ = await asyncio.wait_for(
                loop.create_connection(
                    lambda: protocol,
                    self._address(family, sockaddr),
                    self._port,
                    family=family,
                    flags=0,
                    ssl=ssl_context,
                    server_hostname=(
                        self._host if ssl_context is not None else None
                    ),
                ),
                timeout,
            )
            return _Connection(protocol, _NativeStreamWriter(transport, protocol))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self._address(family, sockaddr),
                self._port,
                family=family,
                flags=0,
                limit=self._limits.read_high_water,
                ssl=ssl_context,
                server_hostname=self._host if ssl_context is not None else None,
            ),
            timeout,
        )
        return _Connection(reader, writer)

    async def _connect(self) -> _Connection:
        addresses = await self._resolve()
        if self._scheme == "https" and self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        tasks = [
            asyncio.create_task(
                self._open_address(
                    address_info,
                    index * _HAPPY_EYEBALLS_DELAY,
                    self._ssl_context,
                )
            )
            for index, address_info in enumerate(addresses)
        ]
        pending = set(tasks)
        errors: list[BaseException] = []
        winner: _Connection | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        connection = task.result()
                    except (OSError, ssl.SSLError, TimeoutError) as error:
                        errors.append(error)
                    else:
                        if winner is None:
                            winner = connection
                        else:
                            connection.writer.close()
                if winner is not None:
                    return winner
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in tasks:
                if (
                    task.done()
                    and not task.cancelled()
                    and task.exception() is None
                    and task.result() is not winner
                ):
                    task.result().writer.close()
        tls_error = next((error for error in errors if isinstance(error, ssl.SSLError)), None)
        if tls_error is not None:
            raise TLSFailure("TLS connection failed") from tls_error
        last_error = errors[-1] if errors else None
        raise ConnectError("destination connection failed") from last_error

    async def _release(self, connection: _Connection, reusable: bool) -> None:
        keep = False
        async with self._condition:
            self._active.discard(connection)
            if (
                reusable
                and self._started
                and not connection.writer.is_closing()
                and not connection.reader.at_eof()
                and len(self._idle) < self._limits.max_keepalive_connections
            ):
                self._idle.append(connection)
                keep = True
            else:
                self._open -= 1
            if self._started:
                self._condition.notify(1)
            else:
                self._condition.notify_all()
        if not keep:
            connection.writer.close()
            try:
                await connection.writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_response(
        self, reader: asyncio.StreamReader, method: str
    ) -> tuple[ClientResponse, bool]:
        while True:
            try:
                head = await _timed(
                    reader.readuntil(b"\r\n\r\n"), self._timeout.response_headers
                )
            except TimeoutError as error:
                raise ResponseTimeout("timed out reading response headers") from error
            except asyncio.LimitOverrunError as error:
                raise ProtocolError("response headers exceed stream limit") from error
            if len(head) > self._limits.max_response_header_bytes:
                raise ProtocolError("response headers exceed configured limit")
            try:
                parsed = _client_codec.parse_response_head(head)
            except ValueError as error:
                raise ProtocolError(str(error)) from error
            assert parsed is not None
            minor, status, reason, headers, consumed = parsed
            if consumed != len(head):
                raise ProtocolError("response parser did not consume the complete head")
            if status == 101:
                raise ProtocolError("protocol switching is not supported")
            if status >= 200:
                break

        body, framed = await self._read_body(reader, method, status, headers)
        connection_tokens = {
            token.strip().lower()
            for name, value in headers
            if name == b"connection"
            for token in value.split(b",")
            if token.strip()
        }
        reusable = framed and b"close" not in connection_tokens
        if minor == 0:
            reusable = reusable and b"keep-alive" in connection_tokens
        return (
            ClientResponse(status, tuple(headers), body, f"1.{minor}", reason),
            reusable,
        )

    async def _read_body(
        self,
        reader: asyncio.StreamReader,
        method: str,
        status: int,
        headers: list[tuple[bytes, bytes]],
    ) -> tuple[bytes, bool]:
        try:
            mode, length = _client_codec.response_framing(method, status, headers)
        except ValueError as error:
            raise ProtocolError(str(error)) from error
        if mode == "none":
            self._reject_buffered_extra(reader)
            return b"", True
        if mode == "chunked":
            body = await self._read_chunked(reader)
            self._reject_buffered_extra(reader)
            return body, True
        if mode == "length":
            if length > self._limits.max_response_bytes:
                raise ResponseTooLarge("response body exceeds configured limit")
            try:
                body = await _timed(
                    reader.readexactly(length), self._timeout.response_body
                )
            except TimeoutError as error:
                raise ResponseTimeout("timed out reading response body") from error
            self._reject_buffered_extra(reader)
            return body, True

        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                chunk = await _timed(
                    reader.read(min(64 * 1024, self._limits.max_response_bytes + 1 - size)),
                    self._timeout.response_body,
                )
            except TimeoutError as error:
                raise ResponseTimeout("timed out reading close-delimited response") from error
            if not chunk:
                return b"".join(chunks), False
            size += len(chunk)
            if size > self._limits.max_response_bytes:
                raise ResponseTooLarge("response body exceeds configured limit")
            chunks.append(chunk)

    @staticmethod
    def _reject_buffered_extra(reader: asyncio.StreamReader) -> None:
        buffered = getattr(reader, "_buffer", None)
        if buffered:
            raise ProtocolError("unsolicited bytes follow the framed response body")

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        total = 0
        while True:
            try:
                line = await _timed(
                    reader.readuntil(b"\r\n"), self._timeout.response_body
                )
            except TimeoutError as error:
                raise ResponseTimeout("timed out reading response chunk") from error
            if len(line) > 1024:
                raise ProtocolError("response chunk line exceeds limit")
            size_data = line[:-2].split(b";", 1)[0]
            if not size_data or any(
                byte not in b"0123456789abcdefABCDEF" for byte in size_data
            ):
                raise ProtocolError("invalid response chunk size")
            size = int(size_data, 16)
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = await _timed(
                        reader.readuntil(b"\r\n"), self._timeout.response_body
                    )
                    trailer_bytes += len(trailer)
                    if trailer_bytes > self._limits.max_response_header_bytes:
                        raise ProtocolError("response trailers exceed configured limit")
                    if trailer == b"\r\n":
                        return bytes(body)
                    try:
                        _client_codec.parse_response_head(
                            b"HTTP/1.1 200 OK\r\n" + trailer + b"\r\n"
                        )
                    except ValueError as error:
                        raise ProtocolError(str(error)) from error
            total += size
            if total > self._limits.max_response_bytes:
                raise ResponseTooLarge("response body exceeds configured limit")
            try:
                chunk = await _timed(
                    reader.readexactly(size + 2), self._timeout.response_body
                )
            except TimeoutError as error:
                raise ResponseTimeout("timed out reading response chunk") from error
            if chunk[-2:] != b"\r\n":
                raise ProtocolError("malformed response chunk terminator")
            body.extend(chunk[:-2])


__all__ = [
    "ClientClosed",
    "ClientError",
    "ClientLimits",
    "ClientResponse",
    "ClientSnapshot",
    "ClientTimeout",
    "ConnectError",
    "DNSFailure",
    "DestinationPolicy",
    "DestinationRejected",
    "HTTPClient",
    "PoolTimeout",
    "ProtocolError",
    "ProxyError",
    "RedirectError",
    "RatePolicy",
    "RedirectPolicy",
    "RequestTimeout",
    "ResponseTimeout",
    "ResponseTooLarge",
    "RetryPolicy",
    "TLSFailure",
]
