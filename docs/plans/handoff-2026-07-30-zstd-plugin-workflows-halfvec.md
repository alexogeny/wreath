# Handoff — 2026-07-30

Written mid-session because the human had to leave. Everything below is in the
working tree, **uncommitted** (per AGENTS.md). One gate is red and is the first
thing to pick up.

## The one open problem

`uv run pytest -n 6` (the full suite, as `wreath-check` runs it) fails 9 tests in
`tests/orm/test_vector_queries.py`:

```
test_each_operator_compiles_to_its_symbol[l2_distance-<->]      (and the 3 siblings)
test_a_distance_orders_descending_too
test_the_order_by_value_binds_between_the_where_and_the_limit
test_a_threshold_comparison_is_a_predicate
test_ordering_by_a_distance_and_a_column_together
test_a_declared_query_binds_the_vector_per_call
```

**They pass when `tests/orm` is run serially.** `uv run pytest tests/orm -q` is
all dots. So this is a parallel/ordering interaction, not a logic error in the
vector query compiler.

**Not yet established: whether this is pre-existing or something I introduced.**
That is the first question to answer, and AGENTS.md's rule applies — "pre-existing
is a diagnosis, not a disposition". The next command to run (it was interrupted
before it finished) is:

```bash
uv run pytest tests/orm/test_vector_queries.py -q -n 6      # same file alone, parallel
uv run pytest tests/orm/test_vector_queries.py::test_a_threshold_comparison_is_a_predicate -q
```

**Leading hypothesis.** Extension OID binding is *process-global*
(`bind_extension_oid` writes a codec table keyed by type name, and
`_DECLARED_EXTENSION_TYPES` is a module-level list). `tests/orm/test_vector_codec.py`
binds `vector` to a made-up OID (987654) in a module-scoped autouse fixture, and
`test_vector_queries.py` may be relying on that having happened. Adding two new
files to `tests/orm` changes how xdist distributes tests across workers, so
`test_vector_queries.py` can now land on a worker where nothing bound `vector`.

