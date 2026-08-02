"""Upload state stores, assembly backends, admission, and the sweeper.

`test_uploads_protocol.py` drives the wire; this drives the pieces underneath
it — where in-progress state lives, how each backend assembles parts, what is
refused before the first byte, and what reclaims an upload nobody finished.
"""

from __future__ import annotations

import asyncio

import pytest

from wreath import Wreath
from wreath.objects import (
    PARTIAL_UPLOAD,
    MemoryObjectStore,
    MemoryUploadStore,
    ObjectError,
    ObjectUploadStore,
    ResumableUploads,
    S3ObjectStore,
    UploadLimits,
    UploadState,
    resumable,
    sniff_content_type,
)
from wreath.testing import TestClient

PART = {"content-type": PARTIAL_UPLOAD}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _header(response, name: str) -> str | None:
    for key, value in response.headers:
        if key.lower() == name.encode():
            return value.decode()
    return None


def _location(response) -> str:
    location = _header(response, "location")
    if location is None:
        raise AssertionError("no Location header")
    return location


def _state(**kw) -> UploadState:
    base = {"id": "abc", "key": "uploads/abc", "created": 1.0, "updated": 1.0}
    return UploadState(**{**base, **kw})


# --- the conditional advance ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", ["memory", "object"])
async def test_advance_refuses_when_the_offset_moved_underneath(factory: str) -> None:
    """The whole point of `advance` being conditional rather than a write.

    Two appends racing on one upload both read offset 0. The first wins; the
    second must lose, or it rewinds the offset and the next append lands in a
    hole.
    """
    objects = MemoryObjectStore()
    store = MemoryUploadStore() if factory == "memory" else ObjectUploadStore(objects)
    await store.create(_state())

    first = _state(offset=8)
    second = _state(offset=4)

    assert await store.advance(first, expected=0) is True
    assert await store.advance(second, expected=0) is False

    held = await store.read("abc")
    assert held is not None
    assert held.offset == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", ["memory", "object"])
async def test_a_store_hands_out_independent_copies(factory: str) -> None:
    """Mutating what `read` returned must not land in the store early.

    Sharing the instance made the conditional advance compare the new offset
    against itself, so every append was refused with 409 — a store that looks
    correct in isolation and rejects everything in use.
    """
    objects = MemoryObjectStore()
    store = MemoryUploadStore() if factory == "memory" else ObjectUploadStore(objects)
    await store.create(_state())

    held = await store.read("abc")
    held.offset = 999
    held.backend["parts"] = 7

    fresh = await store.read("abc")
    assert fresh.offset == 0
    assert fresh.backend == {}


@pytest.mark.asyncio
async def test_creating_the_same_upload_twice_is_refused() -> None:
    store = MemoryUploadStore()
    await store.create(_state())
    with pytest.raises(ObjectError):
        await store.create(_state())


@pytest.mark.asyncio
async def test_object_store_records_survive_a_second_worker() -> None:
    """The reason `ObjectUploadStore` exists: resume on a worker that did not create."""
    objects = MemoryObjectStore()
    worker_a = resumable(objects, uploads=ObjectUploadStore(objects))
    worker_b = resumable(objects, uploads=ObjectUploadStore(objects))

    app_a, app_b = Wreath(), Wreath()
    app_a.include_router(worker_a.router("/uploads"))
    app_b.include_router(worker_b.router("/uploads"))

    async with TestClient(app_a) as a, TestClient(app_b) as b:
        created = await a.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"half"
        )
        location = _location(created)

        resumed = await b.head(location)
        assert resumed.status == 204
        assert _header(resumed, "upload-offset") == "4"

        finished = await b.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"rest",
        )
        assert finished.status == 204

    keys = [s.key async for s in objects.list(prefix="uploads/")]
    assert await objects.read(keys[0]) == b"halfrest"


@pytest.mark.asyncio
async def test_memory_store_fails_closed_on_a_second_worker() -> None:
    """Documented limitation, asserted so it stays a 404 and not a corruption."""
    objects = MemoryObjectStore()
    app_a, app_b = Wreath(), Wreath()
    app_a.include_router(resumable(objects).router("/uploads"))
    app_b.include_router(resumable(objects).router("/uploads"))

    async with TestClient(app_a) as a, TestClient(app_b) as b:
        created = await a.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"half"
        )
        assert (await b.head(_location(created))).status == 404


