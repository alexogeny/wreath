# Serving MCP tools

A model that can only read your API is a model that can only talk about it. The
Model Context Protocol is how an assistant is handed a list of things it may
actually *do* — with names, descriptions, and a schema for each — and then does
them.

Almost none of that is new work for a framework that already knows the shape of
its own handlers. A tool needs a JSON Schema; Wreath computes one for every
typed route so the OpenAPI document can exist. A tool needs bounds, an exception
boundary, and somewhere for the failure to be recorded; those are route
concerns, and an MCP call is a route activation like any other. So `wreath.mcp`
is not a second stack sitting next to your application. It is a single endpoint
and a registry, and everything underneath it is the machinery you were already
running.

## Your first server

```python
from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP, ToolError

app = Wreath()
mcp = MCP(app, name="camera-trap", version="1.0.0", path="/mcp")


@dataclass
class SightingQuery:
    species: str
    since: str | None = None


@mcp.tool(description="Find recent sightings of a species.")
async def find_sightings(
    request, query: Annotated[SightingQuery, Body()], limit: int = 20
) -> dict:
    rows = await lookup(query.species, since=query.since, limit=limit)
    if not rows:
        raise ToolError(f"no camera has seen a {query.species} recently")
    return {"sightings": rows}
```

That is the whole server. `MCP(app, ...)` registers `POST /mcp`, `GET /mcp` and
`DELETE /mcp`; the decorator reads the signature once and derives the schema a
client will see in `tools/list`:

```json
{
  "type": "object",
  "properties": {
    "query": {"$ref": "#/$defs/SightingQuery"},
    "limit": {"type": "integer", "default": 20}
  },
  "required": ["query"],
  "additionalProperties": false,
  "$defs": {"SightingQuery": {"type": "object", "...": "..."}}
}
```

The handler contract is the route contract, deliberately. The first parameter is
the `Request`. Every later parameter is bound by name from the call's
`arguments` object using its annotation, and a structured argument is a
dataclass or ORM model marked `Annotated[T, Body()]` — the same spelling that
would bind an HTTP request body. There is no second declaration syntax to learn
and no second validator to keep in step.

## Descriptions are not decoration

`mcp.tool` refuses a tool that has neither a `description=` nor a docstring.
That is not tidiness. The description is the *entire* basis on which a model
decides whether this is the tool for the job; an undescribed tool is not ignored,
it is guessed at. Write it for a reader who has your tool list and nothing else:
what it does, and when it is the right one.

## Failing usefully

There are two ways for a call to go wrong and they are not the same, so they do
not look the same on the wire.

Raise `ToolError` when the tool worked correctly and the answer is bad news — no
match, a name that does not exist, a state that forbids the action. It comes
back as an ordinary result carrying `isError: true` and your message, which a
model can read and act on. Nothing else in this guide is as load-bearing for the
quality of an agent's behaviour.

Everything else — a driver error, a bug — is caught at the boundary so that one
tool cannot take the session down, counted in `mcp.tool_errors`, and reported to
the caller as the exception's *type* and nothing more. Its message was written
for an operator, and whoever is driving the model is not that.

An argument that does not match the published schema is a third thing again. It
never reaches the tool, so it counts as `mcp.schema_rejections`, not as a tool
error, and it comes back as a JSON-RPC `invalid params` listing every field that
was wrong at once. Keeping the two counters apart is what lets you tell a broken
tool from a confused caller without reading logs.

## Cancellation

A call runs in its own task, so a client that sends
`notifications/cancelled` naming the request id actually stops the work: the
handler sees `asyncio.CancelledError` at its next await point. Release resources
in a `finally:`, exactly as you would anywhere else. No response is sent for a
cancelled call — the specification forbids one — so the POST that carried it
answers `202` with an empty body.

Ending a session with `DELETE` cancels everything that session still has in
flight, for the same reason.

## The transport, and the one thing to know about it

Wreath implements **streamable HTTP**, the current transport: one endpoint,
`POST` for every client message, `GET` to open the server-to-client notification
stream, `DELETE` to end a session. The legacy two-endpoint HTTP+SSE transport is
not implemented and will not be. It is the single most common source of MCP
interop confusion, so it is worth saying plainly rather than leaving to be
discovered: if a client can only speak the 2024 two-endpoint transport, it
cannot talk to this server.

`initialize` mints a session and returns its identifier in `Mcp-Session-Id`.
Every later message must carry that header; one that does not is a `400` naming
the missing header, and one naming a session this process does not know is a
`404`, which is a client's signal to initialize again. Sessions are in memory,
which is right for a single process and wrong for a fleet — a load balancer must
pin a session to a worker until the store moves behind `wreath.session_store`.

