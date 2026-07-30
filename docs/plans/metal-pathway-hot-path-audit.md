# Prescriptive plan: the metal pathway's remaining hot-path cost

Status: **tier 1 implemented** (July 2026). Tiers 2 and 3 below are prescriptive
and unbuilt. One tier-1 item was measured, disproved, and is recorded here as a
refuted hypothesis rather than deleted, because the mechanism that refutes it
will look like an obvious optimization again to the next reader.

Related material:

- `AGENTS.md` (measurement rules; the cProfile prohibition this audit obeys)
- `docs/decisions/0006-optional-extensions-and-tiers.md` (what "metal" is)
- `docs/plans/future/16-native-event-loop.md` (fixed files, SEND_ZC, spin: planned, unbuilt)
- `docs/agents/request-boundary-baseline.json`
- `src/wreath/reactor.py`, `src/wreath/_native/reactor_poller.c`,
  `src/wreath/_native/reactor_transport.c`, `src/wreath/_native/reactor_ring.c`
- `src/wreath/_native/server_http1.c`, `src/wreath/_native/server_common.c`,
  `src/wreath/_native/http.c`, `src/wreath/app.py`, `src/wreath/server.py`

## How these were found, and what that constrains

`perf record` on a saturated single metal worker (`benchmarks.wreath_server
--loop metal`, plaintext `/`, pinned to one core, load generator pinned to
others), two independent captures of 123k and 47k samples. Every percentage
below is "share of that worker's cycles" and reproduced across both captures;
anything that did not reproduce is not in this document. `perf` rather than
cProfile because cProfile's ~1-2us per call is larger than most of what is being
measured -- see `AGENTS.md`, which records the accepted-then-worthless change
that mistake already produced here.

Two limits worth stating before anyone re-runs this:

- **The load generator co-limits.** `benchmarks/load.py` is the stdlib
  development generator and is itself Python. Throughput A/B on this machine
  (powersave governor, laptop) did not clear the noise floor for anything under
  ~5%. Cycle *attribution* reproduced; throughput *deltas* mostly did not. Any
  item below whose payoff is stated as a throughput number needs an independent
  generator before it is believed.
- **The adaptive spin inflates the denominator.** An unsaturated metal loop
  busy-polls, so some fraction of the profile is a `pause` loop doing no work.
  Percentages are therefore slightly conservative as a share of productive work.

## Tier 1 -- implemented

### 1. Response-head integers no longer go through `PyOS_snprintf`

Every response formats a status code and a content-length, and a chunked one
formats a size line per chunk, through `PyOS_snprintf("%zd"/"%zx")`. glibc's
printf machinery measured at **1.8-2.2% of worker cycles** (`__printf_buffer`,
`__printf_buffer_write`, `__printf_buffer_done`, `_itoa_word`, `___vsnprintf`,
`PyOS_snprintf`), reproduced in both captures. Isolated: 55.4ns per call against
7.4ns for a direct write. The in-situ share exceeds the isolated delta because
printf is called cold and evicts i-cache and branch history the request path
wants back.

Done: `wreath_write_decimal` / `wreath_write_hex` in `server.h`, used by
`append_decimal` (`server_common.c`), `response_append_decimal` and the chunked
size line (`server_http1.c`). Truncation is impossible by construction rather
than checked, guarded by a `_Static_assert` -- compile-time on purpose, because
`python -O` strips `assert` and a wire-format invariant may not depend on an
interpreter flag. **After: the entire printf symbol family is absent from the
profile.** Tests: `tests/test_server_protocol.py`, the three
`test_response_*_renders_*` / `*_nibble_boundaries` cases, parameterized over
pure and native so the pure protocol is the oracle for the native writer.

### 2. `_select_tcp_protocol` no longer runs per accepted connection

`Server._protocol_factory` is called once per accepted connection and called
`_select_tcp_protocol(config)`, which reads `os.environ` (two `KeyError`s raised
and caught inside `os._Environ.get`) and calls `importlib.import_module`.
**Measured 1.82us per connection** to recompute a constant.

Done: cached on `Server._protocol_cls`, resolved lazily on first use so a
`Server` built by hand with an unservable protocol set still fails where it
always did. Test: `tests/test_server.py::test_protocol_class_is_resolved_once_not_per_connection`.

### 3. The accepted socket carries the listener's family/type/proto

`rp_activate_accepted_connection` built the Python socket as
`socket.socket(fileno=fd)`. With family/type/proto at -1 CPython issues
`getsockopt(SO_TYPE)` and `getsockopt(SO_PROTOCOL)` on top of the `getsockname`
it always does -- confirmed by `strace`. **Measured 4.21us -> 2.29us per
accepted connection**, three paired runs, +/-35ns.

