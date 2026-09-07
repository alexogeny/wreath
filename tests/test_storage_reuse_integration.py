from typing import Any

import pytest

from wreath._asgi_state import ResponseCapture
from wreath.objects import MemoryObjectStore
from wreath.request import UploadedFile


@pytest.mark.parametrize("kind", ["http.response.body", "wreath.response"])
async def test_capture_normalizes_single_coerced_bytes_subclass(kind):
    class Body(bytes):
        pass

    class Source:
        def __bytes__(self):
            return Body(b"payload")

    capture = ResponseCapture(strict=False)
    await capture.send({"type": kind, "status": 200, "body": Source()})
    result = capture.body
    assert result == b"payload"
    assert type(result) is bytes


async def test_memory_stream_noncontiguous_view_keeps_type_error_and_old_value():
    store = MemoryObjectStore()
    await store.write("body", b"original")

    async def chunks():
        yield b"prefix"
        yield memoryview(b"abcdef")[::2]

    with pytest.raises(TypeError):
        await store.write_stream("body", chunks())
    assert await store.read("body") == b"original"


@pytest.mark.parametrize("instance_override", [False, True])
def test_upload_custom_chunks_retains_join_snapshot_timing(instance_override):
    def chunks():
        buffer = bytearray(b"first")
        yield buffer
        buffer[:] = b"other"
        yield buffer

    class Upload(UploadedFile):
        def chunks(self, size=65536):
            return chunks()

    upload: Any = Upload("file", "body.bin", [], spool=object())
    if instance_override:
        upload.chunks = chunks
    assert upload.read() == b"otherother"


def test_upload_custom_chunks_retains_iterator_failure_precedence():
    class Upload(UploadedFile):
        def chunks(self, size=65536):
            yield "invalid"
            raise LookupError("iterator failed")

    upload = Upload("file", "body.bin", [], spool=object())
    with pytest.raises(LookupError, match="iterator failed"):
        upload.read()


@pytest.mark.parametrize("conditional", [False, True])
async def test_memory_metadata_rechecks_nonexact_stored_bytes(conditional):
    class Body(bytes):
        reported_size = 3

        def __bytes__(self):
            return self

        def __len__(self):
            return self.reported_size

    body = Body(b"abcdef")
    store = MemoryObjectStore()
    if conditional:
        first = await store._upload_compare_and_swap("body", body, expected_etag=None)
    else:
        first = await store.write("body", body)
    assert first is not None and first.size == 3
    body.reported_size = 6
    assert await store.read("body") is body
    assert (await store.stat("body")).size == 6
    assert [value.size async for value in store.list()] == [6]
