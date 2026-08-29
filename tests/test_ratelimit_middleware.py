from __future__ import annotations

from typing import Any

import pytest
from _doubles import PooledConnection

from wreath import Wreath
from wreath._native import _core
from wreath.policy import (
    HttpPolicy,
    MemoryRateLimitStore,
    PostgresRateLimitStore,
    RateLimitPolicy,
)
from wreath.testing import TestClient

_BUCKETS = [_core.TokenBucket]


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
    bucket = bucket_type(capacity=5.0, rate=1.0, max_entries=100)
    # Four complete table turnovers prove repeated eviction.
    for index in range(400):
        bucket.acquire(f"attacker-{index}", 1000.0 + index * 0.001)
    assert bucket.tracked <= 100

    # Even when every bucket is still actively limited, so none can be reclaimed.
    hot = bucket_type(capacity=2.0, rate=0.001, max_entries=50)
    for index in range(200):
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


class _ModelBucket:
    """The token bucket written from its definition, as the arithmetic oracle.

    `tokens = min(capacity, tokens + rate * elapsed)`, spend `cost` when there
    is enough and otherwise report how long the shortfall takes to refill. It
    tracks one key, keeps no table, and evicts nothing -- the parts the C exists
    to do well -- so a divergence over the sweep below is arithmetic, which is
    the half a bounded table cannot be allowed to perturb.
    """

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity, self.rate = capacity, rate
        self.tokens, self.last = capacity, None

    def acquire(self, now: float, cost: float = 1.0) -> float:
        if self.last is not None:
            self.tokens = min(self.capacity, self.tokens + self.rate * max(0.0, now - self.last))
        self.last = now if self.last is None else max(self.last, now)
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        return (cost - self.tokens) / self.rate


@pytest.mark.parametrize("bucket_type", _BUCKETS)
def test_bucket_refill_matches_the_arithmetic_over_a_long_sweep(bucket_type: Any) -> None:
    bucket = bucket_type(capacity=3.0, rate=2.0, max_entries=64)
    models = {f"k{index}": _ModelBucket(3.0, 2.0) for index in range(3)}
    now = 1000.0
    for step in range(400):
        key = f"k{step % 3}"
        now += 0.03
        if step == 200:
            now += 3600.0  # idle an hour: refill clamps at capacity
        elif step == 300:
            now -= 5.0  # a clock that went backwards mints nothing
        cost = 2.0 if step % 37 == 0 else 1.0
        assert bucket.acquire(key, now, cost) == pytest.approx(models[key].acquire(now, cost)), (
            step,
            key,
            now,
            cost,
        )
    assert bucket.tracked == len(models)


def _app(**kwargs: Any) -> Wreath:
    app = Wreath(http_policy=HttpPolicy(rate_limit=RateLimitPolicy(**kwargs)))

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
    app = Wreath(http_policy=HttpPolicy(rate_limit=RateLimitPolicy(limit=1, window=60.0)))

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
            RateLimitPolicy(**kwargs)


def test_a_store_cannot_be_shared_between_conflicting_policies() -> None:
    store = MemoryRateLimitStore()
    RateLimitPolicy(limit=10, window=60.0, store=store)
    with pytest.raises(ValueError, match="already configured"):
        RateLimitPolicy(limit=99, window=60.0, store=store)


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
    # `updated` is a last-touched mark, not a deadline: a bucket refills rather
    # than expiring, so only an idle purge retires one.
    assert "updated timestamptz NOT NULL" in sql


def test_postgres_store_refuses_a_second_policy() -> None:
    store = PostgresRateLimitStore(object())
    store.configure(3.0, 1.0)
    with pytest.raises(ValueError, match="already configured"):
        store.configure(3.0, 1.0)


def test_memory_store_refuses_a_second_policy_even_an_identical_one() -> None:
    store = MemoryRateLimitStore()
    store.configure(3.0, 1.0)
    with pytest.raises(ValueError, match="already configured"):
        store.configure(3.0, 1.0)


def test_a_reconfigure_can_never_hand_a_throttled_caller_a_full_bucket() -> None:
    store = MemoryRateLimitStore()
    store.configure(10.0, 1.0)
    for _ in range(10):
        store.try_acquire("alice", 1.0, 100.0)
    assert store.try_acquire("alice", 1.0, 100.0) > 0.0, "alice should be throttled"

    with pytest.raises(ValueError):
        store.configure(10.0, 1.0)

    # Still throttled: the refusal left the buckets alone.
    assert store.try_acquire("alice", 1.0, 100.0) > 0.0


# These pin the store's contract against a fake connection. The SQL itself is
# only meaningful against a real server: it was verified on Postgres 16, where
# 30 concurrent acquires against a capacity-3 bucket admitted exactly 3.


def _pg_store(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[float, bool]]
) -> tuple[Any, Any, PooledConnection]:
    """A store wired to a fake connection. The caller starts the database.

    The returned connection is a view onto the shared `calls` list rather than
    the only object the pool holds, so an assertion about what was executed
    sees every pooled connection's statements in order.
    """
    from wreath.postgres import Database, PoolConfig

    calls: list[tuple[str, tuple[object, ...]]] = []
    connection = PooledConnection(rows, calls)

    async def connect(dsn: str) -> PooledConnection:
        return PooledConnection(rows, calls)

    monkeypatch.setattr("wreath.postgres.connect", connect)
    database = Database("rl", "postgresql://x/y", pools={"write": PoolConfig(min_size=1)})
    return PostgresRateLimitStore(database), database, connection


