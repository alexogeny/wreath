"""The native static-site generator: config, markdown subset, and the build."""
from __future__ import annotations

import re

import pytest

from wreath._docs import Nav, Page, Section, Site, build
from wreath._docs.markdown import render, slugify

# --- config -----------------------------------------------------------------


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        Page("", "x.md")
    with pytest.raises(ValueError):
        Page("Title", "x.txt")           # must be .md
    with pytest.raises(ValueError):
        Site("s", "docs", "out", Nav())  # nav needs a page
    nav = Nav(Page("A", "a.md"), Section("Group", Page("B", "b.md")))
    assert [p.source for p in nav.pages()] == ["a.md", "b.md"]


# --- markdown ---------------------------------------------------------------


def test_markdown_blocks_and_inline() -> None:
    out = render("# Title\n\n## Sub\n\nText **b** *i* `c` [x](https://a.io).\n\n- one\n- two\n")
    assert '<h1 id="title">' in out.html and out.title == "Title"
    assert "<strong>b</strong>" in out.html and "<em>i</em>" in out.html
    assert "<code>c</code>" in out.html and 'href="https://a.io"' in out.html
    assert "<li>one</li>" in out.html
    assert [(e.level, e.slug) for e in out.toc] == [(1, "title"), (2, "sub")]


def test_markdown_escapes_and_rejects_dangerous_links() -> None:
    out = render("<script>alert(1)</script> and [x](javascript:alert(1))")
    assert "<script>" not in out.html and "&lt;script&gt;" in out.html
    assert "javascript:" not in out.html          # scheme rejected -> href="#"


def test_fenced_code_keeps_language_and_escapes() -> None:
    out = render("```python\nif a < b: pass\n```\n")
    assert 'class="language-python"' in out.html
    assert "&lt;" in out.html and "<b" not in out.html   # `<` escaped, not raw markup
    assert 'class="tok-keyword">if' in out.html          # highlighted


def test_slugify_is_github_style() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("A B  C") == "a-b--c" or slugify("A B  C") == "a-b-c"


def test_duplicate_heading_slugs_are_disambiguated() -> None:
    out = render("# Setup\n\n# Setup\n")
    slugs = [e.slug for e in out.toc]
    assert slugs == ["setup", "setup-1"]


# --- build ------------------------------------------------------------------


def _site(tmp_path):
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "index.md").write_text("# Home\n\nSee [routing](guides/routing.md).\n")
    (src / "guides" / "routing.md").write_text("# Routing\n\n## Basics\n\ntext\n")
    return Site(
        name="Demo", source="docs", output="site",
        nav=Nav(Page("Home", "index.md"), Section("Guides", Page("Routing", "guides/routing.md"))),
    )


def test_build_writes_pages_css_and_rewrites_links(tmp_path) -> None:
    report = build(_site(tmp_path), root=tmp_path)
    assert report.ok and report.pages == 2
    index = (tmp_path / "site" / "index.html").read_text()
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'href="guides/routing.html"' in index      # .md link rewritten
    assert (tmp_path / "site" / "assets" / "docs.css").is_file()
    assert '../assets/docs.css' in routing             # relative from a nested page
    assert 'class="active"' in routing                 # active nav item


