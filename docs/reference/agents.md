---
description: Provider-neutral model agents, governed MCP tools, durable effects and ChatOps approvals.
keywords: Wreath agents OpenAI Anthropic Gemini MCP ChatOps approvals model routing
---

# Agents

`wreath.agents` connects model providers, MCP tools, durable jobs, ChatOps identities,
approvals, memory, artifacts, and recording without a vendor SDK or a second runtime.
Nothing runs on the ordinary request path unless an application creates an agent.

## A bounded runtime

Create the outbound client through the application so Wreath owns its pool, DNS and
destination policy for the lifespan. The adapter streams response bytes directly into
the selected backplane.

```python title="agent-runtime.py"
import os

from wreath import Wreath
from wreath.agents import (
    AgentCatalog,
    AgentProfile,
    AgentRuntime,
    HTTPClientTransport,
    OpenAIResponsesBackplane,
)

app = Wreath()
http = app.http_client("models", base_url="https://api.openai.com")
transport = HTTPClientTransport(http, base_url="https://api.openai.com/v1")
models = OpenAIResponsesBackplane(
    api_key=os.environ["OPENAI_API_KEY"],
    transport=transport,
)
profile = AgentProfile(
    name="support",
    backplane=models,
    model="gpt-5",
    max_turns=6,
    max_tool_calls=8,
    max_output_tokens=2_000,
    max_total_tokens=12_000,
    timeout=90,
)
runtime = AgentRuntime(AgentCatalog((profile,), default="support"))
```

The same normalized contract is implemented by `AnthropicMessagesBackplane`,
`GeminiGenerateContentBackplane`, `OpenAICompatibleBackplane`, and
`AzureOpenAIBackplane`. Azure accepts either an API key or an asynchronous Entra
token provider and fetches a fresh token for each attempt. A catalog may map
tenants to different profiles. Retryable provider failure may cross to a declared
fallback only before the provider emits an event; partial output and tool effects are
never replayed through another model.

For deployments that select a provider by capability, residency and live health,
put a `RoutedBackplane` behind the profile. `ModelCandidate` declares immutable
capability and region facts; `ModelRoutePolicy` fixes each tenant's ordered candidate
set. Construction refuses a tenant with no statically eligible candidate. Request-time
routing may narrow, but never widen, those constraints, and never fails over after a
provider event.

`AgentRuntime.execute(prompt, context=...)` yields normalized text, tool-call, usage,
and completion events. `AgentRuntime.run(...)` also implements ChatOps' vendor-neutral
`AgentBackend` protocol.

## Governed tools without protocol loopback

`MCPToolCatalog` selects a fixed subset of an existing `MCP` registry. Selection
compiles schemas once and refuses unknown, duplicate, over-limit, sampling, and
elicitation tools before requests begin.

```python title="agent-tools.py"
from typing import Any

from wreath import Request
from wreath.agents import (
    AgentCatalog,
    AgentProfile,
    AgentRuntime,
    MCPToolCatalog,
)
from wreath.mcp import MCP


def support_agent(models: Any) -> AgentRuntime:
    mcp = MCP(name="support-tools", version="1")

    @mcp.tool(
        description="Read one support case.",
        action="Case::read",
        resource='Case::"requested"',
    )
    async def read_case(request: Request, case_id: str) -> dict[str, Any]:
        return await request.state.cases.read(request.identity, case_id)

    profile = AgentProfile(
        name="support",
        backplane=models,
        model="gpt-5",
        tools=("read_case",),
        delegation_scope=frozenset({"Case::read"}),
    )
    return AgentRuntime(
        AgentCatalog((profile,)),
        tools=MCPToolCatalog(mcp),
    )
```

Invocation calls the tool directly. It still uses the MCP binding schema, Cedar and
role/permission/second-factor checks, tenant-and-principal rate limits, progress, error
normalization, and recording. Every call receives an unambiguous effect ID. Operations
that require an interactive MCP client session refuse at profile construction instead
of failing halfway through a turn.

