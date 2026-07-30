# Handoff — 2026-07-30

Written mid-session because the human had to leave, and **appended to three times on
2026-07-30 by later sessions**. The original text below described work that was
uncommitted at the time; all of it has since been committed (`4fe7fae`
through `a6b6ec4`), as has session two's (`88a29d8`, `2c58180`, `f9b44f4`).

**Start at the bottom.** "## Session four" is the one to read before running
`wreath mutant` — it records three ways the tool silently under-reports, a defect in
the tool itself, and the pattern every high-value finding shared. "## Session three"
above it closed session two's open items; the live tests it finally ran found four
defects. Sessions three and four are both uncommitted.

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

Both closed in session three; see below.

- ~~`sparsevec` has no `wreath.migrations` round-trip test of its own.~~ The
  prediction was right about the column and wrong about the neighbourhood: see
  "Three types could not be migrated at all" below.
- ~~No mutant run over the new code.~~ Now 0.98 with 0 unreached.

## Session three — running the live tests, and what they found

The container was started and the 15 never-executed live tests were run. **Four
defects, three of them in shipped code rather than in the tests.** Nothing here is
committed; the tree is dirty per AGENTS.md.

### The two size assertions measured the wrong thing

`test_a_quantized_signature_is_thirty_two_times_smaller_on_disk` and
`test_a_sparse_column_costs_what_it_stores_not_what_it_declares` both used
`pg_relation_size`, **which excludes TOAST**. A `vector(512)` is 2,056 bytes, past
the threshold at which PostgreSQL moves a value out of line, so the dense table's
main fork holds pointers and measures 16KB while its TOAST relation holds 800KB of
floats. The `bit(512)` and the four-element `sparsevec` stay inline, so by that
function the *quantized* table looked two times larger than the dense one it
shrinks by fourteen. `pg_table_size` counts TOAST and excludes the identical
primary-key index on both sides, which is what these comparisons want.

Worth noting the docstrings already said "toast and page overhead keep this from
being exactly 32x" — the author knew TOAST was involved and still picked the
function that hides it.

### The jaccard test's arithmetic was wrong

`test_jaccard_distance_measures_overlap_rather_than_agreement` asserted that
`11110000` and `00001111` are four bits apart. They differ in **all eight**
positions. The pair was chosen to show Hamming ranking two pairs the same way
Jaccard ranks them differently, and it does not: Hamming ranks that pair correctly.
Replaced with a pair that genuinely inverts — `11110000`/`11111111` is Hamming 4,
Jaccard 0.5, while `10000000`/`01000000` is Hamming 2, Jaccard 1.0, so Hamming
calls the second closer and Jaccard the first. Both operators' values are pinned
against the server, and the inversion is asserted as a comparison rather than left
for a reader to perform on four constants.

### Three types could not be migrated at all

Adding the missing `sparsevec` migration round trip turned up that `halfvec` and
`bit` had none either, and the `bit` one **failed**: `generate` emitted an empty
`MANUAL` statement in place of `add column "signature" bit(8)` and then emitted
`create index ... using hnsw ("signature" bit_hamming_ops)` on a column that was
never added. Applying that plan fails.

`render_column_type` in `migration_sql.c` maps a built-in OID to its spelling
through a switch, and **a type missing from that switch renders as an empty MANUAL
rather than failing loudly.** Three were missing:

- `json` (114) and `character varying` (1043) — both of whose *array* forms (199,
  1015) were present from the start. `Varchar` is not an obscure corner.
- `bit` (1560), which is a different problem: `bit(8)` and `bit(512)` share an OID,
  so the switch cannot serve it at all. It now spells itself in the descriptor the
  way an extension type does, which means the set is written twice —
  `migrations._MODIFIER_BEARING_OIDS` and a hard-coded `<> 1560` in
  `_SINGLE_CATALOG_SQL`, because a `CASE` cannot read a frozenset. **Both sides
  must produce the identical string or the column is rediscovered as drift
  forever**, the same silent failure the default-opclass defect had.

The guide's `Bit(1536)` example at `docs/guides/vector-search.md:228` was therefore
false when written. It is true now.

`tests/migrations/test_object_coverage.py` now **enumerates every built-in `PgType`
in `__all__`** and asserts each renders without a MANUAL, plus a guard that the
enumeration is non-empty — a parametrised suite over an empty list passes while
testing nothing. `test_catalog_integration.py` round-trips all eighteen against a
real catalog. Point tests for the three would not have stopped the fourth.

### A pre-existing suite-order defect, unmasked by finally having a DSN

