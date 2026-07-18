"""The native complexity linter must actually catch things.

A linter nobody has watched catch a defect is decoration. Each rule is driven
against a fixture that contains the pattern it was written for, plus the
false-positive shapes that previously fooled it: prose in comments, patterns in
string literals, one-time cached imports, and a single-line loop body.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from wreath._devtools.native_lint import RULES, main, scan_text


def _codes(source: str) -> list[str]:
    return [f.code for f in scan_text("fixture.c", source)]


def test_front_deletion_is_reported() -> None:
    assert "NC001" in _codes("""
static int drain(Foo *self) {
    if (PySequence_DelItem(self->q, 0) < 0) return -1;
    return 0;
}
""")


def test_front_slice_is_reported() -> None:
    assert "NC001" in _codes("""
static int drain(Foo *self) {
    PyList_SetSlice(self->q, 0, 1, NULL);
    return 0;
}
""")


def test_removal_inside_a_forward_loop_is_reported() -> None:
    assert "NC002" in _codes("""
static int drain(Foo *self) {
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(self->q); i++) {
        if (PySequence_DelItem(self->q, i) < 0) return -1;
    }
    return 0;
}
""")


def test_reverse_removal_is_not_reported() -> None:
    """Reverse iteration is the correct idiom and must stay quiet."""
    assert "NC002" not in _codes("""
static int drain(Foo *self) {
    for (Py_ssize_t i = PyList_GET_SIZE(self->q) - 1; i >= 0; i--) {
        if (PySequence_DelItem(self->q, i) < 0) return -1;
    }
    return 0;
}
""")


def test_additive_growth_is_reported() -> None:
    assert "NC003" in _codes("""
static int grow(Buf *b) {
    Py_ssize_t capacity = b->capacity + 64;
    b->data = PyMem_Realloc(b->data, (size_t)capacity);
    return 0;
}
""")


def test_geometric_growth_is_not_reported() -> None:
    assert "NC003" not in _codes("""
static int grow(Buf *b, Py_ssize_t need) {
    Py_ssize_t capacity = b->capacity > 0 ? b->capacity : 256;
    while (capacity < need) capacity *= 2;
    b->data = PyMem_Realloc(b->data, (size_t)capacity);
    return 0;
}
""")


def test_import_in_a_per_value_function_is_reported() -> None:
    assert "NC004" in _codes("""
static PyObject *decode_bytea(const unsigned char *d, Py_ssize_t n) {
    PyObject *m = PyImport_ImportModule("binascii");
    return m;
}
""")


def test_import_at_module_init_is_not_reported() -> None:
    assert "NC004" not in _codes("""
static int wreath_pg_codec_init(PyObject *module) {
    PyObject *m = PyImport_ImportModule("datetime");
    return 0;
}
""")


def test_lazily_cached_import_is_not_reported() -> None:
    """A static-guarded import runs once for the process; that is the fix."""
    assert "NC004" not in _codes("""
static PyObject *stdlib_loads(PyObject *arg) {
    static PyObject *loads = NULL;
    if (loads == NULL) {
        PyObject *module = PyImport_ImportModule("json");
        loads = PyObject_GetAttrString(module, "loads");
    }
    return PyObject_CallOneArg(loads, arg);
}
""")


def test_method_dispatch_in_a_loop_is_reported() -> None:
    assert "NC005" in _codes("""
static int cancel_all(Foo *self) {
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *r = PyObject_CallMethod(items[i], "cancel", NULL);
    }
    return 0;
}
""")


def test_single_line_loop_does_not_leak_depth() -> None:
    """A `for (...) { ... }` on one line must not mark the rest of the function.

    This exact shape (`for (...) { if (...) { q = i; break; } }`) made the first
    version of the depth tracker report every later call as being in a loop.
    """
    assert "NC005" not in _codes("""
static int scan(Foo *self) {
    for (Py_ssize_t i = 0; i < pl; i++) { if (pp[i] == '?') { q = i; break; } }
    PyObject *r = PyObject_CallMethod(self->x, "y", NULL);
    return 0;
}
""")


def test_rescan_from_zero_in_a_parser_is_reported() -> None:
    assert "NC006" in _codes("""
static int drive_head(Proto *self) {
    Py_ssize_t end = find_sub(p, n, "\\r\\n\\r\\n", 4);
    return 0;
}
""")


def test_resumable_scan_is_not_reported() -> None:
    assert "NC006" not in _codes("""
static int drive_head(Proto *self) {
    Py_ssize_t end = find_sub_from(p, n, "\\r\\n\\r\\n", 4, &self->head_scan);
    return 0;
}
""")


def test_const_table_fromstring_is_reported() -> None:
    assert "NC007" in _codes("""
static int resolve(Table *t, Py_ssize_t i, PyObject **name) {
    *name = PyBytes_FromString(STATIC_NAMES[i - 1]);
    return 0;
}
""")


def test_cached_static_table_is_not_reported() -> None:
    """Handing out a reference to a prebuilt object is the fix, not the defect."""
    assert "NC007" not in _codes("""
static int resolve(Table *t, Py_ssize_t i, PyObject **name) {
    *name = Py_NewRef(static_name_objects[i - 1]);
    return 0;
}
""")


def test_plain_literal_fromstring_is_not_reported() -> None:
    """A one-off literal is not the per-lookup table pattern NC007 targets."""
    assert "NC007" not in _codes("""
static PyObject *host_name(void) {
    return PyBytes_FromString("host");
}
""")


def test_patterns_in_comments_are_not_reported() -> None:
    """Several real comments describe these patterns; prose must not fire."""
    assert _codes("""
/* Replaced PySequence_DelItem(list, 0) with a head index, because deleting
 * index 0 shifts the whole list. See also PyImport_ImportModule notes. */
static int fine(Foo *self) {
    return 0;
}
""") == []


def test_patterns_in_string_literals_are_not_reported() -> None:
    assert _codes("""
static const char *doc = "PySequence_DelItem(x, 0) is quadratic";
""") == []


def test_waiver_suppresses_its_rule() -> None:
    assert _codes("""
static int drain(Foo *self) {
    /* native-lint: allow NC001 -- bounded: at most four spare slabs. */
    if (PySequence_DelItem(self->spares, 0) < 0) return -1;
    return 0;
}
""") == []


def test_waiver_only_suppresses_the_named_rule() -> None:
    codes = _codes("""
static int drain(Foo *self) {
    /* native-lint: allow NC005 -- unrelated rule. */
    if (PySequence_DelItem(self->q, 0) < 0) return -1;
    return 0;
}
""")
    assert "NC001" in codes


def test_waiver_without_a_reason_is_itself_a_finding() -> None:
    """A waiver has to say what it waives and why."""
    assert "NC000" in _codes("""
static int drain(Foo *self) {
    /* native-lint: allow */
    if (PySequence_DelItem(self->q, 0) < 0) return -1;
    return 0;
}
""")


def test_every_rule_has_a_hint() -> None:
    for rule in RULES.values():
        assert rule.hint.strip(), rule.code
        assert rule.summary.strip(), rule.code


def test_the_native_tree_is_clean() -> None:
    """The committed C must stay free of these patterns.

    When this fails, either fix the pattern or waive it in place with a reason
    that says why the bound is acceptable.
    """
    assert main([]) == 0, "wreath-native-lint reported findings in src/neo/_native"


@pytest.mark.parametrize("args", [["--list-rules"], ["--format", "json"]])
def test_cli_entrypoint_runs(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "wreath._devtools.native_lint", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
