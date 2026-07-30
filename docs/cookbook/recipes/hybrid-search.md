# Combine keyword and semantic search

Keyword search finds the rows that share your words. Semantic search finds the
rows that share your meaning. Ship one and you will keep meeting the queries it
is bad at — an exact product code an embedding blurs away, or a paraphrase no
keyword matches. Run both and merge them by rank.

Everything here is one table and one declaration. You need
[pgvector](../../guides/vector-search.md) for the embedding half; the text half
needs nothing.

## The model

One row, two searchable representations of it:

```python
from typing import Mapped

from wreath.orm import Model, column
from wreath.orm.types import Int64, Text, TsVector, Vector


class Document(Model, table="documents"):
    id:        Mapped[int]         = column(Int64, primary_key=True)
    title:     Mapped[str]         = column(Text)
    body:      Mapped[str]         = column(Text)
    embedding: Mapped[list[float]] = column(
        Vector(1536), index="hnsw", index_ops="vector_cosine_ops"
    )
    search:    Mapped[bytes]       = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )
```

`search` is a generated column — PostgreSQL recomputes it inside the statement
that changed `title` or `body`, so the GIN index is never stale. `embedding` is
yours to write:

```python
document = Document(title=title, body=body, embedding=await embed(f"{title}\n{body}"))
await session.add(document)
```

`embed` is your model provider. Wreath stores and searches vectors; producing
them is an application choice, not a framework dependency.

## The searches, and the fusion

```python
from wreath.queries import Param, Queries, fuse, query


class Documents(Queries[Document]):
    nearest = (
        query()
        .order_by(Document.embedding.cosine_distance(Param("q")))
        .limit(50)
    )
    matching = (
        query(Document.search.matches(Param("terms")))
        .order_by(Document.search.rank(Param("terms")).desc())
        .limit(50)
    )
    hybrid = fuse(nearest, matching).limit(10)
```

Three declarations, all compiled once at class-definition time. `nearest` and
`matching` are ordinary named reads you can still call on their own — useful when
a bug report says "semantic search returned nonsense" and you want to see one
half in isolation.

`hybrid` scores each row by where it *placed* in each search —
`Σ 1 / (60 + rank)` — rather than by what either search scored it. That matters:
a cosine distance and a `ts_rank` are different units, and any weighting between
them is fitted to today's embedding model. A rank has no units. See
[Hybrid search](../../guides/hybrid-search.md) for why, and for what `k` does.

## The endpoint

```python
@app.get("/search")
async def search(session: Session, q: str) -> list[dict]:
    found = await Documents(session).hybrid(q=await embed(q), terms=q)
    return [{"id": item.id, "title": item.title} for item in found]
```

The user's text goes to both halves: embedded for the vector search, raw for the
text search. `.matches()` parses it with `websearch_to_tsquery`, which treats
`"`, `&`, `!` and a lone `:` as characters rather than syntax, so a search box
cannot produce a 500.

`hybrid` returns hydrated `Document` objects, best first. A row both searches
found appears once.

## Sizing the shortlists

The `.limit(50)` on each half is the shortlist depth, and it is load-bearing
twice over.

It is what makes the vector half fast: `ORDER BY embedding <=> $1 LIMIT 50` is
the only shape an HNSW index answers, and without the `LIMIT` you have a
sequential scan with a distance computed per row. Wreath refuses an unbounded
search in a fusion for that reason, at the line that declared it.

And it is what fusion has to work with. Set it well above the number of rows you
show — 50 per search for 10 results is a good default. A depth of 10 would throw
away exactly the rows that fusion exists to rescue: the ones each search ranked
20th and neither ranked first.

## What it costs

Each search is its own statement, so a fusion is two round trips today. That was
the trade: the single-query alternative available right now computes its ranks
with window functions over every surviving row, which is one round trip and a
sequential scan. Two bounded index-assisted searches win at any table size worth
the feature. The declaration above does not change when that becomes one
statement.

## Confirm the halves

Before blaming the fusion, check that each search is doing its job:

```sql
EXPLAIN SELECT id FROM documents ORDER BY embedding <=> '[...]' LIMIT 50;
EXPLAIN SELECT id FROM documents WHERE search @@ websearch_to_tsquery('english', 'llamas');
```

An `Index Scan` with an `Order By:` line is the HNSW index answering; a `Seq
Scan` or a `Sort` above the scan means it is not, and the usual cause is an
operator class that does not match the distance. A `Bitmap Index Scan` on the GIN
index is the text half working.

More in [Hybrid search](../../guides/hybrid-search.md),
[Vector search](../../guides/vector-search.md), and
[Search text with PostgreSQL](search-documents.md).