`tests/orm` with `WREATH_TEST_POSTGRES_DSN` set had **never been run to
completion**, and it did not pass: every test in `test_vector_queries.py` errored
at setup with `'vector' is already bound to OID 987001 ... this database assigns it
16960`. Reproduced on pristine `HEAD` in a scratch worktree before changing
anything, so it is not session two's.

This is the *same* trap the "general lesson" note above describes, twice more:
`test_type_declaration_refusals.py` assigns `column.oid = 987001` on a throwaway
`Vector(4)`, and `test_sparsevec_codec.py` assigns a fake OID on two throwaway
`Sparsevec(5)`s. `ExtensionType.__init__` appends **every** instance to the
process-global `_DECLARED_EXTENSION_TYPES`, permanently, so each throwaway stays an
entry carrying a foreign OID for the rest of the interpreter and the next
`bind_extension_oid` against a real server refuses. Both now restore `0` in a
`finally` (0 means unresolved, and binding accepts it). `tests/orm` is 839 passed
with a live DSN.

**The lesson is now three for three: never leave an assigned `.oid` on a
locally-constructed extension type.** A `grep -rn '\.oid = ' tests/orm/` audit is
cheap and was clean afterwards.

### The three pgvector facts are confirmed

All three were read from pgvector's source and are now pinned against the server's
own refusal messages, in `test_sparsevec_live.py`, from both sides of each bound:

- `SPARSEVEC_MAX_NNZ` is 16,000 — 16,001 gives "cannot have more than 16000
  non-zero elements".
- `SPARSEVEC_MAX_DIM` is 1e9 — 1e9+1 gives "cannot have more than 1000000000
  dimensions".
- HNSW indexes a `sparsevec` to 1,000 non-zeros — 1,001 gives "cannot have more
  than 1000 non-zero elements for hnsw index". The limit belongs to the access
  method, not the type, so nothing about the column declaration reveals it.

### Mutants: 0.91 → 0.98, 0 unreached

`--path src/wreath/_sparsevec.py --path src/wreath/orm/types.py --tests tests/orm`,
43s. **Run it with the DSN set**: without one the live tests skip and the score
reads 0.91 with 3 unreached, which understates coverage and points at branches that
are in fact tested.

`_sparsevec.py` had no offline refusal tests at all — its bounds were covered only
by the DSN-gated live suite. Added to `test_type_declaration_refusals.py`:
non-int and out-of-range dimension, non-int and out-of-range index, the 1-based
boundary from both ends, the nnz ceiling from both sides, the accepting side of
`Sparsevec`'s dimension check, and both arms of
`elements if isinstance(elements, dict) else dict(elements)` — the documented
"iterable of pairs" half of the signature was never exercised.

**The five kept survivors were settled rather than re-deferred.** The four
provably-dead clauses are gone: `isinstance(dim, bool)` in `Vector`, `Halfvec` and
`Sparsevec`, `isinstance(length, bool)` in `Bit` (all unreachable because
`__class__ is not int` is already true for a `bool`), and `isinstance(sources, str)`
in `TsVector` (a `str` is not a `list` or `tuple`). `_sparsevec.py` already wrote
the check without the bool clause, so this is consistency rather than taste, and a
comment at `Vector` records why `__class__` and not `isinstance` — the next reader's
obvious "fix" is the one thing that breaks it.

`Array`'s two `None if item is None` conditionals were **kept**. They are not dead,
only redundant with `PgType.to_wire`'s own `None` guard; removing them would make
`Array`'s correctness depend silently on a distant invariant. Three survivors
remain, all provably equivalent: those two, and forcing
`dict(elements)` on a value that is already a `dict` (an extra copy, no behaviour).

### Still open

- ~~**The broad psql mutant sweep is still not done**~~ — done in session four; see
  below. `orm/compiler.py`, `orm/session.py` and `migrations.py` are now measured
  and are where the remaining survivors concentrate.
- `migration_sql.c`'s type switch is now covered for everything wreath *declares*.
  Nothing covers a built-in a user might already have in a schema wreath is
  adopting (`bpchar`, `inet`, `interval`), which would render as an empty MANUAL the
  same way. Whether that is a defect or the correct refusal is a decision, not an
  oversight — but an empty MANUAL is a poor way to say either.
- The `os.fork()` warnings recorded under "Deferred test-infrastructure warning"
  are untouched.

## Environment notes

- Test PostgreSQL: `podman start wreath-test-pg` (image
  `docker.io/pgvector/pgvector:pg17`; there is no docker daemon on this machine,
  podman only, and podman needs the fully-qualified image name).
  `export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"`
  It has been **stopped** to save battery.
- The PyPI research this all came from lives in `~/research/pypi-downloads/`
  (`query.py --groups <name>`, snapshot 2026-07-01).

