from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._auth.permissions import (
    SURFACE_ACTIONS,
    _decide,
    _instance_token,
    _policy_fingerprint,
    _request_flags,
    _shared_fingerprint,
    declared_actions,
    permission_document,
    permissions_router,
)
from wreath._auth.requirements import AuthRequirement, PolicyRequirement


def _endpoint(router: Any, name: str) -> Any:
    return next(route.endpoint for route in router._routes if route.endpoint.__name__ == name)


class _Request:
    def __init__(self, identity: Any, payload: Any = None) -> None:
        self.identity = identity
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    def header(self, _name: str) -> None:
        return None


def _declared_app(*actions: str, image: bool = False) -> Any:
    async def endpoint(_request: Any) -> None:
        return None

    requirement = AuthRequirement(
        policies=tuple(PolicyRequirement(action, object()) for action in actions)
    )
    definition = SimpleNamespace(endpoint=endpoint, requirement=requirement)
    app = SimpleNamespace(_routes=[definition], _ws_routes=())
    if image:
        app._application_image = SimpleNamespace(
            routes=lambda: (definition,),
            requirements=lambda: (requirement,),
        )
    return app


def test_declared_actions_uses_the_compiled_application_image_when_present() -> None:
    app = _declared_app("Image::read", image=True)
    app._routes = []
    app._application_image.routes()[0].requirement = AuthRequirement()

    assert declared_actions(app) == {"Image": ("Image::read",)}


def test_declared_actions_uses_route_requirements_without_an_image() -> None:
    assert declared_actions(_declared_app("Route::read")) == {"Route": ("Route::read",)}


def test_declared_actions_ignores_a_definition_without_an_endpoint() -> None:
    app = _declared_app("Route::read", image=True)
    requirement = AuthRequirement(policies=(PolicyRequirement("Hidden::read", object()),))
    definition = SimpleNamespace(endpoint=None, requirement=requirement)
    app._application_image = SimpleNamespace(
        routes=lambda: (definition,),
        requirements=lambda: (requirement,),
    )

    assert declared_actions(app) == {}


def test_declared_actions_groups_an_unqualified_action_under_the_empty_type() -> None:
    assert declared_actions(_declared_app("read")) == {"": ("read",)}


def test_declared_actions_reads_a_bound_surfaces_live_vocabulary() -> None:
    class Surface:
        def endpoint(self, _request: Any) -> None:
            return None

        def actions(self) -> dict[str, tuple[str, ...]]:
            return {"Tool": ("Tool::run",)}

    surface = Surface()
    setattr(surface, SURFACE_ACTIONS, surface.actions)
    definition = SimpleNamespace(endpoint=surface.endpoint, requirement=AuthRequirement())
    app = SimpleNamespace(_routes=[definition], _ws_routes=())

    assert declared_actions(app) == {"Tool": ("Tool::run",)}


@pytest.mark.asyncio
async def test_decide_returns_immediately_for_an_empty_ask_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._auth.permissions as module

    authorizer = SimpleNamespace(authorize=pytest.fail)
    monkeypatch.setattr(module.asyncio, "gather", pytest.fail)

    assert await _decide(object(), authorizer, (), limit=8) == []


@pytest.mark.asyncio
async def test_decide_runs_multiple_asks_in_the_callers_task_when_sequential() -> None:
    caller = asyncio.current_task()
    tasks: list[asyncio.Task[Any] | None] = []

    class Authorizer:
        async def authorize(self, _request: Any, _requirement: Any) -> Any:
            tasks.append(asyncio.current_task())
            return SimpleNamespace(allowed=True)

    asks = ((object(), "read"), (object(), "write"))

    assert await _decide(object(), Authorizer(), asks, limit=1) == [True, True]
    assert tasks == [caller, caller]


@pytest.mark.asyncio
async def test_decide_keeps_a_single_ask_in_the_callers_task() -> None:
    caller = asyncio.current_task()
    tasks: list[asyncio.Task[Any] | None] = []

    class Authorizer:
        async def authorize(self, _request: Any, _requirement: Any) -> Any:
            tasks.append(asyncio.current_task())
            return SimpleNamespace(allowed=True)

    assert await _decide(object(), Authorizer(), ((object(), "read"),), limit=8) == [True]
    assert tasks == [caller]


def test_permission_document_watches_nothing_when_no_roles_model_is_supplied() -> None:
    document = permission_document(SimpleNamespace(_routes=(), _ws_routes=()))

    assert document._watch == frozenset()


def test_permission_document_watches_the_supplied_roles_model() -> None:
    document = permission_document(
        SimpleNamespace(_routes=(), _ws_routes=()), roles_model="Membership"
    )

    assert document._watch == {"Membership"}


