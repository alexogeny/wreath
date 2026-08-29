from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _replaydrive import (  # `tests/` is on sys.path
    Supervisor,
    keyed_store,
    until,
)

import wreath
from wreath._replay_adapters import AdapterFault, DatabaseDouble, scripted_row
from wreath.postgres import Connection, PostgresError
from wreath.replay import (
    CanonicalRequest,
    FaultSchedule,
    ReplayAdapters,
    fault_corpus,
    replay_endpoint_plan,
)

CORPUS = fault_corpus()


def _schedule(name: str) -> FaultSchedule:
    """Round-tripped through its container, as the corpus is meant to be used."""
    return FaultSchedule.from_bytes(CORPUS[name].to_bytes())


def _double(name: str, target: str = "main") -> DatabaseDouble:
    return ReplayAdapters.from_faults(_schedule(name).adapter_faults).databases[target]


def _db_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/rows")
    async def rows(request: wreath.Request, db: Connection) -> dict:
        return {"n": len(await db.fetch("SELECT id FROM things"))}

    return app


@pytest.mark.asyncio
async def test_a_dropped_statement_leaves_the_connection_usable() -> None:
    double = _double("adapter-connection_drop")
    connection = await double.acquire("read")
    with pytest.raises(PostgresError):
        await connection.fetch("SELECT 1")
    assert await connection.fetch("SELECT 2") == []  # the lease still works


@pytest.mark.asyncio
async def test_a_failed_connection_latches_and_answers_every_later_call() -> None:
    double = _double("adapter-connection_failed")
    connection = await double.acquire("write")
    with pytest.raises(Exception) as first:  # identity is the assertion
        await connection.fetch("SELECT 1")
    with pytest.raises(Exception) as again:
        await connection.fetch("SELECT 2")
    assert again.value is first.value
    with pytest.raises(Exception) as third:
        await connection.execute("ROLLBACK")
    assert third.value is first.value
    assert connection.failed


@pytest.mark.asyncio
async def test_a_failed_connection_still_reaches_an_owned_status() -> None:
    double = _double("adapter-connection_failed")
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/rows"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked


@pytest.mark.asyncio
async def test_a_decode_failure_is_not_a_postgres_error() -> None:
    double = _double("adapter-decode_error")
    connection = await double.acquire("read")
    with pytest.raises(ValueError) as caught:  # the type is the point
        await connection.fetch("SELECT tags FROM things")
    assert not isinstance(caught.value, PostgresError)


@pytest.mark.asyncio
async def test_a_decode_failure_is_not_caught_by_the_drivers_own_guard() -> None:
    from wreath.jobs import JobRunner

    double = _double("adapter-decode_error")
    runner = JobRunner(double, name="recovery")

    @runner.task("drive_me")
    async def drive_me(ctx: Any) -> None:  # pragma: no cover - never invoked
        return None

    with pytest.raises(ValueError):  # the type is the point
        await runner.enqueue("drive_me")
    # And the framework still gave the connection back on the way out.
    assert double.acquired == double.released == 1


@pytest.mark.asyncio
async def test_a_decode_failure_still_reaches_an_owned_status() -> None:
    double = _double("adapter-decode_error")
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/rows"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked


@pytest.mark.asyncio
async def test_a_poisoned_statement_works_once_and_then_never_again() -> None:
    double = _double("adapter-prepared_poison")
    connection = await double.acquire("read")
    assert await connection.fetch("SELECT $1::regclass", "things") == []
    with pytest.raises(TypeError):
        await connection.fetch("SELECT $1::regclass", "things")
    with pytest.raises(TypeError):
        await connection.fetch("SELECT   $1::regclass", "things")  # whitespace-normalised


@pytest.mark.asyncio
async def test_reconnecting_does_not_un_poison_a_statement() -> None:
    double = _double("adapter-prepared_poison")
    first = await double.acquire("read")
    await first.fetch("SELECT $1::regclass", "things")
    second = await double.acquire("read")
    with pytest.raises(TypeError):
        await second.fetch("SELECT $1::regclass", "things")


