"""Serve gRPC methods over the native HTTP/2 server.

gRPC is HTTP/2 plus three conventions: a five-byte length prefix in front of
every message, a `grpc-status` in the response *trailers*, and a small set of
`grpc-`-prefixed headers. Wreath's native HTTP/2 server already speaks the hard
part -- it emits response trailers (`http.response.trailers`) and validates the
`te: trailers` a gRPC client is required to send -- so this module is ordinary
Python over the ASGI messages that server already understands. There is no C
here and no change to the server.

**This is a server-tier feature.** `wreath.server` is the only conforming server
in the project that implements HTTP/2, and the ASGI trailers extension is what
carries `grpc-status`. Running a gRPC service behind a foreign ASGI server --
uvicorn, hypercorn -- fails at startup with `GrpcUnsupported` naming the reason,
rather than answering requests that no client can interpret. See
`docs/guides/grpc.md`.

A method is a **route**. `GrpcService.router()` returns an ordinary `Router`
whose routes are `POST /{service}/{method}`, so `roles=`, `dependencies=`,
`rate_limit=` and `@authorize`'s `action=` mean exactly what they mean on a REST
route, are enforced by the same middleware tape, and are read by the same
`permissions_router` and `wreath mutant`. There is no second authorization
model.

    from wreath.grpc import GrpcService
    from wreath.protobuf import message, field

    @message
    class PositionRequest:
        collar_id: int = field(1)

    tracker = GrpcService("camera.Tracker")

    @tracker.unary(request=PositionRequest, response=Position, action="read")
    async def GetPosition(request, message: PositionRequest) -> Position: ...

    app.include_router(tracker.router())

**Not built:** server reflection, gRPC-Web, the health-checking protocol, and
client-side concerns (load balancing, retry configuration, xDS). Reflection is
not an oversight -- it requires protobuf *descriptors*, which `wreath.protobuf`
deliberately does not build, so the two decisions are coupled and would have to
be reopened together.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from enum import IntEnum
from typing import Any

from . import protobuf as _protobuf
from ._auth import requirements as _requirements
from .exceptions import HTTPException
from .response import StreamingResponse
from .router import Router

__all__ = [
    "GrpcError",
    "GrpcService",
    "GrpcUnsupported",
    "Status",
    "frame_message",
    "parse_timeout",
]

#: The wire content types a gRPC client sends and this server answers with.
#: `application/grpc` is the bare form; `+proto` names the message encoding.
CONTENT_TYPE = "application/grpc+proto"
_ACCEPTED_CONTENT_TYPES = frozenset({"application/grpc", "application/grpc+proto"})

#: One message's header: a compressed flag byte, then a four-byte big-endian
#: length. The length is attacker-controlled on a public endpoint, so every
#: read of it is bounded against `max_message_bytes` *before* anything is
#: allocated -- see `Unframer.feed`.
_PREFIX_BYTES = 5

#: Default ceiling on one decoded message. gRPC's own default is 4 MiB and
#: clients expect to be refused rather than truncated past it.
DEFAULT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class Status(IntEnum):
    """The gRPC status codes, from the specification's status-codes table.

    These are the values that travel in the `grpc-status` trailer. `OK` is
    zero and is sent on success -- a gRPC call that succeeded still carries an
    explicit status, which is why the trailer is mandatory rather than an
    error-only affordance.
    """

    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


class GrpcError(Exception):
    """Refuse a call with an explicit gRPC status.

    Raise this from a method body when the refusal is the answer. Anything else
    that escapes a handler is mapped by `status_for`, which is deliberately
    conservative: an unrecognised exception becomes `UNKNOWN`, never a status
    that would tell a client the call is safe to retry.
    """

    def __init__(self, status: Status, message: str = "") -> None:
        super().__init__(f"{status.name}: {message}" if message else status.name)
        self.status = status
        self.message = message


class GrpcUnsupported(RuntimeError):
    """The running server cannot carry gRPC.

    Raised at startup rather than per request. gRPC needs HTTP/2 *and* response
    trailers; a server offering neither would answer with a body no client can
    read, and a 200 full of unreadable bytes is a worse failure than a refusal
    naming the reason.
    """


#: HTTP exception -> gRPC status. Anything absent maps to UNKNOWN, and the
#: mapping is one-way on purpose: a wreath 422 is INVALID_ARGUMENT because the
#: caller sent something the contract refuses, and a 429 is RESOURCE_EXHAUSTED
#: because that is the code a gRPC client's backoff already understands.
_STATUS_BY_HTTP: dict[int, Status] = {
    400: Status.INVALID_ARGUMENT,
    401: Status.UNAUTHENTICATED,
    403: Status.PERMISSION_DENIED,
    404: Status.NOT_FOUND,
    405: Status.UNIMPLEMENTED,
    409: Status.ABORTED,
    413: Status.RESOURCE_EXHAUSTED,
    422: Status.INVALID_ARGUMENT,
    429: Status.RESOURCE_EXHAUSTED,
    499: Status.CANCELLED,
    500: Status.INTERNAL,
    501: Status.UNIMPLEMENTED,
    503: Status.UNAVAILABLE,
    504: Status.DEADLINE_EXCEEDED,
}


def status_for(exc: BaseException) -> tuple[Status, str]:
    """The status and message a raised exception should answer with."""
    if isinstance(exc, GrpcError):
        return exc.status, exc.message
    if isinstance(exc, HTTPException):
        # `HTTPException.status` is a class attribute, not `status_code`.
        return _STATUS_BY_HTTP.get(exc.status, Status.UNKNOWN), exc.detail or ""
    if isinstance(exc, TimeoutError):
        return Status.DEADLINE_EXCEEDED, "deadline exceeded"
    return Status.UNKNOWN, ""


# --- message framing --------------------------------------------------------


def frame_message(payload: bytes, *, compressed: bool = False) -> bytes:
    """Prefix one encoded message with its gRPC five-byte header."""
    length = len(payload)
    if length > 0xFFFFFFFF:
        raise GrpcError(Status.RESOURCE_EXHAUSTED, "message exceeds the wire length field")
    return bytes((1 if compressed else 0,)) + length.to_bytes(4, "big") + payload


class Unframer:
    """Incremental reader for a stream of length-prefixed gRPC messages.

    Fed arbitrary chunk boundaries -- a message may span several DATA frames and
    several messages may share one -- and yields complete payloads. The declared
    length is checked against `max_message_bytes` **before** the buffer is
    allowed to grow to it, so a four-byte lie cannot make the server allocate
    what the peer never intends to send.
    """

    def __init__(self, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        self._buffer = bytearray()
        self._max = max_message_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add bytes; return every message that completed."""
        self._buffer.extend(chunk)
        out: list[bytes] = []
        while True:
            if len(self._buffer) < _PREFIX_BYTES:
                return out
            compressed = self._buffer[0]
            length = int.from_bytes(self._buffer[1:5], "big")
            if length > self._max:
                raise GrpcError(
                    Status.RESOURCE_EXHAUSTED,
                    f"message of {length} bytes exceeds the {self._max}-byte limit",
                )
            if compressed:
                # `grpc-encoding` negotiation is not implemented, so a peer that
                # sets the flag is told plainly rather than handed a decode
                # failure from the codec three layers down.
                raise GrpcError(
                    Status.UNIMPLEMENTED, "compressed messages are not supported"
                )
            if len(self._buffer) < _PREFIX_BYTES + length:
                return out
            out.append(bytes(self._buffer[_PREFIX_BYTES : _PREFIX_BYTES + length]))
            del self._buffer[: _PREFIX_BYTES + length]

    def finish(self) -> None:
        """Refuse a trailing partial message rather than dropping it."""
        if self._buffer:
            raise GrpcError(
                Status.INTERNAL,
                f"stream ended mid-message with {len(self._buffer)} bytes buffered",
            )


