from __future__ import annotations

from typing import Any

import pytest

import wreath._auth.cedar_engine as cedar_engine
from wreath import Wreath
from wreath._auth.cedar import _request_now
from wreath._auth.models import AuthorizationDecision
from wreath._auth.principal import Narrowing
from wreath._auth.requirements import PolicyRequirement
from wreath._native import _core
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import (
    CedarAuthorizer,
    CedarEntity,
    CedarParseError,
    CedarPolicies,
    CedarSchema,
    EntityUid,
    authorize,
)
from wreath.request import Request
from wreath.state import State

ALICE = EntityUid("User", "alice")
BOB = EntityUid("User", "bob")
READ = EntityUid("Action", "read")
DOC = EntityUid("Document", "42")


def test_cedar_tokenizer_keeps_integer_and_underscored_identifier_boundaries() -> None:
    tokens = cedar_engine._tokenize("9 snake_case snake9")

    assert [(token.kind, token.value) for token in tokens] == [
        ("int", "9"),
        ("ident", "snake_case"),
        ("ident", "snake9"),
        ("eof", ""),
    ]


def _request_for(identity: Identity | None) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "GET", "path": "/documents", "headers": []},
        receive,
    )
    request._set_identity(identity)
    return request


def decide(source: str, *, principal=ALICE, action=READ, resource=DOC, context=None, entities=()):
    return CedarPolicies(source, entities=entities).is_authorized(
        principal=principal, action=action, resource=resource, context=context
    )


def test_a_policy_set_parses_once_and_reports_its_size() -> None:
    policies = CedarPolicies("permit(principal, action, resource);")
    assert len(policies) == 1


@pytest.mark.parametrize("reference", ["missing", "::alice", "User::", "::"])
def test_entity_reference_requires_both_type_and_id(reference: str) -> None:
    with pytest.raises(CedarParseError, match='expected Type::"id"'):
        EntityUid.parse(reference)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("allow(principal, action, resource);", "expected 'permit' or 'forbid'"),
        ("permit(resource, action, resource);", "expected 'principal'"),
        ("permit(principal action, resource);", "expected ','"),
        ("permit(principal, action, resource)", "found 'eof'"),
        ("permit(principal, action, resource) when { context.x.nope() };", "unknown method .nope"),
        ('permit(principal, action, resource) when { ip("127.0.0.1") };', "extension function ip"),
        (
            'permit(principal, action, resource) when { datetime("x") };',
            "extension function datetime",
        ),
        ("permit(principal, action, resource) when { mystery };", "unknown identifier 'mystery'"),
    ],
)
def test_parser_refusals_name_the_failed_grammar_rule(source: str, message: str) -> None:
    with pytest.raises(CedarParseError, match=message):
        CedarPolicies(source)


def test_parser_refusal_names_the_actual_token_value() -> None:
    with pytest.raises(CedarParseError, match="expected ',', found 'action'"):
        CedarPolicies("permit(principal action, resource);")


@pytest.mark.parametrize(
    "source",
    [
        r'permit(principal, action, resource) when { "\u" == "u" };',
        r'permit(principal, action, resource) when { "\q{" == "q" };',
        'permit(principal, action, resource) when { "trailing\\',
    ],
)
def test_malformed_string_escapes_are_refused(source: str) -> None:
    with pytest.raises(CedarParseError, match="string|escape"):
        CedarPolicies(source)


@pytest.mark.parametrize("scope", ["principal", "resource"])
def test_only_action_scope_accepts_a_set(scope: str) -> None:
    source = f'permit({scope} in [User::"a"], action, resource);'
    if scope == "resource":
        source = 'permit(principal, action, resource in [Doc::"a"]);'

    with pytest.raises(CedarParseError):
        CedarPolicies(source)


def test_not_equal_is_not_equal_rather_than_equal() -> None:
    source = "permit(principal, action, resource) when { context.n != 1 };"

    assert decide(source, context={"n": 2}).allowed is True
    assert decide(source, context={"n": 1}).allowed is False


def test_empty_sets_and_each_record_key_form_parse() -> None:
    source = (
        "permit(principal, action, resource) when { [] == [] "
        '&& {plain: 1, "quoted-key": 2, true: 3} '
        '== {plain: 1, "quoted-key": 2, true: 3} };'
    )

    assert decide(source).allowed is True


def test_empty_record_parses_as_a_value() -> None:
    assert decide("permit(principal, action, resource) when { {} == {} };").allowed


def test_record_keys_refuse_expression_tokens() -> None:
    with pytest.raises(CedarParseError, match="expected a record key"):
        CedarPolicies("permit(principal, action, resource) when { {1: 2} == {} };")


def test_quoted_record_keys_are_unescaped() -> None:
    source = (
        r'permit(principal, action, resource) when { {"line\nkey": 1} '
        r'== {"line\u{a}key": 1} };'
    )

    assert decide(source).allowed is True


def test_escaped_star_is_unescaped_in_has_and_record_keys() -> None:
    has_star = 'permit(principal, action, resource) when { resource has "\\*" };'
    entity = CedarEntity(DOC, attrs={"*": True})
    assert decide(has_star, entities=(entity,)).allowed

    records = 'permit(principal, action, resource) when { {"\\*": 1} == {"*": 1} };'
    assert decide(records).allowed


def test_identifier_attribute_and_record_keys_avoid_string_unescaping(
    monkeypatch,
) -> None:
    import wreath._auth.cedar_engine as module

    def unexpected(value: str) -> str:
        raise AssertionError("an identifier entered string-unescape handling")

    monkeypatch.setattr(module, "_unescape_star", unexpected)
    source = (
        "permit(principal, action, resource) when { resource has owner && {plain: 1}.plain == 1 };"
    )
    entity = CedarEntity(DOC, attrs={"owner": ALICE})

    assert decide(source, entities=(entity,)).allowed


def test_has_requires_an_identifier_or_string_attribute_name() -> None:
    with pytest.raises(CedarParseError, match="expected an attribute name"):
        CedarPolicies("permit(principal, action, resource) when { resource has 1 };")


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("ip", "unknown identifier 'ip'"),
        ("mystery()", "unknown identifier 'mystery'"),
    ],
)
def test_extension_refusal_requires_both_a_known_name_and_a_call(
    expression: str, message: str
) -> None:
    source = f"permit(principal, action, resource) when {{ {expression} }};"
    with pytest.raises(CedarParseError, match=message):
        CedarPolicies(source)


