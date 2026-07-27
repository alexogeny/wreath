"""What each region's owned recovery actually is, region by region.

The corpus properties in `test_replay_corpus_properties.py` hold every schedule
to two universal rules -- no hang, no silence. Universal rules cannot say what a
*particular* fault should do, and "a region must name a failure the owned code
answers differently from its neighbours" is only a real bar if somebody checks
the answers are different. That is this file.

Each test below drives a real subsystem, not a synthetic caller, and asserts the
specific recovery: which connection came back, which counter moved, which state
was recorded, and -- for the pairs that exist precisely because they are easy to
confuse -- that two neighbouring regions do *not* produce the same answer.
"""

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


# --- connection_failed vs connection_drop: the pair that must stay apart ------


@pytest.mark.asyncio
async def test_a_dropped_statement_leaves_the_connection_usable() -> None:
    """`CONNECTION_DROP` is one statement's failure, and the lease survives it.

    Half of the distinction. A caller holding this connection may reasonably
    re-issue on it, and the region would be a lie if the double refused.
    """
    double = _double("adapter-connection_drop")
    connection = await double.acquire("read")
    with pytest.raises(PostgresError):
        await connection.fetch("SELECT 1")
    assert await connection.fetch("SELECT 2") == []  # the lease still works


@pytest.mark.asyncio
async def test_a_failed_connection_latches_and_answers_every_later_call() -> None:
    """`CONNECTION_FAILED` ends the *lease*, identically, for everything after.

    The other half. The error object is the same one -- not merely an equal
    message -- because that is what a fan-out failure looks like from a caller's
    seat: one failure, delivered to everybody, rather than a fresh failure per
    attempt. Retrying on this connection can only produce the answer it already
    has.
    """
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
    """Through the real pipeline: an owned 500, and the lease comes back.

    A latching failure is exactly the shape that could strand a lease -- every
    attempt to tidy up raises the same error -- so release is the assertion.
    """
    double = _double("adapter-connection_failed")
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/rows"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked


# --- decode_error: the statement worked and the answer did not ----------------


@pytest.mark.asyncio
async def test_a_decode_failure_is_not_a_postgres_error() -> None:
    """The whole reason this is a region rather than a flavour of server error.

    The live defect raised `ValueError: text-format array decoding is not
    supported` on a cold catalog path in a *default* configuration. Every
    `except PostgresError` in this tree steps around a `ValueError`, so a corpus
    that raised a server error here would have proved a recovery that does not
    exist for the failure that actually happened.
    """
    double = _double("adapter-decode_error")
    connection = await double.acquire("read")
    with pytest.raises(ValueError) as caught:  # the type is the point
        await connection.fetch("SELECT tags FROM things")
    assert not isinstance(caught.value, PostgresError)


@pytest.mark.asyncio
async def test_a_decode_failure_is_not_caught_by_the_drivers_own_guard() -> None:
    """The concrete consequence, spelled out against real code.

    `JobRunner._record_drive_failure` suppresses `(PostgresError, TimeoutError,
    OSError)` -- a deliberately narrow guard, and correct for what it names. A
    decode failure is none of the three, so it propagates. This test exists so
    that the day someone widens or narrows that tuple, the *reason* the region
    is separate is written down next to a failing assertion rather than in a
    comment nobody reads.
    """
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


# --- prepared_poison: the failure that does not exist on the first call -------


@pytest.mark.asyncio
async def test_a_poisoned_statement_works_once_and_then_never_again() -> None:
    """The region's entire content, in three calls.

    A smoke test that runs each statement once passes. A second call is the
    cheapest possible check and it is the one nothing was doing, which is how
    `$1::regclass` reached a default code path.
    """
    double = _double("adapter-prepared_poison")
    connection = await double.acquire("read")
    assert await connection.fetch("SELECT $1::regclass", "things") == []
    with pytest.raises(TypeError):
        await connection.fetch("SELECT $1::regclass", "things")
    with pytest.raises(TypeError):
        await connection.fetch("SELECT   $1::regclass", "things")  # whitespace-normalised


