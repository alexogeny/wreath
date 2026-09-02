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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import (
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
    from .site import BuildReport, build

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

    loaded = import_module(f".{module}", __name__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
