"""The spatial axis, and the fill rules it inherits rather than restates.

Slice 3 of the cross-product plan. The claim under test is not "wreath can
group by a grid cell" -- any two libraries give you that. It is that a heatmap
is *the same kind of declaration* a time chart already is, with the same
obligation: every cell in the extent present, and fill decided per measure, so
an empty cell reads as a zero count and an undefined average rather than as an
absence the caller has to notice or a zero that draws a hole in the map.

The strongest available proof that the two agree is that they share one
function. `test_fill_is_the_same_function_series_uses` asserts that
identity directly, so the rules cannot drift by being restated.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath._series import envelope
from wreath.geospatial import BoundingBox, Coordinate, grid
from wreath.series import Cells, Series, SeriesError, avg, count, max_, sum_
from wreath.temporal import Day

from .conftest import Sighting

EXTENT = BoundingBox(-30.0, -29.0, 150.0, 151.0)


async def run(view: Any, session: Any, database: Any, rows: list[Any], **values: Any) -> Any:
    database.connection.responses.clear()
    database.connection.script("SELECT", rows)
    return await view.run(session, **values)


def sql_of(database: Any) -> str:
    return database.connection.calls[-1][0]


class TestTheSpineIsDense:
    def test_every_cell_in_the_extent_is_present(self, registry, database):
        made = grid(EXTENT, metres=25_000)
        declaration = (
            Cells(Sighting)
            .measure(sightings=count())
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        assert declaration.grid == made
        assert declaration.grid.count == made.rows * made.columns

    def test_the_declaration_knows_its_cell_count_before_running(self):
        declaration = (
            Cells(Sighting)
            .measure(sightings=count())
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        # A declaration-time fact, which is what lets the ceiling be enforced
        # where it can be read rather than after the database has done the work.
        assert declaration.grid.count > 0


class TestFillIsPerMeasureAndNotRestated:
    def test_a_cell_and_a_bucket_fill_identically(self):
        """The claim of the slice, asserted as an equality rather than a copy.

        Two declarations over the same measures — one bucketed by time, one by
        cell — put through the *same* function, and required to agree. A
        parallel spatial fill table that happened to match today would pass a
        hand-written assertion and fail this one the day either moved.
        """
        measures = {"seen": count(), "mean_weight": avg(Sighting.weight_kg)}
        spatial = (
            Cells(Sighting)
            .measure(**measures)
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        temporal = Series(Sighting, at=Sighting.seen_at, bucket=Day).measure(**measures)
        for name in measures:
            assert envelope.fill(spatial, name, spatial._d.fills.get(name)) == (
                envelope.fill(temporal, name, temporal._d.fills.get(name))
            )

    def test_an_empty_cell_counts_zero_and_averages_null(self):
        declaration = (
            Cells(Sighting)
            .measure(seen=count(), mean_weight=avg(Sighting.weight_kg))
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        # The DoD row, asserted against the shared function rather than against
        # a hand-copied table.
        assert envelope.fill(declaration, "seen", None) == 0
        assert envelope.fill(declaration, "mean_weight", None) is None

    def test_sum_fills_zero_and_max_fills_null(self):
        declaration = (
            Cells(Sighting)
            .measure(total=sum_(Sighting.weight_kg), heaviest=max_(Sighting.weight_kg))
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        assert envelope.fill(declaration, "total", None) == 0
        assert envelope.fill(declaration, "heaviest", None) is None

    def test_an_explicit_fill_still_wins(self):
        declaration = (
            Cells(Sighting)
            .measure(mean_weight=avg(Sighting.weight_kg))
            .fill(mean_weight=0.0)
            .over(Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT)
        )
        assert envelope.fill(declaration, "mean_weight", declaration._d.fills["mean_weight"]) == 0.0


class TestDeclarationRefusals:
    def test_a_cell_count_past_the_ceiling_is_refused_at_declaration(self):
        # Refused where it can be read, not after the database has scanned.
        with pytest.raises(SeriesError, match="cells"):
            (
                Cells(Sighting)
                .measure(seen=count())
                .over(Sighting.lat, Sighting.lon, metres=10, extent=EXTENT)
            )

    def test_the_default_ceiling_is_what_it_claims(self):
        """The one mutant this file cannot kill, asserted anyway.

        `DEFAULT_CELL_LIMIT` is read once, when `over`'s signature is
        evaluated, so widening the constant after import cannot change the
        bound default and no test can observe the mutation. That makes it
        equivalent in practice rather than uncovered — but the number is a
        real decision, so it is pinned here.
        """
        from wreath.series import DEFAULT_CELL_LIMIT

        assert DEFAULT_CELL_LIMIT == 10_000
        default = Cells(Sighting).measure(seen=count()).over
        assert default.__defaults__ is None  # keyword-only, bound in __kwdefaults__
        assert default.__kwdefaults__["limit"] == DEFAULT_CELL_LIMIT

    def test_the_ceiling_is_declarable(self):
        made = (
            Cells(Sighting)
            .measure(seen=count())
            .over(
                Sighting.lat,
                Sighting.lon,
                metres=1_000,
                extent=EXTENT,
                limit=100_000,
            )
        )
        assert made.grid.count > 10_000

    async def test_running_without_measures_is_refused(self, session):
        declaration = Cells(Sighting).over(
            Sighting.lat, Sighting.lon, metres=25_000, extent=EXTENT
        )
        with pytest.raises(SeriesError, match="no measures"):
            await declaration.run(session)

    async def test_running_without_a_spatial_axis_is_refused(self, session):
        declaration = Cells(Sighting).measure(seen=count())
        with pytest.raises(SeriesError, match="no spatial axis"):
            await declaration.run(session)

    @pytest.mark.parametrize("bad", [0, -1, "many", None, 1.5, True])
    def test_an_invalid_ceiling_is_refused(self, bad):
        # `True` is an int in Python, so a boolean would otherwise read as a
        # ceiling of one cell.
        with pytest.raises(SeriesError, match="positive integer"):
            Cells(Sighting).measure(seen=count()).over(
                Sighting.lat,
                Sighting.lon,
                metres=25_000,
                extent=EXTENT,
                limit=bad,
            )

    def test_the_second_axis_is_checked_too(self):
        # Both columns, not just the first: a declaration that validated `lat`
        # and took `lon` on trust would fail much later and much worse.
        with pytest.raises(SeriesError, match="model column"):
            Cells(Sighting).measure(seen=count()).over(
                Sighting.lat, "lon", metres=25_000, extent=EXTENT
            )

    def test_a_non_column_axis_is_refused(self):
        with pytest.raises(SeriesError, match="model column"):
            Cells(Sighting).measure(seen=count()).over(
                "lat", Sighting.lon, metres=25_000, extent=EXTENT
            )

    def test_the_grid_refusals_reach_the_declaration(self):
        from wreath.geospatial import GeospatialError

        with pytest.raises(GeospatialError, match="antimeridian"):
            Cells(Sighting).measure(seen=count()).over(
                Sighting.lat,
                Sighting.lon,
                metres=25_000,
                extent=BoundingBox(-30.0, -29.0, 179.0, -179.0),
            )


class TestTheResultCarriesGeography:
    def _view(self):
        return (
            Cells(Sighting)
            .measure(seen=count(), mean_weight=avg(Sighting.weight_kg))
            .over(Sighting.lat, Sighting.lon, metres=50_000, extent=EXTENT)
        )

    async def test_a_cell_reports_its_bounds_and_centre(self, session, database):
        made = grid(EXTENT, metres=50_000)
        rows = [
            (row, column, 1, 2.0)
            for row in range(made.rows)
            for column in range(made.columns)
        ]
        result = await run(self._view(), session, database, rows)
        assert len(result.cells) == made.count
        first = result.cells[0]
        assert isinstance(first.bounds, BoundingBox)
        assert isinstance(first.centre, Coordinate)
        assert first.bounds.contains(first.centre)
        # The cell's own geography, not an index the caller has to resolve.
        assert first.bounds == made.cell(first.row, first.column)

    async def test_an_unmatched_cell_is_filled_rather_than_absent(
        self, session, database
    ):
        made = grid(EXTENT, metres=50_000)
        # The spine LEFT JOINs, so a cell nothing fell into arrives as nulls.
        rows = [
            (row, column, None, None)
            for row in range(made.rows)
            for column in range(made.columns)
        ]
        result = await run(self._view(), session, database, rows)
        assert len(result.cells) == made.count
        for cell in result.cells:
            assert cell.values["seen"] == 0
            assert cell.values["mean_weight"] is None

    async def test_a_populated_cell_keeps_its_measured_values(self, session, database):
        made = grid(EXTENT, metres=50_000)
        rows = [
            (row, column, None, None)
            for row in range(made.rows)
            for column in range(made.columns)
        ]
        rows[0] = (0, 0, 4, 11.5)
        result = await run(self._view(), session, database, rows)
        assert result.cells[0].values == {"seen": 4, "mean_weight": 11.5}
        assert result.cells[1].values == {"seen": 0, "mean_weight": None}

    async def test_the_statement_generates_the_whole_lattice(self, session, database):
        await run(self._view(), session, database, [])
        sql = sql_of(database)
        assert "generate_series" in sql
        assert "CROSS JOIN" in sql
        assert "LEFT JOIN" in sql


class TestTheExtentFilterComposesWithAWhere:
    """The `' AND ' if predicates else ' WHERE '` seam.

    Both arms need exercising: a declaration with a `where()` conjoins the
    extent onto the predicate, and one without opens its own `WHERE`. Only one
    was covered, so a mutation forcing either arm survived — and getting it
    wrong produces a statement PostgreSQL refuses outright, which is at least
    loud, but a test that never renders one arm is not a test of it.
    """

    def _view(self, *predicates):
        view = Cells(Sighting).measure(seen=count())
        if predicates:
            view = view.where(*predicates)
        return view.over(
            Sighting.lat, Sighting.lon, metres=50_000, extent=EXTENT
        )

    async def test_without_a_predicate_the_extent_opens_the_where(
        self, session, database
    ):
        await run(self._view(), session, database, [])
        sql = sql_of(database)
        assert " WHERE " in sql
        assert " AND " in sql  # the extent's own four bounds are conjoined

    async def test_with_a_predicate_the_extent_is_conjoined_onto_it(
        self, session, database
    ):
        await run(
            self._view(Sighting.species == "llama"), session, database, []
        )
        sql = sql_of(database)
        assert " WHERE " in sql
        # The predicate renders first, then the extent joins it with AND rather
        # than opening a second WHERE.
        assert sql.count(" WHERE ") == 1

    async def test_a_predicate_reaches_the_statement(self, session, database):
        await run(
            self._view(Sighting.species == "llama"), session, database, []
        )
        assert "species" in sql_of(database)
