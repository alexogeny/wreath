# Handoff — 2026-07-30

Written mid-session because the human had to leave, and **appended to on
2026-07-30 by a later session**. The original text below described work that was
uncommitted at the time; all of it has since been committed (`4fe7fae`
through `a6b6ec4`). Read "## Session two" near the bottom for what is true now.

## The one open problem — resolved, and not by the hypothesis below

`uv run pytest -n 6` failed 9 tests in `tests/orm/test_vector_queries.py`, green
serially. It was **flaky, not deterministic**: a later session saw two runs green
and two red with a *different* subset of those 9 each time, which is the tell
that it depends on which xdist worker drew which file.

**The actual cause was not the module-scoped bind in `test_vector_codec.py`.** It
was `_vector_oid()`'s early return. That helper returned the first declared
`vector` type that had an OID and, having found one, never called
`bind_extension_oid` — so `Document.embedding` stayed on OID 0 and
`to_wire` raised `ExtensionNotInstalledError`.

What put a bound-but-unrelated type in front of it was the "already fixed, same
family" change recorded in the original text:
`test_require_oid_returns_the_oid_once_the_type_is_resolved` builds a throwaway
`Vector(4)` and assigns `column.oid = 987001` directly. `ExtensionType.__init__`
appends **every** instance to the process-global `_DECLARED_EXTENSION_TYPES`,
permanently, so that throwaway is a `vector` entry carrying a foreign OID for the
rest of the process. That fix cured its own symptom and moved the failure next
door.

**The fix**: bind unconditionally rather than returning early. `bind_extension_oid`
is idempotent for a repeated identical OID and walks *all* declared types, so
binding again is the cheap way to reach declarations that did not exist when
someone else bound. Applied to both copies of the helper
(`test_vector_queries.py` and `test_extension_oid.py`).

**The general lesson, worth carrying to the next extension type:** finding *a*
bound instance does not mean *this module's* instance is bound. Any test that
assigns `.oid` on a locally-constructed extension type leaves a permanent global
entry that can satisfy someone else's early return.

`tests/orm -q -n 6` is now green across repeated runs.

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

- ~~**`sparsevec`**~~ and ~~**`bit` + hamming/jaccard**~~ — both shipped in
  session two; see below.
- **The broad psql mutant sweep the human asked for.** Measured cost:
  ~0.66 s/mutant against `tests/orm`. Extrapolating ~16k lines of psql code at
  roughly a mutant per six lines is ~2,600 mutants, and against the full DB test
  set each pytest invocation is several times slower — **on the order of 1–2 hours
  of continuous CPU.** Deferred deliberately: the human is on battery. `orm/types.py`
  is done (0.96). Next highest value is probably `orm/compiler.py` (1,576 lines),
  `orm/session.py` (1,594) and `migrations.py`.

## Deferred test-infrastructure warning

The deliberate crash tests in `tests/test_flight_reproduce.py` and
`tests/test_flight_ring_file.py` call `os.fork()`. Under the six-worker suite,
CPython 3.14 warns because the xdist worker is multi-threaded and a fork from a
multi-threaded process may deadlock. Four warnings remain after the ordinary
pytest warning cleanup. Do not silence them: later, move child creation behind
a spawn- or forkserver-safe helper while preserving the properties these tests
actually assert -- shared recorder files, termination by signal, and a parent
that can inspect the child's wait status.

## Session two — `sparsevec`, `bit`, hamming and jaccard

Uncommitted in the working tree, per AGENTS.md. A second agent was working in the
same tree throughout; nothing below touches its files except one deliberate
overlap noted at the end.

### `Sparsevec(dim)` — `EXT_KIND_SPARSEVEC = 3`

The distinct Python value type the original text predicted is
`src/wreath/_sparsevec.py::SparseVector`: a dimension plus its non-zero elements.
It lives in its own module holding one class and **no wreath imports**, because
both codec twins need it — `_pure/postgres.py` imports it directly and
`codec.c` resolves it in module init beside `uuid.UUID` and `decimal.Decimal`.
Anything reaching back into the package from there would be an import cycle.
Re-exported from `wreath.postgres` and `wreath.orm.types`.

**Indices are 1-based in Python and 0-based on the binary wire.** pgvector's text
form (`{1:1.5,3:3.5}/5`) and its documentation count from one, so that is what
`SparseVector` exposes; the conversion lives in `_encode_sparsevec` /
`_decode_sparsevec` and their C twins and nowhere else. This is the type's one
real trap, and `test_sparsevec_codec.py` asserts it against literal bytes rather
than only through a round trip — a round trip is exactly what stays green when
both directions are wrong.

Other decisions worth not re-litigating:

- Explicit zeros are dropped at construction. The server drops them too, so
  keeping them would mean a value did not survive its own round trip.
