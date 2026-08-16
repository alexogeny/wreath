# Coverage & the test corpus

## The corpus

The regression corpus lives under `tests/port/corpus/` as a **glob of app
roots** — each directory is a self-contained FastAPI app used as *input text*
(never executed or imported by the test run). It is **derived from real
production apps but fully anonymized** into a fictional domain (a llama-trekking
& alpaca-boarding company) so it carries no reference to its origin.

Three app roots give stack diversity:

- `tumbleweed_api/` — the primary, **ormar**-based app: many routers, the full
  param surface, Pydantic v2 DTOs (incl. validators and `get_pydantic`
  metaprogramming), ormar models (FK, enum, JSONB, array, nullable,
  `server_default`, UUID pk), a lifespan with a supervised asyncio loop, custom
  middleware, a dynamic `include_router` loop, GraphQL, a message broker, object
  storage, an advisory lock, an OIDC/JWKS + M2M auth surface, and one Alembic
  script.
- `roost_api/` — a smaller, different stack: **SQLModel/SQLAlchemy** models,
  **Celery** tasks, **authlib/python-jose** auth, nested `BaseSettings`, SMS
  (Twilio) and feature-flag (Unleash) integrations, and a `create_app()` factory
  with per-env docs gating.
- `driftwood_gateway/` — an **integration/gateway** service (derived from a real
  integration app): a class-based `Depends(AuthorizeWithActions([...]))`
  dependency, an **authlib `OAuth2Session`** client-credentials (M2M) client,
  **PyJWT** verification, a legacy **pydantic v1 `BaseSettings`**, the nested
  `class Meta(OrmarMeta)` + `UniqueColumns` ormar spelling, module-level
  `HTTPException` constants, an outbound `httpx` provider adapter, and an inbound
  HMAC webhook-signature verification.

Adding more app roots later widens the corpus with **zero code change** — the
coverage harness globs the directory. New construct usage surfaces as new
`unsupported` categories in the report, which is exactly the signal for the next
rule to build.

## The coverage metric

**Auto-translation coverage = translated / recognized constructs**, reported
per-category and overall, and tracked in CI so rule improvements are visible and
regressions are caught.

## The honest number

Weighting real construct counts by translatability:

- The **declarative surface** is large and high-precision — routes, plain
  Pydantic models, `Depends` signatures, per-column ORM mapping, imports, CORS,
  literal-status `HTTPException`, settings scaffolding — plausibly **~70–80% of
  declaration sites** auto-translate with high confidence.
- The **long tail is imperative and dominates line count** — ORM `.objects.`
  queries, bespoke multi-scheme auth, dynamic router registration, GraphQL /
  message brokers / SMS / feature flags, and metaprogramming. Counting those,
  realistic **end-to-end coverage lands ~45–60% of constructs**, and materially
  less by "lines that need no human touch."

The value is **not** the percentage. It is that the untranslatable 40–55% is
**precisely enumerated up front** in the report, instead of being discovered at
runtime after a hand-port.

## Original Phase 0 baseline

The first report-only analyzer measured the following over the three app roots:

- **234 recognized constructs** — 164 translated · 44 needs-review · 26 unsupported.
- **Overall auto-translatable ≈ 70%** (within the honest 0.40–0.80 band the suite
  asserts; the corpus is declaration-dense, so it sits at the top of the range).
- Per-category coverage all clears its floor: routing 0.97, params 1.00,
  pydantic_models 0.92, dependencies 1.00, orm_models 0.88, exceptions 1.00,
  settings 0.72, queries 0.00 (the `.objects.` tar-pit is annotate-only by design —
  all 18 query findings are `unsupported`, never silently "translated").

These numbers are asserted by `tests/port/test_corpus_coverage.py` and
`test_report_contract.py`, which activate automatically once `wreath.port` is
importable from a built wreath.
