"""Verify a SAML 2.0 assertion, and turn what it proves into facts.

An enterprise identity provider signs an XML assertion saying "this person
authenticated, here is who they are, and here is what I know about them". This
module checks that signature against keys you configured, checks the assertion
is addressed to you and is live right now, spends its identifier so it cannot be
presented twice, and hands back the values it carries.

**It decides nothing.** A verified assertion is a `VerifiedAssertion`, and
`VerifiedAssertion.facts()` is a mapping for Cedar context. Whether the person it
names may do the thing they asked for is a policy question, and the policy set
answers it — exactly as `wreath.signatures` establishes for a signed request.
"Verified" means the identity provider said this; it does not mean "trusted".

## What this is, and what it is not

This is the *service provider* half, and only the receiving end of it. It reads
a `Response` or a bare `Assertion` that arrived by some route you own — the POST
body of your assertion consumer endpoint, most often. It does not act as an
identity provider, publish service-provider metadata, decrypt an
`EncryptedAssertion`, or mount the redirect/POST binding endpoints; see
`docs/reference/roadmap.md`, which names each of those as absent rather than
implied.

## The three properties it is built on

**Transforms are an allow-list of exactly two.** A `Reference` may declare
enveloped-signature and exclusive canonicalization, in that order, and nothing
else. An unknown transform is a refusal naming the algorithm, never a skip: a
permissive transform list is a signature-wrapping vector in its own right,
because a transform the verifier ignores is a transform that changed what the
digest covers.

**Verification runs over source bytes.** `wreath.xml` records the byte range
every element was parsed from and canonicalizes by re-reading *those bytes*, so
the subtree the digest covers and the subtree the values are read from are the
same handle. The reference is resolved once, by `Document.find_id`, which
refuses a repeated identifier rather than picking one; and the element it
resolves to must be the very object this module then reads. There is no second
lookup for an attacker to divert.

**An assertion identifier is spent once.** Replay protection is a `claim` on a
`wreath.store` store — `MemoryStore` in one worker, `PostgresStore` across
several — which is one atomic insert-or-reclaim rather than a read followed by a
write. There is no ledger in this module.

## Keys are configured, never fetched

`IdentityProvider` takes the certificates your deployment obtained from the
identity provider out of band, and parses them once, at construction. Nothing
here reads `KeyInfo` to decide which key to use and nothing here opens a socket.
A signature under a key you did not configure is simply unverified. That rule is
`wreath.signatures`': resolving a key by fetching lets an unauthenticated caller
aim an outbound request from your network, and the assertion has not been
verified yet at the moment the fetch would happen.

```python
from wreath.saml import IdentityProvider, ServiceProvider, verify_response
from wreath.store import MemoryStore

IDP = IdentityProvider(entity_id="https://idp.example/metadata", certificates=[CERT_PEM])
SP = ServiceProvider(entity_id="https://app.example/saml", acs_url="https://app.example/acs")
SEEN = MemoryStore(ttl=600.0)

assertion = await verify_response(body, idp=IDP, sp=SP, ledger=SEEN)
context = assertion.facts()   # for a Cedar policy to read; not a decision
```
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from ._auth._ecverify import on_p256_curve, verify_es256

# The RSA half of this is `wreath._auth.jwt`'s, deliberately: it already parses a
# DER public key without a third-party dependency and already implements
# RSASSA-PKCS1-v1_5 verification against a minimum modulus size. A second
# spelling of either beside it is how the two drift apart.
from ._auth.jwt import (
    MIN_RSA_MODULUS_BITS,
    RsaPublicKey,
    _der_read_tlv,
    _verify_rs,
)
from .xml import Document, Element, Limits, XMLRefusal, canonicalize_span, parse

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ALLOWED_TRANSFORMS",
    "LIMITS",
    "IdentityProvider",
    "ReplayLedger",
    "SamlRefusal",
    "ServiceProvider",
    "VerifiedAssertion",
    "ledger_declaration",
    "verify_response",
]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_PROTOCOL: Final = "urn:oasis:names:tc:SAML:2.0:protocol"
_ASSERTION: Final = "urn:oasis:names:tc:SAML:2.0:assertion"
_DS: Final = "http://www.w3.org/2000/09/xmldsig#"
_EXC_C14N: Final = "http://www.w3.org/2001/10/xml-exc-c14n#"

_STATUS_SUCCESS: Final = "urn:oasis:names:tc:SAML:2.0:status:Success"
_BEARER: Final = "urn:oasis:names:tc:SAML:2.0:cm:bearer"

#: The two transforms a `Reference` may declare, and the only two. Exclusive
#: canonicalization is the algorithm the digest is computed over; the enveloped
#: signature transform removes the `Signature` from the element that contains
#: it. Everything else -- XPath, XSLT, base64, inclusive c14n, the
#: `WithComments` variants -- is refused by name.
ALLOWED_TRANSFORMS: Final = (
    "http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    _EXC_C14N,
)

_ENVELOPED: Final = ALLOWED_TRANSFORMS[0]

#: Digest algorithms, by `DigestMethod/@Algorithm`. SHA-1 is absent on purpose
#: and is named in the refusal when it appears, because a collision on the
#: digest of a `Reference` is a forged assertion.
_DIGESTS: Final = {
    "http://www.w3.org/2001/04/xmlenc#sha256": "sha256",
    "http://www.w3.org/2001/04/xmldsig-more#sha384": "sha384",
    "http://www.w3.org/2001/04/xmlenc#sha512": "sha512",
}

#: Signature algorithms, by `SignatureMethod/@Algorithm`, each carrying the key
#: family it requires. The family is checked against the configured key so an
#: RSA signature cannot be presented for verification against an EC key or the
#: other way round.
_SIGNATURES: Final = {
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256": ("RSA", "sha256"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384": ("RSA", "sha384"),
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512": ("RSA", "sha512"),
    "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256": ("EC", "sha256"),
}

#: `Conditions` children this profile understands. SAML says an assertion
#: carrying a condition the consumer cannot evaluate is *indeterminate*, which
#: is a refusal here: a validity constraint nobody read is not a constraint.
#: `OneTimeUse` needs no handling because every assertion is single-use, and
#: `ProxyRestriction` bounds onward issuance, which this module never does.
_KNOWN_CONDITIONS: Final = frozenset({
    f"{{{_ASSERTION}}}AudienceRestriction",
    f"{{{_ASSERTION}}}OneTimeUse",
    f"{{{_ASSERTION}}}ProxyRestriction",
})

#: Bounds for a SAML payload. There is no unbounded setting, here or below it.
LIMITS: Final = Limits(max_bytes=512 * 1024, max_depth=40)

#: The widest clock skew this module will accept as a configuration. Five
#: minutes is what every identity provider's own documentation suggests; more is
#: not "tolerant", it is an expired assertion staying usable for longer.
MAX_CLOCK_SKEW: Final = 300.0

#: The longest `Conditions` window an assertion may declare. It is bounded
#: because the replay ledger's TTL is what makes single use *stay* true: an
#: assertion valid for longer than the ledger remembers it is replayable the
#: moment the entry ages out.
MAX_LIFETIME: Final = 3600.0


class SamlRefusal(ValueError):
    """A SAML payload was refused, with a stable `reason` code.

    Every refusal in this module carries one. The message says which check
    failed and what it saw; the code is what a log filter or a metric groups on,
    and it does not change when the prose does.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _refuse(reason: str, message: str) -> SamlRefusal:
    return SamlRefusal(reason, message)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PublicKey:
    """One configured verification key: RSA modulus/exponent, or a P-256 point."""

    family: str
    rsa: RsaPublicKey | None = None
    point: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class IdentityProvider:
    """The directory whose assertions this service provider accepts.

    `certificates` are the signing certificates obtained out of band — from the
    identity provider's metadata document, downloaded and reviewed by a person,
    not by this process. Give it more than one during a rotation: every
    configured key is tried, so the old and new certificates can both be live
    while the identity provider switches over.

    Each is a PEM `CERTIFICATE` block, a PEM `PUBLIC KEY` block, or the bare
    base64 DER that a `<ds:X509Certificate>` element holds. They are parsed
    here, at construction, so a malformed certificate fails when the application
    is described rather than on the first login.
    """

    entity_id: str
    certificates: tuple[str, ...]
    #: Parsed once, at construction. Not part of equality: two providers
    #: configured with the same certificate text are the same provider.
    keys: tuple[_PublicKey, ...] = field(init=False, repr=False, compare=False, default=())

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("an identity provider needs an entity_id to match Issuer against")
        text = tuple(
            value.decode("ascii") if isinstance(value, (bytes, bytearray)) else value
            for value in self.certificates
        )
        if not text:
            raise ValueError(
                "an identity provider needs at least one signing certificate; a key "
                "resolved on the request path is a fetch, which this module does not do"
            )
        object.__setattr__(self, "certificates", text)
        object.__setattr__(self, "keys", tuple(_key_from_text(value) for value in text))


