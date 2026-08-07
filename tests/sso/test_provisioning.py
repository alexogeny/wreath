"""Turning a verified assertion into an account and a membership.

Migrated from `tests/thesis/test_sso_contract.py`.
"""

from __future__ import annotations

import pytest

from wreath.sso import AttributeMapping, JitProvisioning, SsoRefusal

# --- attribute mapping ------------------------------------------------------


def test_the_declared_attributes_are_read_out() -> None:
    mapping = AttributeMapping(email="mail", display_name="displayName")
    values = mapping.apply({"mail": "ana@acme.example", "displayName": "Ana"})
    assert values["email"] == "ana@acme.example"
    assert values["display_name"] == "Ana"


def test_an_undeclared_attribute_is_refused_rather_than_dropped() -> None:
    """Declaration, never inference.

    A heuristic that reads `email` and misses `mail` is confident and wrong, and
    the failure is one duplicate account per user per identity provider. So an
    attribute nobody mapped is a configuration question, and answering it
    silently is how it stays unanswered.
    """
    mapping = AttributeMapping(email="mail")
    with pytest.raises(SsoRefusal, match="department") as raised:
        mapping.apply({"mail": "ana@acme.example", "department": "eng"})
    assert raised.value.reason == "undeclared-attribute"


def test_a_missing_email_is_its_own_refusal() -> None:
    """Distinct from the undeclared case: one is a directory sending too much
    and the other too little, and they are fixed in different places."""
    with pytest.raises(SsoRefusal, match="identifies an account by email") as raised:
        AttributeMapping(email="mail").apply({})
    assert raised.value.reason == "missing-attribute"


# --- provisioning -----------------------------------------------------------


def test_provisioning_creates_the_account_and_the_membership_together() -> None:
    """An account with no membership sees nothing and reads as a bug in login;
    a membership with no account cannot be signed in."""
    result = JitProvisioning(roles=("member",)).provision(
        organization="acme", email="ana@acme.example")
    assert result.user_id
    assert result.membership == ("member",)
    assert result.created is True


def test_provisioning_adopts_an_existing_account_rather_than_duplicating() -> None:
    """The choice `scim_router`'s `POST /Users` already makes.

    Somebody who signed up with a password before their company bought SSO must
    keep their data.
    """
    provisioning = JitProvisioning(roles=("member",))
    first = provisioning.provision(organization="acme", email="ana@acme.example")
    second = provisioning.provision(organization="acme", email="ana@acme.example")
    assert first.user_id == second.user_id
    assert second.created is False


def test_an_email_is_matched_case_and_whitespace_insensitively() -> None:
    """Directories are inconsistent about both, and each inconsistency is one
    duplicate account for the same person."""
    provisioning = JitProvisioning(roles=("member",))
    first = provisioning.provision(organization="acme", email="Ana@Acme.Example")
    second = provisioning.provision(organization="acme", email=" ana@acme.example ")
    assert first.user_id == second.user_id


def test_the_same_email_in_two_organisations_is_two_accounts() -> None:
    """A consultant at two customers is two memberships, and conflating them
    would let one customer's directory reach the other's account."""
    provisioning = JitProvisioning(roles=("member",))
    acme = provisioning.provision(organization="acme", email="ana@example.com")
    globex = provisioning.provision(organization="globex", email="ana@example.com")
    assert acme.user_id != globex.user_id


def test_a_role_outside_the_declared_vocabulary_is_refused_at_configuration() -> None:
    """An identity-provider attribute is whatever a customer's directory admin
    typed, so it must not be able to name a role this application never
    declared. Checked when the provisioning is described rather than on
    somebody's first login, because configuration that can only fail then is
    configuration nobody tested.
    """
    with pytest.raises(SsoRefusal, match="not in the declared role vocabulary") as raised:
        JitProvisioning(roles=("owner",), vocabulary=("member", "admin"))
    assert raised.value.reason == "role-outside-vocabulary"


def test_a_declared_role_is_accepted() -> None:
    """The other half, so the refusal above is not passing for free."""
    assert JitProvisioning(roles=("admin",), vocabulary=("member", "admin"))


def test_an_organisation_requiring_a_second_factor_yields_a_pending_session() -> None:
    """Composes with what ships rather than bypassing it.

    `SessionIdentityBackend` already refuses to turn a pending login into an
    identity, so SSO produces a pending session and the existing second-factor
    routes finish it.
    """
    result = JitProvisioning(roles=("member",), require_second_factor=True).provision(
        organization="acme", email="ana@acme.example")
    assert result.session_state == "pending"


def test_without_a_second_factor_the_session_is_authenticated() -> None:
    result = JitProvisioning(roles=("member",)).provision(
        organization="acme", email="ana@acme.example")
    assert result.session_state == "authenticated"


def test_revoking_removes_the_account_so_a_later_login_is_a_new_one() -> None:
    """A revoked user holding a live cookie is the reason SSO was bought."""
    provisioning = JitProvisioning(roles=("member",))
    first = provisioning.provision(organization="acme", email="ana@acme.example")
    assert provisioning.revoke(organization="acme", email="ana@acme.example") == 1
    assert provisioning.revoke(organization="acme", email="ana@acme.example") == 0
    again = provisioning.provision(organization="acme", email="ana@acme.example")
    assert again.user_id != first.user_id
