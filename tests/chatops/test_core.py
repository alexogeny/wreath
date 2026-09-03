from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath import Wreath
from wreath import logging as log
from wreath.auth import Identity
from wreath.chat import (
    AgentEvent,
    ChatAdmissionError,
    ChatConfigurationError,
    ChatContext,
    ChatCorrelation,
    ChatOps,
    ExternalIdentityKey,
    ExternalIdentityResolver,
    IdentityResolutionError,
    InMemoryChatActionStore,
    PrincipalBinding,
)
from wreath.exceptions import Forbidden
from wreath.policy import ConcurrencyPolicy, RateLimitPolicy
from wreath.webhooks import LocalReplayStore, PostgresWebhookInbox


def context() -> ChatContext:
    return ChatContext(
        provider="test",
        installation="tenant-1",
        tenant="test:tenant-1",
        actor="actor-1",
        conversation="conversation-1",
        delivery_id="delivery-1",
        native={},
    )


async def test_declared_action_refuses_startup_without_authorizer() -> None:
    chat = ChatOps(name="ops")

    @chat.command("deploy", action="Deploy::run")
    async def deploy() -> None:
        pass

    with pytest.raises(ChatConfigurationError, match="deploy.*authorizer"):
        await chat._startup()


async def test_declared_action_cannot_dispatch_without_authorizer() -> None:
    chat = ChatOps(name="ops")
    ran = False

    @chat.command("deploy", action="Deploy::run")
    async def deploy() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(PermissionError, match="authorizer"):
        await chat._dispatch(kind="command", name="deploy", context=context())
    assert ran is False


@dataclass
class BindingStore:
    binding: PrincipalBinding

    async def lookup(self, _key: ExternalIdentityKey) -> tuple[PrincipalBinding]:
        return (self.binding,)


async def test_identity_store_cannot_return_a_binding_for_another_external_key() -> None:
    requested = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    wrong = ExternalIdentityKey(provider="slack", installation="T2", subject="U1")
    binding = PrincipalBinding(identity=Identity("user-1"), external=wrong)
    resolver = ExternalIdentityResolver(store=BindingStore(binding))

    with pytest.raises(IdentityResolutionError, match="mismatched-identity-link"):
        await resolver.resolve(requested)


async def test_external_identity_resolution_applies_the_configured_federation() -> None:
    requested = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    binding = PrincipalBinding(identity=Identity("user-1"), external=requested)

    class Federation:
        async def resolve(
            self, key: ExternalIdentityKey, current: PrincipalBinding
        ) -> PrincipalBinding:
            assert key is requested
            return PrincipalBinding(
                identity=current.identity,
                external=current.external,
                principal=current.principal,
                tenant="acme",
            )

    resolver = ExternalIdentityResolver(store=BindingStore(binding), federation=Federation())

    assert (await resolver.resolve(requested)).tenant == "acme"


async def test_chatops_refuses_a_custom_resolver_binding_for_another_external_key() -> None:
    requested = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    wrong = ExternalIdentityKey(provider="slack", installation="T2", subject="U1")

    class Resolver:
        async def resolve(self, _key: ExternalIdentityKey) -> PrincipalBinding:
            return PrincipalBinding(identity=Identity("user-1"), external=wrong)

    chat = ChatOps(name="ops", identity=Resolver())
    current = context()
    current.external_identity = requested

    @chat.event("probe")
    async def probe() -> None:
        pass

    with pytest.raises(IdentityResolutionError, match="mismatched-identity-link"):
        await chat._dispatch(kind="event", name="probe", context=current)


async def test_default_replay_owner_is_bounded_and_rejects_a_duplicate() -> None:
    chat = ChatOps(name="ops")

    assert chat._local_replay.max_entries == 4096
    assert chat._local_replay.ttl == 600.0

    assert await chat._claim(provider="discord", installation="guild:1", delivery="interaction-1")
    assert not await chat._claim(
        provider="discord", installation="guild:1", delivery="interaction-1"
    )
    assert chat.replay_size == 1


