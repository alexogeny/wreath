from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wreath import Wreath
from wreath.testing import TestClient

from .support import chatops, command_payload, component_payload, discord, modal_payload


def _signed(payload: dict[str, object]) -> tuple[bytes, bytes, str, str]:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    timestamp = "1700000000"
    signature = private.sign(timestamp.encode() + body).hex()
    return private.public_key().public_bytes_raw(), body, timestamp, signature


async def _raw_post(app: Wreath, path: str, body: bytes, headers: list[tuple[bytes, bytes]]):
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "server": ("test", 443),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def test_ed25519_verification_uses_timestamp_and_exact_raw_body() -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed({"type": 1, "text": "café"})
    verifier = api.DiscordInteractionVerifier(public_key)

    verifier.verify(signature=signature, timestamp=timestamp, body=body)

    with pytest.raises(api.InvalidDiscordSignature):
        verifier.verify(signature=signature, timestamp=timestamp, body=body + b" ")
    with pytest.raises(api.InvalidDiscordSignature):
        verifier.verify(signature=signature, timestamp="1700000001", body=body)


@pytest.mark.parametrize("signature,timestamp", [(None, "1"), ("00", None), ("zz", "1")])
async def test_missing_or_malformed_signature_headers_are_unauthorized(
    signature: str | None, timestamp: str | None
) -> None:
    api = discord()
    app = Wreath()
    provider = api.Discord(application_id="app-1", public_key=bytes(32), bot_token="token")
    chatops().ChatOps(app, name="test", providers=(provider,))

    response = await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=b'{"type":1}',
        headers={
            key: value
            for key, value in {
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": timestamp,
            }.items()
            if value is not None
        },
    )

    assert response.status == 401
    assert response.body == b""


async def test_ping_is_verified_before_returning_pong() -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed({"type": 1})
    app = Wreath()
    provider = api.Discord(
        application_id="app-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chatops().ChatOps(app, name="test", providers=(provider,))

    response = await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=body,
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        },
    )

    assert response.status == 200
    assert response.json() == {"type": 1}


async def test_duplicate_signature_headers_are_refused() -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed({"type": 1})
    app = Wreath()
    chatops().ChatOps(
        app,
        name="test",
        providers=(
            api.Discord(
                application_id="app-1",
                public_key=public_key,
                bot_token="token",
                clock=lambda: 1_700_000_000,
            ),
        ),
    )
    sent = await _raw_post(
        app,
        "/_wreath/chat/discord/interactions",
        body,
        [
            (b"x-signature-ed25519", b"00"),
            (b"x-signature-ed25519", signature.encode()),
            (b"x-signature-timestamp", timestamp.encode()),
        ],
    )
    assert sent[0]["status"] == 401


@pytest.mark.parametrize(
    "content_types",
    [
        (b"application/json", b"application/x-www-form-urlencoded"),
        (b"application/x-www-form-urlencoded", b"application/json"),
    ],
)
async def test_duplicate_content_type_cannot_select_discord_interaction_parser(
    content_types: tuple[bytes, bytes],
) -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed({"type": 1})
    app = Wreath()
    chatops().ChatOps(
        app,
        name="test",
        providers=(
            api.Discord(
                application_id="app-1",
                public_key=public_key,
                bot_token="token",
                clock=lambda: 1_700_000_000,
            ),
        ),
    )

    sent = await _raw_post(
        app,
        "/_wreath/chat/discord/interactions",
        body,
        [
            *((b"content-type", value) for value in content_types),
            (b"x-signature-ed25519", signature.encode()),
            (b"x-signature-timestamp", timestamp.encode()),
        ],
    )

    assert sent[0]["status"] == 415


async def test_discord_interaction_refuses_non_json_media_type() -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed({"type": 1})
    app = Wreath()
    chatops().ChatOps(
        app,
        name="test",
        providers=(
            api.Discord(
                application_id="app-1",
                public_key=public_key,
                bot_token="token",
                clock=lambda: 1_700_000_000,
            ),
        ),
    )

    sent = await _raw_post(
        app,
        "/_wreath/chat/discord/interactions",
        body,
        [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"x-signature-ed25519", signature.encode()),
            (b"x-signature-timestamp", timestamp.encode()),
        ],
    )

    assert sent[0]["status"] == 415


async def test_durable_ingress_passes_exact_verified_envelope_to_shared_atomic_owner() -> None:
    class Inbox:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def claim_and_enqueue(
            self,
            *,
            source: str,
            envelope: Any,
            enqueue: Any,
            result_status: int,
        ) -> bool:
            self.calls.append(
                {"source": source, "envelope": envelope, "result_status": result_status}
            )
            await enqueue(transaction=self)
            return True

    class Jobs:
        def task(self, _name: str, **_options: Any) -> Any:
            return lambda handler: handler

        async def enqueue(self, _task: str, *_args: Any, **_options: Any) -> int:
            return 7

    api = discord()
    payload = command_payload()
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    body = json.dumps(payload, indent=2).encode()
    timestamp = "1700000000"
    signature = private.sign(timestamp.encode() + body).hex()
    public_key = private.public_key().public_bytes_raw()
    inbox = Inbox()
    app = Wreath()
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(
        app,
        name="test",
        providers=(provider,),
        inbox=inbox,
        jobs=Jobs(),
    )

    @chat.command("agent", execution="durable")
    async def agent(prompt: str, private: bool) -> None:
        raise AssertionError(f"durable command ran inline: {prompt=} {private=}")

    response = await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=body,
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        },
    )

    assert response.status == 200
    assert response.json() == {"type": 5}
    assert len(inbox.calls) == 1
    call = inbox.calls[0]
    assert call["source"] == "discord:guild:guild-1"
    assert call["envelope"].id == "interaction-1"
    assert call["envelope"].type == "command:agent"
    assert call["envelope"].body == body
    assert call["envelope"].timestamp.timestamp() == 1_700_000_000
    assert call["result_status"] == 200


