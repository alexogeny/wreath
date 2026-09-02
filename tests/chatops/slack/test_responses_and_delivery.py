from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from wreath import Wreath
from wreath.chat import ChatOps, ChatReply
from wreath.chat.slack import Slack, SlackInstallation, SlackRateLimited
from wreath.testing import TestClient

from .conftest import (
    NOW,
    SIGNING_SECRET,
    RecordingTransport,
    form_body,
    signed_headers,
)


class RecordingJobs:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}
        self.enqueued: list[tuple[str, object, str | None]] = []
        self.enqueue_options: list[tuple[str, object | None]] = []

    def task(self, name: str):
        def register(handler: object) -> object:
            self.registered[name] = handler
            return handler

        return register

    async def enqueue(
        self,
        task: str,
        payload: object,
        *,
        key: str | None = None,
        tenant: str = "",
        tx: object | None = None,
    ) -> int:
        self.enqueued.append((task, payload, key))
        self.enqueue_options.append((tenant, tx))
        return 17


class MemoryInbox:
    def __init__(self) -> None:
        self.claims: set[tuple[str, str, str]] = set()
        self.atomic_claims: set[tuple[str, str]] = set()

    async def claim(self, *, provider: str, installation: str, delivery: str) -> bool:
        key = (provider, installation, delivery)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def claim_and_enqueue(
        self,
        *,
        source: str,
        envelope: object,
        enqueue: Any,
        **_options: object,
    ) -> bool:
        delivery = str(envelope.id)
        key = (source, delivery)
        if key in self.atomic_claims:
            return False
        self.atomic_claims.add(key)
        await enqueue(transaction=self)
        return True


def configured(
    *,
    jobs: object | None = None,
    transport: object | None = None,
    sleep: object | None = None,
    inbox: object | None = None,
):
    app = Wreath()
    slack = Slack(
        signing_secret=SIGNING_SECRET,
        clock=lambda: NOW,
        http_client=transport,
        sleep=sleep or asyncio.sleep,
    )
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(slack,),
        jobs=jobs,
        inbox=inbox,
    )
    return chat, slack, app


async def invoke(app: Wreath, values: dict[str, str]):
    body = form_body(**values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)


async def test_none_is_the_exact_empty_ack(slash_values: dict[str, str]) -> None:
    chat, _, app = configured()

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        pass

    response = await invoke(app, slash_values)
    assert response.status == 200
    assert response.body == b""


async def test_durable_command_acks_before_work_and_uses_a_delivery_dedup_key(
    slash_values: dict[str, str],
) -> None:
    jobs = RecordingJobs()
    chat, _, app = configured(jobs=jobs, inbox=MemoryInbox())
    ran = False

    @chat.command("deploy", execution="durable")
    async def deploy(environment: str) -> ChatReply:
        nonlocal ran
        ran = True
        return ChatReply.ephemeral(f"deployed {environment}")

    first = await invoke(app, slash_values)
    duplicate = await invoke(app, slash_values)

    assert (first.status, first.body) == (200, b"")
    assert (duplicate.status, duplicate.body) == (200, b"")
    assert ran is False
    assert len(jobs.enqueued) == 1
    assert jobs.enqueued[0][2]
    assert jobs.registered.keys() == {"chat_operations_deploy"}


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/commands/T123/1/secret",
        "https://hooks.slack.com.evil.example/commands/T123/1/secret",
        "https://user@hooks.slack.com/commands/T123/1/secret",
        "https://127.0.0.1/actions/T123/1/secret",
        "https://hooks.slack.com:444/commands/T123/1/secret",
        "https://hooks.slack.com/commands/T999/1/secret",
    ],
)
def test_response_url_is_pinned_to_slack_and_the_installation(url: str) -> None:
    _, slack, _ = configured()
    with pytest.raises(ValueError, match="response_url"):
        slack.response_url(url, installation="T123")


async def test_delayed_response_posts_only_to_a_validated_response_url() -> None:
    transport = RecordingTransport()
    _, slack, _ = configured(transport=transport)
    target = slack.response_url(
        "https://hooks.slack.com/commands/T123/1/secret", installation="T123"
    )
    await slack.respond(target, ChatReply.ephemeral("done"))

    assert len(transport.requests) == 1
    method, url, headers, body = transport.requests[0]
    assert (method, url) == ("POST", "https://hooks.slack.com/commands/T123/1/secret")
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"response_type": "ephemeral", "text": "done"}


async def test_web_api_429_honours_retry_after_without_blocking_other_workspaces() -> None:
    transport = RecordingTransport(
        [
            (429, {"retry-after": "2"}, {"ok": False, "error": "ratelimited"}),
            (200, {}, {"ok": True, "ts": "1.2"}),
            (200, {}, {"ok": True, "ts": "1.3"}),
        ]
    )
    waits: list[float] = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    _, slack, _ = configured(transport=transport, sleep=record_wait)
    acme = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-acme",
        bot_user_id="UAPP",
        scopes=frozenset({"chat:write"}),
    )
    globex = SlackInstallation(
        app_id="A123",
        team_id="T999",
        bot_token="xoxb-globex",
        bot_user_id="UAPP2",
        scopes=frozenset({"chat:write"}),
    )

    acme_result = await slack.call(
        acme, "chat.postMessage", {"channel": "C123", "text": "hello"}, idempotent=True
    )
    globex_result = await slack.call(
        globex, "chat.postMessage", {"channel": "C999", "text": "hello"}, idempotent=True
    )

    assert waits == [2.0]
    assert acme_result["ts"] == "1.2"
    assert globex_result["ts"] == "1.3"


async def test_non_idempotent_native_call_surfaces_rate_limit_without_replaying() -> None:
    transport = RecordingTransport(
        [(429, {"retry-after": "30"}, {"ok": False, "error": "ratelimited"})]
    )
    _, slack, _ = configured(transport=transport)
    installation = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-acme",
        bot_user_id="UAPP",
        scopes=frozenset({"views:write"}),
    )

    with pytest.raises(SlackRateLimited, match="30"):
        await slack.call(installation, "views.open", {"trigger_id": "123.456"})
    assert len(transport.requests) == 1


async def test_provider_native_escape_hatch_preserves_unknown_slack_fields() -> None:
    transport = RecordingTransport([(200, {}, {"ok": True, "view": {"id": "V123"}})])
    _, slack, _ = configured(transport=transport)
    installation = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-acme",
        bot_user_id="UAPP",
        scopes=frozenset({"views:write"}),
    )
    native = {
        "trigger_id": "123.456",
        "view": {"type": "modal", "callback_id": "future-slack-surface", "blocks": []},
    }
    result = await slack.call(installation, "views.open", native)
    assert result["view"]["id"] == "V123"
    assert json.loads(transport.requests[0][3]) == native
