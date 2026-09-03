from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import inspect
import io
import json
import time
import urllib.parse
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from .._auth import jwt as _jwt
from .._auth.jwt import JwtError, key_from_jwk
from ..response import JSONResponse, Response
from ..state import State
from ..webhooks import LocalReplayStore
from ._core import IdentityResolutionError, StaleChatJobFence

BOT_CONNECTOR_ISSUER = "https://api.botframework.com"
BOT_CONNECTOR_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_BOT_CONNECTOR_ORIGIN = ("https", "login.botframework.com", 443)
_MANIFEST_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json"
)
_SCOPES = frozenset({"personal", "team", "groupChat"})
_CLOCK_SKEW = 300
_CONTEXT_PARAMETERS = frozenset(
    {"request", "context", "command", "event", "interaction", "principal"}
)
_DURABLE_TEXT_LIMIT = 28_000
_JWKS_REFRESH_INTERVAL = 300
_TOKEN_REFRESH_SKEW = 60


class TeamsRefusal(Exception):
    __slots__ = ("reason", "status")

    def __init__(self, reason: str, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


class TeamsConnectorError(Exception):
    __slots__ = ("retry_after", "status")

    def __init__(self, *, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"the Teams connector returned HTTP {status}")
        self.status = int(status)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TeamsBotConfig:
    app_id: str
    app_secret: str
    messaging_endpoint: str
    allowed_tenants: frozenset[str]
    login_issuers: Mapping[str, str] = field(default_factory=dict)
    max_token_lifetime: int = 3600

    def __post_init__(self) -> None:
        if not self.app_id:
            raise ValueError("Teams app_id must be non-empty")
        if not self.app_secret:
            raise ValueError("Teams app_secret must be non-empty")
        _require_https(self.messaging_endpoint, "messaging_endpoint")
        if not self.allowed_tenants:
            raise ValueError("Teams allowed_tenants must contain at least one tenant")
        if any(not tenant for tenant in self.allowed_tenants):
            raise ValueError("Teams allowed_tenants may not contain an empty tenant")
        if not 0 < self.max_token_lifetime <= 86_400:
            raise ValueError("Teams max_token_lifetime must be between 1 and 86400 seconds")
        copied = dict(self.login_issuers)
        for tenant, issuer in copied.items():
            if tenant not in self.allowed_tenants:
                raise ValueError(
                    f"login issuer tenant {tenant!r} is not present in allowed_tenants"
                )
            _require_https(issuer, f"login issuer for tenant {tenant!r}")
        object.__setattr__(self, "login_issuers", copied)


@dataclass(frozen=True, slots=True)
class TeamsActivity:
    id: str
    kind: str
    channel: str
    service_url: str
    tenant_id: str
    aad_object_id: str | None
    installation_key: tuple[str, str]
    conversation_id: str
    conversation_type: str
    reply_to_id: str
    sender_id: str
    sender_name: str | None
    recipient_id: str
    recipient_name: str | None
    text: str | None
    name: str | None
    action: str | None
    value: Mapping[str, Any]
    members_added: tuple[str, ...]
    raw: Mapping[str, Any]
    connector_verified: bool = False

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> TeamsActivity:
        if not isinstance(payload, Mapping):
            raise TeamsRefusal("malformed-activity", "Teams activity must be a JSON object")
        activity_id = _required_text(payload, "id", "missing-activity-id", "activity id")
        service_url = _required_text(payload, "serviceUrl", "missing-service-url", "serviceUrl")
        channel = payload.get("channelId")
        if channel != "msteams":
            raise TeamsRefusal("wrong-channel", "Teams activity channelId must be 'msteams'")
        conversation = _object(payload.get("conversation"))
        conversation_id = _required_text(
            conversation, "id", "missing-conversation", "conversation id"
        )
        conversation_type = conversation.get("conversationType")
        if not isinstance(conversation_type, str) or not conversation_type:
            conversation_type = "channel"
        channel_data = _object(payload.get("channelData"))
        data_tenant = _object(channel_data.get("tenant")).get("id")
        conversation_tenant = conversation.get("tenantId")
        if (
            isinstance(data_tenant, str)
            and data_tenant
            and isinstance(conversation_tenant, str)
            and conversation_tenant
            and data_tenant != conversation_tenant
        ):
            raise TeamsRefusal(
                "ambiguous-tenant",
                "Teams activity names two different tenants; refusing to prefer either",
                status=403,
            )
        if not isinstance(conversation_tenant, str) or not conversation_tenant:
            raise TeamsRefusal("missing-tenant", "Teams activity must name its tenant")
        tenant = conversation_tenant
        if conversation_type == "channel":
            installation_id = _object(channel_data.get("team")).get("id")
            if not isinstance(installation_id, str) or not installation_id:
                raise TeamsRefusal(
                    "missing-installation",
                    "a Teams channel activity must name its team installation",
                )
        else:
            installation_id = conversation_id
        sender = _object(payload.get("from"))
        recipient = _object(payload.get("recipient"))
        raw_sender_id = sender.get("id")
        sender_id: str = raw_sender_id if isinstance(raw_sender_id, str) else ""
        raw_recipient_id = recipient.get("id")
        recipient_id: str = raw_recipient_id if isinstance(raw_recipient_id, str) else ""
        value = payload.get("value")
        native_value = dict(value) if isinstance(value, Mapping) else {}
        members = payload.get("membersAdded")
        members_added = (
            tuple(
                item["id"]
                for item in members
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            )
            if isinstance(members, list)
            else ()
        )
        raw_kind = payload.get("type")
        kind = raw_kind if isinstance(raw_kind, str) else ""
        return cls(
            id=activity_id,
            kind=kind,
            channel="msteams",
            service_url=service_url,
            tenant_id=tenant,
            aad_object_id=(
                sender.get("aadObjectId") if isinstance(sender.get("aadObjectId"), str) else None
            ),
            installation_key=(tenant, installation_id),
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            reply_to_id=activity_id,
            sender_id=sender_id,
            sender_name=sender.get("name") if isinstance(sender.get("name"), str) else None,
            recipient_id=recipient_id,
            recipient_name=(
                recipient.get("name") if isinstance(recipient.get("name"), str) else None
            ),
            text=payload.get("text") if isinstance(payload.get("text"), str) else None,
            name=payload.get("name") if isinstance(payload.get("name"), str) else None,
            action=payload.get("action") if isinstance(payload.get("action"), str) else None,
            value=native_value,
            members_added=members_added,
            raw=dict(payload),
        )

    def verified(self) -> TeamsActivity:
        return replace(self, connector_verified=True)


@dataclass(frozen=True, slots=True)
class TeamsInstallation:
    tenant_id: str
    installation_id: str
    service_url: str
    conversation_id: str
    bot_id: str

    def refreshed_from(
        self, activity: TeamsActivity, *, connector_verified: bool
    ) -> TeamsInstallation:
        if not connector_verified:
            raise TeamsRefusal(
                "unverified-conversation-reference",
                "a Teams conversation reference may only be refreshed from verified ingress",
                status=403,
            )
        if activity.tenant_id != self.tenant_id:
            raise TeamsRefusal(
                "wrong-tenant",
                "a Teams conversation reference cannot move between tenants",
                status=403,
            )
        return replace(
            self,
            service_url=activity.service_url,
            conversation_id=activity.conversation_id,
        )


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json: Mapping[str, Any]


class TeamsConnectorVerifier:
    __slots__ = (
        "_clock",
        "_config",
        "_endorsements",
        "_fetch",
        "_keys",
        "_jwks_uri",
        "_last_refresh",
        "_refresh_lock",
        "refresh_count",
    )

    fetches_on_request_path = True

    def __init__(
        self,
        config: TeamsBotConfig,
        *,
        fetch: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._fetch = fetch or _fetch_json
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._endorsements: dict[str, frozenset[str]] = {}
        self._jwks_uri: str | None = None
        self._last_refresh = float("-inf")
        self._refresh_lock = asyncio.Lock()
        self.refresh_count = 0

    async def startup(self) -> int:
        metadata = await self._fetch(BOT_CONNECTOR_METADATA_URL)
        if metadata.get("issuer") != BOT_CONNECTOR_ISSUER:
            raise TeamsRefusal(
                "wrong-metadata-issuer",
                "Bot Connector metadata names an unexpected issuer",
            )
        algorithms = metadata.get("id_token_signing_alg_values_supported")
        if not isinstance(algorithms, list):
            raise TeamsRefusal(
                "unsupported-signing-algorithm",
                "Bot Connector metadata must advertise RS256",
            )
        if "RS256" not in algorithms:
            raise TeamsRefusal(
                "unsupported-signing-algorithm",
                "Bot Connector metadata must advertise RS256",
            )
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not _same_origin(jwks_uri, _BOT_CONNECTOR_ORIGIN):
            raise TeamsRefusal(
                "unsafe-jwks-uri",
                "Bot Connector JWKS must use HTTPS on the trusted metadata origin",
            )
        self._jwks_uri = jwks_uri
        return await self._refresh_keys(count=False)

    async def _refresh_keys(self, *, count: bool) -> int:
        jwks_uri = self._jwks_uri
        if jwks_uri is None:
            raise TeamsRefusal("missing-jwks-uri", "Bot Connector verifier has not started")
        self._last_refresh = self._clock()
        if count:
            self.refresh_count += 1
        document = await self._fetch(jwks_uri)
        keys: dict[str, Any] = {}
        endorsements: dict[str, frozenset[str]] = {}
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list):
            raise TeamsRefusal("malformed-jwks", "Bot Connector JWKS keys must be a list")
        for raw in raw_keys:
            if not isinstance(raw, Mapping):
                continue
            kid = raw.get("kid")
            if not isinstance(kid, str) or not kid or kid in keys:
                continue
            if raw.get("alg") not in (None, "RS256"):
                continue
            try:
                key = key_from_jwk(raw)
            except JwtError, KeyError, TypeError, ValueError:
                continue
            claimed = raw.get("endorsements")
            values = (
                frozenset(item for item in claimed if isinstance(item, str))
                if isinstance(claimed, list)
                else frozenset()
            )
            keys[kid] = key
            endorsements[kid] = values
        if not keys:
            raise TeamsRefusal(
                "empty-jwks", "Bot Connector JWKS contains no usable RS256 signing key"
            )
        self._keys = keys
        self._endorsements = endorsements
        return len(keys)

    async def verify(self, authorization: str | None, activity: Mapping[str, Any]) -> None:
        token = _bearer_token(authorization)
        try:
            header, claims, signing_input, signature = _jwt._parse_compact(token)
        except KeyError, ValueError:
            raise TeamsRefusal(
                "malformed-token", "Bot Connector Bearer token is not a JWT"
            ) from None
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise TeamsRefusal(
                "unknown-signing-key", "Bot Connector JWT uses an unknown signing key"
            )
        try:
            key = self._keys[kid]
        except KeyError:
            async with self._refresh_lock:
                try:
                    key = self._keys[kid]
                except KeyError:
                    if self._clock() - self._last_refresh >= _JWKS_REFRESH_INTERVAL:
                        await self._refresh_keys(count=True)
                    try:
                        key = self._keys[kid]
                    except KeyError:
                        raise TeamsRefusal(
                            "unknown-signing-key",
                            "Bot Connector JWT uses an unknown signing key",
                        ) from None
        if header.get("alg") != "RS256":
            raise TeamsRefusal("unsupported-signing-algorithm", "Bot Connector JWT must use RS256")
        try:
            signature_ok = _jwt._verify_signature("RS256", key, signing_input, signature)
        except KeyError, OverflowError, ValueError:
            signature_ok = False
        if not signature_ok:
            raise TeamsRefusal("invalid-signature", "Bot Connector JWT signature is invalid")
        now = int(self._clock())
        if claims.get("iss") != BOT_CONNECTOR_ISSUER:
            raise TeamsRefusal("wrong-issuer", "Bot Connector JWT issuer is not trusted")
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = {audience}
        elif isinstance(audience, list):
            if not all(isinstance(item, str) for item in audience):
                raise TeamsRefusal("wrong-audience", "Bot Connector JWT audience is not this bot")
            audiences = set(audience)
        else:
            audiences = set()
        if self._config.app_id not in audiences:
            raise TeamsRefusal("wrong-audience", "Bot Connector JWT audience is not this bot")
        service_url = activity.get("serviceUrl")
        if claims.get("serviceurl") != service_url:
            raise TeamsRefusal(
                "wrong-service-url",
                "Bot Connector JWT serviceurl does not match the activity serviceUrl",
            )
        expiry = claims.get("exp")
        if not isinstance(expiry, int):
            raise TeamsRefusal("expired-token", "Bot Connector JWT has expired")
        if now - _CLOCK_SKEW >= expiry:
            raise TeamsRefusal("expired-token", "Bot Connector JWT has expired")
        if expiry > now + self._config.max_token_lifetime + _CLOCK_SKEW:
            raise TeamsRefusal(
                "token-lifetime-too-long",
                "Bot Connector JWT exceeds Teams max_token_lifetime",
            )
        not_before = claims.get("nbf")
        if isinstance(not_before, int) and now + _CLOCK_SKEW < not_before:
            raise TeamsRefusal("token-not-yet-valid", "Bot Connector JWT is not valid yet")
        channel = activity.get("channelId")
        if not isinstance(channel, str):
            raise TeamsRefusal(
                "missing-channel-endorsement",
                f"Bot Connector signing key does not endorse channel {channel!r}",
                status=403,
            )
        if channel not in self._endorsements[kid]:
            raise TeamsRefusal(
                "missing-channel-endorsement",
                f"Bot Connector signing key does not endorse channel {channel!r}",
                status=403,
            )


@dataclass(slots=True)
class TeamsContext:
    activity: TeamsActivity
    external_identity: Any
    tenant: str | None = None
    identity: Any = None
    principal: Any = None
    action: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    method: str = "POST"
    path: str = "/_wreath/chat/teams"
    state: State = field(default_factory=State)
    agent_request: Any = None
    emit: Any = None
    job_context: Any = None

    provider: ClassVar[str] = "teams"

    @property
    def channel_actor_id(self) -> str:
        return self.activity.sender_id

    @property
    def installation(self) -> str:
        return self.activity.installation_key[1]

    @property
    def actor(self) -> str:
        return self.activity.sender_id

    @property
    def conversation(self) -> str:
        return self.activity.conversation_id

    @property
    def delivery_id(self) -> str:
        return self.activity.id

    @property
    def native(self) -> Mapping[str, Any]:
        return self.activity.raw

    @property
    def raw(self) -> Mapping[str, Any]:
        return self.activity.raw

    @property
    def command(self) -> str | None:
        return self.action


class _TeamsDurableEmitter:
    __slots__ = (
        "_activity",
        "_completed",
        "_job",
        "_key",
        "_length",
        "_parts",
        "_provider",
        "_sent",
    )

    def __init__(
        self,
        provider: Teams,
        job: Any,
        activity: TeamsActivity,
        key: str,
    ) -> None:
        self._provider = provider
        self._job = job
        self._activity = activity
        self._key = key
        self._parts: list[str] = []
        self._length = 0
        self._completed = False
        self._sent = False

    async def __call__(self, event: Any) -> None:
        if self._completed:
            raise RuntimeError("cannot emit an AgentEvent after completed")
        if event.kind == "progress":
            percent = 0.0 if event.percent is None else float(event.percent)
            self._job.report(percent, event.content or "")
            return
        if event.kind == "text":
            if event.content:
                length = self._length + len(event.content)
                if length > _DURABLE_TEXT_LIMIT:
                    raise ValueError(
                        "durable Teams text exceeds Wreath's 28,000-character durable Teams bound"
                    )
                self._parts.append(event.content)
                self._length = length
            return
        if event.kind != "completed":
            raise ValueError(f"unsupported AgentEvent kind {event.kind!r}")
        self._completed = True
        await self._flush()

    async def finish(self, result: Any) -> None:
        if self._sent:
            return
        if result is not None:
            await self._provider.reply(self._activity, result, idempotency_key=self._key)
            self._sent = True
            return
        await self._flush()

    async def _flush(self) -> None:
        if self._sent or not self._parts:
            return
        content = "".join(self._parts)
        self._parts.clear()
        self._length = 0
        await self._provider.reply(
            self._activity,
            content,
            idempotency_key=self._key,
        )
        self._sent = True


class Teams:
    name: ClassVar[str] = "teams"

    def __init__(
        self,
        *,
        config: TeamsBotConfig,
        verifier: Any = None,
        connector: Any = None,
        installations: Any = None,
        token_provider: Callable[[], str | Awaitable[str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.verifier = TeamsConnectorVerifier(config) if verifier is None else verifier
        self.connector = _UrlConnector() if connector is None else connector
        self.installations = installations
        self.clock = clock
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        if token_provider is not None:
            self.token_provider = token_provider
        else:
            self.token_provider = self._connector_token
        self._chat: Any = None
        self._registered: set[tuple[int, str]] = set()
        self._replay = LocalReplayStore(
            max_entries=4096,
            ttl=float(config.max_token_lifetime + _CLOCK_SKEW),
        )

    def external_identity(self, activity: TeamsActivity) -> Any:
        from ._core import ExternalIdentityKey

        subject: Any = activity.aad_object_id
        issuer = self.config.login_issuers.get(activity.tenant_id)
        return ExternalIdentityKey(
            issuer=issuer,
            subject=subject,
            tenant=activity.tenant_id,
        )

    def _mount(self, chat: Any, app: Any, base_path: str) -> None:
        self._chat = chat
        if self.installations is None:
            self.installations = getattr(chat, "installations", None)

        async def startup(_app: Any) -> None:
            await self.verifier.startup()

        app.on_startup(startup)

        @app.post(f"{base_path}/teams")
        async def teams_endpoint(request: Any) -> Response:
            try:
                raw_authorization = request._single_header(b"authorization")
            except ValueError:
                return Response(b"", status=401, media_type=b"")
            authorization = (
                None if raw_authorization is None else raw_authorization.decode("latin-1")
            )
            try:
                payload = await request.json()
                if not isinstance(payload, Mapping):
                    raise TeamsRefusal(
                        "malformed-activity", "Teams activity must be a JSON object", status=400
                    )
                await _maybe_await(self.verifier.verify(authorization, payload))
                activity = TeamsActivity.parse(payload).verified()
                _trusted_service_url(activity.service_url)
                if activity.tenant_id not in self.config.allowed_tenants:
                    raise TeamsRefusal(
                        "unconfigured-tenant",
                        f"Teams tenant {activity.tenant_id!r} is not configured",
                        status=403,
                    )
                await self._validate_installation(activity)
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                return await self._receive(chat, activity, request=request, body=body)
            except TeamsRefusal as refusal:
                return Response(b"", status=refusal.status, media_type=b"")
            except PermissionError:
                return Response(b"", status=403, media_type=b"")
            except IdentityResolutionError:
                return Response(b"", status=403, media_type=b"")
            except ValueError:
                return Response(b"", status=400, media_type=b"")

    def _register_command(self, chat: Any, declaration: Any) -> None:
        if getattr(declaration, "execution", "inline") != "durable":
            return
        name = declaration.name
        identity = (id(chat), name)
        if identity in self._registered:
            return
        jobs = chat.jobs
        if jobs is None:
            return
        job_name = _job_name(chat.name, name)

        @jobs.task(
            job_name,
            retries=4 if declaration.retries is None else declaration.retries,
            backoff="exp",
        )
        async def execute(job: Any, payload: Mapping[str, Any]) -> Any:
            raw_activity = _object(payload.get("activity"))
            verification = payload.get("verification")
            if not isinstance(verification, str) or not hmac.compare_digest(
                verification, self._verification(raw_activity, name)
            ):
                raise TeamsRefusal(
                    "invalid-durable-envelope",
                    "durable Teams activity verification is invalid",
                    status=403,
                )
            activity = TeamsActivity.parse(raw_activity).verified()
            expected_key = self._idempotency_key(activity, name)
            _, inputs = _command(activity)
            handler_arguments = _handler_arguments(chat, name, inputs)
            context = self._context(chat, activity, action=name, inputs=inputs)
            await chat._resolve(context)
            if (
                getattr(job, "tenant", None) != context.tenant
                or getattr(job, "key", None) != expected_key
                or type(getattr(job, "fence", None)) is not int
                or job.fence <= 0
            ):
                raise StaleChatJobFence(expected_key)
            emitter = _TeamsDurableEmitter(self, job, activity, expected_key)
            emitter = await chat._stream_emitter(declaration, context, job, emitter)
            context = chat._durable_context(
                context,
                job_context=job,
                arguments=handler_arguments,
                emit=emitter,
            )
            result = await chat._dispatch(
                kind="command",
                name=name,
                context=context,
                arguments=handler_arguments,
                resolved=True,
            )
            await emitter.finish(result)
            return result

        self._registered.add(identity)

    async def _receive(
        self,
        chat: Any,
        activity: TeamsActivity,
        *,
        request: Any = None,
        body: bytes = b"{}",
    ) -> Response:
        if activity.kind in {"installationUpdate", "conversationUpdate"}:
            if not await self._claim(chat, activity):
                return Response(b"", status=200, media_type=b"")
            await self._installation_event(activity)
            return Response(b"", status=200, media_type=b"")
        if activity.kind == "invoke":
            if not await self._claim(chat, activity):
                return Response(b"", status=200, media_type=b"")
            if activity.name != "adaptiveCard/action":
                return _invoke_response(
                    501,
                    "application/vnd.microsoft.error",
                    {
                        "code": "NotSupported",
                        "message": f"Unsupported Teams invoke: {activity.name}",
                    },
                )
            action = _object(activity.value.get("action"))
            name = action.get("verb")
            if not isinstance(name, str) or not name:
                return _invoke_response(
                    400,
                    "application/vnd.microsoft.error",
                    {"code": "BadRequest", "message": "Adaptive Card action has no verb"},
                )
            inputs = _object(action.get("data"))
            context = self._context(
                chat, activity, action=name, inputs=dict(inputs), request=request
            )
            try:
                kind = "action" if chat._declaration("action", name) is not None else "command"
                result = await chat._dispatch(
                    kind=kind,
                    name=name,
                    context=context,
                    arguments=_handler_arguments(chat, name, inputs),
                )
            except PermissionError as error:
                step_up = str(error) == "second factor required"
                return _invoke_response(
                    401 if step_up else 403,
                    "application/vnd.microsoft.error",
                    {
                        "code": "StepUpRequired" if step_up else "Forbidden",
                        "message": (
                            "Confirm your identity to continue."
                            if step_up
                            else "This action is not permitted."
                        ),
                    },
                )
            except IdentityResolutionError:
                return _invoke_response(
                    403,
                    "application/vnd.microsoft.error",
                    {"code": "IdentityNotLinked", "message": "Link your identity to continue."},
                )
            return _invoke_result(result)
        name, inputs = _command(activity)
        context = self._context(chat, activity, action=name, inputs=inputs, request=request)
        declaration = _declaration(chat, name)
        if getattr(declaration, "execution", "inline") == "durable":
            jobs = chat.jobs
            await chat._resolve(context)
            await chat._authorize_declaration(context, declaration)
            payload = {
                "activity": dict(activity.raw),
                "verification": self._verification(activity.raw, name),
            }

            async def enqueue(*, transaction: Any) -> Any:
                return await jobs.enqueue(
                    _job_name(chat.name, name),
                    payload,
                    tenant=context.tenant,
                    key=self._idempotency_key(activity, name),
                    tx=transaction,
                )

            await chat._claim_and_enqueue(
                provider=self.name,
                installation=f"{activity.tenant_id}:{activity.installation_key[1]}",
                delivery=activity.id,
                body=body,
                event_type=f"command:{name}",
                sent_at=self.clock(),
                result_status=200,
                enqueue=enqueue,
            )
        else:
            if not await self._claim(chat, activity):
                return Response(b"", status=200, media_type=b"")
            result = await chat._dispatch(
                kind="command",
                name=name,
                context=context,
                arguments=_handler_arguments(chat, name, inputs),
            )
            if result is not None:
                await self.reply(activity, result)
        return Response(b"", status=200, media_type=b"")

    async def _validate_installation(self, activity: TeamsActivity) -> None:
        if self.installations is None or activity.kind in {
            "installationUpdate",
            "conversationUpdate",
        }:
            return
        installation = await self.installations.get(activity.installation_key)
        if not isinstance(installation, TeamsInstallation):
            raise TeamsRefusal(
                "unknown-installation",
                "Teams activity does not belong to an installed bot",
                status=403,
            )
        refreshed = installation.refreshed_from(activity, connector_verified=True)
        await self.installations.put(activity.installation_key, refreshed)

    def _context(
        self,
        chat: Any,
        activity: TeamsActivity,
        *,
        action: str | None = None,
        inputs: dict[str, Any],
        request: Any = None,
    ) -> TeamsContext:
        external_identity = self.external_identity(activity) if chat.identity is not None else None
        request_state = request.state if request is not None else State()
        return TeamsContext(
            activity=activity,
            external_identity=external_identity,
            tenant=f"teams:{activity.tenant_id}",
            action=action,
            inputs=inputs,
            method=getattr(request, "method", "POST"),
            path=getattr(request, "path", f"{chat.path}/teams"),
            state=request_state,
        )

    async def _claim(self, chat: Any, activity: TeamsActivity) -> bool:
        if chat.inbox is None:
            return await self._replay.claim(
                f"teams:{activity.tenant_id}:{activity.installation_key[1]}",
                activity.id,
                now=self.clock(),
            )
        return bool(
            await chat._claim(
                provider="teams",
                installation=f"{activity.tenant_id}:{activity.installation_key[1]}",
                delivery=activity.id,
            )
        )

    async def _installation_event(self, activity: TeamsActivity) -> None:
        if self.installations is None:
            return
        if activity.kind == "conversationUpdate":
            if self.config.app_id not in activity.members_added:
                return
            action = "add"
        else:
            action = activity.action
        if action == "remove":
            await self.installations.delete(activity.installation_key)
            return
        if action != "add":
            return
        await self.installations.put(
            activity.installation_key,
            TeamsInstallation(
                tenant_id=activity.tenant_id,
                installation_id=activity.installation_key[1],
                service_url=activity.service_url,
                conversation_id=activity.conversation_id,
                bot_id=self.config.app_id,
            ),
        )

    async def reply(
        self,
        incoming: TeamsActivity,
        reply: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        _trusted_service_url(incoming.service_url)
        token = await _maybe_await(self.token_provider())
        conversation = urllib.parse.quote(incoming.conversation_id, safe="")
        activity_id = urllib.parse.quote(incoming.reply_to_id, safe="")
        body = {
            "type": "message",
            "channelId": "msteams",
            "serviceUrl": incoming.service_url,
            "from": {"id": self.config.app_id, "name": "Wreath"},
            "recipient": {
                "id": incoming.sender_id,
                "name": incoming.sender_name,
            },
            "conversation": {"id": incoming.conversation_id},
            "replyToId": incoming.reply_to_id,
            **_outbound_reply(reply),
        }
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        if idempotency_key is not None:
            headers["x-ms-client-request-id"] = idempotency_key
        request = ConnectorRequest(
            method="POST",
            url=(f"{incoming.service_url}v3/conversations/{conversation}/activities/{activity_id}"),
            headers=headers,
            json=body,
        )
        return await self.connector.send(request)

    async def proactive(
        self,
        installation: TeamsInstallation,
        reply: Any,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        _trusted_service_url(installation.service_url)
        if installation.tenant_id not in self.config.allowed_tenants:
            raise TeamsRefusal(
                "unconfigured-tenant",
                f"Teams tenant {installation.tenant_id!r} is not configured",
                status=403,
            )
        token = await _maybe_await(self.token_provider())
        conversation = urllib.parse.quote(installation.conversation_id, safe="")
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        if idempotency_key is not None:
            if not idempotency_key:
                raise ValueError("Teams idempotency_key must not be empty")
            headers["x-ms-client-request-id"] = idempotency_key
        request = ConnectorRequest(
            method="POST",
            url=(f"{installation.service_url}v3/conversations/{conversation}/activities"),
            headers=headers,
            json={
                "type": "message",
                "channelId": "msteams",
                "serviceUrl": installation.service_url,
                "from": {"id": self.config.app_id},
                "conversation": {"id": installation.conversation_id},
                **_outbound_reply(reply),
            },
        )
        return await self.connector.send(request)

    async def send(
        self,
        *,
        tenant: str,
        destination: Any,
        content: str,
        idempotency_key: str,
    ) -> Any:
        prefix, _, tenant_id = tenant.partition(":")
        if prefix != self.name or not tenant_id:
            raise ValueError("Teams tenant must use the form 'teams:<tenant-id>'")
        installation = destination if isinstance(destination, TeamsInstallation) else None
        if installation is None:
            if self.installations is None:
                raise RuntimeError("Teams proactive delivery requires an installation store")
            installation = await self.installations.get((tenant_id, str(destination)))
        if not isinstance(installation, TeamsInstallation):
            raise KeyError(destination)
        if installation.tenant_id != tenant_id:
            from ._core import ChatTenantMismatch

            raise ChatTenantMismatch(
                f"Teams destination tenant {installation.tenant_id!r} does not match {tenant_id!r}"
            )
        result = await self.proactive(
            installation,
            content,
            idempotency_key=idempotency_key,
        )
        return result

    def manifest(self, chat: Any, *, base_url: str) -> Mapping[str, Any]:
        _require_https(base_url, "manifest base_url")
        origin = base_url.rstrip("/")
        host = urllib.parse.urlsplit(origin).hostname
        commands = tuple(
            {
                "title": declaration.name,
                "description": declaration.description or declaration.name,
            }
            for declaration in sorted(chat.commands.values(), key=lambda item: item.name)
        )
        manifest = TeamsManifest(
            package_id=self.config.app_id,
            app_id=self.config.app_id,
            version="1.0.0",
            name=chat.name,
            short_description=f"{chat.name} commands for Microsoft Teams",
            long_description=f"Run authorized {chat.name} commands from Microsoft Teams.",
            developer_name="Wreath",
            website_url=origin,
            privacy_url=f"{origin}/privacy",
            terms_url=f"{origin}/terms",
            scopes=("personal", "team", "groupChat"),
            commands=commands,
            entra_resource=_entra_resource(host, self.config),
        )
        return manifest.render()

    async def _connector_token(self) -> str:
        async with self._token_lock:
            if self.clock() + _TOKEN_REFRESH_SKEW < self._token_expires_at:
                return self._token
            token, expires_in = await asyncio.to_thread(_fetch_connector_token, self.config)
            self._token = token
            self._token_expires_at = self.clock() + expires_in
            return token

    def _idempotency_key(self, activity: TeamsActivity, name: str) -> str:
        return ":".join(
            ("teams", activity.tenant_id, activity.installation_key[1], activity.id, name)
        )

    def _verification(self, activity: Mapping[str, Any], name: str) -> str:
        payload = json.dumps(
            {"activity": activity, "command": name},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hmac.new(self.config.app_secret.encode(), payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class TeamsManifest:
    package_id: str
    app_id: str
    version: str
    name: str
    short_description: str
    long_description: str
    developer_name: str
    website_url: str
    privacy_url: str
    terms_url: str
    scopes: tuple[str, ...]
    commands: tuple[Mapping[str, str], ...] = ()
    entra_resource: str | None = None

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError("Teams manifest requires at least one bot scope")
        invalid = set(self.scopes) - _SCOPES
        if invalid:
            raise ValueError("Teams bot scopes must be personal, team, or groupChat")
        for label, url in (
            ("website_url", self.website_url),
            ("privacy_url", self.privacy_url),
            ("terms_url", self.terms_url),
        ):
            _require_https(url, label)
        for command in self.commands:
            title = command.get("title")
            description = command.get("description")
            if not isinstance(title, str):
                raise ValueError("each Teams command needs a title of at most 32 characters")
            if not title or len(title) > 32:
                raise ValueError("each Teams command needs a title of at most 32 characters")
            if not isinstance(description, str) or not description:
                raise ValueError("each Teams command needs a title of at most 32 characters")
        if self.entra_resource is not None and self.app_id not in self.entra_resource:
            raise ValueError("Teams entra_resource must identify this app_id")

    def render(self) -> dict[str, Any]:
        host = urllib.parse.urlsplit(self.website_url).hostname
        bot = {
            "botId": self.app_id,
            "scopes": list(self.scopes),
            "supportsFiles": False,
            "isNotificationOnly": False,
            "commandLists": [
                {
                    "scopes": list(self.scopes),
                    "commands": [dict(command) for command in self.commands],
                }
            ],
        }
        document: dict[str, Any] = {
            "$schema": _MANIFEST_SCHEMA,
            "manifestVersion": "1.30",
            "version": self.version,
            "id": self.package_id,
            "packageName": f"com.wreath.{self.package_id}",
            "developer": {
                "name": self.developer_name,
                "websiteUrl": self.website_url,
                "privacyUrl": self.privacy_url,
                "termsOfUseUrl": self.terms_url,
            },
            "name": {"short": self.name, "full": self.name},
            "description": {
                "short": self.short_description,
                "full": self.long_description,
            },
            "icons": {"outline": "outline.png", "color": "color.png"},
            "accentColor": "#7C3AED",
            "bots": [bot],
            "permissions": ["identity"],
            "validDomains": [host, "token.botframework.com"],
        }
        if self.entra_resource is not None:
            document["webApplicationInfo"] = {
                "id": self.app_id,
                "resource": self.entra_resource,
            }
        return document

    def package(self, *, color_icon: bytes, outline_icon: bytes) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(self.render(), separators=(",", ":"), ensure_ascii=False),
            )
            archive.writestr("color.png", color_icon)
            archive.writestr("outline.png", outline_icon)
        return output.getvalue()


def _entra_resource(host: str | None, config: TeamsBotConfig) -> str | None:
    if not config.login_issuers:
        return None
    return f"api://{host}/{config.app_id}"


def _required_text(mapping: Mapping[str, Any], key: str, reason: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise TeamsRefusal(reason, f"Teams activity must include a non-empty {label}")
    return value


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _require_https(url: str, label: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Teams {label} must be an absolute HTTPS URL")


def _same_origin(url: str, origin: tuple[str, str, int]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        return False
    try:
        port = parsed.port or 443
    except ValueError:
        return False
    return (
        parsed.scheme,
        hostname.lower(),
        port,
    ) == origin and parsed.username is None


def _bearer_token(header: str | None) -> str:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise TeamsRefusal("missing-authorization", "a Bot Connector Bearer token is required")
    return header[7:].strip()


def _trusted_service_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise TeamsRefusal(
            "untrusted-service-url",
            "Teams serviceUrl must be a trusted Bot Connector HTTPS endpoint",
            status=403,
        )
    host = hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or host != "smba.trafficmanager.net"
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise TeamsRefusal(
            "untrusted-service-url",
            "Teams serviceUrl must be a trusted Bot Connector HTTPS endpoint",
            status=403,
        )
    if not url.endswith("/"):
        raise TeamsRefusal("untrusted-service-url", "Teams serviceUrl must end in '/'", status=403)


def _command(activity: TeamsActivity) -> tuple[str, dict[str, Any]]:
    if activity.value:
        verb = activity.value.get("verb")
        if isinstance(verb, str) and verb:
            return verb, {key: value for key, value in activity.value.items() if key != "verb"}
    text = (activity.text or "").strip()
    if not text:
        return "", {}
    parts = text.split()
    if parts[0].lower() == "run" and len(parts) > 1:
        return parts[1], {"args": parts[2:]} if len(parts) > 2 else {}
    return parts[0], {"args": parts[1:]} if len(parts) > 1 else {}


def _declaration(chat: Any, name: str) -> Any:
    lookup = getattr(chat, "_command", None)
    if lookup is not None:
        return lookup(name)
    commands = getattr(chat, "commands", getattr(chat, "_commands", {}))
    return commands.get(name) if isinstance(commands, Mapping) else None


def _handler_arguments(chat: Any, name: str, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    declaration = _declaration(chat, name)
    parameters = getattr(declaration, "parameters", ())
    external = tuple(
        parameter for parameter in parameters if parameter.name not in _CONTEXT_PARAMETERS
    )
    if (
        set(inputs) == {"args"}
        and len(external) == 1
        and external[0].name == "prompt"
        and isinstance(inputs["args"], list)
        and all(isinstance(item, str) for item in inputs["args"])
    ):
        return {"prompt": " ".join(inputs["args"])}
    return inputs if external else {}


def _job_name(chat_name: str, command_name: str) -> str:
    digest = hashlib.sha256(f"{chat_name}\0{command_name}".encode()).hexdigest()[:20]
    return f"chat_teams_{digest}"


def _outbound_reply(reply: Any) -> dict[str, Any]:
    adaptive_card = getattr(reply, "adaptive_card", None)
    if isinstance(adaptive_card, Mapping):
        return {
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": _teams_card(reply.for_provider("teams")),
                }
            ]
        }
    content = getattr(reply, "content", None)
    if isinstance(content, str):
        return {"text": content}
    if isinstance(reply, str):
        return {"text": reply}
    raise TypeError("Teams replies must be ChatReply.text() or ChatReply.card()")


def _teams_card(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Adaptive Card content must be an object")
    card = json.loads(json.dumps(value))
    if card.get("type") != "AdaptiveCard" or card.get("version") != "1.5":
        raise ValueError("Teams requires Adaptive Card version 1.5")
    actions = card.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict) or action.get("type") != "Action.Execute":
                continue
            if "fallback" in action:
                continue
            fallback = {
                "type": "Action.Submit",
                "data": {"verb": action.get("verb")},
            }
            if "title" in action:
                fallback["title"] = action["title"]
            action["fallback"] = fallback
    return card


def _invoke_result(result: Any) -> JSONResponse:
    adaptive_card = getattr(result, "adaptive_card", None)
    if isinstance(adaptive_card, Mapping):
        return _invoke_response(
            200,
            "application/vnd.microsoft.card.adaptive",
            _teams_card(result.for_provider("teams")),
        )
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return _invoke_response(
            200, "application/vnd.microsoft.activity.message", {"text": content}
        )
    if result is None:
        return _invoke_response(200, "application/vnd.microsoft.activity.message", {})
    return _invoke_response(200, "application/vnd.microsoft.activity.message", result)


def _invoke_response(status: int, kind: str, value: Any) -> JSONResponse:
    return JSONResponse({"statusCode": status, "type": kind, "value": value}, status=200)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _fetch_json(url: str) -> Mapping[str, Any]:
    def fetch() -> Mapping[str, Any]:
        parsed = _https_target(url)
        connection = http.client.HTTPSConnection(
            _target_host(parsed), parsed.port or 443, timeout=10
        )
        try:
            connection.request(
                "GET", _request_target(parsed), headers={"accept": "application/json"}
            )
            response = connection.getresponse()
            if response.status != 200:
                raise TeamsRefusal(
                    "metadata-fetch-failed",
                    f"Bot Connector metadata returned HTTP {response.status}",
                )
            body = response.read(512 * 1024 + 1)
        finally:
            connection.close()
        if len(body) > 512 * 1024:
            raise TeamsRefusal("metadata-too-large", "Bot Connector metadata is too large")
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise TeamsRefusal("malformed-metadata", "Bot Connector metadata must be an object")
        return value

    return await asyncio.to_thread(fetch)


class _UrlConnector:
    async def send(self, request: ConnectorRequest) -> Any:
        return await asyncio.to_thread(self._send, request)

    @staticmethod
    def _send(request: ConnectorRequest) -> Any:
        body = json.dumps(request.json, separators=(",", ":")).encode()
        parsed = _https_target(request.url)
        if parsed.hostname != "smba.trafficmanager.net":
            raise TeamsRefusal(
                "untrusted-service-url",
                "Teams connector request must use a trusted serviceUrl",
                status=403,
            )
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=15)
        try:
            connection.request(
                request.method,
                _request_target(parsed),
                body=body,
                headers=dict(request.headers),
            )
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        if response.status >= 400:
            retry = response.getheader("retry-after")
            raise TeamsConnectorError(
                status=response.status,
                retry_after=float(retry) if retry and retry.isdigit() else None,
            )
        return json.loads(raw) if raw else None


def _fetch_connector_token(config: TeamsBotConfig) -> tuple[str, float]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": config.app_id,
            "client_secret": config.app_secret,
            "scope": "https://api.botframework.com/.default",
        }
    ).encode()
    connection = http.client.HTTPSConnection("login.microsoftonline.com", 443, timeout=15)
    try:
        connection.request(
            "POST",
            "/botframework.com/oauth2/v2.0/token",
            body=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise TeamsConnectorError(status=response.status)
    payload = json.loads(raw)
    token = payload.get("access_token") if isinstance(payload, Mapping) else None
    if not isinstance(token, str) or not token:
        raise TeamsConnectorError(status=502)
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float) or expires_in <= 0:
        raise TeamsConnectorError(status=502)
    return token, float(expires_in)


def _https_target(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TeamsRefusal("unsafe-url", "Teams network targets must be absolute HTTPS URLs")
    return parsed


def _request_target(parsed: urllib.parse.SplitResult) -> str:
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _target_host(parsed: urllib.parse.SplitResult) -> str:
    host = parsed.hostname
    if host is None:
        raise TeamsRefusal("unsafe-url", "Teams network target has no hostname")
    return host


__all__ = [
    "BOT_CONNECTOR_ISSUER",
    "BOT_CONNECTOR_METADATA_URL",
    "ConnectorRequest",
    "Teams",
    "TeamsActivity",
    "TeamsBotConfig",
    "TeamsConnectorError",
    "TeamsConnectorVerifier",
    "TeamsContext",
    "TeamsInstallation",
    "TeamsManifest",
    "TeamsRefusal",
]
