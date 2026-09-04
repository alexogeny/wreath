"""First-class cross-origin HTTP policy.

Preflight `OPTIONS` requests short-circuit from the `_ingress` stage; simple
requests get their response headers appended by the `_egress` stage. All
allow-list computation happens once at construction, so the per-request work
is a header lookup and, for hits, a few list appends:

```python
app.configure_http_policy(HttpPolicy(
    cors=CorsPolicy(allow_origins=["https://app.example"]),
))
```
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from .._http import _is_http_token
from .._webpolicy import append_vary, find_response_header, normalize_origin
from ..request import Request
from ..response import Response

_DEFAULT_METHODS = ("GET", "HEAD", "POST", "OPTIONS")
_DUPLICATE = object()
_REFUSED = "_wreath_cors_refused"


def _normalize_origin(value: str) -> str:
    """Lower-case the scheme and host of an origin, leaving `*` alone.

    An origin's scheme and authority are case-insensitive; the rest of a URL is
    not, but an origin has no rest.
    """
    return value.lower()


def _normalize_allowed_origin(value: str, *, allow_credentials: bool) -> str:
    if not isinstance(value, str):
        raise TypeError("allow_origins entries must be str")
    if value == "*":
        return value
    if value.lower() == "null":
        if allow_credentials:
            raise ValueError("invalid CORS origin with credentials: 'null'")
        return "null"
    if "\\" in value:
        raise ValueError(
            f"invalid CORS origin {value!r}: use an ASCII browser origin"
        )
    try:
        return normalize_origin(value, label="CORS").decode("ascii")
    except (UnicodeError, ValueError) as error:
        raise ValueError(
            f"invalid CORS origin {value!r}: use an ASCII browser origin"
        ) from error


def _http_tokens(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    compiled = tuple(values)
    if any(not isinstance(value, str) or not _is_http_token(value) for value in compiled):
        raise ValueError(f"{label} entries must each be one HTTP token")
    return compiled


def _request_header(request: Request, name: str) -> str | object | None:
    single = getattr(request, "_single_header", None)
    if single is None:
        return request.header(name)
    try:
        value = single(name.encode("ascii"))
    except ValueError:
        return _DUPLICATE
    return None if value is None else value.decode("latin-1")


def _mark_refused(request: Request) -> None:
    state = getattr(request, "state", None)
    if state is not None:
        state.__setattr__(_REFUSED, True)


class CorsPolicy:
    """Answer CORS preflights and add CORS headers to cross-origin responses.

    Global policy, so it covers routed responses, route misses, static
    files, and error responses alike. Every allow-list is computed once in the
    constructor; per request the work is a header lookup and, on a hit, a few
    list appends.

    A preflight -- `OPTIONS` carrying both `Origin` and
    `Access-Control-Request-Method` -- is answered from `_ingress` and never
    reaches a route. An `OPTIONS` request missing either header is not a
    preflight and falls through to the route as normal. Because preflights
    target paths whose routes rarely declare an `OPTIONS` method, this class
    also exposes `_preflight`, which the application consults for
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

    __slots__ = (
        "_allow_all_origins",
        "_allow_headers",
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
        if type(allow_credentials) is not bool:
            raise TypeError("allow_credentials must be bool")
        if type(max_age) is not int or max_age < 0:
            raise TypeError("max_age must be a non-negative int")
        methods = _http_tokens(allow_methods, label="allow_methods")
        allowed_headers = _http_tokens(allow_headers, label="allow_headers")
        exposed_headers = _http_tokens(expose_headers, label="expose_headers")
        origins = tuple(
            _normalize_allowed_origin(item, allow_credentials=allow_credentials)
            for item in allow_origins
        )
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
        self._allow_headers = frozenset(header.lower() for header in allowed_headers)

        preflight: list[tuple[bytes, bytes]] = [
            (b"access-control-allow-methods", ", ".join(methods).encode("ascii")),
            (b"access-control-max-age", str(max_age).encode("ascii")),
        ]
        headers = ", ".join(allowed_headers)
        if headers:
            preflight.append((b"access-control-allow-headers", headers.encode("ascii")))
        simple: list[tuple[bytes, bytes]] = []
        exposed = ", ".join(exposed_headers)
        if exposed:
            simple.append((b"access-control-expose-headers", exposed.encode("ascii")))
        if allow_credentials:
            credentials = (b"access-control-allow-credentials", b"true")
            preflight.append(credentials)
            simple.append(credentials)
        self._preflight_headers = tuple(preflight)
        self._simple_headers = tuple(simple)
        self._allow_methods = frozenset(method.upper() for method in methods)

    def describe(self):
        """The cross-origin headers this policy negotiates.

        Values depend on the request's `Origin`, so none is a `const`.
        """
        from .base import HeaderSpec, PolicyContract

        return PolicyContract(
            request_headers=(
                HeaderSpec("Origin", description="The requesting origin, when cross-site."),
            ),
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Access-Control-Allow-Origin",
                        description="Echoed origin, or `*`, when the origin is allowed.",
                    ),
                ),
                (
                    None,
                    HeaderSpec(
                        "Vary",
                        description="Includes `Origin` whenever the answer depends on it.",
                    ),
                ),
            ),
        )

    def _origin_header(self, origin: str) -> tuple[bytes, bytes] | None:
        if self._allow_all_origins:
            return (b"access-control-allow-origin", b"*")
        # Exact match first: an origin arrives already lower-cased from every
        # browser, so normalizing before comparing put string work on every
        # cross-origin request to serve the rare header that is not. The
        # normalized compare is the fallback, which is what makes
        # `HTTPS://App.Example` the same origin as `https://app.example`
        # (RFC 9110 §4.2.3 -- scheme and authority are case-insensitive).
        if origin in self._allow_origins or _normalize_origin(origin) in self._allow_origins:
            # Echoed as the client sent it, which is what it will compare against.
            return (b"access-control-allow-origin", origin.encode("latin-1"))
        return None

    def _ingress_sync(self, request: Request) -> Any | None:
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
        origin = _request_header(request, "origin")
        requested = _request_header(request, "access-control-request-method")
        if origin is _DUPLICATE or requested is _DUPLICATE:
            _mark_refused(request)
            return Response(
                b"duplicate CORS header", status=400, media_type=b"text/plain"
            )
        if origin is None or requested is None:
            return None  # not a CORS preflight; fall through to the route
        origin = cast(str, origin)
        requested = cast(str, requested)
        requested_headers = _request_header(request, "access-control-request-headers")
        if requested_headers is _DUPLICATE:
            _mark_refused(request)
            return Response(
                b"duplicate CORS header", status=400, media_type=b"text/plain"
            )
        if requested_headers is not None:
            requested_headers = cast(str, requested_headers)
            names = tuple(part.strip() for part in requested_headers.split(","))
            if (
                any(not _is_http_token(name) for name in names)
                or (
                    "*" not in self._allow_headers
                    and any(name.lower() not in self._allow_headers for name in names)
                )
            ):
                _mark_refused(request)
                refusal = Response(
                    b"disallowed header", status=403, media_type=b"text/plain"
                )
                refusal.headers.append((b"vary", b"origin"))
                return refusal
        if requested.upper() not in self._allow_methods:
            # The preflight *asks* whether a method is allowed. Echoing the
            # configured list regardless answered a question the client did not
            # put, and told it nothing about the one it did.
            _mark_refused(request)
            refusal = Response(b"disallowed method", status=403, media_type=b"text/plain")
            refusal.headers.append((b"vary", b"origin"))
            return refusal
        allowed = self._origin_header(origin)
        if allowed is None:
            _mark_refused(request)
            refusal = Response(b"disallowed origin", status=403, media_type=b"text/plain")
            # The refusal is origin-dependent too, so a shared cache must not
            # replay it to an origin that would have been allowed.
            refusal.headers.append((b"vary", b"origin"))
            return refusal
        response = Response(b"", status=204, media_type=None)
        response.headers.append(allowed)
        response.headers.extend(self._preflight_headers)
        if not self._allow_all_origins:
            response.headers.append((b"vary", b"origin"))
        return response

    async def _ingress(self, request: Request) -> Any | None:
        return self._ingress_sync(request)

    # Preflight requests target routes that usually declare no OPTIONS
    # method, so route-fused stages never see them.  Wreath consults this
    # app-level fallback for unmatched OPTIONS requests.
    _preflight = _ingress

    def _egress_inplace(self, request: Request, response: Any) -> None:
        """Add the simple-request CORS headers to a cross-origin response.

        A request with no `Origin` header is left exactly as it is. An origin
        that is not allowed gets `Vary: Origin` and nothing else, so the browser
        withholds the response from the caller and no cache confuses the two
        answers.

        For an allowed origin this appends `Access-Control-Allow-Origin`, the
        configured expose-headers and credentials headers, and `Vary: Origin`.
        A response that already carries an `Access-Control-Allow-Origin` -- the
        preflight answered by `_ingress`, or a handler that set its own -- is left
        alone rather than having a second one appended.

        `origin` is *merged* into whatever `Vary` the response already carries
        rather than appended only when there is none. Compression adds
        `accept-encoding` and content negotiation adds `accept`, so "no Vary at
        all" is the uncommon case; keying on it meant the responses most likely
        to be cached were exactly the ones that never gained `origin`.
        """
        state = getattr(request, "state", None)
        if state is not None and state.get(_REFUSED):
            return
        origin = _request_header(request, "origin")
        if origin is _DUPLICATE:
            headers = getattr(response, "headers", None)
            if headers is not None:
                append_vary(headers, b"origin")
            return
        if origin is None:
            return
        origin = cast(str, origin)
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
            return
        if (
            headers is not None
            # Respect a response that already carries CORS headers — the
            # preflight short-circuit from `_ingress`, or a handler's own.
            # `find_response_header` is the same scan in C, and it matches
            # case-insensitively, so a handler's `Access-Control-Allow-Origin`
            # is now honored rather than duplicated.
            and find_response_header(headers, b"access-control-allow-origin") is None
        ):
            headers.append(allowed)
            headers.extend(self._simple_headers)
            if not self._allow_all_origins:
                append_vary(headers, b"origin")

    def _egress_sync(self, request: Request, response: Any) -> Any:
        self._egress_inplace(request, response)
        return response

    async def _egress(self, request: Request, response: Any) -> Any:
        return self._egress_sync(request, response)


__all__ = ["CorsPolicy"]
