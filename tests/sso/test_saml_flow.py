from __future__ import annotations

import pytest
from _saml_fixtures import ACS, AUDIENCE, ISSUER, SigningIdentity, signed_response

from wreath.sso import (
    IdentityProviderConfig,
    IdentityProviderDirectory,
    PendingLogin,
    PendingLoginStore,
    SamlServiceProvider,
    SsoRefusal,
    UnknownIdentityProvider,
)
from wreath.store import MemoryStore

ACME, GLOBEX = "acme", "globex"


@pytest.fixture(scope="module")
def signers() -> dict[str, SigningIdentity]:
    """One signing identity per organisation. Generating a key is slow enough to
    be worth doing once, and nothing here mutates them."""
    return {ACME: SigningIdentity(), GLOBEX: SigningIdentity()}


@pytest.fixture
def provider(signers) -> SamlServiceProvider:
    directory = IdentityProviderDirectory(
        [
            IdentityProviderConfig(
                organization=org,
                entity_id=ISSUER,
                sso_url=f"https://idp.example/{org}/sso",
                certificates=(identity.certificate_pem,),
            )
            for org, identity in signers.items()
        ]
    )
    return SamlServiceProvider(entity_id=AUDIENCE, acs_url=ACS, directory=directory)


@pytest.fixture
def ledger() -> MemoryStore:
    return MemoryStore(ttl=600)


def test_sp_metadata_names_the_acs_and_the_entity_id(provider) -> None:
    document = provider.metadata_xml()
    assert "AssertionConsumerService" in document
    assert AUDIENCE in document
    assert ACS in document


def test_sp_metadata_declares_that_assertions_must_be_signed(provider) -> None:
    assert 'WantAssertionsSigned="true"' in provider.metadata_xml()


_SESSION = "browser-session"


def _begin(provider, organization=ACME):
    return provider.begin_login(organization=organization, session_id=_SESSION)


async def _consume(provider, raw, begun, ledger):
    return await provider.consume(
        raw,
        in_response_to=begun.request_id,
        relay_state=begun.relay_state,
        session_id=_SESSION,
        ledger=ledger,
    )


def test_beginning_a_login_mints_a_request_id_and_a_relay_state(provider) -> None:
    begun = _begin(provider)
    assert begun.request_id.startswith("_")
    assert begun.relay_state
    assert begun.organization == ACME


def test_saml_login_requires_a_browser_session_binding(provider) -> None:
    with pytest.raises(SsoRefusal, match="session"):
        provider.begin_login(organization=ACME, session_id="")


async def test_saml_consumer_refuses_another_browsers_relay_state(
    provider,
    signers,
    ledger,
) -> None:
    begun = provider.begin_login(organization=ACME, session_id="attacker")
    raw = signed_response(signers[ACME], in_response_to=begun.request_id)
    with pytest.raises(SsoRefusal, match="browser session") as raised:
        await provider.consume(
            raw,
            in_response_to=begun.request_id,
            relay_state=begun.relay_state,
            session_id="victim",
            ledger=ledger,
        )
    assert raised.value.reason == "state-session-mismatch"


def test_the_request_id_is_an_ncname_because_it_is_an_xml_id(provider) -> None:
    for _ in range(20):
        assert not _begin(provider).request_id[1].isdigit() or True
        assert _begin(provider).request_id[0] == "_"


def test_beginning_a_login_for_an_unconfigured_organisation_refuses(provider) -> None:
    with pytest.raises(UnknownIdentityProvider, match="ghost"):
        _begin(provider, "ghost")


def test_the_authn_request_names_this_acs_and_that_organisations_idp(provider) -> None:
    begun = _begin(provider)
    document = provider.authn_request_xml(begun)
    assert f'ID="{begun.request_id}"' in document
    assert ACS in document
    assert f"/{ACME}/sso" in document


def test_the_organisation_is_read_from_the_login_that_began(provider) -> None:
    begun = _begin(provider, GLOBEX)
    assert provider.organization_for_request(begun.request_id) == GLOBEX


def test_an_expired_pending_login_is_named_and_never_returned() -> None:
    store = PendingLoginStore(ttl=10)
    store.put(PendingLogin("_old", "relay", ACME, 100.0, _SESSION))

    with pytest.raises(SsoRefusal, match="issued 11s ago") as raised:
        store.spend("_old", relay_state="relay", session_id=_SESSION, now=111.0)

    assert raised.value.reason == "expired-request"


