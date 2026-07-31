"""ASGI response primitives.

Every response type wreath sends, plus the two seams around them: the `coerce_*`
builders that a handler's return value passes through, and `ProblemDetail` --
the RFC 9457 document every wreath error becomes.

`Response` and its subclasses hold a complete body in memory and send it as two
ASGI messages. `StreamingResponse`, `SSEResponse`, and `FileResponse` are
deliberately not `Response` subclasses: they own their emission and have no
in-memory body, which is also why `wreath.response_cache` and
`wreath.middleware.idempotency` refuse to store them.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final, cast
from urllib.parse import quote, urlsplit

from ._json import dumps as _json_dumps
from ._native import _core

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
    """A complete HTTP response held in memory: status, headers, and a byte body.

    The base of every non-streaming response type, and what a handler gets back
    from `coerce_text`, `coerce_json`, and `coerce_bytes`. Emission is two ASGI
    messages -- one `http.response.start`, one `http.response.body`.

    With no `headers`, the header list is generated: `content-type` from the
    class `media_type` (`application/octet-stream` here, or `media_type` when
    given) and `content-length` from the body. With `headers`, that list is sent
    as written and `content-type`/`content-length` are appended only if the
    caller did not already supply them, matched case-insensitively. A 204 or a
    304 may carry no content (RFC 9110 §6.4.1, §15.4.5), so a body passed with
    either status is dropped at construction and no `content-length` is emitted.

    The header list is a plain mutable list of `(name, value)` byte pairs and
    stays writable after construction; `set_cookie()` and `set_cache_control()`
    are the two supported ways to edit it.

    Args:
        status: HTTP status code, defaulting to 200.
        headers: Full header list to send, replacing the generated defaults.
        media_type: Overrides the class `media_type`; empty bytes emit no content-type.
        background: Awaited by the application after the response has been sent.
    """

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
                # `_content_length` inlined on this branch alone. It is one
                # Python call, and one Python call is 49ns of the 412ns this
                # constructor costs -- 12%, on the path every ordinary response
                # takes. The function stays for the `headers is not None`
                # branch below and for external callers, where it runs once
                # against much more surrounding work.
                size = len(body)
                response_headers.append(
                    (
                        _CONTENT_LENGTH,
                        _CONTENT_LENGTHS[size]
                        if size < 1024
                        else str(size).encode("ascii"),
                    )
                )
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
        """Emit the response over `send` as `http.response.start` plus one body.

        Nothing is recomputed here -- status, headers, and body are whatever the
        instance holds -- so one response object can be sent more than once. For
        a `HEAD` request the application, not this method, replaces the body with
        empty bytes, leaving the advertised `content-length` intact.
        """
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
        """Append one `set-cookie` header to this response.

        The name and value are sent verbatim; a caller quotes or encodes a value
        containing a separator. By default the cookie is host-only (no `Domain`),
        scoped to `Path=/`, `SameSite=Lax`, not `Secure`, not `HttpOnly`, and a
        session cookie -- it expires when the browser does, because neither
        `max_age` nor `expires` is set.

        Enforces the browser-mandated attribute rules so a cookie is never
        emitted in a form every browser silently drops: `SameSite` must be one
        of `strict`/`lax`/`none` and `SameSite=None` requires `Secure`
        (RFC 6265bis 5.4.7); the `__Secure-`/`__Host-` name prefixes require
        `Secure` (and, for `__Host-`, `Path=/` with no `Domain`) per
        RFC 6265bis 4.1.3. A control character or an attribute separator in the
        name, value, path, or domain is a header-injection vector and is refused
        here rather than trusted as far as the serializer.

        Args:
            max_age: Lifetime in seconds; the attribute is omitted when None.
            expires: An HTTP-date string, emitted verbatim as `Expires`.
            path: `Path` attribute, `/` by default; an empty string omits it.
            domain: `Domain` attribute; None keeps the cookie host-only.
            secure: Adds `Secure`, restricting the cookie to HTTPS.
            httponly: Adds `HttpOnly`, hiding the cookie from scripts.
            samesite: `strict`, `lax` (the default), or `none`; None omits the attribute.

        Raises:
            ValueError: Any of the attribute rules or injection checks above failed.
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
        """Expire a cookie by resending it empty with `Max-Age=0` and a 1970 `Expires`.

        A browser replaces a stored cookie only when the name, `Path`, and
        `Domain` all match, so this must be given the same `path` and `domain`
        the cookie was set with -- the defaults clear a cookie that was set with
        the defaults, and a cookie scoped to `/admin` is not cleared by a
        deletion at `/`. `SameSite` is omitted, and `Secure` is carried for a
        `__Secure-`/`__Host-` name because a prefixed cookie can only be cleared
        by a Set-Cookie that still meets the prefix's attribute rules.
        """
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
        """Replace every `cache-control` header on this response with `policy`.

        Existing `cache-control` headers are dropped first rather than appended
        to, because a cache reads the first line it finds and a second one added
        after a permissive first would have no effect.
        """
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))


