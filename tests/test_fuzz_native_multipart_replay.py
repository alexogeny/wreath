from __future__ import annotations

import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("name", ("multipart_harness.c", "http_replay_harness.c"))
def test_harness_compiles_as_c11(name: str, tmp_path: Path) -> None:
    compiler = shutil.which("clang")
    assert compiler is not None, "the native fuzz toolchain requires clang"
    harness = ROOT / "tools/fuzz_native" / name

    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Werror",
            "-fsyntax-only",
            f"-I{sysconfig.get_config_var('INCLUDEPY')}",
            f"-I{harness.parent}",
            str(harness),
            "-o",
            str(tmp_path / f"{harness.stem}.o"),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("name", "native_calls"),
    (
        ("multipart_harness.c", ("multipart_parse",)),
        (
            "http_replay_harness.c",
            ("http_exchange_decode", "http_exchange_encode"),
        ),
    ),
)
def test_harness_calls_native_boundary_and_bounds_input(
    name: str, native_calls: tuple[str, ...]
) -> None:
    source = (ROOT / "tools/fuzz_native" / name).read_text()

    assert '"wreath._native._core"' in source
    assert "size > 65536" in source
    for native_call in native_calls:
        assert f'"{native_call}"' in source


def test_multipart_harness_bounds_materialized_parser_work() -> None:
    source = (ROOT / "tools/fuzz_native/multipart_harness.c").read_text()

    assert "PyLong_FromLong(64)" in source
    assert "PyLong_FromLong(8192)" in source
    assert "PyLong_FromLong(65536)" in source
    assert "PyExc_ValueError" in source
    assert "PyObject_RichCompareBool" in source


def test_http_replay_harness_keeps_canonicalization_defects_visible() -> None:
    source = (ROOT / "tools/fuzz_native/http_replay_harness.c").read_text()

    assert "canonical = encode(state, exchange);" in source
    assert "reparsed = decode(state, canonical);" in source
    assert "second = encode(state, reparsed);" in source
    assert "PyExc_ValueError" not in source
    assert "PyObject_RichCompareBool" in source
    assert "if (!equal) abort();" in source
