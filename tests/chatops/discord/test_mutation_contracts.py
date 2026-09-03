from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wreath import Wreath
from wreath.jobs import JobContext
from wreath.testing import TestClient

from .support import (
    Clock,
    HTTPClient,
    HTTPResponse,
    chatops,
    command_payload,
    component_payload,
    discord,
)


def signed(payload: Any) -> tuple[bytes, str, str, bytes]:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = "1700000000"
    signature = private.sign(timestamp.encode() + body).hex()
    return body, timestamp, signature, private.public_key().public_bytes_raw()


async def post_signed(app: Wreath, payload: Any) -> Any:
    body, timestamp, signature, _key = signed(payload)
    return await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=body,
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        },
    )


def test_verifier_accepts_hex_key_and_refuses_invalid_key_forms() -> None:
    api = discord()
    verifier = api.DiscordInteractionVerifier(bytes(32).hex(), verify=lambda *_args: True)
    verifier.verify(signature=bytes(64).hex(), timestamp="1", body=b"{}")

    with pytest.raises(api.DiscordConfigurationError, match="raw bytes or hex"):
        api.DiscordInteractionVerifier("not-hex")
    with pytest.raises(api.DiscordConfigurationError, match="exactly 32"):
        api.DiscordInteractionVerifier(bytes(31))


@pytest.mark.parametrize("max_age", [-1.0, 0.0, float("nan"), float("inf")])
def test_verifier_refuses_invalid_signature_windows(max_age: float) -> None:
    api = discord()

    with pytest.raises(api.DiscordConfigurationError, match="positive and finite"):
        api.DiscordInteractionVerifier(bytes(32), max_age=max_age)


@pytest.mark.parametrize(
    ("signature", "timestamp"),
    [(bytes(63).hex(), "1"), (bytes(64).hex(), "")],
)
def test_verifier_refuses_short_signature_and_empty_timestamp(
    signature: str, timestamp: str
) -> None:
    api = discord()
    verifier = api.DiscordInteractionVerifier(bytes(32), verify=lambda *_args: True)

    with pytest.raises(api.InvalidDiscordSignature):
        verifier.verify(signature=signature, timestamp=timestamp, body=b"{}")


def test_verifier_refuses_non_numeric_timestamp_when_age_is_bounded() -> None:
    api = discord()
    verifier = api.DiscordInteractionVerifier(bytes(32), verify=lambda *_args: True, max_age=300)

    with pytest.raises(api.InvalidDiscordSignature, match="timestamp"):
        verifier.verify(signature=bytes(64).hex(), timestamp="not-a-number", body=b"{}")


def test_installation_declaration_refuses_invalid_kind_and_empty_owner() -> None:
    api = discord()
    with pytest.raises(ValueError, match="kind"):
        api.DiscordInstallation("channel", "1")
    with pytest.raises(ValueError, match="owner_id"):
        api.DiscordInstallation("guild", "")


@pytest.mark.parametrize("field_name", ["id", "application_id", "token"])
def test_non_ping_interaction_requires_delivery_fields(field_name: str) -> None:
    api = discord()
    payload = command_payload()
    payload[field_name] = ""

    with pytest.raises(api.UnsupportedDiscordInteraction, match=field_name):
        api.DiscordInteraction.parse(payload)

    ping = {"type": 1, "user": {"id": "user-1"}}
    assert api.DiscordInteraction.parse(ping).kind == "ping"


def test_component_tolerates_native_shape_variants_without_inventing_values() -> None:
    api = discord()
    payload = command_payload()
    payload["type"] = 3
    payload["data"] = {"custom_id": "button", "component_type": 2, "values": "not-a-list"}
    payload["message"] = "not-an-object"

    interaction = api.DiscordInteraction.parse(payload)

    assert interaction.component.message_id is None
    assert interaction.component.values == ()
    assert interaction.modal is None

    payload["data"]["values"] = ["one", "two"]
    assert api.DiscordInteraction.parse(payload).component.values == ("one", "two")


def test_native_interaction_does_not_acquire_a_modal_surface() -> None:
    api = discord()
    payload = command_payload()
    payload["type"] = 99

    interaction = api.DiscordInteraction.parse(payload, allow_unknown=True)

    assert interaction.modal is None


