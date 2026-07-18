"""Stage 0 Native Flight Recorder: schema, codec, metadata image, config, C parity.

No runtime telemetry exists yet; these tests pin the wire schema, the
deterministic metadata image, config validation, and byte-for-byte parity
between the Python schema and its C mirror header.
"""

from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

import pytest

from wreath import _flight_schema as fs
from wreath._flight_metadata import build_metadata_image
from wreath._pure import flight as codec
from wreath.recording import (
    BodyCapture,
    CaptureBudget,
    RecordingPolicy,
    RecordingPolicyError,
    RedactionPolicy,
)
from wreath.telemetry import (
    HistogramConfig,
    Mode,
    PerRoutePolicy,
    TelemetryConfig,
    TelemetryConfigError,
)

_NATIVE = Path(__file__).parents[1] / "src" / "wreath" / "_native"


# --- cell codecs ------------------------------------------------------------


def test_completion_cell_round_trips() -> None:
    cell = fs.CompletionCell(
        request_id=2**63 + 7,
        connection_id=12345,
        route_id=9,
        plan_id=4,
        duration_us=1_234_567,
        status=200,
        bytes_in=512,
        bytes_out=65_536,
        protocol=fs.Protocol.HTTP2,
        terminal=fs.TerminalStatus.OK,
        error_class=0,
        worker_id=3,
        flags=fs.FLAG_SAMPLED | fs.FLAG_PROPAGATION_VALID,
    )
    raw = cell.encode()
    assert len(raw) == fs.CELL_SIZE
    assert fs.CompletionCell.decode(raw) == cell


def test_correlation_cell_round_trips_128_bit_trace() -> None:
    cell = fs.CorrelationCell(
        request_id=99,
        trace_id=0x0123456789ABCDEF_FEDCBA9876543210,
        span_id=0xA1B2C3D4E5F60718,
        parent_span_id=0x1122334455667788,
        flags=fs.FLAG_SAMPLED,
    )
    assert fs.CorrelationCell.decode(cell.encode()) == cell


def test_completion_decode_rejects_bad_input() -> None:
    good = fs.CompletionCell(1, 1, 1, 1, 1, 200, 0, 0).encode()
    with pytest.raises(fs.SchemaError):
        fs.CompletionCell.decode(good[:-1])  # truncated
    bad_version = bytes([2]) + good[1:]
    with pytest.raises(fs.SchemaError):
        fs.CompletionCell.decode(bad_version)
    bad_kind = good[:1] + bytes([fs.EventKind.PHASE]) + good[2:]
    with pytest.raises(fs.SchemaError):
        fs.CompletionCell.decode(bad_kind)


def test_phase_record_round_trips() -> None:
    record = fs.PhaseRecord(
        phase_id=fs.PhaseKind.DB_QUERY,
        duration_us=4321,
        start_offset_us=99,
        dependency_id=17,
        coverage=fs.PhaseCoverage.EXTERNAL,
        sequence=7,
    )
    assert fs.PhaseRecord.decode(record.encode()) == record
    assert len(record.encode()) == fs.PHASE_CELL_SIZE


def test_phase_batch_cell_round_trips_and_is_64_bytes() -> None:
    records = tuple(
        fs.PhaseRecord(phase_id=fs.PhaseKind(k), duration_us=k * 10, sequence=k)
        for k in range(1, fs.PHASE_RECORDS_PER_BATCH + 1)
    )
    cell = fs.PhaseBatchCell(request_id=1234, records=records, worker_id=5)
    encoded = cell.encode()
    assert len(encoded) == fs.CELL_SIZE
    assert fs.PhaseBatchCell.decode(encoded) == cell


