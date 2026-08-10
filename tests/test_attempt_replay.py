"""Replaying a recorded job attempt: doubles, refusals, and the generated test.

The property that makes any of this safe to point at a production recording is
in `test_attempt_capture_live.py`, against a real queue. These cover what a
replay does with the recording it is handed.
"""

from __future__ import annotations

import pytest
from _doubles import SilentConnection

from wreath.jobs import JobRunner
from wreath.objects import ObjectError
from wreath.postgres import PostgresError
from wreath.recording import AttemptOutcome, AttemptRecord, BoundaryEvent
from wreath.replay import (
    AdapterFault,
    AdapterSeam,
    AttemptReplayError,
    DatabaseDouble,
    ReplayAdapters,
    ReplayError,
    attempt_adapters,
    attempt_fault_schedule,
    generate_attempt_test,
    open_attempt_recording,
    open_recording,
    recording_kind,
    replay_attempt,
)


class FakeDatabase:
    """The *live* database. Anything that reaches it during a replay is a bug."""

    name = "queue"

    def __init__(self) -> None:
        self.touched = 0

    async def acquire(self, workload):
        self.touched += 1
        return SilentConnection()

    async def release(self, workload, connection):
        pass


class Scope:
    def __init__(self, **registries) -> None:
        self._databases = registries.get("databases", {})
        self._http_clients = registries.get("clients", {})
        self._object_stores = registries.get("stores", {})


def _record(**overrides) -> AttemptRecord:
    fields = {
        "job_id": 4171,
        "queue": "work",
        "task": "send",
        "attempt": 4,
        "max_attempts": 5,
        "tenant": "acme",
        "dedup_key": "",
        "fence": 9,
        "trace_context": "",
        "boundaries": (),
        "outcome": str(AttemptOutcome.COMPLETED),
        "error_type": "",
        "error_message": "",
        "argument_count": 0,
    }
    fields.update(overrides)
    return AttemptRecord(**fields)


def _runner() -> JobRunner:
    return JobRunner(FakeDatabase(), name="work", lease=30.0)


# --- the fault schedule a recording implies ----------------------------------


def test_a_successful_boundary_contributes_no_fault():
    record = _record(
        boundaries=(BoundaryEvent(seam=int(AdapterSeam.DB_QUERY), target="main",
                                  coordinate=0),)
    )
    assert attempt_fault_schedule(record).adapter_faults == ()
    assert "main" in attempt_adapters(record).databases


def test_a_failed_boundary_becomes_the_fault_that_produces_its_exception():
    record = _record(
        boundaries=(
            BoundaryEvent(seam=int(AdapterSeam.DB_QUERY), target="main", coordinate=2,
                          error_type="PostgresError"),
        )
    )
    (fault,) = attempt_fault_schedule(record).adapter_faults
    assert fault.seam == int(AdapterSeam.DB_QUERY)
    assert fault.target == "main"
    assert fault.coordinate == 2
    assert fault.kind == str(AdapterFault.SERVER_ERROR)


def test_an_unmodelled_error_type_is_refused_rather_than_approximated():
    """Injecting the nearest fault would replay a different failure and report
    that it had reproduced the recorded one."""
    record = _record(
        boundaries=(
            BoundaryEvent(seam=int(AdapterSeam.DB_QUERY), target="main", coordinate=0,
                          error_type="ZeroDivisionError"),
        )
    )
    with pytest.raises(AttemptReplayError, match="no modelled fault"):
        attempt_fault_schedule(record)


def test_a_seam_with_no_fault_table_at_all_is_refused():
    """`DB_LISTEN` and `DB_CONNECTION` have no error -> fault inverse, so a
    boundary event naming one must not fall through as 'no fault here'."""
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.DB_LISTEN), "main", 0, "OperationalError"),
        )
    )
    with pytest.raises(AttemptReplayError, match="no modelled fault"):
        attempt_fault_schedule(record)


