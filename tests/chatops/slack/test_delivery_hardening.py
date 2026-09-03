from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

import wreath.chat.slack as slack_api
from wreath import Wreath
from wreath.chat import ChatOps, ChatTenantMismatch
from wreath.chat.slack import Slack, SlackInstallation
from wreath.http_client import ClientResponse
from wreath.jobs import JobContext
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, form_body, signed_headers


@dataclass
class AtomicInbox:
    transaction: object = field(default_factory=object)
    calls: list[dict[str, Any]] = field(default_factory=list)
    accepted: set[tuple[str, str]] = field(default_factory=set)

    async def claim(self, **_options: Any) -> bool:
        raise AssertionError("durable Slack delivery used a non-atomic inbox claim")

    async def claim_and_enqueue(
        self,
        *,
        source: str,
        envelope: Any,
        enqueue: Any,
        **_options: Any,
    ) -> bool:
        self.calls.append(
            {
                "source": source,
                "message_id": envelope.id,
                "tenant": source,
                "body": envelope.body,
                "event_type": envelope.type,
                "sent_at": envelope.timestamp.timestamp(),
                "result_status": 200,
            }
        )
        key = (source, envelope.id)
        if key in self.accepted:
            return False
        self.accepted.add(key)
        await enqueue(transaction=self.transaction)
        return True


class RecordingJobs:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.enqueued: list[dict[str, Any]] = []

    def task(self, name: str):
        def register(handler: Any) -> Any:
            self.registered[name] = handler
            return handler

        return register

    async def enqueue(
        self,
        task: str,
        payload: Any,
        *,
        key: str,
        tenant: str,
        tx: Any,
    ) -> int:
        self.enqueued.append(
            {
                "task": task,
                "payload": payload,
                "key": key,
                "tenant": tenant,
                "tx": tx,
            }
        )
        return 17


def slash_body() -> bytes:
    return form_body(
        api_app_id="A123",
        team_id="T123",
        channel_id="C123",
        user_id="U123",
        command="/deploy",
        text="production",
    )


async def post_command(app: Wreath, body: bytes):
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)


async def test_inline_slash_command_claims_replay_before_dispatch() -> None:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
    )
    calls = 0

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        nonlocal calls
        calls += 1

    body = slash_body()
    first = await post_command(app, body)
    duplicate = await post_command(app, body)

    assert (first.status, first.body) == (200, b"")
    assert (duplicate.status, duplicate.body) == (200, b"")
    assert calls == 1


async def test_durable_claim_and_enqueue_share_the_inbox_transaction_and_tenant() -> None:
    inbox = AtomicInbox()
    jobs = RecordingJobs()
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        inbox=inbox,
        jobs=jobs,
    )

    @chat.command("deploy", execution="durable")
    async def deploy(environment: str) -> None:
        raise AssertionError("durable command ran inline")

    body = slash_body()
    first = await post_command(app, body)
    duplicate = await post_command(app, body)

    assert (first.status, first.body) == (200, b"")
    assert (duplicate.status, duplicate.body) == (200, b"")
    assert len(jobs.enqueued) == 1
    queued = jobs.enqueued[0]
    assert queued["tenant"] == "slack:T123"
    assert queued["tx"] is inbox.transaction
    assert queued["key"] == queued["payload"]["context"]["delivery_id"]
    assert inbox.calls == [
        {
            "source": "slack:T123",
            "message_id": queued["key"],
            "tenant": "slack:T123",
            "body": body,
            "event_type": "command:deploy",
            "sent_at": float(NOW),
            "result_status": 200,
        },
        {
            "source": "slack:T123",
            "message_id": queued["key"],
            "tenant": "slack:T123",
            "body": body,
            "event_type": "command:deploy",
            "sent_at": float(NOW),
            "result_status": 200,
        },
    ]


