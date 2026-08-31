from __future__ import annotations

import dataclasses
import json
import re

import pytest

from wreath._docs import Nav, Page, Section, Site, build
from wreath._docs._fenced import title_of
from wreath._docs.figures import _timing_wheel
from wreath._docs.markdown import render, slugify


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        Page("", "x.md")
    with pytest.raises(ValueError):
        Page("Title", "x.txt")  # must be .md
    with pytest.raises(ValueError):
        Site("s", "docs", "out", Nav())  # nav needs a page
    nav = Nav(Page("A", "a.md"), Section("Group", Page("B", "b.md")))
    assert [p.source for p in nav.pages()] == ["a.md", "b.md"]


def test_timing_wheel_draws_only_the_six_occupied_timer_slots() -> None:
    figure = _timing_wheel("wheel")

    assert figure.count('<circle class="f-timer"') == 6


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
    assert "javascript:" not in out.html  # scheme rejected -> href="#"


def test_fenced_code_keeps_language_and_escapes() -> None:
    out = render("```python\nif a < b: pass\n```\n")
    assert 'class="language-python"' in out.html
    assert "&lt;" in out.html and "<b" not in out.html  # `<` escaped, not raw markup
    assert 'class="tok-keyword">if' in out.html  # highlighted


def test_fenced_title_ignores_an_unclosed_heading() -> None:
    tokens = {"hero": '<h1 class="hero-title">unfinished'}

    assert title_of(tokens, "hero-title") == ""


def test_slugify_is_github_style() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("A B  C") == "a-b--c" or slugify("A B  C") == "a-b-c"


def test_duplicate_heading_slugs_are_disambiguated() -> None:
    out = render("# Setup\n\n# Setup\n")
    slugs = [e.slug for e in out.toc]
    assert slugs == ["setup", "setup-1"]


def _site(tmp_path):
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "index.md").write_text("# Home\n\nSee [routing](guides/routing.md).\n")
    (src / "guides" / "routing.md").write_text("# Routing\n\n## Basics\n\ntext\n")
    return Site(
        name="Demo",
        source="docs",
        output="site",
        nav=Nav(Page("Home", "index.md"), Section("Guides", Page("Routing", "guides/routing.md"))),
    )


def test_build_writes_pages_css_and_rewrites_links(tmp_path) -> None:
    report = build(_site(tmp_path), root=tmp_path)
    assert report.ok and report.pages == 2
    index = (tmp_path / "site" / "index.html").read_text()
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'href="guides/routing.html"' in index  # .md link rewritten
    assert (tmp_path / "site" / "assets" / "docs.css").is_file()
    assert "../assets/docs.css" in routing  # relative from a nested page
    assert 'class="nav-page active"' in routing  # active nav item
    assert (tmp_path / "site" / "assets" / "docs.js").is_file()
    assert "../assets/docs.js" in routing


