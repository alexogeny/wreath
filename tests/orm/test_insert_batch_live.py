from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.orm import Mapped, Model, Registry, column
from wreath.orm.errors import ORMError, StaleDataError
from wreath.orm.model import PERSISTENT
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text
from wreath.postgres import Database, PoolConfig, PostgresError

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(
        not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
        reason="set WREATH_TEST_POSTGRES_DSN for live insert batching tests",
    ),
]

_SCHEMA = f"wreath_insert_batch_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"


class BatchEvent(Model, table="events", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


class GeneratedEvent(Model, table="generated_events", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


class LiveMembership(Model, table="memberships", schema=_SCHEMA):
    org_id: Mapped[int] = column(Int64, primary_key=True)
    user_id: Mapped[int] = column(Int64, primary_key=True)
    role: Mapped[str] = column(Text)


@pytest.fixture
async def registry() -> Any:
    database = Database(
        "insert-batch-live",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={"write": PoolConfig(min_size=1, max_size=2)},
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."events" (id bigint PRIMARY KEY, label text NOT NULL)'
        )
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."generated_events" '
            "(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, label text NOT NULL)"
        )
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."memberships" '
            "(org_id bigint NOT NULL, user_id bigint NOT NULL, role text NOT NULL, "
            "PRIMARY KEY (org_id, user_id))"
        )
    finally:
        await database.release("write", connection)
    built = Registry(
        database,
        [BatchEvent, GeneratedEvent, LiveMembership],
        validate_schema="off",
    )
    try:
        yield built
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
            await database.stop()


async def test_complete_same_shape_rows_round_trip_in_input_order(registry: Registry) -> None:
    session = Session(registry, "write")
    events = [BatchEvent(id=index, label=f"event-{index}") for index in range(1, 5)]
    try:
        for event in events:
            session.add(event)
        await session.flush()
        assert all(event._orm_state == PERSISTENT for event in events)
        fetched = sorted(await session.fetch(BatchEvent.select()), key=lambda event: event.id)
    finally:
        await session.close()

    assert [(event.id, event.label) for event in fetched] == [
        (index, f"event-{index}") for index in range(1, 5)
    ]


async def test_server_generated_keys_keep_their_input_correspondence(registry: Registry) -> None:
    session = Session(registry, "write")
    events = [GeneratedEvent(label="first"), GeneratedEvent(label="second")]
    try:
        for event in events:
            session.add(event)
        await session.flush()
    finally:
        await session.close()

    assert [event.label for event in events] == ["first", "second"]
    assert events[0].id < events[1].id


async def test_a_batch_constraint_error_rolls_back_every_row(registry: Registry) -> None:
    session = Session(registry, "write")
    events = [BatchEvent(id=1, label="first"), BatchEvent(id=1, label="duplicate")]
    for event in events:
        session.add(event)

    with pytest.raises(PostgresError) as raised:
        await session.flush()

    assert raised.value.sqlstate == "23505"
    assert session._new == events
    assert all(event._orm_state != PERSISTENT for event in events)
    connection = await registry.database.acquire("write")
    try:
        count = await connection.fetchval(f'SELECT count(*) FROM "{_SCHEMA}"."events"')
    finally:
        await registry.database.release("write", connection)
        await session.close()
    assert count == 0


async def test_a_caught_insert_postflight_failure_rolls_back_every_row(
    registry: Registry,
) -> None:
    connection = await registry.database.acquire("write")
    try:
        await connection.execute(
            f'CREATE FUNCTION "{_SCHEMA}".skip_generated_event() RETURNS trigger AS '
            "$body$ BEGIN IF NEW.label = 'skip' THEN RETURN NULL; END IF; "
            "RETURN NEW; END; $body$ LANGUAGE plpgsql"
        )
        await connection.execute(
            f"CREATE TRIGGER skip_generated_event BEFORE INSERT ON "
            f'"{_SCHEMA}".generated_events FOR EACH ROW EXECUTE FUNCTION '
            f'"{_SCHEMA}".skip_generated_event()'
        )
    finally:
        await registry.database.release("write", connection)

    session = Session(registry, "write")
    events = [GeneratedEvent(label="keep"), GeneratedEvent(label="skip")]
    try:
        async with session.begin():
            for event in events:
                session.add(event)
            with pytest.raises(ORMError, match=r"returned 1 rows.*batch of 2"):
                await session.flush()

        connection = await registry.database.acquire("write")
        try:
            count = await connection.fetchval(
                f'SELECT count(*) FROM "{_SCHEMA}".generated_events '
                "WHERE label IN ('keep', 'skip')"
            )
        finally:
            await registry.database.release("write", connection)
        assert count == 0
        assert session._new == events
        assert all(event._orm_state != PERSISTENT for event in events)
    finally:
        await session.close()


async def test_update_and_delete_batches_round_trip_through_postgresql(
    registry: Registry,
) -> None:
    session = Session(registry, "write")
    events = [BatchEvent(id=index, label=f"before-{index}") for index in range(10, 14)]
    try:
        for event in events:
            session.add(event)
        await session.flush()
        for event in events:
            event.label = f"after-{event.id}"
        await session.flush()
        session.delete(events[0])
        session.delete(events[-1])
        await session.flush()

        rows = await session.raw(f'SELECT id, label FROM "{_SCHEMA}"."events" ORDER BY id').fetch()
    finally:
        await session.close()

    assert [tuple(row) for row in rows] == [(11, "after-11"), (12, "after-12")]


async def test_composite_key_update_and_delete_batches_round_trip(
    registry: Registry,
) -> None:
    session = Session(registry, "write")
    memberships = [LiveMembership(org_id=1, user_id=index, role="member") for index in range(1, 4)]
    try:
        for membership in memberships:
            session.add(membership)
        await session.flush()
        for membership in memberships:
            membership.role = "admin"
        await session.flush()
        for membership in memberships:
            session.delete(membership)
        await session.flush()
        count = await session.raw(f'SELECT count(*) FROM "{_SCHEMA}"."memberships"').fetchval()
    finally:
        await session.close()

    assert count == 0


async def test_a_short_update_returning_result_preserves_every_dirty_instance(
    registry: Registry,
) -> None:
    session = Session(registry, "write")
    events = [BatchEvent(id=index, label="before") for index in (20, 21)]
    try:
        for event in events:
            session.add(event)
        await session.flush()
        for event in events:
            event.label = "after"

        connection = await registry.database.acquire("write")
        try:
            await connection.execute(f'DELETE FROM "{_SCHEMA}"."events" WHERE id = 21')
        finally:
            await registry.database.release("write", connection)

        with pytest.raises(StaleDataError, match=r"BatchEvent.*\(21,\)"):
            await session.flush()
        assert all(event._orm_has_changes() for event in events)
    finally:
        await session.close()


async def test_cached_model_reads_keep_data_rows_on_the_native_receive_path(
    registry: Registry,
) -> None:
    session = Session(registry, "write")
    memberships = [LiveMembership(org_id=9, user_id=index, role="member") for index in range(1, 4)]
    try:
        for membership in memberships:
            session.add(membership)
        await session.flush()
        await session.fetch(LiveMembership.select())
        connection = session.connection
        before = connection._reader._receive_stats()
        rows = await session.fetch(LiveMembership.select())
        after = connection._reader._receive_stats()
    finally:
        await session.close()

    assert rows == memberships
    assert after["direct_data_rows"] - before["direct_data_rows"] == 3
