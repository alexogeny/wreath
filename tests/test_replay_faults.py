"""Red-team the framework's owned failure handling with replay fault injection.

These tests use replay + fault injection to drive Wreath's *own* code under
adversity and assert the owned outcome is deterministic and safe:

- **Transport / parser:** truncated, short-read, reset, and half-closed inbound
  streams never crash the driver, never fabricate a success response, and produce
  the same owned outcome on a re-run (a first-class property for a parser).
- **PostgreSQL / ORM:** a boundary fault (pool timeout, server error, connection
  drop, ambiguous commit) is mapped to an owned status *and the leased connection
  is returned to the pool* — the framework, not the handler, owns release.
- **Outbound HTTP:** a connect/read fault propagates through the client's owned
  timeout/error path and, through a handler, maps to an owned status.

This file is the behavioral baseline: adding a route and a fault here exercises
real framework logic, not a mock of it.
"""

from __future__ import annotations

import importlib

import pytest

import wreath
from wreath.postgres import Connection
from wreath.replay import (
    AdapterFault,
    CanonicalRequest,
    DatabaseDouble,
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    FaultyHttpClient,
    PlanMode,
    ReplayAdapters,
    record_transport_segments,
    replay_endpoint_plan,
    replay_transport,
)

try:
    _native = importlib.import_module("wreath._native._server")
    _NATIVE_HTTP1 = _native.Http1Protocol
except ImportError:
    _NATIVE_HTTP1 = None

from wreath._pure.server import Http1Protocol as _PURE_HTTP1

PROTOCOLS = [pytest.param(_PURE_HTTP1, id="pure")]
PROTOCOLS.append(
    pytest.param(
        _NATIVE_HTTP1,
        id="native",
        marks=pytest.mark.skipif(_NATIVE_HTTP1 is None, reason="native server not built"),
    )
)
proto = pytest.mark.parametrize("protocol_cls", PROTOCOLS)

GET = b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
_TERMINALS = {"closed", "aborted", "open"}


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    return app


# --- transport / parser red-team ---------------------------------------------


@proto
@pytest.mark.asyncio
async def test_truncation_at_every_offset_is_safe_and_never_succeeds(protocol_cls: type) -> None:
    # Splitting the request into three segments and truncating at each byte of the
    # first segment must never crash and never yield a 200 (the request is
    # incomplete), and must be deterministic.
    rec = record_transport_segments([GET[:20], GET[20:35], GET[35:]])
    for cut in range(0, 20):
        sched = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), 0, cut),))
        a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        assert a.terminal in _TERMINALS
        assert b"HTTP/1.1 200" not in a.response
        assert a.matches(b)


@proto
@pytest.mark.asyncio
async def test_reset_or_half_close_at_every_segment_is_deterministic(protocol_cls: type) -> None:
    segments = [GET[:12], GET[12:24], GET[24:38], GET[38:]]
    rec = record_transport_segments(segments)
    for kind in (FaultKind.RESET, FaultKind.HALF_CLOSE):
        for index in range(len(segments)):
            sched = FaultSchedule((FaultDescriptor(int(kind), index),))
            a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
            b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
            assert a.terminal in _TERMINALS
            assert a.matches(b)


@proto
@pytest.mark.asyncio
async def test_short_read_never_fabricates_a_response(protocol_cls: type) -> None:
    rec = record_transport_segments([GET])
    for keep in range(0, len(GET)):
        sched = FaultSchedule((FaultDescriptor(int(FaultKind.SHORT_READ), 0, keep),))
        result = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        # A partial head that stops before the terminator cannot complete a request.
        if keep < len(GET):
            assert b"pong" not in result.response


@proto
@pytest.mark.asyncio
async def test_malformed_request_line_maps_to_an_owned_error_not_a_crash(
    protocol_cls: type,
) -> None:
    malformed = b"GET \x00\x01\x02 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    rec = record_transport_segments([malformed])
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    # Whatever the owned decision (400 or silent close), it is the same each time
    # and never a 200.
    assert b"HTTP/1.1 200" not in a.response
    assert a.matches(b)


@proto
@pytest.mark.asyncio
async def test_timeout_fault_fires_the_drivers_own_request_deadline(protocol_cls: type) -> None:
    # A TIMEOUT fault is not a fabricated outcome: it fires the *driver's own*
    # armed deadline enforcement (native ``_replay_fire_timeout`` -> C
    # ``enforce_deadline``; the pure twin mirrors it). A complete head that
    # promises a body which never fully arrives leaves the request deadline armed
    # and the response unanswered -> the owned path emits a real 408, identically
    # on both twins.
    app = wreath.Wreath()

    @app.post("/upload")
    async def upload(request: wreath.Request) -> wreath.Response:
        await request.body()  # awaits the CL-promised body that never completes
        return wreath.response.TextResponse("ok")

    head = b"POST /upload HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nhalf"
    rec = record_transport_segments([head], close=None)
    sched = FaultSchedule((FaultDescriptor(int(FaultKind.TIMEOUT), 0),))
    a = await replay_transport(app, rec, protocol_cls=protocol_cls, faults=sched)
    b = await replay_transport(app, rec, protocol_cls=protocol_cls, faults=sched)
    assert b"HTTP/1.1 408" in a.response
    assert a.matches(b)
    assert a.terminal in _TERMINALS


