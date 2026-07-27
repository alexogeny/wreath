# 0015. The ORM depends on `postgres`; loading is always explicit

Date: 2026-07-27
Status: Accepted

## Context

An ORM and its driver co-evolve, and the easy shape is mutual: the driver grows
model-aware fast paths, the ORM reaches into connection internals for
efficiency, and after a while neither can be tested or reasoned about alone.

Separately, lazy loading is the single most common source of accidental N+1
queries. An attribute access that silently issues a query is invisible at the
call site and catastrophic in a loop.

## Decision

**One-way direction: `orm → postgres`, never the reverse.** `wreath.postgres`
knows nothing about models. The ORM builds on the driver's public surface.

**Loading is always explicit.** There is no lazy relationship access. A relation
is loaded by asking for it — `.include(...)` — and an unloaded relation raises
rather than querying.

## Consequences

- The driver is usable without the ORM, and testable without models.
- Native model hydration lives in `src/wreath/_native/postgres/` as a driver-side
  capability driven by a *shape* the ORM computes, not by the ORM's types —
  which keeps the direction intact while allowing the fast path.
- N+1 is not preventable by accident, but it is not creatable by accident
  either. `NPlusOneGuard` raises from inside the offending query in dev and
  staging, and `wreath doctor n-plus-one` diagnoses a running server from
  recorded hydrate phases without reproducing anything.
- Users coming from lazy-loading ORMs must change how they write reads, and
  `wreath port` classifies `.objects.` chains per verb with `select_related` →
  `.include(...)` pointing at the fix.
- Extra explicitness in every query that traverses a relation. This is the cost,
  paid at every call site.

## Alternatives rejected

- **Lazy loading with an N+1 warning.** Rejected: the warning fires after the
  queries, in an environment where somebody is watching, which is not
  production.
- **Lazy loading disabled by default, enabled per session.** Rejected: a global
  mode that changes what an attribute access *does* is worse than either
  consistent choice.
- **Driver-side model awareness for speed.** Rejected as stated; the shape-based
  hydration above gets the speed without the coupling.

## What would reverse this

Nothing for the direction. For explicit loading: a mechanism that makes a lazy
access statically visible at the call site — which would no longer be lazy
loading as the term is used.
