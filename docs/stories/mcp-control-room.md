---
description: Build a governed MCP server for real operational work with OAuth, policy, progress and audit.
keywords: MCP server Model Context Protocol OAuth authorization tool resources prompts audit step-up
boost: 1.5
---

```hero
eyebrow: Story 04 · a model at a real control boundary
title: Give an agent tools—safely.
lede: Let a model inspect deployments, subscribe to changing resources and start bounded operations without creating a second, less-governed backend.
signal: MCP tools and resources
signal: OAuth resource boundary
signal: human step-up
signal: recorded outcomes
action: See the decisive moment -> #the-decisive-moment
action: Browse the AI surface -> ../reference/index.md#ai-and-protocols
```

## The scene

An on-call engineer opens their preferred agent client and asks why checkout errors
increased after a deployment. The agent reads a health resource, queries recent
deployment facts and subscribes to the rollout state. Those are ordinary application
operations exposed through a deliberate MCP catalogue.

The agent can diagnose. It cannot silently promote itself into an operator.

## The decisive moment

The agent proposes a rollback. Reading was permitted by the caller's token; changing
production requires a fresh human factor. The client presents the boundary, the person
proves it, and only then does the tool start a durable workflow. Progress returns to
the conversation. The call, arguments, decision and outcome remain inspectable.

> The invariant: exposing an operation through MCP never weakens the authorization,
> limits or human requirements already attached to that operation.

## One operation, not two backends

```python title="operations.py"
from wreath import Wreath
from wreath.mcp import MCP

app = Wreath()
mcp = MCP(app, name="operations", version="1.0.0", path="/mcp")


@mcp.tool(description="Read the current health of a named service.")
async def service_health(request, service: str) -> dict:
    return {"service": service, "status": "healthy"}
```

The declaration is small because the surrounding problems already have owners. The
binding layer derives schemas. Authentication establishes the principal. Policy
decides. Progress and messaging carry long work. Recording applies its existing
redaction budget.

## Implement the control room

The useful catalogue has three different kinds of thing: a tool the model may call,
a resource it may read or subscribe to, and a prompt a person deliberately chooses.
The rollback tool also declares its human boundary in the catalogue itself.

```python title="operations.py"
from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.mcp import MCP
from wreath.temporal import now, to_timestamp


def verify(token: str) -> Identity | None:
    if token == "on-call-fresh":
        return Identity(
            id="ada",
            claims={"second_factor_at": to_timestamp(now()).seconds},
        )
    if token == "on-call-no-factor":
        return Identity(id="ada")
    return None


app = Wreath()
app.configure_auth(BearerTokenBackend(verify))
mcp = MCP(
    app,
    name="operations",
    version="1.0.0",
    path="/mcp",
    instructions="Diagnose first. Roll back only after the operator confirms.",
)

services = {
    "checkout": {"status": "degraded", "release": "2026.08.28.3"},
    "catalogue": {"status": "healthy", "release": "2026.08.27.9"},
}


@mcp.tool(description="Read the current health and release of a service.")
async def service_health(request, service: str) -> dict:
    return {"service": service, **services[service]}


@mcp.resource(
    "rollout://checkout/current",
    title="Checkout rollout",
    description="The release and health currently serving checkout traffic.",
)
async def checkout_rollout(request) -> dict:
    return services["checkout"]


@mcp.prompt(description="Prepare a concise incident handoff for a service.")
async def incident_handoff(request, service: str) -> str:
    return f"Summarise the evidence, current risk and next action for {service}."


@mcp.tool(
    description="Roll a service back to its previous healthy release.",
    second_factor=300,
)
async def rollback_service(request, service: str) -> dict:
    services[service] = {"status": "healthy", "release": "2026.08.27.9"}
    mcp.notify_resource_updated("rollout://checkout/current")
    return {"service": service, "state": "rolled_back"}
```

The verifier is deliberately tiny for the tutorial. In production, pass an OIDC
provider's bearer verifier or configure `MCPAuth` so the endpoint also publishes its
OAuth protected-resource metadata and enforces the resource audience.

### Call the protocol, not the Python function

