"""A client that goes away stops the PostgreSQL backend it started.

The whole chain, end to end, against a live server: a real socket closes, the
server cancels the application task, the driver sends a wire-level
`CancelRequest` on a second connection, and PostgreSQL stops scanning.

**The evidence is `pg_stat_activity` read over an independent connection.**
Asserting that the handler saw `CancelledError` proves only that asyncio works,
and asserting that our own `await` raised proves less than that. Every verdict
here is what a *third* connection saw the victim backend doing.

`tests/test_server_cancel_on_disconnect.py` pins the server's half of this
without a database. This file exists because the two halves were built years
apart and only their composition answers the question anybody actually has.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

import wreath
from wreath.postgres import Database, PoolConfig, connect
from wreath.server import ServerConfig, _select_protocol, serve

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.database

#: Long enough that a backend still running it is unambiguous, never reached.
_SLEEP_SECONDS = 30

#: Bound on every wait here. A cancel that hangs is worse than one that leaks.
_PATIENCE = 10.0


@pytest.fixture
async def observer() -> Any:
    """An independent connection, used only to watch other backends."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live cancellation tests")
    connection = await connect(_DSN)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def database() -> Any:
    """A started `Database`, so the pool is the thing under test and not a stub."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live cancellation tests")
    db = Database(
        "cancel",
        _DSN,
        pools={"read": PoolConfig(min_size=1, max_size=1)},
    )
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture(params=[False, True], ids=["native", "pure"])
def protocol(request: Any, monkeypatch: Any) -> type:
    """Drive the native HTTP/1 protocol and the pure reference in turn."""
    if request.param:
        monkeypatch.setenv("WREATH_PURE", "1")
    else:
        monkeypatch.delenv("WREATH_PURE", raising=False)
    return _select_protocol()


async def _backend_state(observer: Any, pid: int) -> str | None:
    return await observer.fetchval(
        "SELECT state FROM pg_stat_activity WHERE pid = $1", pid
    )


async def _settle(observer: Any, pid: int, *, want: str) -> str | None:
    """Poll briefly for `want`; a cancel is a round trip, not instantaneous."""
    state = None
    for _ in range(100):
        state = await _backend_state(observer, pid)
        if state == want:
            return state
        await asyncio.sleep(0.05)
    return state


class _Scan:
    """The handler's side of one long query: which backend, and what happened."""

    __slots__ = ("cancelled", "completed", "pid")

    def __init__(self) -> None:
        self.pid: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self.cancelled = asyncio.Event()
        self.completed = asyncio.Event()


def _scanning_app(database: Any, scan: _Scan) -> wreath.Wreath:
    """A GET and a POST that each run a long query on a pooled connection."""
    app = wreath.Wreath()

    async def run() -> None:
        connection = await database.acquire("read")
        try:
            if not scan.pid.done():
                scan.pid.set_result(await connection.fetchval("SELECT pg_backend_pid()"))
            try:
                await connection.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})")
            except asyncio.CancelledError:
                # Recorded and re-raised. Swallowing it here would leave the
                # driver's own cancellation half-run.
                scan.cancelled.set()
                raise
            scan.completed.set()
        finally:
            await database.release("read", connection)

    @app.get("/scan")
    async def scan_get(request: wreath.Request) -> wreath.Response:
        await run()
        return wreath.Response(b"late")

    @app.post("/scan")
    async def scan_post(request: wreath.Request) -> wreath.Response:
        await run()
        return wreath.Response(b"late")

    return app


async def _serve(app: Any) -> Any:
    return await serve(app, ServerConfig(host="127.0.0.1", port=0, lifespan="off"))


def _port(server: Any) -> int:
    return server.sockets[0].getsockname()[1]


def _request(method: str) -> bytes:
    return (
        f"{method} /scan HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
    ).encode()


