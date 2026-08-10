"""Live-PostgreSQL checks for the spatial axis.

The fake-driver suite in ``tests/series/test_cells.py`` proves the statement's
shape and the fill rules. These prove the two things only a real server can:

* **The lattice PostgreSQL computes is the lattice Python computes.**
  ``compile_cells`` renders ``FLOOR((lat - origin) / step)`` clamped into range,
  and ``Grid.index_of`` does the same arithmetic in Python. Two spellings of one
  rule is how they drift apart, and a drift here puts observations in the wrong
  cell -- which nothing downstream can detect, because a heatmap with everything
  shifted one cell east still looks like a heatmap.
* **The spine really is dense.** A ``generate_series`` cross join returning one
  row per cell is a claim about PostgreSQL's behaviour, not about the builder's
  string.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database.
"""

from __future__ import annotations

import datetime
import os

import pytest

from wreath.geospatial import BoundingBox, Coordinate, grid
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Float64, Int64, Text, TimestampTz
from wreath.postgres import Database
from wreath.series import Cells, avg, count

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live spatial-axis tests",
)

#: A one-degree box in the southern hemisphere at a longitude where a sign
#: error would be obvious rather than cancelling out.
EXTENT = BoundingBox(-30.0, -29.0, 150.0, 151.0)
CELL_METRES = 25_000


#: One schema per xdist worker, read once and assigned rather than defaulted.
#: `os.environ.setdefault` in a conftest silently no-ops for this: the
#: controller imports it during collection and then spawns workers carrying its
#: own environment, so every worker would inherit one name and race on
#: `CREATE SCHEMA`.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")
SCHEMA = f"wreath_cells_{WORKER}"


