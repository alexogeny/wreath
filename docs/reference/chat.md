---
description: ChatOps runtime and Slack, Microsoft Teams and Discord provider APIs.
keywords: API reference ChatOps Slack Teams Discord federation durable agents
---

# ChatOps

`ChatOps` mounts verified provider ingress on a Wreath application. Commands share
one declaration, identity, authorization, durable-work and audit model while each
provider keeps its native protocol and outbound escape hatches.

The core has no provider SDK dependency. Slack signatures, Teams connector tokens,
Discord signatures, manifests, acknowledgements and outbound wire shapes are owned
by Wreath. A configured installation store supplies tenant credentials; a configured
`ExternalIdentityResolver` turns only a verified provider identity into an existing
Wreath principal. Profile names and email addresses never create an account link.

Commands that declare an authorization action refuse startup without an authorizer.
Durable commands require the existing job owner and a transactional inbox. The
default inline replay window is bounded and process-local; deployments with several
workers must supply a shared inbox for cross-worker deduplication.

Configure `PostgresWebhookInbox` with the same transaction factory used by the job
owner. Wreath then claims the provider delivery, inserts the job and records fenced
completion in one commit. A crash cannot leave a remembered delivery with no job.

```python
inbox = PostgresWebhookInbox(
    session_factory=inbox_transaction,
    lease_owner=replica_id,
    lease_seconds=30,
)

chat = ChatOps(
    app,
    name="operations",
    providers=(slack, teams, discord),
    jobs=jobs,
    inbox=inbox,
)
```

## One command, three providers

Add the provider objects to one `ChatOps` runtime, then declare the business action
once. Provider adapters verify and normalize ingress before the runtime binds typed
arguments, resolves an existing Wreath identity, applies second-factor and Cedar
requirements, and invokes the handler.

```python
chat = ChatOps(
    app,
    name="operations",
    providers=(slack, teams, discord),
    installations=installations,
    identity=identity_resolver,
    authorizer=authorizer,
    audit=audit,
)


@chat.command(
    "deploy",
    action="Release::deploy",
    resource="Release::environment",
    second_factor=300,
)
async def deploy(environment: Literal["staging", "production"]) -> ChatReply:
    return ChatReply.in_channel(f"Deploying {environment}")
```

Federation is deliberately lookup-only. Slack and Discord installation identities,
and Teams Entra identities, may resolve to a principal already owned by Wreath's
identity store. Provider profile fields do not provision or merge users. A missing,
ambiguous or mismatched link refuses the identity-dependent command.

`OrganizationFederation` can then bind an installation to the same organization
store SCIM provisions. It checks the linked Wreath identity's current membership on
every resolution, so removing a member through SCIM also removes ChatOps access; it
does not copy memberships into a chat-specific table.

```python
identity_resolver = ExternalIdentityResolver(
    store=external_identities,
    federation=OrganizationFederation(organizations, installation_organizations),
)
```

The ordinary `ConcurrencyPolicy` and `RateLimitPolicy` can protect ChatOps after
that identity and tenant have been resolved. The rate-limit key includes the tenant,
Wreath identity (or provider actor when unlinked), and command name. Both policies
keep their existing counters and stores.

```python
chat = ChatOps(
    app,
    name="operations",
    admission=ConcurrencyPolicy(32),
    rate_limit=RateLimitPolicy(limit=20, window=60),
)
```

## Durable agents

A durable handler receives the original `ChatContext`, including an `AgentRequest`,
the real job context and a provider emitter. `context.job_context.fence` is the owner
for application effects that must occur once across retries. Provider emitters only
perform their validated, idempotent replacement operation; arbitrary application
effects remain the handler's responsibility.

```python
@chat.command("investigate", execution="durable")
async def investigate(context: ChatContext) -> None:
    request = context.agent_request
    if request is None:
        raise RuntimeError("durable agent request is unavailable")
    async for event in agent.run(request):
        await context.emit(event)
```

Pass an existing `Streams` owner as `streams=` to retain the same fenced event
sequence for reconnecting web clients. ChatOps keeps the provider delivery as the
only producer: it joins that job's fence to the stream log instead of launching a
second job. `context.stream_key` identifies the resulting resumable stream.

Every completed or failed dispatch also emits a structured ChatOps outcome through
Wreath logging, which places it on the active Flight Recorder and OTLP
pipeline without a separate audit transport. An optional application audit store is
still appended for durable compliance records. `wreath doctor preflight` reports
missing providers, jobs, transactional inboxes, authorizers, and stream owners before
deployment.

Notification kinds can deliver through ChatOps with `wreath.notifications.Chat`.
The channel resolves a tenant-bound destination and derives a deterministic
idempotency key from the notification kind, recipient, tenant, and rendered content.

## Provider-neutral tests

`TestClient.chat()` drives the same compiled declarations without constructing a
Slack, Teams, or Discord payload. Each `ChatTurn` retains the reply, emitted agent
events, normalized context, and supplied arguments.

```python
transcript = TestClient(app).chat(
    "operations",
    provider="slack",
    installation="T1",
    tenant="slack:T1",
    actor="U1",
    conversation="C1",
)
turn = await transcript.command("deploy", environment="staging")
assert turn.reply.content == "Deploying staging"
```

`ChatOps.problem(error)` uses Wreath's RFC 9457 `ProblemDetail` vocabulary for
provider errors. Expected authorization and input failures retain their useful
detail; unexpected failures become a generic 500 detail rather than leaking an
exception message into chat.

Retained agent text is bounded before append: 40,000 characters for Slack, 28,000
for Teams and 2,000 for Discord. Slack delivery requires a validated `response_url`;
Teams limits connector destinations to its supported service origin; Discord binds
proactive destinations to the declared tenant. App-secret rotation invalidates
already queued Teams envelopes, so drain or replace those jobs during rotation.

::: wreath.chat

::: wreath.chat.slack

::: wreath.chat.teams

::: wreath.chat.discord
