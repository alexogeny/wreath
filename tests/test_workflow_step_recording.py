"""A saga that stopped mid-way, written down so somebody can reconstruct it.

The roadmap called this the highest-value unbuilt record kind, and named why: a
saga failure mid-way is the least reproducible state in the framework. Some side
effects have happened, some undos have run, some have not, and nothing outside a
recording says which. `Outcome.compensation_errors` gives a *count*, which
answers "did the unwind hold" and not "what is the state of the world".

Three things this suite is actually about:

* the unit is `(workflow instance, step)`, with a position and a predecessor,
  and it is a **second record kind inside `WFR1`** rather than a second
  container -- so a reader that predates it still reads the file;
* the record is written **after** the undo chain, because the compensations are
  most of what it is for;
* `compensation_failed` is a different outcome from `raised`, because a saga
  that did not unwind is not reachable from where it now is by a retry.
"""

from __future__ import annotations

import pytest

from wreath._flight_schema import SCHEMA_VERSION, MetadataImage, SchemaError
from wreath._recording_format import (
    COMPENSATION_FAILED,
    COMPENSATION_NONE,
    COMPENSATION_RAN,
    AttemptOutcome,
    AttemptRecord,
    WFR1Writer,
    WorkflowStepOutcome,
    WorkflowStepRecord,
    read_recording,
    read_step_recording,
)
from wreath.recording import (
    RecordingPolicyError,
    WorkflowStepPolicy,
    WorkflowStepRecorder,
    WorkflowStepTrigger,
    WorkflowStepTriggerKind,
    instance_sample_value,
)
from wreath.workflows import InMemoryWorkflowStore, Workflow


def _image() -> MetadataImage:
    return MetadataImage(SCHEMA_VERSION, *([()] * 11))


def _record(**overrides) -> WorkflowStepRecord:
    fields = {
        "instance": "checkout:4471",
        "workflow": "checkout",
        "step": "book_courier",
        "position": 2,
        "after": "charge_card",
        "tenant": "",
        "trace_context": "",
        "boundaries": (),
        "outcome": str(WorkflowStepOutcome.RAISED),
    }
    fields.update(overrides)
    return WorkflowStepRecord(**fields)  # type: ignore[arg-type]


# -- the record kind ---------------------------------------------------------


def test_a_step_record_round_trips_through_its_encoding() -> None:
    from wreath._recording_format import BoundaryEvent

    record = _record(
        boundaries=(BoundaryEvent(1, "main", 3, "PostgresError"),),
        error_type="RuntimeError",
        error_message="the courier API returned 503",
        completed_before=2,
        compensations=(("charge_card", COMPENSATION_RAN), ("reserve", COMPENSATION_NONE)),
    )
    assert WorkflowStepRecord.decode(record.encode()) == record


def test_a_torn_step_record_is_refused_rather_than_read_as_a_smaller_one() -> None:
    """A tear does not leave a shorter saga; it loses the entries nobody can count."""
    encoded = _record(compensations=(("charge_card", COMPENSATION_RAN),)).encode()
    with pytest.raises(SchemaError) as raised:
        WorkflowStepRecord.decode(encoded[:-4])
    assert "declares" in str(raised.value) and "holds" in str(raised.value)


def test_a_step_record_is_not_an_attempt_record_and_says_so() -> None:
    with pytest.raises(SchemaError) as raised:
        WorkflowStepRecord.decode(_attempt().encode())
    assert "not a workflow-step recording" in str(raised.value)
    with pytest.raises(SchemaError) as raised:
        AttemptRecord.decode(_record().encode())
    assert "not an attempt recording" in str(raised.value)


def _attempt() -> AttemptRecord:
    return AttemptRecord(
        job_id=1, queue="q", task="t", attempt=1, max_attempts=3, tenant="",
        dedup_key="", fence=1, trace_context="", boundaries=(),
        outcome=str(AttemptOutcome.RAISED),
    )


