"""OAuth browser backend-for-frontend primitives from RFC 10017.

A browser BFF keeps OAuth tokens in a server-side session and exposes only
application-shaped routes to the browser. Each proxied resource is compiled to
one fixed-origin outbound client and one fixed path prefix; no request value can
select a scheme, host, port, or resource client.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Set
from dataclasses import dataclass
from math import isfinite
from typing import Any, cast
from urllib.parse import quote, urlsplit

from ._http import _is_http_token
from .exceptions import BadRequest, Forbidden, Unauthorized
from .policy.sessions import SessionPolicy, rotate_session
from .request import Request
from .response import Response
from .router import Router

__all__ = [
    "BFFResource",
    "bff_access_token",
    "bff_router",
    "bff_session_policy",
    "clear_bff_tokens",
    "set_bff_tokens",
]

type TokenResolver = Callable[[Request], str | None | Awaitable[str | None]]

_SESSION_KEY = "_wreath_bff"
_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*\Z")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._~-]*[A-Za-z0-9])?\Z")
_INVALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_FORBIDDEN_METHODS = frozenset({"CONNECT", "OPTIONS", "TRACE"})
_RESERVED_RESOURCES = frozenset({"logout", "session"})
_CORS_SAFELISTED_HEADERS = frozenset(
    {b"accept", b"accept-language", b"content-language", b"content-type", b"range"}
)
_REQUEST_HEADERS = frozenset(
    {
        b"accept",
        b"accept-language",
        b"content-digest",
        b"content-type",
        b"idempotency-key",
        b"if-match",
        b"if-modified-since",
        b"if-none-match",
        b"if-unmodified-since",
        b"prefer",
        b"range",
        b"repr-digest",
    }
)
_RESPONSE_HEADERS = frozenset(
    {
        b"accept-ranges",
        b"cache-control",
        b"content-digest",
        b"content-disposition",
        b"content-language",
        b"content-range",
        b"content-type",
        b"etag",
        b"last-modified",
        b"link",
        b"preference-applied",
        b"repr-digest",
        b"retry-after",
        b"vary",
    }
)


def bff_session_policy(
    secret: str,
    *,
    store: Any,
    max_age: int = 14 * 24 * 3600,
    previous_secrets: Any = (),
) -> SessionPolicy:
    """Build the strict server-side session required by an OAuth browser BFF.

    The cookie is host-only, scoped to `/`, Secure, HttpOnly, SameSite=Strict,
    and carries only an opaque signed session id. A missing store is refused
    because Wreath's ordinary fallback stores session contents in the cookie;
    signing OAuth tokens is not the encryption RFC 10017 requires for that
    deployment shape.
    """
    if store is None:
        raise ValueError("a BFF requires a server-side SessionStore; store cannot be None")
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
        raise ValueError("BFF session max_age must be a positive integer number of seconds")
    return SessionPolicy(
        secret,
        cookie="__Host-Http-wreath_bff",
        max_age=max_age,
        same_site="strict",
        secure=True,
        http_only=True,
        store=store,
        previous_secrets=previous_secrets,
    )


def _validate_access_token(value: object, *, source: str) -> str:
    if not isinstance(value, str) or not value or _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{source} must be a non-empty RFC 6750 bearer token without whitespace")
    return value


def set_bff_tokens(
    request: Request,
    *,
    access_token: str,
    refresh_token: str | None = None,
    expires_at: int | float | None = None,
) -> None:
    """Put an OAuth token set in the active server-side BFF session.

    Call this after the authorization-code exchange. The session id is rotated
    at the privilege change, and no token is returned to the browser.
    """
    token = _validate_access_token(access_token, source="access_token")
    if refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token):
        raise ValueError("refresh_token must be a non-empty string or None")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int | float)
    ):
        raise TypeError("expires_at must be an int, float, or None")
    if isinstance(expires_at, float) and not isfinite(expires_at):
        raise ValueError("expires_at must be a finite timestamp")
    state = request.state
    if state.get("_session_server_side") is not True:
        raise RuntimeError(
            "set_bff_tokens requires a server-side BFF session from bff_session_policy"
        )
    session = state.session
    token_set: dict[str, str | int | float] = {"access_token": token}
    if expires_at is not None:
        token_set["expires_at"] = expires_at
    if refresh_token is not None:
        token_set["refresh_token"] = refresh_token
    session[_SESSION_KEY] = token_set
    rotate_session(request)


def clear_bff_tokens(request: Request) -> None:
    """Remove the BFF token set and rotate the active server-side session."""
    state = request.state
    if state.get("_session_server_side") is not True:
        raise RuntimeError(
            "clear_bff_tokens requires a server-side BFF session from bff_session_policy"
        )
    session = state.session
    session.pop(_SESSION_KEY, None)
    rotate_session(request)


def bff_access_token(request: Request) -> str | None:
    """Return the unexpired access token held in the active BFF session."""
    state = request.state
    if state.get("_session_server_side") is not True:
        return None
    session = state.get("session")
    if not isinstance(session, dict):
        return None
    token_set = session.get(_SESSION_KEY)
    if not isinstance(token_set, dict):
        return None
    access_token = token_set.get("access_token")
    if not isinstance(access_token, str) or _TOKEN.fullmatch(access_token) is None:
        return None
    expires_at = token_set.get("expires_at")
    if (
        isinstance(expires_at, bool)
        or expires_at is not None
        and not isinstance(expires_at, int | float)
    ):
        return None
    if isinstance(expires_at, float) and not isfinite(expires_at):
        return None
    if expires_at is not None and expires_at <= time.time():
        return None
    return access_token


def _validate_target_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"target_prefix must be str, not {type(value).__name__}")
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("target_prefix must be an origin-relative path beginning with one '/'")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("target_prefix must be ASCII and percent-encoded") from error
    if _INVALID_PERCENT.search(value):
        raise ValueError("target_prefix contains an invalid percent escape")
    decoded = _percent_decode(encoded)
    if b"\\" in decoded or any(segment in {b".", b".."} for segment in decoded.split(b"/")):
        raise ValueError("target_prefix cannot contain backslashes or dot segments")
    return value.rstrip("/")


def _percent_decode(value: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(value):
        if value[index] == 37:
            decoded.append(int(value[index + 1 : index + 3], 16))
            index += 3
        else:
            decoded.append(value[index])
            index += 1
    return bytes(decoded)


@dataclass(frozen=True, slots=True)
class BFFResource:
    """One fixed OAuth resource server exposed through the BFF.

    `client` is normally a `wreath.http_client.HTTPClient`. Its HTTPS
    origin is inspected once here; outbound requests receive only
    origin-relative targets under `target_prefix`.
    """

    client: Any
    target_prefix: str = "/"
    methods: Set[str] = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT", "QUERY"})

    def __post_init__(self) -> None:
        origin = getattr(self.client, "origin", None)
        if not isinstance(origin, str):
            raise TypeError("client must expose its HTTPS origin as text")
        parsed = urlsplit(origin)
        if parsed.scheme != "https":
            raise ValueError("BFF resource clients must use an HTTPS origin")
        if (
            parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("client origin must contain only an HTTPS scheme, host, and port")
        if not callable(getattr(self.client, "request", None)):
            raise TypeError("client must expose an async request method")
        normalized_methods: set[str] = set()
        for method in self.methods:
            if not isinstance(method, str):
                raise TypeError(f"BFF resource methods must be str, not {type(method).__name__}")
            normalized = method.upper()
            if not _is_http_token(normalized):
                raise ValueError(f"invalid BFF resource method {method!r}; use an HTTP token")
            if normalized in _FORBIDDEN_METHODS:
                raise ValueError(f"BFF resource method {normalized} is not proxyable")
            normalized_methods.add(normalized)
        if not normalized_methods:
            raise ValueError("BFF resource methods must contain at least one method")
        if "GET" in normalized_methods:
            normalized_methods.discard("HEAD")
        object.__setattr__(self, "target_prefix", _validate_target_prefix(self.target_prefix))
        object.__setattr__(self, "methods", frozenset(normalized_methods))


def _csrf_header(value: tuple[bytes, bytes]) -> tuple[bytes, bytes]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError("csrf_header must be one (name, value) bytes tuple")
    name, expected = value
    if not isinstance(name, bytes) or not isinstance(expected, bytes):
        raise TypeError("csrf_header name and value must be bytes")
    name = name.lower()
    try:
        token_name = name.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("csrf_header name must be an ASCII HTTP token") from error
    if not _is_http_token(token_name) or not expected or b"\r" in expected or b"\n" in expected:
        raise ValueError("csrf_header must contain an HTTP token name and non-empty safe value")
    if name in _CORS_SAFELISTED_HEADERS:
        raise ValueError("csrf_header must not be CORS-safelisted; use a custom header")
    return name, expected


def _validate_resource_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"BFF resource names must be str, not {type(name).__name__}")
    if _RESOURCE_NAME.fullmatch(name) is None:
        raise ValueError(
            f"invalid BFF resource name {name!r}; use letters, digits, dots, "
            "underscores, or hyphens"
        )
    if name.lower() in _RESERVED_RESOURCES:
        raise ValueError(f"BFF resource name {name!r} is reserved")
    return name


def _validate_dynamic_path(path: str) -> str:
    if "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
        raise BadRequest("BFF resource paths cannot contain backslashes or dot segments")
    return quote(path, safe="/!$&'()*+,-.:;=@_~")


def _query_suffix(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise BadRequest("BFF query strings must be ASCII and percent-encoded") from error
    if _INVALID_PERCENT.search(value) or "#" in value or any(ord(char) < 32 for char in value):
        raise BadRequest("BFF query strings must use valid percent escapes and contain no controls")
    return "?" + value


def _target(resource: BFFResource, path: str, query: bytes) -> str:
    suffix = _validate_dynamic_path(path)
    prefix = resource.target_prefix
    if suffix:
        target = f"{prefix}/{suffix}" if prefix else f"/{suffix}"
    else:
        target = prefix or "/"
    return target + _query_suffix(query)


def _forwarded_request_headers(request: Request, token: str) -> tuple[tuple[bytes, bytes], ...]:
    selected = [(name, value) for name, value in request.headers if name in _REQUEST_HEADERS]
    selected.append((b"authorization", b"Bearer " + token.encode("ascii")))
    return tuple(selected)


def _forwarded_response(response: Any) -> Response:
    headers = tuple(
        (name.lower(), value)
        for name, value in response.headers
        if name.lower() in _RESPONSE_HEADERS
    )
    return Response(response.body, status=response.status, headers=headers, media_type=b"")


def bff_router(
    resources: Mapping[str, BFFResource],
    *,
    prefix: str = "/bff",
    token: TokenResolver = bff_access_token,
    csrf_header: tuple[bytes, bytes] = (b"x-wreath-bff", b"1"),
) -> Router:
    """Compile fixed-resource browser routes for an OAuth backend-for-frontend.

    Every proxy request must carry `csrf_header` exactly once. Since a custom
    header is not CORS-safelisted, a cross-origin browser request must first
    pass the application's CORS preflight policy. Cookie, Authorization, Host,
    hop-by-hop fields, and unlisted extension fields never reach the resource
    server; its `Set-Cookie` and hop-by-hop fields never reach the browser.
    """
    if not isinstance(resources, Mapping) or not resources:
        raise ValueError("resources must map at least one name to a BFFResource")
    expected_header = _csrf_header(csrf_header)
    compiled = tuple(
        (_validate_resource_name(name), resource) for name, resource in resources.items()
    )
    for name, resource in compiled:
        if not isinstance(resource, BFFResource):
            raise TypeError(
                f"resources[{name!r}] must be BFFResource, not {type(resource).__name__}"
            )

    if not callable(token):
        raise TypeError("token must be a callable BFF token resolver")
    async_resolver = inspect.iscoroutinefunction(token) or inspect.iscoroutinefunction(
        type(token).__call__
    )
    router = Router(prefix=prefix, tags=("browser-bff",))

    async def resolve(request: Request) -> str | None:
        candidate = token(request)
        if async_resolver or inspect.isawaitable(candidate):
            resolved = await cast(Awaitable[str | None], candidate)
        else:
            resolved = candidate
        if resolved is None:
            return None
        try:
            return _validate_access_token(resolved, source="token resolver result")
        except ValueError as error:
            raise RuntimeError(str(error)) from error

    def require_csrf(request: Request) -> None:
        name, expected = expected_header
        values = [value for key, value in request.headers if key == name]
        if values != [expected]:
            raise Forbidden(f"BFF requests require exactly one {name.decode('ascii')} header")

    async def forward(request: Request, resource: BFFResource, path: str) -> Response:
        require_csrf(request)
        access_token = await resolve(request)
        if access_token is None:
            raise Unauthorized("The BFF session is not authenticated", challenge="BFF")
        target = _target(resource, path, request.query_string)
        body = await request.body()
        upstream = await resource.client.request(
            request.method,
            target,
            headers=_forwarded_request_headers(request, access_token),
            body=body,
        )
        return _forwarded_response(upstream)

    def resource_handlers(
        bound_resource: BFFResource,
    ) -> tuple[Callable[[Request], Awaitable[Response]], Callable[..., Awaitable[Response]]]:
        async def resource_root(request: Request) -> Response:
            return await forward(request, bound_resource, "")

        async def resource_path(request: Request, path: str) -> Response:
            return await forward(request, bound_resource, path)

        return resource_root, resource_path

    @router.get("/session", summary="Read BFF session status")
    async def session_status(request: Request) -> dict[str, bool]:
        return {"active": await resolve(request) is not None}

    @router.post("/logout", summary="Revoke the BFF session", response_only=True)
    async def logout(request: Request) -> Response:
        require_csrf(request)
        request.state.session.clear()
        rotate_session(request)
        return Response(status=204)

    for name, resource in compiled:
        methods = tuple(sorted(resource.methods))
        resource_root, resource_path = resource_handlers(resource)

        router.route(
            f"/{name}",
            methods=methods,
            summary=f"Proxy {name} through the browser BFF",
            response_only=True,
        )(resource_root)
        router.route(
            f"/{name}/{{path:path}}",
            methods=methods,
            summary=f"Proxy {name} paths through the browser BFF",
            response_only=True,
        )(resource_path)

    return router
