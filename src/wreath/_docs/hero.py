"""The page opener — a ``` ``hero ```` block.

Front matter for a page that has an argument to make rather than an API to
document. It renders one band: a mono eyebrow naming what the page is organised
by, a display headline, a lede, and up to a few actions.

    ```hero
    eyebrow: The request path
    title: Most of a request never reaches Python.
    lede: Ingress, routing, and authorization are native code.
    action: Read the benchmarks -> ../perf/index.md
    action: The timer story -> ../explorations/the-timer.md
    ```

Deliberately not a template language. A hero is the one place a docs page is
allowed to be loud, and the way to keep that from spreading is to give it four
fields and no way to add a fifth. Action targets go through the same `.md` ->
`.html` rewrite and the same dead-link check as any other link on the page,
because a hero that quietly links to a page you deleted is worse than no hero.
"""

from __future__ import annotations

from . import _fenced

__all__ = ["extract", "restore", "title_of"]

_OPEN = "```hero"
#: Actions are the only repeatable field, so they are the only one parsed as a
#: list. Four is not a limit anyone will hit -- it is the point at which a hero
#: has stopped choosing.
_MAX_ACTIONS = 4
_MAX_SIGNALS = 6


def title_of(tokens: dict[str, str]) -> str:
    """The headline of the first hero, for the page `<title>`."""
    return _fenced.title_of(tokens, "hero-title")


def extract(text: str) -> tuple[str, dict[str, str]]:
    """Replace each ```hero block with a token; return (token, markup) pairs."""
    return _fenced.extract(text, _OPEN, _render, "HERO")


restore = _fenced.restore


def _render(config: list[str]) -> str:
    fields: dict[str, str] = {}
    actions: list[tuple[str, str]] = []
    signals: list[str] = []
    for line in config:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "action":
            label, arrow, href = value.partition("->")
            if arrow and len(actions) < _MAX_ACTIONS:
                actions.append((label.strip(), href.strip()))
        elif key == "signal" and len(signals) < _MAX_SIGNALS:
            signals.append(value)
        else:
            fields[key] = value

    wide = fields.get("wide", "").lower() in {"1", "true", "yes"}
    parts = [f'<div class="hero{" wide" if wide else ""}">']
    if eyebrow := fields.get("eyebrow"):
        parts.append(f'<p class="hero-eyebrow">{_esc(eyebrow)}</p>')
    # The headline is an `<h1>`, not a styled paragraph: it is the page's real
    # title, so it belongs in the outline, the table of contents, and the tab.
    if title := fields.get("title"):
        parts.append(f'<h1 class="hero-title" id="{_slug(title)}">{_esc(title)}</h1>')
    if lede := fields.get("lede"):
        parts.append(f'<p class="hero-lede">{_esc(lede)}</p>')
    if signals:
        parts.append(
            '<div class="hero-signals">'
            + "".join(f"<span>{_esc(signal)}</span>" for signal in signals)
            + "</div>"
        )
    if actions:
        links = "".join(
            f'<a class="hero-action{" primary" if index == 0 else ""}" '
            f'href="{_esc(href)}">{_esc(label)}</a>'
            for index, (label, href) in enumerate(actions)
        )
        parts.append(f'<p class="hero-actions">{links}</p>')
    parts.append("</div>")
    return "".join(parts)


def _slug(text: str) -> str:
    from .markdown import slugify

    return slugify(text) or "top"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
