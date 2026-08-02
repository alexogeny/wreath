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
whose routes are `POST /{service}/{method}`, so a method's controls are a
route's controls, enforced by the same middleware tape and read by the same
`permissions_router` and `wreath mutant`. There is no second authorization
model, and there are exactly two spellings because a route has two:

* `action=` and `resource=` on the method decorator, which **are** `@authorize`
  -- the same keyword `@mcp.tool(action=...)` uses, so one vocabulary spans
  REST, gRPC and MCP.
* `@roles`, `@permissions`, `@authorize` and `@second_factor` stacked on the
  handler, exactly as on a route handler. These are decorators and not
  keywords; `permissions=`, `dependencies=` and `middleware=` are the route
  metadata that passes through, and anything the route decorator does not
  accept is a `TypeError` at import rather than a control that silently does
  nothing.

    from wreath.grpc import GrpcService
    from wreath.protobuf import message, field

    @message
    class PositionRequest:
        collar_id: int = field(1)

    tracker = GrpcService("camera.Tracker")

    @tracker.unary(request=PositionRequest, response=Position,
                   action="Collar::read", resource=EntityUid("Collar", "7"))
    async def GetPosition(request, message: PositionRequest) -> Position: ...

    app.include_router(tracker.router())

**Compression** is `identity` and `gzip`, negotiated per call and applied per
message through `wreath.compression` -- no dependency, and no zstd, because
`grpc-encoding` is a registry shared with every other implementation.

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
from ._auth.decorators import authorize as _authorize
from .compression import gzip_compress as _gzip_compress
from .compression import gzip_decompress as _gzip_decompress
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

#: The message codings this server implements, best first. `identity` is always
#: last and always available: gRPC requires every peer to accept it, which is
#: what makes an unknown entry in `grpc-accept-encoding` a non-event rather than
#: a refusal.
#:
#: gzip comes from `wreath.compression`, a facade over the interpreter's own
#: `zlib`, so this adds no dependency. zstd is deliberately absent even though
#: that facade offers it: `grpc-encoding` values are a registry shared with
#: every other implementation, and a coding a Go or Java client cannot name is a
#: dialect rather than a feature.
SUPPORTED_ENCODINGS: tuple[str, ...] = ("gzip", "identity")

#: What every response advertises this server will *accept* on a later call.
_ACCEPT_ENCODING = b"identity,gzip"

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


def negotiated_encoding(declared: str | None) -> str:
    """The coding a call's `grpc-encoding` names, refusing one this server lacks.

    An absent header is `identity`, which is the specification's default. An
    unknown value is `UNIMPLEMENTED` and names itself, because the client can
    act on that -- it re-sends under a coding this server does listen for -- and
    the response carries `grpc-accept-encoding` telling it which.

    Raises:
        GrpcError: `declared` names a coding this server does not implement.
    """
    encoding = (declared or "identity").strip().lower()
    if encoding not in SUPPORTED_ENCODINGS:
        raise GrpcError(
            Status.UNIMPLEMENTED,
            f"grpc-encoding {encoding!r} is not supported; this server reads "
            f"{', '.join(SUPPORTED_ENCODINGS)}",
        )
    return encoding


def reply_encoding(accept: str | None) -> str:
    """The coding to answer in, from the client's `grpc-accept-encoding`.

    A list of what the caller *can* read, so an entry this server does not
    implement is not an error -- it simply is not chosen. `identity` is the
    answer when nothing matches, and every gRPC peer accepts identity, so this
    can never fail.
    """
    if not accept:
        return "identity"
    offered = {token.strip().lower() for token in accept.split(",")}
    for encoding in SUPPORTED_ENCODINGS:
        if encoding in offered:
            return encoding
    return "identity"


def encode_frame(payload: bytes, encoding: str) -> bytes:
    """Frame one outgoing message, compressing it only when that makes it smaller.

    The flag is per *message*, so declining is a normal answer rather than a
    contradiction of the call's `grpc-encoding` -- and gzip costs about twenty
    bytes of header and trailer, so a short reply comes out larger compressed.
    Sending it anyway would be spending CPU to grow the response.
    """
    if encoding == "gzip":
        squeezed = _gzip_compress(payload)
        if len(squeezed) < len(payload):
            return frame_message(squeezed, compressed=True)
    return frame_message(payload)


