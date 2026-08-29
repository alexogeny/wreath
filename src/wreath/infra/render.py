"""Turn an `InfrastructurePlan` into something to read, or something to parse.

The plan is the artefact; these are two views of it. `render_text` is written to
be pasted into a review or a ticket -- it names every requirement and, where a
requirement is absent, says so out loud rather than leaving a silence for the
reader to interpret. `render_json` is the same content with no opinions, so a
pipeline can diff two plans or feed one to something else.
"""

from __future__ import annotations

import json
import textwrap

from .model import GapKind, InfrastructurePlan, Presence, as_dict

__all__ = ["render_json", "render_text"]

_NOTHING = "-- nothing supplies this"


def render_json(plan: InfrastructurePlan, *, indent: int = 2) -> str:
    """The plan as JSON, keys in declaration order, one trailing newline."""
    return json.dumps(as_dict(plan), indent=indent) + "\n"


def _rule(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(title)
    lines.append("-" * len(title))


def _pairs(lines: list[str], rows: list[tuple[str, str]], *, indent: str = "    ") -> None:
    """Two columns, the first padded. Every call site builds at least one row."""
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        lines.append(f"{indent}{label.ljust(width)}  {value}")


def _databases(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, f"PostgreSQL ({len(plan.databases)})")
    if not plan.databases:
        lines.append("  none: this application registers no database.")
        return
    for database in plan.databases:
        owner = f" as {database.user}" if database.user else ""
        lines.append(f"  {database.name}  {database.endpoint}/{database.database}{owner}")
        rows: list[tuple[str, str]] = []
        for pool in database.pools:
            where = "" if pool.endpoint == database.endpoint else f" -> {pool.endpoint}"
            rows.append((f"pool {pool.workload}", f"{pool.min_size}..{pool.max_size}{where}"))
        for budget in database.budgets:
            if not budget.held:
                continue
            rows.append(
                (
                    f"held {budget.workload}",
                    f"{budget.held} of {budget.pool_max} for the life of the process "
                    f"({', '.join(budget.holders)}); {budget.available} left for requests",
                )
            )
        rows.append(("schemas", ", ".join(database.schemas) or "public"))
        rows.append(("extensions", ", ".join(database.extensions) or "none"))
        rows.append(
            (
                "application tables",
                f"{database.models} ORM model(s); their DDL comes from wreath migrations",
            )
        )
        if database.components:
            for index, component in enumerate(database.components):
                relations = ", ".join(f"{component.schema}.{r}" for r in component.relations)
                rows.append(
                    (
                        "wreath tables" if index == 0 else "",
                        f"{relations}  ({component.name}, from {component.declared_by})",
                    )
                )
        else:
            rows.append(("wreath tables", "none: no registered subsystem owns tables here"))
        _pairs(lines, rows)


def _object_stores(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, f"Object storage ({len(plan.object_stores)})")
    if not plan.object_stores:
        lines.append("  none: this application registers no object store.")
        return
    for store in plan.object_stores:
        if store.backend == "local":
            lines.append(f"  {store.name}  local disk, root {store.root}")
        else:
            style = "path-style" if store.path_style else "virtual-hosted"
            lines.append(
                f"  {store.name}  s3 bucket {store.bucket} in {store.region}, "
                f"{style} at {store.host}"
            )
            lines.append(f"      credentials from {store.credentials}")
        for requirement in store.requires:
            lines.append(f"      requires  {requirement}")


def _egress(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, f"Egress ({len(plan.egress)})")
    if not plan.egress:
        lines.append("  none: this application pins no outbound HTTP client.")
        lines.append("  A ServiceClient built over a client the application did not register is")
        lines.append("  invisible here; see the notes.")
        return
    for rule in plan.egress:
        lines.append(f"  {rule.name}  {rule.origin}{rule.base_path}")
        rows = [
            ("declared by", rule.declared_by),
            ("max sockets", str(rule.max_connections)),
            ("destination", rule.destination),
        ]
        _pairs(lines, rows, indent="      ")


def _listeners(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, "Listener")
    for listener in plan.listeners:
        methods = ", ".join(listener.methods) or "none"
        lines.append(
            f"  {listener.protocol}  {listener.routes} route(s), "
            f"{listener.websocket_routes} websocket route(s)"
        )
        rows = [("methods", methods)]
        if listener.mounts:
            rows.append(("mounts", ", ".join(listener.mounts)))
        _pairs(lines, rows, indent="      ")
    lines.append("      the port, the TLS termination and the load balancer are")
    lines.append("      deployment decisions; the application does not declare them")


def _columns(lines: list[str], rows: list[tuple[str, ...]], *, indent: str = "  ") -> None:
    """Left-align every column but the last, which runs to the end of the line.

    `rows` always carries its header, so there is nothing to guard against here.
    """
    widths = [
        # complexity: allow SL-COMP-LOOP -- widths visit every input table cell
        max(len(row[index]) for row in rows)
        for index in range(len(rows[0]) - 1)
    ]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths, strict=True)]
        lines.append(indent + "  ".join([*cells, row[-1]]).rstrip())


