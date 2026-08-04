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
from urllib.parse import urlencode

from ._codecs import parse_qs as _parse_qs
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

#: The headers a tag is written to, because the CDNs disagree and an application
#: should not have to care which one is in front of it. Fastly reads
#: `Surrogate-Key`, Cloudflare reads `Cache-Tag`, Akamai reads
#: `Edge-Cache-Tag`; all three ignore the others, so emitting all three costs a
#: few dozen bytes and removes a deployment-time decision from the application.
TAG_HEADERS: Final = (b"cache-tag", b"surrogate-key", b"edge-cache-tag")

#: Bytes of digest in a surrogate key. Short because it travels on every
#: response and long enough that two model names in one application do not
#: collide -- a collision here over-purges, which is safe, so this is sized for
#: header weight rather than for certainty.
_TAG_BYTES: Final = 8

#: Response types whose body is produced lazily/streamed and must never be cached.
_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")


def default_cache_key(request: Any) -> str:
    """`"GET /path?query"` — a shared, public-cache key (no per-user identity)."""
    query = request.query_string.decode("latin-1")
    base = f"{request.method} {request.path}"
    return f"{base}?{query}" if query else base


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

    def key(request: Any) -> str:
        base = f"{request.method} {request.path}"
        if not declared:
            return base
        values: dict[str, str] = {}
        for name, value in _parse_qs(request.query_string, 0):
            values.setdefault(name, value)
        # Encode each parsed component again before joining it into the cache
        # key.  Concatenating decoded values directly lets an embedded `%26`
        # and `%3D` impersonate another field and collide with a different
        # request (for example one `a=x&b=y` value versus two `a=x`, `b=y`
        # values).
        selected = urlencode(
            [(name, values[name]) for name in declared if name in values]
        )
        return f"{base}?{selected}" if selected else base

    # This helper deliberately builds a shared key with no principal.  Keep the
    # marker on the callable so `cached(key=cache_key_for(...))` and the
    # `query_params=` shorthand both enforce the authenticated-request bypass.
    key._wreath_public = True  # ty: ignore[unresolved-attribute]
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
    """A cache entry for `result`, or None if it must not be cached."""
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
            # Not defaulted to something derived, and not optional: a tag with
            # no secret is a plain hash of a name anyone can guess and then
            # purge, which turns an edge cache into something a stranger can
            # empty. Making the caller supply it is the only way that decision
            # gets made by somebody.
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
        """The surrogate keys for several models, sorted and de-duplicated.

        Sorted so a response's tag header does not change with the order the
        handler happened to declare its models in -- a header that varies for no
        reason defeats any downstream comparison of two responses.
        """
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
        """Hand one purge to the job queue without making the writer wait.

        The write has already committed, so this cannot fail the transaction --
        which is exactly why it has to be counted instead. `progress` and
        `_orm_events` use the same deliberate fire-and-forget shape; the
        difference between that and a bug is the counter below.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a synchronous test, or a write on a worker thread with
            # nothing to schedule onto. Counted rather than raised, because the
            # row is already committed and there is nothing to undo.
            self._dropped += 1
            return
        task = loop.create_task(self._enqueue(tag))
        # Held only until it completes. `create_task` keeps no strong reference
        # of its own, so a task nobody holds can be collected mid-await and the
        # purge simply never happens -- silently, which is the one failure this
        # class exists to make visible. Added before the callback is attached,
        # so there is no ordering in which `discard` runs against a set the
        # task was never in.
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    async def _enqueue(self, tag: str) -> None:
        try:
            await self._jobs.enqueue(self._task, tag, key=self._key(tag))
        except Exception:  # noqa: BLE001 - counted; the write already committed
            # Broad and counted, per the `MessageBus` reference: what `enqueue`
            # can raise is a driver error, a pool timeout or a queue that is not
            # running, and none of them may surface as a failed write.
            self._dropped += 1
            return
        self._enqueued += 1

    def counters(self) -> Any:
        """This purger's counters, for `wreath.metrics.collect`.

        `dropped` is the reason this exists. Its failure mode is *silence* --
        the edge keeps serving stale content and nothing in the application
        looks wrong -- so a number nobody scrapes is a number that cannot do
        the one job it was added for.
        """
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
    """Write the surrogate-key headers onto a `Response`, if it is one.

    A handler that returns a dict or a string is left alone: the tag has to
    travel on the response, and the response for those is built downstream by
    the serializer, which this decorator does not see. Silently doing nothing
    is right here rather than raising -- a handler returning a dict is the
    ordinary case, and the tag it did not get is visible as a missing header
    rather than as a broken route. `Response` is the documented way to tag one.
    """
    if not isinstance(result, Response):
        return
    headers = result.headers
    for name in TAG_HEADERS:
        # Appended rather than replaced: a handler that set its own
        # `Cache-Tag` has said something this decorator has no basis to
        # overrule, and both keys purging the response is the correct union.
        #
        # Skipped when this exact pair is already present, which makes this
        # idempotent -- and it has to be. `Response.__call__` recomputes
        # nothing, so returning one module-level response object from a handler
        # is a supported pattern, and a handler that does it would otherwise
        # accumulate three more headers on every cache miss, forever.
        if (name, value) not in headers:
            headers.append((name, value))


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
        return cached(ttl=ttl, max_entries=max_entries, key=key,
                      methods=methods, store=store,
                      invalidate_on=invalidate_on, query_params=query_params,
                      tags=tags)(fn)

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
    # Whether this is the shared/public key or one the caller wrote. A public
    # key cannot be used for an identified caller; see the wrapper below.
    public_key = key is default_cache_key or getattr(key, "_wreath_public", False)

    window = None if ttl is None else Duration.of(ttl).total_seconds()
    the_store: BoundedCache = store if store is not None else BoundedCache(
        max_entries=max_entries, ttl=window)

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
                # Deliberately `BaseException`, and deliberately re-raised:
                # nothing is swallowed here. Waiters are parked on this future,
                # so whatever ends the handler has to reach them -- including
                # `CancelledError`, which is the one that would otherwise leave
                # them awaiting a future that never resolves.
                future.set_exception(error)
                # Consumed here so a future nobody awaits does not log
                # "exception was never retrieved"; every waiter still sees it.
                future.exception()
                raise
            finally:
                del inflight[cache_key]
                if not future.done():
                    future.set_result(result)
            if tag_value is not None:
                # Applied before the snapshot, so a cache hit is served with the
                # same tags as the miss that filled it. A response that carried
                # its tag only on the cold path would be purgeable exactly once.
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
