from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._agents.remote_mcp import (
    MCPHTTPResponse,
    MCPInvocationAudit,
    MCPRemoteError,
    MCPRemoteScopeError,
    MCPToolCatalogDrift,
    MCPToolLimitExceeded,
    RemoteMCPClient,
    RemoteMCPToolCatalog,
    UnknownToolOutcome,
)


def response(
    result: Mapping[str, Any],
    *,
    request_id: int,
    headers: Mapping[str, str] | None = None,
) -> MCPHTTPResponse:
    return MCPHTTPResponse(
        url="https://tools.example/mcp",
        status=200,
        headers={"content-type": "application/json", **dict(headers or {})},
        body=json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode(),
    )


def initialized(*, session: str = "session-1", request_id: int = 1) -> MCPHTTPResponse:
    return response(
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test", "version": "1"},
        },
        request_id=request_id,
        headers={"mcp-session-id": session},
    )


def tools_page(
    *tools: Mapping[str, Any], request_id: int, cursor: str | None = None
) -> MCPHTTPResponse:
    result: dict[str, Any] = {"tools": list(tools)}
    if cursor is not None:
        result["nextCursor"] = cursor
    return response(result, request_id=request_id)


def tool(name: str, *, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Run {name}",
        "inputSchema": {"type": "object"} if schema is None else schema,
    }


class Transport:
    origin = "https://tools.example"

    def __init__(self, *responses: MCPHTTPResponse | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.response_limits: list[int] = []
        self.closed = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        max_response_bytes: int,
    ) -> MCPHTTPResponse:
        self.calls.append((method, url, dict(headers), body))
        self.response_limits.append(max_response_bytes)
        answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


class TokenProvider:
    def __init__(self) -> None:
        self.origins: list[str] = []

    async def token(self, origin: str) -> str:
        self.origins.append(origin)
        return "access-token"


class Audit:
    def __init__(self) -> None:
        self.events: list[MCPInvocationAudit] = []

    async def record(self, event: MCPInvocationAudit) -> None:
        self.events.append(event)


def payload(call: tuple[str, str, dict[str, str], bytes | None]) -> dict[str, Any]:
    assert call[3] is not None
    return json.loads(call[3])


def context() -> SimpleNamespace:
    return SimpleNamespace(
        tenant="tenant-secret",
        principal=SimpleNamespace(id="user-secret"),
        conversation="conversation-1",
        correlation_id="trace-1",
    )


@pytest.mark.asyncio
async def test_connect_initializes_notifies_and_compiles_bounded_paginated_tools_once() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=2, cursor="page-2"),
        tools_page(tool("release"), request_id=3),
    )
    tokens = TokenProvider()
    client = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=transport,
        token_provider=tokens,
        max_tools=2,
        max_pages=2,
    )

    await client.connect()
    first_specifications = client.specifications
    await client.connect()

    assert [item.name for item in first_specifications] == ["lookup", "release"]
    assert client.specifications is first_specifications
    assert client.protocol_version == "2025-11-25"
    assert client.session_id == "session-1"
    assert len(transport.calls) == 4
    assert [payload(call).get("id") for call in transport.calls if call[3]] == [1, None, 2, 3]
    assert payload(transport.calls[0])["method"] == "initialize"
    assert payload(transport.calls[1]) == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    assert payload(transport.calls[3])["params"] == {"cursor": "page-2"}
    for _method, url, headers, _body in transport.calls:
        assert url == "https://tools.example/mcp"
        assert headers["authorization"] == "Bearer access-token"
        assert "tenant" not in repr(headers).lower()
        assert "principal" not in repr(headers).lower()
    assert transport.calls[2][2]["mcp-session-id"] == "session-1"
    assert transport.calls[2][2]["mcp-protocol-version"] == "2025-11-25"
    assert tokens.origins == ["https://tools.example"] * 4


def test_catalog_select_is_synchronous_only_after_connect_and_refuses_collisions() -> None:
    one = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=Transport(initialized()),
        name="one",
    )
    catalog = RemoteMCPToolCatalog((one,), max_clients=1)

    with pytest.raises(RuntimeError, match="connect"):
        catalog.select(("lookup",))
    with pytest.raises(ValueError, match="duplicate remote MCP client"):
        RemoteMCPToolCatalog((one, one), max_clients=2)


