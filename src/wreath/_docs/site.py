"""Build a `Site` into a static HTML tree.

The Python facade owns everything except the per-file markdown parse: page
collection from the nav, output-path mapping, relative-link resolution, the
nav/TOC assembly, strict orphan/dead-link checks, and all file I/O. The result is
a plain directory of self-contained HTML you can serve with wreath's hardened
`StaticFiles`.

**Every markdown page under the source tree is link-checked, in the nav or not.**
Reachability decides what gets *written*, never what gets *verified*: an orphan
is warned about as an orphan and then held to the same link and anchor rules as
anything else, because a page that is not in the nav yet is precisely the page
whose links nobody has read.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
from pathlib import Path

from . import (
    apidoc,
    capabilities,
    charts,
    codeblocks,
    figures,
    hero,
    markdown,
    plate,
    repo,
    scripts,
    search,
    theme,
)
from .config import Page, Section, Site

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_CSS_PATH = "assets/docs.css"
_JS_PATH = "assets/docs.js"
_INTERNAL_MD = re.compile(r'href="([^"#:]+)\.md(#[^"]*)?"')
_ANCHOR = re.compile(r'href="#([^"]+)"')
_FM_DESC = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
_FM_KEYWORDS = re.compile(r"^keywords:\s*(.+?)\s*$", re.MULTILINE)
_FM_BOOST = re.compile(r"^boost:\s*([0-9.]+)\s*$", re.MULTILINE)
#: Where one indexable section of a page starts. Only h2/h3 — an h4 is a label
#: inside a section, not a destination somebody searches for.
_SECTION_SPLIT = re.compile(r'<h([23]) id="([^"]+)"')
#: How much prose to keep per section. It is both the ranking signal and the
#: result snippet, so it wants the topic sentence and nothing after it.
_SECTION_CHARS = 280
#: The heading a section opens with, and the `#` permalink every heading carries.
_OWN_HEADING = re.compile(r"^<h[1-6][^>]*>.*?</h[1-6]>", re.DOTALL)
_PERMALINK = re.compile(r'<a class="anchor".*?</a>', re.DOTALL)
#: The dependency plate's list of package names, which is *shown* on the home
#: page and must not be *indexed* there. Those names are the capability map's
#: vocabulary: `capabilities.py` deliberately scores them as a low-weight alias
#: field so the page that maps them outranks any page that merely mentions
#: them, and a home page carrying all hundred and fifty-five as ordinary prose
#: would beat the map at its own job -- with a search snippet that is a run of
#: package names and no sentence.
_PLATE_NAMES = re.compile(r'<ul class="plate-names".*?</ul>', re.DOTALL)


@dataclass(frozen=True, slots=True)
class BuildReport:
    pages: int
    output: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _output_path(source: str) -> str:
    return source[:-3] + ".html"       # "guides/routing.md" -> "guides/routing.html"


@cache
def _relative_to(directory: str, to_output: str) -> str:
    """Relative href from a page *in* `directory` to `to_output`."""
    return posixpath.relpath(to_output, start=directory) or "."


def _relative(from_output: str, to_output: str) -> str:
    """Relative href from one output page to another (or an asset).

    Memoised on the *directory* rather than the page, because that is what the
    answer depends on: every page under `guides/` reaches `assets/docs.css` by
    the same `../assets/docs.css`. The nav asks this once per page per nav
    entry, so a 364-page tree over 19 directories asks 134,680 times for 6,916
    distinct answers, and `posixpath.relpath` is ~5us of splitting and
    rejoining. That one line was 700ms of a 3.3s build.

    Nothing can go stale: the answer is a function of two path strings and of
    nothing on disk, so a cache that outlives a build (`docs serve` rebuilds on
    every keystroke) is still correct on the next one.
    """
    return _relative_to(posixpath.dirname(from_output), to_output)


def _first_page(item: Page | Section) -> Page | None:
    """The page a nav entry lands on when it is clicked as a whole."""
    if isinstance(item, Page):
        return item
    for child in item.items:
        found = _first_page(child)
        if found is not None:
            return found
    return None


def _holds(item: Page | Section, current: str) -> bool:
    """Does this nav subtree contain the page currently being rendered?"""
    if isinstance(item, Page):
        return _output_path(item.source) == current
    return any(_holds(child, current) for child in item.items)


def _render_items(
    items: tuple[Page | Section, ...], current: str, depth: int,
) -> tuple[str, bool]:
    """One nav level as HTML, plus whether it holds the current page.

    The `on-path` flag is what draws the thread: every level between the root of
    the sidebar and the page you are on lights its rail, so "you are here"
    carries its ancestry instead of being an isolated highlight.
    """
    parts: list[str] = []
    on_path = False
    for item in items:
        if isinstance(item, Page):
            target = _output_path(item.source)
            active = target == current
            on_path = on_path or active
            css = "nav-page active" if active else "nav-page"
            aria = ' aria-current="page"' if active else ""
            parts.append(f'<a class="{css}" href="{_esc(_relative(current, target))}"{aria}>'
                         f"{_esc(item.title)}</a>")
        else:
            inner, inner_path = _render_items(item.items, current, depth + 1)
            on_path = on_path or inner_path
            parts.append(
                f'<details class="sec sec-{depth}{" on-path" if inner_path else ""}"'
                f'{" open" if inner_path else ""}>'
                f"<summary>{_esc(item.title)}</summary>{inner}</details>")
    # The root level draws no rail, so marking it would be a class that styles
    # nothing and reads, wrongly, as "the thread starts here".
    css = f"lvl lvl-{depth}{' on-path' if on_path and depth else ''}"
    return f'<div class="{css}">{"".join(parts)}</div>', on_path


def _nav_context(site: Site, current: str) -> tuple[str, str, str, str]:
    """The section menu, the sidebar tree, the section's name, and its landing href.

    With sections on, the top level of the nav moves into the header's section
    switcher and the sidebar shows only the section you are inside. A 237-page
    tree in one scroller is a list; split at the top level it is a structure you
    can hold in your head.

    The third value is what the switcher shows when it is closed. It is the
    whole reason the control can replace a row of tabs: a tab row communicates
    "where am I" by underlining one of twelve, which only works if all twelve
    are on screen, and they never were.

    The fourth is where that section *starts*, which the sidebar heads itself
    with. Without it the only route from a recipe back to the cookbook's own
    index was a nav entry labelled "Overview", with nothing on the page saying
    which section "Overview" belonged to -- so the way back existed and did not
    read as one.
    """
    if not site.use_tabs():
        side, _ = _render_items(site.nav.items, current, 0)
        return "", side, "", ""

    entries: list[str] = []
    side = ""
    here = ""
    landing_href = ""
    for item in site.nav.items:
        landing = _first_page(item)
        if landing is None:
            continue
        active = _holds(item, current)
        href = _relative(current, _output_path(landing.source))
        entries.append(f'<a href="{_esc(href)}"{" class=\"active\"" if active else ""}'
                       f'{" aria-current=\"true\"" if active else ""}>{_esc(item.title)}</a>')
        if active:
            here = item.title
            if isinstance(item, Section):
                side, _ = _render_items(item.items, current, 0)
                landing_href = href
    return "".join(entries), side, here, landing_href


def _render_toc(entries) -> str:
    shown = [entry for entry in entries if 2 <= entry.level <= 3]
    if not shown:
        return ""
    rows = "".join(
        f'<a href="#{_esc(entry.slug)}"{" class=\"sub\"" if entry.level == 3 else ""}>'
        f"{_esc(entry.text)}</a>" for entry in shown)
    return f'<div class="toc-rail">{rows}</div>'


@dataclass(slots=True)
class _RenderedPage:
    page: Page
    out_rel: str
    title: str
    html: str
    toc: tuple
    slugs: frozenset[str]
    description: str
    #: Front-matter `keywords:` — the words a reader would use that the page
    #: itself does not say. See `_write_search_index`.
    keywords: str = ""
    #: Front-matter `boost:` — a multiplier on this page's search score.
    boost: float = 1.0
    #: Generated search aliases — currently the capability map's package names.
    #: Kept apart from `keywords` because they are scored lower: nobody wrote
    #: them, so they are weaker evidence than a term the page claims for itself.
    aliases: str = ""


def build(site: Site, root: Path | None = None) -> BuildReport:
    """Render `site` to its output directory. Returns a `BuildReport`."""
    base = Path(root or ".")
    source_dir = base / site.source
    output_dir = base / site.output
    pages = site.nav.pages()
    known = {_output_path(p.source) for p in pages}

    errors: list[str] = []
    warnings: list[str] = []

    # Orphans are still reported *as* orphans — that is a fact about the nav, and
    # a page mid-authoring is allowed to be one. But being unreachable never made
    # its links right, and skipping them meant the gate had a hole in exactly the
    # place a new page starts life: a deliberately dead link on an orphan drew no
    # warning at all, and three of them appeared at once the day the page was
    # added to the nav. So an orphan is rendered and link-checked like any other
    # page; only its *output* is withheld, because nothing links to it.
    orphans: list[Page] = []
    if source_dir.is_dir():
        listed = {p.source for p in pages}
        for md in sorted(source_dir.rglob("*.md")):
            rel = md.relative_to(source_dir).as_posix()
            if rel in listed or _excluded(rel, site.exclude):
                continue
            warnings.append(f"orphan page not in nav: {rel}")
            orphans.append(Page(rel, rel))

    # Phase 1 — render every page and collect its heading slugs (needed before
    # any cross-page anchor can be validated).
    rendered_pages: list[_RenderedPage] = []
    rendered_orphans: list[_RenderedPage] = []
    chart_sources: set[Path] = set()
    #: An orphan's chart data is read to render it, but not published — nothing
    #: in the built site would link to it.
    unpublished_chart_sources: set[Path] = set()
    queue = [(page, False) for page in pages] + [(page, True) for page in orphans]
    for page, orphan in queue:
        src = source_dir / page.source
        if not src.is_file():
            # An orphan was listed by rglob moments ago, so this only fires if it
            # was deleted mid-build (`docs serve` rebuilds on every change).
            errors.append(
                f"{'orphan' if orphan else 'nav'} page missing on disk: {page.source}")
            continue
        text = src.read_text(encoding="utf-8")
        front = _frontmatter(text)
        description = _field(front, _FM_DESC) or site.description
        keywords = _field(front, _FM_KEYWORDS)
        boost = float(_field(front, _FM_BOOST) or 1.0)
        aliases = ""
        # The Python in the page, checked against the real objects before the
        # markdown is touched. Structural checks pass a page whose first line
        # raises `AttributeError`; five such pages shipped in one week.
        findings, _ = codeblocks.check_page(text, page.source)
        (errors if site.strict else warnings).extend(str(f) for f in findings)
        # ```chart -> SVG; note any data files read so we can publish them.
        text, chart_tokens = charts.extract(
            text, source_dir,
            unpublished_chart_sources if orphan else chart_sources)
        text, figure_tokens = figures.extract(text)
        text, hero_tokens = hero.extract(text)
        # The home page's dependency plate, minted from the same subsystem
        # manifest the capability map reads. Same bargain as every other
        # generated block: strict fails, a preview carries on.
        text, plate_tokens = plate.extract(
            text, source_dir, errors if site.strict else warnings)
        if capabilities.has_directive(text):
            # The capability map, minted from the subsystem manifest. Same
            # bargain as the reference directive: strict fails, a preview keeps
            # the note and carries on.
            text = capabilities.expand(
                text, source_dir, page.source, errors if site.strict else warnings
            )
            # ... and the reverse index: the packages the map names are also the
            # words a reader arrives already knowing, so they are searchable as
            # more than a mention in a table cell — but in their own, lower-
            # scored field, because the page never claimed them for itself.
            aliases = capabilities.alias_text(source_dir)
        if apidoc.has_directives(text):
            # Strict builds fail on a directive that could not be rendered; a
            # non-strict preview keeps the inline note and carries on.
            text = apidoc.expand(
                text, page.source, errors if site.strict else warnings
            )
        rendered = markdown.render(text)
        html = plate.restore(
            hero.restore(
                figures.restore(
                    charts.restore(rendered.html, chart_tokens), figure_tokens),
                hero_tokens),
            plate_tokens)
        (rendered_orphans if orphan else rendered_pages).append(_RenderedPage(
            page, _output_path(page.source),
            rendered.title or hero.title_of(hero_tokens)
            or plate.title_of(plate_tokens) or page.title,
            html, rendered.toc, frozenset(e.slug for e in rendered.toc), description,
            keywords, boost, aliases))

    slugs_by_page = {
        rp.out_rel: rp.slugs for rp in [*rendered_pages, *rendered_orphans]}

    # Phase 2 — validate dead links and broken anchors against the full slug map.
    sink = errors if site.strict else warnings
    for rp in rendered_pages:
        _check_page(rp.html, rp.out_rel, known, slugs_by_page, sink)
    # An orphan is judged against what the site *would* contain once it joined
    # the nav — nav pages plus the other orphans — so the answer is "are these
    # links right", not a second copy of "this page is not in the nav". A nav
    # page linking to an orphan is still dead, because the orphan is not built.
    reachable = known | {rp.out_rel for rp in rendered_orphans}
    for rp in rendered_orphans:
        _check_page(
            rp.html, rp.out_rel, reachable, slugs_by_page, sink, label="orphan")

    # Phase 3 — write each page with prev/next from nav order.
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_asset(output_dir / _CSS_PATH, theme.stylesheet(site.palette, site.feel))
    _write_asset(output_dir / _JS_PATH, scripts.runtime())
    index_pages: list[dict] = []
    index_sections: list[dict] = []
    # One resolution per build, not per page: the counts are a property of the
    # repository, and asking the host 160 times would be a rate limit, not a
    # header. A failure here is a warning and an unadorned link.
    repo_html = theme.repo_link(
        repo.describe(site.repo, warnings) if site.repo else None)
    links_html = theme.link_row(site.links)
    breadcrumbs = _breadcrumbs(site)
    for pos, rp in enumerate(rendered_pages):
        prev = rendered_pages[pos - 1] if pos > 0 else None
        nxt = rendered_pages[pos + 1] if pos + 1 < len(rendered_pages) else None
        tabs_html, nav_html, section_title, section_href = _nav_context(site, rp.out_rel)
        content = _rewrite_md_links(rp.html)
        html = theme.page(
            site_name=site.name,
            page_title=rp.title,
            content=content,
            nav_html=nav_html,
            tabs_html=tabs_html,
            section_title=section_title,
            section_href=section_href,
            toc_html=_render_toc(rp.toc),
            css_href=_relative(rp.out_rel, _CSS_PATH),
            js_href=_relative(rp.out_rel, _JS_PATH),
            palette=site.palette,
            feel=site.feel,
            search_root="../" * rp.out_rel.count("/"),
            description=rp.description,
            footer=_footer(rp.out_rel, prev, nxt, site),
            home_href=_relative(rp.out_rel, "index.html"),
            canonical=f"{site.base_url.rstrip('/')}/{rp.out_rel}" if site.base_url else "",
            repo_html=repo_html,
            links_html=links_html,
        )
        out_file = output_dir / rp.out_rel
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(html, encoding="utf-8")
        page_id = len(index_pages)
        record = {"u": rp.out_rel, "t": rp.title}
        # Only what this page actually declares: a field carrying its default on
        # 160 pages is 160 copies of "nothing to say".
        if breadcrumbs.get(rp.out_rel):
            record["c"] = breadcrumbs[rp.out_rel]
        if rp.keywords:
            record["k"] = rp.keywords
        if rp.aliases:
            record["a"] = rp.aliases
        if rp.boost != 1.0:
            record["b"] = rp.boost
        index_pages.append(record)
        for anchor, heading, body, words in _sections(content, rp.toc, rp.title):
            section = {"p": page_id, "a": anchor, "h": heading, "x": body}
            if words:
                section["w"] = words
            index_sections.append(section)

    # Phase 4 — site-level artifacts.
    (output_dir / "assets" / "search-index.json").write_text(
        json.dumps({"p": index_pages, "s": index_sections}, separators=(",", ":")),
        encoding="utf-8")
    _write_llms_txt(output_dir, site, rendered_pages)
    _write_robots(output_dir, site)
    _write_404(output_dir, site, repo_html, links_html)
    _copy_chart_sources(chart_sources, source_dir.resolve(), output_dir)
    if site.base_url:
        _write_sitemap(output_dir, site, rendered_pages)

    return BuildReport(len(rendered_pages), str(output_dir), tuple(errors), tuple(warnings))


def _breadcrumbs(site: Site) -> dict[str, str]:
    """Output path -> the nav trail above it, e.g. `"API reference"`.

    A result reading `wreath.queries` is ambiguous to anyone who has not already
    read the page; the same result under *API reference* is not. Only the trail
    *above* the page is kept — the page's own title is already the group line.
    """
    trails: dict[str, str] = {}

    def walk(items, trail: tuple[str, ...]) -> None:
        for item in items:
            if isinstance(item, Section):
                walk(item.items, trail + (item.title,))
            else:
                trails[_output_path(item.source)] = " › ".join(trail)

    walk(site.nav.items, ())
    return trails


def _sections(html: str, toc, title: str) -> list[tuple[str, str, str, str]]:
    """Split a page into `(anchor, heading, snippet, words)` search records.

    One record per h2/h3 rather than one blob per page. It costs a little more
    JSON and buys three things a page-level index cannot: a result that lands on
    the *section* you wanted, a heading match that can outrank a body match, and
    a snippet drawn from near the term instead of from the top of the page.

    The snippet is capped because it is shown to a reader. `words` is what makes
    the *rest* of the section findable — see `search.word_set`.
    """
    headings = {entry.slug: entry.text for entry in toc}
    out: list[tuple[str, str, str, str]] = []
    cursor = 0
    anchor, heading = "", title
    for match in _SECTION_SPLIT.finditer(html):
        chunk = _prose(html[cursor:match.start()])
        if chunk or not out:
            out.append((anchor, heading, chunk[:_SECTION_CHARS],
                        search.word_set(chunk, heading + " " + chunk[:_SECTION_CHARS])))
        anchor = match.group(2)
        heading = headings.get(anchor, anchor)
        cursor = match.start()
    chunk = _prose(html[cursor:])
    out.append((anchor, heading, chunk[:_SECTION_CHARS],
                search.word_set(chunk, heading + " " + chunk[:_SECTION_CHARS])))
    # A reference page opens with its `# module` title and goes straight into the
    # first `##`, leaving a lead record with the page's own name and no prose --
    # a result that repeats the group line above it and lands where the reader
    # already was. Kept only when it is the page's only record.
    if len(out) > 1 and not out[0][2] and not out[0][3]:
        del out[0]
    return out


def _prose(html: str) -> str:
    """Section HTML as the prose a reader would see, in full.

    The section's own heading comes off the front and the permalink glyphs come
    out throughout: the record already carries the heading in its own field, so
    leaving it in the body double-counted it when scoring and put "Routing # "
    at the head of every snippet.
    """
    html = _OWN_HEADING.sub("", html.lstrip())
    html = _PLATE_NAMES.sub(" ", html)
    return _WS.sub(" ", _TAG.sub(" ", _PERMALINK.sub("", html))).strip()


def _write_robots(output_dir: Path, site: Site) -> None:
    lines = ["User-agent: *", "Allow: /"]
    if site.base_url:
        lines.append(f"Sitemap: {site.base_url.rstrip('/')}/sitemap.xml")
    (output_dir / "robots.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_404(output_dir: Path, site: Site, repo_html: str, links_html: str) -> None:
    body = markdown.render(
        "# Page not found\n\nThe page you were looking for doesn't exist. "
        "Head back to the [home page](index.html) or press "
        "`Ctrl K` to search the docs.\n")
    tabs_html, nav_html, section_title, section_href = _nav_context(site, "404.html")
    html = theme.page(
        site_name=site.name, page_title="Page not found",
        content=_rewrite_md_links(body.html), nav_html=nav_html, tabs_html=tabs_html,
        section_title=section_title, section_href=section_href,
        toc_html="", css_href=_relative("404.html", _CSS_PATH),
        js_href=_relative("404.html", _JS_PATH), palette=site.palette,
        feel=site.feel, search_root="", description="", footer="",
        repo_html=repo_html, links_html=links_html)
    (output_dir / "404.html").write_text(html, encoding="utf-8")


def _footer(
    current: str, prev: _RenderedPage | None, nxt: _RenderedPage | None, site: Site,
) -> str:
    """Prev/next through nav order, and the page's own source when it has one."""
    left = (f'<a class="nav-prev" href="{_esc(_relative(current, prev.out_rel))}">'
            f'<span class="dir">&larr; Previous</span>'
            f'<span class="title">{_esc(prev.title)}</span></a>' if prev else "<span></span>")
    right = (f'<a class="nav-next" href="{_esc(_relative(current, nxt.out_rel))}">'
             f'<span class="dir">Next &rarr;</span>'
             f'<span class="title">{_esc(nxt.title)}</span></a>' if nxt else "<span></span>")
    meta = ""
    if site.source_url:
        source = current[:-5] + ".md"
        meta = (f'<div class="page-meta"><a href="'
                f'{_esc(site.source_url.rstrip("/"))}/{_esc(source)}">Edit this page</a></div>')
    return f'<nav class="page-nav">{left}{right}</nav>{meta}'


