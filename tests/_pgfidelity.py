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
    ScriptedRecord as FakeRecord,
)
from wreath._replay_adapters import (
    driver_row_value,
    refuse_uninferable_cast,
)
from wreath._replay_adapters import (
    refuse_multiple_commands as check_single_statement,
)
from wreath._replay_adapters import (
    refuse_unbindable as check_bindable,
)
from wreath._replay_adapters import (
    scripted_row as record,
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
    "driver_row_value",
    "record",
]

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
