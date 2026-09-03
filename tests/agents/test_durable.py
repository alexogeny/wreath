from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._agents.durable import (
    DurableAgent,
    DurableTurn,
    EffectCheckpoint,
    ModelResult,
    StaleAgentFence,
    ToolCall,
    UnknownModelOutcome,
    stable_tool_call_id,
    stable_turn_id,
)
from wreath.jobs import JobContext


class Jobs:
    def __init__(self) -> None:
        self.registration: tuple[str, dict[str, Any]] | None = None
        self.handler: Any = None
        self.enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def task(self, name: str, **options: Any) -> Any:
        self.registration = (name, options)

        def register(handler: Any) -> Any:
            self.handler = handler
            return handler

        return register

    async def enqueue(self, task: str, *args: Any, **options: Any) -> int:
        self.enqueued.append((task, args, options))
        return 41


class Checkpoints:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], EffectCheckpoint] = {}
        self.expected_fence = 7
        self.validations: list[tuple[str, str, str, int]] = []

    async def completed(
        self, turn_id: str, call_id: str, *, tenant: str, principal_id: str
    ) -> EffectCheckpoint | None:
        record = self.records.get((turn_id, call_id))
        if record is not None and (record.tenant != tenant or record.principal_id != principal_id):
            raise AssertionError("cross-principal checkpoint lookup")
        return record

    async def validate_fence(
        self, turn_id: str, *, tenant: str, principal_id: str, fence: int
    ) -> None:
        self.validations.append((turn_id, tenant, principal_id, fence))
        if fence != self.expected_fence:
            raise StaleAgentFence(turn_id, fence)

    async def complete(self, checkpoint: EffectCheckpoint) -> bool:
        key = (checkpoint.turn_id, checkpoint.call_id)
        if key in self.records:
            return False
        self.records[key] = checkpoint
        return True


class Backend:
    def __init__(self, result: ModelResult | BaseException) -> None:
        self.result = result
        self.turns: list[DurableTurn] = []

    async def complete(self, turn: DurableTurn) -> ModelResult:
        self.turns.append(turn)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, Any]] = []

    async def execute(self, call: ToolCall, *, context: Any) -> Any:
        self.calls.append((call, context))
        return {"released": True}


def invocation() -> SimpleNamespace:
    return SimpleNamespace(
        tenant="tenant-a",
        principal=SimpleNamespace(id="user-7"),
        conversation="conversation-2",
        correlation_id="trace-9",
    )


def invocation_with_principal(principal: Any) -> SimpleNamespace:
    value = invocation()
    value.principal = principal
    return value


def job_context(*, fence: int = 7, tenant: str = "tenant-a") -> JobContext:
    return JobContext(
        job_id=41,
        task="agent_releaser",
        attempt=1,
        fence=fence,
        tenant=tenant,
        key=stable_turn_id("tenant-a", "user-7", "conversation-2", "message-4"),
        trace_context="trace-9",
    )


def test_turn_and_tool_ids_are_stable_and_domain_separated() -> None:
    first = stable_turn_id("tenant-a", "user-7", "conversation-2", "message-4")
    second = stable_turn_id("tenant-a", "user-7", "conversation-2", "message-4")
    other_tenant = stable_turn_id("tenant-b", "user-7", "conversation-2", "message-4")
    call = stable_tool_call_id(first, 0, "release", {"version": 3, "force": False})
    reordered = stable_tool_call_id(first, 0, "release", {"force": False, "version": 3})

    assert first == second
    assert first != other_tenant
    assert call == reordered
    assert call != stable_tool_call_id(first, 1, "release", {"version": 3, "force": False})

    arguments = {"nested": {"version": 3}}
    tool_call = ToolCall("release", arguments)
    arguments["nested"]["version"] = 4
    assert tool_call.arguments == {"nested": {"version": 3}}


@pytest.mark.parametrize(
    ("turn_id", "name"),
    [("", "release"), ("turn", "")],
)
def test_tool_call_ids_refuse_each_empty_identity_part(turn_id: str, name: str) -> None:
    with pytest.raises(ValueError, match="require a turn ID and tool name"):
        stable_tool_call_id(turn_id, 0, name, {})


def test_tool_call_ids_refuse_negative_indexes() -> None:
    with pytest.raises(ValueError, match="index must be non-negative"):
        stable_tool_call_id("turn", -1, "release", {})


def test_tool_calls_refuse_empty_names_and_non_object_arguments() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        ToolCall("", {})
    with pytest.raises(TypeError, match="arguments must be a JSON object"):
        ToolCall("release", cast(Any, []))


@pytest.mark.parametrize("empty", range(4))
def test_turn_ids_refuse_each_empty_identity_component(empty: int) -> None:
    parts = ["tenant-a", "user-7", "conversation-2", "message-4"]
    parts[empty] = ""

    with pytest.raises(ValueError, match="require tenant"):
        stable_turn_id(*parts)