class Observation(Model, table="cells_observations", schema=SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    species: Mapped[str] = column(Text)
    lat: Mapped[float] = column(Float64)
    lon: Mapped[float] = column(Float64)
    weight_kg: Mapped[float] = column(Float64, nullable=True)
    seen_at: Mapped[object] = column(TimestampTz)


def schema_name() -> str:
    return SCHEMA


async def _execute(database, sql: str, *args) -> None:
    connection = await database.acquire("write")
    try:
        await connection.execute(sql, *args)
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database(
        "main",
        dsn,
        pools={
            "read": {"min_size": 1, "max_size": 2},
            "write": {"min_size": 1, "max_size": 2},
        },
    )
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def session(database):
    schema = schema_name()
    await _execute(database, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _execute(database, f'CREATE SCHEMA "{schema}"')
    await _execute(
        database,
        f'CREATE TABLE "{schema}".cells_observations ('
        "id bigint PRIMARY KEY, species text NOT NULL, "
        "lat double precision NOT NULL, lon double precision NOT NULL, "
        "weight_kg double precision, seen_at timestamptz NOT NULL)",
    )
    registry = Registry(database, [Observation], validate_schema="off")
    session = Session(registry, "write")
    try:
        yield session
    finally:
        # Before the DDL, and before `database` stops: an unreturned lease costs
        # the pool its full 10s `shutdown_timeout`, and a `DROP SCHEMA` issued
        # while this session still holds an open transaction blocks on its locks.
        await session.close()
        await _execute(database, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def insert(database, rows):
    schema = schema_name()
    for index, (lat, lon, weight) in enumerate(rows):
        await _execute(
            database,
            f'INSERT INTO "{schema}".cells_observations '
            "(id, species, lat, lon, weight_kg, seen_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            index,
            "llama",
            lat,
            lon,
            weight,
            datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
        )


def view(**kwargs):
    return (
        Cells(Observation)
        .measure(seen=count(), mean_weight=avg(Observation.weight_kg))
        .over(
            Observation.lat,
            Observation.lon,
            metres=CELL_METRES,
            extent=EXTENT,
            **kwargs,
        )
    )


class TestTheSpineIsDenseOnARealServer:
    async def test_every_cell_arrives_even_with_no_rows_at_all(self, session, database):
        lattice = grid(EXTENT, metres=CELL_METRES)
        result = await view().run(session)
        assert len(result.cells) == lattice.count
        assert {(cell.row, cell.column) for cell in result.cells} == {
            (row, column)
            for row in range(lattice.rows)
            for column in range(lattice.columns)
        }

    async def test_an_empty_cell_counts_zero_and_averages_null(self, session, database):
        # The DoD row, against a real LEFT JOIN rather than a scripted null.
        await insert(database, [(-29.5, 150.5, 10.0)])
        result = await view().run(session)
        populated = [cell for cell in result.cells if cell.values["seen"]]
        empty = [cell for cell in result.cells if not cell.values["seen"]]
        assert len(populated) == 1
        assert empty, "the extent should have more cells than the one row fills"
        for cell in empty:
            assert cell.values["seen"] == 0
            assert cell.values["mean_weight"] is None

    async def test_cells_arrive_in_row_major_order(self, session, database):
        result = await view().run(session)
        keys = [(cell.row, cell.column) for cell in result.cells]
        assert keys == sorted(keys)


class TestSqlAndPythonAgreeOnWhichCell:
    async def test_sql_cell_assignment_matches_index_of(self, session, database):
        """The load-bearing test of the whole slice.

        Every observation is placed by PostgreSQL, and the cell it landed in is
        compared against the one `Grid.index_of` computes in Python. A
        drift between them is invisible downstream: a map with everything one
        cell east still renders.
        """
        lattice = grid(EXTENT, metres=CELL_METRES)
        points = [
            (-29.5, 150.5),   # middle
            (-30.0, 150.0),   # the extent's south-west corner exactly
            (-29.0, 151.0),   # the north-east corner exactly, the clamping case
            (-29.999, 150.001),
            (-29.001, 150.999),
            (-29.25, 150.75),
            (-29.75, 150.25),
        ]
        await insert(database, [(lat, lon, 1.0) for lat, lon in points])
        result = await view().run(session)

        expected: dict[tuple[int, int], int] = {}
        for lat, lon in points:
            index = lattice.index_of(Coordinate(lat=lat, lon=lon))
            assert index is not None, f"({lat}, {lon}) should be inside the extent"
            expected[index] = expected.get(index, 0) + 1

        actual = {
            (cell.row, cell.column): cell.values["seen"]
            for cell in result.cells
            if cell.values["seen"]
        }
        assert actual == expected

    async def test_a_point_on_the_far_edge_lands_in_the_edge_cell(self, session, database):
        """The clamp, which is where a half-open rule would drop the row.

        `BoundingBox.contains` is inclusive at both edges, so a point exactly on
        the northern boundary is inside the region the reader asked for. The
        statement's `LEAST(...)` and `index_of`'s `min(...)` both put it in the
        last cell rather than one past the end.
        """
        lattice = grid(EXTENT, metres=CELL_METRES)
        await insert(database, [(EXTENT.lat_max, EXTENT.lon_max, 1.0)])
        result = await view().run(session)
        landed = [cell for cell in result.cells if cell.values["seen"]]
        assert len(landed) == 1
        assert (landed[0].row, landed[0].column) == (
            lattice.rows - 1,
            lattice.columns - 1,
        )

    async def test_a_point_outside_the_extent_is_excluded_entirely(self, session, database):
        await insert(database, [(-29.5, 150.5, 1.0), (0.0, 0.0, 99.0)])
        result = await view().run(session)
        assert sum(cell.values["seen"] for cell in result.cells) == 1


class TestMeasuresAreRealAggregates:
    async def test_the_average_is_the_average_of_the_cell(self, session, database):
        await insert(
            database,
            [(-29.5, 150.5, 10.0), (-29.5, 150.5, 20.0), (-29.5, 150.5, 30.0)],
        )
        result = await view().run(session)
        populated = [cell for cell in result.cells if cell.values["seen"]]
        assert len(populated) == 1
        assert populated[0].values["seen"] == 3
        assert populated[0].values["mean_weight"] == pytest.approx(20.0)

    async def test_a_cell_whose_rows_are_all_null_averages_null_not_zero(self, session, database):
        # AVG over three NULLs is NULL, and it must not be confused with the
        # fill for a cell nothing landed in -- both read null, and both are
        # correct, which is the point.
        await insert(
            database,
            [(-29.5, 150.5, None), (-29.5, 150.5, None)],
        )
        result = await view().run(session)
        populated = [cell for cell in result.cells if cell.values["seen"]]
        assert len(populated) == 1
        assert populated[0].values["seen"] == 2
        assert populated[0].values["mean_weight"] is None