def test_strict_build_flags_dead_links(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text("# Home\n\n[gone](guides/missing.md)\n")
    report = build(site, root=tmp_path)
    assert not report.ok
    assert any("dead link" in error for error in report.errors)


def test_orphan_page_is_warned(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text("# Loose\n")   # not in nav
    report = build(site, root=tmp_path)
    assert any("orphan" in warning for warning in report.warnings)


def test_content_tabs_render_as_a_tab_group() -> None:
    out = render('=== "A"\n    one\n\n=== "B"\n    two\n')
    assert "data-tabs" in out.html
    assert out.html.count("tab-label") >= 2 and out.html.count("tab-panel") == 2


def test_code_is_highlighted_and_still_escaped() -> None:
    out = render("```python\ndef f(): return 1 < 2  # c\n```\n")
    assert 'class="tok-keyword">def' in out.html
    assert 'tok-comment' in out.html and "&lt;" in out.html


def test_admonition_and_table(tmp_path) -> None:
    table = render("| A | B |\n|:-:|--|\n| 1 | 2 |\n")
    assert "<table>" in table.html and "text-align:center" in table.html
    adm = render('!!! warning "Careful"\n    body text\n')
    assert 'class="admonition warning"' in adm.html and "admonition-title" in adm.html


def test_search_index_is_written(tmp_path) -> None:
    import json

    site = _site(tmp_path)
    build(site, root=tmp_path)
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    entry = next(e for e in index if e["u"] == "guides/routing.html")
    assert entry["t"] == "Routing"
    assert any(h["s"] == "basics" for h in entry["h"])
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'id="docs-search"' in routing and 'data-root="../"' in routing


def test_prev_next_and_frontmatter_description(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text(
        "---\ndescription: The landing page.\n---\n# Home\n\ntext\n")
    build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'meta name="description" content="The landing page."' in index
    assert "page-nav" in index and "Routing →" in index      # next
    assert "nav-prev" in routing and "← Home" in routing      # prev


def test_llms_txt_and_sitemap(tmp_path) -> None:
    site = Site("Demo", "docs", "site", _site(tmp_path).nav,
                base_url="https://d.io", description="Demo docs.")
    build(site, root=tmp_path)
    llms = (tmp_path / "site" / "llms.txt").read_text()
    assert "# Demo" in llms and "https://d.io/guides/routing.html" in llms
    sitemap = (tmp_path / "site" / "sitemap.xml").read_text()
    assert "<loc>https://d.io/index.html</loc>" in sitemap


def test_strict_anchor_validation(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text(
        "# Home\n\n[bad](guides/routing.md#no-such-anchor)\n")
    report = build(site, root=tmp_path)
    assert any("missing anchor" in error for error in report.errors)


def test_images_strikethrough_and_task_lists() -> None:
    out = render("![a pic](p.png)\n\n~~old~~ new\n\n- [ ] todo\n- [x] done\n")
    assert '<img src="p.png" alt="a pic" loading="lazy">' in out.html
    assert "<del>old</del>" in out.html
    assert 'type="checkbox" disabled>' in out.html and "disabled checked>" in out.html
    assert 'src="#"' in render("![x](javascript:alert(1))").html   # unsafe src rejected


def test_themes_and_feels_compose(tmp_path) -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    assert set(THEMES) == {"wreath", "slate", "sepia", "nord", "terminal"}
    assert "#5e81ac" in stylesheet(THEMES["nord"])            # nord primary
    assert "feTurbulence" in stylesheet(THEMES["sepia"], "papery")
    assert "--radius:0px" in stylesheet(THEMES["wreath"], "hardcore")
    assert "border-radius:999px" in stylesheet(THEMES["wreath"], "orby")
    with pytest.raises(ValueError, match="unknown feel"):
        Site("s", "docs", "o", Nav(Page("H", "i.md")), feel="nope")


def test_build_applies_theme_and_feel(tmp_path) -> None:
    from wreath._docs import THEMES

    site = Site("D", "docs", "site", _site(tmp_path).nav,
                palette=THEMES["terminal"], feel="hardcore")
    build(site, root=tmp_path)
    css = (tmp_path / "site" / "assets" / "docs.css").read_text()
    assert "#16a34a" in css and "--radius:0px" in css


def test_chart_from_json_renders_svg(tmp_path) -> None:
    import json

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bench.json").write_text(json.dumps({
        "results": [
            {"name": "a", "rps": 100}, {"name": "b", "rps": 250}, {"name": "a", "rps": 180},
        ]}))
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "guides" / "routing.md").write_text("# R\n")
    (src / "index.md").write_text(
        "# Bench\n\n```chart\nsource: ../data/bench.json\ndata: results\n"
        "x: name\ny: rps\nsort: desc\ntitle: RPS\n```\n")
    build(_site_like(tmp_path), root=tmp_path)
    html = (tmp_path / "site" / "index.html").read_text()
    assert '<figure class="chart">' in html and "<svg" in html
    # Two bars (rx="3"); non-wreath labels use the muted competitor hatch.
    assert html.count('rx="3"') == 2 and "url(#wc-hatch-" in html
    # a bad source degrades to a visible note, not a crash
    (src / "index.md").write_text("# X\n\n```chart\nsource: gone.json\n```\n")
    build(_site_like(tmp_path), root=tmp_path)
    assert "chart-error" in (tmp_path / "site" / "index.html").read_text()


