"""The shared core with no time axis — a bar chart, a KPI, a scatter.

The interesting property is the one it does *not* share with a series: an
aggregate refuses past its ceiling rather than folding a remainder. Folding is
meaningful where it preserves the total, which is what a part-to-whole chart is
for; a bar chart's bars *are* the answer, so quietly dropping some of them
draws a chart that is wrong rather than absent.
"""

from __future__ import annotations

import pytest

from wreath.queries import Param
from wreath.series import Aggregate, SeriesError, avg, count, sum_

from .conftest import Herd, Trek

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def run(view, session, database, rows, **values):
    database.connection.responses.clear()
    database.connection.script("SELECT", rows)
    return await view.run(session, **values)


def sql_of(database):
    return database.connection.calls[-1][0]


class TestUngrouped:
    async def test_a_kpi_is_one_row_with_no_group_by(self, session, database):
        view = Aggregate(Trek).measure(treks=count(), distance=sum_(Trek.distance_km))
        result = await run(view, session, database, [(12, 340.5)])
        assert "GROUP BY" not in sql_of(database)
        assert len(result) == 1
        assert result.rows[0].key is None
        assert result.rows[0].values == {"treks": 12, "distance": 340.5}

    async def test_the_measure_names_are_carried(self, session, database):
        view = Aggregate(Trek).measure(treks=count())
        result = await run(view, session, database, [(3,)])
        assert result.measures == ("treks",)


class TestGrouped:
    def _view(self, **kwargs):
        return (
            Aggregate(Trek)
            .measure(treks=count(), distance=sum_(Trek.distance_km, unit="km"))
            .by(Trek.paddock_id, **kwargs)
        )

    async def test_one_row_per_group_keyed_by_the_grouping_value(
        self, session, database
    ):
        rows = [(10, 5, 20.0), (7, 3, 12.0)]
        result = await run(self._view(), session, database, rows)
        assert [item.key for item in result.rows] == [10, 7]
        assert result.rows[0].values == {"treks": 5, "distance": 20.0}
        assert result.rows[0].label == "10"

    async def test_it_orders_by_the_first_measure_then_the_key(
        self, session, database
    ):
        await run(self._view(), session, database, [])
        assert "ORDER BY 2 DESC NULLS LAST, 1 ASC" in sql_of(database)

    async def test_it_fetches_one_more_than_the_ceiling_to_notice_overflow(
        self, session, database
    ):
        await run(self._view(limit=3), session, database, [])
        _sql, args = database.connection.calls[-1]
        assert 4 in args, "ceiling + 1, so 'this is all of it' is distinguishable"

    async def test_it_refuses_past_the_ceiling_rather_than_truncating(
        self, session, database
    ):
        rows = [(index, 1, 1.0) for index in range(4)]
        with pytest.raises(SeriesError, match="more than 3 groups"):
            await run(self._view(limit=3), session, database, rows)

    async def test_the_refusal_says_how_to_raise_the_ceiling(self, session, database):
        rows = [(index, 1, 1.0) for index in range(4)]
        with pytest.raises(SeriesError, match="by\\(\\.\\.\\., limit=N\\)"):
            await run(self._view(limit=3), session, database, rows)

    async def test_exactly_the_ceiling_is_fine(self, session, database):
        rows = [(index, 1, 1.0) for index in range(3)]
        result = await run(self._view(limit=3), session, database, rows)
        assert len(result) == 3


class TestPredicatesAndJoins:
    async def test_a_param_binds_per_call(self, session, database):
        view = Aggregate(Trek).measure(n=count()).where(Trek.herd_id == Param("herd"))
        await run(view, session, database, [(4,)], herd=9)
        _sql, args = database.connection.calls[-1]
        assert 9 in args

    async def test_grouping_through_a_relation_joins_and_is_a_source(
        self, session, database
    ):
        view = Aggregate(Trek).measure(n=count()).by(Trek.herd.name)
        await run(view, session, database, [("alpha", 2)])
        assert "INNER JOIN" in sql_of(database)
        assert set(view.sources) == {Trek, Herd}

    async def test_a_measure_over_a_related_column_joins_too(self, session, database):
        view = Aggregate(Trek).measure(n=count()).where(Trek.herd.name == "alpha")
        await run(view, session, database, [(2,)])
        assert "INNER JOIN" in sql_of(database)


class TestScatter:
    async def test_two_measures_per_entity_is_the_scatter_shape(
        self, session, database
    ):
        """S4 falls out of the core with no spine: two measures, grouped by entity.

        Not a separate primitive -- the only thing that differs is the ceiling,
        which is a declared number rather than a type.
        """
        view = (
            Aggregate(Trek)
            .measure(distance=avg(Trek.distance_km), trips=count())
            .by(Trek.paddock_id, limit=3)
        )
        rows = [(1, 5.0, 2), (2, 7.5, 4)]
        result = await run(view, session, database, rows)
        assert [(item.key, item.values["distance"]) for item in result.rows] == [
            (1, 5.0),
            (2, 7.5),
        ]

    async def test_a_scatter_past_its_ceiling_refuses(self, session, database):
        view = Aggregate(Trek).measure(x=avg(Trek.distance_km)).by(
            Trek.paddock_id, limit=2
        )
        rows = [(index, 1.0) for index in range(3)]
        with pytest.raises(SeriesError, match="Narrow it"):
            await run(view, session, database, rows)


class TestNoMeasures:
    async def test_running_with_no_measures_refuses(self, session, database):
        with pytest.raises(SeriesError, match="nothing to compute"):
            await run(Aggregate(Trek), session, database, [])
