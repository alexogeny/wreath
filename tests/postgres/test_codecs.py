from __future__ import annotations

import datetime
import importlib
import math
import struct
import uuid
from typing import Any

import pytest

from wreath import _pgdriver as pure

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

BACKENDS = [pytest.param(pure, id="pure")]
if native is not None:
    BACKENDS.append(pytest.param(native, id="native"))

# Malformed wire data surfaces as ProtocolError from the reference backend and
# as ValueError from the native raw decoders, for every codec since the first
# one. Tests accept either rather than assert a distinction neither backend makes.
MALFORMED = (ValueError, pure.ProtocolError)

CASES = (
    (16, True, b"t", b"\x01"),
    (16, False, b"f", b"\x00"),
    (21, -123, b"-123", struct.pack("!h", -123)),
    (23, 123456, b"123456", struct.pack("!i", 123456)),
    (20, -(2**40), str(-(2**40)).encode(), struct.pack("!q", -(2**40))),
    (700, 1.5, b"1.5", struct.pack("!f", 1.5)),
    (701, -2.25, b"-2.25", struct.pack("!d", -2.25)),
    (25, "héllo", "héllo".encode(), "héllo".encode()),
    (1043, "value", b"value", b"value"),
    (17, b"\x00\xff", b"\\x00ff", b"\x00\xff"),
    (
        2950,
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        b"12345678-1234-5678-1234-567812345678",
        bytes.fromhex("12345678123456781234567812345678"),
    ),
    # Binary date/timestamp values count from 2000-01-01: 8962 days to
    # 2024-07-15, plus 49_530_123_456us for 13:45:30.123456 within that day.
    (1082, datetime.date(2000, 1, 1), b"2000-01-01", struct.pack("!i", 0)),
    (1082, datetime.date(2024, 2, 29), b"2024-02-29", struct.pack("!i", 8825)),
    (1082, datetime.date(1999, 12, 31), b"1999-12-31", struct.pack("!i", -1)),
    (
        1114,
        datetime.datetime(2024, 7, 15, 13, 45, 30, 123456),
        b"2024-07-15 13:45:30.123456",
        struct.pack("!q", 8962 * 86_400_000_000 + 49_530_123_456),
    ),
    (
        1184,
        datetime.datetime(2024, 7, 15, 13, 45, 30, 123456, tzinfo=datetime.UTC),
        b"2024-07-15 13:45:30.123456+00:00",
        struct.pack("!q", 8962 * 86_400_000_000 + 49_530_123_456),
    ),
    (114, '{"a": 1}', b'{"a": 1}', b'{"a": 1}'),
    (3802, '{"a": 1}', b'{"a": 1}', b'\x01{"a": 1}'),
)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(("oid", "value", "text", "binary"), CASES)
def test_initial_codecs_round_trip(
    backend: Any, oid: int, value: object, text: bytes, binary: bytes
) -> None:
    assert backend._encode_text(value, oid) == text
    assert backend._encode_binary(value, oid) == binary
    decoded_text = backend._decode_value(oid, 0, text)
    decoded_binary = backend._decode_value(oid, 1, binary)
    if isinstance(value, float):
        assert math.isclose(decoded_text, value, rel_tol=1e-6)
        assert math.isclose(decoded_binary, value, rel_tol=1e-6)
    else:
        assert decoded_text == value
        assert decoded_binary == value


@pytest.mark.parametrize("backend", BACKENDS)
def test_null_is_encoded_without_payload_and_decodes_to_none(backend: Any) -> None:
    assert backend._encode_text(None, 23) is None
    assert backend._encode_binary(None, 23) is None
    assert backend._decode_value(23, 0, None) is None
    assert backend._decode_value(23, 1, None) is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_integer_codecs_reject_overflow(backend: Any) -> None:
    with pytest.raises(OverflowError):
        backend._encode_binary(2**15, 21)
    with pytest.raises(OverflowError):
        backend._encode_binary(2**31, 23)
    with pytest.raises(OverflowError):
        backend._encode_binary(2**63, 20)


@pytest.mark.parametrize("backend", BACKENDS)
def test_binary_codec_rejects_wrong_value_type(backend: Any) -> None:
    message = "int4 codec requires int" if backend is pure else "integer codec requires int"
    with pytest.raises(TypeError, match=message):
        backend._encode_binary("1", 23)
    with pytest.raises(TypeError):
        backend._encode_binary(1, 2950)


@pytest.mark.parametrize("backend", BACKENDS)
def test_unknown_oid_is_returned_as_bytes(backend: Any) -> None:
    assert backend._decode_value(999_999, 0, b"opaque") == b"opaque"
    assert backend._decode_value(999_999, 1, b"opaque") == b"opaque"


