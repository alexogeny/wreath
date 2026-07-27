"""A thin, auth-aware wrapper around `wreath.http_client.HTTPClient`.

Calling another service usually means the same two chores on every request: put a
base path in front of the target, and attach a bearer token that has to be kept
fresh. `ServiceClient` binds both once so call sites read like the API:

```python
from wreath.service_client import ServiceClient
from wreath._auth.oauth2 import ClientCredentials

billing = ServiceClient(
    http_client,                                   # an origin-pinned HTTPClient
    base_path="/billing/v1",
    token=ClientCredentials(http_client=http_client, token_path="/oauth/token",
                            client_id=..., client_secret=...),
)
response = await billing.get(f"/invoices/{id}")
invoice = loads(response.body)
```

The token source is auto-refreshing: a `ClientCredentials` caches and
renews the M2M token before it expires, so `ServiceClient` just asks for the
current one per request. A plain `str` (static token) or a zero-arg async
callable (your own refresh logic) work too.

The response is whatever the wrapped client returns -- for `HTTPClient` that is
a `ClientResponse`, which holds the body as `bytes` and does not decode it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ServiceClient"]

# The bearer-token source may be a static ``str``, an async ``() -> str`` callable,
# or anything with an awaitable ``.token()`` (e.g. ClientCredentials).
_Headers = tuple[tuple[bytes, bytes], ...]


class ServiceClient:
    """An HTTP client bound to a base path and a (refreshing) bearer token.

    Every method delegates to the wrapped client's `request`, so the pool,
    timeouts, retries, and destination policy are the wrapped client's -- this
    adds path prefixing and an `Authorization` header and nothing else. It
    holds no state of its own and is safe to share across concurrent requests to
    exactly the degree the wrapped client is.

    Three token sources are accepted, resolved in this order: a plain `str`
    (static), any object with an awaitable `.token()` (a
    `wreath._auth.oauth2.ClientCredentials`, which caches and renews),
    and a zero-argument async callable. Anything else raises `TypeError` on
    the first request rather than at construction.

    Args:
        client: An origin-pinned client, normally a `wreath.http_client.HTTPClient`.
        token: Bearer-token source, or None to send no `Authorization` header.
        base_path: Prefix put in front of every path; a trailing `/` is stripped.
        default_headers: Sent on every request, after the bearer header and before the caller's.
    """

    __slots__ = ("_base_path", "_client", "_default_headers", "_token")

    def __init__(
        self,
        client: Any,
        *,
        token: Any = None,
        base_path: str = "",
        default_headers: _Headers = (),
    ) -> None:
        self._client = client
        self._token = token
        self._base_path = base_path.rstrip("/")
        self._default_headers = tuple(default_headers)

    async def _bearer(self) -> _Headers:
        token = self._token
        if token is None:
            return ()
        if isinstance(token, str):
            value = token
        elif hasattr(token, "token"):
            value = await token.token()          # e.g. ClientCredentials
        elif callable(token):
            value = await token()                # a custom async provider
        else:
            raise TypeError(f"unusable token source: {type(token)!r}")
        return ((b"authorization", f"Bearer {value}".encode("latin-1")),)

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> Any:
        """Send one request to `base_path + path` with the bearer header attached.

        The token is resolved per call, so a refreshing source hands over a
        current credential on every request. Header precedence is bearer, then
        `default_headers`, then `headers`; nothing is de-duplicated, so a
        caller that passes its own `authorization` sends two of them.

        Args:
            path: Origin-relative; a leading `/` is optional and one is inserted if absent.
            idempotency_key: Forwarded to the wrapped client, which sends it as `Idempotency-Key`.

        Returns:
            Whatever the wrapped client returns -- a `ClientResponse` for `HTTPClient`.

        Raises:
            TypeError: The configured token source is none of the three supported shapes.
        """
        target = self._base_path + path if path.startswith("/") else f"{self._base_path}/{path}"
        merged = await self._bearer() + self._default_headers + tuple(headers)
        return await self._client.request(
            method, target, headers=merged, body=body, idempotency_key=idempotency_key
        )

    async def get(self, path: str, *, headers: _Headers = ()) -> Any:
        """`GET` through `request`."""
        return await self.request("GET", path, headers=headers)

    async def post(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"", idempotency_key: str | None = None,
    ) -> Any:
        """`POST` through `request`. The only verb here taking an idempotency key."""
        return await self.request("POST", path, headers=headers, body=body,
                                  idempotency_key=idempotency_key)

    async def put(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        """`PUT` through `request`."""
        return await self.request("PUT", path, headers=headers, body=body)

    async def patch(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        """`PATCH` through `request`."""
        return await self.request("PATCH", path, headers=headers, body=body)

    async def delete(self, path: str, *, headers: _Headers = ()) -> Any:
        """`DELETE` through `request`. Sends no body."""
        return await self.request("DELETE", path, headers=headers)
