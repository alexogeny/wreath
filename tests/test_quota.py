from __future__ import annotations

from typing import Any

import pytest
from _doubles import PooledConnection

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies
from wreath._auth.principal import human
from wreath._auth.requirements import add_authenticated
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize
from wreath.policy import HttpPolicy, RateLimitPolicy, TieredRateLimitPolicy
from wreath.policy.ratelimit import MemoryRateLimitStore
from wreath.quota import MemoryQuotaStore, PostgresQuotaStore, Quota, Quotas
from wreath.testing import TestClient


def test_a_quota_refuses_configuration_that_would_refuse_everything() -> None:
    with pytest.raises(ValueError, match="cost must not exceed the limit"):
        Quota(name="api", limit=1.0, period=60.0, cost=2.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0.0, "period": 60.0}, "limit must be positive"),
        ({"limit": 1.0, "period": 0.0}, "period must be positive"),
        ({"limit": 1.0, "period": 60.0, "cost": 0.0}, "cost must be positive"),
    ],
)
def test_a_quota_refuses_a_non_positive_number(kwargs: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Quota(name="api", **kwargs)


def test_an_unnamed_quota_is_refused() -> None:
    with pytest.raises(ValueError, match="a quota needs a name"):
        Quota(name="", limit=1.0, period=60.0)


def test_two_quotas_cannot_share_a_name() -> None:
    quotas = Quotas()
    quotas.declare("api", limit=10.0, period=60.0)
    with pytest.raises(ValueError, match="already declared"):
        quotas.declare("api", limit=20.0, period=60.0)


def test_an_undeclared_meter_names_what_was_declared() -> None:
    quotas = Quotas()
    quotas.declare("api", limit=10.0, period=60.0)
    with pytest.raises(KeyError, match="declared: api"):
        quotas.meter("apu")


def test_an_empty_registry_says_none_rather_than_nothing() -> None:
    with pytest.raises(KeyError, match="declared: none"):
        Quotas().meter("api")


def test_the_counter_admits_to_the_limit_then_reports_the_reset() -> None:
    store = MemoryQuotaStore()
    store.configure(Quota(name="api", limit=2.0, period=10.0))

    assert store.try_spend("k", 0.0) == 0.0
    assert store.try_spend("k", 0.0) == 0.0
    assert store.try_spend("k", 0.0) == 10.0  # refused, seconds to reset
    assert store.peek("k") == 2.0  # and nothing was spent on it


def test_the_reset_shrinks_as_the_period_elapses() -> None:
    store = MemoryQuotaStore()
    store.configure(Quota(name="api", limit=1.0, period=100.0))
    store.try_spend("k", 10.0)

    assert store.try_spend("k", 90.0) == pytest.approx(10.0)


def test_a_new_period_restores_the_allowance_with_no_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quotas = Quotas()
    meter = quotas.declare("api", limit=1.0, period=10.0)
    request = _Requesting("ada")

    monkeypatch.setattr("wreath.quota._wall_time", lambda: 5.0)
    first = meter.key(request)
    monkeypatch.setattr("wreath.quota._wall_time", lambda: 15.0)
    second = meter.key(request)

    assert first != second
    assert meter.store.try_spend(first, 5.0) == 0.0
    assert meter.store.try_spend(first, 5.0) > 0.0  # spent, inside the period
    assert meter.store.try_spend(second, 15.0) == 0.0  # a new period, a new key


def test_the_key_names_the_quota_the_principal_and_the_period() -> None:
    quotas = Quotas()
    meter = quotas.declare("api", limit=1.0, period=10.0)

    assert meter.key(_Requesting("ada")) != meter.key(_Requesting("bo"))


def test_an_unidentified_request_has_no_key() -> None:
    quotas = Quotas()
    meter = quotas.declare("api", limit=1.0, period=10.0)

    assert meter.key(_Anonymous()) is None


def test_each_key_counts_separately() -> None:
    store = MemoryQuotaStore()
    store.configure(Quota(name="api", limit=1.0, period=10.0))

    assert store.try_spend("ada", 0.0) == 0.0
    assert store.try_spend("bo", 0.0) == 0.0
    assert store.try_spend("ada", 0.0) > 0.0


def test_configuring_a_store_twice_is_refused() -> None:
    store = MemoryQuotaStore()
    store.configure(Quota(name="api", limit=1.0, period=10.0))
    with pytest.raises(ValueError, match="already configured"):
        store.configure(Quota(name="api", limit=1.0, period=10.0))


def test_the_ceiling_evicts_the_fullest_counter() -> None:
    store = MemoryQuotaStore(max_entries=2)
    store.configure(Quota(name="api", limit=5.0, period=10.0))
    store.try_spend("heavy", 0.0)
    store.try_spend("heavy", 0.0)
    store.try_spend("light", 0.0)

    store.try_spend("new", 0.0)

    assert store.peek("heavy") == 0.0  # evicted
    assert store.peek("light") == 1.0  # kept


def test_a_store_used_before_configure_says_so() -> None:
    with pytest.raises(RuntimeError, match="before configure"):
        MemoryQuotaStore().try_spend("k", 0.0)


def test_a_store_with_no_room_for_a_key_is_refused() -> None:
    with pytest.raises(ValueError, match="max_entries must be positive"):
        MemoryQuotaStore(max_entries=0)


def test_spending_again_on_a_known_key_evicts_nothing() -> None:
    store = MemoryQuotaStore(max_entries=2)
    store.configure(Quota(name="api", limit=9.0, period=10.0))
    store.try_spend("ada", 0.0)
    for _ in range(3):
        store.try_spend("bo", 0.0)  # bo is the fullest, so bo is what an
        # unguarded eviction would take
    store.try_spend("ada", 0.0)

    assert store.peek("ada") == 2.0
    assert store.peek("bo") == 3.0
    assert store.tracked == 2


# The same offline coverage `PostgresRateLimitStore` has: construction, the
# identifier guard, the offered DDL, and the configure-once rule are all
# decidable without a server. Only `spend` and `used` need one, and they are
# gated on WREATH_TEST_POSTGRES_DSN exactly as `acquire` is.


def test_the_postgres_store_rejects_an_unsafe_table_name() -> None:
    for bad in ("a; DROP TABLE users", "", "1abc", "sch.ema"):
        with pytest.raises(ValueError, match="plain SQL identifier"):
            PostgresQuotaStore(object(), table=bad)


def test_the_postgres_schema_is_offered_as_a_migration() -> None:
    sql = PostgresQuotaStore(object(), table="allowances").schema_sql()

    assert "CREATE TABLE IF NOT EXISTS allowances" in sql
    assert "key text PRIMARY KEY" in sql
    # `updated` is a last-touched mark rather than a deadline: the period lives
    # in the key, so a row is retired by housekeeping, never by expiry.
    assert "updated timestamptz NOT NULL" in sql


def test_the_postgres_store_refuses_a_second_allowance() -> None:
    store = PostgresQuotaStore(object())
    store.configure(Quota(name="api", limit=1.0, period=60.0))
    with pytest.raises(ValueError, match="already configured"):
        store.configure(Quota(name="api", limit=1.0, period=60.0))


def test_the_postgres_store_used_before_configure_says_so() -> None:
    store = PostgresQuotaStore(object())
    with pytest.raises(RuntimeError, match="before configure"):
        store.purge_pass()


def test_the_postgres_store_names_the_database_its_table_belongs_to() -> None:
    database = object()

    assert PostgresQuotaStore(database).schema_database is database


def _pg_quota(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> tuple[Any, Any, Any]:
    from wreath.postgres import Database, PoolConfig

    calls: list[tuple[str, tuple[Any, ...]]] = []
    connection = PooledConnection(rows, calls)

    async def connect(dsn: str) -> PooledConnection:
        return PooledConnection(rows, calls)

    monkeypatch.setattr("wreath.postgres.connect", connect)
    database = Database("q", "postgresql://x/y", pools={"write": PoolConfig(min_size=1)})
    return PostgresQuotaStore(database), database, connection


def _spends(connection: Any) -> list[tuple[str, tuple[Any, ...]]]:
    return [c for c in connection.calls if c[0].startswith("INSERT INTO wreath_quota")]


async def test_the_postgres_store_translates_the_row_into_a_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, connection = _pg_quota(monkeypatch, [(1.0, True), (2.0, False)])
    store.configure(Quota(name="api", limit=2.0, period=100.0))
    await database.start()

    assert await store.spend("k", 0.0) == 0.0
    # Refused: the answer is the time to the period edge, not a boolean.
    assert await store.spend("k", 40.0) == pytest.approx(60.0)

    sql, args = _spends(connection)[0]
    assert args == ("k", 1.0, 2.0)  # key, cost, limit
    # One statement: a read-then-write would race, and a raced quota overspends
    # in the direction that reaches an invoice.
    assert sql.count(";") == 0
    assert "ON CONFLICT (key) DO UPDATE" in sql


async def test_the_postgres_store_reads_the_used_total_without_spending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, connection = _pg_quota(monkeypatch, [(7.0,), None])
    store.configure(Quota(name="api", limit=10.0, period=100.0))
    await database.start()

    assert await store.used("k", 0.0) == 7.0
    # A key nobody has written is nothing consumed, not an error: the first
    # request of a new period reads exactly this.
    assert await store.used("k", 0.0) == 0.0
    assert _spends(connection) == []


def test_the_purge_pass_waits_two_periods() -> None:
    store = PostgresQuotaStore(object())
    store.configure(Quota(name="api", limit=1.0, period=3600.0))

    assert store.purge_pass() is not None


def _app(middleware: Any) -> Wreath:
    app = Wreath(http_policy=HttpPolicy(principal_rate_limit=middleware))

    @app.get("/llamas")
    @add_authenticated
    async def llamas(request: Any) -> dict:
        return {"ok": True}

    return app


def _metered(limit: float, *, rate: int = 1000) -> tuple[Wreath, Any]:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=limit, period=3600.0)
    app = _app(
        TieredRateLimitPolicy(tiers={"pro": (rate, 60.0)}, default=(rate, 60.0), quota=meter)
    )
    return app, meter


async def test_an_exhausted_quota_is_refused_with_the_reset() -> None:
    app, meter = _metered(1.0)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")

        assert (await ada.get("/llamas")).status == 200
        refused = await ada.get("/llamas")

    assert refused.status == 429
    assert refused.header("x-quota-limit") == "1"
    assert refused.header("x-quota-remaining") == "0"
    assert int(refused.header("retry-after")) > 0
    assert meter.refused == 1


async def test_the_quota_refusal_is_distinguishable_from_the_rate_refusal() -> None:
    app, _ = _metered(1.0)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        await ada.get("/llamas")
        quota = await ada.get("/llamas")

    body = quota.json()
    assert body["title"] == "Quota Exceeded"
    assert "api_calls" in body["detail"]
    assert quota.header("x-ratelimit-limit") is None


async def test_each_principal_gets_its_own_allowance() -> None:
    app, _ = _metered(1.0)
    async with TestClient(app) as client:
        assert (await client.acting_as("ada").get("/llamas")).status == 200
        assert (await client.acting_as("ada").get("/llamas")).status == 429
        assert (await client.acting_as("bo").get("/llamas")).status == 200


async def test_a_throttled_request_is_not_charged_against_the_quota() -> None:
    app, meter = _metered(10.0, rate=1)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        assert (await ada.get("/llamas")).status == 200
        throttled = await ada.get("/llamas")
        assert (await ada.get("/llamas")).status == 429

    assert throttled.json()["title"] == "Too Many Requests"
    # Three requests, one admitted: the meter counted exactly the admitted one.
    assert meter.store.peek(meter.key(_Requesting("ada"))) == 1.0
    assert meter.refused == 0


class _Requesting:
    """Just enough of a request for `QuotaMeter.key`."""

    def __init__(self, who: str) -> None:
        self.identity = Identity(id=who)


class _Anonymous:
    """A request nobody has identified."""

    identity = None


async def test_an_anonymous_request_is_not_metered() -> None:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)
    app = _app(TieredRateLimitPolicy(tiers={"pro": (99, 60.0)}, default=(99, 60.0), quota=meter))
    async with TestClient(app) as client:
        for _ in range(3):
            assert (await client.get("/llamas")).status == 401

    assert meter.store.tracked == 0


