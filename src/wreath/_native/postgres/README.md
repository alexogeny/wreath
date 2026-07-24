# `wreath/_native/postgres` — the metal PostgreSQL tier

This directory is one CPython extension, `wreath._native._postgres`, built from
`../_postgresmodule.c` which `#include`s or links every `.c` here. It holds two
cooperating subsystems:

1. **The PostgreSQL client** — wire protocol, decode, and row hydration for the
   ORM and pool.
2. **The migration engine** — a format stack that turns ORM intent and a live
   catalog into a checksummed, verifiable, *reversible* migration artifact, and
   applies (or reverts) it under a transaction lock.

Most day-to-day migration logic and all I/O orchestration lives in Python
(`src/wreath/migrations.py`, `src/wreath/_migrations_cli.py`); the C here is the
bounded, allocation-frugal core those call into. **Rule of thumb:** parsing,
diffing, SQL text, checksums, and inversion are metal; connection lifecycle,
transactions, and history SQL are Python.

## Build / test / lint

```bash
uv run python setup.py build_ext --inplace     # rebuild the extension
uv run pytest tests/migrations/                 # migration suite (no live DB needed)
uv run wreath-native-lint                       # native memory/error lints
```

The migration tests use a mock `Connection` and monkeypatch the catalog decode,
so the full apply/downgrade orchestration is exercised without PostgreSQL. Only
`tests/migrations/test_catalog_integration.py` needs a real DB (set
`WREATH_TEST_POSTGRES_DSN`; it is otherwise skipped).

## The client subsystem (wire ↔ rows)

| File | Role |
|---|---|
| `protocol.c` | Frontend/backend message state machine; the fused-stream entry point. |
| `codec.c` / `codec.h` | Encode/decode individual PostgreSQL wire messages and parameter binding. |
| `decode.c` / `decode.h` | Field/tape decoding primitives shared by hydration and the catalog image builder. |
| `hydrate.c` | Turn decoded rows into ORM model instances. |
| `record.c`, `tape.c`, `slab.c`, `buffer.c` | Bounded arena/tape/buffer primitives; no per-row Python allocation. |
| `connection.c`, `pool.c` | Connection and pool handles surfaced to Python. |
| `model.c`, `operation.c`, `plan.c` | ORM-side compiled query plan structures. |

`buffer.[ch]` (the `WreathPgBuffer` grow-append-finish helper) is the one piece
the migration engine borrows heavily — see below.

## The migration engine

### Format stack (all little-endian, magic-tagged, length-checked)

| Magic | What it is | Built by | Layout summary |
|---|---|---|---|
| `WMD1` | **Descriptor** — ORM intent or catalog rows as text (schema, table, name, kind, signature) | Python `_registry_descriptor`; catalog SQL | 12-byte header + per-record `u16×4 lengths, u32 kind, then bytes` |
| `WMI1` | **Image** — canonical packed schema, sorted by `(kind, object_id)` | `_migration_compile_desired` (from WMD1); catalog builder (from live rows) | 16-byte header + 24-byte records: `object_id(8) parent_id(8) kind(4) signature(4)` |
| `WMP1` | **Named plan** — the *authoritative* operation list with full before/after signature text | `_migration_plan_descriptors` (diff of two descriptors) | 12-byte header + per-op `u32 action, u32 kind, u16 schema/table/name/before/after lengths, u16 reserved, then bytes` |
| `WMO1` | **Operation tape** — compact 24-byte ops (`action, kind, object_id, before_sig, after_sig`) | `_migration_operations_from_plan` (re-derived from WMP1) | header + 24-byte records |
| `WMS1` | **SQL tape** — one deterministic DDL statement per op, with `DESTRUCTIVE`/`MANUAL` flags | `_migration_render_sql` (re-derived from WMP1) | header + per-stmt `u32 flags, u32 length, bytes` |
| `WMA1` | **Artifact** — the immutable, SHA-256-checksummed envelope binding WMO1+WMP1+WMS1 with migration_id, parent, source & target fingerprints | `_migration_build_artifact` | 168-byte header (checksum at offset 136) + `operations ‖ named_plan ‖ sql` |
| `WMC1` | **Chain** — a packed sequence of WMA1 artifacts for parent/source continuity checks | Python `_verify_native_chain` | header + per-artifact `u32 length, bytes` |

**WMP1 is the single source of truth.** WMO1 and WMS1 are *always* re-derived
from WMP1 and byte-compared when an artifact is built or verified
(`migration_artifact.c`). `action ∈ {add=1, drop=2, alter=3}`,
`kind ∈ {table=1, column=2, constraint=3, index=4}`. `object_id` is a 64-bit
FNV-style fold of `(kind, schema, table, name)` — the same function
(`wreath_pg_migration_object_id`) is used everywhere, so ids match across plans,
images, and descriptors.

### Files