# --- deadlines --------------------------------------------------------------

#: `grpc-timeout` is a positive integer and a one-character unit.
_TIMEOUT = re.compile(r"^(\d{1,8})([HMSmun])$")

_TIMEOUT_SCALE = {
    "H": 3600.0,
    "M": 60.0,
    "S": 1.0,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
}


def parse_timeout(value: str) -> float:
    """Seconds from a `grpc-timeout` header value, e.g. `100m` or `5S`.

    A client that sent a deadline has already decided when to give up, so an
    unparseable value is refused rather than ignored -- silently treating it as
    "no deadline" would let a call outlive the caller that is waiting on it.
    """
    match = _TIMEOUT.match(value)
    if match is None:
        raise GrpcError(Status.INVALID_ARGUMENT, f"malformed grpc-timeout: {value!r}")
    return int(match.group(1)) * _TIMEOUT_SCALE[match.group(2)]


# --- trailer encoding -------------------------------------------------------

#: `grpc-message` is percent-encoded: the spec restricts it to a byte range
#: narrower than a header nominally allows, and an unescaped newline would let
#: an error string forge a header.
_SAFE = frozenset(
    b"abcdefghijklmnopqrstuvwxyz"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    b"0123456789"
    b" !\"#$&'()*+,-./:;<=>?@[]^_`{|}~"
)