async def test_durable_command_is_authorized_before_it_is_enqueued() -> None:
    class Authorizer:
        def __init__(self) -> None:
            self.actions: list[str] = []

        async def authorize(self, _context: Any, requirement: Any) -> Any:
            from wreath.authorization import AuthorizationDecision

            self.actions.append(requirement.action)
            return AuthorizationDecision(True)

    authorizer = Authorizer()
    inbox = AtomicInbox()
    jobs = RecordingJobs()
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        inbox=inbox,
        jobs=jobs,
        authorizer=authorizer,
    )

    @chat.command("deploy", execution="durable", action="Release::deploy")
    async def deploy(environment: str) -> None:
        raise AssertionError("durable command ran inline")

    assert (await post_command(app, slash_body())).status == 200
    assert authorizer.actions == ["Release::deploy"]
    assert len(jobs.enqueued) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "slack:T999"),
        ("key", "another-delivery"),
        ("fence", 0),
        ("fence", None),
    ],
)
async def test_durable_worker_refuses_every_misbound_job_fact(field: str, value: Any) -> None:
    inbox = AtomicInbox()
    jobs = RecordingJobs()
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        inbox=inbox,
        jobs=jobs,
    )
    dispatched = False

    @chat.command("deploy", execution="durable")
    async def deploy(environment: str) -> None:
        nonlocal dispatched
        dispatched = True

    assert (await post_command(app, slash_body())).status == 200
    queued = jobs.enqueued[0]
    facts: dict[str, Any] = {
        "tenant": queued["tenant"],
        "key": queued["key"],
        "fence": 1,
    }
    facts[field] = value
    job = JobContext(
        job_id=17,
        task=queued["task"],
        attempt=1,
        fence=facts["fence"],
        tenant=facts["tenant"],
        key=facts["key"],
    )

    with pytest.raises(RuntimeError, match="stale or misbound durable Slack job"):
        await jobs.registered[queued["task"]](job, queued["payload"])
    assert dispatched is False


@dataclass
class OriginClient:
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: Any,
        body: bytes,
        idempotency_key: str,
    ) -> ClientResponse:
        self.requests.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "body": body,
                "idempotency_key": idempotency_key,
            }
        )
        return ClientResponse(200, (), b'{"ok":true,"ts":"1.2"}', "1.1")


def installation() -> SlackInstallation:
    return SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"chat:write"}),
    )


async def test_chat_send_binds_slack_tenant_and_provider_idempotency() -> None:
    client = OriginClient()
    provider = Slack(signing_secret=SIGNING_SECRET, api_client=client)
    chat = ChatOps(name="operations", providers=(provider,))
    destination = slack_api.SlackDestination(
        channel_id="C123",
        tenant="slack:T123",
        installation=installation(),
    )

    result = await chat.send(
        tenant="slack:T123",
        destination=destination,
        content="scheduled report",
        idempotency_key="report:2026-09-02",
    )

    assert result["ts"] == "1.2"
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["target"] == "/api/chat.postMessage"
    assert request["idempotency_key"] == "report:2026-09-02"
    assert json.loads(request["body"]) == {
        "channel": "C123",
        "client_msg_id": "report:2026-09-02",
        "text": "scheduled report",
    }


async def test_chat_send_preserves_idempotency_through_the_absolute_url_client() -> None:
    client = OriginClient()
    chat = ChatOps(
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET, http_client=client),),
    )
    destination = slack_api.SlackDestination(
        channel_id="C123",
        tenant="slack:T123",
        installation=installation(),
    )

    await chat.send(
        tenant="slack:T123",
        destination=destination,
        content="scheduled report",
        idempotency_key="report:2026-09-02",
    )

    assert client.requests[0]["target"] == "https://slack.com/api/chat.postMessage"
    assert client.requests[0]["idempotency_key"] == "report:2026-09-02"


async def test_chat_send_refuses_cross_tenant_destination_before_transport() -> None:
    client = OriginClient()
    chat = ChatOps(
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET, api_client=client),),
    )
    destination = slack_api.SlackDestination(
        channel_id="C123",
        tenant="slack:T999",
        installation=installation(),
    )

    with pytest.raises(ChatTenantMismatch, match="slack:T999"):
        await chat.send(
            tenant="slack:T123",
            destination=destination,
            content="scheduled report",
            idempotency_key="report:2026-09-02",
        )
    assert client.requests == []


async def test_chat_send_refuses_a_tenant_bound_to_another_installation() -> None:
    client = OriginClient()
    chat = ChatOps(
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET, api_client=client),),
    )
    destination = slack_api.SlackDestination(
        channel_id="C123",
        tenant="slack:T999",
        installation=installation(),
    )

    with pytest.raises(ChatTenantMismatch, match="slack:T999"):
        await chat.send(
            tenant="slack:T999",
            destination=destination,
            content="scheduled report",
            idempotency_key="report:2026-09-02",
        )
    assert client.requests == []


async def test_chat_send_refuses_an_empty_idempotency_key() -> None:
    client = OriginClient()
    chat = ChatOps(
        name="operations",
        providers=(Slack(signing_secret=SIGNING_SECRET, api_client=client),),
    )
    destination = slack_api.SlackDestination(
        channel_id="C123",
        tenant="slack:T123",
        installation=installation(),
    )

    with pytest.raises(ValueError, match="idempotency_key"):
        await chat.send(
            tenant="slack:T123",
            destination=destination,
            content="scheduled report",
            idempotency_key="",
        )
    assert client.requests == []
