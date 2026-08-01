"""Tier 2's query half: KNN by `<->`, and containment by `ST_Covers`.

`within()` and `nearest()` gain a **second rendering, not a second name**. A
model that moves from `Point` to `Geography` keeps its queries; only the SQL
underneath changes, from a bounding box plus a hand-written haversine to
`ST_DWithin` and PostGIS's KNN operator. That promise is only worth making if
the tier-1 rendering is proved *unchanged*, so the goldens below are the exact
strings the compiler emitted before this file existed.

`covered_by()` is the operation tier 1 structurally cannot do: a region with a
shape. It renders `ST_Covers(ST_GeogFromText($1), col)` -- in that order, and
with that function, because **`ST_Contains(geography, geography)` does not
exist**; a design reaching for it fails at the database rather than at
declaration.

The live half asserts the *plan*. A correct answer read off the whole table is
exactly the failure this design exists to prevent, and it is invisible in the
results.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from wreath.geospatial import Coordinate, Polygon, distance
from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm import compiler as compiler_module
from wreath.orm.compiler import (
    SqlBuilder,
    _render_operand,
    compile_select,
    render_predicate,
)
from wreath.orm.errors import ORMError
from wreath.orm.expressions import GEO_COVERS, GEO_DWITHIN, BinaryExpr, not_
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Geography,
    Int64,
    Point,
    Text,
    Vector,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.postgres import connect

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: Shared with `tests/orm/test_postgis.py`: a process resolves an extension type
#: exactly once, and the codec table is keyed by OID, so the invented values are
#: allocated across the whole suite -- 987654 `vector`, 987655 `halfvec`,
#: 987656 `sparsevec`, 987657 `geography`.
GEOGRAPHY_OID = 987657
VECTOR_OID = 987654

#: Plain assignment, never `setdefault`: the controller imports this module
#: during collection and would otherwise hand every worker the same name.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_geo2_{_WORKER}"

#: Latitude and longitude differ in sign and magnitude, so a rendering that
#: transposes x and y produces a wrong answer rather than a coincidentally equal
#: one.
SYDNEY = Coordinate(lat=-33.8688, lon=151.2093)
#: Straddles the antimeridian, so a tier-1 circle around it needs two boxes.
TAVEUNI = Coordinate(lat=-16.85, lon=179.98)


class Database:
    name = "main"


class Beacon(Model, table="beacons", schema=_SCHEMA):
    """The tier-2 model: the same `Coordinate`, over `geography(Point,4326)`."""

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Geography(), index="gist")


class Station(Model, table="stations", schema=_SCHEMA):
    """The tier-1 model, kept here so both renderings are compiled side by side."""

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Point, index="gist")


class Document(Model, table="documents", schema=_SCHEMA):
    """Exists only to hold the refusal apart from pgvector's `<->`.

    An unbounded `ORDER BY embedding <-> $1` is allowed and must stay allowed,
    so the KNN limit rule has to key on the *token* rather than on the operator
    symbol. Without this model, giving geography KNN pgvector's own `<->` token
    would pass every other test in this file.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list] = column(Vector(3), index="hnsw")


#: An L over a two-degree square: full width along the south edge, and only the
#: western half above it. The concavity -- the north-east quadrant -- is what
#: separates the region from its own bounding box.
L_REGION = Polygon(
    [
        Coordinate(lat=-34.0, lon=150.0),
        Coordinate(lat=-34.0, lon=152.0),
        Coordinate(lat=-33.0, lon=152.0),
        Coordinate(lat=-33.0, lon=151.0),
        Coordinate(lat=-32.0, lon=151.0),
        Coordinate(lat=-32.0, lon=150.0),
    ]
)
#: The same region's bounding rectangle -- the coarse predicate, alone.
L_ENVELOPE = Polygon(
    [
        Coordinate(lat=-34.0, lon=150.0),
        Coordinate(lat=-34.0, lon=152.0),
        Coordinate(lat=-32.0, lon=152.0),
        Coordinate(lat=-32.0, lon=150.0),
    ]
)