async def _start_and_wait_active(
    port: int, method: str, scan: _Scan, observer: Any
) -> tuple[Any, int]:
    """Send the request and return once the backend is demonstrably scanning."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_request(method))
    await writer.drain()
    pid = await asyncio.wait_for(scan.pid, timeout=_PATIENCE)
    state = await _settle(observer, pid, want="active")
    assert state == "active", f"backend {pid} never started the scan (state={state!r})"
    del reader
    return writer, pid


async def _drop(writer: Any) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass


async def test_a_disconnected_get_stops_the_backend(
    protocol: type, database: Any, observer: Any
) -> None:
    """The finding, closed. Observed from a connection that is not ours."""
    scan = _Scan()
    server = await _serve(_scanning_app(database, scan))
    try:
        writer, pid = await _start_and_wait_active(
            _port(server), "GET", scan, observer
        )
        await _drop(writer)
        state = await _settle(observer, pid, want="idle")
    finally:
        await server.close()
    assert state == "idle", (
        f"backend {pid} was still {state!r} after the client disconnected; "
        "the scan outlived the request that asked for it"
    )
    assert scan.cancelled.is_set()
    assert not scan.completed.is_set()


async def test_an_unabandoned_query_keeps_running(
    protocol: type, database: Any, observer: Any
) -> None:
    """The control. Without it the test above could pass vacuously.

    If `pg_sleep` were not actually running by the time we look, or `state` read
    `idle` for some unrelated reason, the assertion above would be satisfied by
    a backend nobody ever cancelled.
    """
    scan = _Scan()
    server = await _serve(_scanning_app(database, scan))
    try:
        writer, pid = await _start_and_wait_active(
            _port(server), "GET", scan, observer
        )
        await asyncio.sleep(1.0)
        state = await _backend_state(observer, pid)
        assert state == "active", (
            f"backend {pid} went {state!r} with the client still attached, so "
            "the cancellation test above proves nothing"
        )
        assert not scan.cancelled.is_set()
        await _drop(writer)
    finally:
        await server.close()


async def test_a_disconnected_post_leaves_the_backend_running(
    protocol: type, database: Any, observer: Any
) -> None:
    """An unsafe method is not cancelled implicitly, all the way down."""
    scan = _Scan()
    server = await _serve(_scanning_app(database, scan))
    try:
        writer, pid = await _start_and_wait_active(
            _port(server), "POST", scan, observer
        )
        await _drop(writer)
        await asyncio.sleep(1.0)
        state = await _backend_state(observer, pid)
        assert state == "active", (
            f"backend {pid} went {state!r} after a POST client disconnected; "
            "an unsafe method must not be cancelled implicitly"
        )
        assert not scan.cancelled.is_set()
    finally:
        await server.close()


async def test_the_pooled_connection_serves_the_next_request(
    protocol: type, database: Any, observer: Any
) -> None:
    """A poisoned connection back in the pool is worse than a wasted scan.

    The pool holds exactly one connection, so the request that follows the
    abandoned one is served by the same backend that was cancelled -- there is
    no second connection for it to hide behind.
    """
    scan = _Scan()
    app = _scanning_app(database, scan)
    answered = asyncio.Event()

    @app.get("/after")
    async def after(request: wreath.Request) -> wreath.Response:
        connection = await database.acquire("read")
        try:
            value = await connection.fetchval("SELECT 42")
            pid = await connection.fetchval("SELECT pg_backend_pid()")
        finally:
            await database.release("read", connection)
        answered.set()
        return wreath.Response(f"{value}:{pid}".encode())

    server = await _serve(app)
    try:
        writer, pid = await _start_and_wait_active(
            _port(server), "GET", scan, observer
        )
        await _drop(writer)
        assert await _settle(observer, pid, want="idle") == "idle"

        reader, writer2 = await asyncio.open_connection("127.0.0.1", _port(server))
        writer2.write(b"GET /after HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer2.drain()
        answer = await asyncio.wait_for(reader.read(65536), timeout=_PATIENCE)
        await _drop(writer2)
    finally:
        await server.close()

    assert answered.is_set()
    assert f"42:{pid}".encode() in answer, (
        "the next request was not served by the cancelled connection, so this "
        f"proves nothing about it; expected backend {pid}, saw {answer!r}"
    )
    assert database.pool("read").snapshot().borrowed == 0, (
        "the cancelled request never returned its lease to the pool"
    )


async def test_the_backend_is_idle_not_idle_in_transaction(
    protocol: type, database: Any, observer: Any
) -> None:
    """`idle in transaction` after a cancel is a snapshot held open forever."""
    scan = _Scan()
    server = await _serve(_scanning_app(database, scan))
    try:
        writer, pid = await _start_and_wait_active(
            _port(server), "GET", scan, observer
        )
        await _drop(writer)
        state = await _settle(observer, pid, want="idle")
    finally:
        await server.close()
    assert state == "idle", f"expected a clean idle backend, saw {state!r}"
