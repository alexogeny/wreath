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
#: The ORM's compiler qualifies and quotes -- ``"replays"."tries" = $4`` -- while
#: the keyset emits bare column names. Both are real shapes the driver sends, so
#: the fake reads both and resolves them to the same column.
_SCALAR = re.compile(
    r'^(?:"?\w+"?\.)?"?(\w+)"?\s*(>=|<=|<>|!=|>|<|=)\s*(\$\d+)$'
)
#: A verification predicate is a caller's sentence rather than the driver's
#: emission, so the fake reads the two shapes people actually write.
_NULL_TEST = re.compile(r'^"?(\w+)"?\s+(IS NOT NULL|IS NULL)$', re.IGNORECASE)
_LITERAL_COMPARE = re.compile(
    r"^\"?(\w+)\"?\s*(>=|<=|<>|!=|>|<|=)\s*('(?:[^']|'')*'|-?\d+(?:\.\d+)?)$"
)


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
    # A verification predicate is written by a caller rather than emitted by the
    # driver, and the shape people actually write is a null test.
    match = _NULL_TEST.match(predicate)
    if match is not None:
        present = row.get(match.group(1)) is not None
        return present if match.group(2).upper().startswith("IS NOT") else not present
    match = _LITERAL_COMPARE.match(predicate)
    if match is not None:
        return _compare(row.get(match.group(1)), match.group(2), _literal_value(match.group(3)))
    raise SqlUnsupported(f"the fake cannot evaluate {predicate!r}")


def _literal_value(token: str) -> Any:
    token = token.strip()
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    try:
        return int(token)
    except ValueError:
        return float(token)


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
        #: Dead-lettered chunks, keyed the way the real unique index keys them.
        self.holes: dict[tuple[str, str, str], dict[str, Any]] = {}
        #: Constraints a gate has added, by name, so ``VALIDATE`` can really scan.
        self.constraints: dict[str, str] = {}
        self.now = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)
        #: What `pg_class.reltuples` reports. ``None`` stands for a table that
        #: has never been analysed, which really does answer -1.
        self.reltuples: Any = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        #: Called with (sql, args) before each statement runs, so a test can
        #: make one of them fail exactly where it wants to.
        self.before: Any = None

    def snapshot(self) -> Any:
        return (
            copy.deepcopy(self.rows),
            copy.deepcopy(self.ledger),
            copy.deepcopy(self.holes),
        )

    def restore(self, snapshot: Any) -> None:
        self.rows, self.ledger, self.holes = snapshot

    def sql_of(self, fragment: str) -> list[str]:
        return [sql for sql, _ in self.statements if fragment in sql]

    def ledger_row(self, name: str = "", tenant: str = "") -> dict[str, Any]:
        """The one ledger row, or the named one when a test holds several."""
        if name:
            return self.ledger[(name, tenant)]
        return next(iter(self.ledger.values()))


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
_HOLES = '"wreath".pass_holes'


def _run(world: World, sql: str, args: tuple[Any, ...]) -> Any:
    world.statements.append((sql, args))
    if world.before is not None:
        world.before(sql, args)
    text = " ".join(sql.split())
    if text.startswith("SET LOCAL"):
        return "SET"
    if text.startswith("SELECT clock_timestamp() - make_interval"):
        return [{"value": world.now - datetime.timedelta(seconds=float(args[0]))}]
    if text.startswith("SELECT reltuples"):
        # A table that has never been analysed really does answer -1, which is
        # why the denominator has to check rather than trust.
        return [{"reltuples": -1 if world.reltuples is None else int(world.reltuples)}]
    if _HOLES in text and _LEDGER not in text:
        return _holes_statement(world, text, args)
    if _LEDGER in text:
        return _ledger_statement(world, text, args)
    if text.startswith("ALTER TABLE"):
        return _alter(world, text)
    if text.startswith("SELECT count(*) FROM"):
        return [{"count": len(world.rows)}]
    if text.startswith("SELECT min("):
        # A bucketed walk's one query: where the first bucket starts.
        column = text[len("SELECT min(") : text.index(")")]
        values = [row[column] for row in world.rows if row.get(column) is not None]
        return [{"anchor": min(values) if values else None}]
    if text.startswith("SELECT"):
        return _select(world, text, args)
    if text.startswith("DELETE FROM"):
        return _delete(world, text, args)
    if text.startswith("UPDATE"):
        return _update(world, text, args)
    raise SqlUnsupported(sql)


def _key(args: tuple[Any, ...]) -> tuple[str, str]:
    return (str(args[0]), str(args[1]))