class TextResponse(Response):
    """UTF-8 text, sent as `text/plain; charset=utf-8`.

    Encodes `body` with UTF-8 and sets `content-length` from the encoded bytes.
    Status defaults to 200. `background`, keyword-only here, is awaited by the
    application after the response has been sent.
    """

    media_type = b"text/plain; charset=utf-8"

    def __init__(
        self, body: str, status: int = 200, *, background: Background | None = None
    ) -> None:
        super().__init__(body.encode("utf-8"), status=status, background=background)


class JSONResponse(Response):
    """A JSON document, sent as `application/json`.

    Serializes with wreath's own encoder, which is compact (no spaces after
    separators) and stricter than the stdlib: an object key that is not a `str`
    raises `TypeError`, a non-finite float raises `ValueError`, and a value no
    encoder understands raises `TypeError`. Dates, times, datetimes and
    durations encode as ISO-8601 strings, and an object defining `__jsonable__`
    is asked how it wants to be encoded. Status defaults to 200. `background`,
    keyword-only here, is awaited after the response has been sent.
    """

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
    """Build a 200 `text/plain; charset=utf-8` response from `body`.

    One of the three builders the application applies to a handler's return
    value, chosen when the value's type is exactly `str`; a `str` subclass goes
    through `TextResponse` instead. The result is byte-for-byte what
    `TextResponse(body)` produces, which `tests/test_response_fast_path.py`
    asserts. There is no type check and no status argument: a handler that needs
    another status or another header constructs the response itself.

    Raises:
        UnicodeEncodeError: `body` holds a lone surrogate and cannot be encoded.
    """
    return _build_response(body.encode("utf-8"), _TEXT_TYPE_HEADER)


def coerce_json(data: Any) -> Response:
    """Build a 200 `application/json` response from `data`.

    Applied to a handler's return value when its type is exactly `dict`; a list,
    tuple, scalar, None, or `dict` subclass reaches `JSONResponse` instead, with
    the same encoder and the same result. Accepts anything that encoder
    understands and refuses the rest, at the same strictness as `JSONResponse`.

    Raises:
        TypeError: An object key that is not a `str`, or an unserializable value.
        ValueError: A non-finite float, which has no JSON spelling.
    """
    return _build_response(_json_dumps(data), _JSON_TYPE_HEADER)


def coerce_bytes(body: bytes) -> Response:
    """Build a 200 `application/octet-stream` response from `body`.

    Applied to a handler's return value when its type is exactly `bytes`; the
    bytes are sent unchanged. The content type is fixed, so a handler returning
    bytes of a known type returns `Response(body, media_type=...)` instead.
    """
    return _build_response(body, _OCTET_TYPE_HEADER)


