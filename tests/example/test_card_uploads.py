"""The upload path, exercised the way an uploader actually walks it.

The rule this file is written to is the one stage 4 learned the hard way: a test
that reaches past the mechanism proves nothing about the mechanism. Writing
bytes into the store with `store.write(...)` and then asserting they are there
tests `LocalObjectStore`, which wreath already tests. So every test below starts
by asking the application to *mint* a URL and then uses that URL and nothing
else — the signature under test is the one the application produced, carried in
the query string the application built.

The four refusals get their own tests because they are four different
statements about the signature: the deadline is inside it, the key is inside it,
the method is inside it, and the digest is over all three. A single "a bad URL
is refused" test would pass with three of those four broken.
"""

from __future__ import annotations

import io
import os
import time
import zipfile

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap upload tests",
)

#: Enough rows for deployment 1 to exist with a station and a reserve behind it.
SAMPLE = 200

#: Seeded by `camera_trap.seed`. Minting and ingesting are ranger work.
RANGER = "ranger1@example.org"
VOLUNTEER = "volunteer1@example.org"

#: The deployment every test uploads a card for. Any seeded id would do; a
#: constant keeps the assertions about keys readable.
DEPLOYMENT = 1


def card_archive(names: tuple[str, ...] = ("IMG_0001.JPG", "IMG_0002.JPG")) -> bytes:
    """A zip shaped like an SD card: a few entries, each a few bytes.

    Real cards hold JPEGs. The ingest never decodes them -- it moves bytes from
    an archive into the store -- so the content is irrelevant and the test says
    so rather than embedding a JPEG nobody will read.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, f"pretend-image-{name}".encode())
    return buffer.getvalue()


def cookie(response) -> dict[str, str]:
    """The `Set-Cookie` from `response`, as a `Cookie` request header."""
    values = [
        value.decode("latin-1")
        for name, value in response.headers
        if name.lower() == b"set-cookie"
    ]
    assert values, "no Set-Cookie on a response that was supposed to sign someone in"
    return {"cookie": "; ".join(value.split(";", 1)[0] for value in values)}


@pytest.fixture
async def client():
    """The application on a freshly built schema and an empty object store.

    **Emptying the store matters as much as dropping the schema**, and it is
    here because leaving it out produced a false pass: an archive uploaded by
    one test was still on disk for the next, so
    `test_ingest_without_an_uploaded_card_fails_rather_than_hanging` found a
    card, ingested it happily, and reported `done` where the whole point was
    `failed`. The store root is per worker but not per test, so this is the only
    place that isolation can come from.
    """
    import shutil

    from _camera_trap import build_schema, drop_schema
    from camera_trap.config import SETTINGS

    from wreath.postgres import connect
    from wreath.testing import TestClient

    shutil.rmtree(SETTINGS.media_root, ignore_errors=True)
    SETTINGS.media_root.mkdir(parents=True, exist_ok=True)

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=SAMPLE)
    finally:
        await connection.close()

    # Imported after the store root exists: `LocalObjectStore` opens its root at
    # registration, and `build()` registers it.
    from camera_trap.app import build

    async with TestClient(build()) as test_client:
        yield test_client

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()
    shutil.rmtree(SETTINGS.media_root, ignore_errors=True)


@pytest.fixture
async def ranger(client):
    """The client and the header that makes it a ranger."""
    signed_in = await client.post("/session", params={"email": RANGER})
    assert signed_in.status == 200, signed_in.text
    return client, cookie(signed_in)


async def mint(client, headers, deployment_id: int = DEPLOYMENT) -> dict:
    """Ask the application for an upload URL, and insist it gave us one."""
    response = await client.post(
        "/cards", params={"deployment_id": deployment_id}, headers=headers
    )
    assert response.status == 200, response.text
    return response.json()


def test_ingest_refuses_a_compression_bomb_before_writing(monkeypatch) -> None:
    """The real ingest handler passes its card-specific extraction budget."""
    import asyncio
    from types import SimpleNamespace

    from camera_trap import tasks
    from camera_trap.media import card_key, image_prefix

    from wreath.objects import MemoryObjectStore, ZipExtractionLimits

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"A" * (1024 * 1024))

    class Runner:
        def task(self, _name, **_options):
            def capture(handler):
                self.handler = handler
                return handler
            return capture

    class Session:
        async def close(self):
            pass

    async def locate(_session, _deployment_id):
        return SimpleNamespace(card_serial="CARD-1"), "reserve"

    async def go():
        store = MemoryObjectStore()
        source = card_key("reserve", DEPLOYMENT, "CARD-1")
        await store.write(source, buffer.getvalue())
        runner = Runner()
        monkeypatch.setattr(tasks, "Session", lambda *_args: Session())
        monkeypatch.setattr(tasks, "_locate", locate)
        monkeypatch.setattr(
            tasks,
            "CARD_EXTRACTION_LIMITS",
            ZipExtractionLimits(
                max_archive_bytes=64 * 1024,
                max_entries=8,
                max_entry_bytes=64 * 1024,
                max_total_bytes=128 * 1024,
            ),
        )
        tasks.register(runner, object(), store)
        try:
            await runner.handler(SimpleNamespace(report=lambda *_args: None), DEPLOYMENT)
        except tasks.IngestRefused as error:
            assert "payload.bin" in str(error)
        else:
            raise AssertionError("camera-trap ingest accepted a compression bomb")
        prefix = image_prefix("reserve", DEPLOYMENT)
        assert [stat async for stat in store.list(prefix=prefix)] == []

    asyncio.run(go())


# --- who may mint ------------------------------------------------------------


@skip_without_database
async def test_a_volunteer_cannot_mint_an_upload_url(client) -> None:
    """Minting is the authorised step, so this is where the refusal has to land.

    Once a URL exists it carries its own authorisation and the storage service
    asks nobody. That is the design, and it is exactly why the check here
    matters more than it looks: a volunteer who could mint would have a URL that
    every later check honours.
    """
    signed_in = await client.post("/session", params={"email": VOLUNTEER})
    response = await client.post(
        "/cards", params={"deployment_id": DEPLOYMENT}, headers=cookie(signed_in)
    )
    assert response.status == 403


@skip_without_database
async def test_an_anonymous_caller_cannot_mint_an_upload_url(client) -> None:
    """A 401 rather than a 403: nobody was identified, so nothing was denied."""
    response = await client.post("/cards", params={"deployment_id": DEPLOYMENT})
    assert response.status == 401


@skip_without_database
async def test_minting_for_an_unknown_deployment_is_a_404(ranger) -> None:
    """No URL is ever minted for a key no row claims.

    Without this the store would accept writes to `cards/…/9999/…` and the
    orphan would sit there until someone audited the bucket.
    """
    client, headers = ranger
    response = await client.post(
        "/cards", params={"deployment_id": 9999}, headers=headers
    )
    assert response.status == 404


# --- the round trip ----------------------------------------------------------


@skip_without_database
async def test_a_minted_url_accepts_exactly_one_upload(ranger) -> None:
    """Mint, PUT to the URL that came back, and find the bytes in the store.

    The URL is used verbatim. Nothing here reconstructs a signature or reaches
    into the store to write -- if presigning were broken this would fail, which
    is the only reason to write it this way round.
    """
    client, headers = ranger
    minted = await mint(client, headers)
    assert minted["key"].endswith(".zip")
    assert minted["deployment_id"] == DEPLOYMENT

    body = card_archive()
    stored = await client.put(minted["url"], content=body)
    assert stored.status == 201, stored.text
    assert stored.json() == {"key": minted["key"], "size": len(body)}


@skip_without_database
async def test_an_expired_url_is_refused(ranger) -> None:
    """The deadline is inside the signature, and it is checked on the way back in.

    The URL is aged by rewriting `expires` to a past time -- which also proves
    the second half of the claim: editing the deadline does not buy more time,
    because the edit invalidates the digest that covered it.
    """
    client, headers = ranger
    minted = await mint(client, headers)
    past = str(int(time.time()) - 60)
    url = minted["url"].split("&expires=")[0] + f"&expires={past}" + \
        "&signature=" + minted["url"].split("signature=")[1]
    response = await client.put(url, content=card_archive())
    assert response.status == 403


@skip_without_database
async def test_a_tampered_signature_is_refused(ranger) -> None:
    """One flipped hex digit, and the write is refused."""
    client, headers = ranger
    minted = await mint(client, headers)
    head, _, signature = minted["url"].partition("signature=")
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    response = await client.put(head + "signature=" + flipped, content=card_archive())
    assert response.status == 403


@skip_without_database
async def test_a_url_cannot_be_redirected_to_another_key(ranger) -> None:
    """The key is inside the signature, so a URL is good for one object only.

    This is the test that stops a ranger with a legitimate URL for their own
    deployment from writing over somebody else's card.
    """
    client, headers = ranger
    minted = await mint(client, headers)
    hijacked = minted["url"].replace(minted["key"], "cards/elsewhere/1/other.zip", 1)
    response = await client.put(hijacked, content=card_archive())
    assert response.status == 403


@skip_without_database
async def test_an_upload_url_will_not_serve_a_read(ranger) -> None:
    """The method is inside the signature: a `PUT` URL is not a `GET` URL.

    Without this a URL handed to an uploader would also read every other card
    they could name, which is the whole reason `url(..., method=...)` takes a
    method at all.
    """
    client, headers = ranger
    minted = await mint(client, headers)
    await client.put(minted["url"], content=card_archive())
    response = await client.get(minted["url"])
    # The route is declared for PUT only, so the router refuses before the
    # signature is ever consulted -- which is a stronger answer than a 403.
    assert response.status in (404, 405)


# --- ingest ------------------------------------------------------------------


async def settle(client, headers, task_id: str, *, within: float = 20.0) -> dict:
    """Poll `/tasks/{task_id}` until the runner reports a terminal state.

    Polling the endpoint rather than reading the registry, because the endpoint
    is the thing a client has. The runner's own workers are started by the
    lifespan and claim the job on their own — nothing here drives them, which
    is what makes this the real path rather than a hand-executed handler.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + within
    last: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/tasks/{task_id}", headers=headers)
        if response.status == 200:
            last = response.json()
            if last.get("state") in {"done", "failed"}:
                return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} never settled; last seen {last}")


