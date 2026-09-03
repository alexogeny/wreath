from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._agents.artifacts import AgentArtifactManager, ArtifactLimitExceeded, _digest_parts
from wreath.objects import MemoryObjectStore, ObjectStat
from wreath.provenance import Provenance


class Store(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, Any, str | None]] = []
        self.stream_writes: list[str] = []
        self.deleted: list[str] = []

    async def write(
        self,
        key: str,
        data: bytes | bytearray | memoryview,
        *,
        content_type: str | None = None,
    ) -> ObjectStat:
        self.writes.append((key, data, content_type))
        return await super().write(key, data, content_type=content_type)

    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes | bytearray | memoryview],
        *,
        content_type: str | None = None,
    ) -> ObjectStat:
        self.stream_writes.append(key)
        body = bytearray()
        async for chunk in chunks:
            body.extend(chunk)
        return await super().write(key, body, content_type=content_type)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        await super().delete(key)


def context(
    *,
    tenant: str = "tenant-a",
    principal: str = "user-7",
    conversation: str = "conversation-2",
) -> SimpleNamespace:
    return SimpleNamespace(
        tenant=tenant,
        principal=SimpleNamespace(id=principal),
        conversation=conversation,
    )


@pytest.mark.parametrize("part", ["", cast(str, 1)])
def test_artifact_digest_refuses_invalid_identity_parts(part: str) -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        _digest_parts("tenant", part)


@pytest.mark.asyncio
async def test_artifact_key_and_metadata_are_identity_bound_and_body_is_written_once() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=16, max_artifacts=2)
    body = b"report"

    artifact = await manager.write(
        context(),
        artifact_id="report.csv",
        ordinal=0,
        body=body,
        media_type="text/csv",
        trust="tool",
    )
    other_tenant = manager.key(context(tenant="tenant-b"), "report.csv", ordinal=0)
    other_principal = manager.key(context(principal="user-8"), "report.csv", ordinal=0)

    assert artifact.key != other_tenant
    assert artifact.key != other_principal
    assert store.writes == [(artifact.key, body, "text/csv")]
    assert store.writes[0][1] is body
    assert artifact.digest == hashlib.sha256(body).hexdigest()
    assert artifact.media_type == "text/csv"
    assert artifact.trust == "tool"
    assert isinstance(artifact.provenance, Provenance)
    assert artifact.provenance.digest.hex() == artifact.digest
    assert artifact.stat.size == 6


@pytest.mark.asyncio
async def test_mutable_artifact_body_is_snapshotted_before_storage_can_suspend() -> None:
    class YieldingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.resume = asyncio.Event()

        async def write(
            self,
            key: str,
            data: bytes | bytearray | memoryview,
            *,
            content_type: str | None = None,
        ) -> ObjectStat:
            self.started.set()
            await self.resume.wait()
            return await super().write(key, data, content_type=content_type)

    store = YieldingStore()
    manager = AgentArtifactManager(store, max_bytes=16, max_artifacts=1)
    body = bytearray(b"good")
    pending = asyncio.create_task(
        manager.write(context(), artifact_id="result", ordinal=0, body=body)
    )
    await store.started.wait()
    body[:] = b"evil"
    store.resume.set()

    artifact = await pending

    assert await store.read(artifact.key) == b"good"
    assert artifact.digest == hashlib.sha256(b"good").hexdigest()


@pytest.mark.asyncio
async def test_byte_and_count_ceilings_refuse_before_writing() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=4, max_artifacts=1)

    with pytest.raises(ArtifactLimitExceeded, match="byte ceiling"):
        await manager.write(context(), artifact_id="large", ordinal=0, body=b"12345")
    with pytest.raises(ArtifactLimitExceeded, match="count ceiling"):
        await manager.write(context(), artifact_id="second", ordinal=1, body=b"1")
    with pytest.raises(ValueError, match="media_type"):
        await manager.write(context(), artifact_id="bad", ordinal=0, body=b"1", media_type="")

    assert store.writes == []
    assert manager.key(context(), "first-name", ordinal=0) != manager.key(
        context(), "replacement-name", ordinal=0
    )


@pytest.mark.asyncio
async def test_oversized_mutable_body_refuses_before_snapshotting() -> None:
    class ObservableBytearray(bytearray):
        snapshots = 0

        def __bytes__(self) -> bytes:
            self.snapshots += 1
            return memoryview(self).tobytes()

    body = ObservableBytearray(b"oversized")
    manager = AgentArtifactManager(Store(), max_bytes=4, max_artifacts=1)

    with pytest.raises(ArtifactLimitExceeded, match="byte ceiling"):
        await manager.write(context(), artifact_id="large", ordinal=0, body=body)

    assert body.snapshots == 0


@pytest.mark.asyncio
async def test_write_refuses_non_buffer_body_before_storage() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=4, max_artifacts=1)

    with pytest.raises(TypeError, match="body must be bytes"):
        await manager.write(
            context(), artifact_id="invalid", ordinal=0, body=cast(Any, object())
        )

    assert store.writes == []


@pytest.mark.asyncio
async def test_memoryview_ceiling_counts_bytes_not_elements() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=3, max_artifacts=1)
    body = memoryview(bytearray(b"1234")).cast("H")

    with pytest.raises(ArtifactLimitExceeded, match="byte ceiling"):
        await manager.write(context(), artifact_id="large", ordinal=0, body=body)

    assert len(body) == 2
    assert body.nbytes == 4
    assert store.writes == []


