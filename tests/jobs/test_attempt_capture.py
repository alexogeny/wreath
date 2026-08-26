"""A `JobRunner` armed to record its attempts.

The unit half: a fake database, a real runner, real arming and a real `WFR1`
file on disk. The live half -- lease expiry, and a replay that must not touch
the queue -- is in `test_attempt_capture_live.py`.
"""

from __future__ import annotations

import pytest
from _pgfidelity import check_for

from wreath.jobs import JobRunner, _Claimed
from wreath.postgres import PostgresError
from wreath.recording import (
    AttemptOutcome,
    AttemptPolicy,
    AttemptRecorder,
    AttemptTrigger,
    AttemptTriggerKind,
    read_attempt_recording,
)


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        #: SQL text this connection refuses, so a test can put a real driver
        #: error at a known coordinate in the boundary trace.
        self.fail_on: str | None = None

    def _record(self, sql, args):
        check_for(self, sql, args)
        self.calls.append((sql, args))
        if self.fail_on is not None and self.fail_on in sql:
            raise PostgresError("relation does not exist")

    async def execute(self, sql, *args):
        self._record(sql, args)
        return "OK"

    async def fetch(self, sql, *args):
        self._record(sql, args)
        return []

    async def fetchval(self, sql, *args):
        self._record(sql, args)
        return 1

    async def fetchrow(self, sql, *args):
        self._record(sql, args)
        return None


class FakeDatabase:
    name = "main"

    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquired = 0
        self.released = 0

    async def acquire(self, workload):
        self.acquired += 1
        return self.connection

    async def release(self, workload, connection):
        self.released += 1


def _recorder(tmp_path, *triggers, **policy_kw) -> AttemptRecorder:
    return AttemptRecorder(
        AttemptPolicy(triggers=tuple(triggers), **policy_kw), directory=str(tmp_path)
    )


def _runner(tmp_path, *triggers, **policy_kw) -> JobRunner:
    return JobRunner(
        FakeDatabase(),
        name="work",
        lease=30.0,
        attempts=_recorder(tmp_path, *triggers, **policy_kw),
    )


def _claim(**overrides) -> _Claimed:
    fields = {
        "id": 41,
        "task": "send",
        "args": ["alex@example.com", "reset-token-nobody-may-keep"],
        "tenant": "acme",
        "attempts": 3,
        "max_attempts": 5,
        "fence": 9,
        "key": "work:reset:41",
    }
    fields.update(overrides)
    return _Claimed(**fields)


async def test_an_unarmed_runner_writes_nothing(tmp_path):
    runner = _runner(tmp_path)

    @runner.task("send")
    async def send(ctx, *args):
        raise RuntimeError("boom")

    await runner._run(_claim())
    assert list(tmp_path.iterdir()) == []
    assert runner._attempts.written == 0


async def test_arming_on_failure_records_the_attempt_that_raised(tmp_path):
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("the token was already spent")

    await runner._run(_claim())

    written = list(tmp_path.iterdir())
    assert len(written) == 1
    assert written[0].name == "work-41-4.wfr1"
    record = read_attempt_recording(written[0].read_bytes())
    assert record.outcome == AttemptOutcome.RAISED
    assert record.error_type == "ValueError"
    assert record.error_message == "the token was already spent"
    assert record.job_id == 41
    assert record.task == "send"
    assert record.attempt == 4
    assert record.tenant == "acme"
    assert record.dedup_key == "work:reset:41"


async def test_the_recording_names_the_fence_the_worker_held(tmp_path):
    """Two workers can both believe they own a job after a lease expiry. A
    recording that cannot say which one it was is a recording of an ambiguity."""
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim(fence=9))
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.fence == 9


async def test_a_completed_attempt_is_not_recorded_by_a_failure_arm(tmp_path):
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        return None

    await runner._run(_claim())
    assert list(tmp_path.iterdir()) == []


async def test_arming_on_final_failure_waits_for_the_last_attempt(tmp_path):
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FINAL_FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim(attempts=3))  # attempt 4 of 5
    assert list(tmp_path.iterdir()) == []
    await runner._run(_claim(attempts=4))  # attempt 5 of 5 -- the dead-letter
    assert [p.name for p in tmp_path.iterdir()] == ["work-41-5.wfr1"]


async def test_arming_by_task_name_records_a_success_of_that_task_only(tmp_path):
    runner = _runner(
        tmp_path, AttemptTrigger(AttemptTriggerKind.TASK, task="import_herd")
    )

    @runner.task("import_herd")
    async def import_herd(ctx, *args):
        return None

    @runner.task("send")
    async def send(ctx, *args):
        return None

    await runner._run(_claim(task="import_herd", args=[]))
    await runner._run(_claim(id=42, task="send", args=[]))
    assert [p.name for p in tmp_path.iterdir()] == ["work-41-4.wfr1"]
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.outcome == AttemptOutcome.COMPLETED
    assert record.error_type == ""
    assert record.error_message == ""


