"""Deterministic fuzzing of the XML parser.

A C parser on a security boundary has two obligations under arbitrary input:
never crash the interpreter, and never disagree with its pure twin. Both are
checked here over a seeded corpus, so a failure reproduces from the seed printed
in the assertion rather than from a saved artifact.

Marked ``fuzz`` so the default marker expression skips it; ``-m ''`` runs it.
"""

from __future__ import annotations

import random

import pytest

from wreath import xml as facade
from wreath._pure import xml as pure
from wreath.xml import Limits, XMLRefusal

pytestmark = pytest.mark.fuzz

native = pytest.importorskip(
    "wreath._native._core",
    reason="the C accelerator is absent from this build",
)

#: Fragments chosen to land on the parser's decision points -- tag boundaries,
#: entity starts, namespace syntax, and the byte ranges it refuses.
FRAGMENTS = [
    b"<a>",
    b"</a>",
    b"<a/>",
    b"<a ",
    b'b="1"',
    b'xmlns="urn:d"',
    b'xmlns:p="urn:p"',
    b"<p:c/>",
    b"&amp;",
    b"&#65;",
    b"&#x",
    b"&",
    b";",
    b"<!--",
    b"-->",
    b"<![CDATA[",
    b"]]>",
    b"<?xml ",
    b"?>",
    b"<!DOCTYPE",
    b"]>",
    b"'",
    b'"',
    b"/",
    b">",
    b"<",
    b"=",
    b" ",
    b"\t",
    b"\r\n",
    b"\x00",
    b"\xff",
    b"\xc0\xaf",
    b"\xed\xa0\x80",
    b"\xf0\x9f\x8e\x84",
    b"text",
    b":",
    b"\xef\xbb\xbf",
]


def _corpus(seed: int, count: int) -> list[bytes]:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        pieces = [rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 24))]
        out.append(b"".join(pieces))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_arbitrary_input_either_parses_or_is_refused(seed: int) -> None:
    """No input produces anything other than a tree or an XMLRefusal."""
    limits = Limits()
    for payload in _corpus(seed, 400):
        try:
            facade._parse_native(payload, limits)
        except XMLRefusal:
            continue
        except Exception as unexpected:  # noqa: BLE001 - the assertion is the point
            pytest.fail(f"seed={seed} payload={payload!r} raised {unexpected!r}")


@pytest.mark.parametrize("seed", [11, 12, 13, 14, 15, 16])
def test_both_backends_agree_on_arbitrary_input(seed: int) -> None:
    """The parity contract, under input nobody designed."""
    limits = Limits()
    for payload in _corpus(seed, 300):
        c_refusal = python_refusal = None
        c_tree = python_tree = None
        try:
            c_tree = facade._parse_native(payload, limits).canonicalize()
        except XMLRefusal as refusal:
            c_refusal = refusal.reason
        try:
            python_tree = pure.parse_document(payload, limits).canonicalize()
        except XMLRefusal as refusal:
            python_refusal = refusal.reason
        assert c_refusal == python_refusal, f"seed={seed} payload={payload!r}"
        assert c_tree == python_tree, f"seed={seed} payload={payload!r}"


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_canonicalizing_a_parsed_document_is_idempotent(seed: int) -> None:
    """Canonical output must itself parse to the same canonical bytes.

    A signature is computed over the canonical form, so a canonicalizer whose
    output canonicalizes differently would make verification depend on how many
    times the document had been round-tripped.
    """
    limits = Limits()
    for payload in _corpus(seed, 300):
        try:
            once = facade._parse_native(payload, limits).canonicalize()
        except XMLRefusal:
            continue
        twice = facade._parse_native(once, limits).canonicalize()
        assert once == twice, f"seed={seed} payload={payload!r}"


@pytest.mark.parametrize("seed", [31, 32])
def test_every_span_indexes_the_source(seed: int) -> None:
    """A byte range that does not address the source is a provenance bug."""
    limits = Limits()
    for payload in _corpus(seed, 300):
        try:
            doc = facade._parse_native(payload, limits)
        except XMLRefusal:
            continue

        def walk(element: object, source: bytes = payload) -> None:
            start, end = element.span  # type: ignore[attr-defined]
            assert 0 <= start < end <= len(source)
            for child in element.children:  # type: ignore[attr-defined]
                walk(child, source)

        walk(doc.root)


def test_deeply_nested_input_is_refused_rather_than_recursing() -> None:
    """Depth is bounded in the parser, so no input can exhaust the C stack."""
    limits = Limits()
    payload = b"<a>" * 50_000
    with pytest.raises(XMLRefusal) as caught:
        facade._parse_native(payload, limits)
    assert caught.value.reason in {"depth", "size"}
    with pytest.raises(XMLRefusal):
        pure.parse_document(payload, limits)