def _subsystems(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, "What would be a separate service somewhere else")
    lines.append("  Every row is PostgreSQL, the local disk, or this process. There is no")
    lines.append("  broker, no cache server and no second datastore anywhere in this plan,")
    lines.append("  and that is a property of wreath rather than of this application.")
    lines.append("")
    rows: list[tuple[str, ...]] = [("", "subsystem", "lives in", "instead of")]
    for row in plan.subsystems:
        rows.append((row.presence.value, row.name, row.backing, ", ".join(row.instead_of)))
    _columns(lines, rows)
    lines.append("")
    for row in plan.subsystems:
        if row.presence is not Presence.ABSENT:
            lines.append(f"  {row.name}: {row.detail}")


def _settings(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, "Settings contract")
    if not plan.settings:
        lines.append("  not checked: no settings model was named.")
        return
    for contract in plan.settings:
        prefix = contract.prefix or "(no prefix)"
        lines.append(f"  {contract.model}, prefix {prefix}")
        lines.append("  keys as wreath.config.Environment.bind resolves them, in declaration order")
        rows: list[tuple[str, ...]] = [("key", "field", "type", "supplied by")]
        rows.extend(
            (key.key, key.field, key.annotation, key.supplied_by or _NOTHING)
            for key in contract.keys
        )
        _columns(lines, rows, indent="    ")
        for key in contract.unread:
            lines.append(f"    {key}  supplied, and read by no field")


def _gaps(plan: InfrastructurePlan, lines: list[str]) -> None:
    _rule(lines, f"Gaps ({len(plan.gaps)})")
    if not plan.gaps:
        lines.append("  none.")
        return
    for gap in plan.gaps:
        label = "missing" if gap.kind is GapKind.SETTINGS_KEY else gap.kind.value
        lines.append(f"  [{label}] {gap.subject}")
        lines.extend(
            textwrap.wrap(gap.detail, width=76, initial_indent="      ", subsequent_indent="      ")
        )


def _notes(plan: InfrastructurePlan, lines: list[str]) -> None:
    if not plan.notes:
        return
    _rule(lines, "Notes")
    for note in plan.notes:
        wrapped = textwrap.wrap(note, width=76, initial_indent="  - ", subsequent_indent="    ")
        lines.extend(wrapped)


def render_text(plan: InfrastructurePlan) -> str:
    """The plan as a report to read, ending in one newline.

    Absence is always stated. A section with nothing in it prints a sentence
    saying so, because a plan that silently omits object storage and a plan that
    found none look identical otherwise, and only one of them is an answer.
    """
    title = f"Infrastructure inferred from {plan.application}"
    lines = [title, "=" * len(title)]
    _databases(plan, lines)
    _object_stores(plan, lines)
    _egress(plan, lines)
    _listeners(plan, lines)
    _subsystems(plan, lines)
    _settings(plan, lines)
    _gaps(plan, lines)
    _notes(plan, lines)
    return "\n".join(lines) + "\n"
