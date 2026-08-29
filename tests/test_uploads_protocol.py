from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.objects import (
    PARTIAL_UPLOAD,
    MemoryObjectStore,
    ResumableUploads,
    UploadLimits,
    resumable,
)
from wreath.testing import TestClient

PART = {"content-type": PARTIAL_UPLOAD}


def _app(uploads: ResumableUploads) -> Wreath:
    app = Wreath()
    app.include_router(uploads.router("/uploads"))
    return app


def _mounted(**options):
    store = MemoryObjectStore()
    uploads = resumable(store, **options)
    return store, uploads, _app(uploads)


async def _create(
    client,
    *,
    complete: bool = False,
    length: int | None = None,
    content: bytes = b"",
    content_type: str | None = None,
):
    headers = {"upload-complete": "?1" if complete else "?0"}
    if length is not None:
        headers["upload-length"] = str(length)
    if content_type is not None:
        headers["content-type"] = content_type
    return await client.post("/uploads", headers=headers, content=content)


def _location(response) -> str:
    for name, value in response.headers:
        if name.lower() == b"location":
            return value.decode()
    raise AssertionError("no Location header on the creation response")


def _header(response, name: str) -> str | None:
    for key, value in response.headers:
        if key.lower() == name.encode():
            return value.decode()
    return None


@pytest.mark.asyncio
async def test_single_shot_creation_stores_the_object() -> None:
    store, _, app = _mounted()
    async with TestClient(app) as client:
        response = await _create(client, complete=True, content=b"one round trip")
        assert response.status == 201
        assert _header(response, "upload-offset") == "14"
        assert _header(response, "upload-complete") == "?1"

    keys = [key async for key in _keys(store)]
    assert len(keys) == 1
    assert await store.read(keys[0]) == b"one round trip"
    # Completion reclaims its own staging; the sweeper is for what never
    # completes, not for the ordinary path.
    assert [key async for key in _staging(store)] == []


async def _keys(store):
    """Finished objects only: staging lives under a prefix nobody serves."""
    async for stat in store.list():
        if not stat.key.startswith(".uploads/"):
            yield stat.key


async def _staging(store):
    async for stat in store.list(prefix=".uploads/"):
        yield stat.key


@pytest.mark.asyncio
async def test_head_reports_offset_and_forbids_caching() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        created = await _create(client, content=b"abcd")
        response = await client.head(_location(created))

        assert response.status == 204
        assert _header(response, "upload-offset") == "4"
        assert _header(response, "upload-complete") == "?0"
        assert _header(response, "cache-control") == "no-store"


@pytest.mark.asyncio
async def test_delete_cancels_and_the_resource_is_gone() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        created = await _create(client, content=b"abcd")
        location = _location(created)

        assert (await client.delete(location)).status == 204
        assert (await client.head(location)).status == 404


@pytest.mark.asyncio
async def test_unknown_upload_is_404_not_409() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.head("/uploads/deadbeef")
        assert response.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [0, 4, 7, 9, 16], ids=lambda n: f"cut-at-{n}")
async def test_interrupted_upload_resumes_byte_identical(cut: int) -> None:
    payload = bytes(range(64)) * 4
    store, _, app = _mounted()

    async with TestClient(app) as client:
        created = await _create(client, length=len(payload), content=payload[:cut])
        location = _location(created)
        assert _header(created, "upload-offset") == str(cut)

        # The client "crashes" here and comes back knowing nothing but the URL.
        probe = await client.head(location)
        offset = int(_header(probe, "upload-offset") or "-1")
        assert offset == cut

        response = await client.patch(
            location,
            headers={**PART, "upload-offset": str(offset), "upload-complete": "?1"},
            content=payload[offset:],
        )
        assert response.status == 204
        assert _header(response, "upload-complete") == "?1"

    keys = [key async for key in _keys(store)]
    assert len(keys) == 1
    assert await store.read(keys[0]) == payload


@pytest.mark.asyncio
async def test_many_small_appends_assemble_in_order() -> None:
    payload = bytes(range(256))
    store, _, app = _mounted()

    async with TestClient(app) as client:
        created = await _create(client, length=len(payload))
        location = _location(created)
        offset = 0
        while offset < len(payload):
            piece = payload[offset : offset + 17]
            final = offset + len(piece) >= len(payload)
            response = await client.patch(
                location,
                headers={
                    **PART,
                    "upload-offset": str(offset),
                    "upload-complete": "?1" if final else "?0",
                },
                content=piece,
            )
            assert response.status == 204
            offset += len(piece)

    keys = [key async for key in _keys(store)]
    assert await store.read(keys[0]) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [0, 1, 3, 99])
async def test_a_mismatched_offset_is_refused_with_the_real_one(claimed: int) -> None:
    store, _, app = _mounted()
    async with TestClient(app) as client:
        created = await _create(client, content=b"abcd")  # offset is 4
        location = _location(created)

        response = await client.patch(
            location,
            headers={**PART, "upload-offset": str(claimed), "upload-complete": "?1"},
            content=b"XXXX",
        )
        assert response.status == 409
        assert _header(response, "upload-offset") == "4"
        # And the refusal wrote nothing.
        assert int(_header(await client.head(location), "upload-offset")) == 4

    assert [key async for key in _keys(store)] == []


