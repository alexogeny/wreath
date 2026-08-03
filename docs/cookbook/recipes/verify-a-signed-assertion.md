# Read a value out of a signed XML assertion

**Problem.** A document arrives with a signature over one subtree, and you need
the values inside that subtree — without the classic mistake of verifying one
element and then reading another.

**Solution.** Resolve the subtree once, canonicalize *it*, and read from the same
handle you verified.

```python
from wreath.xml import Limits, XMLRefusal, parse

LIMITS = Limits(max_bytes=512 * 1024, max_depth=40)


def read_subject(raw: bytes, reference_id: str, verify) -> str:
    doc = parse(raw, LIMITS)

    # One lookup. Two elements claiming this ID is a refusal, not a tie broken
    # by document order.
    assertion = doc.find_id(reference_id)
    if assertion is None:
        raise ValueError(f"no element carries ID {reference_id!r}")

    # Canonical bytes come from re-reading the subtree's own source bytes, so
    # what `verify` checks and what the next lines read are the same tree.
    if not verify(doc.canonicalize(assertion)):
        raise ValueError("signature does not verify")

    subject = assertion.children[0].children[0]
    return subject.text
```

The signature's `InclusiveNamespaces` element, when present, names prefixes the
canonical form must carry even where the subtree does not use them. `#default`
names the default namespace:

```python
canonical = doc.canonicalize(assertion, inclusive_prefixes=("ds", "#default"))
```

## Handle the refusal as a refusal

The parser rejects a large amount of ordinary XML on purpose. Log what it
refused rather than retrying with something more permissive — every entry in
that list is an attack the profile removes:

```python
try:
    doc = parse(raw, LIMITS)
except XMLRefusal as refusal:
    log.warning("refused %s at byte %d", refusal.reason, refusal.offset)
    raise
```

`reason` is stable and machine-readable: `doctype`, `comment`, `cdata`,
`entity-reference`, `encoding`, `depth`, `duplicate-id` and the rest.

## What this recipe deliberately does not do

It does not verify the signature. `verify` above is yours — the crypto lives in
`wreath._auth`, and `wreath.xml` produces the canonical bytes it operates on plus
the provenance that makes the answer mean something.

**See also:** [Reading signed XML](../../guides/signed-xml.md) for why the profile
refuses comments, and how byte provenance makes signature wrapping
unexpressible rather than merely checked for.
