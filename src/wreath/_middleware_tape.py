"""Compile boot-frozen middleware policy into something C can execute.

A global middleware decides its policy once, when the application boots: an
allow-list of origins, a bucket rate, a CSP string. What it pays per request is
not deciding that policy -- it is walking a Python object graph to re-apply a
decision that was settled at startup. That is the same defect the response
validator had, one layer out.

This module extracts the settled decision. `Wreath(middleware="native")` opts in;
the compiler reads each middleware's frozen configuration and returns a
descriptor the native server can act on without materializing a `Request`.

Only what is *proven* is compiled. Anything this module does not recognise stays
in the Python tape, which keeps running exactly as it did -- so opting in never
silently drops a middleware, it only moves the ones named here.

## CORS preflight

The first and simplest: an `OPTIONS` carrying both `Origin` and
`Access-Control-Request-Method` is answerable entirely from configuration. The
response varies only by which origin gets echoed, so every possible answer is
built here, once, and the server picks one by dictionary lookup.

**This changes ordering, which is why it is opt-in.** Answered in C, a preflight
never reaches the Python tape, so middleware registered *before* CORS no longer
sees it: a rate limiter does not count preflights against the bucket, and
`ProxyHeadersMiddleware` does not rewrite their client address. Neither affects
the response a browser gets -- `CORSMiddleware.before_sync` short-circuits ahead
of the route either way -- but both are visible in metrics, and an application
that rate-limits preflights deliberately should not set the flag.
"""

from __future__ import annotations

from typing import Any

#: Execution strategy for the global middleware tape. `python` runs every hook
#: in Python, as it always has. `native` additionally lets the server answer
#: what this module could compile, without entering Python at all.
MiddlewareMode = str

_ALLOWED_MODES = ("python", "native")


class PreflightProgram:
    """Every answer a configured `CORSMiddleware` can give a preflight.

    Built once at route-compile time. `methods` is the set a preflight may ask
    for, and `by_origin` maps an allowed origin to the *pair* of answers it can
    get: the 204, and the 403 for asking about a method outside `methods`.

    The refusal is per-origin because CORS's egress hook runs on it -- a
    refusing `before` is a completed hook, so it keeps its own `after`, which
    adds `access-control-allow-origin` for an origin that is allowed. Recording
    one shared refusal produced a 403 missing that header for exactly the
    origins that should have had it.

    `wildcard` is the single pair used when every origin is allowed, where the
    echoed value is `*` and so does not vary. Each answer is
    `(status, headers, body)` in the shape `_wreath_response` already takes.
    """

    __slots__ = ("methods", "by_origin", "wildcard")

    def __init__(
        self,
        methods: frozenset[str],
        by_origin: dict[bytes, tuple[Any, Any]],
        wildcard: tuple[Any, Any] | None,
    ) -> None:
        self.methods = methods
        self.by_origin = by_origin
        self.wildcard = wildcard


async def _no_body() -> dict[str, Any]:
    """A receive callable for the boot-time probe; a preflight has no body."""
    return {"type": "http.request", "body": b"", "more_body": False}


def _ask(middleware: Any, origin: str, method: str) -> Any:
    """What this middleware answers one synthetic preflight, at boot.

    The table is recorded from the middleware rather than reconstructed beside
    it. Reconstruction is how the two drift: a first version of this module
    rebuilt the 204 from `_preflight_headers` and produced a response missing
    `content-type: application/octet-stream`, which `Response(b"", status=204,
    media_type=None)` emits from the class default. Nothing in the CORS
    configuration mentions that header, so no amount of reading the config
    would have found it.

    Asking the middleware cannot drift, and it compiles a subclass with its own
    logic correctly for free, so long as its answer depends only on the origin
    and the requested method. One that consults anything else -- a clock, a
    counter, the path -- must not be compiled, and is not: the probes below run
    twice and a middleware whose two answers differ is declined.
    """
    from .request import Request

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [
            (b"host", b"compile.invalid"),
            (b"origin", origin.encode("latin-1")),
            (b"access-control-request-method", method.encode("latin-1")),
        ],
        "server": ("127.0.0.1", 443),
        "client": ("127.0.0.1", 0),
        "root_path": "",
        "extensions": {},
    }
    request = Request(scope, _no_body)
    response = middleware.before_sync(request)
    if response is None:
        return None
    # Then its own egress, because the tape runs it. A `before` that answers
    # instead of the handler is a *completed* hook, so its `after` is inside the
    # unwound prefix -- which is how the 403 refusals pick up
    # `access-control-allow-origin`. Recording only what `before_sync` returned
    # produced a refusal missing that header, and the differential test caught
    # it at byte 64.
    egress = (
        getattr(middleware, "after_inplace", None)
        or getattr(middleware, "after_sync", None)
    )
    if egress is not None:
        returned = egress(request, response)
        if returned is not None:
            response = returned
    return response


