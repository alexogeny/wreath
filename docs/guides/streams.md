# Streaming that survives a reconnect

Somebody asks a question, a model starts answering, and four minutes into a
five-minute answer the train goes into a tunnel. The browser does the right
thing — `EventSource` reconnects on its own — and the server does the only thing
it can, which is start the whole generation again. The tokens already on screen
are thrown away, the model is paid for twice, and the person watching learns
that your product does not like tunnels.

The reason is not laziness. It is that HTTP has no resume, and neither does a
generator: there is no "carry on from token 847". A response is a connection,
and when the connection ends so does everything attached to it.

So the answer is to stop attaching anything to the connection. Let the work run
somewhere that outlives it, write what it produces down in order, and make the
request a *reader* of that order rather than the thing producing it. Then a
reconnect is a second read, from wherever the first one stopped, and the
producer never learns it happened.

Wreath was unusually close to this already. The hard half is durable execution
with attempt identity — a job that survives a worker dying, and a fence that
says which of two workers is speaking — and [durable jobs](jobs.md) have had
that for a long time. The other half is a total order with a cursor a reader can
come back with, which is [`wreath.log`](../reference/log.md). What was missing
was the join, and that is all `wreath.streams` is.

## The shape

Three pieces: a producer registered as a task, a `start` that enqueues it, and
an `attach` that reads it.

```python
from wreath.jobs import JobRunner
from wreath.log import PostgresLog
from wreath.streams import Streams, declaration

runner = app.jobs("work", database="app")
streams = Streams(jobs=runner, log=PostgresLog(database, declaration()))


@streams.producer("chat")
async def answer(stream, question: str) -> None:
    async for token in your_model_client(question):
        await stream.write(token)
```

The producer takes a `StreamWriter` and whatever arguments the caller passed. It
runs **in a worker**, under a lease, with a fence — not in the request. That is
the property everything else here depends on: it keeps producing when nobody is
attached, and it is re-run by an ordinary job retry when the worker holding it
dies.

Registration happens at import time in every process, which is why the producer
is not simply handed to `start`. The worker that runs it never calls `start`, so
a callable passed there would have been registered in the one process that does
not need it.

```python
@app.post("/chat")
async def ask(body: Ask) -> StreamHandle:
    return await streams.start("chat", key=body.stream_id, args=(body.text,))


@app.get("/chat/{key}")
async def resume(request: Request, key: str) -> Response:
    return streams.attach(key, since=request.header("last-event-id"))
```

`start` hands back a handle with an id; `attach` returns Server-Sent Events. A
browser needs no library for the second one:

```javascript
const source = new EventSource(`/chat/${key}`);
source.addEventListener("chunk", (event) => { output.textContent += event.data; });
source.addEventListener("end", () => source.close());
```

`EventSource` sends `Last-Event-ID` on its own when it reconnects, and every
event Wreath frames carries an `id`. So the tunnel case needs no code at all:
the browser reconnects, sends the id of the last chunk it rendered, and the
handler above hands it to `since=`.

## No model client ships here, and that is deliberate

Providers change monthly. [`wreath.http_client`](http-client.md) already pools,
retries and rate-limits, and a thin wrapper over one vendor's streaming
endpoint would be stale before the next release. What is framework-shaped is
this primitive, and it does not care what is on the other end of the producer —
a model, a long report, a slow export, a shell command's output.

## Exactly-once is something the client does

The wire is at-least-once. A reconnecting client can be handed a chunk it
already has, because the alternative — the server remembering exactly what
reached each client — is a promise no transport keeps and every product in this
category claims anyway.

What Wreath gives you instead is the thing that makes de-duplication *possible*:
every event has an id that is a position in one total order, so a client that
remembers the last id it applied can drop anything at or before it. That is a
smaller promise, and it is one that holds.

## What happens when the producer is retried

This is the honest hard part, and it is worth reading before you ship.

A worker dies at chunk 400. The lease expires, the job is handed to another
worker, and the producer runs again — from the beginning, because a model
generation is not resumable. Without care, the log now holds two copies of
chunks 1 to 400 and every reader concatenates both.

