from __future__ import annotations

import pytest

from wreath._series.tiers import Tier, build, plan, serves_zone, width
from wreath.series import Range, Series, SeriesError, avg, count, sum_
from wreath.temporal import Day, Hour, Minute, Month, Year
from wreath.temporal import zone as tz

from .conftest import Trek, utc


def view(**kwargs):
    kwargs.setdefault("stored_in", tz("UTC"))
    return Series(Trek, at=Trek.started_at, bucket=Day, **kwargs)


def tiered(*, measures=None, seal="2h", **windows):
    declared = view().measure(**(measures or {"n": count()}))
    if seal is not None:
        declared = declared.seal(after=seal)
    return declared.retain(**windows)


DAY = 86400.0


class TestLadder:
    def test_raw_is_required_because_it_is_the_bottom_rung(self):
        with pytest.raises(SeriesError, match="must name 'raw'"):
            build({"day": "1 year"}, refuse=SeriesError)

    def test_an_unknown_tier_name_is_refused_with_the_real_ones(self):
        with pytest.raises(SeriesError, match="unknown tier 'fortnight'"):
            build({"raw": "3 days", "fortnight": "1 year"}, refuse=SeriesError)

    def test_tiers_are_ordered_finest_first_whatever_order_they_were_written(self):
        ladder = build({"month": None, "raw": "3 days", "day": "1 year"}, refuse=SeriesError)
        assert [tier.name for tier in ladder] == ["raw", "day", "month"]

    def test_retention_must_grow_as_the_grain_coarsens(self):
        with pytest.raises(SeriesError, match="less time than a finer tier"):
            build({"raw": "1 year", "day": "3 days"}, refuse=SeriesError)

    def test_a_bounded_tier_above_an_unbounded_one_is_refused(self):
        with pytest.raises(SeriesError, match="finer tier forever"):
            build({"raw": None, "day": "1 year"}, refuse=SeriesError)

    @pytest.mark.parametrize(
        "written,seconds",
        [
            ("3 days", 3 * DAY),
            ("14 days", 14 * DAY),
            ("2h", 7200.0),
            ("1 week", 7 * DAY),
            (900, 900.0),
        ],
    )
    def test_a_window_is_written_the_way_people_say_it(self, written, seconds):
        ladder = build({"raw": written}, refuse=SeriesError)
        assert ladder.raw.keep == pytest.approx(seconds)

    def test_a_year_is_accepted_here_though_seal_would_refuse_it(self):
        ladder = build({"raw": "3 days", "day": "1 year"}, refuse=SeriesError)
        assert ladder.named("day").keep == pytest.approx(31557600.0)
        with pytest.raises(SeriesError):
            view().measure(n=count()).seal(after="1 year")

    def test_none_means_forever_and_zero_is_not_a_way_to_spell_delete(self):
        assert build({"raw": None}, refuse=SeriesError).raw.keep is None
        with pytest.raises(SeriesError, match="nothing here deletes anything"):
            build({"raw": 0}, refuse=SeriesError)

    def test_gibberish_is_refused_rather_than_read_as_zero(self):
        with pytest.raises(SeriesError, match="is not a duration"):
            build({"raw": "soonish"}, refuse=SeriesError)


class TestDeclaration:
    def test_a_coarser_tier_needs_a_seal(self):
        with pytest.raises(SeriesError, match="needs seal\\(\\)"):
            view().measure(n=count()).retain(raw="3 days", day="1 year")

    def test_raw_alone_needs_no_seal(self):
        declared = view().measure(n=count()).retain(raw="3 days")
        assert [tier.name for tier in declared.tiers] == ["raw"]

    def test_one_ladder_per_view(self):
        with pytest.raises(SeriesError, match="already declared"):
            tiered(raw="3 days", day="1 year").retain(raw="1 day")

    def test_retain_cannot_be_combined_with_by(self):
        declared = view().measure(n=count()).seal(after="2h")
        with pytest.raises(SeriesError, match="cannot be combined with by"):
            declared.by(Trek.paddock_id).retain(raw="3 days")

    def test_by_after_retain_is_refused_from_the_other_side(self):
        declared = tiered(raw="3 days", day="1 year")
        with pytest.raises(SeriesError, match="cannot be combined with retain"):
            declared.by(Trek.paddock_id)

    def test_nothing_on_this_surface_deletes_anything(self):
        declared = tiered(raw="3 days", day="1 year")
        assert "DELETE" not in str(declared.tiers)
        for method in ("archive", "drop"):
            with pytest.raises(SeriesError, match="not implemented|opt-in"):
                getattr(declared, method)(raw=True)


