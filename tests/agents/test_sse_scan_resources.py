from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from wreath._agents import backplanes
from wreath._agents.backplanes import BackplaneError


@pytest.mark.parametrize("chunk_size", [1, 16, 1024, 4096])
async def test_sse_searches_each_partial_line_byte_once(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    searched = []

    class Buffer(bytearray):
        def find(self, sub: bytes, start: int = 0, end: int | None = None) -> int:
            stop = len(self) if end is None else end
            found = super().find(sub, start, stop)
            searched.append((stop if found < 0 else found + len(sub)) - start)
            return found

    monkeypatch.setattr(backplanes, "bytearray", Buffer, raising=False)
    events = [{"text": "a" * 1000}, {"text": "next"}]
    body = b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events)

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]
            yield b""

    actual = [
        event async for event in backplanes._json_sse(chunks(), maximum=len(body), provider="test")
    ]

    assert actual == events
    assert len(searched) >= 4
    assert sum(searched) == len(body)


@pytest.mark.parametrize("chunk_size", [1, 7, 1024])
async def test_sse_offset_preserves_multiline_done_and_unterminated_events(chunk_size: int) -> None:
    body = (
        b'\n: comment\r\nevent: ignored\r\ndata: {"n":\r\ndata: 1}\r\n\r\n'
        b'data: [DONE]\n\ndata: {"last":2}'
    )

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]

    actual = [
        event async for event in backplanes._json_sse(chunks(), maximum=len(body), provider="test")
    ]

    assert actual == [{"n": 1}, {"type": "__done__"}, {"last": 2}]


async def test_sse_maximum_refusal_closes_the_partial_line_producer() -> None:
    closed = []

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield b'data: {"text":'
            yield b'"too long"}'
        finally:
            closed.append(True)

    with pytest.raises(BackplaneError, match="test response exceeds 15 bytes"):
        _ = [event async for event in backplanes._json_sse(chunks(), maximum=15, provider="test")]

    assert closed == [True]


async def test_sse_cancellation_closes_a_producer_with_an_incomplete_line() -> None:
    waiting = asyncio.Event()
    closed = []

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield b'data: {"text":'
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    async def consume() -> list[Any]:
        return [
            event async for event in backplanes._json_sse(chunks(), maximum=100, provider="test")
        ]

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == [True]
