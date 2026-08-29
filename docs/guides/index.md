---
description: Find every conventional web-framework concern in Wreath without learning Wreath's module names first.
keywords: guides middleware routing validation forms files static templates sessions CORS CSRF caching migrations webhooks deployment
boost: 1.4
---

```hero
eyebrow: Guides · the ordinary things still matter
title: Find the thing you expected a framework to have.
lede: Start with the name you already use—middleware, forms, sessions, migrations, caching or webhooks—and follow it to the Wreath owner that makes the behavior explicit.
signal: HTTP basics
signal: browser security
signal: data lifecycle
signal: operations
action: Build one API -> http-api.md
action: See the complete surface -> ../reference/index.md
```

## HTTP application basics

| You are looking for | Start here | Public owner |
|---|---|---|
| application, lifespan and state | [configuration and lifecycle](configuration.md) | `wreath.app`, `wreath.state`, `wreath.config` |
| routes and routers | [build an HTTP API](http-api.md) | `wreath.router`, `Wreath.route`, `Wreath.include_router` |
| path, query, header and cookie inputs | [build an HTTP API](http-api.md) | `wreath.binding` |
| JSON, forms and file bodies | [HTTP API](http-api.md), [browser apps](browser-apps.md), [objects](objects.md) | `wreath.binding`, `wreath.objects` |
| validation and problem responses | [build an HTTP API](http-api.md) | `wreath.binding`, `wreath.validation_errors`, `wreath.exceptions` |
| response classes and streaming | [application reference](../reference/application.md) | `wreath.response` |
| dependencies | [application reference](../reference/application.md) | `wreath.binding.Depends` |
| content negotiation | [protocols](protocols.md) | `wreath.negotiation` |
| OpenAPI and generated clients | [build an HTTP API](http-api.md) | `wreath.openapi`, `wreath.typegen` |
| API versions and pagination | [application reference](../reference/application.md) | `wreath.versioning`, `wreath.pagination` |

## What other frameworks call middleware

The standard stack is [first-class HTTP policy](policy.md), not a list of nested
callables. That page covers proxy trust, hosts, maintenance, bot traffic, rate limits,
request decompression, IDs, timing, CORS, CSRF, CSP/HSTS, WebSocket origins, sessions,
idempotency, cache headers, compression, concurrency and deadlines. It also shows the
custom hook protocol for behavior that is genuinely application-specific.

## Browser-facing applications

| Need | Guide or reference | Owner |
|---|---|---|
| safe server-rendered HTML | [browser apps and assets](browser-apps.md) | `wreath.templates` |
| static assets, ranges and validators | [browser apps and assets](browser-apps.md) | `wreath.staticfiles` |
| URL-encoded and multipart forms | [browser apps and assets](browser-apps.md) | `wreath.binding.Form` |
| uploads and resumable objects | [objects and uploads](objects.md) | `wreath.binding.File`, `wreath.objects` |
| cookie or server-side sessions | [policy](policy.md), [identity](identity.md) | `wreath.policy.sessions`, `wreath.session_store` |
| CORS, CSRF and browser headers | [policy and hardening](policy.md) | `wreath.policy` |

## Identity and multi-tenancy

[Identity and users](identity.md) starts with authentication and denial paths. The
[enterprise story](../stories/enterprise.md) carries the same principal through tenant
resolution, Cedar authorization, SAML, OIDC, SCIM, quotas and support operations.
The [identity reference](../reference/identity.md) contains the complete users,
organisations, tokens, sessions, second-factor, privacy and tenancy APIs.

## Data and background work

| Need | Guide | Owner |
|---|---|---|
| pools, models, queries and transactions | [PostgreSQL and models](data.md) | `wreath.postgres`, `wreath.orm` |
| migrations and schema generation | [detect through rollback](migration-workflow.md) | `wreath.migrations`, `wreath.schema` |
| safe raw SQL | [PostgreSQL and models](data.md) | `wreath.sql` |
| temporal and analytical queries | [time-series story](../stories/time-series-lab.md) | `wreath.temporal`, `wreath.series` |
| objects and uploads | [objects and uploads](objects.md) | `wreath.objects` |
| live sockets and rooms | [realtime and durable work](realtime.md) | `wreath.websocket`, `wreath.rooms` |
| jobs, workflows and progress | [realtime and durable work](realtime.md) | `wreath.jobs`, `wreath.workflows`, `wreath.progress` |
| safe large-table maintenance | [chunked passes](chunked-passes.md) | `wreath.passes` |
| retries and external effects | [exactly-once effects](../cookbook/recipes/exactly-once.md) | idempotency, jobs, webhooks |

## Integration and protocol boundaries

[Integration boundaries](integrations.md) covers outbound HTTP, service clients,
verified inbound webhooks, durable outbound delivery, email and notifications.
[Protocols](protocols.md) covers Protobuf, gRPC and GraphQL; [MCP](mcp.md) covers tools,
resources, prompts, OAuth and agent-facing authorization.

## Production and proof

[Deploy Wreath](deployment.md) is the production runbook for extras, TLS, edge
topology, worker sizing, graceful shutdown, service configuration and diagnosis.
[Operations](operations.md) covers health, readiness and observability. The
[command-line task map](cli.md) connects scaffolding, migrations, preflight,
capture, replay, durable work and crash evidence. [Testing and evidence](testing.md) covers
the in-process client, WebSockets, the Wreath runner, mutation confidence, recording,
replay and deterministic simulation. The [operations reference](../reference/operations.md)
holds logging, metrics, telemetry, hardening and diagnostic APIs.

The [version and upgrade contract](../start/releases.md) states exactly which
Wreath release this site documents, the supported wheel matrix and how compatibility
changes before 1.0.

If a conventional term is absent from this page, the [complete surface map](../reference/index.md)
lists every subsystem, and the generated reference imports every documentable public
module during the strict build.
