"""Chart data as a declaration, not a hand-rolled query.

Almost every web application ends up with the same paragraph of code: fetch a
few thousand rows, loop over them in Python, bucket them by day, and hand the
result to a chart. It is written that way not because anyone wants to, but
because the query layer could not say *count these per day, in the reader's
timezone, for the last quarter, with the quiet days showing as zero* — so the
rows came into the process and the arithmetic happened in a loop.

That loop is where the interesting bugs live. It buckets by UTC and the days
come out shifted for everyone east of Greenwich. It skips the empty days, so
the line joins across a gap that was really a zero. It fills the empty days
with zero for *every* measure, so an average collapses to the floor on the
quietest day of the week. And it re-reads the whole history on every page load,
because there is nowhere to put the answer.

A declaration says the same thing once, and the database does the work::

    from wreath.series import Series, count, sum_
    from wreath.temporal import Day, zone

    activity = (
        Series(Trek, at=Trek.started_at, bucket=Day, stored_in=zone("Pacific/Auckland"))
            .where(Trek.herd_id == Param("herd"))
            .measure(started=count(), distance=sum_(Trek.distance_km, unit="km"))
            .by(Trek.paddock_id, top=7)
    )

    @app.get("/herds/{herd_id}/activity")
    @cached(ttl=300, invalidate_on=activity.sources)
    async def herd_activity(request, herd_id: int, session: Session):
        return await activity.run(
            session, herd=herd_id, range=Range(start, end), zone=request.zone,
        )

Four things follow from declaring rather than building, and each of them is a
bug that no longer has anywhere to happen:

* **Every bucket in the range exists**, because the range generates a spine and
  the aggregate is joined onto it. An empty Tuesday is a zero, not an absence.
* **Fill is per measure.** A count of nothing is zero; an average of nothing is
  undefined, and stays ``None`` so the renderer draws a gap.
* **Every series has a stable key**, taken from the grouping value rather than
  from its position, so a filter change cannot repaint the legend.
* **Mistakes move to import time.** Bucketing by a column that holds no time,
  averaging text, grouping by a collection — those fail when the declaration is
  written, not on the request that first draws an empty chart.

**What this is not.** It takes one source model, declared measures, and a
bounded result. If you cannot name the model, name each measure and its unit,
or state the largest the result can get, then what you have is a query rather
than a chart, and ``session.raw()`` is the honest tool for it. This module
refuses to become a way to express everything, which is SQL with worse syntax.

Reference: :doc:`/reference/series`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._series.compile import compile_aggregate, compile_series
from ._series.envelope import aggregate_rows, fill, series_rows
from .orm.compiler import check_predicate_columns, compile_rebind
from .orm.errors import DeclarationError
from .orm.expressions import ColumnExpr, Predicate, RelatedColumnExpr
from .orm.types import (
    Date,
    Float32,
    Float64,
    Int16,
    Int32,
    Int64,
    Timestamp,
    TimestampTz,
)

# `Param` builds this node in place of a value. It is the marker
# `compile_rebind` looks for, and reaching it through the private name is a
# small wart: `wreath.queries` should give it a public one, and this import
# becomes a plain one when it does.
from .queries import _Placeholder
from .temporal import Bucket, Instant

__all__ = [
    "Aggregate",
    "AggregateResult",
    "AggregateRow",
    "Measure",
    "Range",
    "Series",
    "SeriesData",
    "SeriesError",
    "SeriesResult",
    "avg",
    "count",
    "max_",
    "min_",
    "sum_",
]


class SeriesError(DeclarationError):
    """A declaration cannot mean what it says, or its result will not fit.

    A ``DeclarationError`` because that is what the ORM already raises when a
    query is malformed at the point it is written, and a calculated view is a
    query. Cardinality refusals raise it too: a result too large to draw is a
    fact about the declaration, even when only the run discovers it.
    """


#: Columns a bucket can be cut from. A bucket is a span of wall-clock time, so
#: the column has to hold one; anything else has no boundary to truncate to.
_TEMPORAL_TYPES = frozenset({TimestampTz.name, Timestamp.name, Date.name})

#: Columns a measure can aggregate. Deliberately no ``numeric``/``decimal``
#: entry yet: the ORM does not ship one, and listing a type nothing can declare
#: would be a promise rather than a check.
_NUMERIC_TYPES = frozenset(
    {Int16.name, Int32.name, Int64.name, Float32.name, Float64.name}
)

#: How many series a grouped view keeps before folding the rest together. Past
#: roughly seven, colour stops telling series apart and the honest form is a
#: table -- so this is where the default sits, and a caller who needs more says
#: so where a reviewer can see it.
DEFAULT_TOP = 7

#: The largest ``top=`` a declaration may raise the fold to. Not from the design
#: document, which fixes only the default: this is a guard rail on the number
#: being *raised*, chosen because two dozen lines is already past any legend and
#: well into the territory where the answer is a table. Refusing here means a
#: typo cannot turn into a thousand-series payload.
MAX_TOP = 24

#: How many groups an ungrouped-in-time aggregate returns before it refuses.
#: Unlike a series it does not fold: a bar chart's bars are the answer, and
#: silently dropping some of them draws a chart that is wrong rather than
#: absent.
DEFAULT_GROUP_LIMIT = 50


@dataclass(frozen=True, slots=True)
class Measure:
    """One aggregate, its unit, and how it behaves where there is no data.

    Built by :func:`count`, :func:`sum_`, :func:`avg`, :func:`min_` and
    :func:`max_` rather than directly, because the identity element and the
    rollup rule are properties of the function rather than choices.
    """

    function: str
    column: ColumnExpr | None
    #: What kind of quantity this is, carried to the client so a renderer can
    #: pick an axis without guessing from the name.
    kind: str
    unit: str | None = None
    #: The value an empty bucket takes, when the function has one.
    identity: Any = None
    #: Whether :attr:`identity` means anything. ``sum`` of no rows is ``0``;
    #: ``avg`` of no rows is undefined, and the difference is the whole of §3.1.
    has_identity: bool = False
    #: Whether aggregating this measure's own output again gives the same answer
    #: as aggregating the rows. False for ``avg``. Unused until coarser tiers
    #: exist, and recorded now because it is a fact about the function, not
    #: about the stage that first needs it.
    rollup_safe: bool = True

    def __repr__(self) -> str:
        inner = "" if self.column is None else self.column.column.python_name
        return f"<{self.kind} {self.function.lower()}({inner})>"


def count() -> Measure:
    """How many rows fall in each bucket. Empty buckets read as ``0``."""
    return Measure("COUNT", None, "count", None, 0, True)


def sum_(column: Any, *, unit: str | None = None) -> Measure:
    """The total of ``column``. Empty buckets read as ``0``."""
    return Measure("SUM", _numeric(column, "sum_"), "sum", unit, 0, True)


def avg(column: Any, *, unit: str | None = None) -> Measure:
    """The mean of ``column``. Empty buckets read as ``None``, never ``0``.

    An average of no rows is undefined. Rendering it as zero draws a line
    plunging to the floor on every quiet day, which reads as a collapse in the
    thing being measured rather than as an absence of it.
    """
    return Measure("AVG", _numeric(column, "avg"), "average", unit, rollup_safe=False)


def min_(column: Any, *, unit: str | None = None) -> Measure:
    """The smallest value of ``column``. Empty buckets read as ``None``."""
    return Measure("MIN", _numeric(column, "min_"), "minimum", unit)


def max_(column: Any, *, unit: str | None = None) -> Measure:
    """The largest value of ``column``. Empty buckets read as ``None``."""
    return Measure("MAX", _numeric(column, "max_"), "maximum", unit)


def _numeric(column: Any, name: str) -> ColumnExpr:
    if not isinstance(column, ColumnExpr):
        raise SeriesError(
            f"{name}() takes a model column such as Trek.distance_km, got {column!r}"
        )
    if column.column.pg_type.name not in _NUMERIC_TYPES:
        raise SeriesError(
            f"{name}() cannot aggregate {column.column.python_name}, which is "
            f"{column.column.pg_type.sql}; measures need a numeric column"
        )
    return column


@dataclass(frozen=True, slots=True)
class Range:
    """A half-open span of time — ``start <= t < end``.

    Half-open, stated once, and used for the filter, the spine bounds, and every
    boundary derived from them. The off-by-one in a chart comes from writing the
    boundary twice with two different intentions; there is only one here.
    """

    start: Any
    end: Any

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if getattr(value, "tzinfo", None) is None:
                raise SeriesError(
                    f"Range {name} must carry a UTC offset; a naive value has no "
                    f"single instant, and assuming UTC is the popular wrong guess"
                )
        if self.end <= self.start:
            raise SeriesError(
                f"Range is empty: end {self.end} is not after start {self.start}"
            )


@dataclass(frozen=True, slots=True)
class SeriesData:
    """One plottable line: a stable identity, its unit, and its values.

    ``key`` is the grouping value, never a rank — a series keeps its identity
    when its neighbours come and go. ``other`` marks the folded remainder, which
    also carries a ``None`` key, and is what tells it apart from a group whose
    value genuinely is ``NULL``.
    """

    measure: str
    key: Any
    label: str
    unit: str | None
    kind: str
    values: tuple[Any, ...]
    other: bool = False


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """A dense run of buckets and one named series per measure per group."""

    range: Range
    zone: str
    bucket: str
    #: The start instant of every bucket in the range, in order and without
    #: gaps. A renderer can use this as the x axis directly.
    buckets: tuple[Any, ...]
    series: tuple[SeriesData, ...]

    def __len__(self) -> int:
        return len(self.buckets)


@dataclass(frozen=True, slots=True)
class AggregateRow:
    key: Any
    label: str
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """Grouped totals with no time axis — a bar chart, a KPI, a scatter."""

    rows: tuple[AggregateRow, ...]
    measures: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class _Declaration:
    """What both shapes have in common, kept immutable so it can be shared."""

    model: type
    measures: tuple[tuple[str, Measure], ...] = ()
    predicates: tuple[Predicate, ...] = ()
    group: Any = None
    fills: dict[str, Any] = field(default_factory=dict)


class _Builder:
    """The shared half of the declaration surface.

    Every builder method returns a new object, so a declaration written once at
    import time is safe to reuse per request without a defensive copy — the same
    property ``Select`` has, for the same reason.
    """

    __slots__ = ("_d",)

    _d: _Declaration

    def _with(self, **changes: Any) -> Any:
        raise NotImplementedError

    # -- declaration ------------------------------------------------------

    @property
    def model(self) -> type:
        return self._d.model

    @property
    def measures(self) -> tuple[tuple[str, Measure], ...]:
        return self._d.measures

    @property
    def group(self) -> Any:
        return self._d.group

    @property
    def sources(self) -> tuple[type, ...]:
        """Every model this declaration reads, for ``invalidate_on``.

        Derived from the query rather than restated beside it, so a predicate
        that starts filtering through a relationship cannot leave a cache
        serving a stale chart.
        """
        found: list[type] = [self._d.model]
        for expression in (*self._d.predicates, self._d.group):
            for related in _related_columns(expression):
                owner = related.column.owner
                if owner is not None and owner not in found:
                    found.append(owner)
        return tuple(found)

    def where(self, *predicates: Predicate) -> Any:
        """Narrow this view; predicates combine with AND.

        Takes the same predicates a ``Select`` does — not a parallel filter
        language, the same one, compiled by the same code. A ``Param`` stands
        where a value would and is supplied per call to :meth:`run`.
        """
        for item in predicates:
            if not isinstance(item, Predicate):
                raise SeriesError(
                    f"where() takes SQL predicates such as Trek.herd_id == 1, got {item!r}"
                )
            check_predicate_columns(self._d.model, item)
        return self._with(predicates=self._d.predicates + tuple(predicates))

    def measure(self, **measures: Measure) -> Any:
        """Declare one or more named measures.

        Named rather than positional because the name is the series key in the
        result and the field name in a generated client; positional measures
        produce ``value_0``, which nobody wants in a component.

        Two measures return two *separate* named series, each with its own unit
        and kind. They are never merged into one plottable line: two quantities
        with different units on one pair of axes is a dual-axis chart, whose
        alignment is arbitrary and invents a correlation that is not in the data.
        """
        for name, item in measures.items():
            if not isinstance(item, Measure):
                raise SeriesError(
                    f"measure {name}= takes count(), sum_(), avg(), min_() or "
                    f"max_(), got {item!r}"
                )
            if name in dict(self._d.measures):
                raise SeriesError(f"measure {name!r} is declared twice")
        if not measures:
            raise SeriesError("measure() needs at least one named measure")
        return self._with(measures=self._d.measures + tuple(measures.items()))

    def fill(self, **values: Any) -> Any:
        """Override what an empty bucket reads as, per measure.

        Only needed to disagree with the measure's own identity — a count and a
        sum already fill with zero. Filling an average with a number is allowed
        and has to be written here, at the call site, where a reviewer can see
        that the flat line on a quiet day was a decision.
        """
        declared = dict(self._d.measures)
        for name in values:
            if name not in declared:
                raise SeriesError(
                    f"fill({name}=...) names no declared measure; this view has "
                    f"{', '.join(declared) or 'none'}"
                )
        return self._with(fills={**self._d.fills, **values})

    def _grouped_by(self, column: Any) -> Any:
        if not isinstance(column, ColumnExpr):
            raise SeriesError(
                f"by() takes a model column such as Trek.paddock_id, got {column!r}"
            )
        if self._d.group is not None:
            raise SeriesError(
                "by() is already declared; one grouping key per view, because a "
                "second one multiplies the series count rather than adding to it"
            )
        return column

    # -- the stages that are specified but not built ----------------------
    #
    # Named here so the surface a later stage fills in is visible now, and so a
    # caller who writes one gets an answer rather than a silent no-op. Each
    # refuses by name; none of them ever destroys anything, and `drop()` in
    # particular stays opt-in and unreachable by omission.

    def seal(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SeriesError(
            "seal() is not implemented yet: settled buckets and corrections are "
            "a later stage. Today every run recomputes the range it is given."
        )

    def retain(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SeriesError(
            "retain() is not implemented yet: tiered retention is a later stage. "
            "Nothing is expired or moved today, which is the safe default."
        )

    def archive(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SeriesError(
            "archive() is not implemented yet: cold storage ships with the "
            "erasure tooling that makes archived rows deletable, never before it."
        )

    def drop(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SeriesError(
            "drop() is not implemented, and it is the only part of this surface "
            "that destroys anything. It will stay opt-in when it arrives."
        )

    # -- running ----------------------------------------------------------

    def _bind(self, values: dict[str, Any]) -> tuple[Predicate, ...]:
        """This view's predicates with each ``Param`` replaced by its value."""
        # Two passes, because binding a missing name raises a bare KeyError from
        # inside the placeholder and the caller needs to be told *which*
        # parameter they left out, not which dict lookup failed.
        binders: list[Any] = []
        expected: list[str] = []
        for predicate in self._d.predicates:
            found: list[Any] = []
            binders.append(compile_rebind(predicate, _Placeholder, found))
            expected.extend(item.name for item in found)
        missing = [name for name in expected if name not in values]
        if missing:
            raise TypeError(f"run() is missing parameter {missing[0]!r}")
        unexpected = [name for name in values if name not in expected]
        if unexpected:
            raise TypeError(f"run() got an unexpected parameter {unexpected[0]!r}")
        return tuple(
            predicate if binder is None else binder(values)
            for predicate, binder in zip(self._d.predicates, binders, strict=True)
        )


