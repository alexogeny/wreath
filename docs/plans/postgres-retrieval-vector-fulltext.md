# Prescriptive plan: vector and full-text retrieval in the ORM

Status: **stages 1-4 implemented** (July 2026), and the codec table proved as
additive as intended: `Halfvec`, `Sparsevec`, and `Bit` (with pgvector's
`<~>`/`<%>` over it) all followed stage 2 without reopening the mechanism. Each
needed a branch at the same six dispatch sites in both twins and nothing more.
See `docs/plans/handoff-2026-07-30-zstd-plugin-workflows-halfvec.md` for what
running their live suites found — including three built-in types that could not be
migrated at all, which had nothing to do with vectors and had been broken far
longer.

Three deliberate deviations from the text below, all documented where a reader
will meet them:

- **Stage 4 is a fusion over two declared queries, not an expression in the
  query DSL.** The plan asked for reciprocal-rank fusion "expressed in the DSL";
  that framing does not survive contact with the compiler, because fusion needs
  *ranks* and ranks need window functions or bounded top-k derived tables. Three
  shapes were considered and the third was chosen: `fuse(nearest, matching, k=60)`
  over two already-declared searches, merged by primary key.

  Rejected: **window functions in `ORDER BY`** — fits today's compiler, but
  `row_number() OVER (ORDER BY embedding <=> $1)` computes over every surviving
  row and neither window can be bounded by the `LIMIT` that makes the HNSW index
  worth having, so it would ship a hybrid search whose guide must admit it does
  not use the index stage 2 built. Rejected **for now**: **two bounded top-k
  subqueries joined in `FROM`** — the better long-term shape and genuinely
  index-assisted, but it needs derived tables in `FROM`, which wreath has never
  had (`InSubqueryExpr` only places a subquery in `WHERE`). That is a compiler
  feature with its own blast radius and deserves its own plan.

  **The door is deliberately left open.** `fuse` today runs each search as its
  own statement and merges in Python — two round trips — and nothing about that
  is observable from the public surface: callers declare *which* searches, the
  constant `k`, and how many rows they want, the return type is a list of
  hydrated objects, and no per-search intermediate result is part of the
  contract. When derived tables in `FROM` land, `fuse` compiles to one statement
  and no application changes. The cost and the reasoning are stated plainly in
  `docs/guides/hybrid-search.md` rather than left for someone to discover.

  Three invariants are checked at class-definition time because each is a silent
  failure otherwise: every fused search must be **ordered** (the rank *is* the
  position, so `LIMIT` with no `ORDER BY` fuses noise), **bounded** (a fusion
  merges two shortlists, and for the vector half the `LIMIT` is what the HNSW
  index answers), and **named** (a search written inside the `fuse(...)` call
  belongs to no class, so it is in no `declarations()` listing and the
  transitional-column scanner in `_migrations/scan.py` skips it without a word).
  The third one had two other candidate fixes — widen the scanner, or document
  the gap — and refusing was chosen per ADR 0019: it is a few lines rather than
  surgery on migration scanning, and relaxing it later stays open where
  tightening later would not. A fourth is quieter and cost real thought: a
  resolved fusion
  holds the *same* declaration objects the class body resolved, because the
  registry keys prepared plans on declaration identity and a private copy would
  compile the same SQL twice and hold a second cache entry forever.

- **`CREATE EXTENSION` is never emitted.** The plan asked for it as the first
  statement of the first migration that needs it. It is not, because the
  privilege usually does not belong to the migration runner; the failure is
  instead raised at startup with the extension and schema named, which is the
  outcome the plan actually wanted. See `docs/guides/vector-search.md` and the
  roadmap row.
- **Changing an existing generated column's expression is `MANUAL`.** It is
  detected (the expression is part of the column signature), but rewriting one
  recomputes every row, so it is emitted for a human rather than applied.

**One defect found and closed since.** The live catalog round trips in
`tests/migrations/test_vector.py` — which replaced renderer-versus-synthetic-image
tests that only ever agreed with themselves — caught an `ivfflat` index declared
`index_ops="vector_l2_ops"` being rediscovered as drift on every `detect` and
emitted as `MANUAL` forever, for an index already correct on the server.
`vector_l2_ops` is ivfflat's default operator class, the only default pgvector
defines (every `hnsw` opclass has `opcdefault = f`, which is why hnsw never
showed it), and PostgreSQL does not record that a default was *named*. The fix
is the one the plan's shape already implied: `orm/introspection.py` resolves
`pg_opclass.opcdefault` per `(access method, indexed type)` the same way it
resolves extension OIDs, the migration entry points cache it on the registry, and
`_registry_descriptor` blanks a declared default so both sides say the same
thing. The emitted `CREATE INDEX` then omits the clause, which builds the
identical index. An unknown access method or a database with no such default
contributes nothing and every declared operator class stays explicit.

