# GraphQL

Wreath serves GraphQL from the models you already have. The schema is derived
from the ORM registry — the same `ModelSpec` the SQL compiler, OpenAPI, and
typegen read — so the GraphQL surface cannot drift from the REST one or from the
database.

```python
from wreath.graphql import GraphQL

api = GraphQL(app.orm("main"), models=[User, Post])
app.include_router(api.router())
```

That serves `POST /graphql`. Nothing else is required: types, root fields, and
relationship traversal all come from the models.

## What the schema looks like

Each exposed model contributes an object type, a singular root field, and a
plural one:

```graphql
type User {
  id: Int!
  email: String!
  created_at: String
  posts: [Post!]!
}

type Query {
  user(id: ID!): User
  users(limit: Int, offset: Int): [User!]!
}
```

Nullability comes from the column definition, so a `NOT NULL` column is
non-null in GraphQL without you restating it.

## Relationships are batched, not N+1

This is the reason to use Wreath's GraphQL rather than mount a library beside it.

```graphql
{ users(limit: 50) { id posts { title } } }
```

That is **two** statements, not fifty-one. A relationship selection is not
resolved per parent: the whole level is collected and handed to the session's
batched select-in loader, with the identity map deduplicating. Other GraphQL
stacks reach for a DataLoader layer to approximate this; here it is the layer
underneath, and you get it without writing one.

## Computed fields, batched by default

Columns and relationships come from the models. Everything else is a resolver —
and a resolver sees **the whole level**, not one object:

```python
@api.field("User", "displayName", returns="String")
async def display_name(users, info):
    return [f"{u.name} <{u.email}>" for u in users]
```

One call for fifty users, not fifty. This is deliberate: per-parent resolvers are
how application code reintroduces the N+1 the data layer just solved, so the
batched form is the one that is easy to write. When work genuinely cannot be
batched, ask for the other shape explicitly:

```python
@api.field("User", "avatar", returns="String", batch=False)
async def avatar(user, info):          # one object at a time
    return await gravatar(user.email)
```

Resolvers may be `async` or plain functions.

## Chained resolvers

A computed field that needs another field says so. The executor topologically
orders the level so the dependency is resolved — in batch — first:

```python
@api.field("User", "postCount", returns="Int", requires=["posts"])
async def post_count(users, info):
    return [len(user.posts) for user in users]
```

```graphql
{ users { id postCount } }
```

Two statements. The `posts` relationship is loaded once for the whole level
because `requires` declared it, and it is **not** added to the response —
asking for a computed field never silently widens the payload.

Chains compose to any depth, and the order you *select* fields in does not
matter:

```python
@api.field("User", "a", returns="Int")
async def a(users, info): ...

@api.field("User", "b", returns="Int", requires=["a"])
async def b(users, info): ...

@api.field("User", "c", returns="Int", requires=["b"])
async def c(users, info): ...
```

`{ users { c b a } }` runs `a`, then `b`, then `c`.

**A cycle or a missing dependency is a startup error**, raised when the router is
built — not on the first request that happens to select that field.

## Custom roots and mutations

A root field needs no backing table:

```python
@api.query("search", returns="User", is_list=True)
async def search(info):
    term = info.arguments["term"]
    return await info.session.fetch(User.select().where(User.email.like(f"%{term}%")))
```

```python
@api.mutation("createUser", returns="User")
async def create_user(info):
    user = User(email=info.arguments["email"])
    info.session.add(user)
    await info.session.flush()
    return user
```

Mutations live in their own namespace on purpose: they are the write surface, so
one policy can cover **all** writes without enumerating them.

The resolver receives a small `info`: `request`, `session`, `arguments` (with
variables already substituted), `path`, and `parent_type`. Nothing from the
executor's internals, so the execution strategy stays free to change.

## Exposure is opt-in

```python
GraphQL(app.orm("main"), models=[User, Post])   # only these two
```

Passing `models=None` exposes every model in the registry, which is convenient
in development and rarely right in production — a registry holds every table the
application has, including ones with no business being queryable from the
internet.

A relationship pointing at a model you did **not** expose is dropped from the
schema rather than exposed transitively, so narrowing actually narrows.

## Safety limits

A public GraphQL endpoint is a denial-of-service surface: a tiny document can
demand an enormous amount of work. Five limits apply, enforced during the parse,
and they can be widened but not switched off.

```python
from wreath.graphql import GraphQL, Limits

GraphQL(
    registry,
    models=[User, Post],
    limits=Limits(
        max_document_bytes=16 * 1024,  # rejected on len(), before scanning
        max_depth=12,                  # selection nesting
        max_complexity=1000,            # total selected fields
        max_aliases=50,                 # aliases of one field
        max_steps=200_000,              # token budget backstop
    ),
)
```