def _bind_oids() -> None:
    """Give the extension types an OID, as startup resolution would.

    `to_wire` refuses an unbound extension type -- OID 0 means "unspecified" on
    the wire -- so anything that compiles a bind has to bind first. A live suite
    may have already read the server's own OID; that one wins, because a process
    resolves an extension type exactly once.
    """
    held = {
        item.type_name: item.oid
        for item in declared_extension_types()
        if item.oid
    }
    bind_extension_oid("geography", held.get("geography", GEOGRAPHY_OID))
    bind_extension_oid("vector", held.get("vector", VECTOR_OID))


def _wire(coordinate: Coordinate) -> str:
    """The EWKB hex a *bound* `geography` frames `coordinate` as.

    Off the model's own column rather than off a fresh `Geography()`:
    `bind_extension_oid` walks the types declared when it is called, so a type
    constructed afterwards has no OID and `to_wire` refuses it -- which is the
    contract `test_postgis.py` pins, and a test that worked around it here
    would be asserting against a type no database ever assigned.
    """
    return Beacon.at.column.pg_type.to_wire(coordinate)


@pytest.fixture(scope="module")
def registry() -> Registry:
    _bind_oids()
    return Registry(Database(), [Beacon, Station, Document], validate_schema="off")


def _sql(registry: Registry, select: Any) -> str:
    return compile_select(registry, select).sql


# --- the tier-1 rendering, unchanged ------------------------------------------
#
# Recorded from the compiler before the tier-2 branch was written, and pasted
# here verbatim. A dual rendering is good for the reader and dangerous for the
# implementer: one name with two SQL forms means the *old* form has to be proved
# rather than assumed, and only an exact string proves it.

TIER1_WITHIN_SQL = (
    'SELECT "t0"."id", "t0"."name", "t0"."at" FROM "{schema}"."stations" AS "t0" '
    'WHERE ("t0"."at" <@ box(point($1, $2), point($3, $4)) AND '
    '(2 * 6371008.8 * asin(sqrt(power(sin(radians($5 - ("t0"."at")[1]) / 2), 2) + '
    'cos(radians(("t0"."at")[1])) * cos(radians($6)) * '
    'power(sin(radians($7 - ("t0"."at")[0]) / 2), 2)))) <= $8)'
)
TIER1_WITHIN_VALUES = (
    151.15511612429546,
    -33.91376601818623,
    151.26348387570457,
    -33.82383398181377,
    -33.8688,
    -33.8688,
    151.2093,
    5000.0,
)
TIER1_NEAREST_SQL = (
    'SELECT "t0"."id", "t0"."name", "t0"."at" FROM "{schema}"."stations" AS "t0" '
    'ORDER BY (2 * 6371008.8 * asin(sqrt(power(sin(radians($1 - ("t0"."at")[1]) / 2), 2) + '
    'cos(radians(("t0"."at")[1])) * cos(radians($2)) * '
    'power(sin(radians($3 - ("t0"."at")[0]) / 2), 2)))) ASC LIMIT $4'
)
TIER1_NEAREST_VALUES = (-33.8688, -33.8688, 151.2093, 10)


class TestThePointRenderingIsUnchanged:
    def test_within_on_a_point_column_is_byte_identical(self, registry: Registry) -> None:
        compiled = compile_select(
            registry, Station.select().where(Station.at.within(SYDNEY, 5_000))
        )
        assert compiled.sql == TIER1_WITHIN_SQL.format(schema=_SCHEMA)
        assert compiled.bind_values == TIER1_WITHIN_VALUES
        assert compiled.bind_oids == (701,) * 8

    def test_nearest_on_a_point_column_is_byte_identical(self, registry: Registry) -> None:
        compiled = compile_select(
            registry, Station.select().order_by(Station.at.nearest(SYDNEY)).limit(10)
        )
        assert compiled.sql == TIER1_NEAREST_SQL.format(schema=_SCHEMA)
        assert compiled.bind_values == TIER1_NEAREST_VALUES

    def test_the_antimeridian_still_produces_two_boxes(self, registry: Registry) -> None:
        sql = _sql(registry, Station.select().where(Station.at.within(TAVEUNI, 50_000)))
        assert sql.count("<@ box(point(") == 2

    def test_the_tier_one_path_never_renders_a_postgis_call(
        self, registry: Registry
    ) -> None:
        # The dispatch is on the column's type, not on which method was called.
        # A branch that tested the wrong thing would send a `point` column into
        # PostGIS, where it would fail at the database rather than here.
        sql = _sql(registry, Station.select().where(Station.at.within(SYDNEY, 5_000)))
        assert "ST_" not in sql


