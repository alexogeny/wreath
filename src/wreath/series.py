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

A declaration says the same thing once, and the database does the work:

```python
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
```

Declaration enforces four properties:

* **Every bucket in the range exists**, because the range generates a spine and
  the aggregate is joined onto it. An empty Tuesday is a zero, not an absence.
* **Fill is per measure.** A count of nothing is zero; an average of nothing is
  undefined, and stays `None` so the renderer draws a gap.
* **Every series has a stable key**, taken from the grouping value rather than
  from its position, so a filter change cannot repaint the legend.
* **Mistakes move to import time.** Bucketing by a column that holds no time,
  averaging text, grouping by a collection — those fail when the declaration is
  written, not on the request that first draws an empty chart.

**What this is not.** It takes one source model, declared measures, and a
bounded result. If you cannot name the model, name each measure and its unit,
or state the largest the result can get, then what you have is a query rather
than a chart, and `session.raw()` is the honest tool for it. This module
refuses to become a way to express everything, which is SQL with worse syntax.

Reference: `/reference/series`.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from ._duration import decimal_unit
from ._native import _core
from ._series.compile import (
    CURRENT,
    PREVIOUS,
    compile_aggregate,
    compile_cells,
    compile_events,
    compile_series,
)
from ._series.envelope import aggregate_rows, cell_rows, fill
from ._series.settle import (
    Seal,
    SealState,
    SettledStore,
    difference,
    fold,
    params_key,
    select_settled,
    view_key,
    watermark,
)
from ._series.tiers import Ladder, Segment, Tier
from ._series.tiers import build as _build_ladder
from ._series.tiers import plan as _plan_tiers
from ._series.tiers import width as _grain_width
from .geospatial import grid as _grid
from .orm.compiler import (
    check_predicate_columns,
    compile_declared_expression_values,
    compile_rebind,
)
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

# `Param` builds this node in place of a value; it is the marker
# `compile_rebind` looks for. Public in `wreath.queries` precisely because two
# modules now share that contract.
from .queries import Placeholder
from .temporal import (
    _DATETIME_CAPI,
    Bucket,
    Instant,
    TemporalError,
    from_wall_clock,
    parse_duration,
    wall_clock,
)
from .temporal import _tzinfo as _temporal_tzinfo
from .temporal import now as _now
from .temporal import zone as _zone_of

__all__ = [
    "Aggregate",
    "AggregateResult",
    "AggregateRow",
    "Cell",
    "Cells",
    "CellsResult",
    "ChartData",
    "Measure",
    "Range",
    "Series",
    "SeriesComparison",
    "SeriesData",
    "SeriesError",
    "SeriesEvent",
    "SeriesResult",
    "SettledStore",
    "Tier",
    "avg",
    "count",
    "max_",
    "min_",
    "nice_ticks",
    "project_chart",
    "project_chart_spine",
    "reconcile",
    "series_path",
    "sum_",
    "lttb",
]


class SeriesError(DeclarationError):
    """A declaration cannot mean what it says, or its result will not fit.

    A `DeclarationError` because that is what the ORM already raises when a
    query is malformed at the point it is written, and a calculated view is a
    query. Cardinality refusals raise it too: a result too large to draw is a
    fact about the declaration, even when only the run discovers it.
    """


#: Columns a bucket can be cut from. A bucket is a span of wall-clock time, so
#: the column has to hold one; anything else has no boundary to truncate to.
_TEMPORAL_TYPES = frozenset({TimestampTz.name, Timestamp.name, Date.name})

#: Columns a measure can aggregate. Deliberately no `numeric`/`decimal`
#: entry yet: the ORM does not ship one, and listing a type nothing can declare
#: would be a promise rather than a check.
_NUMERIC_TYPES = frozenset({Int16.name, Int32.name, Int64.name, Float32.name, Float64.name})

#: How many series a grouped view keeps before folding the rest together. Past
#: roughly seven, colour stops telling series apart and the honest form is a
#: table -- so this is where the default sits, and a caller who needs more says
#: so where a reviewer can see it.
DEFAULT_TOP = 7

#: The largest `top=` a declaration may raise the fold to. Not from the design
#: document, which fixes only the default: this is a guard rail on the number
#: being *raised*, chosen because two dozen lines is already past any legend and
#: well into the territory where the answer is a table. Refusing here means a
#: typo cannot turn into a thousand-series payload.
MAX_TOP = 24

#: How many markers an annotation layer carries before it refuses. A hundred
#: markers is not an annotation layer, it is noise -- so the default is a
#: quarter of that, and `MAX_EVENTS` is where "annotation" stops being a
#: description of what you asked for.
DEFAULT_EVENTS = 25
MAX_EVENTS = 100

#: How many groups an ungrouped-in-time aggregate returns before it refuses.
#: Unlike a series it does not fold: a bar chart's bars are the answer, and
#: silently dropping some of them draws a chart that is wrong rather than
#: absent.
DEFAULT_GROUP_LIMIT = 50

#: How many cells a `Cells` declaration may produce before it refuses.
#: Every cell is a row on the wire whether or not anything is in it — that is
#: the point of a dense axis — so the ceiling is on the lattice rather than on
#: the rows that matched. 10 000 is a 100x100 map, which is already more cells
#: than a screen has room to distinguish.
DEFAULT_CELL_LIMIT = 10_000


def reconcile(
    buckets: Any,
    sparse: dict[tuple[Any, bool], dict[Any, dict[str, Any]]],
    fills: dict[str, Any],
) -> tuple[tuple[tuple[Any, bool], str, tuple[Any, ...]], ...]:
    """Reconcile sparse measured values against a dense bucket run.

    This is the storage-neutral series kernel. `buckets` may be any ordered
    iterable; `sparse` maps a stable `(key, other)` series identity to its
    measured buckets; and `fills` gives each measure's absent value in output
    order. It accepts no declaration, model, connection, ORM type or SQL, so an
    in-memory producer and the PostgreSQL envelope use the same operation.
    """
    return _core.series_reconcile(buckets, sparse, fills)


def lttb(x: Any, y: Any, threshold: int) -> tuple[int, ...]:
    """Indices selected by Largest-Triangle-Three-Buckets downsampling.

    Inputs are finite numeric arrays of equal length. Returning indices keeps
    the operation independent of the caller's point type and lets one selection
    address parallel arrays such as labels, confidence bounds and tooltips.
    """
    return _core.series_lttb(x, y, threshold)


def nice_ticks(minimum: float, maximum: float, target: int = 6) -> tuple[float, ...]:
    """Readable 1/2/5 × 10ⁿ ticks covering an inclusive numeric extent."""
    return _core.series_nice_ticks(minimum, maximum, target)


def series_path(x: Any, y: Any) -> str:
    """An SVG line path whose `None` values begin a new segment.

    Missing measurements are gaps, never zeroes and never bridges. The output
    therefore emits `M` after every gap and `L` only within a contiguous
    run of finite numeric values.
    """
    return _core.series_path(x, y)


class ChartData:
    """An immutable numerical chart dataset prepared from plain iterables.

    Construction is the Python boundary: buckets, sparse readings and fills are
    validated and copied into owned numeric storage once. Repeated projections
    then retain dense cells, presence bits, LTTB selections and path buffers in
    that storage until the final paths and axes materialise.

    The ordinary `project_chart` and `project_chart_spine` calls
    remain useful for one-shot data. Use this value when the same in-memory or
    database-derived snapshot serves more than one projection.
    """

    __slots__ = (
        "_chart_cache",
        "_chart_joined_text_cache",
        "_chart_plan_cache",
        "_chart_text_cache",
        "_data",
    )

    def __init__(
        self,
        buckets: Any,
        sparse: dict[tuple[Any, bool], dict[Any, dict[str, Any]]],
        fills: dict[str, Any],
    ) -> None:
        self._data = _core.series_data(buckets, sparse, fills)
        # One immutable snapshot usually serves one stable chart shape. Keep
        # only its latest projection: operation-owned, bounded, and enough to
        # turn a repeated application write into a close-object cache hit.
        self._chart_cache = None
        self._chart_joined_text_cache = None
        self._chart_plan_cache = None
        self._chart_text_cache = None

    @classmethod
    def from_rows(
        cls,
        buckets: Any,
        series: Iterable[
            tuple[tuple[Any, bool], Iterable[tuple[Any, dict[str, Any]]]]
        ],
        fills: dict[str, Any],
    ) -> ChartData:
        """Prepare a chart without materializing a nested sparse mapping.

        Each `series` item is a stable `(key, other)` identity paired with a
        one-pass iterable of `(bucket, measure_dict)` readings. Series retain
        their input order; every bucket must belong to `buckets` and may occur
        only once within its series.
        """
        prepared = cls.__new__(cls)
        prepared._data = _core.series_data_rows(buckets, series, fills)
        prepared._chart_cache = None
        prepared._chart_joined_text_cache = None
        prepared._chart_plan_cache = None
        prepared._chart_text_cache = None
        return prepared

    def _chart_plan(
        self,
        key: tuple[tuple[int, ...], tuple[int, ...], int, int],
    ) -> Any:
        cached = self._chart_plan_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        downsample, full, threshold, tick_target = key
        plan = _core.series_data_chart_plan(
            self._data,
            downsample,
            full,
            threshold,
            tick_target,
        )
        self._chart_plan_cache = (key, plan)
        return plan

    def project_chart(
        self,
        *,
        downsample_rows: Any,
        full_rows: Any = (),
        threshold: int = 128,
        tick_target: int = 9,
        cache: bool = True,
    ) -> tuple[
        int,
        tuple[tuple[Any, bool], ...],
        tuple[str, ...],
        tuple[tuple[float, ...], ...],
    ]:
        """Project this snapshot to final SVG paths and tick axes."""

        downsample = tuple(downsample_rows)
        full = tuple(full_rows)
        key = (downsample, full, threshold, tick_target)
        cacheable = all(type(row) is int for row in downsample) and all(
            type(row) is int for row in full
        )
        cached = self._chart_cache
        if cache and cacheable and cached is not None and cached[0] == key:
            return cached[1]
        result = (
            _core.series_chart_plan(self._chart_plan(key))
            if cacheable
            else _core.series_data_chart(
                self._data,
                downsample,
                full,
                threshold,
                tick_target,
            )
        )
        if cache and cacheable:
            self._chart_cache = (key, result)
        return result

    def project_chart_text(
        self,
        *,
        downsample_rows: Any,
        full_rows: Any = (),
        threshold: int = 128,
        tick_target: int = 9,
        cache: bool = True,
    ) -> tuple[int, tuple[tuple[Any, bool], ...], tuple[str, ...], str, int]:
        """Serialize tick axes at their final text egress boundary."""

        downsample = tuple(downsample_rows)
        full = tuple(full_rows)
        key = (downsample, full, threshold, tick_target)
        cacheable = all(type(row) is int for row in downsample) and all(
            type(row) is int for row in full
        )
        cached = self._chart_text_cache
        if cache and cacheable and cached is not None and cached[0] == key:
            return cached[1]
        result = (
            _core.series_chart_plan_text(self._chart_plan(key))
            if cacheable
            else _core.series_data_chart_text(
                self._data,
                downsample,
                full,
                threshold,
                tick_target,
            )
        )
        if cache and cacheable:
            self._chart_text_cache = (key, result)
        return result

    def project_chart_text_joined(
        self,
        *,
        downsample_rows: Any,
        full_rows: Any = (),
        threshold: int = 128,
        tick_target: int = 9,
        cache: bool = True,
    ) -> tuple[int, tuple[tuple[Any, bool], ...], str, int, str, int]:
        """Write all SVG paths contiguously and serialize the tick axes."""

        downsample = tuple(downsample_rows)
        full = tuple(full_rows)
        key = (downsample, full, threshold, tick_target)
        if not all(type(row) is int for row in downsample) or not all(
            type(row) is int for row in full
        ):
            raise TypeError("joined chart projection requires integer row indices")
        cached = self._chart_joined_text_cache
        if cache and cached is not None and cached[0] == key:
            return cached[1]
        result = _core.series_chart_plan_text_joined(self._chart_plan(key))
        if cache:
            self._chart_joined_text_cache = (key, result)
        return result