**A session is bound to the subject that opened it**, so a leaked
`Mcp-Session-Id` is not, on its own, worth anything to a different caller: a
message naming somebody else's session is a `401` with a Bearer challenge,
whatever token it carries. The binding holds whichever way the endpoint
authenticates — through `MCPAuth` below, or through the application's own
`app.configure_auth(...)` backend. An endpoint with *neither* has no subject to
bind to, and its sessions are therefore only as private as their identifiers.

A session ends when it is abandoned, not only when it is closed. `DELETE` is the
polite path and it is not the common one — a process is killed, a laptop sleeps,
a model stops mid-conversation — so a session with no traffic for
`session_idle_seconds` (fifteen minutes by default) is collected, and anything it
still had in flight is cancelled with it. *Idle* means no traffic, not old: a
conversation that runs all day and keeps talking is never collected out from
under itself.

A reply travels as JSON when the client accepts `application/json`, and as a
single Server-Sent Event when it accepts only `text/event-stream` — through the
same `SSEResponse` that serves every other stream here.

`GET /mcp` with `Accept: text/event-stream` and the session header opens that
session's **notification stream**: one per session, because a second would split
one conversation's notifications between the two at random, and a second request
for one gets a `409` saying so. Everything the server sends unasked travels
there — a subscribed resource changing, a running tool's progress — while every
*reply* still travels on the POST that asked for it. Notifications are queued
whether or not the stream is open, up to `max_pending_notifications`; past that
the newest is dropped and counted in `mcp.notifications_dropped`, because a
progress bar that never moves and a server that is silently discarding are
indistinguishable until someone reads that number. The stream ends when the
session does, and emits an SSE comment every `stream_keepalive_seconds` so an
intermediary does not reap a connection that is working exactly as intended.
Resumption with `Last-Event-ID` is not implemented: a client that reconnects
opens a new stream and re-reads what it needs.

The protocol revision is pinned. `initialize` answers with a revision this build
implements, and a later request whose `MCP-Protocol-Version` header names one it
does not gets a JSON-RPC error saying so, rather than an empty `400` that a
client author has to reverse-engineer.

**One error code is worth knowing about.** The specification names `-32002` for
a `resources/read` of a URI a server does not serve, and Wreath uses it for
exactly that. Its own codes sit around it: `-32001` a policy refusal, `-32003` a
rate limit (with `data.retryAfter` in seconds), `-32004` a session at its
concurrency or subscription ceiling.

## Things to read, not do

A tool is something a model *does*. A resource is something it *reads*,
addressed by a URI rather than by a name and an argument object:

```python
@mcp.resource(
    "camera://ridge/latest",
    description="The most recent frame from the ridge camera.",
    mime_type="image/jpeg",
)
async def ridge_latest(request) -> bytes:
    return await read_frame("ridge")
```

The reader takes the request and nothing else — a `resources/read` carries a URI
and no arguments, so there is nothing to bind. Return `bytes` and they travel as
a base64 `blob`; return a string and it travels as `text`; return anything else
and it is rendered as JSON. A client chooses between the two by which key is
present, not by the media type, which is why bytes never arrive as text.

**The URI is the Cedar resource.** `@mcp.resource(uri, action="Camera::read")`
decides against the URI itself, so unlike a tool there is no second identifier
to pass and nothing for the two to disagree about.

A reader that raises `ToolError` is saying "this is gone", and comes back as the
specification's own `-32002`; a reader that raises anything else is a fault, and
comes back as an internal error naming the exception's *type* and nothing more,
counted in `mcp.resource_errors`. A read the policy refused is neither: it
counts in `mcp.unauthorized_calls`, like every other refusal.

### Telling a client that something changed

A client may `resources/subscribe` to a URI. When the thing behind it changes,
say so:

```python
@app.post("/cameras/{camera_id}/frames")
async def store_frame(request, camera_id: str) -> dict:
    await save(camera_id, await request.body())
    mcp.notify_resource_updated(f"camera://{camera_id}/latest")
    return {"stored": True}
```

`notify_resource_updated` is synchronous and never blocks — the caller is the
code that just wrote the row, and a fan-out that could block would put a
client's reading speed on your write path. It returns how many sessions were
told. Subscriptions live on the session and go with it; a session may hold up to
`max_subscriptions` of them.

Templated resources — a URI with a placeholder resolved per read — are not
declarable. `resources/templates/list` answers with an empty list rather than
`method not found`, because a server that advertises the resources capability
and then rejects a method that capability implies reads as broken.

## Prompts are chosen by a person

This is the part that is easy to miss when reading the specification next to
`tools/list`. A tool is picked by the model; a prompt is picked by a *person* —
a slash command, a menu entry — which is why it has arguments instead of a
schema and why every one of them is a string:

