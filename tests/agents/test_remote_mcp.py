from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

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
    _origin,
    _response_message,
    _sse_objects,
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
    def __init__(self, value: str = "access-token") -> None:
        self.origins: list[str] = []
        self.value = value

    async def token(self, origin: str) -> str:
        self.origins.append(origin)
        return self.value


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


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://tools.example/mcp", "absolute HTTPS"),
        ("/mcp", "absolute HTTPS"),
        ("https:///mcp", "absolute HTTPS"),
        ("https://user@tools.example/mcp", "userinfo"),
        ("https://tools.example/mcp#fragment", "fragment"),
    ],
)
def test_remote_origin_refuses_each_invalid_endpoint_part(
    endpoint: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _origin(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://tools.example\r.evil/mcp",
        "https://tools.example/mcp\t/../../admin",
        "https://tools.example/mcp\x7fadmin",
    ],
)
def test_remote_origin_refuses_parser_control_ambiguity(endpoint: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTPS"):
        _origin(endpoint)


def test_remote_origin_requires_text() -> None:
    with pytest.raises(ValueError, match="absolute HTTPS"):
        _origin(cast(Any, 7))


def test_remote_origin_normalizes_default_and_preserves_nondefault_ports() -> None:
    assert _origin("https://tools.example:443/mcp") == "https://tools.example"
    assert _origin("https://tools.example:8443/mcp") == "https://tools.example:8443"


def test_remote_origin_normalizes_ipv6_and_idna_for_credential_pinning() -> None:
    assert _origin("https://[2001:db8::1]:443/mcp") == "https://[2001:db8::1]"
    assert _origin("https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example/mcp") == (
        "https://xn--bcher-kva.example"
    )


def test_remote_origin_refuses_an_unencodable_hostname_with_the_correct_form() -> None:
    with pytest.raises(ValueError, match="absolute HTTPS"):
        _origin("https://\ud800.example/mcp")


def test_remote_origin_names_the_correct_form_for_an_invalid_port() -> None:
    with pytest.raises(ValueError, match="absolute HTTPS"):
        _origin("https://tools.example:not-a-port/mcp")


@pytest.mark.parametrize(
    "option",
    ["max_tools", "max_pages", "max_schema_bytes", "max_response_bytes", "max_sse_events"],
)
def test_client_refuses_each_non_positive_limit(option: str) -> None:
    with pytest.raises(ValueError, match="limits must be positive"):
        RemoteMCPClient(
            "https://tools.example/mcp",
            transport=Transport(),
            **{option: 0},
        )


def test_client_refuses_empty_name_and_mismatched_transport_origin() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        RemoteMCPClient("https://tools.example/mcp", transport=Transport(), name="")

    other = Transport()
    other.origin = "https://other.example"
    with pytest.raises(ValueError, match="does not match endpoint origin"):
        RemoteMCPClient("https://tools.example/mcp", transport=other)


@pytest.mark.asyncio
async def test_initialized_headers_require_protocol_and_omit_absent_session() -> None:
    client = RemoteMCPClient("https://tools.example/mcp", transport=Transport())

    with pytest.raises(RuntimeError, match="protocol version is not initialized"):
        await client._headers(initialized=True)

    client._protocol_version = "2025-11-25"
    assert await client._headers(initialized=True) == {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-11-25",
    }


@pytest.mark.parametrize(
    "token", ["line\r\ninjected", "tab\tvalue", "delete\x7f", cast(Any, 7)]
)
async def test_token_provider_cannot_inject_outbound_header_framing(token: Any) -> None:
    client = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=Transport(),
        token_provider=TokenProvider(token),
    )

    with pytest.raises(MCPRemoteError, match="token provider"):
        await client._headers(initialized=False)


@pytest.mark.asyncio
async def test_post_refuses_http_error_status() -> None:
    transport = Transport(
        MCPHTTPResponse(
            "https://tools.example/mcp",
            429,
            {"content-type": "application/json"},
            b"{}",
        )
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)

    with pytest.raises(MCPRemoteError, match="status 429"):
        await client._post({"jsonrpc": "2.0", "method": "probe"}, initialized=False)


