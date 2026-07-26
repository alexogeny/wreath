"""A JWKS key cache with single-flight, rate-limited refresh.

Keys are fetched over a lifespan-managed :class:`wreath.http_client.HTTPClient`
pinned to the identity provider's origin, so a hostile ``kid`` can never steer a
fetch elsewhere — ``kid`` is only ever a dictionary key here, never part of a
URL. Refresh-on-unknown-kid is guarded by a single-flight lock plus a negative
cache (a minimum interval between fetches) so a stream of bogus ``kid`` values
cannot be amplified into a request flood against the IdP.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .jwt import JwtKey, key_from_jwk

__all__ = ["JwksCache"]

# Absolute cap on a JWKS document so a hostile endpoint cannot exhaust memory.
_MAX_JWKS_BYTES = 512 * 1024


class JwksCache:
    """Holds parsed JWKS keys and refreshes them on demand."""

    __slots__ = (
        "_client",
        "_expires_at",
        "_jwks_path",
        "_keys",
        "_last_refresh",
        "_lock",
        "_min_refresh",
        "_ttl",
    )

    def __init__(
        self,
        *,
        http_client: Any,
        jwks_path: str,
        ttl: float = 600.0,
        min_refresh_interval: float = 30.0,
    ) -> None:
        self._client = http_client
        self._jwks_path = jwks_path
        self._keys: dict[str, JwtKey] = {}
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._last_refresh = 0.0
        self._ttl = ttl
        self._min_refresh = min_refresh_interval

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _lookup(self, kid: str | None) -> JwtKey | None:
        if kid is not None:
            return self._keys.get(kid)
        # A token without a kid is only unambiguous when the set holds one key.
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    async def resolve(self, kid: str | None) -> JwtKey | None:
        """Return the key for ``kid``, refreshing once if it is unknown/stale."""
        now = self._now()
        key = self._lookup(kid)
        if key is not None and now < self._expires_at:
            return key
        await self._maybe_refresh(kid)
        return self._lookup(kid)

    async def prefetch(self) -> None:
        """Populate the cache at startup so the first request pays nothing."""
        async with self._lock:
            await self._fetch()

    async def _maybe_refresh(self, kid: str | None) -> None:
        async with self._lock:
            now = self._now()
            # Another waiter may have refreshed while we held on the lock.
            if self._lookup(kid) is not None and now < self._expires_at:
                return
            # Negative cache: never re-hit the IdP more than once per interval
            # for an unknown kid once we already hold some keys.
            if self._keys and (now - self._last_refresh) < self._min_refresh:
                return
            await self._fetch()

    async def _fetch(self) -> None:
        response = await self._client.get(self._jwks_path)
        if response.status != 200:
            # Leave the existing keys in place; a transient IdP error must not
            # wipe a working cache.
            return
        body = response.body
        if len(body) > _MAX_JWKS_BYTES:
            raise ValueError("JWKS document exceeds size cap")
        document = json.loads(body)
        keys: dict[str, JwtKey] = {}
        for index, jwk in enumerate(document.get("keys", ())):
            use = jwk.get("use")
            if use not in (None, "sig"):
                continue  # encryption keys are not signing keys
            try:
                key = key_from_jwk(jwk)
            except Exception:  # noqa: BLE001 - skip a single malformed JWK
                continue
            kid = jwk.get("kid")
            keys[kid if isinstance(kid, str) else f"__nokid_{index}"] = key
        if keys:
            self._keys = keys
        self._last_refresh = self._now()
        self._expires_at = self._last_refresh + _ttl_from_headers(response, self._ttl)


def _ttl_from_headers(response: Any, default: float) -> float:
    value = response.header(b"cache-control")
    if not value:
        return default
    for directive in value.split(b","):
        directive = directive.strip().lower()
        if directive.startswith(b"max-age="):
            try:
                return float(int(directive[len(b"max-age=") :]))
            except ValueError:
                return default
    return default
