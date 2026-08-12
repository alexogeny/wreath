"""The statement a series renders, and the envelope it assembles from the rows.

The SQL assertions here are about *shape*, not text: that the spine is
generated on the local wall clock and converted back afterwards, that the
half-open range is honoured once, and that the top-N fold happens before
aggregation rather than after. Each of those is a bug that reads as a plausible
chart, which is why they are pinned by structure rather than by eye.
"""

from __future__ import annotations

import pytest

from wreath.queries import Param
from wreath.series import Range, Series, SeriesError, avg, count, reconcile, sum_
from wreath.temporal import Day, Month, zone

from .conftest import Herd, Trek, utc


def series(**kwargs):
    return Series(Trek, at=Trek.started_at, bucket=Day, **kwargs)


WEEK = Range(utc(2026, 1, 1), utc(2026, 1, 4))


async def run(view, session, database, rows, **kwargs):
    """Run *view* with *rows* scripted as the driver's answer."""
    database.connection.responses.clear()
    database.connection.script("spine", rows)
    kwargs.setdefault("range", WEEK)
    kwargs.setdefault("zone", "UTC")
    return await view.run(session, **kwargs)


def sql_of(database):
    return database.connection.calls[-1][0]


class TestTheSpine:
    async def test_it_is_generated_in_local_time_and_converted_back(
        self, session, database
    ):
        """The order is the whole trick, and getting it backwards is the DST bug.

        `generate_series` stepping over *naive* local timestamps advances by a
        calendar day, which is what a reader means by "daily". Stepping over
        `timestamptz` advances by exactly 24 hours, so the day a clock changes
        is an hour out and every boundary after it is wrong.
        """
        view = series().measure(n=count())
        await run(view, session, database, [])
        sql = sql_of(database)
        spine = sql[sql.index('"spine"') :]
        assert "generate_series(date_trunc('day', $" in spine
        assert "AT TIME ZONE" in spine.split("interval")[0], "truncated on the wall clock"
        assert '"s"."b" AT TIME ZONE' in sql, "and converted back for the payload"

    async def test_the_upper_bound_is_exclusive(self, session, database):
        """A range ending exactly on a boundary excludes the bucket starting there.

        Written once, as one microsecond subtracted before truncating, because
        the off-by-one in a chart comes from writing the boundary twice with two
        different intentions.
        """
        await run(series().measure(n=count()), session, database, [])
        assert "- interval '1 microsecond'" in sql_of(database)

    async def test_the_bucket_unit_reaches_both_halves(self, session, database):
        view = Series(Trek, at=Trek.started_at, bucket=Month).measure(n=count())
        await run(view, session, database, [])
        sql = sql_of(database)
        assert sql.count("date_trunc('month'") == 3, "assignment plus both spine bounds"
        assert "interval '1 month'" in sql

    async def test_the_range_filters_the_rows_as_well_as_the_spine(
        self, session, database
    ):
        await run(series().measure(n=count()), session, database, [])
        sql = sql_of(database)
        assert '"t0"."started_at" >= $' in sql and '"t0"."started_at" < $' in sql


class TestTopN:
    def _view(self):
        return (
            series()
            .measure(started=count(), pace=avg(Trek.distance_km))
            .by(Trek.paddock_id, top=2)
        )

    async def test_survivors_are_ranked_over_the_whole_range_not_per_bucket(
        self, session, database
    ):
        await run(self._view(), session, database, [])
        sql = sql_of(database)
        survivors = sql[sql.index('"survivors"') : sql.index('"agg"')]
        assert "date_trunc" not in survivors, "ranked across the range, not within a bucket"
        assert "ORDER BY COUNT(*) DESC NULLS LAST, 1 ASC" in survivors

    async def test_ties_break_on_the_key_so_the_survivor_set_is_stable(
        self, session, database
    ):
        """Without a deterministic tie-break, two runs of one query can differ.

        A series that appears and vanishes between refreshes is worse than one
        that is merely wrong, because nothing in the chart says it happened.
        """
        await run(self._view(), session, database, [])
        assert ", 1 ASC LIMIT" in sql_of(database)

    async def test_the_fold_happens_before_aggregation(self, session, database):
        """So the remainder's average is a real average, not an average of averages.

        Folding after aggregation is the tempting shape -- group, rank, then
        re-aggregate the tail -- and it silently produces a mean of means, which
        is not the mean of anything unless every group is the same size.
        """
        await run(self._view(), session, database, [])
        sql = sql_of(database)
        agg = sql[sql.index('"agg" AS') :]
        assert 'CASE WHEN "sv"."hit"' in agg
        assert 'AVG("t0"."distance_km")' in agg, "aggregating base rows, not group results"
        assert agg.count("AVG(") == 1, "and aggregating them exactly once"

    async def test_a_folded_tail_carries_the_reserved_null_key(self, session, database):
        rows = [
            (utc(2026, 1, 1), 10, False, 5, 2.0),
            (utc(2026, 1, 1), None, True, 3, 1.0),
        ]
        result = await run(self._view(), session, database, rows)
        other = [item for item in result.series if item.other]
        assert {item.key for item in other} == {None}
        assert {item.label for item in other} == {"other"}

    async def test_a_genuinely_null_group_stays_distinct_from_the_fold(
        self, session, database
    ):
        """Both carry a null key, so `other` is what tells them apart.

        A grouping column that is nullable will produce a null-keyed series of
        its own, and merging it into the remainder would silently add the
        unassigned rows to the tail.
        """
        rows = [
            (utc(2026, 1, 1), None, False, 4, 1.0),   # paddock_id IS NULL, survived
            (utc(2026, 1, 1), None, True, 9, 3.0),    # the folded tail
        ]
        result = await run(self._view(), session, database, rows)
        counts = {(item.key, item.other): item.values for item in result.series
                  if item.measure == "started"}
        assert counts[(None, False)] == (4,)
        assert counts[(None, True)] == (9,)

    async def test_the_survivor_marker_matches_nulls_to_nulls(self, session, database):
        await run(self._view(), session, database, [])
        assert 'IS NOT DISTINCT FROM "t0"."paddock_id"' in sql_of(database)


