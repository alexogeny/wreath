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