class TestAdditivity:
    """§7.5's correctness trap, and the design calls it the big one."""

    def test_an_average_with_a_coarser_tier_is_refused(self):
        with pytest.raises(SeriesError, match="cannot be rolled up"):
            tiered(
                measures={"mean": avg(Trek.distance_km)},
                raw="3 days",
                month=None,
            )

    def test_the_refusal_names_the_measure_the_tier_and_both_ways_out(self):
        with pytest.raises(SeriesError) as caught:
            tiered(measures={"mean": avg(Trek.distance_km)}, raw="3 days", month=None)
        message = str(caught.value)
        assert "'mean'" in message and "'month'" in message
        assert "retain(raw=None" in message, "must name the pin-raw escape"
        assert "sum and a count" in message, "must name the fix that is not built"

    def test_an_average_is_fine_when_raw_is_kept_forever(self):
        declared = tiered(measures={"mean": avg(Trek.distance_km)}, raw=None, month=None)
        assert [tier.name for tier in declared.tiers] == ["raw", "month"]

    def test_an_average_is_fine_with_no_tier_coarser_than_the_view(self):
        declared = tiered(measures={"mean": avg(Trek.distance_km)}, raw="3 days", day="1 year")
        assert [tier.name for tier in declared.tiers] == ["raw", "day"]

    def test_counts_and_sums_and_extremes_all_roll_up(self):
        declared = tiered(
            measures={"n": count(), "total": sum_(Trek.distance_km)},
            raw="3 days",
            month=None,
        )
        assert len(declared.tiers) == 2

    def test_a_measure_added_after_the_ladder_faces_the_same_check(self):
        declared = tiered(raw="3 days", month=None)
        with pytest.raises(SeriesError, match="cannot be rolled up"):
            declared.measure(mean=avg(Trek.distance_km))

    def test_the_check_reads_the_same_property_that_folds_a_correction(self):
        assert avg(Trek.distance_km).rollup_safe is False
        assert avg(Trek.distance_km).has_identity is False
        assert count().rollup_safe is True


class TestZoneCompatibility:
    """§7.4: a materialised tier is zone-specific, and that is the subtle part."""

    at = utc(2026, 3, 1)

    def test_raw_serves_every_zone_because_it_is_not_cut_into_anything(self):
        assert serves_zone(None, "Pacific/Auckland", "Europe/London", at=self.at)

    def test_the_zone_it_was_computed_in_always_works(self):
        assert serves_zone(Day, "Pacific/Auckland", "Pacific/Auckland", at=self.at)

    def test_daily_rows_serve_only_the_zone_they_were_cut_in(self):
        assert not serves_zone(Day, "Pacific/Auckland", "Europe/London", at=self.at)
        assert not serves_zone(Month, "UTC", "Europe/London", at=self.at)

    def test_hourly_rows_serve_any_whole_hour_zone(self):
        assert serves_zone(Hour, "UTC", "Europe/London", at=self.at)
        assert serves_zone(Hour, "UTC", "Pacific/Auckland", at=self.at)

    @pytest.mark.parametrize("zone", ["Asia/Kolkata", "Asia/Kathmandu", "Pacific/Chatham"])
    def test_hourly_rows_do_not_serve_a_fractional_offset(self, zone):
        assert not serves_zone(Hour, "UTC", zone, at=self.at)

    def test_minute_rows_serve_even_the_fractional_offsets(self):
        for zone in ("Asia/Kolkata", "Asia/Kathmandu", "Pacific/Chatham"):
            assert serves_zone(Minute, "UTC", zone, at=self.at)

    def test_a_read_in_an_unservable_zone_refuses_rather_than_lying(self):
        declared = tiered(raw="3 days", day=None)
        with pytest.raises(SeriesError, match="no tier can answer for zone"):
            plan(
                ladder=declared._tiers,
                requested=Day,
                start=utc(2020, 1, 1),
                end=utc(2020, 6, 1),
                now=utc(2026, 3, 1),
                stored_zone="UTC",
                read_zone="Europe/London",
                allow_coarsening=False,
                refuse=SeriesError,
            )

    def test_the_advice_is_to_materialise_finer_when_readers_span_zones(self):
        assert serves_zone(Hour, "UTC", "Europe/London", at=self.at)
        assert not serves_zone(Day, "UTC", "Europe/London", at=self.at)