@proto
@pytest.mark.asyncio
async def test_timeout_fault_on_an_idle_connection_closes_deterministically(
    protocol_cls: type,
) -> None:
    # With no request in flight the armed deadline is the keep-alive timer: firing
    # it closes the idle connection with no response -- the other owned branch of
    # the same mechanism, and still twin-deterministic.
    partial = b"GET /ping HTTP/1.1\r\nHost: x\r\n"  # head never terminates
    rec = record_transport_segments([partial], close=None)
    sched = FaultSchedule((FaultDescriptor(int(FaultKind.TIMEOUT), 0),))
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
    assert b"HTTP/1.1 200" not in a.response
    assert a.terminal in _TERMINALS
    assert a.matches(b)


# --- PostgreSQL / ORM red-team -----------------------------------------------


def _db_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/users")
    async def users(request: wreath.Request, db: Connection) -> dict:
        rows = await db.fetch("SELECT id FROM users")
        return {"count": len(rows)}

    @app.get("/two")
    async def two(request: wreath.Request, db: Connection) -> dict:
        await db.execute("BEGIN")
        rows = await db.fetch("SELECT 1")  # the second query
        return {"count": len(rows)}

    @app.get("/guarded")
    async def guarded(request: wreath.Request, db: Connection) -> dict:
        try:
            await db.fetch("SELECT 1")
        except Exception:
            return {"handled": True}
        return {"handled": False}

    return app


@pytest.mark.asyncio
async def test_query_server_error_maps_to_500_and_releases_the_connection() -> None:
    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        _db_app(), CanonicalRequest("GET", "/users"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked  # the framework returned the connection on the error path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [AdapterFault.POOL_TIMEOUT, AdapterFault.POOL_EXHAUSTED],
)
async def test_acquire_faults_map_to_500_without_a_phantom_release(fault: AdapterFault) -> None:
    double = DatabaseDouble("main", acquire_fault=fault)
    result = await replay_endpoint_plan(
        _db_app(), CanonicalRequest("GET", "/users"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    # Nothing was leased, so nothing is released; the framework does not
    # double-release a connection it never got.
    assert double.acquired == 1 and double.released == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [AdapterFault.SERVER_ERROR, AdapterFault.CONNECTION_DROP, AdapterFault.LOST_COMMIT],
)
async def test_midflight_db_faults_never_leak_a_connection(fault: AdapterFault) -> None:
    double = DatabaseDouble("main", query_faults={0: fault})
    result = await replay_endpoint_plan(
        _db_app(), CanonicalRequest("GET", "/users"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked


@pytest.mark.asyncio
async def test_fault_on_the_second_query_still_releases() -> None:
    # The Nth-query coordinate: the first query succeeds, the second faults.
    double = DatabaseDouble("main", query_faults={1: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        _db_app(), CanonicalRequest("GET", "/two"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked


@pytest.mark.asyncio
async def test_handler_that_swallows_the_db_error_still_releases() -> None:
    # Release is framework-owned: even when the handler catches the error and
    # returns 200, the binder returns the connection to the pool.
    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        _db_app(), CanonicalRequest("GET", "/guarded"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 200
    assert result.body == b'{"handled":true}'
    assert not double.leaked


@pytest.mark.asyncio
async def test_db_fault_outcome_is_deterministic() -> None:
    def run():
        double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
        return replay_endpoint_plan(
            _db_app(), CanonicalRequest("GET", "/users"),
            adapters=ReplayAdapters(databases={"main": double}),
        )

    a = await run()
    b = await run()
    assert a.matches(b)


# --- outbound HTTP red-team --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault, expected",
    [
        (AdapterFault.CONNECT_ERROR, ConnectionError),
        (AdapterFault.READ_TIMEOUT, TimeoutError),
    ],
)
async def test_outbound_fault_propagates_through_the_owned_client(
    fault: AdapterFault, expected: type
) -> None:
    client = FaultyHttpClient("api", request_faults={0: fault})
    with pytest.raises(expected):
        await client.request("GET", "/upstream")


@pytest.mark.asyncio
async def test_outbound_fault_through_a_handler_maps_to_an_owned_status() -> None:
    faulty = FaultyHttpClient("api", request_faults={0: AdapterFault.CONNECT_ERROR})
    app = wreath.Wreath()

    @app.get("/proxy")
    async def proxy(request: wreath.Request) -> dict:
        response = await faulty.request("GET", "/upstream")
        return {"status": response.status}

    # The handler's outbound call fails; the owned dispatch maps it to a 500.
    result = await replay_endpoint_plan(app, CanonicalRequest("GET", "/proxy"))
    assert result.status == 500


@pytest.mark.asyncio
async def test_outbound_scripted_response_lets_a_handler_complete() -> None:
    from wreath.http_client import ClientResponse

    ok = FaultyHttpClient(
        "api", responses=(ClientResponse(status=201, headers=(), body=b"", http_version="1.1"),)
    )
    app = wreath.Wreath()

    @app.get("/proxy")
    async def proxy(request: wreath.Request) -> dict:
        response = await ok.request("POST", "/upstream")
        return {"status": response.status}

    result = await replay_endpoint_plan(
        app, CanonicalRequest("GET", "/proxy"), mode=PlanMode.INVOKE
    )
    assert result.status == 200
    assert result.body == b'{"status":201}'
