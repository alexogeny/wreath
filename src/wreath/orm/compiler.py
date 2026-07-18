"""Compile immutable query objects into parameterized PostgreSQL statements.

This runs once per query *shape* and the result is cached on the registry, so
it is not on the per-row path; readable, auditable SQL generation is worth more
here than speed. Values never reach the SQL text or the cache key.

Only semantics-preserving rewrites happen: duplicate projections collapse,
primary keys are added because identity requires them, and duplicate load
requests merge. PostgreSQL chooses scans and joins; Wreath chooses only the shape
of the data and the number of round trips.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._native import _core
from .errors import DeclarationError, ORMError
from .expressions import (
    AND,
    ASC,
    DESC,
    BinaryExpr,
    BooleanExpr,
    ColumnExpr,
    Expression,
    InExpr,
    OrderExpr,
    Predicate,
    RelatedColumnExpr,
    UnaryExpr,
    ValueExpr,
)
from .query import Select
from .relations import LoadOption
from .schema import ColumnSpec, ModelSpec, RelationshipSpec

# Only these operator tokens can reach SQL text. Anything else is a bug in a
# node constructor rather than user input, and must not be rendered.
_BINARY_OPERATORS = frozenset({"=", "<>", "<", "<=", ">", ">=", "LIKE", "ILIKE"})
_BOOLEAN_OPERATORS = frozenset({"AND", "OR"})
_UNARY_OPERATORS = frozenset({"NOT", "IS NULL", "IS NOT NULL"})
_IN_OPERATORS = frozenset({"IN", "NOT IN"})
_DIRECTIONS = frozenset({ASC, DESC})

_INT8_OID = 20

#: PostgreSQL's wire-protocol limit on bind parameters.
MAX_BIND_PARAMETERS = 65535
#: Identities per select-in batch, before the parameter limit is applied.
MAX_SELECTIN_KEYS = 1000


@dataclass(frozen=True, slots=True)
class JoinedStep:
    """A to-one relationship assembled from a LEFT JOIN in the same statement."""

    relationship: RelationshipSpec
    columns: tuple[ColumnSpec, ...]
    offset: int
    alias: str
    nested: tuple[JoinedStep, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectinStep:
    """A relationship loaded by a second, batched statement."""

    relationship: RelationshipSpec
    nested: tuple[LoadOption, ...] = ()


@dataclass(frozen=True, slots=True)
class LoadPlan:
    columns: tuple[ColumnSpec, ...]
    joined: tuple[JoinedStep, ...] = ()
    selectin: tuple[SelectinStep, ...] = ()


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    sql: str
    bind_values: tuple[Any, ...]
    bind_oids: tuple[int, ...]
    result_model: ModelSpec | None
    selected_columns: tuple[ColumnSpec, ...]
    load_plan: LoadPlan
    shape_key: bytes
    #: Columns the caller asked for, when that is narrower than what was
    #: selected; identity forces primary keys into every projection.
    projected_columns: tuple[ColumnSpec, ...] = ()


@dataclass(slots=True)
class _CachedPlan:
    """Everything about a shape that does not depend on runtime values."""

    sql: str
    bind_oids: tuple[int, ...]
    result_model: ModelSpec
    selected_columns: tuple[ColumnSpec, ...]
    load_plan: LoadPlan
    projected_columns: tuple[ColumnSpec, ...]
    #: Native direct-hydration plan for this shape, compiled on first use.
    #: ``False`` records that this shape cannot use the direct path, which is
    #: distinct from "not compiled yet".
    hydrate_plan: Any = None


def quote(identifier: str) -> str:
    """Quote one registry-validated identifier.

    Identifiers reach this function only after ``validate_identifier``, so the
    embedded-quote escape is belt-and-braces rather than the actual defense.
    """
    return '"' + identifier.replace('"', '""') + '"'


def qualified(spec: ModelSpec) -> str:
    return f"{quote(spec.schema)}.{quote(spec.table)}"


class _Builder:
    """Accumulates SQL text and bind values for one statement."""

    __slots__ = ("oids", "parts", "values")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.values: list[Any] = []
        self.oids: list[int] = []

    def text(self, value: str) -> None:
        self.parts.append(value)

    def bind(self, value: Any, oid: int) -> str:
        self.values.append(value)
        self.oids.append(oid)
        return f"${len(self.values)}"

    def sql(self) -> str:
        return "".join(self.parts)


def compile_select(registry: Any, select: Select) -> CompiledQuery:
    """Compile ``select`` against ``registry``, using its bounded plan cache."""
    spec = registry.spec_for(select.model)
    shape_key = shape_of(registry, select)
    plan = registry.cached_plan(shape_key)
    if plan is None:
        plan = registry.store_plan(shape_key, _build_plan(registry, select, spec))
    values, oids = _collect_binds(select)
    if len(values) > MAX_BIND_PARAMETERS:
        raise ORMError(
            f"query needs {len(values)} bind parameters, above PostgreSQL's "
            f"{MAX_BIND_PARAMETERS} limit"
        )
    return CompiledQuery(
        sql=plan.sql,
        bind_values=values,
        bind_oids=plan.bind_oids,
        result_model=plan.result_model,
        selected_columns=plan.selected_columns,
        load_plan=plan.load_plan,
        shape_key=shape_key,
        projected_columns=plan.projected_columns,
    )


def _build_plan(registry: Any, select: Select, spec: ModelSpec) -> _CachedPlan:
    requested = _projection(spec, select)
    projected = requested
    columns = _with_primary_key(spec, requested)
    joined, selectin = _resolve_loads(registry, spec, select)

    builder = _Builder()
    selected: list[ColumnSpec] = list(columns)
    parts = [f"{quote('t0')}.{quote(item.database_name)}" for item in columns]
    clauses: list[str] = []
    joined_steps = _plan_joins(spec, joined, selected, parts, clauses)
    # After the load joins, so alias numbering is stable and the LEFT JOINs a
    # query loads through are emitted before the INNER JOINs it filters through.
    filter_aliases = _plan_filter_joins(registry, spec, select, clauses)

    builder.text("SELECT ")
    builder.text(", ".join(parts))
    builder.text(f" FROM {qualified(spec)} AS {quote('t0')}")
    for clause in clauses:
        builder.text(clause)

    if select.predicates:
        builder.text(" WHERE ")
        _render_predicate(_conjoin(select.predicates), builder, "t0", filter_aliases)
    if select.orderings:
        builder.text(" ORDER BY ")
        builder.text(
            ", ".join(
                f"{quote('t0')}.{quote(item.expression.column.database_name)} "
                f"{_direction(item)}"
                for item in select.orderings
            )
        )
    if select.limit_ is not None:
        builder.text(f" LIMIT {builder.bind(select.limit_, _INT8_OID)}")
    if select.offset_ is not None:
        builder.text(f" OFFSET {builder.bind(select.offset_, _INT8_OID)}")
    if select.for_update_:
        builder.text(" FOR UPDATE")

    return _CachedPlan(
        sql=builder.sql(),
        bind_oids=tuple(builder.oids),
        result_model=spec,
        selected_columns=tuple(selected),
        load_plan=LoadPlan(
            columns=tuple(columns), joined=joined_steps, selectin=tuple(selectin)
        ),
        projected_columns=tuple(projected),
    )


def _filter_paths(node: Expression, out: list[tuple[Any, ...]]) -> None:
    """Every distinct relationship path a predicate reaches through."""
    if isinstance(node, RelatedColumnExpr):
        if node.path not in out:
            out.append(node.path)
        return
    if isinstance(node, BinaryExpr):
        _filter_paths(node.left, out)
        _filter_paths(node.right, out)
        return
    if isinstance(node, InExpr):
        _filter_paths(node.left, out)
        for item in node.values:
            _filter_paths(item, out)
        return
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            _filter_paths(operand, out)
        return
    if isinstance(node, UnaryExpr):
        _filter_paths(node.operand, out)


def _plan_filter_joins(
    registry: Any, spec: ModelSpec, select: Select, clauses: list[str]
) -> dict[tuple[Any, ...], str]:
    """Emit an INNER JOIN per relationship path a predicate filters through.

    These joins constrain rows and select nothing: filtering is not loading, so
    a filtered relation stays unloaded unless the query also `.include()`s it.
    INNER rather than LEFT because a parent with no matching child cannot
    satisfy a predicate on the child's column, and INNER lets PostgreSQL reorder
    the join.
    """
    paths: list[tuple[Any, ...]] = []
    for predicate in select.predicates:
        _filter_paths(predicate, paths)
    if not paths:
        return {}

    aliases: dict[tuple[Any, ...], str] = {}
    for path in paths:
        current = spec
        parent_alias = "t0"
        for depth, relationship in enumerate(path, start=1):
            prefix = path[:depth]
            existing = aliases.get(prefix)
            if existing is not None:
                # A shared prefix joins once: Book.author.name and
                # Book.author.id must not produce two joins of authors.
                current = _relationship_spec_by_name(current, relationship.python_name)
                current = current.target
                parent_alias = existing
                continue
            related = _relationship_spec_by_name(current, relationship.python_name)
            if related.cardinality != "one":
                raise ORMError(
                    f"{current.model_type.__name__}.{relationship.python_name} is a "
                    "to-many relationship; filtering a parent by a collection's "
                    "column would duplicate parents, and needs an EXISTS predicate "
                    "that wreath.orm does not offer yet"
                )
            alias = f"w{len(aliases) + 1}"
            condition = " AND ".join(
                f"{quote(alias)}.{quote(remote.database_name)} = "
                f"{quote(parent_alias)}.{quote(local.database_name)}"
                for local, remote in zip(
                    related.local_columns, related.remote_columns, strict=True
                )
            )
            clauses.append(
                f" INNER JOIN {qualified(related.target)} AS {quote(alias)} ON {condition}"
            )
            aliases[prefix] = alias
            current = related.target
            parent_alias = alias
    return aliases


def _relationship_spec_by_name(spec: ModelSpec, name: str) -> RelationshipSpec:
    for item in spec.relationships:
        if item.name == name:
            return item
    raise ORMError(f"{spec.model_type.__name__} has no relationship {name!r}")


def _plan_joins(
    spec: ModelSpec,
    options: list[LoadOption],
    selected: list[ColumnSpec],
    parts: list[str],
    clauses: list[str],
    parent_alias: str = "t0",
    counter: list[int] | None = None,
) -> tuple[JoinedStep, ...]:
    """Append LEFT JOINs for to-one loads, recording where each lands.

    Column positions are assigned as the joins are emitted, so a hydrator reads
    each model's fields at a fixed offset without searching by name.
    """
    counter = counter if counter is not None else [0]
    steps: list[JoinedStep] = []
    for option in options:
        relationship = _relationship_spec(spec, option)
        counter[0] += 1
        alias = f"j{counter[0]}"
        target = relationship.target
        offset = len(selected)
        columns = _with_primary_key(target, target.columns)
        selected.extend(columns)
        parts.extend(f"{quote(alias)}.{quote(item.database_name)}" for item in columns)
        condition = " AND ".join(
            f"{quote(alias)}.{quote(remote.database_name)} = "
            f"{quote(parent_alias)}.{quote(local.database_name)}"
            for local, remote in zip(
                relationship.local_columns, relationship.remote_columns, strict=True
            )
        )
        clauses.append(f" LEFT JOIN {qualified(target)} AS {quote(alias)} ON {condition}")
        nested = _plan_joins(
            target,
            [item for item in option.nested if item.strategy == "joined"],
            selected,
            parts,
            clauses,
            alias,
            counter,
        )
        steps.append(
            JoinedStep(
                relationship=relationship,
                columns=tuple(columns),
                offset=offset,
                alias=alias,
                nested=nested,
            )
        )
    return tuple(steps)


def _projection(spec: ModelSpec, select: Select) -> tuple[ColumnSpec, ...]:
    if not select.projection:
        return spec.columns
    seen: dict[str, ColumnSpec] = {}
    for item in select.projection:
        # Duplicates keep their first occurrence, so column order stays the
        # order the caller wrote.
        column = spec.by_name[item.column.python_name]
        seen.setdefault(column.python_name, column)
    return tuple(seen.values())


def _with_primary_key(
    spec: ModelSpec, columns: tuple[ColumnSpec, ...]
) -> tuple[ColumnSpec, ...]:
    """Add any missing primary-key columns; identity cannot be built without them."""
    present = {item.python_name for item in columns}
    missing = [item for item in spec.primary_key if item.python_name not in present]
    if not missing:
        return columns
    return (*columns, *missing)


def _relationship_spec(spec: ModelSpec, option: LoadOption) -> RelationshipSpec:
    found = spec.relationship(option.relationship.python_name)
    if found is None or found.relationship.owner is not spec.model_type:
        raise DeclarationError(
            f"{getattr(option.relationship.owner, '__name__', '?')}."
            f"{option.relationship.python_name} is not a relationship of "
            f"{spec.model_type.__name__}"
        )
    return found


def _resolve_loads(
    registry: Any, spec: ModelSpec, select: Select
) -> tuple[list[LoadOption], list[SelectinStep]]:
    """Merge declared load defaults with explicit includes; explicit wins."""
    chosen: dict[str, LoadOption] = {}
    for relationship in spec.relationships:
        if relationship.default_load == "raise":
            continue
        chosen[relationship.name] = LoadOption(
            relationship.relationship, relationship.default_load, ()
        )
    for option in select.includes:
        _relationship_spec(spec, option)
        chosen[option.relationship.python_name] = option

    joined: list[LoadOption] = []
    selectin: list[SelectinStep] = []
    for name, option in chosen.items():
        relationship = spec.relationship(name)
        assert relationship is not None
        strategy = option.strategy
        if relationship.cardinality == "many":
            if strategy == "joined":
                raise ORMError(
                    f"{spec.model_type.__name__}.{name} is a collection; loading it "
                    "with a join would multiply parent rows. Use .selectin()."
                )
            selectin.append(SelectinStep(relationship, option.nested))
        elif strategy == "joined":
            joined.append(option)
        else:
            selectin.append(SelectinStep(relationship, option.nested))
    return joined, selectin


def _conjoin(predicates: tuple[Predicate, ...]) -> Predicate:
    if len(predicates) == 1:
        return predicates[0]
    return BooleanExpr(AND, predicates)


def _direction(item: OrderExpr) -> str:
    if item.direction not in _DIRECTIONS:
        raise ORMError(f"invalid sort direction {item.direction!r}")
    return item.direction


def _render_predicate(
    node: Expression,
    builder: _Builder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
) -> None:
    if isinstance(node, BinaryExpr):
        if node.operator not in _BINARY_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        _render_operand(node.left, builder, alias, joins)
        builder.text(f" {node.operator} ")
        _render_operand(node.right, builder, alias, joins)
        return
    if isinstance(node, InExpr):
        if node.operator not in _IN_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        _render_operand(node.left, builder, alias, joins)
        builder.text(f" {node.operator} (")
        for index, value in enumerate(node.values):
            if index:
                builder.text(", ")
            _render_operand(value, builder, alias, joins)
        builder.text(")")
        return
    if isinstance(node, BooleanExpr):
        if node.operator not in _BOOLEAN_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        builder.text("(")
        for index, operand in enumerate(node.operands):
            if index:
                builder.text(f" {node.operator} ")
            _render_predicate(operand, builder, alias, joins)
        builder.text(")")
        return
    if isinstance(node, UnaryExpr):
        if node.operator not in _UNARY_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        if node.operator == "NOT":
            builder.text("NOT (")
            _render_predicate(node.operand, builder, alias, joins)
            builder.text(")")
            return
        _render_operand(node.operand, builder, alias, joins)
        builder.text(f" {node.operator}")
        return
    raise ORMError(f"cannot render {type(node).__name__} as a predicate")


def _render_operand(
    node: Expression,
    builder: _Builder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
) -> None:
    # Before ColumnExpr: RelatedColumnExpr is a subclass of it.
    if isinstance(node, RelatedColumnExpr):
        builder.text(f"{quote(joins[node.path])}.{quote(node.column.database_name)}")
        return
    if isinstance(node, ColumnExpr):
        builder.text(f"{quote(alias)}.{quote(node.column.database_name)}")
        return
    if isinstance(node, ValueExpr):
        builder.text(builder.bind(node.pg_type.to_wire(node.value), node.pg_type.oid))
        return
    raise ORMError(f"cannot render {type(node).__name__} as an operand")


def _collect_binds_pure(select: Select) -> tuple[tuple[Any, ...], tuple[int, ...]]:
    """Extract bind values in the order the compiler emits placeholders.

    A cache hit skips SQL generation but must still read this query's values,
    so this walk mirrors the rendering order exactly.
    """
    values: list[Any] = []
    oids: list[int] = []
    if select.predicates:
        _walk_values(_conjoin(select.predicates), values, oids)
    if select.limit_ is not None:
        values.append(select.limit_)
        oids.append(_INT8_OID)
    if select.offset_ is not None:
        values.append(select.offset_)
        oids.append(_INT8_OID)
    return tuple(values), tuple(oids)


def _collect_binds_native(select: Select) -> tuple[tuple[Any, ...], tuple[int, ...]]:
    """The native traversal returns the ordered value nodes; encoding to wire
    format stays in this flat Python loop (a per-value call must not cross into
    C). Output is byte-identical to :func:`_collect_binds_pure`."""
    values: list[Any] = []
    oids: list[int] = []
    # A predicate-free read has no tree to walk; skip the C crossing so it is
    # never slower than the pure short-circuit.
    if select.predicates:
        for node in _collect_value_nodes(select):
            pg_type = node.pg_type
            values.append(pg_type.to_wire(node.value))
            oids.append(pg_type.oid)
    if select.limit_ is not None:
        values.append(select.limit_)
        oids.append(_INT8_OID)
    if select.offset_ is not None:
        values.append(select.offset_)
        oids.append(_INT8_OID)
    return tuple(values), tuple(oids)


if _core is not None and hasattr(_core, "orm_collect_values"):
    _collect_value_nodes = _core.orm_collect_values
    _collect_binds = _collect_binds_native
else:
    _collect_binds = _collect_binds_pure


def _walk_values(node: Expression, values: list[Any], oids: list[int]) -> None:
    if isinstance(node, ValueExpr):
        values.append(node.pg_type.to_wire(node.value))
        oids.append(node.pg_type.oid)
        return
    if isinstance(node, BinaryExpr):
        _walk_values(node.left, values, oids)
        _walk_values(node.right, values, oids)
        return
    if isinstance(node, InExpr):
        _walk_values(node.left, values, oids)
        for item in node.values:
            _walk_values(item, values, oids)
        return
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            _walk_values(operand, values, oids)
        return
    if isinstance(node, UnaryExpr):
        _walk_values(node.operand, values, oids)
        return
    if isinstance(node, ColumnExpr):
        return
    raise ORMError(f"cannot extract values from {type(node).__name__}")


# -- shape keys ---------------------------------------------------------------


#: Encoded model names, keyed by the model class. Bounded by the number of
#: declared models, which is bounded by the application.
_MODEL_SHAPES: dict[type, bytes] = {}


def _model_shape(model: type) -> bytes:
    shape = _MODEL_SHAPES.get(model)
    if shape is None:
        shape = _MODEL_SHAPES[model] = model.__qualname__.encode("utf-8")
    return shape


def _shape_expression(node: Expression, out: list[bytes]) -> None:
    if isinstance(node, RelatedColumnExpr):
        out.append(b"j")
        for item in node.path:
            out.append(item.shape_ref)
        out.append(node.column.shape_ref)
        return
    if isinstance(node, ColumnExpr):
        out.append(node.column.shape_ref)
        return
    if isinstance(node, ValueExpr):
        # The value itself is deliberately excluded: only its type shapes SQL.
        out.append(node.pg_type.shape_value)
        return
    if isinstance(node, BinaryExpr):
        out.append(b"b" + node.operator.encode("ascii"))
        _shape_expression(node.left, out)
        _shape_expression(node.right, out)
        return
    if isinstance(node, InExpr):
        # The operand count changes the SQL text, so it belongs in the key.
        out.append(
            b"i" + node.operator.encode("ascii") + str(len(node.values)).encode("ascii")
        )
        _shape_expression(node.left, out)
        for item in node.values:
            _shape_expression(item, out)
        return
    if isinstance(node, BooleanExpr):
        out.append(
            b"l" + node.operator.encode("ascii") + str(len(node.operands)).encode("ascii")
        )
        for operand in node.operands:
            _shape_expression(operand, out)
        return
    if isinstance(node, UnaryExpr):
        out.append(b"u" + node.operator.encode("ascii"))
        _shape_expression(node.operand, out)
        return
    raise ORMError(f"cannot key {type(node).__name__}")


def _shape_loads(options: tuple[LoadOption, ...], out: list[bytes]) -> None:
    for option in options:
        out.append(
            b"L"
            + option.relationship.python_name.encode("utf-8")
            + b":"
            + option.strategy.encode("ascii")
        )
        _shape_loads(option.nested, out)


def _shape_of_pure(registry: Any, select: Select) -> bytes:
    """A cache key covering everything that changes the SQL, and no values.

    Runs on every query, so the pieces that cannot vary -- encoded column names,
    type tags, the model name -- are precomputed and merely appended here.

    The key is the joined bytes, deliberately not a digest of them. Hashing it
    with SHA-256 cost ~370ns per request and bought nothing: this is a dict key,
    not a fingerprint anyone stores or compares across processes, and `dict`
    hashes the bytes itself.
    """
    out: list[bytes] = [registry.fingerprint, _model_shape(select.model)]
    for item in select.projection:
        out.append(item.column.shape_projection)
    out.append(b"|")
    for item in select.predicates:
        _shape_expression(item, out)
    out.append(b"|")
    for item in select.orderings:
        out.append(
            b"o"
            + item.expression.column.shape_ref
            + item.direction.encode("ascii")
        )
    _shape_loads(select.includes, out)
    out.append(b"f" if select.for_update_ else b"-")
    # Only presence matters: limits and offsets are bound, not inlined.
    out.append(b"m" if select.limit_ is not None else b"-")
    out.append(b"n" if select.offset_ is not None else b"-")
    return b"\x1e".join(out)


# The cache key runs on every query. The native builder skips the per-node
# Python recursion frame and writes the key straight into one buffer; it is
# configured with the Expression classes so it can dispatch by exact type, and
# produces byte-identical keys to `_shape_of_pure` (pinned by parity tests).
if _core is not None and hasattr(_core, "orm_shape"):
    _core.orm_shape_configure(
        ColumnExpr,
        RelatedColumnExpr,
        ValueExpr,
        BinaryExpr,
        InExpr,
        BooleanExpr,
        UnaryExpr,
        ORMError,
    )
    shape_of = _core.orm_shape
else:
    shape_of = _shape_of_pure


__all__ = [
    "MAX_BIND_PARAMETERS",
    "MAX_SELECTIN_KEYS",
    "CompiledQuery",
    "JoinedStep",
    "LoadPlan",
    "SelectinStep",
    "compile_select",
    "qualified",
    "quote",
    "shape_of",
]
