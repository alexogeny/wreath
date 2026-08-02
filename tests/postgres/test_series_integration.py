"""Live-PostgreSQL checks for the two temporal claims a fake cannot settle.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database. The
fake-driver suite in ``tests/series/`` proves the statement's shape and the
envelope's rules; these prove the things only a real ``date_trunc`` and a real
``generate_series`` can:

* **Python's bucket arithmetic agrees with PostgreSQL's.** ``Bucket.floor`` is
  documented as the mirror of ``date_trunc(unit, t AT TIME ZONE zone)``, and
  until this runs that is a claim reasoned from the documentation rather than a
  measured fact. Sealing will depend on the two agreeing, so a drift here is a
  bucket that settles at the wrong moment.
* **The spine steps a calendar day, not 86400 seconds.** This is the DST bug the
  whole ordering in the design exists to prevent, and it manifests twice a year,
  in one bucket, in a way nobody traces back to the chart.
* **A sealed bucket survives the round trip to storage.** `TestSealingPersists`
  drives a declaration through `run` and `reconcile` against the server. Until it
  existed, two files both *looked* like sealing coverage and the persistence path
  fell between them: `tests/series/test_sealing.py` proves the arithmetic against
  a fake, and the check above proves the *DDL applies* -- so a settled write the
  driver refused outright passed every test in the repository, for as long as
  sealing had existed. Anything that changes how a measure is bound belongs here
  rather than in the fake suite, because the fake is the thing that failed to
  notice.

The first two are asserted against a zone with a large offset and a
southern-hemisphere transition (Auckland), because a bug that cancels out in UTC
or in Europe shows up there.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest

from wreath._series.settle import (
    BUCKET_TABLE,
    CORRECTION_TABLE,
    SCHEMA,
    schema_sql,
    watermark,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Float64, Int64, Text, TimestampTz
from wreath.postgres import Database
from wreath.queries import Param
from wreath.series import Range, Series, count, sum_
from wreath.temporal import Day, Hour, Instant, Month, Week, from_wall_clock, zone

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live calculated-view integration tests",
)

AUCKLAND = "Pacific/Auckland"


def local(day: str, hour: int = 0) -> datetime.datetime:
    """A wall-clock time in Auckland, as an instant.

    The offset is read from the zone rather than written into the literal.
    Auckland is +13 in March and +12 in June, so a hardcoded offset makes a
    test that quietly measures the day either side of the one it names -- which
    is exactly what three of these tests did before they were first run.
    """
    naive = datetime.datetime.fromisoformat(f"{day}T00:00:00").replace(hour=hour)
    return from_wall_clock(naive, zone(AUCKLAND))


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database(
        "main",
        dsn,
        pools={
            "read": {"min_size": 1, "max_size": 2},
            "write": {"min_size": 1, "max_size": 2},
        },
    )
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


async def fetchval(database, sql, *args):
    connection = await database.acquire("read")
    try:
        return await connection.fetchval(sql, *args)
    finally:
        await database.release("read", connection)


#: Moments chosen to straddle both Auckland transitions in 2026 -- DST ends on
#: 5 April (the 25-hour day) and begins on 27 September (the 23-hour day) --
#: plus ordinary days either side, so a rule that only holds away from a
#: boundary fails here rather than in production.
MOMENTS = [
    datetime.datetime(2026, 4, 4, 12, tzinfo=datetime.UTC),
    datetime.datetime(2026, 4, 4, 13, tzinfo=datetime.UTC),   # 2026-04-05 02:00 NZDT
    datetime.datetime(2026, 4, 4, 14, tzinfo=datetime.UTC),   # after the clock went back
    datetime.datetime(2026, 4, 5, 6, tzinfo=datetime.UTC),
    datetime.datetime(2026, 9, 26, 13, tzinfo=datetime.UTC),
    datetime.datetime(2026, 9, 26, 14, tzinfo=datetime.UTC),  # into the gap
    datetime.datetime(2026, 9, 27, 6, tzinfo=datetime.UTC),
    datetime.datetime(2026, 6, 15, 9, tzinfo=datetime.UTC),
]


class TestFloorMatchesDateTrunc:
    """The claim ``Bucket.floor`` makes about itself, checked against the source."""

    @pytest.mark.parametrize("unit", [Hour, Day, Week, Month])
    @pytest.mark.parametrize("moment", MOMENTS)
    async def test_python_and_postgres_agree_on_the_boundary(
        self, database, unit, moment
    ):
        # `date_trunc` on the wall clock, converted back the same way the spine
        # converts -- so this compares the whole round trip, not just the
        # truncation.
        theirs = await fetchval(
            database,
            f"SELECT date_trunc('{unit.trunc}', $1::timestamptz AT TIME ZONE $2) "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        ours = unit.floor(moment, zone(AUCKLAND))
        assert theirs == ours.astimezone(datetime.UTC), (
            f"{unit.name} boundary for {moment.isoformat()} disagrees"
        )

    async def test_end_of_matches_one_step_of_generate_series(self, database):
        moment = datetime.datetime(2026, 4, 5, 6, tzinfo=datetime.UTC)
        theirs = await fetchval(
            database,
            "SELECT (date_trunc('day', $1::timestamptz AT TIME ZONE $2) "
            "+ interval '1 day') AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        assert theirs == Day.end_of(moment, zone(AUCKLAND)).astimezone(datetime.UTC)


class TestTheSpineStepsACalendarDay:
    async def test_a_dst_day_is_twenty_five_hours_of_real_time(self, database):
        """Auckland leaves daylight saving on 2026-04-05: that day runs 25 hours.

        Generated over naive local timestamps and converted back, consecutive
        buckets are 25 hours apart in real time while remaining one calendar day
        apart on the wall clock. Generated over ``timestamptz`` they would be
        exactly 24 hours apart, which is the bug.
        """
        rows = await _spine(database, "2026-04-04", "2026-04-07", "day")
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(rows, rows[1:], strict=False)]
        assert 25 in gaps, f"expected a 25-hour day among {gaps}"

    async def test_the_other_transition_is_twenty_three_hours(self, database):
        rows = await _spine(database, "2026-09-26", "2026-09-29", "day")
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(rows, rows[1:], strict=False)]
        assert 23 in gaps, f"expected a 23-hour day among {gaps}"

    async def test_every_bucket_is_local_midnight(self, database):
        rows = await _spine(database, "2026-04-04", "2026-04-07", "day")
        local = [item.astimezone(zone(AUCKLAND)) for item in rows]
        assert {(item.hour, item.minute) for item in local} == {(0, 0)}

    async def test_the_upper_bound_is_exclusive(self, database):
        """A range ending exactly on a boundary stops at the previous bucket."""
        inclusive = await _spine(database, "2026-06-01", "2026-06-04", "day")
        assert len(inclusive) == 3, "1st, 2nd and 3rd -- not the 4th"


async def _spine(database, start: str, end: str, unit: str) -> list:
    """The spine exactly as ``_series.compile`` renders it."""
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            f"SELECT generate_series("
            f"date_trunc('{unit}', $1::timestamptz AT TIME ZONE $3), "
            f"date_trunc('{unit}', ($2::timestamptz AT TIME ZONE $3) "
            f"- interval '1 microsecond'), "
            f"interval '1 {unit}') AT TIME ZONE $3 AS b",
            local(start),
            local(end),
            AUCKLAND,
        )
        return [row[0] for row in rows]
    finally:
        await database.release("read", connection)


class TestTheComparisonShift:
    """`compare(previous=...)` shifts the local bounds, and only a real server
    can confirm what `interval '1 month'` does to a naive local timestamp."""

    async def test_a_month_shift_is_calendar_arithmetic_not_thirty_days(
        self, database
    ):
        """"The same day last month" has to land on the same day number.

        Subtracting a fixed number of days walks backwards through the calendar;
        `interval '1 month'` on a naive local timestamp does not.
        """
        # Noon *in Auckland* on 31 March. Written as a UTC literal this was
        # 2026-03-31T12:00Z, which is 1 April locally -- so the test shifted
        # April back to March and read a pass as a failure.
        moment = local("2026-03-31", hour=12)
        shifted = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) - interval '1 month') "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        there = shifted.astimezone(zone(AUCKLAND))
        assert (there.month, there.day) == (2, 28), "clamped to February's last day"

    async def test_the_shift_preserves_the_wall_clock_across_a_transition(
        self, database
    ):
        """Shifting a local bound and converting back keeps the wall time and
        moves the instant, which is what makes a comparison period comparable.

        The other order — shifting the instant — keeps the instant's spacing and
        moves the wall time by an hour, so every bucket after a transition is
        compared against the wrong one.
        """
        # 2026-04-20 is after Auckland's April transition; one month earlier is
        # before it, so the offset differs between the two.
        moment = datetime.datetime(2026, 4, 19, 12, tzinfo=datetime.UTC)
        shifted = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) - interval '1 month') "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        here = moment.astimezone(zone(AUCKLAND))
        there = shifted.astimezone(zone(AUCKLAND))
        assert (there.hour, there.minute) == (here.hour, here.minute)
        assert there.utcoffset() != here.utcoffset(), "the offset really did change"

    async def test_the_two_arms_may_be_different_lengths(self, database):
        """March against February is 31 buckets against 28.

        The envelope gives each period its own bucket run precisely because
        this is true; a shared run would have to invent three buckets.
        """
        current = await _spine(database, "2026-03-01", "2026-04-01", "day")
        previous = await _shifted_spine(database, "2026-03-01", "2026-04-01", "day")
        assert len(current) == 31
        assert len(previous) == 28


class TestTheMarkerBucket:
    async def test_a_marker_lands_in_the_bucket_that_contains_it(self, database):
        """The bucket travels with the event, computed by the same `date_trunc`
        in the same zone, so a marker cannot sit a column away from the bar it
        describes."""
        # 13:00 UTC on 4 April 2026 is 02:00 on the 5th in Auckland -- inside
        # the ambiguous hour, and on the far side of local midnight from UTC.
        moment = datetime.datetime(2026, 4, 4, 13, tzinfo=datetime.UTC)
        bucket = await fetchval(
            database,
            "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        local = bucket.astimezone(zone(AUCKLAND))
        assert (local.month, local.day, local.hour) == (4, 5, 0)
        assert bucket <= moment, "a marker never precedes its own bucket"


async def _shifted_spine(database, start: str, end: str, unit: str) -> list:
    """The comparison arm exactly as ``_series.compile`` renders it."""
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            f"SELECT generate_series("
            f"date_trunc('{unit}', (($1::timestamptz AT TIME ZONE $3) "
            f"- interval '1 month')), "
            f"date_trunc('{unit}', ((($2::timestamptz AT TIME ZONE $3) "
            f"- interval '1 microsecond') - interval '1 month')), "
            f"interval '1 {unit}') AT TIME ZONE $3 AS b",
            local(start),
            local(end),
            AUCKLAND,
        )
        return [row[0] for row in rows]
    finally:
        await database.release("read", connection)


# -- stage 7: sealing ---------------------------------------------------------


class TestSealedBucketBoundaries:
    """What only a real server can settle about a settled bucket.

    The arithmetic in `_series.settle` decides *which* buckets are sealed; these
    check that the boundary it picks is the boundary PostgreSQL would pick, on
    the two days a year the answer is interesting.
    """

    async def test_the_watermark_lands_on_a_boundary_date_trunc_agrees_with(
        self, database
    ):
        """A settled bucket start and a freshly computed one must be one instant.

        If they disagree even once, a settled row files itself under a bucket
        the spine will never generate, and the value silently disappears from
        every later read.
        """
        for moment in (
            datetime.datetime(2026, 4, 5, 1, 30, tzinfo=datetime.UTC),
            datetime.datetime(2026, 9, 27, 1, 30, tzinfo=datetime.UTC),
        ):
            theirs = await fetchval(
                database,
                "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) "
                "AT TIME ZONE $2",
                moment,
                AUCKLAND,
            )
            ours = watermark(
                Instant.of(moment), bucket=Day, zone_name=AUCKLAND, after=0
            )
            assert ours == theirs, f"python and postgres disagree at {moment}"

    async def test_the_lateness_allowance_is_elapsed_time(self, database):
        """Two hours after a 23-hour day still means two hours.

        The allowance is subtracted as a fixed offset and only the *bucket*
        boundary is calendar arithmetic. Checking against the server keeps that
        split honest across the spring-forward day.
        """
        moment = datetime.datetime(2026, 9, 27, 14, tzinfo=datetime.UTC)
        theirs = await fetchval(
            database,
            "SELECT date_trunc('day', ($1::timestamptz - interval '2 hours') "
            "AT TIME ZONE $2) AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        ours = watermark(
            Instant.of(moment), bucket=Day, zone_name=AUCKLAND, after=7200
        )
        assert ours == theirs

    async def test_the_gap_step_lands_on_the_next_bucket_across_a_short_day(
        self, database
    ):
        """Stepping past the last settled bucket is `end_of`, not plus 24 hours.

        On a 23-hour day, adding a nominal day would start the gap an hour into
        a bucket that is already stored — recomputing part of a settled value
        and leaving a real gap unfilled.
        """
        start = await fetchval(
            database,
            "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) AT TIME ZONE $2",
            datetime.datetime(2026, 9, 27, 6, tzinfo=datetime.UTC),
            AUCKLAND,
        )
        theirs = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) + interval '1 day') "
            "AT TIME ZONE $2",
            start,
            AUCKLAND,
        )
        assert Day.end_of(Instant.of(start), AUCKLAND) == theirs

    async def test_the_settled_tables_apply_cleanly(self, database):
        """The DDL a migration would carry, against a real server -- twice.

        Applied statement by statement because `execute` prepares, and a
        prepared statement cannot carry several commands. That is the same
        splitting `tests/postgres/test_passes_integration.py` does; every
        caller of a `schema_sql()` currently has to know it.
        """
        for _ in range(2):
            connection = await database.acquire("write")
            try:
                for part in schema_sql(schema="wreath_series_test").split(";\n"):
                    if part.strip():
                        await connection.execute(part.strip())
            finally:
                await database.release("write", connection)


class TestRollupAgreesWithTheDatabase:
    """Stage 8: a coarser tier has to hold the number a coarse query would.

    The fake suite proves which statements run and which tier answers where.
    What it cannot prove is that a month materialised from source rows equals
    ``date_trunc('month', ...)`` over the same rows -- and if those ever
    disagree, the rollup is a confidently wrong chart rather than a slow one.
    """

    async def test_a_month_bucket_start_agrees_with_date_trunc(self, database):
        """The coarse grain files under the boundary the spine will generate.

        Same failure mode as the sealing check one grain up: a monthly row
        stored at an instant ``generate_series`` never emits is a value that
        silently vanishes from every read.
        """
        for moment in (
            datetime.datetime(2026, 4, 5, 1, 30, tzinfo=datetime.UTC),
            datetime.datetime(2026, 9, 27, 1, 30, tzinfo=datetime.UTC),
        ):
            theirs = await fetchval(
                database,
                "SELECT date_trunc('month', $1::timestamptz AT TIME ZONE $2) "
                "AT TIME ZONE $2",
                moment,
                AUCKLAND,
            )
            assert Month.floor(Instant.of(moment), AUCKLAND) == theirs

    async def test_a_month_of_days_sums_to_the_month_bucket(self, database):
        """Additivity, against the server rather than against the rule.

        ``count`` and ``sum`` are declared rollup-safe. This is that claim
        measured: aggregating a month directly equals aggregating its days and
        adding them, across a month containing a DST transition.
        """
        rows = await _fetch(
            database,
            """
            WITH samples AS (
              SELECT generate_series(
                timestamptz '2026-09-01 00:00+12',
                timestamptz '2026-09-30 00:00+12',
                interval '7 hours'
              ) AS at
            ),
            by_day AS (
              SELECT date_trunc('day', at AT TIME ZONE $1) AS bucket, count(*) AS n
              FROM samples GROUP BY 1
            ),
            by_month AS (
              SELECT date_trunc('month', at AT TIME ZONE $1) AS bucket, count(*) AS n
              FROM samples GROUP BY 1
            )
            SELECT (SELECT sum(n) FROM by_day), (SELECT sum(n) FROM by_month)
            """,
            AUCKLAND,
        )
        summed_days, direct_month = rows[0]
        assert summed_days == direct_month

    async def test_an_average_of_averages_really_is_wrong(self, database):
        """The refusal exists for a reason; this is the reason, measured.

        If these ever came out equal the additivity check would be needless
        ceremony. They do not: a quiet bucket and a busy one weigh the same once
        you average their averages, and the error is small enough to look
        plausible on a chart.
        """
        rows = await _fetch(
            database,
            """
            WITH samples(bucket, value) AS (
              VALUES ('a', 1.0), ('a', 1.0), ('a', 1.0), ('a', 1.0), ('b', 100.0)
            ),
            per_bucket AS (
              SELECT bucket, avg(value) AS mean FROM samples GROUP BY bucket
            )
            SELECT (SELECT avg(value) FROM samples)::float8,
                   (SELECT avg(mean) FROM per_bucket)::float8
            """,
        )
        honest, average_of_averages = rows[0]
        assert honest != average_of_averages
        assert abs(honest - average_of_averages) > 20


async def _fetch(database, sql: str, *args):
    connection = await database.acquire("read")
    try:
        return await connection.fetch(sql, *args)
    finally:
        await database.release("read", connection)


# -- sealing, end to end against the server -----------------------------------

#: Each xdist worker owns a source schema, so the rows one test aggregates are
#: never another's. The settled tables cannot be separated the same way --
#: `_series.settle` hard-codes the `wreath` schema and nothing threads an
#: override through `run` -- so isolation there comes from `params` instead, via
#: a `Param` bound to the worker name. That is `params`' actual job: one settled
#: value per declaration, per set of bound parameters, per bucket.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")
SEAL_SCHEMA = f"wreath_seal_{WORKER}"


class SealTrek(Model, table="treks", schema=SEAL_SCHEMA):
    """A minimal source table: one timestamp to bucket by, one number to sum."""

    id: Mapped[int] = column(Int64, primary_key=True)
    grade: Mapped[str] = column(Text)
    distance_km: Mapped[float] = column(Float64)
    started_at: Mapped[object] = column(TimestampTz)


def sealed_view() -> Series:
    """One declaration, shared by every test here so they agree on `view_key`.

    Deliberately no `avg()`: `avg` over an integer column is `numeric`, which
    the driver decodes to `Decimal`, which `json.dumps` cannot serialise -- so a
    settled average would fail on the *write* for a reason that has nothing to do
    with sealing. That is a real and separate defect; `count` and `sum` keep this
    test pointed at the thing it is here to prove.
    """
    return (
        Series(SealTrek, at=SealTrek.started_at, bucket=Day, stored_in=zone("UTC"))
        .where(SealTrek.grade == Param("grade"))
        .measure(treks=count(), distance=sum_(SealTrek.distance_km))
        .seal(after="2h")
    )


#: The bucket every test settles, and a `now` well past its watermark.
SEAL_DAY = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
SEAL_RANGE = Range(SEAL_DAY, datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
WELL_AFTER = datetime.datetime(2026, 3, 4, tzinfo=datetime.UTC)


async def _execute(database, sql: str, *args) -> None:
    connection = await database.acquire("write")
    try:
        await connection.execute(sql, *args)
    finally:
        await database.release("write", connection)


async def _settled_rows(database) -> list:
    return await _fetch(
        database,
        f'SELECT bucket, measures FROM "{SCHEMA}"."{BUCKET_TABLE}" '
        "WHERE params = $1 ORDER BY bucket",
        _params_key(),
    )


async def _correction_rows(database) -> list:
    return await _fetch(
        database,
        f'SELECT bucket, delta FROM "{SCHEMA}"."{CORRECTION_TABLE}" '
        "WHERE params = $1 ORDER BY bucket",
        _params_key(),
    )


def _params_key() -> str:
    """The `params` column this worker's rows are filed under.

    Derived through the shipped helper rather than restated, so a change to how
    parameters are keyed moves the assertions with it instead of leaving them
    reading a column nothing writes.
    """
    from wreath._series.settle import params_key

    return params_key({"grade": WORKER})


@pytest.fixture
async def sealing(database):
    """A source table for this worker, the settled tables, and a clean slate.

    The settled tables are created under an advisory lock: `CREATE SCHEMA IF NOT
    EXISTS` is not atomic against a concurrent creator, and PostgreSQL reports
    the race as a `pg_namespace_nspname_index` unique violation, which reads like
    anything except a test-isolation bug.
    """
    await _execute(database, f'DROP SCHEMA IF EXISTS "{SEAL_SCHEMA}" CASCADE')
    await _execute(database, f'CREATE SCHEMA "{SEAL_SCHEMA}"')
    await _execute(
        database,
        f'CREATE TABLE "{SEAL_SCHEMA}"."treks" ('
        "  id bigint PRIMARY KEY,"
        "  grade text NOT NULL,"
        "  distance_km double precision NOT NULL,"
        "  started_at timestamptz NOT NULL)",
    )

    connection = await database.acquire("write")
    try:
        await connection.execute("SELECT pg_advisory_lock(8_675_309)")
        try:
            for part in schema_sql().split(";\n"):
                if part.strip():
                    await connection.execute(part.strip())
        finally:
            await connection.execute("SELECT pg_advisory_unlock(8_675_309)")
    finally:
        await database.release("write", connection)

    # A killed run leaves settled rows behind, and a settled row is exactly the
    # thing these tests are trying to observe appearing.
    for table in (BUCKET_TABLE, CORRECTION_TABLE):
        await _execute(
            database,
            f'DELETE FROM "{SCHEMA}"."{table}" WHERE params = $1',
            _params_key(),
        )

    registry = Registry(database, [SealTrek], validate_schema="off")
    yield database, Session(registry, "write")

    await _execute(database, f'DROP SCHEMA IF EXISTS "{SEAL_SCHEMA}" CASCADE')
    for table in (BUCKET_TABLE, CORRECTION_TABLE):
        await _execute(
            database,
            f'DELETE FROM "{SCHEMA}"."{table}" WHERE params = $1',
            _params_key(),
        )


async def _add_treks(database, *rows: tuple[int, float, int]) -> None:
    """Insert `(id, distance_km, hour)` treks into this worker's source table."""
    for identifier, distance, hour in rows:
        await _execute(
            database,
            f'INSERT INTO "{SEAL_SCHEMA}"."treks" '
            "(id, grade, distance_km, started_at) VALUES ($1, $2, $3, $4)",
            identifier,
            WORKER,
            distance,
            SEAL_DAY.replace(hour=hour),
        )


