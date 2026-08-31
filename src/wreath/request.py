"""The incoming HTTP request, read lazily from an ASGI scope.

`Request` keeps the scope and the receive channel and parses nothing until it is
asked. Method, path, scheme, client, query string and the raw header list are
reads of what the server already delivered, so they cost nothing to touch. The
cookies, the body, the decoded JSON and the parsed form are produced on first
access and cached on the request, so a middleware, a dependency and the handler
that each read the body share one read of it.

`RequestLimits` bounds what a single request may buffer in memory before it is
refused. An application owns one and hands it to every request it builds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from hashlib import new as new_hash
from hmac import compare_digest
from tempfile import TemporaryFile
from typing import TYPE_CHECKING, Any

from ._headers import build_header_map, find_header
from ._json import loads as _json_loads
from ._native import _core
from .digest import Digest, DigestError, DigestPreferences
from .exceptions import (
    BadRequest,
    ClientDisconnect,
    PayloadTooLarge,
    RequestHeaderFieldsTooLarge,
)
from .state import BODY_CHECK_SLOT, State

if TYPE_CHECKING:
    # **This one import decides whether `wreath` can be entered from any door.**
    # At runtime it would load `._auth`, whose `__init__` imports `.backends`
    # and `.cedar`, and both of those import `Request` back from this module --
    # a cycle through a half-built `wreath.request`. Nothing hit it while
    # `wreath/__init__` eagerly imported `.app` first, because that finished
    # this module before anything could ask `._auth` for it; entering through
    # `from wreath import Request` or `import wreath.request` does not.
    # `Identity` appears only in annotations here and this module has postponed
    # evaluation, so there is nothing to import at runtime. Twenty-four modules
    # import `Request` from here and the rest of that graph is untouched --
    # this is the single edge that closed the loop.
    from ._auth.models import Identity


@dataclass(frozen=True, slots=True)
class RequestLimits:
    """What one request may buffer in memory before it is refused.

    These bound the framework side of a request, and so apply behind any
    conforming ASGI server -- `wreath.server`'s own limits stop a body at the
    socket, but Uvicorn and friends will hand over whatever arrives. They are
    enforced while receiving, before an oversized body is joined or a part is
    copied out of it.

    Every one of them refuses with a status a client can act on. All but one
    raise `PayloadTooLarge` (413); `max_cookie_bytes` raises
    `RequestHeaderFieldsTooLarge` (431), because the fix is to send fewer
    cookies rather than a smaller body. The multipart parser limits --
    `max_parts`, `max_part_header_bytes`, `max_part_bytes` -- are enforced
    inside the codec, which raises `ValueError`; `form()` converts those three
    to 413. A body the parser cannot *read* is a different failure and still
    raises `ValueError`.

    Every limit must be positive; constructing one with a zero or negative value
    raises `ValueError`.

    Defaults are conservative but chosen not to break working applications, and
    are a pre-1.0 decision: they may tighten before 1.0. Raise them on the
    application (`Wreath(limits=...)`) rather than working around them.

    Args:
        max_body_bytes: total bytes `Request.body()` buffers before it refuses
        max_parts: parts one multipart form may contain
        max_part_header_bytes: header-block bytes allowed per multipart part
        max_part_bytes: bytes one multipart part may hold in memory
        max_form_memory_bytes: aggregate in-memory bytes across all retained parts
        spool_max_bytes: part size past which the payload spools to a temporary file
        max_cookie_bytes: bytes the `Cookie` header may carry before it refuses
        max_form_fields: fields one urlencoded body may contain
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
    #: Bytes one multipart part may hold in memory before its payload is spooled
    #: to a temporary file. Past this the parsed form keeps no reference to the
    #: payload -- it is written out and dropped, and `UploadedFile` reads it back
    #: from disk on demand. The parse still copies the part out of the body as
    #: `bytes` once on its way to the spool, so this bounds what is *retained*,
    #: not the peak.
    #:
    #: Multipart parsing is incremental: once a file part crosses this value,
    #: subsequent bytes go straight to its temporary file and `form()` never
    #: retains a complete request-body buffer.
    spool_max_bytes: int = 1024 * 1024
    #: Bytes the `Cookie` header may carry. Browsers stay far under this; a
    #: request past it is either broken or probing, and parsing it builds a dict
    #: proportional to whatever arrived -- on any route that reads a session, a
    #: CSRF token, or a bearer cookie, which is most of them. Refused with 431.
    #:
    #: There is deliberately no `max_headers` beside it. The header *count* is
    #: already bounded by every server's frame limits, and enforcing a second
    #: bound here cost a crossing in `pre_activation`.
    #: The cookie bound is the one that pays for itself, because it guards a
    #: parse rather than a length.
    max_cookie_bytes: int = 16 * 1024
    #: Fields one urlencoded form body may contain. A large body is otherwise
    #: only bounded by `max_body_bytes`, which admits millions of tiny fields;
    #: this bounds the field count directly, refused while scanning and before
    #: the offending field is decoded.
    #:
    #: Refused with 413. The codec raises `ValueError`, which `form()` converts
    #: to `PayloadTooLarge`: a limit whose whole purpose is to refuse hostile
    #: input has to report the caller's fault as the caller's.
    max_form_fields: int = 1024

    def __post_init__(self) -> None:
        """Refuse a non-positive limit at construction.

        Zero is not "unlimited" here: every check compares against the value, so
        a zero bound rejects essentially everything -- or, for `spool_max_bytes`,
        spools it. Catching that where the limit is written beats discovering it
        on the first request.
        """
        for name in (
            "max_body_bytes",
            "max_cookie_bytes",
            "spool_max_bytes",
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
    """One file field from a multipart form.

    Built by `Request.form()`, one per part that carried a filename. A payload of
    `RequestLimits.spool_max_bytes` or less stays in memory and is reachable as
    `data`. A larger one is written to an unnamed temporary file, `data` is left
    empty, and the payload is reachable only through `chunks()` (streaming) or
    `read()` (materialising, which undoes the point of spooling for anything
    large). Ask `spooled` which kind you hold rather than testing `data`, since
    a spooled upload and an empty one both report `data == b""`.

    `size` is the payload length either way. `field_name`, `filename` and
    `headers` come from the part itself, so `filename` is whatever the client
    sent -- it is not sanitised, and joining it onto a path unchecked is how a
    caller writes outside the directory it meant to.

    The spool is released by `close()` here, by `FormData.close()`, or when the
    last reference to this object goes away and CPython closes the temporary
    file; the file is deleted when it closes and cannot be reopened. Nothing in
    the framework closes a form for you, so a handler that keeps an upload past
    its own return must copy the bytes out first.

    Args:
        data: the payload when it is held in memory, empty when it is spooled
        spool: an open temporary file holding the payload, or None
        size: payload length, which the caller must pass when it spools
    """

    __slots__ = ("_size", "_spool", "data", "field_name", "filename", "headers")

    def __init__(
        self,
        field_name: str,
        filename: str,
        headers: list[tuple[bytes, bytes]],
        data: bytes | None = None,
        spool: Any = None,
        size: int | None = None,
    ) -> None:
        """Hold a multipart payload, in memory or as an already-written spool.

        `data` and `spool` are alternatives, and nothing here enforces that:
        `size` defaults to `len(data)`, which is 0 for a spooled payload, so a
        caller that passes a spool passes its length too. `_uploaded` is the
        only builder in the framework and does both.
        """
        self.field_name = field_name
        self.filename = filename
        self.headers = headers
        self.data = data if data is not None else b""
        self._spool = spool
        self._size = size if size is not None else len(self.data)

    @property
    def spooled(self) -> bool:
        """Whether this payload lives in a temporary file rather than in memory."""
        return self._spool is not None

    @property
    def size(self) -> int:
        """Payload length in bytes, whether it is spooled or not."""
        return self._size

    def chunks(self, size: int = 64 * 1024) -> Iterator[bytes]:
        """Yield the payload in pieces, without materialising it.

        The streaming read. For an in-memory payload this yields it in one
        piece, so a caller does not have to know which kind it holds. For a
        spooled one it rereads from the start of the temporary file, so two
        iterations both see the whole payload; `size` bounds each read.

        This is a generator, so a closed spool is reported when iteration
        begins rather than when `chunks` is called.

        Raises:
            ValueError: the payload was spooled and the spool has been closed
        """
        if self._spool is None:
            if self.data:
                yield self.data
            return
        if self._spool.closed:
            raise ValueError("this upload's spool has been closed")
        self._spool.seek(0)
        while True:
            chunk = self._spool.read(size)
            if not chunk:
                return
            yield chunk

    def read(self) -> bytes:
        """The whole payload as bytes.

        Materialises a spooled file, which is exactly what spooling avoided --
        fine for a small one, and a way to reintroduce the problem for a large
        one. Prefer `chunks()`.

        Raises:
            ValueError: the payload was spooled and the spool has been closed
        """
        if self._spool is None:
            return self.data
        return b"".join(self.chunks())

    def close(self) -> None:
        """Release the spool, if there is one. Idempotent.

        A no-op for an in-memory payload, which stays readable. For a spooled
        one the temporary file is deleted, and `chunks()` and `read()` raise
        `ValueError` from then on.
        """
        if self._spool is not None and not self._spool.closed:
            self._spool.close()

    @property
    def content_type(self) -> str:
        """The part's declared `Content-Type`, or `application/octet-stream`.

        Scanned out of the part's headers on every read; nothing is cached. The
        value is client-supplied and describes nothing about the bytes -- check
        the content itself before trusting it.
        """
        value = find_header(self.headers, b"content-type")
        return value.decode("latin-1") if value else "application/octet-stream"


class FormData:
    """Parsed form fields plus uploaded files, first value wins per name.

    The mapping protocol -- `form[name]`, `get()`, `in`, `len()`, iteration --
    covers `fields` only: the text parts, decoded as UTF-8 with undecodable
    bytes replaced rather than raising. Parts that carried a filename are in
    `files`, keyed by field name, and are invisible to the mapping and to
    `getlist()`. A name submitted twice keeps its first value in `fields`, and
    the rest stay reachable through `getlist()`.

    Built by `Request.form()` and cached on the request, so every reader of a
    request shares one parse and one set of spooled uploads. Nothing closes
    those for you; see `close()`.

    Args:
        all_values: every value per name, in order, defaulting to one per field
    """

    __slots__ = ("_all", "fields", "files")

    def close(self) -> None:
        """Release every spooled upload this form holds. Idempotent.

        The framework never calls this: a form is cached on the request and the
        request does not know when the last reader is finished with it. Left
        unclosed, a spool survives until the `UploadedFile` is collected, so a
        handler that is done with its uploads closes the form to give the
        temporary files back at a moment it chooses. Fields stay readable
        afterwards; the uploads do not.
        """
        for uploaded in self.files.values():
            uploaded.close()

    def __init__(
        self,
        fields: dict[str, str],
        files: dict[str, UploadedFile],
        all_values: dict[str, list[str]] | None = None,
    ) -> None:
        """Take the parsed parts as they came out of the codec.

        `all_values` is what `getlist` reads. Omitting it says the caller kept
        no duplicates, and every field is then treated as having been submitted
        exactly once -- which is true of a form built by hand, and not of one
        parsed from a body, so `Request.form()` always passes it.
        """
        self.fields = fields
        self.files = files
        self._all = (
            all_values
            if all_values is not None
            else {name: [value] for name, value in fields.items()}
        )

    def getlist(self, name: str) -> list[str]:
        """Every value submitted under `name`, in order. Empty when there is none.

        `fields` keeps the first, which is what most handlers want and what the
        binding layer reads. The rest were being dropped with no way to see them
        -- and a repeated field is exactly where an upstream proxy or WAF may
        have read a *different* value than the application will.

        Text fields only. A repeated file field keeps its first upload in
        `files` and the others are not retained.
        """
        return list(self._all.get(name, ()))

    def __getitem__(self, name: str) -> str:
        """The first value submitted under `name`. Raises `KeyError` when absent.

        Uploads are not reachable this way; read `files` for those.
        """
        return self.fields[name]

    def get(self, name: str, default: str | None = None) -> str | None:
        """The first value submitted under `name`, or `default` when absent.

        Text fields only, like the rest of the mapping protocol -- an uploaded
        file is not found here even when a part of that name was submitted.
        Read `files` for those.
        """
        return self.fields.get(name, default)

    def __contains__(self, name: str) -> bool:
        """Whether a text field of that name was submitted. False for an upload."""
        return name in self.fields

    def __iter__(self) -> Any:
        """Iterate the text field names, in submission order.

        Uploads are not included; iterate `files` for those.
        """
        return iter(self.fields)

    def __len__(self) -> int:
        """How many distinct text fields were submitted, uploads excluded."""
        return len(self.fields)


#: RFC 2046 §5.1.1: 1..70 characters from this set, not ending in a space.
_BOUNDARY_CHARS = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'()+_,-./:=? "


def _valid_boundary(value: bytes) -> bool:
    """Whether `value` is a boundary the RFC allows.

    Unchecked, a boundary was whatever followed `boundary=` -- any length, any
    byte. That is where a parser differential lives: the proxy or WAF in front
    reads the body one way and this reads it another, and the disagreement is
    the whole attack.
    """
    return (
        1 <= len(value) <= 70
        and not value.endswith(b" ")
        # Strip permitted bytes from both ends in one built-in scan. If every
        # byte is permitted nothing remains; the first invalid byte stops the
        # scan and therefore survives in the result.
        and not value.strip(_BOUNDARY_CHARS)
    )


def _multipart_boundary(content_type: bytes) -> bytes | None:
    for fragment in content_type.split(b";"):
        key, sep, value = fragment.strip(b" \t").partition(b"=")
        if sep and key.strip(b" \t").lower() == b"boundary":
            value = value.strip(b" \t")
            if value[:1] == b'"' == value[-1:]:
                value = value[1:-1]
            return value if _valid_boundary(value) else None
    return None


async def _stream_multipart(
    chunks: AsyncIterator[bytes], boundary: bytes, limits: RequestLimits
) -> FormData:
    """Parse multipart data incrementally, spooling file bytes as they arrive."""
    parser = _core.MultipartStreamParser(
        boundary,
        limits.max_parts,
        limits.max_part_header_bytes,
        limits.max_part_bytes,
        PayloadTooLarge,
    )
    retained = 0
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    all_values: dict[str, list[str]] = {}
    headers: list[tuple[bytes, bytes]] = []
    field_name = ""
    filename: str | None = None
    part_data = bytearray()
    part_spool: Any = None
    part_size = 0

    def begin_part(header_block: bytes) -> None:
        nonlocal headers, field_name, filename, part_data, part_spool, part_size
        headers, parsed_name, filename = _core.multipart_part_info(header_block)
        if not parsed_name:
            raise ValueError("multipart Content-Disposition needs a non-empty form-data name")
        field_name = parsed_name
        part_data = bytearray()
        part_spool = None
        part_size = 0

    def feed_part(data: bytes | bytearray) -> None:
        nonlocal part_spool, part_size
        if not data:
            return
        part_size += len(data)
        if part_spool is not None:
            part_spool.write(data)
            return
        if filename is not None and len(part_data) + len(data) > limits.spool_max_bytes:
            part_spool = TemporaryFile()
            part_spool.write(part_data)
            part_spool.write(data)
            part_data.clear()
            return
        if retained + len(part_data) + len(data) > limits.max_form_memory_bytes:
            raise PayloadTooLarge(
                f"form parts exceed {limits.max_form_memory_bytes} bytes in memory"
            )
        part_data.extend(data)

    def finish_part() -> None:
        nonlocal retained, part_spool
        if filename is not None:
            if part_spool is None:
                data = bytes(part_data)
                retained += len(data)
                uploaded = UploadedFile(field_name, filename, headers, data)
            else:
                part_spool.flush()
                uploaded = UploadedFile(
                    field_name,
                    filename,
                    headers,
                    data=None,
                    spool=part_spool,
                    size=part_size,
                )
                part_spool = None
            previous = files.setdefault(field_name, uploaded)
            if previous is not uploaded:
                uploaded.close()
            return
        data = bytes(part_data)
        retained += len(data)
        decoded = data.decode("utf-8", "replace")
        fields.setdefault(field_name, decoded)
        all_values.setdefault(field_name, []).append(decoded)

    try:
        async for chunk in chunks:
            for kind, payload in parser.feed(chunk):
                if kind == 0:
                    begin_part(payload)
                elif kind == 1:
                    feed_part(payload)
                else:
                    finish_part()
        parser.finish()
    except BaseException:
        if part_spool is not None:
            part_spool.close()
        for uploaded in files.values():
            uploaded.close()
        raise
    return FormData(fields, files, all_values)


Receive = Callable[[], Awaitable[dict[str, Any]]]
_MISSING = object()
_STREAMING = object()
_STREAM_CONSUMED = object()


class StreamConsumed(RuntimeError):
    """The one-shot request body stream has already been consumed."""


class Request:
    """A deliberately small request object backed directly by an ASGI scope.

    Construction parses nothing. `method`, `path`, `scheme`, `client`,
    `query_string` and `headers` hand back what the server already delivered.
    `cookies`, `body()`, `json()` and `form()` each parse on first access and
    cache the result on the request, so a middleware, a dependency and the
    handler that all read the body pay for one read between them. `state` and
    `path_params` allocate their container on first read and not before.

    Behind wreath's own server there is no ASGI scope dict until something reads
    `scope`: the properties come off a native request context, and reading one
    header does not materialise the scope.

    A request belongs to the task handling it. Nothing here takes a lock, so two
    tasks awaiting `body()` on the same request at the same time race for the
    receive channel.

    Args:
        scope: an ASGI HTTP scope dict, or wreath's native request context
        receive: the ASGI receive callable, awaited only by `body()`
        path_params: the router's decoded path parameters
        limits: what this request may buffer in memory before it is refused
    """

    __slots__ = (
        "_body",
        "_app",
        "_cookies",
        "_client_source",
        "_form",
        "_header_map",
        "_header_scanned",
        "_identity",
        "_json",
        "_limits",
        "_path_params",
        "_policy_mask",
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
        app: Any = None,
    ) -> None:
        """Wrap one request. Two backings, one interface.

        A dict is an ASGI scope from a conforming server and is used as-is;
        anything else is wreath's native request context, and `_scope` stays
        None until `scope` is read so the dict is never built for a request
        that does not ask for it. Every accessor below branches on which of the
        two it holds.
        """
        if isinstance(scope, dict):
            self._scope: dict[str, Any] | None = scope
            self._context: Any | None = None
        else:
            self._scope = None
            self._context = scope
        self._receive = receive
        self._client_source = "socket"
        self._app = app
        # A conforming ASGI server gets the ordinary dict below. Wreath's own
        # server supplies its request-owned native header index instead, so
        # policy lookups do not first materialize the ASGI list and a second
        # Python container containing the same objects.
        self._header_map: Any | None = None
        self._header_scanned = False
        self._path_params = path_params
        # Completed first-class policy stages. The reference executor updates
        # this bitset; the native server keeps the equivalent C-owned state and
        # never touches it. It is not middleware depth and has no ordering API.
        self._policy_mask = 0
        self._identity: Identity | None = None
        self._route_outcome: str | None = None
        self._state: State | None = None
        self._limits = limits

    @property
    def event(self) -> Any:
        """This request's canonical log line, for attaching your own fields.

        One structured record per request carries what the recorder already
        knows -- route, status, timings, trace and span ids -- and this is how
        application code adds to it:

        ```python
        request.event.set("tenant_id", tenant.id)
        request.event.set("cache", "miss", raw=True)
        ```

        Fields follow the same deny-by-default rule as log arguments: a scalar
        is written, a string is fingerprinted unless `raw=True`. `promote()`
        publishes this request's buffered TRACE/DEBUG records even though it
        succeeded, for an anomaly the framework cannot see.

        Outside a configured recorder this returns an inert stand-in, so the
        call is always safe and never needs a guard.
        """
        from .logging import current_scope

        return current_scope()

    @property
    def identity(self) -> Identity | None:
        """Who the pipeline authenticated this request as, or None.

        A plain read of what the pipeline stored. Reading it never runs the
        authentication backend, and it is None until the backend has run --
        which happens only for a route that requires authentication, so a public
        route sees None even when the client sent perfectly good credentials.
        Before the `identity` stage hook it is None on every request.
        """
        return self._identity

    @property
    def authenticated(self) -> bool:
        """Whether an `Identity` has been established for this request.

        Exactly `identity is not None`, with the same caveat: False means
        nothing has authenticated this request yet, not that the client is
        anonymous.
        """
        return self._identity is not None

    def _set_identity(self, identity: Identity | None) -> None:
        self._identity = identity

    def _bearer_token(self) -> str | None:
        """One valid built-in bearer token, without materializing native headers.

        The dict-scope half is the independent ASGI definition.  Wreath's
        request context performs the same duplicate refusal and syntax check
        over validated header spans, then materializes only the token string.
        """
        context = self._context
        if context is not None:
            return context._bearer_token()
        return _core.bearer_token(self.headers)

    def _bearer_verify(self, verifier: Any) -> Any:
        """Activate a built-in bearer verifier at the native header boundary.

        The portable half remains the independent ASGI definition.  Wreath's
        request context scans and calls in one C entry, returning an async
        verifier's coroutine untouched for dispatch to await.
        """
        context = self._context
        if context is not None:
            return context._bearer_verify(verifier)
        token = self._bearer_token()
        return None if token is None else verifier(token)

    @property
    def state(self) -> State:
        """Per-request scratch space, shared by middleware, hooks and handlers.

        A `State` namespace scoped to this request and thrown away with it; the
        application-wide one is `app.state`. Allocated on first read and cached,
        so a request whose hooks never touch it never builds one, and the
        routing outcome the pipeline recorded is copied in when it is built.
        """
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
            return state.get("route_outcome")
        return self._route_outcome

    @property
    def scope(self) -> dict[str, Any]:
        """The ASGI scope dict for this request.

        Behind a third-party server this is the dict the server passed in, so a
        write is visible to anything else holding it. Behind wreath's own server
        there is no dict until this is read, and the first read builds one and
        caches it on both the request and the native context -- which is why the
        properties above exist: each reads one field without paying for that.

        Reach for it for the parts of ASGI wreath does not surface, such as
        `root_path` or `extensions`. To override the peer or the scheme, use
        `ProxyPolicy`; writing to this dict after something has read
        a property does not update the native context behind it.
        """
        scope = self._scope
        if scope is None:
            context = self._context
            if context is None:
                raise RuntimeError("request has neither an ASGI scope nor a native context")
            scope = self._scope = context._asgi_scope()
        return scope

    @property
    def method(self) -> str:
        """The HTTP method, uppercase, as the server parsed it.

        A field read on either backing. Nothing is parsed or cached, and the
        value never changes for the life of the request.
        """
        context = self._context
        if context is not None:
            return context.method
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope["method"]

    @property
    def path(self) -> str:
        """The percent-decoded request path, without the query string.

        A field read on either backing -- nothing is parsed or cached. It is the
        whole path the server delivered, prefixes included; the undecoded form
        is in `scope["raw_path"]` when the server supplied one.
        """
        context = self._context
        if context is not None:
            return context.path
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope["path"]

    def url_path_for(self, name: str, **parameters: Any) -> str:
        """Build a path for a route named on this request's application."""
        app = self._app
        if app is None:
            raise RuntimeError("this Request is not attached to a Wreath application")
        root_path = self.scope.get("root_path", "").rstrip("/")
        return root_path + app.url_path_for(name, **parameters)

    def url_for(self, name: str, **parameters: Any) -> str:
        """Build an absolute URL for a named route.

        A host-specific route renders its declared host placeholders. Other
        routes use the request's Host header, falling back to the ASGI server
        address when the header is absent.
        """
        app = self._app
        if app is None:
            raise RuntimeError("this Request is not attached to a Wreath application")
        host = app._host_for(name, parameters)
        if host is None:
            host = self.header("host")
        if not host:
            server = self.scope.get("server")
            if server is None:
                raise RuntimeError("cannot build an absolute URL without a host")
            address, port = server
            default_port = 443 if self.scheme == "https" else 80
            host = str(address) if port in (None, default_port) else f"{address}:{port}"
        return f"{self.scheme}://{host}{self.url_path_for(name, **parameters)}"

    @property
    def path_params(self) -> dict[str, str]:
        """The path parameters the router captured, as raw strings.

        Values are undecoded from the router's point of view -- conversion to
        the types a handler declared is the binding layer's job, and a handler
        reading this dict directly gets strings. Empty for a route with no
        parameters, and for a request that never matched one; the dict is
        allocated on the first read that finds none and then cached, so
        mutating it is a way to pass a value along the rest of the request.
        """
        params = self._path_params
        if params is None:
            params = self._path_params = {}
        return params

    @path_params.setter
    def path_params(self, params: dict[str, str]) -> None:
        """Replace the captured parameters wholesale.

        The seam the application uses to attach a match to a request built
        before routing ran. Assigning to it in a handler or middleware is
        supported and immediately visible, but it overwrites what the router
        captured -- the binding layer has already read these by the time a
        handler runs, so a late write changes what later readers see and not
        the arguments the handler was called with.
        """
        self._path_params = params

    @property
    def client(self) -> tuple[str, int | None] | None:
        """The peer's `(host, port)`, or None when the server did not report one.

        The socket peer, which behind a load balancer or a reverse proxy is the
        proxy and not the caller. `X-Forwarded-For` is deliberately ignored here
        -- any client can send it. Add `ProxyPolicy` with the proxy
        networks you trust and it rewrites this from the forwarded header, in
        which case the port is None because no forwarding header carries it.
        Everything that buckets by caller, rate limiting included, reads this.
        """
        context = self._context
        if context is not None:
            return context.client
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope.get("client")

    @property
    def client_source(self) -> str:
        """Why `client` is trusted: `socket` or `forwarded`.

        `forwarded` is set only when `ProxyPolicy` accepted the immediate
        socket peer and successfully parsed its forwarding chain. Merely sending
        an `X-Forwarded-For` header never changes this value.
        """
        return self._client_source

    @property
    def scheme(self) -> str:
        """The request scheme, `"http"` or `"https"`, defaulting to `"http"`.

        The scheme of the connection this process accepted. Behind a
        TLS-terminating proxy that connection is plaintext, so this reads
        `"http"` for a request the browser made over HTTPS -- which silently
        disables HSTS and breaks CSRF's origin check. `ProxyPolicy`,
        configured with the proxy networks you trust, restores it from
        `X-Forwarded-Proto`; nothing else honours that header.
        """
        context = self._context
        if context is not None:
            return context.scheme
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope.get("scheme", "http")

    def _set_client(self, client: tuple[str, int | None], *, source: str = "socket") -> None:
        # ProxyPolicy rewrites the peer from X-Forwarded-For. The
        # write goes to the context when one backs this request so the ASGI
        # scope is never materialized just to carry it.
        context = self._context
        if context is not None:
            context._set_client(client)
        else:
            scope = self._scope
            if scope is None:
                raise RuntimeError("request scope is unavailable")
            scope["client"] = client
        self._client_source = source

    def _set_scheme(self, scheme: str) -> None:
        context = self._context
        if context is not None:
            context._set_scheme(scheme)
            return
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        scope["scheme"] = scheme

    @property
    def query_string(self) -> bytes:
        """The raw query string, undecoded, without the `?`. Empty when absent.

        Bytes, and never parsed here: a field read on either backing, with no
        cache because there is nothing to cache. Handlers take query parameters
        through `Query` markers rather than reading this; it is here for the
        cases that need the bytes exactly as they arrived.
        """
        context = self._context
        if context is not None:
            return context.query_string
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope.get("query_string", b"")

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        """The raw ASGI header list -- `(name, value)` pairs of bytes, in order.

        Names arrive lowercase, as ASGI requires of servers, and a header sent
        more than once appears more than once here. No copy is made: this is the
        list the request is backed by, so appending to it or rewriting an entry
        changes what every later reader sees -- except that it does not update
        the name index `header()` may already have built, which keeps returning
        the old value. Use `header()` to read one by name; it decodes the value
        and resolves duplicates.
        """
        context = self._context
        if context is not None:
            return context.headers
        scope = self._scope
        if scope is None:
            raise RuntimeError("request scope is unavailable")
        return scope.get("headers", [])

    @property
    def cookies(self) -> dict[str, str]:
        """The `Cookie` header parsed into a dict. Empty when there is none.

        Parsed on first read and cached, so the session, the CSRF check and a
        bearer-cookie backend share one parse. A cookie sent twice keeps its
        first value. Split `Cookie` header lines are combined with `; ` as HTTP/2
        requires. Values are the octets as they arrived, decoded latin-1 -- neither unquoted nor
        percent-decoded, because a cookie is bytes and only its writer knows how
        it was encoded.

        A `Cookie` header longer than `RequestLimits.max_cookie_bytes` is
        refused rather than parsed, and refused again on every subsequent read
        -- the failure is not cached, so a later reader sees the same 431 and
        not an empty dict.

        Raises:
            RequestHeaderFieldsTooLarge: the header exceeds `max_cookie_bytes`
        """
        # Parsed once and cached request-locally, like `_body` and
        # `_header_map`: repeated reads are common (session, CSRF, auth) and
        # each one otherwise rescanned the header list and rebuilt the dict.
        cached: Any = getattr(self, "_cookies", _MISSING)
        if cached is not _MISSING:
            return cached
        # HTTP/2 permits Cookie to be split across field lines. Combine every
        # line with the RFC cookie separator while enforcing the limit before
        # allocating the joined value; first-line-only creates proxy/app auth
        # ambiguity and silently drops CSRF/session cookies.
        context = self._context
        native_parse = None if context is None else getattr(context, "_parse_cookies", None)
        if native_parse is None:
            parsed: dict[str, str] = _core.parse_cookie_headers(
                self.headers,
                self._limits.max_cookie_bytes,
                RequestHeaderFieldsTooLarge,
            )
        else:
            parsed = native_parse(
                self._limits.max_cookie_bytes,
                RequestHeaderFieldsTooLarge,
            )
        self._cookies = parsed
        return parsed

    def _set_header(self, name: bytes, value: bytes) -> None:
        # Only ProxyPolicy needs this: it rewrites Host from a
        # trusted X-Forwarded-Host before anything downstream reads it. The
        # cache maintenance lives here because the caches do.
        context = self._context
        native_set = None if context is None else getattr(context, "_set_header", None)
        if native_set is not None:
            native_set(name, value)
            return
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
        header_map = self._header_map
        if header_map is not None:
            header_map[name] = value

    def _remove_headers(self, *names: bytes) -> None:
        """Remove every occurrence of internal representation headers."""
        context = self._context
        native_remove = None if context is None else getattr(context, "_remove_headers", None)
        if native_remove is not None:
            native_remove(*names)
        else:
            wanted = frozenset(names)
            headers = self.headers
            headers[:] = [pair for pair in headers if pair[0] not in wanted]
        header_map = self._header_map
        if header_map is not None and header_map is not context:
            for name in names:
                header_map.pop(name, None)

    def _single_header(self, name: bytes) -> bytes | None:
        """Return one raw value, refusing duplicate security-sensitive fields."""
        context = self._context
        native_single = None if context is None else getattr(context, "_single_header", None)
        if native_single is not None:
            return native_single(name)
        found = None
        for candidate, value in self.headers:
            if candidate != name:
                continue
            if found is not None:
                raise ValueError("request header occurs more than once")
            found = value
        return found

    def _index_headers(self) -> Any:
        """Return the first-value header index for multi-header consumers."""
        header_map = self._header_map
        if header_map is None:
            context = self._context
            native_index = None if context is None else getattr(context, "_header_index", None)
            header_map = build_header_map(self.headers) if native_index is None else native_index()
            self._header_map = header_map
            self._header_scanned = True
        return header_map

    def header(self, name: str | bytes, default: str | None = None) -> str | None:
        """One header by name, decoded, or `default` when it was not sent.

        `name` may be `str` or `bytes` and is matched case-insensitively -- it is
        lowercased here, and ASGI servers deliver names already lowercased. A
        header sent more than once yields its first value; read `headers` to see
        the rest. The value is decoded latin-1, which is what HTTP header octets
        are, so a UTF-8 header value arrives mojibaked unless you re-encode it.

        The first lookup on a request scans the header list; the second builds a
        name index, and every lookup after that is a dict hit.

        Args:
            name: header name, in any case
            default: returned when the header is absent
        """
        context = self._context
        if context is not None:
            try:
                native_header_text = context._header_text
            except AttributeError:
                pass
            else:
                return native_header_text(name, default)
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
            header_map = self._index_headers()
        value = header_map.get(target)
        if value is None:
            return default
        return value.decode("latin-1")

    def _header_bytes(self, name: bytes) -> bytes | None:
        """One raw header value without materializing the header collection."""
        context = self._context
        if context is not None:
            try:
                native_header = context._header
            except AttributeError:
                pass
            else:
                return native_header(name)
        return find_header(self.headers, name)

    @property
    def locale(self) -> str:
        """The caller's preferred language tag, or `"en"`.

        Read from `Accept-Language`, highest `q` first, with ties going to the
        tag the client listed earlier. This is what `wreath.temporal.relative()`
        takes, so "3 hours ago" can be translated later without finding every
        call site that renders one.

        Deliberately forgiving: the header is attacker-controlled and a
        malformed one must never fail a request, so anything unparseable falls
        back to the default rather than raising. Only the tag is returned -- no
        negotiation against a list of supported languages, because the caller
        knows which those are and this does not.

        Computed on every read from `header()`, which caches the header index
        but not this result.
        """
        header = self.header("accept-language")
        if not header:
            return "en"
        return _core.locale_preference(header)

    def preferred_content_digest(self, *supported: str) -> str | None:
        """Best supported Want-Content-Digest algorithm, or None for no usable hint."""
        return self._preferred_digest(b"want-content-digest", supported)

    def preferred_repr_digest(self, *supported: str) -> str | None:
        """Best supported Want-Repr-Digest algorithm, or None for no usable hint."""
        return self._preferred_digest(b"want-repr-digest", supported)

    def _preferred_digest(self, header: bytes, supported: tuple[str, ...]) -> str | None:
        raw = self._header_bytes(header)
        if raw is None:
            return None
        try:
            preferences = DigestPreferences.parse(raw)
        except DigestError:
            return None
        return preferences.preferred(*supported)

    async def verify_content_digest(self, *, required: bool = False) -> str | None:
        """Verify RFC 9530 Content-Digest against this request's message content.

        Returns the active algorithm used, None when the field is absent and
        optional, and raises 400 for a required, malformed, unsupported, or
        mismatching field. This reads and caches the body; call it after binding
        for ordinary buffered APIs rather than before a one-shot `stream()`.
        """
        raw = self._header_bytes(b"content-digest")
        if raw is None:
            if required:
                raise BadRequest("Content-Digest is required")
            return None
        try:
            digest = Digest.parse(raw)
            return digest.verify(await self.body())
        except DigestError as error:
            raise BadRequest(str(error)) from error

    async def verify_repr_digest(
        self,
        representation: bytes | None = None,
        *,
        required: bool = False,
    ) -> str | None:
        """Verify Repr-Digest against complete selected representation data.

        `representation` defaults to this message's content, which is correct
        when the request encloses the complete selected representation. Supply
        the reconstructed representation for partial or otherwise transformed
        messages.
        """
        raw = self._header_bytes(b"repr-digest")
        if raw is None:
            if required:
                raise BadRequest("Repr-Digest is required")
            return None
        content = await self.body() if representation is None else representation
        try:
            return Digest.parse(raw).verify(content)
        except DigestError as error:
            raise BadRequest(str(error)) from error

    async def body(self) -> bytes:
        """The whole request body as bytes, read once and cached.

        The first call drains `stream()` and caches the result; every call after it
        returns the same object without touching the channel, so middleware, a
        dependency and the handler can each ask for the body. This method
        materialises the body in full; use `stream()` to process chunks without
        retaining them. Both paths are bounded by `RequestLimits.max_body_bytes`,
        checked as chunks arrive rather than after joining them.

        A request that is refused or cut short caches an **empty** body, so a
        second call returns `b""` instead of raising again. The channel is
        already spent by then and re-reading it would hand back a truncated
        payload; the caller that saw the exception is the one that knows.

        Raises:
            PayloadTooLarge: the body exceeds `RequestLimits.max_body_bytes`
            ClientDisconnect: the peer went away before the body finished
            BadRequest: a covered `Content-Digest` does not match these bytes.
                See `_check_body` -- ingress cannot do this check, because the
                body has not arrived when global middleware runs.
        """
        cached: Any = getattr(self, "_body", _MISSING)
        if cached is _STREAMING or cached is _STREAM_CONSUMED:
            raise StreamConsumed("request body stream has already been consumed")
        if cached is not _MISSING:
            return cached

        first_chunk: bytes | None = None
        buffer: bytearray | None = None
        async for body in self.stream():
            if body:
                if first_chunk is None and buffer is None:
                    first_chunk = body
                else:
                    if buffer is None:
                        if first_chunk is None:
                            raise RuntimeError("request body collector lost its first chunk")
                        buffer = bytearray(first_chunk)
                        first_chunk = None
                    buffer.extend(body)

        if buffer is not None:
            result = bytes(buffer)
        elif first_chunk is not None:
            # The common one-chunk request reuses the ASGI bytes object directly.
            result = first_chunk
        else:
            result = b""
        self._body = result
        self._check_body(result)
        return result

    def _take_body_check(self) -> tuple[str, bytes] | None:
        """The deferred whole-body digest a middleware parked here, spent once.

        One `is None` on the common path, because `state` is not allocated at
        all for a request whose middleware never touched it. `wreath.signatures`
        is the only writer: it verifies an RFC 9421 signature at ingress, where
        the body has not arrived yet, so the `Content-Digest` that signature
        covered can only be checked from here. Taken rather than read, so a
        second `body()` off the cache does not re-hash what it already checked.
        """
        state = self._state
        if state is None:
            return None
        expected = state.get(BODY_CHECK_SLOT, None)
        if expected is None:
            return None
        state.__setattr__(BODY_CHECK_SLOT, None)
        return expected

    def _check_body(self, body: bytes) -> None:
        """Refuse a body that does not hash to what its signature covered."""
        expected = self._take_body_check()
        if expected is None:
            return
        algorithm, digest = expected
        if not compare_digest(new_hash(algorithm.replace("-", ""), body).digest(), digest):
            raise BadRequest("request body does not match its signed content-digest")

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield request-body chunks directly from the ASGI receive channel.

        Streaming enforces `max_body_bytes` as bytes arrive but retains no
        complete body. It is one-shot: after a streamed body completes (or a
        consumer stops early), `body()`, `json()`, `form()`, and a second stream
        raise `StreamConsumed`. If `body()` ran first, its cached bytes are
        replayed once without touching the receive channel.

        A `Content-Digest` a signature covered is hashed **incrementally** here
        and compared when the last chunk arrives, so a streaming handler gets
        the same guarantee as `body()` without materialising anything. A
        consumer that stops early leaves it unchecked, which it must: the bytes
        it did not read cannot be hashed, and there is no complete body to have
        an opinion about.

        Raises:
            BadRequest: a covered `Content-Digest` does not match these bytes.
        """
        cached: Any = getattr(self, "_body", _MISSING)
        if cached is _STREAMING or cached is _STREAM_CONSUMED:
            raise StreamConsumed("request body stream has already been consumed")
        if cached is not _MISSING:
            body = cached
            if body:
                yield body
            return
        self._body = _STREAMING
        total = 0
        limit = self._limits.max_body_bytes
        expected = self._take_body_check()
        body_check = (
            None if expected is None else (new_hash(expected[0].replace("-", "")), expected[1])
        )
        try:
            while True:
                message = await self._receive()
                if type(message) is tuple:
                    body, more_body, disconnected = message
                    if disconnected:
                        self._body = b""
                        raise ClientDisconnect(
                            "the client disconnected before the request body was received"
                        )
                    if body:
                        if len(body) > limit - total:
                            self._body = b""
                            raise PayloadTooLarge(f"request body exceeds {limit} bytes")
                        total += len(body)
                        if body_check is not None:
                            body_check[0].update(body)
                        yield body
                    if not more_body:
                        if body_check is not None:
                            if not compare_digest(body_check[0].digest(), body_check[1]):
                                raise BadRequest(
                                    "request body does not match its signed content-digest"
                                )
                        return
                    continue
                message_type = message["type"]
                if message_type == "http.disconnect":
                    self._body = b""
                    raise ClientDisconnect(
                        "the client disconnected before the request body was received"
                    )
                if message_type != "http.request":
                    continue
                body = message.get("body", b"")
                if body:
                    if len(body) > limit - total:
                        self._body = b""
                        raise PayloadTooLarge(f"request body exceeds {limit} bytes")
                    total += len(body)
                    if body_check is not None:
                        # Before the yield, not after: a consumer that stops
                        # early must not leave a chunk it *did* read unhashed.
                        body_check[0].update(body)
                    yield body
                if not message.get("more_body", False):
                    if body_check is not None:
                        if not compare_digest(body_check[0].digest(), body_check[1]):
                            raise BadRequest(
                                "request body does not match its signed content-digest"
                            )
                    return
        finally:
            if getattr(self, "_body", _MISSING) is _STREAMING:
                self._body = _STREAM_CONSUMED

    async def json(self) -> Any:
        """The body decoded as JSON, decoded once and cached.

        Reads `body()`, so the same limits and the same read-once rule apply,
        and the decoded value is cached separately -- two handlers asking for
        the JSON share one decode. The `Content-Type` is not consulted: a body
        that parses as JSON is returned whatever the client called it.

        A malformed body raises on every call, since nothing is cached until a
        decode succeeds. Note that this is a bare `ValueError`, which the
        pipeline reports as a 500. A handler that declares a body parameter
        instead of reading this gets the binding layer's 400 with the decoder's
        message.

        Raises:
            ValueError: the body is not valid JSON
            PayloadTooLarge: the body exceeds `RequestLimits.max_body_bytes`
            ClientDisconnect: the peer went away before the body finished
        """
        cached = getattr(self, "_json", _MISSING)
        if cached is not _MISSING:
            return cached
        result = _json_loads(await self.body())
        self._json = result
        return result

    async def form(self) -> FormData:
        """Parse a urlencoded or multipart request body. Parsed once and cached.

        A `multipart/form-data` content type is parsed as multipart, and
        anything else -- including a missing content type -- is parsed as
        urlencoded. The urlencoded codec refuses nothing: a JSON body handed to
        it comes back as one field whose name is the whole body, not an error.
        Uploaded parts, meaning the ones carrying a filename, land in
        `FormData.files`; every other part is a text field.

        Multipart input drains `stream()` incrementally, so the body limit and
        form limits are checked while data arrives: `max_parts`,
        `max_part_header_bytes`, `max_part_bytes`, `max_form_memory_bytes` across
        the parts kept in memory, and
        `max_form_fields` while scanning a urlencoded body. A part over
        `spool_max_bytes` is written to a temporary file and does not count
        against the in-memory total. **Every one of those limits refuses with a
        413**: a limit that exists to refuse hostile input has to report the
        caller's fault as the caller's, not as a 500.

        A body the parser cannot read -- a bad boundary, an unterminated part, a
        malformed header line -- is a different failure and still raises
        `ValueError`.

        The parsed form is cached on the request, so its spooled uploads live
        until `FormData.close()` is called or the request is collected.

        Raises:
            ValueError: the multipart body is malformed
            PayloadTooLarge: the body, the parts, or the field count exceed a limit
            ClientDisconnect: the peer went away before the body finished
        """
        cached: Any = getattr(self, "_form", _MISSING)
        if cached is not _MISSING:
            return cached
        # Via the `headers` property, not `self.scope`: on the native path the
        # ASGI scope is lazily built, and reading one header should not force it.
        content_type = find_header(self.headers, b"content-type")
        if content_type is not None and content_type.startswith(b"multipart/form-data"):
            boundary = _multipart_boundary(content_type)
            if boundary is None:
                raise ValueError("multipart body without a boundary parameter")
            result = await _stream_multipart(self.stream(), boundary, self._limits)
            self._form = result
            return result
        body = await self.body()
        limit = self._limits.max_form_fields
        fields, every = _core.parse_form_urlencoded(body, limit, PayloadTooLarge)
        result = FormData(fields, {}, every)
        self._form = result
        return result


# The framework's native activation seam uses this immutable, module-owned
# layout to materialize the same public Request without executing its Python
# initializer. Direct callers keep the ordinary Request(...) API above.
_REQUEST_LAYOUT = _core.request_layout(Request)
