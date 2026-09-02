from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from .support import chatops, command_payload, component_payload, discord


@dataclass
class DurableInbox:
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    accepted: bool = True

    async def claim_and_enqueue(
        self,
        *,
        source: str,
        envelope: Any,
        enqueue: Any,
        result_status: int,
    ) -> bool:
        self.calls.append((source, envelope.id, envelope.type, result_status, envelope.body))
        if not self.accepted:
            return False
        await enqueue(transaction=self)
        return True


@dataclass
class Jobs:
    handlers: dict[str, Any] = field(default_factory=dict)
    enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    cancelled: list[tuple[str, str]] = field(default_factory=list)

    def task(self, name: str, **_options: Any) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[name] = handler
            return handler

        return register

    async def enqueue(self, task: str, *args: Any, **options: Any) -> str:
        self.enqueued.append((task, args, options))
        return "job-1"

    async def cancel(self, *, key: str, reason: str) -> bool:
        self.cancelled.append((key, reason))
        return len(self.cancelled) == 1


@dataclass
class Effects:
    completed: set[tuple[str, str]] = field(default_factory=set)
    fences: dict[str, int] = field(default_factory=dict)

    async def publish(self, job_id: str, event: Any, *, fence: int) -> bool:
        if fence < self.fences.get(job_id, fence):
            raise chatops().StaleChatJobFence(job_id)
        self.fences[job_id] = fence
        key = (job_id, event.id)
        if key in self.completed:
            return False
        self.completed.add(key)
        return True


class Agent:
    async def run(self, request: Any) -> AsyncIterator[Any]:
        api = chatops()
        yield api.AgentEvent.progress("reading", percent=10)
        yield api.AgentEvent.text(f"answer: {request.prompt}")
        yield api.AgentEvent.completed()


class UnboundedConversationStore:
    async def append(self, conversation: str, message: Any) -> None:
        return None


class BoundedConversationStore(UnboundedConversationStore):
    retention_days = 30

    async def erase(self, conversation: str) -> None:
        return None


def provider(**options: Any) -> Any:
    return discord().Discord(
        application_id="application-1",
        public_key=bytes(32),
        bot_token="token",
        **options,
    )


def test_agent_backend_protocol_has_no_model_vendor_types() -> None:
    api = chatops()
    backend = Agent()

    assert isinstance(backend, api.AgentBackend)
    assert "openai" not in api.AgentBackend.__module__.lower()
    assert "anthropic" not in api.AgentBackend.__module__.lower()
    request = api.AgentRequest(
        tenant="discord:guild:guild-1",
        actor="user-1",
        conversation="conversation-1",
        prompt="ship it",
        correlation=api.ChatCorrelation(interaction_id="interaction-1", trace_id="trace-1"),
        native=command_payload(),
    )
    assert request.prompt == "ship it"


def test_agent_backend_is_registered_through_shared_chat_command() -> None:
    api = chatops()
    chat = api.ChatOps(name="test", providers=(provider(),))
    backend = Agent()

    @chat.command("agent", execution="durable", action="agent:run", resource="conversation")
    async def ask(context: Any) -> None:
        async for event in backend.run(context.agent_request):
            await context.emit(event)

    declaration = chat.commands["agent"]
    assert declaration.handler is ask
    assert declaration.execution == "durable"
    assert declaration.action == "agent:run"


def test_durable_task_names_distinguish_normalized_command_names() -> None:
    api = chatops()
    jobs = Jobs()
    chat = api.ChatOps(
        name="test",
        providers=(provider(),),
        inbox=DurableInbox(),
        jobs=jobs,
    )

    @chat.command("foo-bar", execution="durable")
    async def dashed() -> None:
        pass

    @chat.command("foo_bar", execution="durable")
    async def underscored() -> None:
        pass

    assert len(jobs.handlers) == 2


@pytest.mark.asyncio
async def test_inbox_claim_and_job_enqueue_are_one_atomic_operation() -> None:
    api = chatops()
    inbox = DurableInbox()
    jobs = Jobs()
    chat = api.ChatOps(name="test", providers=(provider(),), inbox=inbox, jobs=jobs)

    @chat.command("agent", execution="durable")
    async def agent() -> None:
        pass

    interaction = discord().DiscordInteraction.parse(command_payload())

    first = await chat.accept(interaction)
    inbox.accepted = False
    duplicate = await chat.accept(interaction)

    assert first.enqueued is True
    assert duplicate.enqueued is False
    assert jobs.enqueued[0][2]["tx"] is inbox
    assert jobs.enqueued[0][2]["key"] == "discord:interaction:interaction-1"
    assert len(jobs.enqueued) == 1
    assert [call[:4] for call in inbox.calls] == [
        ("discord:guild:guild-1", "interaction-1", "command:agent", 200),
        ("discord:guild:guild-1", "interaction-1", "command:agent", 200),
    ]
    assert all(isinstance(call[4], bytes) for call in inbox.calls)