def _new_ledger_row(world: World, args: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "name": args[0], "tenant": args[1], "phase": "walking", "cursor": None,
        "ceiling": None, "keyspace_from": None, "pending": [], "units_done": 0,
        "rows_done": 0, "denominator": None, "denominator_kind": None,
        "chunk_limit": args[2], "paced_reason": None, "window_started": None,
        "window_rows": 0, "window_units": 0, "started_at": world.now,
        "last_advance": None, "cycle_started": None, "driven_at": None,
        "last_drive_error": None, "verified_at": None, "verified_fact": None,
        "last_error": None,
    }


def _decorate(world: World, row: dict[str, Any]) -> dict[str, Any]:
    """A ledger row as the read statement returns it: with the clock and holes."""
    body = dict(row)
    body["now"] = world.now
    body["holes_open"] = sum(
        1 for key in world.holes if key[0] == row["name"] and key[1] == row["tenant"]
    )
    return body


def _ledger_statement(world: World, text: str, args: tuple[Any, ...]) -> Any:
    if text.startswith("INSERT INTO"):
        world.ledger.setdefault(_key(args), _new_ledger_row(world, args))
        return "INSERT 0 1"
    if text.startswith("WITH held"):
        # claim_pending: take the oldest queued unit, atomically.
        row = world.ledger.get(_key(args))
        if row is None or not row["pending"]:
            return []
        unit = row["pending"][0]
        row["pending"] = row["pending"][1:]
        return [{"unit": unit}]
    if text.startswith("SELECT"):
        if "WHERE verified_at IS NOT NULL" in text:
            # published_facts: read by a migration that holds no declaration,
            # so it filters on the fact rather than on a pass name.
            rows = [row for row in world.ledger.values() if row["verified_at"] is not None]
            if "verified_fact = $1" in text:
                rows = [row for row in rows if row["verified_fact"] == args[0]]
            return [dict(row) for row in rows]
        if "WHERE name = $1" in text:
            row = world.ledger.get(_key(args))
            return [] if row is None else [_decorate(world, row)]
        return [_decorate(world, row) for row in world.ledger.values()]
    row = world.ledger.get(_key(args))
    if row is None:
        return "UPDATE 0"
    if "SET cursor = $3::jsonb, units_done" in text:
        if row["cursor"] != _json(args[3]):
            return "UPDATE 0"
        row["cursor"] = _json(args[2])
        row["units_done"] += 1
        row["last_advance"] = world.now
        row["last_error"] = None
        return "UPDATE 1"
    if "SET cursor = $3::jsonb, last_advance" in text:
        # skip_to: past the hole, and deliberately not counting a unit done.
        if row["cursor"] != _json(args[3]):
            return "UPDATE 0"
        row["cursor"] = _json(args[2])
        row["last_advance"] = world.now
        return "UPDATE 1"
    if "SET rows_done" in text:
        row["rows_done"] += args[2]
        window = float(args[3])
        started = row["window_started"]
        rolled = started is None or (world.now - started).total_seconds() > window
        if rolled:
            row["window_started"] = world.now
            row["window_units"] = 0
            row["window_rows"] = 0
        else:
            row["window_units"] += 1
            row["window_rows"] += args[2]
        return "UPDATE 1"
    if "SET ceiling" in text:
        row["ceiling"] = _json(args[2])
        if "cycle_started" in text:
            row["cycle_started"] = world.now
        return "UPDATE 1"
    if "SET denominator" in text:
        row["denominator"] = args[2]
        row["denominator_kind"] = args[3]
        return "UPDATE 1"
    if "SET keyspace_from" in text:
        row["keyspace_from"] = _json(args[2])
        return "UPDATE 1"
    if "SET chunk_limit" in text:
        row["chunk_limit"] = args[2]
        row["paced_reason"] = args[3]
        return "UPDATE 1"
    if "SET driven_at" in text:
        row["driven_at"] = world.now
        row["last_drive_error"] = args[2]
        return "UPDATE 1"
    if "SET phase = $3, last_error" in text:
        row["phase"] = args[2]
        row["last_error"] = args[3]
        return "UPDATE 1"
    if "SET phase = 'walking', last_error = NULL" in text:
        # unblock: only a pass stopped at a hole, never one stopped at a
        # verification that answered no.
        if row["phase"] != args[2]:
            return "UPDATE 0"
        row["phase"] = "walking"
        row["last_error"] = None
        return "UPDATE 1"
    if "SET verified_at" in text:
        row["verified_at"] = world.now
        row["verified_fact"] = args[2]
        row["last_error"] = None
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
    if "SET pending = pending ||" in text:
        unit = _json(args[2])[0]
        if unit in row["pending"]:
            return "UPDATE 0"
        row["pending"] = [*row["pending"], unit]
        return "UPDATE 1"
    if "SET pending = COALESCE" in text:
        unit = _json(args[2])
        row["pending"] = [item for item in row["pending"] if item != unit]
        return "UPDATE 1"
    if "SET last_error" in text:
        row["last_error"] = args[2]
        return "UPDATE 1"
    raise SqlUnsupported(text)


