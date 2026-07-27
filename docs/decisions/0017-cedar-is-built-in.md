# 0017. The Cedar engine is built in, not a dependency

Date: 2026-07-27
Status: Accepted
Absorbs: the retired `built-in Cedar engine` record.

## Context

Authorization beyond role checks needs a policy language. Cedar is a good one:
explicit `permit`/`forbid` with forbid-overriding-permit, default deny, an
entity hierarchy, and a published grammar.

The reference implementation is Rust. Binding it means a compiled dependency
with its own toolchain, which ADR 0002 exists to avoid, and a version-coupling
between Wreath's release cadence and an external crate's.

## Decision

Wreath implements Cedar itself: a C-first evaluator in
`src/wreath/_native/cedar.c` and `authz.c` with a pure twin, exposed through
`CedarPolicies` (`src/wreath/_auth/cedar_engine.py:837`).

Policies are parsed at startup, not per request. Extensions and schema
validation fail loudly at parse time. Forbid overrides permit; the default is
deny.

## Consequences

- No Rust toolchain, no crate version coupling, no second build system.
- Wreath owns Cedar bugs and Cedar's spec drift. The subset implemented must be
  documented as a subset rather than implied to be complete.
- Authorization is one engine serving both route-level `@authorize` and per-row
  `object_authorizer`, so a policy cannot mean two things depending on where it
  is enforced.
- `permissions_router` answers *what may this caller do* from the same
  authorizer that enforces it, with the action vocabulary read off the routes'
  own declarations rather than a second hand-maintained list.
- Per-request entities were a performance trap: `is_authorized(entities=...)`
  rebuilt the transitive closure over the entire static hierarchy on every call,
  which is quadratic on a chained hierarchy — measured at 10.4 ms for 400
  entities against 8 µs after layering the request's entities over a precomputed
  base. Row-level authorization is precisely the caller that passes per-request
  entities, so the trap was in the path that most needed it to be fast.

## Alternatives rejected

- **Bind the Rust reference implementation.** Rejected on ADR 0002, and on
  release coupling.
- **A bespoke policy DSL.** Rejected: inventing a language means owning its
  semantics with no external specification to be checked against, and Cedar's
  forbid-overrides-permit default-deny shape is the part that matters.
- **Roles and permissions only.** Rejected: they cannot express row-level rules,
  and the alternative to expressing them is scattering the same rule across
  handlers.

## What would reverse this

A pure-Python Cedar implementation maintained by the Cedar project itself, or a
stable C ABI for the reference implementation that could be linked optionally
under ADR 0006's rules.
