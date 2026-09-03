from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from wreath import Wreath
from wreath._orm_events import publish_write
from wreath.auth import Identity
from wreath.authorization import (
    CedarAuthorizer,
    CedarPolicies,
    EntityUid,
    LiveDocument,
    authorize,
    declared_actions,
    permission_document,
    permissions_router,
)
from wreath.policy import HttpPolicy
from wreath.testing import TestClient

POLICIES = """
    permit(principal in Role::"editor", action == Action::"Llama::read", resource);
    permit(principal in Role::"editor", action == Action::"Llama::edit", resource);
    permit(principal in Role::"admin", action, resource);
    permit(principal, action == Action::"Trek::read", resource);
"""


class _Backend:
    """Authenticates `Bearer <name>:<role>,<role>`; roles drive the policies."""

    scheme = "Bearer"

    def challenge(self, request: Any) -> str:
        return "Bearer"

    async def authenticate(self, request: Any) -> Identity | None:
        header = request.header("authorization")
        if not header or not header.startswith("Bearer "):
            return None
        name, _, roles = header[7:].partition(":")
        return Identity(name, roles=frozenset(r for r in roles.split(",") if r))


def _app(*, mount: bool = True) -> Wreath:
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    @authorize(
        action="Llama::read",
        resource=lambda request: EntityUid("Llama", request.path_params["llama_id"]),
    )
    async def read_llama(request) -> dict:
        return {"id": request.path_params["llama_id"]}

    @app.patch("/llamas/{llama_id}")
    @authorize(
        action="Llama::edit",
        resource=lambda request: EntityUid("Llama", request.path_params["llama_id"]),
    )
    async def edit_llama(request) -> dict:
        return {"ok": True}

    @app.delete("/llamas/{llama_id}")
    @authorize(
        action="Llama::delete",
        resource=lambda request: EntityUid("Llama", request.path_params["llama_id"]),
    )
    async def delete_llama(request) -> dict:
        return {"ok": True}

    @app.get("/treks/{trek_id}")
    @authorize(
        action="Trek::read",
        resource=lambda request: EntityUid("Trek", request.path_params["trek_id"]),
    )
    async def read_trek(request) -> dict:
        return {"ok": True}

    @app.get("/health")  # no policy: not a permission
    async def health(request) -> dict:
        return {"ok": True}

    app.configure_auth(_Backend(), CedarAuthorizer(engine=CedarPolicies(POLICIES)))
    if mount:
        app.include_router(permissions_router(app))
    return app


# Testing authorization means running the same request as several people. Doing
# that with headers means every test carries a token-shaped literal that has
# nothing to do with what it is checking.


@pytest.mark.asyncio
async def test_a_client_can_act_as_a_role_without_a_token() -> None:
    async with TestClient(_app()) as client:
        rider = client.acting_as("bo", roles=["rider"])
        editor = client.acting_as("ada", roles=["editor"])

        assert (await rider.get("/llamas/7")).status == 403
        assert (await editor.get("/llamas/7")).status == 200


@pytest.mark.asyncio
async def test_acting_as_reaches_the_permissions_endpoint_too() -> None:
    async with TestClient(_app()) as client:
        admin = client.acting_as("root", roles=["admin"])
        body = (await admin.post("/permissions", json={"type": "Llama", "ids": ["7"]})).json()

    assert body["permissions"]["7"] == ["Llama::delete", "Llama::edit", "Llama::read"]


@pytest.mark.asyncio
async def test_two_actors_do_not_interfere() -> None:
    async with TestClient(_app()) as client:
        admin = client.acting_as("root", roles=["admin"])
        rider = client.acting_as("bo", roles=["rider"])

        first, second, third = (
            await admin.delete("/llamas/7"),
            await rider.delete("/llamas/7"),
            await admin.delete("/llamas/7"),
        )

    assert (first.status, second.status, third.status) == (200, 403, 200)


@pytest.mark.asyncio
async def test_an_identity_can_be_passed_whole() -> None:
    async with TestClient(_app()) as client:
        editor = client.acting_as(Identity("ada", roles=frozenset({"editor"})))
        assert (await editor.get("/llamas/7")).status == 200


