"""Lifespan-managed dependency-free outbound HTTP/1.1 client.

One `HTTPClient` is one bounded connection pool for one origin. The
origin is fixed at construction from `base_url` and never changes: every
request target is origin-relative, and a redirect that would leave the origin is
refused rather than followed. Calling a second service means a second client,
which is what makes each service's limits, timeouts, retries, and destination
policy separately configurable and separately observable.

```python
client = HTTPClient("billing", base_url="https://billing.internal/v1")
async with client:
    response = await client.get("/invoices/42")
```

The client must be started before it will serve a request; `async with` does
that, and an application normally starts and closes it from the lifespan instead.
A request on a client that is not started raises `ClientClosed` rather
than starting one implicitly, so a missing lifespan registration fails loudly.

Everything the caller configures is a frozen dataclass validated at construction
(`ClientLimits`, `ClientTimeout`, `RetryPolicy`,
`RedirectPolicy`, `DestinationPolicy`, `RatePolicy`), so a
misconfiguration raises at boot rather than on the first outbound call.

Responses are read fully into memory under `ClientLimits.max_response_bytes`;
there is no streaming response API. That is a deliberate limitation of this
client, which exists to call other services, not to download files.

The public policy and pool remain in Python. Byte codecs start with the pure
reference implementation and are the parity contract for the optional native
client protocol.
"""

from __future__ import annotations  # noqa: I001 -- Ruff misorders the local codec facade

import asyncio
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import SplitResult, urljoin, urlsplit

from . import _client_codec
from . import telemetry as _telemetry
from ._jobcore import compute_backoff
from ._native import _client as _native_client
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

# Accelerated transport-facing stream; optional like every native piece. Resolved
# through the `_native` loader rather than imported straight from the compiled
# submodule, so it takes the same Any-typed, None-on-absence path as `_core`: a
# direct import names a module that need not have been built.
_NativeClientStream = (
    None if _native_client is None else getattr(_native_client, "Http1ClientStream", None)
)
if os.environ.get("WREATH_CLIENT_NATIVE_STREAM") == "0":
    _NativeClientStream = None

type _AddressInfo = tuple[int, int, int, str, tuple[object, ...]]

_HAPPY_EYEBALLS_DELAY = 0.25

# RFC 6052's well-known NAT64 prefix is globally routed, so `ipaddress` quite
# correctly classifies an address inside it as global without interpreting the
# final 32 bits.  A translator does interpret those bits, however: accepting
# `64:ff9b::7f00:1` would therefore admit a route to 127.0.0.1 on a NAT64 host.
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")

# RFC 4291's deprecated IPv4-compatible form also puts an IPv4 address in the
# final 32 bits. Python 3.14 intentionally does not expose it as `ipv4_mapped`
# and classifies spellings such as `::127.0.0.1` as global. Some stacks and
# translators still honour the embedded destination, so apply the IPv4 policy
# rather than allowing that classification mismatch to become an SSRF bypass.
_IPV4_COMPATIBLE_PREFIX = ipaddress.IPv6Network("::/96")
_IPV6_LOOPBACK = ipaddress.IPv6Address("::1")


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
    """Base class for outbound client failures.

    Every failure this module raises derives from it, so `except ClientError`
    is the one catch that covers the client without also catching bugs in the
    calling code. `ClientResponse.raise_for_status` raises this base class
    directly for a >=400 status, which is the only case where the exception type
    carries no more information than the message.
    """


class ClientClosed(ClientError):
    """The client is not started, or was closed while this request was in flight.

    Raised rather than starting the client implicitly: a client that is not
    started is almost always one that was never registered with the lifespan,
    and silently starting it would move the failure to shutdown.
    """


class PoolTimeout(ClientError):
    """No connection became available within `ClientTimeout.pool`.

    Also raised immediately, without waiting, when `ClientLimits.max_waiters`
    callers are already queued -- shedding at a known bound beats an unbounded
    queue that turns one slow origin into the whole process's latency.
    """


class ConnectError(ClientError):
    """Every candidate address failed to connect within the connect timeout.

    Carries the last underlying error as its `__cause__`.
    """


class DNSFailure(ConnectError):
    """The origin host did not resolve, resolution timed out, or yielded nothing."""


class TLSFailure(ConnectError):
    """The TLS handshake failed. Preferred over `ConnectError` whenever any
    candidate address raised an `ssl.SSLError`, so a certificate problem is never
    reported as an unreachable host."""


class RequestTimeout(ClientError):
    """The whole request exceeded `ClientTimeout.total`.

    The outermost deadline: it covers pool waiting, DNS, connect, TLS, retries,
    backoff sleeps, and redirect hops together, so no combination of the inner
    timeouts can outlast it. `total=None` removes it.
    """


class ResponseTimeout(ClientError):
    """The origin stopped sending mid-response.

    Distinct from `RequestTimeout`: this one is an inner deadline
    (`ClientTimeout.response_headers` or `response_body`), and it is
    retryable because it means the connection stalled rather than that the
    caller's budget ran out.
    """


class ProtocolError(ClientError):
    """The origin's bytes are not a response this client can frame.

    Covers an unparseable head, headers past the configured limit, a malformed
    chunked body, a `101` (protocol switching is not supported), and bytes
    still buffered after a fully framed body -- the last being a
    request-smuggling shape, which is why it is refused rather than ignored.
    """


