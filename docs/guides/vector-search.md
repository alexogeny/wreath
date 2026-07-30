# Vector search

A retrieval-augmented application needs two things from its database: somewhere
to put embeddings, and a way to find the nearest ones. PostgreSQL has both, once
[pgvector](https://github.com/pgvector/pgvector) is installed — and Wreath owns
the whole column between your model and the wire, so there is no second service
to operate and no dialect to negotiate.

Wreath stores and searches vectors. It does not *produce* them: turning text into
an embedding means a model provider, and that is your application's choice rather
than a runtime dependency of the framework.

## What this replaces

The usual shape of a 2026 retrieval stack is an application database *plus* a
vector database: Postgres for your rows, and Pinecone or Qdrant or Chroma for your
embeddings. Two stores, two clients, two backup stories, two things to be down —
and a consistency problem you now own, because a document and its embedding are
written in two places with no transaction across them.

Wreath's answer is that you already have a database that can do this. For scale,
here is what the Python ecosystem actually installs, over the twelve months to
July 2026:

| You would otherwise run | Installs/year | With Wreath |
| --- | --- | --- |
| `weaviate-client` | 200.9M | — |
| `opensearch-py` | 57.4M | — |
| `elasticsearch` | 55.7M | — |
| `pgvector` (the Python client) | 30.5M | not needed — the codec is Wreath's own |
| `qdrant-client` | 24.6M | — |
| `chromadb` | 13.3M | — |
| `pinecone` | 8.0M | — |

Every row above is a service to run or a hosted bill to pay, and none of them can
join against your `users` table. `pgvector` is the most-installed *self-hosted*
answer of the lot, and it is a PostgreSQL extension rather than a second server.

Three things follow from keeping embeddings in the same database as everything
else, and they are the real argument rather than the operational tidiness:

* **One transaction.** A document and its embedding commit together or neither
  does. In a two-store setup that write is two writes, and the window between
  them is a source of documents that are invisible to search and embeddings that
  point at rows which no longer exist.
* **Filters are just `WHERE`.** "Nearest neighbours *belonging to this tenant,
  published, not archived, in these three categories*" is one query with a real
  planner behind it. Most vector databases model this as metadata filtering with
  its own dialect and its own performance cliff.
* **Joins.** The nearest documents *and their authors and their comment counts*
  is one round trip. Across two stores it is a search, then an `IN (...)` query,
  then a merge in Python — and the `IN` list is capped by whatever the first
  query returned, so pagination stops being expressible.

**And Wreath needs no `pgvector` Python package.** The wire codec is ours, in
`_pure/postgres.py` and `_native/postgres/codec.c`, alongside every other type.
What *is* required is the server extension — see below.

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

## Half precision: `Halfvec`

At 1536 dimensions a `vector` column costs 6,148 bytes a row, and the HNSW index
over it is usually the thing that stopped fitting in memory. `Halfvec` stores each
element as an IEEE-754 binary16 instead of a binary32, which halves both:

```python
from wreath.orm.types import Halfvec

class Document(Model, table="documents"):
    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list[float]] = column(
        Halfvec(1536), index="hnsw", index_ops="halfvec_cosine_ops"
    )
```

The Python value is the same `list[float]`. Note the operator class is the type's
own — `halfvec_cosine_ops`, not `vector_cosine_ops`; naming `vector`'s on a
`halfvec` column is an error pgvector reports at index creation, and
`tests/orm/test_halfvec_live.py` asserts both halves of that.

**Be deliberate about the precision.** binary16 carries roughly three decimal
digits, so a value written and read back is not the value you wrote: `0.1` returns
`0.0999755859375`. For embedding similarity this is almost always irrelevant —
what matters is the *ranking*, and rankings are robust to the third digit — but a
`Halfvec` is the wrong type for anything you intend to compare for equality or
accumulate.

Two refusals happen on assignment rather than at the server. A magnitude above
65504 (`MAX_HALF_MAGNITUDE`) would round to an infinity, which pgvector rejects;
and NaN and infinity are refused exactly as for `Vector`. Both name the element,
which an `INSERT` failure would not.

There is one wart worth knowing, because it is visible to an equality assertion.
The decimal you get back depends on the result format: our binary decoder widens
the stored binary16 exactly (`0.0999755859375`), while pgvector's *text* rendering
prints nine significant digits (`0.099975586`). Those are different Python floats,
differing around the tenth digit. Neither is wrong and the gap is far below
anything similarity depends on — it is written down here so it does not surprise
anyone, and pinned in
`tests/orm/test_halfvec_live.py::test_the_result_format_decides_which_decimal_comes_back`.

## Only the positions that matter: `Sparsevec`

`Vector` and `Halfvec` store every position. Some vectors are not shaped like
that. A bag-of-words over a 30,000-term vocabulary, or a SPLADE-style learned
sparse expansion, has a dimension in the tens of thousands and perhaps forty
values that are not zero. Storing 30,000 floats to say that is the problem
`Sparsevec` exists to avoid:

```python
from wreath.orm.types import Sparsevec
from wreath.postgres import SparseVector

class Document(Model, table="documents"):
    id: Mapped[int] = column(Int64, primary_key=True)
    terms: Mapped[SparseVector] = column(
        Sparsevec(30000), index="hnsw", index_ops="sparsevec_l2_ops"
    )
```

**The Python value is a `SparseVector`, not a list.** This is the one type here
that needs a value class of its own, and the reason is that no builtin says both
halves at once: a `dict` names no dimension, a `list` defeats the point, and a
`(dim, dict)` tuple is a shape every caller has to remember the order of.

```python
from wreath.postgres import SparseVector

terms = SparseVector(30000, {17: 0.9, 4021: 1.4})     # index -> weight
terms = SparseVector.from_dense(scores)               # drops the zeros for you

terms.dim        # 30000 -- the positions that exist
len(terms)       # 2     -- the positions that are stored (pgvector's nnz)
terms.to_dict()  # {17: 0.9, 4021: 1.4}
```

**Indices count from 1**, matching how pgvector writes them — `'{1:1.5,3:3.5}/5'`
is the first and third of five positions, and that is what `psql` shows you. The
binary wire format counts from zero, and the conversion happens in the codec so
the two numberings never both appear in your code.

Three things are refused on assignment rather than at the server: a
`SparseVector` whose dimension is not the column's, more than 16,000 non-zero
elements (`MAX_SPARSEVEC_NNZ`, pgvector's own ceiling — a value denser than that
wants `Vector`), and NaN or infinity, exactly as for `Vector`. An explicit zero
is dropped rather than stored, because the server drops it too and a value
should survive its own round trip.

The same four distances apply as for a dense vector, and the operator classes are
again the type's own: `sparsevec_l2_ops`, not `vector_l2_ops`. Note that pgvector
indexes a `sparsevec` to 1,000 non-zero elements even though the column may hold
16,000.

## One bit per dimension: `Bit`

Binary quantization replaces each element of an embedding with its sign. 1,536
float4s (6,148 bytes) become 1,536 bits (192 bytes) — 32x smaller, index
included — and enough of the geometry survives to *shortlist* candidates that a
second pass over the real vectors then re-scores:

```python
from wreath.orm.types import Bit

class Document(Model, table="documents"):
    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list[float]] = column(Vector(1536))
    signature: Mapped[str] = column(
        Bit(1536), index="hnsw", index_ops="bit_hamming_ops"
    )
```

The Python value is a `str` of `'0'` and `'1'`, exactly `length` of them — what
`psql` shows and what `'101'::bit(3)` means. `bytes` is accepted on the way *in*,
for the quantizers that produce it (`numpy.packbits(...).tobytes()`), and is
unpacked using the declared length; the unused bits of the final byte must be
zero, since they name positions the column does not have. A read always returns
the `str`, because `bytes` would have to carry its own bit count to be
unambiguous.

**`bit` is PostgreSQL's own type; only the operators are pgvector's.** So a `Bit`
column needs no extension to store and has no OID to resolve — but
`hamming_distance` (`<~>`), `jaccard_distance` (`<%>`) and the
`bit_hamming_ops` / `bit_jaccard_ops` index classes all come from
`CREATE EXTENSION vector`.

The two distances differ in what they count.
[`hamming_distance`](../reference/orm.md#hamming_distance) counts the positions
where two signatures disagree; it is the usual choice for a quantized dense
embedding, where every position carries a sign.
[`jaccard_distance`](../reference/orm.md#jaccard_distance) counts the bits set in
both against the bits set in either, so it measures set overlap — the right
choice when the bits are sparse *flags* (a feature is present or it is not) and
the shared zeros that dominate Hamming are not evidence of similarity.

The rerank is the point, and it is two ordinary queries:

```python
shortlist = await session.fetch(
    Document.select()
    .order_by(Document.signature.hamming_distance(query_signature))
    .limit(200)
)
best = sorted(shortlist, key=lambda row: cosine(row.embedding, query))[:10]
```

Keep the full-precision column. Binary quantization is a *filter* over a cheap
index, not a replacement for the vectors — the shortlist is approximate, and
without the second pass the ranking it produces is visibly worse than the one
`cosine_distance` gives you directly.

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

## Five things to do with a nearest-neighbour query

Retrieval for a language model is the reason most people install pgvector, and it
is the least interesting thing a distance operator does. All five of these are the
same `order_by` you have already seen — what changes is what you embedded.

### Near-duplicate detection, on the way in

The cheapest moment to notice that a support ticket is the same ticket as
yesterday's is before you create it. A distance *threshold* rather than a limit:

```python
@app.post("/tickets")
async def create(request, ticket: NewTicket, session: Session):
    embedding = await embed(ticket.body)
    duplicates = await session.fetch(
        Ticket.select()
        .where(Ticket.embedding.cosine_distance(embedding) < 0.08)
        .order_by(Ticket.embedding.cosine_distance(embedding))
        .limit(3)
    )
    if duplicates:
        return JSONResponse({"merged_into": duplicates[0].id}, status=409)
```

Exact-hash deduplication cannot see this: "printer won't connect to wifi" and "my
printer can't find the network" share no words at all. Tune the threshold against
your own corpus — 0.08 is a starting point, not a constant.

### "More like this", without a recommender system

An embedding of the thing itself makes recommendation a one-liner, and the
excluded self is the only subtlety:

```python
similar = await session.fetch(
    Article.select()
    .where(Article.id != article.id, Article.published == True)  # noqa: E712
    .order_by(Article.embedding.cosine_distance(article.embedding))
    .limit(6)
)
```

Note the `WHERE` doing real work beside the distance. That clause is why this
lives in your database: `published`, tenant scoping and "not the current article"
are ordinary predicates the planner can combine with the index, not metadata
filters in a second system's dialect.

### A semantic cache in front of an expensive model

If two questions mean the same thing, the second one can have the first one's
answer. This is the same near-duplicate query pointed at a cache table, and it
turns a 2-second, metered model call into a 2-millisecond index probe:

```python
class Answer(Model, table="answer_cache"):
    id: Mapped[int] = column(Int64, primary_key=True)
    question: Mapped[str] = column(Text)
    answer: Mapped[str] = column(Text)
    embedding: Mapped[list[float]] = column(
        Halfvec(1536), index="hnsw", index_ops="halfvec_cosine_ops"
    )
```

`Halfvec` earns its place here: a cache is allowed to be approximate, it is
allowed to be large, and halving the index is worth more than the third decimal
digit. Pair it with [`wreath.cache`](caching.md) for the exact-match layer in front
— identical questions should never reach a vector at all.

### Search over images, audio, or anything with an encoder

Nothing in this guide is about text. A multimodal encoder puts images and the
sentences describing them in one space, so "photos that look like this one" and
"photos of a red bicycle in the rain" are the same query against the same column:

```python
class Photo(Model, table="photos"):
    id: Mapped[int] = column(Int64, primary_key=True)
    key: Mapped[str] = column(Text)          # the object in wreath.objects
    embedding: Mapped[list[float]] = column(
        Vector(512), index="hnsw", index_ops="vector_cosine_ops"
    )
```

The row lives beside the object key, so a search returns a presigned URL in the
same round trip — see [presign an upload](../cookbook/recipes/presign-upload.md).

### Anomaly detection, by reading the distance instead of the order

Every query above throws the distance away and keeps the ordering. Keep the
distance and the same index answers a different question: *how unlike everything
else is this?* A login event, a transaction, a log line whose nearest neighbour is
far away is the interesting one. Select the distance as a column, alert above a
threshold, and you have novelty detection with no model to train and no second
service to run.

## What is not here

No embedding generation, no chunking strategy, no re-embedding on write. Those
are your pipeline, not framework surface. No client for a separate vector
database either — the pitch is that you do not need one.

**Wreath implements the client half of pgvector, not the server half.** The wire
codec is ours -- `wreath` needs no `pgvector` Python package, and the framing lives
in `_pure/postgres.py` and `_native/postgres/codec.c` like every other type. But
the `vector`, `halfvec` and `sparsevec` *types*, the
`<->`/`<=>`/`<#>`/`<+>`/`<~>`/`<%>` operators, and the HNSW
and IVFFlat access methods are PostgreSQL extension code running inside the
server, and `CREATE EXTENSION vector` is what creates them. No client driver can
substitute for that: an index access method cannot be defined from outside the
database. Storing embeddings as `real[]` and computing distances in SQL would
avoid the extension and lose ANN indexing entirely, which is the whole point. So
the extension is required, and `doctor` checks for it rather than letting a query
fail on an unresolved OID.

Reference: [`Vector`](../reference/orm.md#vector) and the distance methods on
[`ColumnExpr`](../reference/orm.md#columnexpr), in
[`wreath.orm`](../reference/orm.md). To put one of these searches in front of a
model rather than a person, see
[MCP: a retrieval tool, end to end](mcp.md#a-retrieval-tool-end-to-end).
