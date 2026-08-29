"""Turn a printed plan into chunked passes, and refuse to run a different one.

Execution is deliberately the small module. It builds no walk of its own: every
action becomes a `wreath.passes.ChunkedPass`, which already keyset-walks
without `OFFSET`, advances its cursor inside the chunk transaction, paces
itself against live traffic and resumes after a crash. Re-deriving any of that
here would be the fourth copy of a loop the repository already owns once.

Two properties are enforced here rather than hoped for:

**The plan that runs is the plan that was printed.** `erase` recomputes the
plan and compares digests. A model deployed since the plan was read, a
classification added, a table that has become unreachable -- each moves the
digest, and the erasure refuses rather than running a plan nobody reviewed.

**A blocked plan does not run at all.** Unreachable classified data means the
subject would be told they were erased while their data sits in a table the
traversal never reached. Refusing is the only honest outcome, and it is
checked here as well as reported by the planner, because the CLI is not the
only caller.

**The erasure records itself, and the record is gated on completion.** An
erasure is deliberately *not* one transaction -- it is a chunked, resumable
walk per table, each chunk committing with its own cursor -- so there is no
single transaction for a receipt to share, and pretending otherwise would be
the sort of claim this module exists not to make. What `record_erasure` does
instead is stronger in the direction that matters: it opens one transaction,
reads the pass ledger *inside it* to establish that every pass this erasure
declared has reached `done`, and writes the record only then. So a record
cannot exist for an erasure that did not finish. The other direction -- a
finished erasure whose record was lost to a crash in that window -- is
recoverable rather than silent, because re-running `erase` re-reads the same
ledger, finds the same completed passes and writes the record it is missing.
`wreath._privacy.record` holds the argument for the record's contents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .._pgname import validate_identifier
from .graph import Graph, build_graph
from .model import Disposal, Erase, ErasurePlan, TableAction
from .record import ErasureRecord
from .registry import PrivacyRegistry

__all__ = [
    "ErasureBlocked",
    "ErasureIncomplete",
    "PlanMoved",
    "PreparedErasure",
    "prepare",
    "predicate_for",
    "primary_key",
    "record_erasure",
]

#: What `Erase.REDACT` writes. Fixed and value-independent: a marker derived
#: from the old value would carry information about it, which is the whole
#: thing redaction is for.
REDACTED = "[erased]"


#: The same rule `wreath.passes.Table` applies to a table name, for the same
#: reason: the name is interpolated into statement text rather than bound.
class ErasureBlocked(RuntimeError):
    """The plan would leave personal data behind, so it will not run."""


class PlanMoved(RuntimeError):
    """The plan changed since it was printed; the review no longer applies."""


class ErasureIncomplete(RuntimeError):
    """A pass this erasure declared has not finished, so nothing is recorded.

    Raised rather than returned, and raised *before* the record is written.
    An erasure that stopped part-way and recorded itself anyway would be a
    receipt for something that did not happen, and the subject has already been
    told the opposite.
    """


@dataclass(frozen=True, slots=True)
class PreparedErasure:
    """One reviewed plan, resolved into the passes that carry it out."""

    plan: ErasurePlan
    #: `(action, ChunkedPass)` in running order. A `CASCADE` or `RETAIN`
    #: action has no pass and is still listed, because "the database does this
    #: one" is information the operator needs and a silent omission is not.
    steps: tuple[tuple[TableAction, Any], ...]
    #: Where this erasure records that it happened.
    record: ErasureRecord = field(default_factory=ErasureRecord)
    #: The pass ledger's schema and pool, carried so the recorder reads the
    #: same ledger the walks wrote to. A record derived from a different
    #: ledger would be a receipt for somebody else's erasure.
    schema: str = "wreath"
    workload: str = "write"

    @property
    def passes(self) -> tuple[Any, ...]:
        return tuple(step for _action, step in self.steps if step is not None)


def prepare(
    registry: PrivacyRegistry,
    orm_registry: Any,
    subject_id: str,
    *,
    plan: ErasurePlan,
    digest: str | None = None,
    limit: int = 500,
    within: str = "2s",
    schema: str = "wreath",
    workload: str = "write",
    record_retain: float | None = None,
) -> PreparedErasure:
    """Resolve a reviewed plan into passes, refusing anything unreviewed.

    Args:
        registry: the declarations.
        orm_registry: the compiled ORM registry the plan was derived from.
        subject_id: the subject's identity value.
        plan: the plan as printed.
        digest: the digest the operator read. `None` skips the comparison and
            is for a caller that just built the plan itself; the CLI always
            passes one.
        schema: where the pass ledger lives, and where the erasure record
            lives with it. Must match the job runner's, for the same reason
            `wreath.passes` says so: the ledger row and the walked table commit
            together, and a pass writing its cursor to a ledger nobody else
            reads resumes nowhere.
        workload: the connection pool the walk uses.
        record_retain: how long an erasure record is kept, in seconds, or
            `None` for kept-and-reported. `wreath._privacy.record` has the
            argument for why there is no default.

    Raises:
        PlanMoved: the recomputed digest differs from `digest`.
        ErasureBlocked: the plan reports unreachable data or a blocking cycle.
    """
    if digest is not None and digest != plan.digest:
        raise PlanMoved(
            f"the plan changed since it was printed: reviewed {digest}, now "
            f"{plan.digest}. Re-run `wreath privacy plan` and read it again -- a "
            "digest moves when a model, a classification or the reachable set has "
            "changed, and the review no longer covers what would run"
        )
    if plan.blocked:
        raise ErasureBlocked(
            "this plan would leave the subject's personal data behind: "
            f"{len(plan.unreachable)} unreachable classified table(s), "
            f"{sum(1 for cycle in plan.cycles if not cycle.deferrable)} blocking "
            f"foreign-key cycle(s), {len(plan.surviving_references)} surviving "
            "reference(s). Erasing now would report success for an incomplete "
            "erasure"
        )
    graph = build_graph(orm_registry)
    by_table = {
        (graph.nodes[model].schema, graph.nodes[model].table): model for model in graph.nodes
    }
    steps: list[tuple[TableAction, Any]] = []
    for action in plan.tables:
        model = by_table.get((action.schema, action.table))
        steps.append(
            (
                action,
                _pass_for(
                    action,
                    model,
                    graph,
                    registry,
                    subject_id,
                    limit=limit,
                    within=within,
                    schema=schema,
                    workload=workload,
                ),
            )
        )
    return PreparedErasure(
        plan=plan,
        steps=tuple(steps),
        record=ErasureRecord(schema=schema, retain=record_retain),
        schema=schema,
        workload=workload,
    )


#: The pass ledger columns the recorder reads. Named here rather than derived
#: from a `PassStatus`, because `wreath.passes.read_status` opens a connection
#: of its own and this read has to happen *inside* the record's transaction --
#: a completion established on another connection is a completion that could
#: have changed by the time the record commits.
_LEDGER_COLUMNS = "name, phase, rows_done"


async def record_erasure(prepared: PreparedErasure, database: Any) -> bool:
    """Write this erasure's record, having proved the erasure finished.

    One transaction: read the pass ledger, require every declared pass to be
    `done`, then append the record. The read and the write share a transaction
    on purpose -- a completion read on a different connection is a completion
    that another worker could have undone before this row landed.

    Returns:
        Whether a row was written. `False` means this erasure was already
        recorded, which a redelivered job produces and which is not an error.

    Raises:
        ErasureIncomplete: a declared pass is missing from the ledger or has
            not reached `done`.
    """
    names = tuple(walk.name for walk in prepared.passes)
    connection = await database.acquire(prepared.workload)
    try:
        async with connection.transaction() as tx:
            rows = await _ledger_rows(tx, prepared.schema, names)
            _require_complete(prepared.plan, names, rows)
            return await prepared.record.write(
                tx,
                plan=prepared.plan,
                tables_touched=len(names),
                rows_affected=sum(int(row[2]) for row in rows.values()),
            )
    finally:
        await database.release(prepared.workload, connection)


async def _ledger_rows(tx: Any, schema: str, names: tuple[str, ...]) -> dict[str, tuple[Any, ...]]:
    """This erasure's ledger rows by pass name, read in the caller's transaction.

    An empty `names` is answered without a query rather than with a `WHERE name
    IN ()`, which PostgreSQL parses and which no test would distinguish from
    the general case: an erasure with nothing to do has no ledger rows to find.
    """
    if not names:
        return {}
    from .._passes.ledger import table_name

    placeholders = ", ".join(f"${index}" for index in range(1, len(names) + 1))
    records = await tx.fetch(
        f"SELECT {_LEDGER_COLUMNS} FROM {table_name(schema)} WHERE name IN ({placeholders})",
        *names,
    )
    rows: dict[str, tuple[Any, ...]] = {}
    for record in records or ():
        row = tuple(record)
        rows[str(row[0])] = row
    return rows


def _require_complete(
    plan: ErasurePlan, names: tuple[str, ...], rows: dict[str, tuple[Any, ...]]
) -> None:
    """Refuse to record an erasure the ledger does not say finished."""
    unfinished = [name for name in names if name not in rows or str(rows[name][1]) != "done"]
    if not unfinished:
        return
    raise ErasureIncomplete(
        f"{len(unfinished)} of {len(names)} pass(es) for subject "
        f"{plan.subject_id} have not finished: {', '.join(sorted(unfinished))}. "
        "No erasure record is written, because a record is evidence the erasure "
        "was performed and this one was not. Drive the passes to completion and "
        "run the erasure again -- the walks are resumable, so nothing is redone"
    )


def _pass_for(
    action: TableAction,
    model: type | None,
    graph: Graph,
    registry: PrivacyRegistry,
    subject_id: str,
    *,
    limit: int,
    within: str,
    schema: str,
    workload: str,
) -> Any:
    """One table's pass, or None when nothing here writes.

    A `CASCADE` row is removed by the parent's own referential action and a
    `RETAIN` row is kept on purpose; neither wants a pass, and inventing one
    would issue a delete the plan did not promise.
    """
    if action.disposal in (Disposal.CASCADE.value, Disposal.RETAIN.value):
        return None
    if model is None:
        raise ErasureBlocked(
            f"{action.schema}.{action.table} is in the plan but not in the ORM "
            "registry handed to prepare(); the plan and the registry disagree"
        )
    from ..passes import Ceiling, ChunkedPass, DutyCycle, Purge, Rewrite, Rows, Sql

    where = predicate_for(action, graph, registry, subject_id)
    if action.disposal == Disposal.DELETE.value:
        work: Any = Purge(where=Sql(where.text, where.values))
    else:
        assignments, guard = _anonymise(action)
        if not assignments:
            return None
        # The guard is what makes the chunk re-runnable: a second run matches
        # no rows because every one of them has already been emptied. Without
        # it a retried chunk would rewrite rows it had already rewritten --
        # harmless here, but the pass promises idempotence and a shape that
        # relies on "harmless" stops being true the first time somebody adds a
        # trigger.
        text = f"({where.text}) AND ({guard})"
        work = Rewrite(set_=assignments, where=Sql(text, where.values))
    key = _key_expression(model)
    return ChunkedPass(
        f"privacy_erase_{action.table}_{subject_id}",
        over=model,
        units=Rows(key=key, limit=limit, within=within),
        # A fixed frontier, so the walk terminates. `wreath.passes.Ceiling`
        # states the precondition it comes with -- *a pass converts the past;
        # the application writes the future in the shape the pass is converting
        # to* -- and for an erasure that reads: the subject's account must
        # already be closed to new writes when this runs. An application still
        # inserting rows for an erased subject has a bug this cannot paper over,
        # and a recurring frontier would hide it by sweeping forever.
        # `at_launch()` refuses a key it cannot prove is assigned in increasing
        # order, which is the right refusal: a row landing behind the cursor is
        # a row the erasure never sees, and silently missing rows is the whole
        # failure mode. A UUIDv7 or ULID key passes `monotone=` to say so.
        frontier=Ceiling.at_launch(),
        work=work,
        pace=DutyCycle(0.25),
        schema=schema,
        workload=workload,
    )


@dataclass(frozen=True, slots=True)
class _Predicate:
    text: str
    values: tuple[Any, ...]


def predicate_for(
    action: TableAction,
    graph: Graph,
    registry: PrivacyRegistry,
    subject_id: str,
) -> _Predicate:
    """The `WHERE` that selects this table's rows for one subject.

    A table that declares the subject column matches directly. Anything else
    matches through the foreign-key path the plan printed, as nested `IN`
    subqueries -- one per edge, innermost at the subject.

    Nested `IN` rather than a join because the fragment is spliced into a
    chunked walk that already has its own `WHERE` over the key range, and a
    join would change the shape of that statement. The nesting depth is the
    path length, which the planner minimised by walking breadth-first.

    **Parent tables are rendered by `wreath.passes` own resolver**, not by
    qualifying `schema.table` here. The two have to agree, and agreeing by
    coincidence is not agreeing: a model declared with a plain string schema
    reaches its table through `search_path` and a pass renders it unqualified,
    so a fully-qualified subquery beside an unqualified `FROM` would read one
    table and write another. Sharing the function makes that impossible rather
    than unlikely.
    """
    path = action.reach.path
    # `_declares_subject` is not conjoined with a `match_column` test: the
    # planner's `_match_column` returns the classification's own `subject=`
    # whenever one is declared, so a true answer here already implies a
    # non-empty match column. The second spelling was redundant, and two
    # spellings of one condition is how they drift apart later.
    if not path or _declares_subject(action, registry, graph):
        return _Predicate(f"{_identifier(action.match_column)} = ?", (subject_id,))
    inner = _select_keys(path, len(path) - 1, registry.subject_key, graph)
    return _Predicate(f"{_identifier(path[-1].from_column)} IN ({inner})", (subject_id,))


def _select_keys(path: tuple[Any, ...], index: int, subject_column: str, graph: Graph) -> str:
    """The values `path[index].from_column` must match, as a subquery.

    `path[index].to_table` is the parent of the table at `index`, and -- for
    every index above zero -- it is also the table at `index - 1`. That
    identity is what makes the recursion one line: each level selects its own
    referenced column from its parent, restricted by the level beneath it,
    and level zero restricts by the subject itself.
    """
    edge = path[index]
    parent_table = _table_sql(edge.to_table, graph)
    referenced = _identifier(edge.to_column)
    if index == 0:
        return f"SELECT {referenced} FROM {parent_table} WHERE {_identifier(subject_column)} = ?"
    above = path[index - 1]
    return (
        f"SELECT {referenced} FROM {parent_table} "
        f"WHERE {_identifier(above.from_column)} IN "
        f"({_select_keys(path, index - 1, subject_column, graph)})"
    )


def _table_sql(qualified: str, graph: Graph) -> str:
    """One table as `wreath.passes` will write it, resolved from the model.

    Every path this is called with was built out of `graph`, so the lookup
    always finds a model. The refusal is here rather than a fallback that
    qualifies the name itself: a hand-built path naming a table no model owns
    would get a *plausible* `"schema"."table"` that `wreath.passes` renders
    differently, and a subquery that reads one table beside a walk that writes
    another is the worst possible way to be wrong here. Unreachable by
    construction, and loud if construction ever changes.
    """
    from ..passes import _resolve_source

    for model, node in graph.nodes.items():
        if node.qualified == qualified:
            return _resolve_source(model)[1]
    raise ValueError(
        f"{qualified} is in this erasure path and no model in the graph owns it, "
        "so the subquery cannot be rendered the way the walk will render it"
    )


def _declares_subject(action: TableAction, registry: PrivacyRegistry, graph: Graph) -> bool:
    for model, item in registry.classifications.items():
        node = graph.nodes.get(model)
        if node is None:
            continue
        if (node.schema, node.table) == (action.schema, action.table):
            return bool(item.subject)
    return False


def _anonymise(action: TableAction) -> tuple[dict[str, str], str]:
    """`(column -> SQL expression, the not-yet-done guard)`.

    `RETAIN` columns are skipped and pseudonymised ones are not written by this
    module at all: replacing a value with a stable token needs a token source
    the application owns, so the plan names them and the application supplies
    the rewrite. Silently hashing here would be the module inventing the one
    transform it refuses to call erasure.
    """
    assignments: dict[str, str] = {}
    guards: list[str] = []
    for column in action.columns:
        name = _identifier(column.column)
        if column.erase == Erase.NULL.value:
            assignments[column.column] = "NULL"
            guards.append(f"{name} IS NOT NULL")
        elif column.erase == Erase.REDACT.value:
            assignments[column.column] = f"'{REDACTED}'"
            guards.append(f"{name} IS DISTINCT FROM '{REDACTED}'")
    # No "did we collect any guards?" early return: every branch above appends
    # to both lists, so `guards` is empty exactly when `assignments` is, and
    # `_pass_for` already refuses to build a pass for empty assignments. A
    # mutation run found the early return changed no outcome.
    return assignments, " OR ".join(guards)


def primary_key(model: Any) -> tuple[Any, ...]:
    """The model's primary-key columns, as the ORM recorded them.

    Annotated `Any` rather than `type` deliberately: the attribute is a
    `ClassVar` on `wreath.orm.Model`, and a `type`-annotated parameter would
    force either a `getattr` the linter rewrites or an inline suppression the
    repository forbids. One narrow helper is the honest way to say "this is a
    model, not any class".

    No emptiness guard, either: `wreath.orm.Model` refuses a mapped model with
    no primary-key column at class creation, and every model reaching here came
    out of a compiled registry. A second check would be a clause the guard
    above it already subsumes.
    """
    return model.__wreath_primary_key__


def _key_expression(model: Any) -> Any:
    """The model's primary key as the pass's ordering key."""
    primary = primary_key(model)
    if len(primary) == 1:
        return getattr(model, primary[0].python_name)
    return tuple(getattr(model, column.python_name) for column in primary)


def _identifier(name: str) -> str:
    """A column name, refused unless it is a plain identifier.

    Column names reach the fragment by interpolation because an identifier
    cannot be a bind parameter. Every one of them came from a model
    declaration rather than from a request, so this is a belt on top of
    braces -- but the statement text is assembled here, so the check belongs
    here too.
    """
    validate_identifier(name, "quoted column")
    return f'"{name}"'
