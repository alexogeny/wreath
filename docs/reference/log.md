# `wreath.log`

An append-only log in PostgreSQL, with a cursor a reader can come back with.

An audit trail, a change feed, a stream of a job's output, a meter of billable
events — four features that need the same small thing, which is rows in commit
order and a way to say *give me everything after where I got to*. Written four
times, the hard part is re-derived four times, and it is a hard part that fails
quietly: a sequence number is allocated before a transaction commits, so a
reader that remembers the highest one it saw skips every row a slower
transaction commits behind it.

So the shape is written once here, and the cursor is a transaction id first and
a sequence number second. Two plausible-looking answers are ruled out by it: a
`bigserial` high-water mark, which the paragraph above shows skips rows; and a
commit timestamp, which is not monotonic across a clock adjustment and is not
unique under concurrency.

**Appends batch, because write amplification is the objection this design gets.**
`append` is one statement, and a producer whose rows are small and frequent —
a token at a time, a metered event per request, an ORM flush of a hundred
audited instances — would pay a round trip for each. `append_many` decomposes a
batch into powers of two and issues one prepared multi-row `INSERT` per rung, so
a thousand rows are six statements. Measured against a local PostgreSQL, that is
69.2 µs per row appended one at a time and 4.06 µs per row in a batch of 512 —
17.1× — with the shipped and batched arms interleaved and an A/A noise floor of
0.9 % of the baseline. The rungs are a bounded set of statement texts on purpose:
an `INSERT` shaped to the exact batch is one round trip and a brand new SQL
string every flush, which would evict the application's own prepared statements
from the driver's per-connection plan cache. Measured, that shape was inside the
noise floor at every batch size tried, so the cache hygiene decided it.

**Retention is declared here and executed by a walk.** `retain=` says how long a
row lives; `retention_pass` turns that into a
[`ChunkedPass`](../guides/chunked-passes.md) — durable, resumable, paced, and
counted, so "we have a retention policy" is a number in the pass ledger rather
than a claim. `purge()` remains, and it is the unbounded `DELETE` a pass exists
to replace: reach for it on a small log, and for the pass on one large enough to
have needed retention in the first place. A `KEEP_FOREVER` log — an audit trail —
refuses both.

Reach for it when something outside the process needs to follow what changed and
must not miss any of it. Reach for [`wreath.messaging`](messaging.md) instead
when a fire-and-forget announcement is enough — a `NOTIFY` is the doorbell that
says *something moved*, and this is the thing you read when it rings. Reach for
[`wreath.queue`](queue.md) when the hand-off is inside one process and losing an
item under pressure is acceptable as long as it is counted.

::: wreath.log
