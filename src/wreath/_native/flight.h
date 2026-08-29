/* Native Flight Recorder (NFR) — Stage 1 runtime API.
 *
 * The recorder "worker" owns one process/loop's telemetry state: a single-writer
 * SPSC ring, an active-request table, counters, log2 histograms, and bounded
 * loss accounting. Off performs no work; Pulse commits at most one completion
 * cell per request. Nothing here allocates, locks, or blocks on the request
 * path: the writer checks capacity once and either publishes or drops+counts.
 *
 * This header is the C runtime surface. The wire schema it emits is defined by
 * flight_schema.h (the Python-mirrored cell layouts).
 */
#ifndef WREATH_FLIGHT_H
#define WREATH_FLIGHT_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdatomic.h>
#include <stdint.h>

#include "flight_schema.h"

/* Capsule name for the versioned C API other extensions resolve once. */
#define WREATH_FLIGHT_CAPI_NAME "wreath._native._flight._C_API"
/* The C API the other native extensions resolve from the _flight capsule. All
 * the native extensions are built together from this header and check exact
 * equality, so this is an internal ABI guard against a stale mixed build, not a
 * compatibility promise to any external consumer. Bump it in lockstep only if a
 * later change reorders or removes a vtable entry. */
#define WREATH_FLIGHT_CAPI_VERSION 1

/* Capsule name wrapping a single recorder's wreath_nfr_worker* (no ownership). */
#define WREATH_FLIGHT_WORKER_CAPSULE "wreath._native._flight.worker"

/* Per-request context. Embedded inline in HTTP/1 request state and each
 * HTTP/2/3 stream (provisional bounds, not tuning targets). Off never initializes it; Pulse fills only
 * correlation, active state, route attribution, and completion fields. */
typedef struct {
    uint64_t request_id;
    uint64_t connection_id;
    uint64_t trace_id_hi;
    uint64_t trace_id_lo;
    uint64_t parent_span_id;
    uint64_t span_id;
    uint64_t start_ns;      /* monotonic start */
    uint32_t route_id;
    uint32_t plan_id;
    int32_t active_slot;    /* index into the active table, or -1 */
    int32_t phase_slot;     /* index into the phase-scratch pool, or -1 (armed only) */
    int32_t capture_slot;   /* index into the capture-slab pool, or -1 (Forensic armed) */
    uint32_t capture_used;  /* bytes written into the reserved slab (header + fields) */
    uint16_t flags;         /* WREATH_NFR_FLAG_* */
    uint8_t mode;           /* WREATH_NFR_MODE_* captured at start */
    uint8_t protocol;       /* WREATH_NFR_PROTO_* */
    uint8_t terminal;       /* WREATH_NFR_TERM_* */
    uint8_t error_class;
    uint8_t phase_count;    /* phase records written into the scratch block */
    uint16_t client_flags;  /* WREATH_NFR_CLIENT_*; zero until resolved */
    uint16_t user_agent_rule_id;
    uint8_t client_country[2];
} wreath_nfr_context;

/* Loss counters, one per LossReason. */
typedef struct {
    _Atomic uint64_t reason[WREATH_NFR_LOSS_REASON_COUNT];
} wreath_nfr_losses;

/* Opaque recorder worker; layout lives in flight.c. */
typedef struct wreath_nfr_worker wreath_nfr_worker;

/* Publish a pre-packed 64-byte cell into the worker's ring.
 *
 * The log emitter's provisional path: Python packs a WREATH_NFR_KIND_LOG cell
 * and hands it here, so records ride the same single-writer ring, the same
 * capacity check, and the same RING_FULL accounting as a completion. A native
 * emitter that packs the cell in C replaces the packing, not this publish.
 *
 * Returns 1 when published, 0 when the ring was full (counted as RING_FULL).
 * Must be called from the worker's own thread, like every other writer. */
int wreath_nfr_publish_cell(wreath_nfr_worker *worker, const void *cell);

/* SipHash-2-4 over `data`, the keyed redaction fingerprint. Exposed so the log
 * emitter can hash with the site registry's key -- the same key the Python
 * packer uses, so the two agree byte for byte. */
uint64_t wreath_nfr_fingerprint(const void *data, size_t len, uint64_t k0,
                                uint64_t k1);

/* --- worker lifecycle (control plane, not the request path) --------------- */

