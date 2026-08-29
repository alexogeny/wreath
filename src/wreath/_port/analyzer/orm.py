"""The ORM image of a tree: which classes are models, what columns and
constraints they declare, and what type each primary key is."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from .imports import _Imports
from .nodes import _is_true
from .sources import _SKIPPABLE, _parse_file

# ormar PK column type -> wreath PgType name, for FK type inference from the referenced model.
_PK_PGTYPE = {
    "UUID": "Uuid",
    "Integer": "Int64",
    "BigInteger": "Int64",
    "SmallInteger": "Int16",
    "String": "Varchar",
    "Text": "Text",
}


def module_pk_types(tree: ast.Module, imports: _Imports) -> dict[str, str]:
    """{ORM model name -> wreath PgType of its primary key}, resolved within one module.

    Used to give a ForeignKey its real column type instead of a guess. See
    `tree_pk_types` for the whole-tree version, which is what actually resolves
    most of them — a model is usually declared in a different file from the one
    that points at it.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _base_kind(imports, node) in ("ormar", "sqlmodel"):
            for stmt in node.body:
                value = stmt.value if isinstance(stmt, (ast.AnnAssign, ast.Assign)) else None
                if not isinstance(value, ast.Call):
                    continue
                if any(kw.arg == "primary_key" and _is_true(kw.value) for kw in value.keywords):
                    pg = _PK_PGTYPE.get(imports.origin(value.func).split(".")[-1])
                    if pg:
                        out[node.name] = pg
                    break
    return out


def tree_pk_types(
    files: list[Path],
    on_skip=None,
    trees: Mapping[Path, ast.Module] | None = None,
) -> dict[str, str]:
    """{ORM model name -> primary key type} across every file in the tree.

    A foreign key names a model, and that model almost never lives in the same
    file — almost every foreign key points out of its own module — and each one
    was emitted with a guessed `Uuid` column and a note asking a human to look up
    a type this walk can just read.

    **A name declared twice with two different key types resolves to neither.**
    Two apps in one tree can both own a `Llama`, and picking whichever was
    walked first would silently give one of them the other's key type. Dropping
    the name puts it back where it was: flagged, with the type left to a human.
    """
    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for path in files:
        if trees is not None:
            tree = trees.get(path)
            if tree is None:
                continue
        else:
            try:
                tree = _parse_file(path)
            except _SKIPPABLE as exc:
                if on_skip is not None:
                    on_skip(path, exc)
                continue
        for name, pg in module_pk_types(tree, _Imports().visit(tree)).items():
            if out.setdefault(name, pg) != pg:
                ambiguous.add(name)
    for name in ambiguous:
        del out[name]
    return out


def _base_kind(imports: _Imports, cls: ast.ClassDef) -> str | None:
    """Which framework model kind (if any) this class subclasses."""
    for base in cls.bases:
        origin = imports.origin(base)
        if origin == "pydantic.BaseModel":
            return "pydantic"
        # A BaseHTTPMiddleware subclass is the middleware itself — the construct
        # a porter rewrites. `mw.custom` used to fire only where one was *wired
        # up* (`add_middleware(...)`), so a class defined in its own module and
        # imported elsewhere went unreported entirely.
        if origin in (
            "starlette.middleware.base.BaseHTTPMiddleware",
            "fastapi.middleware.base.BaseHTTPMiddleware",
        ):
            return "middleware"
        # pydantic-settings (v2) and the legacy pydantic v1 BaseSettings both appear
        # in real apps — recognize either.
        if origin in ("pydantic_settings.BaseSettings", "pydantic.BaseSettings"):
            return "settings"
        if origin == "ormar.Model":
            return "ormar"
        if origin == "sqlmodel.SQLModel":
            return "sqlmodel"
    return None


def _class_member_names(node: ast.ClassDef) -> set[str]:
    """Names a class body declares: methods and class-level attributes."""
    out: set[str] = set()
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(child.name)
        elif isinstance(child, ast.Assign):
            out.update(t.id for t in child.targets if isinstance(t, ast.Name))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            out.add(child.target.id)
    return out


