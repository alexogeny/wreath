"""A tiny in-process PostgreSQL wire stand-in for the workload verifier.

It speaks just enough of the v3 protocol to let the real Wreath driver prepare and
run extended-protocol operations, and it records the SQL of every executed
operation plus the Sync-delimited "flights" so the verifier can assert wire
semantics (one Sync per input, read-before-write ordering) without a live
server. It is a verification aid, not a benchmark target.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct


def _message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


# Column type oids: id is int4, message is text.
_COLUMN_OIDS = {"id": 23, "value": 23, "message": 25}


def _row_description(columns: tuple[str, ...]) -> bytes:
    payload = struct.pack("!H", len(columns))
    for name in columns:
        payload += name.encode() + b"\x00"
        # table oid, column, type oid, size, modifier, format (0 = text)
        payload += struct.pack("!IhIhih", 0, 0, _COLUMN_OIDS[name], -1, -1, 0)
    return _message(b"T", payload)


def _encode(column: str, value: int | str, *, binary: bool) -> bytes:
    if isinstance(value, str):
        return value.encode()  # text has no distinct binary wire form
    if binary:
        return struct.pack("!i", value)
    return str(value).encode()


def _data_row(columns: tuple[str, ...], values: tuple[int | str, ...], *, binary: bool) -> bytes:
    payload = struct.pack("!H", len(values))
    for column, value in zip(columns, values, strict=True):
        encoded = _encode(column, value, binary=binary)
        payload += struct.pack("!i", len(encoded)) + encoded
    return _message(b"D", payload)


class FakePostgres:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.executed_sql: list[str] = []
        self.flights: list[list[bytes]] = []

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return f"postgresql://wreath:secret@127.0.0.1:{self.port}/wreath"

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            startup_len = struct.unpack("!I", await reader.readexactly(4))[0]
            payload = await reader.readexactly(startup_len - 4)
            if startup_len == 16 and struct.unpack_from("!I", payload)[0] == 80877102:
                return  # cancel request
            writer.write(_message(b"R", struct.pack("!I", 0)))
            writer.write(_message(b"S", b"server_version\x0017.0\x00"))
            writer.write(_message(b"K", struct.pack("!II", 1, 2)))
            writer.write(_message(b"Z", b"I"))
            await writer.drain()

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
                    body = await reader.readexactly(length - 4)
                    flight.append((kind, body))
                    if kind == b"S":
                        break
                    kind = await reader.readexactly(1)
                self.flights.append([item[0] for item in flight])

                parse = next((body for k, body in flight if k == b"P"), None)
                if parse is not None:
                    name, _, tail = parse.partition(b"\x00")
                    query, _, param_tail = tail.partition(b"\x00")
                    sql = query.decode()
                    statements[name] = sql
                    param_count = struct.unpack_from("!H", param_tail)[0]
                    cold = True
                else:
                    bind = next(body for k, body in flight if k == b"B")
                    _, _, bind_tail = bind.partition(b"\x00")
                    name = bind_tail.partition(b"\x00")[0]
                    sql = statements[name]
                    param_count = 0
                    cold = False
                self.executed_sql.append(sql)

                returns_rows = sql.lstrip().lower().startswith("select")
                columns = ("id", "message") if "message" in sql else ("id", "value")
                if cold:
                    writer.write(_message(b"1"))
                    description = struct.pack("!H", param_count)
                    description += struct.pack("!I", 23) * param_count
                    writer.write(_message(b"t", description))
                    writer.write(_row_description(columns) if returns_rows else _message(b"n"))
                writer.write(_message(b"2"))
                if returns_rows:
                    if "message" in sql:
                        rows: tuple[tuple[int | str, ...], ...] = ((1, "alpha"), (2, "beta"))
                    else:
                        rows = ((1, 100),)
                    for row in rows:
                        writer.write(_data_row(columns, row, binary=not cold))
                    writer.write(_message(b"C", b"SELECT 1\x00"))
                else:
                    writer.write(_message(b"C", b"UPDATE 1\x00"))
                command = sql.lstrip().upper()
                if command.startswith(("BEGIN", "START")):
                    transaction_status = b"T"
                elif command.startswith(("COMMIT", "ROLLBACK")):
                    transaction_status = b"I"
                writer.write(_message(b"Z", transaction_status))
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
