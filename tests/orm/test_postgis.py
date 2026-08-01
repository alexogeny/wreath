"""Tier 2: a PostGIS `geography` column, on the pgvector contract.

Tier 1 needs no extension and is where most of the value is. This is the other
half of the plan: `geography(Point,4326)`, its GiST index, and the startup
resolution that decides whether the extension is there at all.

The contract is deliberately word-for-word pgvector's, because it is the same
mechanism: an extension's type OID is assigned by `CREATE EXTENSION`, so it is
not a compile-time constant, it is read once at startup, and two things must
not move with it -- the plan-cache shape token and the model fingerprint. The
third invariant is a failure rather than a stability: a column of the type on a
database without the extension fails at *startup*, naming it, rather than at
the first query with an unrecognised OID.

**Tier 1's no-extension claim is not weakened by any of this.**
`tests/orm/test_geospatial_live.py` runs the whole tier-1 surface in a database
it creates from `template0`, which carries no extensions on any image; nothing
here touches that database.
"""

from __future__ import annotations

import os
import struct
import uuid
from typing import Any

import pytest

from wreath.geospatial import Coordinate
from wreath.orm import Mapped, Model, column
from wreath.orm.errors import ExtensionNotInstalledError
from wreath.orm.introspection import probe_extension_types, resolve_extension_types
from wreath.orm.registry import Registry
from wreath.orm.schema import fingerprint_model
from wreath.orm.types import (
    EXT_KIND_GEOGRAPHY,
    ExtensionType,
    Geography,
    Int64,
    Text,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.postgres import connect

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: The OID this process pretends the database assigned `geography` when no live
#: suite has read a real one. A process resolves an extension type exactly once,
#: so this has to be the only invented value in the file.
#:
#: **It must also differ from every other suite's invented OID**, because the
#: codec table is per *process* and keyed by OID: registering one OID under two
#: kinds is refused, correctly, and the refusal lands as a setup ERROR in
#: whichever suite happened to run second. The set in use is 987654 `vector`,
#: 987655 `halfvec`, 987656 `sparsevec`, 987657 `geography`.
GEOGRAPHY_OID = 987657

SYDNEY = Coordinate(lat=-33.8688, lon=151.2093)


class Database:
    name = "main"


class Station(Model, table="stations", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    at: Mapped[Coordinate] = column(Geography(), index="gist")


def _geography_oid() -> int:
    """The OID this process holds for `geography`, binding one if it holds none."""
    oid = GEOGRAPHY_OID
    for item in declared_extension_types():
        if item.type_name == "geography" and item.oid:
            oid = item.oid
            break
    bind_extension_oid("geography", oid)
    return oid


def _bound(srid: int = 4326) -> ExtensionType:
    """A `Geography` with an OID, as startup resolution would leave it.

    `to_wire` refuses an unbound type -- OID 0 means "unspecified" on the wire
    -- so anything asserting on the wire form has to bind first, exactly as the
    application lifespan does. `bind_extension_oid` walks the types declared
    *when it is called*, so the binding follows the construction.
    """
    declared = Geography(srid=srid)
    _geography_oid()  # binds every declared geography, this one included
    return declared


def _fingerprint(spec: Any) -> bytes:
    return fingerprint_model(
        spec.schema, spec.table, spec.columns, spec.relationships,
        spec.table_uniques, spec.table_indexes,
    )


# --- declaration --------------------------------------------------------------


def test_the_column_spells_the_type_postgres_will_spell_back() -> None:
    """`format_type` writes `geography(Point,4326)`, with no space.

    The descriptor carries this spelling and the catalog read produces
    `format_type`'s; a difference of one byte reports the column as drifted on
    every run forever, which is the pgvector opclass defect in another type.
    """
    assert Geography().sql == "geography(Point,4326)"


def test_a_non_default_srid_is_spelled_and_distinguished() -> None:
    other = Geography(srid=4269)
    assert other.sql == "geography(Point,4269)"
    assert other.shape_value != Geography().shape_value


def test_the_extension_is_named_as_create_extension_spells_it() -> None:
    assert Geography().extension == "postgis"
    assert Geography().type_name == "geography"


@pytest.mark.parametrize("srid", [0, -1, "4326", 4326.0, True])
def test_a_nonsense_srid_is_refused_at_declaration(srid: Any) -> None:
    from wreath.orm import DeclarationError

    with pytest.raises(DeclarationError):
        Geography(srid=srid)


def test_the_column_holds_a_coordinate_and_nothing_else() -> None:
    coerce = Geography().coerce
    assert coerce(SYDNEY) is SYDNEY
    with pytest.raises(TypeError):
        coerce(((-33.8688), 151.2093))
    with pytest.raises(TypeError):
        coerce("POINT(151.2093 -33.8688)")


# --- the two things that must not move with the OID ---------------------------


def test_the_shape_token_is_name_derived_not_oid_derived() -> None:
    geography = Geography()
    assert geography.shape_value == b"xgeography(Point,4326)"


def test_the_shape_token_does_not_change_when_the_oid_does() -> None:
    geography = Geography(srid=4267)
    before = geography.shape_value
    assert geography.oid == 0
    assert geography.oid != _geography_oid()
    assert geography.shape_value == before


def test_the_model_fingerprint_does_not_move_with_the_oid() -> None:
    oid = _geography_oid()
    registry = Registry(Database(), [Station], validate_schema="off")
    spec = registry.spec_for(Station)
    before = _fingerprint(spec)
    assert spec.by_name["at"].oid == oid  # non-zero: the OID really moved
    assert _fingerprint(spec) == before


def test_two_srids_fingerprint_differently() -> None:
    class Wgs(Model, table="wide", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Geography())

    class Nad(Model, table="wide", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        at: Mapped[Coordinate] = column(Geography(srid=4269))

    wgs = Registry(Database(), [Wgs], validate_schema="off").spec_for(Wgs)
    nad = Registry(Database(), [Nad], validate_schema="off").spec_for(Nad)
    assert _fingerprint(wgs) != _fingerprint(nad)


def test_geography_declares_its_own_codec_kind() -> None:
    """A kind of its own, not `vector`'s: the wire formats share nothing.

    A codec that reused a vector kind would decode EWKB as a list of floats and
    return a plausible answer rather than raising, which is the failure the
    kind table exists to make impossible.
    """
    from wreath.orm import types as orm_types

    assert orm_types._EXTENSION_KINDS["geography"] == EXT_KIND_GEOGRAPHY
    assert EXT_KIND_GEOGRAPHY not in {
        orm_types._EXTENSION_KINDS[name]
        for name in ("vector", "halfvec", "sparsevec")
    }


def test_every_declared_geography_is_discoverable() -> None:
    declared = declared_extension_types()
    assert any(item.type_name == "geography" for item in declared)
    assert all(isinstance(item, ExtensionType) for item in declared)


# --- the wire form ------------------------------------------------------------
#
# `to_wire` produces EWKB hex, which is what `geography_in` reads on the text
# parameter path *and* what un-hexing gives `geography_recv` on the binary one.
# One spelling serves both, exactly as `Point`'s `(x,y)` literal does -- and for
# the same reason: a column type with two representations has two places for the
# longitude/latitude order to be got wrong.


def test_the_wire_form_is_ewkb_hex_with_the_srid_flag() -> None:
    wire = _bound().to_wire(SYDNEY)
    raw = bytes.fromhex(wire)
    assert len(raw) == 25, wire
    order, kind, srid = struct.unpack_from("<BII", raw, 0)
    assert order == 1  # little-endian
    assert kind == 0x20000001  # point, with the SRID flag set
    assert srid == 4326


def test_the_wire_form_puts_longitude_first() -> None:
    """The convention PostGIS, GeoJSON and PostgreSQL's own `point` all share.

    `Coordinate` refuses a positional pair precisely because this order is the
    opposite of the one people say aloud, so this is the assertion that catches
    a transposition at the one boundary where it cannot be seen by reading.
    """
    raw = bytes.fromhex(_bound().to_wire(SYDNEY))
    x, y = struct.unpack_from("<dd", raw, 9)
    assert x == pytest.approx(SYDNEY.lon)
    assert y == pytest.approx(SYDNEY.lat)


def test_the_wire_form_round_trips_through_from_wire() -> None:
    geography = _bound()
    assert geography.from_wire(geography.to_wire(SYDNEY)) == SYDNEY


def test_from_wire_reads_the_binary_form_the_prepared_path_returns() -> None:
    """A prepared read hands back EWKB bytes, not the hex text form."""
    geography = _bound()
    assert geography.from_wire(bytes.fromhex(geography.to_wire(SYDNEY))) == SYDNEY


def test_from_wire_reads_a_big_endian_ewkb() -> None:
    """Byte order is a field, not an assumption; a peer may write either."""
    payload = struct.pack(">BIIdd", 0, 0x20000001, 4326, SYDNEY.lon, SYDNEY.lat)
    assert Geography().from_wire(payload) == SYDNEY


def test_from_wire_reads_an_ewkb_with_no_srid() -> None:
    """`geography` defaults to 4326, and a WKB without the flag is legal input."""
    payload = struct.pack("<BIdd", 1, 1, SYDNEY.lon, SYDNEY.lat)
    assert Geography().from_wire(payload) == SYDNEY


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x02" + b"\x00" * 24,                                  # bad byte order
        struct.pack("<BIIdd", 1, 0x20000003, 4326, 1.0, 2.0),    # a polygon
        struct.pack("<BIIdd", 1, 0x20000001, 4326, 1.0, 2.0)[:20],
        b"not hex at all",
    ],
)
def test_an_unreadable_wire_value_is_refused_rather_than_guessed(payload: Any) -> None:
    with pytest.raises(ValueError):
        Geography().from_wire(payload)


# The binary parameter codec -- which a `geography` needs in *both* twins, the
# same wall `point` hit one type further out -- is held byte-for-byte by
# `tests/orm/test_geospatial_codec_parity.py`, next to `point`'s. Driving it for
# real is `test_a_coordinate_round_trips_through_a_geography_column` below.


# --- startup ------------------------------------------------------------------


class _NoExtensionConnection:
    """A connection whose database has no extensions at all."""

    async def fetchrow(self, sql: str, *args: Any) -> tuple[Any, ...]:
        return (0, "", "public")


class _FakeDatabase:
    name = "postgisless"

    def pool(self, workload: str) -> Any:
        raise KeyError(workload)

    async def acquire(self, workload: str) -> Any:
        return _NoExtensionConnection()

    async def release(self, workload: str, connection: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_an_absent_postgis_fails_at_startup_naming_it() -> None:
    registry = Registry(_FakeDatabase(), [Station], validate_schema="off")
    with pytest.raises(ExtensionNotInstalledError) as caught:
        await resolve_extension_types(registry)
    message = str(caught.value)
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in message
    assert "Station.at" in message
    assert caught.value.extension == "postgis"


@pytest.mark.asyncio
async def test_binding_a_value_before_resolution_names_the_call() -> None:
    late = Geography(srid=4268)
    try:
        with pytest.raises(ExtensionNotInstalledError) as caught:
            late.to_wire(SYDNEY)
    finally:
        from wreath.orm import types as orm_types

        orm_types._DECLARED_EXTENSION_TYPES.remove(late)
    message = str(caught.value)
    assert "geography" in message
    assert "resolve_extension_types" in message
    assert caught.value.extension == "postgis"


# --- against a real PostGIS ----------------------------------------------------


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.asyncio
@pytest.mark.database
async def test_the_real_oid_is_read_from_the_catalog() -> None:
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except Exception:  # noqa: BLE001 - reported as a skip, see below
            # A server without PostGIS cannot answer this, and the suite must
            # say so rather than pass by accident. The image is
            # postgis/postgis:17-3.5, not the pgvector one the rest of the
            # repository uses -- tier 2 is the only thing that needs it.
            pytest.skip("this PostgreSQL has no PostGIS; use postgis/postgis:17-3.5")
        found = await probe_extension_types(db, {"geography": "postgis"})
        assert len(found) == 1
        assert found[0].installed
        assert found[0].oid > 16384, "an extension OID is user-assigned, not built in"
    finally:
        await db.close()


@pytest.mark.skipif(_DSN is None, reason="set WREATH_TEST_POSTGRES_DSN for PostGIS tests")
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_coordinate_round_trips_through_a_geography_column() -> None:
    """Write a Coordinate, read a Coordinate, through the binary parameter path.

    This is the test that decides whether the wire codec needed a change: a
    `geography` OID the encoder does not enumerate raises rather than falling
    back, so an unhandled type fails here and nowhere earlier.
    """
    from wreath.orm.types import _unbind_extension_oids

    db = await connect(_DSN)
    schema = f"wreath_postgis_{uuid.uuid4().hex[:12]}"
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except Exception:  # noqa: BLE001 - reported as a skip, see above
            pytest.skip("this PostgreSQL has no PostGIS; use postgis/postgis:17-3.5")
        rows = await db.fetch("SELECT oid FROM pg_type WHERE typname = 'geography'")
        if not rows:
            pytest.skip("this PostGIS installs no 'geography' type")
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await db.execute(
            f'CREATE TABLE "{schema}".stations '
            "(id bigint primary key, at geography(Point,4326) not null)"
        )
        # The OID *this server* assigned, never the invented one the offline
        # tests bind. A process resolves an extension type once, so whichever
        # test ran first decided it -- and binding the fake here would frame the
        # parameter against an OID no database has. Releasing first is what
        # makes this suite's result independent of collection order.
        geography = Geography()
        _unbind_extension_oids()
        bind_extension_oid("geography", int(rows[0][0]))
        await db.execute(
            f'INSERT INTO "{schema}".stations (id, at) VALUES (1, $1)',
            geography.to_wire(SYDNEY),
        )
        row = await db.fetchrow(f'SELECT at FROM "{schema}".stations WHERE id = 1')
        assert geography.from_wire(row["at"]) == SYDNEY
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()
