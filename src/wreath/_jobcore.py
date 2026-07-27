"""Pure-Python core for the durable-jobs and messaging coordinator.

Everything here is deterministic, allocation-light, and free of I/O so it can be
unit-tested without a database and, later, replaced by a native `_queue`
accelerator behind a byte-identical twin (see `docs/plans` / design 01).

TODO(native-queue): the state-machine validator, backoff arithmetic, and dedup
hashing are the concerns design 01 earmarks for `_native/_queue/` (envelope /
jobstate / backoff / dedup). They live in pure Python for this cut; a native
fast-path plus pure twin can drop in without changing the coordinator API.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Final

# --- job/message lifecycle state machine -----------------------------------
#
# `ready` -> `leased` (a worker claims it) -> `done` | `failed` (retry,
# back to `ready`) | `dead` (attempts exhausted). `leased` -> `ready` is
# the lease-expiry reclaim path.
#
# This table is the *reference* for that lifecycle, not a runtime gate. The
# coordinators enforce it in SQL -- every transition is an UPDATE with
# `WHERE id=$1 AND fence=$2`, so a fenced or stale worker's move simply
# matches no row -- which is the only place it can be enforced correctly, since
# two workers can disagree about the current state but not about what the row
# says. `valid_transition`/`check_transition` exist for callers reasoning about
# the machine (and for the tests that pin it); this comment used to claim they
# were checked on every worker transition, and nothing called them.

READY: Final = "ready"
LEASED: Final = "leased"
DONE: Final = "done"
FAILED: Final = "failed"
DEAD: Final = "dead"

STATES: Final = frozenset({READY, LEASED, DONE, FAILED, DEAD})

_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    READY: frozenset({LEASED}),
    # A leased item completes, goes back to ready for retry, or dies.
    LEASED: frozenset({DONE, READY, DEAD}),
    # Terminal-ish: failed is a transient label a retry leaves via ready; done
    # and dead are absorbing.
    FAILED: frozenset({READY, DEAD}),
    DONE: frozenset(),
    DEAD: frozenset(),
}


def validate_identifier(value: str, kind: str) -> str:
    """Validate a bounded SQL-safe identifier (queue/schema/channel/task name).

    The shared rule for every config-time name the jobs and messaging
    coordinators derive Postgres object names and LISTEN/NOTIFY channels from:
    1..63 bytes, each character an ASCII alphanumeric or `_`/`$`. `kind`
    names the identifier in the error so callers get an actionable message.
    """
    if not value or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{kind} must be 1..63 bytes: {value!r}")
    for character in value:
        # ASCII alphanumerics only. `str.isalnum()` is true for `café`, `½`, and
        # Arabic-Indic digits, none of which are what "SQL-safe identifier"
        # means here -- these names are interpolated into DDL and derived into
        # LISTEN/NOTIFY channels, where the encoding assumptions are the
        # server's, not Python's.
        if not (character.isascii() and (character.isalnum() or character in "_$")):
            raise ValueError(f"invalid {kind} character {character!r} in {value!r}")
    return value


class TransitionError(ValueError):
    """An illegal job/message state transition was attempted."""


def valid_transition(old: str, new: str) -> bool:
    """Return whether `old -> new` is a legal lifecycle move."""
    if old not in STATES or new not in STATES:
        return False
    return new in _TRANSITIONS[old]


def check_transition(old: str, new: str) -> None:
    """Raise `TransitionError` unless `old -> new` is legal."""
    if not valid_transition(old, new):
        raise TransitionError(f"illegal job transition: {old!r} -> {new!r}")


# --- retry backoff ----------------------------------------------------------

BackoffKind = str  # "exp" | "linear" | "fixed"


def compute_backoff(
    attempt: int,
    *,
    kind: BackoffKind = "exp",
    base: float = 1.0,
    factor: float = 2.0,
    cap: float = 3600.0,
    jitter: float = 0.0,
    jitter_fn: Callable[[], float] | None = None,
) -> float:
    """Seconds to wait before retry `attempt` (1-based).

    Deterministic given `jitter_fn` (injected in tests). `jitter` is the
    fraction of the computed delay added as bounded random jitter, so a thundering
    herd of same-age failures does not retry in lockstep.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if kind == "fixed":
        delay = base
    elif kind == "linear":
        delay = base * attempt
    elif kind == "exp":
        # base * factor**(attempt-1), guarded against overflow at large attempts.
        exponent = min(attempt - 1, 32)
        delay = base * (factor ** exponent)
    else:
        raise ValueError(f"unknown backoff kind: {kind!r}")
    delay = min(delay, cap)
    if jitter > 0.0:
        source = jitter_fn() if jitter_fn is not None else _default_jitter()
        # source in [0,1); scale to +/- jitter fraction of the delay.
        delay += delay * jitter * (source * 2.0 - 1.0)
    return max(0.0, delay)


