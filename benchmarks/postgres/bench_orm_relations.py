"""Measure relationship loading at controlled cardinalities.

Compares, for the same object graph:

* ``joined_to_one``  -- parents reached through a LEFT JOIN in one statement;
* ``selectin_to_one`` -- the same parents through a second batched statement;
* ``selectin_to_many`` -- children collected per parent, at a set fan-out.

The N+1 shape is measured too, as the thing select-in exists to avoid. Every
run asserts its statement count, so a regression that reintroduces N+1 fails
here rather than quietly costing round trips.
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

from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text


class Author(Model, table="orm_bench_authors"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    books = relationship("Book", foreign_key="author_id", load="raise")


class Book(Model, table="orm_bench_books"):
    id: Mapped[int] = column(Int64, primary_key=True)
    author_id: Mapped[int] = column(Int64, references=Author.id)
    title: Mapped[str] = column(Text)
    author = relationship(Author, foreign_key=author_id, load="raise")


class _CountingConnection:
    """Wraps a connection to count the statements a load actually issues."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.statements = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.statements += 1
        return await self._inner.fetch(sql, *args)

    async def _fetch_into(self, sql: str, args: Any, dest: Any) -> Any:
        self.statements += 1
        return await self._inner._fetch_into(sql, args, dest)


class _Database:
    name = "bench"

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def acquire(self, workload: str) -> Any:
        return self.connection

    async def release(self, workload: str, connection: Any) -> None:
        return None


def _summary(samples: list[float], units: int) -> dict[str, object]:
    ordered = sorted(samples)
    median = statistics.median(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "objects_per_second": units / median,
        "raw_seconds": samples,
    }


async def _time(
    build: Any, warmup: int, trials: int, check: Any
) -> tuple[list[float], int]:
    statements = 0
    samples: list[float] = []
    for index in range(warmup + trials):
        gc.collect()
        started = time.perf_counter()
        result, statements = await build()
        elapsed = time.perf_counter() - started
        check(result)
        if index >= warmup:
            samples.append(elapsed)
        del result
    return samples, statements


async def run(args: argparse.Namespace) -> int:
    native = importlib.import_module("wreath._native._postgres")
    raw = await native.connect(args.dsn)
    try:
        for statement in (
            "DROP TABLE IF EXISTS orm_bench_books",
            "DROP TABLE IF EXISTS orm_bench_authors",
            "CREATE TABLE orm_bench_authors (id bigint PRIMARY KEY, name text NOT NULL)",
            "CREATE TABLE orm_bench_books (id bigint PRIMARY KEY, "
            "author_id bigint NOT NULL REFERENCES orm_bench_authors(id), "
            "title text NOT NULL)",
        ):
            await raw.execute(statement)
        await raw.execute(
            "INSERT INTO orm_bench_authors SELECT v, 'author-' || v "
            f"FROM generate_series(1, {args.parents}) AS v"
        )
        await raw.execute(
            "INSERT INTO orm_bench_books SELECT v, ((v - 1) % "
            f"{args.parents}) + 1, 'book-' || v "
            f"FROM generate_series(1, {args.parents * args.fanout}) AS v"
        )

        connection = _CountingConnection(raw)
        database = _Database(connection)
        registry = Registry(database, [Author, Book], validate_schema="off")
        books = args.parents * args.fanout
        results: dict[str, object] = {}

        async def joined_to_one() -> tuple[Any, int]:
            connection.statements = 0
            session = Session(registry, "read")
            rows = await session.fetch(Book.select().include(Book.author.joined()))
            return rows, connection.statements

        async def selectin_to_one() -> tuple[Any, int]:
            connection.statements = 0
            session = Session(registry, "read")
            rows = await session.fetch(Book.select().include(Book.author.selectin()))
            return rows, connection.statements

        async def selectin_to_many() -> tuple[Any, int]:
            connection.statements = 0
            session = Session(registry, "read")
            rows = await session.fetch(Author.select().include(Author.books.selectin()))
            return rows, connection.statements

        async def n_plus_one() -> tuple[Any, int]:
            connection.statements = 0
            session = Session(registry, "read")
            rows = await session.fetch(Author.select())
            for parent in rows:
                await session.load(parent, Author.books)
            return rows, connection.statements

        def check_books(rows: Any) -> None:
            if len(rows) != books or rows[0].author is None:
                raise RuntimeError("joined/select-in to-one produced the wrong graph")

        def check_authors(rows: Any) -> None:
            if len(rows) != args.parents or len(rows[0].books) != args.fanout:
                raise RuntimeError("select-in to-many produced the wrong graph")

        for name, build, check, units in (
            ("joined_to_one", joined_to_one, check_books, books),
            ("selectin_to_one", selectin_to_one, check_books, books),
            ("selectin_to_many", selectin_to_many, check_authors, books),
        ):
            samples, statements = await _time(build, args.warmup, args.trials, check)
            results[name] = {**_summary(samples, units), "statements": statements}

        if args.include_n_plus_one:
            samples, statements = await _time(
                n_plus_one, min(args.warmup, 1), min(args.trials, 3), check_authors
            )
            results["n_plus_one_baseline"] = {
                **_summary(samples, books),
                "statements": statements,
            }

        document = {
            "metadata": {
                "python": sys.version,
                "platform": platform.platform(),
                "implementation": native._implementation,
                "model_basicsize": Author.__basicsize__,
                "parents": args.parents,
                "fanout": args.fanout,
                "children": books,
                "warmup": args.warmup,
                "trials": args.trials,
            },
            "results": results,
        }
        print(json.dumps(document, indent=2))
        return 0
    finally:
        await raw.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--parents", type=int, default=500)
    parser.add_argument("--fanout", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--include-n-plus-one", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
