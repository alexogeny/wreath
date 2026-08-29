"""Benchmark Wreath column-batch decoding against Slice 3 and PostgreSQL drivers."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any


def _native_backend() -> Any:
    try:
        return importlib.import_module("wreath._native._postgres")
    except ImportError as error:
        raise RuntimeError("build the native PostgreSQL extension first") from error


def _query(rows: int) -> str:
    return (
        "select value::int4 as number, (value % 2 = 0)::bool as enabled, "
        "value::text as label from generate_series(1, "
        f"{rows}) as value"
    )


async def _measure(
    operation: Callable[[], Awaitable[None]], warmup: int, trials: int
) -> list[float]:
    for _ in range(warmup):
        await operation()
    samples = []
    for _ in range(trials):
        started = time.perf_counter()
        await operation()
        samples.append(time.perf_counter() - started)
    return samples


def _verify(rows: list[Any], count: int) -> None:
    if len(rows) != count:
        raise RuntimeError(f"expected {count} rows, received {len(rows)}")
    first = rows[0]
    last = rows[-1]
    if tuple(first) != (1, False, "1") or tuple(last) != (
        count,
        count % 2 == 0,
        str(count),
    ):
        raise RuntimeError("driver returned incorrect decoded values")


async def _wreath_samples(
    backend: Any,
    dsn: str,
    sql: str,
    rows: int,
    warmup: int,
    trials: int,
    *,
    batch: bool,
) -> list[float]:
    previous = backend.Connection._batch_decode
    backend.Connection._batch_decode = batch
    connection = await backend.connect(dsn)
    try:
        _verify(await connection.fetch(sql), rows)

        async def operation() -> None:
            _verify(await connection.fetch(sql), rows)

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()
        backend.Connection._batch_decode = previous


async def _asyncpg_samples(dsn: str, sql: str, rows: int, warmup: int, trials: int) -> list[float]:
    asyncpg: Any = importlib.import_module("asyncpg")
    connection = await asyncpg.connect(dsn)
    try:

        async def operation() -> None:
            _verify(await connection.fetch(sql), rows)

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()


async def _psycopg3_samples(dsn: str, sql: str, rows: int, warmup: int, trials: int) -> list[float]:
    psycopg: Any = importlib.import_module("psycopg")
    connection = await psycopg.AsyncConnection.connect(dsn)
    try:

        async def operation() -> None:
            async with connection.cursor() as cursor:
                await cursor.execute(sql)
                _verify(await cursor.fetchall(), rows)

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()


async def _psycopg2_samples(dsn: str, sql: str, rows: int, warmup: int, trials: int) -> list[float]:
    psycopg2: Any = importlib.import_module("psycopg2")

    def blocking() -> list[float]:
        connection = psycopg2.connect(dsn)
        try:
            cursor = connection.cursor()
            samples = []
            for trial in range(warmup + trials):
                started = time.perf_counter()
                cursor.execute(sql)
                _verify(cursor.fetchall(), rows)
                elapsed = time.perf_counter() - started
                if trial >= warmup:
                    samples.append(elapsed)
            return samples
        finally:
            connection.close()

    return await asyncio.to_thread(blocking)


def _summary(samples: list[float], rows: int) -> dict[str, object]:
    median = statistics.median(samples)
    ordered = sorted(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "rows_per_second": rows / median,
        "raw_seconds": samples,
    }


async def run(args: argparse.Namespace) -> int:
    backend = _native_backend()
    sql = _query(args.rows)
    slice3 = await _wreath_samples(
        backend, args.dsn, sql, args.rows, args.warmup, args.trials, batch=False
    )
    slice4 = await _wreath_samples(
        backend, args.dsn, sql, args.rows, args.warmup, args.trials, batch=True
    )
    asyncpg, psycopg3, psycopg2 = await asyncio.gather(
        _asyncpg_samples(args.dsn, sql, args.rows, args.warmup, args.trials),
        _psycopg3_samples(args.dsn, sql, args.rows, args.warmup, args.trials),
        _psycopg2_samples(args.dsn, sql, args.rows, args.warmup, args.trials),
    )
    summaries = {
        "wreath_slice3_scalar": _summary(slice3, args.rows),
        "wreath_slice4_batch": _summary(slice4, args.rows),
        "asyncpg": _summary(asyncpg, args.rows),
        "psycopg3": _summary(psycopg3, args.rows),
        "psycopg2": _summary(psycopg2, args.rows),
    }
    improvement = float(summaries["wreath_slice4_batch"]["rows_per_second"]) / float(
        summaries["wreath_slice3_scalar"]["rows_per_second"]
    )
    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "rows": args.rows,
            "warmup": args.warmup,
            "trials": args.trials,
            "columns": 3,
        },
        "results": summaries,
        "slice4_over_slice3_speedup": improvement,
    }
    print(json.dumps(document, indent=2))
    return 1 if args.require_improvement and improvement <= 1.0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--require-improvement", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
