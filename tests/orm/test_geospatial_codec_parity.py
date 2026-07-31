"""The `point` binary parameter codec, in both languages, held byte-for-byte.

Lane G established that the *expression compiler* needs no C for a new operator
token. The *wire codec* is a separate axis and does: the prepared path binds
parameters in binary, and both the pure and the native encoder enumerate OIDs
with no shared fallback. So `point` has two encoders, and this is what keeps
them from drifting.
"""

from __future__ import annotations

import struct

import pytest

from wreath._pure.postgres import _encode_point as _pure_encode_point

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
    """Through `_encode_binary`, not just the helper.

    The helper is reachable from a test; the *dispatch arm* that selects it is
    only reached when a point is bound on the prepared path, which under the
    native driver never enters this module at all. Calling the dispatcher
    directly is what covers the pure twin without `WREATH_PURE=1`.
    """
    from wreath._pure.postgres import _encode_binary

    assert _encode_binary(literal, 600) == _pure_encode_point(literal)


def test_the_binary_dispatch_still_refuses_an_oid_it_has_no_encoder_for() -> None:
    from wreath._pure.postgres import _encode_binary

    with pytest.raises(TypeError, match="no binary encoder"):
        _encode_binary("whatever", 1_000_000)


@pytest.mark.parametrize(
    "bad", ["", "1,2", "(1)", "(a,b)", "(1,2", "1,2)", "(1,2,3)", "()"]
)
def test_a_malformed_literal_is_refused_rather_than_guessed(bad: str) -> None:
    with pytest.raises(TypeError, match="point codec"):
        _pure_encode_point(bad)


def test_a_non_string_is_refused(bad: object = 1.5) -> None:
    with pytest.raises(TypeError, match="point codec"):
        _pure_encode_point(bad)


def test_the_native_encoder_is_covered_by_the_live_round_trip() -> None:
    """Where the native twin is actually proved, and why not here.

    `encode_point` in `_native/postgres/codec.c` is not exposed to Python, so
    there is nothing to call from this file. Skipping would be the wrong answer
    -- that would be a gap in Wreath dressed as a gap in the environment, which
    is the shape `AGENTS.md` rules out. The native encoder is instead driven for
    real by `tests/orm/test_geospatial_live.py`, whose inserts go through the
    prepared, binary-parameter path that selects it; a divergence from the pure
    encoder puts the wrong coordinate in the table and
    `test_a_coordinate_round_trips_through_a_bind` fails.

    This test exists to make that pointer discoverable from the parity file
    someone will look in first.
    """
    from pathlib import Path

    codec = Path(__file__).resolve().parents[2] / "src/wreath/_native/postgres/codec.c"
    source = codec.read_text(encoding="utf-8")
    # If the native encoder is ever removed, the live suite would silently fall
    # back to nothing and this pointer would be a lie.
    assert "encode_point" in source
    assert "PG_POINT" in source
