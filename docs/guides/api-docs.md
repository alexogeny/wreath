# Interactive API docs

Every team wants Swagger in staging and nobody wants it in production — and the usual result is docs gating scattered across three places, one of which gets forgotten the day the OpenAPI endpoint leaks. Wreath's docs page is **self-contained** (no CDN, no external assets) and **fail-closed** by construction.

## User story: a "try it out" console for staging QA only

> *As an API author, I want the staging docs page to have a "try it out" console so QA can exercise real endpoints from the browser — but I don't want to open an SSRF hole, add a second auth surface, or risk it appearing in production.*

```python
app.enable_api_docs(
    environments=("staging",),                # never registered in production
    auth=BearerTokenBackend(verify),          # guards just the docs routes
    try_it_out=True,                          # same-origin request console
)
```

`try_it_out=True` (off by default) issues requests from the browser against the app's *own* routes — no server-side proxy, so no SSRF surface — and inherits exactly the auth gate the docs page already carries. In production, `staging` isn't the current environment, so the routes are never registered at all: a plain `404`, nothing to remember to turn off.

## Enable it

```python
app.enable_api_docs(
    environments=("dev", "staging"),          # never registered elsewhere
    auth=BearerTokenBackend(verify),          # guards just the docs routes
    title="Trailhead", version="2.1.0",
)
```

The page and the `/openapi.json` document are rendered from the *same* signature inspection that drives request binding — so the docs, the spec, and the generated typed clients cannot drift.

## Gating is declarative and fail-closed

- **Environment gate is non-registration.** The current environment is resolved from `env` → `WREATH_ENV` → a default of `"production"`. If `environments` is set and the current one isn't listed, the routes are **never registered** — a plain `404`, no hint the endpoint exists. Forget to set anything and you ship with docs *off*, not exposed.
- **Auth gate.** Pass `auth=` to guard the two docs routes with a backend scoped to *just them* — no global `configure_auth` required, no side effects elsewhere. `authorize=` attaches a Cedar requirement; `authenticated=`/`permissions=` attach the simpler ones. All are enforced by the same audited authorization path as any other route.

Contrast this with the FastAPI pattern — `docs_url=None if ENV=="production" else "/docs"` in the app constructor, plus a hand-rolled `Depends` on `/docs`, plus a second guard on `/openapi.json`. Three imperative places to get right; wreath collapses them into one declared call with nothing to remember to turn *off*.

## Try-it-out

`try_it_out=True` adds a same-origin request console (off by default). It issues requests from the browser against the app's own routes — no server-side proxy, so no SSRF surface — and inherits exactly the auth gate the docs page itself carries.
