from __future__ import annotations

import importlib
from typing import Any

import pytest

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    native is None,
    reason="native _postgres codec required (pure array twin deferred)",
)

# (array_oid, python value) -- element oids: int4=23, int8=20, text=25, bool=16.
CASES = [
    (1007, [1, 2, 3]),  # int4[]
    (1007, []),  # empty int4[] (ndims 0)
    (1007, [1, None, 3]),  # int4[] carrying a NULL element (has_null)
    (1016, [10, 20, 30]),  # int8[]
    (1009, ["a", "", "z"]),  # text[]
    (1009, ["x", None]),  # text[] with NULL
    (1000, [True, False]),  # bool[]
]


@pytest.mark.parametrize(("oid", "value"), CASES, ids=lambda p: repr(p))
def test_array_binary_round_trip(oid: int, value: list[Any]) -> None:
    wire = native._encode_binary(value, oid)
    assert wire is not None
    decoded = native._decode_value(oid, 1, wire)
    assert decoded == value
