from __future__ import annotations

import json

import pytest

from .support import Clock, HTTPClient, HTTPResponse, discord


@pytest.mark.asyncio
async def test_command_defer_is_type_five_and_ephemeral_is_fixed_on_initial_response() -> None:
    api = discord()
    client = HTTPClient()
    responder = api.DiscordResponder(
        application_id="app-1",
        interaction_id="interaction-1",
        token="token-1",
        received_at=1_000.0,
        client=client,
        clock=lambda: 1_001.0,
    )

    callback = await responder.defer(ephemeral=True)

    assert callback == {"type": 5, "data": {"flags": 64}}
    assert client.requests == []
    with pytest.raises(api.InteractionAlreadyAcknowledged):
        await responder.defer(ephemeral=False)


@pytest.mark.asyncio
async def test_component_defer_uses_update_type_six_without_message_data() -> None:
    api = discord()
    responder = api.DiscordResponder.for_component(
        application_id="app-1",
        interaction_id="interaction-1",
        token="token-1",
        received_at=1_000.0,
        client=HTTPClient(),
        clock=lambda: 1_001.0,
    )

    assert await responder.defer_update() == {"type": 6}


@pytest.mark.asyncio
async def test_followup_and_edits_use_discords_exact_webhook_routes() -> None:
    api = discord()
    client = HTTPClient()
    responder = api.DiscordResponder(
        application_id="app-1",
        interaction_id="interaction-1",
        token="token-1",
        received_at=1_000.0,
        client=client,
        clock=lambda: 1_010.0,
        acknowledged=True,
    )

    await responder.edit_original(content="working")
    await responder.followup(content="done", ephemeral=True)
    await responder.edit_followup("message-9", content="really done")

    assert [(method, path, json.loads(call["body"])) for method, path, call in client.requests] == [
        (
            "PATCH",
            "/webhooks/app-1/token-1/messages/@original",
            {"content": "working"},
        ),
        ("POST", "/webhooks/app-1/token-1", {"content": "done", "flags": 64}),
        (
            "PATCH",
            "/webhooks/app-1/token-1/messages/message-9",
            {"content": "really done"},
        ),
    ]


@pytest.mark.asyncio
async def test_webhook_identifiers_cannot_escape_their_path_segments() -> None:
    api = discord()
    client = HTTPClient()
    responder = api.DiscordResponder(
        application_id="app/../victim",
        interaction_id="interaction-1",
        token="token?admin=true#fragment",
        received_at=1_000.0,
        client=client,
        clock=lambda: 1_010.0,
        acknowledged=True,
    )

    await responder.edit_followup("../@original", content="bounded")

    assert client.requests[0][1] == (
        "/webhooks/app%2F..%2Fvictim/token%3Fadmin%3Dtrue%23fragment/"
        "messages/..%2F%40original"
    )


@pytest.mark.asyncio
async def test_expired_acknowledgement_and_interaction_token_refuse_locally() -> None:
    api = discord()
    client = HTTPClient()
    late_ack = api.DiscordResponder(
        application_id="app-1",
        interaction_id="interaction-1",
        token="token-1",
        received_at=1_000.0,
        client=client,
        clock=lambda: 1_003.001,
    )
    expired = api.DiscordResponder(
        application_id="app-1",
        interaction_id="interaction-1",
        token="token-1",
        received_at=1_000.0,
        client=client,
        clock=lambda: 1_900.001,
        acknowledged=True,
    )

    with pytest.raises(api.InteractionAcknowledgementExpired):
        await late_ack.defer()
    with pytest.raises(api.InteractionTokenExpired):
        await expired.followup(content="too late")
    assert client.requests == []


@pytest.mark.asyncio
async def test_rate_limits_learn_shared_bucket_identity_and_major_resource_at_runtime() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    exhausted = HTTPResponse(
        200,
        {
            "X-RateLimit-Bucket": "runtime-bucket",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "2.5",
        },
    )
    await limiter.observe(
        "DELETE /channels/{channel_id}/messages/{message_id}",
        "channel-1",
        HTTPResponse(
            200,
            {
                "X-RateLimit-Bucket": "runtime-bucket",
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset-After": "2.5",
            },
        ),
    )
    await limiter.observe("POST /channels/{channel_id}/messages", "channel-1", exhausted)

    await limiter.acquire("DELETE /channels/{channel_id}/messages/{message_id}", "channel-1")
    await limiter.acquire("POST /channels/{channel_id}/messages", "channel-2")

    assert clock.sleeps == [2.5]


@pytest.mark.asyncio
async def test_429_retry_after_and_global_scope_gate_future_requests() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe(
        "POST /channels/{channel_id}/messages",
        "channel-1",
        HTTPResponse(
            429,
            {"Retry-After": "1.75", "X-RateLimit-Global": "true"},
            {"retry_after": 1.75, "global": True},
        ),
    )

    await limiter.acquire("GET /users/@me", None)

    assert clock.sleeps == [1.75]


def test_rate_limit_values_are_not_declared_as_static_route_constants() -> None:
    api = discord()

    assert not hasattr(api.DiscordRateLimiter, "ROUTE_LIMITS")