@pytest.mark.asyncio
async def test_completed_side_effect_is_not_replayed_and_stale_retry_is_fenced() -> None:
    api = chatops()
    effects = Effects()
    event = api.AgentEvent.text("answer", id="event-1")

    assert await effects.publish("job-1", event, fence=7) is True
    assert await effects.publish("job-1", event, fence=8) is False
    with pytest.raises(api.StaleChatJobFence):
        await effects.publish("job-1", api.AgentEvent.text("stale", id="event-2"), fence=7)


def test_progress_coalesces_instead_of_sending_per_token() -> None:
    api = chatops()
    coalescer = api.ChatProgressCoalescer(interval=1.0)
    emitted = [
        coalescer.offer(api.AgentEvent.progress(f"token {index}", percent=index), now=index / 100)
        for index in range(100)
    ]

    assert [item for item in emitted if item is not None] == []
    delivery = coalescer.flush(now=1.0)
    assert delivery.content == "token 99"
    assert delivery.mode == "edit_original"


@pytest.mark.asyncio
async def test_workflow_approval_action_is_single_use_actor_and_tenant_bound() -> None:
    api = chatops()
    actions = api.InMemoryChatActionStore()
    chat = api.ChatOps(name="test", providers=(provider(),))
    action = await actions.issue(
        workflow="release-7",
        decision="approve",
        tenant="discord:guild:guild-1",
        actor="user-1",
    )
    interaction = discord().DiscordInteraction.parse(component_payload(action.custom_id))

    first = await chat.claim_action(interaction, store=actions)
    duplicate = await chat.claim_action(interaction, store=actions)

    assert first.workflow == "release-7"
    assert first.decision == "approve"
    assert duplicate is None


def test_transcript_defaults_off_and_unbounded_store_refuses_at_startup() -> None:
    api = chatops()
    chat = api.ChatOps(name="test", providers=(provider(),))
    assert chat.conversation_store is None

    with pytest.raises(api.ChatConfigurationError, match="conversation_store.*retention.*erase"):
        api.ChatOps(
            name="test",
            providers=(provider(),),
            conversation_store=UnboundedConversationStore(),
        )

    bounded = BoundedConversationStore()
    retained = api.ChatOps(name="test", providers=(provider(),), conversation_store=bounded)
    assert retained.conversation_store is bounded


def test_streams_deliver_live_output_without_enabling_transcripts() -> None:
    api = chatops()
    streams = object()
    chat = api.ChatOps(name="test", providers=(provider(),))

    @chat.command("agent", execution="durable", streams=streams)
    async def ask(context: Any) -> None:
        await context.emit(api.AgentEvent.text("answer"))

    assert chat.commands["agent"].streams is streams
    assert chat.conversation_store is None


@pytest.mark.asyncio
async def test_cancellation_is_idempotent_and_reaches_existing_job_owner() -> None:
    api = chatops()
    jobs = Jobs()
    chat = api.ChatOps(name="test", providers=(provider(),), jobs=jobs)

    assert await chat.cancel("job-1", reason="requested by user") is True
    assert await chat.cancel("job-1", reason="duplicate click") is False
    assert jobs.cancelled == [
        ("job-1", "requested by user"),
        ("job-1", "duplicate click"),
    ]


def test_audit_correlation_carries_interaction_job_trace_and_delivery() -> None:
    api = chatops()
    correlation = api.ChatCorrelation(
        interaction_id="interaction-1",
        job_id="job-1",
        trace_id="trace-1",
        provider_message_id="message-1",
    )
    audit = api.ChatAuditEvent(
        tenant="discord:guild:guild-1",
        actor="user-1",
        action="agent:run",
        correlation=correlation,
    )

    assert audit.correlation.interaction_id == "interaction-1"
    assert audit.correlation.job_id == "job-1"
    assert audit.correlation.trace_id == "trace-1"
    assert audit.correlation.provider_message_id == "message-1"


@pytest.mark.asyncio
async def test_proactive_message_uses_bot_auth_not_interaction_token() -> None:
    api = chatops()
    deliveries: list[Any] = []
    discord_provider = provider(deliver=lambda message: deliveries.append(message))
    chat = api.ChatOps(name="test", providers=(discord_provider,))

    await chat.send(
        tenant="discord:guild:guild-1",
        destination=discord().DiscordDestination(
            channel_id="channel-1", tenant="discord:guild:guild-1"
        ),
        content="scheduled report",
        idempotency_key="report:2026-09-02",
    )

    assert deliveries[0].interaction_token is None
    assert deliveries[0].channel_id == "channel-1"
    assert deliveries[0].idempotency_key == "report:2026-09-02"


def test_cross_tenant_conversation_refuses_before_store_access() -> None:
    api = chatops()
    chat = api.ChatOps(name="test", providers=(provider(),))
    reference = api.ChatReference(tenant="discord:guild:guild-2", id="conversation-1")

    with pytest.raises(api.ChatTenantMismatch, match="discord:guild:guild-2"):
        chat.require_tenant("discord:guild:guild-1", reference)


def test_provider_native_escape_hatch_is_explicit() -> None:
    api = discord()
    raw = {"flags": 1 << 15, "poll": {"question": {"text": "Ship?"}}}

    assert api.DiscordNativeDelivery(raw=raw).raw is raw
    with pytest.raises(api.UnsupportedDiscordField, match="poll"):
        api.DiscordMessage(content="Ship?", poll=raw["poll"])
