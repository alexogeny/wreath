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
