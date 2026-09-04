from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from wreath._auth.cedar import CedarAuthorizer
from wreath._auth.cedar_engine import CedarEntity, CedarPolicies, EntityUid
from wreath._auth.models import AuthorizationDecision, Identity
from wreath._auth.principal import Narrowing
from wreath._auth.requirements import PolicyRequirement
from wreath.request import Request

DOC = EntityUid("Document", "42")


def request_for(identity: Identity | None) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "GET", "path": "/documents", "headers": []},
        receive,
    )
    request._set_identity(identity)
    return request


class ScalarEngine:
    def is_authorized(self, **arguments: object) -> bool:
        return True


def test_prepared_route_evaluator_requires_both_callable_native_entrypoints() -> None:
    class PreparedOnly(ScalarEngine):
        def _route_denial_prepared(self, **arguments: object) -> None:
            return None

    class NonCallablePrepared(ScalarEngine):
        _route_denial_prepared = object()

        def _route_denial(self, **arguments: object) -> None:
            return None

    prepared_only = CedarAuthorizer(engine=PreparedOnly())
    noncallable_prepared = CedarAuthorizer(engine=NonCallablePrepared())

    assert prepared_only._native_engine_authorize is None
    assert prepared_only._native_engine_prepared is None
    assert noncallable_prepared._native_engine_authorize is not None
    assert noncallable_prepared._native_engine_prepared is None


@pytest.mark.parametrize("mapper", ["principal", "action"])
def test_custom_identity_mappers_prevent_route_plan_compilation(mapper: str) -> None:
    options: dict[str, Any]
    if mapper == "principal":
        options = {"principal": lambda identity: EntityUid("Actor", identity.id)}
    else:
        options = {"action": lambda action, request: EntityUid("Verb", action)}
    authorizer = CedarAuthorizer(
        engine=CedarPolicies("permit(principal, action, resource);"),
        **options,
    )
    requirement = PolicyRequirement("read", DOC)

    assert authorizer._compile_route_requirement(requirement) is requirement
    assert authorizer._compiled_route_uids == {}


class RouteEngine(ScalarEngine):
    def __init__(
        self,
        attributes: frozenset[str] | None,
        principal_entity: bool = True,
    ) -> None:
        self.attributes = attributes
        self.principal_entity = principal_entity

    def _route_denial(self, **arguments: object) -> None:
        return None

    def _route_denial_prepared(self, **arguments: object) -> None:
        return None

    def context_attributes_for_action(self, action: object) -> frozenset[str] | None:
        return self.attributes

    def principal_entity_for_action(self, action: object) -> bool:
        return self.principal_entity


@pytest.mark.parametrize(
    ("attributes", "delegated"),
    [
        (frozenset(), False),
        (None, True),
        (frozenset({"delegated"}), True),
    ],
)
async def test_compiled_query_materializes_delegation_only_when_needed(
    attributes: frozenset[str] | None,
    delegated: bool,
) -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(attributes))
    requirement = authorizer._compile_route_requirement(PolicyRequirement("read", DOC))
    plan = authorizer._compiled_route_uids[requirement]
    request = request_for(Identity("alice"))

    principal, action, _, context = await authorizer._query_base(
        request,
        request.identity,
        requirement.action,
        plan,
    )

    assert principal == ("User", "alice")
    assert action == ("Action", "read")
    assert (context.get("delegated") is False) is delegated


async def test_uncompiled_query_always_materializes_direct_delegation_state() -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(frozenset()))
    request = request_for(Identity("alice"))

    _, _, _, context = await authorizer._query_base(request, request.identity, "read")

    assert context["delegated"] is False


def test_route_plan_tolerates_absent_engine_inventory_methods() -> None:
    class OpaqueRouteEngine(ScalarEngine):
        def _route_denial(self, **arguments: object) -> None:
            return None

        def _route_denial_prepared(self, **arguments: object) -> None:
            return None

    authorizer = CedarAuthorizer(engine=OpaqueRouteEngine())
    requirement = authorizer._compile_route_requirement(PolicyRequirement("read", DOC))
    plan = authorizer._compiled_route_uids[requirement]

    assert plan.context_attributes is None
    assert plan.principal_entity is True


def test_route_plan_preserves_an_engine_principal_entity_refusal() -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(frozenset(), principal_entity=False))
    requirement = authorizer._compile_route_requirement(PolicyRequirement("read", DOC))

    assert authorizer._compiled_route_uids[requirement].principal_entity is False


def test_route_plan_preserves_an_engine_principal_entity_requirement() -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(frozenset(), principal_entity=True))
    requirement = authorizer._compile_route_requirement(PolicyRequirement("read", DOC))

    assert authorizer._compiled_route_uids[requirement].principal_entity is True