async def test_the_global_limiter_refuses_a_quota() -> None:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)
    with pytest.raises(ValueError, match="no principal to meter a quota"):
        RateLimitPolicy(limit=10, quota=meter)


def test_an_unidentified_request_spends_nothing() -> None:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)

    assert meter.spend_sync(_Anonymous()) is None
    assert meter.store.tracked == 0


async def test_an_unidentified_request_spends_nothing_on_the_awaiting_path() -> None:
    quotas = Quotas(store_factory=_AwaitingQuotaStore)
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)

    assert await meter.spend(_Anonymous()) is None
    assert meter.store.tracked == 0


class _AwaitingQuotaStore(MemoryQuotaStore):
    """A store with no `try_spend`, the shape `PostgresQuotaStore` has.

    Every deployment that means anything uses an awaiting store, so the hook
    that pairs a local token bucket with a remote meter is the *common*
    production path -- not an edge. Without this the whole awaiting branch went
    unreached while the suite stayed green.
    """

    def __getattribute__(self, name: str) -> Any:
        if name == "try_spend":
            raise AttributeError(name)
        return super().__getattribute__(name)

    async def spend(self, key: str, now: float) -> float:
        return MemoryQuotaStore.try_spend(self, key, now)


def _awaiting_metered(limit: float, *, rate: int = 1000) -> tuple[Wreath, Any]:
    quotas = Quotas(store_factory=_AwaitingQuotaStore)
    meter = quotas.declare("api_calls", limit=limit, period=3600.0)
    app = _app(
        TieredRateLimitPolicy(tiers={"pro": (rate, 60.0)}, default=(rate, 60.0), quota=meter)
    )
    return app, meter


