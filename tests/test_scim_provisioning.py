"""SCIM 2.0 over `wreath.organizations`: the protocol, and the two things it must not become.

The two are the whole point of the suite:

* **No second membership model.** Every assertion about who is provisioned is
  made against `wreath.organizations` -- and the keystone test checks that a
  user SCIM provisioned is permitted by a *Cedar policy* reading
  `context.organizations`, with no SCIM code in the path at all.
* **No second authorization path.** Every route is refused or permitted by the
  application's own authorizer. There is a test for a principal the policy
  denies, and one for the router refusing to be built without an authorizer.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies
from wreath._scim.router import UNUSABLE_PASSWORD
from wreath._userkit import InMemoryUserStore, verify_password
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize
from wreath.organizations import (
    InMemoryOrganizationStore,
    Memberships,
    scim_router,
)
from wreath.testing import TestClient

ROLES = frozenset({"admin", "member", "billing"})

#: The directory's own principal may provision; nobody else may. Written as two
#: rules over the *organisation* resource, which is what `scim_router` asks
#: about, so the policy set is the only thing that decides.
POLICY = """
permit(principal == User::"directory", action == Action::"scim_read",
       resource == Organization::"acme");
permit(principal == User::"directory", action == Action::"scim_write",
       resource == Organization::"acme");
permit(principal, action == Action::"read", resource)
when { context.organizations.contains("acme") };
permit(principal, action == Action::"invite", resource)
when { context.org_roles.contains("acme:admin") };
"""

PATCH_BODY = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _identity_for(token: str, directory_identity: Identity) -> Identity | None:
    """`let-me-in` is the directory; `as:<id>` is whoever it provisioned.

    Two principals, because the interesting question is whether a user SCIM
    wrote reaches a route -- and the directory's own principal would answer that
    question about itself.
    """
    if token == "let-me-in":
        return directory_identity
    if token.startswith("as:"):
        return Identity(token[3:])
    return None


class Provisioned:
    """One application with SCIM mounted, plus the stores behind it."""

    def __init__(self, identity: Identity, **options: Any) -> None:
        self.users = InMemoryUserStore()
        self.organizations = InMemoryOrganizationStore(roles=ROLES)
        self.app = Wreath()
        self.app.configure_auth(
            BearerTokenBackend(lambda token: _identity_for(token, identity)),
            CedarAuthorizer(
                engine=CedarPolicies(POLICY),
                organizations=Memberships(self.organizations),
            ),
        )
        self.app.include_router(
            scim_router(
                self.app,
                users=self.users,
                organizations=self.organizations,
                organization="acme",
                **options,
            )
        )

        # A route outside SCIM, so a test can ask whether provisioning actually
        # granted anything -- through Cedar, with no SCIM code in the path.
        @self.app.get("/doc")
        @authorize(action="read", resource=lambda request: 'Doc::"d"')
        async def read_doc(request: Any) -> str:
            return "ok"

        @self.app.get("/invite")
        @authorize(action="invite", resource=lambda request: 'Doc::"d"')
        async def invite(request: Any) -> str:
            return "ok"

    def client(self) -> TestClient:
        return TestClient(self.app)


def directory() -> Provisioned:
    return Provisioned(Identity("directory"))


AUTH = {"authorization": "Bearer let-me-in"}
#: The provisioned user themselves, whose id `InMemoryUserStore` mints as "1".
AS_ALICE = {"authorization": "Bearer as:1"}


async def provision(client: TestClient, user_name: str, **extra: Any) -> Any:
    response = await client.post(
        "/scim/v2/Users", json={"userName": user_name, **extra}, headers=AUTH
    )
    return response


# --- discovery --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_content_type_is_scim_json() -> None:
    """RFC 7644 section 3.1. A directory content-negotiates on it."""
    async with directory().client() as client:
        response = await client.get("/scim/v2/ServiceProviderConfig", headers=AUTH)
    assert response.status == 200
    assert (b"content-type", b"application/scim+json") in response.headers


@pytest.mark.asyncio
async def test_the_service_provider_config_describes_this_implementation() -> None:
    async with directory().client() as client:
        body = (await client.get("/scim/v2/ServiceProviderConfig", headers=AUTH)).json()
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True
    assert body["sort"]["supported"] is True
    assert body["bulk"]["supported"] is False
    assert body["etag"]["supported"] is False
    assert body["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert body["meta"]["location"].endswith("/scim/v2/ServiceProviderConfig")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("challenge", "expected"),
    [
        ('Basic realm="scim"', "httpbasic"),
        ("Bearer", "oauthbearertoken"),
        # A backend that offers no challenge at all, and one whose challenge is
        # not a string: the report falls back rather than describing the
        # provider with whatever the backend happened to return.
        (None, "oauthbearertoken"),
        (7, "oauthbearertoken"),
    ],
)
async def test_the_authentication_scheme_follows_the_application_backend(
    challenge: Any, expected: str
) -> None:
    """Reported off the backend's own challenge, so it cannot contradict it."""

    class Backend:
        async def authenticate(self, request: Any) -> Identity:
            return Identity("directory")

    class Challenging(Backend):
        def challenge(self, request: Any) -> Any:
            return challenge

    fixture = directory()
    fixture.app.configure_auth(
        Backend() if challenge is None else Challenging(),
        CedarAuthorizer(
            engine=CedarPolicies(POLICY),
            organizations=Memberships(fixture.organizations),
        ),
    )
    async with fixture.client() as client:
        body = (await client.get("/scim/v2/ServiceProviderConfig", headers=AUTH)).json()
    assert body["authenticationSchemes"][0]["type"] == expected


