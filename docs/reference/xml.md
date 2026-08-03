# `wreath.xml`

A strict XML reader for documents that carry a signature, and the exclusive
canonicalization a signature is computed over.

This exists because nothing else was available. `defusedxml` and `xmlsec` are
third-party runtime dependencies, which `src/wreath` does not take; and the
stdlib has no *exclusive* canonicalization — `ElementTree` ships C14N 2.0, a
different algorithm from the Exclusive XML Canonicalization 1.0 that XML
Signature actually requires.

It refuses far more than it accepts, and the refusals are the feature. It does
not verify signatures and it knows nothing about SAML: it produces the canonical
bytes and the byte provenance that make a verification meaningful, and the
layers above own the rest.

For why the profile is shaped this way, and for the wrapping attacks it is built
to make unexpressible, read the [Reading signed XML](../guides/signed-xml.md)
guide first.

::: wreath.xml
