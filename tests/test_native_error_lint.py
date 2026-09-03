from __future__ import annotations

from wreath._devtools.native_error_lint import (
    DEFAULT_ROOTS,
    WAIVER,
    iter_sources,
    repo_root,
    scan_text,
)


def codes(source: str) -> list[str]:
    return [finding.code for finding in scan_text("fixture.c", source)]


def test_ignored_integer_status_is_reported() -> None:
    assert "NE001" in codes("""
static int broken(PyObject *d, PyObject *k, PyObject *v) {
    PyDict_SetItem(d, k, v);
    return 0;
}
""")


def test_discarded_new_reference_is_reported() -> None:
    assert "NE002" in codes("""
static int broken(PyObject *callable) {
    PyObject_CallNoArgs(callable);
    return 0;
}
""")


def test_null_return_after_clearing_error_is_reported() -> None:
    assert "NE003" in codes("""
static PyObject *broken(void) {
    PyErr_Clear();
    return NULL;
}
""")


def test_blank_lines_do_not_erase_the_previous_error_operation() -> None:
    assert "NE003" in codes("""
static PyObject *broken(void) {
    PyErr_Clear();

    return NULL;
}
""")


def test_pyobject_return_type_is_found_beyond_the_nearby_signature_window() -> None:
    assert "NE003" in codes("""
static PyObject *broken(void) {
    int one = 1;
    int two = 2;
    int three = 3;
    int four = 4;
    int five = one + two + three + four;
    PyErr_Clear();
    return NULL;
}
""")


def test_success_return_with_exception_set_is_reported() -> None:
    assert "NE004" in codes("""
static PyObject *broken(void) {
    PyErr_SetString(PyExc_ValueError, "bad");
    Py_RETURN_NONE;
}
""")


def test_ambiguous_conversion_without_error_check_is_reported() -> None:
    assert "NE005" in codes("""
static PyObject *broken(PyObject *value) {
    long result = PyLong_AsLong(value);
    return PyLong_FromLong(result + 1);
}
""")


def test_a_later_function_cannot_supply_the_conversion_error_check() -> None:
    assert "NE005" in codes("""
static long broken(PyObject *value) {
    long result = PyLong_AsLong(value);
    return 0;
}
static int unrelated(void) {
    return PyErr_Occurred() ? -1 : 0;
}
""")


def test_using_a_conversion_result_before_the_error_check_is_reported() -> None:
    assert "NE005" in codes("""
static long broken(PyObject *value) {
    long result = PyLong_AsLong(value);
    consume(result);
    if (PyErr_Occurred()) return -1;
    return result;
}
""")


def test_minus_one_conversion_with_error_check_is_accepted() -> None:
    assert "NE005" not in codes("""
static PyObject *safe(PyObject *value) {
    long result = PyLong_AsLong(value);
    if (result == -1 && PyErr_Occurred()) return NULL;
    return PyLong_FromLong(result + 1);
}
""")


def test_python_boolean_error_is_not_treated_as_true() -> None:
    assert "NE006" in codes("""
static int broken(PyObject *value) {
    if (PyObject_IsTrue(value)) return 1;
    return 0;
}
""")


def test_multiline_assignment_is_not_mistaken_for_a_discarded_call() -> None:
    assert "NE002" not in codes("""
static int safe(PyObject *factory, State *self) {
    self->future = factory == NULL ? NULL :
        PyObject_CallNoArgs(factory);
    return self->future == NULL ? -1 : 0;
}
""")


def test_non_python_pointer_may_use_null_as_a_no_error_sentinel() -> None:
    assert "NE003" not in codes("""
static State *find_state(void) {
    PyErr_Clear();
    return NULL;
}
""")


def test_checked_status_is_accepted() -> None:
    assert (
        codes("""
static int safe(PyObject *d, PyObject *k, PyObject *v) {
    if (PyDict_SetItem(d, k, v) < 0) return -1;
    return 0;
}
""")
        == []
    )


def test_waiver_requires_and_accepts_a_reason() -> None:
    assert "NE001" not in codes("""
static int intentional(PyObject *d, PyObject *k, PyObject *v) {
    /* native-error-lint: allow NE001 -- best-effort diagnostic cache */
    PyDict_SetItem(d, k, v);
    return 0;
}
""")


def _native_sources() -> list:
    return iter_sources([repo_root() / root for root in DEFAULT_ROOTS])


def test_native_tree_has_no_error_protocol_findings() -> None:
    root = repo_root()
    findings = []
    for source in _native_sources():
        text = source.read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(str(source.relative_to(root)), text))
    assert findings == [], "\n".join(finding.render() for finding in findings)


def test_native_tree_has_no_bare_waivers() -> None:
    root = repo_root()
    bare = []
    for source in _native_sources():
        for number, line in enumerate(
            source.read_text(encoding="utf-8", errors="replace").split("\n"), 1
        ):
            match = WAIVER.search(line)
            if match and len(match.group("reason").split()) < 3:
                bare.append(f"{source.relative_to(root)}:{number}: {line.strip()}")
    assert bare == [], "\n".join(bare)
