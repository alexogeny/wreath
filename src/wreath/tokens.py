"""Purpose-scoped application action tokens with rotation and optional replay refusal."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from . import _json
from ._b64 import b64url_decode, b64url_encode
from ._capability_map import CapabilityMap

MAX_TOKEN_BYTES = 4096
MIN_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class TokenPurpose:
    """Policy for one action such as `verify_email` or `invite_accept`."""

    name: str
    ttl: int
    single_use: bool = False

    def __post_init__(self) -> None:
        if not self.name or len(self.name.encode("utf-8")) > 128:
            raise ValueError("TokenPurpose name must be between 1 and 128 UTF-8 bytes")
        if isinstance(self.ttl, bool) or not isinstance(self.ttl, int) or self.ttl < 1:
            raise ValueError(f"TokenPurpose {self.name!r} ttl must be a positive integer")
        if not isinstance(self.single_use, bool):
            raise ValueError(f"TokenPurpose {self.name!r} single_use must be a boolean")


@dataclass(frozen=True, slots=True)
class TokenClaims:
    purpose: str
    subject: str
    issued_at: int
    expires_at: int
    bound: str
    key_id: str


class MemoryTokenLedger:
    """Bounded in-process ledger for single-use action tokens.

    Use an application-owned shared ledger when consumption must span workers.
    This class is explicit about its ownership instead of presenting local
    memory as distributed replay protection.
    """

    __slots__ = ("_entries",)

    def __init__(self, *, max_entries: int = 10_000) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("MemoryTokenLedger max_entries must be a positive integer")
        self._entries = CapabilityMap(max_entries=max_entries, overflow="refuse")

    def register(self, token_id: str, *, ttl: int, now: float) -> bool:
        return self._entries.claim(token_id, ttl=ttl, now=now)

    def consume(self, token_id: str, *, now: float) -> bool:
        return self._entries.consume(token_id, now=now) is not None


class TokenLedger(Protocol):
    """Application-owned storage contract for single-use token nonces."""

    def register(self, token_id: str, *, ttl: int, now: float) -> bool: ...

    def consume(self, token_id: str, *, now: float) -> bool: ...


class ActionTokens:
    """Issue and verify bounded, versioned, purpose-scoped action tokens."""

    __slots__ = ("_clock", "_current", "_keys", "_ledger", "_purposes", "max_token_bytes")

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        current: str,
        purposes: Iterable[TokenPurpose],
        ledger: TokenLedger | None = None,
        clock: Callable[[], float] = time.time,
        max_token_bytes: int = MAX_TOKEN_BYTES,
    ) -> None:
        copied: dict[str, bytes] = {}
        for key_id, secret in keys.items():
            if not isinstance(key_id, str) or not key_id or len(key_id.encode("utf-8")) > 64:
                raise ValueError(
                    "ActionTokens key ids must be non-empty strings of at most 64 bytes"
                )
            if not isinstance(secret, bytes) or len(secret) < MIN_KEY_BYTES:
                raise ValueError(
                    f"ActionTokens key {key_id!r} must be bytes of at least {MIN_KEY_BYTES} bytes"
                )
            copied[key_id] = secret
        if current not in copied:
            raise ValueError(f"ActionTokens current key {current!r} is not present in keys")
        declared: dict[str, TokenPurpose] = {}
        for purpose in purposes:
            if not isinstance(purpose, TokenPurpose):
                raise TypeError(f"ActionTokens purposes must contain TokenPurpose, got {purpose!r}")
            if purpose.name in declared:
                raise ValueError(f"ActionTokens purpose {purpose.name!r} is declared twice")
            declared[purpose.name] = purpose
        if not declared:
            raise ValueError("ActionTokens needs at least one TokenPurpose")
        if any(item.single_use for item in declared.values()) and ledger is None:
            raise ValueError(
                "ActionTokens has a single-use purpose but no ledger; pass MemoryTokenLedger "
                "or an application-owned ledger with register() and consume()"
            )
        if (
            not isinstance(max_token_bytes, int)
            or max_token_bytes < 128
            or max_token_bytes > MAX_TOKEN_BYTES
        ):
            raise ValueError(
                f"ActionTokens max_token_bytes must be an integer from 128 to {MAX_TOKEN_BYTES}"
            )
        self._keys = copied
        self._current = current
        self._purposes = declared
        self._ledger = ledger
        self._clock = clock
        self.max_token_bytes = max_token_bytes

    def issue(
        self,
        purpose: str,
        subject: str,
        *,
        bound: str = "",
        now: float | None = None,
    ) -> str:
        """Issue a token, registering its nonce first when the purpose is single-use."""
        policy = self._purpose(purpose)
        if not isinstance(subject, str) or not subject or len(subject.encode("utf-8")) > 1024:
            raise ValueError(
                "Action token subject must be a non-empty string of at most 1024 bytes"
            )
        if not isinstance(bound, str) or len(bound.encode("utf-8")) > 1024:
            raise ValueError("Action token bound value must be a string of at most 1024 bytes")
        issued = int(self._clock() if now is None else now)
        expires = issued + policy.ttl
        token_id = b64url_encode(secrets.token_bytes(16))
        ledger = self._ledger
        if policy.single_use:
            if ledger is None:
                raise RuntimeError("single-use ActionTokens lost its declared ledger")
            if not ledger.register(token_id, ttl=policy.ttl, now=float(issued)):
                raise RuntimeError("Action token ledger is full; no token was issued")
        payload = _json.dumps(
            {
                "v": 1,
                "k": self._current,
                "p": purpose,
                "s": subject,
                "iat": issued,
                "exp": expires,
                "b": bound,
                "j": token_id,
            }
        )
        body = b64url_encode(payload)
        mac = b64url_encode(hmac.digest(self._keys[self._current], body.encode("ascii"), "sha256"))
        token = f"w1.{body}.{mac}"
        if len(token.encode("ascii")) > self.max_token_bytes:
            if policy.single_use:
                if ledger is None:
                    raise RuntimeError("single-use ActionTokens lost its declared ledger")
                ledger.consume(token_id, now=float(issued))
            raise ValueError(
                f"Action token is longer than max_token_bytes={self.max_token_bytes}; "
                "shorten subject or bound"
            )
        return token

    def verify(
        self, purpose: str, token: str, *, bound: str = "", now: float | None = None
    ) -> TokenClaims | None:
        """Return verified claims, or `None` for every invalid token shape."""
        policy = self._purpose(purpose)
        if not isinstance(token, str):
            return None
        try:
            token_size = len(token.encode("utf-8"))
        except UnicodeError:
            return None
        if token_size > self.max_token_bytes:
            return None
        current = int(self._clock() if now is None else now)
        try:
            prefix, body, supplied_mac = token.split(".")
            if prefix != "w1":
                return None
            payload = _json.loads(b64url_decode(body))
            if not isinstance(payload, dict) or set(payload) != {
                "v",
                "k",
                "p",
                "s",
                "iat",
                "exp",
                "b",
                "j",
            }:
                return None
            key_id = payload["k"]
            key = self._keys.get(key_id)
            if key is None:
                return None
            expected = hmac.digest(key, body.encode("ascii"), "sha256")
            if not hmac.compare_digest(b64url_decode(supplied_mac), expected):
                return None
            issued = payload["iat"]
            expires = payload["exp"]
            subject = payload["s"]
            token_id = payload["j"]
            token_bound = payload["b"]
            if (
                payload["v"] != 1
                or payload["p"] != purpose
                or not isinstance(token_bound, str)
                or len(token_bound.encode("utf-8")) > 1024
                or token_bound != bound
                or isinstance(issued, bool)
                or not isinstance(issued, int)
                or isinstance(expires, bool)
                or not isinstance(expires, int)
                or expires != issued + policy.ttl
                or current < issued
                or current >= expires
                or not isinstance(subject, str)
                or not subject
                or len(subject.encode("utf-8")) > 1024
                or not isinstance(token_id, str)
                or len(token_id) != 22
            ):
                return None
        except TypeError, ValueError, UnicodeError:
            return None
        if policy.single_use:
            ledger = self._ledger
            if ledger is None:
                raise RuntimeError("single-use ActionTokens lost its declared ledger")
            if not ledger.consume(token_id, now=float(current)):
                return None
        return TokenClaims(purpose, subject, issued, expires, bound, key_id)

    def _purpose(self, name: str) -> TokenPurpose:
        try:
            return self._purposes[name]
        except KeyError, TypeError:
            raise ValueError(
                f"ActionTokens purpose {name!r} is not declared; declare it with TokenPurpose"
            ) from None


def token_fingerprint(token: str) -> str:
    """A log-safe stable identifier; never returns token material."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "MAX_TOKEN_BYTES",
    "MIN_KEY_BYTES",
    "ActionTokens",
    "MemoryTokenLedger",
    "TokenClaims",
    "TokenLedger",
    "TokenPurpose",
    "token_fingerprint",
]