def test_robots_and_404(tmp_path) -> None:
    site = Site("D", "docs", "site", _site(tmp_path).nav, base_url="https://d.io")
    build(site, root=tmp_path)
    assert "Sitemap: https://d.io/sitemap.xml" in (tmp_path / "site" / "robots.txt").read_text()
    assert "Page not found" in (tmp_path / "site" / "404.html").read_text()


def _site_like(tmp_path):
    return Site("Bench", "docs", "site",
                Nav(Page("Home", "index.md"), Section("G", Page("R", "guides/routing.md"))))


# --- dogfooding: build wreath's own docs corpus -----------------------------


def _repo_docs() -> object:
    import pathlib

    docs = pathlib.Path(__file__).resolve().parent.parent / "docs"
    return docs if docs.is_dir() else None


def test_renders_the_whole_docs_corpus_without_crashing() -> None:
    from wreath._docs import apidoc, markdown

    docs = _repo_docs()
    if docs is None:
        pytest.skip("docs/ not present")
    rendered = 0
    for md in sorted(docs.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        if apidoc.has_directives(text):
            text = apidoc.expand(text)      # imports the real modules
        result = markdown.render(text)      # must not raise on any real page
        assert result.html is not None
        rendered += 1
    assert rendered > 100                    # the corpus is ~190 pages


def test_builds_the_guides_as_a_real_site(tmp_path) -> None:
    import pathlib
    import shutil

    docs = _repo_docs()
    if docs is None:
        pytest.skip("docs/ not present")
    # Copy the whole corpus so every cross-link resolves, and nav every page.
    shutil.copytree(docs, tmp_path / "docs")
    all_md = sorted((tmp_path / "docs").rglob("*.md"))
    pages = tuple(Page(p.stem, p.relative_to(tmp_path / "docs").as_posix()) for p in all_md)
    site = Site("Wreath", "docs", "site", Nav(*pages), strict=False)
    report = build(site, root=tmp_path)
    assert report.pages == len(all_md)
    # A real prose page and a real API page both produced HTML.
    assert (tmp_path / "site" / "guides" / "routing.html").is_file()
    index = pathlib.Path(tmp_path / "site" / "index.html").read_text()
    assert "<h1" in index


def test_explicit_heading_anchor() -> None:
    # `{#custom-id}` pins the anchor and is stripped from the visible text.
    out = render("## FaultSchedule {#wreath.replay.FaultSchedule}\n")
    assert 'id="wreath.replay.FaultSchedule"' in out.html
    assert "{#" not in out.html
    assert out.toc[0].slug == "wreath.replay.FaultSchedule"
    assert out.toc[0].text == "FaultSchedule"


def test_apidoc_pins_mkdocstrings_anchor() -> None:
    from wreath._docs import apidoc

    # A `::: dotted.path` directive anchors on the full path, so cross-refs
    # written mkdocstrings-style (#module.Class) resolve under the native SSG.
    html = render(apidoc.expand("::: wreath._docs.config.Site\n")).html
    assert 'id="wreath._docs.config.Site"' in html


def test_exclude_suppresses_orphan_warnings(tmp_path) -> None:
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "plans" / "draft.md").write_text("# Draft\n")
    nav = Nav(Page("Home", "index.md"))
    noisy = build(Site("S", "docs", "out", nav, strict=False), root=tmp_path)
    assert any("orphan" in w for w in noisy.warnings)
    quiet = build(
        Site("S", "docs", "out", nav, strict=False, exclude=("plans/",)), root=tmp_path)
    assert not any("orphan" in w for w in quiet.warnings)


