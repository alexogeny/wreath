from __future__ import annotations

import hashlib
import inspect
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import (
    Any,
    Literal,
    Protocol,
    runtime_checkable,
)

from wreath import logging as log
from wreath._auth.requirements import second_factor_age
from wreath._capability_map import CapabilityMap
from wreath._json import dumps as json_dumps
from wreath.auth import Identity
from wreath.binding import (
    HandlerParameter as _Parameter,
)
from wreath.binding import (
    ValidationError as BindingValidationError,
)
from wreath.binding import (
    convert_parameter,
    inspect_parameters,
)
from wreath.exceptions import HTTPException
from wreath.response import ProblemDetail


class ChatConfigurationError(ValueError):
    pass


class ChatTenantMismatch(ValueError):
    pass


class StaleChatJobFence(RuntimeError):
    pass


class IdentityResolutionError(LookupError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ChatAdmissionError(PermissionError):
    def __init__(self, problem: ProblemDetail) -> None:
        self.problem = problem
        super().__init__(problem.detail or problem.title or "chat request refused")


_DISPATCH_TEMPLATES = {
    ("slack", "succeeded"): "Slack chat action {action} succeeded",
    ("slack", "failed"): "Slack chat action {action} failed",
    ("teams", "succeeded"): "Teams chat action {action} succeeded",
    ("teams", "failed"): "Teams chat action {action} failed",
    ("discord", "succeeded"): "Discord chat action {action} succeeded",
    ("discord", "failed"): "Discord chat action {action} failed",
}


@dataclass(frozen=True, slots=True, init=False)
class ExternalIdentityKey:
    provider: str | None
    installation: str | None
    issuer: str | None
    subject: str
    tenant: str | None

    def __init__(
        self,
        *,
        subject: str,
        provider: str | None = None,
        installation: str | None = None,
        issuer: str | None = None,
        tenant: str | None = None,
    ) -> None:
        if not subject:
            raise IdentityResolutionError("missing-external-subject")
        if provider is None and issuer is None:
            raise IdentityResolutionError("unconfigured-issuer")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "installation", installation)
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "tenant", tenant)


@dataclass(frozen=True, slots=True, init=False)
class PrincipalBinding:
    identity: Identity
    principal: Any
    tenant: str | None
    external: ExternalIdentityKey

    def __init__(
        self,
        *,
        identity: Identity,
        external: ExternalIdentityKey | None = None,
        key: ExternalIdentityKey | None = None,
        principal: Any = None,
        tenant: str | None = None,
    ) -> None:
        resolved = external if external is not None else key
        if resolved is None:
            raise TypeError("PrincipalBinding requires external=ExternalIdentityKey(...)")
        if external is not None and key is not None and external != key:
            raise ValueError("PrincipalBinding external and key identify different users")
        if principal is None:
            from wreath.authorization import human

            principal = human(identity)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "external", resolved)

    @property
    def key(self) -> ExternalIdentityKey:
        return self.external


@dataclass(frozen=True, slots=True)
class IdentityLinkChallenge:
    url: str
    state: str


class ExternalIdentityResolver:
    __slots__ = ("federation", "store")

    def __init__(self, *, store: Any, federation: Any = None) -> None:
        if not callable(getattr(store, "lookup", None)):
            raise TypeError("external identity store must provide lookup(key)")
        if federation is not None and not callable(getattr(federation, "resolve", None)):
            raise TypeError("external identity federation must provide resolve(key, binding)")
        self.store = store
        self.federation = federation

    async def resolve(self, key: ExternalIdentityKey) -> PrincipalBinding:
        matches = tuple(await self.store.lookup(key))
        if not matches:
            raise IdentityResolutionError("identity-not-linked")
        if len(matches) != 1:
            raise IdentityResolutionError("ambiguous-identity-link")
        binding = matches[0]
        if not isinstance(binding, PrincipalBinding) or binding.external != key:
            raise IdentityResolutionError("mismatched-identity-link")
        if self.federation is not None:
            try:
                binding = await self.federation.resolve(key, binding)
            except LookupError as error:
                raise IdentityResolutionError(str(error)) from None
            if not isinstance(binding, PrincipalBinding) or binding.external != key:
                raise IdentityResolutionError("mismatched-federated-identity")
        return binding