/* Create a worker. ring_records must be a power of two (or 0 for Off-only).
 * detailed_sample_threshold is round(rate * 2^32): the ceiling below which a
 * per-request 32-bit draw arms Detailed capture (0 arms none; only consulted in
 * DETAILED/FORENSIC mode).
 *
 * `ring_path`, when non-NULL, maps the ring from that file with MAP_SHARED
 * instead of allocating it, so the records survive a process that dies badly.
 * A path that cannot be opened or sized is a failure rather than a downgrade:
 * the caller asked for a forensic ring, and a server that quietly ran without
 * one would leave nothing to notice until the crash it was meant for.
 *
 * Returns NULL and sets a Python exception on failure. */
wreath_nfr_worker *wreath_nfr_worker_new(uint8_t mode, uint32_t worker_id,
                                         uint32_t ring_records,
                                         uint32_t active_requests,
                                         uint32_t histogram_count,
                                         int completion_summaries,
                                         uint64_t detailed_sample_threshold,
                                         uint32_t phase_slots,
                                         uint64_t slow_threshold_us,
                                         uint32_t capture_slabs,
                                         uint32_t slab_bytes,
                                         uint64_t hash_key0,
                                         uint64_t hash_key1,
                                         const char *ring_path);
void wreath_nfr_worker_free(wreath_nfr_worker *worker);
uint8_t wreath_nfr_worker_mode(const wreath_nfr_worker *worker);

/* --- request path (single writer = the event loop thread) ----------------- */

/* Begin a request. Off is a single predicted branch that zeroes the context and
 * returns. Otherwise assigns a request id, reserves an active slot (or notes a
 * loss), and stamps the monotonic start. */
void wreath_nfr_context_start(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                              uint64_t connection_id, uint8_t protocol,
                              uint64_t start_ns);

/* Attribute the selected route/plan. Safe to call once per request. */
void wreath_nfr_context_route(wreath_nfr_context *ctx, uint32_t route_id,
                              uint32_t plan_id);

/* Record one Detailed phase into the request's scratch block. A no-op unless the
 * request is armed and holds a scratch slot; a request that exceeds its phase
 * budget drops the phase and counts PHASE_SCRATCH_FULL. start_offset_us and
 * duration_us are microseconds relative to the request start. Single writer. */
void wreath_nfr_context_phase(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                              uint16_t phase_id, uint16_t dependency_id,
                              uint8_t coverage, uint32_t start_offset_us,
                              uint32_t duration_us);

/* Capture one policy-approved field into the request's forensic slab. A no-op
 * unless the request is Forensic-armed. The slab is reserved lazily on the first
 * captured field (a request that captures nothing costs no slab). The
 * disposition (WREATH_NFR_CAP_*) is enforced here as the bytes are written:
 * RAW copies up to the slab's remaining room (truncating with BODY_TRUNCATED);
 * HASHED writes an 8-byte keyed hash and never the bytes; MASKED/LENGTH retain
 * only the original length. `max_bytes` is a policy byte cap for RAW (0 = none):
 * a RAW field longer than it keeps a `max_bytes` prefix and records the true
 * original length as truncated, distinct from the slab-overflow bound.
 * Exhaustion counts CAPTURE_POOL_FULL. Single writer. */
void wreath_nfr_context_capture(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                                uint16_t field_class, uint16_t descriptor_id,
                                uint8_t disposition, const uint8_t *data,
                                Py_ssize_t len, uint32_t max_bytes);

/* Pop up to max_slabs committed capture slabs into `out` (a caller buffer of
 * max_slabs * slab_bytes), returning each slab's used_bytes in `lengths` and the
 * count of slabs copied. Each copied slab is returned to the free pool. Single
 * reader (the recording sink / tests). */
Py_ssize_t wreath_nfr_capture_drain(wreath_nfr_worker *worker, uint8_t *out,
                                    uint32_t *lengths, Py_ssize_t max_slabs);

/* The clock calibration captured at worker creation: the CLOCK_MONOTONIC_RAW and
 * Unix (ns) instants of the same moment. The projector maps a completion's
 * `end_offset_ms` to Unix time as epoch_unix_ns + end_offset_ms * 1e6. */
