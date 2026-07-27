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

import asyncio
from collections.abc import Callable, Iterable
from copy import deepcopy
from functools import wraps
from typing import Any

from ._codecs import parse_qs as _parse_qs
from ._orm_events import subscribe_writes
from .cache import BoundedCache
from .response import Response

__all__ = ["cache_key_for", "cached", "default_cache_key", "watched_name"]

#: Response types whose body is produced lazily/streamed and must never be cached.
_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")


def default_cache_key(request: Any) -> str:
    """``"GET /path?query"`` — a shared, public-cache key (no per-user identity)."""
    query = request.query_string.decode("latin-1")
    base = f"{request.method} {request.path}"
    return f"{base}?{query}" if query else base


def cache_key_for(names: Iterable[str]) -> Callable[[Any], str]:
    """A public key built from *declared* query parameters only.

    :func:`default_cache_key` puts the whole query string in the key, so every
    distinct one is a distinct entry -- and an unauthenticated caller varying a
    parameter the handler ignores (``?_=1``, ``?utm_source=…``) fills the store
    and evicts everything real. Naming the parameters that actually change the
    answer bounds the keyspace to what the handler can distinguish.

    Parameters are read in the declared order, so the key does not change with
    the order the client happened to send them in; repeats keep the first value,
    matching how the binding layer reads them.
    """
    declared = tuple(names)

    def key(request: Any) -> str:
        base = f"{request.method} {request.path}"
        if not declared:
            return base
        values: dict[str, str] = {}
        for name, value in _parse_qs(request.query_string, 0):
            values.setdefault(name, value)
        selected = "&".join(
            f"{name}={values[name]}" for name in declared if name in values
        )
        return f"{base}?{selected}" if selected else base

    return key


def _cacheable_response(response: Response) -> bool:
    if type(response).__name__ in _UNCACHEABLE_BODY:
        return False
    # 2xx only. A 3xx used to qualify as "not an error", but a redirect is
    # exactly the response whose `Location` is most often per-caller -- an OAuth
    # hand-off, a post-login bounce, a signed download URL -- and serving one
    # caller's Location to the next is the same leak a cached body would be.
    if not (200 <= response.status < 300):
        return False
    for name, value in response.headers:
        lname = name.lower()
        if lname == b"set-cookie":
            return False   # per-user cookie must never be shared
        if lname == b"vary" and value.strip():
            # `Vary` names request headers the answer depends on, and this
            # cache's key is method+path+query -- it cannot represent them. One
            # entry would be served to every variant: the msgpack body from
            # `serialize()` handed to a JSON client, one locale's copy to every
            # locale, one caller's `Authorization`-shaped answer to the next.
            return False
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
    if isinstance(result, (str, bytes)):
        # Immutable: the same object is safe to hand out repeatedly.
        return ("value", result)
    if isinstance(result, (dict, list)):
        # Copied on the way in *and* on the way out. A cache entry is shared by
        # every later caller, so storing the handler's own object means one
        # mutation anywhere -- the handler keeping a reference, a serializer
        # normalising in place, a caller editing what it was given -- silently
        # rewrites what everyone else is served.
        return ("copy", deepcopy(result))
    return None


def watched_name(model: Any) -> str:
    """The name a model is announced under by the ORM.

    The bare class name, matching what `Session._collect_written` publishes, so
    a model may equally be named as a string by a caller that cannot import it.
    Two models sharing a class name are one entry here; see
    `Session._collect_written` for why that is accepted.
    """
    if isinstance(model, str):
        return model
    return getattr(model, "__name__", None) or str(model)


def _copy_if_mutable(value: Any) -> Any:
    """A private copy of a shared result, for the same reason `_snapshot` copies."""
    return deepcopy(value) if isinstance(value, (dict, list)) else value


def _revive(entry: tuple[str, Any]) -> Any:
    kind, payload = entry
    if kind == "response":
        status, headers, body = payload
        # A fresh headers list per hit, so downstream middleware mutations do
        # not poison the shared cache entry.
        return Response(body, status=status, headers=list(headers))
    if kind == "copy":
        return deepcopy(payload)
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
    query_params: Iterable[str] | None = None,
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
                      invalidate_on=invalidate_on, query_params=query_params)(fn)

    if query_params is not None:
        if key is not default_cache_key:
            raise ValueError("pass either `key` or `query_params`, not both")
        key = cache_key_for(query_params)
    # Whether this is the shared/public key or one the caller wrote. A public
    # key cannot be used for an identified caller; see the wrapper below.
    public_key = key is default_cache_key or getattr(key, "_wreath_public", False)

    the_store: BoundedCache = store if store is not None else BoundedCache(
        max_entries=max_entries, ttl=ttl)

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        # One in-flight computation per key. Without it, every request that
        # arrives while a cold key is being computed runs the handler too -- so
        # the moment an entry expires, the expensive rollup this decorator exists
        # to avoid runs once per concurrent caller. The waiters share the first
        # result; a handler that raises fails them all and leaves nothing cached,
        # so the next request retries rather than inheriting a wedged key.
        inflight: dict[str, asyncio.Future[Any]] = {}

        @wraps(handler)
        async def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            if request.method not in methods:
                return await handler(request, *args, **kwargs)
            if public_key and getattr(request, "identity", None) is not None:
                # The default key is a *shared* key: it carries no principal, so
                # an entry stored for one caller would be served to the next.
                # The docstring said to pass a `key` that includes the
                # principal; nothing enforced it, and the failure is silent and
                # cross-user. An identified caller therefore bypasses the cache
                # entirely rather than being served -- or stored -- under it.
                return await handler(request, *args, **kwargs)
            cache_key = key(request)
            hit = the_store.get(cache_key)
            if hit is not None:
                return _revive(hit)
            pending = inflight.get(cache_key)
            if pending is not None:
                # Somebody else is already computing this key. Awaiting their
                # result is both cheaper and more correct than racing them.
                return _copy_if_mutable(await asyncio.shield(pending))
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            inflight[cache_key] = future
            try:
                result = await handler(request, *args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
                # Consumed here so a future nobody awaits does not log
                # "exception was never retrieved"; every waiter still sees it.
                future.exception()
                raise
            finally:
                del inflight[cache_key]
                if not future.done():
                    future.set_result(result)
            entry = _snapshot(result)
            if entry is not None:
                the_store.set(cache_key, entry)
            return result

        def invalidate(request: Any = None) -> None:
            if request is None:
                the_store.clear()
            else:
                the_store.delete(key(request))

        watched = frozenset(watched_name(model) for model in invalidate_on)
        if watched:
            def _on_write(written: frozenset[str]) -> None:
                # Model-grained, not row-grained: dropping a model's responses
                # when that model is written costs one set intersection per
                # write and nothing per read. Row-grained would need a read set
                # recorded per request -- real work on the hot path to save a
                # few misses on the cold one.
                #
                # Clears the *whole* store, including entries other handlers
                # sharing it put there. That coupling is the documented price of
                # sharing a store -- one budget and one invalidation surface --
                # and `tests/test_cache_invalidation.py` pins it. Give a handler
                # its own store when it should not be swept by its neighbours.
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