async def test_existing_local_replay_owner_uses_its_native_claim_contract() -> None:
    inbox = LocalReplayStore(max_entries=8, ttl=30)
    chat = ChatOps(name="ops", inbox=inbox)

    assert await chat._claim(provider="slack", installation="T1", delivery="event-1")
    assert not await chat._claim(provider="slack", installation="T1", delivery="event-1")
    assert chat.replay_size == 1


@dataclass
class KeywordInbox:
    claims: list[dict[str, str]] = field(default_factory=list)

    async def claim(self, **options: str) -> bool:
        self.claims.append(options)
        return True


async def test_custom_inbox_uses_the_provider_keyword_claim_contract() -> None:
    inbox = KeywordInbox()
    chat = ChatOps(name="ops", inbox=inbox)

    assert await chat._claim(provider="slack", installation="T1", delivery="event-1")
    assert inbox.claims == [{"provider": "slack", "installation": "T1", "delivery": "event-1"}]


async def test_configured_inbox_without_a_claim_contract_refuses() -> None:
    chat = ChatOps(name="ops", inbox=object())

    with pytest.raises(ChatConfigurationError, match="inbox.*claim"):
        await chat._claim(provider="slack", installation="T1", delivery="event-1")


def test_postgres_inbox_requires_transaction_configuration_at_declaration() -> None:
    with pytest.raises(ChatConfigurationError, match="session_factory.*lease_owner"):
        ChatOps(name="ops", inbox=PostgresWebhookInbox())


class SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> None:
        del error_type, error, traceback


def test_postgres_inbox_accepts_transaction_configuration_at_declaration() -> None:
    inbox = PostgresWebhookInbox(
        session_factory=SessionContext,
        lease_owner="worker-1",
        lease_seconds=30,
    )
    assert ChatOps(name="ops", inbox=inbox).inbox is inbox


@dataclass
class AtomicInbox:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def claim_and_enqueue(self, **options: Any) -> bool:
        self.calls.append(options)
        await options["enqueue"](transaction="transaction-1")
        return True


async def test_atomic_claim_and_enqueue_uses_one_production_inbox_transaction() -> None:
    inbox = AtomicInbox()
    enqueued: list[Any] = []
    chat = ChatOps(name="ops", inbox=inbox)

    async def enqueue(*, transaction: Any) -> None:
        enqueued.append(transaction)

    accepted = await chat._claim_and_enqueue(
        provider="slack",
        installation="T1",
        delivery="event-1",
        body=b'{"event_id":"event-1"}',
        event_type="command:deploy",
        sent_at=1_700_000_000,
        result_status=200,
        enqueue=enqueue,
    )

    assert accepted is True
    assert enqueued == ["transaction-1"]
    assert len(inbox.calls) == 1
    options = inbox.calls[0]
    assert options["source"] == "slack:T1"
    assert options["envelope"].id == "event-1"
    assert options["envelope"].type == "command:deploy"
    assert options["envelope"].body == b'{"event_id":"event-1"}'
    assert options["result_status"] == 200


async def test_atomic_shared_claim_uses_the_same_owner_without_enqueuing_work() -> None:
    inbox = AtomicInbox()
    chat = ChatOps(name="ops", inbox=inbox)

    assert await chat._claim(
        provider="teams",
        installation="tenant:conversation",
        delivery="activity-1",
        body=b'{"id":"activity-1"}',
        event_type="message",
        sent_at=1_700_000_000,
    )
    assert len(inbox.calls) == 1
    assert inbox.calls[0]["result_status"] == 200


async def test_atomic_claim_defaults_timestamp_to_chat_clock() -> None:
    inbox = AtomicInbox()
    chat = ChatOps(name="ops", inbox=inbox, clock=lambda: 1_700_000_005)

    await chat._claim(
        provider="discord",
        installation="guild:1",
        delivery="interaction-1",
    )

    assert inbox.calls[0]["envelope"].timestamp.timestamp() == 1_700_000_005