@pytest.mark.asyncio
async def test_the_resource_types_are_user_and_group() -> None:
    async with directory().client() as client:
        body = (await client.get("/scim/v2/ResourceTypes", headers=AUTH)).json()
        one = await client.get("/scim/v2/ResourceTypes/Group", headers=AUTH)
        missing = await client.get("/scim/v2/ResourceTypes/Widget", headers=AUTH)
    assert [entry["id"] for entry in body["Resources"]] == ["User", "Group"]
    assert one.json()["endpoint"] == "/Groups"
    assert missing.status == 404


@pytest.mark.asyncio
async def test_the_published_schema_advertises_only_what_is_stored() -> None:
    """A schema promising `externalId` is a promise the next GET breaks."""
    async with directory().client() as client:
        body = (await client.get("/scim/v2/Schemas", headers=AUTH)).json()
        one = await client.get(
            "/scim/v2/Schemas/urn:ietf:params:scim:schemas:core:2.0:User", headers=AUTH
        )
    user = next(entry for entry in body["Resources"] if entry["name"] == "User")
    published = {attribute["name"] for attribute in user["attributes"]}
    assert published == {"userName", "active", "password", "emails", "groups"}
    assert "externalId" not in published
    assert "name" not in published
    assert one.status == 200
    assert one.json()["id"] == "urn:ietf:params:scim:schemas:core:2.0:User"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["User", "Group"])
async def test_a_schema_is_served_by_its_own_urn(name: str) -> None:
    """Each urn returns *its* schema, not whichever document came first."""
    urn = f"urn:ietf:params:scim:schemas:core:2.0:{name}"
    async with directory().client() as client:
        found = await client.get(f"/scim/v2/Schemas/{urn}", headers=AUTH)
        missing = await client.get("/scim/v2/Schemas/urn:made:up", headers=AUTH)
    assert found.json()["name"] == name
    assert found.json()["meta"]["location"].endswith(urn)
    assert missing.status == 404


# --- the single authorization path ------------------------------------------


@pytest.mark.asyncio
async def test_a_principal_the_policy_does_not_permit_is_refused() -> None:
    """No bespoke allow/deny: the policy set is the whole decision."""
    fixture = Provisioned(Identity("intruder"))
    async with fixture.client() as client:
        read = await client.get("/scim/v2/Users", headers=AUTH)
        write = await provision(client, "victim@example.com")
    assert read.status == 403
    assert write.status == 403
    assert await fixture.organizations.members("acme") == ()


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_is_challenged() -> None:
    async with directory().client() as client:
        response = await client.get("/scim/v2/Users")
    assert response.status == 401


@pytest.mark.asyncio
async def test_the_router_refuses_to_build_without_an_authorizer() -> None:
    """Half-wired, this would serve the directory to any authenticated caller."""
    app = Wreath()
    app.configure_auth(BearerTokenBackend(lambda token: Identity("x")))
    with pytest.raises(ValueError, match="refuses to build without an authorizer"):
        scim_router(
            app,
            users=InMemoryUserStore(),
            organizations=InMemoryOrganizationStore(roles=ROLES),
            organization="acme",
        )


@pytest.mark.asyncio
async def test_a_store_without_a_member_directory_is_refused_at_build_time() -> None:
    class AuthorizationOnlyStore:
        def roles(self) -> frozenset[str]:
            return ROLES

        async def memberships(self, user_id: str) -> tuple[()]:
            return ()

    fixture = directory()
    with pytest.raises(ValueError, match="members, add_member, remove_member"):
        scim_router(
            fixture.app,
            users=InMemoryUserStore(),
            organizations=AuthorizationOnlyStore(),
            organization="acme",
        )