def test_both_record_kinds_live_in_one_container_and_are_counted_apart(tmp_path) -> None:
    """A second kind inside `WFR1`, not a second container."""
    path = tmp_path / "both.wfr1"
    with path.open("wb") as handle:
        writer = WFR1Writer(handle, _image())
        writer.write_attempt(_attempt())
        writer.write_step(_record())
        writer.close()
    decoded = read_recording(path.read_bytes())
    assert decoded.clean
    assert len(decoded.attempts) == 1 and decoded.footer_attempts == 1
    assert len(decoded.steps) == 1 and decoded.footer_steps == 1


def test_a_reader_that_predates_the_step_count_still_reads_the_footer_it_knows(
    tmp_path,
) -> None:
    """The footer is appended to, never widened.

    Asserted by reading the footer the way the *older* struct did: the first
    three counts are still at offset zero and still mean what they meant.
    """
    import struct

    path = tmp_path / "steps.wfr1"
    with path.open("wb") as handle:
        writer = WFR1Writer(handle, _image())
        writer.write_step(_record())
        writer.close()
    raw = path.read_bytes()
    decoded = read_recording(raw)
    assert decoded.footer_steps == 1
    # The old three-field footer, unpacked from the same bytes.
    marker = raw.rindex(b"FOOT")
    payload = raw[marker + 12 :]
    _chunks, slabs, cells = struct.unpack_from("<QQQ", payload, 0)
    assert (slabs, cells) == (0, 0)


def test_read_step_recording_diagnoses_a_tear_before_it_reports_an_absence(
    tmp_path,
) -> None:
    """The order is the diagnosis, not a preference."""
    path = tmp_path / "torn.wfr1"
    with path.open("wb") as handle:
        writer = WFR1Writer(handle, _image())
        writer.write_step(_record())
    torn = path.read_bytes()
    with pytest.raises(SchemaError) as raised:
        read_step_recording(torn)
    assert "the file has no footer" in str(raised.value)


def test_a_file_with_two_steps_is_refused_because_one_file_is_one_step(tmp_path) -> None:
    path = tmp_path / "two.wfr1"
    with path.open("wb") as handle:
        writer = WFR1Writer(handle, _image())
        writer.write_step(_record(step="a", position=0))
        writer.write_step(_record(step="b", position=1))
        writer.close()
    with pytest.raises(SchemaError) as raised:
        read_step_recording(path.read_bytes())
    assert "step 3 of an instance is a different execution from step 4" in str(raised.value)


# -- the policy --------------------------------------------------------------


def test_no_triggers_records_nothing() -> None:
    """Deny-by-default, structurally: the tempting exception is not made."""
    policy = WorkflowStepPolicy()
    for outcome in WorkflowStepOutcome:
        assert not policy.captures(
            workflow="checkout", step="charge_card", outcome=outcome, instance="i"
        )


def test_an_unnamed_step_trigger_is_refused_by_what_it_would_mean() -> None:
    with pytest.raises(RecordingPolicyError) as raised:
        WorkflowStepTrigger(WorkflowStepTriggerKind.STEP)
    assert "is 'record every step'" in str(raised.value)


def test_an_unnamed_workflow_trigger_is_refused_by_what_it_would_mean() -> None:
    with pytest.raises(RecordingPolicyError) as raised:
        WorkflowStepTrigger(WorkflowStepTriggerKind.WORKFLOW)
    assert "is 'record every saga'" in str(raised.value)


def test_a_failure_trigger_takes_both_failing_outcomes_and_not_completion() -> None:
    policy = WorkflowStepPolicy(
        triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.FAILURE),)
    )
    assert policy.captures(
        workflow="c", step="s", outcome=WorkflowStepOutcome.RAISED, instance="i"
    )
    assert policy.captures(
        workflow="c", step="s", outcome=WorkflowStepOutcome.COMPENSATION_FAILED,
        instance="i",
    )
    assert not policy.captures(
        workflow="c", step="s", outcome=WorkflowStepOutcome.COMPLETED, instance="i"
    )