def project_chart(
    buckets: Any,
    sparse: dict[tuple[Any, bool], dict[Any, dict[str, Any]]],
    fills: dict[str, Any],
    *,
    downsample_rows: Any,
    full_rows: Any = (),
    threshold: int = 128,
    tick_target: int = 9,
) -> tuple[
    int,
    tuple[tuple[Any, bool], ...],
    tuple[str, ...],
    tuple[tuple[float, ...], ...],
]:
    """Project sparse series data directly to compact chart outputs.

    Row indices address the stable series-major, measure-minor order produced
    by `reconcile`. The operation retains dense cells, LTTB selections and path
    buffers for the call; only identities, final SVG paths and tick axes cross
    into Python.
    """
    return _core.series_chart(
        buckets,
        sparse,
        fills,
        downsample_rows,
        full_rows,
        threshold,
        tick_target,
    )


def project_chart_text(
    buckets: Any,
    sparse: dict[tuple[Any, bool], dict[Any, dict[str, Any]]],
    fills: dict[str, Any],
    *,
    downsample_rows: Any,
    full_rows: Any = (),
    threshold: int = 128,
    tick_target: int = 9,
) -> tuple[int, tuple[tuple[Any, bool], ...], tuple[str, ...], str, int]:
    """Project one sparse snapshot while serializing only final tick text.

    Dense values and one reusable LTTB selection workspace stay owned by this
    call. Unlike :class:`ChartData`, this operation deliberately retains no
    projection result, so it is the appropriate boundary when input is rebuilt
    or may change for every request.
    """
    return _core.series_chart_text(
        buckets,
        sparse,
        fills,
        downsample_rows,
        full_rows,
        threshold,
        tick_target,
    )


def project_chart_spine(
    start: Any,
    end: Any,
    *,
    bucket: Bucket,
    in_zone: Any,
    sparse: dict[tuple[Any, bool], dict[Any, dict[str, Any]]],
    fills: dict[str, Any],
    downsample_rows: Any,
    full_rows: Any = (),
    threshold: int = 128,
    tick_target: int = 9,
) -> tuple[
    int,
    tuple[tuple[Any, bool], ...],
    tuple[str, ...],
    tuple[tuple[float, ...], ...],
]:
    """Project a local-wall-clock dense range without materialising its spine.

    Sparse bucket keys are mapped directly to ordinal positions in the native
    dense run. The range, unit, zone, sparse values and fill values are all
    data: no series declaration, model, connection, ORM type or SQL enters the
    operation. Use `project_chart` when the dense run already exists as
    an iterable, and this form when the run is defined by a temporal range.
    """
    if not isinstance(bucket, Bucket):
        raise SeriesError(
            f"project_chart_spine(bucket=) takes a bucket such as Day, got {bucket!r}"
        )
    start_instant = Instant.of(start)
    end_instant = Instant.of(end)
    tz = _temporal_tzinfo(in_zone)
    unit = ("minute", "hour", "day", "week", "month", "quarter", "year").index(bucket.name)
    return _core.series_chart_spine(
        start_instant,
        end_instant,
        unit,
        tz,
        sparse,
        fills,
        downsample_rows,
        full_rows,
        threshold,
        tick_target,
        _DATETIME_CAPI,
    )


@dataclass(frozen=True, slots=True)
class Measure:
    """One aggregate, its unit, and how it behaves where there is no data.

    Built by `count`, `sum_`, `avg`, `min_` and
    `max_` rather than directly, because the identity element and the
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
    #: Whether `identity` means anything. `sum` of no rows is `0`;
    #: `avg` of no rows is undefined, and the difference is the whole of §3.1.
    has_identity: bool = False
    #: Whether aggregating this measure's own output again gives the same answer
    #: as aggregating the rows. False for `avg`. Unused until coarser tiers
    #: exist, and recorded now because it is a fact about the function, not
    #: about the stage that first needs it.
    rollup_safe: bool = True

    def __repr__(self) -> str:
        inner = "" if self.column is None else self.column.column.python_name
        return f"<{self.kind} {self.function.lower()}({inner})>"


def count() -> Measure:
    """How many rows fall in each bucket. Empty buckets read as `0`."""
    return Measure("COUNT", None, "count", None, 0, True)


def sum_(column: Any, *, unit: str | None = None) -> Measure:
    """The total of `column`. Empty buckets read as `0`."""
    return Measure("SUM", _numeric(column, "sum_"), "sum", unit, 0, True)


def avg(column: Any, *, unit: str | None = None) -> Measure:
    """The mean of `column`. Empty buckets read as `None`, never `0`.

    An average of no rows is undefined. Rendering it as zero draws a line
    plunging to the floor on every quiet day, which reads as a collapse in the
    thing being measured rather than as an absence of it.
    """
    return Measure("AVG", _numeric(column, "avg"), "average", unit, rollup_safe=False)


def min_(column: Any, *, unit: str | None = None) -> Measure:
    """The smallest value of `column`. Empty buckets read as `None`."""
    return Measure("MIN", _numeric(column, "min_"), "minimum", unit)


def max_(column: Any, *, unit: str | None = None) -> Measure:
    """The largest value of `column`. Empty buckets read as `None`."""
    return Measure("MAX", _numeric(column, "max_"), "maximum", unit)


def _numeric(column: Any, name: str) -> ColumnExpr:
    if not isinstance(column, ColumnExpr):
        raise SeriesError(f"{name}() takes a model column such as Trek.distance_km, got {column!r}")
    if column.column.pg_type.name not in _NUMERIC_TYPES:
        raise SeriesError(
            f"{name}() cannot aggregate {column.column.python_name}, which is "
            f"{column.column.pg_type.sql}; measures need a numeric column"
        )
    return column


@dataclass(frozen=True, slots=True)
class Range:
    """A half-open span of time — `start <= t < end`.

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
            raise SeriesError(f"Range is empty: end {self.end} is not after start {self.start}")


@dataclass(frozen=True, slots=True)
class SeriesData:
    """One plottable line: a stable identity, its unit, and its values.

    `key` is the grouping value, never a rank — a series keeps its identity
    when its neighbours come and go. `other` marks the folded remainder, which
    also carries a `None` key, and is what tells it apart from a group whose
    value genuinely is `NULL`.
    """

    measure: str
    key: Any
    label: str
    unit: str | None
    kind: str
    values: tuple[Any, ...]
    other: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "measure": self.measure,
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "kind": self.kind,
            "values": list(self.values),
            "other": self.other,
        }


@dataclass(frozen=True, slots=True)
class SeriesEvent:
    """One marker: when it happened, which bucket that was, and what to call it.

    Both times are carried because both are wanted and neither can be derived
    from the other on the client. `at` puts the marker at its true
    x-position; `bucket` says which column it annotates, computed by the
    same `date_trunc` in the same zone as the series it sits over, so a
    marker cannot land a bucket away from the bar it describes.
    """

    at: Any
    bucket: Any
    label: Any

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "bucket": self.bucket, "label": self.label}


@dataclass(frozen=True, slots=True)
class _Events:
    """A declared annotation layer, waiting for a range to run against."""

    model: type
    at: ColumnExpr
    label: ColumnExpr
    predicates: tuple[Predicate, ...]
    limit: int


