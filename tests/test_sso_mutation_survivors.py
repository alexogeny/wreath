from __future__ import annotations

from typing import Any, cast

import pytest

from wreath.sso import (
    AttributeMapping,
    IdentityProviderConfig,
    IdentityProviderDirectory,
    JitProvisioning,
    OidcRelyingParty,
    PendingLogin,
    PendingLoginStore,
    SamlServiceProvider,
    SsoRefusal,
)


def _pending(
    request_id: str,
    *,
    issued_at: float = 100.0,
    relay_state: str = "relay",
    session_id: str = "session",
) -> PendingLogin:
    return PendingLogin(request_id, relay_state, "acme", issued_at, session_id)


def _provider() -> SamlServiceProvider:
    config = IdentityProviderConfig(
        organization="acme",
        entity_id="https://idp.example/entity",
        sso_url="https://idp.example/sso",
        certificates=("certificate",),
    )
    return SamlServiceProvider(
        entity_id="https://service.example/entity",
        acs_url="https://service.example/acs",
        directory=IdentityProviderDirectory((config,)),
    )


@pytest.mark.parametrize(
    "acs_url",
    [
        "http://service.example/acs",
        "https:///acs",
        "https://user@service.example/acs",
        "https://service.example:invalid/acs",
        "https://service.example:0/acs",
        "https://service.example/acs#fragment",
        "https://service.example\\evil.example/acs",
    ],
)
def test_saml_service_provider_refuses_an_unsafe_assertion_consumer_url(
    acs_url: str,
) -> None:
    config = IdentityProviderConfig("acme", "entity", "https://idp.example/sso", ("certificate",))

    with pytest.raises(SsoRefusal, match="assertion consumer.*absolute HTTPS"):
        SamlServiceProvider(
            entity_id="entity",
            acs_url=acs_url,
            directory=IdentityProviderDirectory((config,)),
        )


@pytest.mark.parametrize("entity_id", [None, "", 1])
def test_saml_service_provider_requires_a_text_entity_id(entity_id: Any) -> None:
    with pytest.raises(SsoRefusal) as raised:
        SamlServiceProvider(
            entity_id=entity_id,
            acs_url="https://service.example/acs",
            directory=IdentityProviderDirectory(),
        )

    assert raised.value.reason == "invalid-service-provider"


@pytest.mark.parametrize(
    "acs_url", ["https://service.example/a\tb", "https://service.example/a\x7fb"]
)
def test_saml_service_provider_refuses_control_characters_in_acs_url(acs_url: str) -> None:
    with pytest.raises(SsoRefusal) as raised:
        SamlServiceProvider(
            entity_id="entity",
            acs_url=acs_url,
            directory=IdentityProviderDirectory(),
        )

    assert raised.value.reason == "insecure-acs-url"


def test_saml_metadata_escapes_configured_certificate_text() -> None:
    provider = SamlServiceProvider(
        entity_id="entity",
        acs_url="https://service.example/acs",
        directory=IdentityProviderDirectory(),
        certificates=("certificate</ds:X509Certificate><evil/>",),
    )

    document = provider.metadata_xml()

    assert "<evil" not in document
    assert "&lt;evil/&gt;" in document


def test_saml_service_provider_copies_metadata_certificates() -> None:
    certificates: Any = ["certificate"]
    provider = SamlServiceProvider(
        entity_id="entity",
        acs_url="https://service.example/acs",
        directory=IdentityProviderDirectory(),
        certificates=certificates,
    )

    certificates.append("replacement")

    assert provider.certificates == ("certificate",)


@pytest.mark.parametrize("certificates", ["certificate", (None,), ("",)])
def test_saml_service_provider_refuses_malformed_metadata_certificates(
    certificates: Any,
) -> None:
    with pytest.raises(SsoRefusal) as raised:
        SamlServiceProvider(
            entity_id="entity",
            acs_url="https://service.example/acs",
            directory=IdentityProviderDirectory(),
            certificates=certificates,
        )

    assert raised.value.reason == "invalid-service-provider-certificate"


