# What a worker holds in memory

Every bounded in-process table and queue Wreath builds, in one place, with the
knob that tunes it.

This page exists because the question "how much memory does a worker hold, and
where do I change it?" used to be answerable only by grepping for a dozen
constructor calls whose parameters were all named differently — `max_entries`,
`max_tasks`, `cache_size`, `query_cache_size`, `statement_cache_size`,
`max_sessions`, `capacity`. The bounds were all there and all sensible; what was
missing was any way to see them together.

**`uv run wreath-map-lint` keeps this table honest.** Every construction of a
`wreath.kv` table or a `wreath.queue` queue under `src/wreath` must appear here,
and MAP013 fails the build for one that does not. A page like this is worth
reading only if something makes it true, which is the same reason
`docs/agents/manifest.json` has a gate.

## The tables

| What it holds | Where | Bound | Default |
| --- | --- | --- | --- |
| Response cache entries | `response_cache.cached` | `max_entries`, `ttl` | 1024, per-decorator |
| Idempotency replays | `middleware.idempotency.MemoryIdempotencyStore` | `max_entries`, `ttl` | 4096, 24h |
| Task progress reports | `progress.ProgressRegistry` | `max_tasks`, `ttl` | per-registry |
| Login attempt counters | `users` | `max_entries`, `ttl` | per-policy |
| Second-factor challenges | `_secondfactor.MemoryChallengeStore` | `max_entries` | 4096 |
| Signature replay nonces | `signatures.NonceLedger` | `max_entries`, `ttl` | 16384, 300s |
| Questions awaiting an answer | `entity.EntityRegistry` | `max_pending` | 1024 |
| Started and attached stream keys | `streams.Streams` | `started_capacity`, log retention | 4096, per-stream retention |
| Parsed GraphQL documents | `graphql` | `cache_size` | per-schema |
| Pinned permission tokens | `_auth.permissions` | `max_entries` | 64 |
| Compiled query plans | `orm.Registry` | `query_cache_size`, `query_cache_bytes` | 512, 8 MiB |
| Prepared statements, per connection | `postgres` / `_pgdriver` | `statement_cache_size`, `statement_cache_bytes` | per-pool |

## The queues

| What it carries | Where | Bound | Default |
| --- | --- | --- | --- |
| Log records, projector to writer | `_export` / `_logsink` | `capacity` | 4096 |
| Finished traces, projector to exporter | `_export` / `_otlp` | `capacity` | 4096 |
| Server-to-client notifications, per MCP session | `_mcp.session` | `max_pending_notifications` | 64 |

## Reading the numbers

Two of these are bounded **twice** — by entry count and by retained bytes — and
those are the two where one entry's size varies by orders of magnitude. A
compiled query plan for a three-column insert and one for a twelve-way join are
both "one entry"; only the byte budget stops five hundred of the second kind
from being a memory problem. See [`wreath.kv`](kv.md) for how `cost` and
`max_bytes` work.

Everything here expires or evicts **lazily**. Nothing sweeps in the background,
because a sweeper duplicates across workers and swallows its own failures — so a
table that has gone quiet holding large values keeps holding them until it is
next touched, and `purge()` is how you make that happen on purpose.

None of these are shared between workers. Four workers hold four copies, so the
figure that matters for a deployment is the sum of this page times the worker
count. Where an answer has to be the same on every worker, the shared half is
[`wreath.store.PostgresStore`](store.md).