def _class_base_names(node: ast.ClassDef) -> list[str]:
    """Trailing names of a class's bases, unresolved.

    Unresolved on purpose: `BaseHandler` imported from a sibling module and
    `web.RequestHandler` both matter here, and the point is to link them by
    name across files rather than to know where either came from.
    """
    out = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def _index_tree(
    files: list[Path],
    on_skip=None,
    trees: Mapping[Path, ast.Module] | None = None,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, dict[str, str]],
    dict[str, str],
    dict[str, tuple[frozenset[str], ...]],
    set[str],
    dict[str, list[str]],
    dict[str, set[str]],
]:
    """Pass 1: model/settings class names, and each ORM model's declared columns.

    The columns are what let a GraphQL type be compared with the model it claims
    to mirror; the names alone can only say the model exists.
    """
    index: dict[str, set[str]] = {
        "pydantic": set(),
        "settings": set(),
        "orm": set(),
        "orm_mixin": set(),
    }
    orm_columns: dict[str, set[str]] = {}
    orm_relations: dict[str, dict[str, str]] = {}
    orm_tables: dict[str, str] = {}
    orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] = {}
    positional_calls: set[str] = set()
    # Tree-wide class -> base names. Handler hierarchies cross modules: one
    # BaseHandler is declared in handlers/base.py and every other handler
    # inherits it by import, so a per-module fixpoint finds one class in a
    # tree that holds fourteen.
    class_bases: dict[str, list[str]] = {}
    # What each class defines itself. `self.write` inside a handler is
    # framework API, and no import statement mentions it -- the only way to
    # tell inherited-from-the-framework from defined-here is to know what
    # the local hierarchy actually declares.
    class_members: dict[str, set[str]] = {}
    classes: list[tuple[ast.ClassDef, _Imports]] = []
    for path in files:
        if trees is not None:
            tree = trees.get(path)
            if tree is None:
                continue
        else:
            try:
                tree = _parse_file(path)
            except _SKIPPABLE as exc:
                if on_skip is not None:
                    on_skip(path, exc)
                continue
        imports = _Imports().visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                if isinstance(node.func, ast.Name):
                    positional_calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    positional_calls.add(node.func.attr)
            if isinstance(node, ast.ClassDef):
                classes.append((node, imports))
                class_bases.setdefault(node.name, []).extend(_class_base_names(node))
                class_members.setdefault(node.name, set()).update(_class_member_names(node))
                kind = _base_kind(imports, node)
                if kind == "pydantic":
                    index["pydantic"].add(node.name)
                elif kind == "settings":
                    index["settings"].add(node.name)
                elif kind in ("ormar", "sqlmodel"):
                    index["orm"].add(node.name)
                    orm_columns[node.name] = _declared_columns(node)
                    relations: dict[str, str] = {}
                    for statement in node.body:
                        if not (
                            isinstance(statement, ast.AnnAssign)
                            and isinstance(statement.target, ast.Name)
                            and isinstance(statement.value, ast.Call)
                            and statement.value.args
                            and imports.origin(statement.value.func)
                            in ("ormar.ForeignKey", "sqlmodel.Relationship")
                        ):
                            continue
                        target = statement.value.args[0]
                        if isinstance(target, ast.Name):
                            relations[statement.target.id] = target.id
                        elif isinstance(target, ast.Attribute):
                            relations[statement.target.id] = target.attr
                        elif isinstance(target, ast.Constant) and isinstance(target.value, str):
                            relations[statement.target.id] = target.value
                    orm_relations[node.name] = relations
                    table = _declared_table(node)
                    if table is not None:
                        orm_tables[node.name] = table
                    orm_unique_constraints[node.name] = _declared_unique_constraints(node, imports)
                elif any(
                    isinstance(statement, (ast.AnnAssign, ast.Assign))
                    and isinstance(statement.value, ast.Call)
                    and (
                        isinstance(statement.target, ast.Name)
                        if isinstance(statement, ast.AnnAssign)
                        else len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                    )
                    and imports.origin(statement.value.func).startswith("ormar.")
                    for statement in node.body
                ):
                    # Ormar permits a plain class containing column declarations
                    # to act as a model mixin. Wreath supports the same shape as
                    # a table-less Model subclass, so index it across files and
                    # let the emitter move the shared columns once at their
                    # declaration rather than copying them into every table.
                    index["orm_mixin"].add(node.name)
                    orm_columns[node.name] = _declared_columns(node)
    # Framework model families are transitive. A direct ``BaseModel`` class is
    # only the root; every ordinary subclass and every ``model_as_partial()``
    # subclass still describes a request/response model and must become a
    # dataclass too. Stopping at the root produced syntactically valid output in
    # which the subclass's own annotations were invisible to dataclasses and to
    # Wreath's binder.
    changed = True
    while changed:
        changed = False
        for node, _imports in classes:
            if node.name in index["pydantic"] or node.name in index["settings"]:
                continue
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else None
                partial_owner = (
                    base.func.value.id
                    if isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "model_as_partial"
                    and isinstance(base.func.value, ast.Name)
                    else None
                )
                if name in index["pydantic"] or partial_owner in index["pydantic"]:
                    index["pydantic"].add(node.name)
                    changed = True
                    break
                if name in index["settings"]:
                    index["settings"].add(node.name)
                    changed = True
                    break
    return (
        index,
        orm_columns,
        orm_relations,
        orm_tables,
        orm_unique_constraints,
        positional_calls,
        class_bases,
        class_members,
    )


