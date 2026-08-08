"""Everything the application reads before it serves a request.

Configuration and state are different things and wreath keeps them apart. This
module is the start-up half: values fixed for the life of the process, read once
at import, never consulted again. Anything that changes while the application
runs belongs on ``request.state`` or ``app.state`` instead.

Each variable is here because an operator would genuinely set it. Three shape
what the application answers:

* ``CAMERA_TRAP_DSN`` — the database. No default, because guessing at
  ``localhost`` and connecting to the wrong one is worse than refusing to start.
* ``CAMERA_TRAP_MAX_WINDOW_DAYS`` — the widest span of sightings one request may
  ask for. This is the only thing standing between an anonymous caller and a
  scan of 140,000 rows, and how wide it can safely be depends on the machine.
* ``CAMERA_TRAP_SPECIES_CACHE_TTL`` — how long the cached species list may be
  stale. A controlled vocabulary changes a few times a year; the cache is also
  cleared by any write to the table, so this is the backstop rather than the
  mechanism.

Four more are deployment facts rather than behaviour: ``CAMERA_TRAP_SESSION_SECRET``
and ``CAMERA_TRAP_SESSION_INSECURE`` for the cookie, and
``CAMERA_TRAP_MEDIA_ROOT`` and ``CAMERA_TRAP_MEDIA_SECRET`` for the object store
that holds uploaded cards. The two secrets are separate keys on purpose; see
``DEVELOPMENT_MEDIA_SECRET``.

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
import warnings
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

SESSION_SECRET_VARIABLE = "CAMERA_TRAP_SESSION_SECRET"
SESSION_INSECURE_VARIABLE = "CAMERA_TRAP_SESSION_INSECURE"

MEDIA_ROOT_VARIABLE = "CAMERA_TRAP_MEDIA_ROOT"
MEDIA_SECRET_VARIABLE = "CAMERA_TRAP_MEDIA_SECRET"

#: The shortest secret that is worth calling one. `SessionPolicy` signs with
#: HMAC-SHA256, so a short secret is not a shorter signature -- it is a smaller
#: search space, and the failure is silent.
MIN_SECRET_LENGTH = 32

#: Used when `CAMERA_TRAP_SESSION_SECRET` is unset, so that `wreath serve`, the
#: seeder and the example's own tests all run on a fresh clone with no setup.
#:
#: **It says what it is in its own value**, which is the entire design. The
#: alternatives were both worse: refusing to start makes the example's first
#: experience an error message about a variable a reader has no opinion about
#: yet, and generating a random secret at import works perfectly on one process
#: and silently signs out every user the moment a second replica starts or the
#: first one restarts -- a bug that reproduces only where nobody develops.
#:
#: A public constant is not a weaker secret than a shared default; it is the
#: same thing with the pretence removed. Anything real sets the variable, and
#: `Settings.session_secret` says so on the way past.
# wreath-audit: allow hardcoded-secret -- the documented fallback above
DEVELOPMENT_SECRET = "camera-trap-development-secret-not-for-real-deployments"

#: The presign secret's development fallback. Same reasoning as
#: `DEVELOPMENT_SECRET`, and a *different constant* on purpose.
#:
#: Signing keys do not get shared across purposes. The session secret proves
#: "this cookie came from us"; this one proves "this upload URL came from us".
#: One key for both means a leaked presign secret forges sessions, and a rotated
#: session secret invalidates every URL a field team is holding — two failures
#: that have nothing to do with each other, welded together to save a variable.
# wreath-audit: allow hardcoded-secret -- the documented fallback above
DEVELOPMENT_MEDIA_SECRET = "camera-trap-development-presign-secret-not-for-real-deployments"

#: Where `LocalObjectStore` keeps its bytes when nothing says otherwise.
#: Alongside the package rather than in a temp directory, so an upload survives
#: a restart and a reader can go and look at what landed.
DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[1] / "media"


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

    #: `None` when unset, for the same reason as `dsn`: a session secret with a
    #: default is a session secret every deployment shares.
    session_key: str | None

    #: `Secure` on the session cookie. True everywhere except a plain-HTTP
    #: developer machine, and inverted in the variable name so that the
    #: dangerous setting is the one you have to type.
    session_secure: bool

    #: Where uploaded card archives and the images unpacked out of them live.
    #:
    #: Defaulted on the field, unlike `dsn`, because there is a right answer
    #: that needs no operator opinion: a directory beside the package. Guessing
    #: a database is dangerous; guessing a scratch directory is not.
    media_root: Path = DEFAULT_MEDIA_ROOT

    #: `None` when unset, like `session_key`, and for the same reason.
    media_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        if ENV_FILE.is_file():
            # Guarded rather than caught: `load_env` raises FileNotFoundError
            # for a missing file, and a missing dotenv is the normal case here
            # rather than an exceptional one.
            load_env(ENV_FILE, search=False, apply=True)
        dsn = os.environ.get(DSN_VARIABLE) or os.environ.get(FALLBACK_DSN_VARIABLE)
        insecure = os.environ.get(SESSION_INSECURE_VARIABLE, "").strip().lower()
        return cls(
            dsn=dsn or None,
            max_window_days=_number(WINDOW_VARIABLE, 90, int),
            species_cache_ttl=_number(CACHE_TTL_VARIABLE, 300.0, float),
            session_key=os.environ.get(SESSION_SECRET_VARIABLE) or None,
            session_secure=insecure not in {"1", "true", "yes", "on"},
            media_root=Path(os.environ.get(MEDIA_ROOT_VARIABLE) or DEFAULT_MEDIA_ROOT),
            media_key=os.environ.get(MEDIA_SECRET_VARIABLE) or None,
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

    def session_secret(self) -> str:
        """The session-signing secret, warning once if it is the public one.

        A supplied secret that is too short is refused rather than accepted:
        that is someone who meant to set it and got it wrong, and silently
        accepting eight characters is how a session cookie ends up forgeable.
        An *absent* secret falls back to `DEVELOPMENT_SECRET` with a warning
        naming the variable — see that constant for why the trade goes this way
        round.
        """
        if self.session_key is None:
            warnings.warn(
                f"{SESSION_SECRET_VARIABLE} is unset; signing sessions with the "
                f"public development secret. Set it to a random string of at "
                f"least {MIN_SECRET_LENGTH} characters before serving this to "
                "anyone: `python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'`",
                RuntimeWarning,
                stacklevel=2,
            )
            return DEVELOPMENT_SECRET
        if len(self.session_key) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"{SESSION_SECRET_VARIABLE} is {len(self.session_key)} characters; "
                f"at least {MIN_SECRET_LENGTH} are needed"
            )
        return self.session_key

    def media_secret(self) -> str:
        """The presign-signing secret, warning once if it is the public one.

        Same shape as `session_secret` and the same trade, because the reasoning
        is the same: a reader who has not set it should still be able to run an
        upload end to end, and should be told they are doing so with a key that
        is printed in the source.

        What a short secret costs here is worth being concrete about. The
        signature is what stops a caller who can reach the upload endpoint from
        minting a URL for somebody else's key — so a guessable secret is not a
        weaker signature, it is an object store with no authorization on it at
        all.
        """
        if self.media_key is None:
            warnings.warn(
                f"{MEDIA_SECRET_VARIABLE} is unset; signing upload URLs with the "
                f"public development secret. Set it to a random string of at "
                f"least {MIN_SECRET_LENGTH} characters before serving this to "
                "anyone: `python -c 'import secrets; "
                "print(secrets.token_urlsafe(32))'`",
                RuntimeWarning,
                stacklevel=2,
            )
            return DEVELOPMENT_MEDIA_SECRET
        if len(self.media_key) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"{MEDIA_SECRET_VARIABLE} is {len(self.media_key)} characters; "
                f"at least {MIN_SECRET_LENGTH} are needed"
            )
        return self.media_key


#: Read once, at import. A module-level value rather than a lookup on every use
#: because the alternative is re-reading the environment on a request path, and
#: because the two decorators that consume it need it before any request exists.
SETTINGS = Settings.from_env()
