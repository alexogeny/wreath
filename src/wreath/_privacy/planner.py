"""Turn declarations plus a foreign-key graph into a plan a person can check.

The plan is the product. Executing it is comparatively easy; knowing what the
execution *will not reach* is the hard part, and it is the part that fails in
the field -- the EDPB's February 2026 coordinated enforcement report on the
right to erasure found late and incomplete responses still widespread, and an
incomplete response is almost never a deliberate one. It is a table nobody
remembered.

So this module is built to produce findings, not just actions. Four of them,
each corresponding to a way an erasure silently misses data:

* **Unreachable.** A model with declared personal columns that no foreign-key
  path connects to the subject. The erasure would run, report success, and
  leave the rows.
* **Orphan risk.** A `SET NULL` or `SET DEFAULT` edge. Delete the parent and
  the child survives holding personal data with the only column that pointed
  at the subject now null -- unreachable *forever*, and created by the erasure
  itself.
* **Cycle.** Foreign keys that form a loop admit no ordering of plain deletes.
  Either the constraints are deferrable and one transaction can carry it, or a
  human breaks the loop. Guessing is how an erasure half-runs.
* **Retained.** Rows that survive under a written exemption. Not a defect --
  an audit trail that erased the record of its own erasure would be a
  compliance failure in the other direction -- but a reader is entitled to see
  exactly which personal data an erasure leaves behind, and why.

A plan with any unreachable table or any non-deferrable cycle reports
`blocked`, and `wreath privacy erase` refuses to run it.
"""

from __future__ import annotations

from typing import Any

from .graph import Graph, build_graph, order_children_first
from .model import (
    ColumnAction,
    CycleFinding,
    Disposal,
    ErasurePlan,
    ExportPlan,
    OrphanRisk,
    Pseudonymise,
    Reach,
    Retained,
    SurvivingReference,
    TableAction,
    Unreachable,
)
from .registry import PrivacyRegistry

__all__ = ["build_export_plan", "build_plan"]

#: Referential actions that leave a child row alive with a nulled or defaulted
#: foreign key when its parent is deleted.
_ORPHANING = frozenset({"n", "d"})

#: Referential actions that refuse the parent's delete outright.
#:
#: Ordering answers these only when the child rows are *also* deleted --
#: `order_children_first` puts them first and the reference is gone by the time
#: the parent goes. A child that survives the erasure keeps its foreign key, so
#: the same two codes become the finding `_surviving_references` reports. The
#: rule and its exception therefore live in one place rather than in a comment
#: claiming the topological sort covers both.
_BLOCKING = frozenset({"a", "r"})


def build_plan(
    registry: PrivacyRegistry, orm_registry: Any, subject_id: str
) -> ErasurePlan:
    """Derive what erasing one subject would do. Opens nothing, writes nothing.

    Raises:
        ValueError: when no subject model has been declared. Without one there
            is no root to walk from, and a plan over "every table with an
            `email` column" would be the heuristic this module refuses to be.
    """
    if registry.subject_model is None:
        raise ValueError(
            "no subject model declared; call privacy.subject(User, key='id') "
            "before planning an erasure"
        )
    graph = build_graph(orm_registry)
    root = registry.subject_model
    if root not in graph.nodes:
        raise ValueError(
            f"the subject model {getattr(root, '__name__', root)!r} is not compiled "
            "into this ORM registry, so it has no foreign-key graph"
        )
    reachable = graph.reachable_from(root)

    retained: list[Retained] = []
    actionable: dict[type, tuple[Reach, Any]] = {}
    for model, reach in reachable.items():
        item = registry.classifications.get(model)
        if model is root and item is None:
            # The subject's own row is always in scope even without a
            # `classify()` call: it is definitionally the subject's data.
            actionable[model] = (reach, None)
            continue
        if item is None:
            # Reached, but nothing about it is declared personal. Traversal
            # passes *through* it to its children; it is not itself acted on.
            continue
        if item.exempt is not None:
            node = graph.nodes[model]
            retained.append(
                Retained(
                    model=node.label,
                    schema=node.schema,
                    table=node.table,
                    reason=item.exempt,
                    reach=reach,
                )
            )
            continue
        actionable[model] = (reach, item)

    ordered, cycle_groups = order_children_first(graph, set(actionable))
    tables = _table_actions(graph, registry, actionable, ordered, root)
    cycles = _cycle_findings(graph, cycle_groups)
    orphans = _orphan_risks(graph, actionable, tables)
    surviving = _surviving_references(graph, tables)
    unreachable = _unreachable(graph, registry, reachable)
    return ErasurePlan(
        subject_model=graph.nodes[root].label,
        subject_column=registry.subject_key,
        subject_id=subject_id,
        tables=tuple(tables),
        retained=tuple(retained),
        unreachable=tuple(unreachable),
        orphan_risks=tuple(orphans),
        cycles=tuple(cycles),
        surviving_references=tuple(surviving),
        notes=_notes(tables, retained, unreachable, cycles, orphans, surviving),
    )


