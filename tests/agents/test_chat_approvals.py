from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._agents.approvals import InMemoryApprovalStore
from wreath._agents.chat_approvals import ChatApprovalFlow
from wreath.auth import Identity
from wreath.authorization import human
from wreath.chat import ChatContext, ChatOps, ChatReply


def context(
    provider: str = "slack",
    *,
    tenant: str = "tenant-a",
    principal_id: str = "user-7",
    claims: dict[str, object] | None = None,
) -> ChatContext:
    identity = Identity(principal_id, claims=claims or {})
    return ChatContext(
        provider=provider,
        installation="installation-1",
        tenant=tenant,
        actor="channel-user-9",
        conversation="conversation-2",
        delivery_id="delivery-3",
        native={},
        identity=identity,
        principal=human(identity),
    )


def flow(clock: list[float]) -> tuple[ChatOps, InMemoryApprovalStore, ChatApprovalFlow]:
    chat = ChatOps(name="ops")
    store = InMemoryApprovalStore(max_entries=8, clock=lambda: clock[0])
    return chat, store, ChatApprovalFlow(chat, store)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["slack", "teams", "discord"])
async def test_issue_renders_provider_native_single_use_actions(provider: str) -> None:
    chat, store, approvals = flow([100.0])

    reply = await approvals.issue(
        context(provider),
        approval_id="approval_7",
        action="deploy",
        resource="production",
        ttl=30,
    )

    approve = f"{approvals.approve_prefix}approval_7"
    deny = f"{approvals.deny_prefix}approval_7"
    rendered = reply.for_provider(provider)
    if provider == "slack":
        buttons = rendered["blocks"][1]["elements"]
        assert [(item["text"]["text"], item["action_id"]) for item in buttons] == [
            ("Approve", approve),
            ("Deny", deny),
        ]
        assert all("value" not in item for item in buttons)
    elif provider == "teams":
        assert [(item["title"], item["verb"]) for item in rendered["actions"]] == [
            ("Approve", approve),
            ("Deny", deny),
        ]
        assert [item["fallback"]["data"] for item in rendered["actions"]] == [
            {"verb": approve},
            {"verb": deny},
        ]
    else:
        assert rendered["flags"] == 64
        buttons = rendered["components"][0]["components"]
        assert [(item["label"], item["custom_id"]) for item in buttons] == [
            ("Approve", approve),
            ("Deny", deny),
        ]
    assert "deploy" in str(rendered)
    assert "production" in str(rendered)

    issued = await store.claim("approval_7", tenant="tenant-a", principal_id="user-7")
    assert (issued.action, issued.resource) == ("deploy", "production")
    assert set(chat.actions) == {approvals.approve_prefix, approvals.deny_prefix}


@pytest.mark.asyncio
async def test_approve_and_deny_handlers_claim_for_the_linked_internal_principal() -> None:
    chat, _, approvals = flow([100.0])
    current = context()
    await approvals.issue(
        current,
        approval_id="approve_1",
        action="deploy",
        ttl=30,
    )
    await approvals.issue(
        current,
        approval_id="deny_1",
        action="delete",
        ttl=30,
    )

    current.action = f"{approvals.approve_prefix}approve_1"
    approved = await chat._dispatch(kind="action", name=current.action, context=current)
    assert approved is not None
    assert approved.content == "Approved."

    current.action = f"{approvals.deny_prefix}deny_1"
    denied = await chat._dispatch(kind="action", name=current.action, context=current)
    assert denied is not None
    assert denied.content == "Denied."


async def test_human_approval_dispatches_the_bound_action_handler() -> None:
    chat, _, approvals = flow([100.0])
    seen: list[tuple[ChatContext, Any]] = []

    async def execute(current: ChatContext, grant: Any) -> ChatReply:
        seen.append((current, grant))
        return ChatReply.text("Refund executed by your approval.")

    approvals.on_approved("billing.refund", execute)
    current = context()
    await approvals.issue(
        current,
        approval_id="refund_1",
        action="billing.refund",
        resource="refund:organization:acme:payment:41:USD:100",
        ttl=30,
    )
    current.action = f"{approvals.approve_prefix}refund_1"

    reply = await chat._dispatch(kind="action", name=current.action, context=current)

    assert reply is not None
    assert reply.content == "Refund executed by your approval."
    assert len(seen) == 1
    approved_context, grant = seen[0]
    assert approved_context is current
    assert grant.action == "billing.refund"
    assert grant.resource == "refund:organization:acme:payment:41:USD:100"

    current.action = f"{approvals.approve_prefix}approve_1"
    assert await chat._dispatch(kind="action", name=current.action, context=current) is None
    current.action = f"{approvals.approve_prefix}deny_1"
    assert await chat._dispatch(kind="action", name=current.action, context=current) is None


