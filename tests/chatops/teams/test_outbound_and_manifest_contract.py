from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from typing import Any

import pytest

import wreath.chat.teams as teams_module
from wreath.chat import ChatOps, ChatReply, ChatTenantMismatch
from wreath.chat.teams import (
    Teams,
    TeamsActivity,
    TeamsBotConfig,
    TeamsInstallation,
    TeamsManifest,
    TeamsRefusal,
)

from ._support import (
    APP_ID,
    ENTRA_TENANT,
    SERVICE_URL,
    MemoryInstallations,
    RecordingConnector,
    activity,
)


def provider(connector: Any = None) -> Teams:
    return Teams(
        config=TeamsBotConfig(
            app_id=APP_ID,
            app_secret="secret",
            messaging_endpoint="https://chat.example.test/_wreath/chat/teams",
            allowed_tenants=frozenset({ENTRA_TENANT}),
            login_issuers={ENTRA_TENANT: f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"},
        ),
        connector=connector or RecordingConnector(),
        token_provider=lambda: "connector-access-token",
    )


async def test_reply_uses_the_verified_service_url_and_exact_reply_route() -> None:
    connector = RecordingConnector()
    teams = provider(connector)
    incoming = TeamsActivity.parse(activity())

    await teams.reply(incoming, ChatReply.text("Working on it"))

    request = connector.requests[0]
    assert request.method == "POST"
    assert request.url == (
        f"{SERVICE_URL}v3/conversations/19%3Aconversation%40thread.tacv2/activities/activity-1"
    )
    assert request.headers == {
        "authorization": "Bearer connector-access-token",
        "content-type": "application/json",
    }
    assert request.json == {
        "type": "message",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": APP_ID, "name": "Wreath"},
        "recipient": {
            "id": "29:opaque-teams-member-id",
            "name": "Ada Lovelace",
        },
        "conversation": {"id": "19:conversation@thread.tacv2"},
        "replyToId": "activity-1",
        "text": "Working on it",
    }


async def test_proactive_message_uses_a_stored_tenant_scoped_conversation_reference() -> None:
    connector = RecordingConnector()
    teams = provider(connector)
    installation = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="19:team@thread.tacv2",
        service_url=SERVICE_URL,
        conversation_id="19:conversation@thread.tacv2",
        bot_id=APP_ID,
    )

    await teams.proactive(installation, ChatReply.text("Deployment finished"))

    request = connector.requests[0]
    assert request.url == (
        f"{SERVICE_URL}v3/conversations/19%3Aconversation%40thread.tacv2/activities"
    )
    assert request.json == {
        "type": "message",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": APP_ID},
        "conversation": {"id": "19:conversation@thread.tacv2"},
        "text": "Deployment finished",
    }
    assert "replyToId" not in request.json


async def test_reply_serializes_adaptive_cards_and_refuses_unknown_shapes() -> None:
    connector = RecordingConnector()
    teams = provider(connector)
    incoming = TeamsActivity.parse(activity())
    card = {
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [{"type": "TextBlock", "text": "Approved"}],
    }
    await teams.reply(incoming, ChatReply.card(card))
    assert connector.requests[0].json["attachments"] == [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }
    ]

    with pytest.raises(TypeError, match="Teams repl"):
        await teams.reply(incoming, object())


async def test_proactive_delivery_refuses_foreign_tenants_and_empty_idempotency() -> None:
    foreign = TeamsInstallation(
        tenant_id="foreign-tenant",
        installation_id="team",
        service_url=SERVICE_URL,
        conversation_id="conversation",
        bot_id=APP_ID,
    )
    with pytest.raises(TeamsRefusal) as raised:
        await provider().proactive(foreign, "hello")
    assert raised.value.reason == "unconfigured-tenant"

    allowed = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="team",
        service_url=SERVICE_URL,
        conversation_id="conversation",
        bot_id=APP_ID,
    )
    with pytest.raises(ValueError, match="idempotency_key"):
        await provider().proactive(allowed, "hello", idempotency_key="")