async def test_a_job_with_no_dedup_key_records_an_empty_one(tmp_path):
    """`""` rather than `"None"`. The container has no null string, and a
    stringified `None` reads as a dedup key nothing will ever match."""
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim(args=[], key=None))
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.dedup_key == ""


async def test_a_deadline_cancellation_is_recorded_as_itself(tmp_path):
    """Not as `raised`. Nothing failed -- work was stopped -- and a recording
    that says otherwise sends somebody looking for a bug in the handler."""
    import asyncio

    runner = JobRunner(
        FakeDatabase(),
        name="work",
        lease=30.0,
        attempts=_recorder(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE)),
    )

    @runner.task("send", timeout=0.01)
    async def send(ctx, *args):
        await asyncio.sleep(5)

    await runner._run(_claim())
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.outcome == AttemptOutcome.DEADLINE_CANCELLED
    # Not `"None"`. There is no exception, and a recording that stringifies the
    # absence of one puts a four-character lie where a reader looks for a cause.
    assert record.error_type == ""
    assert record.error_message == ""
    assert runner.run_timeouts == 1


async def test_a_raised_arm_ignores_a_deadline_cancellation(tmp_path):
    import asyncio

    runner = JobRunner(
        FakeDatabase(),
        name="work",
        lease=30.0,
        attempts=_recorder(tmp_path, AttemptTrigger(AttemptTriggerKind.RAISED)),
    )

    @runner.task("send", timeout=0.01)
    async def send(ctx, *args):
        await asyncio.sleep(5)

    await runner._run(_claim())
    assert list(tmp_path.iterdir()) == []


# --- what must never reach the disk ------------------------------------------


async def test_the_arguments_are_absent_from_the_bytes(tmp_path):
    """Not masked in the reader -- absent from the file.

    `args jsonb` is a positional array and `RedactionPolicy` is name-keyed, so
    there is no name for an operator to deny. Until there is, the arguments do
    not go in at all; only how many there were.
    """
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim())
    raw = (tmp_path / "work-41-4.wfr1").read_bytes()
    assert b"reset-token-nobody-may-keep" not in raw
    assert b"alex@example.com" not in raw
    assert read_attempt_recording(raw).argument_count == 2


async def test_a_recording_whose_boundary_trace_overflows_is_refused_not_truncated(
    tmp_path,
):
    """A truncated trace replays as a *different* failure: the injected fault
    lands at whatever statement sits at that coordinate in the shorter run."""
    runner = _runner(
        tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE), max_boundaries=4
    )

    @runner.task("walk")
    async def walk(ctx, *args):
        # Resolved at call time, as the binder resolves a `FromDatabase`: an
        # observer installed for the attempt is only reached by a lookup that
        # happens inside it.
        database = runner._db
        for _ in range(20):
            connection = await database.acquire("read")
            await connection.fetch("SELECT 1")
            await database.release("read", connection)
        raise ValueError("boom")

    await runner._run(_claim(task="walk", args=[]))
    assert list(tmp_path.iterdir()) == []
    assert runner._attempts.refused_oversize == 1
    assert runner._attempts.written == 0


async def test_a_boundary_crossing_is_recorded_with_the_error_that_ended_it(tmp_path):
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("walk")
    async def walk(ctx, *args):
        connection = await runner._db.acquire("read")
        await connection.fetch("SELECT 1")
        await connection.fetchval("SELECT bad")
        raise RuntimeError("unreachable")  # pragma: no cover

    runner._db.connection.fail_on = "SELECT bad"
    await runner._run(_claim(task="walk", args=[]))
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    seams = [(b.seam, b.coordinate, b.error_type) for b in record.boundaries]
    assert seams[0] == (0, 0, "")  # AdapterSeam.DB_ACQUIRE
    assert seams[1] == (1, 0, "")  # the first query on that lease
    assert seams[2] == (1, 1, "PostgresError")  # the one that raised
    assert all(event.target == "main" for event in record.boundaries)
    assert record.error_type == "PostgresError"


