"""Bot-challenge verification over Wreath's lifespan-owned HTTP client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from ._http import _is_http_token
from ._json import loads
from .request import Request

__all__ = [
    "BotChallenge",
    "ChallengeRefused",
    "ChallengeResult",
    "Turnstile",
    "challenge_dependency",
]

_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4096


class ChallengeRefused(ValueError):
    """A challenge token was absent, rejected, or valid for the wrong context."""


@dataclass(frozen=True, slots=True)
class ChallengeResult:
    provider: str
    hostname: str | None = None
    action: str | None = None
    challenge_at: datetime | None = None
    cdata: str | None = None


@runtime_checkable
class BotChallenge(Protocol):
    async def verify(self, token: str, request: Request) -> ChallengeResult: ...


class Turnstile:
    """Cloudflare Turnstile siteverify, fail-closed and origin constrained.

    `client` must be a Wreath HTTP client pinned to
    `https://challenges.cloudflare.com` and registered on the application, so
    lifecycle, SSRF policy, timeouts, Flight Recorder capture, and replay are the
    same outbound boundary as every other service call.
    """

    __slots__ = ("_action", "_client", "_hostname", "_secret", "_target")

    def __init__(
        self,
        client: Any,
        *,
        secret: str,
        hostname: str | None = None,
        action: str | None = None,
        target: str = "/turnstile/v0/siteverify",
    ) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("Turnstile secret must be a non-empty string")
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or target.startswith("//")
            or "?" in target
            or "#" in target
            or "\\" in target
            or any(ord(character) <= 0x20 or ord(character) >= 0x7F for character in target)
        ):
            raise ValueError(
                "Turnstile target must be an origin-relative path string such as "
                "'/turnstile/v0/siteverify'"
            )
        if hostname is not None and (not isinstance(hostname, str) or not hostname):
            raise ValueError("Turnstile hostname must be a non-empty string or None")
        if action is not None and (not isinstance(action, str) or not action):
            raise ValueError("Turnstile action must be a non-empty string or None")
        self._client = client
        self._secret = secret
        self._hostname = hostname
        self._action = action
        self._target = target

    async def verify(self, token: str, request: Request) -> ChallengeResult:
        if token == "":
            raise ChallengeRefused("Turnstile token is required")
        if not isinstance(token, str):
            raise ChallengeRefused(
                f"Turnstile token must be an ASCII string of at most {_MAX_TOKEN_BYTES} bytes"
            )
        try:
            token_size = len(token.encode("ascii"))
        except UnicodeError as exc:
            raise ChallengeRefused(
                f"Turnstile token must be an ASCII string of at most {_MAX_TOKEN_BYTES} bytes"
            ) from exc
        if token_size > _MAX_TOKEN_BYTES:
            raise ChallengeRefused(
                f"Turnstile token must be an ASCII string of at most {_MAX_TOKEN_BYTES} bytes"
            )
        form = {"secret": self._secret, "response": token}
        if request.client is not None:
            form["remoteip"] = request.client[0]
        response = await self._client.post(
            self._target,
            headers=((b"content-type", b"application/x-www-form-urlencoded"),),
            body=urlencode(form).encode("ascii"),
        )
        if response.status != 200:
            raise ChallengeRefused(f"Turnstile verification endpoint answered {response.status}")
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ChallengeRefused("Turnstile verification response is too large")
        try:
            payload = loads(response.body)
        except ValueError as exc:
            raise ChallengeRefused("Turnstile verification response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            codes = payload.get("error-codes", ()) if isinstance(payload, dict) else ()
            detail = (
                ",".join(
                    value
                    for value in codes[:8]
                    if isinstance(value, str) and len(value) <= 64 and _is_http_token(value)
                )
                if isinstance(codes, list)
                else ""
            )
            raise ChallengeRefused(
                "Turnstile rejected the token" + (f": {detail}" if detail else "")
            )
        hostname = payload.get("hostname")
        action = payload.get("action")
        if self._hostname is not None and hostname != self._hostname:
            raise ChallengeRefused(
                f"Turnstile token hostname {hostname!r} is not {self._hostname!r}"
            )
        if self._action is not None and action != self._action:
            raise ChallengeRefused(f"Turnstile token action {action!r} is not {self._action!r}")
        challenge_at = None
        raw_time = payload.get("challenge_ts")
        if isinstance(raw_time, str):
            try:
                challenge_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ChallengeRefused("Turnstile challenge_ts is invalid") from exc
        return ChallengeResult(
            provider="turnstile",
            hostname=hostname if isinstance(hostname, str) else None,
            action=action if isinstance(action, str) else None,
            challenge_at=challenge_at,
            cdata=payload.get("cdata") if isinstance(payload.get("cdata"), str) else None,
        )


def challenge_dependency(challenge: BotChallenge, *, header: str = "cf-turnstile-response"):
    """Build a dependency over the canonical bot-challenge protocol."""
    if not callable(getattr(challenge, "verify", None)):
        raise TypeError("bot challenge must expose async verify(token, request)")
    if not isinstance(header, str) or not header:
        raise ValueError("bot-challenge token header must be a non-empty string")
    if not _is_http_token(header):
        raise ValueError("bot-challenge token header must be a valid HTTP field name")
    header_bytes = header.encode("latin-1").lower()

    async def verify(request: Request) -> ChallengeResult:
        try:
            raw_token = request._single_header(header_bytes)
        except ValueError as exc:
            raise ChallengeRefused(
                f"bot-challenge token header {header!r} occurs more than once"
            ) from exc
        if raw_token is None:
            raise ChallengeRefused(f"bot-challenge token header {header!r} is required")
        token = raw_token.decode("latin-1")
        return await challenge.verify(token, request)

    return verify