@pytest.mark.asyncio
async def test_reconnecting_does_not_un_poison_a_statement() -> None:
    """Which is why "it worked when I tried it" is true and useless.

    The inference lives with the statement text, not with the connection, so a
    caller that takes a fresh lease and retries gets the same failure. A double
    that reset the poison per connection would have modelled a transient blip
    and re-blessed the retry loop that never terminates.
    """
    double = _double("adapter-prepared_poison")
    first = await double.acquire("read")
    await first.fetch("SELECT $1::regclass", "things")
    second = await double.acquire("read")
    with pytest.raises(TypeError):
        await second.fetch("SELECT $1::regclass", "things")


@pytest.mark.asyncio
async def test_a_different_statement_is_unaffected_by_the_poison() -> None:
    """Otherwise the region would model "the database broke", which it is not."""
    double = _double("adapter-prepared_poison")
    connection = await double.acquire("read")
    await connection.fetch("SELECT $1::regclass", "things")
    assert await connection.fetch("SELECT id FROM things") == []


# --- claim_lost: a successful statement that returns no row -------------------


@pytest.mark.asyncio
async def test_a_lost_claim_leaves_the_caller_holding_nothing() -> None:
    """`PostgresStore.claim` must report refusal, not carry on.

    A row comes back only when the insert succeeded or an expired row was
    reclaimed, so "a row came back" *is* the claim. Under `CLAIM_LOST` no row
    comes back and no exception is raised, which is the entire hazard: nothing
    fails, and a caller that treats "no error" as "I hold it" runs the
    critical section twice.

    Driven with a scripted row so the control genuinely claims -- without one,
    "no row" would be the control as well as the fault and this would be
    comparing silence with silence.
    """
    control = DatabaseDouble("main", results=(scripted_row({"key": "k"}),))
    assert await keyed_store(control).claim("k") is True

    faulted = _double("adapter-claim_lost")
    faulted.results = (scripted_row({"key": "k"}),)
    assert await keyed_store(faulted).claim("k") is False
    assert faulted.acquired == faulted.released == 1


@pytest.mark.asyncio
async def test_a_lost_claim_makes_idempotency_re_run_rather_than_replay() -> None:
    """The middleware's documented degradation, pinned so it stays deliberate.

    `PostgresIdempotencyStore.reserve` answers `fresh` when the claim is refused
    and the follow-up read finds nothing -- "deleted between the claim and the
    read; the safe reading is run it". So a lost claim costs *effect-once* and
    keeps *correctness*, which is the trade `docs/guides/idempotency.md`
    describes. What it must never do is answer `done` with a fabricated replay,
    or `in_flight` for a request nobody is running.
    """
    from wreath.middleware.idempotency import PostgresIdempotencyStore

    double = _double("adapter-claim_lost")
    outcome, replay = await PostgresIdempotencyStore(double).reserve("k")
    assert outcome == "fresh"
    assert replay is None


# --- release_error: the slot comes back anyway --------------------------------


