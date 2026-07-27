"""Render one declaration into one PostgreSQL statement.

Everything here reuses the ORM's own predicate machinery rather than growing a
parallel one: :func:`~wreath.orm.compiler.plan_filter_joins` emits the joins a
predicate reaches through, and :func:`~wreath.orm.compiler.render_predicate`
renders the predicate itself. A calculated view's ``where()`` is therefore the
same filter language a ``Select`` takes, compiled by the same code — which is
the point, and is why a predicate that works in one works in the other.

Following ``compile_count`` rather than ``compile_select``, none of this is
plan-cached. A declaration runs once per chart request, not once per row, so
rendering fresh is simpler and lets bound values be captured directly. Claiming
a cache would pay here would need a measurement nobody has taken.
"""

from __future__ import annotations

from typing import Any

from ..orm.compiler import (
    SqlBuilder,
    conjoin,
    plan_filter_joins,
    qualified,
    quote,
    render_predicate,
)
from ..orm.expressions import ColumnExpr, RelatedColumnExpr

#: ``timestamptz``. Range bounds bind as this so PostgreSQL compares them to the
#: declared column without an inferred cast.
_TIMESTAMPTZ_OID = 1184
_TEXT_OID = 25
_INT8_OID = 20

#: The finest resolution a PostgreSQL timestamp holds. Subtracting exactly one
#: before truncating is what makes the spine's upper bound exclusive: a range
#: ending precisely on a boundary stops at the previous bucket, and one ending
#: mid-bucket still includes the bucket it stops in. See ``_spine``.
_ONE_TICK = "interval '1 microsecond'"


def _column_sql(expression: Any, aliases: dict[tuple[Any, ...], str]) -> str:
    """A column reference, qualified by whichever alias holds it.

    Deliberately narrow: a grouping key is a column, never an expression, so
    anything else is a declaration error caught before rendering. Column
    references never bind a value, which is what lets the same key be rendered
    at several points in one statement without disturbing the placeholder
    numbering.
    """
    # Before ColumnExpr: RelatedColumnExpr subclasses it.
    if isinstance(expression, RelatedColumnExpr):
        return f"{quote(aliases[expression.path])}.{quote(expression.column.database_name)}"
    if isinstance(expression, ColumnExpr):
        return f"{quote('t0')}.{quote(expression.column.database_name)}"
    raise TypeError(f"cannot render {type(expression).__name__} as a column")


def _measure_sql(measure: Any, aliases: dict[tuple[Any, ...], str]) -> str:
    if measure.column is None:
        return "COUNT(*)"
    return f"{measure.function}({_column_sql(measure.column, aliases)})"


def _from_clause(builder: SqlBuilder, spec: Any, joins: list[str]) -> None:
    builder.text(f" FROM {qualified(spec)} AS {quote('t0')}")
    for clause in joins:
        builder.text(clause)


def _where(
    builder: SqlBuilder,
    predicates: tuple[Any, ...],
    aliases: dict[tuple[Any, ...], str],
) -> None:
    if not predicates:
        return
    builder.text(" WHERE ")
    render_predicate(conjoin(predicates), builder, "t0", aliases)


def _plan(
    registry: Any, declaration: Any, predicates: tuple[Any, ...]
) -> tuple[Any, list[str], dict[tuple[Any, ...], str]]:
    """The model spec, its join clauses, and the alias map they produced.

    The grouping key joins through the same planner as the predicates, so
    grouping by a to-one relation's column costs the join it needs and grouping
    by a to-many one is refused with the message the ORM already gives.
    """
    spec = registry.spec_for(declaration.model)
    reached: list[Any] = list(predicates)
    if declaration.group is not None:
        reached.append(declaration.group)
    for _name, measure in declaration.measures:
        if measure.column is not None:
            reached.append(measure.column)
    clauses: list[str] = []
    aliases = plan_filter_joins(registry, spec, reached, clauses)
    return spec, clauses, aliases