def test_nav_sections_collapse_and_auto_open(tmp_path) -> None:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "guides" / "a.md").write_text("# A\n")
    (docs / "guides" / "b.md").write_text("# B\n")
    nav = Nav(
        Page("Home", "index.md"),
        Section("Guides", Page("A", "guides/a.md")),
        Section("Other", Page("B", "guides/b.md")),
    )
    build(Site("S", "docs", "out", nav, strict=False), root=tmp_path)
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    assert "<details class=\"section\"" in html          # collapsible groups
    # The branch holding the current page is open; the sibling section is not.
    assert '<details class="section" open><summary>Guides</summary>' in html
    assert '<details class="section" open><summary>Other</summary>' not in html


def test_chart_colors_wreath_arms_distinctly() -> None:
    from wreath._docs import charts

    svg = charts._svg_bar(
        [("Wreath (metal)", 3.0), ("Wreath (native)", 2.0), ("BlackSheep", 1.0)], "t", "")
    # Each wreath arm gets its own fill; the competitor gets the hatch.
    assert "var(--primary)" in svg and "var(--accent)" in svg
    assert "url(#wc-hatch-" in svg


def test_two_charts_on_a_page_do_not_share_a_pattern_id() -> None:
    """An SVG `pattern` id is document-scoped.

    Two charts both calling their hatch `wc-hatch` is invalid HTML, and the
    second chart's bars resolve against the first chart's pattern. `wreath
    audit` reported it as a duplicate-id error on the performance page.
    """
    import re

    from wreath._docs import charts

    first = charts._svg_bar([("BlackSheep", 1.0)], "one", "")
    second = charts._svg_bar([("BlackSheep", 1.0)], "two", "")
    ids = re.compile(r'pattern id="([^"]+)"')
    assert ids.search(first).group(1) != ids.search(second).group(1)
    # Stable across builds, so a rebuild is not a diff.
    assert first == charts._svg_bar([("BlackSheep", 1.0)], "one", "")


def test_chart_source_is_published(tmp_path) -> None:
    docs = tmp_path / "docs"
    (docs / "data").mkdir(parents=True)
    (docs / "data" / "d.json").write_text('{"results":[{"k":"a","v":3},{"k":"b","v":5}]}')
    (docs / "index.md").write_text(
        "# Home\n\n```chart\nsource: data/d.json\ndata: results\nx: k\ny: v\n```\n")
    build(Site("S", "docs", "out", Nav(Page("Home", "index.md")), strict=False), root=tmp_path)
    out = (tmp_path / "out" / "index.html").read_text()
    assert '<figure class="chart">' in out and "chart-error" not in out
    # The data file the chart read is copied into the site so its link resolves.
    assert (tmp_path / "out" / "data" / "d.json").is_file()


def test_cli_build_end_to_end(tmp_path) -> None:
    from wreath._docs_cli import execute

    src = tmp_path / "docs"
    src.mkdir()
    (src / "index.md").write_text("# Home\n")
    (tmp_path / "wreath_docs.py").write_text(
        "from wreath._docs import Site, Nav, Page\n"
        "site = Site('D', 'docs', 'site', Nav(Page('Home', 'index.md')))\n"
    )
    config = str(tmp_path / "wreath_docs.py")
    namespace = type("N", (), {"docs_action": "check", "config": config})()
    assert execute(namespace) == 0


# --- theme: the design system holds together --------------------------------
#
# The theme is CSS, which is where unexplained numbers breed. These pin the
# properties that make it a system rather than a pile of values, and the
# accessibility floor that `wreath audit` would otherwise never see (its
# contrast rules only read inline <style>, so they were dormant while the whole
# stylesheet was an external file).


def _all_themes():
    from wreath._docs import THEMES

    return sorted(THEMES.items())


