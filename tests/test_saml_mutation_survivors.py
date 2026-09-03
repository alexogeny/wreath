from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from _saml_fixtures import (
    ACS,
    ASSERTION_NS,
    AUDIENCE,
    DS_NS,
    EXC_C14N,
    ISSUER,
    SigningIdentity,
    assertion_xml,
    digest_of,
    response_xml,
    signature_xml,
    signed_response,
)
from cryptography.hazmat.primitives import serialization

import wreath.saml as saml
from wreath.xml import Document, Element, Limits, parse

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _element(xml: str) -> Element:
    return parse(xml.encode()).root


def _reason(expected: str, call, /, *args, **kwargs) -> None:
    with pytest.raises(saml.SamlRefusal) as raised:
        call(*args, **kwargs)
    assert raised.value.reason == expected


@pytest.fixture(scope="module")
def signer() -> SigningIdentity:
    return SigningIdentity()


def test_identity_provider_refuses_each_missing_required_value(signer: SigningIdentity) -> None:
    with pytest.raises(ValueError, match="needs an entity_id to match Issuer"):
        saml.IdentityProvider("", (signer.certificate_pem,))
    with pytest.raises(ValueError, match="needs at least one signing certificate"):
        saml.IdentityProvider(ISSUER, ())


def test_identity_provider_accepts_ascii_certificate_bytes(signer: SigningIdentity) -> None:
    provider = saml.IdentityProvider(
        ISSUER,
        cast(tuple[str, ...], (signer.certificate_pem.encode("ascii"),)),
    )
    assert provider.certificates == (signer.certificate_pem,)
    assert provider.keys[0].family == "RSA"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"entity_id": ""}, "entity_id for AudienceRestriction"),
        ({"acs_url": ""}, "acs_url for SubjectConfirmationData"),
        ({"clock_skew": -0.01}, "clock_skew must be between"),
        ({"clock_skew": 300.01}, "clock_skew must be between"),
    ],
)
def test_service_provider_refuses_each_invalid_setting(
    settings: dict[str, object], message: str
) -> None:
    values = {"entity_id": AUDIENCE, "acs_url": ACS, "clock_skew": 60.0, **settings}
    with pytest.raises(ValueError, match=message):
        saml.ServiceProvider(**values)


def test_key_text_distinguishes_empty_unlabelled_certificates_and_public_keys(
    signer: SigningIdentity,
) -> None:
    with pytest.raises(ValueError, match="key is empty"):
        saml._key_from_text("\n \t\n")

    bare_certificate = signer.certificate_b64
    assert saml._key_from_text(bare_certificate).family == "RSA"

    public_key = signer._key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert saml._key_from_text(public_key.decode()).family == "RSA"


