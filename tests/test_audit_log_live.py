from __future__ import annotations

import json
import os

import pytest

from wreath.audit_log import (
    REDACTED,
    AuditTrail,
    Unattributed,
    actor,
    append_only_statements,
    audited,
    current_actor,
    declaration,
)
from wreath.log import PostgresLog
from wreath.orm import Model, Registry, Session, column
from wreath.orm.errors import StaleDataError
from wreath.orm.types import Int64, Text
from wreath.postgres import Database

pytestmark = [
    pytest.mark.asyncio,
    # `network` rather than `database`, against `pyproject.toml`'s general rule:
    # every read here stops at a cluster-wide horizon that any other worker's
    # open transaction pins. See `tests/test_log_cursor_live.py` for the
    # measurement and the reasoning.
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN to run live audit trail tests",
    ),
]

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_audit_{_WORKER}"

_DECLARATION = declaration("audit_records", schema=_SCHEMA)

#: The stream an audited row's records land in. `SchemaSpec.qualified_name` is
#: **unquoted** -- `schema.table` -- and the stream name has to match it exactly.
#: Spelled once here: getting it wrong makes every read return nothing, which
#: makes the negative tests (`no record after a rollback`) pass for the wrong
#: reason. It did, and that is why this is a constant.
_PHOTOS = f"{_SCHEMA}.photos"


class Photo(Model, table="photos", schema=_SCHEMA):
    id: int = column(Int64, primary_key=True)
    caption: str = column(Text)
    exif_gps: str = column(Text, nullable=True)

    _audit = audited(redact={"exif_gps"})


class Untracked(Model, table="untracked", schema=_SCHEMA):
    id: int = column(Int64, primary_key=True)
    note: str = column(Text)


