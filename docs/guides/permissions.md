# Permissions in the UI

Every application with a real authorization model ends up maintaining it twice.
Once as the policies the server evaluates, and once in the frontend as a
scattering of `user.role === "editor"` checks deciding which buttons to render.

The second copy drifts, and it drifts *quietly*. A button that should have been
hidden is only a 403 the user did not expect; a button that should have been
shown is a feature nobody can find. Neither fails a test.

Wreath owns the Cedar engine **and** the typegen IR, so the second copy can be
deleted rather than maintained.

## User story: hiding what the user cannot do

> *The llama detail page has Edit and Delete buttons. Riders may do neither,
> editors may edit, admins may do both — and that rule already exists, in
> Cedar. I do not want to write it again in TypeScript.*

Mount the router:

```python
from wreath.authorization import permissions_router

app.include_router(permissions_router(app))
```

Four endpoints, all answered by the authorizer the app already uses:

| | |
| --- | --- |
| `GET /permissions` | the vocabulary — which actions exist, per resource type |
| `GET /permissions/manifest` | what this caller may **ever** do (with an `ETag`) |
| `GET /permissions/stream` | SSE: that manifest moved, ask again |
| `POST /permissions` | what this caller may do to **these rows** |

**All four require an authenticated caller**, the vocabulary included. That last
one is a deliberate closing: the vocabulary is the complete map of your
authorization surface — every resource type and every action you enforce — and
handing it to anonymous callers buys nothing, because the generated client
learns the vocabulary at *build* time, from your application object, and only
ever calls `POST /permissions` at runtime. An anonymous `GET /permissions` is a
`401`.

There is no configuration and no list of actions to keep in step, because the
vocabulary is read off your routes:

```python
@app.delete("/llamas/{llama_id}")
@authorize(action="Llama::delete", resource=...)
async def delete_llama(request): ...
```

`Llama::delete` is now something the UI can ask about. An action you never
declared is refused with a `400` — otherwise the endpoint would be an oracle
for probing policies that do not exist.

## One vocabulary, four protocols

Wreath serves REST, gRPC, MCP and GraphQL, and each of them declares
authorization in the same words. **All four land in the same vocabulary**, read
off their own declarations, so there is still exactly one list:

```python
@app.delete("/llamas/{llama_id}")                          # REST
@authorize(action="Llama::delete", resource=...)
async def delete_llama(request): ...

@tracker.unary(request=Position, response=Position,        # gRPC
               action="Collar::read", resource=...)
async def GetPosition(request, message): ...

@mcp.tool(action="Camera::read")                           # MCP
async def read_camera(request): ...

api = GraphQL(registry, models=[Llama], authorizer=authorizer)   # GraphQL
```

Nothing extra is wired up. A gRPC method **is** a route, so its `action=` is
already on the route table; the MCP endpoint and the GraphQL endpoint are one
route each in front of many declarations, so each of them tells the vocabulary
what it fronts — live, by reading its own registry, never by handing over a
copy that could go stale.

`wreath typegen` reads the same function, so the generated client's action
unions cover all four surfaces too. Declare a tool and the button that calls it
is typed.

### GraphQL is the interesting one, and it needs no new shape

A GraphQL endpoint names **one** action — the `action=` you constructed it with,
`"read"` by default — over resources that are *fields*: `User::"email"`,
`Query::"llamas"`, `Mutation::"retire"`. So it joins the vocabulary as its
schema types, and the existing two-endpoint split answers it exactly as it
answers everything else:

| question | endpoint | asks |
| --- | --- | --- |
| may this caller read `User` at all? | the manifest | `authorize("read", User::"…type-level")` |
| may this caller read `User.email`? | `POST /permissions` | `authorize("read", User::"email")` |

The second row is character for character the decision the GraphQL executor
takes, so a client can ask which columns to render in one call:

```json
POST /permissions
{"type": "User", "ids": ["id", "email", "phone"]}
```

That works because a **field is declared and finite** where a row is neither —
the same reason the manifest can enumerate actions but not llamas.

!!! note "An endpoint contributes only what it enforces"

    `GraphQL` takes its authorizer explicitly and evaluates no policy without
    one. An endpoint constructed with no `authorizer=` therefore adds nothing to
    the vocabulary, because advertising an action nothing checks would be the
    one kind of entry this document must never contain. If you hand GraphQL a
    *different* authorizer from the application's, the manifest still answers
    through the application's — hand both the same object, as
    [the GraphQL guide](graphql.md) already asks.

!!! warning "A wider vocabulary is still not a wider promise"

    Four protocols in the manifest does not make the manifest authoritative for
    any of them. It is chrome, exactly as before: a stale or coarse answer draws
    a button that then 403s, and every surface takes its own decision again on
    the actual call. A GraphQL type-level `yes` does **not** mean every field of
    that type is readable, an MCP entry does not mean the tool's rate limit will
    admit you, and neither is a substitute for the check on the route.

## Two questions, two endpoints

