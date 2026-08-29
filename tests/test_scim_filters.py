from __future__ import annotations

import pytest

from wreath._scim.filters import (
    MAX_DEPTH,
    MAX_LENGTH,
    Compare,
    FilterError,
    Logical,
    ValuePath,
    matches,
    parse,
    values_at,
)

USER = {
    "id": "7",
    "userName": "Alice@Example.com",
    "active": True,
    "emails": [{"value": "alice@example.com", "primary": True, "type": "work"}],
    "groups": [{"value": "admin", "display": "admin"}],
    "meta": {"resourceType": "User", "lastModified": "2026-01-02T03:04:05Z"},
}

ATTRIBUTES = frozenset({"id", "username", "active", "emails", "groups", "meta"})


def ok(source: str) -> bool:
    return matches(parse(source, attributes=ATTRIBUTES), USER)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('userName eq "alice@example.com"', True),
        ('userName eq "ALICE@EXAMPLE.COM"', True),
        ('userName ne "bob@example.com"', True),
        ('userName ne "alice@example.com"', False),
        ('userName co "example"', True),
        ('userName co "nobody"', False),
        ('userName sw "alice"', True),
        ('userName sw "bob"', False),
        ('userName ew ".com"', True),
        ('userName ew ".org"', False),
        ("userName pr", True),
        ("displayName pr", False),
        ("active eq true", True),
        ("active eq false", False),
        # The four orderings are asserted from *both* sides. `ge` against an
        # equal value and `le` against an equal value pass under `gt`/`lt` too,
        # so a strict/non-strict confusion needs the unequal case to show.
        ('id gt "5"', True),
        ('id gt "7"', False),
        ('id lt "9"', True),
        ('id lt "7"', False),
        ('id lt "5"', False),
        ('id ge "7"', True),
        ('id ge "8"', False),
        ('id le "7"', True),
        ('id le "6"', False),
        # `co` is true here, so a `sw`/`ew` that collapsed into it would pass.
        ('userName sw "example"', False),
        ('userName ew "alice"', False),
        # A substring operator against a non-string is false, never an error.
        ("userName co 3", False),
        ('active sw "t"', False),
    ],
)
def test_each_operator_answers_the_representation(source: str, expected: bool) -> None:
    # `displayName` is not in ATTRIBUTES for the endpoint, but the evaluator
    # itself must answer `pr` for an absent attribute rather than raise.
    node = parse(source, attributes=None)
    assert matches(node, USER) is expected


def test_a_string_comparison_ignores_case_in_both_directions() -> None:
    assert ok('userName eq "alice@EXAMPLE.com"')
    assert ok('userName sw "ALICE"')


def test_a_boolean_never_equals_a_number() -> None:
    assert matches(parse("active eq true", attributes=None), USER)
    assert not matches(parse("active eq 1", attributes=None), USER)


def test_a_comparison_between_incomparable_types_is_false_not_an_error() -> None:
    assert not matches(parse('active gt "x"', attributes=None), USER)
    assert not matches(parse("userName gt 3", attributes=None), USER)


def test_pr_is_false_for_an_empty_string_and_an_empty_list() -> None:
    empty = {"userName": "", "groups": []}
    assert not matches(parse("userName pr", attributes=None), empty)
    assert not matches(parse("groups pr", attributes=None), empty)


def test_and_binds_tighter_than_or() -> None:
    node = parse(
        'userName eq "nobody" or userName sw "alice" and active eq true',
        attributes=ATTRIBUTES,
    )
    assert isinstance(node, Logical)
    assert node.op == "or"
    assert isinstance(node.right, Logical)
    assert node.right.op == "and"
    assert matches(node, USER)


def test_parentheses_override_precedence() -> None:
    assert not matches(
        parse(
            '(userName eq "nobody" or userName sw "alice") and active eq false',
            attributes=ATTRIBUTES,
        ),
        USER,
    )


def test_not_negates_a_group() -> None:
    assert ok('not (userName eq "bob@example.com")')
    assert not ok('not (userName sw "alice")')


def test_a_value_path_asks_whether_any_element_matches() -> None:
    node = parse('emails[type eq "work"]', attributes=ATTRIBUTES)
    assert isinstance(node, ValuePath)
    assert matches(node, USER)
    assert not matches(parse('emails[type eq "home"]', attributes=None), USER)


def test_a_sub_attribute_path_distributes_over_a_multi_valued_attribute() -> None:
    assert ok('emails.value ew "example.com"')
    assert ok('groups.value eq "admin"')


def test_a_schema_urn_prefix_names_the_same_attribute() -> None:
    node = parse(
        'urn:ietf:params:scim:schemas:core:2.0:User:userName eq "alice@example.com"',
        attributes=ATTRIBUTES,
    )
    assert isinstance(node, Compare)
    assert node.path == "username"
    assert matches(node, USER)


