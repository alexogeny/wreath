# Prescriptive plan: a first-party MCP server surface

Status: **stages 1-4 implemented** (July 2026). `wreath.mcp` serves the
protocol, its authentication and authorization, resources, prompts,
`expose_routes`, and the long tail stage 4 held back for "only if asked for" —
sampling, elicitation, enforced roots, completions, and the stdio wrapper.

What is deliberately **not** served, and answers JSON-RPC `method not found`
naming the stage it waits for rather than failing obscurely: `logging/setLevel`,
stream resumption by `Last-Event-ID`, templated resources, and `listChanged`
notifications for the three listings. Dynamic client registration is not
planned — it is an authorization-server endpoint, and the deployment's identity
provider owns it. `docs/reference/roadmap.md` carries that list for readers who
never open this plan.

Related material:

- `AGENTS.md`
- `docs/agents/manifest.json`
- `docs/plans/omni-directional-webhooks.md` (the shape to copy for a subsystem
  that is routes + policy + a supervised background half)
- `docs/plans/middleware-auth-rbac-cedar-comforts.md`
- `docs/guides/sse.md`, `docs/guides/auth.md`, `docs/guides/openapi-typegen.md`
- `~/research/pypi-downloads/wreath-gap-analysis.md` (the evidence below)

## Goal

Ship `wreath.mcp`: a Model Context Protocol server surface that turns declared
Python callables — and, optionally, existing routes — into MCP tools, resources,
and prompts, served over streamable HTTP with SSE, authenticated as an OAuth 2.1
resource server, authorized per tool by Cedar, and recorded on the Flight
Recorder ring.

## Why this, and why now

Twelve-month PyPI installs to 2026-07-01: `mcp` 281.3M from a package first
published 2024-11-20; `fastmcp` 96.3M; `fastmcp-slim` 64.0M; `fastapi-mcp`
20.4M; `httpx-sse` 198.6M; `sse-starlette` 283.6M — the last a 2020 package
sitting at PyPI rank #142 because `mcp` requires `sse-starlette>=3.0.0` and the
two counts track within 1%. That is ~950M installs a year in a category that did
not exist twenty months ago. For scale, the ten most-installed Django extensions
sum to ~155M.

The argument is not "MCP is popular". It is that **every hard part of a
production MCP server is a thing Wreath already owns and the FastAPI stack has
to assemble from four packages**:

| MCP needs | Wreath already has |
|---|---|
| SSE framing | `ServerSentEvent`/`SSEResponse` (`response.py:646,779`), native `_native/sse.c` |
| A tool's JSON Schema | `openapi.generate_openapi` computes exactly this per operation, from `binding.py` |
| OAuth 2.1 resource server + PKCE | `_auth/oauth2.py`, `_auth/oidc.py`, `_auth/jwks.py`, `_native/jose.c` |
| Per-tool authorization | `CedarAuthorizer` (`authorization.py`) |
| An audit trail of what was called with what | the Flight Recorder, plus `crud.py`'s sensitive-name redaction |
| Abuse control per tool | `middleware/ratelimit.py`, incl. `TieredRateLimitPolicy` |

`fastapi-mcp` (20.4M installs/yr) exists for no purpose other than converting
FastAPI route metadata into tool schemas from outside the framework. Wreath
computes that metadata already.

## Non-goals

- Not an MCP **client**. `http_client.py` plus `service_client.py` could grow one
  later; it is a separate plan and shares no code with this one.
- Not an LLM abstraction. Wreath does not wrap `openai`/`anthropic`/`litellm` and
  must not grow a runtime dependency on any of them.
- Not the stdio transport as a first-class server mode. `wreath mcp stdio` may
  wrap the HTTP app for local development, but the supported deployment is a
  remote HTTP server, which is where auth, authz, and audit matter.
- Not sampling, elicitation, or roots in stage 1. Reserve the names; return
  method-not-found until they land.

## Repository constraints

- `src/wreath/mcp.py` (facade) plus `src/wreath/_mcp/` (implementation), matching
  the literal-API rule: `wreath.mcp` is `src/wreath/mcp.py`.
- No runtime dependency. JSON-RPC 2.0 over HTTP+SSE is stdlib-shaped, and every
  byte-level piece already has a native implementation here.
- Compile tool declarations into ordinary Wreath routes so middleware, request
  limits, exception ownership, and observability apply unchanged. An MCP call is
  a route activation like any other.
- Preserve ASGI semantics: the server must work under uvicorn, not only under
  `wreath run`.
- Pin the protocol revision Wreath implements the way `_h2_codec` pins its own,
  and negotiate it in `initialize`.
