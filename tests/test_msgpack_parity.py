"""The native MessagePack encoder is byte-for-byte the pure encoder.

`src/wreath/_pure/msgpack.py` stays the reference implementation and the parity
contract; `src/wreath/_native/msgpack.c` is a faster twin of it. Every case here
asserts the two produce identical bytes, so a divergence fails as a parity bug
rather than as a mysterious wire-format change at a client.

The boundary cases matter more than the shapes: MessagePack picks its tag from
the *value*, so every width transition (fixint/uint8/uint16/uint32/uint64 and
the negative mirror, fixstr/str8/str16, fixarray/array16, fixmap/map16) is a
branch that can be off by one in exactly one implementation.
"""

from __future__ import annotations

import math

import pytest

from wreath._pure.msgpack import packb as pure_packb

native_packb = pytest.importorskip("wreath._native._core").msgpack_dumps


def _same(value: object) -> bytes:
    """Assert both encoders agree, and return the bytes for spec assertions."""
    expected = pure_packb(value)
    assert native_packb(value) == expected, f"native diverged from pure for {value!r}"
    return expected


# -- scalars ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, True, False])
def test_singletons(value: object) -> None:
    _same(value)


@pytest.mark.parametrize(
    "value",
    [
        0, 1, 0x7F,                      # fixint upper edge
        0x80, 0xFF,                      # uint8
        0x100, 0xFFFF,                   # uint16
        0x10000, 0xFFFFFFFF,             # uint32
        0x100000000, 0xFFFFFFFFFFFFFFFF,  # uint64, incl. the very top
        -1, -0x20,                       # negative fixint edge
        -0x21, -0x80,                    # int8
        -0x81, -0x8000,                  # int16
        -0x8001, -0x80000000,            # int32
        -0x80000001, -0x8000000000000000,  # int64, incl. the very bottom
    ],
)
def test_integer_width_transitions(value: int) -> None:
    _same(value)


@pytest.mark.parametrize(
    "value", [0x10000000000000000, -0x8000000000000001]
)
def test_integers_out_of_range_are_refused_by_both(value: int) -> None:
    with pytest.raises(ValueError):
        pure_packb(value)
    with pytest.raises(ValueError):
        native_packb(value)


@pytest.mark.parametrize(
    "value",
    [0.0, -0.0, 1.0, -1.5, 3.141592653589793, 1e308, 1e-308, math.inf, -math.inf],
)
def test_floats_are_always_float64(value: float) -> None:
    encoded = _same(value)
    assert encoded[0] == 0xCB
    assert len(encoded) == 9


def test_nan_encodes_identically() -> None:
    # Compared as bytes, not as a value: NaN != NaN, but its encoding is fixed.
    assert native_packb(math.nan) == pure_packb(math.nan)


def test_bool_is_not_encoded_as_int() -> None:
    """bool is an int subclass; both encoders must test it first."""
    assert _same(True) == b"\xc3"
    assert _same(False) == b"\xc2"
    assert _same(1) == b"\x01"
    assert _same(0) == b"\x00"


# -- strings and binary ------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 31, 32, 255, 256, 65535, 65536])
def test_string_width_transitions(length: int) -> None:
    _same("x" * length)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ascii",
        "héllo",            # 2-byte UTF-8
        "日本語",             # 3-byte UTF-8
        "🎄wreath",          # 4-byte UTF-8, non-BMP
        "\x00\x01\x1f",     # control characters are not escaped in msgpack
        "a" * 31 + "é",     # multibyte pushing a fixstr over its byte edge
    ],
)
def test_unicode_is_encoded_by_utf8_length(value: str) -> None:
    _same(value)


@pytest.mark.parametrize("length", [0, 1, 255, 256, 65535, 65536])
def test_bytes_width_transitions(length: int) -> None:
    _same(b"\xab" * length)


def test_bytearray_and_memoryview_match_bytes() -> None:
    payload = b"\x00\xff" * 40
    expected = _same(payload)
    assert native_packb(bytearray(payload)) == expected
    assert native_packb(memoryview(payload)) == expected
    assert pure_packb(bytearray(payload)) == expected
    assert pure_packb(memoryview(payload)) == expected


# -- containers --------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 15, 16, 65535])
def test_array_width_transitions(length: int) -> None:
    _same([0] * length)


@pytest.mark.parametrize("length", [0, 1, 15, 16, 65535])
def test_map_width_transitions(length: int) -> None:
    _same({str(index): index for index in range(length)})


def test_tuple_encodes_as_array() -> None:
    assert _same((1, 2, 3)) == pure_packb([1, 2, 3])


def test_map_preserves_insertion_order() -> None:
    """Both walk the dict in iteration order, so the bytes are order-sensitive."""
    first = {"a": 1, "b": 2}
    second = {"b": 2, "a": 1}
    assert _same(first) != _same(second)


def test_scalar_keys_are_allowed_by_both() -> None:
    """Non-str keys are fine as long as they are scalars the format can carry."""
    _same({1: "a", 2: "b"})
    _same({b"bin": "a", 1.5: "b", None: "c", True: "d"})


def test_container_keys_are_refused_by_both_in_the_same_words() -> None:
    """Replaces an earlier test that blessed a tuple key as an array key.

    That was deliberate once -- `_native/msgpack.c` documented non-str keys as
    intentional -- but a round-trip sweep showed an array key is unreconstructable
    by any decoder targeting a mapping, while `json.dumps` refuses the same value.
    The two serializers now agree, so the failure a handler sees does not depend
    on which content type was negotiated.

    The messages must match, not merely both raise: parity here is what stops one
    twin drifting into accepting a key the other rejects.
    """
    with pytest.raises(TypeError) as pure_error:
        pure_packb({(1, 2): "x"})
    with pytest.raises(TypeError) as native_error:
        native_packb({(1, 2): "x"})
    assert str(pure_error.value) == str(native_error.value)
    assert "not tuple" in str(pure_error.value)


def test_nested_document() -> None:
    _same(
        {
            "rows": [
                {"id": index, "name": f"n{index}", "tags": ["x", "y"], "ok": index % 2 == 0}
                for index in range(50)
            ],
            "total": 50,
            "cursor": None,
            "ratio": 0.5,
        }
    )


# -- refusals ----------------------------------------------------------------


@pytest.mark.parametrize("value", [object(), {1, 2}, 1 + 2j])
def test_unsupported_types_are_refused_by_both(value: object) -> None:
    with pytest.raises(TypeError):
        pure_packb(value)
    with pytest.raises(TypeError):
        native_packb(value)


def test_deep_nesting_is_refused_rather_than_crashing() -> None:
    """Both encoders recurse; each must bound that itself.

    The pure side was previously unpinned here -- it behaved the same way, but
    nothing said so, which is how a reference implementation drifts from the twin
    that is tested against it.
    """
    document: object = 0
    for _ in range(2000):
        document = [document]
    with pytest.raises((ValueError, RecursionError)):
        native_packb(document)
    with pytest.raises((ValueError, RecursionError)):
        pure_packb(document)


def test_self_referential_container_is_refused() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises((ValueError, RecursionError)):
        native_packb(cycle)
    with pytest.raises((ValueError, RecursionError)):
        pure_packb(cycle)
