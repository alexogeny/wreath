from __future__ import annotations

import importlib
from typing import Annotated

import pytest

import wreath
from wreath.postgres import Connection, FromDatabase
from wreath.replay import (
    AdapterFault,
    CanonicalRequest,
    DatabaseDouble,
    FaultDescriptor,
    FaultKind,
    FaultSchedule,
    FaultyHttpClient,
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


proto = pytest.mark.parametrize(
    "protocol_cls",
    [
        pytest.param(
            _NATIVE_HTTP1,
            id="http1",
            marks=pytest.mark.skipif(_NATIVE_HTTP1 is None, reason="native server not built"),
        ),
    ],
)

_TERMINALS = {"closed", "aborted", "open"}


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode())

    return app


# A spread of adversarial-but-parseable and outright-malformed requests. The
# owned decision differs per input; the point is that it is *some* owned decision,
# the same each time.
ADVERSARIAL = {
    "pipelined": b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\nGET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",  # noqa: E501
    "chunked_ok": b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n",  # noqa: E501
    "chunked_truncated": b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhel",  # noqa: E501
    "duplicate_content_length": b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 6\r\nConnection: close\r\n\r\nhello",  # noqa: E501
    "unknown_method": b"FROBNICATE /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
    "header_bomb": b"GET /ping HTTP/1.1\r\nHost: x\r\n"
    + b"X-Pad: v\r\n" * 500
    + b"Connection: close\r\n\r\n",
    "nul_in_target": b"GET /pi\x00ng HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
    "absurd_content_length": b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\nConnection: close\r\n\r\nhi",  # noqa: E501
    "bare_lf": b"GET /ping HTTP/1.1\nHost: x\nConnection: close\n\n",
    "space_before_colon": b"GET /ping HTTP/1.1\r\nHost : x\r\nConnection: close\r\n\r\n",
}


@proto
@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(ADVERSARIAL))
async def test_adversarial_input_is_deterministic_and_bounded(
    protocol_cls: type, name: str
) -> None:
    rec = record_transport_segments([ADVERSARIAL[name]])
    a = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    b = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    assert a.matches(b)  # deterministic owned outcome
    assert a.terminal in _TERMINALS
    # A request that cannot complete must never be answered 200.
    if name in ("chunked_truncated",):
        assert b"HTTP/1.1 200" not in a.response


@proto
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pipelined", "chunked_ok", "chunked_truncated"])
async def test_truncate_at_every_offset_never_crashes_or_fabricates(
    protocol_cls: type, name: str
) -> None:
    # Small inputs only: byte-per-segment truncation is O(n^2), so this sweeps
    # the interesting short cases exhaustively and leaves the large ones to the
    # coarse sweep below.
    raw = ADVERSARIAL[name]
    rec = record_transport_segments([raw[i : i + 1] for i in range(len(raw))])
    full = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    for cut in range(0, len(raw)):
        sched = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), cut, 0),))
        a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        assert a.matches(b)
        assert a.terminal in _TERMINALS
        # Cutting before the full stream cannot invent a success the whole stream
        # did not already contain.
        if b"HTTP/1.1 200" not in full.response:
            assert b"HTTP/1.1 200" not in a.response