This distinction is the one worth internalising:

* **"Can this user ever edit a llama?"** — the *manifest*. Nav items, route
  guards, whether the Edit column exists at all. One fetch per session.
* **"Can this user edit llama 7?"** — the *batch endpoint*. A Cedar decision
  generally depends on the resource, so no manifest can enumerate rows.

A table of fifty llamas is **one** call, not fifty:

```json
POST /permissions
{"type": "Llama", "ids": ["7", "8", "9"]}

{"type": "Llama", "permissions": {"7": ["Llama::edit", "Llama::read"], ...}}
```

### The list is bounded, and going over it refuses

One request costs `len(ids) × len(actions)` policy evaluations, so the length of
that list is the whole cost — and an unbounded list is a denial of service any
authenticated caller can post. **At most 200 ids per request**, which is a
generous UI page; raise or lower it explicitly:

```python
app.include_router(permissions_router(app, max_ids=500))
```

Over the ceiling the endpoint answers `400` and names the limit. It does **not**
truncate, and that is the interesting half of the decision: a silently shortened
answer draws a table whose remaining rows look unauthorized, which is a UI that
is confidently wrong. A refusal is only a UI that is absent, and the client can
page. Unbounded cardinality refuses.

### If your authorizer is remote, the round trips are bounded too

`max_ids` bounds how much work a request asks for. `max_concurrency` bounds how
much of it is in flight at once:

```python
app.include_router(permissions_router(app, max_concurrency=16))
```

With the built-in Cedar engine this changes nothing you can observe — it runs
in-process and never yields, so the evaluations happen in the same order either
way. It matters when the authorizer is **remote**, which the `CedarEngine`
protocol invites: there each evaluation is a round trip, and asking for them one
at a time makes a full batch `max_ids × len(actions)` round trips end to end.
Eight at a time, it is that number divided by eight.

It is a ceiling rather than "as many as possible" on purpose. Firing the whole
product at once would fix this endpoint's own denial of service by pointing one
at your authorization service instead, and a burst of six hundred is a burst of
six hundred whoever receives it.

One requirement comes with this: **an authorizer must tolerate concurrent calls
carrying a single request.** The built-in one does — it reads the request and
never writes to it. If yours cannot, `max_concurrency=1` returns evaluation to
strictly sequential and nothing else changes.

## User story: the client that stops asking

> *We call the permissions endpoint on every page transition. It is fast, but
> it is still a round trip in front of every render.*

Fetch the manifest once at sign-in and revalidate it:

```
GET /permissions/manifest
If-None-Match: W/"3f2c…"

304 Not Modified
```

