---
name: release-notes
description: >-
  Summarize the changes between two wreath versions into a curated release-notes
  page at docs/release_notes/<version>.md, and link it from the release-notes
  index. Use when preparing a release, cutting a version, or when someone asks
  for release notes / a changelog for a version. The publish.yml workflow
  requires this file to exist before it will cut the release.
---

# Release notes

Produce `docs/release_notes/<version>.md` — the human-facing summary of what
changed since the previous release. `.github/workflows/publish.yml` uses this
file verbatim as the GitHub Release body and **fails the release if it is
missing**, so generate it before bumping the version in `pyproject.toml`.

## 1. Determine the version range

- **Target version** = the `version` in `pyproject.toml`. If the caller names a
  version, prefer that and confirm it matches (or will match) `pyproject.toml`.
- **Previous version** = the newest of:
  - the highest-numbered existing `docs/release_notes/*.md` (excluding `index.md`), or
  - the latest `v*` git tag (`git tag --list 'v*' --sort=-v:refname | head -1`).
  - If neither exists this is the first release; summarize from the repository
    root (`git log --no-merges`).

Compute the commit range: `git log --no-merges <prev_tag>..HEAD` when a previous
tag exists, otherwise `<prev_version_release_commit>..HEAD`, otherwise all
history. If git has no commits yet, ask the caller what to include rather than
inventing entries.

## 2. Gather and classify changes

Read the commit subjects/bodies and, where a subject is terse, the diff of that
commit. **Never invent changes** — every bullet must trace to a real commit or
diff. Group into these sections, dropping any that are empty:

- **Breaking changes** — anything that changes a public API, a default, wire
  format, or config surface. Lead with these; note the migration in one line.
- **Added** — new user-facing features or public API.
- **Changed** — behavior changes that are not breaking.
- **Fixed** — bug fixes (name the symptom, not just the internal cause).
- **Performance** — measurable wins; cite the number if the commit states one.
- **Docs & internals** — a short roll-up; do not enumerate every chore.

Write for a user of the framework, not a committer: describe the observable
effect, collapse a cluster of commits into one bullet, and omit merge/CI/format
noise. Keep bullets to a line or two. Link PRs/issues as `(#123)` when known.

## 3. Write the page

Create `docs/release_notes/<version>.md` using this template (omit empty
sections, keep the heading order):

```markdown
# v<version>

_Released <YYYY-MM-DD>._

<One or two sentences on the theme of the release.>

## Breaking changes

- ...

## Added

- ...

## Changed

- ...

## Fixed

- ...

## Performance

- ...

## Docs & internals

- ...
```

Use today's date. Match the terse, precise tone of the existing docs (see
`docs/index.md` and the guides).

## 4. Link it from the index AND add it to the nav

Both, not either. In `docs/release_notes/index.md`, insert a newest-first link
just below the `<!-- releases:start -->` marker:

```markdown
- [v<version>](<version>.md) — <YYYY-MM-DD>
```

Then add the page to the `Release notes` section in `wreath_docs.py`, newest
first:

```python
    Section(
        "Release notes",
        Page("Overview", "release_notes/index.md"),
        Page("v<version>", "release_notes/<version>.md"),
        ...
    ),
```

**This step used to say the opposite** — that the nav entry was unnecessary
because `exclude` exempted `release_notes/[0-9]*.md`. That was wrong in a way
nothing could catch until a release was actually cut: the site generator
withholds an orphan page's *output* on the stated grounds that nothing links to
it (`_docs/site.py`), so a page that **is** linked from the index but **is not**
in the nav is never written, and `wreath docs check` fails with a dead link. The
first release to follow this skill hit exactly that. If you are tempted to drop
the nav entry again, run `wreath docs check` before believing it.

## 5. Report

Tell the caller the path written, the version range summarized, and the section
counts, then remind them the publish workflow will pick this file up once the
`pyproject.toml` version bump lands on `main`.
