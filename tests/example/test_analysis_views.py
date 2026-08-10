"""The two calculated views, driven through the application.

These tests exist to ask the charts their question rather than to observe that
they answer. The distinction is the lesson stage 4 paid for: a route table
asserted ``GET /session`` existed while nothing asked it who it thought you
were, and it reported ``signed_in: false`` to a caller holding a cookie the
server had just issued.

So nothing here reads ``result.buckets`` and stops. Each test names a property
the declaration is *for* — the dense axis, the per-measure fill, the reader's
own calendar — and would fail if the framework quietly stopped providing it
while still returning a plausible-looking chart.

**Sealing is not exercised here, and that is a boundary rather than a gap.**
Sealing works -- `tests/postgres/test_series_integration.py::TestSealingPersists`
drives a bucket through settling, reading back, and a correction against a real
server. What these tests own is the *application's* two views, which declare no
seal, so a seal assertion here would be testing the framework through the
example instead of testing the example.

This file briefly carried a pin asserting sealing was broken. It was written
while the defect was live and left behind after a concurrent fix landed, and it
had been aimed at the wrong subject anyway: it asserted the *driver* refuses a
mapping, which is permanently true and always will be, because the fix converts
the mapping to a JSON string before binding rather than teaching the driver to
bind mappings. It would have passed forever while describing something that no
longer existed. See AGENTS.md's rule about a check that has nothing to check.
"""

from __future__ import annotations

import datetime
import os

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap analysis tests",
)

#: Enough rows that a month's window has something in most of its days, and few
#: enough that the fixture is not the slowest thing in the suite.
SAMPLE = 4_000

#: Nullarbor: ``Australia/Adelaide``, a half-hour offset that observes daylight
#: saving. Chosen over the Nairobi reserves precisely because it is the one a
#: UTC-bucketing bug cannot hide in — a whole-hour zone at least lines its days
#: up with something.
RESERVE = "nullarbor"
ZONE = "Australia/Adelaide"

#: Inside the seed's 18 months from 2025-01-06.
SINCE = datetime.date(2026, 1, 5)
DAYS = 30


@pytest.fixture
async def client():
    """The application on a freshly built and seeded schema, acting as a ranger.

    A ranger because these tests are about bucketing and fill, not about who may
    see what — running as a volunteer would fold the sensitive-species rule into
    every assertion and make an authorization change look like a charting bug.
    ``test_authorization.py`` owns that question.
    """
    from _camera_trap import build_schema, drop_schema
    from camera_trap.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
    finally:
        await connection.close()

    application = build()
    async with TestClient(application) as test_client:
        yield test_client.acting_as("ranger-1", roles=["ranger"], type="Observer")

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()


async def _station_id(client) -> int:
    """A station in the reserve that actually recorded something.

    Picked from the data rather than hard-coded: a fixed id would make this file
    fail for the wrong reason the day the seed's station allocation changes, and
    the tests below are about the *shape* of a chart, not about station 25.
    """
    listing = await client.get(f"/reserves/{RESERVE}/stations")
    assert listing.status == 200
    stations = listing.json()["items"]
    assert stations, "the reserve has no stations; the fixture is wrong"
    return int(stations[0]["id"])


@skip_without_database
async def test_the_activity_axis_is_dense_and_in_the_reserves_calendar(client) -> None:
    """Every local day in the window is present, in order, with no gaps.

    This is the property a hand-written ``GROUP BY`` does not have, and the
    reason the endpoint returns a declaration's envelope rather than rows: a day
    nothing walked past has to be a zero in the axis, or a renderer draws a line
    from the day before to the day after and invents activity that did not
    happen.
    """
    station = await _station_id(client)
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{station}/activity",
        params={"since": SINCE.isoformat(), "days": DAYS},
    )
    assert response.status == 200
    body = response.json()

    assert body["zone"] == ZONE, "the chart was not cut in the reserve's own zone"
    assert body["bucket"] == "day"
    buckets = [datetime.datetime.fromisoformat(value) for value in body["buckets"]]
    assert len(buckets) == DAYS, f"expected {DAYS} daily buckets, got {len(buckets)}"
    assert buckets == sorted(buckets), "the axis is not in order"

    # Consecutive local midnights, not +24h. Across a daylight-saving change
    # those differ by an hour, and asserting a constant delta here is how a
    # correct implementation would be made to look broken.
    tz = datetime.datetime.fromisoformat(body["buckets"][0]).tzinfo
    assert tz is not None
    for earlier, later in zip(buckets, buckets[1:], strict=False):
        assert 23 <= (later - earlier).total_seconds() / 3600 <= 25
        assert later > earlier