def test_an_awaiting_quota_forces_the_awaiting_hook() -> None:
    quotas = Quotas(store_factory=_AwaitingQuotaStore)
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)
    limiter = RateLimitPolicy(limit=10, quota=meter, _route_scoped=True)

    assert meter.awaits
    assert limiter._ingress is not None
    assert limiter._ingress_sync is None


def test_two_local_stores_keep_the_synchronous_hook() -> None:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)
    limiter = RateLimitPolicy(limit=10, quota=meter, _route_scoped=True)

    assert not meter.awaits
    assert limiter._ingress is None
    assert limiter._ingress_sync is not None


async def test_an_awaiting_quota_is_charged_and_refuses() -> None:
    app, meter = _awaiting_metered(1.0)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")

        assert (await ada.get("/llamas")).status == 200
        refused = await ada.get("/llamas")

    assert refused.status == 429
    assert refused.json()["title"] == "Quota Exceeded"
    assert meter.refused == 1


async def test_an_awaiting_quota_is_not_charged_for_a_throttled_request() -> None:
    app, meter = _awaiting_metered(10.0, rate=1)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        assert (await ada.get("/llamas")).status == 200
        assert (await ada.get("/llamas")).status == 429

    assert meter.store.peek(meter.key(_Requesting("ada"))) == 1.0
    assert meter.refused == 0


