from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath.auth import Identity
from wreath.jobs import JobContext

from .support import chatops, command_payload, discord


@dataclass(frozen=True)
class RawResponse:
    status: int = 200
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b"{}"


@dataclass
class RawClient:
    responses: list[RawResponse] = field(default_factory=list)
    requests: list[tuple[str, str, tuple[tuple[bytes, bytes], ...], bytes, str | None]] = field(
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
    ) -> RawResponse:
        self.requests.append((method, target, headers, body, idempotency_key))
        if self.responses:
            return self.responses.pop(0)
        return RawResponse()


@dataclass
class AtomicInbox:
    async def claim_and_enqueue(self, *, enqueue: Any, **_options: Any) -> bool:
        await enqueue(transaction="transaction")
        return True


@dataclass
class SignatureFaithfulJobs:
    handlers: dict[str, Any] = field(default_factory=dict)
    enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    def task(self, name: str, **_options: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[name] = handler
            return handler

        return register

    async def enqueue(
        self,
        task: str,
        *args: Any,
        tx: Any = None,
        key: str | None = None,
        tenant: str = "",
    ) -> int:
        self.enqueued.append((task, args, {"tx": tx, "key": key, "tenant": tenant}))
        return 7


@dataclass
class Progress:
    reports: list[tuple[str, float, str]] = field(default_factory=list)

    def report(self, task_id: str, percent: float, message: str) -> None:
        self.reports.append((task_id, percent, message))


def provider(**options: Any) -> Any:
    return discord().Discord(
        application_id="application-1",
        public_key=bytes(32),
        bot_token="token",
        **options,
    )


@pytest.mark.asyncio
async def test_http_codec_matches_wreath_client_and_decodes_response_body() -> None:
    api = discord()
    command = api.DiscordCommand(name="agent", description="Run agent")
    client = RawClient([RawResponse(body=json.dumps([command.as_discord()]).encode())])

    result = await api.DiscordManifest("application-1", (command,)).sync(client, scope="global")

    assert result.changed is False
    assert client.requests == [("GET", "/applications/application-1/commands", (), b"", None)]


@pytest.mark.asyncio
async def test_responder_and_proactive_send_use_raw_json_and_idempotency_key() -> None:
    api = discord()
    client = RawClient()
    responder = api.DiscordResponder(
        application_id="application-1",
        interaction_id="interaction-1",
        token="interaction-token",
        received_at=1_000,
        acknowledged=True,
        client=client,
        clock=lambda: 1_001,
    )
    await responder.followup(content="done")
    await provider(client=client).send(
        tenant="discord:guild:guild-1",
        destination=api.DiscordDestination(channel_id="channel-1", tenant="discord:guild:guild-1"),
        content="scheduled",
        idempotency_key="delivery-1",
    )

    followup = client.requests[0]
    proactive = client.requests[1]
    assert dict(followup[2])[b"content-type"] == b"application/json"
    assert json.loads(followup[3]) == {"content": "done"}
    assert dict(proactive[2])[b"authorization"] == b"Bot token"
    assert json.loads(proactive[3]) == {"content": "scheduled"}
    assert proactive[4] == "delivery-1"


@pytest.mark.asyncio
async def test_durable_task_is_registered_and_enqueue_uses_job_runner_signature() -> None:
    api = chatops()
    jobs = SignatureFaithfulJobs()
    client = RawClient()
    external = api.ExternalIdentityKey(
        provider="discord", installation="guild:guild-1", subject="user-1"
    )
    binding = api.PrincipalBinding(
        external=external,
        identity=Identity("canonical-user"),
        tenant="acme",
    )

    class Resolver:
        async def resolve(self, key: Any) -> Any:
            assert key == external
            return binding

    chat = api.ChatOps(
        name="test",
        providers=(provider(client=client),),
        inbox=AtomicInbox(),
        jobs=jobs,
        identity=Resolver(),
    )
    handled: list[Any] = []

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str, private: bool) -> None:
        assert private is True
        handled.append(context)
        await context.emit(api.AgentEvent.progress("reading", percent=10))
        await context.emit(api.AgentEvent.text(f"done: {prompt}"))
        await context.emit(api.AgentEvent.completed())

    interaction = discord().DiscordInteraction.parse(command_payload())
    accepted = await chat.accept(interaction)

    assert accepted.enqueued is True
    assert set(jobs.handlers) == {"chat_test_agent_discord"}
    task, args, options = jobs.enqueued[0]
    assert task == "chat_test_agent_discord"
    assert args[0]["interaction"] == interaction.native
    assert isinstance(args[0]["received_at"], float)
    assert options == {
        "tx": "transaction",
        "key": "discord:interaction:interaction-1",
        "tenant": "acme",
    }
    progress = Progress()
    job_context = JobContext(
        job_id=7,
        task=task,
        attempt=2,
        fence=11,
        tenant="acme",
        key=options["key"],
        progress=progress,
        trace_context="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    await jobs.handlers[task](job_context, args[0])

    context = handled[0]
    assert context.job_context is job_context
    assert context.agent_request == api.AgentRequest(
        tenant="acme",
        actor="user-1",
        conversation="discord:actor:user-1",
        prompt="ship it",
        correlation=api.ChatCorrelation(
            interaction_id="interaction-1",
            job_id="7",
            trace_id=job_context.trace_context,
        ),
        native=interaction.native,
        principal=binding.principal,
    )
    assert progress.reports == [("7", 10.0, "reading")]
    assert len(client.requests) == 1
    assert client.requests[0][0:2] == (
        "PATCH",
        "/webhooks/application-1/interaction-token/messages/@original",
    )
    assert json.loads(client.requests[0][3]) == {"content": "done: ship it"}


@pytest.mark.asyncio
async def test_durable_retry_repeats_only_the_idempotent_original_message_edit() -> None:
    api = chatops()
    jobs = SignatureFaithfulJobs()
    client = RawClient()
    chat = api.ChatOps(
        name="retry", providers=(provider(client=client),), inbox=AtomicInbox(), jobs=jobs
    )
    attempts = 0

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str, private: bool) -> None:
        assert private is True
        nonlocal attempts
        attempts += 1
        await context.emit(api.AgentEvent.text(f"done: {prompt}"))
        await context.emit(api.AgentEvent.completed())
        if attempts == 1:
            raise RuntimeError("retry after delivery")

    interaction = discord().DiscordInteraction.parse(command_payload())
    await chat.accept(interaction)
    task, args, options = jobs.enqueued[0]

    def job_context(fence: int) -> JobContext:
        return JobContext(
            job_id=7,
            task=task,
            attempt=fence,
            fence=fence,
            tenant=interaction.tenant,
            key=options["key"],
        )

    with pytest.raises(RuntimeError, match="retry after delivery"):
        await jobs.handlers[task](job_context(11), args[0])
    await jobs.handlers[task](job_context(12), args[0])

    assert len(client.requests) == 2
    assert client.requests[0][0:4] == client.requests[1][0:4]
    assert client.requests[0][1].endswith("/messages/@original")