def _declared_table(cls: ast.ClassDef) -> str | None:
    """The literal table name on an ormar configuration, when present."""
    for statement in cls.body:
        if not (
            isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "ormar_config"
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Call)
        ):
            continue
        for keyword in statement.value.keywords:
            if (
                keyword.arg == "tablename"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return None


def _declared_unique_constraints(
    cls: ast.ClassDef, imports: _Imports
) -> tuple[frozenset[str], ...]:
    """Unique column sets that PostgreSQL can arbitrate for get-or-create."""
    constraints: list[frozenset[str]] = []
    for statement in cls.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and isinstance(statement.value, ast.Call)
            and any(
                keyword.arg == "unique" and _is_true(keyword.value)
                for keyword in statement.value.keywords
            )
        ):
            constraints.append(frozenset((statement.target.id,)))
        if not (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)):
            continue
        values = next(
            (keyword.value for keyword in statement.value.keywords if keyword.arg == "constraints"),
            None,
        )
        if not isinstance(values, (ast.List, ast.Tuple)):
            continue
        for value in values.elts:
            if not (
                isinstance(value, ast.Call)
                and imports.origin(value.func).split(".")[-1] == "UniqueColumns"
                and value.args
                and all(
                    isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                    for argument in value.args
                )
            ):
                continue
            constraints.append(
                frozenset(
                    argument.value
                    for argument in value.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                )
            )
    return tuple(constraints)


def _declared_columns(cls: ast.ClassDef) -> set[str]:
    """The attribute names an ORM model class declares as columns."""
    names: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            target = stmt.target.id
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            target = stmt.targets[0].id
        else:
            continue
        if target in ("ormar_config", "__tablename__", "model_config") or not isinstance(
            stmt.value, ast.Call
        ):
            continue
        names.add(target)
    return names


def _index_is_over_columns(node: ast.Call) -> bool:
    """`create_index(name, table, ["a", "b"])` — plain columns, not an expression.

    `[sa.text("lower(name)")]` and a runtime column list are both outside what
    detection reads, and both look the same from the verb alone.
    """
    columns = (
        node.args[2]
        if len(node.args) > 2
        else next((kw.value for kw in node.keywords if kw.arg == "columns"), None)
    )
    if not isinstance(columns, (ast.List, ast.Tuple)):
        return False
    return bool(columns.elts) and all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in columns.elts
    )
