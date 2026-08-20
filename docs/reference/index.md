---
description: How to use Wreath's generated API reference and where each public module lives.
keywords: API docs, class reference, function reference, module index
---

# API reference

This section answers exact questions about public Python: signatures, fields,
return values, and exceptions. Its pages are generated from the source Wreath
imports, so a public symbol and its reference share one owner.

Use the [documentation map](../map.md) or a guide when your question is “which
part should own this?” Use this reference when you already know the module.

## The shortest index

| You are looking for | Module reference |
|---|---|
| application and routing | [`wreath.app`](app.md), [`wreath.router`](router.md) |
| request input and output | [`wreath.request`](request.md), [`wreath.response`](response.md), [`wreath.binding`](binding.md) |
| reusable data contracts | [`wreath.contracts`](contracts.md), [`wreath.protobuf`](protobuf.md) |
| identity and policy | [`wreath.auth`](auth.md), [`wreath.authorization`](authorization.md), [`wreath.tokens`](tokens.md) |
| PostgreSQL and models | [`wreath.postgres`](postgres.md), [`wreath.orm`](orm.md), [`wreath.migrations`](migrations.md) |
| queries and lists | [`wreath.queries`](queries.md), [`wreath.pagination`](pagination.md), [`wreath.series`](series.md) |
| durable work | [`wreath.jobs`](jobs.md), [`wreath.messaging`](messaging.md), [`wreath.workflows`](workflows.md) |
| outbound boundaries | [`wreath.http_client`](http_client.md), [`wreath.objects`](objects.md), [`wreath.email`](email.md), [`wreath.provenance`](provenance.md) |
| runtime and operations | [`wreath.server`](server.md), [`wreath.telemetry`](telemetry.md), [`wreath.logging`](logging.md), [`wreath.testing`](testing.md) |

The sidebar contains every public module. `Ctrl K` searches symbol names and
docstrings across all of them.

## Reference rules

- A module documents only names in its public surface.
- Examples are checked against the imported objects during the docs build.
- A new public module must appear in the repository map and render here.
- Named but unfinished APIs do not get empty reference pages; they live in the
  [roadmap](roadmap.md) until they exist.