def test_a_boundary_that_succeeded_still_gets_its_double():
    """Otherwise the replay reaches whatever is really there. Only a *faulted*
    boundary comes back from `ReplayAdapters.from_faults`, so the successful
    ones have to be added on top."""
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.HTTP_REQUEST), "api", 0),
            BoundaryEvent(int(AdapterSeam.OBJECT_STORE), "objects", 0),
            BoundaryEvent(int(AdapterSeam.DB_TRANSACTION), "main", 0),
        )
    )
    adapters = attempt_adapters(record, databases=("queue",))
    assert set(adapters.clients) == {"api"}
    assert set(adapters.object_stores) == {"objects"}
    assert set(adapters.databases) == {"main", "queue"}


def test_the_same_error_name_means_different_faults_at_different_seams():
    """`TimeoutError` at a pool is not `TimeoutError` at an HTTP read."""
    db = _record(
        boundaries=(BoundaryEvent(int(AdapterSeam.DB_ACQUIRE), "main", 0, "TimeoutError"),)
    )
    http = _record(
        boundaries=(BoundaryEvent(int(AdapterSeam.HTTP_REQUEST), "api", 0, "TimeoutError"),)
    )
    assert attempt_fault_schedule(db).adapter_faults[0].kind == str(
        AdapterFault.POOL_TIMEOUT
    )
    assert attempt_fault_schedule(http).adapter_faults[0].kind == str(
        AdapterFault.READ_TIMEOUT
    )


# --- the replay --------------------------------------------------------------


async def test_a_recorded_completion_replays_as_a_completion():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        assert ctx.job_id == 4171 and ctx.attempt == 4 and ctx.fence == 9
        assert ctx.tenant == "acme"

    result = await replay_attempt(runner, _record())
    assert result.outcome == "completed"
    assert result.matched
    assert result.note is None


async def test_a_recorded_raise_replays_as_that_raise():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        raise ValueError("the token was already spent")

    record = _record(
        outcome=str(AttemptOutcome.RAISED),
        error_type="ValueError",
        error_message="the token was already spent",
    )
    result = await replay_attempt(runner, record)
    assert result.outcome == "raised"
    assert result.error_type == "ValueError"
    assert result.error_message == "the token was already spent"
    assert result.matched


async def test_a_divergence_is_reported_rather_than_asserted_away():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        return None  # the handler was fixed since the recording

    record = _record(outcome=str(AttemptOutcome.RAISED), error_type="ValueError")
    result = await replay_attempt(runner, record)
    assert not result.matched
    assert result.note == "the recording ended raised (ValueError); this replay ended completed"


async def test_the_same_outcome_with_a_different_exception_is_a_divergence():
    """`matched` is both halves. A replay that raises *something* where the
    recording raised is not a reproduction of the failure that was recorded."""
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        raise KeyError("a different failure entirely")

    record = _record(outcome=str(AttemptOutcome.RAISED), error_type="ValueError")
    result = await replay_attempt(runner, record)
    assert result.outcome == str(record.outcome)
    assert not result.matched
    assert result.note == (
        "the recording ended raised (ValueError); this replay ended raised (KeyError)"
    )


async def test_two_outcomes_that_both_raised_nothing_are_still_told_apart():
    """`matched` is both halves in the other direction too. A recording that
    was *cancelled at its deadline* and a replay that *completed* agree on the
    error type -- there isn't one -- and are not the same thing."""
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        return None

    record = _record(outcome=str(AttemptOutcome.DEADLINE_CANCELLED))
    result = await replay_attempt(runner, record)
    assert result.error_type == record.error_type == ""
    assert not result.matched
    assert result.note == (
        "the recording ended deadline_cancelled; this replay ended completed"
    )


async def test_a_seam_this_build_cannot_double_is_refused_before_anything_runs():
    """A recording from a newer build naming a boundary kind this one has no
    double for. The crossing that *succeeded* is the dangerous one: it carries
    no fault, so it would contribute nothing and the replay would reach the
    real resource."""
    runner = _runner()

    @runner.task("send")
    async def send(ctx):  # pragma: no cover - the refusal happens first
        raise AssertionError("the handler ran")

    with pytest.raises(AttemptReplayError, match="no boundary double for"):
        await replay_attempt(
            runner, _record(boundaries=(BoundaryEvent(99, "future", 0),))
        )