def test_empty_policy_source_has_a_specific_refusal() -> None:
    with pytest.raises(CedarParseError, match="non-empty Cedar text"):
        CedarPolicies("   \n")


def test_route_fast_path_is_compiled_only_for_native_data_mappers() -> None:
    engine = CedarPolicies("permit(principal, action, resource);")
    direct = CedarAuthorizer(engine=engine)

    assert direct._native_engine_authorize is not None
    assert (
        CedarAuthorizer(
            engine=engine,
            resource=lambda resource, request: resource,
        )._native_engine_authorize
        is None
    )
    assert (
        CedarAuthorizer(
            engine=engine,
            resource_entities=lambda resource, request: (),
        )._native_engine_authorize
        is None
    )

    class ScalarEngine:
        def is_authorized(self, **request: object) -> bool:
            return True

    assert CedarAuthorizer(engine=ScalarEngine())._native_engine_authorize is None

    class NonCallableRouteEngine(ScalarEngine):
        _route_denial = object()

    assert CedarAuthorizer(engine=NonCallableRouteEngine())._native_engine_authorize is None

    backend = BearerTokenBackend(lambda token: None)
    app = Wreath()
    app.configure_auth(backend, direct)
    assert app._route_authorize is not None
    app.configure_auth(backend)
    assert app._route_authorize is None

    class NonCallableRouteAuthorizer:
        _authorize_route = object()

        async def authorize(self, request: object, requirement: object) -> bool:
            return True

    app.configure_auth(backend, NonCallableRouteAuthorizer())
    assert app._route_authorize is None


def test_static_route_resource_is_parsed_during_route_compilation() -> None:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: None),
        CedarAuthorizer(engine=CedarPolicies("permit(principal, action, resource);")),
    )

    @app.get("/document")
    @authorize(action="read", resource='Document::"42"')
    async def document(request):
        return "document"

    app._compile_routes()
    requirement = next(iter(app._handler_requirements.values()))

    assert requirement.policies == (PolicyRequirement("read", DOC),)


def test_invalid_static_route_resource_is_refused_at_startup() -> None:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: None),
        CedarAuthorizer(engine=CedarPolicies("permit(principal, action, resource);")),
    )

    @app.get("/document")
    @authorize(action="read", resource="missing-type-separator")
    async def document(request):
        return "document"

    with pytest.raises(CedarParseError, match="expected Type"):
        app._compile_routes()


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        "permit(principal, action, resource)",  # missing semicolon
        "allow(principal, action, resource);",  # not an effect
        "permit(principal == User::alice, action, resource);",  # unquoted id
        "permit(principal, action is Action, resource);",  # is in action scope
        'permit(principal, action, resource) when { ip("1.2.3.4") };',  # extension
        "permit(principal, action, resource) when { principal.x( };",
        "permit(principal, action, resource) when { {a: 1, a: 2} == context };",
        "permit(principal, action, resource) when { 9223372036854775808 > 0 };",
    ],
)
def test_invalid_cedar_fails_at_construction(source: str) -> None:
    with pytest.raises(CedarParseError):
        CedarPolicies(source)


def test_parse_errors_carry_position() -> None:
    with pytest.raises(CedarParseError) as excinfo:
        CedarPolicies("permit(principal action, resource);")
    assert "line 1" in str(excinfo.value)


def test_a_member_operator_requires_a_name_immediately_after_the_dot() -> None:
    source = "permit(principal, action, resource) when { principal. };"
    with pytest.raises(CedarParseError, match="attribute or method name after"):
        CedarPolicies(source)


def test_entity_uid_parses_cedar_and_bare_forms() -> None:
    assert EntityUid.parse('User::"alice"') == ALICE
    assert EntityUid.parse("User::alice") == ALICE
    assert EntityUid.parse('App::User::"a::b"') == EntityUid("App::User", "a::b")
    with pytest.raises(CedarParseError):
        EntityUid.parse("no-separator")


_SCHEMA = """
type RequestContext = { "seat": { "status": String } };
entity User;
entity Document;
action "document";
action "read" in ["document"] appliesTo {
  principal: [User], resource: [Document], context: RequestContext
};
"""


def test_schema_rejects_a_misspelled_context_attribute_at_construction() -> None:
    with pytest.raises(CedarParseError, match=r"context\.seat\.statsu"):
        CedarPolicies(
            'permit(principal, action == Action::"read", resource) '
            'when { context.seat.statsu == "active" };',
            schema=_SCHEMA,
        )


def test_schema_rejects_a_misspelled_guarded_attribute_at_construction() -> None:
    with pytest.raises(CedarParseError, match=r"context\.statsu"):
        CedarPolicies(
            'permit(principal, action == Action::"read", resource) when { context has statsu };',
            schema=_SCHEMA,
        )


def test_schema_action_groups_feed_the_native_hierarchy() -> None:
    engine = CedarPolicies(
        'permit(principal, action in Action::"document", resource);',
        schema=CedarSchema(_SCHEMA),
    )
    assert engine.is_authorized(
        principal=ALICE,
        action=EntityUid("Action", "read"),
        resource=DOC,
    ).allowed


def test_schema_action_entities_cannot_be_overridden() -> None:
    with pytest.raises(CedarParseError, match="cannot be overridden"):
        CedarPolicies(
            'permit(principal, action == Action::"read", resource);',
            schema=_SCHEMA,
            entities=(CedarEntity(EntityUid("Action", "read")),),
        )


def test_schema_allows_unrelated_declared_entities() -> None:
    engine = CedarPolicies(
        'permit(principal, action == Action::"read", resource);',
        schema=_SCHEMA,
        entities=(CedarEntity(EntityUid("User", "alice")),),
    )

    assert len(engine) == 1


def test_only_undefined_static_parents_enter_the_dangling_inventory() -> None:
    known = EntityUid("Group", "known")
    missing = EntityUid("Group", "missing")
    engine = CedarPolicies(
        "permit(principal, action, resource);",
        entities=(
            CedarEntity(known),
            CedarEntity(EntityUid("User", "alice"), parents=(known, missing)),
        ),
    )

    assert engine._dangling == frozenset({("Group", "missing")})


def test_schema_validation_is_a_noop_without_a_schema() -> None:
    engine = CedarPolicies("permit(principal, action, resource);")

    assert engine._validate_schema() == ()


