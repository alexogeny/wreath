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
- **broken anchors** — a `#heading` no heading on that page produces → error, exit 1
- **orphan pages** — a `.md` under `source/` that no nav entry references → warning

It's the same drop-into-a-pipeline ergonomics as `wreath migrations check`.

**Being an orphan does not exempt a page from the link check.** The two are
separate signals and both are reported: `orphan page not in nav: drafts/new.md`
says where the page sits in the nav, and `drafts/new.html (orphan): dead link to
gone.md` says its links don't resolve. Reachability decides what gets *written* —
an orphan is not built, so a nav page linking to one still counts as dead — but
never what gets *verified*. A page that isn't in the nav yet is exactly the page
whose links nobody has clicked, and the checker used to skip it entirely: a
deliberately broken link on an orphan drew no warning at all, and three of them
surfaced at once the day the page joined the nav. An orphan's links are resolved
against the nav plus the other orphans, so a set of pages written together checks
out before any of them lands. To opt a directory out of both signals, `exclude`
it.

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

- **Themes** (colours, light + dark): `wreath` (evergreen on a green-cast paper,
  brass held back for state), `slate`, `sepia` (warm paper, serif), `nord`,
  `terminal`.
- **Feels** (surface treatment): `flat` (default), `elevated` (soft shadows),
  `papery` (faint grain + soft shadows), `hardcore` (square, heavy borders,
  uppercase headings), `orby` (big radii, pill controls, glow).

Both are just CSS variables, so a bespoke palette is a `Palette(primary=…, …)`
away — no theme build step. A palette also picks its two type voices: `font` is
the reading face (`"system"`, `"serif"`, or `"mono"`) and `display` is the
heading face. `dark_primary` and `dark_accent` cover the case a light-first
palette always gets wrong — a brand colour tuned to read *on paper* is too dark
to *fill* anything on a near-black ground, so chart bars and rules vanish in dark
mode. Leave them empty and they are derived by lightening.

### What the theme guarantees

The look is a **design system, not a pile of values**: one type scale (a 1.25
ratio from a 16px base), one 4px space scale, one elevation ramp, and a set of
colour roles. Every rule spends those tokens, which is what keeps a page looking
deliberate rather than approximate — and it means a bespoke palette inherits the
proportions for free.

Three properties hold for **every** theme, and each is a test:

- **AA contrast in both modes** — body text, secondary text, links, and text on
  the code surface all clear 4.5:1, light and dark. Control boundaries and the
  focus ring clear 3:1 (WCAG 1.4.11); the decorative hairline deliberately does
  not, because a table rule at 3:1 is a cage.
- **Syntax colours belong to your theme.** Each token is a hue tuned for
  legibility, then mixed toward the page foreground so it sits in the palette
  instead of on top of it — a code block in `sepia` reads warm, not GitHub-blue.
  The measured floor is 5.2:1 across all five themes and both modes.
- **Nothing reaches the network.** No CDN, no web font, no remote image. The
  design tokens are inlined into every page, so a page keeps its colours even if
  the stylesheet is missing — and `wreath audit`'s contrast rules, which only
  read inline `<style>`, can actually check them.

Motion honours `prefers-reduced-motion`, tables scroll inside their own
container rather than scrolling the page, there is a skip link, both navigation
landmarks are labelled, and there is a print stylesheet.

!!! note "One audit warning is deliberate"

    `wreath audit` flags the component stylesheet as a render-blocking `<link>`
    on every page, and it stays that way on purpose. The tokens and the layout
    frame *are* inlined; what is left in the external file is the components —
    the sidebar, code blocks, callouts, tabs. Loading that asynchronously would
    trade a blocked first paint for a flash of unstyled components on every
    navigation, which is the worse of the two. The file is one small same-origin
    request, cached across the whole site.

### Three type voices

The theme sets its chrome in three faces with one job each, and the split is the
project's own rule — *the brand may be poetic, the API must stay literal* — made
visible. A serif carries page titles and section heads; the body face carries the
prose; and the **mono face carries structure**: nav section labels, the table-of-
contents head, table headers, admonition titles, chart captions, keyboard hints,
code. That last one is what makes the hierarchy readable without adding a single
box or rule. None of the three is downloaded, so there is no font flash and no
network request.

### Finding your way in a large site

Once the nav has three or more top-level entries, the top level moves into a row
of **section tabs** in the header and the sidebar shows only the section you are
in (`tabs="never"` keeps the whole tree in one scroller). Inside that tree the
branch you are on is drawn as **one continuous thread** from the section root
down to a dot on your current page, so "you are here" carries its whole ancestry
rather than being an isolated highlight.

## What the built site does at runtime

The runtime is one cached `assets/docs.js`, no framework and no build step, and
it is **enhancement only**. With scripts off you keep every page, every link,
every nav section, every content tab, and the whole table of contents; you lose
the four things below and nothing else.

- **Search palette** — `Ctrl K` (`⌘K` on a Mac) or `/`. Results are scored per
  *section*, not per page, so a hit lands on the heading you wanted; matches are
  highlighted, results group under their page, and arrow keys move real focus
  between them. The index is fetched once, on your first keystroke.