def test_phase_batch_cell_rejects_bad_input() -> None:
    good = fs.PhaseBatchCell(
        request_id=1, records=(fs.PhaseRecord(fs.PhaseKind.HANDLER, 1),)
    ).encode()
    with pytest.raises(fs.SchemaError):
        fs.PhaseBatchCell.decode(good[:-1])  # truncated
    bad_kind = good[:1] + bytes([fs.EventKind.COMPLETION]) + good[2:]
    with pytest.raises(fs.SchemaError):
        fs.PhaseBatchCell.decode(bad_kind)
    bad_count = good[:2] + bytes([fs.PHASE_RECORDS_PER_BATCH + 1]) + good[3:]
    with pytest.raises(fs.SchemaError):
        fs.PhaseBatchCell.decode(bad_count)
    # A batch with too many / zero records cannot be encoded.
    with pytest.raises(fs.SchemaError):
        fs.PhaseBatchCell(request_id=1, records=()).encode()


def test_histogram_bucket_is_monotonic_and_clamped() -> None:
    assert fs.histogram_bucket(0) == 0
    assert fs.histogram_bucket(1) == 0
    assert fs.histogram_bucket(2) == 1
    assert fs.histogram_bucket(1023) == 9
    assert fs.histogram_bucket(1 << 70) == fs.HISTOGRAM_BUCKETS - 1
    prev = -1
    for us in (1, 4, 16, 256, 4096, 1_000_000):
        bucket = fs.histogram_bucket(us)
        assert bucket >= prev
        prev = bucket


# --- metadata image determinism --------------------------------------------


def _image(routes: list[fs.RouteMeta]) -> fs.MetadataImage:
    return fs.MetadataImage(
        version=fs.METADATA_VERSION,
        routes=tuple(routes),
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(),
        models=(),
    )


def _route(route_id: int, method: str, path: str) -> fs.RouteMeta:
    return fs.RouteMeta(
        route_id=route_id,
        method=method,
        path=path,
        operation_id=f"{method.lower()}_{path.strip('/')}",
        plan_id=0,
        tags=(),
        dependency_ids=(),
        middleware_ids=(),
        auth_policy_id=0,
    )


def test_metadata_image_is_order_independent() -> None:
    a = _image([_route(1, "GET", "/a"), _route(2, "GET", "/b")])
    b = _image([_route(2, "GET", "/b"), _route(1, "GET", "/a")])
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.image_hash() == b.image_hash()


def test_metadata_hash_changes_on_semantic_change() -> None:
    base = _image([_route(1, "GET", "/a")])
    changed = _image([_route(1, "GET", "/different")])
    assert base.image_hash() != changed.image_hash()
    assert len(base.image_hash()) == 32
    assert len(base.image_hash_short()) == fs.IMAGE_HASH_BYTES


# --- container codec --------------------------------------------------------


def test_container_round_trips_with_events() -> None:
    image = _image([_route(1, "GET", "/a")])
    events = (
        fs.CompletionCell(1, 1, 1, 0, 100, 200, 0, 0).encode(),
        fs.CorrelationCell(1, trace_id=42, span_id=7).encode(),
    )
    blob = codec.encode_recording(image, events)
    decoded = codec.decode_recording(blob)
    assert decoded.image.canonical_bytes() == image.canonical_bytes()
    assert decoded.events == events


def test_container_round_trips_metadata_only() -> None:
    image = _image([_route(1, "GET", "/a"), _route(2, "POST", "/b")])
    decoded = codec.decode_recording(codec.encode_recording(image))
    assert decoded.image.canonical_bytes() == image.canonical_bytes()
    assert decoded.events == ()


def test_container_rejects_corruption_and_truncation() -> None:
    image = _image([_route(1, "GET", "/a")])
    blob = codec.encode_recording(image)

    with pytest.raises(codec.SchemaError):
        codec.decode_recording(b"XXXX" + blob[4:])  # bad magic
    with pytest.raises(codec.SchemaError):
        codec.decode_recording(blob[:8])  # truncated header
    with pytest.raises(codec.SchemaError):
        codec.decode_recording(blob[:-1])  # truncated final chunk
    # Flip a byte inside the metadata payload -> CRC mismatch.
    corrupt = bytearray(blob)
    corrupt[-1] ^= 0xFF
    with pytest.raises(codec.SchemaError):
        codec.decode_recording(bytes(corrupt))


def test_metadata_image_rejects_trailing_bytes() -> None:
    image = _image([_route(1, "GET", "/a")])
    with pytest.raises(codec.SchemaError):
        codec.decode_metadata_image(image.canonical_bytes() + b"junk")


