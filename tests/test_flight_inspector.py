from __future__ import annotations

import asyncio
import json
import struct
import threading

import pytest

from wreath import Wreath
from wreath._cli import main as cli_main
from wreath.inspector import (
    HEADER,
    MAGIC,
    PROTOCOL_VERSION,
    Command,
    InspectorClient,
    InspectorConfig,
    InspectorError,
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


def _recorder():
    return _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)


@pytest.mark.asyncio
async def test_hello_reports_protocol_and_capabilities(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        async with InspectorClient(server.path) as client:
            hello = await client.hello()
        assert hello["server"] == "wreath"
        assert hello["protocol"] == PROTOCOL_VERSION
        assert "ACTIVE_REQUESTS" in hello["capabilities"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_workers_and_pressure_reflect_recorder_state(tmp_path) -> None:
    recorder = _recorder()
    req = recorder.begin(connection_id=1, protocol=1, start_ns=0)
    server = await serve_inspector(
        recorder, _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        async with InspectorClient(server.path) as client:
            workers = await client.workers()
            pressure = await client.pressure()
        assert len(workers) == 1
        assert workers[0].requests == 1
        assert workers[0].active_count == 1
        assert pressure["active_count"] == 1
        assert pressure["generation"] == 1
        assert pressure["losses"]["ring_full"] == 0
    finally:
        req.finish(now_ns=1000, status=200)
        await server.close()


@pytest.mark.asyncio
async def test_active_requests_lists_in_flight_and_pages(tmp_path) -> None:
    recorder = _recorder()
    requests = [recorder.begin(connection_id=i, protocol=1, start_ns=0) for i in range(5)]
    server = await serve_inspector(
        recorder, _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        async with InspectorClient(server.path) as client:
            rows = await client.active_requests()
            assert len(rows) == 5
            assert {row.request_id for row in rows} == {1, 2, 3, 4, 5}
            assert all(row.protocol == "http1" for row in rows)
            # Paging: a short page carries the truncated flag.
            page = await client.call(Command.ACTIVE_REQUESTS, {"offset": 0, "limit": 2})
            assert len(page["requests"]) == 2
            assert page["total"] == 5
            assert page["truncated"] is True
    finally:
        for req in requests:
            req.finish(now_ns=1000, status=200)
        await server.close()


@pytest.mark.asyncio
async def test_explain_route_and_plan_join_metadata_names(tmp_path) -> None:
    app = _app()
    server = await serve_inspector(
        _recorder(), app, InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        image = server._metadata_image()
        routes_by_id = server._routes_by_id
        assert server._metadata_image() is image
        assert server._routes_by_id is routes_by_id
        async with InspectorClient(server.path) as client:
            route = await client.explain_route(method="GET", path="/widgets/{widget_id}")
            assert route["route_id"] != 0
            assert route["method"] == "GET"
            same = await client.explain_route(route_id=route["route_id"])
            assert same == route
            plan = await client.explain_plan(route["plan_id"])
            assert plan["plan_id"] == route["plan_id"]
            assert ["widget_id", "path", "int"] in plan["params"]
            with pytest.raises(InspectorError, match="route not found"):
                await client.explain_route(route_id=999)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_metadata_pages_with_truncation_flag(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        async with InspectorClient(server.path) as client:
            routes = await client.metadata("routes")
            assert routes["total"] == 1
            assert routes["rows"][0]["path"] == "/widgets/{widget_id}"
            with pytest.raises(InspectorError, match="unknown metadata table"):
                await client.metadata("rings")
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_malformed_magic_gets_one_error_frame_then_close(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(b"NOPE" + bytes(12))
        await writer.drain()
        header = await reader.readexactly(HEADER.size)
        magic, _v, _c, flags, _rid, length = HEADER.unpack(header)
        assert magic == MAGIC
        assert flags & 1  # error frame
        body = json.loads(await reader.readexactly(length))
        assert "bad magic" in body["error"]
        assert await reader.read() == b""  # then the server closed it
        writer.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_oversized_payload_is_rejected(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        huge = HEADER.pack(MAGIC, PROTOCOL_VERSION, 1, 0, 7, 10 * 1024 * 1024)
        writer.write(huge)
        await writer.drain()
        header = await reader.readexactly(HEADER.size)
        _m, _v, _c, flags, rid, length = HEADER.unpack(header)
        assert flags & 1 and rid == 7
        body = json.loads(await reader.readexactly(length))
        assert "too large" in body["error"]
        assert await reader.read() == b""
        writer.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_unknown_command_errors_but_keeps_the_connection(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        async with InspectorClient(server.path) as client:
            with pytest.raises(InspectorError, match="unknown command"):
                await client.call(99)
            # The connection survives a well-framed but unknown command.
            hello = await client.hello()
            assert hello["server"] == "wreath"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disconnect_mid_frame_does_not_wedge_the_server(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        _reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(HEADER.pack(MAGIC, PROTOCOL_VERSION, 1, 0, 1, 32)[:10])
        await writer.drain()
        writer.close()  # gone mid-header, payload never sent
        await asyncio.sleep(0)
        async with InspectorClient(server.path) as client:
            assert (await client.hello())["server"] == "wreath"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_slow_client_is_disconnected_by_the_idle_timeout(tmp_path) -> None:
    server = await serve_inspector(
        _recorder(),
        _app(),
        InspectorConfig(path=str(tmp_path / "wfi.sock"), idle_timeout=0.15),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_refuses_to_replace_a_non_socket_path(tmp_path) -> None:
    path = tmp_path / "wfi.sock"
    path.write_text("precious")
    with pytest.raises(InspectorError, match="not a socket"):
        await serve_inspector(_recorder(), _app(), InspectorConfig(path=str(path)))
    assert path.read_text() == "precious"  # the file was not touched


@pytest.mark.asyncio
async def test_socket_is_owner_only(tmp_path) -> None:
    import os
    import stat as stat_module

    server = await serve_inspector(
        _recorder(), _app(), InspectorConfig(path=str(tmp_path / "wfi.sock"))
    )
    try:
        mode = os.lstat(server.path).st_mode
        assert stat_module.S_ISSOCK(mode)
        assert not (mode & 0o077)  # no group/other access
    finally:
        await server.close()


def test_cli_inspect_summary_and_json(tmp_path, capsys) -> None:
    # The CLI is a protocol client: point it at a live socket served from a
    # background loop and read what it prints. No application import happens.
    path = str(tmp_path / "wfi.sock")
    recorder = _recorder()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(
        serve_inspector(recorder, _app(), InspectorConfig(path=path)), loop
    ).result(5)
    try:
        assert cli_main(["inspect", path, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["version"] == 1
        assert data["data"]["workers"][0]["mode"] == int(_flight.MODE_PULSE)

        assert cli_main(["inspect", path]) == 0
        human = capsys.readouterr().out
        assert "wreath inspector" in human
        assert "phases: 0/0" in human  # Pulse reserves no phase pool

        assert cli_main(["inspect", path, "routes"]) == 0
        assert "/widgets/{widget_id}" in capsys.readouterr().out
    finally:
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def test_cli_inspect_reports_unreachable_socket(tmp_path, capsys) -> None:
    rc = cli_main(["inspect", str(tmp_path / "missing.sock")])
    assert rc == 1
    assert "cannot reach inspector" in capsys.readouterr().err


def test_config_rejects_bad_limits() -> None:
    with pytest.raises(ValueError):
        InspectorConfig(path="")
    with pytest.raises(ValueError):
        InspectorConfig(path="/tmp/x.sock", max_payload_bytes=0)
    with pytest.raises(ValueError):
        InspectorConfig(path="/tmp/x.sock", idle_timeout=0)


def test_frame_header_is_sixteen_bytes() -> None:
    # The wire header is part of the versioned contract.
    assert HEADER.size == 16
    assert struct.calcsize("!4sBBHII") == 16


@pytest.mark.asyncio
async def test_server_starts_and_stops_the_inspector_with_telemetry(tmp_path) -> None:
    # ServerConfig wiring: telemetry + inspector => a live socket for the run,
    # gone after close. Without telemetry the inspector config binds nothing.
    import os

    from wreath.server import ServerConfig, serve
    from wreath.telemetry import Mode, TelemetryConfig

    path = str(tmp_path / "wfi.sock")
    app = _app()
    config = ServerConfig(
        port=0,
        telemetry=TelemetryConfig(mode=Mode.PULSE),
        inspector=InspectorConfig(path=path),
    )
    server = await serve(app, config)
    try:
        async with InspectorClient(path) as client:
            hello = await client.hello()
            assert hello["server"] == "wreath"
    finally:
        await server.close()
    assert "wfi.sock" not in os.listdir(tmp_path)  # removed on shutdown

    quiet = ServerConfig(port=0, inspector=InspectorConfig(path=path))
    silent_server = await serve(app, quiet)
    try:
        assert "wfi.sock" not in os.listdir(tmp_path)  # no recorder, no bind
    finally:
        await silent_server.close()
