# Porting from FastAPI with `wreath port`

You have a FastAPI application and a deadline. `wreath port` reads it — statically, never importing it — and writes back native wreath source: the declarations rewritten, the logic preserved, and everything it can't safely translate flagged for your eyes rather than silently guessed.

It is a codemod, not a magic wand. Its contract is simple and unwavering: **transpile declarations, copy logic, and never emit subtly-wrong code.**

## Run it

```bash
# Static analysis + a migration report — no files written, zero risk.
wreath port ./app --report-only

# Emit a translated copy into a sister tree (the safe default).
wreath port ./app --output ../app-wreath

# Rewrite in place (refuses on a dirty tree unless --force).
wreath port ./app --in-place
```

`--report-only` is the place to start. It walks the tree, resolves which framework symbol every name refers to, classifies each construct, and prints counts plus a `file:line` list of what will translate cleanly and what needs review.

## What it translates vs. annotates

The emitter rewrites **declarative** surfaces and copies **function bodies byte-for-byte**, inserting a `# TODO(wreath-port: … [rule])` line above anything it won't touch.

Translated automatically:

- `FastAPI()`/`APIRouter()` → `Wreath()`/`Router()`, the five method decorators, and `request: Request` inserted as the first handler parameter.
- `Query(20, ge=1, le=100)` → `Annotated[int, Query(minimum=1, maximum=100)] = 20` (the marker-as-default split, `ge`/`le` → `minimum`/`maximum`).
- `class X(BaseModel)` → `@dataclass` (pydantic v1 and v2); `= []` → `field(default_factory=list)`.
- `class X(ormar.Model)` → `class X(Model, table="…")` with per-column type mapping; a `ForeignKey` splits into a `column(<pk-type>, references=…)` plus a `relationship(…)`, with the FK type **inferred from the referenced model's primary key**.
- `HTTPException(status_code=404, …)` → `raise NotFound(…)`; `add_middleware(CORSMiddleware, …)` → the instance form.
- A model bound from a form (`as_form`) → `Annotated[Model, Form()]`.

Annotated for you (a real wreath target exists, but the rewrite isn't statically safe):

- ORM `.objects.` query chains, custom `BaseHTTPMiddleware`, lifespan context managers, bespoke auth bodies — each pointed at the corresponding built-in (`db.lock`, `app.jobs`, `oidc_provider`, …).

Left untouched and flagged `unsupported` (no wreath equivalent — keep the library): DynamoDB, OR-Tools, GraphQL servers, cloud SDKs.

## Idempotent, re-runnable

Every emitted file carries a provenance header with a hash of its source and of its own output. Re-running skips unchanged sources and **refuses to clobber a file you've hand-edited** (unless `--force`), so a port can be an iterative conversation, not a one-shot leap.

## Coverage is a diagnostic, not a target

The report includes a coverage number — currently around **0.75** of constructs auto-translated on representative apps. Do not chase it toward 1.0. A meaningful fraction of any real app (queries, domain logic, third-party integrations) is *correctly* left for a human; the tool's value is that this remainder is **precisely enumerated up front** instead of discovered at runtime. A lower, honest number beats a higher, hopeful one.

## The report as a checklist

`--report-only` emits both a human summary and `wreath-port-report.json` (`translated` / `needs-review` / `unsupported`, each with `file:line` and a rule id). Pair it with a `grep TODO(wreath-port` over the emitted tree and you have a complete, deduplicated worklist for the parts that are yours to finish.
