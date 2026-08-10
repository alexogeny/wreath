"""Long mutations: enqueue a durable job, watch it finish.

"This takes ninety seconds" is a universal problem with no good framework
answer. The usual shape is four moving parts a team wires by hand -- a queue, a
place to keep the percentage, a transport to push it, and an identifier tying
them together -- and the wiring is where it goes wrong: the status endpoint
404s until a worker picks the job up, the progress is stuck on whichever worker
wrote it, and nothing marks the task finished when the job dies.

Wreath owns the queue, the progress registry, the bus, and SSE, so the wiring is
the framework's. These tests pin what that buys: one identifier (the job id),
progress that any worker can answer for, and terminal states the runner sets
itself, because it is the only thing that actually knows.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _pgfidelity import check_for

from wreath.jobs import JobRunner
from wreath.progress import PROGRESS_CHANNEL, ProgressRegistry


class FakeConnection:
    def __init__(self, fetchval_result: Any = 1) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fetchval_result = fetchval_result
        #: Successive `fetchval` results, when a test needs them to differ.
        self.fetchval_script: list[Any] | None = None

    async def execute(self, sql, *args):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        if self.fetchval_script:
            return self.fetchval_script.pop(0)
        return self.fetchval_result

    async def fetch(self, sql, *args):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql, *args):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return None


class FakeDatabase:
    def __init__(self, fetchval_result: Any = 1) -> None:
        self.connection = FakeConnection(fetchval_result)

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        return None


class FakeBus:
    """Ephemeral fan-out: a publish reaches every listener, sender included."""

    def __init__(self, *, fail: bool = False) -> None:
        self.handlers: list[tuple[str, Any]] = []
        self.published: list[tuple[str, Any]] = []
        self.peers: list[FakeBus] = []
        self.fail = fail

    def subscribe(self, channel: str, **kwargs: Any):
        def register(handler):
            self.handlers.append((channel, handler))
            return handler

        return register

    async def publish(self, channel: str, payload: Any, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("the bus is down")
        self.published.append((channel, payload))
        for bus in (self, *self.peers):
            for subscribed, handler in bus.handlers:
                if subscribed == channel:
                    await handler(_BusMessage(channel, payload))


class _BusMessage:
    def __init__(self, channel: str, payload: Any) -> None:
        self.channel = channel
        self.payload = payload


def _elsewhere(bus: FakeBus) -> FakeBus:
    """Another worker's handle on the same channel, reachable both ways."""
    peer = FakeBus()
    peer.peers = [bus]
    bus.peers = [*bus.peers, peer]
    return peer


# --- progress across workers --------------------------------------------------


@pytest.mark.asyncio
async def test_progress_written_on_one_worker_is_readable_on_another() -> None:
    """The job runs on worker 3; the browser is connected to worker 1."""
    worker_1_bus = FakeBus()
    worker_1 = ProgressRegistry(worker_1_bus)
    worker_3 = ProgressRegistry(_elsewhere(worker_1_bus))

    worker_3.report("77", 42, "counting llamas")
    await asyncio.sleep(0)

    here = worker_1.get("77")
    assert here is not None
    assert (here.percent, here.message) == (42.0, "counting llamas")


@pytest.mark.asyncio
async def test_a_registry_ignores_the_echo_of_its_own_report() -> None:
    registry = ProgressRegistry(FakeBus())
    registry.report("77", 42, "counting llamas")
    await asyncio.sleep(0)
    assert registry.get("77").percent == 42.0     # applied once, not twice


@pytest.mark.asyncio
async def test_a_received_report_is_never_rebroadcast() -> None:
    """One hop out from the writer; adding workers cannot make a storm."""
    bus = FakeBus()
    here = ProgressRegistry(bus)
    peer = _elsewhere(bus)

    await peer.publish(
        PROGRESS_CHANNEL,
        {"task_id": "77", "percent": 10.0, "message": "", "state": "running",
         "error": None, "origin": "worker-3"},
    )
    for _ in range(5):
        await asyncio.sleep(0)

    assert here.get("77").percent == 10.0
    assert bus.published == []


