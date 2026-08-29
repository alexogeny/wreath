"""Durable-jobs and messaging coordinator primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Final

from ._pgname import validate_identifier as validate_identifier
from .temporal import Recurrence

READY: Final = "ready"
LEASED: Final = "leased"
DONE: Final = "done"
FAILED: Final = "failed"
DEAD: Final = "dead"

STATES: Final = frozenset({READY, LEASED, DONE, FAILED, DEAD})

_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    READY: frozenset({LEASED}),
    LEASED: frozenset({DONE, READY, DEAD}),
    FAILED: frozenset({READY, DEAD}),
    DONE: frozenset(),
    DEAD: frozenset(),
}


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
        delay = base * (factor**exponent)
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


# Standard `minute hour day-of-month month day-of-week` with `*`, lists
# (`1,2`), ranges (`1-5`), and steps (`*/5`, `0-30/10`). No named months
# or special strings — enough for scheduled jobs without a cron dependency.


def CronSchedule(expression: str) -> Recurrence:
    """The old spelling of `Recurrence.cron`, kept so existing callers still run.

    The cron parser used to live here, and `wreath.temporal.Recurrence` now owns
    it -- along with the zone this spelling could never carry. Everything a
    caller of this got before, it still gets: the returned object answers
    `matches(minute=..., hour=...)` identically, and a bad expression still
    raises a `ValueError` (`RecurrenceError` is one).

    What it gets *now* is UTC, because that is what it always meant. Reach for
    `Recurrence.cron(expression, tz=...)` to say otherwise.
    """
    return Recurrence.cron(expression)