def _acquires(connection: PooledConnection) -> list[tuple[str, tuple[object, ...]]]:
    # Startup prepares statements on the connection; keep only real acquires.
    # Named on the bucket table rather than on `INSERT INTO`, because schema
    # bootstrap writes its version marker with one of those too.
    return [
        call for call in connection.calls if call[0].startswith("INSERT INTO wreath_rate_limit")
    ]


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
    middleware = RateLimitPolicy(limit=3, window=3.0, store=store)
    # No try_acquire on this store, so the awaiting hook must have been bound.
    assert middleware._ingress.__name__ == "_before_remote"
    assert middleware._ingress_sync is None
    await database.start()

    app = Wreath(http_policy=HttpPolicy(rate_limit=middleware))

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        assert (await client.get("/")).status == 200
        assert (await client.get("/")).status == 429


def test_middleware_binds_the_sync_path_for_the_memory_store() -> None:
    middleware = RateLimitPolicy(limit=1, window=1.0)
    # The memory store exposes a synchronous before_sync hook (fused, no await)
    # and leaves the awaiting before hook unset.
    assert middleware._ingress is None
    assert middleware._ingress_sync.__name__ == "_before_local_sync"


# `wreath mutant` survived three controls here. Two are the same scenario from
# both ends: a request the key function cannot name. `principal_key` returns
# None for one with no identity and no client address, and `_identify` turns
# that None into the shared `UNKEYED` bucket -- because `None` is reserved for
# "exempt", and a request nobody can key must still be limited rather than
# waved through. Nothing exercised either half.


def _scope(client: tuple[str, int] | None) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "query_string": b"",
        "headers": [(b"host", b"example.test")],
    }
    if client is not None:
        scope["client"] = client
    return scope


async def _receive_body() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_principal_key_is_none_when_there_is_nobody_and_no_address() -> None:
    from wreath.policy import principal_key
    from wreath.request import Request

    assert principal_key(Request(_scope(("203.0.113.7", 5000)), _receive_body)) == (
        "ip:203.0.113.7"
    )
    assert principal_key(Request(_scope(None), _receive_body)) is None


async def test_a_request_nobody_can_key_is_still_limited() -> None:
    middleware = RateLimitPolicy(limit=1, window=60.0, key=lambda request: None)
    from wreath.request import Request

    first = middleware._ingress_sync(Request(_scope(("203.0.113.7", 5000)), _receive_body))
    second = middleware._ingress_sync(Request(_scope(("198.51.100.9", 5000)), _receive_body))
    assert first is None  # admitted
    assert second is not None  # ... and the next one is refused,
    assert second.status == 429  # sharing one bucket rather than none


async def test_an_exempt_request_is_the_only_thing_that_skips_the_bucket() -> None:
    middleware = RateLimitPolicy(
        limit=1,
        window=60.0,
        key=lambda request: None,
        exempt=lambda request: True,
    )
    from wreath.request import Request

    for _ in range(5):
        assert (
            middleware._ingress_sync(Request(_scope(("203.0.113.7", 5000)), _receive_body)) is None
        )


def test_a_limit_that_admits_nobody_is_refused() -> None:
    for limit in (0, -1):
        with pytest.raises(ValueError, match="limit must be positive"):
            RateLimitPolicy(limit=limit, window=60.0)


# Each was covered by a test that could not tell the clause was there: the
# exemption test only ever passed a predicate that says *yes*, and the keying
# test only ever passed a request with nobody to identify. A control needs the
# case where it answers the other way.


def test_an_exemption_that_says_no_still_limits() -> None:
    from wreath.policy import RateLimitPolicy
    from wreath.request import Request

    policy = RateLimitPolicy(limit=1, window=60.0, exempt=lambda request: False)
    request = Request(_scope(("203.0.113.7", 5000)), _receive_body)
    # Not `None`: `None` is the "do not limit" answer, so a predicate that
    # declined the exemption must produce a real key. The default key is the
    # client address, unprefixed.
    assert policy._identify(request) == "203.0.113.7"


def test_principal_key_names_the_caller_rather_than_the_address() -> None:
    from wreath.policy import principal_key
    from wreath.request import Request

    class _Identity:
        type = "user"
        id = "alice"

    request = Request(_scope(("203.0.113.7", 5000)), _receive_body)
    # Through the private slot the `identity` stage hook writes: the property is
    # read-only on purpose, and going around it here is the point -- the test
    # needs an identified request without standing up authentication.
    request._identity = _Identity()  # type: ignore[assignment]
    assert principal_key(request) == "user:alice"
    # And the fallback still answers for an anonymous one, so this pins the
    # branch rather than replacing it.
    assert principal_key(Request(_scope(("203.0.113.7", 5000)), _receive_body)) == (
        "ip:203.0.113.7"
    )


def test_clearing_a_store_that_was_never_configured_is_a_no_op() -> None:
    from wreath.policy import MemoryRateLimitStore

    MemoryRateLimitStore().clear()  # must not raise