def _tlv(tag: int, value: bytes) -> bytes:
    if len(value) < 128:
        length = bytes((len(value),))
    else:
        encoded = len(value).to_bytes((len(value).bit_length() + 7) // 8, "big")
        length = bytes((0x80 | len(encoded),)) + encoded
    return bytes((tag,)) + length + value


def test_versionless_certificate_walk_does_not_skip_the_serial_number() -> None:
    spki = _tlv(0x30, _tlv(0x30, _tlv(0x06, b"oid")) + _tlv(0x03, b"\x00key"))
    tbs = b"".join(
        (
            _tlv(0x02, b"\x01"),
            _tlv(0x30, b"sig"),
            _tlv(0x30, b"issuer"),
            _tlv(0x30, b"validity"),
            _tlv(0x30, b"subject"),
            spki,
        )
    )
    certificate = _tlv(0x30, _tlv(0x30, tbs) + _tlv(0x30, b"sig") + _tlv(0x03, b"value"))
    assert saml._spki_from_certificate(certificate) == spki


def test_tree_helpers_refuse_missing_duplicate_and_blank_values() -> None:
    parent = _element(f'<a xmlns:s="{ASSERTION_NS}"><s:x/><s:x/></a>')
    _reason("missing", saml._only, parent, ASSERTION_NS, "missing", reason="missing", what="a")
    _reason("duplicate", saml._only, parent, ASSERTION_NS, "x", reason="duplicate", what="a")
    _reason("blank", saml._attribute, parent, "missing", reason="blank", what="a")


def _reference(transforms: str) -> Element:
    return _element(
        f'<ds:Reference xmlns:ds="{DS_NS}" xmlns:ec="{EXC_C14N}">{transforms}</ds:Reference>'
    )


@pytest.mark.parametrize(
    ("transforms", "reason"),
    [
        ("", "transforms-missing"),
        ("<ds:Transforms/>", "transforms-empty"),
        (
            '<ds:Transforms><ds:Transform Algorithm="urn:unknown"/></ds:Transforms>',
            "transform-refused",
        ),
        (
            f'<ds:Transforms><ds:Transform Algorithm="{saml._ENVELOPED}"/>'
            f'<ds:Transform Algorithm="{saml._ENVELOPED}"/></ds:Transforms>',
            "transform-repeated",
        ),
        (
            f'<ds:Transforms><ds:Transform Algorithm="{EXC_C14N}"/>'
            f'<ds:Transform Algorithm="{EXC_C14N}"/></ds:Transforms>',
            "transform-repeated",
        ),
        (
            f'<ds:Transforms><ds:Transform Algorithm="{saml._ENVELOPED}"/></ds:Transforms>',
            "transform-not-exclusive",
        ),
    ],
)
def test_transform_chain_refuses_each_ambiguous_or_unsupported_shape(
    transforms: str, reason: str
) -> None:
    _reason(reason, saml._transform_chain, _reference(transforms))


def test_transform_chain_reports_both_allowed_transforms() -> None:
    reference = _reference(
        f'<ds:Transforms><ds:Transform Algorithm="{saml._ENVELOPED}"/>'
        f'<ds:Transform Algorithm="{EXC_C14N}"><ec:InclusiveNamespaces '
        'PrefixList="saml ds"/></ds:Transform></ds:Transforms>'
    )
    assert saml._transform_chain(reference) == (True, ("saml", "ds"))


def test_inclusive_prefixes_ignores_other_children_and_reads_one_holder() -> None:
    unrelated = _element(
        f'<ds:Transform xmlns:ds="{DS_NS}" xmlns:ec="{EXC_C14N}"><ds:Other '
        'PrefixList="wrong"/></ds:Transform>'
    )
    assert saml._inclusive_prefixes(unrelated) == ()

    declared = _element(
        f'<ds:Transform xmlns:ds="{DS_NS}" xmlns:ec="{EXC_C14N}">'
        '<ec:InclusiveNamespaces PrefixList="a b"/></ds:Transform>'
    )
    assert saml._inclusive_prefixes(declared) == ("a", "b")

    ambiguous = _element(
        f'<ds:Transform xmlns:ds="{DS_NS}" xmlns:ec="{EXC_C14N}">'
        '<ec:InclusiveNamespaces PrefixList="a"/>'
        '<ec:InclusiveNamespaces PrefixList="b"/></ds:Transform>'
    )
    _reason("prefix-list-ambiguous", saml._inclusive_prefixes, ambiguous)


def test_canonical_digest_input_handles_no_exclusion_and_rejects_each_partial_overlap() -> None:
    document = parse(b"<root><target><cut/></target><outside/></root>")
    target = document.root.children[0]
    outside = document.root.children[1]
    assert saml._canonical_digest_input(document, target, None, ()) == document.canonicalize(target)
    assert (
        saml._canonical_digest_input(document, target, target.children[0], ())
        == b"<target></target>"
    )
    _reason("enveloped-outside", saml._canonical_digest_input, document, target, outside, ())

    before = Element("cut", {}, "", "", (), (0, target.span[1] - 1), ())
    after = Element("cut", {}, "", "", (), (target.span[0] + 1, len(document.source)), ())
    _reason("enveloped-outside", saml._canonical_digest_input, document, target, before, ())
    _reason("enveloped-outside", saml._canonical_digest_input, document, target, after, ())


def test_canonical_digest_input_uses_the_document_backend() -> None:
    document = parse(b"<target><cut/></target>")
    target = document.root
    cut = target.children[0]
    calls: list[bytes] = []

    def canonicalizer(source, start, end, inherited, prefixes):
        calls.append(source)
        return b"custom"

    selected = Document(target, document.source, canonicalizer)
    assert saml._canonical_digest_input(selected, target, cut, ()) == b"custom"
    assert calls == [b"<target></target>"]


def test_canonical_digest_input_falls_back_to_the_module_backend() -> None:
    parsed = parse(b"<target><cut/></target>")
    document = Document(parsed.root, parsed.source, None)
    assert (
        saml._canonical_digest_input(document, document.root, document.root.children[0], ())
        == b"<target></target>"
    )


def _signature_case(
    *,
    uri: str = "#_covered",
    transforms: tuple[str, ...] = (EXC_C14N,),
    canonicalization: str = EXC_C14N,
    signature_algorithm: str = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
    digest_algorithm: str = "http://www.w3.org/2001/04/xmlenc#sha256",
    digest: str | None = None,
) -> tuple[Document, Element, Element]:
    covered_xml = '<covered ID="_covered"><value>signed</value></covered>'
    signature = signature_xml(
        digest=digest if digest is not None else digest_of(covered_xml),
        reference=uri,
        signature_algorithm=signature_algorithm,
        transforms=transforms,
        digest_algorithm=digest_algorithm,
        canonicalization=canonicalization,
        value="AA==",
    )
    document = parse(f"<root>{covered_xml}{signature}</root>".encode())
    return document, document.root.children[1], document.root.children[0]


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"canonicalization": "urn:other"}, "c14n-refused"),
        ({"signature_algorithm": "urn:other"}, "signature-algorithm-refused"),
        ({"uri": "external"}, "reference-uri"),
        ({"uri": "#"}, "reference-uri"),
        ({"uri": "#missing"}, "reference-unresolved"),
        ({"digest_algorithm": "urn:other"}, "digest-algorithm-refused"),
        ({"digest": "AA=="}, "digest-mismatch"),
    ],
)
def test_signature_verification_refuses_each_invalid_contract(
    settings: dict[str, object], reason: str
) -> None:
    document, signature, covered = _signature_case(**settings)
    _reason(
        reason,
        saml._verify_signature,
        document,
        signature,
        covered,
        SimpleNamespace(keys=()),
    )


