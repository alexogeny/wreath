"""Edge cases the exploit corpus does not reach.

`test_xml_refusals.py` is organised by attack; this is organised by *branch*.
Everything here was found by `wreath mutant` surviving on a guard no exploit
happened to exercise -- a truncated UTF-8 sequence, a start tag that ends
mid-attribute, an XML declaration with no terminator. A refusal nobody reaches
is a refusal nobody has checked.
"""

from __future__ import annotations

import pytest

from wreath.xml import (
    MAX_DEPTH_CEILING,
    XML_NAMESPACE,
    Limits,
    XMLRefusal,
    canonicalize_span,
    parse,
)

# --------------------------------------------------------------------------
# Truncation inside every construct
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"<a", "unexpected-end"),
        (b"<a ", "unexpected-end"),
        (b"<a b", "attribute-syntax"),
        (b"<a b=", "attribute-syntax"),
        (b'<a b="', "unexpected-end"),
        (b'<a b="value', "unexpected-end"),
        (b"<a/", "tag-syntax"),
        (b"<a>text", "mismatched-end-tag"),
        (b"<a></a", "tag-syntax"),
        (b"<a></", "invalid-name"),
        (b"<a>&", "entity-reference"),
        (b"<a>&#", "character-reference"),
        (b"<a>&#x", "character-reference"),
        (b'<a b="&', "entity-reference"),
    ],
)
def test_truncation_is_refused_wherever_it_lands(payload: bytes, reason: str) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.reason == reason


def test_a_slash_must_be_followed_by_a_close_bracket() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a/x>")
    assert "'/' must be followed by '>'" in str(caught.value)
    assert caught.value.reason == "tag-syntax"


def test_attributes_must_be_separated_by_whitespace() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<a b="1"c="2"/>')
    assert "whitespace is required between attributes" in str(caught.value)
    assert caught.value.reason == "tag-syntax"


def test_an_attribute_without_a_value_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a b/>")
    assert "an attribute needs a value" in str(caught.value)
    assert caught.value.reason == "attribute-syntax"


def test_an_unquoted_attribute_value_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a b=1/>")
    assert "an attribute value must be quoted" in str(caught.value)
    assert caught.value.reason == "attribute-syntax"


def test_single_quoted_attribute_values_are_accepted() -> None:
    assert parse(b"<a b='1'/>").root.attrib == {"b": "1"}


def test_an_end_tag_must_finish_with_a_close_bracket() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a></a x>")
    assert "an end tag must finish with '>'" in str(caught.value)
    assert caught.value.reason == "tag-syntax"


def test_whitespace_is_permitted_before_an_end_tag_close() -> None:
    assert parse(b"<a></a  >").root.tag == "a"


# --------------------------------------------------------------------------
# UTF-8 boundary conditions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (b"<r>\xc3", "the document ends inside a UTF-8 sequence"),
        (b"<r>\xe2\x9c", "the document ends inside a UTF-8 sequence"),
        (b"<r>\xf0\x9f\x8e", "the document ends inside a UTF-8 sequence"),
        (b"<r>\xc3\x28</r>", "is not a UTF-8 continuation byte"),
        (b"<r>\xe2\x28\xa1</r>", "is not a UTF-8 continuation byte"),
        (b"<r>\xc1\xbf</r>", "does not start a valid UTF-8 sequence"),
        (b"<r>\xf5\x8f\xbf\xbf</r>", "does not start a valid UTF-8 sequence"),
        (b"<r>\xfe</r>", "does not start a valid UTF-8 sequence"),
    ],
)
def test_utf8_boundaries(payload: bytes, fragment: str) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert fragment in str(caught.value)
    assert caught.value.reason == "encoding"


@pytest.mark.parametrize("payload", [b"<r>\xef\xbf\xbe</r>", b"<r>\xef\xbf\xbf</r>"])
def test_the_noncharacters_are_refused(payload: bytes) -> None:
    """U+FFFE and U+FFFF are well-formed UTF-8 and invalid XML characters."""
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert "is not a valid XML character" in str(caught.value)
    assert caught.value.reason == "encoding"


def test_the_widest_legal_codepoint_is_accepted() -> None:
    assert parse(b"<r>\xf4\x8f\xbf\xbf</r>").root.text == "\U0010ffff"


@pytest.mark.parametrize(
    "payload", [b"<r>&#xFFFE;</r>", b"<r>&#xFFFF;</r>", b"<r>&#x;</r>", b"<r>&#zz;</r>"]
)
def test_character_reference_edges(payload: bytes) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.reason == "character-reference"


def test_a_character_reference_may_produce_legal_whitespace() -> None:
    assert parse(b"<r>&#x9;&#xA;</r>").root.text == "\t\n"


# --------------------------------------------------------------------------
# The XML declaration
# --------------------------------------------------------------------------


def test_an_unterminated_declaration_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<?xml version="1.0"')
    assert "the XML declaration is not terminated" in str(caught.value)
    assert caught.value.reason == "unexpected-end"


def test_a_declaration_without_a_version_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<?xml ?><r/>")
    assert caught.value.reason == "version"


def test_a_target_beginning_xml_is_a_processing_instruction() -> None:
    """`<?xmlfoo?>` is not a declaration, so the PI refusal must catch it."""
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<?xmlfoo?><r/>")
    assert caught.value.reason == "processing-instruction"


def test_a_bare_lt_question_at_the_start_is_a_processing_instruction() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<?php ?><r/>")
    assert caught.value.reason == "processing-instruction"