def test_schema_requires_action_uids_in_action_scope() -> None:
    with pytest.raises(CedarParseError, match="use an Action"):
        CedarPolicies(
            'permit(principal, action == Document::"read", resource);',
            schema=_SCHEMA,
        )


def test_exact_action_scope_does_not_include_group_descendants() -> None:
    exact = CedarPolicies(
        'permit(principal, action == Action::"document", resource);', schema=_SCHEMA
    )
    grouped = CedarPolicies(
        'permit(principal, action in Action::"document", resource);', schema=_SCHEMA
    )

    assert exact.is_authorized(principal=ALICE, action=READ, resource=DOC).allowed is False
    assert grouped.is_authorized(principal=ALICE, action=READ, resource=DOC).allowed is True


def test_schema_context_validation_distinguishes_exact_from_group_scope() -> None:
    expression = 'when { context.seat.status == "active" };'

    with pytest.raises(CedarParseError, match="context.seat"):
        CedarPolicies(
            f'permit(principal, action == Action::"document", resource) {expression}',
            schema=_SCHEMA,
        )
    CedarPolicies(
        f'permit(principal, action in Action::"document", resource) {expression}',
        schema=_SCHEMA,
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('context.flags.contains("x")', frozenset({"x"})),
        ('context.flags.containsAny(["x", "y"])', frozenset({"x", "y"})),
        ("context.flags.isEmpty()", None),
        ("context.flags == context.flags", None),
        ('context.regions.contains("x")', frozenset()),
    ],
)
def test_referenced_flag_inventory_recognizes_only_literal_naming_methods(
    expression: str, expected: frozenset[str] | None
) -> None:
    engine = CedarPolicies(f"permit(principal, action, resource) when {{ {expression} }};")

    assert engine.referenced_flags() == expected


def test_schema_rejects_an_unknown_action_at_construction() -> None:
    with pytest.raises(CedarParseError, match="unknown action 'raed'"):
        CedarPolicies(
            'permit(principal, action == Action::"raed", resource);',
            schema=_SCHEMA,
        )


def test_schema_requires_an_attribute_on_every_action_a_group_policy_can_match() -> None:
    schema = """
    action "documents";
    action "read" in ["documents"] appliesTo { context: { "seat": String } };
    action "write" in ["documents"] appliesTo { context: { "actor": String } };
    """
    with pytest.raises(CedarParseError, match=r"context\.seat"):
        CedarPolicies(
            'forbid(principal, action in Action::"documents", resource) '
            'when { context.seat == "suspended" };',
            schema=schema,
        )


def test_default_deny_when_nothing_matches() -> None:
    decision = decide('permit(principal == User::"bob", action, resource);')
    assert not decision.allowed
    assert decision.reason == "no permit policy matched"


def test_scope_equality_permits() -> None:
    decision = decide('permit(principal == User::"alice", action == Action::"read", resource);')
    assert decision.allowed
    assert decision.reason == "cedar permit"


def test_forbid_overrides_permit() -> None:
    decision = decide(
        'permit(principal, action, resource);forbid(principal == User::"alice", action, resource);'
    )
    assert not decision.allowed
    assert decision.reason == "explicit forbid"
    assert any("forbid" in line and "matched" in line for line in decision.diagnostics)


def test_batched_decisions_match_scalar_resource_and_context_semantics() -> None:
    engine = CedarPolicies(
        'permit(principal in Role::"reader", action == Action::"read", resource) '
        "when { context.enabled && resource.active };"
        'forbid(principal, action, resource == Document::"blocked");'
    )
    resources = (
        EntityUid("Document", "allowed"),
        EntityUid("Document", "blocked"),
        EntityUid("Document", "later"),
    )
    arguments = {
        "principal": ALICE,
        "action": READ,
        "context": {"enabled": True},
        "entities": (
            CedarEntity(ALICE, parents=(EntityUid("Role", "reader"),)),
            *(CedarEntity(resource, attrs={"active": True}) for resource in resources),
        ),
    }
    expected = tuple(engine.is_authorized(resource=resource, **arguments) for resource in resources)
    assert (
        engine._is_authorized_many(resources=resources, stop_on_denied=False, **arguments)
        == expected
    )
    assert (
        engine._is_authorized_many(resources=resources, stop_on_denied=True, **arguments)
        == expected[:2]
    )

    context_free = CedarPolicies("permit(principal, action, resource);")
    assert context_free._is_authorized_many(
        principal=ALICE,
        action=READ,
        resources=(DOC,),
        context=None,
        entities=(),
        stop_on_denied=False,
    ) == (context_free.is_authorized(principal=ALICE, action=READ, resource=DOC),)


@pytest.mark.asyncio
async def test_authorizer_native_batch_uses_the_native_engine_entrypoint() -> None:
    sentinel = object()

    class Engine:
        def _is_authorized_many_native(self, **arguments: object) -> object:
            return sentinel

        def _is_authorized_many(self, **arguments: object) -> object:
            raise AssertionError("materialized batch authorization ran")

        def is_authorized(self, **arguments: object) -> bool:
            raise AssertionError("scalar authorization ran")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "GET", "path": "/documents", "headers": []},
        receive,
    )
    request._set_identity(Identity("alice"))
    authorizer = CedarAuthorizer(engine=Engine())

    result = await authorizer._authorize_resources(
        request,
        "read",
        (DOC,),
        stop_on_denied=False,
        native=True,
    )

    assert result is sentinel


@pytest.mark.asyncio
async def test_authorizer_batch_empty_and_anonymous_boundaries() -> None:
    class Engine:
        def is_authorized(self, **arguments: object) -> bool:
            raise AssertionError("an empty or anonymous batch reached the engine")

    def unexpected_principal(identity: Identity) -> object:
        raise AssertionError("an empty batch resolved its principal")

    authorizer = CedarAuthorizer(engine=Engine(), principal=unexpected_principal)
    assert (
        await authorizer._authorize_resources(
            _request_for(Identity("alice")), "read", (), stop_on_denied=False
        )
        == ()
    )

    denied = await authorizer._authorize_resources(
        _request_for(None),
        "read",
        (DOC, EntityUid("Document", "43")),
        stop_on_denied=False,
    )
    assert tuple((item.allowed, item.reason) for item in denied) == (
        (False, "anonymous"),
        (False, "anonymous"),
    )


