# Reading signed XML

Some XML arrives with a signature over part of it, and the part that is signed
is the part you are about to trust. A SAML assertion is the common case: an
identity provider signs a subtree, your application checks the signature, and
whoever the assertion names gets logged in.

Almost every serious failure of that flow has the same cause. **The verifier and
the consumer looked at the document separately, and an attacker made them
disagree.**

`wreath.xml` is built so they cannot.

## The attack this is shaped around

XML Signature Wrapping works like this. An attacker takes a genuine, correctly
signed assertion — one they legitimately obtained, perhaps by logging in as
themselves — and builds a new document around it. The signed subtree is still
there, byte for byte, so the signature still verifies. But a *second* assertion
sits beside it, unsigned, naming somebody else.

The verifier looks up the assertion the signature references and checks it:
valid. The consumer then asks the document for "the assertion" and walks to the
first one it finds, which is the forgery. Both components did something
reasonable. The document was built so that "reasonable" meant two different
elements.

The variations are endless — the real assertion hidden inside a `<ds:Object>`,
relocated under an `<Extensions>` wrapper, or simply duplicated with the same
`ID` — and libraries have spent years patching them one at a time.

## Two properties, instead of a list of patches

**Every element remembers the bytes it came from.** Parsing records a byte range
per element, and canonicalization re-reads *those bytes* rather than
re-serializing the tree. So a caller resolves a subtree once, canonicalizes it,
and reads its values from that same handle. There is no second lookup to divert.

```python
from wreath.xml import parse

doc = parse(raw)
assertion = doc.find_id("_a1")          # the ID the signature references
signed_bytes = doc.canonicalize(assertion)
# ... verify signed_bytes against the signature ...
name = assertion.children[0].children[0].text   # read from the verified handle
```

**A repeated identifier is a refusal, not a resolution order.** `find_id` counts
every element carrying the identifier and raises `XMLRefusal` if there is more
than one. A document with two candidates for one `ID` never reaches the point
where document order decides which one you get.

```python
doc = parse(wrapped)
doc.find_id("_a1")
# XMLRefusal: the identifier '_a1' appears 2 times; a document with two
# candidates for one identifier is refused (at byte 214)
```

## Why exclusive canonicalization

A signature is computed over bytes, but XML has many spellings for one document:
attribute order, empty-element syntax, which namespace declarations appear where.
Canonicalization picks one spelling so both ends compute the same bytes.

*Exclusive* canonicalization adds the property a detached signature needs: a
subtree renders only the namespace declarations it **visibly utilizes**. That is
what lets an assertion signed inside one envelope still verify after arriving
inside a different one that happens to declare unrelated namespaces.

```python
first = parse(b'<r xmlns:p="urn:p"><p:a ID="x">v</p:a></r>')
second = parse(
    b'<other xmlns:p="urn:p" xmlns:extra="urn:e"><wrap><p:a ID="x">v</p:a></wrap></other>'
)
assert first.canonicalize(first.find_id("x")) == second.canonicalize(second.find_id("x"))
```

When a signature's `InclusiveNamespaces` element names prefixes that must be
carried anyway, pass them through — `#default` names the default namespace:

```python
doc.canonicalize(assertion, inclusive_prefixes=("q", "#default"))
```

## The profile refuses ordinary XML

This is deliberate, and it will reject documents other parsers accept.

`<!DOCTYPE` is refused in every form. It is the only way to declare an entity,
so removing it removes XXE, billion laughs and quadratic blowup as a class
rather than as three checks. Entity references outside the five predefined ones
are refused for the same reason: nothing is declarable, so nothing else can mean
anything.

**Comments are refused**, which surprises people. A comment splits a text node,
and `<NameID>admin@corp.example<!---->.attacker.example</NameID>` reads as one
value to a parser that concatenates around it and another to a parser that stops
at the first text node. That ambiguity has logged people in as the wrong person.
Refusing comments means the two readings cannot differ, because the document does
not parse. CDATA is refused on the same reasoning — it is a second spelling of
text.

Also refused: processing instructions, a byte order mark, any declared encoding
but UTF-8, malformed or overlong or surrogate UTF-8, control characters, a
prefixed attribute whose prefix is unbound, and two attributes that expand to one
name. Bounds on document size, nesting depth, element count and attribute count
are always in force; there is no unbounded setting, because a parser on an
unauthenticated boundary reads whatever it is given.

```python
from wreath.xml import Limits, parse

parse(raw, Limits(max_bytes=256 * 1024, max_depth=40))
```

Every refusal carries a stable `reason` code and the byte `offset` it stopped at:

```python
try:
    parse(raw)
except XMLRefusal as refusal:
    log.warning("refused %s at %d", refusal.reason, refusal.offset)
```

## Reading the tree

`tag` and the keys of `attrib` use the `{uri}local` spelling
`xml.etree.ElementTree` uses, so values read the same as they would from the
stdlib. Elements iterate over their children, and `text` and `tail` follow the
same convention.

```python
doc = parse(b'<r xmlns="urn:d"><c a="1">body</c></r>')
doc.root.tag                      # '{urn:d}r'
child = doc.root.children[0]
child.tag, child.attrib, child.text   # ('{urn:d}c', {'a': '1'}, 'body')
child.span                        # (17, 34) -- the bytes it was parsed from
doc.subtree_bytes(child)          # b'<c a="1">body</c>'
```

## Two implementations, held to each other

A C parser runs in an ordinary build and a pure-Python twin runs under
`WREATH_PURE=1`. Two implementations of one parser would normally be a liability
here — a verifier on one and a consumer on the other is the same disagreement in
a new place — so they are driven over the whole corpus, every exploit included,
and must agree on the tree, the byte spans, the canonical bytes, and the reason
for every refusal. `wreath.xml.BACKEND` names the one in force.
