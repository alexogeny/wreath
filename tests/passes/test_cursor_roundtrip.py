"""The cursor must survive the ledger exactly, in value *and* in order.

A pass resumes by reading its cursor back out of a ``jsonb`` column and asking
for the next rows after it. So the round trip

    values -> encode_cursor -> json dumps -> jsonb -> json loads -> decode_cursor

has to be an exact inverse, and it has to preserve ordering, because the value
is used in a row comparison. A cursor that decodes to a *different* value
resumes in the wrong place; a cursor that decodes to a value that *sorts*
differently does the same thing while looking correct.

These are property sweeps rather than examples on purpose. The equivalent sweep
over ``format_duration``/``parse_duration`` found 19254 failures in 20012
samples, having survived because its one round-trip test used the single shape
that happened to work.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from wreath._json import dumps as json_dumps
from wreath._json import loads as json_loads
from wreath._passes.keyset import (
    Key,
    PassDeclarationError,
    decode_cursor,
    encode_cursor,
    refuse_unsound_key,
)

AKL = ZoneInfo("Pacific/Auckland")
KTM = ZoneInfo("Asia/Kathmandu")  # +5:45
KOL = ZoneInfo("Asia/Kolkata")  # +5:30
CHA = ZoneInfo("Pacific/Chatham")  # +12:45 / +13:45
UTC = dt.UTC


def through_ledger(keys: tuple[Key, ...], values: tuple[object, ...]) -> tuple[object, ...]:
    """The whole path a cursor takes, not just the two functions at its ends."""
    blob = json_dumps(encode_cursor(keys, values))
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    decoded = decode_cursor(keys, json_loads(blob))
    assert decoded is not None
    return decoded


def instant(value: object) -> object:
    """Compare timestamps as instants: PEP 495 ignores ``fold`` across zones."""
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return value


def _timestamps(rng: random.Random, count: int) -> list[dt.datetime]:
    out: list[dt.datetime] = []
    # Both Auckland transitions, including the ambiguous and non-existent hours.
    for base in (dt.datetime(2026, 4, 5, 2, 0), dt.datetime(2026, 9, 27, 2, 0)):
        for fold in (0, 1):
            for minute in (0, 30, 59):
                out.append(base.replace(minute=minute, fold=fold, tzinfo=AKL))
    # Fractional-offset zones.
    out += [dt.datetime(2026, 3, 1, 12, 0, tzinfo=z) for z in (KTM, KOL, CHA, UTC, AKL)]
    # Microsecond precision at every digit count.
    out += [
        dt.datetime(2026, 3, 1, 12, 0, 0, m, tzinfo=UTC)
        for m in (0, 1, 10, 100, 1000, 10000, 100000, 999999)
    ]
    out += [dt.datetime.min.replace(tzinfo=UTC), dt.datetime.max.replace(tzinfo=UTC)]
    out += [dt.datetime.min, dt.datetime.max, dt.datetime(2026, 3, 1, 12, 0)]
    for _ in range(count):
        out.append(
            dt.datetime(
                rng.randint(1, 9999), rng.randint(1, 12), rng.randint(1, 28),
                rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
                rng.randint(0, 999999), tzinfo=UTC,
            )
        )
    return out


def _uuids(rng: random.Random, count: int) -> list[uuid.UUID]:
    out = [uuid.UUID(int=0), uuid.UUID(int=(1 << 128) - 1)]
    out += [uuid.UUID(int=rng.getrandbits(128)) for _ in range(count)]
    # v7-shaped: time-ordered, so its ordering property is the interesting one.
    for i in range(count // 2):
        raw = ((1750000000000 + i * 1000) << 80) | (7 << 76) | rng.getrandbits(74)
        out.append(uuid.UUID(int=raw))
    return out


def _domain(name: str, rng: random.Random, scale: int) -> list[object]:
    if name in ("timestamptz", "timestamp"):
        return list(_timestamps(rng, scale))
    if name == "date":
        return [dt.date.min, dt.date.max, dt.date(2026, 3, 1)] + [
            dt.date(rng.randint(1, 9999), rng.randint(1, 12), rng.randint(1, 28))
            for _ in range(scale)
        ]
    if name == "uuid":
        return list(_uuids(rng, scale))
    if name in ("bigint", "integer"):
        edges = [0, 1, -1, 2**31 - 1, -(2**31), 2**63 - 1, -(2**63), 2**15 - 1, -(2**15)]
        return edges + [rng.randint(-(2**63), 2**63 - 1) for _ in range(scale)]
    if name == "text":
        fixed = ["", "a", "z", "Z", "0", "~", "é", "\U0001f999", '"', "\\", "{}",
                 "[]", ":", ",", "\n", "\t", "a" * 1000, "퟿"]
        return fixed + [
            "".join(chr(rng.randint(1, 0x2FFF)) for _ in range(rng.randint(0, 12)))
            for _ in range(scale)
        ]
    if name == "float8":
        return [0.0, -0.0, 1.0, -1.0, 1e-300, 1e300, 0.1, 1 / 3, float(2**53)] + [
            rng.uniform(-1e9, 1e9) for _ in range(scale)
        ]
    if name == "bytea":
        return [b"", b"\x00", b"\xff", bytes(range(256))] + [
            bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 20))) for _ in range(scale)
        ]
    raise AssertionError(name)


TYPES = ("timestamptz", "timestamp", "date", "uuid", "bigint", "integer", "text",
         "float8", "bytea")


def _sweep(pg_type: str, scale: int, pairs: int) -> None:
    rng = random.Random(20260727)
    key = (Key("k", pg_type, indexed=True, unique=True),)
    values = _domain(pg_type, rng, scale)

    survived = []
    for value in values:
        decoded = through_ledger(key, (value,))[0]
        assert instant(decoded) == instant(value), (
            f"{pg_type}: {value!r} came back as {decoded!r}"
        )
        survived.append((value, decoded))

    checked = 0
    for (x, dx), (y, dy) in itertools.combinations(survived[:pairs], 2):
        try:
            ordered = instant(x) < instant(y)
        except TypeError:  # naive vs aware is not comparable, and never mixed in one key
            continue
        checked += 1
        if ordered:
            assert instant(dx) < instant(dy), f"{pg_type}: {x!r} < {y!r} inverted by the round trip"
        elif instant(y) < instant(x):
            assert instant(dy) < instant(dx), f"{pg_type}: {y!r} < {x!r} inverted by the round trip"
    assert checked > 0


@pytest.mark.parametrize("pg_type", TYPES)
def test_a_cursor_survives_the_ledger_in_value_and_in_order(pg_type: str) -> None:
    """A fast representative sample, so the default suite still runs in seconds."""
    _sweep(pg_type, scale=12, pairs=30)


@pytest.mark.fuzz
@pytest.mark.parametrize("pg_type", TYPES)
def test_the_full_cursor_sweep(pg_type: str) -> None:
    """The thorough version: ~200 values and ~7000 ordered pairs per type."""
    _sweep(pg_type, scale=200, pairs=120)


def test_a_composite_cursor_keeps_its_lexicographic_order() -> None:
    rng = random.Random(20260727)
    keys = (Key("t", "timestamptz", indexed=True), Key("u", "uuid", unique=True))
    pairs = [
        (t, u)
        for t in _timestamps(rng, 8)[:24]
        for u in _uuids(rng, 4)[:5]
    ]
    survived = []
    for pair in pairs:
        decoded = through_ledger(keys, pair)
        assert tuple(instant(v) for v in decoded) == tuple(instant(v) for v in pair)
        survived.append((pair, decoded))
    for (x, dx), (y, dy) in itertools.combinations(survived[:60], 2):
        kx = tuple(instant(v) for v in x)
        ky = tuple(instant(v) for v in y)
        try:
            ordered = kx < ky
        except TypeError:
            continue
        if ordered:
            assert tuple(instant(v) for v in dx) < tuple(instant(v) for v in dy)


def test_a_decimal_key_is_refused_rather_than_silently_collapsed() -> None:
    """Two boundaries a decimal place apart must not become one number.

    ``float()`` is lossy by construction and there is no decimal codec to read a
    cursor back with, so this is refused where it is declared -- the same answer
    a non-unique boundary gets, and for the same reason.
    """
    for pg_type in ("numeric", "decimal", "NUMERIC"):
        with pytest.raises(PassDeclarationError, match="skips every row between them"):
            refuse_unsound_key(
                (Key("amount", pg_type, indexed=True, unique=True),), table="ledger"
            )


def test_the_collapse_the_decimal_refusal_prevents() -> None:
    """The defect itself, so the refusal is not mistaken for caution."""
    key = (Key("n", "float8", indexed=True, unique=True),)
    low, high = Decimal("1.0000000000000000001"), Decimal("1.0000000000000000002")
    assert low < high
    assert float(str(low)) == float(str(high))  # the two boundaries become one
    assert through_ledger(key, (float(str(low)),)) == through_ledger(key, (float(str(high)),))


def test_a_timestamp_comes_back_on_a_fixed_offset_naming_the_same_instant() -> None:
    """Pinned because it looks like a defect and is not -- see decode_cursor."""
    key = (Key("t", "timestamptz", indexed=True, unique=True),)
    original = dt.datetime(2026, 4, 5, 2, 0, fold=1, tzinfo=AKL)
    decoded = through_ledger(key, (original,))[0]
    assert isinstance(decoded, dt.datetime)
    assert decoded.tzinfo is not AKL
    assert decoded.utcoffset() == original.utcoffset()
    assert decoded.astimezone(UTC) == original.astimezone(UTC)
    # PEP 495: inside an ambiguous hour these compare unequal while naming one instant.
    assert decoded != original
