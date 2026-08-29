from __future__ import annotations

import asyncio

import pytest

from wreath import objects
from wreath.objects import (
    LocalObjectStore,
    MemoryObjectStore,
    MemoryUploadStore,
    ObjectStat,
    ResumableUploads,
    S3ObjectStore,
    UploadState,
)

CONTENT = b"nine bytes, then some more: " + bytes(range(256))


def _run(coro):
    return asyncio.run(coro)


def _buffers():
    """The same content, in each accepted spelling."""
    return [
        ("bytes", bytes(CONTENT)),
        ("bytearray", bytearray(CONTENT)),
        ("memoryview", memoryview(bytearray(CONTENT))),
    ]


def test_objects_memory_store_accepts_every_buffer_type():
    async def go():
        store = MemoryObjectStore()
        for name, buffer in _buffers():
            stat = await store.write(f"k/{name}.bin", buffer)
            assert stat.size == len(CONTENT)
            assert await store.read(f"k/{name}.bin") == CONTENT

    _run(go())


def test_objects_local_store_accepts_every_buffer_type(tmp_path):
    async def go():
        store = LocalObjectStore(tmp_path)
        for name, buffer in _buffers():
            stat = await store.write(f"k/{name}.bin", buffer)
            assert stat.size == len(CONTENT)
            assert await store.read(f"k/{name}.bin") == CONTENT

    _run(go())


class _FakeResponse:
    def __init__(self, status, headers=()):
        self.status = status
        self.headers = tuple(headers)
        self.body = b""

    def header(self, name):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


class _FakeClient:
    """Copies the body on the way out, exactly as `HTTPClient.request` does."""

    def __init__(self):
        self.bodies: list[bytes] = []

    async def request(self, method, target, *, headers=(), body=b""):
        self.bodies.append(bytes(body))
        return _FakeResponse(200, [(b"etag", b'"abc"')])


def _s3(client):
    return S3ObjectStore(
        client,
        bucket="b",
        region="us-east-1",
        access_key="AKIAEXAMPLE",
        secret_key="secretkey",
        host="b.s3.us-east-1.amazonaws.com",
    )


def test_objects_s3_store_accepts_every_buffer_type():
    async def go():
        client = _FakeClient()
        store = _s3(client)
        for name, buffer in _buffers():
            stat = await store.write(f"k/{name}.bin", buffer, content_type="text/plain")
            assert stat.size == len(CONTENT)
        assert client.bodies == [CONTENT, CONTENT, CONTENT]

    _run(go())


def test_objects_s3_signs_a_bytearray_body_exactly_as_it_signs_bytes():

    async def go():
        signatures = []
        for _, buffer in _buffers():
            client = _FakeClient()
            captured: list[dict[str, str]] = []

            async def request(method, target, *, headers=(), body=b"", _c=captured):
                _c.append({k.decode().lower(): v.decode("latin-1") for k, v in headers})
                return _FakeResponse(200, [(b"etag", b'"abc"')])

            client.request = request  # type: ignore[method-assign]
            await _s3(client).write("k/x.bin", buffer)
            signatures.append(captured[0]["x-amz-content-sha256"])
        assert len(set(signatures)) == 1

    _run(go())


def test_objects_memory_store_does_not_retain_the_callers_buffer():
    async def go():
        store = MemoryObjectStore()
        buffer = bytearray(CONTENT)
        await store.write("k/a.bin", buffer)
        buffer[0:4] = b"XXXX"
        assert await store.read("k/a.bin") == CONTENT

    _run(go())


def test_objects_local_store_does_not_retain_the_callers_buffer(tmp_path):
    async def go():
        store = LocalObjectStore(tmp_path)
        buffer = bytearray(CONTENT)
        await store.write("k/a.bin", buffer)
        buffer[0:4] = b"XXXX"
        assert await store.read("k/a.bin") == CONTENT

    _run(go())


def test_objects_s3_store_does_not_retain_the_callers_buffer():
    async def go():
        client = _FakeClient()
        buffer = bytearray(CONTENT)
        await _s3(client).write("k/a.bin", buffer)
        buffer[0:4] = b"XXXX"
        assert client.bodies == [CONTENT]

    _run(go())


def test_objects_memory_write_stream_does_not_alias_its_own_buffer():

    async def go():
        store = MemoryObjectStore()

        async def chunks():
            yield CONTENT[:10]
            yield CONTENT[10:]

        await store.write_stream("k/a.bin", chunks())
        assert await store.read("k/a.bin") == CONTENT

        # Two independent writes must not share storage.
        async def other():
            yield b"different"

        await store.write_stream("k/b.bin", other())
        assert await store.read("k/a.bin") == CONTENT
        assert await store.read("k/b.bin") == b"different"

    _run(go())


class _RecordingStore:
    """Satisfies `ObjectStore`; records the *type* of each buffer it is given."""

    def __init__(self):
        self.written: dict[str, bytes] = {}
        self.kinds: list[str] = []

    async def read(self, key):
        return self.written[key]

    async def write(self, key, data, *, content_type=None):
        self.kinds.append(type(data).__name__)
        self.written[key] = bytes(data)
        return ObjectStat(key, len(data), "e", None, content_type)

    async def write_stream(self, key, chunks, *, content_type=None):
        buf = bytearray()
        async for chunk in chunks:
            buf += chunk
        self.written[key] = bytes(buf)
        return ObjectStat(key, len(buf), "e", None, content_type)

    async def stat(self, key):
        return ObjectStat(key, len(self.written[key]), "e", None, None)

    async def exists(self, key):
        return key in self.written

    async def list(self, prefix="", *, delimiter=None):
        for key in sorted(self.written):
            if key.startswith(prefix):
                yield ObjectStat(key, len(self.written[key]), "e", None, None)

    def read_stream(self, key, *, range=None):
        async def _one():
            yield self.written[key]

        return _one()

    async def delete(self, key):
        self.written.pop(key, None)


class _Request:
    """The only thing the append path asks of a request."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def test_objects_append_hands_the_backend_the_unfrozen_buffer():

    async def go():
        store = _RecordingStore()
        uploads = ResumableUploads(store, uploads=MemoryUploadStore())
        state = UploadState(id="u1", key="objects/one.bin")
        await uploads._uploads.create(state)
        await uploads._consume_locked(
            _Request([CONTENT[:10], CONTENT[10:]]), state, complete=False, first=True
        )
        # The record write is the upload's JSON; the part write is the payload.
        assert "bytearray" in store.kinds
        part = next(k for k in store.written if k.endswith(".part"))
        assert store.written[part] == CONTENT
        assert state.offset == len(CONTENT)

    _run(go())


def test_objects_append_still_sniffs_and_refuses_on_the_first_chunk():
    png = b"\x89PNG\r\n\x1a\n" + bytes(64)

    async def go():
        store = _RecordingStore()
        uploads = ResumableUploads(store, uploads=MemoryUploadStore())
        state = UploadState(id="u2", key="objects/two.bin", content_type="text/plain")
        await uploads._uploads.create(state)
        with pytest.raises(objects._Refused):
            await uploads._consume_locked(_Request([png]), state, complete=False, first=True)

    _run(go())
