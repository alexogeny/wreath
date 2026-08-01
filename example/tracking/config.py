"""The three things this application reads before it serves a request.

The camera-trap example's ``config`` module is long because that application has
an object store, a cache, a session cookie and a bound on how much history one
request may ask for. This one has a database, a schema and a signing secret, and
listing anything else would be inventing configuration to demonstrate a feature.

* ``TRACKING_DSN`` — the database. No default: guessing at ``localhost`` and
  connecting to the wrong one is worse than refusing to start.
* ``TRACKING_SCHEMA`` — the PostgreSQL namespace. Read at *import*, because
  ``schema=`` is fixed when a model class is built, so a process serves exactly
  one namespace and changing it means a new process. That is also what lets a
  parallel test run give each worker its own.
* ``TRACKING_SESSION_SECRET`` — the cookie key.

The DSN is resolved lazily by :meth:`Settings.database_url` so that importing a
router, a policy or a serializer needs no database at all. That is what lets
this example's policy tests — the ones that carry its argument — run in
milliseconds with nothing installed.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass

#: The variable an operator sets.
DSN_VARIABLE = "TRACKING_DSN"

#: Wreath's own database suites already export this, so honouring it removes a
#: step for anyone who has run them.
FALLBACK_DSN_VARIABLE = "WREATH_TEST_POSTGRES_DSN"

#: The namespace name ``\dt tracking.*`` shows. The framework's own tables live
#: in ``wreath``; an application's belong somewhere it chose.
DEFAULT_SCHEMA = "tracking"

#: Resolved at import. See the module docstring for why it cannot be later.
SCHEMA = os.environ.get("TRACKING_SCHEMA", DEFAULT_SCHEMA)

#: A development key, published in the source, so ``wreath serve`` works before
#: anyone has read this file. Using it in a deployment is caught below.
DEVELOPMENT_SESSION_SECRET = "tracking-development-session-secret-not-for-deployment"

#: The conservancy's own calendar. A constant and not configuration: this is one
#: reserve in one place, and "how far did it walk yesterday" is asked in the wall
#: clock the field team keeps. It is *not* the reader's zone -- a partner
#: institution in Zurich asking about Tuesday means Tuesday in the Rift Valley,
#: because that is the day the animal had.
#:
#: The camera-trap example keeps its zone on the `Reserve` row, because it has
#: four of them on three continents. One reserve does not need a column, and a
#: column that always holds one value teaches the wrong lesson about where
#: configuration lives.
CONSERVANCY_ZONE = "Africa/Nairobi"


@dataclass(frozen=True, slots=True)
class Settings:
    """Start-up configuration. Immutable, because none of it changes later."""

    schema: str = SCHEMA

    def database_url(self) -> str:
        """The DSN, or a refusal naming both variables it looked at.

        Lazy rather than a field, so importing this module never needs a
        database. The refusal names the fallback too, because someone who has
        run wreath's own suites has already exported it and would otherwise be
        told to set a variable they do not need.
        """
        dsn = os.environ.get(DSN_VARIABLE) or os.environ.get(FALLBACK_DSN_VARIABLE)
        if not dsn:
            raise RuntimeError(
                f"set {DSN_VARIABLE} (or {FALLBACK_DSN_VARIABLE}) to a PostgreSQL "
                f"DSN; the tracking example has no default because connecting to "
                f"the wrong database is worse than refusing to start"
            )
        return dsn

    def session_secret(self) -> str:
        """The cookie key, warning once if it is the published one."""
        secret = os.environ.get("TRACKING_SESSION_SECRET")
        if secret:
            return secret
        warnings.warn(
            "TRACKING_SESSION_SECRET is unset; using the development key that is "
            "printed in tracking/config.py. Anybody can forge a session with it.",
            RuntimeWarning,
            stacklevel=2,
        )
        return DEVELOPMENT_SESSION_SECRET


#: One instance, read at import, shared by the app and the seeder.
SETTINGS = Settings()
