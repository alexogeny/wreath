# API lifecycle tooling plan

## Status

Future proposal; builds on typed binding and existing OpenAPI 3.1 generation.

## Objective

Make public HTTP APIs evolvable and consumable through richer schemas, compatibility checks, explicit version policy, and reproducible client generation while preserving Neo's own validation semantics.

## Scope

- Complete documentation of the schemas Neo can validate and generate.
- Stable operation IDs and component names.
- API versioning and deprecation conventions.
- Machine-readable compatibility diffing in CI.
- Webhook/callback descriptions and asynchronous protocol supplements.
- Pagination, idempotency, problem-detail, and correlation conventions.
- Generated-client fixtures and interoperability tests.

## Schema contract

OpenAPI output must derive from the same compiled binding metadata used at runtime. Unsupported type shapes fail or produce an explicit documented fallback; they must not generate a schema stricter than validation or imply validation that does not exist. Component naming is deterministic across process runs.

Route metadata adds explicit operation ID, deprecation, security, response schemas, examples, and visibility. Automatic naming remains available but collision is a startup error. The generated document is compiled/cached and immutable after route finalization.

## Compatibility tool

A CLI compares two normalized documents and classifies removed operations, narrowed inputs, widened required fields, changed security, response changes, and documented non-breaking additions. Policy is configurable but deterministic. Suppressions identify an owner, reason, and expiry.

This is an API contract tool, not a guarantee that every generated client language interprets OpenAPI identically.

## Asynchronous APIs

Ordinary OpenAPI does not adequately describe WebSocket or broker subprotocols. Define an optional separate protocol manifest or support an established asynchronous API document through an optional package. Do not overload HTTP route schemas with connection semantics.

## C and pure split

Generation and compatibility analysis are startup/CI work and should remain understandable Python unless profiling finds a specific parser or canonicalization bottleneck. Reuse native JSON only as an implementation detail with pure parity. No request hot path should change merely to generate richer documentation.

## Phases

1. Inventory binding/schema gaps and define exact correspondence rules.
2. Add deterministic operation/component identity and response metadata.
3. Add normalized schema export and compatibility CLI.
4. Publish versioning, pagination, idempotency, and error conventions.
5. Add client generation/interoperability fixtures.
6. Design asynchronous protocol documentation separately.

## Verification

Differentially test runtime acceptance against generated schemas for supported shapes. Snapshot deterministic documents, collision failures, security metadata, compatibility classifications, recursive/union types, and generated clients making real test requests.

## Completion criteria

Consumers can detect reviewed breaking changes before deployment, generated schemas do not promise behavior Neo lacks, and HTTP versus persistent-protocol contracts are documented in appropriate formats.
