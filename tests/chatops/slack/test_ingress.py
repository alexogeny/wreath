from __future__ import annotations

import json

import pytest

from wreath import Wreath
from wreath.chat import ChatContext, ChatOps
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, form_body, json_body, signed_headers


def mounted(slack: Slack) -> tuple[ChatOps, Wreath]:
    app = Wreath()
    chat = ChatOps(app, name="operations", providers=(slack,), path="/chat")
    return chat, app


async def test_url_verification_returns_the_exact_plaintext_challenge() -> None:
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    body = json_body(
        {"token": "deprecated", "challenge": "challenge-value", "type": "url_verification"}
    )

    _, app = mounted(slack)
    response = await TestClient(app).post(
        "/chat/slack/events", content=body, headers=signed_headers(body)
    )

    assert response.status == 200
    assert response.body == b"challenge-value"
    assert dict(response.headers)[b"content-type"] == b"text/plain; charset=utf-8"


async def test_events_api_dispatches_the_inner_event_and_exact_envelope(
    event_envelope: dict[str, object],
) -> None:
    seen: list[ChatContext] = []
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    chat, app = mounted(slack)

    @chat.event("app_mention")
    async def mention(event: ChatContext) -> None:
        seen.append(event)

    body = json_body(event_envelope)
    response = await TestClient(app).post(
        "/chat/slack/events", content=body, headers=signed_headers(body)
    )

    assert response.status == 200
    assert response.body == b""
    assert len(seen) == 1
    assert seen[0].provider == "slack"
    assert seen[0].delivery_id == "Ev123"
    assert seen[0].installation == "T123"
    assert seen[0].actor == "U123"
    assert seen[0].conversation == "C123"
    assert seen[0].native == event_envelope


async def test_unknown_events_are_acknowledged_without_calling_another_handler(
    event_envelope: dict[str, object],
) -> None:
    seen: list[ChatContext] = []
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    chat, app = mounted(slack)

    @chat.event("reaction_added")
    async def reaction(event: ChatContext) -> None:
        seen.append(event)

    body = json_body(event_envelope)
    response = await TestClient(app).post(
        "/chat/slack/events", content=body, headers=signed_headers(body)
    )

    assert response.status == 200
    assert response.body == b""
    assert seen == []


async def test_duplicate_event_delivery_is_claimed_before_dispatch(
    event_envelope: dict[str, object],
) -> None:
    seen: list[str] = []
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    chat, app = mounted(slack)

    @chat.event("app_mention")
    async def mention(event: ChatContext) -> None:
        seen.append(event.delivery_id)

    body = json_body(event_envelope)
    headers = signed_headers(body)
    client = TestClient(app)
    first = await client.post("/chat/slack/events", content=body, headers=headers)
    duplicate = await client.post(
        "/chat/slack/events",
        content=body,
        headers={
            **headers,
            "x-slack-retry-num": "1",
            "x-slack-retry-reason": "http_timeout",
        },
    )

    assert (first.status, first.body) == (200, b"")
    assert (duplicate.status, duplicate.body) == (200, b"")
    assert seen == ["Ev123"]


async def test_same_event_id_is_namespaced_by_api_app_and_installation(
    event_envelope: dict[str, object],
) -> None:
    seen: list[tuple[str, str]] = []
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    chat, app = mounted(slack)

    @chat.event("app_mention")
    async def mention(event: ChatContext) -> None:
        seen.append((str(event.native["api_app_id"]), event.installation))

    client = TestClient(app)
    for app_id, team_id in (("A123", "T123"), ("A999", "T123"), ("A123", "T999")):
        envelope = dict(event_envelope, api_app_id=app_id, team_id=team_id)
        body = json_body(envelope)
        await client.post("/chat/slack/events", content=body, headers=signed_headers(body))

    assert seen == [("A123", "T123"), ("A999", "T123"), ("A123", "T999")]


async def test_interactivity_decodes_the_single_form_payload_field() -> None:
    seen: list[ChatContext] = []
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    chat, app = mounted(slack)

    @chat.action("approve")
    async def approve(interaction: ChatContext) -> None:
        seen.append(interaction)

    payload = {
        "type": "block_actions",
        "api_app_id": "A123",
        "team": {"id": "T123", "domain": "acme"},
        "user": {"id": "U123", "username": "mara"},
        "channel": {"id": "C123", "name": "operations"},
        "actions": [{"action_id": "approve", "type": "button", "value": "release-7"}],
        "response_url": "https://hooks.slack.com/actions/T123/1/secret",
        "trigger_id": "123.456",
    }
    body = form_body(payload=json.dumps(payload, separators=(",", ":")))
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    response = await TestClient(app).post("/chat/slack/interactions", content=body, headers=headers)

    assert (response.status, response.body) == (200, b"")
    assert len(seen) == 1
    assert seen[0].native["actions"][0]["action_id"] == "approve"
    assert seen[0].native["actions"][0]["value"] == "release-7"
    assert seen[0].raw == payload


@pytest.mark.parametrize(
    ("path", "content_type", "body"),
    [
        ("/chat/slack/events", "application/x-www-form-urlencoded", b"payload=%7B%7D"),
        ("/chat/slack/commands", "application/json", b"{}"),
        ("/chat/slack/interactions", "application/json", b"{}"),
    ],
)
async def test_each_ingress_refuses_the_wrong_media_type(
    path: str, content_type: str, body: bytes
) -> None:
    slack = Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW)
    headers = signed_headers(body)
    headers["content-type"] = content_type
    _, app = mounted(slack)
    response = await TestClient(app).post(path, content=body, headers=headers)
    assert response.status == 415
