from __future__ import annotations

from typing import Annotated, Any

import pytest
from _pgfidelity import check_for

from wreath import Response, Wreath
from wreath.background import BackgroundTask
from wreath.postgres import Connection, FromDatabase
from wreath.response import StreamingResponse
from wreath.testing import TestClient


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.read_only = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        check_for(self, sql, args)
        if sql == "SET default_transaction_read_only = on":
            self.read_only = True
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return 1

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_starts_with_lifespan_and_binds_read_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeConnection] = []

    async def connect(dsn: str) -> FakeConnection:
        connection = FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    database = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"read": {"min_size": 1, "max_size": 1}},
    )

    @app.get("/health")
    async def health(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> dict[str, object]:
        return {"value": await conn.fetchval("select 1")}

    async with TestClient(app) as client:
        assert database.started
        response = await client.request("GET", "/health")
        assert response.status == 200
        assert response.json() == {"value": 1}
        assert database.pool("read").borrowed == 0

    assert connections[0].closed


def test_multiple_databases_require_from_database() -> None:
    app = Wreath()
    app.postgres("one", dsn="postgresql://one/app")
    app.postgres("two", dsn="postgresql://two/app")

    @app.get("/ambiguous")
    async def ambiguous(request: Any, conn: Connection) -> None:
        pass

    with pytest.raises(TypeError, match="FromDatabase"):
        app._compile_routes()


def test_security_workload_cannot_be_injected_into_handler() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://primary/app")

    @app.get("/unsafe")
    async def unsafe(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="security_read")],
    ) -> None:
        pass

    with pytest.raises(TypeError, match="security_read"):
        app._compile_routes()


@pytest.mark.asyncio
async def test_streaming_response_retains_connection_until_body_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()

    async def connect(dsn: str) -> FakeConnection:
        return connection

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    database = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"read": {"min_size": 1, "max_size": 1}},
    )

    @app.get("/stream")
    async def stream(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> StreamingResponse:
        async def body():
            assert database.pool("read").borrowed == 1
            yield b"chunk"

        return StreamingResponse(body())

    async with TestClient(app) as client:
        response = await client.request("GET", "/stream")
        assert response.body == b"chunk"
        assert database.pool("read").borrowed == 0


@pytest.mark.asyncio
async def test_handler_connection_released_before_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(dsn: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    database = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"read": {"min_size": 1, "max_size": 1}},
    )
    observed: list[int] = []

    async def task() -> None:
        observed.append(database.pool("read").borrowed)

    @app.get("/audit")
    async def audit(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> Response:
        await conn.fetchval("select 1")
        return Response(b"ok", background=BackgroundTask(task))

    async with TestClient(app) as client:
        response = await client.request("GET", "/audit")
        assert response.body == b"ok"
    # The borrowed connection was already back in the pool when the task ran.
    assert observed == [0]


@pytest.mark.asyncio
async def test_streaming_connection_released_before_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(dsn: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    database = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"read": {"min_size": 1, "max_size": 1}},
    )
    observed: list[int] = []

    async def task() -> None:
        observed.append(database.pool("read").borrowed)

    @app.get("/stream")
    async def stream(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> StreamingResponse:
        async def body():
            yield b"chunk"

        return StreamingResponse(body(), background=BackgroundTask(task))

    async with TestClient(app) as client:
        response = await client.request("GET", "/stream")
        assert response.body == b"chunk"
    # Stream cleanup returns the connection before the user task runs.
    assert observed == [0]


@pytest.mark.asyncio
async def test_streaming_connection_released_when_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(dsn: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    database = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"read": {"min_size": 1, "max_size": 1}},
    )

    @app.get("/stream")
    async def stream(
        request: Any,
        conn: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> StreamingResponse:
        await conn.fetchval("select 1")

        async def body():
            yield b"chunk"
            raise RuntimeError("stream failed")

        return StreamingResponse(body())

    async with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="stream failed"):
            await client.request("GET", "/stream")
        assert database.pool("read").borrowed == 0
