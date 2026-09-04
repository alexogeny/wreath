from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

import pytest

from wreath import _flight_schema as fs

_NATIVE = Path(__file__).parents[1] / "src" / "wreath" / "_native"


def test_log_cell_round_trips() -> None:
    cell = fs.LogCell(
        request_id=2**63 + 11,
        site_id=4242,
        severity=fs.Severity.WARN,
        offset_ms=123_456,
        worker_id=3,
        args=(fs.LogArg.integer(-17), fs.LogArg.text("denied")),
        flags=fs.LOG_FLAG_PROMOTED,
        dropped_siblings=9,
    )
    raw = cell.encode()
    assert len(raw) == fs.CELL_SIZE
    assert fs.LogCell.decode(raw) == cell


def test_log_cell_is_a_ring_cell() -> None:
    assert struct.calcsize(fs._LOG.format) == fs.CELL_SIZE
    assert fs.EventKind.LOG not in {
        fs.EventKind.COMPLETION,
        fs.EventKind.CORRELATION,
        fs.EventKind.PHASE,
        fs.EventKind.CONTROL,
        fs.EventKind.CAPTURE,
    }


def test_log_cell_without_a_request_is_valid() -> None:
    cell = fs.LogCell(request_id=0, site_id=1, severity=fs.Severity.INFO)
    assert fs.LogCell.decode(cell.encode()).request_id == 0


def test_log_cell_decode_rejects_a_short_buffer() -> None:
    cell = fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO)
    with pytest.raises(fs.SchemaError, match="needs exactly 64 bytes"):
        fs.LogCell.decode(cell.encode()[:32])


def test_log_cell_decode_rejects_a_tailed_buffer() -> None:
    cell = fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO)
    with pytest.raises(fs.SchemaError, match="exactly 64 bytes"):
        fs.LogCell.decode(cell.encode() + b"another-cell")


def test_log_cell_decode_rejects_another_kind() -> None:
    other = fs.CorrelationCell(request_id=1, trace_id=2, span_id=3).encode()
    with pytest.raises(fs.SchemaError, match="expected log kind"):
        fs.LogCell.decode(other)


def test_log_cell_decode_rejects_a_foreign_schema_version() -> None:
    raw = bytearray(fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO).encode())
    raw[0] = fs.SCHEMA_VERSION + 1
    with pytest.raises(fs.SchemaError, match="unsupported schema version"):
        fs.LogCell.decode(bytes(raw))


def test_log_cell_decode_rejects_arg_bytes_past_the_inline_area() -> None:
    raw = bytearray(fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO).encode())
    raw[27] = fs.LOG_INLINE_ARG_BYTES + 1  # arg_bytes
    with pytest.raises(fs.SchemaError, match="argument bytes"):
        fs.LogCell.decode(bytes(raw))


def test_log_cell_decode_rejects_a_truncated_argument_payload() -> None:
    cell = fs.LogCell(
        request_id=1, site_id=1, severity=fs.Severity.INFO, args=(fs.LogArg.text("abcdef"),)
    )
    raw = bytearray(cell.encode())
    raw[27] = 3  # arg_bytes now stops mid-payload
    with pytest.raises(fs.SchemaError, match="truncated"):
        fs.LogCell.decode(bytes(raw))


def test_log_cell_decode_rejects_an_unknown_argument_tag() -> None:
    cell = fs.LogCell(
        request_id=1, site_id=1, severity=fs.Severity.INFO, args=(fs.LogArg.integer(1),)
    )
    raw = bytearray(cell.encode())
    raw[32] = 250  # the first argument's type tag
    with pytest.raises(fs.SchemaError, match="argument type"):
        fs.LogCell.decode(bytes(raw))


def test_log_cell_decode_rejects_unknown_flags() -> None:
    raw = bytearray(fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO).encode())
    raw[2:4] = (1 << 15).to_bytes(2, "little")
    with pytest.raises(fs.SchemaError, match="unknown log flags"):
        fs.LogCell.decode(bytes(raw))


@pytest.mark.parametrize(
    "arg",
    [
        fs.LogArg.none(),
        fs.LogArg.boolean(True),
        fs.LogArg.boolean(False),
        fs.LogArg.integer(0),
        fs.LogArg.integer(-1),
        fs.LogArg.integer(2**63 - 1),
        fs.LogArg.integer(-(2**63)),
        fs.LogArg.real(0.0),
        fs.LogArg.real(-1.5),
        fs.LogArg.text(""),
        fs.LogArg.text("hello"),
        fs.LogArg.hashed(0xDEADBEEFCAFEF00D),
        fs.LogArg.length(4096),
    ],
)
def test_log_argument_round_trips(arg: fs.LogArg) -> None:
    cell = fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO, args=(arg,))
    assert fs.LogCell.decode(cell.encode()).args == (arg,)


def test_log_arguments_keep_their_order() -> None:
    args = (fs.LogArg.integer(1), fs.LogArg.text("b"), fs.LogArg.boolean(False))
    cell = fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO, args=args)
    assert fs.LogCell.decode(cell.encode()).args == args


