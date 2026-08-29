from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from wreath._pgdriver import Connection as PureConnection
from wreath._pgdriver import connect as pure_connect
from wreath.postgres import connect

from .test_connection import POSTGRES_BACKENDS, FakePostgres

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def _connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real PostgreSQL reader tests")
    return await connect(_DSN)


@pytest.fixture
async def loopback_dsn() -> AsyncIterator[str]:
    """A scripted PostgreSQL on loopback, so this property needs no container.

    The reader's failure handling is protocol-level and a fake server drives it
    exactly. Keeping it off `WREATH_TEST_POSTGRES_DSN` matters more than usual
    here: the first version of this suite was gated on a container *and* marked
    `network`, so the property it pins went unrun in the default suite -- and
    the seam below stayed open underneath a test that looked like it covered it.
    """
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    try:
        yield dsn
    finally:
        await server.close()


@pytest.mark.parametrize(
    "hook",
    [
        # Raised while the operation is still the head of `_emitted`.
        "_consume_message",
        # Raised after `_emitted.popleft()` and before `_finish_operation`
        # appends to `_completed` -- the seam where the operation belongs to no
        # queue at all. Decoding a row batch happens here, which is why a
        # catalog read the decoder could not read hung rather than failed.
        "_flush_decode_batch",
        # Raised between the same two points, one step later.
        "_finish_operation",
        # Raised with the operation already in `_completed`.
        "_publish_completed",
    ],
)
@pytest.mark.parametrize("backend", POSTGRES_BACKENDS, ids=lambda backend: backend._implementation)
async def test_a_reader_defect_fails_every_caller_wherever_it_is_raised(
    monkeypatch: pytest.MonkeyPatch, loopback_dsn: str, backend: Any, hook: str
) -> None:
    if hook == "_flush_decode_batch" and not backend.Connection._batch_decode:
        pytest.skip("this backend decodes row by row and never flushes a batch")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValueError("simulated reader defect")

    connection = await backend.connect(loopback_dsn)
    try:
        monkeypatch.setattr(backend.Connection, hook, explode)

        # Two operations, so "every waiting caller" is more than a claim: the
        # first is the one in transit, the second is still queued behind it.
        first = asyncio.ensure_future(connection.fetch("select 1"))
        second = asyncio.ensure_future(connection.fetch("select 2"))
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=5.0
        )

        for index, result in enumerate(results):
            assert isinstance(result, BaseException), (
                f"operation {index} returned {result!r} instead of failing"
            )
            assert not isinstance(result, TimeoutError), (
                f"operation {index} timed out, so the reader died without failing it"
            )
        assert connection.closed
    finally:
        monkeypatch.undo()
        await connection.close()


async def test_a_decode_failure_in_the_reader_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real PostgreSQL reader tests")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValueError("simulated decoder defect")

    connection = await pure_connect(_DSN)
    try:
        monkeypatch.setattr(PureConnection, "_consume_message", explode)

        with pytest.raises(Exception) as caught:  # asserted below
            await asyncio.wait_for(connection.fetch("SELECT 1"), timeout=5.0)

        assert not isinstance(caught.value, TimeoutError), (
            "the caller timed out, which means the reader died without failing it"
        )
        chain = f"{caught.value}{caught.value.__cause__}"
        assert "simulated decoder defect" in chain
    finally:
        monkeypatch.undo()
        await connection.close()


async def test_a_cold_catalog_read_decodes_on_the_first_call() -> None:
    from uuid import uuid4

    from wreath.migrations import detect_single
    from wreath.orm import Mapped, Model, column
    from wreath.orm.registry import Registry
    from wreath.orm.types import Int64

    schema = f"wreath_cold_{uuid4().hex[:12]}"

    class Widget(Model, table="widgets", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)

    class _Db:
        name = "cold-catalog-test"

    registry = Registry(_Db(), [Widget], validate_schema="off")
    connection = await _connection()
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'CREATE TABLE "{schema}"."widgets" (id bigint PRIMARY KEY)')

        # No warm-up: this is the first catalog statement this connection sends.
        detected = await asyncio.wait_for(detect_single(registry, connection), timeout=15.0)

        assert detected.current
        assert detected.diff.operation_count == 0
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