@dataclass(frozen=True, slots=True)
class ChatCorrelation:
    interaction_id: str | None = None
    job_id: str | None = None
    trace_id: str | None = None
    provider_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatReference:
    tenant: str
    id: str


@dataclass(frozen=True, slots=True)
class AgentRequest:
    tenant: str
    actor: str
    conversation: str
    prompt: str
    correlation: ChatCorrelation
    native: Any = None
    principal: Any = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: Literal["progress", "text", "completed"]
    content: str | None = None
    percent: int | None = None
    id: str | None = None

    @classmethod
    def progress(cls, content: str, *, percent: int | None = None) -> AgentEvent:
        return cls("progress", content, percent)

    @classmethod
    def text(cls, content: str, *, id: str | None = None) -> AgentEvent:
        return cls("text", content, id=id)

    @classmethod
    def completed(cls) -> AgentEvent:
        return cls("completed")


@runtime_checkable
class AgentBackend(Protocol):
    def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]: ...


@dataclass(frozen=True, slots=True)
class _ProgressDelivery:
    content: str
    mode: str = "edit_original"


class ChatProgressCoalescer:
    __slots__ = ("_interval", "_last", "_pending")

    def __init__(self, *, interval: float) -> None:
        if interval <= 0:
            raise ValueError("chat progress interval must be positive")
        self._interval = interval
        self._last = 0.0
        self._pending: AgentEvent | None = None

    def offer(self, event: AgentEvent, *, now: float) -> _ProgressDelivery | None:
        self._pending = event
        if now - self._last < self._interval:
            return None
        return self.flush(now=now)

    def flush(self, *, now: float) -> _ProgressDelivery:
        pending = self._pending
        if pending is None or pending.content is None:
            raise RuntimeError("no chat progress is pending")
        self._pending = None
        self._last = now
        return _ProgressDelivery(pending.content)


@dataclass(frozen=True, slots=True)
class ChatAuditEvent:
    tenant: str
    actor: str
    action: str
    correlation: ChatCorrelation


@dataclass(frozen=True, slots=True)
class _AuditActor:
    id: str
    channel: str


