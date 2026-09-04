from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import wreath.chat._core as chat_core
from wreath import logging as log
from wreath._auth.models import qualified_identity_key
from wreath.auth import Identity
from wreath.authorization import AuthorizationDecision
from wreath.binding import ValidationError as BindingValidationError
from wreath.chat import (
    AgentEvent,
    ChatAdmissionError,
    ChatConfigurationError,
    ChatContext,
    ChatOps,
    ChatProgressCoalescer,
    ChatReference,
    ChatReply,
    ExternalIdentityKey,
    ExternalIdentityResolver,
    IdentityResolutionError,
    PrincipalBinding,
)
from wreath.policy import ConcurrencyPolicy
from wreath.response import ProblemDetail


def context(**values: Any) -> ChatContext:
    defaults = {
        "provider": "test",
        "installation": "installation-1",
        "tenant": "test:tenant-1",
        "actor": "actor-1",
        "conversation": "conversation-1",
        "delivery_id": "delivery-1",
        "native": {"verified": True},
    }
    defaults.update(values)
    return ChatContext(**defaults)


class Marker:
    pass


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"provider": "test", "subject": ""}, "missing-external-subject"),
        ({"subject": "user-1"}, "unconfigured-issuer"),
    ],
)
def test_external_identity_key_refuses_incomplete_identity(
    options: dict[str, str], reason: str
) -> None:
    with pytest.raises(IdentityResolutionError, match=reason):
        ExternalIdentityKey(**options)


def test_principal_binding_refuses_missing_or_conflicting_external_key() -> None:
    identity = Identity("user-1")
    first = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    second = ExternalIdentityKey(provider="slack", installation="T2", subject="U1")

    with pytest.raises(TypeError, match="requires external"):
        PrincipalBinding(identity=identity)
    with pytest.raises(ValueError, match="different users"):
        PrincipalBinding(identity=identity, external=first, key=second)
    assert PrincipalBinding(identity=identity, external=first, key=first).external is first


def test_identity_resolver_refuses_a_store_without_lookup() -> None:
    with pytest.raises(TypeError, match="lookup"):
        ExternalIdentityResolver(store=object())


class WrongBindingStore:
    async def lookup(self, _key: ExternalIdentityKey) -> tuple[object]:
        return (object(),)


async def test_identity_resolver_refuses_a_non_binding_result() -> None:
    resolver = ExternalIdentityResolver(store=WrongBindingStore())
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")

    with pytest.raises(IdentityResolutionError, match="mismatched-identity-link"):
        await resolver.resolve(key)


def test_identity_resolver_refuses_a_federation_without_resolve() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    binding = PrincipalBinding(identity=Identity("user-1"), external=key)

    class Store:
        async def lookup(self, _key: ExternalIdentityKey) -> tuple[PrincipalBinding]:
            return (binding,)

    with pytest.raises(TypeError, match="federation.*resolve"):
        ExternalIdentityResolver(store=Store(), federation=object())


@pytest.mark.parametrize("federated", [object(), None])
async def test_identity_resolver_refuses_invalid_federated_binding(
    federated: object | None,
) -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    local = PrincipalBinding(identity=Identity("user-1"), external=key)

    class Store:
        async def lookup(self, _key: ExternalIdentityKey) -> tuple[PrincipalBinding]:
            return (local,)

    class Federation:
        async def resolve(
            self, _key: ExternalIdentityKey, _binding: PrincipalBinding
        ) -> object | None:
            return federated

    resolver = ExternalIdentityResolver(store=Store(), federation=Federation())
    with pytest.raises(IdentityResolutionError, match="mismatched-federated-identity"):
        await resolver.resolve(key)


