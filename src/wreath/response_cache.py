"""Cache a handler's response in a small, bounded, in-process store.

No Redis, no adapter, no background worker — just the evictable
:class:`~wreath.cache.BoundedCache`, so the cache is something you can size, TTL,
inspect (``.cache_store.stats``), and clear (``.invalidate()``) with certainty.

    from wreath.response_cache import cached

    @app.get("/reports/summary")
    @cached(ttl=30)                     # 30s, shared across callers
    async def summary(request):
        return await expensive_rollup()

Safe by default — a response is **not** cached when it would leak or mislead:

* only the methods you allow (``GET`` by default),
* only success statuses (< 400),
* never a response that sets a cookie (that would replay one user's cookie to
  everyone),
* never one marked ``Cache-Control: no-store`` or ``private``.

The default key is ``method + path + query`` — i.e. a **shared/public** cache. If
a response depends on who is asking, pass a ``key`` that includes the principal
(e.g. ``lambda r: f"{r.identity.id}:{r.path}"``) or don't cache it.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from .cache import BoundedCache
from .response import Response

__all__ = ["cached", "default_cache_key"]

#: Response types whose body is produced lazily/streamed and must never be cached.
_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")


def default_cache_key(request: Any) -> str:
    """``"GET /path?query"`` — a shared, public-cache key (no per-user identity)."""
    query = request.query_string.decode("latin-1")
    base = f"{request.method} {request.path}"
    return f"{base}?{query}" if query else base


def _cacheable_response(response: Response) -> bool:
    if type(response).__name__ in _UNCACHEABLE_BODY:
        return False
    if response.status >= 400:
        return False
    for name, value in response.headers:
        lname = name.lower()
        if lname == b"set-cookie":
            return False   # per-user cookie must never be shared
        if lname == b"cache-control":
            directives = value.lower()
            if b"no-store" in directives or b"private" in directives:
                return False
    return True


def _snapshot(result: Any) -> tuple[str, Any] | None:
    """A cache entry for ``result``, or None if it must not be cached."""
    if isinstance(result, Response):
        if not _cacheable_response(result):
            return None
        # Body is immutable bytes (shareable); headers are copied on revive.
        return ("response", (result.status, tuple(result.headers), result.body))
    if isinstance(result, (str, bytes, dict, list)):
        return ("value", result)
    return None


def _revive(entry: tuple[str, Any]) -> Any:
    kind, payload = entry
    if kind == "response":
        status, headers, body = payload
        # A fresh headers list per hit, so downstream middleware mutations do
        # not poison the shared cache entry.
        return Response(body, status=status, headers=list(headers))
    return payload


def cached(
    fn: Callable[..., Any] | None = None,
    *,
    ttl: float | None = None,
    max_entries: int = 1024,
    key: Callable[[Any], str] = default_cache_key,
    methods: tuple[str, ...] = ("GET",),
    store: BoundedCache | None = None,
) -> Any:
    """Cache a route handler's response in a bounded in-process store.

    Args:
        ttl: seconds an entry stays fresh (``None`` = until evicted by capacity).
        max_entries: hard ceiling on cached responses (LRU eviction past it).
        key: builds the cache key from the request (default: method+path+query).
        methods: request methods that may be served from cache.
        store: an explicit :class:`BoundedCache` to share across handlers; a
            private one is created if omitted.

    The wrapped handler gains ``.cache_store`` (the store, for ``.stats``) and
    ``.invalidate(request=None)`` — drop one key, or clear all when omitted.
    """
    if fn is not None:
        return cached(ttl=ttl, max_entries=max_entries, key=key,
                      methods=methods, store=store)(fn)

    the_store: BoundedCache = store if store is not None else BoundedCache(
        max_entries=max_entries, ttl=ttl)

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        async def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            if request.method not in methods:
                return await handler(request, *args, **kwargs)
            cache_key = key(request)
            hit = the_store.get(cache_key)
            if hit is not None:
                return _revive(hit)
            result = await handler(request, *args, **kwargs)
            entry = _snapshot(result)
            if entry is not None:
                the_store.set(cache_key, entry)
            return result

        def invalidate(request: Any = None) -> None:
            if request is None:
                the_store.clear()
            else:
                the_store.delete(key(request))

        wrapper.cache_store = the_store       # ty: ignore[unresolved-attribute]
        wrapper.invalidate = invalidate       # ty: ignore[unresolved-attribute]
        return wrapper

    return decorate