async def test_non_transactional_inbox_cannot_queue_a_durable_delivery() -> None:
    chat = ChatOps(name="ops", inbox=LocalReplayStore(max_entries=8, ttl=30))

    async def enqueue(*, transaction: Any) -> None:
        pass

    with pytest.raises(ChatConfigurationError, match="transactional claim_and_enqueue"):
        await chat._claim_and_enqueue(
            provider="slack",
            installation="T1",
            delivery="event-1",
            body=b"{}",
            event_type="command:deploy",
            sent_at=None,
            result_status=200,
            enqueue=enqueue,
        )


@dataclass
class Jobs:
    cancellations: list[tuple[int | None, str | None, str]] = field(default_factory=list)

    async def cancel(
        self,
        job_id: int | None = None,
        *,
        key: str | None = None,
        reason: str = "cancelled",
    ) -> bool:
        self.cancellations.append((job_id, key, reason))
        return True


async def test_string_cancellation_uses_the_real_job_runner_key_contract() -> None:
    jobs = Jobs()
    chat = ChatOps(name="ops", jobs=jobs)

    assert await chat.cancel("discord:interaction:1", reason="user request")
    assert jobs.cancellations == [(None, "discord:interaction:1", "user request")]


async def test_durable_handler_failure_reaches_job_retry_owner() -> None:
    chat = ChatOps(name="ops")

    @chat.command("deploy", execution="durable")
    async def deploy() -> None:
        raise RuntimeError("retry me")

    with pytest.raises(RuntimeError, match="retry me"):
        await chat._dispatch(kind="command", name="deploy", context=context())
    assert chat.handler_errors == 1


@dataclass(frozen=True)
class JobContext:
    job_id: int = 41
    fence: int = 7
    attempt: int = 1
    trace_context: str = "00-trace-parent"


def test_durable_context_carries_agent_request_job_fence_and_emitter() -> None:
    chat = ChatOps(name="ops")
    current = context()
    job = JobContext()

    async def emit(_event: Any) -> None:
        pass

    activated = chat._durable_context(
        current,
        job_context=job,
        arguments={"prompt": "ship production"},
        emit=emit,
    )

    assert activated is current
    assert activated.job_context is job
    assert activated.emit is emit
    assert activated.agent_request is not None
    assert activated.agent_request.tenant == current.tenant
    assert activated.agent_request.actor == current.actor
    assert activated.agent_request.conversation == current.conversation
    assert activated.agent_request.prompt == "ship production"
    assert activated.agent_request.native is current.native
    assert activated.agent_request.correlation == ChatCorrelation(
        interaction_id=current.delivery_id,
        job_id="41",
        trace_id="00-trace-parent",
    )


async def test_durable_agent_request_is_rebound_after_identity_resolution() -> None:
    external = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    binding = PrincipalBinding(
        identity=Identity("user-1"),
        external=external,
        tenant="tenant-resolved",
    )
    chat = ChatOps(
        name="ops",
        identity=ExternalIdentityResolver(store=BindingStore(binding)),
    )
    current = context()
    current.external_identity = external
    seen: list[Any] = []

    @chat.command("agent", execution="durable")
    async def agent(context: ChatContext) -> None:
        seen.append(context.agent_request)

    async def emit(_event: Any) -> None:
        pass

    chat._durable_context(
        current,
        job_context=JobContext(),
        arguments={"prompt": "hello"},
        emit=emit,
    )
    await chat._dispatch(kind="command", name="agent", context=current)

    request = seen[0]
    assert request.tenant == "tenant-resolved"
    assert request.principal is binding.principal