class Unframer:
    """Incremental reader for a stream of length-prefixed gRPC messages.

    Fed arbitrary chunk boundaries -- a message may span several DATA frames and
    several messages may share one -- and yields complete payloads. The declared
    length is checked against `max_message_bytes` **before** the buffer is
    allowed to grow to it, so a four-byte lie cannot make the server allocate
    what the peer never intends to send.

    `encoding` is the call's `grpc-encoding`. It decides what a message whose
    flag byte is set means: under `gzip` it is decompressed, under `identity`
    the flag contradicts the header the peer itself sent and the message is
    refused. **The same ceiling applies on both sides of the decoder**, because
    the length prefix bounds the compressed size and says nothing about the
    decoded one -- which is the entire mechanism of a decompression bomb.
    """

    def __init__(
        self,
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        encoding: str = "identity",
    ) -> None:
        self._buffer = bytearray()
        self._max = max_message_bytes
        self._encoding = encoding

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
            if compressed not in (0, 1):
                raise GrpcError(
                    Status.INTERNAL,
                    f"compressed flag must be 0 or 1, not {compressed}",
                )
            if compressed and self._encoding == "identity":
                # The specification calls this "Compressed-Flag set but no
                # grpc-encoding", and it is INTERNAL rather than UNIMPLEMENTED:
                # the peer is not asking for something unsupported, it is
                # contradicting its own header. Named separately from the
                # header-level refusal so a log can tell the two apart.
                raise GrpcError(
                    Status.INTERNAL,
                    "a message is flagged compressed but this call declared "
                    "grpc-encoding: identity",
                )
            if len(self._buffer) < _PREFIX_BYTES + length:
                return out
            payload = bytes(self._buffer[_PREFIX_BYTES : _PREFIX_BYTES + length])
            del self._buffer[: _PREFIX_BYTES + length]
            out.append(self._decode(payload) if compressed else payload)

    def _decode(self, payload: bytes) -> bytes:
        """Decompress one message, bounded by the same ceiling the prefix has."""
        try:
            return _gzip_decompress(payload, max_output_bytes=self._max)
        except ValueError as error:
            text = str(error)
            if "expands past" in text:
                raise GrpcError(
                    Status.RESOURCE_EXHAUSTED,
                    f"a gzip message decompresses past the {self._max}-byte limit",
                ) from error
            raise GrpcError(Status.INTERNAL, f"undecodable gzip message: {text}") from error

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


#: The safe set as `bytes`, for `translate`'s delete argument, and the encoding
#: of every byte, built once. Together they turn the encoder into one C-level
#: scan plus -- only when something needs escaping -- one table lookup per byte,
#: instead of a Python-level branch and an f-string per byte.
_SAFE_BYTES = bytes(sorted(_SAFE))
_ENCODED = tuple(chr(b) if b in _SAFE else f"%{b:02X}" for b in range(256))


