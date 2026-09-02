from __future__ import annotations

from typing import Literal

import pytest

from wreath import Wreath
from wreath.chat import ChatContext, ChatOps, ChatReply
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, form_body, signed_headers


def configured() -> tuple[ChatOps, Wreath]:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
    )
    return chat, app


async def invoke(app: Wreath, values: dict[str, str]):
    body = form_body(**values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)


async def test_form_encoded_slash_command_binds_typed_arguments(
    slash_values: dict[str, str],
) -> None:
    seen: list[tuple[str, bool, int, Literal["blue", "green"]]] = []
    chat, app = configured()

    @chat.command("deploy", description="Deploy a service")
    async def deploy(
        environment: str,
        force: bool = False,
        replicas: int = 1,
        colour: Literal["blue", "green"] = "blue",
    ) -> ChatReply:
        seen.append((environment, force, replicas, colour))
        return ChatReply.ephemeral("queued")

    response = await invoke(app, dict(slash_values, text="production --force --replicas 3"))

    assert response.status == 200
    assert response.json() == {"response_type": "ephemeral", "text": "queued"}
    assert seen == [("production", True, 3, "blue")]


async def test_command_context_exposes_slack_native_fields(
    slash_values: dict[str, str],
) -> None:
    seen: list[ChatContext] = []
    chat, app = configured()

    @chat.command("deploy")
    async def deploy(command: ChatContext, environment: str) -> None:
        seen.append(command)

    response = await invoke(app, slash_values)

    assert (response.status, response.body) == (200, b"")
    assert seen[0].provider == "slack"
    assert seen[0].installation == "T123"
    assert seen[0].actor == "U123"
    assert seen[0].conversation == "C123"
    assert seen[0].native == slash_values


@pytest.mark.parametrize(
    ("text", "parameter"),
    [
        ("", "environment"),
        ("production --replicas many", "replicas"),
        ("production --unknown yes", "unknown"),
        ("production extra-positional", "extra-positional"),
    ],
)
async def test_binding_errors_are_ephemeral_and_do_not_run_the_command(
    slash_values: dict[str, str], text: str, parameter: str
) -> None:
    ran = False
    chat, app = configured()

    @chat.command("deploy")
    async def deploy(environment: str, replicas: int = 1) -> None:
        nonlocal ran
        ran = True

    response = await invoke(app, dict(slash_values, text=text))

    assert response.status == 200
    assert response.json()["response_type"] == "ephemeral"
    assert parameter in response.json()["text"]
    assert ran is False


async def test_undeclared_common_option_is_not_silently_ignored(
    slash_values: dict[str, str],
) -> None:
    ran = False
    chat, app = configured()

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        nonlocal ran
        ran = True

    response = await invoke(app, dict(slash_values, text="production --force"))

    assert response.status == 200
    assert response.json() == {
        "response_type": "ephemeral",
        "text": "unknown command option --force",
    }
    assert ran is False


async def test_a_different_slash_command_cannot_activate_the_registered_command(
    slash_values: dict[str, str],
) -> None:
    ran = False
    chat, app = configured()

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        nonlocal ran
        ran = True

    response = await invoke(app, dict(slash_values, command="/delete"))

    assert response.status == 200
    assert response.json() == {
        "response_type": "ephemeral",
        "text": "Unknown command /delete",
    }
    assert ran is False


async def test_inline_in_channel_response_preserves_blocks_and_metadata(
    slash_values: dict[str, str],
) -> None:
    chat, app = configured()

    @chat.command("deploy")
    async def deploy(environment: str) -> ChatReply:
        return ChatReply.in_channel(
            f"Deploying {environment}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*Queued*"}}],
            native={"metadata": {"event_type": "wreath_deploy", "event_payload": {"id": "7"}}},
        )

    response = await invoke(app, slash_values)
    payload = response.json()
    assert payload["response_type"] == "in_channel"
    assert payload["text"] == "Deploying production"
    assert payload["blocks"][0]["type"] == "section"
    assert payload["metadata"]["event_type"] == "wreath_deploy"


def test_command_refuses_a_slash_in_the_provider_neutral_name() -> None:
    chat, _ = configured()
    with pytest.raises(ValueError, match="deploy.*without.*slash"):

        @chat.command("/deploy")
        async def deploy() -> None:
            pass


def test_duplicate_command_name_is_refused_at_declaration() -> None:
    chat, _ = configured()

    @chat.command("deploy")
    async def first() -> None:
        pass

    with pytest.raises(ValueError, match="duplicate.*deploy"):

        @chat.command("deploy")
        async def second() -> None:
            pass