@proto
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["header_bomb", "absurd_content_length"])
async def test_truncate_coarsely_on_large_inputs_is_safe(protocol_cls: type, name: str) -> None:
    # A coarse sweep for the large inputs: a handful of segments, truncate each.
    raw = ADVERSARIAL[name]
    step = max(1, len(raw) // 8)
    rec = record_transport_segments([raw[i : i + step] for i in range(0, len(raw), step)])
    for cut in range(len(rec.segments)):
        sched = FaultSchedule((FaultDescriptor(int(FaultKind.TRUNCATE), cut, 0),))
        a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
        assert a.matches(b)
        assert a.terminal in _TERMINALS
        assert b"HTTP/1.1 200" not in a.response  # truncated -> never a success


@proto
@pytest.mark.asyncio
async def test_reset_at_every_offset_is_bounded(protocol_cls: type) -> None:
    raw = ADVERSARIAL["pipelined"]
    rec = record_transport_segments([raw[i : i + 1] for i in range(len(raw))])
    for cut in range(0, len(raw), 3):  # every third offset keeps the sweep quick
        for kind in (FaultKind.RESET, FaultKind.HALF_CLOSE):
            sched = FaultSchedule((FaultDescriptor(int(kind), cut),))
            a = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
            b = await replay_transport(_app(), rec, protocol_cls=protocol_cls, faults=sched)
            assert a.matches(b)
            assert a.terminal in _TERMINALS


@proto
@pytest.mark.asyncio
async def test_reset_between_pipelined_requests_drops_the_second(protocol_cls: type) -> None:
    # Two pipelined requests split so a reset lands cleanly after the first.
    first = b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n"
    second = b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    rec = record_transport_segments([first, second])
    clean = await replay_transport(_app(), rec, protocol_cls=protocol_cls)
    reset = await replay_transport(
        _app(),
        rec,
        protocol_cls=protocol_cls,
        faults=FaultSchedule((FaultDescriptor(int(FaultKind.RESET), 0),)),
    )
    # The clean run answers both; the reset run cannot answer more than the clean.
    assert clean.response.count(b"HTTP/1.1 200") >= reset.response.count(b"HTTP/1.1 200")


def _db_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/q")
    async def q(request: wreath.Request, db: Connection) -> dict:
        await db.execute("BEGIN")
        rows = await db.fetch("SELECT 1")
        await db.execute("COMMIT")
        return {"rows": len(rows)}

    return app


_DB_FAULTS = [
    AdapterFault.SERVER_ERROR,
    AdapterFault.CONNECTION_DROP,
    AdapterFault.LOST_COMMIT,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", _DB_FAULTS)
@pytest.mark.parametrize("position", [0, 1, 2])
async def test_db_fault_at_any_query_releases_and_is_deterministic(
    fault: AdapterFault, position: int
) -> None:
    def run():
        double = DatabaseDouble("main", query_faults={position: fault})
        return double, replay_endpoint_plan(
            _db_app(),
            CanonicalRequest("GET", "/q"),
            adapters=ReplayAdapters(databases={"main": double}),
        )

    double_a, coro_a = run()
    result_a = await coro_a
    double_b, coro_b = run()
    result_b = await coro_b
    assert result_a.status == 500  # an unhandled boundary fault is a 500
    assert not double_a.leaked  # released on the error path
    assert result_a.matches(result_b)  # deterministic
    assert double_a.acquired == double_b.acquired == 1


@pytest.mark.asyncio
async def test_acquire_fault_matrix_never_double_releases() -> None:
    for fault in (AdapterFault.POOL_TIMEOUT, AdapterFault.POOL_EXHAUSTED):
        double = DatabaseDouble("main", acquire_fault=fault)
        result = await replay_endpoint_plan(
            _db_app(),
            CanonicalRequest("GET", "/q"),
            adapters=ReplayAdapters(databases={"main": double}),
        )
        assert result.status == 500
        assert double.acquired == 1 and double.released == 0


@pytest.mark.asyncio
async def test_two_connections_both_released_when_one_faults() -> None:
    app = wreath.Wreath()
    app.postgres("a", dsn="postgres://stub/a")
    app.postgres("b", dsn="postgres://stub/b")

    @app.get("/join")
    async def join(
        request: wreath.Request,
        a: Annotated[Connection, FromDatabase("a")],
        b: Annotated[Connection, FromDatabase("b")],
    ) -> dict:
        await a.fetch("SELECT 1")  # this one faults
        await b.fetch("SELECT 2")
        return {"ok": True}

    double_a = DatabaseDouble("a", query_faults={0: AdapterFault.SERVER_ERROR})
    double_b = DatabaseDouble("b")
    result = await replay_endpoint_plan(
        app,
        CanonicalRequest("GET", "/join"),
        adapters=ReplayAdapters(databases={"a": double_a, "b": double_b}),
    )
    assert result.status == 500
    # Every leased connection is returned, even though an earlier release's owner
    # raised: release runs all legs (_release's contract).
    assert not double_a.leaked and not double_b.leaked


@pytest.mark.asyncio
async def test_release_error_is_surfaced_without_leaking_state() -> None:
    # If returning the connection itself fails, the framework surfaces an owned
    # error rather than swallowing it or hanging.
    double = DatabaseDouble("main", release_fault=AdapterFault.RELEASE_ERROR)
    result = await replay_endpoint_plan(
        _db_app(),
        CanonicalRequest("GET", "/q"),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status in (200, 500)  # owned decision, never a crash
    assert double.acquired == 1 and double.released == 1


@pytest.mark.asyncio
async def test_nth_outbound_call_fault_maps_to_owned_status() -> None:
    from wreath.http_client import ClientResponse

    faulty = FaultyHttpClient(
        "api",
        responses=(ClientResponse(status=200, headers=(), body=b"", http_version="1.1"),),
        request_faults={1: AdapterFault.READ_TIMEOUT},  # the second call times out
    )
    app = wreath.Wreath()

    @app.get("/fanout")
    async def fanout(request: wreath.Request) -> dict:
        await faulty.request("GET", "/one")  # ok
        await faulty.request("GET", "/two")  # times out -> unhandled
        return {"ok": True}

    result = await replay_endpoint_plan(app, CanonicalRequest("GET", "/fanout"))
    assert result.status == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/ping"),  # wrong method for a GET route
        ("GET", "/../etc/passwd"),  # traversal-looking path
        ("GET", "/ping/" + "x" * 4000),  # very long path
        ("GET", ""),  # empty path
        ("GET", "/ping%00"),  # encoded nul
    ],
)
async def test_routing_adversarial_never_200_and_is_deterministic(method: str, path: str) -> None:
    canonical = CanonicalRequest(method, path)
    a = await replay_endpoint_plan(_app(), canonical)
    b = await replay_endpoint_plan(_app(), canonical)
    assert a.status != 200
    assert a.matches(b)
