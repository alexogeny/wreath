# Serve your first MCP tool

You want an assistant to be able to *do* one thing in your application — look a
customer up, kick off a report — rather than only read about it. That is one
object, one decorator, and no new dependency:

```python
from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP, ToolError

app = Wreath()
mcp = MCP(app, name="support-desk", version="1.0.0", path="/mcp")


@dataclass
class TicketQuery:
    customer_email: str
    status: str | None = None


@mcp.tool(description="Find a customer's support tickets, newest first.")
async def find_tickets(
    request, query: Annotated[TicketQuery, Body()], limit: int = 10
) -> dict:
    rows = await lookup_tickets(query.customer_email, query.status, limit)
    if not rows:
        raise ToolError(f"no tickets for {query.customer_email}")
    return {"tickets": rows}
```

`MCP(app, ...)` registers `POST /mcp`, `GET /mcp` and `DELETE /mcp`. The
decorator reads `find_tickets`' signature once and publishes the schema a model
will see — the same schema the OpenAPI document would show for the same
annotations, from the same code.

## Drive it from a test

An MCP client speaks JSON-RPC over that one endpoint. `initialize` first; the
session identifier comes back in a header and every later message carries it:

```python
from wreath.mcp import PROTOCOL_VERSION
from wreath.testing import TestClient

async def test_the_tool_is_callable():
    async with TestClient(app) as client:
        opened = await client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        })
        session = dict(opened.headers)[b"mcp-session-id"].decode()

        listed = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"mcp-session-id": session},
        )
        assert [t["name"] for t in listed.json()["result"]["tools"]] == ["find_tickets"]

        called = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {
                    "name": "find_tickets",
                    "arguments": {"query": {"customer_email": "ada@example.com"}},
                },
            },
            headers={"mcp-session-id": session},
        )
        assert called.json()["result"]["isError"] is False
```

A successful call comes back with a text `content` block and, when the tool
returned a mapping, the same value again as `structuredContent`, so a client can
use it without re-parsing.

## Before you point anything real at it

A bare `MCP(app, ...)` installs no authentication, so the endpoint is exactly as
protected as the route is. Treat every tool as callable by a persuadable third
party with arguments of its choosing, and give it a boundary:

```python
from wreath.mcp import MCPAuth, ToolRateLimit

mcp = MCP(
    app,
    name="support-desk",
    version="1.0.0",
    path="/mcp",
    auth=MCPAuth(
        resource="https://api.example.com/mcp",
        authorization_servers=("https://idp.example",),
        verifier=provider.bearer_verifier(),
    ),
)


@mcp.tool(
    description="Refund a customer's most recent order.",
    action="Order::refund",
    resource=lambda request: request.state.mcp.arguments.get("order_id"),
    rate_limit=ToolRateLimit(limit=5, window=60.0),
)
async def refund_order(request, order_id: str) -> dict:
    ...
```

That publishes `/.well-known/oauth-protected-resource/mcp`, answers a request
with no token with a `401` naming it, refuses a token minted for a *different*
resource, and puts the refund behind the same Cedar policy a route would use.

Then read the failure model — `ToolError` versus an unplanned exception versus a
schema rejection versus a denial — in [Serving MCP tools](../../guides/mcp.md)
before you write the second tool; it is the part that decides how well an agent
behaves when things go wrong.

## Routes you already have

If the thing you want the model to do is already a route, name it — one tag, or
one path, never all of them:

```python
from wreath.mcp import expose_routes

expose_routes(mcp, app, tags=("tickets",))
```

Each selected route becomes a tool whose description is its handler's docstring
and whose schema comes from its signature, and it keeps whatever `@authorize`,
`@roles` or `@permissions` it was behind. There is deliberately no flag that
exposes everything: that line would turn your whole HTTP surface, destructive
half included, into model-callable actions, and nobody reviews a line that
short.

A route with no docstring is refused, and so is one whose signature an MCP call
cannot fill — a path placeholder, a header, an upload, a `Depends(...)`. The
message names the route and the parameter. When you hit it, the fix is usually a
three-line `@mcp.tool` that takes those values as arguments and calls the same
code, which also gets you a description written for a model rather than for
someone reading an API reference.

## Trying it from an editor

An editor on your laptop usually speaks stdio rather than HTTP:

```bash
wreath mcp stdio app:app
```

That is a relay over the endpoint you just wrote, not a second server, so the
tool behaves identically to the way it will in production — same routing, same
authentication, same limits, same record. Point your editor's MCP configuration
at that command while you are iterating, and at the URL when you deploy.
