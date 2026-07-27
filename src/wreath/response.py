"""ASGI response primitives."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote, urlsplit

from ._json import dumps as _json_dumps

# PreparedResponse's only request-time work is replaying two prebuilt ASGI
# messages, so there is nothing above the noise floor for C to accelerate; the
# pure implementation is the shipped one (see docs/reference/responses.md).
from ._pure.response import PreparedResponse as PreparedResponse
from .background import Background
from .cache_control import CacheControl

Send = Callable[[dict[str, Any]], Awaitable[None]]

_STATUS_WITHOUT_BODY = frozenset({204, 304})
_CONTENT_TYPE = b"content-type"
_CONTENT_LENGTH = b"content-length"
# Small content-length values are overwhelmingly common; formatting them once
# keeps str+encode out of the per-response path.
_CONTENT_LENGTHS = tuple(str(n).encode("ascii") for n in range(1024))


def _content_length(size: int) -> bytes:
    if size < 1024:
        return _CONTENT_LENGTHS[size]
    return str(size).encode("ascii")


class Response:
    __slots__ = ("background", "body", "headers", "status")

    media_type = b"application/octet-stream"
    # The default content-type pair never varies per instance, so each class
    # carries it prebuilt (None when the media type is empty).
    _media_type_header: tuple[bytes, bytes] | None = (_CONTENT_TYPE, media_type)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls._media_type_header = (_CONTENT_TYPE, cls.media_type) if cls.media_type else None

    def __init__(
        self,
        body: bytes = b"",
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        media_type: bytes | None = None,
        background: Background | None = None,
    ) -> None:
        # Neither a 204 nor a 304 may carry content (RFC 9110 §6.4.1, §15.4.5),
        # so a body passed with one is dropped here rather than sent. Decided at
        # construction because the status is already being tested for the
        # content-length below -- doing it again in `__call__` would put one
        # more membership test on every response the server sends.
        bodyless = status in _STATUS_WITHOUT_BODY
        self.body = b"" if bodyless else body
        self.status = status
        self.background = background
        if headers is None:
            if media_type is None:
                media_type_header = self._media_type_header
            else:
                media_type_header = (_CONTENT_TYPE, media_type) if media_type else None
            response_headers: list[tuple[bytes, bytes]] = (
                [media_type_header] if media_type_header is not None else []
            )
            if not bodyless:
                response_headers.append((_CONTENT_LENGTH, _content_length(len(body))))
        else:
            if media_type is None:
                media_type = self.media_type
            response_headers = list(headers)
            has_type = has_length = False
            for key, _ in response_headers:
                key = key.lower()
                if key == _CONTENT_TYPE:
                    has_type = True
                elif key == _CONTENT_LENGTH:
                    has_length = True
            if media_type and not has_type:
                response_headers.append((_CONTENT_TYPE, media_type))
            if not bodyless and not has_length:
                response_headers.append((_CONTENT_LENGTH, _content_length(len(body))))
        self.headers = response_headers

    async def __call__(self, send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status, "headers": self.headers})
        await send({"type": "http.response.body", "body": self.body})

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        max_age: int | None = None,
        expires: str | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
    ) -> None:
        """Append a Set-Cookie header. Values are sent verbatim; callers quote
        or encode values that contain separators.

        Enforces the browser-mandated attribute rules so a cookie is never
        emitted in a form every browser silently drops: ``SameSite`` must be one
        of ``strict``/``lax``/``none`` and ``SameSite=None`` requires ``Secure``
        (RFC 6265bis 5.4.7); the ``__Secure-``/``__Host-`` name prefixes require
        ``Secure`` (and, for ``__Host-``, ``Path=/`` with no ``Domain``) per
        RFC 6265bis 4.1.3.
        """
        if samesite is not None:
            samesite = samesite.lower()
            if samesite not in ("strict", "lax", "none"):
                raise ValueError(
                    f"samesite must be 'strict', 'lax', or 'none', got {samesite!r}"
                )
            if samesite == "none" and not secure:
                raise ValueError(
                    "SameSite=None cookies must be Secure (RFC 6265bis 5.4.7); "
                    "pass secure=True"
                )
        # Fail at the call, not later at the native serializer: a control
        # character (CR/LF especially) in a cookie name or value is a
        # header-injection vector and never valid (RFC 6265 §4.1.1). `path` and
        # `domain` are interpolated into the same header line and are just as
        # injectable, so they are checked here too rather than trusted for
        # being "configuration" -- a router that builds a cookie path from a
        # request is ordinary code.
        for field_name, field_value in (
            ("name", name),
            ("value", value),
            ("path", path),
            ("domain", domain),
        ):
            if field_value is None:
                continue
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in field_value):
                raise ValueError(f"cookie {field_name} contains a control character")
            if field_name in ("path", "domain") and ";" in field_value:
                raise ValueError(f"cookie {field_name} contains an attribute separator")
        if name.startswith("__Secure-") and not secure:
            raise ValueError("__Secure- cookies must be Secure; pass secure=True")
        if name.startswith("__Host-") and not (secure and path == "/" and domain is None):
            raise ValueError(
                "__Host- cookies must be Secure, have Path=/, and set no Domain "
                "(RFC 6265bis 4.1.3)"
            )
        parts = [f"{name}={value}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if expires is not None:
            parts.append(f"Expires={expires}")
        if path:
            parts.append(f"Path={path}")
        if domain is not None:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite is not None:
            parts.append(f"SameSite={samesite.capitalize()}")
        self.headers.append((b"set-cookie", "; ".join(parts).encode("latin-1")))

    def delete_cookie(self, name: str, *, path: str = "/", domain: str | None = None) -> None:
        # A prefixed cookie can only be cleared by a Set-Cookie that still meets
        # the prefix's attribute rules, so carry Secure for those names.
        secure = name.startswith(("__Secure-", "__Host-"))
        self.set_cookie(
            name,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path=path,
            domain=domain,
            secure=secure,
            samesite=None,
        )

    def set_cache_control(self, policy: CacheControl) -> None:
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))


class TextResponse(Response):
    media_type = b"text/plain; charset=utf-8"

    def __init__(
        self, body: str, status: int = 200, *, background: Background | None = None
    ) -> None:
        super().__init__(body.encode("utf-8"), status=status, background=background)


class JSONResponse(Response):
    media_type = b"application/json"

    def __init__(
        self, data: Any, status: int = 200, *, background: Background | None = None
    ) -> None:
        super().__init__(_json_dumps(data), status=status, background=background)


# --- coercion fast paths ----------------------------------------------------
# Handlers overwhelmingly return str/bytes/dict with the default 200 status.
# _coerce_response builds those into a Response in one frame -- skipping the
# subclass __init__ -> Response.__init__ double call and its branch logic --
# producing byte-for-byte the same result as the constructors above. The type
# headers derive from each class's media_type so they cannot drift. Guarded by
# the equivalence test in tests/test_response_fast_path.py.
_TEXT_TYPE_HEADER = (_CONTENT_TYPE, TextResponse.media_type)
_JSON_TYPE_HEADER = (_CONTENT_TYPE, JSONResponse.media_type)
_OCTET_TYPE_HEADER = (_CONTENT_TYPE, Response.media_type)


def _build_response(body: bytes, type_header: tuple[bytes, bytes]) -> Response:
    # 200 is never in _STATUS_WITHOUT_BODY, so content-length always applies.
    response = Response.__new__(Response)
    response.body = body
    response.status = 200
    response.background = None
    response.headers = [type_header, (_CONTENT_LENGTH, _content_length(len(body)))]
    return response


def coerce_text(body: str) -> Response:
    return _build_response(body.encode("utf-8"), _TEXT_TYPE_HEADER)


def coerce_json(data: Any) -> Response:
    return _build_response(_json_dumps(data), _JSON_TYPE_HEADER)


def coerce_bytes(body: bytes) -> Response:
    return _build_response(body, _OCTET_TYPE_HEADER)


@dataclass(frozen=True, slots=True)
class ProblemDetail:
    status: int
    title: str | None = None
    detail: str | None = None
    type: str = "about:blank"
    instance: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        try:
            title = self.title or HTTPStatus(self.status).phrase
        except ValueError:
            title = self.title or "HTTP Error"
        document: dict[str, Any] = {
            "type": self.type,
            "title": title,
            "status": self.status,
        }
        if self.detail is not None:
            document["detail"] = self.detail
        if self.instance is not None:
            document["instance"] = self.instance
        document.update(self.extensions)
        return document


class ProblemResponse(Response):
    media_type = b"application/problem+json"

    def __init__(
        self,
        problem: ProblemDetail | None = None,
        *,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        type: str = "about:blank",
        instance: str | None = None,
        extensions: dict[str, Any] | None = None,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        background: Background | None = None,
    ) -> None:
        if problem is None:
            if status is None:
                raise TypeError("ProblemResponse requires a problem or status")
            problem = ProblemDetail(
                status,
                title,
                detail,
                type,
                instance,
                {} if extensions is None else extensions,
            )
        super().__init__(
            _json_dumps(problem.as_dict()),
            status=problem.status,
            headers=headers,
            background=background,
        )


class HTMLResponse(Response):
    media_type = b"text/html; charset=utf-8"

    def __init__(
        self, body: str, status: int = 200, *, background: Background | None = None
    ) -> None:
        super().__init__(body.encode("utf-8"), status=status, background=background)


class RedirectResponse(Response):
    media_type = b""

    #: Schemes a redirect may name. A relative target (no scheme) is always
    #: allowed; so is a protocol-relative ``//host/path``, which inherits the
    #: current one.
    allowed_schemes = frozenset({"http", "https"})

    def __init__(
        self, url: str, status: int = 307, *, background: Background | None = None
    ) -> None:
        # `javascript:` and `data:` in a Location are script execution on the
        # origin that redirected, so the scheme is checked rather than trusted.
        # Parsed rather than string-matched because urlsplit applies the same
        # tab/newline stripping a browser does -- "java\nscript:" is a scheme
        # here exactly as it would be there.
        scheme = urlsplit(url).scheme.lower()
        if scheme and scheme not in self.allowed_schemes:
            raise ValueError(
                f"redirect scheme {scheme!r} is not allowed; expected one of "
                f"{', '.join(sorted(self.allowed_schemes))} or a relative target"
            )
        # Encode like a URL: characters outside the URL-safe set percent-escape.
        location = quote(url, safe=":/%#?=@[]!$&'()*+,;")
        super().__init__(
            b"",
            status=status,
            headers=[(b"location", location.encode("ascii"))],
            background=background,
        )


class StreamingResponse:
    __slots__ = ("_cleanup", "background", "body", "headers", "status")

    def __init__(
        self,
        body: AsyncIterable[bytes],
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        background: Background | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = list(headers) if headers is not None else []
        self.background = background
        self._cleanup: Background | None = None

    def set_cache_control(self, policy: CacheControl) -> None:
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))

    async def __call__(self, send: Send) -> None:
        try:
            await send(
                {"type": "http.response.start", "status": self.status, "headers": self.headers}
            )
            async for chunk in self.body:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            cleanup = self._cleanup
            if cleanup is not None:
                self._cleanup = None
                await cleanup()


class ServerSentEvent:
    """One Server-Sent Event. Every field is optional; ``data`` may be multi-line
    (each line is framed as its own ``data:`` field). A ``comment``-only event is
    a keep-alive."""

    __slots__ = ("data", "event", "id", "retry", "comment")

    def __init__(
        self,
        data: str | bytes | None = None,
        *,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
        comment: str | None = None,
    ) -> None:
        self.data = data
        self.event = event
        self.id = id
        self.retry = retry
        self.comment = comment


def _sse_single_line(field: str, value: str) -> str:
    """One single-line SSE field, refusing a value that would inject a frame.

    ``data`` and ``comment`` are multi-line by construction and are re-framed
    per line by :func:`_sse_lines`. ``event`` and ``id`` are not: a CR or LF in
    either ends the field -- and a blank line ends the *event* -- so a caller
    passing user text through one could append arbitrary frames to the stream.
    Refused rather than stripped, because silently sending a different event
    name than the caller asked for is its own bug.
    """
    if "\r" in value or "\n" in value:
        raise ValueError(f"SSE {field} must not contain a newline")
    return f"{field}: {value}"


def _sse_lines(field: str, value: str, out: list[str]) -> None:
    # An SSE field value cannot contain CR or LF, so a multi-line value becomes
    # one repeated field per line (normalising CRLF/CR to LF first).
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        out.append(f"{field}: {line}" if line else f"{field}:")


def _encode_sse(event: ServerSentEvent | str | bytes | Mapping[str, Any]) -> bytes:
    """Frame one event to the ``text/event-stream`` wire format.

    Accepts a :class:`ServerSentEvent`, a ``str``/``bytes`` (treated as ``data``),
    or a mapping with ``data``/``event``/``id``/``retry``/``comment`` keys.

    TODO(native)/TODO(pure-twin): framing is trivial per-event string work and
    there is no existing native primitive to reuse, so this is pure Python; a
    native ``_core.sse_frame`` can drop in behind this function later if a
    benchmark ever justifies it. It already behaves identically under
    ``WREATH_PURE=1`` (no native path to diverge from).
    """
    if isinstance(event, ServerSentEvent):
        comment, name, ident, retry, data = (
            event.comment, event.event, event.id, event.retry, event.data,
        )
    elif isinstance(event, (str, bytes)):
        comment = name = ident = retry = None
        data = event
    elif isinstance(event, Mapping):
        comment = event.get("comment")
        name = event.get("event")
        ident = event.get("id")
        retry = event.get("retry")
        data = event.get("data")
    else:
        raise TypeError(f"cannot frame SSE event of type {type(event).__name__!r}")
    lines: list[str] = []
    if comment is not None:
        _sse_lines("", str(comment), lines)
    if name is not None:
        lines.append(_sse_single_line("event", str(name)))
    if ident is not None:
        lines.append(_sse_single_line("id", str(ident)))
    if retry is not None:
        lines.append(f"retry: {int(retry)}")
    if data is not None:
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        _sse_lines("data", text, lines)
    if not lines:
        lines.append(":")  # bare keep-alive comment
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class SSEResponse(StreamingResponse):
    """Server-Sent Events over the streaming-response plumbing.

    Frames each item from an async iterator of events (a
    :class:`ServerSentEvent`, a ``str``/``bytes`` treated as ``data``, or a
    mapping) as ``text/event-stream`` with the correct no-buffering headers
    (``cache-control: no-cache``, ``x-accel-buffering: no``). Emit a keep-alive
    by yielding ``ServerSentEvent(comment="ping")``.

    This is the SSE *transport* only; a progress/status convention is left to the
    application (consumer-owned, by design).
    """

    __slots__ = ()
    media_type = b"text/event-stream"

    def __init__(
        self,
        events: AsyncIterable[ServerSentEvent | str | bytes | Mapping[str, Any]],
        *,
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        background: Background | None = None,
    ) -> None:
        combined: list[tuple[bytes, bytes]] = [
            (_CONTENT_TYPE, self.media_type),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),
            (b"connection", b"keep-alive"),
        ]
        if headers is not None:
            combined.extend(headers)
        super().__init__(
            self._framed(events), status=status, headers=combined, background=background
        )

    @staticmethod
    async def _framed(
        events: AsyncIterable[ServerSentEvent | str | bytes | Mapping[str, Any]],
    ) -> AsyncIterable[bytes]:
        async for event in events:
            yield _encode_sse(event)


_FILE_CHUNK = 256 * 1024
#: Chunks a file reader may buffer ahead of the ASGI send. Bounds read-ahead
#: memory to _FILE_READAHEAD * _FILE_CHUNK regardless of file size.
_FILE_READAHEAD = 4
#: End-of-file sentinel handed from the reader worker to the sender.
_EOF = object()


async def _send_from_descriptor(
    fd: int, size: int, status: int, headers: list[tuple[bytes, bytes]], send: Send
) -> None:
    """Stream an already-open file descriptor with one executor submission.

    A single reader worker owns the descriptor (reads and closes it) and hands
    chunks to the event loop through a bounded queue; the queue's bound applies
    backpressure to the reader, so read-ahead and memory stay bounded no matter
    how large the file is. This replaces one executor job per 256 KiB chunk.
    """
    start = {"type": "http.response.start", "status": status,
             "headers": [*headers, (_CONTENT_LENGTH, _content_length(size))]}
    await send(start)
    if size == 0:
        os.close(fd)
        await send({"type": "http.response.body", "body": b""})
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_FILE_READAHEAD)
    stop = threading.Event()

    def _reader() -> None:
        try:
            remaining = size
            while remaining > 0 and not stop.is_set():
                chunk = os.read(fd, min(_FILE_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                # Block the reader until the sender makes room; this is the
                # backpressure that keeps read-ahead bounded, and it releases
                # promptly once the sender drains the queue on teardown.
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                if stop.is_set():
                    return
            asyncio.run_coroutine_threadsafe(queue.put(_EOF), loop).result()
        except BaseException as exc:  # noqa: BLE001 - relayed to the sender
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
        finally:
            os.close(fd)

    worker = loop.run_in_executor(None, _reader)
    try:
        while True:
            item = await queue.get()
            if item is _EOF:
                break
            if isinstance(item, BaseException):
                raise item
            await send({"type": "http.response.body", "body": item, "more_body": True})
        # File shrank mid-send leaves content-length short; a terminal empty body
        # ends the stream and the server closes rather than mis-framing.
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        stop.set()
        # Unblock a reader parked on a full queue so it can observe stop.
        while not queue.empty():
            queue.get_nowait()
        await worker


async def _send_native_descriptor(
    fd: int, size: int, status: int, headers: list[tuple[bytes, bytes]], send: Send
) -> bool:
    protocol: Any = getattr(cast(Any, send), "__self__", None)
    start = getattr(protocol, "_wreath_file_start", None)
    if start is None:
        return False
    file = os.fdopen(fd, "rb", closefd=True)
    try:
        await start(
            status,
            [*headers, (_CONTENT_LENGTH, _content_length(size))],
            file,
            size,
        )
    finally:
        file.close()
    await protocol._wreath_file_finish()
    return True


def _disposition(filename: str) -> bytes:
    """``content-disposition`` for an attachment named ``filename``.

    The name reaches a quoted-string, so a bare interpolation lets a ``"`` end
    the quoting and a CR/LF end the header. RFC 6266 §4.1 wants the quoted form
    escaped and a non-ASCII name carried in ``filename*`` (RFC 5987) with an
    ASCII fallback, which is also what keeps the latin-1 encode below from
    raising on an ordinary accented name.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in filename):
        raise ValueError("attachment filename contains a control character")
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    if filename.isascii():
        return f'attachment; filename="{escaped}"'.encode("ascii")
    # The fallback keeps only ASCII so every client can read something; the
    # `filename*` parameter carries the real name for those that support it.
    fallback = escaped.encode("ascii", "replace").decode("ascii")
    encoded = quote(filename, safe="")
    return (
        f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
    ).encode("ascii")


