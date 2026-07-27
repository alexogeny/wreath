# 0012. Server deadlines use a slot array with a tournament tree, not asyncio's heap

Date: 2026-07-27
Status: Accepted
Absorbs: the retired `keep generic asyncio timers separate from server deadlines`
record, whose separation principle survives.

## Context

asyncio keeps timers in a binary heap: O(log n) insert and cancel, plus a
cancellation-compaction pass. A server at high request rate churns **two timers
per request** — a keep-alive deadline and a request deadline — and **almost
always cancels them before they fire**.

That workload is unusual. Classical timer analyses optimise for firing; here
firing is the rare case and cancel-before-fire is the common one. The heap pays
its `log n` and its compaction on exactly the operation that dominates.

A first attempt used a classic hashed timing wheel: `slots` buckets at
`resolution` seconds, a `rounds` counter per node, and a cursor that ticks. The
tick itself became the cost — a mostly-idle server still advanced the cursor
slot by slot, and the bridge heartbeat that drove it was pure overhead.

## Decision

Server-owned deadlines use a dedicated native structure: a slot array **plus a
tournament tree over per-slot minima**, with `min_ties` recording how many live
nodes tie a slot's tree minimum (`src/wreath/_native/reactor_internal.h:26`).
The cursor **jumps** to the next real deadline rather than ticking. Insert and
cancel are pointer splices on an intrusive doubly-linked list — no reallocation,
no heapify, no compaction.

Generic `asyncio.call_later` timers keep their own semantic path. The separation
is deliberate: server deadlines can take a structure tuned for cancel-heavy
churn precisely because they do not have to preserve general timer semantics.

## Consequences

- Insert and cancel are O(1) with a fixed, small memory footprint.
- The structure is a hybrid, not the textbook hashed wheel — there is no
  `rounds` counter and no overflow list. Documentation that describes it as
  Varghese–Lauck is wrong, and was, until this record.
- Being tickless is a property of the tournament tree, so the tree is not an
  optimisation on top of the wheel; it is what makes the wheel viable.
- **A known defect follows from the slot masking.** Deadlines congruent modulo
  `nslots` share a slot, and the fire loop walks that slot's entire chain per
  sweep. Measured: 4000 timers in one slot cost 25.5–26.9 ms against 0.36 ms for
  the same count spread one per slot — ~75× on arrangement alone. It is recorded
  as a marked complexity contract (ADR 0022) rather than silently tolerated, and
  it is not yet fixed, because sorted-chain insertion only relocates the
  quadratic to insert and the real fixes are structural.

## Alternatives rejected

- **asyncio's heap.** Rejected on the workload: `log n` insert and cancel plus
  compaction, paid on the operation that dominates.
- **A classic ticking hashed wheel.** Tried and rejected: the tick was the cost.
- **A sorted list per slot.** Rejected: it moves the walk from fire to insert
  without removing it.

## What would reverse this

`IORING_OP_LINK_TIMEOUT`. A timeout linked to the receive operation it bounds is
cancelled by that operation's completion, with no userspace bookkeeping at all —
which is exactly the shape of a request deadline. That would delete this
structure for request deadlines rather than improve it, and deleting beats
optimising. Keep-alive deadlines span operations and would still need something.