def compile_aggregate(
    registry: Any, declaration: Any, predicates: tuple[Any, ...]
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """``SELECT`` the declared measures, grouped by the declared key.

    One row per group, or exactly one row when nothing is grouped. The row
    budget binds as ``LIMIT ceiling + 1`` so the caller can tell "this is the
    whole answer" from "there was more", and refuse rather than quietly draw a
    truncated chart (§6).
    """
    spec, joins, aliases = _plan(registry, declaration, predicates)
    builder = SqlBuilder()
    builder.text("SELECT ")
    if declaration.group is not None:
        builder.text(f"{_column_sql(declaration.group, aliases)} AS {quote('g')}, ")
    builder.text(
        ", ".join(
            f"{_measure_sql(measure, aliases)} AS {quote(f'm{index}')}"
            for index, (_name, measure) in enumerate(declaration.measures)
        )
    )
    _from_clause(builder, spec, joins)
    _where(builder, predicates, aliases)
    if declaration.group is not None:
        builder.text(" GROUP BY 1")
        # Ordered by the first measure so a bar chart arrives sorted, then by
        # the key so ties do not reshuffle between two runs of the same query.
        builder.text(" ORDER BY 2 DESC NULLS LAST, 1 ASC")
        builder.text(f" LIMIT {builder.bind(declaration.limit + 1, _INT8_OID)}")
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def compile_series(
    registry: Any,
    declaration: Any,
    predicates: tuple[Any, ...],
    *,
    start: Any,
    end: Any,
    zone_name: str,
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """The whole series — spine, aggregate, and top-N fold — as one statement.

    Four parts, in the order they are rendered:

    ``survivors``
        the grouping values that make the cut, ranked over the *whole* range
        rather than per bucket, so a series does not appear and vanish as the
        reader scrolls. Ties break on the key itself, which is what makes the
        survivor set stable between two runs of the same query.
    ``agg``
        one aggregate per bucket per surviving key, with everything else folded
        into a single remainder. The fold is applied *before* aggregation, so
        the remainder's average is a true average of the tail's rows rather than
        an average of averages — the trap that makes a folded mean meaningless.
    ``spine``
        every bucket in the range, whether or not anything happened in it,
        generated on the local wall clock and converted back afterwards.
    the outer select
        the spine LEFT JOINed to the aggregate, so an empty bucket arrives as a
        row with nulls rather than as an absence the caller has to notice.
    """
    spec, joins, aliases = _plan(registry, declaration, predicates)
    at_sql = _column_sql(declaration.at, aliases)
    trunc = declaration.bucket.trunc
    builder = SqlBuilder()
    grouped = declaration.group is not None

    if grouped:
        key_sql = _column_sql(declaration.group, aliases)
        builder.text(f"WITH {quote('survivors')} AS (SELECT {key_sql} AS {quote('g')}")
        _from_clause(builder, spec, joins)
        _where(builder, predicates, aliases)
        rank = _measure_sql(declaration.measures[0][1], aliases)
        builder.text(f" GROUP BY 1 ORDER BY {rank} DESC NULLS LAST, 1 ASC")
        builder.text(f" LIMIT {builder.bind(declaration.top, _INT8_OID)}), ")
    else:
        builder.text("WITH ")

    builder.text(f"{quote('agg')} AS (SELECT ")
    zone_bind = builder.bind(zone_name, _TEXT_OID)
    builder.text(f"date_trunc('{trunc}', {at_sql} AT TIME ZONE {zone_bind}::text) AS {quote('b')}")
    if grouped:
        # `hit` is a marker column rather than an `IN` test because a grouping
        # value may itself be NULL. `IS NOT DISTINCT FROM` matches NULL to NULL,
        # so a surviving NULL key stays its own series while the folded tail --
        # which also carries a NULL key -- is told apart by `other`.
        builder.text(
            f", CASE WHEN {quote('sv')}.{quote('hit')} THEN {key_sql} END AS {quote('g')}"
            f", ({quote('sv')}.{quote('hit')} IS NOT TRUE) AS {quote('other')}"
        )
    for index, (_name, measure) in enumerate(declaration.measures):
        builder.text(f", {_measure_sql(measure, aliases)} AS {quote(f'm{index}')}")
    _from_clause(builder, spec, joins)
    if grouped:
        builder.text(
            f" LEFT JOIN (SELECT {quote('g')}, true AS {quote('hit')} "
            f"FROM {quote('survivors')}) AS {quote('sv')} "
            f"ON {quote('sv')}.{quote('g')} IS NOT DISTINCT FROM {key_sql}"
        )
    _where(builder, predicates, aliases)
    builder.text(" GROUP BY 1, 2, 3)" if grouped else " GROUP BY 1)")

    _spine(builder, declaration, start=start, end=end, trunc=trunc, zone_name=zone_name)

    builder.text(f"SELECT {quote('s')}.{quote('b')} AT TIME ZONE ")
    builder.text(f"{builder.bind(zone_name, _TEXT_OID)}::text AS {quote('bucket')}")
    if grouped:
        builder.text(f", {quote('a')}.{quote('g')}, {quote('a')}.{quote('other')}")
    for index, _measure in enumerate(declaration.measures):
        builder.text(f", {quote('a')}.{quote(f'm{index}')}")
    builder.text(
        f" FROM {quote('spine')} AS {quote('s')} LEFT JOIN {quote('agg')} AS {quote('a')} "
        f"ON {quote('a')}.{quote('b')} = {quote('s')}.{quote('b')}"
    )
    # The surviving keys before the fold, so "other" is last in the payload the
    # way it is last in a legend.
    builder.text(" ORDER BY 1, 3, 2" if grouped else " ORDER BY 1")
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def _spine(
    builder: SqlBuilder,
    declaration: Any,
    *,
    start: Any,
    end: Any,
    trunc: str,
    zone_name: str,
) -> None:
    """Every bucket in the range, generated on the local wall clock.

    The order is the whole trick, and it is the reason this is worth owning.
    ``AT TIME ZONE`` on a ``timestamptz`` yields a *naive* local timestamp;
    ``generate_series`` stepping over naive timestamps advances by a calendar
    day, which is what a reader means by "daily". Generating over
    ``timestamptz`` instead steps by exactly 24 hours, so the day a clock
    changes is an hour out and every boundary after it is wrong. Converting back
    at the end yields the correct instant for each local midnight, including the
    ones 23 and 25 hours apart.

    The upper bound subtracts one microsecond before truncating, which is how
    the half-open range is honoured in the one place it is written: a range
    ending exactly on a boundary excludes the bucket starting there.
    """
    step = declaration.bucket.step
    # Bound in the order they appear in the text: placeholder numbering is
    # positional, and a statement whose $n run backwards is correct but reads
    # like a bug to the next person diffing it against the plan.
    from_bind = builder.bind(start, _TIMESTAMPTZ_OID)
    from_zone = builder.bind(zone_name, _TEXT_OID)
    to_bind = builder.bind(end, _TIMESTAMPTZ_OID)
    to_zone = builder.bind(zone_name, _TEXT_OID)
    builder.text(
        f", {quote('spine')} AS (SELECT generate_series("
        f"date_trunc('{trunc}', {from_bind} AT TIME ZONE {from_zone}::text), "
        f"date_trunc('{trunc}', ({to_bind} AT TIME ZONE {to_zone}::text) - {_ONE_TICK}), "
        f"interval '{step}') AS {quote('b')}) "
    )
