from __future__ import annotations

from types import SimpleNamespace

from wreath import Wreath
from wreath.auth import Identity
from wreath.chat import (
    ChatOps,
    ExternalIdentityKey,
    IdentityLinkChallenge,
    PrincipalBinding,
)
from wreath.chat.slack import Slack
from wreath.testing import TestClient

from .conftest import NOW, SIGNING_SECRET, form_body, signed_headers
from .test_responses_and_delivery import MemoryInbox, RecordingJobs


class DirectoryResolver:
    def __init__(self, bindings: dict[ExternalIdentityKey, PrincipalBinding] | None = None):
        self.bindings = dict(bindings or {})
        self.lookups: list[ExternalIdentityKey] = []
        self.links: list[tuple[ExternalIdentityKey, str]] = []

    async def resolve(self, key: ExternalIdentityKey) -> PrincipalBinding | None:
        self.lookups.append(key)
        return self.bindings.get(key)

    async def begin_link(
        self, key: ExternalIdentityKey, *, return_to: str
    ) -> IdentityLinkChallenge:
        self.links.append((key, return_to))
        return IdentityLinkChallenge(
            url="https://ops.example/auth/login?provider=entra&state=single-use-state",
            state="single-use-state",
        )


async def invoke(app: Wreath, **overrides: str):
    values = {
        "api_app_id": "A123",
        "team_id": "T123",
        "team_domain": "acme",
        "channel_id": "C123",
        "channel_name": "operations",
        "user_id": "U123",
        "user_name": "mara",
        "user_email": "attacker-controlled@example.net",
        "command": "/whoami",
        "text": "",
        "response_url": "https://hooks.slack.com/commands/T123/1/secret",
        "trigger_id": "123.456",
        **overrides,
    }
    body = form_body(**values)
    headers = signed_headers(body)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return await TestClient(app).post("/chat/slack/commands", content=body, headers=headers)


def configured(resolver: DirectoryResolver) -> tuple[ChatOps, Wreath]:
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        identity=resolver,
    )
    return chat, app


async def test_authoritative_directory_mapping_reuses_the_existing_wreath_principal() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T123", subject="U123")
    identity = Identity(
        id="user-7",
        roles=frozenset({"operator"}),
        permissions=frozenset({"Deploy::run"}),
        claims={"iss": "https://login.microsoftonline.com/tenant/v2.0", "sub": "entra-7"},
    )
    binding = PrincipalBinding(key=key, identity=identity)
    resolver = DirectoryResolver({key: binding})
    chat, app = configured(resolver)
    seen: list[Identity] = []

    @chat.command("whoami")
    async def whoami(principal: Identity) -> None:
        seen.append(principal)

    response = await invoke(app)

    assert (response.status, response.body) == (200, b"")
    assert resolver.lookups == [key]
    assert seen == [binding.principal.bind()]
    assert seen[0].permissions == frozenset({"Deploy::run"})


async def test_same_slack_user_id_in_another_installation_is_not_the_same_identity() -> None:
    acme = ExternalIdentityKey(provider="slack", installation="T123", subject="U123")
    identity = Identity(id="user-7", permissions=frozenset({"Deploy::run"}))
    resolver = DirectoryResolver({acme: PrincipalBinding(key=acme, identity=identity)})
    chat, app = configured(resolver)

    @chat.command("whoami")
    async def whoami(principal: Identity) -> None:
        raise AssertionError("another installation inherited Acme's mapping")

    response = await invoke(app, team_id="T999")

    assert response.status == 200
    assert resolver.lookups == [
        ExternalIdentityKey(provider="slack", installation="T999", subject="U123")
    ]


async def test_email_and_display_name_never_create_an_implicit_federation() -> None:
    resolver = DirectoryResolver()
    chat, app = configured(resolver)
    ran = False

    @chat.command("whoami")
    async def whoami(principal: Identity) -> None:
        nonlocal ran
        ran = True

    response = await invoke(
        app,
        user_name="Known Employee",
        user_email="known.employee@acme.example",
    )

    assert response.status == 200
    assert response.json()["response_type"] == "ephemeral"
    assert response.json()["blocks"][0]["accessory"]["url"].startswith(
        "https://ops.example/auth/login?provider=entra&state="
    )
    assert resolver.lookups == [
        ExternalIdentityKey(provider="slack", installation="T123", subject="U123")
    ]
    assert resolver.links[0][0] == resolver.lookups[0]
    assert "known.employee" not in resolver.links[0][1]
    assert ran is False


async def test_bot_actor_is_not_offered_a_human_sso_link() -> None:
    resolver = DirectoryResolver()
    chat, app = configured(resolver)

    @chat.command("whoami")
    async def whoami(principal: Identity) -> None:
        raise AssertionError("bot should not resolve as a user")

    response = await invoke(app, user_id="USLACKBOT", bot_id="B123")

    assert response.status == 403
    assert resolver.links == []


async def test_durable_command_restores_the_external_identity_before_execution() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T123", subject="U123")
    identity = Identity(id="user-7", permissions=frozenset({"Deploy::run"}))
    resolver = DirectoryResolver(
        {key: PrincipalBinding(key=key, identity=identity, tenant="acme")}
    )
    jobs = RecordingJobs()
    app = Wreath()
    chat = ChatOps(
        app,
        name="operations",
        path="/chat",
        providers=(Slack(signing_secret=SIGNING_SECRET, clock=lambda: NOW),),
        identity=resolver,
        jobs=jobs,
        inbox=MemoryInbox(),
    )
    seen: list[Identity] = []

    @chat.command("whoami", execution="durable")
    async def whoami(principal: Identity) -> None:
        seen.append(principal)

    await invoke(app)
    task, payload, job_key = jobs.enqueued[0]
    assert jobs.enqueue_options[0][0] == "acme"
    await jobs.registered[task](
        SimpleNamespace(
            job_id=7,
            tenant="acme",
            key=job_key,
            fence=1,
            trace_context=None,
        ),
        payload,
    )

    assert resolver.lookups == [key, key]
    assert [principal.id for principal in seen] == [identity.id]
    assert seen[0].permissions == identity.permissions
