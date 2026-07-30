# Generate CRUD without leaking secrets

Some models just want the boring five endpoints — list, read, create, update,
delete — and hand-writing them for every table is tedious. Wreath generates them
from an ORM model, off by default and safe by default, because auto-CRUD's classic
failure is a `GET /users` that returns everyone's `password_hash`:

```python
from wreath.orm import Session

app.enable_crud()                        # 1. app-level opt-in (off by default)

def open_session(request):               # a fresh ORM session per request
    return Session(registry, "write")

app.crud(Account, open_session)          # 2. model-level opt-in — this model only
```

That mounts `GET /account` (paginated list), `GET /account/{id}`, `POST /account`,
`PATCH /account/{id}`, and `DELETE /account/{id}`. Both opt-ins are required —
`app.crud(...)` without `app.enable_crud()` raises, so CRUD can't be switched on
for a model without the app deciding to allow it at all. Any column whose name
looks like a secret — `password`, `*_hash`, `token`, `secret`, `salt`, `api_key`,
`ssn`, and friends — is automatically excluded from responses *and* rejected from
input, so a `password_hash` never serializes and a client can't set one:

```python
app.crud(
    Account,
    open_session,
    operations=("list", "retrieve"),      # read-only: no create/update/delete
    readonly=("owner_id", "created_at"),  # present in output, never accepted as input
    expose=("token_count",),              # opt a safe-but-sensitive-looking name back in
    prefix="/accounts",                   # default is "/<model name>"
)
```

Retrieval columns — a `Vector` embedding, a `TsVector` — are withheld the same
way, and that one is typed rather than guessed: an embedding in a list response
is about thirty thousand floats per page, and an embedding a client may `PATCH`
puts a row at the top of every semantic search without touching a word of its
visible content. `expose=("embedding",)` opts one back into both directions; a
generated column stays unwritable whatever you name. See
[Generating CRUD routes](../../guides/crud.md).

To expose a "sensitive-looking" column that's actually safe, name it in `expose`
— a deliberate, greppable act, never a default. Setting a real secret is
intentionally *not* something CRUD does for you; do it through a purpose-built
endpoint (or [`wreath.users`](../../guides/users.md)) where you can hash and
validate. If you'd rather mount it yourself, `crud_router(model, open_session,
...)` returns a plain `Router` for `app.include_router(...)`.