# --- config validation ------------------------------------------------------


def test_default_config_is_off_and_valid() -> None:
    config = TelemetryConfig()
    assert config.mode is Mode.OFF
    assert config.memory_budget(route_count=100).total >= 0


def test_config_rejects_invalid_mode_and_ring() -> None:
    with pytest.raises(TelemetryConfigError):
        TelemetryConfig(mode=99)  # type: ignore[arg-type]
    with pytest.raises(TelemetryConfigError):
        TelemetryConfig(mode=Mode.PULSE, ring_records=10_000)  # not power of two


def test_forensic_requires_capture_slabs() -> None:
    with pytest.raises(TelemetryConfigError):
        TelemetryConfig(mode=Mode.FORENSIC, capture_slabs=0)
    ok = TelemetryConfig(mode=Mode.FORENSIC, capture_slabs=8, slab_bytes=4096)
    assert ok.memory_budget().capture == 8 * 4096


def test_capped_histograms_reject_excess_cardinality() -> None:
    config = TelemetryConfig(
        mode=Mode.PULSE,
        histograms=HistogramConfig(per_route=PerRoutePolicy.CAPPED, max_route_histograms=10),
    )
    assert config.memory_budget(route_count=10).histograms > 0
    with pytest.raises(TelemetryConfigError):
        config.memory_budget(route_count=11)


def test_selected_and_global_histogram_counts() -> None:
    glob = HistogramConfig(per_route=PerRoutePolicy.GLOBAL)
    assert glob.histogram_count(1000) == 1
    sel = HistogramConfig(per_route=PerRoutePolicy.SELECTED, max_route_histograms=5)
    assert sel.histogram_count(1000) == 6


