from __future__ import annotations

import decimal

import pytest

from wreath._passes.keyset import _INEXACT_TYPES, Key, refuse_unsound_key
from wreath._passes.progress import _EXAMPLE, Keyspace, position


@pytest.mark.parametrize("sql_type", ["numeric", "decimal", "NUMERIC", "Decimal"])
def test_a_numeric_key_is_refused(sql_type: str) -> None:
    assert sql_type.lower() in _INEXACT_TYPES
    keys = (Key("amount", sql_type, indexed=True, unique=True),)
    with pytest.raises(Exception) as caught:
        refuse_unsound_key(keys, table="ledger")
    assert "amount" in str(caught.value)


def test_the_refusal_no_longer_blames_a_missing_codec() -> None:
    import inspect

    from wreath._passes import keyset

    source = inspect.getsource(keyset)
    _, _, after = source.partition("#: SQL type names a cursor")
    justification, _, _ = after.partition("_INEXACT_TYPES =")
    assert justification, "the _INEXACT_TYPES comment moved or was deleted"

    # The premise that expired must not read as current.
    assert "That reason expired" in justification, (
        "the comment still presents the missing-codec argument as live"
    )
    # The reason it is *actually* still refused must be named.
    assert "position" in justification and "Keyspace" in justification


def test_position_still_cannot_place_a_decimal() -> None:
    key = Key("amount", "numeric", indexed=True, unique=True)
    assert position(key, decimal.Decimal("1.5")) is None


def test_keyspace_would_accept_a_numeric_key_at_declaration() -> None:
    assert isinstance(_EXAMPLE["numeric"], float)
    keys = (Key("amount", "numeric", indexed=True, unique=True),)
    Keyspace().refuse(keys, table="ledger")  # accepts, and that is the trap


def test_postgres_orders_nan_but_python_raises_on_it() -> None:
    with pytest.raises(decimal.InvalidOperation):
        _ = decimal.Decimal("NaN") > decimal.Decimal("1")


def test_the_ordering_property_that_would_permit_a_lift() -> None:
    import itertools
    import json

    from wreath._passes.keyset import _encode_one

    values = [
        decimal.Decimal("1.0000000000000000001"),
        decimal.Decimal("1.0000000000000000002"),
        decimal.Decimal("1.1"),
        decimal.Decimal("-1E+100"),
        decimal.Decimal("1E-100"),
        decimal.Decimal("0"),
    ]

    def roundtrip(value: decimal.Decimal) -> decimal.Decimal:
        return decimal.Decimal(json.loads(json.dumps([_encode_one(value)]))[0])

    assert all(roundtrip(v) == v for v in values)
    for a, b in itertools.combinations(values, 2):
        assert (a < b) == (roundtrip(a) < roundtrip(b))
