"""The OIDC relying-party flow: PKCE, state, nonce, and no request-path fetch.

Migrated from `tests/thesis/test_sso_contract.py`.
"""

from __future__ import annotations

import pytest

from wreath.sso import OidcRelyingParty, SsoRefusal


def _party() -> OidcRelyingParty:
    return OidcRelyingParty(issuer="https://idp.example", client_id="wreath-app")


def test_the_plain_pkce_method_is_refused() -> None:
    """`plain` sends the verifier *as* the challenge, so it protects nothing.

    Refused at construction: a relying party that accepts it has a downgrade
    nobody would notice, because the flow still completes.
    """
    with pytest.raises(SsoRefusal, match="protects nothing") as raised:
        OidcRelyingParty(issuer="https://idp.example", client_id="x", pkce="plain")
    assert raised.value.reason == "weak-pkce"


def test_beginning_a_login_mints_state_nonce_and_an_s256_challenge() -> None:
    """Three single-use values, each defending a different thing."""
    flow = _party().begin_login(organization="acme", session_id="s1")
    assert flow.state and flow.nonce and flow.verifier
    assert flow.challenge != flow.verifier
    # base64url, unpadded, as RFC 7636 requires.
    assert "=" not in flow.challenge


def test_the_challenge_is_the_sha256_of_the_verifier() -> None:
    """Asserted against the definition rather than against itself: a challenge
    computed some other way would still be "not the verifier"."""
    import hashlib
    from base64 import urlsafe_b64encode

    flow = _party().begin_login(organization="acme")
    expected = urlsafe_b64encode(
        hashlib.sha256(flow.verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert flow.challenge == expected


def test_two_logins_never_share_a_state() -> None:
    party = _party()
    states = {party.begin_login(organization="acme").state for _ in range(50)}
    assert len(states) == 50


def test_the_state_is_single_use() -> None:
    """A state that is not single-use is CSRF on the login endpoint."""
    party = _party()
    flow = party.begin_login(organization="acme", session_id="s1")
    party.consume_state(flow.state, session_id="s1")
    with pytest.raises(SsoRefusal, match="already spent") as raised:
        party.consume_state(flow.state, session_id="s1")
    assert raised.value.reason == "unknown-state"


def test_a_state_from_another_browser_session_is_refused() -> None:
    """**The login-CSRF this binding exists for.**

    An attacker's authorization code, redeemed in a victim's browser, signs the
    victim into the *attacker's* account -- where everything they then do is
    visible to the attacker. Binding state to the session that began the flow is
    what makes the redemption fail.
    """
    party = _party()
    flow = party.begin_login(organization="acme", session_id="victim")
    with pytest.raises(SsoRefusal, match="different browser session") as raised:
        party.consume_state(flow.state, session_id="attacker")
    assert raised.value.reason == "state-session-mismatch"


def test_a_state_this_application_never_issued_is_refused() -> None:
    with pytest.raises(SsoRefusal, match="did not issue"):
        _party().consume_state("fabricated", session_id="s1")


def test_the_flow_remembers_which_organisation_it_began_for() -> None:
    """As in the SAML half: the trust anchor comes from the request, never from
    the document answering it."""
    party = _party()
    flow = party.begin_login(organization="globex", session_id="s1")
    assert party.consume_state(flow.state, session_id="s1").organization == "globex"


def test_an_id_token_with_the_wrong_nonce_is_refused() -> None:
    """The nonce binds the token to *this* authorization request."""
    with pytest.raises(SsoRefusal, match="does not match") as raised:
        _party().check_nonce({"nonce": "other"}, expected_nonce="mine")
    assert raised.value.reason == "nonce-mismatch"


def test_an_id_token_with_no_nonce_at_all_is_refused() -> None:
    """A missing claim must not compare equal to a missing expectation."""
    with pytest.raises(SsoRefusal, match="nonce"):
        _party().check_nonce({}, expected_nonce="mine")


def test_the_matching_nonce_is_accepted() -> None:
    _party().check_nonce({"nonce": "mine"}, expected_nonce="mine")


# --- nothing on the request path --------------------------------------------


def test_the_relying_party_never_fetches_on_the_request_path() -> None:
    """The rule `wreath.signatures` already holds, for the same two reasons.

    A key fetch driven by a request lets an unauthenticated caller aim an
    outbound request at a host they chose, and it puts the identity provider's
    outage in front of every login rather than in front of the refresh.
    """
    assert _party().fetches_on_request_path is False


def test_an_unknown_key_id_is_unverified_rather_than_fetched() -> None:
    """Stated as behaviour, not only as the flag above.

    The tempting implementation refreshes JWKS when it meets a `kid` it does not
    know -- which is exactly the request-path fetch, wearing a reason.
    """
    with pytest.raises(SsoRefusal, match="never fetched on the request path") as raised:
        _party().key_for("kid-nobody-loaded")
    assert raised.value.reason == "unknown-key"


async def test_refresh_loads_keys_from_discovery_and_is_the_only_loader() -> None:
    """The startup path, with the fetch injected so no socket is opened here."""
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
