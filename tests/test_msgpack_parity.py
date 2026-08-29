from __future__ import annotations

import math

import pytest

native_packb = pytest.importorskip("wreath._native._core").msgpack_dumps


def _both(value: object, expected: bytes) -> bytes:
    """Assert the encoder emits the spec's bytes for `value`."""
    assert native_packb(value) == expected, f"diverged from the spec for {value!r}"
    return expected


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "c0"),  # nil format
        (True, "c3"),  # bool format family
        (False, "c2"),
    ],
)
def test_singletons(value: object, expected: str) -> None:
    _both(value, _hex(expected))


@pytest.mark.parametrize(
    "value, expected",
    [
        # "int format family". Unsigned side, smallest-encoding rule applied at
        # each ceiling: 0x7f is the last positive fixint, 0xff the last uint 8,
        # 0xffff the last uint 16, 0xffffffff the last uint 32.
        (0, "00"),
        (1, "01"),
        (0x7F, "7f"),  # fixint upper edge
        (0x80, "cc80"),  # uint 8
        (0xFF, "ccff"),
        (0x100, "cd0100"),  # uint 16
        (0xFFFF, "cdffff"),
        (0x10000, "ce00010000"),  # uint 32
        (0xFFFFFFFF, "ceffffffff"),
        (0x100000000, "cf0000000100000000"),  # uint 64
        (0xFFFFFFFFFFFFFFFF, "cfffffffffffffffff"),  # ... incl. the very top
        # Signed side. negative fixint stores a 5-bit negative integer, so it
        # reaches -32 and no further; below that the int N formats carry the
        # two's-complement value, big-endian.
        (-1, "ff"),
        (-0x20, "e0"),  # negative fixint edge
        (-0x21, "d0df"),  # int 8; -33 == 0xdf
        (-0x80, "d080"),
        (-0x81, "d1ff7f"),  # int 16; -129 == 0xff7f
        (-0x8000, "d18000"),
        (-0x8001, "d2ffff7fff"),  # int 32
        (-0x80000000, "d280000000"),
        (-0x80000001, "d3ffffffff7fffffff"),  # int 64
        (-0x8000000000000000, "d38000000000000000"),  # ... incl. the very bottom
    ],
)
def test_integer_width_transitions(value: int, expected: str) -> None:
    _both(value, _hex(expected))


@pytest.mark.parametrize("value", [0x10000000000000000, -0x8000000000000001])
def test_integers_out_of_range_are_refused(value: int) -> None:
    with pytest.raises(ValueError):
        native_packb(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        # "float format family": float 64 is 0xcb followed by the IEEE 754
        # binary64 encoding, big-endian. Each pattern below was derived from
        # the value by exact rational arithmetic (sign, unbiased exponent,
        # 52-bit fraction) and not read off an encoder.
        (0.0, "cb0000000000000000"),
        (-0.0, "cb8000000000000000"),  # sign bit alone
        (1.0, "cb3ff0000000000000"),  # exponent 1023, fraction 0
        (-1.5, "cbbff8000000000000"),
        (3.141592653589793, "cb400921fb54442d18"),
        (1e308, "cb7fe1ccf385ebc8a0"),  # exponent 2046, the last normal decade
        (1e-308, "cb000730d67819e8d2"),  # subnormal: exponent field is 0
        (math.inf, "cb7ff0000000000000"),  # exponent all ones, fraction 0
        (-math.inf, "cbfff0000000000000"),
    ],
)
def test_floats_are_always_float64(value: float, expected: str) -> None:
    encoded = _both(value, _hex(expected))
    assert encoded[0] == 0xCB
    assert len(encoded) == 9


def test_nan_is_a_float64_with_the_ieee_754_nan_bit_pattern() -> None:
    encoded = native_packb(math.nan)
    assert encoded[0] == 0xCB
    assert len(encoded) == 9
    bits = int.from_bytes(encoded[1:], "big")
    assert (bits >> 52) & 0x7FF == 0x7FF, "exponent field is not all ones"
    assert bits & ((1 << 52) - 1) != 0, "fraction is zero, so this is an infinity"


def test_bool_is_not_encoded_as_int() -> None:
    _both(True, b"\xc3")
    _both(False, b"\xc2")
    _both(1, b"\x01")
    _both(0, b"\x00")