@pytest.mark.asyncio
async def test_an_offset_claim_cannot_reach_into_another_upload() -> None:
    store, _, app = _mounted()
    async with TestClient(app) as client:
        _location(await _create(client, content=b"AAAAAAAA"))
        second = _location(await _create(client, content=b"BB"))

        # `second` is at 2; claiming `first`'s offset of 8 against it is refused.
        response = await client.patch(
            second,
            headers={**PART, "upload-offset": "8", "upload-complete": "?1"},
            content=b"CC",
        )
        assert response.status == 409
        assert _header(response, "upload-offset") == "2"

        finished = await client.patch(
            second,
            headers={**PART, "upload-offset": "2", "upload-complete": "?1"},
            content=b"CC",
        )
        assert finished.status == 204

    keys = sorted([key async for key in _keys(store)])
    assert len(keys) == 1
    assert await store.read(keys[0]) == b"BBCC"


@pytest.mark.asyncio
async def test_a_second_append_to_a_completed_upload_is_refused() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await _create(client, complete=True, content=b"done"))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"more",
        )
        # The record is gone once the object exists, so this is a 404 rather
        # than a 409 -- either way the bytes are not appended to a finished
        # object, which is the property under test.
        assert response.status in (404, 409)


@pytest.mark.asyncio
async def test_declared_length_over_max_size_is_refused_before_any_bytes() -> None:
    store, _, app = _mounted(limits=UploadLimits(max_size=10))
    async with TestClient(app) as client:
        response = await _create(client, length=11)
        assert response.status == 413

    assert [key async for key in _keys(store)] == []


@pytest.mark.asyncio
async def test_overrunning_the_declared_length_is_refused_on_append() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await _create(client, length=8, content=b"abcd"))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"far too much",
        )
        assert response.status == 413
        assert int(_header(await client.head(location), "upload-offset")) == 4


@pytest.mark.asyncio
async def test_overrunning_max_size_is_refused_even_without_a_declared_length() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_size=6))
    async with TestClient(app) as client:
        location = _location(await _create(client, content=b"abcd"))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"efgh",
        )
        assert response.status == 413


@pytest.mark.asyncio
async def test_completing_short_of_the_declared_length_is_refused() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        response = await _create(client, complete=True, length=99, content=b"abcd")
        assert response.status == 400


@pytest.mark.asyncio
async def test_a_short_non_final_append_is_refused_when_a_floor_is_advertised() -> None:
    _, uploads, app = _mounted(limits=UploadLimits(min_append_size=8))
    assert uploads.limits.min_append_size == 8

    async with TestClient(app) as client:
        location = _location(await _create(client))
        short = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": "?0"},
            content=b"tiny",
        )
        assert short.status == 400

        # The floor does not apply to the append that completes the upload.
        final = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": "?1"},
            content=b"tiny",
        )
        assert final.status == 204


@pytest.mark.asyncio
async def test_max_append_size_is_clamped_to_the_readable_body_size() -> None:
    _, uploads, _ = _mounted(limits=UploadLimits(max_append_size=1 << 30), max_append_bytes=4096)
    assert uploads.limits.max_append_size == 4096


@pytest.mark.asyncio
async def test_an_oversize_append_is_refused() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_append_size=4))
    async with TestClient(app) as client:
        location = _location(await _create(client))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": "?1"},
            content=b"more than four",
        )
        assert response.status == 413


@pytest.mark.asyncio
async def test_append_requires_the_partial_upload_media_type() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await _create(client))
        response = await client.patch(
            location,
            headers={
                "content-type": "application/octet-stream",
                "upload-offset": "0",
                "upload-complete": "?1",
            },
            content=b"abcd",
        )
        assert response.status == 415


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "1", "", "?2"])
async def test_a_non_structured_upload_complete_is_refused(value: str) -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.post("/uploads", headers={"upload-complete": value})
        assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["-1", "+1", " 1", "1.0", "abc"])
async def test_a_non_structured_upload_offset_is_refused(value: str) -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await _create(client))
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": value, "upload-complete": "?1"},
            content=b"a",
        )
        assert response.status == 400


@pytest.mark.asyncio
async def test_changing_the_declared_length_is_refused() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(await _create(client, length=8, content=b"abcd"))
        response = await client.patch(
            location,
            headers={
                **PART,
                "upload-offset": "4",
                "upload-complete": "?1",
                "upload-length": "9",
            },
            content=b"efgh",
        )
        assert response.status == 400
        # **The status alone proves nothing here.** Removing the refusal lets
        # the new length be adopted, and the completion check downstream then
        # answers 400 for a different reason -- so a test asserting only the
        # code passes whichever branch fired. A mutant survived on exactly that.
        assert b"must not change" in response.body


@pytest.mark.asyncio
async def test_the_advertised_limit_header_is_a_structured_dictionary() -> None:
    _, _, app = _mounted(limits=UploadLimits(max_size=100, min_append_size=8))
    async with TestClient(app) as client:
        created = await _create(client)
        limit = _header(created, "upload-limit")

    assert "max-size=100" in limit
    assert "min-append-size=8" in limit
    assert "max-age=" in limit
