from __future__ import annotations

import json
from typing import Any, cast

import pytest

from wreath import Wreath
from wreath._auth.cedar_engine import CedarPolicies
from wreath._scim.router import UNUSABLE_PASSWORD, _sortable_value
from wreath._userkit import InMemoryUserStore, verify_password
from wreath.auth import AuthenticationBackend, BearerTokenBackend, Identity
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
SEARCH_BODY = "urn:ietf:params:scim:api:messages:2.0:SearchRequest"


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
        self.revoked_session_users: list[str] = []
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
                revoke_sessions=self.revoke_sessions,
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

    async def revoke_sessions(self, user_id: str) -> None:
        self.revoked_session_users.append(user_id)

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


@pytest.mark.asyncio
async def test_the_content_type_is_scim_json() -> None:
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
    assert body["pagination"] == {
        "cursor": True,
        "index": True,
        "defaultPaginationMethod": "index",
        "defaultPageSize": 100,
        "maxPageSize": 200,
        "cursorTimeout": 3600,
    }


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

    class Backend:
        async def authenticate(self, request: Any) -> Identity:
            return Identity("directory")

    class Challenging(Backend):
        def challenge(self, request: Any) -> Any:
            return challenge

    fixture = directory()
    fixture.app.configure_auth(
        cast(
            "AuthenticationBackend",
            Backend() if challenge is None else Challenging(),
        ),
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
    urn = f"urn:ietf:params:scim:schemas:core:2.0:{name}"
    async with directory().client() as client:
        found = await client.get(f"/scim/v2/Schemas/{urn}", headers=AUTH)
        missing = await client.get("/scim/v2/Schemas/urn:made:up", headers=AUTH)
    assert found.json()["name"] == name
    assert found.json()["meta"]["location"].endswith(urn)
    assert missing.status == 404


@pytest.mark.asyncio
async def test_a_principal_the_policy_does_not_permit_is_refused() -> None:
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


def test_a_user_store_without_batch_lookup_is_refused_at_build_time() -> None:
    class SingleLookupStore:
        async def get_by_id(self, user_id: str) -> None:
            return None

        async def get_by_email(self, email: str) -> None:
            return None

        async def create(self, email: str, hashed_password: str) -> None:
            return None

        async def update(self, user: Any) -> None:
            return None

    fixture = directory()
    with pytest.raises(ValueError, match="get_many_by_id"):
        scim_router(
            fixture.app,
            users=SingleLookupStore(),
            organizations=fixture.organizations,
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


@pytest.mark.parametrize(
    ("organization", "fragment"),
    [
        ("acme:admin", "must not contain ':'"),
        ("acme\nadmin", "must not contain controls"),
        ("acme\x85admin", "must not contain controls"),
    ],
)
def test_an_organization_id_must_be_safe_for_data_and_policy_namespaces(
    organization: str,
    fragment: str,
) -> None:
    fixture = directory()
    with pytest.raises(ValueError, match=fragment):
        scim_router(
            fixture.app,
            users=fixture.users,
            organizations=fixture.organizations,
            organization=organization,
        )


@pytest.mark.asyncio
async def test_provisioning_a_user_makes_cedar_permit_them() -> None:
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
        response = await client.post(
            "/scim/v2/Users",
            json={"userName": "alice@example.com"},
            headers={**AUTH, "host": "scim.example"},
        )
    body = response.json()
    assert response.status == 201
    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert body["userName"] == "alice@example.com"
    assert body["active"] is True
    assert body["emails"] == [{"value": "alice@example.com", "primary": True, "type": "work"}]
    assert body["groups"] == []
    location = dict(response.headers)[b"location"].decode()
    assert location == "http://scim.example/scim/v2/Users/1"
    assert body["meta"]["location"] == location


@pytest.mark.asyncio
async def test_a_request_without_host_gets_a_relative_location() -> None:
    fixture = directory()
    router = scim_router(
        fixture.app,
        users=fixture.users,
        organizations=fixture.organizations,
        organization="acme",
    )
    handler = next(
        route.endpoint for route in router.routes if route.path == "/scim/v2/ServiceProviderConfig"
    )

    class Request:
        path_params: dict[str, str] = {}
        scope = {"root_path": "/mounted"}
        scheme = "https"

        def header(self, name: str) -> None:
            return None

    response = await handler(Request())

    assert json.loads(response.body)["meta"]["location"] == (
        "/mounted/scim/v2/ServiceProviderConfig"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hosts",
    [
        [b"attacker.example", b"directory.example"],
        [b"attacker.example/path"],
    ],
    ids=["duplicate", "invalid"],
)
async def test_ambiguous_host_gets_a_relative_location(hosts: list[bytes]) -> None:
    app = directory().app
    sent: list[dict[str, Any]] = []
    path = "/scim/v2/ServiceProviderConfig"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer let-me-in"),
            *((b"host", host) for host in hosts),
        ],
        "server": ("directory.example", 443),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    body = json.loads(next(message["body"] for message in sent if "body" in message))

    assert body["meta"]["location"] == "/scim/v2/ServiceProviderConfig"


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
    fixture = directory()
    existing = await fixture.users.create("alice@example.com", "scrypt$1$1$1$a$b")
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com")
    assert response.status == 201
    assert response.json()["id"] == existing.id
    assert len(fixture.users._by_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"password": "replacement"}, {"active": False}])
