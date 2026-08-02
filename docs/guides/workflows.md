# Workflows

Some work is one thing. Sending a receipt is one thing: it either happens or it
is retried, and [`wreath.jobs`](jobs.md) already does that properly — claimed
with `FOR UPDATE SKIP LOCKED`, fenced, retried, dead-lettered.

Other work only looks like one thing from the outside. "Check out" is really:
hold the stock, charge the card, book the courier, send the receipt. Four calls
to four systems, three of which can fail after the ones before them have already
changed something in the world. That shape has a name — a *saga* — and what makes
it hard is not the ordering. It is that your worker can die between any two steps,
and the world does not roll back with it.

## User story: a checkout that can be interrupted

> *As a payments engineer, I need the card charged only after stock is held, and
> the hold released if the courier booking fails. And when a deploy restarts the
> worker mid-checkout, I need it to carry on — not charge the customer twice.*

```python
from wreath.workflows import PostgresWorkflowStore, Workflow

checkout = Workflow("checkout")

@checkout.step(compensate=release_hold)
async def reserve_stock(context):
    return await inventory.hold(context.results["order_id"])

@checkout.step(compensate=refund)
async def charge_card(context):
    return await payments.charge(context.results["order_id"])

@checkout.step
async def book_courier(context):
    return await courier.book(context.results["reserve_stock"])

store = PostgresWorkflowStore(database)
await checkout.run(store=store, key=f"checkout:{order_id}")
```

Declaration order is execution order. There is no dependency graph, deliberately:
a saga's undo chain is inherently a sequence, and a graph would make "newest
first" ambiguous at exactly the moment you need it to be obvious.

## The three properties that matter

**A completed step never runs twice.** Each step's return value is recorded before
the next one starts, so `resume` re-enters at the first step with no record. This
is the whole difference from a `for` loop over callables, which re-runs everything
from the top and repeats every non-idempotent side effect on the way.

```python
# the worker died after reserve_stock; another picks it up
await checkout.resume(store=store, key=f"checkout:{order_id}")
```

The resumed step reads the *recorded* result, not a recomputed one —
`context.results["reserve_stock"]` is the hold id the dead worker got.

**A failure undoes what succeeded, newest first.** Reverse order is not cosmetic:
step 3's undo routinely depends on state step 2 established. And the step that
*raised* is not compensated, because it did not complete — running an undo against
state that was never established is how a recoverable failure becomes a corrupt
one.

**A compensation that fails is counted, not swallowed.** Compensation runs when
something has already gone wrong, which is exactly when `except Exception: pass`
is most tempting and most damaging: it leaves a half-compensated saga with no
signal at all. The undos behind a failed one still run, and the count is recorded:

```python
status = await checkout.status(store=store, key=f"checkout:{order_id}")
if status.compensation_errors:
    ...   # partly undone; a human needs to look
```

That count is read from the store rather than returned, and the reason is worth
stating: a run that compensates re-raises the step's own exception, because a
caller wants the error it would have got without a saga wrapped around it. So the
summary has to be durable — which is the more useful place anyway, since a
half-compensated saga must be findable *later*, by someone who is not holding the
traceback.

## Two refusals

**One instance key runs once.** `key=` is the same guarantee
`JobRunner.enqueue(key=...)` gives. A second `run` for a key that already finished
returns the recorded outcome rather than executing anything.

**Renaming a step of a live instance is refused.** Completion is recorded per step
*name*. Rename one while an instance is mid-flight and the record matches nothing,
so a naive resume redoes work it already did — and reports success. `resume`
compares the stored step list against the definition and raises
`WorkflowDefinitionChanged` naming the step it cannot account for:

```python
try:
    await checkout.resume(store=store, key=key)
except WorkflowDefinitionChanged:
    ...   # finish or discard the instance, or restore the step name
```

This is the one failure in the module that would otherwise be completely
invisible, which is why it is loud.

## A query budget for a step