**The C-versus-pure question is settled: the C codec earns its place, and the
measurement is recorded in `src/wreath/_native/postgres/codec.c`'s header
comment** rather than here, so it sits next to the code it justifies. Summary:
on the workload this plan names — a 50-row x 1536-dimension decode — native beats
the pure twin by 373-491us against a 4-13us A/A noise floor, and encode by ~5.8x,
reproduced over four independent runs with the arms interleaved. An earlier
reading that showed decode as unresolved was taken against a stale `.so` and did
not include the hand-rolled float4 conversion; **rebuild before re-measuring.**

Related material:

- `AGENTS.md`
- `docs/agents/manifest.json` (subsystems `orm`, `postgres`, `migrations`,
  `queries`)
- `docs/reference/roadmap.md` — the "broader migration object coverage" row this
  plan closes
- `docs/decisions/0014-migrations-are-generated-from-the-catalog.md`
- `docs/plans/native-c-orm.md`, `docs/plans/native-postgres-tenancy-migrations.md`
- `~/research/pypi-downloads/wreath-gap-analysis.md`

## Goal

Give the ORM two retrieval capabilities Postgres already has and Wreath cannot
currently express: **vector similarity** (pgvector) and **full-text search**
(`tsvector`), each end to end — column type, binary wire codec, query DSL
operators, index DDL through `detect`/`generate`/`apply`, and introspection so
drift is visible.

## Why this, and why now

The LLM application stack is the largest non-infrastructure install cohort in the
2026-07 PyPI data: `litellm` 633.2M, `openai` 369.0M, `langchain` 330.1M,
`google-genai` 286.3M, `tiktoken` 216.1M, `anthropic` 158.1M, `langgraph` 65.0M.
Every retrieval-augmented application in that cohort needs two things from its
database, and `pgvector` (30.5M) is the most-installed self-hosted answer to the
first — a Postgres extension rather than another service to operate.

The gap is sharper than "not implemented yet". `tsvector` and `to_tsquery` appear
in `src/wreath` in exactly two files — `_port/rules.py` and `_port/analyzer.py` —
which means **Wreath can already recognise full-text search in somebody else's
application while porting it, and has nowhere to put it**. Every ORM Wreath
competes with has both.

Wreath owns the whole column here: the binary protocol (`orm/types.py`,
`_native/postgres/codec.c`), the DDL renderer (`_native/postgres/migration_sql.c`),
the descriptor builder (`migrations.py`), the query DSL (`queries.py`,
`orm/expressions.py`), and catalog introspection (`orm/introspection.py`). No
dialect negotiation with anyone.

## Non-goals

- No embedding *generation*. Wreath stores and searches vectors; producing them
  is the application's business and would mean a runtime dependency on a model
  provider.
- No client for Qdrant, Chroma, Pinecone, or Weaviate. `objects.py` is the
  precedent for pluggable backends and this is deliberately not that: the pitch
  is that you do not need one.
- Not `postgis`. The dynamic-OID mechanism below is built so geometry *could*
  follow, but nothing in this plan implements it.
- No automatic re-embedding on write, no chunking strategy, no ingestion
  pipeline. Those are recipes, not framework surface.

## The hard part, found first

`PgType.__init__` takes `oid: int` and immediately derives
`shape_value = b"v" + str(oid).encode("ascii")`, the type's contribution to the
plan-cache key (`orm/types.py:25-46`). Every OID in the codebase is a
compile-time constant, and `_native/postgres/codec.c` dispatches on them with
`switch (oid)` against `PG_*` macros.

**pgvector's OIDs are not constants.** `vector`, `halfvec`, and `sparsevec` are
extension types whose OIDs are assigned by `CREATE EXTENSION` and differ between
databases — and between a tenant schema and the one next to it if the extension
was installed separately. A `case PG_VECTOR:` cannot be written.

This is the design constraint the plan is built around, and it must be solved
before any of the pleasant parts:

1. **Resolve at startup.** `orm/introspection.py` already reads `pg_catalog` once
   at startup and reports deterministically. Extend it to resolve extension type
   OIDs by name and record them on the registry.
2. **Give the codec a small dynamic table.** `codec.c` grows a registration entry
   point — `register_extension_type(name, oid, kind)` — writing into a fixed-size
   table checked *before* the `switch`, keyed by OID. Bounded, allocated once,
   read-only after startup. This is the reusable mechanism; `hstore`, `citext`,
   and geometry would all use it later.