def test_durable_payload_refuses_schema_and_type_drift() -> None:
    turn = DurableTurn("turn", "tenant-a", "user-7", "conversation-2", "prompt")
    payload = turn.as_payload()
    assert DurableTurn.from_payload(payload) == turn
    invalid = (
        {key: value for key, value in payload.items() if key != "prompt"},
        {**payload, "extra": "value"},
        {**payload, "tenant": 7},
        {**payload, "correlation_id": 7},
    )

    for candidate in invalid:
        with pytest.raises(ValueError, match="payload|string|correlation"):
            DurableTurn.from_payload(candidate)


def test_durable_declarations_refuse_empty_runtime_identity() -> None:
    with pytest.raises(ValueError, match="durable turns require"):
        DurableTurn("", "tenant-a", "user-7", "conversation-2", "prompt")
    with pytest.raises(ValueError, match="name must be non-empty"):
        DurableAgent(
            name="",
            jobs=Jobs(),
            backend=Backend(ModelResult()),
            tools=Tools(),
            checkpoints=Checkpoints(),
        )
    with pytest.raises(ValueError, match="principal_id"):
        DurableAgent(
            name="agent",
            jobs=Jobs(),
            backend=Backend(ModelResult()),
            tools=Tools(),
            checkpoints=Checkpoints(),
            principal_id="",
        )


def test_durable_invocation_accepts_public_principal_shapes_and_refuses_ambiguous_values() -> None:
    raw = invocation_with_principal("user-7")
    subject = invocation_with_principal(SimpleNamespace(subject="user-7"))
    fallback = invocation_with_principal(SimpleNamespace(id=7, subject="user-7"))
    empty_fallback = invocation_with_principal(SimpleNamespace(id="", subject="user-7"))

    assert DurableTurn.from_invocation(raw, prompt="go", message_id="message").principal_id == (
        "user-7"
    )
    assert (
        DurableTurn.from_invocation(subject, prompt="go", message_id="message").principal_id
        == "user-7"
    )
    assert (
        DurableTurn.from_invocation(fallback, prompt="go", message_id="message").principal_id
        == "user-7"
    )
    assert (
        DurableTurn.from_invocation(empty_fallback, prompt="go", message_id="message").principal_id
        == "user-7"
    )
    for principal in ("", SimpleNamespace(id=""), object()):
        with pytest.raises(ValueError, match="principal"):
            DurableTurn.from_invocation(
                invocation_with_principal(principal),
                prompt="go",
                message_id="message",
            )


@pytest.mark.asyncio
async def test_effect_checkpoint_race_refuses_the_stale_worker() -> None:
    class RacingCheckpoints(Checkpoints):
        async def complete(self, checkpoint: EffectCheckpoint) -> bool:
            return False

    jobs = Jobs()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(ModelResult(tool_calls=(ToolCall("release", {}),))),
        tools=Tools(),
        checkpoints=RacingCheckpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(StaleAgentFence, match="stale fence 7"):
        await jobs.handler(job_context(), payload)


@pytest.mark.asyncio
async def test_adapter_refuses_recovery_attempts_until_effect_claims_are_atomic() -> None:
    jobs = Jobs()
    tools = Tools()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(ModelResult(tool_calls=(ToolCall("release", {}),))),
        tools=tools,
        checkpoints=Checkpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()
    retried = replace(job_context(), attempt=2)

    with pytest.raises(UnknownModelOutcome, match="attempt 2"):
        await jobs.handler(retried, payload)

    assert tools.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [None, True])
async def test_adapter_refuses_non_integer_recovery_attempts(attempt: object) -> None:
    jobs = Jobs()
    tools = Tools()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(ModelResult()),
        tools=tools,
        checkpoints=Checkpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(UnknownModelOutcome, match="recovery attempt"):
        await jobs.handler(replace(job_context(), attempt=cast(Any, attempt)), payload)

    assert tools.calls == []


@pytest.mark.asyncio
async def test_duplicate_explicit_call_ids_refuse_before_any_effect() -> None:
    jobs = Jobs()
    tools = Tools()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(
            ModelResult(
                tool_calls=(
                    ToolCall("release", {"version": 1}, "duplicate"),
                    ToolCall("notify", {"version": 2}, "duplicate"),
                )
            )
        ),
        tools=tools,
        checkpoints=Checkpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(ValueError, match="duplicate tool call ID"):
        await jobs.handler(job_context(), payload)

    assert tools.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turn_id", "another-turn"),
        ("call_id", "another-call"),
        ("tenant", "tenant-b"),
        ("principal_id", "user-8"),
    ],
)
async def test_misbound_checkpoint_cannot_suppress_an_effect(field: str, value: str) -> None:
    class MisboundCheckpoints(Checkpoints):
        async def completed(
            self,
            turn_id: str,
            call_id: str,
            *,
            tenant: str,
            principal_id: str,
        ) -> EffectCheckpoint | None:
            checkpoint = EffectCheckpoint(turn_id, call_id, tenant, principal_id, 7)
            return replace(checkpoint, **{field: value})

    jobs = Jobs()
    tools = Tools()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(ModelResult(tool_calls=(ToolCall("release", {}, "call-1"),))),
        tools=tools,
        checkpoints=MisboundCheckpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(ValueError, match="misbound checkpoint"):
        await jobs.handler(job_context(), payload)

    assert tools.calls == []