The `ETag` covers everything that can change the answer — who is asking, the
roles they hold, the policy set itself, and the caller's enabled
[feature flags](auth.md#feature-flags-in-a-policy) when a policy reads them. So
a **promotion** invalidates that user's manifest, a **deploy that widens a
rule** invalidates everyone's, a **flag flip** invalidates the manifests it
changes, and nothing else does. Until one of those happens the client holds the
document and sends nothing but a conditional request.

The policy half of that tag is derived from the policies themselves, so every
worker parsing the same policy text produces the same tag — a conditional
request still gets its `304` when the load balancer sends it somewhere else, or
after a restart that did not change the rules. `CedarPolicies` supplies that
text through its read-only `source`; a custom engine can offer `fingerprint`,
`source`, or `policies` and be treated identically. One that exposes none of
them gets a random tag minted once per engine instance instead: still correct,
still moves on a reload, but no longer comparable across workers.

!!! note "Neither endpoint is enforcement"

    Both are hints for drawing a UI. The policy is evaluated again on the next
    real request, so a stale manifest can only ever draw a button that then
    403s — never permit something. Keep `@authorize` on the route; that is what
    is actually protecting it.

## User story: the herd lead who was promoted an hour ago

> *Bo was made a paddock lead this morning. He has had the trek planner open
> since breakfast, and the "Schedule a trek" button is still not there. He
> reloads. Now it is. That reload is the whole bug: nothing told his browser
> that the answer had changed.*

Two things can move a manifest, and Wreath can see both — its own policy set,
and a committed write to the table that grants Bo his role. So it can tell him:

```python
from wreath.authorization import permission_document, permissions_router

permissions = permission_document(
    app,
    roles_model=Membership,                          # a write here is a promotion
    bus=app.messaging("bus", database="app"),         # ... on whichever worker took it
)
app.include_router(permissions_router(app, document=permissions))
```

```ts
const changes = new EventSource("/permissions/stream");

changes.addEventListener("change", (message) => {
  const { etag } = JSON.parse(message.data);
  if (etag !== held) refetchManifest();       // conditional; a 304 is cheap
});
```

Bo's button appears without a reload, and without a poll: the client fetches the
manifest once and then waits to be told.

Both halves need something only the framework has. `roles_model=` works because
the ORM already announces which models a committed transaction wrote — an
external authorization service cannot know your `memberships` table was written,
and a frontend library can only poll. `bus=` matters because the promotion was
committed by whichever worker took that request, and Bo's stream is open on a
different one; the announcement crosses in one hop over the database you already
have. No Redis, and no second service.

`roles_model=` is named rather than guessed, because Wreath cannot know which of
your tables grants a role. Leave it out and the stream still runs — it still
notices a policy-set change, and you can still push one yourself:

```python
permissions.notify_all("policies")     # e.g. after reloading the policy file
```

Announcements are model-grained, not row-grained: a write to `Membership` tells
*every* open stream to revalidate, not only Bo's. That is the deliberate trade —
the cost is one conditional request each, and a conditional request that finds
nothing changed is a `304`.

!!! warning "The stream is at-most-once, and that is why it is safe"

    It is an ephemeral fan-out over a connection that can drop, so a
    **narrowing** change may arrive late or not at all. Bo losing the lead role
    may leave the button on screen for a while.

    That is acceptable only because **enforcement stays on the route**. The
    manifest is chrome: a stale one can draw a button that then 403s — annoying,
    cosmetic — and it can never permit anything, because `@authorize` evaluates
    the policy again on the actual request. Treat the stream as a permission
    cache with a push, and drop the route check because "the UI knows", and you
    have built the one thing this design cannot make safe.

!!! note "Feature flags move the answer, and the stream will not tell you"

    Once a policy reads `context.flags`, a **flag flip** changes what a caller
    may do. The manifest's `ETag` covers that, so the next conditional request
    returns a fresh document — but a flip is not a policy change, so no stream
    event announces it, and an open stream keeps quiet until something else
    moves.

    This is the same optimistic-chrome property as a route behind
    `@second_factor`, which the manifest can also read as permitted and then
    answer 403 on: the manifest models *what the policies say about you*, not
    every condition evaluated at the moment you act. It stays safe for the same
    reason — enforcement is on the route, and chrome drawn a little too
    generously costs an unexpected 403, never an unauthorized success. If a
    flag gates something whose button must vanish promptly, flip it alongside a
    policy change, or poll the manifest rather than waiting to be told.

Two more properties worth knowing, because they are the ones a hand-rolled
version usually misses. Subscriptions are **bounded** — per principal and
overall — and a caller past the cap gets a `503` rather than a slot nobody
frees; it falls back to revalidating the manifest, which is this feature minus
the push. And a stream **holds no database connection** while it waits, so a
thousand idle tabs do not become a thousand idle transactions.

## User story: a typed client, generated

> *I want `canEdit` in my component, not a string comparison against an action
> name I might typo.*

`wreath typegen` emits a permissions module alongside the models and client,
built from the same vocabulary:

```ts
export type LlamaAction = "Llama::delete" | "Llama::edit" | "Llama::read";

export interface LlamaPermissions {
  canDelete: boolean;
  canEdit: boolean;
  canRead: boolean;
}
```

With `--react-query`, a hook comes too:

```tsx
function LlamaActions({ llama }: { llama: Llama }) {
  const { canEdit, canDelete } = usePermission(baseUrl, "Llama", llama.id);

  return (
    <>
      {canEdit && <EditButton llama={llama} />}
      {canDelete && <DeleteButton llama={llama} />}
    </>
  );
}
```

Ask about an action the API does not enforce and it is a **compile error**,
because the union came from the server. That is the property a hand-written
copy of the rules can never have: it fails at build time instead of drifting
until someone notices.

An application that declares no policies gets no permissions module — there is
nothing to say.

## User story: testing per role

> *I want to assert that a rider gets 403 and an editor gets 200, without every
> test carrying a fake token.*

```python
async with TestClient(app) as client:
    admin = client.acting_as("root", roles=["admin"])
    editor = client.acting_as("ada", roles=["editor"])
    rider = client.acting_as("bo", roles=["rider"])

    assert (await rider.delete("/llamas/7")).status == 403
    assert (await editor.get("/llamas/7")).status == 200
    assert (await admin.delete("/llamas/7")).status == 200
```

The identity rides the request rather than the backend, so `admin` and `rider`
can have requests in flight at once and cannot interfere. `acting_as` also
accepts a whole `Identity` when you need one with permissions or a non-default
type.

!!! warning "It bypasses authentication"

    While an acting-as client exists, the application's authentication backend
    is replaced by one that trusts the request scope; it is restored when the
    client exits. That is the right trade for an authorization test and the
    wrong one for a test *of* authentication — use a real token there.

## Why this cannot be a plugin

An external authorization service can answer "may this principal do this" — but
it does not know which actions your routes declare, so the vocabulary becomes a
third copy. A frontend library can cache permissions — but it cannot know when
your policy set changed, and it certainly cannot know that a row in your
`memberships` table was written a moment ago, on another worker. Its only option
is to poll. The value here is not the evaluation; it is that the vocabulary, the
answer, the change signal, the generated types, and the enforcement all come
from the same declaration.