@pytest.mark.asyncio
async def test_an_organization_id_that_would_break_the_entity_reference_is_refused() -> None:
    fixture = directory()
    with pytest.raises(ValueError, match="must not contain a quote"):
        scim_router(
            fixture.app,
            users=InMemoryUserStore(),
            organizations=InMemoryOrganizationStore(roles=ROLES),
            organization='acme" or resource == Organization::"globex',
        )


# --- provisioning writes the organisation, and nothing else -----------------


@pytest.mark.asyncio
async def test_provisioning_a_user_makes_cedar_permit_them() -> None:
    """The keystone. No SCIM code runs on the request that is finally permitted."""
    fixture = directory()
    async with fixture.client() as client:
        before = await client.get("/doc", headers=AS_ALICE)
        created = await provision(client, "alice@example.com")
        after = await client.get("/doc", headers=AS_ALICE)
    assert before.status == 403
    assert created.status == 201
    # `/doc` is permitted by `context.organizations.contains("acme")`, resolved
    # from the membership SCIM wrote into the ordinary store. Nothing in
    # `wreath._scim` runs on that request.
    assert created.json()["id"] == "1"
    assert after.status == 200
    assert [m.user_id for m in await fixture.organizations.members("acme")] == ["1"]


@pytest.mark.asyncio
async def test_a_created_user_carries_a_location_and_the_created_status() -> None:
    async with directory().client() as client:
        response = await provision(client, "alice@example.com")
    body = response.json()
    assert response.status == 201
    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert body["userName"] == "alice@example.com"
    assert body["active"] is True
    assert body["emails"] == [
        {"value": "alice@example.com", "primary": True, "type": "work"}
    ]
    assert body["groups"] == []
    location = dict(response.headers)[b"location"].decode()
    assert location.endswith("/scim/v2/Users/1")
    assert body["meta"]["location"] == location


@pytest.mark.asyncio
async def test_a_provisioned_account_has_a_password_nothing_can_satisfy() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.hashed_password == UNUSABLE_PASSWORD
    assert verify_password("", UNUSABLE_PASSWORD) is False
    assert verify_password(UNUSABLE_PASSWORD, UNUSABLE_PASSWORD) is False


@pytest.mark.asyncio
async def test_provisioning_the_same_user_twice_is_a_conflict() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        again = await provision(client, "alice@example.com")
    assert again.status == 409
    assert again.json()["scimType"] == "uniqueness"


@pytest.mark.asyncio
async def test_an_account_that_already_exists_is_adopted_rather_than_duplicated() -> None:
    """Somebody who signed up before the directory ever knew about them."""
    fixture = directory()
    existing = await fixture.users.create("alice@example.com", "scrypt$1$1$1$a$b")
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com")
    assert response.status == 201
    assert response.json()["id"] == existing.id
    assert len(fixture.users._by_id) == 1


@pytest.mark.asyncio
async def test_an_address_a_renamed_account_no_longer_carries_is_free() -> None:
    """A store's address index may answer for an address a record has dropped.

    `InMemoryUserStore` documents exactly that, and trusting the hit would make
    this `POST` adopt somebody else's account under a name they no longer have.
    """
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        renamed = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "replace", "path": "userName", "value": "alice2@example.com"}
                ],
            },
            headers=AUTH,
        )
        reused = await provision(client, "alice@example.com")
    assert renamed.status == 200
    assert reused.status == 201
    assert reused.json()["id"] == "2"
    assert reused.json()["userName"] == "alice@example.com"


@pytest.mark.asyncio
async def test_a_body_with_no_user_name_is_refused() -> None:
    async with directory().client() as client:
        response = await client.post("/scim/v2/Users", json={"active": True}, headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidValue"
    assert (b"content-type", b"application/scim+json") in response.headers


@pytest.mark.asyncio
async def test_a_refused_create_leaves_the_organisation_untouched() -> None:
    """The membership is written after every refusal this body can draw."""
    fixture = directory()
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com", active="yes")
    assert response.status == 400
    assert "active must be true or false" in response.json()["detail"]
    assert await fixture.organizations.members("acme") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["post", "put", "patch"])
