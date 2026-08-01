"""A `point` column and its GiST index through detect/generate/apply/down.

The claim `wreath.geospatial` rests on is that a proximity search is *indexable
on a stock PostgreSQL* -- no extension, because `point` and the `point_ops`
operator class are core. An index that cannot survive a migration round trip
does not deliver that: `detect` would report drift on an index it created
itself, `generate` would emit the same statement forever, and the operator
would eventually stop reading the output.

That failure is silent in exactly the way the pgvector one was, and it had
exactly one cause there -- a declared operator class that PostgreSQL treats as
its *default* and therefore does not record. `point_ops` is gist's default for
`point`, so this suite runs the same arm deliberately: `index="gist"` with no
opclass, and `index="gist", index_ops="point_ops"` naming the default out loud.
Both have to come back clean.

The rendering tests below prove the emitter agrees with itself. Only the gated
ones prove it agrees with *PostgreSQL*, which is the claim the guide makes.

Named `test_geospatial_columns` rather than `test_geospatial`: `tests/` has no
`__init__.py`, so pytest derives a module name from the basename alone and two
files called `test_geospatial.py` in different directories collide with an
"import file mismatch" that names the *other* file.
"""

from __future__ import annotations

import importlib
import os
import struct
import uuid
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.geospatial import Coordinate
from wreath.migrations import (
    _build_native_artifact,
    apply_single_artifact,
    detect_single,
    generate_single_plan,
    revert_single_artifact,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Point, Text
from wreath.postgres import connect

native: Any = importlib.import_module("wreath._native._postgres")

EMPTY_IMAGE = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"


class Database:
    name = "main"


class Station(Model, table="stations", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Point, index="gist")


def _statements(tape: bytes) -> list[tuple[int, str]]:
    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


def _image(*models: type) -> bytes:
    return migrations._registry_descriptor(
        Registry(Database(), list(models), validate_schema="off")
    )


def _forward(*models: type) -> list[tuple[int, str]]:
    plan = native._migration_plan_descriptors(_image(*models), EMPTY_IMAGE)
    return _statements(native._migration_render_sql(plan))


# --- rendering ----------------------------------------------------------------


def test_a_point_column_is_created_as_the_builtin_type() -> None:
    assert any(
        'add column "at" point not null;' in sql for _flags, sql in _forward(Station)
    )


def test_the_index_names_gist_as_its_access_method() -> None:
    (created,) = [sql for _f, sql in _forward(Station) if sql.startswith("create index")]
    assert 'using gist ("at")' in created, created


def test_nothing_about_a_point_model_falls_back_to_manual() -> None:
    """A MANUAL step is one an operator writes by hand, which is not the promise."""
    assert not any(flags & 2 for flags, _sql in _forward(Station)), _forward(Station)


def test_down_drops_the_index_and_the_column() -> None:
    plan = native._migration_plan_descriptors(_image(Station), EMPTY_IMAGE)
    reversed_plan = native._migration_reverse_plan(plan)
    rendered = _statements(native._migration_render_sql(reversed_plan))
    statements = [sql for _flags, sql in rendered]
    assert any(sql.startswith("drop index ") for sql in statements), statements
    assert any('drop table "app"."stations"' in sql for sql in statements), statements
    assert not any(flags & 2 for flags, _sql in rendered), rendered


# --- against a real server -----------------------------------------------------

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark_live = [
    pytest.mark.skipif(
        _DSN is None,
        reason="set WREATH_TEST_POSTGRES_DSN for the point/GiST catalog round trip",
    ),
    pytest.mark.asyncio,
    pytest.mark.database,
]


async def _round_trip(model: type, schema: str) -> None:
    """Apply the model's DDL, then assert a second `generate` has nothing to say."""
    db = await connect(_DSN)
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        registry = Registry(Database(), [model], validate_schema="off")

        generation = await generate_single_plan(registry, db)
        emitted = _statements(generation.sql.tape)
        assert emitted
        assert not any(flags & 2 for flags, _sql in emitted), emitted
        for _flags, statement in emitted:
            await db.execute(statement)

        assert (await detect_single(registry, db)).current
        assert _statements((await generate_single_plan(registry, db)).sql.tape) == []

        # And the index PostgreSQL actually built uses gist, which a matching
        # descriptor alone would not prove: a descriptor pair can agree with
        # each other about an index that was never created with that method.
        method = await db.fetchval(
            "SELECT am.amname FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_catalog.pg_am am ON am.oid = ic.relam "
            "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND am.amname <> 'btree'",
            schema,
        )
        # `pg_am.amname` is PostgreSQL's `name` type, which has no binary codec
        # here and arrives as raw bytes rather than as `str`.
        assert bytes(method) == b"gist", method
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_gist_index_on_a_point_round_trips() -> None:
    schema = f"wreath_geo_{uuid.uuid4().hex[:12]}"

    class Live(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Point, index="gist")

    await _round_trip(Live, schema)


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_naming_gists_default_operator_class_round_trips_too() -> None:
    """The arm that found the pgvector defect, run against the geo default.

    `point_ops` is gist's default operator class for `point`, and PostgreSQL
    does not record that a default was named -- the catalog read blanks it. A
    desired descriptor that kept the name would disagree with the catalog on
    every run, so `detect` would report drift on an index that is already
    exactly right and `generate` would emit a MANUAL forever.
    """
    schema = f"wreath_geo_{uuid.uuid4().hex[:12]}"

    class Live(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Point, index="gist", index_ops="point_ops")

    await _round_trip(Live, schema)


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_the_declared_and_blanked_spellings_are_one_migration_not_two() -> None:
    """Declaring the default and omitting it must describe the *same* index.

    Two spellings that each round-trip individually could still be different
    descriptors, in which case switching between them would emit a spurious
    migration. Resolved against this database's real defaults, they are one.
    """
    schema = f"wreath_geo_{uuid.uuid4().hex[:12]}"

    class Bare(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Point, index="gist")

    class Named(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Point, index="gist", index_ops="point_ops")

    db = await connect(_DSN)
    try:
        bare = Registry(Database(), [Bare], validate_schema="off")
        named = Registry(Database(), [Named], validate_schema="off")
        await migrations._resolve_default_opclasses(bare, db)
        await migrations._resolve_default_opclasses(named, db)
        assert migrations._registry_descriptor(bare) == migrations._registry_descriptor(
            named
        )
    finally:
        await db.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_apply_then_down_returns_the_schema_to_where_it_started() -> None:
    """The whole cycle: generate, apply, detect clean, revert, nothing left.

    `apply` and `down` are the two halves nobody runs in a rendering test, and
    the reverse tape is derived from the forward plan rather than regenerated
    -- so a `point` column or a GiST index the inverter did not understand
    would strand the schema half-migrated rather than raise.
    """
    schema = f"wreath_geo_{uuid.uuid4().hex[:12]}"

    class Live(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        at: Mapped[Coordinate] = column(Point, index="gist")

    registry = Registry(Database(), [Live], validate_schema="off")
    db = await connect(_DSN)
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        generation = await generate_single_plan(registry, db)
        artifact = _build_native_artifact(
            migration_id=uuid.uuid4().bytes,
            parent_checksum=bytes(32),
            source_fingerprint=generation.actual_fingerprint,
            target_fingerprint=generation.desired_fingerprint,
            operation_tape=generation.diff.tape,
            named_plan=generation.plan.tape,
            sql_tape=generation.sql.tape,
        )

        applied = await apply_single_artifact(registry, db, artifact.data)
        assert applied.checksum == artifact.checksum
        assert (await detect_single(registry, db)).current

        indexes = await db.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_catalog.pg_am am ON am.oid = ic.relam "
            "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND am.amname = 'gist'",
            schema,
        )
        assert indexes == 1

        # `force` because the running registry still maps what the reverse
        # drops, which is the ordinary state of a local rewind; the hazard scan
        # is tested where it belongs, in test_downgrade.py.
        reverted = await revert_single_artifact(
            registry, db, artifact.data, allow_destructive=True, force=True
        )
        assert reverted.forced

        remaining = await db.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = 'stations'",
            schema,
        )
        assert remaining == 0, "the reverse tape left the table behind"
        tip = await db.fetchval(
            'SELECT count(*) FROM "wreath_migrations"."history" '
            "WHERE target_schema = $1",
            schema,
        )
        assert tip == 0
    finally:
        await db.execute(
            'DELETE FROM "wreath_migrations"."history" WHERE target_schema = $1',
            schema,
        )
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


