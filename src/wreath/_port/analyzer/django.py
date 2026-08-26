"""The Django image of a tree: which models are plain, and what they declare.

Whether ``Model.objects`` is every row is a property of the **model**, and the
model is declared in one module and queried from a dozen others. Asking the
*querying* module whether it imports Django answers a different question
entirely: two files carrying the same chains against the same manager-free
models get opposite verdicts because one of them also imports
``django.db.transaction``. So the manager is resolved here, once, over the whole
tree, and every call site is classified against that answer.

A model is plain when neither it nor any ancestor this tree declares attaches a
``Manager``/``QuerySet`` subclass or overrides ``save``/``delete``. Both are
inherited in Django -- an abstract base model carrying ``objects =
SoftDeleteManager()`` hands its predicate to every subclass -- so the walk climbs
the tree-wide base map rather than reading one class body.

The columns and relations are collected in the same pass because a query rewrite
needs them: ``.filter(observer__email=x)`` is only a translation once ``observer``
has been proven to point at ``Observer``, and ``Observer`` is in another file.

**Reverse accessors come from ``ForeignKey``/``OneToOneField`` and never from
``ManyToManyField``.** ``related_name`` on a foreign key names a one-to-many
wreath declares; on a many-to-many it names an association table Django creates
implicitly and wreath has no model for, so registering it would turn a query
across a table that does not exist into a `translated` verdict.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from ..foreign import (
    _DJANGO_MANAGERS,
    django_manager_callees,
    django_overrides_persistence,
    resolve_family,
)
from .imports import _Imports
from .sources import _SKIPPABLE, _parse_file

#: Django field constructors that declare a relation to another model.
_DJANGO_RELATION_FIELDS = ("ForeignKey", "OneToOneField")


@dataclass(frozen=True)
class DjangoImage:
    """What the whole tree knows about its Django models."""

    #: Every class in the tree whose base resolves to `django.db.models.Model`.
    models: frozenset[str] = frozenset()
    #: The subset whose `.objects` provably selects every row of the table.
    plain_models: frozenset[str] = frozenset()
    columns: dict[str, set[str]] = dataclass_field(default_factory=dict)
    relations: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    tables: dict[str, str] = dataclass_field(default_factory=dict)

    def objects_is_every_row(self, model: str, *, reads_django: bool) -> bool:
        """Whether `<model>.objects` can be read as `<model>.select()`.

        A model this tree declares answers for itself. One it does not -- a
        `django.contrib.auth` model, or a name that is not a model at all --
        falls back to the module-level question, which is what keeps an ormar
        tree (where nothing imports Django) translating exactly as before.
        """
        if model in self.models:
            return model in self.plain_models
        return not reads_django


def _model_bases(node: ast.ClassDef) -> list[str]:
    out = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def _declared(
    node: ast.ClassDef,
) -> tuple[set[str], dict[str, str], list[tuple[str, str]], str | None]:
    """One Django model body: its columns, relations, reverse accessors, table.

    The reverse accessors are a **list** of `(owning model, accessor)` pairs and
    not a dictionary. One `related_name` is unique per target, not per declaring
    class: `Tally` points at both `Range` and `Observer` with
    `related_name="tallies"`, which is legal and ordinary, and keying by the
    accessor alone lost whichever was read first.
    """
    columns: set[str] = set()
    relations: dict[str, str] = {}
    reverse: list[tuple[str, str]] = []
    table: str | None = None
    for stmt in node.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            for child in stmt.body:
                if (
                    isinstance(child, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "db_table"
                        for target in child.targets
                    )
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)
                ):
                    table = child.value.value
            continue
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
        elif (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            name = stmt.targets[0].id
        else:
            continue
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        tail = (
            value.func.attr
            if isinstance(value.func, ast.Attribute)
            else value.func.id
            if isinstance(value.func, ast.Name)
            else ""
        )
        if not tail.endswith("Field") and tail not in _DJANGO_RELATION_FIELDS:
            continue
        columns.add(name)
        if tail not in _DJANGO_RELATION_FIELDS:
            continue
        target = value.args[0] if value.args else None
        target_name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else target.value
            if isinstance(target, ast.Constant) and isinstance(target.value, str)
            else None
        )
        if target_name is None:
            continue
        relations[name] = target_name
        related = next(
            (
                keyword.value.value
                for keyword in value.keywords
                if keyword.arg == "related_name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            None,
        )
        if related is not None:
            reverse.append((target_name, related))
    return columns, relations, reverse, table


def django_image(
    files: list[Path],
    on_skip=None,
    trees: Mapping[Path, ast.Module] | None = None,
) -> DjangoImage:
    """Read every Django model in the tree, and decide which are plain."""
    class_bases: dict[str, list[str]] = {}
    managers: dict[str, tuple[str, ...]] = {}
    persistence: set[str] = set()
    models: set[str] = set()
    columns: dict[str, set[str]] = {}
    relations: dict[str, dict[str, str]] = {}
    tables: dict[str, str] = {}
    #: `related_name` -> (owning model, target model), applied after the walk so
    #: a forward declaration in another file still resolves.
    reverses: list[tuple[str, str, str]] = []
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
        django = "django" in imports.roots
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            class_bases.setdefault(node.name, []).extend(_model_bases(node))
            managers[node.name] = django_manager_callees(node)
            if django_overrides_persistence(node):
                persistence.add(node.name)
            if not (django and any(base.endswith("Model") for base in _model_bases(node))):
                continue
            models.add(node.name)
            declared, forward, reverse, table = _declared(node)
            columns[node.name] = declared
            relations[node.name] = forward
            if table is not None:
                tables[node.name] = table
            reverses.extend((owner, accessor, node.name) for owner, accessor in reverse)
    for owner, accessor, target in reverses:
        relations.setdefault(owner, {}).setdefault(accessor, target)
    manager_family = resolve_family(class_bases, _DJANGO_MANAGERS)

    def carries(name: str) -> bool:
        """Whether this model or any ancestor in the tree is not just fields."""
        seen: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in persistence or any(
                callee in _DJANGO_MANAGERS or callee in manager_family
                for callee in managers.get(current, ())
            ):
                return True
            stack.extend(class_bases.get(current, ()))
        return False

    return DjangoImage(
        frozenset(models),
        frozenset(name for name in models if not carries(name)),
        columns,
        relations,
        tables,
    )
