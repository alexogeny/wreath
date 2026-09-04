from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin
from urllib.parse import quote

from .._auth._ecverify import verify_ed25519
from .._json import dumps, loads
from ..request import Request
from ..response import JSONResponse, Response
from ._core import ExternalIdentityKey, IdentityResolutionError, StaleChatJobFence

__all__ = [
    "Discord",
    "DiscordCommand",
    "DiscordComponent",
    "DiscordConfigurationError",
    "DiscordDestination",
    "DiscordInstallation",
    "DiscordInteraction",
    "DiscordInteractionVerifier",
    "DiscordManifest",
    "DiscordMessage",
    "DiscordModal",
    "DiscordNativeDelivery",
    "DiscordOption",
    "DiscordRateLimiter",
    "DiscordResponder",
    "DiscordSyncError",
    "DiscordTenantKey",
    "InteractionAcknowledgementExpired",
    "InteractionAlreadyAcknowledged",
    "InteractionTokenExpired",
    "InvalidDiscordSignature",
    "UnsupportedDiscordField",
    "UnsupportedDiscordInteraction",
]


class DiscordConfigurationError(ValueError):
    pass


class InvalidDiscordSignature(ValueError):
    pass


class UnsupportedDiscordInteraction(ValueError):
    pass


class UnsupportedDiscordField(ValueError):
    pass


class InteractionAlreadyAcknowledged(RuntimeError):
    pass


class InteractionAcknowledgementExpired(RuntimeError):
    pass


class InteractionTokenExpired(RuntimeError):
    pass


class DiscordSyncError(RuntimeError):
    pass


class DiscordInteractionVerifier:
    __slots__ = ("_clock", "_max_age", "_public_key", "_verify")

    def __init__(
        self,
        public_key: bytes | str,
        *,
        verify: Callable[[bytes, bytes, bytes], bool] = verify_ed25519,
        clock: Callable[[], float] = time.time,
        max_age: float | None = None,
    ) -> None:
        if isinstance(public_key, str):
            try:
                public_key = bytes.fromhex(public_key)
            except ValueError as exc:
                raise DiscordConfigurationError(
                    "Discord public_key must be 32 raw bytes or hex"
                ) from exc
        if len(public_key) != 32:
            raise DiscordConfigurationError("Discord public_key must be exactly 32 bytes")
        if max_age is not None and (not math.isfinite(max_age) or max_age <= 0):
            raise DiscordConfigurationError("Discord max_age must be positive and finite")
        self._public_key = bytes(public_key)
        self._verify = verify
        self._clock = clock
        self._max_age = max_age

    def verify(self, *, signature: str, timestamp: str, body: bytes) -> None:
        try:
            raw_signature = bytes.fromhex(signature)
            signed = timestamp.encode("ascii") + body
        except (AttributeError, TypeError, UnicodeEncodeError, ValueError) as exc:
            raise InvalidDiscordSignature("invalid Discord Ed25519 signature") from exc
        if len(raw_signature) != 64 or not timestamp:
            raise InvalidDiscordSignature("invalid Discord Ed25519 signature")
        if not self._verify(self._public_key, signed, raw_signature):
            raise InvalidDiscordSignature("invalid Discord Ed25519 signature")
        if self._max_age is not None:
            try:
                sent_at = int(timestamp)
            except ValueError as exc:
                raise InvalidDiscordSignature("invalid Discord signature timestamp") from exc
            if abs(self._clock() - sent_at) > self._max_age:
                raise InvalidDiscordSignature(
                    "Discord signature timestamp is outside the allowed age"
                )


class DiscordActor(str):
    __slots__ = ()

    @property
    def id(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class DiscordInstallation:
    kind: Literal["guild", "user"]
    owner_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"guild", "user"}:
            raise ValueError("Discord installation kind must be 'guild' or 'user'")
        if not self.owner_id:
            raise ValueError("Discord installation owner_id cannot be empty")


class DiscordTenantKey:
    @staticmethod
    def from_installation(installation: DiscordInstallation) -> str:
        return f"discord:{installation.kind}:{installation.owner_id}"

    @classmethod
    def from_interaction(cls, interaction: DiscordInteraction) -> str:
        return cls.from_installation(interaction.installation)


