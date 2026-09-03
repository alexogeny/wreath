from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.objects import (
    _S3_MAX_PARTS,
    PARTIAL_UPLOAD,
    MemoryObjectStore,
    MemoryUploadStore,
    UploadLimits,
    UploadState,
    _S3UploadBackend,
    resumable,
    sniff_content_type,
)
from wreath.testing import TestClient

PART = {"content-type": PARTIAL_UPLOAD}


def _header(response, name: str) -> str | None:
    for key, value in response.headers:
        if key.lower() == name.encode():
            return value.decode()
    return None


def _names(response) -> set[str]:
    return {key.decode().lower() for key, _ in response.headers}


def _location(response) -> str:
    location = _header(response, "location")
    if location is None:
        raise AssertionError("no Location header")
    return location


def _mounted(**options):
    store = MemoryObjectStore()
    uploads = resumable(store, **options)
    app = Wreath()
    app.include_router(uploads.router("/uploads", public=True))
    return store, uploads, app


def test_an_unlimited_resource_advertises_no_limit_dictionary() -> None:
    assert UploadLimits().to_header() is None


def test_a_limit_dictionary_renders_only_what_is_set() -> None:
    assert UploadLimits(max_size=10).to_header() == b"max-size=10"


@pytest.mark.asyncio
async def test_ordinary_upload_resource_advertises_its_finite_default_size() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.post("/uploads", headers={"upload-complete": "?0"})
    limit = _header(response, "upload-limit")
    assert limit is not None
    assert "max-size=536870912" in limit
    assert "min-append-size" not in limit


@pytest.mark.asyncio
async def test_upload_length_is_absent_until_it_is_declared() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        undeclared = await client.post("/uploads", headers={"upload-complete": "?0"})
        assert "upload-length" not in _names(undeclared)

        declared = await client.post(
            "/uploads", headers={"upload-complete": "?0", "upload-length": "16"}
        )
        assert _header(declared, "upload-length") == "16"


@pytest.mark.asyncio
async def test_only_the_offset_response_forbids_caching() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        created = await client.post("/uploads", headers={"upload-complete": "?0"})
        assert "cache-control" not in _names(created)
        assert _header(await client.head(_location(created)), "cache-control") == "no-store"


def test_the_append_ceiling_takes_the_body_limit_when_no_ceiling_is_declared() -> None:
    _, uploads, _ = _mounted(max_append_bytes=4096)
    assert uploads.limits.max_append_size == 4096


def test_the_append_ceiling_keeps_the_smaller_declared_value() -> None:
    _, uploads, _ = _mounted(limits=UploadLimits(max_append_size=1024), max_append_bytes=4096)
    assert uploads.limits.max_append_size == 1024


def test_the_append_ceiling_is_the_declared_one_with_no_body_limit() -> None:
    _, uploads, _ = _mounted(limits=UploadLimits(max_append_size=1024))
    assert uploads.limits.max_append_size == 1024


def test_a_backend_with_no_floor_advertises_none() -> None:
    _, uploads, _ = _mounted()
    assert uploads.limits.min_append_size is None
    assert b"min-append-size" not in (uploads.limits.to_header() or b"")


@pytest.mark.asyncio
async def test_a_declared_length_inside_max_size_is_accepted() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_size=10))
    async with TestClient(app) as client:
        response = await client.post(
            "/uploads", headers={"upload-complete": "?0", "upload-length": "10"}
        )
    assert response.status == 201


@pytest.mark.asyncio
async def test_an_upload_with_no_declared_length_is_not_refused_by_max_size() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_size=10))
    async with TestClient(app) as client:
        response = await client.post("/uploads", headers={"upload-complete": "?0"})
    assert response.status == 201


@pytest.mark.asyncio
async def test_an_append_inside_the_ceiling_is_accepted() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_append_size=8))
    async with TestClient(app) as client:
        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": "?1"},
            content=b"12345678",
        )
    assert response.status == 204


@pytest.mark.asyncio
async def test_redeclaring_the_same_length_is_accepted() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post(
                "/uploads",
                headers={"upload-complete": "?0", "upload-length": "8"},
                content=b"abcd",
            )
        )
        response = await client.patch(
            location,
            headers={
                **PART,
                "upload-offset": "4",
                "upload-complete": "?1",
                "upload-length": "8",
            },
            content=b"efgh",
        )
    assert response.status == 204


