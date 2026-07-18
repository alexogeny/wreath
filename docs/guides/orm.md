# ORM

`wreath.orm` is where your data takes shape: models, fields, relationships, query
construction, and the mapping between rows and objects. It sits on top of the
native [PostgreSQL driver](postgres.md) and never reaches around it.

A model earns its keep twice. It describes a table, and its columns *also* serve
as the validator for incoming data — so a request body bound to a model is
checked once, by the model itself, against the same rules the database will
enforce. Definition and validation stay in one place and cannot drift apart.

```python
from wreath.orm import Model, fields

class Widget(Model):
    id: int = fields.primary_key()
    name: str
    price: int
```

Query building, relationships, and sessions live alongside the model in the same
module. Business rules — the checks that go beyond a column's type — are written
once and can be emitted two ways: raised as an exception when you want to stop, or
collected into a validation response when you want to tell the caller everything
that's wrong at once. One source of truth, two honest presentations.

The precise field, query, and session APIs are generated from the code, so reach
for the reference when you need exact signatures.

**Reference:** [`wreath.orm`](../reference/orm.md).
