"""First-class HTTP policy compiled by Wreath and its native server.

Policy is configuration, not middleware.  None of the component classes expose
the public middleware protocol, none can be registered on a middleware tape,
and their ordering is fixed by `HttpPolicy` rather than by priorities.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .admission import AdmissionStats, ConcurrencyPolicy
    from .cache import CachePolicy
    from .compression import CompressionPolicy
    from .cors import CorsPolicy
    from .csrf import CsrfPolicy, csrf_token
    from .deadline import DeadlinePolicy
    from .idempotency import (
        IdempotencyPolicy,
        IdempotencyStore,
        MemoryIdempotencyStore,
        PostgresIdempotencyStore,
    )
    from .maintenance import MaintenancePolicy
    from .proxy import ProxyPolicy
    from .ratelimit import (
        MemoryRateLimitStore,
        PostgresRateLimitStore,
        RateLimitPolicy,
        RateLimitStore,
        TieredRateLimitPolicy,
        principal_key,
    )
    from .request_decompression import RequestDecompressionPolicy
    from .request_id import RequestIdPolicy, request_id
    from .security import (
        SecurityHeadersPolicy,
        TrustedHostPolicy,
        WebSocketOriginPolicy,
        csp_nonce,
    )
    from .sessions import SessionPolicy, rotate_session
    from .signed_routes import SignedRoutePolicy
    from .timing import ServerTimingPolicy, elapsed
    from .traffic import (
        AI_SCRAPERS,
        AIScrapingPolicy,
        TrafficClass,
        TrafficPolicy,
        traffic_class,
    )


def _policy_module(module: str) -> Any:
    from importlib import import_module

    return import_module(f".{module}", __name__)


def _policy_export(module: str, name: str) -> Any:
    return getattr(_policy_module(module), name)


_EXPECTED_TYPES = {
    "proxy": (("proxy", "ProxyPolicy"),),
    "trusted_host": (("security", "TrustedHostPolicy"),),
    "maintenance": (("maintenance", "MaintenancePolicy"),),
    "ai_scraping": (("traffic", "AIScrapingPolicy"),),
    "traffic": (("traffic", "TrafficPolicy"),),
    "rate_limit": (("ratelimit", "RateLimitPolicy"),),
    "principal_rate_limit": (
        ("ratelimit", "RateLimitPolicy"),
        ("ratelimit", "TieredRateLimitPolicy"),
    ),
    "signed_routes": (("signed_routes", "SignedRoutePolicy"),),
    "request_decompression": (
        ("request_decompression", "RequestDecompressionPolicy"),
    ),
    "request_id": (("request_id", "RequestIdPolicy"),),
    "server_timing": (("timing", "ServerTimingPolicy"),),
    "cors": (("cors", "CorsPolicy"),),
    "csrf": (("csrf", "CsrfPolicy"),),
    "security_headers": (("security", "SecurityHeadersPolicy"),),
    "websocket_origin": (("security", "WebSocketOriginPolicy"),),
    "session": (("sessions", "SessionPolicy"),),
    "idempotency": (("idempotency", "IdempotencyPolicy"),),
    "cache_control": (("cache", "CachePolicy"),),
    "compression": (("compression", "CompressionPolicy"),),
    "concurrency": (("admission", "ConcurrencyPolicy"),),
    "deadline": (("deadline", "DeadlinePolicy"),),
}

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

    Standard policy components are immutable after construction. At startup,
    Wreath freezes every natively representable combination into a descriptor
    consumed by the native policy engine. The readable executor below is the
    independent reference used by pure builds, differential tests, and policy
    combinations awaiting a native opcode; it is not a middleware tape.
    """

    __slots__ = (
        "_components",
        "_dynamic_always",
        "_has_action",
        "_has_activation",
        "_has_post_auth",
        "_native_ingress_only",
        "_native_descriptor",
        "ai_scraping",
        "cors",
        "csrf",
        "cache_control",
        "compression",
        "concurrency",
        "deadline",
        "idempotency",
        "maintenance",
        "proxy",
        "rate_limit",
        "principal_rate_limit",
        "request_decompression",
        "request_id",
        "security_headers",
        "server_timing",
        "signed_routes",
        "traffic",
        "session",
        "trusted_host",
        "websocket_origin",
    )

    def __init__(
        self,
        *,
        proxy: ProxyPolicy | None = None,
        trusted_host: TrustedHostPolicy | None = None,
        ai_scraping: AIScrapingPolicy | None = None,
        rate_limit: RateLimitPolicy | None = None,
        principal_rate_limit: RateLimitPolicy | TieredRateLimitPolicy | None = None,
        request_decompression: RequestDecompressionPolicy | None = None,
        request_id: RequestIdPolicy | None = None,
        server_timing: ServerTimingPolicy | None = None,
        signed_routes: SignedRoutePolicy | None = None,
        traffic: TrafficPolicy | None = None,
        cors: CorsPolicy | None = None,
        csrf: CsrfPolicy | None = None,
        security_headers: SecurityHeadersPolicy | None = None,
        websocket_origin: WebSocketOriginPolicy | None = None,
        session: SessionPolicy | None = None,
        idempotency: IdempotencyPolicy | None = None,
        cache_control: CachePolicy | None = None,
        compression: CompressionPolicy | None = None,
        concurrency: ConcurrencyPolicy | None = None,
        deadline: DeadlinePolicy | None = None,
        maintenance: MaintenancePolicy | None = None,
    ) -> None:
        values = (
            ("proxy", proxy),
            ("trusted_host", trusted_host),
            ("maintenance", maintenance),
            ("ai_scraping", ai_scraping),
            ("traffic", traffic),
            ("rate_limit", rate_limit),
            ("principal_rate_limit", principal_rate_limit),
            ("signed_routes", signed_routes),
            ("request_decompression", request_decompression),
            ("request_id", request_id),
            ("server_timing", server_timing),
            ("cors", cors),
            ("csrf", csrf),
            ("security_headers", security_headers),
            ("websocket_origin", websocket_origin),
            ("session", session),
            ("idempotency", idempotency),
            ("cache_control", cache_control),
            ("compression", compression),
            ("concurrency", concurrency),
            ("deadline", deadline),
        )
        for name, value in values:
            if value is not None:
                expected_types = tuple(
                    _policy_export(module, export)
                    for module, export in _EXPECTED_TYPES[name]
                )
                if type(value) in expected_types:
                    continue
                expected_name = " or ".join(item.__name__ for item in expected_types)
                raise TypeError(
                    f"{name} must be an exact {expected_name}; "
                    "subclass behavior cannot be frozen into native policy"
                )
        self.proxy = proxy
        self.trusted_host = trusted_host
        self.ai_scraping = ai_scraping
        self.rate_limit = rate_limit
        self.principal_rate_limit = principal_rate_limit
        self.request_decompression = request_decompression
        self.request_id = request_id
        self.server_timing = server_timing
        self.signed_routes = signed_routes
        self.traffic = traffic
        self.cors = cors
        self.csrf = csrf
        self.security_headers = security_headers
        self.websocket_origin = websocket_origin
        self.session = session
        self.idempotency = idempotency
        self.cache_control = cache_control
        self.compression = compression
        self.concurrency = concurrency
        self.deadline = deadline
        self.maintenance = maintenance
        self._has_action = concurrency is not None or deadline is not None
        self._has_activation = session is not None
        self._has_post_auth = principal_rate_limit is not None or idempotency is not None
        self._dynamic_always = (
            session is not None
            or idempotency is not None
            or (cache_control is not None and cache_control.policy is not None)
        )
        self._components = tuple(value for _name, value in values if value is not None)
        self._native_descriptor = self._freeze_native()
        self._native_ingress_only = (
            ai_scraping is not None
            and self._native_descriptor is not None
            and all(value is None for name, value in values if name != "ai_scraping")
        )

    @property
    def components(self) -> tuple[Any, ...]:
        """Configured components in their canonical ingress order."""
        return self._components

    @property
    def counter_sources(self) -> tuple[Any, ...]:
        """Policy components that may contribute to the canonical metrics walk."""
        return self._components

    def merged(self, other: HttpPolicy) -> HttpPolicy:
        """Return one policy containing two disjoint feature declarations."""
        return self._merged(other, replace_default_ai=False)

    def _merged(self, other: HttpPolicy, *, replace_default_ai: bool) -> HttpPolicy:
        """Merge application declarations, optionally replacing Wreath's default."""
        values: dict[str, Any] = {}
        for name in (
            "proxy",
            "trusted_host",
            "ai_scraping",
            "rate_limit",
            "principal_rate_limit",
            "signed_routes",
            "request_decompression",
            "request_id",
            "server_timing",
            "traffic",
            "cors",
            "csrf",
            "security_headers",
            "websocket_origin",
            "session",
            "idempotency",
            "cache_control",
            "compression",
            "concurrency",
            "deadline",
            "maintenance",
        ):
            current = getattr(self, name)
            incoming = getattr(other, name)
            replacing = name == "ai_scraping" and replace_default_ai and incoming is not None
            if current is not None and incoming is not None and not replacing:
                raise ValueError(f"HTTP policy feature {name!r} is already configured")
            values[name] = incoming if replacing or current is None else current
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
        if self.security_headers is not None:
            self.security_headers._prepare_nonce(request)

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
        if self.maintenance is not None:
            candidate = self.maintenance._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.ai_scraping is not None:
            candidate = self.ai_scraping._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.traffic is not None:
            candidate = self.traffic._ingress_sync(request)
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
        if self.signed_routes is not None:
            candidate = self.signed_routes._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.request_decompression is not None:
            candidate = await self.request_decompression._ingress(request)
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
            candidate = (
                self.csrf._ingress_sync(request)
                if self.csrf._form_field is None
                else await self.csrf._ingress(request)
            )
            mask |= _CSRF
            request._policy_mask = mask
            return candidate
        return None

    def _reference_ingress_scope(self, scope: Any, method: str, path: str) -> Any | None:
        """Execute the sole scope-safe ingress component before activation."""
        if not self._native_ingress_only or self.ai_scraping is None:
            raise RuntimeError("HTTP policy is not scope-only ingress")
        return self.ai_scraping._ingress_scope(scope, method, path)

    async def _reference_post_auth(self, request: Any) -> Any | None:
        """Apply the principal-aware limiter after identity is available."""
        limiter = self.principal_rate_limit
        if limiter is not None:
            if type(limiter) is _policy_export("ratelimit", "RateLimitPolicy"):
                rate_limiter = cast("RateLimitPolicy", limiter)
                sync_ingress = rate_limiter._ingress_sync
                if sync_ingress is not None:
                    candidate = sync_ingress(request)
                else:
                    ingress = rate_limiter._ingress
                    if ingress is None:
                        raise RuntimeError("principal rate-limit policy has no ingress stage")
                    candidate = await ingress(request)
            else:
                tiered_limiter = cast("TieredRateLimitPolicy", limiter)
                candidate = await tiered_limiter._ingress(request)
            if candidate is not None:
                return candidate
        idempotency = self.idempotency
        return None if idempotency is None else await idempotency.action(request)

    async def _reference_activation(self, request: Any) -> None:
        """Load request state whose semantics begin after native ingress."""
        session = self.session
        if session is not None:
            await session.before(request)

    def _reference_action_enter(self) -> Any | None:
        """Acquire the fail-fast handler permit, or return its 503 response."""
        concurrency = self.concurrency
        if concurrency is None or concurrency._acquire():
            return None
        return concurrency._refusal()

    def _reference_action_exit(self) -> None:
        """Release a handler permit acquired by `_reference_action_enter`."""
        concurrency = self.concurrency
        if concurrency is not None:
            concurrency._release()

    async def _reference_dynamic_egress(
        self, request: Any, response: Any, *, native_one_shot: bool = False
    ) -> Any:
        """Run stateful/payload policy around the fixed native egress program."""
        if self.idempotency is not None:
            response = await self.idempotency.after(request, response)
        if self.session is not None:
            response = await self.session.after(request, response)
        if self.cache_control is not None and (
            not native_one_shot or self.cache_control.policy is not None
        ):
            response = await self.cache_control.after(request, response)
        if self.compression is not None and not native_one_shot:
            response = await self.compression.after(request, response)
        return response

    async def _reference_egress(self, request: Any, response: Any) -> Any:
        """Independent readable definition of the compiled egress program."""
        response = await self._reference_dynamic_egress(request, response)
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
        if self.ai_scraping is not None:
            candidate = self.ai_scraping._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.traffic is not None:
            candidate = self.traffic._ingress_sync(request)
            if candidate is not None:
                return candidate
        if self.websocket_origin is not None:
            return await self.websocket_origin._ingress(request)
        return None

    def _freeze_native(self) -> tuple[Any, ...] | None:
        """Return the immutable v3 C descriptor, or None for a non-native option.

        All-or-nothing is load-bearing: a policy never splits into independently
        ordered Python and C halves.  Callback keys/exemptions, remote rate
        stores, and quota callbacks are deliberately refused by this compiler.
        The native engine gains an opcode before one of those options can enter
        this descriptor.
        """
        if (
            self.traffic is not None
            or self.request_decompression is not None
            or self.signed_routes is not None
        ):
            return None

        proxy = self.proxy
        if proxy is not None:
            proxy = (proxy._networks, proxy._trust_proto, proxy._trust_host)

        trusted = self.trusted_host
        if trusted is not None:
            trusted = tuple(value.encode("ascii") for value in trusted.allowed_hosts)

        ai_scraping = self.ai_scraping
        if ai_scraping is not None:
            ai_scraping = ai_scraping._native()

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
            from os import urandom

            request_id = (
                request_id._header_bytes,
                request_id._trust_inbound,
                request_id._echo,
                request_id._max_length,
                urandom,
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
                frozenset(value.encode("ascii") for value in cors._allow_headers),
                "*" in cors._allow_headers,
            )

        csrf = self.csrf
        if csrf is not None:
            if csrf._exempt is not None or csrf._form_field is not None:
                return None
            from .._webpolicy import origin_matches, replace_cookie
            from .csrf import _csrf_new_token, _csrf_validate

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
                _csrf_new_token,
                _csrf_validate,
                origin_matches,
                replace_cookie,
            )

        security = self.security_headers
        if security is not None:
            if security._has_nonce:
                return None
            security = (security.headers, security.https_headers)

        websocket_origin = self.websocket_origin
        if websocket_origin is not None:
            from .._webpolicy import origin_matches

            websocket_origin = (
                websocket_origin.allowed_origins,
                origin_matches,
            )

        cache = self.cache_control
        if cache is not None:
            cache = (
                None
                if cache.default is None or cache.policy is not None
                else (cache.default.to_header(), cache.default.public)
            )

        compression = self.compression
        if compression is not None:
            from .._compression import _dcz_compress, require_zstd

            native_dcz = tuple(compression._dcz_dictionaries)
            compression = (
                compression.minimum_size,
                compression.gzip_level,
                compression.zstd_level,
                compression.compress_authenticated,
                require_zstd().compress,
                compression._gzip_workspace,
                native_dcz,
                _dcz_compress,
                compression._gzip_fragments,
            )

        maintenance = self.maintenance
        if maintenance is not None:
            maintenance = maintenance._native()

        return (
            "wreath.http-policy.v5",
            proxy,
            trusted,
            ai_scraping,
            rate,
            request_id,
            timing,
            cors,
            csrf,
            security,
            websocket_origin,
            cache,
            compression,
            maintenance,
        )


