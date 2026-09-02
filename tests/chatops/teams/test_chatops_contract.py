from __future__ import annotations

import json
import time
from typing import Any

import pytest

import wreath.chat.teams as teams_module
from wreath import Wreath
from wreath.chat import (
    AgentEvent,
    AgentRequest,
    ChatCorrelation,
    ChatOps,
    ChatReply,
    StaleChatJobFence,
)
from wreath.chat.teams import (
    Teams,
    TeamsActivity,
    TeamsBotConfig,
    TeamsConnectorError,
    TeamsRefusal,
)
from wreath.jobs import JobContext, JobRunner
from wreath.testing import TestClient

from ._support import (
    APP_ID,
    ENTRA_TENANT,
    SERVICE_URL,
    AcceptingVerifier,
    MemoryInbox,
    MemoryInstallations,
    RecordingAudit,
    RecordingConnector,
    RecordingJobs,
    activity,
)


def mounted(
    *,
    jobs: Any = None,
    inbox: Any = None,
    installations: Any = None,
    connector: Any = None,
    audit: Any = None,
    identity: Any = None,
    authorizer: Any = None,
    chat_installations: Any = None,
    clock: Any = time.time,
    allowed_tenants: frozenset[str] = frozenset({ENTRA_TENANT}),
    login_issuers: dict[str, str] | None = None,
    max_token_lifetime: int = 3600,
) -> tuple[Wreath, ChatOps, Teams, AcceptingVerifier]:
    app = Wreath()
    verifier = AcceptingVerifier()
    provider = Teams(
        config=TeamsBotConfig(
            app_id=APP_ID,
            app_secret="secret",
            messaging_endpoint="https://chat.example.test/_wreath/chat/teams",
            allowed_tenants=allowed_tenants,
            login_issuers=(
                {ENTRA_TENANT: f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"}
                if login_issuers is None
                else login_issuers
            ),
            max_token_lifetime=max_token_lifetime,
        ),
        verifier=verifier,
        connector=connector or RecordingConnector(),
        installations=installations,
        token_provider=lambda: "connector-access-token",
        clock=clock,
    )
    chat = ChatOps(
        app,
        name="operations",
        providers=(provider,),
        jobs=jobs,
        inbox=inbox,
        audit=audit,
        identity=identity,
        authorizer=authorizer,
        installations=chat_installations,
        clock=clock,
    )
    return app, chat, provider, verifier


def test_falsey_injected_protocol_adapters_are_not_silently_replaced() -> None:
    class FalseyAdapter:
        def __bool__(self) -> bool:
            return False

    verifier = FalseyAdapter()
    connector = FalseyAdapter()
    teams = Teams(
        config=TeamsBotConfig(
            app_id=APP_ID,
            app_secret="secret",
            messaging_endpoint="https://chat.example.test/_wreath/chat/teams",
            allowed_tenants=frozenset({ENTRA_TENANT}),
        ),
        verifier=verifier,
        connector=connector,
    )
    assert teams.verifier is verifier
    assert teams.connector is connector


async def post(app: Wreath, payload: Any) -> Any:
    async with TestClient(app) as client:
        return await client.post(
            "/_wreath/chat/teams",
            json=payload,
            headers={"authorization": "Bearer connector.jwt"},
        )


async def test_ordinary_activity_is_verified_then_acknowledged_exactly_once() -> None:
    app, chat, _, verifier = mounted(inbox=MemoryInbox())
    seen: list[str] = []

    @chat.command("deploy", description="Deploy a release", execution="inline")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    response = await post(app, activity(text="deploy"))

    assert response.status == 200
    assert response.body == b""
    assert response.headers == [(b"content-length", b"0")]
    assert len(verifier.calls) == 1
    assert seen == ["activity-1"]


async def test_inline_command_reply_is_delivered_on_the_verified_reply_route() -> None:
    connector = RecordingConnector()
    app, chat, _, _ = mounted(connector=connector, inbox=MemoryInbox())

    @chat.command("status")
    async def status(request: Any) -> ChatReply:
        return ChatReply.text("all green")

    response = await post(app, activity(text="status"))
    assert (response.status, response.body) == (200, b"")
    assert connector.requests[0].json["text"] == "all green"
    assert connector.requests[0].url.endswith("/activities/activity-1")


async def test_chat_context_preserves_request_state_from_outer_middleware() -> None:
    app, chat, _, _ = mounted(inbox=MemoryInbox())
    seen: list[str] = []

    async def state_marker(request: Any, call_next: Any) -> Any:
        request.state.chat_marker = "trusted-ingress"
        return await call_next(request)

    app.add_middleware(state_marker)

    @chat.command("status")
    async def status(request: Any) -> None:
        seen.append(request.state.chat_marker)

    assert (await post(app, activity(text="status"))).status == 200
    assert seen == ["trusted-ingress"]


async def test_endpoint_refuses_a_non_object_json_activity_before_verification() -> None:
    app, _, _, verifier = mounted(inbox=MemoryInbox())
    response = await post(app, [])
    assert response.status == 400
    assert verifier.calls == []


async def test_inline_commands_do_not_register_durable_workers() -> None:
    jobs = RecordingJobs()
    _, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("status")
    async def status(request: Any) -> None:
        return None

    assert jobs.handlers == {}


async def test_duplicate_delivery_is_acknowledged_without_second_execution() -> None:
    inbox = MemoryInbox()
    app, chat, _, _ = mounted(inbox=inbox)
    seen: list[str] = []

    @chat.command("deploy", execution="inline")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    first = await post(app, activity())
    duplicate = await post(app, activity())

    assert (first.status, first.body) == (200, b"")
    assert (duplicate.status, duplicate.body) == (200, b"")
    assert seen == ["activity-1"]
    assert inbox.claims == {("teams", f"{ENTRA_TENANT}:19:team@thread.tacv2", "activity-1")}


async def test_provider_replay_window_covers_the_configured_jwt_lifetime() -> None:
    now = [1000.0]
    app, chat, _, _ = mounted(
        clock=lambda: now[0],
        max_token_lifetime=1200,
    )
    calls = 0

    @chat.command("deploy")
    async def deploy(request: Any) -> None:
        nonlocal calls
        calls += 1

    assert (await post(app, activity())).status == 200
    now[0] += 700
    assert (await post(app, activity())).status == 200
    assert calls == 1
    now[0] += 801
    assert (await post(app, activity())).status == 200
    assert calls == 2


async def test_provider_replay_store_is_bounded_to_4096_deliveries() -> None:
    _, chat, teams, _ = mounted()
    first = TeamsActivity.parse(activity(id="delivery-0"))
    for index in range(4097):
        current = teams_module.replace(first, id=f"delivery-{index}")
        assert await teams._claim(chat, current)
    assert await teams._claim(chat, first)


async def test_same_activity_id_in_two_tenants_is_not_a_duplicate() -> None:
    second_tenant = "33333333-3333-4333-8333-333333333333"
    inbox = MemoryInbox()
    app, chat, _, _ = mounted(
        inbox=inbox,
        allowed_tenants=frozenset({ENTRA_TENANT, second_tenant}),
        login_issuers={
            ENTRA_TENANT: f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0",
            second_tenant: f"https://login.microsoftonline.com/{second_tenant}/v2.0",
        },
    )
    seen: list[str] = []

    @chat.command("deploy", execution="inline")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.tenant_id)

    other = activity(
        conversation={
            "id": "19:other-conversation@thread.tacv2",
            "conversationType": "channel",
            "tenantId": second_tenant,
        },
        channelData={
            "tenant": {"id": second_tenant},
            "team": {"id": "19:team@thread.tacv2"},
            "channel": {"id": "19:channel@thread.tacv2"},
        },
    )
    assert (await post(app, activity())).status == 200
    assert (await post(app, other)).status == 200
    assert seen == [ENTRA_TENANT, second_tenant]


