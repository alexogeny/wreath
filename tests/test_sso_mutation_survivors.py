from __future__ import annotations

from typing import Any

import pytest

from wreath.sso import (
    AttributeMapping,
    IdentityProviderConfig,
    IdentityProviderDirectory,
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


@pytest.mark.parametrize("ttl", [0, -1])
def test_pending_login_store_requires_a_positive_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        PendingLoginStore(ttl=ttl)


@pytest.mark.parametrize("max_entries", [0, -1])
def test_pending_login_store_requires_at_least_one_entry(max_entries: int) -> None:
    with pytest.raises(ValueError, match="at least one"):
        PendingLoginStore(max_entries=max_entries)


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


@pytest.mark.parametrize(
    "issuer",
    [
        "http://idp.example",
        "https:///missing-host",
        "https://user@idp.example",
    ],
)
def test_oidc_issuer_requires_https_a_host_and_no_userinfo(issuer: str) -> None:
    with pytest.raises(SsoRefusal) as raised:
        OidcRelyingParty(issuer=issuer, client_id="client")

    assert raised.value.reason == "insecure-issuer"


@pytest.mark.parametrize("ttl", [0, -1])
def test_oidc_requires_a_positive_state_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        _party(ttl=ttl)


@pytest.mark.parametrize("max_pending", [0, -1])
def test_oidc_requires_at_least_one_pending_slot(max_pending: int) -> None:
    with pytest.raises(ValueError, match="at least one"):
        _party(max_pending=max_pending)


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


def test_oidc_begin_login_preserves_an_explicit_timestamp() -> None:
    flow = _party().begin_login(organization="acme", session_id="session", now=123.5)

    assert flow.issued_at == 123.5


def test_oidc_consume_requires_a_non_empty_session_binding() -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="session", now=100)

    with pytest.raises(SsoRefusal) as raised:
        party.consume_state(flow.state, session_id="", now=100)

    assert raised.value.reason == "session-binding-required"


def test_oidc_sweep_returns_at_the_exact_next_deadline() -> None:
    party = _party(ttl=10)
    flow = party.begin_login(organization="acme", session_id="session", now=100)

    party._sweep_flows(110)

    assert party._flows.held(flow.state) is flow
    assert party._next_sweep == 110
