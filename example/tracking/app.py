"""Application assembly for the tracking example.

One `Wreath` object holds the whole thing. Compared with the camera-trap
example's `build`, what is *missing* is as informative as what is here: no
object store, no job runner, no cache, no generated CRUD. This application
ingests, answers and streams, and every part of it is here because one of those
three needs it.

The migration tooling loads this module by name::

    PYTHONPATH=. wreath migrations generate tracking.app:app --initial --output tracking/migrations/
    PYTHONPATH=. wreath migrations apply tracking.app:app tracking/migrations/migration.bin

and so does the server::

    PYTHONPATH=. wreath serve tracking.app:app

so the attribute at the bottom is a real entry point, not a convenience.
"""

from __future__ import annotations

from wreath.app import Wreath
from wreath.auth import SessionIdentityBackend
from wreath.authorization import CedarAuthorizer
from wreath.policy import HttpPolicy, SessionPolicy
from wreath.rooms import RoomRegistry

from .config import SCHEMA, SETTINGS
from .live import LiveMap
from .models import MODELS
from .policies import ENGINE
from .routers import routers
from .rpc import ingest_service


def dsn() -> str:
    """The database URL, from the environment, with no default.

    A function so the seeder, the migration tooling and the tests have one name
    for it. See `tracking.config` for what is read and why there is no fallback
    to a localhost guess.
    """
    return SETTINGS.database_url()


def build(*, validate_schema: str = "error", cross_worker: bool = True) -> Wreath:
    """Assemble the application.

    Args:
        validate_schema: `"off"` for the seeder, which runs before the schema
            exists -- the one moment the check is guaranteed to fail for a good
            reason. Everything else runs on the framework default, which is the
            point: this example is only worth reading if it runs the
            configuration a real deployment gets.
        cross_worker: Whether the live map's rooms reach other workers over the
            message bus. `True` is the deployed answer and the one the tests
            use. `False` gives `RoomRegistry` no bus, which `wreath.rooms` says
            plainly is the right default for a single process -- and it is what
            a reader wanting to run this against a database they cannot create
            tables in should pass.

    Returns:
        A configured `Wreath` application.
    """
    application = Wreath()
    application.postgres("main", dsn=dsn())
    registry = application.orm(
        database="main", models=list(MODELS), validate_schema=validate_schema
    )
    # The daily-distance chart is a sealed `Series`, so wreath owns two tables
    # for its settled buckets. This is what puts them in
    # `app.schema_components()` and so has the lifespan create them, the same
    # way the message bus below gets its own.
    application.series(database="main")

    # --- who you are ---------------------------------------------------------
    #
    # Session state is a first-class activation policy, fixed before identity.
    application.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret=SETTINGS.session_secret()))
    )
    # The same engine `tracking.policies.precision_for` queries directly. One
    # policy set, one evaluator, two entry points -- the route decorators go
    # through `CedarAuthorizer`, and the per-row precision question goes
    # straight to `ENGINE`. A second implementation of "how precisely may this
    # reader be told" is how a rule and its enforcement drift apart.
    application.configure_auth(SessionIdentityBackend(), CedarAuthorizer(engine=ENGINE))

    # --- the live map --------------------------------------------------------
    #
    # The bus is the whole of the cross-worker story: with it, a position
    # ingested by worker 3 reaches a browser connected to worker 1; without it
    # the fan-out is local and correct and smaller. `RoomRegistry` takes one or
    # takes None, and nothing else in this file changes between the two.
    bus = (
        application.messaging("live", database="main", schema=SCHEMA)
        if cross_worker
        else None
    )
    live = LiveMap(RoomRegistry(bus))

    for router in routers(live):
        application.include_router(router)

    # --- the same ingest, streamed -------------------------------------------
    #
    # A station on a permanent link streams positions instead of POSTing a
    # batch. It is the same `accept()` underneath, which is the point: a second
    # transport must not become a second ingest. gRPC needs HTTP/2 and therefore
    # wreath's own server with TLS, so this is additive -- the REST relay is
    # what a deployment behind another ASGI server keeps using. See
    # `tracking.rpc`.
    application.include_router(ingest_service(registry, live).router())

    # An SSE response finishes when its generator does, and a generator parked
    # on a queue nobody will fill again is a connection that outlives the
    # process trying to stop. Registered rather than left to a signal handler
    # because a graceful shutdown is exactly when this matters.
    @application.on_shutdown
    async def _end_live_streams(_: Wreath) -> None:
        live.close_all()

    # Kept on `app.state` so a test, an operator endpoint or a later chapter can
    # ask how many readers a worker holds. Explicit ownership rather than a
    # module-level singleton: this object holds open connections.
    application.state.live = live
    return application


#: The tooling's target. Built by the module import, so an unset DSN fails here
#: with the message `tracking.config` writes rather than deep inside a driver.
app = build()