@pytest.mark.asyncio
async def test_authorizer_batch_applies_delegation_scope_per_resource() -> None:
    class Engine:
        def _is_authorized_many(self, **arguments: object) -> object:
            raise AssertionError("a delegated batch bypassed scalar scope checks")

        def is_authorized(self, **arguments: object) -> bool:
            return True

    identity = Identity("alice", narrowing=Narrowing(actor="agent", scope=frozenset({"inspect"})))
    decisions = await CedarAuthorizer(engine=Engine())._authorize_resources(
        _request_for(identity), "read", (DOC,), stop_on_denied=False
    )

    assert len(decisions) == 1
    assert decisions[0].allowed is False
    assert decisions[0].reason == "delegation scope does not cover this action"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["mapper", "entities", "entity"])
async def test_batch_fast_path_requires_plain_resource_data(variant: str) -> None:
    calls: list[dict[str, object]] = []

    class Engine:
        def _is_authorized_many(self, **arguments: object) -> object:
            raise AssertionError("the batch fast path accepted configurable resource data")

        def is_authorized(self, **arguments: object) -> bool:
            calls.append(arguments)
            return True

    options: dict[str, Any] = {}
    resources: tuple[object, ...] = (DOC,)
    if variant == "mapper":
        options["resource"] = lambda resource, request: EntityUid("Mapped", resource.id)
    elif variant == "entities":
        options["resource_entities"] = lambda resource, request: ()
    else:
        resources = (CedarEntity(DOC, attrs={"classified": True}),)

    decisions = await CedarAuthorizer(engine=Engine(), **options)._authorize_resources(
        _request_for(Identity("alice")),
        "read",
        resources,
        stop_on_denied=False,
    )

    assert len(decisions) == 1 and decisions[0].allowed is True
    assert len(calls) == 1
    if variant == "entity":
        assert tuple(calls[0]["entities"])[-1].attrs == {"classified": True}


@pytest.mark.asyncio
async def test_non_native_batch_does_not_call_the_native_entrypoint() -> None:
    class Engine:
        def _is_authorized_many_native(self, **arguments: object) -> object:
            raise AssertionError("native batch authorization ran without a native request")

        def _is_authorized_many(self, **arguments: object) -> tuple[bool, ...]:
            return (True,)

        def is_authorized(self, **arguments: object) -> bool:
            raise AssertionError("scalar authorization ran")

    decisions = await CedarAuthorizer(engine=Engine())._authorize_resources(
        _request_for(Identity("alice")), "read", (DOC,), stop_on_denied=False
    )

    assert len(decisions) == 1 and decisions[0].allowed is True


@pytest.mark.asyncio
async def test_noncallable_native_batch_falls_through_to_materialized_batch() -> None:
    class Engine:
        _is_authorized_many_native = object()

        def _is_authorized_many(self, **arguments: object) -> tuple[bool, ...]:
            return (True,)

        def is_authorized(self, **arguments: object) -> bool:
            raise AssertionError("scalar authorization ran")

    decisions = await CedarAuthorizer(engine=Engine())._authorize_resources(
        _request_for(Identity("alice")),
        "read",
        (DOC,),
        stop_on_denied=False,
        native=True,
    )

    assert len(decisions) == 1 and decisions[0].allowed is True


def test_route_denial_layers_request_entities_and_accepts_absent_context() -> None:
    engine = CedarPolicies(
        "permit(principal, action, resource) when { resource.owner == principal };"
    )
    owner = CedarEntity(DOC, attrs={"owner": ALICE})

    denied = engine._route_denial(
        principal=ALICE, action=READ, resource=DOC, context=None, entities=None
    )
    allowed = engine._route_denial(
        principal=ALICE, action=READ, resource=DOC, context=None, entities=(owner,)
    )

    assert denied == "no permit policy matched"
    assert allowed is None


def test_prepared_route_denial_reads_request_owned_sets() -> None:
    engine = CedarPolicies(
        'permit(principal, action, resource) when { context.flags.contains("dense") '
        '&& context.flags == ["dense", "dense"] };'
    )

    denial = engine._route_denial_prepared(
        principal=("User", "alice"),
        action=("Action", "read"),
        resource=("Document", "42"),
        context={"flags": frozenset({"dense"})},
    )

    assert denial is None


def test_prepared_route_denial_converts_unprepared_context_values() -> None:
    engine = CedarPolicies(
        'permit(principal, action, resource) when { context.flags.contains("dense") };'
    )

    denial = engine._route_denial_prepared(
        principal=("User", "alice"),
        action=("Action", "read"),
        resource=("Document", "42"),
        context={"flags": ("dense",)},
    )

    assert denial is None


def test_prepared_route_denial_preserves_conversion_refusals() -> None:
    engine = CedarPolicies("permit(principal, action, resource);")

    with pytest.raises(TypeError, match="has no Cedar equivalent"):
        engine._route_denial_prepared(
            principal=("User", "alice"),
            action=("Action", "read"),
            resource=("Document", "42"),
            context={"ratio": 0.5},
        )


def test_materialized_batch_layers_request_entities() -> None:
    engine = CedarPolicies(
        "permit(principal, action, resource) when { resource.owner == principal };"
    )
    owner = CedarEntity(DOC, attrs={"owner": ALICE})

    denied = engine._is_authorized_many(
        principal=ALICE,
        action=READ,
        resources=(DOC,),
        context=None,
        entities=None,
        stop_on_denied=False,
    )
    allowed = engine._is_authorized_many(
        principal=ALICE,
        action=READ,
        resources=(DOC,),
        context=None,
        entities=(owner,),
        stop_on_denied=False,
    )

    assert denied[0].allowed is False
    assert allowed[0].allowed is True


def test_native_batch_receives_layered_entities_and_empty_context(monkeypatch) -> None:
    import wreath._auth.cedar_engine as module

    seen: list[tuple[object, object]] = []

    def observed(
        policies: object,
        principal: object,
        action: object,
        resources: object,
        context: object,
        store: object,
        stop_on_denied: bool,
    ) -> object:
        seen.append((context, store))
        return object()

    monkeypatch.setattr(module._core, "cedar_is_authorized_many_native", observed)
    engine = CedarPolicies("permit(principal, action, resource);")
    owner = CedarEntity(DOC, attrs={"owner": ALICE})

    engine._is_authorized_many_native(
        principal=ALICE,
        action=READ,
        resources=(DOC,),
        context={"tenant": "acme"},
        entities=(owner,),
        stop_on_denied=False,
    )

    assert seen[0][0] == {"tenant": "acme"}
    assert seen[0][1][("Document", "42")][0] == {"owner": ("User", "alice")}