```python
@checkout.step(compensate=release_hold, query_budget=50)
async def reserve_stock(context): ...
```

How many times this step may hydrate one model before that is a defect.
Crossing it raises from the query that did, so the traceback names the loop.

It covers the step's **compensation** too, counted as its own scope. That is
the half worth watching: an undo runs only when something has already gone
wrong, so an N+1 in one is discovered during an incident or not at all.

Omitted, the step is observed rather than bounded — see
[Finding the N+1 query](n-plus-one.md) for why failing is opt-in everywhere
outside a request.

## A saga is one trace

The instance records the traceparent of the run that began it, and every later
execution of that instance runs under it — including a `resume` in a different
worker on a different day, and including the compensation chain.

That is deliberately *not* "whatever the resuming worker happened to be doing".
A saga's whole reason to exist is surviving a crash and carrying on elsewhere,
so if the resume adopted the new worker's context the trace would break at the
one moment you are relying on it, and the two halves of one business
transaction would appear as two unrelated traces.

An instance begun with no trace binds nothing, rather than leaving the resuming
worker's context in place — a trace that names the wrong cause is worse than one
that names none.

Only the `traceparent` is stored, not `tracestate`. `tracestate` is vendor
routing for the next hop of a live call; a saga that resumes an hour later is
resuming a trace, not continuing a conversation, so storing it would only age in
the row.

A workflow instance created by an older build has no such column. That resumes
untraced rather than failing: losing the trace is a degradation, losing the saga
is not.

## Choosing a store

`InMemoryWorkflowStore` is for tests and single-process work. Be clear-eyed about
its limit: it satisfies the resume contract *within* one process, which is
precisely not the situation sagas exist for.

`PostgresWorkflowStore` is the durable one. It keeps its tables in a dedicated
system schema with a `tenant` column rather than relying on `search_path`, for the
reason [`wreath.jobs`](jobs.md) does the same — tenant isolation should not depend
on a session setting being right. Apply the DDL with `schema_sql()`, or through
[`wreath.schema`](../reference/schema.md) alongside the rest of your components.

`compensate=False` on `run` is the escape hatch for a *retryable* failure: the
completed steps stay recorded, nothing is undone, and `resume` carries on from
where it stopped. Use it when the failure invalidates the attempt but not the
saga — a timeout, a rate limit — and leave the default when the whole thing has to
come apart.

## Writing down the half-finished ones

There is one state a saga can be in that nothing else in Wreath can describe
after the fact: stopped in the middle. The stock is held, the card is charged,
the courier call failed, the refund either ran or it did not — and by the time
somebody is looking at a stuck order, the traceback is gone and
`compensation_errors` is a number that says *whether* the unwind held rather
than what happened.

So a step can be recorded, into the same `WFR1` file a job attempt goes into:

```python
from wreath.recording import (
    WorkflowStepPolicy, WorkflowStepRecorder,
    WorkflowStepTrigger, WorkflowStepTriggerKind,
)

recorder = WorkflowStepRecorder(
    WorkflowStepPolicy(
        triggers=(
            WorkflowStepTrigger(WorkflowStepTriggerKind.COMPENSATION_FAILURE),
        ),
    ),
    directory="/var/lib/wreath/recordings",
)

await checkout.run(store=store, key=f"checkout:{order_id}", recorder=recorder)
```

The trigger above is the one worth arming first. It fires only when a saga
stopped *and did not unwind* — the state that no retry reaches from where it
now is, and the one that needs a person rather than a redeploy.

The recording is written after the undo chain, so it carries which
compensations ran, which failed, and which steps had none to run at all. It
names the step's position and the step before it, because a saga's cause is the
step before it rather than a request. It records no step's return value, and
nothing arms by default.

See [recording a workflow step](../reference/recording.md#recording-a-workflow-step)
for the whole shape.

**Reference:** [`wreath.workflows`](../reference/workflows.md).
