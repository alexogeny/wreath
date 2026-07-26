# Building a docs site (`wreath docs`)

Wreath ships its own static-site generator — markdown to a self-contained HTML
tree, configured in typed Python, with no third-party dependency and no YAML. It
is wreath's answer to reaching for mkdocs: the same `strict` orphan/dead-link
gate, a light/dark theme, and output you serve with wreath's own hardened
[`StaticFiles`](static-files.md).

## User story: a docs site without a docs toolchain

> *As a library author, I want a documentation site from my markdown files, but I
> don't want to add mkdocs + a theme + plugins (and their version churn) to my
> project. I want to describe the site in Python I already understand and run one
> command.*

Describe the site in a `wreath_docs.py`:

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

Then:

```bash
wreath docs build          # render docs/ -> site/
wreath docs serve          # build, then preview at http://127.0.0.1:8000
```

The nav is the single source of page ordering; each `.md` becomes a
self-contained `.html` (relative links, a sidebar, and a per-page table of
contents), plus one small stylesheet. No CDN, no web fonts, no JavaScript
framework — just a light/dark theme that follows the OS preference.

## User story: fail CI when a link rots

> *As a maintainer, I keep renaming and moving pages, and internal links go stale.
> I want the build to fail the moment a link points at a page that no longer
> exists, or a markdown file drops out of the nav.*

```bash
wreath docs check          # exit 1 on a dead link; reports orphan pages
```

`check` builds strictly and reports:

- **dead links** — a `[text](other.md)` whose target isn't a real page → error, exit 1
- **orphan pages** — a `.md` under `source/` that no nav entry references → warning

It's the same drop-into-a-pipeline ergonomics as `wreath migrations check`.

## Safe by construction

Rendering escapes every text and attribute span, and link targets are
scheme-checked — a `[click](javascript:…)` in someone's markdown is neutralised,
and raw `<script>` in content is escaped, not emitted. Your docs can't become an
XSS vector because a contributor pasted the wrong thing.

## Themes and feel — two axes

Pick a **colour theme** and, independently, a surface **feel**. Any of the five
themes composes with any of the five feels, so there are twenty-five looks out of
the box:

```python
from wreath._docs import THEMES

site = Site(..., palette=THEMES["sepia"], feel="papery")
```

- **Themes** (colours, light + dark): `wreath` (deep purple/cyan), `slate`,
  `sepia` (warm paper, serif), `nord`, `terminal`.
- **Feels** (surface treatment): `flat` (default), `elevated` (soft shadows),
  `papery` (faint grain + soft shadows), `hardcore` (square, heavy borders,
  uppercase headings), `orby` (big radii, pill controls, glow).

Both are just CSS variables, so a bespoke palette is a `Palette(primary=…, …)`
away — no theme build step.

## Charts from your data

Point a ```` ```chart ```` block at a JSON file — a benchmark's `latest.json`,
anything you already emit — and it renders as an inline SVG bar chart **at build
time**. No runtime JavaScript, no chart library, no CDN; the SVG recolours with
the active theme.

````markdown
```chart
source: ../benchmark-results/latest.json
data: results               # dotted path to the list inside the JSON
x: framework                # label field
y: requests_per_second      # value field
where: scenario=plaintext   # optional filter
title: Requests/sec (plaintext)
sort: desc
limit: 12
```
````

The data stays outside the docs, so the chart is always current with the source
file. A missing file or field degrades to a visible note rather than failing the
build — unless you're in `check`, where you want to know. Bars whose label names a
wreath arm (`Wreath (metal)`, `(native)`, `(pure)`, …) each get a distinct
theme-aware colour, and everything else renders as a muted hatch — so your own
series never blends into the field. Chart data files that live under your `source`
tree are copied into the built site, so a "raw data" link to the JSON resolves.

## Serving the built site from your app

The output is a plain directory, so mount it like any other static tree:

```python
app.static("/docs", "site")
```

## API reference from your code

A reference page doesn't have to be hand-written. Point the `:::` directive at a
module, class, or function and it expands — before rendering — into a heading, the
signature, and the docstring (google-style `Args:`/`Returns:`/`Raises:` become
lists):

```markdown
# Responses

::: wreath.response.JSONResponse
```

Because it emits *markdown*, the generated reference travels the same path as
prose — same anchors, same table of contents. This is wreath's built-in stand-in
for mkdocstrings.

Cross-references keep the mkdocstrings convention: a directive anchors its object
at the full dotted path, so a link written `[`Site`](#wreath._docs.config.Site)`
resolves to `::: wreath._docs.config.Site` — no rewrite needed when you move a
project off mkdocstrings. (Any heading can pin an explicit anchor the same way,
with a trailing `{#custom-id}`.)

## Keeping working notes out of the build

Not every markdown file under `source` is meant to publish — design notes, ADRs,
an agent manifest. List glob patterns in `exclude` and they won't be built or
flagged as orphans (the equivalent of mkdocs' `exclude_docs`):

```python
site = Site(
    ...,
    exclude=("plans/", "decisions/", "release_notes/[0-9]*.md"),
)
```

## Built in

- **Markdown**: headings (GitHub slugs + TOC), fenced code, nested lists,
  blockquotes, thematic breaks, GFM tables, admonitions (`!!! note`), content
  tabs (`=== "Tab"`), YAML front-matter, and inline code / strong / emphasis /
  links / autolinks.
- **Syntax highlighting** for python, bash, c, and json — a small built-in
  tokenizer, no Pygments, no dependency.
- **Client-side search** — a build-time JSON index and a tiny vanilla-JS box in
  the header; no lunr, no service.
- **API reference** via the `:::` directive.
- **Reader polish**: previous/next page links (from nav order), a scroll-spy
  "on this page" TOC, and a copy button on every code block.
- **`llms.txt`** — an [llmstxt.org](https://llmstxt.org) index of the whole site,
  generated automatically, so coding agents and LLMs can navigate your docs.
- **`sitemap.xml`** and per-page `<meta name="description">` (from front-matter)
  when you set `base_url` — SEO without a plugin.
- **Theme**: light/dark following the OS, one small stylesheet, no CDN or JS
  framework beyond the ~2 KB search/tabs/copy/theme script.

`wreath docs check` is a real gate: beyond dead `.md` links, it validates every
`#anchor` — an internal link to a heading that has moved or vanished fails the
build, so cross-references can't rot silently.

Wreath's own documentation site — the one you're reading — is minted by this
generator from a `wreath_docs.py` at the repository root: 128 published pages,
`wreath docs check` clean under `strict`. The follow-on is the native `_docs` C
extension for full-CommonMark parsing (via a versioned render tape the pure
renderer shares) — the config, CLI, theme, and strict-check surface above are
stable, so that's a drop-in behind the same `markdown.render()` seam.