def _default_jitter() -> float:
    # Imported lazily so importing this module never trips a "no top-level
    # random" lint, and so tests always inject a deterministic jitter_fn.
    import random

    return random.random()


# --- idempotency / dedup ----------------------------------------------------


def dedup_key(scope: str, key: str) -> str:
    """A stable idempotency key for `(scope, key)`.

    Hashed rather than concatenated so an arbitrary user key can't collide with a
    different scope's key by sharing a delimiter, and so the stored key is a
    bounded fixed width regardless of user input length.
    """
    digest = hashlib.blake2s(
        b"%b\x00%b" % (scope.encode("utf-8"), key.encode("utf-8")), digest_size=16
    )
    return digest.hexdigest()


# --- NOTIFY payload bounds --------------------------------------------------

# PostgreSQL caps a NOTIFY payload at 8000 bytes; leave headroom for the channel
# envelope so an ephemeral publish that would be truncated is rejected up front
# and routed to the durable path instead.
MAX_NOTIFY_PAYLOAD: Final = 7000


class PayloadTooLarge(ValueError):
    """An ephemeral publish payload exceeded the NOTIFY size bound."""


def check_notify_payload(payload: bytes) -> None:
    if len(payload) > MAX_NOTIFY_PAYLOAD:
        raise PayloadTooLarge(
            f"ephemeral payload is {len(payload)} bytes; "
            f"limit is {MAX_NOTIFY_PAYLOAD} — publish with durable=True instead"
        )


# --- minimal 5-field cron ---------------------------------------------------
#
# Standard `minute hour day-of-month month day-of-week` with `*`, lists
# (`1,2`), ranges (`1-5`), and steps (`*/5`, `0-30/10`). No named months
# or special strings — enough for scheduled jobs without a cron dependency.


class CronSchedule:
    """A parsed 5-field cron expression with a `next_after` computation."""

    __slots__ = ("_expr", "_minute", "_hour", "_dom", "_month", "_dow")

    _BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {len(fields)}: {expression!r}"
            )
        self._expr = expression
        sets = [
            _parse_cron_field(field, low, high, wrap=index == 4)
            for index, (field, (low, high)) in enumerate(
                zip(fields, self._BOUNDS, strict=True)
            )
        ]
        self._minute, self._hour, self._dom, self._month, self._dow = sets

    @property
    def expression(self) -> str:
        return self._expr

    def matches(self, *, minute: int, hour: int, day: int, month: int, weekday: int) -> bool:
        """Whether a wall-clock instant matches (weekday: Monday=0..Sunday=6 -> cron Sun=0)."""
        cron_dow = (weekday + 1) % 7  # Python Mon=0 -> cron Sun=0
        if minute not in self._minute or hour not in self._hour or month not in self._month:
            return False
        # Vixie-cron semantics: when both day-of-month and day-of-week are
        # restricted, either matching is sufficient; otherwise both must match.
        dom_restricted = len(self._dom) != 31
        dow_restricted = len(self._dow) != 7
        dom_ok = day in self._dom
        dow_ok = cron_dow in self._dow
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def _parse_cron_field(
    field: str, low: int, high: int, *, wrap: bool = False
) -> frozenset[int]:
    """Parse one cron field into the set of values it matches.

    `wrap` is the day-of-week field, where every crontab accepts **7** as a
    second spelling of Sunday (`0`). Refusing it made `0 0 * * 7` -- a form
    people copy straight out of a crontab -- a startup error.
    """
    if wrap:
        high = 7
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        body = part
        if "/" in part:
            body, _, step_text = part.partition("/")
            step = int(step_text)
            if step < 1:
                raise ValueError(f"cron step must be >= 1: {part!r}")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_text, _, end_text = body.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(body)
        if start < low or end > high or start > end:
            raise ValueError(f"cron field out of range [{low},{high}]: {part!r}")
        values.update(range(start, end + 1, step))
    if wrap:
        values = {0 if value == 7 else value for value in values}
    return frozenset(values)
