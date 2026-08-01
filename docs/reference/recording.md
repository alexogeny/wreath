# `wreath.recording`

Two halves of one subsystem, reached for at opposite ends of a bad day.

**Capture policy**, which is most of this module: deny-by-default value types
describing what a Forensic-mode recorder may retain, validated so a policy
cannot exceed its own bounds. The never-capture field classes cannot be enabled
through this API at all — that is structural, not a default. Capture itself
ships: the native recorder arms, triggers, redacts, and writes `WFR1`.

**Crash forensics**, which is `read_ring_file`. Given
`TelemetryConfig.ring_path`, the recorder maps its ring from a file instead of
the heap, so a process that dies badly leaves its last records readable; this
reads them back. It reports what it could not recover rather than raising,
because a file recovered from a crash is where a strict reader is least useful.
`wreath flight read <path>` is the same thing from a terminal.

This is not durability. The mapping survives the *process* — a segfault, a
`SIGKILL`, an `abort()` — because the pages belong to the kernel. It does not
survive a machine losing power before they are written back. A clean shutdown
`msync`s; nothing else does.

## Recording a durable job attempt

A failed request can be replayed from its bytes. A failed **durable job** is
harder: the request that caused it succeeded hours ago and is gone, the
arguments were computed from state that has since changed, and the failure is
on attempt 4 after two retries and a lease expiry. Wreath owns the queue, the
payload, the retry policy, the driver and the recorder, so it can record one
attempt at the fidelity it records a request — and
[`wreath replay to-test`](replay.md) turns it into a pytest.

### What an attempt recording is

**One execution of one task by one worker, and the boundaries it crossed.**
Four groups of facts, and nothing else:

1. **Identity.** The job id, the queue, the task name, the attempt number, the
   tenant, the dedup key — and **the fence the worker held**. The fence is not
   decoration. After a lease expiry two workers can both believe they own a
   job, and a recording that cannot say which one it was is a recording of an
   ambiguity.
2. **Cause.** The `traceparent` the queue row carries, so an attempt recording
   joins to the request that produced its arguments. It is free: the queue
   already stores it.
