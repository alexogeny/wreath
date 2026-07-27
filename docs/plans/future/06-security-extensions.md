# Security extensions plan

## Status

Future proposal; supplements existing bearer authentication, route authorization, Cedar, sessions, CSRF, and browser-security middleware.

## Objective

Provide production integration points for human identity, service identity, long-lived connections, signed callbacks, credential rotation, and immutable audit without putting vendor SDKs or mandatory cryptography dependencies in Neo core.

## Scope

- OIDC/OAuth2 token validation and JWKS lifecycle.
- Service credentials, API keys, and signed requests/webhooks.
- Mutual-TLS identity supplied by trusted server/proxy boundaries.
- Credential reload, revocation, and rotation.
- Tenant/security context propagation.
- Authentication renewal for persistent sessions.
- Structured authorization and administrative audit events.

## Architecture

Core defines credential extractors, verifier protocols, cache contracts, identity mapping, challenge construction, and audit hooks. Optional packages implement OIDC discovery, JOSE algorithms, platform secret stores, and certificate watchers. Existing route requirements continue to compile at startup; dynamic verification changes identity attributes, not route structure.

JWKS caches have bounded key counts, explicit refresh deadlines, stale-key policy, single-flight refresh, issuer/audience/algorithm allow-lists, and failure-closed defaults. Unknown key identifiers may trigger one bounded refresh but never unbounded attacker-controlled fetching.

API-key verification stores derived/verifier material rather than raw keys where possible and uses timing-safe comparisons. Signed requests bind method, authority, path, timestamp, nonce, and a payload digest under a documented canonicalization scheme.

## Persistent connection security

A connection records immutable authentication evidence and a replaceable current authorization context. Renewal has an explicit protocol and deadline. Expiration never silently preserves privileges. Revocation propagation, forced disconnect, and in-flight operation behavior are policy choices surfaced to applications.

## C and pure split

C may accelerate strict token segment parsing, base64url, canonical byte construction, timing-safe comparison, bounded key lookup, certificate fingerprint parsing, and replay-cache primitives. Python owns key retrieval, cryptographic provider calls, identity mapping, policy, network I/O, and callbacks. Pure twins implement every accelerated byte/state primitive.

Do not implement novel cryptography. Optional proven providers remain responsible for signature algorithms and certificate verification.

## Audit contract

Audit events include actor, authentication method, tenant/context, action, resource, decision, policy identifier/version, correlation/trace IDs, timestamp, and outcome. Secrets and full tokens are forbidden. Audit delivery can use the reliable outbox plan when durability is required.

## Phases

1. Threat model and verifier/audit contracts.
2. Optional OIDC/JWKS integration with bounded caching.
3. Service API keys and signed callback requests.
4. Trusted mTLS identity integration and rotation.
5. Persistent-session renewal and revocation hooks.
6. Durable audit adapter and security review.

## Verification

Test algorithm confusion, issuer/audience mismatch, unknown keys, refresh storms, stale caches, rotation, clock skew, replay, canonicalization ambiguity, proxy spoofing, expiration during persistent sessions, audit redaction, and failure-closed behavior.

## Completion criteria

No client-supplied identity attribute is trusted without a configured verifier, credential rotation does not require process restart where advertised, and every privileged control action can produce a secret-free audit record.
