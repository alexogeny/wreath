from __future__ import annotations

import asyncio

import pytest

import wreath
from wreath._flight_schema import CaptureDisposition, CaptureFieldClass
from wreath._recording_format import read_recording
from wreath.inspector import InspectorClient, InspectorConfig
from wreath.recording import BodyCapture, RecordingPolicy, RedactionPolicy
from wreath.server import ServerConfig, serve
from wreath.telemetry import Mode, SamplingPolicy, TelemetryConfig

# exc_type=ImportError, not the default: an extension that is present but refuses
# to initialise raises plain ImportError, and pytest only auto-skips on
# ModuleNotFoundError -- so without this these modules fail collection instead of
# skipping as the docstring says.
pytest.importorskip("wreath._native._server", exc_type=ImportError)
pytest.importorskip("wreath._native._flight", exc_type=ImportError)

TOKEN = "capture-token-abcdef123456"


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    return app


def _config(sock: str, wfr1: str) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC,
            ring_records=256,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16,
            slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16,
            max_capture_bytes=1 << 20,
            redaction=RedactionPolicy(
                header_allowlist=frozenset({"x-trace"}),
                header_hash=frozenset({"x-request-id"}),
                body=BodyCapture.HASHED,
            ),
        ),
        recording_path=wfr1,
    )


async def _get(port: int, path: str, extra_headers: str = "") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: x\r\n{extra_headers}Connection: close\r\n\r\n".encode()
    )
    await writer.drain()
    body = await asyncio.wait_for(reader.read(), timeout=2.0)
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError, BrokenPipeError:
        pass
    return body


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


@pytest.mark.asyncio
async def test_armed_forensic_request_captures_headers_to_wfr1(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_app(), _config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        # Arm capture (capture is gated on an active arm).
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                # The arm requests exactly what it asserts below; per-arm
                # narrowing means an arm captures only its own subset of the
                # ceiling, not the whole ceiling.
                redaction={
                    "header_allowlist": ["x-trace"],
                    "header_hash": ["x-request-id"],
                    "body": "hashed",
                },
                expiry_seconds=60,
            )
        # A request carrying an allowlisted header, a hashed header, a forbidden
        # header, and an unlisted header.
        headers = (
            "X-Trace: trace-abc-123\r\n"
            "X-Request-Id: req-9f8e7d\r\n"
            "Authorization: Bearer super-secret-token\r\n"
            "User-Agent: curl/8.0\r\n"
        )
        assert b"pong" in await _get(port, "/ping", headers)
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    assert len(decoded.slabs) >= 1
    headers_class = CaptureFieldClass.REQUEST_HEADER
    fields = [f for slab in decoded.slabs for f in slab.fields if f.field_class is headers_class]
    by_desc = {f.descriptor_id: f for f in fields}
    # x-trace captured verbatim (RAW); x-request-id hashed (never plaintext).
    raw_values = {f.payload for f in by_desc.values() if f.disposition is CaptureDisposition.RAW}
    assert b"trace-abc-123" in raw_values
    hashed = [f for f in by_desc.values() if f.disposition is CaptureDisposition.HASHED]
    assert hashed and all(len(f.payload) == 8 for f in hashed)
    # Deny-by-default: forbidden + unlisted headers never entered the file.
    blob = _read(wfr1)
    for secret in (b"super-secret-token", b"curl/8.0", b"req-9f8e7d"):
        assert secret not in blob


@pytest.mark.asyncio
async def test_unarmed_forensic_request_captures_nothing(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_app(), _config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        # No arm installed: capture is off even though the request is sampled.
        assert b"pong" in await _get(port, "/ping", "X-Trace: t\r\n")
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.slabs == ()  # nothing captured without an active arm


def _resp_header_config(sock: str, wfr1: str) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC,
            ring_records=256,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16,
            slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16,
            max_capture_bytes=1 << 20,
            redaction=RedactionPolicy(
                # One allowlisted response header; content-type is deliberately
                # left out to prove deny-by-default on the response side.
                header_allowlist=frozenset({"x-custom"}),
                body=BodyCapture.NONE,
            ),
        ),
        recording_path=wfr1,
    )


def _resp_header_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        response = wreath.response.TextResponse("pong")
        response.headers.append((b"x-custom", b"response-marker"))
        return response

    return app


@pytest.mark.asyncio
async def test_armed_request_captures_response_headers(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_resp_header_app(), _resp_header_config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                redaction={"header_allowlist": ["x-custom"], "body": "none"},
                expiry_seconds=60,
            )
        assert b"pong" in await _get(port, "/ping")
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    response_headers = [
        f
        for slab in decoded.slabs
        for f in slab.fields
        if f.field_class is CaptureFieldClass.RESPONSE_HEADER
    ]
    # The allowlisted response header is captured verbatim.
    assert any(
        f.disposition is CaptureDisposition.RAW and f.payload == b"response-marker"
        for f in response_headers
    )
    # Deny-by-default holds for the response side too: the unlisted content-type
    # header never entered the file.
    assert b"text/plain" not in _read(wfr1)


