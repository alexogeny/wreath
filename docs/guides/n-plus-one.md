# Finding the N+1 query

Fifty fast queries where one belonged. Every statement is correct, every
statement is quick, the endpoint returns 200, and the test suite is green — and
the page takes two seconds because it went to the database fifty-one times.

N+1 is the most common performance bug in an ORM-backed API and the least
visible, because nothing about it is *wrong* from inside any single layer. The
ORM sees an ordinary `SELECT`. The server sees a successful request. Seeing the
bug means holding the route and the queries in the same field of view, and in
most stacks nothing does.

Wreath owns the ORM *and* the Flight Recorder, so it can say the useful
sentence:

```text
GET /llamas issued 51 statements; 50 of them hydrated Trek
```

That sentence contains the fix.

## User story: catching it before it ships

> *A handler of mine loops over llamas and touches `llama.treks` inside the
> loop. It passes review, it passes CI, and it falls over the first time a herd
> has more than a dozen animals in it. I want that to be a test failure.*

Install the guard in development:

```python
from wreath.doctor import NPlusOneGuard

app.add_middleware(NPlusOneGuard(limit=10))
```

Now the tenth query for the same model doesn't run. It raises:

```pytb
Traceback (most recent call last):
  File "herd.py", line 22, in llamas
    trek_count = len(await session.fetch(Trek.select().where(...)))
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
wreath.doctor.NPlusOneDetected: GET /llamas issued 10 statements;
10 of them hydrated Trek
```

The traceback points at line 22 — the loop. That is the whole value: an N+1 is
not hard to fix once you know which line is doing it, and the hard part has
always been finding the line.

Three things worth knowing:

* **The offending query never runs.** The ledger is consulted *before* the
  statement goes out, so the guard stops the loop rather than watching it
  finish.
* **Each model trips once per request.** A runaway loop gives you one
  diagnosis, not a thousand.
* **`limit=10` is a judgement, not a law.** A handful of related lookups is
  ordinary work; ten of the same model is a loop. Raise it for an endpoint that
  legitimately fans out, or lower it to be strict.

The fix is nearly always an eager load:

```python
herd = await session.fetch(Llama.select().include(Llama.treks.selectin()))
```

One statement for the llamas, one for all of their treks. Two, not fifty-one.

## User story: staging should tell you, not stop you

> *I want the finding in the logs, but I do not want a staging request to fail
> over a performance smell.*

```python
app.add_middleware(NPlusOneGuard(limit=25, on_detect=log.warning))
```

`on_detect` receives a [`Finding`](../reference/doctor.md) and the request
carries on. `Finding.describe()` is the one-liner above; `finding.repetitions`
has the per-model counts if you want to emit them as metrics.

!!! note "Development and staging, not production"

    The guard is cheap — an application that never constructs one does not so
    much as read a context variable per query — but a guard that fails
    production requests is a worse outage than the N+1 it was watching for. In
    production, read the recorder instead.

## User story: it's already in production

> *A route got slow last week. I cannot reproduce it locally, and I am not
> going to deploy a profiler to find out why.*

You do not have to reproduce anything. The Flight Recorder already recorded
what those requests did — including, for each ORM read, which model it
hydrated. Ask the running server:

```console
$ wreath doctor n-plus-one /run/wreath/inspector.sock
3 request(s) queried one model 10+ times:

  GET /llamas issued 51 statements; 50 of them hydrated Trek
      Trek                        50 queries     41.2ms
      replay it: wreath replay --request 8821

  GET /treks/{trek_id} issued 34 statements; 33 of them hydrated Llama
      Llama                       33 queries     28.7ms
      replay it: wreath replay --request 8907

An eager load usually collapses these into one statement.
```

`wreath doctor` is a protocol client: it connects to the Inspector socket and
never imports your application. Nothing is deployed, nothing is restarted, and
the diagnosis comes from traces the server was already keeping.

The server must be recording in Detailed mode or better — phases are what carry
the model, and an unsampled request has none. See
[Observability](observability.md) for recorder modes.

### As a CI gate

```console
$ wreath doctor n-plus-one "$SOCKET" --threshold 20 --strict
```

`--strict` exits non-zero when anything is found, so a load-test stage can fail
the build on a regression. `--json` prints the same findings as a versioned
document for anything that wants to consume them.

## Why other frameworks ship this as a plugin

Because they have to. An N+1 detector bolted on beside the framework can see
the queries or it can see the routes, rarely both, and it cannot see which
*model* a statement hydrated without re-parsing the SQL it did not write.

Wreath records that at the seam where it is already known. The ORM stamps each
read's model onto the recorder's `ORM_HYDRATE` phase, the completion carries
the route, and the two are joined off the request path by the projector that
was already running. The detector is a few dozen lines because everything it
needs was already being written down.

The same `request_id` in the report is what
[`wreath replay`](../reference/replay.md) needs, so the next step after finding
one is turning it into a regression test.
