"""Resolve a value at most once per request, cached on `request.state`.

Four subsystems need the same guarantee and each wrote it: `_auth.facts.SetFact`
for a Cedar context key, `_auth.geofence.resolve_precision` for a precision
ladder, `wreath.signatures` for a verification outcome. The guarantee is not a
performance one -- it is that **one request gets one answer**. A route behind
several policies asks the authorizer once per policy, and a fact re-resolved per
policy could answer differently inside one decision; a `permit` and a `forbid`
disagreeing about the same caller is not a decision anybody wrote. A list
response asks a precision ladder for every row, and `rows x rungs` authorization
calls is the same bug wearing a cost.

## The trap this exists to close

Every one of those sites reached for `request.state.get(slot)` and compared
against `None`, which is wrong whenever `None` is a legitimate answer -- and in
two of the three it is. `geofence` noticed and worked around it by *boxing* the
answer in a one-tuple, with an unboxing helper beside it; `signatures` and
`SetFact` happen to be safe only because their answers are a dataclass and a
`frozenset`, which is a property of today's return types rather than of the
caching rule.

`State.get` takes a `default`, so the fix is smaller than the workaround: read
with a private sentinel and absence becomes distinguishable from a stored
`None`. That is the whole primitive, and it is here rather than copied a fourth
time because the copies were already diverging in exactly the way that decides
whether a coordinate reaches a caller entitled to none.

Slots are the caller's to choose and are not namespaced here -- they are private
attribute names on a per-request namespace, and the modules that use them
already derive stable ones. A logical value with several providers uses the
keyed variants, which keep the same absence and `None` rules without making
each subsystem rebuild a provider-to-value dictionary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: Distinct from every value a resolver may legitimately return, including
#: `None` and every falsy builtin. Private: a caller comparing against it is
#: reimplementing this module.
_MISSING: Any = object()


def resolve_once[T](request: Any, slot: str, resolve: Callable[[], T]) -> T:
    """`resolve()`'s answer for this request, computed at most once.

    Args:
        request: anything carrying a `state` namespace.
        slot: the attribute name to cache under. Stable per logical value; a
            slot derived from arguments must include everything that can change
            the answer, or the second caller gets the first one's result.
        resolve: called with no arguments on the first read of `slot` in this
            request, and never again. Bind whatever it needs in a closure.

    Returns:
        The resolved value, which may be `None` -- a cached `None` is returned
        as a hit, not recomputed.
    """
    state = request.state
    cached = state.get(slot, _MISSING)
    if cached is not _MISSING:
        return cached
    resolved = resolve()
    state.__setattr__(slot, resolved)
    return resolved


async def resolve_once_async[T](
    request: Any, slot: str, resolve: Callable[[], Awaitable[T]]
) -> T:
    """`resolve_once` for a resolver that must await.

    The cache read stays synchronous, so a hit does not yield to the event loop
    and cannot interleave with another task resolving the same slot. Two
    concurrent *misses* on one request would both resolve -- which no caller
    here can produce, because a request's own work is sequential -- and the
    second simply overwrites with an equal answer.
    """
    state = request.state
    cached = state.get(slot, _MISSING)
    if cached is not _MISSING:
        return cached
    resolved = await resolve()
    state.__setattr__(slot, resolved)
    return resolved


def resolve_keyed_once[K, T](
    request: Any,
    slot: str,
    key: K,
    resolve: Callable[[], T],
) -> T:
    """Resolve one key in a request-local dictionary at most once."""
    state = request.state
    cache = state.get(slot, _MISSING)
    if cache is not _MISSING:
        if not isinstance(cache, dict):
            raise TypeError(f"request.state.{slot} must be a keyed request cache")
        if key in cache:
            return cache[key]
    resolved = resolve()
    if cache is _MISSING:
        cache = {}
        state.__setattr__(slot, cache)
    cache[key] = resolved
    return resolved


async def resolve_keyed_once_async[K, T](
    request: Any,
    slot: str,
    key: K,
    resolve: Callable[[], Awaitable[T]],
) -> T:
    """`resolve_keyed_once` for a resolver that must await."""
    state = request.state
    cache = state.get(slot, _MISSING)
    if cache is not _MISSING:
        if not isinstance(cache, dict):
            raise TypeError(f"request.state.{slot} must be a keyed request cache")
        if key in cache:
            return cache[key]
    resolved = await resolve()
    if cache is _MISSING:
        cache = {}
        state.__setattr__(slot, cache)
    cache[key] = resolved
    return resolved


__all__ = [
    "resolve_keyed_once",
    "resolve_keyed_once_async",
    "resolve_once",
    "resolve_once_async",
]