3. **Keep the plan-cache key stable.** `shape_value` must not embed a
   database-assigned OID, or the same query against two databases produces two
   cache entries and, worse, a key that changes when an extension is reinstalled.
   Extension types take a **name-derived** shape token (`b"x" + name`), which is
   stable and still distinguishes `vector` from `halfvec`. Add a test that pins
   this, because the failure mode — a silently duplicated plan cache — is
   invisible until someone profiles it.
4. **Fail clearly when the extension is absent.** A `Vector` column on a database
   without `CREATE EXTENSION vector` must raise at startup with a message naming
   the extension and the schema, not at first query with an OID error. `doctor.py`
   is where this check belongs, alongside the other readiness checks.

## The C/Python split, decided

**C, with a pure twin — the binary codec only.**

`_native/postgres/codec.c` and `decode.c` grow encode/decode for the pgvector
binary formats. This is the one place the work is genuinely structural rather
than constant-factor, and it is the same argument `_native/sse.c` records in its
own header comment:

- `vector` wire format is `uint16 dim`, `uint16 unused`, then `dim` big-endian
  float4s. A 1536-dimension embedding is 6,148 bytes.
- The pure-Python encoder is a `struct.pack` over 1536 floats, per parameter, per
  statement; the decoder is 1536 `struct.unpack` results into a list, **per row**.
  A 50-row similarity result decodes 76,800 floats. That is O(rows × dim)
  allocations through the interpreter, not a constant factor.
- The C path walks the buffer once into a preallocated list.

Write the pure twin in `_pure/postgres.py` and a parity test, as every other
native module here does. `WREATH_PURE=1` must produce identical values, and the
parity test is what makes that a contract rather than a hope.

**Python — everything else.**

- `Vector(dim)`, `Halfvec(dim)`, `Sparsevec(dim)` as `PgType`s in
  `orm/types.py`, with dimension validation on `coerce`.
- The distance operators and the search DSL in `orm/expressions.py` and
  `queries.py`. These build SQL strings at declaration time; `queries.py`'s whole
  design is that a named query compiles when the class is defined, not per
  request.
- `tsvector` columns, `to_tsquery`/`websearch_to_tsquery`/`ts_rank`, and the
  configuration (`english`, `simple`) as a declared argument.
- Descriptor construction in `migrations.py`.

**C, small — the DDL renderer.**

Index DDL is rendered natively: `migration_sql.c:580` emits
`create index`/`create unique index`, fed by descriptor records built in
`migrations.py:767`, which already folds a non-btree method into the descriptor
name (`index:<base>:<method>`). Two consequences, both good:

- The **channel already exists**. `orm/fields.py:200` defines
  `_INDEX_METHODS = frozenset({"btree", "gin"})` — GIN is already a supported
  per-column index method, so full-text's index half is mostly plumbing.
- The **genuinely new part is operator classes**. `USING hnsw (embedding
  vector_cosine_ops)` has no expression in the current descriptor, and neither do
  index-method options (`WITH (m = 16, ef_construction = 64)`). Those extend the
  descriptor signature and the renderer.

Do not move index rendering to Python to avoid touching C. It lives in C because
migration rendering is native end to end, and splitting it would be worse than
the change it avoids.

## Public model

```python
from wreath.orm import Model, field
from wreath.orm.types import Text, Vector

class Document(Model):
    body: str = field(Text)
    embedding: list[float] = field(
        Vector(1536), index="hnsw", index_ops="vector_cosine_ops"
    )
    search: str = field(TsVector(config="english", sources=("title", "body")), index="gin")
```

Query side, through the existing named-query machinery:

```python
from wreath.queries import Queries, Param, query

class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(Param("k"))
    )
    matching = query(Document.search.matches(Param("terms"))).order_by(
        Document.search.rank(Param("terms")).desc()
    )
```

Operators to expose, named for what they do rather than for the symbol:

| Method | SQL | Notes |
|---|---|---|
| `.l2_distance(other)` | `<->` | orderable |
| `.cosine_distance(other)` | `<=>` | orderable; the common default |
| `.inner_product(other)` | `<#>` | negative inner product, per pgvector |
| `.l1_distance(other)` | `<+>` | |
| `.matches(query)` | `@@ websearch_to_tsquery(...)` | predicate |
| `.rank(query)` | `ts_rank(...)` | orderable |

`websearch_to_tsquery` is the default parser because it is the one that does not
raise on user input; `to_tsquery` is available explicitly for callers who want
operator syntax and will handle the syntax errors.

