from __future__ import annotations

from wreath import xml
from wreath._fuzz import XML_STRATEGY, FuzzTarget
from wreath.xml import Limits, XMLRefusal

from ._corpus import load_versioned

_LIMITS = Limits(
    max_bytes=65_536,
    max_depth=128,
    max_elements=8_192,
    max_attributes=256,
    max_attribute_bytes=16_384,
)


def run(data: bytes) -> tuple[str, ...]:
    try:
        document = xml._parse_native(data, _LIMITS)
    except XMLRefusal as refusal:
        return (f"xml:refused:{refusal.reason}",)

    stack = [document.root]
    element_count = 0
    while stack:
        element = stack.pop()
        start, end = element.span
        if not 0 <= start < end <= len(data):
            raise AssertionError(
                f"XML element span {element.span!r} does not address a non-empty source range"
            )
        element_count += 1
        stack.extend(element.children)

    canonical = document.canonicalize()
    reparsed = xml._parse_native(canonical, _LIMITS)
    if reparsed.canonicalize() != canonical:
        raise AssertionError("XML canonicalization is not idempotent")
    return (
        "xml:parsed",
        "xml:canonical",
        "xml:namespace" if b"xmlns" in data else "xml:no-namespace",
        "xml:tree:multiple" if element_count > 1 else "xml:tree:single",
    )


TARGET = FuzzTarget(
    "xml-parser",
    run,
    seeds=load_versioned("xml"),
    dictionary=(
        b"<a>",
        b"</a>",
        b"<a/>",
        b"xmlns=\"urn:wreath\"",
        b"&amp;",
        b"&#65;",
        b"<!DOCTYPE",
        b"<![CDATA[",
        b"<!--",
        b"<?xml ",
        b"\x00",
        b"\xc0\xaf",
    ),
    source_files=(
        "src/wreath/xml.py",
        "src/wreath/_xml_model.py",
        "src/wreath/_native/xml.c",
    ),
    operator_names=(
        "guard.always-fires",
        "guard.never-fires",
        "guard.remove-raise",
        "predicate.always-true",
        "predicate.drop-operand",
        "value.widen-bound",
    ),
    strategy=XML_STRATEGY,
)