class Aggregate(_Builder):
    """Grouped totals with no time axis — a bar chart, a KPI, or a scatter.

    The shared core, usable on its own::

        by_paddock = (
            Aggregate(Trek)
                .where(Trek.started_at >= Param("since"))
                .measure(treks=count(), distance=sum_(Trek.distance_km))
                .by(Trek.paddock_id)
        )

    Unlike a series it does not fold a long tail into a remainder: the bars are
    the answer, so a result past the ceiling refuses rather than drawing a chart
    that is quietly wrong.
    """

    __slots__ = ("_limit",)

    def __init__(self, model: type, *, _state: _Declaration | None = None,
                 _limit: int = DEFAULT_GROUP_LIMIT) -> None:
        self._d = _state if _state is not None else _Declaration(model=model)
        self._limit = _limit

    def _with(self, **changes: Any) -> Aggregate:
        return Aggregate(
            self._d.model,
            _state=replace_declaration(self._d, **changes),
            _limit=self._limit,
        )

    @property
    def limit(self) -> int:
        return self._limit

    def by(self, column: Any, *, limit: int = DEFAULT_GROUP_LIMIT) -> Aggregate:
        """Group by ``column``, refusing beyond ``limit`` groups.

        The ceiling is declared rather than passed per request, so it lives
        where it is reviewed instead of in a query parameter a client can set to
        a million.
        """
        checked = self._grouped_by(column)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SeriesError(f"by(limit=) must be a positive integer, got {limit!r}")
        return Aggregate(
            self._d.model,
            _state=replace_declaration(self._d, group=checked),
            _limit=limit,
        )

    async def run(self, session: Any, **values: Any) -> AggregateResult:
        """Run this declaration on ``session`` and assemble the result."""
        if not self._d.measures:
            raise SeriesError("this view declares no measures; there is nothing to compute")
        predicates = self._bind(values)
        sql, args, _oids = compile_aggregate(session.registry, self, predicates)
        rows = await session.declared(sql, args)
        if self._d.group is not None and len(rows) > self._limit:
            raise SeriesError(
                f"this view matched more than {self._limit} groups. Narrow it, or "
                f"raise the ceiling in the declaration with by(..., limit=N) -- "
                f"truncating would draw a chart that is wrong rather than absent"
            )
        return AggregateResult(
            rows=tuple(
                AggregateRow(key=key, label=_label(key), values=found)
                for key, found in aggregate_rows(self, rows)
            ),
            measures=tuple(name for name, _measure in self._d.measures),
        )

    def __repr__(self) -> str:
        return (
            f"<Aggregate {self._d.model.__name__} "
            f"measures={len(self._d.measures)} "
            f"by={'-' if self._d.group is None else self._d.group.column.python_name}>"
        )