@pytest.mark.parametrize(
    "length, header",
    [
        # "str format family": fixstr is 0xa0 | length for lengths 0-31, then
        # str 8 (0xd9) to 255, str 16 (0xda) to 65535, str 32 (0xdb) beyond.
        (0, "a0"),
        (1, "a1"),
        (31, "bf"),  # fixstr's last length
        (32, "d920"),  # ... and the first that does not fit it
        (255, "d9ff"),  # str 8's last length
        (256, "da0100"),
        (65535, "daffff"),  # str 16's last length
        (65536, "db00010000"),
    ],
)
def test_string_width_transitions(length: int, header: str) -> None:
    _both("x" * length, _hex(header) + b"x" * length)


@pytest.mark.parametrize(
    "value, expected",
    [
        # The length prefix counts UTF-8 *bytes*, not code points, so each of
        # these pins the encoder's idea of "length" as well as its tag.
        ("", "a0"),
        ("ascii", "a56173636969"),
        ("héllo", "a668c3a96c6c6f"),  # é U+00E9 -> c3 a9; 6 bytes
        ("日本語", "a9e697a5e69cace8aa9e"),  # 3 x 3-byte; 9 bytes
        ("🎄wreath", "aaf09f8e84777265617468"),  # U+1F384 -> f0 9f 8e 84; 10 bytes
        ("\x00\x01\x1f", "a300011f"),  # controls are not escaped
        (
            "a" * 31 + "é",
            # 31 ASCII + a 2-byte é is 33 bytes, so a fixstr cannot hold it
            # even though the string is 32 characters: str 8, length 0x21.
            "d921" + "61" * 31 + "c3a9",
        ),
    ],
)
def test_unicode_is_encoded_by_utf8_length(value: str, expected: str) -> None:
    _both(value, _hex(expected))


@pytest.mark.parametrize(
    "length, header",
    [
        # "bin format family": bin 8 (0xc4) to 255, bin 16 (0xc5) to 65535,
        # bin 32 (0xc6) beyond. There is no "fixbin", so even b"" costs a
        # length byte.
        (0, "c400"),
        (1, "c401"),
        (255, "c4ff"),
        (256, "c50100"),
        (65535, "c5ffff"),
        (65536, "c600010000"),
    ],
)
def test_bytes_width_transitions(length: int, header: str) -> None:
    _both(b"\xab" * length, _hex(header) + b"\xab" * length)


def test_bytearray_and_memoryview_match_bytes() -> None:
    payload = b"\x00\xff" * 40
    expected = _hex("c450") + payload  # bin 8, length 0x50 == 80
    for value in (payload, bytearray(payload), memoryview(payload)):
        _both(value, expected)


@pytest.mark.parametrize(
    "length, header",
    [
        # "array format family": fixarray is 0x90 | count for 0-15, then
        # array 16 (0xdc) with a big-endian uint16 count.
        (0, "90"),
        (1, "91"),
        (15, "9f"),
        (16, "dc0010"),
        (65535, "dcffff"),
    ],
)
def test_array_width_transitions(length: int, header: str) -> None:
    # Every element is 0, which is a positive fixint and so exactly one 0x00
    # byte -- the payload is spellable in full, and only the header varies.
    _both([0] * length, _hex(header) + b"\x00" * length)


#: The pairs of `{str(i): i for i in range(n)}`, spelled per the spec: each key
#: is a fixstr (0xa0 | byte length) over ASCII digits -- "0" is 0x30 -- and each
#: value here is small enough to be a positive fixint, which is its own byte.
MAP_PAIRS = (
    "a13000",
    "a13101",
    "a13202",
    "a13303",
    "a13404",
    "a13505",
    "a13606",
    "a13707",
    "a13808",
    "a13909",
    "a231300a",
    "a231310b",
    "a231320c",
    "a231330d",
    "a231340e",
    "a231350f",
)


@pytest.mark.parametrize(
    "length, header",
    [
        # "map format family": fixmap is 0x80 | count for 0-15, then map 16
        # (0xde) with a big-endian uint16 count. The count is *pairs*, not
        # objects, which is the mistake this pins.
        (0, "80"),
        (1, "81"),
        (15, "8f"),
        (16, "de0010"),
    ],
)
def test_map_width_transitions(length: int, header: str) -> None:
    expected = _hex(header + "".join(MAP_PAIRS[:length]))
    _both({str(index): index for index in range(length)}, expected)