def test_memory_budget_components_are_exact() -> None:
    config = TelemetryConfig(
        mode=Mode.DETAILED, ring_records=1024, active_requests=100, phase_slots=64
    )
    budget = config.memory_budget(route_count=0)
    assert budget.ring == 1024 * fs.CELL_SIZE
    # Phase scratch is one batch-cell block per phase slot: BUDGET / RECORDS_PER_BATCH
    # cells of 64 bytes each. It scales with phase_slots, not active_requests.
    phase_block_bytes = (fs.PHASE_CELL_BUDGET // fs.PHASE_RECORDS_PER_BATCH) * fs.CELL_SIZE
    assert budget.phase_scratch == 64 * phase_block_bytes
    assert budget.total == (
        budget.active_slots
        + budget.ring
        + budget.histograms
        + budget.phase_scratch
        + budget.capture
        + budget.export_queue
    )


# --- recording policy (deny-by-default) -------------------------------------


def test_forbidden_headers_cannot_be_allowlisted() -> None:
    with pytest.raises(RecordingPolicyError):
        RedactionPolicy(header_allowlist=frozenset({"Authorization"}))
    with pytest.raises(RecordingPolicyError):
        RedactionPolicy(header_allowlist=frozenset({"cookie"}))
    ok = RedactionPolicy(header_allowlist=frozenset({"X-Trace"}))
    assert ok.header_allowlist == frozenset({"x-trace"})


def test_capture_ceiling_bounds_runtime_arms() -> None:
    ceiling = RecordingPolicy(
        capture_slabs=8, max_capture_bytes=8 * 4096,
        redaction=RedactionPolicy(body=BodyCapture.HASHED),
    )
    from wreath.recording import CapturePolicy

    within = CapturePolicy(
        redaction=RedactionPolicy(body=BodyCapture.METADATA),
        budget=CaptureBudget(slabs=4, slab_bytes=4096),
    )
    beyond = CapturePolicy(
        redaction=RedactionPolicy(body=BodyCapture.METADATA),
        budget=CaptureBudget(slabs=64, slab_bytes=4096),
    )
    assert ceiling.permits(within)
    assert not ceiling.permits(beyond)


# --- metadata builder from a real app ---------------------------------------


def _demo_app(order: str = "abc"):
    from wreath import Wreath

    app = Wreath()

    async def home(request):
        return {"page": "home"}

    async def user(request, uid: str):
        return {"uid": uid}

    async def create(request, uid: str):
        return {"created": uid}

    routes = {
        "a": lambda: app.get("/")(home),
        "b": lambda: app.get("/users/{uid}")(user),
        "c": lambda: app.post("/users/{uid}")(create),
    }
    for key in order:
        routes[key]()
    return app


def test_metadata_builder_is_deterministic_across_order() -> None:
    first = build_metadata_image(_demo_app("abc"))
    second = build_metadata_image(_demo_app("cba"))
    assert first.image_hash() == second.image_hash()
    # Round-trips through the container unchanged.
    decoded = codec.decode_recording(codec.encode_recording(first))
    assert decoded.image.image_hash() == first.image_hash()


def test_metadata_builder_covers_routes_and_plans() -> None:
    image = build_metadata_image(_demo_app())
    paths = {(r.method, r.path) for r in image.routes}
    assert ("GET", "/") in paths
    assert ("GET", "/users/{uid}") in paths
    assert ("POST", "/users/{uid}") in paths
    # Typed handlers produce a plan with the path param bound.
    user_route = next(r for r in image.routes if r.path == "/users/{uid}" and r.method == "GET")
    plan = next(p for p in image.plans if p.plan_id == user_route.plan_id)
    assert any(name == "uid" for name, _kind, _type in plan.params)
    assert user_route.coverage == "mixed"


# --- C / Python schema parity ----------------------------------------------


def _header_text() -> str:
    return (_NATIVE / "flight_schema.h").read_text()


def test_c_header_defines_match_python() -> None:
    text = _header_text()

    def define(name: str) -> int:
        match = re.search(rf"#define {name}\s+(\d+)", text)
        assert match, f"missing #define {name}"
        return int(match.group(1))

    assert define("WREATH_NFR_SCHEMA_VERSION") == fs.SCHEMA_VERSION
    assert define("WREATH_NFR_METADATA_VERSION") == fs.METADATA_VERSION
    assert define("WREATH_NFR_CELL_SIZE") == fs.CELL_SIZE
    assert define("WREATH_NFR_PHASE_CELL_SIZE") == fs.PHASE_CELL_SIZE
    assert define("WREATH_NFR_PHASE_CELL_BUDGET") == fs.PHASE_CELL_BUDGET
    assert define("WREATH_NFR_PHASE_RECORDS_PER_BATCH") == fs.PHASE_RECORDS_PER_BATCH
    assert define("WREATH_NFR_HISTOGRAM_BUCKETS") == fs.HISTOGRAM_BUCKETS
    assert define("WREATH_NFR_IMAGE_HASH_BYTES") == fs.IMAGE_HASH_BYTES

    # Flag bits: `#define NAME (1u << n)`.
    def flag(name: str) -> int:
        match = re.search(rf"#define {name} \(1u << (\d+)\)", text)
        assert match, f"missing flag {name}"
        return 1 << int(match.group(1))

    assert flag("WREATH_NFR_FLAG_SAMPLED") == fs.FLAG_SAMPLED
    assert flag("WREATH_NFR_FLAG_HAS_CORRELATION") == fs.FLAG_HAS_CORRELATION


def test_c_enums_match_python() -> None:
    text = _header_text()

    def enum_value(name: str) -> int:
        match = re.search(rf"{name} = (\d+)", text)
        assert match, f"missing enum {name}"
        return int(match.group(1))

    assert enum_value("WREATH_NFR_KIND_COMPLETION") == fs.EventKind.COMPLETION
    assert enum_value("WREATH_NFR_KIND_CORRELATION") == fs.EventKind.CORRELATION
    assert enum_value("WREATH_NFR_MODE_FORENSIC") == fs.Mode.FORENSIC
    assert enum_value("WREATH_NFR_PROTO_HTTP3") == fs.Protocol.HTTP3
    assert enum_value("WREATH_NFR_TERM_TIMEOUT") == fs.TerminalStatus.TIMEOUT
    assert enum_value("WREATH_NFR_LOSS_BODY_TRUNCATED") == fs.LossReason.BODY_TRUNCATED


def test_c_struct_layout_matches_python(tmp_path: Path) -> None:
    """Compile a probe that prints sizeof/offsetof and compare to the Python
    struct offsets. This is the byte-for-byte layout guarantee."""
    import shutil

    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if cc is None:  # pragma: no cover - CI always has a compiler
        pytest.skip("no C compiler available")

    probe = tmp_path / "probe.c"
    probe.write_text(
        '#include <stddef.h>\n'
        '#include <stdio.h>\n'
        f'#include "{(_NATIVE / "flight_schema.h").as_posix()}"\n'
        "int main(void) {\n"
        '    printf("cell=%zu\\n", sizeof(wreath_nfr_completion_cell));\n'
        '    printf("corr=%zu\\n", sizeof(wreath_nfr_correlation_cell));\n'
        '    printf("phase=%zu\\n", sizeof(wreath_nfr_phase_cell));\n'
        '    printf("phasebatch=%zu\\n", sizeof(wreath_nfr_phase_batch_cell));\n'
        '    printf("batch_request_id=%zu\\n",'
        ' offsetof(wreath_nfr_phase_batch_cell, request_id));\n'
        '    printf("batch_records=%zu\\n",'
        ' offsetof(wreath_nfr_phase_batch_cell, records));\n'
        '    printf("phase_start=%zu\\n", offsetof(wreath_nfr_phase_cell, start_offset_us));\n'
        '    printf("phase_dur=%zu\\n", offsetof(wreath_nfr_phase_cell, duration_us));\n'
        '    printf("status=%zu\\n", offsetof(wreath_nfr_completion_cell, status));\n'
        '    printf("request_id=%zu\\n", offsetof(wreath_nfr_completion_cell, request_id));\n'
        '    printf("duration_us=%zu\\n", offsetof(wreath_nfr_completion_cell, duration_us));\n'
        '    printf("bytes_out=%zu\\n", offsetof(wreath_nfr_completion_cell, bytes_out));\n'
        '    printf("protocol=%zu\\n", offsetof(wreath_nfr_completion_cell, protocol));\n'
        '    printf("trace_lo=%zu\\n", offsetof(wreath_nfr_correlation_cell, trace_id_lo));\n'
        "    return 0;\n"
        "}\n"
    )
    binary = tmp_path / "probe"
    subprocess.run(
        [cc, "-std=c11", str(probe), "-o", str(binary)], check=True, capture_output=True
    )
    output = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    values = dict(line.split("=") for line in output.strip().splitlines())

    assert int(values["cell"]) == fs.CELL_SIZE
    assert int(values["corr"]) == fs.CELL_SIZE
    assert int(values["phase"]) == fs.PHASE_CELL_SIZE
    assert int(values["phasebatch"]) == fs.CELL_SIZE
    # The batch header is 16 bytes; records begin where the header ends.
    assert int(values["batch_request_id"]) == 8
    assert int(values["batch_records"]) == fs.PHASE_CELL_SIZE
    assert int(values["phase_start"]) == 8
    assert int(values["phase_dur"]) == 12
    # Offsets must match the Python struct format exactly.
    assert int(values["status"]) == 4
    assert int(values["request_id"]) == 8
    assert int(values["duration_us"]) == 32
    assert int(values["bytes_out"]) == 48
    assert int(values["protocol"]) == 56
    assert int(values["trace_lo"]) == 24


def test_python_struct_sizes_are_sixty_four() -> None:
    # A guard that the Python packing itself never drifts from the cell budget.
    assert struct.calcsize(fs._COMPLETION.format) == fs.CELL_SIZE
    assert struct.calcsize(fs._CORRELATION.format) == fs.CELL_SIZE
    assert struct.calcsize(fs._PHASE_RECORD.format) == fs.PHASE_CELL_SIZE
    assert (
        struct.calcsize(fs._PHASE_BATCH_HEADER.format)
        + fs.PHASE_RECORDS_PER_BATCH * struct.calcsize(fs._PHASE_RECORD.format)
        == fs.CELL_SIZE
    )