async def test_post_refuses_redirect_status_without_interpreting_its_body() -> None:
    transport = Transport(replace(initialized(), status=302))
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)

    with pytest.raises(MCPRemoteError, match="status 302"):
        await client._post({"jsonrpc": "2.0", "method": "probe"}, initialized=False)


def test_session_header_accepts_absence_and_refuses_each_invalid_byte_range() -> None:
    client = RemoteMCPClient("https://tools.example/mcp", transport=Transport())
    assert client._session_from_initialize() is None

    client._last_response = MCPHTTPResponse(
        "https://tools.example/mcp", 200, {}, b"{}"
    )
    assert client._session_from_initialize() is None

    for session in ("", "contains space", "contains\x7f"):
        client._last_response = MCPHTTPResponse(
            "https://tools.example/mcp",
            200,
            {"mcp-session-id": session},
            b"{}",
        )
        with pytest.raises(MCPRemoteError, match="visible ASCII"):
            client._session_from_initialize()


def test_tool_normalization_refuses_each_malformed_declaration() -> None:
    client = RemoteMCPClient(
        "https://tools.example/mcp",
        transport=Transport(),
        max_schema_bytes=16,
    )
    invalid = (
        ([], "declaration must be an object"),
        ({"name": "", "inputSchema": {}}, "name must be a non-empty string"),
        ({"name": 7, "inputSchema": {}}, "name must be a non-empty string"),
        ({"name": "lookup", "inputSchema": []}, "inputSchema must be an object"),
        (
            {"name": "lookup", "inputSchema": {"description": "far too large"}},
            "schema byte ceiling",
        ),
        (
            {"name": "lookup", "description": 7, "inputSchema": {}},
            "description must be text",
        ),
    )

    for declaration, message in invalid:
        with pytest.raises(MCPRemoteError, match=message):
            client._normalize_tool(declaration)


@pytest.mark.parametrize(
    ("declaration", "description"),
    [
        ({"name": "lookup", "description": "Description", "inputSchema": {}}, "Description"),
        ({"name": "lookup", "title": "Title", "inputSchema": {}}, "Title"),
        ({"name": "lookup", "inputSchema": {}}, "lookup"),
        ({"name": "lookup", "description": "", "title": "Title", "inputSchema": {}}, "Title"),
        ({"name": "lookup", "description": "", "title": "", "inputSchema": {}}, "lookup"),
    ],
)
def test_tool_normalization_uses_the_declared_description_fallback_order(
    declaration: dict[str, Any], description: str
) -> None:
    client = RemoteMCPClient("https://tools.example/mcp", transport=Transport())
    assert client._normalize_tool(declaration)["description"] == description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initialize_result", "message"),
    [
        (
            {"protocolVersion": "old", "capabilities": {"tools": {}}},
            "unsupported protocol version",
        ),
        ({"protocolVersion": "2025-11-25", "capabilities": None}, "tools capability"),
        (
            {"protocolVersion": "2025-11-25", "capabilities": {"tools": []}},
            "tools capability",
        ),
    ],
)
async def test_connect_refuses_invalid_protocol_and_capability_negotiation(
    initialize_result: Mapping[str, Any], message: str
) -> None:
    transport = Transport(response(initialize_result, request_id=1))

    with pytest.raises(MCPRemoteError, match=message):
        await RemoteMCPClient("https://tools.example/mcp", transport=transport).connect()

    assert transport.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [None, "not-an-array"])
async def test_tool_listing_refuses_non_array_pages(page: object) -> None:
    transport = Transport(response({"tools": page}, request_id=1))
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    client._protocol_version = "2025-11-25"

    with pytest.raises(MCPRemoteError, match="requires a tools array"):
        await client._list_tools()