@pytest.mark.asyncio
async def test_a_different_statement_is_unaffected_by_the_poison() -> None:
    double = _double("adapter-prepared_poison")
    connection = await double.acquire("read")
    await connection.fetch("SELECT $1::regclass", "things")
    assert await connection.fetch("SELECT id FROM things") == []


@pytest.mark.asyncio
async def test_a_lost_claim_leaves_the_caller_holding_nothing() -> None:
    control = DatabaseDouble("main", results=(scripted_row({"key": "k"}),))
    assert await keyed_store(control).claim("k") is True

    faulted = _double("adapter-claim_lost")
    faulted.results = (scripted_row({"key": "k"}),)
    assert await keyed_store(faulted).claim("k") is False
    assert faulted.acquired == faulted.released == 1


@pytest.mark.asyncio
async def test_a_lost_claim_makes_idempotency_re_run_rather_than_replay() -> None:
    from wreath.policy.idempotency import PostgresIdempotencyStore

    double = _double("adapter-claim_lost")
    outcome, replay = await PostgresIdempotencyStore(double).reserve("k")
    assert outcome == "fresh"
    assert replay is None


@pytest.mark.asyncio
async def test_a_failed_singleton_returns_its_pool_slot() -> None:
    from wreath._locks import SingletonRunner

    double = DatabaseDouble("main", results=(True,))

    async def work() -> None:
        raise RuntimeError("the guarded work failed")

    runner = SingletonRunner(double, "leader", work, poll_interval=0.01, jitter=lambda base: 0.0)
    try:
        assert await until(lambda: double.acquired >= 3, within=1.0), (
            f"leadership stopped being contended after {double.acquired} rounds"
        )
    finally:
        await runner.stop()
    assert double.acquired == double.released, (
        "a failed work() kept its pool slot; the next round would block in "
        "acquire() and read as 'not the leader'"
    )
    assert runner.lead_errors >= 3, "a work() that fails every time went uncounted"


@pytest.mark.asyncio
async def test_a_singleton_survives_a_release_that_fails() -> None:
    from wreath._locks import SingletonRunner

    double = _double("adapter-release_error")
    double.results = ()  # try-lock answers None -> never the leader, keep polling

    async def work() -> None:  # pragma: no cover - leadership is never won here
        raise AssertionError("work() ran despite the lock not being held")

    runner = SingletonRunner(double, "leader", work, poll_interval=0.01, jitter=lambda base: 0.0)
    try:
        assert await until(lambda: double.acquired >= 3, within=1.0), (
            f"a failing release ended contention after {double.acquired} round(s)"
        )
        assert runner.release_errors >= 3, (
            "releases failed but nothing counted them -- a silent degradation is "
            f"the defect this replaced (release_errors={runner.release_errors})"
        )
        assert runner.lead_errors == 0, (
            "a failed release is not a failed leadership attempt; conflating them "
            "would hide a work() that never runs"
        )
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_a_clean_singleton_round_leaves_release_errors_at_zero() -> None:
    from wreath._locks import SingletonRunner

    double = _double("adapter-pool_timeout")  # a fault on acquire, not release
    double.acquire_fault = None  # ... and then disarmed: this round is clean
    double.results = ()

    runner = SingletonRunner(
        double,
        "leader",
        lambda: asyncio.sleep(0),
        poll_interval=0.01,
        jitter=lambda base: 0.0,
    )
    try:
        assert await until(lambda: double.acquired >= 3, within=1.0)
        assert runner.release_errors == 0, (
            f"a clean round counted a release error ({runner.release_errors})"
        )
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_a_failed_release_is_surfaced_through_the_request_pipeline() -> None:
    double = _double("adapter-release_error")
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/rows"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status in (200, 500)
    assert double.acquired == 1 and double.released == 1


