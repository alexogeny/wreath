"""Application assembly for the camera-trap example.

One `Wreath` object holds the whole thing: the database, the compiled ORM
registry, and the routers. That is the example's argument in miniature — the
routers are not a separate service talking to a separate ORM over a separate
connection pool, they are three declarations on one application, and the pieces
find each other because the framework already knows about all of them.

Later stages add jobs, policies, an object store and the analysis views to this
same function. Nothing here has to be rearranged to make room for them.

The migration tooling loads this module by name::

    wreath migrations generate camera_trap.app:app --initial --output migrations/
    wreath migrations apply    camera_trap.app:app migrations/0001-*.wma1

and so does the server::

    wreath serve camera_trap.app:app

so the attribute below is a real entry point, not a convenience.
"""

from __future__ import annotations

from wreath.app import Wreath
from wreath.auth import SessionIdentityBackend
from wreath.authorization import CedarAuthorizer, permissions_router
from wreath.orm import Session
from wreath.policy import HttpPolicy, SessionPolicy
from wreath.progress import ProgressRegistry

from . import tasks
from .config import SETTINGS
from .models import MODELS, SCHEMA
from .policies import ENGINE
from .routers import ROUTERS, admin, uploads


def dsn() -> str:
    """The database URL, from the environment, with no default.

    Kept as a function so the seeding script and the tests have one name for it.
    See `camera_trap.config` for what is read and why there is no fallback
    to a localhost guess.
    """
    return SETTINGS.database_url()


def build(*, validate_schema: str = "error") -> Wreath:
    """Assemble the application.

    `validate_schema` is a parameter because the seeder needs `"off"` --
    it runs *before* the schema exists, which is the one moment the check is
    guaranteed to fail for a good reason. Everything else runs on the framework
    default, `"error"`, which is the point: this example is only worth reading if
    it runs the configuration a real application gets.

    `tests/example/test_schema_integration.py` asserts the constraints really
    are present in the catalog, so this schema is checked from both ends: the
    application refuses to start against a database that does not match, and a
    test proves the database is what the models say.
    """
    application = Wreath()
    application.postgres("main", dsn=dsn())
    registry = application.orm(
        database="main", models=list(MODELS), validate_schema=validate_schema
    )
    # The activity chart is a sealed `Series`, so wreath owns two tables for its
    # settled buckets. Declaring them here is what puts them in
    # `app.schema_components()`, which is what makes the lifespan create them --
    # the same route the job ledger takes. Without it they were emitted by
    # `wreath schema sql` and created by nothing.
    application.series(database="main")

    # --- who you are ---------------------------------------------------------
    #
    # Session state is a first-class activation policy, fixed before identity.
    application.configure_http_policy(
        HttpPolicy(
            session=SessionPolicy(
                secret=SETTINGS.session_secret(), secure=SETTINGS.session_secure
            )
        )
    )
    application.configure_auth(
        SessionIdentityBackend(),
        # The same engine the handlers query directly. One policy set, one
        # evaluator, two entry points -- see `camera_trap.policies`.
        CedarAuthorizer(engine=ENGINE),
    )

    for router in ROUTERS:
        application.include_router(router)

    # --- the console's own question ------------------------------------------
    #
    # "What may I do?", answered from the same `@authorize` declarations that
    # enforce it. The console greys out the buttons a volunteer cannot press
    # without a second list of rules to keep in step with the first.
    application.include_router(permissions_router(application))

    def open_session(request: object) -> Session:
        """A write session for the generated registry routes.

        Built here rather than imported because `crud_router` needs the
        registry, and the registry does not exist until `app.orm(...)` has run.
        """
        return Session(registry, "write")

    admin.mount(application, open_session)

    # --- bytes, and the work that follows them -------------------------------
    #
    # The store is registered on the application so its root is opened at
    # registration and closed on shutdown -- including when startup fails part
    # way, which is the case a `finally` in this function would miss.
    #
    # `url_secret` is bytes. `app.objects` takes `**options: Any`, so a `str`
    # here is not refused at registration; it flows through to the first
    # `store.url(...)` call and raises `TypeError` from inside `hmac.new`,
    # which names neither the option nor this line. Encoding at the call site
    # is the whole fix, and the reason it is worth a comment is that nothing
    # else would have told us.
    store = application.objects(
        "media",
        backend="local",
        root=SETTINGS.media_root,
        url_secret=SETTINGS.media_secret().encode(),
    )

    # A progress registry with no bus: this example runs one process, and
    # `ProgressRegistry` says plainly that the busless form is the right default
    # for a single worker. Handing it the bus is the one change needed to make
    # an ingest launched on one worker watchable from another.
    progress = ProgressRegistry()
    runner = application.jobs(
        tasks.QUEUE, database="main", progress=progress, schema=SCHEMA
    )
    tasks.register(runner, registry, store)
    uploads.mount(application, store, runner)
    return application


#: The tooling's target. Built by the module import, so an unset DSN fails here
#: with the message above rather than deep inside a driver.
app = build()
