"""Tier-3 auto-remediation (`--fix`).

Structurally-safe, semantics-preserving fixes applied by **byte-offset splicing** at the
parser's element positions — never re-serialising the document, so CSP nonces, formatting
and comments survive untouched. Only the safe subset from design 08 §5 is applied
(inject `lang`, add `alt=""`, add `th` `scope`, clamp a positive `tabindex`,
strip a zoom-disabling viewport); everything else stays suggestion-only. For source-owned
*generated* HTML (the API-docs shell) the CLI presents these as patch suggestions rather
than editing rendered bytes, since that artefact is rebuilt each run.
"""
from __future__ import annotations

import re

from .dom import parse_html


class _Text:
    """Offset index + ordered, non-overlapping edit buffer over one HTML source."""

    def __init__(self, html: str) -> None:
        self.html = html
        self._starts = [0]
        for i, ch in enumerate(html):
            if ch == "\n":
                self._starts.append(i + 1)
        self._edits: list[tuple[int, int, str]] = []

    def off(self, line: int, col: int) -> int:
        return self._starts[line - 1] + col

    def tag_close(self, start: int) -> int:
        """Index of the `>` that closes the tag opening at `start` (quote-aware)."""
        quote = ""
        i = start
        while i < len(self.html):
            ch = self.html[i]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == ">":
                return i
            i += 1
        return len(self.html)

    def insert(self, at: int, text: str) -> None:
        self._edits.append((at, at, text))

    def replace(self, start: int, end: int, text: str) -> None:
        self._edits.append((start, end, text))

    def build(self) -> str:
        out, last = [], 0
        for start, end, text in sorted(self._edits):
            if start < last:
                continue  # drop an overlapping edit rather than corrupt the output
            out.append(self.html[last:start])
            out.append(text)
            last = end
        out.append(self.html[last:])
        return "".join(out)


_FIXABLE = ("html-lang", "img-alt", "table-headers", "tabindex", "viewport-scale")


def apply_fixes(html: str) -> tuple[str, list[str]]:
    """Return `(fixed_html, applied)` — the safe-subset remediations spliced in."""
    root = parse_html(html)
    tx = _Text(html)
    applied: list[str] = []

    for node in root.walk():
        start = tx.off(node.line, node.col)
        attr_point = start + 1 + len(node.tag)

        # A positive tabindex / a zoom-locked viewport need a whole-tag rewrite; do that
        # instead of (never as well as) an attribute insert on the same element.
        raw = node.attr("tabindex")
        if raw and raw.lstrip("-").isdigit() and int(raw) > 0:
            close = tx.tag_close(start)
            tag = html[start:close + 1]
            tag = re.sub(r'tabindex\s*=\s*"[^"]*"', 'tabindex="0"', tag, count=1)
            tag = re.sub(r"tabindex\s*=\s*'[^']*'", "tabindex='0'", tag, count=1)
            tx.replace(start, close + 1, tag)
            applied.append(f"tabindex ({node.loc}): clamped positive tabindex to 0")
            continue
        if node.tag == "meta" and (node.attr("name") or "").lower() == "viewport":
            content = node.attr("content") or ""
            stripped = re.sub(
                r"\s*,?\s*(user-scalable\s*=\s*no|maximum-scale\s*=\s*[0-9.]+)",
                "", content, flags=re.I,
            ).strip(", ")
            if stripped != content:
                close = tx.tag_close(start)
                tx.replace(start, close + 1, html[start:close + 1].replace(content, stripped))
                applied.append(f"viewport-scale ({node.loc}): removed the zoom restriction")
            continue

        if node.tag == "html" and not (node.attr("lang") or "").strip():
            tx.insert(attr_point, ' lang="en"')
            applied.append(f'html-lang ({node.loc}): added lang="en"')
        elif node.tag == "img" and not node.has_attr("alt"):
            tx.insert(attr_point, ' alt=""')
            applied.append(f'img-alt ({node.loc}): added alt="" (review: describe the image)')
        elif node.tag == "th" and not node.has_attr("scope"):
            tx.insert(attr_point, ' scope="col"')
            applied.append(f'table-headers ({node.loc}): added scope="col"')

    return tx.build(), applied
