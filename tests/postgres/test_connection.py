from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import re
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from wreath import _pgdriver as pure_postgres

native_postgres: Any = None
try:
    native_postgres = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

POSTGRES_BACKENDS = [pure_postgres]
if native_postgres is not None:
    POSTGRES_BACKENDS.append(native_postgres)


@pytest.fixture(params=POSTGRES_BACKENDS, ids=lambda backend: backend._implementation)
def postgres(request: pytest.FixtureRequest) -> Any:
    return request.param


def _message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _row_description() -> bytes:
    # name, table oid, column, type oid, type size, modifier, format (text)
    field = b"value\x00" + struct.pack("!IhIhih", 0, 0, 23, 4, -1, 0)
    return _message(b"T", struct.pack("!H", 1) + field)


def _data_row(value: bytes, *, binary: bool = False) -> bytes:
    payload = struct.pack("!H", 1) + struct.pack("!i", len(value)) + value
    return _message(b"D", payload)


def _error(message: str, code: str = "42601") -> bytes:
    return _message(
        b"E",
        b"SERROR\x00" + b"C" + code.encode() + b"\x00M" + message.encode() + b"\x00\x00",
    )


class FakePostgres:
    def __init__(self, *, auth: str = "trust", fragment: bool = True) -> None:
        self.auth = auth
        self.fragment = fragment
        self.flights: list[list[bytes]] = []
        self.password: bytes | None = None
        self.server: asyncio.AbstractServer | None = None
        self.handlers: set[asyncio.Task[None]] = set()
        self.port = 0
        self.path: Path | None = None
        self.query_gate: asyncio.Event | None = None
        self.cancel_event = asyncio.Event()
        self.cancel_response_sent = asyncio.Event()
        self.flight_received = asyncio.Event()
        # SQL text of every executed operation, in the order the server ran it.
        self.executed_sql: list[str] = []

    async def start_tcp(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket = self.server.sockets[0]
        self.port = int(socket.getsockname()[1])
        return f"postgresql://wreath:secret@127.0.0.1:{self.port}/wreath"

    async def start_unix(self, path: Path) -> str:
        self.path = path
        self.server = await asyncio.start_unix_server(self._handle, path)
        return f"postgresql://wreath:secret@/wreath?host={quote(str(path.parent))}&port={path.name}"

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        current = asyncio.current_task()
        handlers = tuple(task for task in self.handlers if task is not current)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if self.path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        if self.fragment:
            for byte in data:
                writer.write(bytes((byte,)))
                await writer.drain()
        else:
            writer.write(data)
            await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.handlers.add(task)
        try:
            startup_len = struct.unpack("!I", await reader.readexactly(4))[0]
            startup_payload = await reader.readexactly(startup_len - 4)
            if startup_len == 16 and struct.unpack_from("!I", startup_payload)[0] == 80877102:
                self.cancel_event.set()
                return
            if self.auth == "cleartext":
                await self._send(writer, _message(b"R", struct.pack("!I", 3)))
                assert await reader.readexactly(1) == b"p"
                length = struct.unpack("!I", await reader.readexactly(4))[0]
                self.password = (await reader.readexactly(length - 4)).removesuffix(b"\x00")
            await self._send(writer, _message(b"R", struct.pack("!I", 0)))
            await self._send(writer, _message(b"S", b"server_version\x0017.0\x00"))
            await self._send(writer, _message(b"K", struct.pack("!II", 1, 2)))
            await self._send(writer, _message(b"Z", b"I"))

            statements: dict[bytes, str] = {}
            transaction_status = b"I"
            while True:
                kind = await reader.readexactly(1)
                if kind == b"X":
                    length = struct.unpack("!I", await reader.readexactly(4))[0]
                    await reader.readexactly(length - 4)
                    return

                flight: list[tuple[bytes, bytes]] = []
                while True:
                    length = struct.unpack("!I", await reader.readexactly(4))[0]
                    payload = await reader.readexactly(length - 4)
                    flight.append((kind, payload))
                    if kind == b"S":
                        break
                    kind = await reader.readexactly(1)
                self.flights.append([item[0] for item in flight])
                self.flight_received.set()

                parse = next((payload for kind, payload in flight if kind == b"P"), None)
                sql = ""
                parameter_count = 0
                if parse is not None:
                    statement_name, _, tail = parse.partition(b"\x00")
                    query, _, parameter_data = tail.partition(b"\x00")
                    sql = query.decode()
                    statements[statement_name] = sql
                    parameter_count = struct.unpack_from("!H", parameter_data)[0]
                else:
                    bind = next(payload for kind, payload in flight if kind == b"B")
                    _, _, bind_tail = bind.partition(b"\x00")
                    statement_name = bind_tail.partition(b"\x00")[0]
                    sql = statements[statement_name]
                self.executed_sql.append(sql)

                if "broken" in sql or "constraint" in sql:
                    if "constraint" in sql:
                        error = _error("duplicate key", "23505")
                    else:
                        error = _error("syntax error")
                    await self._send(writer, error)
                    await self._send(writer, _message(b"Z", b"I"))
                    continue

                cold = parse is not None
                returns_rows = sql.lstrip().lower().startswith("select")
                if self.query_gate is not None and not self.query_gate.is_set():
                    gate = asyncio.create_task(self.query_gate.wait())
                    cancelled = asyncio.create_task(self.cancel_event.wait())
                    done, pending = await asyncio.wait(
                        {gate, cancelled}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    if cancelled in done:
                        self.cancel_event.clear()
                        await self._send(writer, _error("canceling statement", "57014"))
                        await self._send(writer, _message(b"Z", b"I"))
                        self.cancel_response_sent.set()
                        continue
                if cold:
                    await self._send(writer, _message(b"1"))
                    parameter_description = struct.pack("!H", parameter_count)
                    parameter_description += struct.pack("!I", 23) * parameter_count
                    await self._send(writer, _message(b"t", parameter_description))
                    await self._send(writer, _row_description() if returns_rows else _message(b"n"))
                await self._send(writer, _message(b"2"))
                describes_portal = any(
                    kind == b"D" and payload.startswith(b"P") for kind, payload in flight
                )
                if describes_portal:
                    await self._send(writer, _row_description() if returns_rows else _message(b"n"))
                if returns_rows:
                    series = re.search(
                        r"generate_series\((-?\d+),\s*(-?\d+)\)", sql, re.IGNORECASE
                    )
                    match = re.match(r"\s*select\s+(-?\d+)", sql, re.IGNORECASE)
                    if series is not None:
                        values = range(int(series.group(1)), int(series.group(2)) + 1)
                    else:
                        values = (int(match.group(1)) if match is not None else 42,)
                    for integer in values:
                        value = struct.pack("!i", integer) if not cold else str(integer).encode()
                        await self._send(writer, _data_row(value, binary=not cold))
                    await self._send(writer, _message(b"C", b"SELECT 1\x00"))
                else:
                    await self._send(writer, _message(b"C", b"UPDATE 1\x00"))
                command = sql.lstrip().upper()
                if command.startswith(("BEGIN", "START")):
                    transaction_status = b"T"
                elif command.startswith(("COMMIT", "ROLLBACK")):
                    transaction_status = b"I"
                await self._send(writer, _message(b"Z", transaction_status))
        except asyncio.IncompleteReadError:
            pass
        finally:
            if task is not None:
                self.handlers.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


@pytest.fixture
async def database() -> AsyncIterator[tuple[FakePostgres, str]]:
    server = FakePostgres()
    dsn = await server.start_tcp()
    try:
        yield server, dsn
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_connect_and_close(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    assert not conn.closed
    await conn.close()
    assert conn.closed
    await conn.close()


@pytest.mark.asyncio
async def test_cold_then_cached_query_flights(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        first = await conn.fetchval("select $1::int4", 42)
        second = await conn.fetchval("select $1::int4", 42)
    finally:
        await conn.close()

    assert first == second == 42
    assert server.flights == [
        [b"P", b"D", b"B", b"D", b"E", b"S"],
        [b"B", b"E", b"S"],
    ]


@pytest.mark.asyncio
async def test_result_modes_and_record_access(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        row = await conn.fetchrow("select 42")
        rows = await conn.fetch("select 42")
        value = await conn.fetchval("select 42")
        tag = await conn.execute("update things set value = 1")
    finally:
        await conn.close()

    assert isinstance(row, postgres.Record)
    assert row[0] == row["value"] == 42
    assert list(row) == [42]
    assert len(rows) == 1 and isinstance(rows[0], postgres.Record)
    assert value == 42
    assert tag == "UPDATE 1"


@pytest.mark.asyncio
async def test_cached_execute_without_result_columns(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    """Re-executing a statement that returns no rows must use the cached plan."""
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        first = await conn.execute("update things set value = 2")
        receive_stats = getattr(conn._reader, "_receive_stats", None)
        before = receive_stats() if receive_stats is not None else None
        second = await conn.execute("update things set value = 2")
        after = receive_stats() if receive_stats is not None else None
    finally:
        await conn.close()

    assert first == second == "UPDATE 1"
    if before is not None and after is not None:
        assert after["queued_messages"] - before["queued_messages"] == 1
    assert server.flights == [
        [b"P", b"D", b"B", b"E", b"S"],
        [b"B", b"E", b"S"],
    ]


@pytest.mark.asyncio
async def test_cached_fetchval_completes_from_native_receive_slab(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    """The hot scalar path materializes only its value, not a Z message."""
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        assert await conn.fetchval("select 42") == 42
        receive_stats = getattr(conn._reader, "_receive_stats", None)
        before = receive_stats() if receive_stats is not None else None
        assert await conn.fetchval("select 42") == 42
        after = receive_stats() if receive_stats is not None else None
    finally:
        await conn.close()

    if before is not None and after is not None:
        assert after["direct_completions"] - before["direct_completions"] == 1
        assert after["queued_messages"] - before["queued_messages"] == 0


@pytest.mark.asyncio
async def test_plan_created_by_execute_is_reused_by_fetch(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        tag = await conn.execute("select 42")
        rows = await conn.fetch("select 42")
    finally:
        await conn.close()

    assert tag == "SELECT 1"
    assert [row["value"] for row in rows] == [42]
    assert server.flights == [
        [b"P", b"D", b"B", b"E", b"S"],
        [b"B", b"E", b"S"],
    ]


@pytest.mark.asyncio
async def test_fetchval_does_not_allocate_record_or_result_list(
    postgres: Any,
    database: tuple[FakePostgres, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)

    class ForbiddenRecord:
        def __init__(self, *args: object) -> None:
            raise AssertionError("fetchval allocated a Record")

    monkeypatch.setattr(postgres, "Record", ForbiddenRecord)
    allocation_count = getattr(postgres, "_record_allocation_count", None)
    before = allocation_count() if allocation_count is not None else None
    try:
        assert await conn.fetchval("select 42") == 42
        if allocation_count is not None:
            assert allocation_count() == before
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_error_is_consumed_through_ready_for_query_and_connection_reused(
    postgres: Any,
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        with pytest.raises(postgres.PostgresError) as error:
            await conn.execute("broken sql")
        assert error.value.sqlstate == "42601"
        assert await conn.fetchval("select 42") == 42
        with pytest.raises(postgres.PostgresError) as constraint:
            await conn.execute("constraint violation")
        assert constraint.value.sqlstate == "23505"
        assert await conn.fetchval("select 42") == 42
    finally:
        await conn.close()


def test_a_declared_sqlstate_survives_construction(postgres: Any) -> None:
    """A subclass that names its condition keeps it.

    `PostgresError.__init__` used to assign `self.sqlstate` unconditionally, so
    a class-level declaration was overwritten with `None` by the very
    constructor that was supposed to leave it alone -- and a caller classifying
    by sqlstate, which is the correct way to tell a lock timeout from a
    deadlock, read `None` for exactly the classes that had bothered to say.
    Both spellings are pinned here because both are in use: the fakes in
    `tests/passes/fakes.py` pass it up through `super().__init__` (the
    workaround, which had to stay working) and the declaration is what should
    have worked all along.
    """

    class Declared(postgres.PostgresError):
        sqlstate = "23514"

    class PassedUp(postgres.PostgresError):
        def __init__(self, message: str) -> None:
            super().__init__(message, sqlstate="42710")

    assert Declared("check violated").sqlstate == "23514"
    assert PassedUp("already there").sqlstate == "42710"
    # An explicit code still wins over the declaration, and does not leak back
    # onto the class for the next instance.
    assert Declared("elsewhere", sqlstate="40P01").sqlstate == "40P01"
    assert Declared("check violated").sqlstate == "23514"
    # A class that names nothing still answers, rather than raising.
    assert postgres.PostgresError("plain").sqlstate is None
    assert postgres.InterfaceError("closed").sqlstate is None


def test_the_error_classes_are_one_set_across_both_backends() -> None:
    """The native backend re-exports these classes rather than redefining them.

    Which is why the fix above needs no C counterpart: there is no second
    constructor to keep in step. If that ever changes, this fails.
    """
    if native_postgres is None:
        pytest.skip("the native PostgreSQL extension is not built")
    for name in (
        "PostgresError",
        "InterfaceError",
        "OperationalError",
        "ProtocolError",
        "PipelineFullError",
    ):
        assert getattr(native_postgres, name) is getattr(pure_postgres, name), name


@pytest.mark.asyncio
async def test_cleartext_password_authentication(postgres: Any) -> None:
    server = FakePostgres(auth="cleartext")
    dsn = await server.start_tcp()
    try:
        conn = await postgres.connect(dsn)
        await conn.close()
        assert server.password == b"secret"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_unix_socket_connection(postgres: Any, tmp_path: Path) -> None:
    server = FakePostgres()
    socket_path = tmp_path / ".s.PGSQL.55432"
    dsn = await server.start_unix(socket_path)
    try:
        conn = await postgres.connect(dsn)
        assert await conn.fetchval("select 42") == 42
        await conn.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_connection_serializes_concurrent_operations(
    postgres: Any,
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        values = await asyncio.gather(
            conn.fetchval("select 42"),
            conn.fetchval("select 42"),
        )
    finally:
        await conn.close()
    assert values == [42, 42]


@pytest.mark.asyncio
async def test_connection_plan_cache_evicts_oldest_and_closes_statement(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    conn = await pure_postgres.connect(dsn, statement_cache_size=2)
    try:
        await conn.fetchval("select $1::int4", 1)     # shape A: cold
        await conn.execute("update t set a = $1", 2)  # shape B: cold
        await conn.execute("update t set b = $1", 3)  # shape C: cold -> evicts A
        assert conn.prepared_plan_count == 2
        # A cache hit now carries the Close of evicted statement A on the wire.
        await conn.execute("update t set a = $1", 4)  # shape B: hit
        # The evicted shape is cold again (its plan was dropped).
        await conn.fetchval("select $1::int4", 5)     # shape A: cold again
    finally:
        await conn.close()

    assert conn.prepared_plan_count <= 2
    # Some flight carried a frontend Close ('C') for the evicted statement.
    assert any(b"C" in flight for flight in server.flights), server.flights


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_connection_plan_cache_evicts_to_its_byte_budget(
    database: tuple[FakePostgres, str], backend: Any,
) -> None:
    _, dsn = database
    conn = await backend.connect(dsn, statement_cache_bytes=1)
    try:
        await conn.fetchval("select $1::int4", 1)
        assert conn.prepared_plan_count == 0
    finally:
        await conn.close()