def _parts(response: Any) -> tuple[int, list[tuple[bytes, bytes]], bytes] | None:
    """`(status, headers, body)` in the shape `_wreath_response` takes."""
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    body = getattr(response, "body", None)
    status = getattr(response, "status", None)
    if headers is None or body is None or status is None:
        return None
    if any(
        not isinstance(name, bytes) or not isinstance(value, bytes)
        for name, value in headers
    ):
        return None  # a str header would break the native writer -- decline
    return (int(status), [(bytes(n), bytes(v)) for n, v in headers], bytes(body))


def compile_preflight(middleware: Any) -> PreflightProgram | None:
    """The preflight answers for one `CORSMiddleware`, or None if unrecognised.

    Reads the middleware's frozen attributes to know *which* questions to ask,
    then records the answers by asking. A type that does not carry those
    attributes is not a CORS middleware this understands.
    """
    try:
        allow_all = bool(middleware._allow_all_origins)
        origins = middleware._allow_origins
        methods = middleware._allow_methods
    except AttributeError:
        return None
    if not isinstance(methods, frozenset) or not isinstance(origins, frozenset):
        return None
    allowed = frozenset(str(m).upper() for m in methods)
    if not allowed:
        return None
    good = next(iter(sorted(allowed)))
    #: A method no configuration would list, to record the refusal.
    bad = "\x01NOT-A-METHOD"

    def pair(origin: str) -> tuple[Any, Any] | None:
        """The 204 and the 403 this origin gets, or None if either is unusable."""
        accept = _parts(_ask(middleware, origin, good))
        refuse = _parts(_ask(middleware, origin, bad))
        if accept is None or refuse is None:
            return None
        # Asked twice. A recorded answer is only usable if the middleware gives
        # the same one every time -- a subclass consulting a clock, a counter or
        # anything but the origin and the requested method would be frozen at
        # whatever it happened to say here, and nothing else could detect it.
        if _parts(_ask(middleware, origin, good)) != accept:
            return None
        if _parts(_ask(middleware, origin, bad)) != refuse:
            return None
        return (accept, refuse)

    try:
        if allow_all:
            wildcard = pair("https://any.invalid")
            if wildcard is None:
                return None
            return PreflightProgram(allowed, {}, wildcard)
        by_origin: dict[bytes, tuple[Any, Any]] = {}
        for origin in origins:
            if not isinstance(origin, str):
                return None
            recorded = pair(origin)
            if recorded is None:
                return None
            by_origin[origin.encode("latin-1")] = recorded
    except (AttributeError, TypeError, ValueError, KeyError, LookupError):
        # Boot-time probing runs application code. A middleware that cannot
        # answer a synthetic request is one this module does not understand, so
        # it declines and the Python tape keeps it. Refusing to start would
        # punish an application for a middleware that works fine in Python.
        # Narrow rather than blanket: a middleware raising something exotic is
        # a surprise worth surfacing at boot, not swallowing.
        return None
    return PreflightProgram(allowed, by_origin, None)


def preflight_answer(
    program: PreflightProgram, headers: list[tuple[bytes, bytes]]
) -> tuple[int, list[tuple[bytes, bytes]], bytes] | None:
    """The recorded answer for this request, or None to let Python handle it.

    None means "not a preflight, or not one this table covers" -- an `OPTIONS`
    without both headers is an ordinary request that may have a route, and an
    origin outside the recorded set still has to reach `CORSMiddleware`, whose
    normalized compare accepts spellings this table does not carry.

    One pass over the header list rather than two lookups: the list is short and
    already in cache, and the alternative is building an index no other part of
    this request needs.
    """
    origin: bytes | None = None
    requested: bytes | None = None
    for name, value in headers:
        if name == b"origin":
            origin = value
        elif name == b"access-control-request-method":
            requested = value
    if origin is None or requested is None:
        return None
    entry = program.wildcard if program.wildcard is not None else program.by_origin.get(origin)
    if entry is None:
        # An origin the table does not carry is *not* refused here:
        # `CORSMiddleware` would still normalize it and might allow it. Falling
        # through costs a Python request and keeps the answer correct.
        return None
    accept, refuse = entry
    if requested.decode("latin-1", "replace").upper() not in program.methods:
        return refuse
    return accept


def compile_tape(mode: str, global_middleware: tuple[Any, ...]) -> PreflightProgram | None:
    """What the native server may answer for this application, or None.

    `mode` is checked here rather than by the caller so there is one place that
    knows an unrecognised mode is an error: a typo in `middleware="natve"` must
    not silently mean "python".
    """
    if mode not in _ALLOWED_MODES:
        raise ValueError(
            f"middleware={mode!r} is not a valid mode; choose one of {_ALLOWED_MODES}"
        )
    if mode == "python":
        return None
    for item in global_middleware:
        if type(item).__name__ != "CORSMiddleware":
            continue
        return compile_preflight(item)
    return None