@pytest.mark.asyncio
async def test_forged_actor_and_tenant_cannot_claim_an_approval() -> None:
    chat, _, approvals = flow([100.0])
    owner = context()
    await approvals.issue(owner, approval_id="approval_7", action="deploy", ttl=30)

    forged_actor = context(principal_id="user-8")
    forged_actor.action = f"{approvals.approve_prefix}approval_7"
    assert (
        await chat._dispatch(kind="action", name=forged_actor.action, context=forged_actor) is None
    )
    forged_tenant = context(tenant="tenant-b")
    forged_tenant.action = forged_actor.action
    assert (
        await chat._dispatch(kind="action", name=forged_tenant.action, context=forged_tenant)
        is None
    )

    owner.action = forged_actor.action
    assert await chat._dispatch(kind="action", name=owner.action, context=owner) is not None


@pytest.mark.asyncio
async def test_expired_and_malformed_actions_refuse_without_claiming_another_id() -> None:
    clock = [100.0]
    chat, _, approvals = flow(clock)
    current = context()
    await approvals.issue(current, approval_id="approval_7", action="deploy", ttl=1)
    clock[0] = 102.0

    for suffix in ("approval_7", "", "../approval_7", "x" * 65):
        current.action = f"{approvals.approve_prefix}{suffix}"
        assert await chat._dispatch(kind="action", name=current.action, context=current) is None


@pytest.mark.asyncio
async def test_fresh_auth_uses_the_newest_linked_authentication_stamp() -> None:
    clock = [100.0]
    chat, _, approvals = flow(clock)
    issued_by = context()
    await approvals.issue(
        issued_by,
        approval_id="approval_7",
        action="deploy",
        ttl=30,
        require_fresh_auth=True,
    )

    stale = context(claims={"auth_time": 99.0, "second_factor_at": 98.0})
    stale.action = f"{approvals.approve_prefix}approval_7"
    assert await chat._dispatch(kind="action", name=stale.action, context=stale) is None

    fresh = context(claims={"auth_time": 99.0, "second_factor_at": 101.0})
    fresh.action = stale.action
    result = await chat._dispatch(kind="action", name=fresh.action, context=fresh)
    assert result is not None
    assert result.content == "Approved."


@pytest.mark.asyncio
async def test_issue_refuses_unlinked_unknown_provider_and_invalid_ids_before_store_use() -> None:
    _, _, approvals = flow([100.0])
    unlinked = context()
    unlinked.identity = None
    unlinked.principal = None
    with pytest.raises(LookupError, match="linked identity"):
        await approvals.issue(unlinked, approval_id="approval_7", action="deploy", ttl=30)
    with pytest.raises(ValueError, match="unsupported chat approval provider"):
        await approvals.issue(context("matrix"), approval_id="approval_7", action="deploy", ttl=30)
    for approval_id in ("", "contains:colon", "x" * 65):
        with pytest.raises(ValueError, match="approval ID"):
            await approvals.issue(context(), approval_id=approval_id, action="deploy", ttl=30)


def test_overlapping_flows_refuse_when_registering_fixed_action_prefixes() -> None:
    chat = ChatOps(name="ops")
    first = InMemoryApprovalStore()
    second = InMemoryApprovalStore()
    ChatApprovalFlow(chat, first)

    with pytest.raises(ValueError, match="overlaps|duplicate"):
        ChatApprovalFlow(chat, second)


def test_flow_refuses_non_chat_and_non_approval_store_owners() -> None:
    with pytest.raises(TypeError, match="requires ChatOps"):
        ChatApprovalFlow(cast(Any, object()), InMemoryApprovalStore())
    with pytest.raises(TypeError, match="ApprovalStore"):
        ChatApprovalFlow(ChatOps(name="ops"), cast(Any, object()))


def test_approved_handlers_refuse_invalid_and_duplicate_declarations() -> None:
    _, _, approvals = flow([100.0])

    async def handler(_context: ChatContext, _grant: Any) -> ChatReply:
        return ChatReply.text("done")

    for action in ("", cast(str, 1)):
        with pytest.raises(ValueError, match="non-empty string"):
            approvals.on_approved(action, handler)
    for invalid in (object(), lambda _context, _grant: ChatReply.text("sync")):
        with pytest.raises(TypeError, match="async callable"):
            approvals.on_approved("deploy", cast(Any, invalid))

    approvals.on_approved("deploy", handler)
    with pytest.raises(ValueError, match="duplicate chat approved action"):
        approvals.on_approved("deploy", handler)


