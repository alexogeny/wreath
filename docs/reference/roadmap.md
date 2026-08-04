# Reserved and in-progress surfaces

Wreath prefers to reserve a name early so a feature can land without a later
breaking move. The modules once listed here have since shipped —
[`wreath.telemetry`](telemetry.md), [`wreath.recording`](recording.md), and
[`wreath.replay`](replay.md) are real APIs with their own reference pages.

What remains genuinely unfinished is listed below, so no page has to imply
more than exists:

| Surface | Status |
|---|---|
| Declaring infrastructure, and applying any of it | Not shipped. [`wreath.infra`](infra.md) *reads* an application and reports what it requires — databases, object stores, egress, the listener, the wreath tables each subsystem owns, and the environment keys a settings model needs supplied — and that is deliberately the whole of it. There is no typed resource declaration surface, no Terraform-JSON emission, no live-state introspection or diff, and no `apply`; `wreath infra infer` never opens a socket and never imports a cloud SDK, and two tests assert exactly that. An inference that is subtly wrong is worse than none, because it looks authoritative, so this stage emits a plan for a person to read rather than a stack to run. <!-- absent: wreath.infra.Stack --> |
| The rest of the MCP surface | [`wreath.mcp`](mcp.md) serves tools, resources and prompts, and the boundary in front of them: `initialize`, `ping`, `tools/*`, `resources/*` (including `subscribe`), `prompts/*` and cancellation over streamable HTTP; idle-bounded in-memory sessions; the `GET` server-to-client notification stream, one per session, carrying subscribed-resource changes and progress relayed from `wreath.progress` rather than from a second mechanism; `expose_routes` for turning selected existing routes into tools, through an explicit selector with no `all=True`, refusing a route with no docstring and carrying the route's own `AuthRequirement` through; `MCPAuth` for OAuth 2.1 protected-resource metadata, the RFC 6750 challenge naming it, and audience-bound verification that refuses a token minted for another resource; `@mcp.tool(action=...)` for per-tool Cedar authorization; `ToolRateLimit` and `MCPLimits` for the bounds; and one Flight Recorder marker per call with the arguments recorded under the existing redaction rules. **Sampling, elicitation, roots, completions and the stdio wrapper now ship too**: `sampling/createMessage` declared per tool with `@mcp.tool(sampling=...)` so it is Cedar-gated, throttled through the tool's own rate-limit bucket and recorded at the same Flight Recorder site as the call; `elicitation/create` whose requested schema is `derive_input_schema` over a dataclass's fields, whose answer is validated by the same binding layer and recorded under the same redaction, and which is declared per tool with `@mcp.tool(elicitation=...)` and gated, throttled and recorded exactly as sampling is -- a form renders inside a client UI the person already trusts, so a tool that asks for an API key is a phishing surface wearing that chrome, and being able to decline is the control social engineering is built to walk through; `roots/list` **enforced** rather than listed, confining `ToolContext.read_file` through `wreath._fsguard` beneath both `MCP(file_root=...)` and the client's declared roots; `completion/complete` answered from the values a prompt argument's `Literal`/`Enum` annotation already declared, with no second registry; and `wreath mcp stdio`, a byte relay driving the same ASGI app through the in-process transport rather than a second dispatch path. The correlation those need is bounded by `MCPLimits(max_pending_requests=..., client_request_seconds=...)`, a capability the client never advertised is refused rather than sent, cancelling a call cancels the question it asked, and ending a session fails every outstanding one. What is **not** built is `logging/setLevel`, stream resumption by `Last-Event-ID`, templated resources, and `listChanged` notifications for the three listings. Every method this revision defines but Wreath does not serve answers JSON-RPC `method not found` naming the stage it waits for, rather than failing obscurely. Dynamic client registration is not planned: it is an authorization-server endpoint, and the deployment's identity provider owns it. |
| Passkeys as a first factor | The authenticator-app factor ships, and so does step-up: two-phase enrolment, replay-proof verification, single-use recovery codes, a pending login that is not an identity, `second_factor_at` on the session, `wreath.auth.second_factor(max_age=...)`, `context.second_factor_age` for a Cedar policy, and `DELETE /auth/2fa/{id}` guarded by a fresh factor. **WebAuthn as a second factor ships too**: registration and assertion over `second_factor_router(rp_id=...)`, `none` attestation, ES256 and Ed25519 (an RS256 authenticator is refused by name), single-use challenges bound to the user who began the ceremony, cloned-authenticator detection from the signature counter, and the user-verification outcome recorded on the session as `second_factor_uv` — see [Second factors](../guides/second-factors.md). What is **not** built is passkeys as a *first* factor: discoverable credentials and usernameless login, where a failed ceremony has no password to fall back to. Two properties are stronger than they were: replay protection now survives concurrency, because `SecondFactorStore.touch` is a conditional advance that reports whether it won and a lost advance is refused as a replay, so two requests carrying one observed code cannot both complete; and mounting `second_factor_router` while forgetting `user_router(second_factors=...)` no longer signs a user in with a password alone, since the login refuses when the account has a factor it cannot check. One known gap in what does ship: the permission manifest does not model freshness, so a route behind `@second_factor` can read as permitted there and then answer 403 — the same optimistic-chrome property the manifest already documents. **A WebAuthn challenge is now single-use unconditionally**, which used to be the second gap here: ceremony state no longer rides in the session at all, but goes to a `ChallengeStore` whose default is real, and the consuming statement is one `DELETE ... RETURNING` whose `WHERE` carries the user binding — so a mismatched attempt matches no row and costs the rightful user nothing. The `UserWarning` that stood in for this is gone with it; it could only ever name half its condition, since whether `SessionMiddleware` has a `store=` is not knowable from inside the router, and a warning nobody can act on with certainty is one people learn to ignore. `challenges=PostgresChallengeStore(db)` is what a multi-worker deployment passes; the in-process default confines a ceremony to the worker that began it, which fails closed. |
| Trace context on ephemeral bus messages | Not shipped, and deliberately so. Durable publish carries the calling request's `traceparent` on its row, and the consumer runs its handler under it — the direct analogue of the durable queue. Ephemeral fan-out has no row: `pg_notify($1, $2)` carries the caller's payload *as* the message, so the only place a traceparent could go is inside that payload. That means wrapping every ephemeral message in an envelope, which is a breaking change to a live wire format between processes and needs a **versioned** envelope that a subscriber on the old build can still read through a rolling deploy — publishers and subscribers upgrade at different moments, and getting it wrong drops messages in both directions for the length of the deploy. The 8000-byte `NOTIFY` bound is *not* the obstacle; a traceparent is 55 bytes. Until it ships, `wreath doctor trace` names ephemeral messages as unsearched on every run, `tests/messaging/test_trace.py` pins the current wire format so a change has to be made on purpose, and an application that needs the causality can publish durably. The surface that would exist if this shipped is the versioned envelope itself, named here so the claim of absence is checkable rather than prose. <!-- absent: wreath.messaging.MessageEnvelope --> |
| Broader migration object coverage | `detect`/`generate`/`apply`/`down` cover tables, columns, primary keys (including composite), per-column and composite unique constraints, foreign keys with referential actions and deferrability, and single- and multi-column btree indexes (including unique indexes, and partial indexes whose predicate is built from `eq`/`one_of`/`all_of` over text, integer, and boolean columns, or from `is_null`/`is_not_null` over any column type). **Non-btree access methods and index-method options now ship**: `gin`, and pgvector's `hnsw` and `ivfflat`, each with an operator class (`index_ops=`) and `WITH (...)` options (`index_with=`), round-tripping against the catalog so a matching index is not rediscovered as drift. A declared *default* operator class round-trips too: PostgreSQL does not record that a default was named, so wreath reads this database's defaults per access method and normalises the declaration through them — `index_ops="vector_l2_ops"` on an `ivfflat` index is understood rather than rediscovered forever. See [Vector search](../guides/vector-search.md). Extension-typed columns come with them: a `vector(1536)` column is created, dropped, and *re-dimensioned* (a rewrite, emitted as one rather than skipped). **Stored generated columns ship too**: a `TsVector` column renders as `GENERATED ALWAYS AS (to_tsvector(...)) STORED`, in the normal form PostgreSQL deparses back, and is ordered after the columns its expression reads on the way up and before them on the way down — see [Full-text search](../guides/full-text-search.md). Changing an existing generated column's expression is *detected* (it is part of the column signature and the model fingerprint) but emitted as `MANUAL`, because rewriting one recomputes every row. Expression/covering indexes, partial predicates outside that vocabulary, rename hints, changing an existing index's operator class, and changing an existing foreign key's action are still being implemented (emitted as `MANUAL`); `CREATE EXTENSION` is deliberately never emitted, because the privilege is usually not the runner's. Keep Alembic for schemas that use the rest. |