def _hole_key(args: tuple[Any, ...], cursor_to: Any) -> tuple[str, str, str]:
    import json

    return (str(args[0]), str(args[1]), json.dumps(cursor_to, sort_keys=True, default=str))


def _holes_statement(world: World, text: str, args: tuple[Any, ...]) -> Any:
    if text.startswith("INSERT INTO"):
        cursor_to = _json(args[3])
        key = _hole_key(args, cursor_to)
        existing = world.holes.get(key)
        if existing is None:
            world.holes[key] = {
                "name": args[0], "tenant": args[1], "cursor_from": _json(args[2]),
                "cursor_to": cursor_to, "attempts": args[4], "error": args[5],
                "predicate": args[6], "failed_at": world.now,
            }
        else:
            existing["attempts"] += args[4]
            existing["error"] = args[5]
            existing["failed_at"] = world.now
        return "INSERT 0 1"
    if text.startswith("DELETE FROM"):
        key = _hole_key(args, _json(args[2]))
        return f"DELETE {1 if world.holes.pop(key, None) is not None else 0}"
    if text.startswith("SELECT count(*)"):
        return [{"count": sum(
            1 for key in world.holes if key[0] == str(args[0]) and key[1] == str(args[1])
        )}]
    if text.startswith("SELECT"):
        rows = list(world.holes.values())
        if "WHERE name = $1 AND tenant = $2" in text:
            rows = [
                row for row in rows
                if row["name"] == args[0] and row["tenant"] == args[1]
            ]
        elif "WHERE name = $1" in text:
            rows = [row for row in rows if row["name"] == args[0]]
        return [dict(row) for row in rows]
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
    out = []
    for row in rows:
        projected: dict[str, Any] = {}
        for column in columns:
            name, _, alias = column.partition(" AS ")
            if name in row:
                projected[alias.strip() or name] = row[name]
            else:
                # `SELECT 1 AS present` -- a probe that only asks whether a row
                # came back at all, which is how a verification is written.
                projected[alias.strip() or name] = _literal_value(name)
        out.append(projected)
    return out


_ADD_CONSTRAINT = re.compile(
    r"^ALTER TABLE (?P<table>\S+) ADD CONSTRAINT (?P<name>\w+) CHECK \((?P<check>.+)\) NOT VALID$"
)
_VALIDATE = re.compile(r"^ALTER TABLE (?P<table>\S+) VALIDATE CONSTRAINT (?P<name>\w+)$")


class CheckViolation(Exception):
    """What PostgreSQL raises when ``VALIDATE CONSTRAINT`` finds an offending row."""

    sqlstate = "23514"

    def __init__(self, name: str, row: Any) -> None:
        super().__init__(
            f'check constraint "{name}" is violated by some row: {row!r}'
        )


def _alter(world: World, text: str) -> str:
    """``NOT VALID`` records the check; ``VALIDATE`` really scans for a violation.

    Evaluating the predicate for real is the whole reason this fake exists: a
    stub that always said "valid" would make the strongest verification grade in
    the design the one nothing tested.
    """
    match = _ADD_CONSTRAINT.match(text)
    if match is not None:
        name = match.group("name")
        if name in world.constraints:
            raise SqlUnsupported(f'constraint "{name}" already exists')
        world.constraints[name] = match.group("check")
        return "ALTER TABLE"
    match = _VALIDATE.match(text)
    if match is None:
        raise SqlUnsupported(text)
    name = match.group("name")
    check = world.constraints.get(name)
    if check is None:
        raise SqlUnsupported(f'constraint "{name}" does not exist')
    for row in world.rows:
        if not evaluate(check, row, ()):
            raise CheckViolation(name, row)
    return "ALTER TABLE"


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
        expression = expression.strip()
        for row in touched:
            if "$" in expression:
                row[column.strip()] = _value(expression, args)
            else:
                # `SET grade_text = 'moderate'` really stores `moderate`. Keeping
                # the quotes would make every assertion about a converted value
                # test the fake's spelling rather than the pass's behaviour.
                try:
                    row[column.strip()] = _literal_value(expression)
                except ValueError:
                    row[column.strip()] = expression
    return f"UPDATE {len(touched)}"
