"""Design 20 §14's ablations for `wreath.passes`, against a live PostgreSQL.

Run with ``WREATH_TEST_POSTGRES_DSN`` set. Every number is a median of repeated
full walks, and every comparison carries the measured A/A noise floor -- two
runs of the *same* configuration -- because on a powersave governor most deltas
in this codebase do not clear it, and `AGENTS.md` is explicit that below the
floor means **unresolved, not zero**.

Deliberately *not* a pytest: these walks take minutes and rewrite ten million
rows. `tests/postgres/test_passes_integration.py` owns the correctness claims.

    .venv/bin/python benchmarks/bench_chunked_pass.py --sizes 100000 1000000
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from typing import Any

from wreath.passes import Ceiling, ChunkedPass, DutyCycle, Key, Rewrite, Rows, Table
from wreath.postgres import Database

SCHEMA = "wreath_bench_pass"
TABLE = f'"{SCHEMA}".bench_rows'


# --- fixture ------------------------------------------------------------------


async def apply(database: Any, sql: str) -> None:
    connection = await database.acquire("write")
    try:
        for statement in (part.strip() for part in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


async def make_table(database: Any, rows: int) -> None:
    """Seed server-side: `generate_series` never crosses the wire."""
    from wreath.passes import schema_sql

    await apply(database, f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    await apply(database, schema_sql(SCHEMA))
    await apply(database, f"DROP TABLE IF EXISTS {TABLE}")
    await apply(
        database,
        f"CREATE TABLE {TABLE} (\n"
        "  id bigint PRIMARY KEY,\n"
        "  herd_id bigint NOT NULL,\n"
        "  grade int NOT NULL,\n"
        "  grade_text text\n"
        ")",
    )
    await apply(
        database,
        f"INSERT INTO {TABLE} (id, herd_id, grade, grade_text) "
        f"SELECT i, i % 1000, i % 10, NULL FROM generate_series(1, {rows}) i",
    )
    await apply(database, f"ANALYZE {TABLE}")


async def reset(database: Any) -> None:
    """Undo a rewrite so the next arm walks the same work."""
    await apply(database, f"UPDATE {TABLE} SET grade_text = NULL")
    await apply(database, f'TRUNCATE "{SCHEMA}".passes')
    await apply(database, f'TRUNCATE "{SCHEMA}".pass_holes')


def walk_for(limit: int, *, name: str = "bench", work: Any = None) -> ChunkedPass:
    return ChunkedPass(
        name,
        over=Table("bench_rows", schema=SCHEMA),
        units=Rows(
            key=Key("id", "bigint", indexed=True, unique=True, monotone=True),
            limit=limit,
            within="60s",
        ),
        frontier=Ceiling.at_launch(),
        work=work
        or Rewrite(set_={"grade_text": "grade::text"}, where="grade_text IS NULL"),
        pace=DutyCycle(1.0),  # unpaced: this measures throughput, not politeness
        schema=SCHEMA,
        # One shift for the whole walk: `run()` would otherwise re-enter every
        # 10s, and shift re-entry is not what H1 is varying.
        shift="900s",
    )


# --- one walk -----------------------------------------------------------------


async def timed_walk(database: Any, walk: ChunkedPass) -> tuple[float, int]:
    started = time.perf_counter()
    result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))
    return time.perf_counter() - started, result.rows


async def repeat(database: Any, make: Any, runs: int) -> list[float]:
    samples = []
    for index in range(runs):
        await reset(database)
        elapsed, _rows = await timed_walk(database, make(index))
        samples.append(elapsed)
    return samples


# --- the counterfactual for H4 ------------------------------------------------


class RewritePerRow(Rewrite):
    """H4's counterfactual: one `UPDATE` per row instead of one per chunk.

    Subclasses the shipped `Rewrite` only to satisfy `ChunkedPass`'s
    declaration-time work-type refusal -- which is doing its job by rejecting a
    stranger. Nothing here ships; it exists so the shipped shape can be measured
    against the thing everyone writes first.
    """

    async def apply(self, tx: Any, chunk: Any, binds: Any) -> int:
        rows = await tx.fetch(
            f"SELECT id FROM {chunk.table} WHERE {chunk.where} AND grade_text IS NULL",
            *binds.args,
        )
        for row in rows:
            await tx.execute(
                f"UPDATE {chunk.table} SET grade_text = grade::text WHERE id = $1",
                row[0],
            )
        return len(rows)


# --- naive baselines ----------------------------------------------------------


async def baseline_offset(database: Any, page: int) -> float:
    """B1, the honest hand-roll: OFFSET pages, one long transaction, no pacing."""
    await reset(database)
    connection = await database.acquire("write")
    started = time.perf_counter()
    try:
        async with connection.transaction() as tx:
            offset = 0
            while True:
                moved = await tx.execute(
                    f"UPDATE {TABLE} SET grade_text = grade::text WHERE id IN ("
                    f"  SELECT id FROM {TABLE} WHERE grade_text IS NULL"
                    f"  ORDER BY id LIMIT {page} OFFSET {offset})"
                )
                count = int(str(moved).rsplit(" ", 1)[-1]) if moved else 0
                if not count:
                    break
                offset += page
    finally:
        await database.release("write", connection)
    return time.perf_counter() - started


async def baseline_one_update(database: Any) -> float:
    """B2, the 2am psql session: one UPDATE over the whole table."""
    await reset(database)
    connection = await database.acquire("write")
    started = time.perf_counter()
    try:
        await connection.execute(f"UPDATE {TABLE} SET grade_text = grade::text")
    finally:
        await database.release("write", connection)
    return time.perf_counter() - started


# --- reporting ----------------------------------------------------------------


def summarise(label: str, samples: list[float], rows: int, floor: float) -> None:
    median = statistics.median(samples)
    throughput = rows / median
    marker = ""
    if floor and abs(median - floor) < floor:
        marker = ""
    print(
        f"  {label:28s} {median:8.3f}s  {throughput:12,.0f} rows/s"
        f"  (n={len(samples)}, spread {max(samples) - min(samples):.3f}s){marker}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    parser.add_argument("--limits", type=int, nargs="+", default=[100, 1_000, 10_000, 100_000])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--skip-h4", action="store_true")
    parser.add_argument("--skip-baselines", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("WREATH_TEST_POSTGRES_DSN")
    if not dsn:
        raise SystemExit("set WREATH_TEST_POSTGRES_DSN")

    database = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 4}})
    await database.start()
    try:
        for rows in args.sizes:
            print(f"\n=== {rows:,} rows " + "=" * 40)
            await make_table(database, rows)

            # A/A floor: the same configuration, twice, nothing varied.
            aa = await repeat(database, lambda _i: walk_for(1_000), 2)
            floor = abs(aa[0] - aa[1])
            print(
                f"  A/A noise floor = {floor:.3f}s "
                f"({floor / statistics.median(aa) * 100:.1f}% of a 1k-limit walk); "
                f"a delta must exceed {floor * 2:.3f}s to be reported\n"
            )

            print("  H1 - chunk size")
            for limit in args.limits:
                if rows >= 10_000_000 and limit < 1_000:
                    label = "limit=" + str(limit)
                    print(f"  {label:28s} skipped (would be {rows // limit:,} chunks)")
                    continue
                samples = await repeat(database, lambda _i, n=limit: walk_for(n), args.runs)
                summarise(f"limit={limit}", samples, rows, floor)

            if not args.skip_h4:
                print("\n  H4 - one statement per chunk vs one per row")
                shipped = await repeat(database, lambda _i: walk_for(1_000), args.runs)
                summarise("one UPDATE per chunk", shipped, rows, floor)
                if rows <= 100_000:
                    per_row = await repeat(
                        database,
                        lambda _i: walk_for(
                            1_000,
                            work=RewritePerRow(
                                set_={"grade_text": "grade::text"},
                                where="grade_text IS NULL",
                            ),
                        ),
                        max(1, args.runs - 2),
                    )
                    summarise("one UPDATE per row", per_row, rows, floor)
                else:
                    print(f"  {'one UPDATE per row':28s} skipped (would be {rows:,} statements)")

            if not args.skip_baselines:
                print("\n  baselines")
                if rows <= 200_000:
                    b1 = [
                        await baseline_offset(database, 1_000)
                        for _ in range(max(1, args.runs - 1))
                    ]
                    summarise("B1 OFFSET, one tx", b1, rows, floor)
                else:
                    # N^2/(2c) by construction (§14.1). Running it at 10^6 would
                    # take longer than every other arm combined, and the shape is
                    # already visible at 10^5.
                    print(f"  {'B1 OFFSET, one tx':28s} skipped (quadratic; see 10^5)")
                b2 = [await baseline_one_update(database) for _ in range(args.runs)]
                summarise("B2 one UPDATE", b2, rows, floor)
    finally:
        await apply(database, f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await database.stop()


if __name__ == "__main__":
    asyncio.run(main())
