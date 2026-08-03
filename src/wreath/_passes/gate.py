"""The terminal gate: materialise, verify, and only then the irreversible step.

Every arrow in that sequence is a place where reversing the order loses data
permanently, so the ledger records which stage the pass is in and every
transition is a compare-and-swap. A process that dies between `verifying` and
`verified` re-verifies on restart rather than proceeding on trust, which is
always the right choice: verification is idempotent and cheap relative to the
thing it guards.

**Counters are progress, never proof.** `rows_done == denominator` is a
statement about the pass's own bookkeeping, and the failure it absorbs perfectly
is a walk that skipped one range and double-counted another. So verification is
always a question the *database* answers, independent of the walk -- and for the
case wreath is unusually placed to handle, it is the constraint the database
will go on enforcing afterwards (`Constraint`), which is the one form
where "did the check match the walk's own mistake?" cannot arise at all.

The gate always writes a durable verified fact. Running an irreversible step is
separate and opt-in, because the two callers need different things: a deferred
migration's terminal step is *permission for a later migration someone else
runs*, while a rollup owns the partition it is dropping. One mechanism covers
both -- the fact is published either way, and `then=` is consumed only by the
caller that has something to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..postgres import PostgresError
from .keyset import PassDeclarationError

#: Phases the gate owns. `walking` and `done` belong to the walk.
VERIFYING = "verifying"
VERIFIED = "verified"
APPLYING = "applying"
#: A verification that ran and answered "no". Deliberately its own phase rather
#: than sharing `blocked` with a dead-lettered chunk: a hole is cleared by
#: retrying the chunk, and this is not retryable at all (§10.7), so letting
#: `wreath passes retry` treat them alike would be the whole point missed.
UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Verification:
    """What a verification answered, and whether the answer was final."""

    ok: bool
    detail: str = ""
    #: The check could not run -- a connection dropped, a lock timed out. Not an
    #: answer, so the pass retries rather than concluding anything.
    transient: bool = False


def _sqlstate(error: BaseException) -> str:
    for attribute in ("sqlstate", "pgcode", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


#: `check_violation`. A constraint that failed to validate is an answer; every
#: other error is the check not having run.
CHECK_VIOLATION = "23514"


def _violation(error: BaseException) -> bool:
    if _sqlstate(error) == CHECK_VIOLATION:
        return True
    # A driver that does not surface SQLSTATE still says this in words, and
    # guessing wrong here is safe in one direction only: treating a violation as
    # transient retries a check that will fail identically, which is noisy but
    # not destructive, while the reverse would block a healthy pass.
    return "violates check constraint" in str(error).lower()


@dataclass(frozen=True, slots=True)
class NoRowsMatch:
    """Verified when this predicate matches no rows.

    The plain form of "ask the table the question the irreversible step depends
    on": `NoRowsMatch("grade_text IS NULL")` before a migration narrows the
    column. It must not restate the walk's own `where` -- see
    `refuse_reused_predicate` -- because a walk whose predicate was subtly
    wrong would then verify its own bug and report success.
    """

    where: str

    def __post_init__(self) -> None:
        if not isinstance(self.where, str) or not self.where.strip():
            raise PassDeclarationError(
                "NoRowsMatch(...) needs a SQL predicate the database can answer"
            )

    @property
    def signature(self) -> str:
        return _normalise(self.where)

    def explain(self, walk: Any) -> str:
        return f"no rows in {walk.table} match ({self.where})"

    async def check(self, executor: Any, *, walk: Any, scope: str | None = None) -> Verification:
        where = self.where if scope is None else f"({self.where}) AND ({scope})"
        try:
            found = await executor.fetchrow(
                f"SELECT 1 AS present FROM {walk.table} WHERE {where} LIMIT 1"
            )
        except PostgresError as error:
            # Narrow on purpose: the database failing to answer is transient, but
            # a TypeError building this SQL is a bug and must not be reported as
            # "could not run" -- that is a wrong answer wearing a retry.
            return Verification(False, f"verification could not run: {error!r}", transient=True)
        if found is None:
            return Verification(True, self.explain(walk))
        return Verification(False, f"rows still match ({where}) in {walk.table}")


@dataclass(frozen=True, slots=True)
class Reconcile:
    """Verified when two independent counts agree.

    A real reconciliation of a materialised tier against a recount of the source
    -- cheap at coarse grain, and possible only *before* the source is removed,
    which is the only moment it will ever be possible.

    Both arguments are scalar SQL. They are interpolated, so they are the
    declaration's own text and never a request value.
    """

    source: str
    against: str

    def __post_init__(self) -> None:
        for value, name in ((self.source, "source"), (self.against, "against")):
            if not isinstance(value, str) or not value.strip():
                raise PassDeclarationError(
                    f"Reconcile({name}=...) needs a scalar SQL query"
                )

    @property
    def signature(self) -> str:
        return _normalise(f"{self.source}||{self.against}")

    def explain(self, walk: Any) -> str:
        return f"({self.source}) reconciles with ({self.against})"

    async def check(self, executor: Any, *, walk: Any, scope: str | None = None) -> Verification:
        try:
            left = await executor.fetchval(self.source)
            right = await executor.fetchval(self.against)
        except PostgresError as error:
            # See NoRowsMatch.check: only a database failure is a could-not-run.
            return Verification(False, f"verification could not run: {error!r}", transient=True)
        if left == right:
            return Verification(True, f"{self.explain(walk)}: both {left!r}")
        return Verification(False, f"{self.source} = {left!r} but {self.against} = {right!r}")


@dataclass(frozen=True, slots=True)
class Constraint:
    """Verified by asking the database, in the terms it will hold you to.

    `NOT VALID` is instant and checks nothing; `VALIDATE CONSTRAINT` scans
    under `SHARE UPDATE EXCLUSIVE`, which blocks neither reads nor writes, and
    names the offending row when it fails.

    This is the form §10.3's concern cannot arise in. The verification and the
    thing that will enforce the invariant afterwards are the *same predicate*,
    so there is no way for the check to agree with a walk that was wrong -- and
    it is available only because the same tool emits the DDL. A bolt-on backfill
    library has to hand-write a `SELECT` and hope it matches the constraint
    somebody adds later.

    The constraint is left in place on success, which is the point: the table
    goes on refusing what the pass just finished ruling out.
    """

    name: str
    check_: str

    def __post_init__(self) -> None:
        from .keyset import _IDENTIFIER  # one shared identifier rule

        if not _IDENTIFIER.fullmatch(self.name or ""):
            raise PassDeclarationError(
                f"Constraint(name={self.name!r}) must be a plain SQL identifier"
            )
        if not isinstance(self.check_, str) or not self.check_.strip():
            raise PassDeclarationError("Constraint(...) needs a CHECK expression")

    @property
    def signature(self) -> str:
        return _normalise(self.check_)

    def explain(self, walk: Any) -> str:
        return f"{walk.table} satisfies CHECK ({self.check_}) as {self.name}"

    async def check(self, executor: Any, *, walk: Any, scope: str | None = None) -> Verification:
        if scope is not None:
            return Verification(
                False,
                "Constraint verifies a whole table, so it cannot be used with "
                "Gate(scope='unit')",
            )
        try:
            await executor.execute(
                f"ALTER TABLE {walk.table} ADD CONSTRAINT {self.name} "
                f"CHECK ({self.check_}) NOT VALID"
            )
        except PostgresError as error:
            # Already there from an earlier attempt is the ordinary case on a
            # re-verify, and re-validating it is exactly what should happen.
            if "already exists" not in str(error).lower():
                return Verification(
                    False, f"could not add {self.name}: {error!r}", transient=True
                )
        try:
            await executor.execute(
                f"ALTER TABLE {walk.table} VALIDATE CONSTRAINT {self.name}"
            )
        except PostgresError as error:
            if _violation(error):
                return Verification(False, f"{self.name} does not hold: {error}")
            return Verification(False, f"could not validate: {error!r}", transient=True)
        return Verification(True, self.explain(walk))


def _normalise(text: str) -> str:
    return " ".join(str(text).split()).lower()


@dataclass(frozen=True, slots=True)
class Gate:
    """Verify, publish the fact, and only then run whatever is irreversible.

    Args:
        verify: `NoRowsMatch`, `Reconcile` or `Constraint`.
        publishes: the name of the fact this pass establishes, written into the
            ledger once verification passes. A later migration reads it -- see
            `published_facts` -- which is how a deferred migration's
            terminal step becomes *permission for someone else's future step*
            rather than a statement this pass runs.
        then: an optional async callable run once, after the fact is published,
            under a phase compare-and-swap. Called as ``then(executor, walk,
            unit)`, where *unit* is `None`` for a whole-pass gate and the
            chunk's `(from, to)` range for a per-unit one.
        scope: `"pass"` verifies the whole table once the walk completes;
            `"unit"` verifies each chunk as the walk passes it, for a
            recurring pass where one bad bucket must not freeze the ladder.

    `scope` is the one place in this design where a flag changes control flow
    rather than a value, and it is flagged in the design as the judgement call
    most wanting review. The defence is that the sequence, the verification
    grades, the publish and the opt-in-ness are identical in both; only the loop
    the sequence sits inside differs.
    """

    verify: Any
    publishes: str | None = None
    then: Any = None
    scope: str = "pass"

    def __post_init__(self) -> None:
        if not hasattr(self.verify, "check"):
            raise PassDeclarationError(
                "Gate(verify=...) must be NoRowsMatch(...), Reconcile(...) or "
                f"Constraint(...); got {self.verify!r}"
            )
        if self.scope not in ("pass", "unit"):
            raise PassDeclarationError(
                f"Gate(scope=...) must be 'pass' or 'unit'; got {self.scope!r}"
            )
        if self.then is not None and not callable(self.then):
            raise PassDeclarationError("Gate(then=...) must be an async callable")
        if self.publishes is not None and not str(self.publishes).strip():
            raise PassDeclarationError(
                "Gate(publishes=...) names the fact this pass establishes, so it "
                "needs a name rather than an empty string"
            )
        if self.publishes is None and self.then is None:
            raise PassDeclarationError(
                "a Gate must do something with what it verified: publishes= names "
                "a fact a later migration can read, then= runs an irreversible "
                "step here. A gate with neither verifies and discards the answer."
            )
        if self.scope == "unit" and self.publishes is not None:
            raise PassDeclarationError(
                "Gate(scope='unit') has no whole-pass completion, so there is no "
                "moment at which a fact about the whole table becomes true. Use "
                "then= for per-unit work, or scope='pass' to publish."
            )


def refuse_reused_predicate(gate: Gate, work: Any) -> None:
    """Refuse a verification that just restates the walk's own predicate.

    If the walk selected `WHERE grade_text IS NULL` and the verification asks
    `WHERE grade_text IS NULL`, a walk whose predicate was subtly wrong
    verifies its own bug and reports success -- the same defect as a check that
    silently had nothing to check.

    This is a weak check and does not pretend otherwise: it catches the literal
    restatement, which is the one people write. `Constraint` is the
    strong version, because there the verification *is* the invariant.
    """
    signature = getattr(gate.verify, "signature", None)
    if signature is None:  # pragma: no cover - every shipped grade has one
        return
    where = getattr(work, "where", None)
    if where is None or not isinstance(where, str):
        return
    if _normalise(where) == signature:
        raise PassDeclarationError(
            f"the gate verifies ({where}) and the work walks ({where}) -- the same "
            "predicate. A walk whose predicate was wrong would verify its own bug "
            "and report success. Derive the verification from the invariant the "
            "irreversible step needs, not from the walk; or use "
            "Constraint(...), where the check and the thing the database will go "
            "on enforcing are the same expression by construction."
        )


__all__ = [
    "APPLYING",
    "Constraint",
    "Gate",
    "NoRowsMatch",
    "Reconcile",
    "UNVERIFIED",
    "VERIFIED",
    "VERIFYING",
    "Verification",
    "refuse_reused_predicate",
]