class Series(_Builder):
    """A quantity per interval over a range, in the reader's timezone.

    Every bucket in the range exists whether or not anything happened in it,
    which is what makes the line honest: a quiet Tuesday is a zero rather than a
    segment joining Monday to Wednesday.
    """

    __slots__ = ("_at", "_bucket", "_stored_in", "_top")

    def __init__(
        self,
        model: type,
        *,
        at: Any,
        bucket: Bucket,
        stored_in: Any = None,
        _state: _Declaration | None = None,
        _top: int = DEFAULT_TOP,
    ) -> None:
        self._d = _state if _state is not None else _Declaration(model=model)
        self._at = _temporal(at)
        if not isinstance(bucket, Bucket):
            raise SeriesError(
                f"bucket= takes a bucket from wreath.temporal such as Day, got {bucket!r}"
            )
        self._bucket = bucket
        self._stored_in = stored_in
        self._top = _top

    def _with(self, **changes: Any) -> Series:
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=replace_declaration(self._d, **changes),
            _top=self._top,
        )

    @property
    def at(self) -> ColumnExpr:
        return self._at

    @property
    def bucket(self) -> Bucket:
        return self._bucket

    @property
    def top(self) -> int:
        return self._top

    def by(self, column: Any, *, top: int = DEFAULT_TOP) -> Series:
        """Split into one series per value of ``column``, keeping the top ``top``.

        Everything past the cut folds into a single remainder carrying the
        reserved key ``None`` and ``other=True``. Folding is meaningful where
        refusing would not be: the remainder preserves the total, which is what
        a part-to-whole chart is for.

        The survivors are ranked over the *whole* range by the first declared
        measure, so a series does not appear and vanish as the reader pans. The
        fold happens before aggregation, so the remainder's average is a true
        average of the tail's rows rather than an average of averages.
        """
        checked = self._grouped_by(column)
        if isinstance(top, bool) or not isinstance(top, int) or top < 1:
            raise SeriesError(f"by(top=) must be a positive integer, got {top!r}")
        if top > MAX_TOP:
            raise SeriesError(
                f"by(top={top}) is above the ceiling of {MAX_TOP}. Past a couple of "
                f"dozen lines a legend cannot distinguish them and the honest form "
                f"is a table -- group more coarsely, or render one"
            )
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=replace_declaration(self._d, group=checked),
            _top=top,
        )

    async def run(
        self, session: Any, *, range: Range, zone: Any = None, **values: Any
    ) -> SeriesResult:
        """Run this declaration for one range and one reader's zone.

        The range is a runtime argument because it changes per request. The zone
        is too, for now — nothing is materialised yet, so every run buckets
        fresh and any zone is as cheap as any other. ``stored_in`` is the
        default when a caller does not name one, and becomes load-bearing once
        buckets are settled: a materialised Auckland day cannot be re-cut into a
        London day after the fact.
        """
        if not self._d.measures:
            raise SeriesError("this view declares no measures; there is nothing to plot")
        if not isinstance(range, Range):
            raise SeriesError(f"run() needs range=Range(start, end), got {range!r}")
        zone_name = _zone_name(zone if zone is not None else self._stored_in)
        predicates = self._bind(values)
        window = (
            self._at >= _instant(range.start),
            self._at < _instant(range.end),
        )
        sql, args, _oids = compile_series(
            session.registry,
            self,
            (*window, *predicates),
            start=range.start,
            end=range.end,
            zone_name=zone_name,
        )
        rows = await session.declared(sql, args)
        return self._envelope(rows, range, zone_name)

    def _envelope(self, rows: list[Any], range: Range, zone_name: str) -> SeriesResult:
        buckets, found = series_rows(self, rows)
        blank: dict[str, Any] = {}
        series: list[SeriesData] = []
        for (key, other), by_bucket in found.items():
            for name, measure in self._d.measures:
                empty = fill(self, name, self._d.fills.get(name))
                series.append(
                    SeriesData(
                        measure=name,
                        key=key,
                        label=_label(key, other=other, measure=name),
                        unit=measure.unit,
                        kind=measure.kind,
                        other=other,
                        values=tuple(
                            _or_fill(by_bucket.get(item, blank).get(name), empty)
                            for item in buckets
                        ),
                    )
                )
        return SeriesResult(
            range=range,
            zone=zone_name,
            bucket=self._bucket.name,
            buckets=tuple(buckets),
            series=tuple(series),
        )

    def __repr__(self) -> str:
        return (
            f"<Series {self._d.model.__name__} by {self._bucket.name} "
            f"measures={len(self._d.measures)} "
            f"by={'-' if self._d.group is None else self._d.group.column.python_name}>"
        )