async def test_a_recorded_completion_that_now_raises_is_a_divergence_too():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        raise ValueError("a regression since the recording")

    result = await replay_attempt(runner, _record())
    assert not result.matched
    assert result.note == (
        "the recording ended completed; this replay ended raised (ValueError)"
    )


async def test_an_empty_dedup_key_is_no_key_rather_than_an_empty_string():
    """`JobContext.key` is `str | None`, and a handler that branches on it must
    see the same thing the live attempt saw. A recording writes `""` where the
    row held NULL, because the container has no null string."""
    runner = _runner()
    seen: list[object] = []

    @runner.task("send")
    async def send(ctx):
        seen.append(ctx.key)

    await replay_attempt(runner, _record(dedup_key=""))
    await replay_attempt(runner, _record(dedup_key="work:once"))
    assert seen == [None, "work:once"]


async def test_supplied_adapters_are_used_instead_of_the_derived_ones():
    """A test that wants to script a result -- rather than reproduce a fault --
    hands its own doubles in, and the recording's are not built over the top."""
    runner = _runner()
    scripted = ReplayAdapters(
        databases={"queue": DatabaseDouble("queue"), "main": DatabaseDouble("main")}
    )

    @runner.task("send")
    async def send(ctx):
        connection = await ctx_scope._databases["main"].acquire("read")
        await connection.fetch("SELECT 1")

    ctx_scope = Scope(databases={"main": object()})
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.DB_QUERY), "main", 0, "PostgresError"),
        ),
        outcome=str(AttemptOutcome.RAISED),
        error_type="PostgresError",
    )
    result = await replay_attempt(
        runner, record, scope=ctx_scope, adapters=scripted
    )
    assert result.adapters is scripted
    # The recording's fault was not injected, because these doubles carry none.
    assert result.outcome == "completed"
    assert not result.matched


async def test_an_unregistered_task_is_refused_by_name():
    runner = _runner()
    with pytest.raises(AttemptReplayError, match="not registered on runner 'work'"):
        await replay_attempt(runner, _record(task="vanished"))


async def test_the_recorded_arity_must_be_supplied():
    """The recording holds the argument *count* and none of the values, so a
    replay that quietly ran with no arguments would be replaying a different
    call."""
    runner = _runner()

    @runner.task("send")
    async def send(ctx, address, token):
        return None

    with pytest.raises(AttemptReplayError, match="carried 2 argument"):
        await replay_attempt(runner, _record(argument_count=2))

    result = await replay_attempt(
        runner, _record(argument_count=2), args=("alex@example.com", "tok")
    )
    assert result.matched


async def test_a_recorded_deadline_cancellation_replays_as_one():
    import asyncio

    runner = _runner()

    @runner.task("send", timeout=0.01)
    async def send(ctx):
        await asyncio.sleep(5)

    record = _record(outcome=str(AttemptOutcome.DEADLINE_CANCELLED))
    result = await replay_attempt(runner, record)
    assert result.outcome == "deadline_cancelled"
    assert result.matched


# --- one test per doubled boundary -------------------------------------------


async def test_the_database_boundary_is_doubled():
    runner = _runner()
    live = runner._db

    @runner.task("send")
    async def send(ctx):
        database = ctx_scope._databases["main"]
        connection = await database.acquire("read")
        try:
            await connection.fetch("SELECT 1")
            await connection.fetch("SELECT 2")
        finally:
            await database.release("read", connection)

    class Live:
        name = "main"

        async def acquire(self, workload):  # pragma: no cover - must not be reached
            raise AssertionError("the replay reached the real database")

        async def release(self, workload, connection):  # pragma: no cover
            raise AssertionError("the replay reached the real database")

    ctx_scope = Scope(databases={"main": Live()})
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.DB_QUERY), "main", 0),
            BoundaryEvent(int(AdapterSeam.DB_QUERY), "main", 1, "PostgresError"),
        ),
        outcome=str(AttemptOutcome.RAISED),
        error_type="PostgresError",
    )
    result = await replay_attempt(runner, record, scope=ctx_scope)
    assert result.matched
    double = result.adapters.databases["main"]
    # The owned pool lifecycle ran for real even though the query raised.
    assert double.acquired == 1 and double.released == 1
    assert not double.leaked
    assert live.touched == 0
    assert ctx_scope._databases["main"] is not double  # restored on exit


