"""Tests for `wreath.mutant`.

The one that matters is `test_a_run_reports_both_a_killed_and_a_surviving_control`:
a mutation tester that can only report KILLED is exactly the shape ADR 0024
names -- a check that passes because it has nothing to check -- and it would be
trusted anyway, because a green mutation report reads like good news. So the
end-to-end test drives a project with one *watched* control and one *unwatched*
one, and demands that the tool tells them apart.

Every assertion here was observed failing before it was made to pass: the
operator tests against a fixture module that does not contain the construct,
the patch tests against an unpatched function, and the end-to-end test against
a fixture whose second control was also covered.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from wreath._mutant import operators
from wreath._mutant.patch import (
    CodePatch,
    PatchError,
    ValuePatch,
    compile_module,
    find_code,
    same_bytecode,
    transform_module,
)
from wreath._mutant.runner import build_plan, module_name_for, sample_identifiers
from wreath.mutant import OPERATORS, Outcome, Report, Verdict, render

SOURCE = textwrap.dedent(
    '''
    """A module that declares controls, so the operators have something to do."""
    import re

    MAX_UPLOAD_SIZE = 1024
    SENSITIVE_FIELD = re.compile("secret")
    DEFERRED_ALGORITHMS = frozenset({"none"})
    POLICY = """
    permit(principal, action, resource) when { context.ok };
    forbid(principal, action, resource);
    """


    class Gate:
        def is_permitted(self, identity, role):
            """A permission check with two clauses."""
            if identity is None:
                raise PermissionError("anonymous")
            return identity.trusted and role in identity.roles


    def charge(principal, session, limiter):
        """A rate limit keyed on the caller, or on the session."""
        key = principal if principal is not None else session
        return limiter(key)
    '''
)


@pytest.fixture
def module(tmp_path: Path) -> Path:
    path = tmp_path / "controls.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


#: Deadline for a nested `wreath mutant` invocation. Each one runs a pytest
#: baseline and then forks per mutant, so several of them inside an xdist run
#: are the most load-sensitive thing in this file. Generous on purpose: these
#: are bounded fixture projects, so a run that genuinely hangs is still caught,
#: while a run merely competing for the machine is not reported as a failure.
_NESTED_TIMEOUT = 600

_CRUD_GROUP = pytest.mark.xdist_group(name="mutant_crud")
_CEDAR_GROUP = pytest.mark.xdist_group(name="mutant_cedar")
_SAMPLE_GROUP = pytest.mark.xdist_group(name="mutant_sample")


def _scan(path: Path) -> list[operators.Candidate]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return operators.scan(tree, None)


def _by(found: list[operators.Candidate], operator: str) -> list[operators.Candidate]:
    return [c for c in found if c.operator == operator]


# -- the operator library ---------------------------------------------------


def test_every_named_operator_is_reachable_from_the_public_surface() -> None:
    """A derived list pins its own names (ADR 0024).

    Without this, an operator that stops being emitted shrinks coverage
    silently and the run still exits 0.
    """
    assert len(OPERATORS) == 21
    assert set(OPERATORS) == {
        "predicate.drop-operand", "predicate.always-true", "expression.take-branch",
        "comprehension.drop-clause", "guard.remove-raise", "guard.never-fires",
        "guard.always-fires", "guard.drop-statement", "declaration.drop-keyword",
        "declaration.widen-bound", "crud.drop-operation-authorize",
        "crud.widen-access", "crud.permit-refused-operation",
        "crud.unprotect-column", "crud.expose-sensitive", "cedar.flip-effect",
        "cedar.drop-condition", "cedar.delete-policy", "value.widen-bound",
        "value.disable-pattern", "value.empty-denylist",
    }


def test_a_conjunction_yields_one_mutation_per_clause(module: Path) -> None:
    found = _by(_scan(module), "predicate.drop-operand")
    controls = [c.control for c in found]
    assert any("identity.trusted" in c for c in controls), controls
    assert any("role in identity.roles" in c for c in controls), controls


def test_a_refusal_becomes_a_mutation_that_does_not_refuse(module: Path) -> None:
    found = _by(_scan(module), "guard.remove-raise")
    assert [c.control for c in found] == ["the refusal `raise PermissionError('anonymous')`"]


def test_a_keyed_choice_yields_both_branches_named_by_what_is_kept(module: Path) -> None:
    """The 'limit that does not limit' operator: key on the free-to-mint side.

    The label has to name the branch that *survives*. It named the other one at
    first, and the report then blamed a set of tests that had nothing to do with
    the mutation -- a wrong answer that still looked like a right one.
    """
    found = _by(_scan(module), "expression.take-branch")
    assert len(found) == 2
    kept = {c.control.rsplit("always ", 1)[1] for c in found}
    assert kept == {"`principal`)", "`session`)"}


def test_a_predicate_named_like_a_permission_check_can_answer_true(module: Path) -> None:
    found = _by(_scan(module), "predicate.always-true")
    assert [c.control for c in found] == ["every check in `is_permitted` (it now answers True)"]


def test_the_always_true_mutation_watches_the_body_not_the_def_line(module: Path) -> None:
    """A `def` line runs once, at import.

    Watching it attributed the mutation to no test at all, so a control every
    test exercised was reported UNREACHED -- the tool inventing ADR 0024's own
    failure mode.
    """
    found = _by(_scan(module), "predicate.always-true")[0]
    assert found.watch, "no watch lines: every test would be filtered out"
    assert found.line not in found.watch or len(found.watch) > 1


def test_cedar_policy_text_is_mutated_as_a_policy_not_as_a_string(module: Path) -> None:
    found = _scan(module)
    assert any(c.operator == "cedar.flip-effect" for c in found)
    assert any(c.operator == "cedar.drop-condition" for c in found)
    assert any(c.operator == "cedar.delete-policy" for c in found)


def test_the_control_vocabulary_is_what_scopes_the_predicate_operators() -> None:
    assert "authoriz" in operators.CONTROL_TOKENS
    assert "second_factor" in operators.CONTROL_KEYWORDS
    quiet = ast.parse("def encode(value):\n    if value:\n        raise ValueError('x')\n")
    assert operators.scan(quiet, None) == []


def _scan_resolved(tmp_path: Path, source: str, name: str) -> list[operators.Candidate]:
    """Scan a module that has been *imported*, so callee resolution can work.

    `_scan` passes `None` for the module name, which makes `_resolve_callee`
    return `None` for everything and confines the declaration operators to the
    route branch. The declaration surfaces this section covers -- crud, mcp,
    graphql -- are answered through resolution or through the declaring-call
    table, so they need the live module the way a real run has one.
    """
    import importlib.util

    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return operators.scan(tree, name)
    finally:
        sys.modules.pop(name, None)


#: A crud declaration written the way a factory writes one: the callee resolves,
#: and every control is a keyword.
_CRUD_FACTORY = """
    from wreath.crud import Access, crud_router


    def _open(request): ...


    class Account:
        # A real column map, so `crud.expose-sensitive` has a withheld set to
        # read. `api_token` and `password_hash` match `SENSITIVE_FIELD`;
        # `id` and `name` do not.
        __wreath_column_map__ = {
            "id": None, "name": None, "api_token": None, "password_hash": None,
        }


    def build():
        return crud_router(
            Account, _open,
            expose=("api_token",),
            readonly=("id",),
            page_size=20,
            authorize={
                "list": Access.roles("admin"),
                "create": Access.deny(),
                "delete": Access.permissions("account:delete"),
            },
        )