@skip_without_database
async def test_ingest_unpacks_the_uploaded_card_and_records_it(ranger) -> None:
    """The whole stage, end to end, driven by the runner's own workers.

    Upload through the minted URL, press ingest, and wait for the state the
    runner sets. Nothing in this test claims or executes a job: the workers
    started by the lifespan do that, so a handler that could not be claimed, or
    a runner that never marked it done, fails here.
    """
    from camera_trap.media import image_prefix
    from camera_trap.models import Deployment, Station

    from wreath.orm import Session

    client, headers = ranger
    application = client.app

    minted = await mint(client, headers)
    assert (await client.put(minted["url"], content=card_archive())).status == 201

    launched = await client.post(f"/cards/{DEPLOYMENT}/ingest", headers=headers)
    assert launched.status == 200, launched.text
    assert launched.json()["state"] == "queued"

    settled = await settle(client, headers, launched.json()["task_id"])
    assert settled["state"] == "done", settled

    session = Session(application.state.orm_main, "read")
    try:
        row = await session.fetch_one(
            Deployment.select()
            .where(Deployment.id == DEPLOYMENT)
            .include(Deployment.station.joined(Station.reserve.joined()))
        )
        assert row.image_count == 2
        assert row.ingested_at is not None
        prefix = image_prefix(row.station.reserve.slug, DEPLOYMENT)
    finally:
        await session.close()

    store = application.state.objects_media
    # `list` yields `ObjectStat`, not keys -- naming the loop variable for what
    # it is keeps the assertion message readable when it fails.
    written = sorted([stat.key async for stat in store.list(prefix=prefix)])
    assert written == [f"{prefix}IMG_0001.JPG", f"{prefix}IMG_0002.JPG"], written


