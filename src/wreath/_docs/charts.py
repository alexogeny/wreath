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
from pathlib import Path
from typing import Any

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
    pairs = _pairs(node, config)
    if not pairs:
        return '<div class="chart-error">chart: no plottable numeric data</div>'
    if config.get("sort") == "desc":
        pairs.sort(key=lambda p: p[1], reverse=True)
    elif config.get("sort") == "asc":
        pairs.sort(key=lambda p: p[1])
    limit = _int(config.get("limit"), len(pairs))
    return _svg_bar(pairs[:limit], config.get("title", ""), config.get("unit", ""))


def _pairs(node: Any, config: dict[str, str]) -> list[tuple[str, float]]:
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

_HATCH_DEFS = (
    '<defs><pattern id="wc-hatch" width="7" height="7" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(45)"><rect width="7" height="7" fill="#9aa4b2"/>'
    '<line x1="0" y1="0" x2="0" y2="7" stroke="#6b7280" stroke-width="2.5"/></pattern></defs>')


def _bar_fill(label: str) -> str:
    low = label.lower()
    if "wreath" in low:
        for key, color in _WREATH_FILL.items():
            if key in low:
                return color
        return "var(--primary)"
    return "url(#wc-hatch)"


def _svg_bar(pairs: list[tuple[str, float]], title: str, unit: str) -> str:
    width, label_w, value_w, row_h = 720, 172, 82, 32
    bar_area = width - label_w - value_w - 12
    top = 36 if title else 8
    height = top + len(pairs) * row_h + 8
    top_value = max((v for _, v in pairs), default=1.0) or 1.0
    parts = [
        f'<figure class="chart"><svg viewBox="0 0 {width} {height}" '
        f'role="img" width="100%" style="max-width:{width}px">', _HATCH_DEFS]
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
            f'rx="3" fill="{_bar_fill(label)}"/>'
            f'<text x="{label_w + bar_w + 6:.1f}" y="{mid + 4:.0f}" fill="currentColor" '
            f'font-size="12" font-weight="{weight}" '
            f'opacity="0.9">{_fmt(value)}{_esc(unit)}</text>')
    parts.append("</svg></figure>")
    return "".join(parts)


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default
