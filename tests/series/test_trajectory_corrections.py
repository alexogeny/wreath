"""Slice 6: a derived spatial measure that respects late data.

A collar buffers three days out of satellite range and then dumps; a van
backfills after a dead zone. The fixes land *behind* the watermark, and they
change a distance that has already been reported.

Wreath's answer to a late row is already written down: the settled value stays
immutable and the difference is recorded beside it, folded in on read. What
this file establishes is that the rule extends to a **path** measure, where a
late fix is not simply another row -- it splits a leg in two, so its
contribution is not "what that row measures" but "what the whole path measures
now, minus what it measured then".

Two findings are pinned here because they are properties of the geometry rather
than of the implementation, and neither is obvious:

* **Distance is monotone under insertion.** A fix landing between two others
  replaces one leg with two, and the triangle inequality holds on a sphere, so
  the total can only rise. A correction to a sealed distance is therefore never
  negative, which is a real check on a reconcile that claims otherwise.
* **Speed is not additively correctable, for the same reason an average is not.**
  It is a ratio whose denominator also moves, so a correction must carry the
  replacement rather than a difference -- which is exactly the split
  `difference` already makes on `has_identity`.
"""

from __future__ import annotations

import datetime

import pytest

from wreath._series.settle import difference, fold
from wreath.geospatial import Coordinate, GeospatialError, Trajectory
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
        """The anchor property, which is the reason `between` takes one.

        Without it every leg crossing midnight is lost, and each day still
        looks plausible on its own.
        """
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
        """Both bounds, and the refusal named rather than merely raised.

        The first version of this asserted `Exception` matching "aware", which
        had no teeth: with the guard removed, comparing a naive bound to an
        aware fix raises `TypeError: can't compare offset-naive and offset-aware
        datetimes`, which also matches. A mutation pass removed the guard and
        nothing objected. It now pins the type and the sentence, so the guard is
        what is being tested rather than datetime's own error.
        """
        path = Trajectory(BASE)
        with pytest.raises(GeospatialError, match="between\\(\\) takes aware timestamps"):
            path.between(start, end)

    def test_an_inverted_window_is_refused(self):
        path = Trajectory(BASE)
        with pytest.raises(GeospatialError, match="is before start"):
            path.between(TUESDAY, MONDAY)


class TestDistanceIsMonotoneUnderALateFix:
    def test_a_late_fix_inside_the_path_can_only_lengthen_it(self):
        """The triangle inequality, asserted rather than assumed.

        This is what makes a distance correction always non-negative, and it is
        the property a reconcile that produced a negative delta would violate.
        """
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
        recomputed = {
            "distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance
        }

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
        recomputed = {
            "distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance
        }
        assert difference(sealed, recomputed, measures) is None

    def test_the_sealed_value_is_never_mutated(self):
        measures = (("distance", _distance()),)
        sealed = {"distance": Trajectory(BASE).between(MONDAY, TUESDAY).distance}
        original = dict(sealed)
        late = fix(at(MONDAY, 6), -29.20, 150.05)
        recomputed = {
            "distance": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).distance
        }
        folded = fold(sealed, difference(sealed, recomputed, measures))
        assert sealed == original
        assert folded is not sealed


class TestSpeedCorrectsByReplacementNotDifference:
    def test_a_ratio_carries_the_answer_rather_than_a_delta(self):
        """Speed is `avg`-shaped, and the existing split already knows it.

        `difference` decides additive-versus-replacement on
        `has_identity`, and a mean speed has no identity element for the
        same reason an average does not: the denominator moves too. Adding a
        delta to a stale ratio would produce a number that is not a speed of
        anything.
        """
        measures = (("speed", avg(_column())),)
        path = Trajectory(BASE)
        sealed = {"speed": path.between(MONDAY, TUESDAY).speed}
        late = fix(at(MONDAY, 6), -29.20, 150.05)
        recomputed = {
            "speed": Trajectory([*BASE, late]).between(MONDAY, TUESDAY).speed
        }

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
