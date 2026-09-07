from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from wreath._mutant import cli as mutant_cli
from wreath._mutant import operators
from wreath._mutant import runner as mutant_runner
from wreath._mutant.patch import (
    CodePatch,
    PatchError,
    ValuePatch,
    compile_module,
    compile_scope,
    find_code,
    same_bytecode,
    transform_module,
)
from wreath._mutant.runner import build_plan, module_name_for, sample_identifiers
from wreath.mutant import OPERATORS, Mutation, Outcome, Report, Site, Verdict, render

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


def test_catalog_records_do_not_allocate_instance_dictionaries() -> None:
    candidate = operators.Candidate("operator", "control", 1, ("scope",))
    site = Site("module.py", 1, "scope")
    mutation = Mutation("id", "operator", "control", site, "module")

    assert not hasattr(candidate, "__dict__")
    assert not hasattr(site, "__dict__")
    assert not hasattr(mutation, "__dict__")


def test_mutant_command_defaults_to_the_native_baseline_and_candidate_engine() -> None:
    parser = argparse.ArgumentParser()
    mutant_cli.add_arguments(parser)

    assert parser.parse_args([]).test_engine == "native"


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

#: What a whole-project nested run passes for `--jobs`. The shipped default is
#: 1, and it is right for the tool: `wreath mutant` runs beside whatever else is
#: on the machine. It is wrong for these fixtures, which fork eight to twelve
#: mutants that each take ~90ms behind ~500ms of serial baseline and planning,
#: so the run is almost entirely the tail. Measured over three runs each, the
#: crud project went 2.24s at one job to 1.58s at two and 1.18s at four, and the
#: cedar project 2.21s to 1.56s to 1.20s -- with byte-identical verdicts at every
#: setting, which is the part that had to be true before this was allowed. Two,
#: not four: the saving is mostly in the first step, and these fixtures already
#: hold an xdist worker while the other seven run.
_NESTED_JOBS = ("--jobs", "2")

_CRUD_GROUP = pytest.mark.xdist_group(name="mutant_crud")
_CEDAR_GROUP = pytest.mark.xdist_group(name="mutant_cedar")
_SAMPLE_GROUP = pytest.mark.xdist_group(name="mutant_sample")


def _scan(path: Path) -> list[operators.Candidate]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return operators.scan(tree, None)


def _by(found: list[operators.Candidate], operator: str) -> list[operators.Candidate]:
    return [c for c in found if c.operator == operator]