def test_non_mapping_command_data_produces_an_empty_native_command() -> None:
    api = discord()
    payload = command_payload()
    payload["data"] = "not-an-object"

    interaction = api.DiscordInteraction.parse(payload)

    assert interaction.command.name == ""
    assert interaction.command.options == {}


def test_installation_context_selects_matching_owner_from_dual_install() -> None:
    api = discord()
    payload = command_payload(guild_id="guild-1")
    payload["authorizing_integration_owners"] = {"0": "0", "1": "user-owner"}

    payload["context"] = 0
    guild = api.DiscordInteraction.parse(payload)
    payload["context"] = 2
    user = api.DiscordInteraction.parse(payload)

    assert guild.installation == api.DiscordInstallation("guild", "guild-1")
    assert user.installation == api.DiscordInstallation("user", "user-owner")


def test_installation_owner_fallbacks_preserve_context_and_owner_meaning() -> None:
    api = discord()
    payload = command_payload(guild_id="guild-visible", user_id="actor")

    payload["context"] = 2
    payload["authorizing_integration_owners"] = {"0": "guild-owner"}
    assert api.DiscordInteraction.parse(payload).installation == api.DiscordInstallation(
        "guild", "guild-owner"
    )

    payload["context"] = 0
    payload["authorizing_integration_owners"] = {"1": "user-owner"}
    assert api.DiscordInteraction.parse(payload).installation == api.DiscordInstallation(
        "user", "user-owner"
    )

    payload["authorizing_integration_owners"] = {"0": "guild-owner"}
    assert api.DiscordInteraction.parse(payload).installation.owner_id == "guild-owner"

    del payload["authorizing_integration_owners"]
    assert api.DiscordInteraction.parse(payload).installation == api.DiscordInstallation(
        "guild", "guild-visible"
    )
    del payload["guild_id"]
    assert api.DiscordInteraction.parse(payload).installation == api.DiscordInstallation(
        "user", "actor"
    )

    payload["context"] = 1
    payload["authorizing_integration_owners"] = {"0": "0"}
    assert api.DiscordInteraction.parse(payload).installation == api.DiscordInstallation(
        "guild", "0"
    )

    payload["context"] = 0
    assert api.DiscordInteraction.parse(payload).installation.owner_id == "0"

    payload["context"] = 1
    payload["guild_id"] = "guild-from-payload"
    assert api.DiscordInteraction.parse(payload).installation.owner_id == "guild-from-payload"

    payload["authorizing_integration_owners"] = {}
    del payload["guild_id"]
    assert api.DiscordInteraction.parse(payload).installation.owner_id == "actor"


@pytest.mark.asyncio
async def test_responder_requires_acknowledgement_before_using_token() -> None:
    api = discord()
    responder = api.DiscordResponder(
        application_id="app",
        interaction_id="interaction",
        token="token",
        received_at=1,
        client=HTTPClient(),
        clock=lambda: 2,
    )

    with pytest.raises(api.InteractionAlreadyAcknowledged, match="not been acknowledged"):
        await responder.edit_original(content="no")


def test_rate_limiter_refuses_non_positive_capacity() -> None:
    api = discord()
    with pytest.raises(ValueError, match="capacity"):
        api.DiscordRateLimiter(capacity=0)


@pytest.mark.asyncio
async def test_rate_limiter_learns_retry_from_body_and_non_global_bucket() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe(
        "POST /channels/{channel_id}/messages",
        "channel-1",
        HTTPResponse(429, {}, {"retry_after": 1.25, "global": False}),
    )

    await limiter.acquire("POST /channels/{channel_id}/messages", "channel-1")
    await limiter.acquire("POST /channels/{channel_id}/messages", "channel-2")

    assert clock.sleeps == [1.25]