def _bus(double: DatabaseDouble) -> Any:
    from wreath.messaging import MessageBus

    bus = MessageBus(double, name="recovery", poll_interval=60.0)

    async def handler(message: Any) -> None:
        return None

    bus.subscribe("things")(handler)
    return bus


def _runner(double: DatabaseDouble) -> Any:
    from wreath.jobs import JobRunner

    runner = JobRunner(double, name="recovery", concurrency=1, poll_interval=60.0)

    @runner.task("noop")
    async def noop(ctx: Any) -> None:  # pragma: no cover - never claimed
        return None

    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_bus, _runner], ids=["bus", "jobs"])
async def test_a_healthy_doorbell_does_not_reconnect(build: Any) -> None:
    double = DatabaseDouble("main")
    service = build(double)
    supervisor = Supervisor()
    try:
        await service.start(supervisor)
        assert await until(lambda: double.streams == 1)
        # If the stream returns, Doorbell increments its reconnect counter in
        # that same task turn, before waiting out any backoff.  Let the parked
        # pump settle without paying several real backoff periods.
        for _ in range(3):
            await asyncio.sleep(0)
        assert service.doorbell_reconnects == 0
        assert double.streams == 1
        assert len(double.listened) == 1
    finally:
        await supervisor.stop(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_bus, _runner], ids=["bus", "jobs"])
@pytest.mark.parametrize("name", ["adapter-notify_stream_end", "adapter-notify_stream_error"])
async def test_both_ways_a_stream_can_stop_lead_to_a_reopen(build: Any, name: str) -> None:
    double = _double(name)
    service = build(double)
    supervisor = Supervisor()
    try:
        await service.start(supervisor)
        assert await until(lambda: double.streams >= 2), (
            f"{name}: the doorbell did not reopen (streams={double.streams})"
        )
        assert len(double.listened) >= 2, (
            "reopened without re-LISTENing: a connection with no subscriptions "
            "delivers nothing, which is the outage continuing quietly"
        )
        assert service.doorbell_reconnects >= 1
    finally:
        await supervisor.stop(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_bus, _runner], ids=["bus", "jobs"])
async def test_a_doorbell_that_cannot_re_listen_keeps_trying(build: Any) -> None:
    double = _double("adapter-doorbell-drop-then-refused-reopen")
    service = build(double)
    supervisor = Supervisor()
    try:
        await service.start(supervisor)
        # Waited on the *doorbell's* counter, not on `acquired`: the job runner
        # leases connections for its workers and sweeper too, so an acquisition
        # count would clear this bar without the doorbell having retried once.
        assert await until(lambda: service.doorbell_reconnects >= 2), (
            f"gave up after a refused reopen: reconnects="
            f"{service.doorbell_reconnects} acquired={double.acquired}"
        )
        assert double.acquired >= 3, "retried without taking a new connection"
    finally:
        await supervisor.stop(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "adapter-server_error",
        "adapter-decode_error",
        "adapter-connection_failed",
        "adapter-pool_timeout",
    ],
)
async def test_a_failed_launch_seeds_no_task_to_watch(name: str) -> None:
    from wreath.jobs import JobRunner
    from wreath.progress import ProgressRegistry

    double = _double(name)
    double.results = (41,)
    registry = ProgressRegistry()
    runner = JobRunner(double, name="recovery", progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx: Any) -> None:  # pragma: no cover - never invoked
        return None

    with pytest.raises(Exception):  # noqa: B017 -- the failure is owned upstream
        await runner.launch("import_herd")
    assert registry.get("41") is None
    # Every lease taken was given back. Written as an inequality rather than
    # `not double.leaked` because an *acquire* fault never leases at all, and
    # the framework must not release a connection it never got.
    assert double.released == (0 if double.acquire_fault is not None else double.acquired)


