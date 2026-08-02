"""The declared controls, one test per removable decision.

Written against `wreath mutant --changed HEAD --path src/wreath/objects.py`,
which removed each control in turn and reported the ones no test noticed. Every
case below started as a survivor: an advertised limit that was never asserted
absent, an accepted value that was only ever tested when it was refused, a
branch whose *other* side nothing exercised.

The recurring shape is worth naming, because it is the one this file exists to
fix: **a refusal test alone does not pin a comparison.** `declared > maximum`
survives a test that only sends an over-size length, because deleting the
comparison still refuses that request. What kills it is the request that must
be *accepted*.
"""

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
    app.include_router(uploads.router("/uploads"))
    return store, uploads, app


# --- the advertised limits ------------------------------------------------


def test_an_unlimited_resource_advertises_no_limit_dictionary() -> None:
    """`to_header()` must answer None, not an empty dictionary value."""
    assert UploadLimits().to_header() is None


def test_a_limit_dictionary_renders_only_what_is_set() -> None:
    assert UploadLimits(max_size=10).to_header() == b"max-size=10"


@pytest.mark.asyncio
async def test_upload_limit_is_absent_when_nothing_is_limited() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        response = await client.post("/uploads", headers={"upload-complete": "?0"})
    # `max-age` always applies, so the header is present; the assertion that
    # matters is that an unset limit contributes nothing to it.
    limit = _header(response, "upload-limit")
    assert "max-size" not in limit
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
    """`no-store` is for the offset read; putting it everywhere says nothing."""
    _, _, app = _mounted()
    async with TestClient(app) as client:
        created = await client.post("/uploads", headers={"upload-complete": "?0"})
        assert "cache-control" not in _names(created)
        assert _header(await client.head(_location(created)), "cache-control") == "no-store"


def test_the_append_ceiling_takes_the_body_limit_when_no_ceiling_is_declared() -> None:
    _, uploads, _ = _mounted(max_append_bytes=4096)
    assert uploads.limits.max_append_size == 4096


def test_the_append_ceiling_keeps_the_smaller_declared_value() -> None:
    _, uploads, _ = _mounted(
        limits=UploadLimits(max_append_size=1024), max_append_bytes=4096
    )
    assert uploads.limits.max_append_size == 1024


def test_the_append_ceiling_is_the_declared_one_with_no_body_limit() -> None:
    _, uploads, _ = _mounted(limits=UploadLimits(max_append_size=1024))
    assert uploads.limits.max_append_size == 1024


def test_a_backend_with_no_floor_advertises_none() -> None:
    """`floor or None` — a zero floor must not render as `min-append-size=0`."""
    _, uploads, _ = _mounted()
    assert uploads.limits.min_append_size is None
    assert b"min-append-size" not in (uploads.limits.to_header() or b"")


# --- comparisons need their accepted case --------------------------------


@pytest.mark.asyncio
async def test_a_declared_length_inside_max_size_is_accepted() -> None:
    """The other half of the max-size comparison."""
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
    """The other half of the max-append-size comparison."""
    _, _, app = _mounted(limits=UploadLimits(max_append_size=8))
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": "?1"},
            content=b"12345678",
        )
    assert response.status == 204


@pytest.mark.asyncio
async def test_redeclaring_the_same_length_is_accepted() -> None:
    """The other half of `declared != state.length`."""
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
    """`Upload-Length` may arrive later; once adopted it is enforced."""
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
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


# --- protocol hygiene on the append path ---------------------------------


@pytest.mark.asyncio
async def test_an_append_with_no_content_type_is_refused_not_crashed() -> None:
    """A missing header must reach the 415, not an AttributeError and a 500."""
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        response = await client.patch(
            location, headers={"upload-offset": "0", "upload-complete": "?1"}, content=b"a"
        )
    assert response.status == 415


@pytest.mark.asyncio
async def test_a_media_type_with_parameters_is_still_a_partial_upload() -> None:
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
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
    """Covered on creation already; the append branch is a separate control."""
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "0", "upload-complete": value},
            content=b"a",
        )
    assert response.status == 400


