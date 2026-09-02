---
description: A compact map of Wreath's public modules, grouped by the problem each one owns.
keywords: API reference modules capabilities map Wreath surface
boost: 1.3
---

```hero
eyebrow: Reference · the public surface
title: Find the owner of the problem.
lede: Wreath modules are named after the job they do. Start from the boundary in your system, then follow the owning module into its API.
signal: dependency-free core
signal: explicit ownership
signal: startup compilation
action: Build the first route -> ../start/index.md
action: Choose a story -> ../stories/index.md
```

## Request path

| Need | Module |
|---|---|
| application and lifespan | `wreath.app` |
| routing | `wreath.router` |
| typed inputs and dependencies | `wreath.binding` |
| request and response objects | `wreath.request`, `wreath.response` |
| middleware and first-class policy | `wreath.middleware`, `wreath.policy` |
| OpenAPI and client generation | `wreath.openapi`, `wreath.typegen` |
| templates and static assets | `wreath.templates`, `wreath.staticfiles` |

## Identity, policy and tenancy

| Need | Module |
|---|---|
| identities and authentication | `wreath.auth` |
| Cedar authorization | `wreath.authorization` |
| users, sessions and second factors | `wreath.users`, `wreath.session_store` |
| organisations and membership | `wreath.organizations` |
| tenant resolution and isolation | `wreath.tenancy` |
| SAML, OIDC and provider flows | `wreath.saml`, `wreath.sso` |
| SCIM users and groups | `wreath.organizations.scim_router` |
| quotas and entitlements | `wreath.quota` |
| platform support operations | `wreath.platform` |

## Data and analysis

| Need | Module |
|---|---|
| PostgreSQL protocol and pooling | `wreath.postgres` |
| models, queries and sessions | `wreath.orm` |
| schema change | `wreath.migrations` |
| safe SQL and stores | `wreath.sql`, `wreath.store` |
| time zones, buckets and recurrence | `wreath.temporal` |
| aggregation and chart projection | `wreath.series` |
| geographic values and predicates | `wreath.geospatial` |
| bounded client synchronization | `wreath.sync` |
| object storage and resumable uploads | `wreath.objects` |
| artifact attestation | `wreath.provenance` |

## Realtime and durable work

| Lifetime | Module |
|---|---|
| one WebSocket connection | `wreath.websocket` |
| a live room | `wreath.rooms` |
| cross-worker messages | `wreath.messaging` |
| resumable output | `wreath.streams` |
| visible task status | `wreath.progress` |
| a durable attempt | `wreath.jobs` |
| several durable steps | `wreath.workflows` |
| one live owner of an entity | `wreath.entity` |
| user-facing delivery | `wreath.notifications` |

## AI and protocols

| Need | Module |
|---|---|
| MCP tools, resources and prompts | `wreath.mcp` |
| governed model agents, backplanes and tool execution | `wreath.agents` |
| Slack, Teams and Discord ChatOps | `wreath.chat` |
| GraphQL | `wreath.graphql` |
| gRPC | `wreath.grpc` |
| Protocol Buffers | `wreath.protobuf` |
| OAuth authorization server | `wreath.oauth` |
| HTTP message signatures | `wreath.signatures` |

## Boundaries and delivery

| Need | Module |
|---|---|
| outbound HTTP | `wreath.http_client` |
| hosted payments, subscriptions and Stripe | `wreath.billing`, `wreath.payments` |
| service-to-service calls | `wreath.service_client` |
| verified inbound and durable outbound webhooks | `wreath.webhooks` |
| native reverse proxy | `wreath.edge` |
| response caching and purge tags | `wreath.response_cache` |
| email and push delivery | `wreath.email`, `wreath.notifications` |

## Operations and evidence

| Need | Module |
|---|---|
| native HTTP server | `wreath.server` |
| health and readiness | `wreath.health` |
| logs, metrics and traces | `wreath.logging`, `wreath.metrics`, `wreath.telemetry` |
| bounded request evidence | `wreath.recording` |
| deterministic reproduction | `wreath.replay`, `wreath.simulation` |
| application testing | `wreath.testing` |
| startup hardening and diagnostics | `wreath.hardening`, `wreath.doctor` |

## Complete member reference

Every page below is generated from the installed Wreath objects during a strict docs
build. A renamed class, broken import or stale target fails the build instead of
quietly publishing fictional API documentation.

- [Application and HTTP](application.md)
- [First-class policy](policy.md)
- [Identity and tenancy](identity.md)
- [Data and analysis](data.md)
- [Realtime and durable work](realtime.md)
- [Protocols and delivery](protocols.md)
- [MCP](mcp.md)
- [Agents](agents.md)
- [ChatOps](chat.md)
- [Billing and subscriptions](billing.md)
- [Operations](operations.md)
- [Tooling](tooling.md)

For task-oriented examples, begin with the [guides](../guides/http-api.md). The
reference pages answer “what exactly ships?”; the guides answer “how do these parts
fit together safely?”