def test_log_text_argument_is_utf8() -> None:
    cell = fs.LogCell(
        request_id=1, site_id=1, severity=fs.Severity.INFO, args=(fs.LogArg.text("héllo"),)
    )
    assert fs.LogCell.decode(cell.encode()).args[0].text_value == "héllo"


def test_log_arguments_that_overflow_the_inline_area_truncate_and_flag() -> None:
    cell = fs.LogCell(
        request_id=1,
        site_id=1,
        severity=fs.Severity.INFO,
        args=(fs.LogArg.text("x" * 40), fs.LogArg.integer(7)),
    )
    decoded = fs.LogCell.decode(cell.encode())
    assert decoded.flags & fs.LOG_FLAG_TRUNCATED
    assert len(decoded.args) < 2


def test_log_argument_count_is_bounded() -> None:
    args = tuple(fs.LogArg.boolean(True) for _ in range(fs.LOG_MAX_ARGS + 4))
    cell = fs.LogCell(request_id=1, site_id=1, severity=fs.Severity.INFO, args=args)
    decoded = fs.LogCell.decode(cell.encode())
    assert len(decoded.args) <= fs.LOG_MAX_ARGS
    assert decoded.flags & fs.LOG_FLAG_TRUNCATED


def test_log_text_argument_longer_than_the_inline_area_is_clipped() -> None:
    cell = fs.LogCell(
        request_id=1,
        site_id=1,
        severity=fs.Severity.INFO,
        args=(fs.LogArg.text("y" * 200),),
    )
    decoded = fs.LogCell.decode(cell.encode())
    assert decoded.flags & fs.LOG_FLAG_TRUNCATED
    if decoded.args:
        assert len(decoded.args[0].text_value.encode()) <= fs.LOG_INLINE_ARG_BYTES


def test_log_text_clipping_never_splits_a_utf8_sequence() -> None:
    cell = fs.LogCell(
        request_id=1,
        site_id=1,
        severity=fs.Severity.INFO,
        args=(fs.LogArg.text("é" * 60),),
    )
    decoded = fs.LogCell.decode(cell.encode())
    if decoded.args:
        clipped = decoded.args[0].text_value  # must not raise UnicodeDecodeError
        assert clipped == "é" * (len(clipped))


def test_severity_numbers_match_the_otel_bands() -> None:
    assert 1 <= fs.Severity.TRACE <= 4
    assert 5 <= fs.Severity.DEBUG <= 8
    assert 9 <= fs.Severity.INFO <= 12
    assert 13 <= fs.Severity.WARN <= 16
    assert 17 <= fs.Severity.ERROR <= 20
    assert 21 <= fs.Severity.FATAL <= 24


def test_severity_text_is_derived_not_stored() -> None:
    assert fs.severity_text(fs.Severity.WARN) == "WARN"
    assert fs.severity_text(fs.Severity.FATAL) == "FATAL"
    # An in-band value the framework never emits still names its band. The
    # stdlib bridge produces these: any level maps into a band, not onto a
    # named member.
    assert fs.severity_text(19) == "ERROR"
    assert fs.severity_text(1) == "TRACE"
    assert fs.severity_text(24) == "FATAL"


@pytest.mark.parametrize(
    ("stdlib_level", "severity"),
    [
        (10, fs.Severity.DEBUG),
        (20, fs.Severity.INFO),
        (30, fs.Severity.WARN),
        (40, fs.Severity.ERROR),
        (50, fs.Severity.FATAL),
    ],
)
def test_stdlib_levels_map_onto_band_midpoints(stdlib_level: int, severity: int) -> None:
    assert fs.severity_from_stdlib(stdlib_level) == severity
    assert fs.severity_to_stdlib(severity) == stdlib_level


def test_stdlib_levels_between_bands_round_down() -> None:
    assert fs.severity_from_stdlib(25) == fs.Severity.INFO
    assert fs.severity_from_stdlib(0) == fs.Severity.TRACE
    assert fs.severity_from_stdlib(100) == fs.Severity.FATAL


def test_log_loss_reasons_are_distinct_and_dense() -> None:
    values = [int(r) for r in fs.LossReason]
    assert values == sorted(set(values))
    assert values == list(range(len(values)))
    for name in (
        "LOG_SCRATCH_FULL",
        "LOG_ARGS_TRUNCATED",
        "LOG_SITE_TABLE_FULL",
        "LOG_SAMPLED",
        "LOG_OFF_LOOP",
    ):
        assert hasattr(fs.LossReason, name), f"missing LossReason.{name}"


def test_log_flags_do_not_collide() -> None:
    flags = (
        fs.LOG_FLAG_PROMOTED,
        fs.LOG_FLAG_TRUNCATED,
        fs.LOG_FLAG_REDACTED,
        fs.LOG_FLAG_OFF_LOOP,
        fs.LOG_FLAG_EVENT_FIELDS,
    )
    assert len({*flags}) == len(flags)
    for flag in flags:
        assert flag.bit_count() == 1