async def test_adoption_cannot_change_a_standalone_accounts_security_state(
    extra: dict[str, object],
) -> None:
    fixture = directory()
    existing = await fixture.users.create("alice@example.com", "existing-password-hash")
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com", **extra)
    assert response.status == 400
    assert response.json()["scimType"] == "mutability"
    stored = await fixture.users.get_by_id(existing.id)
    assert stored is not None
    assert stored.hashed_password == "existing-password-hash"
    assert stored.is_active is True
    assert await fixture.organizations.members("acme") == ()


@pytest.mark.asyncio
async def test_an_address_a_renamed_account_no_longer_carries_is_free() -> None:
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
    fixture = directory()
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com", active="yes")
    assert response.status == 400
    assert "active must be true or false" in response.json()["detail"]
    assert await fixture.organizations.members("acme") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["post", "put", "patch"])
async def test_a_body_that_is_not_json_is_a_400_rather_than_a_500(verb: str) -> None:
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
    assert fixture.revoked_session_users == ["1"]


@pytest.mark.asyncio
async def test_a_password_over_the_hashing_ceiling_is_a_scim_refusal() -> None:
    fixture = directory()
    async with fixture.client() as client:
        response = await provision(client, "alice@example.com", password="x" * 1025)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidValue"
    assert "at most 1024 UTF-8 bytes" in response.json()["detail"]
    assert await fixture.organizations.members("acme") == ()


