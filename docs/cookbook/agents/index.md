# Cookbook for coding agents

If you're an agent working in this codebase, this section is written for you.
The wreath is a fellowship of parts held in one shape, and a change is welcome
into it only when it keeps that shape — when it preserves the invariants below
and can prove it works. Start with
[`AGENTS.md`](https://github.com/alexogeny/wreath/blob/main/AGENTS.md) and
[`repo-map.md`](https://github.com/alexogeny/wreath/blob/main/repo-map.md); the map
points you at the source, tests, and benchmarks for every subsystem.

Before grepping for where something lives, look it up in
`docs/agents/manifest.json`. Every subsystem is there with its guides, reference
pages, sources, focused tests, the invariants it must hold, and the decisions
behind it — and `uv run wreath-map-lint` fails the build if any of that drifts
from the repository, so it is safe to trust.

- **[The gates](checks.md)** — every check a change must pass, and what each one
  is actually protecting.
- **[Add an endpoint or model](add-an-endpoint.md)** — the smallest correct
  change, from route to test.
- **[Verify a change](verify-a-change.md)** — how to prove behaviour, not just
  green tests, including red-teaming the failure paths with
  [replay and fault injection](../recipes/fuzz-your-routes.md).
- **[Documenting a module](documenting-a-module.md)** — the docs a new public
  module or feature must ship with, and the voice they must be in.
- **[One name, one meaning](naming.md)** — the verbs that appear in more than
  one subsystem, what each means, and which to reach for. Read it before adding
  a `component()`, a storage port, a `stats()`, or a native fallback.

## Invariants to preserve

These are the shape of the wreath. Hold them.

- **Correctness and ASGI semantics come before speed.** Never claim a performance
  win from a single run.
- **The dependency direction is one-way:** `wreath.orm` → `wreath.postgres`,
  never the reverse. The driver knows nothing about models.
- **Configuration and state stay distinct.** Configuration is how Wreath starts;
  state is what it holds while running. They don't share an API.
- **The public top level stays small.** A new feature lives in its own
  clearly-named module, not re-exported from `wreath`.
- **An accelerator is checked against something outside Wreath.** The RFC, the
  published vectors, the stdlib — never a second implementation of ours, which
  can be wrong in both halves and agree.