def _party(**options: Any) -> OidcRelyingParty:
    return OidcRelyingParty(
        issuer="https://idp.example",
        client_id="client",
        **options,
    )


def test_identity_provider_requires_at_least_one_signing_certificate() -> None:
    with pytest.raises(SsoRefusal) as raised:
        IdentityProviderConfig("acme", "entity", "https://idp.example/sso", ())

    assert raised.value.reason == "no-signer"


def test_identity_provider_bounds_configured_signing_certificates() -> None:
    with pytest.raises(SsoRefusal, match="at most 16 signing certificates"):
        IdentityProviderConfig(
            "acme",
            "entity",
            "https://idp.example/sso",
            tuple(f"certificate-{index}" for index in range(17)),
        )


@pytest.mark.parametrize("certificates", ["certificate", ("certificate", None), ("",)])
def test_identity_provider_refuses_malformed_certificate_collections(
    certificates: Any,
) -> None:
    with pytest.raises(SsoRefusal) as raised:
        IdentityProviderConfig(
            "acme",
            "entity",
            "https://idp.example/sso",
            certificates,
        )

    assert raised.value.reason == "invalid-signer-configuration"


@pytest.mark.parametrize("kid", [None, [1], 1, "", "x" * 257])
def test_oidc_key_lookup_refuses_invalid_key_identifiers_without_echoing_them(
    kid: Any,
) -> None:
    party = _party()

    with pytest.raises(SsoRefusal) as raised:
        party.key_for(kid)

    assert raised.value.reason == "invalid-key-id"
    assert len(str(raised.value)) < 300


@pytest.mark.parametrize(
    "sso_url",
    [
        "http://idp.example/sso",
        "https:///sso",
        "https://operator@idp.example/sso",
        "https://idp.example/sso#fragment",
        "https://idp.example/ss\to",
        "https://idp.example/sso\x7f",
        "https://idp.example:invalid/sso",
        "https://idp.example:0/sso",
        "https://idp.example\\evil.example/sso",
    ],
)
def test_identity_provider_refuses_an_unsafe_browser_redirect(sso_url: str) -> None:
    with pytest.raises(SsoRefusal, match="SSO URL.*absolute HTTPS"):
        IdentityProviderConfig("acme", "entity", sso_url, ("certificate",))


def test_identity_provider_directory_refuses_duplicate_organisation_anchors() -> None:
    first = IdentityProviderConfig(
        "acme", "first", "https://first.example/sso", ("first-certificate",)
    )
    replacement = IdentityProviderConfig(
        "acme", "replacement", "https://replacement.example/sso", ("replacement-certificate",)
    )

    with pytest.raises(SsoRefusal, match="duplicate.*acme"):
        IdentityProviderDirectory((first, replacement))


def test_identity_provider_directory_add_cannot_replace_a_trust_anchor() -> None:
    first = IdentityProviderConfig(
        "acme", "first", "https://first.example/sso", ("first-certificate",)
    )
    replacement = IdentityProviderConfig(
        "acme", "replacement", "https://replacement.example/sso", ("replacement",)
    )
    directory = IdentityProviderDirectory((first,))

    with pytest.raises(SsoRefusal, match="duplicate.*acme"):
        directory.add(replacement)

    assert directory.for_organization("acme") is first


def test_identity_provider_directory_add_accepts_a_new_organization() -> None:
    provider = IdentityProviderConfig("acme", "entity", "https://idp.example/sso", ("certificate",))
    directory = IdentityProviderDirectory()

    directory.add(provider)

    assert directory.for_organization("acme") is provider


def test_identity_provider_copies_mutable_trust_configuration() -> None:
    certificates: Any = ["first-certificate"]
    roles: Any = ["member"]
    provider = IdentityProviderConfig(
        "acme",
        "entity",
        "https://idp.example/sso",
        certificates,
        roles=roles,
    )

    certificates[0] = "attacker-certificate"
    roles[0] = "owner"

    assert provider.certificates == ("first-certificate",)
    assert provider.roles == ("member",)


