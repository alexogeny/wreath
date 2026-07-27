# ORM

`wreath.orm` is where your data takes shape: models, fields, relationships, query
construction, and the mapping between rows and objects. It sits on top of the
native [PostgreSQL driver](postgres.md) and never reaches around it.

A model earns its keep twice. It describes a table, and its columns *also* serve
as the validator for incoming data — so a request body bound to a model is
checked once, by the model itself, against the same rules the database will
enforce. Definition and validation stay in one place and cannot drift apart.

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text

class Widget(Model, table="widgets"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    price: Mapped[int] = column(Int64)
```

Every column names its PostgreSQL type explicitly — nothing is inferred from
the annotation — and columns are `NOT NULL` unless you pass `nullable=True`.
Wire the models onto the application with `app.postgres("main", dsn=...)`
followed by `app.orm(database="main", models=[Widget])`, and ask for a
request-scoped session in a handler with
`Annotated[Session, FromORM("main", workload="read")]`. If you're arriving
from SQLAlchemy or SQLModel, [the translation page](../from-fastapi/sqlmodel.md)
maps the whole surface.

## User story: one model validates the body and writes the row

> *As an API author, I want `POST /widgets` to check the request body against the
> very model that defines the table — no second schema to keep in sync — and then
> persist it on a write session.*

```python
from typing import Annotated
from wreath.orm import FromORM, Session

@app.post("/widgets")
async def create(
    request,
    widget: Widget,                                          # the body, validated by the model
    session: Annotated[Session, FromORM("main", workload="write")],
):
    session.add(widget)
    await session.flush()                                    # INSERT runs; widget.id is populated
    return {"id": widget.id, "name": widget.name}
```

Binding the body to `Widget` runs it through the same column rules the database
will enforce, so the check and the schema can't drift apart. `flush()` outside an
explicit transaction opens one for the write and commits it atomically.

A write built from a loaded object raises `StaleDataError` when it matches no
row — the object was deleted, or the key it was found by changed, in another
session. The statement "succeeded" in the driver's terms, so this used to pass
unnoticed and only showed up as the next read disagreeing.


### Bounding a query

`Registry(..., statement_timeout=5.0)` — or `Session(..., statement_timeout=…)`
for one unit of work — issues `SET LOCAL statement_timeout` on the outermost
transaction. Transaction-local on purpose: a session-level `SET` would travel
with the pooled connection into somebody else's work. Without it, one
pathological query holds a connection for as long as PostgreSQL allows, which in
a default install is forever.


## User story: fetch a row, or a filtered page

> *As an API author, I want to read one row by id and run a small filtered query
> on a request-scoped session that only leases a read connection when I actually
> touch it.*

```python
@app.get("/widgets/{id}")
async def read(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
):
    widget = await session.get(Widget, request.path_params["id"])   # None on a miss
    if widget is None:
        return Response(status_code=404)
    cheap = await session.fetch(
        Widget.select().where(Widget.price < 1000).order_by(Widget.price).limit(20)
    )
    return {"widget": widget.name, "cheap": [w.name for w in cheap]}
```

`session.get` returns `None` for a missing primary key rather than raising, and a
session that is never queried leases no connection at all.

## User story: name the reads instead of rebuilding them

> *As an application author, I have four handlers that all fetch llamas — by
> paddock, by trek, the overdue ones, the ones a keeper still has to check — and
> the filters are copied into each of them. I want the query that fetches a
> paddock's herd to have a name, one place to change, and no per-request cost for
> the privilege.*

That module of small query-building functions is the data-access layer every
application grows. `wreath.queries` is that layer, declared rather than written:

```python
from wreath.orm.types import Timestamp
from wreath.queries import Param, Queries, query

class Llama(Model, table="llamas"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    paddock_id: Mapped[int] = column(Int64)
    checked_at: Mapped[object] = column(Timestamp)

class Llamas(Queries[Llama]):
    by_paddock = query(Llama.paddock_id == Param("paddock")).order_by(Llama.name)
    overdue = query(Llama.checked_at < Param("before")).order_by(Llama.checked_at)
    by_name = query(Llama.name == Param("name")).one()

@app.get("/paddocks/{id}/herd")
async def herd(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
):
    llamas = Llamas(session)
    herd = await llamas.by_paddock(paddock=request.path_params["id"])
    stale = await llamas.overdue.count(before=cutoff)
    return {"herd": [llama.name for llama in herd], "overdue": stale}
```

A `Param` is the piece to understand. The declaration is written once with
placeholders where the values go, so the *shape* of the query is fixed when the
class is defined and only the values vary per call — which is exactly what the
registry's compiled-plan cache is keyed on. `by_paddock` therefore compiles one
statement and reuses it for every paddock in the herd, and the plan-cache key it
produces is byte-identical to the one the hand-written query would have made.
There is no second cache involved.

Declaring also moves mistakes earlier. A column belonging to another model, or a
parameter written somewhere no caller could ever supply it — an `order_by`, a
`limit` — fails when the class is defined, at import, rather than on the first
request that runs it. Per call, a missing or unexpected parameter is named in the
error, and a value of the wrong type is rejected by the column's own rules before
any SQL is sent.

Parameters work with the six comparison operators, in either order. Operators
spelled as methods — `like`, `in_`, the jsonb and array operators — bind their
operand as they are written, so those take literals for now.

`Queries` reads and never writes: writes belong to the session, whose strict
one-way direction is an ORM invariant worth keeping in one place. That is why
this is `Queries` and not a repository — the smaller surface is the correct one.
Calling a declaration returns hydrated objects (or one object, or `None`, after
`.one()`); `.count(...)` answers how many rows match without hydrating any of
them; and `Llamas.by_paddock.bind(paddock=7)` hands back an ordinary `Select`, so
anything that takes one — `session.fetch`, `wreath.pagination` — takes a declared
query too.

**Reference:** [`wreath.queries`](../reference/queries.md).

## Constraints and indexes

A single column carries its own constraints as keywords: `unique=True`,
`index=True`, and `references=Other.id` for a foreign key. Foreign keys take
referential actions and deferrability alongside the reference:

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64

class Account(Model, table="accounts"):
    id: Mapped[int] = column(Int64, primary_key=True)

class Order(Model, table="orders"):
    id: Mapped[int] = column(Int64, primary_key=True)
    account_id: Mapped[int] = column(
        Int64, references=Account.id, on_delete="cascade", on_update="restrict"
    )
    reviewer_id: Mapped[int] = column(
        Int64, references=Account.id, deferrable=True, nullable=True
    )
```

`on_delete`/`on_update` accept `"no action"`, `"restrict"`, `"cascade"`,
`"set null"`, or `"set default"`; `deferrable=True` makes the constraint
`DEFERRABLE INITIALLY DEFERRED`.

Anything that spans more than one column is declared in the body with `unique(...)`
or `index(...)`, named by column. They are found by type, like `rule` and
`narrow`, so the attribute name is only documentation:

```python
from wreath.orm import Mapped, Model, column, index, unique
from wreath.orm.types import Int64, Text

class Membership(Model, table="memberships"):
    org_id: Mapped[int] = column(Int64, primary_key=True)
    user_id: Mapped[int] = column(Int64)
    email: Mapped[str] = column(Text)
    _identity = unique("org_id", "user_id")     # composite UNIQUE
    _by_user = index("user_id", "email")        # multi-column btree index
    _one_email = index("email", unique=True)     # unique index
```

A composite primary key needs no separate declaration — mark each part
`primary_key=True`. The [migration engine](migrations.md) reads all of these,
names the underlying constraints and indexes deterministically, and can both
apply and downgrade them; expression, partial, covering, and non-btree indexes
are not modelled yet.

## Queries, relationships, and sessions

Query building, relationships, and sessions live alongside the model in the same
module. Business rules — the checks that go beyond a column's type — are written
once and can be emitted two ways: raised as an exception when you want to stop, or
collected into a validation response when you want to tell the caller everything
that's wrong at once. One source of truth, two honest presentations.

A `Registry` owns an access-ordered compiled-query cache. Its
`query_cache_size` and `query_cache_bytes` limits constrain both shape count and
approximate retained plan storage; insertion evicts the oldest plans until both
budgets hold.

The precise field, query, and session APIs are generated from the code, so reach
for the reference when you need exact signatures.

**Reference:** [`wreath.orm`](../reference/orm.md).
