"""Finding the N+1 query: the tally, the finding, and the trace scan.

An N+1 is fifty fast queries where one belonged. Nothing about it looks wrong
from inside a single layer -- the ORM sees a perfectly ordinary `SELECT`, the
server sees a request that returned 200, and the test suite sees green. It is
visible only from a vantage point that holds the route *and* the queries at
once, which is why the usual answer is a bolt-on profiler that knows neither.

Wreath owns both, so it can name the offender: *this route issued fifty-one
statements, and fifty of them hydrated `Trek`.* That sentence contains the fix.

Two ways in, one vocabulary:

* A `QueryLedger` bound to the running request counts what the ORM
  hydrates and trips the moment a model is queried once too often, so the
  traceback lands on the line that did it. This is the development path.
* `find_n_plus_one` reads the same fact out of recorded traces --
  `ORM_HYDRATE` phases carry the model, the completion carries the route --
  so a production endpoint can be diagnosed without reproducing it.

Both produce a `Finding`, so the sentence a developer reads in a
traceback is the sentence an operator reads in `wreath doctor`.

A request is not the only place an N+1 happens, and it is not the worst. A
durable job, a workflow step and a chunked-pass shift do the heaviest ORM work
in most systems, and an N+1 *per chunk* is the classic production disaster --
it passes every test on a table small enough to fit in one chunk, then runs for
six hours against the real one. `watching` is the general scope those bind, and
`Origin` is what tells a reader which of them a finding came from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "WATCHING",
    "Finding",
    "NPlusOneDetected",
    "Origin",
    "QueryLedger",
    "Repetition",
    "check_budget",
    "find_n_plus_one",
    "query_ledger",
    "scope_for",
    "watch",
    "watching",
]

#: What an observing scope counts to before it has anything to report. Only
#: reached by a scope that did not declare a budget, which is every background
#: scope in a process that installed a guard: it counts and reports, and the
#: number is the same ten a request guard defaults to.
DEFAULT_OBSERVE_LIMIT = 10

#: The running request's `QueryLedger`, or `None`.
query_ledger: ContextVar[Any] = ContextVar("wreath_query_ledger", default=None)

#: Whether any `NPlusOneGuard` exists in this process.
#: The ORM seam reads this module attribute before it reads `query_ledger`,
#: because a `ContextVar.get` is a Python/native boundary crossing and an
#: application that never installed a guard should not pay one per query to
#: discover that. It latches on and is never cleared: a guard that existed once
#: may have bound a ledger to a request still in flight.
WATCHING = False


def watch() -> None:
    """Arm the ORM seam. Called when a guard is constructed."""
    global WATCHING
    WATCHING = True


#: Phase names as the Inspector puts them on the wire (`PhaseKind` lowercased).
_ORM_HYDRATE = "orm_hydrate"
_DB_QUERY = "db_query"


@dataclass(frozen=True, slots=True)
class Repetition:
    """One model queried repeatedly inside a single execution scope."""

    model: str
    count: int
    total_us: int = 0


@dataclass(frozen=True, slots=True)
class Origin:
    """Which execution scope a finding came from.

    `kind` is one of `request`, `job`, `step` or `shift`. `label` is what a
    reader should be shown -- a route, a task name, a step name -- and
    `identifier` is what they should go and look up: a job id, a workflow
    instance id, empty for a request (whose id lives on `Finding.request_id`,
    where `wreath replay` already looks for it).
    """

    kind: str = "request"
    label: str = ""
    identifier: str = ""

    def explain(self) -> str:
        """`GET /llamas` for a request; `job ingest_card#41` for an attempt."""
        if self.kind == "request":
            return self.label
        suffix = f"#{self.identifier}" if self.identifier else ""
        return f"{self.kind} {self.label}{suffix}"


@dataclass(frozen=True, slots=True)
class Finding:
    """One scope that queried a model far more often than it should have."""

    route: str
    #: Repetitions past the threshold, worst first.
    repetitions: tuple[Repetition, ...]
    #: Database statements attributed to the scope. From a recorded trace this
    #: is every statement; from a live ledger it is the ORM's, which is what the
    #: guard can see.
    queries: int = 0
    #: The recorded request this came from, or 0 when it came from a live
    #: ledger. It is what `wreath replay` needs to turn this into a test.
    request_id: int = 0
    #: Which scope produced this. Defaults to a request named by `route`, so
    #: every existing producer and reader of a `Finding` keeps working.
    origin: Origin = field(default_factory=Origin)

    @property
    def worst(self) -> Repetition:
        """The repetition to fix first."""
        return self.repetitions[0]

    def explain(self) -> str:
        """One line that contains the diagnosis and implies the fix."""
        worst = self.worst
        others = f" (and {len(self.repetitions) - 1} more)" if len(self.repetitions) > 1 else ""
        where = self.origin.explain() or self.route
        return (
            f"{where} issued {self.queries} statements; "
            f"{worst.count} of them hydrated {worst.model}{others}"
        )


class NPlusOneDetected(RuntimeError):
    """Raised at the query that crossed `QueryLedger`'s limit.

    Deliberately raised from the ORM call rather than reported at the end of the
    request: the whole difficulty with an N+1 is finding the loop, and a
    traceback is a precise answer to exactly that question.
    """

    __slots__ = ("finding",)

    def __init__(self, finding: Finding) -> None:
        super().__init__(finding.explain())
        self.finding = finding


class QueryLedger:
    """Per-request tally of ORM queries by model.

    Passive by default: it counts, and `finding` reports afterwards.
    Give it `on_exceeded` and it acts the moment a model crosses `limit` --
    once per model, so a runaway loop produces one diagnosis rather than a
    thousand.
    """

    __slots__ = ("counts", "limit", "on_exceeded", "origin", "route", "_tripped")

    def __init__(
        self,
        *,
        limit: int,
        route: str = "",
        origin: Origin | None = None,
        on_exceeded: Callable[[Finding], None] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.counts: dict[str, int] = {}
        self.limit = limit
        #: A request scope is spelled with `route=`, which is the older and
        #: shorter way to say `Origin(kind="request", label=route)`. Keeping
        #: both means no existing caller changed.
        self.origin = origin if origin is not None else Origin(label=route)
        self.route = route or self.origin.label
        self.on_exceeded = on_exceeded
        self._tripped: set[str] = set()

    def record(self, model: str) -> None:
        """Count one query that hydrated `model`.

        `model` is a key, not a label: the ORM passes `module.QualName`,
        because two models of the same name in different modules would
        otherwise share a tally and trip this on two innocent reads. What a
        reader is shown comes from `_display`.
        """
        count = self.counts.get(model, 0) + 1
        self.counts[model] = count
        if count < self.limit or model in self._tripped or self.on_exceeded is None:
            return
        self._tripped.add(model)
        self.on_exceeded(
            Finding(
                route=self.route,
                repetitions=(Repetition(model=self._display(model), count=count),),
                queries=sum(self.counts.values()),
                origin=self.origin,
            )
        )

    def _display(self, key: str) -> str:
        """The shortest name that still says which model this is.

        The module prefix is noise until it is the answer, so it appears only
        when this ledger has counted another model with the same bare name --
        which is exactly when a reader would otherwise be told to go and look
        at a `Trek` without being told *which* `Trek`.
        """
        bare = key.rpartition(".")[2] or key
        for other in self.counts:
            if other != key and (other.rpartition(".")[2] or other) == bare:
                return key
        return bare

    def finding(self) -> Finding | None:
        """Everything at or past the limit when the request ended, or None."""
        repetitions = tuple(
            sorted(
                (
                    Repetition(model=self._display(model), count=count)
                    for model, count in self.counts.items()
                    if count >= self.limit
                ),
                key=lambda item: (-item.count, item.model),
            )
        )
        if not repetitions:
            return None
        return Finding(
            route=self.route,
            repetitions=repetitions,
            queries=sum(self.counts.values()),
            origin=self.origin,
        )

    def __repr__(self) -> str:
        return f"<QueryLedger origin={self.origin.explain()!r} limit={self.limit} {self.counts}>"


@contextmanager
def watching(
    origin: Origin,
    *,
    limit: int,
    raises: bool = False,
) -> Iterator[QueryLedger]:
    """Bind a `QueryLedger` for one execution scope, and unbind it after.

    This is the general form the request guard is the first caller of. It arms
    the ORM seam, binds the ledger to the running context, and resets the
    binding on every exit path -- including a raise and a cancellation, which
    is not optional: a worker that leaked a ledger would accumulate counts for
    its whole lifetime and eventually report a fiction.

    `raises=True` makes the scope fail at the query that crossed `limit`, which
    is what a declared budget means. It is deliberately not the default:
    raising inside a durable job turns a slow job into a failed one and then
    into a retry storm, and the framework does not know whether five hundred
    queries was a defect or the point of the task.
    """
    watch()
    ledger = QueryLedger(limit=limit, origin=origin, on_exceeded=_raise if raises else None)
    token = query_ledger.set(ledger)
    try:
        yield ledger
    finally:
        # Reset rather than set(None): a nested scope must hand the outer one
        # its own binding back, not a cleared one.
        query_ledger.reset(token)


def scope_for(origin: Origin, *, budget: int | None) -> Any:
    """The ledger scope for one background execution, or nothing when nobody asked.

    The one place the three-state rule lives, so a job, a workflow step and a
    pass shift cannot drift apart on it:

    * a declared budget -- count, and fail at the ceiling;
    * no budget, but a guard exists somewhere in this process -- count and
      report, because something already asked to be told;
    * neither -- bind nothing. An application that never asked for this must
      not pay a ContextVar read per query to discover that, which is the whole
      reason `WATCHING` is a latched module flag rather than a lookup.
    """
    if budget is None and not WATCHING:
        return nullcontext(None)
    return watching(
        origin,
        limit=budget if budget is not None else DEFAULT_OBSERVE_LIMIT,
        raises=budget is not None,
    )


def check_budget(budget: int | None, subject: str) -> None:
    """Refuse a nonsense budget where the traceback still names the declaration."""
    if budget is not None and budget < 1:
        raise ValueError(
            f"{subject} declares query_budget={budget}; a budget is a count of "
            "queries to one model and must be >= 1. Omit it to observe without "
            "a ceiling."
        )


def _raise(finding: Finding) -> None:
    raise NPlusOneDetected(finding)


def find_n_plus_one(
    traces: Iterable[Mapping[str, Any]],
    *,
    threshold: int = 10,
    routes: Sequence[Mapping[str, Any]] = (),
    models: Sequence[Mapping[str, Any]] = (),
) -> list[Finding]:
    """Scan recorded traces for requests that repeated one model's query.

    `traces` are Inspector `timeline` traces; `routes` and `models` are
    its `metadata` tables, used only to turn IDs into names. An ID with no
    entry degrades to `model:5` rather than being dropped -- a metadata image
    that has moved on is a reason to read the number, not to hide the finding.

    Returns findings worst first, so the top of the list is where to start.
    """
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    route_names = {
        entry["id"]: f"{entry.get('method', '')} {entry.get('path', '')}".strip()
        for entry in routes
    }
    model_names = {entry["id"]: entry["name"] for entry in models}

    findings: list[Finding] = []
    for trace in traces:
        counts: dict[int, int] = {}
        durations: dict[int, int] = {}
        statements = 0
        for phase in trace.get("phases", ()):
            name = phase.get("phase")
            if name == _DB_QUERY:
                statements += 1
            elif name == _ORM_HYDRATE:
                model_id = phase.get("dependency_id", 0)
                counts[model_id] = counts.get(model_id, 0) + 1
                durations[model_id] = durations.get(model_id, 0) + phase.get("duration_us", 0)
        repetitions = tuple(
            sorted(
                (
                    Repetition(
                        model=model_names.get(model_id, f"model:{model_id}"),
                        count=count,
                        total_us=durations[model_id],
                    )
                    for model_id, count in counts.items()
                    if count >= threshold
                ),
                key=lambda item: (-item.count, item.model),
            )
        )
        if not repetitions:
            continue
        route_id = trace.get("route_id", 0)
        route = route_names.get(route_id) or f"route:{route_id}"
        findings.append(
            Finding(
                route=route,
                repetitions=repetitions,
                queries=statements,
                request_id=trace.get("request_id", 0),
                # A recorded trace is a request by construction. Naming the
                # origin here rather than leaving it defaulted means the report
                # groups these the same way it groups a live background finding.
                origin=Origin(kind="request", label=route),
            )
        )
    findings.sort(key=lambda f: (-f.worst.count, f.request_id))
    return findings
