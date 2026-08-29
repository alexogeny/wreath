from __future__ import annotations

import struct

import pytest

from wreath._pgdriver import _encode_point as _pure_encode_point

_core = pytest.importorskip("wreath._native.postgres", reason="native driver not built")

CASES = [
    "(0.0,0.0)",
    "(151.2093,-33.8688)",
    "(-179.999,89.999)",
    "(180.0,-90.0)",
    "(1e-7,-1e-7)",
    "(1.7976931348623157e+308,-1.7976931348623157e+308)",
]


@pytest.mark.parametrize("literal", CASES)
def test_the_pure_encoder_is_two_big_endian_float8_x_then_y(literal: str) -> None:
    raw = _pure_encode_point(literal)
    assert len(raw) == 16
    x, y = struct.unpack("!dd", raw)
    body = literal[1:-1].split(",")
    assert x == float(body[0])
    assert y == float(body[1])


@pytest.mark.parametrize("literal", CASES)
def test_the_binary_dispatch_routes_oid_600_to_the_point_encoder(literal: str) -> None:
    from wreath._pgdriver import _encode_binary

    assert _encode_binary(literal, 600) == _pure_encode_point(literal)


def test_the_binary_dispatch_still_refuses_an_oid_it_has_no_encoder_for() -> None:
    from wreath._pgdriver import _encode_binary

    with pytest.raises(TypeError, match="no binary encoder"):
        _encode_binary("whatever", 1_000_000)


@pytest.mark.parametrize("bad", ["", "1,2", "(1)", "(a,b)", "(1,2", "1,2)", "(1,2,3)", "()"])
def test_a_malformed_literal_is_refused_rather_than_guessed(bad: str) -> None:
    with pytest.raises(TypeError, match="point codec"):
        _pure_encode_point(bad)


def test_a_non_string_is_refused(bad: object = 1.5) -> None:
    with pytest.raises(TypeError, match="point codec"):
        _pure_encode_point(bad)


def test_the_native_encoder_is_covered_by_the_live_round_trip() -> None:
    from pathlib import Path

    codec = Path(__file__).resolve().parents[2] / "src/wreath/_native/postgres/codec.c"
    source = codec.read_text(encoding="utf-8")
    # If the native encoder is ever removed, the live suite would silently fall
    # back to nothing and this pointer would be a lie.
    assert "encode_point" in source
    assert "PG_POINT" in source


# The same axis one type further out. `geography`'s OID is assigned by
# `CREATE EXTENSION`, so its encoder is selected from the runtime kind table
# rather than from a `case` -- but the wall is identical: the prepared path
# binds in binary and neither twin falls back for an OID it does not enumerate.

#: An OID the Python codec table can be told about without colliding with the one
#: a live PostGIS assigns. Registered into the *pure* table directly, because
#: `bind_extension_oid` writes to whichever backend is active and that is the
#: native one in a normal build.
#:
#: Shared with `tests/orm/test_postgis.py`, and distinct from every other
#: suite's invented OID: the codec table is per process and keyed by OID, so one
#: OID under two kinds is refused and the refusal lands in whichever suite ran
#: second. 987654 `vector`, 987655 `halfvec`, 987656 `sparsevec`, 987657
#: `geography`.
GEOGRAPHY_OID = 987657

GEOGRAPHY_CASES = [
    "0101000020e6100000000000000000000000000000000000f03f",
    "0101000020E6100000000000000060634000000000008040C0",
    "",
]


def _register_pure_geography() -> None:
    from wreath._pgdriver import (
        _EXT_KIND_GEOGRAPHY,
        _register_extension_type,
    )

    _register_extension_type("geography", GEOGRAPHY_OID, _EXT_KIND_GEOGRAPHY)


@pytest.mark.parametrize("hexed", GEOGRAPHY_CASES)
def test_the_pure_encoder_un_hexes_and_nothing_else(hexed: str) -> None:
    from wreath._pgdriver import _encode_geography

    assert _encode_geography(hexed) == bytes.fromhex(hexed)


@pytest.mark.parametrize("hexed", GEOGRAPHY_CASES)
def test_the_binary_dispatch_routes_a_geography_oid_to_its_encoder(hexed: str) -> None:
    from wreath._pgdriver import _encode_binary, _encode_geography

    _register_pure_geography()
    assert _encode_binary(hexed, GEOGRAPHY_OID) == _encode_geography(hexed)


@pytest.mark.parametrize("bad", ["abc", "zz", "01 02", "01\n", "0g"])
def test_a_malformed_hex_value_is_refused_rather_than_guessed(bad: str) -> None:
    from wreath._pgdriver import _encode_geography

    with pytest.raises(TypeError, match="geography codec"):
        _encode_geography(bad)


def test_a_non_string_is_refused_by_the_geography_encoder() -> None:
    from wreath._pgdriver import _encode_geography

    with pytest.raises(TypeError, match="geography codec"):
        _encode_geography(1.5)


def test_an_unknown_codec_kind_is_still_refused_at_registration() -> None:
    from wreath._pgdriver import _register_extension_type

    with pytest.raises(ValueError, match="unknown extension codec kind"):
        _register_extension_type("nonsense", GEOGRAPHY_OID + 1, 99)


def test_the_native_geography_encoder_is_covered_by_the_live_round_trip() -> None:
    from pathlib import Path

    codec = Path(__file__).resolve().parents[2] / "src/wreath/_native/postgres/codec.c"
    source = codec.read_text(encoding="utf-8")
    assert "encode_geography" in source
    assert "WREATH_PG_EXT_GEOGRAPHY" in source


# Reading needed no new decoder for `point`: an OID the driver does not
# enumerate falls through to raw bytes and `Point.from_wire` reads them. A
# *registered* extension OID is different -- the native decoder plan routes
# every one of them to `wreath_pg_decode_extension`, so a kind with no case
# there raises `no decoder for extension OID` instead of falling through, which
# is what a `geography` column did on its first live read.


@pytest.mark.parametrize("format_code", [0, 1])
def test_the_pure_decoder_hands_a_geography_back_unread(format_code: int) -> None:
    from wreath._pgdriver import _decode_value

    _register_pure_geography()
    payload = bytes.fromhex(GEOGRAPHY_CASES[1])
    assert _decode_value(GEOGRAPHY_OID, format_code, payload) == payload


def test_the_native_decoder_has_a_geography_arm() -> None:
    from pathlib import Path

    codec = Path(__file__).resolve().parents[2] / "src/wreath/_native/postgres/codec.c"
    source = codec.read_text(encoding="utf-8")
    decode = source.split("wreath_pg_decode_extension", 1)[-1]
    assert "WREATH_PG_EXT_GEOGRAPHY" in decode, (
        "wreath_pg_decode_extension has no geography arm; every registered "
        "extension OID is routed to it, so the read raises rather than falling "
        "back to raw bytes"
    )
