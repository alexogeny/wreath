---
description: The explicit route map for Wreath's guides, recipes, examples, reference, performance evidence, and release notes.
keywords: docs navigation, table of contents, sitemap, where is, browse documentation
boost: 2
---

# Documentation map

Choose the question you have. Each route has a different job; no page tries to
be tutorial, recipe, explanation, and reference at once.

## I am starting or migrating

| If this is you | Take this route |
|---|---|
| I have an empty directory | [Installation and your first app](getting-started/index.md) |
| I want Wreath to scaffold the project | [Starting a project](getting-started/new-project.md) |
| I need the production layout | [Project structure and deployment](getting-started/deployment.md) |
| I know FastAPI and Pydantic | [Wreath for FastAPI developers](from-fastapi/index.md) |
| I use SQLModel or SQLAlchemy | [The Wreath ORM translation](from-fastapi/sqlmodel.md) |
| Alembic owns my schema | [The migration path](from-fastapi/alembic.md) |
| I want to port code mechanically | [Automated porting](guides/porting.md) |

## I am building a service

| Part of the service | Begin here | Common next step |
|---|---|---|
| request path | [Routing](guides/routing.md) | [Binding and validation](guides/binding.md) |
| responses and formats | [Requests and responses](guides/requests-responses.md) | [Content negotiation](guides/content-negotiation.md) |
| database | [PostgreSQL](guides/postgres.md) | [ORM](guides/orm.md) and [migrations](guides/migrations.md) |
| lists and search | [Pagination](guides/pagination.md) | [Full-text](guides/full-text-search.md) or [vector search](guides/vector-search.md) |
| identity and policy | [Authentication and authorization](guides/auth.md) | [Users](guides/users.md) and [organisations](guides/organizations.md) |
| background work | [Jobs and messaging](guides/jobs.md) | [Workflows](guides/workflows.md) and [progress](guides/progress.md) |
| realtime | [WebSockets](guides/websockets.md) | [SSE](guides/sse.md), [streams](guides/streams.md), [live queries](guides/sync.md) |
| other services | [Outbound HTTP](guides/http-client.md) | [Service clients](guides/service-client.md), [webhooks](reference/webhooks.md), [MCP](guides/mcp.md) |
| files and delivery | [Object storage](guides/objects.md) | [Static files](guides/static-files.md), [caching](guides/caching.md), [compression](guides/compression.md) |
| operations | [Configuration and state](guides/config-state.md) | [Health](guides/health-flags-versioning.md), [observability](guides/observability.md), [server](guides/server.md) |
| assurance | [Testing](guides/testing.md) | [Preflight](guides/preflight.md), [hardening](guides/hardening.md), [mutation testing](guides/mutant.md) |

## I need an answer I can adapt

The [cookbook](cookbook/index.md) groups complete recipes by task. It is the
right route when your question begins with a verb: paginate a list, send a
webhook, add TOTP, expose metrics, or deploy behind a proxy.

Coding agents have a separate route through the same repository:
[agent cookbook](cookbook/agents/index.md).

## I need exact API facts

The [API reference](reference/index.md) is generated from each public module.
Use it for signatures, fields, return values, and exceptions. Use a guide when
you need ownership, lifecycle, or trade-offs.

| Symbol family | Reference |
|---|---|
| application, routing, request, response | [`app`](reference/app.md), [`router`](reference/router.md), [`request`](reference/request.md), [`response`](reference/response.md) |
| contracts and binding | [`contracts`](reference/contracts.md), [`binding`](reference/binding.md), [`validation_errors`](reference/validation_errors.md) |
| data | [`postgres`](reference/postgres.md), [`orm`](reference/orm.md), [`migrations`](reference/migrations.md), [`pagination`](reference/pagination.md) |
| security | [`auth`](reference/auth.md), [`authorization`](reference/authorization.md), [`tokens`](reference/tokens.md), [`signatures`](reference/signatures.md) |
| work | [`jobs`](reference/jobs.md), [`messaging`](reference/messaging.md), [`workflows`](reference/workflows.md), [`streams`](reference/streams.md) |
| service boundaries | [`http_client`](reference/http_client.md), [`objects`](reference/objects.md), [`provenance`](reference/provenance.md), [`email`](reference/email.md) |

## I am evaluating Wreath

- [Capability map](capabilities.md): what ships and what remains outside the framework.
- [Performance](perf/index.md): current measurements, environment, controls, and limits.
- [Request path](internals/index.md): the structures behind the hot path.
- [Real application](example/index.md): the camera-trap example and its deliberate hard cases.
- [Release notes](release_notes/index.md): changes by version.
- [Roadmap](reference/roadmap.md): named surfaces that have not shipped.

If none of these names your question, search with `Ctrl K`. The search index is
section-based, so a result lands on the heading that answers rather than at the
top of a long page.