@pytest.mark.asyncio
async def test_link_binding_refuses_each_missing_or_conflicting_server_side_fact() -> None:
    _, _, approvals = flow([100.0])
    cases = [
        context(tenant=""),
        context(),
        context(),
        context(),
    ]
    cases[1].identity = Identity("")
    cases[1].principal = human(cases[1].identity)
    cases[2].principal = None
    cases[3].principal = SimpleNamespace(identity=Identity("user-8"))
    for current in cases:
        with pytest.raises(LookupError, match="linked identity|does not match"):
            await approvals.issue(
                current,
                approval_id="approval_7",
                action="deploy",
                ttl=30,
            )

    plain = context()
    plain.principal = SimpleNamespace(id="user-7")
    reply = await approvals.issue(
        plain,
        approval_id="plain_1",
        action="deploy",
        ttl=30,
    )
    assert reply.content is not None

    mismatched_plain = context()
    mismatched_plain.principal = SimpleNamespace(id="user-8")
    with pytest.raises(LookupError, match="does not match"):
        await approvals.issue(
            mismatched_plain,
            approval_id="plain_2",
            action="deploy",
            ttl=30,
        )

    opaque = context()
    opaque.principal = object()
    reply = await approvals.issue(
        opaque,
        approval_id="opaque_1",
        action="deploy",
        ttl=30,
    )
    assert reply.content is not None


@pytest.mark.asyncio
async def test_non_mapping_and_boolean_auth_claims_never_count_as_fresh_auth() -> None:
    clock = [0.0]
    chat, _, approvals = flow(clock)
    await approvals.issue(
        context(),
        approval_id="approval_7",
        action="deploy",
        ttl=30,
        require_fresh_auth=True,
    )
    invalid = context()
    invalid.identity = Identity("user-7", claims=cast(Any, []))
    invalid.action = f"{approvals.approve_prefix}approval_7"
    assert await chat._dispatch(kind="action", name=invalid.action, context=invalid) is None

    boolean = context(claims={"auth_time": True, "second_factor_at": False})
    boolean.action = invalid.action
    assert approvals._authenticated_at(boolean) is None
    assert await chat._dispatch(kind="action", name=boolean.action, context=boolean) is None

    recording = RecordingStore()
    recording_chat = ChatOps(name="recording")
    recording_flow = ChatApprovalFlow(recording_chat, recording)
    invalid.action = f"{recording_flow.approve_prefix}approval_8"
    result = await recording_chat._dispatch(kind="action", name=invalid.action, context=invalid)
    assert result is not None
    assert recording.calls == [
        (
            "claim",
            {
                "approval_id": "approval_8",
                "tenant": "tenant-a",
                "principal_id": "user-7",
                "authenticated_at": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_render_without_resource_does_not_invent_one() -> None:
    _, _, approvals = flow([100.0])
    rendered = (
        await approvals.issue(
            context("slack"),
            approval_id="approval_7",
            action="deploy",
            ttl=30,
        )
    ).for_provider("slack")
    assert rendered["text"] == "Approve deploy?"


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def issue(self, **options: Any) -> Any:
        self.calls.append(("issue", options))
        return SimpleNamespace(**options)

    async def claim(self, approval_id: str, **options: Any) -> Any:
        self.calls.append(("claim", {"approval_id": approval_id, **options}))
        return object()

    async def deny(self, approval_id: str, **options: Any) -> None:
        self.calls.append(("deny", {"approval_id": approval_id, **options}))


@pytest.mark.asyncio
async def test_each_action_performs_one_store_call_with_no_client_supplied_authority() -> None:
    chat = ChatOps(name="ops")
    store = RecordingStore()
    approvals = ChatApprovalFlow(chat, store)
    current = context(claims={"auth_time": 101.0})

    await approvals.issue(
        current,
        approval_id="approval_7",
        action="deploy",
        resource="production",
        ttl=30,
    )
    assert store.calls[-1][0] == "issue"
    current.action = f"{approvals.approve_prefix}approval_7"
    await chat._dispatch(kind="action", name=current.action, context=current)
    assert store.calls[-1] == (
        "claim",
        {
            "approval_id": "approval_7",
            "tenant": "tenant-a",
            "principal_id": "user-7",
            "authenticated_at": 101.0,
        },
    )
    assert len(store.calls) == 2

    current.action = f"{approvals.deny_prefix}approval_8"
    await chat._dispatch(kind="action", name=current.action, context=current)
    assert store.calls[-1] == (
        "deny",
        {
            "approval_id": "approval_8",
            "tenant": "tenant-a",
            "principal_id": "user-7",
        },
    )
    assert len(store.calls) == 3