async def test_durable_chat_output_can_share_the_existing_stream_owner() -> None:
    class Writer:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []
            self.finished = 0

        async def write(self, chunk: bytes) -> None:
            self.chunks.append(chunk)

        async def finish(self) -> None:
            self.finished += 1

        async def fail(self, detail: str) -> None:
            raise AssertionError(detail)

    class Streams:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []
            self.writer_instance = Writer()

        async def writer(self, key: str, *, fence: int, attempt: int) -> Writer:
            self.calls.append((key, fence, attempt))
            return self.writer_instance

    class Delegate:
        def __init__(self) -> None:
            self.events: list[Any] = []
            self.reply: Any = None

        async def __call__(self, event: Any) -> None:
            self.events.append(event)

        async def finish(self, reply: Any) -> None:
            self.reply = reply

    streams = Streams()
    chat = ChatOps(name="ops")
    current = context()
    delegate = Delegate()

    @chat.command("agent", execution="durable", streams=streams)
    async def agent(context: ChatContext) -> str:
        await context.emit(AgentEvent.text("hello"))
        return "done"

    declaration = chat.commands["agent"]
    emitter = await chat._stream_emitter(declaration, current, JobContext(), delegate)
    chat._durable_context(
        current,
        job_context=JobContext(),
        arguments={},
        emit=emitter,
    )
    reply = await chat._dispatch(kind="command", name="agent", context=current)
    await emitter.finish(reply)

    assert streams.calls == [(current.stream_key, 7, 1)]
    assert streams.writer_instance.chunks == [b'{"kind":"text","content":"hello"}']
    assert streams.writer_instance.finished == 1
    assert delegate.events == [AgentEvent.text("hello")]


async def test_chat_dispatch_outcome_reaches_the_structured_flight_log() -> None:
    chat = ChatOps(name="ops")

    @chat.command("status")
    async def status() -> str:
        return "ok"

    current = context()
    current.provider = "slack"
    with log.testing_runtime(level=log.INFO) as records:
        await chat._dispatch(kind="command", name="status", context=current)
        rendered = log.render(records[0])

    assert len(records) == 1
    assert rendered.startswith("Slack chat action")
    assert rendered.endswith("succeeded")
    assert len(records[0].args) == 1


async def test_federated_chat_identity_is_rate_limited_before_handler_activation() -> None:
    policy = RateLimitPolicy(limit=1, window=60.0)
    chat = ChatOps(name="ops", rate_limit=policy)
    calls = 0

    @chat.command("deploy")
    async def deploy() -> None:
        nonlocal calls
        calls += 1

    await chat._dispatch(kind="command", name="deploy", context=context())
    with pytest.raises(ChatAdmissionError) as refused:
        await chat._dispatch(kind="command", name="deploy", context=context())

    assert chat.problem(refused.value).status == 429
    assert calls == 1


async def test_chat_releases_the_shared_concurrency_permit_after_a_failure() -> None:
    policy = ConcurrencyPolicy(1)
    chat = ChatOps(name="ops", admission=policy)

    @chat.command("deploy", execution="durable")
    async def deploy() -> None:
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError):
        await chat._dispatch(kind="command", name="deploy", context=context())
    assert policy.stats().active == 0


def test_chat_errors_use_wreath_problem_details_without_leaking_internal_failures() -> None:
    chat = ChatOps(name="ops")

    denied = chat.problem(Forbidden("policy denied the action"))
    failed = chat.problem(RuntimeError("database password is secret"))

    assert denied.as_dict()["status"] == 403
    assert denied.as_dict()["detail"] == "policy denied the action"
    assert failed.as_dict()["status"] == 500
    assert failed.as_dict()["detail"] == "Internal Server Error"


async def test_inline_handler_failure_is_counted_and_acknowledged() -> None:
    chat = ChatOps(name="ops")

    @chat.command("deploy")
    async def deploy() -> None:
        raise RuntimeError("do not expose me")

    assert await chat._dispatch(kind="command", name="deploy", context=context()) is None
    assert chat.handler_errors == 1


async def test_unknown_command_parameter_refuses_before_handler() -> None:
    chat = ChatOps(name="ops")
    ran = False

    @chat.command("deploy")
    async def deploy(environment: str) -> None:
        nonlocal ran
        ran = True

    with pytest.raises(ValueError, match="unexpected chat command parameter extra"):
        await chat._dispatch(
            kind="command",
            name="deploy",
            context=context(),
            arguments={"environment": "production", "extra": "ignored"},
        )
    assert ran is False


