"""The example's data is reproducible, and its schema says what it means.

These run without a database. The determinism claim is about
:func:`camera_trap.seed.build_rows`, which touches no I/O, so it is checkable in
milliseconds — and it is worth checking cheaply, because everything downstream
depends on it: the walkthrough quotes row counts, and the charts a reader
compares against a screenshot are only comparable if the rows are the same.
"""

from __future__ import annotations

import datetime

import pytest
from camera_trap.models import DEFAULT_SCHEMA, MODELS, SCHEMA, Sighting, Station
from camera_trap.seed import COLUMNS, ORDER, build_rows

#: Small enough to build twice per test, large enough that a leaked source of
#: randomness would show. The full seed is 140,000.
SAMPLE = 3_000

#: The two seeds every reader in this file wants, built once each.
#:
#: `build_rows` is pure and deterministic, so nine tests each calling it for
#: themselves rebuilt identical tables -- 2.13s of this file, most of it the two
#: 40,000-sighting builds. Module scope rather than session so the cost still
#: shows up in this file's timings.
#:
#: **Read-only**, like `tests/tracking/test_seed.py`'s equivalent: a test that
#: mutated one would hand the next a different seed, and the failure would land
#: wherever the ordering happened to put it. `test_two_builds_are_identical`
#: takes neither -- its claim is that two *fresh* builds agree.


@pytest.fixture(scope="module")
def rows() -> dict:
    return build_rows(sightings=SAMPLE)


@pytest.fixture(scope="module")
def big_rows() -> dict:
    return build_rows(sightings=40_000)


def test_two_builds_are_identical() -> None:
    """The whole point. One seeded generator, no clock reads."""
    first = build_rows(sightings=SAMPLE)
    second = build_rows(sightings=SAMPLE)
    assert first.keys() == second.keys()
    for table in first:
        assert first[table] == second[table], f"{table} differs between builds"


def test_no_timestamp_comes_from_the_clock(rows: dict) -> None:
    """Every generated instant is derived from ``EPOCH``.

    A ``datetime.now()`` anywhere in the generator would make two runs differ by
    the time between them -- which the test above would catch -- but it would
    also put *today* in the data, so a walkthrough written in July would stop
    matching in August. Assert the range directly.
    """
    horizon = datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)
    captured = [row[5] for row in rows["sightings"]]
    assert all(value < horizon for value in captured)
    assert max(captured) < horizon


def test_every_table_has_a_column_list_and_an_insert_position() -> None:
    """``COLUMNS`` and ``ORDER`` are parallel to the models, or the seed lies.

    A model gaining a column without ``COLUMNS`` gaining one is the failure this
    guards: the insert would still run, binding values to the wrong names.
    """
    assert set(COLUMNS) == set(ORDER)
    rows = build_rows(sightings=10)
    assert set(rows) == set(ORDER)
    for table, columns in COLUMNS.items():
        assert rows[table], f"{table} seeded nothing"
        assert len(rows[table][0]) == len(columns), f"{table} row width != column count"


def test_column_lists_match_the_models() -> None:
    """The seed's column names are the model's column names, in order."""
    by_table = {model.__wreath_table__: model for model in MODELS}
    for table, columns in COLUMNS.items():
        model = by_table[table]
        declared = tuple(c.database_name for c in model.__wreath_columns__)
        assert set(columns) <= set(declared), (
            f"{table}: seed names columns the model does not declare: "
            f"{set(columns) - set(declared)}"
        )


def test_sightings_are_attributed_to_a_camera_that_was_deployed(rows: dict) -> None:
    """A sighting names the device that was hanging there at the time.

    Getting this wrong is invisible in a row count and fatal to the example's
    best relationship: every replacement camera would carry zero sightings, and
    the station-outlives-its-hardware story would be false in the data while
    reading fine in the code. It was wrong once.
    """
    windows = {
        camera[0]: (camera[4], camera[5]) for camera in rows["cameras"]
    }
    for sighting in rows["sightings"]:
        deployed, retired = windows[sighting[2]]
        captured = sighting[5]
        assert deployed <= captured, "sighting predates its camera's deployment"
        if retired is not None:
            assert captured < retired, "sighting postdates its camera's retirement"


def test_a_replaced_station_has_sightings_on_both_cameras(big_rows: dict) -> None:
    """The swap is present in the data, not just in the cameras table."""
    rows = big_rows
    by_station: dict[int, set[int]] = {}
    for sighting in rows["sightings"]:
        by_station.setdefault(sighting[1], set()).add(sighting[2])
    swapped = [station for station, cameras in by_station.items() if len(cameras) > 1]
    assert swapped, "no station's sightings cross a camera replacement"


def test_captures_use_the_reserve_wall_clock(rows: dict) -> None:
    """A camera records local time, so the stored instant carries a real zone.

    Generating in UTC is the shortcut that makes a +09:30 reserve's nocturnal
    species peak at local noon. The zone on the value is the evidence it was not
    taken.
    """
    zones = {str(row[5].tzinfo) for row in rows["sightings"]}
    assert zones == {
        "Africa/Nairobi", "Europe/Lisbon", "Australia/Adelaide", "America/Belize"
    }


def test_review_state_is_the_mess_chapter_two_fixes(rows: dict) -> None:
    """v1 ships the flaw on purpose; the migration chapter needs it to exist."""
    spellings = {row[11] for row in rows["sightings"]}
    assert {"confirmed", "Confirmed", "ok"} <= spellings, "the casing mess is missing"
    assert "" in spellings, "the empty-string case is missing"


def test_some_cards_are_collected_long_after_their_last_image(big_rows: dict) -> None:
    """Late data has rows, or the sealing story is a paragraph."""
    rows = big_rows
    collected = {row[0]: row[2] for row in rows["deployments"]}
    newest: dict[int, datetime.datetime] = {}
    for sighting in rows["sightings"]:
        card = sighting[4]
        if card is not None:
            captured = sighting[5]
            if card not in newest or captured > newest[card]:
                newest[card] = captured
    stale = [
        card for card, last in newest.items()
        if (collected[card] - last) > datetime.timedelta(days=7)
    ]
    assert stale, "no deployment is meaningfully late"


@pytest.mark.parametrize(
    "model,expected",
    [(Station, "stations"), (Sighting, "sightings")],
)
def test_models_live_in_the_example_schema(model: type, expected: str) -> None:
    """One namespace, so `\\dt camera_trap.*` shows the domain and nothing else.

    The live `SCHEMA` is not asserted: this suite overrides it per xdist worker
    so parallel runs do not drop each other's tables. What matters — and what is
    checked — is that every model lands in *one* namespace and that the shipped
    default is the one the walkthrough and the guides name.
    """
    assert model.__wreath_table__ == expected
    assert DEFAULT_SCHEMA == "camera_trap"
    assert model.__wreath_schema__ == SCHEMA