@pytest.mark.asyncio
async def test_mixing_an_identity_and_roles_is_refused() -> None:
    async with TestClient(_app()) as client:
        with pytest.raises(TypeError, match="not both"):
            client.acting_as(Identity("ada"), roles=["editor"])


@pytest.mark.asyncio
async def test_a_plain_client_is_still_anonymous() -> None:
    async with TestClient(_app()) as client:
        client.acting_as("root", roles=["admin"])  # exists, unused
        assert (await client.get("/llamas/7")).status == 401


@pytest.mark.asyncio
async def test_the_applications_own_backend_is_restored_afterwards() -> None:
    app = _app()
    original = app._auth_backend
    async with TestClient(app) as client:
        client.acting_as("root", roles=["admin"])
        assert app._auth_backend is not original
    assert app._auth_backend is original


@pytest.mark.asyncio
async def test_default_headers_ride_every_request() -> None:
    async with TestClient(_app()) as client:
        editor = client.with_headers(authorization="Bearer ada:editor")
        assert (await editor.get("/llamas/7")).status == 200


def test_the_actions_are_read_off_the_routes() -> None:
    assert declared_actions(_app()) == {
        "Llama": ("Llama::delete", "Llama::edit", "Llama::read"),
        "Trek": ("Trek::read",),
    }


def test_a_route_with_no_policy_contributes_nothing() -> None:
    vocabulary = declared_actions(_app())
    assert not any("health" in action for actions in vocabulary.values() for action in actions)


def test_an_app_with_no_policies_has_an_empty_vocabulary() -> None:
    app = Wreath()

    @app.get("/open")
    async def open_route(request) -> dict:
        return {}

    assert declared_actions(app) == {}


@pytest.mark.asyncio
async def test_the_endpoint_publishes_the_vocabulary() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.get("/permissions", headers={"authorization": "Bearer ada:editor"})
        ).json()

    assert body["resources"] == {
        "Llama": ["Llama::delete", "Llama::edit", "Llama::read"],
        "Trek": ["Trek::read"],
    }


@pytest.mark.asyncio
async def test_the_vocabulary_is_not_handed_to_anonymous_callers() -> None:
    async with TestClient(_app()) as client:
        assert (await client.get("/permissions")).status == 401


@pytest.mark.asyncio
async def test_the_vocabulary_is_still_the_same_list_for_every_caller() -> None:
    async with TestClient(_app()) as client:
        rider = client.acting_as("bo", roles=["rider"])
        admin = client.acting_as("root", roles=["admin"])

        assert (await rider.get("/permissions")).json() == (await admin.get("/permissions")).json()


@pytest.mark.asyncio
async def test_an_editor_gets_exactly_what_the_policies_allow() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.post(
                "/permissions",
                json={"type": "Llama", "ids": ["7"]},
                headers={"authorization": "Bearer ada:editor"},
            )
        ).json()

    assert body["permissions"] == {"7": ["Llama::edit", "Llama::read"]}


@pytest.mark.asyncio
async def test_an_admin_gets_everything() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.post(
                "/permissions",
                json={"type": "Llama", "ids": ["7"]},
                headers={"authorization": "Bearer root:admin"},
            )
        ).json()

    assert body["permissions"]["7"] == ["Llama::delete", "Llama::edit", "Llama::read"]


@pytest.mark.asyncio
async def test_a_reader_gets_nothing_on_llamas_but_something_on_treks() -> None:
    async with TestClient(_app()) as client:
        headers = {"authorization": "Bearer bob:"}
        llamas = (
            await client.post("/permissions", json={"type": "Llama", "ids": ["7"]}, headers=headers)
        ).json()
        treks = (
            await client.post("/permissions", json={"type": "Trek", "ids": ["3"]}, headers=headers)
        ).json()

    assert llamas["permissions"] == {"7": []}
    assert treks["permissions"] == {"3": ["Trek::read"]}