# --- the in-flight guard --------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_append_in_flight_is_refused_not_interleaved() -> None:
    objects = MemoryObjectStore()
    uploads = resumable(objects)
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        created = await client.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"ab"
        )
        location = _location(created)
        upload_id = location.rsplit("/", 1)[-1]

        uploads._inflight.add(upload_id)
        try:
            response = await client.patch(
                location,
                headers={**PART, "upload-offset": "2", "upload-complete": "?1"},
                content=b"cd",
            )
        finally:
            uploads._inflight.discard(upload_id)

        assert response.status == 409
        assert _header(response, "upload-offset") == "2"


@pytest.mark.asyncio
async def test_concurrent_appends_do_not_both_land() -> None:
    """Two real requests at once: one 204, one 409, and the object is intact."""
    objects = MemoryObjectStore()
    uploads = resumable(objects)
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        both = await asyncio.gather(
            client.patch(
                location,
                headers={**PART, "upload-offset": "0", "upload-complete": "?0"},
                content=b"AAAA",
            ),
            client.patch(
                location,
                headers={**PART, "upload-offset": "0", "upload-complete": "?0"},
                content=b"BBBB",
            ),
        )

    statuses = sorted(response.status for response in both)
    assert statuses == [204, 409]


# --- admission ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_quota_predicate_refuses_before_the_bytes() -> None:
    """The seam a metering subsystem fills, checked at the only useful moment."""
    seen: list[int | None] = []

    async def quota(request, declared):
        seen.append(declared)
        return False

    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(objects, quota=quota).router("/uploads"))

    async with TestClient(app) as client:
        response = await client.post(
            "/uploads", headers={"upload-complete": "?0", "upload-length": "4096"}
        )

    assert response.status == 413
    assert seen == [4096]
    assert [s.key async for s in objects.list()] == []


@pytest.mark.asyncio
async def test_key_for_chooses_the_final_object_key() -> None:
    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(
        resumable(objects, key_for=lambda request, upload_id: "fixed/name.bin")
        .router("/uploads")
    )

    async with TestClient(app) as client:
        await client.post(
            "/uploads", headers={"upload-complete": "?1"}, content=b"payload"
        )

    assert await objects.read("fixed/name.bin") == b"payload"


def test_on_complete_without_a_runner_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="durable job"):
        ResumableUploads(MemoryObjectStore(), on_complete="process")


def test_a_non_positive_expiry_is_refused() -> None:
    with pytest.raises(ValueError, match="expire"):
        ResumableUploads(MemoryObjectStore(), expire=0)


@pytest.mark.asyncio
async def test_completion_enqueues_one_durable_job_keyed_by_the_upload() -> None:
    calls: list[tuple] = []

    class FakeRunner:
        async def enqueue(self, task, *args, **kw):
            calls.append((task, args, kw.get("key")))
            return 1

    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(
        resumable(objects, jobs=FakeRunner(), on_complete="process").router("/uploads")
    )

    async with TestClient(app) as client:
        created = await client.post(
            "/uploads", headers={"upload-complete": "?1"}, content=b"done"
        )
        upload_id = _location(created).rsplit("/", 1)[-1]

    assert len(calls) == 1
    task, args, key = calls[0]
    assert task == "process"
    assert args[0] == upload_id
    assert key == f"upload:{upload_id}"


@pytest.mark.asyncio
async def test_an_incomplete_upload_enqueues_nothing() -> None:
    calls: list[tuple] = []

    class FakeRunner:
        async def enqueue(self, task, *args, **kw):
            calls.append((task, args))
            return 1

    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(
        resumable(objects, jobs=FakeRunner(), on_complete="process").router("/uploads")
    )

    async with TestClient(app) as client:
        await client.post("/uploads", headers={"upload-complete": "?0"}, content=b"ab")

    assert calls == []


# --- sniffing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"GIF89a...", "image/gif"),
        (b"%PDF-1.7", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"<html><body>", "text/plain"),
        (b"<!DOCTYPE html>", "text/plain"),
        (b"RIFF....WEBP", None),
        (b"just some text", None),
        (b"", None),
    ],
)
def test_sniff_recognises_only_unambiguous_signatures(prefix: bytes, expected) -> None:
    assert sniff_content_type(prefix) == expected


