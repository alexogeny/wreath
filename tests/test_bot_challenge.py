from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from wreath._json import dumps
from wreath.bot import (
    BotChallenge,
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
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[tuple[str, tuple[tuple[bytes, bytes], ...], bytes]] = []

    async def post(self, target: str, *, headers, body) -> ClientResponse:
        self.calls.append((target, headers, body))
        return ClientResponse(self.status, (), dumps(self.payload), "1.1")


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
    assert result.hostname == "app.example"
    assert result.action == "signup"
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

    assert result == ChallengeResult(provider="turnstile")


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


@pytest.mark.parametrize("secret", [1, ""], ids=("non-string", "empty"))
def test_turnstile_refuses_each_invalid_secret(secret: object) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        Turnstile(Client({}), secret=cast("str", secret))


@pytest.mark.parametrize(
    "target",
    [None, "relative", "//other.example/path"],
    ids=("non-string", "relative", "scheme-relative"),
)
def test_turnstile_refuses_each_non_origin_relative_target(target: object) -> None:
    with pytest.raises(ValueError, match="origin-relative path string"):
        Turnstile(Client({}), secret="secret", target=cast("str", target))


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("hostname", ""), ("hostname", 1), ("action", ""), ("action", 1)],
    ids=("empty-hostname", "typed-hostname", "empty-action", "typed-action"),
)
def test_turnstile_refuses_invalid_optional_constraints(keyword: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"Turnstile {keyword}"):
        if keyword == "hostname":
            Turnstile(Client({}), secret="secret", hostname=cast("str", value))
        else:
            Turnstile(Client({}), secret="secret", action=cast("str", value))


@pytest.mark.asyncio
async def test_turnstile_refuses_an_empty_token_and_non_success_status() -> None:
    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused, match="token is required"):
        await Turnstile(Client({}), secret="secret").verify("", request)
    with pytest.raises(ChallengeRefused, match="answered 503"):
        await Turnstile(Client({}, status=503), secret="secret").verify("token", request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [[], {"success": False}], ids=("non-object", "failure")
)
async def test_turnstile_refuses_each_unsuccessful_payload(payload: object) -> None:
    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused, match="rejected the token"):
        await Turnstile(Client(payload), secret="secret").verify("token", request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"success": False}, "Turnstile rejected the token"),
        (
            {"success": False, "error-codes": ["timeout", "bad-input"]},
            "Turnstile rejected the token: timeout,bad-input",
        ),
    ],
    ids=("without-detail", "with-detail"),
)
async def test_turnstile_rejection_message_preserves_provider_detail(
    payload: dict[str, object], message: str
) -> None:
    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused) as caught:
        await Turnstile(Client(payload), secret="secret").verify("token", request)
    assert str(caught.value) == message


@pytest.mark.asyncio
async def test_turnstile_refuses_non_json_and_invalid_timestamp() -> None:
    class InvalidJsonClient:
        async def post(self, target: str, *, headers, body) -> ClientResponse:
            return ClientResponse(200, (), b"not-json", "1.1")

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused, match="not JSON"):
        await Turnstile(InvalidJsonClient(), secret="secret").verify("token", request)
    with pytest.raises(ChallengeRefused, match="challenge_ts is invalid"):
        await Turnstile(
            Client({"success": True, "challenge_ts": "yesterday"}), secret="secret"
        ).verify("token", request)


@pytest.mark.asyncio
async def test_turnstile_preserves_unconstrained_string_metadata() -> None:
    request = Request({"type": "http", "headers": []}, receive)
    result = await Turnstile(
        Client({"success": True, "hostname": "edge.example", "action": "login", "cdata": "opaque"}),
        secret="secret",
    ).verify("token", request)
    assert result.hostname == "edge.example"
    assert result.action == "login"
    assert result.cdata == "opaque"


@pytest.mark.asyncio
async def test_turnstile_discards_non_string_optional_metadata() -> None:
    request = Request({"type": "http", "headers": []}, receive)
    result = await Turnstile(
        Client({"success": True, "hostname": 1, "action": 2, "cdata": 3}),
        secret="secret",
    ).verify("token", request)
    assert result == ChallengeResult(provider="turnstile")


@pytest.mark.asyncio
async def test_turnstile_refuses_a_hostname_mismatch() -> None:
    request = Request({"type": "http", "headers": []}, receive)
    challenge = Turnstile(
        Client({"success": True, "hostname": "other.example"}),
        secret="secret",
        hostname="app.example",
    )
    with pytest.raises(ChallengeRefused, match="hostname"):
        await challenge.verify("token", request)


def test_challenge_dependency_refuses_invalid_declarations() -> None:
    with pytest.raises(TypeError, match="async verify"):
        challenge_dependency(cast("BotChallenge", object()))

    class Challenge:
        async def verify(self, token: str, request: Request) -> ChallengeResult:
            return ChallengeResult(provider="custom")

    for header in (1, ""):
        with pytest.raises(ValueError, match="non-empty string"):
            challenge_dependency(Challenge(), header=cast("str", header))


@pytest.mark.asyncio
async def test_challenge_dependency_refuses_a_missing_header() -> None:
    class Challenge:
        async def verify(self, token: str, request: Request) -> ChallengeResult:
            return ChallengeResult(provider="custom")

    dependency = challenge_dependency(Challenge(), header="x-challenge")
    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ChallengeRefused, match="x-challenge"):
        await dependency(request)