@pytest.mark.asyncio
async def test_adapter_registers_one_non_retrying_job_and_enqueues_a_serializable_turn() -> None:
    jobs = Jobs()
    adapter = DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(ModelResult()),
        tools=Tools(),
        checkpoints=Checkpoints(),
    )

    job_id = await adapter.enqueue(invocation(), prompt="release 3", message_id="message-4")

    assert jobs.registration == ("agent_releaser", {"retries": 0})
    assert job_id == 41
    task, args, options = jobs.enqueued[0]
    assert task == "agent_releaser"
    assert options == {"key": args[0]["turn_id"], "tenant": "tenant-a"}
    assert args[0] == {
        "turn_id": stable_turn_id("tenant-a", "user-7", "conversation-2", "message-4"),
        "tenant": "tenant-a",
        "principal_id": "user-7",
        "conversation": "conversation-2",
        "prompt": "release 3",
        "correlation_id": "trace-9",
    }


@pytest.mark.asyncio
async def test_completed_effect_is_not_replayed_and_new_effect_is_fenced() -> None:
    jobs = Jobs()
    checkpoints = Checkpoints()
    tools = Tools()
    turn_id = stable_turn_id("tenant-a", "user-7", "conversation-2", "message-4")
    completed_call = ToolCall("release", {"version": 2}, "call-completed")
    checkpoints.records[(turn_id, "call-completed")] = EffectCheckpoint(
        turn_id=turn_id,
        call_id="call-completed",
        tenant="tenant-a",
        principal_id="user-7",
        fence=3,
    )
    fresh_call = ToolCall("notify", {"channel": "ops"})
    backend = Backend(ModelResult(tool_calls=(completed_call, fresh_call)))
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=backend,
        tools=tools,
        checkpoints=checkpoints,
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release 3", message_id="message-4"
    ).as_payload()

    await jobs.handler(job_context(), payload)

    assert len(tools.calls) == 1
    executed, effect_context = tools.calls[0]
    assert executed.call_id == stable_tool_call_id(turn_id, 1, "notify", {"channel": "ops"})
    assert effect_context.job_context == job_context()
    assert effect_context.tenant == "tenant-a"
    assert effect_context.principal_id == "user-7"
    assert executed.call_id is not None
    assert checkpoints.records[(turn_id, executed.call_id)].fence == 7
    assert checkpoints.validations == [
        (turn_id, "tenant-a", "user-7", 7),
        (turn_id, "tenant-a", "user-7", 7),
    ]


@pytest.mark.asyncio
async def test_failed_effect_is_not_checkpointed() -> None:
    class FailingTools(Tools):
        async def execute(self, call: ToolCall, *, context: Any) -> Any:
            raise RuntimeError("declined")

    jobs = Jobs()
    checkpoints = Checkpoints()
    backend = Backend(ModelResult(tool_calls=(ToolCall("release", {}),)))
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=backend,
        tools=FailingTools(),
        checkpoints=checkpoints,
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(RuntimeError, match="declined"):
        await jobs.handler(job_context(), payload)

    assert checkpoints.records == {}


@pytest.mark.asyncio
async def test_unknown_model_outcome_propagates_and_task_has_no_automatic_retry() -> None:
    jobs = Jobs()
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=Backend(UnknownModelOutcome("connection ended after request write")),
        tools=Tools(),
        checkpoints=Checkpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()

    with pytest.raises(UnknownModelOutcome, match="after request write"):
        await jobs.handler(job_context(), payload)

    assert jobs.registration == ("agent_releaser", {"retries": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "payload_change", "match"),
    [
        (job_context(tenant="tenant-b"), {}, "tenant"),
        (job_context(), {"principal_id": "user-8"}, "principal"),
        (job_context(fence=8), {}, "fence"),
    ],
)
async def test_job_rejects_mismatched_tenant_principal_and_fence(
    context: JobContext, payload_change: dict[str, Any], match: str
) -> None:
    jobs = Jobs()
    backend = Backend(ModelResult(tool_calls=(ToolCall("release", {}),)))
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=backend,
        tools=Tools(),
        checkpoints=Checkpoints(),
        principal_id="user-7",
    )
    turn = DurableTurn.from_invocation(invocation(), prompt="release", message_id="message-4")
    payload = replace(turn, **payload_change).as_payload()

    with pytest.raises((ValueError, StaleAgentFence), match=match):
        await jobs.handler(context, payload)

    assert backend.turns == []


@pytest.mark.asyncio
async def test_job_key_is_bound_to_the_stable_turn_id() -> None:
    jobs = Jobs()
    backend = Backend(ModelResult())
    DurableAgent(
        name="releaser",
        jobs=jobs,
        backend=backend,
        tools=Tools(),
        checkpoints=Checkpoints(),
    )
    payload = DurableTurn.from_invocation(
        invocation(), prompt="release", message_id="message-4"
    ).as_payload()
    wrong_key = replace(job_context(), key="another-turn")

    with pytest.raises(ValueError, match="job key"):
        await jobs.handler(wrong_key, payload)

    assert backend.turns == []