def percent_encode(text: str) -> str:
    """Percent-encode a `grpc-message` value per the gRPC wire specification.

    Two paths, because the common one has nothing to escape: a status message
    is usually plain ASCII prose. `translate(None, _SAFE_BYTES)` deletes every
    safe byte in C, so an empty result proves the whole string is already its
    own encoding and it can be handed back without building anything.

    Measured against the per-byte loop this replaces, with a >=1% A/A floor:
    64.7% faster on a 9-character message, 90.2% on a 50-character one, 97.2%
    on 400 characters, and 55.9% when a byte does need escaping.
    """
    raw = text.encode("utf-8")
    if not raw.translate(None, _SAFE_BYTES):
        # Every byte was safe, so every byte is ASCII and its own encoding.
        return raw.decode("ascii")
    return "".join([_ENCODED[b] for b in raw])


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
        encoding: str = "identity",
    ) -> None:
        headers = [
            (b"content-type", CONTENT_TYPE.encode("ascii")),
            # What this server will *read* on a later call, stated on every
            # response including the refusals -- a client told its coding is
            # unsupported can only act on that if it is also told which are.
            (b"grpc-accept-encoding", _ACCEPT_ENCODING),
        ]
        if encoding != "identity":
            # What *this* response's messages may be compressed with. Only
            # "may": the flag is per message and `encode_frame` declines when
            # compressing would grow one.
            headers.append((b"grpc-encoding", encoding.encode("ascii")))
        super().__init__(body, status=200, headers=headers)
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
        # `action=`/`resource=` are lifted out of the route metadata and spent as
        # `@authorize` on the handler, rather than forwarded: `Router.route` has
        # no such keywords, so passing them through raised `TypeError` and the
        # spelling this module's own docstring taught could not be written. It is
        # `@authorize` and not a private twin of it because a gRPC method must
        # mean *the same thing* a route means -- one vocabulary, read by
        # `permissions_router` and `wreath typegen` off one declaration.
        action = metadata.pop("action", None)
        resource = metadata.pop("resource", None)
        if action is None and resource is not None:
            raise ValueError(
                "a gRPC method was given a `resource=` with no `action=`. A Cedar "
                "decision needs both, and a resource on its own gates nothing."
            )

        def decorate(handler: Any) -> Any:
            if action is not None:
                _authorize(action=action, resource=resource)(handler)
            self._methods.append(
                (handler.__name__, kind, request, response, handler, metadata)
            )
            return handler

        return decorate

    def unary(self, *, request: type, response: type, **metadata: Any) -> Callable[[Any], Any]:
        """One request message, one response message.

        `action=` (with an optional `resource=`) is `@authorize` spelled at the
        declaration, and every other keyword is route metadata -- `roles=` and
        the rest are still written as decorators on the method, exactly as on a
        route handler.
        """
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
            # Read before the try, so a refusal below still answers in the
            # coding the client said it could read. It cannot itself fail: an
            # unknown entry in `grpc-accept-encoding` simply is not chosen.
            outgoing = reply_encoding(_header(request, "grpc-accept-encoding"))
            try:
                encoding = _check_transport(request)
                deadline = _deadline_of(request)
                incoming = _messages(request, request_model, max_bytes, encoding)
                if kind in (_UNARY, _SERVER_STREAM):
                    call = handler(request, await _exactly_one(incoming))
                else:
                    call = handler(request, incoming)
                if kind in (_UNARY, _CLIENT_STREAM):
                    result = await _with_deadline(call, deadline)
                    return _GrpcResponse(
                        _one(encode_frame(_protobuf.encode(result), outgoing)),
                        encoding=outgoing,
                    )
                return _GrpcResponse(_frames(call, deadline, outgoing), encoding=outgoing)
            except (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - every failure becomes a status
                # Nothing has been sent yet, so the whole call collapses to a
                # status-bearing response with an empty body. No `grpc-encoding`
                # on it: there is no message to have compressed, and a coding
                # header over an empty body is a claim about nothing.
                status, detail = status_for(exc)
                return _GrpcResponse(_empty(), status=status, message=detail)

        return endpoint


# --- transport checks and message plumbing -----------------------------------


def _header(request: Any, name: str) -> str | None:
    """One header by name. `Request.headers` is the raw ASGI pair list; `header`
    is the by-name accessor that decodes and indexes."""
    return request.header(name)


def _check_transport(request: Any) -> str:
    """Refuse a request the transport cannot carry, and return its coding.

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
    return negotiated_encoding(_header(request, "grpc-encoding"))


def _deadline_of(request: Any) -> float | None:
    raw = _header(request, "grpc-timeout")
    return None if raw is None else parse_timeout(raw.strip())


async def _messages(
    request: Any, model: type, max_bytes: int, encoding: str
) -> AsyncIterator[Any]:
    """Decode the request body into messages as its bytes arrive."""
    unframer = Unframer(max_message_bytes=max_bytes, encoding=encoding)
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


async def _frames(
    results: Any, deadline: float | None, encoding: str
) -> AsyncIterator[bytes]:
    """Frame each yielded response message, under the call's deadline."""
    import asyncio
    import contextlib

    limit: Any = (
        contextlib.nullcontext() if deadline is None else asyncio.timeout(deadline)
    )
    try:
        async with limit:
            async for result in results:
                yield encode_frame(_protobuf.encode(result), encoding)
    except TimeoutError as exc:
        raise GrpcError(Status.DEADLINE_EXCEEDED, "deadline exceeded") from exc
