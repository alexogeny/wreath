from __future__ import annotations

import pytest

from wreath.geospatial import Coordinate
from wreath.orm import types as orm_types


class TestPointColumnType:
    """`Point` is a core PostgreSQL type, not an extension one."""

    def test_point_is_declared_with_the_core_oid(self) -> None:
        # 600 is `point` in pg_type, allocated in the catalog rather than by
        # CREATE EXTENSION -- which is why this is a plain PgType and why the
        # tier-1 no-extension claim can hold at all.
        assert orm_types.Point.oid == 600
        assert orm_types.Point.sql == "point"

    def test_it_coerces_a_coordinate_and_refuses_anything_else(self) -> None:
        here = Coordinate(lat=-33.8, lon=151.2)
        assert orm_types.Point.coerce(here) == here
        for bad in ((151.2, -33.8), [151.2, -33.8], "(151.2,-33.8)", 1.0, None):
            with pytest.raises((TypeError, ValueError)):
                orm_types.Point.coerce(bad)

    def test_a_bare_pair_is_refused_by_the_column_too(self) -> None:
        # The keyword-only rule is the module's headline decision; a column that
        # quietly accepted a positional pair would reintroduce the transposition
        # bug one layer down, where it is harder to see.
        with pytest.raises(TypeError):
            orm_types.Point.coerce((151.2, -33.8))


class TestPointWireFormat:
    """PostgreSQL hands `point` back as its text form; `from_wire` reads it."""

    def test_text_wire_bytes_round_trip(self) -> None:
        here = Coordinate(lat=-33.8, lon=151.2)
        # x is longitude, y is latitude -- PostGIS convention, and the order the
        # `point` literal uses.
        assert orm_types.Point.from_wire(b"(151.2,-33.8)") == here

    def test_wire_form_is_x_lon_y_lat(self) -> None:
        got = orm_types.Point.from_wire(b"(10.5,-20.25)")
        assert got.lon == 10.5
        assert got.lat == -20.25

    def test_a_str_wire_value_reads_the_same_as_bytes(self) -> None:
        assert orm_types.Point.from_wire("(1.5,2.5)") == orm_types.Point.from_wire(b"(1.5,2.5)")

    def test_binary_wire_bytes_round_trip(self) -> None:
        # 16 bytes, two big-endian float8: x then y. Requested-binary result
        # formats exist on the prepared path, so both forms must decode.
        import struct

        raw = struct.pack("!dd", 151.2, -33.8)
        assert orm_types.Point.from_wire(raw) == Coordinate(lat=-33.8, lon=151.2)

    def test_a_malformed_wire_value_is_refused_by_name(self) -> None:
        for bad in (b"", b"151.2,-33.8", b"(151.2)", b"(a,b)", b"(1,2"):
            with pytest.raises(ValueError, match="point"):
                orm_types.Point.from_wire(bad)

    def test_to_wire_produces_the_literal_postgres_parses(self) -> None:
        assert orm_types.Point.to_wire(Coordinate(lat=-33.8, lon=151.2)) == "(151.2,-33.8)"