def test_identity_provider_refuses_ambiguous_role_configuration() -> None:
    roles: Any = "admin"
    with pytest.raises(SsoRefusal) as raised:
        IdentityProviderConfig(
            "acme",
            "entity",
            "https://idp.example/sso",
            ("certificate",),
            roles=roles,
        )

    assert raised.value.reason == "invalid-role-configuration"


@pytest.mark.parametrize("roles", [(None,), ("",), (1,)])
def test_identity_provider_refuses_invalid_role_elements(roles: Any) -> None:
    with pytest.raises(SsoRefusal) as raised:
        IdentityProviderConfig(
            "acme",
            "entity",
            "https://idp.example/sso",
            ("certificate",),
            roles=roles,
        )

    assert raised.value.reason == "invalid-role-configuration"


def test_identity_provider_second_factor_flag_requires_a_boolean() -> None:
    invalid: Any = 1
    with pytest.raises(SsoRefusal) as raised:
        IdentityProviderConfig(
            "acme",
            "entity",
            "https://idp.example/sso",
            ("certificate",),
            require_second_factor=invalid,
        )

    assert raised.value.reason == "invalid-second-factor"


@pytest.mark.parametrize("ttl", [0, -1])
def test_pending_login_store_requires_a_positive_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl must be a positive finite number"):
        PendingLoginStore(ttl=ttl)


@pytest.mark.parametrize("max_entries", [0, -1, 1.5])
def test_pending_login_store_requires_at_least_one_entry(max_entries: Any) -> None:
    with pytest.raises(ValueError, match="at least one"):
        PendingLoginStore(max_entries=max_entries)


def test_pending_login_store_refuses_boolean_capacity() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PendingLoginStore(max_entries=True)


def test_pending_login_store_refuses_an_insert_at_capacity() -> None:
    store = PendingLoginStore(max_entries=1)
    store.put(_pending("first"))

    with pytest.raises(SsoRefusal) as raised:
        store.put(_pending("second"))

    assert raised.value.reason == "pending-capacity"


@pytest.mark.parametrize(
    ("relay_state", "session_id"),
    [("", "session"), ("relay", "")],
)
def test_pending_login_spend_requires_both_browser_bindings(
    relay_state: str, session_id: str
) -> None:
    store = PendingLoginStore()
    store.put(
        _pending(
            "request",
            relay_state=relay_state,
            session_id=session_id,
        )
    )

    with pytest.raises(SsoRefusal) as raised:
        store.spend("request", relay_state=relay_state, session_id=session_id, now=100)

    assert raised.value.reason == "state-session-mismatch"
    assert str(raised.value) == "SAML RelayState and its browser session binding are required"


def test_pending_login_spend_checks_relay_state_independently() -> None:
    store = PendingLoginStore()
    store.put(_pending("request"))

    with pytest.raises(SsoRefusal, match="different browser session"):
        store.spend("request", relay_state="other", session_id="session", now=100)


def test_pending_login_repr_hides_browser_capabilities() -> None:
    pending = _pending(
        "request", relay_state="relay-secret-value", session_id="session-secret-value"
    )

    rendered = repr(pending)

    assert "relay-secret-value" not in rendered
    assert "session-secret-value" not in rendered


def test_pending_login_organization_refuses_an_unknown_request() -> None:
    with pytest.raises(SsoRefusal) as raised:
        PendingLoginStore().organization_for("unknown")

    assert raised.value.reason == "unsolicited"


def test_pending_login_sweep_returns_before_the_next_deadline() -> None:
    store = PendingLoginStore(ttl=10)
    pending = _pending("request")
    store.put(pending)

    store._sweep(109)

    assert store._by_id.held("request") is pending
    assert store._next_sweep == 110


def test_saml_begin_login_preserves_an_explicit_timestamp() -> None:
    flow = _provider().begin_login(organization="acme", session_id="session", now=123.5)

    assert flow.issued_at == 123.5


def test_saml_begin_login_requires_a_text_session_binding() -> None:
    session_id: Any = True
    with pytest.raises(SsoRefusal) as raised:
        _provider().begin_login(organization="acme", session_id=session_id)

    assert raised.value.reason == "session-binding-required"