@dataclass(frozen=True, slots=True)
class ServiceProvider:
    """This application, as the assertion has to address it.

    `entity_id` is what an `AudienceRestriction` must name and `acs_url` is what
    a bearer `SubjectConfirmationData/@Recipient` must equal. Both are required:
    an assertion minted for a different service provider verifying here is the
    whole reason `AudienceRestriction` exists.
    """

    entity_id: str
    acs_url: str
    #: Seconds of tolerance applied to `NotBefore` and `NotOnOrAfter`, both
    #: ends. Bounded by `MAX_CLOCK_SKEW`.
    clock_skew: float = 60.0

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("a service provider needs an entity_id for AudienceRestriction")
        if not self.acs_url:
            raise ValueError("a service provider needs an acs_url for SubjectConfirmationData")
        if not 0.0 <= self.clock_skew <= MAX_CLOCK_SKEW:
            raise ValueError(
                f"clock_skew must be between 0 and {MAX_CLOCK_SKEW} seconds, "
                f"not {self.clock_skew}"
            )


@runtime_checkable
class ReplayLedger(Protocol):
    """Where an assertion identifier is spent, exactly once.

    This is `wreath.store`'s `claim` and nothing more: `MemoryStore(ttl=...)`
    and `PostgresStore(database, ledger_declaration())` both satisfy it as they
    are. `MemoryStore.claim` is synchronous and `PostgresStore.claim` is a
    coroutine, so a result that is awaitable is awaited.

    Give the store a TTL at least as long as `MAX_LIFETIME` plus your clock
    skew. The ledger is what makes single use true, and an entry that ages out
    before the assertion expires reopens exactly the window it closed.
    """

    def claim(self, key: str) -> Any:
        """True when this caller took `key`, False when it was already held."""
        ...


