"""RateLimitMiddleware and its stores."""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath._pure.ratelimit import TokenBucket as PureTokenBucket
from wreath.middleware import (
    MemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimitMiddleware,
)
from wreath.testing import TestClient

_BUCKETS = [PureTokenBucket]
if _core is not None and hasattr(_core, "TokenBucket"):
    _BUCKETS.append(_core.TokenBucket)


# --- bucket mechanics -------------------------------------------------------


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_admits_a_burst_then_refills_continuously(bucket_type: Any) -> None:
    bucket = bucket_type(capacity=3.0, rate=1.0, max_entries=100)
    assert [bucket.acquire("a", 1000.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.acquire("a", 1000.0) == pytest.approx(1.0)  # retry in 1s
    assert bucket.acquire("a", 1000.5) == pytest.approx(0.5)  # half a token short
    assert bucket.acquire("a", 1001.0) == 0.0
    assert bucket.acquire("b", 1000.0) == 0.0  # keys are independent


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_never_exceeds_capacity_when_idle(bucket_type: Any) -> None:
    bucket = bucket_type(capacity=2.0, rate=1.0, max_entries=100)
    bucket.acquire("a", 1000.0)
    # Idle for an hour: the bucket refills to capacity, not beyond it.
    assert [bucket.acquire("a", 4600.0) for _ in range(2)] == [0.0, 0.0]
    assert bucket.acquire("a", 4600.0) > 0.0


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_ignores_a_clock_that_moves_backwards(bucket_type: Any) -> None:
    bucket = bucket_type(capacity=1.0, rate=1.0, max_entries=100)
    assert bucket.acquire("a", 1000.0) == 0.0
    assert bucket.acquire("a", 900.0) > 0.0  # no tokens minted by the step


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_validates_configuration(bucket_type: Any) -> None:
    for kwargs in (
        {"capacity": 0.0, "rate": 1.0},
        {"capacity": 1.0, "rate": 0.0},
        {"capacity": 1.0, "rate": 1.0, "max_entries": 0},
    ):
        with pytest.raises(ValueError):
            bucket_type(**kwargs)
    bucket = bucket_type(capacity=1.0, rate=1.0)
    with pytest.raises(ValueError):
        bucket.acquire("a", 1.0, 0.0)
    with pytest.raises(ValueError):
        bucket.acquire("a", 1.0, 5.0)  # cost above capacity could never succeed


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_honours_max_entries_under_key_spraying(bucket_type: Any) -> None:
    """Distinct-key floods must not grow the table without bound."""
    bucket = bucket_type(capacity=5.0, rate=1.0, max_entries=100)
    for index in range(20000):
        bucket.acquire(f"attacker-{index}", 1000.0 + index * 0.001)
    assert bucket.tracked <= 100

    # Even when every bucket is still actively limited, so none can be reclaimed.
    hot = bucket_type(capacity=2.0, rate=0.001, max_entries=50)
    for index in range(2000):
        for _ in range(3):
            hot.acquire(f"k{index}", 1000.0)
    assert hot.tracked <= 50


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_keeps_limiting_a_key_it_still_tracks(bucket_type: Any) -> None:
    bucket = bucket_type(capacity=1.0, rate=0.001, max_entries=10)
    assert bucket.acquire("victim", 1000.0) == 0.0
    assert bucket.acquire("victim", 1000.0) > 0.0
    bucket.clear()
    assert bucket.tracked == 0
    assert bucket.acquire("victim", 1000.0) == 0.0


def test_native_bucket_agrees_with_pure_reference() -> None:
    if _core is None or not hasattr(_core, "TokenBucket"):
        pytest.skip("native core unavailable")
    native = _core.TokenBucket(capacity=3.0, rate=2.0, max_entries=64)
    pure = PureTokenBucket(capacity=3.0, rate=2.0, max_entries=64)
    now = 1000.0
    for step in range(400):
        key = f"k{step % 17}"
        now += 0.03
        assert native.acquire(key, now) == pytest.approx(pure.acquire(key, now)), (key, now)
    assert native.tracked == pure.tracked


# --- middleware -------------------------------------------------------------


def _app(**kwargs: Any) -> Wreath:
    app = Wreath()
    app.add_middleware(RateLimitMiddleware(**kwargs), priority=-5)

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    return app


async def test_requests_beyond_the_burst_get_429_with_retry_after() -> None:
    async with TestClient(_app(limit=3, window=60.0)) as client:
        statuses = [(await client.get("/")).status for _ in range(4)]
        limited = await client.get("/")

    assert statuses == [200, 200, 200, 429]
    assert limited.status == 429
    assert limited.header("content-type") == "application/problem+json"
    # Whole seconds, and never 0 -- a 0 would invite an immediate retry.
    assert int(limited.header("retry-after")) >= 1


async def test_burst_is_configurable_independently_of_the_rate() -> None:
    async with TestClient(_app(limit=1, window=60.0, burst=5)) as client:
        statuses = [(await client.get("/")).status for _ in range(6)]
    assert statuses == [200] * 5 + [429]


async def test_exempt_requests_bypass_the_limit() -> None:
    async with TestClient(_app(limit=1, window=60.0, exempt=lambda request: True)) as client:
        statuses = [(await client.get("/")).status for _ in range(5)]
    assert statuses == [200] * 5


async def test_custom_key_separates_callers() -> None:
    def by_header(request: Any) -> str | None:
        return request.header("x-tenant")

    app = _app(limit=1, window=60.0, key=by_header)
    async with TestClient(app) as client:
        first_a = await client.get("/", headers={"x-tenant": "a"})
        first_b = await client.get("/", headers={"x-tenant": "b"})
        second_a = await client.get("/", headers={"x-tenant": "a"})
        unkeyed = await client.get("/")  # no key: cannot identify, so not limited

    assert (first_a.status, first_b.status) == (200, 200)
    assert second_a.status == 429
    assert unkeyed.status == 200


async def test_limiting_covers_responses_the_router_never_reached() -> None:
    app = Wreath()
    app.add_middleware(RateLimitMiddleware(limit=1, window=60.0), priority=-5)

    async with TestClient(app) as client:
        first = await client.get("/missing")
        second = await client.get("/missing")

    assert first.status == 404
    assert second.status == 429  # a 404 flood is still a flood


def test_middleware_validates_configuration() -> None:
    for kwargs in (
        {"limit": 0},
        {"limit": 1, "window": 0.0},
        {"limit": 1, "cost": 0.0},
        {"limit": 1, "burst": 1, "cost": 2.0},
    ):
        with pytest.raises(ValueError):
            RateLimitMiddleware(**kwargs)


def test_a_store_cannot_be_shared_between_conflicting_policies() -> None:
    store = MemoryRateLimitStore()
    RateLimitMiddleware(limit=10, window=60.0, store=store)
    with pytest.raises(ValueError, match="already configured"):
        RateLimitMiddleware(limit=99, window=60.0, store=store)


def test_memory_store_reports_what_it_tracks() -> None:
    store = MemoryRateLimitStore(max_entries=8)
    store.configure(2.0, 1.0)
    assert store.tracked == 0
    store.try_acquire("a", 1.0, 1000.0)
    assert store.tracked == 1
    store.clear()
    assert store.tracked == 0


def test_postgres_store_rejects_an_unsafe_table_name() -> None:
    for bad in ("a; DROP TABLE users", "", "1abc", "sch.ema"):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            PostgresRateLimitStore(object(), table=bad)


def test_postgres_store_schema_is_offered_as_a_migration() -> None:
    store = PostgresRateLimitStore(object(), table="limits")
    sql = store.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS limits" in sql
    assert "key text PRIMARY KEY" in sql


# --- Postgres store ---------------------------------------------------------
#
# These pin the store's contract against a fake connection. The SQL itself is
# only meaningful against a real server: it was verified on Postgres 16, where
# 30 concurrent acquires against a capacity-3 bucket admitted exactly 3.


class _FakeConnection:
    def __init__(self, rows: list[tuple[float, bool]]) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows = rows

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "DELETE 0"

    async def fetchrow(self, sql: str, *args: object) -> object:
        self.calls.append((sql, args))
        return self.rows.pop(0)

    async def close(self) -> None:
        return None


def _pg_store(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[float, bool]]
) -> tuple[Any, Any, _FakeConnection]:
    """A store wired to a fake connection. The caller starts the database."""
    from wreath.postgres import Database, PoolConfig

    connection = _FakeConnection(rows)

    async def connect(dsn: str) -> _FakeConnection:
        return connection

    monkeypatch.setattr("wreath.postgres.connect", connect)
    database = Database("rl", "postgresql://x/y", pools={"write": PoolConfig(min_size=1)})
    return PostgresRateLimitStore(database), database, connection


def _acquires(connection: _FakeConnection) -> list[tuple[str, tuple[object, ...]]]:
    # Startup prepares statements on the connection; keep only real acquires.
    return [call for call in connection.calls if call[0].startswith("INSERT INTO")]


async def test_postgres_store_translates_the_row_into_a_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, connection = _pg_store(monkeypatch, [(2.0, True), (0.25, False)])
    store.configure(3.0, 1.0)
    await database.start()

    assert await store.acquire("k", 1.0, 0.0) == 0.0
    # Denied with a quarter token banked and 1 token/s: 0.75s until it is owed.
    assert await store.acquire("k", 1.0, 0.0) == pytest.approx(0.75)

    sql, args = _acquires(connection)[0]
    assert args == ("k", 3.0, 1.0, 1.0)  # key, capacity, cost, rate
    # One statement: a read-then-write would race between workers.
    assert sql.count(";") == 0
    assert "ON CONFLICT (key) DO UPDATE" in sql
    # now() is fixed at transaction start and would freeze refills.
    assert "clock_timestamp()" in sql
    assert "now()" not in sql


async def test_postgres_store_purges_idle_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, connection = _pg_store(monkeypatch, [])
    store.configure(3.0, 1.0)
    await database.start()
    await store.purge(3600.0)
    sql, args = connection.calls[-1]
    assert sql.startswith("DELETE FROM wreath_rate_limit")
    assert args == (3600.0,)


async def test_middleware_awaits_a_store_without_a_sync_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, _connection = _pg_store(monkeypatch, [(2.0, True), (0.0, False)])
    middleware = RateLimitMiddleware(limit=3, window=3.0, store=store)
    # No try_acquire on this store, so the awaiting hook must have been bound.
    assert middleware.before.__name__ == "_before_remote"
    await database.start()

    app = Wreath()
    app.add_middleware(middleware, priority=-5)

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        assert (await client.get("/")).status == 200
        assert (await client.get("/")).status == 429


def test_middleware_binds_the_sync_path_for_the_memory_store() -> None:
    middleware = RateLimitMiddleware(limit=1, window=1.0)
    assert middleware.before.__name__ == "_before_local"
