"""DDL emission order: a foreign key never lands before the key it references.

The engine used to rank every constraint the same, which left the order inside
the constraint block decided by ``object_id`` -- a content hash of
``(kind, schema, table, name)``. Whether a schema applied at all therefore came
down to which hash happened to sort first, and for a realistic set of tables it
does not: ``stations``' foreign key to ``reserves`` sorts nine statements ahead
of ``reserves``' primary key, and PostgreSQL answers

    there is no unique constraint matching given keys for referenced table "reserves"

The single flat ``Widget`` the apply suite used could not see this, because a
table with no relationship has no ordering constraint to get wrong. The fixture
below is the shape that does: nine tables and thirteen foreign keys, taken from
``example/camera_trap`` so the regression is pinned to the artifact that found
it.
"""

from __future__ import annotations

import importlib
import os
import struct
import uuid
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import (
    _build_native_artifact,
    apply_single_artifact,
    detect_single,
    generate_single_plan,
)
from wreath.orm import Mapped, Model, column, eq, index, one_of, unique
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Bool,
    Int16,
    Int32,
    Int64,
    Jsonb,
    Numeric,
    Text,
    TimestampTz,
)

native: Any = importlib.import_module("wreath._native._postgres")

_EMPTY_DESCRIPTOR = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"


class Database:
    name = "ddl-ordering"


