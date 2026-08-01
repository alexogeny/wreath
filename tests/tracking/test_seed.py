"""The seed is reproducible, and it contains the cases the example claims.

No database: `build_rows` is deliberately separable from the insert, so the
determinism claim is checkable in milliseconds and the "the data has these
properties" claims are checkable at all. Both matter for the same reason -- the
documentation quotes numbers out of this data, and a number in a document that
moves between runs is worse than no number.
"""

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


def test_two_runs_produce_byte_identical_rows() -> None:
    """One seeded generator and no clock reads, which is the whole claim.

    Compared as whole tables rather than as counts: a generator reseeded per
    animal would produce the same *number* of rows every time while producing
    different coordinates, and a count assertion would never notice.
    """
    assert build_rows() == build_rows()


def test_no_timestamp_comes_from_the_clock() -> None:
    """Every instant is an offset from `EPOCH`, so nothing moves overnight.

    Checked by bound rather than by mocking `datetime`: every `recorded_at` has
    to lie inside the window the constants describe, and a `now()` anywhere in
    the generator lands outside it.
    """
    rows = build_rows()
    last = EPOCH + datetime.timedelta(days=DAYS)
    for row in rows["fixes"]:
        assert EPOCH <= row[1] < last


def test_the_row_counts_are_the_ones_the_documentation_quotes() -> None:
    """18 animals, 18 collars, 6 landmarks, 51,840 fixes."""
    rows = build_rows()
    assert len(rows["animals"]) == len(ANIMALS) == 18
    assert len(rows["collars"]) == 18
    assert len(rows["landmarks"]) == len(LANDMARKS) == 6
    assert len(rows["fixes"]) == 18 * DAYS * (24 * 60 // FIX_MINUTES) == 51_840


def test_the_three_protection_tiers_are_all_populated() -> None:
    """Two restricted, five sensitive, eleven open.

    An example whose sensitive tier was empty would pass every policy test in
    this suite and prove nothing about the application, because no request would
    ever reach the interesting branch.
    """
    tiers: dict[str, int] = {}
    for _id, _name, _taxon, protection in build_rows()["animals"]:
        tiers[protection] = tiers.get(protection, 0) + 1
    assert tiers == {"restricted": 2, "sensitive": 5, "open": 11}


def test_the_silences_are_real_gaps_and_not_a_paragraph() -> None:
    """Two collars go quiet, and their fixes arrive in one dump afterwards.

    This is the seed's contribution to the late-data chapter: without it, the
    sealing story would need a test to manufacture its own late data, and the
    example would be describing a case it does not have.
    """
    rows = build_rows()
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


def test_the_stored_legs_are_the_distances_between_consecutive_fixes() -> None:
    """The seeded `leg_m` is what `Trajectory` would measure, not an estimate.

    The ingest path maintains this column and a test holds it to `Trajectory`
    there too. Asserting it in the seed as well is what stops the two halves of
    the data -- seeded history and ingested present -- from disagreeing about
    what a leg is.
    """
    rows = build_rows()
    first = [row for row in rows["fixes"] if row[2] == 8][:200]
    assert first[0][8] is None, "the first fix of an animal has no leg before it"
    path = Trajectory(
        [(row[1], Coordinate(lat=row[4], lon=row[5])) for row in first]
    )
    # `approx`, not `==`: `Trajectory.distance` and this sum add the same legs
    # in the same order but through different accumulators, and float addition
    # is not associative. Seven parts in 10^15 apart on 19 km is agreement.
    assert sum(row[8] for row in first[1:]) == pytest.approx(path.distance, rel=1e-12)


def test_the_animals_stay_inside_a_conservancy_sized_area() -> None:
    """A pull towards water, so a season does not diffuse a wildebeest into Tanzania.

    Without it `within(waterhole, 5 km)` finds nothing after the first week and
    the proximity chapter has no data. The bound is loose because the exact
    extent is the walk's business; what is asserted is that there *is* one.
    """
    rows = build_rows()
    centre = Coordinate(lat=rows["landmarks"][0][3], lon=rows["landmarks"][0][4])
    furthest = max(
        distance(centre, Coordinate(lat=row[4], lon=row[5])) for row in rows["fixes"]
    )
    assert furthest < 40_000.0, f"an animal wandered {furthest / 1000:.0f} km from Ndovu"


def test_every_animal_has_a_collar_and_every_fix_has_both() -> None:
    """Referential integrity, before the database is asked to enforce it.

    A seeder that violated a foreign key fails on the insert with a message
    naming a constraint rather than the row that was wrong, which is a much
    worse place to find out.
    """
    rows = build_rows()
    animals = {row[0] for row in rows["animals"]}
    collars = {row[0] for row in rows["collars"]}
    assert {row[1] for row in rows["collars"]} <= animals
    for row in rows["fixes"]:
        assert row[0] in collars
        assert row[2] in animals


def test_the_primary_key_is_unique_across_the_whole_seed() -> None:
    """`(collar_id, recorded_at)` is the identity, so the seed cannot collide.

    A duplicate would surface as a unique-violation on the insert, and the
    generator that produced it -- one fix per collar per twenty-minute slot --
    is exactly the shape that goes wrong if the slot arithmetic is off by one.
    """
    rows = build_rows()["fixes"]
    assert len({(row[0], row[1]) for row in rows}) == len(rows)
