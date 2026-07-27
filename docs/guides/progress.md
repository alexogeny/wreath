# Reporting task progress

A long task — importing a spreadsheet, rebuilding a report, calling a slow
upstream — should be able to say "42%, processing invoices", and the client
should be able to watch it. Wreath already has the transports (JSON, SSE,
WebSockets) and durable jobs give you the state; `wreath.progress` is the small
convention in between.

## User story: a progress bar for an import

> *As an API author, my `POST /imports` kicks off a slow import. The frontend
> wants to show a progress bar and a status line, then say "done". I want to
> report progress from the task and expose both a poll endpoint and a live stream
> without hand-rolling a convention each time.*

```python
from wreath.progress import ProgressRegistry, status_response, progress_stream

progress = ProgressRegistry()      # bounded, in-process; create once

@app.post("/imports")
async def start_import(request):
    task_id = new_id()
    reporter = progress.reporter(task_id)
    asyncio.create_task(run_import(reporter, await request.body()))   # your task runner
    return {"task_id": task_id}

@app.get("/imports/{task_id}/status")     # polling
async def import_status(request):
    return status_response(progress, request.path_params["task_id"])

@app.get("/imports/{task_id}/stream")     # live (SSE)
async def import_stream(request):
    return progress_stream(progress, request.path_params["task_id"])
```

Inside the task, report against the handle:

```python
async def run_import(reporter, body):
    rows = parse(body)
    for i, row in enumerate(rows):
        await ingest(row)
        reporter.update(100 * (i + 1) / len(rows), f"row {i + 1}/{len(rows)}")
    reporter.done("import complete")
    # on error: reporter.fail(exc)
```

- **`GET .../status`** returns `{"task_id", "percent", "message", "state", "error"}`
  — `404` once the entry expires. Perfect for a poll every second or two.
- **`GET .../stream`** is an SSE stream of `progress` events until the task reaches
  `done`/`failed`, ideal for a live bar with no polling.

### Who may watch a task

`status_response` and `progress_stream` take an `authorize(task_id) -> bool`
predicate. It matters more than it looks: `jobs.launch` makes the task id the
job id, which is a **sequence**, so without a guard the ids are countable and
every task's state, message, and error text is readable by whoever counts.

```python
@app.get("/imports/{task_id}/status")
async def status(request):
    task_id = request.path_params["task_id"]
    return status_response(progress, task_id, authorize=lambda tid: owns(request, tid))
```

A refused caller gets the same `404` an unknown id does — a distinct `403`
would confirm which ids exist, which is most of what enumeration wants. The
predicate is synchronous; await what you need before calling.

`progress_stream(..., max_duration=…)` bounds how long one connection may stay
open, and messages are capped at `MAX_MESSAGE_CHARS`.


## Over a WebSocket instead

```python
from wreath.progress import push_progress

@app.websocket("/imports/{task_id}/ws")
async def import_ws(ws):
    await ws.accept()
    await push_progress(ws, progress, ws.path_params["task_id"])
```

## User story: the mutation that takes ninety seconds

> *Importing a herd takes a minute and a half. It cannot happen inside the
> request, so it has to become a durable job — and now I need a task id, a
> place to keep the percentage, a way for the web worker to see progress
> written by a job worker, and something to mark the task finished when the job
> dies. That is four things to get wrong.*

It is one thing. Give the job runner a progress registry, and give the registry
the message bus:

```python
progress = ProgressRegistry(app.messaging("bus", database="app"))
jobs = app.jobs("work", database="app", progress=progress)

@jobs.task("import_herd")
async def import_herd(ctx, path):
    llamas = parse(path)
    for i, llama in enumerate(llamas):
        await ingest(llama)
        ctx.report(100 * (i + 1) / len(llamas), f"llama {i + 1}/{len(llamas)}")
```

The endpoint hands back something to watch, and the stream endpoint is the one
you already have:

```python
@app.post("/herd/imports")
async def start_import(request, path: str):
    return (await jobs.launch("import_herd", path)).as_dict()
    # -> {"task_id": "8821", "state": "queued"}

@app.get("/herd/imports/{task_id}/stream")
async def watch_import(request):
    return progress_stream(jobs.progress, request.path_params["task_id"])
```

Four properties, each of which is a bug someone has shipped:

* **The job id *is* the task id.** No second identifier to mint, correlate, or
  leak. `ctx.task_id` inside the handler is the same string the client got.
* **The task is watchable before a worker picks it up.** `launch` seeds it as
  `queued`, so a client that polls immediately sees a pending task instead of a
  `404` it will quite reasonably read as failure.
