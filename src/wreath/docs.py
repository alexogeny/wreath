"""Public static-site generation API.

`Site` compiles Markdown into a checked static tree. `Theme` and
`AssetManifest` extend the generated pages without moving content stamping,
link assurance, or output ownership into a browser build tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._docs.config import (
        ICONS,
        THEMES,
        AssetManifest,
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
    )
    from ._docs.site import BuildReport, build

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

_EXPORTS = {
    "ICONS": "config",
    "THEMES": "config",
    "AssetManifest": "config",
    "BuildReport": "site",
    "Link": "config",
    "Nav": "config",
    "Page": "config",
    "PageContext": "config",
    "PageTemplate": "config",
    "Palette": "config",
    "Repo": "config",
    "Section": "config",
    "Site": "config",
    "StaticAsset": "config",
    "Theme": "config",
    "build": "site",
}

_MODULE_EXPORTS = {
    "config": (
        "ICONS",
        "THEMES",
        "AssetManifest",
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
    ),
    "site": ("BuildReport", "build"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f"._docs.{module}", __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
