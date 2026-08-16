# Full-text search

PostgreSQL has had full-text search since long before anyone shipped a separate
service for it, and for most applications it is the search engine you already
operate. The wreath ORM makes it a column type and two methods: declare a
`TsVector` over the columns you want searchable, and query it with `.matches()`
and `.rank()`.

Nothing here needs an extension. Unlike `Vector`, `tsvector` is built into
every PostgreSQL, so a search column works on a managed tier that will not let
you run `CREATE EXTENSION`.

## User story: search a document by its words, not its substrings

> *As an API author, my `documents` table has a title and a body, and I want the
> search box to find "llama husbandry" whether the user typed
> `llamas husbandry`, `HUSBANDRY llama`, or `"llama" & husbandry` — and I want a
> hostile string to return no rows rather than a 500.*

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

```python
results = await session.fetch(
    Document.select()
    .where(Document.search.matches(terms))
    .order_by(Document.search.rank(terms).desc())
    .limit(20)
)
```

That is the whole feature. What follows is why each piece is shaped the way it
is, because two of them are load-bearing.

## The column is generated, and that is the point

`TsVector` does not declare a column you write to. It declares one PostgreSQL
computes, and the migration renders it as

```sql
search tsvector generated always as (
    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))
) stored
```

`GENERATED ALWAYS AS ... STORED` is the form that keeps the index correct
*without a trigger*. PostgreSQL recomputes the column inside the same statement
that changed `title` or `body`, so there is no window — not even a
microsecond-wide one inside a transaction — where the GIN index disagrees with
the row it points at. A trigger can be dropped, disabled, or written to fire on
the wrong events; a generated column cannot be out of date.

The consequence is that the value is the database's, not yours:

```python
document = Document(id=1, title="Llama husbandry", body="...")   # fine
document.search = b"..."                                          # TypeError
Document(id=1, title="...", body="...", search=b"...")            # TypeError
```

You write the sources; PostgreSQL derives the vector. After an `INSERT` the
computed value comes back in the statement's `RETURNING` list, so the in-memory
object is not left with a hole.

!!! note "Sources must be `text` columns"

    A `varchar` source is refused at startup. PostgreSQL's own deparsing of the
    expression casts a non-text column — `(COALESCE(vch, ''::character
    varying))::text` — and the exact cast depends on the declared width, so
    wreath cannot predict what the catalog will report back. Guessing wrong
    there is not a crash but something worse: `detect` would report the column
    as drifted on every migration run, forever, with nothing actually wrong. The
    same reasoning bounds partial-index predicates; see
    `wreath.orm._index_predicate`.

!!! note "A tsvector column reads back opaque"

    Selecting a model selects its `search` column too, and the value arrives as
    the driver's raw wire bytes rather than a parsed structure. That is
    deliberate for now: a tsvector is a search index, and what you do with one is
    `.matches()` and `.rank()`, in SQL. If you need to see the lexemes, ask the
    database: `SELECT search::text FROM documents`.

    For the same reason it does not appear in a [generated CRUD](crud.md)
    response — base64 text derived from columns already in the same payload — nor
    in [pagination](pagination.md)'s default sort allow-list. `expose=("search",)`
    puts it back in a response; nothing makes it writable, because nothing can.

## `websearch_to_tsquery` is the default because it does not raise

A search box produces hostile input constantly, and not because anyone is
attacking you — `llamas &` is what you get the moment somebody types an
ampersand and a space.

`.matches(terms)` compiles to

```sql
search @@ websearch_to_tsquery('english', $1)
```

`websearch_to_tsquery` is the parser that treats `"`, `&`, `!` and a lone `:` as
characters rather than syntax. It understands the small grammar people already
know from web search engines — quoted phrases, `or`, a leading `-` for
exclusion — and it never raises.

`to_tsquery` is the other one, and it is available when you want it:

```python
Document.search.matches("llamas & !alpacas", parser="to_tsquery")
```

That parser understands `&`, `|`, `!` and `<->`, and it raises
`no operand in tsquery` on malformed input. Choose it where the query text is
the application's own and the error is handled — not where it came from a user.

The configuration (`english`, `simple`, …) is *not* a call argument. It comes
from the column's own declaration, because a query analysed under a different
configuration than the stored vector matches nothing at all — and "no results"
reads as missing data rather than as a mistake.

## Filtering and ranking are two calls, on purpose

`.matches()` is a predicate and can use the GIN index. `.rank()` is a number
PostgreSQL computes per surviving row, and no index can answer it. Writing a
search as one call would hide which half costs what:

```python
found = (
    Document.select()
    .where(Document.search.matches(terms))          # indexed, cheap
    .order_by(Document.search.rank(terms).desc())   # per matching row
    .limit(20)
)
```

Filter first, rank what survived. Pass the *same* terms to both — ranking one
query while filtering by another is a bug that presents as bad relevance rather
than as an error.

A rank is a number, so it is also comparable:

```python
Document.select().where(Document.search.rank(terms) > 0.1)
```

and it is *not* a predicate on its own. `where(Document.search.rank(terms))`
raises at the line that wrote it, rather than letting PostgreSQL refuse it later
with a message about the argument of `WHERE`.

Note the direction: a relevance score is *higher* for a better match, so ranking
wants `.desc()`. A vector distance is the opposite — smaller is nearer — which
is why `.asc()` is the one that goes with `cosine_distance`.

## Declared queries

A search compiles once at class-definition time and substitutes the terms per
call, like any other declared query:

```python
from wreath.queries import Param, Queries, query


class Documents(Queries[Document]):
    matching = query(Document.search.matches(Param("terms"))).order_by(
        Document.search.rank(Param("terms")).desc()
    )
```

The same parameter binds into both positions from one argument, so
`Documents(session).matching(terms="llama husbandry")` fills the filter and the
ranking together. The search text never reaches the SQL text or the plan-cache
key — two searches over the same column share one prepared statement.

A declared search is also what [hybrid search](hybrid-search.md) fuses: `fuse`
merges this one with a [vector](vector-search.md) search by rank, which is the
only scale a `ts_rank` and a cosine distance share.

## Migrations

`detect`, `generate`, `apply`, and `down` all cover a generated column and its
GIN index, and the ordering is handled: the column is created *after* the
columns its expression reads and dropped *before* them, because PostgreSQL
refuses either the other way round.

One case is deliberately not automatic. **Changing a `TsVector`'s configuration
or its sources is emitted as `MANUAL`.** The change is detected — the expression
is part of the column's signature and of the model fingerprint, so it can never
be a silent no-op — but rewriting a stored generated column means recomputing it
for every row, and that is a cost an operator should see and schedule rather
than discover during a deploy. Drop and re-add the column in a migration you
wrote.

Reference: [`TsVector`](../reference/orm.md#tsvector), and
[`matches`](../reference/orm.md#matches) and
[`rank`](../reference/orm.md#rank) on
[`ColumnExpr`](../reference/orm.md#columnexpr) — both carry the parser argument
and the exact SQL they emit, in [`wreath.orm`](../reference/orm.md). To hand one
of these searches to a model rather than to a person, see
[MCP: a retrieval tool, end to end](mcp.md#a-retrieval-tool-end-to-end).
