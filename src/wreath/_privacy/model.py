"""The typed plan `wreath.privacy.plan` returns, and the vocabulary it speaks.

Every type here is frozen and holds only data: no method reaches back into the
application, opens a connection, or writes a row. That separation is the whole
safety argument -- an erasure plan is meant to be rendered, compared, digested
and *read by a person* before anything deletes anything, so it must be inert
once built. `wreath.infra.model` is the precedent and this follows it
deliberately.

The vocabulary is small and it draws one line very hard:

**Erasure is irreversible. Pseudonymisation is not erasure.**

`Erase.NULL` and `Erase.REDACT` destroy the value. A hash -- keyed or not --
does not: it is a stable identifier for the same person, which is the definition
of pseudonymous data rather than erased data, and calling it erasure in a
compliance report is a false statement about a subject's rights. So there is no
`Erase.HASH`. There is `Pseudonymise`, it is a different type, and it cannot be
spelled without a written reason -- the shape `wreath.passes.Declared` already
established for "this needs a human to have thought about it".
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import blake2b
from typing import Any

__all__ = [
    "ColumnAction",
    "CycleFinding",
    "Disposal",
    "Edge",
    "Erase",
    "ErasurePlan",
    "ExportPlan",
    "OrphanRisk",
    "Pseudonymise",
    "Reach",
    "Retained",
    "SurvivingReference",
    "TableAction",
    "Unreachable",
    "as_dict",
    "plan_digest",
]


class Erase(StrEnum):
    """What happens to one classified column when its subject is erased.

    Three values, and the absent fourth is the point. `NULL` and `REDACT` are
    irreversible: after either, the original value cannot be recovered from the
    row. `RETAIN` keeps the value and says so out loud, so a plan can show a
    reader exactly which personal data survives and force the question of why.

    There is deliberately no hash disposition here. See `Pseudonymise`.
    """

    #: Set the column to NULL. Refused at declaration for a NOT NULL column,
    #: because the erasure would fail at three in the morning instead.
    NULL = "null"
    #: Overwrite with a fixed, value-independent marker. For a NOT NULL column,
    #: this is what `NULL` would have been.
    REDACT = "redact"
    #: Keep the value. Legitimate interest, legal hold, or a column that is
    #: personal but load-bearing. Always printed in the plan.
    RETAIN = "retain"


@dataclass(frozen=True, slots=True)
class Pseudonymise:
    """Replace a value with a stable non-identifying token, having said why.

    This is **not** erasure and this type exists so that nothing can pretend it
    is. A pseudonymised column still distinguishes one subject from another, so
    the data remains personal data; every plan that contains one says so, and
    the reason is printed beside it.

    The reason is `wreath.passes.Declared` rather than a second string wrapper,
    because it is the same idea -- a decision a human has to have made in
    writing, refused when empty -- and two spellings of that would drift.
    """

    #: A `wreath.passes.Declared`. Typed loosely to keep this module free of a
    #: `wreath.passes` import; `registry.classify` does the isinstance check
    #: where the refusal message can name the caller's mistake.
    reason: Any

    @property
    def text(self) -> str:
        return str(getattr(self.reason, "reason", self.reason))


class Disposal(StrEnum):
    """What happens to one whole table's rows for this subject."""

    #: Every matching row is deleted.
    DELETE = "delete"
    #: Rows survive; classified columns are nulled, redacted or pseudonymised.
    ANONYMISE = "anonymise"
    #: Rows survive untouched, under a declared exemption.
    RETAIN = "retain"
    #: The parent's own `ON DELETE CASCADE` removes these rows. Named anyway:
    #: a cascade is a deletion, and a plan that hides it is not a plan.
    CASCADE = "cascade"


@dataclass(frozen=True, slots=True)
class Edge:
    """One foreign key, as the ORM declared it.

    `on_delete` is PostgreSQL's own `confdeltype` code -- `a` no action,
    `r` restrict, `c` cascade, `n` set null, `d` set default -- carried
    verbatim from `wreath.orm.schema.ColumnRef` rather than translated, so it
    compares byte-for-byte with the catalog and with a migration signature.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on_delete: str
    deferrable: bool = False

    def explain(self) -> str:
        return f"{self.from_table}.{self.from_column} -> {self.to_table}.{self.to_column}"


@dataclass(frozen=True, slots=True)
class Reach:
    """How a table reaches the subject, as the chain of foreign keys walked.

    An empty path means the table carries the subject column itself. A plan
    prints the path because "why is this table in my erasure?" is the first
    question a reviewer asks, and a table name alone cannot answer it.
    """

    path: tuple[Edge, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.path)

    def explain(self) -> str:
        if not self.path:
            return "declares the subject column"
        return " via ".join(edge.explain() for edge in self.path)


@dataclass(frozen=True, slots=True)
class ColumnAction:
    """One classified column and what the erasure does to it."""

    column: str
    erase: str
    #: Set when the disposition is a `Pseudonymise`; the written reason.
    pseudonym_reason: str | None = None
    note: str = ""

    @property
    def irreversible(self) -> bool:
        """Whether this action destroys the value.

        The property the whole module turns on, so it is computed in one place
        rather than re-derived by each renderer and each executor.
        """
        return self.erase in (Erase.NULL.value, Erase.REDACT.value)


@dataclass(frozen=True, slots=True)
class TableAction:
    """One table's part of an erasure, and how it was reached."""

    model: str
    schema: str
    table: str
    disposal: str
    reach: Reach
    columns: tuple[ColumnAction, ...] = ()
    #: The column this table's rows are matched on, and the value's origin.
    #: For a directly-classified table this is the subject column; for a
    #: reached one it is the leading foreign key of `reach.path`.
    match_column: str = ""
    reason: str = ""
    #: Order within the erasure. Children before parents, so a restricting or
    #: no-action foreign key does not refuse the parent's delete.
    order: int = 0


