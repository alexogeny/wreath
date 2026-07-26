"""Wreath's native static-site generator (``wreath docs``).

A typed-Python-configured docs site — markdown to a self-contained HTML tree,
no third-party dependency and no YAML. Configuration is a :class:`Site` in a
``wreath_docs.py`` module; the build is a plain directory served by wreath's own
:class:`~wreath.staticfiles.StaticFiles`.

The per-file markdown parse is a pure-Python CommonMark *subset* today; the seam
for the native ``_docs`` extension (full CommonMark + a syntax highlighter, via a
versioned WDT1 render tape) is :func:`wreath._docs.markdown.render`.
"""

from __future__ import annotations

from .config import THEMES, Nav, Page, Palette, Section, Site
from .site import BuildReport, build

__all__ = ["THEMES", "BuildReport", "Nav", "Page", "Palette", "Section", "Site", "build"]
