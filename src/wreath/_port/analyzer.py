"""Static (never-import-the-target) FastAPI/Pydantic/ormar/SQLModel analyzer.

Design 07's load-bearing constraint: the source cannot be imported (private deps,
import-time side effects), so this walks ``ast`` only. Two passes: (1) index every
module's classes by framework base across the whole tree so body-params and query
targets resolve cross-module; (2) classify constructs into findings.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

from .ir import Finding, Report
from .rules import RULES

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_MARKER_RULE = {
    "Query": "param.query",
    "Path": "param.path",
    "Header": "param.header",
    "Cookie": "param.cookie",
    "Form": "param.form",
    "File": "param.file",
}
_STR_CONSTRAINTS = frozenset({"min_length", "max_length", "regex", "pattern"})

# ormar PK column type -> wreath PgType name, for FK type inference from the referenced model.
_PK_PGTYPE = {
    "UUID": "Uuid", "Integer": "Int64", "BigInteger": "Int64",
    "SmallInteger": "Int16", "String": "Varchar", "Text": "Text",
}


def _is_true_c(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def module_pk_types(tree: ast.Module, imports: _Imports) -> dict[str, str]:
    """{ORM model name -> wreath PgType of its primary key}, resolved within one module.

    Used to give a ForeignKey its real column type instead of a guess. Cross-module
    references (target not in this module) resolve to nothing -> the FK stays needs-review.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _base_kind(imports, node) in ("ormar", "sqlmodel"):
            for stmt in node.body:
                value = stmt.value if isinstance(stmt, (ast.AnnAssign, ast.Assign)) else None
                if not isinstance(value, ast.Call):
                    continue
                if any(kw.arg == "primary_key" and _is_true_c(kw.value) for kw in value.keywords):
                    pg = _PK_PGTYPE.get(imports.origin(value.func).split(".")[-1])
                    if pg:
                        out[node.name] = pg
                    break
    return out


def _iter_py(root: Path):
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


