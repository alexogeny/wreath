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

## Scope and lifetime

The registry is **in-process and bounded** (`max_tasks`, `ttl`) — no external
store, no unbounded growth. It's built for tasks running in the web process
(background tasks, a single-process worker).

For a **durable, multi-process** job whose worker runs elsewhere, the web process
can't see an in-memory reporter — keep the percentage on the job row in Postgres
and build the same `Progress` shape in your status endpoint. The convention
(`percent`/`message`/`state`/`error`, the endpoints, the SSE framing) is identical;
only where the number is stored changes.
