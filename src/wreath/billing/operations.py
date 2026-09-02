from __future__ import annotations

import math
import threading
import time
from types import MappingProxyType
from typing import Any

from ..health import HealthCheck
from ..metrics import Counters


class BillingOperationsUnhealthy(RuntimeError):
    pass


def _positive_number(value: float, field: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"billing {field} must be a positive number of seconds")
    return float(value)


def _count(value: int, field: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"billing {field} must be a {qualifier} integer")
    return value


class BillingOperations:
    __slots__ = (
        "_clock",
        "_counters",
        "_last_reconciliation",
        "_last_webhook",
        "_lock",
        "_reconciliation_unhealthy",
        "_values",
        "_webhook_unhealthy",
        "name",
    )

    def __init__(self, name: str, *, clock: Any = time.monotonic) -> None:
        if type(name) is not str or not name:
            raise ValueError("billing operations name must not be empty")
        if not callable(clock):
            raise TypeError("billing operations clock must be callable")
        now = float(clock())
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._last_webhook = now
        self._last_reconciliation = now
        self._webhook_unhealthy = False
        self._reconciliation_unhealthy = False
        self._values = {
            "webhooks_applied": 0,
            "webhook_failures": 0,
            "reconciliations_completed": 0,
            "reconciliation_failures": 0,
            "unknown_outcomes": 0,
            "dead_outcomes": 0,
            "webhook_lag_seconds": 0,
            "reconciliation_age_seconds": 0,
        }
        self._counters = Counters(
            subsystem="billing",
            instance=name,
            values=MappingProxyType(self._values),
            gauges=frozenset(
                {
                    "unknown_outcomes",
                    "dead_outcomes",
                    "webhook_lag_seconds",
                    "reconciliation_age_seconds",
                }
            ),
        )

    def webhook_applied(self) -> None:
        with self._lock:
            self._values["webhooks_applied"] += 1
            self._last_webhook = float(self._clock())
            self._webhook_unhealthy = False

    def webhook_failed(self) -> None:
        with self._lock:
            self._values["webhook_failures"] += 1
            self._webhook_unhealthy = True

    def reconciliation_completed(self) -> None:
        with self._lock:
            self._values["reconciliations_completed"] += 1
            self._last_reconciliation = float(self._clock())
            self._reconciliation_unhealthy = False

    def reconciliation_failed(self) -> None:
        with self._lock:
            self._values["reconciliation_failures"] += 1
            self._reconciliation_unhealthy = True

    def outcome_unknown(self, count: int = 1) -> None:
        count = _count(count, "unknown outcome count")
        with self._lock:
            self._values["unknown_outcomes"] += count

    def outcome_dead(self, count: int = 1) -> None:
        count = _count(count, "dead outcome count")
        with self._lock:
            self._values["dead_outcomes"] += count

    def outcome_resolved(self, *, unknown: int = 0, dead: int = 0) -> None:
        unknown = _count(unknown, "resolved unknown outcome count", allow_zero=True)
        dead = _count(dead, "resolved dead outcome count", allow_zero=True)
        with self._lock:
            if unknown > self._values["unknown_outcomes"]:
                raise ValueError("resolved count exceeds unresolved unknown outcomes")
            if dead > self._values["dead_outcomes"]:
                raise ValueError("resolved count exceeds unresolved dead outcomes")
            self._values["unknown_outcomes"] -= unknown
            self._values["dead_outcomes"] -= dead

    def _refresh_ages(self) -> None:
        now = float(self._clock())
        self._values["webhook_lag_seconds"] = int(max(0.0, now - self._last_webhook))
        self._values["reconciliation_age_seconds"] = int(
            max(0.0, now - self._last_reconciliation)
        )

    def counters(self) -> Counters:
        with self._lock:
            self._refresh_ages()
        return self._counters

    def alert(
        self,
        *,
        webhook_lag: float,
        reconciliation_age: float,
        timeout: float | None = 1.0,
    ) -> HealthCheck:
        webhook_limit = _positive_number(webhook_lag, "webhook_lag")
        reconciliation_limit = _positive_number(
            reconciliation_age, "reconciliation_age"
        )

        async def probe() -> dict[str, int]:
            with self._lock:
                self._refresh_ages()
                webhook_age = self._values["webhook_lag_seconds"]
                reconciliation_seconds = self._values["reconciliation_age_seconds"]
                unknown = self._values["unknown_outcomes"]
                dead = self._values["dead_outcomes"]
                webhook_failed = self._webhook_unhealthy
                reconciliation_failed = self._reconciliation_unhealthy
                webhook_failures = self._values["webhook_failures"]
                reconciliation_failures = self._values["reconciliation_failures"]
            failures: list[str] = []
            if webhook_age > webhook_limit:
                failures.append(f"webhook lag: {webhook_age}s")
            if reconciliation_seconds > reconciliation_limit:
                failures.append(f"reconciliation age: {reconciliation_seconds}s")
            if unknown:
                failures.append(f"unknown outcomes: {unknown}")
            if dead:
                failures.append(f"dead outcomes: {dead}")
            if webhook_failed:
                failures.append(f"webhook failures: {webhook_failures}")
            if reconciliation_failed:
                failures.append(f"reconciliation failures: {reconciliation_failures}")
            if failures:
                raise BillingOperationsUnhealthy("; ".join(failures))
            return {
                "webhook_lag_seconds": webhook_age,
                "reconciliation_age_seconds": reconciliation_seconds,
                "unknown_outcomes": unknown,
                "dead_outcomes": dead,
            }

        return HealthCheck(
            name=f"billing-{self.name}",
            probe=probe,
            critical=False,
            timeout=timeout,
        )


__all__ = ["BillingOperations", "BillingOperationsUnhealthy"]
