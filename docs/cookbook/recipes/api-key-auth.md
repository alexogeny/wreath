# Authenticate with an API key

Not every caller carries a bearer token — a service, a webhook, a cron job often
authenticates with a long-lived API key in a header. Authentication is just a
backend that turns a credential into an `Identity` (or into nothing), so write a
tiny backend that reads your header and looks the key up, then gate handlers with
`@authenticated()`:

```python
from wreath import Wreath
from wreath.auth import AuthenticationBackend, Identity, authenticated
from wreath.request import Request

app = Wreath()

class ApiKeyBackend(AuthenticationBackend):
    async def authenticate(self, request: Request) -> Identity | None:
        key = request.header("x-api-key")
        if key is None:
            return None
        account = await lookup_api_key(key)          # your DB / cache lookup
        if account is None:
            return None
        return Identity(account.id, roles=frozenset(account.roles))

    def challenge(self, request: Request) -> str | None:
        return "ApiKey"

app.configure_auth(ApiKeyBackend())

@app.get("/ingest")
@authenticated()
async def ingest(request) -> dict:
    return {"account": request.identity.id}
```

`authenticate` returns `None` for a missing *or* invalid key — never raise to
signal "no identity", because `@authenticated()` turns `None` into a clean `401`
with a `WWW-Authenticate` challenge, while an exception would be a `500`. Past the
gate, `request.identity` is guaranteed present. To accept *either* an API key or a
bearer token, wrap several backends in `CompositeBackend(...)` — the first one to
return an `Identity` wins.
