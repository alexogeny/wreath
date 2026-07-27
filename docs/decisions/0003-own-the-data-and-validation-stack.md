# 0003. Own the validation and data stack; no Pydantic, no SQLAlchemy

Date: 2026-07-27
Status: Accepted

## Context

The dominant Python API stack is FastAPI over Pydantic over SQLAlchemy. A new
framework can integrate with it, compete with it, or ignore it. Integrating
means inheriting two large dependencies (ADR 0002) and, more importantly, two
type systems that must be reconciled with the one the framework already needs
for routing, OpenAPI generation, and client typegen.

The specific friction: Pydantic owns validation *and* serialization *and* schema
generation. Wreath needs all three, keyed to the same declarations that drive
`wreath.orm` column types, `wreath.temporal` instants, and the TypeScript
emitter. Two owners of that mapping is one too many.

## Decision

Pydantic is permitted only as a benchmark or test dependency, never in runtime
code or a public API. No SQLAlchemy integration or compatibility layer exists;
Wreath ships and owns its PostgreSQL driver and ORM (`AGENTS.md`, Engineering
rules).

Validation is `src/wreath/binding.py` with a native plan interpreter in
`src/wreath/_native/validate.c`. Models are plain dataclasses or
`wreath.orm.Model` subclasses.

## Consequences

- One declaration settles the ORM column, the binding coercion, the REST JSON
  shape, the OpenAPI `format`, the typegen alias, and the GraphQL scalar.
  `wreath.temporal` is the clearest instance of the payoff.
- Users arriving from the FastAPI stack must translate. That cost is met head-on
  rather than papered over: `docs/from-fastapi/` gives the equivalence tables,
  and `wreath port` (`src/wreath/_port/`) analyses a FastAPI application
  statically and reports what it cannot translate rather than guessing.
- Extra fields on a body are always rejected. Wreath has no `model_config`
  equivalent to relax that, by design.
- Every validation defect is ours. The `validate-unexpected-fields` and
  `validate-union-bomb` complexity probes exist because of it.

## Alternatives rejected

- **Accept Pydantic models as body types.** Rejected: it splits schema
  generation between two systems that disagree at the edges — `Field(ge=…)`
  versus `Query(minimum=…)` is the visible seam, and OpenAPI output is where the
  disagreement would surface as a wrong contract.
- **A SQLAlchemy Core compatibility layer for queries.** Rejected: the ORM's
  one-way direction (ADR 0015) and its explicit-loading rule are not expressible
  through a layer designed around lazy attribute access.
- **Support both and let users choose.** Rejected as the most expensive option:
  it is every cost of integration plus every cost of ownership.

## What would reverse this

Nothing plausible for SQLAlchemy. For Pydantic, a stable ABI-level protocol for
validation and schema that Wreath could implement rather than adopt — the
decision is against inheriting an implementation, not against interoperating.
