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
    assert degrade(FIX, EXACT) is FIX


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_the_same_fix_always_degrades_to_the_same_answer(grade: Precision) -> None:
    answers = {degrade(FIX, grade) for _ in range(1_000)}
    assert len(answers) == 1


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_two_fixes_in_one_cell_are_indistinguishable(grade: Precision) -> None:
    step = grade.metres / (4.0 * METRES_PER_DEGREE)
    here = degrade(FIX, grade)
    nudged = degrade(Coordinate(lat=FIX.lat + step * 0.1, lon=FIX.lon), grade)
    assert here == nudged or distance(here, nudged) >= grade.metres * 0.5


@pytest.mark.parametrize("grade", [COARSE, APPROXIMATE])
def test_the_answer_stays_inside_the_cell_it_claims(grade: Precision) -> None:
    limit = grade.metres * math.sqrt(2.0) / 2.0 * 1.05
    for lat_step in range(-6, 7):
        for lon_step in range(-6, 7):
            point = Coordinate(lat=-1.9705 + lat_step * 0.037, lon=36.1042 + lon_step * 0.041)
            assert distance(point, degrade(point, grade)) <= limit


def test_a_coarser_grade_is_never_more_informative_than_a_finer_one() -> None:
    coarse = 0.0
    approximate = 0.0
    for step in range(60):
        point = Coordinate(lat=-2.2 + step * 0.008, lon=36.0 + step * 0.011)
        coarse += distance(point, degrade(point, COARSE))
        approximate += distance(point, degrade(point, APPROXIMATE))
    assert approximate > coarse * 5


def test_a_fix_near_the_pole_does_not_raise() -> None:
    for lat in (89.98, -89.98, 90.0, -90.0):
        shown = degrade(Coordinate(lat=lat, lon=17.0), APPROXIMATE)
        assert -90.0 <= shown.lat <= 90.0
        # Every meridian passes through a pole, so longitude has stopped
        # carrying information there -- the same answer `bounding_boxes` gives.
        assert shown.lon == 0.0


def test_a_fix_beside_the_antimeridian_stays_in_range() -> None:
    for lon in (179.99, -179.99, 180.0, -180.0):
        shown = degrade(Coordinate(lat=12.0, lon=lon), COARSE)
        assert -180.0 <= shown.lon <= 180.0


def test_a_cell_boundary_does_not_sit_on_a_round_number() -> None:
    cell = COARSE.metres / METRES_PER_DEGREE
    shown = degrade(Coordinate(lat=0.0, lon=36.0), COARSE)
    assert shown.lat != 0.0
    assert abs(shown.lat) < cell


def test_the_metres_are_what_the_grades_claim() -> None:
    assert (EXACT.metres, COARSE.metres, APPROXIMATE.metres) == (0.0, 1_000.0, 10_000.0)
