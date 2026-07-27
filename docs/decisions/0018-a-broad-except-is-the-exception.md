# 0018. A broad `except` is the exception, not the rule

Date: 2026-07-27
Status: Accepted

## Context

Four blanket suppressions were found in a single session, and three shared one
failure mode: **the system keeps working, quietly degraded, with no signal.**

- A dropped `LISTEN` connection ended cross-worker fan-out for the process
  lifetime.
- A database down at boot left no doorbell task spawned at all.
- A chunked pass whose first shift never enqueued was simply never driven.

The first carries the trap that makes this a decision rather than a style note:
`Connection.notifications()` **returns** rather than raises when the connection
closes, so the loop died with no exception at all and the `suppress` was
catching nothing. **A site is not safe merely because nothing appears to raise
there.**

## Decision

Reach for these in order, and fall to the next only when the one above genuinely
cannot work:

1. **Guard the precondition** so nothing can raise and the `try` disappears.
   This is also the fast path from first principles: raising and unwinding costs
   far more than a predicate, so a broad catch on a path where the "exceptional"
   case is not rare has routed the *common* case through the expensive
   machinery. If you are catching something that happens often, you wanted a
   check.
2. **Catch the specific type.** `except (OSError, ValueError)` is not a blanket
   catch. A `suppress(Exception)` around a database call hides driver errors and
   programming errors alike, and only one deserves to be survivable.
3. **Catch broadly, count it, and waive it in place with a written reason.** A
   bare `# noqa: BLE001` is itself a finding.

Never swallow `CancelledError`, `KeyboardInterrupt` or `SystemExit`.

`messaging.MessageBus` is the reference for step 3: the catch is narrow, the
degradation is counted, and infrastructure failure stays distinguishable from a
user callback raising — `doorbell_reconnects` versus `handler_errors`.

## Consequences

- Every long-lived loop that can fail carries a counter, so degradation is
  observable rather than inferred from absence.
- `ruff`'s `BLE` rules are enabled repo-wide, which surfaced 49 waivers written
  against a rule nothing was enforcing (ADR 0024).
- Some catches are legitimately broad and say so in place: best-effort cleanup,
  a fire-and-forget publish where the row already committed, and a connection
  boundary in the server where one bad peer must not stop the process. A
  supervisor, an accept loop, or a startup path is never one of these.
- Scope matters as much as breadth. A failing `Pool.release` inside
  `SingletonRunner`'s `finally` ended the contention task for the process
  lifetime; the guard belongs in the supervised loop, not in `_release`, because
  a user's `async with db.lock(...)` raising is *visible* and should stay so.

## Alternatives rejected

- **Ban broad catches outright.** Rejected: the legitimate cases above are real,
  and a rule with no exception gets routed around with `except Exception as e:
  pass  # noqa`.
- **Allow them with a comment.** Rejected as insufficient: three of the four
  incidents had comments. The counter is what makes the degradation *visible*;
  prose only makes it *explicable* after someone already suspects it.
- **Log instead of counting.** Rejected: a log line in a loop that fires every
  reconnect is noise that gets filtered, and a filtered signal is no signal.

## What would reverse this

Nothing. The ordering could gain a step, but a broad catch with no counter is
the shape that produced every one of these incidents.