```python
@mcp.prompt(description="Draft a report on a species' recent sightings.")
async def sighting_report(request, species: str, tone: str = "neutral") -> str:
    return f"Summarise this month's {species} sightings in a {tone} tone."
```

Return the text of one message, or a sequence of `{"role": ..., "content": ...}`
mappings when the prompt seeds a conversation rather than a single turn.

A parameter annotated as anything but a string is **refused at registration**.
MCP carries prompt arguments as a flat map of strings, so an `int` parameter is
a declaration a compliant client cannot satisfy: it would send `"3"`, validation
would reject it, and the failure would land on whoever was filling in the form
rather than on whoever wrote the annotation. Annotate it `str` and convert
inside the handler.

## Progress, without a second mechanism

A tool that takes a while has a `ProgressReporter` waiting for it, and it is the
ordinary `wreath.progress` one:

```python
@mcp.tool(description="Re-index every sighting. Slow.")
async def reindex(request) -> dict:
    reporter = request.state.mcp.progress
    for done, total in walk_sightings():
        reporter.update(100 * done / total, f"{done} of {total}")
    return {"indexed": total}
```

The reporter is always there, whether or not the client asked for progress. When
the call carried `_meta.progressToken`, the server relays each report onto that
session's notification stream as `notifications/progress`; when it did not, the
reports still land in the registry, where a status route or your own SSE stream
can read them.

That is deliberate: there is no MCP-specific progress mechanism to learn.
`wreath.progress` already models a running task reporting a percentage and a
message, already has a status endpoint and a stream of its own, and already
spans workers when you give it the message bus — which is what makes a durable
job's progress reach the client whatever worker is holding the stream:

```python
from wreath.progress import ProgressRegistry

mcp = MCP(app, name="camera-trap", version="1.0.0",
          progress=ProgressRegistry(app.messaging("bus", database="app")))
```

Reports are polled every `progress_interval` seconds (a quarter of a second by
default), and percentages are a running commentary rather than a ledger: a
report that is superseded before the next poll is not sent, and nothing depends
on one arriving.

## Asking the client something

Three MCP methods travel the other way: the *server* asks and waits. A tool
reaches all three through `request.state.mcp`, and each answer arrives on a
separate request from the client while the call that asked is still parked —
which works because a `tools/call` has always run in its own task.

### Sampling: borrowing the caller's model

```python
@mcp.tool(
    description="Summarise this month's sightings on one trail.",
    sampling="Model::sample",
)
async def summarise_trail(request, trail: str) -> dict:
    notes = await load_notes(trail)
    answer = await request.state.mcp.sample(
        f"Summarise these field notes in two sentences:\n\n{notes}",
        max_tokens=200,
    )
    return {"summary": answer["content"]["text"]}
```

**A tool that did not declare `sampling=` cannot sample at all**, and that is
the important part. Whether a given tool may spend the caller's model on text of
its own choosing is a policy question, not a code one, so it is one more Cedar
action decided by the authorizer you already installed — and it shows up in
`mcp.declared_actions()` beside everything else a model can reach. Pass `True`
instead of an action to declare the capability with no policy attached, which is
what a development server wants and not what a deployment does.

Two more reuses come with it. A sampling request draws on the tool's **own**
`rate_limit=` bucket, so a tool that samples on every call spends its allowance
twice per invocation — which is the honest accounting, because the second half
is the expensive half. And it leaves its own Flight Recorder marker, the same
one a `tools/call` leaves, with an outcome of `sampled`, `sample_denied`,
`sample_throttled` or `sample_failed`.

### Elicitation: asking the person

```python
from dataclasses import dataclass

@dataclass
class Confirm:
    reason: str
    approve: bool


@mcp.tool(
    description="Retire a camera. Asks before it does.",
    elicitation="Form::ask",
)
async def retire_camera(request, camera_id: str) -> dict:
    answer = await request.state.mcp.elicit(
        f"Retire camera {camera_id}?", Confirm
    )
    if answer is None or not answer.approve:
        raise ToolError("nobody confirmed, so nothing was retired")
    return await retire(camera_id, reason=answer.reason)
```

**A tool that did not declare `elicitation=` cannot prompt at all**, exactly as
one that did not declare `sampling=` cannot sample — same keyword shape, same
`AuthRequirement`, same authorizer, same `mcp.declared_actions()`, and the
request spends the same rate-limit bucket. `elicitation=True` declares it with
no policy, which is what a development server wants and not what a deployment
does.