def test_strict_build_flags_dead_links(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text("# Home\n\n[gone](guides/missing.md)\n")
    report = build(site, root=tmp_path)
    assert not report.ok
    assert any("dead link" in error for error in report.errors)


def test_strict_build_parses_fenced_wreath_commands_with_the_live_cli(tmp_path) -> None:
    site = _site(tmp_path)
    guide = tmp_path / "docs" / "guides" / "routing.md"
    guide.write_text(
        "# Routing\n\n```bash\n"
        "WREATH_HOST=0.0.0.0 uv run wreath serve app:app --workers 4\n"
        "```\n"
    )

    report = build(site, root=tmp_path)

    assert any("Wreath CLI example" in error and "serve" in error for error in report.errors)

    guide.write_text(
        "# Routing\n\n```bash\n"
        "uv run wreath run app:app --loop metal --workers 4\n"
        "wreath capture /run/wreath/app.sock disarm --arm-id 17\n"
        "```\n"
    )

    assert build(site, root=tmp_path).ok


def test_strict_build_checks_nested_cli_arguments(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "guides" / "routing.md").write_text(
        "# Routing\n\n```bash\n"
        "wreath capture /run/wreath/app.sock disarm 17\n"
        "```\n"
    )

    report = build(site, root=tmp_path)

    assert any("Wreath CLI example" in error and "arm-id" in error for error in report.errors)


def test_orphan_page_is_warned(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text("# Loose\n")  # not in nav
    report = build(site, root=tmp_path)
    assert any("orphan" in warning for warning in report.warnings)
    assert report.ok and report.pages == 2  # an orphan is not built


def test_a_dead_link_on_an_orphan_page_is_still_reported(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text(
        "# Loose\n\nSee [gone](guides/missing.md) and [up](nowhere.md).\n"
    )
    report = build(site, root=tmp_path)
    dead = [error for error in report.errors if "dead link" in error]
    assert len(dead) == 2
    assert all("loose.html (orphan)" in error for error in dead)


def test_the_orphan_warning_and_its_link_errors_are_separate_signals(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text("# Loose\n\n[x](gone.md)\n")
    report = build(site, root=tmp_path)
    assert [w for w in report.warnings if "orphan page not in nav: loose.md" in w]
    assert [e for e in report.errors if "loose.html (orphan): dead link to gone.md" in e]


def test_an_orphan_may_link_to_a_nav_page_and_to_another_orphan(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text(
        "# Loose\n\n[nav](guides/routing.md#basics) and [sibling](other.md).\n"
    )
    (tmp_path / "docs" / "other.md").write_text("# Other\n")
    report = build(site, root=tmp_path)
    assert report.ok, report.errors


def test_a_nav_page_linking_to_an_orphan_is_still_dead(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text("# Home\n\n[loose](loose.md)\n")
    (tmp_path / "docs" / "loose.md").write_text("# Loose\n")
    report = build(site, root=tmp_path)
    assert any("index.html: dead link to loose.md" in e for e in report.errors)


def test_a_broken_anchor_on_an_orphan_page_is_reported(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "loose.md").write_text(
        "# Loose\n\n[a](#nope) and [b](guides/routing.md#nope).\n"
    )
    report = build(site, root=tmp_path)
    assert any("loose.html (orphan): broken anchor #nope" in e for e in report.errors)
    assert any("loose.html (orphan): link to missing anchor #nope" in e for e in report.errors)


def test_an_excluded_page_is_neither_an_orphan_nor_link_checked(tmp_path) -> None:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "guides" / "routing.md").write_text("# Routing\n")
    (docs / "plans").mkdir()
    (docs / "plans" / "draft.md").write_text("# Draft\n\n[x](gone.md)\n")
    site = Site("S", "docs", "out", Nav(Page("Home", "index.md")), exclude=("plans/",))
    report = build(site, root=tmp_path)
    assert not any("draft" in message for message in report.errors + report.warnings)


def test_content_tabs_need_no_javascript() -> None:
    out = render('=== "A"\n    one\n\n=== "B"\n    two\n')
    assert out.html.count('type="radio"') == 2
    assert out.html.count("tab-label") >= 2 and out.html.count("tab-panel") == 2
    assert "checked" in out.html  # the first tab is selected
    assert "<button" not in out.html


def test_two_tab_groups_on_a_page_are_independent() -> None:
    out = render(
        '=== "A"\n    one\n\n=== "B"\n    two\n\ntext\n\n=== "C"\n    three\n\n=== "D"\n    four\n'
    )
    names = set(re.findall(r'name="([^"]+)"', out.html))
    assert len(names) == 2, names


def test_the_tab_stylesheet_can_select_every_rendered_tab() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import _MAX_TABS, stylesheet

    css = stylesheet(THEMES["wreath"])
    for index in range(_MAX_TABS):
        assert f".tabbed>input:nth-of-type({index + 1}):checked" in css


def test_code_is_highlighted_and_still_escaped() -> None:
    out = render("```python\ndef f(): return 1 < 2  # c\n```\n")
    assert 'class="tok-keyword">def' in out.html
    assert "tok-comment" in out.html and "&lt;" in out.html


def test_admonition_and_table(tmp_path) -> None:
    table = render("| A | B |\n|:-:|--|\n| 1 | 2 |\n")
    assert "<table>" in table.html and "text-align:center" in table.html
    adm = render('!!! warning "Careful"\n    body text\n')
    assert 'class="admonition warning"' in adm.html and "admonition-title" in adm.html


def test_heading_and_table_parsers_keep_ambiguous_whitespace_out_of_backtracking() -> None:
    heading = render("# title" + " " * 2_000 + "x\n")
    invalid_id = render("# title" + " " * 2_000 + "{#bad!}\n")
    malformed_table = render("A | B\n---|---" + " " * 2_000 + "x\n")

    assert heading.title == "title" + " " * 2_000 + "x"
    assert invalid_id.title == "title" + " " * 2_000 + "{#bad!}"
    assert "<table>" not in malformed_table.html


def test_search_index_is_written(tmp_path) -> None:
    import json

    site = _site(tmp_path)
    build(site, root=tmp_path)
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    page_id = next(i for i, p in enumerate(index["p"]) if p["u"] == "guides/routing.html")
    assert index["p"][page_id]["t"] == "Routing"
    # One record per heading, not one blob per page: a hit can land on the
    # section the reader asked for, and its own text is the result snippet.
    section = next(s for s in index["s"] if s["p"] == page_id and s["a"] == "basics")
    assert section["h"] == "Basics"
    # The body is prose only: the heading lives in its own field, and the `#`
    # permalink is chrome. Both used to lead every snippet ("Basics # text").
    assert section["x"] == "text"
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'id="docs-search"' in routing and 'data-root="../"' in routing


def test_prev_next_and_frontmatter_description(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text(
        "---\ndescription: The landing page.\n---\n# Home\n\ntext\n"
    )
    build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'meta name="description" content="The landing page."' in index
    assert "page-nav" in index and "Routing" in index  # next
    assert "nav-prev" in routing and "Home" in routing  # prev


def test_a_declared_map_page_is_an_obvious_header_route(tmp_path) -> None:
    base = _site(tmp_path)
    site = dataclasses.replace(base, map_page="guides/routing.md")
    build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert '<a class="browse" href="guides/routing.html">Browse</a>' in index
    assert '<a class="browse" href="routing.html">Browse</a>' in routing


def test_a_map_page_must_be_in_the_navigation() -> None:
    with pytest.raises(ValueError, match="map_page.*is not in nav"):
        Site(
            "S",
            "docs",
            "out",
            Nav(Page("Home", "index.md")),
            map_page="map.md",
        )


def test_llms_txt_and_sitemap(tmp_path) -> None:
    site = Site(
        "Demo",
        "docs",
        "site",
        _site(tmp_path).nav,
        base_url="https://d.io",
        description="Demo docs.",
    )
    build(site, root=tmp_path)
    llms = (tmp_path / "site" / "llms.txt").read_text()
    assert "# Demo" in llms and "https://d.io/guides/routing.html" in llms
    sitemap = (tmp_path / "site" / "sitemap.xml").read_text()
    assert "<loc>https://d.io/index.html</loc>" in sitemap


def test_strict_anchor_validation(tmp_path) -> None:
    site = _site(tmp_path)
    (tmp_path / "docs" / "index.md").write_text(
        "# Home\n\n[bad](guides/routing.md#no-such-anchor)\n"
    )
    report = build(site, root=tmp_path)
    assert any("missing anchor" in error for error in report.errors)


def test_images_strikethrough_and_task_lists() -> None:
    out = render("![a pic](p.png)\n\n~~old~~ new\n\n- [ ] todo\n- [x] done\n")
    assert '<img src="p.png" alt="a pic" loading="lazy">' in out.html
    assert "<del>old</del>" in out.html
    assert 'type="checkbox" disabled>' in out.html and "disabled checked>" in out.html
    assert 'src="#"' in render("![x](javascript:alert(1))").html  # unsafe src rejected


def test_themes_and_feels_compose(tmp_path) -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    assert set(THEMES) == {"wreath", "slate", "sepia", "nord", "terminal", "signal"}
    assert "#5e81ac" in stylesheet(THEMES["nord"])  # nord primary
    assert "feTurbulence" in stylesheet(THEMES["sepia"], "papery")
    assert "--radius:0px" in stylesheet(THEMES["wreath"], "hardcore")
    assert "border-radius:999px" in stylesheet(THEMES["wreath"], "orby")
    assert "--hero-glow:1" in stylesheet(THEMES["signal"], "luminous")
    with pytest.raises(ValueError, match="unknown feel"):
        Site("s", "docs", "o", Nav(Page("H", "i.md")), feel="nope")


def test_build_applies_theme_and_feel(tmp_path) -> None:
    from wreath._docs import THEMES

    site = Site(
        "D", "docs", "site", _site(tmp_path).nav, palette=THEMES["terminal"], feel="hardcore"
    )
    build(site, root=tmp_path)
    css = (tmp_path / "site" / "assets" / "docs.css").read_text()
    assert "#16a34a" in css and "--radius:0px" in css


def test_chart_from_json_renders_svg(tmp_path) -> None:
    import json

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bench.json").write_text(
        json.dumps(
            {
                "results": [
                    {"name": "a", "rps": 100},
                    {"name": "b", "rps": 250},
                    {"name": "a", "rps": 180},
                ]
            }
        )
    )
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "guides" / "routing.md").write_text("# R\n")
    (src / "index.md").write_text(
        "# Bench\n\n```chart\nsource: ../data/bench.json\ndata: results\n"
        "x: name\ny: rps\nsort: desc\ntitle: RPS\n```\n"
    )
    build(_site_like(tmp_path), root=tmp_path)
    html = (tmp_path / "site" / "index.html").read_text()
    assert '<figure class="chart">' in html and "<svg" in html
    # Two bars; non-wreath labels use the muted competitor hatch.
    assert html.count('<rect x="168"') == 2 and "url(#wc-hatch-" in html
    # a bad source degrades to a visible note, not a crash
    (src / "index.md").write_text("# X\n\n```chart\nsource: gone.json\n```\n")
    build(_site_like(tmp_path), root=tmp_path)
    assert "chart-error" in (tmp_path / "site" / "index.html").read_text()


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("desc", ["b", "c", "a"]),
        ("asc", ["a", "c", "b"]),
        (None, ["a", "b", "c"]),
    ],
)
def test_a_chart_orders_its_bars_the_way_the_block_asked(tmp_path, sort, expected) -> None:
    from wreath._docs.charts import _render

    (tmp_path / "b.json").write_text(
        json.dumps(
            {
                "results": [
                    {"name": "a", "rps": 100},
                    {"name": "b", "rps": 250},
                    {"name": "c", "rps": 180},
                ]
            }
        )
    )
    config = {"source": "b.json", "data": "results", "x": "name", "y": "rps"}
    if sort is not None:
        config["sort"] = sort

    svg = _render(config, tmp_path)

    assert re.findall(r">([abc])<", svg) == expected


def test_robots_and_404(tmp_path) -> None:
    site = Site("D", "docs", "site", _site(tmp_path).nav, base_url="https://d.io")
    build(site, root=tmp_path)
    assert "Sitemap: https://d.io/sitemap.xml" in (tmp_path / "site" / "robots.txt").read_text()
    assert "Page not found" in (tmp_path / "site" / "404.html").read_text()


def _site_like(tmp_path):
    return Site(
        "Bench",
        "docs",
        "site",
        Nav(Page("Home", "index.md"), Section("G", Page("R", "guides/routing.md"))),
    )


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
    quiet = build(Site("S", "docs", "out", nav, strict=False, exclude=("plans/",)), root=tmp_path)
    assert not any("orphan" in w for w in quiet.warnings)


def _threaded_nav_site(tmp_path, **kwargs) -> Nav:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "guides" / "a.md").write_text("# A\n")
    (docs / "guides" / "b.md").write_text("# B\n")
    nav = Nav(
        Page("Home", "index.md"),
        Section("Guides", Section("Deep", Page("A", "guides/a.md"))),
        Section("Other", Page("B", "guides/b.md")),
    )
    build(Site("S", "docs", "out", nav, strict=False, **kwargs), root=tmp_path)
    return nav


def test_nav_sections_collapse_and_auto_open(tmp_path) -> None:
    _threaded_nav_site(tmp_path, tabs="never")
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    # The branch holding the current page is open; the sibling section is not.
    assert '<details class="sec sec-0 on-path" open><summary>Guides</summary>' in html
    assert '<details class="sec sec-0" open><summary>Other</summary>' not in html


def test_the_nav_thread_traces_only_the_branch_you_are_in(tmp_path) -> None:
    _threaded_nav_site(tmp_path, tabs="never")
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    nav = html.split('<nav class="side"')[1].split("</nav>")[0]
    assert nav.count("on-path") == 4  # two <details> and their two levels
    assert '<a class="nav-page active"' in nav and 'aria-current="page"' in nav
    other = nav.split("Other")[1]
    assert "on-path" not in other


def test_the_switcher_lifts_the_top_level_into_the_header(tmp_path) -> None:
    _threaded_nav_site(tmp_path)  # tabs="auto", three top-level entries
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    menu = html.split('<div class="sections-menu">')[1].split("</div>")[0]
    assert menu.count("<a ") == 3 and 'class="active"' in menu
    # ... and the sidebar then shows only the section you are inside.
    nav = html.split('<nav class="side"')[1].split("</nav>")[0]
    assert "Deep" in nav and "Other" not in nav


def test_the_switcher_names_the_section_it_is_closed_on(tmp_path) -> None:
    _threaded_nav_site(tmp_path)
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    here = html.split('<span class="sections-here">')[1].split("</span>")[0]
    assert here == "Guides"


def test_the_header_carries_no_second_row(tmp_path) -> None:
    _threaded_nav_site(tmp_path)
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    assert '<nav class="tabs"' not in html
    assert "has-tabs" not in html


def test_a_page_outside_any_section_drops_the_sidebar(tmp_path) -> None:
    _threaded_nav_site(tmp_path)
    html = (tmp_path / "out" / "index.html").read_text()
    assert '<nav class="side"' not in html
    assert 'class="layout no-side"' in html


def test_the_reading_column_stays_put_without_a_sidebar() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    assert ".layout.no-side>main{grid-column:2;}" in css
    # ... and released again once the grid collapses to a single track.
    assert ".layout.no-side>main{grid-column:auto;}" in css


def test_chart_colors_wreath_arms_distinctly() -> None:
    from wreath._docs import charts

    svg = charts._svg_bar(
        [
            ("Wreath (metal)", 3.0),
            ("Wreath (native)", 2.0),
            ("Wreath (ASGI)", 1.5),
            ("BlackSheep", 1.0),
        ],
        "t",
        "",
    )
    # The arms differ by how much of the stack is native, which is an ordered
    # quantity, so they are one hue at three strengths rather than three hues.
    # Two of the four used to be hard-coded hexes and went off-palette in every
    # theme but the default.
    bars = re.findall(r"<rect x=\"168\"[^>]*fill=\"([^\"]+)\"", svg)
    arms = [fill for fill in bars if "primary" in fill]
    assert len(arms) == 3 and len(set(arms)) == 3, arms
    # No bar carries a fixed hex: that is what took two of the four arms
    # off-palette in every theme but the default.
    assert not [fill for fill in bars if fill.startswith("#")], bars
    assert "url(#wc-hatch-" in bars[-1]  # the field is hatched


def test_two_charts_on_a_page_do_not_share_a_pattern_id() -> None:
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
        "# Home\n\n```chart\nsource: data/d.json\ndata: results\nx: k\ny: v\n```\n"
    )
    build(Site("S", "docs", "out", Nav(Page("Home", "index.md")), strict=False), root=tmp_path)
    out = (tmp_path / "out" / "index.html").read_text()
    assert '<figure class="chart">' in out and "chart-error" not in out
    # The data file the chart read is copied into the site so its link resolves.
    assert (tmp_path / "out" / "data" / "d.json").is_file()