def replace_declaration(state: _Declaration, **changes: Any) -> _Declaration:
    from dataclasses import replace

    return replace(state, **changes)


def _related_columns(node: Any) -> list[RelatedColumnExpr]:
    """Every related column a predicate or a grouping key reaches.

    A predicate is a tree, so the column that names another model is usually an
    operand rather than the node itself -- which is what makes ``sources``
    derived rather than restated. The shape mirrors the compiler's own path
    walk; it collects owners instead of join paths, and stops at the same node
    types for the same reason.
    """
    found: list[RelatedColumnExpr] = []
    _walk_related(node, found)
    return found


def _walk_related(node: Any, out: list[RelatedColumnExpr]) -> None:
    # Before ColumnExpr: RelatedColumnExpr subclasses it, and a plain column
    # names no second model so there is nothing below it to collect.
    if isinstance(node, RelatedColumnExpr):
        if node.column is not None:
            out.append(node)
        return
    if isinstance(node, ColumnExpr):
        return
    for attribute in ("left", "right", "operand"):
        child = getattr(node, attribute, None)
        if child is not None:
            _walk_related(child, out)
    for attribute in ("operands", "values"):
        for child in getattr(node, attribute, ()) or ():
            _walk_related(child, out)


def _or_fill(value: Any, empty: Any) -> Any:
    return empty if value is None else value


def _label(key: Any, *, other: bool = False, measure: str | None = None) -> str:
    if other:
        return "other"
    if key is None:
        return measure or "total"
    return str(key)


def _temporal(column: Any) -> ColumnExpr:
    if not isinstance(column, ColumnExpr):
        raise SeriesError(
            f"at= takes a model column such as Trek.started_at, got {column!r}"
        )
    if column.column.pg_type.name not in _TEMPORAL_TYPES:
        raise SeriesError(
            f"at= cannot bucket {column.column.python_name}, which is "
            f"{column.column.pg_type.sql}; there is nothing to truncate to a "
            f"boundary. Bucketing needs a date or a timestamp column"
        )
    return column


def _zone_name(value: Any) -> str:
    if value is None:
        raise SeriesError(
            "no time zone: pass zone= to run(), or stored_in= to the declaration. "
            "A bucket is a span of somebody's wall clock, and UTC is a choice "
            "rather than an absence of one"
        )
    if isinstance(value, str):
        return value
    name = getattr(value, "key", None)
    if isinstance(name, str):
        return name
    raise SeriesError(
        f"zone= takes an IANA name or a wreath.temporal.zone(...), got {value!r}"
    )


def _instant(value: Any) -> Any:
    return Instant.of(value)
