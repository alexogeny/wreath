"""Typed configuration for the static-site generator — Python, not YAML.

Wreath has no YAML dependency and no desire for one; a docs site is described by
a ``wreath_docs.py`` module that exposes a :class:`Site` (mirroring how the
migrations and server layers take typed config, not stringly files). Everything
here is a frozen dataclass, so a config is validated once and cheap to pass
around.

    # wreath_docs.py
    from wreath._docs.config import Site, Nav, Section, Page, Palette

    site = Site(
        name="Wreath",
        source="docs",
        output="site",
        nav=Nav(
            Page("Home", "index.md"),
            Section("Guides", Page("Routing", "guides/routing.md")),
        ),
        palette=Palette(primary="#6d28d9", accent="#06b6d4"),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Page:
    """One documentation page: a nav title and a source markdown file."""

    title: str
    source: str          # path relative to Site.source, e.g. "guides/routing.md"

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("a nav Page needs a title")
        if not self.source.endswith(".md"):
            raise ValueError(f"page source must be a .md file: {self.source!r}")


@dataclass(frozen=True, slots=True)
class Section:
    """A named group of pages (and sub-sections) in the navigation tree."""

    title: str
    items: tuple[Page | Section, ...]

    def __init__(self, title: str, *items: Page | Section) -> None:
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "items", tuple(items))
        if not title:
            raise ValueError("a nav Section needs a title")


@dataclass(frozen=True, slots=True)
class Nav:
    """The ordered navigation tree — the single source of page ordering."""

    items: tuple[Page | Section, ...]

    def __init__(self, *items: Page | Section) -> None:
        object.__setattr__(self, "items", tuple(items))

    def pages(self) -> tuple[Page, ...]:
        """Every :class:`Page` in the tree, depth-first in nav order."""
        out: list[Page] = []
        _collect_pages(self.items, out)
        return tuple(out)


def _collect_pages(items: tuple[Page | Section, ...], out: list[Page]) -> None:
    for item in items:
        if isinstance(item, Page):
            out.append(item)
        else:
            _collect_pages(item.items, out)


@dataclass(frozen=True, slots=True)
class Palette:
    """A full, coherent colour theme (light + dark surfaces), fonts, and a radius.

    Defaults are wreath's pressed-viridian look — an evergreen structural colour
    on a faintly green-cast paper, with brass reserved for state. Pick a
    ready-made one from :data:`THEMES` (``palette=THEMES["sepia"]``) or tweak any
    field.
    """

    primary: str = "#1d5b4a"
    accent: str = "#8a5a12"
    #: Light-mode surfaces.
    bg: str = "#f7f8f5"
    fg: str = "#151a14"
    muted: str = "#5a6560"
    border: str = "#dde0d6"
    surface: str = "#edf0e8"       # code blocks / sidebar
    #: Dark-mode surfaces.
    dark_bg: str = "#0e1310"
    dark_fg: str = "#e4eae2"
    dark_muted: str = "#93a099"
    dark_border: str = "#26302a"
    dark_surface: str = "#161d18"
    #: Body-link colours. Empty means "derive": light links fall back to
    #: ``primary``, dark links to an auto-lightened ``primary`` so they stay
    #: legible on a dark background instead of inheriting a too-dark brand colour.
    link: str = ""
    dark_link: str = ""
    #: Dark-mode brand fills. A brand colour tuned to read on paper is usually
    #: too dark to *fill* anything on a near-black surface — a viridian chart bar
    #: or a brass rule disappears into the ground. Empty means "derive" by
    #: lightening, the same trick ``dark_link`` uses for text.
    dark_primary: str = ""
    dark_accent: str = ""
    #: "system" (sans), "serif", or "mono" reading stack for body copy.
    font: str = "system"
    #: The heading voice: "serif", "sans", or "mono". Body carries the reading;
    #: display carries the personality; mono carries the structural chrome
    #: (eyebrows, nav labels, table headers) in every theme regardless.
    display: str = "serif"
    #: Corner radius for cards and code blocks.
    radius: str = "8px"


#: Coherent, ready-made themes. Pass one as ``Site(palette=THEMES["nord"])``.
THEMES: dict[str, Palette] = {
    # Pressed viridian: an evergreen structural colour on faintly green-cast
    # paper, brass held back for state. The dark mode grounds on a pine ink
    # rather than the usual blue-black, which keeps the hue family intact
    # instead of switching themes when the lights go out.
    "wreath": Palette(
        link="#1d5b4a", dark_link="#6fd3b0",
        dark_primary="#4bb595", dark_accent="#e0a94c"),
    "slate": Palette(
        primary="#4f46e5", accent="#0ea5e9", fg="#0f172a", muted="#64748b",
        bg="#ffffff", border="#e2e8f0", surface="#f1f5f9",
        dark_bg="#0f172a", dark_fg="#e2e8f0", dark_muted="#94a3b8",
        dark_border="#1e293b", dark_surface="#1e293b",
        link="#4f46e5", dark_link="#a5b4fc", dark_primary="#8b83f5",
        display="sans"),
    "sepia": Palette(
        primary="#a3451b", accent="#b7791f", bg="#faf6ee", fg="#43382a",
        muted="#7c6f5f", border="#e4d8c4", surface="#f2e9d8",
        dark_bg="#211c16", dark_fg="#e8ddc9", dark_muted="#a8987f",
        dark_border="#3a3226", dark_surface="#2a241c", font="serif",
        link="#b5561f", dark_link="#e0a878",
        dark_primary="#d9793f", dark_accent="#dfa845"),
    "nord": Palette(
        primary="#5e81ac", accent="#88c0d0", bg="#eceff4", fg="#2e3440",
        muted="#5b6472", border="#d8dee9", surface="#e5e9f0",
        dark_bg="#2e3440", dark_fg="#eceff4", dark_muted="#a6accd",
        dark_border="#3b4252", dark_surface="#3b4252",
        # Nord's `primary` is a mid-tone blue that reads at 3.5:1 on the light
        # surface -- below AA for body text. The link is darkened two steps down
        # the same ramp (5.3:1) and keeps the hue; `primary` stays the brand fill.
        link="#456485", dark_link="#8fbcbb", dark_primary="#81a1c1",
        display="sans"),
    "terminal": Palette(
        primary="#16a34a", accent="#0891b2", bg="#ffffff", fg="#111827",
        muted="#6b7280", border="#e5e7eb", surface="#f3f4f6",
        dark_bg="#0a0a0a", dark_fg="#e5e5e5", dark_muted="#a3a3a3",
        dark_border="#262626", dark_surface="#171717", radius="4px",
        # Same problem as nord, worse: a saturated green on white is 3.5:1.
        # Deepened to 6.2:1, which also stops it vibrating against the surface.
        link="#08703c", dark_link="#4ade80", dark_primary="#34d399",
        dark_accent="#22d3ee", display="mono"),
}


@dataclass(frozen=True, slots=True)
class Site:
    """A whole documentation site: sources, output, nav, and theme.

    Args:
        name: site/brand name, shown in the header and ``<title>`` suffix.
        source: directory holding the markdown sources.
        output: directory the built HTML is written to.
        nav: the navigation tree (also the page-ordering source of truth).
        palette: theme colours.
        strict: fail the build on an orphan page (a ``.md`` not in ``nav``), a
            dead internal link, or a broken ``#anchor``. On by default — a docs
            build should not rot.
        base_url: canonical site URL (e.g. ``https://docs.trailhead.example``). When set,
            a ``sitemap.xml`` and absolute URLs in ``llms.txt`` are generated.
        description: one-line site description (used in ``llms.txt`` and page
            ``<meta>`` when a page has none of its own).
        exclude: glob patterns (matched against each source-relative ``.md`` path)
            for files that live under ``source`` but are deliberately unpublished —
            working notes, ADRs, agent manifests. Matching files never raise an
            orphan warning. Mirrors mkdocs' ``exclude_docs``.
        source_url: base URL each page's "Edit this page" link is built from, e.g.
            ``https://github.com/you/proj/edit/main/docs``. Empty means no edit
            links. The page's source-relative path is appended to it.
        tabs: promote the top level of ``nav`` into a row of section tabs in the
            header, leaving the sidebar to show only the section you are in. The
            default, ``"auto"``, does this once the nav has at least three
            top-level entries — below that a tab row is chrome around nothing.
            ``"never"`` keeps the whole tree in one sidebar.
    """

    name: str
    source: str
    output: str
    nav: Nav
    palette: Palette = field(default_factory=Palette)
    strict: bool = True
    base_url: str = ""
    description: str = ""
    #: Surface treatment: "flat", "elevated", "papery", "hardcore", or "orby".
    feel: str = "flat"
    #: Source-relative globs to exempt from the orphan check (never published).
    exclude: tuple[str, ...] = ()
    #: Base URL for per-page "Edit this page" links; empty disables them.
    source_url: str = ""
    #: "auto" (tabs once the nav has 3+ top-level entries) or "never".
    tabs: str = "auto"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Site.name must not be empty")
        if not self.nav.pages():
            raise ValueError("Site.nav must contain at least one page")
        if self.feel not in _FEELS:
            raise ValueError(f"unknown feel {self.feel!r}; choose from {sorted(_FEELS)}")
        if self.tabs not in ("auto", "never"):
            raise ValueError(f"Site.tabs must be 'auto' or 'never', not {self.tabs!r}")
        if self.palette.font not in _FACES:
            raise ValueError(f"unknown Palette.font {self.palette.font!r}; "
                             f"choose from {sorted(_FACES)}")
        if self.palette.display not in _FACES:
            raise ValueError(f"unknown Palette.display {self.palette.display!r}; "
                             f"choose from {sorted(_FACES)}")

    def use_tabs(self) -> bool:
        """Should the top level of the nav render as header tabs?"""
        return self.tabs == "auto" and len(self.nav.items) >= 3


#: Feel names, mirrored from theme.FEELS so config can validate without importing it.
_FEELS = frozenset({"flat", "elevated", "papery", "hardcore", "orby"})

#: The three type roles a palette can point `font` and `display` at.
_FACES = frozenset({"system", "sans", "serif", "mono"})


__all__ = ["THEMES", "Nav", "Page", "Palette", "Section", "Site"]