def test_values_at_walks_case_insensitively() -> None:
    assert values_at(USER, "USERNAME") == ["Alice@Example.com"]
    assert values_at(USER, "meta.lastmodified") == ["2026-01-02T03:04:05Z"]
    assert values_at(USER, "nothing.here") == []


def test_an_attribute_this_provider_does_not_hold_is_refused_not_answered() -> None:
    with pytest.raises(FilterError) as caught:
        parse('externalId eq "abc"', attributes=ATTRIBUTES)
    assert "does not hold an attribute named 'externalid'" in caught.value.detail


def test_an_unheld_attribute_inside_a_value_path_is_refused_too() -> None:
    with pytest.raises(FilterError) as caught:
        parse('phoneNumbers[type eq "work"]', attributes=ATTRIBUTES)
    assert "does not hold an attribute named 'phonenumbers'" in caught.value.detail


def test_nesting_deeper_than_the_budget_is_refused() -> None:
    source = "(" * (MAX_DEPTH + 1) + "userName pr" + ")" * (MAX_DEPTH + 1)
    with pytest.raises(FilterError) as caught:
        parse(source, attributes=ATTRIBUTES)
    assert f"deeper than {MAX_DEPTH} levels" in caught.value.detail


def test_nesting_at_the_budget_is_accepted() -> None:
    source = "(" * MAX_DEPTH + "userName pr" + ")" * MAX_DEPTH
    assert matches(parse(source, attributes=ATTRIBUTES), USER)


def test_a_filter_longer_than_the_budget_is_refused() -> None:
    source = 'userName eq "' + "a" * MAX_LENGTH + '"'
    with pytest.raises(FilterError) as caught:
        parse(source, attributes=ATTRIBUTES)
    assert f"longer than {MAX_LENGTH} characters" in caught.value.detail


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("", "filter is empty"),
        ("userName", "ends where more was expected"),
        ('userName xx "a"', "unknown operator"),
        ("userName eq alice", "unquoted value"),
        ('userName eq "alice', "unterminated string"),
        ('userName eq "a\\q"', "unknown string escape"),
        ('userName eq "\\u00"', "truncated \\u escape"),
        ('userName eq "a" bogus', "trailing input"),
        ("(userName pr]", "expected ')'"),
        ("(userName pr", "ends where more was expected"),
        ('userName eq "a" and', "ends where more was expected"),
        ("userName eq !", "unexpected character"),
        ('"quoted" eq "a"', "expected an attribute"),
        ('urn: eq "a"', "empty attribute name"),
        ('userName ("a")', "expected an operator"),
        ("userName eq [", "expected a value"),
        ('userName eq "a\\', "ends inside a string escape"),
        ('userName eq "\\uZZZZ"', "invalid \\u escape"),
    ],
)
def test_each_malformed_filter_has_its_own_refusal(source: str, fragment: str) -> None:
    with pytest.raises(FilterError) as caught:
        parse(source, attributes=None)
    assert fragment in caught.value.detail


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('userName eq "a\\u0062c"', "abc"),
        ('userName eq "a\\"b"', 'a"b'),
        ('userName eq "a\\\\b"', "a\\b"),
        ('userName eq "a\\tb"', "a\tb"),
        ('userName eq "a\\/b"', "a/b"),
    ],
)
def test_a_json_escape_survives_into_the_comparison(source: str, expected: str) -> None:
    node = parse(source, attributes=None)
    assert isinstance(node, Compare)
    assert node.value == expected


def test_a_non_string_key_in_a_resource_is_skipped_rather_than_lowercased() -> None:
    assert values_at({1: "surprise", "userName": "alice"}, "userName") == ["alice"]


def test_a_quoted_keyword_is_a_value_and_not_an_operator() -> None:
    node = parse('userName eq "and"', attributes=None)
    assert isinstance(node, Compare)
    assert node.value == "and"
    assert node.op == "eq"


def test_a_quoted_keyword_cannot_join_two_expressions() -> None:
    with pytest.raises(FilterError) as caught:
        parse('userName pr "and" userName pr', attributes=None)
    assert "trailing input" in caught.value.detail


def test_null_is_a_value_rather_than_an_unquoted_word() -> None:
    node = parse("userName eq null", attributes=None)
    assert isinstance(node, Compare)
    assert node.value is None


def test_walking_into_a_string_yields_nothing_rather_than_raising() -> None:
    assert values_at(USER, "userName.nonsense") == []


def test_a_number_value_parses_as_a_number() -> None:
    node = parse("id eq 7", attributes=None)
    assert isinstance(node, Compare)
    assert node.value == 7
    node = parse("id eq 7.5", attributes=None)
    assert isinstance(node, Compare)
    assert node.value == 7.5
