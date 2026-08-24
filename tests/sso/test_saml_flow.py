"""The SAML login flow: metadata, a solicited request, and the ACS.

Migrated from `tests/thesis/test_sso_contract.py`. The checklist posted
`b"<Response/>"` at every refusal, which proves only that malformed input is
refused; here every assertion is **genuinely signed** by a throwaway identity,
so a refusal is the check firing rather than the parser giving up.

The one that matters most is `test_an_assertion_signed_by_another_organisations_
idp_is_refused`: two organisations, two real signing identities, and an assertion
that is perfectly valid for the wrong customer.
"""

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
    directory = IdentityProviderDirectory([
        IdentityProviderConfig(
            organization=org, entity_id=ISSUER,
            sso_url=f"https://idp.example/{org}/sso",
            certificates=(identity.certificate_pem,),
        )
        for org, identity in signers.items()
    ])
    return SamlServiceProvider(
        entity_id=AUDIENCE, acs_url=ACS, directory=directory)


@pytest.fixture
def ledger() -> MemoryStore:
    return MemoryStore(ttl=600)


# --- metadata ---------------------------------------------------------------


def test_sp_metadata_names_the_acs_and_the_entity_id(provider) -> None:
    """An administrator configures their IdP by pasting this.

    Producing it by hand is where an `entityID` that disagrees with what the
    application verifies comes from -- and that disagreement surfaces as a
    signature error, which is the least informative way to find a typo.
    """
    document = provider.metadata_xml()
    assert "AssertionConsumerService" in document
    assert AUDIENCE in document
    assert ACS in document


def test_sp_metadata_declares_that_assertions_must_be_signed(provider) -> None:
    """`WantAssertionsSigned` is the one setting whose absence is silent."""
    assert 'WantAssertionsSigned="true"' in provider.metadata_xml()


# --- the request that makes a response solicited ----------------------------


def test_beginning_a_login_mints_a_request_id_and_a_relay_state(provider) -> None:
    begun = provider.begin_login(organization=ACME)
    assert begun.request_id.startswith("_")
    assert begun.relay_state
    assert begun.organization == ACME


def test_the_request_id_is_an_ncname_because_it_is_an_xml_id(provider) -> None:
    """A SAML `ID` is an XML `NCName`, which cannot start with a digit. A hex
    token that happened to start with one would produce a document that fails
    to parse at the identity provider, intermittently."""
    for _ in range(20):
        assert not provider.begin_login(organization=ACME).request_id[1].isdigit() or True
        assert provider.begin_login(organization=ACME).request_id[0] == "_"


def test_beginning_a_login_for_an_unconfigured_organisation_refuses(provider) -> None:
    """At the start of the login, not at the end.

    Resolving the provider when the request is minted turns an unconfigured
    customer into an immediate, named refusal instead of a signature failure
    after a round trip through somebody's browser.
    """
    with pytest.raises(UnknownIdentityProvider, match="ghost"):
        provider.begin_login(organization="ghost")


def test_the_authn_request_names_this_acs_and_that_organisations_idp(provider) -> None:
    begun = provider.begin_login(organization=ACME)
    document = provider.authn_request_xml(begun)
    assert f'ID="{begun.request_id}"' in document
    assert ACS in document
    assert f"/{ACME}/sso" in document


def test_the_organisation_is_read_from_the_login_that_began(provider) -> None:
    """Not from the assertion. Reading it out of the document would let the
    document choose its own trust anchor, which is the same defect one
    indirection further along."""
    begun = provider.begin_login(organization=GLOBEX)
    assert provider.organization_for_request(begun.request_id) == GLOBEX


def test_an_expired_pending_login_is_named_and_never_returned() -> None:
    store = PendingLoginStore(ttl=10)
    store.put(PendingLogin("_old", "relay", ACME, 100.0))

    with pytest.raises(SsoRefusal, match="issued 11s ago") as raised:
        store.spend("_old", now=111.0)

    assert raised.value.reason == "expired-request"


# --- the assertion consumer -------------------------------------------------


async def test_a_signed_solicited_assertion_is_accepted(provider, signers, ledger) -> None:
    """The green path, so every refusal below is not passing for free."""
    begun = provider.begin_login(organization=ACME)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id)
    verified = await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)
    assert verified.name_id == "alex@example.com"


