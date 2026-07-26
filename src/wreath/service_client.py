"""A thin, auth-aware wrapper around :class:`wreath.http_client.Client`.

Calling another service usually means the same two chores on every request: put a
base path in front of the target, and attach a bearer token that has to be kept
fresh. :class:`ServiceClient` binds both once so call sites read like the API:

    from wreath.service_client import ServiceClient
    from wreath._auth.oauth2 import ClientCredentials

    billing = ServiceClient(
        http_client,                                   # an origin-pinned Client
        base_path="/billing/v1",
        token=ClientCredentials(http_client=http_client, token_path="/oauth/token",
                                client_id=..., client_secret=...),
    )
    invoice = await (await billing.get(f"/invoices/{id}")).json()

The token source is auto-refreshing: a :class:`ClientCredentials` caches and
renews the M2M token before it expires, so ``ServiceClient`` just asks for the
current one per request. A plain ``str`` (static token) or a zero-arg async
callable (your own refresh logic) work too.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ServiceClient"]

# The bearer-token source may be a static ``str``, an async ``() -> str`` callable,
# or anything with an awaitable ``.token()`` (e.g. ClientCredentials).
_Headers = tuple[tuple[bytes, bytes], ...]


class ServiceClient:
    """An HTTP client bound to a base path and a (refreshing) bearer token."""

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
        target = self._base_path + path if path.startswith("/") else f"{self._base_path}/{path}"
        merged = await self._bearer() + self._default_headers + tuple(headers)
        return await self._client.request(
            method, target, headers=merged, body=body, idempotency_key=idempotency_key
        )

    async def get(self, path: str, *, headers: _Headers = ()) -> Any:
        return await self.request("GET", path, headers=headers)

    async def post(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"", idempotency_key: str | None = None,
    ) -> Any:
        return await self.request("POST", path, headers=headers, body=body,
                                  idempotency_key=idempotency_key)

    async def put(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        return await self.request("PUT", path, headers=headers, body=body)

    async def patch(
        self, path: str, *, headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        return await self.request("PATCH", path, headers=headers, body=body)

    async def delete(self, path: str, *, headers: _Headers = ()) -> Any:
        return await self.request("DELETE", path, headers=headers)