@pytest.mark.asyncio
async def test_an_unpaired_surrogate_password_is_a_scim_refusal() -> None:
    fixture = directory()
    async with fixture.client() as client:
        response = await client.post(
            "/scim/v2/Users",
            content=b'{"userName":"alice@example.com","password":"\\ud800"}',
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "invalidValue"
    assert "valid Unicode" in response.json()["detail"]


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
async def test_cursor_pagination_walks_users_without_exposing_an_index() -> None:
    async with directory().client() as client:
        for index in range(5):
            await provision(client, f"user{index}@example.com")
        first = (await client.get("/scim/v2/Users?cursor&count=2", headers=AUTH)).json()
        second = (
            await client.get(
                f"/scim/v2/Users?cursor={first['nextCursor']}&count=2",
                headers=AUTH,
            )
        ).json()
        third = (
            await client.get(
                f"/scim/v2/Users?cursor={second['nextCursor']}&count=2",
                headers=AUTH,
            )
        ).json()
    assert [row["userName"] for row in first["Resources"]] == [
        "user0@example.com",
        "user1@example.com",
    ]
    assert [row["userName"] for row in second["Resources"]] == [
        "user2@example.com",
        "user3@example.com",
    ]
    assert [row["userName"] for row in third["Resources"]] == ["user4@example.com"]
    assert "startIndex" not in first
    assert "startIndex" not in second
    assert "nextCursor" not in third


@pytest.mark.asyncio
async def test_cursor_pagination_is_available_to_post_search_requests() -> None:
    async with directory().client() as client:
        for index in range(3):
            await provision(client, f"user{index}@example.com")
        first = (
            await client.post(
                "/scim/v2/Users/.search",
                json={"schemas": [SEARCH_BODY], "cursor": "", "count": 2},
                headers=AUTH,
            )
        ).json()
        second = (
            await client.post(
                "/scim/v2/Users/.search",
                json={
                    "schemas": [SEARCH_BODY],
                    "cursor": first["nextCursor"],
                    "count": 2,
                },
                headers=AUTH,
            )
        ).json()
    assert [row["userName"] for row in first["Resources"]] == [
        "user0@example.com",
        "user1@example.com",
    ]
    assert [row["userName"] for row in second["Resources"]] == ["user2@example.com"]


@pytest.mark.asyncio
async def test_post_search_refuses_an_invalid_message_shape() -> None:
    async with directory().client() as client:
        wrong_schema = await client.post(
            "/scim/v2/Users/.search",
            json={"schemas": ["wrong"], "cursor": ""},
            headers=AUTH,
        )
        wrong_cursor = await client.post(
            "/scim/v2/Users/.search",
            json={"schemas": [SEARCH_BODY], "cursor": 7},
            headers=AUTH,
        )
    assert wrong_schema.status == 400
    assert wrong_schema.json()["scimType"] == "invalidSyntax"
    assert wrong_cursor.status == 400
    assert wrong_cursor.json()["scimType"] == "invalidValue"


@pytest.mark.asyncio
async def test_post_search_supports_group_cursors() -> None:
    async with directory().client() as client:
        first = (
            await client.post(
                "/scim/v2/Groups/.search",
                json={"schemas": [SEARCH_BODY], "cursor": "", "count": 2},
                headers=AUTH,
            )
        ).json()
        second = (
            await client.post(
                "/scim/v2/Groups/.search",
                json={
                    "schemas": [SEARCH_BODY],
                    "cursor": first["nextCursor"],
                    "count": 2,
                },
                headers=AUTH,
            )
        ).json()
    assert len(first["Resources"]) == 2
    assert len(second["Resources"]) == 1
    assert "nextCursor" not in second


@pytest.mark.asyncio
async def test_cursor_errors_distinguish_count_syntax_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._scim.router as router_module

    now = 1_000_000
    monkeypatch.setattr(router_module.time, "time", lambda: now)
    fixture = Provisioned(Identity("directory"), cursor_timeout=1)
    async with fixture.client() as client:
        for index in range(2):
            await provision(client, f"user{index}@example.com")
        invalid_count = await client.get("/scim/v2/Users?cursor&count=not-an-integer", headers=AUTH)
        first = (await client.get("/scim/v2/Users?cursor&count=1", headers=AUTH)).json()
        now += 2
        expired = await client.get(
            f"/scim/v2/Users?cursor={first['nextCursor']}&count=1", headers=AUTH
        )
    assert invalid_count.status == 400
    assert invalid_count.json()["scimType"] == "invalidCount"
    assert expired.status == 400
    assert expired.json()["scimType"] == "expiredCursor"


@pytest.mark.asyncio
async def test_cursor_is_bound_to_the_initial_count_and_query() -> None:
    async with directory().client() as client:
        for index in range(3):
            await provision(client, f"user{index}@example.com")
        first = (await client.get("/scim/v2/Users?cursor&count=1", headers=AUTH)).json()
        cursor = first["nextCursor"]
        changed_count = await client.get(f"/scim/v2/Users?cursor={cursor}&count=2", headers=AUTH)
        changed_filter = await client.get(
            f'/scim/v2/Users?cursor={cursor}&count=1&filter=userName%20sw%20"u"',
            headers=AUTH,
        )
    assert changed_count.status == 400
    assert changed_count.json()["scimType"] == "invalidCount"
    assert changed_filter.status == 400
    assert changed_filter.json()["scimType"] == "invalidCursor"


@pytest.mark.asyncio
async def test_cursor_is_bound_to_its_resource_and_organization() -> None:
    users = InMemoryUserStore()
    organizations = InMemoryOrganizationStore(roles=ROLES)
    app = Wreath()
    policy = POLICY + """
permit(principal == User::"directory", action == Action::"scim_read",
       resource == Organization::"globex");
permit(principal == User::"directory", action == Action::"scim_write",
       resource == Organization::"globex");
"""
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("directory")),
        CedarAuthorizer(
            engine=CedarPolicies(policy),
            organizations=Memberships(organizations),
        ),
    )
    app.include_router(
        scim_router(
            app,
            users=users,
            organizations=organizations,
            organization=lambda request: request.path_params["tenant"],
            prefix="/scim/v2/{tenant}",
            cursor_secret=b"shared-cursor-secret-that-is-long-enough",
        )
    )
    async with TestClient(app) as client:
        for index in range(2):
            await client.post(
                "/scim/v2/acme/Users",
                json={"userName": f"user{index}@example.com"},
                headers=AUTH,
            )
        first = (
            await client.get("/scim/v2/acme/Users?cursor&count=1", headers=AUTH)
        ).json()
        cursor = first["nextCursor"]
        wrong_resource = await client.get(
            f"/scim/v2/acme/Groups?cursor={cursor}&count=1", headers=AUTH
        )
        wrong_org = await client.get(
            f"/scim/v2/globex/Users?cursor={cursor}&count=1", headers=AUTH
        )
    assert wrong_resource.status == 400
    assert wrong_resource.json()["scimType"] == "invalidCursor"
    assert wrong_org.status == 400
    assert wrong_org.json()["scimType"] == "invalidCursor"