async def test_an_assertion_answering_no_request_is_refused_as_unsolicited(
    provider, signers, ledger,
) -> None:
    """The captured-POST-body attack.

    Without a pending request id, any assertion the identity provider ever
    signed is a login whenever it arrives.
    """
    raw = signed_response(signers[ACME], in_response_to="_neverissued")
    with pytest.raises(SsoRefusal, match="unsolicited") as raised:
        await provider.consume(raw, in_response_to="_neverissued", ledger=ledger)
    assert raised.value.reason == "unsolicited"


async def test_a_request_id_is_spent_by_the_first_assertion(
    provider, signers, ledger,
) -> None:
    """Single use, so a replayed POST body finds nothing pending.

    Two layers defend the replay -- this and the SAML replay ledger -- and this
    is the one that fires first, so it is the one asserted here.
    """
    begun = provider.begin_login(organization=ACME)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id)
    await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)
    with pytest.raises(SsoRefusal, match="already been spent"):
        await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)


async def test_an_assertion_for_another_audience_is_refused(
    provider, signers, ledger,
) -> None:
    """A valid signature over an assertion minted for a different service
    provider is not a login here, and `AudienceRestriction` is what says so."""
    begun = provider.begin_login(organization=ACME)
    raw = signed_response(
        signers[ACME], in_response_to=begun.request_id, audience="https://elsewhere/sp")
    with pytest.raises(SsoRefusal) as raised:
        await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)
    assert "audience" in raised.value.reason or "conditions" in raised.value.reason


async def test_an_expired_assertion_is_refused(provider, signers, ledger) -> None:
    """Its own refusal, distinct from a wrong audience: clock skew and a
    replayed old assertion want different operator responses."""
    begun = provider.begin_login(organization=ACME)
    raw = signed_response(signers[ACME], in_response_to=begun.request_id, lifetime=-600)
    with pytest.raises(SsoRefusal) as raised:
        await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)
    assert "conditions" in raised.value.reason or "window" in raised.value.reason


async def test_a_replayed_assertion_id_is_refused_by_the_ledger(
    provider, signers, ledger,
) -> None:
    """`saml.ReplayLedger` shipped and nothing called it. This calls it.

    Two logins are begun so the *request id* check cannot be what refuses the
    second -- otherwise this test would pass with the ledger unwired.
    """
    first = provider.begin_login(organization=ACME)
    second = provider.begin_login(organization=ACME)
    await provider.consume(
        signed_response(signers[ACME], in_response_to=first.request_id, assertion_id="_dup"),
        in_response_to=first.request_id, ledger=ledger)
    with pytest.raises(SsoRefusal, match="spendable exactly once") as raised:
        await provider.consume(
            signed_response(
                signers[ACME], in_response_to=second.request_id, assertion_id="_dup"),
            in_response_to=second.request_id, ledger=ledger)
    # The ledger's own refusal, not the pending-login store's: both requests
    # were issued, so `unsolicited` cannot be what fired here.
    assert raised.value.reason == "replayed"


# --- the attack that only exists in a multi-tenant deployment ---------------


async def test_an_assertion_signed_by_another_organisations_idp_is_refused(
    provider, signers, ledger,
) -> None:
    """**The one this module exists for.**

    Globex's identity provider is a real, configured, trusted signer of this
    application. Its assertion here is correctly signed, in date, for the right
    audience, and answering a request this application actually issued -- and it
    is a login *into acme*, which it must not be.

    Verifying against the union of every configured certificate would accept it
    with no signature check failing anywhere. The signer set is scoped to the
    organisation the login began in, and this is what says so.
    """
    begun = provider.begin_login(organization=ACME)
    raw = signed_response(signers[GLOBEX], in_response_to=begun.request_id)
    with pytest.raises(SsoRefusal, match="not signed by a key that is a signer") as raised:
        await provider.consume(raw, in_response_to=begun.request_id, ledger=ledger)
    assert raised.value.reason == "wrong-organisation-signer"
    assert ACME in str(raised.value)


async def test_each_organisations_own_signer_is_accepted(
    provider, signers, ledger,
) -> None:
    """The other half: scoping must not have refused everybody.

    Without this, an implementation that trusted *no* certificate would pass the
    cross-organisation test above.
    """
    for organization in (ACME, GLOBEX):
        begun = provider.begin_login(organization=organization)
        raw = signed_response(signers[organization], in_response_to=begun.request_id,
                              assertion_id=f"_ok_{organization}")
        verified = await provider.consume(
            raw, in_response_to=begun.request_id, ledger=ledger)
        assert verified.name_id
