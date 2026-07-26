# Version your API

When `v2` of a resource has to ship without breaking the `v1` callers still in
the wild, group each version's routes and mount them under a URL prefix.
`VersionedRouter` collects one `Router` per version and mounts each under
`/v1`, `/v2`, and so on:

```python
from wreath.versioning import VersionedRouter

api = VersionedRouter()          # prefixes default to /v{version}

v1 = api.version("1")            # a Router mounted under /v1
v2 = api.version("2")            # a Router mounted under /v2

@v1.get("/llamas")
async def list_v1(request): ...

@v2.get("/llamas")
async def list_v2(request): ...

app.include_router(api.router())   # -> /v1/llamas and /v2/llamas
```

`api.version(tag)` returns (creating on first call) the `Router` for that
version, so you register handlers on it like any other router. `api.router()`
returns a fresh `Router` with every version mounted at its prefix, ready to
`include_router`. Versioning is a router-construction helper, not a lifecycle
resource — so it composes with ordinary `include_router` rather than an `app.*`
factory.

Prefer to keep one URL and branch inside a handler? `negotiate_version` reads an
opt-in `Accept-Version` header, falling back to your default when it's absent or
names a version you don't support:

```python
from wreath.versioning import negotiate_version

@app.get("/llamas")
async def list_llamas(request):
    version = negotiate_version(request, default="1", supported=("1", "2"))
    return list_v2(request) if version == "2" else list_v1(request)
```
