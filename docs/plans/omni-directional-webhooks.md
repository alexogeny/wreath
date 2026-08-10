# Prescriptive plan: omni-directional webhooks

Status: ready after the managed outbound HTTP client proof point

Related material:

- `AGENTS.md`
- `repo-map.md`
- `docs/agents/manifest.json`
- `docs/plans/native-outbound-http-client.md`
- `docs/plans/background-tasking.md`
- `wreath.services`
- `wreath.telemetry` / `wreath.logging`
- `wreath.auth` / `wreath.authorization`
- `wreath.jobs`
- `wreath.webhooks`

## Goal

Provide one explicit webhook subsystem for inbound verification, outbound signed delivery, safe inbound-to-outbound relaying, and optional PostgreSQL inbox/outbox guarantees. Build on Neo's existing routing, request binding, JSON codec, security middleware, PostgreSQL ownership, supervised services, and the managed HTTP client in `docs/plans/native-outbound-http-client.md`. Keep cryptography and delivery policy in safe Python while receiving native augmentation from ingress parsing, JSON encoding, outbound serialization, bounded replay structures where measured, and the native HTTP client.

## Fixed scope and terminology

**Omni-directional** means:

- inbound receipt, signature verification, replay protection, decoding, and dispatch;
- outbound encoding, signing, HTTP delivery, retry, and outcome tracking;
- relay into a separately identified outbound event with correlation and causation metadata;
- optional durable inbox/outbox behavior across process restarts.

It does not mean exactly-once remote effects, a bidirectional socket protocol, arbitrary callback URLs, automatic forwarding of trust context, or a generic broker abstraction.

Direct delivery is process-local and caller-owned. Durable delivery is explicitly PostgreSQL-backed and supervised. Response background tasks are not a durable dispatcher.

## Repository constraints

- Keep `src/neo` dependency-free and Python 3.14-first.
- Compile inbound sources into normal Neo routes so portable ASGI behavior, request limits, exception ownership, middleware, and observability remain intact.
- Use the named managed client from `docs/plans/native-outbound-http-client.md`; do not create a webhook-only socket stack or pool.
- Use stdlib `hmac` and `hashlib` for the first signature profile. Do not implement cryptographic algorithms in C.
- Sign and verify exact body bytes before decoding or coercion.
- Keep replay stores, queues, concurrency, retries, history, and retained payloads bounded.
- Use Neo's PostgreSQL stack for durable inbox/outbox state; do not add SQLAlchemy or a parallel persistence layer.
- Run dispatchers as explicit supervised application services with observable startup, readiness, failure, cancellation, and shutdown.
- Promise at-least-once dispatch where durable mode is enabled, never exactly-once remote effects.

## Public model

Add `src/neo/webhooks.py` with explicit hubs, sources, destinations, envelopes, contexts, signers/verifiers, result types, and replay stores:

```python
hooks = app.webhooks("partners")
```

### Envelope

```python
@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    id: str
    type: str
    version: str
    timestamp: datetime
    content_type: str
    body: bytes
    correlation_id: str | None = None
    causation_id: str | None = None
    ordering_key: str | None = None
```

The envelope contains exact transmitted body bytes. Store timestamps as immutable UTC instants and serialize them in one fixed RFC 3339 UTC form. Display timezone changes must never mutate stored history.

### Inbound source

```python
github = hooks.source(
    "github",
    path="/webhooks/github",
    verifier=HMACWebhookVerifier(
        keys=key_provider,
        signature_header="x-hub-signature-256",
        timestamp_header="x-webhook-timestamp",
        event_id_header="x-github-delivery",
        max_age=300.0,
    ),
    replay=LocalReplayStore(max_entries=100_000, ttl=600.0),
    limits=WebhookLimits(max_body_bytes=1_000_000),
)

@github.event("push", payload=PushEvent)
async def receive_push(context: WebhookContext, event: PushEvent) -> None:
    ...
```

Provide an explicit CSRF exemption predicate for configured signed webhook routes rather than weakening global CSRF defaults.

### Outbound destination

