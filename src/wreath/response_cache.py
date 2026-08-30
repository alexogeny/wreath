"""Cache a handler's response in a small, bounded, in-process store.

No Redis, no adapter, no background worker — just the evictable
`BoundedCache`, so the cache is something you can size, TTL,
inspect (`.cache_store.stats`), and clear (`.invalidate()`) with certainty.

    from wreath.response_cache import cached

    @app.get("/reports/summary")
    @cached(ttl=30)                     # 30s, shared across callers
    async def summary(request):
        return await expensive_rollup()

Safe by default — a response is **not** cached when it would leak or mislead:

* only the methods you allow (`GET` by default),
* only success statuses (< 400),
* never a response that sets a cookie (that would replay one user's cookie to
  everyone),
* never one marked `Cache-Control: no-store` or `private`.

The default key is `method + path + query` — i.e. a **shared/public** cache. If
a response depends on who is asking, pass a `key` that includes the principal
(e.g. `lambda r: f"{r.identity.id}:{r.path}"`) or don't cache it.

## The same signal, one layer out

The invalidation above is exact because Wreath owns both halves: the ORM knows
what committed, the cache knows what it holds. A CDN in front of the application
is the same problem displaced by one hop, and it is normally solved by hand —
somebody tags responses, somebody else remembers to purge, and the two drift
until a stale page outlives an incident.

`Tags` and `CDNPurge` close that loop from the same declaration.
The models a handler names in `invalidate_on` are what the response *read*; the
models the ORM announces are what a transaction *wrote*; a tag derived from the
first and a purge triggered by the second make correct edge invalidation a
property of the schema rather than of anybody's discipline. See
the cache-tags guide.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from hashlib import blake2b
from typing import Any, Final

from ._native import _core
from ._orm_events import subscribe_writes
from .cache import BoundedCache
from .response import Response
from .temporal import Duration

__all__ = [
    "CDNPurge",
    "TAG_HEADERS",
    "Tags",
    "cache_key_for",
    "cached",
    "default_cache_key",
    "watched_name",
]

#: Compatible purge-tag headers for Fastly, Cloudflare, and Akamai.
TAG_HEADERS: Final = (b"cache-tag", b"surrogate-key", b"edge-cache-tag")

#: Digest bytes carried in each surrogate key.
_TAG_BYTES: Final = 8

#: Response types whose body is produced lazily/streamed and must never be cached.
_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")


def default_cache_key(request: Any) -> str:
    """`"GET /path?query"` — a shared, public-cache key (no per-user identity)."""
    query = request.query_string
    base = f"{request.method} {request.path}"
    return f"{base}?{query.decode('latin-1')}" if query else base


def cache_key_for(names: Iterable[str]) -> Callable[[Any], str]:
    """A public key built from *declared* query parameters only.

    `default_cache_key` puts the whole query string in the key, so every
    distinct one is a distinct entry -- and an unauthenticated caller varying a
    parameter the handler ignores (`?_=1`, `?utm_source=…`) fills the store
    and evicts everything real. Naming the parameters that actually change the
    answer bounds the keyspace to what the handler can distinguish.

    Parameters are read in the declared order, so the key does not change with
    the order the client happened to send them in; repeats keep the first value,
    matching how the binding layer reads them.
    """
    declared = tuple(names)
    for index, name in enumerate(declared):
        if not isinstance(name, str):
            raise TypeError(f"cache_key_for names[{index}] must be str, got {type(name).__name__}")

    def key(request: Any) -> str:
        base = f"{request.method} {request.path}"
        if not declared:
            return base
        return _core.cache_key_selected(
            request.method, request.path, request.query_string, declared
        )

    # Mark keys that contain no principal for the authenticated-request guard.
    key._wreath_public = True  # ty: ignore[unresolved-attribute]
    return key


def _cacheable_response(response: Response) -> bool:
    if type(response).__name__ in _UNCACHEABLE_BODY:
        return False
    # Redirect locations are commonly caller-specific, so only 2xx is cacheable.
    if not (200 <= response.status < 300):
        return False
    # The native scan refuses Set-Cookie, non-empty Vary, and private/no-store
    # directives without allocating a lowercase header per field.
    return _core.cacheable_headers(response.headers)


def _snapshot(result: Any) -> tuple[str, Any] | None:
    """A cache entry for `result`, or None if it must not be cached."""
    if isinstance(result, Response):
        if not _cacheable_response(result):
            return None
        return ("response", (result.status, tuple(result.headers), result.body))
    if isinstance(result, (str, bytes)):
        return ("value", result)
    if isinstance(result, (dict, list)):
        # Isolate mutable results from both the handler and later callers.
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


@dataclass(frozen=True, slots=True)
class Tags:
    """How a model name becomes a surrogate key on a response.

    **The keys are hashed, and that is not decoration.** A surrogate key travels
    in a response header, so anyone who can read one learns whatever the key
    spells out -- table names, and with row identity in it, which rows a page was
    built from and therefore that a given row exists. Deriving them through a
    keyed digest means an edge can still match a purge to a response while a
    reader learns nothing but that two responses share a dependency.

    `secret` is the key. It must be the same in every worker and across a
    deploy that does not also purge everything -- a rotated secret renames every
    tag, so the old ones become unpurgeable until they expire. Rotate it the way
    you would a cache version: deliberately, and with a full purge behind it.

    `prefix` distinguishes several applications sharing one CDN account, where
    an unprefixed tag from one would purge the other's responses.
    """

    secret: bytes = b""
    prefix: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.secret, str):
            raise TypeError("Tags(secret=...) must be bytes, not str")
        if not self.secret:
            raise ValueError(
                "Tags(secret=...) is required: an unkeyed surrogate key can be "
                "computed by anyone who knows your model names, and computing "
                "it is all it takes to purge that tag"
            )

    def key(self, model: Any) -> str:
        """The surrogate key for one model."""
        name = watched_name(model)
        digest = blake2b(
            f"{self.prefix}\x00{name}".encode(), key=self.secret, digest_size=_TAG_BYTES
        )
        return digest.hexdigest()

    def keys(self, models: Iterable[Any]) -> tuple[str, ...]:
        """Sorted, de-duplicated surrogate keys for several models."""
        return tuple(sorted({self.key(model) for model in models}))

    def header_value(self, models: Iterable[Any]) -> bytes:
        """The header body for these models: space-separated keys.

        Space rather than comma, which is what Fastly and Cloudflare both parse
        and what Akamai accepts.
        """
        return " ".join(self.keys(models)).encode("ascii")


class CDNPurge:
    """Purge an edge cache when the rows behind a response move.

    `purge` is called with the surrogate keys to drop. It is **never called
    inline on the commit path**: the ORM announces a write after the transaction
    commits, and a slow CDN API reached from there would turn a slow edge into a
    slow write. Instead the call is handed to `wreath.jobs`, which is
    durable, retried and observable -- a purge that fails at three in the morning
    is a job somebody can see and replay, not a lost signal.

    `key` is what makes a burst of writes one purge: it is passed to
    `enqueue` as the deduplication key, so a thousand rows written in a loop
    enqueue one purge per tag rather than a thousand. That is the difference
    between an invalidation and a denial of service against your own CDN.

    Every degradation is counted. `dropped()` is purges that could not be
    enqueued at all -- the queue was down, the loop was gone -- and it exists
    because the failure mode here is silence: the edge keeps serving the old
    page and nothing in the application looks wrong.
    """

    __slots__ = ("_dropped", "_enqueued", "_jobs", "_key", "_tags", "_task", "_watching")

    def __init__(
        self,
        jobs: Any,
        *,
        tags: Tags,
        task: str = "wreath_cdn_purge",
        key: Callable[[str], str] | None = None,
    ) -> None:
        self._jobs = jobs
        self._tags = tags
        self._task = task
        self._key = key or (lambda tag: f"purge:{tag}")
        self._dropped = 0
        self._enqueued = 0
        self._watching: frozenset[str] = frozenset()

    @property
    def tags(self) -> Tags:
        return self._tags

    @property
    def task(self) -> str:
        """The job name a purge is enqueued under."""
        return self._task

    @property
    def watching(self) -> frozenset[str]:
        """The model names whose writes trigger a purge."""
        return self._watching

    def enqueued(self) -> int:
        """Purge jobs successfully enqueued. Never resets."""
        return self._enqueued

    def dropped(self) -> int:
        """Purges that could not be enqueued, and were therefore lost.

        A number that is not zero means the edge is serving something the
        database no longer says. It never resets, so a doctor check or a metric
        can watch its rate.
        """
        return self._dropped

    def watch(self, *models: Any) -> None:
        """Purge these models' tags whenever the ORM says they were written.

        Registered against the process-global write signal, so a `CDNPurge` is
        an application-lifetime object. Call it once during startup.
        """
        self._watching = self._watching | {watched_name(model) for model in models}
        subscribe_writes(self._on_write)

    def _on_write(self, written: frozenset[str]) -> None:
        """Enqueue one purge per written model this purge watches.

        Model-grained, matching the signal. A row-grained purge would need the
        response's read set recorded per request, which is the trade
        `wreath._orm_events` already declined for the local cache and declines
        again here for the same reason.
        """
        moved = written & self._watching
        if not moved:
            return
        for name in sorted(moved):
            self._schedule(self._tags.key(name))

    def _schedule(self, tag: str) -> None:
        """Hand one purge to the job queue without delaying the committed write."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A committed write has no transaction left to fail.
            self._dropped += 1
            return
        task = loop.create_task(self._enqueue(tag))
        # Hold the task strongly until its callback runs.
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _enqueue(self, tag: str) -> None:
        try:
            await self._jobs.enqueue(self._task, tag, key=self._key(tag))
        except Exception:  # noqa: BLE001 - counted; the write already committed
            # Purge infrastructure failure cannot reverse the committed write.
            self._dropped += 1
            return
        self._enqueued += 1

    def counters(self) -> Any:
        """This purger's counters, for `wreath.metrics.collect`."""
        from .metrics import Counters

        return Counters(
            subsystem="cdn_purge",
            instance=self._task,
            values={"enqueued": self._enqueued, "dropped": self._dropped},
        )

    def __repr__(self) -> str:
        return (
            f"<CDNPurge task={self._task!r} watching={sorted(self._watching)} "
            f"enqueued={self._enqueued} dropped={self._dropped}>"
        )


