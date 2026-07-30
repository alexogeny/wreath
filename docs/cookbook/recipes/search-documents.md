# Search text with PostgreSQL

A search box is one of the first things anyone asks an API for, and the usual
answer — stand up a search service, then keep it in step with the database
forever — is a lot of machinery for a feature PostgreSQL already has. Declare a
`TsVector` column and query it.

## The column

```python
from typing import Mapped

from wreath.orm import Model, column
from wreath.orm.types import Int64, Text, TsVector


class Document(Model, table="documents"):
    id:     Mapped[int]   = column(Int64, primary_key=True)
    title:  Mapped[str]   = column(Text)
    body:   Mapped[str]   = column(Text)
    search: Mapped[bytes] = column(
        TsVector("english", sources=("title", "body")), index="gin"
    )
```

`search` is not a column you write. PostgreSQL derives it from `title` and
`body` on every insert and update — `generate` renders it as
`GENERATED ALWAYS AS (to_tsvector(...)) STORED` — so it can never fall behind
the row, and the GIN index over it can never point at stale words. Assigning it
raises; you write the sources.

## The endpoint

```python
from wreath.queries import Param, Queries, query


class Documents(Queries[Document]):
    matching = (
        query(Document.search.matches(Param("terms")))
        .order_by(Document.search.rank(Param("terms")).desc())
        .limit(20)
    )


@app.get("/documents")
async def search(session: Session, q: str) -> list[dict]:
    found = await Documents(session).matching(terms=q)
    return [{"id": item.id, "title": item.title} for item in found]
```

`q` goes straight from the query string into the search with no sanitising,
because there is nothing to sanitise: it is a bound parameter, and
`websearch_to_tsquery` — the default parser — treats `"`, `&`, `!` and a lone
`:` as characters rather than syntax. A user typing `llamas &` gets results, not
a 500. The one parser that *does* raise is opt-in
(`matches(q, parser="to_tsquery")`), for queries the application writes itself.

The filter and the ranking take the same `Param`, so one argument fills both.
`.matches()` uses the GIN index; `.rank()` scores only the rows that survived it.
Higher is better, so ranking wants `.desc()`.

## What you get for free

Migrations cover the column, its expression, and the index, in both directions —
and the column is created after the columns it reads and dropped before them,
which is the ordering PostgreSQL insists on. Changing the configuration or the
source list is *detected* rather than silently ignored, but emitted as `MANUAL`:
rewriting a stored generated column recomputes every row, and that is a cost to
schedule rather than to discover mid-deploy.

Full details, including why the configuration belongs to the column rather than
to the query, are in [Full-text search](../../guides/full-text-search.md).
