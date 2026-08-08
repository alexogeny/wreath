"""First-class HTTP policy compiled by Wreath and its native server.

Policy is configuration, not middleware.  None of the component classes expose
the public middleware protocol, none can be registered on a middleware tape,
and their ordering is fixed by `HttpPolicy` rather than by priorities.
"""

from __future__ import annotations

from os import urandom as _urandom
from typing import Any

from .._webpolicy import origin_matches as _native_origin_matches
from .._webpolicy import replace_cookie as _native_replace_cookie
from .cors import CorsPolicy
from .csrf import CsrfPolicy, csrf_token
from .csrf import _csrf_new_token as _native_csrf_new_token
from .csrf import _csrf_validate as _native_csrf_validate
from .proxy import ProxyPolicy
from .ratelimit import (
    MemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimitPolicy,
    RateLimitStore,
    TieredRateLimitPolicy,
    principal_key,
)
from .request_id import RequestIdPolicy, request_id
from .security import SecurityHeadersPolicy, TrustedHostPolicy, WebSocketOriginPolicy
from .timing import ServerTimingPolicy, elapsed

_PROXY = 1 << 0
_TRUSTED_HOST = 1 << 1
_RATE_LIMIT = 1 << 2
_REQUEST_ID = 1 << 3
_TIMING = 1 << 4
_CORS = 1 << 5
_CSRF = 1 << 6
_SECURITY = 1 << 7


