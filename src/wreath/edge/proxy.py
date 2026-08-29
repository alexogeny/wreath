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

from ..http_client import ClientError, HTTPClient
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
IDEMPOTENT: frozenset[str] = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})

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
        "_max_body",
        "_pool",
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
        self._max_body = max_body
        self._attempts = max(1, attempts)
        self._buffer_below = buffer_below

    async def __call__(self, request: Any) -> Response | StreamingResponse:
        """Proxy one request. Returns the upstream's response, streamed."""
        method = request.method.upper()
        # A method that cannot carry a body does not pay to ask for one. `body()`
        # is not free even when there is nothing to read: it is a coroutine, and
        # awaiting one per request to be handed `b""` is the kind of cost that
        # only shows up in aggregate.
        if method in _BODYLESS:
            body = b""
        else:
            body = await request.body()
            if len(body) > self._max_body:
                return Response(b"", status=413)

        headers = self._outbound(request)
        target = request.path
        query = request.query_string
        if query:
            target = f"{target}?{query.decode('latin-1') if isinstance(query, bytes) else query}"
        # Retryable only while nothing has been sent to the client yet, which is
        # exactly up to the moment the head comes back. After that the response
        # is already going out and a second attempt would deliver a prefix twice.
        retryable = method in IDEMPOTENT or request.header("idempotency-key")

        tried: frozenset[str] = frozenset()
        last = "no upstream available"
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
                if not retryable:
                    break
                continue
            headers = forwardable(response.headers)
            small = self._buffered_length(response)
            if small is not None:
                return await self._buffered(stream, response, upstream, started, headers)
            return StreamingResponse(
                self._relay(stream, response, upstream, started),
                status=response.status,
                headers=headers,
            )
        # 502, because the request was fine and the upstreams were not. The
        # distinction matters to whoever reads the log: a 4xx here would blame
        # the client for the proxy's own dependency.
        return Response(last.encode("utf-8"), status=502)

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