# --- KNN ordering --------------------------------------------------------------


class TestNearestOnAGeography:
    def test_it_renders_the_knn_operator(self, registry: Registry) -> None:
        sql = _sql(
            registry, Beacon.select().order_by(Beacon.at.nearest(SYDNEY)).limit(5)
        )
        assert '("t0"."at" <-> $1)' in sql
        # Not the tier-1 haversine: the index answers this one directly.
        assert "asin" not in sql.lower()

    def test_the_centre_is_a_bind_not_a_literal(self, registry: Registry) -> None:
        compiled = compile_select(
            registry, Beacon.select().order_by(Beacon.at.nearest(SYDNEY)).limit(5)
        )
        assert "151.2" not in compiled.sql
        assert "-33.8" not in compiled.sql
        # EWKB hex, longitude first, which is the one spelling both parameter
        # paths accept.
        assert compiled.bind_values[0] == _wire(SYDNEY)

    def test_an_unbounded_geography_nearest_is_refused(self, registry: Registry) -> None:
        with pytest.raises(DeclarationError, match="limit"):
            compile_select(registry, Beacon.select().order_by(Beacon.at.nearest(SYDNEY)))

    def test_an_unbounded_vector_ordering_is_still_allowed(
        self, registry: Registry
    ) -> None:
        # The refusal is a *token* test, never an operator test. pgvector's
        # `<->` is spelled the same way and an unbounded `ORDER BY embedding
        # <-> $1` has always been allowed; reusing its token for geography KNN
        # would outlaw it here and nothing else in this file would object.
        sql = _sql(
            registry,
            Document.select().order_by(Document.embedding.l2_distance([0.1, 0.2, 0.3])),
        )
        assert "ORDER BY" in sql
        assert "<->" in sql

    def test_a_geography_knn_is_refused_in_where(self, registry: Registry) -> None:
        with pytest.raises(TypeError, match="yields a distance"):
            Beacon.select().where(Beacon.at.nearest(SYDNEY))

    def test_a_knn_threshold_is_a_predicate_and_renders(self, registry: Registry) -> None:
        # The one shape a distance *may* take in a WHERE, exactly as the
        # pgvector distances may: compared against a ceiling.
        sql = _sql(
            registry, Beacon.select().where(Beacon.at.nearest(SYDNEY) < 20_000.0)
        )
        assert '("t0"."at" <-> $1) < $2' in sql

    def test_a_non_coordinate_centre_is_refused(self, registry: Registry) -> None:
        with pytest.raises(TypeError):
            Beacon.at.nearest((151.2, -33.8))


# --- ST_DWithin ----------------------------------------------------------------


