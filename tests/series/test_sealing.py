from __future__ import annotations

import json

import pytest

from wreath._series.settle import (
    Seal,
    difference,
    fold,
    params_key,
    schema_sql,
    view_key,
    watermark,
)
from wreath.series import Range, Series, SeriesError, avg, count, sum_
from wreath.temporal import Day, Hour, parse
from wreath.temporal import zone as tz

from .conftest import Trek, utc


def view(**kwargs):
    return Series(Trek, at=Trek.started_at, bucket=Day, **kwargs)


def sealed(after="2h", **kwargs):
    """A sealed view stores its zone rather than taking one per request.

    That is the design's point rather than a test convenience: a materialised
    Auckland day cannot be re-cut into a London day after the fact, so the zone
    a bucket was settled in is part of what it is.
    """
    kwargs.setdefault("stored_in", tz("UTC"))
    return view(**kwargs).measure(n=count()).seal(after=after)


class TestWatermark:
    def test_a_bucket_is_open_until_its_end_plus_the_allowance(self):
        edge = watermark(parse("2026-03-04T01:59:00Z"), bucket=Day, zone_name="UTC", after=7200)
        assert edge == utc(2026, 3, 3), "the 3rd is still open at 01:59 on the 4th"

    def test_and_sealed_once_the_allowance_has_passed(self):
        edge = watermark(parse("2026-03-04T02:00:00Z"), bucket=Day, zone_name="UTC", after=7200)
        assert edge == utc(2026, 3, 4), "the 3rd sealed; the 4th is now the open one"

    def test_the_allowance_is_elapsed_time_not_wall_clock(self):
        assert Seal(after=7200).after == 7200

    def test_a_zone_decides_when_the_day_closed(self):
        auckland = watermark(
            parse("2026-03-04T00:00:00Z"),
            bucket=Day,
            zone_name="Pacific/Auckland",
            after=0,
        )
        utc_edge = watermark(parse("2026-03-04T00:00:00Z"), bucket=Day, zone_name="UTC", after=0)
        assert auckland != utc_edge

    def test_an_hourly_view_seals_far_more_often_than_a_daily_one(self):
        at = parse("2026-03-04T05:30:00Z")
        assert watermark(at, bucket=Hour, zone_name="UTC", after=0) == utc(2026, 3, 4, 5)
        assert watermark(at, bucket=Day, zone_name="UTC", after=0) == utc(2026, 3, 4)


class TestDeclaration:
    def test_the_allowance_reads_in_the_compact_house_spelling(self):
        assert view().measure(n=count()).seal(after="2h").sealed_after == 7200
        assert view().measure(n=count()).seal(after="30m").sealed_after == 1800
        assert view().measure(n=count()).seal(after=90).sealed_after == 90

    def test_iso_8601_works_too(self):
        assert view().measure(n=count()).seal(after="PT2H").sealed_after == 7200

    def test_a_negative_allowance_is_refused(self):
        with pytest.raises(SeriesError, match="before it closed"):
            view().measure(n=count()).seal(after=-1)

    def test_nonsense_names_both_spellings(self):
        with pytest.raises(SeriesError, match="ISO-8601"):
            view().measure(n=count()).seal(after="soon")

    def test_on_late_takes_two_answers(self):
        assert view().measure(n=count()).seal(after="2h", on_late="reopen")
        with pytest.raises(ValueError, match="correct"):
            view().measure(n=count()).seal(after="2h", on_late="maybe")

    def test_a_second_seal_is_refused(self):
        with pytest.raises(SeriesError, match="already declared"):
            view().measure(n=count()).seal(after="2h").seal(after="4h")

    def test_a_view_that_seals_nothing_reports_none(self):
        assert view().measure(n=count()).sealed_after is None


class TestWhatCannotBeSealed:
    def test_a_grouped_view_is_refused_because_the_fold_moves(self):
        declared = view().measure(n=count()).by(Trek.paddock_id)
        with pytest.raises(SeriesError, match="ranked over the whole range"):
            declared.seal(after="2h")

    def test_a_comparison_is_refused_because_it_is_a_second_range(self):
        declared = view().measure(n=count()).compare(previous=Day)
        with pytest.raises(SeriesError, match="second range"):
            declared.seal(after="2h")

    def test_an_aggregate_says_it_has_no_buckets_to_close(self):
        from wreath.series import Aggregate

        with pytest.raises(SeriesError, match="no buckets to close"):
            Aggregate(Trek).measure(n=count()).seal(after="2h")