async def test_identity_resolver_refuses_federated_binding_for_another_key() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    other = ExternalIdentityKey(provider="slack", installation="T2", subject="U1")
    local = PrincipalBinding(identity=Identity("user-1"), external=key)

    class Store:
        async def lookup(self, _key: ExternalIdentityKey) -> tuple[PrincipalBinding]:
            return (local,)

    class Federation:
        async def resolve(
            self, _key: ExternalIdentityKey, _binding: PrincipalBinding
        ) -> PrincipalBinding:
            return PrincipalBinding(identity=Identity("user-1"), external=other)

    resolver = ExternalIdentityResolver(store=Store(), federation=Federation())
    with pytest.raises(IdentityResolutionError, match="mismatched-federated-identity"):
        await resolver.resolve(key)


def test_chat_admission_error_uses_detail_title_and_final_fallback() -> None:
    assert str(ChatAdmissionError(ProblemDetail(503, detail="capacity reached"))) == (
        "capacity reached"
    )
    assert str(ChatAdmissionError(ProblemDetail(503, title="Overloaded"))) == "Overloaded"
    assert str(ChatAdmissionError(ProblemDetail(503))) == "chat request refused"


def test_progress_coalescer_refuses_invalid_or_empty_flushes() -> None:
    with pytest.raises(ValueError, match="positive"):
        ChatProgressCoalescer(interval=0)
    coalescer = ChatProgressCoalescer(interval=1)
    with pytest.raises(RuntimeError, match="no chat progress"):
        coalescer.flush(now=1)
    coalescer.offer(AgentEvent.completed(), now=0.5)
    with pytest.raises(RuntimeError, match="no chat progress"):
        coalescer.flush(now=1)


def test_progress_coalescer_emits_at_interval_and_resets_pending() -> None:
    coalescer = ChatProgressCoalescer(interval=1)

    assert coalescer.offer(AgentEvent.progress("first"), now=0.5) is None
    delivery = coalescer.offer(AgentEvent.progress("second"), now=1)
    assert delivery is not None
    assert (delivery.content, delivery.mode) == ("second", "edit_original")
    with pytest.raises(RuntimeError, match="no chat progress"):
        coalescer.flush(now=1)


@pytest.mark.parametrize(
    "value",
    ["", 0, False, object()],
)
def test_chat_name_must_be_non_empty_text(value: Any) -> None:
    with pytest.raises(ChatConfigurationError, match="name"):
        ChatOps(name=value)


@pytest.mark.parametrize("provider", [object(), SimpleNamespace(name="")])
def test_provider_requires_a_non_empty_name(provider: Any) -> None:
    chat = ChatOps(name="ops")
    with pytest.raises(ChatConfigurationError, match="provider.*name"):
        chat.add(provider)


def test_conversation_store_requires_positive_retention() -> None:
    store = SimpleNamespace(retention_days=0, erase=lambda _conversation: None)
    with pytest.raises(ChatConfigurationError, match="conversation_store"):
        ChatOps(name="ops", conversation_store=store)


@pytest.mark.parametrize("option", ["admission", "rate_limit"])
def test_chat_policies_require_their_runtime_contract(option: str) -> None:
    with pytest.raises(TypeError, match=option.replace("_", "[- ]")):
        ChatOps(name="ops", **{option: object()})


async def test_startup_refuses_a_stream_owner_without_writer() -> None:
    chat = ChatOps(name="ops")

    @chat.command("stream", streams=object())
    async def stream() -> None:
        pass

    with pytest.raises(ChatConfigurationError, match="stream.*writer"):
        await chat._startup()


async def test_startup_accepts_a_stream_owner_with_writer() -> None:
    class Streams:
        async def writer(self) -> None:
            pass

    chat = ChatOps(name="ops")

    @chat.command("stream", streams=Streams())
    async def stream() -> None:
        pass

    await chat._startup()


def test_conversation_store_requires_erasure() -> None:
    store = SimpleNamespace(retention_days=30)
    with pytest.raises(ChatConfigurationError, match="conversation_store"):
        ChatOps(name="ops", conversation_store=store)


