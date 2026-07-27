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

from .config import SETTINGS
from .models import MODELS
from .routers import ROUTERS


def dsn() -> str:
    """The database URL, from the environment, with no default.

    Kept as a function so the seeding script and the tests have one name for it.
    See :mod:`camera_trap.config` for what is read and why there is no fallback
    to a localhost guess.
    """
    return SETTINGS.database_url()


def build(*, validate_schema: str = "error") -> Wreath:
    """Assemble the application.

    ``validate_schema`` is a parameter because the seeder needs ``"off"`` --
    it runs *before* the schema exists, which is the one moment the check is
    guaranteed to fail for a good reason.
    """
    application = Wreath()
    application.postgres("main", dsn=dsn())
    application.orm(database="main", models=list(MODELS), validate_schema=validate_schema)
    for router in ROUTERS:
        application.include_router(router)
    return application


#: The tooling's target. Built by the module import, so an unset DSN fails here
#: with the message above rather than deep inside a driver.
app = build()
