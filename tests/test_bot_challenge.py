from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wreath._json import dumps
from wreath.bot import (
    ChallengeRefused,
    ChallengeResult,
    Turnstile,
    challenge_dependency,
)
from wreath.http_client import ClientResponse
from wreath.request import Request


async def receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


class Client:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, tuple[tuple[bytes, bytes], ...], bytes]] = []

    async def post(self, target: str, *, headers, body) -> ClientResponse:
        self.calls.append((target, headers, body))
        return ClientResponse(200, (), dumps(self.payload), "1.1")


@pytest.mark.asyncio
async def test_turnstile_validates_origin_action_and_forwards_trusted_client() -> None:
    client = Client({
        "success": True,
        "hostname": "app.example",
        "action": "signup",
        "challenge_ts": "2026-08-13T01:02:03Z",
    })
    request = Request(
        {"type": "http", "client": ("203.0.113.8", 1234), "headers": []}, receive
    )
    result = await Turnstile(
        client, secret="secret", hostname="app.example", action="signup"
    ).verify("token", request)
    assert result.challenge_at == datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
    assert b"remoteip=203.0.113.8" in client.calls[0][2]


@pytest.mark.asyncio
async def test_turnstile_fails_closed_on_action_mismatch() -> None:
    client = Client({"success": True, "hostname": "app.example", "action": "login"})
    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused, match="action"):
        await Turnstile(client, secret="secret", action="signup").verify("token", request)


@pytest.mark.asyncio
async def test_turnstile_accepts_a_success_without_an_optional_challenge_time() -> None:
    client = Client({"success": True})
    request = Request({"type": "http", "headers": []}, receive)

    result = await Turnstile(client, secret="secret").verify("token", request)

    assert result.challenge_at is None


@pytest.mark.asyncio
async def test_dependency_layers_over_the_bot_challenge_protocol() -> None:
    seen: list[str] = []

    class Challenge:
        async def verify(self, token: str, request: Request) -> ChallengeResult:
            seen.append(token)
            return ChallengeResult(provider="custom")

    dependency = challenge_dependency(Challenge(), header="x-challenge")
    request = Request(
        {"type": "http", "headers": [(b"x-challenge", b"proof")]}, receive
    )
    assert (await dependency(request)).provider == "custom"
    assert seen == ["proof"]
