from __future__ import annotations

import tracemalloc
from hashlib import sha256
from typing import Any

import pytest

from wreath.request import Request
from wreath.state import BODY_CHECK_SLOT


@pytest.mark.parametrize("native_messages", [False, True])
@pytest.mark.parametrize("signed_body", [False, True])
async def test_body_collection_retains_one_body_buffer(
    native_messages: bool, signed_body: bool
) -> None:
    chunk_size = 128 * 1024
    chunks = 64
    size = chunk_size * chunks
    received = 0

    async def receive() -> Any:
        nonlocal received
        body = bytes([received]) * chunk_size
        received += 1
        more = received < chunks
        if native_messages:
            return body, more, False
        return {"type": "http.request", "body": body, "more_body": more}

    request = Request({"type": "http"}, receive)
    if signed_body:
        digest = sha256()
        for index in range(chunks):
            digest.update(bytes([index]) * chunk_size)
        setattr(request.state, BODY_CHECK_SLOT, ("sha-256", digest.digest()))
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        body = await request.body()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert received == chunks
    assert body == b"".join(bytes([index]) * chunk_size for index in range(chunks))
    assert await request.body() is body
    assert peak - before < size * 5 // 4


async def test_body_collection_preserves_single_chunk_identity() -> None:
    chunk = b"one complete body"
    received = 0

    async def receive() -> dict[str, Any]:
        nonlocal received
        received += 1
        return {"type": "http.request", "body": chunk}

    request = Request({"type": "http"}, receive)
    assert await request.body() is chunk
    assert await request.body() is chunk
    assert received == 1


@pytest.mark.parametrize("native_messages", [False, True])
async def test_empty_signed_stream_has_no_payload_chunks(native_messages: bool) -> None:
    async def receive() -> Any:
        if native_messages:
            return b"", False, False
        return {"type": "http.request", "body": b""}

    request = Request({"type": "http"}, receive)
    setattr(request.state, BODY_CHECK_SLOT, ("sha-256", sha256(b"").digest()))
    assert [chunk async for chunk in request.stream()] == []


@pytest.mark.parametrize("read_body", [False, True])
async def test_unsigned_single_chunk_needs_no_collector(
    monkeypatch: pytest.MonkeyPatch, read_body: bool
) -> None:
    def unexpected_collector() -> None:
        raise AssertionError("unsigned single chunk allocated a body collector")

    monkeypatch.setattr("wreath.request.BytesIO", unexpected_collector)
    chunk = b"already complete"

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": chunk}

    request = Request({"type": "http"}, receive)
    if read_body:
        assert await request.body() is chunk
    else:
        assert [part async for part in request.stream()] == [chunk]
