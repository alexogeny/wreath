"""Linked editorial cards for a story-led landing page."""

from __future__ import annotations

from . import _fenced
from .markdown import _safe_href

__all__ = ["extract", "restore"]

_OPEN = "```cards"
_MAX_CARDS = 12


def extract(text: str) -> tuple[str, dict[str, str]]:
    return _fenced.extract(text, _OPEN, _render, "CARDS")


restore = _fenced.restore


def _render(config: list[str]) -> str:
    label = "Explore"
    wide = False
    entries: list[tuple[str, str, str, str]] = []
    for line in config:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "label":
            label = value
        elif key == "wide":
            wide = value.lower() in {"1", "true", "yes"}
        elif key == "card" and len(entries) < _MAX_CARDS:
            parts = tuple(part.strip() for part in value.split("|", 3))
            if len(parts) == 4 and all(parts[:3]):
                entries.append(parts)

    cards = []
    for index, (title, description, href, meta) in enumerate(entries, 1):
        cards.append(
            f'<a class="story-card" href="{_safe_href(href)}">'
            f'<span class="story-index">{index:02}</span>'
            f"<h2>{_esc(title)}</h2><p>{_esc(description)}</p>"
            f'<span class="story-meta">{_esc(meta)}</span>'
            '<span class="story-arrow" aria-hidden="true">&#8599;</span></a>'
        )
    css = "story-grid wide" if wide else "story-grid"
    return f'<section class="{css}" aria-label="{_esc(label)}">{"".join(cards)}</section>'


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
