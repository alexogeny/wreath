from __future__ import annotations

import asyncio
import contextvars
import gc
import importlib
import weakref
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest

from wreath._devtools import query_probe
from wreath._pgdriver import Connection as PureConnection

from .test_connection import FakePostgres

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built"),
]


@pytest.fixture
async def database() -> AsyncIterator[tuple[FakePostgres, str]]:
    server = FakePostgres(fragment=True)
    dsn = await server.start_tcp()
    try:
        yield server, dsn
    finally:
        await server.close()


async def test_native_map_allocates_no_asyncio_task_per_operation(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    loop = asyncio.get_running_loop()
    created: list[Coroutine[Any, Any, Any]] = []

    def task_factory(
        task_loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        *,
        context: contextvars.Context | None = None,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        created.append(coroutine)
        return asyncio.Task(coroutine, loop=task_loop, context=context, name=name)

    try:
        await connection.fetchval("select $1::int4", 0)
        loop.set_task_factory(task_factory)
        results = await connection.map(
            "fetchval", "select $1::int4", ((value,) for value in range(20))
        )
    finally:
        loop.set_task_factory(None)
        await connection.close()

    assert results == [42] * 20
    assert created == []


async def test_native_map_retains_only_one_submission_window(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    connection = await native.connect(dsn)
    produced: list[int] = []

    def arguments() -> Any:
        for value in range(12):
            produced.append(value)
            yield (value,)

    try:
        mapping = asyncio.create_task(
            connection.map(
                "fetchval", "select $1::int4", arguments(), max_in_flight=3
            )
        )
        await server.flight_received.wait()
        await asyncio.sleep(0)
        assert produced == [0, 1, 2]
        server.query_gate.set()
        assert await mapping == [42] * 12
    finally:
        server.query_gate.set()
        await connection.close()


async def test_native_map_conversion_failure_submits_none_of_its_window(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    connection = await native.connect(dsn)

    class BadArguments:
        def __iter__(self) -> Any:
            raise RuntimeError("argument conversion failed")

    try:
        before = len(server.flights)
        with pytest.raises(RuntimeError, match="argument conversion failed"):
            await connection.map(
                "fetchval", "select $1::int4", [(1,), BadArguments(), (3,)]
            )
        await asyncio.sleep(0)
        assert len(server.flights) == before
        assert await connection.fetchval("select 42") == 42
    finally:
        await connection.close()


async def test_cancelling_native_map_cleans_every_operation(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    connection = await native.connect(dsn)
    try:
        mapping = asyncio.create_task(
            connection.map(
                "fetchval", "select $1::int4", [(value,) for value in range(8)]
            )
        )
        await server.flight_received.wait()
        mapping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await mapping
        server.query_gate.set()
        await asyncio.wait_for(connection._idle_event.wait(), timeout=1)
        assert await connection.fetchval("select 42") == 42
    finally:
        server.query_gate.set()
        await connection.close()


async def test_native_map_first_database_error_cleans_later_operations(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        with pytest.raises(native.PostgresError, match="syntax error"):
            await connection.map(
                "fetchval", "select broken", [(value,) for value in range(8)]
            )
        await asyncio.wait_for(connection._idle_event.wait(), timeout=1)
        assert await connection.fetchval("select 42") == 42
    finally:
        await connection.close()


async def test_native_map_falls_back_when_window_exceeds_pipeline_capacity(
    database: tuple[FakePostgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    monkeypatch.setattr(native.Connection, "max_queued_operations", 2)
    try:
        with pytest.raises(native.PipelineFullError):
            await connection.map(
                "fetchval",
                "select $1::int4",
                [(value,) for value in range(4)],
                max_in_flight=4,
            )
        await asyncio.wait_for(connection._idle_event.wait(), timeout=1)
        assert await connection.fetchval("select 42") == 42
    finally:
        await connection.close()


async def test_overridden_submit_keeps_native_map_fault_seam(
    database: tuple[FakePostgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated submit failure")

    monkeypatch.setattr(native.Connection, "_submit", explode)
    try:
        with pytest.raises(RuntimeError, match="simulated submit failure"):
            await connection.map("fetchval", "select 42", [()])
    finally:
        await connection.close()


async def test_native_map_transaction_collapses_window_to_one(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    connection = await native.connect(dsn)
    produced: list[int] = []

    def arguments() -> Any:
        for value in range(4):
            produced.append(value)
            yield (value,)

    try:
        async with connection.transaction():
            server.flight_received.clear()
            server.query_gate = asyncio.Event()
            mapping = asyncio.create_task(
                connection.map(
                    method="fetchval",
                    statement="select $1::int4",
                    argument_sets=arguments(),
                    max_in_flight=4,
                )
            )
            await server.flight_received.wait()
            await asyncio.sleep(0)
            assert produced == [0]
            server.query_gate.set()
            assert await mapping == [42] * 4
    finally:
        if server.query_gate is not None:
            server.query_gate.set()
        await connection.close()


async def test_query_probe_ungrafts_map_with_the_pipeline() -> None:
    assert "map" in query_probe.GRAFTED


async def test_transport_failure_resolves_one_shared_map_completion_once(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    successful_wake = asyncio.Event()

    def arguments() -> Any:
        for value in range(500):
            if value == 32:
                successful_wake.set()
            yield (value,)

    try:
        mapping = asyncio.create_task(
            connection.map(
                "fetchval",
                "select $1::int4",
                arguments(),
                max_in_flight=32,
            )
        )
        await successful_wake.wait()
        assert not mapping.done()
        assert connection._waiting_live or connection._emitted

        connection._fail_connection(native.OperationalError("forced transport failure"))

        with pytest.raises(native.OperationalError, match="forced transport failure"):
            await asyncio.wait_for(mapping, timeout=1)
        assert connection.closed
    finally:
        await connection.close()


async def test_overridden_publisher_uses_per_operation_futures(
    database: tuple[FakePostgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    monkeypatch.setattr(
        native.Connection, "_publish_completed", PureConnection._publish_completed
    )
    try:
        results = await connection.map(
            "fetchval", "select $1::int4", [(value,) for value in range(4)]
        )
    finally:
        await connection.close()

    assert results == [42] * 4


async def test_native_map_capacity_ignores_cancelled_waiting_tombstones(
    database: tuple[FakePostgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    connection = await native.connect(dsn)
    monkeypatch.setattr(native.Connection, "max_queued_operations", 4)
    monkeypatch.setattr(native.Connection, "max_emitted_operations", 1)
    loop = asyncio.get_running_loop()
    created: list[Coroutine[Any, Any, Any]] = []

    def task_factory(
        task_loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, Any],
        *,
        context: contextvars.Context | None = None,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        created.append(coroutine)
        return asyncio.Task(coroutine, loop=task_loop, context=context, name=name)

    active = asyncio.create_task(connection.fetchval("select $1::int4", 0))
    waiting: list[asyncio.Task[Any]] = []
    try:
        await server.flight_received.wait()
        waiting = [
            asyncio.create_task(connection.fetchval("select $1::int4", value))
            for value in range(1, 4)
        ]
        await asyncio.sleep(0)
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        assert connection._waiting_live == 0
        assert len(connection._waiting) == 3

        loop.set_task_factory(task_factory)
        loop.call_soon(server.query_gate.set)
        results = await connection.map(
            "fetchval", "select $1::int4", [(4,), (5,), (6,)], max_in_flight=3
        )
    finally:
        loop.set_task_factory(None)
        server.query_gate.set()
        await asyncio.gather(active, *waiting, return_exceptions=True)
        await connection.close()

    assert results == [42, 42, 42]
    assert created == []


async def test_native_map_all_result_modes_and_empty_input(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        assert await connection.map("execute", "update widget set v=1", [()]) == [
            "UPDATE 1"
        ]
        rows = await connection.map("fetch", "select 42", [()])
        assert len(rows) == 1
        assert len(rows[0]) == 1
        assert rows[0][0][0] == 42
        row = await connection.map("fetchrow", "select 42", [()])
        assert len(row) == 1
        assert row[0][0] == 42
        assert await connection.map("fetchval", "select 42", [()]) == [42]
        for method in ("execute", "fetch", "fetchrow", "fetchval"):
            assert await connection.map(method, "select 42", []) == []
    finally:
        await connection.close()


async def test_native_map_start_and_conversion_failures_are_one_shot(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)

    class BadStatement:
        @property
        def sql(self) -> str:
            raise RuntimeError("statement conversion failed")

    class BadArguments:
        def __iter__(self) -> Any:
            raise RuntimeError("argument conversion failed")

    try:
        for mapping, message in (
            (connection.map("fetchval", BadStatement(), [()]), "statement conversion"),
            (
                connection.map("fetchval", "select 42", [BadArguments()]),
                "argument conversion",
            ),
        ):
            with pytest.raises(RuntimeError, match=message):
                await mapping
            with pytest.raises(RuntimeError, match="already consumed"):
                await mapping
    finally:
        await connection.close()


async def test_consumed_native_map_releases_argument_source(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)

    class Arguments:
        def __iter__(self) -> Any:
            yield (1,)

    arguments = Arguments()
    reference = weakref.ref(arguments)
    mapping = connection.map("fetchval", "select $1::int4", arguments)
    try:
        assert await mapping == [42]
        del arguments
        gc.collect()
        assert reference() is None
    finally:
        await connection.close()


async def test_closing_after_map_wake_fails_remaining_group_once(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    successful_wake = asyncio.Event()

    def arguments() -> Any:
        for value in range(500):
            if value == 32:
                successful_wake.set()
            yield (value,)

    mapping = asyncio.create_task(
        connection.map(
            "fetchval",
            "select $1::int4",
            arguments(),
            max_in_flight=32,
        )
    )
    await successful_wake.wait()
    assert not mapping.done()

    await connection.close()

    with pytest.raises(native.InterfaceError, match="connection is closed"):
        await asyncio.wait_for(mapping, timeout=1)
