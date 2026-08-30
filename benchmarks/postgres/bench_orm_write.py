"""Drive equal-shape ORM updates through a real PostgreSQL connection."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from wreath.orm import Mapped, Model, Registry, column
from wreath.orm import session as session_module
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text
from wreath.postgres import Database, PoolConfig

_SCHEMA = "wreath_bench_orm_write"


class BenchRow(Model, table="rows", schema=_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


async def run(dsn: str, mode: str, rows: int, iterations: int) -> None:
    database = Database(
        "orm-write-benchmark",
        dsn,
        pools={"write": PoolConfig(min_size=1, max_size=1)},
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."rows" '
            "(id bigint PRIMARY KEY, label text NOT NULL)"
        )
    finally:
        await database.release("write", connection)

    registry = Registry(database, [BenchRow], validate_schema="off")
    session = Session(registry, "write")
    original_limit = session_module.MAX_BIND_PARAMETERS
    session_settings: Any = session_module
    try:
        for index in range(rows):
            session.add(BenchRow(id=index, label="initial"))
        await session.flush()
        session_settings.MAX_BIND_PARAMETERS = 1 if mode == "scalar" else original_limit
        for iteration in range(iterations):
            loaded = await session.fetch(BenchRow.select())
            for item in loaded:
                item.label = f"{iteration}:{item.id}"
            await session.flush()
        final = await session.raw(
            f'SELECT id, label FROM "{_SCHEMA}"."rows" ORDER BY id'
        ).fetch()
    finally:
        session_settings.MAX_BIND_PARAMETERS = original_limit
        await session.close()
        connection = await database.acquire("write")
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await database.release("write", connection)
            await database.stop()

    expected_iteration = iterations - 1
    if len(final) != rows or any(
        row[1] != f"{expected_iteration}:{row[0]}" for row in final
    ):
        raise RuntimeError("ORM write benchmark returned incorrect rows")
    print(
        json.dumps(
            {
                "rows": rows,
                "iterations": iterations,
                "checksum": sum(row[0] for row in final),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--mode", choices=("scalar", "batched"), required=True)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    if args.rows < 1 or args.iterations < 1:
        parser.error("--rows and --iterations must both be at least 1")
    asyncio.run(run(args.dsn, args.mode, args.rows, args.iterations))


if __name__ == "__main__":
    main()