```python
crm = hooks.destination(
    "crm",
    client=partners,
    path="/callbacks/orders",
    signer=HMACWebhookSigner(keys=key_provider, key_id="2026-07"),
    retry=WebhookRetryPolicy(attempts=6),
)

result = await crm.send(
    "order.updated",
    payload,
    event_id=event_id,
)
```

`send()` waits for a direct delivery workflow and returns a structured `WebhookDeliveryResult`. It is non-durable and follows caller cancellation.

Durable intent is separate:

```python
await crm.enqueue(
    session,
    "order.updated",
    payload,
    event_id=event_id,
)
```

`enqueue()` writes exact payload bytes and delivery metadata in the caller's current PostgreSQL transaction. It performs no network traffic before commit.

## Signing profile

Start with a versioned HMAC-SHA256 profile:

```text
neo-v1\n
<timestamp>\n
<event-id>\n
<event-type>\n
<raw-body>
```

Standard Neo headers:

```text
Neo-Webhook-Id
Neo-Webhook-Type
Neo-Webhook-Version
Neo-Webhook-Timestamp
Neo-Webhook-Key-Id
Neo-Webhook-Signature
Neo-Correlation-Id
Neo-Causation-Id
```

Rules:

- sign the exact bytes transmitted;
- include signature profile/version and key ID;
- use fixed timestamp canonicalization and bounded clock skew;
- compare signatures with `hmac.compare_digest`;
- support current and previous keys during explicit rotation windows;
- reject unknown key IDs without attacker-controlled remote fetching;
- cap signature/header count and length;
- never log keys or complete signatures;
- return stable external errors without revealing which verification sub-check failed;
- implement provider-specific formats as adapters over the same raw-body, key, timestamp, and replay contracts.

Do not add C cryptography. Existing native request parsing, JSON encoding, and outbound HTTP serialization augment both directions. A native signature-base builder or signature-header parser is eligible only after ablation shows whole-request value.

## Inbound processing order

Every source follows this order:

1. Match configured route and method.
2. Enforce header count/bytes and raw-body limits.
3. Read the complete bounded raw body.
4. Parse required event and signature metadata.
5. Validate timestamp and select a configured key.
6. Verify the signature over exact bytes.
7. Claim `(source, event_id)` in the replay/inbox store.
8. Decode and validate the declared payload type.
9. Invoke the application handler.
10. Commit durable inbox state with application side effects where configured.
11. Return the configured minimal acknowledgement.

Verification failures never reach handlers. Decode failures occur only after authenticity is established. External failures use stable `400`, `401`, `409`, `413`, or `415` behavior while retaining secret-free structured diagnostics internally.

A source must define acknowledgement behavior for success, duplicate success, active duplicate, prior terminal failure, invalid payload, and handler failure. Avoid returning arbitrary exception details to senders.

## Outbound delivery behavior

The destination:

1. validates event ID/type/version and payload limits;
2. serializes the payload exactly once;
3. creates the immutable envelope;
4. signs the final bytes and metadata;
5. sends through its named HTTP client;
6. classifies the response or transport uncertainty;
7. retries only under method, idempotency, body replayability, deadline, and destination policy;
8. returns or persists the structured outcome.

Use the event ID as idempotency metadata. A destination may configure accepted success statuses rather than assuming only `200`.

A timeout after request bytes may have reached the receiver is an uncertain outcome. Direct mode returns that uncertainty. Durable mode records `unknown` and retries/reconciles only under declared idempotency policy.

## Relay behavior

A relay creates a new outbound envelope:

- assign a new outbound ID;
- preserve inbound correlation ID or initialize it from the inbound ID;
- set `causation_id` to the inbound event ID;
- copy only application-selected payload values;
- do not forward arbitrary headers, credentials, signatures, IP identity, authorization results, or tenant claims;
- require explicit tenant/source-to-destination mapping across trust boundaries;
- prevent accidental source/destination loops through configured hop or causation checks.

Relaying into durable mode inserts the outbound intent in the same transaction as durable inbound handler side effects where the application requests that guarantee.

## Replay stores

### Process-local store

```python
LocalReplayStore(max_entries=100_000, ttl=600.0)
```

Requirements:

