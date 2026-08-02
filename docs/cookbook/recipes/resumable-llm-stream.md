# Stream a model's answer so a reconnect resumes it

A five-minute answer is a five-minute HTTP response, and the browser will lose
that connection. The fix is not a longer timeout: it is to stop attaching the
generation to the connection at all. The producer runs as a durable job, writes
into an append-only log, and the request becomes a *reader* of that log — so a
reconnect is a second read from wherever the first one stopped.

```python
from wreath.jobs import JobRunner
from wreath.log import PostgresLog
from wreath.streams import Streams, declaration

jobs = app.jobs("work", database="app")
streams = Streams(jobs=jobs, log=PostgresLog(database, declaration()))
```

`declaration()` is a chunk buffer with an hour's retention, and it **refuses**
`retain=KEEP_FOREVER` — the log is delivery, not transcript. Register its schema
alongside your other components, and drive the retention walk:

```python
jobs.drive(streams.retention_pass(), cron="*/5 * * * *")
```

## The producer

Registered at import time, in every process, because the worker that runs it
never calls `start`:

```python
@streams.producer("chat", retries=2)
async def answer(stream, question: str) -> None:
    async with provider.stream("POST", "/v1/messages", json={...}) as response:
        async for token in response.aiter_text():
            await stream.write(token)
```

Use [`wreath.http_client`](../../reference/http_client.md) for the call — it
already pools, retries and rate-limits, and Wreath deliberately ships no
provider client of its own.

## The two routes

```python
@app.post("/chat")
async def ask(body: Ask) -> StreamHandle:
    return await streams.start("chat", key=body.stream_id, args=(body.text,))


@app.get("/chat/{key}")
async def resume(request: Request, key: str) -> Response:
    return streams.attach(
        key,
        since=request.header("last-event-id"),
        authorize=lambda stream: stream in request.state.session.streams,
    )
```

`authorize` is not decoration. A stream key is usually a conversation id, and
without it whoever can guess one can read somebody's generation; a refusal
answers `404`, identical to an unknown key, so the endpoint is not an oracle for
which keys exist.

## The client

```javascript
const output = document.querySelector("#answer");
const source = new EventSource(`/chat/${key}`);

source.addEventListener("chunk", (event) => { output.textContent += event.data; });
source.addEventListener("superseded", () => { output.textContent = ""; });
source.addEventListener("end", () => source.close());
source.addEventListener("error", (event) => { show(event.data); source.close(); });
```

`EventSource` sends `Last-Event-ID` on reconnect by itself, so the tunnel case
needs no code. The `superseded` listener is the one line people leave out and
must not: it fires when a retried producer has replaced what you already
rendered, and a client that concatenates instead of clearing shows the answer
twice and blames the model.

## Before it goes to production

Two checks, both of which pass silently in development and fail in production:

```nginx
proxy_buffering off;    # or nginx holds every token until the response ends
proxy_read_timeout 3600s;
```

```python
from wreath.streams import check_stream_attachment

for finding in check_stream_attachment(streams):
    log.warning(finding)
```

The second one is a bill, not a bug. The producer runs whether or not anybody is
attached — that is the whole point — so an application that starts a stream on
every page load and attaches on one in ten is paying for output nobody reads.

See [Streaming that survives a reconnect](../../guides/streams.md) for the
reasoning, and [`wreath.streams`](../../reference/streams.md) for the API.