@dataclass(frozen=True, slots=True)
class ProblemDetail:
    """An RFC 9457 problem document -- the one error shape wreath produces.

    Every error a wreath application returns is this document, serialized as
    `application/problem+json` by `ProblemResponse`: a raised `HTTPException`, a
    request-validation failure, and an unhandled exception alike. It is
    emphatically not `{"detail": ...}`; a 500 renders

    ```json
    {"type":"about:blank","title":"Internal Server Error","status":500,
     "detail":"Internal Server Error"}
    ```

    Frozen and slotted, so an application can build one per error class at import
    time and hand the same instance to a `ProblemResponse` on every request.

    Args:
        status: HTTP status code, repeated inside the document body.
        title: Short summary of the problem kind; defaults to the status phrase.
        detail: Explanation specific to this one occurrence.
        type: URI identifying the problem kind, `about:blank` when unclassified.
        instance: URI identifying this occurrence, typically the request path.
        extensions: Extra top-level members merged into the document last.
    """

    status: int
    title: str | None = None
    detail: str | None = None
    type: str = "about:blank"
    instance: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The document as a JSON-ready mapping, in RFC 9457 member order.

        `type`, `title`, and `status` are always present. `detail` and
        `instance` appear only when they are not None, and `extensions` is
        merged last -- so an extension reusing a reserved member name overwrites
        the value above it. `title` falls back to the `HTTPStatus` phrase for
        `status`, or to `HTTP Error` when the status is not one the stdlib
        knows, so a custom status still yields a complete document.
        """
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
    """An RFC 9457 error response, sent as `application/problem+json`.

    What every wreath error becomes on the wire. The response status is the
    problem's `status` rather than a separate argument, so the code in the
    status line and the one inside the body cannot disagree. The body is
    `ProblemDetail.as_dict()` encoded as JSON.

    Give it either a ready `ProblemDetail` as `problem`, or the document's
    members as keyword arguments -- in which case `status` is required and the
    rest default exactly as on `ProblemDetail`. When `problem` is given, the
    member keyword arguments are ignored.

    Args:
        problem: A ready problem document, replacing the member keywords.
        status: Required when `problem` is None; becomes the response status.
        headers: Extra headers, such as the `www-authenticate` an `Unauthorized` carries.
        background: Awaited by the application after the response has been sent.

    Raises:
        TypeError: Neither `problem` nor `status` was given.
    """

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
    """A UTF-8 HTML document, sent as `text/html; charset=utf-8`.

    Encodes `body` with UTF-8 and sets `content-length` from the encoded bytes.
    Status defaults to 200. Nothing is escaped or validated -- the markup is sent
    exactly as given, so interpolated user text is escaped by the caller or by a
    template (`wreath.templates` escapes by default). `background`, keyword-only
    here, is awaited after the response has been sent.
    """

    media_type = b"text/html; charset=utf-8"

    def __init__(
        self, body: str, status: int = 200, *, background: Background | None = None
    ) -> None:
        super().__init__(body.encode("utf-8"), status=status, background=background)


class RedirectResponse(Response):
    """A redirect: an empty body and a `location` header.

    Status defaults to 307, which preserves the request method and body; pass
    303 to send a browser to a GET after a POST, or 308/301 for a permanent
    move. No `content-type` is emitted (there is no representation to describe),
    and `content-length` is 0.

    The target is percent-encoded on the way into the header, so a URL holding a
    space or a non-ASCII character is emitted in a form a client accepts. Its
    scheme is checked against `allowed_schemes` rather than trusted: a
    `javascript:` or `data:` target in a `Location` is script execution on the
    origin that redirected. A relative target, and a protocol-relative
    `//host/path` that inherits the current scheme, are always allowed.

    Args:
        url: Redirect target, relative or absolute.
        status: A 3xx status, 307 by default.
        background: Awaited by the application after the response has been sent.

    Raises:
        ValueError: `url` names a scheme outside `allowed_schemes`.
    """

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
    """A response whose body is produced chunk by chunk by an async iterable.

    Not a `Response` subclass, and it invents no headers: it sends exactly the
    `headers` given. In particular there is **no `content-type`** and **no
    `content-length`**, so an HTTP/1.1 server frames the body with chunked
    transfer encoding (an HTTP/1.0 one has to frame it by closing the
    connection). A caller who knows the length sets the header itself. Status
    defaults to 200.

    Each `bytes` the iterable yields becomes one `http.response.body` message
    with `more_body=True`, sent as it is produced, and a terminal empty message
    closes the body. Because the length is unknown up front the response cannot
    be cached or replayed -- `wreath.response_cache` and the idempotency
    middleware skip it by design.

    Args:
        body: Async iterable of `bytes` chunks; a `str` chunk is not encoded for you.
        headers: The complete header list to send, empty by default.
        background: Awaited by the application after the response has been sent.
    """

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
        """Replace every `cache-control` header on this response with `policy`.

        Identical in behaviour to `Response.set_cache_control()`, and defined
        again here only because `StreamingResponse` is not a `Response`
        subclass. On an `SSEResponse` this is also how the constructor's
        `no-cache` is displaced, since dropping first is what keeps the
        stricter policy from landing behind it.
        """
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))

    async def __call__(self, send: Send) -> None:
        """Emit the start message, then one body message per chunk.

        Nothing is buffered: a chunk goes out as the iterable produces it, so an
        iterable that awaits between chunks streams. `content-length` is not
        computed -- the size is not known when the headers are sent.

        If the peer disconnects mid-body the server's `send` raises, and that
        exception unwinds through here rather than being swallowed, leaving the
        iterable abandoned at its suspension point. Either way the deferred
        cleanup runs: `wreath.binding` parks the release of request-scoped
        resources a streaming handler borrowed (a pooled database connection,
        say) on this response, and the `finally` below runs it exactly once on
        success, failure, and cancellation alike.
        """
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
    """One Server-Sent Event, as `SSEResponse` frames it.

    Every field is optional, and an instance carrying none frames as a bare `:`
    comment -- the conventional keep-alive. `data` may be multi-line and is
    re-framed as one `data:` field per line; `comment` behaves the same way.
    Fields are emitted in the order comment, `event`, `id`, `retry`, `data`, and
    a blank line terminates the event.

    A CR or LF in `event` or `id` raises `ValueError` when the event is framed,
    not when it is constructed: either character ends the field, so passing user
    text through one would let a caller append arbitrary frames to the stream.

    Args:
        data: The payload; `bytes` are decoded as UTF-8, anything else is `str()`-ed.
        event: Event name the client dispatches on, `message` when omitted.
        id: Last-Event-ID the client echoes when it reconnects.
        retry: Reconnection delay in milliseconds the client should adopt.
        comment: A `:`-prefixed line ignored by clients, used as a keep-alive.
    """

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

    `data` and `comment` are multi-line by construction and are re-framed
    per line by `_sse_lines`. `event` and `id` are not: a CR or LF in
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
    """Frame one event to the `text/event-stream` wire format.

    Accepts a `ServerSentEvent`, a `str`/`bytes` (treated as `data`), or a
    mapping with `data`/`event`/`id`/`retry`/`comment` keys; anything else is a
    `TypeError`. Shape dispatch and coercion stay here, in Python, and only the
    framing itself goes through `_frame_fields` -- `_core.sse_frame` when the
    accelerator is loaded, `_sse_frame_fields` under `WREATH_PURE=1` or a
    source checkout with no build. The two agree byte for byte, which
    `tests/test_sse_frame_parity.py` asserts.
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
    return _frame_fields(
        None if comment is None else str(comment),
        None if name is None else str(name),
        None if ident is None else str(ident),
        None if retry is None else int(retry),
        None if data is None else (
            data.decode("utf-8") if isinstance(data, bytes) else str(data)
        ),
    )


