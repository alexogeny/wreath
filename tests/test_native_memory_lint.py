from __future__ import annotations

from wreath._devtools.native_memory_lint import scan_text


def codes(source: str) -> list[str]:
    return [finding.code for finding in scan_text("example.c", source)]


def test_flags_double_decref() -> None:
    source = """
static void cleanup(PyObject *value) {
    Py_DECREF(value);
    Py_DECREF(value);
}
"""
    assert "NM001" in codes(source)


def test_flags_use_after_decref() -> None:
    source = """
static int consume(PyObject *value) {
    Py_DECREF(value);
    return PyObject_IsTrue(value);
}
"""
    assert "NM002" in codes(source)


def test_flags_pointer_dereference_after_release() -> None:
    source = """
static int consume(Node *value) {
    free(value);
    return value->kind;
}
"""
    assert "NM002" in codes(source)


def test_flags_return_of_freed_pointer() -> None:
    source = """
static char *broken(char *buffer) {
    PyMem_Free(buffer);
    return buffer;
}
"""
    assert "NM003" in codes(source)


def test_flags_decref_of_borrowed_reference() -> None:
    source = """
static void broken(PyObject *mapping, PyObject *key) {
    PyObject *value = PyDict_GetItemWithError(mapping, key);
    Py_DECREF(value);
}
"""
    assert "NM004" in codes(source)


def test_accepts_owned_copy_of_borrowed_reference() -> None:
    source = """
static void safe(PyObject *mapping, PyObject *key) {
    PyObject *value = PyDict_GetItemWithError(mapping, key);
    Py_XINCREF(value);
    Py_XDECREF(value);
}
"""
    assert "NM004" not in codes(source)


def test_accepts_newref_copy_of_borrowed_reference() -> None:
    source = """
static void safe(PyObject *mapping, PyObject *key) {
    PyObject *value = PyDict_GetItemWithError(mapping, key);
    value = Py_NewRef(value);
    Py_DECREF(value);
}
"""
    assert "NM004" not in codes(source)


def test_flags_realloc_that_overwrites_the_only_pointer() -> None:
    source = """
static int grow(char *buffer, size_t size) {
    buffer = PyMem_Realloc(buffer, size);
    return buffer != NULL;
}
"""
    assert "NM005" in codes(source)


def test_null_check_after_xdecref_is_not_a_use() -> None:
    source = """
static int call(PyObject *result) {
    Py_XDECREF(result);
    if (result == NULL) return -1;
    return 0;
}
"""
    assert "NM002" not in codes(source)


def test_assignment_starts_a_new_pointer_lifetime() -> None:
    source = """
static void safe(PyObject *value) {
    Py_DECREF(value);
    value = PyLong_FromLong(1);
    Py_DECREF(value);
}
"""
    assert "NM001" not in codes(source)
    assert "NM002" not in codes(source)


def test_assignment_replaces_a_borrowed_pointer_lifetime() -> None:
    source = """
static void safe(PyObject *mapping, PyObject *key) {
    PyObject *value = PyDict_GetItemWithError(mapping, key);
    value = PyLong_FromLong(1);
    Py_DECREF(value);
}
"""
    assert "NM004" not in codes(source)


def test_pointer_state_does_not_cross_function_or_label_boundaries() -> None:
    functions = """
static void first(PyObject *value) {
    Py_DECREF(value);
}
static void second(PyObject *value) {
    Py_DECREF(value);
}
"""
    label = """
static void cleanup(PyObject *value) {
    Py_DECREF(value);
cleanup_tail:
    Py_DECREF(value);
}
"""

    assert "NM001" not in codes(functions)
    assert "NM001" not in codes(label)


def test_pointer_state_expires_with_the_function_attribution_window() -> None:
    source = (
        "static int cleanup(PyObject *value) {\n"
        "    Py_DECREF(value);\n"
        + "    counter += 1;\n" * 400
        + "    return PyObject_IsTrue(value);\n"
        "}\n"
    )

    assert "NM002" not in codes(source)


def test_pointer_state_stops_at_each_control_flow_boundary() -> None:
    branch = """
static int cleanup(PyObject *value, int condition) {
    Py_DECREF(value);
    if (condition) return 0;
    return PyObject_IsTrue(value);
}
"""
    jump = """
static int cleanup(PyObject *value) {
    Py_DECREF(value);
    goto done;
    PyObject_IsTrue(value);
done:
    return 0;
}
"""
    block_end = """
static int cleanup(PyObject *value, int condition) {
    if (condition) {
        Py_DECREF(value);
    }
    return PyObject_IsTrue(value);
}
"""

    assert "NM002" not in codes(branch)
    assert "NM002" not in codes(jump)
    assert "NM002" not in codes(block_end)


def test_double_release_diagnostic_names_the_original_line() -> None:
    source = """static void cleanup(PyObject *value) {
    Py_DECREF(value);
    Py_DECREF(value);
}
"""
    finding = scan_text("example.c", source)[0]

    assert finding.message == "'value' released again after line 2"


def test_rule_can_be_waived_with_a_reason() -> None:
    source = """
static void cleanup(PyObject *value) {
    Py_DECREF(value);
    /* native-memory-lint: allow NM001 -- an independent owned reference remains */
    Py_DECREF(value);
}
"""
    assert "NM001" not in codes(source)
