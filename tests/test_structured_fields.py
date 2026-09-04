from __future__ import annotations

from typing import Any, cast

import pytest

from wreath._structured_fields import (
    Date,
    DisplayString,
    Item,
    StructuredFieldError,
    Token,
    parse_boolean_item,
    parse_dictionary,
    serialize_dictionary,
    serialize_item,
    serialize_list,
)


def test_a_string_list_uses_the_rfc_9651_wire_form() -> None:
    assert serialize_list([Item("one"), Item('two "quoted"')]) == (b'"one", "two \\"quoted\\""')


def test_tokens_and_parameters_keep_their_structured_types() -> None:
    assert (
        serialize_list([Item(Token("Wreath"), {"fwd": Token("uri-miss"), "stored": True})])
        == b"Wreath;fwd=uri-miss;stored"
    )


def test_a_dictionary_uses_structured_member_keys_and_values() -> None:
    assert (
        serialize_dictionary(
            {
                "public": Item(True),
                "max-age": Item(60),
                "label": Item("edge"),
                "mode": Item(Token("fast")),
            }
        )
        == b'public, max-age=60, label="edge", mode=fast'
    )


def test_an_empty_structured_dictionary_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        serialize_dictionary({})


def test_a_date_item_uses_the_rfc_9651_wire_form() -> None:
    assert serialize_item(Item(Date(1_688_169_599))) == b"@1688169599"


def test_a_display_string_uses_utf8_percent_encoding() -> None:
    assert serialize_item(Item(DisplayString('caf\N{LATIN SMALL LETTER E WITH ACUTE} "menu"'))) == (
        b'%"caf%c3%a9 %22menu%22"'
    )


def test_a_display_string_refuses_an_unpaired_surrogate() -> None:
    with pytest.raises(ValueError, match="valid Unicode"):
        DisplayString(chr(0xD800))


@pytest.mark.parametrize(
    "value",
    [
        b"?1",
        b" ?1;wait;level=2 ",
        "?1;why=stream",
        b'?1; shown=%"caf%c3%a9"',
        b"?1;binary=:aGk:",
    ],
)
def test_boolean_items_parse_with_ignored_parameters(value: bytes | str) -> None:
    assert parse_boolean_item(value) is True


@pytest.mark.parametrize("value", [b"?0", b"?0;buffered", b"?0;reason=policy"])
def test_false_boolean_items_parse_with_ignored_parameters(value: bytes) -> None:
    assert parse_boolean_item(value) is False


@pytest.mark.parametrize(
    "value",
    [
        b"1",
        b"?2",
        b"?1;",
        b"?1;Bad",
        b'?1;x="',
        b"?1;x=:a=:",
        b'?1;x=%"%C3%A9"',
        b'?1;x=%"%ff"',
    ],
)
def test_non_boolean_or_invalid_items_are_not_accepted(value: bytes) -> None:
    assert parse_boolean_item(value) is None


@pytest.mark.parametrize("value", ["caf\N{LATIN SMALL LETTER E WITH ACUTE}", "line\nfeed"])
def test_invalid_structured_strings_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="structured string"):
        serialize_list([Item(value)])


def test_invalid_parameter_names_are_refused() -> None:
    with pytest.raises(ValueError, match="parameter name"):
        serialize_list([Item(Token("Wreath"), {"Not-Lower": True})])


def test_dictionary_member_limit_is_enforced_before_returning_members() -> None:
    with pytest.raises(StructuredFieldError, match="too many members"):
        parse_dictionary("a=:YQ==:, b=:Yg==:", max_members=1)


def test_an_empty_byte_sequence_round_trips_through_a_dictionary() -> None:
    assert parse_dictionary("a=::") == {"a": Item(b"")}


@pytest.mark.parametrize("value", ["A=:YQ==:", "1a=:YQ==:", "a:b=:YQ==:"])
def test_dictionary_member_names_follow_the_rfc_key_grammar(value: str) -> None:
    with pytest.raises(StructuredFieldError, match="dictionary key"):
        parse_dictionary(value)


@pytest.mark.parametrize(
    "value",
    [
        "a=1000000000000000",
        "a=-1000000000000000",
        "a=1000000000000.0",
        "a=-1000000000000.0",
        "a=1.0000",
    ],
)
def test_dictionary_numbers_follow_the_rfc_numeric_limits(value: str) -> None:
    with pytest.raises(StructuredFieldError, match="number"):
        parse_dictionary(value)


@pytest.mark.parametrize("value", ["a;Bad", "a;1bad", "a;bad:name"])
def test_dictionary_parameter_names_follow_the_rfc_key_grammar(value: str) -> None:
    with pytest.raises(StructuredFieldError, match="parameter key"):
        parse_dictionary(value)


@pytest.mark.parametrize("value", [".dot", "_private", "/path"])
def test_dictionary_tokens_require_an_rfc_token_initial(value: str) -> None:
    with pytest.raises(StructuredFieldError, match="token"):
        parse_dictionary(f"a={value}")


def test_dictionary_tokens_accept_the_complete_rfc_token_alphabet() -> None:
    value = "A!#$%&'*+-.^_`|~:/z09"
    assert parse_dictionary(f"a={value}") == {"a": Item(value)}


@pytest.mark.parametrize(
    ("argument", "value", "error_type"),
    [
        ("max_bytes", True, TypeError),
        ("max_bytes", 0, ValueError),
        ("max_members", True, TypeError),
        ("max_members", 0, ValueError),
    ],
)
def test_dictionary_parser_limits_are_exact_positive_integers(
    argument: str, value: object, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type, match=argument.replace("_", " ")):
        parse_dictionary("a=:YQ==:", **cast(Any, {argument: value}))
