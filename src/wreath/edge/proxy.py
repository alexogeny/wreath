"""The reverse proxy itself: one request in, one upstream chosen, bytes relayed.

Built on `HttpClient.stream`, which is the piece that made this possible: a
proxy cannot buffer a response, and `request` reads whole bodies into memory
under `max_response_bytes`.

**Request bodies are still bounded**, and that is the honest edge of this
prototype. `serialize_request` takes `bytes`, so an upload is read into memory
under `max_body_bytes` here and refused above it. Streaming the request half
needs the client's write path to accept an async iterator, which is the same
shape of work again.
"""

from __future__ import annotations

import time
from typing import Any

from .._structured_fields import parse_boolean_item
from ..http_client import (
    ClientClosed,
    ClientError,
    ConnectError,
    DestinationRejected,
    DNSFailure,
    HTTPClient,
    PoolTimeout,
    ProtocolError,
    RequestTimeout,
    ResponseTimeout,
    TLSFailure,
)
from ..proxy_status import ProxyStatus
from ..response import Response, StreamingResponse
from .headers import forwardable, request_headers, via_token
from .upstream import Upstream, UpstreamPool

#: Request body a proxied call may carry. Over it the proxy answers 413 rather
#: than reading an unbounded upload into memory on the origin's behalf.
DEFAULT_MAX_BODY = 8 * 1024 * 1024

#: What `Via` calls this hop.
DEFAULT_VIA_NAME = "wreath"

#: Methods a failed attempt may be retried on another upstream. RFC 9110 §9.2.2:
#: these are the ones defined to have the same effect whether applied once or
#: several times, so sending one twice cannot create a second order.
#:
#: `POST` is absent and stays absent. A client that knows its POST is safe to
#: repeat says so with `Idempotency-Key`, which `wreath.policy.idempotency`
#: already speaks -- and that is a claim only the client can make.
IDEMPOTENT: frozenset[str] = frozenset(
    {"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "QUERY", "TRACE"}
)

#: Methods with no request body to read. `GET` with a body is legal and
#: meaningless -- RFC 9110 §9.3.1 says content on one has no defined semantics --
#: so nothing is lost by not looking, and every proxied read saves a coroutine.
_BODYLESS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "DELETE"})

#: The two schemes a request can arrive on, pre-encoded so the hot path never
#: does it. Anything else falls back to encoding per request.
_SCHEMES: dict[str, bytes] = {"http": b"http", "https": b"https"}

#: Responses at or under this declared length are read into memory and sent as
#: one buffered reply instead of a streamed one.
#:
#: Streaming a 200-byte JSON body costs three nested async generators, a
#: `StreamingResponse`, and a chunked frame per read -- machinery that exists for
#: the 2 GB download and is pure overhead for the response most proxies actually
#: carry. Buffering also returns the upstream connection to the pool *before*
#: the reply is written to the client rather than after, which is why nginx
#: buffers proxied responses by default.
#:
#: Only when `Content-Length` says so. A chunked or close-delimited body has no
#: declared size, and guessing one is how a proxy turns an unbounded upstream
#: into an unbounded allocation.
DEFAULT_BUFFER_BELOW = 64 * 1024

#: Upstreams one request may try before the proxy gives up. Two, not more: the
#: point is to survive *one* bad origin, and a client waiting on a proxy walking
#: a whole pool would rather have the 502 and retry itself.
DEFAULT_ATTEMPTS = 2