| File | Owns |
|---|---|
| `migration_image.c` | WMD1→WMI1 compile, catalog→WMI1 builder, image fingerprint (SHA-256), and the linear-merge **diff** that emits WMP1. Object-id and signature hashing live here. |
| `migration_sql.c` | WMP1 parsing, WMP1→WMO1, WMP1→WMS1 (DDL text), the **reverse plan** (`_migration_reverse_plan`), and the **downgrade hazard scan** (`_migration_downgrade_hazards`). |
| `migration_artifact.c` | SHA-256, WMA1 build/verify (re-deriving WMO1/WMS1), and WMC1 chain verification. |
| `migration_runner.c` | WMS1 → one dollar-quoted transactional `DO` block, rejecting `MANUAL` and gating `DESTRUCTIVE`. |
| `migration_resolver.c` | Packed fleet readiness classification (`resolve_fleet`). |
| `migration_*.h` | Init hooks (`wreath_pg_migration_*_init`) called from `_postgresmodule.c`, plus the few cross-file symbols (`object_id`, `signature`, `sha256`, `render_sql`, `operations_from_plan`). |

### Data flow

```
generate:  models ─_registry_descriptor→ WMD1 ─compile→ WMI1 ┐
           live catalog ─catalog SQL→ WMI1 ────────────────┤ diff → WMP1 ─┬→ WMO1
                                                                          ├→ WMS1
           fingerprints = SHA-256(WMI1 desired) & SHA-256(WMI1 live)     └→ WMA1 (checksummed)

apply:     WMA1 ─verify→ WMS1 ─build_ddl_block→ DO-block
           BEGIN; advisory-xact-lock(schema); check history tip == parent
             & live fingerprint == source; run block; require live == target;
             INSERT history; COMMIT (else ROLLBACK)

downgrade: WMA1 ─verify→ WMP1 ─reverse_plan→ WMP1' ─render_sql→ WMS1' ─build_ddl_block→ DO-block
           hazard scan: reverse WMP1' ∩ live ORM WMI1  → refuse if non-empty (unless --force)
           BEGIN; lock; check tip == this artifact & live == target;
             run reverse block; require live == source; DELETE tip row; COMMIT
```

### Key invariants & design notes

- **Reversibility is free.** Every WMP1 op carries both a before and an after
  signature, so `_migration_reverse_plan` inverts by flipping the action and
  swapping before/after — no database, no guessing. The reversed WMP1 is itself a
  valid forward-shaped plan and re-derives its own WMO1/WMS1. `operation_rank`
  orders drops inner-to-outer and adds outer-to-inner so a reverse plan sequences
  correctly (drop index → constraint → column → table; add the reverse order).
- **Constraints and indexes are named deterministically** as
  `"wreath_" + hex(object_id)` (`append_derived_object_name`). Forward creates
  them by that name so a downgrade can drop them by the same name. This is
  fingerprint-safe: images identify constraints/indexes by *columns*
  (`p:cols:::`, `u:cols:::`, `f:cols:schema:table:cols`, `i:cols`, `ui:cols`),
  never by the Postgres-assigned name, so naming changes DDL bytes (and thus the
  WMA1 checksum) but never a fingerprint. Composite keys are just comma-joined
  columns. A foreign key's *name* is its identity (columns + target); its
  *signature* additionally carries `:deltype:updtype:deferrable` so a changed
  referential action shows as drift (rendered `MANUAL`, since an FK action change
  is a drop+recreate, not an in-place alter).
- **The hazard scan** (`_migration_downgrade_hazards`) binary-searches the live
  ORM WMI1 (sorted by `kind, object_id`) for every table/column a reverse plan
  would drop, or column whose signature it would change, and returns
  `(schema, table, name, kind, reason)` tuples. Python raises
  `DowngradeWouldStrandCode` unless `force=True`. This is what stops a production
  downgrade from stranding a still-deployed release that references the column.
- **`MANUAL` is honest, both ways.** Objects Wreath cannot render (composite
  constraints, full FK actions/deferrability, expression/partial indexes) are
  emitted as `MANUAL` statements with no SQL. `migration_runner.c` refuses any
  tape containing one, so such an artifact is ineligible for both `apply` and
  `down`.
- **No fallbacks.** Every Python entry point checks for its native symbol and
  raises "rebuild Wreath's native extensions" rather than degrading silently.
- **History table** (`wreath_migrations.history`, created lazily under an
  advisory lock): `(target_schema, migration_id)` PK, `UNIQUE(target_schema,
  checksum)`, storing parent/source/target fingerprints and the destructive
  approval. `apply` appends the tip; `down` deletes it, so schema *and* history
  return to their exact pre-apply state and a re-apply behaves identically.

When extending: add object coverage in the diff (`migration_image.c`) and the
renderers (`migration_sql.c`) together, keep forward `ADD` and reverse `DROP`
symmetric, and add both the forward golden SQL and a reverse round-trip test in
`tests/migrations/`.