def _open_fd(path: str) -> tuple[int, int]:
    """Open a file for reading and return its descriptor and size (one worker)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    return fd, os.fstat(fd).st_size


class FileResponse:
    """Stream a file from disk with a known content-length.

    The stat happens lazily on send, so constructing one for a missing file
    raises ``FileNotFoundError`` at response time; route-level code that wants
    a 404 checks existence first (as ``wreath.staticfiles`` does).
    """

    __slots__ = ("_fd", "_stat", "background", "headers", "media_type", "path", "status")

    def __init__(
        self,
        path: str | os.PathLike[str],
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        media_type: bytes | None = None,
        filename: str | None = None,
        background: Background | None = None,
    ) -> None:
        self.path = os.fspath(path)
        self.status = status
        self.headers = list(headers) if headers is not None else []
        if media_type is None:
            guessed, _ = mimetypes.guess_type(self.path)
            media_type = (guessed or "application/octet-stream").encode("ascii")
        self.media_type = media_type
        self.headers.append((_CONTENT_TYPE, media_type))
        if filename is not None:
            self.headers.append((b"content-disposition", _disposition(filename)))
        self.background = background
        self._fd: int | None = None
        self._stat: os.stat_result | None = None

    @classmethod
    def from_descriptor(
        cls,
        fd: int,
        stat: os.stat_result,
        name: str,
        *,
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> FileResponse:
        """Build a response over an already-open descriptor (owned and closed by
        the response). The file checked by the opener is the file served — there
        is no reopen-by-name window. ``name`` is used only to guess the type."""
        self = cls.__new__(cls)
        self.path = name
        self.status = status
        self.headers = list(headers) if headers is not None else []
        guessed, _ = mimetypes.guess_type(name)
        self.media_type = (guessed or "application/octet-stream").encode("ascii")
        self.headers.append((_CONTENT_TYPE, self.media_type))
        self.background = None
        self._fd = fd
        self._stat = stat
        return self

    def set_cache_control(self, policy: CacheControl) -> None:
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))

    async def __call__(self, send: Send) -> None:
        if self._fd is not None:
            # Already opened beneath a trusted root (see wreath.staticfiles);
            # serve the descriptor we hold rather than reopening a pathname.
            size = self._stat.st_size if self._stat is not None else 0
            fd, self._fd = self._fd, None
            if await _send_native_descriptor(fd, size, self.status, self.headers, send):
                return
            await _send_from_descriptor(fd, size, self.status, self.headers, send)
            return
        # Open once (open + fstat in a single worker), then stream through the
        # same single-submission reader. A missing file raises here.
        fd, size = await asyncio.to_thread(_open_fd, self.path)
        if await _send_native_descriptor(fd, size, self.status, self.headers, send):
            return
        await _send_from_descriptor(fd, size, self.status, self.headers, send)
