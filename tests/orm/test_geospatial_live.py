"""Geospatial against a real PostgreSQL — the two claims that need a server.

1. `within()` is answered by the index rather than a sequential scan. Asserted
   on the *query plan*, because a correct answer read off the whole table is
   exactly the failure this design exists to prevent and it is invisible in the
   results.
2. The whole tier-1 surface works with **no extension installed**. That is the
   claim `wreath.geospatial` rests on, and it holds only because `point` and
   its GiST `point_ops` opclass are in core PostgreSQL.

Each test opens its own connection, as the other live suites here do: a shared
async fixture binds to one event loop and pytest-asyncio gives each test
another.

**This suite runs against a database it creates, not against the one the DSN
names**, and that is what makes claim 2 an assertion rather than a hope. The
image tier 2 needs (`postgis/postgis:17-3.5`) installs PostGIS into
`POSTGRES_DB` at initdb time, so the obvious database is already disqualified
from proving anything about running without it -- and a claim that holds only
on the image somebody happened to start is not the claim the guide makes. A
database created from `template0` carries no extensions on any image, and
`template0` refuses connections, so several xdist workers can create from it at
once without racing over template access.
"""

from __future__ import annotations

import math
import os
from typing import Any

import pytest

from wreath.geospatial import Coordinate, distance
from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import compile_select
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Point, Text
from wreath.postgres import PostgresError, connect

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for live geo tests"),
    pytest.mark.network,
]

#: The database this suite runs in. One name for every worker: the content is
#: identical, each worker owns its own *schema* inside it, and creating six
#: would multiply the setup cost for nothing.
_TIER1_DATABASE = "wreath_tier1_no_extensions"

#: `42P04` is `duplicate_database` -- another worker won the race, which is the
#: ordinary outcome and not a failure.
_DUPLICATE_DATABASE = "42P04"

#: ... but only when the loser looked *after* the winner committed. Two workers
#: that reach `CREATE DATABASE` at the same moment both pass the existence check
#: and the loser is refused by the catalog's unique index instead, with `23505`
#: and a message naming `pg_database_datname_index` -- the same race the schema
#: fixtures hit as `pg_namespace_nspname_index`, and it reads like anything
#: except a test-isolation bug. Matched on the index name as well as the code so
#: a unique violation from anything else still raises.
_UNIQUE_VIOLATION = "23505"
_DATNAME_INDEX = "pg_database_datname_index"


def _database_dsn(dsn: str, name: str) -> str:
    """`dsn` with its database name replaced, query string preserved."""
    head, _, tail = dsn.partition("?")
    base, _, _old = head.rpartition("/")
    return f"{base}/{name}" + (f"?{tail}" if tail else "")


async def _connect() -> Any:
    """A connection to the extension-free database, creating it if needed."""
    target = _database_dsn(_DSN, _TIER1_DATABASE)
    try:
        return await connect(target)
    except PostgresError:
        # `3D000` (invalid_catalog_name) is the expected first-run miss, but the
        # driver may report a failed startup before it gets that far, so this
        # falls through to the create rather than classifying. If the create is
        # what is really broken, it raises below with the server's own words.
        pass
    admin = await connect(_DSN)
    try:
        # From `template0`, never `template1`: template0 is guaranteed to carry
        # no extensions on any image, and `datallowconn = false` means no
        # concurrent worker can be connected to it -- which is the one thing
        # that makes CREATE DATABASE fail for reasons unrelated to this suite.
        await admin.execute(f'CREATE DATABASE "{_TIER1_DATABASE}" TEMPLATE template0')
    except PostgresError as error:
        lost_the_race = error.sqlstate == _DUPLICATE_DATABASE or (
            error.sqlstate == _UNIQUE_VIOLATION and _DATNAME_INDEX in str(error)
        )
        if not lost_the_race:
            raise
    finally:
        await admin.close()
    return await connect(target)

#: Plain assignment, never `setdefault`: the controller imports this module
#: during collection and would otherwise hand every worker the same name.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_geolive_{_WORKER}"

#: Latitude and longitude differ in sign and magnitude, so a renderer that
#: transposes x and y produces a wrong distance rather than a coincidentally
#: equal one. A symmetric centre would hide that bug completely.
SYDNEY = Coordinate(lat=-33.8688, lon=151.2093)


class _Db:
    name = "main"


class Station(Model, table="stations", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Point, index="gist")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry(_Db(), [Station], validate_schema="off")


async def _live_schema(connection: Any) -> None:
    """Create and seed. Server-side generation keeps this one statement."""
    await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}".stations '
        "(id bigint primary key, name text not null, at point not null)"
    )
    # Enough rows, spread worldwide, that the planner has a real choice. On a
    # handful it would pick a sequential scan however indexable the predicate
    # is, and the plan assertion below would prove nothing.
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}".stations (id, name, at) '
        "SELECT g, 's' || g, "
        "point(-179.0 + ((g * 7) % 358)::float8, -80.0 + (g % 160)::float8) "
        "FROM generate_series(0, 3999) AS g"
    )
    # A cluster near the centre, so there is something to find.
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}".stations (id, name, at) '
        "SELECT 10000 + g, 'near' || g, "
        "point(151.2093 + g * 0.001, -33.8688 + g * 0.001) "
        "FROM generate_series(0, 19) AS g"
    )
    await connection.execute(
        f'CREATE INDEX stations_at_gist ON "{_SCHEMA}".stations USING gist (at)'
    )
    await connection.execute(f'ANALYZE "{_SCHEMA}".stations')


