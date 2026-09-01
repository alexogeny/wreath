"""Public static-site generation API.

`Site` compiles Markdown into a checked static tree. `Theme` and
`AssetManifest` extend the generated pages without moving content stamping,
link assurance, or output ownership into a browser build tool.
"""

from __future__ import annotations

from ._docs import (
    ICONS,
    THEMES,
    AssetManifest,
    BuildReport,
    Link,
    Nav,
    Page,
    PageContext,
    PageTemplate,
    Palette,
    Repo,
    Section,
    Site,
    StaticAsset,
    Theme,
    build,
)

__all__ = [
    "ICONS",
    "THEMES",
    "AssetManifest",
    "BuildReport",
    "Link",
    "Nav",
    "Page",
    "PageContext",
    "PageTemplate",
    "Palette",
    "Repo",
    "Section",
    "Site",
    "StaticAsset",
    "Theme",
    "build",
]
