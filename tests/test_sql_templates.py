"""`wreath.sql` turns a t-string into text plus bind parameters.

The property under test is the one the whole module exists for: **a value that
was interpolated never reaches the SQL text.** Every case below feeds a hostile
string through an interpolation and asserts it came out as a parameter, not as
syntax.
"""
from __future__ import annotations

import pytest

from wreath.sql import Fragment, Identifier, Statement

# The a01 payload from the range: it closes the ILIKE literal, closes the
# parenthesis the WHERE clause opened, and unions a table it was never meant to
# read. Against a t-string it is just a string.
INJECTION = "zz%') UNION SELECT id, label, secret, 'x', 'y' FROM vault --"


def test_interpolated_value_becomes_a_parameter() -> None:
    needle = "acme"
    statement = Statement(t"SELECT id FROM shipments WHERE reference ILIKE {needle}")
    assert statement.text == "SELECT id FROM shipments WHERE reference ILIKE $1"
    assert statement.args == ("acme",)


def test_injection_payload_never_reaches_the_text() -> None:
    pattern = f"%{INJECTION}%"
    statement = Statement(t"SELECT id FROM shipments WHERE reference ILIKE {pattern}")
    assert statement.text == "SELECT id FROM shipments WHERE reference ILIKE $1"
    assert "UNION" not in statement.text
    assert statement.args == (f"%{INJECTION}%",)


def test_parameters_are_numbered_in_order() -> None:
    org, status, limit = 7, "booked", 25
    statement = Statement(
        t"SELECT id FROM shipments WHERE org_id = {org} AND status = {status} LIMIT {limit}"
    )
    assert statement.text == (
        "SELECT id FROM shipments WHERE org_id = $1 AND status = $2 LIMIT $3"
    )
    assert statement.args == (7, "booked", 25)


def test_adjacent_interpolations_stay_separate_parameters() -> None:
    a, b = "one", "two"
    statement = Statement(t"SELECT {a}{b}")
    assert statement.text == "SELECT $1$2"
    assert statement.args == ("one", "two")


def test_implicit_concatenation_spans_one_statement() -> None:
    org = 3
    statement = Statement(
        t"SELECT id, reference FROM shipments "
        t"WHERE org_id = {org} "
        t"ORDER BY id"
    )
    assert statement.text == (
        "SELECT id, reference FROM shipments WHERE org_id = $1 ORDER BY id"
    )
    assert statement.args == (3,)


def test_a_template_with_no_interpolation_is_its_own_text() -> None:
    statement = Statement(t"SELECT 1")
    assert statement.text == "SELECT 1"
    assert statement.args == ()


def test_a_plain_string_is_refused() -> None:
    with pytest.raises(TypeError, match="t-string"):
        Statement("SELECT 1")  # type: ignore[arg-type]


def test_an_fstring_is_refused_because_it_is_a_plain_string() -> None:
    needle = INJECTION
    with pytest.raises(TypeError, match="t-string"):
        Statement(f"SELECT id FROM shipments WHERE reference ILIKE '%{needle}%'")  # type: ignore[arg-type]


# -- identifiers ------------------------------------------------------------
#
# A schema or table name cannot be a bind parameter -- PostgreSQL resolves those
# at parse time -- so the module needs one other way to interpolate, and it
# quotes rather than trusting.


def test_identifier_is_quoted_into_the_text() -> None:
    statement = Statement(t"SELECT id FROM {Identifier('northwind', 'shipments')}")
    assert statement.text == 'SELECT id FROM "northwind"."shipments"'
    assert statement.args == ()


def test_identifier_escapes_an_embedded_quote() -> None:
    assert Identifier('we"ird').text == '"we""ird"'


def test_identifier_refuses_a_null_byte() -> None:
    with pytest.raises(ValueError, match="NUL"):
        Identifier("bad\x00name")


def test_identifier_refuses_an_empty_part() -> None:
    with pytest.raises(ValueError, match="empty"):
        Identifier("")


def test_identifier_refuses_no_parts() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Identifier()


def test_identifier_refuses_a_part_that_is_not_a_string() -> None:
    # The quoting is `str.replace`, so a non-string would raise `AttributeError`
    # from inside `text` -- at render time, naming the wrong thing, in a
    # traceback that points at this module rather than at the caller.
    with pytest.raises(TypeError, match="must be str, got int"):
        Identifier(7)  # type: ignore[arg-type]