@dataclass(frozen=True, slots=True)
class _AuditRecord:
    outcome: Literal["succeeded", "failed"]
    actor: _AuditActor
    tenant: str
    external_identity: ExternalIdentityKey | None
    channel_actor_id: str
    action: str

    def public_fields(self) -> dict[str, Any]:
        external = self.external_identity
        return {
            "outcome": self.outcome,
            "actor": {"id": self.actor.id, "channel": self.actor.channel},
            "tenant": self.tenant,
            "external_identity": None
            if external is None
            else {
                "provider": external.provider,
                "installation": external.installation,
                "issuer": external.issuer,
                "subject": external.subject,
                "tenant": external.tenant,
            },
            "channel_actor_id": self.channel_actor_id,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class ChatReply:
    content: str | None = None
    visibility: Literal["default", "ephemeral", "in_channel"] = "default"
    blocks: tuple[Mapping[str, Any], ...] = ()
    native: Mapping[str, Any] = field(default_factory=dict)
    adaptive_card: Mapping[str, Any] | None = None

    @classmethod
    def text(cls, content: str) -> ChatReply:
        return cls(content=content)

    @classmethod
    def ephemeral(
        cls,
        content: str,
        *,
        blocks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        native: Mapping[str, Any] | None = None,
    ) -> ChatReply:
        return cls(content, "ephemeral", tuple(blocks), dict(native or {}))

    @classmethod
    def in_channel(
        cls,
        content: str,
        *,
        blocks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        native: Mapping[str, Any] | None = None,
    ) -> ChatReply:
        return cls(content, "in_channel", tuple(blocks), dict(native or {}))

    @classmethod
    def card(cls, value: Mapping[str, Any]) -> ChatReply:
        if value.get("type") != "AdaptiveCard" or value.get("version") != "1.5":
            raise ValueError("Adaptive Card version 1.5 is required")
        return cls(adaptive_card=dict(value))

    def for_provider(self, provider: str) -> dict[str, Any]:
        if provider == "slack":
            result: dict[str, Any] = {}
            if self.visibility != "default":
                result["response_type"] = self.visibility
            if self.content is not None:
                result["text"] = self.content
            if self.blocks:
                result["blocks"] = list(self.blocks)
            result.update(self.native)
            return result
        if provider == "teams" and self.adaptive_card is not None:
            card = dict(self.adaptive_card)
            actions = card.get("actions")
            if isinstance(actions, list):
                rendered: list[Any] = []
                for action in actions:
                    if not isinstance(action, Mapping) or action.get("type") != "Action.Execute":
                        rendered.append(action)
                        continue
                    item = dict(action)
                    if "fallback" not in item:
                        fallback_data = {"verb": item.get("verb")}
                        data = item.get("data")
                        if isinstance(data, Mapping):
                            fallback_data.update(data)
                        item["fallback"] = {
                            "type": "Action.Submit",
                            "data": fallback_data,
                            "title": item.get("title"),
                        }
                    rendered.append(item)
                card["actions"] = rendered
            return card
        if self.adaptive_card is not None:
            return dict(self.adaptive_card)
        return {"text": self.content or "", **self.native}


@dataclass(slots=True)
class ChatContext:
    provider: str
    installation: str
    tenant: str
    actor: str
    conversation: str
    delivery_id: str
    native: Any
    raw: Any = None
    command: str | None = None
    action: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    response_url: str | None = None
    identity: Identity | None = None
    principal: Any = None
    external_identity: ExternalIdentityKey | None = None
    agent_request: AgentRequest | None = None
    emit: Callable[[AgentEvent], Awaitable[None]] | None = None
    job_context: Any = None
    stream_key: str | None = None


@dataclass(frozen=True, slots=True)
class ChatDeclaration:
    name: str
    handler: Callable[..., Any]
    description: str | None
    action: str | None
    resource: Any
    second_factor: float | None
    execution: Literal["inline", "durable"]
    streams: Any
    parameters: tuple[_Parameter, ...]
    prefix: bool = False
    retries: int | None = None


@dataclass(frozen=True, slots=True)
class _AuthorizationRequirement:
    action: str
    resource: Any


@dataclass(frozen=True, slots=True)
class _IssuedAction:
    custom_id: str
    workflow: str
    decision: str
    tenant: str
    actor: str


class InMemoryChatActionStore:
    __slots__ = ("_actions",)

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        ttl: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._actions = CapabilityMap(
            max_entries=max_entries,
            ttl=ttl,
            clock=clock,
            overflow="refuse",
        )

    @property
    def size(self) -> int:
        return len(self._actions)

    async def issue(
        self, *, workflow: str, decision: str, tenant: str, actor: str
    ) -> _IssuedAction:
        action = _IssuedAction(secrets.token_urlsafe(24), workflow, decision, tenant, actor)
        if not self._actions.put(action.custom_id, action):
            raise ChatConfigurationError("chat action store reached its bounded capacity")
        return action

    async def claim(self, custom_id: str, *, tenant: str, actor: str) -> _IssuedAction | None:
        return self._actions.consume(
            custom_id,
            predicate=lambda action: action.tenant == tenant and action.actor == actor,
        )


class _HandlerBoundary:
    __slots__ = ("chat", "context", "declaration", "failed", "suppress")

    def __init__(
        self,
        chat: ChatOps,
        context: Any,
        declaration: ChatDeclaration,
        *,
        suppress: bool,
    ) -> None:
        self.chat = chat
        self.context = context
        self.declaration = declaration
        self.failed = False
        self.suppress = suppress

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, _type: Any, error: BaseException | None, _traceback: Any) -> bool:
        if error is None or not isinstance(error, Exception):
            return False
        self.chat.handler_errors += 1
        await self.chat._audit(self.context, self.declaration, outcome="failed")
        fail = getattr(getattr(self.context, "emit", None), "fail", None)
        if fail is not None:
            await fail(f"{type(error).__name__}: {error}")
        self.failed = True
        return self.suppress


class _StreamEmitter:
    __slots__ = ("_delegate", "_terminal_error", "_writer")

    def __init__(self, delegate: Any, writer: Any, *, terminal_error: bool) -> None:
        self._delegate = delegate
        self._writer = writer
        self._terminal_error = terminal_error

    async def __call__(self, event: AgentEvent) -> None:
        payload: dict[str, Any] = {"kind": event.kind}
        if event.content is not None:
            payload["content"] = event.content
        if event.percent is not None:
            payload["percent"] = event.percent
        if event.id is not None:
            payload["id"] = event.id
        await self._writer.write(json_dumps(payload))
        await self._delegate(event)

    async def finish(self, reply: ChatReply | None) -> None:
        try:
            await self._delegate.finish(reply)
        except Exception as error:
            await self.fail(f"{type(error).__name__}: {error}")
            raise
        await self._writer.finish()

    async def fail(self, detail: str) -> None:
        if self._terminal_error:
            await self._writer.fail(detail)
        else:
            self._writer.abandon()


