"""A multi-project migration inventory built without importing any target.

``analyze_all`` answers how much source can be translated.  A multi-project needs
the other axis as well: which project owns each route, which dependencies and
access guards cross that boundary, and whether its declared Python range can
even reach the interpreter Wreath targets.  This module keeps that inventory
separate from the codemod report so merging two roots never loses provenance.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analyzer import _Imports, _iter_py, _parse_file, analyze
from .ir import Report

_ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
_ACCESS_DECORATORS = frozenset(
    {"public", "identify", "authenticated", "roles", "permissions", "authorize", "authorise"}
)
_GUARD_WORDS = ("auth", "guard", "permission", "role", "policy", "access")
_ACTION_WORDS = ("action", "operation", "permission", "verb")
_RESOURCE_WORDS = ("resource", "subject", "object")
_CONDITION_WORDS = ("condition", "when", "context")
_ACTION_KEYS = frozenset(_ACTION_WORDS)
_RESOURCE_KEYS = frozenset(_RESOURCE_WORDS)
_CONDITION_KEYS = frozenset(_CONDITION_WORDS)
_DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*")
_VERSION_CLAUSE = re.compile(r"^(<=|>=|==|!=|~=|<|>)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")

# A dependency finding is useful only when the replacement is a capability
# Wreath really ships.  Keep this deliberately smaller than a package synonym
# table: an unknown package is reported as unknown, never guessed from its name.
_REPLACEMENTS = {
    "fastapi": "wreath",
    "starlette": "wreath",
    "pydantic": "dataclasses + wreath.binding",
    "pydantic-settings": "wreath.config.Environment",
    "ormar": "wreath.orm",
    "sqlalchemy": "wreath.orm / wreath.postgres",
    "sqlmodel": "wreath.orm",
    "alembic": "wreath.migrations",
    "strawberry-graphql": "wreath.graphql",
    "httpx": "wreath.http_client",
    "cachetools": "wreath.cache / wreath.response_cache",
    "celery": "wreath.jobs",
    "authlib": "wreath.auth",
}

_BASELINE_RETIRED_RULES = frozenset(
    {
        "mig.raw_sql",
        "mig.data",
        "mig.rename",
        "mig.manual",
        "mig.index_manual",
        "mig.schema_op",
        "mig.unmodelled_type",
    }
)


def _tail(node: ast.AST, imports: _Imports) -> str:
    return imports.origin(node).split(".")[-1]


def _literal_text(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _expression(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except ValueError:
        return None


def _keyword(call: ast.Call, names: frozenset[str]) -> ast.AST | None:
    for item in call.keywords:
        if item.arg in names:
            return item.value
    return None


def _enum_tail(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower()
    return None


def _resource_type(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if _literal_text(key) in {"type", "kind", "resource_type"}:
                return _enum_tail(value)
    if isinstance(node, ast.Call):
        for keyword in node.keywords:
            if keyword.arg in {"type", "kind", "resource_type"}:
                return _enum_tail(keyword.value)
    return _enum_tail(node)


def _resource_lookup(node: ast.AST | None) -> str | None:
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values, strict=True):
        if _literal_text(key) == "lookup":
            return _literal_text(value)
    return None


def _cedar_type(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    rendered = "".join(word[:1].upper() + word[1:] for word in words) or "Resource"
    return f"Resource{rendered}" if rendered[0].isdigit() else rendered


def _identifier(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not rendered or rendered[0].isdigit():
        rendered = f"route_{rendered}"
    return rendered


def _cedar_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _path_pattern(path: str | None) -> str:
    if path is None:
        return "*"
    return re.sub(r"\{[^{}]+\}", "*", path)


def _cedar_condition(condition: str | None) -> str | None:
    """Preserve the narrow Cedar attribute shape without inventing semantics."""
    if condition is None:
        return None
    match = re.fullmatch(r"principal\.([A-Za-z_][A-Za-z0-9_]*)", condition)
    if match is None:
        return None
    return f"principal.{match.group(1)}"


def _factory_defaults(root: Path) -> dict[str, dict[str, ast.AST]]:
    """Statically visible dependency-factory defaults, only when unambiguous."""
    found: dict[str, list[dict[str, ast.AST]]] = {}
    for path in _iter_py(root):
        try:
            tree = _parse_file(path)
        except OSError, UnicodeError, SyntaxError, ValueError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = (*node.args.posonlyargs, *node.args.args)
            defaulted_arguments = (
                positional[-len(node.args.defaults) :] if node.args.defaults else ()
            )
            positional_defaults = {
                argument.arg: default
                for argument, default in zip(defaulted_arguments, node.args.defaults, strict=True)
            }
            keyword_defaults = {
                argument.arg: default
                for argument, default in zip(
                    node.args.kwonlyargs, node.args.kw_defaults, strict=True
                )
                if default is not None
            }
            defaults = positional_defaults | keyword_defaults
            relevant: dict[str, ast.AST] = {
                name: value
                for name, value in defaults.items()
                if name in _ACTION_WORDS + _RESOURCE_WORDS + _CONDITION_WORDS
            }
            if relevant:
                found.setdefault(node.name, []).append(relevant)

    resolved: dict[str, dict[str, ast.AST]] = {}
    for name, candidates in found.items():
        signatures = {
            tuple(sorted((key, ast.dump(value)) for key, value in candidate.items()))
            for candidate in candidates
        }
        if len(signatures) == 1:
            resolved[name] = candidates[0]
    return resolved


def _dependency_calls(node: ast.AST, imports: _Imports) -> list[ast.Call]:
    """Factory calls nested directly under ``Depends``/``Security``."""
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if _tail(child.func, imports) not in {"Depends", "Security"} or not child.args:
            continue
        target = child.args[0]
        if isinstance(target, ast.Call):
            found.append(target)
    return found


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """One dependency guard whose action/resource vocabulary is recoverable."""

    factory: str
    action: str | None
    resource_type: str | None
    resource: str | None
    lookup: str | None
    condition: str | None
    conditions: tuple[str, ...]
    complete: bool

    @classmethod
    def from_call(
        cls,
        call: ast.Call,
        imports: _Imports,
        defaults: dict[str, ast.AST] | None = None,
    ) -> PolicyCandidate | None:
        factory = _tail(call.func, imports)
        if not any(word in factory.lower() for word in _GUARD_WORDS):
            return None
        declared = defaults or {}
        action_node = _keyword(call, _ACTION_KEYS) or next(
            (declared[name] for name in _ACTION_WORDS if name in declared), None
        )
        resource_node = _keyword(call, _RESOURCE_KEYS) or next(
            (declared[name] for name in _RESOURCE_WORDS if name in declared), None
        )
        condition_node = _keyword(call, _CONDITION_KEYS) or next(
            (declared[name] for name in _CONDITION_WORDS if name in declared), None
        )
        action = _enum_tail(action_node)
        kind = _resource_type(resource_node)
        condition = _literal_text(condition_node) or None
        conditions = tuple(
            sorted(
                set(re.findall(r"\b(?:principal|context)\.[A-Za-z_][A-Za-z0-9_]*", condition or ""))
            )
        )
        return cls(
            factory=factory,
            action=action,
            resource_type=kind,
            resource=_expression(resource_node),
            lookup=_resource_lookup(resource_node),
            condition=condition,
            conditions=conditions,
            complete=action is not None and kind is not None,
        )

    @property
    def action_id(self) -> str | None:
        if not self.complete:
            return None
        return f"{_cedar_type(self.resource_type or '')}::{self.action}"

    def as_dict(self) -> dict[str, Any]:
        decorator = None
        if self.action_id is not None and self.resource is not None:
            decorator = f"authorize(action={self.action_id!r}, resource={self.resource})"
        return {
            "factory": self.factory,
            "action": self.action,
            "action_id": self.action_id,
            "resource_type": self.resource_type,
            "resource": self.resource,
            "lookup": self.lookup,
            "condition": self.condition,
            "conditions": list(self.conditions),
            "complete": self.complete,
            "cedar_effect": "permit" if _cedar_condition(self.condition) else "deny-review",
            "wreath_decorator": decorator,
        }


@dataclass(frozen=True, slots=True)
class RouteContract:
    """A source-level HTTP or WebSocket route with explicit uncertainty."""

    file: str
    line: int
    owner: str
    method: str
    path: str | None
    handler: str
    dependencies: tuple[str, ...]
    access: str
    policies: tuple[PolicyCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "owner": self.owner,
            "method": self.method,
            "path": self.path,
            "handler": self.handler,
            "dependencies": list(self.dependencies),
            "access": self.access,
            "policies": [item.as_dict() for item in self.policies],
        }


def _route_contracts(root: Path) -> tuple[RouteContract, ...]:
    routes: list[RouteContract] = []
    factory_defaults = _factory_defaults(root)
    for path in _iter_py(root):
        try:
            tree = _parse_file(path)
        except OSError, UnicodeError, SyntaxError, ValueError:
            continue
        imports = _Imports().visit(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            access_decorators = {
                _tail(dec.func if isinstance(dec, ast.Call) else dec, imports).lower()
                for dec in node.decorator_list
            }
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                ):
                    continue
                method = decorator.func.attr.lower()
                if method not in _ROUTE_METHODS | {"websocket"}:
                    continue
                dependency_calls = _dependency_calls(node, imports)
                dependencies = tuple(
                    sorted({_tail(call.func, imports) for call in dependency_calls})
                )
                policies = tuple(
                    candidate
                    for call in dependency_calls
                    if (
                        candidate := PolicyCandidate.from_call(
                            call,
                            imports,
                            factory_defaults.get(_tail(call.func, imports)),
                        )
                    )
                    is not None
                )
                if access_decorators & _ACCESS_DECORATORS:
                    access = "declared"
                elif policies or any(
                    any(word in dependency.lower() for word in _GUARD_WORDS)
                    for dependency in dependencies
                ):
                    access = "dependency-guarded"
                else:
                    access = "implicit-or-inherited"
                routes.append(
                    RouteContract(
                        file=str(path.relative_to(root)) if path != root else path.name,
                        line=decorator.lineno,
                        owner=_expression(decorator.func.value) or "<dynamic>",
                        method="WEBSOCKET" if method == "websocket" else method.upper(),
                        path=_literal_text(decorator.args[0]) if decorator.args else None,
                        handler=node.name,
                        dependencies=dependencies,
                        access=access,
                        policies=policies,
                    )
                )
    return tuple(sorted(routes, key=lambda route: (route.file, route.line, route.method)))


def _manifest(root: Path) -> Path | None:
    start = root.parent if root.is_file() else root
    for candidate in (start, *tuple(start.parents)[:3]):
        path = candidate / "pyproject.toml"
        if path.is_file():
            return path
    return None


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = [int(part) for part in value.split(".")[:3]]
    major, minor, patch = (parts + [0, 0, 0])[:3]
    return major, minor, patch


def _allows_python(specifier: str | None, target: tuple[int, int, int]) -> bool | None:
    if not specifier:
        return None
    answer = True
    understood = False
    for raw in specifier.split(","):
        match = _VERSION_CLAUSE.match(raw.strip())
        if match is None:
            continue
        understood = True
        op = match.group(1)
        version = tuple(int(item or 0) for item in match.groups()[1:])
        if op == ">=":
            answer &= target >= version
        elif op == ">":
            answer &= target > version
        elif op == "<=":
            answer &= target <= version
        elif op == "<":
            answer &= target < version
        elif op == "==":
            answer &= target == version
        elif op == "!=":
            answer &= target != version
        elif op == "~=":
            answer &= target >= version and target[:1] == version[:1]
    return answer if understood else None


def _dependency_name(requirement: str) -> str:
    match = _DEPENDENCY_NAME.match(requirement.strip())
    return (match.group(0) if match is not None else requirement.strip()).lower().replace("_", "-")


def _dependency_audit(root: Path, target: tuple[int, int, int]) -> dict[str, Any]:
    manifest = _manifest(root)
    if manifest is None:
        return {
            "manifest": None,
            "requires_python": None,
            "target_status": "unknown",
            "dependencies": [],
        }
    document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    project = document.get("project", {})
    requires_python = project.get("requires-python")
    allowed = _allows_python(requires_python, target)
    lock_path = manifest.with_name("uv.lock")
    lock_python = None
    if lock_path.is_file():
        try:
            lock_python = tomllib.loads(lock_path.read_text(encoding="utf-8")).get(
                "requires-python"
            )
        except tomllib.TOMLDecodeError:
            lock_python = None
    lock_allowed = _allows_python(lock_python, target)
    sources = document.get("tool", {}).get("uv", {}).get("sources", {})
    requirements = list(project.get("dependencies", ()))
    for values in project.get("optional-dependencies", {}).values():
        requirements.extend(values)
    dependencies = []
    for requirement in sorted(set(requirements)):
        name = _dependency_name(requirement)
        source = sources.get(name, sources.get(name.replace("-", "_"), {}))
        source_kind = next(
            (
                kind
                for kind in ("path", "git", "url", "index")
                if isinstance(source, dict) and kind in source
            ),
            "default-index",
        )
        dependencies.append(
            {
                "name": name,
                "requirement": requirement,
                "source": source_kind,
                "replacement": _REPLACEMENTS.get(name),
                "python_compatibility": "unverified",
            }
        )
    status = "compatible"
    if allowed is False:
        status = "project-blocked"
    elif lock_allowed is False:
        status = "lock-blocked"
    elif allowed is None:
        status = "unknown"
    return {
        "manifest": str(manifest),
        "requires_python": requires_python,
        "lock_requires_python": lock_python,
        "target_status": status,
        "dependencies": dependencies,
    }


def _imported_integrations(root: Path) -> tuple[str, ...]:
    local = {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()
    imported: set[str] = set()
    for path in _iter_py(root):
        try:
            tree = _parse_file(path)
        except OSError, UnicodeError, SyntaxError, ValueError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".", 1)[0]
                if top not in sys.stdlib_module_names and top not in local:
                    imported.add(top)
    return tuple(sorted(imported))


@dataclass(frozen=True, slots=True)
class ProjectReport:
    name: str
    root: str
    report: Report
    routes: tuple[RouteContract, ...]
    integrations: tuple[str, ...]
    dependencies: dict[str, Any]

    def effective_counts(self, *, migration_strategy: str) -> dict[str, int]:
        """Counts after applying an explicitly selected adoption strategy."""
        counts = self.report.counts()
        retired = 0
        if migration_strategy == "baseline":
            for finding in self.report.findings:
                if finding.rule_id not in _BASELINE_RETIRED_RULES:
                    continue
                counts[finding.tag.replace("-", "_")] -= 1
                retired += 1
        return {**counts, "retired": retired}

    def as_dict(self, *, migration_strategy: str) -> dict[str, Any]:
        report = self.report.as_dict()
        effective = self.effective_counts(migration_strategy=migration_strategy)
        report["effective_counts"] = effective
        report["baseline_retired_findings"] = effective["retired"]
        policies = [policy for route in self.routes for policy in route.policies]
        actions = sorted({policy.action_id for policy in policies if policy.action_id})
        resources = sorted({policy.resource_type for policy in policies if policy.resource_type})
        return {
            "name": self.name,
            "root": self.root,
            "analysis": report,
            "routes": [route.as_dict() for route in self.routes],
            "security": {
                "implicit_or_inherited": sum(
                    route.access == "implicit-or-inherited" for route in self.routes
                ),
                "policy_candidates": [policy.as_dict() for policy in policies],
                "typed_actions": actions,
                "resource_types": resources,
            },
            "integrations": list(self.integrations),
            "dependency_audit": self.dependencies,
        }


class MigrationInventory:
    """One provenance-preserving inventory across several source projects."""

    __slots__ = ("migration_strategy", "projects", "target_python")

    def __init__(
        self,
        projects: tuple[ProjectReport, ...],
        *,
        target_python: tuple[int, int, int],
        migration_strategy: str,
    ) -> None:
        self.projects = projects
        self.target_python = target_python
        self.migration_strategy = migration_strategy

    def counts(self) -> dict[str, int]:
        totals = {
            "translated": 0,
            "needs_review": 0,
            "unsupported": 0,
            "retired": 0,
        }
        for project in self.projects:
            counts = project.effective_counts(migration_strategy=self.migration_strategy)
            totals["translated"] += counts["translated"]
            totals["needs_review"] += counts["needs_review"]
            totals["unsupported"] += counts["unsupported"]
            totals["retired"] += counts["retired"]
        return totals

    def as_dict(self) -> dict[str, Any]:
        routes = sum(len(project.routes) for project in self.projects)
        return {
            "format": "wreath-port-inventory-1",
            "target_python": ".".join(map(str, self.target_python[:2])),
            "migration_strategy": self.migration_strategy,
            "counts": self.counts(),
            "route_count": routes,
            "projects": [
                project.as_dict(migration_strategy=self.migration_strategy)
                for project in self.projects
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def cedar_module(
        self,
        *,
        required_condition: str | None = None,
        default_condition: str | None = None,
        action_conditions: Mapping[str, str] | None = None,
        condition_map: Mapping[str, str] | None = None,
        authentication_factories: Collection[str] = (),
    ) -> str:
        """Render fail-closed Cedar and route decorators for complete guards.

        A Cedar-shaped principal attribute predicate is preserved as a permit;
        configured required, condition, and action mappings can express richer
        generic temporal or hierarchical policy. Every other complete candidate gets a
        path-scoped forbid, so generated code compiles and protects the route
        while making the missing grant a review task. Incomplete candidates
        stay in `REVIEW` and never become an invented authorization decision.
        """
        actions = action_conditions or {}
        conditions = condition_map or {}
        authentication = frozenset(authentication_factories)
        policies: list[str] = []
        functions: list[str] = []
        decorators: list[tuple[str, str]] = []
        review: list[dict[str, Any]] = []
        seen_policies: set[str] = set()
        for project in self.projects:
            for route in project.routes:
                policy_candidates = [
                    candidate
                    for candidate in route.policies
                    if candidate.factory not in authentication
                ]
                complete = [candidate for candidate in policy_candidates if candidate.complete]
                for candidate in policy_candidates:
                    if not candidate.complete:
                        review.append(
                            {
                                "project": project.name,
                                "file": route.file,
                                "line": route.line,
                                "handler": route.handler,
                                "factory": candidate.factory,
                                "reason": "action or resource type was not static",
                            }
                        )
                if not complete:
                    continue
                decorator_name = _identifier(
                    f"authorize_{project.name}_{route.handler}_{route.line}"
                )
                decorators.append(
                    (f"{project.name}:{route.file}:{route.line}:{route.handler}", decorator_name)
                )
                body = [f"def {decorator_name}(handler: Any) -> Any:"]
                for index, candidate in enumerate(complete):
                    entity_type = _cedar_type(candidate.resource_type or "")
                    action = candidate.action_id or ""
                    pattern = _path_pattern(route.path)
                    if candidate.condition is not None:
                        specific = conditions.get(
                            candidate.condition, _cedar_condition(candidate.condition)
                        )
                        condition_known = specific is not None
                    else:
                        specific = default_condition
                        condition_known = True
                    condition = " && ".join(
                        f"({item})"
                        for item in (
                            required_condition,
                            actions.get(candidate.action or ""),
                            specific,
                        )
                        if item is not None
                    )
                    path_clause = f'context.path like "{_cedar_string(pattern)}"'
                    if not condition_known or not condition:
                        statement = (
                            f'forbid(principal, action == Action::"{_cedar_string(action)}", '
                            f"resource is {entity_type}) when {{ {path_clause} }};"
                        )
                        review.append(
                            {
                                "project": project.name,
                                "file": route.file,
                                "line": route.line,
                                "handler": route.handler,
                                "factory": candidate.factory,
                                "reason": (
                                    "replace generated default-deny with the intended Cedar grant"
                                ),
                            }
                        )
                    else:
                        statement = (
                            f'permit(principal, action == Action::"{_cedar_string(action)}", '
                            f"resource is {entity_type}) when {{ {path_clause} && {condition} }};"
                        )
                    if statement not in seen_policies:
                        policies.append(statement)
                        seen_policies.add(statement)
                    resource_name = f"_{decorator_name}_resource_{index}"
                    placeholders = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)", route.path or "")
                    lookup = (
                        candidate.lookup
                        if candidate.lookup in placeholders
                        else (placeholders[0] if placeholders else None)
                    )
                    if lookup is None:
                        identifier = "request.path"
                    else:
                        identifier = f"request.path_params[{lookup!r}]"
                    functions.extend(
                        (
                            "",
                            f"def {resource_name}(request: Request) -> EntityUid:",
                            f"    return EntityUid({entity_type!r}, str({identifier}))",
                        )
                    )
                    body.append(
                        f"    handler = authorize(action={action!r}, "
                        f"resource={resource_name})(handler)"
                    )
                body.append("    return handler")
                functions.extend(("", *body))
        if not policies:
            policies.append("forbid(principal, action, resource);")
        policy_source = "\n".join(policies) + "\n"
        routes = "{\n" + "".join(f"    {key!r}: {name},\n" for key, name in decorators) + "}"
        review_literal = json.dumps(review, indent=4, sort_keys=True)
        return (
            '"""Generated Wreath authorization declarations; review every REVIEW entry."""\n\n'
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n"
            "from wreath.authorization import (\n"
            "    CedarAuthorizer,\n"
            "    CedarPolicies,\n"
            "    EntityUid,\n"
            "    authorize,\n"
            ")\n"
            "from wreath.request import Request\n\n"
            f"POLICY_SOURCE = {policy_source!r}\n"
            "POLICIES = CedarPolicies(POLICY_SOURCE)\n"
            "AUTHORIZER = CedarAuthorizer(engine=POLICIES)\n"
            + "\n".join(functions)
            + "\n\n"
            + f"ROUTE_DECORATORS = {routes}\n"
            + f"REVIEW = {review_literal}\n"
        )

    def to_markdown(self) -> str:
        counts = self.counts()
        lines = [
            "# wreath port — migration inventory",
            "",
            f"- projects: **{len(self.projects)}**",
            f"- routes inventoried: **{sum(len(project.routes) for project in self.projects)}**",
            f"- target Python: **{'.'.join(map(str, self.target_python[:2]))}**",
            f"- migration strategy: **{self.migration_strategy}**",
            f"- translated: **{counts['translated']}** · needs-review: "
            f"**{counts['needs_review']}** · unsupported: **{counts['unsupported']}** · "
            f"retired history: **{counts['retired']}**",
            "",
            "| project | files | routes | implicit/inherited access | Python | "
            "review | unsupported |",
            "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
        ]
        for project in self.projects:
            counts = project.effective_counts(migration_strategy=self.migration_strategy)
            implicit = sum(route.access == "implicit-or-inherited" for route in project.routes)
            lines.append(
                f"| {project.name} | {project.report.files_analyzed} | {len(project.routes)} | "
                f"{implicit} | {project.dependencies['target_status']} | "
                f"{counts['needs_review']} | {counts['unsupported']} |"
            )
        return "\n".join(lines) + "\n"


def inventory_projects(
    roots: Sequence[str | Path],
    *,
    target_python: str = "3.14",
    migration_strategy: str = "preserve",
) -> MigrationInventory:
    """Analyze several applications without merging away their ownership."""
    if migration_strategy not in {"preserve", "baseline"}:
        raise ValueError("migration_strategy must be 'preserve' or 'baseline'")
    target = _version_tuple(target_python)
    paths = [Path(root) for root in roots]
    base_names = [path.name or path.parent.name for path in paths]
    totals: dict[str, int] = {}
    for base_name in base_names:
        totals[base_name] = totals.get(base_name, 0) + 1
    seen: dict[str, int] = {}
    projects: list[ProjectReport] = []
    for root, base_name in zip(paths, base_names, strict=True):
        seen[base_name] = seen.get(base_name, 0) + 1
        suffix = seen[base_name]
        name = base_name if totals[base_name] == 1 else f"{base_name}-{suffix}"
        projects.append(
            ProjectReport(
                name=name,
                root=str(root),
                report=analyze(root),
                routes=_route_contracts(root),
                integrations=_imported_integrations(root),
                dependencies=_dependency_audit(root, target),
            )
        )
    return MigrationInventory(
        tuple(projects), target_python=target, migration_strategy=migration_strategy
    )


__all__ = [
    "PolicyCandidate",
    "ProjectReport",
    "RouteContract",
    "MigrationInventory",
    "inventory_projects",
]
