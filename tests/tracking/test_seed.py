from __future__ import annotations

import datetime

import pytest
from tracking.seed import (
    ANIMALS,
    DAYS,
    EPOCH,
    FIX_MINUTES,
    LANDMARKS,
    SILENCES,
    build_rows,
)

from wreath.geospatial import Coordinate, Trajectory, distance


@pytest.fixture(scope="module")
def rows() -> dict:
    """One seeded build, shared by every test that only reads it.

    `build_rows()` generates 51,840 fixes and took 0.47s; nine tests calling it
    for themselves spent 4.2s of the suite regenerating identical data. Module
    scope rather than session so the cost still lands in this file's timings,
    where anyone reading the heat map will look for it.

    **Read-only.** Nothing here mutates the tables, and nothing added here may:
    a test that did would hand the next one a different seed and the failure
    would land wherever the tests happen to be ordered. The determinism test
    deliberately does not take this fixture -- its claim is that two *fresh*
    builds agree, which a shared one cannot make.
    """
    return build_rows()


def test_two_runs_produce_byte_identical_rows() -> None:
    assert build_rows() == build_rows()


def test_no_timestamp_comes_from_the_clock(rows: dict) -> None:
    last = EPOCH + datetime.timedelta(days=DAYS)
    for row in rows["fixes"]:
        assert EPOCH <= row[1] < last


def test_the_row_counts_are_the_ones_the_documentation_quotes(rows: dict) -> None:
    assert len(rows["animals"]) == len(ANIMALS) == 18
    assert len(rows["collars"]) == 18
    assert len(rows["landmarks"]) == len(LANDMARKS) == 6
    assert len(rows["fixes"]) == 18 * DAYS * (24 * 60 // FIX_MINUTES) == 51_840


def test_the_three_protection_tiers_are_all_populated(rows: dict) -> None:
    tiers: dict[str, int] = {}
    for _id, _name, _taxon, protection in rows["animals"]:
        tiers[protection] = tiers.get(protection, 0) + 1
    assert tiers == {"restricted": 2, "sensitive": 5, "open": 11}


def test_the_silences_are_real_gaps_and_not_a_paragraph(rows: dict) -> None:
    for animal, start, length in SILENCES:
        during = [
            row
            for row in rows["fixes"]
            if row[2] == animal
            and EPOCH + datetime.timedelta(days=start)
            <= row[1]
            < EPOCH + datetime.timedelta(days=start + length)
        ]
        assert during, f"animal {animal} has no fixes in its silence"
        arrivals = {row[3] for row in during}
        assert len(arrivals) == 1, "a buffer dump arrives all at once"
        dump = arrivals.pop()
        assert dump > max(row[1] for row in during)

        # **Bounded on both sides.** A silence with no lower edge would make
        # every fix from the beginning of time part of one dump, which still
        # looks like a buffer dump from inside the window and is not one. The
        # day before must arrive promptly.
        before = [
            row
            for row in rows["fixes"]
            if row[2] == animal
            and EPOCH + datetime.timedelta(days=start - 1)
            <= row[1]
            < EPOCH + datetime.timedelta(days=start)
        ]
        assert before, "the silence must not start on the animal's first day"
        assert all(row[3] != dump for row in before), (
            "fixes before the silence share its arrival, so the gap has no lower edge"
        )
        assert max(row[3] - row[1] for row in before) < datetime.timedelta(hours=1)


def test_the_stored_legs_are_the_distances_between_consecutive_fixes(rows: dict) -> None:
    first = [row for row in rows["fixes"] if row[2] == 8][:200]
    assert first[0][8] is None, "the first fix of an animal has no leg before it"
    path = Trajectory([(row[1], Coordinate(lat=row[4], lon=row[5])) for row in first])
    # `approx`, not `==`: `Trajectory.distance` and this sum add the same legs
    # in the same order but through different accumulators, and float addition
    # is not associative. Seven parts in 10^15 apart on 19 km is agreement.
    assert sum(row[8] for row in first[1:]) == pytest.approx(path.distance, rel=1e-12)


def test_the_animals_stay_inside_a_conservancy_sized_area(rows: dict) -> None:
    centre = Coordinate(lat=rows["landmarks"][0][3], lon=rows["landmarks"][0][4])
    furthest = max(distance(centre, Coordinate(lat=row[4], lon=row[5])) for row in rows["fixes"])
    assert furthest < 40_000.0, f"an animal wandered {furthest / 1000:.0f} km from Ndovu"


def test_every_animal_has_a_collar_and_every_fix_has_both(rows: dict) -> None:
    animals = {row[0] for row in rows["animals"]}
    collars = {row[0] for row in rows["collars"]}
    assert {row[1] for row in rows["collars"]} <= animals
    for row in rows["fixes"]:
        assert row[0] in collars
        assert row[2] in animals


def test_the_primary_key_is_unique_across_the_whole_seed(rows: dict) -> None:
    fixes = rows["fixes"]
    assert len({(row[0], row[1]) for row in fixes}) == len(fixes)