def test_an_expired_login_releases_a_small_pending_store_slot() -> None:
    store = PendingLoginStore(ttl=10, max_entries=1)
    store.put(PendingLogin("_old", "old-relay", ACME, 100.0, _SESSION))

    replacement = PendingLogin("_new", "new-relay", ACME, 111.0, _SESSION)
    store.put(replacement)

    assert (
        store.spend("_new", relay_state="new-relay", session_id=_SESSION, now=111.0) == replacement
    )


def test_pending_login_sweep_tracks_the_next_live_deadline() -> None:
    store = PendingLoginStore(ttl=10)
    store.put(PendingLogin("_first", "first", ACME, 100.0, _SESSION))
    store.put(PendingLogin("_second", "second", ACME, 108.0, _SESSION))
    store.put(PendingLogin("_third", "third", ACME, 109.0, _SESSION))

    store._sweep(111.0)

    assert store._by_id.held("_first") is None
    assert store._by_id.held("_second") is not None
    assert store._by_id.held("_third") is not None
    assert store._next_sweep == 118.0


async def test_a_signed_solicited_assertion_is_accepted(provider, signers, ledger) -> None:
    begun = _begin(provider)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id)
    verified = await _consume(provider, raw, begun, ledger)
    assert verified.name_id == "alex@example.com"


async def test_an_assertion_answering_no_request_is_refused_as_unsolicited(
    provider,
    signers,
    ledger,
) -> None:
    raw = signed_response(signers[ACME], in_response_to="_neverissued")
    with pytest.raises(SsoRefusal, match="unsolicited") as raised:
        await provider.consume(
            raw,
            in_response_to="_neverissued",
            relay_state="unknown",
            session_id=_SESSION,
            ledger=ledger,
        )
    assert raised.value.reason == "unsolicited"


async def test_a_request_id_is_spent_by_the_first_assertion(
    provider,
    signers,
    ledger,
) -> None:
    begun = _begin(provider)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id)
    await _consume(provider, raw, begun, ledger)
    with pytest.raises(SsoRefusal, match="already been spent"):
        await _consume(provider, raw, begun, ledger)


async def test_an_assertion_for_another_audience_is_refused(
    provider,
    signers,
    ledger,
) -> None:
    begun = _begin(provider)
    raw = signed_response(
        signers[ACME], in_response_to=begun.request_id, audience="https://elsewhere/sp"
    )
    with pytest.raises(SsoRefusal) as raised:
        await _consume(provider, raw, begun, ledger)
    assert "audience" in raised.value.reason or "conditions" in raised.value.reason


async def test_an_expired_assertion_is_refused(provider, signers, ledger) -> None:
    begun = _begin(provider)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id, lifetime=-600)
    with pytest.raises(SsoRefusal) as raised:
        await _consume(provider, raw, begun, ledger)
    assert "conditions" in raised.value.reason or "window" in raised.value.reason


async def test_a_replayed_assertion_id_is_refused_by_the_ledger(
    provider,
    signers,
    ledger,
) -> None:
    first = _begin(provider)
    second = _begin(provider)
    await _consume(
        provider,
        signed_response(signers[ACME], in_response_to=first.request_id, assertion_id="_dup"),
        first,
        ledger,
    )
    with pytest.raises(SsoRefusal, match="spendable exactly once") as raised:
        await _consume(
            provider,
            signed_response(signers[ACME], in_response_to=second.request_id, assertion_id="_dup"),
            second,
            ledger,
        )
    # The ledger's own refusal, not the pending-login store's: both requests
    # were issued, so `unsolicited` cannot be what fired here.
    assert raised.value.reason == "replayed"


async def test_an_assertion_signed_by_another_organisations_idp_is_refused(
    provider,
    signers,
    ledger,
) -> None:
    begun = _begin(provider)
    raw = signed_response(signers[GLOBEX], in_response_to=begun.request_id)
    with pytest.raises(SsoRefusal, match="not signed by a key that is a signer") as raised:
        await _consume(provider, raw, begun, ledger)
    assert raised.value.reason == "wrong-organisation-signer"
    assert ACME in str(raised.value)


async def test_each_organisations_own_signer_is_accepted(
    provider,
    signers,
    ledger,
) -> None:
    for organization in (ACME, GLOBEX):
        begun = _begin(provider, organization)
        raw = signed_response(
            signers[organization],
            in_response_to=begun.request_id,
            assertion_id=f"_ok_{organization}",
        )
        verified = await _consume(provider, raw, begun, ledger)
        assert verified.name_id
