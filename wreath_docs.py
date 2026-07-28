"""Wreath's own documentation site — built by wreath's native SSG.

This is the hero dogfood: wreath's docs, described in typed Python and minted by
``wreath docs build`` (see ``docs/guides/docs-ssg.md``), with no mkdocs, no
mkdocs-material, and no mkdocstrings. The navigation below is the single source
of page ordering and mirrors the thematic, user-journey structure the docs use.

    wreath docs build          # render docs/ -> site/
    wreath docs check          # strict: fail on a dead link or broken anchor
    wreath docs serve          # build, then preview at http://127.0.0.1:8000
"""

from __future__ import annotations

from wreath._docs import THEMES, Nav, Page, Section, Site

nav = Nav(
    Page("Home", "index.md"),
    Section(
        "Getting started",
        Page("Installation and first app", "getting-started/index.md"),
        Page("Project structure and deployment", "getting-started/deployment.md"),
    ),
    Section(
        "A real application",
        Page("The camera-trap example", "example/index.md"),
        Page("Run it", "example/quickstart.md"),
        Page("A tour of the schema, in psql", "example/walkthrough.md"),
        Page("The read API", "example/read-api.md"),
        Page("Uploads and ingest", "example/ingest.md"),
        Page("Charts and calculated views", "example/analysis.md"),
    ),
    Page("Performance", "perf/index.md"),
    Page("Under the hood", "internals/index.md"),
    Section(
        "Coming from FastAPI",
        Page("Wreath for FastAPI developers", "from-fastapi/index.md"),
        Page("Pydantic and validation", "from-fastapi/pydantic.md"),
        Page("SQLModel, SQLAlchemy, and the ORM", "from-fastapi/sqlmodel.md"),
        Page("Alembic and migrations", "from-fastapi/alembic.md"),
        Page("Automated porting (wreath port)", "guides/porting.md"),
    ),
    Section(
        "Guides",
        Section(
            "Handling a request",
            Page("Routing", "guides/routing.md"),
            Page("Requests and responses", "guides/requests-responses.md"),
            Page("Binding, validation, and dependencies", "guides/binding.md"),
            Page("Middleware", "guides/middleware.md"),
            Page("Form-model binding", "guides/forms.md"),
            Page("Templates", "guides/templates.md"),
            Page("Dates and times", "guides/dates-and-times.md"),
        ),
        Section(
            "Working with data",
            Page("PostgreSQL", "guides/postgres.md"),
            Page("ORM", "guides/orm.md"),
            Page("JSONB and arrays", "guides/jsonb-arrays.md"),
            Page("Pagination, filtering, and sorting", "guides/pagination.md"),
            Page("Calculated views", "guides/calculated-views.md"),
            Page("Generating CRUD", "guides/crud.md"),
            Page("Distributed locks", "guides/distributed-locks.md"),
            Page("FastAPI and Alembic migration", "guides/migrations.md"),
        ),
        Section(
            "Users, auth, and security",
            Page("Authentication and authorization", "guides/auth.md"),
            Page("Permissions in the UI", "guides/permissions.md"),
            Page("User management", "guides/users.md"),
            Page("Idempotent writes", "guides/idempotency.md"),
        ),
        Section(
            "Realtime and background work",
            Page("WebSockets", "guides/websockets.md"),
            Page("Server-Sent Events", "guides/sse.md"),
            Page("Task progress", "guides/progress.md"),
            Page("Durable jobs and messaging", "guides/jobs.md"),
            Page("Chunked passes", "guides/chunked-passes.md"),
        ),
        Section(
            "Talking to other services",
            Page("Outbound HTTP and webhooks", "guides/http-client.md"),
            Page("Calling another service", "guides/service-client.md"),
            Page("Object storage", "guides/objects.md"),
        ),
        Section(
            "Speed and delivery",
            Page("Caching", "guides/caching.md"),
            Page("Response caching", "guides/response-cache.md"),
            Page("Compression", "guides/compression.md"),
            Page("Static files", "guides/static-files.md"),
            Page("Content negotiation", "guides/content-negotiation.md"),
        ),
        Section(
            "Your API's surface",
            Page("OpenAPI and typed clients", "guides/openapi-typegen.md"),
            Page("Interactive API docs", "guides/api-docs.md"),
            Page("Building a docs site", "guides/docs-ssg.md"),
        ),
        Section(
            "Configuration and operations",
            Page("Configuration and state", "guides/config-state.md"),
            Page("Health, flags, and versioning", "guides/health-flags-versioning.md"),
            Page("GraphQL", "guides/graphql.md"),
            Page("Observability bridges", "guides/observability.md"),
            Page("Native server and protocols", "guides/server.md"),
        ),
        Section(
            "Testing and quality",
            Page("Testing", "guides/testing.md"),
            Page("Finding the N+1 query", "guides/n-plus-one.md"),
            Page("Auditing (a11y & performance)", "guides/auditing.md"),
        ),
    ),
    Section(
        "Cookbook",
        Page("Overview", "cookbook/index.md"),
        Section(
            "Handling requests",
            Page("Handle file uploads", "cookbook/recipes/file-uploads.md"),
            Page("Rate-limit an endpoint", "cookbook/recipes/rate-limiting.md"),
            Page("JSON or MessagePack from one handler", "cookbook/recipes/negotiate-format.md"),
        ),
        Section(
            "Users, auth, and security",
            Page("Add OIDC / OAuth2 login", "cookbook/recipes/oauth2-login.md"),
            Page("Authenticate with an API key", "cookbook/recipes/api-key-auth.md"),
            Page("Authorize with Cedar (RBAC)", "cookbook/recipes/cedar-rbac.md"),
            Page("Make a POST safe to retry", "cookbook/recipes/idempotent-writes.md"),
        ),
        Section(
            "Working with data",
            Page("Paginate, sort, and filter", "cookbook/recipes/paginate-a-list.md"),
            Page("Build an admin console", "cookbook/recipes/build-an-admin-console.md"),
            Page("Query JSONB and arrays", "cookbook/recipes/jsonb-query.md"),
            Page("Run a transaction", "cookbook/recipes/unit-of-work.md"),
            Page("Generate CRUD safely", "cookbook/recipes/safe-crud.md"),
            Page("Manage a database pool", "cookbook/recipes/database-lifespan.md"),
        ),
        Section(
            "Realtime and background work",
            Page("Live progress for a long task", "cookbook/recipes/stream-job-progress.md"),
            Page("Broadcast over WebSockets", "cookbook/recipes/websocket-broadcast.md"),
            Page("Enqueue a durable job", "cookbook/recipes/durable-job.md"),
            Page("Write it exactly once", "cookbook/recipes/exactly-once.md"),
            Page("Run work after the response", "cookbook/recipes/background-jobs.md"),
        ),
        Section(
            "Talking to other services",
            Page("Call a service with a token", "cookbook/recipes/call-a-service.md"),
            Page("Send a signed webhook", "cookbook/recipes/send-webhook.md"),
            Page("Presign an upload", "cookbook/recipes/presign-upload.md"),
        ),
        Section(
            "Speed and delivery",
            Page("Cache an expensive endpoint", "cookbook/recipes/cache-an-endpoint.md"),
        ),
        Section(
            "Your API's surface",
            Page("Generate a typed client", "cookbook/recipes/typed-client.md"),
            Page("Interactive API docs", "cookbook/recipes/interactive-docs.md"),
            Page("Build a docs site", "cookbook/recipes/build-a-docs-site.md"),
        ),
        Section(
            "Operations",
            Page("Liveness and readiness", "cookbook/recipes/health-checks.md"),
            Page("Feature-flag a rollout", "cookbook/recipes/feature-flag-rollout.md"),
            Page("Version your API", "cookbook/recipes/api-versioning.md"),
            Page("Prometheus metrics", "cookbook/recipes/prometheus-metrics.md"),
            Page("OpenTelemetry tracing", "cookbook/recipes/otel-tracing.md"),
            Page("Deploy behind a proxy", "cookbook/recipes/behind-a-proxy.md"),
        ),
        Section(
            "Migrating and quality",
            Page("Move a FastAPI + Alembic SaaS app", "cookbook/recipes/fastapi-alembic-saas.md"),
            Page("Fuzz your own routes", "cookbook/recipes/fuzz-your-routes.md"),
        ),
        Section(
            "For coding agents",
            Page("Overview", "cookbook/agents/index.md"),
            Page("The gates", "cookbook/agents/checks.md"),
            Page("Add an endpoint or model", "cookbook/agents/add-an-endpoint.md"),
            Page("Verify a change", "cookbook/agents/verify-a-change.md"),
            Page("Documenting a module", "cookbook/agents/documenting-a-module.md"),
        ),
    ),
    Section(
        "API reference",
        Page("Application", "reference/app.md"),
        Page("Router", "reference/router.md"),
        Page("Request", "reference/request.md"),
        Page("Responses", "reference/response.md"),
        Page("Binding and validation", "reference/binding.md"),
        Page("Middleware", "reference/middleware.md"),
        Page("Authentication", "reference/auth.md"),
        Page("Authorization", "reference/authorization.md"),
        Page("WebSockets", "reference/websocket.md"),
        Page("Native server", "reference/server.md"),
        Page("PostgreSQL", "reference/postgres.md"),
        Page("ORM", "reference/orm.md"),
        Page("Migrations", "reference/migrations.md"),
        Page("Wreath's own schema", "reference/schema.md"),
        Page("Outbound HTTP client", "reference/http_client.md"),
        Page("Webhooks", "reference/webhooks.md"),
        Page("Templates", "reference/templates.md"),
        Page("Dates and times", "reference/temporal.md"),
        Page("Keyed store", "reference/store.md"),
        Page("Application cache", "reference/cache.md"),
        Page("Cache-Control policies", "reference/cache_control.md"),
        Page("Compression", "reference/compression.md"),
        Page("Background tasks", "reference/background.md"),
        Page("Static files", "reference/staticfiles.md"),
        Page("Testing", "reference/testing.md"),
        Page("Diagnostics", "reference/doctor.md"),
        Page("Audit rules", "reference/audit.md"),
        Page("Object storage", "reference/objects.md"),
        Page("Durable jobs", "reference/jobs.md"),
        Page("Chunked passes", "reference/passes.md"),
        Page("Messaging", "reference/messaging.md"),
        Page("Supervised services", "reference/services.md"),
        Page("User management", "reference/users.md"),
        Page("Named queries", "reference/queries.md"),
        Page("Calculated views", "reference/series.md"),
        Page("Pagination", "reference/pagination.md"),
        Page("Health checks", "reference/health.md"),
        Page("Rooms", "reference/rooms.md"),
        Page("Session store", "reference/session_store.md"),
        Page("Validation errors", "reference/validation_errors.md"),
        Page("GraphQL", "reference/graphql.md"),
        Page("Feature flags", "reference/flags.md"),
        Page("API versioning", "reference/versioning.md"),
        Page("Codemod (wreath port)", "reference/port.md"),
        Page("Configuration", "reference/config.md"),
        Page("Inspector", "reference/inspector.md"),
        Page("Telemetry", "reference/telemetry.md"),
        Page("Recording policies", "reference/recording.md"),
        Page("Replay and fault injection", "reference/replay.md"),
        Page("State", "reference/state.md"),
        Page("OpenAPI", "reference/openapi.md"),
        Page("Client type generation", "reference/typegen.md"),
        Page("Exceptions", "reference/exceptions.md"),
        Page("CLI", "reference/cli.md"),
        Page("Reserved and in-progress surfaces", "reference/roadmap.md"),
    ),
    Section(
        "Explorations",
        Page("The timer that wouldn't settle", "explorations/the-timer-that-wouldnt-settle.md"),
    ),
    Section(
        "Release notes",
        # Each version page must be listed here, not merely linked from the
        # index. The generator withholds an orphan page's output "because
        # nothing links to it" (_docs/site.py), so a page that IS linked but
        # is NOT in the nav is never written and the docs gate fails on a
        # dead link. The release-notes skill adds a line here as well as to
        # the index.
        Page("Overview", "release_notes/index.md"),
        Page("v0.1.0a1", "release_notes/0.1.0a1.md"),
    ),
)

site = Site(
    name="Wreath",
    source="docs",
    output="site",
    nav=nav,
    palette=THEMES["wreath"],
    base_url="https://alexogeny.github.io/wreath/",
    source_url="https://github.com/alexogeny/wreath/edit/main/docs",
    description=(
        "Guides, cookbook, and API reference for the Wreath ASGI framework "
        "and native server."
    ),
    # Working notes, ADRs, and agent manifests live under docs/ but aren't
    # published — mirrors mkdocs' exclude_docs / not_in_nav.
    #
    # Per-version release notes are deliberately NOT excluded and ARE in the
    # nav above: they are user-facing, the index lists them, and publish.yml
    # uses the same file as the GitHub Release body. Excluding them left the
    # index pointing at pages that were never built, so the first release to
    # follow the documented workflow failed the docs gate on a dead link.
    exclude=(
        "plans/",
        "decisions/",
        "agents/",
    ),
)
