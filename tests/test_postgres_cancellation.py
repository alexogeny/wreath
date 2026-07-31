"""Cancelling the awaiting task stops the PostgreSQL backend, not just the await.

A cancelled Python task does not, by itself, cancel a running server-side query:
the backend keeps scanning until it tries to write to a socket nobody is reading.
Stopping it needs a wire-level `CancelRequest` on a *second* connection, carrying
the backend PID and secret key from the startup handshake. `Connection` sends
one, and these tests hold it to that from outside — `pg_stat_activity`, observed
over an independent connection, is the evidence. Asserting that our own `await`
raised `CancelledError` would prove only that asyncio works.

Both drivers share this path rather than twinning it: the native `Connection`
subclasses the pure one, so `_cancel_operation` and `_send_cancel_request` are
one implementation, exercised here under whichever backend is selected.

**This file covers layers 2-4 of the cancellation chain and deliberately not
layer 1.** A client disconnect does not currently cancel the handler task -- the
server queues `http.disconnect` for a handler that is not awaiting `receive()`
-- so nothing upstream generates the `CancelledError` these tests inject by hand.
See `.plans/14-cancellation-into-storage.md`; when that lands, its own test
belongs with the server, and this file keeps guarding the half beneath it.
"""

from __future__ import annotations

import asyncio
import os

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.database

#: Long enough that a backend still running it is unambiguous, never reached.
_SLEEP_SECONDS = 30


@pytest.fixture
async def pair():
    """A victim connection to cancel, and an observer to watch it from."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live cancellation tests")
    from wreath.postgres import connect

    victim = await connect(_DSN)
    observer = await connect(_DSN)
    try:
        yield victim, observer
    finally:
        await victim.close()
        await observer.close()


async def _backend_state(observer, pid: int) -> str | None:
    return await observer.fetchval(
        "SELECT state FROM pg_stat_activity WHERE pid = $1", pid
    )


async def _settle(observer, pid: int, *, want: str) -> str | None:
    """Poll briefly for `want`; a cancel is a round trip, not instantaneous."""
    state = None
    for _ in range(40):
        state = await _backend_state(observer, pid)
        if state == want:
            return state
        await asyncio.sleep(0.05)
    return state


async def test_cancelling_the_await_stops_the_backend(pair):
    """The server-side query ends, observed from another connection."""
    victim, observer = pair
    pid = await victim.fetchval("SELECT pg_backend_pid()")

    task = asyncio.create_task(victim.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})"))
    assert await _settle(observer, pid, want="active") == "active"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _settle(observer, pid, want="idle") == "idle", (
        "the backend was still running after the awaiting task was cancelled; "
        "a CancelRequest was not delivered"
    )


async def test_an_uncancelled_query_keeps_running(pair):
    """The control. Without this, the test above could pass vacuously.

    If `pg_sleep` were somehow not running by the time we look, or the state
    column read `idle` for an unrelated reason, the assertion above would be
    satisfied by a backend that was never cancelled at all.
    """
    victim, observer = pair
    pid = await victim.fetchval("SELECT pg_backend_pid()")

    task = asyncio.create_task(victim.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})"))
    try:
        assert await _settle(observer, pid, want="active") == "active"
        await asyncio.sleep(1.0)
        assert await _backend_state(observer, pid) == "active", (
            "the backend stopped without anyone cancelling it, so the "
            "cancellation test above proves nothing"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_connection_survives_a_cancelled_query(pair):
    """A poisoned connection returned to a pool is worse than a wasted scan."""
    victim, observer = pair
    pid = await victim.fetchval("SELECT pg_backend_pid()")

    task = asyncio.create_task(victim.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})"))
    assert await _settle(observer, pid, want="active") == "active"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle(observer, pid, want="idle")

    assert await asyncio.wait_for(victim.fetchval("SELECT 42"), timeout=10) == 42
    assert await victim.fetchval("SELECT pg_backend_pid()") == pid, (
        "the connection was silently replaced rather than reused"
    )


async def test_a_cancelled_query_leaves_the_transaction_clean(pair):
    """`idle in transaction` after a cancel is a held-open snapshot."""
    victim, observer = pair
    pid = await victim.fetchval("SELECT pg_backend_pid()")

    task = asyncio.create_task(victim.fetchval(f"SELECT pg_sleep({_SLEEP_SECONDS})"))
    assert await _settle(observer, pid, want="active") == "active"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = await _settle(observer, pid, want="idle")
    assert state == "idle", f"expected a clean idle backend, saw {state!r}"
