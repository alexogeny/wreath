"""The middleware Wreath ships, and the contracts every middleware is written to.

`base` holds the contracts: `MiddlewareHooks` for a route-scoped middleware,
`PipelineHooks` for one placed at the named pipeline boundaries, `MiddlewareRoute`
for the compile-time predicate, and `MiddlewareTape` for the compiled artifact.
Read `wreath.middleware.base` for how a hook is dispatched and exactly when an
`after` hook runs.

Everything else here is a concrete middleware, mounted with
`Wreath.add_middleware`. Most carry `global_scope = True`, which registers them
around routing rather than inside a route, so they also cover misses, static
files, and error responses. Two do not: `SessionMiddleware` and
`TieredRateLimitMiddleware` are route middleware, the latter because it is keyed
on `request.identity`, which does not exist until route authorization has run.

Ordering is by `priority`, ascending, ties broken by registration order. The
ones that must run first say so in their own documentation --
`ProxyHeadersMiddleware` corrects the scheme, host, and client that everything
downstream reads, so it belongs at a negative priority ahead of
`TrustedHostMiddleware`, `CSRFMiddleware`, and `SecurityHeadersMiddleware`.
"""

from .base import (
    CallNext,
    Middleware,
    MiddlewareHooks,
    MiddlewareRoute,
    MiddlewareTape,
    PipelineHooks,
    ResponseValue,
)
from .cache import CacheControlMiddleware
from .compression import CompressionMiddleware
from .cors import CORSMiddleware
from .csrf import CSRFMiddleware, csrf_token
from .idempotency import (
    IdempotencyMiddleware,
    IdempotencyStore,
    MemoryIdempotencyStore,
    PostgresIdempotencyStore,
)
from .proxy import ProxyHeadersMiddleware
from .ratelimit import (
    MemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimitMiddleware,
    RateLimitStore,
    TieredRateLimitMiddleware,
    principal_key,
)
from .request_id import RequestIDMiddleware, request_id
from .security import SecurityHeadersMiddleware, TrustedHostMiddleware
from .sessions import SessionMiddleware
from .timing import ServerTimingMiddleware, elapsed

__all__ = [
    "CSRFMiddleware",
    "CORSMiddleware",
    "CacheControlMiddleware",
    "CompressionMiddleware",
    "CallNext",
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "PostgresIdempotencyStore",
    "MemoryRateLimitStore",
    "Middleware",
    "MiddlewareHooks",
    "MiddlewareRoute",
    "MiddlewareTape",
    "PipelineHooks",
    "PostgresRateLimitStore",
    "ProxyHeadersMiddleware",
    "RateLimitMiddleware",
    "RateLimitStore",
    "TieredRateLimitMiddleware",
    "principal_key",
    "RequestIDMiddleware",
    "ResponseValue",
    "SecurityHeadersMiddleware",
    "ServerTimingMiddleware",
    "SessionMiddleware",
    "TrustedHostMiddleware",
    "csrf_token",
    "elapsed",
    "request_id",
]