@pytest.mark.asyncio
async def test_one_call_answers_for_a_whole_list() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.post(
                "/permissions",
                json={"type": "Llama", "ids": [str(n) for n in range(50)]},
                headers={"authorization": "Bearer ada:editor"},
            )
        ).json()

    assert len(body["permissions"]) == 50
    assert all(
        allowed == ["Llama::edit", "Llama::read"] for allowed in body["permissions"].values()
    )


def _bounded_app(max_ids: int) -> Wreath:
    """`_app`, with the batch endpoint's cardinality ceiling set explicitly."""
    app = _app(mount=False)
    app.include_router(permissions_router(app, max_ids=max_ids))
    return app


@pytest.mark.asyncio
async def test_a_list_past_the_ceiling_is_refused_and_the_limit_is_named() -> None:
    async with TestClient(_bounded_app(3)) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": ["1", "2", "3", "4"]},
            headers={"authorization": "Bearer ada:editor"},
        )

    assert response.status == 400
    assert "3" in response.json()["detail"]


@pytest.mark.asyncio
async def test_the_ceiling_refuses_rather_than_truncating() -> None:
    async with TestClient(_bounded_app(3)) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": ["1", "2", "3", "4"]},
            headers={"authorization": "Bearer ada:editor"},
        )

    assert "permissions" not in response.json()


@pytest.mark.asyncio
async def test_a_list_exactly_at_the_ceiling_is_answered() -> None:
    async with TestClient(_bounded_app(3)) as client:
        body = (
            await client.post(
                "/permissions",
                json={"type": "Llama", "ids": ["1", "2", "3"]},
                headers={"authorization": "Bearer ada:editor"},
            )
        ).json()

    assert len(body["permissions"]) == 3


@pytest.mark.asyncio
async def test_the_default_ceiling_is_a_generous_ui_page() -> None:
    async with TestClient(_app()) as client:
        headers = {"authorization": "Bearer ada:editor"}
        allowed = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": [str(n) for n in range(200)]},
            headers=headers,
        )
        refused = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": [str(n) for n in range(201)]},
            headers=headers,
        )

    assert allowed.status == 200 and len(allowed.json()["permissions"]) == 200
    assert refused.status == 400
    assert "200" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_the_ceiling_is_checked_before_the_resource_type_is_looked_up() -> None:
    async with TestClient(_bounded_app(1)) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Alpaca", "ids": ["1", "2"]},
            headers={"authorization": "Bearer root:admin"},
        )

    assert response.status == 400
    assert "1" in response.json()["detail"] and "Alpaca" not in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_caller_can_narrow_the_actions_it_cares_about() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.post(
                "/permissions",
                json={"type": "Llama", "ids": ["7"], "actions": ["Llama::delete"]},
                headers={"authorization": "Bearer ada:editor"},
            )
        ).json()

    assert body["permissions"] == {"7": []}


@pytest.mark.asyncio
async def test_every_requested_action_must_be_a_string() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": ["7"], "actions": ["Llama::read", 7]},
            headers={"authorization": "Bearer root:admin"},
        )

    assert response.status == 400
    assert response.json()["detail"] == "`actions` must be a list of strings"


@pytest.mark.asyncio
async def test_an_action_outside_the_declared_set_is_refused() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": ["7"], "actions": ["Llama::purge"]},
            headers={"authorization": "Bearer root:admin"},
        )

    assert response.status == 400
    assert "Llama::purge" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_unknown_resource_type_is_refused() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Alpaca", "ids": ["7"]},
            headers={"authorization": "Bearer root:admin"},
        )
    assert response.status == 400


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_told_to_authenticate() -> None:
    async with TestClient(_app()) as client:
        response = await client.post("/permissions", json={"type": "Llama", "ids": ["7"]})
    assert response.status == 401


@pytest.mark.asyncio
async def test_the_answer_is_never_cached_by_a_shared_cache() -> None:
    async with TestClient(_app()) as client:
        response = await client.post(
            "/permissions",
            json={"type": "Llama", "ids": ["7"]},
            headers={"authorization": "Bearer ada:editor"},
        )
    assert b"private" in response.header("cache-control", "").encode()