def build_export_plan(
    registry: PrivacyRegistry, orm_registry: Any, subject_id: str
) -> ExportPlan:
    """The same traversal in read mode, for a subject-access request.

    Derived from the erasure plan rather than from a second walk, because two
    walks would eventually disagree about which tables hold a subject's data
    and the disagreement would show up as a subject-access response that omits
    what the erasure deletes.
    """
    plan = build_plan(registry, orm_registry, subject_id)
    return ExportPlan(
        subject_model=plan.subject_model,
        subject_column=plan.subject_column,
        subject_id=plan.subject_id,
        # Exempt tables are *read* even though they are not erased: an
        # exemption from erasure is not an exemption from access.
        tables=plan.tables,
        withheld=plan.retained,
        unreachable=plan.unreachable,
    )


def _table_actions(
    graph: Graph,
    registry: PrivacyRegistry,
    actionable: dict[type, tuple[Reach, Any]],
    ordered: list[type],
    root: type,
) -> list[TableAction]:
    placed = list(ordered)
    # A model caught in a cycle has no position; it still belongs in the plan,
    # appended after everything orderable so a reader sees the whole scope and
    # the cycle finding explains why it cannot run yet.
    placed.extend(sorted(set(actionable) - set(ordered), key=lambda m: graph.nodes[m].qualified))
    actions: list[TableAction] = []
    for index, model in enumerate(placed):
        reach, item = actionable[model]
        node = graph.nodes[model]
        columns = _column_actions(item)
        disposal = _disposal(model, item, reach, registry, root)
        actions.append(
            TableAction(
                model=node.label,
                schema=node.schema,
                table=node.table,
                disposal=disposal.value,
                reach=reach,
                columns=tuple(columns),
                match_column=_match_column(reach, registry, root, model),
                reason=_reason(disposal, item, reach),
                order=index,
            )
        )
    return actions


def _column_actions(item: Any) -> list[ColumnAction]:
    if item is None:
        return []
    actions: list[ColumnAction] = []
    for column in sorted(item.personal):
        disposition = item.personal[column]
        if isinstance(disposition, Pseudonymise):
            actions.append(
                ColumnAction(
                    column=column,
                    erase="pseudonymise",
                    pseudonym_reason=disposition.text,
                    note="still personal data: the subject remains distinguishable",
                )
            )
            continue
        actions.append(ColumnAction(column=column, erase=str(disposition)))
    return actions


def _disposal(
    model: type, item: Any, reach: Reach, registry: PrivacyRegistry, root: type
) -> Disposal:
    if model is root:
        return Disposal.DELETE if registry.subject_delete else Disposal.ANONYMISE
    if item is not None and item.delete:
        return Disposal.DELETE
    if reach.path and reach.path[-1].on_delete == "c" and _root_deleted(registry):
        # The parent's own cascade will remove these rows. Reported as its own
        # disposal rather than folded into DELETE, because a reader needs to
        # know the database does it and this plan does not.
        return Disposal.CASCADE
    return Disposal.ANONYMISE


def _root_deleted(registry: PrivacyRegistry) -> bool:
    return registry.subject_delete


