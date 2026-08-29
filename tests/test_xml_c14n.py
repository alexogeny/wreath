from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from wreath.xml import parse

# Serialization rules


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"<a/>", b"<a></a>"),
        (b"<a></a>", b"<a></a>"),
        (b"<a>  </a>", b"<a>  </a>"),
        (b"<a><b/></a>", b"<a><b></b></a>"),
        (b'<a b="1" a="2"/>', b'<a a="2" b="1"></a>'),
        (b"<a>x &amp; y</a>", b"<a>x &amp; y</a>"),
        (b"<a>x &lt; y</a>", b"<a>x &lt; y</a>"),
        (b"<a>x &gt; y</a>", b"<a>x &gt; y</a>"),
        (b"<a>x > y</a>", b"<a>x &gt; y</a>"),
        (b'<a b="&quot;"/>', b'<a b="&quot;"></a>'),
        (b'<a b="&amp;"/>', b'<a b="&amp;"></a>'),
        (b'<a b="&lt;"/>', b'<a b="&lt;"></a>'),
        (b'<a b="x&#x9;y"/>', b'<a b="x&#x9;y"></a>'),
        (b'<a b="x&#xA;y"/>', b'<a b="x&#xA;y"></a>'),
        (b"<a>x&#xD;y</a>", b"<a>x&#xD;y</a>"),
        (b'<a b=">"/>', b'<a b=">"></a>'),
    ],
)
def test_serialization_rules(source: bytes, expected: bytes) -> None:
    assert parse(source).canonicalize() == expected


def test_a_carriage_return_in_text_is_normalized_before_it_is_escaped() -> None:
    assert parse(b"<a>x\ry</a>").canonicalize() == b"<a>x\ny</a>"
    assert parse(b"<a>x&#xD;y</a>").canonicalize() == b"<a>x&#xD;y</a>"


def test_attribute_order_is_by_namespace_then_local_name() -> None:
    source = b'<a xmlns:z="urn:z" xmlns:m="urn:m" z:k="1" m:k="2" b="3"/>'
    expected = b'<a xmlns:m="urn:m" xmlns:z="urn:z" b="3" m:k="2" z:k="1"></a>'
    assert parse(source).canonicalize() == expected


def test_the_default_declaration_precedes_prefixed_ones() -> None:
    source = b'<a xmlns:p="urn:p" xmlns="urn:d" p:x="1"/>'
    expected = b'<a xmlns="urn:d" xmlns:p="urn:p" p:x="1"></a>'
    assert parse(source).canonicalize() == expected


# Exclusivity: only visibly utilized prefixes are rendered


def test_an_unused_prefix_declaration_is_dropped() -> None:
    source = b'<p:a xmlns:p="urn:p" xmlns:unused="urn:u"><p:b/></p:a>'
    expected = b'<p:a xmlns:p="urn:p"><p:b></p:b></p:a>'
    assert parse(source).canonicalize() == expected


def test_a_prefix_used_only_by_an_attribute_is_rendered() -> None:
    source = b'<a xmlns:p="urn:p" p:x="1"/>'
    assert parse(source).canonicalize() == b'<a xmlns:p="urn:p" p:x="1"></a>'


def test_an_unprefixed_attribute_does_not_utilize_the_default_namespace() -> None:
    source = b'<a xmlns="urn:d" b="1"/>'
    assert parse(source).canonicalize() == b'<a xmlns="urn:d" b="1"></a>'
    nested = b'<p:a xmlns:p="urn:p" xmlns="urn:d" b="1"/>'
    assert parse(nested).canonicalize() == b'<p:a xmlns:p="urn:p" b="1"></p:a>'


def test_a_prefix_utilized_only_by_a_descendant_is_rendered_there() -> None:
    source = b'<a xmlns:p="urn:p"><p:b/></a>'
    expected = b'<a><p:b xmlns:p="urn:p"></p:b></a>'
    assert parse(source).canonicalize() == expected