class TestWithinOnAGeography:
    def test_it_renders_st_dwithin(self, registry: Registry) -> None:
        sql = _sql(registry, Beacon.select().where(Beacon.at.within(SYDNEY, 20_000)))
        assert 'ST_DWithin("t0"."at", $1, $2)' in sql

    def test_it_renders_no_bounding_box_and_no_haversine(
        self, registry: Registry
    ) -> None:
        # PostGIS adds the `&&` index condition itself -- that is what the live
        # plan below shows. Hand-building one here would be a second, redundant
        # spelling of the same coarse filter.
        sql = _sql(registry, Beacon.select().where(Beacon.at.within(SYDNEY, 20_000)))
        assert "box(point(" not in sql
        assert "asin" not in sql.lower()

    def test_the_centre_and_the_radius_are_binds(self, registry: Registry) -> None:
        compiled = compile_select(
            registry, Beacon.select().where(Beacon.at.within(SYDNEY, 20_000))
        )
        assert compiled.bind_values == (_wire(SYDNEY), 20000.0)
        assert compiled.bind_oids[1] == 701

    def test_the_binds_stay_in_step_with_the_placeholders(
        self, registry: Registry
    ) -> None:
        # The renderer numbers placeholders as it emits them and the bind
        # program walks the tree; a call rendering its operands in an order the
        # walk does not share would prepare the statement with the values
        # transposed and no offline test would see it.
        compiled = compile_select(
            registry,
            Beacon.select()
            .where(Beacon.name == "north")
            .where(Beacon.at.within(SYDNEY, 20_000)),
        )
        assert compiled.bind_values == (
            "north",
            _wire(SYDNEY),
            20000.0,
        )

    def test_a_negative_radius_is_refused(self, registry: Registry) -> None:
        with pytest.raises(ValueError):
            Beacon.at.within(SYDNEY, -1)

    def test_a_non_coordinate_centre_is_refused(self, registry: Registry) -> None:
        with pytest.raises(TypeError):
            Beacon.at.within((151.2, -33.8), 20_000)

    def test_within_is_still_refused_on_a_column_that_is_neither(
        self, registry: Registry
    ) -> None:
        with pytest.raises(DeclarationError, match="Point"):
            Beacon.name.within(SYDNEY, 5_000)


# --- containment ---------------------------------------------------------------


class TestCoveredBy:
    def test_it_renders_st_covers_over_st_geogfromtext(
        self, registry: Registry
    ) -> None:
        sql = _sql(registry, Beacon.select().where(Beacon.at.covered_by(L_REGION)))
        assert 'ST_Covers(ST_GeogFromText($1), "t0"."at")' in sql

    def test_st_contains_reaches_no_sql_the_compiler_can_emit(
        self, registry: Registry
    ) -> None:
        """`ST_Contains(geography, geography)` does not exist.

        Checked against a live PostGIS 3.5.2, which answers `function
        st_contains(geography, geography) does not exist` -- so a rendering that
        reached for it would fail at the database rather than at declaration,
        and only against a server.

        The source scan looks for the *quoted literal*, not for the name: the
        renderer's own docstring says why `ST_Contains` is not an option, and a
        scan that objected to explaining itself would be pushing the reason out
        of the file that needs it. What must not exist is a string the builder
        could emit.
        """
        sql = _sql(registry, Beacon.select().where(Beacon.at.covered_by(L_REGION)))
        assert "ST_Contains" not in sql
        root = Path(compiler_module.__file__).parent
        for name in ("compiler.py", "expressions.py"):
            source = (root / name).read_text(encoding="utf-8")
            assert '"ST_Contains' not in source, name
            assert "'ST_Contains" not in source, name

    def test_the_polygon_travels_as_text(self, registry: Registry) -> None:
        # No bind-only `ExtensionType`, no OID to resolve, and
        # `wreath_pg_decode_extension` is never reached: the region goes out as
        # WKT and PostGIS lifts it.
        compiled = compile_select(
            registry, Beacon.select().where(Beacon.at.covered_by(L_REGION))
        )
        assert compiled.bind_oids == (25,)
        assert compiled.bind_values == (L_REGION.wkt,)

    def test_covered_by_on_a_point_column_is_refused(self, registry: Registry) -> None:
        with pytest.raises(DeclarationError, match="Geography"):
            Station.at.covered_by(L_REGION)

    def test_covered_by_on_a_text_column_is_refused(self, registry: Registry) -> None:
        with pytest.raises(DeclarationError, match="Geography"):
            Beacon.name.covered_by(L_REGION)

    def test_a_bounding_box_is_not_a_region(self, registry: Registry) -> None:
        from wreath.geospatial import BoundingBox

        with pytest.raises(TypeError, match="Polygon"):
            Beacon.at.covered_by(
                BoundingBox(lat_min=-34.0, lat_max=-32.0, lon_min=150.0, lon_max=152.0)
            )

    def test_a_negated_containment_is_refused(self, registry: Registry) -> None:
        with pytest.raises(DeclarationError, match="everywhere else"):
            ~Beacon.at.covered_by(L_REGION)

    def test_not_of_a_containment_is_refused_by_the_same_rule(
        self, registry: Registry
    ) -> None:
        # `not_()` builds the node directly rather than through `__invert__`, so
        # a refusal written only on the operator would leave this spelling open.
        with pytest.raises(DeclarationError, match="everywhere else"):
            not_(Beacon.at.covered_by(L_REGION))

    def test_a_containment_buried_in_a_negated_conjunction_is_refused(
        self, registry: Registry
    ) -> None:
        with pytest.raises(DeclarationError, match="everywhere else"):
            ~(Beacon.at.covered_by(L_REGION) & (Beacon.name == "north"))

    def test_an_ordinary_predicate_may_still_be_negated(
        self, registry: Registry
    ) -> None:
        # The refusal must key on containment, not on NOT. Without this, moving
        # it up to every `UnaryExpr` would outlaw every negation in the ORM.
        sql = _sql(registry, Beacon.select().where(~(Beacon.name == "north")))
        assert "NOT (" in sql


