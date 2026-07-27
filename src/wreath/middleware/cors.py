"""CORS middleware compiled into Wreath's hook-based middleware tape.

Preflight `OPTIONS` requests short-circuit from the `before` hook; simple
requests get their response headers appended by the `after` hook. All
allow-list computation happens once at construction, so the per-request work
is a header lookup and, for hits, a few list appends::

    app.add_middleware(CORSMiddleware(allow_origins=["https://app.example"]))
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .._webpolicy import append_vary, find_response_header
from ..request import Request
from ..response import Response

_DEFAULT_METHODS = ("GET", "HEAD", "POST", "OPTIONS")


def _normalize_origin(value: str) -> str:
    """Lower-case the scheme and host of an origin, leaving `*` alone.

    An origin's scheme and authority are case-insensitive; the rest of a URL is
    not, but an origin has no rest.
    """
    if value == "*":
        return value
    scheme, separator, authority = value.partition("://")
    if not separator:
        return value.lower()
    return f"{scheme.lower()}://{authority.lower()}"


class CORSMiddleware:
    """Answer CORS preflights and add CORS headers to cross-origin responses.

    Global middleware, so it covers routed responses, route misses, static
    files, and error responses alike. Every allow-list is computed once in the
    constructor; per request the work is a header lookup and, on a hit, a few
    list appends.

    A preflight -- `OPTIONS` carrying both `Origin` and
    `Access-Control-Request-Method` -- is answered from `before` and never
    reaches a route. An `OPTIONS` request missing either header is not a
    preflight and falls through to the route as normal. Because preflights
    target paths whose routes rarely declare an `OPTIONS` method, this class
    also exposes `handle_preflight`, which the application consults for
    unmatched `OPTIONS` requests. Only one global CORS preflight handler may be
    registered per application.

    A simple cross-origin request from an origin that is *not* allowed is passed
    through untouched apart from `Vary: Origin`. Wreath does not refuse it; the
    absent `Access-Control-Allow-Origin` is what makes the browser withhold the
    response from the caller's JavaScript. Only preflights are refused outright.

    `Vary: Origin` is added whenever the answer depends on the request's origin,
    including when the origin is rejected, so a shared cache cannot replay one
    origin's answer to another. It is omitted only when every origin is allowed
    without credentials, where the answer is the same for all of them.

    Args:
        allow_origins: Permitted origins, or `*` for any. Case-insensitive on scheme and host.
        allow_methods: Methods a preflight may ask for. Defaults to GET, HEAD, POST, OPTIONS.
        allow_headers: Request headers a preflight may ask for. Empty sends no header.
        allow_credentials: Permit credentialed requests. Refused together with `*`.
        expose_headers: Response headers the caller's JavaScript may read.
        max_age: Seconds a browser may cache a preflight result.

    Raises:
        ValueError: `allow_origins` contains `*` and `allow_credentials` is True.
    """

    global_scope = True
    __slots__ = (
        "_allow_all_origins",
        "_allow_methods",
        "_allow_credentials",
        "_allow_origins",
        "_preflight_headers",
        "_simple_headers",
    )

    def __init__(
        self,
        *,
        allow_origins: Iterable[str] = (),
        allow_methods: Iterable[str] = _DEFAULT_METHODS,
        allow_headers: Iterable[str] = (),
        allow_credentials: bool = False,
        expose_headers: Iterable[str] = (),
        max_age: int = 600,
    ) -> None:
        origins = tuple(_normalize_origin(item) for item in allow_origins)
        if "*" in origins and allow_credentials:
            # Reflecting an arbitrary origin *with* credentials lets any site
            # read authenticated responses from this one. Browsers refuse the
            # literal `*` with credentials, so the only way to honour this
            # configuration is to echo whichever origin asked -- which is the
            # vulnerability, not a workaround for it. Named origins with
            # credentials are fine and stay supported.
            raise ValueError(
                "allow_origins=['*'] cannot be combined with allow_credentials=True: "
                "it reflects every origin with credentials. Name the origins that "
                "may send credentials."
            )
        self._allow_all_origins = "*" in origins
        self._allow_origins = frozenset(origins)
        self._allow_credentials = allow_credentials

        preflight: list[tuple[bytes, bytes]] = [
            (b"access-control-allow-methods", ", ".join(allow_methods).encode("latin-1")),
            (b"access-control-max-age", str(max_age).encode("ascii")),
        ]
        headers = ", ".join(allow_headers)
        if headers:
            preflight.append((b"access-control-allow-headers", headers.encode("latin-1")))
        simple: list[tuple[bytes, bytes]] = []
        exposed = ", ".join(expose_headers)
        if exposed:
            simple.append((b"access-control-expose-headers", exposed.encode("latin-1")))
        if allow_credentials:
            credentials = (b"access-control-allow-credentials", b"true")
            preflight.append(credentials)
            simple.append(credentials)
        self._preflight_headers = tuple(preflight)
        self._simple_headers = tuple(simple)
        self._allow_methods = frozenset(method.upper() for method in allow_methods)

    def _origin_header(self, origin: str) -> tuple[bytes, bytes] | None:
        if self._allow_all_origins and not self._allow_credentials:
            return (b"access-control-allow-origin", b"*")
        # Exact match first: an origin arrives already lower-cased from every
        # browser, so normalizing before comparing put string work on every
        # cross-origin request to serve the rare header that is not. The
        # normalized compare is the fallback, which is what makes
        # `HTTPS://App.Example` the same origin as `https://app.example`
        # (RFC 9110 §4.2.3 -- scheme and authority are case-insensitive).
        if (
            self._allow_all_origins
            or origin in self._allow_origins
            or _normalize_origin(origin) in self._allow_origins
        ):
            # Echoed as the client sent it, which is what it will compare against.
            return (b"access-control-allow-origin", origin.encode("latin-1"))
        return None

    async def before(self, request: Request) -> Any | None:
        """Answer a CORS preflight, or return None to let the request proceed.

        Returns None for anything that is not a preflight: a non-`OPTIONS`
        method, or an `OPTIONS` request without both `Origin` and
        `Access-Control-Request-Method`.

        A preflight asking for a method outside `allow_methods` is answered
        403 `disallowed method`; one from an origin outside `allow_origins` is
        answered 403 `disallowed origin`. Both carry `Vary: Origin`. These two
        are plain text rather than the RFC 9457 problem document the rest of the
        framework returns, because the caller is a browser's preflight machinery,
        which reads the status and discards the body.

        An accepted preflight is answered 204 with `Access-Control-Allow-Origin`,
        `Access-Control-Allow-Methods`, `Access-Control-Max-Age`, and the
        configured allow-headers and credentials headers.
        """
        # `request.method`, not `request.scope["method"]`: on the native server
        # the scope is a lazily materialized dict over `_RequestContext`, so
        # reading it here built the whole thing on every request purely to
        # compare one string. `method` is a direct attribute on the context.
        if request.method != "OPTIONS":
            return None
        origin = request.header("origin")
        requested = request.header("access-control-request-method")
        if origin is None or requested is None:
            return None  # not a CORS preflight; fall through to the route
        if requested.upper() not in self._allow_methods:
            # The preflight *asks* whether a method is allowed. Echoing the
            # configured list regardless answered a question the client did not
            # put, and told it nothing about the one it did.
            refusal = Response(b"disallowed method", status=403, media_type=b"text/plain")
            refusal.headers.append((b"vary", b"origin"))
            return refusal
        allowed = self._origin_header(origin)
        if allowed is None:
            refusal = Response(b"disallowed origin", status=403, media_type=b"text/plain")
            # The refusal is origin-dependent too, so a shared cache must not
            # replay it to an origin that would have been allowed.
            refusal.headers.append((b"vary", b"origin"))
            return refusal
        response = Response(b"", status=204, media_type=None)
        response.headers.append(allowed)
        response.headers.extend(self._preflight_headers)
        if not self._allow_all_origins or self._allow_credentials:
            response.headers.append((b"vary", b"origin"))
        return response

    # Preflight requests target routes that usually declare no OPTIONS
    # method, so route-fused hooks never see them.  Wreath consults this
    # app-level fallback for unmatched OPTIONS requests.
    handle_preflight = before

    async def after(self, request: Request, response: Any) -> Any:
        """Add the simple-request CORS headers to a cross-origin response.

        A request with no `Origin` header is left exactly as it is. An origin
        that is not allowed gets `Vary: Origin` and nothing else, so the browser
        withholds the response from the caller and no cache confuses the two
        answers.

        For an allowed origin this appends `Access-Control-Allow-Origin`, the
        configured expose-headers and credentials headers, and `Vary: Origin`.
        A response that already carries an `Access-Control-Allow-Origin` -- the
        preflight answered by `before`, or a handler that set its own -- is left
        alone rather than having a second one appended.

        `origin` is *merged* into whatever `Vary` the response already carries
        rather than appended only when there is none. Compression adds
        `accept-encoding` and content negotiation adds `accept`, so "no Vary at
        all" is the uncommon case; keying on it meant the responses most likely
        to be cached were exactly the ones that never gained `origin`.
        """
        origin = request.header("origin")
        if origin is None:
            return response
        allowed = self._origin_header(origin)
        headers = getattr(response, "headers", None)
        if allowed is None:
            # No ACAO for this origin -- but the *absence* is still a function of
            # the Origin header, so the response has to say so or a shared cache
            # will hand this bodyless-to-JavaScript answer to an allowed origin
            # (and the reverse). `append_vary` merges into an existing header
            # (in C where the extension is built) instead of adding a second
            # one, and is a no-op when `origin` is already listed.
            if headers is not None:
                append_vary(headers, b"origin")
            return response
        if (
            headers is not None
            # Respect a response that already carries CORS headers — the
            # preflight short-circuit from `before`, or a handler's own.
            # `find_response_header` is the same scan in C, and it matches
            # case-insensitively, so a handler's `Access-Control-Allow-Origin`
            # is now honored rather than duplicated.
            and find_response_header(headers, b"access-control-allow-origin") is None
        ):
            headers.append(allowed)
            headers.extend(self._simple_headers)
            if not self._allow_all_origins or self._allow_credentials:
                append_vary(headers, b"origin")
        return response


__all__ = ["CORSMiddleware"]
