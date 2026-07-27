"""Build-time charts from external JSON — a ```chart fenced block.

Point a page at a JSON file (a benchmark's ``latest.json``, any data you already
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

The SVG uses ``currentColor`` for text and ``var(--primary)`` for bars, so it
recolors with the active theme and light/dark automatically.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeIs

__all__ = ["extract", "restore"]

_OPEN = "```chart"


def extract(
    text: str, base_dir: Path, sources: set[Path] | None = None,
) -> tuple[str, dict[str, str]]:
    """Replace each ```chart block with a token; return (text, {token: svg-html}).

    Every data file a chart successfully reads is added to ``sources`` (if given),
    so the caller can publish the raw JSON alongside the rendered chart.
    """
    lines = text.splitlines()
    out: list[str] = []
    tokens: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if lines[i].strip() == _OPEN:
            i += 1
            config: list[str] = []
            while i < len(lines) and lines[i].strip() != "```":
                config.append(lines[i])
                i += 1
            i += 1                                  # consume the closing fence
            token = f"\x00CHART{len(tokens)}\x00"
            tokens[token] = _render(_parse(config), base_dir, sources)
            out.append(token)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out), tokens


def restore(html: str, tokens: dict[str, str]) -> str:
    """Swap chart tokens (as rendered by the markdown pass) for their SVG."""
    for token, svg in tokens.items():
        html = html.replace(f"<p>{token}</p>", svg).replace(token, svg)
    return html


def _parse(config: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in config:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _render(config: dict[str, str], base_dir: Path, sources: set[Path] | None = None) -> str:
    source = config.get("source", "")
    path = base_dir / source
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return (f'<div class="chart-error">chart: cannot load {_esc(source)}: '
                f"{_esc(str(error))}</div>")
    if sources is not None:
        sources.add(path.resolve())
    node: Any = data
    for key in config.get("data", "").split("."):
        if key:
            try:
                node = node[key]
            except (KeyError, TypeError, IndexError):
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
    """Plottable pairs from a serialized ``SeriesResult`` or ``AggregateResult``.

    A calculated view already answers the question this block was written to ask
    by hand: point ``source:`` at a file some job wrote with
    ``result.as_dict()`` and the chart is the declaration's own numbers, bucket
    labels and all. ``None`` means "not one of those envelopes", so the literal
    JSON path below is reached exactly as before.

    ``measure:`` picks one of several named measures and ``series:`` picks one
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
            raise _ChartError(f"no measure {measure!r}; this view has "
                              f"{', '.join(map(str, node['measures'])) or 'none'}")
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
            names = sorted({str(item.get("measure")) for item in node["series"]
                            if isinstance(item, dict)})
            raise _ChartError(f"no measure {wanted!r}; this view has "
                              f"{', '.join(names) or 'none'}")
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
    ``2026-03-01``; anything sub-day keeps its time and drops the offset, which
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


# The wreath arms each get a distinct, theme-aware fill so they never blend into
# the competitor bars; everything else is a muted slate with a diagonal hatch, so
# it reads as "the field" at a glance regardless of colour-blindness or theme.
_WREATH_FILL = {
    "metal": "var(--primary)",
    "native": "var(--accent)",
    "pure": "#f59e0b",
    "asgi": "#8b5cf6",
    "uvicorn": "#8b5cf6",
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
        "</pattern></defs>")


def _bar_fill(label: str, uid: str) -> str:
    low = label.lower()
    if "wreath" in low:
        for key, color in _WREATH_FILL.items():
            if key in low:
                return color
        return "var(--primary)"
    return f"url(#wc-hatch-{uid})"


def _svg_bar(pairs: list[tuple[str, float]], title: str, unit: str) -> str:
    # Derived from the chart's own content, so the id is stable across builds
    # (a counter would renumber every chart when one is inserted above it).
    uid = sha256(
        f"{title}\x00{unit}\x00{pairs}".encode()).hexdigest()[:8]
    width, label_w, value_w, row_h = 720, 172, 82, 32
    bar_area = width - label_w - value_w - 12
    top = 36 if title else 8
    height = top + len(pairs) * row_h + 8
    top_value = max((v for _, v in pairs), default=1.0) or 1.0
    parts = [
        f'<figure class="chart"><svg viewBox="0 0 {width} {height}" '
        f'role="img" width="100%" style="max-width:{width}px">', _hatch_defs(uid)]
    if title:
        parts.append(
            f'<text x="0" y="21" font-weight="700" font-size="15" '
            f'fill="currentColor">{_esc(title)}</text>')
    for index, (label, value) in enumerate(pairs):
        cy = top + index * row_h
        mid = cy + row_h / 2
        bar_w = max(2.0, value / top_value * bar_area)
        is_wreath = "wreath" in label.lower()
        weight = "700" if is_wreath else "400"
        parts.append(
            f'<text x="{label_w - 8}" y="{mid + 4:.0f}" text-anchor="end" font-weight="{weight}" '
            f'fill="currentColor" font-size="13">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{cy + 4}" width="{bar_w:.1f}" height="{row_h - 10}" '
            f'rx="3" fill="{_bar_fill(label, uid)}"/>'
            f'<text x="{label_w + bar_w + 6:.1f}" y="{mid + 4:.0f}" fill="currentColor" '
            f'font-size="12" font-weight="{weight}" '
            f'opacity="0.9">{_fmt(value)}{_esc(unit)}</text>')
    parts.append("</svg></figure>")
    return "".join(parts)


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _is_num(value: Any) -> TypeIs[int | float]:
    """A plottable number. ``TypeIs`` so callers narrow before ``float(...)``.

    ``bool`` is excluded deliberately: ``True`` is an ``int`` and would plot as
    a bar of height one, which is never what a flag in the data meant.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default