@pytest.mark.asyncio
async def test_a_failed_singleton_returns_its_pool_slot() -> None:
    """The regression test for a leak that ended leadership for a whole process.

    `SingletonRunner` drops the connection when `work()` raises, so the advisory
    lock is released server-side. Setting the reference to `None` at the same
    time looked tidy and leaked the slot: `Pool.release` is what removes a
    connection from `_borrowed`, so a dropped-but-never-released connection
    pinned it forever and the *next* round blocked in `acquire()`. One `work()`
    failure therefore ended leadership -- invisibly, because a runner parked in
    `acquire()` looks exactly like one that is simply not the leader.

    Asserted as accounting rather than as a hang, because the accounting is what
    fails first and a hang is what it becomes.
    """
    from wreath._locks import SingletonRunner

    double = DatabaseDouble("main", results=(True,))

    async def work() -> None:
        raise RuntimeError("the guarded work failed")

    runner = SingletonRunner(
        double, "leader", work, poll_interval=0.01, jitter=lambda base: 0.0
    )
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
    """A failing release must degrade the round, not end the runner.

    Every other supervised loop in this tree agrees: `Doorbell._give_back`
    suppresses and moves on, `MessageBus._sweeper` counts and carries on. This
    one used to stop, and stop silently -- the exception escaped a `finally` and
    sat on a task nobody awaits until `stop()`, so it surfaced at GC as "Task
    exception was never retrieved" or not at all.

    The lock is scripted as *not* acquired, so the loop's own contract is to keep
    contending. A `work()` that returns would relinquish leadership voluntarily
    and end the loop for a legitimate reason, which would make a passing
    assertion here prove nothing about the release path.
    """
    from wreath._locks import SingletonRunner

    double = _double("adapter-release_error")
    double.results = ()  # try-lock answers None -> never the leader, keep polling

    async def work() -> None:  # pragma: no cover - leadership is never won here
        raise AssertionError("work() ran despite the lock not being held")

    runner = SingletonRunner(
        double, "leader", work, poll_interval=0.01, jitter=lambda base: 0.0
    )
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
    """The counter's control. Without this, "it moved" proves nothing -- a
    counter incremented unconditionally would satisfy the test above."""
    from wreath._locks import SingletonRunner

    double = _double("adapter-pool_timeout")  # a fault on acquire, not release
    double.acquire_fault = None  # ... and then disarmed: this round is clean
    double.results = ()

    runner = SingletonRunner(
        double, "leader", lambda: asyncio.sleep(0), poll_interval=0.01,
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
    """The framework decides, and it decides the same way twice.

    Not a 500 assertion: whether a release failure after a successful handler
    becomes an error response is the framework's call. What is not negotiable is
    that exactly one lease was taken and exactly one release attempted.
    """
    double = _double("adapter-release_error")
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/rows"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status in (200, 500)
    assert double.acquired == 1 and double.released == 1


# --- the doorbell, and the control that makes its counter mean something ------


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
    """The control the reconnect counter needs to be a signal at all.

    The double's un-faulted notification stream used to *return* immediately, so
    a healthy doorbell churned exactly as hard as one hitting
    `NOTIFY_STREAM_END` -- and every assertion about reconnecting was passing
    against a baseline that was already broken. Held open, one connection is
    taken, one `LISTEN` is issued, and the counter stays at zero.
    """
    double = DatabaseDouble("main")
    service = build(double)
    supervisor = Supervisor()
    try:
        await service.start(supervisor)
        await asyncio.sleep(0.15)  # several backoff periods, had it been retrying
        assert service.doorbell_reconnects == 0
        assert double.streams == 1
        assert len(double.listened) == 1
    finally:
        await supervisor.stop(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_bus, _runner], ids=["bus", "jobs"])
@pytest.mark.parametrize(
    "name", ["adapter-notify_stream_end", "adapter-notify_stream_error"]
)
async def test_both_ways_a_stream_can_stop_lead_to_a_reopen(build: Any, name: str) -> None:
    """One counter, two causes, and the silent one is the one that shipped.

    `notifications()` *returns* when its connection closes rather than raising,
    so a supervisor written around `except` sees nothing at all. Both the bus
    and the job runner are driven because the fix lives in a module they share
    and a regression would only show up in whichever one somebody tested.
    """
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
    """The compound region: the stream ends *and* the reopen is refused.

    Either fault alone is handled. Together they are the database that went away
    and came back refusing, and treating the failed reopen as terminal is how a
    transient outage becomes a permanent one. The assertion is the *counter
    moving twice*, because a supervisor that stops after the first retry passes
    any check that only asks whether it retried.
    """
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


# --- launch: a failed enqueue must not leave a phantom task -------------------


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
    """`launch` seeds progress as `queued` so a polling client sees a task
    rather than a 404. A launch that failed must seed nothing at all.

    A phantom `queued` entry is worse than the 404 it exists to avoid: the
    client streams a task that no worker will ever run, and the entry sits at
    0% until its TTL expires. Nothing else in the system would ever contradict
    it, because there is no row for a sweeper to find.
    """
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
    """The sharper case: the row committed and the doorbell notify failed.

    `enqueue` inserts and then issues `pg_notify` on the same connection, so a
    fault on the *second* statement leaves durable work queued while the caller
    sees a failure. That is survivable -- the row is the truth and a worker will
    poll it up -- but only if no half-built handle escapes and no progress entry
    claims a task id the caller never received.
    """
    from wreath.jobs import JobRunner
    from wreath.progress import ProgressRegistry

    double = DatabaseDouble(
        "main", results=(41,), query_faults={1: AdapterFault.SERVER_ERROR}
    )
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
    """The control. Without it the two tests above pass against a `launch` that
    never seeds anything, which is the assertion-with-nothing-to-check shape."""
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