def test_a_compensation_failure_trigger_takes_only_the_saga_that_did_not_unwind() -> None:
    policy = WorkflowStepPolicy(
        triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.COMPENSATION_FAILURE),)
    )
    assert not policy.captures(
        workflow="c", step="s", outcome=WorkflowStepOutcome.RAISED, instance="i"
    )
    assert policy.captures(
        workflow="c", step="s", outcome=WorkflowStepOutcome.COMPENSATION_FAILED,
        instance="i",
    )


def test_sampling_keeps_or_drops_a_whole_saga_rather_than_scattered_steps() -> None:
    """A per-step sample would record step 2 and step 5 and nothing between."""
    values = {
        step: instance_sample_value("checkout", "checkout:4471")
        for step in ("reserve", "charge", "courier")
    }
    assert len(set(values.values())) == 1
    # And two instances of one workflow do not share an answer, or sampling
    # would be a coin flipped once for the whole deployment.
    assert instance_sample_value("checkout", "a") != instance_sample_value("checkout", "b")


def test_a_named_workflow_trigger_ignores_another_workflows_steps() -> None:
    policy = WorkflowStepPolicy(
        triggers=(
            WorkflowStepTrigger(WorkflowStepTriggerKind.WORKFLOW, workflow="checkout"),
        )
    )
    assert policy.captures(
        workflow="checkout", step="s", outcome=WorkflowStepOutcome.COMPLETED, instance="i"
    )
    assert not policy.captures(
        workflow="refund", step="s", outcome=WorkflowStepOutcome.COMPLETED, instance="i"
    )


# -- the recorder ------------------------------------------------------------


def test_two_instances_whose_keys_slug_alike_do_not_share_a_file(tmp_path) -> None:
    """Substitution alone would overwrite one saga's evidence with another's."""
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.FAILURE),)
        ),
        directory=str(tmp_path),
    )
    first = recorder.write(_record(instance="order/4471"))
    second = recorder.write(_record(instance="order:4471"))
    assert first is not None and second is not None
    assert first != second
    assert read_step_recording(open(first, "rb").read()).instance == "order/4471"


def test_a_recorder_that_cannot_write_counts_it_rather_than_raising(tmp_path) -> None:
    """A recorder that can take a saga down with it is worse than none."""
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(), directory=str(tmp_path / "does-not-exist")
    )
    assert recorder.write(_record()) is None
    assert recorder.errors == 1
    assert recorder.written == 0


def test_an_overflowing_boundary_trace_refuses_the_recording(tmp_path) -> None:
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(max_boundaries=2), directory=str(tmp_path)
    )
    trace = recorder.trace()
    for index in range(4):
        trace.note(1, f"target-{index}")
    assert recorder.write(_record(), trace) is None
    assert recorder.refused_oversize == 1


# -- the saga ----------------------------------------------------------------


def _checkout(*, courier_fails: bool, refund_fails: bool = False) -> Workflow:
    workflow = Workflow("checkout")

    async def release_hold(context):
        return None

    async def refund(context):
        if refund_fails:
            raise RuntimeError("the payment gateway refused the refund")
        return None

    @workflow.step(compensate=release_hold)
    async def reserve_stock(context):
        return "held"

    @workflow.step(compensate=refund)
    async def charge_card(context):
        return "charged"

    @workflow.step
    async def book_courier(context):
        if courier_fails:
            raise RuntimeError("the courier API returned 503")
        return "booked"

    return workflow


def _recorder(tmp_path, *kinds) -> WorkflowStepRecorder:
    return WorkflowStepRecorder(
        WorkflowStepPolicy(triggers=tuple(WorkflowStepTrigger(kind) for kind in kinds)),
        directory=str(tmp_path),
    )


def _whole_saga(tmp_path) -> WorkflowStepRecorder:
    """Every step of the checkout workflow, which a WORKFLOW trigger must name."""
    return WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(
                WorkflowStepTrigger(
                    WorkflowStepTriggerKind.WORKFLOW, workflow="checkout"
                ),
            )
        ),
        directory=str(tmp_path),
    )