## Session four — the broad mutation sweep, and three ways it lies

The sweep deferred above was run, plus five more covering most of `src/wreath`:
roughly 8,900 mutants. **Measured cost: the psql sweep was 66 minutes, not the 1-2
hours estimated** — 23% of its mutants exceed 20s (a deterministic ~30s
connection-pool wait) and consume 92% of the wall time, while the median is 0.76s.
Nothing is committed; the tree is dirty per AGENTS.md.

### Read this before running `wreath mutant` again

Three ways it under-reports, each of which looks like "this code is untested" rather
than like a mistake. All three were walked into during this session.

**1. A wrong `--tests` set lies quietly, and `runner.py` says so** — "a test wrongly
left out turns a killed mutant into a reported survivor, which is a lie in the
direction people act on." Hand-picking test paths produced a report claiming 0.46
with 75% `unreached` for the auth stack. It was false: the sweep never received
`test_binding*.py`, `test_cedar*.py`, `test_jwt.py`, `test_permissions.py`, or any
users test. It also manufactured a phantom defect — two `_auth/requirements.py`
mutants "hanging" past 60s, which with the right tests die in 2.4s. **Derive the test
set from `docs/agents/manifest.json`.**

**2. The manifest is not sufficient either. 80 of 438 test files are attributed to no
subsystem**, and `wreath-map-lint` does not check for it — it checks dangling paths
and public-modules-without-subsystem only. `test_cors_middleware.py`,
`test_binding_unresolvable_annotations.py`, `tests/compliance/test_jwt_ec.py` and
`test_declaration_refusals.py` are among them. Measured impact: web scored 0.6695
from the manifest's list and 0.7538 once the unattributed files that *import* those
subsystems were unioned in. So union the manifest with import-derived ownership.

**This is a decision, not an oversight to fix blindly**: about 40 of the 80 are
`tests/rgb/` and `tests/compliance/` suites that genuinely span subsystems, and
forcing each into one owner may be worse than the gap. Three options, none taken:
require attribution and promote the cross-cutting dirs to subsystems; require it with
a declared allowlist the lint prints; or fix only the ~40 top-level omissions. The
per-file evidence, with owners derived from each file's `wreath.*` imports, was
generated during the session and is cheap to regenerate.

**3. Two whole categories of code are structurally unmutatable.**

- **Anything inside a `classmethod`** was, until this session — see the defect below.
- **Anything reachable only under `WREATH_PURE=1`.** Every pure twin behind an
  `if _native_x is not None:` fork never executes while the extension is built, so
  its guards report `unreached` forever. **Run the sweep twice**, once with
  `WREATH_PURE=1`, on the same argument AGENTS.md makes for free-threading and the
  JIT being separately tested execution modes.

Two more operational notes. **Set `WREATH_TEST_POSTGRES_DSN`** — without it the
DB-gated suites skip and the score reads low (`orm/types.py` reads 0.91 with 3
unreached instead of 0.98 with 0). **Adding tests is monotone**, so a finished sweep
never needs redoing: a `killed` verdict is final and only the non-killed ids need
re-asking, via repeated `--only`. That turned a 2.5-hour redo into minutes, twice.
Note the score is `killed/(killed+survived)`; `unreached` is excluded from the
denominator.

### The defect in `wreath mutant` itself

**`resolve_scope` could not reach any control inside a `classmethod`.** It walks a
dotted `Class.method` path with `getattr`, and `getattr` *invokes* the descriptor, so
what arrived at `_unwrap` was a bound `MethodType` and never the `classmethod` object
the code checked for. The docstring claimed classmethods were unwrapped; that branch
was dead on the only path that runs.

It failed invisibly rather than loudly: the refusal was reported as an `error`
outcome and excluded from the score, so a report read "97% verified" while quietly
not counting what it could not reach. **51 classmethods across 25 files**, and the
blind spot was concentrated in exactly the declared controls the tool exists for --
`crud.py`'s six `Access.*` constructors, `orm/schema.py`'s `SchemaMode`,
`migrations.py`'s `ResolutionPolicy`, `passes.py`'s `Ceiling.at_launch`.

Fixed with a `MethodType` branch first in `_unwrap` (the `staticmethod | classmethod`
branch stays -- it is still reachable for an object read out of a `__dict__`).
`cedar_engine.py` went from 8 "not a function" errors to 223 mutations, 0 errors.
`resolve_scope` had **no tests at all**, which is why this survived; there are now
tests for method, classmethod, staticmethod, property and `functools.wraps`, plus the
refusal path and two planner-level checks.

### The pattern every high-value finding shared

**A hand-rolled parser of attacker-controlled input whose refusals were never
exercised.** The happy paths were well covered; the guards were decoration.

