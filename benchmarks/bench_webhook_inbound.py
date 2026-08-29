"""Interleaved whole-route benchmark for signed inbound webhook processing."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath import Wreath
from wreath.binding import validate
from wreath.testing import TestClient
from wreath.webhooks import (
    HMACWebhookSigner,
    HMACWebhookVerifier,
    LocalReplayStore,
    WebhookEnvelope,
)

_KEY = {"benchmark": b"wreath-webhook-inbound-benchmark-key"}
_BODY = b'{"value":1}'


@dataclass
class Payload:
    value: int


class LegacyVerifier(HMACWebhookVerifier):
    """Ablation restoring the redundant source-to-verifier normalization copy."""

    def _verify_normalized(
        self,
        *,
        body: bytes,
        headers: dict[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        copied = {key.lower(): value for key, value in headers.items()}
        return super()._verify_normalized(body=body, headers=copied, now=now)


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "raw_ns": samples,
    }


def _app(*, legacy: bool, generic_validation: bool, entries: int) -> Wreath:
    app = Wreath()
    verifier_type = LegacyVerifier if legacy else HMACWebhookVerifier
    source = app.webhooks("benchmark").source(
        "sender",
        path="/hooks/benchmark",
        verifier=verifier_type(_KEY, max_age=3600),
        replay=LocalReplayStore(max_entries=entries, ttl=3600),
    )

    @source.event("benchmark.event", payload=Payload)
    async def receive(context: Any, event: Payload) -> None:
        if event.value != 1:
            raise RuntimeError("webhook payload integrity failure")

    if generic_validation:
        source._handlers["benchmark.event"] = (
            lambda value, loc: validate(Payload, value, loc),
            receive,
        )
    return app


def _headers(prefix: str, count: int) -> list[dict[str, str]]:
    signer = HMACWebhookSigner(_KEY, key_id="benchmark")
    timestamp = datetime.now(UTC)
    result: list[dict[str, str]] = []
    for index in range(count):
        envelope = WebhookEnvelope(
            id=f"{prefix}-{index}",
            type="benchmark.event",
            version="1",
            timestamp=timestamp,
            content_type="application/json",
            body=_BODY,
        )
        result.append(
            {
                name.decode("ascii"): value.decode("ascii")
                for name, value in signer.headers(envelope)
            }
        )
    return result


async def _trial(count: int, trial: int) -> dict[str, float]:
    names = (
        "optimized_a",
        "generic_validation",
        "legacy_copy",
        "legacy_pipeline",
        "optimized_b",
    )
    apps = {
        "optimized_a": _app(legacy=False, generic_validation=False, entries=count + 1),
        "generic_validation": _app(legacy=False, generic_validation=True, entries=count + 1),
        "legacy_copy": _app(legacy=True, generic_validation=False, entries=count + 1),
        "legacy_pipeline": _app(legacy=True, generic_validation=True, entries=count + 1),
        "optimized_b": _app(legacy=False, generic_validation=False, entries=count + 1),
    }
    headers = {name: _headers(f"trial-{trial}-{name}", count) for name in names}
    totals = dict.fromkeys(names, 0)
    async with (
        TestClient(apps["optimized_a"]) as optimized_a,
        TestClient(apps["generic_validation"]) as generic_validation,
        TestClient(apps["legacy_copy"]) as legacy_copy,
        TestClient(apps["legacy_pipeline"]) as legacy_pipeline,
        TestClient(apps["optimized_b"]) as optimized_b,
    ):
        clients = {
            "optimized_a": optimized_a,
            "generic_validation": generic_validation,
            "legacy_copy": legacy_copy,
            "legacy_pipeline": legacy_pipeline,
            "optimized_b": optimized_b,
        }
        for index in range(count):
            order = names if (trial + index) % 2 == 0 else tuple(reversed(names))
            for name in order:
                started = perf_counter_ns()
                response = await clients[name].post(
                    "/hooks/benchmark", headers=headers[name][index], content=_BODY
                )
                totals[name] += perf_counter_ns() - started
                if response.status != 204:
                    raise RuntimeError(
                        f"inbound webhook integrity failure: {name}={response.status}"
                    )
    return {name: totals[name] / count for name in names}


async def run(count: int, trials: int) -> dict[str, Any]:
    samples = {
        "optimized_a": [],
        "generic_validation": [],
        "legacy_copy": [],
        "legacy_pipeline": [],
        "optimized_b": [],
    }
    for trial in range(trials):
        measured = await _trial(count, trial)
        for name, value in measured.items():
            samples[name].append(value)
    noise = [
        abs(left - right)
        for left, right in zip(samples["optimized_a"], samples["optimized_b"], strict=True)
    ]
    optimized = [
        (left + right) / 2
        for left, right in zip(samples["optimized_a"], samples["optimized_b"], strict=True)
    ]
    normalization_deltas = [
        legacy - selected
        for legacy, selected in zip(samples["legacy_copy"], optimized, strict=True)
    ]
    validation_deltas = [
        generic - selected
        for generic, selected in zip(samples["generic_validation"], optimized, strict=True)
    ]
    pipeline_deltas = [
        legacy - selected
        for legacy, selected in zip(samples["legacy_pipeline"], optimized, strict=True)
    ]
    noise_floor = abs(
        statistics.median(samples["optimized_a"]) - statistics.median(samples["optimized_b"])
    )
    resolution = 2 * noise_floor
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "count": count,
            "trials": trials,
            "payload_bytes": len(_BODY),
            "interleaved": True,
        },
        "aa_noise": {
            "optimized_a_raw_ns": samples["optimized_a"],
            "optimized_b_raw_ns": samples["optimized_b"],
            "absolute_trial_delta_ns": noise,
            "noise_floor_ns": noise_floor,
            "resolution_ns": resolution,
        },
        "results": {
            "optimized": _summary(optimized),
            "legacy_redundant_copy": _summary(samples["legacy_copy"]),
            "generic_validation": _summary(samples["generic_validation"]),
            "legacy_pipeline": _summary(samples["legacy_pipeline"]),
            "normalization_saved_ns": _summary(normalization_deltas),
            "validation_saved_ns": _summary(validation_deltas),
            "pipeline_saved_ns": _summary(pipeline_deltas),
            "normalization_resolved": (statistics.median(normalization_deltas) > resolution),
            "validation_resolved": (statistics.median(validation_deltas) > resolution),
            "pipeline_resolved": statistics.median(pipeline_deltas) > resolution,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2_000)
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
