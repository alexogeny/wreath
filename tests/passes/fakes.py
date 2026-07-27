"""A tiny PostgreSQL stand-in that really evaluates what a pass emits.

A fake that records statements and hands back canned answers proves that a pass
called something, not that what it called was correct -- and every rule in the
chunked-pass design is about the *shape* of a statement. So this evaluates the
small grammar the driver actually emits: row comparisons, a scalar frontier, an
``ORDER BY`` in one direction, ``OFFSET``/``LIMIT``, ``DELETE`` and ``UPDATE``
over that predicate, and the ledger's compare-and-swaps.

It is deliberately strict. A predicate it cannot parse raises rather than
matching everything, because "the fake quietly matched all the rows" is exactly
the failure a hand-rolled backfill has.

Transactions are real enough to matter: ``BEGIN`` snapshots the world and
``ROLLBACK`` restores it, which is what makes cursor-and-work atomicity
something a test can assert rather than something a comment claims.
"""

from __future__ import annotations

import copy
import datetime
import re
from typing import Any

_ROW_COMPARISON = re.compile(r"^\(([\w, ]+)\)\s*(>=|<=|>|<)\s*\(([\s$\d,]+)\)$")
_SCALAR = re.compile(r"^(\w+)\s*(>=|<=|<>|!=|>|<|=)\s*(\$\d+)$")


class SqlUnsupported(AssertionError):
    """The fake refuses to guess at a statement it was not built to answer."""


# --- predicate evaluation ----------------------------------------------------