def test_signature_verification_refuses_multiple_references() -> None:
    document, signature, covered = _signature_case()
    raw = document.source
    reference_start = raw.index(b"<ds:Reference")
    reference_end = raw.index(b"</ds:Reference>") + len(b"</ds:Reference>")
    duplicate = raw[:reference_end] + raw[reference_start:reference_end] + raw[reference_end:]
    changed = parse(duplicate)
    _reason(
        "reference-count",
        saml._verify_signature,
        changed,
        changed.root.children[1],
        changed.root.children[0],
        SimpleNamespace(keys=()),
    )


def test_signature_reference_must_resolve_to_the_consumed_element() -> None:
    document, signature, _covered = _signature_case()
    other = _element('<covered ID="_covered"/>')
    _reason(
        "reference-elsewhere",
        saml._verify_signature,
        document,
        signature,
        other,
        SimpleNamespace(keys=()),
    )


def test_signature_reference_must_be_a_fragment_not_an_external_name() -> None:
    document, signature, covered = _signature_case(uri="external")
    _reason(
        "reference-uri",
        saml._verify_signature,
        document,
        signature,
        covered,
        SimpleNamespace(keys=()),
    )


def test_signature_inside_covered_element_requires_the_enveloped_transform() -> None:
    covered_xml = '<covered ID="_covered"><value>signed</value></covered>'
    signature = signature_xml(
        digest=digest_of(covered_xml),
        reference="#_covered",
        signature_algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        transforms=(EXC_C14N,),
        value="AA==",
    )
    document = parse(f'<covered ID="_covered"><value>signed</value>{signature}</covered>'.encode())
    _reason(
        "enveloped-missing",
        saml._verify_signature,
        document,
        document.root.children[1],
        document.root,
        SimpleNamespace(keys=()),
    )


def test_signature_outside_covered_element_is_not_spliced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saml, "_any_key_verifies", lambda *args: True)
    document, signature, covered = _signature_case(transforms=(saml._ENVELOPED, EXC_C14N))
    saml._verify_signature(document, signature, covered, SimpleNamespace(keys=()))