| Module | Parses | Untested refusals | Input from |
| --- | --- | --- | --- |
| `_webauthn.py` | CBOR / COSE keys | 48 | the client |
| `_auth/jwt.py` | compact JWT | 55 unreached | the client |
| `binding.py` | request bodies and params | 54 unreached | the client |
| `_auth/oauth2.py` | OAuth2 responses | 50 unreached | a third party |
| `_auth/cedar_engine.py` | Cedar policy text | 18 unreached | a developer |
| `_auth/_ecverify.py` | P-256 point arithmetic | 16 unreached | derived from client data |

Two were closed; the rest are the obvious next work.

**`_webauthn.py`: 0.80, non-killed 132 -> 75.** `tests/test_webauthn_parser_refusals.py`
(55 tests) covers the CBOR decoder and COSE key parser: truncation, indefinite-length
framing, reserved additional information, lengths that outrun the buffer, the
near-2**64 length the source comments on ("a refusal instead of an allocation"),
non-UTF-8 text, floats, tags, non-integer and duplicate map keys, and every COSE
algorithm/curve/coordinate check. What remains uncovered is the DER signature parser
(`:324-342`), attested credential data (`:440-447`) and the attestation object
(`:506-510`) — the same shape, not yet done.

**`_auth/jwt.py`: 0.5851 -> 0.6739, `_parse_compact` non-killed 19 -> 1.** The one
survivor is `if _native_parse is not None`, which is provably equivalent within a
single mode. Getting there took two false starts worth recording:

- A subprocess version of the tests (following `test_http_client_protocol.py`) passed
  16 tests and moved nothing — lines 311-321 stayed `unreached`. **Subprocess tests
  cannot kill mutants**: the mutation is in a forked child's memory and
  `subprocess.run(sys.executable, ...)` reads pristine source from disk.
- An in-process version asserting only `pytest.raises(ValueError)` left seven alive.
  **The caps are ordered and overlapping** — a token past the 1 MiB total cap
  necessarily has a segment past the 21849 segment cap, so deleting the total-cap
  `raise` merely falls through to the next guard and still raises `ValueError`. Only
  the message says which guard fired.

The working shape is in-process, mode-agnostic, message-pinned tests, run in both
modes. `tests/test_jwt_pure_parse_caps.py` carries a table of both twins' wording for
fourteen malformed tokens.

**And it surfaced a twin divergence.** The Python branch's comment claims it enforces
"the same hard size caps as native jose_parse". It does not, by two characters:
native refuses a segment at length >= 21848 (the base64 length of a 16 KiB payload),
the Python branch computes `(_MAX_SEGMENT_BYTES * 4) // 3 + 4` = 21849 and refuses
only what is strictly greater. Lengths 21848 and 21849 are refused by one twin and
accepted by the other. Two bytes on a 16 KiB cap is not a hole, but which bound is
intended is a decision; aligning the Python branch is a one-character change in the
conservative direction. **Left for a human**, with the measured table in the test
docstring and nothing asserted inside the disputed window — a test that asserted
there would pin the discrepancy rather than the contract. Native is also the more
specific of the two: it separates "must have exactly two dots" from "has an empty
segment" where the Python branch answers both with one message.

### `orm/schema.py`: 0.533 -> 1.000

Six tests. The findings were the `orm/types.py` shape again — happy paths covered,
refusals not — plus one that is worth more than the others:

**`fingerprint_model` did not distinguish a unique table index from a plain one in
any test.** Both arms of `b"ui\x1f" if table_index.unique else b"i\x1f"` survived. The
fingerprint is what migration drift detection compares, and the code three lines
below that marker documents precisely this hazard for a partial index's predicate --
"without this a partial index could have its WHERE edited and no fingerprint would
move". The `unique` flag had the same hole and no test: a plain index changed to
unique is a new constraint on the data, and if the fingerprint does not move, nothing
tells anyone to build it.

The others: an invalid `isolation` was never passed to `SchemaMode.isolated` (its
`raise` unreached); a non-`str` schema name was never passed (`fullmatch` would raise
`TypeError`, naming neither the schema nor the problem); nothing exceeded the 63-byte
identifier limit, where PostgreSQL *truncates* rather than refusing, so wreath would
address a schema it did not create; and `qualified_name`'s two arms were pinned only
through `compile_select`, never through the property that decides them.

One test there deliberately records a non-finding: the 63-byte check reads
`len(value.encode("utf-8")) > 63`, but `_SCHEMA_IDENTIFIER` is `[a-z_][a-z0-9_$]*`,
so everything surviving the regex is ASCII and the `encode` cannot disagree with
`len`. It is defensive rather than load-bearing, and the test asserts the *refusal
order* so that stays a checked fact — it will fail exactly when someone widens the
regex, which is when the encode would begin to matter.