class ReverseProxy:
    """Relay a request to one of `pool`'s upstreams and stream the reply back.

    One `HTTPClient` per upstream, held for the proxy's life: the client owns
    the connection pool, and building one per request would give away keep-alive
    -- which is most of what a proxy is for.

    Args:
        pool: The upstreams and the policy for choosing between them.
        clients: One started `HTTPClient` per upstream URL.
        via_name: The token this hop writes into `Via`.
        max_body: Largest request body relayed; over it, 413.
    """

    __slots__ = (
        "_attempts",
        "_buffer_below",
        "_clients",
        "_incremental_refused",
        "_max_body",
        "_pool",
        "_proxy_name",
        "_via",
    )

    def __init__(
        self,
        pool: UpstreamPool,
        clients: dict[str, HTTPClient],
        *,
        via_name: str = DEFAULT_VIA_NAME,
        max_body: int = DEFAULT_MAX_BODY,
        attempts: int = DEFAULT_ATTEMPTS,
        buffer_below: int = DEFAULT_BUFFER_BELOW,
    ) -> None:
        if max_body < 0:
            raise ValueError("max_body must be non-negative")
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if buffer_below < 0:
            raise ValueError("buffer_below must be non-negative")
        missing = [u.url for u in pool.upstreams if u.url not in clients]
        if missing:
            # At construction, not at the first request: a pool with no client
            # for one member answers every request until that member is chosen,
            # and then fails for a reason nobody connects to this line.
            raise ValueError(f"no client for upstream(s): {', '.join(missing)}")
        self._pool = pool
        self._clients = clients
        # Encoded once: it is `"1.1 <name>"` for the life of the proxy, and
        # building it per request was an f-string plus an encode on the hot path.
        self._via = via_token("1.1", via_name)
        self._proxy_name = via_name
        self._incremental_refused = ProxyStatus(
            via_name,
            error="incremental_refused",
        ).to_header()
        self._max_body = max_body
        self._attempts = attempts
        self._buffer_below = buffer_below

    async def __call__(self, request: Any) -> Response | StreamingResponse:
        """Proxy one request. Returns the upstream's response, streamed."""
        incremental, idempotency_key = _request_controls(request.headers)
        if incremental is True:
            return Response(
                b"",
                status=501,
                headers=[(b"proxy-status", self._incremental_refused)],
            )
        method = request.method.upper()
        # A method that cannot carry a body does not pay to ask for one. `body()`
        # is not free even when there is nothing to read: it is a coroutine, and
        # awaiting one per request to be handed `b""` is the kind of cost that
        # only shows up in aggregate.
        if method in _BODYLESS:
            body = b""
        else:
            body = await self._read_body(request)
            if body is None:
                return Response(b"", status=413)

        headers = self._outbound(request)
        scope = getattr(request, "scope", None)
        raw_path = scope.get("raw_path") if isinstance(scope, dict) else None
        target = raw_path.decode("ascii") if isinstance(raw_path, bytes) else request.path
        query = request.query_string
        if query:
            target = f"{target}?{query.decode('latin-1') if isinstance(query, bytes) else query}"
        # Retryable only while nothing has been sent to the client yet, which is
        # exactly up to the moment the head comes back. After that the response
        # is already going out and a second attempt would deliver a prefix twice.
        retryable = method in IDEMPOTENT or idempotency_key is not None

        tried: frozenset[str] = frozenset()
        last = "no upstream available"
        proxy_error = "destination_unavailable"
        for _ in range(self._attempts):
            upstream = self._pool.choose(exclude=tried)
            if upstream is None:
                break
            # A frozenset built per *attempt* rather than a set mutated and
            # re-frozen per attempt: the common case is one attempt, where this
            # allocates nothing at all.
            tried = tried | {upstream.url}
            client = self._clients[upstream.url]
            started = time.monotonic()
            upstream.inflight += 1
            try:
                stream = client.stream(method, target, headers=tuple(headers), body=body)
                response = await stream.__aenter__()
            except (ClientError, OSError) as error:
                upstream.inflight -= 1
                self._pool.failed(upstream)
                last = str(error)
                proxy_error = _proxy_error(error)
                if not retryable:
                    break
                continue
            incremental, headers = _response_incremental_headers(forwardable(response.headers))
            small = None if incremental is True else self._buffered_length(response)
            if small is not None:
                return await self._buffered(stream, response, upstream, started, headers)
            return StreamingResponse(
                self._relay(stream, response, upstream, started),
                status=response.status,
                headers=headers,
                incremental=incremental is not False,
            )
        # 502, because the request was fine and the upstreams were not. The
        # distinction matters to whoever reads the log: a 4xx here would blame
        # the client for the proxy's own dependency.
        return Response(
            last.encode("utf-8"),
            status=502,
            headers=[
                (
                    b"proxy-status",
                    ProxyStatus(self._proxy_name, error=proxy_error).to_header(),
                )
            ],
        )

    async def _read_body(self, request: Any) -> bytes | None:
        first_chunk = None
        buffer = None
        total = 0
        stream = request.stream()
        async for chunk in stream:
            if not chunk:
                continue
            if len(chunk) > self._max_body - total:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
                return None
            total += len(chunk)
            if first_chunk is None and buffer is None:
                first_chunk = chunk
                continue
            if buffer is None:
                if first_chunk is None:
                    raise RuntimeError("proxy body collector lost its first chunk")
                buffer = bytearray(first_chunk)
                first_chunk = None
            buffer.extend(chunk)
        if buffer is not None:
            return bytes(buffer)
        return first_chunk if first_chunk is not None else b""

    def _buffered_length(self, response: Any) -> int | None:
        """The declared body size when it is small enough to read in one go."""
        declared = response.header(b"content-length")
        if declared is None:
            return None
        try:
            length = int(declared)
        except ValueError:
            # Not a number. Leave it to the streaming path, whose framing call
            # will refuse it properly rather than guessing here.
            return None
        return length if 0 <= length <= self._buffer_below else None

    async def _buffered(
        self,
        stream: Any,
        response: Any,
        upstream: Upstream,
        started: float,
        headers: list[tuple[bytes, bytes]],
    ) -> Response:
        """Read a small body, release the connection, and reply in one piece."""
        try:
            chunks = [chunk async for chunk in response.iter_bytes()]
        except ClientError, OSError:
            self._pool.failed(upstream)
            raise
        finally:
            upstream.inflight -= 1
            await stream.__aexit__(None, None, None)
        self._pool.succeeded(upstream, time.monotonic() - started)
        return Response(b"".join(chunks), status=response.status, headers=headers)

    async def _relay(self, stream: Any, response: Any, upstream: Upstream, started: float) -> Any:
        """Yield the upstream body, then account for the request either way.

        The bookkeeping lives here rather than beside the `choose` call because
        a streamed response is not finished when the handler returns -- it is
        finished when the last chunk leaves, and a latency sample taken any
        earlier measures the head and calls it the request.
        """
        try:
            async for chunk in response.iter_bytes():
                yield chunk
        except ClientError, OSError:
            self._pool.failed(upstream)
            raise
        else:
            self._pool.succeeded(upstream, time.monotonic() - started)
        finally:
            upstream.inflight -= 1
            await stream.__aexit__(None, None, None)

    def _outbound(self, request: Any) -> list[tuple[bytes, bytes]]:
        """The headers to send upstream: relayed, minus hop-by-hop, plus ours.

        `Host` and `Content-Length` never survive. The first because the
        outbound `Host` is the upstream's authority, and relaying the client's
        would ask one origin to answer for another's name. The second because
        **the outbound framing must describe what this proxy actually sends** --
        forwarding a claimed length is how a proxy and an origin come to
        disagree about where one message ends, which is the whole of request
        smuggling. `HTTPClient` writes both, and the codec refuses a
        caller-supplied one outright rather than sending two.
        """
        client = request.client
        return request_headers(
            request.headers,
            client=client[0] if client else None,
            scheme=_SCHEMES.get(request.scheme) or request.scheme.encode("latin-1"),
            via=self._via,
        )


