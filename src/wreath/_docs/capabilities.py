"""The capability map — the `::: capability-map` directive.

`docs/capabilities.md` answers one question for someone still deciding whether
to use Wreath: *what do I no longer install?* The answer runs to three dozen
rows and it moves every time a module lands, which makes a hand-written table
exactly the kind of map this repository has already watched rot once.

So the page carries no table. It carries this directive, and the table is minted
at build time from `docs/agents/manifest.json` — the file `AGENTS.md` already
requires a new module to update in the same change, and the file
`uv run wreath-map-lint` already refuses to let cite a path that is not there. A
subsystem reaches the capability map by describing itself in the manifest, and
its links are the `guides` it had to list anyway.

Two fields carry the prose:

* `capability` — the human sentence for the map, or `null` for a subsystem that
  is implementation rather than a user-facing surface (`devtools`, `example`).
* `replaces` — the distribution names a reader would recognise. They are
  vocabulary, not a comparison: they exist to locate a capability in the words
  the reader already uses, and nothing here claims anything about them.

Everything else in a row is derived, which is what keeps the page honest. The
"In Wreath" column is the subsystem's own public modules, read from `sources`,
so it cannot name a module that is not in the tree; the guide links are the
subsystem's own `guides`, so the docs build's dead-link check holds them to the
same standard as any hand-written link.

`aliases` reads the same `replaces` names the other way round, as the reverse
index: a reader who arrives already knowing the word `celery` types it into the
site search, and the page whose whole job is to answer that should be the first
result rather than the one that happens to mention it in a table cell. Only
`replaces` — the capability sentences are the page's own prose and already
indexed as prose, and promoting them would let the map outrank the guides it
exists to point at.

They ride in the search index in **their own field**, beside the page's
front-matter `keywords` rather than merged into it, because they are weaker
evidence and `scripts.py` scores them accordingly: an alias is a third-party
name a page happens to list, not a term the page claims about itself. Merged,
they were equals, and the English words that are also package names showed it —
`limits` and `arrow` are real distributions, and a search for either outranked
`RequestLimits` and the guide about porting off `arrow` with a capability map.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "MANIFEST", "alias_text", "aliases", "expand", "has_directive", "public_modules",
    "table",
]

#: The manifest, relative to the docs source directory (it lives under `docs/`
#: but is excluded from the nav — it is data, not a page).
MANIFEST = "agents/manifest.json"

_DIRECTIVE = re.compile(r"^:::\s+capability-map\s*$")
_H1 = re.compile(r"^#\s+(.*?)\s*#*\s*$", re.MULTILINE)

_HEADER = (
    "| Capability | Elsewhere you'd install | In Wreath | Guide |",
    "|---|---|---|---|",
)

#: What an empty column says. "built in" is the honest answer for a capability
#: that has no module of its own — advisory locks are a method on the Postgres
#: connection, not a `wreath.locks` — and an em dash is the answer for "nobody
#: ships a package for this".
_NO_PACKAGES = "—"
_NO_MODULES = "built in"


def has_directive(source: str) -> bool:
    return any(_DIRECTIVE.match(line) for line in source.splitlines())


def expand(
    source: str, source_dir: Path, page: str = "", sink: list[str] | None = None,
) -> str:
    """Replace each `::: capability-map` line with the generated table.

    Like the `:::` reference directive, a failure renders an inline note so a
    non-strict local preview still builds and shows the problem on the page, and
    *also* reports to `sink` so a strict build fails rather than publishing a
    page whose body is an apology.
    """
    lines = source.splitlines()
    if not any(_DIRECTIVE.match(line) for line in lines):
        return source
    where = f"{page}: " if page else ""
    try:
        manifest = json.loads((source_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if sink is not None:
            sink.append(f"{where}::: capability-map cannot read {MANIFEST}: {error}")
        rendered = f"> **Capability map unavailable:** cannot read `{MANIFEST}`."
    else:
        rendered, problems = table(manifest, source_dir, page)
        if sink is not None:
            sink.extend(f"{where}::: capability-map {problem}" for problem in problems)
    return "\n".join(
        rendered if _DIRECTIVE.match(line) else line for line in lines)


def alias_text(source_dir: Path) -> str:
    """Every name on the map, as the one string the search index carries.

    A manifest that cannot be read yields no aliases rather than an error --
    `expand` has already turned that into a build failure, and a second copy of
    the same failure on the same page is noise.
    """
    try:
        manifest = json.loads((source_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return ", ".join(aliases(manifest))


def aliases(manifest: dict) -> list[str]:
    """Every distribution name the map names, in manifest order, without repeats.

    A subsystem held off the map by `"capability": null` contributes nothing: a
    reader who searched for its package would arrive at a page with no row for
    it, which is a worse answer than the search having missed.
    """
    names: list[str] = []
    seen: set[str] = set()
    for subsystem in manifest.get("subsystems", []):
        if subsystem.get("capability") is None:
            continue
        for entry in subsystem.get("replaces") or ():
            name = str(entry).strip().lower()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def table(
    manifest: dict, source_dir: Path, page: str = "",
) -> tuple[str, list[str]]:
    """The capability table as markdown, plus everything wrong with it.

    Rows come out in manifest order, which is the order the subsystems were
    written down in and already runs roughly the way the nav does: handling a
    request, then data, then auth, then background work, then operations.
    """
    #: Guide links are written relative to the page holding the directive.
    prefix = "../" * page.count("/")
    rows: list[str] = []
    problems: list[str] = []
    for index, subsystem in enumerate(manifest.get("subsystems", [])):
        name = subsystem.get("name", f"subsystems[{index}]")
        if "capability" not in subsystem:
            problems.append(
                f"subsystem {name!r} has no `capability`: add the sentence it belongs"
                " on the map with, or `null` to say it is internal")
            continue
        capability = subsystem["capability"]
        if capability is None:
            continue
        guides, missing = _links(subsystem.get("guides", ()), source_dir, prefix)
        problems.extend(
            f"{name}: guide {path!r} does not exist" for path in missing)
        rows.append(
            f"| {_cell(str(capability))} | {_packages(subsystem.get('replaces') or ())}"
            f" | {_modules(subsystem.get('sources', ()))} | {guides or _NO_PACKAGES} |")
    return "\n".join([*_HEADER, *rows]), problems


def _cell(text: str) -> str:
    """A pipe would split the cell in two; nothing here wants one."""
    return text.replace("|", "/").strip()


def _packages(names: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"`{_cell(name)}`" for name in names) or _NO_PACKAGES


def public_modules(sources: tuple[str, ...] | list[str]) -> list[str]:
    """The subsystem's public modules, in the order it lists its sources.

    Only top-level public names under `src/wreath`: `orm/` is `wreath.orm`, but
    `_native/postgres/model.c` is how the ORM is built rather than what a reader
    imports, and naming it here would be showing somebody the machine room.

    Both the rendered table and the shipped capability index
    (`wreath._capability_data`) derive their module column from here, so the two
    cannot disagree about what a subsystem exposes.
    """
    modules: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not source.startswith("src/wreath/"):
            continue
        trimmed = source.removeprefix("src/wreath/").rstrip("/")
        if "/" in trimmed:
            continue
        name = trimmed.removesuffix(".py")
        if not name or name.startswith("_"):
            continue
        rendered = f"wreath.{name}"
        if rendered not in seen:
            seen.add(rendered)
            modules.append(rendered)
    return modules


def _modules(sources: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"`{name}`" for name in public_modules(sources)) or _NO_MODULES


def _links(
    guides: tuple[str, ...] | list[str], source_dir: Path, prefix: str,
) -> tuple[str, list[str]]:
    """Markdown links to a subsystem's guides, titled by each page's own `# H1`.

    A guide outside the docs tree — `benchmarks/README.md`, which the
    performance subsystem lists — is not a page of this site, so it is skipped
    rather than linked into a 404.
    """
    root = f"{source_dir.name}/"
    parts: list[str] = []
    missing: list[str] = []
    for guide in guides:
        if not guide.startswith(root):
            continue
        relative = guide.removeprefix(root)
        path = source_dir / relative
        if not path.is_file():
            missing.append(guide)
            continue
        parts.append(f"[{_cell(_title(path, relative))}]({prefix}{relative})")
    return ", ".join(parts), missing


def _title(path: Path, relative: str) -> str:
    match = _H1.search(path.read_text(encoding="utf-8"))
    if match is None:
        return relative
    # The heading as words: a guide titled "`wreath.orm`" would otherwise put
    # backticks inside a link label, where they render as backticks.
    return match.group(1).replace("`", "").replace("*", "")