async def test_a_body_that_is_not_json_is_a_400_rather_than_a_500(verb: str) -> None:
    """Distinguished from a body that parses and is not an object.

    Both are 400 `invalidSyntax`, so asserting the code alone would pass on
    either branch -- and the branch that fired is the difference between "your
    JSON is broken" and "your JSON is fine and the wrong shape".
    """
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        broken = await getattr(client, verb)(
            "/scim/v2/Users" if verb == "post" else "/scim/v2/Users/1",
            content=b"{not json",
            headers=AUTH,
        )
        wrong_shape = await client.post(
            "/scim/v2/Users", json=["not", "an", "object"], headers=AUTH
        )
    assert broken.status == 400
    assert "not valid JSON" in broken.json()["detail"]
    assert wrong_shape.status == 400
    assert "must be a JSON object" in wrong_shape.json()["detail"]


@pytest.mark.asyncio
async def test_a_password_sent_on_creation_is_hashed_and_usable() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com", password="correct horse")
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.hashed_password.startswith("scrypt$")
    assert verify_password("correct horse", record.hashed_password)


# --- listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_list_is_a_list_response_with_one_based_paging() -> None:
    async with directory().client() as client:
        for index in range(5):
            await provision(client, f"user{index}@example.com")
        page = await client.get("/scim/v2/Users?startIndex=2&count=2", headers=AUTH)
    body = page.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert body["totalResults"] == 5
    assert body["startIndex"] == 2
    assert body["itemsPerPage"] == 2
    assert [entry["userName"] for entry in body["Resources"]] == [
        "user1@example.com",
        "user2@example.com",
    ]


@pytest.mark.asyncio
async def test_the_page_size_defaults_and_its_ceiling_both_bound_a_list() -> None:
    """Both are bounds rather than formalities: one caps an unasked page, one caps a greedy ask."""
    from wreath._scim.router import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

    fixture = directory()
    for index in range(MAX_PAGE_SIZE + 1):
        record = await fixture.users.create(f"user{index:04d}@example.com", "x$y")
        await fixture.organizations.add_member("acme", record.id)
    async with fixture.client() as client:
        unasked = (await client.get("/scim/v2/Users", headers=AUTH)).json()
        greedy = (await client.get("/scim/v2/Users?count=100000", headers=AUTH)).json()
    assert unasked["totalResults"] == MAX_PAGE_SIZE + 1
    assert unasked["itemsPerPage"] == DEFAULT_PAGE_SIZE
    assert greedy["itemsPerPage"] == MAX_PAGE_SIZE


@pytest.mark.asyncio
async def test_a_record_with_no_timestamps_omits_them_rather_than_dating_them_to_1970() -> None:
    fixture = directory()
    record = await fixture.users.create("alice@example.com", "x$y")
    record.created_at = 0.0
    record.updated_at = 1767225845.0
    await fixture.organizations.add_member("acme", record.id)
    async with fixture.client() as client:
        meta = (await client.get(f"/scim/v2/Users/{record.id}", headers=AUTH)).json()["meta"]
    assert "created" not in meta, "a missing timestamp is absent, never 1970"
    assert meta["lastModified"] == "2026-01-01T00:04:05Z", "RFC 3339, UTC, 'Z'"
    record.updated_at = 0.0
    async with fixture.client() as client:
        bare = (await client.get(f"/scim/v2/Users/{record.id}", headers=AUTH)).json()
    assert "lastModified" not in bare["meta"]
    # And the ordinary case, from the other side: a record the store stamped
    # carries both, so the omission above is the exception rather than the rule.
    async with fixture.client() as client:
        created = await provision(client, "bob@example.com")
    assert "created" in created.json()["meta"]
    assert created.json()["meta"]["lastModified"].endswith("Z")


@pytest.mark.asyncio
async def test_a_count_of_zero_returns_the_total_and_no_resources() -> None:
    """Section 3.4.2.4 spells this one out, and directories use it to count."""
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        body = (await client.get("/scim/v2/Users?count=0", headers=AUTH)).json()
    assert body["totalResults"] == 1
    assert body["Resources"] == []


@pytest.mark.asyncio
async def test_a_negative_count_and_a_zero_start_index_are_clamped_not_refused() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        body = (
            await client.get("/scim/v2/Users?count=-4&startIndex=0", headers=AUTH)
        ).json()
    assert body["startIndex"] == 1
    assert body["Resources"] == []


@pytest.mark.asyncio
async def test_a_count_that_is_not_a_number_is_refused() -> None:
    async with directory().client() as client:
        response = await client.get("/scim/v2/Users?count=lots", headers=AUTH)
    assert response.status == 400
    assert "count must be an integer" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_filter_selects_by_user_name() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        found = await client.get(
            '/scim/v2/Users?filter=userName eq "alice@example.com"', headers=AUTH
        )
    body = found.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "alice@example.com"