_POLICY_TYPES = {
    f"{__name__}.admission": ("admission", frozenset({"ConcurrencyPolicy"})),
    f"{__name__}.cache": ("cache", frozenset({"CachePolicy"})),
    f"{__name__}.compression": ("compression", frozenset({"CompressionPolicy"})),
    f"{__name__}.cors": ("cors", frozenset({"CorsPolicy"})),
    f"{__name__}.csrf": ("csrf", frozenset({"CsrfPolicy"})),
    f"{__name__}.deadline": ("deadline", frozenset({"DeadlinePolicy"})),
    f"{__name__}.idempotency": ("idempotency", frozenset({"IdempotencyPolicy"})),
    f"{__name__}.maintenance": ("maintenance", frozenset({"MaintenancePolicy"})),
    f"{__name__}.proxy": ("proxy", frozenset({"ProxyPolicy"})),
    f"{__name__}.ratelimit": (
        "ratelimit",
        frozenset({"RateLimitPolicy", "TieredRateLimitPolicy"}),
    ),
    f"{__name__}.request_decompression": (
        "request_decompression",
        frozenset({"RequestDecompressionPolicy"}),
    ),
    f"{__name__}.request_id": ("request_id", frozenset({"RequestIdPolicy"})),
    f"{__name__}.security": (
        "security",
        frozenset(
            {"SecurityHeadersPolicy", "TrustedHostPolicy", "WebSocketOriginPolicy"}
        ),
    ),
    f"{__name__}.sessions": ("sessions", frozenset({"SessionPolicy"})),
    f"{__name__}.signed_routes": (
        "signed_routes",
        frozenset({"SignedRoutePolicy"}),
    ),
    f"{__name__}.timing": ("timing", frozenset({"ServerTimingPolicy"})),
    f"{__name__}.traffic": (
        "traffic",
        frozenset({"AIScrapingPolicy", "TrafficPolicy"}),
    ),
}