@pytest.mark.asyncio
async def test_rate_limiter_distinguishes_global_signals_status_and_retry_source() -> None:
    api = discord()

    async def delay_for(response: HTTPResponse, route: str = "GET /users/@me") -> list[float]:
        clock = Clock()
        limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
        await limiter.observe(route, "one", response)
        await limiter.acquire("GET /guilds/{guild_id}", "two")
        return clock.sleeps

    assert await delay_for(
        HTTPResponse(429, {"Retry-After": "2", "X-RateLimit-Global": "true"})
    ) == [2.0]
    assert await delay_for(HTTPResponse(429, {}, {"retry_after": 3, "global": True})) == [3.0]
    assert (
        await delay_for(HTTPResponse(200, {"Retry-After": "4", "X-RateLimit-Global": "true"})) == []
    )
    assert await delay_for(HTTPResponse(429, {"X-RateLimit-Global": "true"})) == []

    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe("GET /users/@me", "one", HTTPResponse(429, {"Retry-After": "2"}))
    await limiter.acquire("GET /guilds/{guild_id}", "two")
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_429_uses_response_bucket_instead_of_stale_route_mapping() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    route = "POST /channels/{channel_id}/messages"
    sibling = "DELETE /channels/{channel_id}/messages/{message_id}"
    await limiter.observe(
        route,
        "channel",
        HTTPResponse(
            200,
            {
                "X-RateLimit-Bucket": "old",
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset-After": "4",
            },
        ),
    )
    await limiter.observe(
        sibling,
        "channel",
        HTTPResponse(
            200,
            {
                "X-RateLimit-Bucket": "new",
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset-After": "4",
            },
        ),
    )
    await limiter.observe(
        route,
        "channel",
        HTTPResponse(
            429,
            {"X-RateLimit-Bucket": "new", "Retry-After": "2"},
        ),
    )

    await limiter.acquire(sibling, "channel")

    assert clock.sleeps == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "2",
        },
        {
            "X-RateLimit-Bucket": "bucket",
            "X-RateLimit-Reset-After": "2",
        },
        {
            "X-RateLimit-Bucket": "bucket",
            "X-RateLimit-Remaining": "0",
        },
    ],
)
async def test_incomplete_bucket_headers_do_not_invent_a_limit(
    headers: dict[str, str],
) -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe("GET /users/@me", None, HTTPResponse(200, headers))

    await limiter.acquire("GET /users/@me", None)

    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_missing_remaining_header_returns_without_resetting_bucket() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe(
        "GET /users/@me",
        None,
        HTTPResponse(
            200,
            {
                "X-RateLimit-Bucket": "users",
                "X-RateLimit-Reset-After": "2",
            },
        ),
    )
    await limiter.acquire("GET /users/@me", None)
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_rate_limiter_ignores_non_mapping_body_and_positive_remaining() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe(
        "GET /users/@me",
        None,
        HTTPResponse(
            200,
            {
                "X-RateLimit-Bucket": "users",
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset-After": "5",
            },
            ["not", "an", "object"],
        ),
    )

    await limiter.acquire("GET /users/@me", None)

    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_expired_global_limit_is_pruned() -> None:
    api = discord()
    clock = Clock()
    limiter = api.DiscordRateLimiter(clock=clock.now, sleep=clock.sleep)
    await limiter.observe(
        "GET /users/@me",
        None,
        HTTPResponse(429, {"Retry-After": "1", "X-RateLimit-Global": "true"}),
    )
    clock.value += 2

    await limiter.acquire("GET /users/@me", None)

    assert clock.sleeps == []


