"""The per-module walk. One pass over one file's AST, emitting a finding for
every construct the rule catalog names; the verdicts themselves come from
the sibling modules, so a report and its emitted note cannot disagree."""

from __future__ import annotations

import ast
from pathlib import Path

from ..ir import Finding
from ..rules import RULES
from .background import (
    celery_enqueue_rule,
    celery_runner_names,
    celery_task_rule,
    is_celery_task,
)
from .django import DjangoImage
from .imports import _Imports
from .migrations import (
    _FK_ACTION_KWARGS,
    _MIG_ALTER_KWARGS,
    _MIG_DERIVED_OPS,
    _MIG_INDEX_MANUAL_KWARGS,
    _MIG_RENAME_OPS,
    _MIG_REVIEW_OPS,
    _MODELLED_TYPE_ORIGINS,
    _SA_MODELLED_TYPES,
    _SA_TABLE_CONSTRAINTS,
)
from .models import (
    _STR_CONSTRAINTS,
    _config_extra,
    _plain_graphql_dataclass,
    dataclass_needs_kw_only,
    pydantic_field_rule,
    pydantic_projection_rule,
    redundant_literal_validator,
)
from .nodes import _is_false, _is_true, parent_map
from .orm import _base_kind, _index_is_over_columns
from .queries import chain_tail, plain_filter_mappings, query_rule
from .responses import (
    _RESPONSE_CLASSES,
    http_exception_rule,
    response_class_rule,
    status_code_rule,
)
from .routes import _MARKER_RULE, HTTP_METHODS, _is_lifespan, lifespan_names, lifespan_shape
from .settings import (
    _SETTINGS_FIELD_RULE,
    settings_class_rule,
    settings_field_shape,
    settings_required,
)
from .sources import _relative_to

# arrow module-level constructors that are a straight rename onto
# `wreath.temporal`. Anything else on the module (`Arrow(...)`, `interval`,
# `Arrow.range`) needs a look, so it bills separately rather than riding on
# these.
_ARROW_RENAMES = frozenset({"utcnow", "now", "get", "fromtimestamp", "fromdatetime"})

# cachetools stores that map onto a wreath cache.
_CACHE_STORES = frozenset({"TTLCache", "LRUCache", "LFUCache", "Cache", "FIFOCache"})

# Declarative fastapi.security schemes, all of which become an auth backend.
_SECURITY_SCHEMES = frozenset(
    {
        "HTTPBearer",
        "HTTPBasic",
        "HTTPDigest",
        "APIKeyHeader",
        "APIKeyQuery",
        "APIKeyCookie",
        "OAuth2PasswordBearer",
        "OAuth2AuthorizationCodeBearer",
    }
)


def _boto3_service(node: ast.Call) -> str | None:
    """The AWS service named by `boto3.client("s3")` / `.resource("s3")`.

    `None` when the name is not a literal — a service chosen at runtime is not
    one this analyzer can route, and guessing would put an S3 verdict on a call
    that talks to something else.
    """
    argument = node.args[0] if node.args else None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value.lower()
    return None


def _middleware_writes_state(node: ast.ClassDef) -> bool:
    """Whether a middleware class assigns through a ``.state`` attribute."""
    for inner in ast.walk(node):
        targets: list[ast.AST] = []
        if isinstance(inner, ast.Assign):
            targets.extend(inner.targets)
        elif isinstance(inner, ast.AnnAssign):
            targets.append(inner.target)
        for target in targets:
            current = target
            while isinstance(current, ast.Attribute):
                if current.attr == "state":
                    return True
                current = current.value
    return False


