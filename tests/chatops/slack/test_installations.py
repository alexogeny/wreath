from __future__ import annotations

from wreath import Wreath
from wreath.chat import ChatContext, ChatOps
from wreath.chat.slack import Slack, SlackInstallation
from wreath.testing import TestClient

from .conftest import (
    NOW,
    SIGNING_SECRET,
    RecordingInstallationStore,
    form_body,
    signed_headers,
)


async def request(app: Wreath, **values: str):
    body = form_body(**values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)


def app_for(store: RecordingInstallationStore) -> tuple[ChatOps, Wreath]:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(
            Slack(
                signing_secret=SIGNING_SECRET,
                app_id="A123",
                installations=store,
                clock=lambda: NOW,
            ),
        ),
    )
    return chat, app


async def test_workspace_installation_is_selected_by_team_and_enterprise_fact(
    slash_values: dict[str, str],
) -> None:
    installation = SlackInstallation(
        app_id="A123",
        enterprise_id="E123",
        team_id="T123",
        is_enterprise_install=False,
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"commands", "chat:write"}),
    )
    store = RecordingInstallationStore({("E123", "T123"): installation})
    chat, app = app_for(store)
    seen: list[ChatContext] = []

    @chat.command("deploy")
    async def deploy(command: ChatContext, environment: str) -> None:
        seen.append(command)

    response = await request(app, **slash_values, enterprise_id="E123")

    assert (response.status, response.body) == (200, b"")
    assert store.queries == [("E123", "T123", False)]
    assert seen[0].installation == installation.key


async def test_org_installation_uses_enterprise_identity_without_inventing_a_team_owner() -> None:
    installation = SlackInstallation(
        app_id="A123",
        enterprise_id="E123",
        team_id=None,
        is_enterprise_install=True,
        bot_token="xoxb-org-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"commands"}),
    )
    store = RecordingInstallationStore({("E123", None): installation})
    chat, app = app_for(store)
    seen: list[ChatContext] = []

    @chat.command("deploy")
    async def deploy(context: ChatContext, environment: str) -> None:
        seen.append(context)

    response = await request(
        app,
        api_app_id="A123",
        enterprise_id="E123",
        is_enterprise_install="true",
        team_id="T999",
        user_id="U123",
        channel_id="C123",
        command="/deploy",
        text="production",
        response_url="https://hooks.slack.com/commands/T999/1/secret",
    )

    assert response.status == 200
    assert store.queries == [("E123", "T999", True)]
    assert seen[0].installation == installation.key
    assert seen[0].tenant == f"slack:{installation.key}"


async def test_unknown_installation_is_refused_before_handler_dispatch(
    slash_values: dict[str, str],
) -> None:
    store = RecordingInstallationStore()
    chat, app = app_for(store)
    ran = False

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        nonlocal ran
        ran = True

    response = await request(app, **slash_values)
    assert response.status == 401
    assert ran is False


async def test_payload_for_another_slack_app_is_refused(
    slash_values: dict[str, str],
) -> None:
    installation = SlackInstallation(
        app_id="A123",
        team_id="T123",
        bot_token="xoxb-secret",
        bot_user_id="UAPP",
        scopes=frozenset({"commands"}),
    )
    store = RecordingInstallationStore({(None, "T123"): installation})
    chat, app = app_for(store)

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        raise AssertionError("wrong app reached the command")

    response = await request(app, **dict(slash_values, api_app_id="A999"))
    assert response.status == 401


def test_installation_requires_exactly_one_workspace_or_enterprise_owner() -> None:
    for values in (
        {"enterprise_id": None, "team_id": None, "is_enterprise_install": False},
        {"enterprise_id": None, "team_id": "T123", "is_enterprise_install": True},
        {"enterprise_id": "E123", "team_id": None, "is_enterprise_install": False},
    ):
        try:
            SlackInstallation(
                app_id="A123",
                bot_token="xoxb-secret",
                bot_user_id="UAPP",
                scopes=frozenset(),
                **values,
            )
        except ValueError:
            continue
        raise AssertionError(f"invalid installation accepted: {values}")
