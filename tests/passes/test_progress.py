"""Stage two: a percentage with a provenance, a rate, and an honestly absent ETA.

The rules under test are the ones that make a status line worth reading at three
in the morning. A percentage always carries where its denominator came from. An
ETA is absent rather than invented. And the three states that need three
different responses -- slow, stalled, blocked -- are told apart rather than
lumped into "still going".
"""

from __future__ import annotations

import datetime

import pytest

from wreath._passes import progress as _progress
from wreath._passes.ledger import LedgerRow
from wreath.passes import (
    ChunkedPass,
    DutyCycle,
    Exact,
    Key,
    Keyspace,
    PassDeclarationError,
    Purge,
    Rows,
    Sealed,
    Table,
)

from .fakes import FakeDatabase, World

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)


async def _nap(_seconds):
    return None


def purge_pass(**overrides):
    options = {
        "over": Table("replays"),
        "units": Rows(key=(EXPIRES, KEY), limit=3, within="2s"),
        "frontier": Sealed(),
        "work": Purge(),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("purge_replays", **options)


def _row(**overrides):
    body = {
        "name": "purge_replays", "tenant": "", "phase": "walking",
        "cursor": None, "ceiling": None, "keyspace_from": None, "pending": [],
        "units_done": 0, "rows_done": 0, "denominator": None,
        "denominator_kind": None, "chunk_limit": 1000, "paced_reason": None,
        "window_started": None, "window_rows": 0, "window_units": 0,
        "started_at": NOW, "last_advance": None, "cycle_started": None,
        "driven_at": NOW, "last_drive_error": None, "verified_at": None,
        "verified_fact": None, "last_error": None, "now": NOW, "holes_open": 0,
    }
    body.update(overrides)
    return LedgerRow(**body)


def _ago(seconds):
    return NOW - datetime.timedelta(seconds=seconds)


@pytest.fixture
def world():
    rows = [
        {"key": f"k{index:03d}", "expires": NOW - datetime.timedelta(hours=index + 1)}
        for index in range(9)
    ]
    return World("replays", rows)


@pytest.fixture
def database(world):
    return FakeDatabase(world)


# --- the denominator and its provenance ---------------------------------------


async def test_the_default_denominator_is_the_free_one(database, world):
    world.reltuples = 9000
    walk = purge_pass()

    await walk.run(database, sleep=_nap)
    status = await walk.status(database)

    # A full count in front of a long pass delays the work to make a progress
    # bar prettier, so `estimated` is the default and it is stated as such.
    assert status.progress.denominator_kind == "estimated"
    assert status.progress.denominator == 9000
    assert "reltuples" in " ".join(world.sql_of("reltuples"))


async def test_an_exact_denominator_counts_and_says_that_it_counted(database, world):
    walk = purge_pass(progress=Exact())

    await walk.run(database, sleep=_nap)
    status = await walk.status(database)

    assert status.progress.denominator_kind == "exact"
    assert status.progress.denominator == 9
    assert world.sql_of("SELECT count(*) FROM replays")


async def test_a_table_that_was_never_analysed_reports_no_percentage(database, world):
    # reltuples answers -1 for a table ANALYZE has never touched. A negative
    # denominator is not a denominator, and no percentage beats a wrong one.
    world.reltuples = None
    walk = purge_pass()

    await walk.run(database, sleep=_nap)
    status = await walk.status(database)

    assert status.progress.denominator is None
    assert status.progress.percent is None


async def test_a_percentage_never_travels_without_its_provenance(database, world):
    world.reltuples = 9
    walk = purge_pass()

    await walk.run(database, sleep=_nap)
    body = (await walk.status(database)).as_dict()

    # Not two independent fields that a caller might render one of: whenever
    # there is a percent there is a kind, because "64%" and "64% (estimated)"
    # are different sentences and only one of them is true.
    assert (body["percent"] is None) == (body["denominator_kind"] is None)


def test_a_keyspace_percentage_is_where_the_cursor_sits_between_the_ends():
    row = _row(
        denominator_kind="keyspace",
        keyspace_from=["2026-07-27T00:00:00+00:00", "k000"],
        ceiling=["2026-07-27T10:00:00+00:00"],
        cursor=["2026-07-27T02:30:00+00:00", "k042"],
    )

    assert _progress.percent_of(row, ()) == pytest.approx(25.0)


def test_a_keyspace_walk_that_has_not_started_is_at_zero():
    row = _row(
        denominator_kind="keyspace",
        keyspace_from=[0],
        ceiling=[1000],
        cursor=None,
    )

    assert _progress.percent_of(row, ()) == 0.0


def test_keyspace_is_refused_on_a_key_it_cannot_place_on_a_line():
    with pytest.raises(PassDeclarationError) as caught:
        purge_pass(
            units=Rows(key=(Key("token", "text", indexed=True, unique=True),), limit=3),
            frontier=None,
            progress=Keyspace(),
        )

    assert "number or a timestamp" in str(caught.value)


def test_a_denominator_kind_that_is_not_one_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        purge_pass(progress="estimated")

    assert "Estimated(), Exact() or Keyspace()" in str(caught.value)


# --- rate, and refusing to guess ----------------------------------------------


def test_the_rate_is_measured_over_the_trailing_window_not_since_launch():
    row = _row(
        started_at=_ago(3600), rows_done=1_000_000,
        window_started=_ago(20), window_rows=100, window_units=5,
        last_advance=NOW,
    )

    # Ten minutes of hard pacing should show up as five rows a second, not be
    # averaged away against a fast first hour.
    assert _progress.rate_of(row) == pytest.approx(5.0)


def test_an_empty_rate_window_has_no_rate_rather_than_a_rate_of_zero():
    row = _row(window_started=_ago(20), window_rows=0, window_units=0, last_advance=NOW)

    assert _progress.rate_of(row) is None


def test_no_rate_means_no_eta_and_the_row_says_which_input_was_missing():
    row = _row(
        denominator=1000, denominator_kind="estimated", rows_done=100,
        window_started=None, window_units=0, last_advance=None,
    )

    reported = _progress.describe(row, (), now=NOW)

    # Not infinity, not zero, not "calculating..." forever. Somebody plans
    # around an ETA, so a fabricated one is worse than none at all.
    assert reported.eta_seconds is None
    assert "rate window is empty" in reported.eta_absent


def test_no_denominator_means_no_eta_and_says_so():
    row = _row(
        denominator=None, window_started=_ago(10), window_rows=100,
        window_units=2, last_advance=NOW,
    )

    reported = _progress.describe(row, (), now=NOW)

    assert reported.eta_seconds is None
    assert "no denominator" in reported.eta_absent


def test_a_keyspace_pass_refuses_an_eta_because_the_units_do_not_match():
    row = _row(
        denominator_kind="keyspace", keyspace_from=[0], ceiling=[1000], cursor=[250],
        window_started=_ago(10), window_rows=100, window_units=2, last_advance=NOW,
    )

    reported = _progress.describe(row, (), now=NOW)

    assert reported.percent == pytest.approx(25.0)
    assert reported.eta_seconds is None
    assert "would not be a time" in reported.eta_absent


def test_an_eta_is_remaining_over_the_trailing_rate():
    row = _row(
        denominator=1000, denominator_kind="exact", rows_done=500,
        window_started=_ago(10), window_rows=100, window_units=4, last_advance=NOW,
    )

    reported = _progress.describe(row, (), now=NOW)

    assert reported.rows_per_second == pytest.approx(10.0)
    assert reported.eta_seconds == pytest.approx(50.0)


# --- slow, stalled, blocked ---------------------------------------------------


def test_a_paced_pass_reports_slow_and_names_the_policy():
    row = _row(paced_reason="duty cycle 0.25", last_advance=_ago(1), driven_at=_ago(1))

    reported = _progress.describe(row, (), now=NOW)

    # A paced pass that does not report being paced is indistinguishable from a
    # broken one, which is the single most valuable field in the ledger.
    assert reported.state == "slow"
    assert "duty cycle 0.25" in reported.state_reason


def test_a_cursor_that_has_stopped_moving_is_stalled_not_slow():
    row = _row(
        paced_reason="duty cycle 0.25", driven_at=_ago(1),
        window_started=_ago(40), window_rows=10, window_units=10,
        last_advance=_ago(600),
    )

    reported = _progress.describe(row, (), now=NOW)

    assert reported.state == "stalled"
    assert "pg_stat_activity" in reported.state_reason


def test_the_stall_threshold_scales_with_the_passs_own_chunk_time():
    # Six minutes of window holding five chunks: 72 seconds a chunk, so silence
    # only counts as a stall after twelve minutes of it. An absolute threshold
    # would page somebody about a pass that is behaving exactly as declared.
    slow_chunks = _row(
        window_started=_ago(600), window_rows=10, window_units=5, last_advance=_ago(240)
    )
    assert _progress.stall_after(slow_chunks) == pytest.approx(720.0)

    # Chunks taking a tenth of a second: the floor applies instead, so a pass
    # that has only just started is not called stalled for pausing to breathe.
    fast_chunks = _row(
        window_started=_ago(1), window_rows=100, window_units=10, last_advance=NOW
    )
    assert _progress.stall_after(fast_chunks) == _progress.STALL_FLOOR_SECONDS


def test_a_pass_nothing_has_ever_driven_is_blocked():
    row = _row(driven_at=None)

    reported = _progress.describe(row, (), now=NOW)

    assert reported.state == "blocked"
    assert "nothing has ever tried to drive" in reported.state_reason


def test_a_pass_nothing_has_driven_lately_is_blocked_even_while_walking():
    # Everything else looks healthy. Nothing has enqueued a shift in ten
    # minutes, which means the scheduler is down and this will never finish.
    row = _row(driven_at=_ago(600), last_advance=_ago(1))

    reported = _progress.describe(row, (), now=NOW)

    assert reported.state == "blocked"
    assert "check the job runner" in reported.state_reason


def test_a_drive_failure_makes_the_pass_blocked_and_carries_the_reason():
    row = _row(last_drive_error="ConnectionRefusedError()", last_advance=_ago(1))

    reported = _progress.describe(row, (), now=NOW)

    assert reported.state == "blocked"
    assert "ConnectionRefusedError" in reported.state_reason


def test_a_finished_pass_is_done_and_wants_no_eta():
    row = _row(phase="done", denominator=10, denominator_kind="exact", rows_done=10)

    reported = _progress.describe(row, (), now=NOW)

    assert reported.state == "done"
    assert reported.eta_seconds is None
    assert reported.eta_absent is None


async def test_the_clock_the_states_are_judged_against_comes_from_the_database(
    database, world
):
    # Not from the reader's own clock. A host whose clock runs ten minutes fast
    # would otherwise invent a stall on a perfectly healthy pass -- and the
    # timestamps it is comparing against were all written by the database.
    walk = purge_pass()
    await walk.run(database, sleep=_nap)

    reads = walk.ledger
    connection = await database.acquire("write")
    row = await reads.read(connection)

    assert row.now == world.now
    assert any("clock_timestamp() AS now" in sql for sql in world.sql_of("SELECT"))
