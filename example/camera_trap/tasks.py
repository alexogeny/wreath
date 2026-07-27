"""Background work: unpacking an SD card that has just been uploaded.

Ingest is the example's one genuinely long operation, and it is long for a
reason no amount of optimisation removes — a card is thousands of images, and
the work is proportional to the card. A request cannot wait for it. So the
route enqueues and hands back an id, the runner does the work on whichever
worker picks it up, and the client watches.

**Why this is a durable job and not a background task.** `wreath.background`
runs work after the response on *this* process; if the process dies, the work is
gone and nobody knows. A field team that has just spent forty minutes uploading
a card over a satellite link has to be able to lose the connection, close the
laptop, and come back to a finished ingest. That requires the work to be a row
in PostgreSQL, retried by whoever is alive — which is what `app.jobs(...)` is.

**Why the handler reports progress but never sets a terminal state.** `ctx.report`
is commentary; `done` and `failed` belong to the runner, because only the runner
knows whether a raised exception means "dead-lettered" or "retrying in eight
seconds". A handler that wrote `done` itself would be lying on the attempt that
is about to be retried.
"""

from __future__ import annotations

import datetime
import zipfile
from typing import Any

from wreath.objects import ObjectError, unzip_stream
from wreath.orm import Registry, Session

from .media import card_key, image_prefix
from .models import Deployment, Reserve, Station

#: The job name. A constant because the route enqueues it by string and the
#: registration decorates by string, and two spellings of one name is exactly
#: the bug `JobRunner` raises `KeyError: unknown task` for.
INGEST_CARD = "ingest_card"

#: The runner's queue name, and the schema its tables live in.
#:
#: The queue goes in the *application's* schema rather than the `wreath`
#: default, so that everything this example owns is one namespace: one
#: `DROP SCHEMA ... CASCADE` removes the tables, the queue and the job history
#: together, and a second copy of the example on the same database cannot pick
#: up the first one's jobs.
QUEUE = "ingest"

#: The tables `queue_schema_sql` creates, for anyone who needs to tell the
#: application's own tables from its queue's.
#:
#: **The table is `jobs`, not `ingest_jobs`** — `JobRunner` names it from the
#: schema and not from the queue name, so two runners sharing a schema share a
#: table. That is fine here (there is one runner) and it is the reason the
#: queue gets the application's schema rather than the `wreath` default: a
#: second copy of this example on the same database would otherwise pick up the
#: first one's jobs.
QUEUE_TABLES = ("jobs",)


def queue_schema_sql(schema: str) -> str:
    """The durable-queue DDL, for whoever is building the database.

    **This is not in `example/migrations/`, and that is a real seam rather than
    an oversight.** `wreath migrations generate` derives its artifact from the
    ORM models, and the job queue is not an ORM model — it is infrastructure
    the runner owns and describes with `JobRunner.schema_sql()`. So a database
    built purely from the migration artifact has every table the application
    declares and none of the tables its runner needs, and the failure arrives
    at the first `launch` as `relation "…jobs" does not exist`.

    Ten subsystems in wreath produce a `schema_sql()` the migration system does
    not consume. Until that is settled, an application carries the join itself,
    which is what this function is: one name for the DDL so the quickstart, the
    seeder and the test fixtures apply the same thing.

    Args:
        schema: the namespace the queue tables are created in.

    Returns:
        `CREATE TABLE IF NOT EXISTS` statements, semicolon-separated.
    """
    from wreath.jobs import JobRunner

    # A runner built only to be asked what its tables look like. `schema_sql`
    # reads the name and the schema and touches no database, so the argument is
    # never used -- but passing `None` for it is a claim about the internals,
    # so the real object is constructed and thrown away instead.
    return JobRunner(None, name=QUEUE, schema=schema).schema_sql()


class IngestRefused(Exception):
    """The card cannot be ingested, and retrying will not change that.

    Distinct from every other failure on purpose. A missing archive or an
    unreadable zip is a *permanent* condition — the bytes are wrong, and the
    runner re-running this handler five times with exponential backoff only
    delays the moment somebody is told. Raised so the shape is nameable at the
    registration site, where `retries=0` turns it into one attempt and a
    dead-letter.
    """