class _TransportError(ProtocolError):
    """A transient connection failure during an HTTP exchange.

    Deliberately a `ProtocolError` subclass and deliberately private: a
    connection that died mid-exchange is retryable, unlike the framing failures
    its base class otherwise names, and callers catch the base class.
    """


class ResponseTooLarge(ClientError):
    """The response body exceeded `ClientLimits.max_response_bytes`.

    A declared `Content-Length` over the limit is refused before a byte of body
    is read; chunked and close-delimited bodies are counted as they arrive. A
    missing or lying length therefore cannot get past it either.
    """


class RedirectError(ClientError):
    """A redirect could not be followed under `RedirectPolicy`.

    Raised when the hop limit is exhausted, when the `Location` is not ASCII,
    when it leaves the origin, and when a 301/302 on a non-GET/HEAD method would
    require rewriting the method -- refused rather than silently turned into a
    GET, because that would drop the body the caller asked to send.
    """


class DestinationRejected(ClientError):
    """A URL or a resolved address is outside `DestinationPolicy`.

    The SSRF guard. Raised from the constructor for a bad `base_url`, and per
    request from DNS resolution for an address the policy does not allow.
    """


@dataclass(frozen=True, slots=True)
class ClientLimits:
    """Sizes and counts one client will not exceed. Validated at construction.

    These are the client's share of the process, chosen per origin: a slow
    dependency can only ever hold `max_connections` sockets and
    `max_waiters` parked callers, whatever the rest of the application does.

    Args:
        max_connections: Sockets open to the origin at once, idle and in-flight together.
        max_keepalive_connections: Idle sockets retained for reuse. Cannot exceed `max_connections`.
        max_waiters: Callers parked on a full pool; the next one gets `PoolTimeout` immediately.
        max_request_header_bytes: Serialized request head ceiling; over it raises `ClientError`.
        max_response_header_bytes: Response head ceiling, also applied to chunked trailers.
        max_response_bytes: Decoded response-body ceiling; over it raises `ResponseTooLarge`.
        read_high_water: Transport read buffer, in bytes. Not a response-size limit.
        dns_cache_ttl: Seconds a resolution is reused. 0 re-resolves every connect.

    Raises:
        ValueError: Any count is non-positive, the TTL is negative, or keepalive exceeds max.
    """

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
class ClientTLS:
    """Where this client's trust comes from, as paths rather than a context.

    **Paths, not a built `ssl.SSLContext`, and that is what makes the fast path
    reachable.** There is no supported way to borrow OpenSSL's `SSL_CTX *` out
    of a Python context, so a client handed one can only take asyncio's TLS --
    a Python object per read and per write, measured at 2.14x the cost of the
    native path. Naming the material lets the reactor build its own. `TLSConfig`
    and the HTTP/3 backend already answer this the same way.

    Args:
        cafile: PEM bundle of trusted roots. None uses the system store.
        capath: Directory of hashed trusted roots.
        verify: Check the peer's chain and host name. Off is a decision that has
            to be typed out, because a client that skips the check is faster
            than one that does not and looks identical until it matters.
    """

    cafile: str | None = None
    capath: str | None = None
    verify: bool = True


