# Vector search

A retrieval-augmented application needs two things from its database: somewhere
to put embeddings, and a way to find the nearest ones. PostgreSQL has both, once
[pgvector](https://github.com/pgvector/pgvector) is installed — and Wreath owns
the whole column between your model and the wire, so there is no second service
to operate and no dialect to negotiate.

Wreath stores and searches vectors. It does not *produce* them: turning text into
an embedding means a model provider, and that is your application's choice rather
than a runtime dependency of the framework.

## Before anything else: the extension

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

This is not optional and Wreath will not do it for you. A registry with a
`Vector` column, pointed at a database without the extension, fails at **startup**
with a message naming the extension, the schema it looked in, and the column that
wanted it. That is deliberate: the alternative is an unrecognised-OID error on the
first search, at 3am, with none of that in it.

Some managed PostgreSQL tiers restrict who may run `CREATE EXTENSION`. If yours
is one, it has to be enabled by the provider — there is no fallback path, because
a fallback would be a slower answer that hides the problem.

You can ask before deploying:

```python
from wreath.doctor import check_extension_types

for line in await check_extension_types(registry):
    print(line)
```

## Declaring a column

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text, Vector

class Document(Model, table="documents"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list[float]] = column(
        Vector(1536),
        index="hnsw",
        index_ops="vector_cosine_ops",
        index_with={"m": 16, "ef_construction": 64},
    )
```

The Python value is a `list[float]` of exactly `dim` finite floats. Coercion is
strict in both directions that matter: a wrong length is a dimension mismatch the
database would reject anyway, and NaN or infinity is refused because pgvector
stores neither and every distance involving one is meaningless. Both fail on
assignment, at the line that wrote the value, rather than at flush time.

`1536` is OpenAI's `text-embedding-3-small`; `768` is a common sentence-transformer
size. Pick the one your model emits — changing it later is a table rewrite, and
Wreath will tell you so (see [Migrations](#migrations)).

**The generated layers treat this column as infrastructure.**
[Generated CRUD](crud.md) neither serializes it nor accepts it — a page of twenty
`Vector(1536)` rows would be thirty thousand floats, and a client that may write
one can put a row at the top of every search without changing a visible word.
[Pagination](pagination.md)'s default sort allow-list leaves it out too, because
`ORDER BY embedding` is valid SQL that runs a full sort on kilobyte values with no
index to serve it. Both have an explicit opt-in — `expose=("embedding",)` and
`allow=("embedding",)` — for the applications that mean it.

## Searching

A search is an **ordering**, not a filter. That is the shape the feature actually
has, and it is also the only shape an approximate index can answer:

```python
nearest = await session.fetch(
    Document.select()
    .order_by(Document.embedding.cosine_distance(query_vector))
    .limit(10)
)
```

A `Vector` column carries four distances, named for what they compute rather
than for their symbols — `<#>` is not a thing anyone reads twice. Each renders
`column <operator> $n` and each is orderable:
[`cosine_distance`](../reference/orm.md#cosine_distance), the common default for
text embeddings; [`l2_distance`](../reference/orm.md#l2_distance), Euclidean;
[`l1_distance`](../reference/orm.md#l1_distance), taxicab; and
[`inner_product`](../reference/orm.md#inner_product). The reference carries the
SQL operator each one emits, generated from the method itself, so this guide
does not keep a second copy of that table for you to find out of date.

`inner_product` is *negative*, as pgvector defines it, so that ordering ascending
still puts the most similar row first — read a result of `-0.9` as an inner
product of `0.9`.

A distance evaluates to a *number*, so it is not a predicate on its own.
`where()` says so rather than letting PostgreSQL refuse it later with a message
about the argument of `WHERE`. Compare it against a threshold when you want one:

```python
Document.select().where(Document.embedding.cosine_distance(query) < 0.3)
```

**A threshold does not use the index.** Only `ORDER BY ... LIMIT` does. A
threshold filter over a large table is a sequential scan and a full distance
computation per row; it is the right tool for "everything close enough" over a
small set, and the wrong one for "the ten nearest".

Ordinary filters compose as usual, and the index still answers the ordering:

```python
(
    Document.select()
    .where(Document.tenant_id == tenant)
    .order_by(Document.embedding.cosine_distance(query))
    .limit(10)
)
```

## Naming the search

The [declared query](orm.md) machinery takes a vector parameter, so a search can
have a name and one compiled shape:

```python
from wreath.queries import Param, Queries, query

class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(10)
    )

found = await Documents(session).nearest(q=query_vector)
```

A named search is also what [hybrid search](hybrid-search.md) fuses: `fuse` takes
two declared searches — this one and a [full-text](full-text-search.md) one — and
merges their results by rank, which is the only scale a cosine distance and a
`ts_rank` share.

The vector varies per call; the SQL does not. The plan-cache key is derived from
the column *name* and the distance operator, never from the extension's OID —
which matters more than it sounds, because that OID differs between databases,
and a key containing one would silently give the same query two cache entries.

## Indexes

An approximate index is what turns a similarity search from O(rows) into
something sublinear, and **the operator class is what decides which distance it
can answer**. An `hnsw` index built with `vector_l2_ops` will not be used for a
cosine search. Match them.

```python
column(Vector(1536), index="hnsw", index_ops="vector_cosine_ops")
column(Vector(1536), index="ivfflat", index_ops="vector_l2_ops",
       index_with={"lists": 100})
```

`hnsw` gives better recall and is slower to build; `ivfflat` builds faster and
wants `lists` tuned to your row count (pgvector suggests `rows / 1000` up to a
million rows). `index_with` passes method options straight through:
`WITH (m = 16, ef_construction = 64)`.

Both the operator class and the option values reach the DDL as text rather than
as bound parameters, so they are checked at declaration and the check is narrow
on purpose: an operator class or option name must be an unquoted identifier, and
an option value must be a number or an identifier such as `on`. Anything else —
including a value that would open a comment — is a `DeclarationError` where you
wrote it, not a syntax error from PostgreSQL at apply time.

**HNSW builds are slow, and the build holds a lock on the table.** A few hundred
thousand 1536-dimension rows is minutes, not seconds, and the index is bigger than
you expect. Wreath emits the `CREATE INDEX` as an ordinary statement so you can
see the cost in the generated migration before applying it. If that cost is more
downtime than you have, run the index build by hand with `CONCURRENTLY` — Wreath
does not emit that itself, because `CREATE INDEX CONCURRENTLY` cannot run inside
a transaction and the migration runner is a transaction.

Verify the planner is actually using it, rather than assuming:

```sql
EXPLAIN SELECT id FROM documents ORDER BY embedding <=> '[...]' LIMIT 10;
```

An `Index Scan` with an `Order By:` line is the index answering. A `Seq Scan` or a
`Sort` node above the scan means it is not, and the usual cause is an operator
class that does not match the distance.

## Migrations

`detect`/`generate`/`apply`/`down` cover vector columns and their indexes:

* A vector column added or dropped renders as an ordinary `ALTER TABLE`.
* **Re-dimensioning is a rewrite, and is emitted as one.** `vector(1536)` to
  `vector(3)` keeps pgvector's OID, so nothing about the type identity changes;
  Wreath compares the type's *spelling* as well, and emits
  `ALTER COLUMN ... TYPE vector(3)` rather than silently doing nothing. That
  statement rewrites the table. It is visible in the generated migration for
  exactly that reason.
* HNSW and IVFFlat indexes round-trip with their operator class and method
  options, so a matching index is not rediscovered as drift on every run.
* `down` drops the index and the column.

**A declared default operator class is understood.** `vector_l2_ops` is
`ivfflat`'s default — the only default pgvector defines, which is why `hnsw`
never showed this — and PostgreSQL does not record that a default was *named*:
the catalog reports it as absent. Wreath reads this database's defaults per
access method at migration time and normalises the declaration through them, so
`index_ops="vector_l2_ops"` on an `ivfflat` index compares equal to the catalog
and the emitted `CREATE INDEX` simply omits the clause — the same index either
way. Nothing is asked of you: declare the operator class you mean.

Two things are still manual. `CREATE EXTENSION` is not emitted — the privilege
usually is not the migration runner's to use, and a migration that failed on it
would be worse than a documented prerequisite. And *changing* an existing index's
operator class is emitted as `MANUAL`, because there is no `ALTER INDEX` that
rebuilds one; drop and recreate it deliberately.

## Running the tests

The vector suites need a PostgreSQL with the extension, so the container line
from `AGENTS.md` uses the pgvector image:

```bash
docker run -d --name wreath-test-pg -e POSTGRES_PASSWORD=wreath \
  -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test -p 55432:5432 \
  pgvector/pgvector:pg17 -c max_connections=200 -c fsync=off -c synchronous_commit=off
export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
```

## What is not here

No embedding generation, no chunking strategy, no re-embedding on write. Those
are your pipeline, not framework surface. No client for a separate vector
database either — the pitch is that you do not need one.

`halfvec` and `sparsevec` are not implemented. The mechanism that resolves
`vector`'s OID is general, so they are additive when someone asks for them.

Reference: [`Vector`](../reference/orm.md#vector) and the distance methods on
[`ColumnExpr`](../reference/orm.md#columnexpr), in
[`wreath.orm`](../reference/orm.md). To put one of these searches in front of a
model rather than a person, see
[MCP: a retrieval tool, end to end](mcp.md#a-retrieval-tool-end-to-end).