def test_cli_build_end_to_end(tmp_path) -> None:
    from wreath._docs.cli import execute

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


# The theme is CSS, which is where unexplained numbers breed. These pin the
# properties that make it a system rather than a pile of values, and the
# accessibility floor that `wreath audit` would otherwise never see (its
# contrast rules only read inline <style>, so they were dormant while the whole
# stylesheet was an external file).


def _all_themes():
    from wreath._docs import THEMES

    return sorted(THEMES.items())


def test_every_theme_meets_aa_in_both_modes() -> None:
    from wreath._audit.contrast import contrast_ratio

    failures = []
    for name, p in _all_themes():
        pairs = {
            "body": (p.fg, p.bg),
            "muted": (p.muted, p.bg),
            "link": (p.link or p.primary, p.bg),
            "on-surface": (p.fg, p.surface),
            "dark body": (p.dark_fg, p.dark_bg),
            "dark muted": (p.dark_muted, p.dark_bg),
            "dark link": (p.dark_link or p.primary, p.dark_bg),
            "dark on-surface": (p.dark_fg, p.dark_surface),
        }
        for role, (fg, bg) in pairs.items():
            ratio = contrast_ratio(fg, bg) or 0.0
            if ratio < 4.5:
                failures.append(f"{name} {role}: {ratio:.2f}:1")
    assert not failures, "below WCAG AA (4.5:1): " + ", ".join(failures)


def test_control_boundaries_meet_non_text_contrast() -> None:
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
    from wreath._docs import THEMES
    from wreath._docs.theme import critical_css

    for name, palette in THEMES.items():
        size = len(critical_css(palette).encode("utf-8"))
        assert size <= 16 * 1024, f"{name} critical CSS is {size} bytes (budget 16 KiB)"


def test_a_built_page_carries_its_tokens_inline() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import page

    html = page(
        site_name="D",
        page_title="P",
        content="<p>x</p>",
        nav_html="",
        toc_html="",
        css_href="assets/docs.css",
        palette=THEMES["nord"],
    )
    assert "<style>" in html
    assert "--fg:" in html and "--bg:" in html


def test_no_built_page_reaches_the_network(tmp_path) -> None:
    import re

    build(_site(tmp_path), root=tmp_path)
    external = re.compile(r'(?:src|href)\s*=\s*"(?:https?:)?//')
    for path in (tmp_path / "site").rglob("*.html"):
        assert not external.search(path.read_text()), f"{path.name} loads a remote asset"


def test_motion_is_optional() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    assert "prefers-reduced-motion" in css


def test_the_type_and_space_scales_are_declared_once() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import critical_css

    css = critical_css(THEMES["wreath"])
    for token in ("--text-base", "--text-3xl", "--space-1", "--space-6", "--measure"):
        assert token in css, f"missing design token {token}"


def test_syntax_colours_are_tinted_into_the_theme() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["sepia"])
    for github_hex in ("#032f62", "#d73a49", "#6f42c1", "#e36209"):
        assert github_hex not in css, f"{github_hex} is a fixed GitHub token colour"
    assert "--tok-keyword:color-mix(in oklab," in css and "var(--fg))" in css
    assert css.count("--tok-keyword") >= 2, "light and dark need their own hue"


def test_syntax_tokens_stay_legible_on_every_code_surface() -> None:
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
        mixed = (unlin(lin(x) * pct + lin(y) * (1 - pct)) for x, y in zip(a, b, strict=True))
        return "#" + "".join(f"{channel:02x}" for channel in mixed)

    hexes = re.compile(r"--tok-(\w+):color-mix\(in oklab, (#[0-9a-f]{6})")
    failures = []
    for name, p in THEMES.items():
        for is_light, fg, surface in ((True, p.fg, p.surface), (False, p.dark_fg, p.dark_surface)):
            for token, hue in hexes.findall(_syntax(is_light)):
                ratio = contrast_ratio(mix(hue, fg, _TINT / 100), surface) or 0.0
                if ratio < 4.5:
                    mode = "light" if is_light else "dark"
                    failures.append(f"{name} {mode} {token}: {ratio:.2f}:1")
    assert not failures, "syntax token below AA: " + ", ".join(failures)


def test_no_synthetic_font_weights() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    for weight in ("font-weight:750", "font-weight:650", "font-weight:550"):
        assert weight not in css, f"{weight} is not a system-font weight"


def test_a_wide_table_scrolls_itself(tmp_path) -> None:
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert 'class="table-wrap"' in out.html
    assert out.html.index("table-wrap") < out.html.index("<table")


def test_table_headers_are_scoped() -> None:
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert out.html.count('<th scope="col"') == 2


def test_the_page_is_navigable_by_keyboard_and_screen_reader() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import page

    html = page(
        site_name="D",
        page_title="P",
        content="<p>x</p>",
        nav_html="<a href='x.html'>X</a>",
        toc_html="<a href='#y'>Y</a>",
        css_href="assets/docs.css",
        palette=THEMES["wreath"],
    )
    assert 'href="#content"' in html  # skip link
    assert 'id="content"' in html  # and its target
    assert html.count("aria-label") >= 4  # both nav landmarks labelled
    assert 'role="listbox"' not in html  # invalid with <a> children


def test_every_theme_builds_a_whole_site(tmp_path) -> None:
    from wreath._docs import THEMES

    for name, palette in THEMES.items():
        root = tmp_path / name
        root.mkdir()
        src = root / "docs"
        (src / "guides").mkdir(parents=True)
        (src / "index.md").write_text("# Home\n\ntext\n")
        (src / "guides" / "routing.md").write_text("# Routing\n\n## S\n\n`x`\n")
        site = Site(
            "D",
            "docs",
            "site",
            Nav(Page("Home", "index.md"), Section("G", Page("R", "guides/routing.md"))),
            palette=palette,
        )
        report = build(site, root=root)
        assert report.ok, f"{name}: {report.errors}"
        assert (root / "site" / "index.html").read_text().startswith("<!doctype html>")


def _apidoc_site(tmp_path, directive: str):
    src = tmp_path / "docs"
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.md").write_text(f"# Home\n\n{directive}\n")
    return Site(
        name="Demo",
        source="docs",
        output="site",
        nav=Nav(Page("Home", "index.md")),
        strict=True,
    )


def test_a_broken_autodoc_target_fails_a_strict_build(tmp_path) -> None:
    report = build(_apidoc_site(tmp_path, "::: wreath.nope.NotAThing"), root=tmp_path)

    assert not report.ok, "a directive naming a missing target must fail --strict"
    assert any("wreath.nope.NotAThing" in e for e in report.errors)
    # The inline note still renders, so a reader of the page sees it too.
    assert "API reference unavailable" in (tmp_path / "site" / "index.html").read_text()


def test_a_broken_autodoc_target_is_only_a_warning_when_not_strict(tmp_path) -> None:
    site = _apidoc_site(tmp_path, "::: wreath.nope.NotAThing")
    report = build(
        Site(
            name=site.name,
            source=site.source,
            output=site.output,
            nav=site.nav,
            strict=False,
        ),
        root=tmp_path,
    )

    assert report.ok, "a local preview still builds"
    assert any("wreath.nope.NotAThing" in w for w in report.warnings)