@pytest.mark.asyncio
async def test_streaming_write_hashes_and_bounds_in_one_pass() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=8, max_artifacts=2)
    iterations = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal iterations
        for chunk in (b"abc", b"def"):
            iterations += 1
            yield chunk

    artifact = await manager.write_stream(
        context(),
        artifact_id="answer.txt",
        ordinal=0,
        chunks=chunks(),
        media_type="text/plain",
        trust="model",
    )

    assert iterations == 2
    assert store.stream_writes == [artifact.key]
    assert await store.read(artifact.key) == b"abcdef"
    assert artifact.digest == hashlib.sha256(b"abcdef").hexdigest()


@pytest.mark.asyncio
async def test_stream_refuses_before_forwarding_the_over_limit_chunk_and_erase_is_exact() -> None:
    class CountingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.forwarded: list[bytes] = []

        async def write_stream(
            self,
            key: str,
            chunks: AsyncIterable[bytes | bytearray | memoryview],
            *,
            content_type: str | None = None,
        ) -> ObjectStat:
            async for chunk in chunks:
                self.forwarded.append(bytes(chunk))
            return ObjectStat(key, sum(map(len, self.forwarded)), "etag")

    store = CountingStore()
    manager = AgentArtifactManager(store, max_bytes=4, max_artifacts=1)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"123"
        yield b"45"

    with pytest.raises(ArtifactLimitExceeded, match="byte ceiling"):
        await manager.write_stream(context(), artifact_id="answer", ordinal=0, chunks=chunks())
    assert store.forwarded == [b"123"]

    key = manager.key(context(), "answer", ordinal=0)
    await manager.erase(context(), "answer", ordinal=0)
    assert store.deleted == [key]


@pytest.mark.asyncio
async def test_stream_refuses_invalid_metadata_and_chunk_before_storage() -> None:
    store = Store()
    manager = AgentArtifactManager(store, max_bytes=8, max_artifacts=2)

    async def invalid_chunk() -> AsyncIterator[Any]:
        yield "not bytes"

    with pytest.raises(ValueError, match="trust label"):
        await manager.write_stream(
            context(),
            artifact_id="bad",
            ordinal=0,
            chunks=invalid_chunk(),
            trust=cast(Any, "unknown"),
        )
    with pytest.raises(TypeError, match="chunks must be bytes"):
        await manager.write_stream(context(), artifact_id="bad", ordinal=0, chunks=invalid_chunk())
    with pytest.raises(ValueError, match="media_type"):
        await manager.write_stream(
            context(),
            artifact_id="bad",
            ordinal=0,
            chunks=invalid_chunk(),
            media_type=cast(Any, 7),
        )

    assert store.stream_writes == [manager.key(context(), "bad", ordinal=0)]


def test_artifact_configuration_and_scope_refuse_invalid_facts() -> None:
    store = Store()
    with pytest.raises(ValueError, match="max_bytes"):
        AgentArtifactManager(store, max_bytes=0, max_artifacts=1)
    with pytest.raises(ValueError, match="max_artifacts"):
        AgentArtifactManager(store, max_bytes=1, max_artifacts=0)
    with pytest.raises(TypeError, match="max_bytes"):
        AgentArtifactManager(store, max_bytes=True, max_artifacts=1)
    with pytest.raises(TypeError, match="max_artifacts"):
        AgentArtifactManager(store, max_bytes=1, max_artifacts=cast(Any, 1.5))
    manager = AgentArtifactManager(store, max_bytes=8, max_artifacts=2)

    invalid = (
        (context(tenant=""), "artifact tenant"),
        (context(conversation=""), "artifact conversation"),
    )
    for bad_context, message in invalid:
        with pytest.raises(ValueError, match=message):
            manager.key(bad_context, "artifact", ordinal=0)
    with pytest.raises(ValueError, match="artifact_id"):
        manager.key(context(), "", ordinal=0)
    with pytest.raises(ArtifactLimitExceeded, match="count ceiling"):
        manager.key(context(), "artifact", ordinal=-1)
    with pytest.raises(TypeError, match="ordinal"):
        manager.key(context(), "artifact", ordinal=True)


def test_artifact_identity_accepts_public_shapes_and_refuses_ambiguous_values() -> None:
    manager = AgentArtifactManager(Store(), max_bytes=8, max_artifacts=2)
    raw = SimpleNamespace(tenant="tenant-a", principal="user-7", conversation="conversation-2")
    subject = SimpleNamespace(
        tenant="tenant-a",
        principal=SimpleNamespace(subject="user-7"),
        conversation="conversation-2",
    )
    fallback = SimpleNamespace(
        tenant="tenant-a",
        principal=SimpleNamespace(id=7, subject="user-7"),
        conversation="conversation-2",
    )
    empty_fallback = SimpleNamespace(
        tenant="tenant-a",
        principal=SimpleNamespace(id="", subject="user-7"),
        conversation="conversation-2",
    )

    assert manager.key(raw, "artifact", ordinal=0) == manager.key(subject, "artifact", ordinal=0)
    assert manager.key(raw, "artifact", ordinal=0) == manager.key(fallback, "artifact", ordinal=0)
    assert manager.key(raw, "artifact", ordinal=0) == manager.key(
        empty_fallback, "artifact", ordinal=0
    )
    for bad in (
        SimpleNamespace(tenant=7, principal="user-7", conversation="conversation-2"),
        SimpleNamespace(tenant="tenant-a", principal="", conversation="conversation-2"),
        SimpleNamespace(tenant="tenant-a", principal=object(), conversation="conversation-2"),
        SimpleNamespace(tenant="tenant-a", principal="user-7", conversation=7),
    ):
        with pytest.raises(ValueError, match="tenant|principal|conversation"):
            manager.key(bad, "artifact", ordinal=0)
    with pytest.raises(ValueError, match="artifact_id"):
        manager.key(raw, cast(Any, 7), ordinal=0)
