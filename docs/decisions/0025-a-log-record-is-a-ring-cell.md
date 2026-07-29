# 0025. A log record is a ring cell, not a logging subsystem

Date: 2026-07-28
Status: Accepted

## Context

Wreath shipped a Native Flight Recorder — a single-writer SPSC ring whose
publish is one capacity check and a release store, bounded loss counters with a
distinct reason per drop, an off-path projector thread that reassembles traces,
and OTLP, Prometheus, StatsD and CloudWatch EMF adapters behind a bounded export
queue. It did not ship first-class logging. The five stdlib `logging` call sites
in `src/wreath` were exception paths and the audit middleware.

Surveying how the fast loggers work — NanoLog's compile-time extraction of the
static half of a statement, Quill's hot-frontend/cold-backend split with an
explicit queue policy, Zap's per-call-site sampler, the failure-triggered
ring-buffer pattern Brian Marick wrote up in 2000, Stripe's canonical log line —
turned up an uncomfortable amount of overlap with what the recorder already
does. Wait-free publish: built. Bounded counted loss: built. Off-path
formatting thread: built. Trace correlation: *better* than built, because
`wreath_nfr_context` carries the trace and span ids inline on the request path,
where a general-purpose library would need a context lookup and an SDK object.

The genuinely missing pieces were small and specific: a message body, a severity,
an interned call-site table, a human-readable sink, and an OTLP logs mapping.

## Decision

Logging is not a subsystem. **A log record is a sixth `EventKind` on the ring
that already exists**, and the work is the missing pieces rather than a parallel
pipeline.

Consequences of that framing, each of which is a decision in its own right:

- **`EventKind.LOG`, one 64-byte cell.** Version in byte 0, kind in byte 1, like
  every other cell. It carries `site_id`, `request_id`, severity, an offset, a
  `dropped_siblings` count, and 32 bytes of packed arguments.
- **No trace or span id on the record.** The projector already joins a
  completion to its correlation carrier by request id; duplicating 24 bytes onto
  records that outnumber completions 10–100× would buy nothing.
- **The static half is interned at import.** Python has no preprocessor, so
  `log.event(...)` binds template, severity, field names, types and redaction
  dispositions once and returns a callable; the record carries `site_id` plus
  arguments. This is NanoLog's split with a different binding time.
- **Arguments stay self-describing** — one type tag each — even though the site
  declares their types. The extra byte buys a decode that validates a stale or
  torn record rather than trusting it.
- **Drop and count, never block and never grow.** A full ring, a full scratch
  buffer, a full writer queue, a full site table, a rate-limited record: five
  distinct `LossReason` values, because each one has a different operator
  response.
- **Deny-by-default redaction**, matching `wreath.recording`. Scalars verbatim;
  anything string-shaped fingerprinted with a keyed SipHash unless the site
  declares `RAW`.
- **Verbose records are failure-triggered.** TRACE and DEBUG accumulate in a
  per-request buffer and publish only on the recorder's existing
  `FLAG_ERROR_PROMOTED` / `FLAG_SLOW_PROMOTED`, or an explicit `promote()`.
- **`WARN` and above are never sampled.** The per-call-site limiter covers INFO
  and below only.
- **The framing is designed for a file-backed ring.** Every cell is versioned
  and every decode validates lengths against the buffer, so an mmap-backed
  forensic ring could be added without a format break. It since has been, and
  it needed a header in front of the cells rather than a re-framing -- which is
  what the discipline bought. Retrofitting it would have been a compatibility
  event on a format operators' tooling already reads.

Two things are deliberately excluded. **`wreath.audit` keeps its own path**,
because "never blocks the request path" and "never loses a record" are
incompatible promises and the audit trail needs the second one. **The stdlib
bridge is opt-in**, because installing a handler on the root logger fights
`dictConfig` and either double-emits or discards a user's configuration; the
cost of that restraint — two disjoint streams — is paired with
`doctor.check_logging_streams`, which names the loggers it applies to.

## Consequences

- Logging inherits the ring, the quiet-cycle settle, the loss accounting, the
  bounded export queue and the WFR1 container with no new concepts. Adding the
  OTLP logs signal was `build_logs_request` plus a third tick.
- `MemoryBudget` grows a `logging` component. It is an **estimate**, because it
  describes Python tables rather than a native reservation, and says so in its
  own docstring — a budget that silently omits a component is worse than one
  that names what it approximates. Its per-entry constants are measured, not
  guessed; three of the four original guesses were low.
- The registration tier is unfamiliar, and the kwargs tier is slower. Both are
  documented as such rather than one being quietly better.
- **The claim is now measured, and the shape this decision chose is what made
  the measurement actionable.** The emitter was Python when this was written and
  no claim was made; `benchmarks/bench_logging.py` has since run the six
  measurements the plan owed, and `wreath_nfr_log` packs a published record
  straight into a ring cell in C. A two-argument call costs 0.42 µs against
  structlog's 2.59 µs and stdlib's 2.85–3.88 µs; a *disabled* call — the premise
  of the whole promotion story — costs 0.07 µs, which is where that premise
  needed it to be. The swap was mechanical rather than a redesign because of
  three things decided here: a dense `site_id`, argument types declared at the
  call site, and a level check that precedes marshalling.
- **A buffered record is the exception, and it is expensive: 3.0 µs.** The
  promotion tier holds records as Python objects because they must survive until
  the request decides, so it did not move to C, and the shipped default buffers
  every DEBUG record. `docs/plans/first-class-logging.md` carries the
  decomposition and names the cause (frozen slots dataclass construction, which
  every cell type in `_flight_schema` shares) rather than leaving it to be
  rediscovered.
- **A record made off the loop cannot use the ring.** The single-writer rule
  this decision leans on is what makes the emitter cheap, and its cost is that a
  `wreath.jobs` worker's record takes a counted slow path — staged, published by
  the loop one interval later, flagged `LOG_FLAG_OFF_LOOP`. Before that existed,
  such a call raced the loop into `ring_publish` and corrupted the ring.
