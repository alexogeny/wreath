"""The example's schema and seed against a real PostgreSQL.

Skipped without ``WREATH_TEST_POSTGRES_DSN``. These assert the numbers
``docs/example/walkthrough.md`` prints, so the page cannot drift away from the
data without a test going red -- which is the whole reason the example is a
tested package rather than prose.

The schema is built here by executing the checked-in migration artifact's DDL in
the order the artifact emits it. That sentence used to need a paragraph of
apology: the engine sorted every constraint into one block by content-hash name,
so a foreign key could land before the primary key it referenced, and this file
carried a helper that reordered them. The engine now ranks foreign keys after
both keys and unique indexes, ``wreath migrations apply`` applies this artifact
end to end, and the reordering is gone —
:func:`test_the_artifact_emits_its_statements_in_a_usable_order` is what remains
of it, so a regression is a failing test rather than a rediscovered workaround.
"""

from __future__ import annotations

import os
from inspect import signature

import pytest
from _camera_trap import build_schema, drop_schema, statements
from camera_trap.models import SCHEMA
from camera_trap.seed import build_rows, seed
from camera_trap.tasks import QUEUE_TABLES

#: No ``pytest.mark.asyncio`` here: ``asyncio_mode = "auto"`` already marks the
#: async tests, and a module-level mark also lands on the two synchronous ones,
#: where pytest-asyncio warns that it cannot apply it. A marker that does
#: nothing reads exactly like a marker that is silently not being applied, which
#: is the more expensive of the two to discover. ``test_read_api.py`` carries the
#: same note for the same reason.

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap schema tests",
)

#: A sample, not the full 140,000 -- these tests assert *shape* against the
#: database, and the walkthrough's exact counts are asserted from the generator
#: in the sibling module, which needs no I/O. Seeding 140k here would add ~13s
#: to every run for no extra coverage.
SAMPLE = 5_000


@pytest.fixture
async def seeded():
    """A freshly built schema with a sample of the seed in it.

    The artifact does not create its own namespace -- a migration describes
    tables, and which schema they land in is the application's decision, made by
    `schema=SCHEMA` on the models. `build_schema` creates it first for that
    reason.
    """
    from wreath.postgres import connect

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
        yield connection
    finally:
        await drop_schema(connection)
        await connection.close()


def test_the_artifact_emits_its_statements_in_a_usable_order() -> None:
    """Tables, then columns, then keys and indexes, then foreign keys.

    A foreign key needs the key or unique index it points at to exist already,
    and for a while the engine sorted every constraint into one block by its
    content-hash name -- so ``stations``' foreign key to ``reserves`` landed nine
    statements before ``reserves`` got its primary key, and the example could not
    apply its own migration. This asserts the property rather than the fix, so it
    stays true whatever the emitter does next. No database needed.
    """
    ranks = {"create table": 0, "add column": 1, "foreign key": 3}

    def rank(statement: str) -> int:
        if statement.startswith("create table"):
            return ranks["create table"]
        if "add column" in statement:
            return ranks["add column"]
        if "foreign key" in statement:
            return ranks["foreign key"]
        return 2  # primary/unique constraints and indexes

    seen = [rank(statement) for statement in statements()]
    assert seen == sorted(seen), "the artifact's statements are not in dependency order"


@skip_without_database
async def test_the_artifact_builds_the_whole_schema(seeded) -> None:
    """Nine model tables plus the durable queue, and the partial indexes survived.

    The tenth table is `ingest_jobs`, and counting it separately is the point.
    It does **not** come from the migration artifact: `wreath migrations
    generate` derives that from the ORM models, and the job queue is not one --
    it is described by `JobRunner.schema_sql()` and applied alongside. So this
    assertion is really two, and splitting them is what stops a future artifact
    losing a model table while the queue keeps the total right.
    """
    # One placeholder per value rather than `= ANY($1)`: the driver refuses to
    # bind a list, because a sequence has no inferable element type. Its own
    # error says to write it this way. `QUEUE_TABLES` is one name today, and
    # the assertion below is what fails loudly if that stops being true rather
    # than letting this query quietly check the wrong thing.
    assert len(QUEUE_TABLES) == 1, "this query binds exactly one queue table name"
    (queue_table,) = QUEUE_TABLES

    model_tables = await seeded.fetchval(
        "SELECT count(*) FROM pg_tables "
        "WHERE schemaname = $1::text AND tablename <> $2::text",
        SCHEMA,
        queue_table,
    )
    assert model_tables == 9
    queue_tables = await seeded.fetchval(
        "SELECT count(*) FROM pg_tables "
        "WHERE schemaname = $1::text AND tablename = $2::text",
        SCHEMA,
        queue_table,
    )
    assert queue_tables == 1, "the durable queue's table was never applied"
    partial = await seeded.fetchval(
        "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "WHERE n.nspname = $1::text AND i.indpred IS NOT NULL "
        "AND t.relname <> $2::text",
        SCHEMA,
        queue_table,
    )
    assert partial == 5, "the five declared partial indexes are not in the catalog"