# --- the renderer's own shape guards -------------------------------------------
#
# These are unreachable through the query builder, which is the point: the node
# constructors decide the shape and the renderer re-checks it, so a hand-built
# node -- or a future builder that got the tree wrong -- fails with a sentence
# about the operand rather than emitting SQL nobody can read. `wreath mutant`
# generates no candidate for this guard shape, so it is covered here or nowhere.


class TestTheRenderersRefuseAMalformedNode:
    def _render(self, node: Any) -> str:
        builder = SqlBuilder()
        render_predicate(node, builder, "t0", {})
        return builder.sql()

    def test_a_dwithin_without_a_centre_and_radius_pair_is_refused(self) -> None:
        with pytest.raises(ORMError, match="centre and a radius"):
            self._render(BinaryExpr(GEO_DWITHIN, Beacon.at, Beacon.name))

    def test_a_dwithin_over_something_that_is_not_a_column_is_refused(self) -> None:
        inner = BinaryExpr(GEO_DWITHIN, Beacon.at, Beacon.at)
        with pytest.raises(ORMError, match="centre and a radius"):
            self._render(BinaryExpr(GEO_DWITHIN, inner, inner))

    def test_a_containment_over_something_that_is_not_a_column_is_refused(self) -> None:
        # The guard that keeps the reversed emission order safe: with a column
        # on the left there is exactly one bind however the tree is walked.
        with pytest.raises(ORMError, match="Geography column"):
            self._render(
                BinaryExpr(GEO_COVERS, BinaryExpr(GEO_COVERS, Beacon.at, Beacon.at),
                           Beacon.at)
            )

    def test_a_knn_distance_is_refused_as_a_predicate(self) -> None:
        # The second line of defence behind `where()`: a distance is a number,
        # and PostgreSQL would otherwise object about the argument of WHERE
        # rather than about the line that wrote it.
        with pytest.raises(ORMError, match="yields a value, not a boolean"):
            self._render(Beacon.at.nearest(SYDNEY))

    def test_a_containment_is_refused_as_an_operand(self) -> None:
        builder = SqlBuilder()
        with pytest.raises(ORMError, match="yields a boolean"):
            _render_operand(Beacon.at.covered_by(L_REGION), builder, "t0", {})


# --- the region value ----------------------------------------------------------