- bounded key count and key length;
- deterministic expiry;
- explicit duplicate/claimed/completed states;
- visible eviction and saturation counters;
- no claim of protection across replicas or restart.

A native implementation may own a compact hash table, expiry heap/ring, and transition validation only after measuring the Python store. Pure/native transition, expiry, eviction, and snapshot behavior must match.

### PostgreSQL inbox

Use existing Neo PostgreSQL schema facilities; return schema SQL and never auto-apply it. Required logical fields:

```text
source
message_id
payload_version
payload_hash
state
lease_owner
lease_expires_at
fencing_token
received_at_utc
completed_at_utc
result_status
failure_code
failure_summary
retention_until_utc
```

Enforce unique `(source, message_id)`. The claim, application side effects, and completion share one transaction in durable mode.

Duplicate policy:

- prior success returns the configured acknowledgement;
- prior terminal failure returns a stable configured response;
- active processing returns conflict/retry guidance;
- stale processing may be reclaimed only with an incremented fencing token.

Retention/purge is explicit because deleting inbox rows weakens replay protection.

## PostgreSQL outbox

Required logical fields:

```text
delivery_id
event_id
destination
event_type
payload_version
payload_bytes
content_type
signature_profile
key_id
state
attempts
next_attempt_at_utc
lease_owner
lease_expires_at
fencing_token
idempotency_key
ordering_key
correlation_id
causation_id
last_response_status
last_failure_code
last_failure_summary
created_at_utc
last_attempt_at_utc
completed_at_utc
retention_until_utc
```

Baseline states:

```text
pending -> leased -> sending -> delivered
                         |       retry_wait
                         |       failed
                         |       cancelled
                         +-----> unknown
```

A committed intent is recoverable. Dispatch is at least once. Loss after send and before acknowledgement persistence is represented as `unknown`.

Payload bytes, IDs, signature profile, and selected key ID are immutable after enqueue. Key retention must cover the maximum delivery horizon, or a policy-authorized re-sign transition must be explicitly recorded.

Retries use bounded exponential backoff with jitter and clamp `Retry-After`. Destination/global concurrency, attempts, retained response summary, and delivery history are bounded. Ordering keys serialize only their configured partition, not the entire destination.

## Supervision and shutdown

Durable dispatchers use `wreath.services`:

- no raw untracked `asyncio.create_task()`;
- readiness reflects critical dispatcher/client state;
- claims and in-flight deliveries are bounded;
- shutdown stops new claims and drains or releases owned leases to a deadline;
- stale workers cannot renew/finalize without the current fencing token;
- permanent failure is retained and affects readiness according to policy;
- no child task, database connection, HTTP response, or lease remains owned after shutdown.

Response-bound `BackgroundTask` is suitable only for explicitly non-durable best-effort work and must not back the outbox dispatcher.

## Security, tenancy, and audit

- Inbound identity comes only from the configured verifier and source, never payload claims alone.
- Tenant mapping is explicit and validated before handler side effects.
- Outbound destination URLs are startup-configured and inherit the HTTP client's SSRF/TLS policy.
- Redirects default off; redirecting invalidates the original webhook signature target policy.
- Payload bodies, keys, signatures, authorization headers, and unrestricted response bodies are excluded from logs and metrics.
- Audit records may contain event/delivery ID, configured source/destination, signature profile/key ID, attempt, outcome, correlation/causation, and tenant mapping decision.
- Audit timestamps are immutable UTC instants and display in a sortable timezone-permanent form.
- Audit retention and webhook payload retention are configured separately.

## Observability

Expose bounded metrics/events for:

- inbound verification outcome, replay hits, decode failures, handler result, and acknowledgement status;
- outbound queue depth/age, attempts, delivered/failed/unknown outcome, and drain duration;
- inbox claims, duplicates, stale recovery, and retention purge;
- destination HTTP pool wait/connect/TLS/request duration through the shared client;
- dropped diagnostics and bounded-store saturation.

Configured source/destination, signature profile, bounded outcome, attempt bucket, and status may be metric dimensions. Event IDs, tenant IDs, signatures, payloads, and untrusted hostnames are not labels.

## Correctness rules

