"""The JSON-RPC surface of `wreath.mcp`: one method at a time, plus the refusals.

Every assertion here is about what a client sees on the wire. An MCP client that
meets a bare 400 has nothing to act on, so the error *shapes* are as much the
contract as the happy paths are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP, PROTOCOL_VERSION, ToolError
from wreath.request import RequestLimits
from wreath.testing import TestClient


@dataclass
class SightingQuery:
    species: str
    since: str | None = None


def build() -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Find recent sightings of a species.")
    async def find_sightings(
        request, query: Annotated[SightingQuery, Body()], limit: int = 20
    ) -> dict:
        return {"species": query.species, "since": query.since, "limit": limit}

    @mcp.tool(description="Always fails, for the caller's benefit.")
    async def refuse(request) -> dict:
        raise ToolError("no camera covers that trail")

    @mcp.tool(description="Fails in a way nobody planned.")
    async def explode(request) -> dict:
        raise ZeroDivisionError("secret operational detail")

    return app, mcp


def header(response, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
    )
    assert response.status == 200
    session_id = header(response, "mcp-session-id")
    assert session_id
    return session_id


async def call(client: TestClient, session_id: str, payload: dict) -> dict:
    response = await client.post("/mcp", json=payload, headers={"mcp-session-id": session_id})
    assert response.status == 200
    return response.json()


async def test_initialize_negotiates_and_mints_a_session() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
            },
        )
        body = response.json()
        assert body["id"] == 1
        assert body["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert body["result"]["serverInfo"] == {"name": "camera-trap", "version": "1.0.0"}
        assert body["result"]["capabilities"]["tools"] == {"listChanged": False}
        assert header(response, "mcp-session-id")


async def test_initialize_answers_with_a_revision_it_implements() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
        )
        # The client, not the server, decides whether it can live with this.
        assert response.json()["result"]["protocolVersion"] == PROTOCOL_VERSION


async def test_ping_is_an_empty_result() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(client, session_id, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert body == {"jsonrpc": "2.0", "id": 2, "result": {}}


async def test_tools_list_renders_every_declared_tool() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(client, session_id, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = [tool["name"] for tool in body["result"]["tools"]]
        assert names == ["explode", "find_sightings", "refuse"]
        entry = next(t for t in body["result"]["tools"] if t["name"] == "find_sightings")
        assert entry["description"] == "Find recent sightings of a species."
        assert entry["inputSchema"]["required"] == ["query"]


async def test_tools_list_serializes_once_and_reuses_the_bytes() -> None:
    _, mcp = build()
    first = mcp._registry.listing()
    assert mcp._registry.listing() is first


async def test_tools_list_cache_is_invalidated_by_a_late_registration() -> None:
    _, mcp = build()
    before = mcp._registry.listing()

    @mcp.tool(description="Declared after the first listing was served.")
    async def latecomer(request) -> dict:
        return {}

    assert mcp._registry.listing() != before
    assert b"latecomer" in mcp._registry.listing()


async def test_tools_call_binds_arguments_and_returns_structured_content() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "find_sightings",
                    "arguments": {"query": {"species": "fox"}, "limit": 5},
                },
            },
        )
        result = body["result"]
        assert result["isError"] is False
        assert result["structuredContent"] == {"species": "fox", "since": None, "limit": 5}
        assert result["content"][0]["type"] == "text"
        assert mcp.tool_calls == 1
        assert mcp.tool_errors == 0


async def test_tools_call_rejects_arguments_that_miss_the_schema() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "find_sightings", "arguments": {"limit": "many"}},
            },
        )
        assert body["error"]["code"] == -32602
        locations = {tuple(entry["loc"]) for entry in body["error"]["data"]["errors"]}
        assert ("arguments", "query") in locations
        assert ("arguments", "limit") in locations
        # A rejected call never reached the tool, so it is not a tool error.
        assert mcp.schema_rejections == 1
        assert mcp.tool_calls == 0
        assert mcp.tool_errors == 0


async def test_tools_call_rejects_an_argument_the_tool_never_declared() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "find_sightings",
                    "arguments": {"query": {"species": "fox"}, "colour": "red"},
                },
            },
        )
        errors = body["error"]["data"]["errors"]
        assert [entry["loc"] for entry in errors] == [["arguments", "colour"]]


async def test_tool_error_is_a_result_not_a_transport_error() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "refuse"}},
        )
        assert "error" not in body
        assert body["result"]["isError"] is True
        assert body["result"]["content"][0]["text"] == "no camera covers that trail"
        assert mcp.tool_errors == 1


async def test_an_unplanned_exception_reports_its_type_and_not_its_message() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "explode"}},
        )
        assert body["result"]["isError"] is True
        text = body["result"]["content"][0]["text"]
        assert "ZeroDivisionError" in text
        assert "secret operational detail" not in text
        assert mcp.tool_errors == 1


async def test_unknown_tool_is_invalid_params() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}},
        )
        assert body["error"]["code"] == -32602
        assert "nope" in body["error"]["message"]


async def test_unknown_method_is_method_not_found() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(client, session_id, {"jsonrpc": "2.0", "id": 10, "method": "tools/fly"})
        assert body["error"]["code"] == -32601


async def test_a_reserved_method_says_which_stage_it_waits_for() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        body = await call(
            client,
            session_id,
            {"jsonrpc": "2.0", "id": 11, "method": "sampling/createMessage"},
        )
        assert body["error"]["code"] == -32601
        assert "sampling" in body["error"]["message"]


async def test_malformed_json_is_a_parse_error() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert response.status == 400
        assert response.json()["error"]["code"] == -32700


async def test_a_batch_is_refused_by_name() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        assert response.status == 400
        body = response.json()
        assert body["error"]["code"] == -32600
        assert "batching" in body["error"]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": [1], "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 7},
    ],
)
async def test_a_malformed_envelope_is_an_invalid_request(payload: dict) -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post("/mcp", json=payload)
        assert response.status == 400
        assert response.json()["error"]["code"] == -32600


async def test_positional_params_are_refused() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]}
        )
        assert response.status == 400
        assert response.json()["error"]["code"] == -32602


async def test_a_non_json_content_type_is_refused_with_a_reason() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp", content=b"ping", headers={"content-type": "text/plain"}
        )
        assert response.status == 415
        assert "application/json" in response.json()["error"]["message"]


async def test_an_unacceptable_accept_header_is_refused() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"accept": "application/xml"},
        )
        assert response.status == 406


async def test_an_oversized_message_is_refused_by_the_app_s_own_body_limit() -> None:
    """`MCPLimits` has no payload ceiling, and this is why it needs none.

    A `tools/call` body is a POST body, so `RequestLimits.max_body_bytes` refuses
    an oversized one while it is still arriving -- before the endpoint is
    entered, and therefore before a tool name has even been read. The claim that
    a second ceiling in `MCPLimits` would be redundant is only true if this
    holds, so it is asserted rather than asserted-about.
    """
    app = Wreath(limits=RequestLimits(max_body_bytes=256))
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Find recent sightings of a species.")
    async def find_sightings(request, query: Annotated[SightingQuery, Body()]) -> dict:
        return {"species": query.species}

    async with TestClient(app) as client:
        session_id = await initialize(client)
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "find_sightings",
                    "arguments": {"query": {"species": "a" * 1024}},
                },
            },
            headers={"mcp-session-id": session_id},
        )
        assert response.status == 413
        assert mcp.tool_calls == 0
        assert mcp.schema_rejections == 0


async def test_a_notification_is_accepted_without_a_reply() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session_id = await initialize(client)
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"mcp-session-id": session_id},
        )
        assert response.status == 202
        assert response.body == b""
