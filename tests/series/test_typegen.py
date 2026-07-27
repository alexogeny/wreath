"""Stage 6: a declaration reaches the client as a type, not as `number[][]`.

The point of the whole stage is that the measure names a declaration was
written with survive all the way into a component. These tests assert that
journey end to end -- discovery off the routes, into the IR, out as TypeScript.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.series import Aggregate, Series, avg, count, sum_
from wreath.temporal import Day, Month
from wreath.typegen import build_api_model, render_typescript
from wreath.typegen.targets.typescript import _series_module

from .conftest import Deploy, Trek

# Module-level, which is the shape the guide teaches and the shape discovery
# relies on: a declaration is written once next to the handler that runs it.
activity = (
    Series(Trek, at=Trek.started_at, bucket=Day)
    .measure(started=count(), mean_distance=avg(Trek.distance_km))
    .by(Trek.paddock_id)
    .compare(previous=Month)
    .events(Deploy, at=Deploy.happened_at, label=Deploy.version)
)

by_paddock = Aggregate(Trek).measure(treks=count(), km=sum_(Trek.distance_km)).by(
    Trek.paddock_id
)

_private = Series(Trek, at=Trek.started_at, bucket=Day).measure(started=count())


def _app() -> Wreath:
    app = Wreath()

    @app.get("/activity")
    async def herd_activity(request):  # pragma: no cover - never called
        return activity

    @app.get("/paddocks")
    async def paddocks(request):  # pragma: no cover - never called
        return by_paddock

    return app


@pytest.fixture
def api():
    return build_api_model(_app(), allow_unknown=True)


def test_declarations_are_discovered_from_the_routes(api):
    assert [shape.name for shape in api.series] == ["activity", "by_paddock"]


def test_an_underscored_declaration_is_not_part_of_the_api(api):
    # `_private` is module-level and reachable, but the leading underscore says
    # it is not public and typegen describes the public surface.
    assert "_private" not in [shape.name for shape in api.series]


def test_a_declaration_the_handler_does_not_use_is_not_emitted():
    """Visible to the module is not the same as used by the route.

    Module scope alone was the first cut of this and it swept in any
    declaration a routed module happened to import.
    """
    app = Wreath()

    @app.get("/unrelated")
    async def unrelated(request):  # pragma: no cover - never called
        return {}

    api = build_api_model(app, allow_unknown=True)
    assert api.series == ()


def test_the_shape_records_what_the_declaration_asked_for(api):
    shape = next(item for item in api.series if item.name == "activity")
    assert shape.form == "series"
    assert shape.bucket == "day"
    assert shape.grouped is True
    assert shape.compares == "month"
    assert shape.events is True
    assert [measure.name for measure in shape.measures] == ["started", "mean_distance"]


def test_an_aggregate_has_no_time_axis(api):
    shape = next(item for item in api.series if item.name == "by_paddock")
    assert shape.form == "aggregate"
    assert shape.bucket is None
    assert shape.compares is None
    assert shape.events is False


def test_fill_behaviour_reaches_the_ir(api):
    """A count fills with zero; an average of no rows stays undefined."""
    shape = next(item for item in api.series if item.name == "activity")
    fills = {measure.name: measure.fills for measure in shape.measures}
    assert fills == {"started": True, "mean_distance": False}


def test_an_explicit_fill_makes_a_measure_dense(api):
    """`.fill(mean=0)` is a decision, and it changes the generated type."""
    declaration = (
        Series(Trek, at=Trek.started_at, bucket=Day)
        .measure(mean=avg(Trek.distance_km))
        .fill(mean=0)
    )
    from wreath.typegen.inspect import _series_shape

    shape = _series_shape("filled", declaration)
    assert shape.measures[0].fills is True


# -- emission ---------------------------------------------------------------


def test_measure_names_become_field_names(api):
    module = _series_module(api)
    assert 'export type ActivityMeasure = "started" | "mean_distance";' in module
    assert 'export type ByPaddockMeasure = "treks" | "km";' in module


def test_a_measure_that_fills_has_no_nulls_in_its_values(api):
    module = _series_module(api)
    assert '(SeriesData<number> & { measure: "started" })' in module
    assert '(SeriesData<number | null> & { measure: "mean_distance" })' in module


def test_the_result_alias_is_the_generic_envelope(api):
    module = _series_module(api)
    assert "export type ActivityResult = SeriesResult<ActivitySeries>;" in module
    assert "export type ByPaddockResult = AggregateResult<ByPaddockMeasure>;" in module


def test_the_module_ships_only_when_the_app_declares_views():
    plain = Wreath()

    @plain.get("/ping")
    async def ping(request):  # pragma: no cover - never called
        return {}

    files = render_typescript(build_api_model(plain, allow_unknown=True))
    assert "series.ts" not in files
    assert 'export * from "./series";' not in files["index.ts"]


def test_the_module_ships_and_is_exported_when_they_exist(api):
    files = render_typescript(api)
    assert "series.ts" in files
    assert 'export * from "./series";' in files["index.ts"]
    assert "series.ts" in files["wreath-typegen.json"]


def test_the_envelope_field_names_match_as_dict():
    """The generated interface and the wire body are one contract.

    They are written in two places -- a TypeScript string here and a dict in
    `series.py` -- so this asserts they still agree. A renamed field that only
    moved on one side is exactly the drift the type exists to prevent.
    """
    from wreath.series import Range, SeriesData, SeriesResult
    from wreath.temporal import parse

    span = Range(parse("2026-03-01T00:00:00Z"), parse("2026-03-02T00:00:00Z"))
    body = SeriesResult(
        range=span,
        zone="UTC",
        bucket="day",
        buckets=(span.start,),
        series=(SeriesData("started", None, "all", None, "count", (1,)),),
    ).as_dict()

    module = _series_module(build_api_model(_app(), allow_unknown=True))
    interface = module[module.index("export interface SeriesResult") :]
    interface = interface[: interface.index("}")]
    declared = {
        line.strip().split(":")[0]
        for line in interface.splitlines()
        if ":" in line and not line.strip().startswith("//")
    }
    assert set(body) == declared - {"export interface SeriesResult<S = SeriesData"}
