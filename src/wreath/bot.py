"""Bot-challenge verification over Wreath's lifespan-owned HTTP client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from ._json import loads
from .request import Request

__all__ = [
    "BotChallenge",
    "ChallengeRefused",
    "ChallengeResult",
    "Turnstile",
    "challenge_dependency",
]


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
        if not secret:
            raise ValueError("Turnstile secret cannot be empty")
        if not target.startswith("/") or target.startswith("//"):
            raise ValueError("Turnstile target must be an origin-relative path")
        self._client = client
        self._secret = secret
        self._hostname = hostname
        self._action = action
        self._target = target

    async def verify(self, token: str, request: Request) -> ChallengeResult:
        if not token:
            raise ChallengeRefused("Turnstile token is required")
        form = {"secret": self._secret, "response": token}
        if request.client is not None:
            form["remoteip"] = request.client[0]
        response = await self._client.post(
            self._target,
            headers=((b"content-type", b"application/x-www-form-urlencoded"),),
            body=urlencode(form).encode("ascii"),
        )
        if response.status != 200:
            raise ChallengeRefused(
                f"Turnstile verification endpoint answered {response.status}"
            )
        try:
            payload = loads(response.body)
        except ValueError as exc:
            raise ChallengeRefused("Turnstile verification response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            codes = payload.get("error-codes", ()) if isinstance(payload, dict) else ()
            detail = ",".join(str(value) for value in codes)
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
            raise ChallengeRefused(
                f"Turnstile token action {action!r} is not {self._action!r}"
            )
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


def challenge_dependency(
    challenge: BotChallenge, *, header: str = "cf-turnstile-response"
):
    """Build a dependency over the canonical bot-challenge protocol."""
    if not callable(getattr(challenge, "verify", None)):
        raise TypeError("bot challenge must expose async verify(token, request)")
    if not header:
        raise ValueError("bot-challenge token header cannot be empty")

    async def verify(request: Request) -> ChallengeResult:
        token = request.header(header)
        if token is None:
            raise ChallengeRefused(
                f"bot-challenge token header {header!r} is required"
            )
        return await challenge.verify(token, request)

    return verify
