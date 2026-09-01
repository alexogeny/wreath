from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _source(root: Path, source: str = "index.md") -> None:
    path = root / "docs" / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Home\n\n## Detail\n\nHello.\n")


def test_docs_site_configuration_is_public() -> None:
    from wreath.docs import (
        AssetManifest,
        BuildReport,
        Nav,
        Page,
        PageContext,
        Site,
        StaticAsset,
        Theme,
        build,
    )

    assert all(
        value is not None
        for value in (
            AssetManifest,
            BuildReport,
            Nav,
            Page,
            PageContext,
            Site,
            StaticAsset,
            Theme,
            build,
        )
    )


def test_general_page_layout_adds_theme_assets_without_docs_chrome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wreath._docs import site as site_module
    from wreath.docs import AssetManifest, Nav, Page, Site, StaticAsset, Theme, build

    _source(tmp_path, "notes/entry.md")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundle.joinpath("site.a1.css").write_text(".cinematic{display:block}")
    bundle.joinpath("site.b2.js").write_text("globalThis.cinematic=true")
    bundle.joinpath("mark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    bundle.joinpath("icons/nested").mkdir(parents=True)
    bundle.joinpath("icons/nested/mark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assets = AssetManifest(
        StaticAsset("site-css", "bundle/site.a1.css", "assets/site.a1.css"),
        StaticAsset("site-js", "bundle/site.b2.js", "assets/site.b2.js"),
        StaticAsset("mark", "bundle/mark.svg", "assets/mark.svg"),
        StaticAsset("icons", "bundle/icons", "assets/icons"),
    )
    monkeypatch.setattr(
        site_module,
        "_compile_navigation",
        lambda _site: pytest.fail("general pages must not compile documentation navigation"),
    )
    site = Site(
        "Notes",
        "docs",
        "site",
        Nav(Page("Entry", "notes/entry.md")),
        map_page="notes/entry.md",
        layout="page",
        theme=Theme(
            assets=assets,
            stylesheets=("site-css",),
            scripts=("site-js",),
            head_html='<meta name="cinematic" content="ready">',
        ),
    )

    report = build(site, root=tmp_path)

    assert report.ok
    html = tmp_path.joinpath("site/notes/entry.html").read_text()
    assert '<meta name="cinematic" content="ready">' in html
    assert 'href="../assets/site.a1.css"' in html
    assert 'src="../assets/site.b2.js"' in html
    assert "site-nav" not in html
    assert "nav-scrim" not in html
    assert "Search documentation" not in html
    assert 'class="browse"' not in html
    assert "page-nav" not in html
    assert tmp_path.joinpath("site/assets/mark.svg").is_file()
    assert tmp_path.joinpath("site/assets/icons/nested/mark.svg").is_file()


def test_custom_page_template_receives_relative_manifest_paths(tmp_path: Path) -> None:
    from wreath.docs import AssetManifest, Nav, Page, PageContext, Site, StaticAsset, Theme, build

    _source(tmp_path, "notes/entry.md")
    tmp_path.joinpath("logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    seen: list[PageContext] = []

    def render(context: PageContext) -> str:
        seen.append(context)
        return f"<title>{context.page_title}</title>{context.content}:{context.asset('logo')}"

    site = Site(
        "Notes",
        "docs",
        "site",
        Nav(Page("Entry", "notes/entry.md")),
        theme=Theme(
            template=render,
            assets=AssetManifest(StaticAsset("logo", "logo.svg", "assets/logo.123.svg")),
        ),
    )

    assert build(site, root=tmp_path).ok
    assert seen[0].output == "notes/entry.html"
    assert seen[0].asset("logo") == "../assets/logo.123.svg"
    assert "../assets/logo.123.svg" in tmp_path.joinpath("site/notes/entry.html").read_text()


def test_general_template_context_omits_documentation_facts(tmp_path: Path) -> None:
    from wreath.docs import Nav, Page, PageContext, Site, Theme, build

    _source(tmp_path)
    seen: list[PageContext] = []

    def render(context: PageContext) -> str:
        seen.append(context)
        return context.content

    site = Site(
        "Pages",
        "docs",
        "site",
        Nav(Page("Home", "index.md")),
        layout="page",
        theme=Theme(template=render),
    )

    assert build(site, root=tmp_path).ok
    assert seen[0].toc_html == ""
    assert seen[0].nav_html == ""
    assert seen[0].footer == ""
    assert seen[0].map_href == ""


def test_documentation_layout_without_a_map_has_no_browse_link(tmp_path: Path) -> None:
    from wreath.docs import Nav, Page, Site, build

    _source(tmp_path)
    assert build(Site("Docs", "docs", "site", Nav(Page("Home", "index.md"))), root=tmp_path).ok

    assert 'class="browse"' not in tmp_path.joinpath("site/index.html").read_text()


def test_theme_refuses_an_asset_name_the_manifest_does_not_define() -> None:
    from wreath.docs import Theme

    with pytest.raises(ValueError, match="stylesheet asset 'missing'.*StaticAsset"):
        Theme(stylesheets=("missing",))


def test_asset_manifest_accepts_a_renderer_mapping() -> None:
    from wreath.docs import AssetManifest

    manifest = AssetManifest.from_mapping(
        {"app": "app.a1.js"}, source_root="frontend/dist", output_root="assets"
    )

    assert manifest.assets[0].source == "frontend/dist/app.a1.js"
    assert manifest.path("app") == "assets/app.a1.js"


def test_build_reports_missing_and_colliding_assets(tmp_path: Path) -> None:
    from wreath.docs import AssetManifest, Nav, Page, Site, StaticAsset, Theme, build

    _source(tmp_path)
    tmp_path.joinpath("replacement.css").write_text("body{}")
    site = Site(
        "Docs",
        "docs",
        "site",
        Nav(Page("Home", "index.md")),
        theme=Theme(
            assets=AssetManifest(
                StaticAsset("missing", "missing.svg", "assets/missing.svg"),
                StaticAsset("collision", "replacement.css", "assets/docs.css"),
            )
        ),
    )

    report = build(site, root=tmp_path)

    assert report.errors == (
        "theme asset 'missing' source is missing: 'missing.svg'",
        "theme asset 'collision' output collides with generated path: 'assets/docs.css'",
    )
    assert "--primary:" in tmp_path.joinpath("site/assets/docs.css").read_text()


def test_theme_refuses_a_non_callable_template() -> None:
    from wreath.docs import Theme

    template: Any = "page.html"
    with pytest.raises(TypeError, match="Theme.template must be callable"):
        Theme(template=template)


def test_custom_template_must_return_html_text(tmp_path: Path) -> None:
    from wreath.docs import Nav, Page, Site, Theme, build

    _source(tmp_path)

    def render(_context: Any) -> Any:
        return b"not text"

    site = Site(
        "Pages",
        "docs",
        "site",
        Nav(Page("Home", "index.md")),
        theme=Theme(template=render),
    )

    with pytest.raises(TypeError, match="Theme.template.*index.html.*return str.*bytes"):
        build(site, root=tmp_path)