def test_a_renderer_bug_is_not_reported_as_a_missing_target(monkeypatch) -> None:
    from wreath._docs import apidoc

    def exploding(path):
        raise AttributeError("renderer bug")

    monkeypatch.setattr(apidoc, "_render_object", exploding)
    with pytest.raises(AttributeError, match="renderer bug"):
        apidoc.expand("::: wreath.response.JSONResponse\n")


def test_a_missing_target_names_what_was_missing() -> None:
    from wreath._docs.apidoc import TargetNotFound, _import

    with pytest.raises(TargetNotFound, match="has no attribute 'NotAThing'"):
        _import("wreath.response.NotAThing")
    with pytest.raises(TargetNotFound, match="cannot import"):
        _import("wreath.not_a_module.Thing")


def test_a_wrapped_list_item_still_renders_its_markup() -> None:
    out = render(
        "- A bullet long enough to wrap, with `WREATH_BUILD_HTTP3=1` and a\n"
        "  [link](guides/routing.md) on the second line.\n"
    )
    assert "<code>WREATH_BUILD_HTTP3=1</code>" in out.html
    assert 'href="guides/routing.md"' in out.html
    assert "`" not in out.html and "](" not in out.html


def test_a_code_fence_can_name_its_file_and_shade_lines() -> None:
    out = render('```python title="app.py" hl_lines="2 4-5"\none\ntwo\nthree\nfour\nfive\n```\n')
    assert '<span class="code-title">app.py</span>' in out.html
    assert '<span class="code-lang">python</span>' in out.html
    assert out.html.count('<span class="hl">') == 3  # line 2, 4, 5
    # A fence with neither keeps the plain block: a language chip over every one
    # of a corpus's code blocks is noise, not information.
    assert "code-head" not in render("```python\none\n```\n").html


def test_line_shading_survives_a_multiline_token() -> None:
    out = render('```python hl_lines="2"\nx = 1\ny = """a\nb"""\n```\n')
    assert out.html.count('<span class="hl">') == 1
    assert out.html.count("<span") == out.html.count("</span>")


def test_an_admonition_can_be_collapsible() -> None:
    assert "<details" in render('??? note "Later"\n    body\n').html
    assert "<details" in render('???+ note "Open"\n    body\n').html
    assert " open>" in render('???+ note "Open"\n    body\n').html
    assert " open>" not in render('??? note "Later"\n    body\n').html
    assert "<details" not in render('!!! note "Always"\n    body\n').html


def test_the_runtime_is_an_external_enhancement(tmp_path) -> None:
    from wreath._docs.scripts import BOOT, runtime

    build(_site(tmp_path), root=tmp_path)
    js = (tmp_path / "site" / "assets" / "docs.js").read_text()
    assert js == runtime() and len(js) > 4000
    html = (tmp_path / "site" / "index.html").read_text()
    # Only the anti-flash boot is inlined; the rest is fetched once and cached.
    assert BOOT in html and "addCopyButtons" not in html
    assert 'src="assets/docs.js" defer' in html


def test_the_theme_control_offers_all_three_states(tmp_path) -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import page, stylesheet

    html = page(
        site_name="D",
        page_title="P",
        content="<p>x</p>",
        nav_html="",
        toc_html="",
        css_href="a.css",
        palette=THEMES["wreath"],
    )
    assert 'data-mode="system"' in html
    css = stylesheet(THEMES["wreath"])
    for mode in ("system", "light", "dark"):
        assert f".theme[data-mode={mode}]" in css


def test_source_url_adds_an_edit_link(tmp_path) -> None:
    plain = _site(tmp_path)
    build(
        dataclasses.replace(plain, source_url="https://git.example/edit/main/docs"), root=tmp_path
    )
    routing = (tmp_path / "site" / "guides" / "routing.html").read_text()
    assert 'href="https://git.example/edit/main/docs/guides/routing.md"' in routing
    assert "Edit this page" in routing
    # ... and no link at all when the site does not say where its source lives.
    build(plain, root=tmp_path)
    assert "Edit this page" not in (tmp_path / "site" / "guides" / "routing.html").read_text()


def test_a_palette_names_its_two_type_voices() -> None:
    from wreath._docs import THEMES
    from wreath._docs.config import Palette
    from wreath._docs.theme import critical_css

    css = critical_css(THEMES["wreath"])
    assert "--font:" in css and "--font-display:" in css and "--font-mono:" in css
    with pytest.raises(ValueError, match="Palette.display"):
        Site("s", "d", "o", Nav(Page("A", "a.md")), palette=Palette(display="comic"))