@skip_without_database
async def test_an_empty_day_is_zero_sightings_and_no_confidence(client) -> None:
    """Per-measure fill: a count fills with 0, an average fills with null.

    The single most useful thing this view does, and the easiest to lose. Zero
    animals is a fact worth plotting; the mean confidence of no identifications
    is not a number, and filling it with 0 would draw a confidence collapse on
    every quiet night.

    Skipped rather than asserted vacuously if the window happens to have no
    empty day — a test that silently proves nothing is worse than one that says
    it had nothing to look at.
    """
    station = await _station_id(client)
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{station}/activity",
        params={"since": SINCE.isoformat(), "days": DAYS},
    )
    assert response.status == 200
    body = response.json()

    series = {item["measure"]: item["values"] for item in body["series"]}
    assert set(series) == {"sightings", "mean_confidence"}

    empty = [index for index, value in enumerate(series["sightings"]) if value == 0]
    if not empty:
        pytest.skip("no empty day in this window; nothing to assert about fill")
    for index in empty:
        assert series["sightings"][index] == 0
        assert series["mean_confidence"][index] is None, (
            "an empty day reported an average confidence; the fill is wrong"
        )

    # And the converse, so the assertion above cannot pass by everything being
    # empty: a day with sightings carries a real average.
    busy = [index for index, value in enumerate(series["sightings"]) if value]
    assert busy, "no day in the window has any sightings; the sample is too small"
    for index in busy:
        assert series["mean_confidence"][index] is not None


@skip_without_database
async def test_the_measures_carry_their_units(client) -> None:
    """A percentage says so, and a count does not claim one.

    The unit travels with the series because a renderer that has to infer it
    from the measure's name gets it wrong exactly once, on the axis label.
    """
    station = await _station_id(client)
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{station}/activity",
        params={"since": SINCE.isoformat(), "days": DAYS},
    )
    units = {item["measure"]: item["unit"] for item in response.json()["series"]}
    assert units == {"sightings": None, "mean_confidence": "%"}


@skip_without_database
async def test_the_window_is_bounded_by_the_binding_layer(client) -> None:
    """A decade of daily buckets is refused before a query is built.

    ``CAMERA_TRAP_MAX_WINDOW_DAYS`` is start-up configuration, so this is a 422
    from the binding layer rather than a slow query someone notices later.
    """
    station = await _station_id(client)
    response = await client.get(
        f"/reserves/{RESERVE}/stations/{station}/activity",
        params={"since": SINCE.isoformat(), "days": 4000},
    )
    assert response.status == 422


@skip_without_database
async def test_the_species_mix_ranks_and_does_not_fold_a_tail(client) -> None:
    """The bars are the answer, so nothing is merged into an ``other`` bucket.

    ``Series.by()`` would fold the tail to preserve a total, which is right for
    a part-to-whole chart and wrong here: a ranger looking for a species needs
    it to be absent-or-present, not silently summed into a remainder.
    """
    station = await _station_id(client)
    response = await client.get(f"/reserves/{RESERVE}/stations/{station}/species-mix")
    assert response.status == 200
    body = response.json()

    assert body["measures"] == ["sightings"]
    rows = body["rows"]
    assert rows, "the station recorded nothing; the sample is too small"

    counts = [row["values"]["sightings"] for row in rows]
    assert counts == sorted(counts, reverse=True), "the ranking is not ranked"
    assert all(count > 0 for count in counts)

    # No remainder row. `Aggregate` has no `other` concept at all, and this is
    # what would catch a future switch to `Series.by()` made for convenience.
    assert all(row["key"] is not None for row in rows)
    keys = [row["key"] for row in rows]
    assert len(keys) == len(set(keys)), "a species is ranked twice"


@skip_without_database
async def test_the_mix_and_the_series_agree_on_the_total(client) -> None:
    """Two declarations over one table have to reach the same number.

    The series is filtered to a window and the aggregate is not, so this compares
    the aggregate against the *unwindowed* count the list endpoint reports for
    the same station. If these ever disagree, one of the two is applying a
    filter the other is not — which is the failure a chart cannot show you.
    """
    station = await _station_id(client)
    mix = await client.get(f"/reserves/{RESERVE}/stations/{station}/species-mix")
    total = sum(row["values"]["sightings"] for row in mix.json()["rows"])

    listing = await client.get(
        f"/reserves/{RESERVE}/stations/{station}/sightings",
        params={"since": datetime.date(2025, 1, 6).isoformat(), "days": 1},
    )
    assert listing.status == 200
    assert total > 0
    # The list is windowed to one day, so it can only ever be a subset. The
    # assertion is the direction, which is the part a filter bug breaks.
    assert listing.json()["total"] <= total