def is_policy_component(value: Any) -> bool:
    """Whether a value belongs in ``HttpPolicy``, never on a custom tape."""
    value_type = type(value)
    if value_type is HttpPolicy:
        return True
    policy_type = _POLICY_TYPES.get(value_type.__module__)
    if policy_type is None or value_type.__name__ not in policy_type[1]:
        return False
    return value_type is _policy_export(policy_type[0], value_type.__name__)


__all__ = [
    "AI_SCRAPERS",
    "AIScrapingPolicy",
    "CachePolicy",
    "AdmissionStats",
    "CompressionPolicy",
    "ConcurrencyPolicy",
    "CorsPolicy",
    "CsrfPolicy",
    "DeadlinePolicy",
    "HttpPolicy",
    "IdempotencyPolicy",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "PostgresIdempotencyStore",
    "MaintenancePolicy",
    "MemoryRateLimitStore",
    "PostgresRateLimitStore",
    "ProxyPolicy",
    "RateLimitPolicy",
    "RateLimitStore",
    "RequestDecompressionPolicy",
    "RequestIdPolicy",
    "SecurityHeadersPolicy",
    "ServerTimingPolicy",
    "SessionPolicy",
    "SignedRoutePolicy",
    "TieredRateLimitPolicy",
    "TrafficClass",
    "TrafficPolicy",
    "TrustedHostPolicy",
    "WebSocketOriginPolicy",
    "csrf_token",
    "csp_nonce",
    "elapsed",
    "principal_key",
    "rotate_session",
    "request_id",
    "traffic_class",
]