def test_a_hero_becomes_the_pages_h1_and_title(tmp_path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text(
        "```hero\n"
        "eyebrow: The request path\n"
        "title: Most of a request never reaches Python.\n"
        "lede: Routing and authorization are native code.\n"
        "action: See the benchmarks -> other.md\n"
        "```\n\ntext\n"
    )
    (docs / "other.md").write_text("# Other\n")
    build(
        Site("S", "docs", "out", Nav(Page("Home", "index.md"), Page("Other", "other.md"))),
        root=tmp_path,
    )
    html = (tmp_path / "out" / "index.html").read_text()
    assert html.count("<h1") == 1 and 'class="hero-title"' in html
    assert "<title>Most of a request never reaches Python. · S</title>" in html
    assert 'class="hero-eyebrow"' in html and 'class="hero-lede"' in html
    assert 'href="other.html"' in html


def test_a_hero_can_name_the_signals_it_demonstrates() -> None:
    from wreath._docs import hero

    _, tokens = hero.extract(
        "```hero\ntitle: Systems under pressure\nsignal: realtime\nsignal: durable work\n```\n"
    )
    rendered = next(iter(tokens.values()))
    assert 'class="hero-signals"' in rendered
    assert "realtime" in rendered and "durable work" in rendered


def test_story_cards_are_link_checked_and_bounded(tmp_path) -> None:
    from wreath._docs import cards

    source = "```cards\n" + "\n".join(
        f"card: Story {index} | A system under pressure. | story-{index}.md | realtime"
        for index in range(14)
    ) + "\n```\n"
    _, tokens = cards.extract(source)
    rendered = next(iter(tokens.values()))
    assert rendered.count('class="story-card"') == 12
    assert 'href="story-0.md"' in rendered

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text(
        "```cards\ncard: Missing | Not here. | missing.md | refusal\n```\n"
    )
    report = build(Site("S", "docs", "out", Nav(Page("Home", "index.md"))), root=tmp_path)
    assert not report.ok
    assert any("dead link" in error for error in report.errors)


def test_a_hero_link_to_a_missing_page_fails_the_build(tmp_path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text("```hero\ntitle: Hello\naction: Gone -> nowhere.md\n```\n")
    report = build(Site("S", "docs", "out", Nav(Page("Home", "index.md"))), root=tmp_path)
    assert not report.ok
    assert any("dead link" in error for error in report.errors)


def test_every_figure_draws_and_stays_inside_its_viewbox() -> None:
    from wreath._docs import figures

    assert set(figures.FIGURES) == {"request-boundary", "route-bitset", "timing-wheel"}
    for name, draw in figures.FIGURES.items():
        svg = draw("uid")
        assert svg.count("<g") == svg.count("</g>"), name
        width = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
        for match in re.finditer(r"<text[^>]*x=\"(\d+)\"[^>]*>([^<]*)<", svg):
            x, label = int(match.group(1)), match.group(2)
            span = len(label) * 6.6  # a generous mono advance
            right = x + span / 2 if "middle" in match.group(0) else x + span
            assert right <= width, f"{name}: {label!r} runs to {right:.0f} of {width}"


def test_an_unknown_figure_says_so_rather_than_drawing_nothing() -> None:
    from wreath._docs import figures

    _, tokens = figures.extract("```figure\nname: not-a-figure\n```\n")
    rendered = next(iter(tokens.values()))
    assert "chart-error" in rendered and "not-a-figure" in rendered
    assert "request-boundary" in rendered  # ... and lists what it does have


def test_the_bitset_figure_agrees_with_the_matching_it_illustrates() -> None:
    from wreath._native._core import PolicyRouteTable

    from wreath._docs.figures import _COLUMNS, _ROUTES

    table = PolicyRouteTable()
    for route, _ in _ROUTES:
        table.add(route, "GET", route)
    matched = table.match("GET", "/orders/42/items")
    assert matched is not None
    survivors = [route for route, alive in _ROUTES if alive == len(_COLUMNS) - 1]
    assert survivors == [matched[0]], (survivors, matched)


def test_a_figure_can_be_stopped_without_javascript(tmp_path) -> None:
    from wreath._docs import THEMES, figures
    from wreath._docs.theme import stylesheet

    _, tokens = figures.extract("```figure\nname: timing-wheel\n```\n")
    rendered = next(iter(tokens.values()))
    assert 'type="checkbox"' in rendered and "fig-pause" in rendered
    css = stylesheet(THEMES["wreath"])
    assert ".fig:has(.fig-pause:checked) .fig-body *{animation-play-state:paused" in css


def test_figures_are_readable_with_motion_turned_off() -> None:
    from wreath._docs import THEMES
    from wreath._docs.theme import stylesheet

    css = stylesheet(THEMES["wreath"])
    reduced = css.split("prefers-reduced-motion")[1]
    for revealed in (".f-fold", ".f-cell-on", ".f-pip", ".f-mark"):
        assert revealed in reduced, revealed


def test_a_block_inside_a_longer_fence_is_an_example_not_a_block() -> None:
    from wreath._docs import figures, hero

    page = (
        "````markdown\n"
        "```hero\n"
        "title: An example\n"
        "action: Nowhere -> nowhere.md\n"
        "```\n"
        "````\n\n"
        "```hero\ntitle: The real one\n```\n"
    )
    text, tokens = hero.extract(page)
    assert len(tokens) == 1
    assert "The real one" in next(iter(tokens.values()))
    assert "An example" in text and "nowhere.md" in text  # left as source

    fenced = "````markdown\n```figure\nname: timing-wheel\n```\n````\n"
    _, none = figures.extract(fenced)
    assert not none


def test_a_tilde_fence_encloses_a_block_the_same_way() -> None:
    from wreath._docs import hero

    text, tokens = hero.extract("~~~~\n```hero\ntitle: Example\n```\n~~~~\n")
    assert not tokens and "title: Example" in text


def test_double_backticks_swallow_the_prose_between_two_literals() -> None:
    html = render("A ``pass`` entry and a ``fail`` entry both appear.\n").html
    assert "<code> entry and a </code>" in html  # the prose, eaten


def test_rest_roles_and_literal_blocks_render_as_damage() -> None:
    # A role prints its own name into the sentence...
    assert ":class:<code>" in render("See :class:`HealthCheck` now.\n").html
    # ...and a `::` literal block loses the code formatting entirely.
    block = render("Mount it::\n\n    app.health()\n").html
    assert "<code" not in block and "::</p>" in block


def test_rest_markup_finds_each_construct() -> None:
    from wreath._docs import apidoc

    found = apidoc.rest_markup("A ``literal`` and :class:`Thing` and a block::\n\n    x = 1\n")
    assert "``literal``" in found
    assert ":class:`" in found
    assert "::" in found


def test_rest_markup_ignores_fenced_examples() -> None:
    from wreath._docs import apidoc

    assert apidoc.rest_markup("```text\nuse ``this`` in Sphinx\n```\n") == []


def test_a_strict_build_refuses_rest_markup_in_a_rendered_docstring() -> None:
    from wreath._docs import apidoc

    class Fixture:
        """Prose with a ``literal`` in it."""

    module = type(apidoc)("fixture_module")
    module.Fixture = Fixture
    Fixture.__module__ = "fixture_module"
    module.__all__ = ["Fixture"]
    import sys

    sys.modules["fixture_module"] = module
    try:
        sink: list[str] = []
        apidoc.expand("::: fixture_module", "reference/fixture.md", sink)
    finally:
        del sys.modules["fixture_module"]

    assert len(sink) == 1, sink
    assert "reference/fixture.md" in sink[0]
    assert "``literal``" in sink[0]
    assert "single backticks" in sink[0]


def test_the_rest_gate_has_no_exemption_list() -> None:
    from wreath._docs import apidoc

    assert not hasattr(apidoc, "REST_PENDING")
    assert not hasattr(apidoc, "_rest_pending")


def test_apidoc_renders_properties() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.request.Request")
    for name in ("method", "path", "headers", "cookies", "query_string"):
        assert f"#### `{name}` *(property)*" in out, name
    # A property is read, not called: it shows its type, not a call signature.
    assert "```python\nmethod: str\n```" in out


def test_apidoc_renders_classmethods_and_marks_them() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.response.FileResponse")
    assert "#### `from_descriptor` *(classmethod)*" in out
    # `cls` is dropped the way `self` is -- a caller passes neither.
    assert "from_descriptor(cls" not in out


def test_apidoc_renders_inherited_members_and_names_the_base() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.orm.constraints.Ge")
    assert "#### `source` *(inherited from `_Comparison`)*" in out
    assert "#### `check_type` *(inherited from `Check`)*" in out


def test_apidoc_stops_inheriting_at_classes_wreath_does_not_own() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.health.PassesUnhealthy")
    assert "args" not in out.replace("Args", "")
    assert "add_note" not in out


def test_apidoc_marks_coroutine_functions_async() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.postgres.Pool")
    # `acquire` grew a `shared` keyword when the pool learned to batch, so this
    # matches the `async` marker and the name rather than the whole signature --
    # what the test is for is the marker, not the parameter list.
    assert "async acquire(" in out
    assert "async release(connection" in out
    assert "async " not in out.split("#### `snapshot`")[1].split("```")[1]


def test_apidoc_strips_annotation_repr_quotes() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.postgres.Pool")
    assert "-> 'Any'" not in out and "-> Any" in out
    assert ": 'int'" not in out


def test_apidoc_skips_slots_and_non_members() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.health.HealthCheck")
    assert "#### `name`" not in out  # a slot, not a method
    assert "HealthCheck(" in out  # ...but the field is in the signature


def test_apidoc_renders_module_level_type_instances() -> None:
    from wreath._docs import apidoc

    out = apidoc.expand("::: wreath.orm.types")
    for name in ("Int64", "Text", "Jsonb", "Uuid", "TimestampTz"):
        assert f"### `{name}` *(value)*" in out, name
    # The declaration shows the type it is an instance of and what it reprs as,
    # which is where the OID a reader is checking actually lives.
    assert "Int64: PgType = <PgType int8 oid=20>" in out


def test_a_documentable_value_needs_a_type_the_module_itself_defines() -> None:
    import sys

    from wreath._docs import apidoc

    class Widget:
        def __repr__(self) -> str:
            return "<Widget>"

    module = type(apidoc)("value_fixture")
    Widget.__module__ = "value_fixture"
    module.Widget = Widget
    module.WIDGET = Widget()  # own type, own repr -> documented
    module.IMPORTED = sys.maxsize  # not this module's type -> skipped
    module.__all__ = ["Widget", "WIDGET", "IMPORTED"]
    sys.modules["value_fixture"] = module
    try:
        out = apidoc.expand("::: value_fixture")
    finally:
        del sys.modules["value_fixture"]

    assert "### `WIDGET` *(value)*" in out
    assert "WIDGET: Widget = <Widget>" in out
    assert "IMPORTED" not in out


def test_a_value_with_no_repr_of_its_own_is_not_documented() -> None:
    import sys

    from wreath._docs import apidoc

    class Plain:
        pass

    module = type(apidoc)("plain_fixture")
    Plain.__module__ = "plain_fixture"
    module.Plain = Plain
    module.ANON = Plain()
    module.__all__ = ["Plain", "ANON"]
    sys.modules["plain_fixture"] = module
    try:
        out = apidoc.expand("::: plain_fixture")
    finally:
        del sys.modules["plain_fixture"]

    assert "### `ANON`" not in out
    assert "0x" not in out


def test_a_value_documents_its_own_docstring_over_its_class_one() -> None:
    import sys

    from wreath._docs import apidoc

    class Marker:
        """What a marker is in general."""

    module = type(apidoc)("doc_fixture")
    Marker.__module__ = "doc_fixture"
    module.Marker = Marker
    module.SPECIFIC = Marker()
    module.SPECIFIC.__doc__ = "The one the router uses."
    module.__all__ = ["Marker", "SPECIFIC"]
    sys.modules["doc_fixture"] = module
    try:
        out = apidoc.expand("::: doc_fixture")
    finally:
        del sys.modules["doc_fixture"]

    assert "### `SPECIFIC` *(value)*" in out
    assert "The one the router uses." in out


def test_a_signature_default_never_prints_a_heap_address() -> None:
    import sys

    from wreath._docs import apidoc

    module = type(apidoc)("address_fixture")

    def fallback() -> None: ...

    sentinel = object()

    def handler(hook=fallback, missing=sentinel) -> None:
        """Takes a hook."""

    for obj in (fallback, handler):
        obj.__module__ = "address_fixture"
    module.handler = handler
    module.__all__ = ["handler"]
    sys.modules["address_fixture"] = module
    try:
        out = apidoc.expand("::: address_fixture")
    finally:
        del sys.modules["address_fixture"]

    assert "hook=<function test_a_signature_default_never_prints_a_heap_address" in out
    assert "fallback>" in out
    assert "missing=<object object>" in out
    assert " at 0x" not in out


def test_a_strict_build_refuses_a_module_directive_with_no_members() -> None:
    import sys

    from wreath._docs import apidoc

    facade = type(apidoc)("facade_fixture")
    facade.__doc__ = "A facade with nothing of its own."
    facade.__all__ = []
    sys.modules["facade_fixture"] = facade
    try:
        sink: list[str] = []
        apidoc.expand("::: facade_fixture", "reference/facade.md", sink)
    finally:
        del sys.modules["facade_fixture"]

    assert len(sink) == 1, sink
    assert "reference/facade.md" in sink[0]
    assert "no members" in sink[0]


def test_a_facade_is_allowed_beside_the_submodules_that_document_it() -> None:
    import sys

    from wreath._docs import apidoc

    facade = type(apidoc)("facade2")
    facade.__all__ = []
    part = type(apidoc)("facade2.part")

    class Thing:
        """A thing."""

    Thing.__module__ = "facade2.part"
    part.Thing = Thing
    part.__all__ = ["Thing"]
    sys.modules["facade2"] = facade
    sys.modules["facade2.part"] = part
    try:
        sink: list[str] = []
        apidoc.expand("::: facade2\n\n::: facade2.part", "reference/f.md", sink)
    finally:
        del sys.modules["facade2.part"]
        del sys.modules["facade2"]

    assert sink == []


def test_no_reference_page_is_waived_out_of_having_an_api() -> None:
    from wreath._docs import apidoc

    assert not hasattr(apidoc, "EMPTY_MODULE_OK")


#: The five facades that rendered no API at all, with the names of their
#: `__all__` that still do not render. Every one is a constant of a *builtin*
#: type -- a `str` or a `tuple` -- which `_is_documentable_value` refuses for
#: the reason it states: its type is not one this package minted, so there is
#: nothing to render but the name the module's prose already carries. The list
#: is pinned rather than derived so that a class or a function joining it turns
#: this red instead of quietly shrinking the reference.
_UNRENDERED_CONSTANTS = {
    "wreath.auth": set(),
    "wreath.authorization": {"PERMISSION_CHANNEL"},
    "wreath.compression": {"ZSTD_DEFAULT_LEVEL", "ZSTD_MAX_LEVEL", "ZSTD_MIN_LEVEL"},
    "wreath.mcp": {"PROTOCOL_VERSION", "SUPPORTED_PROTOCOL_VERSIONS"},
    "wreath.port": {"NEEDS_REVIEW", "TRANSLATED", "UNSUPPORTED"},
}


@pytest.mark.parametrize("path", sorted(_UNRENDERED_CONSTANTS))
def test_a_facade_over_a_private_package_renders_its_whole_public_api(path: str) -> None:
    import importlib

    from wreath._docs import apidoc

    module = importlib.import_module(path)
    rendered = {name for name, _, _ in apidoc._module_members(module)}
    assert rendered, f"{path} renders no API at all"
    missing = set(module.__all__) - rendered
    assert missing == _UNRENDERED_CONSTANTS[path], path
    for name in sorted(missing):
        assert type(getattr(module, name)).__module__ == "builtins", (
            f"{path}.{name} is not a plain constant; it should be rendered"
        )


def test_a_re_export_is_api_only_where_its_module_declares_it() -> None:
    import sys

    from wreath._docs import apidoc

    private = type(apidoc)("pkg_fixture._impl")

    class Exported:
        """Public."""

    class Internal:
        """Not public."""

    Exported.__module__ = Internal.__module__ = "pkg_fixture._impl"
    private.Exported, private.Internal = Exported, Internal

    facade = type(apidoc)("pkg_fixture")
    facade.Exported, facade.Internal = Exported, Internal
    facade.__all__ = ["Exported"]

    undeclared = type(apidoc)("pkg2_fixture")
    undeclared.Exported = Exported

    sys.modules["pkg_fixture"] = facade
    sys.modules["pkg_fixture._impl"] = private
    sys.modules["pkg2_fixture"] = undeclared
    try:
        out = apidoc.expand("::: pkg_fixture")
        bare = apidoc.expand("::: pkg2_fixture")
    finally:
        for name in ("pkg_fixture", "pkg_fixture._impl", "pkg2_fixture"):
            del sys.modules[name]

    assert "### `Exported`" in out
    assert "### `Internal`" not in out  # imported, never declared
    assert "### `Exported`" not in bare  # a module with no __all__ declares nothing


def test_a_private_source_in_another_package_is_not_this_module_s_api() -> None:
    import sys

    from wreath._docs import apidoc

    foreign = type(apidoc)("_elsewhere")

    class Borrowed:
        """Theirs."""

    Borrowed.__module__ = "_elsewhere"
    foreign.Borrowed = Borrowed

    facade = type(apidoc)("own_fixture")
    facade.Borrowed = Borrowed
    facade.__all__ = ["Borrowed"]
    sys.modules["own_fixture"] = facade
    sys.modules["_elsewhere"] = foreign
    try:
        sink: list[str] = []
        out = apidoc.expand("::: own_fixture", "reference/own.md", sink)
    finally:
        del sys.modules["own_fixture"]
        del sys.modules["_elsewhere"]

    assert "### `Borrowed`" not in out
    assert len(sink) == 1 and "no members" in sink[0]


def test_a_class_never_borrows_a_docstring_from_a_foreign_base() -> None:
    import sys
    from typing import Protocol

    from wreath._docs import apidoc

    module = type(apidoc)("doc_inherit_fixture")

    class Backend(Protocol):
        def check(self) -> bool: ...

    class Base:
        """What the base means."""

    class Derived(Base):
        pass

    for cls in (Backend, Base, Derived):
        cls.__module__ = "doc_inherit_fixture"
    module.Backend, module.Base, module.Derived = Backend, Base, Derived
    module.__all__ = ["Backend", "Base", "Derived"]
    sys.modules["doc_inherit_fixture"] = module
    try:
        out = apidoc.expand("::: doc_inherit_fixture")
    finally:
        del sys.modules["doc_inherit_fixture"]

    assert "Base class for protocol classes" not in out
    assert "### `Backend`" in out  # still rendered, just wordless
    assert out.count("What the base means.") == 2  # Base, and Derived inheriting it


def test_a_repo_link_names_the_repository_without_counts(tmp_path) -> None:
    from wreath._docs import Repo

    site = dataclasses.replace(_site(tmp_path), repo=Repo("https://github.com/you/proj"))
    build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    assert 'href="https://github.com/you/proj"' in index
    assert ">you/proj<" in index  # owner/name, derived
    assert "repo-stats" not in index  # nothing was fetched


def test_repo_counts_are_baked_in_at_build_time(tmp_path, monkeypatch) -> None:
    from wreath._docs import Repo
    from wreath._docs import repo as repo_mod

    calls = []

    def fake_get(host, slug, warnings):
        calls.append((host, slug))
        return {"stargazers_count": 1234, "forks_count": 7}

    monkeypatch.setattr(repo_mod, "_CACHE", {})
    monkeypatch.setattr(repo_mod, "_get", fake_get)
    site = dataclasses.replace(
        _site(tmp_path), repo=Repo("https://github.com/you/proj", stats=True)
    )
    report = build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    assert ">1.2k<" in index and ">7<" in index  # compacted, not raw
    assert "1234 stars, 7 forks" in index  # ...but exact for a reader
    assert not report.warnings
    # Once for the whole site, not once per page, and never from the page itself.
    assert calls == [("github", "you/proj")]
    assert "api.github.com" not in index


def test_an_unreachable_host_costs_the_counts_and_nothing_else(tmp_path, monkeypatch) -> None:
    import urllib.error

    from wreath._docs import Repo
    from wreath._docs import repo as repo_mod

    def boom(request, timeout=0):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(repo_mod, "_CACHE", {})
    monkeypatch.setattr(repo_mod.urllib.request, "urlopen", boom)
    site = dataclasses.replace(
        _site(tmp_path), strict=True, repo=Repo("https://github.com/you/proj", stats=True)
    )
    report = build(site, root=tmp_path)
    assert report.ok and not report.errors
    assert any("repo stats" in w for w in report.warnings)
    index = (tmp_path / "site" / "index.html").read_text()
    assert 'href="https://github.com/you/proj"' in index and "repo-stats" not in index


def test_repo_stats_need_a_host_with_an_api() -> None:
    from wreath._docs import Repo

    Repo("https://git.example/you/proj")  # a plain link is fine
    with pytest.raises(ValueError):
        Repo("https://git.example/you/proj", stats=True)  # ...counts are not
    with pytest.raises(ValueError):
        Repo("ftp://github.com/you/proj")


def test_compact_counts_round_the_way_a_reader_reads_them() -> None:
    from wreath._docs.repo import compact

    assert [compact(n) for n in (0, 999, 1000, 1234, 12_500, 1_000_000, 2_400_000)] == [
        "0",
        "999",
        "1k",
        "1.2k",
        "12.5k",
        "1M",
        "2.4M",
    ]


def test_header_links_draw_only_built_in_marks(tmp_path) -> None:
    from wreath._docs import ICONS, Link
    from wreath._docs.theme import ICON_MARKS

    # A name config accepts is a mark the theme can draw. Nothing else is drawable.
    assert set(ICON_MARKS) == set(ICONS)
    with pytest.raises(ValueError):
        Link("Chat", "https://example.test", icon="discord")  # not in the registry
    with pytest.raises(ValueError):
        Link("Home", "javascript:alert(1)")  # not a link at all
    site = dataclasses.replace(
        _site(tmp_path), links=(Link("Wreath on PyPI", "https://pypi.test/p/w", icon="package"),)
    )
    build(site, root=tmp_path)
    index = (tmp_path / "site" / "index.html").read_text()
    assert 'href="https://pypi.test/p/w"' in index
    # The name is *visible*, not an `aria-label` on a bare glyph. The links used
    # to be unlabelled icons in the bar whose only name was for a screen reader,
    # which left every sighted reader guessing at a wireframe cube; in the menu
    # the label is on screen and is the accessible name for everyone.
    assert ">Wreath on PyPI<" in index
    assert "<img" not in index  # a mark, not a badge fetched from a service


def test_the_stemmer_agrees_with_its_twin_in_the_browser() -> None:
    from wreath._docs.scripts import runtime
    from wreath._docs.search import stem

    table = {
        "params": "param",
        "parameters": "parameter",
        "queries": "query",
        "routes": "route",
        "classes": "class",
        "cors": "cors",
        "jobs": "jobs",
        "class": "class",
        "process": "process",
        "response": "response",
    }
    for word, expected in table.items():
        assert stem(word) == expected, word
    # The browser half is the same five rules in the same order.
    js = runtime()
    for rule in (
        "/ies$/",
        "/(ches|shes|sses|xes|zes)$/",
        "/es$/",
        "/s$/.test(word) && !/ss$/.test(word)",
        "word.length <= 4",
    ):
        assert rule in js, rule


def test_a_section_is_searchable_past_its_first_paragraph(tmp_path) -> None:
    import json

    src = tmp_path / "docs"
    src.mkdir()
    tail = "The bound value is read from the query parameters of the request."
    (src / "index.md").write_text(
        "# Home\n\n## Binding\n\n" + ("filler word. " * 40) + "\n\n" + tail + "\n"
    )
    site = Site("Demo", "docs", "site", Nav(Page("Home", "index.md")))
    build(site, root=tmp_path)
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    section = next(s for s in index["s"] if s["h"] == "Binding")
    assert "query parameters" not in section["x"]  # past the snippet...
    assert "parameter" in section["w"]  # ...but still findable
    # The word set carries what the snippet cannot answer for, and not a copy of it.
    assert "filler" not in section["w"]


def test_a_page_can_declare_the_words_readers_search_for(tmp_path) -> None:
    import json

    src = tmp_path / "docs"
    src.mkdir()
    (src / "index.md").write_text(
        "---\nkeywords: query parameters, querystring\nboost: 2\n---\n# Binding\n\ntext\n"
    )
    site = Site("Demo", "docs", "site", Nav(Page("Home", "index.md")))
    build(site, root=tmp_path)
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    assert index["p"][0]["k"] == "query parameters, querystring"
    assert index["p"][0]["b"] == 2.0


def test_a_result_says_where_in_the_nav_it_sits(tmp_path) -> None:
    import json

    index_json = tmp_path / "site" / "assets" / "search-index.json"
    build(_site(tmp_path), root=tmp_path)
    index = json.loads(index_json.read_text())
    routing = next(p for p in index["p"] if p["u"] == "guides/routing.html")
    assert routing["c"] == "Guides"
    home = next(p for p in index["p"] if p["u"] == "index.html")
    assert "c" not in home  # top-level: no trail to draw


def test_a_code_heading_is_indexed_as_the_word_it_shows(tmp_path) -> None:
    import json

    src = tmp_path / "docs"
    src.mkdir()
    (src / "index.md").write_text("# `wreath.binding`\n\n## `Query`\n\ntext\n")
    site = Site("Demo", "docs", "site", Nav(Page("Home", "index.md")))
    build(site, root=tmp_path)
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    assert index["p"][0]["t"] == "wreath.binding"
    assert any(s["h"] == "Query" for s in index["s"])
    page = (tmp_path / "site" / "index.html").read_text()
    assert "<title>wreath.binding · Demo</title>" in page
    assert "`" not in page.split('<div class="toc-rail">')[1].split("</div>")[0]
    assert "<code>Query</code>" in page  # the heading itself still renders as code


def test_the_palette_does_not_repeat_a_page_or_a_heading() -> None:
    from wreath._docs.scripts import runtime

    js = runtime()
    assert "perPage[id] <= 3" in js  # one page may not fill the palette
    assert "if (seen[label]) { continue; }" in js  # nor offer one heading twice


def test_a_generated_alias_ranks_below_a_term_the_page_claims() -> None:
    from wreath._docs.scripts import runtime

    js = runtime()
    assert "if (a) { total += a === 2 ? 40 : 20; }" in js  # generated alias
    assert "if (k) { total += k === 2 ? 80 : 40; }" in js  # declared keyword
    assert "heading.indexOf(term) === 0 ? 120 : 60" in js  # loosest heading
    assert "if (b) { total += b === 2 ? 8 : 4; }" in js  # ... and prose


def _capability_site(tmp_path, manifest: dict):
    """A site whose home page is the capability map, plus one guide to link to."""
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "agents").mkdir(parents=True)
    (src / "agents" / "manifest.json").write_text(json.dumps(manifest))
    (src / "index.md").write_text("# What you don't have to install\n\n::: capability-map\n")
    (src / "guides" / "widgets.md").write_text("# Holding a widget\n\ntext\n")
    return Site(
        name="Demo",
        source="docs",
        output="site",
        nav=Nav(Page("Home", "index.md"), Section("Guides", Page("Widgets", "guides/widgets.md"))),
        exclude=("agents/",),
    )


def _one_subsystem(**overrides) -> dict:
    subsystem = {
        "name": "widgets",
        "capability": "Widgets, and the holding of them",
        "replaces": ["widgetlib"],
        "guides": ["docs/guides/widgets.md"],
        "sources": [
            "src/wreath/widgets.py",
            "src/wreath/_private.py",
            "src/wreath/_native/widget.c",
        ],
    }
    return {"subsystems": [subsystem | overrides]}


def test_capability_map_renders_a_row_per_documented_subsystem(tmp_path) -> None:
    report = build(_capability_site(tmp_path, _one_subsystem()), root=tmp_path)

    assert report.ok
    page = (tmp_path / "site" / "index.html").read_text()
    assert "Widgets, and the holding of them" in page
    assert "<code>widgetlib</code>" in page
    # The "In Wreath" column is derived from `sources`, so it cannot name a
    # module that is not there -- and does not name the machine room either.
    assert "<code>wreath.widgets</code>" in page
    assert "_private" not in page and "widget.c" not in page
    assert 'href="guides/widgets.html"' in page and "Holding a widget" in page


def test_capability_map_omits_a_subsystem_marked_internal(tmp_path) -> None:
    site = _capability_site(tmp_path, _one_subsystem(capability=None))
    report = build(site, root=tmp_path)

    assert report.ok
    assert "widgetlib" not in (tmp_path / "site" / "index.html").read_text()
    # Nor as a search alias: a reader who typed it would land on a page with no
    # row for it, which is a worse answer than the search having missed.
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    assert "widgetlib" not in next(p for p in index["p"] if p["u"] == "index.html").get("a", "")


def test_capability_map_fails_strictly_on_a_subsystem_with_no_capability(
    tmp_path,
) -> None:
    manifest = _one_subsystem()
    del manifest["subsystems"][0]["capability"]
    report = build(_capability_site(tmp_path, manifest), root=tmp_path)

    assert not report.ok
    assert any("capability" in error and "widgets" in error for error in report.errors)


def test_capability_map_makes_the_page_findable_by_the_name_you_searched_for(
    tmp_path,
) -> None:
    site = _capability_site(tmp_path, _one_subsystem())
    (tmp_path / "docs" / "index.md").write_text(
        "---\nkeywords: do i still need\n---\n"
        "# What you don't have to install\n\n::: capability-map\n"
    )
    report = build(site, root=tmp_path)

    assert report.ok
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    page = next(p for p in index["p"] if p["u"] == "index.html")
    assert "widgetlib" in page["a"]
    # Its own field, not the author's: an alias is generated, and `scripts.py`
    # scores it below a term the page claims for itself.
    assert page["k"] == "do i still need"
    # An alias is a search term, not content: the page itself is unchanged.
    assert "widgetlib" not in (tmp_path / "site" / "index.html").read_text().split("<table")[0]


def test_capability_map_without_a_manifest_fails_strictly(tmp_path) -> None:
    site = _capability_site(tmp_path, _one_subsystem())
    (tmp_path / "docs" / "agents" / "manifest.json").unlink()
    report = build(site, root=tmp_path)

    assert not report.ok
    assert any("capability-map" in error for error in report.errors)
    # ... and the page still renders, so a local preview shows what is wrong.
    assert "Capability map unavailable" in (tmp_path / "site" / "index.html").read_text()


def _plate_site(tmp_path, manifest: dict, block: str = ""):
    """A site whose home page opens with a ```plate block."""
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "agents").mkdir(parents=True)
    (src / "agents" / "manifest.json").write_text(json.dumps(manifest))
    (src / "index.md").write_text(
        block
        or (
            "```plate\n"
            "caption: One package.\n"
            "title: Everything here is something you no longer install.\n"
            "action: The map -> guides/widgets.md\n"
            "```\n\nProse after the plate.\n"
        )
    )
    (src / "guides" / "widgets.md").write_text("# Holding a widget\n\ntext\n")
    return Site(
        name="Demo",
        source="docs",
        output="site",
        nav=Nav(Page("Home", "index.md"), Section("Guides", Page("Widgets", "guides/widgets.md"))),
        exclude=("agents/",),
    )


