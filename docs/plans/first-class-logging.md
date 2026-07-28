# First-class logging

Status: stages 1–6 landed and wired into the server (Python emitter).
Stage 7 deferred by decision.
See [ADR 0025](../decisions/0025-a-log-record-is-a-ring-cell.md) for why logging
is a ring cell rather than a subsystem, and
[the guide](../guides/logging.md) for the user-facing story.

## Shape

```
call site (import)        registry: template, severity, types, dispositions
      |
handler (request path)    level check -> limiter -> pack -> publish a 64-byte cell
      |                                        \-> or buffer, if TRACE/DEBUG in a request
      v
projector thread          drain, join to trace by request_id, settle on a quiet cycle
      |
      +---> writer thread (bounded queue)   render -> text / JSON lines
      +---> export pipeline (bounded queue) -> build_logs_request -> OTLP /v1/logs
```

## Stages

| # | Stage | State |
| --- | --- | --- |
| 1 | `EventKind.LOG` cell, typed argument packing, severity, loss reasons, C mirror | landed |
| 2 | Interned call-site table; `log.event` tier and `log.info` tier | landed |
| 3 | Projector integration, writer thread, text and JSON renderers | landed |
| 4 | Failure-triggered promotion, per-call-site limiting | landed |
| 5 | Canonical log line (`scope.set` / `log.set_field`), `MemoryBudget.logging` | landed |
| 6 | `build_logs_request`, third export tick, stdlib bridge, doctor check | landed |
| — | Server wiring: per-request scope, recorder-backed sink, writer lifecycle | landed |
| — | HTTP/2, HTTP/3 and WebSocket scopes; full `LoggingConfig`; `request.event` | landed |
| 7 | mmap-backed forensic ring, binary archival stream, decoder | deferred |

Stage 7 is deferred but **not designed out**: the cell carries its schema version
in byte 0 and every decode validates lengths against the buffer, so a file-backed
ring can be added without a format break. That framing was a constraint on the
stage 1 commit, not a later task.

## Files

| Concern | File |
| --- | --- |
| Cell layout, severity, argument packing | `src/wreath/_flight_schema.py` |
| C mirror and static asserts | `src/wreath/_native/flight_schema.h` |
| Interned sites, packing, rendering | `src/wreath/_logsite.py` |
| Per-request buffering, per-site limiting | `src/wreath/_logscratch.py` |
| Renderers, writer thread, canonical line | `src/wreath/_logsink.py` |
| Public API | `src/wreath/logging.py` |
| Ring drain and trace join | `src/wreath/_projector.py` |
| OTLP logs mapping | `src/wreath/_otlp.py` |
| Export tick | `src/wreath/_export.py` |
| Fixed-size budgets | `src/wreath/telemetry.py` (`LoggingConfig`) |
| Split-stream check | `src/wreath/doctor.py` |
| Ring publish seam | `wreath_nfr_publish_cell` (`flight.c`), `Recorder.publish_log` |
| Per-request scope | `src/wreath/app.py` (`_handle_http` / `_finish_http`) |
| Runtime + writer lifecycle | `src/wreath/server.py` (`_create_logging`) |

## Not done, and why

**The emitter is Python.** The design keeps the shape a C emitter would take —
a dense `site_id`, declared argument types so packing can be branch-free, a
level check that precedes argument marshalling — but `wreath_nfr_log` does not
exist yet. Until it does, no performance claim should be made about this module,
and none is made in its docs.

**Nothing is benchmarked.** The measurements that matter, none of which have been
run:

1. `SITE(a, b)` against `logging.getLogger(...).warning(...)` and structlog, same
   machine, same run, distributions rather than means.
2. **The cost of a disabled `DEBUG(...)` call.** This is the load-bearing one:
   the promotion story assumes verbose instrumentation is affordable, and that
   assumption is exactly this number. If it comes back badly, the `if SITE:`
   guard stops being an escape hatch and becomes the documented idiom.
3. Ring publish cost for a `LOG` cell against a `COMPLETION` cell — they should
   be near-identical, and if they are not, the packing is wrong.
4. Projector drain throughput with log cells mixed in. Logs outnumber
   completions 10–100× and the projector was sized for completions.
5. End-to-end request latency with logging on and off, which is the only number
   a user actually cares about.
6. Whether `MemoryBudget.logging`'s per-entry constants resemble reality.

**The wiring is in.** `_handle_http` opens a scope keyed on the recorder's own
request id, `_finish_http` closes it with the promotion verdict, and
`_create_logging` installs a runtime whose sink publishes into the ring through
`Recorder.publish_log`. `tests/test_logging_live.py` drives it end to end over a
real socket: a record written in a handler comes out of the writer carrying the
trace and span ids the recorder generated.

Two properties that wiring had to preserve, both enforced by gates:

- **A recorder without logging adds no boundary crossings.** `wreath-request-trace`
  caught the first two attempts, which read the request id and the response
  status eagerly — both are calls into C. The request path now checks a plain
  module global (`logging._ACTIVE`) before touching either, so the baseline is
  unchanged.
- **Spans and records agree about a request's identity.** An unpropagated
  request has no correlation cell, and the synthesis for that case used to live
  only in `_otlp`. It is now `ProjectedTrace.effective_ids`, which both the span
  export and the log projection call — previously they could have disagreed.

**All four transports are wired.** HTTP/1 through the request-context object;
HTTP/2, HTTP/3 and a WebSocket session through the dict scope, where the
protocols seed the recorder's request id into `_wreath_flight` (one shared
helper, `wreath_request_scope_seed_flight`). A WebSocket session is one recorder
context, so it is one log scope for its whole life, promoted when the session
ends badly.

Seeding that id surfaced a defect worth remembering: an **Off** worker's
`context_start` sets `ctx->mode` and returns *without initializing the rest*, so
reading `request_id` there is uninitialized stack. `tests/http2/test_logging.py`
caught it publishing garbage ids; the helper now gates on `mode`, which is the
same discriminator `set_armed` relies on for the same reason.

**Off-loop emission takes the slow path by design.** The ring is single-writer.
`wreath.jobs` and thread-pool work get a counted slow path rather than per-thread
staging buffers, which would reintroduce cross-thread timestamp ordering as a
permanent tax. `LossReason.LOG_OFF_LOOP` is reserved for it; the slow path itself
is not built, because nothing yet emits from off the loop.