async def test_keyword_only_context_is_injected_by_name() -> None:
    chat = ChatOps(name="ops")
    seen: ChatContext | None = None

    @chat.command("deploy")
    async def deploy(*, context: ChatContext) -> None:
        nonlocal seen
        seen = context

    current = context()
    await chat._dispatch(kind="command", name="deploy", context=current)
    assert seen is current


async def test_action_prefix_dispatches_a_bound_single_use_identifier() -> None:
    chat = ChatOps(name="ops")
    seen: list[str] = []

    @chat.action("agent:approve:", prefix=True)
    async def approve(context: ChatContext) -> None:
        seen.append(context.action or "")

    current = context()
    current.action = "agent:approve:approval-7"

    await chat._dispatch(
        kind="action",
        name="agent:approve:approval-7",
        context=current,
    )

    assert seen == ["agent:approve:approval-7"]


def test_overlapping_action_prefixes_refuse_at_declaration() -> None:
    chat = ChatOps(name="ops")

    @chat.action("agent:", prefix=True)
    async def broad() -> None:
        return None

    with pytest.raises(ValueError, match="overlaps declared prefix 'agent:'"):

        @chat.action("agent:approve:", prefix=True)
        async def ambiguous() -> None:
            return None


@pytest.mark.parametrize("signature", ["positional-only", "variadic"])
def test_unsupported_handler_signature_refuses_at_declaration(signature: str) -> None:
    chat = ChatOps(name="ops")

    with pytest.raises(ValueError, match=f"{signature}.*named parameters"):
        if signature == "positional-only":

            @chat.command("deploy")
            async def deploy(environment: str, /) -> None:
                pass

        else:

            @chat.command("deploy")
            async def deploy(*values: str) -> None:
                pass


async def test_local_action_store_refuses_capacity_and_expires_capabilities() -> None:
    now = 100.0
    store = InMemoryChatActionStore(max_entries=1, ttl=10, clock=lambda: now)
    action = await store.issue(
        workflow="release-1",
        decision="approve",
        tenant="teams:T1",
        actor="user-1",
    )

    with pytest.raises(ChatConfigurationError, match="action store.*capacity"):
        await store.issue(
            workflow="release-2",
            decision="approve",
            tenant="teams:T1",
            actor="user-1",
        )

    now = 111.0
    assert await store.claim(action.custom_id, tenant="teams:T1", actor="user-1") is None
    replacement = await store.issue(
        workflow="release-2",
        decision="approve",
        tenant="teams:T1",
        actor="user-1",
    )
    assert replacement.workflow == "release-2"


def test_local_action_store_refuses_an_infinite_capability_lifetime() -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        InMemoryChatActionStore(max_entries=1, ttl=math.inf)


class Provider:
    name = "test"

    def __init__(self) -> None:
        self.mounts: list[tuple[Any, Any, str]] = []

    def _mount(self, chat: ChatOps, app: Wreath, path: str) -> None:
        self.mounts.append((chat, app, path))


def test_provider_can_be_added_to_the_one_shared_runtime() -> None:
    app = Wreath()
    chat = ChatOps(app, name="ops")
    provider = Provider()

    assert chat.add(provider) is provider
    assert provider.mounts == [(chat, app, "/_wreath/chat")]
    with pytest.raises(ChatConfigurationError, match="duplicate.*test"):
        chat.add(Provider())


def test_one_application_refuses_ambiguous_chat_runtime_names() -> None:
    app = Wreath()
    ChatOps(app, name="ops")

    with pytest.raises(ChatConfigurationError, match="duplicate ChatOps runtime 'ops'"):
        ChatOps(app, name="ops")


def test_application_defined_chatops_subclass_participates_in_duplicate_detection() -> None:
    class DeploymentChatOps(ChatOps):
        pass

    app = Wreath()
    DeploymentChatOps(app, name="ops")

    with pytest.raises(ChatConfigurationError, match="duplicate ChatOps runtime 'ops'"):
        ChatOps(app, name="ops")
