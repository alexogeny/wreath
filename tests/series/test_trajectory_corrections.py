from __future__ import annotations

import datetime

import pytest

from wreath._series.settle import difference, fold
from wreath.geospatial import BoundingBox, Coordinate, GeospatialError, Trajectory, grid
from wreath.series import Measure, avg

MONDAY = datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC)
TUESDAY = datetime.datetime(2026, 3, 3, tzinfo=datetime.UTC)
WEDNESDAY = datetime.datetime(2026, 3, 4, tzinfo=datetime.UTC)


def at(day: datetime.datetime, hours: float) -> datetime.datetime:
    return day + datetime.timedelta(hours=hours)


def fix(when: datetime.datetime, lat: float, lon: float):
    return (when, Coordinate(lat=lat, lon=lon))


#: A straight-ish run east, one fix every six hours.
BASE = [
    fix(at(MONDAY, 0), -29.00, 150.00),
    fix(at(MONDAY, 12), -29.00, 150.10),
    fix(at(TUESDAY, 0), -29.00, 150.20),
    fix(at(TUESDAY, 12), -29.00, 150.30),
]


class TestAWindowedDistanceAddsUp:
    def test_the_windows_partition_the_whole_distance(self):
        path = Trajectory(BASE)
        monday = path.between(MONDAY, TUESDAY).distance
        tuesday = path.between(TUESDAY, WEDNESDAY).distance
        assert monday + tuesday == pytest.approx(path.distance, rel=1e-9)

    def test_a_window_with_no_fixes_still_carries_the_leg_through_it(self):
        # A vehicle that drove through Tuesday without reporting still covered
        # ground on Tuesday. The anchor is what says so.
        sparse = Trajectory([BASE[0], BASE[3]])
        tuesday = sparse.between(TUESDAY, WEDNESDAY)
        assert len(tuesday) == 2
        assert tuesday.distance > 0.0

    def test_a_window_before_any_fix_is_empty(self):
        path = Trajectory(BASE)
        earlier = path.between(MONDAY - datetime.timedelta(days=2), MONDAY)
        assert len(earlier) == 0
        assert earlier.distance == 0.0

    def test_the_window_is_half_open(self):
        path = Trajectory(BASE)
        monday = path.between(MONDAY, TUESDAY)
        # The fix exactly on Tuesday belongs to Tuesday, and appears in
        # Monday's path only as the anchor for the following window.
        assert [when for when, _ in monday.fixes] == [at(MONDAY, 0), at(MONDAY, 12)]

    @pytest.mark.parametrize(
        "start, end",
        [
            (datetime.datetime(2026, 3, 2), TUESDAY),
            (MONDAY, datetime.datetime(2026, 3, 3)),
            (datetime.datetime(2026, 3, 2), datetime.datetime(2026, 3, 3)),
        ],
    )
    def test_a_naive_bound_is_refused(self, start, end):
        path = Trajectory(BASE)
        with pytest.raises(GeospatialError, match="between\\(\\) takes aware timestamps"):
            path.between(start, end)

    def test_an_inverted_window_is_refused(self):
        path = Trajectory(BASE)
        with pytest.raises(GeospatialError, match="is before start"):
            path.between(TUESDAY, MONDAY)

    def test_grid_summary_matches_the_materialized_window(self):
        path = Trajectory(BASE)
        lattice = grid(BoundingBox(-29.5, -28.5, 149.5, 150.5), metres=20_000)
        window = path.between(MONDAY, WEDNESDAY)
        expected_cells = {
            found for _when, point in window.fixes if (found := lattice.index_of(point)) is not None
        }
        cells, speed = path.grid_summary(MONDAY, WEDNESDAY, lattice)
        assert set(cells) == expected_cells
        assert speed == pytest.approx(window.speed)

    def test_grid_summary_keeps_the_window_anchor(self):
        path = Trajectory(BASE)
        lattice = grid(BoundingBox(-29.5, -28.5, 149.5, 150.5), metres=20_000)
        cells, speed = path.grid_summary(TUESDAY, WEDNESDAY, lattice)
        window = path.between(TUESDAY, WEDNESDAY)
        assert set(cells) == {lattice.index_of(point) for _when, point in window.fixes}
        assert speed == pytest.approx(window.speed)

    def test_grid_summary_keeps_only_the_identical_latest_window(self):
        path = Trajectory(BASE)
        lattice = grid(BoundingBox(-29.5, -28.5, 149.5, 150.5), metres=20_000)
        first = path.grid_summary(MONDAY, WEDNESDAY, lattice)

        assert path.grid_summary(MONDAY, WEDNESDAY, lattice) is first

        equal_start = MONDAY.replace()
        second = path.grid_summary(equal_start, WEDNESDAY, lattice)
        assert second == first
        assert second is not first