async def test_unconfigured_tenant_is_forbidden_before_inbox_or_handler() -> None:
    inbox = MemoryInbox()
    app, chat, _, _ = mounted(inbox=inbox)
    seen: list[str] = []

    @chat.command("deploy", execution="inline")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    payload = activity()
    payload["conversation"]["tenantId"] = "foreign"
    payload["channelData"]["tenant"]["id"] = "foreign"
    response = await post(app, payload)

    assert response.status == 403
    assert seen == []
    assert inbox.claims == set()


async def test_untrusted_service_url_is_forbidden_before_inbox_or_handler() -> None:
    inbox = MemoryInbox()
    app, chat, _, _ = mounted(inbox=inbox)

    @chat.command("deploy", execution="inline")
    async def deploy(request: Any) -> None:
        raise AssertionError("untrusted serviceUrl must not dispatch")

    response = await post(app, activity(serviceUrl="https://connector.example.test/amer/"))

    assert response.status == 403
    assert inbox.claims == set()


async def test_durable_command_acknowledges_before_handler_execution() -> None:
    jobs = RecordingJobs()
    inbox = MemoryInbox()
    app, chat, _, _ = mounted(jobs=jobs, inbox=inbox)
    seen: list[str] = []

    @chat.command(
        "deploy",
        description="Deploy safely",
        execution="durable",
    )
    async def deploy(request: Any) -> ChatReply:
        seen.append(request.activity.id)
        return ChatReply.text("deployed")

    response = await post(app, activity())

    assert (response.status, response.body) == (200, b"")
    assert seen == []
    assert len(jobs.pending) == 1
    name, payload, options = jobs.pending[0]
    assert name.startswith("chat_teams_")
    assert payload["activity"]["id"] == "activity-1"
    assert isinstance(payload["verification"], str)
    assert options["tenant"] == f"teams:{ENTRA_TENANT}"
    assert options["key"] == (
        "teams:11111111-1111-4111-8111-111111111111:19:team@thread.tacv2:activity-1:deploy"
    )
    assert options["tx"] is inbox
    assert inbox.atomic_calls[0]["envelope"].type == "command:deploy"
    assert b'"id":"activity-1"' in inbox.atomic_calls[0]["envelope"].body

    await jobs.run_next()
    assert seen == ["activity-1"]


