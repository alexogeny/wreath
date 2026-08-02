# 0027. A log cursor is a transaction id, not a sequence number

Date: 2026-08-02
Status: Accepted

## Context

`wreath.log` is one append-only log shared by four callers — the audit trail, a
change feed, a stream of a job's output, and a meter of billable events. Every
one of them reads it the same way: *give me everything after where I got to*.

The obvious cursor is the primary key. A `bigint GENERATED ALWAYS AS IDENTITY`
column is monotonic, cheap, indexable, and already there.

**It is also wrong, and wrong silently.** A sequence value is allocated when the
`INSERT` runs, not when the transaction commits. Two overlapping writers can
therefore commit out of allocation order:

| | writer A | writer B |
|---|---|---|
| t0 | `BEGIN`; insert → `seq = 1` | |
| t1 | | `BEGIN`; insert → `seq = 2` |
| t2 | | `COMMIT` |
| t3 | reader sees `seq = 2`, records cursor `2` | |
| t4 | `COMMIT` | |
| t5 | reader asks for `seq > 2` — **row 1 is never delivered** | |

The gap is invisible (nothing errors), intermittent (it needs the overlap), and
load-dependent (it gets more likely exactly when the system is busy). For a
response cache that is a stale entry. For a change feed it is a delete the
client never applied, and the client keeps serving a row it should not have.

## The second wrong answer

The obvious repair is to keep ordering by `seq` and *gate* on visibility — read
only rows whose transaction has settled, `WHERE xid < pg_snapshot_xmin(...)`,
and keep remembering `seq`.

That is still wrong, and it is worth writing down because it looks correct.
Sequence order and transaction-id order are independent. Suppose row 3 takes a
*later* xid than row 5:

* the reader's horizon admits row 5 but not row 3, so it delivers row 5 and
  records cursor `seq = 5`;
* row 3's transaction settles;
* the reader asks for `seq > 5`. Row 3 is below the cursor and is never
  delivered.

Gating fixes *which* rows are eligible. It does not fix the fact that the
ordering key and the eligibility key are different keys.

## Decision

**The ordering key and the cursor are both `(xid, seq)`.**

Every row carries `xid xid8 NOT NULL DEFAULT pg_current_xact_id()` beside its
identity column. A read is:

```sql
SELECT ... FROM log
WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
  AND (xid, seq) > ($1::xid8, $2)
ORDER BY xid, seq
```

`wreath.log.Cursor` is that pair, and `Cursor.encode` is what a client
round-trips (a `Last-Event-ID`, a `?cursor=` parameter).

### Why it cannot skip

Let the reader's cursor be `(cx, cs)`, reached under a horizon `H`. Every row it
delivered had `xid < H`.

A row that becomes newly eligible later must have had `xid >= H` at that read —
otherwise it was already eligible and already delivered. `pg_snapshot_xmin` is
monotonically non-decreasing, because it is the oldest *active* transaction id
and ids are assigned increasing. And the cursor's own `cx < H`.

So any newly eligible row has `xid >= H > cx`, which makes `(xid, seq) > (cx, cs)`
true regardless of its sequence number. Nothing can arrive behind the cursor.

The row comparison is spelled as a row comparison rather than as
`xid > $1 OR (xid = $1 AND seq > $2)` so the planner drives the composite index
directly; the two are the same predicate, and only one of them is recognised
cheaply.

`xid8` rather than `xid`: the 32-bit type wraps, and a cursor built from a
wrapping counter compares wrongly exactly once every four billion transactions —
rare enough to survive review, certain enough to happen.

## Consequences

**Reads lag by the oldest open transaction.** A row is invisible to the log
until every transaction older than it has finished. On a healthy database that
is milliseconds. Under a transaction somebody left open — an idle-in-transaction
session, a long analytical query, a debugger paused at a breakpoint — the
horizon is pinned and *every* log reader stalls while writers carry on.

This is the honest cost of the design and it is observable rather than
mysterious: `PostgresLog.horizon_lag` reports the distance between the newest
appended row and the horizon. Zero is healthy; a number that grows and does not
come back is a held snapshot, not a broken log.

**`pg_current_xact_id()` assigns a permanent transaction id.** For an appender
this costs nothing — the `INSERT` was going to assign one anyway. It does mean
`wreath.log` must not be used for a read-only path that would otherwise never
take an xid.

**A cursor is opaque.** Callers must not do arithmetic on it, compare it for
distance, or synthesise one. `Cursor.decode` refuses anything that is not a
cursor it emitted, because on the resumption path the value arrives in a
client-supplied header.

## Alternatives considered

**An advisory-lock critical section around the append**, so allocation order
equals commit order and `seq` alone becomes a valid cursor. Correct, and it
removes the horizon lag entirely — a row is readable the instant it commits.
The cost is that every append to a log serialises against every other append to
that log. That is the right trade for a log whose readers cannot tolerate lag
and whose write rate is low, and the wrong one for the chunk buffer, which is
the highest-rate caller. It is not built: a `strict_order=True` flag that nobody
had yet needed would be an untested second ordering discipline, and the moment
one is needed this record is where the design already is.

**A logical replication slot.** Correct, and it is what the sync-engine products
outside wreath use. It needs `wal_level=logical`, a slot per consumer, and an
operational answer to the abandoned-slot failure mode — an unread slot pins WAL
until the disk fills. That is a large operational liability for a primitive that
four in-process subsystems consume, and it would need a `wreath doctor` check of
its own to be safe to ship.

**A commit-timestamp column** (`track_commit_timestamp`). Rejected: it is off by
default, it is a cluster-wide setting wreath cannot assume, and its resolution
is not a total order.