async def test_shared_send_routes_to_a_tenant_scoped_teams_installation() -> None:
    connector = RecordingConnector()
    teams = provider(connector)
    chat = ChatOps(name="operations", providers=(teams,))
    installation = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="19:team@thread.tacv2",
        service_url=SERVICE_URL,
        conversation_id="19:conversation@thread.tacv2",
        bot_id=APP_ID,
    )

    await chat.send(
        tenant=f"teams:{ENTRA_TENANT}",
        destination=installation,
        content="Deployment finished",
        idempotency_key="delivery-7",
    )

    assert connector.requests[0].headers["x-ms-client-request-id"] == "delivery-7"
    assert connector.requests[0].json["text"] == "Deployment finished"


@pytest.mark.parametrize("tenant", [ENTRA_TENANT, "slack:tenant", "teams", "teams:"])
async def test_shared_send_requires_the_provider_qualified_tenant(tenant: str) -> None:
    with pytest.raises(ValueError, match="teams:<tenant-id>"):
        await provider().send(
            tenant=tenant,
            destination="team",
            content="hello",
            idempotency_key="delivery-1",
        )


async def test_shared_send_resolves_and_validates_stored_installations() -> None:
    connector = RecordingConnector()
    installation = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="team",
        service_url=SERVICE_URL,
        conversation_id="conversation",
        bot_id=APP_ID,
    )
    teams = provider(connector)
    teams.installations = MemoryInstallations({(ENTRA_TENANT, "team"): installation})
    await teams.send(
        tenant=f"teams:{ENTRA_TENANT}",
        destination="team",
        content="hello",
        idempotency_key="delivery-1",
    )
    assert connector.requests[0].url.endswith("/conversations/conversation/activities")

    teams.installations = MemoryInstallations()
    with pytest.raises(KeyError):
        await teams.send(
            tenant=f"teams:{ENTRA_TENANT}",
            destination="missing",
            content="hello",
            idempotency_key="delivery-1",
        )


async def test_shared_send_requires_an_installation_store_and_tenant_match() -> None:
    teams = provider()
    with pytest.raises(RuntimeError, match="installation store"):
        await teams.send(
            tenant=f"teams:{ENTRA_TENANT}",
            destination="missing",
            content="hello",
            idempotency_key="delivery-1",
        )

    foreign = TeamsInstallation(
        tenant_id="foreign-tenant",
        installation_id="foreign",
        service_url=SERVICE_URL,
        conversation_id="conversation",
        bot_id=APP_ID,
    )
    teams.installations = MemoryInstallations({(ENTRA_TENANT, "foreign"): foreign})
    with pytest.raises(ChatTenantMismatch):
        await teams.send(
            tenant=f"teams:{ENTRA_TENANT}",
            destination="foreign",
            content="hello",
            idempotency_key="delivery-1",
        )


async def test_default_connector_token_is_cached_until_its_refresh_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    now = [1000.0]

    def fetch(_config: TeamsBotConfig) -> tuple[str, float]:
        calls.append("fetch")
        return f"token-{len(calls)}", 3600.0

    monkeypatch.setattr(teams_module, "_fetch_connector_token", fetch)
    teams = Teams(
        config=provider().config,
        connector=RecordingConnector(),
        clock=lambda: now[0],
    )

    assert await teams._connector_token() == "token-1"
    assert await teams._connector_token() == "token-1"
    now[0] += 3541
    assert await teams._connector_token() == "token-2"
    assert calls == ["fetch", "fetch"]


async def test_concurrent_connector_token_requests_share_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch(_config: TeamsBotConfig) -> tuple[str, float]:
        calls.append("fetch")
        time.sleep(0.01)
        return "shared-token", 3600.0

    monkeypatch.setattr(teams_module, "_fetch_connector_token", fetch)
    teams = Teams(config=provider().config, connector=RecordingConnector(), clock=lambda: 1000.0)
    assert await asyncio.gather(teams._connector_token(), teams._connector_token()) == [
        "shared-token",
        "shared-token",
    ]
    assert calls == ["fetch"]