### Scores, and where the remaining work is

Each corrected by re-checking non-killed mutants against the fuller test set.

| Area | Reported | Corrected | Unreached | Concentration |
| --- | --- | --- | --- | --- |
| psql / ORM (~14k lines) | 0.6945 | recheck pending | 117 | `orm/session.py` 132, `orm/compiler.py` 72, `orm/registry.py` 62, `migrations.py` 59, `_migrations/scan.py` 45 |
| web | 0.6695 | **0.7538** | 66 | `response.py` 60, `_pure/dtrouter.py` 50, `middleware/cors.py` 47, `request.py` 44 |
| authz | 0.6371 | **0.6752** | 224 | `binding.py` 196, `_auth/jwt.py` 103, `cedar_engine.py` 85 |
| identity | 0.6470 | **0.6698** | 79 | `_webauthn.py` 132, `users.py` 134, `_secondfactor.py` 52 |
| `orm/schema.py` | 0.533 | **1.000** | 0 | done |
| `orm/types.py` + `_sparsevec.py` | 0.72 | **0.9804** | 0 | done |

`_pure/dtrouter.py`'s 50 are probably category 3 above — a pure twin needing a
`WREATH_PURE=1` sweep rather than more tests. Check that before writing any.

Not yet triaged when this was written: the `runtime` sweep (jobs, objects, workflows,
locks, temporal, series) and the `app` sweep (application, openapi, graphql, mcp,
crud, pagination, configuration, doctor, http-client), plus the psql re-check.

`users.py` still has untested declaration refusals of the `orm/types.py` kind --
`max_attempts must be at least 1`, `window must be positive` -- and
`_secondfactor.py` has five WebAuthn ceremony refusals unreached.

## Session five — a fourth way the sweep lies, and `objects.py` closed

The runtime sweep's largest concentration was `objects.py` (94 non-killed) and it
turned out to be mostly a lie. **`objects.py` + `_sigv4.py`: 0.4251 → 0.9457, with
7 `unreached` and 4 `timeout` now 0.** Of the 113 mutants the sweep did not kill,
103 are killed and the 10 that remain are each redundant by construction, with the
proof written next to the test that pins the backstop it leans on. The denominator
moves from 167 to 184 because a decided mutant joins it -- `unreached` and
`timeout` are excluded, so closing them *lowers* the score before the tests raise
it. Nothing is committed; the tree stays dirty per AGENTS.md.

### 4. A test that loads its module by path cannot kill anything