class HttpPolicy:
    """The complete, fixed-order HTTP policy for one application.

    Standard policy components are immutable after construction.  At startup
    Wreath freezes them into a descriptor consumed by the native policy engine.
    The readable executor below is the independent reference used by pure builds
    and by differential tests; it is not a middleware tape.
    """

    __slots__ = (
        "_components",
        "_native_descriptor",
        "cors",
        "csrf",
        "proxy",
        "rate_limit",
        "principal_rate_limit",
        "request_id",
        "security_headers",
        "server_timing",
        "trusted_host",
        "websocket_origin",
    )

    def __init__(
        self,
        *,
        proxy: ProxyPolicy | None = None,
        trusted_host: TrustedHostPolicy | None = None,
        rate_limit: RateLimitPolicy | None = None,
        principal_rate_limit: RateLimitPolicy | TieredRateLimitPolicy | None = None,
        request_id: RequestIdPolicy | None = None,
        server_timing: ServerTimingPolicy | None = None,
        cors: CorsPolicy | None = None,
        csrf: CsrfPolicy | None = None,
        security_headers: SecurityHeadersPolicy | None = None,
        websocket_origin: WebSocketOriginPolicy | None = None,
    ) -> None:
        values = (
            ("proxy", proxy, ProxyPolicy),
            ("trusted_host", trusted_host, TrustedHostPolicy),
            ("rate_limit", rate_limit, RateLimitPolicy),
            (
                "principal_rate_limit",
                principal_rate_limit,
                (RateLimitPolicy, TieredRateLimitPolicy),
            ),
            ("request_id", request_id, RequestIdPolicy),
            ("server_timing", server_timing, ServerTimingPolicy),
            ("cors", cors, CorsPolicy),
            ("csrf", csrf, CsrfPolicy),
            ("security_headers", security_headers, SecurityHeadersPolicy),
            ("websocket_origin", websocket_origin, WebSocketOriginPolicy),
        )
        for name, value, expected in values:
            expected_types = expected if isinstance(expected, tuple) else (expected,)
            if value is not None and type(value) not in expected_types:
                expected_name = " or ".join(item.__name__ for item in expected_types)
                raise TypeError(
                    f"{name} must be an exact {expected_name}; "
                    "subclass behavior cannot be frozen into native policy"
                )
        self.proxy = proxy
        self.trusted_host = trusted_host
        self.rate_limit = rate_limit
        self.principal_rate_limit = principal_rate_limit
        self.request_id = request_id
        self.server_timing = server_timing
        self.cors = cors
        self.csrf = csrf
        self.security_headers = security_headers
        self.websocket_origin = websocket_origin
        self._components = tuple(value for _name, value, _expected in values if value is not None)
        self._native_descriptor = self._freeze_native()

    @property
    def components(self) -> tuple[Any, ...]:
        """Configured components in their canonical ingress order."""
        return self._components

    def merged(self, other: HttpPolicy) -> HttpPolicy:
        """Return one policy containing two disjoint feature declarations."""
        values: dict[str, Any] = {}
        for name in (
            "proxy",
            "trusted_host",
            "rate_limit",
            "principal_rate_limit",
            "request_id",
            "server_timing",
            "cors",
            "csrf",
            "security_headers",
            "websocket_origin",
        ):
            current = getattr(self, name)
            incoming = getattr(other, name)
            if current is not None and incoming is not None:
                raise ValueError(f"HTTP policy feature {name!r} is already configured")
            values[name] = current if current is not None else incoming
        return HttpPolicy(**values)

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """Persistent stores delegated to by configured policy."""
        owners: list[Any] = []
        for component in self._components:
            owners.extend(getattr(component, "schema_owners", ()))
        return tuple(owners)

    async def _reference_ingress(self, request: Any) -> Any | None:
        """Independent readable definition of the compiled ingress program."""
        mask = _SECURITY if self.security_headers is not None else 0
        request._policy_mask = mask

        if self.proxy is not None:
            self.proxy._ingress_sync(request)
            mask |= _PROXY
            request._policy_mask = mask
        if self.trusted_host is not None:
            candidate = self.trusted_host._ingress_sync(request)
            mask |= _TRUSTED_HOST
            request._policy_mask = mask
            if candidate is not None:
                return candidate
        rate_limit = self.rate_limit
        if rate_limit is not None:
            if rate_limit._ingress_sync is not None:
                candidate = rate_limit._ingress_sync(request)
            else:
                ingress = rate_limit._ingress
                if ingress is None:
                    raise RuntimeError("rate-limit policy has no bound ingress stage")
                candidate = await ingress(request)
            mask |= _RATE_LIMIT
            request._policy_mask = mask
            if candidate is not None:
                return candidate
        if self.request_id is not None:
            self.request_id._ingress_sync(request)
            mask |= _REQUEST_ID
            request._policy_mask = mask
        if self.server_timing is not None:
            self.server_timing._ingress_sync(request)
            mask |= _TIMING
            request._policy_mask = mask
        if self.cors is not None:
            candidate = self.cors._ingress_sync(request)
            mask |= _CORS
            request._policy_mask = mask
            if candidate is not None:
                return candidate
        if self.csrf is not None:
            candidate = self.csrf._ingress_sync(request)
            mask |= _CSRF
            request._policy_mask = mask
            if candidate is not None:
                return candidate
        return None

    async def _reference_post_auth(self, request: Any) -> Any | None:
        """Apply the principal-aware limiter after identity is available."""
        limiter = self.principal_rate_limit
        if limiter is not None:
            if isinstance(limiter, RateLimitPolicy):
                sync_ingress = limiter._ingress_sync
                if sync_ingress is not None:
                    return sync_ingress(request)
                ingress = limiter._ingress
                if ingress is None:
                    raise RuntimeError("principal rate-limit policy has no ingress stage")
                return await ingress(request)
            return await limiter._ingress(request)
        return None

    async def _reference_egress(self, request: Any, response: Any) -> Any:
        """Independent readable definition of the compiled egress program."""
        mask = request._policy_mask
        if mask & _SECURITY:
            component = self.security_headers
            if component is None:
                raise RuntimeError("security policy mask has no configured component")
            component._egress_inplace(request, response)
        if mask & _CSRF:
            component = self.csrf
            if component is None:
                raise RuntimeError("CSRF policy mask has no configured component")
            component._egress_inplace(request, response)
        if mask & _CORS:
            component = self.cors
            if component is None:
                raise RuntimeError("CORS policy mask has no configured component")
            component._egress_inplace(request, response)
        if mask & _TIMING:
            component = self.server_timing
            if component is None:
                raise RuntimeError("timing policy mask has no configured component")
            component._egress_inplace(request, response)
        if mask & _REQUEST_ID:
            component = self.request_id
            if component is None:
                raise RuntimeError("request-ID policy mask has no configured component")
            component._egress_inplace(request, response)
        return response

    async def _reference_websocket(self, request: Any) -> Any | None:
        """Run the fixed handshake policy on a conforming ASGI server."""
        if self.proxy is not None:
            self.proxy._ingress_sync(request)
        if self.trusted_host is not None:
            candidate = self.trusted_host._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.websocket_origin is not None:
            return await self.websocket_origin._ingress(request)
        return None

    def _freeze_native(self) -> tuple[Any, ...] | None:
        """Return the immutable v1 C descriptor, or None for a non-native option.

        All-or-nothing is load-bearing: a policy never splits into independently
        ordered Python and C halves.  Callback keys/exemptions, remote rate
        stores, and quota callbacks are deliberately refused by this compiler.
        The native engine gains an opcode before one of those options can enter
        this descriptor.
        """
        proxy = self.proxy
        if proxy is not None:
            proxy = (proxy._networks, proxy._trust_proto, proxy._trust_host)

        trusted = self.trusted_host
        if trusted is not None:
            trusted = tuple(value.encode("ascii") for value in trusted.allowed_hosts)

        rate = self.rate_limit
        if rate is not None:
            if rate._exempt is not None or rate._quota is not None:
                return None
            if rate._key is not rate._default_key:
                return None
            bucket = getattr(rate._store, "_bucket", None)
            if bucket is None:
                return None
            rate = (bucket, rate._cost, rate._policy_headers, rate)

        request_id = self.request_id
        if request_id is not None:
            request_id = (
                request_id._header_bytes,
                request_id._trust_inbound,
                request_id._echo,
                request_id._max_length,
                _urandom,
            )

        timing = self.server_timing
        if timing is not None:
            timing = (timing._metric, timing._emit)

        cors = self.cors
        if cors is not None:
            cors = (
                cors._allow_all_origins,
                frozenset(value.encode("latin-1") for value in cors._allow_origins),
                frozenset(value.encode("ascii") for value in cors._allow_methods),
                cors._preflight_headers,
                cors._simple_headers,
            )

        csrf = self.csrf
        if csrf is not None:
            if csrf._exempt is not None:
                return None
            csrf = (
                csrf._secret,
                csrf._cookie_name.encode("ascii"),
                csrf._cookie_prefix,
                csrf._header_name_bytes,
                csrf._max_age,
                csrf._secure,
                csrf._same_site.encode("ascii"),
                csrf._trusted_origins,
                tuple(value.encode("ascii") for value in csrf._trusted_hosts),
                csrf._allow_missing_origin,
                csrf,
                _native_csrf_new_token,
                _native_csrf_validate,
                _native_origin_matches,
                _native_replace_cookie,
            )

        security = self.security_headers
        if security is not None:
            security = (security.headers, security.https_headers)

        websocket_origin = self.websocket_origin
        if websocket_origin is not None:
            websocket_origin = (
                websocket_origin.allowed_origins,
                _native_origin_matches,
            )

        return (
            "wreath.http-policy.v1",
            proxy,
            trusted,
            rate,
            request_id,
            timing,
            cors,
            csrf,
            security,
            websocket_origin,
        )


_POLICY_TYPES = frozenset(
    {
        CorsPolicy,
        CsrfPolicy,
        HttpPolicy,
        ProxyPolicy,
        RateLimitPolicy,
        RequestIdPolicy,
        SecurityHeadersPolicy,
        ServerTimingPolicy,
        TieredRateLimitPolicy,
        TrustedHostPolicy,
        WebSocketOriginPolicy,
    }
)


def is_policy_component(value: Any) -> bool:
    """Whether a value belongs in ``HttpPolicy``, never on a custom tape."""
    return type(value) in _POLICY_TYPES


__all__ = [
    "CorsPolicy",
    "CsrfPolicy",
    "HttpPolicy",
    "MemoryRateLimitStore",
    "PostgresRateLimitStore",
    "ProxyPolicy",
    "RateLimitPolicy",
    "RateLimitStore",
    "RequestIdPolicy",
    "SecurityHeadersPolicy",
    "ServerTimingPolicy",
    "TieredRateLimitPolicy",
    "TrustedHostPolicy",
    "WebSocketOriginPolicy",
    "csrf_token",
    "elapsed",
    "principal_key",
    "request_id",
]