The Native Flight Recorder is **not** on this list. Capture ships: the native
recorder arms, triggers, redacts, and writes `WFR1` — see
[`wreath.recording`](recording.md) for the policy surface and
`tests/test_flight_capture_live.py`, which drives an armed forensic request end
to end and reads the captured headers, query parameters, and bounded bodies back
out of the file. `wreath.orm.TenantContext` is not on this list either; a tenant
session binds its schema and role transaction-locally and executes.

Feature flags as Cedar context is **not** on this list either: a
`CedarAuthorizer(flags=...)` puts the caller's enabled flags in `context.flags`
as a set of names, resolved once per request, with a misspelled name refused at
startup — see [Auth](../guides/auth.md#feature-flags-in-a-policy). It shares one
known limit with the `@second_factor` note above, and for the same reason: the
permission manifest tags flag state into its `ETag`, so a conditional request
sees a flip, but no stream event announces one, so a manifest can read as
permitted and then answer 403. That is the optimistic-chrome property the
manifest already documents, in a second place rather than a new kind.

Organisation membership, roles within an organisation, and plan entitlements are
**not** on this list either. `CedarAuthorizer(organizations=..., entitlements=...)`
puts them in `context.organizations`, `context.org_roles` and
`context.entitlements` on the same machinery as flags and regions — resolved once
per request, resolved at all only when a policy names the key, empty and still
supplied when no provider is configured, and refused at startup for a name the
provider does not hold. See
[Organisations and delegation](../guides/organizations.md). They extend the same
optimistic-chrome limit into a *third* and *fourth* place, for the same reason:
the manifest tags membership and entitlement state into its `ETag`, so a
conditional request sees a change, but no stream event announces one — a
manifest can read as permitted after a member is removed or a plan is downgraded,
and the route then answers 403. **The list of things that move a manifest's
answer without announcing it is now roles, second-factor freshness, flags,
regions, memberships and entitlements**, which is worth saying plainly: the
manifest is chrome, and a decision behind one is a misuse of it whatever the
fact.

The packaging question that used to block SAML is settled: `wreath.xml` owns a
strict profile and Exclusive XML Canonicalization 1.0, so there is no
third-party XML-DSig dependency to take. [`wreath.saml`](saml.md) verifies an
assertion on top of it and hands back a `VerifiedAssertion` whose `facts()` is a
Cedar context mapping — it **decides nothing**, exactly as `wreath.signatures`
establishes for a signed request.

What is still absent is the *routing* half, and it is one row rather than a
paragraph: there is no assertion-consumer endpoint, no service-provider
metadata, no redirect/POST binding, and no `EncryptedAssertion` support. Like
`scim_router`, that half is an adapter onto `wreath.organizations` rather than a
second membership model.
<!-- absent: wreath.organizations.saml_router -->

SCIM shipped without three optional parts of RFC 7644, and
`ServiceProviderConfig` reports each of them as unsupported rather than leaving
a client to discover it: **sorting** (`sortBy` is answered 501 instead of in an
arbitrary order), **bulk operations**, and **`ETag` resource versions** — the
last because a resource's version would have to be computed from a modification
time neither store is required to keep, and a version that changes when nothing
did is worse than none. `/Me` is likewise absent: it is an alias for a resource
the caller can already address, and the identity behind a provisioning token is
the directory rather than a user.

`externalId` and the rest of RFC 7643 §4.1's optional user attributes —
`name.*`, `displayName`, `phoneNumbers`, `addresses` — are **not stored**, and
that is a model disagreement rather than a missing feature:
`wreath.users.UserRecord` has nowhere to put them, and giving SCIM a table of
its own would be precisely the second user store the adapter exists to avoid.
They are absent from the published `/Schemas`, and a *filter* naming one is
refused with 400 `invalidFilter` rather than answered with an empty page.
Closing this needs a decision about `wreath.users` — either a metadata column on
`UserRecord` or a store-level extension seam — and it belongs to that subsystem,
not to SCIM.

Two more SCIM limits are worth stating rather than discovering. A provisioning
request that touches both stores is **not one transaction**: `scim_router` orders
its writes so that every refusal it can raise happens before the organisation is
touched, but a user store that fails *after* a membership was written leaves the
two disagreeing, and neither seam offers a transaction to join. Making that
atomic means a unit of work spanning `UserStore` and `OrganizationStore`, which
is a change to those protocols rather than to SCIM. And a **filtered** list reads
one account per member of the organisation, because `wreath.users` has no batch
read; the ceiling (`scim_router(max_filter_scan=...)`) bounds that fan-out rather
than removing it, and removing it means a `get_many` on the user store, again in
that subsystem.

## Tenant-fleet DDL

This row has left the table: `wreath.migrations.apply_fleet` applies one
artifact across a fleet of tenant schemas, and `generate_single_plan(fleet=True)`
produces an artifact that can cross one.

Three things embedded the schema name and all three are gone. The **catalog
fingerprint** read `nspname` as the first column of every branch, so two
byte-identical tenants fingerprinted differently; `_FLEET_CATALOG_SQL` blanks
tenant-local names while keeping a foreign key into a *shared* schema named,
since that is a fact every tenant has in common rather than a difference between
them. The **desired image** came from a descriptor that wrote each spec's
schema; `_registry_descriptor(fleet=True)` writes it empty. The **DDL** was
schema-qualified; a zero-length schema now renders unqualified, and
`apply_fleet` binds `SET LOCAL search_path` per tenant so the statements land in
the right place — transaction-local, so nothing leaks onto a pooled connection's
next borrower.

A zero-length schema therefore *means* tenant template rather than being
malformed, and the three native parsers that refused it say so. The table name
is still required: an operation naming no relation is a corrupt tape however it
was produced.

The runner itself: a session advisory lock, so two deploys cannot interleave
per-tenant transactions and leave a fleet in a state neither artifact describes;
**one transaction per tenant**, because one spanning a thousand schemas holds
every lock for the length of the slowest; a tenant already at the target
**skipped rather than refused**, so a run that stopped at tenant 400 of 1000 is
finished by re-running instead of producing 399 errors that all mean "this one
is fine"; serial application, because concurrent DDL contends on shared catalog
rows and the failure it produces is a deadlock partway through a fleet; and a
per-tenant result rather than a pass rate, because a fleet run has no atomic
answer and the shape should not pretend otherwise.

Every tenant goes through the *same* guarded apply as a single-schema run — the
five refusals are the whole safety argument of a migration, and a fleet-only copy
would be exactly where a drift went unnoticed.

`tests/migrations/test_fleet_apply.py` proves it against a live server: one
artifact migrating three tenants into their own schemas, a re-run skipping them
all, a stopped run finished by re-running, the lock excluding a second runner,
and the rendered DDL naming no tenant at all.

## Cross-site request forgery for HTML form posts

`wreath.middleware.CSRFMiddleware` reads the resubmitted token from a request
**header**, which suits a script client that sets one and cannot work for a plain
HTML form — a form post carries no header. Mounting the middleware in front of a
server-rendered form does not defend it, it refuses it.

That gap is why [`wreath.admin`](admin.md) *requires* a `csrf=` verifier before it
will generate any write route, rather than shipping an unprotected escalation
path or growing a second CSRF implementation beside the one that exists. What is
missing is small and well-shaped: a configured form-field name that the
middleware reads when the request carries a form content type, alongside the
header it reads today. When that lands, the admin's `csrf=` becomes a one-line
pointer at it and this row leaves the page.

## Bulk actions in the admin

The generated admin deletes one row at a time, behind a confirmation page. A
"delete selected" affordance is the feature every generated admin eventually
grows, and the reason it is not here yet is that the invariant it has to satisfy
is the interesting part: a bulk action must be audited as **one attributable
event**, not N row events, and `wreath.audit_log` records per row from the ORM's
own write path. Reconciling those two is a design question about the audit trail
rather than a screen to draw, so it waits for someone to answer it deliberately.
<!-- absent: wreath.admin.bulk_action -->

## `series` charts in the admin

The generated admin renders lists, detail pages and forms, and draws no charts.
An application that already declares a [`wreath.series`](series.md) query has
nowhere to put its result inside the admin, and reads it from a route of its own
instead. Nothing here is blocked on a decision; it is simply unbuilt.
<!-- absent: wreath.admin.chart -->

## OAuth issuance

Wreath *verifies* OAuth bearer tokens — `MCPAuth` publishes protected-resource
metadata, answers with the RFC 6750 challenge naming it, and refuses a token
minted for another audience — and it mints none. There is no authorization
server: no authorization-code or client-credentials endpoint, no token or
introspection endpoint, and no client registry. That is the same division the
MCP row states for dynamic client registration: issuance belongs to the
deployment's identity provider, and taking it on is a decision nobody has made
rather than a gap in an existing surface.
<!-- absent: wreath.oauth -->

## Quota usage as a recorded event

[`wreath.quota`](quota.md) meters and refuses, and reports what it did through
its headers and its store. It writes nothing to [`wreath.log`](log.md), so there
is no ordered, `(xid, seq)`-addressable record of what a tenant consumed — the
shape a billing or usage export wants. Closing this makes `wreath.quota` a
caller of the log rather than a second recorder, which is why it is written down
here instead of being solved twice.
<!-- absent: wreath.quota.UsageEvent -->

## Named template fragments

`wreath.templates` compiles and renders whole templates. Rendering a **named
fragment** with its own `ETag` and `Vary` is what attribute-driven partial
updates (htmx and its neighbours) need, and it was expected to be the admin's one
genuine addition. It turned out not to be needed: full-page server-rendered forms
need no fragment, and shipping no JavaScript is what lets the admin send
`default-src 'none'` rather than the permissive policy an inline-script page would
require. The capability is still worth having for applications that do want
partial updates; it simply has no caller inside wreath yet, and `_livedoc`'s
discipline says to wait for one.

A second surface is unbuilt in the same by-absence way, and it belongs to
[query subscriptions](sync.md). A reconnecting client is sent a fresh
`snapshot` rather than resuming from where it left off. That is *correct* — the
snapshot's key set is authoritative, so applying it drops every row the client
has since lost, which is the tombstone rule applied wholesale for the price of
one bounded query. What it is not is cheap for a client on a slow link that
reconnects often.

Resuming instead needs a **row-grained change feed appended inside the writing
transaction**: a feed appended after commit can be lost by a crash in the gap,
and a feed appended before it describes writes that rolled back. The only place
that append can go is the ORM session's write path, which is exactly where
[the audit trail](audit_log.md) already sits — so this is one hook growing a
second caller rather than a second hook, and `wreath.log`'s `(xid, seq)` cursor
is already the right resume token. Until it lands, a shape's `limit` bounds what
a reconnect costs, which is the reason the bound is mandatory.
<!-- absent: wreath.sync.resume -->

When one of these ships, its row leaves this page and its reference page tells
the full story.
