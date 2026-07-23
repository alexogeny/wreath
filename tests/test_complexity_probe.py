"""Regression tests for the empirical complexity-contract harness."""
from __future__ import annotations

import pytest

from wreath._devtools.complexity_probe import Probe, Result, _contract


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


def test_contract_records_the_scaled_axis_and_assumption() -> None:
    contract = _contract(_probe())

    assert contract["axis"] == "items"
    assert contract["assumption"] == "work is at most linear in items"
    assert contract["stage"] == "test"