_EXPORTS = {
    "AI_SCRAPERS": "traffic",
    "AIScrapingPolicy": "traffic",
    "AdmissionStats": "admission",
    "CachePolicy": "cache",
    "CompressionPolicy": "compression",
    "ConcurrencyPolicy": "admission",
    "CorsPolicy": "cors",
    "CsrfPolicy": "csrf",
    "DeadlinePolicy": "deadline",
    "IdempotencyPolicy": "idempotency",
    "IdempotencyStore": "idempotency",
    "MaintenancePolicy": "maintenance",
    "MemoryIdempotencyStore": "idempotency",
    "MemoryRateLimitStore": "ratelimit",
    "PostgresIdempotencyStore": "idempotency",
    "PostgresRateLimitStore": "ratelimit",
    "ProxyPolicy": "proxy",
    "RateLimitPolicy": "ratelimit",
    "RateLimitStore": "ratelimit",
    "RequestDecompressionPolicy": "request_decompression",
    "RequestIdPolicy": "request_id",
    "SecurityHeadersPolicy": "security",
    "ServerTimingPolicy": "timing",
    "SessionPolicy": "sessions",
    "SignedRoutePolicy": "signed_routes",
    "TieredRateLimitPolicy": "ratelimit",
    "TrafficClass": "traffic",
    "TrafficPolicy": "traffic",
    "TrustedHostPolicy": "security",
    "WebSocketOriginPolicy": "security",
    "csrf_token": "csrf",
    "csp_nonce": "security",
    "elapsed": "timing",
    "principal_key": "ratelimit",
    "request_id": "request_id",
    "rotate_session": "sessions",
    "traffic_class": "traffic",
}