Done: `EventLoop._start_serving` carries the listener's three into the poller's
listener spec (now a 6-tuple), `rp_activate_accepted_connection` passes them
positionally. `SOCK_NONBLOCK`/`SOCK_CLOEXEC` are masked out of `type`, because a
listener handed in via `sock=` can carry them and `SO_TYPE` never reports them --
masking keeps the accepted socket's `.type` identical to what the old path
produced. Verified: zero `SO_TYPE`/`SO_PROTOCOL` syscalls across five
connections under `strace`. Test:
`tests/reactor/test_metal_tier.py::test_metal_accept_gives_the_socket_the_listener_family_type_and_proto`.

Together items 2 and 3 return **~3.7us per accepted connection** on the one path
metal otherwise keeps entirely in C.

### 4. REFUTED: `am_send` on the immediate awaitables

The hypothesis was that `ImmediateAwaitableType` / `ValueAwaitableType`
(`server_common.c`) raise `StopIteration` from `tp_iternext` where they could
implement `am_send` and return through a status code instead -- `immediate_next
-> _PyErr_SetObject` is 0.44-0.55% of cycles and is the only caller of
`_PyErr_SetObject` on the request path.

**It does not work, and it was measured rather than reasoned about.** A probe
extension with two type shapes, driven by 1000 real `await`s each:

| shape | `tp_iternext` calls | `am_send` calls |
| --- | --- | --- |
| `am_await` + `am_send` + `tp_iternext` | 1000 | 0 |
| `am_await` + `am_send`, no `tp_iternext` | `TypeError: __await__() returned non-iterator` | -- |

CPython's `SEND` opcode tests `PyIter_Check(receiver)` before it would reach
`am_send`, so an awaitable that is also an iterator always lands in
`tp_iternext`; and dropping `tp_iternext` is not available, because
`GET_AWAITABLE` requires `__await__` to return an iterator. `am_send` on a
custom awaitable is reachable only from explicit `PyIter_Send` callers.

The change was reverted and a comment left at both slot tables saying why. The
raise is structural to the awaitable protocol: **removing it means removing the
`await`, not re-slotting it** -- see tier 2 item 3.

## Tier 2 -- structural, evidenced, unbuilt

### 1. Collapse the `_wreath_http` forwarding coroutine

`app.py:1484` is a pure-forwarding `async def`: it does the `_dirty` check, then
`await self._handle_http(context, receive, send, context.method, context.path,
True)`. A request creates four coroutine objects and this is one of them for no
work. Coroutine machinery (`RETURN_GENERATOR`, `make_gen`, `gen_dealloc`,
`SEND_GEN`, `GET_AWAITABLE`, `_PyFrame_ClearExceptCode`) is **~4.8% of cycles**;
one awaited hop measured at **~120ns** (141ns leaf vs 261ns one-hop, repeated).

Precise change:

1. Move `if self._dirty: self._compile_routes()` from `_wreath_http` and
   `__call__` into the head of `_handle_http` (`app.py:1492`). One branch, no
   new frame, and both existing callers already ran it immediately before.
2. In `spawn_app_task` (`server_http1.c:2168`), when `self->native_app` is set
   and the scope is a `_RequestContext`, vectorcall `_handle_http` with six
   arguments instead of `_wreath_http` with three. Read `method` and `path` off
   the request-context struct directly rather than through `context.method` /
   `context.path`, which also removes two attribute crossings.
3. Resolve the six-argument callable once at protocol init, where
   `native_app` is resolved today. It must stay a bound method looked up per
   protocol, not cached across recompiles, for the same reason `native_app` is.
4. Delete `_wreath_http`.

Risk: `_handle_http` is private and has exactly these callers; the signature is
already six positional arguments. Verify with `wreath-request-trace --check`
(the pre-activation Python-frame count should fall by one) and
`tests/reactor/test_parity.py`.

### 2. Fold the post-parse header scans into the parser, then make the list lazy

After `wreath_http_parse_request_parts` returns, the header list is walked end to
end **four times** on every HTTP/1.1 request, five with a recorder attached:

| scan | site |
| --- | --- |
| host count | `server_http1.c` `handle_head`, the `minor == 1` block |
| framing (`expect`/`content-length`/`transfer-encoding`) | `decide_framing`, `server_http1.c:1882` |
| upgrade (`upgrade` + `connection`) | `is_upgrade_request`, `server_http1.c:2226` |
| response keep-alive (`connection`) | `reset_response_state` -> `headers_have_connection_token`, `server_http1.c:2075` |
| traceparent | `begin_request`, recorder-armed only |

Meanwhile `wreath_http_parse_request_parts` (`http.c:103`) already lowercases
every header name into a stack buffer before interning it, so the classification
is nearly free at the point the bytes are hot.

Precise change, in two independently landable steps:

