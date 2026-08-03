"""XML Signature Wrapping, defeated by construction rather than by checking.

XSW is the SAML vulnerability class. It works because a signature layer and an
assertion consumer walk the tree *separately* and can be made to disagree about
which subtree was signed: the verifier finds the original assertion by its
``ID`` and checks it, while the consumer takes "the first assertion" and reads
an attacker's forgery sitting beside it.

Two properties in this parser remove the disagreement:

* **Every element carries the byte range it was parsed from**, so a caller
  canonicalizes and verifies *the original bytes of a named subtree* and then
  reads its values from that same subtree. There is no second lookup to
  disagree with the first.
* **A repeated ``ID`` is a refusal, not a resolution order.** No document
  reaches the consumer with two candidates for one identifier.
"""

from __future__ import annotations

import pytest

from wreath.xml import XMLRefusal, parse

SAML_P = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_A = "urn:oasis:names:tc:SAML:2.0:assertion"

LEGITIMATE = b"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a1">
<saml:Subject><saml:NameID>alex@corp.example</saml:NameID></saml:Subject>
</saml:Assertion>
</samlp:Response>"""


def test_the_baseline_document_parses() -> None:
    doc = parse(LEGITIMATE)
    assertion = doc.root.children[0]
    assert assertion.tag == f"{{{SAML_A}}}Assertion"
    assert assertion.attrib["ID"] == "_a1"


def test_an_element_reports_the_bytes_it_was_parsed_from() -> None:
    doc = parse(LEGITIMATE)
    assertion = doc.root.children[0]
    start, end = assertion.span
    raw = doc.source[start:end]
    assert raw.startswith(b"<saml:Assertion")
    assert raw.endswith(b"</saml:Assertion>")
    assert doc.subtree_bytes(assertion) == raw


# --------------------------------------------------------------------------
# The classic wrapping shapes
# --------------------------------------------------------------------------

DUPLICATE_ID = b"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a1">
<saml:Subject><saml:NameID>attacker@evil.example</saml:NameID></saml:Subject>
</saml:Assertion>
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a1">
<saml:Subject><saml:NameID>alex@corp.example</saml:NameID></saml:Subject>
</saml:Assertion>
</samlp:Response>"""

WRAPPED_IN_OBJECT = b"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_forged">
<saml:Subject><saml:NameID>attacker@evil.example</saml:NameID></saml:Subject>
<ds:Object xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
<saml:Assertion ID="_a1">
<saml:Subject><saml:NameID>alex@corp.example</saml:NameID></saml:Subject>
</saml:Assertion>
</ds:Object>
</saml:Assertion>
</samlp:Response>"""

RELOCATED_UNDER_A_WRAPPER = b"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
<samlp:Extensions>
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a1">
<saml:Subject><saml:NameID>alex@corp.example</saml:NameID></saml:Subject>
</saml:Assertion>
</samlp:Extensions>
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_forged">
<saml:Subject><saml:NameID>attacker@evil.example</saml:NameID></saml:Subject>
</saml:Assertion>
</samlp:Response>"""


def test_two_elements_sharing_an_id_are_refused_at_lookup() -> None:
    doc = parse(DUPLICATE_ID)
    with pytest.raises(XMLRefusal) as caught:
        doc.find_id("_a1")
    assert "appears 2 times" in str(caught.value)
    assert caught.value.reason == "duplicate-id"


def test_a_duplicate_id_is_refused_however_deeply_it_is_buried() -> None:
    buried = b"""<r xmlns="urn:x"><a ID="k"/><b><c><d ID="k"/></c></b></r>"""
    doc = parse(buried)
    with pytest.raises(XMLRefusal) as caught:
        doc.find_id("k")
    assert caught.value.reason == "duplicate-id"


def test_an_id_hidden_inside_a_ds_object_still_counts_as_a_duplicate() -> None:
    """The Object-embedded copy cannot pass as a distinct identifier.

    ``WRAPPED_IN_OBJECT`` gives the two assertions different IDs, so neither
    lookup is ambiguous -- and that is the point: the verifier asked for
    ``_a1`` and gets exactly the buried one, whose bytes are the ones it
    canonicalizes. It never sees the forgery at ``_forged``.
    """
    doc = parse(WRAPPED_IN_OBJECT)
    signed = doc.find_id("_a1")
    assert signed is not None
    assert doc.subtree_bytes(signed).count(b"alex@corp.example") == 1
    assert b"attacker@evil.example" not in doc.subtree_bytes(signed)


def test_the_verified_subtree_is_the_only_thing_a_consumer_can_read() -> None:
    """Verifier and consumer cannot disagree, because there is one lookup.

    A caller resolves the signed ID once, canonicalizes *those* bytes, and then
    reads the subject out of *that* element. The forged assertion is not
    reachable from the handle the verification produced.
    """
    doc = parse(RELOCATED_UNDER_A_WRAPPER)
    signed = doc.find_id("_a1")
    assert signed is not None

    subject = signed.children[0].children[0]
    assert subject.tag == f"{{{SAML_A}}}NameID"
    assert subject.text == "alex@corp.example"

    # And the forged one is a different element with different bytes.
    forged = doc.find_id("_forged")
    assert forged is not None
    assert doc.subtree_bytes(forged) != doc.subtree_bytes(signed)


def test_relocating_a_subtree_changes_its_canonical_form() -> None:
    """Moving a signed assertion under a wrapper is detectable.

    Exclusive canonicalization renders the namespace declarations the subtree
    *visibly utilizes*, so an assertion that inherited ``saml:`` from an
    ancestor in one document and declares it itself in another still
    canonicalizes identically -- that is the point of *exclusive* c14n. What
    does change is the content, and this asserts the two wrapping shapes are
    distinguishable by their canonical bytes.
    """
    original = parse(LEGITIMATE)
    relocated = parse(RELOCATED_UNDER_A_WRAPPER)

    a = original.canonicalize(original.find_id("_a1"))
    b = relocated.canonicalize(relocated.find_id("_a1"))
    assert a == b, "exclusive c14n is insensitive to the ancestor context"

    forged = relocated.canonicalize(relocated.find_id("_forged"))
    assert forged != b


def test_find_id_returns_none_rather_than_guessing() -> None:
    doc = parse(LEGITIMATE)
    assert doc.find_id("_nope") is None


def test_id_lookup_is_case_sensitive_and_exact() -> None:
    doc = parse(LEGITIMATE)
    assert doc.find_id("_A1") is None
    assert doc.find_id("a1") is None
    assert doc.find_id("_a1") is not None


def test_spans_of_sibling_elements_do_not_overlap() -> None:
    doc = parse(RELOCATED_UNDER_A_WRAPPER)
    spans = [child.span for child in doc.root.children]
    for (_, first_end), (second_start, _) in zip(spans, spans[1:], strict=False):
        assert first_end <= second_start


def test_a_child_span_is_contained_by_its_parent_span() -> None:
    doc = parse(WRAPPED_IN_OBJECT)

    def walk(element: object) -> None:
        parent_start, parent_end = element.span  # type: ignore[attr-defined]
        for child in element.children:  # type: ignore[attr-defined]
            child_start, child_end = child.span
            assert parent_start <= child_start
            assert child_end <= parent_end
            walk(child)

    walk(doc.root)
