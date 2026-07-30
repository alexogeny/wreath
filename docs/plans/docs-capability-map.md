# Prescriptive plan: make the covered surface visible

Status: **all three stages implemented** (July 2026). `docs/capabilities.md` and
the `::: capability-map` directive (`src/wreath/_docs/capabilities.py`) render
the table from the manifest; `wreath-map-lint` gates the `capability`/`replaces`
fields (MAP010–MAP012); the reverse index ships as a separate `aliases` field in
the search record rather than merged into front-matter `keywords`, because
merged, package names that are also English words — `limits`, `arrow` —
outranked the pages they were meant to point at. The three edits under "the two
edits that make people reach it" are all in place.

Related material:

- `AGENTS.md` §Documentation rules
- `docs/agents/manifest.json` — the data file this plan extends
- `wreath_docs.py` — the nav, and the single source of page ordering
- `docs/index.md`, `docs/from-fastapi/index.md`
- `docs/cookbook/agents/documenting-a-module.md`
- `~/research/pypi-downloads/wreath-gap-analysis.md`

## The problem

An ecosystem survey of the 2026-07 PyPI download data found that Wreath already
answers roughly **22 of the capability buckets** a Python web application reaches
for a package to get — rate limiting, CORS, background jobs, OAuth2/OIDC, object
storage, signed webhooks, feature flags, dependency injection, OpenAPI and typed
clients, observability bridges, migrations, distributed locks, and so on. Several
of those need three or four packages elsewhere.

**None of that is visible from the docs site.** The evidence:

- `docs/index.md`'s "What Wreath gives you" *does* state the breadth — in a
  single nine-line run-on sentence beginning "Middleware, authentication and
  authorization ... and an in-process test client." It is accurate and
  unreadable. Nobody scans a comma-separated list of twenty features; they skim
  it and retain "lots of stuff".
- The nav is a correct tree and a poor map. 55 reference pages, 44 guides, and
  ~35 recipes across four levels of nesting mean the breadth is only visible to
  someone who expands everything and already knows what to look for.
- **Nowhere does the site name the package Wreath replaces.** Someone evaluating
  Wreath is not asking "does it have a middleware section"; they are asking "do I
  still need `slowapi`, `celery`, `python-multipart`, `sse-starlette`,
  `django-storages`, `authlib`". That question has no page.
- "Coming from FastAPI" is four pages, and they are good pages, but they cover
  the *migration mechanics of three dependencies* — FastAPI itself, Pydantic,
  SQLModel/SQLAlchemy, Alembic. That is the porting story, not the breadth story.
  A reader finishes it knowing how to translate their models and still not
  knowing that Wreath has durable jobs.

The result is a framework whose most unusual property — that the batteries are
genuinely included — is the one a visitor is least likely to learn.

## Goal

One page a visitor can scan in ninety seconds that answers "what do I no longer
install?", generated from data that a gate keeps true, plus the two edits that
make sure people reach it.

## Non-goals

- Not a competitor comparison page. No benchmark bars, no feature-matrix
  scoring, no "vs Django" column. The claim is about Wreath's own surface; the
  package names are there to locate a capability in the reader's vocabulary, not
  to argue anyone else is bad.
- Not a replacement for the guides. Every row links out; the page itself carries
  no teaching.
- **No download counts on the page.** They were the right evidence for choosing
  what to build and they are the wrong content for a docs page — they date
  instantly, they invite an argument about methodology, and they make a
  neutral map read as marketing. Keep them in the research file.

## Design: generate it from the manifest

`docs/agents/manifest.json` already lists 38 subsystems, each with `guides`,
`reference`, `sources`, `tests`, and `decisions`, and `uv run wreath-map-lint`
already fails when any cited path is missing. It is the natural home.

**Add one optional key per subsystem:**

```json
{
  "name": "middleware",
  "replaces": ["slowapi", "flask-limiter", "django-cors-headers", "flask-cors"],
  "capability": "Rate limiting, CORS, CSRF, sessions, compression, request IDs",
  "guides": ["docs/guides/middleware.md"],
  "reference": ["docs/reference/middleware.md"]
}
```