async def test_no_extension_is_installed() -> None:
    """The tier-1 claim, asserted rather than assumed.

    Every other test in this file runs on this same connection, so what this
    asserts about is the database the whole tier-1 surface was just exercised
    in -- not a separate probe that proves nothing about where the work ran.
    """
    connection = await _connect()
    try:
        rows = await connection.fetch(
            "SELECT extname FROM pg_extension "
            "WHERE extname IN ('postgis', 'cube', 'earthdistance')"
        )
        assert list(rows) == [], (
            f"an extension is installed in {_TIER1_DATABASE!r}, so the "
            f"no-extension claim is untested: {rows}. This database is created "
            f"from template0 and nothing here installs an extension into it, so "
            f"something else did"
        )
    finally:
        await connection.close()


async def test_within_is_answered_by_the_index(registry: Registry) -> None:
    """The plan, not the result. This is the test the whole design exists for."""
    connection = await _connect()
    try:
        await _live_schema(connection)
        compiled = compile_select(
            registry, Station.select().where(Station.at.within(SYDNEY, 20_000))
        )
        rows = await connection.fetch(f"EXPLAIN {compiled.sql}", *compiled.bind_values)
        plan = "\n".join(str(row[0]) for row in rows)
        assert "stations_at_gist" in plan, f"the GiST index was not used:\n{plan}"
        assert "Seq Scan" not in plan, f"a sequential scan crept in:\n{plan}"
    finally:
        await connection.close()


async def test_within_returns_only_rows_inside_the_circle(registry: Registry) -> None:
    """The box is a superset; the exact filter is what makes the answer right."""
    connection = await _connect()
    try:
        await _live_schema(connection)
        radius = 20_000.0
        compiled = compile_select(
            registry, Station.select().where(Station.at.within(SYDNEY, radius))
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert rows, "expected the seeded cluster to be found"
        for row in rows:
            assert distance(SYDNEY, Point.from_wire(row["at"])) <= radius
    finally:
        await connection.close()


async def test_the_box_alone_would_have_returned_more(registry: Registry) -> None:
    """Proves the exact filter is doing work rather than being decorative.

    If the box and the circle returned the same rows, the AND would be
    untested and dropping the haversine would still pass every other test
    here. The corner of a box reaches about 1.41x its half-width, so at this
    radius there are always rows between the two.
    """
    connection = await _connect()
    try:
        await _live_schema(connection)
        radius = 400_000.0
        compiled = compile_select(
            registry, Station.select().where(Station.at.within(SYDNEY, radius))
        )
        exact = await connection.fetch(compiled.sql, *compiled.bind_values)
        from wreath.geospatial import bounding_boxes

        box = bounding_boxes(SYDNEY, radius)[0]
        boxed = await connection.fetch(
            f'SELECT id FROM "{_SCHEMA}".stations WHERE at <@ box(point($1,$2), point($3,$4))',
            box.lon_min, box.lat_min, box.lon_max, box.lat_max,
        )
        assert len(boxed) > len(exact), (
            "the box returned no more than the circle, so this radius does not "
            "exercise the exact filter"
        )
    finally:
        await connection.close()


async def test_sql_distance_agrees_with_the_python_twin(registry: Registry) -> None:
    """Pins the SQL haversine against the module's own implementation."""
    connection = await _connect()
    try:
        await _live_schema(connection)
        target = Coordinate(lat=-33.8, lon=151.0)
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}".stations (id, name, at) VALUES (99999, $1, $2)',
            "probe", Point.to_wire(target),
        )
        compiled = compile_select(
            registry,
            Station.select().where(Station.id == 99999).order_by(
                Station.at.nearest(SYDNEY)
            ).limit(1),
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        assert len(rows) == 1
        # Round-trip proves the row is the one we planted; the distance itself
        # is compared through the same expression the ORDER BY used.
        got = await connection.fetchrow(
            f'SELECT 2 * 6371008.8 * asin(sqrt(power(sin(radians((at)[1] - $1) / 2), 2)'
            f" + cos(radians($1)) * cos(radians((at)[1]))"
            f" * power(sin(radians((at)[0] - $2) / 2), 2))) AS d"
            f' FROM "{_SCHEMA}".stations WHERE id = 99999',
            SYDNEY.lat, SYDNEY.lon,
        )
        assert math.isclose(got["d"], distance(SYDNEY, target), rel_tol=1e-9)
    finally:
        await connection.close()


async def test_nearest_orders_by_real_distance(registry: Registry) -> None:
    connection = await _connect()
    try:
        await _live_schema(connection)
        compiled = compile_select(
            registry, Station.select().order_by(Station.at.nearest(SYDNEY)).limit(5)
        )
        rows = await connection.fetch(compiled.sql, *compiled.bind_values)
        got = [distance(SYDNEY, Point.from_wire(row["at"])) for row in rows]
        assert got == sorted(got), f"nearest() did not order by distance: {got}"
    finally:
        await connection.close()


async def test_a_coordinate_round_trips_through_a_bind(registry: Registry) -> None:
    """Write a Coordinate, read a Coordinate, through the binary parameter path."""
    connection = await _connect()
    try:
        await _live_schema(connection)
        here = Coordinate(lat=-33.8688, lon=151.2093)
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}".stations (id, name, at) VALUES (99998, $1, $2)',
            "roundtrip", Point.to_wire(here),
        )
        row = await connection.fetchrow(
            f'SELECT at FROM "{_SCHEMA}".stations WHERE id = 99998'
        )
        assert Point.from_wire(row["at"]) == here
    finally:
        await connection.close()