The same defect as the subprocess pitfall (#2 above) wearing different clothes.
`tests/storage/test_zip.py` did

```python
spec = importlib.util.spec_from_file_location("wreath_objects_standalone", _SRC / "objects.py")
```

so that it runs under a bare `/usr/bin/python3` with no built extension. That
execs pristine source into a **second module object**, and the mutation is applied
to `wreath.objects` in the forked child's memory, so it never reaches the code
under test. Every `unzip_stream` limit was already covered by a passing test and
every one was reported `survived`.

`tests/test_statsd.py` and `tests/test_cloudwatch_emf.py` had the right shape
already -- try `importlib.import_module(f"wreath.{name}")` first, fall back to the
by-path load only on `ImportError` -- and `test_zip.py`, `test_sigv4.py` and
`test_prometheus.py` did not. Adopting it recovered **28 of the 107 reported
survivors with no new test at all**, and it is worth checking before writing any:
`grep -rln spec_from_file_location tests/` is the whole audit, and the two
observability files it also fixed have never been swept.

### The pattern this time: a guard whose neighbour answers for it

Session four's lesson was that overlapping ordered guards mask each other
(`_auth/jwt.py`'s caps). Here it is most of what is left, and the useful move is
to *prove* the masking rather than keep writing tests at it:

- `_zip_entry_count`'s `eocd < 0` and `zip64 < 0` are both unreachable-by-effect
  because a negative offset becomes `directory_end`, and
  `directory_end - directory_size` is then negative for every unsigned size, so
  `directory_start < 0` refuses it one guard later either way.
- `cursor > directory_end` mid-walk makes the final `cursor == directory_end`'s
  `else None` unreachable, and vice versa: one of the two is always redundant.
- `exists`'s `if resp.status == 200: return True` is redundant with the
  fall-through `bool(self._ok(resp, 200))` it ends in.
- `EMPTY_SHA256` **is** `sha256_hex(b"")`, so `_send`'s conditional cannot be
  observed -- it exists to avoid hashing, not to produce a different answer.

Each is defence in depth rather than dead code, so none was deleted; what the
tests pin instead is the *backstop*, named in a comment, so the argument fails
loudly if someone reorders the guards.

### The real gaps it was hiding

**`_zip_entry_count` had no caller in any test.** It is the pre-check that bounds
work before `zipfile` materializes a declared entry graph, and every one of its
framing refusals returns `None` ("let `zipfile` diagnose it"), so the entire body
could be replaced with `return None` and nothing would fail. Two findings:

- **The existing entry-count test could not tell the two limits apart.** Two
  entries against a limit of one reads `"2 entries"` whether the pre-check refused
  (it stops at `max_entries + 1`) or the `infolist()` check below it did (it reports
  the true total). Both guards and both raises were free to be deleted. Ten entries
  against a limit of three reads `"4"` from the first and `"10"` from the second,
  which is what distinguishes them.
- **A negative `directory_start` does not refuse itself.** Python indexes a
  negative slice from the *end*, so `raw[cursor:cursor + 4]` reads real archive
  bytes and a central-directory signature can match; a crafted `cd_size` walks a
  directory the file never declared and reports an entry count from it.

**`zipfile` accepts trailing bytes after the end record and the pre-check does
not**, which is the only route to the `infolist()` limit -- and the reason
`declared_entries is not None` is load-bearing: without it a readable archive
fails on `None > 1024`.

**`sign()` was unpinned.** Its one test asserted a prefix and a tautology
(`... or out["x-amz-content-sha256"]`, whose second arm is always truthy). Nothing
had ever signed with a **session token**, which STS and instance-role credentials
always carry: it has to be both signed and sent, and the two mutants for those two
halves both survived. The vectors pin `canonical_request`/`string_to_sign`/
`signing_key` directly but never see what `sign` assembles before calling them, so
the new test spells the expected header set out by hand and re-derives the
signature from the vector-pinned primitives.

**A fake that serves the same page forever converts a bug into a hang.** Four
`S3ObjectStore.list` mutants dropped the continuation token or the loop break and
were reported `timeout`, which is "undecided" -- against a real bucket that is an
unbounded spend. The handlers now refuse a page past the last one, which turns all
four into failures. `test_list_paginates` needed the same bound: it ran *before*
the new tests, so its hang consumed the deadline and the new bound was never
reached. **A per-mutant `--timeout 30` (from 60) roughly halved the recheck.**

Also closed: `list` never sent a `prefix` or `delimiter` in any test; `stat` was
never handed a response missing `content-length`/`etag`, or holding them empty;
no test used a bytes-like `url_secret` from a second store, so a store that
ignored the secret and signed with a random key verified its own URLs and passed;
`normalize_key` never saw a `DEL`; and `delete`'s parent walk (`create=False`)
had no test, where removing the refusal makes a delete of a key that was never
written silently `mkdir` the path.

### What is next in the runtime sweep

`objects.py` was the largest concentration; the rest of the runtime sweep is
untouched and its data is on disk (`sweep_runtime.json`, complete, 0.6755).
Remaining non-killed by file: `jobs.py` 50, `_passes/driver.py` 46, `series.py`
41, `_passes/ledger.py` 37, `passes.py` 25, `_passes/gate.py` 24,
`messaging.py` 22. Before writing tests for any of them, check for a bounded-fake
hang and for `spec_from_file_location` in the owning test files -- both were worth
more here than any test was.

### `jobs.py`: 0.7209 → 1.0000, 25 `unreached` → 0

The largest single finding: **`jobs.drive()`'s shift handler had never been
executed by a test.** `_start_passes` was covered, so the pass got its *first*
shift; nothing ever ran one. Every branch deciding whether a pass continues,
halts or fails was `unreached` — a pass that silently stopped after one chunk
would have looked exactly like a passing suite, and this is the mechanism that
keeps the purge and rewrite passes behind `session_store`, `webhooks`,
`middleware/idempotency` and `middleware/ratelimit` making progress.

The test set mattered twice more, both pitfall #1: the manifest's list omits
`tests/rgb/` (7 files import `wreath.jobs`, all unattributed), and `tests/passes/`
owns the pass-driving suites. Adding them took the baseline from 569 to 858 tests
— and moved **nothing**: all 20 of `drive`'s `unreached` mutants stayed unreached,
which is how a genuine gap looks once the test set is no longer the explanation.
Worth knowing that a wider set is cheap to try and cheap to rule out.

Also closed: the two interval guards that share one message (`lease` and
`poll_interval` — each had to be shown separately to reach it), `batch < 1`,
`retries < 0`, `timeout <= 0`, a non-identifier task name, `_tick_schedules`
(the cron matcher had never run: a schedule that never fires and one that fires
every tick were the same passing suite), `_report_terminal`'s `100 if done`
(reporting a *failure* at 100 tells a watching client the work finished), `_fail`
with `handler=None` (reached for a task this release no longer registers), and
`_discard_claim` removing the *right* claim rather than the first — a wrong
removal hands back a job that is already running, which is two workers on one job.

`enqueue`'s unknown-task refusal needed `match=`, not `pytest.raises(KeyError)`:
the dict lookup three lines below raises `KeyError` too, so the guard was
deletable with the suite green. Same lesson as `_auth/jwt.py`'s caps.

### One mutant is knowingly undecided, and that is the right answer

`guard.never-fires@jobs.py:808` — the worker's park between empty claims — reads
`timeout` in a whole-directory run. It **is** killed, by
`test_a_worker_with_nothing_to_claim_waits_instead_of_re_querying`, verified with
`--tests tests/jobs/test_runner.py`. It reads `timeout` in the wider run because
`test_runner_doorbell.py` spawns real workers against a fake whose `await`s never
suspend, so a busy-spinning worker starves the event loop and the pytest process
hangs before reaching the test that would fail.

**Do not "fix" that by making the fake yield.** Adding `await asyncio.sleep(0)` to
`DatabaseDouble.acquire` (or to the doorbell's `FakeDatabase`) does decide the
mutant, and it was tried and reverted: it changes shipped code to make a tooling
run terminate, with no defect behind it. A mutant that converts a poll loop into a
busy-spin *is* a hang. Bounding a fake you are already writing is fair; editing a
shared double so a mutant terminates is not. Name the killing test and narrow
`--tests` instead.

Note the score reads 1.0000 because `timeout` is excluded from the denominator
(`killed/(killed+survived)`), not because everything was decided — here that is
not misleading only because the mutant is separately verified as killed.

### Runtime sweep, remaining

`objects.py` and `jobs.py` are done. Still untouched, data on disk in
`sweep_runtime.json`: `_passes/driver.py` 46 non-killed, `series.py` 41,
`_passes/ledger.py` 37, `passes.py` 25, `_passes/gate.py` 24, `messaging.py` 22,
`_series/compile.py` 17, `progress.py` 14, `_sigv4.py` done, `_passes/buckets.py`
13. The `app` sweep (2751 mutants) and the psql re-check were interrupted in
session four and never restarted.

Environment: the test Postgres needed `podman restart` — the container was up but
its host port forwarder had died, so `pg_isready` passed *inside* while
`127.0.0.1:55432` refused. `podman restart` failed once ("conmon exited
prematurely") and a plain `podman start` after it worked. The DSN made **no
difference** to any jobs number: those suites are in-memory, and only 5 of the 85
tests in `tests/jobs/` are DB-gated.

## Session six — `binding.py`, and a lint suppression that hid the real answer

**`binding.py`: 0.6650 → 0.8192, non-killed 196 → 95, `unreached` 61 → 13.** It was
the largest single concentration left in any sweep (the authz sweep's 196, ahead of
`_auth/jwt.py`'s 103). Nothing is committed; the tree stays dirty per AGENTS.md.

The sweep data from sessions four and five was recovered from the previous session's
`/tmp` scratchpad and copied forward — it is 8.7 MB of `sweep_*.json` and it had
survived, but re-earning the psql sweep alone costs 66 minutes, so it should not live
only in `/tmp`.

### Pitfall #3 is closed; pitfall #1 was worth 41 mutants on its own

`grep -rln spec_from_file_location tests/` returns five files and **all five now try
`importlib.import_module` first**, so there are no free recoveries of that kind left
anywhere in the suite. Checked first because session five said to.

Pitfall #1 was still worth a great deal. The manifest attributes seven test files to
the `binding` subsystem and omits `test_binding_unresolvable_annotations.py`,
`test_dependency_and_path_refusals.py`, `test_lazy_scope.py`, `tests/orm/test_binding.py`,
`tests/orm/test_validation.py` and `tests/security/test_web_framework_hardening.py`.
Unioning those in — 208 tests, 1.6 s — **killed 41 of the 196 with no test written.**
The remaining 155 were then real.

### The pattern: a compound guard needs one input per clause

Session four found overlapping *ordered* guards masking each other; session five found
a guard whose neighbour answered for it. Here it is neither. It is that a clause is
only held by an input that isolates it, and almost every survivor was one of:

- **Two spellings of one idea.** `annotation is Any or annotation is inspect.Parameter.empty`,
  `annotation is None or annotation is _NONE_TYPE`, `annotation is Instant or annotation
  is _datetime.datetime`. A test using one spelling leaves the other clause deletable.
  `Parameter.empty` reaches `_convert_scalar` only through an **unannotated path
  parameter** — a path parameter binds by name and needs no marker, which is the one
  parameter shape that can arrive without an annotation at all.
- **An accumulator that had only ever been empty.** `errors = invalid.errors if errors
  is None else [*errors, *invalid.errors]` appears once per parameter kind, and each
  needs *two* requests: one where that kind fails first (`errors is None`) and one where
  it fails after another kind. No test had ever sent a request that failed in two places,
  so a client that got three things wrong would have been told about one. The path-param
  site needs two bad segments in one route, because nothing runs before it.
- **A branch reachable only from a shape nobody builds.** `compile_binder`'s twenty
  `() if spec is None else spec.<field>` expressions: `inspect_handler` returns `None`
  for a `(request)`-only handler and `compile_binder` returns it untouched — *unless*
  route-level `dependencies` were passed, which is the sole path where `spec is None`
  and the body still runs. One test for that shape (a side-effect-only dependency on a
  handler that binds nothing) reached all twenty.

Also closed: the four connection-injection refusals (`security_read`, unnamed-with-0
and unnamed-with-2 databases, an unknown name), an absent header or cookie falling back
to its default, `Optional[T]` on a scalar source, `Mapped[T]`/`Optional[T]`/
`Mapped[T] | None` peeling in `_unwrap_form_type`, and `_compile_plan` — the *native*
plan compiler, which mirrors `_validate` "exactly" and had never been exercised through
a request, the only thing that runs a plan.

**One finding worth more than its mutant count: the validation bomb had no test at all.**
`_VALIDATE_MAX_STEPS = 2_000_000` is a denial-of-service bound whose own comment
describes the attack, and nothing had ever triggered it. A nest of 23 two-arm unions
(~200 bytes of JSON) exhausts it in 1.9 s and must report exactly one `too_complex`
error at the root. Note the mutants that *remove* the bound turn the bomb back into
unbounded work and are reported `timeout` — a removed DoS bound really is a hang, which
is session five's `jobs.py:808` situation again, and undecided is the honest outcome.

### The `noqa` that inverted its own test — now a rule in AGENTS.md

`_validate`'s `args[0] if args else Any` and `args[1] if len(args) == 2 else Any`
fallbacks (and their `_compile_plan` twins) are reached only by an annotation with an
origin and **no args**. Measured: the only spellings that produce one are the deprecated
`typing.List`/`typing.Dict`, plus a synthetic `types.GenericAlias(list, ())`. Every
modern spelling either has no origin (`list`, `collections.abc.Sequence`) or has args.

Four `# noqa: UP006`s went in to reach them, and the episode is why AGENTS.md now
forbids suppressing a lint to pass your own new code:

- `ruff check --fix` had already rewritten one *unsuppressed* use to bare `list`, which
  **inverted what the test asserted** — bare `list` has no origin and *is* `unsupported`.
  It passed alone (nothing had run before it) and failed in the suite. A test that
  asserts the opposite of its docstring is worse than no test.
- The correct answer was not a suppression. Those fallbacks are **not dead** — a
  *caller's* handler may be annotated `typing.List`, and their lint config is not ours —
  they are simply unmeasurable from inside this repository. The four surviving arms are
  honestly undecided, and deciding them means allowing the alias in one declared,
  scoped `per-file-ignores` entry, which is a policy call for a human.

The test now pins what *is* true and modern (bare `list`/`dict` are reported
`unsupported`, naming the annotation) and records the limit in its docstring.

### What is left in `binding.py`, and what is next

95 non-killed: `compile_binder` 38 (the dependency-cache and resource-release paths —
`marker.use_cache`, `borrowed or opened`, `defer_release`, `StreamingResponse`),
`_compile_dep` 13, `inspect_handler` 11, `_form_model_fields` 6, `_release` 6,
`AppScope` 7 (concurrency: `future.done()` races and `aclose` error aggregation, one
already `timeout`), `_compile_plan` 3, `_validate` 3, `_unwrap_form_type` 2.

The `AppScope`/`_release` group is the hard one — it needs two coroutines racing for the
same app-scoped dependency, and one mutant there is already hang-shaped. The
`compile_binder` resource group is ordinary work.

Unchanged from session five: the `app` sweep (2751 mutants) has never produced a file
(`sweep_app.json` is 0 bytes), the psql re-check never ran, no `WREATH_PURE=1` sweep has
ever run, and the manifest-attribution decision (80 of 438 test files own no subsystem)
is still open. `_auth/jwt.py` at 103 non-killed is now the largest single concentration.