`capability` is the human sentence; `replaces` is the vocabulary. The page is
rendered from those two fields plus the `guides`/`reference` links each subsystem
already carries, so **a new subsystem appears on the capability map by being
added to the manifest**, which `AGENTS.md` already requires in the same change
that adds a module.

This is the difference between a page that is true today and a page that stays
true. The maps in this repository drifted badly once and a gate exists because of
it; a hand-written table of twenty-two rows would be the next thing to drift.

**Gate it.** Extend `wreath-map-lint` with three checks:

1. Every `replaces` entry is a plausible distribution name (the lint cannot
   verify PyPI exists offline, and should not try — it checks shape, not truth).
2. Every subsystem that has `guides` or `reference` has `capability`, or is
   explicitly marked `"capability": null` to mean "internal, not user-facing"
   (`devtools`, `docs-ssg`, `example`, `performance`, `port` are the likely
   nulls). A subsystem that is neither described nor explicitly excluded is the
   failure this catches.
3. The generated page's links resolve — which the existing dangling-path check
   already does once the page is in `docs/`.

**Where the renderer goes.** `src/wreath/_docs/` owns the site generator and
already has a reference directive (`:::`). Add a small directive —
`::: capability-map` — that reads the manifest at build time and emits the table,
the same way `:::` emits API reference. That keeps `wreath docs check` as the
single gate and avoids a generated file checked into `docs/`.

## The page

`docs/capabilities.md`, titled **"What you don't have to install"**.

Opening, three sentences: Wreath is one dependency-free package; here is what
that covers; every row links to the guide that teaches it. Then one table,
grouped by the same themes the nav uses so a reader who scrolls the nav
afterwards recognises the shape:

| Capability | Elsewhere you'd install | In Wreath | Guide |
|---|---|---|---|
| Validation and settings | `pydantic`, `pydantic-settings` | `wreath.binding`, `wreath.config` | [Binding](guides/binding.md) |
| File uploads | `python-multipart` | built in | [Forms](guides/forms.md) |
| Server-sent events | `sse-starlette` | `wreath.response.SSEResponse` | [SSE](guides/sse.md) |
| Rate limiting | `slowapi`, `flask-limiter`, `limits` | `wreath.middleware.ratelimit` | [Middleware](guides/middleware.md) |
| CORS, CSRF, sessions, security headers | `flask-cors`, `django-cors-headers`, `secure` | `wreath.middleware` | [Middleware](guides/middleware.md) |
| ORM and migrations | `sqlalchemy`, `alembic`, `asyncpg` | `wreath.orm`, `wreath.migrations`, `wreath.postgres` | [ORM](guides/orm.md) |
| Background and durable jobs | `celery`, `rq`, `arq`, `apscheduler` | `wreath.jobs`, `wreath.passes`, `wreath.background` | [Jobs](guides/jobs.md) |
| Pub/sub and app cache | `redis` | `wreath.messaging`, `wreath.cache`, `wreath.rooms` | [Jobs](guides/jobs.md), [Caching](guides/caching.md) |
| Distributed locks | `redis`, `python-redis-lock` | built into the Postgres driver | [Locks](guides/distributed-locks.md) |
| JWT, OAuth2, OIDC login | `pyjwt`, `authlib`, `oauthlib`, `python-jose` | `wreath.auth` | [Auth](guides/auth.md) |
| User registration, login, reset | `fastapi-users`, `django-allauth`, `flask-login` | `wreath.users` | [Users](guides/users.md) |
| Authorization policy | `casbin`, `openfga-sdk`, `django-guardian` | `wreath.authorization` (Cedar) | [Permissions](guides/permissions.md) |
| OpenAPI schema and docs UI | `drf-spectacular`, `apispec`, `drf-yasg` | `wreath.openapi` | [OpenAPI](guides/openapi-typegen.md) |
| Typed client generation | `openapi-python-client`, `datamodel-code-generator` | `wreath.port` | [OpenAPI](guides/openapi-typegen.md) |
| Object storage | `boto3`, `django-storages`, `minio` | `wreath.objects` | [Objects](guides/objects.md) |
| Signed webhooks | `svix`, `standardwebhooks` | `wreath.webhooks` | [HTTP client](guides/http-client.md) |
| Outbound HTTP with retries | `httpx`, `tenacity` | `wreath.http_client` | [HTTP client](guides/http-client.md) |
| Metrics, traces, logs | `opentelemetry-*`, `prometheus-client`, `statsd`, `structlog` | `wreath.telemetry`, `wreath.logging` | [Observability](guides/observability.md) |
| Static files | `whitenoise` | `wreath.staticfiles` | [Static files](guides/static-files.md) |
| Templates | `jinja2` | `wreath.templates` | [Templates](guides/templates.md) |
| GraphQL | `strawberry-graphql`, `graphene`, `ariadne` | `wreath.graphql` | [GraphQL](guides/graphql.md) |
| Feature flags | `flagsmith`, `openfeature-sdk`, `django-waffle` | `wreath.flags` | [Flags](guides/health-flags-versioning.md) |
| Dependency injection | `dependency-injector`, `svcs` | `Depends`, `wreath.services` | [Binding](guides/binding.md) |
| Pagination | `fastapi-pagination`, `django-filter` | `wreath.pagination` | [Pagination](guides/pagination.md) |
| Test client and fixtures | `httpx`, `respx`, `schemathesis` | `wreath.testing`, `wreath.replay` | [Testing](guides/testing.md) |

