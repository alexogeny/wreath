"""Organisations, memberships and invitations, at the store.

The identity-layer half of tenancy. Two properties carry the weight: a role name
is meaningless without the organisation it applies in, and an invitation must
survive the invitee having no account yet.
"""

from __future__ import annotations

import pytest

from wreath.organizations import (
    InMemoryOrganizationStore,
    Invitation,
    Membership,
    Organization,
)

ROLES = frozenset({"admin", "member", "billing"})


def _store() -> InMemoryOrganizationStore:
    return InMemoryOrganizationStore(roles=ROLES)


# --- the id rule -------------------------------------------------------------


def test_an_organization_id_may_not_contain_a_colon() -> None:
    """A colon in an id would let one tenant's id spell another tenant's role.

    Roles are namespaced `"<org>:<role>"`, so an organisation literally named
    `"acme:admin"` produces the qualified role `"acme:admin:member"` -- but also
    collides with `acme`'s admin in any policy doing prefix work. Refused at the
    only place it can be: construction.
    """
    with pytest.raises(ValueError, match="must not contain ':'"):
        Organization(id="acme:evil")


def test_an_organization_id_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Organization(id="")


# --- membership --------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_user_two_organizations_with_different_roles() -> None:
    """The case a single-tenant model cannot express, and the leak it invites."""
    store = _store()
    await store.add_member("acme", "alice", roles={"admin"})
    await store.add_member("globex", "alice", roles={"member"})

    memberships = await store.memberships("alice")
    assert memberships == (
        Membership("acme", "alice", frozenset({"admin"})),
        Membership("globex", "alice", frozenset({"member"})),
    )

    qualified = set()
    for membership in memberships:
        qualified |= membership.qualified_roles()
    assert qualified == {"acme:admin", "globex:member"}
    assert "globex:admin" not in qualified, (
        "an admin of one organization became an admin of another"
    )


@pytest.mark.asyncio
async def test_an_undeclared_role_is_refused() -> None:
    """Roles are a declared vocabulary; that is what makes startup validation
    of a policy naming one possible at all."""
    store = _store()
    with pytest.raises(ValueError, match="unknown role"):
        await store.add_member("acme", "alice", roles={"amdin"})


def test_a_store_with_no_declared_roles_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty role vocabulary"):
        InMemoryOrganizationStore(roles=())


@pytest.mark.asyncio
async def test_adding_a_member_replaces_their_roles_rather_than_adding() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"admin", "billing"})
    await store.add_member("acme", "alice", roles={"member"})
    memberships = await store.memberships("alice")
    assert memberships[0].roles == frozenset({"member"}), (
        "roles accumulated; a demotion would not have taken effect"
    )


@pytest.mark.asyncio
async def test_removing_a_membership_reports_whether_there_was_one() -> None:
    store = _store()
    await store.add_member("acme", "alice", roles={"member"})
    assert await store.remove_member("acme", "alice") is True
    assert await store.remove_member("acme", "alice") is False
    assert await store.memberships("alice") == ()


@pytest.mark.asyncio
async def test_a_user_with_no_memberships_resolves_to_nothing() -> None:
    assert await _store().memberships("nobody") == ()


@pytest.mark.asyncio
async def test_adding_a_member_creates_the_organization_implicitly() -> None:
    store = _store()
    await store.add_member("fresh", "alice", roles={"admin"})
    assert await store.organization("fresh") == Organization(id="fresh")


@pytest.mark.asyncio
async def test_creating_a_duplicate_organization_is_refused() -> None:
    store = _store()
    await store.create(Organization(id="acme", name="Acme"))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(Organization(id="acme"))


# --- invitations -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invitation_survives_the_invitee_having_no_account() -> None:
    """The whole reason this is a record rather than a call."""
    store = _store()
    invitation = await store.invite("acme", "new@example.com", roles={"member"})

    assert isinstance(invitation, Invitation)
    assert invitation.accepted_by is None
    assert await store.memberships("later-user-id") == ()

    membership = await store.accept(invitation.token, "later-user-id")
    assert membership == Membership("acme", "later-user-id", frozenset({"member"}))