@pytest.mark.asyncio
async def test_cursor_signatures_are_compared_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._scim.router as router_module

    compared: list[tuple[str, str]] = []
    original = router_module.hmac.compare_digest

    def observed(left: str, right: str) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(router_module.hmac, "compare_digest", observed)
    async with directory().client() as client:
        response = await client.get("/scim/v2/Users?cursor=forged.value&count=2", headers=AUTH)
    assert response.status == 400
    assert compared and compared[-1][0] == "value"


@pytest.mark.asyncio
async def test_a_forged_cursor_is_refused() -> None:
    async with directory().client() as client:
        response = await client.get("/scim/v2/Users?cursor=forged.value&count=2", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidCursor"


@pytest.mark.asyncio
async def test_user_lists_issue_one_ordered_batch_lookup() -> None:
    class CountingStore(InMemoryUserStore):
        def __init__(self) -> None:
            super().__init__()
            self.batch_calls: list[tuple[str, ...]] = []

        async def get_many_by_id(self, user_ids):
            ordered = tuple(user_ids)
            self.batch_calls.append(ordered)
            return await super().get_many_by_id(ordered)

    fixture = directory()
    fixture.users = CountingStore()
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
        await client.post("/scim/v3/Users", json={"userName": "b@example.com"}, headers=AUTH)
        await client.post("/scim/v3/Users", json={"userName": "a@example.com"}, headers=AUTH)
        page = await client.get("/scim/v3/Users", headers=AUTH)
        filtered = await client.get("/scim/v3/Users?filter=userName pr", headers=AUTH)
        empty = await client.get("/scim/v3/Users?count=0", headers=AUTH)

    assert page.status == 200
    assert filtered.status == 200
    assert empty.status == 200
    assert fixture.users.batch_calls == [("1", "2"), ("1", "2")]


@pytest.mark.asyncio
async def test_the_page_size_defaults_and_its_ceiling_both_bound_a_list() -> None:
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
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        body = (await client.get("/scim/v2/Users?count=0", headers=AUTH)).json()
    assert body["totalResults"] == 1
    assert body["Resources"] == []


@pytest.mark.asyncio
async def test_a_negative_count_and_a_zero_start_index_are_clamped_not_refused() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        body = (await client.get("/scim/v2/Users?count=-4&startIndex=0", headers=AUTH)).json()
    assert body["startIndex"] == 1
    assert body["Resources"] == []


@pytest.mark.asyncio
async def test_a_count_that_is_not_a_number_is_refused() -> None:
    async with directory().client() as client:
        response = await client.get("/scim/v2/Users?count=lots", headers=AUTH)
    assert response.status == 400
    assert "count must be an integer" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["count=1&count=2", "count=1&COUNT=2"],
    ids=("same-case", "mixed-case"),
)
async def test_ambiguous_get_query_parameters_are_refused(query: str) -> None:
    async with directory().client() as client:
        response = await client.get(f"/scim/v2/Users?{query}", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidSyntax"
    assert "more than once" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_query_field_count_is_bounded_before_materialization() -> None:
    query = "&".join(f"extension{index}=x" for index in range(17))
    async with directory().client() as client:
        response = await client.get(f"/scim/v2/Users?{query}", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidSyntax"
    assert "at most 16 fields" in response.json()["detail"]


@pytest.mark.asyncio
async def test_search_body_member_count_is_bounded_before_normalization() -> None:
    body = {"schemas": [SEARCH_BODY], **{f"extension{index}": "x" for index in range(16)}}
    async with directory().client() as client:
        response = await client.post("/scim/v2/Users/.search", json=body, headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidSyntax"
    assert "at most 16 members" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_query_bytes_are_bounded_before_decoding() -> None:
    query = "extension=" + "x" * 8192
    async with directory().client() as client:
        response = await client.get(f"/scim/v2/Users?{query}", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "invalidSyntax"
    assert "at most 8192 bytes" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_and_search_refuse_ignored_or_incomplete_query_parameters() -> None:
    async with directory().client() as client:
        unknown = await client.get("/scim/v2/Users?filtre=userName%20pr", headers=AUTH)
        projection = await client.get("/scim/v2/Users?attributes=userName", headers=AUTH)
        get_order = await client.get("/scim/v2/Users?sortOrder=descending", headers=AUTH)
        search_order = await client.post(
            "/scim/v2/Users/.search",
            json={"schemas": [SEARCH_BODY], "sortOrder": "descending"},
            headers=AUTH,
        )
    assert unknown.status == 400
    assert unknown.json()["scimType"] == "invalidSyntax"
    assert projection.status == 400
    assert projection.json()["scimType"] == "invalidValue"
    assert get_order.status == 400
    assert get_order.json()["scimType"] == "invalidValue"
    assert search_order.status == 400
    assert search_order.json()["scimType"] == "invalidValue"


@pytest.mark.asyncio
async def test_individual_resources_do_not_silently_ignore_query_parameters() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        user_projection = await client.get(
            "/scim/v2/Users/1?attributes=userName",
            headers=AUTH,
        )
        user_filter = await client.get(
            "/scim/v2/Users/1?filter=userName%20pr",
            headers=AUTH,
        )
        group_projection = await client.get(
            "/scim/v2/Groups/admin?excludedAttributes=members",
            headers=AUTH,
        )
    for response in (user_projection, user_filter, group_projection):
        assert response.status == 400
        assert response.json()["scimType"] == "invalidValue"


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
async def test_an_explicitly_empty_filter_is_invalid_for_get_and_search() -> None:
    async with directory().client() as client:
        get_response = await client.get("/scim/v2/Users?filter=", headers=AUTH)
        search_response = await client.post(
            "/scim/v2/Users/.search",
            json={"schemas": [SEARCH_BODY], "filter": ""},
            headers=AUTH,
        )
    assert get_response.status == 400
    assert get_response.json()["scimType"] == "invalidFilter"
    assert search_response.status == 400
    assert search_response.json()["scimType"] == "invalidFilter"


@pytest.mark.asyncio
async def test_a_successful_filter_parse_is_reused_by_its_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath._scim import router as router_module

    original = router_module.parse_filter
    calls = 0

    def counted(source: str, *, attributes: frozenset[str] | None = None) -> Any:
        nonlocal calls
        calls += 1
        return original(source, attributes=attributes)

    monkeypatch.setattr(router_module, "parse_filter", counted)
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        path = '/scim/v2/Users?filter=userName eq "alice@example.com"'
        first = await client.get(path, headers=AUTH)
        second = await client.get(path, headers=AUTH)

    assert first.json() == second.json()
    assert calls == 1


@pytest.mark.asyncio
async def test_filter_caches_do_not_cross_resource_attribute_vocabularies() -> None:
    async with directory().client() as client:
        group = await client.get("/scim/v2/Groups?filter=displayName pr", headers=AUTH)
        user = await client.get("/scim/v2/Users?filter=displayName pr", headers=AUTH)

    assert group.status == 200
    assert user.status == 400
    assert user.json()["scimType"] == "invalidFilter"


@pytest.mark.asyncio
async def test_a_filter_on_an_attribute_this_provider_lacks_is_refused() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        response = await client.get('/scim/v2/Users?filter=externalId eq "abc"', headers=AUTH)
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
        groups = await client.get("/scim/v2/Groups?filter=members pr", headers=AUTH)
        unfiltered_groups = await client.get("/scim/v2/Groups", headers=AUTH)
        one_group = await client.get("/scim/v2/Groups/admin", headers=AUTH)
        group_write = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "add", "path": "members", "value": [{"value": "1"}]}
                ],
            },
            headers=AUTH,
        )
        unfiltered = await client.get("/scim/v2/Users", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"
    assert groups.status == 400
    assert groups.json()["scimType"] == "tooMany"
    assert unfiltered_groups.status == 400
    assert unfiltered_groups.json()["scimType"] == "tooMany"
    assert one_group.status == 400
    assert one_group.json()["scimType"] == "tooMany"
    assert group_write.status == 400
    assert group_write.json()["scimType"] == "tooMany"
    # An ordinary user page reads only the users on that page and does not
    # materialize nested role membership, so it remains available over the
    # ceiling. Group resources and filtered users expand the whole candidate
    # set and refuse instead.
    assert unfiltered.status == 200
    assert unfiltered.json()["totalResults"] == 2


@pytest.mark.asyncio
async def test_a_filtered_user_list_counts_nested_role_assignments() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=3)
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"admin", "member"})
        await fixture.organizations.add_member("acme", "2", roles={"billing", "member"})
        response = await client.get("/scim/v2/Users?filter=userName%20pr", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"
    assert "4 role assignments" in response.json()["detail"]


@pytest.mark.asyncio
async def test_one_user_resource_refuses_an_oversized_role_set_before_writing() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=1)
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"admin", "member"})
        listed = await client.get("/scim/v2/Users", headers=AUTH)
        read = await client.get("/scim/v2/Users/1", headers=AUTH)
        write = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {"op": "replace", "path": "userName", "value": "changed@example.com"}
                ],
            },
            headers=AUTH,
        )
    for response in (listed, read, write):
        assert response.status == 400
        assert response.json()["scimType"] == "tooMany"
    record = await fixture.users.get_by_id("1")
    assert record is not None
    assert record.email == "alice@example.com"