async def test_durable_command_refuses_a_tampered_verified_envelope() -> None:
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        raise AssertionError("tampered durable work must not dispatch")

    assert (await post(app, activity())).status == 200
    jobs.pending[0][1]["activity"]["from"]["aadObjectId"] = "forged-subject"

    with pytest.raises(TeamsRefusal, match="verification is invalid"):
        await jobs.run_next()


@pytest.mark.parametrize("verification", [None, "not-a-valid-mac"])
async def test_durable_command_refuses_missing_or_invalid_envelope_mac(
    verification: Any,
) -> None:
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        raise AssertionError("unverified durable work must not dispatch")

    assert (await post(app, activity())).status == 200
    jobs.pending[0][1]["verification"] = verification
    with pytest.raises(TeamsRefusal) as raised:
        await jobs.run_next()
    assert raised.value.reason == "invalid-durable-envelope"


async def test_durable_command_refuses_a_mismatched_job_fence_context() -> None:
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        raise AssertionError("misbound durable work must not dispatch")

    assert (await post(app, activity())).status == 200
    jobs.pending[0][2]["fence"] = 0

    with pytest.raises(StaleChatJobFence):
        await jobs.run_next()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "foreign-tenant"),
        ("key", "another-delivery"),
        ("fence", None),
        ("fence", True),
    ],
)
async def test_durable_command_refuses_every_misbound_job_fact(field: str, value: Any) -> None:
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        raise AssertionError("misbound durable work must not dispatch")

    assert (await post(app, activity())).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]
    facts: dict[str, Any] = {
        "tenant": f"teams:{ENTRA_TENANT}",
        "key": options["key"],
        "fence": 1,
    }
    facts[field] = value
    job = JobContext(
        job_id=7,
        task=task_name,
        attempt=1,
        fence=facts["fence"],
        tenant=facts["tenant"],
        key=facts["key"],
    )
    with pytest.raises(StaleChatJobFence):
        await registered(job, envelope)


