# Output modes & safety posture

## In-place vs sister folder

- `wreath port SRC --output ../app-wreath` (default, safest) writes a parallel
  tree, preserving package layout so imports resolve.
- `wreath port SRC --in-place` rewrites files where they sit; it refuses on a
  dirty git tree unless `--force` (git is the undo).

**Import rewriting** is driven by the same rule catalog: `from fastapi import
APIRouter` → `from wreath import Router`, `from pydantic import BaseModel` →
`from dataclasses import dataclass, field`, and so on. An unmapped import is left
intact and reported — never dropped.

## Marking uncertain translations

Every non-1:1 site gets an inline annotation:

```python
guests: int  # TODO(wreath-port: move ge=1/le=12 to a model check) [pydantic-field-constraint]
```

plus a machine-readable companion report `wreath-port-report.json` (and a `.md`
render). Each finding carries `file`, `line`, `construct`, `tag`, `rule_id`,
`message`; the top level carries `counts` (`translated` / `needs_review` /
`unsupported`) and the auto-translation percentage.

## Idempotency

Each emitted file carries a provenance header: the tool version, the **hash of
the source it was generated from**, and the **hash of the emitted output**. On
re-run:

- source hash unchanged → skip regeneration (a true no-op),
- emitted file's current hash ≠ recorded output hash → it was **hand-edited** →
  refuse to overwrite (report "hand-edited, skipped") unless `--force`.

Re-running after upstream source changes is therefore safe: only changed sources
are re-emitted, and hand-edits are never clobbered. A later `--merge` mode
re-emits only the declarative surface while preserving edited bodies.

## The fail-safe contract

1. **Business logic is never "translated."** The only operation on a function
   body is verbatim copy plus optional `# TODO` insertion — never expression
   rewriting. (This is why ORM query calls and bespoke auth are annotate-only.)
2. **When in doubt, don't transform.** Low-confidence constructs are left
   byte-identical and reported; there is no best-effort guess path.
3. **Every non-1:1 edit is both annotated inline and counted** — `grep
   'TODO(wreath-port'` reconciles with the report.
4. **Nothing is silently dropped** — a token is either reproduced or transformed
   by a named rule.
5. **Round-trip guard** — every emitted file is re-parsed with `ast.parse`; a
   structurally broken emit is a tool bug and fails loudly, never ships.
6. **Human review is the declared final step.** The output is a correct,
   reviewable *starting point*, and the report is the review checklist.