class TestFill:
    def test_plain_iterables_use_the_same_dense_sparse_kernel(self):
        buckets = (item for item in ("mon", "tue", "wed"))
        sparse = {
            ("north", False): {
                "mon": {"count": 3, "mean": 1.5},
                "wed": {"count": None, "mean": 2.5},
            },
            (None, True): {"tue": {"count": 4}},
        }
        assert reconcile(buckets, sparse, {"count": 0, "mean": None}) == (
            (("north", False), "count", (3, 0, 0)),
            (("north", False), "mean", (1.5, None, 2.5)),
            ((None, True), "count", (0, 4, 0)),
            ((None, True), "mean", (None, None, None)),
        )

    def test_a_malformed_sparse_bucket_names_the_required_shape(self):
        with pytest.raises(TypeError, match="values must be a dict keyed by bucket"):
            reconcile((1,), {(None, False): []}, {"count": 0})

    async def test_a_count_fills_an_empty_bucket_with_zero(self, session, database):
        rows = [
            (utc(2026, 1, 1), 3),
            (utc(2026, 1, 2), None),
            (utc(2026, 1, 3), 5),
        ]
        result = await run(series().measure(n=count()), session, database, rows)
        assert result.series[0].values == (3, 0, 5)

    async def test_an_average_fills_with_none_so_the_renderer_draws_a_gap(
        self, session, database
    ):
        """An average of no rows is undefined; zero is not a synonym for it.

        Filling it with zero draws a line plunging to the floor on every quiet
        day, which reads as a collapse in the thing being measured rather than
        as an absence of it.
        """
        rows = [
            (utc(2026, 1, 1), 4.0),
            (utc(2026, 1, 2), None),
            (utc(2026, 1, 3), 6.0),
        ]
        view = series().measure(pace=avg(Trek.distance_km))
        result = await run(view, session, database, rows)
        assert result.series[0].values == (4.0, None, 6.0)

    async def test_an_explicit_fill_overrides_the_identity(self, session, database):
        rows = [(utc(2026, 1, 1), 4.0), (utc(2026, 1, 2), None)]
        view = series().measure(pace=avg(Trek.distance_km)).fill(pace=0)
        result = await run(view, session, database, rows)
        assert result.series[0].values == (4.0, 0)

    async def test_each_measure_fills_by_its_own_rule(self, session, database):
        rows = [(utc(2026, 1, 1), 2, 3.0), (utc(2026, 1, 2), None, None)]
        view = series().measure(n=count(), pace=avg(Trek.distance_km))
        result = await run(view, session, database, rows)
        by_name = {item.measure: item.values for item in result.series}
        assert by_name["n"] == (2, 0)
        assert by_name["pace"] == (3.0, None)