class TestATrajectoryRefusesWhatItCannotMeasure:
    """The constructor's own guards, which `between()`'s tests cannot reach.

    Same shape as the naive-bound refusal above, one step earlier: a trajectory
    built entirely from naive timestamps sorts perfectly well and answers every
    question, wrongly, across a DST boundary. Nothing failed when the guard was
    deleted, because every existing test hands it aware fixes -- the refusal was
    never the reason any of them passed.
    """

    def test_a_naive_timestamp_is_refused_when_the_trajectory_is_built(self):
        naive = [
            (datetime.datetime(2026, 3, 2, 0), Coordinate(lat=-29.0, lon=150.0)),
            (datetime.datetime(2026, 3, 2, 12), Coordinate(lat=-29.0, lon=150.1)),
        ]
        with pytest.raises(GeospatialError, match="timestamp with no timezone"):
            Trajectory(naive)

    def test_the_refusal_names_the_offending_fix(self):
        with pytest.raises(GeospatialError, match=r"fix 1 has a timestamp"):
            Trajectory([BASE[0], (datetime.datetime(2026, 3, 3, 0), BASE[1][1])])

    def test_a_fix_that_is_not_a_pair_is_refused(self):
        with pytest.raises(GeospatialError, match=r"fix 0 must be a \(timestamp"):
            Trajectory([(at(MONDAY, 0),)])

    def test_a_fix_with_no_length_at_all_is_refused_by_name(self):
        with pytest.raises(GeospatialError, match=r"fix 0 must be a \(timestamp"):
            Trajectory([at(MONDAY, 0)])

        with pytest.raises(GeospatialError, match=r"fix 1 must be a \(timestamp"):
            Trajectory([BASE[0], None])

    def test_a_fix_whose_position_is_not_a_coordinate_is_refused(self):
        with pytest.raises(GeospatialError, match="must carry a Coordinate, got tuple"):
            Trajectory([(at(MONDAY, 0), (-29.0, 150.0))])


class TestDistanceIsMonotoneUnderALateFix:
    def test_a_late_fix_inside_the_path_can_only_lengthen_it(self):
        before = Trajectory(BASE).distance
        late = fix(at(MONDAY, 6), -29.05, 150.05)  # off the straight line
        after = Trajectory([*BASE, late]).distance
        assert after >= before

    def test_a_fix_on_the_line_leaves_the_distance_almost_unchanged(self):
        before = Trajectory(BASE).distance
        on_line = fix(at(MONDAY, 6), -29.00, 150.05)
        after = Trajectory([*BASE, on_line]).distance
        assert after == pytest.approx(before, rel=1e-6)

    def test_a_detour_lengthens_it_measurably(self):
        before = Trajectory(BASE).distance
        detour = fix(at(MONDAY, 6), -29.50, 150.05)
        after = Trajectory([*BASE, detour]).distance
        assert after > before * 1.5


class TestACorrectionReachesTheRecomputedTruth:
    """The composition itself: seal a day, land a late fix, record the delta.

    `fold(settled, delta) == recomputed` is the contract `difference`
    documents. These drive it with a *path* measure, whose late arrival is not
    an added row but a split leg -- the case the sealing design was not written
    against and which it nonetheless handles, because the delta is computed
    from the totals rather than from the arriving row.
    """

    def test_a_late_fix_inside_a_sealed_day_folds_to_the_recomputed_value(self):
        measures = (("distance", _distance()),)
        path = Trajectory(BASE)
        sealed = {"distance": path.between(MONDAY, TUESDAY).distance}

        late = fix(at(MONDAY, 6), -29.20, 150.05)
        corrected = Trajectory([*BASE, late])
        recomputed = {"distance": corrected.between(MONDAY, TUESDAY).distance}

        delta = difference(sealed, recomputed, measures)
        assert delta is not None, "a late fix that moved the distance must record one"
        assert fold(sealed, delta) == pytest.approx(recomputed)

    def test_the_recorded_delta_is_a_difference_and_is_non_negative(self):
        measures = (("distance", _distance()),)
        path = Trajectory(BASE)
        sealed = {"distance": path.between(MONDAY, TUESDAY).distance}
        late = fix(at(MONDAY, 6), -29.20, 150.05)
        recomputed = {"distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance}

        delta = difference(sealed, recomputed, measures)
        # A plain number, not a {"set": ...} replacement: distance is additive,
        # so the settled value survives and the difference rides beside it.
        assert isinstance(delta["distance"], float)
        assert delta["distance"] >= 0.0

    def test_a_day_nothing_landed_in_records_no_correction(self):
        measures = (("distance", _distance()),)
        path = Trajectory(BASE)
        sealed = {"distance": path.between(MONDAY, TUESDAY).distance}
        # The late fix lands on Tuesday, so Monday is unchanged and a reconcile
        # over it must write nothing at all rather than a row of zeroes.
        late = fix(at(TUESDAY, 6), -29.20, 150.25)
        recomputed = {"distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance}
        assert difference(sealed, recomputed, measures) is None

    def test_the_sealed_value_is_never_mutated(self):
        measures = (("distance", _distance()),)
        sealed = {"distance": Trajectory(BASE).between(MONDAY, TUESDAY).distance}
        original = dict(sealed)
        late = fix(at(MONDAY, 6), -29.20, 150.05)
        recomputed = {"distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance}
        folded = fold(sealed, difference(sealed, recomputed, measures))
        assert sealed == original
        assert folded is not sealed


class TestSpeedCorrectsByReplacementNotDifference:
    def test_a_ratio_carries_the_answer_rather_than_a_delta(self):
        measures = (("speed", avg(_column())),)
        path = Trajectory(BASE)
        sealed = {"speed": path.between(MONDAY, TUESDAY).speed}
        late = fix(at(MONDAY, 6), -29.20, 150.05)
        recomputed = {"speed": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).speed}

        delta = difference(sealed, recomputed, measures)
        assert delta is not None
        assert delta["speed"] == {"set": recomputed["speed"]}
        assert fold(sealed, delta) == pytest.approx(recomputed)


def _distance() -> Measure:
    """A path length, declared with a sum's correction rules.

    Distance over a window is additive and has an identity of zero: a window a
    subject did not move in covered no ground, which is a real zero rather than
    an undefined value. That is the whole of what `difference` needs to
    know to correct it by delta rather than by replacement.
    """
    return Measure("SUM", None, "sum", "m", 0, True)


def _column():
    """The measure `avg` wants a column; the ratio under test is computed in
    Python, so this only has to satisfy the constructor."""
    from tests.series.conftest import Sighting

    return Sighting.weight_kg
