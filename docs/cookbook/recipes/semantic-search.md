# Search by meaning with embeddings

Keyword search finds the rows that share your words. Semantic search finds the
rows that share your *meaning* — "how do I cancel" matching a paragraph about
terminating a subscription. Store an embedding beside the row and order by
distance; PostgreSQL does the rest, once
[pgvector](https://github.com/pgvector/pgvector) is installed.

Wreath stores and searches vectors. Producing them is your application's
choice — an embedding model is a provider, not a framework dependency.

## The prerequisite

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Wreath does not emit this, because the privilege usually is not the migration
runner's. A registry with a `Vector` column pointed at a database without the
extension fails at **startup**, naming the extension, the schema, and the column
that wanted it — rather than at the first search with an unrecognised OID.

## The column

```python
from typing import Mapped

from wreath.orm import Model, column
from wreath.orm.types import Int64, Text, Vector


class Document(Model, table="documents"):
    id:        Mapped[int]         = column(Int64, primary_key=True)
    body:      Mapped[str]         = column(Text)
    embedding: Mapped[list[float]] = column(
        Vector(1536),
        index="hnsw",
        index_ops="vector_cosine_ops",
        index_with={"m": 16, "ef_construction": 64},
    )
```

The value is a `list[float]` of exactly `dim` finite floats. A wrong length or a
NaN is refused on assignment, at the line that wrote it, rather than at flush
time — pgvector stores neither, and every distance involving a NaN is
meaningless.

**The operator class decides which distance the index can answer.**
`vector_cosine_ops` serves `.cosine_distance()`; an index built with
`vector_l2_ops` will not be used for a cosine search, and the symptom is a silent
`Seq Scan` rather than an error. Match them.

## Writing a row

```python
document = Document(body=text, embedding=await embed(text))
await session.add(document)
```

`embed` is yours — an API call to a model provider, or a local
sentence-transformer. `1536` is OpenAI's `text-embedding-3-small`; `768` is a
common sentence-transformer size. Pick the one your model emits, because changing
it later rewrites the table.

## The endpoint

```python
from wreath.queries import Param, Queries, query


class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(10)
    )


@app.get("/search")
async def search(session: Session, q: str) -> list[dict]:
    found = await Documents(session).nearest(q=await embed(q))
    return [{"id": item.id, "body": item.body} for item in found]
```

**A search is an ordering, not a filter.** That is the shape the feature has, and
the only shape an approximate index can answer: `ORDER BY ... LIMIT` uses the
index, and a distance threshold (`where(... < 0.3)`) does not — it is a
sequential scan and a full distance computation per row. Reach for a threshold
when you want "everything close enough" over a small set, not "the ten nearest".

Ordinary filters compose, and the index still answers the ordering:

```python
class Documents(Queries[Document]):
    nearest_for_tenant = (
        query(Document.tenant_id == Param("tenant"))
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(10)
    )
```

The vector varies per call; the SQL does not. The plan-cache key comes from the
column name and the operator, never from the extension's OID — which matters
because that OID differs between databases, and a key containing one would
quietly give the same query two cache entries.

## Confirm the index is used

```sql
EXPLAIN SELECT id FROM documents ORDER BY embedding <=> '[...]' LIMIT 10;
```

An `Index Scan` with an `Order By:` line is the index answering. A `Seq Scan`, or
a `Sort` above the scan, means it is not — and the usual cause is a mismatched
operator class.

Budget for the build: **HNSW is slow and holds a lock**. A few hundred thousand
1536-dimension rows is minutes, not seconds. Wreath emits the `CREATE INDEX` as
an ordinary statement so the cost is visible in the generated migration before
you apply it; if it is more downtime than you have, build it by hand with
`CONCURRENTLY`, which cannot run inside the runner's transaction.

The four distances, re-dimensioning as a rewrite, `ivfflat` tuning, and what is
deliberately absent are in [Vector search](../../guides/vector-search.md). For
keyword search over the same table, see
[Search text with PostgreSQL](search-documents.md).