@pytest.mark.parametrize("operation", ["route", "batch", "native"])
def test_absent_request_entities_skip_entity_conversion(operation: str, monkeypatch) -> None:
    import wreath._auth.cedar_engine as module

    engine = CedarPolicies("permit(principal, action, resource);")

    def unexpected(value: object) -> object:
        raise AssertionError("None entered request-entity conversion")

    monkeypatch.setattr(module, "_as_entities", unexpected)
    if operation == "route":
        assert (
            engine._route_denial(
                principal=ALICE, action=READ, resource=DOC, context=None, entities=None
            )
            is None
        )
    elif operation == "batch":
        assert engine._is_authorized_many(
            principal=ALICE,
            action=READ,
            resources=(DOC,),
            context=None,
            entities=None,
            stop_on_denied=False,
        )[0].allowed
    else:
        engine._is_authorized_many_native(
            principal=ALICE,
            action=READ,
            resources=(DOC,),
            context=None,
            entities=None,
            stop_on_denied=False,
        )


class _RouteBoundaryEngine:
    def _route_denial(self, **arguments: object) -> None:
        return None

    def is_authorized(self, **arguments: object) -> AuthorizationDecision:
        return AuthorizationDecision(False, "materialized")


@pytest.mark.asyncio
async def test_route_fast_path_refuses_anonymous_and_delegated_callers() -> None:
    authorizer = CedarAuthorizer(engine=_RouteBoundaryEngine())
    requirement = PolicyRequirement("read", DOC)

    assert await authorizer._authorize_route(_request_for(None), requirement) == "anonymous"
    delegated = Identity("alice", narrowing=Narrowing(actor="agent", scope=frozenset({"inspect"})))
    assert (
        await authorizer._authorize_route(_request_for(delegated), requirement)
        == "delegation scope does not cover this action"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource",
    [CedarEntity(DOC, attrs={"classified": True}), "42"],
)
async def test_route_fast_path_materializes_non_native_resource_shapes(
    resource: object,
) -> None:
    authorizer = CedarAuthorizer(engine=_RouteBoundaryEngine())

    denial = await authorizer._authorize_route(
        _request_for(Identity("alice")), PolicyRequirement("read", resource)
    )

    assert denial == "materialized"


@pytest.mark.asyncio
async def test_typed_string_resource_stays_on_the_route_fast_path() -> None:
    calls = 0

    class Engine(_RouteBoundaryEngine):
        def _route_denial(self, **arguments: object) -> None:
            nonlocal calls
            calls += 1

        def is_authorized(self, **arguments: object) -> AuthorizationDecision:
            raise AssertionError("a typed static resource was materialized")

    denial = await CedarAuthorizer(engine=Engine())._authorize_route(
        _request_for(Identity("alice")),
        PolicyRequirement("read", 'Document::"42"'),
    )

    assert denial is None
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (AuthorizationDecision(False), "Forbidden"),
        (AuthorizationDecision(False, "policy said no"), "policy said no"),
    ],
)
async def test_route_fallback_preserves_a_reason_or_supplies_one(
    decision: AuthorizationDecision, expected: str
) -> None:
    class Engine:
        def is_authorized(self, **arguments: object) -> AuthorizationDecision:
            return decision

    authorizer = CedarAuthorizer(engine=Engine())

    assert (
        await authorizer._authorize_route(
            _request_for(Identity("alice")), PolicyRequirement("read", DOC)
        )
        == expected
    )


def test_route_requirement_compilation_covers_each_resource_shape() -> None:
    native = CedarAuthorizer(engine=CedarPolicies("permit(principal, action, resource);"))
    typed = PolicyRequirement("read", 'Document::"42"')
    identifier = PolicyRequirement("read", "document_id")
    entity = PolicyRequirement("read", DOC)

    assert native._compile_route_requirement(typed).resource == DOC
    assert native._compile_route_requirement(identifier) is identifier
    assert native._compile_route_requirement(entity) is entity
    with pytest.raises(TypeError, match="Cedar route resource must be"):
        native._compile_route_requirement(PolicyRequirement("read", 42))

    opaque = CedarAuthorizer(engine=_RouteBoundaryEngine(), resource=lambda value, request: value)
    invalid = PolicyRequirement("read", object())
    assert opaque._compile_route_requirement(invalid) is invalid


def test_action_context_inventory_excludes_entity_attributes() -> None:
    engine = CedarPolicies(
        'permit(principal, action == Action::"read", resource) '
        "when { principal.active && resource.active && context.enabled };"
    )

    assert engine.context_attributes_for_action(READ) == frozenset({"enabled"})


def test_action_principal_inventory_excludes_unreachable_entity_work() -> None:
    engine = CedarPolicies(
        'permit(principal, action == Action::"render", resource);'
        'permit(principal in Role::"reader", action == Action::"read", resource);'
        'permit(principal, action == Action::"inspect", resource) '
        "when { principal has active && principal.active };"
    )

    assert not engine.principal_entity_for_action(EntityUid("Action", "render"))
    assert engine.principal_entity_for_action(READ)
    assert engine.principal_entity_for_action(EntityUid("Action", "inspect"))


def test_principal_in_group_uses_the_transitive_hierarchy() -> None:
    entities = (
        CedarEntity(ALICE, parents=(EntityUid("Group", "staff"),)),
        CedarEntity(EntityUid("Group", "staff"), parents=(EntityUid("Group", "everyone"),)),
    )
    source = 'permit(principal in Group::"everyone", action, resource);'
    assert decide(source, entities=entities).allowed
    assert not decide(source, principal=BOB, entities=entities).allowed


def test_action_scope_accepts_a_set() -> None:
    source = 'permit(principal, action in [Action::"read", Action::"list"], resource);'
    assert decide(source).allowed
    assert not decide(source, action=EntityUid("Action", "write")).allowed


def test_principal_is_type_with_and_without_ancestor() -> None:
    entities = (CedarEntity(ALICE, parents=(EntityUid("Group", "staff"),)),)
    assert decide("permit(principal is User, action, resource);").allowed
    assert not decide("permit(principal is Robot, action, resource);").allowed
    assert decide(
        'permit(principal is User in Group::"staff", action, resource);', entities=entities
    ).allowed
    assert not decide('permit(principal is User in Group::"staff", action, resource);').allowed


def test_annotation_names_the_policy_in_diagnostics() -> None:
    decision = decide('@id("docs-read") permit(principal, action, resource);')
    assert decision.diagnostics == ("permit docs-read matched",)