@pytest.mark.asyncio
async def test_a_length_declared_first_on_an_append_is_adopted() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        adopted = await client.patch(
            location,
            headers={
                **PART,
                "upload-offset": "0",
                "upload-complete": "?0",
                "upload-length": "8",
            },
            content=b"abcd",
        )
        assert adopted.status == 204
        assert _header(adopted, "upload-length") == "8"

        overrun = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"far too much",
        )
        assert overrun.status == 413


@pytest.mark.asyncio
async def test_an_append_with_no_content_type_is_refused_not_crashed() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        response = await client.patch(
            location, headers={"upload-offset": "0", "upload-complete": "?1"}, content=b"a"
        )
    assert response.status == 415


@pytest.mark.asyncio
async def test_a_media_type_with_parameters_is_still_a_partial_upload() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        response = await client.patch(
            location,
            headers={
                "content-type": f"{PARTIAL_UPLOAD}; charset=binary",
                "upload-offset": "0",
                "upload-complete": "?1",
            },
            content=b"a",
        )
    assert response.status == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "1", "", "?2"])
async def test_an_append_needs_a_structured_upload_complete(value: str) -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": value},
            content=b"a",
        )
    assert response.status == 400


@pytest.mark.asyncio
async def test_a_creation_declaring_the_fragment_type_does_not_store_it() -> None:
    store, _, app = _mounted(sniff=False)
    async with TestClient(app) as client:
        await client.post(
            "/uploads",
            headers={"upload-complete": "?1", "content-type": PARTIAL_UPLOAD},
            content=b"payload",
        )

    key = [s.key async for s in store.list(prefix="uploads/")][0]
    assert (await store.stat(key)).content_type != PARTIAL_UPLOAD


@pytest.mark.asyncio
async def test_a_later_append_is_not_re_sniffed() -> None:
    store, _, app = _mounted()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"}, content=png)
        )
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": str(len(png)), "upload-complete": "?1"},
            content=b"<html>trailing bytes</html>",
        )
        assert response.status == 204

    key = [s.key async for s in store.list(prefix="uploads/")][0]
    assert (await store.stat(key)).content_type == "image/png"


@pytest.mark.asyncio
async def test_a_completed_record_that_outlived_its_assembly_refuses_more_bytes() -> None:
    store, uploads, app = _mounted()

    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"}, content=b"abcd")
        )
        upload_id = location.rsplit("/", 1)[-1]

        held = await uploads._uploads.read(upload_id)
        held.complete = True
        await uploads._uploads.advance(held, expected=4)

        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"efgh",
        )

    assert response.status == 409
    assert _header(response, "upload-offset") == "4"


def test_xml_is_recognised_as_text() -> None:
    assert sniff_content_type(b'<?xml version="1.0"?><svg/>') == "text/plain"
    assert sniff_content_type(b"<?xmlfoo") is None


@pytest.mark.asyncio
async def test_the_s3_part_ceiling_is_refused_rather_than_sent() -> None:

    class Store:
        _part_size = 8
        orphaned_uploads = 0

        def _obj_path(self, key: str) -> str:
            return f"/{key}"

        async def _initiate(self, path, content_type):
            return "UP1"

        async def _put_part(self, path, upload_id, num, data):
            raise AssertionError("must be refused before a part is sent")

    backend = _S3UploadBackend(Store())
    state = UploadState(
        id="abc",
        key="uploads/abc",
        backend={"upload_id": "UP1", "parts": [[n, "e"] for n in range(_S3_MAX_PARTS)]},
    )

    with pytest.raises(Exception, match="limited to"):
        await backend.append(state, b"one part too many")


@pytest.mark.asyncio
async def test_an_unrecognised_body_keeps_its_declared_type() -> None:
    store, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.post(
            "/uploads",
            headers={"upload-complete": "?1", "content-type": "text/csv"},
            content=b"llama,paddock\nfern,north\n",
        )
        assert response.status == 201

    key = [s.key async for s in store.list(prefix="uploads/")][0]
    assert (await store.stat(key)).content_type == "text/csv"