@pytest.mark.asyncio
async def test_a_filter_on_an_attribute_this_provider_lacks_is_refused() -> None:
    """The alternative is an empty page, which reads as "create them again"."""
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        response = await client.get(
            '/scim/v2/Users?filter=externalId eq "abc"', headers=AUTH
        )
    assert response.status == 400
    assert response.json()["scimType"] == "invalidFilter"
    assert "externalid" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_filtered_list_refuses_an_organisation_over_the_scan_ceiling() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=1)
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        response = await client.get("/scim/v2/Users?filter=userName pr", headers=AUTH)
        unfiltered = await client.get("/scim/v2/Users", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"
    # The ceiling bounds a *filter*, which is evaluated per member. An ordinary
    # page reads only the members on the page, so it is not bounded by it -- and
    # a list that started refusing at 1000 members would be a worse bug than the
    # fan-out this ceiling exists to prevent.
    assert unfiltered.status == 200
    assert unfiltered.json()["totalResults"] == 2


@pytest.mark.asyncio
async def test_users_are_sorted_before_the_page_is_selected() -> None:
    async with directory().client() as client:
        for name in ("charlie@example.com", "alice@example.com", "bob@example.com"):
            await provision(client, name)
        response = await client.get(
            "/scim/v2/Users?sortBy=userName&sortOrder=descending&startIndex=2&count=1",
            headers=AUTH,
        )
    assert response.status == 200
    assert [row["userName"] for row in response.json()["Resources"]] == [
        "bob@example.com"
    ]


async def test_groups_can_be_sorted_by_display_name() -> None:
    async with directory().client() as client:
        response = await client.get(
            "/scim/v2/Groups?sortBy=displayName&sortOrder=descending", headers=AUTH
        )
    assert response.status == 200
    assert [row["displayName"] for row in response.json()["Resources"]] == [
        "member",
        "billing",
        "admin",
    ]


async def test_sorting_refuses_an_unknown_attribute_and_order() -> None:
    async with directory().client() as client:
        attribute = await client.get(
            "/scim/v2/Users?sortBy=displayName", headers=AUTH
        )
        order = await client.get(
            "/scim/v2/Users?sortBy=userName&sortOrder=sideways", headers=AUTH
        )
    assert attribute.status == 400
    assert attribute.json()["scimType"] == "invalidValue"
    assert order.status == 400
    assert "ascending" in order.json()["detail"]


@pytest.mark.asyncio
async def test_a_user_outside_this_organisation_is_not_found_rather_than_forbidden() -> None:
    """A directory for one tenant may not learn that an account exists in another."""
    fixture = directory()
    other = await fixture.users.create("bob@example.com", "scrypt$1$1$1$a$b")
    await fixture.organizations.add_member("globex", other.id)
    async with fixture.client() as client:
        response = await client.get(f"/scim/v2/Users/{other.id}", headers=AUTH)
    assert response.status == 404


# --- updating ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_patch_renames_a_user() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "replace", "path": "userName", "value": "alice2@example.com"}
                ],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert response.json()["userName"] == "alice2@example.com"
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.email == "alice2@example.com"


@pytest.mark.asyncio
async def test_a_rename_onto_another_account_is_a_conflict() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "replace", "path": "userName", "value": "bob@example.com"}
                ],
            },
            headers=AUTH,
        )
    assert response.status == 409
    assert response.json()["scimType"] == "uniqueness"


@pytest.mark.asyncio
async def test_deactivating_a_user_disables_the_account() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert response.json()["active"] is False
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.is_active is False


@pytest.mark.asyncio
async def test_deactivating_a_user_who_belongs_elsewhere_is_refused() -> None:
    """`is_active` is the account, not the membership. One tenant does not own it."""
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("globex", "1")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "mutability"
    assert "1 other organization(s)" in response.json()["detail"]
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.is_active is True


@pytest.mark.asyncio
async def test_a_put_replaces_writable_attributes_and_ignores_the_rest() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        response = await client.put(
            "/scim/v2/Users/1",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "id": "999",
                "userName": "alice@example.com",
                "active": False,
                "externalId": "directory-side-id",
                "groups": [{"value": "admin"}],
            },
            headers=AUTH,
        )
    body = response.json()
    assert response.status == 200
    assert body["id"] == "1"
    assert body["active"] is False
    assert body["groups"] == []
    assert "externalId" not in body


