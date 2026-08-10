"""`numeric` is exact, and the sweep is what proves it.

PostgreSQL's `numeric` exists to hold values binary floating point cannot. The
codec landed it on `float` before this, so `Decimal("1.0000000000000000001")`
and `...002` both arrived as `1.0` -- two distinct values collapsing onto one.
That is a correctness defect rather than a rounding one: a chunked walk keying
on such a column advances its cursor to a value *below* where it actually
reached and skips every row in between.

So the tests here are a property sweep rather than a handful of examples. A
`format_duration` sweep earlier in this codebase found 19,254 failures in 20,012
samples, in code whose one round-trip test used the single shape that happened
to work. The domain below is chosen to include the shapes that break naive
implementations: group boundaries, scale that must survive, exponents past
float's reach, and the non-finite values `numeric` gained in PostgreSQL 14.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from wreath._pgdriver import _decode_numeric, _encode_numeric
from wreath.postgres import Database, PoolConfig

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_live = pytest.mark.skipif(
    not _DSN, reason="set WREATH_TEST_POSTGRES_DSN for live numeric codec tests"
)

try:  # the native twin is optional; parity is only asserted when it is built
    from wreath._native import _postgres as _native
except ImportError:  # pragma: no cover - pure-only builds
    _native = None


def _domain() -> list[Decimal]:
    """Values chosen for where numeric implementations actually go wrong."""
    values = [
        Decimal("0"),
        Decimal("-0"),
        Decimal("1"),
        Decimal("-1"),
        # Base-10000 group boundaries: the split is per four decimal digits, so
        # these are where an off-by-one in the grouping shows up.
        Decimal("9999"),
        Decimal("10000"),
        Decimal("10001"),
        Decimal("99999999"),
        Decimal("100000000"),
        Decimal("0.9999"),
        Decimal("0.0001"),
        Decimal("0.00001"),
        # Scale that must survive: 1.10 is not 1.1 as a numeric.
        Decimal("1.10"),
        Decimal("1.100000"),
        Decimal("0.00"),
        Decimal("-1.10"),
        # More significant digits than a double can hold. These are the values
        # that motivated the type; a float codec collapses them.
        Decimal("1.0000000000000000001"),
        Decimal("1.0000000000000000002"),
        Decimal("123456789012345678901234567890.123456789"),
        Decimal("-123456789012345678901234567890.123456789"),
        # Exponent extremes.
        Decimal("1E+100"),
        Decimal("1E-100"),
        Decimal("-1E-100"),
        Decimal("1E-1000"),
        # Fractions whose padding to a group boundary is non-trivial.
        Decimal("0.5"),
        Decimal("0.25"),
        Decimal("0.125"),
        Decimal("3.14159265358979323846264338327950288"),
        Decimal("-0.000000000000000000000000000001"),
    ]
    # A deterministic spread so the sweep is wider than the hand-picked list
    # without becoming irreproducible.
    for whole in range(0, 40):
        for scale in (0, 1, 4, 5, 8):
            values.append(Decimal(f"{whole}.{'1234567890'[:scale] or '0'}"))
            values.append(Decimal(f"-{whole}.{'9876543210'[:scale] or '0'}"))
    return values


NON_FINITE = [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]


def test_the_binary_round_trip_is_exact_over_the_whole_domain() -> None:
    """Every value survives encode->decode with its scale intact."""
    domain = _domain()
    failures = []
    for value in domain:
        decoded = _decode_numeric(_encode_numeric(value))
        # PostgreSQL normalises a positive exponent to a plain integer with
        # scale 0 -- `SELECT '1E+100'::numeric` is the expanded digits -- so the
        # comparison is on value plus scale, not on `str` for those.
        if value.as_tuple().exponent > 0:  # type: ignore[operator]
            if decoded != value:
                failures.append((value, decoded))
        elif str(decoded) != str(value.copy_abs() if value == 0 else value):
            failures.append((value, decoded))
    assert not failures, f"{len(failures)} of {len(domain)}: {failures[:5]}"


def test_a_value_float_cannot_hold_survives() -> None:
    """The defect this codec exists to fix, stated as one assertion."""
    near = Decimal("1.0000000000000000001")
    far = Decimal("1.0000000000000000002")
    assert float(near) == float(far), "the premise: float collapses these"
    assert _decode_numeric(_encode_numeric(near)) != _decode_numeric(_encode_numeric(far))


def test_scale_is_carried_not_inferred() -> None:
    """`1.10` is a different numeric from `1.1`; dscale is what preserves it."""
    assert str(_decode_numeric(_encode_numeric(Decimal("1.10")))) == "1.10"
    assert str(_decode_numeric(_encode_numeric(Decimal("1.1")))) == "1.1"


@pytest.mark.parametrize("value", NON_FINITE, ids=str)
def test_the_non_finite_values_round_trip(value: Decimal) -> None:
    decoded = _decode_numeric(_encode_numeric(value))
    if value.is_nan():
        assert decoded.is_nan()
    else:
        assert decoded == value


def test_a_float_is_refused_rather_than_silently_converted() -> None:
    """Accepting a float here would reinstate the collapse the type prevents."""
    with pytest.raises(TypeError, match="cannot hold a numeric exactly"):
        _encode_numeric(1.5)


def test_a_negative_zero_goes_on_the_wire_as_postgresql_spells_it() -> None:
    """`SELECT '-0'::numeric` is `0`; the codec must not invent a sign."""
    assert _encode_numeric(Decimal("-0")) == _encode_numeric(Decimal("0"))


@pytest.mark.skipif(_native is None, reason="native extension not built")
def test_both_codecs_agree_byte_for_byte() -> None:
    """`_pgdriver` and `codec.c` encode identically and decode identically.

    They are not alternatives -- the C `Connection` subclasses the Python one --
    but a row can be decoded through either depending on which entry point the
    read took, so a divergence is a value that changes with the call path.
    """
    domain = [*_domain(), *NON_FINITE]
    encode_diff = [v for v in domain if _encode_numeric(v) != _native._encode_binary(v, 1700)]
    assert not encode_diff, f"{len(encode_diff)} encode divergences: {encode_diff[:5]}"
    decode_diff = []
    for value in domain:
        wire = _encode_numeric(value)
        pure, native = _decode_numeric(wire), _native._decode_value(1700, 1, wire)
        if not ((pure.is_nan() and native.is_nan()) or str(pure) == str(native)):
            decode_diff.append((value, pure, native))
    assert not decode_diff, f"{len(decode_diff)} decode divergences: {decode_diff[:5]}"


@_live
@pytest.mark.asyncio
async def test_the_round_trip_through_postgresql_is_exact() -> None:
    """The sweep that a fake cannot run: real server, both wire formats.

    A cold (unprepared) query binds and returns text format; a cached plan uses
    binary. Both paths decode `numeric`, so both are swept here -- a codec that
    only got one right would still hand a caller the wrong value half the time.
    """
    database = Database("main", _DSN or "", pools={"write": PoolConfig(min_size=1, max_size=2)})
    await database.start()
    domain = [*_domain(), *NON_FINITE]
    try:
        connection = await database.acquire("write")
        try:
            await connection.execute("DROP TABLE IF EXISTS numeric_sweep")
            await connection.execute("CREATE TABLE numeric_sweep (v numeric)")
            failures = []
            for value in domain:
                await connection.execute("DELETE FROM numeric_sweep")
                await connection.execute(
                    "INSERT INTO numeric_sweep (v) VALUES ($1)", value
                )
                back = await connection.fetchval("SELECT v FROM numeric_sweep")
                if not isinstance(back, Decimal):
                    failures.append((value, back, "not a Decimal"))
                elif value.is_nan():
                    if not back.is_nan():
                        failures.append((value, back, "NaN lost"))
                elif back != value:
                    failures.append((value, back, "value changed"))
            assert not failures, f"{len(failures)} of {len(domain)}: {failures[:5]}"
        finally:
            await database.release("write", connection)
    finally:
        await database.stop()


@_live
@pytest.mark.asyncio
async def test_avg_over_a_numeric_column_returns_a_decimal() -> None:
    """The reported symptom: `avg()` handed raw bytes to every caller."""
    database = Database("main", _DSN or "", pools={"write": PoolConfig(min_size=1, max_size=2)})
    await database.start()
    try:
        connection = await database.acquire("write")
        try:
            await connection.execute("DROP TABLE IF EXISTS numeric_avg")
            await connection.execute("CREATE TABLE numeric_avg (v numeric(30,10))")
            await connection.execute(
                "INSERT INTO numeric_avg (v) VALUES (1.0), (2.5)"
            )
            average = await connection.fetchval("SELECT avg(v) FROM numeric_avg")
            total = await connection.fetchval("SELECT sum(v) FROM numeric_avg")
            assert isinstance(average, Decimal), f"avg gave {type(average).__name__}"
            assert isinstance(total, Decimal), f"sum gave {type(total).__name__}"
            assert average == Decimal("1.75")
            assert total == Decimal("3.5")
        finally:
            await database.release("write", connection)
    finally:
        await database.stop()