def _match_column(
    reach: Reach, registry: PrivacyRegistry, root: type, model: type
) -> str:
    if model is root:
        return registry.subject_key
    classification = registry.classifications.get(model)
    if classification is not None and classification.subject:
        return classification.subject
    if reach.path:
        return reach.path[-1].from_column
    return ""


def _reason(disposal: Disposal, item: Any, reach: Reach) -> str:
    if disposal is Disposal.CASCADE:
        return "removed by the parent's ON DELETE CASCADE"
    if disposal is Disposal.DELETE:
        return "the row exists only as the subject's data"
    if item is not None and not item.personal:
        return "reached, with no personal columns declared"
    return f"reached {reach.explain()}"


def _cycle_findings(graph: Graph, groups: list[tuple[type, ...]]) -> list[CycleFinding]:
    findings: list[CycleFinding] = []
    for group in groups:
        members = set(group)
        edges = [
            edge
            for model in group
            for edge, parent in graph.outbound.get(model, ())
            if parent in members
        ]
        # `bool(edges)` is not redundant beside `all(...)`: `all(())` is True,
        # so a component whose members reference each other only through edges
        # this graph does not model would be reported as *deferrable* and let
        # the erasure run. No schema this planner can build produces one -- a
        # component exists because of the edges -- so the clause is a floor
        # under a claim rather than a branch a test can reach.
        deferrable = bool(edges) and all(edge.deferrable for edge in edges)
        names = tuple(graph.nodes[model].qualified for model in group)
        findings.append(
            CycleFinding(
                tables=names,
                deferrable=deferrable,
                detail=(
                    "every foreign key in the loop is DEFERRABLE, so one "
                    "transaction can carry the deletes"
                    if deferrable
                    else "no ordering of plain deletes exists; make one of the "
                    "foreign keys DEFERRABLE, or null an edge before deleting"
                ),
            )
        )
    return findings


def _orphan_risks(
    graph: Graph, actionable: dict[type, tuple[Reach, Any]], tables: list[TableAction]
) -> list[OrphanRisk]:
    """Edges that would strand a child row holding personal data.

    Only reported for a parent this plan actually deletes. A `SET NULL` edge
    onto a table nothing deletes is an ordinary schema choice, and reporting it
    would bury the real finding in noise.
    """
    deleting = {
        (action.schema, action.table)
        for action in tables
        if action.disposal in (Disposal.DELETE.value, Disposal.CASCADE.value)
    }
    risks: list[OrphanRisk] = []
    for model in sorted(actionable, key=lambda m: graph.nodes[m].qualified):
        for edge, parent in graph.outbound.get(model, ()):
            if edge.on_delete not in _ORPHANING:
                continue
            node = graph.nodes[parent]
            if (node.schema, node.table) not in deleting:
                continue
            risks.append(
                OrphanRisk(
                    edge=edge,
                    detail=(
                        f"deleting {edge.to_table} sets {edge.from_table}."
                        f"{edge.from_column} to "
                        f"{'NULL' if edge.on_delete == 'n' else 'its default'}; the "
                        "child row survives holding the subject's data and can never "
                        "be found again. This plan orders the child first"
                    ),
                )
            )
    return risks


