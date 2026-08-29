from __future__ import annotations

import asyncio
import json
import threading

import pytest

from wreath import Wreath
from wreath._cli import main as cli_main
from wreath._flight_schema import (
    FLAG_AI_SCRAPING_REFUSED,
    FLAG_POLICY_REFUSED,
    Protocol,
    TerminalStatus,
)
from wreath._projector import ProjectedTrace, Projector
from wreath.inspector import (
    Command,
    InspectorClient,
    InspectorConfig,
    InspectorError,
    _trace_payload,
    serve_inspector,
)

_flight = pytest.importorskip("wreath._native._flight")


def _app() -> Wreath:
    app = Wreath()

    @app.get("/widgets/{widget_id}")
    async def widget(request, widget_id: int) -> str:
        return "ok"

    app._compile_routes()
    return app


def _fed_projector() -> Projector:
    """A recorder driven with three OK completions on route 7 and one 500 error
    on route 9, drained through a projector until settled."""
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=256, active_requests=32)
    for i in range(3):
        rec.record(
            start_ns=0,
            end_ns=(i + 1) * 1000,
            protocol=int(Protocol.HTTP1),
            route_id=7,
            status=200,
            connection_id=i,
        )
    rec.record(
        start_ns=0,
        end_ns=9000,
        protocol=int(Protocol.HTTP1),
        route_id=9,
        status=500,
        terminal=int(TerminalStatus.ERROR),
        error_class=3,
    )
    proj = Projector(rec)
    for _ in range(3):
        proj.poll()
    return proj


@pytest.mark.asyncio
async def test_capabilities_include_projection_only_with_projector(tmp_path) -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE)
    without = await serve_inspector(rec, _app(), InspectorConfig(path=str(tmp_path / "a.sock")))
    try:
        async with InspectorClient(without.path) as client:
            caps = (await client.hello())["capabilities"]
        assert "TIMELINE" not in caps
        assert "ACTIVE_REQUESTS" in caps
    finally:
        await without.close()

    with_proj = await serve_inspector(
        rec,
        _app(),
        InspectorConfig(path=str(tmp_path / "b.sock")),
        projector=_fed_projector(),
    )
    try:
        async with InspectorClient(with_proj.path) as client:
            caps = (await client.hello())["capabilities"]
        assert {"TIMELINE", "RECENT_FAILURES", "ROUTE_DISTRIBUTIONS"} <= set(caps)
    finally:
        await with_proj.close()


@pytest.mark.asyncio
async def test_timeline_lists_recent_traces_newest_first(tmp_path) -> None:
    proj = _fed_projector()
    server = await serve_inspector(
        proj._recorder,
        _app(),
        InspectorConfig(path=str(tmp_path / "wfi.sock")),
        projector=proj,
    )
    try:
        async with InspectorClient(server.path) as client:
            body = await client.timeline(limit=10)
    finally:
        await server.close()

    assert body["assembled"] == 4
    assert body["total"] == 4
    assert len(body["traces"]) == 4
    # Newest first: the 500/error request was recorded last.
    assert body["traces"][0]["status"] == 500
    assert body["traces"][0]["is_failure"] is True
    assert "loss" in body


def test_trace_payload_names_ai_scraping_refusals() -> None:
    trace = ProjectedTrace(
        request_id=1,
        connection_id=2,
        route_id=0,
        plan_id=0,
        worker_id=0,
        duration_us=3,
        status=403,
        terminal=TerminalStatus.OK,
        protocol=Protocol.HTTP1,
        error_class=0,
        flags=FLAG_POLICY_REFUSED | FLAG_AI_SCRAPING_REFUSED,
        bytes_in=0,
        bytes_out=0,
    )
    assert _trace_payload(trace)["policy_disposition"] == "ai_scraping"


@pytest.mark.asyncio
async def test_recent_failures_only_returns_failures(tmp_path) -> None:
    proj = _fed_projector()
    server = await serve_inspector(
        proj._recorder,
        _app(),
        InspectorConfig(path=str(tmp_path / "wfi.sock")),
        projector=proj,
    )
    try:
        async with InspectorClient(server.path) as client:
            body = await client.recent_failures()
    finally:
        await server.close()

    assert body["total"] == 1
    (failure,) = body["traces"]
    assert failure["status"] == 500
    assert failure["terminal"] == "error"
    assert failure["error_class"] == 3


@pytest.mark.asyncio
async def test_route_distributions_aggregate_per_route(tmp_path) -> None:
    proj = _fed_projector()
    server = await serve_inspector(
        proj._recorder,
        _app(),
        InspectorConfig(path=str(tmp_path / "wfi.sock")),
        projector=proj,
    )
    try:
        async with InspectorClient(server.path) as client:
            body = await client.route_distributions()
    finally:
        await server.close()

    by_route = {r["route_id"]: r for r in body["routes"]}
    assert by_route[7]["count"] == 3
    assert by_route[7]["errors"] == 0
    assert by_route[9]["count"] == 1
    assert by_route[9]["errors"] == 1
    assert len(by_route[9]["buckets"]) == 64


@pytest.mark.asyncio
async def test_projection_commands_error_without_projector(tmp_path) -> None:
    server = await serve_inspector(
        _flight.Recorder(_flight.MODE_PULSE),
        _app(),
        InspectorConfig(path=str(tmp_path / "wfi.sock")),
    )
    try:
        async with InspectorClient(server.path) as client:
            with pytest.raises(InspectorError, match="projection is not enabled"):
                await client.call(Command.TIMELINE)
    finally:
        await server.close()


def test_cli_renders_projection_topics(tmp_path, capsys) -> None:
    path = str(tmp_path / "wfi.sock")
    proj = _fed_projector()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(
        serve_inspector(proj._recorder, _app(), InspectorConfig(path=path), projector=proj),
        loop,
    ).result(5)
    try:
        assert cli_main(["inspect", path, "timeline", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["topic"] == "timeline"
        assert data["data"]["assembled"] == 4

        assert cli_main(["inspect", path, "failures"]) == 0
        out = capsys.readouterr().out
        assert "failure(s)" in out
        assert "error" in out

        assert cli_main(["inspect", path, "distributions"]) == 0
        assert "route distributions" in capsys.readouterr().out

        assert (
            cli_main(
                [
                    "inspect",
                    path,
                    "metadata",
                    "--table",
                    "components",
                    "--json",
                ]
            )
            == 0
        )
        metadata = json.loads(capsys.readouterr().out)
        assert metadata["data"]["table"] == "components"
    finally:
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