- Signature verification covers exact transmitted bytes and precedes decoding.
- Event IDs, payload bytes, timestamps, signature profile, and durable intent metadata are immutable.
- Required/optional/nullable payload semantics reuse Neo binding/validation.
- Local replay protection is process-local only; PostgreSQL uniqueness provides cross-replica deduplication.
- Durable inbox side effects and completion share a transaction.
- Durable outbox provides at-least-once dispatch, never exactly-once effects.
- Unknown transport outcomes remain unknown until idempotency/reconciliation resolves them.
- Tenant and authorization context never cross boundaries implicitly.
- Redirects, retries, and key rotation cannot silently change signed intent.
- Queues, replay state, concurrency, retries, histories, payloads, and diagnostics remain bounded.
- No cryptographic algorithm is hand-written in C.

## Test-first work

### Direct inbound/outbound proof point

- [x] Add envelope, source, destination, context, result, signer, verifier, and limits contracts.
- [x] Compile inbound sources into normal Neo routes and expose a narrow CSRF exemption predicate for registered POST paths.
- [x] Test exact-body HMAC signing, timestamp windows, current/previous key rotation, missing required headers, and signature rejection.
- [x] Add an instrumented assertion that invalid signatures traverse the constant-time comparison path.
- [x] Test verification-before-handler dispatch and webhook-specific bounded raw-body rejection.
- [x] Add local replay claims, duplicates, completion, expiry, and saturation tests.
- [x] Add direct outbound delivery through a named client.
- [x] Add relay API/tests for new IDs and correlation/causation propagation.
- [x] Add explicit relay no-authorization/cookie leakage tests.
- [x] Add a signed, bounded relay path with duplicate-hop and maximum-hop rejection for direct and durable relays.

### Durable inbox/outbox

- [x] Define explicit outbox schema SQL through the current session/raw-query boundary; do not auto-apply it.
- [x] Add/test inbox claim, duplicate/active/failed classification, stale lease reclamation, completion, and fencing through the caller session.
- [x] Add bounded, `SKIP LOCKED` retention purge operations for terminal inbox/outbox rows.
- [x] Integrate inbox claim, typed handler side effects through `context.session`, and fenced completion in one explicit caller-session transaction.
- [x] Test outbox enqueue in the caller transaction and immutable payload metadata.
- [x] Add supervisor-compatible `run_once` claim, fenced sending/delivered/retry/unknown/failed transitions, and bounded backoff.
- [x] Add a supervisor-owned long-running loop with bounded idle waits, cancellation propagation, and explicit stopping.
- [x] Add separate-session in-flight lease renewal, readiness state, and Neo lifespan management as the planned supervisor boundary.
- [x] Inject claim, pre-send persistence, uncertain send, remote-acceptance/ack-loss, rollback, and restart/reclaim failures; add real PostgreSQL process-loss coverage gated by `NEO_TEST_POSTGRES_DSN`.
- [x] Add stale-fence tests and a real four-replica `SKIP LOCKED` contention test gated by `NEO_TEST_POSTGRES_DSN` (unavailable locally; see `benchmark-results-webhooks/postgres-unavailable.json`).

### Optional native webhook structures

- [x] Isolate local replay and signature-base construction with A/A samples in `benchmark-results-webhooks/native-decision.json`.
- [x] Do not add a native replay table: measured signature-base/replay costs are about 0.26/1.51 µs while signing/verifying cost about 4.24/7.18 µs, and C cannot own the asyncio lock or HMAC; retain Python and add a 2,000-step deterministic transition/expiry model test.
- [x] Keep signing, key retrieval, database transactions, tenant mapping, and user callbacks in Python.
- [x] Run native complexity plus full-project native memory/error/GIL gates; attempt free-threaded import parity and retain the unsupported-build result in `benchmark-results-webhooks/free-threaded-unavailable.json`.

## Benchmark plan

