# 0004. Each feature lives in the module its name implies

Date: 2026-07-27
Status: Accepted

## Context

Wreath's brand is poetic. The temptation in a framework with a name like this is
to theme the API to match — threads, roots, kindling, leaves. That reads well in
a README and costs every user afterwards, because a themed name is a name you
must be taught.

There is a second, sharper pressure: agents. A coding agent arriving cold either
guesses a path correctly or greps for it, and a wrong guess costs a plan.

## Decision

**The brand may be poetic; the API must stay literal.** `wreath.pagination` is
`src/wreath/pagination.py`. `wreath.jobs` is `src/wreath/jobs.py`. A leading
underscore means implementation, reached through the facade that exports it,
never directly.

Enforced rather than encouraged: `wreath-map-lint`'s `MAP003` fails when a public
module under `src/wreath` belongs to no subsystem in
`docs/agents/manifest.json` (`src/wreath/_devtools/map_lint.py:162`), so a new
public surface cannot appear unmapped.

## Consequences

- Guessing the path is usually right, which is the whole point.
- The facade pattern costs a layer: `wreath.postgres` re-exports from
  `src/wreath/_locks.py`, and a reader chasing `SingletonRunner` finds the
  facade first.
- Some names are duller than they could be. `wreath.objects` for blob storage
  and `wreath.store` for the keyed-table primitive are close enough to confuse,
  and the documentation has to say so explicitly rather than relying on evocative
  names to separate them.
- No module may be renamed for elegance. `storage.py` became `objects.py`
  because the *contents* changed meaning, not because the new name reads better.

## Alternatives rejected

- **Themed module names.** Rejected: a technical term that has been themed must
  be translated before it can be used, by every reader, forever.
- **A single flat `wreath` namespace.** Rejected: it makes the import line short
  and the module enormous, and it destroys the guessability the record exists to
  protect.
- **Convention documented but unenforced.** Rejected on evidence — the maps
  drifted badly once, naming seven public modules that had since been made
  private, which is what `map_lint.py` was written to stop.

## What would reverse this

Nothing for the naming rule. The *enforcement* would change if the manifest were
replaced by something derived from the tree rather than maintained beside it —
a generated map cannot drift, and `MAP003` exists only because this one can.