class TestReopenNeedsTheRowsItRecomputesFrom:
    """§7.2: ``on_late="reopen"`` against a raw window that cannot outlive the seal.

    Reopening *overwrites* the settled value with a recomputation, and clears
    the correction that would have shown something moved. Doing that from rows
    that have aged out replaces a correct number with a smaller one and leaves
    no trace. Stage 7 could not check this — retention did not exist — so the
    rule sat in the docstring until ``retain()`` landed.
    """

    def reopening(self, *, bucket=Day, after="2h", **windows):
        declared = Series(Trek, at=Trek.started_at, bucket=bucket, stored_in=tz("UTC")).measure(
            n=count()
        )
        return declared.seal(after=after, on_late="reopen").retain(**windows)

    def test_a_raw_window_shorter_than_the_seal_is_refused(self):
        with pytest.raises(SeriesError, match="outlive the seal window"):
            self.reopening(raw="1 day")

    def test_the_refusal_names_both_windows_and_the_way_out(self):
        with pytest.raises(SeriesError) as caught:
            self.reopening(raw="1 day")
        message = str(caught.value)
        assert "86400s" in message and "93600s" in message
        assert "retain(raw=None" in message
        assert "on_late='correct'" in message

    def test_keeping_raw_forever_is_always_sound(self):
        assert self.reopening(raw=None).sealed_after == 7200

    def test_a_comfortable_window_is_accepted(self):
        assert self.reopening(raw="30 days").sealed_after == 7200

    def test_the_default_on_late_is_not_refused(self):
        declared = Series(Trek, at=Trek.started_at, bucket=Day, stored_in=tz("UTC")).measure(
            n=count()
        )
        assert declared.seal(after="2h").retain(raw="1 day").sealed_after == 7200

    def test_the_check_does_not_depend_on_clause_order(self):
        declared = Series(Trek, at=Trek.started_at, bucket=Day, stored_in=tz("UTC")).measure(
            n=count()
        )
        with pytest.raises(SeriesError, match="outlive the seal window"):
            declared.retain(raw="1 day").seal(after="2h", on_late="reopen")

    def test_the_bucket_width_is_part_of_the_requirement(self):
        with pytest.raises(SeriesError, match="outlive the seal window"):
            self.reopening(bucket=Day, after="2h", raw="3 hours")
        # The same numbers against an hour bucket need only 3h, and are fine.
        assert self.reopening(bucket=Hour, after="2h", raw="3 hours").sealed_after == 7200

    def test_equality_is_accepted_because_coverage_is_inclusive(self):
        exact = 7200.0 + 86400.0
        assert self.reopening(raw=exact).sealed_after == 7200
        with pytest.raises(SeriesError, match="outlive the seal window"):
            self.reopening(raw=exact - 1)


class TestWhatASettledRowIsFiledUnder:
    def test_two_declarations_that_compute_the_same_thing_share_a_key(self):
        one = sealed()._identity("UTC", {})
        two = sealed()._identity("UTC", {})
        assert one == two

    def test_changing_what_is_measured_mints_a_new_key(self):
        one = view().measure(n=count()).seal(after="2h")._identity("UTC", {})
        two = (view().measure(n=count(), km=sum_(Trek.distance_km)).seal(after="2h"))._identity(
            "UTC", {}
        )
        assert one[0] != two[0]

    def test_so_does_changing_the_zone(self):
        assert sealed()._identity("UTC", {})[0] != sealed()._identity("Pacific/Auckland", {})[0]

    def test_so_does_changing_a_filter(self):
        one = sealed()._identity("UTC", {})
        two = (view().measure(n=count()).where(Trek.distance_km > 5).seal(after="2h"))._identity(
            "UTC", {}
        )
        assert one[0] != two[0]

    def test_bound_parameters_are_part_of_the_key(self):
        assert params_key({"herd": 1}) != params_key({"herd": 2})
        assert params_key({}) == ""

    def test_the_key_is_stable_across_processes(self):
        assert view_key(
            model=Trek,
            at_column="started_at",
            bucket=Day,
            zone_name="UTC",
            measures=(),
            predicate_sql="",
            fills={},
        ) == view_key(
            model=Trek,
            at_column="started_at",
            bucket=Day,
            zone_name="UTC",
            measures=(),
            predicate_sql="",
            fills={},
        )


