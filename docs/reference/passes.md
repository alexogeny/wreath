# `wreath.passes`

**Backfills, rollups, and reindexes** — a durable, resumable, paced walk over a
table too large to change all at once. Declare a `ChunkedPass`, hand it to
[`jobs.drive`](jobs.md), and it walks by keyset ranges rather than `OFFSET`,
does one chunk per transaction with the cursor advanced *inside* it, holds
itself to a share of the machine, and refuses at declaration time the keys that
would quietly lose rows.

The refusals are the part worth knowing before you reach for it: a boundary that
cannot be proven unique, a leading key column with no index, a fixed ceiling over
a key that is not assigned in order, a `Keyspace()` denominator over a key that
cannot be placed on a line, and a chunk budget that does not fit inside a shift
are all errors where you declared them rather than surprises at three in the
morning.

There are two range sources. `Rows` walks by keyset and asks the table where to
go next, which is why its key has to identify exactly one row. `Buckets` computes
its next range from the calendar and asks the table nothing, so it needs no
unique key at all — only an index, because the chunk's predicate is still a range
scan.

A `Gate` is how a pass makes something else safe afterwards:
**materialise → verify → only then the irreversible step.** Verification is
always a question the database answers — `NoRowsMatch`, `Reconcile`, or
`Constraint`, which adds a `CHECK` as `NOT VALID` and then validates it, so the
check and the thing that will go on enforcing the invariant are the same
predicate. A gate that restates the walk's own `where` is refused: a walk whose
predicate was wrong would otherwise verify its own bug. Verification always
publishes a durable fact, readable through `published_facts()` with nothing but
a connection; running something irreversible is opt-in on top of that.

Two things it reports are worth knowing about before you read a status line.
Every percentage carries the provenance of its denominator, because `64%` and
`64% (estimated)` are different sentences. And an ETA that cannot be computed
honestly is **absent**, with the reason stated, rather than invented — see
[the guide](../guides/chunked-passes.md).

Reach for it directly when you have a re-encryption, a tenant split, or a
re-index to do. Wreath's own store tables already use it — see
[`wreath.store`](store.md) — and neither deferred migrations nor calculated
views ask you to import it.

The guide is [Chunked passes](../guides/chunked-passes.md).

::: wreath.passes
