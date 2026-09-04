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

* only the methods you allow (`GET` and `QUERY` by default),
* only success statuses (< 400),
* never a response that sets a cookie (that would replay one user's cookie to
  everyone),
* never one marked `Cache-Control: no-store` or `private`.

The default key is `scheme + authority + method + path + query` — i.e. a
**shared/public** cache within one request authority, and authenticated callers
bypass it. A custom `key` is private: Wreath scopes it by tenant, principal
namespace/type/id, scheme, and authority before it reaches the store.

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
from dataclasses import dataclass, field
from functools import wraps
from hashlib import blake2b
from typing import Any, Final

from ._native import _core
from ._orm_events import subscribe_writes, unsubscribe_writes
from ._structured_fields import Item, Token, serialize_list
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

_CACHE_GROUPS: Final = b"cache-groups"
_CACHE_GROUP_INVALIDATION: Final = b"cache-group-invalidation"
_CACHE_STATUS: Final = b"cache-status"
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "QUERY", "TRACE"})
_CACHEABLE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "QUERY"})

#: Digest bytes carried in each surrogate key.
_TAG_BYTES: Final = 8

#: Response types whose body is produced lazily/streamed and must never be cached.
_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")


class _AmbiguousAuthority(ValueError):
    pass


def default_cache_key(request: Any) -> str:
    """A shared public key bound to the request's scheme and Host authority."""
    query = request.query_string
    base = f"{request.method} {_request_path(request)}"
    resource = f"{base}?{query.decode('latin-1')}" if query else base
    return f"{_authority_prefix(request)}{resource}"


def _request_path(request: Any) -> str:
    context = getattr(request, "_context", None)
    raw_path = getattr(context, "raw_path", None) if context is not None else None
    if raw_path is None:
        scope = getattr(request, "_scope", None)
        if not isinstance(scope, dict):
            scope = getattr(request, "scope", None)
        raw_path = scope.get("raw_path") if isinstance(scope, dict) else None
    return raw_path.decode("latin-1") if isinstance(raw_path, bytes) else request.path


def _authority_prefix(request: Any) -> str:
    single = getattr(request, "_single_header", None)
    if callable(single):
        try:
            raw = single(b"host")
        except ValueError as error:
            raise _AmbiguousAuthority("request Host occurs more than once") from error
        host = None if raw is None else raw.decode("latin-1")
    else:
        raw_headers = getattr(request, "headers", None)
        if isinstance(raw_headers, (list, tuple)):
            found = None
            for name, value in raw_headers:
                if name.lower() != b"host":
                    continue
                if found is not None:
                    raise _AmbiguousAuthority("request Host occurs more than once")
                found = value
            host = None if found is None else found.decode("latin-1")
        else:
            header = getattr(request, "header", None)
            host = header("host") if callable(header) else None
    if not host:
        scope = getattr(request, "_scope", None)
        if not isinstance(scope, dict):
            scope = getattr(request, "scope", None)
        server = scope.get("server") if isinstance(scope, dict) else None
        if server is None:
            return ""
        server_host, port = server
        host = str(server_host) if port is None else f"{server_host}:{port}"
    scheme = str(getattr(request, "scheme", "http")).lower()
    authority = str(host).lower()
    return f"{len(scheme.encode('utf-8'))}:{scheme}:{len(authority.encode('utf-8'))}:{authority}:"


def _tenant_cache_key(request: Any, key: str) -> str:
    state = getattr(request, "state", None)
    tenant = getattr(state, "tenant", None)
    if tenant is None:
        return f"0:{key}"
    value = str(getattr(tenant, "key", tenant))
    return f"1:{len(value.encode('utf-8'))}:{value}:{key}"