async def test_registered_durable_handler_activates_agent_context_and_emitter() -> None:
    class Progress:
        def __init__(self) -> None:
            self.reports: list[tuple[str, float, str]] = []

        def report(self, task_id: str, percent: float, message: str) -> None:
            self.reports.append((task_id, percent, message))

    jobs = RecordingJobs()
    connector = RecordingConnector()
    app, chat, _, _ = mounted(connector=connector, jobs=jobs, inbox=MemoryInbox())
    seen: list[Any] = []

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        seen.append(context)
        await context.emit(AgentEvent.progress("reading", percent=10))
        await context.emit(AgentEvent.text(f"done: {prompt}"))
        await context.emit(AgentEvent.completed())

    inbound = activity(text=None, value={"verb": "agent", "prompt": "ship production"})
    assert (await post(app, inbound)).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]
    progress = Progress()
    job = JobContext(
        job_id=7,
        task=task_name,
        attempt=2,
        fence=11,
        tenant=f"teams:{ENTRA_TENANT}",
        key=options["key"],
        progress=progress,
        trace_context="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )

    await registered(job, envelope)

    context = seen[0]
    assert context.job_context is job
    assert context.provider == "teams"
    assert context.installation == "19:team@thread.tacv2"
    assert context.actor == "29:opaque-teams-member-id"
    assert context.conversation == "19:conversation@thread.tacv2"
    assert context.delivery_id == "activity-1"
    assert context.agent_request == AgentRequest(
        tenant=f"teams:{ENTRA_TENANT}",
        actor="29:opaque-teams-member-id",
        conversation="19:conversation@thread.tacv2",
        prompt="ship production",
        correlation=ChatCorrelation(
            interaction_id="activity-1",
            job_id="7",
            trace_id=job.trace_context,
        ),
        native=inbound,
    )
    assert progress.reports == [("7", 10.0, "reading")]
    assert len(connector.requests) == 1
    assert connector.requests[0].json["text"] == "done: ship production"
    assert connector.requests[0].headers["x-ms-client-request-id"] == options["key"]


async def test_plain_text_durable_prompt_is_normalized_for_agent_handlers() -> None:
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())
    seen: list[str] = []

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        del context
        seen.append(prompt)

    inbound = activity(text="agent ship production", value={})
    assert (await post(app, inbound)).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]
    await registered(
        JobContext(
            job_id=7,
            task=task_name,
            attempt=1,
            fence=1,
            tenant=f"teams:{ENTRA_TENANT}",
            key=options["key"],
        ),
        envelope,
    )

    assert seen == ["ship production"]


async def test_durable_emitter_reports_defaults_and_refuses_invalid_event_order() -> None:
    class Progress:
        def __init__(self) -> None:
            self.reports: list[tuple[str, float, str]] = []

        def report(self, task_id: str, percent: float, message: str) -> None:
            self.reports.append((task_id, percent, message))

    jobs = RecordingJobs()
    connector = RecordingConnector()
    app, chat, _, _ = mounted(connector=connector, jobs=jobs, inbox=MemoryInbox())
    errors: list[str] = []

    @chat.command("agent", execution="durable")
    async def agent(context: Any) -> None:
        await context.emit(AgentEvent("progress"))
        await context.emit(AgentEvent.text(""))
        try:
            unknown_kind: Any = "unknown"
            await context.emit(AgentEvent(unknown_kind))
        except ValueError as error:
            errors.append(str(error))
        await context.emit(AgentEvent.completed())
        try:
            await context.emit(AgentEvent.text("late"))
        except RuntimeError as error:
            errors.append(str(error))

    assert (await post(app, activity(text="agent"))).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]
    progress = Progress()
    job = JobContext(
        job_id=7,
        task=task_name,
        attempt=1,
        fence=1,
        tenant=f"teams:{ENTRA_TENANT}",
        key=options["key"],
        progress=progress,
    )
    await registered(job, envelope)

    assert progress.reports == [("7", 0.0, "")]
    assert errors == [
        "unsupported AgentEvent kind 'unknown'",
        "cannot emit an AgentEvent after completed",
    ]
    assert connector.requests == []


