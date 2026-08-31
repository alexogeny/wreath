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

Everything it accepts, it accepts the same way every time: `tests/test_xml_parse.py`,
`test_xml_refusals.py`, `test_xml_c14n.py` and `test_xml_wrapping.py` hold the
parser to the whole corpus, including every exploit, and `test_xml_fuzz.py`
drives it with input nobody designed.

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

from typing import TYPE_CHECKING

from ._native import _core

# The tree, the bounds and the refusal are one definition, shared with the C
# parser rather than restated by it. See `wreath._xml_model`.
from ._xml_model import (
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

# The refusal type, handed to the parser once so a caller catches the class
# declared in `wreath._xml_model` rather than one C invented.
_core.xml_configure(XMLRefusal)


def _limit_tuple(limits: Limits) -> tuple[int, int, int, int, int]:
    return (
        limits.max_bytes,
        limits.max_depth,
        limits.max_elements,
        limits.max_attributes,
        limits.max_attribute_bytes,
    )


def _parse_native(data: bytes, limits: Limits | None = None) -> Document:
    """Parse `data` under `limits`, or raise `XMLRefusal`.

    `parse` is the public spelling; this is what it calls.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("XML input must be bytes")
    payload = bytes(data)
    root = _core.xml_parse(payload, Element, *_limit_tuple(limits or Limits()))
    return Document(root=root, source=payload, canonicalizer=_canonicalize_native)


def _canonicalize_native(
    data: bytes,
    start: int,
    end: int,
    inherited: Sequence[tuple[str, str]] = (),
    inclusive_prefixes: Sequence[str] = (),
    limits: Limits | None = None,
) -> bytes:
    """The counterpart of `_parse_native`, and the canonicalizer it attaches."""
    if not 0 <= start < end <= len(data):
        raise ValueError("span does not address the source")
    return _core.xml_c14n(
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
    return _canonicalize_native(data, start, end, inherited, inclusive_prefixes, limits)