@pytest.mark.asyncio
async def test_a_launch_that_fails_after_the_insert_still_seeds_nothing() -> None:
    from wreath.jobs import JobRunner
    from wreath.progress import ProgressRegistry

    double = DatabaseDouble("main", results=(41,), query_faults={1: AdapterFault.SERVER_ERROR})
    registry = ProgressRegistry()
    runner = JobRunner(double, name="recovery", progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx: Any) -> None:  # pragma: no cover - never invoked
        return None

    with pytest.raises(PostgresError):
        await runner.launch("import_herd")
    assert registry.get("41") is None
    assert double.acquired == double.released == 1


@pytest.mark.asyncio
async def test_a_successful_launch_does_seed_a_task() -> None:
    from wreath.jobs import JobRunner
    from wreath.progress import ProgressRegistry

    double = DatabaseDouble("main", results=(41,))
    registry = ProgressRegistry()
    runner = JobRunner(double, name="recovery", progress=registry)

    @runner.task("import_herd")
    async def import_herd(ctx: Any) -> None:  # pragma: no cover - never invoked
        return None

    handle = await runner.launch("import_herd")
    assert handle.task_id == "41"
    seeded = registry.get("41")
    assert seeded is not None and seeded.state == "queued"


# Thirteen introspection tests passed against a fake scripted with Python
# `str`/`int` rows -- rows no PostgreSQL would ever send -- while the default
# `validate_schema="error"` path had never once worked against a real server.
# A double that accepts what the driver rejects hides exactly the defects it
# exists to catch, so the refusals `_replay_adapters` already makes on the
# connection have to hold everywhere a caller can reach.


@pytest.mark.asyncio
async def test_a_transaction_scope_refuses_what_a_connection_refuses() -> None:
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
        await connection.fetch("SELECT * FROM t WHERE id = ANY($1)", [1, 2, 3])
    async with connection.transaction() as tx:
        with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
            await tx.fetch("SELECT * FROM t WHERE id = ANY($1)", [1, 2, 3])


@pytest.mark.asyncio
async def test_a_transaction_scope_refuses_two_commands_in_one_statement() -> None:
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    async with connection.transaction() as tx:
        with pytest.raises(PostgresError, match="multiple commands"):
            await tx.execute("UPDATE t SET x = 1; UPDATE t SET y = 2")


@pytest.mark.asyncio
async def test_a_fan_out_refuses_an_unbindable_argument_set() -> None:
    double = DatabaseDouble("main")
    connection = await double.acquire("read")
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
        await connection.map("fetch", "SELECT $1", [([1, 2],), ((3,),)])
    # And a fan-out of bindable values still works, so the refusal is not a ban.
    assert await connection.map("fetch", "SELECT $1", [(1,), (2,)]) == []


@pytest.mark.asyncio
async def test_a_scope_shares_its_connections_prepared_statement_cache() -> None:
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    async with connection.transaction() as tx:
        await tx.fetch("SELECT $1::regclass", "things")  # first execution: coerced
    with pytest.raises(TypeError, match="no binary encoder"):
        await connection.fetch("SELECT $1::regclass", "things")


def test_the_double_refuses_a_workload_the_pool_does_not_have() -> None:
    double = DatabaseDouble("main")
    with pytest.raises(ValueError, match="unknown PostgreSQL workload"):
        double.statement("s", "SELECT 1", workload="wirte")


def test_the_double_refuses_a_duplicate_statement_name() -> None:
    double = DatabaseDouble("main")
    double.statement("s", "SELECT 1", workload="read")
    with pytest.raises(ValueError, match="duplicate PostgreSQL statement"):
        double.statement("s", "SELECT 2", workload="read")


def test_the_double_refuses_an_empty_statement() -> None:
    double = DatabaseDouble("main")
    with pytest.raises(ValueError, match="statement name and SQL are required"):
        double.statement("s", "   ", workload="read")