@pytest.mark.parametrize("backend", BACKENDS)
def test_timestamp_codecs_separate_naive_and_aware_values(backend: Any) -> None:
    # timestamp and timestamptz disagree about tzinfo, so a value that fits one
    # must never be silently accepted by the other.
    naive = datetime.datetime(2024, 1, 1, 12)
    aware = datetime.datetime(2024, 1, 1, 12, tzinfo=datetime.UTC)
    with pytest.raises(TypeError):
        backend._encode_binary(naive, 1184)
    with pytest.raises(TypeError):
        backend._encode_binary(aware, 1114)
    with pytest.raises(TypeError):
        backend._encode_text(naive, 1184)
    with pytest.raises(TypeError):
        backend._encode_text(aware, 1114)


@pytest.mark.parametrize("backend", BACKENDS)
def test_date_codec_rejects_datetime_values(backend: Any) -> None:
    with pytest.raises(TypeError):
        backend._encode_binary(datetime.datetime(2024, 1, 1), 1082)


@pytest.mark.parametrize("backend", BACKENDS)
def test_aware_timestamps_are_normalized_to_utc(backend: Any) -> None:
    offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    moment = datetime.datetime(2024, 7, 15, 13, 45, 30, tzinfo=offset)
    decoded = backend._decode_value(1184, 1, backend._encode_binary(moment, 1184))
    assert decoded == moment
    assert decoded.tzinfo is datetime.UTC


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("oid", "data"),
    (
        (1082, struct.pack("!i", 2**31 - 1)),
        (1082, struct.pack("!i", -(2**31))),
        (1114, struct.pack("!q", 2**63 - 1)),
        (1114, struct.pack("!q", -(2**63))),
    ),
)
def test_temporal_infinity_is_rejected_rather_than_wrapped(
    backend: Any, oid: int, data: bytes
) -> None:
    # PostgreSQL reserves the integer extremes for infinity, which datetime
    # cannot represent; decoding must fail instead of inventing a date.
    with pytest.raises(MALFORMED):
        backend._decode_value(oid, 1, data)


@pytest.mark.parametrize("backend", BACKENDS)
def test_jsonb_rejects_unknown_wire_version(backend: Any) -> None:
    with pytest.raises(MALFORMED):
        backend._decode_value(3802, 1, b"\x02{}")


# Text-format bytea is decoded in C through a nibble table, with no per-field
# `binascii` import or method dispatch. Behavior must stay byte-for-byte
# identical to `_pgdriver`.

HEX_BYTEA_CASES = [
    pytest.param(b"", b"", id="empty"),
    pytest.param(b"00", b"\x00", id="zero-byte"),
    pytest.param(b"deadbeef", b"\xde\xad\xbe\xef", id="lowercase"),
    pytest.param(b"DEADBEEF", b"\xde\xad\xbe\xef", id="uppercase"),
    pytest.param(b"DeAdBeEf", b"\xde\xad\xbe\xef", id="mixed-case"),
    pytest.param(b"000102030405060708090a0b0c0d0e0f", bytes(range(16)), id="all-nibbles"),
    pytest.param(b"ff" * 4096, b"\xff" * 4096, id="large"),
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(("hex_text", "expected"), HEX_BYTEA_CASES)
def test_hex_bytea_scalar_decode(backend: Any, hex_text: bytes, expected: bytes) -> None:
    assert backend._decode_value(17, 0, b"\\x" + hex_text) == expected


@pytest.mark.parametrize("backend", BACKENDS)
def test_hex_bytea_binary_format_is_raw(backend: Any) -> None:
    assert backend._decode_value(17, 1, b"\\xdead") == b"\\xdead"
    assert backend._decode_value(17, 1, b"\x00\x01\xff") == b"\x00\x01\xff"


@pytest.mark.parametrize("backend", BACKENDS)
def test_hex_bytea_odd_length_is_rejected(backend: Any) -> None:
    with pytest.raises(MALFORMED):
        backend._decode_value(17, 0, b"\\xabc")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("bad", [b"zz", b"0z", b"z0", b"ab\x00cd", b"g1", b"..", b"//"])
def test_hex_bytea_non_hex_digit_is_rejected(backend: Any, bad: bytes) -> None:
    with pytest.raises(MALFORMED):
        backend._decode_value(17, 0, b"\\x" + bad)


@pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built")
def test_whitespace_in_hex_bytea_is_a_known_backend_difference() -> None:
    assert pure._decode_value(17, 0, b"\\xab cd") == b"\xab\xcd"
    with pytest.raises(ValueError):
        native._decode_value(17, 0, b"\\xab cd")


@pytest.mark.parametrize("backend", BACKENDS)
def test_non_hex_text_bytea_passes_through(backend: Any) -> None:
    assert backend._decode_value(17, 0, b"plain") == b"plain"
    assert backend._decode_value(17, 0, b"") == b""


@pytest.mark.parametrize("backend", BACKENDS)
def test_hex_bytea_parity_across_random_payloads(backend: Any) -> None:
    import random

    rng = random.Random(99)
    for _ in range(200):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        encoded = b"\\x" + raw.hex().encode()
        assert backend._decode_value(17, 0, encoded) == raw
