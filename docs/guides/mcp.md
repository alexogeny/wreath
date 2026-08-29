---
description: Expose typed tools, resources and prompts over a real MCP transport.
keywords: guide MCP server tools resources prompts JSON-RPC session auth
---

# Build an MCP server

An MCP catalogue should expose existing application operations, not create a parallel
backend. Wreath derives input schemas from ordinary Python annotations and mounts the
streamable HTTP transport on the same ASGI application.

```python title="operations.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP


@dataclass
class Search:
    service: str
    limit: int = 20


app = Wreath()
mcp = MCP(app, name="operations", version="1.0.0")


@mcp.tool(description="Find recent incidents for one service.")
async def find_incidents(request, query: Annotated[Search, Body()]) -> dict:
    return {
        "service": query.service,
        "incidents": [f"inc-{index}" for index in range(min(query.limit, 3))],
    }


@mcp.resource(
    "status://operations/current",
    title="Operations status",
    description="The current control-plane status.",
)
async def operations_status(request) -> dict:
    return {"status": "healthy"}


@mcp.prompt(description="Prepare an incident handoff.")
async def incident_handoff(request, service: str) -> str:
    return f"Summarise evidence, risk and next action for {service}."
```

Test over JSON-RPC so discovery, session state, schema binding and dispatch all run:

```python title="test_operations.py"
from wreath.mcp import PROTOCOL_VERSION
from wreath.testing import TestClient

from operations import app


def header(response, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def test_a_client_discovers_and_calls_the_tool() -> None:
    async with TestClient(app) as client:
        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "docs", "version": "1"},
                },
            },
        )
        session_id = header(initialized, "mcp-session-id")
        assert session_id is not None
        called = await client.post(
            "/mcp",
            headers={"mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "find_incidents",
                    "arguments": {"query": {"service": "checkout", "limit": 2}},
                },
            },
        )

    assert called.status == 200
    result = called.json()["result"]
    assert result["structuredContent"] == {
        "service": "checkout",
        "incidents": ["inc-0", "inc-1"],
    }
```

Add `MCPAuth` or the application's bearer backend before external traffic. Attach
Cedar actions, recent second-factor requirements, sampling and elicitation explicitly
to the catalogue entries that need them. Long tools report progress through the MCP
session rather than holding opaque work.

See the [governed control-room story](../stories/mcp-control-room.md) and
[MCP member reference](../reference/mcp.md).