def test_every_theme_meets_aa_in_both_modes() -> None:
    """Body, secondary text, and links, on both surfaces, for all five themes.

    Two of these used to fail: `nord` and `terminal` light links sat at ~3.5:1
    against white, which is below AA for normal text.
    """
    from wreath._audit.contrast import contrast_ratio

    failures = []
    for name, p in _all_themes():
        pairs = {
            "body": (p.fg, p.bg), "muted": (p.muted, p.bg),
            "link": (p.link or p.primary, p.bg), "on-surface": (p.fg, p.surface),
            "dark body": (p.dark_fg, p.dark_bg), "dark muted": (p.dark_muted, p.dark_bg),
            "dark link": (p.dark_link or p.primary, p.dark_bg),
            "dark on-surface": (p.dark_fg, p.dark_surface),
        }
        for role, (fg, bg) in pairs.items():
            ratio = contrast_ratio(fg, bg) or 0.0
            if ratio < 4.5:
                failures.append(f"{name} {role}: {ratio:.2f}:1")
    assert not failures, "below WCAG AA (4.5:1): " + ", ".join(failures)


def test_control_boundaries_meet_non_text_contrast() -> None:
    """WCAG 1.4.11 — 3:1, and only for things you can operate.

    The decorative hairline (`--border`) is deliberately faint: a table rule at
    3:1 is a cage, and 1.4.11 does not ask for one. What must clear 3:1 is the
    boundary of a *control* and the focus ring, which is why the theme has a
    second token for them.
    """
    from wreath._audit.contrast import contrast_ratio

    failures = []
    for name, p in _all_themes():
        # `--border-strong` resolves to the muted text colour; see theme.py.
        for mode, strong, bg in (("light", p.muted, p.bg), ("dark", p.dark_muted, p.dark_bg)):
            ratio = contrast_ratio(strong, bg) or 0.0
            if ratio < 3.0:
                failures.append(f"{name} {mode} control border: {ratio:.2f}:1")
    assert not failures, "control boundary below 3:1: " + ", ".join(failures)


def test_the_critical_css_is_inlined_and_within_the_audit_budget() -> None:
    """Inlined so the palette is auditable, small so it stays inlinable.

    `wreath audit`'s contrast rules only inspect inline <style>, and its
    perf rule caps an un-nonced inline asset at 16 KiB. The critical block has
    to sit inside both constraints at once.
    """
    from wreath._docs import THEMES
    from wreath._docs.theme import critical_css

    for name, palette in THEMES.items():
        size = len(critical_css(palette).encode("utf-8"))
        assert size <= 16 * 1024, f"{name} critical CSS is {size} bytes (budget 16 KiB)"


def test_a_built_page_carries_its_tokens_inline() -> None:
    """So the contrast rule has something to read on every page it audits."""
    from wreath._docs import THEMES
    from wreath._docs.theme import page

    html = page(site_name="D", page_title="P", content="<p>x</p>", nav_html="",
                toc_html="", css_href="assets/docs.css", palette=THEMES["nord"])
    assert "<style>" in html
    assert "--fg:" in html and "--bg:" in html


def test_no_built_page_reaches_the_network(tmp_path) -> None:
    """The module's promise is a self-contained document; keep it true."""
    import re

    build(_site(tmp_path), root=tmp_path)
    external = re.compile(r'(?:src|href)\s*=\s*"(?:https?:)?//')
    for path in (tmp_path / "site").rglob("*.html"):
        assert not external.search(path.read_text()), f"{path.name} loads a remote asset"


def test_motion_is_optional() -> None:
    """WCAG 2.3.3 — animation is a vestibular trigger, not a decoration."""
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    assert "prefers-reduced-motion" in css


def test_the_type_and_space_scales_are_declared_once() -> None:
    """Ad-hoc sizes are what made it look janky; the scale is the fix."""
    from wreath._docs import THEMES
    from wreath._docs.theme import critical_css

    css = critical_css(THEMES["wreath"])
    for token in ("--text-base", "--text-3xl", "--space-1", "--space-6", "--measure"):
        assert token in css, f"missing design token {token}"


def test_syntax_colours_are_tinted_into_the_theme() -> None:
    """Fixed GitHub hexes looked wrong in four of the five themes.

    Each token is now a tuned hue mixed toward the page foreground, so it lands
    in the active theme, and the light and dark sets are different colours
    rather than one set used on both surfaces.
    """
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["sepia"])
    for github_hex in ("#032f62", "#d73a49", "#6f42c1", "#e36209"):
        assert github_hex not in css, f"{github_hex} is a fixed GitHub token colour"
    assert "--tok-keyword:color-mix(in oklab," in css and "var(--fg))" in css
    assert css.count("--tok-keyword") >= 2, "light and dark need their own hue"