"""


def test_the_crud_authorize_mapping_is_offered_at_all(tmp_path: Path) -> None:
    """`authorize=` is the control `crud_router` exists to carry.

    Reproduction, 2026-07-31: with everything resolvable the tool offered
    `expose=`, `readonly=` and `page_size=` and *not* `authorize=`, because
    `authorize` was absent from `CONTROL_KEYWORDS`. The most important control
    on the call was invisible for a one-word reason, and the report read clean.
    """
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_factory")
    dropped = {c.control for c in found if c.operator == "declaration.drop-keyword"}
    assert any("`authorize=`" in control for control in dropped), dropped


def test_each_crud_operation_is_verified_independently(tmp_path: Path) -> None:
    """One mutant per operation, not one for the whole mapping.

    Dropping `authorize=` wholesale removes every operation's control at once,
    so a suite that tests only `delete` still kills it while `list` and
    `create` go unverified. A coarse mutant that dies easily reports coverage
    that does not exist -- the same optimistic-union error AGENTS.md records
    for native/pure runs, in a different dimension.
    """
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_ops")
    per_entry = _by(found, "crud.drop-operation-authorize")
    keys = {c.control.split("`")[1] for c in per_entry}
    assert keys == {"list", "create", "delete"}, [c.control for c in per_entry]


def test_a_crud_denial_can_be_turned_into_a_permit(tmp_path: Path) -> None:
    """`Access.deny()` is a refusal somebody wrote on purpose, and it gets its
    own operator.

    The *transform* is shared with `crud.widen-access` -- rewrite the method to
    the permissive twin -- but the finding is not: a surviving
    `permit-refused-operation` says nobody ever checked that the operation is
    refused at all, where a surviving `widen-access` says nobody distinguished a
    permitted caller from a refused one. One name for both reads as one number
    in the report, and the first is much the worse of the two.
    """
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_deny")
    permitted = _by(found, "crud.permit-refused-operation")
    assert any("create" in c.control for c in permitted), [c.control for c in permitted]
    assert all(
        "widen" not in c.operator for c in permitted
    ), "a refusal must not also be offered as a widening"


def test_each_protected_column_is_verified_independently(tmp_path: Path) -> None:
    """`readonly=("id", ...)` is one control per column.

    Dropping the keyword makes every listed column writable in a single mutant,
    so a test that checks any one of them reports the rest as covered. One
    mutant per column names the column nobody checks.

    This is the per-entry operator for a *column* control. The sibling that
    plan 05 asked for -- widening `expose=` to reveal one more sensitive column
    -- is deliberately absent, and `test_widening_expose_is_out_of_static_reach`
    below records why.
    """
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_readonly")
    per_column = _by(found, "crud.unprotect-column")
    assert [c.control.split("`")[1] for c in per_column] == ["id"], [
        c.control for c in per_column
    ]


def test_revealing_one_withheld_column_is_a_mutant_per_column(tmp_path: Path) -> None:
    """`expose=` *can* be widened, and the name comes from the model.

    This was declined once, for a good reason that turned out to be answerable:
    the name a widening must add is exactly the one **not** written at the call
    site, so fabricating one produces a mutant that either raises or is ignored.
    But it is not fabricated -- `crud_router(Account, ...)` names the model, the
    callee resolver already walks a module global to a live object, and
    `wreath.crud.sensitive_fields` is the *declaration* of what is withheld.

    `api_token` is already exposed at the call site, so revealing it would be a
    no-op mutant; only `password_hash` is offered.
    """
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_expose")
    exposed = _by(found, "crud.expose-sensitive")
    assert {c.control.split("`")[1] for c in exposed} == {"password_hash"}, [
        c.control for c in exposed
    ]
    # ... and the wholesale drop is still offered, so `expose=` is watched from
    # both directions: one mutant reveals a column, another removes the
    # exception that reveals one.
    assert any(
        "`expose=`" in c.control for c in _by(found, "declaration.drop-keyword")
    )


def test_a_model_that_does_not_resolve_offers_no_expose_mutant(tmp_path: Path) -> None:
    """The rule the keyword operators already follow: where the declaration
    cannot be read, decline rather than guess. A fabricated column name is a
    mutant that moves the score for a reason that is not about the suite."""
    source = """
        from wreath.crud import crud_router


        def _open(request): ...


        def build(model):
            # The model is a *parameter*, so there is nothing to resolve.
            return crud_router(model, _open, expose=("api_token",))
    """
    found = _scan_resolved(tmp_path, source, "crud_unresolvable_model")
    assert _by(found, "crud.expose-sensitive") == []


def test_a_model_with_no_sensitive_column_offers_nothing(tmp_path: Path) -> None:
    """Nothing is withheld, so there is nothing to reveal -- and an operator
    that fired anyway would be inventing a control."""
    source = """
        from wreath.crud import crud_router


        def _open(request): ...


        class Plain:
            __wreath_column_map__ = {"id": None, "name": None}


        def build():
            return crud_router(Plain, _open, expose=())
    """
    found = _scan_resolved(tmp_path, source, "crud_no_secrets")
    assert _by(found, "crud.expose-sensitive") == []


def test_a_crud_call_through_an_unresolvable_receiver_is_still_answerable(
    tmp_path: Path,
) -> None:
    """`application.crud(...)` inside a mount function.

    This is the shape the camera-trap example uses, and the receiver is a
    parameter -- so `_resolve_callee` cannot answer and the declaration
    operators declined for *every* keyword. The declaring-call table answers it
    the way `_route_metadata` already answers a route decorator.
    """
    found = _scan_resolved(tmp_path, """
        from typing import Any

        from wreath.crud import Access


        class Species: ...


        def mount(application: Any, open_session: Any) -> None:
            application.crud(
                Species, open_session,
                prefix="/admin/species",
                readonly=("id",),
                authorize={"list": Access.roles("admin"), "create": Access.deny()},
            )
    """, "crud_mount")
    assert _by(found, "crud.drop-operation-authorize"), [c.operator for c in found]


def test_an_mcp_tools_gates_are_mutable(tmp_path: Path) -> None:
    """`@mcp.tool(action=..., sampling=..., elicitation=...)` in a factory.

    `mcp` is a local, so the callee never resolved and none of the three gates
    was offered. The sampling and elicitation gates are the ones the roadmap
    argues hardest for: an elicitation is a phishing surface wearing a trusted
    client's chrome, and being able to decline is the control.
    """
    found = _scan_resolved(tmp_path, """
        from wreath.mcp import MCP, ToolRateLimit


        def build():
            mcp = MCP(name="camera-trap", version="1")

            @mcp.tool(description="Find sightings.", action="Sighting::find",
                      resource="all")
            async def find_sightings(context): ...

            @mcp.tool(description="Delete a camera.", action="Camera::delete",
                      rate_limit=ToolRateLimit(2, 60.0),
                      sampling="Sighting::summarise",
                      elicitation="Camera::confirm")
            async def delete_camera(context): ...

            return mcp
    """, "mcp_factory")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for gate in ("action", "sampling", "elicitation", "rate_limit"):
        assert any(f"`{gate}=`" in control for control in dropped), (gate, dropped)


def test_a_grpc_methods_controls_are_mutable(tmp_path: Path) -> None:
    """`service.unary(..., roles=...)` in a factory.

    `wreath.grpc` declares a method with `**metadata` forwarded to
    `RouteDefinition`, so its controls *are* a route's -- but the call is not
    route-shaped (no verb, no literal `/`-path) and `service` is a local, so both
    branches of the declaration operator declined and a gRPC method's guards were
    mutated not at all. An authorization control the mutation tester cannot see
    is the hole this whole section exists to close.

    The source is written the way a real service is rather than imported from
    `wreath.grpc`: the operators read the tree, and the table is keyed on the
    call name, so this holds whether or not the module is present.
    """
    found = _scan_resolved(tmp_path, """
        from typing import Any


        def build(service: Any) -> None:
            @service.unary(request=dict, response=dict,
                           permissions=("track:read",))
            async def GetPosition(request, message): ...

            @service.server_stream(request=dict, response=dict,
                                   roles=("ranger",), rate_limit=(2, 60.0))
            async def WatchPositions(request, message): ...
    """, "grpc_service")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for control in ("permissions", "roles", "rate_limit"):
        assert any(f"`{control}=`" in name for name in dropped), (control, dropped)


def test_a_grpc_methods_wire_types_are_not_treated_as_controls(
    tmp_path: Path,
) -> None:
    """The negative space, and the reason the table names calls not keywords.

    `request=`/`response=` are the message types, not guards -- and they are
    *required* keyword parameters, so dropping one does not fall back to a
    default, it raises. A broken mutant that a test kills inflates the score,
    which is the failure `_defaulted_keywords` already declines a `**kwargs`
    callee to avoid. They stay out because the entry lists controls explicitly.
    """
    found = _scan_resolved(tmp_path, """
        from typing import Any


        def build(service: Any) -> None:
            @service.bidi(request=dict, response=dict, roles=("ranger",))
            async def Chat(request, message): ...
    """, "grpc_wire_types")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`roles=`" in name for name in dropped), dropped
    for wire in ("request", "response"):
        assert not any(f"`{wire}=`" in name for name in dropped), (wire, dropped)


def test_an_mcp_servers_bounds_are_already_widenable(tmp_path: Path) -> None:
    """`MCPLimits(...)` needs no operator of its own.

    Its bounds are numeric keywords on a resolvable dataclass, so
    `declaration.widen-bound` already reaches them -- verified rather than
    assumed, because "covered by an existing operator" is exactly the claim
    that rots. `ToolRateLimit(2, 60.0)` is *positional*, so nothing is offered
    for it: a keyword operator has no keyword to widen, and that is a real
    limit rather than an oversight.
    """
    found = _scan_resolved(tmp_path, """
        from wreath.mcp import MCP, MCPLimits, ToolRateLimit


        def build():
            mcp = MCP(name="t", version="1",
                      limits=MCPLimits(max_tools=256, max_sessions=1024,
                                       session_idle_seconds=900.0))

            @mcp.tool(description="d", rate_limit=ToolRateLimit(2, 60.0))
            async def probe(context): ...

            return mcp
    """, "mcp_limits")
    widened = {c.control.split("`")[1] for c in _by(found, "declaration.widen-bound")}
    assert widened == {"max_tools=256", "max_sessions=1024",
                       "session_idle_seconds=900.0"}, widened


def test_a_graphql_authorizer_is_a_control(tmp_path: Path) -> None:
    """`authorizer=` is what makes every field ask Cedar at all.

    `action=` and `expose=` were already offered; `authorizer=` was not, for
    the same missing-keyword reason as crud's `authorize=`.
    """
    found = _scan_resolved(tmp_path, """
        from wreath.graphql import GraphQL


        def build(registry, authorizer):
            return GraphQL(registry, authorizer=authorizer, action="read",
                           expose=("email",), introspection=False,
                           max_page_size=100)
    """, "graphql_factory")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`authorizer=`" in control for control in dropped), dropped


def test_a_graphql_fields_policy_is_mutable_where_the_endpoint_is_built(
    tmp_path: Path,
) -> None:
    """`@api.field(..., policy=...)` in a factory, which is the whole surface.

    `GraphQL(authorizer=...)` was reachable because `GraphQL` is an imported
    module global. The *per-field* declarations were not: `api` is a local, so
    `_resolve_callee` declined, and `api.field(...)`/`api.query(...)`/
    `api.mutation(...)` are not route-shaped -- which left GraphQL's entire
    authorization vocabulary, one policy per field, mutated not at all while the
    constructor keyword beside it was covered.

    A GraphQL field's `policy=` is the same kind of sentence as an MCP tool's
    `action=`: *this field was gated on that resource*. Dropping it falls back to
    the derived `Type.field`, which is a different resource, so a policy set
    written for the explicit one no longer names it.
    """
    found = _scan_resolved(tmp_path, """
        from typing import Any


        def build(registry: Any) -> Any:
            from wreath.graphql import GraphQL

            api = GraphQL(registry, models=[], authorizer=None)

            @api.field("User", "postCount", returns="Int", policy="Billing::read")
            async def post_count(users, info): ...

            @api.query("search", returns="User", policy="Query::search", cost=25)
            async def search(info): ...

            @api.mutation("retire", returns="User", policy="Mutation::retire")
            async def retire(info): ...

            return api
    """, "graphql_fields")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for call in ("api.field", "api.query", "api.mutation"):
        assert any(
            f"`policy=` on `{call}(...)`" in control for control in dropped
        ), (call, dropped)
    # One mutant per *field*, which is the property plan 05 asked for and did
    # not have. It needed no GraphQL-specific operator in the end: the per-field
    # declaration is an ordinary declaring call, so `declaration.drop-keyword`
    # reaches it once the table names the call.
    per_field = [control for control in dropped if "`policy=` on" in control]
    assert len(per_field) == 3, per_field


def test_an_mcp_servers_oauth_boundary_is_a_control(tmp_path: Path) -> None:
    """`MCP(auth=MCPAuth(...))` is the whole authorization boundary.

    Without it the endpoint is exactly as protected as the route, which for a
    bare application is not at all -- so dropping the keyword is the single
    largest undeclaration available on this surface. It was not offered, because
    `auth` was missing from `CONTROL_KEYWORDS` while `authorizer`, `verifier`
    and `audience` were all in it.
    """
    found = _scan_resolved(tmp_path, """
        from typing import Any

        from wreath.mcp import MCP, MCPAuth


        def build(app: Any, verifier: Any) -> Any:
            return MCP(
                app,
                name="camera-trap",
                version="1",
                auth=MCPAuth(resource="https://example.test/mcp", verifier=verifier),
            )
    """, "mcp_boundary")
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`auth=` on `MCP(...)`" in control for control in dropped), dropped


def test_a_routes_permissions_keyword_is_mutable(tmp_path: Path) -> None:
    """`@app.get(path, permissions=...)` is a control and was not offered.

    `_route_metadata()` read `RouteDefinition`'s defaulted *fields*, and
    `permissions=` is not one of them -- the router folds it into `requirement`
    before building the record. So the decorator keyword that requires a named
    permission, on the spelling applications actually use (an `app` that is a
    local or a parameter), was the one route control the tester could not see.
    """
    path = tmp_path / "permissioned.py"
    path.write_text(textwrap.dedent(
        """
        def build_app(app):
            @app.get("/reports", permissions=("reports:read",))
            async def reports(request) -> dict:
                \"\"\"Every report.\"\"\"
                return {}

            return app
        """
    ), encoding="utf-8")
    found = _by(_scan(path), "declaration.drop-keyword")
    assert [c.control for c in found] == [
        "`permissions=` on `app.get(...)` (it falls back to the default)"
    ]


def test_a_routes_own_controls_are_mutable_where_the_route_is_built(tmp_path: Path) -> None:
    """`@app.get(path, dependencies=...)` goes through `**metadata`.

    Asking the decorator's signature whether `dependencies` has a default
    answers "there is no such parameter", so the operator declined and *the*
    control this tool exists to remove went unmutated. The answer is on
    `RouteDefinition`, one layer down.
    """
    path = tmp_path / "factory.py"
    path.write_text(textwrap.dedent(
        """
        from wreath import Wreath
        from wreath.binding import Depends


        async def only_staff(request) -> None:
            if not request.headers.get("x-staff"):
                raise PermissionError("staff only")


        def build_app():
            app = Wreath()

            @app.get("/reports", dependencies=(Depends(only_staff),))
            async def reports(request) -> dict:
                \"\"\"Every report.\"\"\"
                return {}

            return app
        """
    ), encoding="utf-8")
    found = _by(_scan(path), "declaration.drop-keyword")
    assert [c.control for c in found] == [
        "`dependencies=` on `app.get(...)` (it falls back to the default)"
    ]
    assert found[0].scope == ("build_app",)


def test_a_decorator_belongs_to_the_scope_it_is_written_in(tmp_path: Path) -> None:
    """A decorator runs where the `def` is, not inside the function.

    Attributing it to the decorated function produced a patch that recompiled a
    body nobody had changed. It was caught only because the bytecode came out
    identical -- luck, not design -- so the attribution is pinned here.
    """
    path = tmp_path / "routes.py"
    path.write_text(textwrap.dedent(
        """
        from wreath import Wreath
        from wreath.binding import Depends

        app = Wreath()


        async def only_staff(request) -> None:
            raise PermissionError("staff only")


        @app.get("/reports", dependencies=(Depends(only_staff),))
        async def reports(request) -> dict:
            \"\"\"Every report.\"\"\"
            return {}
        """
    ), encoding="utf-8")
    # Module level: no enclosing function to recompile, so nothing is offered
    # rather than something being offered that cannot be installed.
    assert _by(_scan(path), "declaration.drop-keyword") == []


# -- the patch machinery ----------------------------------------------------


def test_replacing_a_code_object_is_visible_through_an_existing_alias() -> None:
    """`from x import y` copies the function object's *reference*, not its body.

    Reloading the module would leave every such alias enforcing the original
    control; assigning `__code__` cannot, which is the whole reason this tool
    does not reload anything.
    """
    from wreath._mutant import patch as patch_module

    source = "def allowed(value):\n    return value > 0\n"
    tree = ast.parse(source, filename="<fixture>")
    operators.tag(tree)
    namespace: dict[str, Any] = {}
    exec(compile_module(tree, "<fixture>"), namespace)  # noqa: S102 - a fixture
    original: Any = namespace["allowed"]
    alias: Any = original

    comparison: Any = next(node for node in ast.walk(tree) if isinstance(node, ast.Compare))
    mutated = transform_module(
        tree, comparison._mutant_id, lambda _node: ast.Constant(value=True)
    )
    code = find_code(compile_module(mutated, "<fixture>"), "allowed")
    assert code is not None
    assert not same_bytecode(original.__code__, code)
    original.__code__ = code
    assert alias(-1) is True
    assert patch_module.PatchError is PatchError


def test_a_value_patch_rebinds_the_name_in_every_module_that_imported_it() -> None:
    import wreath.crud as crud

    patch = ValuePatch(module_name="wreath.crud", path=("SENSITIVE_FIELD",),
                       value=re.compile("(?!x)x"))
    before = crud.SENSITIVE_FIELD
    assert before.search("password") is not None
    patch.apply()
    try:
        assert crud.SENSITIVE_FIELD.search("password") is None
    finally:
        patch.undo()
    assert crud.SENSITIVE_FIELD is before


def test_a_value_patch_does_not_follow_an_interned_value_across_the_interpreter() -> None:
    """`is` does not mean "came from here" for a small int.

    `crud._MAX_PAGE_SIZE` is 100 and so is a constant in `ssl`; they are the
    same object. The alias sweep proposed rewriting the one in `ssl`, and the
    first such name it reached was read-only, so it raised. Succeeding would
    have been worse: a mutation whose real effect is somewhere nobody is looking
    is noise wearing a finding's clothes.
    """
    import ssl

    import wreath.crud as crud

    before = ssl.OP_NO_TICKET
    patch = ValuePatch(module_name="wreath.crud", path=("_MAX_PAGE_SIZE",), value=1 << 40)
    patch.apply()
    try:
        assert crud._MAX_PAGE_SIZE == 1 << 40
        assert ssl.OP_NO_TICKET == before
    finally:
        patch.undo()
    assert crud._MAX_PAGE_SIZE == 100


def test_a_mutation_that_compiles_to_the_same_bytecode_is_not_a_finding() -> None:
    source = "def f(a, b):\n    return a and b\n"
    tree = ast.parse(source)
    code = compile_module(tree, "<same>")
    assert same_bytecode(code, compile_module(ast.parse(source), "<same>"))


def test_a_patch_whose_target_moved_is_refused_rather_than_silently_skipped() -> None:
    """A patch that cannot apply must be an ERROR, never a survivor.

    A survivor is read as "your tests would not notice". A mutation that never
    reached the interpreter would say that falsely, about a control the suite
    may well be watching.
    """
    code = find_code(compile_module(ast.parse("def other():\n    return 1\n"), "<x>"), "other")
    assert code is not None
    patch = CodePatch(module_name="wreath.crud", scope="crud_router", code=code)
    with pytest.raises(PatchError):
        patch.verify()


# -- planning ---------------------------------------------------------------


def test_a_module_is_named_by_walking_up_while_there_is_an_init(tmp_path: Path) -> None:
    package = tmp_path / "shop" / "web"
    package.mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "routes.py").write_text("")
    assert module_name_for(package / "routes.py") == "shop.web.routes"


def test_planning_declines_a_mutation_it_cannot_build_and_says_why(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    plan = build_plan([broken], tmp_path)
    assert plan.mutations == []
    assert any("unreadable" in reason for _, reason in plan.errors)


# -- the report -------------------------------------------------------------


def test_the_report_separates_a_control_nobody_watches_from_one_nobody_reaches() -> None:
    from wreath._mutant.model import Mutation, Site

    def verdict(outcome: Outcome, control: str) -> Verdict:
        site = Site(path="app/policies.py", line=12, scope="gate")
        return Verdict(Mutation("id", "guard.remove-raise", control, site, "app"), outcome)

    report = Report(verdicts=[
        verdict(Outcome.SURVIVED, "the refusal `raise Forbidden(...)`"),
        verdict(Outcome.UNREACHED, "the role check"),
        verdict(Outcome.KILLED, "the audience check"),
    ])
    text = render(report)
    assert "SURVIVED" in text and "UNREACHED" in text
    assert text.index("SURVIVED") < text.index("UNREACHED")
    assert "REVIEW ASSERTIONS" in text
    assert "%" not in text
    assert report.score == pytest.approx(0.5)
    document = report.to_json()
    assert document["rating"]["label"] == "REVIEW ASSERTIONS"
    assert "score" not in document


@pytest.mark.parametrize(
    ("counts", "label"),
    [
        ({"killed": 3}, "SAMPLE WATCHED"),
        ({"survived": 1}, "REVIEW ASSERTIONS"),
        ({"unreached": 1}, "ADD COVERAGE"),
        ({"timeout": 1}, "FINISH THE SAMPLE"),
        ({}, "NO RATING"),
    ],
)
def test_confidence_ratings_name_the_next_action(
    counts: dict[str, int], label: str
) -> None:
    from wreath._mutant.model import rate_counts

    assert rate_counts(counts).label == label


# -- end to end -------------------------------------------------------------


PROJECT = {
    "shop/__init__.py": "",
    "shop/gate.py": textwrap.dedent(
        '''
        """Two controls. One of them has a test."""


        class Forbidden(Exception):
            pass


        def authorize(identity, roles):
            """Refuse a caller who is missing a role."""
            if identity is None:
                raise Forbidden("anonymous")
            if not roles.issubset(identity["roles"]):
                raise Forbidden("missing role")
            return True


        def redact(row, sensitive):
            """Withhold the sensitive columns."""
            return {k: v for k, v in row.items() if k not in sensitive}
        '''
    ),
    "tests/test_gate.py": textwrap.dedent(
        '''
        import pytest

        from shop.gate import Forbidden, authorize, redact


        def test_a_caller_without_the_role_is_refused():
            with pytest.raises(Forbidden):
                authorize({"roles": {"reader"}}, {"admin"})


        def test_a_caller_with_the_role_is_admitted():
            assert authorize({"roles": {"admin"}}, {"admin"}) is True


        def test_redaction_runs():
            # Exercises `redact` without ever asserting that anything is withheld:
            # the shape ADR 0024 names, and the survivor this run must report.
            assert "id" in redact({"id": 1, "token": "t"}, {"token"})
        '''
    ),
    "pyproject.toml": textwrap.dedent(
        """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """
    ),
}


#: A crud-shaped declaration where one operation is tested and one is not, and
#: one protected column is tested and one is not. The point of the per-entry
#: operators is exactly this asymmetry: the wholesale `declaration.drop-keyword`
#: mutant dies to the `delete` test alone and reports `list` as covered.
CRUD_PROJECT = {
    "shop/__init__.py": "",
    "shop/access.py": textwrap.dedent(
        '''
        """A stand-in for `wreath.crud.Access`, so the fixture needs no database."""


        class Access:
            def __init__(self, verdict):
                self._verdict = verdict

            def __call__(self, identity):
                return self._verdict(identity)

            @classmethod
            def roles(cls, *names):
                wanted = set(names)
                return cls(lambda identity: wanted.issubset(identity["roles"]))

            @classmethod
            def deny(cls):
                return cls(lambda identity: False)

            @classmethod
            def public(cls):
                return cls(lambda identity: True)
        '''
    ),
    "shop/api.py": textwrap.dedent(
        '''
        """A generated router, declared the way an application declares one."""

        from .access import Access


        class Account:
            pass


        def _open(request):
            return None


        def crud_router(model, open_session, *, authorize=None, readonly=()):
            return {"authorize": dict(authorize or {}), "readonly": tuple(readonly)}


        def build():
            return crud_router(
                Account, _open,
                readonly=("id", "created_at"),
                authorize={
                    "list": Access.roles("reader"),
                    "delete": Access.deny(),
                },
            )


        def may(router, operation, identity):
            """Whether `identity` may perform `operation`.

            An operation with no rule falls back to permitted, which is what
            makes dropping one entry a real control removal rather than a
            crash.
            """
            rule = router["authorize"].get(operation)
            return True if rule is None else rule(identity)


        def writable(router, column):
            return column not in router["readonly"]
        '''
    ),
    "tests/test_api.py": textwrap.dedent(
        '''
        from shop.api import build, may, writable

        ANYONE = {"roles": set()}


        def test_delete_is_refused_outright():
            # Watches the `delete` entry: dropping it, or widening it to
            # `public`, makes this pass where it should not.
            assert may(build(), "delete", ANYONE) is False


        def test_listing_runs():
            # Exercises `list` without ever asserting that anyone is refused --
            # so the `list` entry is unwatched, and its mutants must survive.
            assert may(build(), "list", {"roles": {"reader"}}) is True


        def test_the_id_column_is_not_writable():
            assert writable(build(), "id") is False


        def test_a_row_can_be_written():
            # Never asserts that `created_at` is protected: the second column's
            # mutant must survive while the first one's dies.
            assert writable(build(), "name") is True
        '''
    ),
    "pyproject.toml": textwrap.dedent(
        """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """
    ),
}


def _run_mutant(root: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop",
         "--format", "json", "--quiet", *args],
        cwd=root, capture_output=True, text=True, timeout=_NESTED_TIMEOUT, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    if completed.returncode != 0:
        pytest.fail(
            f"wreath mutant exited {completed.returncode}\n{completed.stderr[-4000:]}"
        )
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def crud_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("mutant-crud")
    for relative, body in CRUD_PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return _run_mutant(root)


def _crud_outcomes(run: dict, operator: str) -> dict[str, str]:
    """`{the entry named in the control: outcome}` for one operator."""
    return {
        mutant["control"].split("`")[1]: mutant["outcome"]
        for mutant in run["mutants"]
        if mutant["operator"] == operator
    }


@_CRUD_GROUP
def test_dropping_one_operations_rule_kills_and_survives_independently(
    crud_run: dict,
) -> None:
    """The pair that justifies the operator.

    `delete` is watched and `list` is not. A wholesale `authorize=` mutant dies
    to the `delete` test and reports the whole mapping as covered; per entry,
    the unwatched operation is visible as a survivor.
    """
    outcomes = _crud_outcomes(crud_run, "crud.drop-operation-authorize")
    assert outcomes["delete"] == "killed", crud_run["mutants"]
    assert outcomes["list"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_widening_one_rule_to_public_kills_and_survives_independently(
    crud_run: dict,
) -> None:
    """Two operators, because they are two findings.

    The fixture declares `delete: Access.deny()` -- an outright refusal, watched
    by a test -- and `list: Access.roles("reader")` -- a narrowing, watched by
    nothing. Under one operator name those two land in one number, and the
    serious one (an operation nobody ever checked is refused) is averaged into
    the mild one.
    """
    refused = _crud_outcomes(crud_run, "crud.permit-refused-operation")
    widened = _crud_outcomes(crud_run, "crud.widen-access")
    assert refused == {"delete": "killed"}, crud_run["mutants"]
    assert list(widened) == ["list"], crud_run["mutants"]
    assert widened["list"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_unprotecting_one_column_kills_and_survives_independently(
    crud_run: dict,
) -> None:
    """`readonly=("id", "created_at")` with a test for only the first."""
    outcomes = _crud_outcomes(crud_run, "crud.unprotect-column")
    assert outcomes["id"] == "killed", crud_run["mutants"]
    assert outcomes["created_at"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_the_wholesale_keyword_mutant_is_the_one_that_overstates(
    crud_run: dict,
) -> None:
    """Why the per-entry operators had to exist.

    The coarse mutant dies -- `delete` has a test -- and on its own it would
    report `authorize=` as a watched control. The per-entry survivors above are
    the same declaration telling the truth.
    """
    wholesale = [
        m for m in crud_run["mutants"]
        if m["operator"] == "declaration.drop-keyword" and "authorize" in m["control"]
    ]
    assert wholesale, crud_run["mutants"]
    assert all(m["outcome"] == "killed" for m in wholesale), wholesale


# -- a Cedar policy compiled at import time ---------------------------------
#
# The shape every application writes, and the one this tool used to be unable
# to touch: the text is bound to a module global and a `CedarPolicies` is built
# from it on the next line, so rebinding the global reaches the string and not
# the engine that answers. Measured over `example/camera_trap/policies.py`
# before the fix: 0 killed, 18 survived. The project below is that shape in
# miniature, with one watched policy and one unwatched one, so a run that
# reports both KILLED is as wrong as one that reports both SURVIVED.

CEDAR_PROJECT = {
    "guard/__init__.py": "",
    "guard/policy.py": textwrap.dedent(
        '''
        """One policy set, parsed at import, exactly as an application writes it."""

        from wreath.authorization import CedarEntity, CedarPolicies, EntityUid

        POLICY_SOURCE = """
        // Rangers respond to incidents; they may read a sensitive record.
        permit(principal in Role::"ranger", action == Action::"read", resource)
          when { resource.tier == "sensitive" };

        // Anyone signed in may read an open record. Note the semicolon in this
        // sentence; splitting the source on a bare `;` used to cut it in half.
        permit(principal, action == Action::"read", resource)
          when { resource.tier == "open" };

        // Forbid wins over every permit, including ones added later.
        forbid(principal, action, resource)
          when { principal has suspended && principal.suspended == true };
        """

        ENGINE = CedarPolicies(POLICY_SOURCE)


        def may_read(*, roles=(), tier="open", suspended=False):
            principal = CedarEntity(
                EntityUid("User", "u1"),
                attrs={"suspended": suspended},
                parents=tuple(EntityUid("Role", role) for role in sorted(roles)),
            )
            resource = EntityUid("Record", tier)
            decision = ENGINE.is_authorized(
                principal=principal.uid,
                action=EntityUid("Action", "read"),
                resource=resource,
                context={},
                entities=(principal, CedarEntity(resource, attrs={"tier": tier})),
            )
            return bool(getattr(decision, "allowed", decision))
        '''
    ),
    "tests/test_policy.py": textwrap.dedent(
        '''
        from guard.policy import may_read


        def test_a_volunteer_cannot_read_a_sensitive_record():
            # Watches the open-record permit's `when` clause: dropping it lets
            # anyone read anything, and this is what notices.
            assert may_read(roles=(), tier="sensitive") is False


        def test_a_ranger_can_read_a_sensitive_record():
            assert may_read(roles=("ranger",), tier="sensitive") is True


        def test_a_suspended_ranger_is_refused():
            # Watches the standing `forbid`. Flipping it to a permit makes this
            # pass -- but only if the mutation reaches the compiled engine.
            assert may_read(roles=("ranger",), tier="sensitive", suspended=True) is False


        def test_an_open_record_is_readable():
            # Exercises the ranger permit's tier without ever asserting that a
            # ranger is refused anything, so widening that clause goes unseen.
            assert may_read(roles=(), tier="open") is True
        '''
    ),
    "pyproject.toml": textwrap.dedent(
        """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """
    ),
}


@pytest.fixture(scope="module")
def cedar_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("mutant-cedar")
    for relative, body in CEDAR_PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "guard",
         "--format", "json", "--quiet"],
        cwd=root, capture_output=True, text=True, timeout=_NESTED_TIMEOUT, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    if completed.returncode != 0:
        pytest.fail(f"wreath mutant exited {completed.returncode}\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout)


def _cedar_outcomes(run: dict, operator: str) -> dict[str, str]:
    return {
        mutant["control"].split("`")[1]: mutant["outcome"]
        for mutant in run["mutants"]
        if mutant["operator"] == operator
    }


@_CEDAR_GROUP
def test_flipping_a_forbid_reaches_the_engine_compiled_at_import(
    cedar_run: dict,
) -> None:
    """The load-bearing one.

    `ENGINE` was built from `POLICY_SOURCE` while the module imported, so a
    patch that only rebinds the name leaves the standing refusal in force and
    the mutant survives having removed nothing. There *is* a test watching it,
    so KILLED is the only honest outcome.
    """
    outcomes = [
        mutant["outcome"]
        for mutant in cedar_run["mutants"]
        if mutant["operator"] == "cedar.flip-effect"
    ]
    assert outcomes == ["killed"], cedar_run["mutants"]


@_CEDAR_GROUP
def test_a_watched_and_an_unwatched_clause_are_told_apart(cedar_run: dict) -> None:
    """The pair. Reaching the engine is worth nothing if everything now dies."""
    outcomes = _cedar_outcomes(cedar_run, "cedar.drop-condition")
    # Dropping the open-record permit's condition lets *anyone* read a
    # sensitive record, which the volunteer test asserts against.
    watched = 'when { resource.tier == "open" }'
    # Dropping the ranger permit's condition widens rangers to every tier, and
    # nothing here ever asserts that a ranger is refused anything.
    unwatched = 'when { resource.tier == "sensitive" }'
    assert outcomes.get(watched) == "killed", cedar_run["mutants"]
    assert outcomes.get(unwatched) in ("survived", "unreached"), cedar_run["mutants"]


@_CEDAR_GROUP
def test_deleting_the_policy_a_test_watches_is_killed(cedar_run: dict) -> None:
    deleted = _cedar_outcomes(cedar_run, "cedar.delete-policy")
    ranger = next(key for key in deleted if 'Role::"ranger"' in key)
    assert deleted[ranger] == "killed", cedar_run["mutants"]


@_CEDAR_GROUP
def test_no_cedar_mutant_is_declined_for_failing_to_parse(cedar_run: dict) -> None:
    """A mutation nobody could have noticed is not evidence about a suite.

    Splitting the source on every `;` produced fragments cut out of the middle
    of a comment; some deleted a sentence and one did not parse. Both were
    invisible while the patch changed nothing.
    """
    declined = [m for m in cedar_run["mutants"] if m["outcome"] in ("error", "declined")]
    assert declined == [], declined


@_CEDAR_GROUP
def test_a_comment_is_never_offered_as_a_policy(cedar_run: dict) -> None:
    controls = [
        mutant["control"]
        for mutant in cedar_run["mutants"]
        if mutant["operator"] == "cedar.delete-policy"
    ]
    assert controls, cedar_run["mutants"]
    assert all("//" not in control for control in controls), controls
    assert all(
        "permit" in control or "forbid" in control for control in controls
    ), controls


# -- bounding a pass onto code you just wrote -------------------------------


def _write_project(root: Path, project: dict[str, str]) -> None:
    for relative, body in project.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop", "--quiet", *args],
        cwd=root, capture_output=True, text=True, timeout=_NESTED_TIMEOUT, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )


def test_a_selector_that_matches_nothing_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A bound that selects nothing must not read as a clean run.

    `--only` matches a substring of `operator@path:line`, and the line in that
    id is the line the *operator* anchors to -- an operand's line inside a
    compound condition, a keyword's *value* line in a declaration -- not
    necessarily the line a human reads as the decision. Aiming at the latter
    selects zero, and the run then reports `0 killed, 0 survived` and exits 0,
    which is the ADR 0024 shape one level up: a check that passes because it
    has nothing to check. It cost a sibling agent a whole pass that described
    unrelated code.
    """
    root = tmp_path_factory.mktemp("mutant-empty-selector")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--only", "@shop/api.py:9999")
    assert completed.returncode == 2, completed.stdout[-2000:]
    assert "9999" in completed.stderr
    assert "matched no" in completed.stderr.lower(), completed.stderr


