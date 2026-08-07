"""The dependency plate — a ``` ``plate ```` block.

The home page's opener, and the one argument the docs make before they start
teaching. Wreath's claim is not that it is fast or small; it is that a normal
Python service assembles a couple of dozen packages to do what this one does,
and the honest way to say that is to *show the list*.

    ```plate
    caption: One package. No runtime dependencies.
    title: Everything here is something you no longer install.
    lede: Wreath ships each of these as a module named after the thing it does.
    action: What you don't have to install -> capabilities.md
    ```

The names are not written in the block. They are read from
`docs/agents/manifest.json` — the same `replaces` fields the capability map
already renders as a table, and the same file `AGENTS.md` requires a new module
to update in the same change. A hand-typed list of this length would be wrong
within a month, and a *marketing* list that is wrong is worse than none.

**Why a block of struck-through names rather than the number.** The number is
available and the block prints it, but "163 packages" is a claim a reader
either believes or does not. The list is checkable: it contains `celery` and
`sqlalchemy` and `uvicorn`, and a reader who runs those will recognise their own
`requirements.txt` and can go and see which module replaced each one. Set small
and dense, it also *looks* like the engraving the project is named after —
close up it is a list, at arm's length it is cross-hatch.

The names are plain text, deliberately, though each one does have a guide behind
it. A hundred and sixty-three links in the first screenful is a hundred and
sixty-three tab stops before a keyboard reader reaches the prose, and the
underlines would destroy the texture that makes the block read as a plate. One
action goes to the capability map, which is where the per-package mapping
already lives and is already link-checked.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import _fenced
from .capabilities import MANIFEST, aliases

__all__ = ["extract", "restore", "title_of"]

_OPEN = "```plate"


def title_of(tokens: dict[str, str]) -> str:
    """The headline of the first plate, for the page `<title>`."""
    return _fenced.title_of(tokens, "plate-title")


def extract(
    text: str, source_dir: Path, sink: list[str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Replace each ```plate block with a token; return (text, {token: html})."""
    return _fenced.extract(
        text, _OPEN, lambda body: _render(body, source_dir, sink), "PLATE")


restore = _fenced.restore


def _render(config: list[str], source_dir: Path, sink: list[str] | None) -> str:
    fields: dict[str, str] = {}
    actions: list[tuple[str, str]] = []
    for line in config:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "action":
            label, arrow, href = value.partition("->")
            if arrow:
                actions.append((label.strip(), href.strip()))
        else:
            fields[key] = value

    names = _names(source_dir, sink)
    parts = ['<section class="plate" aria-labelledby="plate-title">']
    if caption := fields.get("caption"):
        parts.append(f'<p class="plate-caption">{_esc(caption)}</p>')
    # An `<h1>`: this is the page's real title, so it belongs in the outline and
    # the table of contents, exactly as a hero's headline does.
    if title := fields.get("title"):
        parts.append(f'<h1 class="plate-title" id="plate-title">{_esc(title)}</h1>')
    if lede := fields.get("lede"):
        parts.append(f'<p class="plate-lede">{_esc(lede)}</p>')

    if names:
        # Alphabetical, and all of them. Manifest order groups by subsystem,
        # which reads well until the list is trimmed -- a prefix then drops
        # whole subsystems off the end rather than thinning the block evenly,
        # and the reader cannot tell which. Printing every name removes the
        # question, and sorting turns the block into something a reader can
        # actually use: they come here to look for the packages *they* run.
        #
        # The names carry no `<s>` element. The strike is drawn in CSS on the
        # list item instead, and the meaning is carried by the label below,
        # which a screen reader announces once -- where a hundred and fifty-five
        # `<s>` elements announce their own strikethrough a hundred and
        # fifty-five times.
        items = "".join(f"<li>{_esc(name)}</li>" for name in sorted(names))
        parts.append(
            f'<ul class="plate-names" aria-label="Packages a Wreath application '
            f'does not install">{items}</ul>')
        parts.append(
            f'<p class="plate-count"><strong>{len(names)}</strong> packages and '
            f"standard-library modules, read "
            f"from the subsystem manifest when this page was built \u2014 so the "
            f"list cannot drift from what Wreath actually ships. Every one of them "
            f"maps to a module on the capability map.</p>")
    if actions:
        links = "".join(
            f'<a class="plate-action{" primary" if index == 0 else ""}" '
            f'href="{_esc(href)}">{_esc(label)}</a>'
            for index, (label, href) in enumerate(actions))
        parts.append(f'<p class="plate-actions">{links}</p>')
    parts.append("</section>")
    return "".join(parts)


def _names(source_dir: Path, sink: list[str] | None) -> list[str]:
    """Every package the manifest says a subsystem replaces, deduplicated.

    A manifest that cannot be read is a build failure through `sink`, not a
    silently shorter list: the block's whole claim is the length of it, and a
    plate that quietly printed nothing would still look deliberate.
    """
    try:
        manifest = json.loads((source_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if sink is not None:
            sink.append(f"```plate cannot read {MANIFEST}: {error}")
        return []
    found = aliases(manifest)
    if not found and sink is not None:
        sink.append(f"```plate found no `replaces` entries in {MANIFEST}")
    return found



def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


