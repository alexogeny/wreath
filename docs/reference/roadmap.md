# Reserved and in-progress surfaces

Wreath prefers to reserve a name early so a feature can land without a later
breaking move. The modules once listed here have since shipped —
[`wreath.telemetry`](telemetry.md), [`wreath.recording`](recording.md), and
[`wreath.replay`](replay.md) are real APIs with their own reference pages.

What remains genuinely unfinished is listed below, so no page has to imply
more than exists:

| Surface | Status |
|---|---|
| Tenant-fleet DDL execution | Not shipped. Single-schema `apply` is available and the managed fleet *readiness* runner (`resolve_fleet`) classifies a whole directory in one metal call, but applying one artifact across many tenant schemas under fleet locking is still to land. |
| Broader migration object coverage | `detect`/`generate`/`apply`/`down` cover tables, columns, primary keys (including composite), per-column and composite unique constraints, foreign keys with referential actions and deferrability, and single- and multi-column btree indexes (including unique indexes, and partial indexes whose predicate is built from `eq`/`is_null`/`is_not_null`/`one_of`/`all_of` over text, integer, and boolean columns). Expression/covering/non-btree indexes, partial predicates outside that vocabulary, index-method options, rename hints, and changing an existing foreign key's action are still being implemented (emitted as `MANUAL`); keep Alembic for schemas that use them. |
| Recording capture engine (`WFR1` sink) | Not shipped. `wreath.recording` currently provides deny-by-default policy types only. |

When one of these ships, its row leaves this page and its reference page tells
the full story.
