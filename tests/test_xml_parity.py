"""The C parser and its pure-Python twin must not disagree about anything.

Two implementations of one parser is the shape that produces a signature-
wrapping bug: a verifier running one and a consumer running the other can be
made to read different documents. Wreath ships both because ``WREATH_PURE=1``
is a supported execution mode, so the disagreement has to be impossible rather
than unlikely.

This drives both backends directly -- not through the facade, which has already
chosen one -- over every document in the corpus, including every exploit, and
asserts they agree on the tree, on the byte spans, on the canonical bytes, and
on the *reason* for each refusal.
"""

from __future__ import annotations

import os

import pytest
from test_xml_c14n import NAMESPACE_FREE
from test_xml_parse import SHARED
from test_xml_refusals import BILLION_LAUGHS, QUADRATIC_BLOWUP, RECURSIVE_ENTITY, XXE_CASES
from test_xml_wrapping import (
    DUPLICATE_ID,
    LEGITIMATE,
    RELOCATED_UNDER_A_WRAPPER,
    WRAPPED_IN_OBJECT,
)

from wreath import xml as facade
from wreath._pure import xml as pure
from wreath.xml import Limits, XMLRefusal

native = pytest.importorskip(
    "wreath._native._core",
    reason="the C accelerator is absent from this build",
)

ACCEPTED = [
    *SHARED,
    *NAMESPACE_FREE,
    LEGITIMATE,
    DUPLICATE_ID,
    WRAPPED_IN_OBJECT,
    RELOCATED_UNDER_A_WRAPPER,
    b'<r xmlns:p="urn:p" xmlns:q="urn:q"><p:c q:a="1">t</p:c></r>',
    b"<r>&#65;&#x1F600;</r>",
    b'<r xml:lang="en"><c xmlns=""/></r>',
]

REFUSED = [
    BILLION_LAUGHS,
    QUADRATIC_BLOWUP,
    RECURSIVE_ENTITY,
    *XXE_CASES.values(),
    b"<r><!--c--></r>",
    b"<r><![CDATA[x]]></r>",
    b"<r><?pi?></r>",
    b"\xef\xbb\xbf<r/>",
    b'<?xml version="1.0" encoding="UTF-16"?><r/>',
    b"<r>\xed\xa0\x80</r>",
    b"<r>\xc0\xaf</r>",
    b"<r>\x00</r>",
    b"<r>&#0;</r>",
    b"<r>&#xD800;</r>",
    b"<p:r/>",
    b'<r a="1" a="2"/>',
    b"<a><b></a>",
    b"<a/><b/>",
    b"",
    b"junk<r/>",
    b"<r>&custom;</r>",
    b'<r xmlns:xml="urn:x"/>',
    b'<?xml version="1.1"?><r/>',
]


def _shape(element: object) -> object:
    return (
        element.tag,  # type: ignore[attr-defined]
        dict(element.attrib),  # type: ignore[attr-defined]
        element.text,  # type: ignore[attr-defined]
        element.tail,  # type: ignore[attr-defined]
        element.span,  # type: ignore[attr-defined]
        tuple(element.nsdeclarations),  # type: ignore[attr-defined]
        tuple(_shape(child) for child in element.children),  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize("source", ACCEPTED)
def test_both_backends_build_the_same_tree(source: bytes) -> None:
    limits = Limits()
    assert _shape(facade._parse_native(source, limits).root) == _shape(
        pure.parse_document(source, limits).root
    )


@pytest.mark.parametrize("source", ACCEPTED)
def test_both_backends_canonicalize_to_the_same_bytes(source: bytes) -> None:
    limits = Limits()
    from_c = facade._parse_native(source, limits)
    from_python = pure.parse_document(source, limits)
    assert from_c.canonicalize() == from_python.canonicalize()


@pytest.mark.parametrize("source", REFUSED)
def test_both_backends_refuse_for_the_same_reason(source: bytes) -> None:
    limits = Limits()
    with pytest.raises(XMLRefusal) as from_c:
        facade._parse_native(source, limits)
    with pytest.raises(XMLRefusal) as from_python:
        pure.parse_document(source, limits)
    assert from_c.value.reason == from_python.value.reason
    assert str(from_c.value) == str(from_python.value)


@pytest.mark.parametrize(
    "limits",
    [
        Limits(max_depth=3),
        Limits(max_bytes=64),
        Limits(max_elements=4),
        Limits(max_attributes=1),
        Limits(max_attribute_bytes=4),
    ],
)
def test_both_backends_enforce_a_bound_identically(limits: Limits) -> None:
    source = b'<r a="aaaaaaaa" b="bb"><x><y><z/></y></x><w/></r>'
    try:
        expected = pure.parse_document(source, limits)
    except XMLRefusal as refusal:
        with pytest.raises(XMLRefusal) as from_c:
            facade._parse_native(source, limits)
        assert from_c.value.reason == refusal.reason
    else:
        assert _shape(facade._parse_native(source, limits).root) == _shape(expected.root)


def test_the_facade_is_running_the_c_backend_in_this_build() -> None:
    """Falsifies the parity suite itself.

    If the facade silently fell back to the pure twin, every test above would
    compare the pure implementation against itself and pass while proving
    nothing -- the exact failure mode AGENTS.md warns about for a suite that
    looks green while executing nothing.

    The check is on the entry points this suite actually calls, not on
    `BACKEND`: under ``WREATH_PURE=1`` the facade resolves to the pure twin by
    design, and parity still has to be proven in that mode.
    """
    assert facade._require_native("xml_parse") is native
    assert facade._require_native("xml_c14n") is native
    assert hasattr(native, "xml_parse")
    document = facade._parse_native(LEGITIMATE, Limits())
    assert document.canonicalizer is facade._canonicalize_native


def test_the_facade_still_honours_wreath_pure() -> None:
    """The gate the fix must not have widened.

    `_parse_native` reaches the C code in either mode; `BACKEND` and the
    ordinary `parse` path must keep following `WREATH_PURE` instead.
    """
    forced_pure = os.environ.get("WREATH_PURE") == "1"
    assert facade.BACKEND == ("pure" if forced_pure else "native")
    assert (facade._native is None) is forced_pure
