# Hybrid search

[Vector search](vector-search.md) finds the rows that share your *meaning*.
[Full-text search](full-text-search.md) finds the rows that share your *words*.
Neither is reliably better than the other, and the cases they fail are not the
same cases: an embedding will happily return a paragraph about the wrong llama,
and a keyword search returns nothing at all when the user typed "cancel" and the
document says "terminate".

Shipping both and picking one per query is a coin toss. Running both and merging
the answers is what production retrieval systems actually do, and
`wreath.queries.fuse` is that merge.

## The problem with adding the scores up

The obvious merge is arithmetic — scale the cosine distance, scale the
`ts_rank`, add them with a weight. It does not work, and it fails quietly.

A cosine distance is in `[0, 2]`, is *smaller* when better, and its useful range
depends entirely on which embedding model produced the vectors. A `ts_rank` is
unbounded above, is *larger* when better, and depends on document length and term
frequency. Any constant that reconciles the two is fitted to one corpus and one
model, and it is wrong again the first time either changes — not with an error,
but with quietly worse results that nobody attributes to the weight.

**Reciprocal-rank fusion throws the numbers away and keeps the positions.** A row
is scored by where it *placed*:

```
score = Σ  1 / (k + rank)
```

summed over the searches that returned it, ranks counted from 1. Rank is the one
thing both searches produce on the same scale, because it has no scale. A row
that both searches placed mid-table beats a row that only one of them placed
first — which is the behaviour hybrid retrieval is wanted for.

## Declaring one

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


found = await Documents(session).hybrid(q=await embed(text), terms=text)
```

`hybrid` takes the union of its searches' parameters — here `q` and `terms` — and
returns ordinary hydrated model objects, best first. A row both searches returned
appears once, and is one object: the session's identity map already guarantees
that.

The two searches are ordinary declared queries. They keep their own names and can
be called on their own, which is the point of declaring them: a fusion is
assembled from reads you can run, inspect, and test individually.

## What each search must be

Three things are checked when the class is defined, because each is a silent
failure otherwise:

**Named.** `fuse` takes the attributes beside it, never a query written inside
the call:

```python
# Refused at class-definition time.
hybrid = fuse(
    query().order_by(Document.embedding.cosine_distance(Param("q"))).limit(50),
    query(Document.search.matches(Param("terms"))).order_by(...).limit(50),
)
```

A search declared inline belongs to no class, so it appears in no
`Documents.declarations()` listing — and the tools that walk a query set by name
would skip it without a word, the migration scanner that finds transitional
columns among them. Declare each search as its own attribute and fuse those
names, as above. A search declared on a *different* `Queries` class is named too,
so `fuse(Documents.nearest, Documents.matching)` is fine.

**Ordered.** A rank *is* a position, so a search with no `order_by` has no
ranking to contribute. `LIMIT` without `ORDER BY` returns whichever rows the
plan happened to produce, and fusing those is fusing noise.

**Bounded.** Every search in a fusion needs a `limit(...)`. This is not
bookkeeping — it is the whole shape of the feature. A fusion is a merge of two
*shortlists*, and for the vector half the bound is also what lets the approximate
index answer at all: `ORDER BY embedding <=> $1 LIMIT 50` is the only form an
HNSW index serves, and without the `LIMIT` you have a sequential scan and a
distance computed for every row in the table.

Pick the shortlist depth well above the number of rows you intend to show. Fifty
per search for ten results is a reasonable starting point: a row that neither
search ranked in its top fifty was not going to survive the fusion anyway, and a
depth of ten would throw away exactly the rows fusion exists to rescue.

## Choosing `k`

`k` damps how much a first place is worth. It defaults to `60`, the value from
the paper the technique comes from, and it is very close to "flat": at `k = 60`
the difference between rank 1 and rank 3 is about 3%, so *agreement between the
searches* dominates and a single strong hit does not run away with the answer.

```python
hybrid = fuse(nearest, matching, k=5).limit(10)
```

Lowering `k` makes the top of each search count for much more — at `k = 5`, rank
1 is worth 1.33× rank 3. Reach for that when one search is much more trustworthy
than the other in your corpus and you would rather it won outright. `k = 0` is
pure reciprocal rank, where first place is worth three times third.

Ties in the fused score are broken by primary key ascending, so the same data
returns the same order.

## What it costs today

**A fusion runs each of its searches as its own statement.** Two searches are two
round trips to PostgreSQL, and the merge happens in the process that asked.

That is a real cost and it was the deliberate trade. The alternative that fits
today's compiler is a single query with the ranks computed by window functions in
the `ORDER BY` — and `row_number() OVER (ORDER BY embedding <=> $1)` computes a
distance for *every surviving row*, because neither window can be bounded by the
`LIMIT` that makes the HNSW index worth building. That is one round trip and a
sequential scan. Two bounded, index-assisted searches beat one unbounded one at
any table size worth the feature.

The shape that would be both — two bounded top-k derived tables joined in `FROM`,
fused in SQL — is a compiler feature wreath does not have yet (subqueries appear
in `WHERE` today, not in `FROM`). **The public API above is the same either
way**: you declare which searches to fuse, the constant, and how many rows you
want, and nothing in the call or the result tells you where the merge happened.
When derived tables land, `fuse` compiles to one statement and no application
changes.

If you are measuring, the thing to measure is the two searches, because that is
where the time is. The merge is a dictionary and a sort over `n × halves` primary
keys, with `n` the shortlist depth you chose.

## Related

* [Vector search](vector-search.md) — the `Vector` column, the four distances,
  and the index that answers them.
* [Full-text search](full-text-search.md) — the `TsVector` column, `.matches()`,
  and `.rank()`.
* [Combine keyword and semantic search](../cookbook/recipes/hybrid-search.md) —
  the whole endpoint, in one page.
* [MCP: a retrieval tool, end to end](mcp.md#a-retrieval-tool-end-to-end) — the
  same `fuse` in front of a model, Cedar-gated, bounded per caller, and on an
  audit trail, with a nine-line handler that declares none of those.
* [`wreath.queries`](../reference/queries.md) — `fuse`, `Fusion`, and the
  declared-query machinery underneath.
