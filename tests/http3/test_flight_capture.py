"""Stage 5 forensic capture over HTTP/3, end-to-end over real QUIC.

Like HTTP/2, a Wreath app dispatches HTTP/3 through the native ``_RequestContext``
scope and the ``native_response`` fast path, so the whole capture surface works on
HTTP/3 with no protocol-specific code. These tests stand up a real Forensic HTTP/3
server, arm capture through the Inspector, drive a QUIC request with ``curl
--http3``, and assert on the ``WFR1`` recording — the HTTP/3 analog of
``tests/test_flight_capture_live.py``.
"""

from __future__ import annotations

import pytest

import wreath
from wreath._flight_schema import CaptureDisposition, CaptureFieldClass
from wreath._recording_format import read_recording
from wreath.inspector import InspectorClient, InspectorConfig
from wreath.recording import BodyCapture, RecordingPolicy, RedactionPolicy
from wreath.server import ServerConfig, TLSConfig, serve
from wreath.telemetry import Mode, SamplingPolicy, TelemetryConfig

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3

pytestmark = [requires_curl_h3, pytest.mark.network, pytest.mark.asyncio]

TOKEN = "capture-token-abcdef123456"


def _app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        response = wreath.response.TextResponse("pong")
        response.headers.append((b"x-custom", b"response-marker"))
        return response

    return app


def _config(sock: str, wfr1: str, redaction: RedactionPolicy) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1", port=0, lifespan="off", protocols=("h3",),
        telemetry=TelemetryConfig(
            mode=Mode.FORENSIC, ring_records=256, active_requests=32,
            detailed=SamplingPolicy(rate=1.0), capture_slabs=16, slab_bytes=4096,
        ),
        inspector=InspectorConfig(path=sock, capture_token=TOKEN),
        recording=RecordingPolicy(
            capture_slabs=16, max_capture_bytes=1 << 20, redaction=redaction
        ),
        recording_path=wfr1,
    )


async def _serve(app: wreath.Wreath, config: ServerConfig):
    cert, key = make_self_signed_cert()
    server = await serve(app, config, tls=TLSConfig(cert, key))
    return server, server.datagram_addresses[0][1]


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


async def test_h3_armed_request_captures_headers_to_wfr1(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    redaction = RedactionPolicy(
        header_allowlist=frozenset({"x-trace"}),
        header_hash=frozenset({"x-request-id"}),
        body=BodyCapture.NONE,
    )
    server, port = await _serve(_app(), _config(sock, wfr1, redaction))
    try:
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                redaction={
                    "header_allowlist": ["x-trace"],
                    "header_hash": ["x-request-id"],
                    "body": "none",
                },
                expiry_seconds=60,
            )
        rc, out = await curl_http3(
            port, "/ping",
            "-H", "X-Trace: trace-abc-123",
            "-H", "X-Request-Id: req-9f8e7d",
            "-H", "Authorization: Bearer super-secret",
        )
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"pong"
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.clean
    request_headers = [
        f for slab in decoded.slabs for f in slab.fields
        if f.field_class is CaptureFieldClass.REQUEST_HEADER
    ]
    raw = {f.payload for f in request_headers if f.disposition is CaptureDisposition.RAW}
    hashed = [f for f in request_headers if f.disposition is CaptureDisposition.HASHED]
    assert b"trace-abc-123" in raw
    assert hashed and all(len(f.payload) == 8 for f in hashed)
    # Deny-by-default: forbidden and hashed values never entered the file verbatim.
    blob = _read(wfr1)
    assert b"super-secret" not in blob
    assert b"req-9f8e7d" not in blob


async def test_h3_captures_response_headers(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    redaction = RedactionPolicy(header_allowlist=frozenset({"x-custom"}), body=BodyCapture.NONE)
    server, port = await _serve(_app(), _config(sock, wfr1, redaction))
    try:
        async with InspectorClient(sock) as client:
            await client.arm_capture(
                token=TOKEN,
                redaction={"header_allowlist": ["x-custom"], "body": "none"},
                expiry_seconds=60,
            )
        rc, out = await curl_http3(port, "/ping")
        assert rc == 0
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    response_headers = [
        f for slab in decoded.slabs for f in slab.fields
        if f.field_class is CaptureFieldClass.RESPONSE_HEADER
    ]
    assert any(
        f.disposition is CaptureDisposition.RAW and f.payload == b"response-marker"
        for f in response_headers
    )


async def test_h3_unarmed_forensic_request_captures_nothing(tmp_path) -> None:
    sock = str(tmp_path / "wfi.sock")
    wfr1 = str(tmp_path / "flight.wfr1")
    redaction = RedactionPolicy(header_allowlist=frozenset({"x-trace"}), body=BodyCapture.NONE)
    server, port = await _serve(_app(), _config(sock, wfr1, redaction))
    try:
        # No arm installed: capture is off even though the request is sampled.
        rc, out = await curl_http3(port, "/ping", "-H", "X-Trace: t")
        assert rc == 0
    finally:
        await server.close()
        await server.wait_closed()

    decoded = read_recording(_read(wfr1))
    assert decoded.slabs == ()
