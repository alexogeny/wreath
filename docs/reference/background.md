# `wreath.background`

Response-bound background tasks that run after the response is sent.

## Two bounds you get for free

A background task runs after the response is emitted but still **inside the ASGI
invocation**, and a conforming server cannot read the next request on that
connection until the invocation returns. A one-second task measurably delays the
next request on the same HTTP/1.1 keep-alive connection by one second — on a
connection whose client has already been told the work finished.

So two things are bounded for you:

- **Time.** `Wreath(background_timeout=30.0)` cancels a response's tasks once
  they have run that long, and counts it in `app.background_timeouts` (kept apart
  from `background_errors`: nothing failed, work was stopped). Pass `None` to
  restore the old unbounded behaviour.
- **Threads.** A synchronous callable is offloaded to a thread — but to *wreath's*
  pool, not the interpreter's default one. They used to be the same pool
  `wreath.objects` uses for every read, write and fsync, so a route that queued a
  blocking sync task competed for threads with file serving for unrelated users.

The second is not a consequence of the first. Cancelling a coroutine that awaits
a thread does not interrupt the thread, so the deadline bounds how long a
*connection* is held and never how long a *thread* is occupied. Separate pools
are the only thing that bounds the second.

None of this makes a background task durable. It dies with the process; for work
that must survive, use [`wreath.jobs`](jobs.md) and hand the task an identifier.

::: wreath.background