def _private_cache_key(request: Any, key: str) -> str:
    identity = getattr(request, "identity", None)
    authority = _authority_prefix(request)
    if identity is None:
        return f"0:{authority}{key}"
    namespace = str(getattr(identity, "namespace", ""))
    kind = str(getattr(identity, "type", type(identity).__name__))
    subject = str(getattr(identity, "id", identity))
    framed = "".join(
        f"{len(value.encode('utf-8'))}:{value}:" for value in (namespace, kind, subject)
    )
    return f"1:{framed}{authority}{key}"


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
        prefix = _authority_prefix(request)
        path = _request_path(request)
        base = f"{prefix}{request.method} {path}"
        if not declared:
            return base
        selected = _core.cache_key_selected(request.method, path, request.query_string, declared)
        return f"{prefix}{selected}"

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

    secret: bytes = field(default=b"", repr=False)
    prefix: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.secret, (bytes, bytearray)):
            raise TypeError("Tags(secret=...) must be bytes, not str")
        if not self.secret:
            raise ValueError(
                "Tags(secret=...) is required: an unkeyed surrogate key can be "
                "computed by anyone who knows your model names, and computing "
                "it is all it takes to purge that tag"
            )
        object.__setattr__(self, "secret", bytes(self.secret))

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

    __slots__ = (
        "__weakref__",
        "_closed",
        "_dropped",
        "_enqueued",
        "_jobs",
        "_key",
        "_pending",
        "_runner",
        "_subscribed",
        "_tags",
        "_task",
        "_watching",
    )

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
        self._closed = False
        self._pending: set[str] = set()
        self._runner: asyncio.Task[None] | None = None
        self._subscribed = False
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
        an application-lifetime object. Call it during startup and call `close`
        during shutdown. Repeated calls extend the watched model set.

        Raises:
            RuntimeError: This purger has been closed and cannot be restarted.
        """
        if self._closed:
            raise RuntimeError(
                "cannot watch models after the CDN purger was closed; create a new CDNPurge"
            )
        self._watching = self._watching | {watched_name(model) for model in models}
        if self._watching and not self._subscribed:
            subscribe_writes(self._on_write, owner=self)
            self._subscribed = True

    def unwatch(self, *models: Any) -> None:
        """Stop purging the named models, or every model when none are given."""
        removed = self._watching if not models else {watched_name(model) for model in models}
        self._watching = self._watching - removed
        if not self._watching and self._subscribed:
            unsubscribe_writes(self._on_write)
            self._subscribed = False

    def close(self) -> None:
        """Stop receiving writes while allowing already scheduled enqueues to finish."""
        if self._closed:
            return
        self.unwatch()
        self._closed = True

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
        """Hand purges to one coalescing runner without delaying the committed write."""
        if tag in self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A committed write has no transaction left to fail.
            self._dropped += 1
            return
        self._pending.add(tag)
        self._start_runner(loop)

    def _start_runner(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._runner is not None or not self._pending:
            return
        if loop is None:
            loop = asyncio.get_running_loop()
        task = loop.create_task(self._drain_pending())
        self._runner = task
        task.add_done_callback(self._runner_done)

    async def _drain_pending(self) -> None:
        while self._pending:
            tag = next(iter(self._pending))
            await self._enqueue(tag)
            self._pending.remove(tag)

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        if self._runner is not task:
            return
        self._runner = None
        if task.cancelled():
            self._dropped += len(self._pending)
            self._pending.clear()
            return
        self._start_runner()

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


def _apply_header(result: Any, name: bytes, value: bytes) -> None:
    if isinstance(result, Response) and (name, value) not in result.headers:
        result.headers.append((name, value))


def _apply_tags(result: Any, value: bytes, groups: bytes) -> None:
    """Write surrogate-key headers when the handler returned a `Response`."""
    if not isinstance(result, Response):
        return
    headers = result.headers
    for name in TAG_HEADERS:
        # Preserve handler tags and keep reused response objects idempotent.
        if (name, value) not in headers:
            headers.append((name, value))
    if (_CACHE_GROUPS, groups) not in headers:
        headers.append((_CACHE_GROUPS, groups))


def _cache_status_values(identifier: str) -> dict[str, bytes]:
    states = {
        "hit": {"hit": True},
        "miss": {"fwd": Token("uri-miss")},
        "stored": {"fwd": Token("uri-miss"), "stored": True},
        "method": {"fwd": Token("method")},
        "bypass": {"fwd": Token("bypass")},
    }
    return {
        state: serialize_list([Item(identifier, parameters)])
        for state, parameters in states.items()
    }


def _normalized_content_type(value: bytes) -> bytes:
    media_type, separator, parameters = value.partition(b";")
    normalized = media_type.strip().lower()
    if not separator:
        return normalized
    return normalized + b";" + parameters.strip()


def _normalized_comma_tokens(value: bytes) -> bytes:
    return b",".join(part.strip().lower() for part in value.split(b","))


def _digest_part(digest: Any, kind: bytes, value: bytes) -> None:
    digest.update(kind)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


async def _query_cache_key(request: Any, base: str) -> str:
    digest = blake2b(digest_size=32)
    for name, value in request.headers:
        if name == b"content-type":
            _digest_part(digest, b"t", _normalized_content_type(value))
        elif name == b"content-encoding":
            _digest_part(digest, b"e", _normalized_comma_tokens(value))
        elif name == b"content-language":
            _digest_part(digest, b"l", _normalized_comma_tokens(value))
        elif name == b"content-location":
            _digest_part(digest, b"u", value.strip())
    _digest_part(digest, b"b", await request.body())
    return f"{base}#query={digest.hexdigest()}"


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
    methods: tuple[str, ...] = ("GET", "QUERY"),
    store: BoundedCache | None = None,
    invalidate_on: Iterable[Any] = (),
    query_params: Iterable[str] | None = None,
    tags: Tags | None = None,
    cache_status: str | None = None,
) -> Any:
    """Cache a route handler's response in a bounded in-process store.

    Args:
        ttl: seconds an entry stays fresh (`None` = until evicted by capacity).
        max_entries: hard ceiling on cached responses (LRU eviction past it).
        key: builds the application part of a private cache key. Wreath adds
            tenant, principal, scheme, and authority ownership. The default is
            the shared scheme+authority+method+path+query key; authenticated
            callers bypass that shared cache.
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
        cache_status: cache identifier to emit in an RFC 9211 `Cache-Status`
            field. Omit it to expose no cache diagnostics.

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
            cache_status=cache_status,
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
    if tags is None:
        tag_value = None
        group_value = None
    else:
        tag_keys = tags.keys(watched)
        tag_value = " ".join(tag_keys).encode("ascii")
        group_value = serialize_list(Item(tag) for tag in tag_keys)
    if cache_status is not None and not isinstance(cache_status, str):
        raise TypeError(f"cached(cache_status=...) must be str, got {type(cache_status).__name__}")
    status_values = _cache_status_values(cache_status) if cache_status is not None else None
    public_key = key is default_cache_key or getattr(key, "_wreath_public", False)
    refused_methods = tuple(method for method in methods if method not in _CACHEABLE_METHODS)
    if refused_methods:
        names = ", ".join(refused_methods)
        raise ValueError(
            f"cached methods {names} can depend on a request body or expose request "
            "headers; cache only GET, HEAD, OPTIONS or QUERY (whose body is part of "
            "the key)"
        )
    cached_methods = frozenset(methods) if len(methods) >= 8 else methods

    window = None if ttl is None else Duration.of(ttl).total_seconds()
    the_store: BoundedCache = (
        store if store is not None else BoundedCache(max_entries=max_entries, ttl=window)
    )

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        # Share one in-flight computation per cold key.
        inflight: dict[str, asyncio.Future[Any]] = {}

        @wraps(handler)
        async def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
            if request.method not in cached_methods:
                result = await handler(request, *args, **kwargs)
                if group_value is not None and request.method not in _SAFE_METHODS:
                    _apply_header(result, _CACHE_GROUP_INVALIDATION, group_value)
                if status_values is not None:
                    reason = "method" if request.method not in _SAFE_METHODS else "bypass"
                    _apply_header(result, _CACHE_STATUS, status_values[reason])
                return result
            if public_key and getattr(request, "identity", None) is not None:
                # A principal must never read or populate a shared key.
                result = await handler(request, *args, **kwargs)
                if status_values is not None:
                    _apply_header(result, _CACHE_STATUS, status_values["bypass"])
                return result
            try:
                base_key = key(request)
                if not public_key:
                    base_key = _private_cache_key(request, base_key)
                cache_key = _tenant_cache_key(request, base_key)
            except _AmbiguousAuthority:
                result = await handler(request, *args, **kwargs)
                if status_values is not None:
                    _apply_header(result, _CACHE_STATUS, status_values["bypass"])
                return result
            if request.method == "QUERY":
                cache_key = await _query_cache_key(request, cache_key)
            hit = the_store.get(cache_key)
            if hit is not None:
                result = _revive(hit)
                if status_values is not None:
                    _apply_header(result, _CACHE_STATUS, status_values["hit"])
                return result
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
            if tag_value is not None and group_value is not None:
                # Snapshot the same purge tags served on the cold response.
                _apply_tags(result, tag_value, group_value)
            entry = _snapshot(result)
            if entry is not None:
                the_store.set(cache_key, entry)
            if status_values is not None:
                state = "stored" if entry is not None else "miss"
                _apply_header(result, _CACHE_STATUS, status_values[state])
            return result

        def invalidate(request: Any = None) -> Any:
            if request is None:
                the_store.clear()
                return None
            try:
                base_key = key(request)
                if not public_key:
                    base_key = _private_cache_key(request, base_key)
                base = _tenant_cache_key(request, base_key)
            except _AmbiguousAuthority:
                return None
            if request.method != "QUERY":
                the_store.delete(base)
                return None

            async def invalidate_query() -> None:
                the_store.delete(await _query_cache_key(request, base))

            return invalidate_query()

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
