# Calling another service

When your app calls a sibling service, every request repeats the same two chores:
prefix the base path, and attach a bearer token that has to stay fresh.
`ServiceClient` binds both once, so the call sites read like the remote API.

## User story: a token that refreshes itself

> *As a service author, I call the billing service with a machine-to-machine
> token. The token expires every hour. I don't want to think about refreshing it
> on every call — I just want to call `billing.get(...)` and have auth handled.*

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

`ClientCredentials` caches the M2M token and renews it before it expires, so
`ServiceClient` just asks for the current one on each request — no expiry
bookkeeping at the call site, no token in your handler code.

## The token source is flexible

`token=` accepts whatever you have:

```python
ServiceClient(http_client, token="static-api-key")           # a fixed string
ServiceClient(http_client, token=my_async_token_provider)     # an async () -> str
ServiceClient(http_client, token=client_credentials)          # anything with .token()
ServiceClient(http_client)                                    # no auth header at all
```

## Shared headers and every verb

```python
svc = ServiceClient(
    http_client,
    base_path="/v2",
    token=creds,
    default_headers=((b"x-app", b"my-service"),),   # sent on every request
)

await svc.get("/things")
await svc.post("/things", body=b"{...}", idempotency_key="once")
await svc.put("/things/1", body=b"{...}")
await svc.patch("/things/1", body=b"{...}")
await svc.delete("/things/1")
```

Per-call headers merge on top of the defaults, and the `Authorization` header is
added for you. Everything else — pooling, timeouts, retries, rate limiting — is
the underlying [`http_client`](http-client.md) doing its job.

## Don't write the methods; generate them

Written by hand, the calls above are stringly typed: a path built with an
f-string, a response that is `Any`, and a shape the caller re-parses. If the
service on the other end is also a wreath application, generate the client
instead:

```bash
wreath typegen llama_service:app --target python --output ./llama_api \
  --class-name LlamaClient
```

The result subclasses the `ServiceClient` on this page — it adds typed methods
and nothing else, so everything above still applies. Responses come back as
dataclasses, validated by the same binding layer the provider uses, and a
breaking change on the provider's side can be made to fail your build rather
than your requests. See
[OpenAPI and typed clients](openapi-typegen.md#calling-a-sibling-service-typed).