- No invariant may depend on `assert`.

## The C/Python split, decided

**No new C in stage 1, 2, or 3.** This is a deliberate decision, not an omission,
and it is the answer AGENTS.md's measurement rules produce:

- **JSON-RPC envelope encode/decode** — `_native/json.c` already does this. The
  envelope is a four-key dict. Writing an MCP-specific parser would duplicate a
  tuned codec to save nothing measurable.
- **SSE framing** — `_native/sse.c` already exists, and its header comment
  already explains why it is in C (five passes over the payload per event per
  subscriber). Reuse it. MCP streams through the same `SSEResponse`.
- **HTTP ingress, routing, header handling** — `http.c`, `dtrouter.c`,
  `headers.c`. An MCP request enters through the same native ingress as any
  other POST.
- **Token verification** — `jose.c` accelerates base64url, JWS parsing, HS
  verification, and claim checks today. MCP bearer tokens ride that path.
- **Authorization** — `cedar.c` and `authz.c` already decide in C.

What is left for `wreath.mcp` itself is a tool registry, schema derivation,
session lifecycle, and dispatch — all of which are **startup-time work or
once-per-call orchestration**, which is exactly the shape AGENTS.md tells us to
solve by "explicit startup compilation and caching over repeated request-time
introspection". The stage-1 optimization is therefore Python: **serialize the
`tools/list` payload to bytes once at startup and serve the cached bytes**, the
way the router compiles at startup.

Wire-format code is authoritative C and is held to independent RFC vectors;
`tests/test_sse_frame.py` is the standing example. A second implementation of
our own would only prove that both copies agree.

**What would change this answer, and how to find out.** Run
`uv run wreath-request-trace` against an app with a registered tool and read
`pre_activation`. The intended shape is that ingress, routing, auth, and authz
stay native and Python is entered when a route is *activated*; if MCP dispatch
adds crossings before activation, that is the defect to fix, and it is a routing
fix rather than a new C module. If a `tools/call` on a realistic tool shows
measurable time in envelope handling under `uv run wreath-decomp` — above its
reported A/A noise floor, not below it — that is the signal to move a specific
function, with the measurement recorded in the change. Do not pre-empt it.

## Public model

```python
from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP, Tool

app = Wreath()
mcp = MCP(app, name="camera-trap", version="1.0.0", path="/mcp")


@dataclass
class SightingQuery:
    species: str
    since: str | None = None


@mcp.tool(description="Find recent sightings of a species.")
async def find_sightings(request, query: Annotated[SightingQuery, Body()]) -> dict:
    ...
```

`MCP.tool` reuses the binding layer verbatim: the parameter annotations that
produce an OpenAPI operation schema produce the tool's `inputSchema`. There is
no second declaration syntax and no second validator.

Surface to export from `wreath.mcp`:

| Name | Purpose |
|---|---|
| `MCP` | the server; mounts routes on an app, owns the registry |
| `Tool`, `Resource`, `Prompt` | declared entries, and what `tools/list` renders |
| `ToolContext` | per-call context: identity, session, progress token, cancellation |
| `ToolError` | a tool-level failure, rendered as an MCP error result rather than a transport error |
| `expose_routes` | opt-in adapter turning selected existing routes into tools |
| `MCPLimits` | bounds: payload size, concurrent calls per session, tools per server |

### `expose_routes` is opt-in, and says why

`fastapi-mcp` exposes every route by default. That is the wrong default for a
framework that ships an authorizer: it converts an app's entire HTTP surface into
model-callable actions in one line, including the destructive ones. Wreath's
version takes an explicit selector (`tags=`, `include=`, a predicate) and refuses
to expose a route that has no `description`, because a tool without a description
is a tool the model will misuse.

## Transport

Implement **streamable HTTP** (the current transport): a single endpoint
accepting `POST` for client→server messages, returning either a JSON response or
an SSE stream depending on whether the response is a single result or a stream of
notifications, plus `GET` to open a server→client notification stream, and
`DELETE` to end a session.

- Session identity rides `Mcp-Session-Id`. Store sessions in the existing
  `session_store.py` backend so a multi-process deployment works without a new
  store; the in-memory backend stays the default for single-process development.
- Honour `MCP-Protocol-Version` on every request after `initialize`, and reject a
  version Wreath does not implement with a JSON-RPC error rather than a 400 with
  no body.
- Bound everything through `MCPLimits`. An unbounded `tools/call` payload is a
  request-size problem the existing request limits already solve — route MCP
  through them rather than adding a parallel check.