#: In-flight purge tasks, held so the event loop does not collect one mid-await.
_PENDING: set[asyncio.Task[None]] = set()


def _apply_tags(result: Any, value: bytes) -> None:
    """Write surrogate-key headers when the handler returned a `Response`."""
    if not isinstance(result, Response):
        return
    headers = result.headers
    for name in TAG_HEADERS:
        # Preserve handler tags and keep reused response objects idempotent.
        if (name, value) not in headers:
            headers.append((name, value))


def _copy_if_mutable(value: Any) -> Any:
    return deepcopy(value) if isinstance(value, (dict, list)) else value


def _revive(entry: tuple[str, Any]) -> Any:
    kind, payload = entry
    if kind == "response":
        status, headers, body = payload
        return Response(body, status=status, headers=list(headers))
    if kind == "copy":
        return deepcopy(payload)
    return payload


def cached(
    fn: Callable[..., Any] | None = None,
    *,
    ttl: Any = None,
    max_entries: int = 1024,
    key: Callable[[Any], str] = default_cache_key,
    methods: tuple[str, ...] = ("GET",),
    store: BoundedCache | None = None,
    invalidate_on: Iterable[Any] = (),
    query_params: Iterable[str] | None = None,
    tags: Tags | None = None,
) -> Any:
    """Cache a route handler's response in a bounded in-process store.

    Args:
        ttl: seconds an entry stays fresh (`None` = until evicted by capacity).
        max_entries: hard ceiling on cached responses (LRU eviction past it).
        key: builds the cache key from the request (default: method+path+query).
        methods: request methods that may be served from cache.
        store: an explicit `BoundedCache` to share across handlers; a
            private one is created if omitted.
        invalidate_on: models whose writes drop this cache. A TTL is a guess --
            too short and the cache does nothing, too long and the application
            serves stale data. Naming the models makes it exact: the ORM
            announces what it wrote once the transaction commits, and the
            matching caches clear. Wreath can do this because it owns both
            layers; a bolt-on cache cannot see your writes.
        tags: emit a surrogate key per `invalidate_on` model, so a CDN in
            front of this handler can be purged by the same declaration that
            invalidates the local cache. Requires `invalidate_on`: a tag
            derived from nothing names nothing, and a response tagged with
            nothing is one no purge will ever reach — which is worse than an
            untagged response, because it looks tagged.

    The wrapped handler gains `.cache_store` (the store, for `.stats`) and
    `.invalidate(request=None)` — drop one key, or clear all when omitted.
    """
    if fn is not None:
        return cached(
            ttl=ttl,
            max_entries=max_entries,
            key=key,
            methods=methods,
            store=store,
            invalidate_on=invalidate_on,
            query_params=query_params,
            tags=tags,
        )(fn)

    if query_params is not None:
        if key is not default_cache_key:
            raise ValueError("pass either `key` or `query_params`, not both")
        key = cache_key_for(query_params)
    watched = frozenset(watched_name(model) for model in invalidate_on)
    if tags is not None and not watched:
        raise ValueError(
            "cached(tags=...) needs invalidate_on=[...]: the surrogate key is "
            "derived from the models the response reads, so with none declared "
            "there is no tag to emit and no purge could ever reach this response"
        )
    tag_value = tags.header_value(watched) if tags is not None else None
    public_key = key is default_cache_key or getattr(key, "_wreath_public", False)

    window = None if ttl is None else Duration.of(ttl).total_seconds()
    the_store: BoundedCache = (
        store if store is not None else BoundedCache(max_entries=max_entries, ttl=window)
    )

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        # Share one in-flight computation per cold key.
        inflight: dict[str, asyncio.Future[Any]] = {}

        @wraps(handler)
        async def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            if request.method not in methods:
                return await handler(request, *args, **kwargs)
            if public_key and getattr(request, "identity", None) is not None:
                # A principal must never read or populate a shared key.
                return await handler(request, *args, **kwargs)
            cache_key = key(request)
            hit = the_store.get(cache_key)
            if hit is not None:
                return _revive(hit)
            pending = inflight.get(cache_key)
            if pending is not None:
                return _copy_if_mutable(await asyncio.shield(pending))
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            inflight[cache_key] = future
            try:
                result = await handler(request, *args, **kwargs)
            except BaseException as error:
                # Every termination, including cancellation, must wake waiters.
                future.set_exception(error)
                # Prevent an unobserved-future warning when there are no waiters.
                future.exception()
                raise
            finally:
                del inflight[cache_key]
                if not future.done():
                    future.set_result(result)
            if tag_value is not None:
                # Snapshot the same purge tags served on the cold response.
                _apply_tags(result, tag_value)
            entry = _snapshot(result)
            if entry is not None:
                the_store.set(cache_key, entry)
            return result

        def invalidate(request: Any = None) -> None:
            if request is None:
                the_store.clear()
            else:
                the_store.delete(key(request))

        if watched:

            def _on_write(written: frozenset[str]) -> None:
                # A shared store intentionally has one model-grained invalidation surface.
                if written & watched:
                    the_store.clear()

            # Wrapper ownership removes the subscription with the decorated handler.
            subscribe_writes(_on_write, owner=wrapper)
            wrapper.invalidated_by = watched  # ty: ignore[unresolved-attribute]

        wrapper.cache_store = the_store  # ty: ignore[unresolved-attribute]
        wrapper.invalidate = invalidate  # ty: ignore[unresolved-attribute]
        return wrapper

    return decorate