async def test_durable_text_retry_reuses_one_verified_reply_idempotency_key() -> None:
    jobs = RecordingJobs()
    connector = RecordingConnector()
    app, chat, _, _ = mounted(connector=connector, jobs=jobs, inbox=MemoryInbox())
    attempts = 0

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str) -> None:
        nonlocal attempts
        attempts += 1
        await context.emit(AgentEvent.text(f"done: {prompt}"))
        await context.emit(AgentEvent.completed())
        if attempts == 1:
            raise RuntimeError("retry after delivery")

    inbound = activity(text=None, value={"verb": "agent", "prompt": "ship production"})
    assert (await post(app, inbound)).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]

    def job(fence: int) -> JobContext:
        return JobContext(
            job_id=7,
            task=task_name,
            attempt=fence,
            fence=fence,
            tenant=f"teams:{ENTRA_TENANT}",
            key=options["key"],
        )

    with pytest.raises(RuntimeError, match="retry after delivery"):
        await registered(job(11), envelope)
    await registered(job(12), envelope)

    assert len(connector.requests) == 2
    assert connector.requests[0].url == connector.requests[1].url
    assert connector.requests[0].json == connector.requests[1].json
    assert (
        connector.requests[0].headers["x-ms-client-request-id"]
        == (connector.requests[1].headers["x-ms-client-request-id"])
    )


async def test_registered_worker_refuses_text_beyond_the_durable_teams_bound() -> None:
    jobs = RecordingJobs()
    connector = RecordingConnector()
    app, chat, _, _ = mounted(connector=connector, jobs=jobs, inbox=MemoryInbox())
    refusals: list[str] = []

    @chat.command("agent", execution="durable")
    async def agent(context: Any) -> None:
        await context.emit(AgentEvent.text("x" * 28_000))
        try:
            await context.emit(AgentEvent.text("y"))
        except ValueError as error:
            refusals.append(str(error))
        await context.emit(AgentEvent.completed())

    assert (await post(app, activity(text="agent"))).status == 200
    task_name, envelope, options = jobs.pending[0]
    registered, _ = jobs.handlers[task_name]
    job = JobContext(
        job_id=7,
        task=task_name,
        attempt=1,
        fence=11,
        tenant=f"teams:{ENTRA_TENANT}",
        key=options["key"],
    )

    await registered(job, envelope)

    assert refusals == ["durable Teams text exceeds Wreath's 28,000-character durable Teams bound"]
    assert connector.requests[0].json["text"] == "x" * 28_000


async def test_durable_commands_refuse_startup_without_jobs_and_an_inbox() -> None:
    app, chat, _, _ = mounted()

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        return None

    with pytest.raises(RuntimeError, match="durable.*jobs.*inbox"):
        async with TestClient(app):
            pass


def test_durable_registration_has_bounded_retry_and_dead_letter_policy() -> None:
    jobs = RecordingJobs()
    _, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        return None

    _, options = next(iter(jobs.handlers.values()))
    assert options["retries"] == 4
    assert options["backoff"] == "exp"


def test_durable_registration_is_duplicate_safe() -> None:
    jobs = RecordingJobs()
    _, chat, teams, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        return None

    registered = dict(jobs.handlers)
    teams._register_command(chat, chat.commands["deploy"])
    assert jobs.handlers == registered


