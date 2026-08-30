from __future__ import annotations

import pytest

from wreath._flight_schema import CaptureDisposition, CaptureFieldClass
from wreath.recording import (
    ArmRegistry,
    AttemptPolicy,
    BodyCapture,
    CaptureBudget,
    CapturePolicy,
    RecordingPolicy,
    RecordingPolicyError,
    RedactionPolicy,
    _compile_sets,
    compile_redaction,
)


def _ceiling() -> RecordingPolicy:
    return RecordingPolicy(
        max_capture_bytes=4096,
        redaction=RedactionPolicy(
            body=BodyCapture.STRUCTURED,
            dependency=BodyCapture.STRUCTURED,
            max_body_bytes=4096,
            max_fields=16,
            max_depth=8,
        ),
    )


def _capture(*, max_matches: int = 0) -> CapturePolicy:
    return CapturePolicy(
        redaction=RedactionPolicy(
            body=BodyCapture.STRUCTURED,
            dependency=BodyCapture.HASHED,
            max_body_bytes=1024,
            max_fields=8,
            max_depth=4,
        ),
        budget=CaptureBudget(slabs=1, slab_bytes=1024),
        expiry_seconds=10,
        max_matches=max_matches,
    )


def test_capture_byte_ceiling_refuses_the_first_out_of_range_value() -> None:
    with pytest.raises(RecordingPolicyError, match="max_body_bytes too large"):
        RedactionPolicy(max_body_bytes=(1 << 30) + 1)


def test_structured_depth_ceiling_refuses_the_first_out_of_range_value() -> None:
    with pytest.raises(RecordingPolicyError, match="max_depth out of range"):
        RedactionPolicy(max_depth=65)


@pytest.mark.parametrize(
    ("body", "expected"),
    [("none", BodyCapture.NONE), ("hashed", BodyCapture.HASHED)],
)
def test_redaction_converts_body_strings_to_the_enum(body: str, expected: BodyCapture) -> None:
    assert RedactionPolicy(body=body).body is expected


def test_redaction_converts_dependency_strings_to_the_enum() -> None:
    assert RedactionPolicy(dependency="hashed").dependency is BodyCapture.HASHED


@pytest.mark.parametrize(("max_fields", "max_depth"), [(0, 1), (1, 0)])
def test_structured_capture_requires_both_shape_bounds(max_fields: int, max_depth: int) -> None:
    with pytest.raises(RecordingPolicyError, match="needs max_fields and max_depth"):
        RedactionPolicy(
            body=BodyCapture.STRUCTURED,
            max_fields=max_fields,
            max_depth=max_depth,
        )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (BodyCapture.NONE, BodyCapture.HASHED, BodyCapture.NONE),
        (BodyCapture.STRUCTURED, BodyCapture.METADATA, BodyCapture.METADATA),
    ],
)
def test_narrow_chooses_the_less_revealing_body(
    left: BodyCapture, right: BodyCapture, expected: BodyCapture
) -> None:
    first = RedactionPolicy(
        body=left,
        max_fields=int(left is BodyCapture.STRUCTURED),
        max_depth=int(left is BodyCapture.STRUCTURED),
    )
    second = RedactionPolicy(
        body=right,
        max_fields=int(right is BodyCapture.STRUCTURED),
        max_depth=int(right is BodyCapture.STRUCTURED),
    )

    assert first.narrow(second).body is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (BodyCapture.NONE, BodyCapture.HASHED, BodyCapture.NONE),
        (BodyCapture.STRUCTURED, BodyCapture.METADATA, BodyCapture.METADATA),
    ],
)
def test_narrow_chooses_the_less_revealing_dependency(
    left: BodyCapture, right: BodyCapture, expected: BodyCapture
) -> None:
    first = RedactionPolicy(dependency=left)
    second = RedactionPolicy(dependency=right)

    assert first.narrow(second).dependency is expected


def test_recording_policy_compares_body_dispositions() -> None:
    ceiling = RecordingPolicy(
        max_capture_bytes=1,
        redaction=RedactionPolicy(body=BodyCapture.METADATA),
    )
    capture = CapturePolicy(redaction=RedactionPolicy(body=BodyCapture.HASHED))

    assert ceiling.permits(capture) is False


def test_recording_policy_compares_dependency_dispositions() -> None:
    ceiling = RecordingPolicy(
        max_capture_bytes=1,
        redaction=RedactionPolicy(dependency=BodyCapture.METADATA),
    )
    capture = CapturePolicy(redaction=RedactionPolicy(dependency=BodyCapture.HASHED))

    assert ceiling.permits(capture) is False


def test_raw_body_uses_the_declared_byte_bound() -> None:
    compiled = _capture().redaction
    plan = compile_redaction(compiled)

    assert plan.body(CaptureFieldClass.REQUEST_BODY) == (CaptureDisposition.RAW, 1024)
    assert plan.dependency() == (CaptureDisposition.HASHED, 0)


def test_compile_sets_refuses_the_descriptor_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.recording as module

    monkeypatch.setattr(module, "_MAX_FIELDS", 2)

    with pytest.raises(RecordingPolicyError, match="descriptor cap"):
        _compile_sets(frozenset({"one", "two"}), frozenset(), frozenset(), kind="header")


def test_active_uses_an_explicit_timestamp_without_reading_the_clock() -> None:
    clock_reads = 0

    def clock() -> float:
        nonlocal clock_reads
        clock_reads += 1
        return 100.0

    registry = ArmRegistry(_ceiling(), clock=clock)
    registry.arm(_capture())
    clock_reads = 0

    active = registry.active(now=105)

    assert active[0].expires_in == 5
    assert clock_reads == 0


def test_unbounded_arm_reports_the_unbounded_remaining_sentinel() -> None:
    registry = ArmRegistry(_ceiling(), clock=lambda: 100)

    assert registry.arm(_capture(max_matches=0)).remaining_matches == -1


def test_bounded_arm_reports_its_remaining_matches() -> None:
    registry = ArmRegistry(_ceiling(), clock=lambda: 100)

    assert registry.arm(_capture(max_matches=3)).remaining_matches == 3


@pytest.mark.parametrize("entry", ["parameter", ".parameter", "task."])
def test_argument_allowlist_requires_a_task_and_parameter(entry: str) -> None:
    with pytest.raises(RecordingPolicyError, match="task.parameter"):
        AttemptPolicy(argument_allowlist=frozenset({entry}))


def test_empty_argument_allowlist_does_not_inspect_the_handler() -> None:
    policy = AttemptPolicy()

    assert policy.capture_arguments(task="task", handler=pytest.fail, args=(), kwargs={}) == ()


def test_argument_capture_requires_a_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.recording as module

    policy = AttemptPolicy(
        argument_allowlist=frozenset({"task.value"}),
        redaction=RedactionPolicy(max_fields=1, max_depth=1, max_body_bytes=16),
    )
    monkeypatch.setattr(module, "_bind_arguments", pytest.fail)

    assert policy.capture_arguments(task="task", handler=None, args=(1,), kwargs={}) == ()


def test_argument_capture_returns_before_binding_when_task_has_no_wanted_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.recording as module

    policy = AttemptPolicy(
        argument_allowlist=frozenset({"other.value"}),
        redaction=RedactionPolicy(max_fields=1, max_depth=1, max_body_bytes=16),
    )
    monkeypatch.setattr(module, "_bind_arguments", pytest.fail)

    assert policy.capture_arguments(task="task", handler=pytest.fail, args=(1,), kwargs={}) == ()