@pytest.mark.parametrize(("stop_on_denied", "expected_count"), [(False, 2), (True, 1)])
async def test_anonymous_resource_batch_obeys_its_denial_boundary(
    stop_on_denied: bool,
    expected_count: int,
) -> None:
    authorizer = CedarAuthorizer(engine=ScalarEngine())

    decisions = await authorizer._authorize_resources(
        request_for(None),
        "read",
        (DOC, EntityUid("Document", "43")),
        stop_on_denied=stop_on_denied,
    )

    assert len(decisions) == expected_count
    assert all(decision == AuthorizationDecision(False, "anonymous") for decision in decisions)


class SequencedEngine:
    def __init__(self, allowed: Iterable[bool]) -> None:
        self.allowed = iter(allowed)
        self.calls: list[dict[str, object]] = []

    def is_authorized(self, **arguments: object) -> bool:
        self.calls.append(arguments)
        return next(self.allowed)

    def reads_context(self, name: str) -> bool:
        return False


@pytest.mark.parametrize(
    ("stop_on_denied", "allowed", "expected"),
    [
        (False, (False, True), (False, True)),
        (True, (True, False, True), (True, False)),
    ],
)
async def test_delegated_resource_batch_obeys_its_denial_boundary(
    stop_on_denied: bool,
    allowed: tuple[bool, ...],
    expected: tuple[bool, ...],
) -> None:
    engine = SequencedEngine(allowed)
    identity = Identity(
        "alice",
        narrowing=Narrowing(actor="agent", scope=frozenset({"read"})),
    )
    resources = tuple(EntityUid("Document", str(index)) for index in range(len(allowed)))

    decisions = await CedarAuthorizer(engine=engine)._authorize_resources(
        request_for(identity),
        "read",
        resources,
        stop_on_denied=stop_on_denied,
    )

    assert tuple(decision.allowed for decision in decisions) == expected
    assert len(engine.calls) == len(expected)


async def test_noncallable_batch_entrypoint_falls_back_to_scalar_evaluation() -> None:
    class Engine(SequencedEngine):
        _is_authorized_many = object()

    engine = Engine((True,))

    decisions = await CedarAuthorizer(engine=engine)._authorize_resources(
        request_for(Identity("alice")),
        "read",
        (DOC,),
        stop_on_denied=False,
    )

    assert tuple(decision.allowed for decision in decisions) == (True,)
    assert engine.calls[0]["resource"] == DOC


async def test_entity_resource_uses_its_uid_and_is_layered_into_entities() -> None:
    engine = SequencedEngine((True,))
    resource = CedarEntity(DOC, attrs={"classified": True})

    await CedarAuthorizer(engine=engine)._authorize_resources(
        request_for(Identity("alice")),
        "read",
        (resource,),
        stop_on_denied=False,
    )

    assert engine.calls[0]["resource"] == DOC
    assert tuple(engine.calls[0]["entities"])[-1] is resource


async def test_resource_entity_mapper_adds_its_entities_to_each_scalar_query() -> None:
    engine = SequencedEngine((True,))
    addition = CedarEntity(EntityUid("Folder", "shared"))
    authorizer = CedarAuthorizer(
        engine=engine,
        resource_entities=lambda resource, request: (addition,),
    )

    await authorizer._authorize_resources(
        request_for(Identity("alice")),
        "read",
        (DOC,),
        stop_on_denied=False,
    )

    assert tuple(engine.calls[0]["entities"])[-1] is addition


@pytest.mark.parametrize(
    ("stop_on_denied", "allowed", "expected"),
    [
        (False, (False, True), (False, True)),
        (True, (True, False, True), (True, False)),
    ],
)
async def test_scalar_resource_batch_obeys_its_denial_boundary(
    stop_on_denied: bool,
    allowed: tuple[bool, ...],
    expected: tuple[bool, ...],
) -> None:
    engine = SequencedEngine(allowed)
    authorizer = CedarAuthorizer(engine=engine, resource=lambda resource, request: resource)
    resources = tuple(EntityUid("Document", str(index)) for index in range(len(allowed)))

    decisions = await authorizer._authorize_resources(
        request_for(Identity("alice")),
        "read",
        resources,
        stop_on_denied=stop_on_denied,
    )

    assert tuple(decision.allowed for decision in decisions) == expected
    assert len(engine.calls) == len(expected)


async def test_compiled_route_refuses_a_lost_prepared_evaluator() -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(frozenset()))
    requirement = authorizer._compile_route_requirement(PolicyRequirement("read", DOC))
    authorizer._native_engine_prepared = None

    with pytest.raises(RuntimeError, match="compiled Cedar route lost its native evaluator"):
        await authorizer._authorize_route(request_for(Identity("alice")), requirement)


def test_cedar_entity_route_resource_is_not_compiled_as_a_uid() -> None:
    authorizer = CedarAuthorizer(engine=RouteEngine(frozenset()))
    entity = CedarEntity(DOC, attrs={"classified": True})
    requirement = PolicyRequirement("read", entity)

    assert authorizer._compile_route_requirement(requirement) is requirement
    assert authorizer._compiled_route_uids == {}