@pytest.mark.asyncio
async def test_patching_a_read_only_attribute_is_refused() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "add", "path": "groups", "value": [{"value": "admin"}]}
                ],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "mutability"


# --- de-provisioning --------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_the_membership_and_keeps_the_account() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        removed = await client.delete("/scim/v2/Users/1", headers=AUTH)
        again = await client.delete("/scim/v2/Users/1", headers=AUTH)
        gone = await client.get("/scim/v2/Users/1", headers=AUTH)
    assert removed.status == 204
    assert removed.body == b""
    assert again.status == 404
    assert gone.status == 404
    assert await fixture.organizations.members("acme") == ()
    # The account and everything hanging off it survive: de-provisioning is not
    # a delete, and a cascade here would be unrecoverable.
    assert await fixture.users.get_by_id("1") is not None


@pytest.mark.asyncio
async def test_de_provisioning_takes_a_users_access_away_through_cedar() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        during = await client.get("/doc", headers=AS_ALICE)
        await client.delete("/scim/v2/Users/1", headers=AUTH)
        after = await client.get("/doc", headers=AS_ALICE)
    assert during.status == 200
    assert after.status == 403


# --- groups are roles -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_groups_are_the_declared_role_vocabulary() -> None:
    async with directory().client() as client:
        body = (await client.get("/scim/v2/Groups", headers=AUTH)).json()
    assert [entry["displayName"] for entry in body["Resources"]] == sorted(ROLES)
    assert body["totalResults"] == len(ROLES)


@pytest.mark.asyncio
async def test_a_group_can_be_filtered_by_display_name() -> None:
    async with directory().client() as client:
        body = (
            await client.get('/scim/v2/Groups?filter=displayName eq "admin"', headers=AUTH)
        ).json()
    assert [entry["id"] for entry in body["Resources"]] == ["admin"]


@pytest.mark.asyncio
async def test_adding_a_member_to_a_group_grants_the_role_through_cedar() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        before = await client.get("/invite", headers=AS_ALICE)
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": "1"}]}
                ],
            },
            headers=AUTH,
        )
        after = await client.get("/invite", headers=AS_ALICE)
    assert before.status == 403
    assert response.status == 200
    assert [member["value"] for member in response.json()["members"]] == ["1"]
    assert after.status == 200
    held = await fixture.organizations.members("acme")
    assert held[0].roles == frozenset({"admin"})


@pytest.mark.asyncio
async def test_removing_a_member_from_a_group_revokes_only_that_role() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"admin", "billing"})
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "remove", "path": 'members[value eq "1"]'}],
            },
            headers=AUTH,
        )
        repeated = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "remove", "path": 'members[value eq "1"]'}],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert repeated.status == 200, "a repeated de-provisioning must converge"
    held = await fixture.organizations.members("acme")
    assert held[0].roles == frozenset({"billing"})


@pytest.mark.asyncio
async def test_a_group_member_who_is_not_in_the_organisation_is_refused() -> None:
    """A role is a grant inside a tenant; it cannot be given to an outsider."""
    fixture = directory()
    stranger = await fixture.users.create("bob@example.com", "scrypt$1$1$1$a$b")
    async with fixture.client() as client:
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": stranger.id}]}
                ],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "invalidValue"
    assert "not a member of this organization" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_put_on_a_group_sets_its_membership_exactly() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"admin"})
        response = await client.put(
            "/scim/v2/Groups/admin",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "admin",
                "members": [{"value": "2"}],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert [member["value"] for member in response.json()["members"]] == ["2"]
    held = {m.user_id: m.roles for m in await fixture.organizations.members("acme")}
    assert held == {"1": frozenset(), "2": frozenset({"admin"})}


@pytest.mark.asyncio
async def test_one_group_is_served_by_its_role_name() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"billing"})
        found = await client.get("/scim/v2/Groups/billing", headers=AUTH)
        empty = await client.get("/scim/v2/Groups/admin", headers=AUTH)
        unknown = await client.get("/scim/v2/Groups/nonesuch", headers=AUTH)
    assert found.status == 200
    assert found.json()["displayName"] == "billing"
    assert [member["value"] for member in found.json()["members"]] == ["1"]
    assert empty.status == 200
    assert empty.json()["members"] == []
    assert unknown.status == 404