3. **Boundaries crossed during the attempt**, in order — database statements,
   object-store calls, outbound HTTP — as `(seam, target, coordinate)` triples.
   That is deliberately the *same* coordinate space a
   [`FaultSchedule`](replay.md#wreath.replay.FaultSchedule) is keyed to, which
   is what lets a recorded failure replay as an injected fault without any
   payload passing between the two.
4. **Outcome**, exactly one of four: `completed`; `raised`, with the error type
   and message; `deadline_cancelled`; or `lease_expired`.

**Four outcomes, not three.** `deadline_cancelled` is separate from `raised`
because nothing failed — `JobRunner` cancels a handler at `deadline_for(task)`
and counts it in `run_timeouts` precisely because the cause is usually a slow
dependency. Folding it into `raised` would make a recording report a defect
where there was none. `lease_expired` is written by the *sweeper*, because the
worker that held the lease is by construction not there to write it; such a
recording carries no boundary trace, and that absence is the truth rather than
a gap.

### What it is explicitly not

- **The scheduler's decision to run it.** Lease acquisition, fencing and retry
  timing are the queue's behaviour and have their own tests. Replaying an
  attempt re-runs *that attempt*.
- **The other attempts.** Attempt 4 of a job is its own recording, in its own
  file. A recording spanning four attempts would have to model the backoff
  clock, which is the scheduler again.
- **The arguments.** See below.

### The arguments, and the names a positional array does not have

**Nothing is captured unless an operator names it, by task and parameter.** The
default `AttemptPolicy` has an empty `argument_allowlist` and records only
`argument_count`, so a reader can see that arguments existed.

The reason there is an allowlist at all, and the reason it is keyed the way it
is: `RedactionPolicy` below is entirely **name**-keyed — allow/hash/mask over
header names, the same three over query-parameter names, and
`BodyCapture.STRUCTURED`, whose selected fields are selected by field name.
Deny-by-default works because every unit it governs *has* a name an operator can
list. `args jsonb` is a **positional array**: `enqueue("send_password_reset",
user_id, token)` stores `[41, "e3b0c4…"]`, and there is no name in the *row*.
There is one in the *process*, though — the runner holds the handler, and the
handler has a signature:

```python
AttemptPolicy(
    triggers=(AttemptTrigger(AttemptTriggerKind.FAILURE),),
    argument_allowlist=frozenset({"send_password_reset.user_id"}),
    redaction=RedactionPolicy(max_fields=32, max_depth=4, max_body_bytes=4096),
)
```

`user_id` is recorded and `token` never is, which is the whole point. Four rules
make that safe and every one of them fails closed:

1. **No signature, no capture.** A task whose handler is not registered in this
   process — the dead-letter path already meets one, from a release whose
   handler no longer accepts that arity — has no names, so nothing is captured.
   The rule is *deny*, never fall back to position.
2. **The mapping must be total and unambiguous.** A value that lands in `*args`
   or `**kwargs` maps to no declared parameter and is never captured, however
   the allowlist is spelled. A parameter the call did not supply is absent
   rather than defaulted: recording a default the caller never sent would be
   recording this process. The leading parameters the runner itself supplies —
   `handler(ctx, *job.args)` — are aligned past and are not nameable.
3. **The parameter is the unit of consent, and it is the whole argument.**
   Allowing `payload` allows everything inside it, bounded below. There is
   deliberately no `task.parameter.field…` path language: a key space whose
   leaves an operator has never seen is a consent nobody gave.
4. **The value must normalise.** Strings, numbers, booleans, `None`, and
   lists/tuples/dicts of them, within `max_depth`, `max_fields` and
   `max_body_bytes`. Anything else — an object, `bytes`, a set, a non-string
   mapping key, a non-finite float, a cycle, an oversize structure — is
   **withheld with the reason recorded in its place**, so a reader can tell a
   refusal from an absence. A breach withholds the *whole* argument rather than
   a truncated version: a reader cannot tell a list of three from the first
   three of nine.

The recorded form is JSON and is exactly one of `{"value": …}` or
`{"withheld": "<reason>"}`. Normalisation copies into fresh containers and
serialises immediately, so a handler that keeps its argument and edits it
afterwards cannot change what was recorded. A non-empty allowlist requires all
three bounds; a policy that names an argument without them is refused where it
is written.

`replay_attempt` still refuses to run with the wrong arity rather than inventing
values, and the generated test says where to supply them.

### Arming one

Deny-by-default, twice over: a runner with no `AttemptRecorder` records nothing,
and a recorder whose `AttemptPolicy` has no triggers records nothing either.

```python
from wreath.recording import (
    AttemptPolicy, AttemptRecorder, AttemptTrigger, AttemptTriggerKind,
)

jobs = app.jobs(
    "work",
    attempts=AttemptRecorder(
        AttemptPolicy(triggers=(
            # the case worth most of this feature
            AttemptTrigger(AttemptTriggerKind.FAILURE),
            # ... or only the attempt that dead-lettered
            AttemptTrigger(AttemptTriggerKind.FINAL_FAILURE),
            # ... or one task under investigation, sampled
            AttemptTrigger(AttemptTriggerKind.TASK, task="import_herd", rate=0.05),
        )),
        directory="/var/lib/wreath/attempts",
    ),
)
```

Sampling is deterministic **in the job id**, never from an RNG: two workers
looking at the same row have to agree about whether it is being recorded, and a
re-run has to reach the same answer as the run it reproduces.

A `TASK` trigger with no task name is refused. It would mean "record every
attempt", and this subsystem has no spelling for that.

### Bounded, and refused rather than truncated

`max_boundaries` bounds one recording. A job that walks ten thousand rows
crosses the bound, and the recorder then **refuses to write the file** and
counts it in `refused_oversize`. A truncated boundary trace would replay as a
*different* failure — the injected fault would land at whatever statement sits
at that coordinate in the shorter run — and reporting that as a reproduction is
worse than having no recording. The ring sets the same precedent with
`RING_FULL`.

### The container

An attempt is a **record kind inside `WFR1`**, not a second format: one
decoder, one reader, one set of forensics tooling. `read_attempt_recording`
is stricter than the capture-slab reader on purpose. A slab stream recovers a
torn tail because every complete slab before the tear is forensic material worth
keeping; an attempt is *one* record, so a tear does not leave a smaller attempt,
it leaves one missing the part nobody can enumerate. A file that is **truncated**
(no footer, or a record declaring more bytes than it holds), **chunked** (a
record carrying the continuation flag), holds **more than one** attempt, or
holds **none**, is refused by name.

::: wreath.recording
