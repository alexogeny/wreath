"""Thin presentation layer for `wreath port` (mirrors `_migrations.cli`).

All analysis lives in the `_port` core; this only parses the namespace, drives
`analyze_all`, and renders.

**Exit codes follow the same convention as the rest of the CLI**: `2` means the
command never got started, `1` means it ran and has something to report, `0`
means it ran clean. `wreath docs` uses exactly this split — `2` for an
unknown action or a config that would not load, `1` for a build that ran and
had errors.

===== ==========================================================================
 0     The analysis ran and left nothing for a human. Every recognized construct
       translates, and every file was read. **An app that has already been
       ported lands here**: files analyzed, nothing recognized, nothing skipped
       is a clean run, so a regression-check re-run stays green.
 1     The analysis ran and there is work remaining: unsupported constructs,
       files that could not be read, or both. The report names which.
 2     The analysis never ran over anything -- no Python file was analyzed. In
       practice this is a wrong or empty directory. Unreadable source paths
       raise from here and reach the same code via `CliError` in `_cli`.
===== ==========================================================================

Skipped files fold into `1` rather than earning a code of their own. They do
change what the numbers mean -- **an unsupported count taken over a partial tree
is a lower bound rather than a count** -- and the report says so in the summary
line, the `skipped` section, and `files_analyzed`. But a third level would be
a scheme no other wreath command has, and the case that actually needs its own
code is "you pointed me at nothing", which `2` covers.

Emit mode (`--output`/`--in-place`) reads the same way: sources that could
not be read are work remaining (`1`), a tree with nothing to emit at all is
`2`, and everything written is `0`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TypedDict

from .analyzer import analyze_all, detect_roots
from .emit import port_tree
from .ir import TRANSLATED, Finding, Report

#: Ran clean: nothing unsupported, nothing skipped. Includes an already-ported
#: tree, which is a successful run that happens to have nothing left to do.
EXIT_OK = 0
#: Ran, and left work: unsupported constructs, unreadable files, or both.
EXIT_WORK_REMAINS = 1
#: Never ran over anything -- no Python file was analyzed. A wrong or empty
#: directory, and the same code `_cli` raises for a source path that is absent.
EXIT_NOT_RUN = 2


def _resolve(report: Report, finding: Finding) -> Path | None:
    """The real path behind `finding.file`, which is spelled relative to a root.

    With one root that is `root / file`. With several it is ambiguous by
    construction -- two apps may both hold `models.py` -- so every root is
    tried and the first hit wins. Ambiguity here costs at most the wrong app's
    identically-named file in a context block; guessing wrong is visible, and the
    alternative (teaching `Finding` which root it came from) changes a
    serialized contract for a display convenience.
    """
    candidates = [Path(root) / finding.file for root in report.roots]
    candidates.append(Path(finding.file))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _context_lines(path: Path, line: int, before: int, after: int) -> list[str]:
    """`before`/`after` source lines around `line`, with the hit marked."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"      <could not read {path}: {exc}>"]
    low = max(0, line - 1 - before)
    high = min(len(source), line + after)
    out = []
    for index in range(low, high):
        marker = ">" if index + 1 == line else " "
        out.append(f"    {marker} {index + 1:>5} {source[index]}")
    return out


def render_by_rule(report: Report) -> str:
    """Cluster the non-translated findings by rule: what this codebase needs."""
    rows = report.rule_counts()
    lines = ["# wreath port — findings by rule", ""]
    if not rows:
        lines.append("_nothing needs review; every recognized construct translates_")
        return "\n".join(lines) + "\n"
    flagged = sum(n for *_, n in rows)
    lines += [
        f"{flagged} finding(s) needing a decision, in {len(rows)} rule(s).",
        "",
        "| count | rule | category | tag |",
        "| --- | --- | --- | --- |",
    ]
    for rule, category, tag, count in rows:
        lines.append(f"| {count} | `{rule}` | {category} | {tag} |")
    return "\n".join(lines) + "\n"