def test_syntax_tokens_stay_legible_on_every_code_surface() -> None:
    """The reason the tokens are tuned hues and not brand derivations.

    Mixing `--accent` toward the foreground collapsed to 2.5:1 on nord's light
    surface — a bright accent cannot be darkened enough to read on a light
    background. These are the measured floors for the set that replaced it.
    """
    from wreath._audit.contrast import _hex_to_rgb, contrast_ratio
    from wreath._docs import THEMES
    from wreath._docs.theme import _TINT, _syntax

    def mix(top: str, bottom: str, pct: float) -> str:
        """Approximate color-mix() in linear-light sRGB, close enough for a floor."""
        def lin(c: int) -> float:
            v = c / 255
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

        def unlin(v: float) -> int:
            out = 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
            return max(0, min(255, round(255 * out)))

        a, b = _hex_to_rgb(top), _hex_to_rgb(bottom)
        mixed = (
            unlin(lin(x) * pct + lin(y) * (1 - pct)) for x, y in zip(a, b, strict=True)
        )
        return "#" + "".join(f"{channel:02x}" for channel in mixed)

    hexes = re.compile(r"--tok-(\w+):color-mix\(in oklab, (#[0-9a-f]{6})")
    failures = []
    for name, p in THEMES.items():
        for is_light, fg, surface in ((True, p.fg, p.surface),
                                      (False, p.dark_fg, p.dark_surface)):
            for token, hue in hexes.findall(_syntax(is_light)):
                ratio = contrast_ratio(mix(hue, fg, _TINT / 100), surface) or 0.0
                if ratio < 4.5:
                    mode = "light" if is_light else "dark"
                    failures.append(f"{name} {mode} {token}: {ratio:.2f}:1")
    assert not failures, "syntax token below AA: " + ", ".join(failures)


def test_no_synthetic_font_weights() -> None:
    """750/650/550 are not real weights; they render inconsistently."""
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    for weight in ("font-weight:750", "font-weight:650", "font-weight:550"):
        assert weight not in css, f"{weight} is not a system-font weight"


def test_a_wide_table_scrolls_itself(tmp_path) -> None:
    """Otherwise the page body scrolls sideways on a phone."""
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert 'class="table-wrap"' in out.html
    assert out.html.index("table-wrap") < out.html.index("<table")


def test_table_headers_are_scoped() -> None:
    """WCAG 1.3.1 — without scope a table reads as an undifferentiated grid."""
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert out.html.count('<th scope="col"') == 2


def test_the_page_is_navigable_by_keyboard_and_screen_reader() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import page

    html = page(site_name="D", page_title="P", content="<p>x</p>",
                nav_html="<a href='x.html'>X</a>", toc_html="<a href='#y'>Y</a>",
                css_href="assets/docs.css", palette=THEMES["wreath"])
    assert 'href="#content"' in html                  # skip link
    assert 'id="content"' in html                     # and its target
    assert html.count("aria-label") >= 4              # both nav landmarks labelled
    assert 'role="listbox"' not in html               # invalid with <a> children


def test_every_theme_builds_a_whole_site(tmp_path) -> None:
    from wreath._docs import THEMES

    for name, palette in THEMES.items():
        root = tmp_path / name
        root.mkdir()
        src = root / "docs"
        (src / "guides").mkdir(parents=True)
        (src / "index.md").write_text("# Home\n\ntext\n")
        (src / "guides" / "routing.md").write_text("# Routing\n\n## S\n\n`x`\n")
        site = Site("D", "docs", "site",
                    Nav(Page("Home", "index.md"),
                        Section("G", Page("R", "guides/routing.md"))),
                    palette=palette)
        report = build(site, root=root)
        assert report.ok, f"{name}: {report.errors}"
        assert (root / "site" / "index.html").read_text().startswith("<!doctype html>")
