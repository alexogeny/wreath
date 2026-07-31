# Cookbook

The guides explain how each part of Wreath works. This is the other half: short,
practical recipes for getting a specific thing done, with the whole solution in
one place so you can read it, adapt it, and move on.

There are two circles of readers here, and each gets its own set.

- **[For developers](#for-developers)** — you're building something and need the
  pattern for a common task.
- **[For coding agents](agents/index.md)** — you're changing this codebase, and
  you need to know the gates it must pass, the invariants it must keep, and how
  to prove a change actually works.

## For developers

### Handling requests

- [Handle file uploads](recipes/file-uploads.md)
- [Rate-limit an endpoint](recipes/rate-limiting.md)
- [Serve JSON or MessagePack from one handler](recipes/negotiate-format.md)
- [Accept a Protocol Buffers request body](recipes/accept-a-protobuf-body.md)
- [Serve a gRPC method](recipes/serve-a-grpc-method.md)

### Users, auth, and security

- [Add OIDC / OAuth2 login](recipes/oauth2-login.md)
- [Authenticate with an API key](recipes/api-key-auth.md)
- [Turn on an authenticator app (TOTP)](recipes/enable-totp.md)
- [Authorize with Cedar policies (RBAC)](recipes/cedar-rbac.md)
- [Make a POST safe to retry](recipes/idempotent-writes.md)

### Working with data

- [Paginate, sort, and filter a list](recipes/paginate-a-list.md)
- [Query JSONB and array columns](recipes/jsonb-query.md)
- [Search text with PostgreSQL](recipes/search-documents.md)
- [Search by meaning with embeddings](recipes/semantic-search.md)
- [Combine keyword and semantic search](recipes/hybrid-search.md)
- [Run a transaction (unit of work)](recipes/unit-of-work.md)
- [Generate CRUD without leaking secrets](recipes/safe-crud.md)
- [Manage a database pool with the lifespan](recipes/database-lifespan.md)

### Realtime and background work

- [Show a live progress bar for a long task](recipes/stream-job-progress.md)
- [Broadcast to many WebSocket clients](recipes/websocket-broadcast.md)
- [Enqueue a durable job with retries](recipes/durable-job.md)
- [Run work after the response](recipes/background-jobs.md)
- [Write it exactly once, end to end](recipes/exactly-once.md)

### Talking to other services

- [Call a service with an auto-refreshing token](recipes/call-a-service.md)
- [Send a signed webhook with retries](recipes/send-webhook.md)
- [Let a client upload straight to object storage](recipes/presign-upload.md)
- [Serve your first MCP tool](recipes/serve-mcp-tools.md)

### Speed and delivery

- [Cache an expensive endpoint](recipes/cache-an-endpoint.md)

### Your API's surface

- [Generate a typed TypeScript client](recipes/typed-client.md)
- [Build an admin console](recipes/build-an-admin-console.md)
- [Turn on the interactive API docs (safely)](recipes/interactive-docs.md)
- [Build a docs site with `wreath docs`](recipes/build-a-docs-site.md)

### Operations

- [Add liveness and readiness endpoints](recipes/health-checks.md)
- [Roll a feature out to a percentage of users](recipes/feature-flag-rollout.md)
- [Version your API](recipes/api-versioning.md)
- [Expose a Prometheus `/metrics` endpoint](recipes/prometheus-metrics.md)
- [Trace requests with OpenTelemetry](recipes/otel-tracing.md)
- [Deploy behind a proxy or load balancer](recipes/behind-a-proxy.md)

### Migrating and quality

- [Move a schema-per-tenant SaaS application from Alembic](recipes/fastapi-alembic-saas.md)
- [Fuzz your own routes](recipes/fuzz-your-routes.md)
- [Find the controls your tests do not watch](recipes/find-unwatched-controls.md)