async def test_the_awaiting_hook_without_a_quota_still_limits() -> None:
    limiter = RateLimitPolicy(limit=1, store=_AwaitingRateStore(), _route_scoped=True)
    app = _app(limiter)
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        assert (await ada.get("/llamas")).status == 200
        assert (await ada.get("/llamas")).status == 429


class _AwaitingRateStore:
    """A rate-limit store with no `try_acquire`, so the hook must await."""

    def __init__(self) -> None:
        self._inner = MemoryRateLimitStore()

    def configure(self, capacity: float, rate: float) -> None:
        self._inner.configure(capacity, rate)

    async def acquire(self, key: str, cost: float, now: float) -> float:
        return self._inner.try_acquire(key, cost, now)


def test_a_limiter_without_a_quota_offers_only_its_own_store() -> None:
    limiter = RateLimitPolicy(limit=10)

    assert len(limiter.schema_owners) == 1


async def test_the_quota_store_is_collected_for_the_schema() -> None:
    quotas = Quotas()
    meter = quotas.declare("api_calls", limit=1.0, period=3600.0)
    limiter = RateLimitPolicy(limit=10, quota=meter, _route_scoped=True)

    assert meter.store in limiter.schema_owners


READ_ONLY_POLICY = """
permit(principal, action, resource);
forbid(principal, action == Action::"write", resource)
when { context.quota.contains("read_only") };
"""


