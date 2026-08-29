from __future__ import annotations

import os
from typing import Any

import pytest

from wreath.geospatial import Coordinate
from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.compiler import compile_select
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Point, Text, TsVector

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: One schema per xdist worker. Sharing one races on `CREATE SCHEMA`, which
#: PostgreSQL reports as a unique violation on a catalog index and which reads
#: like anything except a test-isolation bug.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_geo_{_WORKER}"

SYDNEY = Coordinate(lat=-33.8688, lon=151.2093)
#: Straddles the antimeridian, so a circle around it needs two boxes.
TAVEUNI = Coordinate(lat=-16.85, lon=179.98)


class Database:
    name = "main"


class Station(Model, table="stations", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Point, index="gist")


class Note(Model, table="notes", schema=_SCHEMA):
    """Exists only to give the limit rule a *non-geo* BinaryExpr ordering.

    Without one, dropping the `operator == 'geo_distance'` clause from the
    guard would refuse every unbounded ordering by a rank or a vector distance
    and nothing would object -- a surviving mutant said exactly that.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    search: Mapped[bytes] = column(TsVector("english", sources=("body",)), index="gin")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry(Database(), [Station, Note], validate_schema="off")


def _sql(registry: Registry, select: Any) -> str:
    return compile_select(registry, select).sql


class TestWithinRenders:
    def test_it_renders_a_box_containment_the_index_can_answer(self, registry: Registry) -> None:
        sql = _sql(registry, Station.select().where(Station.at.within(SYDNEY, 5_000)))
        # `<@ box(point(...), point(...))` is the whole point: it is the only
        # form a GiST `point_ops` index answers.
        assert "<@ box(point(" in sql

    def test_the_exact_filter_is_anded_on_not_substituted(self, registry: Registry) -> None:
        sql = _sql(registry, Station.select().where(Station.at.within(SYDNEY, 5_000)))
        # A box is a superset of a circle. Returning the box's rows would be
        # wrong at the corners, so the great-circle test must survive too.
        assert "asin" in sql.lower()
        assert " AND " in sql

    def test_no_coordinate_reaches_the_sql_text(self, registry: Registry) -> None:
        sql = _sql(registry, Station.select().where(Station.at.within(SYDNEY, 5_000)))
        # Every number is a bind. A literal here would defeat the plan cache and
        # put user input in the statement.
        assert "151.2" not in sql
        assert "-33.8" not in sql

    def test_a_circle_crossing_the_antimeridian_renders_both_boxes(
        self, registry: Registry
    ) -> None:
        near = _sql(registry, Station.select().where(Station.at.within(SYDNEY, 5_000)))
        across = _sql(registry, Station.select().where(Station.at.within(TAVEUNI, 50_000)))
        # Two boxes, ORed -- a wrapped edge is not something `<@` understands,
        # and silently searching only one side is the classic date-line bug.
        assert across.count("<@ box(point(") == 2
        assert near.count("<@ box(point(") == 1

    def test_a_non_coordinate_centre_is_refused(self, registry: Registry) -> None:
        with pytest.raises(TypeError):
            Station.at.within((151.2, -33.8), 5_000)

    def test_a_negative_radius_is_refused(self, registry: Registry) -> None:
        with pytest.raises(ValueError):
            Station.at.within(SYDNEY, -1)

    def test_within_is_refused_on_a_column_that_is_not_a_point(self, registry: Registry) -> None:
        with pytest.raises(DeclarationError):
            Station.name.within(SYDNEY, 5_000)


class TestNearestIsOrderedAndBounded:
    def test_nearest_is_an_order_key_not_a_predicate(self, registry: Registry) -> None:
        sql = _sql(registry, Station.select().order_by(Station.at.nearest(SYDNEY)).limit(10))
        assert "ORDER BY" in sql
        assert "asin" in sql.lower()

    def test_an_unbounded_nearest_is_refused(self, registry: Registry) -> None:
        # The same discipline vector search already applies: an ordered search
        # with no ceiling reads the whole table, and the index cannot help.
        with pytest.raises(DeclarationError, match="limit"):
            compile_select(registry, Station.select().order_by(Station.at.nearest(SYDNEY)))

    def test_an_ordinary_unbounded_ordering_is_still_allowed(self, registry: Registry) -> None:
        # The limit rule is for proximity searches only. Without this, dropping
        # either half of the guard's condition would refuse every unbounded
        # `ORDER BY` in the ORM and no test would object -- which is exactly
        # what three surviving mutants reported.
        sql = _sql(registry, Station.select().order_by(Station.name.asc()))
        assert "ORDER BY" in sql

    def test_an_unbounded_ordering_by_a_rank_is_unaffected(self, registry: Registry) -> None:
        # The guard must key on `geo_distance` specifically, not on "is a
        # BinaryExpr". A text-search rank is an unbounded ordering that has
        # always been allowed, and this plan must not quietly outlaw it.
        sql = _sql(registry, Note.select().order_by(Note.search.rank("llamas").desc()))
        assert "ORDER BY" in sql
        assert "ts_rank" in sql

    def test_nearest_is_refused_in_a_where_clause(self, registry: Registry) -> None:
        # `where()` already refuses every distance operator by name, at build
        # time. Putting `geo_distance` in DISTANCE_OPERATORS earns that refusal
        # rather than needing one of its own -- which is the point of reusing
        # the existing node shape instead of inventing a geospatial one.
        with pytest.raises(TypeError, match="yields a distance"):
            Station.select().where(Station.at.nearest(SYDNEY))