@pytest.mark.asyncio
async def test_catalog_refuses_tool_name_collisions_across_origins() -> None:
    first = RemoteMCPClient(
        "https://tools.example/mcp",
        name="first",
        transport=Transport(
            initialized(session="one"),
            MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
            tools_page(tool("same"), request_id=2),
            MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
        ),
    )
    second_transport = Transport(
        replace(initialized(session="two"), url="https://other.example/mcp"),
        MCPHTTPResponse("https://other.example/mcp", 202, {}, b""),
        replace(tools_page(tool("same"), request_id=2), url="https://other.example/mcp"),
        MCPHTTPResponse("https://other.example/mcp", 204, {}, b""),
    )
    second_transport.origin = "https://other.example"
    second = RemoteMCPClient("https://other.example/mcp", name="second", transport=second_transport)

    with pytest.raises(ValueError, match="tool collision.*same"):
        await RemoteMCPToolCatalog((first, second), max_clients=2).connect()
    assert first._transport.closed is True
    assert second_transport.closed is True
    assert first.session_id is None
    assert second.session_id is None


@pytest.mark.asyncio
async def test_discovery_failure_closes_the_negotiated_session_and_transport() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("one"), tool("two"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )
    client = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=transport,
        max_tools=1,
    )

    with pytest.raises(MCPToolLimitExceeded, match="tool ceiling"):
        await client.connect()

    assert transport.calls[-1][0] == "DELETE"
    assert transport.closed is True
    assert client.session_id is None
    assert client.protocol_version is None


@pytest.mark.asyncio
async def test_session_delete_failure_still_closes_transport_and_resets_client() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("one"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 500, {}, b""),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    await client.connect()

    with pytest.raises(MCPRemoteError, match="session close failed"):
        await client.close()

    assert transport.closed is True
    assert client.session_id is None
    assert client.protocol_version is None


@pytest.mark.asyncio
async def test_tool_and_schema_bounds_and_catalogue_drift_refuse() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("one"), tool("two"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport, max_tools=1)
    with pytest.raises(MCPToolLimitExceeded, match="tool ceiling"):
        await client.connect()

    drift_transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("one"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
        initialized(session="session-2", request_id=3),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("changed"), request_id=4),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )
    stable = RemoteMCPClient("https://tools.example/mcp", transport=drift_transport)
    await stable.connect()
    await stable.close()
    with pytest.raises(MCPToolCatalogDrift, match="catalogue drift"):
        await stable.connect()


@pytest.mark.asyncio
async def test_invoke_sends_effect_identity_without_tenant_headers_and_normalizes_sse() -> None:
    sse = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    sse += (
        b"event: message\ndata: "
        b'{"jsonrpc":"2.0","id":3,"result":{"content":'
        b'[{"type":"text","text":"ok"}],"isError":false}}\n\n'
    )
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=2),
        MCPHTTPResponse(
            "https://tools.example/mcp",
            200,
            {"content-type": "text/event-stream"},
            sse,
        ),
    )
    audit = Audit()
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport, audit=audit)
    catalog = RemoteMCPToolCatalog((client,), max_clients=1)
    await catalog.connect()
    selected = catalog.select(("lookup",))

    result = await selected.invoke("lookup", {"key": "value"}, call_id="call-7", context=context())

    request = payload(transport.calls[-1])
    assert request["id"] == 3
    assert request["method"] == "tools/call"
    assert request["params"]["name"] == "lookup"
    assert request["params"]["arguments"] == {"key": "value"}
    assert request["params"]["_meta"]["wreath/callId"] == "call-7"
    assert request["params"]["_meta"]["wreath/effectId"]
    assert "tenant-secret" not in repr(transport.calls[-1])
    assert "user-secret" not in repr(transport.calls[-1])
    assert result["content"][0]["text"] == "ok"
    assert audit.events[0].tenant == "tenant-secret"
    assert audit.events[0].principal_id == "user-secret"
    assert audit.events[0].outcome == "started"


@pytest.mark.asyncio
async def test_client_session_is_permanently_bound_to_one_tenant_and_principal() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=2),
        response({"content": []}, request_id=3),
    )
    audit = Audit()
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport, audit=audit)
    await client.connect()

    await client.invoke("lookup", {}, call_id="first", context=context())
    calls = len(transport.calls)
    events = len(audit.events)
    for changed in (
        SimpleNamespace(
            tenant="other-tenant",
            principal=SimpleNamespace(id="user-secret"),
            conversation="conversation-1",
            correlation_id="trace-1",
        ),
        SimpleNamespace(
            tenant="tenant-secret",
            principal=SimpleNamespace(id="other-principal"),
            conversation="conversation-1",
            correlation_id="trace-1",
        ),
    ):
        with pytest.raises(MCPRemoteScopeError, match="construct a separate client"):
            await client.invoke("lookup", {}, call_id="foreign", context=changed)

    assert len(transport.calls) == calls
    assert len(audit.events) == events