@pytest.mark.parametrize(
    ("mapping", "attributes", "field", "expected"),
    [
        (AttributeMapping(), {"email": "a@example.com"}, "display_name", None),
        (
            AttributeMapping(display_name="name"),
            {"email": "a@example.com", "name": "Ada"},
            "display_name",
            "Ada",
        ),
        (AttributeMapping(), {"email": "a@example.com"}, "external_id", None),
        (
            AttributeMapping(external_id="subject"),
            {"email": "a@example.com", "subject": "123"},
            "external_id",
            "123",
        ),
    ],
)
def test_attribute_mapping_respects_optional_declarations(
    mapping: AttributeMapping,
    attributes: dict[str, str],
    field: str,
    expected: str | None,
) -> None:
    assert mapping.apply(attributes)[field] == expected


@pytest.mark.parametrize("value", [{"only": "value"}, [None]])
def test_attribute_mapping_refuses_non_text_single_value_shapes(value: Any) -> None:
    with pytest.raises(SsoRefusal) as raised:
        AttributeMapping().apply({"email": value})

    assert raised.value.reason == "attribute-cardinality"


@pytest.mark.parametrize(
    "options",
    [
        {"roles": "member"},
        {"vocabulary": "member"},
        {"roles": (None,)},
        {"roles": ("",)},
        {"roles": (1,)},
    ],
)
def test_jit_provisioning_refuses_ambiguous_role_configuration(options: Any) -> None:
    with pytest.raises(SsoRefusal) as raised:
        JitProvisioning(**options)

    assert raised.value.reason == "invalid-role-configuration"


@pytest.mark.parametrize(
    ("organization", "email"),
    [(None, "a@example.com"), ("acme", None)],
)
def test_jit_provisioning_refuses_non_text_identity(
    organization: Any,
    email: Any,
) -> None:
    with pytest.raises(SsoRefusal) as raised:
        JitProvisioning().provision(organization=organization, email=email)

    assert raised.value.reason == "invalid-identity"


@pytest.mark.parametrize(
    "issuer",
    [
        "http://idp.example",
        "https:///missing-host",
        "https://user@idp.example",
        "https://idp.example:0",
        "https://idp.example\\evil.example",
    ],
)
def test_oidc_issuer_requires_https_a_host_and_no_userinfo(issuer: str) -> None:
    with pytest.raises(SsoRefusal) as raised:
        OidcRelyingParty(issuer=issuer, client_id="client")

    assert raised.value.reason == "insecure-issuer"


@pytest.mark.parametrize("ttl", [0, -1])
def test_oidc_requires_a_positive_state_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="state ttl must be a positive finite number"):
        _party(ttl=ttl)


@pytest.mark.parametrize("max_pending", [0, -1, 1.5])
def test_oidc_requires_at_least_one_pending_slot(max_pending: Any) -> None:
    with pytest.raises(ValueError, match="at least one"):
        _party(max_pending=max_pending)


def test_oidc_refuses_boolean_pending_capacity() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _party(max_pending=True)


@pytest.mark.asyncio
async def test_oidc_refresh_refuses_a_mismatched_discovery_issuer() -> None:
    async def fetch(_url: str) -> dict[str, str]:
        return {
            "issuer": "https://other.example",
            "jwks_uri": "https://idp.example/jwks",
        }

    with pytest.raises(SsoRefusal) as raised:
        await _party().refresh(fetch)

    assert raised.value.reason == "issuer-mismatch"


async def test_oidc_refresh_requires_discovery_to_declare_its_issuer() -> None:
    async def fetch(_url: str) -> dict[str, str]:
        return {"jwks_uri": "https://idp.example/jwks"}

    with pytest.raises(SsoRefusal) as raised:
        await _party().refresh(fetch)

    assert raised.value.reason == "issuer-mismatch"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"issuer": "https://idp.example"},
        {"issuer": "https://idp.example", "jwks_uri": None},
        {"issuer": "https://idp.example", "jwks_uri": ""},
        {"issuer": "https://idp.example", "jwks_uri": 1},
    ],
)
async def test_oidc_refresh_refuses_a_malformed_discovery_document(document: Any) -> None:
    async def fetch(_url: str) -> Any:
        return document

    with pytest.raises(SsoRefusal) as raised:
        await _party().refresh(fetch)

    assert raised.value.reason == "invalid-discovery"