- Legacy HTTP+SSE (the two-endpoint transport) is explicitly out of scope. Say so
  in the guide; it is the single most common source of MCP interop confusion and
  a docs sentence is cheaper than a compatibility shim.

## Authentication and authorization

MCP's authorization spec makes a remote server an OAuth 2.1 **resource server**.
Wreath has the parts; this wires them:

1. Serve `/.well-known/oauth-protected-resource` describing the authorization
   servers this MCP endpoint trusts. This is a small JSON document and a new
   route, not a new subsystem.
2. Verify the bearer token with the existing `JwtVerifier`/`OidcProvider` path,
   including audience binding — a token minted for a *different* resource must be
   rejected, which is the failure the spec exists to prevent.
3. Return `401` with a `WWW-Authenticate` challenge naming the resource metadata
   URL. `_auth/oauth2.py` already has the RFC 6750 §3 helper (`_bearer_401`);
   extend it rather than writing a second one.
4. Gate each tool through `AuthRequirement`/`CedarAuthorizer`. A tool declares
   its Cedar action; `declared_actions` already collects those for the permission
   document, so a deployment can see every model-callable action in one place.

The dynamic-client-registration half of the spec is **not** Wreath's job — that
belongs to whatever authorization server the deployment runs. Document that
boundary explicitly.

## Observability

- Emit a Flight Recorder marker per `tools/call` carrying tool name, caller
  identity, duration, and outcome. Arguments are recorded under the existing
  recording policy, which means `crud.py`'s sensitive-name redaction applies
  without new redaction code.
- Count `tool_calls`, `tool_errors`, `unauthorized_calls`, and
  `schema_rejections` as ordinary telemetry counters so the OTLP, Prometheus, and
  StatsD bridges carry them with no per-bridge work.
- A rejected call must be distinguishable from a failed one, for the same reason
  `messaging.MessageBus` separates `doorbell_reconnects` from `handler_errors`.

## Staging

**Stage 1 — the protocol, tools only.**
`initialize`, `tools/list`, `tools/call`, `ping`, and cancellation. Schema
derivation from binding. Cached `tools/list` bytes. In-memory sessions. No auth
beyond whatever backend the app already installs. Ships with a guide, a reference
page, and one recipe.

**Stage 2 — auth, authz, and the record.**
Protected-resource metadata, audience-bound verification, the Bearer challenge,
Cedar-gated tools, Flight Recorder markers, per-tool rate limits, `MCPLimits`.
This is the stage that makes the differentiating claim true, so nothing about it
is optional.

**Stage 3 — resources, prompts, and `expose_routes`.**
`resources/list`, `resources/read`, `resources/subscribe` over the existing SSE
notification stream; `prompts/list`, `prompts/get`; the opt-in route adapter;
progress notifications routed through `progress.py`, which already models exactly
this.

**Stage 4 — the long tail, only if asked for.**
Sampling, elicitation, roots, stdio wrapper, completions.

## Tests

- `tests/test_mcp_protocol.py` — one file per JSON-RPC method, including the
  error shapes. Wrong protocol version, unknown method, malformed envelope,
  oversized payload.
- `tests/test_mcp_schema.py` — a tool's `inputSchema` matches what
  `generate_openapi` produces for the same annotations. This is the parity
  contract that keeps the two from drifting, and it is the test that justifies
  reusing the binding layer instead of writing a second one.
- `tests/test_mcp_auth.py` — a token for the wrong audience is rejected; a
  missing token gets a challenge naming the metadata URL; a Cedar-denied tool
  returns an authorization error and increments `unauthorized_calls`, not
  `tool_errors`.
- `tests/test_mcp_transport.py` — session lifecycle, resumption, `DELETE`,
  concurrent calls bounded by `MCPLimits`.
- `tests/test_mcp_recording.py` — a `tools/call` with a `password` argument
  records the call and not the password.
- Run the suite under `WREATH_PURE=1` too. It should pass unchanged, because
  nothing new is native — and if it does not, something reached into `_core`
  without a guard.

## Risks

- **The spec moves.** Mitigate by pinning a revision, negotiating it in
  `initialize`, and keeping the wire layer behind `_mcp/` so a revision bump is a
  contained change. Do not chase drafts.
- **`expose_routes` becomes a footgun.** Mitigated by the opt-in selector and the
  description requirement above. Revisit if anyone asks for a `all=True` flag;
  the answer is no.
- **Scope creep into an agent framework.** `wreath.mcp` serves tools. It does not
  orchestrate models. If a design question can only be answered by knowing which
  LLM is calling, it is out of scope.
