"""`events(...)`: the annotation layer, aligned by construction.

The requirement is alignment, not a saved round trip. These tests pin the four
things that make a marker trustworthy — same range, same zone, same bucket unit,
and a ceiling that refuses — and deliberately do *not* assert that both
statements travel in one round trip, because nothing here can observe that.
"""

from __future__ import annotations

import pytest

from wreath.series import Range, Series, SeriesError, count
from wreath.temporal import Day, Month

from .conftest import Deploy, Trek, utc

WEEK = Range(utc(2026, 1, 1), utc(2026, 1, 8))


def annotated(**kwargs):
    kwargs.setdefault("at", Deploy.happened_at)
    kwargs.setdefault("label", Deploy.version)
    return (
        Series(Trek, at=Trek.started_at, bucket=Day)
        .measure(n=count())
        .events(Deploy, **kwargs)
    )


async def run(view, session, database, *, spine=(), events=(), **kwargs):
    database.connection.responses.clear()
    database.connection.script('"spine"', list(spine))
    database.connection.script('"label"', list(events))
    kwargs.setdefault("range", WEEK)
    kwargs.setdefault("zone", "UTC")
    return await view.run(session, **kwargs)


def statements(database):
    return [call[0] for call in database.connection.calls]


class TestAlignment:
    async def test_the_markers_are_a_second_statement_not_a_tagged_union(
        self, session, database
    ):
        """A union would force two row shapes into one, half null in every row.

        Worse envelope, worse generated types, worse decode — bought with a
        round trip the driver may already be pipelining. The alignment does not
        depend on the trade, so it is made in favour of the type.
        """
        await run(annotated(), session, database)
        assert len(statements(database)) == 2
        assert "UNION ALL" not in statements(database)[1]

    async def test_the_marker_carries_its_bucket_and_its_exact_instant(
        self, session, database
    ):
        """Neither can be derived from the other on the client.

        The instant puts the marker at its true x-position; the bucket says
        which column it annotates.
        """
        result = await run(
            annotated(), session, database,
            events=[(utc(2026, 1, 3, 14), utc(2026, 1, 3), "v2.1")],
        )
        assert len(result.events) == 1
        marker = result.events[0]
        assert marker.at == utc(2026, 1, 3, 14), "the moment it happened"
        assert marker.bucket == utc(2026, 1, 3), "the column it belongs over"
        assert marker.label == "v2.1"

    async def test_the_bucket_is_cut_by_the_same_unit_as_the_series(
        self, session, database
    ):
        """A marker bucketed by day over a chart bucketed by month lands
        somewhere no column exists."""
        view = (
            Series(Trek, at=Trek.started_at, bucket=Month)
            .measure(n=count())
            .events(Deploy, at=Deploy.happened_at, label=Deploy.version)
        )
        await run(view, session, database, range=Range(utc(2026, 1, 1), utc(2026, 4, 1)))
        markers = statements(database)[1]
        assert "date_trunc('month'" in markers

    async def test_the_markers_are_read_in_the_readers_zone(self, session, database):
        """Bucketing markers in UTC while the chart is in Auckland puts every
        evening deploy on the wrong day."""
        await run(annotated(), session, database, zone="Pacific/Auckland")
        markers = statements(database)[1]
        assert "AT TIME ZONE" in markers
        assert "Pacific/Auckland" in database.connection.calls[1][1]

    async def test_the_markers_use_the_same_range_as_the_series(
        self, session, database
    ):
        """Neither side clipped differently: one `Range`, both statements."""
        await run(annotated(), session, database)
        markers = statements(database)[1]
        assert '"t0"."happened_at" >= $' in markers
        assert '"t0"."happened_at" < $' in markers
        series_args = database.connection.calls[0][1]
        marker_args = database.connection.calls[1][1]
        assert WEEK.start in series_args and WEEK.start in marker_args
        assert WEEK.end in series_args and WEEK.end in marker_args

    async def test_they_arrive_in_the_order_they_happened(self, session, database):
        await run(annotated(), session, database)
        assert "ORDER BY 1" in statements(database)[1]


