"""A JWKS key cache with single-flight, rate-limited refresh.

Keys are fetched over a lifespan-managed `wreath.http_client.HTTPClient`
pinned to the identity provider's origin, so a hostile `kid` can never steer a
fetch elsewhere — `kid` is only ever a dictionary key here, never part of a
URL. Refresh-on-unknown-kid is guarded by a single-flight lock plus a negative
cache (a minimum interval between fetches) so a stream of bogus `kid` values
cannot be amplified into a request flood against the IdP.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from .jwt import JwtError, JwtKey, key_from_jwk

__all__ = ["JwksCache"]

# Absolute cap on a JWKS document so a hostile endpoint cannot exhaust memory.
_MAX_JWKS_BYTES = 512 * 1024

#: Bounds on the cache lifetime a provider may ask for through `Cache-Control`.
#: Unclamped, `max-age=31536000` pinned a rotated -- or withdrawn -- signing key
#: for a year, and `max-age=0` turned every unknown kid into a fetch. The floor
#: is also what makes the negative cache meaningful.
_MIN_TTL = 60.0
_MAX_TTL = 24 * 60 * 60.0


class JwksCache:
    """Holds parsed JWKS keys and refreshes them on demand."""

    __slots__ = (
        "duplicate_kids",
        "empty_documents",
        "fetch_errors",
        "malformed_keys",
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
        #: Fetches that produced no usable document (too large, unparseable).
        self.fetch_errors = 0
        #: JWKs skipped for reusing a `kid` already seen in the same document.
        self.duplicate_kids = 0
        #: JWKs skipped for not being an object, or for failing to parse. A
        #: provider that starts serving junk shows up here rather than as keys
        #: that quietly stopped resolving.
        self.malformed_keys = 0
        #: Refreshes that read a 200 and found no usable signing key in it. The
        #: cache is cleared on one, so this is the count of *revocations* --
        #: distinct from `fetch_errors`, which never clears anything.
        self.empty_documents = 0

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
        """Return the key for `kid`, refreshing once if it is unknown/stale."""
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
            # wipe a working cache. The attempt still counts as a refresh, so
            # the negative cache holds: without this, once `min_refresh` had
            # elapsed since the last *success*, every request carrying an
            # unknown kid hit a provider that was already failing.
            self._last_refresh = self._now()
            return
        body = response.body
        if len(body) > _MAX_JWKS_BYTES:
            # Counted and dropped rather than raised: this runs inside
            # `resolve`, on the authentication path, so raising turned a hostile
            # or misconfigured endpoint into a 500 where a 401 was the answer.
            self.fetch_errors += 1
            self._last_refresh = self._now()
            return
        try:
            document = json.loads(body)
        except ValueError:
            self.fetch_errors += 1
            self._last_refresh = self._now()
            return
        keys: dict[str, JwtKey] = {}
        for index, jwk in enumerate(document.get("keys", ())):
            if not isinstance(jwk, Mapping):
                # `keys` is meant to hold objects. A string or number in there
                # used to reach `.get` and raise out of the whole refresh --
                # one junk entry discarded every valid key after it, because the
                # guard below started one line too late.
                self.malformed_keys += 1
                continue
            use = jwk.get("use")
            if use not in (None, "sig"):
                continue  # encryption keys are not signing keys
            try:
                key = key_from_jwk(jwk)
            except (JwtError, KeyError, ValueError, TypeError):
                # The measured set for a Mapping input: JwtError (and its
                # UnsupportedAlgorithm subclass) for a rejected key, KeyError
                # for a missing member, ValueError for bad base64url, TypeError
                # for a non-string member. A malformed JWK is skipped; anything
                # else is a bug in the parser and must not be swallowed here.
                self.malformed_keys += 1
                continue
            kid = jwk.get("kid")
            name = kid if isinstance(kid, str) else f"__nokid_{index}"
            if name in keys:
                # Two JWKs claiming one kid: the document is ambiguous about
                # which key signs a token naming it, and silently keeping the
                # last one picks an answer the issuer did not give.
                self.duplicate_kids += 1
                continue
            keys[name] = key
        if not keys:
            # **Zero usable keys is an answer, not a failure**, and the two must
            # not share a branch. The `status != 200` path above retains the
            # cache deliberately, because a transient IdP error must not wipe a
            # working one; a 200 carrying `{"keys": []}` is the issuer saying
            # every key it had is withdrawn, and retaining them turns a
            # revocation into a no-op for the process lifetime. The same state
            # is reached by a document that is all `use:enc`, all malformed, or
            # all duplicate `kid` -- in each case the issuer served something
            # readable and none of it signs anything.
            #
            # Counted, because the previous behaviour's whole problem was that
            # it was silent: `fetch_errors` and `malformed_keys` both stayed at
            # zero while authentication carried on against withdrawn keys.
            self.empty_documents += 1
        self._keys = keys
        self._last_refresh = self._now()
        self._expires_at = self._last_refresh + _ttl_from_headers(response, self._ttl)


def _ttl_from_headers(response: Any, default: float) -> float:
    value = response.header(b"cache-control")
    if not value:
        return default
    for raw_directive in value.split(b","):
        directive = raw_directive.strip().lower()
        if directive.startswith(b"max-age="):
            try:
                seconds = float(int(directive[len(b"max-age=") :]))
            except ValueError:
                return default
            # The provider is advising, not instructing: this cache holds the
            # keys that decide who is authenticated.
            return min(max(seconds, _MIN_TTL), _MAX_TTL)
    return default
