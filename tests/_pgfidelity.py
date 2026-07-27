"""What PostgreSQL refuses, so the doubles can refuse it too.

Three defects reached working-looking code in one session because the test
doubles accepted statements a real server rejects:

* ``$1::regclass`` in the default progress denominator -- **worked on the first
  call and raised on every one after**, because only the prepared statement
  carries the inferred parameter type;
* ``= ANY($1)`` with a Python list, in three places, two of them safety
  refusals that had therefore never once fired;
* a predicate the passes fake could not parse, so the branch was untestable,
  so it was untested, so a renamed symbol in it survived a green suite.

The common cause is that a fake models neither PostgreSQL's type system nor its
prepared-statement cache. This module closes that, and every rule in it was
**measured against PostgreSQL 17.10 first** -- see
``tests/postgres/test_double_fidelity.py``, which runs the same assertions
against a real connection so a divergence is a test failure rather than a
discovery six months later.

Nothing here is inferred from documentation or from what PostgreSQL "should"
do. A stricter fiction is still a fiction.
"""

from __future__ import annotations

from typing import Any

# One copy of the rules, in the module whose job is modelling a database
# boundary. Duplicating them here would recreate exactly the drift the contract
# test exists to prevent -- the doubles in `src/` and the fakes in `tests/`
# would be free to disagree, and nothing would notice.
from wreath._replay_adapters import (
    refuse_multiple_commands as check_single_statement,
)
from wreath._replay_adapters import (
    refuse_unbindable as check_bindable,
)
from wreath._replay_adapters import (
    refuse_uninferable_cast,
)
from wreath.postgres import PostgresError as FakePostgresError

#: Named explicitly so `ruff --fix` cannot drop a re-export as "unused" --
#: it did exactly that once, and the failure surfaced three files away.
__all__ = [
    "FakePostgresError",
    "FakeRecord",
    "PreparedStatements",
    "check_bindable",
    "check_single_statement",
    "check_statement",
    "record",
]

# --- Record ------------------------------------------------------------------

#: The real ``Record`` is deliberately narrow: subscript by position or name,
#: a length, and nothing else. ``dir()`` on it is empty. Measured, because a
#: fake returning ``dict`` let ``row.values()`` through and the live test then
#: died on ``AttributeError`` in code nobody had run.
_RECORD_ABSENT = ("keys", "items", "get", "values", "__iter__", "__contains__")


class FakeRecord:
    """A row with exactly the surface ``wreath._native._postgres.Record`` has.

    Deliberately *not* a mapping. ``list(record)`` works, because the sequence
    protocol falls back to ``__getitem__`` until ``IndexError`` -- but
    ``dict(record)`` raises, ``record.values()`` raises, and ``"a" in record``
    compares against the *values*, not the column names. Every one of those is
    measured behaviour, and each is a way a dict-shaped fake quietly diverges.
    """

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._columns.index(key)]
            except ValueError:
                raise KeyError(key) from None
        if isinstance(key, int):
            if not -len(self._values) <= key < len(self._values):
                raise IndexError("Record index out of range")
            return self._values[key]
        raise TypeError(f"Record indices must be int or str, not {type(key).__name__}")

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        pairs = zip(self._columns, self._values, strict=True)
        body = " ".join(f"{n}={v!r}" for n, v in pairs)
        return f"<FakeRecord {body}>"


def record(mapping: dict[str, Any]) -> FakeRecord:
    """A ``FakeRecord`` from the dict a fake naturally builds."""
    return FakeRecord(tuple(mapping), tuple(mapping.values()))


# --- the rules, re-exported --------------------------------------------------
#
# `check_bindable`, `check_single_statement` and the cast tables live in
# `wreath._replay_adapters` and are imported above. They are named here in the
# vocabulary a test reads in, but they are the same functions the shipped
# doubles use -- there is no second copy to drift.


class PreparedStatements:
    """Remembers which SQL texts a connection has already run.

    A cast on a placeholder only bites on the *second* execution, so a fake with
    no notion of preparing cannot reproduce the defect that shipped. Modelling
    it takes one set.
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def check(self, sql: str, args: tuple[Any, ...]) -> None:
        refuse_uninferable_cast(sql, args, self.seen)


def check_statement(
    sql: str, args: tuple[Any, ...], prepared: PreparedStatements | None = None
) -> None:
    """Every refusal a real connection would make before running anything."""
    check_single_statement(sql)
    check_bindable(args)
    if prepared is not None:
        prepared.check(sql, args)
