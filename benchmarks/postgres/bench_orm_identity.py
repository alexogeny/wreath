"""Measure the identity map: cost of a miss (allocate) versus a hit (reuse).

A hit still decodes every field -- it merges the row into the object already in
the map -- so this measures what identity costs, not what it skips.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib
import json
import platform
import statistics
import sys
import time
from typing import Any

from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import compile_select
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text


class Entity(Model, table="orm_bench_identity"):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


SETUP = (
    "DROP TABLE IF EXISTS orm_bench_identity",
    "CREATE TABLE orm_bench_identity (id bigint PRIMARY KEY, label text NOT NULL)",
)


class _Database:
    name = "bench"

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def acquire(self, workload: str) -> Any:
        return self._connection

    async def release(self, workload: str, connection: Any) -> None:
        return None


def _summary(samples: list[float], rows: int) -> dict[str, object]:
    ordered = sorted(samples)
    median = statistics.median(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "rows_per_second": rows / median,
        "raw_seconds": samples,
    }


async def run(args: argparse.Namespace) -> int:
    native = importlib.import_module("wreath._native._postgres")
    connection = await native.connect(args.dsn)
    try:
        for statement in SETUP:
            await connection.execute(statement)
        await connection.execute(
            "INSERT INTO orm_bench_identity SELECT v, 'label-' || v "
            f"FROM generate_series(1, {args.rows}) AS v"
        )
        registry = Registry(_Database(connection), [Entity], validate_schema="off")
        compiled = compile_select(registry, Entity.select().order_by(Entity.id))

        async def fetch(session: Session) -> Any:
            return await session._fetch_objects(connection, compiled, compiled.sql, ())

        # Miss: a new session each time, so every row allocates.
        miss: list[float] = []
        for _ in range(args.warmup + args.trials):
            session = Session(registry, "read")
            gc.collect()
            started = time.perf_counter()
            rows = await fetch(session)
            elapsed = time.perf_counter() - started
            if len(rows) != args.rows:
                raise RuntimeError("wrong row count")
            miss.append(elapsed)
            del rows, session
        miss = miss[args.warmup :]

        # Hit: one warm session, so every row finds its object already mapped.
        session = Session(registry, "read")
        held = await fetch(session)
        if len(held) != args.rows:
            raise RuntimeError("wrong row count")
        hit: list[float] = []
        for index in range(args.warmup + args.trials):
            gc.collect()
            started = time.perf_counter()
            rows = await fetch(session)
            elapsed = time.perf_counter() - started
            if any(row is not original for row, original in zip(rows, held, strict=True)):
                raise RuntimeError("identity map did not reuse its objects")
            if index >= args.warmup:
                hit.append(elapsed)
            del rows

        document = {
            "metadata": {
                "python": sys.version,
                "platform": platform.platform(),
                "implementation": native._implementation,
                "model_basicsize": Entity.__basicsize__,
                "rows": args.rows,
                "warmup": args.warmup,
                "trials": args.trials,
            },
            "results": {
                "identity_miss_allocates": _summary(miss, args.rows),
                "identity_hit_reuses": _summary(hit, args.rows),
            },
            "hit_over_miss_speedup": (statistics.median(miss) / statistics.median(hit)),
        }
        print(json.dumps(document, indent=2))
        return 0
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=10)
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