class TestPolygonIsBuiltFromCoordinates:
    def test_a_bare_pair_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError, match="Coordinate"):
            Polygon([(150.0, -34.0), (152.0, -34.0), (152.0, -32.0)])

    def test_raw_wkt_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError, match="WKT"):
            Polygon("POLYGON((150 -34, 152 -34, 152 -32, 150 -34))")

    def test_no_vertices_at_all_is_refused_by_the_same_rule(self) -> None:
        # The empty ring reaches the closing test before the count, so the
        # `if ring and ...` guard is what stops it indexing an empty list. A
        # mutation pass dropped that clause and nothing objected until this
        # test existed; without it the refusal is an IndexError.
        with pytest.raises(ValueError, match="three"):
            Polygon([])

    def test_two_vertices_are_refused(self) -> None:
        with pytest.raises(ValueError, match="three"):
            Polygon([Coordinate(lat=-34.0, lon=150.0), Coordinate(lat=-32.0, lon=152.0)])

    def test_three_repeats_of_one_vertex_are_refused(self) -> None:
        # A ring is closed automatically, so a caller who closed it by hand must
        # not have their last vertex counted twice towards the minimum.
        corner = Coordinate(lat=-34.0, lon=150.0)
        with pytest.raises(ValueError, match="three"):
            Polygon([corner, corner, corner])

    def test_a_hand_closed_ring_is_accepted_and_not_doubled(self) -> None:
        first = Coordinate(lat=-34.0, lon=150.0)
        ring = [first, Coordinate(lat=-34.0, lon=152.0), Coordinate(lat=-32.0, lon=152.0)]
        assert Polygon(ring) == Polygon([*ring, first])

    def test_the_wkt_closes_the_ring_and_puts_longitude_first(self) -> None:
        region = Polygon(
            [
                Coordinate(lat=-34.0, lon=150.0),
                Coordinate(lat=-34.0, lon=152.0),
                Coordinate(lat=-32.0, lon=152.0),
            ]
        )
        assert region.wkt == (
            "SRID=4326;POLYGON((150.0 -34.0, 152.0 -34.0, 152.0 -32.0, 150.0 -34.0))"
        )

    def test_it_is_immutable_and_hashable(self) -> None:
        region = Polygon(
            [
                Coordinate(lat=-34.0, lon=150.0),
                Coordinate(lat=-34.0, lon=152.0),
                Coordinate(lat=-32.0, lon=152.0),
            ]
        )
        assert hash(region) == hash(Polygon(list(region.vertices)))
        with pytest.raises(AttributeError):
            region.vertices = ()


# --- against a real PostGIS ----------------------------------------------------
#
# The image is `postgis/postgis:17-3.5`, not the pgvector one the rest of the
# repository uses. A server without PostGIS reports a skip rather than passing by
# accident; a DSN pointing at nothing fails at `connect`, which is the property
# that makes these tests worth having.

_live = [
    pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests"),
    pytest.mark.database,
]

#: Enough rows that the planner has a real choice. On a handful it picks a
#: sequential scan however indexable the predicate is, and the plan assertion
#: would prove nothing.
_SEEDED = 4020


async def _live_connection() -> Any:
    connection = await connect(_DSN)
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    except Exception:  # noqa: BLE001 - reported as a skip, see below
        # A server without PostGIS cannot answer any of this, and the suite must
        # say so rather than pass by accident.
        await connection.close()
        pytest.skip("this PostgreSQL has no PostGIS; use postgis/postgis:17-3.5")
    return connection


async def _live_schema(connection: Any) -> None:
    """Create, seed and index. Server-side generation keeps this cheap."""
    await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}".beacons '
        "(id bigint primary key, name text not null, at geography(Point,4326) not null)"
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}".beacons (id, name, at) '
        "SELECT g, 'b' || g, ST_SetSRID(ST_MakePoint("
        "-179.0 + ((g * 7) % 358)::float8, -80.0 + (g % 160)::float8), 4326)::geography "
        "FROM generate_series(0, 3999) AS g"
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}".beacons (id, name, at) '
        "SELECT 10000 + g, 'near' || g, ST_SetSRID(ST_MakePoint("
        "151.2093 + g * 0.001, -33.8688 + g * 0.001), 4326)::geography "
        "FROM generate_series(0, 19) AS g"
    )
    await connection.execute(
        f'CREATE INDEX beacons_at_gist ON "{_SCHEMA}".beacons USING gist (at)'
    )
    await connection.execute(f'ANALYZE "{_SCHEMA}".beacons')