@pytest.mark.asyncio
async def test_a_bus_that_is_down_still_records_progress_locally() -> None:
    registry = ProgressRegistry(FakeBus(fail=True))
    registry.report("77", 42)                     # must not raise
    await asyncio.sleep(0)
    assert registry.get("77").percent == 42.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", ["not-a-dict", {}, {"task_id": 7}, {"task_id": "77", "percent": "half"}]
)
async def test_a_malformed_progress_payload_is_ignored(payload: Any) -> None:
    bus = FakeBus()
    here = ProgressRegistry(bus)
    peer = _elsewhere(bus)
    await peer.publish(PROGRESS_CHANNEL, payload)
    assert here.get("77") is None


def test_a_registry_without_a_bus_is_purely_local() -> None:
    registry = ProgressRegistry()
    registry.report("77", 42)
    assert registry.get("77").percent == 42.0


# --- launching a job ----------------------------------------------------------


def _runner(database: FakeDatabase, **kw: Any) -> JobRunner:
    return JobRunner(database, name="work", **kw)


@pytest.mark.asyncio
async def test_launching_a_job_hands_back_a_watchable_handle() -> None:
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(fetchval_result=42), progress=progress)

    @runner.task("import_herd")
    async def import_herd(ctx, path):
        pass

    handle = await runner.launch("import_herd", "herd.csv")

    assert handle.task_id == "42"                 # the job id, not a new identity
    assert handle.as_dict() == {"task_id": "42", "state": "queued"}


@pytest.mark.asyncio
async def test_a_launched_job_is_visible_before_a_worker_touches_it() -> None:
    """Without this the client polls, gets a 404, and concludes it failed."""
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(fetchval_result=42), progress=progress)

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    await runner.launch("import_herd")
    seeded = progress.get("42")
    assert seeded is not None
    assert seeded.state == "queued"
    assert not seeded.terminal                    # queued is not an ending


@pytest.mark.asyncio
async def test_launching_without_a_registry_still_returns_a_handle() -> None:
    runner = _runner(FakeDatabase(fetchval_result=42))

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    assert (await runner.launch("import_herd")).task_id == "42"


@pytest.mark.asyncio
async def test_a_deduplicated_launch_returns_the_original_handle() -> None:
    """An idempotent submission must hand back the task already running."""
    database = FakeDatabase()
    runner = _runner(database, progress=ProgressRegistry())

    @runner.task("import_herd")
    async def import_herd(ctx):
        pass

    # The unique index dropped the insert; the surviving row is job 17.
    database.connection.fetchval_script = [None, 17]
    handle = await runner.launch("import_herd", key="nightly-2026-07-26")

    assert handle.task_id == "17"
    assert handle.state == "running"          # already under way, not freshly queued
    assert any("SELECT id" in sql for sql, _ in database.connection.calls)


# --- reporting from inside the job --------------------------------------------


@pytest.mark.asyncio
async def test_a_job_reports_progress_under_its_own_id() -> None:
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(), progress=progress)
    midway: list[Any] = []

    @runner.task("import_herd")
    async def import_herd(ctx):
        assert ctx.task_id == "42"                # the job id, no second identity
        ctx.report(50, "halfway up the mountain")
        midway.append(progress.get(ctx.task_id))

    await runner._run(_claim(runner, 42, "import_herd"))

    assert (midway[0].percent, midway[0].message, midway[0].state) == (
        50.0, "halfway up the mountain", "running"
    )


@pytest.mark.asyncio
async def test_a_finished_job_is_marked_done_without_being_asked() -> None:
    """The runner knows the job ended. Making the handler say so invites bugs."""
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(), progress=progress)

    @runner.task("import_herd")
    async def import_herd(ctx):
        ctx.report(90, "nearly")

    await runner._run(_claim(runner, 42, "import_herd"))

    finished = progress.get("42")
    assert finished.state == "done"
    assert finished.percent == 100.0