@pytest.mark.asyncio
async def test_a_group_patch_reads_the_role_it_names_rather_than_every_member() -> None:
    """Removing one member must not hand the role to everybody else in the tenant.

    It would, if the document the patch is applied to listed the organisation's
    members instead of the role's: `remove` would then compute a `members` list
    containing every other member, and the writer would grant it to them.
    """
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"admin"})
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "remove", "path": 'members[value eq "1"]'}],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert response.json()["members"] == []
    held = {m.user_id: m.roles for m in await fixture.organizations.members("acme")}
    assert held == {"1": frozenset(), "2": frozenset()}


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["put", "patch"])
async def test_writing_a_user_outside_this_organisation_is_not_found(verb: str) -> None:
    fixture = directory()
    other = await fixture.users.create("bob@example.com", "scrypt$1$1$1$a$b")
    await fixture.organizations.add_member("globex", other.id)
    async with fixture.client() as client:
        response = await getattr(client, verb)(
            f"/scim/v2/Users/{other.id}",
            json={"schemas": [PATCH_BODY], "Operations": []},
            headers=AUTH,
        )
    assert response.status == 404
    assert await fixture.organizations.members("acme") == ()


# --- the shapes a client can send -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "fragment"),
    [
        ({"op": "replace", "path": "userName", "value": 7}, "userName must be a non-empty"),
        ({"op": "replace", "path": "userName", "value": "  "}, "userName must be a non-empty"),
        ({"op": "replace", "path": "active", "value": "yes"}, "active must be true or false"),
        ({"op": "replace", "path": "password", "value": 7}, "password must be a non-empty"),
        ({"op": "replace", "path": "password", "value": ""}, "password must be a non-empty"),
    ],
)
async def test_a_wrongly_typed_value_is_refused_by_its_own_message(
    operation: dict[str, Any], fragment: str
) -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={"schemas": [PATCH_BODY], "Operations": [operation]},
            headers=AUTH,
        )
    assert response.status == 400
    assert fragment in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_blank_user_name_is_refused_on_creation() -> None:
    async with directory().client() as client:
        response = await provision(client, "   ")
    assert response.status == 400
    assert "userName is required" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [{"value": 7}, {"value": ""}, {"display": "no value"}])
async def test_a_group_member_without_a_string_value_is_refused(
    member: dict[str, Any],
) -> None:
    async with directory().client() as client:
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "add", "path": "members", "value": [member]}],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert "needs a string 'value'" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_group_body_that_is_not_json_and_an_unknown_group_are_refused() -> None:
    async with directory().client() as client:
        broken = await client.patch(
            "/scim/v2/Groups/admin", content=b"{not json", headers=AUTH
        )
        unknown = await client.patch(
            "/scim/v2/Groups/nonesuch",
            json={"schemas": [PATCH_BODY], "Operations": []},
            headers=AUTH,
        )
    assert broken.status == 400
    assert "not valid JSON" in broken.json()["detail"]
    assert unknown.status == 404


@pytest.mark.asyncio
async def test_a_membership_whose_account_is_gone_is_skipped_rather_than_served() -> None:
    """A dangling membership is not a user, and must not become half of one."""
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("acme", "ghost")
        one = await client.get("/scim/v2/Users/ghost", headers=AUTH)
        listed = await client.get("/scim/v2/Users", headers=AUTH)
        filtered = await client.get("/scim/v2/Users?filter=userName pr", headers=AUTH)
    assert one.status == 404
    assert [entry["id"] for entry in listed.json()["Resources"]] == ["1"]
    assert [entry["id"] for entry in filtered.json()["Resources"]] == ["1"]


@pytest.mark.asyncio
async def test_a_user_appears_only_under_the_roles_they_hold() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"billing"})
        groups = (await client.get("/scim/v2/Groups", headers=AUTH)).json()
        user = (await client.get("/scim/v2/Users/1", headers=AUTH)).json()
    holding = {
        entry["displayName"]: [member["value"] for member in entry["members"]]
        for entry in groups["Resources"]
    }
    assert holding == {"admin": [], "billing": ["1"], "member": []}
    assert [group["value"] for group in user["groups"]] == ["billing"]


