from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from wreath.xml import Limits, parse

XML_NS = "http://www.w3.org/XML/1998/namespace"


def test_a_bare_element() -> None:
    doc = parse(b"<r/>")
    assert doc.root.tag == "r"
    assert doc.root.attrib == {}
    assert doc.root.text == ""
    assert doc.root.children == ()


def test_attributes_and_text() -> None:
    doc = parse(b'<r a="1" b="two">body</r>')
    assert doc.root.attrib == {"a": "1", "b": "two"}
    assert doc.root.text == "body"


def test_children_and_tails() -> None:
    doc = parse(b"<r>head<a/>mid<b/>tail</r>")
    assert doc.root.text == "head"
    first, second = doc.root.children
    assert (first.tag, first.tail) == ("a", "mid")
    assert (second.tag, second.tail) == ("b", "tail")


def test_iterating_an_element_yields_its_children() -> None:
    doc = parse(b"<r><a/><b/></r>")
    assert [child.tag for child in doc.root] == ["a", "b"]
    assert len(doc.root) == 2


def test_attribute_value_whitespace_is_normalized() -> None:
    doc = parse(b'<r a="one\ttwo\nthree"/>')
    assert doc.root.attrib["a"] == "one two three"


def test_a_character_reference_in_an_attribute_is_not_re_normalized() -> None:
    doc = parse(b'<r a="one&#x9;two"/>')
    assert doc.root.attrib["a"] == "one\ttwo"


def test_line_endings_are_normalized_to_lf() -> None:
    assert parse(b"<r>a\r\nb</r>").root.text == "a\nb"
    assert parse(b"<r>a\rb</r>").root.text == "a\nb"


# Namespaces


def test_default_namespace_applies_to_elements_not_attributes() -> None:
    doc = parse(b'<r xmlns="urn:d" a="1"/>')
    assert doc.root.tag == "{urn:d}r"
    assert doc.root.attrib == {"a": "1"}


def test_prefixed_names_expand() -> None:
    doc = parse(b'<p:r xmlns:p="urn:p" p:a="1"/>')
    assert doc.root.tag == "{urn:p}r"
    assert doc.root.attrib == {"{urn:p}a": "1"}


def test_a_default_namespace_is_inherited_by_descendants() -> None:
    doc = parse(b'<r xmlns="urn:d"><c/></r>')
    assert doc.root.children[0].tag == "{urn:d}c"


def test_a_default_namespace_can_be_shifted_by_a_descendant() -> None:
    doc = parse(b'<r xmlns="urn:one"><c xmlns="urn:two"><d/></c></r>')
    child = doc.root.children[0]
    assert child.tag == "{urn:two}c"
    assert child.children[0].tag == "{urn:two}d"


def test_a_default_namespace_can_be_undeclared_for_descendants() -> None:
    doc = parse(b'<r xmlns="urn:one"><c xmlns=""><d/></c></r>')
    child = doc.root.children[0]
    assert child.tag == "c"
    assert child.children[0].tag == "d"


def test_a_prefix_can_be_rebound_mid_document() -> None:
    doc = parse(b'<p:r xmlns:p="urn:one"><p:c xmlns:p="urn:two"/></p:r>')
    assert doc.root.tag == "{urn:one}r"
    assert doc.root.children[0].tag == "{urn:two}c"


def test_two_prefixes_for_one_uri_spell_the_same_expanded_name() -> None:
    doc = parse(b'<r xmlns:a="urn:x" xmlns:b="urn:x"><a:c/><b:c/></r>')
    first, second = doc.root.children
    assert first.tag == second.tag == "{urn:x}c"


def test_the_prefixed_and_unprefixed_spellings_are_different_names() -> None:
    doc = parse(b'<r xmlns:p="urn:p"><p:c/><c/></r>')
    prefixed, plain = doc.root.children
    assert prefixed.tag == "{urn:p}c"
    assert plain.tag == "c"


def test_the_xml_prefix_needs_no_declaration() -> None:
    doc = parse(b'<r xml:lang="en" xml:space="preserve"/>')
    assert doc.root.attrib == {
        f"{{{XML_NS}}}lang": "en",
        f"{{{XML_NS}}}space": "preserve",
    }


def test_namespace_declarations_are_reported_per_element() -> None:
    doc = parse(b'<r xmlns="urn:d" xmlns:p="urn:p"><c xmlns:q="urn:q"/></r>')
    assert dict(doc.root.nsdeclarations) == {"": "urn:d", "p": "urn:p"}
    assert dict(doc.root.children[0].nsdeclarations) == {"q": "urn:q"}


# Differential against the stdlib on documents both accept

SHARED = [
    b"<r/>",
    b"<r>text</r>",
    b'<r a="1" b="2"/>',
    b"<r><a/><b><c/></b></r>",
    b"<r>head<a/>mid<b/>tail</r>",
    b'<r xmlns="urn:d"><c/></r>',
    b'<p:r xmlns:p="urn:p"><p:c p:a="1"/></p:r>',
    b'<r xmlns="urn:one"><c xmlns="urn:two"/></r>',
    b"<r>&amp;&lt;&gt;&quot;&apos;</r>",
    b"<r>&#65;&#x42;</r>",
    b'<r a="one two"/>',
    b"<r>\xe2\x9c\x93 unicode \xf0\x9f\x8e\x84</r>",
    b'<?xml version="1.0" encoding="UTF-8"?><r><c/></r>',
]


def _stdlib_shape(element: ET.Element) -> object:
    return (
        element.tag,
        dict(element.attrib),
        element.text or "",
        (element.tail or ""),
        tuple(_stdlib_shape(child) for child in element),
    )


def _our_shape(element: object) -> object:
    return (
        element.tag,  # type: ignore[attr-defined]
        dict(element.attrib),  # type: ignore[attr-defined]
        element.text,  # type: ignore[attr-defined]
        element.tail,  # type: ignore[attr-defined]
        tuple(_our_shape(child) for child in element.children),  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize("source", SHARED)
def test_structure_agrees_with_elementtree(source: bytes) -> None:
    ours = _our_shape(parse(source).root)
    theirs = _stdlib_shape(ET.fromstring(source))
    assert ours == theirs


@pytest.mark.parametrize(
    "source",
    [
        b"<r><!-- a comment --></r>",
        b"<r><![CDATA[raw]]></r>",
        b"<r><?target data?></r>",
        b"<!DOCTYPE r><r/>",
        b"\xef\xbb\xbf<r/>",
    ],
    ids=["comment", "cdata", "pi", "doctype", "bom"],
)
def test_the_profile_is_narrower_than_the_stdlib(source: bytes) -> None:
    ET.fromstring(source)  # the stdlib accepts it
    from wreath.xml import XMLRefusal

    with pytest.raises(XMLRefusal):
        parse(source)


# Limits


def test_default_limits_accept_an_ordinary_assertion() -> None:
    body = b'<a ID="x">' + b"<c>v</c>" * 100 + b"</a>"
    assert len(parse(body).root.children) == 100


def test_limits_are_frozen() -> None:
    limits = Limits()
    with pytest.raises((AttributeError, TypeError)):
        limits.max_depth = 5  # type: ignore[misc]


def test_limits_reject_a_nonpositive_bound() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        Limits(max_depth=0)


def test_source_bytes_are_retained_verbatim() -> None:
    source = b'<r a="1">body</r>'
    assert parse(source).source == source