class _Imports:
    """Resolves local names to their dotted framework origin (honors ``as``)."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.has_star = False

    def visit(self, tree: ast.AST) -> _Imports:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.names[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        self.has_star = True
                        continue
                    qualified = f"{mod}.{alias.name}" if mod else alias.name
                    self.names[alias.asname or alias.name] = qualified
        return self

    def origin(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value)
            return f"{base}.{node.attr}"
        return ""


def _base_kind(imports: _Imports, cls: ast.ClassDef) -> str | None:
    """Which framework model kind (if any) this class subclasses."""
    for base in cls.bases:
        origin = imports.origin(base)
        if origin == "pydantic.BaseModel":
            return "pydantic"
        # pydantic-settings (v2) and the legacy pydantic v1 BaseSettings both appear
        # in real apps — recognize either.
        if origin in ("pydantic_settings.BaseSettings", "pydantic.BaseSettings"):
            return "settings"
        if origin == "ormar.Model":
            return "ormar"
        if origin == "sqlmodel.SQLModel":
            return "sqlmodel"
    return None


def _index_tree(files: list[Path]) -> dict[str, set[str]]:
    """Pass 1: collect model/settings class *names* across the whole tree."""
    index: dict[str, set[str]] = {"pydantic": set(), "settings": set(), "orm": set()}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        imports = _Imports().visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                kind = _base_kind(imports, node)
                if kind == "pydantic":
                    index["pydantic"].add(node.name)
                elif kind == "settings":
                    index["settings"].add(node.name)
                elif kind in ("ormar", "sqlmodel"):
                    index["orm"].add(node.name)
    return index


class _Analyzer(ast.NodeVisitor):
    def __init__(self, path: Path, root: Path, imports: _Imports, index: dict[str, set[str]],
                 pk_types: dict[str, str] | None = None) -> None:
        under_root = root in path.parents or root == path
        self.rel = str(path.relative_to(root)) if under_root else str(path)
        self.imports = imports
        self.index = index
        self.pk_types = pk_types or {}
        self.findings: list[Finding] = []
        self._loop_depth = 0
        self._once: set[str] = set()

    # -- emit -----------------------------------------------------------------
    def _emit(self, rule_id: str, line: int, extra: str = "") -> None:
        construct, category, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        self.findings.append(Finding(self.rel, line, construct, tag, rule_id, message, category))

    def _once_emit(self, key: str, rule_id: str, line: int) -> None:
        if key not in self._once:
            self._once.add(key)
            self._emit(rule_id, line)

    # -- loops (dynamic include_router) ---------------------------------------
    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    # -- classes (models / settings) ------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            if self.imports.origin(dec).split(".")[-1] == "as_form":
                self._emit("form.as_form", node.lineno)
        kind = _base_kind(self.imports, node)
        if kind == "pydantic":
            self._emit("pydantic.model", node.lineno)
            self._scan_pydantic_body(node)
        elif kind == "settings":
            self._emit("settings.class", node.lineno)
            self._scan_settings_body(node)
        elif kind in ("ormar", "sqlmodel"):
            self._emit("orm.model", node.lineno)
            self._scan_orm_body(node, kind)
        # descend for validators, nested calls, etc.
        self.generic_visit(node)

    def _scan_pydantic_body(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
                # pydantic v1 nested config class
                self._emit("pydantic.config_class", stmt.lineno)
                continue
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                if (self._is_field_with_constraints(stmt.value)
                        or self._annotation_is_constrained(stmt.annotation)):
                    self._emit("pydantic.field_constraint", stmt.lineno)
                else:
                    self._emit("pydantic.field", stmt.lineno)
            elif isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
            ):
                extra = self._config_extra(stmt.value)
                if extra == "ignore":
                    self._emit("pydantic.config_ignore", stmt.lineno)
                elif extra == "forbid":
                    self._emit("pydantic.config_forbid", stmt.lineno)

    def _scan_settings_body(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                ann_origin = self.imports.origin(stmt.annotation)
                if (ann_origin.split(".")[-1] in self.index["settings"]
                        or self._value_is_settings(stmt.value)):
                    self._emit("settings.nested", stmt.lineno)
                else:
                    self._emit("settings.field", stmt.lineno)

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
                target_name = target.id if isinstance(target, ast.Name) else (
                    target.attr if isinstance(target, ast.Attribute) else None)
                typed = target_name in self.pk_types
                self._emit("orm.fk_typed" if typed else "orm.fk", stmt.lineno)
            elif origin.startswith("ormar.") or origin.endswith(".ARRAY") or (
                kind == "sqlmodel" and origin.split(".")[-1] == "Field"
            ):
                self._emit("orm.column", stmt.lineno)

    # -- functions (routes / deps / validators / lifespan) --------------------
    def visit_FunctionDef(self, node) -> None:
        self._handle_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _handle_function(self, node) -> None:
        is_route = False
        for dec in node.decorator_list:
            origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            tail = origin.split(".")[-1]
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                attr = dec.func.attr
                if attr in HTTP_METHODS:
                    is_route = True
                    self._emit("route.method", dec.lineno)
                    self._scan_route_options(dec, node)
                elif attr == "websocket":
                    self._emit("route.websocket", dec.lineno)
                elif attr == "exception_handler":
                    self._emit("exc.handler", dec.lineno)
                elif attr in ("task",) and "celery" in origin.lower():
                    self._emit("bg.celery", dec.lineno)
            if tail in ("field_validator", "model_validator", "validator", "root_validator"):
                self._emit("pydantic.validator", getattr(dec, "lineno", node.lineno))
            elif tail == "asynccontextmanager":
                self._emit("lifespan.ctx", node.lineno)
            elif tail == "shared_task":
                self._emit("bg.celery", getattr(dec, "lineno", node.lineno))
        if is_route:
            self._scan_params(node)

    def _scan_route_options(self, dec: ast.Call, node) -> None:
        for kw in dec.keywords:
            if kw.arg == "response_model":
                self._emit("route.response_model", dec.lineno)
            elif kw.arg == "status_code":
                # Translatable only when the body is a single `return <expr>` (the safe
                # subset — wrapping every return risks wrong serialization/multi-path).
                literal = isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int)
                single_return = (
                    len(node.body) == 1
                    and isinstance(node.body[0], ast.Return)
                    and node.body[0].value is not None
                )
                rewritable = literal and single_return
                self._emit("route.status_code_return" if rewritable else "route.status_code",
                           dec.lineno)
            elif kw.arg == "include_in_schema" and _is_false(kw.value):
                self._emit("route.include_in_schema", dec.lineno)

    def _scan_params(self, node) -> None:
        args = node.args
        # The tail of `args.args` is exactly as long as `args.defaults`.
        defaulted = args.args[len(args.args) - len(args.defaults):]
        defaults = dict(zip([a.arg for a in defaulted], args.defaults, strict=True))
        for arg in list(args.args) + list(args.kwonlyargs):
            default = defaults.get(arg.arg)
            ann_origin = self.imports.origin(arg.annotation) if arg.annotation else ""
            if isinstance(default, ast.Call):
                marker = self.imports.origin(default.func).split(".")[-1]
                rule_id = _MARKER_RULE.get(marker)
                if rule_id == "param.query" and any(k.arg in _STR_CONSTRAINTS
                                                    for k in default.keywords):
                    self._emit("param.query_strconstraint", arg.lineno)
                    continue
                if rule_id:
                    self._emit(rule_id, arg.lineno)
                    continue
            if ann_origin.split(".")[-1] == "UploadFile":
                self._emit("param.file", arg.lineno)
            elif arg.annotation is not None and self._annotation_is_model(arg.annotation):
                self._emit("param.body", arg.lineno)

    # -- calls / attributes (queries, deps, middleware, infra) ----------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr == "include_router":
                self._emit("route.include_dynamic" if self._loop_depth else "route.include_static",
                           node.lineno)
            elif attr == "add_middleware":
                self._scan_add_middleware(node)
            elif attr in ("delay", "apply_async"):
                self._emit("bg.celery", node.lineno)
            elif attr in ("send_json", "receive_json"):
                self._emit("ws.json_method", node.lineno)
            elif attr == "create_task" and self.imports.origin(func.value) == "asyncio":
                self._once_emit("asyncio_loop", "bg.asyncio_loop", node.lineno)
        if tail == "FastAPI":
            self._emit("route.app", node.lineno)
        elif tail == "APIRouter":
            self._emit("route.router", node.lineno)
            self._scan_router_deps(node)
        elif tail == "HTTPException":
            self._scan_http_exception(node)
        elif tail == "Depends":
            self._emit("depends.use", node.lineno)
        elif tail == "GraphQL":
            self._emit("graphql.mount", node.lineno)
        elif origin.startswith("boto3") or origin.startswith("aioboto3"):
            self._once_emit("boto3", "ext.boto3", node.lineno)
        elif "dlock" in origin:
            self._once_emit("dlock", "lock.dlock", node.lineno)
        elif tail in ("decode", "get_unverified_header") and "jwt" in origin.lower():
            self._emit("auth.jwt", node.lineno)
        elif tail == "OAuth2Session" or "authlib" in origin:
            self._emit("auth.oauth", node.lineno)
        elif origin.startswith("aiometer"):
            self._once_emit("aiometer", "ext.aiometer", node.lineno)
        elif origin.startswith("gql"):
            self._once_emit("gql", "ext.gql", node.lineno)
        elif origin.startswith("s3path"):
            self._once_emit("s3path", "ext.s3path", node.lineno)
        # migration MANUAL cast
        if any(kw.arg == "postgresql_using" for kw in node.keywords):
            self._emit("mig.manual", node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "objects":
            self._emit("orm.query", node.lineno)
        elif node.attr == "get_pydantic":
            self._emit("pydantic.get_pydantic", node.lineno)
        self.generic_visit(node)

    def _scan_add_middleware(self, node: ast.Call) -> None:
        first = node.args[0] if node.args else None
        origin = self.imports.origin(first) if first else ""
        tail = origin.split(".")[-1]
        if tail == "CORSMiddleware":
            self._emit("mw.cors", node.lineno)
        elif tail == "TrustedHostMiddleware":
            self._emit("mw.trustedhost", node.lineno)
        else:
            self._emit("mw.custom", node.lineno)

    def _scan_router_deps(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "dependencies" and not isinstance(kw.value, (ast.List, ast.Tuple)):
                self._emit("depends.router_call", node.lineno)

    def _scan_http_exception(self, node: ast.Call) -> None:
        status = None
        for kw in node.keywords:
            if kw.arg == "status_code":
                status = kw.value
        if status is None and node.args:
            status = node.args[0]
        if isinstance(status, ast.Constant) and isinstance(status.value, int):
            self._emit("exc.http_literal", node.lineno)
        else:
            self._emit("exc.http_variable", node.lineno)

    # -- small predicates -----------------------------------------------------
    def _annotation_is_model(self, ann: ast.AST) -> bool:
        name = ""
        if isinstance(ann, ast.Name):
            name = ann.id
        elif isinstance(ann, ast.Attribute):
            name = ann.attr
        return name in self.index["pydantic"] or name in self.index["orm"]

    def _annotation_is_constrained(self, ann: ast.AST | None) -> bool:
        """pydantic v1 constrained-type annotation (confloat/conint/constr/...)."""
        if isinstance(ann, ast.Call):
            return self.imports.origin(ann.func).split(".")[-1] in (
                "confloat", "conint", "constr", "condecimal", "conbytes", "conlist",
                "conset", "condate",
            )
        return False

    def _is_field_with_constraints(self, value: ast.AST | None) -> bool:
        if (isinstance(value, ast.Call)
                and self.imports.origin(value.func).split(".")[-1] == "Field"):
            return any(
                k.arg in _STR_CONSTRAINTS or k.arg in ("ge", "le", "gt", "lt", "multiple_of")
                for k in value.keywords
            )
        return False

    def _config_extra(self, value: ast.AST | None) -> str | None:
        if isinstance(value, ast.Call):
            for kw in value.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Constant):
                    # A Constant holds any literal; only a string is an `extra=`
                    # setting. Callers compare against "forbid"/"ignore", so a
                    # non-string was already as good as absent.
                    extra = kw.value.value
                    return extra if isinstance(extra, str) else None
        return None

    def _value_is_settings(self, value: ast.AST | None) -> bool:
        if isinstance(value, ast.Call):
            return self.imports.origin(value.func).split(".")[-1] in self.index["settings"]
        return False

    def _has_kw(self, call: ast.Call, name: str) -> bool:
        return any(kw.arg == name for kw in call.keywords)


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def analyze(root) -> Report:
    """Analyze a single app root (directory or file) and return its Report."""
    root = Path(root)
    files = list(_iter_py(root))
    index = _index_tree(files)
    findings: list[Finding] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        imports = _Imports().visit(tree)
        analyzer = _Analyzer(path, root, imports, index, module_pk_types(tree, imports))
        if imports.has_star:
            analyzer._emit("resolve.star_import", 1)
        analyzer.visit(tree)
        findings.extend(analyzer.findings)
    return Report(findings, roots=[str(root)])


def analyze_all(roots) -> Report:
    """Analyze several app roots (a glob of apps, design 07 §5) into one Report."""
    return Report.merge([analyze(r) for r in roots])