@dataclass(frozen=True, slots=True)
class Retained:
    """A table reached by the traversal that is deliberately not erased.

    The audit log is the canonical member: an erasure that deletes the record
    of itself is a compliance failure in the other direction. Every one carries
    the written exemption that put it here.
    """

    model: str
    schema: str
    table: str
    reason: str
    reach: Reach = field(default_factory=Reach)


@dataclass(frozen=True, slots=True)
class Unreachable:
    """A table holding classified personal data with no path to the subject.

    The finding this module exists to produce. Silent omission -- an erasure
    that quietly misses a table -- is exactly what the EDPB's February 2026
    coordinated enforcement report found controllers doing, and it is invisible
    unless something enumerates the classified tables and subtracts the
    reachable ones.
    """

    model: str
    schema: str
    table: str
    columns: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class OrphanRisk:
    """A `SET NULL`/`SET DEFAULT` edge that would strand personal data.

    The subtlest defect in the category. Deleting the parent leaves the child
    row alive with its foreign key nulled -- so the child still holds the
    subject's data and, having lost the only column that pointed at the
    subject, can never be found again. The plan therefore orders the child
    before the parent and says why.
    """

    edge: Edge
    detail: str


@dataclass(frozen=True, slots=True)
class SurvivingReference:
    """A row that outlives the parent this erasure deletes, and still points at it.

    The finding a plan review cannot make by eye, and the one that turns a
    successful-looking erasure into a half-run one. `NO ACTION` and `RESTRICT`
    are handled by ordering *only when the child rows are themselves deleted*;
    a child that is anonymised, retained under an exemption, or simply not
    classified keeps its foreign key, and the parent's `DELETE` is then refused
    by the database at three in the morning with the subject already told they
    were erased.

    Reported against the whole graph rather than against the plan's tables,
    because the row that refuses the delete is very often one nothing declared
    -- an unclassified join table is exactly as good at holding a foreign key
    as a classified one.
    """

    edge: Edge
    detail: str


@dataclass(frozen=True, slots=True)
class CycleFinding:
    """A foreign-key cycle among the tables this erasure touches.

    Named rather than resolved. A cycle means no ordering of plain deletes
    exists; either the constraints are deferrable and the erasure runs in one
    transaction, or a human breaks the cycle by nulling an edge first. Guessing
    which is how an erasure half-runs.
    """

    tables: tuple[str, ...]
    deferrable: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ErasurePlan:
    """Everything one subject's erasure would do, derived from declarations.

    Inert. Building it opens no connection and deletes nothing; `digest`
    is what lets `wreath privacy erase` prove it is executing the plan that was
    printed rather than a different one computed later.
    """

    subject_model: str
    subject_column: str
    subject_id: str
    tables: tuple[TableAction, ...] = ()
    retained: tuple[Retained, ...] = ()
    unreachable: tuple[Unreachable, ...] = ()
    orphan_risks: tuple[OrphanRisk, ...] = ()
    cycles: tuple[CycleFinding, ...] = ()
    surviving_references: tuple[SurvivingReference, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether a person must resolve something before this can run.

        Three ways the plan is incomplete, and an incomplete erasure that runs
        anyway is worse than one that refuses: the subject is told they were
        erased. Unreachable classified data would leave rows behind; an
        unresolvable cycle admits no ordering; and a surviving reference means
        the database will refuse the delete, which leaves the erasure half-run
        rather than not run.
        """
        return (
            bool(self.unreachable)
            or bool(self.surviving_references)
            or any(not c.deferrable for c in self.cycles)
        )

    @property
    def digest(self) -> str:
        return plan_digest(self)


@dataclass(frozen=True, slots=True)
class ExportPlan:
    """The read-mode traversal behind a subject-access request.

    The same graph walk as an erasure with none of the writes, which is why it
    is derived from the same plan rather than from a second traversal that
    could disagree with it.
    """

    subject_model: str
    subject_column: str
    subject_id: str
    tables: tuple[TableAction, ...] = ()
    #: Tables holding the subject's data that the export will not contain,
    #: with the reason -- an exemption is a gap in a subject-access response
    #: just as much as in an erasure, and the subject is entitled to know.
    withheld: tuple[Retained, ...] = ()
    unreachable: tuple[Unreachable, ...] = ()


def as_dict(plan: ErasurePlan | ExportPlan) -> dict[str, Any]:
    """The plan as plain JSON-compatible data, for `--format json`.

    `dataclasses.asdict` drops properties, so `blocked` and `digest` are
    re-added here rather than stored: computing them twice from one definition
    is what stops the two renderings disagreeing.
    """
    data = dataclasses.asdict(plan)
    if isinstance(plan, ErasurePlan):
        data["blocked"] = plan.blocked
        data["digest"] = plan.digest
    return data


def plan_digest(plan: ErasurePlan) -> str:
    """A stable digest of everything the plan would do.

    `wreath privacy erase --plan <digest>` recomputes the plan and refuses when
    the digest moved, which is what makes "executes a plan that was printed" a
    checkable property rather than a hope. The digest deliberately covers the
    findings as well as the actions: a plan that has grown a newly unreachable
    table is a different plan, even if every action in it is unchanged.
    """
    payload = dataclasses.asdict(plan)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return blake2b(encoded, digest_size=16).hexdigest()
