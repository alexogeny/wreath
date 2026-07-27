# 0001. Target CPython 3.14 and nothing older

Date: 2026-07-27
Status: Accepted

## Context

Wreath began after Python 3.14 shipped. The version carries three things the
framework depends on structurally rather than incidentally: the free-threaded
build as a supported configuration, the JIT as a tested execution mode, and a
C API stable enough to build against without version-guard thickets in every
extension.

Supporting 3.11 or 3.12 as well would mean either abandoning those or carrying
a compatibility layer through `src/wreath` and every `.c` file in
`src/wreath/_native/`.

## Decision

`requires-python = ">=3.14"` (`pyproject.toml:10`). No compatibility shim for
any earlier version exists, and none may be added without reversing this record.

## Consequences

- The framework is unavailable to a large installed base. This is the cost, and
  it is real: most production Python is not on 3.14.
- Native extensions compile against one C API. No `#if PY_VERSION_HEX` ladders.
- Free-threading and the JIT are *separately tested modes* rather than
  hypotheticals (`AGENTS.md`, Engineering rules).
- New syntax and stdlib may be used the day it lands. `wreath.temporal` builds
  on the stdlib rather than vendoring `arrow` partly for this reason.

## Alternatives rejected

- **Support 3.11+.** Rejected: it forecloses the free-threaded build, which is a
  reason this project exists rather than a feature it happens to have.
- **Support 3.13 as a floor.** Rejected as the worst of both — it still costs
  version guards, while buying an installed base barely larger than 3.14's.

## What would reverse this

Adoption evidence that a named, wanted deployment target cannot reach 3.14, and
a measurement showing the compatibility layer costs less than the users it wins.
The bar is deliberately concrete: "people are on 3.12" is not the argument, "this
specific deployment we want cannot upgrade" is.
