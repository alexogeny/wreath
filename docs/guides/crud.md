# Generating CRUD routes

Some models want the boring five endpoints — list, read, create, update, delete —
and writing them by hand for every table is tedious. Wreath can generate them from
an ORM model. It is **off by default and safe by default**, because auto-CRUD's
classic failure is a `GET /users` that returns everyone's `password_hash`.

## User story: quick admin endpoints for an internal model

> *As an API author, I have a `Widget` model behind an internal tool. I want the
> standard REST endpoints without hand-writing them — but I never want a stray
> secret column to leak, and I don't want CRUD turned on for models I didn't
> choose.*

```python
app.enable_crud()                        # 1. app-level opt-in (off by default)

def open_session(request):               # a fresh ORM session per request
    return Session(registry, "write")

app.crud(Widget, open_session)           # 2. model-level opt-in — this model only
```

That mounts:

| Method | Path | Does |
|---|---|---|
| `GET` | `/widget` | paginated list (`?page=&size=`) |
| `GET` | `/widget/{id}` | one row, or `404` |
| `POST` | `/widget` | create from a JSON body |
| `PATCH` | `/widget/{id}` | partial update |
| `DELETE` | `/widget/{id}` | delete, `204` |

Both opt-ins are required. `app.crud(...)` without `app.enable_crud()` raises —
CRUD can't be switched on for one model without the app deciding to allow it at
all.

The primary key is converted according to the model's declared column type, not
according to what the path segment looks like, so a text or UUID key is never
coerced to an integer. A body the model rejects answers `422`; the exception
text travels only when it came from the model's own validation, because a driver
error's message carries table names and constraint identifiers.


### Naming what may leave

`expose` is the escape hatch on a deny-list, and **a deny-list over column names
is a backstop, not a security boundary.** It matches names that *look* like
secrets, so the column nobody thought about is withheld rather than published.
It cannot match `iban`, `date_of_birth`, `tax_id`, or `passport_number` — and
widening the pattern until it does would start withholding ordinary columns
instead, which fails in the other direction. When the model holds anything of
that shape, name the columns instead:

```python
crud_router(Patient, open_session, fields=("id", "name"))   # only these
```

`fields` is an allow-list: it survives somebody adding a column, which is the
case the deny-list cannot cover. Mutually exclusive with `expose`.

Treat the deny-list as insurance against oversight and `fields` as the decision.
Reviewing a `crud_router` call means reading its `fields`; if there is no
`fields`, the review question is what the model may hold next year, not what it
holds today.


## Secrets are hidden and unwritable by default

Any column whose name looks like a secret — `password`, `passcode`, `*_hash`,
`token`, `secret`, `salt`, `api_key`, `signing_key`, `pin`, `cvc`, `ssn`,
`account_number`, `recovery_code`, and friends — is **excluded from responses and
rejected from input**, automatically. `public_key` is deliberately not on that
list, being public by definition:

```python
class Account(Model, table="accounts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text)
    password_hash: Mapped[str] = column(Text)     # never serialized, never accepted

app.crud(Account, open_session)
# GET /account/1 -> {"id": 1, "email": "..."}   (no password_hash)
# POST /account {"password_hash": "..."}         -> the field is silently dropped
```

To expose a "sensitive-looking" column that is actually safe, name it explicitly —
a deliberate, greppable act:

```python
app.crud(Account, open_session, expose=("token_count",))   # opt one field back in
```

Setting a real secret (a password) is intentionally *not* something CRUD does for
you — do it through a purpose-built endpoint (or [`wreath.users`](users.md)), where
you can hash and validate.

## Retrieval columns are withheld, both ways

A `Vector` embedding and a `TsVector` are how rows are **found**, not what they
say. Generated CRUD treats them as infrastructure: neither is serialized into a
response, and neither is accepted from a request body. Unlike the sensitive-name
deny-list this is not a guess — it is the declared column type, so it cannot miss
one and cannot catch an ordinary column by accident. `retrieval_fields(Model)`
returns exactly what is withheld.

```python
class Doc(Model, table="docs"):
    id: Mapped[int] = column(Int64, primary_key=True)
    title: Mapped[str] = column(Text)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(Vector(1536), nullable=True,
                                     index="hnsw", index_ops="vector_cosine_ops")
    search: Mapped[bytes] = column(TsVector("english", sources=("title", "body")),
                                   index="gin")

app.crud(Doc, open_session)
# GET  /doc      -> {"items": [{"id": 1, "title": "...", "body": "..."}], ...}
# PATCH /doc/1 {"embedding": [...]}  -> the field is silently dropped
```

Two different reasons, both worth stating.

**On the way out it is bandwidth.** A `tsvector` is derived from columns that are
already in the same payload — it is noise by construction, and it serializes as a
base64 string when explicitly exposed. A default page of twenty `Vector(1536)`
rows is about thirty thousand floats nobody asked for, on every list request.

**On the way in it is control.** Ranking is the one thing a row's own editor can
change *invisibly*: for an application whose search is semantic, anyone permitted
to `PATCH` a row could otherwise put it at the top of every query while its
visible content stayed exactly as it was.

To opt one back in, name it in `expose=` — the same deliberate, greppable act the
sensitive-name deny-list uses, and it widens both directions at once:

```python
app.crud(Doc, open_session, expose=("embedding",))
# GET  /doc/1                        -> ..., "embedding": [0.01, ...]
# PATCH /doc/1 {"embedding": [...]}  -> accepted
```

That is the right shape when the client computes the vector itself. When an
embedding is produced server-side — a [durable job](jobs.md) after the row is
written — leave it withheld.

**A generated column has no input opt-in.** Every `TsVector` is
`GENERATED ALWAYS AS (...) STORED`, so PostgreSQL recomputes it on every write
and there is nothing a client could usefully send. `expose=("search",)` makes it
*readable*; nothing makes it writable. Like the primary key and `readonly=`
columns, it is dropped from the body rather than rejected, so a client that sends
one gets the row it would have got.

**[GraphQL](graphql.md) withholds them too**, through this same
`retrieval_fields`, so the two generated surfaces cannot disagree about what
they publish. It has no input half to widen: no mutation is generated there.

**A `NOT NULL` vector column and a generated `POST` do not mix.** If the column
is required and no longer writable, `POST /doc` has no way to supply it. Give it
a server default, declare it `nullable=True` (the usual shape when a background
embedder fills it in later), or `expose=` it.

## Shaping the surface

```python
app.crud(
    Widget,
    open_session,
    operations=("list", "retrieve"),     # read-only: no create/update/delete
    readonly=("owner_id", "created_at"),  # present in output, never accepted as input
    exclude=("internal_notes",),          # never serialized at all
    expose=(),                            # withheld columns to include anyway
    prefix="/widgets",                    # default is "/<model name>"
    page_size=50,
)
```

`crud_router(model, open_session, ...)` returns a plain
[`Router`](../reference/router.md) if you'd rather include it yourself (with a
prefix, tags, dependencies, or permissions) via `app.include_router(...)`.

## Authorization, per operation

Generated routes are wide open by default — fine behind an already-authenticated
router, dangerous on the edge. Pass `authorize=` to lock each operation down with
the same [roles, permissions, and Cedar](auth.md) machinery the rest of wreath
uses. A rule can target one operation, a group (`read` = list + retrieve,
`write` = create + update + delete), or `"*"` as the default; the most specific
key wins.

```python
from wreath.crud import Access

app.crud(Widget, open_session, authorize={
    "read":   Access.public(),                      # anyone, logged in or not
    "create": Access.roles("editor", "admin", mode="any"),
    "update": Access.roles("admin"),                # only admins
    "delete": Access.deny(),                        # nobody — the route 403s
})
```

Role, permission, and Cedar rules attach as route metadata that the app enforces
in its [single-pass pipeline](../perf/index.md#the-request-pipeline-routing-auth-dispatch),
*before* your handler and before any database work — a denied `PATCH` never opens
a session. `Access.deny()` answers `403` unconditionally (the route still exists,
so it's a deliberate "forbidden", not a confusing `404`); to remove an operation
entirely, leave it out of `operations=`.

### And prove it again: step-up on a generated route

`DELETE /{id}` is the operation most likely to want more than "you are an admin".
`.within(seconds)` adds [step-up](second-factors.md) to any rule — it composes
with the rule rather than replacing it, so the roles check still runs and the
freshness check runs on top of it:

```python
app.crud(Account, open_session, authorize={
    "read":   Access.authenticated(),
    "delete": Access.roles("admin").within(300),     # ... and a factor, in the last 5 min
})
```

Two admins with identical roles now get different answers, which is the whole
point: the one who proved a factor four minutes ago gets `204`, and the one who
logged in this morning and has not touched a code since gets `403` with
`second_factor_required`. The remediation is `POST /auth/2fa/verify`, after which
the same request succeeds.

It is refused on `Access.public()` and `Access.deny()`. Step-up implies an
identity, so a public rule carrying one would quietly stop being public, and a
`deny()` already refuses everyone. Build it from `authenticated()`, `roles()`,
`permissions()` or `cedar()`.

### Cedar, and richer decisions

`Access.cedar(action=..., resource=...)` attaches a policy decision that your
app's configured [`CedarAuthorizer`](../reference/authorization.md) resolves — that
authorizer (its principal / resource / entity mappers) is the adapter layer when
the evaluation gets involved:

```python
app.crud(Document, open_session, authorize={
    "update": Access.cedar(action='Action::"edit"', resource='Document::"{id}"'),
})
```

The `{id}` (and any other path parameter) is filled in from the request. For a
decision that needs the **loaded row** — ownership, tenant match, a Cedar entity
built from the record's own attributes — pass `object_authorizer=`, run after the
row is fetched:

```python
def same_tenant(request, op, instance) -> bool:
    return instance.tenant_id == request.identity.claims["tenant"]

app.crud(Invoice, open_session,
         authorize={"read": Access.roles("staff")},
         object_authorizer=same_tenant)   # -> 403 when it returns falsey
```

`object_authorizer` also runs over each row of `GET /`, so a model whose rows
are protected individually is not readable in bulk. A page may therefore come
back shorter than `size`.

`object_authorizer` may be async and may return a bool or an
`AuthorizationDecision`; it runs on retrieve, update, delete (the loaded row) and
on create (the new instance, before it is committed).