@pytest.mark.asyncio
async def test_a_declared_type_the_bytes_contradict_is_refused() -> None:
    """The lie that matters: HTML served back from your origin as an image."""
    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(objects).router("/uploads"))

    async with TestClient(app) as client:
        response = await client.post(
            "/uploads",
            headers={"upload-complete": "?1", "content-type": "image/png"},
            content=b"<html><body>not a png</body></html>",
        )

    assert response.status == 415
    assert [s.key async for s in objects.list(prefix="uploads/")] == []


@pytest.mark.asyncio
async def test_the_sniffed_type_is_what_gets_stored() -> None:
    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(objects).router("/uploads"))

    async with TestClient(app) as client:
        await client.post(
            "/uploads", headers={"upload-complete": "?1"}, content=PNG
        )

    key = [s.key async for s in objects.list(prefix="uploads/")][0]
    assert (await objects.stat(key)).content_type == "image/png"


@pytest.mark.asyncio
async def test_sniffing_can_be_turned_off() -> None:
    objects = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(objects, sniff=False).router("/uploads"))

    async with TestClient(app) as client:
        response = await client.post(
            "/uploads",
            headers={"upload-complete": "?1", "content-type": "image/png"},
            content=b"<html>not a png</html>",
        )

    assert response.status == 201


# --- the sweeper ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_reclaims_an_abandoned_upload_and_its_parts() -> None:
    objects = MemoryObjectStore()
    uploads = resumable(objects, uploads=ObjectUploadStore(objects), expire=60.0)
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        created = await client.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"abandoned"
        )
        location = _location(created)

        staged = [s.key async for s in objects.list(prefix=".uploads/")]
        assert any(key.endswith(".part") for key in staged)

        # Nothing is expired yet.
        assert await uploads.sweep() == 0

        # An hour later it is.
        assert await uploads.sweep(now=_much_later()) == 1
        assert uploads.swept_uploads == 1
        assert (await client.head(location)).status == 404

    assert [s.key async for s in objects.list(prefix=".uploads/")] == []


def _much_later() -> float:
    import time

    return time.time() + 3600.0


@pytest.mark.asyncio
async def test_sweep_counts_an_abort_it_could_not_finish() -> None:
    """A sweeper that gives up silently leaves a bill nobody sees."""

    class BrokenStore(MemoryObjectStore):
        async def delete(self, key: str) -> None:
            raise ObjectError("bucket unavailable")

    objects = BrokenStore()
    uploads = resumable(objects, uploads=ObjectUploadStore(objects), expire=1.0)
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        await client.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"abandoned"
        )

    assert await uploads.sweep(now=_much_later()) == 0
    assert uploads.aborted_uploads == 1


@pytest.mark.asyncio
async def test_refused_appends_are_counted() -> None:
    objects = MemoryObjectStore()
    uploads = resumable(objects)
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        await client.patch(
            location,
            headers={**PART, "upload-offset": "42", "upload-complete": "?1"},
            content=b"x",
        )

    assert uploads.refused_appends == 1


# --- the S3 assembly backend ---------------------------------------------


class FakeResp:
    def __init__(self, status: int, headers=(), body: bytes = b"") -> None:
        self.status = status
        self.headers = tuple(headers)
        self.body = body

    def header(self, name: bytes) -> bytes | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


class FakeClient:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, bytes]] = []

    async def request(self, method, target, *, headers=(), body=b""):
        self.calls.append((method, target, bytes(body)))
        return self._handler(method, target, bytes(body))


def _s3(handler, **kw) -> tuple[S3ObjectStore, FakeClient]:
    client = FakeClient(handler)
    store = S3ObjectStore(
        client, bucket="b", region="us-east-1", access_key="AKIAEXAMPLE",
        secret_key="secretkey", host="b.s3.us-east-1.amazonaws.com", **kw,
    )
    return store, client


_INITIATE = (
    b'<?xml version="1.0"?><InitiateMultipartUploadResult>'
    b"<UploadId>UP1</UploadId></InitiateMultipartUploadResult>"
)