def test_a_declaration_is_not_repeated_when_the_ancestor_already_rendered_it() -> None:
    source = b'<p:a xmlns:p="urn:p"><p:b><p:c/></p:b></p:a>'
    expected = b'<p:a xmlns:p="urn:p"><p:b><p:c></p:c></p:b></p:a>'
    assert parse(source).canonicalize() == expected


def test_a_rebound_prefix_is_redeclared_where_it_changes() -> None:
    source = b'<p:a xmlns:p="urn:one"><p:b xmlns:p="urn:two"/></p:a>'
    expected = b'<p:a xmlns:p="urn:one"><p:b xmlns:p="urn:two"></p:b></p:a>'
    assert parse(source).canonicalize() == expected


# Subtree canonicalization -- the property signatures depend on


def test_a_subtree_inherits_only_the_prefixes_it_utilizes() -> None:
    source = b'<root xmlns:p="urn:p" xmlns:q="urn:q"><p:child>t</p:child></root>'
    doc = parse(source)
    child = doc.root.children[0]
    assert doc.canonicalize(child) == b'<p:child xmlns:p="urn:p">t</p:child>'


def test_the_same_subtree_canonicalizes_identically_in_two_documents() -> None:
    first = parse(b'<r xmlns:p="urn:p"><p:a ID="x">v</p:a></r>')
    second = parse(
        b'<other xmlns:p="urn:p" xmlns:extra="urn:e" xmlns="urn:d">'
        b'<wrap><p:a ID="x">v</p:a></wrap></other>'
    )
    assert first.canonicalize(first.find_id("x")) == second.canonicalize(second.find_id("x"))


def test_inclusive_prefix_list_forces_an_otherwise_dropped_declaration() -> None:
    source = b'<root xmlns:p="urn:p" xmlns:q="urn:q"><p:child/></root>'
    doc = parse(source)
    child = doc.root.children[0]
    assert doc.canonicalize(child) == b'<p:child xmlns:p="urn:p"></p:child>'
    with_q = doc.canonicalize(child, inclusive_prefixes=("q",))
    assert with_q == b'<p:child xmlns:p="urn:p" xmlns:q="urn:q"></p:child>'


def test_inclusive_prefix_list_accepts_the_default_namespace_token() -> None:
    source = b'<root xmlns="urn:d" xmlns:p="urn:p"><p:child/></root>'
    doc = parse(source)
    child = doc.root.children[0]
    plain = doc.canonicalize(child)
    assert plain == b'<p:child xmlns:p="urn:p"></p:child>'
    forced = doc.canonicalize(child, inclusive_prefixes=("#default",))
    assert forced == b'<p:child xmlns="urn:d" xmlns:p="urn:p"></p:child>'


def test_an_inclusive_prefix_that_is_not_in_scope_is_simply_absent() -> None:
    doc = parse(b'<r xmlns:p="urn:p"><p:c/></r>')
    child = doc.root.children[0]
    assert doc.canonicalize(child, inclusive_prefixes=("nope",)) == (b'<p:c xmlns:p="urn:p"></p:c>')


def test_canonicalizing_the_root_is_the_default() -> None:
    doc = parse(b"<a><b/></a>")
    assert doc.canonicalize() == doc.canonicalize(doc.root)


def test_text_around_children_is_preserved_in_order() -> None:
    doc = parse(b"<a>one<b>two</b>three</a>")
    assert doc.canonicalize() == b"<a>one<b>two</b>three</a>"


# Differential: where C14N 2.0 and exclusive c14n 1.0 agree, stdlib agrees too

NAMESPACE_FREE = [
    b"<a></a>",
    b"<a>text</a>",
    b'<a b="1" a="2"><c/></a>',
    b"<a>one<b>two</b>three</a>",
    b"<a>&amp;&lt;&gt;</a>",
    b'<a b="x&#x9;y"/>',
    b"<a><b><c><d>deep</d></c></b></a>",
]


@pytest.mark.parametrize("source", NAMESPACE_FREE)
def test_matches_stdlib_c14n_on_namespace_free_documents(source: bytes) -> None:
    ours = parse(source).canonicalize()
    theirs = ET.canonicalize(source.decode(), strip_text=False).encode()
    assert ours == theirs