class TestTheCeiling:
    async def test_too_many_markers_refuses_rather_than_drawing_a_subset(
        self, session, database
    ):
        """Half an annotation layer is worse than none: the chart looks
        annotated, and nothing in it says which markers are missing."""
        events = [(utc(2026, 1, 1), utc(2026, 1, 1), f"v{n}") for n in range(4)]
        with pytest.raises(SeriesError, match="more than 3 markers"):
            await run(annotated(limit=3), session, database, events=events)

    async def test_it_reads_one_past_the_ceiling_to_know_it_was_exceeded(
        self, session, database
    ):
        """A `LIMIT` of exactly the ceiling cannot tell a full answer from a
        truncated one."""
        await run(annotated(limit=3), session, database)
        assert 4 in database.connection.calls[1][1]

    async def test_exactly_the_ceiling_is_fine(self, session, database):
        events = [(utc(2026, 1, 1), utc(2026, 1, 1), f"v{n}") for n in range(3)]
        result = await run(annotated(limit=3), session, database, events=events)
        assert len(result.events) == 3

    def test_a_limit_past_the_hard_ceiling_refuses_at_declaration(self):
        with pytest.raises(SeriesError, match="above the ceiling"):
            annotated(limit=500)

    def test_a_limit_must_be_a_positive_integer(self):
        with pytest.raises(SeriesError, match="positive integer"):
            annotated(limit=0)


class TestDeclaration:
    def test_the_at_column_must_hold_a_time(self):
        with pytest.raises(SeriesError, match="cannot bucket"):
            annotated(at=Deploy.version)

    def test_the_label_must_be_a_column(self):
        with pytest.raises(SeriesError, match="takes a model column"):
            annotated(label="deployed")

    def test_declaring_it_twice_refuses(self):
        with pytest.raises(SeriesError, match="already declared"):
            annotated().events(Deploy, at=Deploy.happened_at, label=Deploy.version)

    def test_a_where_predicate_must_belong_to_the_events_model(self):
        """Filtering markers by a column of the *series* model is a mistake the
        declaration can catch, and an empty annotation layer at runtime is not
        a clear enough symptom of it."""
        with pytest.raises(Exception, match="Trek|belong"):
            annotated(where=Trek.grade == "steep")

    async def test_a_where_predicate_narrows_the_markers(self, session, database):
        view = annotated(where=Deploy.environment == "production")
        await run(view, session, database)
        markers = statements(database)[1]
        assert '"t0"."environment"' in markers
        assert "production" in database.connection.calls[1][1]

    def test_the_events_model_reaches_sources(self):
        """A new deploy changes the chart just as a new trek does.

        Leaving it out of `sources` shows up as a marker missing for five
        minutes and gets blamed on the browser.
        """
        assert Deploy in annotated().sources
        assert Trek in annotated().sources

    def test_a_view_without_events_has_none(self, session):
        view = Series(Trek, at=Trek.started_at, bucket=Day).measure(n=count())
        assert view.sources == (Trek,)


class TestWithOtherStages:
    async def test_events_and_compare_coexist(self, session, database):
        """Markers cover the primary period only — an annotation layer answers
        "what happened during *this*"."""
        view = (
            Series(Trek, at=Trek.started_at, bucket=Day)
            .measure(n=count())
            .compare(previous=Month)
            .events(Deploy, at=Deploy.happened_at, label=Deploy.version)
        )
        await run(
            view, session, database,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 8)),
            events=[(utc(2026, 3, 2), utc(2026, 3, 2), "v9")],
        )
        markers = statements(database)[1]
        assert "interval '1 month'" not in markers, "the primary range only"
        assert len(statements(database)) == 2

    async def test_a_view_that_declares_no_events_runs_one_statement(
        self, session, database
    ):
        view = Series(Trek, at=Trek.started_at, bucket=Day).measure(n=count())
        result = await run(view, session, database)
        assert len(statements(database)) == 1
        assert result.events == ()
