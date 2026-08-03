"""Turn an erasure plan into something a person reads before it runs.

The plan is the artefact; these are two views of it. `render_text` is written
to be pasted into a ticket or a review, and its ordering is the running order,
so a reviewer reads the erasure in the sequence it will happen. `render_json`
is the same content with no opinions, for a pipeline that diffs two plans.

One rule shapes both: **absence is stated, never implied.** A plan that reaches
no unreachable tables says so; a plan that retains nothing says so. A silence
here would read as "nothing to see", and the whole point of this command is
that the dangerous case is the one nobody looked for.
"""

from __future__ import annotations

import json

from .model import Disposal, ErasurePlan, ExportPlan, as_dict

__all__ = ["render_json", "render_text"]


def render_json(plan: ErasurePlan | ExportPlan, *, indent: int = 2) -> str:
    """The plan as JSON, one trailing newline."""
    return json.dumps(as_dict(plan), indent=indent, default=str) + "\n"


def _rule(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(title)
    lines.append("-" * len(title))


def render_text(plan: ErasurePlan) -> str:
    """The plan as text, in the order the erasure would run."""
    lines: list[str] = [
        f"erasure plan for {plan.subject_model}.{plan.subject_column} = {plan.subject_id}",
        f"digest {plan.digest}",
    ]
    _actions(plan, lines)
    _retained(plan, lines)
    _findings(plan, lines)
    _notes(plan, lines)
    _verdict(plan, lines)
    return "\n".join(lines) + "\n"


def _actions(plan: ErasurePlan, lines: list[str]) -> None:
    _rule(lines, f"Actions ({len(plan.tables)}), in running order")
    if not plan.tables:
        lines.append("  none: nothing reachable from this subject is classified.")
        return
    for action in plan.tables:
        match = f" where {action.match_column} = <subject>" if action.match_column else ""
        lines.append(
            f"  {action.order + 1:>3}. {action.schema}.{action.table}"
            f"  [{action.disposal}]{match}"
        )
        lines.append(f"       reached: {action.reach.explain()}")
        if action.reason:
            lines.append(f"       why:     {action.reason}")
        for column in action.columns:
            suffix = "" if column.irreversible else "   <- NOT erasure"
            lines.append(f"       - {column.column}: {column.erase}{suffix}")
            if column.pseudonym_reason:
                lines.append(f"         declared: {column.pseudonym_reason}")


def _retained(plan: ErasurePlan, lines: list[str]) -> None:
    _rule(lines, f"Retained under exemption ({len(plan.retained)})")
    if not plan.retained:
        lines.append("  none: no table claims an exemption from this erasure.")
        return
    for item in plan.retained:
        lines.append(f"  {item.schema}.{item.table}")
        lines.append(f"       reason:  {item.reason}")
        lines.append(f"       reached: {item.reach.explain()}")


def _findings(plan: ErasurePlan, lines: list[str]) -> None:
    _rule(lines, f"Unreachable classified data ({len(plan.unreachable)})")
    if not plan.unreachable:
        lines.append("  none: every classified table is reachable from the subject.")
    for item in plan.unreachable:
        where = f"{item.schema}.{item.table}" if item.table else item.model
        lines.append(f"  {where}  ({', '.join(item.columns)})")
        lines.append(f"       {item.reason}")

    _rule(lines, f"Foreign-key cycles ({len(plan.cycles)})")
    if not plan.cycles:
        lines.append("  none: the reachable tables can be ordered children-first.")
    for cycle in plan.cycles:
        state = "deferrable" if cycle.deferrable else "BLOCKING"
        lines.append(f"  [{state}] {' <-> '.join(cycle.tables)}")
        lines.append(f"       {cycle.detail}")

    _rule(lines, f"Orphan risks ({len(plan.orphan_risks)})")
    if not plan.orphan_risks:
        lines.append("  none: no SET NULL or SET DEFAULT edge points at a deleted row.")
    for risk in plan.orphan_risks:
        lines.append(f"  {risk.edge.explain()}  ON DELETE {_action(risk.edge.on_delete)}")
        lines.append(f"       {risk.detail}")

    _rule(lines, f"Surviving references ({len(plan.surviving_references)})")
    if not plan.surviving_references:
        lines.append("  none: nothing this erasure keeps still points at what it deletes.")
    for reference in plan.surviving_references:
        lines.append(
            f"  {reference.edge.explain()}  ON DELETE "
            f"{_action(reference.edge.on_delete)}"
        )
        lines.append(f"       {reference.detail}")


def _notes(plan: ErasurePlan, lines: list[str]) -> None:
    if not plan.notes:
        return
    _rule(lines, "Notes")
    for note in plan.notes:
        lines.append(f"  * {note}")


def _verdict(plan: ErasurePlan, lines: list[str]) -> None:
    lines.append("")
    if plan.blocked:
        lines.append(
            "BLOCKED. This plan would leave the subject's personal data behind. "
            "`wreath privacy erase` refuses to run it until the findings above "
            "are resolved."
        )
        return
    lines.append(
        "Ready. Quote this digest when you run it, from the application that owns "
        "the database:\n"
        f'    await privacy.erase(database, "{plan.subject_id}", '
        f'digest="{plan.digest}")'
    )


_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _action(code: str) -> str:
    return _ACTIONS.get(code, code)


def render_export_text(plan: ExportPlan) -> str:
    """The read-mode traversal, for a subject-access request."""
    lines = [
        f"access request for {plan.subject_model}.{plan.subject_column} = {plan.subject_id}",
    ]
    _rule(lines, f"Tables to export ({len(plan.tables)})")
    if not plan.tables:
        lines.append("  none.")
    for action in plan.tables:
        if action.disposal == Disposal.CASCADE.value and not action.columns:
            continue
        lines.append(f"  {action.schema}.{action.table}  ({action.reach.explain()})")
    _rule(lines, f"Withheld ({len(plan.withheld)})")
    if not plan.withheld:
        lines.append("  none.")
    for item in plan.withheld:
        lines.append(f"  {item.schema}.{item.table}: {item.reason}")
    _rule(lines, f"Unreachable ({len(plan.unreachable)})")
    if not plan.unreachable:
        lines.append("  none.")
    for item in plan.unreachable:
        lines.append(f"  {item.model}: {item.reason}")
    return "\n".join(lines) + "\n"