async def test_enveloped_assertion_signature_is_accepted(signer: SigningIdentity) -> None:
    verified = await saml.verify_response(
        signed_response(signer, now=NOW),
        idp=saml.IdentityProvider(ISSUER, (signer.certificate_pem,)),
        sp=saml.ServiceProvider(AUDIENCE, ACS),
        ledger=_Ledger(),
        now=NOW,
    )
    assert verified.assertion_id == "_a1"


def test_any_key_verifies_filters_families_and_accumulates_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rsa_calls: list[object] = []
    ec_calls: list[tuple[int, int]] = []

    def verify_rsa(key, *args) -> bool:
        rsa_calls.append(key)
        return key.n == 1

    def verify_ec(x, y, *args) -> bool:
        ec_calls.append((x, y))
        return False

    monkeypatch.setattr(saml, "_verify_rs", verify_rsa)
    monkeypatch.setattr(saml, "verify_es256", verify_ec)
    provider = SimpleNamespace(
        keys=(
            saml._PublicKey("EC", point=(3, 4)),
            saml._PublicKey("RSA", rsa=saml.RsaPublicKey(1, 3)),
            saml._PublicKey("RSA", rsa=saml.RsaPublicKey(2, 3)),
        )
    )
    assert saml._any_key_verifies(provider, "RSA", "sha256", b"input", b"signature")
    assert [key.n for key in rsa_calls] == [1, 2]
    assert ec_calls == []


def test_any_key_verifies_uses_the_declared_family_before_optional_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rsa = saml.RsaPublicKey(1, 3)
    calls: list[str] = []
    monkeypatch.setattr(
        saml,
        "_verify_rs",
        lambda *args: calls.append("rsa") or True,
    )
    monkeypatch.setattr(
        saml,
        "verify_es256",
        lambda *args: calls.append("ec") or False,
    )

    ec_provider = SimpleNamespace(keys=(saml._PublicKey("EC", rsa=rsa, point=(1, 2)),))
    assert not saml._any_key_verifies(ec_provider, "EC", "sha256", b"input", b"signature")
    assert calls == ["ec"]

    calls.clear()
    rsa_provider = SimpleNamespace(keys=(saml._PublicKey("RSA", point=(1, 2)),))
    assert not saml._any_key_verifies(rsa_provider, "RSA", "sha256", b"input", b"signature")
    assert calls == ["ec"]


def test_any_key_verifies_requires_an_ec_point_and_retains_an_earlier_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def verify(x: int, y: int, *_args: object) -> bool:
        calls.append((x, y))
        return x == 1

    monkeypatch.setattr(saml, "verify_es256", verify)
    missing = SimpleNamespace(keys=(saml._PublicKey("EC"),))
    assert not saml._any_key_verifies(missing, "EC", "sha256", b"input", b"signature")

    provider = SimpleNamespace(
        keys=(saml._PublicKey("EC", point=(1, 2)), saml._PublicKey("EC", point=(3, 4)))
    )
    assert saml._any_key_verifies(provider, "EC", "sha256", b"input", b"signature")
    assert calls == [(1, 2), (3, 4)]


def _conditions(
    inner: str, *, before: datetime | None = None, after: datetime | None = None
) -> Element:
    start = (before or NOW - timedelta(seconds=30)).isoformat()
    end = (after or NOW + timedelta(seconds=30)).isoformat()
    return _element(
        f'<saml:Assertion xmlns:saml="{ASSERTION_NS}"><saml:Conditions '
        f'NotBefore="{start}" NotOnOrAfter="{end}">{inner}</saml:Conditions></saml:Assertion>'
    )


def _audience(value: str = AUDIENCE) -> str:
    return (
        f"<saml:AudienceRestriction><saml:Audience>{value}</saml:Audience>"
        "</saml:AudienceRestriction>"
    )


def test_instant_refuses_a_timestamp_without_timezone() -> None:
    _reason("instant", saml._instant, "2026-08-30T12:00:00", reason="instant", what="value")


