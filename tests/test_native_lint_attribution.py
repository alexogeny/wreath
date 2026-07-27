"""`_enclosing_function` must not read a wrapped call statement as a definition.

Every native lint attributes its findings to "the enclosing function", and
`native_boundary_lint`'s NB003/NB004 also *measure* that function's extent from
the line the name was found on. So a misattribution is not cosmetic: it moves
the window the aggregate score is computed over, and the score then spans
unrelated functions.

The failing shape is a call whose arguments wrap to the next line:

    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.poll_stale);

`FUNC_ONE_LINE` matched it exactly -- `return` was absorbed by the return-type
pattern, the callee was captured as the name, and the line carries no semicolon
because the statement is not finished. `wreath-native-boundary-lint` reported
"score 32 in PyLong_FromUnsignedLongLong", naming a CPython API function that
this repository does not define.
"""

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
    """The narrowing must not cost recognition of the definitions themselves --
    otherwise findings would silently lose their attribution instead of gaining
    a wrong one."""
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
