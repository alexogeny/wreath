from __future__ import annotations

import json

import pytest

from wreath._devtools import complexity_probe as complexity
from wreath._devtools.complexity_probe import (
    _REGISTRY,
    Probe,
    Result,
    Todo,
    _contract,
    _graphql_depth_rejection,
    _graphql_policy_plan_alias_control,
    _graphql_policy_plan_unique,
    degree_name,
    probe,
)


def _probe(
    *,
    tolerance: float = 0.5,
    noise_floor: float = 0.0,
    metric: str | None = None,
) -> Probe:
    return Probe(
        name="test",
        fn=lambda size: float(size),
        expect=1.0,
        sizes=(1, 2, 4, 8),
        tolerance=tolerance,
        noise_floor=noise_floor,
        metric=metric,
        axis="items",
        assumption="work is at most linear in items",
        stage="test",
        group="test",
    )


def test_faster_than_declared_upper_bound_passes() -> None:
    result = Result(_probe(), [1.0, 1.0, 1.0, 1.0], [{}, {}, {}, {}])

    assert result.status == "PASS"
    assert result.ok


def test_tail_cliff_fails_even_when_global_fit_is_diluted() -> None:
    result = Result(
        _probe(tolerance=0.25),
        [1.0, 1.0, 2.0, 16.0],
        [{}, {}, {}, {}],
    )

    assert result.tail_exponent > 1.25
    assert result.status == "FAIL"


def test_below_floor_is_unresolved_not_success() -> None:
    result = Result(
        _probe(noise_floor=1e-3),
        [1e-6, 2e-6, 4e-6, 8e-6],
        [{}, {}, {}, {}],
    )

    assert result.status == "UNRESOLVED"
    assert not result.ok


def test_declared_metric_must_be_present_at_every_size() -> None:
    with pytest.raises(ValueError, match="metric 'visits' missing"):
        Result(
            _probe(metric="visits"),
            [1.0, 2.0, 4.0, 8.0],
            [{"visits": 1}, {}, {"visits": 4}, {"visits": 8}],
        )


def test_graphql_depth_probe_reaches_the_bound_it_claims_to_measure() -> None:
    assert _graphql_depth_rejection(32) >= 0.0


def test_graphql_policy_plan_probes_reach_both_same_size_arms() -> None:
    assert _graphql_policy_plan_unique(4) > 0.0
    assert _graphql_policy_plan_alias_control(4) > 0.0


def test_xml_parse_probe_supplies_the_element_type() -> None:
    assert complexity._xml_wide_harness(1, attributes=True, canonical=False) >= 0.0


def test_livedoc_close_probe_reaches_both_principal_distributions() -> None:
    assert complexity._livedoc_close_harness(4, shared_principal=True) >= 0.0
    assert complexity._livedoc_close_harness(4, shared_principal=False) >= 0.0


def test_livedoc_close_probe_builds_the_declared_principal_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath import _livedoc

    subscriptions: list[str] = []

    class DocumentDouble:
        def __init__(self, **_options: object) -> None:
            self.subscribers: list[str] = []

        def subscribe(self, principal: str) -> object:
            subscriptions.append(principal)
            self.subscribers.append(principal)
            return object()

        def close_all(self) -> None:
            self.subscribers.clear()

    monkeypatch.setattr(_livedoc, "LiveDocument", DocumentDouble)

    complexity._livedoc_close_harness(3, shared_principal=True)
    assert subscriptions == ["shared", "shared", "shared"]
    subscriptions.clear()
    complexity._livedoc_close_harness(3, shared_principal=False)
    assert subscriptions == ["principal-0", "principal-1", "principal-2"]


def test_main_dispatches_update_baseline_before_running_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[list[str]] = []
    monkeypatch.setattr(
        complexity,
        "_write_baseline",
        lambda names: selected.append(list(names)) or 0,
    )

    assert complexity.main(["css-no-media-control", "--update-baseline"]) == 0
    assert selected == [["css-no-media-control"]]


def test_main_does_not_update_the_baseline_during_an_ordinary_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[list[str]] = []
    monkeypatch.setattr(
        complexity,
        "_write_baseline",
        lambda names: writes.append(list(names)) or 0,
    )
    monkeypatch.setattr(
        complexity,
        "run_probe",
        lambda registered: Result(registered, [1.0, 2.0, 4.0, 8.0], [{}] * 4),
    )

    assert complexity.main(["css-no-media-control"]) == 0
    assert writes == []


def test_contract_records_the_scaled_axis_and_assumption() -> None:
    contract = _contract(_probe())

    assert contract["axis"] == "items"
    assert contract["assumption"] == "work is at most linear in items"
    assert contract["stage"] == "test"