@pytest.mark.parametrize(
    ("assertion", "reason"),
    [
        (
            _conditions(_audience(), after=NOW + timedelta(seconds=saml.MAX_LIFETIME + 31)),
            "conditions-too-long",
        ),
        (
            _conditions(
                _audience(),
                before=NOW + timedelta(seconds=61),
                after=NOW + timedelta(seconds=120),
            ),
            "not-yet-valid",
        ),
        (
            _conditions(
                _audience(),
                before=NOW - timedelta(seconds=120),
                after=NOW - timedelta(seconds=60),
            ),
            "expired",
        ),
        (_conditions("<saml:Unknown/>" + _audience()), "condition-unknown"),
        (_conditions(""), "audience-absent"),
    ],
)
def test_conditions_refuse_each_invalid_contract(assertion: Element, reason: str) -> None:
    _reason(reason, saml._check_conditions, assertion, saml.ServiceProvider(AUDIENCE, ACS), NOW)


def _subject(inner: str) -> Element:
    return _element(f'<saml:Assertion xmlns:saml="{ASSERTION_NS}">{inner}</saml:Assertion>')


def _confirmation(
    *,
    method: str = saml._BEARER,
    recipient: str = ACS,
    deadline: datetime = NOW + timedelta(minutes=1),
    data: str = "",
) -> str:
    return (
        f'<saml:SubjectConfirmation Method="{method}"><saml:SubjectConfirmationData '
        f'Recipient="{recipient}" NotOnOrAfter="{deadline.isoformat()}" {data}/>'
        "</saml:SubjectConfirmation>"
    )


@pytest.mark.parametrize(
    ("inner", "request", "reason"),
    [
        (
            f"<saml:Subject><saml:NameID> </saml:NameID>{_confirmation()}</saml:Subject>",
            None,
            "nameid-empty",
        ),
        ("<saml:Subject><saml:NameID>x</saml:NameID></saml:Subject>", None, "confirmation-method"),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation(method='other')}</saml:Subject>",
            None,
            "confirmation-method",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation()}{_confirmation()}</saml:Subject>",
            None,
            "confirmation-ambiguous",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>"
            f"{_confirmation(recipient='elsewhere')}</saml:Subject>",
            None,
            "confirmation-recipient",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>"
            f"{_confirmation(deadline=NOW - timedelta(seconds=60))}"
            "</saml:Subject>",
            None,
            "confirmation-expired",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation(data='NotBefore="2026-08-30T11:00:00Z"')}</saml:Subject>",
            None,
            "confirmation-not-before",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation(data='InResponseTo="_r"')}</saml:Subject>",
            None,
            "unsolicited",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation()}</saml:Subject>",
            "_r",
            "unanswered",
        ),
        (
            f"<saml:Subject><saml:NameID>x</saml:NameID>{_confirmation(data='InResponseTo="_other"')}</saml:Subject>",
            "_r",
            "in-response-to",
        ),
    ],
)
def test_subject_refuses_each_invalid_contract(
    inner: str, request: str | None, reason: str
) -> None:
    _reason(
        reason,
        saml._check_subject,
        _subject(inner),
        saml.ServiceProvider(AUDIENCE, ACS),
        NOW,
        request,
    )


def test_subject_accepts_exactly_one_bearer_confirmation_and_matching_request() -> None:
    assertion = _subject(
        f'<saml:Subject><saml:NameID Format="email">alex</saml:NameID>'
        f"{_confirmation(method='other')}{_confirmation(data='InResponseTo="_r"')}</saml:Subject>"
    )
    assert saml._check_subject(assertion, saml.ServiceProvider(AUDIENCE, ACS), NOW, "_r") == (
        "alex",
        "email",
    )


def _authn(inner: str) -> Element:
    return _element(f'<saml:Assertion xmlns:saml="{ASSERTION_NS}">{inner}</saml:Assertion>')


@pytest.mark.parametrize(
    ("inner", "reason"),
    [
        ("", "authn-absent"),
        ("<saml:AuthnStatement/><saml:AuthnStatement/>", "authn-ambiguous"),
        (
            f'<saml:AuthnStatement AuthnInstant="{NOW.isoformat()}"><saml:AuthnContext/>'
            "</saml:AuthnStatement>",
            "authn-context",
        ),
    ],
)
def test_authn_refuses_each_missing_or_ambiguous_contract(inner: str, reason: str) -> None:
    _reason(reason, saml._read_authn, _authn(inner))