@pytest.mark.asyncio
async def test_the_endpoint_needs_an_authorizer() -> None:
    app = Wreath()
    app.include_router(permissions_router(app))

    @app.get("/x")
    @authorize(action="Llama::read", resource="Llama")
    async def x(request) -> dict:
        return {}

    async with TestClient(app) as client:
        response = await client.post("/permissions", json={"type": "Llama", "ids": ["7"]})
    assert response.status in (401, 500)


@pytest.mark.asyncio
async def test_the_manifest_says_what_this_user_can_ever_do() -> None:
    async with TestClient(_app()) as client:
        body = (
            await client.get(
                "/permissions/manifest", headers={"authorization": "Bearer ada:editor"}
            )
        ).json()

    assert body["principal"] == "ada"
    assert sorted(body["roles"]) == ["editor"]
    assert body["allowed"] == {
        "Llama": ["Llama::edit", "Llama::read"],
        "Trek": ["Trek::read"],
    }


@pytest.mark.asyncio
async def test_the_manifest_differs_by_principal() -> None:
    async with TestClient(_app()) as client:
        editor = (
            await client.get(
                "/permissions/manifest", headers={"authorization": "Bearer ada:editor"}
            )
        ).json()
        admin = (
            await client.get(
                "/permissions/manifest", headers={"authorization": "Bearer root:admin"}
            )
        ).json()

    assert admin["allowed"]["Llama"] != editor["allowed"]["Llama"]
    assert "Llama::delete" in admin["allowed"]["Llama"]


@pytest.mark.asyncio
async def test_the_manifest_is_revalidatable_so_the_client_stops_asking() -> None:
    async with TestClient(_app()) as client:
        headers = {"authorization": "Bearer ada:editor"}
        first = await client.get("/permissions/manifest", headers=headers)
        etag = first.header("etag")
        again = await client.get(
            "/permissions/manifest", headers={**headers, "if-none-match": etag}
        )

    assert first.status == 200 and etag
    assert again.status == 304
    assert again.body == b""


@pytest.mark.asyncio
async def test_manifest_revalidation_combines_repeated_if_none_match_fields() -> None:
    async with TestClient(_app()) as client:
        first = await client.get(
            "/permissions/manifest",
            headers={"authorization": "Bearer ada:editor"},
        )
        etag = first.header("etag")
        again = await client.get(
            "/permissions/manifest",
            headers=[
                ("authorization", "Bearer ada:editor"),
                ("if-none-match", '"not-current"'),
                ("if-none-match", etag or ""),
            ],
        )

    assert etag
    assert again.status == 304


@pytest.mark.asyncio
async def test_the_manifest_etag_changes_when_the_users_roles_do() -> None:
    async with TestClient(_app()) as client:
        before = (
            await client.get(
                "/permissions/manifest", headers={"authorization": "Bearer ada:editor"}
            )
        ).header("etag")
        after = (
            await client.get(
                "/permissions/manifest",
                headers={"authorization": "Bearer ada:editor,admin"},
            )
        ).header("etag")

    assert before != after


@pytest.mark.asyncio
async def test_the_manifest_etag_changes_when_the_policies_do() -> None:
    async def tags(policies: str) -> str:
        app = _app()
        app.configure_auth(_Backend(), CedarAuthorizer(engine=CedarPolicies(policies)))
        async with TestClient(app) as client:
            response = await client.get(
                "/permissions/manifest",
                headers={"authorization": "Bearer ada:editor"},
            )
            return response.header("etag")

    assert await tags(POLICIES) != await tags(
        POLICIES + '\npermit(principal in Role::"editor", '
        'action == Action::"Llama::delete", resource);'
    )


@pytest.mark.asyncio
async def test_the_manifest_is_private_and_anonymous_callers_are_refused() -> None:
    async with TestClient(_app()) as client:
        assert (await client.get("/permissions/manifest")).status == 401
        response = await client.get(
            "/permissions/manifest", headers={"authorization": "Bearer ada:editor"}
        )
        assert b"private" in response.header("cache-control", "").encode()