def _surviving_references(
    graph: Graph, tables: list[TableAction]
) -> list[SurvivingReference]:
    """`NO ACTION`/`RESTRICT` edges from a surviving row to a deleted one.

    Every model in the graph is considered, not only the classified ones: the
    row that refuses the delete is whichever row still points at the parent,
    and nothing about holding a foreign key requires being personal data.
    """
    removed = {
        (action.schema, action.table)
        for action in tables
        if action.disposal in (Disposal.DELETE.value, Disposal.CASCADE.value)
    }
    # No "is anything being removed?" early return: with `removed` empty every
    # parent fails the membership test below and the loop yields nothing
    # anyway. A mutation run found the guard changed no outcome, and two
    # spellings of one condition is how they drift apart later.
    findings: list[SurvivingReference] = []
    for model in sorted(graph.nodes, key=lambda m: graph.nodes[m].qualified):
        node = graph.nodes[model]
        if (node.schema, node.table) in removed:
            continue
        for edge, parent in graph.outbound.get(model, ()):
            parent_node = graph.nodes[parent]
            if (parent_node.schema, parent_node.table) not in removed:
                continue
            if edge.on_delete not in _BLOCKING:
                continue
            findings.append(
                SurvivingReference(
                    edge=edge,
                    detail=(
                        f"{edge.from_table} rows survive this erasure and still "
                        f"reference {edge.to_table}, whose rows it deletes. The "
                        "foreign key is ON DELETE NO ACTION or RESTRICT, so the "
                        "database refuses the delete and the erasure stops "
                        "half-way. Delete these rows too, make the edge ON "
                        "DELETE CASCADE or SET NULL, or stop deleting the parent"
                    ),
                )
            )
    return findings


def _unreachable(
    graph: Graph, registry: PrivacyRegistry, reachable: dict[type, Reach]
) -> list[Unreachable]:
    """Classified models the traversal never arrived at.

    The finding. A model can be unmapped in this registry, or mapped and simply
    not connected to the subject by any foreign key -- and the two need
    different fixes, so they are reported with different reasons rather than
    one shrug.
    """
    found: list[Unreachable] = []
    for model, item in registry.classifications.items():
        if model in reachable or not item.personal:
            continue
        node = graph.nodes.get(model)
        columns = tuple(sorted(item.personal))
        if node is None:
            found.append(
                Unreachable(
                    model=getattr(model, "__name__", str(model)),
                    schema="",
                    table="",
                    columns=columns,
                    reason=(
                        "not compiled into this ORM registry, so the plan cannot see "
                        "its table or its foreign keys"
                    ),
                )
            )
            continue
        found.append(
            Unreachable(
                model=node.label,
                schema=node.schema,
                table=node.table,
                columns=columns,
                reason=(
                    "no foreign-key path from the subject reaches this table. Declare "
                    "subject= on the column that identifies the subject, add the "
                    "missing foreign key, or record that these rows are not the "
                    "subject's"
                ),
            )
        )
    return sorted(found, key=lambda item: (item.schema, item.table, item.model))


def _notes(
    tables: list[TableAction],
    retained: list[Retained],
    unreachable: list[Unreachable],
    cycles: list[CycleFinding],
    orphans: list[OrphanRisk],
    surviving: list[SurvivingReference],
) -> tuple[str, ...]:
    """The sentences a reader needs that no single row of the plan carries."""
    notes: list[str] = [
        "Backups are out of scope. This plan covers live tables only; a restore "
        "from a backup taken before the erasure reinstates the data, which is why "
        "the erasure record is retained so a restore can replay it.",
    ]
    pseudonymised = [
        (action.table, column.column)
        for action in tables
        for column in action.columns
        if column.erase == "pseudonymise"
    ]
    if pseudonymised:
        listed = ", ".join(f"{table}.{column}" for table, column in pseudonymised)
        notes.append(
            f"Pseudonymised, not erased: {listed}. These remain personal data and "
            "the subject is still distinguishable in them."
        )
    if retained:
        notes.append(
            f"{len(retained)} table(s) retain the subject's data under a written "
            "exemption; each reason is printed above."
        )
    if unreachable:
        notes.append(
            f"BLOCKED: {len(unreachable)} classified table(s) hold personal data this "
            "traversal cannot reach. Erasure refuses to run until each is resolved."
        )
    if any(not cycle.deferrable for cycle in cycles):
        notes.append(
            "BLOCKED: a foreign-key cycle admits no ordering of plain deletes."
        )
    if surviving:
        notes.append(
            f"BLOCKED: {len(surviving)} foreign key(s) point at rows this plan "
            "deletes from rows it keeps. The database would refuse the delete and "
            "the erasure would stop half-way."
        )
    if orphans:
        notes.append(
            f"{len(orphans)} edge(s) would orphan personal data if the parent went "
            "first; the plan orders the child before the parent."
        )
    return tuple(notes)
