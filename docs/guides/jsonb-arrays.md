# JSONB and arrays

PostgreSQL's `jsonb` and array types are first-class in the wreath ORM — the containment operators and array predicates you'd otherwise drop to raw SQL for are methods on a column, compiled and parameterised like any other expression.

## User story: find rows by what's inside a JSONB column

> *As an API author, my `llama` rows carry a free-form `metadata` document and a
> `tags` array. I want to answer "which guides are tagged for trekking?" as one
> parameterised query — not a `LIKE` over serialized JSON.*

```python
guides = await session.fetch(
    Llama.select()
    .where(Llama.metadata.contains({"role": "guide"}))   # metadata @> $1
    .where(Llama.tags.overlaps(["trek", "boarding"]))    # tags && $2
)
```

Both predicates compile to real PostgreSQL operators against the `gin` index the
column declares, and every value binds as a parameter — the document and the
array each go over the wire as a single `$N`, so the plan cache key doesn't vary
with the array's length. The operator methods are documented below.

## Columns

```python
from typing import Mapped
from wreath.orm import column
from wreath.orm.types import Jsonb, Array, Text, Int64

class Llama(Model, table="llama"):
    metadata: Mapped[dict]      = column(Jsonb, index="gin")
    tags:     Mapped[list[str]] = column(Array(Text))
    scores:   Mapped[list[int]] = column(Array(Int64), nullable=True)
```

`index="gin"` declares the GIN index these operators want (and it round-trips through migrations, so a declared GIN index isn't diffed away as drift).

## JSONB operators

```python
Llama.metadata.contains({"role": "guide"})     # metadata @> $1
Llama.metadata.contained_by({...})             # metadata <@ $1
Llama.metadata.has_key("role")                 # metadata ? $1
Llama.metadata.has_any(["a", "b"])             # metadata ?| $1
Llama.metadata.has_all(["a", "b"])             # metadata ?& $1
Llama.metadata.path(["a", "b"]) == "x"         # (metadata #>> $1) = $2
```

## Array operators

```python
Llama.tags.contains(["trek"])                  # tags @> $1
Llama.tags.overlaps(["trek", "boarding"])      # tags && $1
Llama.tags.any_eq("trek")                      # $1 = ANY(tags)
Llama.tags.all_eq("trek")                      # $1 = ALL(tags)
```

The whole array binds as a single parameter, so the query plan cache key is independent of the array's length — unlike an `IN (…)` list, which encodes its arity. The `?`-family operators are unambiguous because wreath binds with numbered `$N` placeholders over the extended-query protocol, so the classic driver clash simply doesn't arise.

Values still flow through parameters, never string interpolation — the operator set is a fixed allow-list, so widening it never widens your injection surface.