Every row Wreath writes carries the **fence** of the attempt that wrote it, which
the queue was already bumping on each claim. The reader holds the highest fence
that stream has reached and applies one rule:

- a row **below** it is skipped. This is not hypothetical: the worker whose
  lease expired is often still alive and still flushing, and its late rows land
  *after* the newer ones in commit order;
- a row **above** it means a retry, so the reader emits a `superseded` event
  and then the new content.

A client that attaches fresh is never handed the replaced range at all — the
fence is seeded from the table before the first read — so it sees one clean
stream. A client that was already watching gets `superseded` and must clear what
it has:

```javascript
source.addEventListener("superseded", () => { output.textContent = ""; });
```

That event is not optional decoration. A client that concatenates a replaced
range renders duplicated text and blames the model, which is the failure this
whole scheme exists to avoid — so the reader *tells* it rather than leaving it
to notice.

If you would rather the replaced rows simply went away, `Streams(on_retry=
"truncate")` deletes them when the retried attempt starts. It is simpler, it
loses whatever the first attempt produced, and it tells the client exactly the
same thing — the two policies differ only in whether the old bytes survive on
disk.

## Cancellation, once

A stream is a job, so it cancels the way a job cancels and there is no third
path to learn:

```python
await streams.cancel(key, reason="the user closed the tab")
```

The terminal record is written **first** and the queue row is fenced second,
because the invariant that matters is that attaching to a cancelled stream
returns something rather than hanging. Fencing the row is the whole of the
mechanism on the queue's side: the running attempt's bookkeeping matches no row
when it lands, so nothing retries it. What it does not do is reach into another
process and stop a call already in flight — nothing can, and a framework that
implied otherwise would be describing a cancellation that still charges the
card.

## Retention is not optional

`declaration()` refuses `retain=KEEP_FOREVER`, by name. The chunk log is
**delivery, not transcript**: it exists so a reconnecting client can catch up,
and an hour of it is generous for that.

Prompts and completions are the newest large pile of unclassified personal data
in most applications, and the way they become unerasable is exactly this — a
buffer that was only ever meant to hold a few minutes of tokens quietly becomes
the only complete record of what everybody asked, in a table with no owner and
no window. Conversation history belongs in the ORM, where it has a model, a
tenant, and a retention decision somebody made on purpose.

The window is executed rather than declared:

```python
runner.drive(streams.retention_pass(), cron="*/5 * * * *")
```

That is a [chunked pass](chunked-passes.md) — durable, resumable, paced, and
counted — so "we have a retention policy" is a number in the pass ledger rather
than a sentence in a document.

## A stream nobody reads still costs

The producer runs whether or not anybody is attached. That is the entire point,
and it is also a way to spend a great deal of money on output nobody sees: an
application that starts a stream on every page load and attaches on one in ten
has built a token furnace with no meter on it.

So the two numbers are kept apart, and there is a check that reads them:

```python
from wreath.streams import check_stream_attachment

for finding in check_stream_attachment(streams):
    print(finding)
```

## SSE through a proxy

One deployment detail defeats this feature completely and silently. Nginx
buffers proxied responses by default, which means it holds every token until the
response ends — and the response ends when the generation does. In development
there is no proxy and everything works; in production the client gets one
five-minute blob.

`SSEResponse` already sends `x-accel-buffering: no`, which nginx honours, but the
settings that matter live on the proxy:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 3600s;
proxy_http_version 1.1;
```

`wreath.streams.SSE_PROXY_HEADERS` holds the same list, so a deployment check can
read it rather than a wiki page.

## Over a WebSocket instead

When a socket already exists — a client connected for something else, a room it
is already in — a second HTTP request for the stream is a second connection for
no reason:

```python
from wreath.streams import push_stream

@app.websocket("/ws")
async def socket(ws):
    await ws.accept()
    await push_stream(ws, streams, key)
```

It is the same reader, framed as JSON text frames instead of SSE. Each frame
carries `id`, so a client that reconnects resumes exactly as `EventSource`
would.

Reference: [`wreath.streams`](../reference/streams.md), with
[`wreath.log`](../reference/log.md) and [`wreath.jobs`](../reference/jobs.md)
underneath it.