@pytest.mark.asyncio
async def test_a_declared_type_the_bytes_agree_with_is_accepted() -> None:
    store, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.post(
            "/uploads",
            headers={"upload-complete": "?1", "content-type": "image/png"},
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
        )
        assert response.status == 201

    key = [s.key async for s in store.list(prefix="uploads/")][0]
    assert (await store.stat(key)).content_type == "image/png"


@pytest.mark.asyncio
async def test_the_resource_can_be_mounted_at_the_root() -> None:
    store = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(store).router("/", public=True))

    async with TestClient(app) as client:
        response = await client.post("/", headers={"upload-complete": "?1"}, content=b"x")
        assert response.status == 201

    assert [s.key async for s in store.list(prefix="uploads/")] != []


@pytest.mark.asyncio
async def test_an_unknown_upload_is_not_counted_as_a_refused_append() -> None:
    _, uploads, app = _mounted()
    async with TestClient(app) as client:
        assert (await client.head("/uploads/nosuchupload")).status == 404
        assert uploads.refused_appends == 0

        location = _location(await client.post("/uploads", headers={"upload-complete": "?0"}))
        await client.patch(
            location,
            headers={**PART, "upload-offset": "9", "upload-complete": "?1"},
            content=b"x",
        )
        assert uploads.refused_appends == 1


@pytest.mark.asyncio
async def test_the_memory_store_expires_only_what_is_stale() -> None:
    import time

    store = MemoryUploadStore()
    now = time.time()
    await store.create(UploadState(id="fresh", key="uploads/fresh", updated=now))
    await store.create(UploadState(id="stale", key="uploads/stale", updated=now - 7200))

    stale = await store.expired(now - 3600)
    assert [s.id for s in stale] == ["stale"]


@pytest.mark.asyncio
async def test_losing_the_conditional_advance_refuses_rather_than_rewinds() -> None:
    class LosingStore(MemoryUploadStore):
        lose = False

        async def advance(self, state, *, expected):
            if self.lose:
                return False
            return await super().advance(state, expected=expected)

    store = MemoryObjectStore()
    states = LosingStore()
    uploads = resumable(store, uploads=states)
    app = Wreath()
    app.include_router(uploads.router("/uploads", public=True))

    async with TestClient(app) as client:
        created = await client.post("/uploads", headers={"upload-complete": "?0"})
        states.lose = True
        response = await client.patch(
            _location(created),
            headers={**PART, "upload-offset": "0", "upload-complete": "?0"},
            content=b"abcd",
        )

    assert response.status == 409
    assert _header(response, "upload-offset") == "0"
    assert [s.key async for s in store.list(prefix=".uploads/") if s.key.endswith(".part")] == []


@pytest.mark.asyncio
async def test_losing_an_s3_style_advance_aborts_the_new_multipart_upload() -> None:
    class LosingStore(MemoryUploadStore):
        lose = False

        async def advance(self, state, *, expected):
            if self.lose:
                return False
            return await super().advance(state, expected=expected)

    class MultipartStore(MemoryObjectStore):
        _part_size = 1

        def __init__(self) -> None:
            super().__init__()
            self.aborted = []

        def _resumable_backend(self, _prefix):
            return _S3UploadBackend(self)

        def _obj_path(self, key):
            return f"/{key}"

        async def _initiate(self, _path, _content_type):
            return "attempt-1"

        async def _put_part(self, _path, _upload_id, _number, _data):
            return '"etag"'

        async def _abort(self, path, upload_id):
            self.aborted.append((path, upload_id))

    objects = MultipartStore()
    states = LosingStore()
    uploads = resumable(objects, uploads=states)
    app = Wreath()
    app.include_router(uploads.router("/uploads", public=True))

    async with TestClient(app) as client:
        created = await client.post("/uploads", headers={"upload-complete": "?0"})
        upload_id = _location(created).rsplit("/", 1)[-1]
        states.lose = True
        response = await client.patch(
            _location(created),
            headers={**PART, "upload-offset": "0", "upload-complete": "?0"},
            content=b"abcd",
        )

    assert response.status == 409
    assert objects.aborted == [(f"/uploads/{upload_id}", "attempt-1")]
    assert await states.read(upload_id) is None


@pytest.mark.asyncio
async def test_completing_without_a_declared_length_reports_the_final_offset() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"}, content=b"abcd")
        )
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"efghij",
        )

    assert response.status == 204
    assert _header(response, "upload-offset") == "10"
    assert _header(response, "upload-length") == "10"
