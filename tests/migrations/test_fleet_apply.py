from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.migrations import (
    _FLEET_CATALOG_SQL,
    _SINGLE_CATALOG_SQL,
    FleetRunInProgress,
    _build_native_artifact,
    _decode_catalog_snapshot,
    _fingerprint_image,
    apply_fleet,
    generate_single_plan,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.postgres import Database, PoolConfig, connect

pytestmark = [pytest.mark.asyncio, pytest.mark.database]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_live = pytest.mark.skipif(not _DSN, reason="set WREATH_TEST_POSTGRES_DSN for fleet tests")


async def _connection() -> Any:
    return await connect(_DSN or "")


async def _pool() -> Database:
    db = Database(
        name="fleet",
        dsn=_DSN or "",
        pools={"write": PoolConfig(min_size=1, max_size=4)},
    )
    await db.start()
    return db


def _model(schema: str) -> Any:
    class Widget(Model, table="widgets", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text, index=True)

    return Widget


async def _artifact(connection: Any, reference: str) -> Any:
    """A fleet artifact, generated once against any tenant.

    `fleet=True` neutralises both halves: the *actual* fingerprint is read
    through `_FLEET_CATALOG_SQL`, and the *desired* image is built from a
    descriptor whose schema names are empty -- which also makes the rendered
    DDL unqualified, so it binds to the applying transaction's `search_path`.

    `reference` therefore only decides which empty schema the plan is diffed
    against, and any tenant at the same starting point produces the same
    artifact.
    """

    class _Db:
        name = "fleet-artifact"

    registry = Registry(_Db(), [_model(reference)], validate_schema="off")
    generation = await generate_single_plan(registry, connection, fleet=True)
    return _build_native_artifact(
        migration_id=uuid.uuid4().bytes,
        parent_checksum=bytes(32),
        source_fingerprint=generation.actual_fingerprint,
        target_fingerprint=generation.desired_fingerprint,
        operation_tape=generation.diff.tape,
        named_plan=generation.plan.tape,
        sql_tape=generation.sql.tape,
    )


def _statements(tape: bytes) -> list[tuple[int, str]]:
    """The `(flags, sql)` pairs in a rendered SQL tape."""
    import struct

    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


@pytest.fixture
async def fleet():
    """Three empty tenant schemas and a pooled database, dropped afterwards."""
    if not _DSN:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for fleet tests")
    names = [f"tf_{uuid.uuid4().hex[:10]}" for _ in range(3)]
    admin = await _connection()
    for schema in names:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    db = await _pool()
    try:
        yield db, admin, names
    finally:
        await db.stop()
        for schema in names:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        placeholders = ", ".join(f"${index + 1}" for index in range(len(names)))
        await admin.execute(
            f'DELETE FROM "wreath_migrations"."history" WHERE target_schema IN ({placeholders})',
            *names,
        )


@_live
async def test_a_named_fingerprint_differs_between_identical_tenants(fleet) -> None:
    _db, admin, names = fleet
    first, second = names[0], names[1]
    for schema in (first, second):
        await admin.execute(f'CREATE TABLE "{schema}".widgets (id bigint PRIMARY KEY)')

    async def named(schema: str) -> bytes:
        snap = await _decode_catalog_snapshot(admin, _SINGLE_CATALOG_SQL, (schema,))
        return _fingerprint_image(snap.image)

    assert await named(first) != await named(second)


@_live
async def test_a_neutral_fingerprint_matches_across_identical_tenants(fleet) -> None:
    _db, admin, names = fleet
    first, second = names[0], names[1]
    for schema in (first, second):
        await admin.execute(f'CREATE TABLE "{schema}".widgets (id bigint PRIMARY KEY)')

    async def neutral(schema: str) -> bytes:
        snap = await _decode_catalog_snapshot(admin, _FLEET_CATALOG_SQL, (schema,))
        return _fingerprint_image(snap.image)

    assert await neutral(first) == await neutral(second)


@_live
async def test_a_neutral_fingerprint_still_separates_different_structures(fleet) -> None:
    # Neutralising the *name* must not neutralise the structure, or the
    # source-fingerprint refusal would wave through a tenant that has drifted.
    _db, admin, names = fleet
    same, different = names[0], names[1]
    await admin.execute(f'CREATE TABLE "{same}".widgets (id bigint PRIMARY KEY)')
    await admin.execute(f'CREATE TABLE "{different}".widgets (id bigint PRIMARY KEY, extra int)')

    async def neutral(schema: str) -> bytes:
        snap = await _decode_catalog_snapshot(admin, _FLEET_CATALOG_SQL, (schema,))
        return _fingerprint_image(snap.image)

    assert await neutral(same) != await neutral(different)


@_live
async def test_a_failure_names_the_tenant_and_the_reason(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    await admin.execute(f'CREATE TABLE "{names[1]}".widgets (id bigint PRIMARY KEY)')

    result = await apply_fleet(db, artifact.data, [names[1]])

    assert len(result.failed) == 1
    assert "fingerprint" in result.failed[0].error


@_live
async def test_a_second_concurrent_runner_is_refused(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])

    holder = await _pool()
    try:
        connection = await holder.acquire("write")
        try:
            await connection.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1::text, 0))",
                f"wreath:migrations:fleet:{artifact.migration_id}",
            )
            with pytest.raises(FleetRunInProgress, match="fleet lock"):
                await apply_fleet(db, artifact.data, names)
        finally:
            await connection.fetchval(
                "SELECT pg_advisory_unlock(hashtextextended($1::text, 0))",
                f"wreath:migrations:fleet:{artifact.migration_id}",
            )
            await holder.release("write", connection)
    finally:
        await holder.stop()


