"""Rendering a run so the findings are the thing you read first.

The report ends in an action rather than a percentage. A mutation ratio is over
the mutations someone thought to write, and chasing it produces tests shaped
like the operator library. What is worth reading is the list of controls whose
removal nothing noticed, and -- separately, because it is a different problem
with a different fix -- the list of controls no test reaches at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .model import Outcome, Report, Verdict, rate_counts

_HEADINGS = {
    Outcome.SURVIVED: "SURVIVED -- the control was removed and every test still passed",
    Outcome.UNREACHED: "UNREACHED -- no test executes this control at all",
    Outcome.TIMEOUT: "UNDECIDED -- the run did not finish in time",
    Outcome.ERROR: "DECLINED -- this tool could not build or run the mutant",
}


def _group(verdicts: Iterable[Verdict]) -> dict[str, list[Verdict]]:
    grouped: dict[str, list[Verdict]] = {}
    for verdict in verdicts:
        grouped.setdefault(verdict.mutation.site.path, []).append(verdict)
    return grouped


def render(report: Report, *, verbose: bool = False) -> str:
    lines: list[str] = []
    for outcome in (Outcome.SURVIVED, Outcome.UNREACHED, Outcome.TIMEOUT):
        found = report.by_outcome(outcome)
        if not found:
            continue
        lines.append(_HEADINGS[outcome])
        lines.append("")
        for path, verdicts in sorted(_group(found).items()):
            lines.append(f"  {path}")
            for verdict in sorted(verdicts, key=lambda v: v.mutation.site.line):
                mutation = verdict.mutation
                lines.append(
                    f"    :{mutation.site.line:<5} {mutation.control}"
                )
                detail = f"{mutation.operator} in {mutation.site.scope or '<module>'}"
                if outcome is Outcome.SURVIVED:
                    detail += f"; {len(verdict.candidates)} test(s) ran it and none objected"
                lines.append(f"            {detail}")
                # The id, so the reader can re-run exactly this one after
                # writing the test that ought to kill it.
                suffix = mutation.identifier.rpartition(":")[2]
                if "#" in suffix:
                    lines.append(f"            --only '{mutation.identifier}'")
            lines.append("")

    if verbose:
        killed = report.by_outcome(Outcome.KILLED)
        if killed:
            lines.append("KILLED -- the suite noticed")
            lines.append("")
            for verdict in killed:
                mutation = verdict.mutation
                first = verdict.killers[0] if verdict.killers else verdict.note
                lines.append(f"  {mutation.site}  {mutation.control}")
                lines.append(f"            caught by {first}")
            lines.append("")

    declined = report.by_outcome(Outcome.ERROR)
    if declined:
        lines.append(f"{_HEADINGS[Outcome.ERROR]} ({len(declined)})")
        for verdict in declined[:12]:
            lines.append(f"  {verdict.mutation.identifier}: {verdict.note}")
        if len(declined) > 12:
            lines.append(f"  ... and {len(declined) - 12} more; --format json for all.")
        lines.append("")

    counts = {outcome: len(report.by_outcome(outcome)) for outcome in Outcome}
    summary = (
        f"wreath mutant: {counts[Outcome.KILLED]} killed, "
        f"{counts[Outcome.SURVIVED]} survived, "
        f"{counts[Outcome.UNREACHED]} unreached, "
        f"{counts[Outcome.EQUIVALENT]} provably equivalent, "
        f"{counts[Outcome.TIMEOUT]} undecided, "
        f"{counts[Outcome.ERROR]} declined"
    )
    lines.append(summary)
    rating = rate_counts({outcome.value: count for outcome, count in counts.items()})
    lines.append(
        f"                {rating.label}: {rating.action}; "
        f"{report.baseline_tests} test(s) in {report.total_seconds:.0f}s."
    )
    if report.baseline_failures:
        lines.append(
            f"                {len(report.baseline_failures)} test(s) were already failing "
            f"before any mutation; they can never kill anything and were excluded."
        )
    if rating.tone == "good":
        lines.insert(0, "Every control this run could remove was noticed by a test.\n")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2)
