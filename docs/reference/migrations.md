# `wreath.migrations`

`wreath.migrations` is the control surface for Wreath-metal PostgreSQL migration
resolution. It configures managed or strict readiness checks and returns bounded
fleet summaries; the engine requires Wreath's native PostgreSQL extension rather
than silently falling back to a different implementation.

The direct catalog destination, single-schema `detect` and `check` commands,
packed image diff, deterministic named `generate` review plan, checksummed
artifact and chain verification, and strict `show`/`status` commands are available
now. `generate_single_baseline` and the `baseline` command build a verified,
zero-operation root for a matching existing catalog; `adopt_single_baseline`
records that root without application DDL. The single-schema runner applies supported, fully automatic artifacts under
an advisory lock with history and source/target catalog verification, and
`revert_single_artifact` performs the exact inverse — the reverse plan is derived
in metal from the same artifact, guarded by a native scan that refuses to
downgrade past live ORM references unless forced. The managed fleet-readiness
runner (`resolve_fleet`, `TenantState`) classifies a whole tenant directory
against a target migration in one native call. Object coverage spans tables,
columns, primary keys (including composite), per-column and composite unique
constraints, foreign keys with referential actions and deferrability, and single-
and multi-column btree indexes. Expression/partial/covering/non-btree indexes and
tenant-fleet *execution* remain under active implementation; unsupported
operations are marked `MANUAL` and can be neither applied nor reverted.

::: wreath.migrations
