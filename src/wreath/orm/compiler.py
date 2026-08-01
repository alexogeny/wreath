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

import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from .._native import _core
from .errors import DeclarationError, ORMError
from .expressions import (
    AND,
    ASC,
    BOUNDED_ORDER_OPERATORS,
    DESC,
    BinaryExpr,
    BooleanExpr,
    ColumnExpr,
    Expression,
    InExpr,
    InSubqueryExpr,
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
_BINARY_OPERATORS = frozenset({
    "=", "<>", "<", "<=", ">", ">=", "LIKE", "ILIKE",
    # jsonb / array operators; "= ANY"/"= ALL" render specially (see below).
    "@>", "<@", "?", "?|", "?&", "#>>", "#>", "&&", "= ANY", "= ALL",
    # pgvector distances. These yield a number rather than a boolean, so they
    # reach SQL as an ORDER BY key or as the left side of a comparison; `where()`
    # refuses one on its own. The last two are the `bit` distances, which differ
    # only in the column type they are declared over.
    "<->", "<=>", "<#>", "<+>", "<~>", "<%>",
    # Full-text search. Each token names its tsquery parser as well as its
    # operation, so the allowlist still decides every byte reaching SQL.
    "@@ websearch_to_tsquery", "@@ to_tsquery",
    "ts_rank websearch_to_tsquery", "ts_rank to_tsquery",
    # Geospatial. `point` and `box` are two-argument function calls that build
    # the right operand of a `<@`; `geo_distance` yields metres, so like the
    # pgvector distances it reaches SQL as an ORDER BY key or the left side of a
    # comparison and never as a predicate on its own.
    "point", "box", "geo_distance",
    # PostGIS. `geo_knn` is the same two characters pgvector's `<->` renders and
    # a different token on purpose -- see `expressions.GEO_KNN`. The other two
    # are function calls that yield a boolean, so they are predicates and only
    # predicates.
    "geo_knn", "geo_dwithin", "geo_covers",
})
#: Two-argument SQL function calls rendered from both operands, in the shape
#: `name(left, right)`. `ts_rank` predates this table and keeps its own path
#: because it also has to build a tsquery from its right operand.
_GEO_CALLS = {"point": "point", "box": "box"}
#: Tokens that evaluate to a number rather than to a boolean, so they render as
#: an ORDER BY key or the left side of a comparison and never as a predicate.
_GEO_VALUES = frozenset({"geo_distance", "geo_knn"})
#: PostGIS predicates. Each is a function call the renderer builds from the
#: node's own operands, so each has a render path rather than a table entry.
_GEO_PREDICATES = frozenset({"geo_dwithin", "geo_covers"})
#: Operators that render `left = ANY(right)` / `left = ALL(right)` rather
#: than the ordinary infix form, with the array column on the right.
_ARRAY_QUANTIFIERS = {"= ANY": "ANY", "= ALL": "ALL"}
#: Full-text operators, split into the form they render and the tsquery parser
#: they build their right operand with. `@@` is infix with a function call on
#: the right; `ts_rank` wraps *both* operands in a call, which is why it needs a
#: render path of its own rather than an entry in the infix table.
_TEXT_SEARCH = {
    "@@ websearch_to_tsquery": ("@@", "websearch_to_tsquery"),
    "@@ to_tsquery": ("@@", "to_tsquery"),
    "ts_rank websearch_to_tsquery": ("ts_rank", "websearch_to_tsquery"),
    "ts_rank to_tsquery": ("ts_rank", "to_tsquery"),
}
#: A text-search configuration name. The configuration is a declaration-time
#: constant off the column's own type, exactly as its name is, and reaches SQL
#: as a quoted literal rather than a bind -- so it is re-checked here as well as
#: at declaration, because this is the function that writes it out. Matched with
#: `fullmatch`, never `match`: `$` also matches immediately before a trailing
#: newline, and `^...$` let a config carrying one through to the quoted literal.
_TS_CONFIG = re.compile(r"[a-z_][a-z0-9_]*")
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


#: Not `frozen`. A frozen dataclass builds every field through
#: `object.__setattr__`, and at eight fields that cost 0.65us of an 8.7us ORM
#: read -- the single largest item inside `compile_select`. Nothing hashes a
#: `CompiledQuery`, stores one, or compares two: `compile_select` builds it and
#: the session reads it within the same call. `slots=True` still refuses a
#: field nobody declared, which is the mistake that actually happens here;
#: freezing was buying the rest at request-path prices.
@dataclass(slots=True)
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
    bind_program: Callable[[Select], tuple[Any, ...]]
    result_model: ModelSpec
    selected_columns: tuple[ColumnSpec, ...]
    load_plan: LoadPlan
    projected_columns: tuple[ColumnSpec, ...]
    #: Native direct-hydration plan for this shape, compiled on first use.
    #: `False` records that this shape cannot use the direct path, which is
    #: distinct from "not compiled yet".
    hydrate_plan: Any = None


# Test-only, the same contract as the session's `_probes`: counts how many write
# statements were compiled from scratch rather than served from the registry
# cache. The invariant is that this tracks the distinct write *shapes* in a
# flush -- (model, which columns participate) -- not the number of instances
# carrying each shape. `None` in production keeps this to one predictable global
# load per compile.
_write_sql_builds: list[int] | None = None


@contextmanager
def _count_write_sql_builds() -> Iterator[list[int]]:
    """Count write statements compiled from scratch inside the block."""
    global _write_sql_builds
    counter = [0]
    previous, _write_sql_builds = _write_sql_builds, counter
    try:
        yield counter
    finally:
        _write_sql_builds = previous


@dataclass(frozen=True, slots=True)
class WritePlan:
    """A compiled INSERT, UPDATE, or DELETE for one (model, column set).

    Writes have shapes exactly as reads do: what varies between two inserts of
    the same model is *which* columns are loaded, and that selects the statement
    text. What does not vary is the text itself, so it is compiled once per shape
    and held in the same registry cache as read plans -- one budget, one
    eviction policy, one lock.

    `columns` are the values to bind in placeholder order; `key_columns` are
    the primary-key values bound after them (empty for INSERT). `returning` is
    what the statement asks the database to send back.
    """

    sql: str
    columns: tuple[ColumnSpec, ...]
    key_columns: tuple[ColumnSpec, ...] = ()
    returning: tuple[ColumnSpec, ...] = ()


def _write_shape_key(registry: Any, spec: ModelSpec, op: bytes, mask: int) -> bytes:
    """A cache key over everything that changes a write statement.

    The mask is positional over `spec.columns`, so it identifies the
    participating set exactly; the model name and registry fingerprint keep two
    models (or two registries) from colliding.
    """
    width = (mask.bit_length() + 7) // 8
    return b"".join((
        registry.fingerprint,
        b"w",
        op,
        _model_shape(spec.model_type),
        b"|",
        mask.to_bytes(width, "big"),
    ))


def compile_insert(registry: Any, spec: ModelSpec, mask: int) -> WritePlan:
    """The INSERT for the columns selected by `mask`, compiled once per shape.

    Columns outside the mask are what the database fills in -- server defaults
    and anything the caller left unloaded -- so they are exactly the RETURNING
    list. Splitting on the mask also removes the `item not in columns` scan the
    previous implementation ran per column, which made a wide table's INSERT
    quadratic in its own column count.
    """
    cached = registry.cached_plan(key := _write_shape_key(registry, spec, b"i", mask))
    if cached is not None:
        return cached

    if _write_sql_builds is not None:
        _write_sql_builds[0] += 1
    columns = tuple(
        item for position, item in enumerate(spec.columns) if mask & (1 << position)
    )
    returning = tuple(
        item for position, item in enumerate(spec.columns) if not mask & (1 << position)
    )
    names = ", ".join(quote(item.database_name) for item in columns)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    sql = f"INSERT INTO {qualified(spec)}"
    sql += f" ({names}) VALUES ({placeholders})" if columns else " DEFAULT VALUES"
    if returning:
        sql += " RETURNING " + ", ".join(
            quote(item.database_name) for item in returning
        )
    return registry.store_plan(
        key, WritePlan(sql=sql, columns=columns, returning=returning)
    )


def compile_update(registry: Any, spec: ModelSpec, mask: int) -> WritePlan:
    """The UPDATE for the dirty columns selected by `mask`."""
    cached = registry.cached_plan(key := _write_shape_key(registry, spec, b"u", mask))
    if cached is not None:
        return cached

    if _write_sql_builds is not None:
        _write_sql_builds[0] += 1
    columns = tuple(
        item for position, item in enumerate(spec.columns) if mask & (1 << position)
    )
    assignments = ", ".join(
        f"{quote(item.database_name)} = ${index}"
        for index, item in enumerate(columns, start=1)
    )
    predicate = _key_predicate_sql(spec, len(columns) + 1)
    sql = f"UPDATE {qualified(spec)} SET {assignments} WHERE {predicate}"
    return registry.store_plan(
        key,
        WritePlan(sql=sql, columns=columns, key_columns=tuple(spec.primary_key)),
    )


def compile_delete(registry: Any, spec: ModelSpec) -> WritePlan:
    """The DELETE for one model; its only shape is its primary key."""
    cached = registry.cached_plan(key := _write_shape_key(registry, spec, b"d", 0))
    if cached is not None:
        return cached

    if _write_sql_builds is not None:
        _write_sql_builds[0] += 1
    sql = f"DELETE FROM {qualified(spec)} WHERE {_key_predicate_sql(spec, 1)}"
    return registry.store_plan(
        key, WritePlan(sql=sql, columns=(), key_columns=tuple(spec.primary_key))
    )


def _key_predicate_sql(spec: ModelSpec, start: int) -> str:
    """`pk = $n [AND ...]`, with placeholders numbered from `start`."""
    return " AND ".join(
        f"{quote(item.database_name)} = ${start + offset}"
        for offset, item in enumerate(spec.primary_key)
    )


def quote(identifier: str) -> str:
    """Quote one registry-validated identifier.

    Identifiers reach this function only after `validate_identifier`, so the
    embedded-quote escape is belt-and-braces rather than the actual defense.
    """
    return '"' + identifier.replace('"', '""') + '"'


def qualified(spec: ModelSpec) -> str:
    if spec.sql_namespace == "tenant_search_path":
        return quote(spec.table)
    return f"{quote(spec.schema)}.{quote(spec.table)}"


class SqlBuilder:
    """Accumulates SQL text and bind values for one statement.

    Public because `wreath._series` renders its own statements against
    the same predicate machinery. Placeholder numbering is positional, so a
    caller must emit text and binds in the order they appear in the SQL.
    """

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


def _append_bind_paths(
    node: Expression,
    path: tuple[str | int, ...],
    program: list[tuple[str | int, ...]],
) -> None:
    if isinstance(node, ValueExpr):
        program.append(path)
        return
    if isinstance(node, BinaryExpr):
        _append_bind_paths(node.left, (*path, "left"), program)
        _append_bind_paths(node.right, (*path, "right"), program)
        return
    if isinstance(node, InExpr):
        _append_bind_paths(node.left, (*path, "left"), program)
        for index, item in enumerate(node.values):
            _append_bind_paths(item, (*path, "values", index), program)
        return
    if isinstance(node, InSubqueryExpr):
        # Left first, then the subquery's own predicates in their render order:
        # the outer statement numbers placeholders positionally, and the
        # subquery's WHERE is emitted after the left operand.
        _append_bind_paths(node.left, (*path, "left"), program)
        for index, predicate in enumerate(node.select.predicates):
            _append_bind_paths(predicate, (*path, "select", "predicates", index), program)
        return
    if isinstance(node, BooleanExpr):
        for index, operand in enumerate(node.operands):
            _append_bind_paths(operand, (*path, "operands", index), program)
        return
    if isinstance(node, UnaryExpr):
        _append_bind_paths(node.operand, (*path, "operand"), program)
        return
    if isinstance(node, ColumnExpr):
        return
    raise ORMError(f"cannot compile bind extraction for {type(node).__name__}")


def _append_declared_values(
    node: Expression,
    placeholder: type,
    program: list[tuple[str, Any, Any]],
) -> None:
    """Record runtime parameters and fixed literals in SQL bind order."""
    if isinstance(node, placeholder):
        declared: Any = node
        program.append(("parameter", declared.pg_type, declared.name))
        return
    if isinstance(node, ValueExpr):
        program.append(("literal", node.pg_type, node.value))
        return
    if isinstance(node, BinaryExpr):
        _append_declared_values(node.left, placeholder, program)
        _append_declared_values(node.right, placeholder, program)
        return
    if isinstance(node, InExpr):
        _append_declared_values(node.left, placeholder, program)
        for item in node.values:
            _append_declared_values(item, placeholder, program)
        return
    if isinstance(node, InSubqueryExpr):
        _append_declared_values(node.left, placeholder, program)
        for predicate in node.select.predicates:
            _append_declared_values(predicate, placeholder, program)
        return
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            _append_declared_values(operand, placeholder, program)
        return
    if isinstance(node, UnaryExpr):
        _append_declared_values(node.operand, placeholder, program)
        return
    if isinstance(node, ColumnExpr):
        return
    raise ORMError(f"cannot compile declared binds for {type(node).__name__}")


def compile_declared_values(
    select: Select, placeholder: type
) -> Callable[[dict[str, Any]], tuple[Any, ...]]:
    """Compile direct parameter-to-wire extraction for a declared query.

    Unlike the ordinary cached-plan binder, this program does not need a newly
    rebound `Select`: fixed literals come from the declaration and named
    placeholders read the call's value mapping directly. Parameter names are
    identifiers validated by `Param`; they enter generated source only through
    `repr`, while types and literal values stay in the closed namespace.
    """
    program: list[tuple[str, Any, Any]] = []
    for predicate in select.predicates:
        _append_declared_values(predicate, placeholder, program)
    if not select.plain_orderings:
        # After the predicates and before the limit, matching the order the
        # renderer emits placeholders in.
        for item in select.orderings:
            _append_declared_values(item.expression, placeholder, program)

    types: list[Any] = []
    literals: list[Any] = []
    lines = ["def extract(values):"]
    expressions: list[str] = []
    for index, (kind, pg_type, value) in enumerate(program):
        type_index = len(types)
        types.append(pg_type)
        if kind == "parameter":
            if not isinstance(value, str) or not value.isidentifier():
                raise ValueError(
                    f"refusing to generate a binder for parameter {value!r}"
                )
            lines.extend(
                (
                    "    try:",
                    f"        value_{index} = _types[{type_index}].coerce(values[{value!r}])",
                    "    except (TypeError, ValueError, OverflowError) as error:",
                    f"        raise type(error)(\"parameter {value!r}: \" + str(error)) from error",
                )
            )
            expressions.append(
                f"_types[{type_index}].to_wire(value_{index})"
            )
        else:
            literal_index = len(literals)
            literals.append(value)
            expressions.append(
                f"_types[{type_index}].to_wire(_literals[{literal_index}])"
            )
    if select.limit_ is not None:
        expressions.append(repr(select.limit_))
    if select.offset_ is not None:
        expressions.append(repr(select.offset_))
    body = ", ".join(expressions)
    if len(expressions) == 1:
        body += ","
    lines.append(f"    return ({body})")
    namespace = {"_types": tuple(types), "_literals": tuple(literals)}
    exec(  # noqa: S102 -- names are identifier-checked and values stay closed over
        "\n".join(lines), namespace
    )
    return cast(Callable[[dict[str, Any]], tuple[Any, ...]], namespace["extract"])



def _generated_names(body: str) -> list[str]:
    """The bare names a generated extractor body refers to.

    Split on the punctuation the builder emits, so anything that is not a plain
    attribute, index, or keyword shows up as a non-identifier fragment.
    """
    return [
        fragment
        for fragment in re.split(r"[.,()\[\]\s]+", body)
        if fragment and not fragment.isdigit()
    ]


def _compile_bind_program(select: Select) -> Callable[[Select], tuple[Any, ...]]:
    """Generate one fixed attribute program for a query shape.

    Paths contain only compiler-selected attribute names and integer indexes;
    no SQL, identifier, or user value enters the generated source. The resulting
    function uses attribute/index bytecodes on cache hits instead of repeatedly
    classifying the expression tree or calling `getattr` per path component.
    """
    paths: list[tuple[str | int, ...]] = []
    for index, predicate in enumerate(select.predicates):
        _append_bind_paths(predicate, ("predicates", index), paths)
    if not select.plain_orderings:
        # After the predicates and before the limit, matching the order the
        # renderer emits placeholders in.
        for index, item in enumerate(select.orderings):
            _append_bind_paths(
                item.expression, ("orderings", index, "expression"), paths
            )

    expressions: list[str] = []
    for path in paths:
        expression = "select"
        for step in path:
            expression += f"[{step}]" if isinstance(step, int) else f".{step}"
        expressions.append(f"{expression}.pg_type.to_wire({expression}.value)")
    if select.limit_ is not None:
        expressions.append("select.limit_")
    if select.offset_ is not None:
        expressions.append("select.offset_")

    body = ", ".join(expressions)
    if len(expressions) == 1:
        body += ","
    namespace: dict[str, Any] = {}
    # Every fragment reaching this body is an attribute name off a compiled
    # projection, i.e. developer-declared. That was true and unstated, which is
    # exactly the shape AGENTS.md asks generated code to document -- and an
    # assertion is cheaper than the argument, because it fails at the moment the
    # precondition stops holding rather than at whatever this evaluates to.
    for fragment in _generated_names(body):
        if not fragment.isidentifier():
            raise ValueError(
                f"refusing to generate an extractor from {fragment!r}: only "
                "declared attribute names may reach the generated body"
            )
    exec(  # noqa: S102 - every fragment is guarded to isidentifier() directly above
        f"def extract(select):\n    return ({body})", {}, namespace
    )
    return namespace["extract"]


def check_predicate_columns(model: type, node: Expression) -> None:
    """Raise if a predicate names a column of some other model.

    `Select.where` cannot check this, because a predicate is built before it
    meets a query. Nothing needs it per request either -- a handler's predicate
    is written next to the query it filters. A *declared* query is different:
    `wreath.queries` writes the predicate at class-definition time and runs it
    much later, so a mistyped model would surface as a broken statement on a
    request instead of at import. This is the walk that moves it back.

    Relationship traversals are checked only at their first hop; the registry
    owns the rest of the path, and it is not available this early.
    """
    if isinstance(node, RelatedColumnExpr):
        owner = getattr(node.path[0], "owner", None) if node.path else None
        if owner is not None and owner is not model:
            raise DeclarationError(
                f"{getattr(owner, '__name__', '?')}.{node.path[0].python_name} "
                f"is not a relationship of {model.__name__}"
            )
        return
    if isinstance(node, ColumnExpr):
        if node.column.owner is not model:
            raise DeclarationError(
                f"{getattr(node.column.owner, '__name__', '?')}."
                f"{node.column.python_name} is not a column of {model.__name__}"
            )
        return
    if isinstance(node, ValueExpr):
        return
    if isinstance(node, BinaryExpr):
        check_predicate_columns(model, node.left)
        check_predicate_columns(model, node.right)
        return
    if isinstance(node, InExpr):
        check_predicate_columns(model, node.left)
        for item in node.values:
            check_predicate_columns(model, item)
        return
    if isinstance(node, InSubqueryExpr):
        # Only the left operand belongs to this model. The subquery's predicates
        # name *its* model's columns, and checking them here would reject every
        # correct subquery.
        check_predicate_columns(model, node.left)
        for predicate in node.select.predicates:
            check_predicate_columns(node.select.model, predicate)
        return
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            check_predicate_columns(model, operand)
        return
    if isinstance(node, UnaryExpr):
        check_predicate_columns(model, node.operand)


def compile_rebind(
    node: Expression, placeholder: type, found: list[Any]
) -> Callable[[Any], Any] | None:
    """Compile a substitution program for one predicate holding placeholders.

    A declared query (see `wreath.queries`) fixes its tree once and varies
    only the values in it, so the traversal that finds those positions can run
    at declaration time instead of per call. Returns a function that rebuilds
    the predicate from a mapping of parameter values, or `None` when the
    predicate holds no placeholders at all and can be reused as it stands.
    Every placeholder encountered is appended to `found`, in the order it
    binds, so the caller can name its parameters without a second walk.

    This lives here rather than in `wreath.queries` because the shape of an
    expression tree is this module's knowledge: it has to stay in step with
    `_append_bind_paths` and `_shape_expression`, and the three are only
    reviewable together. A `placeholder` node's `bind` method owns what a
    parameter *means*; this function owns only where one may appear.
    """
    if isinstance(node, placeholder):
        found.append(node)
        # `placeholder` is a runtime argument, so no checker can see that the
        # nodes it selects have a `bind`; that contract belongs to the caller.
        bound: Any = node
        return bound.bind
    if isinstance(node, BinaryExpr):
        left = compile_rebind(node.left, placeholder, found)
        right = compile_rebind(node.right, placeholder, found)
        if left is None and right is None:
            return None
        operator, original_left, original_right = node.operator, node.left, node.right

        def rebind_binary(values: Any) -> Any:
            return BinaryExpr(
                operator,
                original_left if left is None else left(values),
                original_right if right is None else right(values),
            )

        return rebind_binary
    if isinstance(node, InExpr):
        left = compile_rebind(node.left, placeholder, found)
        items = tuple(compile_rebind(item, placeholder, found) for item in node.values)
        if left is None and not any(items):
            return None
        operator, original_left, originals = node.operator, node.left, node.values

        def rebind_in(values: Any) -> Any:
            operands: Any = tuple(
                original if program is None else program(values)
                for original, program in zip(originals, items, strict=True)
            )
            return InExpr(operator, original_left if left is None else left(values), operands)

        return rebind_in
    if isinstance(node, InSubqueryExpr):
        left = compile_rebind(node.left, placeholder, found)
        inner = tuple(
            compile_rebind(predicate, placeholder, found)
            for predicate in node.select.predicates
        )
        if left is None and not any(inner):
            return None
        operator, original_left = node.operator, node.left
        original_select, originals = node.select, node.select.predicates

        def rebind_in_subquery(values: Any) -> Any:
            predicates = tuple(
                original if program is None else program(values)
                for original, program in zip(originals, inner, strict=True)
            )
            return InSubqueryExpr(
                operator,
                original_left if left is None else left(values),
                original_select._replace(predicates=predicates),
            )

        return rebind_in_subquery
    if isinstance(node, BooleanExpr):
        programs = tuple(
            compile_rebind(operand, placeholder, found) for operand in node.operands
        )
        if not any(programs):
            return None
        operator, originals = node.operator, node.operands

        def rebind_boolean(values: Any) -> Any:
            return BooleanExpr(
                operator,
                tuple(
                    original if program is None else program(values)
                    for original, program in zip(originals, programs, strict=True)
                ),
            )

        return rebind_boolean
    if isinstance(node, UnaryExpr):
        program = compile_rebind(node.operand, placeholder, found)
        if program is None:
            return None
        operator = node.operator

        def rebind_unary(values: Any) -> Any:
            return UnaryExpr(operator, program(values))

        return rebind_unary
    if isinstance(node, (ColumnExpr, ValueExpr)):
        return None
    raise ORMError(f"cannot bind parameters inside {type(node).__name__}")


def compile_select(registry: Any, select: Select) -> CompiledQuery:
    """Compile `select` against `registry`, using its bounded plan cache."""
    spec = registry.spec_for(select.model)
    shape_key = shape_of(registry, select)
    plan = registry.cached_plan(shape_key)
    if plan is None:
        plan = registry.store_plan(shape_key, _build_plan(registry, select, spec))
    values = plan.bind_program(select)
    return _bind_cached_plan(plan, shape_key, values)


def _bind_cached_plan(
    plan: _CachedPlan, shape_key: bytes, values: tuple[Any, ...]
) -> CompiledQuery:
    """Attach this execution's values to an already selected immutable plan."""
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


def compile_count(registry: Any, select: Select) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """Compile `SELECT COUNT(*)` for the rows `select` matches.

    Reuses the filter-join and predicate machinery but drops projection, load
    joins, ordering, paging, and row locking -- none of them change how many
    parent rows match. Filter joins are always to-one (the compiler rejects
    to-many filter joins), so `COUNT(*)` over the joined-and-filtered rows
    equals the parent-row count; no subquery wrapper is needed.

    Unlike `compile_select` this is not plan-cached: it is called at most
    once per page request, not per row, so rendering fresh -- which lets the
    bound values be captured directly, without the cached bind program -- is
    both simpler and cheap enough. Returns `(sql, values, oids)`.
    """
    spec = registry.spec_for(select.model)
    counting = select._replace(orderings=(), limit_=None, offset_=None, includes=())
    builder = SqlBuilder()
    clauses: list[str] = []
    filter_aliases = _plan_filter_joins(registry, spec, counting, clauses)
    builder.text(f"SELECT COUNT(*) FROM {qualified(spec)} AS {quote('t0')}")
    for clause in clauses:
        builder.text(clause)
    if counting.predicates:
        builder.text(" WHERE ")
        render_predicate(
            conjoin(counting.predicates), builder, "t0", filter_aliases, registry=registry
        )
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def _build_plan(registry: Any, select: Select, spec: ModelSpec) -> _CachedPlan:
    requested = _projection(spec, select)
    projected = requested
    columns = _with_primary_key(spec, requested)
    joined, selectin = _resolve_loads(registry, spec, select)

    builder = SqlBuilder()
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
        render_predicate(
            conjoin(select.predicates), builder, "t0", filter_aliases, registry=registry
        )
    if select.orderings:
        if select.limit_ is None and any(
            isinstance(item.expression, BinaryExpr)
            and item.expression.operator in BOUNDED_ORDER_OPERATORS
            for item in select.orderings
        ):
            # An ordered proximity search with no ceiling reads and sorts every
            # row, and the index cannot help: `ORDER BY ... LIMIT n` is the only
            # shape it answers. Refusing here rather than returning a correct
            # answer slowly is the same call `.within()` makes by ANDing the box
            # on, and the same one hybrid search makes at declaration time.
            #
            # A *token* test, never an operator test. Tier 2's KNN renders the
            # same `<->` pgvector does, and an unbounded `ORDER BY embedding
            # <-> $1` has always been allowed -- so the two have to stay
            # distinguishable here, which is why `geo_knn` is a token of its own.
            raise DeclarationError(
                "ordering by .nearest() needs a limit; an unbounded proximity "
                "search sorts the whole table and no index can answer it"
            )
        builder.text(" ORDER BY ")
        if select.plain_orderings:
            # `plain_orderings` is exactly the statement that every expression
            # here is a ColumnExpr; `Select` computes it once, at construction.
            builder.text(
                ", ".join(
                    f"{quote('t0')}."
                    f"{quote(cast(ColumnExpr, item.expression).column.database_name)} "
                    f"{_direction(item)}"
                    for item in select.orderings
                )
            )
        else:
            # A distance ordering carries a bound value, so it goes through the
            # operand renderer rather than a join of column names -- and it must
            # render *here*, between the WHERE and the LIMIT, because the
            # builder numbers placeholders in the order they are emitted.
            for index, item in enumerate(select.orderings):
                if index:
                    builder.text(", ")
                _render_operand(item.expression, builder, "t0", filter_aliases)
                builder.text(f" {_direction(item)}")
    if select.limit_ is not None:
        builder.text(f" LIMIT {builder.bind(select.limit_, _INT8_OID)}")
    if select.offset_ is not None:
        builder.text(f" OFFSET {builder.bind(select.offset_, _INT8_OID)}")
    if select.for_update_:
        builder.text(" FOR UPDATE")

    return _CachedPlan(
        sql=builder.sql(),
        bind_oids=tuple(builder.oids),
        bind_program=_compile_bind_program(select),
        result_model=spec,
        selected_columns=tuple(selected),
        load_plan=LoadPlan(
            columns=tuple(columns), joined=joined_steps, selectin=tuple(selectin)
        ),
        projected_columns=tuple(projected),
    )


def _filter_paths(
    node: Expression, out: list[tuple[Any, ...]], seen: set[tuple[Any, ...]]
) -> None:
    """Collect each distinct relationship path a predicate reaches through.

    `seen` deduplicates in O(1); a plain `not in out` list scan would be
    O(paths^2) across a predicate tree touching many related columns. First-seen
    order is preserved (the join-emission order downstream depends on it).
    """
    if isinstance(node, RelatedColumnExpr):
        if node.path not in seen:
            seen.add(node.path)
            out.append(node.path)
        return
    if isinstance(node, BinaryExpr):
        _filter_paths(node.left, out, seen)
        _filter_paths(node.right, out, seen)
        return
    if isinstance(node, InExpr):
        _filter_paths(node.left, out, seen)
        for item in node.values:
            _filter_paths(item, out, seen)
        return
    if isinstance(node, InSubqueryExpr):
        # Left only. A relationship reached inside the subquery would need a
        # join on the *subquery's* FROM, not the outer one -- which is why
        # `_check_subquery` refuses one outright rather than planning it here.
        _filter_paths(node.left, out, seen)
        return
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            _filter_paths(operand, out, seen)
        return
    if isinstance(node, UnaryExpr):
        _filter_paths(node.operand, out, seen)


def _plan_filter_joins(
    registry: Any, spec: ModelSpec, select: Select, clauses: list[str]
) -> dict[tuple[Any, ...], str]:
    """Emit an INNER JOIN per relationship path this query's predicates reach."""
    return plan_filter_joins(registry, spec, select.predicates, clauses)


def plan_filter_joins(
    registry: Any,
    spec: ModelSpec,
    expressions: Iterable[Expression],
    clauses: list[str],
) -> dict[tuple[Any, ...], str]:
    """Emit an INNER JOIN per relationship path `expressions` reach through.

    These joins constrain rows and select nothing: filtering is not loading, so
    a filtered relation stays unloaded unless the query also `.include()`s it.
    INNER rather than LEFT because a parent with no matching child cannot
    satisfy a predicate on the child's column, and INNER lets PostgreSQL reorder
    the join.

    Takes expressions rather than a `Select` because a calculated view groups
    by a column as well as filtering by one, and a `GROUP BY` on a related
    column needs the same join a predicate on it would get. Aliases are returned
    keyed by relationship path, which is what `render_predicate` reads.
    """
    paths: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for expression in expressions:
        _filter_paths(expression, paths, seen)
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


def conjoin(predicates: tuple[Predicate, ...]) -> Predicate:
    if len(predicates) == 1:
        return predicates[0]
    return BooleanExpr(AND, predicates)


def _direction(item: OrderExpr) -> str:
    if item.direction not in _DIRECTIONS:
        raise ORMError(f"invalid sort direction {item.direction!r}")
    return item.direction


def render_predicate(
    node: Expression,
    builder: SqlBuilder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
    *,
    registry: Any = None,
) -> None:
    """Render one predicate into `builder`, numbering placeholders positionally.

    Args:
        node: The predicate tree to render.
        builder: Accumulates the SQL text and the bound values, in order.
        alias: The table alias the unqualified columns belong to.
        joins: Alias per relationship path, for columns reached through one.
        registry: Needed only to render an `IN (SELECT ...)` subquery, which has
            to resolve its own model to a table. Callers that never build one
            may leave it None; a subquery reached without it raises rather than
            rendering something half-qualified.
    """
    if isinstance(node, BinaryExpr):
        if node.operator not in _BINARY_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        quantifier = _ARRAY_QUANTIFIERS.get(node.operator)
        if quantifier is not None:
            # `value = ANY(column)` -- the bound value is on the left, the
            # array column on the right, wrapped in the quantifier function.
            _render_operand(node.left, builder, alias, joins)
            builder.text(f" = {quantifier}(")
            _render_operand(node.right, builder, alias, joins)
            builder.text(")")
            return
        text_search = _TEXT_SEARCH.get(node.operator)
        if text_search is not None:
            if text_search[0] != "@@":
                raise ORMError(
                    f"cannot render {node.operator!r} as a predicate: it yields a "
                    "relevance score, not a boolean"
                )
            _render_text_search(node, builder, alias, joins, *text_search)
            return
        if node.operator in _GEO_CALLS or node.operator in _GEO_VALUES:
            # Same refusal as `ts_rank`, for the same reason: these yield a
            # point, a box, or metres. PostgreSQL would reject the query later
            # with a message about the argument of WHERE rather than about the
            # line that wrote it.
            raise ORMError(
                f"cannot render {node.operator!r} as a predicate: it yields a "
                "value, not a boolean; use .within() to filter or .nearest() "
                "as an ORDER BY key"
            )
        if node.operator == "geo_dwithin":
            _render_geo_dwithin(node, builder, alias, joins)
            return
        if node.operator == "geo_covers":
            _render_geo_covers(node, builder, alias, joins)
            return
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
    if isinstance(node, InSubqueryExpr):
        if node.operator not in _IN_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        if registry is None:
            raise ORMError(
                "cannot render IN (SELECT ...) here: this caller renders predicates "
                "without a registry, so the subquery's table cannot be resolved"
            )
        _render_operand(node.left, builder, alias, joins)
        builder.text(f" {node.operator} (")
        _render_subquery(node.select, builder, alias, registry)
        builder.text(")")
        return
    if isinstance(node, BooleanExpr):
        if node.operator not in _BOOLEAN_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        builder.text("(")
        for index, operand in enumerate(node.operands):
            if index:
                builder.text(f" {node.operator} ")
            render_predicate(operand, builder, alias, joins, registry=registry)
        builder.text(")")
        return
    if isinstance(node, UnaryExpr):
        if node.operator not in _UNARY_OPERATORS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        if node.operator == "NOT":
            builder.text("NOT (")
            render_predicate(node.operand, builder, alias, joins, registry=registry)
            builder.text(")")
            return
        _render_operand(node.operand, builder, alias, joins)
        builder.text(f" {node.operator}")
        return
    raise ORMError(f"cannot render {type(node).__name__} as a predicate")


def _render_subquery(
    select: Any, builder: SqlBuilder, outer_alias: str, registry: Any
) -> None:
    """Render a validated one-column subquery inside an enclosing statement.

    The alias is derived from the enclosing one (`t0` -> `t0s`) rather than
    drawn from the outer statement's `t1`, `t2`, ... sequence: those are planned
    before rendering starts and a subquery is discovered during it, so sharing
    the counter would mean threading mutable state through the renderer for no
    gain. Deriving guarantees uniqueness at every depth, and a nested subquery
    reads as `t0ss` -- which says where it came from.

    The subquery's shape is already validated by `_check_subquery`, so there is
    no ordering, paging, locking, eager load, or relationship join to emit; a
    projection, a FROM, and an optional WHERE is the whole statement.
    """
    spec = registry.spec_for(select.model)
    alias = f"{outer_alias}s"
    column = select.projection[0].column
    builder.text(f"SELECT {quote(alias)}.{quote(column.database_name)}")
    builder.text(f" FROM {qualified(spec)} AS {quote(alias)}")
    if select.predicates:
        builder.text(" WHERE ")
        render_predicate(conjoin(select.predicates), builder, alias, {}, registry=registry)


def _render_operand(
    node: Expression,
    builder: SqlBuilder,
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
    # A nested binary node appears as an operand for jsonb path extraction:
    # `(data #>> $1) = $2`. Parenthesize so operator precedence is explicit.
    if isinstance(node, BinaryExpr):
        if node.operator not in _BINARY_OPERATORS or node.operator in _ARRAY_QUANTIFIERS:
            raise ORMError(f"invalid SQL operator {node.operator!r}")
        text_search = _TEXT_SEARCH.get(node.operator)
        if text_search is not None:
            # A rank is an ORDER BY key and the left side of a threshold, so it
            # reaches SQL here far more often than as a predicate. It carries its
            # own parentheses (it is a function call), which is why it takes this
            # branch rather than the infix one below.
            _render_text_search(node, builder, alias, joins, *text_search)
            return
        call = _GEO_CALLS.get(node.operator)
        if call is not None:
            # `point(lon, lat)` / `box(point(...), point(...))`. Both operands
            # are rendered, so the binds inside them are collected by the same
            # walk that collects every other bind.
            builder.text(f"{call}(")
            _render_operand(node.left, builder, alias, joins)
            builder.text(", ")
            _render_operand(node.right, builder, alias, joins)
            builder.text(")")
            return
        if node.operator == "geo_distance":
            _render_geo_distance(node, builder, alias, joins)
            return
        if node.operator in _GEO_PREDICATES:
            # The mirror of the refusal above, and it has to be written rather
            # than left to the infix fallthrough: that would render
            # `("t0"."at" geo_dwithin ...)`, which is not SQL, from an operator
            # the allowlist admits.
            raise ORMError(
                f"cannot render {node.operator!r} as a value: it yields a boolean, "
                "so it belongs in where() rather than in an ORDER BY or a "
                "comparison"
            )
        if node.operator == "geo_knn":
            # `<->`, PostGIS's KNN operator. Parenthesised like every other
            # nested operand, which the planner reads through: the live plan is
            # `Index Scan ... Order By: (at <-> ...)` either way.
            builder.text("(")
            _render_operand(node.left, builder, alias, joins)
            builder.text(" <-> ")
            _render_operand(node.right, builder, alias, joins)
            builder.text(")")
            return
        builder.text("(")
        _render_operand(node.left, builder, alias, joins)
        builder.text(f" {node.operator} ")
        _render_operand(node.right, builder, alias, joins)
        builder.text(")")
        return
    raise ORMError(f"cannot render {type(node).__name__} as an operand")


def _render_geo_distance(
    node: BinaryExpr,
    builder: SqlBuilder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
) -> None:
    """Render great-circle metres between a `point` column and a `point(...)`.

    The haversine, in SQL, over the same sphere radius the Python and C twins
    use -- `wreath.geospatial.EARTH_RADIUS_M`. Three implementations of one
    formula is two too many, so the radius is written once here and asserted
    equal to the module's constant by a test rather than being retyped.

    PostgreSQL indexes a `point` as `p[0]` = x = longitude and `p[1]` = y =
    latitude. Getting that pair backwards is the single most likely defect in
    this function and it fails silently on a symmetric test case, so the live
    tests use a centre whose latitude and longitude differ in sign.
    """
    from ..geospatial import EARTH_RADIUS_M

    if not isinstance(node.left, ColumnExpr) or not isinstance(node.right, BinaryExpr):
        raise ORMError("a geospatial distance needs a Point column and a centre")
    inner = node.right.right
    if not isinstance(inner, BinaryExpr):
        raise ORMError("a geospatial distance needs a Point column and a centre")
    column: ColumnExpr = node.left
    # The centre's three leaves, in the order they are emitted below. Each is
    # rendered exactly once, so the placeholder count matches what the bind
    # program collected -- see the comment where this tree is built.
    lat_delta = node.right.left
    lat_cos = inner.left
    lon_delta = inner.right
    name = column.column.database_name
    joined = joins.get(getattr(column, "path", ()), alias) if joins else alias

    def _lat(part: str) -> None:
        builder.text(f"({quote(joined)}.{quote(name)})[{part}]")

    builder.text(f"(2 * {EARTH_RADIUS_M!r} * asin(sqrt(power(sin(radians(")
    _render_operand(lat_delta, builder, alias, joins)
    builder.text(" - ")
    _lat("1")
    builder.text(") / 2), 2) + cos(radians(")
    _lat("1")
    builder.text(")) * cos(radians(")
    _render_operand(lat_cos, builder, alias, joins)
    builder.text(")) * power(sin(radians(")
    _render_operand(lon_delta, builder, alias, joins)
    builder.text(" - ")
    _lat("0")
    builder.text(") / 2), 2))))")


def _render_geo_dwithin(
    node: BinaryExpr,
    builder: SqlBuilder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
) -> None:
    """Render `ST_DWithin(column, centre, metres)` — tier 2's `within()`.

    Three arguments under a two-operand node, so the centre and the radius
    share the right-hand child. The order they are emitted in is the order
    `_append_bind_paths` walks them, which is what keeps the placeholder
    numbering and the bind program in step; a renderer that emitted the radius
    first would prepare the statement with the two transposed and PostgreSQL
    would report a type error rather than a wrong answer.

    PostGIS adds the `&&` index condition itself and filters the exact
    spheroidal test over what survives -- the shape `within()` hand-builds for
    tier 1. Building one here as well would be a second spelling of the same
    coarse filter, and two spellings of one rule is how they drift apart.
    """
    if not isinstance(node.left, ColumnExpr) or not isinstance(node.right, BinaryExpr):
        raise ORMError(
            "a geography proximity test needs a Geography column, a centre and a radius"
        )
    builder.text("ST_DWithin(")
    _render_operand(node.left, builder, alias, joins)
    builder.text(", ")
    _render_operand(node.right.left, builder, alias, joins)
    builder.text(", ")
    _render_operand(node.right.right, builder, alias, joins)
    builder.text(")")


def _render_geo_covers(
    node: BinaryExpr,
    builder: SqlBuilder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
) -> None:
    """Render `ST_Covers(ST_GeogFromText($1), column)` — tier 2's containment.

    The region comes first, because that is the argument order `ST_Covers`
    takes: the container, then the thing contained. `ST_Contains` is not an
    option and not a preference -- **`ST_Contains(geography, geography)` does
    not exist**, so a rendering that reached for it would fail at the database
    rather than at declaration.

    The operands are emitted in the opposite order to the walk that collects
    binds, which is safe here for exactly one reason: the left operand is a
    column and carries no bound value, so the statement has one placeholder
    however it is walked. The guard below is what keeps that true.
    """
    if not isinstance(node.left, ColumnExpr):
        raise ORMError("a geography containment needs a Geography column")
    builder.text("ST_Covers(ST_GeogFromText(")
    _render_operand(node.right, builder, alias, joins)
    builder.text("), ")
    _render_operand(node.left, builder, alias, joins)
    builder.text(")")


def _render_text_search(
    node: BinaryExpr,
    builder: SqlBuilder,
    alias: str,
    joins: dict[tuple[Any, ...], str],
    form: str,
    parser: str,
) -> None:
    """Render `@@ <parser>(...)` or `ts_rank(column, <parser>(...))`.

    The tsquery is built from the *column's* configuration rather than from
    anything the caller passed: a query analysed under a different configuration
    than the stored vector matches nothing, and that reads as missing data rather
    than as a mistake. Both operands still bind normally, so the search text
    never reaches SQL text.
    """
    config = _text_search_config(node)
    if form == "ts_rank":
        builder.text("ts_rank(")
        _render_operand(node.left, builder, alias, joins)
        builder.text(f", {parser}('{config}', ")
        _render_operand(node.right, builder, alias, joins)
        builder.text("))")
        return
    _render_operand(node.left, builder, alias, joins)
    builder.text(f" @@ {parser}('{config}', ")
    _render_operand(node.right, builder, alias, joins)
    builder.text(")")


def _text_search_config(node: BinaryExpr) -> str:
    """The text-search configuration the left operand's column declares."""
    column = getattr(node.left, "column", None)
    config = getattr(getattr(column, "pg_type", None), "config", None)
    if not isinstance(config, str) or not _TS_CONFIG.fullmatch(config):
        raise ORMError(
            f"cannot render {node.operator!r}: its left operand is not a TsVector "
            "column naming a text-search configuration"
        )
    return config


def _collect_binds_pure(select: Select) -> tuple[tuple[Any, ...], tuple[int, ...]]:
    """Extract bind values in the order the compiler emits placeholders.

    A cache hit skips SQL generation but must still read this query's values,
    so this walk mirrors the rendering order exactly.
    """
    values: list[Any] = []
    oids: list[int] = []
    if select.predicates:
        _walk_values(conjoin(select.predicates), values, oids)
    if not select.plain_orderings:
        for item in select.orderings:
            _walk_values(item.expression, values, oids)
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
    C). Output is byte-identical to `_collect_binds_pure`."""
    values: list[Any] = []
    oids: list[int] = []
    # A predicate-free read has no tree to walk; skip the C crossing so it is
    # never slower than the pure short-circuit.
    if select.predicates:
        for node in _collect_value_nodes(select):
            pg_type = node.pg_type
            values.append(pg_type.to_wire(node.value))
            oids.append(pg_type.oid)
    # The native collector walks predicates only; an ordering that binds a value
    # is rare enough that teaching C about it would cost more than this guard.
    if not select.plain_orderings:
        for item in select.orderings:
            _walk_values(item.expression, values, oids)
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
    if isinstance(node, InSubqueryExpr):
        _walk_values(node.left, values, oids)
        for predicate in node.select.predicates:
            _walk_values(predicate, values, oids)
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
    if isinstance(node, InSubqueryExpr):
        # Everything that changes the subquery's SQL text, and nothing that
        # changes only its bound values: the model, the projected column, and
        # the shape of each predicate. Two subqueries over the same table
        # filtering the same columns share a plan; two over different tables
        # must not, and this is the only place that distinguishes them.
        out.append(
            b"q"
            + node.operator.encode("ascii")
            + str(len(node.select.predicates)).encode("ascii")
        )
        _shape_expression(node.left, out)
        out.append(_model_shape(node.select.model))
        out.append(node.select.projection[0].column.shape_projection)
        for predicate in node.select.predicates:
            _shape_expression(predicate, out)
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
        expression = item.expression
        if isinstance(expression, ColumnExpr):
            # One element, byte-for-byte what `orm_shape.c` emits for the same
            # ordering. The parity test pins it.
            out.append(
                b"o" + expression.column.shape_ref + item.direction.encode("ascii")
            )
            continue
        # A distance ordering keys through the whole expression: the operator
        # and the bound type both change the SQL text, and two searches over one
        # column with different distances are two plans. Only the pure keyer
        # sees these -- `shape_of` routes them here deliberately.
        out.append(b"o")
        _shape_expression(expression, out)
        out.append(item.direction.encode("ascii"))
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
        InSubqueryExpr,
        BooleanExpr,
        UnaryExpr,
        ORMError,
    )
    _shape_of_native = _core.orm_shape

    def shape_of(registry: Any, select: Select) -> bytes:
        """The native key, falling back to the pure one for a node C cannot key.

        `orm_shape_configure` hands the builder a fixed set of expression classes
        and it dispatches by *exact* type, so a node added since the extension
        was built raises `ORMError("cannot key ...")` rather than quietly
        producing a key that ignores it. That refusal is what makes this fallback
        safe: `_shape_of_pure` raises the identical error for anything genuinely
        unkeyable, so catching it either recovers a node the C does not know yet
        or re-raises exactly what the caller would have seen.

        **No shipped node takes this path.** `InSubqueryExpr` used to, at the
        cost of one raised-and-caught exception per compile of a subquery-bearing
        query; `orm_shape.c` now keys it directly. The fallback stays because its
        value is prospective -- it is what lets a node be added to Python without
        the extension being rebuilt in the same change, and without a stale
        extension silently keying two different queries the same way. A
        rebuild-order hazard that degrades to "slower" is worth keeping; the
        alternative degrades to a wrong plan.

        An ordering that is not a bare column takes the pure path *before* the
        call rather than through this fallback: the C keyer reads
        `ordering.expression.column`, which a distance node does not have, and
        that surfaces as an `AttributeError` -- not the `ORMError` this recovery
        is built on. Deciding it from a flag the `Select` already computed is one
        attribute read, and it keeps the contract above as narrow as it claims.
        """
        if not select.plain_orderings:
            return _shape_of_pure(registry, select)
        try:
            return _shape_of_native(registry, select)
        except ORMError:
            return _shape_of_pure(registry, select)
else:
    shape_of = _shape_of_pure


__all__ = [
    "MAX_BIND_PARAMETERS",
    "MAX_SELECTIN_KEYS",
    "CompiledQuery",
    "JoinedStep",
    "LoadPlan",
    "SelectinStep",
    "SqlBuilder",
    "check_predicate_columns",
    "compile_count",
    "compile_declared_values",
    "compile_rebind",
    "compile_select",
    "conjoin",
    "plan_filter_joins",
    "qualified",
    "quote",
    "render_predicate",
    "shape_of",
]