class TestSealingPersists:
    """A sealed bucket is written, read back, and corrected -- on the server.

    Every assertion here is about the *round trip*. The arithmetic these
    numbers come from is already proved against a fake in
    `tests/series/test_sealing.py`; what could not be proved there is that the
    value survives being stored, because a fake accepts a shape the driver
    refuses.
    """

    async def test_a_sealed_bucket_is_stored(self, sealing):
        """The write the whole persistence half of sealing depends on.

        `settle()` is that write, and reading is deliberately not: a chart is a
        `GET`, and a read that stored as a side effect could only run on a
        write-workload session. The read still *answers* correctly before
        anything is stored -- asserted first, below -- which is what makes the
        settling job an optimisation rather than a prerequisite.
        """
        database, session = sealing
        await _add_treks(database, (1, 4.0, 9), (2, 6.0, 11))

        result = await sealed_view().run(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )

        assert result.series[0].values == (2,), "two treks in the sealed day"
        assert result.state is not None and result.state.settled == (SEAL_DAY,)
        assert await _settled_rows(database) == [], "reading a sealed view wrote"

        written = await sealed_view().settle(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )
        assert written == (SEAL_DAY,)

        stored = await _settled_rows(database)
        assert len(stored) == 1, "the sealed bucket reached the table"
        bucket, measures = stored[0]
        assert bucket == SEAL_DAY
        assert json.loads(measures) == {"treks": 2, "distance": 10.0}

    async def test_the_stored_value_is_read_without_the_source_rows(self, sealing):
        """Proof it came from storage, not from recomputation.

        Deleting the source rows between the two reads is a stronger claim than
        watching which statements ran: if the second read still answers 2, the
        only place that number can have come from is the settled table.
        """
        database, session = sealing
        await _add_treks(database, (1, 4.0, 9), (2, 6.0, 11))
        await sealed_view().settle(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )

        await _execute(database, f'DELETE FROM "{SEAL_SCHEMA}"."treks"')

        result = await sealed_view().run(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )
        assert result.series[0].values == (2,), (
            "a settled bucket must not need the rows it was computed from"
        )

    async def test_a_card_pulled_late_becomes_a_correction(self, sealing):
        """The late-data story, end to end.

        A card is pulled weeks after its photos were taken. Its sightings belong
        to a day that sealed long ago, so the settled value is not rewritten --
        the difference is recorded beside it and folded in on read, and the
        envelope says which bucket carries one.
        """
        database, session = sealing
        await _add_treks(database, (1, 4.0, 9), (2, 6.0, 11))
        # Settled *before* the late card arrives, which is the whole situation:
        # a day that was final, and then moved. Reading does not store, so this
        # is the explicit step a scheduled job would take.
        await sealed_view().settle(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )

        # The card comes out of the camera in April, carrying March's rows.
        await _add_treks(database, (3, 5.0, 14))
        moved = await sealed_view().reconcile(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )
        assert moved == (SEAL_DAY,), "reconcile names the bucket it corrected"

        corrections = await _correction_rows(database)
        assert len(corrections) == 1
        assert json.loads(corrections[0][1]) == {"treks": 1, "distance": 5.0}

        settled = await _settled_rows(database)
        assert json.loads(settled[0][1]) == {"treks": 2, "distance": 10.0}, (
            "the settled value stays immutable; the delta lives beside it"
        )

        result = await sealed_view().run(
            session, range=SEAL_RANGE, now=WELL_AFTER, grade=WORKER
        )
        assert result.series[0].values == (3,), "the correction folds in on read"
        assert result.state.corrections == (SEAL_DAY,), (
            "late data looks like late data arriving, not like a discrepancy"
        )