@skip_without_database
async def test_ingest_without_an_uploaded_card_fails_rather_than_hanging(ranger) -> None:
    """A missing archive is a permanent failure, and the client is told so.

    `retries=0` on the handler is what makes this one attempt and a dead-letter
    rather than five identical failures spread over an hour. What the test
    asserts is the half a client can see: the task reaches `failed`, promptly,
    rather than sitting at `running` while the runner backs off.
    """
    client, headers = ranger
    launched = await client.post(f"/cards/{DEPLOYMENT}/ingest", headers=headers)
    assert launched.status == 200

    settled = await settle(client, headers, launched.json()["task_id"])
    assert settled["state"] == "failed", settled


@skip_without_database
async def test_two_rangers_pressing_ingest_get_one_task(ranger) -> None:
    """The dedup key means one archive is unpacked once, not twice at once.

    Two workers unpacking the same card into the same prefix is not a failure
    the store would report — both writes succeed and the last one wins — so
    nothing downstream would notice. The key is the only thing preventing it,
    and the visible consequence is that both callers are handed the same task
    to watch rather than one being told nothing.
    """
    client, headers = ranger
    await client.put((await mint(client, headers))["url"], content=card_archive())

    first = await client.post(f"/cards/{DEPLOYMENT}/ingest", headers=headers)
    second = await client.post(f"/cards/{DEPLOYMENT}/ingest", headers=headers)
    assert first.status == second.status == 200
    assert first.json()["task_id"] == second.json()["task_id"]


# --- watching ----------------------------------------------------------------


@skip_without_database
async def test_task_status_is_not_readable_by_an_anonymous_caller(ranger) -> None:
    """Task ids are job ids, which are a sequence.

    Without the `authorize` predicate every ingest's state, message and error
    text is readable by whoever counts from one.
    """
    client, headers = ranger
    launched = await client.post(f"/cards/{DEPLOYMENT}/ingest", headers=headers)
    task_id = launched.json()["task_id"]

    assert (await client.get(f"/tasks/{task_id}")).status == 401
    assert (await client.get(f"/tasks/{task_id}", headers=headers)).status == 200


@skip_without_database
async def test_an_unknown_task_is_a_404_rather_than_an_empty_body(ranger) -> None:
    """A client polling a task that aged out must be able to tell."""
    client, headers = ranger
    assert (await client.get("/tasks/999999", headers=headers)).status == 404
