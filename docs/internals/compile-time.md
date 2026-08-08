---
description: The work a Wreath request no longer does, because compilation already did it — and the one optimization that measured well and was refused anyway.
---

```hero
eyebrow: Doing less
title: The cheapest instruction is the one compilation already retired.
lede: A request answers questions that were settled the moment routes compiled. Every one of those it re-asks is work nobody ordered — and the biggest of them cost more than everything the SIMD kernels saved.
action: Scanning bytes in bulk -> vectorisation.md
action: What it bought -> #what-it-bought
```

There is an order of magnitude between each of these:

**per deploy** → **per process** → **per connection** → **per request** → **per byte**

[Scanning bytes in bulk](vectorisation.md) is about the last column, and it was
worth doing because those loops were genuinely naive. But per-byte work is
bounded by bytes, and a hello-world request touches a few hundred of them while
spending several thousand instructions deciding *what to do*. The larger lever
is not doing a thing faster. It is doing it one column to the left.

`AGENTS.md` has said so for a long time — *"prefer explicit startup compilation
and caching over repeated request-time introspection"*. This page is what
happened when that rule was checked against the code rather than assumed.

## The response validator was interpreting its annotation three times a response

A handler that declares what it returns is doing the documented, idiomatic
thing:

```python
@app.get("/items/{item_id}")
async def item(request) -> dict[str, Any]:
    return {"id": request.path_params["item_id"], "ok": True}
```

That annotation cost **19 microseconds per request**, against roughly 5 for the
same handler with no annotation at all. Declaring your contract made the route
five times slower.

Three separate walks were reading the same fixed annotation on every response —
`_response_input` to project the value onto the declared shape, `validate` to
check it, `_jsonable` to reduce it to JSON primitives. Each descended the
annotation node by node calling `typing.get_origin` and `typing.get_args`, which
are not cheap, for an answer that was identical on every request since startup.

Meanwhile the *request* body had executed a flat native plan for ages:
`_body_validator` compiles the annotation once and hands it to
`_core.run_validation`. The response side had simply never been given the same
treatment.

So all three walks moved to route-compile time. What comes back is a closure
that descends only the value:

| step | interpreted | compiled | |
| --- | --- | --- | --- |
| project (`_response_input`) | 5.04 µs | 0.66 µs | 7.6× |
| validate | 4.57 µs | 0.47 µs | 9.7× |
| convert (`_jsonable`) | 7.12 µs | 2.87 µs | 2.5× |

The convert step needed one more idea. Six `isinstance` tests stood between a
plain scalar and the exit — enum, `UUID`, `Decimal`, `bytes`, datetime,
container — and nearly every leaf of nearly every response is a plain scalar
that falls through all of them. An **exact-type** test lets those leave first:

```python
if type(value) in _JSON_SCALARS:
    return value
```

`type(value) in`, never `isinstance`, and the distinction is the entire safety
argument. `class Colour(StrEnum)` *is* a `str`, so an `isinstance` fast path
would return the enum member itself and put a non-JSON object in the body — a
bug that `assert body == {"shade": "blue"}` cannot see, because a `StrEnum`
compares equal to its own value. The tests in
`tests/test_binding_response_compilation.py` assert `type(actual) is
type(expected)` for exactly that reason, and that assertion is the one carrying
the case.

Two more removals came out of the same reading. The scalar test is **inlined**
at each container element and each dataclass field rather than left to the
child closure, which would answer it identically one function call later —
nearly every leaf of nearly every response is a scalar, so that call was the
largest cost of serializing a model. And the projection is **dropped entirely**
where `_projection_is_identity` can prove it does nothing: it only ever filters
a mapping to a dataclass's declared fields or rebuilds a container, so an
annotation holding neither — `dict[str, Any]`, the shape an idiomatic JSON
handler returns — was allocating a new mapping with identical contents and
handing it straight to the validator.

That claim is checked rather than asserted. `tests/test_binding_response_compilation.py`
runs the projection over every case the predicate vouches for and requires the
result to be equal *and* the same type, because a wrong `True` is silent: the
validator would receive undeclared keys for a dataclass, or a `tuple` where a
`list` was expected. `list[int]` is deliberately refused even though it is
usually identity — it is only identity when the handler already returned a
list, and a compiler cannot know that.

End to end, in one process with both implementations present:

| response shape | interpreted | compiled | |
| --- | --- | --- | --- |
| `dict[str, Any]` | 17.51 µs | 1.79 µs | 9.8× |
| `dict[str, str]` | 18.00 µs | 1.78 µs | 10.1× |
| `list[int]` | 31.74 µs | 1.95 µs | 16.3× |
| a dataclass | 10.40 µs | 0.92 µs | 11.3× |
| `list[dataclass]`, 5 items | 57.06 µs | 4.75 µs | 12.0× |