@pytest.mark.parametrize(
    "service_url",
    [
        "http://smba.trafficmanager.net/amer/",
        "https://user:password@smba.trafficmanager.net/amer/",
        "https://smba.trafficmanager.net.evil.example/amer/",
        "https://127.0.0.1/internal/",
        "https://smba.trafficmanager.net/amer/?query=/",
        "https://smba.trafficmanager.net/amer/#fragment/",
        "https://smba.trafficmanager.net/amer",
    ],
)
async def test_proactive_messages_never_turn_stored_service_url_into_ssrf(
    service_url: str,
) -> None:
    connector = RecordingConnector()
    installation = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="team",
        service_url=service_url,
        conversation_id="conversation",
        bot_id=APP_ID,
    )

    with pytest.raises(TeamsRefusal, match="serviceUrl") as raised:
        await provider(connector).proactive(installation, ChatReply.text("hello"))
    assert raised.value.reason == "untrusted-service-url"
    assert connector.requests == []


async def test_proactive_rejects_each_service_url_authority_and_suffix_hazard() -> None:
    for service_url in (
        "https:///amer/",
        "https://user@smba.trafficmanager.net/amer/",
        "https://:password@smba.trafficmanager.net/amer/",
        "https://smba.trafficmanager.net/amer/?query=/",
        "https://smba.trafficmanager.net/amer/#fragment/",
    ):
        installation = TeamsInstallation(
            tenant_id=ENTRA_TENANT,
            installation_id="team",
            service_url=service_url,
            conversation_id="conversation",
            bot_id=APP_ID,
        )
        with pytest.raises(TeamsRefusal) as raised:
            await provider().proactive(installation, "hello")
        assert raised.value.reason == "untrusted-service-url"


async def test_a_service_url_change_replaces_the_reference_only_after_verified_ingress() -> None:
    installation = TeamsInstallation(
        tenant_id=ENTRA_TENANT,
        installation_id="19:team@thread.tacv2",
        service_url=SERVICE_URL,
        conversation_id="old-conversation",
        bot_id=APP_ID,
    )
    changed = TeamsActivity.parse(
        activity(
            serviceUrl="https://smba.trafficmanager.net/emea/",
            conversation={
                "id": "new-conversation",
                "conversationType": "channel",
                "tenantId": ENTRA_TENANT,
            },
        )
    )

    refreshed = installation.refreshed_from(changed, connector_verified=True)
    assert refreshed.service_url == "https://smba.trafficmanager.net/emea/"
    assert refreshed.conversation_id == "new-conversation"
    with pytest.raises(TeamsRefusal, match="verified"):
        installation.refreshed_from(changed, connector_verified=False)

    foreign = teams_module.replace(changed, tenant_id="foreign-tenant")
    with pytest.raises(TeamsRefusal) as raised:
        installation.refreshed_from(foreign, connector_verified=True)
    assert raised.value.reason == "wrong-tenant"


def manifest(**changes: Any) -> TeamsManifest:
    values = {
        "package_id": "44444444-4444-4444-8444-444444444444",
        "app_id": APP_ID,
        "version": "1.2.3",
        "name": "Wreath Operations",
        "short_description": "Operate safely from Teams",
        "long_description": "Run authorized Wreath operations from Microsoft Teams.",
        "developer_name": "Example Pty Ltd",
        "website_url": "https://chat.example.test",
        "privacy_url": "https://chat.example.test/privacy",
        "terms_url": "https://chat.example.test/terms",
        "scopes": ("personal", "team", "groupChat"),
        "commands": (
            {"title": "deploy", "description": "Deploy a release"},
            {"title": "status", "description": "Show deployment status"},
        ),
        "entra_resource": f"api://chat.example.test/{APP_ID}",
    }
    values.update(changes)
    return TeamsManifest(**values)


