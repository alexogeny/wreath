"""The built-in Cedar engine: parsing, evaluation semantics, and the app path.

Every semantic case runs through ``CedarPolicies.is_authorized``, which selects
the native evaluator when it is built and the pure one otherwise; the explicit
parity test at the bottom runs both and asserts identical output, so the two
implementations cannot drift apart unnoticed.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import (
    CedarAuthorizer,
    CedarEntity,
    CedarParseError,
    CedarPolicies,
    EntityUid,
    authorize,
)

ALICE = EntityUid("User", "alice")
BOB = EntityUid("User", "bob")
READ = EntityUid("Action", "read")
DOC = EntityUid("Document", "42")


def decide(source: str, *, principal=ALICE, action=READ, resource=DOC, context=None, entities=()):
    return CedarPolicies(source, entities=entities).is_authorized(
        principal=principal, action=action, resource=resource, context=context
    )


# -- parsing ------------------------------------------------------------------


def test_a_policy_set_parses_once_and_reports_its_size() -> None:
    policies = CedarPolicies('permit(principal, action, resource);')
    assert len(policies) == 1


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        "permit(principal, action, resource)",  # missing semicolon
        "allow(principal, action, resource);",  # not an effect
        'permit(principal == User::alice, action, resource);',  # unquoted id
        "permit(principal, action is Action, resource);",  # is in action scope
        'permit(principal, action, resource) when { ip("1.2.3.4") };',  # extension
        "permit(principal, action, resource) when { principal.x( };",
        'permit(principal, action, resource) when { {a: 1, a: 2} == context };',
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


def test_entity_uid_parses_cedar_and_bare_forms() -> None:
    assert EntityUid.parse('User::"alice"') == ALICE
    assert EntityUid.parse("User::alice") == ALICE
    assert EntityUid.parse('App::User::"a::b"') == EntityUid("App::User", "a::b")
    with pytest.raises(CedarParseError):
        EntityUid.parse("no-separator")


# -- scopes and the authorization algorithm -----------------------------------


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
        "permit(principal, action, resource);"
        'forbid(principal == User::"alice", action, resource);'
    )
    assert not decision.allowed
    assert decision.reason == "explicit forbid"
    assert any("forbid" in line and "matched" in line for line in decision.diagnostics)


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
    assert not decide(
        'permit(principal is User in Group::"staff", action, resource);'
    ).allowed


def test_annotation_names_the_policy_in_diagnostics() -> None:
    decision = decide('@id("docs-read") permit(principal, action, resource);')
    assert decision.diagnostics == ("permit docs-read matched",)


# -- conditions and the expression language -----------------------------------


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
        'permit(principal, action, resource) when { resource has owner };', entities=entities
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
    assert decide(
        "permit(principal, action, resource) when { true || (1 < true) };"
    ).allowed
    assert not decide(
        "permit(principal, action, resource) when { false && (1 < true) };"
    ).allowed


# -- error isolation ----------------------------------------------------------


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


# -- inputs and boundary conversion -------------------------------------------


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
        policies.is_authorized(
            principal=ALICE, action=READ, resource=DOC, context={"ratio": 1.5}
        )


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


# -- identifying the policy set -----------------------------------------------


def test_the_policy_source_is_public() -> None:
    """Callers that cache against a policy set need to identify it by content.

    The permission manifest's `ETag` is the live one: a tag derived from the
    engine object rather than its text differs per worker and per restart, so
    `If-None-Match` never matches and the revalidation the manifest exists for
    succeeds exactly never.
    """
    source = 'permit(principal == User::"alice", action, resource);'
    assert CedarPolicies(source).source == source


def test_two_engines_parsed_from_one_text_report_the_same_source() -> None:
    """The property every worker in a fleet depends on."""
    source = "permit(principal, action, resource);"
    assert CedarPolicies(source).source == CedarPolicies(source).source


def test_the_source_is_read_only_and_does_not_add_a_dict() -> None:
    """The parse happens once; a settable source would drift from `_policies`.

    The slots layout matters beyond tidiness -- `permissions.py` weak-references
    engines to cache a tag, and an accidental `__dict__` would change which
    branch of that cache an engine lands in.
    """
    policies = CedarPolicies("permit(principal, action, resource);")
    with pytest.raises(AttributeError):
        policies.source = "permit(principal, action, resource);"  # type: ignore[misc]
    assert not hasattr(policies, "__dict__")


# -- the whole app path -------------------------------------------------------


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


# -- pure/native parity -------------------------------------------------------


def _both_evaluators():
    from wreath._native import _core
    from wreath._pure import cedar as pure

    evaluators = [("pure", pure.cedar_is_authorized)]
    native = getattr(_core, "cedar_is_authorized", None) if _core is not None else None
    if native is not None:
        evaluators.append(("native", native))
    return evaluators


PARITY_SOURCES = [
    "permit(principal, action, resource);",
    'permit(principal == User::"alice", action in [Action::"read"], resource)'
    "  when { context.n + 1 == 2 } unless { context.veto };",
    'forbid(principal is User in Group::"banned", action, resource);'
    "permit(principal, action, resource)"
    "  when { resource has owner && resource.owner == principal };",
    "permit(principal, action, resource) when { context.missing == 1 };",
    'permit(principal, action, resource) when { [1, {a: "x"}].contains({a: "x"}) };',
    'permit(principal, action, resource) when { "wreath" like "w*h" };',
    "permit(principal, action, resource) when { 9223372036854775807 + 1 == 0 };",
]


@pytest.mark.parametrize("source", PARITY_SOURCES)
def test_pure_and_native_evaluators_agree(source: str) -> None:
    engine = CedarPolicies(
        source,
        entities=(
            CedarEntity(DOC, attrs={"owner": ALICE}),
            CedarEntity(ALICE, parents=(EntityUid("Group", "banned"),)),
        ),
    )
    request = dict(
        principal=("User", "alice"),
        action=("Action", "read"),
        resource=("Document", "42"),
        context={"n": 1, "veto": False},
    )
    results = [
        (name, evaluate(engine._policies, *request.values(), engine._store))
        for name, evaluate in _both_evaluators()
    ]
    first = results[0][1]
    for name, result in results[1:]:
        assert result == first, f"{name} disagreed: {result} != {first}"
    assert isinstance(first, tuple) and len(first) == 3