If that is it, the fix belongs in `test_vector_queries.py` (bind what it needs
itself, rather than inheriting another module's global side effect) — not in the
new files. Worth checking whether `tests/orm/conftest.py` should own the binding.

**Already fixed, same family, for reference:** my
`test_require_oid_returns_the_oid_once_the_type_is_resolved` originally called
`bind_extension_oid("vector", 987001)` and failed *only* in a full-directory run,
because `test_vector_codec.py` had already bound a different number. It now
assigns `column.oid` on a local instance and touches no global state. That is the
shape of fix the 9 failures probably want.

## What shipped this session (all green, all gates clean except the above)

`ruff`, `ty`, `wreath-native-lint`, `wreath-map-lint`, `wreath-docs` (177 pages),
`wreath-complexity-probe`, and `wreath-request-trace` (no added crossings) all
pass. Only `pytest` is red, and only under `-n 6`.

### 1. zstd content-encoding

- `src/wreath/_pure/compression.py` — `ZstdCompressor`, `zstd_compress`,
  `ZSTD_MIN_LEVEL`/`ZSTD_MAX_LEVEL`/`ZSTD_DEFAULT_LEVEL` read from libzstd.
- `src/wreath/compression.py` — facade, rewritten docstring.
- `src/wreath/_native/webpolicy.c` + `src/wreath/_pure/webpolicy.py` —
  `select_content_encoding` now returns `"zstd" | "gzip" | None`. **zstd is offered
  only to a client that named it; a bare `*` still means gzip.** Ties go to zstd.
  The two twins are held equal by `tests/test_webpolicy_parity.py` (22 cases).
- `src/wreath/middleware/compression.py` — per-coding compressor and ETag suffix
  (`--gzip` / `--zstd`), new `zstd_level` argument.
- brotli is **deliberately not** offered: it needs a PyPI package.
- Docs: `docs/guides/compression.md`, `docs/reference/compression.md`. The three
  new int constants are registered in `test_docs_ssg.py::_UNRENDERED_CONSTANTS`.

### 2. `pytest11` plugin

- `src/wreath/_pytest_plugin.py`, registered via
  `[project.entry-points.pytest11]` in `pyproject.toml`.
- Fixtures, all `wreath_`-prefixed (the plugin loads in *every* repo that installs
  wreath, so a bare `client`/`db` would shadow a user's own): `wreath_app` (meant
  to be overridden; its default raises with the lines to write), `wreath_client`,
  `wreath_email`, `wreath_postgres_dsn`, `wreath_database`, `wreath_db`
  (transaction rolled back in a `finally`).
- `tests/test_pytest_plugin.py` — 9 tests through `pytester`, two against a live
  database.
- Docs: a section in `docs/guides/testing.md`; manifest `testing` subsystem
  updated.

### 3. `wreath.workflows` — durable sagas

- `src/wreath/workflows.py`: `Workflow`, `Step`, `StepContext`, `Outcome`,
  `InMemoryWorkflowStore`, `PostgresWorkflowStore`, `WorkflowError`,
  `WorkflowDefinitionChanged`, `UnknownWorkflowInstance`.
- Properties: a completed step never re-runs; compensation newest-first; the step
  that *raised* is not compensated; a failing compensation is counted (durably,
  read with `Workflow.status`) and does not stop the ones behind it; a renamed step
  on a live instance raises rather than silently redoing work; `key=` is
  exactly-once *start*.
- `tests/test_workflows_checklist.py` — 15 tests, including a live
  crash-and-resume against real PostgreSQL.
- Docs: `docs/guides/workflows.md`, `docs/reference/workflows.md`, nav entries in
  `wreath_docs.py`, `docs/llms.txt` row, new `workflows` manifest subsystem.

### 4. pgvector: `Halfvec`

- `EXT_KIND_HALFVEC = 2` in `orm/types.py`; `Halfvec(dim)`,
  `MAX_HALF_MAGNITUDE`, `MAX_HALFVEC_DIM`.
- Codec in **both** twins: `_pure/postgres.py` (`struct` `e` format) and
  `_native/postgres/codec.c` (`PyFloat_Pack2`/`Unpack2`).
- **Six dispatch sites needed a branch**, and missing two of them (the two
  `_decode_value` entry points at what are now codec.c:~1799 and ~1897) silently
  returned raw bytes. Worth remembering when `sparsevec` is added.
- `Halfvec`'s `extension` is `"vector"`, not `"halfvec"` — one
  `CREATE EXTENSION vector` provides both, and naming the type would tell readers
  to install something that does not exist.
- `tests/orm/test_halfvec_codec.py` (42 tests, twin parity) and
  `tests/orm/test_halfvec_live.py` (6 tests against real pgvector, including that
  a halfvec HNSW index is measurably smaller than the `vector` equivalent).

### 5. AGENTS.md: no xfail

New rule under "Engineering rules": never `xfail`/`skip` to park a test for
something unbuilt — implement the surface, or write the contract as prose in
`docs/reference/roadmap.md`. The narrow exception is a missing *environment*
capability (no DSN, no pgvector, no free-threaded build). I had started an
`xfail`-marked workflow checklist; it was removed and the module implemented
instead.

## Mutation testing results

`wreath mutant` found four real defects, all fixed:

1. A dead `isinstance(..., (bytes, str))` branch in `PostgresWorkflowStore.load` —
   jsonb always decodes to `str`; I had hedged instead of checking.
2. `if not rows` in that same method was unasserted — `status`/`resume` on an
   unknown key had no test.
3. `middleware/compression.py`'s ETag header scan would have rewritten
   `content-type` instead of the ETag.
4. `orm/types.py` had **29 survivors and 20 unreached**, every one a
   declaration-time *validation* refusal with no test at all. Added
   `tests/orm/test_type_declaration_refusals.py` (62 tests) covering `Timestamp`,
   `TimestampTz`, `Numeric`, `Array`, `Vector`, `TsVector`, `bind_extension_oid`
   and `require_oid`. **Score went 0.72 → 0.96, unreached 20 → 0.**

Also: my DB tests were originally marked `network`, which the default marker
expression *excludes* — the exact mistake `pyproject.toml`'s comment warns about.
They are `database` now and run by default whenever a DSN exists.

### Remaining `orm/types.py` survivors (5) — all provably equivalent, left alone

- `Array` :513/:516 — the `None if item is None else element.to_wire(item)`
  conditionals are redundant with `PgType.to_wire`'s own `None` guard, so no
  behaviour distinguishes the arms. Documented in
  `test_a_none_element_stays_null_through_both_wire_directions`.
- `Vector` :554, `Halfvec` :634 — the `isinstance(dim, bool)` clause is dead:
  `dim.__class__ is not int` is already true for a `bool`.
- `TsVector` :752 — `isinstance(sources, str)` is dead for the same reason
  `not isinstance(sources, (list, tuple))` already catches a `str`.

Removing those clauses would be a legitimate simplification; they were kept as
documentary and are worth a decision rather than a drive-by edit.

## Still not done

- **`sparsevec`.** Documented in `docs/guides/vector-search.md` as unimplemented.
  Additive, but it needs a distinct Python value type (a dimension plus an
  index→value mapping, not a `list[float]`), so it is more than a codec kind.
- **`bit` + hamming/jaccard** (`<~>`, `<%>`) for binary quantization — zero
  presence in the tree.
- **The broad psql mutant sweep the human asked for.** Measured cost:
  ~0.66 s/mutant against `tests/orm`. Extrapolating ~16k lines of psql code at
  roughly a mutant per six lines is ~2,600 mutants, and against the full DB test
  set each pytest invocation is several times slower — **on the order of 1–2 hours
  of continuous CPU.** Deferred deliberately: the human is on battery. `orm/types.py`
  is done (0.96). Next highest value is probably `orm/compiler.py` (1,576 lines),
  `orm/session.py` (1,594) and `migrations.py`.

## Environment notes

- Test PostgreSQL: `podman start wreath-test-pg` (image
  `docker.io/pgvector/pgvector:pg17`; there is no docker daemon on this machine,
  podman only, and podman needs the fully-qualified image name).
  `export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"`
  It has been **stopped** to save battery.
- The PyPI research this all came from lives in `~/research/pypi-downloads/`
  (`query.py --groups <name>`, snapshot 2026-07-01).