The reason for gating it is sharper than sampling's, and worth stating plainly
because a reader who does not see the risk will reach for `elicitation=True` and
ship it. An elicitation puts an arbitrary form **inside a UI the person already
trusts** — their editor, their chat client, their agent's own chrome. A tool that
asks for `{"password": str}` or `{"api_key": str}` looks exactly like the client
asking, and the model that chose to call that tool may have been talked into it
by text it read from a web page five turns ago. "The user is asked and can
decline" is not a control here; it is the *same* control that makes consent
dialogs a weak defence everywhere else, and phishing is the discipline of
getting past it. So which tools may put a prompt in front of a person is a
deployment's decision, written where every other one is —
`@mcp.tool(description=…, elicitation="Form::ask")`.

A refused prompt never reaches the client, counts in `mcp.elicitation_refusals`
and `mcp.unauthorized_calls` — a refusal, never a `tool_error` — and leaves a
marker with an outcome of `elicit_denied`. Watch that counter: an application
probing for a form it may not show is the signal that something is trying to
phish through your client's chrome. A tool that gets its answer records
`elicited`, one the person turned down records `elicit_declined`, and the rate
limit and the client's silence record `elicit_throttled` and `elicit_failed`.

The form is a dataclass and **its fields are the schema the client is asked
for** — through `derive_input_schema`, the same derivation a tool's
`inputSchema` comes from — and what comes back is validated by the same binding
layer that validates a call's arguments. There is no second schema language and
no hand-written check, so a client that fills a field in wrongly is refused with
the error shape you already know, counted in `mcp.schema_rejections`.

`None` means the person declined or cancelled; only an *accepted* answer becomes
an instance. MCP restricts an elicitation's schema to primitives, because a
client renders it as a form, so a field that is not one is refused when the form
is first used, with the field named.

Whatever the person typed is recorded on the request's canonical line under
`mcp.elicit.*`, through the same rules a call's arguments go through — a field
named `password` is recorded as present and never as a value, not even as a
fingerprint. A form is the *most* likely place for one to arrive, which is why
that path is shared rather than reimplemented.

### Roots: where the client says its files are

`roots/list` asks the client for the directories it considers in scope, and
Wreath **enforces** them. A root nobody consults is a comment the client wrote,
so it binds the one thing on this surface that touches a filesystem:

```python
mcp = MCP(app, name="camera-trap", version="1.0.0", file_root="/srv/frames")


@mcp.tool(description="Read one camera's notes file.")
async def read_notes(request, name: str) -> dict:
    return {"text": (await request.state.mcp.read_file(f"{name}/notes.txt")).decode()}
```

Two confinements, both real. The bytes are opened through the same walk static
files and the template loader use — component by component beneath a trusted
directory descriptor, refusing every symlink — so nothing above `file_root` is
reachable however the path is spelled, and a `..` or a symlinked component is a
`PermissionError` rather than a surprise. Then, when the client declared roots,
the file must also lie beneath one of them; a read outside them is refused and
counted in `mcp.roots_refusals`.

A server with no `file_root=` reads no files at all, which is the right default
for a surface a model drives. The roots are asked for once per session and
cached until the client sends `notifications/roots/list_changed`.

### What happens when the client does not answer

Every one of these is a request some other program has to answer, so each of
them has an ending that is not a hang:

- a client that never advertised the capability in `initialize` is refused
  immediately, naming the capability, rather than sent a request nothing will
  answer;
- a client that does not answer within
  `MCPLimits(client_request_seconds=...)` ends the wait with a
  `ClientRequestError` **and** a `notifications/cancelled` telling it to stop
  working on a question nobody is waiting for;
- `notifications/cancelled` on the outer `tools/call` cancels the inner request
  with it;
- ending the session — `DELETE`, idle expiry — fails every outstanding request
  rather than leaving a tool awaiting a future that belongs to a session that is
  gone;
- and the table of outstanding requests is bounded by
  `MCPLimits(max_pending_requests=...)`, because asking and never replying costs
  a client nothing and costs the server a future per attempt.

`ClientRequestError` is an ordinary exception raised inside the tool. Catch it
and raise a `ToolError` the model can read, or leave it to the boundary, where
it counts in `tool_errors` like any other unplanned failure.

## Completing an argument

A prompt argument annotated as a `Literal` or an `Enum` completes itself:

```python
@mcp.prompt(description="Draft a report on one trail.")
async def trail_report(request, trail: Literal["ridge", "creek", "ridgeway"]) -> str:
    return f"Summarise this month's sightings on the {trail} trail."
```

`completion/complete` answers from the values that annotation already declared,
filtered by whatever has been typed. There is deliberately no completion
decorator and no second registry: the candidate values are written down once, in
the annotation the binding layer validates against, so the menu and the
validation cannot disagree. An argument that declared nothing completes to
nothing, which is not an error.

