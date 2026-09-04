"""Durable-jobs and messaging coordinator primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from math import isfinite
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
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("attempt must be an integer >= 1")
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    for name, value in (("base", base), ("factor", factor), ("cap", cap), ("jitter", jitter)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative finite number")
    if jitter > 1:
        raise ValueError("jitter must be between 0 and 1")
    if kind == "fixed":
        delay = base
    elif kind == "linear":
        delay = base * attempt
    elif kind == "exp":
        # base * factor**(attempt-1), guarded against overflow at large attempts.
        exponent = min(attempt - 1, 32)
        try:
            delay = base * (factor**exponent)
        except OverflowError:
            delay = cap
    else:
        raise ValueError(f"unknown backoff kind: {kind!r}")
    delay = min(delay, cap)
    if jitter > 0.0:
        source = jitter_fn() if jitter_fn is not None else _default_jitter()
        if (
            isinstance(source, bool)
            or not isinstance(source, (int, float))
            or not isfinite(source)
            or not 0 <= source <= 1
        ):
            raise ValueError("jitter source must return a finite number between 0 and 1")
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
MAX_DURABLE_PAYLOAD: Final = 16 * 1024 * 1024


class PayloadTooLarge(ValueError):
    """A queue payload exceeded its transport or persistence bound."""


def check_notify_payload(payload: bytes) -> None:
    if len(payload) > MAX_NOTIFY_PAYLOAD:
        raise PayloadTooLarge(
            f"ephemeral payload is {len(payload)} bytes; "
            f"limit is {MAX_NOTIFY_PAYLOAD} — publish with durable=True instead"
        )


def check_durable_payload(size: int) -> None:
    if size > MAX_DURABLE_PAYLOAD:
        raise PayloadTooLarge(
            f"durable payload is {size} bytes; limit is {MAX_DURABLE_PAYLOAD} — "
            "store the body separately and enqueue its identifier instead"
        )


# Standard `minute hour day-of-month month day-of-week` with `*`, lists
# (`1,2`), ranges (`1-5`), and steps (`*/5`, `0-30/10`). No named months
# or special strings — enough for scheduled jobs without a cron dependency.


def CronSchedule(expression: str) -> Recurrence:
    """Create a UTC recurrence; use `Recurrence.cron` to select another zone."""
    return Recurrence.cron(expression)