def _plan(ladder, *, start, end, now, requested=Day, coarsen=False, read="UTC"):
    return plan(
        ladder=ladder,
        requested=requested,
        start=start,
        end=end,
        now=now,
        stored_zone="UTC",
        read_zone=read,
        allow_coarsening=coarsen,
        refuse=SeriesError,
    )


class TestPlanning:
    def test_a_range_inside_raws_window_is_one_raw_segment(self):
        ladder = tiered(raw="3 days", day="1 year")._tiers
        segments = _plan(ladder, start=utc(2026, 3, 3), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert [item.grain for item in segments] == ["raw"]

    def test_raw_wins_wherever_it_still_covers(self):
        ladder = tiered(raw="3 days", day="1 year")._tiers
        segments = _plan(ladder, start=utc(2026, 1, 1), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert segments[-1].grain == "raw"
        assert segments[0].grain == "day"

    def test_the_boundary_is_the_retention_edge(self):
        ladder = tiered(raw="3 days", day="1 year")._tiers
        segments = _plan(ladder, start=utc(2026, 1, 1), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert len(segments) == 2
        assert segments[0].end == segments[1].start == utc(2026, 3, 1)

    def test_the_design_s_own_four_hundred_day_example(self):
        ladder = tiered(raw="3 days", day="1 year", month=None)._tiers
        segments = _plan(
            ladder,
            start=utc(2025, 1, 20),
            end=utc(2026, 3, 4),
            now=utc(2026, 3, 4),
            coarsen=True,
        )
        assert [item.grain for item in segments] == ["month", "day", "raw"]

    def test_segments_are_contiguous_and_half_open(self):
        ladder = tiered(raw="3 days", day="1 year", month=None)._tiers
        segments = _plan(
            ladder,
            start=utc(2025, 1, 20),
            end=utc(2026, 3, 4),
            now=utc(2026, 3, 4),
            coarsen=True,
        )
        assert segments[0].start == utc(2025, 1, 20)
        assert segments[-1].end == utc(2026, 3, 4)
        for left, right in zip(segments, segments[1:], strict=False):
            assert left.end == right.start

    def test_exactly_one_tier_answers_each_piece(self):
        ladder = tiered(raw="3 days", day="1 year", month=None)._tiers
        segments = _plan(
            ladder,
            start=utc(2025, 1, 20),
            end=utc(2026, 3, 4),
            now=utc(2026, 3, 4),
            coarsen=True,
        )
        assert len({item.start for item in segments}) == len(segments)

    def test_coarsening_refuses_by_default_and_names_the_grain_available(self):
        ladder = tiered(raw="3 days", day="1 year", month=None)._tiers
        with pytest.raises(SeriesError, match="'month'"):
            _plan(
                ladder,
                start=utc(2025, 1, 20),
                end=utc(2026, 3, 4),
                now=utc(2026, 3, 4),
            )

    def test_the_refusal_says_how_to_accept_it(self):
        ladder = tiered(raw="3 days", day="1 year", month=None)._tiers
        with pytest.raises(SeriesError) as caught:
            _plan(ladder, start=utc(2025, 1, 20), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert "allow_coarsening=True" in str(caught.value)

    def test_a_range_past_every_window_refuses(self):
        ladder = tiered(raw="3 days", day="1 year")._tiers
        with pytest.raises(SeriesError, match="older than every declared tier"):
            _plan(ladder, start=utc(2020, 1, 1), end=utc(2020, 2, 1), now=utc(2026, 3, 4))

    def test_adjacent_pieces_on_one_tier_are_merged(self):
        ladder = tiered(raw=None, day=None)._tiers
        segments = _plan(ladder, start=utc(2020, 1, 1), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert len(segments) == 1


class TestGrainOrdering:
    def test_grains_order_from_fine_to_coarse(self):
        widths = [width(item) for item in (Minute, Hour, Day, Month, Year)]
        assert widths == sorted(widths)


def _rows(*pairs):
    return [(bucket, value) for bucket, value in pairs]


class TestTieredRead:
    """The caller never learns there were tiers -- but the envelope says so."""

    async def test_one_spine_comes_back_however_many_tiers_answered(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 3), 4)))
        database.connection.script("series_buckets", [(utc(2026, 1, 5), {"n": 9}, None)])
        declared = tiered(raw="3 days", day="1 year")
        result = await declared.run(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 3, 4)),
            now=utc(2026, 3, 4),
        )
        assert len(result.series) == 1, "one measure is one series, tiers or not"
        assert result.bucket == "day"

    async def test_the_envelope_reports_the_grain_used_per_segment(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 3), 4)))
        database.connection.script("series_buckets", [])
        declared = tiered(raw="3 days", day="1 year")
        result = await declared.run(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 3, 4)),
            now=utc(2026, 3, 4),
        )
        assert [item.grain for item in result.segments] == ["day", "raw"]
        assert result.as_dict()["segments"][0]["grain"] == "day"

    async def test_an_untiered_view_reports_no_segments(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 1), 1)))
        declared = view().measure(n=count())
        result = await declared.run(session, range=Range(utc(2026, 3, 1), utc(2026, 3, 2)))
        assert result.segments == ()

    async def test_a_tier_read_never_touches_the_source_table(self, session, database):
        database.connection.script("series_buckets", [(utc(2026, 1, 5), {"n": 9}, None)])
        declared = tiered(raw="3 days", day=None)
        await declared.run(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 1, 20)),
            now=utc(2026, 3, 4),
        )
        assert not any("generate_series" in sql for sql in database.connection.statements)

    async def test_coarsening_is_refused_on_the_read_path_too(self, session, database):
        declared = tiered(raw="3 days", day="1 year", month=None)
        with pytest.raises(SeriesError, match="allow_coarsening=True"):
            await declared.run(
                session,
                range=Range(utc(2025, 1, 20), utc(2026, 3, 4)),
                now=utc(2026, 3, 4),
            )

    async def test_allow_coarsening_accepts_it_and_says_where(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 3), 4)))
        database.connection.script("series_buckets", [])
        declared = tiered(raw="3 days", day="1 year", month=None)
        result = await declared.run(
            session,
            range=Range(utc(2025, 1, 20), utc(2026, 3, 4)),
            now=utc(2026, 3, 4),
            allow_coarsening=True,
        )
        assert [item.grain for item in result.segments] == ["month", "day", "raw"]

    async def test_a_read_in_a_foreign_zone_refuses_rather_than_returning_wrong_days(
        self, session, database
    ):
        declared = tiered(raw="3 days", day=None)
        with pytest.raises(SeriesError, match="no tier can answer for zone"):
            await declared.run(
                session,
                range=Range(utc(2026, 1, 1), utc(2026, 1, 20)),
                zone="Europe/London",
                now=utc(2026, 3, 4),
            )

    async def test_a_foreign_zone_inside_raws_window_is_fine(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 3), 4)))
        declared = tiered(raw="3 days", day=None)
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 3), utc(2026, 3, 4)),
            zone="Europe/London",
            now=utc(2026, 3, 4),
        )
        assert result.zone == "Europe/London"

    async def test_a_runtime_zone_owns_a_tiered_view_with_no_stored_zone(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 3, 3), 4)))
        declared = (
            Series(Trek, at=Trek.started_at, bucket=Day)
            .measure(n=count())
            .seal(after="2h")
            .retain(raw="3 days", day=None)
        )
        result = await declared.run(
            session,
            range=Range(utc(2026, 3, 3), utc(2026, 3, 4)),
            zone="Europe/London",
            now=utc(2026, 3, 4),
        )
        assert result.zone == "Europe/London"


