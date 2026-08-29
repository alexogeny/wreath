from __future__ import annotations

import datetime

import pytest

from wreath._passes.driver import Binds
from wreath.passes import (
    Buckets,
    Ceiling,
    ChunkedPass,
    DutyCycle,
    Key,
    PassDeclarationError,
    Purge,
    Rows,
    Sealed,
    Table,
)
from wreath.temporal import Day, Hour, Month

from .fakes import FakeDatabase, World, evaluate

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)
RECORDED = Key("recorded_at", "timestamptz", indexed=True)


async def _nap(_seconds):
    return None


def rollup_pass(**overrides):
    options = {
        "over": Table("treks"),
        "units": Buckets(on=RECORDED, step=Day, zone="UTC"),
        "frontier": Sealed(),
        "work": Purge(),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("fold_treks", **options)


def treks_across(days: int, *, per_day: int = 2, start_day: int = 24) -> list[dict]:
    """*per_day* rows on each of *days* consecutive days from July *start_day*."""
    rows = []
    for offset in range(days):
        for index in range(per_day):
            rows.append(
                {
                    "id": f"t{offset}{index}",
                    "recorded_at": datetime.datetime(
                        2026, 7, start_day + offset, 6 + index, 0, tzinfo=datetime.UTC
                    ),
                }
            )
    return rows


@pytest.fixture
def world():
    # Four days of treks: the 24th through the 27th. "Now" is noon on the 27th,
    # so the 27th's bucket has not closed yet.
    return World("treks", treks_across(4))


@pytest.fixture
def database(world):
    return FakeDatabase(world)


def test_a_bucket_key_does_not_have_to_be_unique():
    # The refusal a keyset walk needs and a bucketed one must not inherit. A
    # timestamp column is emphatically not unique, and that is fine here.
    walk = rollup_pass()

    assert walk.units.keys[0].unique is False


def test_the_same_key_is_still_refused_for_a_keyset_walk():
    # The mirror of the test above, so "Buckets does not ask" cannot quietly
    # become "nothing asks".
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Rows(key=RECORDED, limit=10))

    assert "not unique" in str(caught.value)


def test_an_unindexed_bucket_column_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=Key("recorded_at", "timestamptz")))

    message = str(caught.value)
    assert "no index" in message
    assert "range scan over one bucket" in message


def test_a_non_temporal_bucket_column_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=Key("name", "text", indexed=True)))

    assert "needs a timestamp column" in str(caught.value)


def test_a_composite_bucket_key_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=(RECORDED, Key("id", "text", unique=True))))

    assert "one temporal column" in str(caught.value)


def test_an_unknown_step_is_refused_at_declaration():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=RECORDED, step="fortnight"))

    assert "wreath.temporal bucket" in str(caught.value)


def test_an_unknown_zone_is_refused_at_declaration():
    with pytest.raises(Exception) as caught:
        rollup_pass(units=Buckets(on=RECORDED, zone="Mars/Olympus"))

    assert "Mars/Olympus" in str(caught.value)


def test_a_naive_since_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=RECORDED, since=datetime.datetime(2026, 7, 1)))

    assert "must carry a zone" in str(caught.value)


def test_per_chunk_must_be_at_least_one():
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(units=Buckets(on=RECORDED, per_chunk=0))

    assert "at least 1" in str(caught.value)


def test_the_next_range_is_computed_not_queried(database, world):
    units = Buckets(on=RECORDED, step=Day, zone="UTC")
    start = datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)

    assert units.advance(start) == datetime.datetime(2026, 7, 25, tzinfo=datetime.UTC)
    # Nothing was asked of the database to work that out.
    assert world.statements == []


def test_a_month_step_is_calendar_arithmetic_not_thirty_days():
    units = Buckets(on=RECORDED, step=Month, zone="UTC")
    february = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)

    assert units.advance(february) == datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)


def test_a_day_across_a_dst_change_is_not_twenty_four_hours():
    # Auckland leaves daylight saving on 2026-04-05, so that local day really
    # runs for 25 hours. A source that stepped a fixed timedelta would cut the
    # bucket an hour short and file an hour of rows under the wrong day, twice
    # a year, in whichever direction nobody checked.
    units = Buckets(on=RECORDED, step=Day, zone="Pacific/Auckland")
    start = units.floor(datetime.datetime(2026, 4, 5, 3, tzinfo=datetime.UTC))
    end = units.advance(start)

    # The offset really moved, which is what makes this day unusual.
    assert start.utcoffset() == datetime.timedelta(hours=13)
    assert end.utcoffset() == datetime.timedelta(hours=12)
    elapsed = end.astimezone(datetime.UTC) - start.astimezone(datetime.UTC)
    assert elapsed == datetime.timedelta(hours=25)


def test_subtracting_two_bucket_boundaries_directly_is_the_trap():
    # Pinned deliberately, because it is CPython's rule rather than this
    # module's and it bites exactly where nobody is looking. Two aware datetimes
    # that share a `tzinfo` subtract on the *wall clock*, so the 25-hour day
    # above measures 24 unless both sides are converted first. Correct on every
    # day but two a year, which is the worst way for something to be wrong.
    units = Buckets(on=RECORDED, step=Day, zone="Pacific/Auckland")
    start = units.floor(datetime.datetime(2026, 4, 5, 3, tzinfo=datetime.UTC))
    end = units.advance(start)

    assert (end - start) == datetime.timedelta(hours=24)
    assert end.astimezone(datetime.UTC) - start.astimezone(datetime.UTC) != (end - start)