def _header_text() -> str:
    return (_NATIVE / "flight_schema.h").read_text()


def test_c_header_defines_match_python_for_logging() -> None:
    text = _header_text()

    def define(name: str) -> int:
        match = re.search(rf"#define {name}\s+(\d+)", text)
        assert match, f"missing #define {name}"
        return int(match.group(1))

    assert define("WREATH_NFR_LOG_INLINE_ARG_BYTES") == fs.LOG_INLINE_ARG_BYTES
    assert define("WREATH_NFR_LOG_MAX_ARGS") == fs.LOG_MAX_ARGS

    def flag(name: str) -> int:
        match = re.search(rf"#define {name} \(1u << (\d+)\)", text)
        assert match, f"missing flag {name}"
        return 1 << int(match.group(1))

    assert flag("WREATH_NFR_LOG_FLAG_PROMOTED") == fs.LOG_FLAG_PROMOTED
    assert flag("WREATH_NFR_LOG_FLAG_TRUNCATED") == fs.LOG_FLAG_TRUNCATED
    assert flag("WREATH_NFR_LOG_FLAG_REDACTED") == fs.LOG_FLAG_REDACTED
    assert flag("WREATH_NFR_LOG_FLAG_OFF_LOOP") == fs.LOG_FLAG_OFF_LOOP
    assert flag("WREATH_NFR_LOG_FLAG_EVENT_FIELDS") == fs.LOG_FLAG_EVENT_FIELDS


def test_c_enums_match_python_for_logging() -> None:
    text = _header_text()

    def enum_value(name: str) -> int:
        match = re.search(rf"{name} = (\d+)", text)
        assert match, f"missing enum {name}"
        return int(match.group(1))

    assert enum_value("WREATH_NFR_KIND_LOG") == fs.EventKind.LOG
    assert enum_value("WREATH_NFR_LOSS_LOG_SCRATCH_FULL") == fs.LossReason.LOG_SCRATCH_FULL
    assert enum_value("WREATH_NFR_LOSS_LOG_SAMPLED") == fs.LossReason.LOG_SAMPLED
    assert enum_value("WREATH_NFR_LOSS_LOG_OFF_LOOP") == fs.LossReason.LOG_OFF_LOOP
    assert enum_value("WREATH_NFR_LOG_ARG_INT") == fs.LogArgType.INT
    assert enum_value("WREATH_NFR_LOG_ARG_STR") == fs.LogArgType.STR
    assert enum_value("WREATH_NFR_LOG_ARG_HASH") == fs.LogArgType.HASH
    assert enum_value("WREATH_NFR_SEVERITY_WARN") == fs.Severity.WARN


def test_c_loss_reason_count_covers_the_log_reasons() -> None:
    text = _header_text()
    match = re.search(r"WREATH_NFR_LOSS_REASON_COUNT = (\d+)", text)
    assert match, "missing WREATH_NFR_LOSS_REASON_COUNT"
    assert int(match.group(1)) == len(fs.LossReason)


def test_c_log_cell_layout_matches_python(tmp_path: Path) -> None:
    import shutil

    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if cc is None:  # pragma: no cover - CI always has a compiler
        pytest.skip("no C compiler available")

    probe = tmp_path / "probe.c"
    probe.write_text(
        "#include <stddef.h>\n"
        "#include <stdio.h>\n"
        f'#include "{(_NATIVE / "flight_schema.h").as_posix()}"\n'
        "int main(void) {\n"
        '    printf("log=%zu\\n", sizeof(wreath_nfr_log_cell));\n'
        '    printf("site_id=%zu\\n", offsetof(wreath_nfr_log_cell, site_id));\n'
        '    printf("request_id=%zu\\n", offsetof(wreath_nfr_log_cell, request_id));\n'
        '    printf("offset_ms=%zu\\n", offsetof(wreath_nfr_log_cell, offset_ms));\n'
        '    printf("dropped=%zu\\n", offsetof(wreath_nfr_log_cell, dropped_siblings));\n'
        '    printf("severity=%zu\\n", offsetof(wreath_nfr_log_cell, severity));\n'
        '    printf("arg_bytes=%zu\\n", offsetof(wreath_nfr_log_cell, arg_bytes));\n'
        '    printf("args=%zu\\n", offsetof(wreath_nfr_log_cell, args));\n'
        "    return 0;\n"
        "}\n"
    )
    binary = tmp_path / "probe"
    subprocess.run([cc, "-std=c11", str(probe), "-o", str(binary)], check=True, capture_output=True)
    output = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    values = dict(line.split("=") for line in output.strip().splitlines())

    assert int(values["log"]) == fs.CELL_SIZE
    assert int(values["site_id"]) == 4
    assert int(values["request_id"]) == 8
    assert int(values["offset_ms"]) == 16
    assert int(values["dropped"]) == 20
    assert int(values["severity"]) == 24
    assert int(values["arg_bytes"]) == 27
    assert int(values["args"]) == 32