async def test_the_failing_step_records_which_compensations_ran(tmp_path) -> None:
    """The state a count cannot describe.

    `Outcome.compensation_errors` says the unwind held. It cannot say that the
    hold was released and the card refunded, in that order, after the courier
    call failed at position 2 -- which is the whole of what somebody looking at
    a stuck order needs.
    """
    recorder = _recorder(tmp_path, WorkflowStepTriggerKind.FAILURE)
    workflow = _checkout(courier_fails=True)
    with pytest.raises(RuntimeError):
        await workflow.run(
            store=InMemoryWorkflowStore(), key="checkout:4471", recorder=recorder
        )
    assert recorder.written == 1
    written = sorted(tmp_path.glob("*.wfr1"))
    record = read_step_recording(written[0].read_bytes())
    assert record.workflow == "checkout"
    assert record.instance == "checkout:4471"
    assert record.step == "book_courier"
    assert record.position == 2
    assert record.after == "charge_card", "the cause is the step before, not the request"
    assert record.completed_before == 2
    assert record.outcome == str(WorkflowStepOutcome.RAISED)
    assert record.error_type == "RuntimeError"
    assert record.error_message == "the courier API returned 503"
    # Newest first, which is the order they actually ran in.
    assert record.compensations == (
        ("charge_card", COMPENSATION_RAN),
        ("reserve_stock", COMPENSATION_RAN),
    )


async def test_a_saga_that_did_not_unwind_records_a_different_outcome(tmp_path) -> None:
    """`compensation_failed` outranks `raised`, and that ordering is the point."""
    recorder = _recorder(tmp_path, WorkflowStepTriggerKind.COMPENSATION_FAILURE)
    workflow = _checkout(courier_fails=True, refund_fails=True)
    store = InMemoryWorkflowStore()
    with pytest.raises(RuntimeError) as raised:
        await workflow.run(store=store, key="checkout:9", recorder=recorder)
    # The step's own exception still reaches the caller, unchanged.
    assert "the courier API returned 503" in str(raised.value)
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.outcome == str(WorkflowStepOutcome.COMPENSATION_FAILED)
    assert record.compensations == (
        ("charge_card", COMPENSATION_FAILED),
        ("reserve_stock", COMPENSATION_RAN),
    )
    # And the saga's own bookkeeping is untouched by the recorder.
    outcome = await workflow.status(store=store, key="checkout:9")
    assert outcome is not None and outcome.compensation_errors == 1


async def test_a_step_with_no_compensation_is_recorded_as_none_not_as_ran(
    tmp_path,
) -> None:
    """"Nothing to undo" and "the undo was never reached" are different states."""
    recorder = _recorder(tmp_path, WorkflowStepTriggerKind.FAILURE)
    workflow = Workflow("import")

    @workflow.step
    async def read_file(context):
        return "read"

    @workflow.step
    async def parse(context):
        raise ValueError("bad header")

    with pytest.raises(ValueError):
        await workflow.run(store=InMemoryWorkflowStore(), key="i", recorder=recorder)
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.compensations == (("read_file", COMPENSATION_NONE),)


async def test_an_unarmed_run_writes_nothing_and_still_completes(tmp_path) -> None:
    """The falsifier: a recorder present and a policy that arms for nothing."""
    recorder = _recorder(tmp_path)
    workflow = _checkout(courier_fails=False)
    outcome = await workflow.run(
        store=InMemoryWorkflowStore(), key="checkout:1", recorder=recorder
    )
    assert outcome.state == "completed"
    assert recorder.written == 0
    assert list(tmp_path.glob("*.wfr1")) == []


async def test_a_run_with_no_recorder_behaves_exactly_as_it_did(tmp_path) -> None:
    workflow = _checkout(courier_fails=True)
    store = InMemoryWorkflowStore()
    with pytest.raises(RuntimeError):
        await workflow.run(store=store, key="checkout:2")
    outcome = await workflow.status(store=store, key="checkout:2")
    assert outcome is not None
    assert outcome.state == "compensated"
    assert outcome.compensation_errors == 0


