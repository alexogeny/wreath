"""The load balancer's data plane: upstreams, selection, and passive health.

**Everything here is memory.** Selection, health and in-flight counts are read
and written on the request path, so none of it touches PostgreSQL -- that is
where *desired* state belongs (which upstreams should exist), not where a
request goes to find out where to send itself.

The whole structure is one process's. That is the deliberate first shape: with
`SO_REUSEPORT` each worker keeps its own table and converges through
LISTEN/NOTIFY, which keeps round-robin and EWMA correct -- each worker's view is
independently valid -- and leaves only *global* least-connections wrong, because
no worker sees the fleet's counts. A shared table is the answer to that and is a
second instance of a shape this tree already has (the Flight Recorder is an
mmap-backed ring with atomic slot claiming), not a new one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .._jobcore import compute_backoff

#: How much of the old average a new sample leaves behind. 0.2 is roughly a
#: 5-request window: fast enough that a newly-slow upstream is demoted within a
#: handful of requests, slow enough that one unlucky sample does not.
_EWMA_ALPHA = 0.2

#: What an upstream's latency is assumed to be before it has served anything.
#: Zero would make a cold upstream the most attractive one and every new member
#: would be stampeded the moment it joined.
_COLD_LATENCY = 0.050


@dataclass(slots=True)
class Upstream:
    """One origin this proxy can send to, and what is known about it right now."""

    url: str
    #: Requests currently in flight. The signal least-connections reads.
    inflight: int = 0
    #: Exponentially weighted mean response time, seconds.
    latency: float = _COLD_LATENCY
    #: Consecutive failures. Reset by any success.
    failures: int = 0
    #: Monotonic deadline before which this upstream is not selected. 0 means
    #: healthy. An ejected upstream is not *removed*: it has to be probed back,
    #: and removing it would lose the only record that it should return.
    ejected_until: float = 0.0
    total: int = 0

    def healthy(self, now: float) -> bool:
        return self.ejected_until <= now

    def score(self) -> float:
        """Lower is better. Latency times queue depth.

        Peak-EWMA rather than plain least-connections or plain least-latency:
        one upstream with two fast requests in flight should beat one with a
        single slow one, and neither signal alone says that.
        """
        return self.latency * (self.inflight + 1)


@dataclass(slots=True)
class Ejection:
    """When a failing upstream stops being chosen, and for how long.

    Passive health: the requests already flowing are the probe, so a failing
    upstream is noticed at the speed of real traffic rather than of a timer.

    Args:
        failures: Consecutive failures before ejection.
        seconds: Base cooldown; doubles per consecutive ejection up to `cap`.
        cap: Longest cooldown, so a long outage does not push retry into hours.
    """

    failures: int = 3
    seconds: float = 5.0
    cap: float = 60.0


class UpstreamPool:
    """A set of upstreams and a policy for choosing between them.

    Not a `wreath.services.Service` and not started: it holds no task and no
    connection. The HTTP clients that talk to these origins are owned by the
    caller, which is what keeps this object cheap enough to consult per request.
    """

    __slots__ = ("_cursor", "_ejection", "_policy", "_upstreams")

    def __init__(
        self,
        upstreams: list[Upstream],
        *,
        policy: str = "ewma",
        ejection: Ejection | None = None,
    ) -> None:
        if not upstreams:
            raise ValueError("an upstream pool needs at least one upstream")
        if policy not in ("ewma", "round-robin", "least-connections"):
            raise ValueError(f"unknown policy: {policy!r}")
        selected_ejection = ejection or Ejection()
        if selected_ejection.failures < 1:
            raise ValueError("ejection.failures must be at least 1")
        if selected_ejection.seconds <= 0:
            raise ValueError("ejection.seconds must be positive")
        if selected_ejection.cap <= 0:
            raise ValueError("ejection.cap must be positive")
        if selected_ejection.cap < selected_ejection.seconds:
            raise ValueError("ejection.cap must be at least ejection.seconds")
        self._upstreams = upstreams
        self._policy = policy
        self._ejection = selected_ejection
        self._cursor = 0

    @property
    def upstreams(self) -> list[Upstream]:
        return self._upstreams

    @property
    def policy(self) -> str:
        """How `choose` picks. Read by `serve()`, which compiles it into C."""
        return self._policy

    @property
    def ejection(self) -> Ejection:
        """When a failing upstream stops being chosen, and for how long."""
        return self._ejection

    def choose(
        self, now: float | None = None, exclude: frozenset[str] = frozenset()
    ) -> Upstream | None:
        """The upstream to send the next request to.

        **Every upstream ejected is not a failure to answer.** When nothing is
        healthy this returns the one whose cooldown ends soonest rather than
        raising: a proxy that refuses while every origin is briefly ejected turns
        a recoverable blip into an outage of its own, and the request it declines
        is the one that would have proved recovery.

        Args:
            now: Monotonic clock reading; defaults to the real one.
            exclude: URLs already tried for *this* request, so a retry moves on.

        Returns:
            The upstream to use, or None when `exclude` covers all of them.
        """
        now = time.monotonic() if now is None else now
        fallback: Upstream | None = None
        healthy: list[Upstream] = []
        untried: list[Upstream] = []
        for upstream in self._upstreams:
            if upstream.url in exclude:
                continue
            if fallback is None or upstream.ejected_until < fallback.ejected_until:
                fallback = upstream
            if upstream.healthy(now):
                healthy.append(upstream)
                if upstream.total == 0:
                    untried.append(upstream)
        if fallback is None:
            return None
        if not healthy:
            return fallback
        # Score only after every healthy upstream has one real measurement.
        if untried:
            self._cursor = (self._cursor + 1) % len(untried)
            return untried[self._cursor]
        if self._policy == "round-robin":
            self._cursor = (self._cursor + 1) % len(healthy)
            return healthy[self._cursor]
        if self._policy == "least-connections":
            return min(healthy, key=lambda upstream: upstream.inflight)
        return min(healthy, key=lambda upstream: upstream.score())

    def succeeded(self, upstream: Upstream, elapsed: float) -> None:
        """Record a completed request and fold its latency into the average."""
        upstream.failures = 0
        upstream.ejected_until = 0.0
        upstream.total += 1
        upstream.latency += _EWMA_ALPHA * (elapsed - upstream.latency)

    def failed(self, upstream: Upstream, now: float | None = None) -> None:
        """Record a failure, ejecting the upstream once it has failed enough."""
        now = time.monotonic() if now is None else now
        upstream.failures += 1
        if upstream.failures < self._ejection.failures:
            return
        over = upstream.failures - self._ejection.failures
        cooldown = compute_backoff(
            over + 1,
            base=self._ejection.seconds,
            cap=self._ejection.cap,
        )
        upstream.ejected_until = now + cooldown

    def stats(self) -> dict[str, int]:
        """Live counters. `dict[str, int]`, per the naming rules."""
        now = time.monotonic()
        return {
            "upstreams": len(self._upstreams),
            "healthy": sum(1 for upstream in self._upstreams if upstream.healthy(now)),
            "inflight": sum(upstream.inflight for upstream in self._upstreams),
            "requests": sum(upstream.total for upstream in self._upstreams),
        }

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        """What each upstream looks like now -- current contents, so `snapshot`."""
        now = time.monotonic()
        return tuple(
            {
                "url": upstream.url,
                "healthy": upstream.healthy(now),
                "inflight": upstream.inflight,
                "latency_ms": round(upstream.latency * 1000, 2),
                "failures": upstream.failures,
                "requests": upstream.total,
            }
            for upstream in self._upstreams
        )
