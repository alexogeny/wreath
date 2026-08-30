from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.orm import Mapped, Model, Registry, column
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
            f'CREATE TABLE "{_SCHEMA}"."events" '
            "(id bigint PRIMARY KEY, label text NOT NULL)"
        )
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."generated_events" '
            "(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, label text NOT NULL)"
        )
    finally:
        await database.release("write", connection)
    built = Registry(database, [BatchEvent, GeneratedEvent], validate_schema="off")
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
