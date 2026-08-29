from __future__ import annotations

import asyncio

import pytest

import wreath
from wreath._flight_schema import CaptureDisposition, CaptureFieldClass, CaptureSlab
from wreath.recording import (
    ArmRegistry,
    CaptureBudget,
    CapturePolicy,
    RecordingPolicy,
    RedactionPolicy,
    compile_redaction,
)
from wreath.server import ServerConfig

from . import support
from .conftest import FakeTransport, _settle

_flight = pytest.importorskip("wreath._native._flight")
try:
    from wreath._native._server import Http2Protocol
except ImportError:  # pragma: no cover -- the native h2 build is optional
    Http2Protocol = None

pytestmark = [
    pytest.mark.skipif(Http2Protocol is None, reason="native h2 not built"),
    pytest.mark.asyncio,
]


def _forensic_app(
    ceiling_redaction: RedactionPolicy, arm_redaction: RedactionPolicy | None
) -> wreath.Wreath:
    app = wreath.Wreath()

    @app.get("/x")
    async def get_x(request: wreath.Request) -> wreath.Response:
        response = wreath.response.TextResponse("ok")
        response.headers.append((b"x-custom", b"response-marker"))
        return response

    @app.post("/echo")
    async def echo(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse((await request.body()).decode())

    app._compile_routes()
    app._build_flight_route_ids()
    ceiling = RecordingPolicy(
        capture_slabs=8, max_capture_bytes=1 << 20, redaction=ceiling_redaction
    )
    registry = ArmRegistry(ceiling)
    if arm_redaction is not None:
        registry.arm(
            CapturePolicy(
                redaction=arm_redaction,
                budget=CaptureBudget(slabs=1, slab_bytes=4096),
                expiry_seconds=60,
            )
        )
    app._set_flight_recording(compile_redaction(ceiling_redaction), registry)
    return app


async def _drive_capture(
    app: wreath.Wreath,
    *,
    method: bytes = b"GET",
    path: bytes = b"/x",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> list[CaptureSlab]:
    rec = _flight.Recorder(
        _flight.MODE_FORENSIC,
        ring_records=256,
        active_requests=16,
        capture_slabs=8,
        slab_bytes=4096,
        detailed_sample_rate=1.0,
    )
    loop = asyncio.get_event_loop()
    protocol = Http2Protocol(app, ServerConfig(protocols=("h2",)), loop, set(), recorder=rec)
    protocol.connection_made(FakeTransport())
    await _settle()
    protocol.data_received(support.PREFACE)
    protocol.data_received(support.encode_settings({}))
    await _settle()
    headers = support.request_headers(method=method, path=path, extra=extra_headers)
    protocol.data_received(support.build_headers_frame(1, headers, end_stream=not body))
    if body:
        protocol.data_received(support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, body))
    await _settle()
    return [CaptureSlab.decode(s) for s in rec.drain_captures()]


def _fields(slabs: list[CaptureSlab], field_class: CaptureFieldClass) -> list:
    return [f for slab in slabs for f in slab.fields if f.field_class is field_class]


async def test_h2_armed_request_captures_headers_by_policy() -> None:
    app = _forensic_app(
        RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}), header_hash=frozenset({"x-request-id"})
        ),
        RedactionPolicy(
            header_allowlist=frozenset({"x-trace"}), header_hash=frozenset({"x-request-id"})
        ),
    )
    slabs = await _drive_capture(
        app,
        extra_headers=[
            (b"x-trace", b"trace-abc-123"),
            (b"x-request-id", b"req-9f8e7d"),
            (b"authorization", b"Bearer super-secret"),
            (b"user-agent", b"curl/8"),
        ],
    )
    request_headers = _fields(slabs, CaptureFieldClass.REQUEST_HEADER)
    raw = {f.payload for f in request_headers if f.disposition is CaptureDisposition.RAW}
    hashed = [f for f in request_headers if f.disposition is CaptureDisposition.HASHED]
    assert b"trace-abc-123" in raw
    assert hashed and all(len(f.payload) == 8 for f in hashed)
    # Deny-by-default: forbidden and unlisted headers never entered a slab.
    blob = b"".join(f.payload for slab in slabs for f in slab.fields)
    assert b"super-secret" not in blob
    assert b"curl/8" not in blob
    assert b"req-9f8e7d" not in blob  # hashed, never verbatim


async def test_h2_unarmed_forensic_request_captures_nothing() -> None:
    app = _forensic_app(RedactionPolicy(header_allowlist=frozenset({"x-trace"})), None)
    slabs = await _drive_capture(app, extra_headers=[(b"x-trace", b"t")])
    assert slabs == []


async def test_h2_captures_response_headers() -> None:
    from wreath.recording import BodyCapture

    redaction = RedactionPolicy(header_allowlist=frozenset({"x-custom"}), body=BodyCapture.NONE)
    app = _forensic_app(redaction, redaction)
    slabs = await _drive_capture(app)
    response_headers = _fields(slabs, CaptureFieldClass.RESPONSE_HEADER)
    assert any(
        f.disposition is CaptureDisposition.RAW and f.payload == b"response-marker"
        for f in response_headers
    )


async def test_h2_captures_bounded_request_and_response_bodies() -> None:
    from wreath.recording import BodyCapture

    redaction = RedactionPolicy(
        body=BodyCapture.STRUCTURED, max_body_bytes=8, max_fields=8, max_depth=4
    )
    app = _forensic_app(redaction, redaction)
    slabs = await _drive_capture(app, method=b"POST", path=b"/echo", body=b"hello world")
    kinds = {f.field_class for slab in slabs for f in slab.fields}
    assert CaptureFieldClass.REQUEST_BODY in kinds
    assert CaptureFieldClass.RESPONSE_BODY in kinds
    for f in _fields(slabs, CaptureFieldClass.REQUEST_BODY):
        assert f.disposition is CaptureDisposition.RAW
        assert f.payload == b"hello wo"  # bounded to 8 bytes
        assert f.original_length == 11
        assert f.truncated


async def test_h2_arm_narrows_below_the_ceiling() -> None:
    # Ceiling allows two headers; the arm allows only one -> only that one captured.
    app = _forensic_app(
        RedactionPolicy(header_allowlist=frozenset({"x-one", "x-two"})),
        RedactionPolicy(header_allowlist=frozenset({"x-one"})),
    )
    slabs = await _drive_capture(
        app, extra_headers=[(b"x-one", b"keep-me"), (b"x-two", b"drop-me")]
    )
    raw = {f.payload for f in _fields(slabs, CaptureFieldClass.REQUEST_HEADER)}
    assert b"keep-me" in raw
    assert b"drop-me" not in b"".join(f.payload for slab in slabs for f in slab.fields)