@dataclass(frozen=True, slots=True)
class ClientTimeout:
    """Deadlines for one outbound request, in seconds. Validated at construction.

    `total` is the outer bound and the only one a caller can rely on: it wraps
    everything, including retry backoff sleeps and redirect hops, so the inner
    deadlines cannot compose into something longer. The inner ones exist to fail
    a *stalled* stage early enough to be worth retrying, which the outer one
    cannot distinguish.

    Args:
        pool: Waiting for a free connection; over it raises `PoolTimeout`.
        connect: TCP connect per candidate address; also bounds DNS resolution.
        tls: Handshake, added to `connect` for the combined per-address deadline.
        response_headers: Silence before the response head completes.
        response_body: Silence during the body, re-armed per read rather than total.
        total: The whole call, retries included. None removes the outer deadline.

    Raises:
        ValueError: Any value is non-positive (`total` may be None, but not 0).
    """

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
    """When and how often a failed attempt is repeated. Validated at construction.

    **Retries are off by default** (`attempts=1` means one attempt, not one
    retry). Turning them on requires deciding that repeating the request is safe,
    which is what `idempotent_only` encodes: with it set, only DELETE, GET,
    HEAD, OPTIONS, PUT and TRACE are repeated -- plus any request carrying an
    `idempotency_key`, because that key is the caller's promise that the origin
    will collapse a duplicate.

    Two kinds of failure are retried: a transport-level one (connect, DNS,
    response timeout, a connection that died mid-exchange) and a response whose
    status is in `statuses`. A `ProtocolError`, a `ResponseTooLarge`, or any
    other `ClientError` is final -- repeating it would fail identically.

    Backoff between attempts is `min(backoff_base * 2**attempt, backoff_cap)`.
    A `Retry-After` on a retryable response replaces that delay entirely, in
    either RFC 9110 §10.2.3 form (delta-seconds or an HTTP-date), but is clamped
    to `backoff_cap * 16` so one absurd header cannot park the caller. Each
    attempt also spends a token of the client's `RatePolicy`, so a retry
    storm is throttled by the same bucket as ordinary traffic.

    Args:
        attempts: Total attempts, not extra ones. 1 disables retrying.
        idempotent_only: Restrict retries to idempotent methods and keyed requests.
        statuses: Response statuses treated as retryable.
        backoff_base: First delay in seconds, doubled per attempt.
        backoff_cap: Ceiling for the computed delay, in seconds.
        jitter: Scale each delay by a uniform factor in [0.5, 1.0) to spread a thundering herd.
        respect_retry_after: Honour a `Retry-After` header in place of the computed delay.

    Raises:
        ValueError: `attempts` is non-positive, or either backoff value is non-positive.
    """

    attempts: int = 1
    idempotent_only: bool = True
    statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
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
    """Client-side outbound throttle. Off by default; validated at construction.

    One continuous token bucket shared by every request from this client
    (`rate` tokens per second, bursting to `capacity`), reusing the same
    `TokenBucket` primitive as the inbound rate limiter rather than a second
    implementation. A request that cannot take a token *parks* rather than being
    rejected, so this shapes traffic to a rate-limited dependency instead of
    turning its limit into the caller's errors -- but only up to `max_wait`,
    past which the wait becomes a `ClientError`.

    Retries spend tokens too: each attempt acquires before it is sent.

    Args:
        enabled: When False every other field is ignored and no bucket exists.
        capacity: Burst size in tokens. One request costs one token.
        rate: Sustained refill in tokens per second.
        max_wait: Seconds a request may park. Exceeding it raises `ClientError`.

    Raises:
        ValueError: `enabled` with a non-positive `rate`, `capacity`, or `max_wait`.
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
    """Parse a `Retry-After` header into seconds-from-now (RFC 9110 §10.2.3).

    Both forms are honoured: delta-seconds (`120`) and an HTTP-date
    (`Wed, 21 Oct 2026 07:28:00 GMT`), the latter converted to a non-negative
    delay relative to now. Anything unparseable returns None so the caller falls
    back to ordinary backoff.
    """
    if raw is None:
        return None
    text = raw.decode("latin-1").strip()
    try:
        seconds = int(text)
    except ValueError:
        return _http_date_delay(text)
    return float(seconds) if seconds >= 0 else None


def _http_date_delay(text: str) -> float | None:
    """Seconds from now until an HTTP-date, clamped at 0; None if unparseable."""
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class RedirectPolicy:
    """Whether 3xx responses are followed. Off by default; validated at construction.

    With redirects disabled the 3xx response is returned to the caller as-is,
    which is a valid way to use this client -- nothing is raised. Enabling them
    requires a hop limit, because the redirect loop is the client's own and an
    unbounded one is a hang.

    Method rewriting follows RFC 9110: a 303 becomes a GET with an empty body,
    while 307 and 308 preserve both. A 301 or 302 on anything other than GET or
    HEAD raises `RedirectError` instead of being rewritten, because rewriting
    would silently drop the body the caller asked to send.

    **Cross-origin redirects are never followed, and there is no option to.**
    The client is pinned to one origin: its pool, its TLS context, its
    destination policy, and its rate and retry budgets all belong to that
    origin, and following a hop off it would run the new origin's requests
    under the old one's everything. A `RedirectPolicy` therefore has no
    cross-origin flag -- one existed, and both of its values refused the hop,
    which read as a supported behaviour that was never implemented. Reaching
    another origin means constructing a client for it.

    Args:
        enabled: Follow 3xx responses. When False they are returned unchanged.
        max_hops: Redirects followed before `RedirectError`. Must be positive when enabled.

    Raises:
        ValueError: `max_hops` is negative, or redirects are enabled with `max_hops=0`.
    """

    enabled: bool = False
    max_hops: int = 0

    def __post_init__(self) -> None:
        if self.max_hops < 0:
            raise ValueError("redirect max_hops cannot be negative")
        if self.enabled and self.max_hops == 0:
            raise ValueError("enabled redirects require max_hops")


@dataclass(frozen=True, slots=True)
class TracePolicy:
    """Whether this client carries the calling request's trace context out.

    Propagation is on by default: a `traceparent` is how a downstream service's
    spans join this one's trace, and the W3C format exists to be sent. It is a
    *policy* rather than a constant for the same reason `DestinationPolicy` is
    -- an origin is not automatically inside your trust boundary, and a trace id
    is a correlation handle you may not want to hand a third party. Turn it off
    per client:

    ```python
    app.http_client("partner", base_url="https://partner.example",
                    trace=TracePolicy(propagate=False))
    ```

    `tracestate` is off by default and separate, because it is vendor key-value
    data rather than an identifier: it is larger, it is not always yours to
    forward, and forwarding it is a deliberate choice. It mirrors
    `wreath.telemetry.PropagationConfig.propagate_tracestate` at the client,
    which is where the destination is known.
    """

    propagate: bool = True
    tracestate: bool = False


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Where this client is permitted to connect. The SSRF guard.

    Checked in two places, because a name check alone is not a destination
    check: `validate_url` runs on the `base_url` at construction and on
    any absolute redirect target, and `validate_address` runs on *every*
    address DNS returned, before a connection is attempted. That second check is
    what a hostname resolving to 127.0.0.1 or 169.254.169.254 has to pass, and
    it is why a DNS answer is validated in full rather than only the address
    that happens to win the connection race. Addresses under the well-known
    NAT64 prefix and deprecated IPv4-compatible IPv6 literals are checked again
    as their translated IPv4 destination, so a globally classified IPv6 answer
    cannot tunnel to loopback or cloud metadata.

    The defaults deny every non-global address. Loopback is denied too, so a
    client aimed at `http://localhost` in a test needs
    `allow_loopback=True` -- the default is chosen for production, and a test
    opting in is a smaller mistake than a service reaching a metadata endpoint.

    Args:
        schemes: URL schemes permitted. Anything outside raises `DestinationRejected`.
        hosts: Allowed hostnames; `*.example.com` matches subdomains, not the apex. Empty: any.
        ports: Allowed ports, compared after the scheme default is applied. Empty allows any.
        allow_private: Permit RFC 1918 and other private ranges.
        allow_loopback: Permit 127.0.0.0/8 and ::1.
        allow_link_local: Permit 169.254.0.0/16 and fe80::/10, which include cloud metadata.
    """

    schemes: frozenset[str] = frozenset({"http", "https"})
    hosts: tuple[str, ...] = ()
    ports: frozenset[int] = frozenset()
    allow_private: bool = False
    allow_loopback: bool = False
    allow_link_local: bool = False

    def validate_url(self, parsed: SplitResult) -> None:
        """Check a parsed URL's scheme, credentials, host, and port.

        Userinfo (`https://user:pass@host`) is refused outright rather than
        stripped or forwarded: it would put a credential in a URL that gets
        logged, and no supported flow needs it.

        Hostnames are IDNA-encoded and lowercased before matching, so a
        Unicode or mixed-case host cannot slip past an allow-list entry.

        Args:
            parsed: The result of `urllib.parse.urlsplit`.

        Raises:
            DestinationRejected: Scheme, credentials, host, or port fail the policy.
            UnicodeError: The hostname is not encodable as IDNA.
        """
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
        """Check one resolved IP address against the policy.

        Deny-by-default on the last rule: an address that is not global and not
        covered by an explicit `allow_*` is refused even if it belongs to no
        named category, so a range this code does not enumerate fails closed.
        Unspecified (`0.0.0.0`, `::`) and multicast addresses are refused
        unconditionally -- no flag admits them.

        Args:
            value: A textual IP address; an IPv6 `%scope` suffix is stripped first.

        Raises:
            DestinationRejected: The address is outside what the policy permits.
            ValueError: `value` is not a valid IP address.
        """
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if address in _NAT64_WELL_KNOWN_PREFIX:
            translated = ipaddress.IPv4Address(address.packed[-4:])
            self._validate_concrete_address(translated)
        if address in _IPV4_COMPATIBLE_PREFIX and address != _IPV6_LOOPBACK:
            translated = ipaddress.IPv4Address(address.packed[-4:])
            self._validate_concrete_address(translated)
        self._validate_concrete_address(address)

    def _validate_concrete_address(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> None:
        """Apply policy to one address with no translation or recursion."""
        if address.is_unspecified or address.is_multicast:
            raise DestinationRejected("special destination address is not allowed")
        if address.is_loopback and not self.allow_loopback:
            raise DestinationRejected("loopback destination address is not allowed")
        if address.is_link_local and not self.allow_link_local:
            raise DestinationRejected("link-local destination address is not allowed")
        if (
            address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not self.allow_private
        ):
            raise DestinationRejected("private destination address is not allowed")
        if not address.is_global and not (
            (address.is_loopback and self.allow_loopback)
            or (address.is_link_local and self.allow_link_local)
            or (address.is_private and self.allow_private)
        ):
            raise DestinationRejected("non-global destination address is not allowed")


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    """A point-in-time reading of one client's pool. For health and metrics.

    Taken without a lock and without awaiting, so the fields are individually
    accurate and collectively approximate -- fine for a gauge, not a basis for a
    decision. `active + idle` is not the pool's open count: a connection being
    created belongs to neither set yet.

    Args:
        active: Connections currently carrying a request.
        idle: Connections held for reuse.
        waiters: Callers parked waiting for a connection right now.
        requests: Requests written since construction; monotonic, never reset.
        reused: Times an idle connection was taken instead of dialling; monotonic.
    """

    active: int
    idle: int
    waiters: int
    requests: int
    reused: int


class _StreamContext:
    """`HttpClient.stream`'s context manager, written out rather than generated.

    Written out rather than `@asynccontextmanager` because the entry and exit
    are genuinely two operations on a held connection, and a generator that
    suspends between them obscures that the connection is checked out for the
    whole block.
    """

    __slots__ = ("_body", "_client", "_connection", "_headers", "_method",
                 "_minor", "_response_headers", "_target")

    def __init__(
        self,
        client: HTTPClient,
        method: str,
        target: str,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes,
    ) -> None:
        self._client = client
        self._method = method.upper()
        self._target = target
        self._headers = headers
        self._body = body
        self._connection: Any = None
        self._minor = 1
        self._response_headers: list[tuple[bytes, bytes]] = []

    async def __aenter__(self) -> StreamingClientResponse:
        # The same guard `_send_with_retries` carries, and for the same reason:
        # the native HTTP/1 driver eagerly steps a Wreath request before it owns
        # an asyncio Task, and `asyncio.timeout` -- which every read below is
        # wrapped in -- requires a current task. Without this, the first
        # streaming call *from inside a handler* dies on "Timeout should be used
        # inside a task" while the buffered path beside it works, because that
        # path already does this.
        if asyncio.current_task() is None:
            await asyncio.sleep(0)
        client = self._client
        if not client._started:
            raise ClientClosed(f"HTTP client {client._name!r} is not started")
        request = _client_codec.serialize_request(
            self._method,
            client._request_target(self._target),
            client._authority().encode("ascii"),
            headers=client._propagated(self._headers),
            body=self._body,
        )
        if len(request) - len(self._body) > client._limits.max_request_header_bytes:
            raise ClientError("request headers exceed configured limit")
        connection = await client._acquire()
        self._connection = connection
        client._framed_cleanly = False
        try:
            connection.writer.write(request)
            await connection.writer.drain()
            client._requests += 1
            minor, status, reason, headers = await client._read_head(connection.reader)
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as error:
            await client._release(connection, False)
            self._connection = None
            raise _TransportError("connection failed during HTTP exchange") from error
        self._minor = minor
        self._response_headers = headers
        return StreamingClientResponse(
            status=status,
            headers=tuple(headers),
            http_version=f"1.{minor}",
            reason=reason,
            _chunks=client._iter_body(
                connection.reader, self._method, status, headers
            ),
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        connection = self._connection
        if connection is None:
            return False
        self._connection = None
        client = self._client
        # Reusable only when the body reached its declared end *and* the
        # framing allows it. A partially-read response leaves the socket
        # mid-message, and pooling it would hand the next caller a prefix.
        reusable = (
            exc_type is None
            and client._framed_cleanly
            and client._keeps_alive(self._minor, self._response_headers, True)
        )
        await client._release(connection, reusable)
        return False


@dataclass(frozen=True, slots=True)
class StreamingClientResponse:
    """A response whose head has arrived and whose body has not.

    Deliberately *not* a `ClientResponse`. That class documents itself as
    "Immutable, fully buffered" and callers rely on `.body`; giving this one a
    `.body` that sometimes worked would be worse than not having one.

    Yielded by `HttpClient.stream`, and only valid inside that block.
    """

    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    http_version: str
    #: `bytes`, matching `ClientResponse.reason`: it is a wire field with no
    #: declared charset, and picking one for someone else's header is how a
    #: client starts corrupting values.
    reason: bytes
    _chunks: Any

    def iter_bytes(self) -> Any:
        """The body, as it arrives. Iterate once."""
        return self._chunks

    def header(self, name: bytes) -> bytes | None:
        """The first value for `name`, or None. Names are lowercase on the wire."""
        for key, value in self.headers:
            if key == name:
                return value
        return None


@dataclass(frozen=True, slots=True)
class ClientResponse:
    """One complete outbound response. Immutable, fully buffered.

    `body` holds the decoded body in full -- the client has no streaming
    response API, and the read is bounded by
    `ClientLimits.max_response_bytes`. Informational (1xx) responses are
    consumed and discarded during the read, so `status` is always final.

    Headers stay raw `bytes` pairs in wire order with names lowercased,
    duplicates included, exactly as the origin sent them. Nothing is combined or
    decoded, because deciding a charset for someone else's header is how a
    client starts corrupting values.

    Args:
        headers: Response headers, lowercased names, in the order received.
        http_version: `"1.0"` or `"1.1"`, from the status line.
        reason: The reason phrase. Advisory; never parse it.
    """

    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    http_version: str
    reason: bytes = b""

    def header(self, name: bytes) -> bytes | None:
        """The first value for `name`, or None. Case-insensitive.

        A header sent more than once resolves to the first occurrence; iterate
        `headers` for the rest, which matters for `Set-Cookie`.
        """
        lowered = name.lower()
        for key, value in self.headers:
            if key == lowered:
                return value
        return None

    def raise_for_status(self) -> None:
        """Raise `ClientError` when the status is 400 or above; otherwise return None.

        Opt-in: the client itself never treats a status as an error, because a
        404 or a 409 is frequently the answer rather than a failure. 3xx does not
        raise here -- an unfollowed redirect is a response the caller asked for.

        Raises:
            ClientError: The status is >= 400. The message carries the status.
        """
        if self.status >= 400:
            raise ClientError(f"HTTP response status {self.status}")


class _NativeStreamWriter:
    """StreamWriter surface over a transport + native client stream.

    The native stream protocol owns backpressure futures (`_drain`,
    `_wait_closed`); writes go straight to the transport, which on the metal
    loop means the retained zero-copy send queue.
    """

    __slots__ = ("_transport", "_protocol")

    def __init__(self, transport: asyncio.Transport, protocol: Any) -> None:
        # create_connection hands back a write-capable Transport, so this is
        # typed Transport (not BaseTransport) and .write() resolves.
        self._transport = transport
        self._protocol = protocol

    def write(self, data: bytes) -> None:
        self._transport.write(data)

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
_DEFAULT_TRACE = TracePolicy()


def _host_matches(host: str, pattern: str) -> bool:
    normalized = pattern.encode("idna").decode("ascii").lower()
    if normalized.startswith("*."):
        suffix = normalized[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == normalized


class HTTPClient:
    """One bounded pool for a configured HTTP origin.

    The origin comes from `base_url` and is fixed for the client's life, as is
    every policy passed here. Any path in `base_url` becomes a prefix on every
    request target. Requests are origin-relative and validated as such: a target
    that is not `/`-prefixed, or is protocol-relative (`//host`), or is not
    ASCII, raises `ValueError` before anything is sent.

    Concurrency is bounded by `ClientLimits`, and connections are reused
    when the response was framed and the peer did not ask to close. Happy
    Eyeballs is applied across the addresses DNS returned: attempts are staggered
    250 ms apart, the first to connect wins, and the losers are closed.

    Not started at construction. `start` admits requests, `close`
    drains them, and the object is an async context manager over both. Every
    method is safe to call from many tasks at once.

    Args:
        name: Identifies this client in errors and diagnostics. Cannot be empty.
        base_url: Scheme, host, optional port, optional path prefix. No query or fragment.
        limits: Pool sizes and byte ceilings.
        timeout: Per-request deadlines.
        retry: When a failed attempt is repeated. Disabled by default.
        redirect: Whether 3xx is followed. Disabled by default.
        destination: Where connecting is permitted. Applied to `base_url` here.
        rate: Outbound throttle. Disabled by default.

    Raises:
        ValueError: `name` is empty, or `base_url` carries a query or fragment.
        DestinationRejected: `base_url` is outside `destination`.
    """

    __slots__ = (
        "_active",
        "_base_path",
        #: Set by `_iter_body` when a body reached its declared end. `stream`
        #: reads it to decide whether the connection may be pooled -- a
        #: partially-read response leaves the socket mid-message.
        "_framed_cleanly",
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
        "_tls",
        "_started",
        "_timeout",
        "_trace",
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
        trace: TracePolicy = _DEFAULT_TRACE,
        tls: ClientTLS | None = None,
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
        self._trace = trace
        # Latches propagation for the whole process: until a client exists there
        # is nothing to propagate *to*, and the request path must not pay a
        # `ContextVar.set` to discover that. Same shape as `_nplusone.WATCHING`.
        _telemetry.propagates()
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
        self._tls = tls or ClientTLS()
        self._active: set[_Connection] = set()
        self._idle: list[_Connection] = []
        self._open = 0
        self._waiters = 0
        self._requests = 0
        self._framed_cleanly = False
        self._reused = 0
        self._started = False

    @property
    def name(self) -> str:
        """The name this client was constructed with. Appears in its error messages."""
        return self._name

    @property
    def started(self) -> bool:
        """Whether the client is accepting requests. False before `start` and after `close`."""
        return self._started

    async def __aenter__(self) -> HTTPClient:
        await self.start()
        return self

    async def __aexit__(self, *_error: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Admit requests. Idempotent, and cheap enough to call from a lifespan hook.

        Opens no connections and resolves no names -- both happen on the first
        request. Starting is only a permission, so a client for an origin that is
        currently down still starts, and the failure surfaces at the call that
        needs it.

        A closed client can be started again; the pool is empty at that point,
        since `close` discarded it.
        """
        async with self._condition:
            if self._started:
                return
            self._started = True
            self._condition.notify_all()

    async def close(self) -> None:
        """Stop admitting requests, drop idle connections, wait for in-flight ones.

        Refusal is immediate: parked callers are woken with `ClientClosed` and a
        connect that completes after this point is closed rather than pooled.
        Idle sockets are closed at once; in-flight requests are then given
        `timeout.total` (or `timeout.response_body` when total is None) to
        finish on their own, after which their transports are closed underneath
        them and they fail rather than hang.

        A socket that errors while closing is ignored rather than propagated --
        shutting down is not a place to acquire a new failure. Idempotent, and
        safe on a client that was never started.
        """
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

    def counters(self) -> Any:
        """This client's pool counters, for `wreath.metrics.collect`."""
        from .metrics import Counters

        reading = self.snapshot()
        return Counters(
            subsystem="http_client",
            instance=self._name,
            values={
                "active": reading.active,
                "idle": reading.idle,
                "waiters": reading.waiters,
                "requests": reading.requests,
                "reused": reading.reused,
            },
        )

    def snapshot(self) -> ClientSnapshot:
        """A `ClientSnapshot` of the pool right now. Synchronous, never blocks."""
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
        """`GET` through `request`. Retried under an idempotent-only policy."""
        return await self.request("GET", target, headers=headers)

    async def post(
        self,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        """`POST` through `request`.

        POST is not idempotent, so an `idempotency_key` is what makes this
        retryable under the default policy: it is sent as an `Idempotency-Key`
        header and is the caller's assertion that the origin collapses
        duplicates. Without one, a POST is attempted exactly once.
        """
        return await self.request(
            "POST", target, headers=headers, body=body, idempotency_key=idempotency_key
        )

    def stream(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> _StreamContext:
        """Send one request and hand back the response before its body arrives.

        The half `request` cannot do. `request` reads the whole body into memory
        under `ClientLimits.max_response_bytes`, which is right for an API call
        and impossible for a proxy, a large download, or an SSE stream that
        never ends.

        ```python
        async with client.stream("GET", "/big") as response:
            async for chunk in response.iter_bytes():
                await sink.write(chunk)
        ```

        **The connection is held for the life of the block**, which is why this
        is a context manager and not a coroutine returning a response: the body
        is still on the socket, and releasing the connection early would hand
        the next caller a pool entry with someone else's bytes in front of it.

        A body left unread is not a bug, and the exit path handles it the only
        safe way: the connection is closed rather than pooled. Draining an
        unknown remainder to make it reusable would let an upstream decide how
        long a client blocks.

        **No retries and no redirects**, deliberately. Once a byte of the body
        has been delivered the request cannot be replayed, and a streaming call
        that silently restarted would deliver a prefix twice. Use `request` when
        those matter; they are what it is for.

        Args:
            method: Case-insensitive; uppercased before it goes on the wire.
            target: Origin-relative path, `/`-prefixed, ASCII/percent-encoded.
            headers: Raw byte pairs, sent in order after the client's own head.
            body: The request body, sent before the response is read.

        Returns:
            An async context manager yielding a `StreamingClientResponse`.
        """
        return _StreamContext(self, method, target, headers, bytes(body))

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        """Send one request to this client's origin and return the full response.

        The whole lifecycle happens here: rate-limit token, pool acquisition,
        DNS with policy validation, connect, TLS, write, read, retries, and
        redirects -- all inside `ClientTimeout.total`.

        The response is returned for *any* status. Nothing raises on a 4xx or
        5xx; call `ClientResponse.raise_for_status` when that is wanted.
        A 3xx is returned unchanged unless `RedirectPolicy` is enabled.

        The request is recorded as one phase in the flight recorder when the
        request that triggered it was armed, including when it fails or times
        out -- the duration of a failed call is precisely what the Inspector
        needs. An unarmed request pays one ContextVar read.

        Args:
            method: Case-insensitive; uppercased before it goes on the wire.
            target: Origin-relative path, `/`-prefixed, ASCII/percent-encoded.
            headers: Raw byte pairs, sent in order after the client's own head.
            body: Sent verbatim. Copied to `bytes` once, so a buffer may be reused after.
            idempotency_key: Sent as `Idempotency-Key`, and makes a non-idempotent method retryable.

        Returns:
            The response, body included.

        Raises:
            ClientClosed: The client is not started, or was closed mid-request.
            ValueError: `target` is not an origin-relative ASCII path.
            ClientError: The request headers exceed `ClientLimits.max_request_header_bytes`.
            PoolTimeout: No connection became free within `ClientTimeout.pool`.
            DNSFailure: The origin did not resolve.
            DestinationRejected: A resolved address is outside `DestinationPolicy`.
            TLSFailure: The handshake failed.
            ConnectError: Every candidate address failed to connect.
            ResponseTimeout: The origin stalled mid-response.
            RequestTimeout: The call exceeded `ClientTimeout.total`.
            ProtocolError: The response could not be framed.
            ResponseTooLarge: The body exceeded `ClientLimits.max_response_bytes`.
            RedirectError: A redirect could not be followed under `RedirectPolicy`.
        """
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
        # The native HTTP/1 driver eagerly steps a Wreath request before it owns
        # an asyncio Task.  A real suspension hands the continuation to one;
        # do that before entering asyncio.timeout(), which requires a current
        # task and otherwise makes Wreath's own inbound and outbound stacks
        # incompatible on the first client call from a handler.
        if asyncio.current_task() is None:
            await asyncio.sleep(0)
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

    def _propagated(
        self, headers: tuple[tuple[bytes, bytes], ...]
    ) -> tuple[tuple[bytes, bytes], ...]:
        """Add this request's trace context, unless there is a reason not to.

        Three reasons not to, in the order they are cheapest to check: this
        client was told not to; nothing in the process ever binds a context; or
        the caller wrote a `traceparent` of their own, which is an explicit
        decision the framework does not overrule.

        Applied once, before the redirect loop, so every hop of one outbound
        call carries the same context -- a redirect is the same causal step.
        """
        if not self._trace.propagate or not _telemetry.PROPAGATING:
            return headers
        context = _telemetry.outbound_context.get(None)
        if context is None:
            return headers
        if any(name.lower() == b"traceparent" for name, _ in headers):
            return headers
        parent, state = context
        headers = (*headers, (b"traceparent", parent.encode("ascii")))
        if self._trace.tracestate and state:
            headers = (*headers, (b"tracestate", state.encode("ascii")))
        return headers

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
        request_headers = self._propagated(request_headers)
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
        `RatePolicy.max_wait`). No-op unless rate limiting is enabled."""
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
        # `compute_backoff` rather than the arithmetic inline: it is the same
        # exponential the job runner, the message bus and the webhook dispatcher
        # retry on, and it was forked here with a *different* jitter
        # distribution -- multiplicative within [0.5x, 1.0x] against its own
        # symmetric +/- fraction. One distribution, so "wreath jitters its
        # retries" is one statement. `attempt` is 0-based here and 1-based
        # there, hence the +1.
        return compute_backoff(
            attempt + 1,
            kind="exp",
            base=policy.backoff_base,
            cap=policy.backoff_cap,
            jitter=0.25 if policy.jitter else 0.0,
        )

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
        except BaseException:  # re-raised; the counter must balance
            # `_open` was incremented before the connect attempt, so *any* exit
            # that is not a live connection has to give the slot back -- a
            # cancelled connect leaks a permit exactly as a refused one does,
            # and `except Exception` would miss it. The pool would then wedge at
            # its ceiling with no connections to show for it. Nothing swallowed.
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

    def _build_ssl_context(self) -> ssl.SSLContext:
        """The outbound context, native when the reactor can provide one.

        Built once per client, at first connect. The native context is an
        `ssl.SSLContext` as well as a C one, so it is safe on any loop -- a loop
        that does not recognise it simply uses the Python half. That is why this
        does not sniff which loop is running.
        """
        from .reactor import metal_tls_client_context

        tls = self._tls
        try:
            return metal_tls_client_context(
                cafile=tls.cafile, capath=tls.capath, verify=tls.verify)
        except RuntimeError:
            # The extension is absent -- the metal tier is Linux-only. Fall
            # through to the portable context rather than refusing https.
            pass
        context = ssl.create_default_context(cafile=tls.cafile, capath=tls.capath)
        if not tls.verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _connect(self) -> _Connection:
        addresses = await self._resolve()
        if self._scheme == "https" and self._ssl_context is None:
            self._ssl_context = self._build_ssl_context()
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

    async def _iter_body(
        self,
        reader: asyncio.StreamReader,
        method: str,
        status: int,
        headers: list[tuple[bytes, bytes]],
    ) -> Any:
        """Yield the body as it arrives, and finish by yielding nothing.

        The streaming twin of `_read_body`, framed by the same
        `_client_codec.response_framing` call so the two cannot disagree about
        where a body ends -- which is the disagreement a proxy turns into a
        desync.

        `max_response_bytes` is deliberately **not** applied. It exists to stop
        a buffered read from eating the heap, and nothing is buffered here; a
        caller streaming a 2 GB file has already said that is what it wants.
        The bound that still applies is the caller's own: it stops iterating.
        """
        mode, length = _client_codec.response_framing(method, status, headers)
        if mode == "none":
            self._framed_cleanly = True
            return
        if mode == "chunked":
            async for chunk in self._iter_chunked(reader):
                yield chunk
            self._framed_cleanly = True
            return
        if mode == "length":
            remaining = length
            while remaining > 0:
                chunk = await _timed(
                    reader.read(min(64 * 1024, remaining)), self._timeout.response_body
                )
                if not chunk:
                    raise ProtocolError("upstream closed mid-body")
                remaining -= len(chunk)
                yield chunk
            self._framed_cleanly = True
            return
        # Close-delimited: the end *is* the close, so the connection can never
        # be reused afterwards however cleanly it ended.
        while True:
            chunk = await _timed(reader.read(64 * 1024), self._timeout.response_body)
            if not chunk:
                return
            yield chunk

    async def _iter_chunked(self, reader: asyncio.StreamReader) -> Any:
        """Yield chunk payloads, verifying every size line and the trailers."""
        while True:
            line = await _timed(reader.readuntil(b"\r\n"), self._timeout.response_body)
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
                        return
                return
            remaining = size
            while remaining > 0:
                chunk = await _timed(
                    reader.read(min(64 * 1024, remaining)), self._timeout.response_body
                )
                if not chunk:
                    raise ProtocolError("upstream closed mid-chunk")
                remaining -= len(chunk)
                yield chunk
            terminator = await _timed(reader.readexactly(2), self._timeout.response_body)
            if terminator != b"\r\n":
                raise ProtocolError("chunk not terminated by CRLF")

    async def _read_head(
        self, reader: asyncio.StreamReader
    ) -> tuple[int, int, bytes, list[tuple[bytes, bytes]]]:
        """Read one final response head, discarding any 1xx that precede it.

        Split out of `_read_response` so the streaming path can stop here. The
        two must not drift: a streamed response that parsed its head by a second
        set of rules would accept heads the buffered path refuses.
        """
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
                return minor, status, reason, headers

    @staticmethod
    def _keeps_alive(minor: int, headers: list[tuple[bytes, bytes]], framed: bool) -> bool:
        """Whether this connection may go back in the pool.

        Both readers use this. It was extracted for the streaming path and
        `_read_response` was left with an inline copy of the same three lines --
        two spellings of one condition, and the `framed` operand was dead in the
        only caller that remained, because streaming always passes True. A
        mutation run noticed: dropping `framed` changed no test's mind.

        `framed` is False for a close-delimited body, where the end of the
        response *is* the close, so the connection can never be reused however
        cleanly it ended.

        **That operand survives mutation, and it is redundant rather than
        untested.** `framed` is False only for a close-delimited body, which by
        definition ends at EOF -- and `_release` independently refuses to pool a
        connection whose `reader.at_eof()`. So dropping the clause changes no
        observable behaviour, and no test can distinguish it. It is kept because
        this is where reusability is *decided* and saying "a close-delimited
        response is reusable" here would be false; `_release`'s check is about
        transport state, not about framing. Recorded so the next reader does not
        spend a session re-deriving it.
        """
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
        return reusable

    async def _read_response(
        self, reader: asyncio.StreamReader, method: str
    ) -> tuple[ClientResponse, bool]:
        minor, status, reason, headers = await self._read_head(reader)
        body, framed = await self._read_body(reader, method, status, headers)
        return (
            ClientResponse(status, tuple(headers), body, f"1.{minor}", reason),
            self._keeps_alive(minor, headers, framed),
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
    "ClientTLS",
    "ClientTimeout",
    "ConnectError",
    "DNSFailure",
    "DestinationPolicy",
    "StreamingClientResponse",
    "DestinationRejected",
    "HTTPClient",
    "PoolTimeout",
    "ProtocolError",
    "RedirectError",
    "RatePolicy",
    "RedirectPolicy",
    "RequestTimeout",
    "ResponseTimeout",
    "ResponseTooLarge",
    "RetryPolicy",
    "TLSFailure",
]