# --- the double must not be more capable than the driver ----------------------
#
# Thirteen introspection tests passed against a fake scripted with Python
# `str`/`int` rows -- rows no PostgreSQL would ever send -- while the default
# `validate_schema="error"` path had never once worked against a real server.
# A double that accepts what the driver rejects hides exactly the defects it
# exists to catch, so the refusals `_replay_adapters` already makes on the
# connection have to hold everywhere a caller can reach.


@pytest.mark.asyncio
async def test_a_transaction_scope_refuses_what_a_connection_refuses() -> None:
    """`= ANY($1)` with a list raises before PostgreSQL is reached.

    The connection double refused it and the transaction double did not, so any
    statement written inside `async with connection.transaction()` was accepted
    here and rejected by the driver -- and the chunked-pass driver writes every
    one of its statements inside a transaction.
    """
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
        await connection.fetch("SELECT * FROM t WHERE id = ANY($1)", [1, 2, 3])
    async with connection.transaction() as tx:
        with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
            await tx.fetch("SELECT * FROM t WHERE id = ANY($1)", [1, 2, 3])


@pytest.mark.asyncio
async def test_a_transaction_scope_refuses_two_commands_in_one_statement() -> None:
    """The extended query protocol takes one command per statement, in or out of
    a transaction."""
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    async with connection.transaction() as tx:
        with pytest.raises(PostgresError, match="multiple commands"):
            await tx.execute("UPDATE t SET x = 1; UPDATE t SET y = 2")


@pytest.mark.asyncio
async def test_a_fan_out_refuses_an_unbindable_argument_set() -> None:
    """`map` binds each argument set separately, so each is refused separately.

    This path checked nothing at all: a fan-out carrying the one value that
    makes the driver raise ran cleanly against the double.
    """
    double = DatabaseDouble("main")
    connection = await double.acquire("read")
    with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
        await connection.map("fetch", "SELECT $1", [([1, 2],), ((3,),)])
    # And a fan-out of bindable values still works, so the refusal is not a ban.
    assert await connection.map("fetch", "SELECT $1", [(1,), (2,)]) == []


@pytest.mark.asyncio
async def test_a_scope_shares_its_connections_prepared_statement_cache() -> None:
    """A cast is poisoned for the *connection*, not for the scope that ran it.

    PostgreSQL caches the plan on the backend, so a statement first executed
    inside a transaction is already inferred when the next one outside it runs.
    A per-scope cache would make the second call look like a first call, which
    is the one thing this trap depends on being wrong about.
    """
    double = DatabaseDouble("main")
    connection = await double.acquire("write")
    async with connection.transaction() as tx:
        await tx.fetch("SELECT $1::regclass", "things")  # first execution: coerced
    with pytest.raises(TypeError, match="no binary encoder"):
        await connection.fetch("SELECT $1::regclass", "things")


def test_the_double_refuses_a_workload_the_pool_does_not_have() -> None:
    """A typo'd workload names a pool that does not exist, and `Database` says
    so. The double registering a statement against `"wirte"` and answering it
    happily is a test that passes for an application that cannot start."""
    double = DatabaseDouble("main")
    with pytest.raises(ValueError, match="unknown PostgreSQL workload"):
        double.statement("s", "SELECT 1", workload="wirte")


def test_the_double_refuses_a_duplicate_statement_name() -> None:
    """`Database.statement` raises on a name it already holds -- a guard that
    exists because two subsystems claiming one name is a real collision."""
    double = DatabaseDouble("main")
    double.statement("s", "SELECT 1", workload="read")
    with pytest.raises(ValueError, match="duplicate PostgreSQL statement"):
        double.statement("s", "SELECT 2", workload="read")


def test_the_double_refuses_an_empty_statement() -> None:
    double = DatabaseDouble("main")
    with pytest.raises(ValueError, match="statement name and SQL are required"):
        double.statement("s", "   ", workload="read")