async def test_the_recording_carries_the_enqueuing_requests_trace_context(tmp_path):
    """Plan 01's `trace_context` column, read off the claim by name.

    Written as a subclass because this worktree predates that column: the
    attribute name is the one the queue populates, and the runner reads it
    with `getattr` so an older row simply has none.
    """
    from dataclasses import dataclass

    @dataclass(slots=True)
    class TracedClaim(_Claimed):
        trace_context: str | None = None

    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    claim = TracedClaim(
        id=41, task="send", args=[], tenant="acme", attempts=3, max_attempts=5,
        fence=9, key=None, trace_context=traceparent,
    )
    await runner._run(claim)
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.trace_context == traceparent


async def test_an_untraced_job_records_an_empty_trace_context(tmp_path):
    runner = _runner(tmp_path, AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim(args=[]))
    record = read_attempt_recording((tmp_path / "work-41-4.wfr1").read_bytes())
    assert record.trace_context == ""


async def test_a_recorder_that_cannot_write_counts_it_and_the_attempt_still_runs(
    tmp_path,
):
    runner = _runner(tmp_path / "nowhere", AttemptTrigger(AttemptTriggerKind.FAILURE))

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim(args=[]))
    assert runner._attempts.errors == 1
    assert runner._attempts.written == 0
    # The attempt was still charged: the recorder is a witness, not a participant.
    assert any(
        "state='ready'" in sql for sql, _ in runner._db.connection.calls
    )


def test_a_recorder_is_optional_and_the_runner_says_so():
    runner = JobRunner(FakeDatabase(), name="work")
    assert runner._attempts is None


@pytest.mark.parametrize(
    "outcome",
    [AttemptOutcome.COMPLETED, AttemptOutcome.RAISED, AttemptOutcome.LEASE_EXPIRED],
)
def test_every_outcome_is_a_value_the_policy_can_be_asked_about(outcome):
    policy = AttemptPolicy(triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),))
    assert policy.captures(
        task="send", outcome=outcome, attempt=1, max_attempts=5, job_id=1
    ) is (outcome is not AttemptOutcome.COMPLETED)


# --- and what may reach it, when somebody says which --------------------------


async def test_an_allowlisted_argument_reaches_the_file_and_its_neighbour_does_not(
    tmp_path,
):
    """The pair that justifies the whole mechanism, through a real runner.

    `send(ctx, address, token)` -- the operator allowed `address` and not
    `token`, and the *bytes on disk* are what says whether that held. Asserted
    against the raw file rather than the decoded record, because a reader that
    masked on the way out would pass the decoded assertion while the token sat
    in the file forever.
    """
    from wreath.recording import RedactionPolicy

    runner = _runner(
        tmp_path,
        AttemptTrigger(AttemptTriggerKind.FAILURE),
        argument_allowlist=frozenset({"send.address"}),
        redaction=RedactionPolicy(max_fields=16, max_depth=3, max_body_bytes=1024),
    )

    @runner.task("send")
    async def send(ctx, address, token):
        raise ValueError("boom")

    await runner._run(_claim())
    raw = (tmp_path / "work-41-4.wfr1").read_bytes()
    assert b"alex@example.com" in raw
    assert b"reset-token-nobody-may-keep" not in raw

    record = read_attempt_recording(raw)
    assert record.arguments == (("address", '{"value":"alex@example.com"}'),)
    assert record.argument_count == 2, "the count is unchanged by what was kept"


async def test_a_task_whose_handler_takes_varargs_captures_nothing(tmp_path):
    """The runner's own shape for a handler it cannot name: `*args` has no
    declared parameter, so no allowlist entry reaches it and the bytes stay
    clean. This is the rule that keeps a signature change from silently
    starting to record."""
    from wreath.recording import RedactionPolicy

    runner = _runner(
        tmp_path,
        AttemptTrigger(AttemptTriggerKind.FAILURE),
        argument_allowlist=frozenset({"send.address", "send.args"}),
        redaction=RedactionPolicy(max_fields=16, max_depth=3, max_body_bytes=1024),
    )

    @runner.task("send")
    async def send(ctx, *args):
        raise ValueError("boom")

    await runner._run(_claim())
    raw = (tmp_path / "work-41-4.wfr1").read_bytes()
    assert b"alex@example.com" not in raw
    assert b"reset-token-nobody-may-keep" not in raw
    assert read_attempt_recording(raw).arguments == ()


def test_an_expired_job_without_a_registered_handler_records_no_arguments(tmp_path):
    class EmptyTrace:
        events = ()

    runner = _runner(
        tmp_path,
        AttemptTrigger(AttemptTriggerKind.FAILURE),
    )
    record = runner._attempt_record(
        _claim(task="removed", args=["private"]),
        AttemptOutcome.LEASE_EXPIRED,
        None,
        EmptyTrace(),
    )
    assert record.arguments == ()
    assert record.argument_count == 1
