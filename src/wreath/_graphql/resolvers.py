"""Custom and chained resolvers.

The ORM-derived schema covers columns and relationships. Everything else -- a
computed field, a field that calls another service, a root field that is not a
table -- is a resolver, and the shape of the resolver API is the whole design
question. Two decisions:

**Batch by default.** A resolver receives *the whole level*, not one parent, and
returns one value per parent. Per-parent resolvers are how GraphQL servers grow
N+1 problems in application code even when the data layer is clean, so the
batched form is the one that is easy to write and the per-parent form
(`batch=False`) is the convenience wrapper -- not the other way round.

**Chaining is declared, not discovered.** A resolver that needs another field
says so with `requires=`. The executor topologically orders the level's fields
so a dependency is resolved, in batch, before anything that reads it. That makes
"this computed field needs the posts relationship loaded" a one-word
declaration instead of a hidden `await` inside a loop.

    @api.field("User", "postCount", requires=["posts"])
    async def post_count(parents, info):
        return [len(user.posts) for user in parents]

The dependency graph is validated when the schema is built, so a cycle or a
missing dependency is a startup error, never a request-time surprise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ResolverError", "ResolverInfo", "ResolverSpec", "order_fields"]


class ResolverError(Exception):
    """A resolver could not be registered, or its dependencies do not resolve."""


@dataclass(frozen=True, slots=True)
class ResolverInfo:
    """What a resolver is told about the call.

    Deliberately small. A resolver that needs the database takes the session; a
    resolver that needs the caller takes the request. Nothing here exposes the
    executor's internals, so the execution strategy stays free to change.
    """

    request: Any
    session: Any
    #: Arguments supplied on this field, with variables already substituted.
    arguments: dict[str, Any]
    #: The response path to this field, for error reporting.
    path: tuple[str, ...]
    #: The parent object type's name.
    parent_type: str


@dataclass(frozen=True, slots=True)
class ResolverSpec:
    """One registered resolver."""

    type_name: str
    field_name: str
    fn: Callable[..., Any]
    #: Sibling fields that must be resolved before this one runs.
    requires: tuple[str, ...] = ()
    #: False when `fn` takes a single parent instead of the level.
    batch: bool = True
    #: The GraphQL type this field reports, e.g. "Int" or a model name.
    type_name_out: str = "String"
    is_list: bool = False
    non_null: bool = False
    #: An authorization resource; None falls back to "Type.field".
    policy: str | None = None
    #: Extra weight this field contributes to query complexity.
    cost: int = 1


@dataclass
class ResolverRegistry:
    """Every resolver an endpoint knows, indexed for O(1) lookup."""

    by_field: dict[tuple[str, str], ResolverSpec] = field(default_factory=dict)
    roots: dict[str, ResolverSpec] = field(default_factory=dict)
    mutations: dict[str, ResolverSpec] = field(default_factory=dict)

    def add(self, spec: ResolverSpec) -> None:
        key = (spec.type_name, spec.field_name)
        if key in self.by_field:
            raise ResolverError(
                f"a resolver for {spec.type_name}.{spec.field_name} is already registered"
            )
        self.by_field[key] = spec

    def add_root(self, spec: ResolverSpec, *, mutation: bool = False) -> None:
        target = self.mutations if mutation else self.roots
        kind = "mutation" if mutation else "query"
        if spec.field_name in target:
            raise ResolverError(
                f"a {kind} resolver named {spec.field_name!r} is already registered"
            )
        target[spec.field_name] = spec

    def for_field(self, type_name: str, field_name: str) -> ResolverSpec | None:
        return self.by_field.get((type_name, field_name))


def order_fields(
    selected: list[Any],
    resolvers: dict[str, ResolverSpec],
    *,
    type_name: str,
) -> list[Any]:
    """Order one level's fields so every `requires` runs first.

    `selected` is the level's AST fields; `resolvers` maps a field name to
    its spec. Returns the same fields, reordered. A dependency the client did
    not select is *not* injected -- it is resolved as a hidden prerequisite by
    the executor instead, so asking for a computed field never silently widens
    the response.

    Raises `ResolverError` on a dependency cycle, naming the cycle.
    """
    by_name: dict[str, Any] = {}
    for item in selected:
        by_name.setdefault(item.name, item)

    ordered: list[Any] = []
    placed: set[str] = set()
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in placed:
            return
        if name in visiting:
            cycle = " -> ".join((*visiting[visiting.index(name):], name))
            raise ResolverError(
                f"resolver dependency cycle on {type_name}: {cycle}"
            )
        spec = resolvers.get(name)
        if spec is not None:
            visiting.append(name)
            for dependency in spec.requires:
                visit(dependency)
            visiting.pop()
        placed.add(name)
        selection = by_name.get(name)
        if selection is not None:
            ordered.append(selection)

    for item in selected:
        visit(item.name)
    # Duplicate selections (the same field twice under different aliases) are
    # preserved: only the first occurrence drove ordering.
    seen_ids = {id(item) for item in ordered}
    ordered.extend(item for item in selected if id(item) not in seen_ids)
    return ordered


def validate_dependencies(
    registry: ResolverRegistry, known_fields: dict[str, set[str]]
) -> None:
    """Check every `requires` names a real field, at schema-build time.

    A dependency that does not exist is a wiring mistake, and finding it on the
    first request that happens to select that field is far too late.
    """
    for (type_name, field_name), spec in registry.by_field.items():
        available = known_fields.get(type_name)
        if available is None:
            raise ResolverError(
                f"resolver {type_name}.{field_name} targets unknown type {type_name!r}"
            )
        for dependency in spec.requires:
            if dependency not in available:
                raise ResolverError(
                    f"{type_name}.{field_name} requires {dependency!r}, which is "
                    f"not a field of {type_name}"
                )
    # A cycle is detectable without a selection: walk the declared graph.
    for type_name, available in known_fields.items():
        specs = {
            name: spec
            for name in available
            if (spec := registry.for_field(type_name, name)) is not None
        }
        if specs:
            order_fields(
                [_NameOnly(name) for name in specs], specs, type_name=type_name
            )


@dataclass(frozen=True, slots=True)
class _NameOnly:
    """A stand-in selection for build-time cycle detection."""

    name: str