Completions on a *resource* reference answer with nothing, because they complete
a templated URI's placeholder and Wreath declares no templated resources —
`resources/templates/list` already says the same thing by returning an empty
list.

## Running it over stdio

The supported deployment is the HTTP endpoint, because that is where
authorization and the audit trail are worth anything. But an editor on a laptop
usually speaks only stdio, so:

```bash
wreath mcp stdio app:app          # --path /mcp by default
```

That is a **byte relay over the application you already have**, not a second
server. Lines of JSON on stdin are POSTed at the same route, the reply is
written back to stdout, and the session's server-to-client stream is opened once
and written out as it arrives — so sampling and elicitation work here with no
extra code. The routing, the authentication, `MCPLimits`, the Flight Recorder
marker and the exception boundary are the endpoint's, because it *is* the
endpoint.

## Routes you already have

`expose_routes` turns selected existing routes into tools. Selected — there is
no `all=True`, and there will not be:

```python
from wreath.mcp import expose_routes

expose_routes(mcp, app, tags=("sightings",))
```

`fastapi-mcp` exposes every route by default. That is the wrong default for a
framework that ships an authorizer, because it converts an application's entire
HTTP surface into model-callable actions in one line, including the destructive
ones, and the person running that line usually had one endpoint in mind. So this
takes an explicit selector: `tags=`, `include=` (exact paths), or a `predicate=`
over the `RouteDefinition` — `lambda route: "GET" in route.methods` is how you
keep the mutating half of a tag out. A route matching **any** selector is
exposed, and a selector that matches nothing is an error rather than a server
with no tools on it.

Call it after the routes exist: the application is read there, not retained.

**What the route brings with it is the point.** The exposed tool carries the
route's own requirement — whatever `@authenticated`, `@roles`, `@permissions`,
`@authorize`, `@second_factor` and any router it was included from imposed — and
the identical dispatch runs it: the same Cedar decision, the same rate limiting,
the same Flight Recorder marker, the same five outcomes. Exposing a route is
never a way around what was put in front of it. A route protected by your
application's own authentication backend works too; the identity is resolved
lazily, on the first declaration that needs one — a gate or a rate limit — so an
endpoint of ungated, unbounded tools never runs the backend at all.

**And what a route brings that a tool cannot carry is refused, not dropped.** A
tool invokes the handler; it does not replay the route's chain, so a route
declared with its own `middleware=` or `dependencies=` — a `before` hook that
returns a `403`, a `Depends(require_api_key)` that raises — is refused at
`expose_routes`, naming the route and what it carries. Silently exposing it
would publish that handler to a model with strictly less in front of it than
the HTTP path has. Move the check into the handler, spell it as
`@authenticated`/`@roles`/`@permissions`/`@authorize`, which a tool *does*
carry, or narrow the selector and declare a tool of your own that calls the same
code. Application middleware added with `app.add_middleware(...)` is not
affected: it already covers the MCP endpoint, because an MCP call is a route
activation like any other.

**Step-up composes with this, and it is the most interesting member of that
list.** A route behind `@second_factor(max_age=300)` becomes a tool the model may
call only while the *person* at the other end has re-proved a factor within the
last five minutes:

```python
from wreath.auth import second_factor

@app.delete("/sightings", tags=("sightings",))
@second_factor(max_age=300)
async def purge_sightings(request) -> dict:
    """Delete every sighting."""
    ...

expose_routes(mcp, app, tags=("sightings",))
```

The claim the tool reads is the one `wreath.users`' `POST /auth/2fa/verify`
stamps on the session — see [Second factors](second-factors.md) — so nothing
MCP-specific is written anywhere, and a caller who never stepped up is refused
with `unauthorized_calls`, not `tool_errors`. `CedarAuthorizer` publishes the
same fact as `context.second_factor_age`, so `when { context.second_factor_age
<= 300 }` gates a tool from a policy file instead. Freshness is the point: holding
a factor is not the same as having just proved one.

Two things are refused, both at registration:

**A route with no docstring.** The description is the entire basis on which a
model decides whether this is the tool for the job, and a route's description is
its handler's docstring — the same text the OpenAPI document carries.

**A route whose signature an MCP call cannot fill.** A `tools/call` carries one
JSON object of arguments and nothing else, so a path placeholder, a header, a
cookie, an upload or a `Depends(...)` is a parameter no caller can supply. This
is the refusal route-derived tools meet far more often than declared ones, so
the message names the route as well as the parameter, and says what to do
instead:

```
cannot expose route /sightings/{sighting_id} as tool 'show_sighting':
parameter 'sighting_id' binds from a path placeholder. An MCP tools/call
carries a single JSON object of arguments, so every bound parameter must
come from it … Narrow the selector so this route is not chosen, or declare
a tool of your own that takes those values as arguments and calls the same
code.
```

That second remedy is usually the right one, and it is three lines: a
`@mcp.tool` taking `sighting_id: str` that calls whatever the route calls. It
also gives you a description written for a model rather than for a person
reading an API reference, which is worth more than the line it saves.

`rate_limit=` applies a `ToolRateLimit` to every tool the call declares, and
`prefix=` keeps two applications from colliding on `list_items`.

## What guards it

Nothing, until you say so. A bare `MCP(app, ...)` is exactly as protected as the
route is, which for a bare `Wreath()` means not at all — fine for a server that
reads public data on a private network, and wrong for anything else. Treat a
tool as an endpoint a persuadable third party can call with arguments of its
choosing, because that is what it is.

Pass `auth=` and the endpoint becomes an OAuth 2.1 **resource server**:

```python
from wreath.auth import JwtVerifier
from wreath.mcp import MCP, MCPAuth

provider = app.oidc_provider(
    "idp", issuer="https://idp.example", audience=None, http_client="idp"
)

mcp = MCP(
    app,
    name="camera-trap",
    version="1.0.0",
    path="/mcp",
    auth=MCPAuth(
        resource="https://api.example.com/mcp",
        authorization_servers=("https://idp.example",),
        verifier=provider.bearer_verifier(),
        scopes_supported=("mcp:tools",),
    ),
)
```

Three things follow from that, and the third is the one that matters.

**A metadata document.** `GET /.well-known/oauth-protected-resource/mcp` returns
the RFC 9728 description of this endpoint: its `resource` identifier and the
authorization servers it trusts. It is served without a token, because a client
that cannot read it cannot get one. The path follows the endpoint's path rather
than sitting at the root, so two MCP servers on one host publish two documents
instead of quietly sharing one; `mcp.metadata_path` tells you where yours is.

**A challenge that says where to go.** A request with no token gets a `401`
carrying `WWW-Authenticate: Bearer resource_metadata="https://…"`. A `401` that
names nothing tells a client it needs a token and nothing about where to get
one, which is how integrations end up hard-coding an issuer.

**Audience binding, which is the actual security property.** A verified token is
then checked against this endpoint's `resource`: its `aud` claim must contain it.
Without that check, a token a user was persuaded to mint for *somebody else's*
MCP server is a perfectly valid token here, and the confused deputy the model is
already holding becomes an authenticated one. `MCPAuth` performs the check
itself rather than trusting that your verifier was constructed with the right
`audience=`, because a deployment that got that wrong would have no symptom
until someone exploited it. Set `audience=` only when your authorization server
names this resource by some identifier other than its URL.