@pytest.mark.parametrize(
    "jwks",
    [
        [],
        {"keys": ""},
        {"keys": "not-a-list"},
        {"keys": ["not-a-key"]},
        {"keys": [{}]},
        {"keys": [{"kid": ""}]},
        {"keys": [{"kid": False}]},
        {"keys": [{"kid": 1}]},
        {"keys": [{"kid": "x" * 257}]},
        {"keys": [{"kid": f"key-{index}"} for index in range(129)]},
    ],
)
async def test_oidc_refresh_refuses_malformed_or_unbounded_key_sets(jwks: Any) -> None:
    async def fetch(url: str) -> dict[str, Any]:
        if url.endswith("openid-configuration"):
            return {
                "issuer": "https://idp.example",
                "jwks_uri": "https://idp.example/jwks",
            }
        return jwks

    with pytest.raises(SsoRefusal) as raised:
        await _party().refresh(fetch)

    assert raised.value.reason == "invalid-jwks"


async def test_oidc_refresh_copies_and_freezes_loaded_key_material() -> None:
    key: dict[str, Any] = {"kid": "key-1", "x5c": ["certificate-1"]}

    async def fetch(url: str) -> dict[str, Any]:
        if url.endswith("openid-configuration"):
            return {
                "issuer": "https://idp.example",
                "jwks_uri": "https://idp.example/jwks",
            }
        return {"keys": [key]}

    party = _party()
    await party.refresh(fetch)
    key["kid"] = "attacker-key"
    key["x5c"][0] = "attacker-certificate"

    loaded = party.key_for("key-1")
    assert loaded["kid"] == "key-1"
    assert loaded["x5c"] == ("certificate-1",)
    with pytest.raises(TypeError):
        loaded["kid"] = "replacement"


def test_oidc_begin_login_preserves_an_explicit_timestamp() -> None:
    flow = _party().begin_login(organization="acme", session_id="session", now=123.5)

    assert flow.issued_at == 123.5


def test_oidc_begin_login_requires_a_text_session_binding() -> None:
    session_id: Any = True
    with pytest.raises(SsoRefusal) as raised:
        _party().begin_login(organization="acme", session_id=session_id)

    assert raised.value.reason == "session-binding-required"


def test_oidc_consume_requires_a_non_empty_session_binding() -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="session", now=100)

    with pytest.raises(SsoRefusal) as raised:
        party.consume_state(flow.state, session_id="", now=100)

    assert raised.value.reason == "session-binding-required"


def test_oidc_consume_requires_a_text_session_binding() -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="session", now=100)

    with pytest.raises(SsoRefusal) as raised:
        party.consume_state(flow.state, session_id=cast(Any, ["session"]), now=100)

    assert raised.value.reason == "session-binding-required"


def test_oidc_nonce_requires_a_text_expected_binding() -> None:
    with pytest.raises(SsoRefusal) as raised:
        _party().check_nonce({"nonce": "nonce"}, expected_nonce=cast(Any, ["nonce"]))

    assert raised.value.reason == "nonce-mismatch"


def test_oidc_flow_repr_hides_authorization_capabilities() -> None:
    flow = _party().begin_login(organization="acme", session_id="session-secret-value", now=100)

    rendered = repr(flow)

    assert flow.state not in rendered
    assert flow.nonce not in rendered
    assert flow.verifier not in rendered
    assert "session-secret-value" not in rendered


def test_oidc_sweep_returns_at_the_exact_next_deadline() -> None:
    party = _party(ttl=10)
    flow = party.begin_login(organization="acme", session_id="session", now=100)

    party._sweep_flows(110)

    assert party._flows.held(flow.state) is flow
    assert party._next_sweep == 110
