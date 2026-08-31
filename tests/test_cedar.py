from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.cedar import (
    _default_context,
    _default_entities,
    _resolve_flags,
)
from wreath._auth.cedar_engine import CedarEntity, CedarPolicies, EntityUid
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize
from wreath.flags import FeatureFlags
from wreath.request import Request


class Engine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def is_authorized(self, **request: object) -> bool:
        self.calls.append(request)
        return request["principal"] == "User::alice" and request["resource"] == "Document::42"


async def invoke(app: Wreath, token: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/documents/42",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent


def _request(identity: Identity | None, *, method: str = "GET", path: str = "/x") -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": method, "path": path, "headers": []}, receive)
    request._set_identity(identity)
    return request


def test_default_entities_are_empty_for_an_anonymous_request() -> None:
    assert _default_entities(_request(None)) == ()


def test_default_entities_preserve_identity_facts() -> None:
    identity = Identity("alice", roles=frozenset({"writer", "reader"}), attributes={"active": True})

    assert _default_entities(_request(identity)) == (
        CedarEntity(
            EntityUid("User", "alice"),
            attrs={"active": True},
            parents=(EntityUid("Role", "reader"), EntityUid("Role", "writer")),
        ),
    )


def test_default_context_omits_an_unproved_second_factor() -> None:
    context = _default_context(_request(Identity("alice"), method="PATCH", path="/docs/1"))

    assert context["method"] == "PATCH"
    assert context["path"] == "/docs/1"
    assert "second_factor_age" not in context


def test_default_context_includes_a_proved_second_factor(monkeypatch) -> None:
    import wreath._auth.cedar as module

    monkeypatch.setattr(module.time, "time", lambda: 1_700_000_100.0)
    identity = Identity("alice", claims={"second_factor_at": 1_700_000_000.0})

    assert _default_context(_request(identity))["second_factor_age"] == 100


def test_plain_feature_flags_take_the_allocation_free_boolean_path(monkeypatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("the typed Flag path allocated for a plain boolean provider")

    monkeypatch.setattr(FeatureFlags, "resolve", unexpected)
    resolved = _resolve_flags(
        _request(Identity("alice")),
        FeatureFlags({"live": "on"}),
        frozenset({"live"}),
    )

    assert resolved == frozenset({"live"})


def test_legacy_flag_providers_are_normalized_once_at_startup() -> None:
    class LegacyFlags:
        def enabled(self, name: str, context: object = None) -> bool:
            return name == "live"

    provider = LegacyFlags()
    authorizer = CedarAuthorizer(
        engine=CedarPolicies("permit(principal, action, resource);"), flags=provider
    )

    assert authorizer._flags is not provider
    assert callable(authorizer._flags.resolve)


class _SelectiveEngine:
    def __init__(self, needed: frozenset[str] | None) -> None:
        self.needed = needed

    def context_attributes_for_action(self, action: object) -> frozenset[str] | None:
        return self.needed

    def is_authorized(self, **request: object) -> bool:
        return True


async def test_default_and_custom_principal_action_mappers_stay_distinct() -> None:
    request = _request(Identity("alice"))
    default = CedarAuthorizer(engine=_SelectiveEngine(frozenset()))
    principal, action, _, _ = await default._query_base(request, request.identity, "read")
    assert principal == EntityUid("User", "alice")
    assert action == EntityUid("Action", "read")

    async def principal(identity: Identity) -> str:
        return f"Principal::{identity.id}"

    async def action(name: str, request: Request) -> str:
        return f"Verb::{name}:{request.method}"

    custom = CedarAuthorizer(
        engine=_SelectiveEngine(frozenset()), principal=principal, action=action
    )
    mapped_principal, mapped_action, _, _ = await custom._query_base(
        request, request.identity, "read"
    )
    assert mapped_principal == "Principal::alice"
    assert mapped_action == "Verb::read:GET"


async def test_default_principal_and_action_do_not_enter_awaitable_resolution(
    monkeypatch,
) -> None:
    import wreath._auth.cedar as module

    calls: list[object] = []
    real = module._resolve

    async def observed(value: object) -> object:
        calls.append(value)
        return await real(value)

    monkeypatch.setattr(module, "_resolve", observed)
    request = _request(Identity("alice"))
    authorizer = CedarAuthorizer(engine=_SelectiveEngine(frozenset()))

    await authorizer._query_base(request, request.identity, "read")

    assert calls == []


async def test_engine_can_suppress_unneeded_principal_entities() -> None:
    class Engine(_SelectiveEngine):
        def principal_entity_for_action(self, action: object) -> bool:
            return False

    request = _request(Identity("alice", roles=frozenset({"admin"})))
    authorizer = CedarAuthorizer(engine=Engine(frozenset()))

    _, _, entities, _ = await authorizer._query_base(request, request.identity, "read")

    assert entities is None


async def test_custom_entities_are_resolved_even_when_the_engine_needs_no_principal() -> None:
    class Engine(_SelectiveEngine):
        def principal_entity_for_action(self, action: object) -> bool:
            return False

    expected = (CedarEntity(EntityUid("Service", "api")),)
    request = _request(Identity("alice"))
    authorizer = CedarAuthorizer(engine=Engine(frozenset()), entities=lambda request: expected)

    _, _, entities, _ = await authorizer._query_base(request, request.identity, "read")

    assert entities == expected


@pytest.mark.parametrize(
    ("needed", "present", "absent"),
    [
        (frozenset({"method"}), "method", "path"),
        (frozenset({"path"}), "path", "method"),
        (frozenset({"client_class"}), "client_class", "method"),
    ],
)
async def test_default_context_materializes_only_the_action_partition_reads(
    needed: frozenset[str], present: str, absent: str
) -> None:
    request = _request(
        Identity("alice", claims={"second_factor_at": 1_700_000_000.0}),
        method="PATCH",
        path="/docs/1",
    )
    authorizer = CedarAuthorizer(engine=_SelectiveEngine(needed))

    _, _, _, context = await authorizer._query_base(request, request.identity, "read")

    assert present in context
    assert absent not in context
    assert set(context) == {present, "delegated"}


async def test_selective_context_adds_second_factor_age_only_when_proved(
    monkeypatch,
) -> None:
    import wreath._auth.cedar as module

    monkeypatch.setattr(module.time, "time", lambda: 1_700_000_100.0)
    authorizer = CedarAuthorizer(engine=_SelectiveEngine(frozenset({"second_factor_age"})))
    unproved = _request(Identity("alice"))
    proved = _request(Identity("alice", claims={"second_factor_at": 1_700_000_000.0}))

    _, _, _, unproved_context = await authorizer._query_base(unproved, unproved.identity, "read")
    _, _, _, proved_context = await authorizer._query_base(proved, proved.identity, "read")

    assert "second_factor_age" not in unproved_context
    assert proved_context["second_factor_age"] == 100


async def test_custom_context_is_not_replaced_by_engine_partition_metadata() -> None:
    request = _request(Identity("alice"))
    authorizer = CedarAuthorizer(
        engine=_SelectiveEngine(frozenset({"path"})),
        context=lambda request: {"application": "kept"},
    )

    _, _, _, context = await authorizer._query_base(request, request.identity, "read")

    assert context["application"] == "kept"
    assert "path" not in context


async def test_engine_that_declares_no_time_read_does_not_receive_time() -> None:
    class Engine:
        def reads_context(self, name: str) -> bool:
            return False

        def is_authorized(self, **request: object) -> bool:
            return True

    request = _request(Identity("alice"))
    authorizer = CedarAuthorizer(engine=Engine(), clock=lambda: 150)

    _, _, _, context = await authorizer._query_base(request, request.identity, "read")

    assert "now" not in context


@pytest.mark.asyncio
async def test_cedar_adapter_is_final_authorization_after_coarse_route_pruning() -> None:
    engine = Engine()

    async def verify(token: str) -> Identity | None:
        return Identity(token) if token in {"alice", "bob"} else None

    authorizer = CedarAuthorizer(
        engine=engine,
        principal=lambda identity: f"User::{identity.id}",
        action=lambda action, request: action,
        resource=lambda resource, request: f"Document::{resource}",
        entities=lambda request: (),
        context=lambda request: {"method": request.method},
        clock=lambda: 150,
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), authorizer)

    @app.get("/documents/{document_id}")
    @authorize(action="Document::read", resource=lambda request: request.path_params["document_id"])
    async def document(request):
        return "allowed"

    allowed = await invoke(app, "alice")
    denied = await invoke(app, "bob")

    assert allowed[0]["status"] == 200
    assert denied[0]["status"] == 403
    # The context mapper here returns only `method`; every other key below is
    # the authorizer's own and is supplied whatever the mapper does, empty when
    # no provider is configured. That is deliberate rather than incidental: an
    # *absent* set makes `forbid ... unless { context.<set>.contains(...) }`
    # evaluate to allowed -- the forbid is skipped rather than standing -- so a
    # custom mapper that omitted one would silently disable every kill-switch
    # written in that shape. `delegated` is a literal `false` for the same
    # reason, since `unless { context.delegated }` against an absent key skips
    # the forbid an agent was supposed to be caught by.
    # `tests/test_cedar_flags.py`, `tests/test_cedar_geofence.py` and
    # `tests/test_principal_narrow.py` pin that engine behaviour for each.
    # Asserting the whole dict rather than a subset is what makes this a
    # *register* of the authorizer's context surface: a new fact fails here
    # first, which is the moment to decide whether it fails closed.
    assert engine.calls[0]["context"] == {
        "method": "GET",
        "flags": frozenset(),
        "regions": frozenset(),
        "organizations": frozenset(),
        "org_roles": frozenset(),
        "entitlements": frozenset(),
        # Present and empty with no provider, like every other set fact -- and
        # the direction it fails matters more here than for the others.
        # `quota` members are read to *forbid* ("past due means read-only"), so
        # an absent key would skip that forbid and hand a degraded caller full
        # access. Empty leaves the forbid standing and inert.
        "quota": frozenset(),
        "delegated": False,
        "now": 150,
    }


class _Identified:
    """An engine that offers its policy text, the way `CedarPolicies` does."""

    def __init__(self, source: str) -> None:
        self.source = source

    def is_authorized(self, **request: object) -> bool:
        return False


class _Fingerprinted:
    """An engine that offers a digest instead of its text."""

    fingerprint = b"a-digest"

    def is_authorized(self, **request: object) -> bool:
        return False


def test_the_authorizer_offers_the_engines_policy_identity() -> None:
    authorizer = CedarAuthorizer(engine=_Identified("permit(principal, action, resource);"))

    assert authorizer.source == "permit(principal, action, resource);"


def test_every_probed_name_is_delegated_not_just_source() -> None:
    assert CedarAuthorizer(engine=_Fingerprinted()).fingerprint == b"a-digest"


def test_an_engine_offering_nothing_leaves_the_names_absent() -> None:
    authorizer = CedarAuthorizer(engine=Engine())

    for name in ("fingerprint", "source", "policies"):
        with pytest.raises(AttributeError):
            getattr(authorizer, name)
        assert getattr(authorizer, name, None) is None


def test_the_delegation_adds_no_dict_to_the_authorizer() -> None:
    authorizer = CedarAuthorizer(engine=_Identified("permit(principal, action, resource);"))

    assert not hasattr(authorizer, "__dict__")
    with pytest.raises(AttributeError):
        authorizer.source = "something else"  # type: ignore[misc]
