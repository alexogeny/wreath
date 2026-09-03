from __future__ import annotations

import shutil
import subprocess
import sysconfig
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
HARNESS = PROJECT_ROOT / "tools/fuzz_native/graphql_harness.c"


def test_graphql_harness_compiles_and_calls_the_native_parser_boundary(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang")
    assert compiler is not None, "the native fuzz toolchain requires clang"

    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Werror",
            "-fsyntax-only",
            f"-I{sysconfig.get_config_var('INCLUDEPY')}",
            f"-I{HARNESS.parent}",
            str(HARNESS),
            "-o",
            str(tmp_path / "graphql_harness.o"),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    source = HARNESS.read_text(encoding="utf-8")
    assert 'PyImport_ImportModule("wreath._native._core")' in source
    assert 'PyObject_GetAttrString(module, "graphql_parse")' in source
    assert 'PyObject_GetAttrString(module, "_CONFIG")' in source
    assert 'PyObject_GetAttrString(module, "GraphQLSyntaxError")' in source
    assert "PyUnicode_DecodeUTF8" in source
    assert "PyExc_UnicodeDecodeError" in source
    assert (
        "state->graphql_parse, source, state->limits, state->config, NULL)"
        in source
    )
    assert "size > 16384" in source