1. **Classify during parse.** Give the parser an out-parameter: a `uint32_t`
   flag word (`HOST`, `CONTENT_LENGTH`, `TRANSFER_ENCODING`, `CONNECTION`,
   `EXPECT`, `UPGRADE`, `TRACEPARENT`), a host count, and a small fixed array of
   the indices of the classified headers. Dispatch on `name_len` first --
   4/6/10/14/16/17 are distinct -- so each header costs one length compare and
   at most one `memcmp`. Rewrite the five scans above to read the flag word.
   This is worth landing alone.
2. **Then make the list lazy.** With no eager consumer left, the ASGI header
   list has no reason to exist before something reads it. A 12-header request
   currently allocates one list, 12 tuples and 24 `bytes` objects that a handler
   reading no headers never touches; `_PyObject_Malloc` + `_PyObject_Free` are
   **~6% of cycles**. Store the head bytes plus an offset/length array on the
   `_RequestContext` and materialize in the `headers` getter, which
   `request.headers` (`request.py:747`) already funnels through. `_index_headers`
   and the native `build_header_map`/`find_header` helpers can then read offsets
   without ever building the pair list.

Step 2 changes an observable: `request.headers` documents that it returns the
list the request is backed by and that mutating it is visible to later readers.
Materialize-once-and-cache preserves that; materialize-per-call would not.

### 3. Give the native one-shot response a synchronous entry point

This is what tier-1 item 4 turns into once `am_send` is ruled out. `_finish_http`
(`app.py:1841`) does `await protocol._wreath_response(status, headers, body)`,
and that awaitable never suspends -- it returns the module-level
`immediate_none`. The `await` costs `GET_AWAITABLE`, `SEND`, and a raised and
caught `StopIteration` per request, for a call that was always synchronous.

Precise change: add `_wreath_response_sync` to the native protocol returning
`None` instead of an awaitable, keep `_wreath_response` for any caller that
needs the awaitable shape, and call the sync one from the `type(response).__call__
is _RESPONSE_CALL and native_response` branch of `_finish_http`. The pure
protocol needs the same method for parity, since that branch is selected on
`native_response`, not on the extension being present.

Expected: removes the only `_PyErr_SetObject` on the request path (0.31-0.55%)
plus the interpreter's unwind, which the profile attributes elsewhere.

### 4. Length-dispatch the response-header scan in `begin_response_parts`

`begin_response_parts` (`server_http1.c:1049`) is **2.9-3.2% of cycles** and its
hottest instructions are the per-header chain of five `header_name_equals` calls
(`date`, `server`, `content-length`, `transfer-encoding`, `connection`). Each is
length-gated already, so the cost is five length compares per response header.
A `switch (name_size)` makes it one. The same loop runs
`wreath_field_name_valid`/`wreath_field_value_valid` byte scans over
framework-minted constants on every response; a separate question, and the
cheaper answer is probably to validate once where the header list is built
rather than to skip validation here.

### 5. Publish the loop's clock instead of re-reading it

`set_deadline` (`server_http1.c`) calls `mono_now()` -- `PyTime_MonotonicRaw`
plus `PyTime_AsSecondsDouble` -- twice per request; `rp_run_once` reads the
clock again; the spin reads it twice per attempt. `[vdso]` is **1.2-1.5%** and
`PyTime_AsSecondsDouble` **0.32%**. The wheel's resolution is 1ms, so nothing on
this path needs a clock fresher than the current loop iteration.

Precise change: store `loop_now` (already computed at the head of `rp_run_once`)
as an `int64_t` nanosecond member on `ReactorPoller`, expose it to the protocol
through the existing poller pointer, and have `set_deadline`/`arm_deadline_timer`
read it. Keep deadline arithmetic in `int64_t` nanoseconds end to end so the
double conversion disappears with it. Deadlines are the only consumer; anything
wanting a true current time must keep calling the clock.

## Tier 3 -- unexplored axes, measure before building

### 1. The adaptive spin's syscall shape

Measured with `diagnostics=True` over 200k requests: 1.34 spin attempts per
request, **99.7% hit rate**, 781 blocking enters, and **3.27 `io_uring_enter`
per request** -- 2.43 syscalls per spin attempt. The spin is doing its job; the
shape is forced, because `IORING_SETUP_DEFER_TASKRUN` means the CQ cannot
advance without a syscall, so the 32-`pause` inner loop
(`rp_spin_for_completions`, `reactor_poller.c:1628`) only throttles the syscall
rate and never observes a completion it did not itself cause.

Unanswered, in order: is the 32-`pause` interval swept at all (there is no
recorded sweep); would one `io_uring_enter` with `min_complete=1` and an
`EXT_ARG` timeout of the same budget beat N zero-timeout probes; does the
`METAL_SPIN_BUDGET_*` band still fit modern arrival cadence. `WREATH_METAL_ADAPTIVE_POLL=0`
already exists as the ablation switch. **This needs an independent load
generator** -- the A/B on the stdlib one landed inside the noise
(on: 39.1/42.9/44.4k rps; off: 43.6/44.4/41.5k).

