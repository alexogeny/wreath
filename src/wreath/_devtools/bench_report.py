"""Render raw benchmark result JSON into a self-contained local HTML report.

Used two ways, and they share one renderer so the live report and the CLI can
never drift apart:

- ``benchmarks/run.py`` and ``benchmarks/lifecycle.py`` call ``generate_report``
  after every scenario to refresh ``latest.html``.
- ``wreath-bench-report`` renders any saved result documents on demand.

The report is one HTML file with no external requests: no CDN, no webfont, no
network. It is written to disk and opened locally; nothing is uploaded.

What this reports that a single run cannot
------------------------------------------
Pass several result documents and rows are grouped by (scenario, framework,
protocol) and reduced to a *median* with the observed range printed beside it.
That matters because a benchmark's run-to-run spread is frequently larger than
the gap it is being used to argue about, and AGENTS.md's "never claim a win from
a single run" is unenforceable if the report cannot show the spread.

A winner is crowned only when it is real:

- the rows must be rankable at all (``is_rankable``) -- one protocol, one load
  generator, and never an errored row; and
- with repeated runs, the leader's worst sample must still beat the runner-up's
  best sample. Overlapping ranges are labelled "within noise" rather than being
  awarded to whichever median happened to land higher.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import webbrowser
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

#: Detail metrics, all lower-is-better.
_METRICS = (
    ("latency_ms_p95", "p95 latency", "ms"),
    ("latency_ms_p99", "p99 latency", "ms"),
    ("normalized_100k_seconds", "normalized 100k-request end-to-end time", "s"),
)
#: The overview metric, higher-is-better. Absent from some documents.
_HEADLINE = "requests_per_second"
_KEY_FIELDS = ("scenario", "framework", "protocol")

#: Document kinds this renders. Anything else is reported and skipped.
_KIND_ROWS = "rows"
_KIND_ROUTING_MEMORY = "routing-memory"
_KIND_ROUTING_BACKENDS = "routing-backends"
_KIND_POSTGRES = "postgres-workload"
_KIND_ORM = "orm-competitors"
_KIND_MIGRATIONS = "migration-resolution"
_KIND_CEDAR = "cedar-authorization"


def classify(document: dict[str, Any]) -> str:
    """Which renderer a loaded document belongs to."""
    tool = document.get("tool")
    if tool == "benchmarks.bench_routing_memory":
        return _KIND_ROUTING_MEMORY
    if tool == "benchmarks.bench_routing_backends":
        return _KIND_ROUTING_BACKENDS
    if tool == "benchmarks.postgres.bench_orm_competitors":
        return _KIND_ORM
    if tool == "benchmarks.bench_migration_resolution":
        return _KIND_MIGRATIONS
    if tool == "benchmarks.bench_cedar":
        return _KIND_CEDAR
    if isinstance(document.get("scenarios"), dict) and "results" not in document:
        return _KIND_POSTGRES
    if document.get("results"):
        return _KIND_ROWS
    return "unknown"


def _mib(value: float) -> str:
    return f"{value / (1024 * 1024):,.1f}"


def _cell(text: str, best: bool = False, worst: bool = False, dim: bool = False) -> str:
    classes = " ".join(c for c, on in
                       (("win", best), ("lose", worst), ("dim", dim)) if on)
    return f'<td class="{classes}">{text}</td>' if classes else f"<td>{text}</td>"


# --- ranking guards -------------------------------------------------------
# A report may only crown a winner when the comparison is legitimate. These are
# the rules, and they are deliberately conservative.

def _format_value(value: float, unit: str) -> str:
    return f"{value:.3f} {unit}"


def _distinct(rows: list[dict[str, Any]], key: str) -> set[str]:
    return {str(row[key]) for row in rows if row.get(key) is not None}


def has_mixed_generators(rows: list[dict[str, Any]]) -> bool:
    """True when compared rows were produced by more than one load generator."""
    return len(_distinct(rows, "load_generator")) > 1


def has_mixed_protocols(rows: list[dict[str, Any]]) -> bool:
    return len(_distinct(rows, "protocol")) > 1


def is_rankable(rows: list[dict[str, Any]]) -> bool:
    """A set of rows may be ranked only when the error-free rows share a single
    protocol and a single load generator (never rank mixed-generator or
    cross-protocol results, and never rank an errored row)."""
    valid = [row for row in rows if int(row.get("errors", 0)) == 0]
    if not valid:
        return False
    return len(_distinct(valid, "load_generator")) <= 1 and \
        len(_distinct(valid, "protocol")) <= 1


# --- aggregation ----------------------------------------------------------

def merge_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce N result documents to one, medianed per (scenario, framework, protocol).

    Every numeric metric keeps its raw samples under ``_samples`` so the range can
    be shown; aggregation never replaces the raw values.
    """
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    order: list[tuple[str, ...]] = []
    for document in documents:
        for row in document.get("results", []):
            if not isinstance(row, dict) or not all(f in row for f in _KEY_FIELDS):
                continue  # tolerate old/foreign result shapes when aggregating many dirs
            key = tuple(str(row[field]) for field in _KEY_FIELDS)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(row)

    merged: list[dict[str, Any]] = []
    for key in order:
        rows = grouped[key]
        row = dict(rows[0])
        samples: dict[str, list[float]] = {}
        for metric in (_HEADLINE, *(name for name, _t, _u in _METRICS)):
            values = [
                float(item[metric]) for item in rows
                if isinstance(item.get(metric), int | float)
            ]
            if not values:
                continue
            samples[metric] = values
            row[metric] = statistics.median(values)
        row["errors"] = sum(int(item.get("errors", 0)) for item in rows)
        row["_samples"] = samples
        row["_runs"] = len(rows)
        merged.append(row)

    metadata: dict[str, Any] = dict(documents[0].get("metadata", {}))
    if len(documents) > 1:
        metadata["runs_merged"] = len(documents)
    return {"metadata": metadata, "results": merged}


def _range(row: dict[str, Any], metric: str) -> tuple[float, float] | None:
    values = row.get("_samples", {}).get(metric)
    if not values or len(values) < 2:
        return None
    return min(values), max(values)