def test_when_reads_context_and_unless_vetoes() -> None:
    source = (
        "permit(principal, action, resource)"
        '  when { context.method == "GET" }'
        "  unless { context.suspicious };"
    )
    assert decide(source, context={"method": "GET", "suspicious": False}).allowed
    assert not decide(source, context={"method": "POST", "suspicious": False}).allowed
    assert not decide(source, context={"method": "GET", "suspicious": True}).allowed


def test_entity_attributes_and_has() -> None:
    entities = (CedarEntity(DOC, attrs={"owner": ALICE, "tags": ["public", "docs"]}),)
    assert decide(
        "permit(principal, action, resource) when { resource.owner == principal };",
        entities=entities,
    ).allowed
    assert decide(
        "permit(principal, action, resource) when { resource has owner };", entities=entities
    ).allowed
    assert not decide(
        'permit(principal, action, resource) when { resource has "missing" };',
        entities=entities,
    ).allowed
    assert decide(
        'permit(principal, action, resource) when { resource.tags.contains("public") };',
        entities=entities,
    ).allowed


@pytest.mark.parametrize(
    ("expression", "context", "allowed"),
    [
        ("1 + 2 * 3 == 7", {}, True),
        ("10 - 3 < 8 && 10 - 3 >= 7", {}, True),
        ("-context.n == 5", {"n": -5}, True),
        ('"abc" like "a*"', {}, True),
        ('"abc" like "*c"', {}, True),
        ('"abc" like "a*c"', {}, True),
        ('"abc" like "b*"', {}, False),
        ('"a*c" like "a\\*c"', {}, True),
        ('"axc" like "a\\*c"', {}, False),
        ("[1, 2, 2].containsAll([2, 1])", {}, True),
        ("[1, 2].containsAny([3, 2])", {}, True),
        ("[].isEmpty()", {}, True),
        ("[1] == [1, 1]", {}, True),
        ("{a: 1, b: [true]} == {b: [true], a: 1}", {}, True),
        ("if context.n > 0 then true else false", {"n": 1}, True),
        ("context.n != 1 || context.n == 1", {"n": 1}, True),
        ('context["quoted key"] == 3', {"quoted key": 3}, True),
        ("1 == true", {}, False),
        ("0 == false", {}, False),
        ('User::"alice" == principal', {}, True),
    ],
)
def test_expression_semantics(expression: str, context: dict, allowed: bool) -> None:
    decision = decide(
        f"permit(principal, action, resource) when {{ {expression} }};", context=context
    )
    assert decision.allowed is allowed, decision.diagnostics


def test_short_circuit_hides_errors_on_the_untaken_side() -> None:
    assert decide("permit(principal, action, resource) when { true || (1 < true) };").allowed
    assert not decide("permit(principal, action, resource) when { false && (1 < true) };").allowed


def test_an_erroring_policy_is_skipped_and_reported_not_satisfied() -> None:
    decision = decide(
        "permit(principal, action, resource) when { context.missing == 1 };"
        "permit(principal, action, resource);"
    )
    assert decision.allowed
    assert any("skipped" in line for line in decision.diagnostics)


def test_an_erroring_forbid_does_not_deny() -> None:
    decision = decide(
        "permit(principal, action, resource);"
        "forbid(principal, action, resource) when { context.missing == 1 };"
    )
    assert decision.allowed


@pytest.mark.parametrize(
    "expression",
    [
        "1 < true",
        '1 + "a" == 2',
        "9223372036854775807 + 1 == 0",
        "principal < resource",
        '"text" in principal',
        "context.method has x",
        "[1].contains(1, 2) || true",  # arity is a parse error, listed here to assert loudness
    ],
)
def test_type_and_overflow_errors_never_satisfy(expression: str) -> None:
    source = f"permit(principal, action, resource) when {{ {expression} }};"
    if "contains(1, 2)" in expression:
        with pytest.raises(CedarParseError):
            CedarPolicies(source)
        return
    decision = decide(source, context={"method": "GET"})
    assert not decision.allowed
    assert any("skipped" in line for line in decision.diagnostics)


def test_uids_arrive_as_objects_or_strings() -> None:
    policies = CedarPolicies('permit(principal == User::"alice", action, resource);')
    for principal in (ALICE, 'User::"alice"', "User::alice"):
        decision = policies.is_authorized(
            principal=principal, action=READ, resource=DOC, context={}
        )
        assert decision.allowed


def test_non_cedar_context_values_are_loud_type_errors() -> None:
    policies = CedarPolicies("permit(principal, action, resource);")
    with pytest.raises(TypeError):
        policies.is_authorized(principal=ALICE, action=READ, resource=DOC, context={"ratio": 1.5})


def test_request_entities_merge_over_static_entities() -> None:
    policies = CedarPolicies(
        'permit(principal in Role::"admin", action, resource);',
        entities=(CedarEntity(BOB, parents=(EntityUid("Role", "admin"),)),),
    )
    request_entity = CedarEntity(ALICE, parents=(EntityUid("Role", "admin"),))
    granted = policies.is_authorized(
        principal=ALICE, action=READ, resource=DOC, context={}, entities=(request_entity,)
    )
    ungranted = policies.is_authorized(principal=ALICE, action=READ, resource=DOC, context={})
    assert granted.allowed
    assert not ungranted.allowed


def test_the_policy_source_is_public() -> None:
    source = 'permit(principal == User::"alice", action, resource);'
    assert CedarPolicies(source).source == source


def test_two_engines_parsed_from_one_text_report_the_same_source() -> None:
    source = "permit(principal, action, resource);"
    assert CedarPolicies(source).source == CedarPolicies(source).source


def test_the_source_is_read_only_and_does_not_add_a_dict() -> None:
    policies = CedarPolicies("permit(principal, action, resource);")
    with pytest.raises(AttributeError):
        object.__setattr__(policies, "source", "permit(principal, action, resource);")
    assert not hasattr(policies, "__dict__")


async def invoke(app: Wreath, token: str, path: str = "/documents/42") -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_route_keeps_an_allowed_native_decision_unmaterialized() -> None:
    class Engine:
        def __init__(self) -> None:
            self.route_calls = 0

        def _route_denial(self, **request: object) -> None:
            self.route_calls += 1

        def is_authorized(self, **request: object) -> bool:
            raise AssertionError("the materialized decision path ran")

    async def verify(token: str) -> Identity:
        return Identity(token)

    engine = Engine()
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

    @app.get("/documents/{document_id}")
    @authorize(action="read", resource=DOC)
    async def document(request):
        return "allowed"

    sent = await invoke(app, "alice")

    assert sent[0]["status"] == 200
    assert engine.route_calls == 1


