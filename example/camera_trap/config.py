"""Everything the application reads before it serves a request.

Configuration and state are different things and wreath keeps them apart. This
module is the start-up half: values fixed for the life of the process, read once
at import, never consulted again. Anything that changes while the application
runs belongs on ``request.state`` or ``app.state`` instead.

Three variables, and each is here because an operator would genuinely set it:

* ``CAMERA_TRAP_DSN`` — the database. No default, because guessing at
  ``localhost`` and connecting to the wrong one is worse than refusing to start.
* ``CAMERA_TRAP_MAX_WINDOW_DAYS`` — the widest span of sightings one request may
  ask for. This is the only thing standing between an anonymous caller and a
  scan of 140,000 rows, and how wide it can safely be depends on the machine.
* ``CAMERA_TRAP_SPECIES_CACHE_TTL`` — how long the cached species list may be
  stale. A controlled vocabulary changes a few times a year; the cache is also
  cleared by any write to the table, so this is the backstop rather than the
  mechanism.

**Read at import, deliberately.** Two of these are baked into decorators — a
``Query(maximum=…)`` bound and a ``@cached(ttl=…)`` — so they have to be known
when the module defining the route is imported. Setting the variable afterwards
does nothing, which is the correct behaviour for start-up configuration and the
wrong behaviour for anything else.

The DSN is the exception: it is resolved lazily by :meth:`Settings.database_url`
so that importing a router, a query, or a serializer needs no database at all.
That is what lets the example's fast tests run without PostgreSQL.

``load_env`` is strict — ``KEY=value``, no expansion, no ``export`` — and an
already-exported variable beats the file, so a developer's shell always wins
over a checked-in default. See ``example/.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wreath.config import load_env

#: The dotenv this package ships an example of. Optional: exporting the
#: variables works identically, and CI has no file.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

DSN_VARIABLE = "CAMERA_TRAP_DSN"

#: Wreath's own database suites already want this exported. Honouring it here
#: removes a step for anyone who has run them.
FALLBACK_DSN_VARIABLE = "WREATH_TEST_POSTGRES_DSN"

WINDOW_VARIABLE = "CAMERA_TRAP_MAX_WINDOW_DAYS"
CACHE_TTL_VARIABLE = "CAMERA_TRAP_SPECIES_CACHE_TTL"


def _number[T: (int, float)](name: str, default: T, parse: type[T]) -> T:
    """One positive numeric variable, or a failure that names it.

    The parse is guarded rather than wrapped in a broad catch: ``int`` and
    ``float`` raise only ``ValueError`` on a string, and that error arrives
    without saying *which* variable was wrong — the one thing you need to know
    when a process refuses to boot.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = parse(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not a {parse.__name__}") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """The resolved configuration. Frozen, because start-up values do not move."""

    #: ``None`` when no variable was set. Kept rather than raised so that
    #: everything which does not touch a database still imports.
    dsn: str | None
    max_window_days: int
    species_cache_ttl: float

    @classmethod
    def from_env(cls) -> Settings:
        if ENV_FILE.is_file():
            # Guarded rather than caught: `load_env` raises FileNotFoundError
            # for a missing file, and a missing dotenv is the normal case here
            # rather than an exceptional one.
            load_env(ENV_FILE, search=False, apply=True)
        dsn = os.environ.get(DSN_VARIABLE) or os.environ.get(FALLBACK_DSN_VARIABLE)
        return cls(
            dsn=dsn or None,
            max_window_days=_number(WINDOW_VARIABLE, 90, int),
            species_cache_ttl=_number(CACHE_TTL_VARIABLE, 300.0, float),
        )

    def database_url(self) -> str:
        """The DSN, or a failure that says how to supply one."""
        if self.dsn is None:
            raise RuntimeError(
                f"set {DSN_VARIABLE} (or {FALLBACK_DSN_VARIABLE}) to a "
                f"PostgreSQL URL, or write one into {ENV_FILE}; the schema "
                "walkthrough has the container command"
            )
        return self.dsn


#: Read once, at import. A module-level value rather than a lookup on every use
#: because the alternative is re-reading the environment on a request path, and
#: because the two decorators that consume it need it before any request exists.
SETTINGS = Settings.from_env()
