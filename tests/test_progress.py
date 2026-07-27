"""Task progress: registry, reporter, status endpoint, and streaming."""
from __future__ import annotations

import asyncio
import json

import pytest

from wreath.progress import (
    ProgressRegistry,
    progress_stream,
    push_progress,
    status_response,
)

pytestmark = pytest.mark.asyncio


async def test_reporter_updates_and_terminal_states() -> None:
    reg = ProgressRegistry()
    reporter = reg.reporter("t1")
    reporter.update(42, "processing")
    snap = reg.get("t1")
    assert snap.percent == 42 and snap.message == "processing" and snap.state == "running"
    reporter.done()
    assert reg.get("t1").state == "done" and reg.get("t1").percent == 100
    reporter.fail(RuntimeError("boom"))
    failed = reg.get("t1")
    assert failed.state == "failed" and "boom" in failed.error


async def test_percent_is_clamped() -> None:
    reg = ProgressRegistry()
    reg.report("t", -5)
    assert reg.get("t").percent == 0.0
    reg.report("t", 250)
    assert reg.get("t").percent == 100.0


async def test_stream_yields_updates_until_terminal() -> None:
    reg = ProgressRegistry()
    reg.report("job", 0, "start")

    async def producer() -> None:
        await asyncio.sleep(0.02)
        reg.report("job", 50, "half")
        await asyncio.sleep(0.02)
        reg.report("job", 100, "done", state="done")

    task = asyncio.create_task(producer())
    states = [p.state async for p in reg.stream("job", interval=0.005)]
    await task
    assert states[0] == "running"
    assert states[-1] == "done"        # stream ended on the terminal state


async def test_status_response_200_and_404() -> None:
    reg = ProgressRegistry()
    reg.report("known", 10, "hi")
    ok = status_response(reg, "known")
    assert ok.status == 200
    assert json.loads(ok.body)["percent"] == 10
    assert status_response(reg, "missing").status == 404


async def test_progress_stream_is_an_sse_response() -> None:
    reg = ProgressRegistry()
    reg.report("t", 100, state="done")
    response = progress_stream(reg, "t")
    assert response.media_type == b"text/event-stream"


async def test_push_progress_sends_json_frames() -> None:
    reg = ProgressRegistry()
    reg.report("t", 100, "finished", state="done")

    class _WS:
        def __init__(self) -> None:
            self.frames: list[str] = []

        async def send_text(self, text: str) -> None:
            self.frames.append(text)

    ws = _WS()
    await push_progress(ws, reg, "t", interval=0.005)
    assert json.loads(ws.frames[-1])["state"] == "done"


# --- why a stream ended (design 22 item 11) ---------------------------------


async def _events(registry, task_id, **kw):
    return [p async for p in registry.stream(task_id, interval=0.01, **kw)]


async def test_an_expired_entry_closes_the_stream_with_a_reason():
    """A long import must not end by appearing to still be running.

    `stream` used to just return when the entry aged out, so the last thing a
    client saw was `state: running` at whatever percent it had reached -- and a
    silent close is indistinguishable from the connection dropping.
    """
    registry = ProgressRegistry(ttl=0.05)
    registry.report("t", 40, "importing")

    seen = await _events(registry, "t")

    assert seen[-1].state == "expired"
    assert seen[-1].ends_stream
    assert not seen[-1].terminal, "the task did not finish; the registry forgot it"
    assert seen[-1].percent == 40.0, "carry the last percent, do not rewind to zero"


async def test_an_unknown_task_closes_the_stream_with_a_reason():
    registry = ProgressRegistry()

    seen = await _events(registry, "never-launched")

    assert [p.state for p in seen] == ["unknown"]
    assert seen[-1].ends_stream


async def test_a_spent_watch_budget_says_reconnect_rather_than_finished():
    registry = ProgressRegistry()
    registry.report("t", 10, "working")

    seen = await _events(registry, "t", max_duration=0.05)

    assert seen[-1].state == "detached"
    assert seen[-1].ends_stream
    assert not seen[-1].terminal, "the task is still going; the stream is not"


async def test_a_finished_task_still_ends_with_its_own_terminal_event():
    """The control: the ending states must not displace done/failed."""
    registry = ProgressRegistry()
    registry.report("t", 100, "finished", state="done")

    seen = await _events(registry, "t")

    assert [p.state for p in seen] == ["done"]
    assert seen[-1].terminal and seen[-1].ends_stream
