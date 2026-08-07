---
description: One model declaration, and everything wreath derives from it — DDL, routes, an admin, an OpenAPI document, a typed client, and a permission manifest.
keywords: model first, derive, generate, one declaration, crud from model, scaffolding, what do I get from a model
---

# From one model to a working stack

Most of what an application needs from a table is mechanical. The DDL, the five
routes, the back-office screen, the API document, the client that calls it, the
list of what a caller may do — each is a restatement of the same declaration, and
each is a place where a hand-written copy drifts from the others.

Wreath derives all of them from the model. The pieces are documented separately,
which is correct, but nothing says they compose in one order — so here it is as a
single path, with what each step reads and what it produces.

```
             models.py            <- you write this
                 │
    ┌────────────┼──────────────┬──────────────┐
    ▼            ▼              ▼              ▼
 migrations   crud_router     Admin       return types
 (your DDL)   (5 routes)   (a console)   (the contract)
                 │              │              │
                 └──────────────┴──────┬───────┘
                                       ▼
                              the OpenAPI document
                                       │
                        ┌──────────────┴─────────────┐
                        ▼                            ▼
                  wreath typegen             permissions_router
                (TS client + hooks)        (what may I do?)
```

Nothing in that picture is generated *into your repository* except the migration
artifact and the TypeScript client, and both are build products with a `--check`
mode. Everything else is computed at startup from the declaration, so it cannot
be stale.

## 1. Declare the model

```python
# shop/models.py
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Float8, Int64, Text

SCHEMA = "shop"


class Item(Model, table="item", schema=SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, unique=True)
    price: Mapped[float] = column(Float8)


MODELS = (Item,)
```

Two defaults are the opposite of what you may expect, and both are deliberate:
the PostgreSQL type is named explicitly rather than inferred from the Python
annotation, and a column is `NOT NULL` unless you say otherwise. A nullable
column nobody chose is a query with an edge case nobody wrote.

## 2. The schema, from `wreath migrations`

```bash
psql "$DSN" -c 'CREATE SCHEMA IF NOT EXISTS shop'
export WREATH_MIGRATION_DSN="$DSN"
wreath migrations generate shop.app:app migrations/migration.bin
wreath migrations apply shop.app:app migrations/migration.bin
```

There is no `create_all`. The application *validates* its schema at startup and
refuses a database that does not match, and `wreath migrations` is the only
thing that changes one. Creating the schema is a separate statement because an
artifact describes tables and not which schema they land in, and `apply` reads
`WREATH_MIGRATION_DSN` rather than reusing your request credentials.

`wreath migrations detect` answers "has my model drifted from the database?"
without changing anything, which is the form to put in CI. See
[the migrations guide](migrations.md).

## 3. The routes, from `crud_router`

```python
application.enable_crud()
application.crud(
    Item,
    open_session,
    prefix="/items",
    fields=("id", "name", "price"),
    readonly=("id",),
    authorize=Access.roles("staff"),
)
```

Five operations — list, retrieve, create, update, delete — with paging, sorting
against an allow-list, and validation compiled from the model.

**It is off by default and opted into twice**, at the application
(`enable_crud()`) and per model (`crud(...)`). A framework that generated CRUD by
default would put `DELETE /observer/{id}` on the network the first time somebody
declared a model. `fields` is an allow-list rather than an `exclude=` list, for
the same reason a deny-list rots: a column added next month is published by an
`exclude` written before it existed.

Take `authorize=` seriously here, and reach for `object_authorizer=` for anything
that needs the row itself — "this tenant's rows, not that one's" cannot be
decided before the row is loaded. See [Generating CRUD](crud.md).

## 4. The console, from `wreath.admin`

```python
admin = Admin(open_session, authorize=Access.roles("support"))
admin.register(Item, list_columns=("id", "name", "price"),
               operations=("list", "retrieve"))
app.include_router(admin.router("/admin"))
```

Built entirely out of the primitives above — the same `crud.Access`, the same
withheld-field rules, the same pagination. Read-only is the version to start
from: `operations=("list", "retrieve")` generates no write route at all, which
is a stronger statement than a policy that denies writes, and it needs no CSRF
verifier because it generates no forms. A writable admin **requires** `csrf=`,
because `CSRFMiddleware` is header-only and an HTML form cannot carry a header.
See [the admin](admin.md) and [the recipe](../cookbook/recipes/read-only-admin.md).

## 5. The contract, from your return annotations

This is the step that decides how much the last two are worth, and it is the one
most easily skipped:

```python
@dataclass(frozen=True, slots=True)
class ItemPage:
    items: list[Item]


@items.get("/", operation_id="listItems")
async def list_items(request: Request) -> ItemPage:
    return ItemPage(items=[])
```

The return annotation *is* the response contract. wreath validates against it,
the OpenAPI document describes it, and `wreath typegen` renders it as a
TypeScript interface. A handler annotated `-> dict` generates a client typed
`Record<string, unknown>` — which compiles, and tells the front end nothing.

`operation_id` names the generated method and its React hook. Without one the
name is derived from the method and path (`getItems`), which is fine until two
routes derive the same one — and naming it means renaming a *path* does not
rename every call site in your front end.

## 6. The client, from `wreath typegen`

```bash
wreath typegen shop.app:app --output web/src/api --react-query
wreath typegen shop.app:app --output web/src/api --react-query --check   # in CI
```

Models, a fetch client, TanStack Query hooks, and a `SPEC_DIGEST` pinning the
document. Generate it into a gitignored directory: it is a build product of the
route table, and a committed copy is a client that lies about the API the moment
somebody edits a handler. The `--check` form is what makes a route change that
nobody regenerated fail the build rather than fail in a browser.

`--target python` generates a typed `ServiceClient` subclass instead, for
another wreath service calling this one. See
[OpenAPI and typed clients](openapi-typegen.md) and
[calling another service](service-client.md).

## 7. The UI's permissions, from the routes' own declarations

```python
app.include_router(permissions_router(app))
```

Answers "what may this caller do?" from the *same* Cedar authorizer that
enforces it, with the action vocabulary read off the routes' `@authorize`
declarations. There is no second list to keep in step, and `wreath typegen`
emits typed `ItemPermissions`/`usePermissions` from that vocabulary — so hiding
a button and refusing the request cannot disagree.

It is not enforcement. It is what lets the UI stop offering an action that would
be refused. See [Permissions in the UI](permissions.md).

## What this does not derive

Being explicit about the edges, because the list above reads as though the whole
application falls out of a table:

- **The domain.** CRUD is the right shape when the work genuinely is "a person
  edits rows in a table". For an observation that arrives through ingest, a
  generated `DELETE /sighting/{id}` is a route no domain rule wants to exist.
- **Authorization.** `authorize=` and `object_authorizer=` are decisions, and
  nothing infers them. `wreath mutant` will tell you whether your tests would
  notice if one went missing — see [Would your tests notice?](mutant.md).
- **The write path a front end actually needs.** Five generated routes are a
  starting point, not an API design.

## Checking it end to end

```bash
wreath new shop --database postgres --frontend react   # all of the above, wired
wreath doctor preflight shop.app:app --environ          # what would stop it
wreath capabilities <package>                           # what you needn't install
```

[`wreath new`](../getting-started/new-project.md) writes this shape out with the
wiring already correct; [preflight](preflight.md) reports what would stop the
application, and names what it could not check.
