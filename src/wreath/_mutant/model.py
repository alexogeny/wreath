"""What a mutation is, and what running one produced.

A mutation here is not "flip `<` to `<=`". It is *the removal of one named
control*: a clause dropped from an authorization predicate, a `raise` that
refuses a request turned into a `pass`, a withheld field set that stops
withholding. Every record therefore carries a `control` -- the English sentence
for what was taken away -- because that sentence is what the reader has to
judge, and a diff of two bytecode objects is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class Outcome(StrEnum):
    """What one mutant did when the tests it could reach were run at it."""

    #: A test that passed at baseline failed with the control removed. The
    #: suite is watching this control.
    KILLED = "killed"
    #: Every test that reaches this line still passed without the control.
    #: A question, not a verdict -- see `Verdict.note`.
    SURVIVED = "survived"
    #: No test in the baseline run executed this line at all. That is a check
    #: with nothing to check in its purest form, and it is reported separately
    #: from `survived`, because
    #: "the suite would not notice" and "the suite never looks" are different
    #: findings with different fixes.
    UNREACHED = "unreached"
    #: The mutated source compiles to bytecode identical to the original.
    #: Provably equivalent; not a finding either way.
    EQUIVALENT = "equivalent"
    #: The run exceeded the per-mutant deadline. Undecided, and said so.
    TIMEOUT = "timeout"
    #: The mutation could not be built or applied. A defect in this tool, not
    #: in the suite under test, and counted where it can be seen.
    ERROR = "error"


#: Outcomes that are findings about the test suite rather than about this tool.
FINDINGS = (Outcome.SURVIVED, Outcome.UNREACHED)


@dataclass(frozen=True)
class ConfidenceRating:
    """A calm, actionable interpretation of one mutation sample."""

    label: str
    tone: str
    action: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "tone": self.tone, "action": self.action}


def rate_counts(counts: Mapping[str, int]) -> ConfidenceRating:
    """Prefer the most actionable finding without collapsing distinct outcomes."""
    killed = counts.get(Outcome.KILLED.value, 0)
    survived = counts.get(Outcome.SURVIVED.value, 0)
    unreached = counts.get(Outcome.UNREACHED.value, 0)
    timeout = counts.get(Outcome.TIMEOUT.value, 0)
    error = counts.get(Outcome.ERROR.value, 0)
    if survived:
        extra = f"; {unreached} more were not reached" if unreached else ""
        return ConfidenceRating(
            "REVIEW ASSERTIONS",
            "attention",
            f"{survived} sampled control(s) ran without an objection{extra}",
        )
    if unreached:
        return ConfidenceRating(
            "ADD COVERAGE",
            "warning",
            f"{unreached} sampled control(s) were not exercised by this run",
        )
    if timeout or error:
        if timeout:
            action = (
                f"{timeout} control(s) remain undecided; increase the mutation budget "
                "when convenient"
            )
        else:
            action = f"inspect {error} control(s) the mutation tool declined"
        return ConfidenceRating("FINISH THE SAMPLE", "incomplete", action)
    if killed:
        return ConfidenceRating(
            "SAMPLE WATCHED",
            "good",
            f"tests objected to all {killed} sampled control removal(s)",
        )
    return ConfidenceRating(
        "NO RATING",
        "neutral",
        "this run produced no mutation decision",
    )


@dataclass(frozen=True)
class Site:
    """Where in the tree a control is written down."""

    path: str
    """Repository-relative path of the file that declares the control."""

    line: int
    """1-based line of the mutated construct."""

    scope: str
    """Dotted name of the top-level definition that will be recompiled.

    This is deliberately the *outermost* enclosing function, not the innermost.
    A route handler defined inside a router factory has no reachable function
    object of its own; recompiling the factory means the next call to it builds
    the mutated handler, which is exactly when a test builds its app.
    """

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


class Patch(Protocol):
    """How a mutation is made real inside a forked child.

    Deliberately not serialisable. Mutants run by `fork()` from a parent that
    has already built every patch, so the child inherits live Python objects
    and applying one costs an attribute store rather than a re-import.
    """

    def apply(self) -> None:
        """Install the mutation. Raises on failure; the runner reports ERROR."""

    def undo(self) -> None:
        """Restore. Only used by the in-process self-tests, never by a fork."""


@dataclass(frozen=True)
class Mutation:
    """One control, removed one way."""

    identifier: str
    """Stable, human-typeable id: `operator@path:line`."""

    operator: str
    """Which operator produced it, e.g. `predicate.drop-operand`."""

    control: str
    """What was removed, in English. The line a reader judges."""

    site: Site

    module: str
    """Dotted module name that owns the mutated construct."""

    patch: Patch | None = field(repr=False, default=None)
    """How to install it. `None` only for the placeholder record that carries
    a mutation this tool declined to build, so the reason stays visible."""


@dataclass
class Verdict:
    """What happened when one mutation met the tests that reach it."""

    mutation: Mutation
    outcome: Outcome
    candidates: tuple[str, ...] = ()
    """Test node ids selected for this mutant by the coverage map."""

    killers: tuple[str, ...] = ()
    """Node ids that failed. Empty unless `outcome` is KILLED."""

    seconds: float = 0.0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.mutation.identifier,
            "operator": self.mutation.operator,
            "control": self.mutation.control,
            "path": self.mutation.site.path,
            "line": self.mutation.site.line,
            "scope": self.mutation.site.scope,
            "outcome": self.outcome.value,
            "candidates": len(self.candidates),
            "killers": list(self.killers[:20]),
            "seconds": round(self.seconds, 3),
            "note": self.note,
        }


@dataclass
class Report:
    """A whole run. A report, not a gate."""

    verdicts: list[Verdict] = field(default_factory=list)
    baseline_tests: int = 0
    baseline_failures: tuple[str, ...] = ()
    baseline_seconds: float = 0.0
    total_seconds: float = 0.0
    sources: tuple[str, ...] = ()
    live_kills: int = 0
    live_probes: int = 0
    live_completed: int = 0
    live_cancelled_at_seal: int = 0
    live_first_started_seconds: float | None = None

    def by_outcome(self, outcome: Outcome) -> list[Verdict]:
        return [v for v in self.verdicts if v.outcome is outcome]

    @property
    def decided(self) -> int:
        """Mutants with a real answer: killed or survived."""
        return len(self.by_outcome(Outcome.KILLED)) + len(self.by_outcome(Outcome.SURVIVED))

    @property
    def score(self) -> float | None:
        """Killed / decided. `None` when nothing was decided.

        Not a percentage to chase. A suite can reach 100% here and still be
        blind to the control nobody wrote a mutation for.
        """
        decided = self.decided
        if decided == 0:
            return None
        return len(self.by_outcome(Outcome.KILLED)) / decided

    def as_dict(self) -> dict[str, Any]:
        counts = {outcome.value: len(self.by_outcome(outcome)) for outcome in Outcome}
        return {
            "sources": list(self.sources),
            "baseline": {
                "tests": self.baseline_tests,
                "failures": list(self.baseline_failures),
                "seconds": round(self.baseline_seconds, 3),
            },
            "counts": counts,
            "rating": rate_counts(counts).as_dict(),
            "seconds": round(self.total_seconds, 3),
            "live_kills": self.live_kills,
            "live": {
                "probes": self.live_probes,
                "completed": self.live_completed,
                "killed": self.live_kills,
                "cancelled_at_seal": self.live_cancelled_at_seal,
                "first_started_seconds": (
                    round(self.live_first_started_seconds, 3)
                    if self.live_first_started_seconds is not None
                    else None
                ),
            },
            "mutants": [v.as_dict() for v in self.verdicts],
        }