@pytest.mark.asyncio
async def test_a_write_that_changes_nothing_touches_neither_store() -> None:
    """Idempotency, at the level below the protocol: a replay writes nothing."""

    class CountingOrganizations(InMemoryOrganizationStore):
        writes = 0

        async def add_member(self, org_id, user_id, *, roles=()):
            type(self).writes += 1
            return await super().add_member(org_id, user_id, roles=roles)

    fixture = directory()
    fixture.organizations = CountingOrganizations(roles=ROLES)
    fixture.app.include_router(
        scim_router(
            fixture.app,
            users=fixture.users,
            organizations=fixture.organizations,
            organization="acme",
            prefix="/scim/v3",
        )
    )
    async with fixture.client() as client:
        await client.post(
            "/scim/v3/Users", json={"userName": "alice@example.com"}, headers=AUTH
        )
        await client.patch(
            "/scim/v3/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": "1"}]}],
            },
            headers=AUTH,
        )
        before = CountingOrganizations.writes
        record = await fixture.users.get_by_id("1")
        assert record is not None
        stamped = record.updated_at
        replayed = await client.patch(
            "/scim/v3/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": "1"}, {"value": "1"}]}
                ],
            },
            headers=AUTH,
        )
        same = await client.put(
            "/scim/v3/Users/1",
            json={"userName": "alice@example.com", "active": True},
            headers=AUTH,
        )
    assert replayed.status == 200
    assert same.status == 200
    assert CountingOrganizations.writes == before, "a no-op group patch wrote a membership"
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.updated_at == stamped, "a no-op user write touched the account"


# --- build-time refusals ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        ({"read_action": ""}, "non-empty read_action and write_action"),
        ({"write_action": ""}, "non-empty read_action and write_action"),
        ({"page_size": 0}, "1 <= page_size <= max_page_size"),
        ({"max_page_size": 0}, "1 <= page_size <= max_page_size"),
        ({"page_size": 50, "max_page_size": 10}, "1 <= page_size <= max_page_size"),
        ({"max_filter_scan": 0}, "positive max_filter_scan"),
    ],
)
async def test_an_incoherent_configuration_is_refused_where_it_is_written(
    options: dict[str, Any], fragment: str
) -> None:
    fixture = directory()
    with pytest.raises(ValueError, match=fragment):
        scim_router(
            fixture.app,
            users=fixture.users,
            organizations=fixture.organizations,
            organization="acme",
            **options,
        )


# --- more than one tenant ---------------------------------------------------


@pytest.mark.asyncio
async def test_one_mount_can_serve_a_tenant_per_path_segment() -> None:
    """The organisation resolved from the request is the one Cedar is asked about.

    Both come from the same function, so the data scope and the policy decision
    cannot disagree about which tenant is meant -- which is the whole reason
    `organization` accepts a callable rather than the router reading a path
    parameter on its own.
    """
    users = InMemoryUserStore()
    organizations = InMemoryOrganizationStore(roles=ROLES)
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("directory")),
        CedarAuthorizer(
            engine=CedarPolicies(POLICY), organizations=Memberships(organizations)
        ),
    )
    app.include_router(
        scim_router(
            app,
            users=users,
            organizations=organizations,
            organization=lambda request: request.path_params["tenant"],
            prefix="/scim/v2/{tenant}",
        )
    )
    async with TestClient(app) as client:
        permitted = await client.post(
            "/scim/v2/acme/Users", json={"userName": "alice@example.com"}, headers=AUTH
        )
        # The policy names `Organization::"acme"` and nothing else, so the same
        # routes under another tenant are refused by the policy set rather than
        # by anything this router decided.
        refused = await client.post(
            "/scim/v2/globex/Users", json={"userName": "bob@example.com"}, headers=AUTH
        )
    assert permitted.status == 201
    assert permitted.json()["meta"]["location"].endswith("/scim/v2/acme/Users/1")
    assert refused.status == 403
    assert await organizations.members("globex") == ()


@pytest.mark.asyncio
async def test_the_member_directory_lists_one_organisation_in_user_id_order() -> None:
    """`members()` is the seam the authorization path never needs and SCIM does."""
    store = InMemoryOrganizationStore(roles=ROLES)
    await store.add_member("acme", "b", roles={"admin"})
    await store.add_member("acme", "a")
    await store.add_member("globex", "c")
    listed = await store.members("acme")
    assert [membership.user_id for membership in listed] == ["a", "b"]
    assert listed[1].roles == frozenset({"admin"})
    assert await store.members("nobody") == ()


@pytest.mark.asyncio
async def test_creating_or_deleting_a_group_is_not_implemented_and_says_why() -> None:
    """The vocabulary is configuration a Cedar policy names by string."""
    async with directory().client() as client:
        created = await client.post(
            "/scim/v2/Groups", json={"displayName": "auditor"}, headers=AUTH
        )
        deleted = await client.delete("/scim/v2/Groups/admin", headers=AUTH)
    assert created.status == 501
    assert "declared vocabulary" in created.json()["detail"]
    assert deleted.status == 501
    assert "cannot be deleted over SCIM" in deleted.json()["detail"]