class Billing:
    """A states provider: the duck type the authorizer accepts."""

    def __init__(self, states: dict[str, set[str]]) -> None:
        self._states = states

    def states(self, identity: Any) -> frozenset[str]:
        return frozenset(self._states.get(identity.id, ()))

    def names(self) -> frozenset[str]:
        return frozenset({"read_only", "past_due"})


async def _status(source: str, *, identity: Identity, quota: Any = None) -> int:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: identity),
        CedarAuthorizer(engine=CedarPolicies(source), quota=quota),
    )

    @app.get("/thing")
    @authorize(action="write", resource=lambda request: 'Doc::"d"')
    async def thing(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/thing", headers={"authorization": "Bearer t"})
    return response.status


async def test_a_declared_state_reaches_the_policy() -> None:
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))

    assert await _status(READ_ONLY_POLICY, identity=Identity(id="ada"), quota=quotas) == 403


async def test_a_caller_without_the_state_is_unaffected() -> None:
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))

    assert await _status(READ_ONLY_POLICY, identity=Identity(id="bo"), quota=quotas) == 200


def test_a_policy_reading_the_states_without_a_provider_is_refused() -> None:
    with pytest.raises(ValueError) as refusal:
        CedarAuthorizer(engine=CedarPolicies(READ_ONLY_POLICY))

    # The message, not the key name: every refusal in this module contains
    # "context.quota", including the misspelled-state one below.
    assert "grants access instead of denying it" in str(refusal.value)
    assert "the forbid never fires, and the request is allowed" in str(refusal.value)


def test_the_states_are_read_in_an_unknowable_shape_and_still_refused() -> None:
    source = """
    permit(principal, action, resource)
    unless { context.quota.isEmpty() };
    """
    with pytest.raises(ValueError, match="grants access instead of denying it"):
        CedarAuthorizer(engine=CedarPolicies(source))


async def test_no_policy_reading_the_states_needs_no_provider() -> None:
    source = "permit(principal, action, resource);"
    CedarAuthorizer(engine=CedarPolicies(source))

    assert await _status(source, identity=Identity(id="ada")) == 200


async def test_a_configured_provider_still_forbids() -> None:
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))
    CedarAuthorizer(engine=CedarPolicies(READ_ONLY_POLICY), quota=quotas)

    assert await _status(READ_ONLY_POLICY, identity=Identity(id="ada"), quota=quotas) == 403


def test_an_engine_that_cannot_be_read_is_not_refused_on_its_silence() -> None:

    class Opaque:
        def is_authorized(self, **request: object) -> bool:
            return True

    CedarAuthorizer(engine=Opaque())


