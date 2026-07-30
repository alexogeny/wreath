---
keywords: mcp, model context protocol, tools, llm tools, json-rpc, streamable http, agent tools
---
# `wreath.mcp`

A Model Context Protocol server, served from your application rather than beside
it. Declare an `MCP`, decorate the callables a model should be able to invoke,
and the endpoint is an ordinary Wreath route — with the request limits, the
middleware, the exception boundary, and the observability that every other route
already has.

The reason this lives in the framework and not in a bolt-on package is the
schema. A tool's `inputSchema` is the same question the OpenAPI document
answers, from the same annotations, computed by the same code: `binding` reads
the signature, and the OpenAPI renderer turns it into JSON Schema.
`tests/test_mcp_schema.py` asserts that the two renderings agree, so a change to
one cannot quietly leave the other behind.

Reach for it when a model — an assistant, an agent, an internal copilot — needs
to *do* something in your system rather than read about it. See
[Serving MCP tools](../guides/mcp.md) for the tour, and
[Serve your first MCP tool](../cookbook/recipes/serve-mcp-tools.md) for the
smallest working server.

`initialize`, `ping`, `tools/*`, `resources/*`, `prompts/*` and cancellation are
implemented, over streamable HTTP with idle-bounded in-memory sessions — and so
is the boundary in front of them. `MCPAuth` makes the endpoint an OAuth 2.1
resource server: protected-resource metadata, a `401` challenge naming it, and a
token whose audience must be this resource and not another one.
`@mcp.tool(action=...)` puts each tool behind the same `CedarAuthorizer` a route
uses, `ToolRateLimit` bounds how often one caller may reach it, `MCPLimits`
bounds the rest, and every call leaves one Flight Recorder marker with the
arguments beside it under the recording policy that already hides a password.

`GET /mcp` opens the session's server-to-client stream, which carries a
subscribed resource changing and a running tool's progress — the latter relayed
from `wreath.progress`, because that module already models a task reporting a
percentage and there must not be a second one. `expose_routes` turns selected
existing routes into tools, through an explicit selector with no `all=True`,
refusing a route with no docstring and carrying the route's own
`AuthRequirement` through so that exposing one is never a way around what was
put in front of it.

Three methods travel the other way, and a tool reaches all three through
`request.state.mcp`. `sample(...)` asks the client's model to generate, declared
per tool with `@mcp.tool(sampling=...)` because whether a tool may spend the
caller's model is a policy question — gated by the same Cedar authorizer,
throttled by the tool's own rate-limit bucket, and recorded at the same Flight
Recorder site as the call. `elicit(message, Form)` asks the person to fill a
dataclass in: its fields are the requested schema, through the derivation a
tool's `inputSchema` comes from, and the answer is validated by the same binding
layer and recorded under the same redaction. It is declared per tool with
`@mcp.tool(elicitation=...)` and gated, throttled and recorded identically,
because a form renders inside a client UI the person already trusts — a tool
asking for an API key is a phishing surface wearing that chrome, and being able
to decline is the control social engineering is built to walk through, so a tool
that declared nothing may not prompt. `read_file(path)` reads beneath
`MCP(file_root=...)` through the containment walk static files use, *and*
beneath whatever `roots` the client declared — a declared root is enforced here,
not displayed. Each is bounded: a capability the client never advertised is
refused rather than sent, a client that does not answer times out and is told to
stop, cancelling the call cancels the question, and ending the session fails
every outstanding one.

`completion/complete` answers from the values a prompt argument's `Literal` or
`Enum` annotation already declared, so there is no completion registry to keep
in step. `wreath mcp stdio app:app` puts the same endpoint behind a pipe as a
byte relay over the in-process transport, not as a second dispatch path.

`logging/setLevel`, stream resumption by `Last-Event-ID`, templated resources
and `listChanged` are not implemented, and every method this revision defines
but Wreath does not serve answers `method not found` naming the stage it is
waiting for. Dynamic client registration is not planned at all: it is the
authorization server's endpoint, and a deployment's identity provider owns it.

::: wreath.mcp