@pytest.mark.asyncio
async def test_unknown_tool_outcome_is_not_retried() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("charge"), request_id=2),
        UnknownToolOutcome("connection ended after write"),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    catalog = RemoteMCPToolCatalog((client,), max_clients=1)
    await catalog.connect()

    with pytest.raises(UnknownToolOutcome, match="after write"):
        await catalog.select(("charge",)).invoke("charge", {}, call_id="call-9", context=context())
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_response_bound_redirect_and_protocol_errors_refuse() -> None:
    oversized = MCPHTTPResponse(
        "https://tools.example/mcp",
        200,
        {"content-type": "application/json"},
        b"x" * 65,
    )
    with pytest.raises(MCPRemoteError, match="response byte ceiling"):
        await RemoteMCPClient(
            "https://tools.example/mcp",
            transport=Transport(oversized),
            max_response_bytes=64,
        ).connect()

    redirected = initialized()
    redirected = MCPHTTPResponse(
        "https://evil.example/mcp", redirected.status, redirected.headers, redirected.body
    )
    with pytest.raises(MCPRemoteError, match="origin-pinned"):
        await RemoteMCPClient(
            "https://tools.example/mcp", transport=Transport(redirected)
        ).connect()


@pytest.mark.asyncio
async def test_cancellation_sends_cancel_notification_and_close_terminates_session() -> None:
    started = asyncio.Event()

    class BlockingTransport(Transport):
        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None,
            max_response_bytes: int,
        ) -> MCPHTTPResponse:
            if body is not None and json.loads(body).get("method") == "tools/call":
                self.calls.append((method, url, dict(headers), body))
                started.set()
                await asyncio.Future()
            return await super().request(
                method,
                url,
                headers=headers,
                body=body,
                max_response_bytes=max_response_bytes,
            )

    transport = BlockingTransport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("wait"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    catalog = RemoteMCPToolCatalog((client,), max_clients=1)
    await catalog.connect()
    task = asyncio.create_task(
        catalog.select(("wait",)).invoke("wait", {}, call_id="call-cancel", context=context())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancellation = payload(transport.calls[-1])
    assert cancellation["method"] == "notifications/cancelled"
    assert cancellation["params"]["requestId"] == 3

    await client.close()
    assert transport.calls[-1][0] == "DELETE"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_concurrent_cancellation_uses_the_invocation_reserved_request_id() -> None:
    first_audit = asyncio.Event()
    release_first_audit = asyncio.Event()
    one_call_started = asyncio.Event()
    two_calls_started = asyncio.Event()

    class SuspendedAudit(Audit):
        async def record(self, event: MCPInvocationAudit) -> None:
            self.events.append(event)
            if event.outcome == "started" and not first_audit.is_set():
                first_audit.set()
                await release_first_audit.wait()

    class BlockingTransport(Transport):
        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None,
            max_response_bytes: int,
        ) -> MCPHTTPResponse:
            message = None if body is None else json.loads(body)
            if message is not None and message.get("method") == "tools/call":
                self.calls.append((method, url, dict(headers), body))
                call_count = sum(payload(call).get("method") == "tools/call" for call in self.calls)
                one_call_started.set()
                if call_count == 2:
                    two_calls_started.set()
                await asyncio.Future()
            return await super().request(
                method,
                url,
                headers=headers,
                body=body,
                max_response_bytes=max_response_bytes,
            )

    audit = SuspendedAudit()
    transport = BlockingTransport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("wait"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
    )
    client = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=transport,
        audit=audit,
    )
    catalog = RemoteMCPToolCatalog((client,), max_clients=1)
    await catalog.connect()
    selected = catalog.select(("wait",))

    first = asyncio.create_task(
        selected.invoke("wait", {}, call_id="call-first", context=context())
    )
    await first_audit.wait()
    second = asyncio.create_task(
        selected.invoke("wait", {}, call_id="call-second", context=context())
    )
    await one_call_started.wait()
    release_first_audit.set()
    await two_calls_started.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    calls = [payload(call) for call in transport.calls]
    first_call = next(
        call
        for call in calls
        if call.get("method") == "tools/call"
        and call["params"]["_meta"]["wreath/callId"] == "call-first"
    )
    cancellations = [call for call in calls if call.get("method") == "notifications/cancelled"]
    assert cancellations[0]["params"]["requestId"] == first_call["id"]


@pytest.mark.asyncio
async def test_cancellation_cleanup_failure_never_replaces_cancellation() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("wait"), request_id=2),
        asyncio.CancelledError(),
        OSError("notification unavailable"),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    catalog = RemoteMCPToolCatalog((client,), max_clients=1)
    await catalog.connect()

    with pytest.raises(asyncio.CancelledError):
        await catalog.select(("wait",)).invoke("wait", {}, call_id="call-cancel", context=context())

    assert client.cancellation_errors == 1