def test_a_declaration_may_carry_standalone() -> None:
    doc = parse(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><r/>')
    assert doc.root.tag == "r"


def test_a_markup_declaration_that_is_not_a_doctype_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<!ENTITY x "y"><r/>')
    assert "markup declarations are refused" in str(caught.value)
    assert caught.value.reason == "markup-declaration"


def test_an_attlist_is_refused_as_a_markup_declaration() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r><!ATTLIST r a CDATA #IMPLIED></r>")
    assert caught.value.reason == "markup-declaration"


# --------------------------------------------------------------------------
# Namespace declaration syntax
# --------------------------------------------------------------------------


def test_a_declaration_with_an_empty_prefix_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:="urn:x"/>')
    assert caught.value.reason in {"invalid-name", "unbound-prefix"}


def test_the_xml_prefix_may_be_declared_to_its_own_uri() -> None:
    """Rebinding `xml` is refused; restating it correctly is legal XML."""
    doc = parse(b'<r xmlns:xml="' + XML_NAMESPACE.encode() + b'" xml:lang="en"/>')
    assert doc.root.attrib[f"{{{XML_NAMESPACE}}}lang"] == "en"


def test_a_qualified_name_with_two_colons_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:p="urn:p"><p:a:b/></r>')
    assert caught.value.reason == "invalid-name"


def test_an_attribute_named_with_two_colons_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:p="urn:p" p:a:b="1"/>')
    assert caught.value.reason == "invalid-name"


def test_a_name_may_not_start_with_a_digit() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<1a/>")
    assert caught.value.reason == "invalid-name"


def test_a_name_may_contain_digits_dots_and_hyphens() -> None:
    assert parse(b"<a-1.b/>").root.tag == "a-1.b"


def test_a_non_ascii_name_is_accepted_when_the_codepoint_is_a_name_char() -> None:
    assert parse("<élément/>".encode()).root.tag == "élément"


def test_a_non_ascii_name_char_outside_the_productions_is_refused() -> None:
    """U+00D7 (multiplication sign) sits in the gap between the ranges."""
    with pytest.raises(XMLRefusal) as caught:
        parse("<a×b/>".encode())
    assert "cannot appear in an XML name" in str(caught.value)
    assert caught.value.reason == "invalid-name"


# --------------------------------------------------------------------------
# Limits validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_depth": -1},
        {"max_elements": 0},
        {"max_attributes": -5},
        {"max_attribute_bytes": 0},
    ],
)
def test_a_nonpositive_bound_is_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        Limits(**kwargs)


def test_a_bool_is_not_an_acceptable_bound() -> None:
    """`True == 1`, so a bare int check would let `max_depth=True` through."""
    with pytest.raises(ValueError, match="must be a positive integer"):
        Limits(max_depth=True)


def test_a_non_integer_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        Limits(max_bytes="lots")  # type: ignore[arg-type]


def test_max_depth_is_capped_at_the_recursion_ceiling() -> None:
    """The C twin is recursive descent, so depth is a stack budget."""
    Limits(max_depth=MAX_DEPTH_CEILING)
    with pytest.raises(ValueError, match="recursion ceiling"):
        Limits(max_depth=MAX_DEPTH_CEILING + 1)


def test_parse_defaults_its_limits_when_none_is_passed() -> None:
    assert parse(b"<r/>", None).root.tag == "r"


def test_parse_refuses_a_str() -> None:
    with pytest.raises(TypeError, match="must be bytes"):
        parse("<r/>")  # type: ignore[arg-type]


def test_parse_accepts_a_bytearray_and_a_memoryview() -> None:
    assert parse(bytearray(b"<r/>")).root.tag == "r"
    assert parse(memoryview(b"<r/>")).root.tag == "r"


# --------------------------------------------------------------------------
# canonicalize_span
# --------------------------------------------------------------------------


def test_canonicalize_span_reads_the_named_bytes() -> None:
    source = b"<outer><a>v</a></outer>"
    assert canonicalize_span(source, 7, 15) == b"<a>v</a>"


def test_canonicalize_span_seeds_the_inherited_scope() -> None:
    source = b'<p:a xmlns:q="urn:q">v</p:a>'
    assert canonicalize_span(source, 0, len(source), (("p", "urn:p"),)) == (
        b'<p:a xmlns:p="urn:p">v</p:a>'
    )


@pytest.mark.parametrize(("start", "end"), [(-1, 4), (0, 0), (3, 2), (0, 999)])
def test_canonicalize_span_refuses_a_span_that_does_not_address_the_source(
    start: int, end: int
) -> None:
    with pytest.raises(ValueError, match="span does not address the source"):
        canonicalize_span(b"<a/>", start, end)


def test_canonicalize_span_defaults_its_limits() -> None:
    assert canonicalize_span(b"<a/>", 0, 4, (), (), None) == b"<a></a>"


def test_an_undeclared_default_namespace_is_rendered_in_the_canonical_form() -> None:
    """`xmlns=""` has to survive canonicalization to mean anything.

    A child that undeclares the default namespace is in no namespace. If the
    canonical form dropped the undeclaration, it would read as inheriting the
    parent's -- a different document with the same signature.
    """
    doc = parse(b'<r xmlns="urn:one"><c xmlns=""><d/></c></r>')
    assert doc.canonicalize() == (
        b'<r xmlns="urn:one"><c xmlns=""><d></d></c></r>'
    )
