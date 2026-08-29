from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from wreath.xml import Element, parse

ISSUER = "https://idp.example/metadata"
AUDIENCE = "https://app.example/saml"
ACS = "https://app.example/acs"

PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
ASSERTION_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
SHA256_DIGEST = "http://www.w3.org/2001/04/xmlenc#sha256"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
ECDSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"

_SIGNATURE_PLACEHOLDER = "SIGNATURE-VALUE-PLACEHOLDER"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SigningIdentity:
    """A key pair and a self-signed certificate that stands in for an IdP."""

    algorithm: str = "rsa"
    _key: object = field(init=False, repr=False, default=None)
    certificate_pem: str = field(init=False, default="")
    #: The bare base64 DER a `<ds:X509Certificate>` element would carry.
    certificate_b64: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if self.algorithm == "rsa":
            key: object = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        self._key = key
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())  # type: ignore[attr-defined]
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(key, hashes.SHA256())  # type: ignore[arg-type]
        )
        der = certificate.public_bytes(serialization.Encoding.DER)
        self.certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        self.certificate_b64 = base64.b64encode(der).decode()

    @property
    def signature_algorithm(self) -> str:
        return RSA_SHA256 if self.algorithm == "rsa" else ECDSA_SHA256

    def sign(self, payload: bytes) -> bytes:
        if self.algorithm == "rsa":
            return self._key.sign(payload, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[attr-defined]
        der = self._key.sign(payload, ec.ECDSA(hashes.SHA256()))  # type: ignore[attr-defined]
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def assertion_xml(
    *,
    assertion_id: str = "_a1",
    issuer: str = ISSUER,
    name_id: str = "alex@example.com",
    name_id_format: str = "urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress",
    audience: str = AUDIENCE,
    recipient: str = ACS,
    in_response_to: str | None = None,
    now: datetime | None = None,
    lifetime: int = 300,
    confirmation_lifetime: int = 300,
    conditions: str | None = None,
    attributes: str | None = None,
    authn: str | None = None,
    subject: str | None = None,
    signature_slot: str = "",
) -> str:
    """The assertion, with `signature_slot` inserted where a Signature belongs."""
    moment = now or datetime.now(UTC)
    not_before = _stamp(moment - timedelta(seconds=30))
    not_after = _stamp(moment + timedelta(seconds=lifetime))
    confirm_after = _stamp(moment + timedelta(seconds=confirmation_lifetime))
    answers = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    if conditions is None:
        conditions = (
            f'<saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_after}">'
            f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
            "</saml:AudienceRestriction></saml:Conditions>"
        )
    if subject is None:
        subject = (
            "<saml:Subject>"
            f'<saml:NameID Format="{name_id_format}">{name_id}</saml:NameID>'
            '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData NotOnOrAfter="{confirm_after}"'
            f' Recipient="{recipient}"{answers}/>'
            "</saml:SubjectConfirmation></saml:Subject>"
        )
    if authn is None:
        authn = (
            f'<saml:AuthnStatement AuthnInstant="{_stamp(moment)}" SessionIndex="_s1">'
            "<saml:AuthnContext><saml:AuthnContextClassRef>"
            "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
            "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        )
    if attributes is None:
        attributes = (
            "<saml:AttributeStatement>"
            '<saml:Attribute Name="groups" FriendlyName="Groups">'
            "<saml:AttributeValue>engineering</saml:AttributeValue>"
            "<saml:AttributeValue>oncall</saml:AttributeValue>"
            "</saml:Attribute>"
            '<saml:Attribute Name="department">'
            "<saml:AttributeValue>platform</saml:AttributeValue>"
            "</saml:Attribute>"
            "</saml:AttributeStatement>"
        )
    return (
        f'<saml:Assertion xmlns:saml="{ASSERTION_NS}" ID="{assertion_id}" Version="2.0"'
        f' IssueInstant="{_stamp(moment)}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"{signature_slot}{subject}{conditions}{authn}{attributes}"
        "</saml:Assertion>"
    )


def signature_xml(
    *,
    digest: str,
    reference: str,
    signature_algorithm: str,
    transforms: tuple[str, ...] = (ENVELOPED, EXC_C14N),
    digest_algorithm: str = SHA256_DIGEST,
    canonicalization: str = EXC_C14N,
    value: str = _SIGNATURE_PLACEHOLDER,
) -> str:
    declared = "".join(f'<ds:Transform Algorithm="{name}"/>' for name in transforms)
    return (
        f'<ds:Signature xmlns:ds="{DS_NS}"><ds:SignedInfo>'
        f'<ds:CanonicalizationMethod Algorithm="{canonicalization}"/>'
        f'<ds:SignatureMethod Algorithm="{signature_algorithm}"/>'
        f'<ds:Reference URI="{reference}"><ds:Transforms>{declared}</ds:Transforms>'
        f'<ds:DigestMethod Algorithm="{digest_algorithm}"/>'
        f"<ds:DigestValue>{digest}</ds:DigestValue></ds:Reference></ds:SignedInfo>"
        f"<ds:SignatureValue>{value}</ds:SignatureValue></ds:Signature>"
    )


def digest_of(xml: str, *, algorithm: str = "sha256") -> str:
    """SHA-256 of the exclusive canonical form of `xml`, parsed on its own.

    This is the independent derivation: the caller hands over the assertion as
    it reads *without* a signature, so nothing here splices anything.
    """
    document = parse(xml.encode())
    return base64.b64encode(hashlib.new(algorithm, document.canonicalize()).digest()).decode()


def response_xml(assertion: str, *, response_id: str = "_r1", status: str | None = None) -> str:
    code = status or "urn:oasis:names:tc:SAML:2.0:status:Success"
    return (
        f'<samlp:Response xmlns:samlp="{PROTOCOL_NS}" ID="{response_id}" Version="2.0"'
        f' IssueInstant="{_stamp(datetime.now(UTC))}">'
        f'<samlp:Status><samlp:StatusCode Value="{code}"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )


def sign_document(identity: SigningIdentity, document_xml: str) -> bytes:
    """Fill in the SignatureValue placeholder over the real SignedInfo bytes.

    `SignatureValue` follows `SignedInfo`, so substituting into it cannot move
    the bytes that were just canonicalized and signed.
    """
    document = parse(document_xml.encode())
    signed_info = _find(document.root, f"{{{DS_NS}}}SignedInfo")
    payload = document.canonicalize(signed_info)
    value = base64.b64encode(identity.sign(payload)).decode()
    return document_xml.replace(_SIGNATURE_PLACEHOLDER, value).encode()


def _find(element: Element, tag: str) -> Element | None:
    if element.tag == tag:
        return element
    for child in element.children:
        found = _find(child, tag)
        if found is not None:
            return found
    return None


def signed_response(
    identity: SigningIdentity,
    *,
    transforms: tuple[str, ...] = (ENVELOPED, EXC_C14N),
    digest_algorithm: str = SHA256_DIGEST,
    canonicalization: str = EXC_C14N,
    signature_algorithm: str | None = None,
    reference: str | None = None,
    **assertion_kwargs: object,
) -> bytes:
    """A complete, genuinely signed SAML response."""
    assertion_id = str(assertion_kwargs.get("assertion_id", "_a1"))
    # One moment for both renderings. Two calls to `datetime.now` would make the
    # signed assertion differ from the one the digest was taken over by more
    # than the signature, which would hide a broken splice behind a broken
    # fixture.
    assertion_kwargs.setdefault("now", datetime.now(UTC))
    plain = assertion_xml(**assertion_kwargs)  # type: ignore[arg-type]
    signature = signature_xml(
        digest=digest_of(plain),
        reference=reference or f"#{assertion_id}",
        signature_algorithm=signature_algorithm or identity.signature_algorithm,
        transforms=transforms,
        digest_algorithm=digest_algorithm,
        canonicalization=canonicalization,
    )
    signed = assertion_xml(signature_slot=signature, **assertion_kwargs)  # type: ignore[arg-type]
    return sign_document(identity, response_xml(signed))