@pytest.mark.asyncio
async def test_builtin_engine_authorizes_through_the_default_mappers() -> None:
    async def verify(token: str) -> Identity | None:
        roles = frozenset({"editor"}) if token == "alice" else frozenset()
        return Identity(token, roles=roles) if token in {"alice", "bob"} else None

    engine = CedarPolicies(
        'permit(principal in Role::"editor", action == Action::"Document::read", resource)'
        '  when { context.method == "GET" };'
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

    @app.get("/documents/{document_id}")
    @authorize(
        action="Document::read",
        resource=lambda request: EntityUid("Document", request.path_params["document_id"]),
    )
    async def document(request):
        return "allowed"

    allowed = await invoke(app, "alice")
    denied = await invoke(app, "bob")
    assert allowed[0]["status"] == 200
    assert denied[0]["status"] == 403


@pytest.mark.asyncio
async def test_account_attributes_and_request_time_are_first_class_policy_data() -> None:
    accounts = {
        "machine": Identity(
            "machine",
            type="Account",
            attributes={
                "active": True,
                "kind": "machine",
                "not_before": 100,
                "expires_at": 200,
            },
        ),
        "inactive": Identity(
            "inactive",
            type="Account",
            attributes={
                "active": False,
                "kind": "machine",
                "not_before": 100,
                "expires_at": 200,
            },
        ),
        "future": Identity(
            "future",
            type="Account",
            attributes={
                "active": True,
                "kind": "machine",
                "not_before": 151,
                "expires_at": 200,
            },
        ),
        "expired": Identity(
            "expired",
            type="Account",
            attributes={
                "active": True,
                "kind": "machine",
                "not_before": 100,
                "expires_at": 150,
            },
        ),
    }

    async def verify(token: str) -> Identity | None:
        return accounts.get(token)

    engine = CedarPolicies(
        'permit(principal is Account, action == Action::"Document::read", resource) '
        'when { principal.active && principal.kind == "machine" '
        "&& context.now >= principal.not_before "
        "&& context.now < principal.expires_at };"
    )
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(verify), CedarAuthorizer(engine=engine, clock=lambda: 150)
    )

    @app.get("/documents/{document_id}")
    @authorize(
        action="Document::read",
        resource=lambda request: EntityUid("Document", request.path_params["document_id"]),
    )
    async def document(request):
        return "allowed"

    assert (await invoke(app, "machine"))[0]["status"] == 200
    assert (await invoke(app, "inactive"))[0]["status"] == 403
    assert (await invoke(app, "future"))[0]["status"] == 403
    assert (await invoke(app, "expired"))[0]["status"] == 403


@pytest.mark.asyncio
async def test_request_time_is_lazy_and_shared_by_every_policy_on_the_request() -> None:
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 150.9

    async def verify(token: str) -> Identity | None:
        return Identity(token)

    untimed = Wreath()
    untimed.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(engine=CedarPolicies("permit(principal, action, resource);"), clock=clock),
    )

    @untimed.get("/untimed")
    @authorize(action="read", resource=EntityUid("Document", "1"))
    async def without_time(request):
        return "allowed"

    assert (await invoke(untimed, "account", "/untimed"))[0]["status"] == 200
    assert calls == 0

    action_untimed = Wreath()
    action_untimed.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(
            engine=CedarPolicies(
                'permit(principal, action == Action::"read", resource);'
                'permit(principal, action == Action::"audit", resource) '
                "when { context.now == 150 };"
            ),
            clock=clock,
        ),
    )

    @action_untimed.get("/action-untimed")
    @authorize(action="read", resource=EntityUid("Document", "1"))
    async def without_time_for_this_action(request):
        return "allowed"

    assert (await invoke(action_untimed, "account", "/action-untimed"))[0]["status"] == 200
    assert calls == 0

    timed = Wreath()
    timed.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(
            engine=CedarPolicies(
                "permit(principal, action, resource) when { context.now == 150 };"
            ),
            clock=clock,
        ),
    )

    @timed.get("/timed")
    @authorize(action="read", resource=EntityUid("Document", "1"))
    @authorize(action="audit", resource=EntityUid("Document", "1"))
    async def with_time(request):
        return "allowed"

    assert (await invoke(timed, "account", "/timed"))[0]["status"] == 200
    assert calls == 1


@pytest.mark.parametrize("value", [True, "150", None])
def test_request_time_refuses_non_numeric_clock_values(value: object) -> None:
    request = type("Request", (), {"state": State()})()

    with pytest.raises(TypeError, match="Unix seconds"):
        _request_now(request, lambda: value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_request_time_refuses_non_finite_clock_values(value: float) -> None:
    request = type("Request", (), {"state": State()})()

    with pytest.raises(ValueError, match="finite Unix seconds"):
        _request_now(request, lambda: value)


@pytest.mark.asyncio
async def test_an_opaque_engine_receives_time_because_it_cannot_declare_its_reads() -> None:
    class Engine:
        context: dict[str, object] | None = None

        def is_authorized(self, **ask: Any) -> bool:
            self.context = dict(ask["context"])
            return True

    async def verify(token: str) -> Identity | None:
        return Identity(token)

    engine = Engine()
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(verify), CedarAuthorizer(engine=engine, clock=lambda: 150.9)
    )

    @app.get("/opaque")
    @authorize(action="read", resource=EntityUid("Document", "1"))
    async def opaque(request):
        return "allowed"

    assert (await invoke(app, "account", "/opaque"))[0]["status"] == 200
    assert engine.context is not None
    assert engine.context["now"] == 150


@pytest.mark.asyncio
async def test_a_route_resource_can_carry_attributes_and_hierarchy() -> None:
    async def verify(token: str) -> Identity | None:
        return Identity(token, roles=frozenset({"reader"}))

    engine = CedarPolicies(
        'permit(principal in Role::"reader", action == Action::"Document::read", '
        'resource in Folder::"archive") when { resource.classification == "internal" };'
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), CedarAuthorizer(engine=engine))

    @app.get("/documents/{document_id}")
    @authorize(
        action="Document::read",
        resource=lambda request: CedarEntity(
            EntityUid("Document", request.path_params["document_id"]),
            attrs={"classification": "internal"},
            parents=(EntityUid("Folder", "archive"),),
        ),
    )
    async def document(request):
        return "allowed"

    assert (await invoke(app, "account"))[0]["status"] == 200


