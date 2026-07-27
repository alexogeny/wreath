"""What a declaration refuses, and when.

Every rejection here is a startup error rather than an empty or wrong chart,
which is the whole argument for a declaration being a value: the mistakes that
would otherwise surface as a plunging average or a repainted legend are made
unwritable instead.
"""

from __future__ import annotations

import pytest

from wreath.series import (
    DEFAULT_TOP,
    MAX_TOP,
    Aggregate,
    Range,
    Series,
    SeriesError,
    avg,
    count,
    max_,
    min_,
    sum_,
)
from wreath.temporal import Day, Hour
from wreath.temporal import zone as tz

from .conftest import Herd, Paddock, Trek, utc


def view(**kwargs):
    return Series(Trek, at=Trek.started_at, bucket=Day, **kwargs)


class TestBucketingColumn:
    def test_at_must_be_a_temporal_column(self):
        with pytest.raises(SeriesError, match="nothing to truncate"):
            Series(Trek, at=Trek.distance_km, bucket=Day)

    def test_at_must_be_a_column_at_all(self):
        with pytest.raises(SeriesError, match="at= takes a model column"):
            Series(Trek, at="started_at", bucket=Day)

    def test_bucket_must_come_from_the_vocabulary(self):
        with pytest.raises(SeriesError, match="bucket= takes a bucket"):
            Series(Trek, at=Trek.started_at, bucket="day")

    def test_a_timestamptz_column_is_accepted(self):
        assert view().at is Trek.started_at
        assert Series(Trek, at=Trek.started_at, bucket=Hour).bucket is Hour


class TestMeasures:
    def test_a_measure_over_text_is_refused(self):
        for build in (sum_, avg, min_, max_):
            with pytest.raises(SeriesError, match="cannot aggregate"):
                build(Trek.grade)

    def test_a_measure_needs_a_column_not_a_name(self):
        with pytest.raises(SeriesError, match="takes a model column"):
            sum_("distance_km")

    def test_count_needs_no_column(self):
        assert count().column is None

    def test_a_measure_must_be_a_measure(self):
        with pytest.raises(SeriesError, match="takes count\\(\\)"):
            view().measure(started=7)

    def test_declaring_the_same_name_twice_is_refused(self):
        with pytest.raises(SeriesError, match="declared twice"):
            view().measure(started=count()).measure(started=count())

    def test_at_least_one_measure_is_needed(self):
        with pytest.raises(SeriesError, match="at least one"):
            view().measure()

    def test_running_with_no_measures_refuses_rather_than_returning_nothing(self):
        with pytest.raises(SeriesError, match="nothing to plot"):
            import asyncio

            asyncio.run(
                view().run(None, range=Range(utc(2026, 1, 1), utc(2026, 1, 2)), zone="UTC")
            )


class TestIdentityAndFill:
    def test_counting_and_summing_carry_a_zero_identity(self):
        for measure in (count(), sum_(Trek.distance_km)):
            assert measure.has_identity and measure.identity == 0

    def test_average_minimum_and_maximum_have_no_identity(self):
        """An average of no rows is undefined, and zero is not a synonym for it."""
        for measure in (avg(Trek.distance_km), min_(Trek.distance_km), max_(Trek.distance_km)):
            assert not measure.has_identity

    def test_only_average_is_unsafe_to_roll_up(self):
        assert not avg(Trek.distance_km).rollup_safe
        for measure in (count(), sum_(Trek.distance_km), min_(Trek.distance_km)):
            assert measure.rollup_safe

    def test_filling_an_undeclared_measure_is_refused(self):
        declared = view().measure(started=count())
        with pytest.raises(SeriesError, match="names no declared measure"):
            declared.fill(distance=0)

    def test_an_explicit_fill_is_recorded(self):
        declared = view().measure(distance=avg(Trek.distance_km)).fill(distance=0)
        assert declared._d.fills == {"distance": 0}


class TestGrouping:
    def test_by_takes_a_column(self):
        with pytest.raises(SeriesError, match="by\\(\\) takes a model column"):
            view().measure(n=count()).by("paddock_id")

    def test_one_grouping_key_per_view(self):
        declared = view().measure(n=count()).by(Trek.paddock_id)
        with pytest.raises(SeriesError, match="already declared"):
            declared.by(Trek.herd_id)

    def test_top_defaults_to_the_readable_ceiling(self):
        assert view().measure(n=count()).by(Trek.paddock_id).top == DEFAULT_TOP

    @pytest.mark.parametrize("bad", [0, -1, True, 2.5, "7"])
    def test_top_must_be_a_positive_integer(self, bad):
        with pytest.raises(SeriesError, match="positive integer"):
            view().measure(n=count()).by(Trek.paddock_id, top=bad)

    def test_top_above_the_ceiling_is_refused_rather_than_silently_capped(self):
        with pytest.raises(SeriesError, match="above the ceiling"):
            view().measure(n=count()).by(Trek.paddock_id, top=MAX_TOP + 1)

    def test_the_ceiling_can_be_raised_within_reason(self):
        assert view().measure(n=count()).by(Trek.paddock_id, top=MAX_TOP).top == MAX_TOP

    def test_grouping_through_a_to_one_relation_is_allowed(self):
        declared = view().measure(n=count()).by(Trek.herd.name)
        assert declared.group.column is Trek.herd.name.column

    def test_grouping_by_a_to_many_relation_is_refused(self, session):
        """One row would land in several buckets, so every total would be wrong.

        The refusal comes from the ORM's own join planner rather than a second
        copy of the rule, which is why the message is the ORM's. It arrives at
        ``run()`` rather than at declaration: cardinality is a fact about the
        registry, and a declaration written at import time does not have one.
        """
        import asyncio

        from wreath.orm.errors import ORMError

        declared = Aggregate(Herd).measure(n=count()).by(Herd.treks.grade)
        with pytest.raises(ORMError, match="to-many relationship"):
            asyncio.run(declared.run(session))