class TestCorrectionArithmetic:
    def test_an_additive_measure_folds_by_adding(self):
        assert fold({"n": 10}, {"n": 3}) == {"n": 13}

    def test_a_non_additive_measure_carries_the_replacement(self):
        assert fold({"mean": 4.0}, {"mean": {"set": 4.5}}) == {"mean": 4.5}

    def test_no_correction_reads_as_the_settled_value(self):
        assert fold({"n": 10}, None) == {"n": 10}

    def test_a_quiet_reconcile_records_nothing(self):
        measures = view().measure(n=count())._d.measures
        assert difference({"n": 10}, {"n": 10}, measures) is None

    def test_a_difference_is_what_makes_the_fold_come_out_right(self):
        measures = view().measure(n=count())._d.measures
        delta = difference({"n": 10}, {"n": 13}, measures)
        assert delta is not None
        assert fold({"n": 10}, delta) == {"n": 13}

    def test_an_average_differences_as_a_replacement(self):
        measures = view().measure(mean=avg(Trek.distance_km))._d.measures
        delta = difference({"mean": 4.0}, {"mean": 4.5}, measures)
        assert delta == {"mean": {"set": 4.5}}
        assert fold({"mean": 4.0}, delta) == {"mean": 4.5}


class TestSchema:
    def test_both_tables_are_emitted_for_a_migration_to_apply(self):
        sql = schema_sql()
        assert "series_buckets" in sql and "series_corrections" in sql

    def test_a_settled_row_is_keyed_by_view_params_and_bucket(self):
        assert "PRIMARY KEY (view, params, bucket)" in schema_sql()

    def test_the_view_itself_never_applies_it(self):
        import wreath.series as module

        source = module.__file__ or ""
        assert source, "expected a real module file"
        text = open(source).read()
        assert "schema_sql" not in text, "series.py must not execute the DDL"

    def test_a_settled_row_never_expires(self):
        columns = schema_sql().lower()
        for expiry in ("expires", "expires_at", " ttl ", "valid_until"):
            assert expiry not in columns, f"{expiry!r} would make this a cache"


def _rows(*pairs):
    """Spine rows as the compiled statement returns them: (bucket, measure...)."""
    return [(bucket, value) for bucket, value in pairs]


class TestReadingASealedView:
    """The whole point: a sealed bucket is computed once, then read.

    The fake driver answers `spine` for the compiled series statement and
    `series_buckets` for the settled read, so these assert on which statements
    ran as much as on the numbers that came back.
    """

    @pytest.fixture
    def declared(self):
        return sealed(after="0s")

    async def test_the_first_read_computes_the_sealed_part_and_writes_nothing(
        self, declared, session, database
    ):
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 5)))
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert not any(
            keyword in sql.upper()
            for sql in statements
            for keyword in ("INSERT", "UPDATE", "DELETE")
        ), statements
        assert result.series[0].values == (5,), "the value is unchanged by not storing it"

    async def test_settling_is_what_stores_it(self, declared, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 5)))
        written = await declared.settle(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert any("INSERT INTO" in sql and "series_buckets" in sql for sql in statements)
        assert written == (utc(2026, 3, 1),)

    async def test_settling_many_buckets_uses_one_write_statement(
        self, declared, session, database
    ):
        database.connection.script(
            "generate_series",
            _rows((utc(2026, 3, 1), 5), (utc(2026, 3, 2), 7)),
        )
        written = await declared.settle(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 3)),
            now=utc(2026, 3, 4),
        )
        writes = [
            sql
            for sql, _args in database.connection.calls
            if "INSERT INTO" in sql and "series_buckets" in sql
        ]
        assert written == (utc(2026, 3, 1), utc(2026, 3, 2))
        assert len(writes) == 1

    async def test_settling_the_same_range_twice_writes_once(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        written = await declared.settle(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert written == ()
        assert not any("INSERT INTO" in sql for sql in statements), statements

    async def test_settling_an_open_view_is_refused_by_name(self, session):
        with pytest.raises(SeriesError, match="settle\\(\\) needs a seal"):
            await view(stored_in=tz("UTC")).settle(
                session, range=Range(utc(2026, 3, 1), utc(2026, 3, 2))
            )

    async def test_a_settled_bucket_is_read_rather_than_recomputed(
        self, declared, session, database
    ):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert not any("generate_series" in sql for sql in statements), (
            "a fully settled range must not touch the source table"
        )
        assert result.series[0].values == (5,)

    async def test_the_open_tail_is_always_recomputed(self, declared, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 4), 2)))
        await declared.run(
            session,
            range=Range(utc(2026, 3, 4), utc(2026, 3, 5)),
            now=utc(2026, 3, 4, 12),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert any("generate_series" in sql for sql in statements)
        assert not any("INSERT INTO" in sql for sql in statements), (
            "an open bucket must never be settled"
        )

    async def test_the_envelope_says_where_the_watermark_fell(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        assert result.state is not None
        assert result.state.sealed_through == utc(2026, 3, 4)
        assert result.state.settled == (utc(2026, 3, 1),)

    async def test_a_view_with_no_seal_carries_no_state(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 5)))
        result = (
            await view(stored_in=tz("UTC"))
            .measure(n=count())
            .run(session, range=Range(utc(2026, 3, 1), utc(2026, 3, 2)))
        )
        assert result.state is None, "a caller who never seals never has to check"


