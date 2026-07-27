# Operational configuration plan

## Status

Future proposal; extends the constrained dotenv loader without turning it into shell emulation.

## Objective

Provide typed startup configuration, secret-provider integration, validated runtime-operational settings, safe diagnostics, and controlled reload while preserving explicit ownership and avoiding hidden mutable global state.

## Configuration classes

Applications declare dataclasses or another Neo-native typed shape. Loading composes explicitly named sources in a declared precedence order. Validation uses Neo's existing type machinery where semantics fit and reports all startup errors with source locations while redacting secret values.

Sources may include process environment, explicit dotenv files, files mounted by an orchestrator, and optional provider adapters. No default file is guessed. Variable expansion, command execution, and shell syntax remain unsupported.

## Secrets

A `Secret` wrapper prevents representation, structured logging, and accidental serialization. Optional adapters retrieve values from platform stores and own authentication/refresh. Secret bytes are copied only where required by downstream APIs; Neo cannot guarantee erasure of immutable Python objects and must not claim it.

Credential and certificate reload uses supervised providers. Consumers subscribe through explicit callbacks or versioned snapshots. Reload is atomic from a consumer's perspective, failure retains the last valid value where policy permits, and health reports staleness without exposing content.

## Dynamic operational settings

Only settings explicitly declared reloadable may change after startup. Updates pass type and cross-field validation, authorization, audit, version comparison, and optional dry-run before atomic publication. Examples include concurrency ceilings, polling intervals, or load-shedding thresholds; route structure, model declarations, and arbitrary code paths remain startup-only.

Readers receive immutable versioned snapshots. A request or control iteration can retain one snapshot to avoid observing half an update. Rollback creates a new audited version rather than mutating history.

## C and pure split

Native code may accelerate strict environment parsing, typed scalar parsing, bounded key lookup, immutable snapshot storage, and change-set comparison. Python owns source adapters, provider network I/O, validation graphs, callbacks, policy, and audit. Pure twins produce identical values, errors, precedence, redaction, and versions.

## Phases

1. Define typed settings, source provenance, redaction, and precedence.
2. Implement immutable startup snapshots and diagnostics.
3. Add optional secret-provider protocol and supervised refresh.
4. Add versioned reloadable subsets with audit and rollback.
5. Add native parsing/snapshot primitives after profiling.
6. Publish deployment recipes for files, environment, and orchestrated secrets.

## Verification

Test malformed input, conflicting sources, absent required values, secret representation/serialization, provider outage, stale refresh, concurrent readers, rejected update, callback failure, rollback, cancellation during reload, and pure/native parity. Logs and exception snapshots are scanned for supplied secret canaries.

## Completion criteria

Applications can explain each effective value's source without revealing it, secret/config rotation has explicit health and failure behavior, readers observe complete immutable versions, and runtime updates are bounded, authorized, validated, and audited.

## Risks

Dynamic configuration can become a second control plane. Keep the reloadable surface small and prefer durable application state for business policy that requires workflow, approval, or historical queries.