def test_shared_manifest_derives_declared_commands_deterministically() -> None:
    chat = ChatOps(name="operations", providers=(provider(),))

    @chat.command("status", description="Show deployment status")
    async def status(request: Any) -> None:
        return None

    @chat.command("deploy", description="Deploy a release")
    async def deploy(request: Any) -> None:
        return None

    document = chat.manifest("teams", base_url="https://chat.example.test")

    assert document["id"] == APP_ID
    assert document["bots"][0]["commandLists"][0]["commands"] == [
        {"title": "deploy", "description": "Deploy a release"},
        {"title": "status", "description": "Show deployment status"},
    ]
    assert document["webApplicationInfo"] == {
        "id": APP_ID,
        "resource": f"api://chat.example.test/{APP_ID}",
    }


def test_shared_manifest_uses_command_name_fallback_and_omits_unconfigured_sso() -> None:
    teams = provider()
    teams.config = teams_module.replace(teams.config, login_issuers={})
    chat = ChatOps(name="operations", providers=(teams,))

    @chat.command("status")
    async def status(request: Any) -> None:
        return None

    document = chat.manifest("teams", base_url="https://chat.example.test")
    assert document["bots"][0]["commandLists"][0]["commands"] == [
        {"title": "status", "description": "status"}
    ]
    assert "webApplicationInfo" not in document


def test_manifest_is_current_deterministic_and_contains_the_bot_sso_contract() -> None:
    document = manifest().render()

    assert document == manifest().render()
    assert document["$schema"] == (
        "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json"
    )
    assert document["manifestVersion"] == "1.30"
    assert document["version"] == "1.2.3"
    assert document["id"] == "44444444-4444-4444-8444-444444444444"
    assert document["bots"] == [
        {
            "botId": APP_ID,
            "scopes": ["personal", "team", "groupChat"],
            "supportsFiles": False,
            "isNotificationOnly": False,
            "commandLists": [
                {
                    "scopes": ["personal", "team", "groupChat"],
                    "commands": [
                        {"title": "deploy", "description": "Deploy a release"},
                        {"title": "status", "description": "Show deployment status"},
                    ],
                }
            ],
        }
    ]
    assert document["webApplicationInfo"] == {
        "id": APP_ID,
        "resource": f"api://chat.example.test/{APP_ID}",
    }
    assert document["validDomains"] == ["chat.example.test", "token.botframework.com"]
    assert document["permissions"] == ["identity"]


def test_manifest_package_has_only_canonical_root_files() -> None:
    color = b"color-png"
    outline = b"outline-png"

    package = manifest().package(color_icon=color, outline_icon=outline)

    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.namelist() == ["manifest.json", "color.png", "outline.png"]
        assert json.loads(archive.read("manifest.json")) == manifest().render()
        assert archive.read("color.png") == color
        assert archive.read("outline.png") == outline


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"scopes": ()}, "scope"),
        ({"scopes": ("channel",)}, "personal, team, or groupChat"),
        ({"website_url": "http://chat.example.test"}, "HTTPS"),
        ({"website_url": "https://chat.example.test?query=1"}, "HTTPS"),
        ({"website_url": "https://chat.example.test#fragment"}, "HTTPS"),
        ({"website_url": "https://user@chat.example.test"}, "HTTPS"),
        ({"website_url": "https:///missing-host"}, "HTTPS"),
        ({"commands": ({"title": 7, "description": "bad"},)}, "command"),
        ({"commands": ({"title": "", "description": "bad"},)}, "command"),
        ({"commands": ({"title": "ok", "description": 7},)}, "command"),
        ({"commands": ({"title": "ok", "description": ""},)}, "command"),
        ({"commands": ({"title": "x" * 33, "description": "too long"},)}, "command"),
        ({"entra_resource": "api://another.example/not-this-app"}, "app_id"),
    ],
)
def test_invalid_manifest_input_refuses_before_packaging(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        manifest(**changes)


def test_manifest_command_fields_are_independently_typed_and_nonempty() -> None:
    invalid_commands = (
        ({"title": 7, "description": "description"},),
        ({"title": "", "description": "description"},),
        ({"title": "title", "description": 7},),
        ({"title": "title", "description": ""},),
    )
    for commands in invalid_commands:
        with pytest.raises(ValueError, match="command"):
            manifest(commands=commands)
