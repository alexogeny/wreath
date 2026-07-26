# Call another service with an auto-refreshing token

When your app calls a sibling service, every request repeats the same two chores:
prefix the base path, and attach a bearer token that has to stay fresh.
`ServiceClient` binds both once, so the call sites read like the remote API:

```python
from wreath.service_client import ServiceClient
from wreath._auth.oauth2 import ClientCredentials

billing = ServiceClient(
    http_client,                                 # your origin-pinned Client
    base_path="/billing/v1",
    token=ClientCredentials(
        http_client=http_client,
        token_path="/oauth/token",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    ),
)

async def get_invoice(invoice_id: str) -> dict:
    response = await billing.get(f"/invoices/{invoice_id}")
    return await response.json()
```

`ClientCredentials` caches the machine-to-machine token and renews it before it
expires, so `ServiceClient` just asks for the current one on each request — no
expiry bookkeeping at the call site, no token in your handler code.

The token source is flexible — `token=` accepts whatever you have:

```python
ServiceClient(http_client, token="static-api-key")          # a fixed string
ServiceClient(http_client, token=my_async_token_provider)    # an async () -> str
ServiceClient(http_client, token=client_credentials)         # anything with .token()
ServiceClient(http_client)                                   # no auth header at all
```

Every verb is there — `get`, `post`, `put`, `patch`, `delete` — with per-call
headers merging on top of any `default_headers` and the `Authorization` header
added for you. Everything else — pooling, timeouts, retries, rate limiting — is
the underlying [`http_client`](../../guides/http-client.md) doing its job.