@dataclass(frozen=True, slots=True)
class DiscordInvokedCommand:
    name: str
    path: tuple[str, ...]
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DiscordComponent:
    custom_id: str
    component_type: int
    message_id: str | None
    values: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscordModal:
    custom_id: str
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DiscordInteraction:
    id: str
    application_id: str
    token: str = field(repr=False)
    kind: str
    actor: DiscordActor
    installation: DiscordInstallation
    native: Mapping[str, Any] = field(repr=False)
    command: DiscordInvokedCommand | None = None
    component: DiscordComponent | None = None
    modal: DiscordModal | None = None

    @property
    def tenant(self) -> str:
        return DiscordTenantKey.from_installation(self.installation)

    @property
    def custom_id(self) -> str:
        if self.component is not None:
            return self.component.custom_id
        if self.modal is not None:
            return self.modal.custom_id
        return ""

    @classmethod
    def parse(
        cls, payload: Mapping[str, Any], *, allow_unknown: bool = False
    ) -> DiscordInteraction:
        interaction_type = payload.get("type")
        kind = {1: "ping", 2: "command", 3: "component", 4: "autocomplete", 5: "modal"}.get(
            interaction_type
        )
        if kind is None:
            if not allow_unknown:
                raise UnsupportedDiscordInteraction(
                    f"unsupported Discord interaction type {interaction_type!r}"
                )
            kind = "native"
        if kind != "ping":
            for field_name in ("id", "application_id", "token"):
                if payload.get(field_name) in (None, ""):
                    raise UnsupportedDiscordInteraction(
                        f"Discord interaction requires non-empty {field_name}"
                    )
        actor = DiscordActor(_actor_id(payload))
        installation = _installation(payload)
        data = payload.get("data")
        data = data if isinstance(data, Mapping) else {}
        command = None
        component = None
        modal = None
        if kind in {"command", "autocomplete"}:
            path, options = _command_options(data.get("options"))
            command = DiscordInvokedCommand(str(data.get("name", "")), path, options)
        elif kind == "component":
            message = payload.get("message")
            message_id = str(message.get("id")) if isinstance(message, Mapping) else None
            values = data.get("values")
            component = DiscordComponent(
                custom_id=str(data.get("custom_id", "")),
                component_type=int(data.get("component_type", 0)),
                message_id=message_id,
                values=tuple(values)
                if isinstance(values, Sequence) and not isinstance(values, str)
                else (),
            )
        elif kind == "modal":
            modal = DiscordModal(
                custom_id=str(data.get("custom_id", "")),
                values=_modal_values(data.get("components")),
            )
        return cls(
            id=str(payload.get("id", "")),
            application_id=str(payload.get("application_id", "")),
            token=str(payload.get("token", "")),
            kind=kind,
            actor=actor,
            installation=installation,
            native=payload,
            command=command,
            component=component,
            modal=modal,
        )


def _actor_id(payload: Mapping[str, Any]) -> str:
    member = payload.get("member")
    if isinstance(member, Mapping):
        user = member.get("user")
        if isinstance(user, Mapping) and user.get("id") is not None:
            return str(user["id"])
    user = payload.get("user")
    if isinstance(user, Mapping) and user.get("id") is not None:
        return str(user["id"])
    return ""


def _installation(payload: Mapping[str, Any]) -> DiscordInstallation:
    owners = payload.get("authorizing_integration_owners")
    if isinstance(owners, Mapping):
        context = payload.get("context")
        if context == 0 and owners.get("0") not in (None, ""):
            owner = owners["0"]
            if owner == "0" and payload.get("guild_id") is not None:
                owner = payload["guild_id"]
            return DiscordInstallation("guild", str(owner))
        if owners.get("1") not in (None, ""):
            return DiscordInstallation("user", str(owners["1"]))
        if owners.get("0") not in (None, ""):
            owner = owners["0"]
            if owner == "0" and payload.get("guild_id") is not None:
                owner = payload["guild_id"]
            return DiscordInstallation("guild", str(owner))
    guild_id = payload.get("guild_id")
    if guild_id is not None:
        return DiscordInstallation("guild", str(guild_id))
    return DiscordInstallation("user", _actor_id(payload))