def test_conversation_store_accepts_bounded_retention_and_erasure() -> None:
    store = SimpleNamespace(retention_days=30, erase=lambda _conversation: None)
    assert ChatOps(name="ops", conversation_store=store).conversation_store is store


@dataclass
class StartupProvider:
    name: str = "test"
    started: int = 0

    async def startup(self) -> None:
        self.started += 1


async def test_provider_startup_runs_once_and_root_path_stays_root() -> None:
    provider = StartupProvider()
    chat = ChatOps(name="ops", providers=(provider,), path="/")

    await chat._startup()
    assert provider.started == 1
    assert chat.path == "/"


@pytest.mark.parametrize(
    ("jobs", "inbox"),
    [(object(), None), (None, object())],
)
async def test_durable_startup_requires_both_job_and_inbox_owners(jobs: Any, inbox: Any) -> None:
    chat = ChatOps(name="ops", jobs=jobs, inbox=inbox)

    @chat.command("run", execution="durable")
    async def run() -> None:
        pass

    with pytest.raises(RuntimeError, match="both.*jobs owner and inbox|both a JobRunner"):
        await chat._startup()


async def test_inline_startup_needs_no_durable_owners() -> None:
    chat = ChatOps(name="ops")

    @chat.command("run")
    async def run() -> None:
        pass

    await chat._startup()


async def test_durable_startup_refuses_a_non_transactional_inbox() -> None:
    chat = ChatOps(name="ops", jobs=object(), inbox=object())

    @chat.command("run", execution="durable")
    async def run() -> None:
        pass

    with pytest.raises(RuntimeError, match="transactional.*claim_and_enqueue"):
        await chat._startup()


async def test_compiled_binding_converts_literals_unions_numbers_and_booleans() -> None:
    chat = ChatOps(name="ops")
    seen: tuple[Any, ...] | None = None

    @chat.command("convert")
    def convert(
        environment: Literal["production", "staging"],
        replicas: int,
        ratio: float,
        enabled: bool,
        note: str | None,
        raw,
    ) -> str:
        nonlocal seen
        seen = (environment, replicas, ratio, enabled, note, raw)
        return "converted"

    reply = await chat._dispatch(
        kind="command",
        name="convert",
        context=context(),
        arguments={
            "environment": "production",
            "replicas": "3",
            "ratio": "0.5",
            "enabled": "off",
            "note": None,
            "raw": object(),
        },
    )

    assert seen is not None
    assert seen[:5] == ("production", 3, 0.5, False, None)
    assert reply == ChatReply.text("converted")


async def test_compiled_binding_preserves_text_and_custom_values_and_accepts_nullable_int() -> None:
    chat = ChatOps(name="ops")
    seen: tuple[Any, ...] | None = None

    marker = Marker()

    @chat.command("convert-more")
    def convert_more(text: str, marker: Marker, count: None | int, enabled: bool) -> None:
        nonlocal seen
        seen = (text, marker, count, enabled)

    await chat._dispatch(
        kind="command",
        name="convert-more",
        context=context(),
        arguments={"text": 7, "marker": marker, "count": "4", "enabled": True},
    )
    assert seen == (7, marker, 4, True)

    await chat._dispatch(
        kind="command",
        name="convert-more",
        context=context(),
        arguments={"text": "x", "marker": marker, "count": None, "enabled": "yes"},
    )
    assert seen == ("x", marker, None, True)


@pytest.mark.parametrize(
    ("annotation", "value", "message"),
    [
        (Literal["production"], "staging", "must be one of"),
        (bool, "sometimes", "boolean"),
        (int | None, "not-an-int", "invalid value"),
    ],
)
async def test_compiled_binding_refuses_invalid_typed_values(
    annotation: Any, value: Any, message: str
) -> None:
    chat = ChatOps(name="ops")

    async def handler(value: Any) -> None:
        pass

    handler.__annotations__["value"] = annotation
    chat.command("convert")(handler)

    with pytest.raises(ValueError, match=message):
        await chat._dispatch(
            kind="command",
            name="convert",
            context=context(),
            arguments={"value": value},
        )


