from __future__ import annotations

from wreath._devtools.native_boundary_lint import scan_text


def codes(source: str) -> list[str]:
    return [finding.code for finding in scan_text("example.c", source)]


def test_flags_python_object_work_inside_a_native_loop() -> None:
    source = """
static PyObject *decode_many(PyObject *items) {
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *value = PyDict_GetItemWithError(items, keys[i]);
        PyObject *number = PyLong_FromLong(i);
    }
}
"""
    assert "NB001" in codes(source)


def test_flags_container_building_for_a_generic_call() -> None:
    source = """
static PyObject *make_uuid(PyObject *type, PyObject *value) {
    PyObject *args = PyTuple_Pack(1, value);
    PyObject *kwargs = Py_BuildValue("{s:O}", "value", value);
    return PyObject_Call(type, args, kwargs);
}
"""
    assert "NB002" in codes(source)


def test_flags_repeated_dynamic_lookups_in_one_function() -> None:
    source = """
static PyObject *extract(PyObject *value) {
    PyObject *a = PyObject_GetAttrString(value, "a");
    PyObject *b = PyObject_GetAttrString(value, "b");
    PyObject *c = PyDict_GetItemString(a, "c");
    return PyObject_CallMethodNoArgs(b, name);
}
"""
    assert "NB003" in codes(source)


def test_flags_high_aggregate_boundary_pressure() -> None:
    source = """
static PyObject *convert(PyObject *value) {
    PyObject *a = PyLong_FromLong(1);
    PyObject *b = PyLong_FromLong(2);
    PyObject *c = PyUnicode_FromString("c");
    PyObject *d = PyTuple_Pack(3, a, b, c);
    PyObject *e = PyObject_GetAttrString(value, "factory");
    PyObject *f = PyLong_FromLong(3);
    PyObject *g = PyUnicode_FromString("g");
    PyObject *h = PyObject_CallOneArg(e, f);
    return PyObject_CallOneArg(e, d);
}
"""
    assert "NB004" in codes(source)


def test_does_not_flag_a_single_boundary_conversion() -> None:
    source = """
static PyObject *size(PyObject *self, PyObject *value) {
    long size = PyLong_AsLong(value);
    return PyLong_FromLong(size + 1);
}
"""
    assert codes(source) == []


def test_rule_can_be_waived_with_a_reason() -> None:
    source = """
static PyObject *decode_many(PyObject *items) {
    for (Py_ssize_t i = 0; i < count; i++) {
        /* native-boundary-lint: allow NB001 -- values must become owned Python objects */
        PyObject *number = PyLong_FromLong(i);
        PyObject *text = PyUnicode_FromFormat("%zd", i);
    }
}
"""
    assert "NB001" not in codes(source)


def test_object_work_inside_a_loop_still_counts_when_it_is_not_an_error_path() -> None:
    source = """
static PyObject *drive(PyObject *items) {
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *name = PyUnicode_FromString("row");
        PyObject *number = PyLong_FromLong(i);
    }
}
"""
    assert "NB001" in codes(source)


def test_objects_built_as_arguments_to_a_raiser_are_not_loop_traffic() -> None:
    source = """
static PyObject *drive(PyObject *items) {
    for (Py_ssize_t i = 0; i < count; i++) {
        if (bad) {
            raise_render(0, PyUnicode_FromString("template output too large"));
            raise_render(0, PyUnicode_FromFormat("invalid opcode %ld", op));
        }
    }
}
"""
    assert "NB001" not in codes(source)


def test_a_raiser_wrapped_across_lines_is_covered_in_full() -> None:
    source = """
static PyObject *drive(PyObject *items) {
    for (Py_ssize_t i = 0; i < count; i++) {
        raise_render(line, joined ? PyUnicode_FromFormat(
                                        "%R is not iterable", joined)
                                  : NULL);
        raise_render(line, other ? PyUnicode_FromFormat(
                                        "%R is not callable", other)
                                 : NULL);
    }
}
"""
    assert "NB001" not in codes(source)


def test_getattr_string_is_still_a_dynamic_lookup() -> None:
    source = """
static PyObject *read_three(PyObject *op) {
    PyObject *a = PyObject_GetAttrString(op, "field_tape");
    PyObject *b = PyObject_GetAttrString(op, "decoder_plan");
    PyObject *c = PyObject_GetAttrString(op, "rows");
}
"""
    assert "NB003" in codes(source)


def test_getattr_with_an_interned_name_is_not_a_dynamic_lookup() -> None:
    source = """
static PyObject *read_three(PyObject *op) {
    PyObject *a = PyObject_GetAttr(op, str_field_tape);
    PyObject *b = PyObject_GetAttr(op, str_decoder_plan);
    PyObject *c = PyObject_GetAttr(op, str_rows);
}
"""
    assert "NB003" not in codes(source)


def test_a_dynamic_lookup_beside_a_cached_one_still_counts() -> None:
    source = """
static PyObject *read_three(PyObject *op) {
    PyObject *a = PyObject_GetAttr(op, str_tape); PyObject *d = PyObject_GetAttrString(op, "x");
    PyObject *b = PyObject_GetAttrString(op, "decoder_plan");
    PyObject *c = PyObject_GetAttrString(op, "rows");
}
"""
    assert "NB003" in codes(source)


def test_nb001_excuses_a_one_time_static_table_build() -> None:
    source = """
static int build_static_table(void) {
    for (size_t i = 0; i < 61; i++) {
        table[i].name = PyBytes_FromString(STATIC_NAMES[i]);
        table[i].value = PyBytes_FromString(STATIC_VALUES[i]);
    }
}
"""
    assert "NB001" not in codes(source)


def test_nb001_still_fires_on_a_similarly_named_hot_function() -> None:
    source = """
static int wreath_build_header_map(PyObject *headers) {
    for (size_t i = 0; i < count; i++) {
        PyObject *name = PyBytes_FromString(raw[i].name);
        PyObject *value = PyBytes_FromString(raw[i].value);
    }
}
"""
    assert "NB001" in codes(source)
