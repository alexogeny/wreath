"""Lazy ASGI request wrapper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ._auth.models import Identity
from ._codecs import parse_cookies, parse_qs
from ._headers import build_header_map, find_header
from ._json import loads as _json_loads
from ._multipart import parse as multipart_parse
from .exceptions import (
    ClientDisconnect,
    PayloadTooLarge,
    RequestHeaderFieldsTooLarge,
)
from .state import State


@dataclass(frozen=True, slots=True)
class RequestLimits:
    """What one request may buffer in memory before it is refused.

    These bound the framework side of a request, and so apply behind any
    conforming ASGI server -- `wreath.server`'s own limits stop a body at the
    socket, but Uvicorn and friends will hand over whatever arrives. They are
    enforced while receiving, before an oversized body is joined or a part is
    copied out of it.

    Defaults are conservative but chosen not to break working applications, and
    are a pre-1.0 decision: they may tighten before 1.0. Raise them on the
    application (`Wreath(limits=...)`) rather than working around them.
    """

    #: Total bytes `Request.body()` will buffer. Refused with 413.
    max_body_bytes: int = 16 * 1024 * 1024
    #: Parts one multipart form may contain.
    max_parts: int = 1024
    #: Header-block bytes per multipart part.
    max_part_header_bytes: int = 16 * 1024
    #: Bytes one multipart part (including an uploaded file) may hold in memory.
    max_part_bytes: int = 8 * 1024 * 1024
    #: Aggregate bytes all retained multipart parts may hold in memory, bounding
    #: the payload retained by parsed fields and files independently of the body
    #: and per-part limits. Defaults to `max_body_bytes`; lower it to bound the
    #: parse-time amplification (body plus materialized parts) more tightly.
    max_form_memory_bytes: int = 16 * 1024 * 1024
    #: Bytes the `Cookie` header may carry. Browsers stay far under this; a
    #: request past it is either broken or probing, and parsing it builds a dict
    #: proportional to whatever arrived -- on any route that reads a session, a
    #: CSRF token, or a bearer cookie, which is most of them. Refused with 431.
    #:
    #: There is deliberately no `max_headers` beside it. The header *count* is
    #: already bounded by every server's frame limits, and enforcing a second
    #: bound here cost a crossing in `pre_activation` -- the number
    #: `docs/agents/request-boundary-baseline.json` asks changes to protect.
    #: The cookie bound is the one that pays for itself, because it guards a
    #: parse rather than a length.
    max_cookie_bytes: int = 16 * 1024
    #: Fields one urlencoded form body may contain. A large body is otherwise
    #: only bounded by `max_body_bytes`, which admits millions of tiny fields;
    #: this bounds the field count directly, refused while scanning with 413.
    max_form_fields: int = 1024

    def __post_init__(self) -> None:
        for name in (
            "max_body_bytes",
            "max_cookie_bytes",
            "max_parts",
            "max_part_header_bytes",
            "max_part_bytes",
            "max_form_memory_bytes",
            "max_form_fields",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


DEFAULT_LIMITS = RequestLimits()


class UploadedFile:
    """One file field from a multipart form."""

    __slots__ = ("data", "field_name", "filename", "headers")

    def __init__(
        self,
        field_name: str,
        filename: str,
        headers: list[tuple[bytes, bytes]],
        data: bytes,
    ) -> None:
        self.field_name = field_name
        self.filename = filename
        self.headers = headers
        self.data = data

    @property
    def content_type(self) -> str:
        value = find_header(self.headers, b"content-type")
        return value.decode("latin-1") if value else "application/octet-stream"


class FormData:
    """Parsed form fields plus uploaded files (first value wins per name)."""

    __slots__ = ("_all", "fields", "files")

    def __init__(
        self,
        fields: dict[str, str],
        files: dict[str, UploadedFile],
        all_values: dict[str, list[str]] | None = None,
    ) -> None:
        self.fields = fields
        self.files = files
        self._all = all_values if all_values is not None else {
            name: [value] for name, value in fields.items()
        }

    def getlist(self, name: str) -> list[str]:
        """Every value submitted under ``name``, in order.

        ``fields`` keeps the first, which is what most handlers want and what
        the binding layer reads. The rest were being dropped with no way to see
        them -- and a repeated field is exactly where an upstream proxy or WAF
        may have read a *different* value than the application will.
        """
        return list(self._all.get(name, ()))

    def __getitem__(self, name: str) -> str:
        return self.fields[name]

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.fields.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def __iter__(self) -> Any:
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)


#: RFC 2046 §5.1.1: 1..70 characters from this set, not ending in a space.
_BOUNDARY_CHARS = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'()+_,-./:=? "
)


def _valid_boundary(value: bytes) -> bool:
    """Whether ``value`` is a boundary the RFC allows.

    Unchecked, a boundary was whatever followed `boundary=` -- any length, any
    byte. That is where a parser differential lives: the proxy or WAF in front
    reads the body one way and this reads it another, and the disagreement is
    the whole attack.
    """
    return (
        1 <= len(value) <= 70
        and not value.endswith(b" ")
        and all(byte in _BOUNDARY_CHARS for byte in value)
    )


def _multipart_boundary(content_type: bytes) -> bytes | None:
    for fragment in content_type.split(b";"):
        key, sep, value = fragment.strip(b" \t").partition(b"=")
        if sep and key.strip(b" \t").lower() == b"boundary":
            value = value.strip(b" \t")
            if len(value) >= 2 and value.startswith(b'"') and value.endswith(b'"'):
                value = value[1:-1]
            return value if _valid_boundary(value) else None
    return None

Receive = Callable[[], Awaitable[dict[str, Any]]]
_MISSING = object()


class Request:
    """A deliberately small request object backed directly by an ASGI scope."""

    __slots__ = (
        "_body",
        "_cookies",
        "_form",
        "_header_map",
        "_header_scanned",
        "_identity",
        "_json",
        "_limits",
        "_path_params",
        "_receive",
        "_route_outcome",
        "_state",
        "_context",
        "_scope",
    )

    def __init__(
        self,
        scope: Any,
        receive: Receive,
        path_params: dict[str, str] | None = None,
        limits: RequestLimits = DEFAULT_LIMITS,
    ) -> None:
        if isinstance(scope, dict):
            self._scope: dict[str, Any] | None = scope
            self._context: Any | None = None
        else:
            self._scope = None
            self._context = scope
        self._receive = receive
        self._body: bytes | object = _MISSING
        self._cookies: dict[str, str] | object = _MISSING
        self._json: Any = _MISSING
        self._form: FormData | object = _MISSING
        self._header_map: dict[bytes, bytes] | None = None
        self._header_scanned = False
        self._path_params = path_params
        self._identity: Identity | None = None
        self._route_outcome: str | None = None
        self._state: State | None = None
        self._limits = limits

    @property
    def identity(self) -> Identity | None:
        return self._identity

    @property
    def authenticated(self) -> bool:
        return self._identity is not None

    def _set_identity(self, identity: Identity | None) -> None:
        self._identity = identity

    @property
    def state(self) -> State:
        state = self._state
        if state is None:
            state = self._state = State()
            # The pipeline tracks the routing outcome in a slot so that
            # requests whose hooks never touch `state` skip the State
            # allocation entirely; the first read materializes it here.
            outcome = self._route_outcome
            if outcome is not None:
                state.route_outcome = outcome
        return state

    def _set_route_outcome(self, outcome: str) -> None:
        self._route_outcome = outcome
        state = self._state
        if state is not None:
            state.route_outcome = outcome

    def _get_route_outcome(self) -> str | None:
        # State wins when it exists: a hook may have overwritten the value.
        state = self._state
        if state is not None:
            return cast("str | None", state.get("route_outcome"))
        return self._route_outcome

    @property
    def scope(self) -> dict[str, Any]:
        scope = self._scope
        if scope is None:
            context = self._context
            assert context is not None
            scope = self._scope = context._asgi_scope()
        return scope

    @property
    def method(self) -> str:
        context = self._context
        if context is not None:
            return context.method
        scope = self._scope
        assert scope is not None
        return scope["method"]

    @property
    def path(self) -> str:
        context = self._context
        if context is not None:
            return context.path
        scope = self._scope
        assert scope is not None
        return scope["path"]

    @property
    def path_params(self) -> dict[str, str]:
        params = self._path_params
        if params is None:
            params = self._path_params = {}
        return params

    @path_params.setter
    def path_params(self, params: dict[str, str]) -> None:
        self._path_params = params

    @property
    def client(self) -> tuple[str, int | None] | None:
        context = self._context
        if context is not None:
            return cast("tuple[str, int | None] | None", context.client)
        scope = self._scope
        assert scope is not None
        return scope.get("client")

    @property
    def scheme(self) -> str:
        context = self._context
        if context is not None:
            return cast(str, context.scheme)
        scope = self._scope
        assert scope is not None
        return scope.get("scheme", "http")

    def _set_client(self, client: tuple[str, int | None]) -> None:
        # ProxyHeadersMiddleware rewrites the peer from X-Forwarded-For. The
        # write goes to the context when one backs this request so the ASGI
        # scope is never materialized just to carry it.
        context = self._context
        if context is not None:
            context._set_client(client)
            return
        scope = self._scope
        assert scope is not None
        scope["client"] = client

    def _set_scheme(self, scheme: str) -> None:
        context = self._context
        if context is not None:
            context._set_scheme(scheme)
            return
        scope = self._scope
        assert scope is not None
        scope["scheme"] = scheme

    @property
    def query_string(self) -> bytes:
        context = self._context
        if context is not None:
            return context.query_string
        scope = self._scope
        assert scope is not None
        return scope.get("query_string", b"")

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        context = self._context
        if context is not None:
            return context.headers
        scope = self._scope
        assert scope is not None
        return scope.get("headers", [])

    @property
    def cookies(self) -> dict[str, str]:
        # Parsed once and cached request-locally, like `_body` and
        # `_header_map`: repeated reads are common (session, CSRF, auth) and
        # each one otherwise rescanned the header list and rebuilt the dict.
        cached = self._cookies
        if cached is not _MISSING:
            return cast(dict[str, str], cached)
        header_map = self._header_map
        value = (
            find_header(self.headers, b"cookie")
            if header_map is None
            else header_map.get(b"cookie")
        )
        if value is not None and len(value) > self._limits.max_cookie_bytes:
            raise RequestHeaderFieldsTooLarge(
                f"Cookie header is {len(value)} bytes; the limit is "
                f"{self._limits.max_cookie_bytes}"
            )
        parsed: dict[str, str] = {} if value is None else parse_cookies(value)
        self._cookies = parsed
        return parsed

    def _set_header(self, name: bytes, value: bytes) -> None:
        # Only ProxyHeadersMiddleware needs this: it rewrites Host from a
        # trusted X-Forwarded-Host before anything downstream reads it. The
        # cache maintenance lives here because the caches do.
        headers = self.headers
        for index, (existing, _value) in enumerate(headers):
            if existing == name:
                headers[index] = (name, value)
                break
        else:
            headers.append((name, value))
        # Updated rather than dropped. The loop above replaces the first match
        # and the index holds first-value-wins, so writing the new value keeps
        # them consistent -- and ProxyHeaders is precisely the caller that has
        # just built the index, so discarding it here would make the next
        # consumer rebuild the whole thing.
        if self._header_map is not None:
            self._header_map[name] = value

    def _index_headers(self) -> dict[bytes, bytes]:
        """Materialize the first-value header index for multi-header consumers."""
        header_map = self._header_map
        if header_map is None:
            header_map = self._header_map = build_header_map(self.headers)
            self._header_scanned = True
        return header_map

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
        # ASGI servers must deliver header names already lowercased, so the
        # raw keys are usable directly; first value wins for duplicates.
        target = name.encode("latin-1") if isinstance(name, str) else name
        target = target.lower()
        header_map = self._header_map
        if header_map is None:
            if not self._header_scanned:
                # A lone lookup is cheaper as a single scan than as a dict
                # build; the map is only materialized on the second lookup.
                self._header_scanned = True
                value = find_header(self.headers, target)
                return value.decode("latin-1") if value is not None else default
            header_map = self._header_map = build_header_map(self.headers)
        value = header_map.get(target)
        if value is None:
            return default
        return value.decode("latin-1")

    @property
    def locale(self) -> str:
        """The caller's preferred language tag, or ``"en"``.

        Read from ``Accept-Language``, highest ``q`` first. This is what
        :func:`wreath.temporal.relative` takes, so "3 hours ago" can be
        translated later without finding every call site that renders one.

        Deliberately forgiving: the header is attacker-controlled and a
        malformed one must never fail a request, so anything unparseable falls
        back to the default rather than raising. Only the tag is returned — no
        negotiation against a list of supported languages, because the caller
        knows which those are and this does not.
        """
        header = self.header("accept-language")
        if not header:
            return "en"
        best: tuple[float, int, str] | None = None
        for index, part in enumerate(header.split(",")):
            tag, _, parameters = part.strip().partition(";")
            tag = tag.strip()
            if not tag or tag == "*":
                continue
            quality = 1.0
            # Every parameter, not just the first: `;charset=utf-8;q=0.1` is
            # legal, and reading only the first one scored it 1.0 -- so the tag
            # the client least wanted could win.
            for parameter in parameters.split(";"):
                key, _, raw = parameter.partition("=")
                if key.strip().lower() != "q":
                    continue
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
                break
            # Negated index so that, at equal quality, the earlier tag wins --
            # which is the order the client listed its preference in.
            candidate = (quality, -index, tag)
            if best is None or candidate > best:
                best = candidate
        return best[2] if best is not None else "en"

    async def body(self) -> bytes:
        cached = self._body
        if cached is not _MISSING:
            return cast(bytes, cached)

        limit = self._limits.max_body_bytes
        first_chunk: bytes | None = None
        buffer: bytearray | None = None
        total = 0
        while True:
            message = await self._receive()
            message_type = message["type"]
            if message_type == "http.disconnect":
                # Not an end-of-body. Returning what had arrived handed the
                # handler a truncated payload that could still parse -- half a
                # JSON document is rarely valid, but half a form or half an
                # upload very often is, and the handler had no way to tell.
                self._body = b""
                raise ClientDisconnect(
                    "the client disconnected before the request body was received"
                )
            if message_type != "http.request":
                continue
            body = message.get("body", b"")
            if body:
                # Checked before the chunk is retained and before the join, so
                # an oversized body is refused while it is still arriving rather
                # than after the whole thing is in memory twice. A conforming
                # ASGI server may hand over chunks of any size, and may not
                # have applied a limit of its own.
                if len(body) > limit - total:
                    self._body = b""
                    raise PayloadTooLarge(
                        f"request body exceeds {limit} bytes"
                    )
                total += len(body)
                if first_chunk is None and buffer is None:
                    first_chunk = body
                else:
                    if buffer is None:
                        assert first_chunk is not None
                        buffer = bytearray(first_chunk)
                        first_chunk = None
                    buffer.extend(body)
            if not message.get("more_body", False):
                break

        if buffer is not None:
            result = bytes(buffer)
        elif first_chunk is not None:
            # The common one-chunk request reuses the ASGI bytes object directly.
            result = first_chunk
        else:
            result = b""
        self._body = result
        return result

    async def json(self) -> Any:
        cached = self._json
        if cached is not _MISSING:
            return cached
        result = _json_loads(await self.body())
        self._json = result
        return result

    async def form(self) -> FormData:
        """Parse a urlencoded or multipart request body.

        Returns a :class:`FormData` mapping field names to string values,
        with uploaded files (multipart parts carrying a filename) available
        via :attr:`FormData.files`.
        """
        cached = self._form
        if cached is not _MISSING:
            return cast(FormData, cached)
        # Via the `headers` property, not `self.scope`: on the native path the
        # ASGI scope is lazily built, and reading one header should not force it.
        content_type = find_header(self.headers, b"content-type")
        body = await self.body()
        if content_type is not None and content_type.startswith(b"multipart/form-data"):
            boundary = _multipart_boundary(content_type)
            if boundary is None:
                raise ValueError("multipart body without a boundary parameter")
            fields: dict[str, str] = {}
            files: dict[str, UploadedFile] = {}
            limits = self._limits
            retained = 0
            all_values: dict[str, list[str]] = {}
            for part in multipart_parse(
                body,
                boundary,
                limits.max_parts,
                limits.max_part_header_bytes,
                limits.max_part_bytes,
            ):
                if part.name is None:
                    continue
                # Bound the aggregate payload retained across all parts, checked
                # before this part is kept so the crossing part is refused rather
                # than materialised into the result.
                retained += len(part.data)
                if retained > limits.max_form_memory_bytes:
                    raise PayloadTooLarge(
                        f"form parts exceed {limits.max_form_memory_bytes} bytes in memory"
                    )
                if part.filename is not None:
                    files.setdefault(
                        part.name,
                        UploadedFile(part.name, part.filename, part.headers, part.data),
                    )
                else:
                    decoded = part.data.decode("utf-8", "replace")
                    fields.setdefault(part.name, decoded)
                    all_values.setdefault(part.name, []).append(decoded)
            result = FormData(fields, files, all_values)
            self._form = result
            return result
        fields = {}
        every: dict[str, list[str]] = {}
        for key, value in parse_qs(body, self._limits.max_form_fields):
            fields.setdefault(key, value)
            every.setdefault(key, []).append(value)
        result = FormData(fields, {}, every)
        self._form = result
        return result