async def test_action_claim_matches_both_tenant_and_actor() -> None:
    from wreath.chat import InMemoryChatActionStore

    store = InMemoryChatActionStore(max_entries=3, ttl=30)
    wrong_tenant = await store.issue(
        workflow="release", decision="approve", tenant="teams:T1", actor="U1"
    )
    assert await store.claim(wrong_tenant.custom_id, tenant="teams:T2", actor="U1") is None
    wrong_actor = await store.issue(
        workflow="release", decision="approve", tenant="teams:T1", actor="U1"
    )
    assert await store.claim(wrong_actor.custom_id, tenant="teams:T1", actor="U2") is None


class IdentityOwner:
    def __init__(self, binding: PrincipalBinding | None) -> None:
        self.binding = binding
        self.calls = 0

    async def resolve(self, _key: ExternalIdentityKey) -> PrincipalBinding | None:
        self.calls += 1
        return self.binding


class PlainPrincipal:
    pass


async def test_resolution_skips_context_without_external_identity() -> None:
    owner = IdentityOwner(None)
    chat = ChatOps(name="ops", identity=owner)
    assert await chat._resolve(context()) is None
    assert owner.calls == 0


async def test_resolution_preserves_context_when_owner_returns_no_binding() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    owner = IdentityOwner(None)
    chat = ChatOps(name="ops", identity=owner)
    current = context(external_identity=key)

    assert await chat._resolve(current) is None
    assert owner.calls == 1
    assert current.identity is None


async def test_resolution_refuses_a_non_binding_owner_result() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")

    class Owner:
        async def resolve(self, _key: ExternalIdentityKey) -> object:
            return object()

    chat = ChatOps(name="ops", identity=Owner())

    with pytest.raises(IdentityResolutionError, match="mismatched-identity-link"):
        await chat._resolve(context(external_identity=key))


async def test_resolution_supports_plain_principal_and_preserves_unmapped_tenant() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    identity = Identity("user-1")
    principal = PlainPrincipal()
    binding = PrincipalBinding(identity=identity, external=key, principal=principal)
    chat = ChatOps(name="ops", identity=IdentityOwner(binding))
    current = context(external_identity=key)

    assert await chat._resolve(current) is binding
    assert current.identity is identity
    assert current.principal is principal
    assert current.tenant == "test:tenant-1"


async def test_resolution_binds_composed_principal_and_replaces_tenant() -> None:
    key = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    identity = Identity("user-1")

    class ComposedPrincipal:
        def bind(self) -> Identity:
            return Identity("bound-user")

    binding = PrincipalBinding(
        identity=identity,
        external=key,
        principal=ComposedPrincipal(),
        tenant="wreath:tenant-2",
    )
    chat = ChatOps(name="ops", identity=IdentityOwner(binding))
    current = context(external_identity=key)
    await chat._resolve(current)

    assert current.identity == Identity("bound-user")
    assert current.tenant == "wreath:tenant-2"


async def test_dispatch_handles_unknown_sync_string_and_invalid_results() -> None:
    chat = ChatOps(name="ops")
    assert await chat._dispatch(kind="command", name="missing", context=context()) is None

    @chat.command("sync")
    def sync_handler() -> str:
        return "done"

    assert await chat._dispatch(kind="command", name="sync", context=context()) == ChatReply.text(
        "done"
    )

    @chat.command("invalid")
    async def invalid_handler() -> int:
        return 1

    with pytest.raises(TypeError, match="ChatReply, str, or None"):
        await chat._dispatch(kind="command", name="invalid", context=context())

    @chat.command("plural")
    async def plural() -> None:
        pass

    with pytest.raises(ValueError, match="parameters first, second"):
        await chat._dispatch(
            kind="command",
            name="plural",
            context=context(),
            arguments={"second": 2, "first": 1},
        )


