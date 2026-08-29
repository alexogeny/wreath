from __future__ import annotations

import pytest

from wreath._devtools.native_lint import _enclosing_function

GETTER = """static PyObject *
rp_get_stale_events(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.poll_stale);
}
""".splitlines()


def test_a_wrapped_return_call_is_not_a_definition() -> None:
    # Index 5 is the wrapped argument line, inside the getter.
    assert _enclosing_function(GETTER, 5) == "rp_get_stale_events"


def test_the_wrapped_call_line_itself_is_attributed_to_the_getter() -> None:
    # Index 4 is `return PyLong_FromUnsignedLongLong(` -- the line that used to
    # be read as the start of a new function.
    assert _enclosing_function(GETTER, 4) == "rp_get_stale_events"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("static int build_static_table(void)", "build_static_table"),
        ("wreath_hpack_decode(PyObject *self, PyObject *args)", "wreath_hpack_decode"),
        ("static PyObject *encode_numeric(PyObject *value)", "encode_numeric"),
    ],
)
def test_real_definitions_are_still_recognised(line: str, expected: str) -> None:
    assert _enclosing_function([line], 0) == expected


@pytest.mark.parametrize(
    "statement",
    [
        "    return PyLong_FromUnsignedLongLong(",
        "    return PyTuple_Pack(2,",
        "    goto cleanup_with(",
        "    do work_item(",
    ],
)
def test_statement_keywords_never_introduce_a_function(statement: str) -> None:
    assert _enclosing_function([statement], 0) == ""
