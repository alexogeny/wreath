---
description: Store bounded objects and resume interrupted uploads from the accepted offset.
keywords: guide object storage resumable upload S3 local memory limits
---

# Objects and uploads

An object store owns bytes and metadata. The resumable router owns incomplete-upload
state and offset checks. Mount the same protocol over memory in tests and durable
storage in production.

```python title="app.py"
from wreath import Wreath
from wreath.objects import MemoryObjectStore, UploadLimits, resumable

objects = MemoryObjectStore()
uploads = resumable(
    objects,
    limits=UploadLimits(
        max_size=512 * 1024 * 1024,
        max_append_size=16 * 1024 * 1024,
        max_age=24 * 60 * 60,
    ),
)

app = Wreath()
app.include_router(uploads.router("/uploads", public=True))
```

`public=True` is an explicit choice for this self-contained example. In an
authenticated application, pass `permissions=("uploads::write",)` instead.

```python title="test_upload.py"
from wreath.objects import PARTIAL_UPLOAD
from wreath.testing import TestClient

from app import app, objects


def header(response, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def test_an_upload_resumes_from_the_server_accepted_offset() -> None:
    content = b"camera bundle" * 100
    cut = 417
    async with TestClient(app) as client:
        created = await client.post(
            "/uploads",
            headers={"upload-complete": "?0", "upload-length": str(len(content))},
            content=content[:cut],
        )
        location = header(created, "location")
        assert location is not None

        status = await client.head(location)
        offset = int(header(status, "upload-offset") or "-1")
        completed = await client.patch(
            location,
            headers={
                "content-type": PARTIAL_UPLOAD,
                "upload-offset": str(offset),
                "upload-complete": "?1",
            },
            content=content[offset:],
        )

    assert completed.status == 204
    stored = [item async for item in objects.list() if not item.key.startswith(".uploads/")]
    assert len(stored) == 1
    assert await objects.read(stored[0].key) == content
```

A stale offset receives `409` and the accepted `Upload-Offset`; it can never overwrite
bytes already committed. `LocalObjectStore` applies filesystem containment and
`S3ObjectStore` signs remote operations. See [object APIs](../reference/data.md) and
the [offline field story](../stories/field-operations.md).