uint64_t wreath_nfr_worker_epoch_mono_ns(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_worker_epoch_unix_ns(const wreath_nfr_worker *worker);

uint64_t wreath_nfr_capture_capacity(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_capture_slab_bytes(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_capture_in_use(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_capture_high_water(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_capture_committed(const wreath_nfr_worker *worker);

/* Apply a W3C `traceparent`. Strictly parses it; a malformed value is dropped
 * (counted as PROPAGATION_INVALID) and never reflected. On success the context
 * carries the incoming trace/parent, keeps its generated child span, and a
 * correlation cell is emitted at completion. */
void wreath_nfr_context_propagate(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                                  const uint8_t *traceparent, Py_ssize_t len);

/* Strict W3C traceparent parse. Returns 0 on success. Exposed for testing. */
int wreath_nfr_parse_traceparent(const uint8_t *data, Py_ssize_t len,
                                 uint64_t *trace_hi, uint64_t *trace_lo,
                                 uint64_t *parent_span, uint8_t *sampled);

/* Complete a request: compute duration, update counters/histograms, publish at
 * most one completion cell (Pulse + completion_summaries), release the active
 * slot. `now_ns` is a monotonic timestamp from the caller's clock. */
void wreath_nfr_context_end(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                            uint64_t now_ns, uint32_t status, uint8_t terminal,
                            uint8_t error_class, uint64_t bytes_in,
                            uint64_t bytes_out);

/* Abandon a started-but-not-completed request (cancellation, connection loss,
 * teardown): release its active slot and emit nothing. Idempotent. */
void wreath_nfr_context_abandon(wreath_nfr_worker *worker, wreath_nfr_context *ctx);

/* --- reader / control plane ----------------------------------------------- */

/* Drain up to max_cells committed cells into out (max_cells * 64 bytes).
 * Returns the number of cells copied. Single reader (projector/tests). */
Py_ssize_t wreath_nfr_ring_drain(wreath_nfr_worker *worker, uint8_t *out,
                                 Py_ssize_t max_cells);

/* Snapshot scalar counters. */
uint64_t wreath_nfr_counter_requests(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_counter_completions(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_loss(const wreath_nfr_worker *worker, int reason);
uint64_t wreath_nfr_ring_occupancy(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_ring_high_water(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_active_count(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_phase_capacity(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_phase_in_use(const wreath_nfr_worker *worker);
uint64_t wreath_nfr_phase_high_water(const wreath_nfr_worker *worker);

/* One row of an active-table snapshot (Stage 3 Inspector). route_id is 0 until
 * asynchronous projection stamps live attribution (stage 4); completion cells
 * carry the authoritative route/plan ids. */
typedef struct {
    uint64_t request_id;
    uint64_t start_ns;
    uint32_t route_id;
    uint8_t protocol;
} wreath_nfr_active_entry;

uint64_t wreath_nfr_active_capacity(const wreath_nfr_worker *worker);
/* Copy up to `max` in-use active slots into `out`, seqlock-consistent per slot
 * (a slot mid-write is retried briefly, then skipped). Returns rows written. */
uint32_t wreath_nfr_active_snapshot(const wreath_nfr_worker *worker,
                                    wreath_nfr_active_entry *out, uint32_t max);

/* Copy the global histogram (HISTOGRAM_BUCKETS uint64 counters) into out. */
void wreath_nfr_histogram_global(const wreath_nfr_worker *worker, uint64_t *out);

/* --- capsule vtable other extensions resolve once ------------------------- */

typedef struct {
    int version;
    void (*context_start)(wreath_nfr_worker *, wreath_nfr_context *, uint64_t,
                          uint8_t, uint64_t);
    void (*context_route)(wreath_nfr_context *, uint32_t, uint32_t);
    void (*context_propagate)(wreath_nfr_worker *, wreath_nfr_context *,
                              const uint8_t *, Py_ssize_t);
    void (*context_end)(wreath_nfr_worker *, wreath_nfr_context *, uint64_t,
                        uint32_t, uint8_t, uint8_t, uint64_t, uint64_t);
    void (*context_abandon)(wreath_nfr_worker *, wreath_nfr_context *);
    uint8_t (*worker_mode)(const wreath_nfr_worker *);
    void (*context_phase)(wreath_nfr_worker *, wreath_nfr_context *, uint16_t,
                          uint16_t, uint8_t, uint32_t, uint32_t);
    void (*context_capture)(wreath_nfr_worker *, wreath_nfr_context *, uint16_t,
                            uint16_t, uint8_t, const uint8_t *, Py_ssize_t,
                            uint32_t);
} WreathFlightCAPI;

#endif /* WREATH_FLIGHT_H */