def _sse_frame_fields(
    comment: str | None,
    name: str | None,
    ident: str | None,
    retry: int | None,
    data: str | None,
) -> bytes:
    """Frame already-resolved SSE fields. The parity contract for `sse.c`.

    Split from `_encode_sse` so the native twin has a narrow, exactly
    mirrorable boundary: shape dispatch and coercion stay in Python, and only
    the framing -- the part that walks the payload -- crosses.
    """
    lines: list[str] = []
    if comment is not None:
        _sse_lines("", comment, lines)
    if name is not None:
        lines.append(_sse_single_line("event", name))
    if ident is not None:
        lines.append(_sse_single_line("id", ident))
    if retry is not None:
        lines.append(f"retry: {retry}")
    if data is not None:
        _sse_lines("data", data, lines)
    if not lines:
        lines.append(":")  # bare keep-alive comment
    return ("\n".join(lines) + "\n\n").encode("utf-8")


if _core is not None and hasattr(_core, "sse_frame"):
    _frame_fields = _core.sse_frame
else:
    _frame_fields = _sse_frame_fields


class SSEResponse(StreamingResponse):
    """Server-Sent Events over the streaming-response plumbing.

    Frames each item an async iterable yields (a `ServerSentEvent`, a
    `str`/`bytes` treated as `data`, or a mapping with the same keys) to the
    `text/event-stream` wire format: one `data:` line per line of payload,
    optional `event:`/`id:`/`retry:` lines, `:`-prefixed comments, and a blank
    line ending each event. Status defaults to 200 and the constructor prepends
    the headers that keep a stream from being buffered or cached --
    `content-type: text/event-stream`, `cache-control: no-cache`,
    `x-accel-buffering: no`, `connection: keep-alive` -- with any `headers`
    given appended after them. There is no `content-length`; the body ends when
    the iterable does.

    **Nothing is emitted on a timer.** No keep-alive is sent unless the
    application yields one (`ServerSentEvent(comment="ping")`), and no `retry:`
    hint is sent unless an event carries `retry`. An idle stream is therefore
    silent, and an intermediary free to drop it after its own idle timeout, so
    an application that must survive one yields a periodic comment itself.

    This is the SSE *transport* only; a progress or status convention on top of
    it is left to the application (consumer-owned, by design). `wreath.progress`
    is one such convention built on this class.

    Args:
        events: Async iterable of events to frame, one message each.
        headers: Extra headers appended after the four preset above.
        background: Awaited by the application after the stream has finished.
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


class _Unsatisfiable:
    """A `Range` that names nothing inside the representation."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSATISFIABLE"


