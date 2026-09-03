from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
HARNESS_ROOT = ROOT / "tools/fuzz_native"


def test_native_harnesses_have_no_mutable_process_global_python_state() -> None:
    declarations: list[str] = []
    for harness in sorted(HARNESS_ROOT.glob("*_harness.c")):
        source = harness.read_text(encoding="utf-8")
        for match in re.finditer(r"^static\s+PyObject\s*\*.*;$", source, re.MULTILINE):
            declarations.append(f"{harness.name}: {match.group()}")

    assert declarations == []


def test_harness_state_is_owned_by_the_current_interpreter() -> None:
    source = (HARNESS_ROOT / "harness.c").read_text(encoding="utf-8")

    assert "PyInterpreterState_GetDict(PyInterpreterState_Get())" in source
    assert "PyCapsule_New" in source
    assert "PyCapsule_GetPointer" in source


def test_harness_state_has_explicit_cleanup_and_missing_state_is_fatal() -> None:
    common = (HARNESS_ROOT / "harness.c").read_text(encoding="utf-8")
    assert "Py_XDECREF(state->function);" in common
    assert "Py_XDECREF(state->refusal);" in common
    assert "PyMem_Free(state);" in common
    assert 'PyExc_RuntimeError, "fuzz harness state %s is unavailable"' in common
    assert "wreath_fuzz_abort_python();" in common

    for name in (
        "graphql_harness.c",
        "h2_harness.c",
        "http_replay_harness.c",
        "multipart_harness.c",
    ):
        source = (HARNESS_ROOT / name).read_text(encoding="utf-8")
        assert "static void\ndestroy_state(PyObject *capsule)" in source
        assert "PyMem_Free(state);" in source