def test_durable_command_registers_with_the_real_job_runner_owner() -> None:
    jobs = JobRunner(object(), name="teams_test_jobs")
    _, chat, _, _ = mounted(jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> None:
        return None

    assert len(jobs._tasks) == 1
    task = next(iter(jobs._tasks.values()))
    assert tuple(task.signature.parameters) == ("job", "payload")
    assert task.max_attempts == 5


async def test_install_and_uninstall_are_tenant_scoped_and_idempotent() -> None:
    installations = MemoryInstallations()
    app, _, _, _ = mounted(installations=installations, inbox=MemoryInbox())
    installed = activity(
        type="installationUpdate",
        action="add",
        text=None,
        id="install-1",
    )
    removed = activity(
        type="installationUpdate",
        action="remove",
        text=None,
        id="install-2",
    )

    assert (await post(app, installed)).status == 200
    key = (ENTRA_TENANT, "19:team@thread.tacv2")
    assert installations.rows[key].service_url.endswith("/amer/")
    assert installations.rows[key].conversation_id == "19:conversation@thread.tacv2"

    assert (await post(app, removed)).status == 200
    assert (await post(app, removed)).status == 200
    assert key not in installations.rows
    assert installations.puts == 1
    assert installations.deletes == 1


async def test_installation_events_without_a_store_and_unknown_actions_are_noops() -> None:
    app, _, _, _ = mounted(inbox=MemoryInbox())
    response = await post(
        app,
        activity(type="installationUpdate", action="add", text=None, id="no-store"),
    )
    assert response.status == 200

    installations = MemoryInstallations()
    app, _, _, _ = mounted(installations=installations, inbox=MemoryInbox())
    response = await post(
        app,
        activity(type="installationUpdate", action="upgrade", text=None, id="unknown-action"),
    )
    assert response.status == 200
    assert installations.rows == {}


async def test_configured_installation_store_fails_closed_before_ordinary_dispatch() -> None:
    installations = MemoryInstallations()
    app, chat, _, verifier = mounted(
        installations=installations,
        inbox=MemoryInbox(),
    )
    seen: list[str] = []

    @chat.command("deploy")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    response = await post(app, activity())
    assert response.status == 403
    assert seen == []
    assert len(verifier.calls) == 1


async def test_configured_installation_store_accepts_and_refreshes_installed_ingress() -> None:
    key = (ENTRA_TENANT, "19:team@thread.tacv2")
    installations = MemoryInstallations(
        {
            key: teams_module.TeamsInstallation(
                tenant_id=ENTRA_TENANT,
                installation_id=key[1],
                service_url=SERVICE_URL,
                conversation_id="old-conversation",
                bot_id=APP_ID,
            )
        }
    )
    app, chat, _, _ = mounted(installations=installations, inbox=MemoryInbox())
    seen: list[str] = []

    @chat.command("deploy")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    response = await post(app, activity())
    assert response.status == 200
    assert seen == ["activity-1"]
    assert installations.rows[key].conversation_id == "19:conversation@thread.tacv2"


async def test_shared_chat_installation_store_is_the_provider_fallback() -> None:
    installations = MemoryInstallations()
    app, _, provider, _ = mounted(
        chat_installations=installations,
        inbox=MemoryInbox(),
    )
    installed = activity(type="installationUpdate", action="add", text=None, id="install-shared")

    assert (await post(app, installed)).status == 200
    assert provider.installations is installations
    assert (ENTRA_TENANT, "19:team@thread.tacv2") in installations.rows


async def test_non_federated_chat_does_not_require_a_login_issuer() -> None:
    app, chat, _, _ = mounted(login_issuers={}, inbox=MemoryInbox())
    seen: list[str] = []

    @chat.command("deploy")
    async def deploy(request: Any) -> None:
        seen.append(request.activity.id)

    response = await post(app, activity())

    assert response.status == 200
    assert seen == ["activity-1"]


async def test_member_add_event_installs_only_when_the_added_member_is_this_bot() -> None:
    installations = MemoryInstallations()
    app, _, _, _ = mounted(installations=installations, inbox=MemoryInbox())
    user_added = activity(
        type="conversationUpdate",
        text=None,
        id="member-1",
        membersAdded=[{"id": "29:a-new-human"}],
    )
    bot_added = activity(
        type="conversationUpdate",
        text=None,
        id="member-2",
        membersAdded=[{"id": APP_ID}],
    )

    assert (await post(app, user_added)).status == 200
    assert installations.rows == {}
    assert (await post(app, bot_added)).status == 200
    assert (ENTRA_TENANT, "19:team@thread.tacv2") in installations.rows


async def test_adaptive_card_execute_has_the_exact_invoke_response_shape() -> None:
    app, chat, _, _ = mounted(inbox=MemoryInbox())

    @chat.command("approve", execution="inline")
    async def approve(request: Any) -> ChatReply:
        assert request.action == "approve"
        assert request.inputs == {"reason": "looks good"}
        return ChatReply.card(
            {
                "type": "AdaptiveCard",
                "version": "1.5",
                "body": [{"type": "TextBlock", "text": "Approved"}],
            }
        )

    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={
            "action": {
                "type": "Action.Execute",
                "verb": "approve",
                "data": {"reason": "looks good"},
            }
        },
    )
    response = await post(app, invoke)

    assert response.status == 200
    assert response.json() == {
        "statusCode": 200,
        "type": "application/vnd.microsoft.card.adaptive",
        "value": {
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [{"type": "TextBlock", "text": "Approved"}],
        },
    }