# --- tier 2: a PostGIS `geography` column ---------------------------------------
#
# The same round trip one type further out, and the extension is the whole of
# the difference. `point` renders from a compile-time OID; `geography`'s OID is
# assigned by `CREATE EXTENSION`, so the descriptor carries the *spelling*
# `geography(Point,4326)` and the catalog read produces `format_type`'s -- a
# disagreement of one byte would rediscover the column as drift forever, which
# is the defect the pgvector suite was written after finding.
#
# Nothing here runs in the database `tests/orm/test_geospatial_live.py` uses:
# that one is created from `template0` and stays extension-free, which is what
# keeps the tier-1 claim an assertion rather than an assumption.


def _bind_geography(oid: int) -> None:
    """Give every declared `geography` this OID, as startup resolution would."""
    from wreath.orm.types import bind_extension_oid

    bind_extension_oid("geography", oid)


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_geography_column_and_its_gist_index_round_trip() -> None:
    """Render, apply, then read back: the two spellings have to agree.

    Asserted against a real PostGIS rather than against a synthetic catalog
    image, because rendering agreeing with itself is not the claim -- the claim
    is that the emitter agrees with PostgreSQL, and that has exactly one
    failure mode and it is silent.
    """
    from wreath.orm.types import Geography, _unbind_extension_oids

    schema = f"wreath_postgis_{uuid.uuid4().hex[:12]}"
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except Exception:  # noqa: BLE001 - reported as a skip, see below
            # A server without PostGIS cannot answer this, and the suite must
            # say so rather than pass by accident. Tier 2 is the only thing in
            # the repository that needs postgis/postgis:17-3.5.
            pytest.skip("this PostgreSQL has no PostGIS; use postgis/postgis:17-3.5")
        rows = await db.fetch("SELECT oid FROM pg_type WHERE typname = 'geography'")
        if not rows:
            pytest.skip("this PostGIS installs no 'geography' type")
        oid = int(rows[0][0])

        class Live(Model, table="stations", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            at: Mapped[Coordinate] = column(Geography(), index="gist")

        # The real OID this server assigned, not an invented one: a process
        # resolves an extension type once, so the fake has to be released first.
        _unbind_extension_oids()
        _bind_geography(oid)

        await db.execute(f'CREATE SCHEMA "{schema}"')
        registry = Registry(Database(), [Live], validate_schema="off")
        generation = await generate_single_plan(registry, db)
        emitted = _statements(generation.sql.tape)
        assert emitted
        assert not any(flags & 2 for flags, _sql in emitted), emitted
        assert any("geography(Point,4326)" in sql for _f, sql in emitted), emitted
        assert any("using gist" in sql for _f, sql in emitted), emitted
        for _flags, statement in emitted:
            await db.execute(statement)

        assert (await detect_single(registry, db)).current
        assert _statements((await generate_single_plan(registry, db)).sql.tape) == []

        # The column PostgreSQL actually built, spelled by `format_type` -- the
        # other side of the comparison the descriptor only asserts about itself.
        spelled = await db.fetchval(
            "SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = 'stations' AND a.attname = 'at'",
            schema,
        )
        assert spelled == "geography(Point,4326)", spelled
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN")
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_geography_migration_applies_and_downgrades() -> None:
    """`apply` and `down` over an extension-typed column and its GiST index.

    The reverse tape is inverted from the forward plan rather than regenerated,
    so a column spelling the inverter did not carry would strand the schema
    half-migrated rather than raise.
    """
    from wreath.orm.types import Geography, _unbind_extension_oids

    schema = f"wreath_postgis_{uuid.uuid4().hex[:12]}"
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except Exception:  # noqa: BLE001 - reported as a skip, see above
            pytest.skip("this PostgreSQL has no PostGIS; use postgis/postgis:17-3.5")
        rows = await db.fetch("SELECT oid FROM pg_type WHERE typname = 'geography'")
        if not rows:
            pytest.skip("this PostGIS installs no 'geography' type")

        class Live(Model, table="stations", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            at: Mapped[Coordinate] = column(Geography(), index="gist")

        _unbind_extension_oids()
        _bind_geography(int(rows[0][0]))

        await db.execute(f'CREATE SCHEMA "{schema}"')
        registry = Registry(Database(), [Live], validate_schema="off")
        generation = await generate_single_plan(registry, db)
        artifact = _build_native_artifact(
            migration_id=uuid.uuid4().bytes,
            parent_checksum=bytes(32),
            source_fingerprint=generation.actual_fingerprint,
            target_fingerprint=generation.desired_fingerprint,
            operation_tape=generation.diff.tape,
            named_plan=generation.plan.tape,
            sql_tape=generation.sql.tape,
        )
        applied = await apply_single_artifact(registry, db, artifact.data)
        assert applied.checksum == artifact.checksum
        assert (await detect_single(registry, db)).current

        reverted = await revert_single_artifact(
            registry, db, artifact.data, allow_destructive=True, force=True
        )
        assert reverted.forced
        remaining = await db.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = 'stations'",
            schema,
        )
        assert remaining == 0, "the reverse tape left the table behind"
    finally:
        await db.execute(
            'DELETE FROM "wreath_migrations"."history" WHERE target_schema = $1',
            schema,
        )
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


# --- the alphabet an extension spelling may use ---------------------------------
#
# `geography(Point,4326)` is the first declared type whose modifier carries
# *letters*. The renderer's whitelist admitted only digits, commas and spaces
# inside the parentheses, so the column became an empty MANUAL and `generate`
# omitted it while still emitting the index that referenced it -- applying such
# a plan fails on a column that was never added.
#
# The alphabet is wider now, and these pin that widening: nothing that could
# close a statement or open a literal is admitted at any point. The spelling
# comes from a declaration rather than from a request, so this is defence in
# depth -- but it is emitted into DDL text rather than bound, which is the one
# place in the migration engine where that distinction stops being academic.


def _one_column_descriptor(spelling: str) -> bytes:
    header = b"WMD1\x01\x00\x00\x00\x01\x00\x00\x00"
    return header + migrations._descriptor_record(
        "app", "subjects", "value", 2, f"column\x1f99999\x1f{spelling}\x1f1\x1f\x1f\x1f"
    )


@pytest.mark.parametrize(
    "spelling",
    [
        "geography(Point); drop table x --,4326)",
        "geography(Point,4326); drop table x",
        'geography("Point",4326)',
        "geography(Point,4326)extra",
        "geography(Point,(4326))",
        "geography(Point,4326)\n",
        "Geography(Point,4326)",
        "geography(Point,4326",
        "",
    ],
)
def test_a_hostile_type_spelling_stays_manual(spelling: str) -> None:
    plan = native._migration_plan_descriptors(
        _one_column_descriptor(spelling), EMPTY_IMAGE
    )
    rendered = _statements(native._migration_render_sql(plan))
    column_steps = [
        (flags, sql) for flags, sql in rendered if not sql.startswith("create table")
    ]
    assert column_steps, rendered
    assert all(flags & 2 for flags, _sql in column_steps), column_steps
    assert not any(spelling and spelling in sql for _f, sql in rendered), rendered


@pytest.mark.parametrize(
    "spelling",
    ["geography(Point,4326)", "geography(PointZ,4326)", "vector(1536)", "bit(8)"],
)
def test_a_format_type_spelling_still_renders(spelling: str) -> None:
    """The other side of the same boundary: what PostgreSQL really produces."""
    plan = native._migration_plan_descriptors(
        _one_column_descriptor(spelling), EMPTY_IMAGE
    )
    rendered = _statements(native._migration_render_sql(plan))
    assert any(spelling in sql for _f, sql in rendered), rendered
    assert not any(flags & 2 for flags, _sql in rendered), rendered
