---
description: Detect schema drift, baseline an existing database, generate and review immutable migrations, apply them once, verify status and roll back safely.
keywords: guide PostgreSQL migrations detect check baseline generate review apply status rollback down deployment order
boost: 1.4
---

# Migrations from detect to rollback

Wreath migrations compare a compiled ORM registry with PostgreSQL's live catalog. A
migration is an immutable binary artifact with source and target fingerprints, a
parent checksum, a named operation plan and a SQL execution tape. The generated SQL
file is for review; the reviewed binary is the execution input.

Do not run migrations from every application replica. Put the same application code
and artifacts in one immutable image, run one migration job with a dedicated
credential, and start or roll application workers only after that job succeeds.

## The command lifecycle

| Stage | Command | Answer |
|---|---|---|
| inspect | `migrations detect` | what differs between models and the live catalog? |
| gate | `migrations check` | is there drift, an unsafe conversion read, or a pending pass? |
| adopt | `migrations baseline` | can an existing matching schema become the reviewed chain root? |
| author | `migrations generate` | what deterministic operation plan reaches the desired registry? |
| review | `migrations show` | is this artifact structurally valid and what does it contain? |
| reconcile | `migrations status` | do code, complete artifact chain, history and live catalog agree? |
| change | `migrations apply` | can this one artifact be locked, applied, recorded and verified? |
| reverse | `migrations down` | can the most recently applied artifact be safely inverted? |

All examples use the ORM registry named `main`. Pass `--database NAME` when an
application owns several registries. Add `--factory` when the target is an application
factory.

## 1. Detect before generating

Load the application using the same ordinary database configuration it uses in the
target environment:

```bash
uv run wreath migrations detect app:app --database main
uv run wreath migrations check app:app --database main
```

`detect` reports desired and actual fingerprints plus the operation count. `check` is
the CI/deployment gate: it exits nonzero on drift, reads pending chunked-pass guards,
and scans deferred conversions for application reads that cannot be proven safe during
the transition.

These two commands inspect through the configured application database. `baseline`,
`status`, `apply` and `down` use the separate `WREATH_MIGRATION_DSN` credential by
default. They never fall back to request-pool credentials.

## 2. Choose the correct root

### A new or migration-owned database

Generate the first artifact against the live starting catalog. A migration ID is 16
bytes written as exactly 32 hexadecimal characters:

```bash
uv run wreath migrations generate app:app \
  --database main \
  --output migrations/0001-initial \
  --migration-id 00000000000000000000000000000001 \
  --initial
```

`--initial` gives the artifact a zero parent checksum. Generation refuses to overwrite
an existing output directory.

### An existing schema that already matches the models

Do not fake a history by replaying DDL over live tables. Generate a zero-operation
baseline, review it, then adopt that exact directory in a second command:

```bash
uv run wreath migrations baseline app:app \
  --database main \
  --output migrations/0001-baseline \
  --migration-id 00000000000000000000000000000001

uv run wreath migrations show migrations/0001-baseline/migration.bin
```

Review the object inventory and catalog fingerprint in `migration.json`. Adoption
rebuilds the candidate and refuses if it differs from the retained review directory:

```bash
export WREATH_MIGRATION_DSN='postgresql://migration-role@db.example/app'

uv run wreath migrations baseline app:app \
  --database main \
  --output migrations/0001-baseline \
  --migration-id 00000000000000000000000000000001 \
  --adopt
```

Baseline only when the live schema and compiled registry already agree. It records the
reviewed root; it does not execute application DDL.

## 3. Generate the next artifact

Every later artifact names the **64-hex checksum of the previous artifact**, not its
human migration ID:

```bash
uv run wreath migrations generate app:app \
  --database main \
  --output migrations/0002-project-state \
  --migration-id 00000000000000000000000000000002 \
  --parent 9f1c0f5d3a6b7c8d9e00112233445566778899aabbccddeeff00112233445566
```

Take the checksum from the preceding `migration.json` or `migrations show` output.
Generation writes three retained review files:

| File | Purpose |
|---|---|
| `migration.bin` | authoritative, checksummed artifact consumed by `apply` and `down` |
| `migration.sql` | readable SQL for review; editing it does not change execution |
| `migration.json` | fingerprints, checksums, operations, statement flags and review metadata |

Review every operation and SQL statement. A manual operation is unresolved work, not
permission to deploy. A destructive statement is marked and will be refused by
`apply` unless the operator supplies `--allow-destructive` deliberately.

Verify the retained artifact rather than trusting filenames:

```bash
uv run wreath migrations show migrations/0002-project-state/migration.bin
```

Store the whole directory in source control. Do not regenerate an already reviewed
artifact under the same name or edit its binary.

## 4. Verify the complete chain in the target environment

`status` takes the root followed by every artifact through the intended tip. It
verifies parent checksums, compares the chain target with the compiled registry, reads
the live catalog and checks Wreath's migration history:

```bash
export WREATH_MIGRATION_DSN='postgresql://migration-role@db.example/app'

uv run wreath migrations status app:app \
  migrations/0001-initial/migration.bin \
  migrations/0002-project-state/migration.bin \
  --database main
```

Before the next artifact is applied, a non-current status is expected only in the
specific direction the reviewed artifact explains. After deployment it must report
that catalog, code, artifacts and history all match.

## 5. Apply once, then prove it

Apply artifacts in parent order through one deployment job:

```bash
uv run wreath migrations apply app:app \
  migrations/0002-project-state/migration.bin \
  --database main

uv run wreath migrations status app:app \
  migrations/0001-initial/migration.bin \
  migrations/0002-project-state/migration.bin \
  --database main
```

`apply` takes the migration lock, checks the source fingerprint and parent history,
executes and records the artifact, then verifies the target. Concurrent migration jobs
do not become a distributed race. A destructive artifact additionally requires:

```bash
uv run wreath migrations apply app:app \
  migrations/0003-remove-legacy/migration.bin \
  --database main \
  --allow-destructive
```

The flag is an operator approval, not a way to turn an unreviewed plan into a safe one.

## 6. Deploy in expand, move, contract order

For a simple additive change:

1. build the new image and run its tests, route manifest and `doctor preflight`;
2. run the new image's migration job;
3. require a current `migrations status`;
4. roll out application workers;
5. verify readiness and application behavior.

For a rename, type conversion, backfill or removal:

1. **expand** with a schema both old and new application code can use;
2. deploy code that writes the transition safely;
3. run a declared [chunked data pass](chunked-passes.md) and require no holes;
4. deploy code that reads the new representation;
5. **contract** only after old code and old data are gone.

`migrations check` sees pending pass guards and refuses a conflicting narrowing
migration. This keeps deployment ordering executable rather than a sentence in a runbook.

## 7. Roll back deliberately

Application rollback and database rollback are different actions. Prefer rolling the
application image back while leaving a compatible expanded schema in place. That is
why expand/contract matters.

When the database change itself must be reversed, use the artifact for the **most
recently applied** migration and code whose registry describes the intended rollback
state:

```bash
uv run wreath migrations down app:app \
  migrations/0002-project-state/migration.bin \
  --database main
```

An inverse that destroys data requires `--allow-destructive`. If live ORM code still
maps an object the downgrade would drop or retype, Wreath refuses. `--force` overrides
that final code/schema guard and should be reserved for a reviewed recovery where the
operator can explain why the running code is no longer authoritative.

After a downgrade, run `status` against the chain ending at the previous artifact and
exercise the application. Never hide `down` inside an automatic “deployment failed”
handler: a partially bad application rollout does not imply that reversing data is safe.

## CI and deployment gates

Use machine-readable output for automation:

```bash
uv run wreath migrations check app:app --database main --json
uv run wreath migrations status app:app migrations/*/migration.bin \
  --database main --json
```

Keep the shell glob ordered by a numeric directory prefix. The status command still
verifies the checksum chain, so a missing or misordered artifact fails rather than
quietly reporting a different tip.

See [PostgreSQL and models](data.md), [deployment](deployment.md), the deep
[migration architecture and fleet-upgrade guide](migrations.md), and the complete
[migration API](../reference/data.md).