def _separated(rows: list[dict[str, Any]], metric: str, lower_better: bool) -> bool:
    """Is the leader's advantage bigger than the run-to-run spread?

    With one run per row there is no spread to compare against, so this answers
    True and the caller falls back to the plain median ordering.
    """
    valid = [r for r in rows if int(r.get("errors", 0)) == 0 and metric in r]
    if len(valid) < 2:
        return True
    ordered = sorted(valid, key=lambda r: float(r[metric]), reverse=not lower_better)
    lead, second = ordered[0], ordered[1]
    lead_range, second_range = _range(lead, metric), _range(second, metric)
    if lead_range is None or second_range is None:
        return True
    if lower_better:  # lead's worst (max) must still beat second's best (min)
        return lead_range[1] < second_range[0]
    return lead_range[0] > second_range[1]


# --- rendering ------------------------------------------------------------

def _bars(rows: list[dict[str, Any]], metric: str, title: str, unit: str,
          lower_better: bool = True) -> str:
    """One metric across frameworks as a ranked bar group.

    Used as the report's hero. The detail lives in the tables below it; this is
    for reading the shape of a result at a glance, not for extracting numbers.
    """
    present = [row for row in rows if isinstance(row.get(metric), int | float)]
    if not present:
        return ""
    valid = [row for row in present if int(row.get("errors", 0)) == 0]
    rankable = is_rankable(present)
    resolved = _separated(present, metric, lower_better=lower_better)
    pick = min if lower_better else max
    winner = pick((float(row[metric]) for row in valid), default=None) if rankable else None
    maximum = max((float(row[metric]) for row in present), default=1.0) or 1.0
    bars: list[str] = []
    for row in sorted(present, key=lambda item: float(item[metric]),
                      reverse=not lower_better):
        value = float(row[metric])
        width = max(1.0, value / maximum * 100)
        errored = int(row.get("errors", 0)) != 0
        is_winner = winner is not None and value == winner and not errored and resolved
        tags = ""
        if is_winner:
            tags += '<span class="tag tag-win">WINNER</span>'
        if errored:
            tags += '<span class="tag tag-err">ERRORS</span>'
        span = _range(row, metric)
        spread = (
            f'<span class="spread">{span[0]:,.3f}–{span[1]:,.3f}</span>' if span else ""
        )
        bars.append(
            '<div class="bar-row">'
            f'<div class="who">{escape(str(row["framework"]))}</div>'
            '<div class="track">'
            f'<div class="bar{" bar-win" if is_winner else ""}'
            f'{" bar-err" if errored else ""}" style="width:{width:.2f}%"></div>'
            "</div>"
            f'<div class="value">{_format_value(value, unit)}{spread}{tags}</div>'
            "</div>"
        )
    note = ""
    if rankable and not resolved:
        note = (
            '<p class="within-noise">Ranges overlap across runs: the gap here is '
            "smaller than the run-to-run spread, so no winner is crowned.</p>"
        )
    return (
        f'<section class="chart"><h3>{escape(title)}</h3>{note}{"".join(bars)}</section>'
    )


def _chart(rows: list[dict[str, Any]], metric: str, title: str, unit: str) -> str:
    """One lower-is-better metric across frameworks, as a ranked bar group."""
    return _bars(rows, metric, title, unit, lower_better=True)


