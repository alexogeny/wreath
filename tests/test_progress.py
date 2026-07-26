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