def _query_config(sock: str, wfr1: str) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC,
            ring_records=256,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16,
            slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16,
            max_capture_bytes=1 << 20,
            redaction=RedactionPolicy(
                query_allowlist=frozenset({"user"}),
                query_hash=frozenset({"session"}),
                body=BodyCapture.NONE,
            ),
        ),
        recording_path=wfr1,
    )


@pytest.mark.asyncio
async def test_armed_request_captures_query_parameters(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_app(), _query_config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                redaction={
                    "query_allowlist": ["user"],
                    "query_hash": ["session"],
                    "body": "none",
                },
                expiry_seconds=60,
            )
        assert b"pong" in await _get(port, "/ping?user=alice&session=deadbeef&secret=nope")
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    params = [
        f
        for slab in decoded.slabs
        for f in slab.fields
        if f.field_class is CaptureFieldClass.QUERY_PARAM
    ]
    raw = {f.payload for f in params if f.disposition is CaptureDisposition.RAW}
    hashed = [f for f in params if f.disposition is CaptureDisposition.HASHED]
    assert b"alice" in raw
    assert hashed and all(len(f.payload) == 8 for f in hashed)
    # Deny-by-default: the unlisted and hashed values never appear verbatim.
    blob = _read(wfr1)
    assert b"nope" not in blob
    assert b"deadbeef" not in blob


def _narrowing_config(sock: str, wfr1: str) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC,
            ring_records=256,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16,
            slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16,
            max_capture_bytes=1 << 20,
            # The ceiling permits BOTH headers; the arm below narrows to one.
            redaction=RedactionPolicy(
                header_allowlist=frozenset({"x-one", "x-two"}),
                body=BodyCapture.NONE,
            ),
        ),
        recording_path=wfr1,
    )


@pytest.mark.asyncio
async def test_arm_narrows_below_the_ceiling(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_app(), _narrowing_config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        async with InspectorClient(sock) as client:
            # Arm only x-one, though the ceiling would allow x-two too.
            await client.arm_capture(
                token=TOKEN,
                redaction={"header_allowlist": ["x-one"], "body": "none"},
                expiry_seconds=60,
            )
        assert b"pong" in await _get(port, "/ping", "X-One: keep-me\r\nX-Two: drop-me\r\n")
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    raw = {
        f.payload
        for slab in decoded.slabs
        for f in slab.fields
        if f.field_class is CaptureFieldClass.REQUEST_HEADER
    }
    # The arm's header is captured; the ceiling-permitted but un-armed one is not.
    assert b"keep-me" in raw
    assert b"drop-me" not in _read(wfr1)


def _body_config(sock: str, wfr1: str) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC,
            ring_records=256,
            active_requests=32,
            detailed=SamplingPolicy(rate=1.0),
            capture_slabs=16,
            slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16,
            max_capture_bytes=1 << 20,
            redaction=RedactionPolicy(
                # STRUCTURED body -> RAW capture, bounded to 8 bytes (also tests
                # truncation); no headers captured here (deny-by-default).
                body=BodyCapture.STRUCTURED,
                max_body_bytes=8,
                max_fields=16,
                max_depth=8,
            ),
        ),
        recording_path=wfr1,
    )


def _echo_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        body = await request.body()
        return wreath.response.TextResponse(body.decode())

    return app


async def _post(port: int, path: str, body: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    out = await asyncio.wait_for(reader.read(), timeout=2.0)
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError, BrokenPipeError:
        pass
    return out


@pytest.mark.asyncio
async def test_armed_request_captures_bounded_bodies(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    server = await serve(_echo_app(), _body_config(sock, wfr1))
    port = server.sockets[0].getsockname()[1]
    try:
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                redaction={
                    "body": "structured",
                    "max_body_bytes": 8,
                    "max_fields": 16,
                    "max_depth": 8,
                },
                expiry_seconds=60,
            )
        assert b"hello world" in await _post(port, "/echo", b"hello world")
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    fields = [f for slab in decoded.slabs for f in slab.fields]
    kinds = {f.field_class for f in fields}
    assert CaptureFieldClass.REQUEST_BODY in kinds
    assert CaptureFieldClass.RESPONSE_BODY in kinds
    for f in fields:
        if f.field_class in (CaptureFieldClass.REQUEST_BODY, CaptureFieldClass.RESPONSE_BODY):
            # RAW, bounded to 8 bytes, marked truncated (original was 11).
            assert f.disposition is CaptureDisposition.RAW
            assert f.payload == b"hello wo"
            assert f.original_length == 11
            assert f.truncated
