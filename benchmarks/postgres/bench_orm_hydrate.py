"""Compare ORM hydration paths: Record-to-model versus direct native hydration.

Four paths over the same rows and the same models:

* ``record_to_pure_model``  -- Records decoded by the reference driver, then
  copied into pure-Python model storage;
* ``record_to_native_model`` -- Records decoded natively, then copied into
  native model storage;
* ``direct_native``          -- the field tape decoded straight into native
  model cells, with no Record in between;
* ``driver_records``         -- the driver's own fetch(), as a floor.

Allocation counts come from tracemalloc and are reported next to the timings,
because the point of the direct path is what it does *not* allocate.
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
import tracemalloc
from collections.abc import Awaitable, Callable
from typing import Any

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Bool, Int32, Int64, Text, Timestamp


class Item(Model, table="orm_bench_items"):
    id: Mapped[int] = column(Int64, primary_key=True)
    number: Mapped[int] = column(Int32)
    enabled: Mapped[bool] = column(Bool)
    label: Mapped[str] = column(Text)
    created_at: Mapped[object] = column(Timestamp)


SETUP = """
DROP TABLE IF EXISTS orm_bench_items;
CREATE TABLE orm_bench_items (
    id bigint PRIMARY KEY,
    number integer NOT NULL,
    enabled boolean NOT NULL,
    label text NOT NULL,
    created_at timestamp NOT NULL
)
"""

FILL = """
INSERT INTO orm_bench_items (id, number, enabled, label, created_at)
SELECT v, v, v % 2 = 0, 'label-' || v, timestamp '2024-01-01' + (v || ' seconds')::interval
FROM generate_series(1, $1) AS v
"""

FULL_SQL = (
    'SELECT "t0"."id", "t0"."number", "t0"."enabled", "t0"."label", "t0"."created_at" '
    'FROM "public"."orm_bench_items" AS "t0"'
)
PROJECTED_SQL = (
    'SELECT "t0"."id", "t0"."number" FROM "public"."orm_bench_items" AS "t0"'
)


class _Database:
    """The minimum surface a Session needs; the benchmark owns the connection."""

    name = "bench"

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def acquire(self, workload: str) -> Any:
        return self._connection

    async def release(self, workload: str, connection: Any) -> None:
        return None


async def _measure(
    operation: Callable[[], Awaitable[Any]], warmup: int, trials: int, expect: int
) -> list[float]:
    for _ in range(warmup):
        if len(await operation()) != expect:
            raise RuntimeError("benchmark returned the wrong row count")
    samples = []
    for _ in range(trials):
        gc.collect()
        started = time.perf_counter()
        rows = await operation()
        elapsed = time.perf_counter() - started
        if len(rows) != expect:
            raise RuntimeError("benchmark returned the wrong row count")
        del rows
        samples.append(elapsed)
    return samples


async def _retained(operation: Callable[[], Awaitable[Any]]) -> dict[str, int]:
    """Memory still held while the result set is alive.

    The snapshot is taken with the rows still referenced: measuring after they
    go out of scope would report what survives collection, which is nearly the
    same for every path and says nothing about what each one allocated.
    """
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    held = await operation()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    stats = after.compare_to(before, "filename")
    result = {
        "retained_blocks": sum(item.count_diff for item in stats),
        "retained_bytes": sum(item.size_diff for item in stats),
        "rows_held": len(held),
    }
    del held
    return result


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


def _fresh_session(registry: Registry, connection: Any) -> Session:
    return Session(registry, "read")


async def _run_path(
    registry: Registry,
    connection: Any,
    sql: str,
    rows: int,
    warmup: int,
    trials: int,
    *,
    direct: bool,
) -> tuple[list[float], dict[str, int]]:
    """Time one hydration path with a cold identity map per iteration."""
    hook = connection._decode_dest
    if not direct:
        # Forcing the Record path on the same connection keeps everything else
        # identical between the two measurements.
        type(connection)._decode_dest = None
    try:
        async def operation() -> Any:
            session = _fresh_session(registry, connection)
            compiled = _compile(registry, sql)
            return await session._fetch_objects(connection, compiled, sql, ())

        samples = await _measure(operation, warmup, trials, rows)
        retained = await _retained(operation)
        return samples, retained
    finally:
        type(connection)._decode_dest = hook


def _compile(registry: Registry, sql: str) -> Any:
    from wreath.orm.compiler import compile_select

    query = Item.select() if sql == FULL_SQL else Item.select(Item.id, Item.number)
    compiled = compile_select(registry, query)
    if compiled.sql != sql:
        raise RuntimeError(f"unexpected SQL:\n{compiled.sql}\n{sql}")
    return compiled


async def run(args: argparse.Namespace) -> int:
    native = importlib.import_module("wreath._native._postgres")
    connection = await native.connect(args.dsn)
    try:
        # The compiled SQL below names `"public"."orm_bench_items"` explicitly,
        # so the table has to land there. It does not by default: the stock
        # `search_path` is `"$user", public`, and the DSN in AGENTS.md connects
        # as `wreath` -- a role whose own schema exists as soon as any test run
        # has created one. Unpinned, setup writes into `wreath` and the
        # benchmark fails with `relation "public.orm_bench_items" does not
        # exist`, which reads like a missing table rather than a search path.
        await connection.execute("SET search_path TO public")
        await connection.execute(SETUP.split(";")[0])
        await connection.execute(SETUP.split(";")[1])
        await connection.execute(FILL.replace("$1", str(args.rows)))

        registry = Registry(_Database(connection), [Item], validate_schema="off")
        results: dict[str, object] = {}

        for name, sql in (("full_row", FULL_SQL), ("projected_row", PROJECTED_SQL)):
            for label, direct in (("direct_native", True), ("record_to_native_model", False)):
                samples, allocations = await _run_path(
                    registry, connection, sql, args.rows, args.warmup, args.trials,
                    direct=direct,
                )
                results[f"{name}.{label}"] = {
                    **_summary(samples, args.rows),
                    **allocations,
                }

        async def driver_only() -> Any:
            return await connection.fetch(FULL_SQL)

        results["full_row.driver_records"] = {
            **_summary(
                await _measure(driver_only, args.warmup, args.trials, args.rows),
                args.rows,
            ),
            **await _retained(driver_only),
        }

        document = {
            "metadata": {
                "python": sys.version,
                "platform": platform.platform(),
                "implementation": native._implementation,
                "rows": args.rows,
                "warmup": args.warmup,
                "trials": args.trials,
                "columns": 5,
                "dsn_port": args.dsn.rsplit(":", 1)[-1].split("/")[0],
            },
            "results": results,
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
