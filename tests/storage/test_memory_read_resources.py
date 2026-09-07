from __future__ import annotations

import tracemalloc
from hashlib import md5, sha256

import pytest

from wreath import objects
from wreath.objects import MemoryObjectStore, ObjectError


async def test_memory_metadata_hashes_each_published_body_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashed = []
    etag = objects._etag

    def counted(data: bytes) -> str:
        hashed.append(data)
        return etag(data)

    monkeypatch.setattr(objects, "_etag", counted)
    store = MemoryObjectStore()
    original = await store.write("nested//body", b"original", content_type="text/plain")
    for _ in range(3):
        assert await store.stat("nested/body") == original
        assert [stat async for stat in store.list("nested/")] == [original]
    assert hashed == [b"original"]

    replacement = await store.write("nested/body", b"replacement", content_type="text/custom")
    assert await store.stat("nested/body") == replacement
    assert [stat async for stat in store.list()] == [replacement]
    assert hashed == [b"original", b"replacement"]
    assert replacement.etag == md5(b"replacement", usedforsecurity=False).hexdigest()


async def test_memory_range_stream_retains_only_bounded_chunk_copies() -> None:
    store = MemoryObjectStore()
    await store.write("body", b"x" * (8 * 1024 * 1024))
    total = 0
    tracemalloc.start()
    try:
        async for chunk in store.read_stream("body", range=(1024, 4 * 1024 * 1024 - 1)):
            assert chunk == b"x" * len(chunk)
            total += len(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert total == 4 * 1024 * 1024 - 1024
    assert peak < 256 * 1024


async def test_memory_metadata_tracks_conditional_write_and_delete_versions() -> None:
    store = MemoryObjectStore()
    original = await store._upload_compare_and_swap("body", b"first", expected_etag=None)
    assert original is not None
    assert await store.stat("body") == original
    assert await store._upload_read_versioned("body") == (b"first", sha256(b"first").hexdigest())
    assert original.etag == md5(b"first", usedforsecurity=False).hexdigest()
    assert (
        await store._upload_compare_and_swap("body", b"wrong", expected_etag=original.etag) is None
    )
    assert await store.stat("body") == original

    updated = await store._upload_compare_and_swap(
        "body", b"second", expected_etag=sha256(b"first").hexdigest(), content_type="text/new"
    )
    assert updated is not None
    assert await store.stat("body") == updated
    assert [stat async for stat in store.list()] == [updated]
    assert await store._upload_read_versioned("body") == (b"second", sha256(b"second").hexdigest())
    assert not await store._upload_delete_versioned("body", expected_etag=original.etag)
    assert await store._upload_delete_versioned("body", expected_etag=sha256(b"second").hexdigest())
    assert [stat async for stat in store.list()] == []
    with pytest.raises(ObjectError, match="no such object"):
        await store.stat("body")
    await store.delete("body")
    assert await store._upload_read_versioned("body") is None


@pytest.mark.parametrize("span", [None, (0, 0), (1, 65536), (65535, 999999), (999999, 1000000)])
async def test_memory_range_stream_matches_bytes_slicing_and_read_override(
    span: tuple[int, int] | None,
) -> None:
    data = bytes(range(256)) * 300
    reads = []

    class Store(MemoryObjectStore):
        async def read(self, key: str) -> bytes:
            reads.append(key)
            return data

    store = Store()
    chunks = [chunk async for chunk in store.read_stream("virtual", range=span)]

    expected = data if span is None else data[span[0] : span[1] + 1]
    assert b"".join(chunks) == expected
    assert all(type(chunk) is bytes and 0 < len(chunk) <= 65536 for chunk in chunks)
    assert reads == ["virtual"]


async def test_memory_range_stream_retains_its_snapshot_across_overwrite_and_early_close() -> None:
    store = MemoryObjectStore()
    original = bytes(range(256)) * 1000
    await store.write("body", original)
    stream = store.read_stream("body", range=(1, len(original) - 2))
    first = await anext(stream)
    await store.write("body", b"replacement")
    rest = b"".join([chunk async for chunk in stream])
    assert first + rest == original[1:-1]

    stream = store.read_stream("body")
    assert await anext(stream) == b"replacement"
    await stream.aclose()
    assert await store.read("body") == b"replacement"
