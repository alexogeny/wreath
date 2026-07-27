# Phased delivery

The tool is built in tiers of increasing ambition and risk. Each tier is gated
on the previous one holding its precision bar on the corpus golden tests.

## Phase 0 — report-only ✅ implemented

`wreath port SRC --report-only` (also `--json`). The `wreath._port` package: the
`ast` analysis pass + cross-module symbol table (`_Imports`/`_index_tree`) + Port
IR (`Finding`/`Report`) + rule catalog/classifier (`rules.py`), emitting **only**
the report (counts + `file:line` + tags + per-category coverage) over the whole
tree. Public API: `wreath.port.analyze(root)` / `analyze_all(roots)` → `Report`
(`.to_json()`, `.to_markdown()`, `.coverage(category)`, `.coverage_overall()`).
Immediate value — a scoped migration plan — with **zero emit risk**; it also
bootstraps the coverage metric the test suite gates on. Verified standalone
against the anonymized corpus: **234 constructs, ~70% auto-translatable, all
category floors met** (see [coverage & corpus](coverage-and-corpus.md)).

## Phase 1 — smallest genuinely-useful emit

The high-precision declarative surface: `FastAPI`/`APIRouter` → `Wreath`/`Router`,
the five method decorators, `request`-param insertion, the
`Query(...)` → `Annotated[..., Query(...)]` split, `BaseModel` → `@dataclass`,
`HTTPException(status_code=<int>)` → exception classes, the CORS instance form,
import rewriting, sister-folder output, idempotency headers, the round-trip
`ast.parse` guard, and golden tests.

## Phase 2 — ORM models

`ormar.Model` / SQLModel → `wreath.orm.Model`, the per-column
type/constraint/nullability mapping, the FK split with a `load=` annotation, and
enums. Ties in the `Jsonb` / `Array` column types. Queries remain annotate-only.

## Phase 3 — dependencies, lifespan, middleware

`Depends` signature edits, `@asynccontextmanager` lifespan → `on_startup` /
`on_shutdown` split, and custom-middleware scaffolding mapped to built-ins.

## Phase 4 — migrations posture & auth/jobs scaffolds

Emit the Alembic command-mapping guidance, and generate *suggested*
`configure_auth` / `app.jobs` scaffolds from detected env vars and patterns,
pointing at the native auth and durable-jobs subsystems. Bespoke bodies are never
auto-translated.

## Phase 5 — assisted query lint (optional, corpus-gated)

A separate, opt-in `wreath port queries --suggest` that proposes
`.objects.` → `select()` rewrites **as review suggestions in the report only**,
never in-place — shipped only if the earlier phases prove the precision bar holds.
