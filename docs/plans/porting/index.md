# Porting FastAPI apps to Wreath (`wreath port`)

!!! note "Analysis, whole-tree emission, inventory, and verification ship"
    `wreath port <src> --report-only` remains the static planning mode, while
    `--output` emits a guarded sister tree and `--verify ... --cases ...`
    compares source and candidate ASGI responses. The pages in this directory
    preserve the design sequence and corpus rationale; the
    [user guide](../../guides/porting.md) is the current contract.

`wreath port` is a source-to-source translator: point it at an existing
FastAPI / Pydantic / SQLAlchemy-or-ormar / Alembic application and it emits
equivalent **native wreath** code, so teams don't hand-port everything.

## The load-bearing constraint: a purely static analyzer

`wreath port` **never imports or runs the target application.** Real apps depend
on private package registries, run side effects at import time (opening database
config at module scope, initializing error reporting, registering routers in
loops), and may target a different Python version. So the tool reads source
**statically** with the standard-library `ast` module and reasons about it — it
is the inverse of every other wreath introspection path (which reflects on a
*loaded, running* app).

## What it does, in one line

> **It transpiles declarations and copies logic.**

Declarative surfaces — routers, decorators, model/column declarations, parameter
signatures, imports, settings — are rewritten into wreath idioms. Imperative
**bodies** (handler logic, dependency bodies, business code) are copied
byte-for-byte, with `# TODO(wreath-port: …)` annotations added wherever a human
needs to finish the job. Nothing is silently dropped, and nothing subtly-wrong
is emitted with false confidence.

## The pages

- [Rule catalog](rule-catalog.md) — every construct, tagged 1:1 / lossy / unsupported.
- [Output & safety](output-and-safety.md) — in-place vs sister-folder, the report, idempotency, the fail-safe posture.
- [Coverage & corpus](coverage-and-corpus.md) — how coverage is measured, and the honest number.
- [Phased plan](phased-plan.md) — the historical sequence from the first analyzer to assisted query work.