* **You never write `done`.** The runner sets it — it is the only thing that
  knows whether a raised exception means *retrying* or *given up*. A retry
  leaves the task `running`, because a retry is not an ending; only a
  dead-letter reports `failed`, with the error attached.
* **Progress crosses workers.** The job runs on worker 3; the browser is
  connected to worker 1. The registry publishes each report on one bus channel
  and every worker applies it, so whichever one holds the SSE stream can answer.

An idempotent submission behaves the way you would want, too: `launch(...,
key="nightly-import")` twice returns the *same* task id, because the second
call looks up the row the unique index kept rather than handing back nothing.
If that row is gone by the time it is read — purged after completing, in the
window between the conflict and the lookup — there is no task to watch, and
`launch` raises `wreath.jobs.JobVanished` instead of returning an id that
cannot be polled. Every `TaskHandle` you are given carries a real job id.

That handle is watchable even when the original `launch` happened somewhere
else. Progress crosses workers at-most-once and is never replayed, so a worker
that started later — or that simply missed the publish — has no entry for a task
that is genuinely running, and the handle it just handed you would `404` on
status and hang on the stream. So a deduplicated `launch` seeds the task as
`running` *when this worker has nothing for it*, and leaves it alone when it
does: an import already at 70% here keeps its 70%, rather than appearing to
start over because somebody submitted it twice.

### From GraphQL

The same handle, and no new schema machinery — a mutation returning `ID` passes
the value straight through:

```python
@api.mutation("importHerd", returns="ID")
async def import_herd(info):
    handle = await jobs.launch("import_herd", info.arguments["file"])
    return handle.task_id
```

```graphql
mutation { importHerd(file: "herd.csv") }     # "8821"
```

Then `GET /herd/imports/8821/stream` — SSE, already built. GraphQL has no
subscription transport here, and it does not need one: the task id is a plain
string and the stream is a plain endpoint.

## Scope and lifetime

The registry is **bounded** (`max_tasks`, `ttl`) — no external store, no
unbounded growth.

### Every stream ends by saying why

A stream that simply stops is indistinguishable from a connection that dropped,
so each one closes with a final event naming the reason:

| `state` | what happened |
| --- | --- |
| `done` / `failed` | the task finished; this is the event you were waiting for |
| `expired` | the registry no longer holds the task. It aged out past `ttl` or was evicted past `max_tasks` — the work may well still be running, but nothing here can tell you. See [below](#what-expired-does-and-does-not-tell-you) |
| `unknown` | the id never appeared. Either it was never launched, or you asked the wrong worker |
| `detached` | `max_duration` ran out while the task was still going. Reconnect and pick up where the registry is |

`Progress.terminal` stays a fact about the *task* — only `done` and `failed`.
`Progress.ends_stream` is the broader question a client actually asks: is
anything else coming? The last three are `ends_stream` without being `terminal`,
because the registry losing track of a task is not the same as the task
stopping. Each carries the last percent seen, so a bar can show "stalled at 40%"
rather than snapping back to zero on the way out.

That matters most for exactly the case the `ttl` is sized for: an import that
outlives its registry entry used to end by appearing to still be running, with
`state: running` as the last thing the client ever saw.

### What `expired` does and does not tell you

`expired` covers two situations the registry cannot tell apart: the task is
still running and this worker can no longer see it, or it finished somewhere
else and the entry aged out. Both are "the registry forgot", and the registry is
the only thing being asked.

It is worth being precise about the one guarantee it *does* carry: **the task
was not terminal as far as this worker ever saw.** A `done` or `failed` report
ends the stream on its own event, before `expired` can fire — so `expired` never
means "it finished and you missed the news on this worker". It means the last
thing this worker knew was in progress.

Distinguishing the two would mean reading the job row, and neither
`status_response` nor the stream does that, deliberately. `status_response` is
synchronous — a handler that needs to await something does it before calling —
and making it a coroutine to answer a diagnostic question would change a public
signature for every caller. In the stream it would put a database round trip on
a polling path, at precisely the moment the registry is under pressure and
entries are aging out.

The division is the same one the rest of this page draws: **progress is
commentary, the job row is the record.** A client that needs an authoritative
answer to "did this finish?" should ask the jobs surface, where the answer is
durable, rather than the registry, where it is bounded by `ttl` and `max_tasks`
by design.

Without a bus it is in-process, which is right for tasks running in the web
process and for tests. With a bus it is fleet-wide, and delivery is
at-most-once as ephemeral fan-out is: a worker that misses an update gets the
next one. Percentages are a running commentary rather than a ledger, and the
terminal state is the one that matters — if it must survive a worker restart,
read the job row.
