"""What every subsystem is counting, in one place a scrape can read.

Two dozen objects in this tree keep counters, each added with a written reason
an operator would want it: `jobs` counts run errors and lease expiries,
`messaging` counts unrouted publishes and doorbell reconnects, `entity` counts
names lost under load, `http_client` counts connection reuse, the pool counts
how deep its wait queue ever got. Every one of them was reachable only by
holding the object and knowing the method's name, which meant a dashboard saw
none of it.

`MessageBus.stats()` says why, for its own six counters: *"an exporter has to
know each name and gains nothing when one is added"*. That argument is right and
was never generalised — this module is the generalisation.

```python
from wreath import metrics

for reading in metrics.collect(app):
    print(reading.subsystem, reading.instance, reading.values)
```

**Collected by asking, not from a list.** Anything the application holds that
offers `counters()` contributes one reading, exactly as `Wreath.schema_components`
collects DDL claims. A hand-maintained registry would be one more place to
forget a new subsystem, and forgetting is the defect this exists to remove.

**Counters only, never a decision.** A reading is a number and a name. What a
number *means* — degraded, paging, fine — is a policy question, and putting a
threshold here would be the second place that decision lives.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["Counters", "CounterSource", "collect", "flatten"]


@dataclass(frozen=True, slots=True)
class Counters:
    """One subsystem instance's counters at a moment.

    `subsystem` names the kind (`"jobs"`), `instance` the one of them
    (`"work"`) — a deployment runs several queues and several buses, and a
    reading that cannot say which is a reading nobody can act on.

    Values are plain `int`. A counter that only rises and a gauge that moves
    both fit, and the distinction belongs to whatever renders them: Prometheus
    wants it, StatsD does not, and encoding it here would make this module
    care about an exposition format.
    """

    subsystem: str
    instance: str
    values: Mapping[str, int]

    def prefixed(self, namespace: str = "wreath") -> dict[str, int]:
        """`{namespace}_{subsystem}_{name}` for every value.

        The flat spelling, for a sink with no label dimension. `instance` is
        *not* folded in — a sink without labels cannot distinguish two queues,
        and silently summing them would be worse than the caller knowing it.
        """
        return {f"{namespace}_{self.subsystem}_{name}": value
                for name, value in self.values.items()}


@runtime_checkable
class CounterSource(Protocol):
    """Anything that can report its own counters.

    Structural, not nominal, like `wreath.services.Service`: a subsystem opts in
    by having the method, not by importing anything from here.
    """

    def counters(self) -> Counters:
        """This object's counters right now. Cheap, and never I/O."""
        ...


def _holders(app: Any) -> list[Any]:
    """Every registered object that might count something.

    The same registries `Wreath.schema_components` walks, plus the two it has no
    reason to: HTTP clients and databases own no tables and do own counters.
    Middleware is walked because the rate-limit, idempotency and session stores
    reach an application that way rather than through a registry of their own —
    the same reason the schema walk reaches them.
    """
    holders: list[Any] = []
    for attribute in (
        "_job_runners", "_message_buses", "_entity_registries",
        "_webhook_hubs", "_series_stores", "_http_clients", "_databases",
    ):
        registry = getattr(app, attribute, None)
        if isinstance(registry, Mapping):
            holders.extend(registry.values())
    for attribute in ("_global_middleware", "_middleware"):
        # The middleware registries hold `(priority, order, middleware)`.
        for item in getattr(app, attribute, ()) or ():
            holders.append(item[2] if isinstance(item, tuple) and len(item) > 2 else item)
    return holders


def collect(app: Any) -> tuple[Counters, ...]:
    """Every registered subsystem's counters, in registration order.

    A holder that raises is skipped rather than failing the scrape: a metrics
    read must not be able to take down the thing it is measuring, and one
    subsystem's bug must not blank every other subsystem's numbers. The
    omission is visible — that instance simply has no row.
    """
    readings: list[Counters] = []
    seen: set[tuple[str, str]] = set()
    for holder in _holders(app):
        for candidate in (holder, *getattr(holder, "counter_sources", ())):
            report = getattr(candidate, "counters", None)
            if report is None or not callable(report):
                continue
            try:
                reading = report()
            except Exception:  # noqa: BLE001 - see the docstring
                continue
            if not isinstance(reading, Counters):
                continue
            key = (reading.subsystem, reading.instance)
            if key in seen:
                continue
            seen.add(key)
            readings.append(reading)
    return tuple(readings)


def flatten(readings: Iterable[Counters], namespace: str = "wreath") -> dict[str, int]:
    """Every reading as one flat mapping, for a sink with no labels.

    Two instances of one subsystem collide on the flat name, so they are
    **summed** — which is the only honest thing a dimensionless sink can do with
    them, and is why `Counters.prefixed` leaves the choice to the caller.
    """
    out: dict[str, int] = {}
    for reading in readings:
        for name, value in reading.prefixed(namespace).items():
            out[name] = out.get(name, 0) + value
    return out