@dataclass
class Limiter:
    acquired: list[tuple[str, str | None]] = field(default_factory=list)
    observed: list[tuple[str, str | None]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return False

    async def acquire(self, route: str, major: str | None) -> None:
        self.acquired.append((route, major))

    async def observe(self, route: str, major: str | None, response: Any) -> None:
        self.observed.append((route, major))


@pytest.mark.asyncio
async def test_falsey_injected_limiter_is_still_used() -> None:
    api = discord()
    limiter = Limiter()
    client = HTTPClient()
    responder = api.DiscordResponder(
        application_id="app",
        interaction_id="interaction",
        token="token",
        received_at=1,
        acknowledged=True,
        client=client,
        clock=lambda: 2,
        rate_limiter=limiter,
    )

    await responder.edit_original(content="done")

    assert limiter.acquired == [
        ("PATCH /webhooks/{application_id}/{token}/messages/@original", "app:token")
    ]
    assert limiter.observed == limiter.acquired

    provider = api.Discord(
        application_id="app",
        public_key=bytes(32),
        bot_token="token",
        client=client,
        rate_limiter=limiter,
    )
    destination = api.DiscordDestination("channel", "discord:guild:guild")
    await provider.send(
        tenant=destination.tenant,
        destination=destination,
        content="scheduled",
        idempotency_key="delivery",
    )
    assert limiter.acquired[-1] == ("POST /channels/{channel_id}/messages", "channel")


def test_command_without_optional_fields_omits_them() -> None:
    api = discord()
    command = api.DiscordCommand(name="status", description="Status")

    assert command.as_discord() == {
        "name": "status",
        "type": 1,
        "description": "Status",
        "integration_types": [0],
        "contexts": [0],
    }


@pytest.mark.asyncio
async def test_manifest_refuses_invalid_sync_scope_forms() -> None:
    api = discord()
    manifest = api.DiscordManifest("app")
    with pytest.raises(ValueError, match="guild_id"):
        await manifest.sync(HTTPClient(), scope="guild")
    with pytest.raises(ValueError, match="guild_id"):
        await manifest.sync(HTTPClient(), scope="other")


@pytest.mark.asyncio
async def test_manifest_sync_uses_bot_auth_and_injected_rate_limiter() -> None:
    api = discord()
    limiter = Limiter()
    client = HTTPClient([HTTPResponse(200, json=[]), HTTPResponse(200, json=[])])
    command = api.DiscordCommand("status", description="Status")
    manifest = api.DiscordManifest("app", (command,), bot_token="secret", rate_limiter=limiter)

    result = await manifest.sync(client, scope="guild", guild_id="guild")

    assert result.changed is True
    assert dict(client.requests[0][2]["headers"])[b"authorization"] == b"Bot secret"
    assert dict(client.requests[1][2]["headers"])[b"authorization"] == b"Bot secret"
    assert limiter.acquired == [
        ("GET /applications/{application_id}/commands", "guild"),
        ("PUT /applications/{application_id}/commands", "guild"),
    ]
    assert limiter.observed == limiter.acquired

    global_limiter = Limiter()
    matching = HTTPClient([HTTPResponse(200, json=[command.as_discord()])])
    global_manifest = api.DiscordManifest("app", (command,), rate_limiter=global_limiter)
    await global_manifest.sync(matching, scope="global")
    assert global_limiter.acquired == [("GET /applications/{application_id}/commands", "app")]


@pytest.mark.asyncio
async def test_manifest_sync_surfaces_get_refusal() -> None:
    api = discord()
    manifest = api.DiscordManifest("app")

    with pytest.raises(api.DiscordSyncError, match="401"):
        await manifest.sync(HTTPClient([HTTPResponse(401)]), scope="global")


def test_provider_refuses_empty_application_and_invalid_signature_window() -> None:
    api = discord()
    with pytest.raises(api.DiscordConfigurationError, match="application_id"):
        api.Discord(application_id="", public_key=bytes(32), bot_token="token")
    with pytest.raises(api.DiscordConfigurationError, match="signature_max_age"):
        api.Discord(
            application_id="app",
            public_key=bytes(32),
            bot_token="token",
            signature_max_age=0,
        )


def test_provider_manifest_preserves_explicit_description_and_derives_fallback() -> None:
    api = chatops()
    provider = discord().Discord(application_id="app", public_key=bytes(32), bot_token="token")
    chat = api.ChatOps(name="manifest", providers=(provider,))

    @chat.command("explicit", description="Chosen description")
    async def explicit() -> None:
        pass

    @chat.command("fallback_name")
    async def fallback() -> None:
        pass

    descriptions = {
        command.name: command.description
        for command in chat.manifest("discord", base_url="https://example.com").commands
    }
    assert descriptions == {
        "explicit": "Chosen description",
        "fallback_name": "fallback name",
    }


def test_manifest_options_skip_context_unions_and_unsupported_annotations() -> None:
    api = chatops()
    provider = discord().Discord(application_id="app", public_key=bytes(32), bot_token="token")
    chat = api.ChatOps(name="options", providers=(provider,))

    @chat.command("inspect")
    async def inspect(
        context: str,
        query: str | None,
        blob: bytes,
        ambiguous: str | int,
        empty_choice: Literal[()],
    ) -> None:
        pass

    options = chat.manifest("discord", base_url="https://example.com").commands[0].options
    assert options == (
        discord().DiscordOption(name="query", description="query", type=3, required=True),
    )


@pytest.mark.asyncio
async def test_async_proactive_deliver_is_awaited_and_missing_transport_refuses() -> None:
    api = discord()

    async def deliver(message: Any) -> str:
        assert message.content == "scheduled"
        return "message-1"

    provider = api.Discord(
        application_id="app", public_key=bytes(32), bot_token="token", deliver=deliver
    )
    destination = api.DiscordDestination("channel", "discord:guild:guild")
    assert (
        await provider.send(
            tenant=destination.tenant,
            destination=destination,
            content="scheduled",
            idempotency_key="delivery",
        )
        == "message-1"
    )

    missing = api.Discord(application_id="app", public_key=bytes(32), bot_token="token")
    with pytest.raises(api.DiscordConfigurationError, match="client or deliver"):
        await missing.send(
            tenant=destination.tenant,
            destination=destination,
            content="scheduled",
            idempotency_key="delivery",
        )


@dataclass
class Jobs:
    handlers: dict[str, Any] = field(default_factory=dict)
    enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def task(self, name: str, **_options: Any) -> Any:
        def register(handler: Any) -> Any:
            if name in self.handlers:
                raise ValueError("duplicate task")
            self.handlers[name] = handler
            return handler

        return register

    async def enqueue(self, task: str, *args: Any, **options: Any) -> int:
        self.enqueued.append((task, args, options))
        return 1


@dataclass
class Inbox:
    seen: set[str] = field(default_factory=set)

    async def claim(self, **options: Any) -> bool:
        delivery = str(options["delivery"])
        if delivery in self.seen:
            return False
        self.seen.add(delivery)
        return True

    async def claim_and_enqueue(self, *, enqueue: Any, **_options: Any) -> bool:
        delivery = str(_options["envelope"].id)
        if delivery in self.seen:
            return False
        self.seen.add(delivery)
        await enqueue(transaction=self)
        return True


@dataclass
class Reports:
    values: list[tuple[str, float, str]] = field(default_factory=list)

    def report(self, task_id: str, percent: float, message: str) -> None:
        self.values.append((task_id, percent, message))


def job(task: str, reports: Reports | None = None) -> JobContext:
    return JobContext(
        job_id=9,
        task=task,
        attempt=1,
        fence=4,
        tenant="discord:guild:guild-1",
        key="discord:interaction:interaction-1",
        progress=reports,
    )


def envelope() -> dict[str, Any]:
    payload = command_payload()
    payload["data"]["options"][0]["options"] = [payload["data"]["options"][0]["options"][0]]
    return {"interaction": payload, "received_at": 1_000.0}


@pytest.mark.asyncio
async def test_durable_emitter_covers_progress_empty_text_invalid_and_completion_states() -> None:
    api = chatops()
    discord_api = discord()
    jobs = Jobs()
    client = HTTPClient()
    provider = discord_api.Discord(
        application_id="application-1",
        public_key=bytes(32),
        bot_token="token",
        client=client,
        clock=lambda: 1_001,
    )
    chat = api.ChatOps(name="states", providers=(provider,), jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        assert context.command == "agent"
        assert context.action is None
        await context.emit(api.AgentEvent.progress("ignored"))
        await context.emit(api.AgentEvent.progress(None, percent=25))
        await context.emit(api.AgentEvent.text(""))
        with pytest.raises(ValueError, match="unsupported"):
            await context.emit(api.AgentEvent("future"))
        await context.emit(api.AgentEvent.text(prompt))
        await context.emit(api.AgentEvent.completed())
        with pytest.raises(RuntimeError, match="after completed"):
            await context.emit(api.AgentEvent.text("late"))

    task, handler = next(iter(jobs.handlers.items()))
    reports = Reports()
    await handler(job(task, reports), envelope())

    assert reports.values == [("9", 25.0, "")]
    assert len(client.requests) == 1
    assert json.loads(client.requests[0][2]["body"]) == {"content": "ship it"}


@pytest.mark.asyncio
async def test_durable_return_value_without_events_edits_original_once() -> None:
    api = chatops()
    jobs = Jobs()
    client = HTTPClient()
    provider = discord().Discord(
        application_id="application-1",
        public_key=bytes(32),
        bot_token="token",
        client=client,
        clock=lambda: 1_001,
    )
    chat = api.ChatOps(name="reply", providers=(provider,), jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent(prompt: str) -> str:
        return prompt

    task, handler = next(iter(jobs.handlers.items()))
    await handler(job(task), envelope())

    assert len(client.requests) == 1
    assert json.loads(client.requests[0][2]["body"]) == {"content": "ship it"}


@pytest.mark.asyncio
async def test_durable_output_requires_client_but_empty_completion_does_not() -> None:
    api = chatops()
    jobs = Jobs()
    provider = discord().Discord(
        application_id="application-1", public_key=bytes(32), bot_token="token"
    )
    chat = api.ChatOps(name="no_client", providers=(provider,), jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        await context.emit(api.AgentEvent.completed())

    task, handler = next(iter(jobs.handlers.items()))
    await handler(job(task), envelope())

    second_jobs = Jobs()
    second = api.ChatOps(name="needs_client", providers=(provider,), jobs=second_jobs)

    @second.command("agent", execution="durable")
    async def reply(prompt: str) -> str:
        return prompt

    second_task, second_handler = next(iter(second_jobs.handlers.items()))
    with pytest.raises(discord().DiscordConfigurationError, match="outbound client"):
        await second_handler(job(second_task), envelope())

    third_jobs = Jobs()
    third = api.ChatOps(name="text_needs_client", providers=(provider,), jobs=third_jobs)

    @third.command("agent", execution="durable")
    async def text(context: Any, prompt: str) -> None:
        await context.emit(api.AgentEvent.text(prompt))
        await context.emit(api.AgentEvent.completed())

    third_task, third_handler = next(iter(third_jobs.handlers.items()))
    with pytest.raises(discord().DiscordConfigurationError, match="outbound client"):
        await third_handler(job(third_task), envelope())


@pytest.mark.asyncio
async def test_empty_text_completion_does_not_create_an_empty_discord_edit() -> None:
    api = chatops()
    jobs = Jobs()
    client = HTTPClient()
    provider = discord().Discord(
        application_id="application-1",
        public_key=bytes(32),
        bot_token="token",
        client=client,
        clock=lambda: 1_001,
    )
    chat = api.ChatOps(name="empty", providers=(provider,), jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        await context.emit(api.AgentEvent.text(""))
        await context.emit(api.AgentEvent.completed())

    task, handler = next(iter(jobs.handlers.items()))
    await handler(job(task), envelope())
    assert client.requests == []


def test_provider_registers_one_durable_task_per_command_and_ignores_inline() -> None:
    api = chatops()
    jobs = Jobs()
    provider = discord().Discord(
        application_id="application-1", public_key=bytes(32), bot_token="token"
    )
    chat = api.ChatOps(name="register", providers=(provider,), jobs=jobs)

    @chat.command("inline")
    async def inline() -> None:
        pass

    @chat.command("first", execution="durable")
    async def first() -> None:
        pass

    @chat.command("second", execution="durable")
    async def second() -> None:
        pass

    assert list(jobs.handlers) == [
        "chat_register_first_discord",
        "chat_register_second_discord",
    ]

    inline_jobs = Jobs()
    inline_chat = api.ChatOps(name="only_inline", providers=(provider,), jobs=inline_jobs)

    @inline_chat.command("inline")
    async def only_inline() -> None:
        pass

    assert inline_jobs.handlers == {}

    no_jobs = api.ChatOps(name="no_jobs", providers=(provider,))

    @no_jobs.command("durable", execution="durable")
    async def durable() -> None:
        pass

    assert list(jobs.handlers) == [
        "chat_register_first_discord",
        "chat_register_second_discord",
    ]


@pytest.mark.asyncio
async def test_durable_worker_refuses_non_mapping_interaction_envelope() -> None:
    api = chatops()
    jobs = Jobs()
    provider = discord().Discord(
        application_id="application-1", public_key=bytes(32), bot_token="token"
    )
    chat = api.ChatOps(name="invalid", providers=(provider,), jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent() -> None:
        pass

    task, handler = next(iter(jobs.handlers.items()))
    with pytest.raises(ValueError, match="mapping"):
        await handler(job(task), {"interaction": "invalid"})


def test_durable_job_name_is_ascii_bounded_and_collision_resistant() -> None:
    api = chatops()

    def registered(name: str) -> str:
        jobs = Jobs()
        provider = discord().Discord(
            application_id="application-1", public_key=bytes(32), bot_token="token"
        )
        chat = api.ChatOps(name=name, providers=(provider,), jobs=jobs)

        @chat.command("agent", execution="durable")
        async def agent() -> None:
            pass

        return next(iter(jobs.handlers))

    dashed = registered("ops-east")
    assert dashed.startswith("chat_ops_east_agent_")
    assert dashed.endswith("_discord")
    unicode_name = registered("ops-é")
    assert unicode_name.startswith("chat_ops___agent_")
    assert unicode_name.endswith("_discord")
    assert dashed != registered("ops_east")
    long_name = registered("a" * 80)
    assert len(long_name.encode()) <= 63
    assert long_name != registered("a" * 79 + "b")


@pytest.mark.asyncio
async def test_accept_requires_each_atomic_durable_owner() -> None:
    api = chatops()
    provider = discord().Discord(
        application_id="application-1", public_key=bytes(32), bot_token="token"
    )
    interaction = discord().DiscordInteraction.parse(command_payload())

    missing_jobs = api.ChatOps(name="missing_jobs", providers=(provider,), inbox=object())
    with pytest.raises(discord().DiscordConfigurationError, match="jobs and inbox"):
        await provider.accept(missing_jobs, interaction)

    missing_inbox = api.ChatOps(name="missing_inbox", providers=(provider,), jobs=Jobs())
    with pytest.raises(discord().DiscordConfigurationError, match="jobs and inbox"):
        await provider.accept(missing_inbox, interaction)

    non_atomic = api.ChatOps(name="non_atomic", providers=(provider,), jobs=Jobs(), inbox=object())
    with pytest.raises(discord().DiscordConfigurationError, match="claim_and_enqueue"):
        await provider.accept(non_atomic, interaction)


def test_mounted_provider_refuses_installation_store_without_fetch() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )

    with pytest.raises(api.DiscordConfigurationError, match="fetch"):
        chatops().ChatOps(
            app,
            name="invalid_installations",
            providers=(provider,),
            installations=object(),
        )


@pytest.mark.asyncio
async def test_ingress_refuses_non_object_and_wrong_application_payloads() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chatops().ChatOps(app, name="ingress", providers=(provider,))

    assert (await post_signed(app, [])).status == 400
    wrong = command_payload(application_id="other-application")
    assert (await post_signed(app, wrong)).status == 401


@pytest.mark.asyncio
async def test_action_context_and_component_duplicate_use_provider_semantics() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    jobs = Jobs()
    chat = chatops().ChatOps(app, name="actions", providers=(provider,), jobs=jobs, inbox=Inbox())
    contexts: list[Any] = []

    @chat.command("approval:approve:nonce-1", execution="durable")
    async def colliding_command() -> None:
        pass

    @chat.action("approval:approve:nonce-1")
    async def approve(context: Any, values: tuple[Any, ...]) -> None:
        contexts.append(context)

    payload = component_payload()
    first = await post_signed(app, payload)
    duplicate = await post_signed(app, payload)

    assert first.json() == {"type": 4, "data": {}}
    assert duplicate.json() == {"type": 6}
    assert contexts[0].command is None
    assert contexts[0].action == "approval:approve:nonce-1"
    assert jobs.enqueued == []


@pytest.mark.asyncio
async def test_component_activates_a_prefix_chat_action() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(app, name="actions", providers=(provider,))
    seen: list[str] = []

    @chat.action("approval:approve:", prefix=True)
    async def approve(context: Any, values: tuple[Any, ...]) -> None:
        del values
        seen.append(context.action)

    response = await post_signed(app, component_payload())

    assert response.json() == {"type": 4, "data": {}}
    assert seen == ["approval:approve:nonce-1"]


@pytest.mark.asyncio
async def test_durable_command_ingress_uses_atomic_acceptance_and_defer() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    jobs = Jobs()
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(
        app, name="durable_ingress", providers=(provider,), jobs=jobs, inbox=Inbox()
    )

    @chat.command("agent", execution="durable")
    async def agent(prompt: str, private: bool) -> None:
        pass

    response = await post_signed(app, command_payload())

    assert response.json() == {"type": 5}
    assert len(jobs.enqueued) == 1


@pytest.mark.asyncio
async def test_unknown_command_and_handler_failure_have_distinct_error_callbacks() -> None:
    api = discord()
    app = Wreath()
    _body, _timestamp, _signature, public_key = signed({"type": 1})
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(app, name="errors", providers=(provider,))

    unknown = command_payload(interaction_id="unknown")
    unknown["data"]["name"] = "missing"
    unknown_response = await post_signed(app, unknown)

    @chat.command("agent")
    async def agent(prompt: str, private: bool) -> None:
        raise RuntimeError("handler failure")

    failed = await post_signed(app, command_payload(interaction_id="failed"))

    assert unknown_response.json()["data"]["content"] == "Unknown command or action."
    assert failed.json()["data"]["content"] == "This request could not be completed."