def _scenario_table(rows: list[dict[str, Any]]) -> str:
    """Every metric for one scenario, as one table. Best per column is green."""
    columns: list[tuple[str, str, bool]] = []
    if any(_HEADLINE in row for row in rows):
        columns.append((_HEADLINE, "req/s", False))
    columns += [(name, title, True) for name, title, _u in _METRICS
                if any(name in row for row in rows)]
    if not columns:
        return ""
    rankable = is_rankable(rows)
    head = "".join(f"<th>{escape(t)}</th>" for _m, t, _lb in columns)
    body: list[str] = []
    for row in rows:
        errored = int(row.get("errors", 0)) != 0
        cells: list[str] = []
        for metric, _title, lower_better in columns:
            if not isinstance(row.get(metric), int | float):
                cells.append('<td class="dim">—</td>')
                continue
            value = float(row[metric])
            valid = [float(r[metric]) for r in rows
                     if isinstance(r.get(metric), int | float)
                     and int(r.get("errors", 0)) == 0]
            resolved = _separated(rows, metric, lower_better=lower_better)
            best = (min(valid) if lower_better else max(valid)) if valid else None
            is_best = (rankable and resolved and not errored
                       and best is not None and value == best and len(valid) > 1)
            span = _range(row, metric)
            hint = f' title="runs: {span[0]:,.3f}–{span[1]:,.3f}"' if span else ""
            shown = f"{value:,.0f}" if metric == _HEADLINE else f"{value:.3f}"
            cells.append(
                f'<td class="{"win" if is_best else ""}"{hint}>{shown}</td>'
            )
        name = escape(str(row["framework"]))
        tag = '<span class="tag tag-err">ERRORS</span>' if errored else ""
        body.append(f"<tr><td>{name} {tag}</td>{''.join(cells)}</tr>")
    return (
        '<div class="scroll"><table><thead><tr><th>framework</th>'
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _overview(rows: list[dict[str, Any]], scenarios: list[str]) -> str:
    """Scenario x framework matrix of the headline metric, higher-is-better."""
    if not any(_HEADLINE in row for row in rows):
        return ""
    frameworks = list(dict.fromkeys(str(r["framework"]) for r in rows))
    head = "".join(f"<th>{escape(f)}</th>" for f in frameworks)
    body: list[str] = []
    for scenario in scenarios:
        cells: list[str] = []
        here = [r for r in rows if str(r["scenario"]) == scenario]
        rankable = is_rankable(here)
        resolved = _separated(here, _HEADLINE, lower_better=False)
        best = max(
            (float(r[_HEADLINE]) for r in here
             if _HEADLINE in r and int(r.get("errors", 0)) == 0),
            default=None,
        ) if rankable and resolved else None
        for framework in frameworks:
            row = next((r for r in here if str(r["framework"]) == framework), None)
            if row is None or _HEADLINE not in row:
                cells.append('<td class="dim">—</td>')
                continue
            value = float(row[_HEADLINE])
            win = best is not None and value == best
            span = _range(row, _HEADLINE)
            title = f' title="runs: {span[0]:,.0f}–{span[1]:,.0f}"' if span else ""
            cells.append(
                f'<td class="{"win" if win else ""}"{title}>{value:,.0f}</td>'
            )
        marker = "" if rankable and resolved else '<span class="dim"> ·&nbsp;unresolved</span>'
        body.append(
            f"<tr><td>{escape(scenario)}{marker}</td>{''.join(cells)}</tr>"
        )
    return (
        '<section class="block"><h2>Throughput overview</h2>'
        '<p class="sub">Requests per second, higher is better. Hover a cell for its '
        "range across runs. Scenarios marked unresolved have a lead smaller than "
        "their own run-to-run spread.</p>"
        '<div class="scroll"><table><thead><tr><th>scenario</th>'
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div></section>"
    )


def _extreme_class(value: float, values: list[float], lower_better: bool) -> dict[str, bool]:
    """Green on the best, red on the worst -- only when there is a real spread."""
    if len(values) < 2 or max(values) == min(values):
        return {}
    best, worst = (min(values), max(values)) if lower_better else (max(values), min(values))
    if max(values) / min(values) < 1.05:  # a 5% spread is not a story
        return {}
    return {"best": value == best, "worst": value == worst}


_PROTOCOLS = ("http/1.1", "h2", "h3")

#: What each server can serve at all, independent of what was measured. A blank
#: cell otherwise reads as "lost", when usually it means "cannot enter".
#: Verified against the installed libraries rather than their documentation:
#: Sanic's own `sanic.http.constants.HTTP` enumerates VERSION_1 and VERSION_3
#: only -- it never implemented HTTP/2 -- and Uvicorn is HTTP/1.1-only, which is
#: what bounds every framework it hosts.
_SERVER_PROTOCOLS: tuple[tuple[str, frozenset[str], str], ...] = (
    ("wreath-native", frozenset({"http/1.1", "h2", "h3"}), "Wreath's own server"),
    (
        "sanic-native",
        frozenset({"http/1.1", "h3"}),
        "Sanic's own server implements HTTP/1 and HTTP/3; it has no HTTP/2",
    ),
    (
        "uvicorn",
        frozenset({"http/1.1"}),
        "Uvicorn speaks HTTP/1.1 only, so every framework it hosts does too",
    ),
)


def _server_capability(server: str) -> tuple[frozenset[str], str]:
    """Match a row's `server` string, which carries a loop and CPU suffix."""
    for prefix, protocols, note in _SERVER_PROTOCOLS:
        if server.startswith(prefix):
            return protocols, note
    return frozenset(_PROTOCOLS), ""


def _protocol_section(rows: list[dict[str, Any]]) -> str:
    """Throughput per protocol, per stack, with unsupported cells named as such.

    Protocol is a result dimension, not a contest: nothing is ranked *across*
    columns, because a request is not the same amount of work in HTTP/1.1 and
    HTTP/3. Within one column the frameworks are comparable, and are ranked --
    unless the rows used different load generators, in which case they are not
    comparable at all and no winner is crowned.
    """
    if not rows:
        return ""
    present = [p for p in _PROTOCOLS if any(str(r.get("protocol")) == p for r in rows)]
    if len(present) < 2:
        return ""  # one protocol is not a comparison

    stacks: dict[tuple[str, str], dict[str, list[float]]] = {}
    for row in rows:
        # The server string carries loop/CPU detail that would split a stack
        # into several rows; key on the framework and keep one label.
        key = (str(row["framework"]), str(row["server"]).split(" [")[0])
        stacks.setdefault(key, {}).setdefault(str(row.get("protocol")), []).append(
            float(row["requests_per_second"])
        )

    ranked = not has_mixed_generators(rows)
    blocks: list[str] = []
    head = "".join(f"<th>{escape(p)}</th>" for p in present)
    body: list[str] = []
    for (framework, server), by_protocol in sorted(stacks.items()):
        capable, note = _server_capability(server)
        medians = {p: statistics.median(v) for p, v in by_protocol.items()}
        column_best: dict[str, float] = {}
        for protocol in present:
            values = [
                statistics.median(s.get(protocol, [0]))
                for s in stacks.values()
                if s.get(protocol)
            ]
            if values:
                column_best[protocol] = max(values)
        cells: list[str] = []
        for protocol in present:
            if protocol in medians:
                value = medians[protocol]
                best = ranked and value == column_best.get(protocol)
                cells.append(_cell(f"{value:,.0f}", best=best))
            elif protocol not in capable:
                cells.append(
                    f'<td class="dim" title="{escape(note)}">not supported</td>'
                )
            else:
                cells.append('<td class="dim" title="not measured in this run">—</td>')
        body.append(
            f"<tr><td>{escape(framework)}<br><span class='sub'>{escape(server)}</span>"
            f"</td>{''.join(cells)}</tr>"
        )
    blocks.append(
        '<div class="scroll"><table><thead><tr><th>framework</th>'
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )
    caveat = (
        ""
        if ranked
        else '<p class="warning">Rows use different load generators, so nothing '
        "here is ranked.</p>"
    )
    return (
        '<section class="block"><h2>Protocols, and who can speak them</h2>'
        '<p class="sub">Requests per second, higher is better. <b>Nothing is ranked '
        "across columns</b> — a request is not the same work in HTTP/1.1 as in HTTP/3, "
        "so protocol is a result dimension, not a contest. Within a column the stacks "
        "are comparable. <b>not supported</b> means the server cannot speak that "
        "protocol at all, which is different from a protocol that simply was not "
        "measured; hover it for why.</p>"
        f"{caveat}{''.join(blocks)}</section>"
    )


def _routing_memory_section(documents: list[dict[str, Any]]) -> str:
    """Compiled size, lazy growth and peak RSS per routing backend, per shape."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    shapes: list[str] = []
    for document in documents:
        for mode, rows in document.get("raw", {}).items():
            for row in rows:
                shape = str(row.get("shape", "app"))
                if shape not in shapes:
                    shapes.append(shape)
                grouped.setdefault((shape, mode), []).append(row)
    if not grouped:
        return ""

    blocks: list[str] = []
    for shape in shapes:
        modes = [m for (s, m) in grouped if s == shape]
        routes = grouped[(shape, modes[0])][0].get("routes", "?")
        def med(mode: str, key: str, _shape: str = shape) -> float:
            return statistics.median(
                [float(r[key]) for r in grouped[(_shape, mode)] if key in r]
            )
        totals = [med(m, "total_bytes") for m in modes]
        peaks = [med(m, "vmhwm_bytes") for m in modes]
        body: list[str] = []
        for mode in modes:
            total, peak = med(mode, "total_bytes"), med(mode, "vmhwm_bytes")
            eager, lazy = med(mode, "compiled_bytes"), med(mode, "lazy_bytes")
            tcls = _extreme_class(total, totals, lower_better=True)
            pcls = _extreme_class(peak, peaks, lower_better=True)
            body.append(
                f"<tr><td>{escape(mode)}</td>"
                + _cell(f"{_mib(total)} MiB", tcls.get("best", False), tcls.get("worst", False))
                + _cell(f"{_mib(eager)}", dim=True)
                + _cell(f"{_mib(lazy)}", dim=True)
                + _cell(f"{_mib(peak)} MiB", pcls.get("best", False), pcls.get("worst", False))
                + _cell(f"{med(mode, 'compile_seconds') * 1000:,.1f} ms")
                + "</tr>"
            )
        blocks.append(
            f'<div class="scroll"><table>'
            f'<caption>{escape(shape)} — {routes:,} routes</caption>'
            "<thead><tr><th>mode</th><th>total</th><th>eager</th><th>lazy</th>"
            "<th>peak RSS</th><th>compile</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>"
        )
    return (
        '<section class="block"><h2>Routing memory over the app lifecycle</h2>'
        '<p class="sub">What each backend holds resident, measured in a fresh process per '
        "mode. <b>total</b> is what it actually costs; <b>eager</b> is what "
        "<code>_compile_routes()</code> allocates and <b>lazy</b> is what appears later as "
        "groups build on first match — i.e. in production, under traffic. Reading only the "
        "eager column understates a backend by everything in the lazy one.</p>"
        f'<div class="grid2">{"".join(blocks)}</div></section>'
    )


def _routing_backends_section(documents: list[dict[str, Any]]) -> str:
    """Every route table implementation on the same queries."""
    tables: dict[str, dict[str, list[float]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    caveat = ""
    for document in documents:
        caveat = document.get("caveat", caveat)
        for table in document.get("tables", []):
            name = str(table["name"])
            meta.setdefault(name, table)
            for backend, values in table.get("backends", {}).items():
                tables.setdefault(name, {}).setdefault(backend, []).extend(
                    values.get("raw_seconds", [])
                )
    if not tables:
        return ""

    blocks: list[str] = []
    for name, backends in tables.items():
        info = meta[name]
        native = {b: statistics.median(v) for b, v in backends.items() if b.startswith("c-")}
        body: list[str] = []
        for backend, samples in backends.items():
            value = statistics.median(samples)
            is_native = backend.startswith("c-")
            cls = _extreme_class(value, list(native.values()), lower_better=True) \
                if is_native else {}
            ns = value / int(info.get("queries", 1)) * 1e9
            body.append(
                f'<tr><td class="{"" if is_native else "dim"}">{escape(backend)}</td>'
                + _cell(f"{value * 1e3:.2f} ms", cls.get("best", False),
                        cls.get("worst", False), dim=not is_native)
                + _cell(f"{ns:.0f} ns", dim=not is_native)
                + "</tr>"
            )
        blocks.append(
            '<div class="scroll"><table>'
            f'<caption>{escape(name)} — {escape(str(info.get("description", "")))}</caption>'
            "<thead><tr><th>backend</th><th>per pass</th><th>per match</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>"
        )
    note = f'<p class="within-noise">{escape(caveat)}</p>' if caveat else ""
    return (
        '<section class="block"><h2>Routing backends, head to head</h2>'
        '<p class="sub">The same queries against every table implementation. Green and red '
        "mark the fastest and slowest <em>native</em> backend; the pure-Python twins are "
        "dimmed because they are a different contest.</p>"
        f'{note}<div class="grid2">{"".join(blocks)}</div></section>'
    )


def _orm_section(documents: list[dict[str, Any]]) -> str:
    """Wreath's ORM against the ORMs people actually use."""
    ops: dict[str, dict[str, list[float]]] = {}
    sync: set[str] = set()
    for document in documents:
        for op, orms in document.get("scenarios", {}).items():
            for orm, values in orms.items():
                if not isinstance(values, dict) or "median_ms" not in values:
                    continue  # skips the wreath_speedup_vs summary block
                if values.get("sync"):
                    sync.add(orm)
                ops.setdefault(op, {}).setdefault(orm, []).append(
                    float(values["median_ms"])
                )
    if not ops:
        return ""
    names = list(dict.fromkeys(o for orms in ops.values() for o in orms))
    head = "".join(
        f'<th>{escape(n)}{" (sync)" if n in sync else ""}</th>' for n in names
    )
    body: list[str] = []
    for op, orms in ops.items():
        medians = {o: statistics.median(v) for o, v in orms.items()}
        # Peewee is synchronous, so it is shown but excluded from the ranking:
        # colouring it against three async ORMs would be comparing unlike things.
        ranked = [v for o, v in medians.items() if o not in sync]
        cells: list[str] = []
        for name in names:
            if name not in medians:
                cells.append('<td class="dim">—</td>')
                continue
            value = medians[name]
            cls = _extreme_class(value, ranked, lower_better=True) \
                if name not in sync else {}
            cells.append(_cell(f"{value:.3f}", cls.get("best", False),
                               cls.get("worst", False), dim=name in sync))
        slowest = max(ranked) if ranked else None
        # Always emit this cell: when Wreath is omitted from a scenario the row
        # would otherwise be one column short and the table would misalign.
        ratio = '<td class="dim">—</td>'
        if "wreath" in medians and medians["wreath"] and slowest:
            ratio = f'<td class="win">{slowest / medians["wreath"]:.1f}x</td>'
        body.append(f"<tr><td>{escape(op)}</td>{''.join(cells)}{ratio}</tr>")
    return (
        '<section class="block"><h2>ORM, against the alternatives</h2>'
        '<p class="sub">Median milliseconds per operation, lower is better. Every '
        "operation returns hydrated model instances against the same table and rows, and "
        "each is row-count checked before timing so nothing can look fast by fetching "
        "less; relationship scenarios touch each loaded relation inside the timed "
        "operation, so none can win by deferring the join. Peewee is <b>synchronous</b> "
        "— shown for scale, dimmed, and excluded from the ranking, because it does not "
        "pay for the event loop the others do. An ORM is <b>omitted</b> from any scenario "
        "it does not support natively rather than given a hand-written equivalent, so a "
        "dash (—) means “no first-class way to express this,” not a failure.</p>"
        '<div class="scroll"><table><thead><tr><th>operation</th>'
        f"{head}<th>wreath vs slowest async</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div></section>"
    )


def _migration_section(documents: list[dict[str, Any]]) -> str:
    """Equivalent already-current plan resolution across migration tools."""
    tools: dict[str, list[float]] = {}
    fleet: list[tuple[int, float]] = []
    fairness = ""
    for document in documents:
        fairness = str(document.get("fairness", fairness))
        for name, values in document.get("results", {}).items():
            if isinstance(values, dict) and "median_ns" in values:
                tools.setdefault(name, []).append(float(values["median_ns"]))
        values = document.get("fleet")
        if isinstance(values, dict) and "median_ns_per_tenant" in values:
            fleet.append((
                int(values.get("tenants", 0)),
                float(values["median_ns_per_tenant"]),
            ))
    if not tools:
        return ""
    medians = {name: statistics.median(values) for name, values in tools.items()}
    ranked = list(medians.values())
    body = []
    for name, value in medians.items():
        cls = _extreme_class(value, ranked, lower_better=True)
        rate = 1_000_000_000 / value
        body.append(
            "<tr>"
            f"<td>{escape(name)}</td>"
            + _cell(f"{value:,.0f}", cls.get("best", False), cls.get("worst", False))
            + _cell(f"{rate:,.0f}", cls.get("best", False), cls.get("worst", False))
            + "</tr>"
        )
    fleet_note = ""
    if fleet:
        tenants, ns = max(fleet, key=lambda item: item[0])
        fleet_note = (
            f"<p><b>Wreath-metal packed fleet:</b> {tenants:,} already-current tenants at "
            f"{ns:,.1f} ns/tenant ({1_000_000_000 / ns:,.0f} tenants/s). "
            "This row is informative and unranked.</p>"
        )
    return (
        '<section class="block"><h2>Migration resolution</h2>'
        '<p class="sub">Already-current migration-plan resolution, median nanoseconds per '
        "schema; lower is better. This measures in-memory control-plane resolution after "
        "current state is known—not catalog I/O or DDL.</p>"
        f'<p class="within-noise">{escape(fairness)}</p>'
        '<div class="scroll"><table><thead><tr><th>tool</th><th>ns/schema</th>'
        f"<th>resolutions/s</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        f"{fleet_note}{_migration_generation_block(documents)}"
        f"{_migration_artifact_block(documents)}</section>"
    )


def _migration_generation_block(documents: list[dict[str, Any]]) -> str:
    """Plan generation for the shared drift; side by side, deliberately unranked."""
    tools: dict[str, list[float]] = {}
    operations: dict[str, int] = {}
    fairness = ""
    for document in documents:
        section = document.get("generation")
        if not isinstance(section, dict):
            continue
        fairness = str(section.get("fairness", fairness))
        for name, values in section.get("results", {}).items():
            if isinstance(values, dict) and "median_ns" in values:
                tools.setdefault(name, []).append(float(values["median_ns"]))
                operations[name] = int(values.get("operations", 0))
    if not tools:
        return ""
    body = []
    for name, values in tools.items():
        median = statistics.median(values)
        body.append(
            "<tr>"
            f"<td>{escape(name)}</td>"
            f"<td>{operations.get(name, 0):,}</td>"
            f"<td>{median:,.0f}</td>"
            f"<td>{1_000_000_000 / median:,.0f}</td>"
            "</tr>"
        )
    return (
        "<h3>Plan generation (side by side, not ranked)</h3>"
        f'<p class="within-noise">{escape(fairness)}</p>'
        '<div class="scroll"><table><thead><tr><th>tool</th><th>ops</th>'
        "<th>ns/plan</th><th>plans/s</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _migration_artifact_block(documents: list[dict[str, Any]]) -> str:
    """Checksummed artifact verification; Wreath-only and unranked."""
    for document in documents:
        section = document.get("artifact")
        if not isinstance(section, dict) or "median_ns" not in section:
            continue
        median = float(section["median_ns"])
        return (
            f"<p><b>WMA1 artifact verification:</b> {int(section.get('bytes', 0)):,}-byte "
            f"artifact verified from bytes in {median:,.0f} ns "
            f"({1_000_000_000 / median:,.0f} verifications/s). "
            f"{escape(str(section.get('fairness', '')))}</p>"
        )
    return ""


def _cedar_section(documents: list[dict[str, Any]]) -> str:
    """Cedar authorization latency: built-in engine, pure twin, and cedarpy."""
    evaluate: dict[str, list[float]] = {}
    stateless: dict[str, list[float]] = {}
    fairness = ""
    skipped: dict[str, str] = {}
    for document in documents:
        fairness = str(document.get("fairness", fairness))
        skipped.update({str(k): str(v) for k, v in document.get("skipped", {}).items()})
        for target, key in ((evaluate, "evaluate"), (stateless, "parse_and_evaluate")):
            for name, values in document.get(key, {}).items():
                if isinstance(values, dict) and "median_ns" in values:
                    target.setdefault(name, []).append(float(values["median_ns"]))
    if not evaluate and not stateless:
        return ""

    def table(title: str, tools: dict[str, list[float]], ranked: bool) -> str:
        if not tools:
            return ""
        medians = {name: statistics.median(values) for name, values in tools.items()}
        spread = list(medians.values())
        body = []
        for name, value in medians.items():
            cls = _extreme_class(value, spread, lower_better=True) if ranked else {}
            body.append(
                "<tr>"
                f"<td>{escape(name)}</td>"
                + _cell(f"{value:,.0f}", cls.get("best", False), cls.get("worst", False))
                + _cell(
                    f"{1_000_000_000 / value:,.0f}",
                    cls.get("best", False),
                    cls.get("worst", False),
                )
                + "</tr>"
            )
        return (
            f"<h3>{escape(title)}</h3>"
            '<div class="scroll"><table><thead><tr><th>engine</th><th>ns/call</th>'
            f"<th>calls/s</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        )

    skip_note = "".join(
        f"<p><b>{escape(name)}:</b> skipped ({escape(reason)}).</p>"
        for name, reason in skipped.items()
    )
    return (
        '<section class="block"><h2>Cedar authorization</h2>'
        '<p class="sub">Two authorizations (one allow, one deny) against a six-policy set '
        "per call, median nanoseconds; lower is better. Decisions are verified to agree "
        "across engines before timing.</p>"
        f'<p class="within-noise">{escape(fairness)}</p>'
        + table("Evaluate (policies compiled once; Wreath engine vs pure twin)",
                evaluate, ranked=False)
        + table("Parse and evaluate (full per-call cost, both arms)", stateless, ranked=True)
        + skip_note
        + "</section>"
    )


def _postgres_section(documents: list[dict[str, Any]]) -> str:
    """Driver latency per operation against the ecosystem drivers."""
    ops: dict[str, dict[str, list[float]]] = {}
    for document in documents:
        for op, drivers in document.get("scenarios", {}).items():
            for driver, values in drivers.items():
                if not isinstance(values, dict) or "median_ms" not in values:
                    continue
                ops.setdefault(op, {}).setdefault(driver, []).append(
                    float(values["median_ms"])
                )
    if not ops:
        return ""
    names = list(dict.fromkeys(d for drivers in ops.values() for d in drivers))
    head = "".join(f"<th>{escape(n)}</th>" for n in names)
    body: list[str] = []
    for op, drivers in ops.items():
        medians = {d: statistics.median(v) for d, v in drivers.items()}
        values = list(medians.values())
        cells: list[str] = []
        for name in names:
            if name not in medians:
                cells.append('<td class="dim">—</td>')
                continue
            value = medians[name]
            cls = _extreme_class(value, values, lower_better=True)
            cells.append(_cell(f"{value:.3f}", cls.get("best", False),
                               cls.get("worst", False)))
        ratio = ""
        if "wreath" in medians and "asyncpg" in medians and medians["wreath"]:
            speedup = medians["asyncpg"] / medians["wreath"]
            klass = "win" if speedup >= 1.0 else "lose"
            ratio = f'<td class="{klass}">{speedup:.2f}x</td>'
        body.append(f"<tr><td>{escape(op)}</td>{''.join(cells)}{ratio}</tr>")
    return (
        '<section class="block"><h2>PostgreSQL driver</h2>'
        '<p class="sub">Median latency per operation in milliseconds, lower is better. '
        "Green is the fastest driver for that operation, red the slowest. The last column "
        "is Wreath against asyncpg — above 1.00x means Wreath is ahead.</p>"
        '<div class="scroll"><table><thead><tr><th>operation</th>'
        f"{head}<th>wreath vs asyncpg</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div></section>"
    )


_STYLE = """
:root{--paper:#F6F7F9;--raise:#FFF;--ink:#0E141B;--muted:#5A6672;--rule:#DDE2E8;
--rule-strong:#C3CBD4;--brass:#8A6416;--good:#0B6E4F;--bad:#A8341A;--floor:#B4BDC7;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--serif:Georgia,"Iowan Old Style","Palatino Linotype",Palatino,serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;}
@media (prefers-color-scheme:dark){:root{--paper:#0D1116;--raise:#141A22;--ink:#E6EAF0;
--muted:#98A4B2;--rule:#222B35;--rule-strong:#33404E;--brass:#D3A248;--good:#35B085;
--bad:#E0705A;--floor:#3E4956;}}
:root[data-theme=dark]{--paper:#0D1116;--raise:#141A22;--ink:#E6EAF0;--muted:#98A4B2;
--rule:#222B35;--rule-strong:#33404E;--brass:#D3A248;--good:#35B085;--bad:#E0705A;
--floor:#3E4956;}
:root[data-theme=light]{--paper:#F6F7F9;--raise:#FFF;--ink:#0E141B;--muted:#5A6672;
--rule:#DDE2E8;--rule-strong:#C3CBD4;--brass:#8A6416;--good:#0B6E4F;--bad:#A8341A;
--floor:#B4BDC7;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);
line-height:1.6;padding:0 1.25rem 5rem}
main{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:3rem}
p{max-width:68ch}
header{padding-top:3.5rem;padding-bottom:1.5rem;border-bottom:2px solid var(--ink);
display:flex;flex-direction:column;gap:.75rem}
.eyebrow{font-family:var(--mono);font-size:.72rem;letter-spacing:.16em;
text-transform:uppercase;color:var(--muted)}
h1{font-family:var(--sans);font-weight:800;letter-spacing:-.025em;margin:0;
font-size:clamp(1.9rem,4.5vw,2.8rem);line-height:1.05;text-wrap:balance}
h2{font-family:var(--sans);font-weight:750;font-size:1.35rem;margin:0;
letter-spacing:-.015em;text-wrap:balance}
h3{font-family:var(--sans);font-weight:700;font-size:.95rem;margin:0 0 .6rem}
.sub{color:var(--muted);font-size:.92rem;margin:.15rem 0 0}
.block,.scenario{display:flex;flex-direction:column;gap:.9rem;
border-top:1px solid var(--rule-strong);padding-top:1.1rem}
.scroll{overflow-x:auto;border:1px solid var(--rule);background:var(--raise)}
table{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:.79rem;
font-variant-numeric:tabular-nums}
th,td{padding:.45rem .8rem;text-align:right;white-space:nowrap;
border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{text-align:left}
thead th{font-weight:700;font-size:.68rem;letter-spacing:.07em;text-transform:uppercase;
color:var(--muted);border-bottom:1px solid var(--rule-strong);background:var(--raise);
position:sticky;top:0}
tbody tr:last-child td{border-bottom:none}
.win{color:var(--good);font-weight:700}
.dim{color:var(--muted)}
.chart{background:var(--raise);border:1px solid var(--rule);padding:1rem 1.1rem;
display:flex;flex-direction:column;gap:.1rem}
.bar-row{display:grid;grid-template-columns:130px minmax(140px,1fr) 260px;gap:.85rem;
align-items:center;padding:.28rem 0}
.who{font-family:var(--sans);font-size:.82rem;font-weight:650}
.track{height:16px;background:var(--rule);overflow:hidden}
.bar{height:100%;background:var(--floor)}
.bar-win{background:var(--good)}
.bar-err{background:var(--bad)}
.value{font-family:var(--mono);font-size:.76rem;font-variant-numeric:tabular-nums;
white-space:nowrap;display:flex;align-items:center;gap:.5rem}
.spread{color:var(--muted);font-size:.7rem}
.tag{font-family:var(--mono);font-size:.6rem;font-weight:700;letter-spacing:.08em;
padding:.1rem .35rem;color:var(--paper);background:var(--good)}
.tag-err{background:var(--bad)}
.within-noise,.warning{font-family:var(--sans);font-size:.8rem;color:var(--muted);
border-left:2px solid var(--brass);padding-left:.7rem;margin:.2rem 0 .6rem}
.warning{color:var(--bad);border-left-color:var(--bad)}
.callout{background:var(--raise);border:1px solid var(--rule);border-left:3px solid var(--brass);
padding:.9rem 1.15rem;font-family:var(--sans);font-size:.85rem;line-height:1.62}
.callout h2{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:0 0 .55rem;border:none;padding:0}
.callout ul{margin:0;padding-left:1.15rem}
.callout li{margin:.3rem 0}
.callout b,.note b{color:var(--ink);font-weight:650}
.callout i{font-style:italic}
.note{font-family:var(--sans);font-size:.82rem;color:var(--muted);
border-left:2px solid var(--brass);padding-left:.7rem;margin:.1rem 0 .5rem}
.meta{columns:2;column-gap:2rem;font-family:var(--mono);font-size:.72rem;
color:var(--muted);line-height:1.8}
.meta div{break-inside:avoid}
.meta b{color:var(--ink);font-weight:600}
footer{border-top:1px solid var(--rule-strong);padding-top:1.1rem;font-family:var(--mono);
font-size:.71rem;color:var(--muted);line-height:1.9}
@media (max-width:760px){.bar-row{grid-template-columns:100px 1fr}
.value{grid-column:2}.meta{columns:1}}
"""


_HOW_TO_READ = (
    '<section class="callout"><h2>How to read this report</h2><ul>'
    "<li><b>Green</b> is the best value in a row, <b>red</b> the worst — but only "
    "when the gap is real (at least 5%). An uncoloured field is one where the "
    "difference is too small to mean anything.</li>"
    "<li><b>Ranges</b> (hover a value, or the small figure beside it) show the "
    "spread across repeated runs. A number is only trustworthy next to its spread: "
    "a scenario's run-to-run variation is routinely larger than the gap being "
    "argued about.</li>"
    "<li>A row reads <b>unresolved</b> or <b>within noise</b> when the leader's "
    "advantage is smaller than that spread. No winner is crowned there — overlapping "
    "ranges are a tie, whatever the medians happen to say.</li>"
    "<li>A real win means the leader's <i>worst</i> sample still beats the "
    "runner-up's <i>best</i> one. A <b>single run has no spread</b>, so nothing in a "
    "one-run report can be told apart from noise.</li>"
    "<li>Nothing is ranked <b>across protocols</b> (a request is different work in "
    "HTTP/1.1 and HTTP/3) or across <b>load generators</b>, and an <b>errored row</b> "
    "is never crowned. Flask is adapted from WSGI to ASGI, so its number carries the "
    "adapter's cost.</li>"
    "<li>This is a shared-machine development tool for sensing direction — not a "
    "published, independently-generated comparison.</li>"
    "</ul></section>"
)

#: Per-scenario semantics — the caveats that decide whether a comparison is
#: even like-for-like. Surfaced in the report so a reader need not know them.
_SCENARIO_NOTES = {
    "template": "Not an engine race. Wreath renders with its own template system "
    "while the competitors use Jinja2 (both autoescaping), so this measures each "
    "framework's idiomatic HTML rendering, not one engine against another.",
    "cache-control": "Wreath builds the header from a validated CacheControl policy "
    "object; the competitors set the raw header string. The Wreath row therefore "
    "includes work the others skip — measuring that cost is the point.",
    "webhook": "Wreath only. The competitors ship no webhook primitive, so there is "
    "no comparable row — this measures Wreath's signed-webhook verification against "
    "itself.",
    "background-noop": "Each framework uses its own response-bound background API, and "
    "completed tasks are counted and verified: none can look faster by dropping or "
    "backlogging the work it was handed.",
    "background-yield": "As background-noop, but the task yields to the event loop "
    "once before completing.",
    "middleware-noop": "Wreath only — the overhead of one compiled no-op route "
    "middleware, with no competitor equivalent.",
    "missing": "Wreath only — a definite route miss and 404 emission.",
    "auth-missing": "Wreath only — a protected route reached with no credentials.",
    "auth-authenticated": "Wreath only — bearer authentication and identity exposure.",
    "auth-rbac-allow": "Wreath only — an RBAC allow path with decision-router pruning.",
    "auth-rbac-deny": "Wreath only — an RBAC deny path with decision-router pruning.",
}


def render(document: dict[str, Any], extra: list[dict[str, Any]] | None = None) -> str:
    """The whole report as one self-contained HTML string.

    `document` is the merged framework/scenario document; `extra` carries the
    single-purpose documents (routing memory, routing backends, PostgreSQL),
    each rendered by its own section.
    """
    metadata = document.get("metadata", {})
    results: list[dict[str, Any]] = document.get("results", [])
    scenarios = list(dict.fromkeys(str(row["scenario"]) for row in results))
    extra = extra or []

    hero = ""
    if scenarios:
        first = [row for row in results if str(row["scenario"]) == scenarios[0]]
        hero = _bars(first, _HEADLINE, f"{scenarios[0]} — requests per second",
                     "req/s", lower_better=False)

    sections: list[str] = []
    for scenario in scenarios:
        rows = [row for row in results if str(row["scenario"]) == scenario]
        warnings: list[str] = []
        if has_mixed_generators(rows):
            warnings.append(
                "Rows use different load generators; cross-protocol ranking is "
                "suppressed (results are not directly comparable)."
            )
        elif has_mixed_protocols(rows):
            warnings.append(
                "Rows span multiple protocols; no winner is crowned across "
                "protocols (protocol is a result dimension, not a contest)."
            )
        banner = "".join(f'<p class="warning">{escape(w)}</p>' for w in warnings)
        caveat = _SCENARIO_NOTES.get(scenario)
        note = f'<p class="note">{escape(caveat)}</p>' if caveat else ""
        sections.append(
            f'<section class="scenario"><h2>{escape(scenario)}</h2>{note}{banner}'
            f"{_scenario_table(rows)}</section>"
        )

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for doc in extra:
        by_kind.setdefault(classify(doc), []).append(doc)
    bespoke = "".join((
        _protocol_section(results),
        _routing_backends_section(by_kind.get(_KIND_ROUTING_BACKENDS, [])),
        _routing_memory_section(by_kind.get(_KIND_ROUTING_MEMORY, [])),
        _orm_section(by_kind.get(_KIND_ORM, [])),
        _migration_section(by_kind.get(_KIND_MIGRATIONS, [])),
        _cedar_section(by_kind.get(_KIND_CEDAR, [])),
        _postgres_section(by_kind.get(_KIND_POSTGRES, [])),
    ))

    meta_html = "".join(
        f"<div><b>{escape(str(key).replace('_', ' '))}</b> {escape(str(value))}</div>"
        for key, value in metadata.items()
    )
    runs = metadata.get("runs_merged")
    if not results:
        standfirst = "Hover any value for its range across runs."
    elif runs:
        standfirst = (
            f"Medians across {runs} merged runs. Hover any value for its range."
        )
    else:
        standfirst = ("A single run. Ranges are unavailable, so no result here can be "
                      "separated from run-to-run noise.")
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wreath benchmark report</title>
<style>{_STYLE}</style>
</head>
<body><main>
<header>
  <div class="eyebrow">Wreath · benchmark report · {generated}</div>
  <h1>Benchmark report</h1>
  <p class="sub">{escape(standfirst)}</p>
</header>
{_HOW_TO_READ}
{hero}
{_overview(results, scenarios)}
{bespoke}
{"".join(sections)}
<section class="block"><h2>Run metadata</h2><div class="meta">{meta_html}</div></section>
<footer>
End-to-end time is normalized to 100,000 requests so differently tiered frameworks stay
comparable; it excludes warmup and server startup.<br>
A winner is crowned only when the rows share one protocol and one load generator, the
row is error-free, and — with repeated runs — the leader's worst sample still beats the
runner-up's best. Overlapping ranges are reported as unresolved rather than ranked.<br>
The bundled client is a development tool, not a publication-grade load generator.
</footer>
</main></body></html>
"""


def generate_report(document: dict[str, Any], output_path: Path) -> None:
    """Write `document` as an HTML report. Used for the live `latest.html`."""
    output_path.write_text(render(document), encoding="utf-8")


# --- CLI ------------------------------------------------------------------

def _load(paths: list[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file in files:
            if file.name == "latest.json" and len(files) > 1:
                continue  # a duplicate of one timestamped file in the same directory
            try:
                data = json.loads(file.read_text())
            except (OSError, json.JSONDecodeError) as error:
                print(f"skipping {file}: {error}", file=sys.stderr)
                continue
            if isinstance(data, dict) and classify(data) != "unknown":
                documents.append(data)
            else:
                print(f"skipping {file}: not a benchmark result document",
                      file=sys.stderr)
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-bench-report",
        description=(
            "Render benchmark result JSON into one self-contained local HTML file. "
            "With no paths, aggregate every benchmark-results*/ directory into a "
            "single holistic report. Pass several documents (or a directory) to get "
            "medians with the run-to-run range, which is the only way a win can be "
            "told from noise."
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path,
                        help="result JSON files or directories of them; omit to "
                             "aggregate every benchmark-results*/ directory")
    parser.add_argument("-o", "--output", type=Path, default=Path("benchmark-report.html"))
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the report in a browser when it is written")
    args = parser.parse_args(argv)

    paths = args.paths
    if not paths:
        # The holistic picture: one report over every benchmark family. Each
        # directory's own runs still yield per-scenario medians and ranges.
        paths = sorted(p for p in Path.cwd().glob("benchmark-results*") if p.is_dir())
        if not paths:
            print(f"no benchmark-results*/ directories found under {Path.cwd()}",
                  file=sys.stderr)
            return 1
        print(f"aggregating {len(paths)} benchmark "
              f"{'family' if len(paths) == 1 else 'families'}: "
              f"{', '.join(p.name for p in paths)}", file=sys.stderr)

    documents = _load(paths)
    if not documents:
        print("no benchmark result documents found", file=sys.stderr)
        return 1

    rows_docs = [d for d in documents if classify(d) == _KIND_ROWS]
    extra = [d for d in documents if classify(d) != _KIND_ROWS]
    document = merge_documents(rows_docs) if rows_docs else {"metadata": {}, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(document, extra), encoding="utf-8")

    kinds = sorted({classify(d) for d in documents})
    print(f"wrote {args.output} ({len(document['results'])} rows from "
          f"{len(rows_docs)} run(s); sections: {', '.join(kinds)})")
    if len(rows_docs) == 1:
        print("note: one run only -- no ranges, so nothing here clears its own noise.",
              file=sys.stderr)
    if args.open_browser:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
