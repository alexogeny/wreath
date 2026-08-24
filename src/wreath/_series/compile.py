"""Render one declaration into one PostgreSQL statement.

Everything here reuses the ORM's own predicate machinery rather than growing a
parallel one: `plan_filter_joins` emits the joins a
predicate reaches through, and `render_predicate`
renders the predicate itself. A calculated view's `where()` is therefore the
same filter language a `Select` takes, compiled by the same code — which is
the point, and is why a predicate that works in one works in the other.

The compiler remains the independent source of SQL and placeholder order. An
unsealed `Series` stores that immutable plan in its registry after the first
run, then executes a startup-compiled value program on hits. That removes the
repeated join planning, quoting, and SQL assembly without changing this module
into a second cache owner or weakening the compiler comparison in the tests.
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

#: `timestamptz`. Range bounds bind as this so PostgreSQL compares them to the
#: declared column without an inferred cast.
_TIMESTAMPTZ_OID = 1184
_TEXT_OID = 25
_INT8_OID = 20

#: The finest resolution a PostgreSQL timestamp holds. Subtracting exactly one
#: before truncating is what makes the spine's upper bound exclusive: a range
#: ending precisely on a boundary stops at the previous bucket, and one ending
#: mid-bucket still includes the bucket it stops in. See `_spine`.
_ONE_TICK = "interval '1 microsecond'"

#: The two values the `period` discriminator takes when a view compares. Only
#: ever emitted as literals from here, never taken from a caller.
CURRENT = "current"
PREVIOUS = "previous"

#: `float8`. The extent's edges bind as this so PostgreSQL compares them to a
#: declared latitude or longitude column without an inferred cast.
_FLOAT8_OID = 701


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


def _local(bound: str, zone: str) -> str:
    """A `timestamptz` read on the zone's wall clock, as a naive timestamp."""
    return f"({bound} AT TIME ZONE {zone}::text)"


def _shifted(builder: SqlBuilder, value: Any, zone_name: str, step: str) -> str:
    """`value` moved one comparison period earlier, as a `timestamptz`.

    The shift happens on the *local* wall clock and is converted back, which is
    the whole of why a comparison period is worth compiling rather than
    subtracting in Python. `interval '1 month'` applied to a naive local
    timestamp is calendar arithmetic, so "the same day last month" lands on the
    same day number whatever the month lengths are, and an intervening clock
    change moves the instant rather than the wall time.

    Binds run in the order they appear in the text, so the placeholder numbering
    reads forwards.
    """
    bound = builder.bind(value, _TIMESTAMPTZ_OID)
    inward = builder.bind(zone_name, _TEXT_OID)
    outward = builder.bind(zone_name, _TEXT_OID)
    return f"({_local(bound, inward)} - interval '{step}') AT TIME ZONE {outward}::text"


def _window(
    builder: SqlBuilder, at_sql: str, *, start: Any, end: Any, zone_name: str, step: str | None
) -> str:
    """`at` inside one half-open window — `start <= at < end`.

    Rendered here rather than passed in as an ORM predicate because the spine's
    bounds are rendered here too, from the same two values: a window and a spine
    that disagree by a bucket is the failure this whole statement exists to make
    impossible, and the surest way to keep them agreeing is to give them one
    author. `step` shifts the window into the comparison period.
    """
    if step is None:
        low = builder.bind(start, _TIMESTAMPTZ_OID)
        high = builder.bind(end, _TIMESTAMPTZ_OID)
        return f"({at_sql} >= {low} AND {at_sql} < {high})"
    low = _shifted(builder, start, zone_name, step)
    high = _shifted(builder, end, zone_name, step)
    return f"({at_sql} >= {low} AND {at_sql} < {high})"


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
    cell = getattr(declaration, "cell", None)
    if cell is not None:
        # The spatial axis joins through the same planner as everything else,
        # so a latitude on a to-one relation costs the join it needs and a
        # to-many one is refused with the message the ORM already gives.
        reached.append(cell.lat)
        reached.append(cell.lon)
    for _name, measure in declaration.measures:
        if measure.column is not None:
            reached.append(measure.column)
    clauses: list[str] = []
    aliases = plan_filter_joins(registry, spec, reached, clauses)
    return spec, clauses, aliases


