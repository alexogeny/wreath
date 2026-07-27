"""Stage 6: a build-time chart drawn from a declaration's own numbers.

`_docs/charts.py` already rendered an SVG bar chart from a JSON file. A
calculated view writes that file, so the docs chart and the API chart are the
same numbers rather than two hand-maintained copies.
"""

from __future__ import annotations

import pytest

from wreath import _json
from wreath._docs import charts
from wreath.series import (
    AggregateResult,
    AggregateRow,
    Range,
    SeriesData,
    SeriesResult,
)
from wreath.temporal import parse


def _result(**changes):
    span = Range(parse("2026-03-01T00:00:00+13:00"), parse("2026-03-04T00:00:00+13:00"))
    buckets = tuple(
        parse(f"2026-03-0{day}T00:00:00+13:00") for day in (1, 2, 3)
    )
    base = dict(
        range=span,
        zone="Pacific/Auckland",
        bucket="day",
        buckets=buckets,
        series=(
            SeriesData("started", None, "all", None, "count", (3, 5, 4)),
            SeriesData("mean_km", None, "all", "km", "average", (1.5, None, 2.5)),
        ),
    )
    return SeriesResult(**{**base, **changes})


def _pairs(document, config):
    return charts._pairs(document, config)


# -- series -----------------------------------------------------------------


def test_a_series_result_plots_bucket_against_value():
    pairs = _pairs(_result().as_dict(), {"measure": "started"})
    assert pairs == [("2026-03-01", 3.0), ("2026-03-02", 5.0), ("2026-03-03", 4.0)]


def test_a_day_bucket_is_labelled_by_its_date_not_its_instant():
    """An ISO instant is exact and unreadable on an axis."""
    (label, _value) = _pairs(_result().as_dict(), {"measure": "started"})[0]
    assert label == "2026-03-01"
    assert "T" not in label


def test_a_sub_day_bucket_keeps_its_time():
    document = _result(bucket="hour").as_dict()
    (label, _value) = _pairs(document, {"measure": "started"})[0]
    assert label == "2026-03-01 00:00"


def test_the_first_measure_is_the_default():
    assert _pairs(_result().as_dict(), {}) == _pairs(
        _result().as_dict(), {"measure": "started"}
    )


def test_a_null_value_is_a_gap_not_a_zero():
    """An average of no rows is undefined; drawing it as zero is a lie."""
    pairs = _pairs(_result().as_dict(), {"measure": "mean_km"})
    assert pairs == [("2026-03-01", 1.5), ("2026-03-03", 2.5)]


def test_naming_a_measure_the_view_does_not_have_is_an_error():
    with pytest.raises(charts._ChartError) as caught:
        _pairs(_result().as_dict(), {"measure": "distance"})
    assert "started" in str(caught.value) and "mean_km" in str(caught.value)


def test_a_truncated_file_is_reported_rather_than_plotted():
    document = _result().as_dict()
    document["buckets"] = document["buckets"][:2]
    with pytest.raises(charts._ChartError) as caught:
        _pairs(document, {"measure": "started"})
    assert "disagree" in str(caught.value)


def test_a_grouped_view_picks_one_line_by_label():
    document = _result(
        series=(
            SeriesData("started", 1, "north", None, "count", (1, 2, 3)),
            SeriesData("started", 2, "south", None, "count", (9, 8, 7)),
        )
    ).as_dict()
    assert _pairs(document, {"series": "south"})[0] == ("2026-03-01", 9.0)


def test_naming_a_series_that_is_not_there_is_an_error():
    with pytest.raises(charts._ChartError):
        _pairs(_result().as_dict(), {"series": "north"})


# -- aggregate --------------------------------------------------------------


def test_an_aggregate_result_plots_label_against_measure():
    document = AggregateResult(
        rows=(
            AggregateRow(1, "north", {"treks": 12, "km": 40.5}),
            AggregateRow(2, "south", {"treks": 7, "km": 18.0}),
        ),
        measures=("treks", "km"),
    ).as_dict()
    assert _pairs(document, {"measure": "km"}) == [("north", 40.5), ("south", 18.0)]
    assert _pairs(document, {}) == [("north", 12.0), ("south", 7.0)]


def test_an_unknown_aggregate_measure_is_an_error():
    document = AggregateResult(
        rows=(AggregateRow(1, "north", {"treks": 12}),), measures=("treks",)
    ).as_dict()
    with pytest.raises(charts._ChartError):
        _pairs(document, {"measure": "km"})


# -- the existing path is untouched -----------------------------------------


def test_a_plain_mapping_still_plots_as_it_always_did():
    assert _pairs({"a": 1, "b": 2.5}, {}) == [("a", 1.0), ("b", 2.5)]


def test_a_list_of_records_still_plots_as_it_always_did():
    node = [{"framework": "wreath", "rps": 90000}, {"framework": "other", "rps": 40000}]
    pairs = _pairs(node, {"x": "framework", "y": "rps"})
    assert pairs == [("wreath", 90000.0), ("other", 40000.0)]


def test_a_chart_error_renders_as_a_message_not_a_build_failure(tmp_path):
    """A docs build should say what is wrong, not stop."""
    path = tmp_path / "activity.json"
    # The encoder a real job would use: `as_dict` keeps Instants, and
    # `wreath._json` is what renders them as ISO-8601.
    path.write_bytes(_json.dumps(_result().as_dict()))
    html = charts._render(
        {"source": "activity.json", "measure": "nope"}, tmp_path, None
    )
    assert "chart-error" in html and "nope" in html


def test_a_series_file_renders_an_svg(tmp_path):
    path = tmp_path / "activity.json"
    # The encoder a real job would use: `as_dict` keeps Instants, and
    # `wreath._json` is what renders them as ISO-8601.
    path.write_bytes(_json.dumps(_result().as_dict()))
    html = charts._render(
        {"source": "activity.json", "measure": "started", "title": "Treks"},
        tmp_path,
        None,
    )
    assert "<svg" in html and "2026-03-01" in html and "chart-error" not in html
