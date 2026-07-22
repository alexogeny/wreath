# The native reactor suite — spec *and* checklist (RED until built)

This directory is the acceptance specification for the native C reactor
(`docs/plans/native-reactor.md`), written **test-first as red tests**. The
reactor does not exist yet, so **every test in here fails today** — that is the
point. Each test is one line of the spec; it turns red → green as its feature
lands. When the whole directory is green, the reactor is done.

```
uv run pytest tests/reactor -q     # today: all RED (native reactor absent)
```

Nothing here is skipped and nothing is green-by-default: a skipped or passing
line would hide unbuilt work. Until `wreath.reactor.new_event_loop()` exists,
every test fails on first use of the loop with a clear
`native reactor not built …` assertion (see `conftest._UnbuiltLoop`). Once the
loop constructs, each test fails instead at its own assertion until that
specific behaviour is implemented — so the failures become a granular burndown.

## asyncio is the oracle, not a passing backend

Many tests assert plain event-loop semantics. Rather than trust prose, they
compute the expected value by running the same scenario on a throwaway stock
`asyncio` loop (`support.asyncio_reference`) and assert the **native** loop
matches it. The asyncio run is scaffolding inside a red test — it never counts
as a passing row. The contract is therefore executable: *the native reactor is
a correct asyncio loop iff it reproduces asyncio's observable behaviour on every
scenario in this suite.*

Tests for behaviour unique to the native reactor — inline-drive of
synchronously-completing handlers, protocol→coroutine fusion, backend
selection — assert against `reactor_stats()` and have no asyncio oracle (asyncio
cannot satisfy them by design).

## Required native surface (the API this suite pins)

```python
import wreath.reactor as r

r.new_event_loop(backend: str | None = None) -> asyncio.AbstractEventLoop
r.available_backends() -> tuple[str, ...]      # e.g. ("epoll",) or ("epoll", "io_uring")

# Serve an ASGI app on a reactor loop (protocol-integration acceptance API):
r.serve(app, *, host, port, protocols, config, loop) -> ServerHandle
#   awaitable; ServerHandle has .host, .port, .udp_port and async .aclose()

# Native loops additionally expose introspection so fusion is observable:
loop.reactor_backend() -> str
loop.reactor_stats() -> dict   # monotonic counters, keys below
#   inline_completions  handlers finished on create_task without scheduling
#   fused_resumes       coro resumed directly from a protocol read callback
#   call_soon_scheduled entries pushed onto the ready queue
#   coro_steps          coro.send/throw invocations
#   poll_calls          blocking poll() syscalls
#   timers_fired        timer callbacks dispatched
#   tasks_promoted      suspended request coros promoted to full drivers
```

The loop must otherwise satisfy `asyncio.AbstractEventLoop` closely enough that
third-party `await`s work unchanged (`test_foreign_await.py`).

## Layout (every file RED until its stage lands)

Loop semantics (oracle-checked against asyncio):
- `test_loop_lifecycle.py`  — run_until_complete / run_forever / stop / close / is_running
- `test_callbacks.py`       — call_soon(_threadsafe) ordering, handles, cancel
- `test_timers.py`          — call_later / call_at ordering, cancel, time(); timing-wheel stress
- `test_futures.py`         — create_future, result/exception/cancel, done-callback scheduling
- `test_tasks.py`           — create_task, gather, cancellation, current_task/all_tasks
- `test_io_readiness.py`    — add_reader/writer, sock_recv/sendall/accept/connect
- `test_foreign_await.py`   — sleep, wait_for, gather, Lock/Event/Queue, run_in_executor, to_thread
- `test_signals_dns.py`     — add_signal_handler, getaddrinfo/getnameinfo (or documented non-goals)
- `test_errors.py`          — call_exception_handler, unhandled task exceptions

Native-only mechanism specs:
- `test_wreath_task.py`     — inline-drive, fused resume, foreign-await fallback, cancel, ctxvar isolation
- `test_poller_backends.py` — epoll ⇆ io_uring parity
- `test_tls.py`             — OpenSSL memory-BIO handshake/data/close, ALPN, SNI
- `test_backpressure.py`    — pause/resume reading & writing, high/low water

Protocol integration on the reactor (real loopback I/O):
- `test_reactor_h1.py`      — HTTP/1.1 request/response, keep-alive, pipelining, deadlines
- `test_reactor_h2.py`      — HTTP/2 preface, multiplexing, flow control, fused body, frame budget
- `test_reactor_h3.py`      — HTTP/3 QUIC handshake, streams, datagrams, timers, reset→cancel
- `test_parity.py`          — same ASGI app, identical observable behaviour asyncio vs native
- `test_cancellation.py`    — request coro cancellation on disconnect/timeout/shutdown (H1/H2/H3)
- `test_concurrency.py`     — multi-reactor workers, SO_REUSEPORT, GIL discipline under load

## Running

```bash
uv run pytest tests/reactor -q            # the whole spec; all RED today
uv run pytest tests/reactor -q -x         # stop at the first unmet line
uv run pytest tests/reactor/test_timers.py -q
```