def render_sites(report: Report, rules: AbstractSet[str], context: int) -> str:
    """Every site for the named rules, with source context. Empty set means all."""
    selected = [
        f for f in report.findings if f.tag != TRANSLATED and (not rules or f.rule_id in rules)
    ]
    lines = ["# wreath port — finding sites", ""]
    if rules:
        unknown = sorted(rules - {f.rule_id for f in report.findings})
        for rule in unknown:
            lines.append(f"_no findings for rule `{rule}`_")
        if unknown:
            lines.append("")
    if not selected:
        lines.append("_no matching findings_")
        return "\n".join(lines) + "\n"

    for finding in selected:
        lines.append(f"`{finding.tag}` **{finding.rule_id}** — {finding.file}:{finding.line}")
        if context:
            resolved = _resolve(report, finding)
            if resolved is None:
                lines.append(f"      <source not found for {finding.file}>")
            else:
                lines.extend(_context_lines(resolved, finding.line, context, context))
        lines.append(f"      {finding.message}")
        lines.append("")
    return "\n".join(lines) + "\n"


class _CedarOptions(TypedDict):
    required_condition: str | None
    default_condition: str | None
    action_conditions: dict[str, str]
    condition_map: dict[str, str]
    authentication_factories: list[str]


def _cedar_options(path: str | None) -> _CedarOptions | None:
    """Read the deliberately small, data-only port policy profile."""
    if path is None:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Cedar semantics must be a JSON object")
    allowed = {
        "required_condition",
        "default_condition",
        "action_conditions",
        "conditions",
        "authentication_factories",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ValueError(f"unknown Cedar semantics key(s): {', '.join(unknown)}")
    scalars: dict[str, str | None] = {}
    for key in ("required_condition", "default_condition"):
        value = document.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Cedar semantics {key} must be a string or null")
        scalars[key] = value
    mappings: dict[str, dict[str, str]] = {}
    for source, target in (
        ("action_conditions", "action_conditions"),
        ("conditions", "condition_map"),
    ):
        value = document.get(source, {})
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError(f"Cedar semantics {source} must map strings to strings")
        mappings[target] = value
    factories = document.get("authentication_factories", [])
    if not isinstance(factories, list) or not all(isinstance(item, str) for item in factories):
        raise ValueError("Cedar semantics authentication_factories must be a list of strings")
    return _CedarOptions(
        required_condition=scalars["required_condition"],
        default_condition=scalars["default_condition"],
        action_conditions=mappings["action_conditions"],
        condition_map=mappings["condition_map"],
        authentication_factories=factories,
    )


def execute(namespace) -> int:
    roots = [Path(s) for s in namespace.source]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        raise ValueError(f"source path(s) not found: {', '.join(missing)}")

    in_place = bool(getattr(namespace, "in_place", False))
    output = getattr(namespace, "output", None)
    force = bool(getattr(namespace, "force", False))
    opinionated = bool(getattr(namespace, "opinionated", False))
    semantics_path = getattr(namespace, "cedar_semantics", None)
    if semantics_path and not getattr(namespace, "inventory", False):
        raise ValueError("--cedar-semantics requires --inventory and --write-cedar")

    if getattr(namespace, "inventory", False):
        if in_place or output:
            raise ValueError("--inventory is a report and cannot be combined with emit mode")
        from .inventory import inventory_projects

        inventory = inventory_projects(
            roots,
            target_python=getattr(namespace, "target_python", "3.14"),
            migration_strategy=getattr(namespace, "migration_strategy", "preserve"),
        )
        rendered = inventory.to_json()
        write_path = getattr(namespace, "write_inventory", None)
        check_path = getattr(namespace, "check_inventory", None)
        if write_path:
            target = Path(write_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(target)
        elif check_path:
            target = Path(check_path)
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                print(f"migration inventory differs: {target}")
                return EXIT_WORK_REMAINS
        cedar_path = getattr(namespace, "write_cedar", None)
        if semantics_path and not cedar_path:
            raise ValueError("--cedar-semantics requires --write-cedar")
        if cedar_path:
            target = Path(cedar_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            options = _cedar_options(semantics_path)
            generated = (
                inventory.cedar_module() if options is None else inventory.cedar_module(**options)
            )
            temporary.write_text(generated, encoding="utf-8")
            temporary.replace(target)
        if getattr(namespace, "as_json", False):
            print(rendered, end="")
        elif not write_path and not check_path and not cedar_path:
            print(inventory.to_markdown(), end="")
        counts = inventory.counts()
        blocked = any(
            project.dependencies["target_status"] in {"project-blocked", "lock-blocked"}
            for project in inventory.projects
        )
        skipped = any(project.report.skipped for project in inventory.projects)
        analyzed = sum(project.report.files_analyzed for project in inventory.projects)
        if not analyzed:
            return EXIT_NOT_RUN
        if counts["unsupported"] or blocked or skipped:
            return EXIT_WORK_REMAINS
        return EXIT_OK

    # Emit mode (Phase 1): --output <dir> or --in-place. Otherwise report-only.
    if in_place or output:
        # Before writing anything: say what this tree is. Emit runs no rules, so
        # without this it will happily produce a complete "ported" copy of an
        # application it has not translated a line of.
        emit_detection = detect_roots(roots)
        for warning in (emit_detection.warnings() if emit_detection else ()):
            print(f"wreath port: {warning}", file=sys.stderr)
        total = 0
        touched = 0
        failed = []
        for root in roots:
            result = port_tree(
                root, output, in_place=in_place, force=force, opinionated=opinionated
            )
            total += len(result.written_files) + len(result.regenerated)
            touched += (
                len(result.written_files)
                + len(result.regenerated)
                + len(result.skipped)
                + len(result.failed)
            )
            failed.extend(result.failed)
            for path in result.written_files:
                print(f"wrote      {path}")
            for path in result.regenerated:
                print(f"regenerated {path}")
            for path in result.skipped:
                print(f"skipped    {path}")
            for item in result.failed:
                print(f"FAILED     {item.file} — {item.reason}: {item.detail}")
        print(f"\n{total} file(s) emitted. Review every `# TODO(wreath-port: ...)` before use.")
        if failed:
            noun = "file" if len(failed) == 1 else "files"
            print(f"{len(failed)} {noun} could not be read and were not ported.")
            return EXIT_WORK_REMAINS
        if not touched:
            print("No Python files were found. Check the source path.")
            return EXIT_NOT_RUN
        return EXIT_OK

    report = analyze_all(roots)
    rules = set(getattr(namespace, "rule", None) or ())
    context = int(getattr(namespace, "context", 0) or 0)
    if getattr(namespace, "as_json", False):
        print(json.dumps(report.as_dict(), indent=2))
    elif getattr(namespace, "by_rule", False):
        print(render_by_rule(report), end="")
    elif rules or context:
        print(render_sites(report, rules, context), end="")
    else:
        print(report.to_markdown())

    _warn_about_stack(report)

    # Nothing analyzed is the one case that is about the *run* rather than the
    # code: no Python file was read, so there is no report to have an opinion
    # about. Recognizing nothing across files that were read is different — that
    # is an already-ported tree, and it is clean.
    if not report.files_analyzed:
        return EXIT_NOT_RUN
    if report.counts()["unsupported"] or report.skipped:
        return EXIT_WORK_REMAINS
    # A framework this tool does not translate is work remaining, not a clean
    # run. Exiting 0 there is how a Django tree and an already-ported tree came
    # to look identical to CI. Absence of evidence stays clean, though: a tree
    # with no web framework at all may simply be the ported one, so only a
    # *positively identified* foreign framework or monkeypatch flips this.
    detection = report.detection
    if detection is not None and (detection.foreign or detection.monkeypatched):
        return EXIT_WORK_REMAINS
    return EXIT_OK


def _warn_about_stack(report) -> None:
    """Put the detection verdict on stderr, whatever the chosen rendering.

    stderr so `--json` stays a clean pipe, and unconditionally so the `--by-rule`
    and `--rule` views — which render findings only — cannot show a coverage
    number without the sentence that qualifies it.
    """
    detection = getattr(report, "detection", None)
    if detection is None:
        return
    for warning in detection.warnings():
        print(f"wreath port: {warning}", file=sys.stderr)
