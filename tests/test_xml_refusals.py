from __future__ import annotations

import pytest

from wreath.xml import Limits, XMLRefusal, parse

# Entity expansion

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<lolz>&lol4;</lolz>"""

QUADRATIC_BLOWUP = (
    b'<?xml version="1.0"?>\n'
    b'<!DOCTYPE bomb [<!ENTITY a "' + b"A" * 1000 + b'">]>\n'
    b"<bomb>" + b"&a;" * 1000 + b"</bomb>"
)

RECURSIVE_ENTITY = (
    b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY x "&y;"><!ENTITY y "&x;">]>\n<r>&x;</r>'
)


@pytest.mark.parametrize(
    "payload",
    [BILLION_LAUGHS, QUADRATIC_BLOWUP, RECURSIVE_ENTITY],
    ids=["billion-laughs", "quadratic-blowup", "recursive-entity"],
)
def test_entity_expansion_refused_at_the_doctype(payload: bytes) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert "document type declaration" in str(caught.value)
    assert caught.value.reason == "doctype"


def test_undeclared_entity_reference_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r>&custom;</r>")
    assert "entity reference" in str(caught.value)
    assert "&custom;" in str(caught.value)
    assert caught.value.reason == "entity-reference"


def test_the_five_predefined_entities_are_the_whole_vocabulary() -> None:
    doc = parse(b"<r>&lt;&gt;&amp;&quot;&apos;</r>")
    assert doc.root.text == "<>&\"'"


# XXE

XXE_CASES = {
    "system-file": b'<!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>',
    "system-http": b'<!DOCTYPE r [<!ENTITY e SYSTEM "http://evil/x">]><r>&e;</r>',
    "public-id": b'<!DOCTYPE r PUBLIC "-//X//EN" "http://evil/x.dtd"><r/>',
    "external-subset": b'<!DOCTYPE r SYSTEM "http://evil/x.dtd"><r/>',
    "parameter-entity": (b'<!DOCTYPE r [<!ENTITY % p SYSTEM "http://evil/e.dtd"> %p;]><r/>'),
    "php-filter": (
        b"<!DOCTYPE r [<!ENTITY e SYSTEM "
        b'"php://filter/convert.base64-encode/resource=index.php">]><r>&e;</r>'
    ),
}


@pytest.mark.parametrize("payload", list(XXE_CASES.values()), ids=list(XXE_CASES))
def test_xxe_cannot_be_expressed(payload: bytes) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.reason == "doctype"
    assert "document type declaration" in str(caught.value)


def test_no_network_or_filesystem_access_is_reachable() -> None:
    payload = b'<!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>'
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.offset == 0


# Processing instructions, comments, CDATA


def test_processing_instruction_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r><?php echo "x"; ?></r>')
    assert "processing instruction" in str(caught.value)
    assert caught.value.reason == "processing-instruction"


def test_xml_declaration_is_not_a_processing_instruction() -> None:
    doc = parse(b'<?xml version="1.0" encoding="UTF-8"?><r>ok</r>')
    assert doc.root.text == "ok"


def test_xml_declaration_is_only_allowed_at_offset_zero() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r><?xml version="1.0"?></r>')
    assert "processing instruction" in str(caught.value)


def test_comment_is_refused_because_it_splits_a_text_node() -> None:
    payload = b"<NameID>admin@corp.example<!---->.attacker.example</NameID>"
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert "comment" in str(caught.value)
    assert caught.value.reason == "comment"


def test_cdata_is_refused_because_it_is_a_second_spelling_of_text() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r><![CDATA[<script>]]></r>")
    assert "CDATA" in str(caught.value)
    assert caught.value.reason == "cdata"


def test_cdata_splitting_cannot_hide_markup() -> None:
    payload = b"<r>admin<![CDATA[]]>@evil</r>"
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.reason == "cdata"


# Encoding


def test_declared_encoding_other_than_utf8_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<?xml version="1.0" encoding="UTF-7"?><r/>')
    assert "encoding" in str(caught.value)
    assert "UTF-7" in str(caught.value)
    assert caught.value.reason == "encoding"


@pytest.mark.parametrize("label", [b"utf-16", b"ISO-8859-1", b"windows-1252"])
def test_only_utf8_is_a_declarable_encoding(label: bytes) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<?xml version="1.0" encoding="' + label + b'"?><r/>')
    assert caught.value.reason == "encoding"


def test_utf8_is_declarable_in_either_case() -> None:
    assert parse(b'<?xml version="1.0" encoding="utf-8"?><r/>').root.tag == "r"
    assert parse(b'<?xml version="1.0" encoding="UTF-8"?><r/>').root.tag == "r"


def test_byte_order_mark_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"\xef\xbb\xbf<r/>")
    assert "byte order mark" in str(caught.value)
    assert caught.value.reason == "byte-order-mark"


@pytest.mark.parametrize(
    ("payload", "ids"),
    [
        (b"<r>\xc0\xaf</r>", "overlong-solidus"),
        (b"<r>\xe0\x80\xaf</r>", "overlong-three-byte"),
        (b"<r>\xed\xa0\x80</r>", "unpaired-high-surrogate"),
        (b"<r>\xed\xb0\x80</r>", "unpaired-low-surrogate"),
        (b"<r>\xf5\x80\x80\x80</r>", "beyond-u10ffff"),
        (b"<r>\x80</r>", "bare-continuation"),
        (b"<r>\xc3</r>", "truncated-sequence"),
    ],
)
def test_malformed_utf8_is_refused(payload: bytes, ids: str) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert "UTF-8" in str(caught.value)
    assert caught.value.reason == "encoding"


@pytest.mark.parametrize("byte", [0x00, 0x01, 0x08, 0x0B, 0x0C, 0x1F])
def test_control_bytes_are_refused(byte: int) -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r>" + bytes([byte]) + b"</r>")
    assert "control character" in str(caught.value)
    assert caught.value.reason == "control-character"


def test_tab_newline_and_carriage_return_are_legal_whitespace() -> None:
    doc = parse(b"<r>a\tb\nc\rd</r>")
    assert doc.root.text == "a\tb\nc\nd"


def test_numeric_character_reference_cannot_smuggle_a_control_byte() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r>&#0;</r>")
    assert "character reference" in str(caught.value)
    assert caught.value.reason == "character-reference"


def test_numeric_character_reference_cannot_exceed_unicode() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r>&#x110000;</r>")
    assert caught.value.reason == "character-reference"


def test_numeric_character_reference_cannot_be_a_surrogate() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r>&#xD800;</r>")
    assert caught.value.reason == "character-reference"


def test_valid_numeric_character_references_decode() -> None:
    doc = parse(b"<r>&#65;&#x42;&#x1F600;</r>")
    assert doc.root.text == "AB\U0001f600"


# Structure and bounds


def test_unclosed_tag_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a><b></a>")
    assert "end tag" in str(caught.value)
    assert caught.value.reason == "mismatched-end-tag"


def test_truncated_document_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a><b>")
    assert caught.value.reason in {"unexpected-end", "mismatched-end-tag"}


def test_trailing_content_after_the_root_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<a/><b/>")
    assert "after the root element" in str(caught.value)
    assert caught.value.reason == "trailing-content"


def test_two_roots_cannot_hide_a_second_assertion() -> None:
    with pytest.raises(XMLRefusal):
        parse(b"<Assertion ID='a'/><Assertion ID='a'/>")


def test_depth_bound_is_enforced() -> None:
    payload = b"<a>" * 200 + b"</a>" * 200
    with pytest.raises(XMLRefusal) as caught:
        parse(payload, Limits(max_depth=64))
    assert "nesting depth" in str(caught.value)
    assert caught.value.reason == "depth"


def test_document_size_bound_is_enforced() -> None:
    payload = b"<r>" + b"x" * 5000 + b"</r>"
    with pytest.raises(XMLRefusal) as caught:
        parse(payload, Limits(max_bytes=1024))
    assert "document size" in str(caught.value)
    assert caught.value.reason == "size"


def test_element_count_bound_is_enforced() -> None:
    payload = b"<r>" + b"<c/>" * 500 + b"</r>"
    with pytest.raises(XMLRefusal) as caught:
        parse(payload, Limits(max_elements=100))
    assert "element count" in str(caught.value)
    assert caught.value.reason == "elements"


def test_attribute_count_bound_is_enforced() -> None:
    attrs = b" ".join(b'a%d="v"' % i for i in range(300))
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<r " + attrs + b"/>", Limits(max_attributes=32))
    assert "attribute count" in str(caught.value)
    assert caught.value.reason == "attributes"


def test_attribute_value_size_bound_is_enforced() -> None:
    payload = b'<r a="' + b"x" * 5000 + b'"/>'
    with pytest.raises(XMLRefusal) as caught:
        parse(payload, Limits(max_attribute_bytes=256))
    assert "attribute value" in str(caught.value)
    assert caught.value.reason == "attribute-size"


def test_duplicate_attribute_on_one_element_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r a="1" a="2"/>')
    assert "duplicate attribute" in str(caught.value)
    assert caught.value.reason == "duplicate-attribute"


def test_duplicate_attribute_through_different_prefixes_is_refused() -> None:
    payload = b'<r xmlns:p="urn:x" xmlns:q="urn:x" p:a="1" q:a="2"/>'
    with pytest.raises(XMLRefusal) as caught:
        parse(payload)
    assert caught.value.reason == "duplicate-attribute"


def test_empty_input_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"")
    assert caught.value.reason == "unexpected-end"


def test_text_before_the_root_element_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"junk<r/>")
    assert caught.value.reason == "content-before-root"


def test_a_lone_gt_in_text_is_accepted_but_lt_is_not() -> None:
    assert parse(b"<r>a > b</r>").root.text == "a > b"
    with pytest.raises(XMLRefusal):
        parse(b"<r>a < b</r>")


# Namespaces


def test_undeclared_prefix_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b"<p:r/>")
    assert "unbound namespace prefix" in str(caught.value)
    assert caught.value.reason == "unbound-prefix"


def test_undeclared_attribute_prefix_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r p:a="1"/>')
    assert caught.value.reason == "unbound-prefix"


def test_xmlns_prefix_cannot_be_rebound() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:xmlns="urn:x"/>')
    assert caught.value.reason == "reserved-prefix"


def test_xml_prefix_cannot_be_rebound() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:xml="urn:x"/>')
    assert caught.value.reason == "reserved-prefix"


def test_the_xml_prefix_is_predeclared() -> None:
    doc = parse(b'<r xml:lang="en"/>')
    assert doc.root.attrib["{http://www.w3.org/XML/1998/namespace}lang"] == "en"


def test_empty_uri_for_a_prefix_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<r xmlns:p=""><p:c/></r>')
    assert caught.value.reason == "empty-prefix-uri"


def test_xml_version_other_than_1_0_is_refused() -> None:
    with pytest.raises(XMLRefusal) as caught:
        parse(b'<?xml version="1.1"?><r/>')
    assert "version" in str(caught.value)
    assert caught.value.reason == "version"
