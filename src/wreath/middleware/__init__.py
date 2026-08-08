"""Custom request hooks and the few compositional middleware Wreath ships.

`base` holds the contracts: `MiddlewareHooks` for a route-scoped middleware,
`PipelineHooks` for one placed at the named pipeline boundaries, `MiddlewareRoute`
for the compile-time predicate, and `MiddlewareTape` for the compiled artifact.
Read `wreath.middleware.base` for how a hook is dispatched and exactly when an
`after` hook runs.

HTTP security, forwarding, correlation, timing, CORS, CSRF, and ingress rate
limits are first-class configuration in `wreath.policy`; they are compiled
by the framework and are not middleware. This package is for custom hooks and
features whose semantics genuinely wrap a selected handler, such as sessions,
compression, caching, and idempotency.
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
from .idempotency import (
    IdempotencyMiddleware,
    IdempotencyStore,
    MemoryIdempotencyStore,
    PostgresIdempotencyStore,
)
from .sessions import SessionMiddleware

__all__ = [
    "CacheControlMiddleware",
    "CompressionMiddleware",
    "CallNext",
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "PostgresIdempotencyStore",
    "Middleware",
    "MiddlewareHooks",
    "MiddlewareRoute",
    "MiddlewareTape",
    "PipelineHooks",
    "ResponseValue",
    "SessionMiddleware",
]
