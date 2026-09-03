from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from wreath import Wreath
from wreath.chat import ChatContext, ChatOps
from wreath.chat.slack import Slack, SlackInstallation, _Inbound
from wreath.http_client import ClientResponse
from wreath.jobs import JobRunner
from wreath.testing import TestClient

from .conftest import (
    NOW,
    SIGNING_SECRET,
    RecordingInstallationStore,
    form_body,
    json_body,
    signed_headers,
)


@dataclass
class OriginClient:
    responses: list[ClientResponse]
    requests: list[tuple[str, str, tuple[tuple[bytes, bytes], ...], bytes]] = field(
        default_factory=list
    )

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        assert target.startswith("/") and not target.startswith("//")
        assert all(isinstance(name, bytes) and isinstance(value, bytes) for name, value in headers)
        self.requests.append((method, target, headers, body))
        return self.responses.pop(0)


def response(status: int = 200, body: bytes = b'{"ok":true}') -> ClientResponse:
    return ClientResponse(status, (), body, "1.1")


class FakeDatabase:
    pass


def test_real_job_runner_accepts_the_registered_task_name() -> None:
    jobs = JobRunner(FakeDatabase(), name="chatjobs")
    chat = ChatOps(
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET),),
        jobs=jobs,
        inbox=object(),
    )

    @chat.command("deploy", execution="durable")
    async def deploy(environment: str) -> None:
        pass

    assert len(jobs._tasks) == 1
    assert next(iter(jobs._tasks)).startswith("chat_operations_deploy")


async def test_origin_pinned_clients_receive_relative_targets_and_wire_headers() -> None:
    api = OriginClient([response(body=b'{"ok":true,"ts":"1.2"}')])
    hooks = OriginClient([response()])
    slack = Slack(
        signing_secret=SIGNING_SECRET,
        api_client=api,
        response_client=hooks,
    )
    installation = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"chat:write"}),
    )

    await slack.call(installation, "chat.postMessage", {"channel": "C123", "text": "hi"})
    target = slack.response_url(
        "https://hooks.slack.com/commands/T123/1/secret", installation="T123"
    )
    await slack.respond(target, "done")

    assert api.requests[0][1] == "/api/chat.postMessage"
    assert (b"authorization", b"Bearer xoxb-secret") in api.requests[0][2]
    assert hooks.requests[0][1] == "/commands/T123/1/secret"


async def test_shared_installation_owner_is_used_when_provider_has_none(
    slash_values: dict[str, str],
) -> None:
    installation = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"commands"}),
    )
    installations = RecordingInstallationStore({(None, "T123"): installation})
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        installations=installations,
    )

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        pass

    body = form_body(**slash_values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    result = await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)

    assert result.status == 200
    assert installations.queries == [(None, "T123", False)]


async def test_enterprise_event_uses_nested_authorization_facts() -> None:
    installation = SlackInstallation(
        app_id="A123",
        enterprise_id="E123",
        team_id=None,
        is_enterprise_install=True,
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"app_mentions:read"}),
    )
    installations = RecordingInstallationStore({("E123", None): installation})
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(
            Slack(
                signing_secret=SIGNING_SECRET,
                installations=installations,
                clock=lambda: NOW,
            ),
        ),
    )
    seen: list[ChatContext] = []

    @chat.event("app_mention")
    async def mention(context: ChatContext) -> None:
        seen.append(context)

    envelope = {
        "type": "event_callback",
        "api_app_id": "A123",
        "team_id": "T999",
        "context_enterprise_id": "E123",
        "context_team_id": "T999",
        "event_id": "Ev123",
        "authorizations": [
            {
                "enterprise_id": "E123",
                "team_id": "T999",
                "user_id": "UAPP",
                "is_bot": True,
                "is_enterprise_install": True,
            }
        ],
        "event": {"type": "app_mention", "user": "U123", "channel": "C123"},
    }
    body = json_body(envelope)
    result = await TestClient(app).post(
        "/chat/slack/events", content=body, headers=signed_headers(body)
    )

    assert result.status == 200
    assert installations.queries == [("E123", "T999", True)]
    assert seen[0].installation == "E123"
    assert seen[0].external_identity.installation == "E123"


async def test_local_replay_owner_stays_at_its_hard_capacity() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW, replay_entries=4)
    chat = ChatOps(name="operations", providers=(slack,))
    for number in range(4):
        inbound = _Inbound(
            "event",
            "app_mention",
            "T123",
            "U123",
            "C123",
            f"Ev{number}",
            {"api_app_id": "A123"},
        )
        assert await slack._claim(chat, inbound)
    for number in range(4, 12):
        inbound = _Inbound(
            "event", "app_mention", "T123", "U123", "C123", f"Ev{number}", {"api_app_id": "A123"}
        )
        assert not await slack._claim(chat, inbound)
    assert slack._replay.size == 4


def test_manifest_refuses_an_event_without_an_explicit_scope_mapping() -> None:
    chat = ChatOps(name="operations", providers=(Slack(signing_secret=SIGNING_SECRET),))

    @chat.event("future_event")
    async def future_event() -> None:
        pass

    with pytest.raises(ValueError, match="future_event.*event_scopes"):
        chat.manifest("slack", base_url="https://ops.example")


def test_manifest_uses_explicit_custom_event_scope_mapping() -> None:
    chat = ChatOps(
        name="operations",
        providers=(
            Slack(
                signing_secret=SIGNING_SECRET,
                event_scopes={"future_event": ("channels:history",)},
            ),
        ),
    )

    @chat.event("future_event")
    async def future_event() -> None:
        pass

    manifest = chat.manifest("slack", base_url="https://ops.example")
    assert manifest["oauth_config"]["scopes"]["bot"] == ["channels:history"]
