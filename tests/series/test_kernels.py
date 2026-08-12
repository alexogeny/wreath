"""Storage-neutral numeric kernels used after a series has been assembled."""

from __future__ import annotations

import datetime

import pytest

from wreath.series import (
    lttb,
    nice_ticks,
    project_chart,
    project_chart_spine,
    reconcile,
    series_path,
)
from wreath.temporal import Day, Hour, Instant, Week, spine, zone


class TestLttb:
    def test_it_keeps_endpoints_and_the_largest_excursion(self):
        x = tuple(range(9))
        y = (0, 0, 0, 0, 20, 0, 0, 0, 0)
        selected = lttb(x, y, 4)
        assert selected[0] == 0 and selected[-1] == 8
        assert 4 in selected
        assert len(selected) == 4

    def test_no_reduction_returns_every_index(self):
        assert lttb((1, 2, 3), (4, 5, 6), 4) == (0, 1, 2)

    def test_parallel_arrays_must_have_the_same_length(self):
        with pytest.raises(ValueError, match="one y value per x value"):
            lttb((1, 2), (3,), 3)

    def test_a_nonfinite_coordinate_names_its_index(self):
        with pytest.raises(ValueError, match="item 1 must be finite"):
            lttb((0, 1, 2), (0, float("nan"), 2), 3)


class TestNiceTicks:
    def test_ticks_cover_the_extent_on_a_one_two_five_step(self):
        ticks = nice_ticks(-123, 9876, 8)
        assert ticks == (-2000.0, 0.0, 2000.0, 4000.0, 6000.0, 8000.0, 10000.0)

    def test_a_zero_width_extent_is_one_tick(self):
        assert nice_ticks(4.5, 4.5) == (4.5,)

    def test_reversed_bounds_are_refused_at_the_numeric_boundary(self):
        with pytest.raises(ValueError, match="below minimum"):
            nice_ticks(2, 1)


class TestSeriesPath:
    def test_none_breaks_the_line_and_the_next_value_moves(self):
        assert series_path((0, 1, 2, 3, 4), (1, 2, None, 4, 5)) == ("M0,1L1,2M3,4L4,5")

    def test_leading_and_trailing_gaps_emit_nothing_for_the_gap(self):
        assert series_path((0, 1, 2), (None, 3, None)) == "M1,3"

    def test_parallel_arrays_must_have_the_same_length(self):
        with pytest.raises(ValueError, match="one y value per x value"):
            series_path((0,), ())

    def test_chart_precision_is_locale_independent_and_bounded(self):
        assert series_path(
            range(5),
            (1.234567891234, 0.0000123456789, 12345678912.0, -0.000000123456789, 9.999999999),
        ) == (
            "M0,1.23456789L1,1.23456789e-5L2,1.23456789e+10"
            "L3,-1.23456789e-7L4,10"
        )


class TestChartProjection:
    def test_it_matches_the_individual_data_kernels(self):
        buckets = tuple(range(9))
        sparse = {
            ("alpha", False): {
                bucket: {
                    "count": float(bucket + 1),
                    "latency": None if bucket in (2, 6) else float(bucket * bucket),
                }
                for bucket in buckets
            },
            ("beta", False): {
                bucket: {"count": float(20 - bucket), "latency": float(bucket)}
                for bucket in buckets
                if bucket % 2 == 0
            },
        }
        fills = {"count": 0.0, "latency": None}
        dense = reconcile(buckets, sparse, fills)
        x = tuple(float(index) for index in buckets)
        selected = lttb(x, tuple(float(value) for value in dense[0][2]), 4)
        expected_path = series_path(
            tuple(x[index] for index in selected),
            tuple(dense[0][2][index] for index in selected),
        )
        expected_full = series_path(x, dense[1][2])
        expected_ticks = nice_ticks(min(dense[0][2]), max(dense[0][2]), 6)

        row_count, keys, paths, ticks = project_chart(
            buckets,
            sparse,
            fills,
            downsample_rows=(0,),
            full_rows=(1,),
            threshold=4,
            tick_target=6,
        )

        assert row_count == 4
        assert keys == (("alpha", False), ("beta", False))
        assert paths == (expected_path, expected_full)
        assert ticks == (expected_ticks,)

    def test_a_row_index_names_the_compact_output_boundary(self):
        with pytest.raises(IndexError, match="outside"):
            project_chart(
                (0,),
                {("alpha", False): {}},
                {"count": 0.0},
                downsample_rows=(1,),
            )

    @pytest.mark.parametrize("width", [Hour, Day, Week])
    @pytest.mark.parametrize("zone_name", ["UTC", "Pacific/Auckland"])
    def test_range_projection_matches_a_materialized_spine(self, width, zone_name):
        timezone = zone(zone_name)
        start = Instant.of(datetime.datetime(2026, 3, 28, tzinfo=datetime.UTC))
        end = Instant.of(datetime.datetime(2026, 4, 9, tzinfo=datetime.UTC))
        buckets = spine(start, end, bucket=width, in_zone=timezone)
        sparse = {
            ("alpha", False): {
                bucket.astimezone(datetime.UTC): {
                    "count": float(index + 1),
                    "latency": None if index % 5 == 0 else index / 3,
                }
                for index, bucket in enumerate(buckets)
                if index % 3 != 0
            }
        }
        fills = {"count": 0.0, "latency": None}
        expected = project_chart(
            buckets,
            sparse,
            fills,
            downsample_rows=(0,),
            full_rows=(1,),
            threshold=16,
            tick_target=6,
        )
        assert project_chart_spine(
            start,
            end,
            bucket=width,
            in_zone=timezone,
            sparse=sparse,
            fills=fills,
            downsample_rows=(0,),
            full_rows=(1,),
            threshold=16,
            tick_target=6,
        ) == expected

    def test_range_projection_shares_duplicate_rows_across_multiple_measures(self):
        timezone = zone("Pacific/Auckland")
        start = Instant.of(datetime.datetime(2026, 3, 28, tzinfo=datetime.UTC))
        end = Instant.of(datetime.datetime(2026, 4, 9, tzinfo=datetime.UTC))
        buckets = spine(start, end, bucket=Hour, in_zone=timezone)
        sparse = {
            (name, False): {
                bucket.astimezone(datetime.UTC): {
                    "count": float(index + series),
                    "latency": None if index % 7 == 0 else index / (series + 1),
                }
                for index, bucket in enumerate(buckets)
                if index % (series + 2) != 0
            }
            for series, name in enumerate(("alpha", "beta", "gamma"), 1)
        }
        fills = {"count": 0.0, "latency": None}
        kwargs = {
            "sparse": sparse,
            "fills": fills,
            "downsample_rows": (0, 1, 2, 4),
            "full_rows": (1, 3, 4),
            "threshold": 24,
            "tick_target": 6,
        }

        assert project_chart_spine(
            start,
            end,
            bucket=Hour,
            in_zone=timezone,
            **kwargs,
        ) == project_chart(buckets, **kwargs)
