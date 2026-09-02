from __future__ import annotations

import json
from typing import Any, Literal

import pytest

from .support import HTTPClient, HTTPResponse, discord


def _command(api: Any, name: str = "agent", command_type: int = 1, **kwargs: object) -> Any:
    name = str(kwargs.pop("name", name))
    command_type = int(kwargs.pop("type", command_type))
    description = kwargs.pop("description", "Run an agent" if command_type == 1 else "")
    return api.DiscordCommand(
        name=name,
        type=command_type,
        description=description,
        **kwargs,
    )


def test_manifest_emits_options_installation_types_contexts_and_permissions() -> None:
    api = discord()
    command = _command(
        api,
        options=(
            api.DiscordOption(
                name="prompt",
                description="What to do",
                type=3,
                required=True,
                min_length=1,
                max_length=6000,
            ),
        ),
        integration_types=(0, 1),
        contexts=(0, 1, 2),
        default_member_permissions="32",
    )

    assert command.as_discord() == {
        "name": "agent",
        "type": 1,
        "description": "Run an agent",
        "options": [
            {
                "name": "prompt",
                "description": "What to do",
                "type": 3,
                "required": True,
                "min_length": 1,
                "max_length": 6000,
            }
        ],
        "integration_types": [0, 1],
        "contexts": [0, 1, 2],
        "default_member_permissions": "32",
    }


@pytest.mark.parametrize(
    "commands,match",
    [
        (("same", 1, "same", 1), "duplicate.*same.*type 1"),
        (("same", 2, "same", 2), "duplicate.*same.*type 2"),
    ],
)
def test_duplicate_command_name_and_type_in_one_scope_refuses_at_manifest_build(
    commands: tuple[str, int, str, int], match: str
) -> None:
    api = discord()
    first_name, first_type, second_name, second_type = commands

    with pytest.raises(ValueError, match=match):
        api.DiscordManifest(
            application_id="app-1",
            commands=(
                _command(api, first_name, first_type),
                _command(api, second_name, second_type),
            ),
        )


def test_same_name_with_different_command_types_is_not_a_duplicate() -> None:
    api = discord()

    manifest = api.DiscordManifest(
        application_id="app-1",
        commands=(_command(api, "inspect", 2), _command(api, "inspect", 3)),
    )

    assert len(manifest.commands) == 2


@pytest.mark.parametrize(
    "command,match",
    [
        ({"name": "UPPER", "type": 1, "description": "x"}, "UPPER.*lowercase"),
        ({"name": "agent", "type": 1, "description": ""}, "agent.*description"),
        ({"name": "Inspect", "type": 2, "description": "not allowed"}, "Inspect.*description"),
        (
            {
                "name": "agent",
                "type": 1,
                "description": "x",
                "integration_types": (0,),
                "contexts": (2,),
            },
            "PRIVATE_CHANNEL.*USER_INSTALL",
        ),
    ],
)
def test_invalid_command_manifest_refuses_at_declaration(
    command: dict[str, object], match: str
) -> None:
    api = discord()

    with pytest.raises(ValueError, match=match):
        _command(api, **command)


@pytest.mark.asyncio
async def test_sync_uses_bulk_overwrite_and_is_idempotent_when_remote_matches() -> None:
    api = discord()
    command = _command(api)
    remote = {
        **command.as_discord(),
        "id": "command-1",
        "application_id": "app-1",
        "version": "7",
    }
    client = HTTPClient([HTTPResponse(200, json=[remote])])
    manifest = api.DiscordManifest(application_id="app-1", commands=(command,))

    result = await manifest.sync(client, scope="global")

    assert result.changed is False
    assert [(method, path) for method, path, _ in client.requests] == [
        ("GET", "/applications/app-1/commands")
    ]


@pytest.mark.asyncio
async def test_sync_bulk_overwrites_exact_desired_set_and_removes_stale_commands() -> None:
    api = discord()
    command = _command(api)
    client = HTTPClient([HTTPResponse(200, json=[{"name": "stale", "type": 1}])])
    manifest = api.DiscordManifest(application_id="app-1", commands=(command,))

    result = await manifest.sync(client, scope="guild", guild_id="guild-1")

    assert result.changed is True
    method, path, options = client.requests[1]
    assert method == "PUT"
    assert path == "/applications/app-1/guilds/guild-1/commands"
    assert dict(options["headers"])[b"content-type"] == b"application/json"
    assert json.loads(options["body"]) == [command.as_discord()]


@pytest.mark.asyncio
async def test_explicit_sync_surfaces_discord_refusal_without_partial_success() -> None:
    api = discord()
    command = _command(api)
    client = HTTPClient([HTTPResponse(200, json=[]), HTTPResponse(403)])
    manifest = api.DiscordManifest(application_id="app-1", commands=(command,))

    with pytest.raises(api.DiscordSyncError, match="403.*command sync"):
        await manifest.sync(client, scope="global")


def test_provider_refuses_missing_credentials_during_local_configuration() -> None:
    api = discord()
    with pytest.raises(api.DiscordConfigurationError, match="public_key"):
        api.Discord(application_id="app-1", public_key=None, bot_token="token")
    with pytest.raises(api.DiscordConfigurationError, match="bot_token"):
        api.Discord(application_id="app-1", public_key=bytes(32), bot_token=None)


def test_provider_startup_never_mutates_discord_command_state() -> None:
    api = discord()
    client = HTTPClient()
    provider = api.Discord(
        application_id="app-1",
        public_key=bytes(32),
        bot_token="token",
        client=client,
    )

    provider.validate()

    assert client.requests == []


def test_provider_manifest_derives_typed_options_from_shared_declaration() -> None:
    from wreath.chat import ChatOps

    provider = discord().Discord(application_id="app-1", public_key=bytes(32), bot_token="token")
    chat = ChatOps(name="test", providers=(provider,))

    @chat.command("deploy", description="Deploy a release")
    async def deploy(environment: Literal["staging", "production"], force: bool = False) -> None:
        pass

    command = chat.manifest("discord", base_url="https://example.com").commands[0]

    assert command.options == (
        discord().DiscordOption(
            name="environment",
            description="environment",
            type=3,
            required=True,
            choices=(
                {"name": "staging", "value": "staging"},
                {"name": "production", "value": "production"},
            ),
        ),
        discord().DiscordOption(name="force", description="force", type=5, required=False),
    )
