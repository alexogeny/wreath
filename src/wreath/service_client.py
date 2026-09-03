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

# The bearer-token source may be a static `str`, an async `() -> str` callable,
# or anything with an awaitable `.token()` (e.g. ClientCredentials).
_Headers = tuple[tuple[bytes, bytes], ...]


def _check_token(value: str, source: str) -> str:
    """Refuse a token that cannot be a header value, naming what is wrong.

    RFC 6750's `b64token` is ASCII, and RFC 9110 field values are printable
    ASCII, so a token outside that range is not a bearer token -- there is no
    encoding that makes it one. Guarding here rather than at the `encode` call
    is what makes the three failures distinguishable:

    * `\\r` or `\\n` in a token spliced whatever followed it into the request as
      a *separate header*. Nothing downstream validated it. That is header
      injection, and silently encoding it would have shipped the attack.
    * U+0080-U+00FF encoded to single bytes no peer decodes back, so the request
      failed as a 401 that reads like bad credentials rather than bad bytes.
    * Anything above U+00FF raised `UnicodeEncodeError` from inside `encode`,
      undocumented and pointing at the wrong layer.

    Args:
        value: The resolved token text.
        source: How it was obtained, for the message -- callers name themselves
            because a refreshing provider's token has no other traceable origin.

    Returns:
        `value` unchanged, so this reads as a guard at the point of use.

    Raises:
        ValueError: The token holds a character no header value may carry.
    """
    for index, char in enumerate(value):
        if char.isascii() and char.isprintable():
            continue
        raise ValueError(
            f"bearer token from {source} contains {char!r} at position {index}, "
            "which cannot appear in an Authorization header -- RFC 6750 tokens "
            "are printable ASCII. A carriage return or newline here would splice "
            "a second header into the request"
        )
    return value


def _authorization_count(headers: _Headers) -> int:
    return sum(name.lower() == b"authorization" for name, _value in headers)


def _validate_path(path: str, *, label: str) -> None:
    path_only = path.partition("?")[0]
    lowered = path_only.lower()
    if any(escape in lowered for escape in ("%2e", "%2f", "%5c")):
        raise ValueError(f"{label} contains an encoded separator or dot segment")
    if "\\" in path_only or any(part in {".", ".."} for part in path_only.split("/")):
        raise ValueError(f"{label} contains a dot segment or backslash separator")


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
    the first request rather than at construction. A resolved token that is not
    printable ASCII is refused with `ValueError` naming the character and its
    position -- a token carrying `\\r` or `\\n` would otherwise splice a second
    header into the request.

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
        default_authorizations = _authorization_count(default_headers)
        if default_authorizations > 1:
            raise ValueError(
                "ServiceClient default_headers must not contain more than one Authorization header"
            )
        if token is not None and default_authorizations:
            raise ValueError(
                "ServiceClient default_headers must not contain Authorization "
                "when token supplies the service credential"
            )
        _validate_path(base_path, label="ServiceClient base_path")
        self._client = client
        self._token = token
        self._base_path = base_path.rstrip("/")
        self._default_headers = tuple(default_headers)

    async def _bearer(self) -> _Headers:
        token = self._token
        if token is None:
            return ()
        if isinstance(token, str):
            value = _check_token(token, "the configured string")
        elif hasattr(token, "token"):
            value = _check_token(await token.token(), f"{type(token).__name__}.token()")
        elif callable(token):
            value = _check_token(await token(), "the configured callable")
        else:
            raise TypeError(f"unusable token source: {type(token)!r}")
        return ((b"authorization", f"Bearer {value}".encode("ascii")),)

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
        current credential on every request. Ambiguous `Authorization`
        combinations are refused; a lone caller-supplied value is accepted only
        when the client has no configured credential.

        Args:
            path: Origin-relative; a leading `/` is optional and one is inserted if absent.
            idempotency_key: Forwarded to the wrapped client, which sends it as `Idempotency-Key`.

        Returns:
            Whatever the wrapped client returns -- a `ClientResponse` for `HTTPClient`.

        Raises:
            TypeError: The configured token source is none of the three supported shapes.
            ValueError: The resolved token is not printable ASCII, so it cannot be
                carried in an `Authorization` header.
        """
        request_authorizations = _authorization_count(headers)
        if request_authorizations > 1:
            raise ValueError(
                "ServiceClient request headers must not contain more than one Authorization header"
            )
        if request_authorizations and (
            self._token is not None or _authorization_count(self._default_headers)
        ):
            raise ValueError(
                "ServiceClient request headers must not contain Authorization; "
                "configure the credential on the client"
            )
        _validate_path(path, label="ServiceClient request path")
        target = self._base_path + path if path.startswith("/") else f"{self._base_path}/{path}"
        merged = await self._bearer() + self._default_headers + tuple(headers)
        return await self._client.request(
            method, target, headers=merged, body=body, idempotency_key=idempotency_key
        )

    async def get(self, path: str, *, headers: _Headers = ()) -> Any:
        """`GET` through `request`."""
        return await self.request("GET", path, headers=headers)

    async def post(
        self,
        path: str,
        *,
        headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> Any:
        """`POST` through `request`. The only verb here taking an idempotency key."""
        return await self.request(
            "POST", path, headers=headers, body=body, idempotency_key=idempotency_key
        )

    async def put(
        self,
        path: str,
        *,
        headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        """`PUT` through `request`."""
        return await self.request("PUT", path, headers=headers, body=body)

    async def patch(
        self,
        path: str,
        *,
        headers: _Headers = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> Any:
        """`PATCH` through `request`."""
        return await self.request("PATCH", path, headers=headers, body=body)

    async def delete(self, path: str, *, headers: _Headers = ()) -> Any:
        """`DELETE` through `request`. Sends no body."""
        return await self.request("DELETE", path, headers=headers)
