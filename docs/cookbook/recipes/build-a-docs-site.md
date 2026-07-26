# Build a docs site with `wreath docs`

Wreath ships its own static-site generator — markdown to a self-contained HTML
tree, configured in typed Python, with no third-party dependency and no YAML.
It's wreath's answer to reaching for mkdocs: the same `strict` orphan/dead-link
gate, a light/dark theme, and output you serve like any static tree.

Describe the site in a `wreath_docs.py` at your project root:

```python
from wreath._docs import Site, Nav, Page, Section, Palette

site = Site(
    name="Trailhead",
    source="docs",
    output="site",
    nav=Nav(
        Page("Home", "index.md"),
        Section("Guides",
            Page("Getting started", "guides/start.md"),
            Page("Configuration", "guides/config.md"),
        ),
    ),
    palette=Palette(primary="#6d28d9", accent="#06b6d4"),
)
```

`Page(title, source)` names a page and its markdown file (relative to
`source`); `Section(title, *items)` groups them; `Nav(*items)` is the single
source of page ordering. Then run one command:

```bash
wreath docs build          # render docs/ -> site/
wreath docs serve          # build, then preview at http://127.0.0.1:8000
```

Each `.md` becomes a self-contained `.html` — relative links, a sidebar, and a
per-page table of contents — plus one small stylesheet. No CDN, no web fonts, no
JavaScript framework; just a theme that follows the OS light/dark preference.

Drop `check` into your pipeline to fail on rot:

```bash
wreath docs check          # exit 1 on a dead link; reports orphan pages
```

`check` builds strictly: a `[text](other.md)` whose target isn't a real page is
an error (exit 1), and a `.md` under `source/` that no nav entry references is a
warning. Same drop-into-a-pipeline ergonomics as `wreath migrations check`.

All three commands default the config to `wreath_docs.py`; pass a path as the
first argument to point elsewhere. The built `output` is a plain directory, so
serve it from your app with `app.static("/docs", "site")`.