@pytest.mark.parametrize(
    ("inner", "reason"),
    [
        ("<saml:AttributeStatement/><saml:AttributeStatement/>", "attributes-split"),
        (
            '<saml:AttributeStatement><saml:Attribute Name="x"/><saml:Attribute Name="x"/>'
            "</saml:AttributeStatement>",
            "attribute-repeated",
        ),
        (
            '<saml:AttributeStatement><saml:Attribute Name="x"><saml:AttributeValue>'
            "<saml:child/></saml:AttributeValue></saml:Attribute></saml:AttributeStatement>",
            "attribute-structured",
        ),
    ],
)
def test_attributes_refuse_ambiguous_or_structured_values(inner: str, reason: str) -> None:
    _reason(reason, saml._read_attributes, _authn(inner))


def test_attributes_distinguish_absence_from_one_statement() -> None:
    assert saml._read_attributes(_authn("")) == {}
    assertion = _authn(
        '<saml:AttributeStatement><saml:Attribute Name="x"><saml:AttributeValue>one'
        "</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>"
    )
    assert saml._read_attributes(assertion) == {"x": ("one",)}


@pytest.mark.parametrize(
    ("xml", "reason"),
    [
        ("<other/>", "root-element"),
        (
            f'<samlp:Response xmlns:samlp="{saml._PROTOCOL}" xmlns:saml="{ASSERTION_NS}">'
            '<samlp:Status><samlp:StatusCode Value="failure"/></samlp:Status></samlp:Response>',
            "status-not-success",
        ),
        (
            response_xml(f'<saml:EncryptedAssertion xmlns:saml="{ASSERTION_NS}"/>'),
            "encrypted-assertion",
        ),
        (response_xml(""), "assertion-count"),
        (
            response_xml(
                f'<saml:Assertion xmlns:saml="{ASSERTION_NS}"/>'
                f'<saml:Assertion xmlns:saml="{ASSERTION_NS}"/>'
            ),
            "assertion-count",
        ),
    ],
)
def test_locate_assertion_refuses_each_wrong_response_shape(xml: str, reason: str) -> None:
    _reason(reason, saml._locate_assertion, _element(xml))


def test_locate_assertion_accepts_a_bare_assertion() -> None:
    assertion = _element(f'<saml:Assertion xmlns:saml="{ASSERTION_NS}"/>')
    assert saml._locate_assertion(assertion) == (assertion, assertion)


def test_signature_of_distinguishes_zero_one_and_multiple() -> None:
    empty = _element(f'<saml:Assertion xmlns:saml="{ASSERTION_NS}"/>')
    assert saml._signature_of(empty) is None
    one = _element(
        f'<saml:Assertion xmlns:saml="{ASSERTION_NS}" xmlns:ds="{DS_NS}"><ds:Signature/>'
        "</saml:Assertion>"
    )
    assert saml._signature_of(one) is one.children[0]
    duplicate = _element(
        f'<saml:Assertion xmlns:saml="{ASSERTION_NS}" xmlns:ds="{DS_NS}">'
        "<ds:Signature/><ds:Signature/></saml:Assertion>"
    )
    _reason("signature-ambiguous", saml._signature_of, duplicate)


class _Ledger:
    def __init__(self, result=True) -> None:
        self.result = result
        self.keys: list[str] = []

    def claim(self, key: str):
        self.keys.append(key)
        return self.result


async def test_verify_response_honours_explicit_limits_now_and_sync_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saml, "_verify_signature", lambda *args: None)
    raw = assertion_xml(now=NOW, signature_slot=f'<ds:Signature xmlns:ds="{DS_NS}"/>').encode()
    ledger = _Ledger()
    verified = await saml.verify_response(
        raw,
        idp=SimpleNamespace(entity_id=ISSUER),
        sp=saml.ServiceProvider(AUDIENCE, ACS),
        ledger=ledger,
        now=NOW,
        limits=Limits(max_bytes=len(raw), max_depth=40),
    )
    assert verified.name_id == "alex@example.com"
    assert ledger.keys == [f"{ISSUER}\x1f_a1"]