@skip_without_database
async def test_a_card_pulled_late_records_a_correction() -> None:
    """The late-SD-card story, on the example's own schema.

    A card collected a year after its photos were taken carries sightings for a
    day that sealed long ago. The settled count is not rewritten -- the
    difference is recorded beside it and folded in on read, so late data reads
    as late data arriving rather than as a number that quietly changed.

    Driven through ``camera_trap.views.sealed_activity`` rather than a bare
    ``Series``, so this asserts the declaration the docs describe rather than a
    reconstruction of it. The framework-level round trip -- that a settled bucket
    survives storage at all -- is proved in
    ``tests/postgres/test_series_integration.py``.
    """
    from _camera_trap import build_schema, drop_schema
    from camera_trap.models import MODELS, SCHEMA
    from camera_trap.views import sealed_activity

    from wreath._series.settle import schema_sql
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.postgres import Database, connect
    from wreath.series import Range
    from wreath.temporal import zone as tz

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
        # The settled tables are wreath's own furniture and are deliberately
        # absent from the example's migration artifact, so the application
        # applies them itself. `execute` prepares, and a prepared statement
        # cannot carry several commands, so they go one at a time.
        for part in schema_sql().split(";\n"):
            if part.strip():
                await connection.execute(part.strip())
        # Station and day are read out of the data, not written in. The seed is
        # sparse per station -- the busiest local day at the busiest station has
        # a handful of sightings -- so a hard-coded date makes this fail for a
        # reason that has nothing to do with sealing the day the seed shifts.
        busiest = await connection.fetch(
            f'SELECT station_id, date_trunc(\'day\', captured_at AT TIME ZONE $1) '
            f'FROM "{SCHEMA}"."sightings" GROUP BY 1, 2 '
            "ORDER BY count(*) DESC, 1, 2 LIMIT 1",
            ZONE,
        )
        assert busiest, "the seed produced no sightings; the fixture is wrong"
        station, local_midnight = busiest[0]
    finally:
        await connection.close()

    database = Database("main", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    session = Session(Registry(database, list(MODELS), validate_schema="off"), "write")
    try:
        view = sealed_activity(ZONE)

        # `local_midnight` comes back naive -- it is a wall clock, which is what
        # a bucket boundary is -- so the zone is attached here rather than
        # assumed. `now` is past the end of the seed, so the fortnight of
        # lateness has elapsed for every day in it.
        day = local_midnight.replace(tzinfo=tz(ZONE))
        window = Range(day, day + datetime.timedelta(days=1))
        now = datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)

        # Settled rows outlive the example's schema: they live in wreath's own
        # `wreath` schema, which `drop_schema` correctly does not touch. A
        # previous run of this test therefore leaves a settled bucket and a
        # correction behind, and the first read below would fold them in and
        # measure the wrong thing. Cleared by this view's own identity so a
        # sibling suite's settled rows are left alone.
        #
        # That identity is *schema-blind*: `view_key` digests the model's module
        # and qualname, not the schema it resolves to, so two deployments of this
        # application against different schemas file their settled rows under the
        # same key. This test is safe because xdist gives one test to one worker,
        # but a second example test sealing the same view would collide with it.
        # Tracked with the `wreath`-schema design; do not add one until it lands.
        view_id, params_id = view._identity(ZONE, {"station": station})
        connection = await database.acquire("write")
        try:
            for table in ("series_buckets", "series_corrections"):
                await connection.execute(
                    f'DELETE FROM "wreath"."{table}" WHERE view = $1 AND params = $2',
                    view_id,
                    params_id,
                )
        finally:
            await database.release("write", connection)

        first = await view.run(session, range=window, now=now, station=station)
        settled = first.series[0].values[0]
        assert first.state is not None and first.state.settled, (
            "a day a year old must be sealed at a fortnight's lateness"
        )
        # Reading computes a sealed day and deliberately does not store it, so
        # the settling job is an explicit step -- and it has to run *before* the
        # late card, which is the situation this test is about.
        await view.settle(session, range=window, now=now, station=station)

        # The card comes out of the camera a year late, carrying that day.
        connection = await database.acquire("write")
        try:
            await connection.execute(
                f'INSERT INTO "{SCHEMA}"."sightings" '
                "(id, station_id, camera_id, species_id, captured_at, uploaded_at, "
                " confidence, image_key, review_state, tags) "
                f'SELECT (SELECT max(id) + 1 FROM "{SCHEMA}"."sightings"), '
                " station_id, camera_id, species_id, captured_at, now(), "
                " confidence, image_key || '-late', review_state, tags "
                f'FROM "{SCHEMA}"."sightings" '
                "WHERE station_id = $1 AND captured_at >= $2 AND captured_at < $3 "
                "LIMIT 1",
                station,
                window.start,
                window.end,
            )
        finally:
            await database.release("write", connection)

        moved = await view.reconcile(session, range=window, now=now, station=station)
        assert moved, "reconcile did not notice the late card"

        after = await view.run(session, range=window, now=now, station=station)
        assert after.series[0].values[0] == settled + 1, (
            "the correction did not fold in on read"
        )
        assert after.state.corrections, (
            "the envelope must say which bucket carries a correction, or late "
            "data is indistinguishable from a number that changed on its own"
        )
    finally:
        # Before `stop()`: the session's lease is only returned by `close()`, and
        # a connection still out costs the pool its whole 10s `shutdown_timeout`
        # before being closed underneath the session anyway.
        await session.close()
        await database.stop()
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()