def test_identifiers_with_the_same_parts_are_equal() -> None:
    assert Identifier("a", "b") == Identifier("a", "b")
    assert Identifier("a", "b") != Identifier("a", "c")
    assert Identifier("a") != "a"
    assert len({Identifier("a"), Identifier("a")}) == 1


def test_an_injection_through_an_identifier_stays_inside_the_quotes() -> None:
    statement = Statement(t"SELECT * FROM {Identifier(INJECTION)}")
    # Quoted, so it names a (nonexistent) table rather than closing one string
    # and opening a second statement.
    assert statement.text.startswith('SELECT * FROM "zz%\') UNION')
    assert statement.text.endswith('--"')
    assert statement.args == ()


# -- composition ------------------------------------------------------------


def test_a_nested_template_splices_and_renumbers() -> None:
    status = "booked"
    org = 4
    clause = t"AND status = {status}"
    statement = Statement(t"SELECT id FROM shipments WHERE org_id = {org} {clause}")
    assert statement.text == (
        "SELECT id FROM shipments WHERE org_id = $1 AND status = $2"
    )
    assert statement.args == (4, "booked")


def test_nesting_goes_more_than_one_level_deep() -> None:
    value = "x"
    inner = t"c = {value}"
    middle = t"b AND {inner}"
    statement = Statement(t"a AND {middle}")
    assert statement.text == "a AND b AND c = $1"
    assert statement.args == ("x",)


def test_a_nested_statement_splices_and_renumbers() -> None:
    org = 4
    status = "booked"
    clause = Statement(t"status = {status}")
    statement = Statement(t"SELECT id FROM s WHERE org_id = {org} AND {clause}")
    assert statement.text == "SELECT id FROM s WHERE org_id = $1 AND status = $2"
    assert statement.args == (4, "booked")


# -- the escape hatch -------------------------------------------------------


def test_fragment_splices_verbatim() -> None:
    statement = Statement(t"SELECT id FROM s ORDER BY id {Fragment('DESC')}")
    assert statement.text == "SELECT id FROM s ORDER BY id DESC"
    assert statement.args == ()


def test_fragment_refuses_a_non_string() -> None:
    with pytest.raises(TypeError, match="str"):
        Fragment(7)  # type: ignore[arg-type]


# -- refusals that keep the mapping honest ----------------------------------


def test_a_conversion_on_a_bound_value_is_refused() -> None:
    # `!r` asks for text formatting that a bind parameter never receives, so
    # honouring the syntax would be a lie and ignoring it would be worse.
    value = "x"
    with pytest.raises(ValueError, match="conversion"):
        Statement(t"SELECT {value!r}")


def test_a_format_spec_on_a_bound_value_is_refused() -> None:
    value = 3
    with pytest.raises(ValueError, match="format specification"):
        Statement(t"SELECT {value:>5}")


def test_a_conversion_on_an_identifier_is_refused() -> None:
    # A spliced value is already SQL, so `!r` has nowhere to happen. Applying it
    # would quote the *quoted* name into the statement and honouring it silently
    # would render `Identifier(...)`'s repr as a table name.
    name = Identifier("shipments")
    with pytest.raises(ValueError, match="conversion on a Identifier"):
        Statement(t"SELECT * FROM {name!r}")


def test_a_format_spec_on_a_fragment_is_refused() -> None:
    order = Fragment("DESC")
    with pytest.raises(ValueError, match="format specification on a Fragment"):
        Statement(t"SELECT 1 ORDER BY id {order:>10}")


def test_a_conversion_on_a_nested_template_is_refused() -> None:
    value = 1
    clause = t"x = {value}"
    with pytest.raises(ValueError, match="conversion on a nested template"):
        Statement(t"SELECT 1 WHERE {clause!s}")


def test_a_format_spec_on_a_nested_statement_is_refused() -> None:
    value = 1
    clause = Statement(t"x = {value}")
    with pytest.raises(ValueError, match="format specification on a nested statement"):
        Statement(t"SELECT 1 WHERE {clause:>3}")


def test_repr_shows_the_text_and_the_parameter_count() -> None:
    value = 1
    assert repr(Statement(t"SELECT {value}")) == "Statement('SELECT $1', 1 parameter)"


def test_statements_with_the_same_text_and_args_are_equal() -> None:
    a, b = 1, 1
    assert Statement(t"SELECT {a}") == Statement(t"SELECT {b}")