async def _locate(session: Session, deployment_id: int) -> tuple[Deployment, str]:
    """The deployment and the slug of the reserve that owns it.

    The slug is two hops away — deployment to station to reserve — and it is
    needed for the key layout, so it is fetched with the deployment rather than
    in a second round trip. `load="raise"` means the alternative is not a
    silent extra query, it is an exception, which is the point of that setting.
    """
    found = await session.fetch_one(
        Deployment.select()
        .where(Deployment.id == deployment_id)
        .include(Deployment.station.joined(Station.reserve.joined()))
    )
    if found is None:
        raise IngestRefused(f"no deployment {deployment_id}")
    reserve: Reserve = found.station.reserve
    return found, reserve.slug


def register(runner: Any, registry: Registry, store: Any) -> None:
    """Register the ingest handler on `runner`.

    A function rather than a module-level decorator because the handler needs
    the ORM registry and the object store, and neither exists until the
    application has been assembled. `camera_trap.app.build` calls this once.

    Args:
        runner: the `JobRunner` from `app.jobs(...)`.
        registry: the compiled ORM registry, for opening a write session.
        store: the object store holding cards and images.
    """

    @runner.task(INGEST_CARD, retries=0)
    async def ingest_card(ctx: Any, deployment_id: int) -> dict[str, Any]:
        """Unpack one uploaded card into the image store.

        `retries=0` is deliberate. Every way this fails is a fact about the
        bytes that were uploaded — absent, truncated, not a zip, or carrying an
        entry name the store refuses — and none of them changes on a second
        attempt. Retrying an unreadable archive five times produces five
        identical failures and tells the field team forty minutes later than it
        could have.

        Args:
            ctx: the job context; `ctx.report` is what the SSE stream carries.
            deployment_id: which collection event's card to unpack.

        Returns:
            A summary the job row keeps: how many entries landed, and where.

        Raises:
            IngestRefused: the deployment is unknown, or its archive is absent
                or unreadable.
        """
        session = Session(registry, "write")
        try:
            deployment, slug = await _locate(session, deployment_id)
            archive = card_key(slug, deployment_id, deployment.card_serial)
            ctx.report(5, f"reading {archive}")

            if not await store.exists(archive):
                raise IngestRefused(
                    f"deployment {deployment_id} has no uploaded card at {archive}"
                )

            prefix = image_prefix(slug, deployment_id)
            ctx.report(20, "unpacking")
            try:
                written = await unzip_stream(store, archive, prefix=prefix)
            except ObjectError as error:
                # An entry name the store refuses -- a `../` traversal, or an
                # empty name. Converted rather than propagated so the field
                # team is told the card is bad, not handed a store-internal
                # error about key normalisation.
                raise IngestRefused(
                    f"card {deployment.card_serial} holds an unusable entry name: {error}"
                ) from error
            except zipfile.BadZipFile as error:
                # Truncated, or never a zip. Named separately from `ObjectError`
                # because the operator's next step is different: a bad entry
                # name means repack the card, a bad archive means re-upload it.
                raise IngestRefused(
                    f"card {deployment.card_serial} is not a readable zip: {error}"
                ) from error

            ctx.report(85, f"unpacked {len(written)} image(s)")
            deployment.image_count = len(written)
            deployment.ingested_at = datetime.datetime.now(tz=datetime.UTC)
            # `flush` outside an explicit `begin()` opens a transaction for the
            # write and commits or rolls it back atomically, which is exactly
            # the scope wanted here: the two fields describe one fact, and a
            # process killed between them would otherwise leave a deployment
            # marked ingested with no image count.
            await session.flush()
            ctx.report(100, "ingested")
            return {"deployment_id": deployment_id, "images": len(written), "prefix": prefix}
        finally:
            # The session is opened here rather than injected, so closing it is
            # this handler's job. A leaked connection per job is a pool that
            # empties over a day and a symptom that looks like a slow database.
            await session.close()
