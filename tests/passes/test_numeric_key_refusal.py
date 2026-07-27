"""`numeric` stays refused as a key type -- for a different reason than before.

The original refusal said there was "no decimal codec to read it back with, so
the value returns as a float". That expired when the numeric codec landed. The
ordering property was then swept over the real ledger path (`Decimal` -> `str`
-> jsonb -> `Decimal`): **403 values, 81,001 ordered pairs, zero value failures
and zero ordering failures**, against 229 value failures and 5 collapsed pairs
for the float path it would replace. By that measure it could be lifted.

It is not lifted, because measuring it surfaced a different blocker:
`_passes.progress.position` places a key value on a line for
`progress=Keyspace()` and handles `int` and `float` but **not** `Decimal`, while
`_EXAMPLE` already maps `numeric` to `0.0`. So a numeric key would pass
`Keyspace.refuse` at declaration and then silently measure nothing at runtime --
a check with nothing to check, which is the failure mode this codebase keeps
finding.

These tests pin both halves: that the refusal holds, and that the blocker is
real, so a future lift cannot happen without tripping over it.
"""

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
    """The stated reason expired; a refusal resting on a false premise is a trap.

    A source-shape guard, following the `"ANY(" not in query` and
    `"_source" not in co_consts` guards elsewhere in this tree: the justification
    lives in a `#:` comment, which no runtime object carries, so the file is the
    only place to assert it.
    """
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
    """The actual blocker to lifting the refusal.

    If this ever starts returning a number, `Keyspace()` over a numeric key
    becomes measurable and the refusal can be revisited -- but the non-finites
    have to be handled first (see below).
    """
    key = Key("amount", "numeric", indexed=True, unique=True)
    assert position(key, decimal.Decimal("1.5")) is None


def test_keyspace_would_accept_a_numeric_key_at_declaration() -> None:
    """Which is why lifting naively would produce a silently empty percentage.

    `_EXAMPLE` maps `numeric` to a float, so the declaration-time probe passes
    while the runtime value -- a `Decimal` -- does not.
    """
    assert isinstance(_EXAMPLE["numeric"], float)
    keys = (Key("amount", "numeric", indexed=True, unique=True),)
    Keyspace().refuse(keys, table="ledger")  # accepts, and that is the trap


def test_postgres_orders_nan_but_python_raises_on_it() -> None:
    """The non-finite half a future lift has to design for.

    PostgreSQL sorts `NaN` above every other numeric. `Decimal("NaN") > x`
    raises `InvalidOperation` rather than returning `False`, so
    `float(Decimal("NaN"))` would hand the percentage arithmetic a `nan` instead
    of an error. The walk itself is unaffected -- it never orders in Python.
    """
    with pytest.raises(decimal.InvalidOperation):
        _ = decimal.Decimal("NaN") > decimal.Decimal("1")


def test_the_ordering_property_that_would_permit_a_lift() -> None:
    """The sweep's result, kept small enough for the default run.

    The full sweep (403 values, 81,001 pairs) lives in the commit message and the
    constant's comment; this is the shape of it, so a regression in the ledger
    codec path shows up here rather than only in a future lift.
    """
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