class ChatOps:
    def __init__(
        self,
        app: Any = None,
        *,
        name: str,
        providers: tuple[Any, ...] = (),
        path: str = "/_wreath/chat",
        installations: Any = None,
        identity: Any = None,
        jobs: Any = None,
        inbox: Any = None,
        outbox: Any = None,
        authorizer: Any = None,
        audit: Any = None,
        admission: Any = None,
        rate_limit: Any = None,
        progress: Any = None,
        conversation_store: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ChatConfigurationError("chat name must not be empty")
        if app is not None and any(
            isinstance(owner, ChatOps) and getattr(owner, "name", None) == name
            for startup in getattr(app, "_startup_handlers", ())
            if (owner := getattr(startup, "__self__", None)) is not None
        ):
            raise ChatConfigurationError(f"duplicate ChatOps runtime {name!r} on one application")
        retention_days = getattr(conversation_store, "retention_days", None)
        if conversation_store is not None and (
            not isinstance(retention_days, int)
            or retention_days <= 0
            or not callable(getattr(conversation_store, "erase", None))
        ):
            raise ChatConfigurationError(
                "conversation_store requires bounded retention_days and erase(conversation)"
            )
        from wreath.webhooks import PostgresWebhookInbox

        if isinstance(inbox, PostgresWebhookInbox) and not inbox.transactional:
            raise ChatConfigurationError(
                "ChatOps PostgresWebhookInbox must be configured with session_factory, "
                "lease_owner, and positive lease_seconds"
            )
        self.name = name
        self.path = path.rstrip("/") or "/"
        self._app = app
        self.providers: dict[str, Any] = {}
        self.installations = installations
        self.identity = identity
        self.jobs = jobs
        self.inbox = inbox
        self.outbox = outbox
        self.authorizer = authorizer
        self.audit = audit
        if admission is not None and not all(
            callable(getattr(admission, member, None))
            for member in ("try_acquire", "release", "refusal")
        ):
            raise TypeError(
                "chat admission policy must provide try_acquire(), release(), and refusal()"
            )
        if rate_limit is not None and not callable(getattr(rate_limit, "admit_key", None)):
            raise TypeError("chat rate-limit policy must provide admit_key(key)")
        self.admission = admission
        self.rate_limit = rate_limit
        self.progress = progress
        self.conversation_store = conversation_store
        self.clock = clock
        self.commands: dict[str, ChatDeclaration] = {}
        self.events: dict[str, ChatDeclaration] = {}
        self.actions: dict[str, ChatDeclaration] = {}
        self._action_prefixes: list[ChatDeclaration] = []
        self.handler_errors = 0
        from wreath.webhooks import LocalReplayStore

        self._local_replay = LocalReplayStore(max_entries=4096, ttl=600.0)
        for provider in providers:
            self.add(provider)
        if app is not None:
            app.on_startup(self._startup)

    @property
    def replay_size(self) -> int:
        if isinstance(self.inbox, type(self._local_replay)):
            return self.inbox.size
        return self._local_replay.size

    def add(self, provider: Any) -> Any:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not name:
            raise ChatConfigurationError("chat provider requires a non-empty name")
        if name in self.providers:
            raise ChatConfigurationError(f"duplicate chat provider {name!r}")
        self.providers[name] = provider
        if self._app is not None:
            provider._mount(self, self._app, self.path)
        register = getattr(provider, "_register_command", None)
        if register is not None:
            for declaration in self.commands.values():
                register(self, declaration)
        return provider

    def _declare(
        self,
        registry: dict[str, ChatDeclaration],
        name: str,
        *,
        description: str | None,
        action: str | None,
        resource: Any,
        second_factor: float | None,
        execution: Literal["inline", "durable"],
        streams: Any,
        prefix: bool = False,
        retries: int | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if not name or name.startswith("/"):
            rendered = name.removeprefix("/")
            raise ValueError(f"declare {rendered!r} without a slash in the provider-neutral name")
        if name in registry:
            raise ValueError(f"duplicate chat declaration {name!r}")
        if execution not in ("inline", "durable"):
            raise ValueError("chat execution must be 'inline' or 'durable'")
        if second_factor is not None and (isinstance(second_factor, bool) or second_factor <= 0):
            raise ValueError("chat second_factor must be a positive duration")
        if retries is not None and (
            isinstance(retries, bool) or not isinstance(retries, int) or retries < 0
        ):
            raise ValueError("chat retries must be a non-negative integer")
        if retries is not None and execution != "durable":
            raise ValueError("chat retries are only valid for durable commands")
        if prefix:
            if registry is not self.actions:
                raise ValueError("only chat actions may use prefix matching")
            for declared in self._action_prefixes:
                if name.startswith(declared.name) or declared.name.startswith(name):
                    raise ValueError(
                        f"chat action prefix {name!r} overlaps declared prefix {declared.name!r}"
                    )

        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            try:
                parameters = inspect_parameters(handler)
            except TypeError as error:
                raise ValueError(str(error)) from None
            declaration = ChatDeclaration(
                name,
                handler,
                description,
                action,
                resource,
                second_factor,
                execution,
                streams,
                parameters,
                prefix,
                retries,
            )
            registry[name] = declaration
            if prefix:
                self._action_prefixes.append(declaration)
            if registry is self.commands:
                for provider in self.providers.values():
                    register = getattr(provider, "_register_command", None)
                    if register is not None:
                        register(self, declaration)
            return handler

        return decorate

    def command(
        self,
        name: str,
        *,
        description: str | None = None,
        action: str | None = None,
        resource: Any = None,
        second_factor: float | None = None,
        execution: Literal["inline", "durable"] = "inline",
        streams: Any = None,
        retries: int | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._declare(
            self.commands,
            name,
            description=description,
            action=action,
            resource=resource,
            second_factor=second_factor,
            execution=execution,
            streams=streams,
            prefix=False,
            retries=retries,
        )

    def agent(
        self,
        name: str,
        backend: AgentBackend,
        *,
        action: str,
        description: str | None = None,
        resource: Any = "conversation",
        second_factor: float | None = None,
        streams: Any = None,
    ) -> Callable[..., Any]:
        if not isinstance(backend, AgentBackend):
            raise TypeError("chat agent backend must implement run(request)")
        if not action:
            raise ValueError("chat agent action must be non-empty")

        async def invoke(context: Any, prompt: str) -> None:
            del prompt
            async for event in backend.run(context.agent_request):
                await context.emit(event)

        self.command(
            name,
            description=description,
            action=action,
            resource=resource,
            second_factor=second_factor,
            execution="durable",
            streams=streams,
            retries=0,
        )(invoke)
        return invoke

    def event(
        self, name: str, *, description: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._declare(
            self.events,
            name,
            description=description,
            action=None,
            resource=None,
            second_factor=None,
            execution="inline",
            streams=None,
            retries=None,
        )

    def action(
        self,
        name: str,
        *,
        description: str | None = None,
        prefix: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._declare(
            self.actions,
            name,
            description=description,
            action=None,
            resource=None,
            second_factor=None,
            execution="inline",
            streams=None,
            prefix=prefix,
            retries=None,
        )

    async def _startup(self, _app: Any = None) -> None:
        durable = any(item.execution == "durable" for item in self.commands.values())
        if durable and (self.jobs is None or self.inbox is None):
            raise RuntimeError(
                "durable chat commands require both a JobRunner jobs owner and inbox"
            )
        if durable and not callable(getattr(self.inbox, "claim_and_enqueue", None)):
            raise RuntimeError(
                "durable chat commands require a transactional inbox with claim_and_enqueue(...)"
            )
        for declaration in self.commands.values():
            if declaration.streams is not None and not callable(
                getattr(declaration.streams, "writer", None)
            ):
                raise ChatConfigurationError(
                    f"chat command {declaration.name!r} streams must provide writer(...)"
                )
            if declaration.action is not None and self.authorizer is None:
                raise ChatConfigurationError(
                    f"chat command {declaration.name!r} declares an action but has no authorizer"
                )
        for provider in self.providers.values():
            startup = getattr(provider, "startup", None)
            if startup is not None:
                await startup()

    def _inbox_envelope(
        self,
        *,
        delivery: str,
        body: bytes,
        event_type: str,
        sent_at: float | None,
    ) -> Any:
        from wreath.webhooks import WebhookEnvelope

        timestamp = self.clock() if sent_at is None else sent_at
        return WebhookEnvelope(
            id=delivery,
            type=event_type,
            version="1",
            timestamp=datetime.fromtimestamp(timestamp, UTC),
            content_type="application/json",
            body=body,
        )

    async def _claim(
        self,
        *,
        provider: str,
        installation: str,
        delivery: str,
        body: bytes = b"{}",
        event_type: str = "interaction",
        sent_at: float | None = None,
    ) -> bool:
        if self.inbox is None:
            return await self._local_replay.claim(f"{provider}:{installation}", delivery)
        if callable(getattr(self.inbox, "claim_and_enqueue", None)):

            async def accepted(*, transaction: Any) -> None:
                return None

            return await self._claim_and_enqueue(
                provider=provider,
                installation=installation,
                delivery=delivery,
                body=body,
                event_type=event_type,
                sent_at=sent_at,
                result_status=200,
                enqueue=accepted,
            )
        claim = getattr(self.inbox, "claim", None)
        if claim is None:
            raise ChatConfigurationError("chat inbox must provide claim(...)")
        if isinstance(self.inbox, type(self._local_replay)):
            return await claim(f"{provider}:{installation}", delivery)
        return bool(await claim(provider=provider, installation=installation, delivery=delivery))

    async def _claim_and_enqueue(
        self,
        *,
        provider: str,
        installation: str,
        delivery: str,
        body: bytes,
        event_type: str,
        sent_at: float | None,
        result_status: int,
        enqueue: Callable[..., Awaitable[Any]],
    ) -> bool:
        atomic = getattr(self.inbox, "claim_and_enqueue", None)
        if not callable(atomic):
            raise ChatConfigurationError(
                "durable chat inbox must provide transactional claim_and_enqueue(...)"
            )
        envelope = self._inbox_envelope(
            delivery=delivery,
            body=body,
            event_type=event_type,
            sent_at=sent_at,
        )
        return bool(
            await atomic(
                source=f"{provider}:{installation}",
                envelope=envelope,
                result_status=result_status,
                enqueue=enqueue,
            )
        )

    async def _resolve(self, context: Any) -> PrincipalBinding | None:
        key = getattr(context, "external_identity", None)
        if self.identity is None or key is None:
            return None
        binding = await self.identity.resolve(key)
        if binding is None:
            return None
        if not isinstance(binding, PrincipalBinding) or binding.external != key:
            raise IdentityResolutionError("mismatched-identity-link")
        bind = getattr(binding.principal, "bind", None)
        context.identity = bind() if bind is not None else binding.identity
        context.principal = binding.principal
        if binding.tenant is not None:
            context.tenant = binding.tenant
        return binding

    def _durable_context(
        self,
        context: ChatContext,
        *,
        job_context: Any,
        arguments: Mapping[str, Any],
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> ChatContext:
        context.job_context = job_context
        context.emit = emit
        prompt = str(arguments.get("prompt", ""))
        context.agent_request = AgentRequest(
            tenant=context.tenant,
            actor=context.actor,
            conversation=context.conversation,
            prompt=prompt,
            correlation=ChatCorrelation(
                interaction_id=context.delivery_id,
                job_id=str(job_context.job_id),
                trace_id=getattr(job_context, "trace_context", None),
            ),
            native=context.native,
            principal=context.principal,
        )
        return context

    async def _stream_emitter(
        self,
        declaration: ChatDeclaration,
        context: ChatContext,
        job_context: Any,
        delegate: Any,
    ) -> Any:
        streams = declaration.streams
        if streams is None:
            return delegate
        digest = hashlib.sha256()
        for value in (context.provider, context.installation, context.delivery_id):
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        context.stream_key = f"chat:{digest.hexdigest()}"
        writer = await streams.writer(
            context.stream_key,
            fence=job_context.fence,
            attempt=job_context.attempt,
        )
        retries = declaration.retries
        terminal_error = retries is not None and job_context.attempt >= retries + 1
        return _StreamEmitter(delegate, writer, terminal_error=terminal_error)

    def _declaration(
        self,
        kind: Literal["command", "event", "action"],
        name: str,
    ) -> ChatDeclaration | None:
        registry = {
            "command": self.commands,
            "event": self.events,
            "action": self.actions,
        }[kind]
        declaration = registry.get(name)
        if declaration is None and kind == "action":
            declaration = next(
                (item for item in self._action_prefixes if name.startswith(item.name)),
                None,
            )
        return declaration

    async def _authorize_declaration(self, context: Any, declaration: ChatDeclaration) -> None:
        identity = getattr(context, "identity", None)
        if declaration.second_factor is not None:
            factor_age = second_factor_age(identity, self.clock())
            if factor_age is None or factor_age > declaration.second_factor:
                await self._audit(context, declaration, outcome="failed")
                raise PermissionError("second factor required")
        if declaration.action is None:
            return
        if self.authorizer is None:
            await self._audit(context, declaration, outcome="failed")
            raise PermissionError("chat action requires an authorizer")
        resource = declaration.resource
        if callable(resource):
            resource = resource(context)
        decision = await self.authorizer.authorize(
            context, _AuthorizationRequirement(declaration.action, resource)
        )
        if not decision.allowed:
            await self._audit(context, declaration, outcome="failed")
            raise PermissionError(decision.reason or "chat action refused")

    async def _dispatch(
        self,
        *,
        kind: Literal["command", "event", "action"],
        name: str,
        context: Any,
        arguments: Mapping[str, Any] | None = None,
        resolved: bool = False,
    ) -> ChatReply | None:
        declaration = self._declaration(kind, name)
        if declaration is None:
            return None
        if not resolved:
            await self._resolve(context)
        agent_request = getattr(context, "agent_request", None)
        if agent_request is not None:
            context.agent_request = replace(
                agent_request,
                tenant=context.tenant,
                principal=context.principal,
            )
        permit = await self._admit(context, declaration)
        try:
            return await self._activate(context, declaration, arguments)
        finally:
            if permit:
                self.admission.release()

    async def _admit(self, context: Any, declaration: ChatDeclaration) -> bool:
        if self.rate_limit is not None:
            identity = getattr(context, "identity", None)
            actor = getattr(identity, "id", None) or getattr(context, "actor", "anonymous")
            key = ":".join(
                (
                    "chat",
                    str(getattr(context, "tenant", "")),
                    str(actor),
                    declaration.name,
                )
            )
            refusal = await self.rate_limit.admit_key(key)
            if refusal is not None:
                await self._audit(context, declaration, outcome="failed")
                raise ChatAdmissionError(ProblemDetail(429, detail="Rate limit exceeded"))
        if self.admission is None:
            return False
        if not self.admission.try_acquire():
            refusal = self.admission.refusal()
            await self._audit(context, declaration, outcome="failed")
            raise ChatAdmissionError(
                ProblemDetail(
                    refusal.status,
                    detail=str(getattr(self.admission, "detail", "Service unavailable")),
                )
            )
        return True

    async def _activate(
        self,
        context: Any,
        declaration: ChatDeclaration,
        arguments: Mapping[str, Any] | None,
    ) -> ChatReply | None:
        identity = getattr(context, "identity", None)
        await self._authorize_declaration(context, declaration)
        supplied = dict(arguments or {})
        keywords: dict[str, Any] = {}
        for parameter in declaration.parameters:
            annotation = parameter.annotation
            if annotation is ChatContext or (
                parameter.name in {"request", "context", "command", "event", "interaction"}
            ):
                keywords[parameter.name] = context
                continue
            if annotation is Identity or parameter.name == "principal":
                if identity is None:
                    raise IdentityResolutionError("identity-not-linked")
                keywords[parameter.name] = identity
                continue
            if parameter.name in supplied:
                try:
                    keywords[parameter.name] = convert_parameter(
                        annotation,
                        supplied.pop(parameter.name),
                        loc=("chat", parameter.name),
                    )
                except BindingValidationError as error:
                    message = error.errors[0]["msg"] if error.errors else "has an invalid value"
                    raise ValueError(
                        f"invalid chat command parameter {parameter.name}: {message}"
                    ) from None
                continue
            if parameter.default is not inspect.Parameter.empty:
                continue
            raise ValueError(f"missing chat command parameter {parameter.name}")
        if supplied:
            names = ", ".join(sorted(supplied))
            noun = "parameter" if len(supplied) == 1 else "parameters"
            raise ValueError(f"unexpected chat command {noun} {names}")
        boundary = _HandlerBoundary(
            self,
            context,
            declaration,
            suppress=declaration.execution == "inline",
        )
        result: Any = None
        async with boundary:
            result = declaration.handler(**keywords)
            if inspect.isawaitable(result):
                result = await result
        if boundary.failed:
            return None
        await self._audit(context, declaration, outcome="succeeded")
        if result is None or isinstance(result, ChatReply):
            return result
        if isinstance(result, str):
            return ChatReply.text(result)
        raise TypeError("chat handlers must return ChatReply, str, or None")

    def problem(self, error: BaseException) -> ProblemDetail:
        if isinstance(error, ChatAdmissionError):
            return error.problem
        if isinstance(error, HTTPException):
            return ProblemDetail(error.status, detail=error.detail)
        if isinstance(error, IdentityResolutionError):
            return ProblemDetail(401, detail="Link your identity to continue")
        if isinstance(error, PermissionError):
            return ProblemDetail(403, detail=str(error) or "Forbidden")
        if isinstance(error, (BindingValidationError, TypeError, ValueError)):
            return ProblemDetail(400, detail=str(error) or "Bad Request")
        return ProblemDetail(500, detail="Internal Server Error")

    async def _audit(
        self,
        context: Any,
        declaration: ChatDeclaration,
        *,
        outcome: Literal["succeeded", "failed"],
    ) -> None:
        if log.active():
            provider = str(getattr(context, "provider", "chat"))
            template = _DISPATCH_TEMPLATES.get((provider, outcome))
            if template is None:
                log.info(
                    (
                        "chat provider {provider} action {action} succeeded"
                        if outcome == "succeeded"
                        else "chat provider {provider} action {action} failed"
                    ),
                    provider=provider,
                    action=declaration.action or declaration.name,
                )
            else:
                log.info(template, action=declaration.action or declaration.name)
        if self.audit is None:
            return
        identity = getattr(context, "identity", None)
        channel_actor_id = getattr(context, "channel_actor_id", None)
        if channel_actor_id is None:
            channel_actor_id = getattr(context, "actor", "")
            channel_actor_id = getattr(channel_actor_id, "id", channel_actor_id)
        actor_id = getattr(identity, "id", None) or str(channel_actor_id)
        channel = str(getattr(context, "provider", "teams"))
        await self.audit.append(
            _AuditRecord(
                outcome=outcome,
                actor=_AuditActor(actor_id, channel),
                tenant=str(getattr(context, "tenant", "")),
                external_identity=getattr(context, "external_identity", None),
                channel_actor_id=str(channel_actor_id),
                action=declaration.action or declaration.name,
            )
        )

    def manifest(self, provider: str, *, base_url: str) -> Any:
        selected = self.providers.get(provider)
        if selected is None:
            raise KeyError(provider)
        return selected.manifest(self, base_url=base_url)

    async def accept(self, interaction: Any) -> Any:
        provider = self.providers.get("discord")
        if provider is None:
            raise ChatConfigurationError("Discord is not configured")
        return await provider.accept(self, interaction)

    async def claim_action(self, interaction: Any, *, store: Any) -> Any:
        actor = interaction.actor
        return await store.claim(
            interaction.custom_id,
            tenant=interaction.tenant,
            actor=getattr(actor, "id", actor),
        )

    async def cancel(self, job_id: str, *, reason: str) -> bool:
        if self.jobs is None:
            raise ChatConfigurationError("chat cancellation requires jobs")
        return bool(await self.jobs.cancel(key=job_id, reason=reason))

    async def send(
        self,
        *,
        tenant: str,
        destination: Any,
        content: str,
        idempotency_key: str,
    ) -> Any:
        provider_name = tenant.partition(":")[0]
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ChatConfigurationError(f"chat provider {provider_name!r} is not configured")
        return await provider.send(
            tenant=tenant,
            destination=destination,
            content=content,
            idempotency_key=idempotency_key,
        )

    def require_tenant(self, tenant: str, reference: ChatReference) -> ChatReference:
        if tenant != reference.tenant:
            raise ChatTenantMismatch(
                f"chat reference belongs to {reference.tenant!r}, not {tenant!r}"
            )
        return reference