async def test_binding_error_without_field_details_uses_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args: Any, **_kwargs: Any) -> Any:
        raise BindingValidationError([])

    monkeypatch.setattr(chat_core, "convert_parameter", reject)
    chat = ChatOps(name="ops")

    @chat.command("deploy")
    async def deploy(environment: int) -> None:
        pass

    with pytest.raises(ValueError, match="environment: has an invalid value"):
        await chat._dispatch(
            kind="command",
            name="deploy",
            context=context(),
            arguments={"environment": "production"},
        )


async def test_context_and_identity_injection_work_by_annotation_and_reserved_name() -> None:
    chat = ChatOps(name="ops")
    current = context(identity=Identity("user-1"))
    seen: tuple[Any, ...] | None = None

    @chat.command("inspect")
    async def inspect_values(
        chat_request: ChatContext,
        member: Identity,
        principal,
    ) -> None:
        nonlocal seen
        seen = (chat_request, member, principal)

    await chat._dispatch(kind="command", name="inspect", context=current)
    assert seen == (current, current.identity, current.identity)


@dataclass
class RecordingAuthorizer:
    decision: AuthorizationDecision
    resources: list[Any] = field(default_factory=list)

    async def authorize(self, _context: Any, requirement: Any) -> AuthorizationDecision:
        self.resources.append(requirement.resource)
        return self.decision


async def test_authorization_resolves_callable_resource_and_preserves_denial_reason() -> None:
    authorizer = RecordingAuthorizer(AuthorizationDecision(False, "maintenance window"))
    chat = ChatOps(name="ops", authorizer=authorizer)

    @chat.command("deploy", action="Release::deploy", resource=lambda value: value.tenant)
    async def deploy() -> None:
        pass

    with pytest.raises(PermissionError, match="maintenance window"):
        await chat._dispatch(kind="command", name="deploy", context=context())
    assert authorizer.resources == ["test:tenant-1"]


async def test_authorization_uses_default_refusal_and_static_resource() -> None:
    resource = object()
    authorizer = RecordingAuthorizer(AuthorizationDecision(False))
    chat = ChatOps(name="ops", authorizer=authorizer)

    @chat.command("deploy", action="Release::deploy", resource=resource)
    async def deploy() -> None:
        pass

    with pytest.raises(PermissionError, match="chat action refused"):
        await chat._dispatch(kind="command", name="deploy", context=context())
    assert authorizer.resources == [resource]


@pytest.mark.parametrize(
    ("identity", "actor", "expected_actor"),
    [
        (None, "channel-user", "channel-user"),
        (Identity("member-7"), "channel-user", "member-7"),
    ],
)
async def test_rate_limit_key_prefers_identity_then_channel_actor(
    identity: Identity | None, actor: str, expected_actor: str
) -> None:
    class RateLimit:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def admit_key(self, key: str) -> None:
            self.keys.append(key)

    rate_limit = RateLimit()
    chat = ChatOps(name="ops", rate_limit=rate_limit)

    @chat.command("deploy")
    async def deploy() -> None:
        pass

    await chat._dispatch(
        kind="command",
        name="deploy",
        context=context(identity=identity, actor=actor),
    )
    actor_key = (
        expected_actor
        if identity is None
        else qualified_identity_key("User", "", expected_actor)
    )
    assert rate_limit.keys == [
        qualified_identity_key("chat:deploy", "test:tenant-1", actor_key)
    ]


async def test_rate_limit_keys_distinguish_identity_types() -> None:
    class RateLimit:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def admit_key(self, key: str) -> None:
            self.keys.append(key)

    rate_limit = RateLimit()
    chat = ChatOps(name="ops", rate_limit=rate_limit)

    @chat.command("deploy")
    async def deploy() -> None:
        pass

    for identity_type in ("User", "Service"):
        await chat._dispatch(
            kind="command",
            name="deploy",
            context=context(identity=Identity("member-7", type=identity_type)),
        )

    assert len(set(rate_limit.keys)) == 2


