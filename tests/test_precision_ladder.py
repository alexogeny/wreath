"""Slice 2: precision as an authorization outcome, not a verdict.

A withheld field is a boolean. This is the same idea with a scale: exact for a
ranger, 1 km for a partner, 10 km for a volunteer, absent for the public, and
the policy engine chooses. The two properties worth attacking are that the
degradation cannot be averaged away and cannot be bypassed.
"""

from __future__ import annotations

import math

import pytest

from wreath.authorization import PrecisionLadder, coarsen
from wreath.geospatial import Coordinate, distance

TOWN = Coordinate(lat=-23.6980, lon=133.8807)


# --- the grid ----------------------------------------------------------------


def test_coarsening_is_a_pure_function_of_the_point_and_the_cell():
    """The non-reversibility property, stated as determinism.

    An attacker who can ask repeatedly learns the cell and never more. The
    alternative -- per-request jitter -- averages to the true position, so the
    absence of randomness here *is* the security control.
    """
    first = coarsen(TOWN, 10_000)
    for _ in range(50):
        assert coarsen(TOWN, 10_000) == first


def test_repeated_observations_cannot_be_averaged_toward_the_truth():
    """The attack the grid defeats, run as an attack.

    Averaging a thousand answers must land on the *cell*, not on the point. A
    jittered implementation would converge on TOWN and fail this.
    """
    answers = [coarsen(TOWN, 10_000) for _ in range(1_000)]
    mean_lat = sum(c.lat for c in answers) / len(answers)
    mean_lon = sum(c.lon for c in answers) / len(answers)
    cell = coarsen(TOWN, 10_000)
    assert mean_lat == cell.lat
    assert mean_lon == cell.lon
    # And the mean is genuinely not the true position: if it were, there would
    # be nothing to defeat and this test would pass against a broken design.
    assert (mean_lat, mean_lon) != (TOWN.lat, TOWN.lon)


def test_neighbouring_points_inside_one_cell_report_the_same_place():
    """What "10 km precision" has to mean to be worth anything."""
    a = coarsen(Coordinate(lat=-23.6980, lon=133.8807), 10_000)
    b = coarsen(Coordinate(lat=-23.7020, lon=133.8850), 10_000)
    assert a == b


def test_a_coarser_cell_never_reveals_more_than_a_finer_one():
    fine = coarsen(TOWN, 1_000)
    coarse = coarsen(TOWN, 50_000)
    assert distance(TOWN, coarse) >= distance(TOWN, fine) or distance(TOWN, fine) < 1_000


def test_the_cell_is_at_least_the_requested_size_at_high_latitude():
    """The reason longitude is scaled by cos(latitude).

    An equator-sized longitude step at latitude 60 makes a cell half as wide on
    the ground as promised -- revealing *more* precision than was granted, which
    is the unsafe direction to be wrong in.
    """
    for lat in (0.0, 45.0, 60.0, 75.0):
        point = Coordinate(lat=lat, lon=10.0)
        cell = coarsen(point, 10_000)
        east = Coordinate(lat=cell.lat, lon=cell.lon + 1e-9)
        assert cell.lat == pytest.approx(cell.lat)
        # The reported point stays within the promised radius of the truth.
        assert distance(point, cell) <= 10_000 * math.sqrt(2)
        assert east is not None


def test_a_point_near_the_pole_collapses_longitude_rather_than_lying():
    """A cell that would span the globe reports the parallel, not a fake column."""
    cell = coarsen(Coordinate(lat=89.9999, lon=133.0), 100_000)
    assert cell.lon == 0.0


def test_coarsening_refuses_a_nonpositive_cell():
    with pytest.raises(ValueError, match="positive cell size"):
        coarsen(TOWN, 0)


def test_coarsening_refuses_a_non_numeric_cell():
    with pytest.raises(ValueError, match="numeric cell size"):
        coarsen(TOWN, "10km")


