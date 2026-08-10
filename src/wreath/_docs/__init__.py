"""Wreath's native static-site generator (`wreath docs`).

A typed-Python-configured docs site — markdown to a self-contained HTML tree,
no third-party dependency and no YAML. Configuration is a `Site` in a
`wreath_docs.py` module; the build is a plain directory served by wreath's own
`StaticFiles`.

The per-file markdown parse is a CommonMark *subset* today; the seam
for the native `_docs` extension (full CommonMark + a syntax highlighter, via a
versioned WDT1 render tape) is `wreath._docs.markdown.render`.
"""

from __future__ import annotations

from .config import ICONS, THEMES, Link, Nav, Page, Palette, Repo, Section, Site
from .site import BuildReport, build

__all__ = ["ICONS", "THEMES", "BuildReport", "Link", "Nav", "Page", "Palette", "Repo",
           "Section", "Site", "build"]