Sessions opened through this are bound to the token's subject, as they are
whichever way you authenticate — see
[The transport](#the-transport-and-the-one-thing-to-know-about-it).

**Dynamic client registration is not Wreath's job.** The MCP authorization spec
also describes clients registering themselves at the authorization server; that
is the authorization server's endpoint, not the resource server's, and it
belongs to whatever identity provider your deployment runs. Wreath tells clients
where that server is and verifies what it issued. It does not stand in for it.

## Deciding per tool

Authentication says who is calling. Cedar says what they may do, and it says it
per tool, because the tools on one server are not alike:

```python
@mcp.tool(
    description="Retire a camera that is no longer in the field.",
    action="Camera::retire",
    resource=lambda request: request.state.mcp.arguments.get("camera_id"),
)
async def retire_camera(request, camera_id: str) -> dict:
    ...
```

That is the same decision `@authorize(action=...)` makes on a route, through the
same `CedarAuthorizer` you installed with `app.configure_auth(backend,
authorizer)`, against the same entity shapes. A tool that declares an action and
finds no authorizer configured refuses the call rather than admitting it — a
declaration that silently does nothing is worse than no declaration. `action=`
implies authentication, exactly as `@authorize(...)` on a route does: a policy is
written about a principal, and admitting a call with no principal so the engine
can deny it is a slower route to the same answer with a worse message. When the
endpoint carries no `MCPAuth`, the caller is resolved through the application's
own authentication backend instead — lazily, on the first declaration that needs
one, so a server of ungated tools never runs it.

`mcp.declared_actions()` covers resources and prompts too. A resource is gated
on its own URI and a prompt on its own name, since both have a stable identity a
tool does not.

A route resolves its Cedar resource from the path; a tool has no path, so it
resolves it from `request.state.mcp.arguments` — the call's raw argument object,
published before the decision precisely so the decision can depend on *which
row* is being asked for. The arguments have already been validated against the
tool's `inputSchema` by the time the resolver runs, so a malformed call is
rejected before any policy is consulted.

`mcp.declared_actions()` returns every action a model can reach through this
server, grouped by resource type, read off what is enforced rather than a second
list someone has to remember to update.

A denied call comes back as a JSON-RPC error and counts in
`mcp.unauthorized_calls`. It is deliberately not a `tool_error`: a refusal and a
failure are different facts about a deployment, and a single counter for both
would hide which one you are looking at.

### Asking for the code again

`second_factor=` puts [step-up](second-factors.md) on a tool that was never a
route — the same declaration `@second_factor(max_age=...)` makes above, without
having to invent a route to hang it on:

```python
@mcp.tool(
    description="Delete every sighting from the archive.",
    action="Sighting::purge",
    second_factor=300,
)
async def purge_sightings(request) -> dict:
    ...
```

It is the same window, checked against the same `second_factor_at` stamp
`wreath.users` writes, and it implies authentication. An identity that carries no
stamp — a bearer token, an OIDC login — never satisfies it, so the default is
closed. Stacking it with an exposed route's own window keeps the shorter one:
merging requirements adds and never subtracts, in either direction.

This is what lets one server say *the model may read, and the human must
re-prove a factor before the model may delete*, which is a sentence worth being
able to write down.

## Bounds

`MCPLimits` holds them, in one object:

```python
from wreath.mcp import MCPLimits

mcp = MCP(
    app,
    name="camera-trap",
    version="1.0.0",
    limits=MCPLimits(
        max_tools=64,                  # tools this server may declare
        max_resources=256,             # resources it may declare
        max_prompts=128,               # prompts it may declare
        max_sessions=1024,             # concurrently live sessions
        max_concurrent_calls=8,        # calls one session may have in flight
        max_subscriptions=256,         # resource subscriptions per session
        max_pending_notifications=64,  # queued per session before dropping
        stream_keepalive_seconds=15.0, # before an idle stream says something
        session_idle_seconds=900.0,    # before an abandoned session is collected
        max_pending_requests=4,        # questions one session may have outstanding
        client_request_seconds=30.0,   # before one of them gives up waiting
        max_file_bytes=1024 * 1024,    # the largest file `read_file` returns
    ),
)
```

The three declaration ceilings are not defensive programming. Every tool,
resource and prompt is text a model reads *before every decision it makes*, so a
long list makes each of those decisions worse; refusing at registration puts
that trade in front of whoever is writing the declaration, which is the only
moment anyone can act on it.

**Payload size is deliberately not among them.** A `tools/call` body is a POST
body, and Wreath already refuses an oversized one before the endpoint is
entered. Set it where every other request's size is already set:
`Wreath(limits=RequestLimits(max_body_bytes=...))`. A second ceiling here would
be a second place to configure and a second place to forget.

Per-tool rate limits are declared on the tool, for the same reason its Cedar
action is:

```python
from wreath.mcp import ToolRateLimit

@mcp.tool(
    description="Re-scan every camera. Expensive; be sure.",
    rate_limit=ToolRateLimit(limit=5, window=60.0),
)
async def rescan(request) -> dict:
    ...
```

The bucket is keyed on the verified subject — never the client address, because
an MCP client is usually a gateway and every caller behind it would share one
bucket. Declaring a ceiling is enough to make the server resolve who the caller
is: a bounded tool authenticates the request *before* it charges the bucket,
through `MCPAuth` when the endpoint carries one and through the application's own
`app.configure_auth(...)` backend when it does not. Only when neither identifies
anybody does the key fall back to the session, and a session is free — so a
ceiling on an endpoint with no authentication at all is a ceiling per
`initialize`, which is to say none. A refused call carries `data.retryAfter` in
seconds and counts in `mcp.throttled`, which is neither a failure nor a refusal
on the merits.

## A retrieval tool, end to end

The most common application shape of 2026 is "let a model search my data", and
both halves of it are first-party here. `wreath.queries` ranks by
[vector similarity](vector-search.md), by [full text](full-text-search.md), or by
[a fusion of the two](hybrid-search.md), against the PostgreSQL the application
is already using. `wreath.mcp` puts one of those in front of a model. Neither
half knows about the other, and the join is a nine-line handler:

```python
from wreath.orm.session import Session
from wreath.queries import Param, Queries, fuse, query

class Notes(Queries[Note]):
    nearest  = query().order_by(Note.embedding.cosine_distance(Param("q"))).limit(3)
    matching = (query(Note.search.matches(Param("terms")))
                .order_by(Note.search.rank(Param("terms")).desc()).limit(3))
    hybrid   = fuse(nearest, matching).limit(4)

@mcp.tool(
    action="Note::search",
    resource="notes",
    rate_limit=ToolRateLimit(limit=30, window=60.0),
)
async def search_notes(request, terms: str, q: list[float]) -> dict:
    """Find notes matching `terms`, ranked by similarity to `q`."""
    session = Session(registry, "read")
    try:
        found = await Notes(session).hybrid(q=q, terms=terms)
        return {"notes": [{"id": n.id, "title": n.title} for n in found]}
    finally:
        await session.close()
```

Read what the handler does *not* do. It does not check who is calling: the Cedar
decision is `action=`, made by the same authorizer as a route. It does not count
anything: the ceiling is `rate_limit=`, keyed on the caller the server resolved
before charging it. It does not log: the call, the caller and the search terms
land on the Flight Recorder's ring, with the terms fingerprinted rather than
written, so an operator can ask *was this the same query* without publishing what
somebody searched for. Add `second_factor=` and it does not check that either.

So: a retrieval tool that is policy-gated, bounded per caller, and on an audit
trail, with no vector database, no separate search service, and no framework
outside the one already serving the application. `tests/test_cross_subsystem.py`
proves this composition against a live PostgreSQL with pgvector, including that
the fused ranking survives the MCP round trip.

Wreath does not produce the embeddings. `q` arrives from whatever model the
application uses; storing the vector, indexing it, and ranking by it are what is
in the box, and that boundary is deliberate — see
[Vector search](vector-search.md).

## What it records

Every `tools/call` leaves one marker on the Flight Recorder's ring, through
`wreath.logging` — the tool name, the caller's subject, the duration, and the
outcome (`ok`, `tool_error`, `raised`, `cancelled`, `denied`, `throttled`,
`schema_rejected`). One record per call, with the outcome named, so "how many
denials yesterday" is a filter rather than an inference from what is missing.
A tool that samples leaves a second marker at the same site, with an outcome of
`sampled`, `sample_denied`, `sample_throttled` or `sample_failed`: a server that
spends a caller's model is doing something an operator has to be able to
reconstruct, and giving that its own record format would have meant a second
field list and a second thing to remember to redact.

The arguments ride the request's canonical log line beside it, under two rules
that already existed and are not re-implemented here. Values follow
`wreath.logging`'s deny-by-default rule: a scalar is written, a string is
fingerprinted. Names follow `wreath.crud`'s sensitive-name pattern — the same one
that hides a password column from a generated CRUD endpoint — and a name it
matches is recorded as present and never as a value. Not even as a fingerprint:
a fingerprint of a password is an offline guessing oracle, and one of a session
token is a correlation handle.

```
MCP tool sign_in ok in 4.2ms for ada (session #86a195f1)
  mcp.arg.username    #70b353c8
  mcp.arg.password    <redacted>
  mcp.arg.attempts    2
```

Beside the record, the server keeps counters: `tool_calls`, `tool_errors`,
`schema_rejections`, `unauthorized_calls`, `throttled`, `expired_sessions`,
`resource_reads`, `resource_errors`, `prompt_renders`, `prompt_errors`,
`notifications_dropped`, `sampling_requests`, `sampling_refusals`,
`elicitations`, `elicitation_declines`, `elicitation_refusals`,
`client_request_timeouts` and `roots_refusals`. Read one at a time as attributes, or all of them as a dict
from `mcp.stats()` — the shape `messaging.MessageBus.stats()` uses, so an
exporter does not have to learn a new name every time one is added.

Publishing them is your route. Wreath's Prometheus, OpenMetrics, StatsD and OTLP
bridges render the *projector's* per-route aggregates — request counts,
durations, errors — and do not go looking for a subsystem's own counters, so
there is no bridge that picks these up on its own:

```python
@app.get("/internal/mcp-stats")
async def mcp_stats(request) -> dict:
    return mcp.stats()
```

That is a deliberate seam rather than a gap: an MCP server's counters say how
often a model was refused and by what, which is not something to expose without
first deciding who may read it.

## What is not here yet

`logging/setLevel` is defined by the revision and not implemented; it answers
`method not found` naming the stage it belongs to, rather than a bare error.
Stream resumption (`Last-Event-ID`), templated resources, and `listChanged`
notifications for the three listings are also absent; the capabilities this
server advertises say so rather than leaving a client to find out. A client that
POSTs `sampling/createMessage`, `elicitation/create` or `roots/list` is told
those go the other way, because "unknown method" would read as "this build is
too old".

Reference: [`wreath.mcp`](../reference/mcp.md). Recipe:
[Serve your first MCP tool](../cookbook/recipes/serve-mcp-tools.md).