async def test_a_grant_shaped_fact_is_still_switchable_off() -> None:
    source = """
    permit(principal, action, resource)
    when { context.flags.contains("beta") };
    """
    CedarAuthorizer(engine=CedarPolicies(source))

    assert await _status(source, identity=Identity(id="ada")) == 403


async def test_a_policy_naming_an_unknown_state_is_refused_at_startup() -> None:
    source = """
    permit(principal, action, resource)
    when { context.quota.contains("read_onyl") };
    """
    with pytest.raises(ValueError, match="read_onyl"):
        CedarAuthorizer(engine=CedarPolicies(source), quota=Quotas(states=Billing({})))


async def test_a_delegation_cannot_narrow_a_state_away() -> None:
    principal = human(Identity(id="ada"))
    delegated = principal.narrow(actor="agent", scope=("write",), ttl=60.0)
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))

    assert await _status(READ_ONLY_POLICY, identity=delegated.identity, quota=quotas) == 403


async def test_the_state_is_resolved_once_per_request() -> None:
    calls = 0

    class Counting(Billing):
        def states(self, identity: Any) -> frozenset[str]:
            nonlocal calls
            calls += 1
            return super().states(identity)

    source = """
    permit(principal, action, resource)
    when { context.quota.contains("past_due") };
    forbid(principal, action == Action::"write", resource)
    when { context.quota.contains("read_only") };
    """
    quotas = Quotas(states=Counting({"ada": {"past_due"}}))
    await _status(source, identity=Identity(id="ada"), quota=quotas)

    assert calls == 1


async def test_a_plain_callable_is_a_states_provider() -> None:
    quotas = Quotas(states=lambda identity: {"read_only"})

    with pytest.warns(RuntimeWarning, match="cannot enumerate"):
        status = await _status(READ_ONLY_POLICY, identity=Identity(id="ada"), quota=quotas)

    assert status == 403


def test_a_provider_that_cannot_enumerate_offers_no_vocabulary_at_all() -> None:
    assert not hasattr(Quotas(states=lambda identity: frozenset()), "names")
    assert hasattr(Quotas(states=Billing({})), "names")
    assert hasattr(Quotas(), "names")  # deliberately off: refuses a typo


async def test_a_provider_that_cannot_enumerate_warns_rather_than_refusing() -> None:
    with pytest.warns(RuntimeWarning, match="cannot enumerate"):
        CedarAuthorizer(
            engine=CedarPolicies(READ_ONLY_POLICY),
            quota=Quotas(states=lambda identity: frozenset()),
        )


async def test_an_anonymous_request_resolves_the_fact_to_nothing() -> None:
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))
    authorizer = CedarAuthorizer(engine=CedarPolicies(READ_ONLY_POLICY), quota=quotas)

    class FakeState:
        def get(self, key: str, default: Any = None) -> Any:
            return getattr(self, key, default)

        def __setattr__(self, key: str, value: Any) -> None:
            object.__setattr__(self, key, value)

    class Anonymous:
        method = "GET"
        path = "/x"
        identity = None

        def __init__(self) -> None:
            self.state = FakeState()

    assert authorizer.facts_for(Anonymous())["quota"] == frozenset()


def test_a_caller_the_provider_does_not_know_has_no_states() -> None:
    quotas = Quotas(states=Billing({"ada": {"read_only"}}))

    assert quotas.for_identity(Identity(id="nobody")) == frozenset()


def test_no_states_provider_resolves_to_nothing() -> None:
    assert Quotas().for_identity(Identity(id="ada")) == frozenset()
    assert Quotas().names() == frozenset()


async def test_a_key_no_policy_names_is_never_resolved() -> None:
    calls = 0

    class Counting(Billing):
        def states(self, identity: Any) -> frozenset[str]:
            nonlocal calls
            calls += 1
            return super().states(identity)

    source = "permit(principal, action, resource);"
    quotas = Quotas(states=Counting({"ada": {"read_only"}}))
    await _status(source, identity=Identity(id="ada"), quota=quotas)

    assert calls == 0