class TestRollup:
    async def test_it_materialises_the_coarser_grain(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 1, 1), 30)))
        database.connection.script("series_buckets", [])
        declared = tiered(raw="3 days", month=None)
        written = await declared.rollup(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 2, 1)),
            now=utc(2026, 3, 4),
        )
        assert written["month"] == (utc(2026, 1, 1),)
        assert any(
            "INSERT INTO" in sql and "series_buckets" in sql
            for sql in database.connection.statements
        )

    async def test_it_reconciles_before_it_rolls_up(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 1, 1), 30)))
        database.connection.script("series_buckets", [(utc(2026, 1, 15), {"n": 1}, None)])
        declared = tiered(raw="3 days", month=None)
        await declared.rollup(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 2, 1)),
            now=utc(2026, 3, 4),
        )
        statements = database.connection.statements
        # Reconcile reads the settled rows *before* computing anything;
        # materialising computes first and reads second. So a stored read
        # arriving before the first source read is the ordering, observably.
        assert "series_buckets" in statements[0], "reconcile has to come first"
        assert any("generate_series" in sql for sql in statements[1:])
        assert any("INSERT INTO" in sql and "series_buckets" in sql for sql in statements), (
            "expected the rollup write"
        )

    async def test_it_does_not_overwrite_a_bucket_it_already_wrote(self, session, database):
        database.connection.script("generate_series", _rows((utc(2026, 1, 1), 30)))
        database.connection.script("series_buckets", [(utc(2026, 1, 1), {"n": 30}, None)])
        declared = tiered(raw="3 days", month=None)
        written = await declared.rollup(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 2, 1)),
            now=utc(2026, 3, 4),
        )
        assert written["month"] == (), "already materialised, nothing to add"

    async def test_it_stops_at_the_watermark_for_the_coarse_grain(self, session, database):
        database.connection.script("generate_series", [])
        database.connection.script("series_buckets", [])
        declared = tiered(raw="3 days", month=None)
        written = await declared.rollup(
            session,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 4)),
            now=utc(2026, 3, 4),
        )
        assert written["month"] == (), "March is still open in March"

    async def test_rollup_without_a_ladder_refuses(self, session):
        declared = view().measure(n=count()).seal(after="2h")
        with pytest.raises(SeriesError, match="needs retain"):
            await declared.rollup(session, range=Range(utc(2026, 1, 1), utc(2026, 2, 1)))

    async def test_a_coarse_tier_is_built_from_source_rows_not_from_the_fine_tier(
        self, session, database
    ):
        database.connection.script("generate_series", _rows((utc(2026, 1, 1), 30)))
        database.connection.script("series_buckets", [])
        declared = tiered(raw="3 days", month=None)
        await declared.rollup(
            session,
            range=Range(utc(2026, 1, 1), utc(2026, 2, 1)),
            now=utc(2026, 3, 4),
        )
        assert any("generate_series" in sql for sql in database.connection.statements), (
            "the coarse grain is computed from the source table"
        )


