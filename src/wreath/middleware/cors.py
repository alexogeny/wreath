"""CORS middleware compiled into Wreath's hook-based middleware tape.

Preflight ``OPTIONS`` requests short-circuit from the ``before`` hook; simple
requests get their response headers appended by the ``after`` hook. All
allow-list computation happens once at construction, so the per-request work
is a header lookup and, for hits, a few list appends::

    app.add_middleware(CORSMiddleware(allow_origins=["https://app.example"]))
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .._webpolicy import find_response_header
from ..request import Request
from ..response import Response

_DEFAULT_METHODS = ("GET", "HEAD", "POST", "OPTIONS")


class CORSMiddleware:
    """Global CORS hooks covering preflight, routes, and early responses."""

    global_scope = True
    __slots__ = (
        "_allow_all_origins",
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
        origins = tuple(allow_origins)
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

    def _origin_header(self, origin: str) -> tuple[bytes, bytes] | None:
        if self._allow_all_origins and not self._allow_credentials:
            return (b"access-control-allow-origin", b"*")
        if self._allow_all_origins or origin in self._allow_origins:
            return (b"access-control-allow-origin", origin.encode("latin-1"))
        return None

    async def before(self, request: Request) -> Any | None:
        # `request.method`, not `request.scope["method"]`: on the native server
        # the scope is a lazily materialized dict over `_RequestContext`, so
        # reading it here built the whole thing on every request purely to
        # compare one string. `method` is a direct attribute on the context.
        if request.method != "OPTIONS":
            return None
        origin = request.header("origin")
        if origin is None or request.header("access-control-request-method") is None:
            return None  # not a CORS preflight; fall through to the route
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
        origin = request.header("origin")
        if origin is None:
            return response
        allowed = self._origin_header(origin)
        headers = getattr(response, "headers", None)
        if allowed is None:
            # No ACAO for this origin -- but the *absence* is still a function of
            # the Origin header, so the response has to say so or a shared cache
            # will hand this bodyless-to-JavaScript answer to an allowed origin
            # (and the reverse).
            if headers is not None and find_response_header(headers, b"vary") is None:
                headers.append((b"vary", b"origin"))
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
                headers.append((b"vary", b"origin"))
        return response


__all__ = ["CORSMiddleware"]