class _Analyzer(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        root: Path,
        imports: _Imports,
        index: dict[str, set[str]],
        pk_types: dict[str, str] | None = None,
        orm_columns: dict[str, set[str]] | None = None,
        orm_relations: dict[str, dict[str, str]] | None = None,
        orm_tables: dict[str, str] | None = None,
        orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] | None = None,
        positional_model_calls: set[str] | frozenset[str] = frozenset(),
        django: DjangoImage | None = None,
    ) -> None:
        self.rel = _relative_to(path, root)
        self.django = django or DjangoImage()
        self.imports = imports
        self.index = index
        self.pk_types = pk_types or {}
        # {ORM model name -> its declared attribute names}, tree-wide. A GraphQL
        # type only mirrors a model when its fields *are* the model's columns, and
        # that comparison needs the columns, not just the class name.
        self.orm_columns = orm_columns or {}
        self.orm_relations = orm_relations or {}
        self.orm_tables = orm_tables or {}
        self.orm_unique_constraints = orm_unique_constraints or {}
        self.positional_model_calls = set(positional_model_calls)
        # Names the module hands to an application as `lifespan=`; filled by
        # `visit_Module`, because the `FastAPI(lifespan=...)` call sits below the
        # `def` it names.
        self.lifespan_names: frozenset[str] = frozenset()
        # Names bound to a `Celery(...)` call, so `@relay.task` is read as the
        # task it is rather than as whatever the variable happens to be called.
        # Filled by `visit_Module`: a task may be defined above its runner.
        self.celery_runners: frozenset[str] = frozenset()
        self.findings: list[Finding] = []
        self._loop_depth = 0
        self._once: set[str] = set()
        # `Model.objects` nodes already accounted for by the verb that follows
        # them, so the chain bills as one rewrite rather than two findings.
        self._claimed_objects: set[int] = set()
        # `id(child) -> parent`, so a query head can see the verbs chained after
        # it. Filled by `visit_Module`; empty until then, which makes
        # `query_rule` fall back to its conservative answer rather than guess.
        self._parents: dict[int, ast.AST] = {}

    def visit_Module(self, node: ast.Module) -> None:
        self._parents = parent_map(node)
        self.lifespan_names = lifespan_names(node)
        self.celery_runners = celery_runner_names(node, self.imports)
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call) and item.args):
            if isinstance(call.func, ast.Name):
                self.positional_model_calls.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                self.positional_model_calls.add(call.func.attr)
        self.generic_visit(node)

    def _emit(self, rule_id: str, line: int, extra: str = "") -> None:
        construct, category, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        self.findings.append(Finding(self.rel, line, construct, tag, rule_id, message, category))

    def _once_emit(self, key: str, rule_id: str, line: int) -> None:
        if key not in self._once:
            self._once.add(key)
            self._emit(rule_id, line)

    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            if origin.split(".")[-1] == "as_form":
                self._emit("form.as_form", node.lineno)
            elif origin.startswith("strawberry.") and origin.split(".")[-1] in (
                "type",
                "input",
                "interface",
                "federation",
            ):
                # One finding per GraphQL type, not per field: wreath derives
                # fields from the ORM model, so `strawberry.auto` (the single
                # most common GraphQL token there is) is deleted rather
                # than ported. A finding per field for a no-op would bury the rest.
                rule_id, reason = self._graphql_type_shape(node, origin.split(".")[-1])
                self._emit(rule_id, node.lineno, reason)
        kind = _base_kind(self.imports, node)
        if kind == "middleware":
            lowered = node.name.lower()
            self._emit(
                "mw.exception"
                if "exception" in lowered or "error" in lowered
                else "mw.state"
                if "state" in lowered or _middleware_writes_state(node)
                else "mw.custom",
                node.lineno,
            )
        elif kind == "pydantic":
            self._emit("pydantic.model", node.lineno)
            offenders = dataclass_needs_kw_only(self.imports, node)
            if offenders:
                self._emit(
                    (
                        "pydantic.model_kw_only"
                        if node.name in self.positional_model_calls
                        else "pydantic.model_kw_only_exact"
                    ),
                    node.lineno,
                    "required after a defaulted field: " + ", ".join(offenders),
                )
            self._scan_pydantic_body(node)
        elif kind == "settings":
            # The class verdict follows its fields, so bill the fields first.
            self._scan_settings_body(node)
            rule_id = settings_class_rule(self.imports, node, self.index["settings"])
            self._emit(
                rule_id,
                node.lineno,
                settings_required(node) if rule_id == "settings.class_env" else "",
            )
        elif kind in ("ormar", "sqlmodel"):
            self._emit("orm.model", node.lineno)
            self._scan_orm_body(node, kind)
        # descend for validators, nested calls, etc.
        self.generic_visit(node)

    def _graphql_type_shape(self, node: ast.ClassDef, decorator: str) -> tuple[str, str]:
        """Whether deleting this strawberry class is provably equivalent.

        Wreath derives the object type from the ORM model, so a class that is
        nothing but `strawberry.auto` over a model's columns has no counterpart
        to write — the same argument that makes an `auto` field emit nothing
        makes the enclosing class a deletion. It only holds when the class really
        is that model's full column set, and two things break it:

        * **a subset is a narrowing.** Exposure in wreath is per model, not per
          field, so deleting a type that lists four of eight columns publishes the
          other four. That is a schema widening, and it must not happen quietly.
        * **snake_case is a rename on the wire.** Strawberry camel-cases field
          names by default; wreath emits `column.python_name` verbatim
          (`_graphql/schema.py`). `fleece_kg` is `fleeceKg` in the old
          schema and `fleece_kg` in the new one, so every client sees it.
        """
        if decorator != "type":
            return "graphql.type", f"a strawberry.{decorator} is not a derived object type"
        if _plain_graphql_dataclass(self.imports, node):
            return "graphql.type_dataclass", "register it in GraphQL(dataclasses=[...])"
        fields: list[str] = []
        auto: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return "graphql.type", "the class carries resolvers, so it is not just a mirror"
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.append(stmt.target.id)
                if self.imports.origin(stmt.annotation) == "strawberry.auto":
                    auto.append(stmt.target.id)
        if not fields or len(auto) != len(fields):
            return "graphql.type", (
                "some fields are declared rather than `strawberry.auto`, so they are not "
                "provably the model's columns"
            )
        columns = self.orm_columns.get(node.name)
        if columns is None:
            return "graphql.type", f"no ORM model named {node.name} in the tree to derive from"
        hidden = sorted(columns - set(fields))
        if hidden:
            return "graphql.type", (
                "the derived type would also expose "
                + ", ".join(hidden)
                + " — wreath's exposure is per model, not per field, so deleting this class "
                "widens the public schema"
            )
        renamed = sorted(name for name in fields if "_" in name)
        if renamed:
            return "graphql.type", (
                "strawberry camel-cases field names and wreath does not, so "
                + ", ".join(renamed)
                + " would change on the wire"
            )
        return "graphql.type_mirror", ""

    def _scan_pydantic_body(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
                # pydantic v1 nested config class
                self._emit("pydantic.config_class", stmt.lineno)
                continue
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                self._emit(pydantic_field_rule(self.imports, stmt), stmt.lineno)
            elif isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
            ):
                extra = _config_extra(stmt.value)
                if extra == "ignore":
                    self._emit("pydantic.config_ignore", stmt.lineno)
                elif extra == "forbid":
                    self._emit("pydantic.config_forbid", stmt.lineno)

    def _scan_settings_body(self, node: ast.ClassDef) -> list[str]:
        """Bill each env field by its shape, and report the shapes for the class verdict."""
        shapes: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                shape = settings_field_shape(self.imports, stmt, self.index["settings"])
                shapes.append(shape)
                self._emit(_SETTINGS_FIELD_RULE[shape], stmt.lineno)
        return shapes

    def _scan_orm_body(self, node: ast.ClassDef, kind: str) -> None:
        for stmt in node.body:
            targets = []
            value = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                targets = [stmt.target.id]
                value = stmt.value
            elif isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                value = stmt.value
            if not targets or targets[0] in ("ormar_config", "__tablename__", "model_config"):
                continue
            if not isinstance(value, ast.Call):
                continue
            origin = self.imports.origin(value.func)
            if origin == "ormar.ForeignKey" or self._has_kw(value, "foreign_key"):
                target = value.args[0] if value.args else None
                target_name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else (target.attr if isinstance(target, ast.Attribute) else None)
                )
                typed = target_name in self.pk_types
                self._emit("orm.fk_typed" if typed else "orm.fk", stmt.lineno)
            elif (
                origin.startswith("ormar.")
                or origin.endswith(".ARRAY")
                or (kind == "sqlmodel" and origin.split(".")[-1] == "Field")
            ):
                self._emit("orm.column", stmt.lineno)

    def visit_FunctionDef(self, node) -> None:
        self._handle_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _handle_function(self, node) -> None:
        is_route = False
        for dec in node.decorator_list:
            origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            tail = origin.split(".")[-1]
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                # Bottle, Sanic and Flask all spell their route decorators the
                # way FastAPI does. Counting them scored a monkeypatched Bottle
                # app at 100% auto-translatable off nineteen decorators the tool
                # had never seen the framework of.
                and self.imports.serves_asgi
            ):
                attr = dec.func.attr
                if attr in HTTP_METHODS:
                    is_route = True
                    self._emit("route.method", dec.lineno)
                    self._scan_route_options(dec, node)
                elif attr == "websocket":
                    self._emit("route.websocket", dec.lineno)
                elif attr == "exception_handler":
                    self._emit("exc.handler", dec.lineno)
            if tail in ("field_validator", "model_validator", "validator", "root_validator"):
                self._emit(
                    "pydantic.validator_literal"
                    if redundant_literal_validator(node, self._parents, self.imports)
                    else "pydantic.validator",
                    getattr(dec, "lineno", node.lineno),
                )
            elif tail == "asynccontextmanager":
                self._scan_lifespan(node)
            elif tail == "shared_task" or is_celery_task(
                dec, origin, self.celery_runners, self.imports
            ):
                # Both spellings, deliberately: `@celery_app.task(bind=True)` is a
                # Call and `@celery_app.task` is a bare Attribute. Checking only the
                # Call form missed every undecorated-argument task -- and the
                # emitter (`emit.py`) has always matched on `tail`, so the report
                # under-counted exactly the sites whose ported source carried a TODO.
                self._emit(celery_task_rule(dec, node), getattr(dec, "lineno", node.lineno))
            elif tail == "field" and origin.startswith("strawberry."):
                self._emit("graphql.resolver", getattr(dec, "lineno", node.lineno))
            elif tail == "cached" and origin.startswith("cachetools"):
                self._emit("cache.decorator", getattr(dec, "lineno", node.lineno))
        if is_route:
            self._scan_params(node)

    def _scan_lifespan(self, node) -> None:
        """Bill an `@asynccontextmanager` only if it is the app's lifespan.

        `contextlib.asynccontextmanager` is stdlib and wreath has no opinion
        about it: an advisory-lock or connection helper written with it needs no
        porting at all, and telling its author to "split at the yield into
        on_startup/on_shutdown" would be advice about a function that has no
        startup. So the decorator alone is not the signal — being handed to the
        application as `lifespan=` is.
        """
        if not _is_lifespan(node, self.lifespan_names, self.imports):
            return
        rule_id, reason = lifespan_shape(node)
        self._emit(rule_id, node.lineno, reason)

    def _scan_route_options(self, dec: ast.Call, node) -> None:
        for kw in dec.keywords:
            if kw.arg == "response_model":
                self._emit("route.response_model", dec.lineno)
            elif kw.arg == "status_code":
                self._emit(status_code_rule(self.imports, kw.value, node), dec.lineno)
            elif kw.arg == "include_in_schema" and _is_false(kw.value):
                self._emit("route.include_in_schema", dec.lineno)
            elif kw.arg == "response_class":
                self._emit(response_class_rule(self.imports, kw.value, node), dec.lineno)

    def _scan_params(self, node) -> None:
        args = node.args
        # The tail of `args.args` is exactly as long as `args.defaults`.
        defaulted = args.args[len(args.args) - len(args.defaults) :]
        defaults = dict(zip([a.arg for a in defaulted], args.defaults, strict=True))
        for arg in list(args.args) + list(args.kwonlyargs):
            default = defaults.get(arg.arg)
            ann_origin = self.imports.origin(arg.annotation) if arg.annotation else ""
            if isinstance(default, ast.Call):
                marker = self.imports.origin(default.func).split(".")[-1]
                if marker == "Body":
                    # A plain `Body(...)` is the explicit spelling of what an
                    # annotated model parameter already says. `embed=True` is
                    # not: it wraps the value in a single-key object, and wreath
                    # has no switch for that — the DTO has to gain the wrapper.
                    self._emit(
                        "param.body_embed"
                        if any(k.arg == "embed" and _is_true(k.value) for k in default.keywords)
                        else "param.body",
                        arg.lineno,
                    )
                    continue
                rule_id = _MARKER_RULE.get(marker)
                if rule_id == "param.query" and any(
                    k.arg in _STR_CONSTRAINTS for k in default.keywords
                ):
                    self._emit("param.query_strconstraint", arg.lineno)
                    continue
                if rule_id:
                    self._emit(rule_id, arg.lineno)
                    continue
            if ann_origin.split(".")[-1] == "UploadFile":
                self._emit("param.file", arg.lineno)
            elif arg.annotation is not None and self._annotation_is_model(arg.annotation):
                self._emit("param.body", arg.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr == "include_router":
                self._emit(
                    "route.include_dynamic" if self._loop_depth else "route.include_static",
                    node.lineno,
                )
            elif attr == "add_middleware":
                self._scan_add_middleware(node)
            elif attr in ("delay", "apply_async"):
                self._emit(
                    celery_enqueue_rule(node, inside_async=self._enclosing_is_async(node)),
                    node.lineno,
                )
            elif attr in ("send_json", "receive_json"):
                self._emit("ws.json_method", node.lineno)
            elif attr == "create_task" and self.imports.origin(func.value) == "asyncio":
                if self._created_task_is_joined(node):
                    self._emit("bg.asyncio_joined", node.lineno)
                else:
                    self._once_emit("asyncio_loop", "bg.asyncio_loop", node.lineno)
        if tail == "Process" and origin.startswith("multiprocessing"):
            # One finding per spawn, the way `bg.celery` bills the decorator
            # rather than every call. `.start()`/`.join()`/`.is_alive()` around it
            # are the same worker, and billing them would report one port as four.
            self._emit("bg.multiprocessing", node.lineno)
        if tail == "FastAPI":
            self._emit("route.app", node.lineno)
        elif tail == "APIRouter":
            self._emit("route.router", node.lineno)
            self._scan_router_deps(node)
        elif tail == "HTTPException":
            self._scan_http_exception(node)
        elif tail == "Depends":
            self._emit("depends.use", node.lineno)
        elif tail in ("GraphQL", "GraphQLRouter", "GraphQLApp"):
            self._emit("graphql.mount", node.lineno)
        elif tail == "Security":
            self._emit("auth.security", node.lineno)
        elif tail in _SECURITY_SCHEMES and self._from_framework(origin):
            self._emit("auth.security_scheme", node.lineno)
        elif tail == "jsonable_encoder":
            self._emit("resp.jsonable", node.lineno)
        elif tail in _RESPONSE_CLASSES and self._from_framework(origin):
            self._emit("resp.class", node.lineno)
        elif tail == "TestClient" and self._from_framework(origin):
            parent = self._parents.get(id(node))
            direct_assignment = (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
            )
            ancestor = parent
            while ancestor is not None and not isinstance(
                ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                ancestor = self._parents.get(id(ancestor))
            self._emit(
                "test.client_local"
                if direct_assignment and ancestor is not None
                else "test.client",
                node.lineno,
            )
        elif tail in _CACHE_STORES and origin.startswith("cachetools"):
            self._emit("cache.store", node.lineno)
        elif origin.startswith("arrow."):
            # Split by constructor rather than billing the module once: the
            # clock and parsing calls are a rename, and lumping them in with the
            # rest would keep reporting shipped work as outstanding.
            if tail in _ARROW_RENAMES:
                self._once_emit("arrow", "time.arrow", node.lineno)
            else:
                self._once_emit("arrow_other", "time.arrow_other", node.lineno)
        elif origin.startswith("httpx."):
            self._once_emit("httpx", "ext.httpx", node.lineno)
        elif origin.startswith(("pandas", "numpy")):
            self._once_emit("pandas", "ext.pandas", node.lineno)
        elif origin.startswith("alembic.op."):
            self._scan_migration_op(node, tail)
        elif origin.startswith("boto3") or origin.startswith("aioboto3"):
            # `boto3.client("s3")` names its service in the first argument, and
            # that argument is what decides the verdict: S3 has a wreath target
            # now, every other service still has none. A module that talks to
            # both bills both, so the once-key carries the service.
            service = _boto3_service(node)
            if service == "s3":
                self._once_emit("boto3-s3", "ext.boto3_s3", node.lineno)
            elif service in {"scheduler", "events"}:
                self._once_emit("boto3-scheduler", "ext.boto3_scheduler", node.lineno)
            elif service in {"cloudwatch", "logs"}:
                self._once_emit("boto3-observability", "ext.boto3_observability", node.lineno)
            elif service in {"cognito-idp", "cognito-identity"}:
                self._once_emit("boto3-identity", "ext.boto3_identity", node.lineno)
            else:
                self._once_emit("boto3", "ext.boto3", node.lineno)
        elif origin in ("hmac.new", "hmac.compare_digest"):
            # A digest compared against a header is a signature check. One
            # finding per module: `hmac.new(...)` and the `compare_digest` that
            # reads it are two halves of one verify, not two ports.
            self._once_emit("hmac", "webhook.hmac", node.lineno)
        elif "dlock" in origin:
            self._once_emit("dlock", "lock.dlock", node.lineno)
        elif tail in ("decode", "get_unverified_header") and "jwt" in origin.lower():
            claims = {keyword.arg for keyword in node.keywords}
            self._emit(
                "auth.oidc_manual" if {"issuer", "audience"} <= claims else "auth.jwt",
                node.lineno,
            )
        elif tail == "OAuth2Session" or "authlib" in origin:
            self._emit("auth.oauth", node.lineno)
        elif origin.startswith("aiometer"):
            self._once_emit("aiometer", "ext.aiometer", node.lineno)
        elif origin.startswith("gql") and tail in {"Client", "gql"}:
            self._once_emit("gql", "ext.gql", node.lineno)
        elif origin.startswith("s3path"):
            self._once_emit("s3path", "ext.s3path", node.lineno)
        # migration MANUAL cast
        if any(kw.arg == "postgresql_using" for kw in node.keywords):
            self._emit("mig.manual", node.lineno)
        self.generic_visit(node)

    def _enclosing_is_async(self, node: ast.AST) -> bool:
        """Whether the function this node sits in can hold an `await`."""
        ancestor = self._parents.get(id(node))
        while ancestor is not None and not isinstance(
            ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            ancestor = self._parents.get(id(ancestor))
        return isinstance(ancestor, ast.AsyncFunctionDef)

    def _created_task_is_joined(self, node: ast.Call) -> bool:
        """Whether this task's exception is observed in its defining function."""
        parent = self._parents.get(id(node))
        if isinstance(parent, ast.Await):
            return True
        while isinstance(
            parent,
            (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.List, ast.Tuple),
        ):
            parent = self._parents.get(id(parent))
        tracked: set[str] = set()
        if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            tracked.add(parent.target.id)
        elif (
            isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
        ):
            tracked.add(parent.targets[0].id)
        elif (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr == "append"
            and isinstance(parent.func.value, ast.Name)
            and len(parent.args) == 1
        ):
            # ``parent`` came from this node's parent map. With one positional
            # argument, that argument can only be ``node``; checking identity
            # again adds no information and therefore no testable branch.
            tracked.add(parent.func.value.id)
        else:
            return False
        ancestor: ast.AST | None = parent
        while ancestor is not None and not isinstance(
            ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            ancestor = self._parents.get(id(ancestor))
        if not isinstance(ancestor, ast.AsyncFunctionDef):
            return False
        # Follow the exact local accumulation shape without inventing a task
        # compatibility layer: ``task = create_task(...); tasks.append(task)``
        # and ``tasks.append(create_task(...))`` are both joined by a later
        # ``gather(*tasks)``. Aliases and helper calls remain conservative.
        for candidate in ast.walk(ancestor):
            if not (
                isinstance(candidate, ast.Call)
                and candidate.lineno >= node.lineno
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "append"
                and isinstance(candidate.func.value, ast.Name)
                and len(candidate.args) == 1
                and isinstance(candidate.args[0], ast.Name)
                and candidate.args[0].id in tracked
            ):
                continue
            tracked.add(candidate.func.value.id)
        for candidate in ast.walk(ancestor):
            if not isinstance(candidate, ast.Await) or candidate.lineno < node.lineno:
                continue
            if isinstance(candidate.value, ast.Name) and candidate.value.id in tracked:
                return True
            if isinstance(candidate.value, ast.Call):
                awaited = candidate.value
                if self.imports.origin(awaited.func) in ("asyncio.gather", "asyncio.wait") and any(
                    (isinstance(argument, ast.Name) and argument.id in tracked)
                    or (
                        isinstance(argument, ast.Starred)
                        and isinstance(argument.value, ast.Name)
                        and argument.value.id in tracked
                    )
                    for argument in awaited.args
                ):
                    return True
        return False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        # `Model.objects.<verb>` — the verb is what names the rewrite, so claim
        # the `.objects` underneath it. Visiting is top-down, so this node is
        # always reached before the one it claims.
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "objects"
            and node.attr == "__class__"
        ):
            self._claimed_objects.add(id(value))
            self._emit("orm.manager_patch", value.lineno)
        elif isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            model = ast.unparse(value.value)
            if not self.django.objects_is_every_row(model, reads_django=self.imports.reads_django):
                # Same spelling, different manager -- and whose manager it is
                # is a property of the model, resolved tree-wide in
                # `analyzer/django.py`, not of this module's import list.
                self._emit("foreign.django.query", value.lineno)
                self.generic_visit(node)
                return
            self._emit(
                query_rule(
                    node.attr,
                    call if isinstance(call, ast.Call) else None,
                    chain_tail(node, self._parents),
                    model=model,
                    relations=self.orm_relations,
                    columns=self.orm_columns,
                    tables=self.orm_tables,
                    unique_constraints=self.orm_unique_constraints,
                    plain_mappings=plain_filter_mappings(
                        call if isinstance(call, ast.Call) else None, self._parents
                    ),
                ),
                value.lineno,
            )
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            # `.objects` used as a value rather than called through: still a
            # query surface, but there is no verb to be specific about.
            self._emit(
                "orm.manager_value"
                if self.django.objects_is_every_row(
                    ast.unparse(value), reads_django=self.imports.reads_django
                )
                else "foreign.django.query",
                node.lineno,
            )
        elif node.attr == "get_pydantic":
            self._emit(pydantic_projection_rule(node, self._parents), node.lineno)
        elif node.attr == "dependency_overrides":
            parent = self._parents.get(id(node))
            key = parent.slice if isinstance(parent, ast.Subscript) else None
            key_name = (
                key.id
                if isinstance(key, ast.Name)
                else key.attr
                if isinstance(key, ast.Attribute)
                else ""
            ).lower()
            is_auth = any(word in key_name for word in ("auth", "user", "principal", "ranger"))
            # No subscript is no override: `app.dependency_overrides = {}` and
            # `.clear()` reset the whole map, which is neither an identity nor an
            # adapter. Reading the key alone put adapter advice on those lines
            # and left the generic rule -- written for exactly this -- dead.
            rule_id = (
                "test.dependency_override"
                if key is None
                else "test.dependency_override_auth"
                if is_auth
                else "test.dependency_override_adapter"
            )
            # Once per *kind*: repeating one override in ten checks is one port,
            # but a suite that resets the map and then swaps the auth dependency
            # has two, and a single per-module slot reported only the first.
            self._once_emit(rule_id, rule_id, node.lineno)
        elif node.attr.startswith("HTTP_") and self.imports.origin(value) in (
            "fastapi.status",
            "starlette.status",
        ):
            self._emit("resp.status_const", node.lineno)
        self.generic_visit(node)

    def _from_framework(self, origin: str) -> bool:
        """Whether a resolved name came from FastAPI/Starlette rather than a local.

        Applications routinely have a class sharing a name with a framework one
        — their own `TestClient` wrapper, for instance — and reporting those
        would be noise a porter has to dismiss by hand.
        """
        return origin.startswith(("fastapi", "starlette"))

    def _scan_migration_op(self, node: ast.Call, tail: str) -> None:
        """Sort one Alembic operation into derivable, manual, or data-rewriting.

        Most revisions in a mature app are ordinary DDL that
        `wreath migrations generate` produces from the model change, and for
        those there is nothing to hand-write at all. The ones that are not are
        the ones worth a porter's attention: raw SQL and a row rewrite, which no
        generator can derive, and a **rename**, which is the operation an image
        differ reads as a drop plus a create — the ordinary-looking op that
        would silently move no data.
        """
        if tail == "execute":
            self._emit("mig.raw_sql", node.lineno)
        elif tail == "get_bind":
            self._emit("mig.data", node.lineno)
        elif tail in _MIG_RENAME_OPS:
            self._emit("mig.rename", node.lineno)
        elif tail in _MIG_REVIEW_OPS:
            self._emit("mig.schema_op", node.lineno)
        elif tail in _MIG_DERIVED_OPS:
            # `postgresql_using` already earns the stronger MANUAL verdict below;
            # emitting both would double-count one operation.
            if not self._has_kw(node, "postgresql_using"):
                self._emit(self._migration_verdict(node, tail), node.lineno)

    def _migration_verdict(self, node: ast.Call, tail: str) -> str:
        """The rule a derivable-*shaped* operation actually earns.

        The verb narrows the candidates; the arguments decide. An index is
        derivable only while it stays btree-over-columns, an `alter_column`
        only while every kwarg it sets is part of the column signature detection
        reads, and a table only while every column type has a wreath PgType.
        """
        if tail in ("create_index", "drop_index"):
            if any(kw.arg in _MIG_INDEX_MANUAL_KWARGS for kw in node.keywords):
                return "mig.index_manual"
            if tail == "create_index" and not _index_is_over_columns(node):
                return "mig.index_manual"
            return "mig.derived"
        if tail == "alter_column":
            if self._has_kw(node, "new_column_name"):
                return "mig.rename"
            if any(kw.arg not in _MIG_ALTER_KWARGS for kw in node.keywords):
                return "mig.schema_op"
            for kw in node.keywords:
                if kw.arg in ("type_", "existing_type") and not self._sa_type_is_modelled(kw.value):
                    return "mig.unmodelled_type"
            return "mig.derived"
        if tail in ("create_table", "add_column"):
            return self._table_body_verdict(node)
        return "mig.derived"

    def _table_body_verdict(self, node: ast.Call) -> str:
        """Whether every column/constraint in a `create_table`/`add_column` is modelled."""
        verdict = "mig.derived"
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue  # the table name
            if not isinstance(argument, ast.Call):
                return "mig.schema_op"  # a splat or a name: nothing to read
            kind = self.imports.origin(argument.func).split(".")[-1]
            if kind not in _SA_TABLE_CONSTRAINTS:
                return "mig.schema_op"
            if any(kw.arg in _FK_ACTION_KWARGS for kw in argument.keywords):
                return "mig.schema_op"
            if kind != "Column":
                continue
            inner = self._column_verdict(argument)
            if inner == "mig.schema_op":
                return inner
            if inner != "mig.derived":
                verdict = inner
        return verdict

    def _column_verdict(self, call: ast.Call) -> str:
        """One `sa.Column(...)`: modelled type, no referential action of its own."""
        for argument in call.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue  # the column name
            if isinstance(argument, ast.Call):
                kind = self.imports.origin(argument.func).split(".")[-1]
                if kind == "ForeignKey":
                    if any(kw.arg in _FK_ACTION_KWARGS for kw in argument.keywords):
                        return "mig.schema_op"
                    continue
            if not self._sa_type_is_modelled(argument):
                return "mig.unmodelled_type"
        return "mig.derived"

    def _sa_type_is_modelled(self, node: ast.AST | None) -> bool:
        """Whether `sa.String(length=80)` / `postgresql.JSONB()` has a wreath PgType."""
        if isinstance(node, ast.Call):
            node = node.func
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return False
        origin = self.imports.origin(node)
        return origin in _MODELLED_TYPE_ORIGINS or origin.split(".")[-1] in _SA_MODELLED_TYPES

    def _scan_add_middleware(self, node: ast.Call) -> None:
        first = node.args[0] if node.args else None
        origin = self.imports.origin(first) if first else ""
        tail = origin.split(".")[-1]
        if tail == "CORSMiddleware":
            self._emit("mw.cors", node.lineno)
        elif tail == "TrustedHostMiddleware":
            allowed = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "allowed_hosts"),
                None,
            )
            wildcard = (
                isinstance(allowed, (ast.List, ast.Tuple))
                and len(allowed.elts) == 1
                and isinstance(allowed.elts[0], ast.Constant)
                and allowed.elts[0].value == "*"
            )
            self._emit(
                "mw.trustedhost_noop" if wildcard else "mw.trustedhost",
                node.lineno,
            )
        else:
            self._emit("mw.custom", node.lineno)

    def _scan_router_deps(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "dependencies" and not isinstance(kw.value, (ast.List, ast.Tuple)):
                self._emit("depends.router_call", node.lineno)

    def _scan_http_exception(self, node: ast.Call) -> None:
        self._emit(http_exception_rule(self.imports, node), node.lineno)

    def _annotation_is_model(self, ann: ast.AST) -> bool:
        name = ""
        if isinstance(ann, ast.Name):
            name = ann.id
        elif isinstance(ann, ast.Attribute):
            name = ann.attr
        return name in self.index["pydantic"] or name in self.index["orm"]

    def _has_kw(self, call: ast.Call, name: str) -> bool:
        return any(kw.arg == name for kw in call.keywords)
