# Documenting a new module or extension

When you add a public module, a feature to one, or turn a reserved scaffold into
a real implementation, the docs are part of the change — not a follow-up. A
feature that isn't documented, or is documented in the wrong voice, isn't done.

## The voice, first

Wreath's documentation has one rule, and it is not optional:

**The brand may be poetic. The API must stay literal.**

Use wreath/circle/fellowship/binding/woven-together imagery in *framing and
narrative* — the home page, a guide's opening, a section intro. Never rename a
technical concept to fit the theme: a middleware is a middleware, a dependency is
a dependency, startup is startup, a connection pool is a connection pool. And
write *warmly* — explain the why, give the reader orientation before the code.
Do not write in the clipped, terse register the docs were first drafted in; that
was inherited from the old name and is being removed. When in doubt, read
`docs/index.md` and `docs/guides/binding.md` and match them.

## What every new public module needs

Work through all of these; the strict docs build (below) fails if the nav and the
pages disagree.

1. **A reference page** — `docs/reference/<module>.md`. A short, hand-written
   intro (what it is, when to reach for it) followed by mkdocstrings:

   ```markdown
   # `wreath.<module>`

   <one or two warm sentences on what this is and when to use it>

   ::: wreath.<module>
   ```

   Add it to the `API reference` section of `nav:` in `mkdocs.yml`.

2. **A guide** — `docs/guides/<topic>.md`, one per major extension. Open with the
   reasoning, then a real example, then a "Reference:" line linking the reference
   page. Add it to the `Guides` section of the nav.

3. **Cookbook recipes** — if the module enables a common task, add a developer
   recipe under `docs/cookbook/recipes/` and, when it changes how the codebase is
   worked on, an agent recipe under `docs/cookbook/agents/`. Add each new page to
   the `Cookbook` nav and link developer recipes from `docs/cookbook/index.md`.

4. **`docs/llms.txt`** — add the new guide and reference to the curated map so
   coding agents can find them.

5. **Home and getting-started** — only if the feature is headline-level. Keep the
   top-level pages small; most features live in their own guide.

## Cross-cutting rules

- **Every Markdown page must be in the nav** (`strict: true`), except paths
  covered by `not_in_nav` / `exclude_docs` in `mkdocs.yml` — currently the
  generated `release_notes/<version>.md`, and the hidden `plans/` and
  `decisions/` trees. Do not add a page and forget its nav entry.
- **Reference is generated, guides are written.** Never hand-transcribe
  signatures into a guide; link to the reference and let mkdocstrings keep it
  accurate.
- **Reserved scaffolds** (`telemetry`, `inspector`, `recording`, `replay`,
  `migrations`) are listed on `docs/reference/roadmap.md`. When you implement
  one, remove its row there and give it a real reference page and guide.

## Verify

```bash
uv run wreath-docs            # strict build; fails on a missing nav entry or bad link
uv run wreath-check --docs    # the full gate, including the strict docs build
```

A green strict build is the gate: it proves every page is reachable and every
autodoc target resolves.