def _plate_manifest(*names: str) -> dict:
    return {
        "subsystems": [
            {
                "name": "widgets",
                "capability": "Widgets",
                "replaces": list(names),
                "guides": ["docs/guides/widgets.md"],
                "sources": ["src/wreath/widgets.py"],
            },
        ]
    }


def test_the_plate_prints_every_name_the_manifest_lists(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("celery", "redis", "sqlalchemy"))
    report = build(site, root=tmp_path)
    assert report.ok, report.errors
    html = (tmp_path / "site" / "index.html").read_text()
    names = re.findall(r"<li>([^<]+)</li>", html.split('class="plate-names"')[1])
    assert names == ["celery", "redis", "sqlalchemy"]  # and sorted
    assert "<strong>3</strong> packages" in html


def test_the_plate_sorts_rather_than_following_the_manifest(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("uvicorn", "alembic", "celery"))
    assert build(site, root=tmp_path).ok
    html = (tmp_path / "site" / "index.html").read_text()
    names = re.findall(r"<li>([^<]+)</li>", html.split('class="plate-names"')[1])
    assert names == ["alembic", "celery", "uvicorn"]


def test_the_plate_carries_no_strikethrough_element(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("celery", "redis"))
    assert build(site, root=tmp_path).ok
    html = (tmp_path / "site" / "index.html").read_text()
    block = html.split('class="plate-names"')[1].split("</ul>")[0]
    assert "<s>" not in block
    assert "does not install" in html  # the label carries the meaning