@pytest.mark.asyncio
async def test_an_invitation_is_single_use() -> None:
    store = _store()
    invitation = await store.invite("acme", "new@example.com", roles={"member"})
    await store.accept(invitation.token, "alice")

    with pytest.raises(ValueError) as caught:
        await store.accept(invitation.token, "mallory")
    # The distinct text, not merely that it refused: every refusal here would
    # otherwise mention the invitation, so asserting on that would pass on
    # whichever branch fired, including the fallthrough.
    assert str(caught.value) == "invitation has already been accepted"


@pytest.mark.asyncio
async def test_an_expired_invitation_is_refused_distinctly() -> None:
    store = _store()
    invitation = await store.invite(
        "acme", "new@example.com", roles={"member"}, ttl=60, now=1000.0
    )
    with pytest.raises(ValueError) as caught:
        await store.accept(invitation.token, "alice", now=1061.0)
    assert str(caught.value) == "invitation has expired"


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused_distinctly() -> None:
    store = _store()
    with pytest.raises(ValueError) as caught:
        await store.accept("not-a-token", "alice")
    assert str(caught.value) == "no such invitation"


@pytest.mark.asyncio
async def test_an_invitation_may_not_grant_an_undeclared_role() -> None:
    store = _store()
    with pytest.raises(ValueError, match="unknown role"):
        await store.invite("acme", "new@example.com", roles={"root"})


@pytest.mark.asyncio
async def test_invitations_are_listed_per_organization() -> None:
    store = _store()
    await store.invite("acme", "a@example.com")
    await store.invite("globex", "b@example.com")
    acme = await store.invitations("acme")
    assert [invitation.email for invitation in acme] == ["a@example.com"]


@pytest.mark.asyncio
async def test_the_synchronous_read_agrees_with_the_asynchronous_one() -> None:
    """`Memberships` reads the sync half on the request path; a divergence
    between the two would be an authorization answer that depends on which
    method happened to be called."""
    store = _store()
    await store.add_member("acme", "alice", roles={"admin"})
    assert store.memberships_for("alice") == await store.memberships("alice")


# --- cases the mutation sweep named ------------------------------------------


@pytest.mark.asyncio
async def test_an_unexpired_invitation_is_accepted() -> None:
    """The half of expiry that fails *open* if the comparison breaks.

    Dropping `now >= expires_at` leaves `expires_at is not None`, so every timed
    invitation reads as expired -- which refuses, and therefore looks correct to
    every test that only checks refusal.
    """
    store = _store()
    invitation = await store.invite("acme", "a@example.com", roles={"member"}, ttl=60, now=1000.0)
    membership = await store.accept(invitation.token, "alice", now=1059.0)
    assert membership.roles == frozenset({"member"})


@pytest.mark.asyncio
async def test_expiry_is_inclusive_at_the_boundary() -> None:
    store = _store()
    invitation = await store.invite("acme", "a@example.com", ttl=60, now=1000.0)
    with pytest.raises(ValueError, match="expired"):
        await store.accept(invitation.token, "alice", now=1060.0)


@pytest.mark.asyncio
async def test_a_token_matches_only_its_own_invitation() -> None:
    """With several invitations outstanding, the comparison must select one.

    A guard that always fires would hand the *first* invitation to whoever
    presented any token -- cross-organisation membership by typo. The earlier
    unknown-token test could not catch it, because with no invitations issued
    the loop never runs.
    """
    store = _store()
    await store.invite("acme", "a@example.com", roles={"admin"})
    second = await store.invite("globex", "b@example.com", roles={"member"})

    membership = await store.accept(second.token, "alice")
    assert membership.organization == "globex", (
        "presenting one token accepted a different invitation"
    )
    assert membership.roles == frozenset({"member"})

    remaining = await store.invitations("acme")
    assert remaining[0].accepted_by is None, "an unrelated invitation was consumed"


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused_with_invitations_outstanding() -> None:
    store = _store()
    await store.invite("acme", "a@example.com")
    with pytest.raises(ValueError) as caught:
        await store.accept("not-a-token", "alice")
    assert str(caught.value) == "no such invitation"


@pytest.mark.asyncio
async def test_invite_and_accept_default_their_clock_to_the_wall_clock() -> None:
    """`now=` is injectable for tests; the default path must still work."""
    store = _store()
    invitation = await store.invite("acme", "a@example.com", roles={"member"}, ttl=3600)
    assert invitation.expires_at is not None
    membership = await store.accept(invitation.token, "alice")
    assert membership.organization == "acme"