#: A range past the end of the representation. Distinct from ``None`` -- which
#: means "ignore the header and send everything" -- because the two have
#: opposite answers: 416 versus 200 (RFC 9110 §14.2, §15.5.17).
UNSATISFIABLE: Final = _Unsatisfiable()


def parse_range(header: str | None, size: int) -> tuple[int, int] | _Unsatisfiable | None:
    """Resolve a `Range` header against a representation of `size` bytes.

    Returns an inclusive `(first, last)`, `UNSATISFIABLE`, or `None`
    for a header to ignore.

    That last case is most of the grammar. RFC 9110 §14.2 says a recipient
    *may* ignore a `Range` it does not understand, and must serve the whole
    representation when it does -- so a multi-range request, a unit that is not
    `bytes`, and anything malformed all fall through to a normal 200 rather
    than an error. Only a syntactically valid range that lies entirely past the
    end is a 416; getting that distinction backwards either breaks resumable
    downloads or answers 416 to clients asking politely.
    """
    if not header:
        return None
    unit, separator, spec = header.partition("=")
    if not separator or unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        return None                     # multi-range: not implemented, so ignored
    first_text, separator, last_text = spec.partition("-")
    if not separator:
        return None
    try:
        if not first_text:
            # A suffix range: the last N bytes.
            length = int(last_text)
            if length <= 0:
                return UNSATISFIABLE
            if size == 0:
                return UNSATISFIABLE
            return (max(0, size - length), size - 1)
        first = int(first_text)
        last = int(last_text) if last_text else size - 1
    except ValueError:
        return None
    if first < 0 or last < 0:
        return None
    if first >= size:
        return UNSATISFIABLE
    if last < first:
        return None
    return (first, min(last, size - 1))


_FILE_CHUNK = 256 * 1024
#: Chunks a file reader may buffer ahead of the ASGI send. Bounds read-ahead
#: memory to _FILE_READAHEAD * _FILE_CHUNK regardless of file size.
_FILE_READAHEAD = 4
#: End-of-file sentinel handed from the reader worker to the sender.
_EOF = object()


