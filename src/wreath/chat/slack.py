from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from .._json import dumps, loads
from ..request import Request
from ..response import JSONResponse, Response
from ..router import Router
from ..webhooks import LocalReplayStore
from ._core import (
    AgentEvent,
    ChatContext,
    ChatReply,
    ExternalIdentityKey,
    IdentityResolutionError,
    StaleChatJobFence,
)

_JSON = "application/json"
_FORM = "application/x-www-form-urlencoded"
_SLACK_API = "https://slack.com/api/"
_RESPONSE_HOSTS = frozenset({"hooks.slack.com", "hooks.slack-gov.com"})
_MAX_DURABLE_TEXT = 40_000
_EVENT_SCOPES: Mapping[str, tuple[str, ...]] = {
    "app_mention": ("app_mentions:read",),
    "channel_created": ("channels:read",),
    "channel_deleted": ("channels:read",),
    "channel_rename": ("channels:read",),
    "member_joined_channel": ("channels:read",),
    "member_left_channel": ("channels:read",),
    "message.channels": ("channels:history",),
    "message.groups": ("groups:history",),
    "message.im": ("im:history",),
    "message.mpim": ("mpim:history",),
    "reaction_added": ("reactions:read",),
    "reaction_removed": ("reactions:read",),
    "team_join": ("users:read",),
    "user_change": ("users:read",),
}
_TASK_CHARACTER = re.compile(r"[^A-Za-z0-9_$]")

__all__ = [
    "Slack",
    "SlackDestination",
    "SlackInstallation",
    "SlackInstallationStore",
    "SlackRateLimited",
    "SlackResponseURL",
]