@pytest.mark.asyncio
async def test_tool_listing_starts_without_cursor_and_refuses_duplicate_names() -> None:
    transport = Transport(
        tools_page(tool("same"), tool("same"), request_id=1)
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    client._protocol_version = "2025-11-25"

    with pytest.raises(MCPRemoteError, match="duplicate remote MCP tool name 'same'"):
        await client._list_tools()

    assert payload(transport.calls[0])["params"] == {}


@pytest.mark.asyncio
async def test_reconnect_with_stable_catalogue_reuses_compiled_specifications() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
        initialized(session="session-2", request_id=3),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=4),
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)

    await client.connect()
    first = client.specifications
    await client.close()
    await client.connect()

    assert client.specifications is first


@pytest.mark.asyncio
async def test_invocation_refuses_invalid_scope_and_unknown_tool_before_io() -> None:
    client = RemoteMCPClient("https://tools.example/mcp", transport=Transport())
    client._connected = True
    client._specifications = (SimpleNamespace(name="lookup"),)

    for tenant in ("", 7):
        invalid = context()
        invalid.tenant = tenant
        with pytest.raises(MCPRemoteScopeError, match="non-empty tenant"):
            await client.invoke(
                "lookup", {}, call_id="call", context=invalid
            )

    with pytest.raises(LookupError, match="has no tool 'missing'"):
        await client.invoke("missing", {}, call_id="call", context=context())

    assert client._scope is None


@pytest.mark.asyncio
async def test_close_without_session_skips_delete_and_propagates_transport_failure() -> None:
    class FailingCloseTransport(Transport):
        async def close(self) -> None:
            raise LookupError("transport close failed")

    transport = FailingCloseTransport()
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    await client._close_session()
    assert transport.calls == []

    with pytest.raises(LookupError, match="transport close failed"):
        await client.close()


@pytest.mark.asyncio
async def test_close_preserves_session_failure_and_notes_transport_failure() -> None:
    class FailingCloseTransport(Transport):
        async def close(self) -> None:
            raise LookupError("transport close failed")

    transport = FailingCloseTransport(
        MCPHTTPResponse("https://tools.example/mcp", 500, {}, b"")
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    client._protocol_version = "2025-11-25"
    client._session_id = "session-1"

    with pytest.raises(MCPRemoteError, match="session close failed") as caught:
        await client.close()

    assert caught.value.__notes__ == [
        "remote MCP transport close also failed: transport close failed"
    ]


@pytest.mark.asyncio
async def test_close_does_not_invent_a_note_when_only_session_close_fails() -> None:
    transport = Transport(MCPHTTPResponse("https://tools.example/mcp", 500, {}, b""))
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    client._protocol_version = "2025-11-25"
    client._session_id = "session-1"

    with pytest.raises(MCPRemoteError, match="session close failed") as caught:
        await client.close()

    assert getattr(caught.value, "__notes__", []) == []


async def test_session_close_refuses_an_off_endpoint_response_url() -> None:
    transport = Transport(
        MCPHTTPResponse("https://evil.example/mcp", 204, {}, b"")
    )
    client = RemoteMCPClient("https://tools.example/mcp", transport=transport)
    client._protocol_version = "2025-11-25"
    client._session_id = "session-1"

    with pytest.raises(MCPRemoteError, match="origin-pinned"):
        await client.close()

    assert transport.closed is True


def test_catalog_refuses_non_positive_and_exceeded_client_ceilings() -> None:
    client = RemoteMCPClient("https://tools.example/mcp", transport=Transport())

    with pytest.raises(ValueError, match="max_clients must be positive"):
        RemoteMCPToolCatalog((), max_clients=0)
    with pytest.raises(MCPToolLimitExceeded, match="client ceiling 1"):
        RemoteMCPToolCatalog((client, client), max_clients=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_catalog_rollback_reports_only_real_cleanup_failures(
    cleanup_fails: bool,
) -> None:
    class Client:
        def __init__(self, name: str, *, connect_fails: bool = False) -> None:
            self.name = name
            self.connect_fails = connect_fails
            self.specifications: tuple[object, ...] = ()

        async def connect(self) -> None:
            if self.connect_fails:
                raise RuntimeError("connect failed")

        async def close(self) -> None:
            if cleanup_fails:
                raise LookupError("cleanup failed")

    first = Client("first")
    second = Client("second", connect_fails=True)
    catalog = RemoteMCPToolCatalog(cast(Any, (first, second)), max_clients=2)

    with pytest.raises(RuntimeError, match="connect failed") as caught:
        await catalog.connect()

    expected = ["remote MCP client 'first' rollback failed: cleanup failed"]
    assert getattr(caught.value, "__notes__", []) == (expected if cleanup_fails else [])


def test_sse_parser_refuses_the_first_event_beyond_its_ceiling() -> None:
    body = b'data: {"event":1}\n\ndata: {"event":2}\n\n'

    with pytest.raises(MCPRemoteError, match="exceeds event ceiling 1"):
        _sse_objects(body, max_events=1)


def test_sse_parser_does_not_treat_unicode_line_boundaries_as_framing() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "one\x85two"}}
    body = b"data: " + json.dumps(message, ensure_ascii=False).encode() + b"\n\n"

    assert _sse_objects(body, max_events=1) == (message,)