@pytest.mark.asyncio
async def test_a_creation_declaring_the_fragment_type_does_not_store_it() -> None:
    """`application/partial-upload` describes the fragment, never the object."""
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
    """Only the first bytes describe the representation.

    Dropping the `first` clause would judge an upload by a fragment from its
    middle, so a PNG whose second chunk happens to start `<html` would be
    refused as a lie about itself.
    """
    store, _, app = _mounted()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    async with TestClient(app) as client:
        location = _location(
            await client.post(
                "/uploads", headers={"upload-complete": "?0"}, content=png
            )
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
    """Reachable when `advance` won and `finish` then failed: the record stays
    complete, and a retry must not append to it."""
    store, uploads, app = _mounted()

    async with TestClient(app) as client:
        location = _location(
            await client.post(
                "/uploads", headers={"upload-complete": "?0"}, content=b"abcd"
            )
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


# --- the sniff table ------------------------------------------------------


def test_xml_is_recognised_as_text() -> None:
    """A separate clause from the two HTML spellings beside it."""
    assert sniff_content_type(b'<?xml version="1.0"?><svg/>') == "text/plain"
    assert sniff_content_type(b"<?xmlfoo") is None


# --- the S3 part ceiling --------------------------------------------------


@pytest.mark.asyncio
async def test_the_s3_part_ceiling_is_refused_rather_than_sent() -> None:
    """S3 refuses an upload past 10,000 parts; reaching it must say so.

    Driven against the backend directly: the bound is impractical to reach
    through the wire, and a control nothing can exercise is a control nobody
    knows works.
    """

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


# --- the last survivors ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_unrecognised_body_keeps_its_declared_type() -> None:
    """Sniffing refuses a *lie*, not an unknown.

    Without the `looks_like is None` return, every declared type over bytes the
    table does not recognise would be refused — which is most real uploads.
    """
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
    """The other half of `declared != looks_like`."""
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
    """`base or "/"` — an empty prefix must still register a path."""
    store = MemoryObjectStore()
    app = Wreath()
    app.include_router(resumable(store).router("/"))

    async with TestClient(app) as client:
        response = await client.post("/", headers={"upload-complete": "?1"}, content=b"x")
        assert response.status == 201

    assert [s.key async for s in store.list(prefix="uploads/")] != []


@pytest.mark.asyncio
async def test_an_unknown_upload_is_not_counted_as_a_refused_append() -> None:
    """A 404 is a client that lost its URL, not a protocol violation.

    Counting it would make `refused_appends` rise whenever an upload expired,
    which is the number people watch to spot a client sending bad offsets.
    """
    _, uploads, app = _mounted()
    async with TestClient(app) as client:
        assert (await client.head("/uploads/nosuchupload")).status == 404
        assert uploads.refused_appends == 0

        location = _location(
            await client.post("/uploads", headers={"upload-complete": "?0"})
        )
        await client.patch(
            location,
            headers={**PART, "upload-offset": "9", "upload-complete": "?1"},
            content=b"x",
        )
        assert uploads.refused_appends == 1


@pytest.mark.asyncio
async def test_the_memory_store_expires_only_what_is_stale() -> None:
    """`MemoryUploadStore.expired` was reached by no test at all."""
    import time

    store = MemoryUploadStore()
    now = time.time()
    await store.create(UploadState(id="fresh", key="uploads/fresh", updated=now))
    await store.create(UploadState(id="stale", key="uploads/stale", updated=now - 7200))

    stale = await store.expired(now - 3600)
    assert [s.id for s in stale] == ["stale"]


@pytest.mark.asyncio
async def test_losing_the_conditional_advance_refuses_rather_than_rewinds() -> None:
    """The 409 nothing reached: `advance` lost to a writer on another worker.

    The in-flight guard covers two appends *this* worker is serving, so the
    only way to this branch is a store that reports a lost race — which is what
    a sibling worker's committed append looks like from here.
    """
    class LosingStore(MemoryUploadStore):
        async def advance(self, state, *, expected):
            return False

    store = MemoryObjectStore()
    uploads = resumable(store, uploads=LosingStore())
    app = Wreath()
    app.include_router(uploads.router("/uploads"))

    async with TestClient(app) as client:
        created = await client.post(
            "/uploads", headers={"upload-complete": "?0"}, content=b"abcd"
        )

    assert created.status == 409
    assert _header(created, "upload-offset") == "0"
    # The bytes went to this attempt's own part key and are assembled into
    # nothing; the sweeper is what reclaims them.
    assert [s.key async for s in store.list(prefix="uploads/")] == []


@pytest.mark.asyncio
async def test_completing_without_a_declared_length_reports_the_final_offset() -> None:
    """`state.length = state.offset` on completion is observable, and must be.

    Without it the terminal response omits `Upload-Length`, so a client that
    completed an upload it never sized has no confirmation of how much the
    server accepted.
    """
    _, _, app = _mounted()
    async with TestClient(app) as client:
        location = _location(
            await client.post(
                "/uploads", headers={"upload-complete": "?0"}, content=b"abcd"
            )
        )
        response = await client.patch(
            location,
            headers={**PART, "upload-offset": "4", "upload-complete": "?1"},
            content=b"efghij",
        )

    assert response.status == 204
    assert _header(response, "upload-offset") == "10"
    assert _header(response, "upload-length") == "10"
