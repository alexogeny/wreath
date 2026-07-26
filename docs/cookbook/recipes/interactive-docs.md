# Turn on the interactive API docs (safely)

Everyone wants Swagger in staging and nobody wants it in production — and the
usual result is docs gating scattered across three places, one of which gets
forgotten the day the OpenAPI endpoint leaks. Wreath collapses it into one
declarative, fail-closed call:

```python
app.enable_api_docs(
    environments=("dev", "staging"),          # never registered elsewhere
    auth=BearerTokenBackend(verify),          # guards just the docs routes
    title="Trailhead", version="2.1.0",
)
```

The page and the `/openapi.json` document are rendered from the *same* signature
inspection that drives request binding — so the docs, the spec, and any generated
typed clients cannot drift. The page is self-contained: no CDN, no external
assets.

Gating is fail-closed by construction:

- **Environment gate is non-registration.** The current environment resolves from
  `env=` → `WREATH_ENV` → a default of `"production"`. If `environments` is set
  and the current one isn't listed, the two routes are **never registered** — a
  plain `404`, no hint the endpoint exists. Forget to set anything and you ship
  with docs *off*, not exposed. `enable_api_docs` returns `False` when the gate
  withheld the routes, `True` when it registered them.
- **Auth gate.** `auth=` guards *just* the docs routes with a backend scoped to
  them — no global `configure_auth`, no side effects elsewhere. `authenticated=`
  and `permissions=` attach the simpler requirements; `authorize=` attaches a
  Cedar one. All are enforced by the same authorization path as any other route.

For a "try it out" console, add `try_it_out=True` (off by default):

```python
app.enable_api_docs(
    environments=("staging",),
    auth=BearerTokenBackend(verify),
    try_it_out=True,                          # same-origin request console
)
```

It issues requests from the browser against the app's *own* routes — no
server-side proxy, so no SSRF surface — and inherits exactly the auth gate the
docs page already carries.
