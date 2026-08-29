"""Build-time charts from external JSON — a ```chart fenced block.

Point a page at a JSON file (a benchmark's `latest.json`, any data you already
emit) and get a bar chart rendered as inline, theme-aware SVG at build time — no
runtime JavaScript, no chart library, no CDN. The whole point is that the data
lives *outside* the docs and the chart stays in sync with it.

    ```chart
    source: ../benchmark-results/latest.json
    data: results               # dotted path to the list inside the JSON
    x: framework                # the label field
    y: requests_per_second      # the value field
    where: scenario=plaintext   # optional filter
    title: Requests/sec (plaintext)
    sort: desc
    limit: 12
    ```

The SVG uses `currentColor` for text and `var(--primary)` for bars, so it
recolors with the active theme and light/dark automatically.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeIs

from . import _fenced

__all__ = ["extract", "restore"]

_OPEN = "```chart"


def extract(
    text: str,
    base_dir: Path,
    sources: set[Path] | None = None,
) -> tuple[str, dict[str, str]]:
    """Replace each ```chart block with a token; return (text, {token: svg-html}).

    Every data file a chart successfully reads is added to `sources` (if given),
    so the caller can publish the raw JSON alongside the rendered chart.
    """
    return _fenced.extract(
        text, _OPEN, lambda body: _render(_parse(body), base_dir, sources), "CHART"
    )


restore = _fenced.restore


def _parse(config: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in config:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _render(config: dict[str, str], base_dir: Path, sources: set[Path] | None = None) -> str:
    source = config.get("source", "")
    path = base_dir / source
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return (
            f'<div class="chart-error">chart: cannot load {_esc(source)}: {_esc(str(error))}</div>'
        )
    if sources is not None:
        sources.add(path.resolve())
    node: Any = data
    for key in config.get("data", "").split("."):
        if key:
            try:
                node = node[key]
            except KeyError, TypeError, IndexError:
                return f'<div class="chart-error">chart: no data at {_esc(config["data"])}</div>'
    try:
        pairs = _pairs(node, config)
    except _ChartError as error:
        return f'<div class="chart-error">chart: {_esc(str(error))}</div>'
    if not pairs:
        return '<div class="chart-error">chart: no plottable numeric data</div>'
    if config.get("sort") == "desc":
        pairs.sort(key=lambda p: p[1], reverse=True)
    elif config.get("sort") == "asc":
        pairs.sort(key=lambda p: p[1])
    limit = _int(config.get("limit"), len(pairs))
    return _svg_bar(pairs[:limit], config.get("title", ""), config.get("unit", ""))


def _series_pairs(node: Any, config: dict[str, str]) -> list[tuple[str, float]] | None:
    """Plottable pairs from a serialized `SeriesResult` or `AggregateResult`.

    A calculated view already answers the question this block was written to ask
    by hand: point `source:` at a file some job wrote with
    `result.as_dict()` and the chart is the declaration's own numbers, bucket
    labels and all. `None` means "not one of those envelopes", so the literal
    JSON path below is reached exactly as before.

    `measure:` picks one of several named measures and `series:` picks one
    of several grouped lines. Both default to the first, and a name that matches
    nothing is an error rather than a silent fallback -- a chart quietly drawing
    a different measure than the one asked for is worse than a chart that says
    it could not find it.
    """
    if not isinstance(node, dict):
        return None
    wanted = config.get("measure", "").strip()

    if isinstance(node.get("rows"), list) and isinstance(node.get("measures"), list):
        measure = wanted or (node["measures"][0] if node["measures"] else "")
        if measure not in node["measures"]:
            raise _ChartError(
                f"no measure {measure!r}; this view has "
                f"{', '.join(map(str, node['measures'])) or 'none'}"
            )
        pairs = []
        for row in node["rows"]:
            value = (row.get("values") or {}).get(measure)
            if _is_num(value):
                pairs.append((str(row.get("label", "")), float(value)))
        return pairs

    if not (isinstance(node.get("buckets"), list) and isinstance(node.get("series"), list)):
        return None
    lines = [item for item in node["series"] if isinstance(item, dict)]
    if wanted:
        lines = [item for item in lines if item.get("measure") == wanted]
        if not lines:
            names = sorted(
                {str(item.get("measure")) for item in node["series"] if isinstance(item, dict)}
            )
            raise _ChartError(f"no measure {wanted!r}; this view has {', '.join(names) or 'none'}")
    label = config.get("series", "").strip()
    if label:
        lines = [item for item in lines if str(item.get("label")) == label]
        if not lines:
            raise _ChartError(f"no series labelled {label!r}")
    if not lines:
        return []
    values = lines[0].get("values") or []
    # The spine guarantees one value per bucket, so a mismatch means the file was
    # edited or truncated rather than written by `as_dict`. Say so.
    if len(values) != len(node["buckets"]):
        raise _ChartError(
            f"{len(values)} values against {len(node['buckets'])} buckets: "
            "the series and its spine disagree"
        )
    return [
        (_bucket_label(bucket, node.get("bucket")), float(value))
        for bucket, value in zip(node["buckets"], values, strict=True)
        if _is_num(value)
    ]


def _bucket_label(bucket: Any, unit: Any) -> str:
    """A bucket start as an axis label, trimmed to the width it represents.

    An ISO instant is exact and unreadable on an axis. A day bucket wants
    `2026-03-01`; anything sub-day keeps its time and drops the offset, which
    is the same for every bucket in the run and so carries no information.
    """
    text = str(bucket)
    if unit in ("year", "month", "week", "day"):
        return text[:10]
    return text[:16].replace("T", " ")


class _ChartError(ValueError):
    """A chart block that names something the data does not have."""


def _pairs(node: Any, config: dict[str, str]) -> list[tuple[str, float]]:
    envelope = _series_pairs(node, config)
    if envelope is not None:
        return envelope
    if isinstance(node, dict):
        return [(str(k), float(v)) for k, v in node.items() if _is_num(v)]
    if not isinstance(node, list):
        return []
    x = config.get("x") or "label"
    y = config.get("y") or "value"
    where_key, _, where_val = config.get("where", "").partition("=")
    aggregated: dict[str, float] = {}
    for record in node:
        if not isinstance(record, dict):
            continue
        if where_key and str(record.get(where_key.strip())) != where_val.strip():
            continue
        label, value = record.get(x), record.get(y)
        if label is None or not _is_num(value):
            continue
        # Several rows per label (trials, cpus): keep the best.
        aggregated[str(label)] = max(aggregated.get(str(label), float("-inf")), float(value))
    return list(aggregated.items())


# The wreath arms are one hue at three strengths, not three unrelated colours.
# They differ by *how much of the stack is native*, which is an ordered quantity,
# so a sequential ramp says something true that a categorical palette did not —
# the old set (brand purple, brand cyan, a fixed amber, a fixed violet) also went
# off-palette in four of the five themes because two of its four values were
# hard-coded hexes. Everything else is a muted slate with a diagonal hatch, so it
# reads as "the field" at a glance regardless of colour-blindness or theme.
_WREATH_FILL = {
    "metal": "var(--primary)",
    "native": "color-mix(in oklab, var(--primary) 68%, var(--bg))",
    "pure": "color-mix(in oklab, var(--primary) 38%, var(--bg))",
    "asgi": "color-mix(in oklab, var(--primary) 38%, var(--bg))",
    "uvicorn": "color-mix(in oklab, var(--primary) 38%, var(--bg))",
}
_OTHER_FILL = "#9aa4b2"


def _hatch_defs(uid: str) -> str:
    """The `field` hatch, with an id unique to this chart.

    An SVG `pattern` id is document-scoped, so a page carrying two charts used to
    emit `wc-hatch` twice — invalid HTML, and the second chart's bars resolve
    against the first chart's pattern. `wreath audit` reports it as a
    duplicate-id error, which is how it was found.
    """
    return (
        f'<defs><pattern id="wc-hatch-{uid}" width="7" height="7" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect width="7" height="7" fill="#9aa4b2"/>'
        '<line x1="0" y1="0" x2="0" y2="7" stroke="#6b7280" stroke-width="2.5"/>'
        "</pattern></defs>"
    )


def _bar_fill(label: str, uid: str) -> str:
    low = label.lower()
    if "wreath" in low:
        for key, color in _WREATH_FILL.items():
            if key in low:
                return color
        return "var(--primary)"
    return f"url(#wc-hatch-{uid})"


def _svg_bar(pairs: list[tuple[str, float]], title: str, unit: str) -> str:
    """One horizontal bar chart, as inline SVG that recolours with the theme.

    The type belongs to the page, not to the chart: labels take the body face,
    values take the mono face with tabular figures, and the caption takes the
    same mono micro-label every other structural heading in the theme uses. A
    chart that ships its own typography is the tell that it came from a library.
    """
    # Derived from the chart's own content, so the id is stable across builds
    # (a counter would renumber every chart when one is inserted above it).
    uid = sha256(f"{title}\x00{unit}\x00{pairs}".encode()).hexdigest()[:8]
    width, label_w, value_w, row_h = 720, 168, 84, 30
    bar_area = width - label_w - value_w - 12
    top = 12
    height = top + len(pairs) * row_h + 6
    top_value = max((v for _, v in pairs), default=1.0) or 1.0
    parts = [
        '<figure class="chart">',
        f'<figcaption class="chart-title">{_esc(title)}</figcaption>' if title else "",
        f'<svg viewBox="0 0 {width} {height}" role="img" width="100%" style="max-width:{width}px">',
        _hatch_defs(uid),
        # The baseline every bar starts from. Without it the bars float and the
        # eye has nothing to judge the left edge against.
        f'<line x1="{label_w - 0.5}" y1="{top - 2}" x2="{label_w - 0.5}" '
        f'y2="{height - 4}" stroke="currentColor" opacity=".18"/>',
    ]
    for index, (label, value) in enumerate(pairs):
        cy = top + index * row_h
        mid = cy + row_h / 2
        bar_w = max(2.0, value / top_value * bar_area)
        is_wreath = "wreath" in label.lower()
        weight = "600" if is_wreath else "400"
        parts.append(
            f'<text class="chart-label" x="{label_w - 10}" y="{mid + 4:.0f}" '
            f'text-anchor="end" font-weight="{weight}" fill="currentColor">'
            f"{_esc(label)}</text>"
            f'<rect x="{label_w}" y="{cy + 4}" width="{bar_w:.1f}" height="{row_h - 11}" '
            f'rx="2" fill="{_bar_fill(label, uid)}"/>'
            f'<text class="chart-value" x="{label_w + bar_w + 8:.1f}" y="{mid + 4:.0f}" '
            f'fill="currentColor" font-weight="{weight}">'
            f"{_fmt(value)}{_esc(unit)}</text>"
        )
    parts.append("</svg></figure>")
    return "".join(parts)


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _is_num(value: Any) -> TypeIs[int | float]:
    """A plottable number. `TypeIs` so callers narrow before `float(...)`.

    `bool` is excluded deliberately: `True` is an `int` and would plot as
    a bar of height one, which is never what a flag in the data meant.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default
