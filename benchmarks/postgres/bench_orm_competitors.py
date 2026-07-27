"""Wreath's ORM against the ORMs people use: Tortoise, SQLAlchemy, SQLModel, Peewee.

The other `bench_orm_*` modules compare Wreath's own paths against each other or
against primitives (a dataclass, hand-written Python, pydantic). None of them
answers "is this ORM fast compared to the alternatives", which is the question a
person choosing an ORM asks. This one does.

Fairness contract
-----------------
- One PostgreSQL, one table, identical rows, seeded once and shared by every ORM.
- Every operation returns **hydrated model instances**, not raw rows or tuples:
  the point of an ORM is the object, and materialising it is the work.
- Each ORM uses its own ecosystem-standard driver (Wreath: `wreath.postgres`;
  Tortoise, SQLAlchemy and SQLModel: asyncpg; Peewee: psycopg2) and a single
  connection, so this measures ORM overhead rather than pool strategy.
- SQLModel is SQLAlchemy plus pydantic, so it is expected to track SQLAlchemy
  and cost a little more; it is here because people pick it, not because it is
  an independent implementation.
- Identical warmup and trial counts; medians reported with raw samples kept.
- Every operation is verified to return the expected row count before timing, so
  an ORM cannot look fast by fetching less.

**Peewee is synchronous** and is recorded as such rather than quietly compared:
it does not pay for the event loop the other three do. Read its column as "a
sync ORM for scale", not as a like-for-like race.

**An ORM is omitted from a scenario it does not support natively**, rather than
being given a hand-written equivalent -- that would measure the driver, not the
ORM. Every ORM here now covers every scenario: Wreath gained the join predicate
`Book.author.name == ...` that `join_filter_by_child` needs, and both eager-load
strategies -- `.joined()` and `.selectin()` -- are measured.

The relationship scenarios touch every loaded relation inside the timed
operation, so an ORM cannot win by returning rows and deferring the join.

Run against a disposable server:

    uv run --with tortoise-orm --with peewee --with psycopg2-binary \\
      --with 'sqlalchemy[asyncio]' --with sqlmodel --with asyncpg \\
      python -m benchmarks.postgres.bench_orm_competitors \\
      --dsn postgresql://wreath:secret@127.0.0.1:55434/wreath --output PATH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

TABLE = "orm_competitor_items"
AUTHORS = "orm_competitor_authors"
BOOKS = "orm_competitor_books"
ROWS = 1000
RANGE_SIZE = 100
#: 50 authors x 20 books: a fan-out big enough that an N+1 cannot hide.
AUTHOR_COUNT = 50
BOOKS_PER_AUTHOR = 20
PARENTS = 50
CHILD_LIMIT = 100
FILTER_AUTHOR = "author-7"

SETUP = f"""
DROP TABLE IF EXISTS {BOOKS};
DROP TABLE IF EXISTS {AUTHORS};
DROP TABLE IF EXISTS {TABLE};
CREATE TABLE {TABLE} (
    id bigint PRIMARY KEY,
    number integer NOT NULL,
    enabled boolean NOT NULL,
    label text NOT NULL
);
CREATE TABLE {AUTHORS} (
    id bigint PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE {BOOKS} (
    id bigint PRIMARY KEY,
    author_id bigint NOT NULL REFERENCES {AUTHORS}(id),
    title text NOT NULL,
    year integer NOT NULL
)
"""
FILL = f"""
INSERT INTO {TABLE} (id, number, enabled, label)
SELECT v, v, v % 2 = 0, 'label-' || v FROM generate_series(1, {{rows}}) AS v
"""
FILL_AUTHORS = f"""
INSERT INTO {AUTHORS} (id, name)
SELECT v, 'author-' || v FROM generate_series(1, {AUTHOR_COUNT}) AS v
"""
FILL_BOOKS = f"""
INSERT INTO {BOOKS} (id, author_id, title, year)
SELECT v, ((v - 1) / {BOOKS_PER_AUTHOR}) + 1, 'book-' || v, 1900 + (v % 120)
FROM generate_series(1, {AUTHOR_COUNT * BOOKS_PER_AUTHOR}) AS v
"""


def _dsn_parts(dsn: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "user": parsed.username or "wreath",
        "password": parsed.password or "",
        "database": (parsed.path or "/wreath").lstrip("/"),
    }


def _touch_to_one(parents: Any, attribute: str) -> None:
    """Materialise a to-one relation on every row, inside the timed operation.

    Eager loading is only worth measuring if the objects come back *usable*. An
    ORM that returns rows and defers the relation would otherwise look fastest
    while doing the least; touching the attribute either finds it loaded or
    raises/emits the N+1 the eager load was supposed to prevent.
    """
    for parent in parents:
        assert getattr(parent, attribute) is not None


def _touch_to_many(parents: Any, attribute: str) -> int:
    total = 0
    for parent in parents:
        total += len(list(getattr(parent, attribute)))
    return total


async def _seed(dsn: str) -> None:
    from wreath.postgres import connect

    connection = await connect(dsn)
    try:
        for statement in SETUP.strip().split(";"):
            if statement.strip():
                await connection.execute(statement)
        await connection.execute(FILL.format(rows=ROWS))
        await connection.execute(FILL_AUTHORS)
        await connection.execute(FILL_BOOKS)
        for table in (TABLE, AUTHORS, BOOKS):
            await connection.execute(f"vacuum analyze {table}")
    finally:
        await connection.close()


# --- wreath ------------------------------------------------------------------

async def _wreath_ops(dsn: str) -> tuple[dict[str, Callable[[], Awaitable[Any]]], Any]:
    from wreath.orm import Mapped, Model, column, relationship
    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.orm.types import Bool, Int32, Int64, Text
    from wreath.postgres import Database, PoolConfig

    class Item(Model, table=TABLE):
        id: Mapped[int] = column(Int64, primary_key=True)
        number: Mapped[int] = column(Int32)
        enabled: Mapped[bool] = column(Bool)
        label: Mapped[str] = column(Text)

    class Author(Model, table=AUTHORS):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        books = relationship("Book", foreign_key="author_id", load="raise")

    class Book(Model, table=BOOKS):
        id: Mapped[int] = column(Int64, primary_key=True)
        author_id: Mapped[int] = column(Int64)
        title: Mapped[str] = column(Text)
        year: Mapped[int] = column(Int32)
        author = relationship(Author, foreign_key=author_id, load="raise")

    database = Database("bench", dsn, pools={"read": PoolConfig(min_size=1, max_size=1)})
    await database.start()
    registry = Registry(database, [Item, Author, Book], validate_schema="off")

    async def get_by_pk() -> Any:
        session = Session(registry, "read")
        try:
            return await session.get(Item, 42)
        finally:
            await session.close()

    async def _fetch(query: Any) -> Any:
        session = Session(registry, "read")
        try:
            return await session.fetch(query)
        finally:
            await session.close()

    async def joined_to_one() -> Any:
        books = await _fetch(
            Book.select().include(Book.author.joined()).order_by(Book.id).limit(CHILD_LIMIT)
        )
        _touch_to_one(books, "author")
        return books

    async def selectin_to_many() -> Any:
        authors = await _fetch(
            Author.select().include(Author.books.selectin()).order_by(Author.id).limit(PARENTS)
        )
        _touch_to_many(authors, "books")
        return authors

    return {
        "get_by_pk": get_by_pk,
        "filter_range_100": lambda: _fetch(
            Item.select().where(Item.id <= RANGE_SIZE).order_by(Item.id)
        ),
        "fetch_all_1000": lambda: _fetch(Item.select().order_by(Item.id)),
        "joined_to_one": joined_to_one,
        "selectin_to_many": selectin_to_many,
        # Traversing the relationship in a predicate compiles to the same
        # INNER JOIN the others emit; it filters without loading, so the books
        # come back with `author` still unloaded, exactly as for tortoise's
        # `author__name=` and sqlalchemy's `.join(Author)`.
        "join_filter_by_child": lambda: _fetch(
            Book.select().where(Book.author.name == FILTER_AUTHOR).order_by(Book.id)
        ),
    }, database.stop()


# --- tortoise -------------------------------------------------------------

async def _tortoise_ops(dsn: str) -> tuple[dict[str, Callable[[], Awaitable[Any]]], Any]:
    from tortoise import Tortoise

    from benchmarks.postgres._tortoise_models import Item

    await Tortoise.init(
        db_url=dsn.replace("postgresql://", "asyncpg://"),
        modules={"models": ["benchmarks.postgres._tortoise_models"]},
        _create_db=False,
    )
    from benchmarks.postgres._tortoise_models import Author, Book

    async def joined_to_one() -> Any:
        books = await Book.all().select_related("author").order_by("id").limit(CHILD_LIMIT)
        _touch_to_one(books, "author")
        return books

    async def selectin_to_many() -> Any:
        authors = await Author.all().prefetch_related("books").order_by("id").limit(PARENTS)
        _touch_to_many(authors, "books")
        return authors

    return {
        "get_by_pk": lambda: Item.get(id=42),
        "filter_range_100": lambda: Item.filter(id__lte=RANGE_SIZE).order_by("id"),
        "fetch_all_1000": lambda: Item.all().order_by("id"),
        "joined_to_one": joined_to_one,
        "selectin_to_many": selectin_to_many,
        "join_filter_by_child": lambda: Book.filter(author__name=FILTER_AUTHOR).order_by("id"),
    }, Tortoise.close_connections()


# --- sqlalchemy -----------------------------------------------------------

async def _sqlalchemy_ops(dsn: str) -> tuple[dict[str, Callable[[], Awaitable[Any]]], Any]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import joinedload, selectinload

    from benchmarks.postgres._sqlalchemy_models import Author, Book, Item

    engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+asyncpg://"),
        pool_size=1, max_overflow=0,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run(build: Callable[[], Any]) -> Any:
        async with factory() as session:
            result = await session.execute(build())
            return list(result.scalars().unique().all())

    async def get_by_pk() -> Any:
        async with factory() as session:
            return await session.get(Item, 42)

    async def joined_to_one() -> Any:
        books = await _run(lambda: select(Book).options(joinedload(Book.author))
                           .order_by(Book.id).limit(CHILD_LIMIT))
        _touch_to_one(books, "author")
        return books

    async def selectin_to_many() -> Any:
        authors = await _run(lambda: select(Author).options(selectinload(Author.books))
                             .order_by(Author.id).limit(PARENTS))
        _touch_to_many(authors, "books")
        return authors

    return {
        "get_by_pk": get_by_pk,
        "filter_range_100": lambda: _run(
            lambda: select(Item).where(Item.id <= RANGE_SIZE).order_by(Item.id)
        ),
        "fetch_all_1000": lambda: _run(lambda: select(Item).order_by(Item.id)),
        "joined_to_one": joined_to_one,
        "selectin_to_many": selectin_to_many,
        "join_filter_by_child": lambda: _run(
            lambda: select(Book).join(Author).where(Author.name == FILTER_AUTHOR)
            .order_by(Book.id)
        ),
    }, engine.dispose()


# --- sqlmodel -------------------------------------------------------------

async def _sqlmodel_ops(dsn: str) -> tuple[dict[str, Callable[[], Awaitable[Any]]], Any]:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import joinedload, selectinload
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from benchmarks.postgres._sqlmodel_models import Author, Book, Item

    engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+asyncpg://"),
        pool_size=1, max_overflow=0,
    )

    async def _run(build: Callable[[], Any]) -> Any:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return list((await session.exec(build())).unique().all())

    async def get_by_pk() -> Any:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await session.get(Item, 42)

    async def joined_to_one() -> Any:
        books = await _run(lambda: select(Book).options(joinedload(Book.author))
                           .order_by(Book.id).limit(CHILD_LIMIT))
        _touch_to_one(books, "author")
        return books

    async def selectin_to_many() -> Any:
        authors = await _run(lambda: select(Author).options(selectinload(Author.books))
                             .order_by(Author.id).limit(PARENTS))
        _touch_to_many(authors, "books")
        return authors

    return {
        "get_by_pk": get_by_pk,
        "filter_range_100": lambda: _run(
            lambda: select(Item).where(Item.id <= RANGE_SIZE).order_by(Item.id)
        ),
        "fetch_all_1000": lambda: _run(lambda: select(Item).order_by(Item.id)),
        "joined_to_one": joined_to_one,
        "selectin_to_many": selectin_to_many,
        "join_filter_by_child": lambda: _run(
            lambda: select(Book).join(Author).where(Author.name == FILTER_AUTHOR)
            .order_by(Book.id)
        ),
    }, engine.dispose()


# --- peewee (synchronous) -------------------------------------------------

def _peewee_ops(dsn: str) -> tuple[dict[str, Callable[[], Any]], Callable[[], None]]:
    import peewee

    parts = _dsn_parts(dsn)
    # Named `handle`, not `database`: a class body does not close over the
    # enclosing function, so `database = database` inside Meta would resolve the
    # right-hand side globally and raise NameError.
    handle = peewee.PostgresqlDatabase(
        parts["database"], user=parts["user"], password=parts["password"],
        host=parts["host"], port=parts["port"],
    )

    class Item(peewee.Model):
        id = peewee.BigIntegerField(primary_key=True)
        number = peewee.IntegerField()
        enabled = peewee.BooleanField()
        label = peewee.TextField()

        class Meta:
            database = handle
            table_name = TABLE

    class Author(peewee.Model):
        id = peewee.BigIntegerField(primary_key=True)
        name = peewee.TextField()

        class Meta:
            database = handle
            table_name = AUTHORS

    class Book(peewee.Model):
        id = peewee.BigIntegerField(primary_key=True)
        author = peewee.ForeignKeyField(Author, backref="books", column_name="author_id")
        title = peewee.TextField()
        year = peewee.IntegerField()

        class Meta:
            database = handle
            table_name = BOOKS

    handle.connect()

    def joined_to_one() -> Any:
        books = list(
            Book.select(Book, Author).join(Author).order_by(Book.id).limit(CHILD_LIMIT)
        )
        _touch_to_one(books, "author")
        return books

    def selectin_to_many() -> Any:
        authors = list(
            peewee.prefetch(Author.select().order_by(Author.id).limit(PARENTS),
                            Book.select())
        )
        _touch_to_many(authors, "books")
        return authors

    return {
        "get_by_pk": lambda: Item.get_by_id(42),
        "filter_range_100": lambda: list(
            Item.select().where(Item.id <= RANGE_SIZE).order_by(Item.id)
        ),
        "fetch_all_1000": lambda: list(Item.select().order_by(Item.id)),
        "joined_to_one": joined_to_one,
        "selectin_to_many": selectin_to_many,
        "join_filter_by_child": lambda: list(
            Book.select().join(Author).where(Author.name == FILTER_AUTHOR).order_by(Book.id)
        ),
    }, handle.close


#: Rows each scenario must return. Checked before timing, for every ORM.
EXPECTED = {
    "get_by_pk": 1,
    "filter_range_100": RANGE_SIZE,
    "fetch_all_1000": ROWS,
    "joined_to_one": CHILD_LIMIT,
    "selectin_to_many": PARENTS,
    "join_filter_by_child": BOOKS_PER_AUTHOR,
}


def _count(value: Any) -> int:
    if value is None:
        return 0
    return len(value) if isinstance(value, list) else 1


async def _time_async(op: Callable[[], Awaitable[Any]], warmup: int,
                      trials: int, expect: int) -> list[float]:
    for _ in range(warmup):
        await op()
    got = _count(await op())
    # Unconditional: the fairness claim is that no ORM can look fast by
    # returning less, and that is only true if every operation is checked.
    assert got == expect, f"expected {expect} rows, got {got}"
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter()
        await op()
        samples.append((perf_counter() - started) * 1000)
    return samples


def _time_sync(op: Callable[[], Any], warmup: int, trials: int, expect: int) -> list[float]:
    for _ in range(warmup):
        op()
    assert _count(op()) == expect, f"expected {expect} rows"
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter()
        op()
        samples.append((perf_counter() - started) * 1000)
    return samples


async def _run_all(dsn: str, warmup: int, trials: int) -> dict[str, Any]:
    scenarios: dict[str, dict[str, Any]] = {name: {} for name in EXPECTED}

    for label, builder in (
        ("wreath", _wreath_ops), ("tortoise", _tortoise_ops),
        ("sqlalchemy", _sqlalchemy_ops), ("sqlmodel", _sqlmodel_ops),
    ):
        ops, teardown = await builder(dsn)
        try:
            for name, op in ops.items():  # an ORM omits what it cannot do natively
                # Tortoise querysets are awaitable but not coroutines; wrap so the
                # timer always awaits exactly one operation.
                async def call(op: Any = op) -> Any:
                    return await op()

                samples = await _time_async(call, warmup, trials, EXPECTED[name])
                scenarios[name][label] = {
                    "raw_ms": samples,
                    "median_ms": statistics.median(samples),
                    "sync": False,
                }
                print(f"{name:<22}{label:<12}{statistics.median(samples):8.3f} ms")
        finally:
            await teardown

    ops_sync, close = _peewee_ops(dsn)
    try:
        for name, op in ops_sync.items():
            samples = _time_sync(op, warmup, trials, EXPECTED[name])
            scenarios[name]["peewee"] = {
                "raw_ms": samples,
                "median_ms": statistics.median(samples),
                "sync": True,
            }
            print(f"{name:<22}{'peewee':<12}{statistics.median(samples):8.3f} ms (sync)")
    finally:
        close()

    for name, drivers in scenarios.items():
        if "wreath" in drivers and drivers["wreath"]["median_ms"]:
            scenarios[name]["wreath_speedup_vs"] = {
                other: drivers[other]["median_ms"] / drivers["wreath"]["median_ms"]
                for other in drivers
                if other not in {"wreath", "wreath_speedup_vs"}
            }
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    asyncio.run(_seed(args.dsn))
    scenarios = asyncio.run(_run_all(args.dsn, args.warmup, args.trials))

    document = {
        "tool": "benchmarks.postgres.bench_orm_competitors",
        "schema_version": 1,
        "metadata": {
            "started_at": datetime.now(UTC).isoformat(),
            "python": sys.version,
            "platform": platform.platform(),
            "table_rows": ROWS,
            "range_size": RANGE_SIZE,
            "warmup": args.warmup,
            "trials": args.trials,
            "note": "peewee is synchronous; every operation returns hydrated models",
        },
        "scenarios": scenarios,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