async def _send_from_descriptor(
    fd: int,
    size: int,
    status: int,
    headers: list[tuple[bytes, bytes]],
    send: Send,
    offset: int = 0,
) -> None:
    """Stream `size` bytes from `fd`, starting at `offset`.

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
    if offset:
        os.lseek(fd, offset, os.SEEK_SET)

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
                #
                # It does mean this thread is held for the life of a slow
                # response -- a slow client parks it. That is the deliberate
                # side of the trade: the alternative is one executor submission
                # per 256 KiB chunk, which
                # `tests/test_framework_features.py::test_file_response_uses_
                # bounded_executor_submissions` exists to forbid. Bounded
                # read-ahead with one worker means the worker waits.
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                if stop.is_set():
                    return
            asyncio.run_coroutine_threadsafe(queue.put(_EOF), loop).result()
        except BaseException as exc:  # noqa: BLE001 - relayed to the sender
            # A worker thread is not a task: there is no cancellation to honour
            # and `KeyboardInterrupt` reaches only the main thread, so breadth
            # here is a relay rather than a swallow -- the sender re-raises
            # whatever arrives on the queue.
            #
            # `RuntimeError` is the single way that relay can fail:
            # `run_coroutine_threadsafe` raises it once the loop is closed, and a
            # closed loop means the sender is already gone, so there is nobody
            # left to tell. Anything else belongs on the executor future rather
            # than in a suppression.
            with contextlib.suppress(RuntimeError):
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
    """`content-disposition` for an attachment named `filename`.

    The name reaches a quoted-string, so a bare interpolation lets a `"` end
    the quoting and a CR/LF end the header. RFC 6266 §4.1 wants the quoted form
    escaped and a non-ASCII name carried in `filename*` (RFC 5987) with an
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

    Not a `Response` subclass: there is no in-memory body, and the file is read
    only while the response is being sent. `content-length` is always emitted --
    the size comes from an `fstat` of the descriptor being served, so it is the
    length actually sent rather than a promise made from a separate stat. The
    content type is guessed from the path with `mimetypes` unless `media_type`
    says otherwise, falling back to `application/octet-stream`. Status is 200,
    or 206 automatically whenever `range` is given.

    **Conditional requests are the caller's job.** This class emits no `etag`,
    no `last-modified`, and no `accept-ranges`, and it never reads the request's
    `Range` header. Serving a byte window is a two-step contract: the caller
    resolves the header with `parse_range()`, sets `content-range` itself, and
    passes the resolved window as `range`. `wreath.staticfiles` does all of that
    and is the thing to reach for when serving a directory of files.

    The open and the stat happen lazily, in a worker thread, when the response is
    sent -- so a missing or unreadable path raises `FileNotFoundError` or
    `PermissionError` from `__call__`, after the handler has already returned.
    That is past the application's exception boundary, so it reaches the server
    as a failed ASGI application rather than a problem+json 404. Route-level code
    that wants a 404 checks existence first, as `wreath.staticfiles` does by
    opening the file itself and using `from_descriptor()`.

    Args:
        path: Filesystem path to send; opened at send time, not here.
        status: Status for a whole-file response; ignored when `range` is given.
        headers: Headers to send before the generated content-type and length.
        media_type: Overrides the type guessed from the path.
        filename: Sends `content-disposition` as an attachment under this name.
        background: Awaited by the application after the file has been sent.
        range: An inclusive `(first, last)` byte window from `parse_range()`.

    Raises:
        ValueError: `filename` contains a control character.
    """

    __slots__ = (
        "_fd", "_stat", "background", "headers", "media_type", "path", "range",
        "status",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        status: int = 200,
        headers: Iterable[tuple[bytes, bytes]] | None = None,
        media_type: bytes | None = None,
        filename: str | None = None,
        background: Background | None = None,
        range: tuple[int, int] | None = None,
    ) -> None:
        self.path = os.fspath(path)
        self.range = range
        self.status = 206 if range is not None else status
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
        range: tuple[int, int] | None = None,
    ) -> FileResponse:
        """Build a response over an already-open descriptor.

        The descriptor is owned by the response. Sending it hands ownership to
        the reader, which closes it when the send finishes, on success and on
        error alike. A response that is *never* sent -- a handler that raises
        after building it, a middleware that replaces it, a conditional request
        answered 304 instead -- closes it from `close()`, which
        `__del__` calls, so an abandoned response costs a descriptor only until
        it is collected rather than for the life of the process. Call `close()`
        directly to release it at a known point.

        The file the opener checked is the
        file served -- there is no reopen-by-name window between the check and
        the send, which is what lets `wreath.staticfiles` guarantee it serves
        only from beneath its root. `name` is used only to guess the content
        type, `stat` only for the length, and `status` becomes 206 when `range`
        is given. No `background` callback is attached.
        """
        self = cls.__new__(cls)
        self.path = name
        self.range = range
        self.status = 206 if range is not None else status
        self.headers = list(headers) if headers is not None else []
        guessed, _ = mimetypes.guess_type(name)
        self.media_type = (guessed or "application/octet-stream").encode("ascii")
        self.headers.append((_CONTENT_TYPE, self.media_type))
        self.background = None
        self._fd = fd
        self._stat = stat
        return self

    def close(self) -> None:
        """Release an unsent descriptor taken by `from_descriptor()`.

        A no-op for a `FileResponse` built from a path (it holds no descriptor
        until it is sent) and for one that has already been sent (`__call__`
        hands the descriptor to the reader and clears the reference before it
        streams, so there is never two owners). Idempotent, which matters
        because closing twice would close whatever descriptor the number has
        since been reused for.
        """
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        """Close a descriptor this response still owns when it is collected.

        `from_descriptor()` takes ownership of an open file, and only the reader
        used to close it -- which never runs if the response is not sent. The
        `getattr` guards a `cls.__new__` instance whose attributes were never
        assigned, where `__del__` would otherwise raise during collection.
        """
        if getattr(self, "_fd", None) is not None:
            self.close()

    def set_cache_control(self, policy: CacheControl) -> None:
        """Replace every `cache-control` header on this response with `policy`.

        Identical in behaviour to `Response.set_cache_control()`, and defined
        again here only because `FileResponse` is not a `Response` subclass.
        """
        self.headers[:] = [
            (name, value) for name, value in self.headers if name.lower() != b"cache-control"
        ]
        self.headers.append((b"cache-control", policy.to_header()))

    def _window(self, size: int) -> tuple[int, int]:
        """The (offset, length) to send: the whole file, or the asked-for slice."""
        if self.range is None:
            return 0, size
        first, last = self.range
        return first, max(0, min(last, size - 1) - first + 1)

    async def __call__(self, send: Send) -> None:
        """Open the file if needed, then stream it with a `content-length`.

        The length is the window's length, so a 206 advertises the slice rather
        than the file. One reader thread owns the descriptor, reads it in 256 KiB
        chunks, and hands them over a bounded queue, which is what keeps
        read-ahead bounded no matter how large the file is; the descriptor is
        closed and the reader joined in a `finally`, so a client that disconnects
        mid-download leaks neither. A disconnect surfaces as the server's `send`
        raising, and that exception propagates.

        A `HEAD` request is not special-cased here: the file is still opened and
        read, and the application discards each body message, leaving the correct
        `content-length` on a response with no body. Under `wreath`'s own server
        a whole-file send hands the descriptor to the server's own file path
        instead of reading it in Python; a ranged send always takes the portable
        reader, which is the only one that can apply an offset.
        """
        if self._fd is not None:
            # Already opened beneath a trusted root (see wreath.staticfiles);
            # serve the descriptor we hold rather than reopening a pathname.
            size = self._stat.st_size if self._stat is not None else 0
            fd, self._fd = self._fd, None
            await self._stream(fd, size, send)
            return
        # Open once (open + fstat in a single worker), then stream through the
        # same single-submission reader. A missing file raises here.
        fd, size = await asyncio.to_thread(_open_fd, self.path)
        await self._stream(fd, size, send)

    async def _stream(self, fd: int, size: int, send: Send) -> None:
        offset, length = self._window(size)
        # The native path sends a whole file from a descriptor and has nowhere
        # to put an offset, so a partial response takes the portable reader.
        if self.range is None and await _send_native_descriptor(
            fd, size, self.status, self.headers, send
        ):
            return
        await _send_from_descriptor(fd, length, self.status, self.headers, send, offset)