| Limit | Attack it stops |
| --- | --- |
| `max_document_bytes` | Sheer size. Parse cost scales with length, and this is checked before a character is read. |
| `max_depth` | `author { posts { author { posts { … } } } }` over a cyclic schema. |
| `max_complexity` | Width — thousands of sibling fields in one document. |
| `max_aliases` | `a: user b: user c: user …` — one expensive resolve each, document stays tiny. |
| `max_steps` | Anything neither deep nor wide: a pathological token stream. |

**Fragment cycles are refused outright.** A fragment that reaches itself expands
forever, and no selection-set depth limit bounds it because the cycle is in the
fragment graph rather than the syntax tree — the document itself can be three
lines long.

A limit breach comes back as a normal GraphQL error with a machine-readable
code, so you can alert on the abusive class (`depth`, `complexity`, `aliases`,
`steps`, `document_size`, `fragment_cycle`) separately from ordinary `syntax`
errors, which are usually just a client bug.

## Authorization, in one language

Every field, relationship, and root has an authorization **resource** — by
default `Type.field` for fields and `Query.name` / `Mutation.name` for roots.
Wire the app's authorizer and Cedar covers REST and GraphQL with one policy set:

```python
api = GraphQL(registry, models=[User, Post], authorizer=app.authorizer)
```

| Selection | Resource asked |
| --- | --- |
| `{ users { … } }` | `Query.users` |
| `{ user { email } }` | `Query.user`, then `User.email` |
| `{ user { posts { … } } }` | `User.posts`, then `Post.*` |
| `mutation { createUser }` | `Mutation.createUser` |

Because roots are resources too, `Query.users` denies a whole entry point
without touching field policies — and `Mutation.*` in a Cedar policy denies
every write at once.

A resolver can name its own resource when the field's identity is not the
policy's:

```python
@api.field("User", "balance", returns="Int", policy="billing.read")
async def balance(users, info): ...
```

**Decisions are cached per request.** The same field under three aliases, or on
three levels of a nested query, is one authorizer call — not one per occurrence.

### Denied fields: fail, or null

```python
GraphQL(..., on_denied="error")   # default: the query fails
GraphQL(..., on_denied="null")    # the field is null, the rest still answers
```

`error` is the default because a null is indistinguishable from real data, and
silently blanking a field can be read as "this user has no email" rather than
"you may not see it". Choose `null` when partial results are genuinely more
useful to the client than a failure — a dashboard that should still render.

## Cost weighting

`max_complexity` counts *weight*, not selections, so a field that fans out can
say so:

```python
@api.field("User", "recommendations", returns="Post", is_list=True, cost=25)
async def recommendations(users, info): ...
```

Derived fields are weighted already: a column costs 1, a relationship 5, a list
root 10. A query selecting a handful of expensive fields is therefore bounded
the same way as one selecting hundreds of cheap ones.

## Typegen: one client, both surfaces

```bash
uv run wreath typegen --target typescript --out ./src/api.ts
```

GraphQL operations are folded into the **same** IR as the REST ones, and models
are merged by name. A type emitted for a REST response is reused rather than
duplicated, so a consumer gets `useGetUser()` and the GraphQL root returning the
identical `User` interface. There is no second codegen pipeline to keep in step.

## Observability

Each field resolve is recorded as a `RESOLVER` phase in the Flight Recorder,
carrying the level's object count. Per-field latency attribution is the
observability GraphQL users most often want and hardest to retrofit; here it
needs no exporter wiring.

## Sensitive columns

A column whose name looks like a secret — `password`, `*_hash`, `token`,
`secret`, `salt`, `api_key`, and the rest of the list `wreath.crud` uses — is
**left out of the schema**. Both surfaces are generated from one `ModelSpec`, so
it would be strange for the REST one to hide a password hash and the GraphQL one
to answer `{ user { passwordHash } }`. Name a column in `expose` to put it back:

```python
GraphQL(registry, models=[User], expose=("User.api_key",))   # or just "api_key"
```

A hidden column is absent from the SDL as well as from execution, so it is not
discoverable either.

## Transport

`POST /graphql` requires `content-type: application/json` (or
`application/graphql+json`) and answers `415` otherwise. That is what keeps a
cross-origin `<form>` from reaching a mutation: a `text/plain` POST is a *simple
request*, so a browser sends it — with the caller's cookies — without the
preflight a CORS policy could refuse.

**The endpoint is exactly as public as the route you mount it on**, and field
policies are evaluated only when you pass an `authorizer=`. Mount it behind
`@authenticated()` (or an `include_router(..., permissions=...)`) unless the
schema is genuinely public.

## Introspection

Off by default — a schema dump is reconnaissance. Turn it on deliberately:

```python
GraphQL(registry, models=[User], introspection=True)   # GET /graphql -> SDL
```

## Cost, measured

Parsing a 169-character, 15-field document costs about 33µs; a repeat of the
same document costs about 0.11µs, because parsed documents are cached by source
text in a bounded LRU. Real clients send a fixed set of queries, so steady-state
parse cost is the cached figure.

The uncached path is the one worth bounding, which is what `max_document_bytes`
is for. If you serve genuinely unbounded distinct documents, lower it.
