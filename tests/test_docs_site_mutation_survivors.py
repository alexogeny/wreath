from __future__ import annotations

import json
from pathlib import Path

from wreath._docs.config import Nav, Page, Section, Site
from wreath._docs.site import _compile_navigation, _nav_context, _write_robots, build


def _site(*, strict: bool = True, base_url: str = "") -> Site:
    return Site(
        "Docs",
        "docs",
        "site",
        Nav(
            Page("Home", "index.md"),
            Section(
                "Guides",
                Page("First", "guides/first.md"),
                Page("Second", "guides/second.md"),
            ),
            Section("Empty"),
        ),
        strict=strict,
        base_url=base_url,
        description="Default description",
        map_page="guides/first.md",
    )


def _write_pages(root: Path) -> None:
    docs = root / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text(
        "---\n"
        "description: Landing description\n"
        "keywords: welcome, start\n"
        "boost: 1.5\n"
        "---\n"
        "# Home\n\nWelcome.\n",
        encoding="utf-8",
    )
    (docs / "guides" / "first.md").write_text("# First\n\n## Start\n\nOne.\n")
    (docs / "guides" / "second.md").write_text("Second page without a heading.\n")


def test_navigation_image_skips_empty_sections_and_owns_nested_pages() -> None:
    image = _compile_navigation(_site())

    assert [(item.title, landing.title) for item, landing in image.entries] == [
        ("Home", "Home"),
        ("Guides", "First"),
    ]
    assert image.owner_by_output == {
        "index.html": 0,
        "guides/first.html": 1,
        "guides/second.html": 1,
    }


def test_navigation_context_marks_only_the_active_section() -> None:
    site = _site()
    image = _compile_navigation(site)

    menu, side, title, landing = _nav_context(site, "guides/second.html", image)

    assert menu.count('class="active"') == 1
    assert menu.count('aria-current="true"') == 1
    assert '<a href="../index.html">Home</a>' in menu
    assert "Guides" in menu
    assert "Second" in side
    assert title == "Guides"
    assert landing == "first.html"


def test_navigation_context_has_no_active_marker_for_an_external_page() -> None:
    site = _site()

    menu, side, title, landing = _nav_context(site, "404.html")

    assert 'class="active"' not in menu
    assert "aria-current" not in menu
    assert side == ""
    assert title == ""
    assert landing == ""


def test_build_preserves_declared_and_default_search_metadata(tmp_path: Path) -> None:
    _write_pages(tmp_path)

    report = build(_site(base_url="https://docs.example.test/base"), root=tmp_path)
    search = json.loads((tmp_path / "site" / "assets" / "search-index.json").read_text())
    by_url = {row["u"]: row for row in search["p"]}

    assert report.ok
    assert report.pages == 3
    assert by_url["index.html"] == {
        "u": "index.html",
        "t": "Home",
        "k": "welcome, start",
        "b": 1.5,
    }
    assert by_url["guides/first.html"] == {
        "u": "guides/first.html",
        "t": "First",
        "c": "Guides",
    }
    assert by_url["guides/second.html"] == {
        "u": "guides/second.html",
        "t": "Second",
        "c": "Guides",
    }
    second = (tmp_path / "site" / "guides" / "second.html").read_text()
    assert 'content="Default description"' in second
    assert 'canonical" href="https://docs.example.test/base/guides/second.html"' in second


def test_build_links_first_middle_and_last_pages_in_nav_order(tmp_path: Path) -> None:
    _write_pages(tmp_path)

    assert build(_site(), root=tmp_path).ok
    home = (tmp_path / "site" / "index.html").read_text()
    first = (tmp_path / "site" / "guides" / "first.html").read_text()
    second = (tmp_path / "site" / "guides" / "second.html").read_text()

    assert "nav-prev" not in home
    assert 'class="nav-next" href="guides/first.html"' in home
    assert 'class="nav-prev" href="../index.html"' in first
    assert 'class="nav-next" href="second.html"' in first
    assert 'class="nav-prev" href="first.html"' in second
    assert "nav-next" not in second


def test_build_separates_strict_errors_from_preview_warnings(
    tmp_path: Path,
) -> None:
    _write_pages(tmp_path)
    (tmp_path / "docs" / "index.md").write_text("# Home\n\n[gone](missing.md)\n")

    strict = build(_site(strict=True), root=tmp_path)
    preview = build(_site(strict=False), root=tmp_path)

    assert strict.errors and not strict.warnings
    assert not preview.errors and preview.warnings
    assert "dead link" in strict.errors[0]
    assert "dead link" in preview.warnings[0]


def test_build_reports_missing_nav_pages_by_name(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("# Home\n")

    report = build(_site(), root=tmp_path)

    assert report.pages == 1
    assert report.errors == (
        "nav page missing on disk: guides/first.md",
        "nav page missing on disk: guides/second.md",
    )


def test_robots_file_includes_sitemap_only_for_absolute_site(tmp_path: Path) -> None:
    without_url = tmp_path / "plain"
    with_url = tmp_path / "absolute"
    without_url.mkdir()
    with_url.mkdir()

    _write_robots(without_url, _site())
    _write_robots(with_url, _site(base_url="https://docs.example.test/base/"))

    assert without_url.joinpath("robots.txt").read_text() == "User-agent: *\nAllow: /\n"
    assert with_url.joinpath("robots.txt").read_text() == (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://docs.example.test/base/sitemap.xml\n"
    )