async def test_a_named_step_trigger_records_a_successful_step_too(tmp_path) -> None:
    """A step under investigation, where the successes are as informative."""
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(
                WorkflowStepTrigger(WorkflowStepTriggerKind.STEP, step="charge_card"),
            )
        ),
        directory=str(tmp_path),
    )
    workflow = _checkout(courier_fails=False)
    await workflow.run(store=InMemoryWorkflowStore(), key="c:3", recorder=recorder)
    assert recorder.written == 1
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.step == "charge_card"
    assert record.outcome == str(WorkflowStepOutcome.COMPLETED)
    assert record.compensations == ()
    assert record.after == "reserve_stock"


async def test_a_resumed_instance_records_the_steps_it_did_not_run_as_prior_work(
    tmp_path,
) -> None:
    """`completed_before` on a resume is work this process never did."""
    recorder = _whole_saga(tmp_path)
    store = InMemoryWorkflowStore()
    first = _checkout(courier_fails=True)
    with pytest.raises(RuntimeError):
        await first.run(store=store, key="c:4", compensate=False)
    # The two completed steps are still recorded, so a resume re-enters at the
    # third -- and the recording says the saga was two steps in already.
    second = _checkout(courier_fails=False)
    outcome = await second.resume(store=store, key="c:4", recorder=recorder)
    assert outcome.state == "completed"
    assert recorder.written == 1
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.step == "book_courier"
    assert record.completed_before == 2
    assert record.after == "charge_card"


async def test_the_record_carries_the_instances_trace_not_the_resuming_workers(
    tmp_path,
) -> None:
    """`run` and `resume` are the same trace even on different days."""
    from wreath import telemetry

    recorder = _whole_saga(tmp_path)
    store = InMemoryWorkflowStore()
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    token = telemetry.outbound_context.set((parent, ""))
    try:
        await _checkout(courier_fails=False).run(store=store, key="c:5")
    finally:
        telemetry.outbound_context.reset(token)
    # A second, untraced process resumes: the record still names the cause.
    store._instances["c:5"]["state"] = "running"
    store._instances["c:5"]["results"].pop("book_courier")
    await _checkout(courier_fails=False).resume(
        store=store, key="c:5", recorder=recorder
    )
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.trace_context == parent


# -- what a mutation run found nobody was watching ---------------------------


def test_a_step_record_shorter_than_its_header_is_refused_by_length() -> None:
    with pytest.raises(SchemaError) as raised:
        WorkflowStepRecord.decode(b"WFS1")
    assert "shorter than its" in str(raised.value)


def test_a_step_record_from_a_future_version_is_refused_rather_than_guessed() -> None:
    """A version this build does not know is not a record it may half-read."""
    import struct

    encoded = bytearray(_record().encode())
    encoded[4] = 2  # the version byte
    with pytest.raises(SchemaError) as raised:
        WorkflowStepRecord.decode(bytes(encoded))
    assert str(raised.value) == "unsupported workflow-step record version 2"
    assert struct.unpack_from("<4s", bytes(encoded), 0)[0] == b"WFS1"


def test_a_chunked_step_record_is_refused_rather_than_joined() -> None:
    """A partially assembled step reports fewer compensations than the saga ran."""
    encoded = bytearray(_record(compensations=(("a", COMPENSATION_RAN),)).encode())
    encoded[5] = 0x01  # the continuation flag
    with pytest.raises(SchemaError) as raised:
        WorkflowStepRecord.decode(bytes(encoded))
    assert "refused rather than joined" in str(raised.value)
    assert "reads as complete" in str(raised.value)


async def test_the_first_step_of_a_saga_has_no_predecessor(tmp_path) -> None:
    """`after` is empty at position zero, and empty is a real answer.

    A record whose cause is "nothing" is different from one whose cause was not
    written down, and only the first step of an instance is the former.
    """
    recorder = _recorder(tmp_path, WorkflowStepTriggerKind.FAILURE)
    workflow = Workflow("import")

    @workflow.step
    async def read_file(context):
        raise OSError("no such card")

    with pytest.raises(OSError):
        await workflow.run(store=InMemoryWorkflowStore(), key="i", recorder=recorder)
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.position == 0
    assert record.after == ""
    assert record.completed_before == 0
    assert record.compensations == ()


