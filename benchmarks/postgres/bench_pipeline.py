"""Compare Wreath pipelining with sequential asyncpg through a latency relay.

This benchmark requires the benchmark dependency group and a real PostgreSQL
server. It warms the prepared statement before measuring so Wreath emits only
Bind/Execute/Sync segments during the measured pipeline.
"""

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
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from wreath.postgres import connect


@dataclass(slots=True)
class LatencyRelay:
    target_host: str
    target_port: int
    one_way_delay: float
    server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _accept(
        self, downstream_reader: asyncio.StreamReader, downstream_writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
        except OSError:
            downstream_writer.close()
            return

        async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while data := await reader.read(256 * 1024):
                    await asyncio.sleep(self.one_way_delay)
                    writer.write(data)
                    await writer.drain()
            except ConnectionError, OSError:
                pass
            finally:
                writer.close()

        await asyncio.gather(
            pump(downstream_reader, upstream_writer),
            pump(upstream_reader, downstream_writer),
        )


def _relay_dsn(dsn: str, port: int) -> str:
    parsed = urlsplit(dsn)
    if parsed.hostname is None:
        raise ValueError("benchmark DSN must use TCP")
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    relayed = SplitResult(
        parsed.scheme,
        f"{userinfo}127.0.0.1:{port}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    )
    return urlunsplit(relayed)


async def _measure(
    operation: Callable[[], Awaitable[None]], warmup: int, trials: int
) -> list[float]:
    for _ in range(warmup):
        await operation()
    samples: list[float] = []
    for _ in range(trials):
        started = time.perf_counter()
        await operation()
        samples.append(time.perf_counter() - started)
    return samples


async def _wreath_samples(dsn: str, concurrency: int, warmup: int, trials: int) -> list[float]:
    connection = await connect(dsn)
    sql = "select $1::int4"
    try:
        assert await connection.fetchval(sql, 0) == 0

        async def operation() -> None:
            values = await asyncio.gather(
                *(connection.fetchval(sql, value) for value in range(concurrency))
            )
            if values != list(range(concurrency)):
                raise RuntimeError("Wreath returned incorrect pipeline results")

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()


async def _asyncpg_samples(dsn: str, concurrency: int, warmup: int, trials: int) -> list[float]:
    try:
        asyncpg: Any = importlib.import_module("asyncpg")
    except ImportError as error:
        raise RuntimeError("install asyncpg to run the pipeline comparison") from error

    connection = await asyncpg.connect(dsn)
    sql = "select $1::int4"
    try:
        assert await connection.fetchval(sql, 0) == 0

        async def operation() -> None:
            values = []
            for value in range(concurrency):
                values.append(await connection.fetchval(sql, value))
            if values != list(range(concurrency)):
                raise RuntimeError("asyncpg returned incorrect sequential results")

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()


async def _psycopg3_samples(dsn: str, concurrency: int, warmup: int, trials: int) -> list[float]:
    try:
        psycopg: Any = importlib.import_module("psycopg")
    except ImportError as error:
        raise RuntimeError("install psycopg[binary] to run the comparison") from error
    connection = await psycopg.AsyncConnection.connect(dsn)
    try:

        async def operation() -> None:
            values = []
            async with connection.cursor() as cursor:
                for value in range(concurrency):
                    await cursor.execute("select %s::int4", (value,))
                    row = await cursor.fetchone()
                    values.append(row[0])
            if values != list(range(concurrency)):
                raise RuntimeError("psycopg3 returned incorrect sequential results")

        return await _measure(operation, warmup, trials)
    finally:
        await connection.close()


async def _psycopg2_samples(dsn: str, concurrency: int, warmup: int, trials: int) -> list[float]:
    try:
        psycopg2: Any = importlib.import_module("psycopg2")
    except ImportError as error:
        raise RuntimeError("install psycopg2-binary to run the comparison") from error

    def blocking_measure() -> list[float]:
        connection = psycopg2.connect(dsn)
        try:
            cursor = connection.cursor()
            samples: list[float] = []
            for trial in range(warmup + trials):
                started = time.perf_counter()
                values = []
                for value in range(concurrency):
                    cursor.execute("select %s::int4", (value,))
                    values.append(cursor.fetchone()[0])
                elapsed = time.perf_counter() - started
                if values != list(range(concurrency)):
                    raise RuntimeError("psycopg2 returned incorrect sequential results")
                if trial >= warmup:
                    samples.append(elapsed)
            return samples
        finally:
            connection.close()

    return await asyncio.to_thread(blocking_measure)


def _summary(samples: list[float], operations: int) -> dict[str, float]:
    median = statistics.median(samples)
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "median_seconds": median,
        "p95_seconds": p95,
        "operations_per_second": operations / median,
    }


async def run(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.dsn)
    if parsed.hostname is None:
        raise ValueError("benchmark DSN must use TCP")
    relay = LatencyRelay(
        parsed.hostname,
        parsed.port or 5432,
        args.latency_ms / 2000,
    )
    relay_port = await relay.start()
    dsn = _relay_dsn(args.dsn, relay_port)
    try:
        wreath_raw = await _wreath_samples(dsn, args.concurrency, args.warmup, args.trials)
        asyncpg = await _asyncpg_samples(dsn, args.concurrency, args.warmup, args.trials)
        psycopg3 = await _psycopg3_samples(dsn, args.concurrency, args.warmup, args.trials)
        psycopg2 = await _psycopg2_samples(dsn, args.concurrency, args.warmup, args.trials)
    finally:
        await relay.close()

    wreath_summary = _summary(wreath_raw, args.concurrency)
    competitors = {
        "asyncpg_sequential": _summary(asyncpg, args.concurrency),
        "psycopg3_sequential": _summary(psycopg3, args.concurrency),
        "psycopg2_sequential": _summary(psycopg2, args.concurrency),
    }
    speedups = {
        name: wreath_summary["operations_per_second"] / summary["operations_per_second"]
        for name, summary in competitors.items()
    }
    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "dsn_host": parsed.hostname,
            "postgres_port": parsed.port or 5432,
            "artificial_round_trip_latency_ms": args.latency_ms,
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "trials": args.trials,
        },
        "wreath_pipeline": {**wreath_summary, "raw_seconds": wreath_raw},
        "asyncpg_sequential": {**competitors["asyncpg_sequential"], "raw_seconds": asyncpg},
        "psycopg3_sequential": {**competitors["psycopg3_sequential"], "raw_seconds": psycopg3},
        "psycopg2_sequential": {**competitors["psycopg2_sequential"], "raw_seconds": psycopg2},
        "wreath_speedups": speedups,
    }
    print(json.dumps(document, indent=2))
    return 1 if args.require_win and any(speedup <= 1.0 for speedup in speedups.values()) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--latency-ms", type=float, default=20.0)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--require-win", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