@pytest.mark.asyncio
async def test_a_group_write_cannot_cross_the_role_assignment_ceiling() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=3)
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await provision(client, "bob@example.com")
        await fixture.organizations.add_member("acme", "1", roles={"billing", "member"})
        await fixture.organizations.add_member("acme", "2", roles={"billing"})
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
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"
    memberships = await fixture.organizations.members("acme")
    assert memberships[0].roles == frozenset({"billing", "member"})


@pytest.mark.asyncio
async def test_a_group_write_bounds_duplicate_input_members_before_mutation() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=3)
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [
                    {
                        "op": "replace",
                        "path": "members",
                        "value": [{"value": "1"}] * 4,
                    }
                ],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"
    memberships = await fixture.organizations.members("acme")
    assert memberships[0].roles == frozenset()


@pytest.mark.asyncio
async def test_group_budget_counts_members_independently() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=3)
    for index in range(4):
        record = await fixture.users.create(f"user{index}@example.com", "unused")
        await fixture.organizations.add_member("acme", record.id)
    async with fixture.client() as client:
        response = await client.get("/scim/v2/Groups", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"


@pytest.mark.asyncio
async def test_group_budget_counts_roles_independently() -> None:
    fixture = directory()
    organizations = InMemoryOrganizationStore(roles={"a", "b", "c", "d"})
    fixture.app.include_router(
        scim_router(
            fixture.app,
            users=fixture.users,
            organizations=organizations,
            organization="acme",
            prefix="/scim/v3",
            max_filter_scan=3,
        )
    )
    async with fixture.client() as client:
        response = await client.get("/scim/v3/Groups", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"


@pytest.mark.asyncio
async def test_group_budget_counts_role_assignments_independently() -> None:
    fixture = Provisioned(Identity("directory"), max_filter_scan=3)
    first = await fixture.users.create("alice@example.com", "unused")
    second = await fixture.users.create("bob@example.com", "unused")
    await fixture.organizations.add_member("acme", first.id, roles=ROLES)
    await fixture.organizations.add_member("acme", second.id, roles={"member"})
    async with fixture.client() as client:
        response = await client.get("/scim/v2/Groups", headers=AUTH)
    assert response.status == 400
    assert response.json()["scimType"] == "tooMany"


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
    assert [row["userName"] for row in response.json()["Resources"]] == ["bob@example.com"]


@pytest.mark.asyncio
async def test_sorting_handles_scalar_list_boolean_and_missing_values() -> None:
    fixture = directory()
    async with fixture.client() as client:
        for name in ("zoe@example.com", "zara@example.com", "amy@example.com"):
            await provision(client, name)
        await client.patch(
            "/scim/v2/Users/2",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=AUTH,
        )
        await fixture.organizations.add_member("acme", "1", roles={"admin"})
        names = await client.get("/scim/v2/Users?sortBy=userName", headers=AUTH)
        active = await client.get("/scim/v2/Users?sortBy=active", headers=AUTH)
        groups = await client.get("/scim/v2/Users?sortBy=groups&sortOrder=descending", headers=AUTH)

    assert [row["userName"] for row in names.json()["Resources"]] == [
        "amy@example.com",
        "zara@example.com",
        "zoe@example.com",
    ]
    assert [row["id"] for row in active.json()["Resources"]] == ["2", "1", "3"]
    assert [row["id"] for row in groups.json()["Resources"]] == ["1", "2", "3"]


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
        attribute = await client.get("/scim/v2/Users?sortBy=displayName", headers=AUTH)
        order = await client.get("/scim/v2/Users?sortBy=userName&sortOrder=sideways", headers=AUTH)
        sub_attribute = await client.get("/scim/v2/Users?sortBy=meta.location", headers=AUTH)
    assert attribute.status == 400
    assert attribute.json()["scimType"] == "invalidValue"
    assert order.status == 400
    assert "ascending" in order.json()["detail"]
    assert sub_attribute.status == 400
    assert "meta.location" in sub_attribute.json()["detail"]


@pytest.mark.asyncio
async def test_a_user_outside_this_organisation_is_not_found_rather_than_forbidden() -> None:
    fixture = directory()
    other = await fixture.users.create("bob@example.com", "scrypt$1$1$1$a$b")
    await fixture.organizations.add_member("globex", other.id)
    async with fixture.client() as client:
        response = await client.get(f"/scim/v2/Users/{other.id}", headers=AUTH)
    assert response.status == 404


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
                "Operations": [{"op": "replace", "path": "userName", "value": "bob@example.com"}],
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
    assert fixture.revoked_session_users == ["1"]


@pytest.mark.asyncio
async def test_reactivating_a_user_revokes_sessions_that_survived_deactivation() -> None:
    fixture = directory()
    async with fixture.client() as client:
        await provision(client, "alice@example.com")
        await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=AUTH,
        )
        fixture.revoked_session_users.clear()
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
            headers=AUTH,
        )
    assert response.status == 200
    assert response.json()["active"] is True
    assert fixture.revoked_session_users == ["1"]


