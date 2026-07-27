"""Getting a card off a laptop and into the system, and watching what happens.

Four endpoints, and they are four rather than one because the bytes and the
metadata travel by different routes:

    POST /cards                     mint a URL to upload one card to
    PUT  /media                     what that URL points at
    POST /cards/{deployment_id}/ingest   unpack it, in the background
    GET  /tasks/{task_id}[/stream]       watch the unpacking

**Why `PUT /media` exists at all.** In production the presigned URL points at S3
and the application never sees the bytes — that is the entire point of
presigning. `LocalObjectStore` has no such service in front of it, so the
example serves its own presigned URLs, and this route is that service. It is
worth reading precisely because it makes the check explicit: the same
`verify_local_url` call S3 performs inside its own infrastructure, written out
where you can see what it is checking.

Swapping `backend="local"` for `backend="s3"` in `camera_trap.app.build` deletes
this one route and changes nothing else.

**Why the key travels in the query string and not the path.** `store.url()`
mints `/<key>?expires=…&signature=…`, and a key here is `cards/kopje/12/SD-1.zip`
— four segments. Wreath's router has no multi-segment path parameter, so a route
cannot bind that path at all; `{key:path}` is not a converter wreath knows, and
it is accepted at registration and then fails per request. So the mint endpoint
hands back the signature in a shape this application can actually route. The
signature covers the key, the method and the deadline and does not care which
part of the URL carried them, so nothing is weakened — but it is a rearrangement
the framework forced, and it is written down rather than smoothed over.

**Mount, do not import.** These routes close over the object store and the job
runner, and neither exists until `app.build` has assembled the application.
Same reason `admin.mount` exists.
"""

from __future__ import annotations

from typing import Annotated, Any

from wreath import Request, Router
from wreath.auth import authenticated
from wreath.authorization import authorize
from wreath.binding import Query
from wreath.exceptions import BadRequest, Forbidden, NotFound
from wreath.objects import ObjectError
from wreath.orm import FromORM, Session
from wreath.progress import progress_stream, status_response
from wreath.response import JSONResponse, Response

from ..media import ARCHIVE_CONTENT_TYPE, UPLOAD_URL_TTL, card_key, mint_upload_url
from ..models import Deployment, Station
from ..policies import ADMINISTER
from ..tasks import INGEST_CARD

ReadSession = Annotated[Session, FromORM("main", workload="read")]

#: The entity the card endpoints are authorised against. A separate registry
#: from the species and station ones: administering the vocabulary and
#: accepting field uploads are different jobs held by different people, and one
#: entity for both would make a policy that admits an ecologist to the species
#: list also admit them to every card.
CARD_REGISTRY = 'Registry::"cards"'

#: The largest card archive the example accepts, in bytes.
#:
#: `unzip_stream` reads the whole archive into memory and decompresses each
#: entry whole, so peak memory is the archive plus its largest entry — its own
#: docstring says so, and says it is safe for an operator's archive and not for
#: an anonymous caller's. This ceiling is what makes "an operator's archive"
#: true here: minting requires a ranger, and this bounds what even a ranger can
#: make an ingest worker allocate. 64 MiB is one real card.
MAX_CARD_BYTES = 64 * 1024 * 1024