async def test_adaptive_card_execute_activates_a_declared_chat_action() -> None:
    app, chat, _, _ = mounted(inbox=MemoryInbox())
    seen: list[str] = []

    @chat.action("approve")
    async def approve(request: Any) -> ChatReply:
        seen.append(request.action)
        return ChatReply.text("approved")

    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={"action": {"type": "Action.Execute", "verb": "approve", "data": {}}},
    )
    response = await post(app, invoke)
    duplicate = await post(app, invoke)
    assert response.json()["value"] == {"text": "approved"}
    assert duplicate.body == b""
    assert seen == ["approve"]


async def test_adaptive_card_execute_activates_a_prefix_chat_action() -> None:
    app, chat, _, _ = mounted(inbox=MemoryInbox())
    seen: list[str] = []

    @chat.action("approval:approve:", prefix=True)
    async def approve(request: Any) -> ChatReply:
        seen.append(request.action)
        return ChatReply.text("approved")

    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={
            "action": {
                "type": "Action.Execute",
                "verb": "approval:approve:nonce-1",
                "data": {},
            }
        },
    )

    response = await post(app, invoke)

    assert response.json()["value"] == {"text": "approved"}
    assert seen == ["approval:approve:nonce-1"]


@pytest.mark.parametrize("verb", [None, "", 7])
async def test_adaptive_card_execute_requires_a_nonempty_text_verb(verb: Any) -> None:
    app, _, _, _ = mounted(inbox=MemoryInbox())
    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={"action": {"type": "Action.Execute", "verb": verb, "data": {}}},
    )
    response = await post(app, invoke)
    assert response.status == 200
    assert response.json() == {
        "statusCode": 400,
        "type": "application/vnd.microsoft.error",
        "value": {"code": "BadRequest", "message": "Adaptive Card action has no verb"},
    }


async def test_empty_message_is_acknowledged_without_dispatch() -> None:
    app, _, _, _ = mounted(inbox=MemoryInbox())
    response = await post(app, activity(text="   "))
    assert (response.status, response.body) == (200, b"")


async def test_card_execute_refusal_is_returned_as_a_visible_card_error() -> None:
    class Authorizer:
        async def authorize(self, request: Any, requirement: Any) -> Any:
            raise AssertionError("second factor must run before authorization")

    app, chat, _, _ = mounted(inbox=MemoryInbox(), authorizer=Authorizer())

    @chat.command("approve", action="Release::approve", second_factor=300)
    async def approve(request: Any) -> None:
        raise AssertionError("authorization must run before handler")

    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={"action": {"type": "Action.Execute", "verb": "approve", "data": {}}},
    )
    response = await post(app, invoke)

    assert response.status == 200
    assert response.json() == {
        "statusCode": 401,
        "type": "application/vnd.microsoft.error",
        "value": {"code": "StepUpRequired", "message": "Confirm your identity to continue."},
    }


async def test_policy_denial_is_forbidden_not_misreported_as_step_up() -> None:
    class DenyingAuthorizer:
        async def authorize(self, request: Any, requirement: Any) -> Any:
            from wreath.authorization import AuthorizationDecision

            return AuthorizationDecision(False, "release is frozen")

    audit = RecordingAudit()
    app, chat, _, _ = mounted(
        inbox=MemoryInbox(),
        authorizer=DenyingAuthorizer(),
        audit=audit,
    )

    @chat.command("approve", action="Release::approve")
    async def approve(request: Any) -> None:
        raise AssertionError("denied command must not execute")

    invoke = activity(
        type="invoke",
        name="adaptiveCard/action",
        text=None,
        value={"action": {"type": "Action.Execute", "verb": "approve", "data": {}}},
    )
    response = await post(app, invoke)

    assert response.status == 200
    assert response.json()["statusCode"] == 403
    assert response.json()["value"]["code"] == "Forbidden"
    assert response.json()["value"]["message"] == "This action is not permitted."
    assert audit.records[-1].outcome == "failed"


