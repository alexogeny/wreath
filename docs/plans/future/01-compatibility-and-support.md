# Compatibility and support contract plan

## Status

Future proposal; not approved for implementation.

## Objective

Define when applications can rely on Neo and how releases communicate compatibility, deployment support, and experimental protocol status. This is a prerequisite for production adoption and must describe evidence rather than turn pre-1.0 behavior into an accidental promise.

## Scope

- Public API stability and deprecation policy.
- Supported CPython, operating system, architecture, ASGI server, PostgreSQL, TLS, and optional native-extension matrix.
- Classification of stable, provisional, experimental, and private interfaces.
- Upgrade notes, schema requirements, and rollback boundaries.
- Published conformance and hardening status per server protocol.
- Security-reporting and supported-release policy.

## Proposed contract

Each public symbol and runtime mode belongs to one maturity class:

- **stable**: compatibility preserved for the documented support window;
- **provisional**: usable but may change with a migration note;
- **experimental**: explicitly unsuitable as a production dependency without workload-specific validation;
- **private**: no compatibility promise.

Release notes identify behavior changes, database schema changes, native ABI changes, removed deprecations, and known operational risks. A machine-readable compatibility document records exact supported combinations and the tests run against each.

## Implementation

Add version and feature metadata without request-time imports or discovery. Public capability inspection should return immutable startup data. Native modules expose their build ABI, enabled features, compiler mode, and protocol versions through bounded constants; the pure backend reports equivalent semantic capabilities without pretending native acceleration exists.

Do not use feature metadata to silently change application semantics. Unsupported explicit configuration fails at startup with an actionable error.

## Phases

1. Inventory current public exports and documented APIs.
2. Define maturity labels and deprecation durations for pre-1.0 and post-1.0 releases.
3. Add machine-readable runtime/build capability reporting.
4. Publish the tested deployment matrix and protocol evidence.
5. Add release-note and compatibility checks to CI.
6. Establish security support and end-of-life procedures.

## Verification

- Snapshot the public export surface and intentional changes.
- Test pure/native capability reports and explicit unsupported configurations.
- Validate wheel metadata and extension ABI metadata on supported builds.
- Run smoke applications on every claimed ASGI server and PostgreSQL version.
- Ensure documentation never infers production readiness from feature presence.

## Completion criteria

- A consumer can determine whether a particular Neo/Python/server/database combination is supported.
- Every public compatibility break has a migration path or explicit pre-1.0 notice.
- Experimental HTTP/2, HTTP/3, PostgreSQL, and native modes are labeled independently.
- Release automation rejects missing compatibility and migration metadata.

## Risks

A broad support matrix can consume more maintenance than implementation. Support only combinations exercised continuously, and distinguish community-reported compatibility from maintained compatibility.
