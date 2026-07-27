"""Binding a sequence is refused, and the refusal teaches.

`= ANY($1)` with a Python list shipped in three places -- `rewritten_columns`,
`pending_facts`, and `Recode`'s conversion SQL -- two of them safety refusals
built the same day. None had ever worked against a real server, because
`_infer_oid` has no `list` case and raises before PostgreSQL is reached. All
three now use `IN ($1, $2, ...)`.

The driver still cannot bind a sequence, and that is deliberate rather than
pending:

* inference is genuinely ambiguous -- `[1, 2]` is equally `int4[]`, `int8[]` or
  `numeric[]`, and `[]` names no element type at all;
* the native codec has an array encoder and the pure twin does not (see
  `tests/orm/test_array_codec_roundtrip.py`, "the pure-Python array twin is
  deferred"). Inferring an array OID would make the native build succeed where
  the pure build raised `no binary encoder for PostgreSQL OID 1007` -- a *new*
  divergence, where today both fail identically.

So the fix was to the message, not the behaviour. A `TypeError` that neither
works nor explains is the worst of both.
"""

from __future__ import annotations

import os

import pytest

from wreath._pure.postgres import _infer_oid
from wreath.postgres import Database, PoolConfig

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
_live = pytest.mark.skipif(
    not _DSN, reason="set WREATH_TEST_POSTGRES_DSN for live array binding tests"
)


@pytest.mark.parametrize("value", [[1, 2, 3], [], ["a"], (1, 2), {1, 2}, frozenset({1})])
def test_a_sequence_is_refused_by_inference(value: object) -> None:
    with pytest.raises(TypeError) as caught:
        _infer_oid(value)
    assert "unsupported PostgreSQL value type" in str(caught.value)


@pytest.mark.parametrize("value", [[1, 2, 3], [], (1, 2)])
def test_the_refusal_names_the_idiom_that_works(value: object) -> None:
    """The point of the change: it has to tell you what to write instead."""
    with pytest.raises(TypeError) as caught:
        _infer_oid(value)
    message = str(caught.value)
    assert "IN ($1, $2, ...)" in message
    assert "ANY($1)" in message
    # and why inference cannot simply guess
    assert "element type" in message


def test_a_non_sequence_keeps_the_short_message() -> None:
    """Only sequences get the essay; an arbitrary object has nothing to be told."""
    with pytest.raises(TypeError) as caught:
        _infer_oid(object())
    assert str(caught.value) == "unsupported PostgreSQL value type: object"


@_live
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    ["SELECT $1::int4[]", "SELECT 1 = ANY($1::int4[])", "SELECT $1::text[]"],
)
async def test_the_server_is_never_reached_for_a_sequence(sql: str) -> None:
    """Refused client-side, so the shape of the SQL is irrelevant."""
    db = Database("main", _DSN or "", pools={"write": PoolConfig(min_size=1, max_size=2)})
    await db.start()
    try:
        connection = await db.acquire("write")
        try:
            with pytest.raises(TypeError, match="unsupported PostgreSQL value type"):
                await connection.fetchval(sql, [1, 2, 3])
        finally:
            await db.release("write", connection)
    finally:
        await db.stop()


@_live
@pytest.mark.asyncio
async def test_the_idiom_the_refusal_recommends_actually_works() -> None:
    """A refusal that recommends something broken would be worse than silence."""
    db = Database("main", _DSN or "", pools={"write": PoolConfig(min_size=1, max_size=2)})
    await db.start()
    try:
        connection = await db.acquire("write")
        try:
            found = await connection.fetchval(
                "SELECT 2 IN ($1, $2, $3)", 1, 2, 3
            )
            assert found is True
        finally:
            await db.release("write", connection)
    finally:
        await db.stop()