@pytest.mark.asyncio
async def test_durable_emitter_refuses_over_limit_chunk_before_retaining_it() -> None:
    api = chatops()
    jobs = SignatureFaithfulJobs()
    client = RawClient()
    chat = api.ChatOps(
        name="bounded", providers=(provider(client=client),), inbox=AtomicInbox(), jobs=jobs
    )

    @chat.command("agent", execution="durable")
    async def agent(context: Any, prompt: str, private: bool) -> None:
        assert private is True
        await context.emit(api.AgentEvent.text("a" * 2_000))
        with pytest.raises(ValueError, match="2,000"):
            await context.emit(api.AgentEvent.text("b"))
        await context.emit(api.AgentEvent.completed())

    interaction = discord().DiscordInteraction.parse(command_payload())
    await chat.accept(interaction)
    task, args, options = jobs.enqueued[0]
    job_context = JobContext(
        job_id=8,
        task=task,
        attempt=1,
        fence=1,
        tenant=interaction.tenant,
        key=options["key"],
    )

    await jobs.handlers[task](job_context, args[0])

    assert len(client.requests) == 1
    assert json.loads(client.requests[0][3]) == {"content": "a" * 2_000}


@pytest.mark.asyncio
async def test_timestamp_freshness_and_proactive_tenant_are_enforced_locally() -> None:
    api = discord()
    verifier = api.DiscordInteractionVerifier(
        bytes(32), verify=lambda *_args: True, clock=lambda: 1_000, max_age=300
    )

    with pytest.raises(api.InvalidDiscordSignature, match="timestamp"):
        verifier.verify(signature=bytes(64).hex(), timestamp="699", body=b"{}")

    destination = api.DiscordDestination(channel_id="channel-1", tenant="discord:guild:guild-2")
    with pytest.raises(chatops().ChatTenantMismatch):
        await provider(deliver=lambda _message: None).send(
            tenant="discord:guild:guild-1",
            destination=destination,
            content="wrong tenant",
            idempotency_key="delivery-1",
        )