def split_conjuncts(text: str) -> list[str]:
    """Split on top-level ``AND`` only, so a parenthesised filter stays whole."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    tokens = re.split(r"(\(|\)|\sAND\s)", text)
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if depth == 0 and token.strip() == "AND" and token != "(":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(token)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _value(token: str, args: tuple[Any, ...]) -> Any:
    token = token.strip()
    if token.startswith("$"):
        return args[int(token[1:]) - 1]
    raise SqlUnsupported(f"the fake only binds parameters, not literals: {token!r}")


def _compare(left: Any, operator: str, right: Any) -> bool:
    if left is None or right is None:
        return False
    return {
        ">": left > right, "<": left < right,
        ">=": left >= right, "<=": left <= right, "=": left == right,
        "<>": left != right, "!=": left != right,
    }[operator]


def evaluate(predicate: str, row: dict[str, Any], args: tuple[Any, ...]) -> bool:
    predicate = predicate.strip()
    if predicate in ("TRUE", "true"):
        return True
    if predicate in ("FALSE", "false"):
        return False
    conjuncts = split_conjuncts(predicate)
    if len(conjuncts) > 1:
        return all(evaluate(part, row, args) for part in conjuncts)
    if predicate.startswith("(") and predicate.endswith(")"):
        inner = predicate[1:-1]
        if _ROW_COMPARISON.match(predicate) is None and split_conjuncts(inner) != [predicate]:
            return evaluate(inner, row, args)
    match = _ROW_COMPARISON.match(predicate)
    if match is not None:
        columns = [name.strip() for name in match.group(1).split(",")]
        values = tuple(_value(token, args) for token in match.group(3).split(","))
        return _compare(tuple(row[name] for name in columns), match.group(2), values)
    match = _SCALAR.match(predicate)
    if match is not None:
        return _compare(row[match.group(1)], match.group(2), _value(match.group(3), args))
    raise SqlUnsupported(f"the fake cannot evaluate {predicate!r}")


def order_rows(rows: list[dict[str, Any]], clause: str) -> list[dict[str, Any]]:
    terms = [term.strip() for term in clause.split(",")]
    ordered = list(rows)
    for term in reversed(terms):
        name, _, direction = term.partition(" ")
        ordered.sort(key=lambda row, n=name: row[n], reverse=direction.strip() == "DESC")
    return ordered


# --- the world ---------------------------------------------------------------


class World:
    """The rows one fake database holds: a walked table plus the pass ledger."""

    def __init__(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.table = table
        self.rows = [dict(row) for row in rows]
        self.ledger: dict[tuple[str, str], dict[str, Any]] = {}
        self.now = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        #: Called with (sql, args) before each statement runs, so a test can
        #: make one of them fail exactly where it wants to.
        self.before: Any = None

    def snapshot(self) -> Any:
        return (copy.deepcopy(self.rows), copy.deepcopy(self.ledger))

    def restore(self, snapshot: Any) -> None:
        self.rows, self.ledger = snapshot

    def sql_of(self, fragment: str) -> list[str]:
        return [sql for sql, _ in self.statements if fragment in sql]


class FakeConnection:
    def __init__(self, world: World) -> None:
        self.world = world

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self.world)

    async def execute(self, sql: str, *args: Any) -> str:
        return _run(self.world, sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        result = _run(self.world, sql, args)
        return result[0] if isinstance(result, list) and result else None

    async def fetch(self, sql: str, *args: Any) -> Any:
        result = _run(self.world, sql, args)
        return result if isinstance(result, list) else []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return next(iter(row.values()))


class FakeTransaction:
    def __init__(self, world: World) -> None:
        self.world = world
        self._snapshot: Any = None

    async def __aenter__(self) -> FakeTransaction:
        self._snapshot = self.world.snapshot()
        self.world.statements.append(("BEGIN", ()))
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.world.statements.append(("COMMIT", ()))
        else:
            self.world.restore(self._snapshot)
            self.world.statements.append(("ROLLBACK", ()))
        return False

    async def execute(self, sql: str, *args: Any) -> str:
        return _run(self.world, sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        result = _run(self.world, sql, args)
        return result[0] if isinstance(result, list) and result else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return None if row is None else next(iter(row.values()))


class FakeDatabase:
    def __init__(self, world: World) -> None:
        self.world = world
        self.acquired = 0
        self.released = 0
        self.fail_acquire = False

    async def acquire(self, workload: str = "write") -> FakeConnection:
        if self.fail_acquire:
            raise TimeoutError("timed out acquiring PostgreSQL connection")
        self.acquired += 1
        return FakeConnection(self.world)

    async def release(self, workload: str, connection: Any) -> None:
        self.released += 1


# --- the interpreter ---------------------------------------------------------

_LEDGER = '"wreath".passes'


def _run(world: World, sql: str, args: tuple[Any, ...]) -> Any:
    world.statements.append((sql, args))
    if world.before is not None:
        world.before(sql, args)
    text = " ".join(sql.split())
    if text.startswith("SET LOCAL"):
        return "SET"
    if text.startswith("SELECT clock_timestamp() - make_interval"):
        return [{"value": world.now - datetime.timedelta(seconds=float(args[0]))}]
    if _LEDGER in text:
        return _ledger_statement(world, text, args)
    if text.startswith("SELECT"):
        return _select(world, text, args)
    if text.startswith("DELETE FROM"):
        return _delete(world, text, args)
    if text.startswith("UPDATE"):
        return _update(world, text, args)
    raise SqlUnsupported(sql)


def _key(args: tuple[Any, ...]) -> tuple[str, str]:
    return (str(args[0]), str(args[1]))


def _ledger_statement(world: World, text: str, args: tuple[Any, ...]) -> Any:
    if text.startswith("INSERT INTO"):
        world.ledger.setdefault(
            _key(args),
            {
                "name": args[0], "tenant": args[1], "phase": "walking", "cursor": None,
                "ceiling": None, "pending": [], "units_done": 0, "rows_done": 0,
                "denominator": None, "denominator_kind": None, "chunk_limit": args[2],
                "paced_reason": None, "started_at": world.now, "last_advance": None,
                "cycle_started": None, "verified_at": None, "verified_fact": None,
                "last_error": None,
            },
        )
        return "INSERT 0 1"
    if text.startswith("SELECT"):
        if "WHERE name = $1" in text:
            row = world.ledger.get(_key(args))
            return [] if row is None else [dict(row)]
        return [dict(row) for row in world.ledger.values()]
    row = world.ledger.get(_key(args))
    if row is None:
        return "UPDATE 0"
    if "SET cursor = $3::jsonb" in text:
        if row["cursor"] != _json(args[3]):
            return "UPDATE 0"
        row["cursor"] = _json(args[2])
        row["units_done"] += 1
        row["last_advance"] = world.now
        row["last_error"] = None
        return "UPDATE 1"
    if "SET rows_done" in text:
        row["rows_done"] += args[2]
        return "UPDATE 1"
    if "SET ceiling" in text:
        row["ceiling"] = _json(args[2])
        if "cycle_started" in text:
            row["cycle_started"] = world.now
        return "UPDATE 1"
    if "SET chunk_limit" in text:
        row["chunk_limit"] = args[2]
        row["paced_reason"] = args[3]
        return "UPDATE 1"
    if "SET phase = $4" in text:
        if row["phase"] != args[2]:
            return "UPDATE 0"
        row["phase"] = args[3]
        return "UPDATE 1"
    if "SET cursor = NULL" in text:
        if row["phase"] not in ("walking", "done"):
            return "UPDATE 0"
        row["cursor"] = None
        row["phase"] = "walking"
        row["cycle_started"] = world.now
        return "UPDATE 1"
    if "SET last_error" in text:
        row["last_error"] = args[2]
        return "UPDATE 1"
    raise SqlUnsupported(text)


def _json(value: Any) -> Any:
    if value is None:
        return None
    import json

    return json.loads(value) if isinstance(value, str) else value


_SELECT = re.compile(
    r"^SELECT (?P<cols>.+?) FROM (?P<table>\S+)(?: WHERE (?P<where>.+?))?"
    r"(?: ORDER BY (?P<order>.+?))?(?: OFFSET \$(?P<offset>\d+))?(?: LIMIT (?P<limit>\d+))?$"
)


def _select(world: World, text: str, args: tuple[Any, ...]) -> Any:
    match = _SELECT.match(text)
    if match is None:
        raise SqlUnsupported(text)
    # A ceiling captured at launch reads the largest key with no predicate at
    # all, so an absent WHERE means every row rather than none.
    where = match.group("where")
    rows = [row for row in world.rows if where is None or evaluate(where, row, args)]
    if match.group("order"):
        rows = order_rows(rows, match.group("order"))
    if match.group("offset"):
        rows = rows[args[int(match.group("offset")) - 1] :]
    if match.group("limit"):
        rows = rows[: int(match.group("limit"))]
    columns = [name.strip() for name in match.group("cols").split(",")]
    return [{name: row[name] for name in columns} for row in rows]


_DELETE = re.compile(r"^DELETE FROM (?P<table>\S+) WHERE (?P<where>.+)$")
_UPDATE = re.compile(r"^UPDATE (?P<table>\S+) SET (?P<set>.+?) WHERE (?P<where>.+)$")


def _delete(world: World, text: str, args: tuple[Any, ...]) -> str:
    match = _DELETE.match(text)
    if match is None:
        raise SqlUnsupported(text)
    doomed = [row for row in world.rows if evaluate(match.group("where"), row, args)]
    world.rows = [row for row in world.rows if row not in doomed]
    return f"DELETE {len(doomed)}"


def _update(world: World, text: str, args: tuple[Any, ...]) -> str:
    match = _UPDATE.match(text)
    if match is None:
        raise SqlUnsupported(text)
    touched = [row for row in world.rows if evaluate(match.group("where"), row, args)]
    for assignment in match.group("set").split(","):
        column, _, expression = assignment.partition("=")
        for row in touched:
            row[column.strip()] = _value(expression, args) if "$" in expression else expression
    return f"UPDATE {len(touched)}"