### 2. Fixed files / direct descriptors

Only `IORING_REGISTER_RING_FDS` is used (`reactor_ring.c:354`, and its own
comment names the technique: "avoids fdget/fdput in every io_uring_enter").
Socket fds are never registered, so every RECV and SEND SQE pays kernel
`fdget`/`fdput`. `IORING_REGISTER_FILES` plus `IOSQE_FIXED_FILE`, with multishot
accept writing into a direct-descriptor slot, removes that and would also remove
the Python `socket` object from the accept path entirely -- which subsumes
tier-1 item 3 and the remaining 2.29us of it. `reactor_internal.h` already lists
`fixed_files` as an ablation axis; `docs/plans/future/16-native-event-loop.md`
lists it as planned and unbuilt.

Blocker to design first: the registered file table is fixed-size and slot
lifetime must survive a stale completion, which is the same generational problem
`MetalSlab` already solves for connections and operations. Reuse that, do not
invent a second scheme.

### 3. `SEND_ZC` is instrumented but never issued

`send_zc_notifications` / `send_zc_copied` / `send_zc_bytes` exist as poller
getters and `tests/reactor/test_metal_tier.py` asserts they stay zero.
`IORING_OP_SEND_ZC` is never submitted. The hard prerequisite is already
built -- `st_submit_send_op` retains the immutable payload under completion
ownership, which is exactly the notification-lifetime requirement. The plan's
own threshold is >=16KiB. Either wire it up or the counters mislead a reader
into thinking it is on.

### 4. Hand the single response straight to the send slot

`st_flush_cork` (`reactor_transport.c:664`) enqueues the corked payload and
`st_pump_egress` immediately dequeues it: `PyList_Append`, then
`PyList_GET_ITEM`, then `PyList_SetSlice(0, 1, NULL)` -- per response, for a
queue that holds one item. When the queue is empty and no SEND is in flight,
transfer the reference straight into `t->send_obj` and skip the list. `PyList_New`
0.53%, `PyList_Append` 0.44%, `list_dealloc` 0.54%. Keep `send_queued_bytes`
accounting identical; the watermark logic reads it.

### 5. `rp_run_task_step` calls `context.run(callback)`

`reactor_poller.c:1405` does `PyObject_CallMethodOneArg(context, "run",
callback)` per task resume -- a method lookup plus a Python-level call.
`PyContext_Enter` / `PyContext_Exit` around a direct vectorcall is the same
semantics without the lookup. Small, contained, and it is on every suspension.

### 6. Interned statics on the accept path

`st_init` (`reactor_transport.c:1451`) uses `PyObject_GetAttrString(loop,
"_poller")`, `PyObject_CallMethod(server, "_attach", "O", op)` and
`st_bound(protocol, "connection_made")`; `PyDict_SetItemString(t->extra,
"socket", sock)` is the same shape. Each mints a fresh `str` per connection.
Module-level interned constants remove roughly four unicode allocations per
connection. Worth doing in the same change as anything else on this path, not
on its own.

### 7. Framework-private slots do not belong in `request.state`

The built-in middleware tape stores its own keys (`_STATE_START`,
`_STATE_ELAPSED`, `_STATE_KEY`, `_STATE_TOKEN`, `_STATE_ISSUE`, `_STATE_MINTER`)
in the user-visible `State`. Each access is three hops: the `state` property
(`request.py:561`), then `State.__setattr__`/`State.get`, then a dict operation.
On the `wreath-request-trace` realistic app that is **23 Python frames per
request** -- `request.state` x10, `State.__setattr__` x7, `State.get` x6 -- for
values no user names. Move them to `Request` slots (or the request context) and
have `State` merge them only when user code reads `state`. The observable to
preserve: a handler that reads `request.state.route_outcome` today must keep
seeing it, and `dir()`-style introspection of `State` is not part of the
contract.

### 8. HTTP/2 does not use the transport fusion CAPI

`server_http2.c:405` writes every frame through
`PyObject_CallOneArg(self->transport_write_fn, chunk)`, while HTTP/1 goes
through `transport_capi->write` (`server_http1.c`, `load_transport_capi`).
Dormant today -- `EventLoop._start_serving` only takes the uring listener when
`sslcontext is None`, and h2 requires TLS ALPN -- so this costs nothing now. It
becomes a hole the day h2 reaches metal, which is why it is written down here
rather than discovered then.

## What is deliberately not in this document

Anything whose payoff could not be reproduced across both captures, and anything
whose only evidence was a throughput A/B on the stdlib generator. Several
plausible items were dropped for the second reason; they are not disproved, they
are unmeasured, and re-running them on an independent generator is the way to
bring them back.