@pytest.mark.parametrize("use_explicit", [False, True])
async def test_verify_response_applies_the_selected_byte_limit(
    monkeypatch: pytest.MonkeyPatch, use_explicit: bool
) -> None:
    raw = assertion_xml(now=NOW, signature_slot=f'<ds:Signature xmlns:ds="{DS_NS}"/>').encode()
    restrictive = Limits(max_bytes=len(raw) - 1, max_depth=40)
    if use_explicit:
        limits = restrictive
    else:
        monkeypatch.setattr(saml, "LIMITS", restrictive)
        limits = None
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(
            raw,
            idp=SimpleNamespace(entity_id=ISSUER),
            sp=saml.ServiceProvider(AUDIENCE, ACS),
            ledger=_Ledger(),
            now=NOW,
            limits=limits,
        )
    assert raised.value.reason == "xml-size"


async def test_verify_response_uses_a_response_signature_when_the_assertion_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saml, "_verify_signature", lambda *args: None)
    signature = f'<ds:Signature xmlns:ds="{DS_NS}"/>'
    raw = response_xml(signature + assertion_xml(now=NOW)).encode()
    verified = await saml.verify_response(
        raw,
        idp=SimpleNamespace(entity_id=ISSUER),
        sp=saml.ServiceProvider(AUDIENCE, ACS),
        ledger=_Ledger(),
        now=NOW,
    )
    assert verified.assertion_id == "_a1"


async def test_verify_response_refuses_unsigned_nested_wrong_issuer_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(entity_id=ISSUER)
    service = saml.ServiceProvider(AUDIENCE, ACS)
    monkeypatch.setattr(saml, "_verify_signature", lambda *args: None)
    unsigned = assertion_xml(now=NOW).encode()
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(unsigned, idp=provider, sp=service, ledger=_Ledger(), now=NOW)
    assert raised.value.reason == "unsigned"

    signature = f'<ds:Signature xmlns:ds="{DS_NS}"/>'
    nested = assertion_xml(
        now=NOW,
        signature_slot=signature,
        attributes=(
            f'<saml:AttributeStatement xmlns:saml="{ASSERTION_NS}">{signature}'
            "</saml:AttributeStatement>"
        ),
    ).encode()
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(nested, idp=provider, sp=service, ledger=_Ledger(), now=NOW)
    assert raised.value.reason == "signature-nested"

    wrong_issuer = assertion_xml(now=NOW, issuer="other", signature_slot=signature).encode()
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(
            wrong_issuer, idp=provider, sp=service, ledger=_Ledger(), now=NOW
        )
    assert raised.value.reason == "issuer-mismatch"

    valid = assertion_xml(now=NOW, signature_slot=signature).encode()
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(valid, idp=provider, sp=service, ledger=_Ledger(False), now=NOW)
    assert raised.value.reason == "replayed"


async def test_verify_response_awaits_an_async_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(saml, "_verify_signature", lambda *args: None)

    class AsyncLedger:
        async def claim(self, key: str) -> bool:
            return key.endswith("_a1")

    raw = assertion_xml(now=NOW, signature_slot=f'<ds:Signature xmlns:ds="{DS_NS}"/>').encode()
    verified = await saml.verify_response(
        raw,
        idp=SimpleNamespace(entity_id=ISSUER),
        sp=saml.ServiceProvider(AUDIENCE, ACS),
        ledger=AsyncLedger(),
        now=NOW,
    )
    assert verified.assertion_id == "_a1"


async def test_verify_response_refuses_an_async_ledger_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saml, "_verify_signature", lambda *args: None)

    class AsyncLedger:
        async def claim(self, key: str) -> bool:
            return False

    raw = assertion_xml(now=NOW, signature_slot=f'<ds:Signature xmlns:ds="{DS_NS}"/>').encode()
    with pytest.raises(saml.SamlRefusal) as raised:
        await saml.verify_response(
            raw,
            idp=SimpleNamespace(entity_id=ISSUER),
            sp=saml.ServiceProvider(AUDIENCE, ACS),
            ledger=AsyncLedger(),
            now=NOW,
        )
    assert raised.value.reason == "replayed"
