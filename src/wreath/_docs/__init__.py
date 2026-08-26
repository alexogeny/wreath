"""Wreath's native static-site generator (`wreath docs`).

A typed-Python-configured docs site — markdown to a self-contained HTML tree,
no third-party dependency and no YAML. Configuration is a `Site` in a
`wreath_docs.py` module; the build is a plain directory served by wreath's own
`StaticFiles`.

The native `_docs` extension compiles the versioned WDT1 block scan, fenced
Python-block index, visible-prose stream, and search-word tape. The Python
renderer applies Wreath's CommonMark subset semantics to that tape; full
CommonMark remains a follow-on at the same `wreath._docs.markdown.render` seam.
"""

from __future__ import annotations

from .config import ICONS, THEMES, Link, Nav, Page, Palette, Repo, Section, Site
from .site import BuildReport, build

__all__ = ["ICONS", "THEMES", "BuildReport", "Link", "Nav", "Page", "Palette", "Repo",
           "Section", "Site", "build"]