def _s3_handler(method: str, target: str, body: bytes) -> FakeResp:
    if method == "POST" and "uploads=" in target and "uploadId" not in target:
        return FakeResp(200, body=_INITIATE)
    if method == "PUT" and "partNumber" in target:
        return FakeResp(200, [(b"etag", b'"p"')])
    if method == "PUT":
        return FakeResp(200, [(b"etag", b'"whole"')])
    if method == "POST" and "uploadId" in target:
        return FakeResp(200, body=b"<CompleteMultipartUploadResult/>")
    if method == "DELETE":
        return FakeResp(204)
    if method == "HEAD":
        return FakeResp(200, [(b"content-length", b"16"), (b"etag", b'"final"')])
    return FakeResp(400)


def _floor(store) -> int:
    """What a non-final append must reach on this store.

    S3 refuses a multipart part under 5 MiB and `S3ObjectStore`'s own default
    part size is larger still, so the number is read off the store rather than
    restated here -- a hard-coded 5 MiB passed the floor check by accident and
    then failed at 8.
    """
    return store._part_size


def test_the_s3_backend_advertises_its_own_part_floor() -> None:
    """5 MiB is S3's rule, so it is advertised rather than hidden."""
    store, _ = _s3(_s3_handler, part_size=8 * 1024 * 1024)
    uploads = resumable(store)
    assert uploads.limits.min_append_size == store._part_size


def test_a_declared_floor_cannot_go_below_the_backend_floor() -> None:
    store, _ = _s3(_s3_handler)
    uploads = resumable(store, limits=UploadLimits(min_append_size=1))
    assert uploads.limits.min_append_size == store._part_size


@pytest.mark.asyncio
async def test_s3_assembly_uses_multipart_and_transfers_nothing_at_completion() -> None:
    store, client = _s3(_s3_handler)
    app = Wreath()
    app.include_router(resumable(store).router("/uploads"))
    first, last = b"A" * _floor(store), b"tail"

    async with TestClient(app) as http:
        created = await http.post(
            "/uploads", headers={"upload-complete": "?0"}, content=first
        )
        location = _location(created)
        finished = await http.patch(
            location,
            headers={**PART, "upload-offset": str(_floor(store)), "upload-complete": "?1"},
            content=last,
        )
        assert finished.status == 204

    methods = [(method, "partNumber" in target) for method, target, _ in client.calls]
    assert ("POST", False) in methods            # initiate
    assert methods.count(("PUT", True)) == 2     # one part per append
    parts = [body for _, target, body in client.calls if "partNumber" in target]
    assert parts == [first, last]
    # Completion is one call carrying only the part manifest: no bytes move.
    completes = [
        body for method, target, body in client.calls
        if method == "POST" and "uploadId" in target
    ]
    assert len(completes) == 1
    assert len(completes[0]) < 512


@pytest.mark.asyncio
async def test_a_short_non_final_append_is_refused_by_the_s3_floor() -> None:
    """The advertised floor is enforced, not merely advertised."""
    store, client = _s3(_s3_handler)
    app = Wreath()
    app.include_router(resumable(store).router("/uploads"))

    async with TestClient(app) as http:
        response = await http.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"far too small"
        )

    assert response.status == 400
    assert client.calls == []   # refused before a byte reached the bucket


@pytest.mark.asyncio
async def test_cancelling_an_s3_upload_aborts_the_multipart() -> None:
    store, client = _s3(_s3_handler)
    app = Wreath()
    app.include_router(resumable(store).router("/uploads"))

    async with TestClient(app) as http:
        created = await http.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"A" * _floor(store)
        )
        assert (await http.delete(_location(created))).status == 204

    aborts = [
        target for method, target, _ in client.calls
        if method == "DELETE" and "uploadId" in target
    ]
    assert len(aborts) == 1
    assert store.orphaned_uploads == 0


@pytest.mark.asyncio
async def test_an_empty_s3_upload_is_a_plain_put() -> None:
    """S3 refuses a completion with no parts, so zero bytes is a `PUT`."""
    store, client = _s3(_s3_handler)
    app = Wreath()
    app.include_router(resumable(store).router("/uploads"))

    async with TestClient(app) as http:
        response = await http.post("/uploads", headers={"upload-complete": "?1"})
        assert response.status == 201

    assert not any("uploads=" in target for _, target, _ in client.calls)
    assert any(method == "PUT" for method, _, _ in client.calls)