def test_the_plate_title_becomes_the_page_title(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("celery"))
    assert build(site, root=tmp_path).ok
    html = (tmp_path / "site" / "index.html").read_text()
    assert "<title>Everything here is something you no longer install. · Demo" in html


def test_a_plate_whose_manifest_is_missing_fails_a_strict_build(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("celery"))
    (tmp_path / "docs" / "agents" / "manifest.json").write_text("{not json")
    report = build(site, root=tmp_path)
    assert not report.ok
    assert any("plate" in error for error in report.errors)


def test_a_plate_action_is_link_checked_like_any_other_link(tmp_path) -> None:
    site = _plate_site(
        tmp_path,
        _plate_manifest("celery"),
        block=("```plate\ntitle: Gone\naction: Nowhere -> guides/deleted.md\n```\n"),
    )
    report = build(site, root=tmp_path)
    assert not report.ok
    assert any("deleted" in error for error in report.errors)


def test_the_plate_names_are_shown_but_not_indexed(tmp_path) -> None:
    site = _plate_site(tmp_path, _plate_manifest("celery", "redis"))
    assert build(site, root=tmp_path).ok
    html = (tmp_path / "site" / "index.html").read_text()
    index = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())

    assert "celery" in html  # shown to a reader
    home = [page for page in index["p"] if page["u"] == "index.html"]
    assert home, "the home page should be in the index"
    position = index["p"].index(home[0])
    carried = [
        section
        for section in index["s"]
        if section.get("p") == position and "celery" in json.dumps(section).lower()
    ]
    assert carried == [], "plate names must not reach the search index"