_MODULE_EXPORTS = {
    "traffic": (
        "AI_SCRAPERS",
        "AIScrapingPolicy",
        "TrafficClass",
        "TrafficPolicy",
        "traffic_class",
    ),
    "admission": ("AdmissionStats", "ConcurrencyPolicy"),
    "cache": ("CachePolicy",),
    "compression": ("CompressionPolicy",),
    "cors": ("CorsPolicy",),
    "csrf": ("CsrfPolicy", "csrf_token"),
    "deadline": ("DeadlinePolicy",),
    "idempotency": (
        "IdempotencyPolicy",
        "IdempotencyStore",
        "MemoryIdempotencyStore",
        "PostgresIdempotencyStore",
    ),
    "maintenance": ("MaintenancePolicy",),
    "proxy": ("ProxyPolicy",),
    "ratelimit": (
        "MemoryRateLimitStore",
        "PostgresRateLimitStore",
        "RateLimitPolicy",
        "RateLimitStore",
        "TieredRateLimitPolicy",
        "principal_key",
    ),
    "request_decompression": ("RequestDecompressionPolicy",),
    "request_id": ("RequestIdPolicy", "request_id"),
    "security": (
        "SecurityHeadersPolicy",
        "TrustedHostPolicy",
        "WebSocketOriginPolicy",
        "csp_nonce",
    ),
    "sessions": ("SessionPolicy", "rotate_session"),
    "signed_routes": ("SignedRoutePolicy",),
    "timing": ("ServerTimingPolicy", "elapsed"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    loaded = _policy_module(module)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


_http_policy_annotate = cast(
    Callable[[Any], dict[str, Any]], HttpPolicy.__init__.__annotate__
)


def _annotate_http_policy(format: Any) -> dict[str, Any]:
    namespace = globals()
    for module, exports in _MODULE_EXPORTS.items():
        loaded = _policy_module(module)
        for export in exports:
            namespace[export] = getattr(loaded, export)
    return _http_policy_annotate(format)


HttpPolicy.__init__.__annotate__ = _annotate_http_policy