class _OpaqueEngine:
    """A third-party `CedarEngine`: satisfies the protocol, exposes nothing else.

    `__slots__` and no `__weakref__` is the ordinary shape for such a class, and
    it is also the shape that makes CPython's address reuse deterministic --
    freeing one instance hands the next allocation the very same block.
    """

    __slots__ = ("_seq",)

    def __init__(self, seq: int) -> None:
        self._seq = seq

    def is_authorized(self, **kwargs: Any) -> Any:
        raise NotImplementedError


class _EngineHolder:
    """An authorizer, as far as `_policy_fingerprint` is concerned."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Any) -> None:
        self._engine = engine


def test_a_replaced_engine_never_carries_the_tag_of_the_one_it_replaced() -> None:
    from wreath._auth.permissions import _policy_fingerprint

    first = _OpaqueEngine(1)
    address = id(first)
    before = _policy_fingerprint(_EngineHolder(first))
    del first
    after = _policy_fingerprint(_EngineHolder(_OpaqueEngine(2)))

    assert before != after
    assert before != repr(address).encode("ascii")


def test_the_built_in_engine_is_fingerprinted_through_its_public_surface() -> None:
    from wreath._auth.permissions import _policy_fingerprint

    engine = CedarPolicies(POLICIES)
    assert _policy_fingerprint(engine) == POLICIES.encode("utf-8")
    assert "_source" not in _policy_fingerprint.__code__.co_consts


def test_the_built_in_authorizer_is_fingerprinted_without_digging_out_its_engine() -> None:
    from wreath._auth.permissions import _policy_fingerprint

    authorizer = CedarAuthorizer(engine=CedarPolicies(POLICIES))

    assert _policy_fingerprint(authorizer) == POLICIES.encode("utf-8")


def test_the_fingerprint_still_knows_the_private_engine_name() -> None:
    assert "_engine" in CedarAuthorizer.__slots__


def test_two_authorizers_over_one_engine_agree_on_the_tag() -> None:
    from wreath._auth.permissions import _policy_fingerprint

    engine = _OpaqueEngine(4)

    assert _policy_fingerprint(CedarAuthorizer(engine=engine)) == _policy_fingerprint(
        CedarAuthorizer(engine=engine)
    )


def test_an_opaque_engines_tag_is_minted_once_not_per_read() -> None:
    from wreath._auth.permissions import _shared_fingerprint

    app = _app()
    app._authorizer = _EngineHolder(_OpaqueEngine(3))

    assert _shared_fingerprint(app) == _shared_fingerprint(app)


@pytest.mark.asyncio
async def test_two_workers_holding_the_same_policies_agree_on_the_etag() -> None:

    async def tag() -> str:
        async with TestClient(_app()) as client:
            return (
                await client.get(
                    "/permissions/manifest",
                    headers={"authorization": "Bearer ada:editor"},
                )
            ).header("etag")

    assert await tag() == await tag()


@pytest.mark.asyncio
async def test_a_route_declared_after_mounting_reaches_the_vocabulary() -> None:
    app = Wreath()
    app.configure_auth(_Backend(), CedarAuthorizer(engine=CedarPolicies(POLICIES)))
    app.include_router(permissions_router(app))

    @app.get("/alpacas/{alpaca_id}")
    @authorize(
        action="Alpaca::read",
        resource=lambda request: EntityUid("Alpaca", request.path_params["alpaca_id"]),
    )
    async def read_alpaca(request) -> dict:
        return {}

    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        body = (await editor.get("/permissions")).json()

    assert body["resources"] == {"Alpaca": ["Alpaca::read"]}


def test_the_vocabulary_memo_notices_a_route_replaced_in_place() -> None:
    import dataclasses

    from wreath._auth.permissions import _vocabulary_reader
    from wreath._auth.requirements import AuthRequirement

    async def unpoliced(request) -> dict:
        return {}

    app = _app(mount=False)
    read = _vocabulary_reader(app)
    assert "Llama" in read()

    app._routes[:] = [
        dataclasses.replace(route, endpoint=unpoliced, requirement=AuthRequirement())
        for route in app._routes
    ]
    assert read() == {}


def test_the_vocabulary_memo_answers_the_same_dict_while_the_routes_hold() -> None:
    from wreath._auth.permissions import _vocabulary_reader

    read = _vocabulary_reader(_app())
    assert read() is read()


# The manifest half stops the client asking; this half tells it when to ask
# again. The test client buffers a response until the application finishes, so
# these end the stream by closing the document -- which is what a disconnected
# browser does in production, one layer down.


def _streaming_app(**kwargs: Any) -> tuple[Wreath, LiveDocument]:
    """An app whose permission stream is wired to a document the test holds."""
    app = _app(mount=False)
    document = permission_document(app, **kwargs)
    app.include_router(permissions_router(app, document=document))
    return app, document


async def _stream(client: Any, document: LiveDocument, poke: Any) -> Any:
    """Open the stream as ``client``, run ``poke()`` once subscribed, then end it."""

    async def drive() -> None:
        deadline = asyncio.get_running_loop().time() + 1.0
        while not document.subscribers:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the stream never subscribed")
            await asyncio.sleep(0)
        poke()
        document.close_all()

    response, _ = await asyncio.gather(client.get("/permissions/stream"), drive())
    return response


def _changes(response: Any) -> list[dict[str, Any]]:
    """The `data:` payloads of an SSE body, keep-alive comments excluded."""
    return [
        json.loads(line[6:])
        for line in response.body.decode().splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_the_stream_is_a_private_event_stream() -> None:
    app, document = _streaming_app()
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        response = await _stream(editor, document, lambda: None)

    assert response.status == 200
    assert response.header("content-type") == "text/event-stream"
    # One `cache-control`, and it must say `private`: the manifest this stream
    # describes is per-principal, so no shared store may hold either.
    assert response.header("cache-control") == "private, no-cache, no-store"


@pytest.mark.asyncio
async def test_a_role_change_reaches_the_stream() -> None:
    app, document = _streaming_app(roles_model="Membership")
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        response = await _stream(editor, document, lambda: publish_write(frozenset({"Membership"})))

    assert _changes(response) == [{"reason": "roles", "etag": None}]


@pytest.mark.asyncio
async def test_a_role_change_carries_no_etag_because_the_one_we_hold_is_stale() -> None:
    app, document = _streaming_app(roles_model="Membership")
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        manifest = await editor.get("/permissions/manifest")
        response = await _stream(editor, document, lambda: publish_write(frozenset({"Membership"})))

    assert manifest.header("etag")  # the manifest can state a tag
    assert _changes(response)[0]["etag"] is None  # ... the stream must not


@pytest.mark.asyncio
async def test_a_write_to_an_unrelated_model_is_not_a_permission_change() -> None:
    app, document = _streaming_app(roles_model="Membership")
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        response = await _stream(editor, document, lambda: publish_write(frozenset({"Llama"})))

    assert _changes(response) == []


@pytest.mark.asyncio
async def test_a_policy_reload_carries_the_new_etag_so_a_client_can_skip() -> None:
    app, document = _streaming_app()
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        response = await _stream(editor, document, lambda: document.notify_all("policies"))
        current = (await editor.get("/permissions/manifest")).header("etag")

    # The roles we hold are still current on a policy change, so the tag is the
    # real one -- and it is the one the manifest itself would answer with.
    assert _changes(response) == [{"reason": "policies", "etag": current}]


@pytest.mark.asyncio
async def test_only_the_principal_whose_document_moved_is_told() -> None:
    app, document = _streaming_app()
    async with TestClient(app) as client:
        ada = client.acting_as("ada", roles=["editor"])
        response = await _stream(ada, document, lambda: document.notify("User::bo", "roles"))

    assert _changes(response) == []


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_open_a_stream() -> None:
    async with TestClient(_app()) as client:
        assert (await client.get("/permissions/stream")).status == 401


@pytest.mark.asyncio
async def test_the_stream_needs_an_authorizer() -> None:
    app = Wreath()
    app.include_router(permissions_router(app))

    @app.get("/x")
    @authorize(action="Llama::read", resource="Llama")
    async def x(request) -> dict:
        return {}

    async with TestClient(app) as client:
        assert (await client.get("/permissions/stream")).status in (401, 500)


@pytest.mark.asyncio
async def test_too_many_streams_is_refused_and_the_manifest_still_works() -> None:
    app, _document = _streaming_app(max_subscribers=0)
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        refused = await editor.get("/permissions/stream")
        manifest = await editor.get("/permissions/manifest")

    assert refused.status == 503
    assert manifest.status == 200


@pytest.mark.asyncio
async def test_a_stream_that_ends_leaves_nothing_behind() -> None:
    app, document = _streaming_app(roles_model="Membership")
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        await _stream(editor, document, lambda: None)

    assert document.subscribers == 0
    assert not document.watching  # and the ORM subscription went with it


def test_the_api_model_carries_the_permission_vocabulary() -> None:
    from wreath.typegen import build_api_model

    api = build_api_model(_app(), allow_unknown=True)
    by_type = {entry.resource_type: entry.actions for entry in api.permissions}
    assert by_type["Llama"] == ("Llama::delete", "Llama::edit", "Llama::read")


def test_the_typescript_target_emits_a_typed_permissions_module() -> None:
    from wreath.typegen import build_api_model
    from wreath.typegen.targets.typescript import render_typescript

    files = render_typescript(build_api_model(_app(), allow_unknown=True), react_query=True)
    source = files["permissions.ts"]

    # The union is the server's vocabulary, so a typo is a compile error.
    assert '"Llama::delete" | "Llama::edit" | "Llama::read"' in source
    # And the shape a component actually destructures.
    assert "canDelete" in source and "canEdit" in source and "canRead" in source
    assert "fetchPermissions" in source

    # The React hook is separate: it needs react-query, the fetcher does not.
    hook = files["use-permissions.ts"]
    assert "usePermissions" in hook and "usePermission" in hook
    assert "@tanstack/react-query" in hook


def test_no_permissions_module_when_the_api_declares_none() -> None:
    from wreath.typegen import build_api_model
    from wreath.typegen.targets.typescript import render_typescript

    app = Wreath()

    @app.get("/open")
    async def open_route(request) -> dict:
        return {}

    files = render_typescript(build_api_model(app, allow_unknown=True))
    assert "permissions.ts" not in files


@pytest.mark.parametrize(
    ("action", "expected"),
    [("Llama::read", "canRead"), ("Llama::force_sync", "canForceSync"), ("read", "canRead")],
)
def test_action_names_become_readable_flags(action: str, expected: str) -> None:
    from wreath.typegen.targets.typescript import permission_flag

    assert permission_flag(action) == expected


# Every answer here is one `authorizer.authorize` per (resource, action). With
# the built-in engine that is in-process and the order never matters. With a
# *remote* authorizer -- which the `CedarEngine` protocol invites -- each one is
# a round trip, and evaluating them one at a time makes a batch of two hundred
# ids over three actions six hundred round trips end to end.


class _CountingAuthorizer:
    """A real authorizer with a round trip in front of it, and a tally."""

    def __init__(self, inner: Any, delay: float = 0.0) -> None:
        self._inner = inner
        self._delay = delay
        self.calls = 0
        self.in_flight = 0
        self.peak = 0

    async def authorize(self, request: Any, requirement: Any) -> Any:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return await self._inner.authorize(request, requirement)
        finally:
            self.in_flight -= 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _counting_app(*, delay: float = 0.0, **router: Any) -> tuple[Wreath, Any]:
    app = _app(mount=False)
    counter = _CountingAuthorizer(app._authorizer, delay=delay)
    app.configure_auth(_Backend(), counter)
    app.include_router(permissions_router(app, **router))
    return app, counter


@pytest.mark.asyncio
async def test_the_manifest_evaluates_its_actions_concurrently() -> None:
    app, counter = _counting_app(delay=0.01)
    async with TestClient(app) as client:
        response = await client.acting_as("root", roles=["admin"]).get("/permissions/manifest")

    assert response.status == 200
    # Llama has three actions and Trek one; every one is still asked.
    assert counter.calls == 4
    # The finding: this was 1, so four round trips ran end to end.
    assert counter.peak > 1


@pytest.mark.asyncio
async def test_the_batch_bounds_concurrency_across_ids_not_just_actions() -> None:
    # ids is the axis that grows, so bounding per id would leave len(ids)
    # sequential rounds however small the limit.
    app, counter = _counting_app(delay=0.01, max_concurrency=8)
    async with TestClient(app) as client:
        response = await client.acting_as("root", roles=["admin"]).post(
            "/permissions", json={"type": "Llama", "ids": [str(n) for n in range(10)]}
        )

    assert response.status == 200
    assert counter.calls == 30  # 10 ids x 3 actions
    assert counter.peak == 8  # the declared ceiling, reached
    assert counter.peak <= 8  # and never exceeded


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_the_declared_ceiling() -> None:
    # The bound is the point: unbounded would make one batch a burst of
    # `max_ids x actions` against an authorization service that is not ours.
    app, counter = _counting_app(delay=0.005, max_concurrency=3)
    async with TestClient(app) as client:
        await client.acting_as("root", roles=["admin"]).post(
            "/permissions", json={"type": "Llama", "ids": [str(n) for n in range(12)]}
        )

    assert counter.calls == 36
    assert counter.peak == 3


@pytest.mark.asyncio
async def test_max_concurrency_one_restores_sequential_evaluation() -> None:
    # The escape hatch for an authorizer that cannot take concurrent calls
    # carrying one request.
    app, counter = _counting_app(delay=0.005, max_concurrency=1)
    async with TestClient(app) as client:
        await client.acting_as("root", roles=["admin"]).post(
            "/permissions", json={"type": "Llama", "ids": ["1", "2", "3"]}
        )

    assert counter.calls == 9
    assert counter.peak == 1


@pytest.mark.asyncio
async def test_concurrent_evaluation_keeps_every_answer_with_its_own_id() -> None:
    # Gathering is only safe if results come back attached to the right ask.
    # `bo` is a rider: the policies grant Trek::read to everyone and nothing on
    # Llama, so every id must answer identically -- and an off-by-one in the
    # reassembly would show as one id disagreeing with the rest.
    app, _ = _counting_app(delay=0.001, max_concurrency=4)
    async with TestClient(app) as client:
        editor = client.acting_as("ada", roles=["editor"])
        body = (
            await editor.post(
                "/permissions",
                json={
                    "type": "Llama",
                    "ids": [str(n) for n in range(9)],
                    "actions": ["Llama::read", "Llama::delete", "Llama::edit"],
                },
            )
        ).json()

    assert sorted(body["permissions"]) == sorted(str(n) for n in range(9))
    for identifier, granted in body["permissions"].items():
        assert granted == ["Llama::read", "Llama::edit"], identifier


@pytest.mark.asyncio
async def test_the_manifest_keeps_each_action_under_its_own_resource_type() -> None:
    app, _ = _counting_app(max_concurrency=4)
    async with TestClient(app) as client:
        body = (await client.acting_as("ada", roles=["editor"]).get("/permissions/manifest")).json()

    # Flattened for evaluation, reassembled per type: a shifted slice would put
    # a Llama action under Trek. Order is the vocabulary's, which is sorted.
    assert body["allowed"]["Llama"] == ["Llama::edit", "Llama::read"]
    assert body["allowed"]["Trek"] == ["Trek::read"]


@pytest.mark.asyncio
async def test_a_composed_cache_middleware_leaves_one_cache_control() -> None:
    # Two `cache-control` headers leaves a proxy reading the first, which would
    # be whatever the middleware set -- `public` on a per-principal answer.
    from wreath.cache_control import CacheControl
    from wreath.policy import CachePolicy

    app = _app(mount=False)
    app.configure_http_policy(
        HttpPolicy(cache_control=CachePolicy(CacheControl(public=True, max_age=60)))
    )
    app.include_router(permissions_router(app))

    async with TestClient(app) as client:
        admin = client.acting_as("root", roles=["admin"])
        for path in ("/permissions", "/permissions/manifest"):
            response = await admin.get(path)
            headers = [value for key, value in response.headers if key.lower() == b"cache-control"]
            assert headers == [b"private, no-store"], path