class TestEnvelope:
    async def test_every_bucket_in_the_range_is_present_even_when_empty(
        self, session, database
    ):
        rows = [(utc(2026, 1, 1), 1), (utc(2026, 1, 2), None), (utc(2026, 1, 3), 2)]
        result = await run(series().measure(n=count()), session, database, rows)
        assert result.buckets == (utc(2026, 1, 1), utc(2026, 1, 2), utc(2026, 1, 3))
        assert len(result) == 3

    async def test_two_measures_are_two_named_series_never_one(self, session, database):
        """Merging them is the dual-axis trap: two units, one pair of axes.

        The alignment of the two scales is arbitrary, so the chart invents a
        correlation that is not in the data. Keeping them separate and labelled
        means a renderer that wants them together *can* -- by facetting, or by
        indexing both to a common base.
        """
        rows = [(utc(2026, 1, 1), 2, 8.0)]
        view = series().measure(
            started=count(), distance=sum_(Trek.distance_km, unit="km")
        )
        result = await run(view, session, database, rows)
        assert len(result.series) == 2
        by_name = {item.measure: item for item in result.series}
        assert by_name["started"].kind == "count" and by_name["started"].unit is None
        assert by_name["distance"].kind == "sum" and by_name["distance"].unit == "km"

    async def test_a_series_is_identified_by_its_key_not_its_position(
        self, session, database
    ):
        """A filter change that drops one group must not repaint the others.

        A reader who learned that paddock 10 is the blue line should still be
        looking at paddock 10 after someone narrows the date range.
        """
        view = series().measure(n=count()).by(Trek.paddock_id, top=3)
        both = await run(
            view, session, database,
            [(utc(2026, 1, 1), 7, False, 1), (utc(2026, 1, 1), 10, False, 4)],
        )
        one = await run(view, session, database, [(utc(2026, 1, 1), 10, False, 4)])
        surviving = next(item for item in one.series if item.key == 10)
        original = next(item for item in both.series if item.key == 10)
        assert surviving.key == original.key == 10
        assert surviving.label == original.label == "10"

    async def test_the_envelope_records_the_range_zone_and_bucket_it_used(
        self, session, database
    ):
        result = await run(
            series().measure(n=count()), session, database, [],
            zone="Pacific/Auckland",
        )
        assert result.zone == "Pacific/Auckland"
        assert result.bucket == "day"
        assert result.range is WEEK

    async def test_an_empty_spine_row_does_not_invent_a_series(self, session, database):
        """A bucket where nothing matched establishes the bucket and nothing else.

        Grouped, such a row arrives with a null key and null measures. Treating
        it as a series would put an unnamed empty line in every legend.
        """
        rows = [
            (utc(2026, 1, 1), 10, False, 4),
            (utc(2026, 1, 2), None, None, None),
        ]
        view = series().measure(n=count()).by(Trek.paddock_id)
        result = await run(view, session, database, rows)
        assert [item.key for item in result.series] == [10]
        assert result.series[0].values == (4, 0), "and the empty bucket still fills"


class TestZone:
    async def test_the_readers_zone_reaches_the_statement(self, session, database):
        await run(
            series().measure(n=count()), session, database, [], zone="Pacific/Auckland"
        )
        _sql, args = database.connection.calls[-1]
        assert "Pacific/Auckland" in args

    async def test_stored_in_is_the_default_when_a_reader_names_none(
        self, session, database
    ):
        view = series(stored_in=zone("Pacific/Auckland")).measure(n=count())
        database.connection.responses.clear()
        result = await view.run(session, range=WEEK)
        assert result.zone == "Pacific/Auckland"

    async def test_a_view_with_no_zone_at_all_refuses(self, session, database):
        with pytest.raises(SeriesError, match="no time zone"):
            await series().measure(n=count()).run(session, range=WEEK)

    async def test_a_zone_object_or_a_name_both_work(self, session, database):
        for value in ("Europe/London", zone("Europe/London")):
            result = await run(
                series().measure(n=count()), session, database, [], zone=value
            )
            assert result.zone == "Europe/London"


class TestParameters:
    async def test_a_param_binds_per_call(self, session, database):
        view = series().measure(n=count()).where(Trek.herd_id == Param("herd"))
        await run(view, session, database, [], herd=42)
        _sql, args = database.connection.calls[-1]
        assert 42 in args

    async def test_a_missing_parameter_is_named(self, session, database):
        view = series().measure(n=count()).where(Trek.herd_id == Param("herd"))
        with pytest.raises(TypeError, match="missing parameter 'herd'"):
            await run(view, session, database, [])

    async def test_an_unexpected_parameter_is_named(self, session, database):
        view = series().measure(n=count())
        with pytest.raises(TypeError, match="unexpected parameter 'paddock'"):
            await run(view, session, database, [], paddock=1)

    async def test_a_predicate_through_a_relation_joins(self, session, database):
        view = series().measure(n=count()).where(Trek.herd.name == "alpha")
        await run(view, session, database, [])
        assert "INNER JOIN" in sql_of(database)
        assert Herd in view.sources


class TestRunArguments:
    async def test_a_range_is_required_and_must_be_a_range(self, session, database):
        with pytest.raises(SeriesError, match="needs range=Range"):
            await series().measure(n=count()).run(
                session, range=(utc(2026, 1, 1), utc(2026, 1, 2)), zone="UTC"
            )