async def test_the_outbound_http_boundary_is_doubled():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        client = ctx_scope._http_clients["api"]
        await client._request_timed(
            "GET", "/x", headers=(), body=b"", idempotency_key=None
        )

    class Live:
        async def _request_timed(self, *a, **kw):  # pragma: no cover
            raise AssertionError("the replay reached the real upstream")

    ctx_scope = Scope(clients={"api": Live()})
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.HTTP_REQUEST), "api", 0, "ConnectionError"),
        ),
        outcome=str(AttemptOutcome.RAISED),
        error_type="ConnectionError",
    )
    result = await replay_attempt(runner, record, scope=ctx_scope)
    assert result.matched
    assert "api" in result.adapters.clients


async def test_the_object_store_boundary_is_doubled():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        await ctx_scope._object_stores["objects"].read("herd/2026.csv")

    class Live:
        async def read(self, key):  # pragma: no cover
            raise AssertionError("the replay reached the real object store")

    ctx_scope = Scope(stores={"objects": Live()})
    record = _record(
        boundaries=(
            BoundaryEvent(int(AdapterSeam.OBJECT_STORE), "objects", 0, "ObjectError"),
        ),
        outcome=str(AttemptOutcome.RAISED),
        error_type="ObjectError",
    )
    result = await replay_attempt(runner, record, scope=ctx_scope)
    assert result.matched
    store = result.adapters.object_stores["objects"]
    assert store.reads == 1


async def test_the_runners_own_database_is_doubled_even_with_no_recorded_boundary():
    """An attempt that never queried the queue still runs on a runner that
    would, and a replay must not reach it."""
    runner = _runner()
    live = runner._db

    @runner.task("send")
    async def send(ctx):
        connection = await runner._db.acquire("write")
        await connection.execute("DELETE FROM jobs")
        await runner._db.release("write", connection)

    result = await replay_attempt(runner, _record())
    assert result.matched
    assert live.touched == 0
    assert result.adapters.databases["queue"].acquired == 1
    assert runner._db is live  # restored


# --- the generated test ------------------------------------------------------


async def test_the_generated_test_is_runnable_and_names_what_it_replays(tmp_path):
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        raise ValueError("the token was already spent")

    record = _record(
        outcome=str(AttemptOutcome.RAISED),
        error_type="ValueError",
        error_message="the token was already spent",
        trace_context="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        boundaries=(BoundaryEvent(int(AdapterSeam.DB_QUERY), "main", 0, "PostgresError"),),
    )
    source = await generate_attempt_test(
        runner, record, target="herd.app:jobs", origin="work-4171-4.wfr1"
    )
    compile(source, "generated.py", "exec")
    assert "from herd.app import jobs" in source
    assert "await replay_attempt(jobs, RECORDED, args=())" in source
    assert "assert result.outcome == 'raised'" in source
    assert "assert result.error_type == 'ValueError'" in source
    assert "assert result.error_message == 'the token was already spent'" in source
    assert "assert result.matched is True" in source
    # The boundary state that produced the raise is written into the file, so
    # the doubles are rebuilt the same way on every run.
    assert (
        "BoundaryEvent(seam=1, target='main', coordinate=0, "
        "error_type='PostgresError')" in source
    )
    # Identity and cause are in the docstring, where somebody reading the file
    # six months later will find them.
    assert "Attempt 4 of 5 of task 'send', job 4171 on queue 'work'" in source
    assert "The worker held fence 9." in source
    assert (
        "Enqueued under trace context "
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01." in source
    )
    assert "from work-4171-4.wfr1" in source


async def test_a_generated_test_for_the_plainest_recording_says_only_what_it_knows():
    """Every optional clause off at once: a module-level runner, no origin, no
    tenant, no trace context, no arguments, no divergence and no message. Each
    of those is a ternary that would otherwise be pinned in one direction only.
    """
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        return None

    source = await generate_attempt_test(
        runner,
        _record(tenant="", trace_context="", argument_count=0),
        target="herd_queue",
        name="test_the_plain_one",
    )
    compile(source, "generated.py", "exec")
    assert "import herd_queue" in source
    assert "from herd_queue import" not in source
    assert "await replay_attempt(herd_queue.jobs, RECORDED, args=())" in source
    assert "async def test_the_plain_one() -> None:" in source
    assert "for tenant" not in source
    assert "The queue row carried no trace context" in source
    assert "Captured and generated by" in source  # no ' from <origin>'
    assert "argument(s) and the recording" not in source
    assert "Replay divergence:" not in source
    assert "result.error_message" not in source
    assert "assert result.matched is True" in source