## Dispatch decides per application, not per request

`_handle_http` is general because applications are: global hooks, four
middleware stages, two routing protocols, a dynamic host matcher, the recorder.
An application that registered none of those still evaluated every one of those
branches to discover it had none of them — and none of the answers can change
between requests.

`_select_dispatch` now picks the dispatcher once, when routes compile.
`_handle_http_plain` exists only for the shape whose branches are all statically
false, and there is no partially-specialized third variant, because two
implementations of dispatch is already the most this is worth.

The invariant that makes it safe is worth stating on its own: **every delegation
back to the general path is decided before the specialized one has any effect
that a second dispatch would repeat** — before the `Request` exists, before a
hook could have run, before the recorder was told anything. Handing the request
back then costs one extra table lookup, paid only by requests that miss, carry
authentication, or are being recorded. A future `await self._handle_http(...)`
placed below the `Request` construction would be a double-execution bug however
plausible it looks.

## A `def` dependency cost more than an `async def` one

`inspect.isawaitable` ran on the return value of every synchronous dependency,
because a `def` may still hand back something to await. It costs ~390 ns — it
finishes on `isinstance(value, collections.abc.Awaitable)`, and an ABC instance
check is ~220 ns of that alone. The asynchronous branch short-circuits before
reaching it, which is why the *synchronous* spelling measured dearer.

`collections.abc.Awaitable` decides membership by looking for `__await__` on the
type, so asking the type directly answers the same question for 72 ns. The two
disagree on exactly one input — a class `register()`ed onto the ABC without
defining `__await__` — and that object was never awaitable, because `await`
looks the method up on the type too. The old answer only bought a `TypeError`
one line later.

The dependency cache keys went the same way. `(marker.fn, marker.scope)` was
rebuilt, and `marker.use_cache` re-read, per dependency per request, at three
sites, for a value fixed at compile time.

| route | before | after |
| --- | --- | --- |
| one synchronous dependency | +3.50 µs | +2.81 µs |
| three synchronous dependencies | +5.89 µs | +4.06 µs |

The same sweep found the same shape on the request side, smaller:
`query_constraints.get(name)` was asked per query parameter per request against
a mapping fixed at compile time, and now rides in the parameter's own plan
entry.

One hypothesis it killed, which is worth recording so nobody re-runs it: an
annotated route with typed parameters stacks two or three `async def` wrappers —
binder, response validator, status coercion — and fusing them looked like the
next win. A wrapper layer measures **0.12 µs**. Three of them are not where the
time is, and the fusion would have traded a lot of clarity for it.

## The same syscall, in a second place

`uv run wreath-tape-decomp` prices the global middleware tape, and it put
`RequestIdPolicy` at **+6.77 µs** — for generating a correlation id. Almost
all of it was one line:

```python
value = os.urandom(16).hex()
```