Close with an **honest short list of what Wreath does not include** and what to
install instead — image processing (`pillow`), spreadsheet and PDF export
(`openpyxl`, `weasyprint`), payments (`stripe`), transactional email providers
(`resend`, `sendgrid`; `wreath.users` ships SMTP only), and whatever
`docs/reference/roadmap.md` currently lists. This paragraph is what makes the
rest of the page credible. A page that claims everything is read as a page that
claims nothing.

## The two edits that make people reach it

**1. `docs/index.md`.** Replace the nine-line run-on bullet with a short one that
names four things and points at the map. The bullet currently tries to be the
capability map and cannot be, because it is a sentence. Something like:

> - **The whole circle, not a starter kit.** An ORM and migrations over a native
>   Postgres driver, durable jobs and messaging, authentication with a built-in
>   Cedar policy engine, OpenAPI with typed clients, and about twenty more things
>   you would otherwise assemble. See [what you don't have to
>   install](capabilities.md).

**2. `wreath_docs.py`.** Add `Page("What you don't have to install",
"capabilities.md")` immediately after `Page("Home", "index.md")` — above
"Getting started", because it is an evaluation page, not a learning page, and the
person who needs it has not decided to install anything yet. Note that the
generator withholds an orphan page's output, so the nav entry is required, not
optional (the release-notes comment in `wreath_docs.py` records what happens when
this is forgotten).

**3. `docs/from-fastapi/index.md`.** Add one section — *"Your requirements.txt,
line by line"* — showing a realistic FastAPI service's requirements file with
each line annotated by what replaces it in Wreath and which lines survive. This
is the highest-conversion format for exactly the reader that page is for: they
already have the file open. It complements the capability map rather than
duplicating it, because it is ordered by *their* dependency list rather than by
Wreath's subsystems.

## Staging

**Stage 1 — the page, hand-checked.** Write `capability` and `replaces` into the
manifest for all 38 subsystems, add the `::: capability-map` directive, land the
page and the three edits above. One pass, one review, one `wreath docs check`.

**Stage 2 — the gate.** Extend `wreath-map-lint` with the three checks. Do this
second only because the data must exist before the lint can require it; do not
leave it undone, because an ungated map is the thing this plan exists to prevent.

**Stage 3 — the reverse index, if wanted.** A short "I searched for X" alias list
(`fastapi rate limiting` → the middleware guide) rendered into the page's front
matter for search. Cheap, and it catches the reader who arrives from a search
engine rather than the front page.

## How we will know it worked

Not by a metric — the site has no analytics and should not grow any for this.
The check is a question a person can answer: **hand the docs site to someone who
has shipped a FastAPI service and ask them, after two minutes, to name five
things Wreath includes that they currently install.** Today that fails. If it
still fails after this lands, the page is in the wrong place in the nav, not the
wrong page.