async def test_chat_admission_holds_and_releases_a_successful_permit() -> None:
    admission = ConcurrencyPolicy(1)
    chat = ChatOps(name="ops", admission=admission)

    @chat.command("deploy")
    async def deploy() -> str:
        assert admission.stats().active == 1
        return "done"

    assert await chat._dispatch(
        kind="command", name="deploy", context=context()
    ) == ChatReply.text("done")
    assert admission.stats().active == 0


async def test_chat_admission_refuses_when_every_permit_is_held() -> None:
    admission = ConcurrencyPolicy(1, detail="Chat is busy")
    assert admission.try_acquire()
    chat = ChatOps(name="ops", admission=admission)
    ran = False

    @chat.command("deploy")
    async def deploy() -> None:
        nonlocal ran
        ran = True

    with pytest.raises(ChatAdmissionError) as refused:
        await chat._dispatch(kind="command", name="deploy", context=context())

    assert refused.value.problem.status == 503
    assert refused.value.problem.detail == "Chat is busy"
    assert ran is False
    admission.release()


@pytest.mark.parametrize(
    "window",
    [True, "60", 0, float("nan"), float("inf"), float("-inf")],
)
def test_chat_declarations_refuse_invalid_second_factor_windows(window: Any) -> None:
    chat = ChatOps(name="ops")

    with pytest.raises(ValueError, match="positive finite duration"):

        @chat.command("deploy", second_factor=window)
        async def deploy() -> None:
            pass


async def test_second_factor_requires_identity_and_enforces_maximum_age() -> None:
    now = 1_000.0
    chat = ChatOps(name="ops", clock=lambda: now)

    @chat.command("deploy", second_factor=60)
    async def deploy() -> str:
        return "deployed"

    with pytest.raises(PermissionError, match="second factor"):
        await chat._dispatch(kind="command", name="deploy", context=context())
    stale = context(identity=Identity("user-1", claims={"second_factor_at": 900}))
    with pytest.raises(PermissionError, match="second factor"):
        await chat._dispatch(kind="command", name="deploy", context=stale)
    fresh = context(identity=Identity("user-1", claims={"second_factor_at": 950}))
    assert await chat._dispatch(kind="command", name="deploy", context=fresh) == ChatReply.text(
        "deployed"
    )


def test_problem_maps_identity_permission_and_empty_request_errors() -> None:
    chat = ChatOps(name="ops")

    identity = chat.problem(IdentityResolutionError("missing"))
    permission = chat.problem(PermissionError())
    request = chat.problem(ValueError())
    detailed_request = chat.problem(ValueError("invalid environment"))

    assert (identity.status, identity.detail) == (401, "Link your identity to continue")
    assert (permission.status, permission.detail) == (403, "Forbidden")
    assert (request.status, request.detail) == (400, "Bad Request")
    assert (detailed_request.status, detailed_request.detail) == (
        400,
        "invalid environment",
    )


async def test_unknown_provider_uses_generic_structured_dispatch_log() -> None:
    chat = ChatOps(name="ops")

    @chat.command("status")
    async def status() -> None:
        pass

    with log.testing_runtime(level=log.INFO) as records:
        await chat._dispatch(kind="command", name="status", context=context())

    assert len(records) == 1
    assert len(records[0].args) == 2


async def test_dispatch_log_prefers_declared_action_then_command_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(chat_core.log, "active", lambda: True)
    monkeypatch.setattr(
        chat_core.log,
        "info",
        lambda template, **values: logged.append((template, values)),
    )
    chat = ChatOps(name="ops")

    @chat.command("status")
    async def status() -> None:
        pass

    @chat.command("deploy", action="Release::deploy")
    async def deploy() -> None:
        pass

    current = context(provider="slack")
    await chat._audit(current, chat.commands["status"], outcome="succeeded")
    await chat._audit(current, chat.commands["deploy"], outcome="failed")

    assert [values["action"] for _, values in logged] == ["status", "Release::deploy"]