def test_permissions_router_builds_a_document_only_when_one_is_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._auth.permissions as module

    sentinel = SimpleNamespace()
    calls: list[Any] = []

    def build(app: Any, **_kwargs: Any) -> Any:
        calls.append(app)
        return sentinel

    monkeypatch.setattr(module, "permission_document", build)
    app = SimpleNamespace(_routes=(), _ws_routes=())

    module.permissions_router(app)
    module.permissions_router(app, document=sentinel)

    assert calls == [app]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["vocabulary", "manifest", "stream", "batch"])
async def test_permission_endpoints_refuse_a_missing_identity(name: str) -> None:
    endpoint = _endpoint(permissions_router(SimpleNamespace(_routes=(), _ws_routes=())), name)

    response = await endpoint(_Request(None))

    assert response.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["manifest", "stream", "batch"])
async def test_permission_decision_endpoints_refuse_a_missing_authorizer(name: str) -> None:
    app = SimpleNamespace(_routes=(), _ws_routes=(), _authorizer=None)
    endpoint = _endpoint(permissions_router(app), name)

    response = await endpoint(_Request(SimpleNamespace(id="ada", type="User", roles=())))

    assert response.status == 500


@pytest.mark.asyncio
async def test_batch_requires_a_json_object() -> None:
    app = SimpleNamespace(_routes=(), _ws_routes=(), _authorizer=object())
    batch = _endpoint(permissions_router(app), "batch")

    response = await batch(_Request(SimpleNamespace(id="ada"), []))

    assert response.status == 400
    assert json.loads(response.body)["detail"] == "the body must be a JSON object"


@pytest.mark.asyncio
async def test_batch_requires_a_string_resource_type() -> None:
    app = SimpleNamespace(_routes=(), _ws_routes=(), _authorizer=object())
    batch = _endpoint(permissions_router(app), "batch")

    response = await batch(_Request(SimpleNamespace(id="ada"), {"type": 7, "ids": []}))

    assert response.status == 400
    assert "`type` (string) and `ids` (list) are required" == json.loads(response.body)["detail"]


@pytest.mark.asyncio
async def test_batch_requires_ids_to_be_a_list() -> None:
    app = SimpleNamespace(_routes=(), _ws_routes=(), _authorizer=object())
    batch = _endpoint(permissions_router(app), "batch")

    response = await batch(
        _Request(SimpleNamespace(id="ada"), {"type": "Llama", "ids": ()})
    )

    assert response.status == 400
    assert "`type` (string) and `ids` (list) are required" == json.loads(response.body)["detail"]


@pytest.mark.asyncio
async def test_batch_requires_actions_to_be_a_list() -> None:
    app = _declared_app("Llama::read")
    app._authorizer = object()
    batch = _endpoint(permissions_router(app), "batch")

    response = await batch(
        _Request(
            SimpleNamespace(id="ada"),
            {"type": "Llama", "ids": [], "actions": ("Llama::read",)},
        )
    )

    assert response.status == 400
    assert json.loads(response.body)["detail"] == "`actions` must be a list of strings"


def test_request_flags_returns_empty_for_a_non_callable_capability() -> None:
    assert _request_flags(SimpleNamespace(flags_for="not callable"), object()) == frozenset()


def test_request_flags_returns_the_authorizers_flags() -> None:
    authorizer = SimpleNamespace(flags_for=lambda _request: frozenset({"preview"}))

    assert _request_flags(authorizer, object()) == {"preview"}


def test_shared_fingerprint_is_empty_without_an_authorizer() -> None:
    app = SimpleNamespace(_authorizer=None, _routes=(), _ws_routes=())

    assert _shared_fingerprint(app) == ""


def test_shared_fingerprint_includes_a_configured_authorizer() -> None:
    app = SimpleNamespace(
        _authorizer=SimpleNamespace(fingerprint=b"policy-v1"),
        _routes=(),
        _ws_routes=(),
    )

    assert _shared_fingerprint(app) != ""


def test_instance_token_is_stable_for_a_new_weak_referenceable_engine() -> None:
    class Engine:
        pass

    engine = Engine()

    first = _instance_token(engine)

    assert len(first) == 16
    assert _instance_token(engine) == first


def test_instance_token_is_stable_for_a_new_unhashable_engine() -> None:
    class Engine:
        __hash__ = None

    engine = Engine()

    first = _instance_token(engine)

    assert len(first) == 16
    assert _instance_token(engine) == first


def test_policy_fingerprint_accepts_a_bytes_capability() -> None:
    assert _policy_fingerprint(SimpleNamespace(fingerprint=b"policy-v1")) == b"policy-v1"