@_live
async def test_an_empty_fleet_is_refused(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    with pytest.raises(ValueError, match="at least one tenant schema"):
        await apply_fleet(db, artifact.data, [])


@_live
async def test_a_repeated_schema_is_refused(fleet) -> None:
    # A fleet is a set of tenants, and a repeat means the directory is wrong --
    # applying twice would refuse on the second pass anyway, having already
    # committed the first.
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    with pytest.raises(ValueError, match="same schema twice"):
        await apply_fleet(db, artifact.data, [names[0], names[0]])


@_live
async def test_a_schema_that_is_not_an_identifier_is_refused(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    with pytest.raises(ValueError, match="plain SQL identifier"):
        await apply_fleet(db, artifact.data, ['t"; DROP SCHEMA public; --'])


@_live
async def test_one_artifact_migrates_every_tenant(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])

    result = await apply_fleet(db, artifact.data, names)

    assert result.complete, result.summary()
    assert set(result.applied) == set(names)
    for schema in names:
        recorded = await admin.fetchval(
            'SELECT count(*) FROM "wreath_migrations"."history" '
            "WHERE target_schema = $1 AND checksum = $2",
            schema,
            artifact.checksum,
        )
        assert recorded == 1, schema
        # Each tenant got its *own* table, not the reference tenant's again.
        exists = await admin.fetchval(
            # `::text` because `nspname` is `name`: without the cast PostgreSQL
            # infers the parameter as `name` too, which the driver cannot
            # encode. The tree's own `wreath-sql-lint` SQL002.
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1::text AND c.relname = 'widgets'",
            schema,
        )
        assert exists == 1, schema


@_live
async def test_a_second_run_skips_rather_than_refusing(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])

    await apply_fleet(db, artifact.data, names)
    again = await apply_fleet(db, artifact.data, names)

    assert again.complete
    assert set(again.skipped) == set(names)
    assert again.applied == ()


@_live
async def test_a_stopped_run_is_finished_by_re_running(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    # A tenant that cannot take the artifact: its structure has already moved.
    await admin.execute(f'CREATE TABLE "{names[1]}".widgets (id bigint PRIMARY KEY)')

    stopped = await apply_fleet(db, artifact.data, names)
    assert not stopped.complete
    assert names[0] in stopped.applied
    assert [o.schema for o in stopped.failed] == [names[1]]
    assert names[2] not in stopped.applied  # halted before it

    await admin.execute(f'DROP TABLE "{names[1]}".widgets')
    finished = await apply_fleet(db, artifact.data, names)
    assert finished.complete, finished.summary()
    assert names[0] in finished.skipped
    assert set(finished.applied) == {names[1], names[2]}


@_live
async def test_continuing_past_a_failure_is_opt_in(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    await admin.execute(f'CREATE TABLE "{names[1]}".widgets (id bigint PRIMARY KEY)')

    result = await apply_fleet(db, artifact.data, names, stop_on_error=False)

    assert set(result.applied) == {names[0], names[2]}
    assert [o.schema for o in result.failed] == [names[1]]


@_live
async def test_the_lock_is_released_so_the_next_run_proceeds(fleet) -> None:
    db, admin, names = fleet
    artifact = await _artifact(admin, names[0])
    assert (await apply_fleet(db, artifact.data, names)).complete
    assert (await apply_fleet(db, artifact.data, names)).complete


@_live
async def test_the_rendered_ddl_names_no_tenant(fleet) -> None:
    _db, admin, names = fleet

    class _Db:
        name = "fleet-ddl"

    registry = Registry(_Db(), [_model(names[0])], validate_schema="off")
    generation = await generate_single_plan(registry, admin, fleet=True)
    rendered = " ".join(sql for _flags, sql in _statements(generation.sql.tape))

    assert "widgets" in rendered
    for schema in names:
        assert schema not in rendered
