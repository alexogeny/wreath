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
        # Several tests here leave a backend deliberately *running* -- that is the
        # claim they make -- so the lease is never coming back and the default
        # 10s grace is spent in full, twice, waiting for something this file
        # arranged not to happen. Measured at 10.01s of teardown per test.
        #
        # Shortened rather than removed: the drain path still runs, and nothing
        # here asserts on how long it waits. The tests' verdicts come from
        # `pg_stat_activity` on a third connection, never from shutdown timing.
        shutdown_timeout=0.5,
    )
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
def protocol() -> type:
    """The HTTP/1 protocol class, resolved the way `Server` resolves it."""
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


# --- the same chain, reached by a gRPC deadline instead of a disconnect -------
#
# `wreath.grpc`'s plan left this unchecked, and honestly: "the deadline cancels
# the handler" was proved, and whether that cancellation reached an in-flight
# ORM query was recorded as *unknown* rather than claimed. It is the same chain
# as the one above -- the driver cancels a backend whenever the awaiting task is
# cancelled, whatever cancelled it -- but "the same chain" is an argument, and
# the plan's own rule is that an argument is not a test.
#
# So this is the composition, driven the way gRPC actually reaches it:
# `grpc-timeout` on the wire, an HTTP/2 stream, and the verdict read from a
# third connection.


def _grpc_scanning_app(database: Any, scan: _Scan) -> Any:
    """One gRPC unary method that runs a long query on a pooled connection."""
    from wreath.grpc import GrpcService
    from wreath.protobuf import field, message

    @message
    class Ask:
        nothing: int = field(1, kind="uint32")

    service = GrpcService("wreath.test.Scanner")

    @service.unary(request=Ask, response=Ask)
    async def Scan(request: Any, msg: Any) -> Any:
        connection = await database.acquire("read")
        try:
            if not scan.pid.done():
                scan.pid.set_result(await connection.fetchval("SELECT pg_backend_pid()"))
            try:
                await connection.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})")
            except asyncio.CancelledError:
                scan.cancelled.set()
                raise
            scan.completed.set()
        finally:
            await database.release("read", connection)
        return msg

    app = wreath.Wreath()
    app.include_router(service.router())
    return app, Ask


async def test_a_grpc_deadline_stops_the_postgresql_backend(observer, database) -> None:
    """`grpc-timeout: 300m` with a 30-second query behind it.

    The evidence is `pg_stat_activity` on a third connection, for the reason
    this file's docstring gives: asserting the handler saw `CancelledError`
    proves asyncio works, and asserting our own await raised proves less.

    This closes the gRPC plan's highest-value follow-up. The guide said
    plainly that `grpc-timeout` bounds the response and is not a guarantee the
    server stopped working; it now is one, for a query on a wreath connection.
    """
    from http2 import support
    from http2.conftest import H2Driver, Http2Protocol

    from wreath.grpc import frame_message
    from wreath.protobuf import encode

    if Http2Protocol is None:
        pytest.skip("native Http2Protocol not built")

    scan = _Scan()
    app, Ask = _grpc_scanning_app(database, scan)
    driver = H2Driver(app)
    try:
        await driver.preface()
        await driver.feed_and_settle(
            support.build_headers_frame(
                1,
                support.request_headers(
                    path=b"/wreath.test.Scanner/Scan",
                    method=b"POST",
                    extra=[
                        (b"content-type", b"application/grpc+proto"),
                        (b"te", b"trailers"),
                        # 300 milliseconds: long enough that the query is
                        # genuinely in flight, short enough that the test is not
                        # a sleep.
                        (b"grpc-timeout", b"300m"),
                    ],
                ),
                end_stream=False,
            )
        )
        await driver.feed_and_settle(
            support.encode_frame(
                support.DATA, 0x1, 1, frame_message(encode(Ask(nothing=0)))
            )
        )

        pid = await asyncio.wait_for(scan.pid, timeout=_PATIENCE)
        assert await _settle(observer, pid, want="active") == "active", (
            "the query never reached the server; this would prove nothing"
        )

        await asyncio.wait_for(scan.cancelled.wait(), timeout=_PATIENCE)
        state = await _settle(observer, pid, want="idle")
        assert state in ("idle", None), (
            f"the backend was still {state!r} after the deadline expired: the "
            "cancellation stopped the handler and not the query it was waiting on"
        )
        assert not scan.completed.is_set(), "the 30-second query ran to completion"
    finally:
        driver.close()


async def test_the_connection_is_usable_after_a_deadline_cancel(
    observer, database
) -> None:
    """A cancelled query must return its connection to the pool *usable*.

    The failure this guards is worse than a leak: a connection returned with
    the cancelled query's state half-consumed answers the next borrower's
    request with the previous one's rows.
    """
    from http2 import support
    from http2.conftest import H2Driver, Http2Protocol

    from wreath.grpc import frame_message
    from wreath.protobuf import encode

    if Http2Protocol is None:
        pytest.skip("native Http2Protocol not built")

    scan = _Scan()
    app, Ask = _grpc_scanning_app(database, scan)
    driver = H2Driver(app)
    try:
        await driver.preface()
        await driver.feed_and_settle(
            support.build_headers_frame(
                1,
                support.request_headers(
                    path=b"/wreath.test.Scanner/Scan",
                    method=b"POST",
                    extra=[
                        (b"content-type", b"application/grpc+proto"),
                        (b"te", b"trailers"),
                        (b"grpc-timeout", b"300m"),
                    ],
                ),
                end_stream=False,
            )
        )
        await driver.feed_and_settle(
            support.encode_frame(
                support.DATA, 0x1, 1, frame_message(encode(Ask(nothing=0)))
            )
        )
        await asyncio.wait_for(scan.cancelled.wait(), timeout=_PATIENCE)
    finally:
        driver.close()

    # The pool has exactly one connection, so this is necessarily the same one.
    connection = await database.acquire("read")
    try:
        assert await connection.fetchval("SELECT 41 + 1") == 42
    finally:
        await database.release("read", connection)
