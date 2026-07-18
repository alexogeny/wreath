"""Rerunnable read/write workload benchmark against real PostgreSQL row data.

Scenarios exercise the driver surface that already has correctness and parity
coverage (``tests/postgres``: connection fetch/fetchrow/execute with bound
arguments, codecs, pipelining): single-row inserts and updates, point selects,
range and bulk reads, and a 32-deep concurrent point-select batch. Wreath submits
the concurrent batch on one pipelined connection; competitors execute the same
batch sequentially on one connection and are labelled accordingly.

Each run writes ``<UTC-timestamp>.json`` and refreshes ``latest.json`` in the
output directory (default ``benchmark-results-postgres``), mirroring the web
framework harness, so results accumulate and stay comparable across runs.

Run it with:

    uv run --with asyncpg --with 'psycopg[binary]' --with psycopg2-binary \
      python -m benchmarks.postgres.bench_workload \
      --dsn postgresql://neo:secret@127.0.0.1:55434/neo
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TABLE = "wreath_bench_workload"
PAYLOAD = bytes(range(64))
LABEL = "workload-row-label-{0:08d}"

CREATE_SQL = (
    f"create table {TABLE} ("
    "id int4 primary key, flag bool not null, score float8 not null, "
    "label text not null, payload bytea not null)"
)
SEED_SQL = (
    f"insert into {TABLE} select value, value % 2 = 0, value * 0.5, "
    f"'{LABEL.format(0)[:-8]}' || lpad(value::text, 8, '0'), "
    f"'\\x{PAYLOAD.hex()}'::bytea from generate_series(1, {{rows}}) as value"
)

DOLLAR = {
    "insert": f"insert into {TABLE} values ($1, $2, $3, $4, $5)",
    "point": f"select id, flag, score, label, payload from {TABLE} where id = $1",
    "update": f"update {TABLE} set score = $1, flag = $2 where id = $3",
    "range": (
        f"select id, flag, score, label, payload from {TABLE} "
        "where id between $1 and $2 order by id"
    ),
    "bulk": f"select id, flag, score, label, payload from {TABLE} order by id",
    "delete": f"delete from {TABLE} where id >= $1",
}
PYFORMAT = {
    key: sql.replace("$1", "%s")
    .replace("$2", "%s")
    .replace("$3", "%s")
    .replace("$4", "%s")
    .replace("$5", "%s")
    for key, sql in DOLLAR.items()
}


class _NeoDriver:
    name = "wreath"
    sql = DOLLAR
    concurrent = True

    async def connect(self, dsn: str) -> None:
        backend: Any = importlib.import_module("wreath._native._postgres")
        self.connection = await backend.connect(dsn)

    async def close(self) -> None:
        await self.connection.close()

    async def execute(self, sql: str, args: tuple[Any, ...]) -> None:
        await self.connection.execute(sql, *args)

    async def fetchrow(self, sql: str, args: tuple[Any, ...]) -> Any:
        return await self.connection.fetchrow(sql, *args)

    async def fetch(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        return await self.connection.fetch(sql, *args)


class _AsyncpgDriver(_NeoDriver):
    name = "asyncpg"
    concurrent = False

    async def connect(self, dsn: str) -> None:
        asyncpg: Any = importlib.import_module("asyncpg")
        self.connection = await asyncpg.connect(dsn)


class _Psycopg3Driver:
    name = "psycopg3"
    sql = PYFORMAT
    concurrent = False

    async def connect(self, dsn: str) -> None:
        psycopg: Any = importlib.import_module("psycopg")
        self.connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)

    async def close(self) -> None:
        await self.connection.close()

    async def execute(self, sql: str, args: tuple[Any, ...]) -> None:
        await self.connection.execute(sql, args)

    async def fetchrow(self, sql: str, args: tuple[Any, ...]) -> Any:
        async with self.connection.cursor() as cursor:
            await cursor.execute(sql, args)
            return await cursor.fetchone()

    async def fetch(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        async with self.connection.cursor() as cursor:
            await cursor.execute(sql, args)
            return await cursor.fetchall()


class _Psycopg2Driver:
    """Synchronous driver; scenarios run inside one worker thread."""

    name = "psycopg2"
    sql = PYFORMAT
    concurrent = False

    async def connect(self, dsn: str) -> None:
        psycopg2: Any = importlib.import_module("psycopg2")
        self.connection = await asyncio.to_thread(psycopg2.connect, dsn)
        self.connection.autocommit = True

    async def close(self) -> None:
        await asyncio.to_thread(self.connection.close)

    def _execute(self, sql: str, args: tuple[Any, ...]) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, args)

    def _fetchrow(self, sql: str, args: tuple[Any, ...]) -> Any:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchone()

    def _fetch(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall()

    async def execute(self, sql: str, args: tuple[Any, ...]) -> None:
        await asyncio.to_thread(self._execute, sql, args)

    async def fetchrow(self, sql: str, args: tuple[Any, ...]) -> Any:
        return await asyncio.to_thread(self._fetchrow, sql, args)

    async def fetch(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        return await asyncio.to_thread(self._fetch, sql, args)


DRIVERS = (_NeoDriver, _AsyncpgDriver, _Psycopg3Driver, _Psycopg2Driver)


def _summary(samples: list[float], errors: int) -> dict[str, object]:
    ordered = sorted(samples)
    median = statistics.median(ordered)
    return {
        "samples": len(samples),
        "errors": errors,
        "median_ms": median * 1000,
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000,
        "p99_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] * 1000,
        "ops_per_second": 1 / median if median > 0 else None,
    }


async def _measure(operation: Any, warmup: int, ops: int) -> dict[str, object]:
    for _ in range(warmup):
        await operation(-1)
    samples: list[float] = []
    for index in range(ops):
        started = time.perf_counter()
        await operation(index)
        samples.append(time.perf_counter() - started)
    return _summary(samples, 0)


async def _run_driver(
    driver_type: type, dsn: str, rows: int, ops: int, warmup: int, batch: int
) -> dict[str, dict[str, object]]:
    driver = driver_type()
    await driver.connect(dsn)
    sql = driver.sql
    insert_base = 1_000_000
    results: dict[str, dict[str, object]] = {}
    try:
        # Applied identically to every driver so write scenarios compare
        # driver and server CPU instead of the commit fsync of the host disk,
        # every driver starts from an equivalent table state, and the cpufreq
        # governor is ramped before the first sample.
        await driver.execute("set synchronous_commit = off", ())
        await driver.execute(sql["delete"], (insert_base,))
        await driver.execute(f"vacuum analyze {TABLE}", ())
        spin_until = time.perf_counter() + 0.3
        while time.perf_counter() < spin_until:
            pass

        counter = iter(range(insert_base + warmup + ops + 1, insert_base + 10 * ops))

        async def insert_row(index: int) -> None:
            row_id = insert_base + index + warmup + 1 if index >= 0 else next(counter)
            await driver.execute(
                sql["insert"],
                (row_id, index % 2 == 0, index * 0.25, LABEL.format(index), PAYLOAD),
            )

        # Warmup rows land above the measured id range via the counter.
        results["insert_row"] = await _measure(insert_row, warmup, ops)

        async def point_select(index: int) -> None:
            row_id = (abs(index) * 37) % rows + 1
            row = await driver.fetchrow(sql["point"], (row_id,))
            if row is None or row[0] != row_id or len(row[3]) != len(LABEL.format(0)):
                raise RuntimeError(f"{driver.name} returned a wrong point row")

        results["point_select"] = await _measure(point_select, warmup, ops)

        async def update_score(index: int) -> None:
            row_id = (abs(index) * 53) % rows + 1
            await driver.execute(sql["update"], (index * 1.5, index % 3 == 0, row_id))

        results["update_score"] = await _measure(update_score, warmup, ops)

        async def range_select(index: int) -> None:
            start = (abs(index) * 97) % max(rows - 100, 1) + 1
            fetched = await driver.fetch(sql["range"], (start, start + 99))
            if len(fetched) != 100:
                raise RuntimeError(f"{driver.name} returned a wrong range")

        results["range_select_100"] = await _measure(range_select, warmup, max(ops // 5, 20))

        async def bulk_read(index: int) -> None:
            fetched = await driver.fetch(sql["bulk"], ())
            if len(fetched) < rows:
                raise RuntimeError(f"{driver.name} returned a short bulk read")

        results["bulk_read_all_rows"] = await _measure(bulk_read, 2, 10)

        async def concurrent_points(index: int) -> None:
            ids = [((abs(index) + offset) * 41) % rows + 1 for offset in range(batch)]
            if driver.concurrent:
                rows_out = await asyncio.gather(
                    *(driver.fetchrow(sql["point"], (row_id,)) for row_id in ids)
                )
            else:
                rows_out = [await driver.fetchrow(sql["point"], (row_id,)) for row_id in ids]
            if len(rows_out) != batch or any(row is None for row in rows_out):
                raise RuntimeError(f"{driver.name} returned wrong concurrent rows")

        results[f"point_select_batch_{batch}"] = await _measure(
            concurrent_points, max(warmup // 8, 2), max(ops // 8, 25)
        )
    finally:
        await driver.close()
    return results


async def _prepare(dsn: str, rows: int) -> str:
    backend: Any = importlib.import_module("wreath._native._postgres")
    connection = await backend.connect(dsn)
    try:
        version = await connection.fetchval("select version()")
        await connection.execute(f"drop table if exists {TABLE}")
        await connection.execute(CREATE_SQL)
        await connection.execute(SEED_SQL.format(rows=rows))
        return str(version)
    finally:
        await connection.close()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    postgres_version = await _prepare(args.dsn, args.rows)
    rotation = args.rotate % len(DRIVERS)
    ordered = DRIVERS[rotation:] + DRIVERS[:rotation]
    scenarios: dict[str, dict[str, Any]] = {}
    for driver_type in ordered:
        driver_results = await _run_driver(
            driver_type, args.dsn, args.rows, args.ops, args.warmup, args.batch
        )
        for scenario, summary in driver_results.items():
            scenarios.setdefault(scenario, {})[driver_type.name] = summary

    for by_driver in scenarios.values():
        wreath_median = by_driver["wreath"]["median_ms"]
        by_driver["wreath_speedup_vs"] = {
            name: round(float(summary["median_ms"]) / float(wreath_median), 3)
            for name, summary in by_driver.items()
            if name != "wreath" and isinstance(summary, dict)
        }

    return {
        "metadata": {
            "started_at": started_at.isoformat(),
            "python": sys.version,
            "platform": platform.platform(),
            "postgres_version": postgres_version,
            "backend": "native",
            "connections_per_driver": 1,
            "table_rows": args.rows,
            "ops_per_scenario": args.ops,
            "warmup_ops": args.warmup,
            "concurrent_batch": args.batch,
            "synchronous_commit": "off",
            "driver_order": [driver_type.name for driver_type in ordered],
            "note": (
                "wreath runs the batch scenario pipelined on one connection; "
                "competitors run the same batch sequentially on one connection"
            ),
        },
        "scenarios": scenarios,
    }


def _write_report(document: dict[str, Any], output: str) -> Path:
    output_directory = Path(output)
    output_directory.mkdir(parents=True, exist_ok=True)
    started_at = datetime.fromisoformat(document["metadata"]["started_at"])
    run_name = started_at.strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps(document, indent=2) + "\n"
    output_path = output_directory / f"{run_name}.json"
    output_path.write_text(payload, encoding="utf-8")
    (output_directory / "latest.json").write_text(payload, encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--ops", type=int, default=400)
    parser.add_argument("--warmup", type=int, default=40)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument(
        "--rotate",
        type=int,
        default=0,
        help="rotate driver execution order to average out run-order bias",
    )
    parser.add_argument("--output", default="benchmark-results-postgres")
    parser.add_argument("--require-win", action="store_true")
    args = parser.parse_args()

    document = asyncio.run(run(args))
    scenarios = document["scenarios"]
    output_path = _write_report(document, args.output)

    width = max(len(name) for name in scenarios)
    for scenario, by_driver in scenarios.items():
        parts = [
            f"{name}={summary['median_ms']:.3f}ms"
            for name, summary in by_driver.items()
            if isinstance(summary, dict) and "median_ms" in summary
        ]
        print(f"{scenario:<{width}}  " + "  ".join(parts))
    print(f"[report] wrote {output_path}")

    if args.require_win:
        losses = [
            scenario
            for scenario, by_driver in scenarios.items()
            if any(ratio < 1.0 for ratio in by_driver["wreath_speedup_vs"].values())
        ]
        if losses:
            print(f"[report] wreath lost scenarios: {', '.join(losses)}")
            raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