`RemoteMCPClient` speaks MCP Streamable HTTP directly to an HTTPS endpoint. It owns
initialize/initialized, protocol and session headers, bounded paginated discovery,
schema drift detection, JSON and SSE responses, cancellation, close, token lookup and
effect-aware audit records. `RemoteMCPToolCatalog` combines connected clients and
refuses tool-name collisions. `MCPHTTPClientTransport` binds it to an application-owned
`HTTPClient` without enabling redirects or an off-origin credential path. A remote tool
with an unknown outcome is never retried.

`FederatedToolCatalog` combines local and remote catalogs under explicit namespace
prefixes. It compiles each participating child once, preserves declared tool order and
invokes the owning child directly. Qualified names make equal tool names from different
servers unambiguous; malformed names, catalog drift and collisions refuse before an
invocation begins.

## ChatOps and federated authority

Register a runtime as one durable command:

```python title="chat-agent.py"
from wreath.agents import AgentRuntime
from wreath.chat import ChatOps


def register_support_agent(chat: ChatOps, runtime: AgentRuntime) -> None:
    chat.agent(
        "ask",
        runtime,
        description="Ask the support agent",
        action="Support::ask",
        resource="conversation",
    )
```

`ChatOps.agent` requires the normal durable ChatOps `jobs` and transactional `inbox`
owners. Slack, Teams, and Discord identity resolution happens before command
authorization. The resolved Wreath principal is carried into the agent invocation; a
profile with `delegation_scope` calls the existing `Principal.narrow(...)` owner, so an
agent can only lose authority. The direct MCP executor evaluates that narrowed identity
again for every tool.

## Safety-biased durable effects and approval

`DurableAgent` adapts a model completion and effect executor to `JobRunner`. Turn and
tool-call IDs are stable, jobs have automatic retries disabled, and tenant, principal,
job key, attempt, and fence are checked before model work. A tool effect is checkpointed
only after it succeeds. A checkpoint observed before execution is skipped; recovery
attempts are refused because a local checkpoint cannot prove whether a suspended remote
effect happened. An unknown outcome is surfaced for an operator instead of guessed safe.
Use a stable tool-call ID as the remote system's idempotency key when that system supports
one. End-to-end recovery requires a store and effect sink that atomically claim the effect
or preserve an explicit unknown state.

`ApprovalStore` is the persistence contract for human approval. The included
`InMemoryApprovalStore` is bounded, expiring, single-use, tenant-and-principal bound,
and may require authentication newer than the approval request. Production deployments
can implement the same contract over their durable store.

`ChatApprovalFlow(chat, approvals)` registers bounded dynamic action prefixes and
renders native Slack Block Kit buttons, Teams Adaptive Card actions, or ephemeral
Discord components. Approval and denial use only the verified, server-resolved tenant
and identity. The action payload carries an opaque approval ID, never authority; the
store owns the action, resource, expiry and fresh-auth requirement.

## Memory and artifacts

`ContextAssembler` performs one bounded store read, checks every returned record's
tenant, principal, and conversation, and selects deterministically under item and
character ceilings. A memory store must declare positive retention and implement
erasure. Records retain explicit trust and provenance labels; Wreath does not silently
promote external text to system instructions.

`AgentArtifactManager` writes through an existing `ObjectStore`. Byte and artifact-count
ceilings are checked before an over-limit chunk reaches storage. Streaming writes hash
and forward each chunk in one pass, and returned metadata includes ownership, media
type, trust, digest, object stat, and a `Provenance` envelope.

## Recording

Pass `AgentObservability` to `AgentRuntime` to record model and tool boundaries:

```python title="agent-observability.py"
from typing import Any

from wreath.agents import (
    AgentCatalog,
    AgentObservability,
    AgentProfile,
    AgentRuntime,
    MCPToolCatalog,
)
from wreath.mcp import MCP


def observed_agent(
    profile: AgentProfile,
    mcp: MCP,
    deployment_observer: Any,
) -> AgentRuntime:
    return AgentRuntime(
        AgentCatalog((profile,)),
        tools=MCPToolCatalog(mcp),
        observability=AgentObservability(observer=deployment_observer),
    )
```

The default record contains identity, correlation, provider/model or tool/call ID,
duration, outcome, usage, and fallback status—not prompts, arguments, or results.
Payload capture requires an explicit bounded `RedactionPolicy`; structured capture also
requires an application redactor. Recorder failures are counted and cannot turn a
completed tool effect into a retry.

::: wreath.agents
