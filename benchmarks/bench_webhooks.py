"""Measure signed webhook and bounded replay overhead with integrity checks."""

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

from wreath.binding import _body_validator, validate
from wreath.webhooks import (
    HMACWebhookSigner,
    HMACWebhookVerifier,
    LocalReplayStore,
    WebhookEnvelope,
    _format_timestamp,
    _parse_timestamp,
    _signature_base,
)

_KEY = {"benchmark": b"wreath-webhook-benchmark-key-material"}
_BODY = b'{"event":"benchmark","value":42}'
_TIMESTAMP = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


@dataclass
class _Payload:
    event: str
    value: int


_ENVELOPE = WebhookEnvelope(
    id="evt-benchmark",
    type="benchmark.event",
    version="1",
    timestamp=_TIMESTAMP,
    content_type="application/json",
    body=_BODY,
)


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "raw_ns": samples,
    }


def _measure(function: Any, iterations: int, trials: int) -> list[float]:
    samples: list[float] = []
    for _ in range(trials):
        start = perf_counter_ns()
        for _iteration in range(iterations):
            function()
        samples.append((perf_counter_ns() - start) / iterations)
    return samples


async def _measure_replay(iterations: int, trials: int) -> list[float]:
    samples: list[float] = []
    for trial in range(trials):
        store = LocalReplayStore(max_entries=iterations + 1, ttl=3600)
        start = perf_counter_ns()
        for index in range(iterations):
            claimed = await store.claim("benchmark", f"{trial}-{index}", now=float(index))
            if not claimed:
                raise RuntimeError("fresh replay key was rejected")
        samples.append((perf_counter_ns() - start) / iterations)
    return samples


def _aa_noise(iterations: int, trials: int) -> dict[str, Any]:
    def control() -> None:
        return None

    left = _measure(control, iterations, trials)
    right = _measure(control, iterations, trials)
    deltas = [abs(a - b) for a, b in zip(left, right, strict=True)]
    return {
        "left_raw_ns": left,
        "right_raw_ns": right,
        "absolute_delta_ns": deltas,
        "noise_floor_ns": max(deltas),
    }


async def run(iterations: int, trials: int) -> dict[str, Any]:
    signer = HMACWebhookSigner(_KEY, key_id="benchmark")
    verifier = HMACWebhookVerifier(_KEY, max_age=300)
    headers = dict(signer.headers(_ENVELOPE))

    def sign() -> tuple[tuple[bytes, bytes], ...]:
        return signer.headers(_ENVELOPE)

    def verify() -> WebhookEnvelope:
        return verifier.verify(body=_BODY, headers=headers, now=_TIMESTAMP)

    def source_verify() -> WebhookEnvelope:
        return verifier._verify_normalized(body=_BODY, headers=headers, now=_TIMESTAMP)

    raw_headers = list(headers.items())

    def normalize_headers() -> dict[bytes, bytes]:
        return {key.lower(): value for key, value in raw_headers}

    def source_headers_two_pass() -> dict[bytes, bytes]:
        if sum(len(name) + len(value) for name, value in raw_headers) > 16_384:
            raise RuntimeError("unexpected benchmark header limit")
        normalized: dict[bytes, bytes] = {}
        for name, value in raw_headers:
            normalized.setdefault(name.lower(), value)
        return normalized

    def source_headers_one_pass() -> dict[bytes, bytes]:
        total = 0
        normalized: dict[bytes, bytes] = {}
        for name, value in raw_headers:
            total += len(name) + len(value)
            if total > 16_384:
                raise RuntimeError("unexpected benchmark header limit")
            normalized.setdefault(name.lower(), value)
        return normalized

    def decode_identity() -> tuple[str, str, str]:
        return (
            headers[b"wreath-webhook-id"].decode("utf-8"),
            headers[b"wreath-webhook-type"].decode("utf-8"),
            headers[b"wreath-webhook-version"].decode("utf-8"),
        )

    decoded = {"event": "benchmark", "value": 42}
    compiled_validate = _body_validator(_Payload)

    def generic_validation() -> Any:
        return validate(_Payload, decoded, ("body",))

    def compiled_validation() -> Any:
        return compiled_validate(decoded, ("body",))

    timestamp = _format_timestamp(_TIMESTAMP)

    def signature_base() -> bytes:
        return _signature_base(timestamp, _ENVELOPE.id, _ENVELOPE.type, _ENVELOPE.body)

    verified = verify()
    if verified.body != _BODY or verified.id != _ENVELOPE.id:
        raise RuntimeError("webhook verification integrity failure")
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "iterations": iterations,
            "trials": trials,
            "payload_bytes": len(_BODY),
            "signature_profile": "wreath-v2-hmac-sha256",
            "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "implementation": "python-policy-stdlib-hmac",
        },
        "aa_noise": _aa_noise(iterations, trials),
        "results": {
            "header_normalization": _summary(_measure(normalize_headers, iterations, trials)),
            "source_headers_two_pass": _summary(
                _measure(source_headers_two_pass, iterations, trials)
            ),
            "source_headers_one_pass": _summary(
                _measure(source_headers_one_pass, iterations, trials)
            ),
            "identity_decode": _summary(_measure(decode_identity, iterations, trials)),
            "timestamp_parse": _summary(
                _measure(lambda: _parse_timestamp(timestamp), iterations, trials)
            ),
            "generic_payload_validation": _summary(
                _measure(generic_validation, iterations, trials)
            ),
            "compiled_payload_validation": _summary(
                _measure(compiled_validation, iterations, trials)
            ),
            "signature_base": _summary(_measure(signature_base, iterations, trials)),
            "hmac_sign": _summary(_measure(sign, iterations, trials)),
            "hmac_verify": _summary(_measure(verify, iterations, trials)),
            "source_verify": _summary(_measure(source_verify, iterations, trials)),
            "local_replay_claim": _summary(await _measure_replay(iterations, trials)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.iterations, args.trials) <= 0:
        parser.error("iteration and trial counts must be positive")
    result = asyncio.run(run(args.iterations, args.trials))
    print(json.dumps(result, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