class TestTierIdentity:
    def test_a_tier_is_the_same_declaration_at_another_grain(self):
        declared = tiered(raw="3 days", month=None)
        day_key, _ = declared._identity("UTC", {})
        month_key, _ = declared._identity("UTC", {}, grain=Month)
        assert day_key != month_key

    def test_the_same_grain_key_is_the_one_sealing_already_writes(self):
        declared = tiered(raw="3 days", day="1 year")
        assert declared._identity("UTC", {}) == declared._identity("UTC", {}, grain=Day)

    def test_a_different_zone_files_separately(self):
        declared = tiered(raw="3 days", month=None)
        assert declared._identity("UTC", {}) != declared._identity("Pacific/Auckland", {})


class TestTierRepr:
    def test_a_tier_says_its_window(self):
        assert "forever" in repr(Tier(grain=None, keep=None))
        assert "raw" in repr(Tier(grain=None, keep=None))

    def test_a_segment_says_which_tier_answered(self):
        ladder = tiered(raw="3 days", day="1 year")._tiers
        segments = _plan(ladder, start=utc(2026, 1, 1), end=utc(2026, 3, 4), now=utc(2026, 3, 4))
        assert "day" in repr(segments[0])


class TestRetentionIsNotDeletion:
    def test_the_module_contains_no_delete_for_a_retention_window(self):
        import wreath._series.tiers as module

        text = open(module.__file__).read().upper()
        for statement in ("DELETE FROM", "TRUNCATE", "DROP TABLE"):
            assert statement not in text, f"{statement} would make retain() an expiry"

    async def test_an_expired_tier_is_still_read_if_a_coarser_one_covers_it(
        self, session, database
    ):
        database.connection.script("series_buckets", [(utc(2025, 6, 1), {"n": 7}, None)])
        declared = tiered(raw="3 days", day="1 year", month=None)
        result = await declared.run(
            session,
            range=Range(utc(2024, 1, 1), utc(2024, 2, 1)),
            now=utc(2026, 3, 4),
            allow_coarsening=True,
        )
        assert [item.grain for item in result.segments] == ["month"]