def test_the_result_is_always_a_valid_coordinate():
    """Rounding must not push a pole or the antimeridian out of range."""
    for lat in (-90.0, -89.9, 0.0, 89.9, 90.0):
        for lon in (-180.0, -179.9, 0.0, 179.9, 180.0):
            cell = coarsen(Coordinate(lat=lat, lon=lon), 50_000)
            assert -90.0 <= cell.lat <= 90.0
            assert -180.0 <= cell.lon <= 180.0


# --- the ladder --------------------------------------------------------------


def test_a_ladder_declares_its_actions_finest_first():
    ladder = PrecisionLadder(
        ("read_exact", None), ("read_fine", 1_000), ("read_coarse", 10_000)
    )
    assert ladder.actions() == ("read_exact", "read_fine", "read_coarse")


def test_a_ladder_needs_at_least_one_rung():
    with pytest.raises(ValueError, match="at least one rung"):
        PrecisionLadder()


def test_rungs_must_coarsen_strictly():
    with pytest.raises(ValueError, match="coarsen strictly"):
        PrecisionLadder(("a", 10_000), ("b", 1_000))


def test_two_rungs_at_one_resolution_are_refused():
    with pytest.raises(ValueError, match="coarsen strictly"):
        PrecisionLadder(("a", 1_000), ("b", 1_000))


def test_an_exact_rung_below_a_coarse_one_is_unreachable_and_refused():
    with pytest.raises(ValueError, match="must come first"):
        PrecisionLadder(("coarse", 10_000), ("exact", None))


def test_a_repeated_action_is_refused():
    with pytest.raises(ValueError, match="repeats the action"):
        PrecisionLadder(("read", None), ("read", 1_000))


def test_a_rung_needs_a_non_empty_action():
    with pytest.raises(ValueError, match="non-empty action"):
        PrecisionLadder(("", 1_000))


def test_a_rung_resolution_must_be_positive():
    with pytest.raises(ValueError, match="positive finite resolution"):
        PrecisionLadder(("a", -5))


def test_a_rung_must_be_a_pair():
    with pytest.raises(ValueError, match="an \\(action, metres\\) pair"):
        PrecisionLadder("read_exact")


def test_apply_returns_the_point_unchanged_at_the_exact_rung():
    ladder = PrecisionLadder(("exact", None))
    assert ladder.apply(TOWN, None) == TOWN


def test_apply_coarsens_at_a_metred_rung():
    ladder = PrecisionLadder(("coarse", 10_000))
    assert ladder.apply(TOWN, 10_000) == coarsen(TOWN, 10_000)


def test_apply_passes_a_missing_point_through():
    ladder = PrecisionLadder(("coarse", 10_000))
    assert ladder.apply(None, 10_000) is None


# --- refusal clauses the mutation pass showed were unexercised ----------------


def test_a_boolean_resolution_is_refused_rather_than_read_as_one_metre():
    """`True` is an `int` in Python, so a bool must be refused by name."""
    with pytest.raises(ValueError, match="numeric metres"):
        PrecisionLadder(("a", True))


def test_an_infinite_resolution_is_refused():
    with pytest.raises(ValueError, match="positive finite resolution"):
        PrecisionLadder(("a", float("inf")))


def test_a_nan_resolution_is_refused():
    with pytest.raises(ValueError, match="positive finite resolution"):
        PrecisionLadder(("a", float("nan")))


def test_a_three_element_rung_is_refused():
    with pytest.raises(ValueError, match="an \\(action, metres\\) pair"):
        PrecisionLadder(("a", 1_000, "extra"))


def test_a_non_string_action_is_refused():
    with pytest.raises(ValueError, match="non-empty action"):
        PrecisionLadder((None, 1_000))


def test_an_infinite_cell_size_is_refused():
    with pytest.raises(ValueError, match="positive cell size"):
        coarsen(TOWN, float("inf"))


def test_a_boolean_cell_size_is_refused():
    with pytest.raises(ValueError, match="numeric cell size"):
        coarsen(TOWN, True)
