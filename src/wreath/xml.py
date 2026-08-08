"""A strict XML reader for documents that carry a signature.

Wreath owns this because the alternatives are not available to it: `defusedxml`
and `xmlsec` are third-party runtime dependencies, which `src/wreath` does not
take, and the stdlib has no **exclusive** canonicalization -- `ElementTree`
ships C14N 2.0, a different algorithm from the Exclusive XML Canonicalization
1.0 that XML Signature actually requires.

## This is a profile, not an XML parser

It refuses far more than it accepts, and the refusals *are* the feature:

**`<!DOCTYPE`, in any form.** The only way to declare an entity, so the only
route to an expansion bomb or an external resolver. Removing it removes XXE and
the billion-laughs family outright.

**Entity references beyond the five predefined.** Nothing else is declarable,
so anything else is a caller expecting an expansion that will not happen.

**Comments.** A comment splits a text node. Two readings of one value is how a
`<NameID>` gets truncated and a login lands as the wrong person.

**CDATA sections.** A second spelling of text, and one value with two spellings
is an ambiguity a signature cannot settle.

**Processing instructions.** No processor exists for them here, so they can only
be a channel to one that does.

**A byte order mark.** The bytes a signature covers must be the bytes that were
parsed.

**Any encoding but UTF-8, and malformed, overlong or surrogate UTF-8.** One byte
sequence, one meaning: declared-versus-actual encoding mismatch is a whole
family of confusion attacks.

**Unbounded depth, size, element and attribute counts.** An unauthenticated
boundary reads whatever it is given.

Everything it accepts, it accepts the same way twice: the C parser and the
pure-Python twin are held byte for byte to each other over the whole corpus,
including every exploit, by `tests/test_xml_parity.py`.

## Byte provenance is the point

Every element records the byte range it was parsed from, and canonicalization
re-reads *those bytes* rather than re-serializing the tree:

```python
from wreath.xml import parse

doc = parse(raw)
assertion = doc.find_id("_a1")        # refuses if two elements claim "_a1"
signed = doc.canonicalize(assertion)  # exclusive c14n of the original bytes
subject = assertion.children[0].children[0].text
```

A verifier canonicalizes `assertion` and a consumer reads values out of that
same element. There is no second lookup to divert, so XML Signature Wrapping --
the SAML vulnerability class, where the verifier checks one subtree and the
consumer reads another -- has nowhere to happen. A repeated identifier is a
refusal rather than a resolution order, which closes the other half.

## What this is not

It does not verify signatures, and it knows nothing about SAML. It produces the
canonical bytes a signature is computed over and the provenance that makes the
result meaningful; the crypto is `wreath._auth`'s and the assertion semantics
belong to the layer above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._native import _core as _core_module
from ._native import extension as _extension
from ._pure import xml as _reference
from ._pure.xml import (
    ID_ATTRIBUTES,
    MAX_DEPTH_CEILING,
    XML_NAMESPACE,
    XMLNS_NAMESPACE,
    Document,
    Element,
    Limits,
    XMLRefusal,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BACKEND",
    "ID_ATTRIBUTES",
    "MAX_DEPTH_CEILING",
    "XMLNS_NAMESPACE",
    "XML_NAMESPACE",
    "Document",
    "Element",
    "Limits",
    "XMLRefusal",
    "canonicalize_span",
    "parse",
]

# `WREATH_PURE=1` is not re-read here: `wreath._native` is the one place that
# gate lives, and it hands back `_core is None` in that mode. The `hasattr` is
# the *other* question -- a build compiled without `xml.c`.
_native: Any = (
    _core_module if _core_module is not None and hasattr(_core_module, "xml_parse") else None
)

#: Which implementation this process resolved to. Read by the parity suite,
#: which would otherwise compare the pure twin against itself and pass while
#: proving nothing.
BACKEND = "native" if _native is not None else "pure"


def _require_native(symbol: str) -> Any:
    """The compiled module, or `RuntimeError` if this build has no C parser.

    `ignore_pure` is what makes `WREATH_PURE=1` irrelevant here: the parity
    suite has to reach the C parser in the very mode that hides it, or it would
    compare the pure twin against itself and pass while proving nothing. A build
    compiled without `xml.c` still fails, which is the case the error message is
    about.
    """
    module = _extension("_core", ignore_pure=True)
    if module is None or not hasattr(module, symbol):
        raise RuntimeError("the C XML parser is not available in this build")
    return module


if _native is not None:  # pragma: no branch - both arms are covered by the suite
    _core_module.xml_configure(XMLRefusal)


def _limit_tuple(limits: Limits) -> tuple[int, int, int, int, int]:
    return (
        limits.max_bytes,
        limits.max_depth,
        limits.max_elements,
        limits.max_attributes,
        limits.max_attribute_bytes,
    )


def _build(node: tuple[Any, ...], inherited: tuple[tuple[str, str], ...]) -> Element:
    """Turn one C-built node tuple into an `Element`.

    The C side returns plain tuples rather than constructing Python objects,
    so the dataclass definition -- and therefore the shape both backends
    produce -- lives in exactly one place.
    """
    tag, attrib, text, tail, span, nsdecl, qualified, prefix, local, children = node
    scope = dict(inherited)
    for declared_prefix, uri in nsdecl:
        if uri:
            scope[declared_prefix] = uri
        else:
            scope.pop(declared_prefix, None)
    nsscope = inherited if not nsdecl else tuple(sorted(scope.items()))
    return Element(
        tag=tag,
        attrib=attrib,
        text=text,
        tail=tail,
        children=tuple(_build(child, nsscope) for child in children),
        span=span,
        nsdeclarations=nsdecl,
        nsscope=nsscope,
        nsinherited=inherited,
        qualified=qualified,
        prefix=prefix,
        local=local,
    )


def _parse_native(data: bytes, limits: Limits | None = None) -> Document:
    """Parse through the C backend regardless of what `BACKEND` resolved to.

    Only the parity and fuzz suites call this; ordinary callers want
    `parse`, which honours `WREATH_PURE`.
    """
    core = _require_native("xml_parse")
    core.xml_configure(XMLRefusal)
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("XML input must be bytes")
    payload = bytes(data)
    root = core.xml_parse(payload, *_limit_tuple(limits or Limits()))
    return Document(
        root=_build(root, ()), source=payload, canonicalizer=_canonicalize_native
    )


def _canonicalize_native(
    data: bytes,
    start: int,
    end: int,
    inherited: Sequence[tuple[str, str]] = (),
    inclusive_prefixes: Sequence[str] = (),
    limits: Limits | None = None,
) -> bytes:
    """Canonicalize through the C backend regardless of `BACKEND`.

    The counterpart of `_parse_native`, and the canonicalizer a document it
    built carries: a natively parsed document that canonicalized through the
    pure twin would let the parity suite compare that twin against itself.
    """
    core = _require_native("xml_c14n")
    if not 0 <= start < end <= len(data):
        raise ValueError("span does not address the source")
    return core.xml_c14n(
        bytes(data),
        start,
        end,
        tuple(inherited),
        tuple(inclusive_prefixes),
        *_limit_tuple(limits or Limits()),
    )


def parse(data: bytes, limits: Limits | None = None) -> Document:
    """Parse `data` under `limits`, or raise `XMLRefusal`.

    `limits` defaults to `Limits`, whose bounds suit a SAML assertion
    or an S3 response. There is no unbounded setting.
    """
    if _native is None:
        return _reference.parse_document(data, limits)
    return _parse_native(data, limits)


def canonicalize_span(
    data: bytes,
    start: int,
    end: int,
    inherited: Sequence[tuple[str, str]] = (),
    inclusive_prefixes: Sequence[str] = (),
    limits: Limits | None = None,
) -> bytes:
    """Exclusive c14n of `data[start:end]`, seeded with `inherited`.

    `Document.canonicalize` is the ordinary entry point; this is the one
    underneath it, exposed for a caller that has bytes and a span from
    somewhere else.
    """
    if _native is None:
        return _reference.canonicalize_span(
            data, start, end, inherited, inclusive_prefixes, limits
        )
    return _canonicalize_native(data, start, end, inherited, inclusive_prefixes, limits)