async def _apply(database, *statements: str) -> None:
    """Execute statements verbatim.

    Deliberately does not split on `";\\n"`, the way the older suites do: the
    append-only trigger carries a dollar-quoted function body, and splitting a
    blob would cut it in half. `Log.statements()` and
    `append_only_statements()` both hand back tuples, which is what the tuple is
    for.
    """
    connection = await database.acquire("write")
    try:
        for statement in statements:
            if statement.strip():
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    handle = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 6}})
    await handle.start()
    await _apply(handle, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(handle, *_DECLARATION.statements())
    await _apply(handle, *append_only_statements("audit_records", schema=_SCHEMA))
    await _apply(
        handle,
        f'CREATE TABLE IF NOT EXISTS "{_SCHEMA}".photos '
        "(id bigint PRIMARY KEY, caption text NOT NULL, exif_gps text)",
        f'CREATE TABLE IF NOT EXISTS "{_SCHEMA}".untracked '
        "(id bigint PRIMARY KEY, note text NOT NULL)",
    )
    try:
        yield handle
    finally:
        await _apply(handle, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await handle.stop()


@pytest.fixture
def trail(database):
    return AuditTrail(PostgresLog(database, _DECLARATION))


@pytest.fixture
def registry(database):
    return Registry(database, [Photo, Untracked], validate_schema="off")


@pytest.fixture
async def sessions(registry):
    """Hands out sessions and closes every one of them.

    A `Session` holds its pooled connection until it is closed, and that
    connection holds locks on the tables it wrote. Leaving one open makes the
    fixture's `DROP SCHEMA ... CASCADE` block on it forever -- which does not
    look like a leaked session, it looks like the suite hanging.
    """
    opened: list[Session] = []

    def open_one(registry_, trail):
        session = Session(registry_, "write", audit=trail)
        opened.append(session)
        return session

    try:
        yield open_one
    finally:
        for session in opened:
            await session.close()


async def _records(trail, key: str):
    batch = await trail.history(_PHOTOS, key)
    return [
        {
            "actor": record["actor"],
            "op": record["op"],
            "fields": json.loads(record["fields"]),
        }
        for record in batch
    ]


async def test_an_insert_records_itself(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Photo(id=1, caption="a heron", exif_gps="-27.4,153.0"))
        await session.flush()

    records = await _records(trail, "1")
    assert len(records) == 1
    assert records[0]["actor"] == "user:41"
    assert records[0]["op"] == "insert"
    assert records[0]["fields"]["caption"] == "a heron"


async def test_an_update_records_only_the_fields_it_set(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        photo = Photo(id=2, caption="before", exif_gps=None)
        session.add(photo)
        await session.flush()
        photo.caption = "after"
        await session.flush()

    records = await _records(trail, "2")
    assert [record["op"] for record in records] == ["insert", "update"]
    # The update names `caption` and not the columns it left alone -- a record
    # that listed every column would make "what changed" unanswerable.
    assert list(records[1]["fields"]) == ["caption"]
    assert records[1]["fields"]["caption"] == "after"


async def test_a_delete_records_itself_with_the_row_it_removed(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        photo = Photo(id=3, caption="going", exif_gps=None)
        session.add(photo)
        await session.flush()
        session.delete(photo)
        await session.flush()

    records = await _records(trail, "3")
    assert [record["op"] for record in records] == ["insert", "delete"]
    assert records[1]["fields"]["caption"] == "going"


async def test_a_write_from_a_background_task_is_recorded_the_same_way(registry, trail, sessions):
    import asyncio

    async def nightly():
        session = sessions(registry, trail)
        with actor("job:nightly-rollup"):
            session.add(Photo(id=4, caption="from a job", exif_gps=None))
            await session.flush()

    await asyncio.create_task(nightly())
    records = await _records(trail, "4")
    assert [record["actor"] for record in records] == ["job:nightly-rollup"]


async def test_a_model_with_no_audit_facet_records_nothing(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Untracked(id=1, note="not audited"))
        await session.flush()
    assert trail.recorded == 0


async def test_a_redacted_column_records_that_it_changed_but_not_to_what(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Photo(id=5, caption="c", exif_gps="-27.4,153.0"))
        await session.flush()

    fields = (await _records(trail, "5"))[0]["fields"]
    # A marker rather than an omission: "changed, and you may not see to what"
    # and "did not change" are different facts.
    assert fields["exif_gps"] == REDACTED
    assert "-27.4" not in json.dumps(fields)


async def test_an_unattributed_write_is_refused(registry, trail, sessions):
    session = sessions(registry, trail)
    session.add(Photo(id=6, caption="anonymous", exif_gps=None))
    with pytest.raises(Unattributed, match="needs an actor"):
        await session.flush()
    assert trail.refused == 1


async def test_an_unattributed_write_never_reaches_the_database(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    session.add(Photo(id=7, caption="anonymous", exif_gps=None))
    with pytest.raises(Unattributed):
        await session.flush()

    connection = await database.acquire("write")
    try:
        count = await connection.fetchval(f'SELECT count(*) FROM "{_SCHEMA}".photos WHERE id = 7')
    finally:
        await database.release("write", connection)
    assert count == 0


async def test_an_unattributed_write_to_an_unaudited_model_is_fine(registry, trail, sessions):
    session = sessions(registry, trail)
    session.add(Untracked(id=2, note="nobody needs to sign for this"))
    await session.flush()


async def test_actors_nest_and_restore(registry, trail, sessions):
    with actor("user:41"):
        assert current_actor() == "user:41"
        with actor("job:import"):
            assert current_actor() == "job:import"
        assert current_actor() == "user:41"
    assert current_actor() is None


async def test_an_empty_actor_is_refused_at_the_point_it_is_bound(registry, trail, sessions):
    for name in ("", "   "):
        with pytest.raises(ValueError, match="non-empty name"):
            with actor(name):
                pass


async def test_a_rolled_back_write_leaves_no_record(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"), pytest.raises(RuntimeError, match="deliberate"):
        async with session.begin():
            session.add(Photo(id=8, caption="doomed", exif_gps=None))
            await session.flush()
            raise RuntimeError("deliberate")

    assert await _records(trail, "8") == []


async def test_a_flush_of_many_audited_rows_records_every_one(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        for identifier in range(100, 111):
            session.add(Photo(id=identifier, caption=f"herd {identifier}", exif_gps=None))
        await session.flush()

    assert trail.recorded == 11
    for identifier in range(100, 111):
        records = await _records(trail, str(identifier))
        assert [record["op"] for record in records] == ["insert"], identifier
        assert records[0]["fields"]["caption"] == f"herd {identifier}"


async def test_batched_updates_and_deletes_create_one_audit_record_per_row(
    registry, trail, sessions
):
    session = sessions(registry, trail)
    photos = [
        Photo(id=identifier, caption="before", exif_gps=None) for identifier in range(120, 124)
    ]
    with actor("user:41"):
        for photo in photos:
            session.add(photo)
        await session.flush()
        for photo in photos:
            photo.caption = "after"
        await session.flush()
        for photo in photos:
            session.delete(photo)
        await session.flush()

    for photo in photos:
        records = await _records(trail, str(photo.id))
        assert [record["op"] for record in records] == ["insert", "update", "delete"]
        assert records[1]["fields"] == {"caption": "after"}


async def test_a_stale_update_batch_creates_no_partial_audit_records(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    photos = [Photo(id=identifier, caption="before", exif_gps=None) for identifier in (140, 141)]
    with actor("user:41"):
        for photo in photos:
            session.add(photo)
        await session.flush()
        await _apply(database, f'DELETE FROM "{_SCHEMA}".photos WHERE id = 141')
        for photo in photos:
            photo.caption = "after"
        with pytest.raises(StaleDataError, match=r"Photo.*\(141,\)"):
            await session.flush()

    assert all(photo._orm_has_changes() for photo in photos)
    for photo in photos:
        assert [record["op"] for record in await _records(trail, str(photo.id))] == ["insert"]


async def test_a_caught_stale_update_batch_cannot_commit_matching_rows(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    photos = [Photo(id=identifier, caption="before", exif_gps=None) for identifier in (150, 151)]
    with actor("user:41"):
        for photo in photos:
            session.add(photo)
        await session.flush()
        await _apply(database, f'DELETE FROM "{_SCHEMA}".photos WHERE id = 151')
        photos[0].caption = "after"
        photos[1].exif_gps = "changed"
        async with session.begin():
            with pytest.raises(StaleDataError, match=r"Photo.*\(151,\)"):
                await session.flush()

    connection = await database.acquire("write")
    try:
        caption = await connection.fetchval(
            f'SELECT caption FROM "{_SCHEMA}".photos WHERE id = 150'
        )
    finally:
        await database.release("write", connection)
    assert caption == "before"
    assert all(photo._orm_has_changes() for photo in photos)
    for photo in photos:
        assert [record["op"] for record in await _records(trail, str(photo.id))] == ["insert"]


async def test_a_caught_stale_delete_batch_cannot_commit_matching_rows(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    photos = [Photo(id=identifier, caption="before", exif_gps=None) for identifier in (160, 161)]
    with actor("user:41"):
        for photo in photos:
            session.add(photo)
        await session.flush()
        await _apply(database, f'DELETE FROM "{_SCHEMA}".photos WHERE id = 161')
        for photo in photos:
            session.delete(photo)
        async with session.begin():
            with pytest.raises(StaleDataError, match=r"Photo.*\(161,\)"):
                await session.flush()

    connection = await database.acquire("write")
    try:
        caption = await connection.fetchval(
            f'SELECT caption FROM "{_SCHEMA}".photos WHERE id = 160'
        )
    finally:
        await database.release("write", connection)
    assert caption == "before"
    for photo in photos:
        assert [record["op"] for record in await _records(trail, str(photo.id))] == ["insert"]


async def test_a_rolled_back_flush_of_many_leaves_none_of_their_records(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"), pytest.raises(RuntimeError, match="deliberate"):
        async with session.begin():
            for identifier in range(200, 211):
                session.add(Photo(id=identifier, caption="doomed", exif_gps=None))
            await session.flush()
            raise RuntimeError("deliberate")

    for identifier in range(200, 211):
        assert await _records(trail, str(identifier)) == [], identifier


async def test_a_flush_that_fails_partway_records_nothing_and_stays_clean(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    await _apply(database, f"INSERT INTO \"{_SCHEMA}\".photos VALUES (300, 'taken', NULL)")

    with actor("user:41"), pytest.raises(Exception, match="duplicate key|unique"):
        session.add(Photo(id=299, caption="fine", exif_gps=None))
        session.add(Photo(id=300, caption="collides", exif_gps=None))
        await session.flush()

    assert await _records(trail, "299") == []
    assert await _records(trail, "300") == []

    # And the next flush carries none of it forward.
    fresh = sessions(registry, trail)
    with actor("user:41"):
        fresh.add(Photo(id=301, caption="afterwards", exif_gps=None))
        await fresh.flush()
    assert len(await _records(trail, "301")) == 1
    assert await _records(trail, "299") == []


async def test_the_trail_refuses_an_update_from_the_database_itself(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Photo(id=9, caption="permanent", exif_gps=None))
        await session.flush()

    connection = await database.acquire("write")
    try:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute(
                f"UPDATE \"{_SCHEMA}\".audit_records SET actor = 'somebody else'"
            )
        with pytest.raises(Exception, match="append-only"):
            await connection.execute(f'DELETE FROM "{_SCHEMA}".audit_records')
        with pytest.raises(Exception, match="append-only"):
            await connection.execute(f'TRUNCATE "{_SCHEMA}".audit_records')
    finally:
        await database.release("write", connection)


async def test_forgetting_a_subject_removes_only_that_subject(registry, trail, sessions):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Photo(id=10, caption="mine", exif_gps=None))
        session.add(Photo(id=11, caption="theirs", exif_gps=None))
        await session.flush()

    await trail.forget(_PHOTOS, "10")
    assert await _records(trail, "10") == []
    assert len(await _records(trail, "11")) == 1


async def test_the_erasure_permission_does_not_outlive_its_transaction(
    registry, trail, database, sessions
):
    session = sessions(registry, trail)
    with actor("user:41"):
        session.add(Photo(id=13, caption="stays", exif_gps=None))
        await session.flush()

    await trail.forget(_PHOTOS, "999")  # a subject with no records; still opens
    connection = await database.acquire("write")
    try:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute(f'DELETE FROM "{_SCHEMA}".audit_records')
    finally:
        await database.release("write", connection)
    assert len(await _records(trail, "13")) == 1


async def test_neutering_the_hook_makes_these_tests_fail(registry, trail, sessions):
    session = Session(registry, "write", audit=None)
    try:
        with actor("user:41"):
            session.add(Photo(id=12, caption="unrecorded", exif_gps=None))
            await session.flush()
    finally:
        await session.close()

    assert await _records(trail, "12") == []
    assert trail.recorded == 0
