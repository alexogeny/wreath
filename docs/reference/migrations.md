# `wreath.migrations`

`wreath.migrations` is the control surface for Wreath-metal PostgreSQL migration
resolution. It configures managed or strict readiness checks and returns bounded
fleet summaries; the engine requires Wreath's native PostgreSQL extension rather
than silently falling back to a different implementation.

The direct catalog destination, single-schema `detect` and `check` commands,
packed image diff, deterministic named `generate` review plan, checksummed
artifact and chain verification, and strict `show`/`status` commands are available
now. Executable SQL artifact generation and the DDL runner remain under active
implementation; unreleased execution commands are not presented as usable APIs.

::: wreath.migrations
