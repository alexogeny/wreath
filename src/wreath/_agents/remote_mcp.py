from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .identity import principal_id

PROTOCOL_VERSION = "2025-11-25"


class MCPRemoteError(RuntimeError):
    pass


class MCPRemoteScopeError(MCPRemoteError):
    pass


class MCPToolLimitExceeded(MCPRemoteError):
    pass


class MCPToolCatalogDrift(MCPRemoteError):
    pass


class UnknownToolOutcome(MCPRemoteError):
    pass


@dataclass(frozen=True, slots=True)
class MCPHTTPResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes


@runtime_checkable
class MCPHTTPTransport(Protocol):
    origin: str

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        max_response_bytes: int,
    ) -> MCPHTTPResponse: ...

    async def close(self) -> None: ...


@runtime_checkable
class MCPTokenProvider(Protocol):
    async def token(self, origin: str) -> str: ...


@dataclass(frozen=True, slots=True)
class MCPInvocationAudit:
    client: str
    tool: str
    call_id: str
    effect_id: str
    tenant: str
    principal_id: str
    conversation: str
    correlation_id: str | None
    outcome: str


@runtime_checkable
class MCPInvocationObserver(Protocol):
    async def record(self, event: MCPInvocationAudit) -> None: ...


async def _captured(awaitable: Awaitable[Any]) -> BaseException | None:
    outcome = (await asyncio.gather(awaitable, return_exceptions=True))[0]
    return outcome if isinstance(outcome, BaseException) else None


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("remote MCP endpoint must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("remote MCP endpoint must not contain userinfo")
    if parsed.fragment:
        raise ValueError("remote MCP endpoint must not contain a fragment")
    port = "" if parsed.port in (None, 443) else f":{parsed.port}"
    return f"https://{parsed.hostname.lower()}{port}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _effect_id(endpoint: str, tool: str, call_id: str, tenant: str, resolved_principal: str) -> str:
    digest = hashlib.sha256(b"wreath.remote-mcp.effect.v2")
    for value in (endpoint, tool, call_id, tenant, resolved_principal):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _json(value: Any) -> bytes:
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    except (TypeError, ValueError) as error:
        raise MCPRemoteError(f"remote MCP message is not JSON serializable: {error}") from error


def _json_object(body: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MCPRemoteError(f"remote MCP returned invalid JSON: {error}") from error
    if not isinstance(decoded, Mapping):
        raise MCPRemoteError("remote MCP response must be a JSON object")
    return decoded


def _sse_objects(body: bytes, *, max_events: int) -> tuple[Mapping[str, Any], ...]:
    try:
        text = body.decode()
    except UnicodeDecodeError as error:
        raise MCPRemoteError("remote MCP SSE response is not UTF-8") from error
    messages: list[Mapping[str, Any]] = []
    data: list[str] = []
    for line in (*text.splitlines(), ""):
        if line == "":
            if not data:
                continue
            messages.append(_json_object("\n".join(data).encode()))
            if len(messages) > max_events:
                raise MCPRemoteError(f"remote MCP SSE response exceeds event ceiling {max_events}")
            data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    return tuple(messages)


def _response_message(
    response: MCPHTTPResponse,
    *,
    request_id: int,
    max_events: int,
) -> Mapping[str, Any]:
    content_type = (_header(response.headers, "content-type") or "").split(";", 1)[0]
    if content_type == "application/json":
        messages = (_json_object(response.body),)
    elif content_type == "text/event-stream":
        messages = _sse_objects(response.body, max_events=max_events)
    else:
        raise MCPRemoteError(
            f"remote MCP response content-type must be application/json or "
            f"text/event-stream, not {content_type!r}"
        )
    for message in messages:
        if message.get("id") == request_id:
            return message
    raise MCPRemoteError(f"remote MCP response did not contain request id {request_id}")


class RemoteMCPClient:
    __slots__ = (
        "_audit",
        "_catalog_fingerprint",
        "_connected",
        "_endpoint",
        "_max_events",
        "_max_pages",
        "_max_response_bytes",
        "_max_schema_bytes",
        "_max_tools",
        "_last_response",
        "_next_id",
        "_origin",
        "_protocol_version",
        "_session_id",
        "_scope",
        "_specifications",
        "_token_provider",
        "_transport",
        "cancellation_errors",
        "name",
    )

    def __init__(
        self,
        endpoint: str,
        *,
        transport: MCPHTTPTransport,
        name: str = "remote",
        token_provider: MCPTokenProvider | None = None,
        audit: MCPInvocationObserver | None = None,
        max_tools: int = 128,
        max_pages: int = 16,
        max_schema_bytes: int = 64 * 1024,
        max_response_bytes: int = 4 * 1024 * 1024,
        max_sse_events: int = 256,
    ) -> None:
        if not name:
            raise ValueError("remote MCP client name must be non-empty")
        if (
            min(
                max_tools,
                max_pages,
                max_schema_bytes,
                max_response_bytes,
                max_sse_events,
            )
            < 1
        ):
            raise ValueError("remote MCP limits must be positive")
        origin = _origin(endpoint)
        if transport.origin.rstrip("/") != origin:
            raise ValueError(
                f"remote MCP transport origin {transport.origin!r} does not match "
                f"endpoint origin {origin!r}"
            )
        self.name = name
        self._endpoint = endpoint
        self._origin = origin
        self._transport = transport
        self._token_provider = token_provider
        self._audit = audit
        self._max_tools = max_tools
        self._max_pages = max_pages
        self._max_schema_bytes = max_schema_bytes
        self._max_response_bytes = max_response_bytes
        self._max_events = max_sse_events
        self._next_id = 1
        self._last_response: MCPHTTPResponse | None = None
        self._connected = False
        self._protocol_version: str | None = None
        self._session_id: str | None = None
        self._scope: tuple[str, str] | None = None
        self._specifications: tuple[Any, ...] | None = None
        self._catalog_fingerprint: str | None = None
        self.cancellation_errors = 0

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def specifications(self) -> tuple[Any, ...]:
        if not self._connected or self._specifications is None:
            raise RuntimeError("remote MCP client must connect before selecting tools")
        return self._specifications

    async def _headers(self, *, initialized: bool) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if initialized:
            protocol = self._protocol_version
            if protocol is None:
                raise RuntimeError("remote MCP protocol version is not initialized")
            headers["mcp-protocol-version"] = protocol
            if self._session_id is not None:
                headers["mcp-session-id"] = self._session_id
        if self._token_provider is not None:
            token = await self._token_provider.token(self._origin)
            if not token:
                raise MCPRemoteError("remote MCP token provider returned an empty token")
            headers["authorization"] = f"Bearer {token}"
        return headers

    async def _post(self, message: Mapping[str, Any], *, initialized: bool) -> MCPHTTPResponse:
        body = _json(message)
        response = await self._transport.request(
            "POST",
            self._endpoint,
            headers=await self._headers(initialized=initialized),
            body=body,
            max_response_bytes=self._max_response_bytes,
        )
        if response.url != self._endpoint:
            raise MCPRemoteError(
                f"remote MCP transport is origin-pinned to {self._endpoint!r}, "
                f"not response URL {response.url!r}"
            )
        if len(response.body) > self._max_response_bytes:
            raise MCPRemoteError(
                f"remote MCP response exceeds response byte ceiling {self._max_response_bytes}"
            )
        if response.status >= 400:
            raise MCPRemoteError(f"remote MCP HTTP request failed with status {response.status}")
        self._last_response = response
        return response

    def _reserve_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        initialized: bool = True,
        request_id: int | None = None,
    ) -> Mapping[str, Any]:
        if request_id is None:
            request_id = self._reserve_request_id()
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            initialized=initialized,
        )
        message = _response_message(
            response,
            request_id=request_id,
            max_events=self._max_events,
        )
        error = message.get("error")
        if error is not None:
            raise MCPRemoteError(f"remote MCP {method} returned JSON-RPC error {error!r}")
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise MCPRemoteError(f"remote MCP {method} result must be an object")
        return result

    async def _notification(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        response = await self._post(message, initialized=True)
        if response.status != 202:
            raise MCPRemoteError(
                f"remote MCP notification {method} failed with status {response.status}"
            )

    async def connect(self) -> None:
        if self._connected:
            return
        connected = False
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "wreath", "version": "1"},
                },
                initialized=False,
            )
            protocol = result.get("protocolVersion")
            if protocol != PROTOCOL_VERSION:
                raise MCPRemoteError(
                    f"remote MCP negotiated unsupported protocol version {protocol!r}; "
                    f"supported: {PROTOCOL_VERSION}"
                )
            capabilities = result.get("capabilities")
            if not isinstance(capabilities, Mapping) or not isinstance(
                capabilities.get("tools"), Mapping
            ):
                raise MCPRemoteError("remote MCP server did not negotiate the tools capability")
            session = self._session_from_initialize()
            self._protocol_version = protocol
            self._session_id = session
            await self._notification("notifications/initialized")
            tools = await self._list_tools()
            fingerprint = hashlib.sha256(_json(tools)).hexdigest()
            if self._catalog_fingerprint is not None and fingerprint != self._catalog_fingerprint:
                raise MCPToolCatalogDrift(
                    f"remote MCP client {self.name!r} catalogue drifted after startup"
                )
            if self._specifications is None:
                self._specifications = self._compile(tools)
                self._catalog_fingerprint = fingerprint
            self._connected = True
            connected = True
        finally:
            if not connected:
                error = sys.exception()
                cleanup_error = await _captured(self.close())
                if cleanup_error is not None:
                    if error is None:
                        raise cleanup_error
                    error.add_note(f"remote MCP connection cleanup failed: {cleanup_error}")

    def _session_from_initialize(self) -> str | None:
        response = self._last_response
        if response is None:
            return None
        session = _header(response.headers, "mcp-session-id")
        if session is not None and (
            not session
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in session)
        ):
            raise MCPRemoteError("remote MCP session ID must contain visible ASCII only")
        return session

    async def _list_tools(self) -> tuple[dict[str, Any], ...]:
        found: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _page in range(self._max_pages):
            params = {} if cursor is None else {"cursor": cursor}
            result = await self._request("tools/list", params)
            page = result.get("tools")
            if not isinstance(page, Sequence) or isinstance(page, str | bytes):
                raise MCPRemoteError("remote MCP tools/list result requires a tools array")
            for raw in page:
                normalized = self._normalize_tool(raw)
                name = normalized["name"]
                if name in seen_names:
                    raise MCPRemoteError(f"duplicate remote MCP tool name {name!r}")
                seen_names.add(name)
                found.append(normalized)
                if len(found) > self._max_tools:
                    raise MCPToolLimitExceeded(
                        f"remote MCP catalogue exceeds tool ceiling {self._max_tools}"
                    )
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tuple(sorted(found, key=lambda item: item["name"]))
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPRemoteError("remote MCP nextCursor must be a non-empty string")
            if next_cursor in seen_cursors:
                raise MCPRemoteError(f"remote MCP tools/list repeated cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPToolLimitExceeded(f"remote MCP tools/list exceeds page ceiling {self._max_pages}")

    def _normalize_tool(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise MCPRemoteError("remote MCP tool declaration must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise MCPRemoteError("remote MCP tool name must be a non-empty string")
        schema = raw.get("inputSchema")
        if not isinstance(schema, Mapping):
            raise MCPRemoteError(f"remote MCP tool {name!r} inputSchema must be an object")
        encoded = _json(schema)
        if len(encoded) > self._max_schema_bytes:
            raise MCPToolLimitExceeded(
                f"remote MCP tool {name!r} schema exceeds schema byte ceiling "
                f"{self._max_schema_bytes}"
            )
        description = raw.get("description") or raw.get("title") or name
        if not isinstance(description, str):
            raise MCPRemoteError(f"remote MCP tool {name!r} description must be text")
        return {"name": name, "description": description, "inputSchema": dict(schema)}

    @staticmethod
    def _compile(tools: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
        from .core import ToolSpecification

        return tuple(
            ToolSpecification(item["name"], item["description"], item["inputSchema"])
            for item in tools
        )

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: Any,
    ) -> Mapping[str, Any]:
        tenant = getattr(context, "tenant", None)
        if not isinstance(tenant, str) or not tenant:
            raise MCPRemoteScopeError("remote MCP invocation requires a non-empty tenant")
        resolved_principal = principal_id(context.principal, label="remote MCP")
        scope = (tenant, resolved_principal)
        if self._scope is None:
            self._scope = scope
        elif self._scope != scope:
            raise MCPRemoteScopeError(
                f"remote MCP client {self.name!r} is bound to tenant/principal "
                f"{self._scope!r}, not {scope!r}; construct a separate client, "
                "transport, and token provider for each tenant/principal"
            )
        known = {item.name for item in self.specifications}
        if name not in known:
            raise LookupError(f"remote MCP client {self.name!r} has no tool {name!r}")
        effect_id = _effect_id(self._endpoint, name, call_id, tenant, resolved_principal)
        request_id = self._reserve_request_id()
        await self._audit_event(
            context,
            name=name,
            call_id=call_id,
            effect_id=effect_id,
            principal=resolved_principal,
            outcome="started",
        )
        try:
            result = await self._request(
                "tools/call",
                {
                    "name": name,
                    "arguments": dict(arguments),
                    "_meta": {
                        "wreath/callId": call_id,
                        "wreath/effectId": effect_id,
                    },
                },
                request_id=request_id,
            )
        except asyncio.CancelledError:
            cancellation = asyncio.create_task(
                self._notification(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "client cancelled"},
                )
            )
            try:
                async with asyncio.timeout(1.0):
                    await asyncio.shield(cancellation)
            except MCPRemoteError, OSError, RuntimeError, TimeoutError, TypeError, ValueError:
                self.cancellation_errors += 1
                cancellation.cancel()
            raise
        return result

    async def _audit_event(
        self,
        context: Any,
        *,
        name: str,
        call_id: str,
        effect_id: str,
        principal: str,
        outcome: str,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            MCPInvocationAudit(
                self.name,
                name,
                call_id,
                effect_id,
                context.tenant,
                principal,
                context.conversation,
                context.correlation_id,
                outcome,
            )
        )

    async def _close_session(self) -> None:
        if self._session_id is None:
            return
        response = await self._transport.request(
            "DELETE",
            self._endpoint,
            headers=await self._headers(initialized=True),
            body=None,
            max_response_bytes=self._max_response_bytes,
        )
        if response.status not in (200, 202, 204, 405):
            raise MCPRemoteError(f"remote MCP session close failed with status {response.status}")

    async def close(self) -> None:
        session_failure: BaseException | None = None
        transport_failure: BaseException | None = None
        try:
            session_failure = await _captured(self._close_session())
        finally:
            try:
                transport_failure = await _captured(self._transport.close())
            finally:
                self._connected = False
                self._protocol_version = None
                self._session_id = None
        failure = session_failure or transport_failure
        if session_failure is not None and transport_failure is not None:
            session_failure.add_note(f"remote MCP transport close also failed: {transport_failure}")
        if failure is not None:
            raise failure


class RemoteMCPToolSet:
    __slots__ = ("_owners", "specifications")

    def __init__(self, owners: Mapping[str, RemoteMCPClient], names: tuple[str, ...]) -> None:
        self._owners = {name: owners[name] for name in names}
        by_name = {
            specification.name: specification
            for owner in self._owners.values()
            for specification in owner.specifications
        }
        self.specifications = tuple(by_name[name] for name in names)

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: Any,
    ) -> Mapping[str, Any]:
        try:
            owner = self._owners[name]
        except KeyError:
            raise LookupError(f"remote MCP tool set does not contain {name!r}") from None
        return await owner.invoke(name, arguments, call_id=call_id, context=context)


class RemoteMCPToolCatalog:
    __slots__ = ("_clients", "_connected", "_owners")

    def __init__(self, clients: Sequence[RemoteMCPClient], *, max_clients: int = 16) -> None:
        if max_clients < 1:
            raise ValueError("remote MCP max_clients must be positive")
        if len(clients) > max_clients:
            raise MCPToolLimitExceeded(f"remote MCP catalog exceeds client ceiling {max_clients}")
        names = [client.name for client in clients]
        if len(names) != len(set(names)):
            raise ValueError("duplicate remote MCP client name")
        self._clients = tuple(clients)
        self._owners: dict[str, RemoteMCPClient] = {}
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        owners: dict[str, RemoteMCPClient] = {}
        connected: list[RemoteMCPClient] = []
        complete = False
        try:
            for client in self._clients:
                await client.connect()
                connected.append(client)
                for specification in client.specifications:
                    if specification.name in owners:
                        raise ValueError(f"remote MCP tool collision for {specification.name!r}")
                    owners[specification.name] = client
            self._owners = owners
            self._connected = True
            complete = True
        finally:
            if not complete:
                error = sys.exception()
                for client in reversed(connected):
                    cleanup_error = await _captured(client.close())
                    if cleanup_error is not None:
                        if error is None:
                            raise cleanup_error
                        error.add_note(
                            f"remote MCP client {client.name!r} rollback failed: {cleanup_error}"
                        )

    def select(self, names: tuple[str, ...]) -> RemoteMCPToolSet:
        if not self._connected:
            raise RuntimeError("remote MCP catalog must connect before select")
        if len(names) != len(set(names)):
            raise ValueError("remote MCP tool selection contains duplicates")
        missing = tuple(name for name in names if name not in self._owners)
        if missing:
            raise LookupError(f"unknown remote MCP tools: {', '.join(missing)}")
        return RemoteMCPToolSet(self._owners, names)


__all__ = [
    "MCPHTTPResponse",
    "MCPHTTPTransport",
    "MCPInvocationAudit",
    "MCPInvocationObserver",
    "MCPRemoteError",
    "MCPRemoteScopeError",
    "MCPTokenProvider",
    "MCPToolCatalogDrift",
    "MCPToolLimitExceeded",
    "PROTOCOL_VERSION",
    "RemoteMCPClient",
    "RemoteMCPToolCatalog",
    "RemoteMCPToolSet",
    "UnknownToolOutcome",
]
