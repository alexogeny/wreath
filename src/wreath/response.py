"""ASGI response primitives."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

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
        self.body = body
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
            if status not in _STATUS_WITHOUT_BODY:
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
            if status not in _STATUS_WITHOUT_BODY and not has_length:
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
        or encode values that contain separators."""
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
        self.set_cookie(
            name,
            "",
            max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
            path=path,
            domain=domain,
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

    def __init__(
        self, url: str, status: int = 307, *, background: Background | None = None
    ) -> None:
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

    worker = asyncio.ensure_future(asyncio.to_thread(_reader))
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
        await asyncio.wrap_future(worker)


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
            disposition = f'attachment; filename="{filename}"'
            self.headers.append((b"content-disposition", disposition.encode("latin-1")))
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
            await _send_from_descriptor(self._fd, size, self.status, self.headers, send)
            return
        # Open once (open + fstat in a single worker), then stream through the
        # same single-submission reader. A missing file raises here.
        fd, size = await asyncio.to_thread(_open_fd, self.path)
        await _send_from_descriptor(fd, size, self.status, self.headers, send)
