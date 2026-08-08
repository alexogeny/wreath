"""Custom request hooks hand-written by applications.

`base` holds the contracts: `MiddlewareHooks` for a route-scoped middleware,
`PipelineHooks` for one placed at the named pipeline boundaries, `MiddlewareRoute`
for the compile-time predicate, and `MiddlewareTape` for the compiled artifact.
Read `wreath.middleware.base` for how a hook is dispatched and exactly when an
`after` hook runs.

Standard behavior belongs to first-class configuration in `wreath.policy` and
never enters this tape. This package contains only the hook protocol and tape
compiler for application-specific hooks.
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

__all__ = [
    "CallNext",
    "Middleware",
    "MiddlewareHooks",
    "MiddlewareRoute",
    "MiddlewareTape",
    "PipelineHooks",
    "ResponseValue",
]
