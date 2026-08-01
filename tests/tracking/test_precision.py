"""Coarsening a coordinate: the properties the whole scheme rests on.

Three of these matter more than the rest, and each one is a way the obvious
implementation fails:

1. **Repetition buys nothing.** Any scheme that adds noise is defeated by asking
   twice and averaging. This is the test that would fail the day somebody
   "improved" the blur by making it random.
2. **The answer is close to the truth.** A coarsening that wandered further than
   it claimed would be a lie in the other direction -- the ``precision_m`` on
   the wire has to mean something.
3. **It does not raise near the edges.** A pole and the antimeridian are real
   places. `Coordinate` refuses a latitude past 90, so a naive snap of a fix at
   89.98 degrees blows up inside a serializer.

No database, no HTTP, no policy: this file is arithmetic.
"""

from __future__ import annotations

import math

import pytest
from tracking.place import (
    APPROXIMATE,
    COARSE,
    EXACT,
    METRES_PER_DEGREE,
    Precision,
    degrade,
)

from wreath.geospatial import Coordinate, distance

#: A fix in the middle of the seeded conservancy.
FIX = Coordinate(lat=-1.9705, lon=36.1042)


def test_an_exact_grade_returns_the_coordinate_it_was_given() -> None:
    """A ranger's answer passes through untouched, and is the same object.

    Identity rather than equality, because a `degrade` that rebuilt the
    coordinate at zero metres would be doing floating-point arithmetic on a
    number nobody asked it to change.
    """
    assert degrade(FIX, EXACT) is FIX


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_the_same_fix_always_degrades_to_the_same_answer(grade: Precision) -> None:
    """**The property the whole scheme rests on.**

    A reader who asks a thousand times gets one value, so there is nothing to
    average. Any unbiased jitter would be defeated by exactly this loop, and a
    biased one would be a lie about where the animal was; a grid is neither.
    """
    answers = {degrade(FIX, grade) for _ in range(1_000)}
    assert len(answers) == 1


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_two_fixes_in_one_cell_are_indistinguishable(grade: Precision) -> None:
    """Coarsening has to actually lose information, not merely round it.

    A "coarsening" that mapped nearby points to nearby-but-different answers
    would let a reader recover the true position from a handful of fixes by
    watching which way the answers drifted. Points well inside one cell must
    come back identical.
    """
    step = grade.metres / (4.0 * METRES_PER_DEGREE)
    here = degrade(FIX, grade)
    nudged = degrade(Coordinate(lat=FIX.lat + step * 0.1, lon=FIX.lon), grade)
    assert here == nudged or distance(here, nudged) >= grade.metres * 0.5


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_the_answer_stays_inside_the_cell_it_claims(grade: Precision) -> None:
    """`precision_m` on the wire has to mean something.

    The furthest a point can be from the centre of the cell containing it is the
    half-diagonal, about 0.71 of the cell width. Checked over a grid of fixes
    spanning the conservancy rather than at one point, because the longitude
    cell is widened by `1/cos(latitude)` and a bug in that widening only shows
    up away from the equator.
    """
    limit = grade.metres * math.sqrt(2.0) / 2.0 * 1.05
    for lat_step in range(-6, 7):
        for lon_step in range(-6, 7):
            point = Coordinate(lat=-1.9705 + lat_step * 0.037, lon=36.1042 + lon_step * 0.041)
            assert distance(point, degrade(point, grade)) <= limit


def test_a_coarser_grade_is_never_more_informative_than_a_finer_one() -> None:
    """Ten kilometres must not accidentally be sharper than one.

    Two independent grids do not nest, so a 10 km answer is not the 1 km answer
    rounded -- but it must still be *further* from the truth on average, or the
    ladder is decoration. Asserted over a spread of fixes rather than one,
    because a single point can fall near the centre of its coarse cell by luck.
    """
    coarse = 0.0
    approximate = 0.0
    for step in range(60):
        point = Coordinate(lat=-2.2 + step * 0.008, lon=36.0 + step * 0.011)
        coarse += distance(point, degrade(point, COARSE))
        approximate += distance(point, degrade(point, APPROXIMATE))
    assert approximate > coarse * 5


def test_a_fix_near_the_pole_does_not_raise() -> None:
    """A research station near a pole is an ordinary situation, not an error.

    Snapping 89.98 degrees onto a 10 km grid lands past 90, which is not a place
    and which `Coordinate` refuses. Clamping is what stops that refusal from
    surfacing from inside a serializer, at the far end of a response.
    """
    for lat in (89.98, -89.98, 90.0, -90.0):
        shown = degrade(Coordinate(lat=lat, lon=17.0), APPROXIMATE)
        assert -90.0 <= shown.lat <= 90.0
        # Every meridian passes through a pole, so longitude has stopped
        # carrying information there -- the same answer `bounding_boxes` gives.
        assert shown.lon == 0.0


def test_a_fix_beside_the_antimeridian_stays_in_range() -> None:
    """A cell centre just past 180 is a real place a few hundred metres over.

    Wrapped rather than clamped: clamping would pile every fix near the date
    line onto exactly 180.0, which is a different and much more informative
    answer than the one the policy intended.
    """
    for lon in (179.99, -179.99, 180.0, -180.0):
        shown = degrade(Coordinate(lat=12.0, lon=lon), COARSE)
        assert -180.0 <= shown.lon <= 180.0


def test_a_cell_boundary_does_not_sit_on_a_round_number() -> None:
    """`floor` plus a half-cell, not `round`.

    Rounding to the nearest multiple puts cell *boundaries* on the round
    numbers, so a fix at exactly 36.0 degrees would sit on a seam and its answer
    would depend on the last bit of a float. This asserts a whole degree lands
    strictly inside a cell rather than on its edge.
    """
    cell = COARSE.metres / METRES_PER_DEGREE
    shown = degrade(Coordinate(lat=0.0, lon=36.0), COARSE)
    assert shown.lat != 0.0
    assert abs(shown.lat) < cell


def test_the_metres_are_what_the_grades_claim() -> None:
    """One kilometre and ten, not one and a half.

    A constant that drifted would leave every other test in this file passing --
    they are all written relative to `grade.metres` -- while quietly changing
    what a partner institution is shown.
    """
    assert (EXACT.metres, COARSE.metres, APPROXIMATE.metres) == (0.0, 1_000.0, 10_000.0)