def test_nested_command_options_are_typed_and_preserve_native_payload() -> None:
    api = discord()
    raw = command_payload()

    interaction = api.DiscordInteraction.parse(raw)

    assert interaction.command.name == "agent"
    assert interaction.command.path == ("ask",)
    assert interaction.command.options == {"prompt": "ship it", "private": True}
    assert interaction.actor.id == "user-1"
    assert interaction.native is raw


def test_component_and_modal_payloads_have_distinct_typed_surfaces() -> None:
    api = discord()

    component = api.DiscordInteraction.parse(component_payload())
    modal = api.DiscordInteraction.parse(modal_payload())

    assert component.kind == "component"
    assert component.component.custom_id == "approval:approve:nonce-1"
    assert component.component.message_id == "message-1"
    assert modal.kind == "modal"
    assert modal.modal.custom_id == "agent:details"
    assert modal.modal.values == {"prompt": "full request"}


def test_unknown_interaction_and_component_types_are_available_to_native_handler() -> None:
    api = discord()
    unknown = command_payload()
    unknown["type"] = 99
    unknown["data"] = {"component_type": 97, "future": True}

    interaction = api.DiscordInteraction.parse(unknown, allow_unknown=True)

    assert interaction.kind == "native"
    assert interaction.native is unknown
    with pytest.raises(api.UnsupportedDiscordInteraction, match="99"):
        api.DiscordInteraction.parse(unknown)


def test_installation_owner_not_triggering_user_defines_user_install_tenant() -> None:
    api = discord()
    payload = command_payload(guild_id="guild-visible", user_id="user-triggering")
    payload["context"] = 2
    payload["authorizing_integration_owners"] = {"1": "user-install-owner"}

    interaction = api.DiscordInteraction.parse(payload)

    assert interaction.installation.kind == "user"
    assert interaction.installation.owner_id == "user-install-owner"
    assert interaction.actor.id == "user-triggering"
    assert api.DiscordTenantKey.from_interaction(interaction) == "discord:user:user-install-owner"


def test_guild_and_user_installations_with_same_snowflake_do_not_share_tenant() -> None:
    api = discord()
    guild = api.DiscordInstallation(kind="guild", owner_id="42")
    user = api.DiscordInstallation(kind="user", owner_id="42")

    assert api.DiscordTenantKey.from_installation(guild) != api.DiscordTenantKey.from_installation(
        user
    )


async def test_inline_empty_failure_unknown_and_duplicate_return_valid_callbacks() -> None:
    api = discord()
    public_key, body, timestamp, signature = _signed(command_payload())
    app = Wreath()
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(app, name="test", providers=(provider,))
    calls: list[str] = []

    @chat.command("agent")
    async def agent(prompt: str, private: bool) -> None:
        calls.append(prompt)

    headers = {
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
    }
    first = await TestClient(app).post(
        "/_wreath/chat/discord/interactions", content=body, headers=headers
    )
    duplicate = await TestClient(app).post(
        "/_wreath/chat/discord/interactions", content=body, headers=headers
    )

    assert first.status == 200
    assert first.json() == {"type": 4, "data": {}}
    assert duplicate.json() == {"type": 5}
    assert calls == ["ship it"]

    failing = command_payload(interaction_id="interaction-2")
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))

    @chat.command("broken")
    async def broken() -> None:
        raise RuntimeError("secret failure")

    failing["data"]["name"] = "broken"
    failing_body = json.dumps(failing, separators=(",", ":")).encode()
    failing_signature = private.sign(timestamp.encode() + failing_body).hex()
    failed = await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=failing_body,
        headers={
            "X-Signature-Ed25519": failing_signature,
            "X-Signature-Timestamp": timestamp,
        },
    )

    assert failed.status == 200
    assert failed.json()["type"] == 4
    assert failed.json()["data"]["flags"] == 64


async def test_configured_installation_store_fails_closed_before_dispatch() -> None:
    class Installations:
        async def fetch(self, **_key: object) -> None:
            return None

    api = discord()
    payload = command_payload()
    public_key, body, timestamp, signature = _signed(payload)
    app = Wreath()
    provider = api.Discord(
        application_id="application-1",
        public_key=public_key,
        bot_token="token",
        clock=lambda: 1_700_000_000,
    )
    chat = chatops().ChatOps(app, name="test", providers=(provider,), installations=Installations())
    called = False

    @chat.command("agent")
    async def agent(prompt: str, private: bool) -> None:
        nonlocal called
        called = True

    response = await TestClient(app).post(
        "/_wreath/chat/discord/interactions",
        content=body,
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        },
    )

    assert response.status == 200
    assert response.json()["data"]["flags"] == 64
    assert called is False