def test_every_named_operator_is_reachable_from_the_public_surface() -> None:
    assert len(OPERATORS) == 21
    assert set(OPERATORS) == {
        "predicate.drop-operand",
        "predicate.always-true",
        "expression.take-branch",
        "comprehension.drop-clause",
        "guard.remove-raise",
        "guard.never-fires",
        "guard.always-fires",
        "guard.drop-statement",
        "declaration.drop-keyword",
        "declaration.widen-bound",
        "crud.drop-operation-authorize",
        "crud.widen-access",
        "crud.permit-refused-operation",
        "crud.unprotect-column",
        "crud.expose-sensitive",
        "cedar.flip-effect",
        "cedar.drop-condition",
        "cedar.delete-policy",
        "value.widen-bound",
        "value.disable-pattern",
        "value.empty-denylist",
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
    found = _by(_scan(module), "expression.take-branch")
    assert len(found) == 2
    kept = {c.control.rsplit("always ", 1)[1] for c in found}
    assert kept == {"`principal`)", "`session`)"}


def test_a_predicate_named_like_a_permission_check_can_answer_true(module: Path) -> None:
    found = _by(_scan(module), "predicate.always-true")
    assert [c.control for c in found] == ["every check in `is_permitted` (it now answers True)"]


def test_the_always_true_mutation_watches_the_body_not_the_def_line(module: Path) -> None:
    found = _by(_scan(module), "predicate.always-true")[0]
    assert found.watch, "no watch lines: every test would be filtered out"
    assert found.line not in found.watch or len(found.watch) > 1


def test_a_guard_over_several_lines_watches_where_its_condition_starts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "guarded.py"
    source.write_text(
        "def check(stamp, ttl):\n"
        "    if (\n"
        "        isinstance(stamp, bool)\n"
        "        or not isinstance(stamp, int)\n"
        "        or stamp > ttl\n"
        "    ):\n"
        "        raise PermissionError('refused')\n"
        "    return True\n",
        encoding="utf-8",
    )
    guards = [c for c in _scan(source) if c.operator.startswith("guard.")]
    conditions = [c for c in guards if c.operator != "guard.remove-raise"]
    assert conditions, "the multi-line guard was not offered at all"
    for candidate in conditions:
        assert candidate.line == 2, "the anchor is still the `if` keyword"
        # The line that actually executes is the first operand's, and it must be
        # watched -- otherwise no test can ever be attributed to this mutation.
        assert 3 in candidate.watch


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
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_factory")
    dropped = {c.control for c in found if c.operator == "declaration.drop-keyword"}
    assert any("`authorize=`" in control for control in dropped), dropped


def test_each_crud_operation_is_verified_independently(tmp_path: Path) -> None:
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_ops")
    per_entry = _by(found, "crud.drop-operation-authorize")
    keys = {c.control.split("`")[1] for c in per_entry}
    assert keys == {"list", "create", "delete"}, [c.control for c in per_entry]


def test_a_crud_denial_can_be_turned_into_a_permit(tmp_path: Path) -> None:
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_deny")
    permitted = _by(found, "crud.permit-refused-operation")
    assert any("create" in c.control for c in permitted), [c.control for c in permitted]
    assert all("widen" not in c.operator for c in permitted), (
        "a refusal must not also be offered as a widening"
    )


def test_each_protected_column_is_verified_independently(tmp_path: Path) -> None:
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_readonly")
    per_column = _by(found, "crud.unprotect-column")
    assert [c.control.split("`")[1] for c in per_column] == ["id"], [c.control for c in per_column]


def test_revealing_one_withheld_column_is_a_mutant_per_column(tmp_path: Path) -> None:
    found = _scan_resolved(tmp_path, _CRUD_FACTORY, "crud_expose")
    exposed = _by(found, "crud.expose-sensitive")
    assert {c.control.split("`")[1] for c in exposed} == {"password_hash"}, [
        c.control for c in exposed
    ]
    # ... and the wholesale drop is still offered, so `expose=` is watched from
    # both directions: one mutant reveals a column, another removes the
    # exception that reveals one.
    assert any("`expose=`" in c.control for c in _by(found, "declaration.drop-keyword"))


def test_a_model_that_does_not_resolve_offers_no_expose_mutant(tmp_path: Path) -> None:
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
    found = _scan_resolved(
        tmp_path,
        """
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
    """,
        "crud_mount",
    )
    assert _by(found, "crud.drop-operation-authorize"), [c.operator for c in found]


def test_an_mcp_tools_gates_are_mutable(tmp_path: Path) -> None:
    found = _scan_resolved(
        tmp_path,
        """
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
    """,
        "mcp_factory",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for gate in ("action", "sampling", "elicitation", "rate_limit"):
        assert any(f"`{gate}=`" in control for control in dropped), (gate, dropped)


def test_a_grpc_methods_controls_are_mutable(tmp_path: Path) -> None:
    found = _scan_resolved(
        tmp_path,
        """
        from typing import Any


        def build(service: Any) -> None:
            @service.unary(request=dict, response=dict,
                           permissions=("track:read",))
            async def GetPosition(request, message): ...

            @service.server_stream(request=dict, response=dict,
                                   roles=("ranger",), rate_limit=(2, 60.0))
            async def WatchPositions(request, message): ...
    """,
        "grpc_service",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for control in ("permissions", "roles", "rate_limit"):
        assert any(f"`{control}=`" in name for name in dropped), (control, dropped)


def test_a_grpc_methods_wire_types_are_not_treated_as_controls(
    tmp_path: Path,
) -> None:
    found = _scan_resolved(
        tmp_path,
        """
        from typing import Any


        def build(service: Any) -> None:
            @service.bidi(request=dict, response=dict, roles=("ranger",))
            async def Chat(request, message): ...
    """,
        "grpc_wire_types",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`roles=`" in name for name in dropped), dropped
    for wire in ("request", "response"):
        assert not any(f"`{wire}=`" in name for name in dropped), (wire, dropped)


def test_an_mcp_servers_bounds_are_already_widenable(tmp_path: Path) -> None:
    found = _scan_resolved(
        tmp_path,
        """
        from wreath.mcp import MCP, MCPLimits, ToolRateLimit


        def build():
            mcp = MCP(name="t", version="1",
                      limits=MCPLimits(max_tools=256, max_sessions=1024,
                                       session_idle_seconds=900.0))

            @mcp.tool(description="d", rate_limit=ToolRateLimit(2, 60.0))
            async def probe(context): ...

            return mcp
    """,
        "mcp_limits",
    )
    widened = {c.control.split("`")[1] for c in _by(found, "declaration.widen-bound")}
    assert widened == {"max_tools=256", "max_sessions=1024", "session_idle_seconds=900.0"}, widened


def test_a_graphql_authorizer_is_a_control(tmp_path: Path) -> None:
    found = _scan_resolved(
        tmp_path,
        """
        from wreath.graphql import GraphQL


        def build(registry, authorizer):
            return GraphQL(registry, authorizer=authorizer, action="read",
                           expose=("email",), introspection=False,
                           max_page_size=100)
    """,
        "graphql_factory",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`authorizer=`" in control for control in dropped), dropped


def test_a_graphql_fields_policy_is_mutable_where_the_endpoint_is_built(
    tmp_path: Path,
) -> None:
    found = _scan_resolved(
        tmp_path,
        """
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
    """,
        "graphql_fields",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    for call in ("api.field", "api.query", "api.mutation"):
        assert any(f"`policy=` on `{call}(...)`" in control for control in dropped), (call, dropped)
    # One mutant per *field*, which is the property plan 05 asked for and did
    # not have. It needed no GraphQL-specific operator in the end: the per-field
    # declaration is an ordinary declaring call, so `declaration.drop-keyword`
    # reaches it once the table names the call.
    per_field = [control for control in dropped if "`policy=` on" in control]
    assert len(per_field) == 3, per_field


def test_an_mcp_servers_oauth_boundary_is_a_control(tmp_path: Path) -> None:
    found = _scan_resolved(
        tmp_path,
        """
        from typing import Any

        from wreath.mcp import MCP, MCPAuth


        def build(app: Any, verifier: Any) -> Any:
            return MCP(
                app,
                name="camera-trap",
                version="1",
                auth=MCPAuth(resource="https://example.test/mcp", verifier=verifier),
            )
    """,
        "mcp_boundary",
    )
    dropped = {c.control for c in _by(found, "declaration.drop-keyword")}
    assert any("`auth=` on `MCP(...)`" in control for control in dropped), dropped


def test_a_routes_permissions_keyword_is_mutable(tmp_path: Path) -> None:
    path = tmp_path / "permissioned.py"
    path.write_text(
        textwrap.dedent(
            """
        def build_app(app):
            @app.get("/reports", permissions=("reports:read",))
            async def reports(request) -> dict:
                \"\"\"Every report.\"\"\"
                return {}

            return app
        """
        ),
        encoding="utf-8",
    )
    found = _by(_scan(path), "declaration.drop-keyword")
    assert [c.control for c in found] == [
        "`permissions=` on `app.get(...)` (it falls back to the default)"
    ]


def test_a_routes_own_controls_are_mutable_where_the_route_is_built(tmp_path: Path) -> None:
    path = tmp_path / "factory.py"
    path.write_text(
        textwrap.dedent(
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
        ),
        encoding="utf-8",
    )
    found = _by(_scan(path), "declaration.drop-keyword")
    assert [c.control for c in found] == [
        "`dependencies=` on `app.get(...)` (it falls back to the default)"
    ]
    assert found[0].scope == ("build_app",)


def test_a_decorator_belongs_to_the_scope_it_is_written_in(tmp_path: Path) -> None:
    path = tmp_path / "routes.py"
    path.write_text(
        textwrap.dedent(
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
        ),
        encoding="utf-8",
    )
    # Module level: no enclosing function to recompile, so nothing is offered
    # rather than something being offered that cannot be installed.
    assert _by(_scan(path), "declaration.drop-keyword") == []


def test_replacing_a_code_object_is_visible_through_an_existing_alias() -> None:
    from wreath._mutant import patch as patch_module

    source = "def allowed(value):\n    return value > 0\n"
    tree = ast.parse(source, filename="<fixture>")
    operators.tag(tree)
    namespace: dict[str, Any] = {}
    exec(compile_module(tree, "<fixture>"), namespace)  # noqa: S102 - a fixture
    original: Any = namespace["allowed"]
    alias: Any = original

    comparison: Any = next(node for node in ast.walk(tree) if isinstance(node, ast.Compare))
    mutated = transform_module(tree, comparison._mutant_id, lambda _node: ast.Constant(value=True))
    code = find_code(compile_module(mutated, "<fixture>"), "allowed")
    assert code is not None
    assert not same_bytecode(original.__code__, code)
    original.__code__ = code
    assert alias(-1) is True
    assert patch_module.PatchError is PatchError


def test_a_value_patch_rebinds_the_name_in_every_module_that_imported_it() -> None:
    import wreath.crud as crud

    patch = ValuePatch(
        module_name="wreath.crud", path=("SENSITIVE_FIELD",), value=re.compile("(?!x)x")
    )
    before = crud.SENSITIVE_FIELD
    assert before.search("password") is not None
    patch.apply()
    try:
        assert crud.SENSITIVE_FIELD.search("password") is None
    finally:
        patch.undo()
    assert crud.SENSITIVE_FIELD is before


def test_a_value_patch_does_not_follow_an_interned_value_across_the_interpreter() -> None:
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


def test_a_value_mutation_updates_and_restores_captured_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = textwrap.dedent(
        """\
        LIMIT = 8192

        def current(value=LIMIT):
            return value

        def unrelated(value=8192):
            return value

        class Bucket:
            def __init__(self, *, capacity=LIMIT):
                self.capacity = capacity
        """
    )
    module_name = "_wreath_mutant_captured_default_fixture"
    fixture_path = tmp_path / f"{module_name}.py"
    fixture_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, fixture_path)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, fixture)
    spec.loader.exec_module(fixture)
    tree = ast.parse(source, filename=str(fixture_path))
    operators.tag(tree)
    candidate = next(
        item for item in operators.scan(tree, module_name) if item.operator == "value.widen-bound"
    )
    mutation = mutant_runner._build(
        candidate,
        tree,
        module_name,
        "fixture.py",
        str(fixture_path),
        "value.widen-bound@fixture.py:1",
    )
    assert isinstance(mutation, Mutation)
    patch = mutation.patch
    assert patch is not None

    patch.apply()
    try:
        assert fixture.LIMIT == 1 << 40
        assert fixture.current() == 1 << 40
        assert fixture.unrelated() == 8192
        assert fixture.Bucket().capacity == 1 << 40
    finally:
        patch.undo()

    assert fixture.LIMIT == 8192
    assert fixture.current() == 8192
    assert fixture.unrelated() == 8192
    assert fixture.Bucket().capacity == 8192


def test_a_mutation_that_compiles_to_the_same_bytecode_is_not_a_finding() -> None:
    source = "def f(a, b):\n    return a and b\n"
    tree = ast.parse(source)
    code = compile_module(tree, "<same>")
    assert same_bytecode(code, compile_module(ast.parse(source), "<same>"))


def test_scope_compilation_preserves_the_target_and_drops_unrelated_siblings() -> None:
    tree = ast.parse(
        """\
from __future__ import annotations

def unrelated():
    return "work that this mutation must not compile"

class Gate:
    def is_permitted(self, value: Missing) -> Missing:
        return value
"""
    )
    complete = compile_module(tree, "<scope>")
    narrowed = compile_scope(tree, "Gate.is_permitted", "<scope>")
    complete_target = find_code(complete, "Gate.is_permitted")
    narrowed_target = find_code(narrowed, "Gate.is_permitted")

    assert complete_target is not None
    assert narrowed_target is not None
    assert same_bytecode(complete_target, narrowed_target)
    assert find_code(narrowed, "unrelated") is None


def test_a_patch_whose_target_moved_is_refused_rather_than_silently_skipped() -> None:
    code = find_code(compile_module(ast.parse("def other():\n    return 1\n"), "<x>"), "other")
    assert code is not None
    patch = CodePatch(module_name="wreath.crud", scope="crud_router", code=code)
    with pytest.raises(PatchError):
        patch.verify()


def test_a_module_is_named_by_walking_up_while_there_is_an_init(tmp_path: Path) -> None:
    package = tmp_path / "shop" / "web"
    package.mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "routes.py").write_text("")
    assert module_name_for(package / "routes.py") == "shop.web.routes"
    assert module_name_for(package / "__init__.py") == "shop.web"


def test_planning_declines_a_mutation_it_cannot_build_and_says_why(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    plan = build_plan([broken], tmp_path)
    assert plan.mutations == []
    assert any("unreadable" in reason for _, reason in plan.errors)


def test_planning_imports_a_module_before_discovering_live_value_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "live_values"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / "policy.py"
    target.write_text("MAX_UPLOAD_SIZE = 1024\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    sys.modules.pop("live_values.policy", None)

    plan = build_plan([target], tmp_path, operators=("value",))

    assert [mutation.operator for mutation in plan.mutations] == ["value.widen-bound"]


def test_planning_declines_an_application_import_failure_without_ending_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "broken_import"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / "policy.py"
    target.write_text(
        "raise RuntimeError('missing deployment setting')\nMAX_UPLOAD_SIZE = 1024\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)
    sys.modules.pop("broken_import.policy", None)

    plan = build_plan([target], tmp_path)
    selection = mutant_runner.select_sample([target], tmp_path, 1)

    assert plan.mutations == []
    assert plan.errors == [
        (
            "broken_import.policy",
            "not importable: RuntimeError: missing deployment setting",
        )
    ]
    assert selection.identifiers == ()
    assert selection.errors == tuple(plan.errors)


def test_top_level_declaration_is_declined_with_the_supported_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "top_level_declaration"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / "routes.py"
    target.write_text(
        "def route(path, *, permissions=()):\n"
        "    return permissions\n"
        "DECLARED = route('/', permissions=('read',))\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)
    sys.modules.pop("top_level_declaration.routes", None)

    plan = build_plan([target], tmp_path, operators=("declaration",))

    assert plan.mutations == []
    assert any(
        "module-level declaration" in reason
        and "application factory function" in reason
        for _, reason in plan.errors
    )
    selection = mutant_runner.select_sample(
        [target], tmp_path, 1, operators=("declaration",)
    )
    assert selection.identifiers == ()
    assert selection.unsupported_declarations == (
        "declaration.drop-keyword@top_level_declaration/routes.py:3",
    )


def test_the_report_separates_a_control_nobody_watches_from_one_nobody_reaches() -> None:
    from wreath._mutant.model import Mutation, Site

    def verdict(outcome: Outcome, control: str) -> Verdict:
        site = Site(path="app/policies.py", line=12, scope="gate")
        return Verdict(Mutation("id", "guard.remove-raise", control, site, "app"), outcome)

    report = Report(
        verdicts=[
            verdict(Outcome.SURVIVED, "the refusal `raise Forbidden(...)`"),
            verdict(Outcome.UNREACHED, "the role check"),
            verdict(Outcome.KILLED, "the audience check"),
        ]
    )
    text = render(report)
    assert "SURVIVED" in text and "UNREACHED" in text
    assert text.index("SURVIVED") < text.index("UNREACHED")
    assert "REVIEW ASSERTIONS" in text
    assert "%" not in text
    assert report.score == pytest.approx(0.5)
    document = report.as_dict()
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
def test_confidence_ratings_name_the_next_action(counts: dict[str, int], label: str) -> None:
    from wreath._mutant.model import rate_counts

    assert rate_counts(counts).label == label


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
        """
        import pytest

        from shop.gate import Forbidden, authorize, redact


        def test_a_caller_without_the_role_is_refused():
            with pytest.raises(Forbidden):
                authorize({"roles": {"reader"}}, {"admin"})


        def test_a_caller_with_the_role_is_admitted():
            assert authorize({"roles": {"admin"}}, {"admin"}) is True


        def test_redaction_runs():
            # Exercises `redact` without ever asserting that anything is withheld:
            # the shape AGENTS.md names, and the survivor this run must report.
            assert "id" in redact({"id": 1, "token": "t"}, {"token"})
        """
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
        """
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
        """
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
        [
            sys.executable,
            "-m",
            "wreath._mutant.cli",
            "--path",
            "shop",
            "--format",
            "json",
            "--quiet",
            *_NESTED_JOBS,
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    if completed.returncode != 0:
        pytest.fail(f"wreath mutant exited {completed.returncode}\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout)


def test_native_mutant_engine_kills_the_same_control_without_pytest_children(
    tmp_path: Path,
) -> None:
    root = tmp_path / "native-mutant"
    package = root / "shop"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        """\
def authorize_positive(value):
    if value <= 0:
        raise ValueError("not positive")
    return "positive"
""",
        encoding="utf-8",
    )
    (tests / "test_policy.py").write_text(
        """\
import pytest

from shop.policy import authorize_positive

def test_positive_sign():
    assert authorize_positive(1) == "positive"

def test_non_positive_sign_is_refused():
    with pytest.raises(ValueError, match="not positive"):
        authorize_positive(0)
""",
        encoding="utf-8",
    )

    native = _run_mutant(
        root,
        "--test-engine",
        "native",
        "--only",
        "guard.remove-raise",
    )
    pytest_document = _run_mutant(
        root,
        "--test-engine",
        "pytest",
        "--only",
        "guard.remove-raise",
    )

    assert native["counts"]["killed"] >= 1
    assert {item["outcome"] for item in native["mutants"]} == {"killed"}

    def identity(item: dict[str, Any]) -> tuple[Any, Any, Any]:
        return item["id"], item["outcome"], item["killers"]

    assert [identity(item) for item in native["mutants"]] == [
        identity(item) for item in pytest_document["mutants"]
    ]


def test_native_mutants_fork_from_the_pristine_prepared_collection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "native-pristine"
    package = root / "shop"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        """\
calls = 0

def is_permitted(value):
    global calls
    calls += 1
    return value
""",
        encoding="utf-8",
    )
    (tests / "test_policy.py").write_text(
        """\
from shop import policy

def test_permitted_value():
    assert policy.calls == 0
    assert policy.is_permitted(True) is True
""",
        encoding="utf-8",
    )

    native = _run_mutant(
        root,
        "--test-engine",
        "native",
        "--only",
        "predicate.always-true",
    )
    pytest_document = _run_mutant(
        root,
        "--test-engine",
        "pytest",
        "--only",
        "predicate.always-true",
    )

    assert native["counts"] == pytest_document["counts"]
    assert native["counts"]["survived"] == 1


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
    outcomes = _crud_outcomes(crud_run, "crud.drop-operation-authorize")
    assert outcomes["delete"] == "killed", crud_run["mutants"]
    assert outcomes["list"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_widening_one_rule_to_public_kills_and_survives_independently(
    crud_run: dict,
) -> None:
    refused = _crud_outcomes(crud_run, "crud.permit-refused-operation")
    widened = _crud_outcomes(crud_run, "crud.widen-access")
    assert refused == {"delete": "killed"}, crud_run["mutants"]
    assert list(widened) == ["list"], crud_run["mutants"]
    assert widened["list"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_unprotecting_one_column_kills_and_survives_independently(
    crud_run: dict,
) -> None:
    outcomes = _crud_outcomes(crud_run, "crud.unprotect-column")
    assert outcomes["id"] == "killed", crud_run["mutants"]
    assert outcomes["created_at"] in ("survived", "unreached"), crud_run["mutants"]


@_CRUD_GROUP
def test_the_wholesale_keyword_mutant_is_the_one_that_overstates(
    crud_run: dict,
) -> None:
    wholesale = [
        m
        for m in crud_run["mutants"]
        if m["operator"] == "declaration.drop-keyword" and "authorize" in m["control"]
    ]
    assert wholesale, crud_run["mutants"]
    assert all(m["outcome"] == "killed" for m in wholesale), wholesale


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

        // Anyone signed in may read an open record; comments may contain semicolons.
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
        """
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
        """
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
        [
            sys.executable,
            "-m",
            "wreath._mutant.cli",
            "--path",
            "guard",
            "--format",
            "json",
            "--quiet",
            *_NESTED_JOBS,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT,
        check=False,
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
    outcomes = [
        mutant["outcome"]
        for mutant in cedar_run["mutants"]
        if mutant["operator"] == "cedar.flip-effect"
    ]
    assert outcomes == ["killed"], cedar_run["mutants"]


@_CEDAR_GROUP
def test_a_watched_and_an_unwatched_clause_are_told_apart(cedar_run: dict) -> None:
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
    assert all("permit" in control or "forbid" in control for control in controls), controls


def _write_project(root: Path, project: dict[str, str]) -> None:
    for relative, body in project.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop", "--quiet", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )


def test_a_selector_that_matches_nothing_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("mutant-empty-selector")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--only", "@shop/api.py:9999")
    assert completed.returncode == 2, completed.stdout[-2000:]
    assert "9999" in completed.stderr
    assert "matched no" in completed.stderr.lower(), completed.stderr


def test_a_selector_that_matches_is_still_accepted(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("mutant-good-selector")
    _write_project(root, CRUD_PROJECT)
    completed = _cli(root, "--only", "crud.widen-access")
    assert completed.returncode == 0, completed.stderr[-2000:]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def test_changed_bounds_a_pass_onto_the_lines_you_just_wrote(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("mutant-changed")
    _write_project(root, CRUD_PROJECT)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    # Append a second, independently-controlled router at the tail of the file.
    api = root / "shop" / "api.py"
    api.write_text(
        api.read_text(encoding="utf-8")
        + textwrap.dedent(
            """

        def build_reports():
            return crud_router(
                Account, _open,
                readonly=("secret",),
                authorize={"export": Access.deny()},
            )
        """
        ),
        encoding="utf-8",
    )

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
    root = tmp_path_factory.mktemp("mutant-changed-limit")
    _write_project(root, CRUD_PROJECT)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    api = root / "shop" / "api.py"
    api.write_text(
        api.read_text(encoding="utf-8")
        + textwrap.dedent(
            """

        def build_reports():
            return crud_router(
                Account, _open,
                readonly=("secret", "other"),
                authorize={"export": Access.deny(), "purge": Access.deny()},
            )
        """
        ),
        encoding="utf-8",
    )
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


def test_sample_represents_every_operator_family_before_filling_remaining_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "sample_families"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for index in range(4):
        (package / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)

    families = {
        "sample_families.module_0": "cedar.flip-effect",
        "sample_families.module_1": "value.widen-bound",
        "sample_families.module_2": "guard.never-fires",
        "sample_families.module_3": "predicate.drop-operand",
    }

    shared_scopes = []
    original_tag = mutant_runner.tag

    def tag(tree: ast.Module) -> dict[int, tuple[str, ...]]:
        scopes = original_tag(tree)
        shared_scopes.append(scopes)
        return scopes

    monkeypatch.setattr(mutant_runner, "tag", tag)

    def scan(
        _tree: ast.Module, module_name: str | None, *, scopes: dict[int, tuple[str, ...]]
    ) -> list[operators.Candidate]:
        assert scopes is shared_scopes[-1]
        assert module_name is not None
        if module_name == "sample_families":
            return []
        operator = families[module_name]
        amount = 1 if operator.startswith(("cedar", "value")) else 6
        return [
            operators.Candidate(operator, f"control {index}", 1, ("check",))
            for index in range(amount)
        ]

    monkeypatch.setattr(mutant_runner, "scan", scan)

    selection = mutant_runner.select_sample([package], tmp_path, 4)

    assert {identifier.split("@", 1)[0] for identifier in selection.identifiers} == set(
        families.values()
    )
    assert selection.eligible_candidates == 14
    assert selection.candidate_files == 4
    assert selection.selected_files == 4
    assert selection.missing_operators == ()
    assert selection.candidate_counts_by_operator == {
        "cedar.flip-effect": 1,
        "guard.never-fires": 6,
        "predicate.drop-operand": 6,
        "value.widen-bound": 1,
    }
    assert selection.selected_counts_by_operator == {
        operator: 1 for operator in sorted(families.values())
    }


def test_sample_reports_operator_families_a_smaller_budget_cannot_represent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "small_sample"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)

    shared_scopes = []
    original_tag = mutant_runner.tag

    def tag(tree: ast.Module) -> dict[int, tuple[str, ...]]:
        scopes = original_tag(tree)
        shared_scopes.append(scopes)
        return scopes

    monkeypatch.setattr(mutant_runner, "tag", tag)

    def scan(
        _tree: ast.Module, _module_name: str | None, *, scopes: dict[int, tuple[str, ...]]
    ) -> list[operators.Candidate]:
        assert scopes is shared_scopes[-1]
        return [
            operators.Candidate("cedar.flip-effect", "rare", 1, ("check",)),
            operators.Candidate("guard.never-fires", "common one", 2, ("check",)),
            operators.Candidate("guard.never-fires", "common two", 3, ("check",)),
        ]

    monkeypatch.setattr(mutant_runner, "scan", scan)

    selection = mutant_runner.select_sample([package], tmp_path, 1)

    assert selection.identifiers[0].startswith("cedar.flip-effect@")
    assert selection.missing_operators == ("guard.never-fires",)


def test_differential_fuzz_kills_a_survivor_and_keeps_a_minimized_artifact(
    tmp_path: Path,
) -> None:
    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import DifferentialFuzzConfig, apply_differential_fuzz
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    verdict = Verdict(mutation, Outcome.SURVIVED)
    report = Report(verdicts=[verdict])
    target = FuzzTarget(
        "policy-probe",
        lambda data: (f"enabled:{subject.enabled}", f"size:{len(data)}"),
        seeds=(b"distinguish me",),
        source_files=("policy.py",),
        operator_names=("guard.never-fires",),
    )

    apply_differential_fuzz(
        report,
        DifferentialFuzzConfig(
            tmp_path / "corpus",
            tmp_path / "artifacts",
            seed=7,
            max_cases=8,
            max_seconds=2,
            targets=(target,),
        ),
        workdir=tmp_path,
    )

    assert verdict.outcome is Outcome.KILLED
    assert verdict.killers[0].startswith("fuzz:policy-probe:")
    finding = verdict.fuzz_evidence[0]["finding"]
    assert Path(finding["input_path"]).is_file()
    assert Path(finding["metadata_path"]).is_file()
    assert subject.enabled is False
    assert report.differential_fuzz is not None
    assert report.differential_fuzz["master_seed"] == 7
    assert "differential fuzz:" in render(report)
    assert "master seed 7" in render(report)


def test_differential_fuzz_preserves_the_target_structured_strategy() -> None:
    from wreath._fuzz import FuzzTarget, StructuredStrategy
    from wreath._mutant.differential import _differential_target
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    strategy = StructuredStrategy("policy", 1, generate=lambda rng, size: b"generated")
    target = FuzzTarget(
        "policy-probe",
        lambda data: (f"size:{len(data)}",),
        source_files=("policy.py",),
        operator_names=("guard.never-fires",),
        strategy=strategy,
    )

    wrapped = _differential_target(target, Verdict(mutation, Outcome.SURVIVED))

    assert wrapped.strategy is strategy


def test_differential_fuzz_observations_start_from_equivalent_process_state() -> None:
    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import _differential_target
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    calls = 0

    def observe(_data: bytes) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return (f"calls:{calls}",)

    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    wrapped = _differential_target(
        FuzzTarget("stateful", observe), Verdict(mutation, Outcome.SURVIVED)
    )

    assert tuple(wrapped.run(b"input")) == ("differential:return", "calls:1")
    assert calls == 0


def test_differential_fuzz_is_only_bounded_evidence_when_outputs_match(
    tmp_path: Path,
) -> None:
    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import DifferentialFuzzConfig, apply_differential_fuzz
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    verdict = Verdict(mutation, Outcome.SURVIVED)
    report = Report(verdicts=[verdict])
    target = FuzzTarget(
        "policy-probe",
        lambda data: (f"size:{len(data)}",),
        seeds=(b"same",),
        source_files=("policy.py",),
        operator_names=("guard.never-fires",),
    )

    apply_differential_fuzz(
        report,
        DifferentialFuzzConfig(
            tmp_path / "corpus",
            tmp_path / "artifacts",
            seed=11,
            max_cases=3,
            max_seconds=2,
            targets=(target,),
        ),
        workdir=tmp_path,
    )

    assert verdict.outcome is Outcome.SURVIVED
    assert verdict.killers == ()
    assert verdict.fuzz_evidence[0]["cases"] == 3
    assert verdict.fuzz_evidence[0]["comparison"] == "semantic-features-and-exception"
    assert report.as_dict()["differential_fuzz"]["cases_executed"] == 3


def test_differential_fuzz_shares_one_global_case_budget_across_survivors(
    tmp_path: Path,
) -> None:
    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import DifferentialFuzzConfig, apply_differential_fuzz
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()

    def surviving(index: int) -> Verdict:
        mutation = Mutation(
            f"guard.never-fires@policy.py:{index}",
            "guard.never-fires",
            f"guard {index}",
            Site("policy.py", index, "authorize"),
            "policy",
            AttributePatch(subject, "enabled", True),
        )
        return Verdict(mutation, Outcome.SURVIVED)

    report = Report(verdicts=[surviving(4), surviving(8)])
    target = FuzzTarget(
        "policy-probe",
        lambda data: (f"size:{len(data)}",),
        seeds=(b"one", b"two"),
        source_files=("policy.py",),
        operator_names=("guard.never-fires",),
    )

    apply_differential_fuzz(
        report,
        DifferentialFuzzConfig(
            tmp_path / "corpus",
            tmp_path / "artifacts",
            seed=13,
            max_cases=4,
            max_seconds=2,
            targets=(target,),
        ),
        workdir=tmp_path,
    )

    assert report.differential_fuzz is not None
    assert report.differential_fuzz["cases_executed"] == 4
    assert [verdict.fuzz_evidence[0]["cases"] for verdict in report.verdicts] == [2, 2]


def test_differential_fuzz_counts_each_remaining_target_in_its_fair_share(
    tmp_path: Path,
) -> None:
    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import DifferentialFuzzConfig, apply_differential_fuzz
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    verdict = Verdict(mutation, Outcome.SURVIVED)
    report = Report(verdicts=[verdict])
    targets = tuple(
        FuzzTarget(
            f"policy-probe-{index}",
            lambda data: (f"size:{len(data)}",),
            seeds=(b"one",),
            source_files=("policy.py",),
            operator_names=("guard.never-fires",),
        )
        for index in range(2)
    )

    apply_differential_fuzz(
        report,
        DifferentialFuzzConfig(
            tmp_path / "corpus",
            tmp_path / "artifacts",
            seed=17,
            max_cases=4,
            max_seconds=2,
            targets=targets,
        ),
        workdir=tmp_path,
    )

    assert [item["cases"] for item in verdict.fuzz_evidence] == [2, 2]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("signal", "signal"), ("exit", "worker-exit"), ("timeout", "timeout")],
)
def test_differential_fuzz_retains_active_input_and_stderr_when_worker_dies(
    tmp_path: Path,
    failure: str,
    expected: str,
) -> None:
    import os
    import signal
    import time

    from wreath._fuzz import FuzzTarget
    from wreath._mutant.differential import DifferentialFuzzConfig, apply_differential_fuzz
    from wreath._mutant.patch import AttributePatch

    class Subject:
        enabled = False

    subject = Subject()
    mutation = Mutation(
        "guard.never-fires@policy.py:4",
        "guard.never-fires",
        "the guard",
        Site("policy.py", 4, "authorize"),
        "policy",
        AttributePatch(subject, "enabled", True),
    )
    verdict = Verdict(mutation, Outcome.SURVIVED)

    def crash(_data: bytes) -> tuple[str, ...]:
        os.write(2, b"exact worker diagnostic\n")
        if failure == "signal":
            os.kill(os.getpid(), signal.SIGKILL)
        if failure == "exit":
            os._exit(7)
        time.sleep(1)
        return ()

    target = FuzzTarget(
        "crash-probe",
        crash,
        seeds=(b"exact-active-input",),
        source_files=("policy.py",),
        operator_names=("guard.never-fires",),
    )
    report = Report(verdicts=[verdict])

    apply_differential_fuzz(
        report,
        DifferentialFuzzConfig(
            tmp_path / "corpus",
            tmp_path / "artifacts",
            seed=19,
            max_cases=1,
            max_seconds=0.05,
            targets=(target,),
        ),
        workdir=tmp_path,
    )

    evidence = verdict.fuzz_evidence[0]
    finding = evidence["crash_finding"]
    input_path = Path(finding["input_path"])
    assert verdict.outcome is Outcome.SURVIVED
    assert evidence["worker_failure"] == expected
    assert input_path.read_bytes() == b"exact-active-input"
    assert input_path.with_name("diagnostic.log").read_bytes() == b"exact worker diagnostic\n"
    assert finding["deterministic"] is False
    assert report.differential_fuzz is not None
    assert report.differential_fuzz["failures"] == 1
    assert mutant_cli._exit_status(report, fail_on_survivor=False) == 1


def test_live_mutant_completed_at_baseline_seal_keeps_its_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InertPatch:
        def apply(self) -> None:
            pass

        def undo(self) -> None:
            pass

    source = tmp_path / "policy.py"
    source.write_text("value = True\n", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    target = tmp_path / "mutant.json"
    mutation = mutant_runner.Mutation(
        "predicate@policy.py:1",
        "predicate",
        "the policy refuses a request",
        mutant_runner.Site("policy.py", 1, "policy"),
        "policy",
        patch=InertPatch(),
    )
    plan = mutant_runner.Plan(
        mutations=[mutation],
        watch={mutation.identifier: (1,)},
    )
    events = iter(
        ([{"nodeid": "tests/test_policy.py::test_refuses", "hits": [[str(source), 1]]}], [])
    )
    monkeypatch.setattr(
        mutant_runner,
        "_live_trace_events",
        lambda _directory, _positions: next(events),
    )
    running = mutant_runner.RunningMutant(123, target, 0.0, 60.0)
    monkeypatch.setattr(mutant_runner, "start_mutant", lambda *_args, **_kwargs: running)
    polls = 0

    def poll(_running: mutant_runner.RunningMutant):
        nonlocal polls
        polls += 1
        if polls == 1:
            baseline.write_text("{}", encoding="utf-8")
            return None
        return (
            mutant_runner.Outcome.KILLED,
            ("tests/test_policy.py::test_refuses",),
            0.01,
            "",
        )

    monkeypatch.setattr(mutant_runner, "poll_mutant", poll)
    monkeypatch.setattr(mutant_runner.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(mutant_runner.os, "waitpid", lambda pid, _flags: (pid, 0))

    verdicts, probes, completed, cancelled, first_started = mutant_runner._run_live_mutants(
        plan,
        tmp_path,
        tmp_path,
        baseline,
        extra=(),
        workdir=tmp_path,
        timeout=60.0,
        maxfail=1,
        jobs=1,
        origin=0.0,
        emit=lambda *_args, **_kwargs: None,
    )

    assert verdicts[0].outcome == mutant_runner.Outcome.KILLED
    assert probes == 1
    assert completed == 1
    assert cancelled == 0
    assert first_started is not None


def test_native_candidates_put_the_focused_control_first(tmp_path: Path) -> None:
    mutation = mutant_runner.Mutation(
        "guard.never-fires@src/wreath/http_client.py:1519",
        "guard.never-fires",
        "the guarded branch `if bucket is None`",
        mutant_runner.Site("src/wreath/http_client.py", 1519, "HTTPClient._throttle"),
        "wreath.http_client",
    )
    plan = mutant_runner.Plan(watch={mutation.identifier: (1519,)})
    source = str((tmp_path / mutation.site.path).resolve())
    broad = "tests/test_http_client.py::test_request_round_trip"
    focused = "tests/test_http_client_rate_retry.py::test_throttle_disabled_is_noop"
    baseline = mutant_runner.Baseline(
        passed=frozenset((broad, focused)),
        failed=(),
        index={(source, 1519): (broad, focused)},
        per_file={},
        seconds=0.0,
    )

    assert mutant_runner.candidates_for(mutation, plan, baseline, tmp_path) == (
        focused,
        broad,
    )


def test_native_baseline_keeps_test_import_roots_during_execution(tmp_path: Path) -> None:
    support = tmp_path / "baseline_support.py"
    support.write_text("VALUE = 7\n", encoding="utf-8")
    test_file = tmp_path / "test_baseline_import.py"
    test_file.write_text(
        "def test_sibling_import():\n"
        "    from baseline_support import VALUE\n"
        "    assert VALUE == 7\n",
        encoding="utf-8",
    )

    baseline = mutant_runner.run_native_baseline(
        (str(test_file),),
        mutant_runner.Plan(),
        workdir=tmp_path,
    )

    assert baseline.failed == ()
    assert len(baseline.passed) == 1
    assert next(iter(baseline.passed)).endswith("test_baseline_import.py::test_sibling_import")


def test_native_baseline_rechecks_a_monitoring_only_failure_without_tracing(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_monitoring_transparency.py"
    test_file.write_text(
        "import sys\n"
        "def test_monitoring_is_not_a_semantic_requirement():\n"
        "    assert sys.monitoring.get_tool(4) is None\n",
        encoding="utf-8",
    )
    watched = tmp_path / "watched.py"
    watched.write_text("VALUE = 1\n", encoding="utf-8")

    baseline = mutant_runner.run_native_baseline(
        (str(test_file),),
        mutant_runner.Plan(watched={str(watched): {1}}),
        workdir=tmp_path,
    )

    assert baseline.failed == ()
    assert len(baseline.passed) == 1


def test_native_baseline_retry_preserves_order_dependent_failures(tmp_path: Path) -> None:
    test_file = tmp_path / "test_ordered_baseline.py"
    test_file.write_text(
        "STATE = []\n"
        "def test_first_changes_module_state():\n"
        "    STATE.append('changed')\n"
        "def test_second_observes_the_change():\n"
        "    assert STATE == []\n",
        encoding="utf-8",
    )
    watched = tmp_path / "watched.py"
    watched.write_text("VALUE = 1\n", encoding="utf-8")

    baseline = mutant_runner.run_native_baseline(
        (str(test_file),),
        mutant_runner.Plan(watched={str(watched): {1}}),
        workdir=tmp_path,
    )

    assert len(baseline.passed) == 1
    assert len(baseline.failed) == 1
    assert baseline.failed[0].endswith("test_ordered_baseline.py::test_second_observes_the_change")


def test_native_baseline_retry_accepts_a_fresh_parameter_id(tmp_path: Path) -> None:
    test_file = tmp_path / "test_dynamic_parameter.py"
    test_file.write_text(
        "import sys\n"
        "import time\n"
        "import pytest\n"
        "@pytest.mark.parametrize('stamp', [time.time_ns()])\n"
        "def test_monitoring_is_not_a_semantic_requirement(stamp):\n"
        "    assert stamp > 0\n"
        "    assert sys.monitoring.get_tool(4) is None\n",
        encoding="utf-8",
    )
    watched = tmp_path / "watched.py"
    watched.write_text("VALUE = 1\n", encoding="utf-8")

    baseline = mutant_runner.run_native_baseline(
        (str(test_file),),
        mutant_runner.Plan(watched={str(watched): {1}}),
        workdir=tmp_path,
    )

    assert baseline.failed == ()
    assert len(baseline.passed) == 1


def test_native_baseline_retry_accepts_parameter_family_cardinality_drift(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_dynamic_family.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_dynamic(value):\n"
        "    assert value > 0\n"
        "def test_stable():\n"
        "    pass\n",
        encoding="utf-8",
    )
    collection = mutant_runner.prepare_native_collection((str(test_file),))
    try:
        dynamic = next(
            case.node_id for case in collection.cases if "::test_dynamic[" in case.node_id
        )
        stable = next(
            case.node_id
            for case in collection.cases
            if case.node_id.endswith("::test_stable")
        )
    finally:
        mutant_runner.release_native_collection(collection)
    stale_dynamic = f"{dynamic.partition('[')[0]}[stale]"

    assert mutant_runner._retry_native_results((stale_dynamic, stable)) == {
        stale_dynamic: "passed",
        stable: "passed",
    }


def test_native_baseline_retry_streams_the_case_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_ids = (
        "tests/test_one.py::test_one",
        "tests/test_two.py::test_two",
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(kwargs["input"])
        assert len(command) == 3
        assert payload == {
            "files": ["tests/test_one.py", "tests/test_two.py"],
            "node_ids": list(node_ids),
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([[node_id, "passed"] for node_id in node_ids]),
            stderr="",
        )

    monkeypatch.setattr(mutant_runner.subprocess, "run", run)

    assert mutant_runner._retry_native_results(node_ids) == {
        node_id: "passed" for node_id in node_ids
    }


def test_native_baseline_reports_a_retry_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_file = tmp_path / "test_monitoring_failure.py"
    test_file.write_text(
        "import sys\n"
        "def test_monitoring_slot_is_free():\n"
        "    assert sys.monitoring.get_tool(4) is None\n",
        encoding="utf-8",
    )
    watched = tmp_path / "watched.py"
    watched.write_text("VALUE = 1\n", encoding="utf-8")

    def fail(_node_ids: tuple[str, ...]) -> dict[str, str]:
        raise RuntimeError("retry broke")

    monkeypatch.setattr(mutant_runner, "_retry_native_results", fail)

    with pytest.raises(RuntimeError, match="retry broke"):
        mutant_runner.run_native_baseline(
            (str(test_file),),
            mutant_runner.Plan(watched={str(watched): {1}}),
            workdir=tmp_path,
        )


def test_completed_test_blocks_shift_cpu_from_tests_to_mutation() -> None:
    def jobs(completed: int) -> int:
        return mutant_runner._progressive_live_jobs(8, completed, 100, max_live=3)

    assert jobs(0) == 0
    assert jobs(9) == 0
    assert jobs(10) == 1
    assert jobs(50) == 2
    assert jobs(70) == 2
    assert jobs(90) == 3
    assert jobs(100) == 3


def test_sealed_native_pool_does_not_reimport_a_live_registration_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "wreath_pool_probe"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "registry.py").write_text(
        "names = set()\n"
        "def register(name):\n"
        "    if name in names:\n"
        "        raise RuntimeError(f'{name} registered twice')\n"
        "    names.add(name)\n",
        encoding="utf-8",
    )
    test_file = tests / "test_policy.py"
    test_file.write_text(
        "from wreath_pool_probe.registry import register\n"
        "register('policy.guard')\n"
        "def test_policy():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    pool: dict[str, object] = {}

    first = mutant_runner.pooled_native_collection((str(test_file),), pool)
    second = mutant_runner.pooled_native_collection((str(test_file),), pool)

    assert first.index.keys() == second.index.keys()
    assert len(mutant_runner.unique_native_collections(pool.values())) == 1
    for collection in mutant_runner.unique_native_collections(pool.values()):
        mutant_runner.release_native_collection(collection)


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

    completed = _cli(root, "--sample", "1", "--budget", "0.0001", "--format", "json")

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["counts"]["timeout"] == 1
    assert document["rating"]["label"] == "FINISH THE SAMPLE"
    selection = document["selection"]
    assert selection["eligible_candidates"] >= selection["selected_candidates"] == 1
    assert selection["candidate_files"] == selection["selected_files"] == 1
    assert sum(item["eligible"] for item in selection["by_operator"].values()) == selection[
        "eligible_candidates"
    ]
    assert sum(item["selected"] for item in selection["by_operator"].values()) == 1


def test_changed_outside_a_repository_says_so(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
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
        [
            sys.executable,
            "-m",
            "wreath._mutant.cli",
            "--path",
            "shop",
            "--format",
            "json",
            "--quiet",
            "--jobs",
            "2",
            "--activity-file",
            str(activity),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT,
        check=False,
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
    root = tmp_path_factory.mktemp("mutant-exit")
    for relative, body in CRUD_PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)}
    base = [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop", "--quiet", *_NESTED_JOBS]
    completed = subprocess.run(
        base,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_NESTED_TIMEOUT,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "SURVIVED" in completed.stdout or "UNREACHED" in completed.stdout

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
    from wreath._devtools.tasks import _CHECKS

    assert not any("mutant" in name for name, _ in _CHECKS), _CHECKS


@_SAMPLE_GROUP
def test_a_run_reports_both_a_killed_and_a_surviving_control(sample_run: dict) -> None:
    outcomes = {(m["scope"], m["operator"]): m["outcome"] for m in sample_run["mutants"]}
    assert outcomes[("authorize", "guard.never-fires")] == "killed"
    survivors = [
        m
        for m in sample_run["mutants"]
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
        for m in caught
        for killer in m["killers"]
    )


@_SAMPLE_GROUP
def test_the_activity_stream_names_live_and_verified_test_files(sample_run: dict) -> None:
    events = sample_run["_activity"]
    assert events[0]["event"] == "planned"
    assert events[0]["total"] > 0
    started = [event for event in events if event["event"] == "started"]
    killed = [
        event for event in events if event["event"] == "finished" and event["outcome"] == "killed"
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
    first_started = next(index for index, event in enumerate(events) if event["event"] == "started")
    first_finished = next(
        index
        for index, event in enumerate(events[first_started:], start=first_started)
        if event["event"] == "finished"
    )
    started = [
        event for event in events[first_started:first_finished] if event["event"] == "started"
    ]

    assert len(started) == 2
    assert len({event["ordinal"] for event in started}) == 2


@_SAMPLE_GROUP
def test_a_mutant_only_runs_the_tests_that_reach_it(sample_run: dict) -> None:
    counts = {m["scope"]: m["candidates"] for m in sample_run["mutants"] if m["candidates"]}
    assert counts, sample_run["mutants"]
    assert max(counts.values()) < 3, counts


@_SAMPLE_GROUP
def test_a_refusal_no_test_ever_triggers_is_reported_unreached_not_survived(
    sample_run: dict,
) -> None:
    unreached = [
        m
        for m in sample_run["mutants"]
        if m["operator"] == "guard.remove-raise" and m["outcome"] == "unreached"
    ]
    assert unreached, sample_run["mutants"]


# Nothing tested `resolve_scope`, and it had a bug that cost real coverage: it
# walks a dotted `Class.method` path with `getattr`, and `getattr` *invokes* a
# descriptor. So a `classmethod` arrives as a method bound to the class, not as the
# `classmethod` object `_unwrap` was checking for, and every mutation inside every
# classmethod was refused with "is method, not a function".
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
    from types import FunctionType

    import tests.test_mutant as this_module
    from wreath._mutant.patch import resolve_scope

    resolved = resolve_scope(this_module, scope)
    assert isinstance(resolved, FunctionType), (scope, type(resolved).__name__)
    assert resolved.__qualname__.endswith(qualname), (scope, resolved.__qualname__)


def test_resolve_scope_still_refuses_something_that_is_not_callable_at_all() -> None:
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
