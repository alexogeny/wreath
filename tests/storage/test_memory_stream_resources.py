from __future__ import annotations

import tracemalloc
from collections.abc import AsyncIterator
from hashlib import md5
from typing import Any

import pytest

from wreath.objects import MemoryObjectStore, ObjectStat


async def test_memory_stream_keeps_one_completed_body_at_the_write_boundary() -> None:
    size = 8 * 1024 * 1024
    chunk = b"x" * (128 * 1024)
    observations = []

    class Store(MemoryObjectStore):
        async def write(
            self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
        ) -> ObjectStat:
            result = await super().write(key, data, content_type=content_type)
            observations.append(tracemalloc.get_traced_memory())
            return result

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield chunk

    store = Store()
    tracemalloc.start()
    try:
        result = await store.write_stream("body", chunks(), content_type="application/octet-stream")
    finally:
        tracemalloc.stop()

    assert len(observations) == 1
    retained, peak = observations[0]
    assert retained < size * 5 // 4
    assert peak < size * 5 // 4
    assert result.size == size
    assert result.content_type == "application/octet-stream"
    assert await store.read("body") == chunk * 64


async def test_memory_stream_snapshots_mutable_chunks_before_resuming_producer() -> None:
    store = MemoryObjectStore()
    buffer = bytearray(b"first")

    async def chunks() -> AsyncIterator[bytearray | memoryview]:
        yield buffer
        buffer[:] = b"other"
        yield memoryview(buffer)
        buffer[:] = b"final"

    result = await store.write_stream("body", chunks())

    assert await store.read("body") == b"firstother"
    assert result.size == 10
    assert result.etag == md5(b"firstother", usedforsecurity=False).hexdigest()


async def test_memory_stream_empty_input_calls_overridden_write_once() -> None:
    calls = []
    expected = ObjectStat("redirected", 0, "custom", None, "custom/type")

    class Store(MemoryObjectStore):
        async def write(
            self, key: str, data: bytes | bytearray | memoryview, *, content_type: str | None = None
        ) -> ObjectStat:
            calls.append((key, bytes(data), content_type))
            return expected

    async def chunks() -> AsyncIterator[bytes]:
        for _ in ():
            yield b""

    store = Store()

    assert await store.write_stream("body", chunks(), content_type="text/plain") is expected
    assert calls == [("body", b"", "text/plain")]


@pytest.mark.parametrize("failure", [TypeError, RuntimeError])
async def test_memory_stream_failure_does_not_publish_partial_data(
    failure: type[Exception],
) -> None:
    store = MemoryObjectStore()
    await store.write("body", b"original")

    async def chunks() -> AsyncIterator[Any]:
        yield b"partial"
        if failure is TypeError:
            yield "not bytes"
        else:
            raise RuntimeError("producer failed")

    with pytest.raises(failure):
        await store.write_stream("body", chunks())

    assert await store.read("body") == b"original"