@pytest.mark.asyncio
async def test_deactivating_a_user_who_belongs_elsewhere_is_refused() -> None:
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
    assert fixture.revoked_session_users == ["1"]


@pytest.mark.asyncio
async def test_patching_a_read_only_attribute_is_refused() -> None:
    async with directory().client() as client:
        await provision(client, "alice@example.com")
        response = await client.patch(
            "/scim/v2/Users/1",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "add", "path": "groups", "value": [{"value": "admin"}]}],
            },
            headers=AUTH,
        )
    assert response.status == 400
    assert response.json()["scimType"] == "mutability"


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
                "Operations": [{"op": "add", "path": "members", "value": [{"value": "1"}]}],
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
    fixture = directory()
    stranger = await fixture.users.create("bob@example.com", "scrypt$1$1$1$a$b")
    async with fixture.client() as client:
        response = await client.patch(
            "/scim/v2/Groups/admin",
            json={
                "schemas": [PATCH_BODY],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": stranger.id}]}],
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
    ids=(
        "typed-user-name",
        "blank-user-name",
        "typed-active",
        "typed-password",
        "blank-password",
    ),
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
@pytest.mark.parametrize(
    "member",
    [{"value": 7}, {"value": ""}, {"display": "no value"}],
    ids=("typed-value", "empty-value", "missing-value"),
)
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
        broken = await client.patch("/scim/v2/Groups/admin", content=b"{not json", headers=AUTH)
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
        await client.post("/scim/v3/Users", json={"userName": "alice@example.com"}, headers=AUTH)
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
        ({"page_size": True}, "page_size must be an integer"),
        ({"page_size": 1.5}, "page_size must be an integer"),
        (
            {"page_size": 1, "max_page_size": 2.5},
            "max_page_size must be an integer",
        ),
        ({"max_filter_scan": True}, "max_filter_scan must be an integer"),
        ({"cursor_timeout": True}, "cursor_timeout must be an integer"),
        ({"cursor_timeout": 1.5}, "cursor_timeout must be an integer"),
        (
            {"cursor_secret": bytearray(b"x" * 32)},
            "cursor_secret must be bytes, str, or None",
        ),
        ({"cursor_secret": 7}, "cursor_secret must be bytes, str, or None"),
        ({"revoke_sessions": 7}, "revoke_sessions must be callable or None"),
    ],
    ids=(
        "empty-read-action",
        "empty-write-action",
        "zero-page-size",
        "zero-maximum-page-size",
        "page-size-over-maximum",
        "zero-filter-scan",
        "boolean-page-size",
        "float-page-size",
        "float-maximum-page-size",
        "boolean-filter-scan",
        "boolean-cursor-timeout",
        "float-cursor-timeout",
        "mutable-cursor-secret",
        "invalid-cursor-secret-type",
        "invalid-session-revoker",
    ),
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


def test_scim_router_refuses_a_non_callable_organization_resolver() -> None:
    fixture = directory()
    with pytest.raises(ValueError, match="organization must be a string or callable"):
        scim_router(
            fixture.app,
            users=fixture.users,
            organizations=fixture.organizations,
            organization=cast("Any", 7),
        )


def test_scim_sort_value_prefers_a_primary_mapping() -> None:
    value = [
        {"value": "fallback", "primary": False},
        {"value": "Chosen", "primary": True},
    ]
    assert _sortable_value(value) == (False, "chosen")


def test_scim_sort_value_ignores_non_mapping_primary_candidates() -> None:
    value = [object(), {"value": "Chosen", "primary": True}]
    assert _sortable_value(value) == (False, "chosen")


def test_scim_sort_value_falls_back_to_the_first_or_missing_value() -> None:
    assert _sortable_value(["First", "second"]) == (False, "first")
    assert _sortable_value([]) == (True, "")


def test_scim_sort_value_keeps_boolean_order_distinct_from_text() -> None:
    assert _sortable_value(True) == (False, "1")
    assert _sortable_value(False) == (False, "0")


@pytest.mark.asyncio
async def test_one_mount_can_serve_a_tenant_per_path_segment() -> None:
    users = InMemoryUserStore()
    organizations = InMemoryOrganizationStore(roles=ROLES)
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("directory")),
        CedarAuthorizer(engine=CedarPolicies(POLICY), organizations=Memberships(organizations)),
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
async def test_the_authorized_organization_and_written_organization_are_one_resolution() -> None:
    users = InMemoryUserStore()
    organizations = InMemoryOrganizationStore(roles=ROLES)
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("directory")),
        CedarAuthorizer(
            engine=CedarPolicies(POLICY),
            organizations=Memberships(organizations),
        ),
    )
    resolved = iter(("acme", "globex"))
    app.include_router(
        scim_router(
            app,
            users=users,
            organizations=organizations,
            organization=lambda request: next(resolved),
            prefix="/scim/v3",
        )
    )
    async with TestClient(app) as client:
        response = await client.post(
            "/scim/v3/Users",
            json={"userName": "alice@example.com"},
            headers=AUTH,
        )
    assert response.status == 201
    assert [member.user_id for member in await organizations.members("acme")] == ["1"]
    assert await organizations.members("globex") == ()


@pytest.mark.asyncio
async def test_the_member_directory_lists_one_organisation_in_user_id_order() -> None:
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
    async with directory().client() as client:
        created = await client.post(
            "/scim/v2/Groups", json={"displayName": "auditor"}, headers=AUTH
        )
        deleted = await client.delete("/scim/v2/Groups/admin", headers=AUTH)
    assert created.status == 501
    assert "declared vocabulary" in created.json()["detail"]
    assert deleted.status == 501
    assert "cannot be deleted over SCIM" in deleted.json()["detail"]