async def test_ordinary_policy_denial_is_a_forbidden_acknowledgement() -> None:
    class DenyingAuthorizer:
        async def authorize(self, request: Any, requirement: Any) -> Any:
            from wreath.authorization import AuthorizationDecision

            return AuthorizationDecision(False, "release is frozen")

    app, chat, _, _ = mounted(inbox=MemoryInbox(), authorizer=DenyingAuthorizer())

    @chat.command("deploy", action="Release::deploy")
    async def deploy(request: Any) -> None:
        raise AssertionError("denied command must not execute")

    response = await post(app, activity())

    assert response.status == 403
    assert response.body == b""


async def test_action_submit_fallback_dispatches_the_same_declared_verb() -> None:
    app, chat, _, _ = mounted(inbox=MemoryInbox())
    seen: list[dict[str, Any]] = []

    @chat.command("approve", execution="inline")
    async def approve(request: Any) -> None:
        seen.append(request.inputs)

    submitted = activity(text=None, value={"verb": "approve", "reason": "legacy client"})
    response = await post(app, submitted)

    assert (response.status, response.body) == (200, b"")
    assert seen == [{"reason": "legacy client"}]


async def test_unknown_invoke_name_is_explicitly_not_implemented() -> None:
    app, _, _, _ = mounted(inbox=MemoryInbox())
    response = await post(
        app,
        activity(type="invoke", name="future/action", text=None, value={"future": True}),
    )

    assert response.status == 200
    assert response.json() == {
        "statusCode": 501,
        "type": "application/vnd.microsoft.error",
        "value": {"code": "NotSupported", "message": "Unsupported Teams invoke: future/action"},
    }


def test_action_execute_cards_include_submit_fallback_for_older_teams_clients() -> None:
    card = ChatReply.card(
        {
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [],
            "actions": [{"type": "Action.Execute", "verb": "approve", "title": "Approve"}],
        }
    ).for_provider("teams")

    assert card["actions"] == [
        {
            "type": "Action.Execute",
            "verb": "approve",
            "title": "Approve",
            "fallback": {
                "type": "Action.Submit",
                "data": {"verb": "approve"},
                "title": "Approve",
            },
        }
    ]


def test_adaptive_cards_refuse_unsupported_schema_at_declaration_time() -> None:
    with pytest.raises(ValueError, match="Adaptive Card version 1.5"):
        ChatReply.card({"type": "AdaptiveCard", "version": "1.4", "body": []})


async def test_handler_failure_is_audited_without_leaking_details_to_teams() -> None:
    audit = RecordingAudit()
    app, chat, _, _ = mounted(inbox=MemoryInbox(), audit=audit)

    @chat.command("deploy", execution="inline")
    async def deploy(request: Any) -> None:
        raise RuntimeError("database password is hunter2")

    response = await post(app, activity())

    assert response.status == 200
    assert response.body == b""
    assert audit.records[-1].outcome == "failed"
    assert audit.records[-1].actor.channel == "teams"
    assert "hunter2" not in json.dumps(audit.records[-1].public_fields())


async def test_connector_throttle_is_retryable_with_server_retry_after() -> None:
    connector = RecordingConnector(
        responses=[TeamsConnectorError(status=429, retry_after=17), {"id": "sent-2"}]
    )
    jobs = RecordingJobs()
    app, chat, _, _ = mounted(connector=connector, jobs=jobs, inbox=MemoryInbox())

    @chat.command("deploy", execution="durable")
    async def deploy(request: Any) -> ChatReply:
        return ChatReply.text("done")

    assert (await post(app, activity())).status == 200
    with pytest.raises(TeamsConnectorError) as raised:
        await jobs.run_next()

    assert raised.value.status == 429
    assert raised.value.retry_after == 17
    assert len(connector.requests) == 1
    assert connector.requests[0].headers["x-ms-client-request-id"] == (
        "teams:11111111-1111-4111-8111-111111111111:19:team@thread.tacv2:activity-1:deploy"
    )
    assert jobs.pending == []
