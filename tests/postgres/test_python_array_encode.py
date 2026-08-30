from __future__ import annotations

import datetime
import importlib
import os
import struct
import uuid
from decimal import Decimal
from typing import Any

import pytest

from wreath import _pgdriver as pure
from wreath.orm.types import WireList

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass


ARRAY_CASES = (
    (1000, [True, None, False]),
    (1001, [b'\x00"\\', None]),
    (1016, [-(2**40), None, 2**40]),
    (1005, [-(2**15), None, 2**15 - 1]),
    (1007, [-(2**31), None, 2**31 - 1]),
    (1009, ["comma,value", 'quote"slash\\', "NULL", "", None, "héllo"]),
    (199, ['{"key":"x,y"}', None]),
    (1021, [1.5, None, -2.25]),
    (1022, [1.5, None, -2.25]),
    (1015, ["value", None, ""]),
    (1182, [datetime.date(2024, 2, 29), None]),
    (1115, [datetime.datetime(2024, 2, 29, 12, 34, 56, 789), None]),
    (
        1185,
        [datetime.datetime(2024, 2, 29, 12, 34, tzinfo=datetime.UTC), None],
    ),
    (1231, [Decimal("1.2300"), None, Decimal("-4.5")]),
    (2951, [uuid.UUID("12345678-1234-5678-1234-567812345678"), None]),
    (3807, ['{"key":"x,y"}', None]),
)


@pytest.mark.parametrize(("oid", "value"), ARRAY_CASES)
@pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built")
def test_python_array_encoders_match_native(oid: int, value: list[object]) -> None:
    assert pure._encode_text(value, oid) == native._encode_text(value, oid)
    assert pure._encode_binary(value, oid) == native._encode_binary(value, oid)


def test_python_text_array_quotes_values_and_preserves_nulls() -> None:
    value = ["comma,value", 'quote"slash\\', "NULL", "", None]
    assert pure._encode_text(value, 1009) == (
        b'{"comma,value","quote\\"slash\\\\","NULL","",NULL}'
    )


def test_python_binary_array_frames_null_and_element_oid() -> None:
    assert pure._encode_binary([1, None, -2], 1007) == (
        struct.pack("!III", 1, 1, 23)
        + struct.pack("!II", 3, 1)
        + struct.pack("!Ii", 4, 1)
        + struct.pack("!i", -1)
        + struct.pack("!Ii", 4, -2)
    )


@pytest.mark.parametrize("value", ([], ()))
def test_python_empty_array_has_zero_dimensions(value: object) -> None:
    assert pure._encode_text(value, 1009) == b"{}"
    assert pure._encode_binary(value, 1009) == struct.pack("!III", 0, 0, 25)


@pytest.mark.parametrize("encoder", (pure._encode_text, pure._encode_binary))
@pytest.mark.parametrize("value", ("not-an-array", {"not": "ordered"}))
def test_python_array_encoder_requires_a_list_or_tuple(encoder: Any, value: object) -> None:
    with pytest.raises(TypeError, match="array codec requires a list or tuple"):
        encoder(value, 1009)


@pytest.mark.database
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN for pure-driver array codec tests",
)
async def test_pure_connection_uses_text_then_binary_array_binds() -> None:
    connection = await pure.connect(os.environ["WREATH_TEST_POSTGRES_DSN"])
    sql = "SELECT value FROM unnest($1::text[]) AS value"
    try:
        first = WireList(["comma,value", 'quote"slash\\', "NULL", "", None], 1009)
        second = WireList(["cached", "bind"], 1009)
        assert [row[0] for row in await connection.fetch(sql, first)] == list(first)
        assert connection.prepared_plan_count == 1
        assert [row[0] for row in await connection.fetch(sql, second)] == list(second)
        assert connection.prepared_plan_count == 1
    finally:
        await connection.close()
