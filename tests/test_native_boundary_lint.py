from __future__ import annotations

from wreath._devtools.native_boundary_lint import scan_text


def codes(source: str) -> list[str]:
    return [finding.code for finding in scan_text("example.c", source)]


def test_flags_a_recursive_boxed_opcode_interpreter() -> None:
    source = """
static PyObject *evaluate(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return evaluate(PyTuple_GET_ITEM(node, 1));
    }
}
"""
    assert codes(source) == ["NB001"]


def test_flags_execution_time_plan_reconstruction() -> None:
    source = """
static int row_plan_init(RowPlan *out, PyObject *plan) {
    out->cells = PyObject_GetAttrString(plan, "cells");
    return out->cells == NULL ? -1 : 0;
}

static PyObject *hydrate_records(PyObject *rows, PyObject *plan) {
    RowPlan native = {0};
    if (row_plan_init(&native, plan) < 0) return NULL;
    return hydrate_all(rows, &native);
}
"""
    assert codes(source) == ["NB002"]


def test_compile_time_plan_construction_is_not_a_finding() -> None:
    source = """
static PyObject *compile_validation_plan(PyObject *plan) {
    NativePlan native = {0};
    if (row_plan_init(&native, plan) < 0) return NULL;
    return plan_capsule(&native);
}
"""
    assert codes(source) == []


def test_tuple_opcode_decoder_used_only_by_a_compiler_is_not_a_finding() -> None:
    source = """
static int decode_tape(PyObject *tape, NativePlan *out) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(tape, 0));
    switch (opcode) {
    case 0: out->value = PyTuple_GET_ITEM(tape, 1); return 0;
    default: return decode_tape(PyTuple_GET_ITEM(tape, 1), out);
    }
}

static PyObject *compile_template(PyObject *tape) {
    NativePlan *plan = plan_new();
    if (decode_tape(tape, plan) < 0) return NULL;
    return plan_capsule(plan);
}
"""
    assert codes(source) == []


def test_tuple_opcode_decoder_shared_with_an_executor_remains_a_finding() -> None:
    source = """
static int decode_tape(PyObject *tape, NativePlan *out) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(tape, 0));
    switch (opcode) {
    case 0: out->value = PyTuple_GET_ITEM(tape, 1); return 0;
    default: return decode_tape(PyTuple_GET_ITEM(tape, 1), out);
    }
}

static PyObject *compile_template(PyObject *tape) {
    NativePlan *plan = plan_new();
    if (decode_tape(tape, plan) < 0) return NULL;
    return plan_capsule(plan);
}

static PyObject *render_template(PyObject *tape) {
    NativePlan plan = {0};
    if (decode_tape(tape, &plan) < 0) return NULL;
    return execute_plan(&plan);
}
"""
    assert codes(source) == ["NB001"]


def test_public_result_materialization_is_a_legitimate_boundary() -> None:
    source = """
static PyObject *materialize_rows(NativeRows *rows) {
    PyObject *result = PyList_New(rows->count);
    for (Py_ssize_t index = 0; index < rows->count; index++) {
        PyObject *item = PyTuple_New(2);
        PyTuple_SET_ITEM(item, 0, PyLong_FromLong(rows->items[index].id));
        PyTuple_SET_ITEM(item, 1, PyUnicode_FromString(rows->items[index].name));
        PyList_SET_ITEM(result, index, item);
    }
    return result;
}
"""
    assert codes(source) == []


def test_python_callback_dispatch_is_a_legitimate_seam() -> None:
    source = """
static PyObject *dispatch_callbacks(PyObject *callbacks, PyObject *value) {
    PyObject *result = PyList_New(0);
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(callbacks); index++) {
        PyObject *item = PyObject_CallOneArg(PyTuple_GET_ITEM(callbacks, index), value);
        if (item == NULL || PyList_Append(result, item) < 0) return NULL;
    }
    return result;
}
"""
    assert codes(source) == []


def test_graphql_ast_construction_at_the_parse_boundary_is_not_a_finding() -> None:
    source = """
static PyObject *graphql_parse(PyObject *source) {
    PyObject *fields = PyList_New(0);
    for (Py_ssize_t index = 0; index < token_count; index++) {
        PyObject *field = PyObject_CallOneArg(Field, tokens[index]);
        if (field == NULL || PyList_Append(fields, field) < 0) return NULL;
    }
    return PyObject_CallOneArg(Document, fields);
}
"""
    assert codes(source) == []


def test_plan_reconstruction_rule_can_be_waived_with_a_reason() -> None:
    source = """
static PyObject *execute_bounded_plan(PyObject *plan) {
    NativePlan native = {0};
    /* native-boundary-lint: allow NB002 -- one bounded compatibility operation */
    if (row_plan_init(&native, plan) < 0) return NULL;
    return execute(&native);
}
"""
    assert codes(source) == []