def _proxy_error(error: ClientError | OSError) -> str:
    if isinstance(error, DNSFailure):
        return "dns_error"
    if isinstance(error, TLSFailure):
        return "tls_protocol_error"
    if isinstance(error, DestinationRejected):
        return "destination_ip_prohibited"
    if isinstance(error, PoolTimeout):
        return "connection_limit_reached"
    if isinstance(error, RequestTimeout):
        return "connection_timeout"
    if isinstance(error, ResponseTimeout):
        return "connection_read_timeout"
    if isinstance(error, ProtocolError):
        return "http_protocol_error"
    if isinstance(error, ConnectError):
        return "destination_unavailable"
    if isinstance(error, ClientClosed):
        return "proxy_internal_error"
    if isinstance(error, OSError):
        return "connection_terminated"
    return "proxy_internal_error"


def _request_controls(headers: list[tuple[bytes, bytes]]) -> tuple[bool | None, bytes | None]:
    incremental = None
    idempotency_key = None
    idempotency_valid = True
    for name, candidate in headers:
        if name == b"incremental":
            if incremental is not None:
                incremental = b""
            else:
                incremental = candidate
        elif name == b"idempotency-key":
            if idempotency_key is not None:
                idempotency_valid = False
            else:
                idempotency_key = candidate
    preference = parse_boolean_item(incremental) if incremental else None
    if not idempotency_valid or not idempotency_key or b"," in idempotency_key:
        idempotency_key = None
    return preference, idempotency_key


def _response_incremental_headers(
    headers: list[tuple[bytes, bytes]],
) -> tuple[bool | None, list[tuple[bytes, bytes]]]:
    values: list[bytes] = []
    forwarded: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if name.lower() == b"incremental":
            values.append(value)
        else:
            forwarded.append((name, value))
    preference = parse_boolean_item(values[0]) if len(values) == 1 else None
    return preference, forwarded
