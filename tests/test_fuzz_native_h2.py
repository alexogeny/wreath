from __future__ import annotations

from pathlib import Path

HARNESS = Path(__file__).parents[1] / "tools/fuzz_native/h2_harness.c"


def test_h2_native_harness_drives_bounded_native_protocol_input() -> None:
    source = HARNESS.read_text()

    assert "LLVMFuzzerInitialize" in source
    assert "LLVMFuzzerTestOneInput" in source
    assert '"wreath._native._server"' in source
    assert '"Http2Protocol"' in source
    assert "data_received(" in source
    assert r"PRI * HTTP/2.0\\r\\n\\r\\nSM\\r\\n\\r\\n" in source
    assert "size > 65536" in source
    assert "if (result == NULL) wreath_fuzz_abort_python();" in source


def test_h2_native_harness_keeps_python_faults_visible() -> None:
    source = HARNESS.read_text()

    assert "wreath_fuzz_abort_python" in source
    assert "PyErr_ExceptionMatches" not in source