def test_map_at_the_map16_ceiling() -> None:
    document = {str(index): index for index in range(65535)}
    keys = 10 * 2 + 90 * 3 + 900 * 4 + 9000 * 5 + 55535 * 6  # 1..5 digits
    values = 128 * 1 + 128 * 2 + 65279 * 3  # fixint, uint 8, uint 16
    encoded = native_packb(document)
    assert encoded[:3] == _hex("deffff"), "map 16 with a 65535 pair count"
    assert encoded[3:6] == _hex("a13000"), '"0" -> a1 30, then 0 -> 00'
    assert encoded[-9:] == _hex("a53635353334cdfffe"), (
        '"65534" -> a5 36 35 35 33 34, then 65534 -> cd ff fe'
    )
    assert len(encoded) == 3 + keys + values


def test_tuple_encodes_as_array() -> None:
    _both((1, 2, 3), _hex("93010203"))
    _both([1, 2, 3], _hex("93010203"))


def test_map_preserves_insertion_order() -> None:
    _both({"a": 1, "b": 2}, _hex("82a16101a16202"))
    _both({"b": 2, "a": 1}, _hex("82a16202a16101"))


def test_scalar_keys_are_allowed_by_both() -> None:
    _both({1: "a", 2: "b"}, _hex("8201a16102a162"))
    _both(
        {b"bin": "a", 1.5: "b", None: "c", True: "d"},
        _hex(
            "84"  # fixmap, 4 pairs
            "c403"
            "62696e"
            "a161"  # bin 8 b"bin" -> "a"
            "cb3ff8000000000000"
            "a162"  # float 64 1.5 -> "b"
            "c0"
            "a163"  # nil -> "c"
            "c3"
            "a164"  # true -> "d"
        ),
    )


def test_container_keys_are_refused_in_so_many_words() -> None:
    with pytest.raises(TypeError) as error:
        native_packb({(1, 2): "x"})
    assert str(error.value) == ("keys must be str, int, float, bool, bytes or None, not tuple")


def test_nested_document() -> None:
    document = {
        "rows": [
            {"id": index, "name": f"n{index}", "tags": ["x", "y"], "ok": index % 2 == 0}
            for index in range(50)
        ],
        "total": 50,
        "cursor": None,
        "ratio": 0.5,
    }
    head = _hex(
        "84"  # fixmap, 4 pairs
        "a4"
        "726f7773"  # "rows"
        "dc0032"  # array 16, 50 elements -- past fixarray's 15
    )
    row_0 = _hex(
        "84"  # fixmap, 4 pairs
        "a2"
        "6964"
        "00"  # "id" -> 0
        "a4"
        "6e616d65"
        "a2"
        "6e30"  # "name" -> "n0"
        "a4"
        "74616773"
        "92"
        "a178"
        "a179"  # "tags" -> ["x", "y"]
        "a2"
        "6f6b"
        "c3"  # "ok" -> true
    )
    row_49 = _hex(
        "84"
        "a2"
        "6964"
        "31"  # 49 == 0x31, still a positive fixint
        "a4"
        "6e616d65"
        "a3"
        "6e3439"  # "n49" is 3 bytes, so a3 not a2
        "a4"
        "74616773"
        "92"
        "a178"
        "a179"
        "a2"
        "6f6b"
        "c2"  # 49 is odd -> false
    )
    tail = _hex(
        "a5"
        "746f74616c"
        "32"  # "total" -> 50
        "a6"
        "637572736f72"
        "c0"  # "cursor" -> nil
        "a5"
        "726174696f"
        "cb3fe0000000000000"  # "ratio" -> float 64 0.5
    )
    encoded = native_packb(document)
    assert encoded.startswith(head + row_0)
    assert encoded.endswith(row_49 + tail)
    # Rows 0-9 name themselves in 2 bytes and rows 10-49 in 3, so a row is
    # 27 or 28 bytes: 1 + 3 + 1 + 5 + (3 or 4) + 5 + 5 + 3 + 1.
    assert len(encoded) == len(head) + 10 * 27 + 40 * 28 + len(tail)


@pytest.mark.parametrize("value", [object(), {1, 2}, 1 + 2j])
def test_unsupported_types_are_refused(value: object) -> None:
    with pytest.raises(TypeError):
        native_packb(value)


def test_deep_nesting_is_refused_rather_than_crashing() -> None:
    document: object = 0
    for _ in range(2000):
        document = [document]
    with pytest.raises((ValueError, RecursionError)):
        native_packb(document)


def test_self_referential_container_is_refused() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises((ValueError, RecursionError)):
        native_packb(cycle)
