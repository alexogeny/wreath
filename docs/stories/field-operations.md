---
description: Build a field operations system with bounded sync, resumable uploads and artifact provenance.
keywords: offline intermittent network sync shapes resumable upload geospatial field operations provenance
---

```hero
eyebrow: Story 08 · the network is a changing capability
title: Assume the network will fail.
lede: Field teams collect assignments, observations and large media in places where connectivity is intermittent and authority changes with location and role.
signal: bounded sync shapes
signal: resumable uploads
signal: geospatial policy
signal: artifact provenance
action: Cut the connection -> #cut-the-connection
action: Browse storage and sync -> ../reference/index.md#data-and-analysis
```

## The scene

Inspectors, responders or environmental teams carry tablets through tunnels, remote
roads and damaged infrastructure. They need the current assignment, nearby assets and
their own collected evidence. A whole database mirror is neither necessary nor safe.

When connected, a team shares a live room. When disconnected, the server remains the
owner of which subset the device may synchronize and where a large upload may resume.

## Cut the connection

Start uploading a large inspection bundle and receiving assignment changes. Remove the
network halfway through. On reconnection, the upload continues from accepted bytes and
the sync subscription sends a fresh bounded snapshot. That snapshot is authoritative:
records no longer inside the device's shape disappear locally.

> The invariant: reconnection resumes bounded work; it never expands the data a device
> may read and never requires a completed part to happen again.

This story deliberately does not promise magical conflict-free editing. Conflict
semantics belong to the product. Resumability, bounded synchronization and provenance
are the reusable infrastructure underneath that decision.

## The system shape

```text
assignment policy ──> authorized sync shape ──> bounded snapshot
        │                                      │
        └── geospatial facts                   ├── live deltas
                                               └── reconnect

media ──> resumable upload ──> object store ──> provenance record
```

| Field requirement | Wreath surface | Owned boundary |
|---|---|---|
| only relevant records | `wreath.sync` | declared and bounded query shapes |
| place-sensitive access | `wreath.geospatial`, authorization regions | coordinate and precision policy |
| large interrupted media | `wreath.objects` | upload state, parts and limits |
| evidence integrity | `wreath.provenance`, `wreath.signatures` | digest and attestation |
| connected collaboration | `wreath.rooms`, `wreath.websocket` | ephemeral presence and live events |
| later escalation | `wreath.notifications`, `wreath.workflows` | durable follow-up after ingest |

## Build it in four acts

### 1. Declare a small shape

Start with “my open assignments,” including an explicit limit. Bind it to the current
principal. Refuse an unbounded shape at declaration time instead of discovering an
accidental full-table subscription in production.

### 2. Reconnect authoritatively

Deliver a snapshot and then deltas. Disconnect after several changes, reconnect, and
prove the fresh snapshot removes a row whose assignment or authorization changed.
Wreath does not claim durable delta resumption here; correctness comes from re-reading
the bounded shape.

### 3. Resume a large object

Create an upload with size and content constraints. Send several parts, interrupt it,
and continue. Finalize only when the declared object is complete; an abandoned upload
remains bounded and purgeable.

### 4. Attach trust to the artifact

Record who or which device submitted the object, its digest and relevant capture facts.
Move the operator outside the authorized region and show the next sync narrowing rather
than relying on the client to hide rows it already received.

## Implement resumable evidence upload

Mount the upload protocol over a bounded object store. `MemoryObjectStore` makes the
example self-contained; the same router accepts a durable store in deployment.

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

The example declares a public upload surface deliberately. A deployed field
application should normally pass its upload permission instead.

The client creates an upload, remembers only the returned `Location`, and asks that
resource for its accepted offset after reconnecting. A wrong offset is answered with
`409` and the real one; Wreath never guesses which bytes the client meant.

### Cut the connection in a test

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


async def test_an_interrupted_bundle_resumes_from_accepted_bytes() -> None:
    bundle = bytes(range(256)) * 4
    cut = 317
    async with TestClient(app) as client:
        created = await client.post(
            "/uploads",
            headers={"upload-complete": "?0", "upload-length": str(len(bundle))},
            content=bundle[:cut],
        )
        location = header(created, "location")
        assert created.status == 201
        assert location is not None

        resumed = await client.head(location)
        offset = int(header(resumed, "upload-offset") or "-1")
        assert offset == cut

        completed = await client.patch(
            location,
            headers={
                "content-type": PARTIAL_UPLOAD,
                "upload-offset": str(offset),
                "upload-complete": "?1",
            },
            content=bundle[offset:],
        )

    assert completed.status == 204
    stored = [item async for item in objects.list() if not item.key.startswith(".uploads/")]
    assert len(stored) == 1
    assert await objects.read(stored[0].key) == bundle


async def test_a_stale_offset_cannot_overwrite_accepted_bytes() -> None:
    async with TestClient(app) as client:
        created = await client.post(
            "/uploads",
            headers={"upload-complete": "?0", "upload-length": "8"},
            content=b"abcd",
        )
        location = header(created, "location")
        assert location is not None
        refused = await client.patch(
            location,
            headers={
                "content-type": PARTIAL_UPLOAD,
                "upload-offset": "0",
                "upload-complete": "?1",
            },
            content=b"WXYZ",
        )

    assert refused.status == 409
    assert header(refused, "upload-offset") == "4"
```

```bash
uv run wreath test -k upload
uv run wreath dev app:app
```

## Declare what a device may synchronize

A sync shape is an ordinary bounded ORM query rebuilt from the current principal on
every evaluation. If a row leaves that answer, the next delta contains a tombstone.
On reconnect, the new snapshot's key set is the wholesale version of the same rule.

```python title="assignments.py"
from wreath import Request
from wreath.exceptions import Conflict
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text
from wreath.sync import Sync, sync_stream

from app import app


class Assignment(Model, table="assignments"):
    id: Mapped[int] = column(Int64, primary_key=True)
    technician_id: Mapped[str] = column(Text)
    status: Mapped[str] = column(Text)
    summary: Mapped[str] = column(Text)


assignments = Sync(
    Assignment,
    max_rows=500,
    max_per_principal=2,
)
assignments.add_shape(
    "mine",
    lambda principal: (
        Assignment.select()
        .where(Assignment.technician_id == principal.id)
        .order_by(Assignment.id.desc())
        .limit(500)
    ),
    limit=500,
)


@app.get("/sync/assignments")
async def sync_assignments(request: Request):
    subscription = assignments.subscribe(request.identity, "mine")
    if subscription is None:
        raise Conflict("this account already has its maximum assignment streams")
    return sync_stream(subscription, session_for)
```

`session_for` opens a fresh ORM session for each evaluation; a long-lived stream must
not pin a database connection while it waits. Pass the message bus to `Sync(...)` when
writes on another API worker need to wake this one. Writes still go through ordinary
authorized routes—this surface is read-only, so the product retains explicit conflict
semantics.

## The larger idea

Offline-friendly architecture is not “cache everything.” It is a set of explicit
continuations: which query, which cursor, which upload and which authority. Wreath makes
those continuations normal application objects rather than special cases scattered
through a mobile API.

Return to [all seven stories](index.md), or [build the first application](../start/index.md).