class SlackRateLimited(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Slack rate limited this call; retry after {retry_after:g} seconds")


@dataclass(frozen=True, slots=True)
class SlackInstallation:
    app_id: str
    team_id: str | None
    bot_token: str = field(repr=False)
    bot_user_id: str
    scopes: frozenset[str]
    enterprise_id: str | None = None
    is_enterprise_install: bool = False

    def __post_init__(self) -> None:
        if not self.app_id:
            raise ValueError("Slack installation app_id is required")
        if not self.bot_token:
            raise ValueError("Slack installation bot_token is required")
        if not self.bot_user_id:
            raise ValueError("Slack installation bot_user_id is required")
        if self.is_enterprise_install:
            if self.enterprise_id is None or self.team_id is not None:
                raise ValueError(
                    "an enterprise Slack installation needs enterprise_id and team_id=None"
                )
        elif self.team_id is None:
            raise ValueError("a workspace Slack installation needs team_id")

    @property
    def key(self) -> str:
        owner = self.enterprise_id if self.is_enterprise_install else self.team_id
        return str(owner)


class SlackInstallationStore(Protocol):
    async def fetch(
        self,
        *,
        enterprise_id: str | None,
        team_id: str | None,
        is_enterprise_install: bool,
    ) -> SlackInstallation | None: ...


@dataclass(frozen=True, slots=True)
class SlackDestination:
    channel_id: str
    tenant: str
    installation: SlackInstallation


@dataclass(frozen=True, slots=True)
class SlackResponseURL:
    url: str
    installation: str


@dataclass(frozen=True, slots=True)
class _Verified:
    body: bytes
    timestamp: int
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Inbound:
    kind: Literal["event", "command", "action", "verification"]
    name: str
    installation: str
    actor: str | None
    conversation: str | None
    delivery_id: str
    native: Mapping[str, Any]
    arguments: Mapping[str, Any] | None = None
    challenge: str = ""
    response_url: str | None = None
    enterprise_id: str | None = None
    is_enterprise_install: bool = False


class _DurableEmitter:
    __slots__ = (
        "_completed",
        "_job_context",
        "_length",
        "_parts",
        "_sent",
        "_slack",
        "_target",
    )

    def __init__(
        self,
        slack: Slack,
        job_context: Any,
        target: SlackResponseURL | None,
    ) -> None:
        self._slack = slack
        self._job_context = job_context
        self._target = target
        self._parts: list[str] = []
        self._length = 0
        self._completed = False
        self._sent = False

    async def __call__(self, event: AgentEvent) -> None:
        if self._completed:
            raise RuntimeError("cannot emit an AgentEvent after completed")
        if event.kind == "progress":
            if event.percent is not None:
                self._job_context.report(float(event.percent), event.content or "")
            return
        if event.kind == "text":
            if event.content:
                length = len(event.content)
                if self._length + length > _MAX_DURABLE_TEXT:
                    raise RuntimeError(
                        "durable Slack output exceeds Wreath's 40,000-character durable Slack bound"
                    )
                self._parts.append(event.content)
                self._length += length
            return
        if event.kind != "completed":
            raise ValueError(f"unsupported AgentEvent kind {event.kind!r}")
        self._completed = True
        await self._flush()

    async def finish(self, reply: Any) -> None:
        if reply is not None and not self._sent:
            await self._replace(reply)
            return
        if not self._sent:
            await self._flush()

    async def _flush(self) -> None:
        if self._sent or not self._parts:
            return
        await self._replace("".join(self._parts))

    async def _replace(self, reply: Any) -> None:
        if self._target is None:
            raise RuntimeError("durable Slack output requires a validated response_url")
        await self._slack._replace_original(self._target, reply)
        self._sent = True


class Slack:
    name: ClassVar[str] = "slack"

    def __init__(
        self,
        *,
        signing_secret: str,
        app_id: str | None = None,
        installations: SlackInstallationStore | None = None,
        http_client: Any = None,
        api_client: Any = None,
        response_client: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_age: int = 300,
        max_retries: int = 3,
        replay_entries: int = 4096,
        event_scopes: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        if not signing_secret:
            raise ValueError("Slack signing secret is required")
        if max_age <= 0:
            raise ValueError("Slack signature max_age must be positive")
        if max_retries < 0:
            raise ValueError("Slack max_retries cannot be negative")
        if replay_entries <= 0:
            raise ValueError("Slack replay_entries must be positive")
        self._secret = signing_secret.encode()
        self.app_id = app_id
        self.installations = installations
        self.http_client = http_client
        self.api_client = api_client
        self.response_client = response_client
        self.clock = clock
        self.sleep = sleep
        self.max_age = max_age
        self.max_retries = max_retries
        scopes = dict(_EVENT_SCOPES)
        for event, required in (event_scopes or {}).items():
            if not event or not required or any(not scope for scope in required):
                raise ValueError("Slack event_scopes need non-empty event and scope names")
            scopes[event] = tuple(required)
        self.event_scopes = scopes
        self._registered_commands: set[str] = set()
        self._replay = LocalReplayStore(max_entries=replay_entries, ttl=max_age)

    def _mount(self, chat: Any, app: Any, base_path: str) -> None:
        router = Router(prefix=f"{base_path}/slack")

        @router.post("/events")
        async def events(request: Request) -> Response:
            return await self._receive(chat, request, expected=_JSON)

        @router.post("/commands")
        async def commands(request: Request) -> Response:
            return await self._receive(chat, request, expected=_FORM)

        @router.post("/interactions")
        async def interactions(request: Request) -> Response:
            return await self._receive(chat, request, expected=_FORM)

        app.include_router(router)

    async def _receive(self, chat: Any, request: Request, *, expected: str) -> Response:
        body = await request.body()
        headers = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in request.headers
        }
        try:
            verified = self._verify(body, headers)
        except ValueError:
            return Response(status=401, media_type=b"")
        media_type = (request.header("content-type") or "").partition(";")[0].strip().lower()
        if media_type != expected:
            return Response(status=415, media_type=b"")
        try:
            inbound = self._parse(verified, media_type)
        except TypeError, ValueError:
            return Response(status=400, media_type=b"")
        if inbound.kind == "verification":
            return Response(
                inbound.challenge.encode(),
                media_type=b"text/plain; charset=utf-8",
            )
        installation = await self._installation_key(chat, inbound)
        if installation is None:
            return Response(status=401, media_type=b"")
        inbound = replace(inbound, installation=installation)
        if inbound.kind in {"event", "action"} and not await self._claim(chat, inbound):
            return Response(media_type=b"")
        context = self._context(inbound)
        if inbound.kind == "command" and "bot_id" in inbound.native:
            return Response(status=403, media_type=b"")
        arguments = inbound.arguments
        if inbound.kind == "command":
            declaration = chat.commands.get(inbound.name)
            if declaration is None:
                return JSONResponse(
                    {"response_type": "ephemeral", "text": f"Unknown command /{inbound.name}"}
                )
            try:
                command_arguments = cast(Mapping[str, Any], arguments)
                arguments = _command_arguments(
                    declaration,
                    str(command_arguments.get("text", "")),
                )
            except ValueError as error:
                problem = chat.problem(error)
                return JSONResponse({"response_type": "ephemeral", "text": problem.detail})
            if declaration.execution == "durable":
                try:
                    await chat._resolve(context)
                except IdentityResolutionError:
                    return await self._link_reply(chat, context)
                try:
                    await chat._authorize_declaration(context, declaration)
                except PermissionError as error:
                    problem = chat.problem(error)
                    return JSONResponse(
                        {"response_type": "ephemeral", "text": problem.detail},
                        status=problem.status,
                    )
                await self._enqueue(chat, declaration, context, arguments, verified=verified)
                return Response(media_type=b"")
            if not await self._claim(chat, inbound):
                return Response(media_type=b"")
        try:
            reply = await chat._dispatch(
                kind=inbound.kind,
                name=inbound.name,
                context=context,
                arguments=arguments,
            )
        except IdentityResolutionError:
            return await self._link_reply(chat, context)
        except PermissionError as error:
            problem = chat.problem(error)
            return JSONResponse(
                {"response_type": "ephemeral", "text": problem.detail},
                status=problem.status,
            )
        except ValueError as error:
            if inbound.kind != "command":
                return Response(status=400, media_type=b"")
            problem = chat.problem(error)
            return JSONResponse({"response_type": "ephemeral", "text": problem.detail})
        if reply is None:
            return Response(media_type=b"")
        return JSONResponse(reply.for_provider("slack"))

    async def _installation_key(self, chat: Any, inbound: _Inbound) -> str | None:
        app_id = _optional_text(inbound.native, "api_app_id")
        if self.app_id is not None and app_id != self.app_id:
            return None
        store = self.installations if self.installations is not None else chat.installations
        if store is None:
            return inbound.installation
        installation = await store.fetch(
            enterprise_id=inbound.enterprise_id,
            team_id=inbound.installation,
            is_enterprise_install=inbound.is_enterprise_install,
        )
        if installation is None or (app_id is not None and installation.app_id != app_id):
            return None
        return installation.key

    async def _claim(self, chat: Any, inbound: _Inbound) -> bool:
        if chat.inbox is not None:
            return await chat._claim(
                provider=self.name,
                installation=inbound.installation,
                delivery=inbound.delivery_id,
            )
        app_id = str(inbound.native.get("api_app_id", ""))
        source = f"{app_id}:{inbound.installation}"
        return await self._replay.claim(source, inbound.delivery_id, now=self.clock())

    def _context(self, inbound: _Inbound) -> ChatContext:
        actor = inbound.actor or ""
        return ChatContext(
            provider=self.name,
            installation=inbound.installation,
            tenant=f"slack:{inbound.installation}",
            actor=actor,
            conversation=inbound.conversation or "",
            delivery_id=inbound.delivery_id,
            native=inbound.native,
            raw=inbound.native,
            command=inbound.name if inbound.kind == "command" else None,
            action=inbound.name if inbound.kind == "action" else None,
            inputs=dict(inbound.arguments or {}),
            response_url=inbound.response_url,
            external_identity=ExternalIdentityKey(
                provider=self.name,
                installation=inbound.installation,
                subject=actor,
            )
            if actor
            else None,
        )

    async def _link_reply(self, chat: Any, context: ChatContext) -> Response:
        begin = getattr(chat.identity, "begin_link", None)
        if begin is None or context.external_identity is None:
            return Response(status=403, media_type=b"")
        challenge = await begin(
            context.external_identity,
            return_to=context.response_url or "",
        )
        return JSONResponse(
            ChatReply.ephemeral(
                "Link your account to continue",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "Link your Wreath account"},
                        "accessory": {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Sign in"},
                            "url": challenge.url,
                        },
                    }
                ],
            ).for_provider("slack")
        )

    def _register_command(self, chat: Any, declaration: Any) -> None:
        if declaration.execution != "durable" or chat.jobs is None:
            return
        task_name = _task_name(chat.name, declaration.name)
        if task_name in self._registered_commands:
            return
        self._registered_commands.add(task_name)
        task_options = {} if declaration.retries is None else {"retries": declaration.retries}

        @chat.jobs.task(task_name, **task_options)
        async def run(job_context: Any, payload: Mapping[str, Any]) -> None:
            values = dict(payload["context"])
            external_identity = values.get("external_identity")
            if isinstance(external_identity, Mapping):
                values["external_identity"] = ExternalIdentityKey(**external_identity)
            context = ChatContext(**values)
            await chat._resolve(context)
            expected_key = context.delivery_id
            if (
                getattr(job_context, "tenant", None) != context.tenant
                or getattr(job_context, "key", None) != expected_key
                or type(getattr(job_context, "fence", None)) is not int
                or job_context.fence <= 0
            ):
                raise StaleChatJobFence(f"stale or misbound durable Slack job {expected_key!r}")
            target = None
            if context.response_url:
                target = self.response_url(
                    context.response_url,
                    installation=str(context.native.get("team_id", context.installation)),
                )
            arguments = dict(payload["arguments"])
            emitter = _DurableEmitter(self, job_context, target)
            emitter = await chat._stream_emitter(declaration, context, job_context, emitter)
            context = chat._durable_context(
                context,
                job_context=job_context,
                arguments=arguments,
                emit=emitter,
            )
            reply = await chat._dispatch(
                kind="command",
                name=declaration.name,
                context=context,
                arguments=arguments,
                resolved=True,
            )
            await emitter.finish(reply)

    async def _enqueue(
        self,
        chat: Any,
        declaration: Any,
        context: ChatContext,
        arguments: Mapping[str, Any],
        *,
        verified: _Verified,
    ) -> bool:
        if chat.jobs is None:
            raise RuntimeError("durable Slack command requires jobs")
        payload = {
            "context": {
                "provider": context.provider,
                "installation": context.installation,
                "tenant": context.tenant,
                "actor": context.actor,
                "conversation": context.conversation,
                "delivery_id": context.delivery_id,
                "native": context.native,
                "raw": context.raw,
                "command": context.command,
                "inputs": dict(context.inputs),
                "response_url": context.response_url,
                "external_identity": _external_identity_payload(context.external_identity),
            },
            "arguments": dict(arguments),
        }

        async def enqueue(*, transaction: Any) -> Any:
            return await chat.jobs.enqueue(
                _task_name(chat.name, declaration.name),
                payload,
                key=context.delivery_id,
                tenant=context.tenant,
                tx=transaction,
            )

        return await chat._claim_and_enqueue(
            provider=self.name,
            installation=context.installation,
            delivery=context.delivery_id,
            body=verified.body,
            event_type=f"command:{declaration.name}",
            sent_at=float(verified.timestamp),
            result_status=200,
            enqueue=enqueue,
        )

    def _verify(self, body: bytes, headers: Mapping[str, str]) -> _Verified:
        normalized = {name.lower(): value for name, value in headers.items()}
        raw_timestamp = normalized.get("x-slack-request-timestamp")
        supplied = normalized.get("x-slack-signature")
        if raw_timestamp is None or not raw_timestamp.isascii() or not raw_timestamp.isdigit():
            raise ValueError("X-Slack-Request-Timestamp must be canonical decimal seconds")
        if supplied is None or not supplied.startswith("v0=") or len(supplied) != 67:
            raise ValueError("X-Slack-Signature must be a v0 SHA-256 signature")
        timestamp = int(raw_timestamp)
        if abs(self.clock() - timestamp) > self.max_age:
            raise ValueError(f"Slack request timestamp is outside the {self.max_age}-second window")
        base = b"v0:" + raw_timestamp.encode("ascii") + b":" + body
        expected = "v0=" + hmac.new(self._secret, base, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("Slack request signature is invalid")
        return _Verified(body, timestamp, normalized)

    def _parse(self, verified: _Verified, media_type: str) -> _Inbound:
        if media_type == _JSON:
            return self._parse_event(verified)
        if media_type == _FORM:
            return self._parse_form(verified)
        raise TypeError("Slack requests require application/json or form encoding")

    def _parse_event(self, verified: _Verified) -> _Inbound:
        value = loads(verified.body)
        if not isinstance(value, dict):
            raise ValueError("Slack event body must be a JSON object")
        if value.get("type") == "url_verification":
            challenge = value.get("challenge")
            if not isinstance(challenge, str):
                raise ValueError("Slack url_verification challenge must be a string")
            return _Inbound(
                "verification", "url_verification", "", None, None, "", value, challenge=challenge
            )
        if value.get("type") != "event_callback":
            raise ValueError("Slack Events API body must be event_callback or url_verification")
        event = value.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("Slack event_callback needs an inner event type")
        team_id = _optional_text(value, "context_team_id") or _required_text(value, "team_id")
        event_id = _required_text(value, "event_id")
        enterprise_id = _optional_text(value, "context_enterprise_id")
        enterprise_install = False
        authorizations = value.get("authorizations")
        if isinstance(authorizations, list):
            for authorization in authorizations:
                if not isinstance(authorization, Mapping):
                    continue
                enterprise_id = enterprise_id or _optional_text(authorization, "enterprise_id")
                enterprise_install = enterprise_install or _truthy(
                    authorization.get("is_enterprise_install")
                )
        return _Inbound(
            "event",
            event["type"],
            team_id,
            _optional_text(event, "user"),
            _optional_text(event, "channel"),
            event_id,
            value,
            enterprise_id=enterprise_id,
            is_enterprise_install=enterprise_install,
        )

    def _parse_form(self, verified: _Verified) -> _Inbound:
        try:
            fields = dict(
                parse_qsl(
                    verified.body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=64,
                )
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("Slack form body is invalid") from error
        if "payload" in fields:
            value = loads(fields["payload"].encode())
            if not isinstance(value, dict):
                raise ValueError("Slack interaction payload must be an object")
            actions = value.get("actions")
            if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
                raise ValueError("Slack interaction needs at least one action")
            action_id = _required_text(actions[0], "action_id")
            team = value.get("team")
            enterprise = value.get("enterprise")
            user = value.get("user")
            channel = value.get("channel")
            return _Inbound(
                "action",
                action_id,
                _nested_text(team, "id"),
                _nested_optional_text(user, "id"),
                _nested_optional_text(channel, "id"),
                _delivery_digest(verified),
                value,
                response_url=_optional_text(value, "response_url"),
                enterprise_id=_nested_optional_text(enterprise, "id"),
                is_enterprise_install=_truthy(value.get("is_enterprise_install")),
            )
        command = _required_text(fields, "command")
        return _Inbound(
            "command",
            command.removeprefix("/"),
            _required_text(fields, "team_id"),
            _optional_text(fields, "user_id"),
            _optional_text(fields, "channel_id"),
            _delivery_digest(verified),
            fields,
            arguments={"text": fields.get("text", "")},
            response_url=_optional_text(fields, "response_url"),
            enterprise_id=_optional_text(fields, "enterprise_id"),
            is_enterprise_install=_truthy(fields.get("is_enterprise_install")),
        )

    def response_url(self, url: str, *, installation: str) -> SlackResponseURL:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _RESPONSE_HOSTS
            or parsed.username is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Slack response_url must be an HTTPS Slack hooks URL")
        parts = tuple(part for part in parsed.path.split("/") if part)
        if len(parts) < 4 or parts[0] not in {"actions", "commands"} or parts[1] != installation:
            raise ValueError("Slack response_url does not belong to this installation")
        return SlackResponseURL(url, installation)

    async def respond(self, target: SlackResponseURL, reply: Any) -> Any:
        return await self._send_response_payload(target, _reply_payload(reply))

    async def _replace_original(self, target: SlackResponseURL, reply: Any) -> Any:
        payload = dict(_reply_payload(reply))
        payload["replace_original"] = True
        return await self._send_response_payload(target, payload)

    async def _send_response_payload(
        self, target: SlackResponseURL, payload: Mapping[str, Any]
    ) -> Any:
        if self.response_client is not None:
            parsed = urlsplit(target.url)
            if parsed.hostname != "hooks.slack.com":
                raise ValueError(
                    "Slack response_client is pinned to hooks.slack.com; "
                    "configure a provider-native transport for another Slack origin"
                )
            return await self._origin_request(
                self.response_client,
                "POST",
                parsed.path,
                {"content-type": "application/json; charset=utf-8"},
                dumps(payload),
            )
        return await self._request(
            "POST",
            target.url,
            {"content-type": "application/json; charset=utf-8"},
            dumps(payload),
        )

    async def call(
        self,
        installation: SlackInstallation,
        method: str,
        payload: Mapping[str, Any],
        *,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        if not method or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._"
            for character in method
        ):
            raise ValueError("Slack Web API method is invalid")
        headers = {
            "authorization": f"Bearer {installation.bot_token}",
            "content-type": "application/json; charset=utf-8",
        }
        body = dumps(dict(payload))
        attempts = 0
        while True:
            if self.api_client is None:
                status, response_headers, value = await self._request(
                    "POST",
                    _SLACK_API + method,
                    headers,
                    body,
                    idempotency_key=idempotency_key,
                )
            else:
                status, response_headers, value = await self._origin_request(
                    self.api_client,
                    "POST",
                    f"/api/{method}",
                    headers,
                    body,
                    idempotency_key=idempotency_key,
                )
            if status != 429:
                if status >= 400:
                    raise RuntimeError(f"Slack Web API {method} failed with HTTP {status}")
                if not isinstance(value, Mapping):
                    raise RuntimeError(f"Slack Web API {method} returned a non-object")
                if value.get("ok") is False:
                    raise RuntimeError(
                        f"Slack Web API {method} failed: {value.get('error', 'unknown_error')}"
                    )
                return value
            delay = _retry_after(response_headers)
            if not idempotent or attempts >= self.max_retries:
                raise SlackRateLimited(delay)
            attempts += 1
            await self.sleep(delay)

    async def _origin_request(
        self,
        client: Any,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        raw_headers = tuple(
            (name.encode("ascii"), value.encode("latin-1")) for name, value in headers.items()
        )
        response = await client.request(
            method,
            target,
            headers=raw_headers,
            body=body,
            idempotency_key=idempotency_key,
        )
        return _response_parts(response)

    async def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        client = self.http_client
        if client is None:
            raise RuntimeError("Slack outbound calls need http_client")
        if idempotency_key is not None:
            raw_headers = tuple(
                (name.encode("ascii"), value.encode("latin-1")) for name, value in headers.items()
            )
            response = await client.request(
                method,
                url,
                headers=raw_headers,
                body=body,
                idempotency_key=idempotency_key,
            )
        else:
            try:
                response = await client.request(method, url, headers=dict(headers), body=body)
            except TypeError:
                raw_headers = tuple(
                    (name.encode("ascii"), value.encode("latin-1"))
                    for name, value in headers.items()
                )
                response = await client.request(method, url, headers=raw_headers, body=body)
        if isinstance(response, tuple):
            status, response_headers, value = response
            return int(status), {str(k).lower(): str(v) for k, v in response_headers.items()}, value
        return _response_parts(response)

    async def send(
        self,
        *,
        tenant: str,
        destination: SlackDestination,
        content: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        expected_tenant = f"{self.name}:{destination.installation.key}"
        if destination.tenant != tenant or tenant != expected_tenant:
            from ._core import ChatTenantMismatch

            raise ChatTenantMismatch(
                f"Slack destination tenant {destination.tenant!r} does not match {tenant!r}"
            )
        if not idempotency_key:
            raise ValueError("Slack idempotency_key must not be empty")
        return await self.call(
            destination.installation,
            "chat.postMessage",
            {
                "channel": destination.channel_id,
                "client_msg_id": idempotency_key,
                "text": content,
            },
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def manifest(self, chat: Any, *, base_url: str) -> Mapping[str, Any]:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Slack manifest base_url must be an absolute HTTPS origin")
        origin = base_url.rstrip("/")
        prefix = f"{origin}{chat.path}/slack"
        commands = [
            {
                "command": f"/{declaration.name}",
                "description": declaration.description or declaration.name,
                "url": f"{prefix}/commands",
            }
            for declaration in chat.commands.values()
        ]
        event_names = sorted(chat.events)
        scopes = {"commands"} if commands else set()
        for event in event_names:
            required = self.event_scopes.get(event)
            if required is None:
                raise ValueError(
                    f"Slack event {event!r} has no scope mapping; pass event_scopes "
                    "with Slack's explicit OAuth scopes"
                )
            scopes.update(required)
        interactivity: dict[str, Any] = {"is_enabled": bool(chat.actions)}
        if chat.actions:
            interactivity["request_url"] = f"{prefix}/interactions"
        return {
            "features": {"slash_commands": commands},
            "oauth_config": {"scopes": {"bot": sorted(scopes)}},
            "settings": {
                "event_subscriptions": {
                    "request_url": f"{prefix}/events",
                    "bot_events": event_names,
                },
                "interactivity": interactivity,
            },
        }


def _delivery_digest(verified: _Verified) -> str:
    return hashlib.sha256(
        str(verified.timestamp).encode("ascii") + b":" + verified.body
    ).hexdigest()


def _task_name(chat_name: str, command_name: str) -> str:
    raw = f"chat_{chat_name}_{command_name}"
    normalized = _TASK_CHARACTER.sub("_", raw)
    if normalized == raw and len(raw.encode("ascii")) <= 63:
        return raw
    digest = hashlib.blake2s(raw.encode(), digest_size=5).hexdigest()
    return f"{normalized[:52]}_{digest}"


def _external_identity_payload(key: ExternalIdentityKey | None) -> Mapping[str, Any] | None:
    if key is None:
        return None
    return {
        "provider": key.provider,
        "installation": key.installation,
        "issuer": key.issuer,
        "subject": key.subject,
        "tenant": key.tenant,
    }


def _response_parts(response: Any) -> tuple[int, Mapping[str, str], Any]:
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1") for name, value in response.headers
    }
    value = loads(response.body) if response.body else {}
    return int(response.status), response_headers, value


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _required_text(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Slack payload {name} must be a non-empty string")
    return result


def _optional_text(value: Mapping[str, Any], name: str) -> str | None:
    result = value.get(name)
    return result if isinstance(result, str) and result else None


def _nested_text(value: Any, name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("Slack payload nested identity must be an object")
    return _required_text(value, name)


def _nested_optional_text(value: Any, name: str) -> str | None:
    return _optional_text(value, name) if isinstance(value, Mapping) else None


def _retry_after(headers: Mapping[str, str]) -> float:
    raw = headers.get("retry-after")
    try:
        delay = float(raw) if raw is not None else 1.0
    except ValueError as error:
        raise RuntimeError("Slack Retry-After header is invalid") from error
    if delay < 0:
        raise RuntimeError("Slack Retry-After header cannot be negative")
    return delay


def _reply_payload(reply: Any) -> Mapping[str, Any]:
    if isinstance(reply, str):
        return {"text": reply}
    render = getattr(reply, "for_provider", None)
    if callable(render):
        return render("slack")
    native = getattr(reply, "native", None)
    payload: dict[str, Any] = dict(native) if isinstance(native, Mapping) else {}
    text = getattr(reply, "content", "")
    if text:
        payload["text"] = text
    visibility = getattr(reply, "visibility", None)
    if visibility in {"ephemeral", "in_channel"}:
        payload["response_type"] = visibility
    blocks = getattr(reply, "blocks", None)
    if blocks is not None:
        payload["blocks"] = blocks
    return payload


def _command_arguments(declaration: Any, text: str) -> dict[str, Any]:
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        raise ValueError(f"invalid command text: {error}") from error
    parameters = [
        parameter
        for parameter in declaration.parameters
        if parameter.name
        not in {"request", "context", "command", "event", "interaction", "principal"}
    ]
    by_flag = {parameter.name.replace("_", "-"): parameter for parameter in parameters}
    supplied: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            positional.append(token)
            index += 1
            continue
        flag, separator, attached = token[2:].partition("=")
        parameter = by_flag.get(flag.removeprefix("no-"))
        if parameter is None:
            raise ValueError(f"unknown command option --{flag}")
        if parameter.annotation is bool:
            if separator:
                supplied[parameter.name] = attached
            else:
                supplied[parameter.name] = not flag.startswith("no-")
            index += 1
            continue
        if separator:
            supplied[parameter.name] = attached
            index += 1
            continue
        if index + 1 >= len(tokens):
            raise ValueError(f"command option --{flag} needs a value")
        supplied[parameter.name] = tokens[index + 1]
        index += 2
    available = [parameter for parameter in parameters if parameter.name not in supplied]
    if len(positional) > len(available):
        raise ValueError(f"unexpected command argument {positional[len(available)]}")
    for parameter, value in zip(available, positional, strict=False):
        supplied[parameter.name] = value
    return supplied
