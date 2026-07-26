"""Build a :class:`~wreath._docs.config.Site` into a static HTML tree.

The Python facade owns everything except the per-file markdown parse: page
collection from the nav, output-path mapping, relative-link resolution, the
nav/TOC assembly, strict orphan/dead-link checks, and all file I/O. The result is
a plain directory of self-contained HTML you can serve with wreath's hardened
:class:`~wreath.staticfiles.StaticFiles`.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from . import apidoc, charts, markdown, theme
from .config import Nav, Page, Section, Site

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_CSS_PATH = "assets/docs.css"
_INTERNAL_MD = re.compile(r'href="([^"#:]+)\.md(#[^"]*)?"')
_ANCHOR = re.compile(r'href="#([^"]+)"')
_FM_DESC = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


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


def _relative(from_output: str, to_output: str) -> str:
    """Relative href from one output page to another (or an asset)."""
    return posixpath.relpath(to_output, start=posixpath.dirname(from_output)) or "."


def _render_nav(nav: Nav, current: str) -> str:
    parts: list[str] = []
    _render_nav_items(nav.items, current, parts)
    return "".join(parts)


def _section_has(item: Page | Section, current: str) -> bool:
    """Does this nav subtree contain the page currently being rendered?"""
    if isinstance(item, Page):
        return _output_path(item.source) == current
    return any(_section_has(child, current) for child in item.items)


def _render_nav_items(items, current: str, parts: list[str]) -> None:
    for item in items:
        if isinstance(item, Page):
            target = _output_path(item.source)
            href = _relative(current, target)
            active = ' class="active"' if target == current else ""
            parts.append(f'<a href="{_esc(href)}"{active}>{_esc(item.title)}</a>')
        elif isinstance(item, Section):
            # A collapsible group; auto-open the branch holding the current page.
            open_attr = " open" if _section_has(item, current) else ""
            parts.append(f'<details class="section"{open_attr}>'
                         f'<summary>{_esc(item.title)}</summary>')
            _render_nav_items(item.items, current, parts)
            parts.append("</details>")


def _render_toc(entries) -> str:
    shown = [e for e in entries if 2 <= e.level <= 3]
    if not shown:
        return ""
    rows = "".join(
        f'<a href="#{_esc(e.slug)}" style="padding-left:{(e.level - 2) * 0.8}rem">'
        f"{_esc(e.text)}</a>" for e in shown)
    return f"<strong>On this page</strong>{rows}"


@dataclass(slots=True)
class _RenderedPage:
    page: Page
    out_rel: str
    title: str
    html: str
    toc: tuple
    slugs: frozenset[str]
    description: str


def build(site: Site, root: Path | None = None) -> BuildReport:
    """Render ``site`` to its output directory. Returns a :class:`BuildReport`."""
    base = Path(root or ".")
    source_dir = base / site.source
    output_dir = base / site.output
    pages = site.nav.pages()
    known = {_output_path(p.source) for p in pages}

    errors: list[str] = []
    warnings: list[str] = []

    if source_dir.is_dir():
        listed = {p.source for p in pages}
        for md in sorted(source_dir.rglob("*.md")):
            rel = md.relative_to(source_dir).as_posix()
            if rel in listed or _excluded(rel, site.exclude):
                continue
            warnings.append(f"orphan page not in nav: {rel}")

    # Phase 1 — render every page and collect its heading slugs (needed before
    # any cross-page anchor can be validated).
    rendered_pages: list[_RenderedPage] = []
    chart_sources: set[Path] = set()
    for page in pages:
        src = source_dir / page.source
        if not src.is_file():
            errors.append(f"nav page missing on disk: {page.source}")
            continue
        text = src.read_text(encoding="utf-8")
        description = _frontmatter_description(text) or site.description
        # ```chart -> SVG; note any data files read so we can publish them.
        text, chart_tokens = charts.extract(text, source_dir, chart_sources)
        if apidoc.has_directives(text):
            text = apidoc.expand(text)
        rendered = markdown.render(text)
        html = charts.restore(rendered.html, chart_tokens)
        rendered_pages.append(_RenderedPage(
            page, _output_path(page.source), rendered.title or page.title,
            html, rendered.toc, frozenset(e.slug for e in rendered.toc), description))

    slugs_by_page = {rp.out_rel: rp.slugs for rp in rendered_pages}

    # Phase 2 — validate dead links and broken anchors against the full slug map.
    sink = errors if site.strict else warnings
    for rp in rendered_pages:
        _check_page(rp.html, rp.out_rel, known, slugs_by_page, sink)

    # Phase 3 — write each page with prev/next from nav order.
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_css(output_dir / _CSS_PATH, site)
    index: list[dict] = []
    for pos, rp in enumerate(rendered_pages):
        prev = rendered_pages[pos - 1] if pos > 0 else None
        nxt = rendered_pages[pos + 1] if pos + 1 < len(rendered_pages) else None
        html = theme.page(
            site_name=site.name,
            page_title=rp.title,
            content=_rewrite_md_links(rp.html),
            nav_html=_render_nav(site.nav, rp.out_rel),
            toc_html=_render_toc(rp.toc),
            css_href=_relative(rp.out_rel, _CSS_PATH),
            palette=site.palette,
            search_root="../" * rp.out_rel.count("/"),
            description=rp.description,
            footer=_footer(rp.out_rel, prev, nxt),
            home_href=_relative(rp.out_rel, "index.html"),
        )
        out_file = output_dir / rp.out_rel
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(html, encoding="utf-8")
        index.append({
            "u": rp.out_rel, "t": rp.title,
            "h": [{"t": e.text, "s": e.slug} for e in rp.toc if e.level <= 3],
            "b": _WS.sub(" ", _TAG.sub(" ", rp.html)).strip()[:1500],
        })

    # Phase 4 — site-level artifacts.
    (output_dir / "assets" / "search-index.json").write_text(
        json.dumps(index, separators=(",", ":")), encoding="utf-8")
    _write_llms_txt(output_dir, site, rendered_pages)
    _write_robots(output_dir, site)
    _write_404(output_dir, site)
    _copy_chart_sources(chart_sources, source_dir.resolve(), output_dir)
    if site.base_url:
        _write_sitemap(output_dir, site, rendered_pages)

    return BuildReport(len(rendered_pages), str(output_dir), tuple(errors), tuple(warnings))


def _write_robots(output_dir: Path, site: Site) -> None:
    lines = ["User-agent: *", "Allow: /"]
    if site.base_url:
        lines.append(f"Sitemap: {site.base_url.rstrip('/')}/sitemap.xml")
    (output_dir / "robots.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_404(output_dir: Path, site: Site) -> None:
    body = markdown.render(
        "# Page not found\n\nThe page you were looking for doesn't exist. "
        "Head back to the [home page](index.html).\n")
    html = theme.page(
        site_name=site.name, page_title="Page not found",
        content=_rewrite_md_links(body.html), nav_html=_render_nav(site.nav, "404.html"),
        toc_html="", css_href=_relative("404.html", _CSS_PATH), palette=site.palette,
        search_root="", description="", footer="")
    (output_dir / "404.html").write_text(html, encoding="utf-8")


def _footer(current: str, prev: _RenderedPage | None, nxt: _RenderedPage | None) -> str:
    left = (f'<a class="nav-prev" href="{_esc(_relative(current, prev.out_rel))}">'
            f"← {_esc(prev.title)}</a>" if prev else "<span></span>")
    right = (f'<a class="nav-next" href="{_esc(_relative(current, nxt.out_rel))}">'
             f"{_esc(nxt.title)} →</a>" if nxt else "<span></span>")
    return f'<nav class="page-nav">{left}{right}</nav>'


def _frontmatter_description(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    match = _FM_DESC.search(text[:end])
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
) -> None:
    current_dir = posixpath.dirname(current)
    for match in _INTERNAL_MD.finditer(html):
        target = posixpath.normpath(posixpath.join(current_dir, match.group(1) + ".html"))
        if target not in known:
            sink.append(f"{current}: dead link to {match.group(1)}.md")
            continue
        anchor = match.group(2)
        if anchor and anchor[1:] not in slugs_by_page.get(target, frozenset()):
            sink.append(f"{current}: link to missing anchor {anchor} in {match.group(1)}.md")
    for match in _ANCHOR.finditer(html):        # same-page #anchor links
        name = match.group(1)
        if name and name not in slugs_by_page.get(current, frozenset()):
            sink.append(f"{current}: broken anchor #{name}")


def _write_css(path: Path, site: Site) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(theme.stylesheet(site.palette, site.feel), encoding="utf-8")


def _copy_chart_sources(sources: set[Path], source_root: Path, output_dir: Path) -> None:
    """Publish each chart's data file so its `raw data` link resolves in the site.

    Only files that live under the docs source tree are copied (a chart pointed at
    a sibling directory like ``../benchmark-results/`` is read at build time but
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
