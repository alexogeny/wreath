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
from wreath.middleware import SessionMiddleware
from wreath.orm import Session

from .config import SETTINGS
from .models import MODELS
from .policies import ENGINE
from .routers import ROUTERS, admin


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

    # --- who you are ---------------------------------------------------------
    #
    # `add_global_middleware`, not `add_middleware`. Route middleware is
    # compiled into a route's tape and runs *after* authentication has decided,
    # while `SessionIdentityBackend` reads `request.state.session` *during*
    # authentication -- so the route-middleware spelling hands every protected
    # route a 401 with a perfectly valid cookie in hand. The framework now
    # refuses that combination at route-compile time and names this call in the
    # message, so the ordering is enforced rather than remembered.
    application.add_global_middleware(
        SessionMiddleware(secret=SETTINGS.session_secret(), secure=SETTINGS.session_secure)
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
    return application


#: The tooling's target. Built by the module import, so an unset DSN fails here
#: with the message above rather than deep inside a driver.
app = build()