def compile_aggregate(
    registry: Any, declaration: Any, predicates: tuple[Any, ...]
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """`SELECT` the declared measures, grouped by the declared key.

    One row per group, or exactly one row when nothing is grouped. The row
    budget binds as `LIMIT ceiling + 1` so the caller can tell "this is the
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


def compile_cells(
    registry: Any, declaration: Any, predicates: tuple[Any, ...]
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """Every cell of the declared lattice, whether or not anything is in it.

    The spatial twin of `compile_series`, and deliberately the same
    four-part shape: an aggregate grouped by cell index, a spine that generates
    the whole lattice, and a `LEFT JOIN` so an empty cell arrives as a row of
    nulls rather than as an absence the caller has to notice. A heatmap with
    missing cells lies about a gap in exactly the way a line chart with missing
    days does.

    The spine is two `generate_series` calls crossed, because a lattice is a
    product of two dense axes -- the same reason the temporal spine is one.

    **Cell assignment mirrors `Grid.index_of` deliberately.** The window is
    inclusive at both edges, matching `BoundingBox.contains`, and the
    computed index is clamped into range -- so a point sitting exactly on the
    extent's far edge belongs to the edge cell here and in Python both. Two
    spellings of one rule is how they drift apart, so
    `test_sql_cell_assignment_matches_index_of` pins them together against a
    live server.
    """
    spec, joins, aliases = _plan(registry, declaration, predicates)
    cell = declaration.cell
    lattice = cell.grid
    lat_sql = _column_sql(cell.lat, aliases)
    lon_sql = _column_sql(cell.lon, aliases)
    builder = SqlBuilder()

    builder.text(f"WITH {quote('agg')} AS (SELECT ")
    lat_origin = builder.bind(lattice.extent.lat_min, _FLOAT8_OID)
    lat_step = builder.bind(lattice.lat_step, _FLOAT8_OID)
    top_row = builder.bind(lattice.rows - 1, _INT8_OID)
    builder.text(
        f"LEAST(GREATEST(FLOOR(({lat_sql} - {lat_origin}) / {lat_step})::int8, 0), "
        f"{top_row}) AS {quote('cy')}, "
    )
    lon_origin = builder.bind(lattice.extent.lon_min, _FLOAT8_OID)
    lon_step = builder.bind(lattice.lon_step, _FLOAT8_OID)
    top_column = builder.bind(lattice.columns - 1, _INT8_OID)
    builder.text(
        f"LEAST(GREATEST(FLOOR(({lon_sql} - {lon_origin}) / {lon_step})::int8, 0), "
        f"{top_column}) AS {quote('cx')}"
    )
    for index, (_name, measure) in enumerate(declaration.measures):
        builder.text(f", {_measure_sql(measure, aliases)} AS {quote(f'm{index}')}")
    _from_clause(builder, spec, joins)
    _where(builder, predicates, aliases)
    builder.text(" AND " if predicates else " WHERE ")
    builder.text(_extent(builder, lat_sql, lon_sql, lattice.extent))
    builder.text(" GROUP BY 1, 2), ")

    rows_bind = builder.bind(lattice.rows - 1, _INT8_OID)
    columns_bind = builder.bind(lattice.columns - 1, _INT8_OID)
    builder.text(
        f"{quote('spine')} AS (SELECT {quote('gy')}.{quote('cy')} AS {quote('cy')}, "
        f"{quote('gx')}.{quote('cx')} AS {quote('cx')} "
        f"FROM generate_series(0::int8, {rows_bind}) AS {quote('gy')}({quote('cy')}) "
        f"CROSS JOIN generate_series(0::int8, {columns_bind}) "
        f"AS {quote('gx')}({quote('cx')})) "
    )

    builder.text(f"SELECT {quote('s')}.{quote('cy')}, {quote('s')}.{quote('cx')}")
    for index in range(len(declaration.measures)):
        builder.text(f", {quote('a')}.{quote(f'm{index}')}")
    builder.text(
        f" FROM {quote('spine')} AS {quote('s')} LEFT JOIN {quote('agg')} AS {quote('a')} "
        f"ON {quote('a')}.{quote('cy')} = {quote('s')}.{quote('cy')} "
        f"AND {quote('a')}.{quote('cx')} = {quote('s')}.{quote('cx')} "
        f"ORDER BY 1, 2"
    )
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def _extent(builder: SqlBuilder, lat_sql: str, lon_sql: str, extent: Any) -> str:
    """A point inside the declared extent, on the same rule as `contains`.

    Inclusive at both edges rather than half-open. A time axis is half-open
    because instants are dense and a bucket boundary belongs to exactly one
    bucket; an extent is a region a reader drew, and a sighting exactly on its
    northern edge is inside the region they asked about.
    """
    lat_low = builder.bind(extent.lat_min, _FLOAT8_OID)
    lat_high = builder.bind(extent.lat_max, _FLOAT8_OID)
    lon_low = builder.bind(extent.lon_min, _FLOAT8_OID)
    lon_high = builder.bind(extent.lon_max, _FLOAT8_OID)
    return (
        f"({lat_sql} >= {lat_low} AND {lat_sql} <= {lat_high} "
        f"AND {lon_sql} >= {lon_low} AND {lon_sql} <= {lon_high})"
    )


def compile_series(
    registry: Any,
    declaration: Any,
    predicates: tuple[Any, ...],
    *,
    start: Any,
    end: Any,
    zone_name: str,
    compare: Any = None,
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """The whole series — spine, aggregate, and top-N fold — as one statement.

    Four parts, in the order they are rendered:

    `survivors`
        the grouping values that make the cut, ranked over the *whole* range
        rather than per bucket, so a series does not appear and vanish as the
        reader scrolls. Ties break on the key itself, which is what makes the
        survivor set stable between two runs of the same query.
    `agg`
        one aggregate per bucket per surviving key, with everything else folded
        into a single remainder. The fold is applied *before* aggregation, so
        the remainder's average is a true average of the tail's rows rather than
        an average of averages — the trap that makes a folded mean meaningless.
    `spine`
        every bucket in the range, whether or not anything happened in it,
        generated on the local wall clock and converted back afterwards.
    the outer select
        the spine LEFT JOINed to the aggregate, so an empty bucket arrives as a
        row with nulls rather than as an absence the caller has to notice.

    `compare` adds a second period. It stays *one* statement — two statements
    are how the periods end up misaligned by a bucket — so the spine gains a
    second arm over the shifted range, every row carries a `period`
    discriminator, and both arms join the same aggregate. The survivors are
    still ranked over the primary period alone: "the top seven paddocks this
    month, and what those seven did last month" keeps a legend that means one
    thing, where ranking across both would let a series that has since gone to
    zero hold a slot.
    """
    spec, joins, aliases = _plan(registry, declaration, predicates)
    at_sql = _column_sql(declaration.at, aliases)
    trunc = declaration.bucket.trunc
    builder = SqlBuilder()
    grouped = declaration.group is not None
    step = None if compare is None else compare.step

    if grouped:
        key_sql = _column_sql(declaration.group, aliases)
        builder.text(f"WITH {quote('survivors')} AS (SELECT {key_sql} AS {quote('g')}")
        _from_clause(builder, spec, joins)
        _where(builder, predicates, aliases)
        builder.text(" AND " if predicates else " WHERE ")
        builder.text(_window(builder, at_sql, start=start, end=end, zone_name=zone_name, step=None))
        rank = _measure_sql(declaration.measures[0][1], aliases)
        builder.text(f" GROUP BY 1 ORDER BY {rank} DESC NULLS LAST, 1 ASC")
        builder.text(f" LIMIT {builder.bind(declaration.top, _INT8_OID)}), ")
    else:
        builder.text("WITH ")

    builder.text(f"{quote('agg')} AS (SELECT ")
    zone_bind = builder.bind(zone_name, _TEXT_OID)
    builder.text(f"date_trunc('{trunc}', {at_sql} AT TIME ZONE {zone_bind}::text) AS {quote('b')}")
    if compare is not None:
        # The two windows are disjoint -- `compare()` refuses a shift shorter
        # than the range -- so which period a row belongs to is decided by one
        # comparison against the primary start rather than by testing both.
        edge = builder.bind(start, _TIMESTAMPTZ_OID)
        builder.text(
            f", CASE WHEN {at_sql} >= {edge} THEN '{CURRENT}' ELSE '{PREVIOUS}' END"
            f"::text AS {quote('period')}"
        )
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
    builder.text(" AND " if predicates else " WHERE ")
    primary = _window(builder, at_sql, start=start, end=end, zone_name=zone_name, step=None)
    if step is None:
        builder.text(primary)
    else:
        shifted = _window(
            builder, at_sql, start=start, end=end, zone_name=zone_name, step=step
        )
        builder.text(f"({primary} OR {shifted})")
    columns = 1 + (compare is not None) + 2 * grouped
    builder.text(f" GROUP BY {', '.join(str(n) for n in range(1, columns + 1))})")

    _spine(
        builder, declaration, start=start, end=end, trunc=trunc,
        zone_name=zone_name, step=step,
    )

    builder.text(f"SELECT {quote('s')}.{quote('b')} AT TIME ZONE ")
    builder.text(f"{builder.bind(zone_name, _TEXT_OID)}::text AS {quote('bucket')}")
    if compare is not None:
        builder.text(f", {quote('s')}.{quote('period')}")
    if grouped:
        builder.text(f", {quote('a')}.{quote('g')}, {quote('a')}.{quote('other')}")
    for index, _measure in enumerate(declaration.measures):
        builder.text(f", {quote('a')}.{quote(f'm{index}')}")
    builder.text(
        f" FROM {quote('spine')} AS {quote('s')} LEFT JOIN {quote('agg')} AS {quote('a')} "
        f"ON {quote('a')}.{quote('b')} = {quote('s')}.{quote('b')}"
    )
    if compare is not None:
        # Joining on the discriminator as well as the bucket is what keeps the
        # two periods from bleeding into each other: without it a bucket that
        # exists in both arms would pick up both aggregates.
        builder.text(f" AND {quote('a')}.{quote('period')} = {quote('s')}.{quote('period')}")
    # The surviving keys before the fold, so "other" is last in the payload the
    # way it is last in a legend. The period leads when there is one, so each
    # period's buckets arrive as one contiguous run.
    order = ["1"]
    if compare is not None:
        order = ["2", "1"]
    if grouped:
        base = 2 + (compare is not None)
        order += [str(base + 1), str(base)]
    builder.text(f" ORDER BY {', '.join(order)}")
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def compile_events(
    registry: Any,
    spec_model: type,
    at: Any,
    label: Any,
    predicates: tuple[Any, ...],
    *,
    start: Any,
    end: Any,
    zone_name: str,
    trunc: str,
    limit: int,
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    """Markers inside the range, each knowing its exact instant and its bucket.

    Alignment is the requirement; one round trip is a nice-to-have that must not
    be bought with a bad type. A tagged `UNION ALL` of buckets and events would
    force both into one row shape with half the columns null in every row, and a
    discriminator the client has to switch on — a worse envelope, worse generated
    types, and a worse decode, in exchange for a round trip the driver may
    already be pipelining.

    So this is a second statement, and alignment is structural instead: `trunc`
    and `zone_name` arrive from the same declaration that rendered the series,
    and the window from the same `Range`. There is no
    second copy of either to drift from.

    The bucket travels *with* the event rather than being recomputed on the
    client, which is what lets a marker sit at its true x-position while still
    knowing which column it belongs over.
    """
    spec = registry.spec_for(spec_model)
    clauses: list[str] = []
    aliases = plan_filter_joins(registry, spec, [*predicates, at, label], clauses)
    at_sql = _column_sql(at, aliases)
    builder = SqlBuilder()
    builder.text(f"SELECT {at_sql} AS {quote('at')}, ")
    inward = builder.bind(zone_name, _TEXT_OID)
    outward = builder.bind(zone_name, _TEXT_OID)
    builder.text(
        f"date_trunc('{trunc}', {_local(at_sql, inward)}) AT TIME ZONE "
        f"{outward}::text AS {quote('bucket')}, "
        f"{_column_sql(label, aliases)} AS {quote('label')}"
    )
    _from_clause(builder, spec, clauses)
    _where(builder, predicates, aliases)
    builder.text(" AND " if predicates else " WHERE ")
    builder.text(_window(builder, at_sql, start=start, end=end, zone_name=zone_name, step=None))
    # Ordered by the instant so markers arrive in the order they happened, and
    # bound one past the ceiling so the caller can tell a full answer from a
    # truncated one and refuse rather than draw a partial annotation layer.
    builder.text(f" ORDER BY 1 LIMIT {builder.bind(limit + 1, _INT8_OID)}")
    return builder.sql(), tuple(builder.values), tuple(builder.oids)


def _spine(
    builder: SqlBuilder,
    declaration: Any,
    *,
    start: Any,
    end: Any,
    trunc: str,
    zone_name: str,
    step: str | None = None,
) -> None:
    """Every bucket in the range, generated on the local wall clock.

    The order is the whole trick, and it is the reason this is worth owning.
    `AT TIME ZONE` on a `timestamptz` yields a *naive* local timestamp;
    `generate_series` stepping over naive timestamps advances by a calendar
    day, which is what a reader means by "daily". Generating over
    `timestamptz` instead steps by exactly 24 hours, so the day a clock
    changes is an hour out and every boundary after it is wrong. Converting back
    at the end yields the correct instant for each local midnight, including the
    ones 23 and 25 hours apart.

    The upper bound subtracts one microsecond before truncating, which is how
    the half-open range is honoured in the one place it is written: a range
    ending exactly on a boundary excludes the bucket starting there.

    `step` adds the comparison arm. Its bounds are the same two values shifted
    on the *local* clock before truncating, so a comparison month is a calendar
    month and the two arms can legitimately be different lengths — February
    against March is 28 buckets against 31, and saying so is more honest than
    padding one of them.
    """
    width = declaration.bucket.step
    if step is None:
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
            f"interval '{width}') AS {quote('b')}) "
        )
        return
    builder.text(f", {quote('spine')} AS (")
    _spine_arm(
        builder, start=start, end=end, trunc=trunc, zone_name=zone_name,
        width=width, shift=None, period=CURRENT,
    )
    builder.text(" UNION ALL ")
    _spine_arm(
        builder, start=start, end=end, trunc=trunc, zone_name=zone_name,
        width=width, shift=step, period=PREVIOUS,
    )
    builder.text(") ")


def _spine_arm(
    builder: SqlBuilder,
    *,
    start: Any,
    end: Any,
    trunc: str,
    zone_name: str,
    width: str,
    shift: str | None,
    period: str,
) -> None:
    """One period's buckets, tagged with which period they are."""
    lower = f"{_local(builder.bind(start, _TIMESTAMPTZ_OID), builder.bind(zone_name, _TEXT_OID))}"
    if shift is not None:
        lower = f"({lower} - interval '{shift}')"
    upper = f"{_local(builder.bind(end, _TIMESTAMPTZ_OID), builder.bind(zone_name, _TEXT_OID))}"
    upper = f"({upper} - {_ONE_TICK})"
    if shift is not None:
        upper = f"({upper} - interval '{shift}')"
    builder.text(
        f"SELECT {quote('g')}.{quote('b')}, '{period}'::text AS {quote('period')} "
        f"FROM generate_series("
        f"date_trunc('{trunc}', {lower}), "
        f"date_trunc('{trunc}', {upper}), "
        f"interval '{width}') AS {quote('g')}({quote('b')})"
    )
