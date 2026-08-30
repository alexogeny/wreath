"""The immutable :class:`PreparedResponse`.

A prepared response computes its status line, headers, and body once and then
replays the same two ASGI messages on every call. Nothing mutates on the send
path, so a single instance is safe to reuse from any number of concurrent
requests and on any conforming ASGI server. ``Date`` and ``Server`` stay
server-owned; a prepared response never invents them.

**This stays Python, and that is measured rather than assumed.** Wreath's own
server recognises the type and emits it through its one-shot response ABI
without ever calling `__call__` (`_native/server.h:96`), so the replay below
runs only on a portable ASGI server. Measured against two bare `await send(...)`
calls it costs **+0.08us on a 0.24us floor**, which is the whole budget a C
version could recover, on the one path that is already not the fast one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ._conditional import STATUS_WITHOUT_BODY as _STATUS_WITHOUT_BODY

# The `_json` facade, which also installs the temporal hook.
from ._json import dumps as _json_dumps

Send = Callable[[dict[str, Any]], Awaitable[None]]

_CONTENT_TYPE = b"content-type"
_CONTENT_LENGTH = b"content-length"
# 204/304 carry no body and, per RFC 9110, no Content-Length either.


def _headers_for(
    body: bytes,
    status: int,
    media_type: bytes | None,
    extra: Iterable[tuple[bytes, bytes]] | None,
) -> tuple[tuple[bytes, bytes], ...]:
    headers: list[tuple[bytes, bytes]] = list(extra) if extra is not None else []
    has_type = has_length = False
    for key, _ in headers:
        lowered = key.lower()
        if lowered == _CONTENT_TYPE:
            has_type = True
        elif lowered == _CONTENT_LENGTH:
            has_length = True
        if has_type and has_length:
            break
    if media_type and not has_type:
        headers.append((_CONTENT_TYPE, media_type))
    if status not in _STATUS_WITHOUT_BODY and not has_length:
        headers.append((_CONTENT_LENGTH, str(len(body)).encode("ascii")))
    return tuple(headers)


class PreparedResponse:
    """An immutable, reusable ASGI response built once at startup.

    Construct one directly from bytes, or via :meth:`text`, :meth:`json`, and
    :meth:`html`. The instance holds prebuilt ``http.response.start`` and
    ``http.response.body`` messages and simply replays them.
    """

    __slots__ = ("_body_message", "_start_message", "body", "headers", "status")

    # A prepared response has no per-instance background work; the attribute
    # exists so the response finisher can read it uniformly.
    background = None

    def __init__(
        self,
        body: bytes = b"",
        status: int = 200,
        *,
        media_type: bytes | None = b"application/octet-stream",
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> None:
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("PreparedResponse body must be bytes")
        body = bytes(body)
        if status in _STATUS_WITHOUT_BODY:
            body = b""
        self.status = status
        self.body = body
        self.headers = _headers_for(body, status, media_type, headers)
        # Prebuilt once; the send path only reads these two dicts.
        self._start_message = {
            "type": "http.response.start",
            "status": status,
            "headers": list(self.headers),
        }
        self._body_message = {"type": "http.response.body", "body": body}

    @classmethod
    def text(
        cls,
        text: str,
        status: int = 200,
        *,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> PreparedResponse:
        return cls(
            text.encode("utf-8"),
            status,
            media_type=b"text/plain; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def html(
        cls,
        html: str,
        status: int = 200,
        *,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> PreparedResponse:
        return cls(
            html.encode("utf-8"),
            status,
            media_type=b"text/html; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def json(
        cls,
        data: Any,
        status: int = 200,
        *,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
    ) -> PreparedResponse:
        return cls(
            _json_dumps(data),
            status,
            media_type=b"application/json",
            headers=headers,
        )

    async def __call__(self, send: Send) -> None:
        await send(self._start_message)
        await send(self._body_message)