def test_a_selector_that_matches_is_still_accepted(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The refusal above must not fire on a working selector."""
    root = tmp_path_factory.mktemp("mutant-good-selector")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--only", "crud.widen-access")
    assert completed.returncode == 0, completed.stderr[-2000:]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin", "HOME": str(root),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def test_changed_bounds_a_pass_onto_the_lines_you_just_wrote(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`--limit N` takes the *first* N candidates, which is the wrong end.

    Mutations are ordered by line, so a bound of 40 over a 1500-line module
    spends the whole budget on whatever sits at the top of the file and reports
    coverage of code the run was not about. New work is appended, so it is
    exactly the code a limit cannot reach.

    `--changed` restricts candidates to lines that differ from a git ref, which
    is the shape the workflow actually has, and it composes with `--limit`.
    """
    root = tmp_path_factory.mktemp("mutant-changed")
    _write_project(root, CRUD_PROJECT)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    # Append a second, independently-controlled router at the tail of the file.
    api = root / "shop" / "api.py"
    api.write_text(api.read_text(encoding="utf-8") + textwrap.dedent(
        '''

        def build_reports():
            return crud_router(
                Account, _open,
                readonly=("secret",),
                authorize={"export": Access.deny()},
            )
        '''
    ), encoding="utf-8")

    completed = _cli(root, "--changed", "HEAD", "--format", "json")
    assert completed.returncode == 0, completed.stderr[-3000:]
    report = json.loads(completed.stdout)
    controls = [m["control"] for m in report["mutants"]]
    assert controls, report

    # Everything selected is from the appended block ...
    assert any("export" in c for c in controls), controls
    assert any("secret" in c for c in controls), controls
    # ... and nothing from the original declaration, which did not change.
    assert not any("delete" in c for c in controls), controls
    assert not any("created_at" in c for c in controls), controls


def test_changed_and_limit_compose(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The bound still bounds: `--changed` narrows, `--limit` truncates."""
    root = tmp_path_factory.mktemp("mutant-changed-limit")
    _write_project(root, CRUD_PROJECT)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    api = root / "shop" / "api.py"
    api.write_text(api.read_text(encoding="utf-8") + textwrap.dedent(
        '''

        def build_reports():
            return crud_router(
                Account, _open,
                readonly=("secret", "other"),
                authorize={"export": Access.deny(), "purge": Access.deny()},
            )
        '''
    ), encoding="utf-8")
    completed = _cli(root, "--changed", "HEAD", "--limit", "2", "--format", "json")
    assert completed.returncode == 0, completed.stderr[-3000:]
    assert len(json.loads(completed.stdout)["mutants"]) == 2


def test_sample_is_stable_bounded_and_drawn_across_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "shop"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    controls = "\n\n".join(
        f"def authorize_{index}(value):\n"
        f"    if value == {index}:\n"
        "        return value\n"
        "    return None"
        for index in range(40)
    )
    target = package / "checks.py"
    target.write_text(controls + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)

    first = sample_identifiers([package], tmp_path, 5)
    second = sample_identifiers([package], tmp_path, 5)

    assert first == second
    assert len(first) == 5
    lines = {int(identifier.rsplit(":", 1)[1].split("#", 1)[0]) for identifier in first}
    assert max(lines) > 30
    plan = build_plan([package], tmp_path, selected_ids=frozenset(first))
    assert {mutation.identifier for mutation in plan.mutations} == set(first)


def test_sample_and_limit_are_refused_as_competing_bounds(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("mutant-competing-bounds")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--sample", "1", "--limit", "1")
    assert completed.returncode == 2
    assert "alternative bounds" in completed.stderr


def test_budget_ceiling_reports_undecided_without_failing_the_pipeline(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("mutant-budget")
    _write_project(
        root,
        {
            "shop/__init__.py": "",
            "shop/gate.py": (
                "def authorize(value):\n"
                "    if value != 'ok':\n"
                "        raise PermissionError('refused')\n"
                "    return value\n"
            ),
            "tests/test_gate.py": (
                "import pytest\n"
                "from shop.gate import authorize\n"
                "def test_refuses():\n"
                "    with pytest.raises(PermissionError):\n"
                "        authorize('bad')\n"
            ),
        },
    )

    completed = _cli(
        root, "--sample", "1", "--budget", "0.0001", "--format", "json"
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["counts"]["timeout"] == 1
    assert document["rating"]["label"] == "FINISH THE SAMPLE"


def test_changed_outside_a_repository_says_so(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A bound that cannot be computed is refused, not silently ignored."""
    root = tmp_path_factory.mktemp("mutant-nogit")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--changed", "HEAD")
    assert completed.returncode == 2, completed.stdout[-2000:]
    assert "git" in completed.stderr.lower(), completed.stderr


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("mutant-e2e")
    activity = root / "mutation-activity.jsonl"
    for relative, body in PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop",
         "--format", "json", "--quiet", "--jobs", "2",
         "--activity-file", str(activity)],
        cwd=root, capture_output=True, text=True, timeout=_NESTED_TIMEOUT, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    if completed.returncode != 0:
        pytest.fail(f"wreath mutant exited {completed.returncode}\n{completed.stderr[-4000:]}")
    document = json.loads(completed.stdout)
    document["_activity"] = [
        json.loads(line) for line in activity.read_text(encoding="utf-8").splitlines()
    ]
    return document


def test_a_withheld_field_set_that_stops_withholding_is_a_mutation(module: Path) -> None:
    found = _by(_scan(module), "comprehension.drop-clause")
    assert found == [] or all("filter" in c.control for c in found)


def test_a_run_leaves_every_source_file_byte_identical(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """No file is ever rewritten.

    The mutated code object is assigned over the live function's `__code__`, so
    an interrupted run cannot leave a mutant on disk and `from x import y`
    aliases see the mutation anyway. An operator that could only be expressed by
    editing the file does not belong in this tool, and this is the assertion
    that says so about the ones just added.
    """
    root = tmp_path_factory.mktemp("mutant-untouched")
    for relative, body in CRUD_PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    sources = sorted(root.rglob("*.py"))
    before = {path: path.read_bytes() for path in sources}
    run = _run_mutant(root)
    assert run["counts"]["killed"] >= 1  # it really did mutate something
    assert {path: path.read_bytes() for path in sources} == before


def test_a_run_with_survivors_still_exits_zero(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A report, not a gate -- and `--fail-on-survivor` is the opt-in.

    The crud fixture has survivors by construction, so this is the case that
    would regress if the new operators were ever wired into a failing exit.
    """
    root = tmp_path_factory.mktemp("mutant-exit")
    for relative, body in CRUD_PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)}
    base = [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop", "--quiet"]
    completed = subprocess.run(
        base, cwd=root, capture_output=True, text=True, timeout=_NESTED_TIMEOUT, check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "SURVIVED" in completed.stdout or "UNREACHED" in completed.stdout

    # The opt-in changes only the interpretation of this completed report. It
    # used to launch the entire mutation engine a second time to prove one
    # boolean at the end of `execute_mutant`, adding more than a second to every
    # suite run while testing no additional mutation behavior.
    from wreath._mutant.cli import _exit_status
    from wreath._mutant.model import Mutation, Site

    mutation = Mutation(
        "id",
        "guard.remove-raise",
        "the refusal",
        Site("shop/api.py", 1, "gate"),
        "shop.api",
    )
    report = Report(verdicts=[Verdict(mutation, Outcome.SURVIVED)])
    assert _exit_status(report, fail_on_survivor=False) == 0
    assert _exit_status(report, fail_on_survivor=True) == 1


def test_mutant_is_not_one_of_the_gates() -> None:
    """`wreath check` must not run it.

    A survivor is a question rather than a verdict, and a question that fails
    the build gets answered with `|| true`. Pinned because adding operators is
    exactly the change that would tempt someone to promote it.
    """
    from wreath._devtools.tasks import _CHECKS

    assert not any("mutant" in name for name, _ in _CHECKS), _CHECKS


@_SAMPLE_GROUP
def test_a_run_reports_both_a_killed_and_a_surviving_control(sample_run: dict) -> None:
    """The guard on the guard.

    A tool that answered KILLED for everything would pass any test that only
    checked it found the covered control, and it would be worse than useless:
    it reports that a suite watches controls it does not watch.
    """
    outcomes = {(m["scope"], m["operator"]): m["outcome"] for m in sample_run["mutants"]}
    assert outcomes[("authorize", "guard.never-fires")] == "killed"
    survivors = [
        m for m in sample_run["mutants"]
        if m["scope"] == "redact" and m["outcome"] in ("survived", "unreached")
    ]
    assert survivors, sample_run["mutants"]
    assert survivors[0]["operator"] == "comprehension.drop-clause"
    assert sample_run["counts"]["killed"] >= 1
    assert sample_run["counts"]["survived"] + sample_run["counts"]["unreached"] >= 1


@_SAMPLE_GROUP
def test_the_run_names_the_test_that_caught_each_control(sample_run: dict) -> None:
    caught = [m for m in sample_run["mutants"] if m["outcome"] == "killed"]
    assert caught
    assert all(m["killers"] for m in caught), caught
    assert any(
        "test_a_caller_without_the_role_is_refused" in killer
        for m in caught for killer in m["killers"]
    )


@_SAMPLE_GROUP
def test_the_activity_stream_names_live_and_verified_test_files(sample_run: dict) -> None:
    events = sample_run["_activity"]
    assert events[0]["event"] == "planned"
    assert events[0]["total"] > 0
    started = [event for event in events if event["event"] == "started"]
    killed = [
        event
        for event in events
        if event["event"] == "finished" and event["outcome"] == "killed"
    ]
    assert started
    assert all(event["tests"] for event in started)
    assert killed
    assert all(event["killers"] for event in killed)


@_SAMPLE_GROUP
def test_parallel_mutants_launch_before_the_first_child_finishes(
    sample_run: dict,
) -> None:
    events = sample_run["_activity"]
    first_started = next(
        index for index, event in enumerate(events) if event["event"] == "started"
    )
    first_finished = next(
        index
        for index, event in enumerate(events[first_started:], start=first_started)
        if event["event"] == "finished"
    )
    started = [
        event
        for event in events[first_started:first_finished]
        if event["event"] == "started"
    ]

    assert len(started) == 2
    assert len({event["ordinal"] for event in started}) == 2


@_SAMPLE_GROUP
def test_a_mutant_only_runs_the_tests_that_reach_it(sample_run: dict) -> None:
    """Selection is the difference between a tool people run and one they mean to.

    Three tests exist; the role check inside `authorize` is reached by two of
    them, and `redact` by one. No mutant should ever run all three.
    """
    counts = {m["scope"]: m["candidates"] for m in sample_run["mutants"] if m["candidates"]}
    assert counts, sample_run["mutants"]
    assert max(counts.values()) < 3, counts


@_SAMPLE_GROUP
def test_a_refusal_no_test_ever_triggers_is_reported_unreached_not_survived(
    sample_run: dict,
) -> None:
    """ADR 0024, stated as an outcome.

    No test in the fixture calls `authorize(None, ...)`, so the anonymous
    refusal never executes. "Nothing would notice if this were deleted" and
    "nothing ever looks at this" are different reports with different fixes.
    """
    unreached = [
        m for m in sample_run["mutants"]
        if m["operator"] == "guard.remove-raise" and m["outcome"] == "unreached"
    ]
    assert unreached, sample_run["mutants"]


# -- resolve_scope: what a dotted path actually yields --------------------------
#
# Nothing tested `resolve_scope`, and it had a bug that cost real coverage: it
# walks a dotted `Class.method` path with `getattr`, and `getattr` *invokes* a
# descriptor. So a `classmethod` arrives as a method bound to the class, not as the
# `classmethod` object `_unwrap` was checking for, and every mutation inside every
# classmethod was refused with "is method, not a function".
#
# The refusal was reported as an `error` outcome. That is the part worth keeping in
# mind: it did not read as "51 classmethods across 25 files are unmeasurable", it
# read as eight lines of noise in a report whose score was computed without them.


class _Subject:
    """One class carrying every callable shape `resolve_scope` claims to unwrap."""

    LIMIT = 3

    def plain(self) -> bool:
        return self.LIMIT > 0

    @classmethod
    def made(cls, value: int) -> bool:
        return value <= cls.LIMIT

    @staticmethod
    def free(value: int) -> bool:
        return value > 0

    @property
    def ready(self) -> bool:
        return self.LIMIT > 0


def _wrapped_target() -> bool:
    return True


def _decorate(fn):
    import functools

    @functools.wraps(fn)
    def outer():
        return fn()

    return outer


decorated = _decorate(_wrapped_target)


@pytest.mark.parametrize(
    "scope,qualname",
    [
        ("_Subject.plain", "_Subject.plain"),
        ("_Subject.made", "_Subject.made"),
        ("_Subject.free", "_Subject.free"),
        ("_Subject.ready", "_Subject.ready"),
        ("decorated", "_wrapped_target"),
    ],
)
def test_resolve_scope_reaches_the_function_behind_every_callable_shape(
    scope: str, qualname: str
) -> None:
    """A method, a classmethod, a staticmethod, a property, and a wrapped function.

    `_Subject.made` is the arm that was failing. The others passed already and are
    here so that a future change to `_unwrap`'s ordering cannot fix one shape by
    breaking another -- the branches are tried in sequence and `MethodType` now
    comes first, which is the kind of ordering that gets 'tidied'.
    """
    from types import FunctionType

    import tests.test_mutant as this_module
    from wreath._mutant.patch import resolve_scope

    resolved = resolve_scope(this_module, scope)
    assert isinstance(resolved, FunctionType), (scope, type(resolved).__name__)
    assert resolved.__qualname__.endswith(qualname), (scope, resolved.__qualname__)


def test_resolve_scope_still_refuses_something_that_is_not_callable_at_all() -> None:
    """The refusal has to survive the fix, or a typo becomes a silent no-op."""
    import tests.test_mutant as this_module
    from wreath._mutant.patch import PatchError, resolve_scope

    with pytest.raises(PatchError, match="not a function"):
        resolve_scope(this_module, "_Subject.LIMIT")


def _representative_control_ids(targets: list[Path], *, dotted_only: bool) -> list[str]:
    """One mutation per callable scope, enough to exercise scope resolution."""
    selected: list[str] = []
    for target in targets:
        tree = ast.parse(target.read_text(encoding="utf-8"))
        seen: set[str] = set()
        for candidate in operators.scan(tree, module_name_for(target)):
            scope = candidate.scope_name
            if scope in seen or (dotted_only and "." not in scope):
                continue
            seen.add(scope)
            selected.append(f"{candidate.operator}@{target}:{candidate.line}")
    return selected


def test_no_real_module_reports_a_scope_it_cannot_patch() -> None:
    """The blind spot itself, stated as the absence of its error message.

    A unit test on `resolve_scope` proves the helper resolves each shape. This
    proves the planner then *builds* for them, which is the part that failed and the
    part a caller sees. It runs over real wreath modules rather than a throwaway
    package because `build_plan` resolves a target by importing it by name: a
    fixture package would have to be put on `sys.path` to be importable at all, and
    the modules that ship are both importable already and the ones that matter.

    Before the `MethodType` branch in `_unwrap`, `_auth/cedar_engine.py` alone
    produced eight of these -- one per control inside `EntityUid.parse` -- and they
    were counted as `error`, not as unmeasured, so the score was computed as though
    those controls did not exist.
    """
    targets = [
        Path("src/wreath/_auth/cedar_engine.py"),
        Path("src/wreath/orm/types.py"),
        Path("src/wreath/_sparsevec.py"),
    ]
    targets = [path for path in targets if path.exists()]
    only = _representative_control_ids(targets, dotted_only=True)
    assert only, "the real modules contain no method-held controls"
    plan = build_plan(targets, Path("."), only=only)
    unpatchable = [pair for pair in plan.errors if "not a function" in pair[1]]
    assert unpatchable == [], unpatchable


def test_a_control_inside_a_classmethod_is_planned_not_skipped() -> None:
    """Every classmethod holding a control contributes a mutation, by name.

    The scopes are derived from the module's own AST rather than hard-coded, so this
    keeps meaning what it says when `cedar_engine` gains or loses a classmethod --
    and it fails, rather than passing vacuously, if the module ever has none.
    """
    target = Path("src/wreath/_auth/cedar_engine.py")
    tree = ast.parse(target.read_text(encoding="utf-8"))
    classmethods = {
        f"{outer.name}.{inner.name}"
        for outer in ast.walk(tree)
        if isinstance(outer, ast.ClassDef)
        for inner in outer.body
        if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "classmethod" for d in inner.decorator_list)
    }
    assert classmethods, "this module has no classmethod left; move this test"

    candidates = operators.scan(tree, module_name_for(target))
    only = [
        f"{candidate.operator}@{target}:{candidate.line}"
        for candidate in candidates
        if candidate.scope_name in classmethods
    ]
    assert only, "no classmethod contains a control"
    plan = build_plan([target], Path("."), only=only)
    planned = {m.site.scope for m in plan.mutations if m.patch is not None}
    reached = classmethods & planned
    assert reached, (
        f"no classmethod contributed a mutation; classmethods={sorted(classmethods)} "
        f"planned={sorted(planned)}"
    )