def test_sse_parser_accepts_each_standard_event_line_ending() -> None:
    expected = ({"event": 1}, {"event": 2})
    assert _sse_objects(
        b'data: {"event":1}\r\rdata: {"event":2}\r\r', max_events=2
    ) == expected
    assert _sse_objects(
        b'data: {"event":1}\r\n\r\ndata: {"event":2}\r\n\r\n', max_events=2
    ) == expected


def test_response_matching_requires_an_exact_integer_id_and_jsonrpc_version() -> None:
    boolean_id = MCPHTTPResponse(
        "https://tools.example/mcp",
        200,
        {"content-type": "application/json"},
        b'{"jsonrpc":"2.0","id":true,"result":{}}',
    )
    with pytest.raises(MCPRemoteError, match="request id 1"):
        _response_message(boolean_id, request_id=1, max_events=1)

    wrong_version = MCPHTTPResponse(
        "https://tools.example/mcp",
        200,
        {"content-type": "application/json"},
        b'{"jsonrpc":"1.0","id":1,"result":{}}',
    )
    with pytest.raises(MCPRemoteError, match="JSON-RPC 2.0"):
        _response_message(wrong_version, request_id=1, max_events=1)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", 1])
async def test_connect_refuses_each_invalid_pagination_cursor(cursor: object) -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        response({"tools": [], "nextCursor": cursor}, request_id=2),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )

    with pytest.raises(MCPRemoteError, match="nextCursor must be a non-empty string"):
        await RemoteMCPClient("https://tools.example/mcp", transport=transport).connect()

    assert transport.closed is True


@pytest.mark.asyncio
async def test_connect_refuses_a_repeated_pagination_cursor() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(request_id=2, cursor="page-2"),
        tools_page(request_id=3, cursor="page-2"),
        MCPHTTPResponse("https://tools.example/mcp", 204, {}, b""),
    )

    with pytest.raises(MCPRemoteError, match="repeated cursor 'page-2'"):
        await RemoteMCPClient("https://tools.example/mcp", transport=transport).connect()

    assert transport.closed is True


@pytest.mark.asyncio
async def test_connect_refuses_an_empty_access_token_before_io() -> None:
    transport = Transport(initialized())

    with pytest.raises(MCPRemoteError, match="empty token"):
        await RemoteMCPClient(
            "https://tools.example/mcp",
            transport=transport,
            token_provider=TokenProvider(""),
        ).connect()

    assert transport.calls == []
    assert transport.closed is True


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
async def test_catalog_connect_is_idempotent_and_selection_is_exact() -> None:
    transport = Transport(
        initialized(),
        MCPHTTPResponse("https://tools.example/mcp", 202, {}, b""),
        tools_page(tool("lookup"), request_id=2),
    )
    catalog = RemoteMCPToolCatalog(
        (RemoteMCPClient("https://tools.example/mcp", transport=transport),),
        max_clients=1,
    )

    await catalog.connect()
    calls = len(transport.calls)
    await catalog.connect()

    assert len(transport.calls) == calls
    with pytest.raises(ValueError, match="selection contains duplicates"):
        catalog.select(("lookup", "lookup"))
    with pytest.raises(LookupError, match="unknown remote MCP tools: missing"):
        catalog.select(("missing",))


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
