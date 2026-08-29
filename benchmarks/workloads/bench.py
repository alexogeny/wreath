"""In-process latency harness for the workload shapes.

Drives each shape through the app's ASGI boundary with the in-process
TestClient and reports median/p95/p99 per shape. This is a micro-latency
harness for relative comparison across shapes; a full throughput
comparison uses an external ASGI server and load generator. Every trial is
retained so the distribution — not a single number — is what is reported.

    uv run python -m benchmarks.workloads.bench --iterations 5000
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from wreath.testing import TestClient

from ._fakepg import FakePostgres
from .app import build_app

SHAPES: list[tuple[str, str, str, dict]] = [
    ("small-json", "GET", "/json", {}),
    ("plaintext", "GET", "/plaintext", {}),
    ("point-read", "GET", "/widget/1", {}),
    ("fan-out-8", "GET", "/widgets?queries=8", {}),
    ("template", "GET", "/quotations", {}),
    ("snapshot", "GET", "/config/greeting", {}),
]


async def _drive(client: TestClient, method: str, path: str, kwargs: dict) -> None:
    if method == "GET":
        await client.get(path, **kwargs)
    else:
        await client.request(method, path, **kwargs)


async def _bench(iterations: int, warmup: int) -> None:
    server = FakePostgres()
    dsn = await server.start()
    app = build_app(dsn)
    print(f"iterations={iterations} warmup={warmup}")
    async with TestClient(app) as client:
        for name, method, path, kwargs in SHAPES:
            for _ in range(warmup):
                await _drive(client, method, path, kwargs)
            samples: list[float] = []
            for _ in range(iterations):
                start = time.perf_counter()
                await _drive(client, method, path, kwargs)
                samples.append((time.perf_counter() - start) * 1e6)
            samples.sort()
            median = statistics.median(samples)
            p95 = samples[int(len(samples) * 0.95)]
            p99 = samples[int(len(samples) * 0.99)]
            print(f"  {name:<12} median={median:8.2f}us  p95={p95:8.2f}us  p99={p99:8.2f}us")
    await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(_bench(args.iterations, args.warmup))


if __name__ == "__main__":
    main()