async def test_the_generated_test_names_the_tenant_when_there_is_one():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        return None

    source = await generate_attempt_test(runner, _record(), target="herd:jobs")
    assert "job 4171 on queue 'work' for tenant 'acme'." in source
    assert "async def test_send_attempt_4() -> None:" in source


def test_the_generated_test_actually_passes(tmp_path, monkeypatch):
    """Generated, written to disk, and run by pytest -- not merely compiled.

    Synchronous on purpose: it drives a subprocess, which must not block an
    event loop, so the generation runs under its own `asyncio.run`.
    """
    import asyncio
    import subprocess
    import sys
    import textwrap

    runner_module = tmp_path / "recorded_queue.py"
    runner_module.write_text(
        textwrap.dedent(
            """
            from wreath.jobs import JobRunner


            class _Db:
                name = "queue"

                async def acquire(self, workload):
                    raise AssertionError("the live queue was reached")

                async def release(self, workload, connection):
                    raise AssertionError("the live queue was reached")


            jobs = JobRunner(_Db(), name="work", lease=30.0)


            @jobs.task("send")
            async def send(ctx):
                raise ValueError("the token was already spent")
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import recorded_queue

    record = _record(
        outcome=str(AttemptOutcome.RAISED),
        error_type="ValueError",
        error_message="the token was already spent",
    )
    source = asyncio.run(
        generate_attempt_test(
            recorded_queue.jobs, record, target="recorded_queue:jobs"
        )
    )
    generated = tmp_path / "test_generated_attempt.py"
    generated.write_text(source)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(generated), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


async def test_a_generated_test_says_when_the_replay_diverged_from_the_recording():
    runner = _runner()

    @runner.task("send")
    async def send(ctx):
        return None

    record = _record(outcome=str(AttemptOutcome.RAISED), error_type="ValueError")
    source = await generate_attempt_test(runner, record, target="herd:jobs")
    assert "Replay divergence:" in source
    assert "assert result.matched is False" in source


async def test_a_generated_test_says_the_arguments_were_withheld():
    runner = _runner()

    @runner.task("send")
    async def send(ctx, address, token):
        return None

    source = await generate_attempt_test(
        runner, _record(argument_count=2), target="herd:jobs",
        args=("alex@example.com", "tok"),
    )
    assert "The job carried 2 argument(s) and the recording" in source
    assert "args=('alex@example.com', 'tok')" in source


# --- which file is which ------------------------------------------------------


def _attempt_file(tmp_path, name="work-4171-4.wfr1", **overrides):
    import io

    from wreath._flight_schema import SCHEMA_VERSION, MetadataImage
    from wreath._recording_format import WFR1Writer

    buffer = io.BytesIO()
    writer = WFR1Writer(buffer, MetadataImage(SCHEMA_VERSION, *([()] * 11)))
    writer.write_attempt(_record(**overrides))
    writer.close()
    path = tmp_path / name
    path.write_bytes(buffer.getvalue())
    return path


def _transport_file(tmp_path):
    from wreath.replay import TransportRecording, TransportSegment

    path = tmp_path / "one-request.wtr1"
    path.write_bytes(
        TransportRecording(
            segments=(TransportSegment(kind=0, offset_us=0, data=b"GET / HTTP/1.1\r\n\r\n"),)
        ).to_bytes()
    )
    return path


def test_the_kind_is_read_from_the_magic_not_the_extension(tmp_path):
    assert recording_kind(str(_attempt_file(tmp_path))) == "attempt"
    assert recording_kind(str(_transport_file(tmp_path))) == "transport"
    stranger = tmp_path / "something.bin"
    stranger.write_bytes(b"NOPE" + b"\x00" * 64)
    with pytest.raises(ReplayError, match="unrecognized recording container"):
        recording_kind(str(stranger))


def test_each_reader_refuses_the_other_container_by_name(tmp_path):
    attempt, transport = _attempt_file(tmp_path), _transport_file(tmp_path)
    with pytest.raises(ReplayError, match="is a WFR1 flight recording"):
        open_recording(str(attempt))
    with pytest.raises(ReplayError, match="is a WTR1 transport recording"):
        open_attempt_recording(str(transport))
    stranger = tmp_path / "something.bin"
    stranger.write_bytes(b"NOPE" + b"\x00" * 64)
    for reader in (open_recording, open_attempt_recording):
        with pytest.raises(ReplayError, match="unrecognized recording container"):
            reader(str(stranger))


def test_each_reader_opens_its_own_container(tmp_path):
    assert open_attempt_recording(str(_attempt_file(tmp_path))).job_id == 4171
    assert open_recording(str(_transport_file(tmp_path))).segments


# --- the command ------------------------------------------------------------


def test_wreath_replay_to_test_takes_an_attempt_recording(tmp_path, monkeypatch, capsys):
    """One subcommand, two record kinds. The target names the *runner* here,
    which `load_application` would have refused for not being callable."""
    import io
    import textwrap

    from wreath._cli import main
    from wreath._flight_schema import SCHEMA_VERSION, MetadataImage
    from wreath._recording_format import WFR1Writer

    (tmp_path / "queue_under_test.py").write_text(
        textwrap.dedent(
            """
            from wreath.jobs import JobRunner


            class _Db:
                name = "queue"

                async def acquire(self, workload):
                    raise AssertionError("the live queue was reached")

                async def release(self, workload, connection):
                    raise AssertionError("the live queue was reached")


            jobs = JobRunner(_Db(), name="work", lease=30.0)


            @jobs.task("send")
            async def send(ctx):
                raise ValueError("the token was already spent")
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    buffer = io.BytesIO()
    writer = WFR1Writer(buffer, MetadataImage(SCHEMA_VERSION, *([()] * 11)))
    writer.write_attempt(
        _record(
            outcome=str(AttemptOutcome.RAISED),
            error_type="ValueError",
            error_message="the token was already spent",
        )
    )
    writer.close()
    recording = tmp_path / "work-4171-4.wfr1"
    recording.write_bytes(buffer.getvalue())

    output = tmp_path / "test_from_cli.py"
    assert main(
        ["replay", "to-test", "queue_under_test:jobs", str(recording), "-o", str(output)]
    ) == 0
    source = output.read_text()
    compile(source, str(output), "exec")
    assert "from queue_under_test import jobs" in source
    assert "assert result.error_type == 'ValueError'" in source

    capsys.readouterr()
    # A target that names nothing is refused with the spelling, not with an
    # AttributeError from four frames down.
    for target in ("queue_under_test:absent", "no_such_module:jobs"):
        assert main(["replay", "to-test", target, str(recording)]) != 0
        assert "could not load the job runner" in capsys.readouterr().err


def test_the_object_error_mapping_names_a_real_exception():
    """The fault tables are the inverse of the double's error constructors; if
    one grows a type the other does not know, this is where it shows."""
    from wreath._replay_adapters import _object_error

    assert isinstance(_object_error(AdapterFault.OBJECT_UNREACHABLE, "k"), ObjectError)
    assert type(_object_error(AdapterFault.OBJECT_UNREACHABLE, "k")).__name__ in {
        "ObjectError"
    }


def test_the_database_error_mapping_names_real_exceptions():
    from wreath._replay_adapters import _db_error

    for error_name, fault in [
        ("PostgresError", AdapterFault.SERVER_ERROR),
        ("OperationalError", AdapterFault.CONNECTION_DROP),
        ("InterfaceError", AdapterFault.POOL_EXHAUSTED),
        ("TimeoutError", AdapterFault.POOL_TIMEOUT),
        ("ValueError", AdapterFault.DECODE_ERROR),
        ("TypeError", AdapterFault.PREPARED_POISON),
    ]:
        assert type(_db_error(fault)).__name__ == error_name, fault
    assert issubclass(PostgresError, Exception)
