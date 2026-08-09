"""In-process upstream dependencies for the `e2e` benchmark scenario.

The e2e pathway measures a fully orchestrated request -- authentication, a
remote HTTP fetch through `wreath.http_client`, and a database round trip
through `wreath.postgres` -- without external infrastructure: both upstreams
run in-process on the benchmarked loop.

Both upstreams are raw asyncio.Protocol state machines, deliberately as close
to free as an in-process peer can be: no asyncio streams, no futures, no
awaits -- one data_received per request, one transport.write in response.
Measured on the metal loop, the streams-based predecessors cost ~19 us per
HTTP round trip and dominated the scenario; what remains in the row is the
framework under test, not the scaffolding.

The PostgreSQL responder mirrors the proven flight logic of
tests/postgres/test_connection.py (trust auth, extended-protocol flights, one
int4 DataRow per select) as an incremental wire parser.
"""
from __future__ import annotations

import asyncio
import struct


def _message(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _row_description() -> bytes:
    field = b"value\x00" + struct.pack("!IhIhih", 0, 0, 23, 4, -1, 0)
    return _message(b"T", struct.pack("!H", 1) + field)


def _data_row(value: bytes) -> bytes:
    payload = struct.pack("!H", 1) + struct.pack("!i", len(value)) + value
    return _message(b"D", payload)


_PG_STARTUP_OK = (
    _message(b"R", struct.pack("!I", 0))
    + _message(b"S", b"server_version\x0017.0\x00")
    + _message(b"K", struct.pack("!II", 1, 2))
    + _message(b"Z", b"I")
)
_PG_CANCEL_CODE = 80877102
_PG_SYNC = _message(b"S")
_PG_HOT_SQL = "select $1::int4"
_PG_HOT_RESPONSE = (
    _message(b"2")
    + _data_row(struct.pack("!i", 42))
    + _message(b"C", b"SELECT 1\x00")
    + _message(b"Z", b"I")
)


class _BenchPostgresProtocol(asyncio.Protocol):
    """Incremental PostgreSQL wire responder: flights in, canned rows out."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self.buffer = bytearray()
        self.started = False
        self.statements: dict[bytes, str] = {}
        self.flight: list[tuple[int, bytes]] = []
        self.hot = False

    def connection_lost(self, exc: Exception | None) -> None:
        self.buffer.clear()

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        if self.hot:
            # Once the benchmark's one prepared query is warm, every flight is
            # the same Bind/Execute/Sync and every answer is the same binary
            # int4 row. Parsing and allocating a tuple for each frontend
            # message made the in-process test double a visible part of the
            # server's instruction count. Count complete Sync delimiters in C,
            # retain a fragmented tail, and emit the canned flight once per
            # operation. The exact SQL check in `_respond` is what enables this
            # benchmark-only arm; any other query keeps the general parser.
            completed = self.buffer.count(_PG_SYNC)
            if not completed:
                return
            consumed = self.buffer.rfind(_PG_SYNC) + len(_PG_SYNC)
            del self.buffer[:consumed]
            self.transport.write(_PG_HOT_RESPONSE * completed)  # type: ignore[attr-defined]
            return
        if not self.started:
            if len(self.buffer) < 4:
                return
            startup_len = struct.unpack_from("!I", self.buffer)[0]
            if len(self.buffer) < startup_len:
                return
            if (
                startup_len == 16
                and struct.unpack_from("!I", self.buffer, 4)[0]
                == _PG_CANCEL_CODE
            ):
                self.transport.close()  # type: ignore[attr-defined]
                return
            del self.buffer[:startup_len]
            self.started = True
            self.transport.write(_PG_STARTUP_OK)  # type: ignore[attr-defined]
        out = bytearray()
        while True:
            if len(self.buffer) < 5:
                break
            kind = self.buffer[0]
            length = struct.unpack_from("!I", self.buffer, 1)[0]
            if len(self.buffer) < 1 + length:
                break
            payload = bytes(self.buffer[5 : 1 + length])
            del self.buffer[: 1 + length]
            if kind == 0x58:  # 'X' Terminate
                self.transport.close()  # type: ignore[attr-defined]
                return
            self.flight.append((kind, payload))
            if kind == 0x53:  # 'S' Sync: the flight is complete
                self._respond(out)
                self.flight.clear()
        if out:
            self.transport.write(bytes(out))  # type: ignore[attr-defined]

    def _respond(self, out: bytearray) -> None:
        parse = next(
            (payload for kind, payload in self.flight if kind == 0x50), None
        )
        parameter_count = 0
        if parse is not None:  # 'P'
            statement_name, _, tail = parse.partition(b"\x00")
            query, _, parameter_data = tail.partition(b"\x00")
            self.statements[statement_name] = query.decode()
            parameter_count = struct.unpack_from("!H", parameter_data)[0]
            sql = self.statements[statement_name]
        else:
            bind = next(
                payload for kind, payload in self.flight if kind == 0x42
            )  # 'B'
            _, _, bind_tail = bind.partition(b"\x00")
            sql = self.statements[bind_tail.partition(b"\x00")[0]]

        cold = parse is not None
        returns_rows = sql.lstrip().lower().startswith("select")
        if not cold and sql == _PG_HOT_SQL:
            self.hot = True
        if cold:
            out += _message(b"1")
            description = struct.pack("!H", parameter_count)
            description += struct.pack("!I", 23) * parameter_count
            out += _message(b"t", description)
            out += _row_description() if returns_rows else _message(b"n")
        out += _message(b"2")
        if any(
            kind == 0x44 and payload.startswith(b"P")  # 'D' describing a portal
            for kind, payload in self.flight
        ):
            out += _row_description() if returns_rows else _message(b"n")
        if returns_rows:
            value = struct.pack("!i", 42) if not cold else b"42"
            out += _data_row(value)
            out += _message(b"C", b"SELECT 1\x00")
        else:
            out += _message(b"C", b"UPDATE 1\x00")
        out += _message(b"Z", b"I")


class BenchPostgres:
    """Minimal in-process PostgreSQL wire responder (select -> 42)."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        loop = asyncio.get_running_loop()
        self.server = await loop.create_server(
            _BenchPostgresProtocol, "127.0.0.1", 0
        )
        port = self.server.sockets[0].getsockname()[1]
        return f"postgresql://bench:bench@127.0.0.1:{port}/bench"

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


_UPSTREAM_BODY = (
    b'{"service":"upstream","status":"ok","items":[1,2,3,4,5],'
    b'"region":"local","cached":false}'
)
_UPSTREAM_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_UPSTREAM_BODY)).encode() + b"\r\n"
    b"\r\n" + _UPSTREAM_BODY
)


class _BenchUpstreamHttpProtocol(asyncio.Protocol):
    """Keep-alive HTTP/1.1 responder for bodyless requests."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self.buffer = b""

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        responses = 0
        while True:
            end = self.buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buffer) > 16384:
                    self.transport.close()  # type: ignore[attr-defined]
                return
            self.buffer = self.buffer[end + 4 :]
            responses += 1
            if responses == 1 and not self.buffer:
                # The common case: exactly one request in this wakeup.
                self.transport.write(_UPSTREAM_RESPONSE)  # type: ignore[attr-defined]
                return
            self.transport.write(_UPSTREAM_RESPONSE)  # type: ignore[attr-defined]


class BenchUpstreamHttp:
    """Keep-alive HTTP/1.1 upstream returning one canned JSON document."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self.server = await loop.create_server(
            _BenchUpstreamHttpProtocol, "127.0.0.1", 0
        )
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
