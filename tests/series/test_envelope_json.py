from __future__ import annotations

import pytest

from wreath._json import dumps, loads
from wreath.response import JSONResponse
from wreath.series import (
    Aggregate,
    AggregateResult,
    AggregateRow,
    Range,
    Series,
    SeriesComparison,
    SeriesData,
    SeriesEvent,
    SeriesResult,
    avg,
    count,
    sum_,
)
from wreath.temporal import Day, parse

from .conftest import Trek


def _span() -> Range:
    return Range(parse("2026-03-01T00:00:00Z"), parse("2026-03-03T00:00:00Z"))


def _full() -> SeriesResult:
    span = _span()
    line = SeriesData("started", 1, "north", None, "count", (1, 2))
    return SeriesResult(
        range=span,
        zone="UTC",
        bucket="day",
        buckets=(span.start, span.end),
        series=(line,),
        comparison=SeriesComparison("month", (span.start,), (line,)),
        events=(SeriesEvent(span.start, span.start, "v2 deployed"),),
    )


def test_a_result_encodes_as_json():
    body = loads(dumps(_full().as_dict()))
    assert body["zone"] == "UTC"
    assert body["bucket"] == "day"
    assert body["series"][0]["measure"] == "started"


def test_instants_encode_as_iso_strings():
    body = loads(dumps(_full().as_dict()))
    assert body["range"]["start"].startswith("2026-03-01T00:00:00")
    assert isinstance(body["buckets"][0], str)
    assert isinstance(body["events"][0]["at"], str)


def test_a_response_can_carry_the_result():
    assert JSONResponse(_full().as_dict()).body


def test_the_result_can_be_returned_directly():
    assert loads(dumps(_full())) == loads(dumps(_full().as_dict()))


def test_an_ordinary_dataclass_is_still_refused():
    from dataclasses import dataclass as plain

    @plain
    class Secretive:
        token: str = "hunter2"

    with pytest.raises(TypeError):
        dumps(Secretive())


def test_absent_comparison_and_events_are_present_and_empty():
    span = _span()
    body = SeriesResult(
        range=span, zone="UTC", bucket="day", buckets=(span.start,), series=()
    ).as_dict()
    assert body["comparison"] is None
    assert body["events"] == []


def test_an_aggregate_encodes_too():
    body = loads(
        dumps(
            AggregateResult(
                rows=(AggregateRow(1, "north", {"treks": 3}),), measures=("treks",)
            ).as_dict()
        )
    )
    assert body == {
        "rows": [{"key": 1, "label": "north", "values": {"treks": 3}}],
        "measures": ["treks"],
    }


def test_the_folded_remainder_keeps_its_flag_through_json():
    span = _span()
    body = SeriesResult(
        range=span,
        zone="UTC",
        bucket="day",
        buckets=(span.start,),
        series=(SeriesData("n", None, "other", None, "count", (1,), other=True),),
    ).as_dict()
    assert body["series"][0]["other"] is True
    assert body["series"][0]["key"] is None


def test_a_declaration_exposes_the_columns_it_aggregates_and_groups_by():
    view = (
        Series(Trek, at=Trek.started_at, bucket=Day)
        .where(Trek.distance_km > 5)
        .measure(n=count(), km=sum_(Trek.distance_km), mean=avg(Trek.distance_km))
        .by(Trek.paddock_id)
    )
    roles = [(role, column.column.python_name) for role, column in view.declared_columns]
    assert ("time", "started_at") in roles
    assert ("aggregate", "distance_km") in roles
    assert ("group", "paddock_id") in roles


def test_count_names_no_column_because_it_touches_none():
    view = Series(Trek, at=Trek.started_at, bucket=Day).measure(n=count())
    assert [role for role, _column in view.declared_columns] == ["time"]


def test_an_aggregate_exposes_its_group_and_has_no_time_column():
    view = Aggregate(Trek).measure(n=count()).by(Trek.paddock_id)
    assert [role for role, _column in view.declared_columns] == ["group"]


def test_filters_are_exposed_as_predicates_not_as_bare_columns():
    view = Series(Trek, at=Trek.started_at, bucket=Day).where(Trek.distance_km > 5)
    assert len(view.predicates) == 1
    assert "distance_km" not in [
        column.column.python_name for _role, column in view.declared_columns
    ]