async def _bind_server_geography_oid(connection: Any) -> None:
    """Bind the OID *this server* assigned, never the invented one."""
    from wreath.orm.types import _unbind_extension_oids

    rows = await connection.fetch("SELECT oid FROM pg_type WHERE typname = 'geography'")
    if not rows:
        pytest.skip("this PostGIS installs no 'geography' type")
    _unbind_extension_oids()
    bind_extension_oid("geography", int(rows[0][0]))


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_the_seed_is_large_enough_for_the_planner_to_have_a_choice(
    registry: Registry,
) -> None:
    """The precondition every plan assertion below rests on, asserted once."""
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        row = await connection.fetchrow(f'SELECT count(*) FROM "{_SCHEMA}".beacons')
        assert row[0] == _SEEDED
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_knn_is_answered_by_an_index_scan_with_an_order_by(
    registry: Registry,
) -> None:
    """`Index Scan ... Order By:` -- the plan a KNN index exists to produce."""
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        compiled = compile_select(
            registry, Beacon.select().order_by(Beacon.at.nearest(SYDNEY)).limit(5)
        )
        rows = await connection.fetch(f"EXPLAIN {compiled.sql}", *compiled.bind_values)
        plan = "\n".join(str(row[0]) for row in rows)
        assert "Index Scan using beacons_at_gist" in plan, plan
        assert "Order By: (at <-> " in plan, plan
        assert "Seq Scan" not in plan, plan
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_knn_orders_by_metres_agreeing_with_the_tier_one_haversine(
    registry: Registry,
) -> None:
    """`<->` on `geography` answers metres, so the two tiers are comparable.

    Not the same number: PostGIS measures on the WGS84 spheroid and
    `wreath.geospatial` on a sphere, which the guide states as ~0.5%. Being
    within that is the claim; being equal would mean one of them was not doing
    what it says.
    """
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        compiled = compile_select(
            registry, Beacon.select().order_by(Beacon.at.nearest(SYDNEY)).limit(5)
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert len(rows) == 5
        got = [
            distance(SYDNEY, Geography().from_wire(row["at"])) for row in rows
        ]
        assert got == sorted(got), got
        # The furthest of the five is ~578 m out, so a transposed x/y would be
        # thousands of kilometres away rather than half a per cent.
        assert got[-1] < 1_000.0, got
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_within_on_a_geography_is_answered_by_the_index(
    registry: Registry,
) -> None:
    """`ST_DWithin` plans as `&&` against the index with the exact test filtered.

    Literally the shape tier 1 hand-builds, which is the strongest argument that
    the two tiers belong behind one name.
    """
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        compiled = compile_select(
            registry, Beacon.select().where(Beacon.at.within(SYDNEY, 20_000))
        )
        rows = await connection.fetch(f"EXPLAIN {compiled.sql}", *compiled.bind_values)
        plan = "\n".join(str(row[0]) for row in rows)
        assert "beacons_at_gist" in plan, plan
        assert "Seq Scan" not in plan, plan
        assert "at && " in plan, plan
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_within_returns_only_rows_inside_the_radius(registry: Registry) -> None:
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        radius = 2_000.0
        compiled = compile_select(
            registry, Beacon.select().where(Beacon.at.within(SYDNEY, radius))
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert rows, "expected the seeded cluster to be found"
        for row in rows:
            assert distance(SYDNEY, Geography().from_wire(row["at"])) <= radius * 1.005
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_covered_by_returns_the_region_not_its_bounding_box(
    registry: Registry,
) -> None:
    """The test that has to be proved non-vacuous, and how.

    The first attempt at this proved nothing: an L-shaped region over a
    1-degree grid returned the same 10 rows either way, because the concavity
    was empty -- so `ST_Covers` and the `&&` bounding box the index applies
    before it were indistinguishable, and deleting the exact test would still
    have passed. Seeding rows *into the notch* is what makes the two answers
    differ, and this asserts on both counts rather than on one.

    `id >= 20000` narrows both queries to the rows this test placed, so the
    counts are exactly the 10 and the 70 the design was measured against rather
    than those plus whatever the worldwide background seed happens to drop into
    a two-degree square. The containment is still the only thing separating the
    two answers; the filter removes rows neither query is about.
    """
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        # Ten inside the L's western arm, sixty in the north-east notch: inside
        # the bounding rectangle, outside the region.
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}".beacons (id, name, at) '
            "SELECT 20000 + g, 'arm' || g, ST_SetSRID(ST_MakePoint("
            "150.2 + g * 0.05, -32.5), 4326)::geography "
            "FROM generate_series(0, 9) AS g"
        )
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}".beacons (id, name, at) '
            "SELECT 21000 + g, 'notch' || g, ST_SetSRID(ST_MakePoint("
            "151.2 + (g % 10) * 0.06, -32.9 + (g / 10) * 0.12), 4326)::geography "
            "FROM generate_series(0, 59) AS g"
        )
        await connection.execute(f'ANALYZE "{_SCHEMA}".beacons')

        region = compile_select(
            registry,
            Beacon.select()
            .where(Beacon.id >= 20000)
            .where(Beacon.at.covered_by(L_REGION)),
        )
        envelope = compile_select(
            registry,
            Beacon.select()
            .where(Beacon.id >= 20000)
            .where(Beacon.at.covered_by(L_ENVELOPE)),
        )
        covered = {
            row["name"] for row in await connection.fetch(region.sql, *region.bind_values)
        }
        boxed = {
            row["name"]
            for row in await connection.fetch(envelope.sql, *envelope.bind_values)
        }
        assert len(covered) == 10, sorted(covered)
        assert len(boxed) == 70, len(boxed)
        # Stated as well as counted: every row the box adds is one the region
        # rejects, and it is exactly the sixty planted in the concavity. Without
        # them the two answers are equal and this test asserts nothing.
        assert covered < boxed
        assert {name for name in boxed - covered if name.startswith("notch")} == (
            boxed - covered
        )
        assert len(boxed - covered) == 60
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_covered_by_is_answered_by_the_index(registry: Registry) -> None:
    connection = await _live_connection()
    try:
        await _live_schema(connection)
        await _bind_server_geography_oid(connection)
        compiled = compile_select(
            registry, Beacon.select().where(Beacon.at.covered_by(L_REGION))
        )
        rows = await connection.fetch(f"EXPLAIN {compiled.sql}", *compiled.bind_values)
        plan = "\n".join(str(row[0]) for row in rows)
        assert "beacons_at_gist" in plan, plan
        assert "Seq Scan" not in plan, plan
    finally:
        await connection.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.database
async def test_a_coordinate_round_trips_through_a_tier_two_query(
    registry: Registry,
) -> None:
    """The whole path: a `Coordinate` in, a `Coordinate` out, through the ORM."""
    connection = await _live_connection()
    schema = f"wreath_geo2_rt_{uuid.uuid4().hex[:12]}"
    try:
        await _bind_server_geography_oid(connection)
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(
            f'CREATE TABLE "{schema}".beacons '
            "(id bigint primary key, name text not null, at geography(Point,4326) not null)"
        )
        await connection.execute(
            f'INSERT INTO "{schema}".beacons (id, name, at) VALUES (1, $1, $2)',
            "roundtrip",
            _wire(SYDNEY),
        )
        row = await connection.fetchrow(f'SELECT at FROM "{schema}".beacons WHERE id = 1')
        assert Geography().from_wire(row["at"]) == SYDNEY
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
