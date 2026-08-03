# `wreath.saml`

Verify a SAML 2.0 assertion, and turn what it proves into facts.

An enterprise identity provider signs an XML assertion saying "this person
authenticated, here is who they are". This module checks that signature against
keys you configured, checks the assertion is addressed to you and is live right
now, spends its identifier so it cannot be presented twice, and hands back the
values it carries.

**It decides nothing.** A verified assertion is a `VerifiedAssertion`, and
`VerifiedAssertion.facts()` is a mapping for Cedar context — whether the person
it names may do what they asked is a policy question, answered by the policy
set. That is the same shape [`wreath.signatures`](signatures.md) establishes:
*verified* means the identity provider said this, not that it is trusted.

It is built on [`wreath.xml`](xml.md), which is why it needs no third-party
XML-DSig dependency: verification canonicalizes by re-reading the *source bytes*
each element was parsed from, so a verifier and a consumer cannot be pointed at
different subtrees. Signature wrapping is unexpressible rather than checked for.

This is the *service provider* half, and only the receiving end of it: it reads
a `Response` or bare `Assertion` that arrived by a route you own. It does not
act as an identity provider, publish service-provider metadata, decrypt an
`EncryptedAssertion`, or mount the binding endpoints — see
[the roadmap](roadmap.md), which names each of those as absent rather than
implied.

::: wreath.saml