## Migrations

`detect`/`generate`/`apply`/`down` must cover:

- A vector column added, dropped, or **re-dimensioned** — the last is a rewrite
  and must be emitted as such, not as a silent no-op.
- HNSW and IVFFlat indexes with operator class and method options. Both are
  expensive to build; emit them so an operator can see the cost before applying,
  and document `CONCURRENTLY` as a manual escape hatch rather than doing it
  implicitly inside a transaction where it cannot work.
- GIN indexes on `tsvector` columns, including generated columns
  (`GENERATED ALWAYS AS (to_tsvector(...)) STORED`), which is the form that keeps
  the index correct without a trigger.
- `CREATE EXTENSION IF NOT EXISTS vector` emitted as the first statement of the
  first migration that needs it, with the privilege caveat documented.

This closes the roadmap row that currently reads "Expression/covering/non-btree
indexes, partial predicates outside that vocabulary, index-method options ... are
still being implemented (emitted as `MANUAL`)" for the non-btree and
index-method-options halves. Update `docs/reference/roadmap.md` in the same
change — that page is the single place the answer lives.

## Measurement

Per AGENTS.md, benchmark before and after, never from one run, and do not use
cProfile to decide:

- Ablate the codec: time a 50×1536 similarity result with the C codec, then with
  `WREATH_PURE=1`. That difference is the C decision's whole justification and it
  should be recorded in the change that lands it.
- `uv run wreath-decomp` for the ORM read stage, against its reported A/A noise
  floor. Below the floor means unresolved, not zero.
- `uv run wreath-request-trace --check`: a vector query is an ORM read on an
  already-activated route, so `pre_activation` must not move. If it does,
  something resolved a type at request time that should have resolved at startup.

## Tests

- `tests/test_orm_vector_codec.py` — round-trip every dimension boundary (0, 1,
  2, 1536, the pgvector maximum), NaN and infinity rejection, dimension mismatch,
  and C/pure parity. Parity is the contract; without it the two implementations
  drift the way any untested pair does.
- `tests/test_orm_vector_queries.py` — each operator compiles to the expected
  SQL; `order_by` on a distance uses the index (assert on the plan, against a
  real PostgreSQL).
- `tests/test_orm_fulltext.py` — `websearch_to_tsquery` survives hostile input
  (`"` , `&`, `!`, a lone `:`), generated column stays correct after an update,
  `ts_rank` orders as expected.
- `tests/test_migrations_vector.py` — index DDL with opclass and options,
  re-dimension emits a rewrite, extension statement ordering, `down` reverses.
- `tests/test_orm_extension_oid.py` — the plan-cache shape token does not change
  when the resolved OID does; an absent extension fails at startup with the
  extension named.

**These need a real PostgreSQL with pgvector installed.** The suites gated on
`WREATH_TEST_POSTGRES_DSN` are the ones that found a defect in a default code
path once already. Extend the container line in `AGENTS.md` to an image that has
the extension (`pgvector/pgvector:pg17`) and make `tests/conftest.py`'s skip
banner name the extension when it is missing, so this cannot go a long time
without running.

## Staging

**Stage 1 — the mechanism.** Dynamic extension-OID resolution, the codec table,
the stable shape token, `doctor.py` readiness, and the startup failure message.
No user-facing type yet. This is the stage that is easy to skip and expensive to
retrofit.

**Stage 2 — vectors.** `Vector`, the C codec and its pure twin, the four distance
operators, HNSW/IVFFlat DDL with opclass and options, migrations, guide,
reference page, recipe.

**Stage 3 — full text.** `TsVector`, generated columns, `matches`/`rank`, GIN
DDL, guide and recipe. Cheaper than stage 2 because it reuses stage 2's
index-DDL work and needs no new codec.

**Stage 4 — hybrid search.** Reciprocal-rank fusion over a vector rank and a text
rank, expressed in the DSL rather than pasted into every application. Worth doing
only after 2 and 3 are real, and worth doing because it is the shape people
actually ship.

`Halfvec`/`Sparsevec` follow stage 2 if anyone asks; the codec table makes them
additive.

## Risks

- **The extension may not be installable.** Some managed Postgres tiers restrict
  `CREATE EXTENSION`. Handled by failing at startup with a clear message and
  documenting it, not by falling back to a slower path that hides the problem.
- **HNSW build time surprises people.** Document it in the migrations guide with
  a number, and mention `CONCURRENTLY` explicitly.
- **Dimension drift between the model and the stored data.** Coercion catches it
  on write; introspection must catch it on startup, which is why re-dimension is
  a detected change rather than a silent one.