def mount(application: Any, store: Any, runner: Any) -> None:
    """Attach the upload, ingest and progress routes to `application`.

    Args:
        application: the assembled `Wreath`.
        store: the object store holding cards and unpacked images.
        runner: the `JobRunner` the ingest task is registered on.
    """
    uploads = Router(tags=("uploads",))

    async def _owning_slug(session: Session, deployment_id: int) -> tuple[Deployment, str]:
        """One deployment and the slug of the reserve holding it, or a 404."""
        found = await session.fetch_one(
            Deployment.select()
            .where(Deployment.id == deployment_id)
            .include(Deployment.station.joined(Station.reserve.joined()))
        )
        if found is None:
            raise NotFound(f"no deployment {deployment_id}")
        return found, found.station.reserve.slug

    @uploads.post("/cards", summary="Mint a URL for uploading one card archive")
    @authorize(action=ADMINISTER, resource=CARD_REGISTRY)
    async def mint_card_url(
        request: Request,
        session: ReadSession,
        deployment_id: Annotated[int, Query(minimum=1)],
    ) -> dict:
        """A signed URL the uploader may `PUT` this deployment's card archive to.

        Minting is authorised; the URL is not. That asymmetry is the design —
        the signature *is* the authorisation, carried by the URL, so the storage
        service can accept the write knowing nothing about observers, reserves
        or Cedar. Everything expensive to check happens here, once, before any
        bytes move.

        The URL is good for `UPLOAD_URL_TTL` seconds and for `PUT` only. Handing
        it back as a `GET` will not read the card out again: the method is
        inside the signature.

        Args:
            deployment_id: the collection event the card belongs to. Checked
                against the database, so a URL is never minted for a key no row
                claims.

        Returns:
            The key, the URL to write to, its lifetime, and the content type
            and size ceiling the upload must respect.
        """
        deployment, slug = await _owning_slug(session, deployment_id)
        key = card_key(slug, deployment.id, deployment.card_serial)
        signed = mint_upload_url(store, key)
        # `signed` is `/<key>?expires=…&signature=…`. Its query carries the
        # whole credential; the path is re-expressed as `?key=` because of the
        # routing limitation in this module's docstring.
        query = signed.partition("?")[2]
        return {
            "deployment_id": deployment.id,
            "key": key,
            "url": f"/media?key={key}&{query}",
            "expires_in": UPLOAD_URL_TTL,
            "content_type": ARCHIVE_CONTENT_TYPE,
            "max_bytes": MAX_CARD_BYTES,
        }

    @uploads.put("/media", summary="What a minted upload URL points at")
    async def accept_upload(
        request: Request,
        key: str,
        expires: Annotated[int, Query(minimum=0)],
        signature: str,
    ) -> Response:
        """Store bytes, if the URL that carried them is authentic and unexpired.

        **No `@authenticated` and no `@authorize`, on purpose.** An uploader
        holding this URL has no session — it is a field laptop running `curl`
        against a URL pasted into a work order. The signature is the credential,
        and `verify_local_url` is where it is checked: the key, the method and
        the deadline are all inside the HMAC, so none of the three can be edited
        in the query string without invalidating it.

        Args:
            key: the object being written.
            expires: the absolute UNIX deadline the URL carries.
            signature: the HMAC over key, method and deadline.

        Returns:
            `201` with the stored size.

        Raises:
            Forbidden: the signature does not match, or the deadline has passed.
                One status for both, deliberately — telling a caller *which* of
                the two failed tells them whether they hold a real signature,
                which is the first thing worth knowing to an attacker.
            BadRequest: the body exceeds `MAX_CARD_BYTES`, or the key is not one
                this store will accept.
        """
        try:
            ok = store.verify_local_url(
                key, method="PUT", expires=expires, signature=signature
            )
        except ObjectError as error:
            raise BadRequest(f"not a usable object key: {error}") from error
        if not ok:
            raise Forbidden("upload URL is not valid for this key, method, or time")

        body = await request.body()
        if len(body) > MAX_CARD_BYTES:
            raise BadRequest(
                f"card archive is {len(body)} bytes; the limit is {MAX_CARD_BYTES}"
            )
        stat = await store.write(key, body, content_type=ARCHIVE_CONTENT_TYPE)
        return JSONResponse({"key": key, "size": stat.size}, status=201)

    @uploads.post("/cards/{deployment_id}/ingest", summary="Unpack an uploaded card")
    @authorize(action=ADMINISTER, resource=CARD_REGISTRY)
    async def start_ingest(
        request: Request, deployment_id: int, session: ReadSession
    ) -> dict:
        """Enqueue the unpack and hand back an id to watch.

        The response is a handle, not a result. Unpacking a card is minutes of
        work and the request is not going to wait for it — which is what makes
        this a durable job rather than something awaited inline. See
        `camera_trap.tasks`.

        A `key` is passed to `launch` so that two rangers pressing the button on
        the same deployment get **one** ingest and two handles to the same task,
        rather than two workers unpacking one archive into one prefix at once.

        Returns:
            `{"task_id": ..., "state": "queued"}`. The id is the job id, so the
            runner, the progress registry and the SSE stream all use one
            identifier.
        """
        await _owning_slug(session, deployment_id)
        handle = await runner.launch(
            INGEST_CARD, deployment_id, key=f"ingest-card:{deployment_id}"
        )
        return handle.as_dict()

    @uploads.get("/tasks/{task_id}", summary="One task's latest progress")
    @authenticated()
    async def task_status(request: Request, task_id: str) -> Response:
        """The task's current percent, message and state, or `404`.

        A `404` covers three cases that are one case to a client: no such task,
        a task that has aged out of the bounded registry, and a task this caller
        may not watch. Distinguishing them would leak the id space the
        `authorize` predicate exists to protect — task ids are job ids, which
        are a sequence, so without a guard every ingest's state and error text
        is readable by whoever counts.
        """
        return status_response(
            runner.progress, task_id, authorize=lambda _id: request.identity is not None
        )

    @uploads.get("/tasks/{task_id}/stream", summary="Server-sent progress for one task")
    @authenticated()
    async def task_stream(request: Request, task_id: str) -> Response:
        """The same progress as an event stream, ending when the task is terminal.

        The stream closes itself on `done` or `failed` rather than leaving the
        connection open for a client to notice. An ingest that dead-letters is
        exactly when a field team is watching, and a stream that merely stops
        producing looks identical to a network problem.
        """
        return progress_stream(
            runner.progress,
            task_id,
            interval=0.5,
            max_duration=600.0,
            authorize=lambda _id: request.identity is not None,
        )

    application.include_router(uploads)
