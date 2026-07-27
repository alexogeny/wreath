"""Token-bucket rate limiting with a pluggable store.

A token bucket rather than a fixed window: a window counter lets a client spend
its whole allowance in the last instant of one window and again in the first
instant of the next, delivering double the intended rate across the boundary.
A bucket refills continuously, so the average rate holds over any interval while
still admitting a configured burst.

The default store keeps buckets in this process::

    app.add_middleware(RateLimitMiddleware(limit=60, window=60.0))

That is per-worker: with four workers the effective limit is four times the
configured one. When the limit must hold across workers, hand it a store backed
by a table the workers share::

    store = PostgresRateLimitStore(app.postgres("main"))
    app.add_middleware(RateLimitMiddleware(limit=60, window=60.0, store=store))

By default each client IP gets its own bucket, read from ``scope["client"]``.
Behind a proxy that address is the proxy's, and every client would share one
bucket -- add :class:`~wreath.middleware.ProxyHeadersMiddleware` ahead of this one
so the scope carries the real client.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any, Protocol

from .._native import _core
from ..request import Request
from ..response import ProblemResponse
from ..store import ALIAS, Column, Keyed, PostgresStore

if _core is not None and hasattr(_core, "TokenBucket"):
    TokenBucket: Any = _core.TokenBucket
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.ratelimit import TokenBucket


class RateLimitStore(Protocol):
    """Where buckets live.

    ``configure`` is called once, when the middleware is constructed, so the
    policy has a single owner and the store can prepare statements before
    startup. A store may additionally expose a synchronous ``try_acquire`` with
    the same contract as ``acquire``; the middleware binds to it and skips
    awaiting when it exists.
    """

    def configure(self, capacity: float, rate: float) -> None: ...

    async def acquire(self, key: str, cost: float, now: float) -> float:
        """Return 0.0 when allowed, else the seconds until enough tokens.

        ``now`` is a monotonic reading from the caller. A store that owns a
        clock its workers agree on (Postgres does) should ignore it.
        """
        ...


class MemoryRateLimitStore:
    """In-process buckets. Fast and dependency-free, but per-worker.

    Not a :class:`wreath.store.MemoryStore`: a bucket is not a stored payload
    but an arithmetic state that every read advances, and the whole
    refill-and-consume step lives in ``TokenBucket`` (native, with its own
    bounded table) so the hot path stays one call.
    """

    __slots__ = ("_bucket", "_max_entries", "_policy")

    def __init__(self, *, max_entries: int = 10000) -> None:
        self._max_entries = max_entries
        self._bucket: Any = None
        self._policy: tuple[float, float] | None = None

    def configure(self, capacity: float, rate: float) -> None:
        if self._policy is not None and self._policy != (capacity, rate):
            raise ValueError(
                "this store is already configured with a different rate limit; "
                "give each RateLimitMiddleware its own store"
            )
        self._policy = (capacity, rate)
        self._bucket = TokenBucket(
            capacity=capacity, rate=rate, max_entries=self._max_entries
        )

    @property
    def tracked(self) -> int:
        return 0 if self._bucket is None else self._bucket.tracked

    def try_acquire(self, key: str, cost: float, now: float) -> float:
        return float(self._bucket.acquire(key, now, cost))

    async def acquire(self, key: str, cost: float, now: float) -> float:
        return self.try_acquire(key, cost, now)

    def clear(self) -> None:
        if self._bucket is not None:
            self._bucket.clear()


class PostgresRateLimitStore:
    """Buckets in a shared table, so one limit covers every worker.

    The whole refill-and-consume decision is one ``INSERT ... ON CONFLICT DO
    UPDATE``. That statement takes a row lock, so concurrent workers serialize
    per key and the limit holds exactly; two statements or a read-then-write
    would race and overspend.

    ``updated`` is a last-touched mark rather than a deadline, which is what
    makes this store's ageing different from the other two in
    :mod:`wreath.store`: a bucket never expires, it refills, and the elapsed
    time since ``updated`` is the arithmetic the statement above does. Only an
    idle purge can retire one.

    The table is not created for you; apply :meth:`schema_sql` as a migration.
    Rows are small and bounded by distinct keys -- call :meth:`purge`
    periodically to drop idle ones.
    """

    __slots__ = ("_capacity", "_database", "_policy", "_rate", "_store")

    def __init__(self, database: Any, *, table: str = "wreath_rate_limit") -> None:
        self._database = database
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                columns=(
                    Column("tokens", "double precision", null=False),
                    Column("allowed", "boolean", null=False),
                ),
                stamp="updated",
                deadline=False,
                # An index on `updated`: the purge pass walks this table in
                # last-touched order, and a keyset walk needs an index under it
                # or it sorts the whole table once per chunk.
                index_stamp=True,
                prefix="wreath_rate_limit",
            ),
        )
        # $1 key, $2 capacity, $3 cost, $4 rate. Nothing about the policy is
        # baked into the text -- capacity and rate are bound per call -- so the
        # statement is built here and `configure` only records the numbers.
        # The refill expression is repeated because ON CONFLICT ... DO UPDATE
        # has nowhere to bind an alias for it; Postgres evaluates it once per
        # row regardless.
        refill = (
            f"LEAST($2::float8, {ALIAS}.tokens + "
            f"EXTRACT(EPOCH FROM (clock_timestamp() - {ALIAS}.updated)) * $4::float8)"
        )
        self._store.define(
            "acquire",
            self._store.upsert(
                values={
                    "key": "$1",
                    "tokens": "$2::float8 - $3::float8",
                    "allowed": "true",
                    "updated": "clock_timestamp()",
                },
                # `allowed` is written as well as returned: when a bucket is
                # short, the stored token count alone cannot say whether this
                # call consumed or was refused -- both land in [0, cost).
                update={
                    "tokens": (
                        f"{refill}\n"
                        f"           - CASE WHEN {refill} >= $3::float8 "
                        "THEN $3::float8 ELSE 0::float8 END"
                    ),
                    "allowed": f"{refill} >= $3::float8",
                    "updated": "clock_timestamp()",
                },
                returning="tokens, allowed",
            ),
        )
        self._capacity = 0.0
        self._rate = 0.0
        self._policy: tuple[float, float] | None = None

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return self._store.schema_sql()

    def configure(self, capacity: float, rate: float) -> None:
        if self._policy is not None:
            raise ValueError(
                "this store is already configured; give each RateLimitMiddleware "
                "its own store"
            )
        self._policy = (capacity, rate)
        self._capacity = capacity
        self._rate = rate

    async def acquire(self, key: str, cost: float, now: float) -> float:
        # `now` is ignored: Postgres is the clock, so workers cannot disagree.
        row = await self._store.statement("acquire").fetchrow(
            key, self._capacity, cost, self._rate
        )
        tokens = float(row[0])
        if row[1]:
            return 0.0
        return (cost - tokens) / self._rate

    def purge_pass(self, idle_seconds: float, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that retires buckets untouched for *idle_seconds*.

        The supported way to keep the table small::

            jobs.drive(store.purge_pass(3600), cron="17 * * * *")

        It walks in last-touched order behind a frontier the database clock
        re-derives each cycle, one transaction per chunk, paced. See
        :mod:`wreath.passes`.
        """
        from .._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration, self._database,
            name=f"purge_{self._store.table}", after=float(idle_seconds),
            chunk=chunk, **options,
        )

    async def purge(self, idle_seconds: float) -> str:
        """Drop buckets untouched for *idle_seconds*, in **one unbounded statement**.

        Safe -- an idle bucket has refilled to capacity, so forgetting it grants
        nothing it would not have granted anyway -- but on a table big enough to
        matter this is the long transaction a chunked pass exists to prevent.
        Prefer :meth:`purge_pass`.
        """
        return await self._store.purge(idle_seconds)