def ledger_declaration(
    *, ttl: float, table: str = "wreath_saml_seen", prefix: str = "wreath_saml"
) -> Any:
    """The `Keyed` declaration behind a shared replay ledger.

    `claim=True`: the generated statement is one insert-or-reclaim, which is the
    whole point — a read followed by a write lets two workers both conclude they
    were first, and two workers both accepting one assertion is the replay this
    exists to prevent. There are no payload columns, because what is stored *is*
    the fact that the identifier was seen.

    `ttl` is required rather than defaulted: how long an identifier must be
    remembered is a function of the identity provider's assertion lifetime, and
    a guess here silently reopens the window this closes. `MAX_LIFETIME` plus
    your clock skew is the smallest safe answer.
    """
    from .store import Keyed

    return Keyed(
        table=table,
        columns=(),
        key="assertion",
        stamp="expires",
        deadline=True,
        ttl=ttl,
        index_stamp=True,
        claim=True,
        prefix=prefix,
    )


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedAssertion:
    """What a verified assertion says. Facts, not a decision.

    Every field here is the identity provider's claim, checked to have been
    signed by a key you configured and addressed to you, within its validity
    window, and not seen before. None of that makes it an authorization: pass
    `facts()` into Cedar context and let a policy decide what the values mean.
    """

    #: The `ID` attribute, which is what was spent in the replay ledger.
    assertion_id: str
    issuer: str
    name_id: str
    name_id_format: str
    #: `AuthnStatement/@SessionIndex`, which a single logout request would name.
    session_index: str
    #: The `AuthnContextClassRef` the directory asserted, e.g. the password or
    #: multi-factor class. A policy that requires a strong factor reads this.
    authn_context: str
    authn_instant: datetime
    not_before: datetime
    not_on_or_after: datetime
    #: Attribute values by `Name`, in document order. Always a tuple, including
    #: for a single-valued attribute: a directory that starts sending two group
    #: memberships must not change the shape of what a policy reads.
    attributes: Mapping[str, tuple[str, ...]]

    def facts(self) -> dict[str, Any]:
        """This assertion as a mapping for Cedar context.

        Flat, JSON-shaped, and named the way the rest of Wreath names facts.
        It is deliberately not a `Principal` and deliberately not a session:
        turning an assertion into either is the application's decision, and one
        this module must not make on its behalf.
        """
        return {
            "saml_issuer": self.issuer,
            "saml_name_id": self.name_id,
            "saml_name_id_format": self.name_id_format,
            "saml_session_index": self.session_index,
            "saml_authn_context": self.authn_context,
            "saml_attributes": {name: list(values) for name, values in self.attributes.items()},
        }


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------

# OID encodings, as they appear inside an AlgorithmIdentifier's OBJECT
# IDENTIFIER value. Comparing the encoded bytes rather than decoding to a dotted
# string keeps the DER reader to the one function borrowed from `_auth.jwt`.
_OID_RSA: Final = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1
_OID_EC: Final = bytes.fromhex("2a8648ce3d0201")  # 1.2.840.10045.2.1
_OID_P256: Final = bytes.fromhex("2a8648ce3d030107")  # 1.2.840.10045.3.1.7


