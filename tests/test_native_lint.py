from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest
from _native_lint_reference import loop_depth_map as _loop_depth_map_reference
from _native_lint_reference import strip_c as _strip_c_reference

from wreath._devtools.native_lint import (
    RULES,
    _c_tape,
    _enclosing_function,
    _function_map,
    main,
    scan_text,
)


def _codes(source: str) -> list[str]:
    return [f.code for f in scan_text("fixture.c", source)]


def test_native_c_tape_matches_the_independent_python_definition() -> None:
    pieces = (
        "static int f(void) {\n",
        "for (int i = 0; i < 3; i++) {\n",
        "while ((item = next()) != NULL) { call(item); }\n",
        "/* block { for (;;) and λ */\n",
        "// line } while (0)\n",
        'const char *s = "escaped \\\\" // still a string";\n',
        "char c = '\\\\'';\n",
        "if (ready) { use(); }\n",
        "}\n",
        "",
    )
    rng = random.Random(20260826)
    corpus = ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 24))) for _ in range(256)]
    corpus.extend(("", "\n", "for (;;) {\n}\n", "for\n(;;)\n{\n}\n"))

    for source in corpus:
        expected_lines = _strip_c_reference(source)
        expected_depth = _loop_depth_map_reference(expected_lines)
        assert _c_tape(source) == (expected_lines, expected_depth)
        assert _function_map(expected_lines) == [
            _enclosing_function(expected_lines, index) for index in range(len(expected_lines))
        ]


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
    assert "NC007" not in _codes("""
static int resolve(Table *t, Py_ssize_t i, PyObject **name) {
    *name = Py_NewRef(static_name_objects[i - 1]);
    return 0;
}
""")


def test_plain_literal_fromstring_is_not_reported() -> None:
    assert "NC007" not in _codes("""
static PyObject *host_name(void) {
    return PyBytes_FromString("host");
}
""")


def test_patterns_in_comments_are_not_reported() -> None:
    assert (
        _codes("""
/* Replaced PySequence_DelItem(list, 0) with a head index, because deleting
 * index 0 shifts the whole list. See also PyImport_ImportModule notes. */
static int fine(Foo *self) {
    return 0;
}
""")
        == []
    )


def test_patterns_in_string_literals_are_not_reported() -> None:
    assert (
        _codes("""
static const char *doc = "PySequence_DelItem(x, 0) is quadratic";
""")
        == []
    )


def test_waiver_suppresses_its_rule() -> None:
    assert (
        _codes("""
static int drain(Foo *self) {
    /* native-lint: allow NC001 -- bounded: at most four spare slabs. */
    if (PySequence_DelItem(self->spares, 0) < 0) return -1;
    return 0;
}
""")
        == []
    )


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
    assert main([]) == 0, "wreath-native-lint reported findings in src/wreath/_native"


@pytest.mark.parametrize(
    ("args", "needs_source"),
    [(["--list-rules"], False), (["--format", "json"], True)],
)
def test_cli_entrypoint_runs(args: list[str], needs_source: bool, tmp_path: Path) -> None:
    paths: list[str] = []
    if needs_source:
        source = tmp_path / "clean.c"
        source.write_text("static int clean(void) { return 0; }\n", encoding="utf-8")
        paths.append(str(source))
    result = subprocess.run(
        [sys.executable, "-m", "wreath._devtools.native_lint", *paths, *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_forward_loop_detection_does_not_leak_past_the_loop() -> None:
    assert "NC002" not in _codes("""
static int drain(Foo *self, Py_ssize_t index) {
    for (Py_ssize_t i = 0; i < 2; i++) {
        inspect(i);
    }
    return PySequence_DelItem(self->q, index);
}
""")


def test_additive_arithmetic_without_reallocation_is_not_growth() -> None:
    assert "NC003" not in _codes("""
static Py_ssize_t next_capacity(Buf *b) {
    Py_ssize_t capacity = b->capacity + 64;
    return capacity;
}
""")


def test_module_scope_import_is_not_a_per_value_import() -> None:
    assert "NC004" not in _codes('PyObject *json = PyImport_ImportModule("json");\n')


def test_static_import_cache_requires_its_null_guard() -> None:
    assert "NC004" in _codes("""
static PyObject *loads = NULL;
static PyObject *decode(PyObject *arg) {
    PyObject *module = PyImport_ImportModule("json");
    loads = PyObject_GetAttrString(module, "loads");
    return PyObject_CallOneArg(loads, arg);
}
""")


def test_cli_list_rules_is_a_distinct_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--list-rules"]) == 0
    output = capsys.readouterr()

    assert "NC001  front deletion from a list" in output.out
    assert "finding(s) across" not in output.out
    assert output.err == ""


def test_cli_refuses_an_empty_source_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path)]) == 1
    output = capsys.readouterr()

    assert output.out == ""
    assert "no C sources found" in output.err


def test_cli_json_shape_and_clean_text_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "clean.c"
    clean.write_text("static int clean(void) { return 0; }\n", encoding="utf-8")

    assert main(["--format", "json", str(clean)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"scanned": 1, "findings": []}

    assert main([str(clean)]) == 0
    output = capsys.readouterr().out
    assert "0 finding(s) across 1 file(s)" in output
    assert "native-lint: allow NC001" in output


def test_cli_findings_set_failure_without_clean_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.c"
    broken.write_text(
        "static int drain(PyObject *q) { return PySequence_DelItem(q, 0); }\n",
        encoding="utf-8",
    )

    assert main([str(broken)]) == 1
    output = capsys.readouterr().out
    assert "NC001" in output
    assert "native-lint: allow NC001" not in output
