---
description: Bind forms, render escaped templates and serve contained static assets with validators and ranges.
keywords: guide browser HTML templates forms multipart static files assets cookies CSRF CSP
---

# Browser apps and assets

Wreath is not JSON-only. Form binding uses the same typed declaration path as JSON,
templates compile once and render through the native tape, and static files stay
beneath a pinned directory while supporting validators and byte ranges.

```python title="app.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Field, Form
from wreath.response import HTMLResponse
from wreath.templates import Template


@dataclass
class Signup:
    name: Annotated[str, Field(min_length=2, max_length=80)]
    email: Annotated[str, Field(min_length=3, max_length=254)]


confirmation = Template.from_string(
    "<h1>Welcome, {{ name }}</h1><p>Check {{ email }}</p>",
    name="signup-confirmation",
)
app = Wreath()


@app.post("/signup")
async def signup(
    request: Request,
    form: Annotated[Signup, Form()],
) -> HTMLResponse:
    return HTMLResponse(confirmation.render(name=form.name, email=form.email))
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app


async def test_form_values_are_bound_and_html_is_escaped() -> None:
    async with TestClient(app) as client:
        response = await client.post(
            "/signup",
            headers={"content-type": "application/x-www-form-urlencoded"},
            content=b"name=Jo+%26+Co&email=hello%40example.test",
        )

    assert response.status == 200
    assert b"Jo &amp; Co" in response.body
    assert b"hello@example.test" in response.body
```

`TemplateDirectory.compile()` resolves includes and reads every source at startup;
rendering touches no template file. `{{ value }}` escapes by default. The language has
bounded `if`, `for` and compile-time `include`, but evaluates no arbitrary Python.

Mount static files after creating the application:

```python title="assets.py"
from wreath import Wreath
from wreath.cache_control import CacheControl
from wreath.policy import CachePolicy, HttpPolicy

app = Wreath(
    http_policy=HttpPolicy(
        cache_control=CachePolicy(
            cdn_default=CacheControl(public=True, max_age=86_400),
        )
    )
)
app.static(
    "/assets",
    "public",
    cache_control=CacheControl(public=True, max_age=3600),
)
```

The mount contains traversal and symlink escape, emits `ETag` and `Last-Modified`,
answers conditional requests, supports resumable byte ranges and gives file lookup its
own bounded executor. Directory listings do not exist. The ordinary `Cache-Control`
keeps browser and generic shared-cache freshness at one hour; RFC 9213
`CDN-Cache-Control` gives a configured CDN one day. Because the CDN policy is a
Structured Dictionary rather than ordinary Cache-Control syntax, Wreath serializes it
separately instead of copying the first field under a different name.

For uploaded files, use `Annotated[UploadedFile, File()]` and enforce size and media
rules before persisting. Use [objects and uploads](objects.md) when a transfer must
resume, be presigned or outlive the request.

A browser write surface normally adds `SessionPolicy`, `CsrfPolicy`, CORS only for
known cross-origin clients, `SecurityHeadersPolicy` and `WebSocketOriginPolicy` through
[first-class policy](policy.md). CSRF exemptions should come from an owned protocol
boundary such as a verified webhook router, not a path-prefix guess.

## Keep OAuth tokens out of browser code

RFC 10017's backend-for-frontend pattern lets a browser call OAuth-protected APIs
without ever receiving an access or refresh token. Wreath gives each resource a fixed
HTTPS origin and path prefix, keeps the token set in a server-side session, removes
browser credentials before forwarding, and adds the access token at the last boundary:

```python title="bff.py"
from wreath import Wreath
from wreath.bff import BFFResource, bff_router, bff_session_policy
from wreath.policy import HttpPolicy
from wreath.session_store import PostgresSessionStore

app = Wreath()
database = app.postgres("main", dsn="postgresql://wreath@db/wreath")
sessions = PostgresSessionStore(database)
catalog = app.http_client("catalog", base_url="https://catalog.internal")

app.configure_http_policy(
    HttpPolicy(
        session=bff_session_policy(
            "replace-with-at-least-32-secret-bytes",
            store=sessions,
        )
    )
)
app.include_router(
    bff_router(
        {
            "catalog": BFFResource(
                catalog,
                target_prefix="/api/v2",
                methods={"GET", "POST", "PATCH", "DELETE"},
            )
        }
    )
)
```

After an Authorization Code + S256 PKCE exchange, call `set_bff_tokens(request,
access_token=..., refresh_token=..., expires_at=...)`. `OidcRelyingParty` owns PKCE,
single-use state, nonce and issuer verification; the BFF owns browser isolation after
the exchange. The session cookie is host-only, `Secure`, `HttpOnly`,
`SameSite=Strict`, and uses the `__Host-Http-` prefix. A client-side session store is
refused because Wreath signs rather than encrypts those cookie contents.

Browser requests go to `/bff/catalog/...` and must carry `X-Wreath-BFF: 1`. That
non-safelisted header makes a cross-origin call pass a CORS preflight before the cookie
can drive a resource request. Resource names, methods, origins and target prefixes are
compiled when the router is built: a request cannot turn the BFF into a general proxy.
`Cookie`, browser-supplied `Authorization`, `Host`, hop-by-hop fields and unlisted
extension headers are dropped outbound; upstream `Set-Cookie` and hop-by-hop fields are
dropped on the way back.