def test_updating_selected_probes_preserves_the_rest_of_the_baseline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "complexity-baseline.json"
    path.write_text(
        json.dumps(
            {
                "version": complexity.BASELINE_VERSION,
                "probes": {"existing-probe": {"contract": {}, "observation": {}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(complexity, "_baseline_path", lambda: path)
    monkeypatch.setattr(
        complexity,
        "run_probe",
        lambda registered: Result(registered, [1.0, 2.0, 4.0, 8.0], [{}] * 4),
    )

    assert complexity._write_baseline(["css-no-media-control"]) == 0

    recorded = json.loads(path.read_text(encoding="utf-8"))["probes"]
    assert set(recorded) == {"existing-probe", "css-no-media-control"}


# A mark records a defect rather than a contract, so it is checked from both
# sides: growing past the recorded degree is a regression, and *shrinking* below
# it means the defect is gone and the mark is now a lie. The second rule is the
# one that keeps a mark honest, so it is tested first and hardest -- without it
# a mark decays into permission and outlives the bug it describes.

_QUADRATIC = Todo(
    degree=2.0,
    target=1.0,
    reason="recorded, not patched",
    owner="#test",
)


def _marked(
    *,
    todo: Todo = _QUADRATIC,
    tolerance: float = 0.5,
) -> Probe:
    return Probe(
        name="marked",
        fn=lambda size: float(size),
        expect=todo.degree,
        sizes=(1, 2, 4, 8),
        tolerance=tolerance,
        noise_floor=0.0,
        axis="items",
        assumption="KNOWN DEFECT",
        stage="test",
        group="test",
        todo=todo,
    )


#: time = size ** k, over sizes (1, 2, 4, 8).
_LINEAR = [1.0, 2.0, 4.0, 8.0]
_QUADRATIC_TIMES = [1.0, 4.0, 16.0, 64.0]
_CUBIC_TIMES = [1.0, 8.0, 64.0, 512.0]


def test_a_mark_passes_while_the_defect_is_still_there() -> None:
    result = Result(_marked(), _QUADRATIC_TIMES, [{}] * 4)

    assert result.status == "PASS"
    assert result.ok


def test_a_mark_goes_red_when_the_defect_is_fixed() -> None:
    result = Result(_marked(), _LINEAR, [{}] * 4)

    assert result.status == "STALE"
    assert not result.ok


def test_a_mark_goes_red_when_the_defect_gets_worse() -> None:
    result = Result(_marked(), _CUBIC_TIMES, [{}] * 4)

    assert result.status == "FAIL"
    assert not result.ok


def test_an_unmarked_probe_is_still_one_sided() -> None:
    result = Result(_probe(), _LINEAR, [{}] * 4)
    assert result.status == "PASS"

    faster = Result(_probe(), [1.0, 1.0, 1.0, 1.0], [{}] * 4)
    assert faster.status == "PASS"


def test_a_mark_tolerates_noise_around_the_recorded_degree() -> None:
    noisy = [1.0, 2.0**1.88, 4.0**1.88, 8.0**1.88]
    result = Result(_marked(tolerance=0.6), noisy, [{}] * 4)

    assert result.status == "PASS"


def test_a_mark_must_aim_below_what_it_records() -> None:
    with pytest.raises(ValueError, match="not better than"):
        Todo(degree=2.0, target=2.0, reason="r", owner="o")


def test_a_mark_needs_a_reason_and_an_owner() -> None:
    with pytest.raises(ValueError, match="reason and an owner"):
        Todo(degree=2.0, target=1.0, reason="  ", owner="#1")
    with pytest.raises(ValueError, match="reason and an owner"):
        Todo(degree=2.0, target=1.0, reason="why", owner="")


def test_the_contract_carries_the_mark_so_retargeting_is_drift() -> None:
    contract = _contract(_marked())

    assert contract["todo"] == {
        "degree": 2.0,
        "target": 1.0,
        "reason": "recorded, not patched",
        "owner": "#test",
    }
    assert _contract(_probe())["todo"] is None
    assert _contract(_marked()) != _contract(
        _marked(todo=Todo(degree=2.0, target=1.5, reason="r", owner="#test"))
    )


def test_a_probe_pins_a_contract_or_a_defect_but_never_both() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        probe("both", expect=1.0, sizes=(1, 2), todo=_QUADRATIC)
    with pytest.raises(ValueError, match="exactly one of"):
        probe("neither", sizes=(1, 2))


def test_degree_names_reach_past_cubic() -> None:
    assert degree_name(1.0) == "linear"
    assert degree_name(2.05) == "quadratic"
    assert degree_name(3.9) == "quartic"
    assert degree_name(5.1) == "quintic"
    assert degree_name(6.0) == "sextic"


def test_fixed_timing_wheel_collision_probe_is_a_linear_contract() -> None:
    wheel = _REGISTRY["wheel-colliding-slot-chain"]

    assert wheel.todo is None
    assert wheel.expect == 1.0


def test_reused_response_header_replacement_is_a_linear_contract() -> None:
    replacement = _REGISTRY["replace-reused-response-headers"]

    assert replacement.todo is None
    assert replacement.expect == 1.0
    assert replacement.axis == "middleware headers accumulated on a reused response"

    lifecycle = _REGISTRY["reused-response-lifecycle"]
    assert lifecycle.todo is None
    assert lifecycle.expect == 1.0
    assert lifecycle.axis == "requests returning the same mutable response"