`os.urandom` performs a real `getrandom` syscall on every call. This is the
defect that [made CSRF token minting cost 2.6 µs](vectorisation.md#not-everything-fast-is-a-kernel),
and the fix for it — `fill_random`, drawing through glibc's vDSO — was already
sitting in `security.c` with no second caller. `_core.random_hex(n)` gives it
one.

| | |
| --- | --- |
| `os.urandom(16).hex()` | 2.79 µs |
| `_core.random_hex(16)` | 0.25 µs |
| | **11.2×** |

The hook went from 4.12 µs to 1.22 µs of its own work, and the whole tape from
+32.59 µs to +24.42 µs of a request.

The rest of the tree was swept for the same call. Every other `os.urandom` is
either seeded once at startup (`_logsite`'s fingerprint key, an object store's
URL secret) or is genuine key material where a documented, auditable source
matters more than 2 µs (`_secondfactor`'s shared secret, `_userkit`'s password
salt). `random_hex` is deliberately bounded to 64 bytes and documented as *not*
key material, so it cannot quietly become the source for one of those.

## Synchronous handlers are served rather than 500ing

A `def` handler used to be neither supported nor refused. It was awaited like
any other endpoint, `await {"ok": True}` raised `TypeError` inside dispatch, and
the caller got a 500 naming nothing — the worst of the three available answers.

It is now called directly and costs no coroutine object, no `send(None)`, and no
`StopIteration` unwind, which is the same trade `before_sync` and `after_sync`
already make for middleware. Dispatch calls the handler and awaits only what
came back awaitable, so an `async def` route pays one `is` test for the
privilege and every wrapper — response validation, status coercion, the binder —
preserves whichever convention it was handed.

The caveat belongs next to the feature and is in
[the routing guide](../guides/routing.md#synchronous-handlers): a synchronous
handler runs **on the event loop**, not in a thread pool. Wreath will not move
it to a thread behind your back, because that would silently cost more than the
coroutine it saved and make the fast spelling the slow one.

| route | `async def` | `def` |
| --- | --- | --- |
| no return annotation | 5.01 µs | 4.72 µs |
| annotated `dict[str, Any]` | 8.20 µs | 8.00 µs |

## What it bought

Interleaved arms, one process, A/A control at the far end of the round from its
twin. The floor was 0.032 µs, so a delta had to clear 0.063 µs to be reported.

| arm | median |
| --- | --- |
| annotated handler, generic dispatch | 9.65 µs |
| annotated handler, specialized dispatch | 9.13 µs |
| the same handler written `def` | 8.93 µs |
| two synchronous dependencies | 8.47 µs |
| bare `Response`, no annotation | 4.78 µs |

Against where this started, an annotated `dict` handler went from **24.0 µs to
9.1 µs**. Most of that is the response validator; the dispatcher accounts for
0.52 µs of it, which is small in absolute terms and comfortably resolved.

One boundary crossing disappeared with it —
`docs/agents/request-boundary-baseline.json` records the minimal application at
4 pre-activation C calls where it recorded 5, because the specialized dispatcher
never reads the length of a hook list it does not have.

## Recycling the `Request` measured well and is not shipped

Constructing a `Request` costs 590 ns, which is real money on a 4.7 µs request.
Reusing one per connection and resetting all fifteen slots costs 186 ns. The
prize is a genuine ~400 ns, and the mechanism is sound-looking: recycle only
when `sys.getrefcount` shows nobody else kept a reference, which is 71 ns.

It is refused, because the guard is the part that free-threading changes.
Reference counting on the free-threaded build is deferred and biased; a
`getrefcount` that under-reports hands a still-live `Request` to the next
connection's handler. That is not a performance regression, it is one caller
reading another caller's body, headers and identity — and `AGENTS.md` treats
free-threading as a supported execution mode, not a variant to be careful about
later.

A version of this can exist, but it needs ownership proven by construction — a
`Request` the handler cannot outlive — rather than inferred from a counter whose
meaning is build-dependent. That is a different change.

## Two more were refused, and the reasons are the useful part

**Answering misses in C.** A `404`, a `405`, a CORS preflight and a `304` are
each a pure function of the request line, the request headers and a table the
router already builds at startup. Answering them inside `server_http1.c` would
take the interpreter out of those requests entirely, which is a bigger idea than
anything above.

It is unsound here, and not for a mechanical reason. Wreath documents that
global middleware runs for a miss — *"a `404` needs an ID and its headers just
as much as a `200` does"* — and one of those middlewares is the rate limiter,
which is [documented as counting misses](../guides/middleware.md): *"a flood of
`404`s counts against the bucket too."* A C short-circuit skips the tape, so it
would switch that defence off for precisely the traffic it exists to absorb.
Answering a scanner's 404 flood more cheaply, while no longer counting it, is
not an optimization.

It is safe in exactly one configuration — no global middleware, no exception or
status handlers, no static files, no preflight fallback — which is the
configuration with the least to skip and the least to gain. The idea is not
dead, but what it needs first is a native middleware tape, so the short circuit
runs the policy instead of stepping around it.

**Batching the C-to-Python boundary.** This one rested on a premise that reading
the code disproved. `server_http1.c` does not create a task per request: it
steps the handler coroutine directly with `PyIter_Send`, and a request that
finishes without suspending returns `PYGEN_RETURN` and **owns no asyncio Task at
all**. The `create_task` nearby is the fallback for a genuine suspension, which
is a request that was going to wait on something anyway.

What batching could still amortize is coroutine creation and the first step,
across the requests one ring drain makes ready together. That is a much smaller
prize than "one task per request" implied, and it cannot be resolved without a
concurrent-load rig — a single-connection microbenchmark never has a batch to
amortize over. It is worth doing after there is a rig that can see it, and not
before.

## Measuring this needed less care than the kernels did, for one reason

Everything on this page is Python, so [the relink perturbation that dominates
`_core` measurement](vectorisation.md#measuring-this-is-harder-than-writing-it)
does not apply — there is no link step between the arms. What still applies is
everything else: discard the first run, compare position-matched runs on a
powersave governor, and interleave the arms so drift hits all of them.

The strongest evidence here is the response-validator table, and it is strong
for a structural reason rather than a statistical one: the interpreted walks are
still in the tree as the definition the compiled ones are crossed against, so
both implementations run **in the same process, in the same round**. That is the
same discipline `_core.simd_probe()` exists to provide for the kernels, arrived
at from the opposite direction.
