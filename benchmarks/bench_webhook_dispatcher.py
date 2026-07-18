"""Measure dispatcher policy, backlog drain, outcomes, work counts, and RSS."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import resource
import statistics
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath.webhooks import OutboxDelivery, WebhookDeliveryResult, WebhookDispatcher


class MemoryOutbox:
    def __init__(self, deliveries: list[OutboxDelivery]) -> None:
        self.pending = deque(deliveries)
        self.leased: list[str] = []
        self.sending: list[str] = []
        self.delivered: list[str] = []
        self.retry: list[str] = []
        self.failed: list[str] = []
        self.unknown: list[str] = []

    async def claim_due(self, session: Any, **options: Any) -> OutboxDelivery | None:
        if not self.pending:
            return None
        delivery = self.pending.popleft()
        self.leased.append(delivery.delivery_id)
        return delivery

    async def mark_sending(self, session: Any, delivery: OutboxDelivery) -> None:
        self.sending.append(delivery.delivery_id)

    async def mark_delivered(
        self, session: Any, delivery: OutboxDelivery, *, status: int
    ) -> None:
        self.delivered.append(delivery.delivery_id)

    async def mark_retry(self, session: Any, delivery: OutboxDelivery, **data: Any) -> None:
        self.retry.append(delivery.delivery_id)

    async def mark_failed(self, session: Any, delivery: OutboxDelivery, **data: Any) -> None:
        self.failed.append(delivery.delivery_id)

    async def mark_unknown(self, session: Any, delivery: OutboxDelivery, **data: Any) -> None:
        self.unknown.append(delivery.delivery_id)


class Destination:
    def __init__(self, outcome: str, status: int | None) -> None:
        self.outcome = outcome
        self.status = status
        self.observed: list[str] = []

    async def _send_envelope(self, envelope: Any, *, key_id: str) -> WebhookDeliveryResult:
        self.observed.append(envelope.id)
        failure = "Timeout" if self.outcome == "unknown" else None
        return WebhookDeliveryResult(
            self.outcome, envelope.id, status=self.status, failure=failure
        )


def _delivery(index: int) -> OutboxDelivery:
    return OutboxDelivery(
        delivery_id=f"delivery-{index}",
        event_id=f"event-{index}",
        destination="receiver",
        event_type="benchmark.event",
        timestamp=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        version="1",
        body=b'{"value":1}',
        content_type="application/json",
        key_id="benchmark",
        attempts=1,
        fencing_token=1,
    )


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "raw_ns": samples,
    }


async def _drain(count: int, outcome: str, status: int | None) -> tuple[float, dict[str, int]]:
    outbox = MemoryOutbox([_delivery(index) for index in range(count)])
    destination = Destination(outcome, status)
    dispatcher = WebhookDispatcher(
        outbox,  # type: ignore[arg-type]
        {"receiver": destination},  # type: ignore[dict-item]
        worker_id="benchmark",
        max_attempts=2,
    )
    started = perf_counter_ns()
    while await dispatcher.run_once(None) is not None:
        pass
    elapsed = (perf_counter_ns() - started) / count
    accounted = (
        len(outbox.delivered)
        + len(outbox.retry)
        + len(outbox.failed)
        + len(outbox.unknown)
    )
    if accounted != count or len(destination.observed) != count:
        raise RuntimeError("dispatcher delivery counts do not account for committed intents")
    return elapsed, {
        "intents": count,
        "receiver_observed": len(destination.observed),
        "delivered": len(outbox.delivered),
        "retry": len(outbox.retry),
        "failed": len(outbox.failed),
        "unknown": len(outbox.unknown),
    }


async def _samples(
    count: int, trials: int, outcome: str, status: int | None
) -> tuple[list[float], dict[str, int]]:
    samples: list[float] = []
    counts: dict[str, int] = {}
    for _ in range(trials):
        elapsed, counts = await _drain(count, outcome, status)
        samples.append(elapsed)
    return samples, counts


async def _aa(count: int, trials: int) -> dict[str, Any]:
    left, _ = await _samples(count, trials, "delivered", 204)
    right, _ = await _samples(count, trials, "delivered", 204)
    deltas = [abs(a - b) for a, b in zip(left, right, strict=True)]
    return {
        "left_raw_ns": left,
        "right_raw_ns": right,
        "absolute_delta_ns": deltas,
        "noise_floor_ns": max(deltas),
    }


async def run(count: int, trials: int) -> dict[str, Any]:
    workloads = {
        "success": ("delivered", 204),
        "rate_limited_429": ("failed", 429),
        "unavailable_503": ("failed", 503),
        "timeout_unknown": ("unknown", None),
        "permanent_400": ("failed", 400),
    }
    results: dict[str, Any] = {}
    for name, (outcome, status) in workloads.items():
        samples, counts = await _samples(count, trials, outcome, status)
        results[name] = {**_summary(samples), "counts": counts}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "count": count,
            "trials": trials,
            "rss_unit": "KiB on Linux, bytes on macOS",
            "peak_rss": rss,
            "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        },
        "aa_noise": await _aa(count, trials),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.count, args.trials) <= 0:
        parser.error("count and trials must be positive")
    result = asyncio.run(run(args.count, args.trials))
    print(json.dumps(result, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
