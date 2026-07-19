"""Stage 4c -- end-to-end: a running server drains its ring through the projector.

Before slice 4c nothing consumed the recorder's ring in a live server, so Pulse
completions accumulated and dropped. These serve a real Wreath app over loopback
with telemetry + an Inspector, drive requests, and confirm the projector (started
by the server) reassembled them and the Inspector's TIMELINE reports them.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import wreath
from wreath.inspector import InspectorClient, InspectorConfig
from wreath.server import ServerConfig, serve
from wreath.telemetry import Mode, TelemetryConfig

pytest.importorskip("wreath._native._server")
pytest.importorskip("wreath._native._flight")


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    return app


async def _raw_get(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    body = await asyncio.wait_for(reader.read(), timeout=2.0)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass
    return body


def _read_bytes(path: str) -> bytes:
    """Sync file read (ruff bans blocking file I/O inside an async function)."""
    with open(path, "rb") as fh:
        return fh.read()


async def _wait_for(predicate, within: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@pytest.mark.asyncio
async def test_running_server_projects_completions(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(mode=Mode.PULSE, ring_records=256, active_requests=32),
        inspector=InspectorConfig(path=sock),
    )
    server = await serve(_app(), config)
    port = server.sockets[0].getsockname()[1]
    try:
        assert server._projector is not None  # the ring now has a consumer

        for _ in range(5):
            body = await _raw_get(port, "/ping")
            assert b"pong" in body

        async def projected() -> bool:
            async with InspectorClient(sock) as client:
                body = await client.timeline(limit=50)
            return body["assembled"] >= 5

        await _wait_for(projected)

        async with InspectorClient(sock) as client:
            timeline = await client.timeline(limit=50)
            distributions = await client.route_distributions()
        assert timeline["assembled"] >= 5
        assert all(t["terminal"] == "ok" for t in timeline["traces"])
        assert sum(r["count"] for r in distributions["routes"]) >= 5
    finally:
        await server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_server_without_telemetry_creates_no_projector() -> None:
    config = ServerConfig(host="127.0.0.1", port=0, lifespan="off")
    server = await serve(_app(), config)
    try:
        assert server._projector is None
        assert server._recorder is None
    finally:
        await server.close()
        await server.wait_closed()


def _projector_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "wreath-flight-projector"]


@pytest.mark.asyncio
async def test_sustained_load_is_fully_projected(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(mode=Mode.PULSE, ring_records=128, active_requests=64),
        inspector=InspectorConfig(path=sock),
    )
    server = await serve(_app(), config)
    port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(60):
            assert b"pong" in await _raw_get(port, "/ping")

        async def projected() -> bool:
            async with InspectorClient(sock) as client:
                return (await client.timeline(limit=1))["assembled"] >= 60

        await _wait_for(projected)
    finally:
        await server.close()
        await server.wait_closed()
    # The projector kept the ring drained under load: no ring overflow.
    assert server.recorder.completions == 60
    assert server.recorder.loss(0) == 0  # LossReason.RING_FULL


@pytest.mark.asyncio
async def test_startup_abort_leaves_no_projector_thread(tmp_path) -> None:
    # An Inspector path that already exists as a regular (non-socket) file makes
    # serve_inspector raise after the projector has started, forcing _abort_startup.
    bad_path = tmp_path / "not-a-socket"
    bad_path.write_text("x")
    before = len(_projector_threads())
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(mode=Mode.PULSE, ring_records=64, active_requests=8),
        inspector=InspectorConfig(path=str(bad_path)),
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 -- InspectorError from the abort
        await serve(_app(), config)
    # The started projector thread was joined during the abort -- none leaked.
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        if len(_projector_threads()) <= before:
            break
        await asyncio.sleep(0.02)
    assert len(_projector_threads()) <= before


@pytest.mark.asyncio
async def test_projector_stops_cleanly_on_shutdown(tmp_path) -> None:
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(mode=Mode.PULSE, ring_records=64, active_requests=8),
    )
    server = await serve(_app(), config)
    projector = server._projector
    assert projector is not None
    await server.close()
    await server.wait_closed()
    # After shutdown the server drops its references and the thread is joined.
    assert server._projector is None
    assert projector._thread is None


@pytest.mark.asyncio
async def test_forensic_server_runs_sink_and_capture_control(tmp_path) -> None:
    # Stage 5e (server-lifecycle half): a Forensic server allocates the capture
    # pool, starts the WFR1 recording sink, and serves token-gated capture control
    # over the Inspector. The request-path capture seam is not wired yet, so this
    # drives the recorder handle directly to prove the sink stores real slabs.
    import wreath._native._flight as native_flight

    from wreath._flight_schema import CaptureFieldClass, CaptureSlab
    from wreath._recording_format import read_recording
    from wreath.recording import BodyCapture, RecordingPolicy, RedactionPolicy
    from wreath.telemetry import SamplingPolicy

    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    token = "capture-token-abcdef123456"
    config = ServerConfig(
        host="127.0.0.1", port=0, lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC, ring_records=256, active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16, slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=token),
        recording=RecordingPolicy(
            capture_slabs=16, max_capture_bytes=1 << 20,
            redaction=RedactionPolicy(
                header_allowlist=frozenset({"x-trace"}), body=BodyCapture.HASHED
            ),
        ),
        recording_path=wfr1,
    )
    server = await serve(_app(), config)
    try:
        assert server._recording_sink is not None
        assert server._arm_registry is not None
        assert server.recorder.capture_capacity == 16

        # Capture control works over the live socket, behind the token.
        async with InspectorClient(sock) as client:
            caps = (await client.hello())["capabilities"]
            assert "ARM_CAPTURE" in caps
            armed = await client.arm_capture(
                token=token, redaction={"header_allowlist": ["x-trace"]},
                expiry_seconds=60,
            )
            status = await client.capture_status(token=token)
        assert [a["arm_id"] for a in status["arms"]] == [armed["arm_id"]]

        # Drive the recorder directly (stands in for the future request seam) so
        # the sink has real slabs to persist.
        for i in range(4):
            req = server.recorder.begin(protocol=1, start_ns=i)
            req.capture(int(CaptureFieldClass.REQUEST_HEADER), 1,
                        native_flight.CAP_RAW, b"trace-%d" % i)
            req.finish(now_ns=i + 1, status=200)
    finally:
        await server.close()
        await server.wait_closed()

    # The sink flushed a clean WFR1 file on shutdown with the four slabs.
    assert server._recording_sink is None  # dropped after stop
    decoded = read_recording(_read_bytes(wfr1))
    assert decoded.clean
    assert len(decoded.slabs) == 4
    assert all(isinstance(s, CaptureSlab) for s in decoded.slabs)
