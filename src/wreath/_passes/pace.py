"""Pacing: how much of the machine a pass is allowed to be.

The failure this exists to prevent has a shape everyone recognises. Every
layer's queue is inside its own limit, every dashboard is green, and p99 is
thirty seconds because each request spent it waiting for a connection a
background job was holding. The backfill's own numbers look excellent. It is the
fastest backfill anyone ever ran and it took the site down.

The strongest control here is not a controller at all. The compare-and-swap on
the ledger row means a pass has one writer at a time, so **a pass is one
connection** -- against a write pool of ten that is a hard ten-percent ceiling
before any policy runs at all. Say it out loud, because it is the part that
still holds when the policy is misconfigured.

On top of that sits a duty cycle, and it ships in the first release rather than
after it: a walk with no pacing is the failure above, and the primitive must not
have a version in which that state exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DutyCycle:
    """Let the pass have *fraction* of wall time, and sleep for the rest.

    After a chunk that took ``d`` seconds, sleep ``d * (1/fraction - 1)``. At the
    default 0.25 that is three seconds of rest per second of work.

    It needs no measurement to justify, which is the point of choosing it first:
    the bound is arithmetic rather than empirical, it degrades in the right
    direction (a chunk made slower by the pressure the pass is causing sleeps
    longer, so the pass's own share shrinks), and its worst case is that the walk
    takes four times as long -- a cost paid in a dimension nobody is holding a
    connection open for.
    """

    fraction: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 < self.fraction <= 1.0:
            raise ValueError(
                f"DutyCycle fraction must be in (0, 1]; got {self.fraction!r}. "
                "A pass with no pacing is the failure this exists to prevent."
            )

    def rest_after(self, elapsed: float) -> float:
        """Seconds to sleep after a chunk that took *elapsed* seconds."""
        if elapsed <= 0.0:
            return 0.0
        return elapsed * (1.0 / self.fraction - 1.0)

    @property
    def reason(self) -> str:
        """What the ledger records while this policy is holding the pass back."""
        return f"duty cycle {self.fraction:g}"


__all__ = ["DutyCycle"]