@pytest.mark.asyncio
async def test_a_job_that_will_retry_is_not_reported_as_failed() -> None:
    """A retry is not an ending; telling the client it failed would be a lie."""
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(), progress=progress)

    @runner.task("import_herd", retries=3)
    async def import_herd(ctx):
        ctx.report(30, "loading")
        raise RuntimeError("the mountain moved")

    await runner._run(_claim(runner, 42, "import_herd"))

    pending = progress.get("42")
    assert pending.state == "running"             # still going, from the outside
    assert pending.percent == 30.0                # right where it got to


@pytest.mark.asyncio
async def test_a_dead_lettered_job_is_reported_as_failed() -> None:
    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(), progress=progress)

    @runner.task("import_herd", retries=0)
    async def import_herd(ctx):
        raise RuntimeError("the mountain moved")

    await runner._run(_claim(runner, 42, "import_herd"))

    failed = progress.get("42")
    assert failed.state == "failed"
    assert "the mountain moved" in failed.error


@pytest.mark.asyncio
async def test_reporting_from_a_runner_with_no_registry_is_a_no_op() -> None:
    runner = _runner(FakeDatabase())

    @runner.task("import_herd")
    async def import_herd(ctx):
        ctx.report(50, "halfway")             # must not raise

    await runner._run(_claim(runner, 42, "import_herd"))


@pytest.mark.asyncio
async def test_a_jobs_progress_reaches_the_worker_serving_the_stream() -> None:
    """End to end: the job runs here, the browser is connected over there."""
    worker_3_bus = FakeBus()
    worker_3 = ProgressRegistry(worker_3_bus)
    worker_1 = ProgressRegistry(_elsewhere(worker_3_bus))
    runner = _runner(FakeDatabase(), progress=worker_3)

    @runner.task("import_herd")
    async def import_herd(ctx):
        ctx.report(50, "halfway up the mountain")

    await runner._run(_claim(runner, 42, "import_herd"))
    await asyncio.sleep(0)

    watched = worker_1.get("42")
    assert watched.state == "done" and watched.percent == 100.0


# --- the long mutation --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_graphql_mutation_can_hand_back_a_task_to_watch() -> None:
    """The long-mutation shape, end to end, with no new GraphQL machinery.

    A mutation whose return type is not an object type passes its value
    straight through, so a task id needs no synthetic `Task` type -- and an
    `ID` is what the client feeds to the progress stream anyway.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from orm.conftest import FakeDatabase as OrmDatabase
    from orm.conftest import Post, User

    from wreath.graphql import GraphQL
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session

    progress = ProgressRegistry()
    runner = _runner(FakeDatabase(fetchval_result=42), progress=progress)

    @runner.task("import_herd")
    async def import_herd(ctx, path):
        pass

    orm_database = OrmDatabase()
    registry = Registry(orm_database, [User, Post])
    api = GraphQL(registry, models=[User, Post])

    @api.mutation("importHerd", returns="ID")
    async def import_herd_mutation(info):
        handle = await runner.launch("import_herd", info.arguments["file"])
        return handle.task_id

    body = await api.run(
        'mutation { importHerd(file: "herd.csv") }', Session(registry, "read")
    )

    task_id = body["data"]["importHerd"]
    assert task_id == "42"
    # ... and that id is immediately watchable, before a worker has touched it.
    assert progress.get(task_id).state == "queued"


def _claim(runner: JobRunner, job_id: int, task: str):
    """A claimed job, as the worker loop would hand it to `_run`."""
    from wreath.jobs import _Claimed

    return _Claimed(
        id=job_id, task=task, args=[], tenant="", attempts=0,
        max_attempts=runner._tasks[task].max_attempts, fence=1, key=None,
    )
