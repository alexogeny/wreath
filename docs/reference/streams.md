# `wreath.streams`

Resumable streams over durable work: the producer runs in a job, its output goes
to a log, and a client attaches to the log instead of to the producer.

An LLM response is a five-minute HTTP response, and HTTP has no resume. The
market answer is a durable session that decouples delivery from the connection,
and it is sold separately by everybody because no application framework provides
it. Wreath already had both halves — `wreath.jobs` is durable execution with a
fence, `wreath.log` is a total order under a cursor that cannot skip — so this
module is the join rather than a new mechanism.

Reach for it when the thing being produced outlives the connection asking for
it, and when a client that reconnects must not pay for it twice. Reach for
[task progress](../guides/progress.md) instead when the client only needs a
percentage and losing one is fine, and for [`wreath.rooms`](rooms.md) when the
traffic is live fan-out that nobody replays.

**Write amplification is the objection this design gets**, so it is measured
rather than argued. A producer emitting a token at a time is the worst case for
an append-only log: one row per token is one round trip per token. `StreamWriter`
buffers through [`wreath.log`](log.md)'s `append_many`, which decomposes a batch
into powers-of-two multi-row inserts, and against a local PostgreSQL that is
**70.5 µs per chunk written one at a time and 8.14 µs per chunk at 512 chunks per
flush — 8.7×** — over 1024 chunks of 29 bytes, eleven interleaved rounds with the
direction of each round alternating, and an A/A noise floor of 0.70 µs (1.0 % of
the baseline). The intermediate rungs land where you would expect: 9.42 µs at 64
chunks per flush and 8.77 µs at 256. The residue above `append_many`'s own 4.06
µs per row is the writer's per-chunk interpreter work, and it is the half worth
knowing about before reaching for anything cleverer.

**Those figures are the ceiling, and a token stream does not reach it.** The
default policy is `Flush(bytes=4096, every=0.05, capacity=1024)`, and a model
emitting fifty short tokens a second produces a couple of hundred bytes a
second — so the 50 ms timer fires long before the byte threshold, and
chunks-per-flush is two or three rather than five hundred. That is a deliberate
trade in the other direction: a token appearing 50 ms late is invisible to the
person reading it and half a second late is not, so the buffer is sized for
latency and the batching is what stops a *fast* producer — a replay, a file, a
model on a local GPU — from paying a round trip per chunk. Raise `every=` if
your producer is fast and your reader is patient.

The guide is [Streaming that survives a reconnect](../guides/streams.md).

::: wreath.streams