def principal_key(request: Request) -> str | None:
    """Key a limit on *who is asking* rather than where they connected from.

    Behind a proxy or a carrier NAT, the client address lumps unrelated callers
    into one bucket and lets one caller earn a fresh allowance per device. For
    an authenticated API the principal is the honest key.

    Anonymous callers fall back to their address, because a single shared
    "anonymous" bucket is a denial of service you inflict on yourself. The
    ``ip:`` prefix keeps an address from ever colliding with a principal id.

    **Must run after authentication.** `request.identity` is set during route
    authorization, so a limiter using this key has to be route middleware --
    a global hook runs at ingress, before anyone has been identified. See
    :class:`TieredRateLimitMiddleware`.
    """
    identity = request.identity
    if identity is not None:
        return f"{identity.type}:{identity.id}"
    client = request.client
    return f"ip:{client[0]}" if client else None


def _client_key(request: Request) -> str | None:
    client = request.client
    if not client:
        return None
    return str(client[0])


class RateLimitMiddleware:
    """Reject requests that exceed a per-key token-bucket allowance."""

    global_scope = True
    __slots__ = ("_cost", "_exempt", "_key", "_store", "_try_acquire", "before", "before_sync")

    def __init__(
        self,
        *,
        limit: int,
        window: float = 60.0,
        burst: int | None = None,
        cost: float = 1.0,
        store: RateLimitStore | None = None,
        key: Callable[[Request], str | None] | None = None,
        exempt: Callable[[Request], bool] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window <= 0.0:
            raise ValueError("window must be positive")
        if cost <= 0.0:
            raise ValueError("cost must be positive")
        capacity = float(limit if burst is None else burst)
        if capacity < cost:
            raise ValueError("burst must be at least the per-request cost")
        if key is principal_key:
            # A global hook runs at ingress, before route authorization has
            # identified anyone, so `principal_key` would silently degrade to
            # the client address and put every caller in one bucket. That is a
            # production incident, not a preference -- refuse it at startup.
            raise ValueError(
                "RateLimitMiddleware is a global hook and runs before "
                "authentication, so it cannot key on the principal; use "
                "TieredRateLimitMiddleware (route middleware) for per-principal "
                "limits, and keep this one keyed on the address for ingress"
            )
        selected = store if store is not None else MemoryRateLimitStore()
        selected.configure(capacity, limit / window)
        self._store = selected
        self._cost = cost
        self._key = key if key is not None else _client_key
        self._exempt = exempt
        # Resolved once rather than branching and re-looking-up per request. A
        # synchronous store also skips a coroutine on the hot path; the memory
        # store is exactly that, so it exposes before_sync (fused, no await)
        # while a remote store keeps the awaiting before hook.
        self._try_acquire: Any = getattr(selected, "try_acquire", None)
        if self._try_acquire is not None:
            self.before = None
            self.before_sync = self._before_local_sync
        else:
            self.before = self._before_remote
            self.before_sync = None

    def _identify(self, request: Request) -> str | None:
        if self._exempt is not None and self._exempt(request):
            return None
        return self._key(request)

    def _limited(self, retry_after: float) -> ProblemResponse:
        response = ProblemResponse(
            status=429, title="Too Many Requests", detail="Rate limit exceeded"
        )
        # Retry-After is whole seconds, and 0 would invite an instant retry.
        seconds = max(1, math.ceil(retry_after))
        response.headers.append((b"retry-after", str(seconds).encode("ascii")))
        return response

    def _before_local_sync(self, request: Request) -> Any | None:
        key = self._identify(request)
        if key is None:
            return None
        retry_after = self._try_acquire(key, self._cost, monotonic())
        return None if retry_after <= 0.0 else self._limited(retry_after)

    async def _before_remote(self, request: Request) -> Any | None:
        key = self._identify(request)
        if key is None:
            return None
        retry_after = await self._store.acquire(key, self._cost, monotonic())
        return None if retry_after <= 0.0 else self._limited(retry_after)


class TieredRateLimitMiddleware:
    """A different allowance per plan, keyed on the principal.

    An authenticated API rarely wants one limit for everybody: the free tier and
    the enterprise tier are different products. The tier comes from the caller's
    roles -- the same roles the Cedar policies authorize with -- so there is one
    answer to "who is this" rather than two that can disagree::

        app.add_middleware(TieredRateLimitMiddleware(
            tiers={"pro": (600, 60.0), "enterprise": (10_000, 60.0)},
            default=(60, 60.0),
        ))

    Each entry is ``(limit, window_seconds)``. A caller holding more than one
    named role gets the **most generous** of them, because holding two plans
    must not be worse than holding the better one. Anyone else gets ``default``.

    **This is route middleware, not a global hook**, and that is not an
    oversight: global hooks run at ingress, before authentication, where there
    is no principal to key on. Register it with ``add_middleware`` on routes
    that require authentication. For unauthenticated ingress protection --
    a flood of 404s, a login-endpoint flood -- use the global
    :class:`RateLimitMiddleware` keyed on the address, and use both.

    Each tier keeps its own buckets, so a promotion arrives with a full
    allowance rather than whatever was left of the old plan's.
    """

    global_scope = False
    __slots__ = ("_default", "_dispatch", "_tier", "_tiers")

    def __init__(
        self,
        *,
        tiers: Mapping[str, tuple[int, float]],
        default: tuple[int, float],
        tier: Callable[[Request], str | None] | None = None,
        key: Callable[[Request], str | None] = principal_key,
        cost: float = 1.0,
        exempt: Callable[[Request], bool] | None = None,
        store_factory: Callable[[], RateLimitStore] | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("at least one tier is required")
        build = store_factory if store_factory is not None else MemoryRateLimitStore
        self._tiers = dict(tiers)
        self._default = default
        self._tier = tier if tier is not None else self._tier_from_roles
        # One limiter -- and so one store, one keyspace -- per tier. Reusing
        # RateLimitMiddleware keeps exactly one implementation of the bucket
        # decision, the 429, and the Retry-After.
        # Resolved to a concrete hook per tier at construction rather than
        # branched per request: `RateLimitMiddleware` exposes a synchronous hook
        # for a local store and an awaiting one for a remote store, and which it
        # is cannot change afterwards.
        self._dispatch: dict[str | None, tuple[Any, bool]] = {}
        for name, (limit, window) in {**tiers, None: default}.items():
            child = RateLimitMiddleware(
                limit=limit, window=window, cost=cost, exempt=exempt, store=build(),
            )
            # Assigned rather than passed: `RateLimitMiddleware` refuses
            # `principal_key` at construction because *it* is a global hook and
            # would key everyone the same. This middleware is route-scoped and
            # runs after authentication, which is precisely what makes the key
            # valid here.
            child._key = key
            hook = child.before
            self._dispatch[name] = (
                (hook, True) if hook is not None else (child.before_sync, False)
            )

    def _tier_from_roles(self, request: Request) -> str | None:
        """The most generous tier among the caller's roles, or None."""
        identity = request.identity
        if identity is None:
            return None
        matched = [role for role in identity.roles if role in self._tiers]
        if not matched:
            return None
        return max(matched, key=lambda role: self._tiers[role][0] / self._tiers[role][1])

    async def before(self, request: Request) -> Any | None:
        hook, awaiting = self._dispatch.get(
            self._tier(request), self._dispatch[None]
        )
        return await hook(request) if awaiting else hook(request)


__all__ = [
    "MemoryRateLimitStore",
    "PostgresRateLimitStore",
    "RateLimitMiddleware",
    "RateLimitStore",
    "TieredRateLimitMiddleware",
    "principal_key",
]
