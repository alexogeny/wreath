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

app = Wreath()
app.static(
    "/assets",
    "public",
    cache_control=CacheControl(public=True, max_age=3600),
)
```

The mount contains traversal and symlink escape, emits `ETag` and `Last-Modified`,
answers conditional requests, supports resumable byte ranges and gives file lookup its
own bounded executor. Directory listings do not exist.

For uploaded files, use `Annotated[UploadedFile, File()]` and enforce size and media
rules before persisting. Use [objects and uploads](objects.md) when a transfer must
resume, be presigned or outlive the request.

A browser write surface normally adds `SessionPolicy`, `CsrfPolicy`, CORS only for
known cross-origin clients, `SecurityHeadersPolicy` and `WebSocketOriginPolicy` through
[first-class policy](policy.md). CSRF exemptions should come from an owned protocol
boundary such as a verified webhook router, not a path-prefix guess.