This test proves schema discovery, session establishment, tool dispatch and the fresh
factor boundary over the actual MCP transport.

```python title="test_operations.py"
from wreath.mcp import PROTOCOL_VERSION
from wreath.testing import TestClient

from operations import app


def response_header(response, name: str) -> str | None:
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
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
    )
    assert response.status == 200
    session_id = response_header(response, "mcp-session-id")
    assert session_id is not None
    return session_id


async def call_tool(
    client: TestClient,
    session_id: str,
    name: str,
    arguments: dict,
    *,
    token: str | None = None,
):
    headers = {"mcp-session-id": session_id}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


async def test_diagnosis_is_open_but_rollback_needs_recent_proof() -> None:
    async with TestClient(app) as client:
        session_id = await initialize(client)
        health = await call_tool(
            client, session_id, "service_health", {"service": "checkout"}
        )
        refused = await call_tool(
            client,
            session_id,
            "rollback_service",
            {"service": "checkout"},
            token="on-call-no-factor",
        )
        approved = await call_tool(
            client,
            session_id,
            "rollback_service",
            {"service": "checkout"},
            token="on-call-fresh",
        )

    assert health.json()["result"]["structuredContent"]["status"] == "degraded"
    assert "second factor" in refused.json()["error"]["message"]
    assert approved.json()["result"]["structuredContent"]["state"] == "rolled_back"
```

```bash
uv run wreath test -k operations
uv run wreath dev operations:app

# The same application, for clients that only speak stdio.
uv run wreath mcp stdio operations:app
```

## Put OAuth around the MCP resource

```python title="oauth_boundary.py"
from wreath.mcp import MCP, MCPAuth, ToolRateLimit

auth = MCPAuth(
    resource="https://api.example.com/mcp",
    authorization_servers=("https://identity.example.com",),
    verifier=oidc.bearer_verifier(),
    scopes_supported=("operations:read", "operations:write"),
)
mcp = MCP(app, name="operations", version="1.0.0", auth=auth)


@mcp.tool(
    description="Read deployment events for one service.",
    rate_limit=ToolRateLimit(limit=30, window=60),
)
async def deployment_events(request, service: str) -> dict:
    return {"events": await deployments.recent(service, limit=50)}
```

Now a token for the ordinary API is not automatically a token for the model-facing
resource. The endpoint advertises where clients obtain the right token, checks its
audience, and meters the expensive tool per caller rather than per process.

| Boundary | Wreath surface | What the model receives |
|---|---|---|
| protocol catalogue | `wreath.mcp` | tools, resources and prompts |
| caller identity | `wreath.mcp.MCPAuth`, `wreath.oauth` | an OAuth-protected resource |
| per-operation policy | `wreath.authorization`, `wreath.auth` | action and resource decisions |
| expensive work | `wreath.jobs`, `wreath.progress` | durable execution and live progress |
| human boundary | `wreath.users`, second-factor requirements | recent proof before sensitive action |
| explanation afterward | `wreath.recording`, `wreath.audit_log` | redacted arguments and outcomes |

## Build it in four acts

### 1. Expose one read operation

Start with a narrow, typed health query. Inspect the generated input schema and call
it through HTTP and stdio transports. A transport changes how bytes arrive, not what
the operation is allowed to do.

### 2. Authenticate the MCP resource

Publish protected-resource metadata and refuse a token minted for a different
audience. Apply a per-tool action and rate limit. Ask the system for its declared
catalogue and verify that an unregistered route is absent.

### 3. Add live and long-running work

Expose a changing deployment as a resource subscription. Start an operation that
reports progress. Cancel it from the client and show the underlying workflow stopping.

### 4. Cross the human boundary

Put rollback behind recent second-factor proof. Record both the refused attempt and
the approved run, with sensitive values redacted by policy rather than by every tool
author remembering to hide them.

## The larger idea

MCP is most compelling when it makes an existing system operable by models without
making that system less legible to humans. Wreath's advantage is not merely speaking
the protocol. It is giving protocol calls the same identity, policy, progress and
evidence as the rest of the application.

Next: [carry those boundaries into enterprise tenancy](enterprise.md), or
[build the first route](../start/index.md).
