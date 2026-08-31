from __future__ import annotations

import pytest

from wreath._structured_fields import (
    Date,
    DisplayString,
    Item,
    Token,
    parse_boolean_item,
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
    assert serialize_dictionary(
        {
            "public": Item(True),
            "max-age": Item(60),
            "label": Item("edge"),
            "mode": Item(Token("fast")),
        }
    ) == b'public, max-age=60, label="edge", mode=fast'


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