- [x] Add `benchmarks/bench_webhooks.py` with exact-body HMAC sign/verify and bounded local replay measurements plus integrity checks and raw samples.
- [x] Retain a small development baseline at `benchmark-results-webhooks/baseline-small.json`.
- [x] Add A/A calibration, real PostgreSQL inbox/outbox and queue-drain workload, dispatcher outcome/backlog, RSS, and end-to-end delivery-count validation; retain local dispatcher evidence and explicit PostgreSQL-unavailable evidence.
- [x] Optimize inbound processing from interleaved whole-route ablations: normalize headers once, decode signed identity fields once, scan header limits in one pass, and compile payload validators at registration. `benchmark-results-webhooks/inbound-optimized-final.json` resolves normalization and validation deltas above its measured A/A floor.

Add `benchmarks/bench_webhooks.py` with:

- unsigned inbound control;
- HMAC-verified inbound;
- verified inbound plus local replay claim;
- verified inbound plus PostgreSQL inbox;
- direct signed outbound on a reused connection;
- new-connection signed outbound control;
- durable outbox enqueue;
- dispatcher success, `429`, `503`, timeout, unknown, and retry workloads;
- inbound-to-durable-outbound relay.

Measure signature/base construction, JSON encoding, request/client transport, replay claim, database transaction, handler, acknowledgement, dispatcher queue, and total delivery separately where possible. Use ablation rather than cProfile for hot-path decisions.

Validate work counts:

- authenticated inbound IDs equal claimed/completed inbox rows under duplicate policy;
- receiver-observed IDs account for delivered and duplicate outbound attempts;
- outbox pending/delivered/failed/unknown totals account for every committed intent;
- shutdown loses no owned delivery silently.

Report response latency and delivery completion/backlog/drain separately. Retain repeated raw trials, A/A noise, environment/client/server/database metadata, errors, median/p95/p99, throughput, RSS, queue depth, attempts, and unique/duplicate counts.

## Likely files touched

```text
src/neo/webhooks.py
src/neo/app.py
src/neo/__init__.py
src/neo/postgres/
src/neo/orm/
tests/test_webhooks.py
tests/test_app.py
tests/postgres/test_webhook_inbox.py
tests/postgres/test_webhook_outbox.py
benchmarks/bench_webhooks.py
benchmarks/README.md
docs/guides/webhooks.md
docs/reference/webhooks.md
`wreath.auth` / `wreath.authorization`
`wreath.webhooks`
docs/agents/manifest.json
repo-map.md
```

Extend the PostgreSQL schema/session modules discovered during implementation; do not create a parallel database layer.

## Out of scope

- Exactly-once remote effects.
- Arbitrary user-controlled callback URLs by default.
- Automatic identity, tenant, authorization, cookie, or signature forwarding.
- Hand-written C cryptography.
- Durable dispatch through response background callbacks.
- Unbounded replay, retries, redirects, queues, payloads, or histories.
- Vendor business schemas in Neo core.
- Automatically applying PostgreSQL schema.
- General broker/messaging abstraction.

## Acceptance checks

- A configured inbound source verifies a bounded exact body before decoding and blocks stale, invalid, and duplicate events according to its store.
- A configured outbound destination signs the exact transmitted bytes and uses stable event/idempotency metadata.
- Direct delivery reports uncertain outcomes without claiming durability.
- Relay creates a new event ID, preserves correlation/causation, and forwards no implicit trust context.
- Local replay behavior is bounded, observable, and documented as process-local.
- PostgreSQL inbox deduplicates `(source, message_id)` transactionally with handler side effects and fenced stale recovery.
- PostgreSQL outbox preserves committed intent, exposes retry/failed/unknown states, and never claims exactly-once effects.
- Dispatcher shutdown leaves no untracked tasks, borrowed resources, live leases, or silently abandoned deliveries.
- Stored timestamps are immutable UTC instants and audit output is sortable/timezone-permanent.
- Logs, metrics, health, exceptions, and audit records contain no secrets, full signatures, authorization headers, or unrestricted payloads.
- Benchmark artifacts validate work counts and include repeated trials, A/A noise, environment metadata, errors, percentiles, throughput, backlog, drain, and memory.
- Any webhook-specific native work clears whole-request noise and has pure/native parity.
- Focused/default/full tests, Ruff, ty, PostgreSQL tests, native lints where applicable, and strict docs build pass.
