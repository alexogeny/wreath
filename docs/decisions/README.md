# Architectural decision records

`AGENTS.md` says what you must do. These say **why**, **what was rejected**, and
**what would reverse it**. A document that only restates a rule is not a record
and does not belong here — two copies of a rule is the failure `CLAUDE.md` opens
by naming, and it is how both copies end up wrong.

Every record cites the code that implements it. A decision nobody can point at
in the tree is not a decision this project made.

## The set

| # | Record |
| --- | --- |
| [0001](0001-python-314-only.md) | Target CPython 3.14 and nothing older |
| [0002](0002-no-mandatory-runtime-dependencies.md) | `src/wreath` ships with no mandatory runtime dependency |
| [0003](0003-own-the-data-and-validation-stack.md) | Own the validation and data stack; no Pydantic, no SQLAlchemy |
| [0004](0004-the-public-api-is-literal.md) | Each feature lives in the module its name implies |
| [0005](0005-c-first-with-a-pure-twin.md) | Every accelerated feature has a pure-Python twin under a parity contract |
| [0006](0006-optional-extensions-and-tiers.md) | Extensions are opt-in at build time; metal is a tier above native |
| [0007](0007-native-code-owns-no-process-global-state.md) | Native code owns no process-global mutable state |
| [0008](0008-markers-annotate-defaults-default.md) | Markers live in `Annotated`; the request is the first parameter |
| [0009](0009-rfc-9457-problem-json.md) | Every built-in error is an RFC 9457 problem document |
| [0010](0010-one-shot-response-extension.md) | Wreath's own servers accept a one-shot response message |
| [0011](0011-bitset-routing-is-the-default.md) | Bitset routing is the default matcher |
| [0012](0012-server-deadlines-leave-the-heap.md) | Server deadlines use a slot array with a tournament tree |
| [0013](0013-postgresql-is-the-queue.md) | PostgreSQL is the queue; there is no broker |
| [0014](0014-migrations-are-generated-from-the-catalog.md) | Migrations are generated from the live catalog, not authored |
| [0015](0015-the-orm-depends-on-postgres-and-never-the-reverse.md) | The ORM depends on `postgres`; loading is always explicit |
| [0016](0016-validate-the-schema-at-startup.md) | Validate the schema at startup; never `create_all` |
| [0017](0017-cedar-is-built-in.md) | The Cedar engine is built in, not a dependency |
| [0018](0018-a-broad-except-is-the-exception.md) | A broad `except` is the exception, not the rule |
| [0019](0019-refuse-rather-than-half-wire.md) | Refuse a wrong declaration rather than half-supporting it |
| [0020](0020-a-double-is-never-more-capable.md) | A test double is never more capable than what it doubles |
| [0021](0021-documentation-is-checked-against-the-api.md) | Documentation is checked against the real API |
| [0022](0022-complexity-is-a-contract.md) | Complexity is a contract, asserted by probes |
| [0023](0023-landing-belongs-to-the-human.md) | Landing belongs to the human; no commits, no attribution |
| [0024](0024-a-check-that-has-nothing-to-check.md) | Name the failure: a check that silently has nothing to check |

## Format

Context, Decision, Consequences, Alternatives rejected, and *What would reverse
this*. The last two carry the value. A record with no cost listed is marketing,
and a decision nothing could overturn was not a decision.

## History

The 22 records that preceded this set were written when the project was called
`neo`, cited module paths that no longer exist, and were never tracked by git —
`.gitignore` excluded `docs/decisions/` entirely, so a fresh clone had none of
them while `docs/agents/manifest.json` cited twenty. They were retired wholesale
rather than patched. Where their reasoning survived the rewrite it is marked in
the record that absorbed it.