def _camera_trap_models(schema: str) -> list[type]:
    """The example's nine tables, re-declared into *schema*.

    Kept faithful to ``example/camera_trap/models.py`` in the parts that decide
    DDL order -- every table, every ``references=``, every unique and index --
    because the point of the fixture is that its hash ordering is the adversarial
    one that was actually observed, not a contrived one.
    """

    class Reserve(Model, table="reserves", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        slug: Mapped[str] = column(Text, unique=True)
        timezone: Mapped[str] = column(Text)
        area_hectares: Mapped[int] = column(Int32)
        created_at: Mapped[object] = column(TimestampTz)

    class Station(Model, table="stations", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        reserve_id: Mapped[int] = column(Int64, references=Reserve.id, index=True)
        name: Mapped[str] = column(Text)
        latitude: Mapped[object] = column(Numeric)
        longitude: Mapped[object] = column(Numeric)
        habitat: Mapped[str] = column(Text)
        sensitive: Mapped[bool] = column(Bool, default=False)
        _sensitive = index("reserve_id", "id", where=eq("sensitive", True))

    class Camera(Model, table="cameras", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
        serial: Mapped[str] = column(Text, unique=True)
        model: Mapped[str] = column(Text)
        deployed_at: Mapped[object] = column(TimestampTz)
        retired_at: Mapped[object] = column(TimestampTz, nullable=True)
        battery_pct: Mapped[int] = column(Int16)
        firmware: Mapped[str] = column(Text)
        _live = index("station_id", "deployed_at")

    class Species(Model, table="species", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        code: Mapped[str] = column(Text, unique=True)
        common_name: Mapped[str] = column(Text)
        scientific_name: Mapped[str] = column(Text)
        protection: Mapped[str] = column(Text)
        nocturnal: Mapped[bool] = column(Bool, default=False)
        _withheld = index(
            "protection", "id", where=one_of("protection", ["sensitive", "restricted"])
        )

    class Deployment(Model, table="deployments", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
        collected_at: Mapped[object] = column(TimestampTz)
        card_serial: Mapped[str] = column(Text)
        image_count: Mapped[int] = column(Int32)
        ingested_at: Mapped[object] = column(TimestampTz, nullable=True)
        _pending = index("station_id", "collected_at")

    class Observer(Model, table="observers", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        email: Mapped[str] = column(Text, unique=True)
        display_name: Mapped[str] = column(Text)
        role: Mapped[str] = column(Text)
        reserve_id: Mapped[object] = column(
            Int64, references=Reserve.id, nullable=True
        )

    class Sighting(Model, table="sightings", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
        camera_id: Mapped[int] = column(Int64, references=Camera.id, index=True)
        species_id: Mapped[int] = column(Int64, references=Species.id, index=True)
        deployment_id: Mapped[object] = column(
            Int64, references=Deployment.id, nullable=True, index=True
        )
        captured_at: Mapped[object] = column(TimestampTz, index=True)
        uploaded_at: Mapped[object] = column(TimestampTz)
        confidence: Mapped[int] = column(Int16)
        image_key: Mapped[str] = column(Text)
        thumbnail_key: Mapped[object] = column(Text, nullable=True)
        identified_by: Mapped[object] = column(
            Int64, references=Observer.id, nullable=True
        )
        review_state: Mapped[str] = column(Text)
        tags: Mapped[object] = column(Jsonb, default=dict)
        notes: Mapped[object] = column(Text, nullable=True)
        _activity = index("station_id", "captured_at")
        _unreviewed = index(
            "station_id", "captured_at", where=eq("review_state", "needs-review")
        )

    class Assignment(Model, table="assignments", schema=schema):
        observer_id: Mapped[int] = column(
            Int64, references=Observer.id, primary_key=True
        )
        reserve_id: Mapped[int] = column(
            Int64, references=Reserve.id, primary_key=True
        )
        level: Mapped[str] = column(Text)
        _by_reserve = index("reserve_id", "level")

    class AuditEntry(Model, table="audit_entries", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        observer_id: Mapped[int] = column(Int64, references=Observer.id, index=True)
        sighting_id: Mapped[int] = column(Int64, references=Sighting.id)
        action: Mapped[str] = column(Text)
        at: Mapped[object] = column(TimestampTz)
        _who = unique("observer_id", "sighting_id", "at")

    return [
        Reserve,
        Station,
        Camera,
        Species,
        Deployment,
        Observer,
        Sighting,
        Assignment,
        AuditEntry,
    ]


def _statements(tape: bytes) -> list[tuple[int, str]]:
    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


def _registry(schema: str) -> Any:
    return Registry(Database(), _camera_trap_models(schema), validate_schema="off")


def _forward_plan(schema: str) -> bytes:
    return native._migration_plan_descriptors(
        migrations._registry_descriptor(_registry(schema)), _EMPTY_DESCRIPTOR
    )


def _forward_sql(schema: str = "camera_trap") -> list[str]:
    return [sql for _flags, sql in _statements(
        native._migration_render_sql(_forward_plan(schema))
    )]


def _table_of(statement: str) -> str:
    """The table an ``alter table "s"."t" ...`` statement acts on."""
    return statement.split('"')[3]


def _referenced_table(statement: str) -> str:
    """The table a ``... references "s"."t" (...)`` statement points at."""
    return statement.split("references ")[1].split('"')[3]


def test_the_fixture_really_has_foreign_keys() -> None:
    """Guards the guard: a fixture that lost its relationships proves nothing."""
    foreign_keys = [sql for sql in _forward_sql() if "foreign key" in sql]
    assert len(foreign_keys) == 13
    assert len({_table_of(sql) for sql in foreign_keys}) == 7


def test_hash_order_alone_would_put_a_foreign_key_before_its_key() -> None:
    """The fixture is adversarial, not merely large.

    Sorted by the derived ``wreath_<hex object id>`` name -- which is what the
    engine did when every constraint shared one rank -- at least one foreign key
    precedes the primary key it depends on. Without this the ordering assertion
    below could pass by luck.
    """
    constraints = sorted(
        (sql for sql in _forward_sql() if " add constraint " in sql),
        key=lambda sql: sql.split('"wreath_')[1],
    )
    position = {}
    for offset, sql in enumerate(constraints):
        if "primary key" in sql or " unique " in sql:
            position.setdefault(_table_of(sql), offset)
    inverted = [
        sql for offset, sql in enumerate(constraints)
        if "foreign key" in sql and offset < position[_referenced_table(sql)]
    ]
    assert inverted, "fixture is no longer adversarial; the ordering test is vacuous"


def test_foreign_keys_are_emitted_after_the_keys_they_reference() -> None:
    forward = _forward_sql()
    first_key: dict[str, int] = {}
    for offset, sql in enumerate(forward):
        if " add constraint " in sql and ("primary key" in sql or " unique " in sql):
            first_key.setdefault(_table_of(sql), offset)
    for offset, sql in enumerate(forward):
        if "foreign key" not in sql:
            continue
        target = _referenced_table(sql)
        assert first_key[target] < offset, (
            f"foreign key on {_table_of(sql)} at {offset} precedes "
            f"{target}'s key at {first_key[target]}"
        )


def test_foreign_keys_are_emitted_after_every_index() -> None:
    """A unique *index* is also a valid foreign-key target, so indexes go first."""
    forward = _forward_sql()
    last_index = max(
        offset for offset, sql in enumerate(forward) if sql.startswith("create ")
        and " index " in sql
    )
    first_foreign_key = min(
        offset for offset, sql in enumerate(forward) if "foreign key" in sql
    )
    assert last_index < first_foreign_key


def test_create_table_and_columns_still_precede_every_constraint() -> None:
    forward = _forward_sql()
    last_column = max(
        offset for offset, sql in enumerate(forward) if " add column " in sql
    )
    first_constraint = min(
        offset for offset, sql in enumerate(forward) if " add constraint " in sql
    )
    assert max(
        offset for offset, sql in enumerate(forward) if sql.startswith("create table ")
    ) < last_column < first_constraint


def test_reverse_plan_drops_foreign_keys_before_anything_they_reference() -> None:
    """The inverse plan has to be a valid forward-shaped plan too."""
    reverse = [
        sql for _flags, sql in _statements(
            native._migration_render_sql(
                native._migration_reverse_plan(_forward_plan("camera_trap"))
            )
        )
    ]
    forward = _forward_sql()
    foreign_key_names = {
        sql.split('"wreath_')[1].split('"')[0]
        for sql in forward if "foreign key" in sql
    }
    dropped_foreign_keys = [
        offset for offset, sql in enumerate(reverse)
        if " drop constraint " in sql
        and sql.split('"wreath_')[1].split('"')[0] in foreign_key_names
    ]
    others = [
        offset for offset, sql in enumerate(reverse)
        if " drop constraint " in sql and offset not in set(dropped_foreign_keys)
    ]
    assert dropped_foreign_keys and others
    assert max(dropped_foreign_keys) < min(others)
    assert max(dropped_foreign_keys) < min(
        offset for offset, sql in enumerate(reverse) if sql.startswith("drop index ")
    )
    assert max(dropped_foreign_keys) < min(
        offset for offset, sql in enumerate(reverse) if sql.startswith("drop table ")
    )


def test_nothing_in_the_nine_table_schema_is_manual() -> None:
    statements = _statements(native._migration_render_sql(_forward_plan("camera_trap")))
    assert not any(flags & 2 for flags, _sql in statements)


# --- against a live PostgreSQL ------------------------------------------------

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


@pytest.mark.asyncio
@pytest.mark.network
async def test_real_apply_of_a_schema_with_foreign_keys() -> None:
    """The whole point: ``apply`` runs the artifact it generated, unedited.

    Before the ordering fix this raised ``there is no unique constraint matching
    given keys for referenced table``, from inside the DO block, on the first
    foreign key whose hash sorted early.
    """
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for the live apply test")
    from wreath.postgres import connect

    schema = f"wreath_fk_{uuid.uuid4().hex[:12]}"
    registry = _registry(schema)
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

        result = await apply_single_artifact(registry, db, artifact.data)

        assert result.checksum == artifact.checksum
        assert (await detect_single(registry, db)).current
        assert await db.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relkind = 'r'",
            schema,
        ) == 9
        assert await db.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND con.contype = 'f'",
            schema,
        ) == 13
    finally:
        await db.execute(
            'DELETE FROM "wreath_migrations"."history" WHERE target_schema = $1',
            schema,
        )
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()