@dataclass
class Audit:
    records: list[dict[str, Any]] = field(default_factory=list)

    async def append(self, record: Any) -> None:
        self.records.append(record.public_fields())


class AllowAuthorizer:
    async def authorize(self, *_args: Any) -> AuthorizationDecision:
        return AuthorizationDecision(True)


async def test_audit_preserves_channel_actor_external_identity_and_action() -> None:
    audit = Audit()
    authorizer = SimpleNamespace(
        authorize=lambda *_args: None,
    )
    chat = ChatOps(name="ops", audit=audit, authorizer=authorizer)

    @chat.command("inspect")
    async def inspect_command() -> None:
        pass

    external = ExternalIdentityKey(provider="slack", installation="T1", subject="U1")
    current = context(external_identity=external)
    await chat._dispatch(kind="command", name="inspect", context=current)

    assert audit.records == [
        {
            "outcome": "succeeded",
            "actor": {"id": "actor-1", "channel": "test"},
            "tenant": "test:tenant-1",
            "external_identity": {
                "provider": "slack",
                "installation": "T1",
                "issuer": None,
                "subject": "U1",
                "tenant": None,
            },
            "channel_actor_id": "actor-1",
            "action": "inspect",
        }
    ]


async def test_audit_prefers_explicit_channel_actor_and_declared_action() -> None:
    audit = Audit()
    chat = ChatOps(
        name="ops",
        audit=audit,
        authorizer=AllowAuthorizer(),
    )

    @chat.command("deploy", action="Release::deploy")
    async def deploy() -> None:
        pass

    current = SimpleNamespace(
        external_identity=None,
        identity=None,
        channel_actor_id="channel-user",
        provider="test",
        tenant="test:tenant-1",
    )
    await chat._dispatch(kind="command", name="deploy", context=current)

    assert audit.records[0]["channel_actor_id"] == "channel-user"
    assert audit.records[0]["actor"] == {"id": "channel-user", "channel": "test"}
    assert audit.records[0]["external_identity"] is None
    assert audit.records[0]["action"] == "Release::deploy"


async def test_durable_context_stringifies_non_text_prompt_and_defaults_missing_prompt() -> None:
    chat = ChatOps(name="ops")
    job = SimpleNamespace(job_id=4, trace_context=None)

    async def emit(_event: AgentEvent) -> None:
        pass

    numbered = chat._durable_context(
        context(), job_context=job, arguments={"prompt": 42}, emit=emit
    )
    assert numbered.agent_request is not None
    assert numbered.agent_request.prompt == "42"
    missing = chat._durable_context(context(), job_context=job, arguments={}, emit=emit)
    assert missing.agent_request is not None
    assert missing.agent_request.prompt == ""


def test_manifest_and_accept_refuse_an_unconfigured_provider() -> None:
    chat = ChatOps(name="ops")
    with pytest.raises(KeyError, match="slack"):
        chat.manifest("slack", base_url="https://example.test")


async def test_send_cancel_and_accept_refuse_missing_owners() -> None:
    chat = ChatOps(name="ops")
    with pytest.raises(ChatConfigurationError, match="provider 'slack'"):
        await chat.send(
            tenant="slack:T1",
            destination="C1",
            content="hello",
            idempotency_key="message-1",
        )
    with pytest.raises(ChatConfigurationError, match="cancellation"):
        await chat.cancel("job-1", reason="stop")
    with pytest.raises(ChatConfigurationError, match="Discord"):
        await chat.accept(object())


def test_matching_tenant_returns_the_same_reference() -> None:
    chat = ChatOps(name="ops")
    reference = ChatReference("discord:guild:1", "conversation-1")
    assert chat.require_tenant("discord:guild:1", reference) is reference
