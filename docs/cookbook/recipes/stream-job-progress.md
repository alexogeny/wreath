# Show a live progress bar for a long task

A slow task — importing a spreadsheet, rebuilding a report — should be able to
say "42%, processing invoices", and the client should be able to watch it. A
`ProgressRegistry` holds that state; you report against a handle from inside the
task and expose a poll endpoint and a live stream without hand-rolling a
convention each time:

```python
import asyncio
from wreath.progress import ProgressRegistry, status_response, progress_stream

progress = ProgressRegistry()      # bounded, in-process; create once

@app.post("/imports")
async def start_import(request):
    task_id = new_id()
    reporter = progress.reporter(task_id)
    asyncio.create_task(run_import(reporter, await request.body()))
    return {"task_id": task_id}

@app.get("/imports/{task_id}/status")      # poll every second or two
async def import_status(request):
    return status_response(progress, request.path_params["task_id"])

@app.get("/imports/{task_id}/stream")      # live SSE, no polling
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

`status_response` returns `{"task_id", "percent", "message", "state", "error"}`
and `404`s once the entry expires — perfect for a poll. `progress_stream` is an
SSE stream of `progress` events until the task is terminal, ideal for a live bar.

The registry is in-process and bounded (`max_tasks`, `ttl`) — no external store,
no unbounded growth. It's built for tasks running in the web process. For a
durable, multi-process job whose worker runs elsewhere, keep the percentage on
the job row in Postgres and build the same shape in your status endpoint.
