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


def test_compile_only_decoder_ignores_functions_that_do_not_call_it() -> None:
    source = """
static int walk_tape(PyObject *tape, NativePlan *out) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(tape, 0));
    switch (opcode) {
    case 0: out->value = PyTuple_GET_ITEM(tape, 1); return 0;
    default: return walk_tape(PyTuple_GET_ITEM(tape, 1), out);
    }
}

static int inspect_value(PyObject *value) {
    return PyObject_IsTrue(value);
}

static PyObject *compile_template(PyObject *tape) {
    NativePlan plan = {0};
    if (walk_tape(tape, &plan) < 0) return NULL;
    return plan_capsule(&plan);
}
"""
    assert codes(source) == []


def test_global_text_is_not_treated_as_a_function_call_site() -> None:
    source = """
#define WALK_ONCE(tape, out) walk_tape(tape, out)
static int walk_tape(PyObject *tape, NativePlan *out) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(tape, 0));
    switch (opcode) {
    case 0: out->value = PyTuple_GET_ITEM(tape, 1); return 0;
    default: return walk_tape(PyTuple_GET_ITEM(tape, 1), out);
    }
}

static PyObject *compile_template(PyObject *tape) {
    NativePlan plan = {0};
    if (walk_tape(tape, &plan) < 0) return NULL;
    return plan_capsule(&plan);
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


def test_boxed_opcode_rule_requires_every_structural_signal() -> None:
    builder = """
static PyObject *compile_evaluate(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return NULL;
    }
}
"""
    no_switch = """
static PyObject *evaluate(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    return Py_NewRef(PyTuple_GET_ITEM(node, opcode));
}
"""
    no_boxed_opcode = """
static PyObject *evaluate(PyObject *node) {
    long opcode = 0;
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 0));
    default: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    }
}
"""
    one_tuple_read = """
static PyObject *evaluate(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(node);
    default: return NULL;
    }
}
"""

    for source in (builder, no_switch, no_boxed_opcode, one_tuple_read):
        assert codes(source) == []


def test_boxed_opcode_rule_accepts_executor_name_or_recursion() -> None:
    named_executor = """
static PyObject *evaluate_once(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return NULL;
    }
}
"""
    recursive_helper = """
static PyObject *walk_tape(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return walk_tape(PyTuple_GET_ITEM(node, 1));
    }
}
"""
    neither = """
static PyObject *walk_once(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return NULL;
    }
}
"""

    assert codes(named_executor) == ["NB001"]
    assert codes(recursive_helper) == ["NB001"]
    assert codes(neither) == []


def test_boxed_opcode_finding_is_anchored_on_the_opcode_read() -> None:
    source = """static PyObject *evaluate(PyObject *node) {
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return NULL;
    }
}
"""
    findings = scan_text("example.c", source)

    assert [(finding.code, finding.line) for finding in findings] == [("NB001", 2)]


def test_boxed_opcode_rule_can_be_waived_with_a_reason() -> None:
    source = """
static PyObject *evaluate(PyObject *node) {
    /* native-boundary-lint: allow NB001 -- bounded compatibility expression */
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(node, 0));
    switch (opcode) {
    case 0: return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    default: return NULL;
    }
}
"""
    assert codes(source) == []


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


def test_plan_build_call_requires_an_execution_function_outside_a_builder() -> None:
    ordinary_helper = """
static int row_plan_init(RowPlan *out, PyObject *plan) {
    return 0;
}
static PyObject *prepare_rows(PyObject *plan) {
    RowPlan native = {0};
    if (row_plan_init(&native, plan) < 0) return NULL;
    return Py_NewRef(plan);
}
"""
    execution_builder = """
static int row_plan_init(RowPlan *out, PyObject *plan) {
    return 0;
}
static PyObject *compile_and_execute(PyObject *plan) {
    RowPlan native = {0};
    if (row_plan_init(&native, plan) < 0) return NULL;
    return Py_NewRef(plan);
}
"""

    assert codes(ordinary_helper) == []
    assert codes(execution_builder) == []


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