async def test_a_completed_step_records_no_error_at_all(tmp_path) -> None:
    """Not `NoneType`, and not the repr of `None`: nothing failed."""
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(
                WorkflowStepTrigger(WorkflowStepTriggerKind.STEP, step="reserve_stock"),
            )
        ),
        directory=str(tmp_path),
    )
    await _checkout(courier_fails=False).run(
        store=InMemoryWorkflowStore(), key="c:6", recorder=recorder
    )
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.outcome == str(WorkflowStepOutcome.COMPLETED)
    assert record.error_type == ""
    assert record.error_message == ""


async def test_a_scoped_recorder_records_the_boundaries_the_step_crossed(tmp_path) -> None:
    """An unscoped recorder writes an empty trace, and that is the truth.

    A `Workflow` holds no database of its own, so unlike a job runner there is
    no slot to observe -- `scope=` is where the boundaries come from, and with
    none there are none to report rather than some going unrecorded.
    """

    class _Database:
        def __init__(self) -> None:
            self.name = "main"

        async def acquire(self, workload):
            return _Connection()

        async def release(self, workload, connection):
            return None

    class _Connection:
        async def fetchval(self, sql, *args):
            return 1

    class _Scope:
        def __init__(self) -> None:
            self._databases = {"main": _Database()}

    scope = _Scope()
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.FAILURE),)
        ),
        directory=str(tmp_path),
        scope=scope,
    )
    workflow = Workflow("report")

    @workflow.step
    async def count_rows(context):
        database = scope._databases["main"]
        connection = await database.acquire("read")
        try:
            await connection.fetchval("SELECT 1")
        finally:
            await database.release("read", connection)
        raise RuntimeError("the report template is missing")

    with pytest.raises(RuntimeError):
        await workflow.run(store=InMemoryWorkflowStore(), key="r", recorder=recorder)
    record = read_step_recording(sorted(tmp_path.glob("*.wfr1"))[0].read_bytes())
    assert record.boundaries, "a scoped recorder saw no boundary at all"
    assert record.boundaries[0].target == "main"


async def test_a_recorded_saga_still_reports_progress(tmp_path) -> None:
    """The reporter and the recorder are independent, and both must fire.

    Both `progress=` and `recorder=` reach `_execute_bound`, and a mutation run
    found the three `reporter is not None` guards had no witness at all -- so a
    saga run with a registry could have stopped reporting and nothing would have
    said so.
    """
    from wreath.progress import ProgressRegistry

    progress = ProgressRegistry()
    recorder = _recorder(tmp_path, WorkflowStepTriggerKind.FAILURE)
    await _checkout(courier_fails=False).run(
        store=InMemoryWorkflowStore(), key="c:7", progress=progress, recorder=recorder
    )
    done = progress.get("c:7")
    assert done is not None
    assert done.state == "done"
    assert done.percent == 100
    assert "workflow 'checkout' complete" in done.message

    with pytest.raises(RuntimeError):
        await _checkout(courier_fails=True).run(
            store=InMemoryWorkflowStore(), key="c:8", progress=progress,
            recorder=recorder,
        )
    failed = progress.get("c:8")
    assert failed is not None
    assert failed.state == "failed"
    assert "step 'book_courier' failed" in failed.message


async def test_progress_advances_a_step_at_a_time(tmp_path) -> None:
    """The mid-run report, which only fires between steps."""
    from wreath.progress import ProgressRegistry

    progress = ProgressRegistry()
    seen: list[float] = []
    workflow = Workflow("slow")

    @workflow.step
    async def one(context):
        seen.append(_percent(progress, "s"))
        return 1

    @workflow.step
    async def two(context):
        seen.append(_percent(progress, "s"))
        return 2

    await workflow.run(store=InMemoryWorkflowStore(), key="s", progress=progress)
    assert seen == [0.0, 50.0]
    current = progress.get("s")
    assert current is not None and current.percent == 100