@pytest.mark.asyncio
async def test_one_application_resource_provider_can_supply_route_hierarchy_and_time() -> None:
    async def verify(token: str) -> Identity | None:
        return Identity(token, type="Account", attributes={"active": True})

    def resource_entities(resource: object, request: object) -> CedarEntity:
        assert isinstance(resource, EntityUid)
        return CedarEntity(
            resource,
            attrs={"not_before": 100, "expires_at": 200},
            parents=(EntityUid("Ranch", "highland"),),
        )

    engine = CedarPolicies(
        'permit(principal is Account, action == Action::"Document::read", '
        'resource in Ranch::"highland") when { principal.active '
        "&& context.now >= resource.not_before && context.now < resource.expires_at };"
    )
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(verify),
        CedarAuthorizer(
            engine=engine,
            resource_entities=resource_entities,
            clock=lambda: 150,
        ),
    )

    @app.get("/documents/{document_id}")
    @authorize(
        action="Document::read",
        resource=lambda request: EntityUid("Document", request.path_params["document_id"]),
    )
    async def document(request):
        return "allowed"

    assert (await invoke(app, "account"))[0]["status"] == 200


#: One policy set per Cedar feature, with the decision Cedar's semantics
#: specify. The answers are written down rather than read off the evaluator: the
#: request is fixed (alice reads Document::"42", which she owns and which sits
#: in Folder::"archive", while she is in Group::"banned"; context is
#: `n=1, veto=false, now=150`), so each of these has exactly one right answer
#: and the interesting half is *why* it is that answer.
CEDAR_SEMANTICS = [
    pytest.param(
        "permit(principal, action, resource);",
        (True, "cedar permit", ("permit policy0 matched",)),
        id="unconstrained-permit",
    ),
    pytest.param(
        'permit(principal == User::"alice", action in [Action::"read"], resource)'
        "  when { context.n + 1 == 2 } unless { context.veto };",
        (True, "cedar permit", ("permit policy0 matched",)),
        id="scope-when-and-unless",  # 1+1==2 holds, veto is false, so both pass
    ),
    pytest.param(
        'forbid(principal is User in Group::"banned", action, resource);'
        "permit(principal, action, resource)"
        "  when { resource has owner && resource.owner == principal };",
        # A forbid that matches beats any permit, however many match -- and the
        # permit here does match, which is what makes this the real test.
        (
            False,
            "explicit forbid",
            ("forbid policy0 matched", "permit policy1 matched"),
        ),
        id="forbid-overrides-a-matching-permit",
    ),
    pytest.param(
        "permit(principal, action, resource);"
        'forbid(principal, action == Action::"read", resource);',
        (
            False,
            "explicit forbid",
            ("permit policy0 matched", "forbid policy1 matched"),
        ),
        id="later-forbid-still-overrides-an-earlier-permit",
    ),
    pytest.param(
        "forbid(principal, action, resource) when { context.missing };"
        "permit(principal, action, resource);",
        (
            True,
            "cedar permit",
            (
                "forbid policy0 skipped: record has no attribute 'missing'",
                "permit policy1 matched",
            ),
        ),
        id="erroring-forbid-is-skipped-before-permit",
    ),
    pytest.param(
        "permit(principal, action, resource) when { context.missing == 1 };",
        # An absent attribute is an evaluation error, and an erroring policy is
        # *skipped* rather than treated as true or as a forbid.
        (
            False,
            "no permit policy matched",
            ("permit policy0 skipped: record has no attribute 'missing'",),
        ),
        id="absent-context-attribute-skips",
    ),
    pytest.param(
        'permit(principal, action, resource) when { [1, {a: "x"}].contains({a: "x"}) };',
        (True, "cedar permit", ("permit policy0 matched",)),
        id="contains-compares-records-by-value",
    ),
    pytest.param(
        'permit(principal, action, resource) when { "wreath" like "w*h" };',
        (True, "cedar permit", ("permit policy0 matched",)),
        id="like-wildcard",
    ),
    pytest.param(
        'permit(principal, action, resource in Folder::"archive") '
        "when { context.now >= 100 && context.now < 200 };",
        (True, "cedar permit", ("permit policy0 matched",)),
        id="resource-in-parent-and-a-time-window",
    ),
    pytest.param(
        "permit(principal, action, resource) when { 9223372036854775807 + 1 == 0 };",
        # Cedar integers are i64 and do not wrap: overflow is an error, so the
        # policy is skipped rather than silently becoming true.
        (
            False,
            "no permit policy matched",
            ("permit policy0 skipped: arithmetic overflowed i64",),
        ),
        id="i64-overflow-is-an-error-not-a-wrap",
    ),
]


@pytest.mark.parametrize(("source", "expected"), CEDAR_SEMANTICS)
def test_the_evaluator_decides_what_cedar_specifies(
    source: str, expected: tuple[bool, str, tuple[str, ...]]
) -> None:
    engine = CedarPolicies(
        source,
        entities=(
            CedarEntity(
                DOC,
                attrs={"owner": ALICE},
                parents=(EntityUid("Folder", "archive"),),
            ),
            CedarEntity(ALICE, parents=(EntityUid("Group", "banned"),)),
        ),
    )
    decision = _core.cedar_is_authorized(
        engine._policies,
        ("User", "alice"),
        ("Action", "read"),
        ("Document", "42"),
        {"n": 1, "veto": False, "now": 150},
        engine._store,
    )
    assert decision == expected


@pytest.mark.parametrize(("source", "expected"), CEDAR_SEMANTICS)
def test_route_evaluation_materializes_only_a_denial(
    source: str, expected: tuple[bool, str, tuple[str, ...]]
) -> None:
    engine = CedarPolicies(
        source,
        entities=(
            CedarEntity(
                DOC,
                attrs={"owner": ALICE},
                parents=(EntityUid("Folder", "archive"),),
            ),
            CedarEntity(ALICE, parents=(EntityUid("Group", "banned"),)),
        ),
    )

    denial = _core.cedar_route_denial(
        engine._plan,
        ("User", "alice"),
        ("Action", "read"),
        ("Document", "42"),
        {"n": 1, "veto": False, "now": 150},
        engine._store,
    )

    assert denial == (None if expected[0] else expected[1])