class TestTheWriteThatArrivesLate:
    """Neither ignored, nor allowed to silently rewrite a settled number."""

    @pytest.fixture
    def declared(self):
        return sealed(after="0s")

    async def test_a_reconcile_records_the_difference(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 7)))
        moved = await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        assert moved == (utc(2026, 3, 1),)
        written = [
            args
            for sql, args in database.connection.calls
            if "series_corrections" in sql and "INSERT" in sql
        ]
        assert written, "a late write must leave a record"
        # Compared after decoding, because what goes on the wire is JSON *text*:
        # the driver has no encoder for `dict` and refuses one outright. This
        # assertion used to compare against a mapping, which passed only because
        # the fake accepted a parameter PostgreSQL never would.
        payload = json.loads(written[0][2])
        assert payload[0]["value"] == {"n": 2}, "the delta, not the new total"

    async def test_reconcile_batches_corrections_for_many_buckets(
        self, declared, session, database
    ):
        database.connection.script(
            "series_buckets",
            [
                (utc(2026, 3, 1), {"n": 5}, None),
                (utc(2026, 3, 2), {"n": 6}, None),
            ],
        )
        database.connection.script(
            "generate_series",
            _rows((utc(2026, 3, 1), 7), (utc(2026, 3, 2), 9)),
        )
        moved = await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 3)),
            now=utc(2026, 3, 4),
        )
        writes = [
            sql
            for sql, _args in database.connection.calls
            if "series_corrections" in sql and "INSERT" in sql
        ]
        assert moved == (utc(2026, 3, 1), utc(2026, 3, 2))
        assert len(writes) == 1

    async def test_the_settled_value_itself_is_never_rewritten(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 7)))
        await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        updates = [
            sql
            for sql, _args in database.connection.calls
            if "series_buckets" in sql and "DO UPDATE" in sql
        ]
        assert not updates, "correct= keeps the settled value immutable"

    async def test_a_correction_is_folded_in_when_the_series_is_read(
        self, declared, session, database
    ):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, {"n": 2})])
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        assert result.series[0].values == (7,), "5 settled plus a 2 that arrived late"

    async def test_and_the_envelope_says_which_buckets_carry_one(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, {"n": 2})])
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        assert result.state.corrections == (utc(2026, 3, 1),), (
            "late data arriving should look like late data arriving"
        )

    async def test_reopen_replaces_the_settled_value_instead(self, session, database):
        declared = view(stored_in=tz("UTC")).measure(n=count()).seal(after="0s", on_late="reopen")
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 7)))
        await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        statements = [sql for sql, _args in database.connection.calls]
        assert any("DO UPDATE" in sql and "series_buckets" in sql for sql in statements)
        assert any("DELETE" in sql and "series_corrections" in sql for sql in statements), (
            "reopening must drop a correction that is no longer true"
        )

    async def test_a_quiet_reconcile_writes_nothing(self, declared, session, database):
        database.connection.script("series_buckets", [(utc(2026, 3, 1), {"n": 5}, None)])
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 5)))
        moved = await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
            now=utc(2026, 3, 4),
        )
        assert moved == ()
        assert not [
            sql
            for sql, _a in database.connection.calls
            if "series_corrections" in sql and "INSERT" in sql
        ]

    async def test_reconcile_needs_a_seal_to_compare_against(self, session):
        with pytest.raises(SeriesError, match="needs a seal"):
            await (
                view(stored_in=tz("UTC"))
                .measure(n=count())
                .reconcile(session, range=Range(utc(2026, 3, 1), utc(2026, 3, 2)))
            )

    async def test_reconcile_leaves_the_open_part_alone(self, declared, session, database):
        moved = await declared.reconcile(
            session,
            range=Range(utc(2026, 3, 4), utc(2026, 3, 5)),
            now=utc(2026, 3, 4, 12),
        )
        assert moved == ()