class TestPredicates:
    def test_where_takes_predicates_not_values(self):
        with pytest.raises(SeriesError, match="takes SQL predicates"):
            view().measure(n=count()).where(True)

    def test_a_predicate_on_another_model_is_refused(self):
        with pytest.raises(Exception, match="Paddock|not a column"):
            view().measure(n=count()).where(Paddock.name == "north")


class TestSources:
    def test_sources_names_the_model_it_reads(self):
        assert view().measure(n=count()).sources == (Trek,)

    def test_a_predicate_through_a_relation_adds_that_model(self):
        declared = view().measure(n=count()).where(Trek.herd.name == "alpha")
        assert set(declared.sources) == {Trek, Herd}

    def test_grouping_through_a_relation_adds_that_model(self):
        declared = view().measure(n=count()).by(Trek.herd.name)
        assert set(declared.sources) == {Trek, Herd}


class TestRange:
    def test_a_range_is_half_open_and_must_not_be_empty(self):
        with pytest.raises(SeriesError, match="Range is empty"):
            Range(utc(2026, 1, 2), utc(2026, 1, 2))
        with pytest.raises(SeriesError, match="Range is empty"):
            Range(utc(2026, 1, 3), utc(2026, 1, 2))

    def test_a_naive_bound_is_refused_rather_than_assumed_to_be_utc(self):
        import datetime

        with pytest.raises(SeriesError, match="must carry a UTC offset"):
            Range(datetime.datetime(2026, 1, 1), utc(2026, 1, 2))


class TestLaterStagesRefuseByName:
    """Specified surface that is not built yet answers, rather than no-opping.

    A method that silently did nothing would be worse than a missing one: the
    declaration would read as though retention were configured and nothing
    would enforce it.
    """

    @pytest.mark.parametrize("method", ["archive", "drop"])
    def test_it_refuses_and_says_why(self, method):
        declared = view().measure(n=count())
        with pytest.raises(SeriesError, match="not implemented"):
            getattr(declared, method)(raw="3 days")

    def test_seal_is_built_now_and_no_longer_refuses(self):
        """Stage 7 landed; this is the entry that left the list."""
        declared = view().measure(n=count()).seal(after="2h")
        assert declared.sealed_after == 7200

    def test_retain_is_built_now_and_no_longer_refuses(self):
        """Stage 8 landed; this is the entry that left the list.

        It still destroys nothing -- ``retain`` says how long a grain stays
        warm, and the two methods that could remove anything are still above.
        """
        declared = (
            view(stored_in=tz("UTC"))
            .measure(n=count())
            .seal(after="2h")
            .retain(raw="3 days", day="1 year")
        )
        assert [tier.name for tier in declared.tiers] == ["raw", "day"]

    def test_drop_says_it_will_stay_opt_in(self):
        with pytest.raises(SeriesError, match="opt-in"):
            view().measure(n=count()).drop(raw=True)


class TestImmutability:
    def test_every_builder_method_returns_a_new_declaration(self):
        base = view()
        with_measure = base.measure(n=count())
        with_group = with_measure.by(Trek.paddock_id)
        assert base.measures == ()
        assert with_measure.group is None
        assert with_group.group is Trek.paddock_id

    def test_an_aggregate_is_immutable_too(self):
        base = Aggregate(Trek).measure(n=count())
        assert base.by(Trek.paddock_id).group is Trek.paddock_id
        assert base.group is None


class TestAggregateCeiling:
    def test_the_group_limit_is_declared_not_passed_per_request(self):
        assert Aggregate(Trek).measure(n=count()).by(Trek.paddock_id, limit=5).limit == 5

    @pytest.mark.parametrize("bad", [0, -3, True])
    def test_the_limit_must_be_a_positive_integer(self, bad):
        with pytest.raises(SeriesError, match="positive integer"):
            Aggregate(Trek).measure(n=count()).by(Trek.paddock_id, limit=bad)