@skip_without_database
async def test_a_partial_index_covers_its_own_predicate(seeded) -> None:
    """PostgreSQL stored the predicate wreath declared, in its normal form.

    If these drift apart, ``wreath migrations detect`` reports a change on every
    run forever, which is the failure partial-index support was built to avoid.
    """
    # Restricted to the ORM's own tables. The durable queue brings a partial
    # index of its own (`jobs_dedup_idx`, `WHERE dedup_key IS NOT NULL`), and
    # it is not one of the five this test is about — it is `JobRunner`'s, and
    # wreath tests it where it is declared.
    predicates = await seeded.fetch(
        "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "WHERE n.nspname = $1::text AND i.indpred IS NOT NULL "
        "AND t.relname <> $2::text",
        SCHEMA,
        QUEUE_TABLES[0],
    )
    rendered = sorted(row[0] for row in predicates)
    assert rendered == [
        "(ingested_at IS NULL)",
        "(protection = ANY (ARRAY['sensitive'::text, 'restricted'::text]))",
        "(retired_at IS NULL)",
        "(review_state = 'needs-review'::text)",
        "(sensitive = true)",
    ]


@skip_without_database
async def test_captures_read_back_in_the_reserve_wall_clock(seeded) -> None:
    """Nocturnal species are nocturnal *in local time*, not in UTC.

    This is the assertion the walkthrough's night-versus-day table rests on, and
    the one that failed when the seed generated hours in UTC: a +09:30 reserve
    showed its night species peaking at local noon.
    """
    rows = await seeded.fetch(
        "SELECT date_part('hour', s.captured_at AT TIME ZONE r.timezone)::int, "
        "       count(*) FILTER (WHERE sp.nocturnal), "
        "       count(*) FILTER (WHERE NOT sp.nocturnal) "
        f'FROM "{SCHEMA}".sightings s '
        f'JOIN "{SCHEMA}".species sp ON sp.id = s.species_id '
        f'JOIN "{SCHEMA}".stations st ON st.id = s.station_id '
        f'JOIN "{SCHEMA}".reserves r ON r.id = st.reserve_id '
        "WHERE date_part('hour', s.captured_at AT TIME ZONE r.timezone) IN (3, 13) "
        "GROUP BY 1 ORDER BY 1"
    )
    by_hour = {row[0]: (row[1], row[2]) for row in rows}
    assert by_hour[3][1] == 0, "a day species was recorded at 03:00 local"
    assert by_hour[13][0] == 0, "a night species was recorded at 13:00 local"
    assert by_hour[3][0] > 0 and by_hour[13][1] > 0


@skip_without_database
async def test_a_late_card_is_queryable(seeded) -> None:
    """``deployment_id`` makes "how late was this row" a question with an answer."""
    stale = await seeded.fetchval(
        "SELECT count(*) FROM ("
        f'  SELECT d.id FROM "{SCHEMA}".deployments d '
        f'  JOIN "{SCHEMA}".sightings s ON s.deployment_id = d.id '
        "  GROUP BY d.id "
        "  HAVING d.collected_at::date - max(s.captured_at)::date > 7"
        ") q"
    )
    assert stale > 0, "no card is meaningfully later than its last image"


@skip_without_database
async def test_the_review_state_column_holds_the_mess(seeded) -> None:
    """Chapter two needs the flaw to be in the database, not just the generator."""
    spellings = await seeded.fetch(
        f'SELECT DISTINCT review_state FROM "{SCHEMA}".sightings ORDER BY 1'
    )
    values = {row[0] for row in spellings}
    assert {"confirmed", "Confirmed", "ok", "needs-review", "needs review"} <= values


@skip_without_database
async def test_seeding_twice_leaves_the_same_rows(seeded) -> None:
    """The determinism claim, end to end through the driver and back.

    The sibling module proves the *generator* is deterministic. This proves
    nothing is lost or reordered on the way through PostgreSQL -- which is the
    form the walkthrough's reader actually depends on.
    """
    digest = (
        f'SELECT md5(string_agg(s::text, \'|\' ORDER BY s.id)) FROM "{SCHEMA}".sightings s'
    )
    first = await seeded.fetchval(digest)
    await seed(seeded, sightings=SAMPLE)
    second = await seeded.fetchval(digest)
    assert first == second


def test_the_generator_matches_the_walkthrough_counts() -> None:
    """The numbers ``walkthrough.md`` prints. No database needed."""
    # Building all 140,000 sightings merely to count them made this assertion
    # spend about 1.3 seconds allocating rows whose contents it never read.
    # The default is the walkthrough contract; a one-row generation proves the
    # parameter still controls the emitted collection.
    assert signature(build_rows).parameters["sightings"].default == 140_000
    rows = build_rows(sightings=1)
    assert len(rows["sightings"]) == 1
    assert len(rows["deployments"]) == 576
    assert len(rows["cameras"]) == 61
    assert len(rows["stations"]) == 48
    assert len(rows["reserves"]) == 4