def _percent(progress, key: str) -> float:
    current = progress.get(key)
    return 0.0 if current is None else current.percent


# -- the recording's filename ------------------------------------------------
#
# A recording nobody can find is a recording nobody has. The name is derived
# from three application-supplied strings, so it has to be both readable and
# injective -- and a mutation run found neither half had a witness.


def _written(tmp_path, **overrides) -> str:
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.FAILURE),)
        ),
        directory=str(tmp_path),
    )
    path = recorder.write(_record(**overrides))
    assert path is not None
    import os

    return os.path.basename(path)


def test_a_plain_instance_key_keeps_its_own_name_in_the_file(tmp_path) -> None:
    """No digest where none is needed: a name somebody has to read stays readable."""
    assert _written(tmp_path, instance="order-4471") == (
        "checkout-order-4471-2-book_courier.wfr1"
    )


@pytest.mark.parametrize("instance", ["a-b", "a_b", "AbC123"])
def test_the_characters_a_filename_may_hold_are_kept(tmp_path, instance: str) -> None:
    assert _written(tmp_path, instance=instance).startswith(f"checkout-{instance}-")


@pytest.mark.parametrize("instance", ["order/4471", "café", "a b", "ünicode"])
def test_anything_else_is_substituted_and_carries_a_digest(tmp_path, instance: str) -> None:
    """Substitution alone is not injective; the digest is what makes it so."""
    name = _written(tmp_path, instance=instance)
    assert "/" not in name and " " not in name
    assert name.isascii()
    assert name.count(".") == 2, f"{name} carries no digest"


def test_a_long_instance_key_is_bounded_and_still_unique(tmp_path) -> None:
    long_one = "checkout:" + "9" * 200
    other = "checkout:" + "9" * 199 + "8"
    first = _written(tmp_path, instance=long_one)
    second = _written(tmp_path, instance=other)
    assert first != second
    assert len(first) < 120
    assert first.count(".") == 2


def test_an_empty_name_component_does_not_produce_an_empty_filename_part(
    tmp_path,
) -> None:
    """A record built by hand may carry one, and a bare `--2-` names nothing."""
    name = _written(tmp_path, instance="", workflow="", step="")
    assert "--" not in name
    assert name.startswith("_-_-2-_")


def test_a_recorder_given_a_metadata_image_writes_that_one(tmp_path) -> None:
    """The default stands in for an application that has none, not for one it has."""
    from wreath._flight_schema import NamedMeta
    from wreath._recording_format import read_recording

    image = MetadataImage(
        SCHEMA_VERSION, (), (), (NamedMeta(1, "session"),), *([()] * 8)
    )
    recorder = WorkflowStepRecorder(
        WorkflowStepPolicy(
            triggers=(WorkflowStepTrigger(WorkflowStepTriggerKind.FAILURE),)
        ),
        directory=str(tmp_path),
        image=image,
    )
    path = recorder.write(_record())
    assert path is not None
    decoded = read_recording(open(path, "rb").read())
    assert decoded.image.image_hash_short() == image.image_hash_short()
    assert decoded.image.image_hash_short() != _image().image_hash_short()


def test_a_long_but_otherwise_clean_key_is_still_bounded(tmp_path) -> None:
    """The length bound is a separate clause from the substitution.

    A key made only of characters a filename may hold still has to be cut, or a
    saga keyed on a 4 KiB composite writes a path no filesystem accepts. A
    mutation run dropped this clause and every other test still passed, because
    each of them used a key that the substitution had already altered.
    """
    clean = "a" * 200
    name = _written(tmp_path, instance=clean)
    assert len(name) < 120
    assert name.count(".") == 2, "a truncated key must carry the digest that keeps it unique"
    assert _written(tmp_path, instance=clean + "b") != name
