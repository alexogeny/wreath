from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.chat import ChatOps
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import SIGNING_SECRET


def test_manifest_derives_routes_commands_events_and_minimum_scopes() -> None:
    chat = ChatOps(
        name="operations",
        path="/_wreath/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, app_id="A123"),),
    )

    @chat.command("deploy", description="Deploy a service")
    async def deploy(environment: str) -> None:
        pass

    @chat.event("app_mention")
    async def mention() -> None:
        pass

    manifest = chat.manifest("slack", base_url="https://ops.example")

    assert manifest["features"]["slash_commands"] == [
        {
            "command": "/deploy",
            "description": "Deploy a service",
            "url": "https://ops.example/_wreath/chat/slack/commands",
        }
    ]
    assert manifest["settings"]["event_subscriptions"] == {
        "request_url": "https://ops.example/_wreath/chat/slack/events",
        "bot_events": ["app_mention"],
    }
    assert manifest["settings"]["interactivity"] == {"is_enabled": False}
    assert manifest["oauth_config"]["scopes"]["bot"] == ["app_mentions:read", "commands"]


def test_manifest_enables_interactivity_only_when_an_action_exists() -> None:
    chat = ChatOps(name="operations", providers=(Slack(signing_secret=SIGNING_SECRET),))

    @chat.action("approve")
    async def approve() -> None:
        pass

    manifest = chat.manifest("slack", base_url="https://ops.example")
    assert manifest["settings"]["interactivity"] == {
        "is_enabled": True,
        "request_url": "https://ops.example/_wreath/chat/slack/interactions",
    }


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Slack(signing_secret=""), "signing secret"),
        (
            lambda: ChatOps(
                name="operations",
                providers=(
                    Slack(signing_secret=SIGNING_SECRET),
                    Slack(signing_secret=SIGNING_SECRET),
                ),
            ),
            "duplicate.*slack",
        ),
    ],
)
def test_invalid_provider_configuration_is_refused_immediately(build, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build()


def test_manifest_refuses_non_https_public_origin() -> None:
    chat = ChatOps(name="operations", providers=(Slack(signing_secret=SIGNING_SECRET),))
    with pytest.raises(ValueError, match="HTTPS"):
        chat.manifest("slack", base_url="http://ops.example")


async def test_durable_command_without_a_job_runner_refuses_at_startup() -> None:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET),),
    )

    @chat.command("deploy", execution="durable")
    async def deploy(environment: str) -> None:
        pass

    with pytest.raises(RuntimeError, match="lifespan startup") as raised:
        async with TestClient(app):
            pass
    assert "JobRunner" in str(raised.value)