def _command_options(raw: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    path: list[str] = []
    current = raw
    while isinstance(current, Sequence) and not isinstance(current, str):
        nested = next(
            (item for item in current if isinstance(item, Mapping) and item.get("type") in {1, 2}),
            None,
        )
        if nested is None:
            break
        path.append(str(nested.get("name", "")))
        current = nested.get("options")
    options: dict[str, Any] = {}
    if isinstance(current, Sequence) and not isinstance(current, str):
        for item in current:
            if isinstance(item, Mapping) and "value" in item:
                options[str(item.get("name", ""))] = item["value"]
    return tuple(path), options


def _modal_values(raw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    stack = list(raw) if isinstance(raw, Sequence) and not isinstance(raw, str) else []
    while stack:
        item = stack.pop()
        if not isinstance(item, Mapping):
            continue
        component = item.get("component")
        if isinstance(component, Mapping):
            stack.append(component)
        children = item.get("components")
        if isinstance(children, Sequence) and not isinstance(children, str):
            stack.extend(children)
        if item.get("custom_id") is not None and "value" in item:
            values[str(item.get("custom_id"))] = item.get("value")
    return values


class DiscordResponder:
    __slots__ = (
        "_acknowledged",
        "_application_id",
        "_client",
        "_clock",
        "_component",
        "_interaction_id",
        "_rate_limiter",
        "_received_at",
        "_token",
    )

    def __init__(
        self,
        *,
        application_id: str,
        interaction_id: str,
        token: str,
        received_at: float,
        client: Any,
        clock: Callable[[], float] = time.monotonic,
        acknowledged: bool = False,
        component: bool = False,
        rate_limiter: DiscordRateLimiter | None = None,
    ) -> None:
        self._application_id = quote(application_id, safe="")
        self._interaction_id = interaction_id
        self._token = quote(token, safe="")
        self._received_at = received_at
        self._client = client
        self._clock = clock
        self._acknowledged = acknowledged
        self._component = component
        self._rate_limiter = rate_limiter if rate_limiter is not None else DiscordRateLimiter()

    @classmethod
    def for_component(cls, **options: Any) -> DiscordResponder:
        return cls(**options, component=True)

    async def defer(self, *, ephemeral: bool = False) -> dict[str, Any]:
        self._acknowledge()
        response: dict[str, Any] = {"type": 5}
        if ephemeral:
            response["data"] = {"flags": 64}
        return response

    async def defer_update(self) -> dict[str, Any]:
        if not self._component:
            raise UnsupportedDiscordInteraction("defer_update requires a component interaction")
        self._acknowledge()
        return {"type": 6}

    async def edit_original(self, **message: Any) -> Any:
        self._require_token()
        return await self._request(
            "PATCH",
            f"/webhooks/{self._application_id}/{self._token}/messages/@original",
            message,
            route="PATCH /webhooks/{application_id}/{token}/messages/@original",
        )

    async def followup(self, *, ephemeral: bool = False, **message: Any) -> Any:
        self._require_token()
        if ephemeral:
            message["flags"] = 64
        return await self._request(
            "POST",
            f"/webhooks/{self._application_id}/{self._token}",
            message,
            route="POST /webhooks/{application_id}/{token}",
        )

    async def edit_followup(self, message_id: str, **message: Any) -> Any:
        self._require_token()
        message_segment = quote(message_id, safe="")
        return await self._request(
            "PATCH",
            f"/webhooks/{self._application_id}/{self._token}/messages/{message_segment}",
            message,
            route="PATCH /webhooks/{application_id}/{token}/messages/{message_id}",
        )

    async def _request(self, method: str, path: str, message: Any, *, route: str) -> Any:
        major = f"{self._application_id}:{self._token}"
        await self._rate_limiter.acquire(route, major)
        response = await _request_json(self._client, method, path, message)
        await self._rate_limiter.observe(route, major, response)
        return response

    def _acknowledge(self) -> None:
        if self._acknowledged:
            raise InteractionAlreadyAcknowledged(
                f"Discord interaction {self._interaction_id} was already acknowledged"
            )
        if self._clock() - self._received_at > 3.0:
            raise InteractionAcknowledgementExpired(
                f"Discord interaction {self._interaction_id} exceeded its 3 second deadline"
            )
        self._acknowledged = True

    def _require_token(self) -> None:
        if not self._acknowledged:
            raise InteractionAlreadyAcknowledged("Discord interaction has not been acknowledged")
        if self._clock() - self._received_at > 900.0:
            raise InteractionTokenExpired(
                "Discord interaction token exceeded its 15 minute lifetime"
            )


@dataclass(slots=True)
class _RateWindow:
    deadline: float


class DiscordRateLimiter:
    __slots__ = ("_capacity", "_clock", "_global", "_route_buckets", "_sleep", "_windows")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        capacity: int = 4096,
    ) -> None:
        if capacity <= 0:
            raise ValueError("Discord rate-limit capacity must be positive")
        self._clock = clock
        self._sleep = sleep
        self._capacity = capacity
        self._route_buckets: dict[str, str] = {}
        self._windows: dict[tuple[str, str | None], _RateWindow] = {}
        self._global: _RateWindow | None = None

    async def acquire(self, route: str, major: str | None) -> None:
        now = self._clock()
        self._prune(now)
        deadlines = [self._global.deadline] if self._global is not None else []
        bucket = self._route_buckets.get(route, route)
        route_window = self._windows.get((bucket, major))
        if route_window is not None:
            deadlines.append(route_window.deadline)
        delay = max(deadlines, default=now) - now
        if delay > 0:
            await self._sleep(delay)

    async def observe(self, route: str, major: str | None, response: Any) -> None:
        raw_headers = response.headers
        items = raw_headers.items() if isinstance(raw_headers, Mapping) else raw_headers
        headers = {_header_text(key).lower(): _header_text(value) for key, value in items}
        now = self._clock()
        retry_after = _number(headers.get("retry-after"))
        body = await _response_json(response)
        if not isinstance(body, Mapping):
            body = {}
        if retry_after is None:
            retry_after = _number(body.get("retry_after"))
        global_limit = str(headers.get("x-ratelimit-global", "")).lower() == "true" or bool(
            body.get("global")
        )
        remaining = _number(headers.get("x-ratelimit-remaining"))
        reset_after = _number(headers.get("x-ratelimit-reset-after"))
        bucket = headers.get("x-ratelimit-bucket")
        if response.status == 429:
            if retry_after is None:
                return
            if global_limit:
                self._global = _RateWindow(now + retry_after)
                return
            identity = bucket or self._route_buckets.get(route, route)
            self._set_window(identity, major, now + retry_after)
            return
        match bucket:
            case str():
                pass
            case _:
                return
        self._route_buckets[route] = bucket
        if remaining is None:
            return
        if remaining > 0:
            return
        if reset_after is None:
            return
        self._set_window(bucket, major, now + reset_after)

    def _set_window(self, bucket: str, major: str | None, deadline: float) -> None:
        key = (bucket, major)
        if key not in self._windows and len(self._windows) >= self._capacity:
            oldest = min(self._windows, key=lambda item: self._windows[item].deadline)
            del self._windows[oldest]
        self._windows[key] = _RateWindow(deadline)

    def _prune(self, now: float) -> None:
        expired = [key for key, window in self._windows.items() if window.deadline <= now]
        for key in expired:
            del self._windows[key]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _header_text(value: Any) -> str:
    return value.decode("latin-1") if isinstance(value, bytes) else str(value)


async def _request_json(
    client: Any,
    method: str,
    path: str,
    value: Any,
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    idempotency_key: str | None = None,
) -> Any:
    encoded_headers = (*headers, (b"content-type", b"application/json"))
    body = dumps(value)
    if idempotency_key is None:
        return await client.request(method, path, headers=encoded_headers, body=body)
    return await client.request(
        method,
        path,
        headers=encoded_headers,
        body=body,
        idempotency_key=idempotency_key,
    )


async def _response_json(response: Any) -> Any:
    value = getattr(response, "json", None)
    if callable(value):
        value = value()
        if inspect.isawaitable(value):
            value = await value
    if value is not None and not callable(value):
        return value
    body = getattr(response, "body", b"")
    if isinstance(body, bytes) and body:
        try:
            decoded = loads(body)
        except ValueError:
            return {}
        return decoded
    return {}


@dataclass(frozen=True, slots=True)
class DiscordOption:
    name: str
    description: str
    type: int
    required: bool = False
    choices: tuple[Any, ...] = ()
    options: tuple[DiscordOption, ...] = ()
    min_value: int | float | None = None
    max_value: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    autocomplete: bool = False

    def as_discord(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "type": self.type,
        }
        if self.required:
            value["required"] = True
        if self.choices:
            value["choices"] = list(self.choices)
        if self.options:
            value["options"] = [option.as_discord() for option in self.options]
        for name in ("min_value", "max_value", "min_length", "max_length"):
            field_value = getattr(self, name)
            if field_value is not None:
                value[name] = field_value
        if self.autocomplete:
            value["autocomplete"] = True
        return value


@dataclass(frozen=True, slots=True)
class DiscordCommand:
    name: str
    type: int = 1
    description: str = ""
    options: tuple[DiscordOption, ...] = ()
    integration_types: tuple[int, ...] = (0,)
    contexts: tuple[int, ...] = (0,)
    default_member_permissions: str | None = None

    def __post_init__(self) -> None:
        if self.type == 1 and self.name != self.name.lower():
            raise ValueError(f"Discord command {self.name!r} must be lowercase")
        if self.type == 1 and not self.description:
            raise ValueError(f"Discord command {self.name!r} requires a description")
        if self.type in {2, 3} and self.description:
            raise ValueError(
                f"Discord command {self.name!r} type {self.type} does not allow description"
            )
        if 2 in self.contexts and 1 not in self.integration_types:
            raise ValueError("Discord PRIVATE_CHANNEL context requires USER_INSTALL")

    def as_discord(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }
        if self.options:
            value["options"] = [option.as_discord() for option in self.options]
        value["integration_types"] = list(self.integration_types)
        value["contexts"] = list(self.contexts)
        if self.default_member_permissions is not None:
            value["default_member_permissions"] = self.default_member_permissions
        return value


@dataclass(frozen=True, slots=True)
class DiscordSyncResult:
    changed: bool


def _managed_discord_value(remote: Any, desired: Any) -> bool:
    if isinstance(desired, Mapping):
        if not isinstance(remote, Mapping):
            return False
        return all(
            key in remote and _managed_discord_value(remote[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(remote, list)
            and len(remote) == len(desired)
            and all(
                _managed_discord_value(found, expected)
                for found, expected in zip(remote, desired, strict=True)
            )
        )
    return remote == desired


def _discord_commands_match(remote: Any, desired: list[dict[str, Any]]) -> bool:
    if not isinstance(remote, list) or len(remote) != len(desired):
        return False
    indexed: dict[tuple[Any, Any], Any] = {}
    for command in remote:
        if not isinstance(command, Mapping):
            return False
        key = (command.get("name"), command.get("type"))
        if key in indexed:
            return False
        indexed[key] = command
    return all(
        _managed_discord_value(indexed.get((command["name"], command["type"])), command)
        for command in desired
    )


@dataclass(frozen=True, slots=True)
class DiscordManifest:
    application_id: str
    commands: tuple[DiscordCommand, ...] = ()
    bot_token: str | None = field(default=None, repr=False, compare=False)
    rate_limiter: DiscordRateLimiter | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        seen: set[tuple[str, int]] = set()
        for command in self.commands:
            key = (command.name, command.type)
            if key in seen:
                raise ValueError(
                    f"duplicate Discord command {command.name!r} with type {command.type}"
                )
            seen.add(key)

    async def sync(
        self, client: Any, *, scope: Literal["global", "guild"], guild_id: str | None = None
    ) -> DiscordSyncResult:
        if scope == "global":
            path = f"/applications/{self.application_id}/commands"
        elif scope == "guild":
            if not guild_id:
                raise ValueError("Discord guild command sync requires guild_id")
            path = f"/applications/{self.application_id}/guilds/{guild_id}/commands"
        else:
            raise ValueError("Discord guild command sync requires guild_id")
        desired = [command.as_discord() for command in self.commands]
        headers = ()
        if self.bot_token is not None:
            headers = ((b"authorization", f"Bot {self.bot_token}".encode()),)
        route = "GET /applications/{application_id}/commands"
        major = guild_id if scope == "guild" else self.application_id
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire(route, major)
        current = await client.request("GET", path, headers=headers)
        if self.rate_limiter is not None:
            await self.rate_limiter.observe(route, major, current)
        if current.status >= 400:
            raise DiscordSyncError(f"Discord returned {current.status} during command sync")
        if _discord_commands_match(await _response_json(current), desired):
            return DiscordSyncResult(False)
        route = "PUT /applications/{application_id}/commands"
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire(route, major)
        updated = await _request_json(client, "PUT", path, desired, headers=headers)
        if self.rate_limiter is not None:
            await self.rate_limiter.observe(route, major, updated)
        if updated.status >= 400:
            raise DiscordSyncError(f"Discord returned {updated.status} during command sync")
        return DiscordSyncResult(True)


@dataclass(frozen=True, slots=True)
class DiscordDestination:
    channel_id: str
    tenant: str


@dataclass(frozen=True, slots=True)
class DiscordNativeDelivery:
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DiscordMessage:
    content: str = ""
    poll: Any = None

    def __post_init__(self) -> None:
        if self.poll is not None:
            raise UnsupportedDiscordField(
                "Discord poll is provider-native; use DiscordNativeDelivery for poll"
            )


@dataclass(frozen=True, slots=True)
class _ProactiveDelivery:
    channel_id: str
    content: str
    idempotency_key: str
    interaction_token: None = field(default=None, init=False)


@dataclass(frozen=True, slots=True)
class _DiscordJob:
    key: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _AcceptedInteraction:
    enqueued: bool
    job: _DiscordJob


class _DurableEmitter:
    __slots__ = ("_completed", "_job_context", "_length", "_parts", "_responder", "_sent")

    def __init__(self, job_context: Any, responder: DiscordResponder | None) -> None:
        self._job_context = job_context
        self._responder = responder
        self._parts: list[str] = []
        self._length = 0
        self._completed = False
        self._sent = False

    async def __call__(self, event: Any) -> None:
        if self._completed:
            raise RuntimeError("cannot emit an AgentEvent after completed")
        if event.kind == "progress":
            if event.percent is not None:
                self._job_context.report(float(event.percent), event.content or "")
            return
        if event.kind == "text":
            if event.content:
                length = len(event.content)
                if self._length + length > 2_000:
                    raise ValueError("Discord durable output exceeds the 2,000 character limit")
                self._parts.append(event.content)
                self._length += length
            return
        if event.kind != "completed":
            raise ValueError(f"unsupported AgentEvent kind {event.kind!r}")
        self._completed = True
        await self._flush()

    async def finish(self, result: Any) -> None:
        if result is not None:
            if self._responder is None:
                raise DiscordConfigurationError(
                    "durable Discord replies require an outbound client"
                )
            await self._responder.edit_original(**_message_data(result))
            self._sent = True
            return
        await self._flush()

    async def _flush(self) -> None:
        if self._sent or not self._parts:
            return
        if self._responder is None:
            raise DiscordConfigurationError("durable Discord output requires an outbound client")
        await self._responder.edit_original(content="".join(self._parts))
        self._sent = True


async def _installation_exists(chat: Any, interaction: DiscordInteraction) -> bool:
    store = chat.installations
    if store is None:
        return True
    installation = await store.fetch(
        provider="discord",
        kind=interaction.installation.kind,
        owner_id=interaction.installation.owner_id,
    )
    return installation is not None


class Discord:
    name = "discord"

    __slots__ = (
        "_application_id",
        "_bot_token",
        "_client",
        "_clock",
        "_deliver",
        "_rate_limiter",
        "_registered_chats",
        "_verifier",
    )

    def __init__(
        self,
        *,
        application_id: str,
        public_key: bytes | str | None,
        bot_token: str | None,
        client: Any = None,
        deliver: Callable[[Any], Any] | None = None,
        verify: Callable[[bytes, bytes, bytes], bool] = verify_ed25519,
        clock: Callable[[], float] = time.time,
        signature_max_age: float = 300.0,
        rate_limiter: DiscordRateLimiter | None = None,
    ) -> None:
        if not application_id:
            raise DiscordConfigurationError("Discord application_id cannot be empty")
        if public_key is None:
            raise DiscordConfigurationError("Discord public_key is required")
        if not bot_token:
            raise DiscordConfigurationError("Discord bot_token is required")
        self._application_id = application_id
        self._bot_token = bot_token
        if signature_max_age <= 0:
            raise DiscordConfigurationError("Discord signature_max_age must be positive")
        self._clock = clock
        self._verifier = DiscordInteractionVerifier(
            public_key, verify=verify, clock=clock, max_age=signature_max_age
        )
        self._client = client
        self._deliver = deliver
        self._rate_limiter = rate_limiter if rate_limiter is not None else DiscordRateLimiter()
        self._registered_chats: set[tuple[int, str]] = set()

    def validate(self) -> None:
        return None

    def _register_command(self, chat: Any, declaration: Any) -> None:
        if declaration.execution != "durable":
            return
        if chat.jobs is None:
            return
        identity = (id(chat), declaration.name)
        if identity in self._registered_chats:
            return
        task_name = _job_task_name(chat.name, declaration.name)
        task_options = {} if declaration.retries is None else {"retries": declaration.retries}

        @chat.jobs.task(task_name, **task_options)
        async def dispatch(job_context: Any, envelope: Mapping[str, Any]) -> None:
            raw = envelope.get("interaction")
            if not isinstance(raw, Mapping):
                raise ValueError("durable Discord job interaction must be a mapping")
            interaction = DiscordInteraction.parse(raw, allow_unknown=True)
            kind, name, arguments = _dispatch_shape(interaction)
            if kind != "command" or name != declaration.name:
                raise ValueError(
                    f"durable Discord task for {declaration.name!r} cannot dispatch {kind} {name!r}"
                )
            responder = None
            if self._client is not None:
                responder = DiscordResponder(
                    application_id=interaction.application_id,
                    interaction_id=interaction.id,
                    token=interaction.token,
                    received_at=float(envelope["received_at"]),
                    client=self._client,
                    clock=self._clock,
                    acknowledged=True,
                    rate_limiter=self._rate_limiter,
                )
            emitter = _DurableEmitter(job_context, responder)
            context = _chat_context(
                interaction,
                kind,
                name,
                arguments,
                federated=chat.identity is not None,
            )
            await chat._resolve(context)
            expected_key = f"discord:interaction:{interaction.id}"
            if (
                getattr(job_context, "tenant", None) != context.tenant
                or getattr(job_context, "key", None) != expected_key
                or type(getattr(job_context, "fence", None)) is not int
                or job_context.fence <= 0
            ):
                raise StaleChatJobFence(expected_key)
            emitter = await chat._stream_emitter(declaration, context, job_context, emitter)
            context = chat._durable_context(
                context,
                job_context=job_context,
                arguments=arguments,
                emit=emitter,
            )
            result = await chat._dispatch(
                kind=kind,
                name=name,
                context=context,
                arguments=arguments,
                resolved=True,
            )
            await emitter.finish(result)

        self._registered_chats.add(identity)

    def manifest(self, chat: Any, *, base_url: str) -> DiscordManifest:
        del base_url
        commands = tuple(
            DiscordCommand(
                name=declaration.name,
                description=declaration.description
                or declaration.name.replace("-", " ").replace("_", " "),
                options=_manifest_options(declaration.parameters),
            )
            for declaration in chat.commands.values()
        )
        return DiscordManifest(
            self._application_id,
            commands,
            bot_token=self._bot_token,
            rate_limiter=self._rate_limiter,
        )

    def _mount(self, chat: Any, app: Any, base_path: str) -> None:
        if chat.installations is not None and not callable(
            getattr(chat.installations, "fetch", None)
        ):
            raise DiscordConfigurationError(
                "Discord installations must provide fetch(provider=, kind=, owner_id=)"
            )
        path = f"{base_path.rstrip('/')}/discord/interactions"

        @app.post(path)
        async def interactions(request: Request) -> Response:
            body = await request.body()
            headers: dict[bytes, bytes] = {}
            protected = {b"x-signature-ed25519", b"x-signature-timestamp"}
            duplicate_content_type = False
            for name, value in request.headers:
                name = name.lower()
                if name in protected and name in headers:
                    return Response(status=401)
                if name == b"content-type" and name in headers:
                    duplicate_content_type = True
                headers[name] = value
            signature = headers.get(b"x-signature-ed25519")
            timestamp = headers.get(b"x-signature-timestamp")
            if signature is None or timestamp is None:
                return Response(status=401)
            try:
                self._verifier.verify(
                    signature=signature.decode("ascii"),
                    timestamp=timestamp.decode("ascii"),
                    body=body,
                )
            except UnicodeDecodeError, InvalidDiscordSignature:
                return Response(status=401)
            if duplicate_content_type:
                return Response(status=415)
            content_type = headers.get(b"content-type")
            if (
                content_type is not None
                and content_type.partition(b";")[0].strip().lower() != b"application/json"
            ):
                return Response(status=415)
            try:
                payload = await request.json()
            except TypeError, ValueError:
                return Response(status=400)
            if not isinstance(payload, Mapping):
                return Response(status=400)
            if payload.get("type") == 1:
                return JSONResponse({"type": 1})
            try:
                interaction = DiscordInteraction.parse(payload, allow_unknown=True)
            except TypeError, ValueError:
                return Response(status=400)
            if interaction.application_id != self._application_id:
                return Response(status=401)
            if not await _installation_exists(chat, interaction):
                return _error_response("This Discord installation is not configured.")
            kind, name, arguments = _dispatch_shape(interaction)
            declaration = chat.commands.get(name) if kind == "command" else None
            if declaration is not None and declaration.execution == "durable":
                try:
                    async with asyncio.timeout(2.5):
                        await self.accept(
                            chat,
                            interaction,
                            body=body,
                            sent_at=float(timestamp),
                        )
                except PermissionError as error:
                    return _error_response(chat.problem(error).detail or "Forbidden")
                except (
                    TimeoutError,
                    DiscordConfigurationError,
                    IdentityResolutionError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    return _error_response("Unable to queue this command. Please try again.")
                return JSONResponse({"type": 5})
            if not await chat._claim(
                provider=self.name,
                installation=interaction.tenant,
                delivery=interaction.id,
            ):
                return JSONResponse({"type": 6 if interaction.kind == "component" else 5})
            if chat._declaration(kind, name) is None:
                return _error_response("Unknown command or action.")
            errors = chat.handler_errors
            try:
                async with asyncio.timeout(2.5):
                    result = await chat._dispatch(
                        kind=kind,
                        name=name,
                        context=_chat_context(
                            interaction,
                            kind,
                            name,
                            arguments,
                            federated=chat.identity is not None,
                        ),
                        arguments=arguments,
                    )
            except (
                TimeoutError,
                IdentityResolutionError,
                PermissionError,
                TypeError,
                ValueError,
            ) as error:
                return _error_response(
                    chat.problem(error).detail or "This request could not be completed."
                )
            if chat.handler_errors != errors:
                return _error_response("This request could not be completed.")
            return _interaction_response(result)

    async def accept(
        self,
        chat: Any,
        interaction: DiscordInteraction,
        *,
        body: bytes | None = None,
        sent_at: float | None = None,
    ) -> _AcceptedInteraction:
        if chat.jobs is None or chat.inbox is None:
            raise DiscordConfigurationError(
                "durable Discord interactions require both jobs and inbox"
            )
        if not callable(getattr(chat.inbox, "claim_and_enqueue", None)):
            raise DiscordConfigurationError(
                "durable Discord inbox must provide atomic claim_and_enqueue"
            )
        job = _DiscordJob(
            key=f"discord:interaction:{interaction.id}",
            payload={"interaction": interaction.native, "received_at": self._clock()},
        )

        kind, name, _arguments = _dispatch_shape(interaction)
        declaration = chat._declaration(kind, name)
        if kind != "command" or declaration is None or declaration.execution != "durable":
            raise DiscordConfigurationError(
                f"Discord interaction {name!r} must name a declared durable command"
            )
        context = _chat_context(
            interaction,
            kind,
            name,
            _arguments,
            federated=chat.identity is not None,
        )
        await chat._resolve(context)
        await chat._authorize_declaration(context, declaration)

        async def enqueue(*, transaction: Any) -> Any:
            return await chat.jobs.enqueue(
                _job_task_name(chat.name, name),
                job.payload,
                key=job.key,
                tenant=context.tenant,
                tx=transaction,
            )

        envelope_body = dumps(interaction.native) if body is None else body
        accepted = await chat._claim_and_enqueue(
            provider=self.name,
            installation=f"{interaction.installation.kind}:{interaction.installation.owner_id}",
            delivery=interaction.id,
            body=envelope_body,
            event_type=f"{kind}:{name}",
            sent_at=sent_at,
            result_status=200,
            enqueue=enqueue,
        )
        return _AcceptedInteraction(bool(accepted), job)

    async def send(
        self,
        *,
        tenant: str,
        destination: DiscordDestination,
        content: str,
        idempotency_key: str,
    ) -> Any:
        if destination.tenant != tenant:
            from ._core import ChatTenantMismatch

            raise ChatTenantMismatch(
                f"Discord destination tenant {destination.tenant!r} does not match {tenant!r}"
            )
        delivery = _ProactiveDelivery(destination.channel_id, content, idempotency_key)
        if self._deliver is not None:
            result = self._deliver(delivery)
            return await result if inspect.isawaitable(result) else result
        if self._client is None:
            raise DiscordConfigurationError("Discord proactive delivery requires client or deliver")
        route = "POST /channels/{channel_id}/messages"
        await self._rate_limiter.acquire(route, destination.channel_id)
        response = await _request_json(
            self._client,
            "POST",
            f"/channels/{destination.channel_id}/messages",
            {"content": content},
            headers=((b"authorization", f"Bot {self._bot_token}".encode()),),
            idempotency_key=idempotency_key,
        )
        await self._rate_limiter.observe(route, destination.channel_id, response)
        return response


def _dispatch_shape(interaction: DiscordInteraction) -> tuple[str, str, dict[str, Any]]:
    if interaction.kind in {"command", "autocomplete"} and interaction.command is not None:
        return "command", interaction.command.name, interaction.command.options
    if interaction.kind == "component" and interaction.component is not None:
        return "action", interaction.component.custom_id, {"values": interaction.component.values}
    if interaction.kind == "modal" and interaction.modal is not None:
        return "action", interaction.modal.custom_id, interaction.modal.values
    return "event", interaction.kind, {}


def _conversation(interaction: DiscordInteraction) -> str:
    channel_id = interaction.native.get("channel_id")
    if channel_id is not None:
        return f"discord:channel:{channel_id}"
    if interaction.component is not None and interaction.component.message_id is not None:
        return f"discord:message:{interaction.component.message_id}"
    return f"discord:actor:{interaction.actor.id}"


def _chat_context(
    interaction: DiscordInteraction,
    kind: str,
    name: str,
    arguments: Mapping[str, Any],
    *,
    federated: bool = False,
) -> Any:
    from ._core import ChatContext

    return ChatContext(
        provider="discord",
        installation=f"{interaction.installation.kind}:{interaction.installation.owner_id}",
        tenant=interaction.tenant,
        actor=interaction.actor.id,
        conversation=_conversation(interaction),
        delivery_id=interaction.id,
        native=interaction.native,
        command=name if kind == "command" else None,
        action=name if kind == "action" else None,
        inputs=arguments,
        external_identity=(
            ExternalIdentityKey(
                provider="discord",
                installation=(
                    f"{interaction.installation.kind}:{interaction.installation.owner_id}"
                ),
                subject=interaction.actor.id,
            )
            if federated
            else None
        ),
    )


def _manifest_options(parameters: Sequence[Any]) -> tuple[DiscordOption, ...]:
    options: list[DiscordOption] = []
    for parameter in parameters:
        if parameter.name in {"request", "context", "command", "event", "interaction", "principal"}:
            continue
        annotation = parameter.annotation
        choices: tuple[Any, ...] = ()
        origin = get_origin(annotation)
        if origin in (Union, UnionType):
            candidates = tuple(item for item in get_args(annotation) if item is not type(None))
            if len(candidates) != 1:
                continue
            annotation = candidates[0]
            origin = get_origin(annotation)
        if origin is Literal:
            values = get_args(annotation)
            if not values:
                continue
            annotation = type(values[0])
            choices = tuple({"name": str(value), "value": value} for value in values)
        option_type = {str: 3, int: 4, bool: 5, float: 10}.get(annotation)
        if option_type is None:
            continue
        options.append(
            DiscordOption(
                name=parameter.name,
                description=parameter.name.replace("_", " "),
                type=option_type,
                required=parameter.default is inspect.Parameter.empty,
                choices=choices,
            )
        )
    options.sort(key=lambda option: not option.required)
    return tuple(options)


def _job_task_name(chat_name: str, command_name: str) -> str:
    source = f"{chat_name}_{command_name}"
    rendered = "".join(
        character if character.isascii() and character.isalnum() else "_" for character in source
    )
    task = f"chat_{rendered}_discord"
    if rendered == source and len(task.encode()) <= 63:
        return task
    import hashlib

    digest = hashlib.sha256(f"{chat_name}\0{command_name}".encode()).hexdigest()[:10]
    return f"chat_{rendered[:39]}_{digest}_discord"


def _message_data(result: Any) -> dict[str, Any]:
    if result is None:
        return {"content": ""}
    if hasattr(result, "content"):
        data: dict[str, Any] = {}
        if result.content is not None:
            data["content"] = result.content
        native = getattr(result, "native", None)
        if isinstance(native, Mapping):
            data.update(native)
        return data
    return {"content": str(result)}


def _error_response(content: str) -> Response:
    return JSONResponse({"type": 4, "data": {"content": content, "flags": 64}})


def _interaction_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, Mapping) and "type" in result:
        return JSONResponse(dict(result))
    if result is None:
        return JSONResponse({"type": 4, "data": {}})
    if hasattr(result, "content") and hasattr(result, "visibility"):
        data: dict[str, Any] = {}
        if result.content is not None:
            data["content"] = result.content
        if result.visibility == "ephemeral":
            data["flags"] = 64
        native = getattr(result, "native", None)
        if isinstance(native, Mapping):
            data.update(native)
        return JSONResponse({"type": 4, "data": data})
    return JSONResponse({"type": 4, "data": {"content": str(result)}})