def _frontmatter(text: str) -> str:
    """The YAML-ish front-matter block, or `""` when the page has none."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[:end] if end >= 0 else ""


def _field(front: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(front)
    return match.group(1).strip("\"'") if match else ""


def _write_llms_txt(output_dir: Path, site: Site, pages: list[_RenderedPage]) -> None:
    """An llms.txt index of the docs (llmstxt.org) — for coding agents and LLMs."""
    base = site.base_url.rstrip("/")
    lines = [f"# {site.name}", ""]
    if site.description:
        lines += [f"> {site.description}", ""]
    lines.append("## Docs")
    for rp in pages:
        url = f"{base}/{rp.out_rel}" if base else rp.out_rel
        note = f": {rp.description}" if rp.description else ""
        lines.append(f"- [{rp.title}]({url}){note}")
    (output_dir / "llms.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_sitemap(output_dir: Path, site: Site, pages: list[_RenderedPage]) -> None:
    base = site.base_url.rstrip("/")
    urls = "".join(f"<url><loc>{_esc(base)}/{_esc(rp.out_rel)}</loc></url>" for rp in pages)
    (output_dir / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n',
        encoding="utf-8")


def _rewrite_md_links(html: str) -> str:
    # Author-relative `foo.md` / `foo.md#a` links point at the built `foo.html`.
    return _INTERNAL_MD.sub(lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"', html)


def _check_page(
    html: str, current: str, known: set[str],
    slugs_by_page: dict[str, frozenset[str]], sink: list[str],
    label: str = "",
) -> None:
    """Report every dead link and broken anchor on one rendered page.

    *label* tags the page's findings when it is not a normal nav page, so an
    orphan's broken links read as an orphan's without being folded into the
    single "orphan page not in nav" warning that already exists for it.
    """
    where = f"{current} ({label})" if label else current
    current_dir = posixpath.dirname(current)
    for match in _INTERNAL_MD.finditer(html):
        target = posixpath.normpath(posixpath.join(current_dir, match.group(1) + ".html"))
        if target not in known:
            sink.append(f"{where}: dead link to {match.group(1)}.md")
            continue
        anchor = match.group(2)
        if anchor and anchor[1:] not in slugs_by_page.get(target, frozenset()):
            sink.append(f"{where}: link to missing anchor {anchor} in {match.group(1)}.md")
    for match in _ANCHOR.finditer(html):        # same-page #anchor links
        name = match.group(1)
        if name and name not in slugs_by_page.get(current, frozenset()):
            sink.append(f"{where}: broken anchor #{name}")


def _write_asset(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_chart_sources(sources: set[Path], source_root: Path, output_dir: Path) -> None:
    """Publish each chart's data file so its `raw data` link resolves in the site.

    Only files that live under the docs source tree are copied (a chart pointed at
    a sibling directory like `../benchmark-results/` is read at build time but
    not republished — it isn't ours to serve).
    """
    for path in sources:
        try:
            rel = path.relative_to(source_root)
        except ValueError:
            continue                       # outside the source tree; don't copy
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())


def _excluded(rel: str, patterns: tuple[str, ...]) -> bool:
    # Match a source-relative path against a glob, treating "plans/" as "plans/**".
    return any(
        fnmatch(rel, pattern) or fnmatch(rel, pattern.rstrip("/") + "/*")
        for pattern in patterns)


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


__all__ = ["BuildReport", "build"]
