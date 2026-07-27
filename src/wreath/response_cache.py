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

from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any

from ._orm_events import subscribe_writes
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
    invalidate_on: Iterable[Any] = (),
) -> Any:
    """Cache a route handler's response in a bounded in-process store.

    Args:
        ttl: seconds an entry stays fresh (``None`` = until evicted by capacity).
        max_entries: hard ceiling on cached responses (LRU eviction past it).
        key: builds the cache key from the request (default: method+path+query).
        methods: request methods that may be served from cache.
        store: an explicit :class:`BoundedCache` to share across handlers; a
            private one is created if omitted.
        invalidate_on: models whose writes drop this cache. A TTL is a guess --
            too short and the cache does nothing, too long and the application
            serves stale data. Naming the models makes it exact: the ORM
            announces what it wrote once the transaction commits, and the
            matching caches clear. Wreath can do this because it owns both
            layers; a bolt-on cache cannot see your writes.

    The wrapped handler gains ``.cache_store`` (the store, for ``.stats``) and
    ``.invalidate(request=None)`` — drop one key, or clear all when omitted.
    """
    if fn is not None:
        return cached(ttl=ttl, max_entries=max_entries, key=key,
                      methods=methods, store=store,
                      invalidate_on=invalidate_on)(fn)

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

        watched = frozenset(
            getattr(model, "__name__", str(model)) for model in invalidate_on
        )
        if watched:
            def _on_write(written: frozenset[str]) -> None:
                # Model-grained, not row-grained: dropping a model's responses
                # when that model is written costs one set intersection per
                # write and nothing per read. Row-grained would need a read set
                # recorded per request -- real work on the hot path to save a
                # few misses on the cold one.
                if written & watched:
                    the_store.clear()

            # Owned by the wrapper, because there is no later moment to
            # unsubscribe at: the subscription is made when the handler is
            # *decorated*. A handler registered on an app lives as long as the
            # app does and this changes nothing; one that goes out of scope
            # takes its subscription with it, instead of leaving a closure in a
            # process-global list that makes `has_subscribers()` true forever
            # and kills the session's skip-collection fast path.
            subscribe_writes(_on_write, owner=wrapper)
            wrapper.invalidated_by = watched  # ty: ignore[unresolved-attribute]

        wrapper.cache_store = the_store       # ty: ignore[unresolved-attribute]
        wrapper.invalidate = invalidate       # ty: ignore[unresolved-attribute]
        return wrapper

    return decorate