- Validation stays in the Python class and is **not** restated in C. Two copies
  of a bounds check are two chances to disagree about what pgvector accepts, and
  the sparse paths are cold. `decode_sparsevec` builds a dict and calls the class.
- `_pg_oid` rides on a *copy* made by `to_wire`, not on the caller's object,
  which may outlive the bind and be bound against another database. Same problem
  `WireList` solves for `vector`, different shape.
- `_infer_oid` gained a final `getattr(value, "_pg_oid", 0)` branch, after every
  built-in shape has missed, for extension values that are not sequences at all.

### `Bit(length)` — a built-in, not an extension type

`bit` is PostgreSQL's own type, OID 1560, a compile-time constant with no
resolution step and no `ExtensionNotInstalledError` path. Only pgvector's
operators over it come from `CREATE EXTENSION vector`, which is why
`_bit_distance` gates on the OID rather than on
`isinstance(pg_type, ExtensionType)`.

The value is a `str` of `'0'`/`'1'`. `bytes` is accepted at *coercion* only —
the column knows the length, the codec does not — with non-zero padding in the
final byte refused rather than masked, since masking would make two wire values
decode to one string.

**The wire packs MSB-first, final byte padded on the right.** Reversed, every
value still round-trips through our own codec, still stores, still indexes, and
still returns *an* ordering — just the wrong one, on a column whose whole job is
approximate ranking, where a real bug looks like "slightly worse recall".
`test_bit_codec.py` asserts the packing against literal bytes for that reason.

### `hamming_distance` (`<~>`) and `jaccard_distance` (`<%>`)

In `DISTANCE_OPERATORS` and the compiler allowlist, and type-gated both ways: a
`bit` column refuses the four dense operators and a `vector`/`halfvec`/`sparsevec`
column refuses the two bit ones. `_distance`'s message now names all three dense
types, which required updating
`test_vector_queries.py::test_a_distance_requires_a_vector_column`.

### Six dispatch sites, again

Exactly as the halfvec note warned, plus `wreath_pg_decode_extension` and the
kind guard in `codec_register_extension_type`. Both twins verified byte-identical
across binary and text, in both directions, for every new type.

### What was run, and what was not

Clean: `ruff`, `ty`, `wreath-native-lint` (0 — one NC005 was found and fixed, a
`PyObject_CallMethod` inside the text-decode loop, replaced with
`PyUnicode_FindChar` plus two slices), `wreath-map-lint`, `wreath-roadmap-lint`,
`wreath-request-trace --check` (no added crossings), `wreath-docs` (177 pages).
Tests: `tests/orm tests/postgres tests/test_docs_ssg.py` → 1209 passed;
`tests/rgb tests/migrations tests/test_cross_subsystem.py` → 612 passed.
`tests/orm -n 6` green across three consecutive runs.

**`uv run wreath-check` was not run** — the human was on battery and asked for it
to be skipped.

**The 15 live tests have never executed.** `tests/orm/test_sparsevec_live.py` (7)
and `tests/orm/test_bit_live.py` (8) collect and skip cleanly, but no container
was started. They are the only thing that proves our framing matches the
*server* rather than merely matching the other twin, so they are the first thing
to run when `wreath-test-pg` is next up. Three facts asserted in them and in the
guide come from reading pgvector's source rather than from a running server, and
should be treated as unconfirmed until those tests pass: `SPARSEVEC_MAX_NNZ` is
16,000, `SPARSEVEC_MAX_DIM` is 1e9, and HNSW indexes a `sparsevec` to 1,000
non-zero elements.

### One overlap with the concurrent agent

That agent added two rows to `docs/reference/roadmap.md` — "Sparse vectors — Not
shipped" and "Binary-quantized vector search — Not shipped", each with an
`<!-- absent: -->` marker — at the same time these features were being
implemented. The rows became false, so they were removed and
`wreath-roadmap-lint` is clean. That is the only place this session touched the
other agent's work, and it is worth a glance from whoever reconciles the two.

### Still open here

- `sparsevec` has no `wreath.migrations` round-trip test of its own. The
  extension-typed-column path is generic and `sparsevec(dim)` renders like
  `vector(dim)`, so it is expected to work; nothing has demonstrated it.
- No mutant run over the new code. `orm/types.py` was at 0.96 before these two
  types were added to it.

## Environment notes

- Test PostgreSQL: `podman start wreath-test-pg` (image
  `docker.io/pgvector/pgvector:pg17`; there is no docker daemon on this machine,
  podman only, and podman needs the fully-qualified image name).
  `export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"`
  It has been **stopped** to save battery.
- The PyPI research this all came from lives in `~/research/pypi-downloads/`
  (`query.py --groups <name>`, snapshot 2026-07-01).
