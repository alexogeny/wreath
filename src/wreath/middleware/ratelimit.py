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
import re
from collections.abc import Callable
from time import monotonic
from typing import Any, Protocol

from .._native import _core
from ..request import Request
from ..response import ProblemResponse

if _core is not None and hasattr(_core, "TokenBucket"):
    TokenBucket: Any = _core.TokenBucket
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.ratelimit import TokenBucket

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """In-process buckets. Fast and dependency-free, but per-worker."""

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

    Postgres owns the clock (``clock_timestamp()``), which keeps workers on
    disagreeing wall clocks from disagreeing about refills. ``clock_timestamp``
    rather than ``now()`` because ``now()`` is fixed at transaction start and
    would freeze refills for a caller inside a transaction.

    The table is not created for you; apply :meth:`schema_sql` as a migration.
    Rows are small and bounded by distinct keys -- call :meth:`purge`
    periodically to drop idle ones.
    """

    __slots__ = ("_acquire", "_capacity", "_database", "_purge", "_rate", "_table")

    def __init__(self, database: Any, *, table: str = "wreath_rate_limit") -> None:
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQL identifier")
        self._database = database
        self._table = table
        self._capacity = 0.0
        self._rate = 0.0
        self._acquire: Any = None
        self._purge: Any = None

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._table} (\n"
            "    key text PRIMARY KEY,\n"
            "    tokens double precision NOT NULL,\n"
            "    allowed boolean NOT NULL,\n"
            "    updated timestamptz NOT NULL\n"
            ")"
        )

    def configure(self, capacity: float, rate: float) -> None:
        if self._acquire is not None:
            raise ValueError(
                "this store is already configured; give each RateLimitMiddleware "
                "its own store"
            )
        self._capacity = capacity
        self._rate = rate
        # $1 key, $2 capacity, $3 cost, $4 rate. The refill expression is
        # repeated because ON CONFLICT ... DO UPDATE has nowhere to bind an
        # alias for it; Postgres evaluates it once per row regardless.
        refill = (
            "LEAST($2::float8, b.tokens + "
            "EXTRACT(EPOCH FROM (clock_timestamp() - b.updated)) * $4::float8)"
        )
        # `allowed` is written as well as returned: when a bucket is short, the
        # stored token count alone cannot say whether this call consumed or was
        # refused -- both land in [0, cost).
        sql = (
            f"INSERT INTO {self._table} AS b (key, tokens, allowed, updated)\n"
            "VALUES ($1, $2::float8 - $3::float8, true, clock_timestamp())\n"
            "ON CONFLICT (key) DO UPDATE SET\n"
            f"    tokens = {refill}\n"
            f"           - CASE WHEN {refill} >= $3::float8 THEN $3::float8 ELSE 0::float8 END,\n"
            f"    allowed = {refill} >= $3::float8,\n"
            "    updated = clock_timestamp()\n"
            "RETURNING tokens, allowed"
        )
        self._acquire = self._database.statement(
            f"wreath_rate_limit_acquire_{self._table}", sql, workload="write"
        )
        self._purge = self._database.statement(
            f"wreath_rate_limit_purge_{self._table}",
            f"DELETE FROM {self._table} "
            "WHERE updated < clock_timestamp() - make_interval(secs => $1::float8)",
            workload="write",
        )

    async def acquire(self, key: str, cost: float, now: float) -> float:
        # `now` is ignored: Postgres is the clock, so workers cannot disagree.
        row = await self._acquire.fetchrow(key, self._capacity, cost, self._rate)
        tokens = float(row[0])
        if row[1]:
            return 0.0
        return (cost - tokens) / self._rate

    async def purge(self, idle_seconds: float) -> str:
        """Drop buckets untouched for `idle_seconds`. Safe: an idle bucket has
        refilled to capacity, so forgetting it grants nothing it would not."""
        return await self._purge.execute(idle_seconds)


def _client_key(request: Request) -> str | None:
    client = request.client
    if not client:
        return None
    return str(client[0])


class RateLimitMiddleware:
    """Reject requests that exceed a per-key token-bucket allowance."""

    global_scope = True
    __slots__ = ("_cost", "_exempt", "_key", "_store", "_try_acquire", "before")

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
        selected = store if store is not None else MemoryRateLimitStore()
        selected.configure(capacity, limit / window)
        self._store = selected
        self._cost = cost
        self._key = key if key is not None else _client_key
        self._exempt = exempt
        # Resolved once rather than branching and re-looking-up per request. A
        # synchronous store also skips a coroutine on the hot path; the memory
        # store is exactly that.
        self._try_acquire: Any = getattr(selected, "try_acquire", None)
        self.before = self._before_local if self._try_acquire is not None else self._before_remote

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

    async def _before_local(self, request: Request) -> Any | None:
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


__all__ = [
    "MemoryRateLimitStore",
    "PostgresRateLimitStore",
    "RateLimitMiddleware",
    "RateLimitStore",
]