@pytest.mark.asyncio
async def test_the_preview_serves_through_wreaths_own_static_files(tmp_path) -> None:
    from wreath._docs.cli import preview_app
    from wreath.testing import TestClient

    assert build(_site(tmp_path), root=tmp_path).ok
    async with TestClient(preview_app(tmp_path / "site")) as client:
        home = await client.get("/")
        assert home.status == 200
        assert b"<title>" in home.body
        etag = home.header("etag")
        assert etag

        # The half the stdlib handler had no answer for: a reload of an
        # unchanged page transfers nothing.
        again = await client.get("/", headers={"if-none-match": etag})
        assert again.status == 304

        nested = await client.get("/guides/routing.html")
        assert nested.status == 200


@pytest.mark.asyncio
async def test_the_preview_resolves_a_directory_to_its_index(tmp_path) -> None:
    from wreath._docs.cli import preview_app
    from wreath.testing import TestClient

    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / "index.md").write_text("# Home\n\n[guides](guides/index.md)\n")
    (src / "guides" / "index.md").write_text("# Guides\n\ntext\n")
    site = Site(
        name="Demo",
        source="docs",
        output="site",
        nav=Nav(Page("Home", "index.md"), Section("Guides", Page("Index", "guides/index.md"))),
    )
    assert build(site, root=tmp_path).ok

    async with TestClient(preview_app(tmp_path / "site")) as client:
        assert (await client.get("/guides/")).status == 200
        assert (await client.get("/nowhere/")).status == 404


def test_the_sidebar_heads_itself_with_a_link_to_the_section_index(tmp_path) -> None:
    _threaded_nav_site(tmp_path)
    html = (tmp_path / "out" / "guides" / "a.html").read_text()
    head = re.search(r'<a class="side-head" href="([^"]+)">([^<]+)</a>', html)
    assert head, "the sidebar should be headed by its section"
    href, label = head.groups()
    assert label == "Guides"
    # ... and it points at where that section starts, not at the current page.
    assert href.endswith(".html") and not href.endswith("/a.html")


def test_the_section_head_is_absent_where_there_is_no_section(tmp_path) -> None:
    _threaded_nav_site(tmp_path)
    html = (tmp_path / "out" / "index.html").read_text()
    assert 'class="side-head"' not in html


def test_the_header_links_are_named_rather_than_guessed_at(tmp_path) -> None:
    from wreath._docs import Link, Repo

    site = dataclasses.replace(
        _site(tmp_path),
        repo=Repo("https://github.com/you/proj"),
        links=(Link("Demo on PyPI", "https://pypi.org/project/demo/", icon="package"),),
    )
    assert build(site, root=tmp_path).ok
    html = (tmp_path / "site" / "index.html").read_text()

    menu = html.split('<div class="more-menu">')[1].split("</details>")[0]
    assert ">you/proj<" in menu  # the repo, named
    assert ">Demo on PyPI<" in menu  # the link, named
    # The bar keeps search and the theme control, and nothing else loose.
    bar = html.split('<div class="bar">')[1].split("</header>")[0]
    assert 'id="theme-toggle"' in bar
    assert 'class="bar-links"' not in bar  # the old loose glyph row


def _access_flags(status: int, path: str) -> int:
    """Encode the access record exactly as the ring would, and return its flags."""
    from wreath._docs.cli import _short
    from wreath._flight_schema import (
        LOG_FLAG_TRUNCATED,
        LogArg,
        LogArgType,
        LogCell,
        Severity,
    )

    cell = LogCell(
        request_id=1,
        site_id=1,
        severity=Severity.INFO,
        args=(
            LogArg(LogArgType.INT, number=status),
            LogArg(LogArgType.STR, payload=_short(path).encode("utf-8")),
        ),
    )
    return LogCell.decode(cell.encode()).flags & LOG_FLAG_TRUNCATED


def test_every_preview_access_record_fits_one_log_cell(tmp_path) -> None:
    site = _site(tmp_path)
    assert build(site, root=tmp_path).ok
    paths = [
        "/" + str(p.relative_to(tmp_path / "site")).replace("\\", "/")
        for p in (tmp_path / "site").rglob("*")
        if p.is_file()
    ]
    paths += ["/", "/cookbook/recipes/serve-a-grpc-method.html", "/" + "x" * 400]
    for path in paths:
        assert _access_flags(200, path) == 0, f"{path!r} truncated its record"
        assert _access_flags(404, path) == 0, f"{path!r} truncated its record"


def test_a_clipped_path_keeps_the_end_that_identifies_it(tmp_path) -> None:
    from wreath._docs.cli import _short

    assert _short("/guides/routing.html") == "/guides/routing.html"  # fits whole
    clipped = _short("/cookbook/recipes/serve-a-grpc-method.html")
    assert clipped.startswith("…")
    assert clipped.endswith("method.html")
    assert len(clipped.encode("utf-8")) <= 21


def test_the_preview_logs_one_line_per_request(tmp_path) -> None:
    import inspect

    from wreath._docs import cli as _docs_cli

    source = inspect.getsource(_docs_cli._serve)
    assert "TelemetryConfig" in source and "Mode.PULSE" in source
    app = _docs_cli.preview_app(tmp_path)
    assert any(
        getattr(item[2], "after_inplace", None) is _docs_cli._log_access
        for item in app._global_middleware
    ), "the access hook should be registered globally"
