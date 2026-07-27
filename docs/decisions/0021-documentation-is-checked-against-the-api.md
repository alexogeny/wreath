# 0021. Documentation is checked against the real API

Date: 2026-07-27
Status: Accepted

## Context

Five documented spellings shipped that do not exist:

- `db.pool("read").fetchval("SELECT 1")` — `Pool` leases connections and has no
  query methods.
- `async with db.pool("write").acquire() as conn`, in three places — `acquire`
  is a plain coroutine.
- `db.ping()` — `Database` has no `ping`.
- `Depends` inside `Annotated`, in four places — silently ignored.
- `pagination.page_params` as a `Depends`, which fails in both spellings.

The health-check one was the worst: `_run_check` **catches** the `AttributeError`
and reports a failed check, so a service copying that recipe answers
`/ready` → 503 forever, with the reason buried in a JSON body, and the load
balancer drains the fleet.

None of this was five mistakes. It was the expected output of a system with no
mechanism: `wreath docs check` verified links, anchors and orphans — structure —
and would happily ship a block that raises on line one.

## Decision

Every Python block in `docs/` is in exactly one of three states, and an
**unmarked block is a hard failure**:

1. **Floor-checked** (the default). Attribute access is resolved statically
   against the real objects; an unresolvable name fails the build. This catches
   `fetchval`, `ping`, and `acquire`-as-context-manager without executing
   anything, and works on fragments that can never run.
2. **Executed.** The page's blocks run in order in one shared namespace, with no
   skip-list — so a block that cannot run is a block that should not be in the
   page.
3. **Declared non-executable, with a written reason** (`no-run: fragment, no app
   in scope`). A reason is reviewable; a bare flag becomes noise.

The floor is backed by a **coverage ratchet**: it reports how many attribute
chains resolved, pinned by a test. One bad vocabulary entry would drop resolution
to near zero *while still exiting 0*, which is the characteristic failure mode of
this class of tool.

## Consequences

- A fictional method cannot be published. A block that runs and does the wrong
  thing still can, which is why executed pages exist for anything teaching a
  whole workflow.
- 269 of 307 published blocks are fragments, so "just run them all" would mean
  rewriting teaching prose into programs. The three-state design exists because
  of that measurement, not despite it.
- Nine real defects surfaced on five pages the day the floor landed.
- Reference pages were already live in this sense — generated from real
  signatures, so they cannot lie about one. This extends the property to prose.
- The generated reference had a *scope* gap of its own: it rendered neither
  properties, classmethods, nor inherited members, so `Request.method`, `.path`,
  `.headers` and `.cookies` — the most-used API in the framework — were absent
  from its own reference.

## Alternatives rejected

- **Execute everything.** Rejected on the 269 fragments.
- **A doctest-style convention, unenforced.** Rejected: convention is what
  produced the five fictions.
- **Mark every block explicitly.** Rejected — it needs ~380 bulk `no-check`
  reasons, and reasons nobody considered are worse than noise because they *look*
  reviewed. The ratchet gives the same assurance without the lie.

## What would reverse this

Nothing. The next step is more executed pages, not fewer checks.
