from __future__ import annotations

import pytest

from wreath.sso import OidcRelyingParty, SsoRefusal


def _party() -> OidcRelyingParty:
    return OidcRelyingParty(issuer="https://idp.example", client_id="wreath-app")


def test_the_plain_pkce_method_is_refused() -> None:
    with pytest.raises(SsoRefusal, match="protects nothing") as raised:
        OidcRelyingParty(issuer="https://idp.example", client_id="x", pkce="plain")
    assert raised.value.reason == "weak-pkce"


@pytest.mark.parametrize(
    "issuer",
    [
        "https://user:password@idp.example",
        "https://idp.example?tenant=acme",
        "https://idp.example#issuer",
    ],
)
def test_oidc_relying_party_refuses_an_ambiguous_issuer(issuer: str) -> None:
    with pytest.raises(SsoRefusal, match="absolute HTTPS URL"):
        OidcRelyingParty(issuer=issuer, client_id="x")


def test_beginning_a_login_mints_state_nonce_and_an_s256_challenge() -> None:
    flow = _party().begin_login(organization="acme", session_id="s1")
    assert flow.state and flow.nonce and flow.verifier
    assert flow.challenge != flow.verifier
    # base64url, unpadded, as RFC 7636 requires.
    assert "=" not in flow.challenge


def test_the_challenge_is_the_sha256_of_the_verifier() -> None:
    import hashlib
    from base64 import urlsafe_b64encode

    flow = _party().begin_login(organization="acme", session_id="s1")
    expected = (
        urlsafe_b64encode(hashlib.sha256(flow.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert flow.challenge == expected


def test_two_logins_never_share_a_state() -> None:
    party = _party()
    states = {party.begin_login(organization="acme", session_id="s1").state for _ in range(50)}
    assert len(states) == 50


def test_the_state_is_single_use() -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="s1")
    party.consume_state(flow.state, session_id="s1")
    with pytest.raises(SsoRefusal, match="already spent") as raised:
        party.consume_state(flow.state, session_id="s1")
    assert raised.value.reason == "unknown-state"


def test_a_state_from_another_browser_session_is_refused() -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="victim")
    with pytest.raises(SsoRefusal, match="different browser session") as raised:
        party.consume_state(flow.state, session_id="attacker")
    assert raised.value.reason == "state-session-mismatch"


def test_an_empty_browser_session_binding_is_refused() -> None:
    party = _party()
    with pytest.raises(SsoRefusal, match="session"):
        party.begin_login(organization="acme", session_id="")


def test_an_expired_oidc_state_is_refused() -> None:
    party = OidcRelyingParty(issuer="https://idp.example", client_id="wreath-app", ttl=10)
    flow = party.begin_login(organization="acme", session_id="s1", now=100)
    with pytest.raises(SsoRefusal, match="expired") as raised:
        party.consume_state(flow.state, session_id="s1", now=111)
    assert raised.value.reason == "expired-state"


def test_oidc_state_lifetime_must_be_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        OidcRelyingParty(
            issuer="https://idp.example",
            client_id="wreath-app",
            ttl=float("inf"),
        )


@pytest.mark.parametrize("now", [99.0, float("nan"), float("-inf")])
def test_oidc_state_refuses_an_invalid_or_rewound_clock(now: float) -> None:
    party = _party()
    flow = party.begin_login(organization="acme", session_id="s1", now=100.0)

    with pytest.raises(SsoRefusal, match="clock") as raised:
        party.consume_state(flow.state, session_id="s1", now=now)

    assert raised.value.reason == "invalid-time"


def test_oidc_pending_state_has_a_hard_capacity_limit() -> None:
    party = OidcRelyingParty(issuer="https://idp.example", client_id="wreath-app", max_pending=1)
    party.begin_login(organization="acme", session_id="s1")

    with pytest.raises(SsoRefusal, match="ceiling") as raised:
        party.begin_login(organization="acme", session_id="s2")

    assert raised.value.reason == "pending-capacity"


def test_oidc_pending_state_sweeps_only_when_expiry_is_possible() -> None:
    party = OidcRelyingParty(
        issuer="https://idp.example",
        client_id="wreath-app",
        ttl=10,
        max_pending=10,
    )
    first = party.begin_login(organization="acme", session_id="s1", now=100)
    party.begin_login(organization="acme", session_id="s2", now=109)

    assert party._next_sweep == 110
    party.begin_login(organization="acme", session_id="s3", now=110)
    assert party._flows.held(first.state) is first

    party.begin_login(organization="acme", session_id="s4", now=111)
    assert party._flows.held(first.state) is None
    assert party._next_sweep == 119


def test_a_state_this_application_never_issued_is_refused() -> None:
    with pytest.raises(SsoRefusal, match="did not issue"):
        _party().consume_state("fabricated", session_id="s1")


def test_the_flow_remembers_which_organisation_it_began_for() -> None:
    party = _party()
    flow = party.begin_login(organization="globex", session_id="s1")
    assert party.consume_state(flow.state, session_id="s1").organization == "globex"


def test_an_id_token_with_the_wrong_nonce_is_refused() -> None:
    with pytest.raises(SsoRefusal, match="does not match") as raised:
        _party().check_nonce({"nonce": "other"}, expected_nonce="mine")
    assert raised.value.reason == "nonce-mismatch"


def test_an_id_token_with_no_nonce_at_all_is_refused() -> None:
    with pytest.raises(SsoRefusal, match="nonce"):
        _party().check_nonce({}, expected_nonce="mine")


def test_the_matching_nonce_is_accepted() -> None:
    _party().check_nonce({"nonce": "mine"}, expected_nonce="mine")


def test_the_relying_party_never_fetches_on_the_request_path() -> None:
    assert _party().fetches_on_request_path is False


def test_an_unknown_key_id_is_unverified_rather_than_fetched() -> None:
    with pytest.raises(SsoRefusal, match="never fetched on the request path") as raised:
        _party().key_for("kid-nobody-loaded")
    assert raised.value.reason == "unknown-key"


async def test_refresh_loads_keys_from_discovery_and_is_the_only_loader() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> dict:
        calls.append(url)
        if url.endswith("openid-configuration"):
            return {"jwks_uri": "https://idp.example/jwks"}
        return {"keys": [{"kid": "k1"}, {"kid": "k2"}]}

    party = _party()
    assert await party.refresh(fetch) == 2
    assert party.key_for("k1") == {"kid": "k1"}
    assert calls == [
        "https://idp.example/.well-known/openid-configuration",
        "https://idp.example/jwks",
    ]


async def test_refresh_refuses_a_jwks_uri_off_the_issuer_origin() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> dict:
        calls.append(url)
        return {"jwks_uri": "http://127.0.0.1/internal"}

    with pytest.raises(SsoRefusal, match="issuer origin"):
        await _party().refresh(fetch)
    assert calls == ["https://idp.example/.well-known/openid-configuration"]


async def test_refresh_refuses_credentials_in_a_discovered_jwks_uri() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> dict:
        calls.append(url)
        return {"jwks_uri": "https://user:password@idp.example/jwks"}

    with pytest.raises(SsoRefusal, match="issuer origin"):
        await _party().refresh(fetch)
    assert calls == ["https://idp.example/.well-known/openid-configuration"]
