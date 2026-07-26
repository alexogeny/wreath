# Query JSONB and array columns

You have a `jsonb` document column and a text `array` column, and you want to ask
"which rows are tagged for trekking and marked as a guide?" as one parameterised
query — not a `LIKE` over serialized JSON. The containment operators and array
predicates are methods on the column, compiled and bound like any other
expression:

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Array, Jsonb, Text

class Llama(Model, table="llama"):
    metadata: Mapped[dict]      = column(Jsonb, index="gin")
    tags:     Mapped[list[str]] = column(Array(Text))

guides = await session.fetch(
    Llama.select()
    .where(Llama.metadata.contains({"role": "guide"}))   # metadata @> $1
    .where(Llama.tags.overlaps(["trek", "boarding"]))    # tags && $2
)
```

Both predicates compile to real PostgreSQL operators against the `gin` index the
column declares, and every value binds as a single `$N` parameter — so the plan
cache key doesn't vary with the array's length, and there's no string
interpolation to inject through. The full JSONB set is `contains`,
`contained_by`, `has_key`, `has_any`, `has_all`, and `path([...])`; for arrays,
`contains`, `overlaps`, `any_eq`, and `all_eq`. The operator set is a fixed
allow-list, so widening what you can express never widens your injection surface.