def percent_encode(text: str) -> str:
    """Percent-encode a `grpc-message` value per the gRPC wire specification."""
    out: list[str] = []
    for byte in text.encode("utf-8"):
        out.append(chr(byte) if byte in _SAFE else f"%{byte:02X}")
    return "".join(out)


#: Asked of `wreath.protobuf` rather than read off the class, so there is one
#: notion of what a message is. This used to read the private plan marker
#: directly, which meant the answer lived in two modules and would have drifted
#: the first time the marker moved.
_is_message = _protobuf.is_message


# --- the response ------------------------------------------------------------


class _GrpcResponse(StreamingResponse):
    """Emit framed messages then a `grpc-status` trailer.

    Drives the ASGI messages itself, which is the only way to reach the trailers
    the native HTTP/2 server implements: `http.response.start` carries
    `trailers: True`, bodies follow, and `http.response.trailers` closes the
    stream. Nothing else in `wreath.response` sends those, because REST has no
    use for them.

    Subclasses `StreamingResponse` rather than standing alone because the
    application's return-value coercion is a **closed** `isinstance` check over
    `Response`, `StreamingResponse`, `FileResponse` and `PreparedResponse` --
    duck-typing a `__call__(send)` is not enough. Inheriting also picks up the
    deferred-cleanup contract, which is what releases a request-scoped database
    connection a streaming handler borrowed.

    The HTTP status is **always 200**, including for a refusal. In gRPC the
    transport succeeded whenever the server was reached, and the call's outcome
    is the `grpc-status` trailer; a 4xx here would make a client report a
    transport failure for an application-level refusal.
    """

    def __init__(
        self,
        body: AsyncIterator[bytes],
        *,
        status: Status = Status.OK,
        message: str = "",
    ) -> None:
        super().__init__(
            body,
            status=200,
            headers=[
                (b"content-type", CONTENT_TYPE.encode("ascii")),
                # Stated up front so a client knows this server will not compress.
                (b"grpc-accept-encoding", b"identity"),
            ],
        )
        self.grpc_status = status
        self.grpc_message = message

    async def __call__(self, send: Any) -> None:
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": self.headers,
                    "trailers": True,
                }
            )
            status, message = self.grpc_status, self.grpc_message
            try:
                async for chunk in self.body:
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
            except (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - every failure becomes a status
                # A handler that raises mid-stream has already sent bytes, so
                # the trailer is the only channel left for the reason.
                # Re-raising would abort the stream with no status at all, which
                # a client reports as an unexplained transport error rather than
                # the refusal it actually was.
                status, message = status_for(exc)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            trailers = [(b"grpc-status", str(int(status)).encode("ascii"))]
            if message:
                trailers.append(
                    (b"grpc-message", percent_encode(message).encode("ascii"))
                )
            await send({"type": "http.response.trailers", "headers": trailers})
        finally:
            cleanup = self._cleanup
            if cleanup is not None:
                self._cleanup = None
                await cleanup()


async def _one(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover - never reached; makes this a generator


# --- the service -------------------------------------------------------------

_UNARY, _SERVER_STREAM, _CLIENT_STREAM, _BIDI = range(4)


class GrpcService:
    """A named collection of gRPC methods, served as ordinary routes.

    `name` is the fully-qualified protobuf service name (`camera.Tracker`) and
    it is also the path prefix, because a gRPC path *is* `/{service}/{method}`.
    """

    def __init__(
        self, name: str, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    ) -> None:
        if not name or name.startswith("/"):
            raise ValueError(
                "service name must be a bare protobuf name like 'camera.Tracker', "
                f"got {name!r}"
            )
        self.name = name
        self.max_message_bytes = max_message_bytes
        self._methods: list[tuple[str, int, type, type, Any, dict[str, Any]]] = []

    def _register(
        self, kind: int, request: type, response: type, metadata: dict[str, Any]
    ) -> Callable[[Any], Any]:
        for model in (request, response):
            if not _is_message(model):
                raise TypeError(
                    f"{model!r} is not a @message: gRPC carries protobuf, so both "
                    "the request and response types must be declared ones"
                )

        def decorate(handler: Any) -> Any:
            self._methods.append(
                (handler.__name__, kind, request, response, handler, metadata)
            )
            return handler

        return decorate

    def unary(self, *, request: type, response: type, **metadata: Any) -> Callable[[Any], Any]:
        """One request message, one response message."""
        return self._register(_UNARY, request, response, metadata)

    def server_stream(
        self, *, request: type, response: type, **metadata: Any
    ) -> Callable[[Any], Any]:
        """One request message; the handler yields response messages."""
        return self._register(_SERVER_STREAM, request, response, metadata)

    def client_stream(
        self, *, request: type, response: type, **metadata: Any
    ) -> Callable[[Any], Any]:
        """The handler consumes request messages and returns one response."""
        return self._register(_CLIENT_STREAM, request, response, metadata)

    def bidi(self, *, request: type, response: type, **metadata: Any) -> Callable[[Any], Any]:
        """The handler consumes request messages and yields response messages."""
        return self._register(_BIDI, request, response, metadata)

    def router(self) -> Router:
        """An ordinary `Router` carrying one POST route per method.

        Metadata passed to a method decorator reaches `RouteDefinition`
        unchanged, so `roles=`, `dependencies=` and `rate_limit=` are enforced
        by the same tape as any REST route and are read by `permissions_router`
        and `wreath mutant` from the same place. There is no second model.
        """
        router = Router()
        for name, kind, request, response, handler, metadata in self._methods:
            endpoint = self._endpoint(kind, request, response, handler)
            endpoint.__name__ = name
            endpoint.__doc__ = handler.__doc__
            # `@authorize`, `@roles` and friends record an `AuthRequirement` on
            # the function they decorate. The route registers the *wrapper*, so
            # without this the decorators a user wrote on their method would be
            # silently dropped -- a declared control that enforces nothing,
            # which is the exact failure `wreath mutant` exists to catch.
            _requirements.set_requirement(
                endpoint, _requirements.requirement_for(handler)
            )
            router.post(
                f"/{self.name}/{name}",
                response_only=True,
                include_in_schema=False,
                **metadata,
            )(endpoint)
        return router

    def _endpoint(self, kind: int, request_model: type, response_model: type, handler: Any) -> Any:
        max_bytes = self.max_message_bytes

        async def endpoint(request: Any) -> Any:
            try:
                _check_transport(request)
                deadline = _deadline_of(request)
                incoming = _messages(request, request_model, max_bytes)
                if kind in (_UNARY, _SERVER_STREAM):
                    call = handler(request, await _exactly_one(incoming))
                else:
                    call = handler(request, incoming)
                if kind in (_UNARY, _CLIENT_STREAM):
                    result = await _with_deadline(call, deadline)
                    return _GrpcResponse(_one(frame_message(_protobuf.encode(result))))
                return _GrpcResponse(_frames(call, deadline))
            except (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - every failure becomes a status
                # Nothing has been sent yet, so the whole call collapses to a
                # status-bearing response with an empty body.
                status, detail = status_for(exc)
                return _GrpcResponse(_empty(), status=status, message=detail)

        return endpoint


# --- transport checks and message plumbing -----------------------------------


def _header(request: Any, name: str) -> str | None:
    """One header by name. `Request.headers` is the raw ASGI pair list; `header`
    is the by-name accessor that decodes and indexes."""
    return request.header(name)


def _check_transport(request: Any) -> None:
    """Refuse a request the transport cannot actually carry, by name.

    **ASGI offers no way to learn at startup which protocols the server will
    speak**, so this cannot be a startup check the way the plan hoped. It is the
    first thing every call does instead, and the refusal names the reason rather
    than letting a client receive a 200 whose trailers never arrive.
    """
    version = str(request.scope.get("http_version", "1.1"))
    if version != "2":
        raise GrpcError(
            Status.UNIMPLEMENTED,
            f"gRPC needs HTTP/2 and response trailers; this request arrived over "
            f"HTTP/{version}. Wreath's native server is the only one here that "
            f"serves h2 -- a foreign ASGI server cannot carry gRPC.",
        )
    content_type = (_header(request, "content-type") or "").split(";")[0].strip()
    if content_type not in _ACCEPTED_CONTENT_TYPES:
        raise GrpcError(
            Status.INTERNAL, f"unsupported content-type {content_type!r}"
        )
    encoding = (_header(request, "grpc-encoding") or "identity").strip()
    if encoding != "identity":
        raise GrpcError(
            Status.UNIMPLEMENTED, f"grpc-encoding {encoding!r} is not supported"
        )


def _deadline_of(request: Any) -> float | None:
    raw = _header(request, "grpc-timeout")
    return None if raw is None else parse_timeout(raw.strip())


async def _messages(request: Any, model: type, max_bytes: int) -> AsyncIterator[Any]:
    """Decode the request body into messages as its bytes arrive."""
    unframer = Unframer(max_message_bytes=max_bytes)
    async for chunk in request.stream():
        for payload in unframer.feed(chunk):
            yield _protobuf.decode(model, payload)
    unframer.finish()


async def _exactly_one(messages: AsyncIterator[Any]) -> Any:
    """The single message a unary or server-streaming call must carry.

    Both counts are refused: none means the client framed nothing, more than one
    means it used the wrong call shape, and either would otherwise be silently
    reinterpreted as a valid call.
    """
    first = _MISSING = object()
    async for message in messages:
        if first is not _MISSING:
            raise GrpcError(
                Status.INVALID_ARGUMENT, "expected exactly one request message, got more"
            )
        first = message
    if first is _MISSING:
        raise GrpcError(Status.INVALID_ARGUMENT, "expected one request message, got none")
    return first


async def _with_deadline(awaitable: Any, deadline: float | None) -> Any:
    if deadline is None:
        return await awaitable
    import asyncio

    try:
        async with asyncio.timeout(deadline):
            return await awaitable
    except TimeoutError as exc:
        raise GrpcError(Status.DEADLINE_EXCEEDED, "deadline exceeded") from exc


async def _frames(results: Any, deadline: float | None) -> AsyncIterator[bytes]:
    """Frame each yielded response message, under the call's deadline."""
    import asyncio
    import contextlib

    limit: Any = (
        contextlib.nullcontext() if deadline is None else asyncio.timeout(deadline)
    )
    try:
        async with limit:
            async for result in results:
                yield frame_message(_protobuf.encode(result))
    except TimeoutError as exc:
        raise GrpcError(Status.DEADLINE_EXCEEDED, "deadline exceeded") from exc
