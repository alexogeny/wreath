"""A driver defect must reach the caller, not park it forever.

`_read_pipeline` runs in its own task. Nobody awaits that task, so an exception
it does not handle used to kill the reader, leave `_reader_task` cleared by the
`finally`, and strand every in-flight operation on a future that would never be
resolved. The caller did not fail -- it waited.

That is how `catalog destination requires binary rows` stayed invisible: the
cold path binds text, the native catalog decoder reads binary only, and the
resulting `ValueError` was outside the three exception classes the reader
handled. Because both affected tests carry `network`, the whole failure lived
under `-m ''`, where it hung the suite instead of failing one test.

These tests pin the property rather than that one bug: whatever goes wrong
inside the reader, the awaiting caller learns about it.
"""

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
@pytest.mark.parametrize(
    "backend", POSTGRES_BACKENDS, ids=lambda backend: backend._implementation
)
async def test_a_reader_defect_fails_every_caller_wherever_it_is_raised(
    monkeypatch: pytest.MonkeyPatch, loopback_dsn: str, backend: Any, hook: str
) -> None:
    """Whatever raises inside the reader, no caller is left waiting on it.

    The reader task is nobody's awaiter, so an exception it does not convert
    into a failed future is a hang rather than an error. Handling the exception
    is only half of that: the handler has to be able to *find* every operation,
    and an operation in transit between two of the three queues was in none of
    them. This walks the fault across all four points of the reader's handling
    of one message, so the seam cannot reopen silently.

    `asyncio.wait_for` is the assertion. Before the fix the `_flush_decode_batch`
    and `_finish_operation` cases never returned, so a bare `pytest.raises`
    would have hung the run instead of failing one test.

    Patched on **the backend's own `Connection`**, not always the Python one.

    That distinction used to make no difference, because the native class
    overrode only `_receive_message` and inherited every hook below from the
    pure base. It stopped being true when `_finish_operation` and
    `_publish_completed` moved into C: patching the base still worked for the
    pure backend and reached nothing at all in the native one, where the
    subclass's own attribute wins. The fault was injected, no exception was
    raised, and the test failed -- correctly, because a seam it believed it was
    walking had gone unwalked.

    The native `Connection` is a heap type, so its attributes *are* assignable;
    the older claim to the contrary here was about a design that no longer
    exists. Patching per backend is what keeps this test honest against both.
    """
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
    """An unexpected exception fails the query instead of hanging it.

    `asyncio.wait_for` is the assertion: before the fix this call never returned,
    so a plain `pytest.raises` would have hung the run rather than failed it.

    Driven through `_pgdriver`'s `Connection` because `_read_pipeline` -- where the
    fix lives -- is pure Python that the native class inherits, and a C type's
    attributes cannot be patched. The reader is the same code either way.

    Kept alongside the loopback parametrization above rather than replaced by
    it: this one is the same property against a real server, and the two have
    failed for different reasons before.
    """
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
    """The first catalog read on a connection is the one that used to hang.

    A cold operation binds text results; the catalog destination decodes binary
    only. Nothing warmed the plan first, so `detect_single` against a fresh
    connection was the failing shape -- and the tests that happened to run a
    `generate` beforehand passed, which is why it looked intermittent.
    """
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