def test_per_chunk_covers_several_buckets_in_one_range():
    units = Buckets(on=RECORDED, step=Hour, zone="UTC", per_chunk=6)
    start = datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)

    assert units.advance(start) == datetime.datetime(2026, 7, 24, 6, tzinfo=datetime.UTC)


async def test_a_bucketed_walk_drops_into_the_existing_machinery(database, world):
    walk = rollup_pass()

    result = await walk.run(database, sleep=_nap)

    # Three sealed days -- the 24th, 25th and 26th -- two rows each.
    assert result.chunks == 3
    assert result.rows == 6
    assert result.complete is True


async def test_a_bucket_whose_end_has_not_passed_is_left_alone(database, world):
    # The sealing rule, and the reason the frontier is tested against the end of
    # a range rather than its start: at noon on the 27th, the 27th is still
    # accepting rows, so folding it in would settle a number that is still moving.
    walk = rollup_pass()

    await walk.run(database, sleep=_nap)

    survivors = sorted(row["id"] for row in world.rows)
    assert survivors == ["t30", "t31"]
    assert all(row["recorded_at"].day == 27 for row in world.rows)


async def test_the_walk_asks_the_table_where_to_start_once_per_cycle(database, world):
    walk = rollup_pass()

    await walk.run(database, sleep=_nap)

    anchors = world.sql_of("SELECT min(recorded_at)")
    assert len(anchors) == 1


async def test_since_removes_even_that_one_query(database, world):
    walk = rollup_pass(
        units=Buckets(
            on=RECORDED,
            step=Day,
            zone="UTC",
            since=datetime.datetime(2026, 7, 25, tzinfo=datetime.UTC),
        )
    )

    result = await walk.run(database, sleep=_nap)

    assert world.sql_of("SELECT min(") == []
    # Starting on the 25th leaves the 24th's rows where they are.
    assert result.chunks == 2
    assert sorted(row["id"] for row in world.rows) == ["t00", "t01", "t30", "t31"]


async def test_an_empty_table_completes_rather_than_looping(database):
    empty = World("treks", [])
    walk = rollup_pass()

    result = await walk.run(FakeDatabase(empty), sleep=_nap)

    assert result.complete is True
    assert result.chunks == 0


def test_a_bucket_chunk_is_half_open_at_the_top():
    # A row landing exactly on a boundary belongs to the bucket that *starts*
    # there, never to the one that ends there. A closed top would put it in
    # both, which double-counts every boundary row in a rollup and is the kind
    # of error that shows up as a total that is slightly, unaccountably high.
    units = Buckets(on=RECORDED, step=Day, zone="UTC")
    binds = Binds()
    where = units.chunk_where(
        binds,
        cursor_from=(datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC),),
        cursor_to=(datetime.datetime(2026, 7, 25, tzinfo=datetime.UTC),),
        frontier=None,
    )
    args = binds.args

    on_lower = {"recorded_at": datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC)}
    on_upper = {"recorded_at": datetime.datetime(2026, 7, 25, tzinfo=datetime.UTC)}
    inside = {"recorded_at": datetime.datetime(2026, 7, 24, 13, tzinfo=datetime.UTC)}

    assert evaluate(where, on_lower, args) is True
    assert evaluate(where, inside, args) is True
    assert evaluate(where, on_upper, args) is False


async def test_the_cursor_survives_a_stopped_shift(database, world):
    walk = rollup_pass(shift="10s")

    first = await walk.run_shift(database, budget=0.0, sleep=_nap)
    assert first.stopped == "budget"

    second = await walk.run(database, sleep=_nap)
    assert second.complete is True
    assert sorted(row["id"] for row in world.rows) == ["t30", "t31"]


async def test_a_recurring_bucketed_pass_starts_a_new_cycle(database, world):
    walk = rollup_pass()
    await walk.run(database, sleep=_nap)

    # A late trek arrives for a day the walk has already folded, and the clock
    # moves on so the 27th has now sealed.
    world.rows.append(
        {"id": "late", "recorded_at": datetime.datetime(2026, 7, 25, 9, tzinfo=datetime.UTC)}
    )
    world.now = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=datetime.UTC)

    await walk.run(database, sleep=_nap)

    # The next cycle rewinds to the start of the domain, so the late row is
    # found -- which is why a re-derived frontier is sound where a fixed ceiling
    # would need the key to be assigned in order.
    assert world.rows == []


async def test_a_fixed_ceiling_over_buckets_still_asks_about_monotonicity(database):
    # `Buckets` does not need the *uniqueness* refusal, but a fixed ceiling's
    # monotonicity question is about the frontier rather than the range source,
    # so it is still asked.
    with pytest.raises(PassDeclarationError) as caught:
        rollup_pass(frontier=Ceiling.at_launch())

    assert "assigned in increasing order" in str(caught.value)
