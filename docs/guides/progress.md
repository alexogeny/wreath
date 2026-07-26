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

Without a bus it is in-process, which is right for tasks running in the web
process and for tests. With a bus it is fleet-wide, and delivery is
at-most-once as ephemeral fan-out is: a worker that misses an update gets the
next one. Percentages are a running commentary rather than a ledger, and the
terminal state is the one that matters — if it must survive a worker restart,
read the job row.