def _key_from_text(value: str) -> _PublicKey:
    """One configured certificate or public key, as verification material."""
    body: list[str] = []
    labelled = False
    is_certificate = False
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("-----BEGIN"):
            labelled = True
            is_certificate = "CERTIFICATE" in line
            continue
        if line.startswith("-----END") or not line:
            continue
        body.append(line)
    if not body:
        raise ValueError("a configured SAML key is empty")
    try:
        der = base64.b64decode("".join(body), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("a configured SAML key is not base64") from error
    # A bare `<ds:X509Certificate>` body carries no label, and it is always a
    # certificate rather than a bare key. So an unlabelled block is read as one.
    spki = _spki_from_certificate(der) if (is_certificate or not labelled) else der
    return _key_from_spki(spki)


def _spki_from_certificate(der: bytes) -> bytes:
    """The SubjectPublicKeyInfo out of an X.509 certificate.

    `Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm,
    signatureValue }`, and inside `tbsCertificate` the SPKI is the seventh field
    — after an optional `[0] EXPLICIT version`, then serialNumber, signature,
    issuer, validity and subject. Nothing else in the certificate is read: this
    module does not build a chain, check a validity period or evaluate a name.
    The certificate is a container for a key an operator already decided to
    trust, and pretending otherwise would be a trust decision made by parsing.
    """
    try:
        _, certificate, _ = _der_read_tlv(der, 0)
        _, tbs, _ = _der_read_tlv(certificate, 0)
        position = 0
        tag, _, after = _der_read_tlv(tbs, position)
        if tag == 0xA0:  # [0] EXPLICIT version
            position = after
        for _ in range(5):  # serialNumber, signature, issuer, validity, subject
            _, _, position = _der_read_tlv(tbs, position)
        _, _, end = _der_read_tlv(tbs, position)
    except (IndexError, ValueError) as error:
        message = "a configured SAML certificate is not a readable X.509 structure"
        raise ValueError(message) from error
    # `_der_read_tlv` hands back the TLV's *value*; the SPKI parser below wants
    # the whole TLV, which is exactly the slice the walk just consumed.
    return tbs[position:end]


def _key_from_spki(spki: bytes) -> _PublicKey:
    """An RSA or P-256 public key out of a SubjectPublicKeyInfo."""
    try:
        _, sequence, _ = _der_read_tlv(spki, 0)
        _, algorithm, after_algorithm = _der_read_tlv(sequence, 0)
        _, oid, after_oid = _der_read_tlv(algorithm, 0)
        tag, bitstring, _ = _der_read_tlv(sequence, after_algorithm)
    except (IndexError, ValueError) as error:
        raise ValueError("a configured SAML key is not a readable SubjectPublicKeyInfo") from error
    if tag != 0x03:
        raise ValueError("a configured SAML key has no subjectPublicKey bit string")
    key_bytes = bitstring[1:]  # the leading byte counts unused bits, always 0 here
    if oid == _OID_RSA:
        _, rsa_sequence, _ = _der_read_tlv(key_bytes, 0)
        tag_n, n_bytes, after_n = _der_read_tlv(rsa_sequence, 0)
        tag_e, e_bytes, _ = _der_read_tlv(rsa_sequence, after_n)
        if tag_n != 0x02 or tag_e != 0x02:
            raise ValueError("an RSA public key expects two INTEGERs")
        n = int.from_bytes(n_bytes, "big")
        e = int.from_bytes(e_bytes, "big")
        if n.bit_length() < MIN_RSA_MODULUS_BITS:
            raise ValueError(
                f"a configured SAML RSA key is {n.bit_length()} bits; at least "
                f"{MIN_RSA_MODULUS_BITS} are required"
            )
        return _PublicKey("RSA", rsa=RsaPublicKey(n, e))
    if oid == _OID_EC:
        _, curve, _ = _der_read_tlv(algorithm, after_oid)
        if curve != _OID_P256:
            raise ValueError("a configured SAML EC key is not on P-256, which is the only curve")
        if len(key_bytes) != 65 or key_bytes[0] != 0x04:
            raise ValueError("a configured SAML EC key is not an uncompressed P-256 point")
        x = int.from_bytes(key_bytes[1:33], "big")
        y = int.from_bytes(key_bytes[33:], "big")
        if not on_p256_curve(x, y):
            raise ValueError("a configured SAML EC key is not a point on the P-256 curve")
        return _PublicKey("EC", point=(x, y))
    raise ValueError("a configured SAML key is neither RSA nor EC")


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------


def _children(element: Element, namespace: str, local: str) -> list[Element]:
    tag = f"{{{namespace}}}{local}"
    return [child for child in element.children if child.tag == tag]


def _only(element: Element, namespace: str, local: str, *, reason: str, what: str) -> Element:
    found = _children(element, namespace, local)
    if not found:
        raise _refuse(reason, f"{what} carries no <{local}>")
    if len(found) > 1:
        raise _refuse(reason, f"{what} carries {len(found)} <{local}> elements, which is ambiguous")
    return found[0]


def _attribute(element: Element, name: str, *, reason: str, what: str) -> str:
    value = element.attrib.get(name)
    if not value:
        raise _refuse(reason, f"{what} has no {name} attribute")
    return value


def _descendants(element: Element) -> list[Element]:
    out: list[Element] = []
    stack = list(element.children)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _contains(ancestor: Element, node: Element) -> bool:
    """Whether `node` is `ancestor` or sits inside it, by byte span.

    Containment is asked of the *spans* rather than of the tree, because the
    spans are what the digest is computed over and a walk could in principle be
    made to disagree with them. They cannot overlap partially: the parser
    produces properly nested ranges.
    """
    outer, outer_end = ancestor.span
    inner, inner_end = node.span
    return outer <= inner and inner_end <= outer_end


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _b64_bytes(text: str, *, reason: str, what: str) -> bytes:
    """Strict base64, with the whitespace an XML element legitimately carries."""
    compact = "".join(text.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _refuse(reason, f"{what} is not base64") from error


def _transform_chain(reference: Element) -> tuple[bool, tuple[str, ...]]:
    """The declared transforms, refused unless they are the two allowed ones.

    Returns whether the enveloped-signature transform was declared and the
    `InclusiveNamespaces` prefix list belonging to the exclusive
    canonicalization transform.
    """
    holders = _children(reference, _DS, "Transforms")
    if len(holders) != 1:
        raise _refuse(
            "transforms-missing",
            "a Reference must declare exactly one <Transforms>; a reference whose "
            "transform chain is implied is a reference whose digest input is not stated",
        )
    declared = _children(holders[0], _DS, "Transform")
    if not declared:
        raise _refuse(
            "transforms-empty",
            "a Reference declares an empty transform chain, so no canonicalization "
            "is stated for its digest input",
        )
    enveloped = False
    canonicalized = False
    prefixes: tuple[str, ...] = ()
    for transform in declared:
        algorithm = _attribute(
            transform, "Algorithm", reason="transform-unnamed", what="a <Transform>"
        )
        if algorithm not in ALLOWED_TRANSFORMS:
            raise _refuse(
                "transform-refused",
                f"the transform {algorithm!r} is not one of the two this profile "
                "allows (enveloped-signature and exclusive canonicalization); an "
                "unrecognised transform changes what the digest covers and is "
                "refused rather than skipped",
            )
        if algorithm == _ENVELOPED:
            if enveloped:
                raise _refuse(
                    "transform-repeated",
                    "the enveloped-signature transform is declared twice, and applying "
                    "it twice would remove a second Signature the first did not",
                )
            enveloped = True
            continue
        if canonicalized:
            raise _refuse(
                "transform-repeated",
                "exclusive canonicalization is declared twice in one transform chain, "
                "so which prefix list applies is undecided",
            )
        canonicalized = True
        prefixes = _inclusive_prefixes(transform)
    if not canonicalized:
        raise _refuse(
            "transform-not-exclusive",
            "a Reference's transform chain does not include exclusive canonicalization, "
            "so its digest input is not the byte form this profile computes",
        )
    return enveloped, prefixes


def _inclusive_prefixes(transform: Element) -> tuple[str, ...]:
    holders = [
        child for child in transform.children if child.tag == f"{{{_EXC_C14N}}}InclusiveNamespaces"
    ]
    if not holders:
        return ()
    if len(holders) > 1:
        raise _refuse(
            "prefix-list-ambiguous",
            "one canonicalization transform carries two <InclusiveNamespaces> elements",
        )
    return tuple(holders[0].attrib.get("PrefixList", "").split())


def _canonical_digest_input(
    document: Document,
    target: Element,
    excluded: Element | None,
    prefixes: Sequence[str],
) -> bytes:
    """The bytes a `Reference`'s digest is computed over.

    ## How enveloped-signature composes with span-based canonicalization

    `wreath.xml` canonicalizes by re-reading an element's own source bytes, and
    the enveloped-signature transform says to canonicalize the referenced
    element *without* the `Signature` that sits inside it. Those compose exactly,
    because an element's span is a contiguous byte range and the `Signature`'s
    span is a contiguous range **strictly inside** it — properly nested, never
    overlapping, since that is what the parser produces.

    So the transform is a splice, not a tree edit: take the referenced element's
    bytes and remove the `Signature`'s bytes from the middle of them,
    `source[start:cut_start] + source[cut_end:end]`. What is left is still the
    identity provider's own bytes, in their original order, with one properly
    nested subtree deleted — which is precisely the document the transform
    describes, and it is re-canonicalized through the same `canonicalize_span`
    entry point with the same inherited namespace scope the element already had.
    Nothing is re-serialized at any point, so there is no reconstruction step for
    the digested form and the read form to disagree across. A span ends before
    the element's tail, so removing one deletes the `Signature` and leaves the
    whitespace around it, which is what the transform requires.

    The document's own canonicalizer is used rather than the module-level
    function, so a document parsed by the C backend is canonicalized by the C
    backend on both the ordinary and the spliced path.
    """
    start, end = target.span
    if excluded is None:
        return document.canonicalize(target, prefixes)
    cut_start, cut_end = excluded.span
    if not (start < cut_start and cut_end <= end):
        raise _refuse(
            "enveloped-outside",
            "the enveloped-signature transform was declared for a reference whose "
            "element does not contain the Signature, so there is nothing to remove",
        )
    spliced = document.source[start:cut_start] + document.source[cut_end:end]
    backend = document.canonicalizer or canonicalize_span
    return backend(spliced, 0, len(spliced), target.nsinherited, tuple(prefixes))


def _verify_signature(
    document: Document, signature: Element, covered: Element, idp: IdentityProvider
) -> None:
    """Check `signature` covers exactly `covered`, under a configured key."""
    signed_info = _only(
        signature, _DS, "SignedInfo", reason="signedinfo", what="a <Signature>"
    )
    value_element = _only(
        signature, _DS, "SignatureValue", reason="signaturevalue", what="a <Signature>"
    )

    method = _only(
        signed_info, _DS, "CanonicalizationMethod", reason="c14n-method", what="a <SignedInfo>"
    )
    algorithm = _attribute(
        method, "Algorithm", reason="c14n-method", what="a <CanonicalizationMethod>"
    )
    if algorithm != _EXC_C14N:
        raise _refuse(
            "c14n-refused",
            f"SignedInfo declares the canonicalization {algorithm!r}; this profile "
            "computes exclusive canonicalization 1.0 and nothing else",
        )
    signed_info_prefixes = _inclusive_prefixes(method)

    signature_method = _only(
        signed_info, _DS, "SignatureMethod", reason="signature-method", what="a <SignedInfo>"
    )
    signature_algorithm = _attribute(
        signature_method, "Algorithm", reason="signature-method", what="a <SignatureMethod>"
    )
    if signature_algorithm not in _SIGNATURES:
        raise _refuse(
            "signature-algorithm-refused",
            f"the signature algorithm {signature_algorithm!r} is not accepted; this "
            "profile verifies RSA-SHA256/384/512 and ECDSA-SHA256, and SHA-1 is "
            "excluded because a collision on it is a forged assertion",
        )
    family, signature_hash = _SIGNATURES[signature_algorithm]

    references = _children(signed_info, _DS, "Reference")
    if len(references) != 1:
        raise _refuse(
            "reference-count",
            f"SignedInfo carries {len(references)} <Reference> elements; this profile "
            "signs exactly one element, and a second reference is a way to make the "
            "verified subtree and the read subtree differ",
        )
    reference = references[0]

    uri = reference.attrib.get("URI", "")
    if not uri.startswith("#") or len(uri) < 2:
        raise _refuse(
            "reference-uri",
            f"the Reference URI {uri!r} is not a same-document fragment; an empty or "
            "external reference is refused, because the only element this profile "
            "verifies is one the document already resolved by identifier",
        )
    try:
        resolved = document.find_id(uri[1:])
    except XMLRefusal as refusal:
        raise _refuse("reference-duplicate-id", str(refusal)) from refusal
    if resolved is None:
        raise _refuse(
            "reference-unresolved",
            f"the Reference URI {uri!r} names an identifier no element carries",
        )
    if resolved is not covered:
        raise _refuse(
            "reference-elsewhere",
            f"the Reference URI {uri!r} resolves to a different element from the one "
            "being consumed; the signature covers a subtree that is not the one whose "
            "values would be read",
        )

    enveloped, prefixes = _transform_chain(reference)
    inside = _contains(covered, signature)
    if inside and not enveloped:
        raise _refuse(
            "enveloped-missing",
            "the Signature sits inside the element it references but the "
            "enveloped-signature transform is not declared, so the digest would be "
            "computed over bytes that include the signature itself",
        )

    digest_method = _only(
        reference, _DS, "DigestMethod", reason="digest-method", what="a <Reference>"
    )
    digest_algorithm = _attribute(
        digest_method, "Algorithm", reason="digest-method", what="a <DigestMethod>"
    )
    if digest_algorithm not in _DIGESTS:
        raise _refuse(
            "digest-algorithm-refused",
            f"the digest algorithm {digest_algorithm!r} is not accepted; this profile "
            "computes SHA-256, SHA-384 and SHA-512, and SHA-1 is excluded because a "
            "collision on it is a forged assertion",
        )
    digest_element = _only(
        reference, _DS, "DigestValue", reason="digest-value", what="a <Reference>"
    )
    declared_digest = _b64_bytes(
        digest_element.text, reason="digest-value", what="a <DigestValue>"
    )

    digest_input = _canonical_digest_input(
        document, covered, signature if (enveloped and inside) else None, prefixes
    )
    computed = hashlib.new(_DIGESTS[digest_algorithm], digest_input).digest()
    if not hmac.compare_digest(computed, declared_digest):
        raise _refuse(
            "digest-mismatch",
            "the DigestValue does not match the canonical form of the element the "
            "Reference names, so the signed subtree has been altered",
        )

    signing_input = document.canonicalize(signed_info, signed_info_prefixes)
    raw_signature = _b64_bytes(
        value_element.text, reason="signature-value", what="a <SignatureValue>"
    )
    if not _any_key_verifies(idp, family, signature_hash, signing_input, raw_signature):
        raise _refuse(
            "signature-unverified",
            "no configured signing certificate verifies this SignedInfo; a key this "
            "deployment was not given is an unverified key, never one to go and fetch",
        )


def _any_key_verifies(
    idp: IdentityProvider,
    family: str,
    signature_hash: str,
    signing_input: bytes,
    raw_signature: bytes,
) -> bool:
    verified = False
    for key in idp.keys:
        # The family check is structural anti-confusion, the same rule
        # `_auth.jwt._verify_signature` applies: an ECDSA signature must not be
        # offered to an RSA key, whose verification would read it as an integer.
        if key.family != family:
            continue
        if family == "RSA" and key.rsa is not None:
            verified = _verify_rs(key.rsa, signature_hash, signing_input, raw_signature) or verified
        elif key.point is not None:
            x, y = key.point
            verified = verify_es256(x, y, signing_input, raw_signature) or verified
    return verified


# ---------------------------------------------------------------------------
# Assertion semantics
# ---------------------------------------------------------------------------


def _instant(value: str, *, reason: str, what: str) -> datetime:
    """One `xs:dateTime`, which must carry a timezone.

    A naive timestamp is refused rather than assumed to be UTC. "Assumed UTC" is
    how a validity window silently moves by hours, and the direction it moves is
    whichever one the attacker's identity provider is in.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _refuse(reason, f"{what} is not an xs:dateTime: {value!r}") from error
    if parsed.tzinfo is None:
        raise _refuse(
            reason,
            f"{what} carries no timezone offset ({value!r}); an instant without one "
            "is not an instant",
        )
    return parsed.astimezone(UTC)


def _check_conditions(
    assertion: Element, sp: ServiceProvider, now: datetime
) -> tuple[datetime, datetime]:
    conditions = _only(
        assertion, _ASSERTION, "Conditions", reason="conditions", what="an <Assertion>"
    )
    not_before_text = _attribute(
        conditions, "NotBefore", reason="conditions-window", what="<Conditions>"
    )
    not_after_text = _attribute(
        conditions, "NotOnOrAfter", reason="conditions-window", what="<Conditions>"
    )
    not_before = _instant(not_before_text, reason="conditions-window", what="Conditions/@NotBefore")
    not_after = _instant(
        not_after_text, reason="conditions-window", what="Conditions/@NotOnOrAfter"
    )
    if not_after <= not_before:
        raise _refuse(
            "conditions-inverted",
            "the Conditions window ends at or before it begins, so there is no instant "
            "at which this assertion is valid",
        )
    if (not_after - not_before).total_seconds() > MAX_LIFETIME:
        raise _refuse(
            "conditions-too-long",
            f"the Conditions window is longer than the {MAX_LIFETIME:.0f} seconds this "
            "profile accepts; single use only stays true while the replay ledger still "
            "remembers the identifier",
        )

    skew = sp.clock_skew
    if (now - not_before).total_seconds() < -skew:
        raise _refuse(
            "not-yet-valid",
            f"the assertion is not valid until {not_before.isoformat()}, which is "
            f"further ahead than the {skew:.0f} seconds of clock skew allowed",
        )
    if (now - not_after).total_seconds() >= skew:
        raise _refuse(
            "expired",
            f"the assertion expired at {not_after.isoformat()}, which is further behind "
            f"than the {skew:.0f} seconds of clock skew allowed",
        )

    unknown = [child.tag for child in conditions.children if child.tag not in _KNOWN_CONDITIONS]
    if unknown:
        raise _refuse(
            "condition-unknown",
            f"the assertion carries the condition {unknown[0]!r}, which this profile "
            "cannot evaluate; a validity constraint nobody read is not a constraint",
        )

    restrictions = _children(conditions, _ASSERTION, "AudienceRestriction")
    if not restrictions:
        raise _refuse(
            "audience-absent",
            "the assertion declares no AudienceRestriction, so nothing in it says it "
            "was minted for this service provider rather than another one",
        )
    # Every restriction must be satisfied, and one is satisfied by any of its
    # audiences: SAML makes the outer list a conjunction and the inner a
    # disjunction, and reading it the other way accepts an assertion addressed
    # to somebody else that merely mentions us.
    for restriction in restrictions:
        audiences = [
            child.text.strip() for child in _children(restriction, _ASSERTION, "Audience")
        ]
        if sp.entity_id not in audiences:
            raise _refuse(
                "audience-mismatch",
                f"an AudienceRestriction names {audiences!r}, which does not include "
                f"this service provider ({sp.entity_id!r})",
            )
    return not_before, not_after


def _check_subject(
    assertion: Element, sp: ServiceProvider, now: datetime, in_response_to: str | None
) -> tuple[str, str]:
    subject = _only(assertion, _ASSERTION, "Subject", reason="subject", what="an <Assertion>")
    name_id = _only(subject, _ASSERTION, "NameID", reason="nameid", what="a <Subject>")
    value = name_id.text.strip()
    if not value:
        raise _refuse("nameid-empty", "the assertion's NameID is empty, so it names nobody")

    confirmations = _children(subject, _ASSERTION, "SubjectConfirmation")
    bearer = [c for c in confirmations if c.attrib.get("Method") == _BEARER]
    if not bearer:
        raise _refuse(
            "confirmation-method",
            "the assertion carries no bearer SubjectConfirmation; holder-of-key and "
            "sender-vouches confirmation are not implemented, and an unconfirmed "
            "subject is not a login",
        )
    if len(bearer) > 1:
        raise _refuse(
            "confirmation-ambiguous",
            "the assertion carries more than one bearer SubjectConfirmation, so which "
            "recipient and deadline apply is undecided",
        )
    data = _only(
        bearer[0],
        _ASSERTION,
        "SubjectConfirmationData",
        reason="confirmation-data",
        what="a bearer <SubjectConfirmation>",
    )

    recipient = _attribute(
        data, "Recipient", reason="confirmation-recipient", what="<SubjectConfirmationData>"
    )
    if recipient != sp.acs_url:
        raise _refuse(
            "confirmation-recipient",
            f"the assertion is addressed to the recipient {recipient!r}, not to this "
            f"service provider's assertion consumer ({sp.acs_url!r})",
        )

    deadline_text = _attribute(
        data, "NotOnOrAfter", reason="confirmation-deadline", what="<SubjectConfirmationData>"
    )
    deadline = _instant(
        deadline_text,
        reason="confirmation-deadline",
        what="SubjectConfirmationData/@NotOnOrAfter",
    )
    if (now - deadline).total_seconds() >= sp.clock_skew:
        raise _refuse(
            "confirmation-expired",
            f"the bearer confirmation expired at {deadline.isoformat()}, so this "
            "assertion may no longer be presented even though it is otherwise intact",
        )
    if "NotBefore" in data.attrib:
        raise _refuse(
            "confirmation-not-before",
            "a bearer SubjectConfirmationData must not carry NotBefore, and one that "
            "does is not the bearer profile this module verifies",
        )

    declared = data.attrib.get("InResponseTo")
    if in_response_to is None and declared is not None:
        raise _refuse(
            "unsolicited",
            f"the assertion answers the request {declared!r}, which this service "
            "provider did not say it made; an identity-provider-initiated login must "
            "not claim to answer one",
        )
    if in_response_to is not None:
        if declared is None:
            raise _refuse(
                "unanswered",
                f"the assertion answers no request, but this service provider is "
                f"expecting the answer to {in_response_to!r}",
            )
        if declared != in_response_to:
            raise _refuse(
                "in-response-to",
                f"the assertion answers the request {declared!r}, not the {in_response_to!r} "
                "this service provider issued",
            )
    return value, name_id.attrib.get("Format", "")


def _read_authn(assertion: Element) -> tuple[str, str, datetime]:
    statements = _children(assertion, _ASSERTION, "AuthnStatement")
    if not statements:
        raise _refuse(
            "authn-absent",
            "the assertion carries no AuthnStatement, so it is an attribute statement "
            "about somebody rather than a record that they authenticated",
        )
    if len(statements) > 1:
        raise _refuse(
            "authn-ambiguous",
            "the assertion carries more than one AuthnStatement, so which "
            "authentication event it records is undecided",
        )
    statement = statements[0]
    instant = _instant(
        _attribute(
            statement, "AuthnInstant", reason="authn-instant", what="<AuthnStatement>"
        ),
        reason="authn-instant",
        what="AuthnStatement/@AuthnInstant",
    )
    context = _only(
        statement, _ASSERTION, "AuthnContext", reason="authn-context", what="an <AuthnStatement>"
    )
    class_refs = _children(context, _ASSERTION, "AuthnContextClassRef")
    if len(class_refs) != 1:
        raise _refuse(
            "authn-context",
            "an AuthnContext must name exactly one AuthnContextClassRef; a policy that "
            "requires a strong factor has nothing to read otherwise",
        )
    return statement.attrib.get("SessionIndex", ""), class_refs[0].text.strip(), instant


def _read_attributes(assertion: Element) -> dict[str, tuple[str, ...]]:
    statements = _children(assertion, _ASSERTION, "AttributeStatement")
    if len(statements) > 1:
        raise _refuse(
            "attributes-split",
            "the assertion carries more than one AttributeStatement, so an attribute "
            "declared in both would have two readings",
        )
    attributes: dict[str, tuple[str, ...]] = {}
    if not statements:
        return attributes
    for attribute in _children(statements[0], _ASSERTION, "Attribute"):
        name = _attribute(attribute, "Name", reason="attribute-unnamed", what="an <Attribute>")
        if name in attributes:
            raise _refuse(
                "attribute-repeated",
                f"the attribute {name!r} is declared twice; merging the two would pick a "
                "reading, and which values an attribute has is not a matter of order",
            )
        # FriendlyName is deliberately ignored. It is an alias the directory
        # chooses and two attributes may share one, so keying on it would let a
        # renamed attribute answer for another.
        values = _children(attribute, _ASSERTION, "AttributeValue")
        if any(value.children for value in values):
            raise _refuse(
                "attribute-structured",
                f"the attribute {name!r} carries a structured AttributeValue; only text "
                "values are read, and flattening markup would invent one of its readings",
            )
        attributes[name] = tuple(value.text for value in values)
    return attributes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _locate_assertion(root: Element) -> tuple[Element, Element]:
    """The `Assertion` to verify and the element the signature may cover."""
    if root.tag == f"{{{_ASSERTION}}}Assertion":
        return root, root
    if root.tag != f"{{{_PROTOCOL}}}Response":
        raise _refuse(
            "root-element",
            f"the document's root is {root.tag!r}; this module reads a SAML 2.0 "
            "<Response> or a bare <Assertion>",
        )
    status = _only(root, _PROTOCOL, "Status", reason="status", what="a <Response>")
    code = _only(status, _PROTOCOL, "StatusCode", reason="status", what="a <Status>")
    value = code.attrib.get("Value", "")
    if value != _STATUS_SUCCESS:
        raise _refuse(
            "status-not-success",
            f"the response reports the status {value!r} rather than Success, so it "
            "carries no assertion to act on",
        )
    if _children(root, _ASSERTION, "EncryptedAssertion"):
        raise _refuse(
            "encrypted-assertion",
            "the response carries an EncryptedAssertion, which this module does not "
            "decrypt; configure the identity provider to sign rather than encrypt",
        )
    assertions = _children(root, _ASSERTION, "Assertion")
    if len(assertions) != 1:
        raise _refuse(
            "assertion-count",
            f"the response carries {len(assertions)} assertions; exactly one is read, "
            "and a second one is how a verified assertion and a consumed assertion "
            "come to be different elements",
        )
    return assertions[0], root


def _signature_of(element: Element) -> Element | None:
    found = _children(element, _DS, "Signature")
    if len(found) > 1:
        raise _refuse(
            "signature-ambiguous",
            f"<{element.tag.rpartition('}')[2]}> carries {len(found)} <Signature> "
            "elements, so which one is being verified is undecided",
        )
    return found[0] if found else None


async def verify_response(
    raw: bytes,
    *,
    idp: IdentityProvider,
    sp: ServiceProvider,
    ledger: ReplayLedger,
    in_response_to: str | None = None,
    now: datetime | None = None,
    limits: Limits | None = None,
) -> VerifiedAssertion:
    """Verify a SAML 2.0 `Response` (or bare `Assertion`) and read its facts.

    `raw` is the XML as it arrived — the decoded `SAMLResponse` form field, not
    the base64 wrapper. `in_response_to` is the `ID` of the authentication
    request this service provider issued, when it issued one; leave it `None`
    for an identity-provider-initiated login, and an assertion claiming to
    answer a request is then refused rather than accepted as unsolicited.

    Raises `SamlRefusal` for everything: a malformed document, a transform
    outside the allow-list, a signature no configured key verifies, an assertion
    outside its window or addressed elsewhere, and an identifier already spent.
    Every refusal carries a `reason` code and says which check failed.

    Returns facts. It does not sign anybody in, does not create a session, and
    does not decide anything — `VerifiedAssertion.facts()` goes into Cedar
    context and a policy reads it there.
    """
    try:
        document = parse(raw, limits or LIMITS)
    except XMLRefusal as refusal:
        raise _refuse(f"xml-{refusal.reason}", str(refusal)) from refusal

    assertion, response = _locate_assertion(document.root)

    # The signature is preferred on the assertion, because that is the subtree
    # whose values are read. A response-level signature is accepted only when
    # the assertion carries none, and it must then cover the response element
    # the assertion was taken from -- checked by object identity below, not by a
    # second lookup.
    signature = _signature_of(assertion)
    covered = assertion
    if signature is None:
        signature = _signature_of(response) if response is not assertion else None
        covered = response
    if signature is None:
        raise _refuse(
            "unsigned",
            "neither the assertion nor the response carries a Signature, and an "
            "unsigned assertion proves nothing about who issued it",
        )
    stray = [
        node
        for node in _descendants(assertion)
        if node.tag == f"{{{_DS}}}Signature" and node is not signature
    ]
    if stray:
        raise _refuse(
            "signature-nested",
            "the assertion contains a Signature other than the one being verified, so "
            "part of what is read carries a claim nothing here checked",
        )

    _verify_signature(document, signature, covered, idp)

    issuer_element = _only(
        assertion, _ASSERTION, "Issuer", reason="issuer", what="an <Assertion>"
    )
    issuer = issuer_element.text.strip()
    if issuer != idp.entity_id:
        raise _refuse(
            "issuer-mismatch",
            f"the assertion was issued by {issuer!r}, not by the identity provider this "
            f"service provider is configured for ({idp.entity_id!r})",
        )

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    not_before, not_after = _check_conditions(assertion, sp, moment)
    name_id, name_id_format = _check_subject(assertion, sp, moment, in_response_to)
    session_index, authn_context, authn_instant = _read_authn(assertion)
    attributes = _read_attributes(assertion)

    assertion_id = _attribute(assertion, "ID", reason="assertion-id", what="an <Assertion>")
    # Spent last, and only once everything above has passed. A ledger is a
    # bounded resource reachable by anyone who can post to the consumer
    # endpoint, so claiming before the signature verifies would let an
    # unauthenticated caller fill it -- and, worse, burn the identifier of an
    # assertion the real user is about to present.
    claimed = ledger.claim(f"{issuer}\x1f{assertion_id}")
    if inspect.isawaitable(claimed):
        claimed = await claimed
    if not claimed:
        raise _refuse(
            "replayed",
            f"the assertion {assertion_id!r} has already been presented; an assertion "
            "identifier is spendable exactly once",
        )

    return VerifiedAssertion(
        assertion_id=assertion_id,
        issuer=issuer,
        name_id=name_id,
        name_id_format=name_id_format,
        session_index=session_index,
        authn_context=authn_context,
        authn_instant=authn_instant,
        not_before=not_before,
        not_on_or_after=not_after,
        attributes=attributes,
    )