@dataclass(frozen=True, slots=True)
class SeriesComparison:
    """The same declaration over the period before it.

    Its own bucket run rather than values slotted into the primary one, because
    the two periods are legitimately different lengths: February against March
    is 28 buckets against 31. Padding one to match the other would invent data;
    lining them up by index is a decision for whoever draws the chart, and this
    payload gives them what they need to make it rather than making it for them.
    """

    #: The bucket width the range was shifted by — `"month"` for a
    #: month-over-month comparison. What a legend calls it.
    previous: str
    buckets: tuple[Any, ...]
    series: tuple[SeriesData, ...]

    def __len__(self) -> int:
        return len(self.buckets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous,
            "buckets": list(self.buckets),
            "series": [item.as_dict() for item in self.series],
        }


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
    #: The prior period, when the declaration asked for one. `None` otherwise,
    #: so a caller that never compares never has to check for it.
    comparison: SeriesComparison | None = None
    #: Markers over the same range, in the order they happened. Empty unless the
    #: declaration asked for them.
    events: tuple[SeriesEvent, ...] = ()
    #: Where the watermark fell and what is known behind it. `None` for a view
    #: that declares no seal, so a caller who never seals never has to check.
    state: Any = None
    #: Which tier answered for which part of the range, oldest first. Empty for
    #: a view with no ladder. Always reported rather than only when it is
    #: surprising: a chart drawn from two grains should be able to say so, and a
    #: caller who passed `allow_coarsening=True` needs to know where it was
    #: taken up.
    segments: tuple[Any, ...] = ()

    def __len__(self) -> int:
        return len(self.buckets)

    def as_dict(self) -> dict[str, Any]:
        """The JSON body, with the field names the generated TypeScript expects.

        Written out rather than derived from the dataclass, because these keys
        are a wire contract shared with `wreath typegen` and a field rename
        should have to be made in both places on purpose.

        Needed because the JSON encoder does not know dataclasses: returning the
        result object itself raises `TypeError` on the first request. Teaching
        `temporal.jsonable` a hook would let the object go back directly and is
        the better long-term shape; it belongs to that module rather than here.
        """
        return {
            "range": {"start": self.range.start, "end": self.range.end},
            "zone": self.zone,
            "bucket": self.bucket,
            "buckets": list(self.buckets),
            "series": [item.as_dict() for item in self.series],
            "comparison": None if self.comparison is None else self.comparison.as_dict(),
            "events": [event.as_dict() for event in self.events],
            "sealed": None
            if self.state is None
            else {
                "through": self.state.sealed_through,
                "settled": list(self.state.settled),
                "corrections": list(self.state.corrections),
            },
            "segments": [
                {"start": item.start, "end": item.end, "grain": item.grain}
                for item in self.segments
            ],
        }

    def __jsonable__(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class AggregateRow:
    key: Any
    label: str
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "values": dict(self.values)}


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """Grouped totals with no time axis — a bar chart, a KPI, a scatter."""

    rows: tuple[AggregateRow, ...]
    measures: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.rows)

    def as_dict(self) -> dict[str, Any]:
        """The JSON body, matching the generated `AggregateResult<M>`."""
        return {
            "rows": [row.as_dict() for row in self.rows],
            "measures": list(self.measures),
        }

    def __jsonable__(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class _CellAxis:
    """The spatial axis: two columns and the lattice they are bucketed onto."""

    lat: ColumnExpr
    lon: ColumnExpr
    grid: Any


@dataclass(frozen=True, slots=True)
class _Declaration:
    """What both shapes have in common, kept immutable so it can be shared."""

    model: type
    measures: tuple[tuple[str, Measure], ...] = ()
    predicates: tuple[Predicate, ...] = ()
    group: Any = None
    fills: dict[str, Any] = field(default_factory=dict)
    cell: _CellAxis | None = None
    _binders: tuple[Any, ...] = field(init=False, repr=False, compare=False)
    _parameters: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _parameter_set: frozenset[str] = field(init=False, repr=False, compare=False)
    _value_program: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Compile the fixed predicate walks while the declaration is built."""
        binders: list[Any] = []
        found: list[Any] = []
        for predicate in self.predicates:
            binders.append(compile_rebind(predicate, Placeholder, found))
        parameters = tuple(item.name for item in found)
        object.__setattr__(self, "_binders", tuple(binders))
        object.__setattr__(self, "_parameters", parameters)
        object.__setattr__(self, "_parameter_set", frozenset(parameters))
        object.__setattr__(
            self,
            "_value_program",
            compile_declared_expression_values(self.predicates, Placeholder),
        )


@dataclass(frozen=True, slots=True)
class _CompiledSeriesPlan:
    """Registry-owned statement facts shared by equivalent declarations."""

    sql: str
    bind_oids: tuple[int, ...]


def _series_plan_key(sql: str, bind_oids: tuple[int, ...]) -> bytes:
    """An exact, collision-free registry-cache key for one Series statement."""
    encoded_oids = b"".join(oid.to_bytes(4, "big") for oid in bind_oids)
    return b"\x00series\x00" + sql.encode("utf-8") + b"\x00oids\x00" + encoded_oids


class _Builder:
    """The shared half of the declaration surface.

    Every builder method returns a new object, so a declaration written once at
    import time is safe to reuse per request without a defensive copy — the same
    property `Select` has, for the same reason.
    """

    __slots__ = ("_d",)

    _d: _Declaration

    def _with(self, **changes: Any) -> Any:
        raise NotImplementedError

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
    def cell(self) -> Any:
        """The spatial axis, or `None` on a declaration that has no place."""
        return self._d.cell

    @property
    def fills(self) -> dict[str, Any]:
        """Per-measure overrides for what an empty bucket or cell reads as."""
        return self._d.fills

    @property
    def sources(self) -> tuple[type, ...]:
        """Every model this declaration reads, for `invalidate_on`.

        Derived from the query rather than restated beside it, so a predicate
        that starts filtering through a relationship cannot leave a cache
        serving a stale chart.
        """
        found: list[type] = [self._d.model]
        seen = {self._d.model}
        for expression in (*self._d.predicates, self._d.group):
            for related in _related_columns(expression):
                owner = related.column.owner
                if owner is not None and owner not in seen:
                    seen.add(owner)
                    found.append(owner)
        return tuple(found)

    @property
    def predicates(self) -> tuple[Predicate, ...]:
        """The declared filters, for a reader that wants to inspect them.

        Public for the same reason `sources` is: a declaration is a value,
        and a tool that has to know what this view touches should read it here
        rather than re-deriving it from source.
        """
        return self._d.predicates

    @property
    def declared_columns(self) -> tuple[tuple[str, ColumnExpr], ...]:
        """Every column this view names, tagged with what it does to it.

        `("time", …)` is the bucketing column, `("aggregate", …)` a column
        summed or averaged, `("group", …)` the grouping key. Filters are
        `predicates` instead, because for a filter the *operator* decides
        whether it is safe and a bare column would throw that away.

        This exists for a reader that does not exist yet, and the reason is
        worth writing down. Design 24 (deferred data migrations) refuses
        `SUM`, `AVG`, `MIN`, `MAX`, `GROUP BY` and joins on a column
        that is *mid-conversion* — a backfill rewriting values underneath a
        running application. Those are not exotic operations here; they are most
        of what a calculated view is made of, and a grouped chart over a
        half-converted column shows one category forking into two with nothing
        raising.

        The check belongs to the migrations side, which is the only half that
        knows a conversion is running — this module deliberately has no notion
        of a converting column and should not grow one. What it owes is
        *inspectability*: a declaration is a value written at import time, so a
        scan can read this instead of parsing handlers. That is the contract,
        and it is why this is a property rather than a note in a design
        document.
        """
        found: list[tuple[str, ColumnExpr]] = []
        at = getattr(self, "_at", None)
        if at is not None:
            found.append(("time", at))
        for _name, measure in self._d.measures:
            if measure.column is not None:
                found.append(("aggregate", measure.column))
        if self._d.group is not None:
            found.append(("group", self._d.group))
        return tuple(found)

    def where(self, *predicates: Predicate) -> Any:
        """Narrow this view; predicates combine with AND.

        Takes the same predicates a `Select` does — not a parallel filter
        language, the same one, compiled by the same code. A `Param` stands
        where a value would and is supplied per call to `run`.
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
        produce `value_0`, which nobody wants in a component.

        Two measures return two *separate* named series, each with its own unit
        and kind. They are never merged into one plottable line: two quantities
        with different units on one pair of axes is a dual-axis chart, whose
        alignment is arbitrary and invents a correlation that is not in the data.
        """
        existing = {name for name, _item in self._d.measures}
        for name, item in measures.items():
            if not isinstance(item, Measure):
                raise SeriesError(
                    f"measure {name}= takes count(), sum_(), avg(), min_() or max_(), got {item!r}"
                )
            if name in existing:
                raise SeriesError(f"measure {name!r} is declared twice")
        if not measures:
            raise SeriesError("measure() needs at least one named measure")
        built = self._with(measures=self._d.measures + tuple(measures.items()))
        # A measure added *after* the ladder has to face the same additivity
        # check the ladder applied to the ones already there -- otherwise
        # `.retain(...).measure(mean=avg(...))` walks straight past it, and
        # builder methods commute everywhere else in this surface.
        ladder = getattr(built, "_tiers", None)
        if ladder is not None:
            _refuse_unrollable(ladder, built.measures, built._bucket)
        return built

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
            raise SeriesError(f"by() takes a model column such as Trek.paddock_id, got {column!r}")
        if self._d.group is not None:
            raise SeriesError(
                "by() is already declared; one grouping key per view, because a "
                "second one multiplies the series count rather than adding to it"
            )
        return column

    # Named here so the surface a later stage fills in is visible now, and so a
    # caller who writes one gets an answer rather than a silent no-op. Each
    # refuses by name; none of them ever destroys anything, and `drop()` in
    # particular stays opt-in and unreachable by omission.

    def seal(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SeriesError(
            "seal() applies to a time series, not to this shape: there are no "
            "buckets to close. Declare it on a Series(...)."
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

    def _bind(self, values: dict[str, Any]) -> tuple[Predicate, ...]:
        """This view's predicates with each `Param` replaced by its value."""
        self._check_values(values)
        return tuple(
            predicate if binder is None else binder(values)
            for predicate, binder in zip(self._d.predicates, self._d._binders, strict=True)
        )

    def _check_values(self, values: dict[str, Any]) -> None:
        """Refuse missing and extra parameters before a compiled extractor runs."""
        missing = [name for name in self._d._parameters if name not in values]
        if missing:
            raise TypeError(f"run() is missing parameter {missing[0]!r}")
        # `_parameter_set` is a startup-compiled frozenset. The discovery scan
        # cannot carry that field type through `_d`, but membership is O(1).
        unexpected = [name for name in values if name not in self._d._parameter_set]
        if unexpected:
            raise TypeError(f"run() got an unexpected parameter {unexpected[0]!r}")

    def _predicate_values(self, values: dict[str, Any]) -> tuple[Any, ...]:
        """Wire-ready predicate values in the compiler's fixed render order."""
        self._check_values(values)
        return self._d._value_program(values)


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell of a heatmap: where it is, and what was in it."""

    #: Index from the extent's south-west corner.
    row: int
    column: int
    #: The ground the cell covers, and the point a renderer pins a marker to.
    bounds: Any
    centre: Any
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "bounds": {
                "lat_min": self.bounds.lat_min,
                "lat_max": self.bounds.lat_max,
                "lon_min": self.bounds.lon_min,
                "lon_max": self.bounds.lon_max,
            },
            "centre": {"lat": self.centre.lat, "lon": self.centre.lon},
            "values": dict(self.values),
        }

    def __jsonable__(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class CellsResult:
    """Every cell of the lattice, in row-major order."""

    cells: tuple[Cell, ...]
    measures: tuple[str, ...]
    grid: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "cells": [cell.as_dict() for cell in self.cells],
            "measures": list(self.measures),
            "grid": {
                "rows": self.grid.rows,
                "columns": self.grid.columns,
                "metres": self.grid.metres,
                "distortion": self.grid.distortion,
                "extent": {
                    "lat_min": self.grid.extent.lat_min,
                    "lat_max": self.grid.extent.lat_max,
                    "lon_min": self.grid.extent.lon_min,
                    "lon_max": self.grid.extent.lon_max,
                },
            },
        }

    def __jsonable__(self) -> dict[str, Any]:
        return self.as_dict()


class Cells(_Builder):
    """A quantity per cell over an extent — a heatmap as a declaration.

    The spatial sibling of `Series`, and it exists for the same reason.
    Every cell in the extent is present whether or not anything happened in it,
    and fill is decided per measure, so a quiet cell is a zero count and an
    undefined average rather than a hole in the map or a plunge to the floor.

    ```python
    heat = (
        Cells(Sighting)
            .where(Sighting.species == Param("species"))
            .measure(seen=count(), mean_weight=avg(Sighting.weight_kg))
            .over(Sighting.lat, Sighting.lon, metres=10_000, extent=reserve)
    )
    ```

    The lattice is a declaration-time fact, so the number of cells a request
    will produce is knowable before it runs — which is what lets the ceiling be
    enforced where a reviewer reads it rather than after the database has done
    the work.
    """

    __slots__ = ()

    def __init__(self, model: type, *, _state: _Declaration | None = None) -> None:
        self._d = _state if _state is not None else _Declaration(model=model)

    def _with(self, **changes: Any) -> Cells:
        return Cells(self._d.model, _state=replace_declaration(self._d, **changes))

    @property
    def grid(self) -> Any:
        """The lattice this declaration buckets onto, or `None` before `over`."""
        return None if self._d.cell is None else self._d.cell.grid

    def over(
        self,
        lat: Any,
        lon: Any,
        *,
        metres: float,
        extent: Any,
        limit: int = DEFAULT_CELL_LIMIT,
    ) -> Cells:
        """Bucket onto a lattice of `metres` cells covering `extent`.

        The ceiling is declared rather than passed per request, for the same
        reason `by(limit=)` is: it lives where it is reviewed instead of in a
        query parameter a client can set to a million. It is checked here, not
        at run time, because `rows * columns` is arithmetic on the extent.
        """
        for name, column in (("lat", lat), ("lon", lon)):
            if not isinstance(column, ColumnExpr):
                raise SeriesError(
                    f"over({name}=...) takes a model column such as Sighting.{name}, got {column!r}"
                )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SeriesError(f"over(limit=) must be a positive integer, got {limit!r}")
        lattice = _grid(extent, metres=metres)
        if lattice.count > limit:
            raise SeriesError(
                f"this extent at {lattice.metres:g} m is {lattice.rows}x"
                f"{lattice.columns} = {lattice.count} cells, past the {limit} "
                f"ceiling. Every cell is a row on the wire whether or not "
                f"anything is in it, so widen the cells, narrow the extent, or "
                f"raise the ceiling in the declaration with over(..., limit=N)"
            )
        return self._with(cell=_CellAxis(lat=lat, lon=lon, grid=lattice))

    async def run(self, session: Any, **values: Any) -> CellsResult:
        """Run this declaration on `session` and assemble every cell."""
        if not self._d.measures:
            raise SeriesError("this view declares no measures; there is nothing to compute")
        if self._d.cell is None:
            raise SeriesError(
                "this view declares no spatial axis; call over(lat, lon, metres=..., extent=...)"
            )
        predicates = self._bind(values)
        sql, args, _oids = compile_cells(session.registry, self, predicates)
        rows = await session.declared(sql, args)
        lattice = self._d.cell.grid
        return CellsResult(
            cells=tuple(
                Cell(
                    row=row,
                    column=column,
                    bounds=lattice.cell(row, column),
                    centre=lattice.centre(row, column),
                    values=found,
                )
                for row, column, found in cell_rows(self, rows)
            ),
            measures=tuple(name for name, _measure in self._d.measures),
            grid=lattice,
        )

    def __repr__(self) -> str:
        lattice = self.grid
        shape = "-" if lattice is None else f"{lattice.rows}x{lattice.columns}"
        return f"<Cells {self._d.model.__name__} measures={len(self._d.measures)} grid={shape}>"


class Aggregate(_Builder):
    """Grouped totals with no time axis — a bar chart, a KPI, or a scatter.

    The shared core, usable on its own:

    ```python
    by_paddock = (
        Aggregate(Trek)
            .where(Trek.started_at >= Param("since"))
            .measure(treks=count(), distance=sum_(Trek.distance_km))
            .by(Trek.paddock_id)
    )
    ```

    Unlike a series it does not fold a long tail into a remainder: the bars are
    the answer, so a result past the ceiling refuses rather than drawing a chart
    that is quietly wrong.
    """

    __slots__ = ("_limit",)

    def __init__(
        self, model: type, *, _state: _Declaration | None = None, _limit: int = DEFAULT_GROUP_LIMIT
    ) -> None:
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
        """Group by `column`, refusing beyond `limit` groups.

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
        """Run this declaration on `session` and assemble the result."""
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

    __slots__ = (
        "_at",
        "_bucket",
        "_compare",
        "_events",
        "_seal",
        "_stored_in",
        "_tiers",
        "_top",
    )

    def __init__(
        self,
        model: type,
        *,
        at: Any,
        bucket: Bucket,
        stored_in: Any = None,
        _state: _Declaration | None = None,
        _top: int = DEFAULT_TOP,
        _compare: Bucket | None = None,
        _events: _Events | None = None,
        _seal: Seal | None = None,
        _tiers: Ladder | None = None,
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
        self._compare = _compare
        self._events = _events
        self._seal = _seal
        self._tiers = _tiers

    def _with(self, **changes: Any) -> Series:
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=replace_declaration(self._d, **changes),
            _top=self._top,
            _compare=self._compare,
            _events=self._events,
            _seal=self._seal,
            _tiers=self._tiers,
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

    @property
    def sources(self) -> tuple[type, ...]:
        """Every model this view reads, the annotation layer included.

        A new deploy changes the chart just as a new trek does, so a cache keyed
        on `sources` has to know about both. Leaving the events model out is
        the kind of omission that shows up as a marker missing for five minutes
        and gets blamed on the browser.
        """
        found = list(super().sources)
        if self._events is None:
            return tuple(found)
        seen = set(found)
        for expression in (self._events.at, self._events.label, *self._events.predicates):
            for related in _related_columns(expression):
                owner = related.column.owner
                if owner is not None and owner not in seen:
                    seen.add(owner)
                    found.append(owner)
        if self._events.model not in seen:
            found.append(self._events.model)
        return tuple(found)

    def by(self, column: Any, *, top: int = DEFAULT_TOP) -> Series:
        """Split into one series per value of `column`, keeping the top `top`.

        Everything past the cut folds into a single remainder carrying the
        reserved key `None` and `other=True`. Folding is meaningful where
        refusing would not be: the remainder preserves the total, which is what
        a part-to-whole chart is for.

        The survivors are ranked over the *whole* range by the first declared
        measure, so a series does not appear and vanish as the reader pans. The
        fold happens before aggregation, so the remainder's average is a true
        average of the tail's rows rather than an average of averages.
        """
        checked = self._grouped_by(column)
        if self._tiers is not None:
            raise SeriesError(
                "by() cannot be combined with retain(), for the reason it cannot "
                "be combined with seal(): a materialised bucket is stored under a "
                "range-independent key, and the top-N fold is ranked over "
                "whichever range was asked for. Group an unmaterialised view"
            )
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
            _compare=self._compare,
            _events=self._events,
            _seal=self._seal,
            _tiers=self._tiers,
        )

    def compare(self, *, previous: Bucket) -> Series:
        """Also compute the same range one `previous` earlier.

        `previous=Month` answers "and what did this look like last month?".
        The shift is a bucket width from `wreath.temporal` rather than a
        duration, because the useful comparisons are calendar ones: a month is
        28 to 31 days depending on when you ask, and "the same days last month"
        is what a reader means even though no fixed number of hours expresses it.

        It stays one statement. Two statements are how the periods end up
        misaligned by a bucket, and the alignment is the entire value of the
        feature — anyone can run the query twice.

        The comparison period must not overlap the primary one. A shift shorter
        than the range would put some rows in both periods, and there is no
        honest way to draw that: counting them twice inflates the comparison,
        counting them once silently drops them from one side.
        """
        if not isinstance(previous, Bucket):
            raise SeriesError(
                f"compare(previous=) takes a bucket from wreath.temporal such as "
                f"Month, got {previous!r}"
            )
        if self._compare is not None:
            raise SeriesError(
                "compare() is already declared; one comparison period per view, "
                "because a second one is a second chart rather than more of this one"
            )
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=self._d,
            _top=self._top,
            _compare=previous,
            _events=self._events,
            _seal=self._seal,
            _tiers=self._tiers,
        )

    def events(
        self,
        model: type,
        *,
        at: Any,
        label: Any,
        where: Any = (),
        limit: int = DEFAULT_EVENTS,
    ) -> Series:
        """Annotate the chart with markers — deploys, incidents, releases.

        The markers come from their own model and are read over the range the
        series already has, which is the whole reason this belongs on the
        declaration rather than beside it. Two hand-written queries drift: one
        clips its range differently, or buckets in a different zone, and a
        marker lands a column away from the event it describes.

        Each marker carries both its exact instant and the bucket it falls in,
        computed by the same `date_trunc` in the same zone as the series, so
        it can sit at its true x-position while still knowing what it annotates.

        This is a **second statement** on the same session rather than a tagged
        `UNION ALL` of buckets and markers. The union would force two
        different row shapes into one, half the columns null in every row, with
        a discriminator the client has to switch on — a worse envelope and worse
        generated types, in exchange for a round trip the driver describes
        itself as pipelining anyway. Whether it really costs one round trip or
        two is a question for a real server; the alignment does not depend on
        the answer.

        With `compare`, markers cover the primary period only. An
        annotation layer answers "what happened during *this*", and drawing last
        month's deploys over this month's chart would need a second axis to be
        readable at all.
        """
        if self._events is not None:
            raise SeriesError(
                "events() is already declared; one annotation layer per view, "
                "because a second one is a second thing to draw rather than more "
                "of this one"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SeriesError(f"events(limit=) must be a positive integer, got {limit!r}")
        if limit > MAX_EVENTS:
            raise SeriesError(
                f"events(limit={limit}) is above the ceiling of {MAX_EVENTS}. Past "
                f"a hundred markers an annotation layer is noise rather than "
                f"annotation -- filter them, or show them as a table beside the chart"
            )
        if not isinstance(label, ColumnExpr):
            raise SeriesError(
                f"events(label=) takes a model column such as Deploy.version, got {label!r}"
            )
        predicates = tuple(where) if isinstance(where, (tuple, list)) else (where,)
        for item in predicates:
            if not isinstance(item, Predicate):
                raise SeriesError(
                    f"events(where=) takes SQL predicates such as "
                    f"Deploy.environment == 'production', got {item!r}"
                )
            check_predicate_columns(model, item)
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=self._d,
            _top=self._top,
            _compare=self._compare,
            _events=_Events(
                model=model,
                at=_temporal(at),
                label=label,
                predicates=predicates,
                limit=limit,
            ),
            _seal=self._seal,
        )

    def seal(self, *, after: Any, on_late: str = "correct") -> Series:
        """Declare when a bucket stops being able to change.

        A bucket is **sealed** once `after` has elapsed since it *closed*, and
        a sealed bucket is computed once and then read. Before that it is open
        and every run recomputes it, because it can still move.

        Args:
            after: the lateness allowance -- `"2h"`, `"30m"`, or a number of
                seconds. How long you are willing to wait for a straggler.
            on_late: what a `reconcile` does when it finds a sealed bucket
                whose rows have since changed. `"correct"` keeps the settled
                value immutable and records the difference beside it.
                `"reopen"` replaces it, which is only sound while the rows are
                still there to recompute from -- so it is never the default.

        A settled bucket is not a cache. It has no TTL, it is never evicted, and
        nothing here deletes the rows it was computed from.
        """
        if self._seal is not None:
            raise SeriesError(
                "seal() is already declared; one watermark per view, because a "
                "second one would make 'sealed' mean two different instants"
            )
        if self._d.group is not None:
            raise SeriesError(
                "seal() cannot be combined with by(): the top-N fold is ranked "
                "over the whole range, so which series survive -- and what lands "
                "in the remainder -- depends on the range being asked for. A "
                "bucket settled for one range would be wrong for the next. Seal "
                "an ungrouped view, or read this one open"
            )
        if self._compare is not None:
            raise SeriesError(
                "seal() cannot be combined with compare(): the comparison period "
                "is a second range and settling it is a separate question. "
                "Declare the seal on the view you read directly"
            )
        declared = Seal(after=_lateness(after), on_late=on_late)
        # A seal declared *after* the ladder faces the same check retain()
        # applies when the ladder comes second -- builder methods commute
        # everywhere else in this surface, and a refusal that depends on the
        # order two clauses were written in is not a refusal anyone can trust.
        _refuse_unreopenable(declared, self._tiers, self._bucket)
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=self._d,
            _top=self._top,
            _compare=self._compare,
            _events=self._events,
            _seal=declared,
            _tiers=self._tiers,
        )

    @property
    def sealed_after(self) -> float | None:
        """The declared lateness allowance in seconds, or `None` if open."""
        return None if self._seal is None else self._seal.after

    def retain(self, **windows: Any) -> Series:
        """How long each grain stays warm, finest to coarsest.

        ```python
        .seal(after="2h").retain(raw="3 days", day="1 year", month=None)
        ```

        Read as: keep the source rows answering for three days, keep daily
        buckets a year, keep monthly buckets forever. `None` means
        indefinitely.

        **Nothing here deletes anything, and this stage adds no way to.**
        `retain` is a promise about what stays warm, not an instruction to
        expire. What it changes today is which tier a read prefers: a range
        older than raw's window is answered from the coarsest tier that still
        covers it, *even though raw happens to still be present*. This keeps the
        query shape stable when retention enforcement begins.

        A tier coarser than `raw` requires `seal`, because a tier stores
        a value on the understanding that it is final and only the seal says
        when a value is final. A measure that cannot be recombined from parts --
        an average -- is refused against a bounded raw window, because a coarse
        bucket built from averages is an average of averages.
        """
        if self._tiers is not None:
            raise SeriesError(
                "retain() is already declared; one ladder per view, because a "
                "second one would give the same grain two retention windows"
            )
        if self._d.group is not None:
            raise SeriesError(
                "retain() cannot be combined with by(), for the reason seal() "
                "cannot: a materialised bucket is stored per range-independent "
                "key, and the top-N fold is ranked over whichever range was "
                "asked for. Retain an ungrouped view"
            )
        ladder = _build_ladder(windows, refuse=SeriesError)
        if ladder.materialised and self._seal is None:
            raise SeriesError(
                "retain() with a grain above 'raw' needs seal(): a tier stores a "
                "bucket on the understanding that it will not change again, and "
                "seal() is what says when that is true. Add "
                ".seal(after='...') before .retain(...), or declare retain(raw=...) "
                "alone to state the source window without materialising anything"
            )
        _refuse_unrollable(ladder, self._d.measures, self._bucket)
        _refuse_unreopenable(self._seal, ladder, self._bucket)
        return Series(
            self._d.model,
            at=self._at,
            bucket=self._bucket,
            stored_in=self._stored_in,
            _state=self._d,
            _top=self._top,
            _compare=self._compare,
            _events=self._events,
            _seal=self._seal,
            _tiers=ladder,
        )

    @property
    def tiers(self) -> tuple[Tier, ...]:
        """The declared retention ladder, finest first. Empty when undeclared."""
        return () if self._tiers is None else self._tiers.tiers

    def _identity(
        self, zone_name: str, values: dict[str, Any], *, grain: Bucket | None = None
    ) -> tuple[str, str]:
        """`(view, params)` -- what a settled row is filed under.

        `grain` is how a tier gets its own key without a second table or a
        second kind of identity: `view_key` already folds the bucket into
        the digest, so asking for the same declaration at `Month` yields the
        monthly tier's key. The daily tier of a view bucketed by day is
        therefore *literally* the rows sealing already writes -- which is the
        concrete form of §7.3's claim that rollup and settlement are one
        mechanism seen from two ends.
        """
        rendered = " ".join(repr(item) for item in self._d.predicates)
        return (
            view_key(
                model=self._d.model,
                at_column=self._at.column.python_name,
                bucket=self._bucket if grain is None else grain,
                zone_name=zone_name,
                measures=self._d.measures,
                predicate_sql=rendered,
                fills=self._d.fills,
            ),
            params_key(values),
        )

    def _compiled_args(
        self,
        values: dict[str, Any],
        *,
        start: Any,
        end: Any,
        zone_name: str,
    ) -> tuple[Any, ...]:
        """Bind one run to the immutable SQL shape in placeholder order.

        This is the value-only mirror of ``compile_series``. The compiler's
        statement emits the declared predicates twice for an ordinary grouped
        view, then repeats the range and zone at the aggregate, spine, and
        final projection. Keeping that fixed sequence here lets a cache hit
        skip identifier quoting, join planning, and SQL assembly entirely.
        Tests compare this tuple with the compiler's independently collected
        values for every grouped/comparison shape, so a new bind added to the
        statement cannot silently drift from this program.
        """
        predicates = self._predicate_values(values)
        bound: list[Any] = []
        grouped = self._d.group is not None
        compared = self._compare is not None
        if grouped:
            bound.extend(predicates)
            bound.extend((start, end, self._top))
        bound.append(zone_name)
        if compared:
            bound.append(start)
        bound.extend(predicates)
        bound.extend((start, end))
        if compared:
            bound.extend((start, zone_name, zone_name, end, zone_name, zone_name))
            bound.extend(
                (
                    start,
                    zone_name,
                    end,
                    zone_name,
                    start,
                    zone_name,
                    end,
                    zone_name,
                )
            )
        else:
            bound.extend((start, zone_name, end, zone_name))
        bound.append(zone_name)
        return tuple(bound)

    async def run(
        self,
        session: Any,
        *,
        range: Range,
        zone: Any = None,
        now: Any = None,
        allow_coarsening: bool = False,
        **values: Any,
    ) -> SeriesResult:
        """Run this declaration for one range and one reader's zone.

        The range is a runtime argument because it changes per request. The zone
        is too, unless the view is sealed — a materialised Auckland day cannot
        be re-cut into a London day after the fact, so a sealed view files its
        settled buckets under the zone they were computed in and reading it in
        another zone settles separately rather than lying.

        `now` decides where the watermark falls and defaults to the present.
        Passing one reads the range as of that instant, which is what makes a
        sealing test deterministic and a "what did this look like on Friday"
        question answerable.

        `allow_coarsening` accepts a coarser grain for the part of a range no
        tier stores at the grain asked for. Off by default: returning monthly
        numbers labelled as days is a lie that survives review, so the honest
        default is to refuse and name the coarsest grain available.
        `SeriesResult.segments` always reports the grain actually used.
        """
        if not self._d.measures:
            raise SeriesError("this view declares no measures; there is nothing to plot")
        if not isinstance(range, Range):
            raise SeriesError(f"run() needs range=Range(start, end), got {range!r}")
        zone_name = _zone_name(zone if zone is not None else self._stored_in)
        if self._compare is not None:
            _refuse_overlap(range, self._compare, zone_name)
        if self._tiers is not None:
            predicates = self._bind(values)
            result = await self._run_tiered(
                session, predicates, range, zone_name, values, now, allow_coarsening
            )
        elif self._seal is not None:
            predicates = self._bind(values)
            result = await self._run_sealed(session, predicates, range, zone_name, values, now)
        else:
            start = _instant(range.start)
            end = _instant(range.end)
            cached = session.registry.cached_prepared_plan(self)
            if cached is None:
                predicates = self._bind(values)
                sql, args, oids = compile_series(
                    session.registry,
                    self,
                    predicates,
                    start=start,
                    end=end,
                    zone_name=zone_name,
                    compare=self._compare,
                )
                shape_key = _series_plan_key(sql, oids)
                plan = session.registry.store_plan(shape_key, _CompiledSeriesPlan(sql, oids))
                session.registry.remember_prepared_shape(self, shape_key)
                sql = plan.sql
            else:
                _shape_key, plan = cached
                sql = plan.sql
                args = self._compiled_args(values, start=start, end=end, zone_name=zone_name)
            rows = await session.declared(sql, args)
            result = self._envelope(rows, range, zone_name)
        if self._events is None:
            return result
        return replace(result, events=await self._markers(session, range, zone_name))

    async def _compute(
        self,
        session: Any,
        predicates: tuple[Predicate, ...],
        start: Any,
        end: Any,
        zone_name: str,
    ) -> dict[Any, dict[str, Any]]:
        """`{bucket: {measure: value}}` straight from the source rows."""
        sql, args, _oids = compile_series(
            session.registry,
            self,
            predicates,
            start=_instant(start),
            end=_instant(end),
            zone_name=zone_name,
            compare=None,
        )
        rows = await session.declared(sql, args)
        buckets, dense = self._dense_rows(rows)
        by_measure = {name: values for _key, name, values in dense}
        return {
            bucket: {name: by_measure[name][index] for name, _measure in self._d.measures}
            for index, bucket in enumerate(buckets)
        }

    async def _run_sealed(
        self,
        session: Any,
        predicates: tuple[Predicate, ...],
        range: Range,
        zone_name: str,
        values: dict[str, Any],
        now: Any,
    ) -> SeriesResult:
        """Read settled buckets from storage and compute only what is still open.

        Three statements at most, and two once the range is warm: read what is
        settled, compute any sealed buckets nobody has settled yet, compute the
        open tail. The middle one is the work sealing exists to stop repeating,
        and it shrinks to nothing as the watermark advances past a range that
        has already been read.
        """
        instant = _instant(now) if now is not None else _now()
        start, end = _instant(range.start), _instant(range.end)
        merged, state = await self._settled_span(
            session, predicates, start, end, zone_name, values, instant
        )
        buckets = sorted(merged)
        found = {(None, False): merged}
        return SeriesResult(
            range=range,
            zone=zone_name,
            bucket=self._bucket.name,
            buckets=tuple(buckets),
            series=self._series_from_sparse(buckets, found),
            state=state,
        )

    async def _settled_span(
        self,
        session: Any,
        predicates: tuple[Predicate, ...],
        start: Any,
        end: Any,
        zone_name: str,
        values: dict[str, Any],
        instant: Any,
    ) -> tuple[dict[Any, dict[str, Any]], Any]:
        """One span at this view's own grain: settled behind the watermark, open ahead.

        Split out from `_run_sealed` because a tiered read needs exactly
        this for the part of a range that raw still answers for, and having two
        copies of "what is sealed here" is how the watermark starts meaning two
        things.
        """
        if self._seal is None:
            raise SeriesError("sealed span evaluation requires seal()")
        edge = watermark(
            instant,
            bucket=self._bucket,
            zone_name=zone_name,
            after=self._seal.after,
        )
        sealed_end = min(end, edge)
        view, params = self._identity(zone_name, values)

        settled: dict[Any, dict[str, Any]] = {}
        corrected: list[Any] = []
        if sealed_end > start:
            stored = await session.declared(select_settled(), (view, params, start, sealed_end))
            for row in stored:
                bucket, measures, delta = row[0], row[1], row[2]
                settled[bucket] = fold(_as_mapping(measures), _as_mapping(delta))
                if delta:
                    corrected.append(bucket)
            # Sealing advances forwards, so the buckets nobody has settled are a
            # suffix: recomputing from just past the last stored one covers them
            # without a second round trip to ask which are missing.
            # `end_of` rather than adding the bucket's nominal length, because
            # the last stored bucket may have been a 23- or 25-hour day and
            # stepping by 24 would start the gap in the middle of one.
            gap_from = self._bucket.end_of(max(settled), zone_name) if settled else start
            if gap_from < sealed_end:
                # Computed and returned, deliberately **not** stored. Reading is
                # a read: a `GET` serving a sealed view runs on a read-workload
                # session, against a replica, or under a role with no INSERT,
                # and a read that settles as a side effect answers `cannot
                # execute INSERT in a read-only transaction` from inside the
                # series machinery on a route that wrote nothing the
                # application can see. `settle()` is the write half, and it is
                # a job. The number is identical either way -- what changes is
                # whether the next reader recomputes it.
                fresh = await self._compute(session, predicates, gap_from, sealed_end, zone_name)
                for bucket, measures in fresh.items():
                    if bucket in settled:
                        continue
                    settled[bucket] = measures

        open_part: dict[Any, dict[str, Any]] = {}
        if end > sealed_end:
            open_part = await self._compute(
                session, predicates, max(start, sealed_end), end, zone_name
            )

        return (
            {**settled, **open_part},
            SealState(
                sealed_through=edge if sealed_end > start else None,
                settled=tuple(sorted(settled)),
                corrections=tuple(sorted(corrected)),
            ),
        )

    async def _run_tiered(
        self,
        session: Any,
        predicates: tuple[Predicate, ...],
        range: Range,
        zone_name: str,
        values: dict[str, Any],
        now: Any,
        allow_coarsening: bool,
    ) -> SeriesResult:
        """Stitch one or more tiers onto a single spine, and say which answered.

        §7.4's promise is that the caller never knows there were tiers -- so the
        pieces are merged into one bucket run and one set of series, exactly as
        an untiered read produces. What the caller *can* see, in
        `SeriesResult.segments`, is which grain answered where: that is
        reporting, not something they have to handle.
        """
        if self._tiers is None:
            raise SeriesError("tiered reads require retain()")
        instant = _instant(now) if now is not None else _now()
        start, end = _instant(range.start), _instant(range.end)
        stored_zone = _zone_name(self._stored_in) if self._stored_in is not None else zone_name
        segments = _plan_tiers(
            ladder=self._tiers,
            requested=self._bucket,
            start=start,
            end=end,
            now=instant,
            stored_zone=stored_zone,
            read_zone=zone_name,
            allow_coarsening=allow_coarsening,
            refuse=SeriesError,
        )

        merged: dict[Any, dict[str, Any]] = {}
        settled: list[Any] = []
        corrections: list[Any] = []
        edge: Any = None
        for segment in segments:
            if segment.tier.is_raw:
                part, state = await self._settled_span(
                    session,
                    predicates,
                    segment.start,
                    segment.end,
                    zone_name,
                    values,
                    instant,
                )
                if state.sealed_through is not None:
                    edge = state.sealed_through if edge is None else max(edge, state.sealed_through)
                settled.extend(state.settled)
                corrections.extend(state.corrections)
            else:
                part, found = await self._tier_span(session, segment, zone_name, values)
                settled.extend(part)
                corrections.extend(found)
            merged.update(part)

        buckets = sorted(merged)
        return SeriesResult(
            range=range,
            zone=zone_name,
            bucket=self._bucket.name,
            buckets=tuple(buckets),
            series=self._series_from_sparse(buckets, {(None, False): merged}),
            state=SealState(
                sealed_through=edge,
                settled=tuple(sorted(settled)),
                corrections=tuple(sorted(corrections)),
            ),
            segments=segments,
        )

    async def _tier_span(
        self,
        session: Any,
        segment: Segment,
        zone_name: str,
        values: dict[str, Any],
    ) -> tuple[dict[Any, dict[str, Any]], list[Any]]:
        """Read one materialised tier's stored rows for one piece of the range.

        One statement, and no contact with the source table at all. A tier is
        the same `series_buckets` rows sealing writes, filed under the key
        that grain hashes to -- so this is `select_settled` again, asking
        a different question of the same table.
        """
        view, params = self._identity(zone_name, values, grain=segment.tier.grain)
        stored = await session.declared(
            select_settled(), (view, params, segment.start, segment.end)
        )
        part: dict[Any, dict[str, Any]] = {}
        corrections: list[Any] = []
        for row in stored:
            bucket, measures, delta = row[0], row[1], row[2]
            part[bucket] = fold(_as_mapping(measures), _as_mapping(delta))
            if delta:
                corrections.append(bucket)
        return part, corrections

    async def rollup(
        self,
        session: Any,
        *,
        range: Range,
        zone: Any = None,
        now: Any = None,
        **values: Any,
    ) -> dict[str, tuple[Any, ...]]:
        """Materialise every coarser tier over the sealed part of `range`.

        The durable-job body §7.3 describes, written as an ordinary method so
        the scheduling stays in `wreath.jobs` where it already
        lives — `jobs.schedule` with a dedup key of declaration, tier and
        bucket makes a re-run a no-op, and the insert refuses to overwrite
        anyway, so at-least-once delivery costs nothing here.

        Two steps, in this order:

        1. `reconcile` the range first, so a late write that landed behind
           the watermark is folded in before anything coarser is built from it.
           Running rollup without this would carve a stale number into a coarser
           grain, where it is harder to notice and no longer traceable to the
           row that caused it.
        2. Compute each coarser grain **from the source rows**, not from the
           finer tier. Nothing is ever removed at this stage, so raw is always
           there, and recomputing from it is both correct and immune to the
           average-of-averages trap. Building a tier from a tier becomes
           necessary only when retention starts genuinely removing rows.

        Returns the buckets written per tier, so a job can log what it did
        rather than reporting that it ran.
        """
        if self._tiers is None:
            raise SeriesError(
                "rollup() needs retain(): with no ladder there is no coarser grain to materialise"
            )
        if self._seal is None:  # retain() refuses coarser tiers without it
            raise SeriesError("rollup() requires seal() before coarser retention tiers")
        zone_name = _zone_name(zone if zone is not None else self._stored_in)
        instant = _instant(now) if now is not None else _now()
        await self.reconcile(session, range=range, zone=zone, now=now, **values)

        predicates = self._bind(values)
        start = _instant(range.start)
        written: dict[str, tuple[Any, ...]] = {}
        for tier in self._tiers.materialised:
            grain = tier.grain
            if grain is None:
                raise SeriesError("a materialised retention tier requires a grain")
            edge = watermark(instant, bucket=grain, zone_name=zone_name, after=self._seal.after)
            end = min(_instant(range.end), edge)
            if end <= start:
                written[tier.name] = ()
                continue
            written[tier.name] = await self._materialise(
                session, predicates, grain, start, end, zone_name, values
            )
        return written

    async def _materialise(
        self,
        session: Any,
        predicates: tuple[Predicate, ...],
        grain: Bucket,
        start: Any,
        end: Any,
        zone_name: str,
        values: dict[str, Any],
    ) -> tuple[Any, ...]:
        """Compute one coarser grain from the source rows and store what is new."""
        coarser = Series(
            self._d.model,
            at=self._at,
            bucket=grain,
            stored_in=self._stored_in,
            _state=self._d,
            _top=self._top,
        )
        fresh = await coarser._compute(session, predicates, start, end, zone_name)
        view, params = self._identity(zone_name, values, grain=grain)
        stored = await session.declared(select_settled(), (view, params, start, end))
        already = {row[0] for row in stored}
        added = [bucket for bucket in sorted(fresh) if bucket not in already]
        if added:
            await _insert_settled_rows(
                session, view, params, ((bucket, fresh[bucket]) for bucket in added)
            )
        return tuple(added)

    async def settle(
        self,
        session: Any,
        *,
        range: Range,
        zone: Any = None,
        now: Any = None,
        **values: Any,
    ) -> tuple[Any, ...]:
        """Store every sealed bucket in `range` that nobody has settled yet.

        **The write half of `.seal()`, and the only one there is.** Reading a
        sealed view never writes: a `GET` runs on a read-workload session, on a
        replica, or under a role with no `INSERT`, and settling as a side effect
        of a read answers `cannot execute INSERT in a read-only transaction`
        from inside the series machinery, on a route that wrote nothing the
        application can see. So the store is filled here, from a scheduled job,
        beside the `reconcile` that keeps it honest — `reconcile` runs this
        first, so an application already scheduling one needs no second job.

        Idempotent by construction: the insert is `ON CONFLICT DO NOTHING`, and
        two workers settling the same bucket compute the same number from the
        same rows, so the loser has nothing to add.

        A `run()` over a range this has never covered returns exactly the same
        numbers — it computes the sealed part and does not keep it. What
        settling buys is that the *next* reader does not have to.

        Returns:
            The bucket starts it stored, so a job can log what it did.

        Raises:
            SeriesError: this view declares no `seal()`.
        """
        if self._seal is None:
            raise SeriesError(
                "settle() needs a seal: with no watermark no bucket is ever "
                "final, so there is nothing that could be stored once and read"
            )
        zone_name = _zone_name(zone if zone is not None else self._stored_in)
        predicates = self._bind(values)
        instant = _instant(now) if now is not None else _now()
        edge = watermark(instant, bucket=self._bucket, zone_name=zone_name, after=self._seal.after)
        start = _instant(range.start)
        sealed_end = min(_instant(range.end), edge)
        if sealed_end <= start:
            return ()
        view, params = self._identity(zone_name, values)
        stored = await session.declared(select_settled(), (view, params, start, sealed_end))
        known = {row[0] for row in stored}
        # The same suffix argument the read path makes: sealing advances
        # forwards, so what is missing starts just past the last stored bucket.
        gap_from = self._bucket.end_of(max(known), zone_name) if known else start
        if gap_from >= sealed_end:
            return ()
        fresh = await self._compute(session, predicates, gap_from, sealed_end, zone_name)
        written = [bucket for bucket in sorted(fresh) if bucket not in known]
        if written:
            await _insert_settled_rows(
                session, view, params, ((bucket, fresh[bucket]) for bucket in written)
            )
        return tuple(written)

    async def reconcile(
        self,
        session: Any,
        *,
        range: Range,
        zone: Any = None,
        now: Any = None,
        **values: Any,
    ) -> tuple[Any, ...]:
        """Find sealed buckets whose rows have changed, and record the difference.

        This is how a late write is noticed. Nothing notices one on the write
        path, and that is deliberate rather than missing: the ORM's write events
        are model-grained by design — they publish which models a session
        touched, not which rows — so they cannot say which bucket a late trek
        belongs to or what it contributes. Making them row-grained to serve a
        chart would put per-row bookkeeping on every write in the application.

        So the application runs this: from a scheduled job, after an import,
        after a backfill. It `settle`s the sealed part of `range` first — since
        reading stores nothing, this is where a bucket becomes stored at all —
        then recomputes it from the source rows, compares each bucket to what
        was settled, and writes a delta where they disagree, or replaces the
        settled value outright if the view declared `on_late="reopen"`.

        Returns the bucket starts it corrected, so a caller can log or alert on
        late data arriving rather than discovering it in a discrepancy later. A
        bucket settled by this same call is not a correction and is not in it;
        `settle` returns those.
        """
        if self._seal is None:
            raise SeriesError(
                "reconcile() needs a seal: with nothing settled, every run "
                "already recomputes from the source rows and there is nothing "
                "to compare against"
            )
        # Reconciliation compares against a settled bucket, so settlement must
        # run first.
        await self.settle(session, range=range, zone=zone, now=now, **values)
        zone_name = _zone_name(zone if zone is not None else self._stored_in)
        predicates = self._bind(values)
        instant = _instant(now) if now is not None else _now()
        edge = watermark(instant, bucket=self._bucket, zone_name=zone_name, after=self._seal.after)
        start = _instant(range.start)
        sealed_end = min(_instant(range.end), edge)
        if sealed_end <= start:
            return ()
        view, params = self._identity(zone_name, values)
        stored = await session.declared(select_settled(), (view, params, start, sealed_end))
        settled = {row[0]: _as_mapping(row[1]) for row in stored}
        if not settled:
            return ()
        current = await self._compute(session, predicates, start, sealed_end, zone_name)
        changes: list[tuple[Any, dict[str, Any]]] = []
        for bucket, was in settled.items():
            if bucket not in current:
                continue
            delta = difference(was, current[bucket], self._d.measures)
            if delta is None:
                continue
            changes.append((bucket, current[bucket] if self._seal.on_late == "reopen" else delta))
        if changes:
            if self._seal.on_late == "reopen":
                await _replace_settled_rows(session, view, params, changes)
            else:
                await _upsert_correction_rows(session, view, params, changes)
        return tuple(sorted(bucket for bucket, _value in changes))

    async def _markers(self, session: Any, range: Range, zone_name: str) -> tuple[SeriesEvent, ...]:
        """The annotation layer, over the same range and in the same zone."""
        declared = self._events
        if declared is None:
            raise SeriesError("marker reads require events()")
        sql, args, _oids = compile_events(
            session.registry,
            declared.model,
            declared.at,
            declared.label,
            declared.predicates,
            start=_instant(range.start),
            end=_instant(range.end),
            zone_name=zone_name,
            trunc=self._bucket.trunc,
            limit=declared.limit,
        )
        rows = await session.declared(sql, args)
        if len(rows) > declared.limit:
            raise SeriesError(
                f"this view matched more than {declared.limit} markers. Narrow "
                f"them with events(where=...), or raise the ceiling in the "
                f"declaration with events(..., limit=N) -- drawing the first "
                f"{declared.limit} would annotate the chart with a subset nothing "
                f"in it explains"
            )
        return tuple(SeriesEvent(at=row[0], bucket=row[1], label=row[2]) for row in rows)

    def _dense_rows(self, rows: Any, *, periods: bool = False) -> Any:
        """Own backend row reconciliation until final bucket/value tuples."""
        measures = dict(self._d.measures)
        fills = self._fill_values()
        labels = (CURRENT, PREVIOUS) if periods else None
        return _core.series_dense_rows(
            rows,
            tuple(measures),
            tuple(fills.values()),
            self._d.group is not None,
            labels,
        )

    def _fill_values(self) -> dict[str, Any]:
        return {
            name: fill(self, name, self._d.fills.get(name)) for name, _measure in self._d.measures
        }

    def _series_from_sparse(self, buckets: Any, sparse: dict[Any, Any]) -> tuple[SeriesData, ...]:
        """Materialize a sparse map at a settlement or tier boundary."""
        return self._series_of(reconcile(buckets, sparse, self._fill_values()))

    def _series_of(self, dense: Any) -> tuple[SeriesData, ...]:
        measures = dict(self._d.measures)
        series: list[SeriesData] = []
        for (key, other), name, values in dense:
            measure = measures[name]
            series.append(
                SeriesData(
                    measure=name,
                    key=key,
                    label=_label(key, other=other, measure=name),
                    unit=measure.unit,
                    kind=measure.kind,
                    other=other,
                    values=values,
                )
            )
        return tuple(series)

    def _envelope(self, rows: list[Any], range: Range, zone_name: str) -> SeriesResult:
        if self._compare is None:
            buckets, dense = self._dense_rows(rows)
            return SeriesResult(
                range=range,
                zone=zone_name,
                bucket=self._bucket.name,
                buckets=tuple(buckets),
                series=self._series_of(dense),
            )
        current, previous = self._dense_rows(rows, periods=True)
        current_buckets, current_dense = current
        previous_buckets, previous_dense = previous
        return SeriesResult(
            range=range,
            zone=zone_name,
            bucket=self._bucket.name,
            buckets=tuple(current_buckets),
            series=self._series_of(current_dense),
            comparison=SeriesComparison(
                previous=self._compare.name,
                buckets=tuple(previous_buckets),
                series=self._series_of(previous_dense),
            ),
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
    operand rather than the node itself -- which is what makes `sources`
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


def _refuse_unrollable(
    ladder: Ladder, measures: tuple[tuple[str, Measure], ...], bucket: Bucket
) -> None:
    """Refuse a measure that cannot be rebuilt from parts against a bounded raw window.

    §7.5's correctness trap, and the design calls it "the big one": **rollup is
    lossy, and average-of-averages is wrong.** A daily average built by averaging
    twenty-four hourly averages weights a quiet 3am hour exactly as heavily as a
    busy noon, and the number it produces is confidently, quietly incorrect.

    Two things make this refusal the right shape rather than an inconvenience.

    The check is the *same predicate* that decides whether a bucket can absorb a
    late correction (§7.2). Additivity over disjoint sets is one property with
    two consequences: a measure that rolls up can take a delta, and a measure
    that cannot roll up cannot take one either. That is why it reads
    `Measure.rollup_safe` -- one fact about the function -- rather than a
    second list that could disagree with the first.

    And it is deliberately conditional on `raw` being *bounded*. A coarse tier
    is computed from the source rows today, so an average over one is correct
    right now; what makes it unsound is the retention window promising those
    rows will stop being there. §7.5 gives exactly two ways out and this enforces
    both: pin raw retention across the whole query window (`raw=None`), or
    take the coarse tier off the ladder.

    The third way out, which is not built: store `avg` decomposed as a sum and
    a count and divide at read time, so it becomes additive after all. That is
    §7.5's own prescription, it is invisible to the caller, and it is a change to
    what a settled row holds rather than a check -- so it is named in the
    refusal instead of being half-implemented here.
    """
    coarser = [
        tier
        for tier in ladder.materialised
        if tier.grain is not None and _grain_width(tier.grain) > _grain_width(bucket)
    ]
    if not coarser or not ladder.raw_bounded:
        return
    for name, measure in measures:
        if measure.rollup_safe:
            continue
        tier = coarser[0].name
        raise SeriesError(
            f"measure {name!r} is an {measure.kind} and cannot be rolled up, but "
            f"this ladder keeps raw for a bounded time and materialises "
            f"{tier!r} above it. Combining {measure.kind}s from coarser parts "
            "means averaging averages, which weights a quiet bucket the same as "
            "a busy one and produces a number that looks reasonable and is "
            "wrong. Either keep the source rows for the whole window the chart "
            f"asks about (retain(raw=None, ...)), or drop the {tier!r} tier. "
            f"Storing {name!r} decomposed as a sum and a count would make it "
            "additive, and that is not implemented"
        )


def _refuse_unreopenable(seal: Seal | None, ladder: Ladder | None, bucket: Bucket) -> None:
    """Refuse `on_late="reopen"` when raw cannot outlive the seal window.

    §7.2's rule. Reopening a sealed bucket means **recomputing it from the
    source rows and overwriting the stored value**. If those rows are gone, the
    recomputation produces a smaller number and writes it over one that was
    correct -- silently, and destructively, since reopening also clears the
    correction that would have shown something was wrong.

    The arithmetic is one bucket wider than the design's phrasing, and the extra
    width is load-bearing. A bucket `[start, end)` seals at `end + after`,
    but recomputing it needs *every* row in it, and the oldest sits at `start`
    -- a whole bucket earlier. So raw has to promise:

    ```text
    keep >= after + width(bucket)
    ```

    Keeping raw for exactly the seal window is not enough: at the instant the
    bucket sealed, its first row would already be past the edge.

    **Equality is accepted, not refused.** `Tier.covers`
    tests `instant >= now - keep`, so a row sitting
    exactly on the retention edge is still covered. Refusing at equality would
    contradict the coverage predicate one module over, and two rules disagreeing
    about the same boundary is worse than either rule being slightly loose.

    **Two durations, two precisions, and why that is sound here.** `after`
    comes from `_lateness`, which is exact -- it cannot even express
    months or years. `keep` comes from `retain()`, which reads `"1 year"`
    as a mean length on purpose. Comparing them is comparing an exact number
    against an approximate one, and the approximation would matter if anything
    else read `keep` differently. Nothing does: `Tier.covers` uses the same
    seconds at runtime, so this check and the behaviour it predicts cannot
    disagree, whatever the calendar does.

    **What this cannot catch, stated rather than implied.** It is a necessary
    condition, not a sufficient one. It refuses a declaration under which reopen
    could *never* be sound. It cannot refuse a `reconcile` that runs a
    month after a bucket sealed, by which time the rows may have aged out under
    a window that was ample at sealing time. That hazard belongs to *when* the
    operation runs rather than to what was declared, so the guide carries it and
    no declaration-time check can.
    """
    if seal is None or seal.on_late != "reopen":
        return
    if ladder is None or not ladder.raw_bounded:
        return
    keep = ladder.raw.keep
    if keep is None:  # pragma: no cover - raw_bounded means a finite value
        raise SeriesError("bounded raw retention requires a finite keep duration")
    needed = seal.after + _grain_width(bucket)
    if keep >= needed:
        return
    raise SeriesError(
        f"seal(on_late='reopen') needs the source rows to outlive the seal "
        f"window, but this ladder keeps raw for {keep:g}s and a {bucket.name} "
        f"bucket is not fully recomputable until {needed:g}s after it closes "
        f"({seal.after:g}s of lateness allowance plus the {bucket.name} itself). "
        "Reopening recomputes a sealed bucket from rows that would already be "
        "eligible to have gone, so it would overwrite a correct value with a "
        "smaller one and clear the correction that showed it had moved. Keep "
        f"raw for at least {needed:g}s, or retain(raw=None, ...) to keep the "
        "source rows indefinitely, or leave on_late='correct' -- the default "
        "records the difference beside the settled value instead of replacing it"
    )


def _refuse_overlap(range: Range, previous: Bucket, zone_name: str) -> None:
    """Refuse a comparison period that would reach into the primary one.

    A shift shorter than the range puts some rows in both periods, and neither
    reading is defensible: counting them on both sides inflates the comparison,
    counting them once drops them from one. Comparing March to "a week ago" is
    not a period-over-period comparison at all, so this refuses rather than
    picking a convention.

    The arithmetic mirrors what the statement does — read the bound on the
    zone's wall clock, step back a calendar unit, convert back — so the boundary
    it enforces is the boundary the SQL would produce. Whether the two agree to
    the microsecond across a clock change is a live-PostgreSQL question, but the
    guard is one period wide and does not turn on a microsecond.
    """
    tzinfo = _zone_of(zone_name)
    local = wall_clock(range.end, tzinfo)
    # A bucket is either a fixed width or a calendar unit, never both, and the
    # branch reads off which one rather than off a flag that could disagree
    # with it.
    if previous.delta is not None:
        shifted = local - previous.delta
    else:
        total = local.year * 12 + local.month - 1 - previous.months
        year, month = total // 12, total % 12 + 1
        # `interval '1 month'` clamps rather than overflowing: the 31st of a
        # month before a 30-day one is that month's last day, not its first.
        day = min(local.day, monthrange(year, month)[1])
        shifted = local.replace(year=year, month=month, day=day)
    if from_wall_clock(shifted, tzinfo) > _instant(range.start):
        raise SeriesError(
            f"compare(previous={previous.name}) overlaps the range it compares "
            f"against: one {previous.name} is shorter than {range.start} to "
            f"{range.end}, so rows would fall in both periods. Compare against a "
            f"longer period, or narrow the range to at most one {previous.name}"
        )


def _label(key: Any, *, other: bool = False, measure: str | None = None) -> str:
    if other:
        return "other"
    if key is None:
        return measure or "total"
    return str(key)


def _temporal(column: Any) -> ColumnExpr:
    if not isinstance(column, ColumnExpr):
        raise SeriesError(f"at= takes a model column such as Trek.started_at, got {column!r}")
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
    raise SeriesError(f"zone= takes an IANA name or a wreath.temporal.zone(...), got {value!r}")


def _instant(value: Any) -> Any:
    return Instant.of(value)


#: `2h`, `30m`, `90s`, `250ms`, `7d` -- the same compact spelling
#: `ChunkedPass(within=...)` takes, so one codebase has one duration syntax.
#:
#: That was aspirational until 2026-07-27: this accepted `d` and
#: `wreath._passes.duration` did not, so `seal(after="3d")` parsed while
#: `Rows(within="3d")` refused. The scales are the same set now, and
#: `tests/series/test_duration_syntax.py` asserts it rather than trusting this
#: comment -- a claim two modules apart is one edit from being false again.
_COMPACT_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _lateness(value: Any) -> float:
    """`seal(after=)` as a number of seconds.

    Takes the compact form (`"2h"`) or ISO-8601 (`"PT2H"`) or a plain number
    of seconds. Neither spelling can express months or years, which is correct
    rather than a limitation: a lateness allowance is *elapsed* time, and "one
    month after the bucket closed" is not a fixed amount of it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SeriesError(
            f"seal(after=) takes a duration like '2h' or a number of seconds, got {value!r}"
        )
    if isinstance(value, str):
        parsed = decimal_unit(value)
        if parsed is not None and (not parsed[1] or parsed[1] in _COMPACT_SCALE):
            number, unit = parsed
            seconds = float(number) * _COMPACT_SCALE[unit or "s"]
        else:
            try:
                seconds = parse_duration(value).total_seconds()
            except TemporalError:
                raise SeriesError(
                    f"seal(after={value!r}) is not a duration. Write it compactly "
                    f"('2h', '30m', '90s') or as ISO-8601 ('PT2H')"
                ) from None
    else:
        seconds = float(value)
    if seconds < 0:
        raise SeriesError(
            f"seal(after={value!r}) is negative, which would seal a bucket before it closed"
        )
    return seconds


_INSERT_SETTLED_ROWS = """
INSERT INTO "wreath"."series_buckets" (view, params, bucket, measures)
SELECT $1, $2, item.bucket, item.measures
FROM jsonb_to_recordset($3::jsonb) AS item(bucket timestamptz, measures jsonb)
ON CONFLICT (view, params, bucket) DO NOTHING
"""

_UPSERT_CORRECTION_ROWS = """
INSERT INTO "wreath"."series_corrections" (view, params, bucket, delta)
SELECT $1, $2, item.bucket, item.value
FROM jsonb_to_recordset($3::jsonb) AS item(bucket timestamptz, value jsonb)
ON CONFLICT (view, params, bucket) DO UPDATE
SET delta = EXCLUDED.delta, noticed_at = now()
"""

_REPLACE_SETTLED_ROWS = """
WITH reopened AS (
    INSERT INTO "wreath"."series_buckets" (view, params, bucket, measures)
    SELECT $1, $2, item.bucket, item.value
    FROM jsonb_to_recordset($3::jsonb) AS item(bucket timestamptz, value jsonb)
    ON CONFLICT (view, params, bucket) DO UPDATE
    SET measures = EXCLUDED.measures, settled_at = now()
    RETURNING bucket
)
DELETE FROM "wreath"."series_corrections" AS correction
USING reopened
WHERE correction.view = $1 AND correction.params = $2
  AND correction.bucket = reopened.bucket
"""


def _jsonb_row_payload(rows: Any, value_name: str) -> str:
    from ._json import dumps

    return dumps(
        [
            {"bucket": bucket.isoformat(), value_name: value}
            for bucket, value in rows
        ]
    ).decode("utf-8")


async def _insert_settled_rows(
    session: Any, view: str, params: str, rows: Any
) -> None:
    await session.declared(
        _INSERT_SETTLED_ROWS, (view, params, _jsonb_row_payload(rows, "measures"))
    )


async def _upsert_correction_rows(
    session: Any, view: str, params: str, rows: Any
) -> None:
    await session.declared(
        _UPSERT_CORRECTION_ROWS, (view, params, _jsonb_row_payload(rows, "value"))
    )


async def _replace_settled_rows(
    session: Any, view: str, params: str, rows: Any
) -> None:
    await session.declared(
        _REPLACE_SETTLED_ROWS, (view, params, _jsonb_row_payload(rows, "value"))
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    """A stored JSONB column as a dict, however the driver handed it back.

    The driver decodes `jsonb` to *text* (`_decode_value` returns
    `data.decode("utf-8")` for it), and a fake may hand back a dict. Accepting
    both keeps the storage shape a fact about PostgreSQL rather than about
    which decoder is installed.
    """
    if value is None:
        return {}
    if isinstance(value, (bytes, bytearray, str)):
        from ._json import loads

        return dict(loads(value))
    return dict(value)