- **Instant navigation** — links prefetch on hover and swap `<main>` in place,
  with a View Transition where the browser has one. The point is not the
  milliseconds; it is that a 147-page sidebar does not blink and lose its scroll
  position on every click.
- **Copy buttons** on every code block, and a table of contents that tracks the
  section you are reading.
- **A three-state theme control** — system, light, dark. A two-state toggle
  silently throws away "follow the OS" the first time you press it, with no way
  back.

## Writing the content

Beyond CommonMark basics, tables, and the `:::` reference directive:

- **Admonitions** — `!!! note "Title"` for a static callout, `??? note` for one
  that starts collapsed, `???+ note` for one that starts open. Only warnings and
  errors get a background wash; a note and a warning that look equally loud are
  two decorations, while a warning that looks louder is information.
- **Content tabs** — `=== "Tab title"` with an indented body. They compile to a
  CSS radio group, so they work with no JavaScript and are keyboard-navigable
  natively.
- **Code fences** carry attributes: ` ```python title="app.py" hl_lines="3 7-9" `.
  A `title` adds a header strip naming the file; `hl_lines` shades those lines.

## Opening a page with a hero

A page that has an argument to make rather than an API to document can open with
a `hero` block — a mono eyebrow, a display headline, a lede, and up to four
actions:

````markdown
```hero
eyebrow: The request path
title: Most of a request never reaches Python.
lede: Ingress, routing, and authorization are native code.
action: See the benchmarks -> ../perf/index.md
action: How we know -> #how-we-know
```
````

The headline becomes the page's real `<h1>` and its `<title>`, so a hero page
needs no separate `#` heading. Action targets go through the same `.md` →
`.html` rewrite and the same dead-link check as any other link on the page — a
hero pointing at a page you deleted fails the build.

There are four fields and no way to add a fifth. A hero is the one place a docs
page is allowed to be loud, and the way to stop that spreading is to give it a
shape it cannot outgrow.

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
wreath arm (`Wreath (metal)`, `(native)`, `(ASGI)`, …) are drawn as one hue at
three strengths — they differ by how much of the stack is native, which is an
ordered quantity — and everything else renders as a muted hatch, so your own
series never blends into the field. Chart data files that live under your `source`
tree are copied into the built site, so a "raw data" link to the JSON resolves.

## Explaining a mechanism

Some things are easier to watch than to read. A `figure` block draws one of a
small set of hand-built diagrams as inline SVG, animated with CSS:

````markdown
```figure
name: timing-wheel
title: Cancelling a timer
note: Both sides cancel the same timer; the squares count what it costs.
```
````

This is deliberately **not** a diagram language. Each figure is a specific
argument about wreath's own machinery, drawn on purpose — `request-boundary`,
`route-bitset`, `timing-wheel` — and they live on
[Under the hood](../internals/index.md). A general renderer would be a
dependency and would draw worse. Naming a figure this build doesn't have
produces a visible note rather than an empty box.

If you are adding one, three rules make the difference between a diagram and a
decoration:

- **Draw the operation, not the state.** The first version of these showed two
  structures sitting still, and nobody could tell what was being compared.
  Showing the *same operation* run on both, with a counter for the steps it
  takes, is what makes a comparison legible.
- **Label the symbols.** A grid of lit and unlit cells means nothing until each
  row is named.
- **Say what the resting state is.** Every reveal keyframe ends hidden, so the
  `prefers-reduced-motion` block has to name the elements that must stay
  visible — otherwise the whole figure is blank for anyone who asked for no
  animation.

Motion can be stopped from the figure's own header, with a checkbox rather than
a script, so the control exists whether or not any JavaScript loaded.

## Serving the built site from your app

The output is a plain directory, so mount it like any other static tree:

```python
app.static("/docs", "site")
```

Set `source_url` on the `Site` and every page also gets an "Edit this page" link
built from its own source path:

```python
site = Site(..., source_url="https://github.com/you/proj/edit/main/docs")
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
an agent manifest. List glob patterns in `exclude` and they won't be built, or
flagged as orphans, or link-checked — `exclude` means "not part of this site", so
it opts out of every signal, which is the difference between it and simply
leaving a page out of the nav (the equivalent of mkdocs' `exclude_docs`):

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
- **Theme**: a tokenised design system (type/space/elevation scales), light/dark
  following the OS with a toggle, AA contrast in every theme, reduced-motion and
  print styles — one small stylesheet, no CDN or web font, and no JS framework
  beyond the ~2 KB search/tabs/copy/theme script.

`wreath docs check` is a real gate: beyond dead `.md` links, it validates every
`#anchor` — an internal link to a heading that has moved or vanished fails the
build, so cross-references can't rot silently.

Wreath's own documentation site — the one you're reading — is minted by this
generator from a `wreath_docs.py` at the repository root: 128 published pages,
`wreath docs check` clean under `strict`. The follow-on is the native `_docs` C
extension for full-CommonMark parsing (via a versioned render tape the pure
renderer shares) — the config, CLI, theme, and strict-check surface above are
stable, so that's a drop-in behind the same `markdown.render()` seam.
