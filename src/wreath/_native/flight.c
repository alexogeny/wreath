/* Native Flight Recorder — Stage 1 native core.
 *
 * Single-writer (event-loop thread) SPSC ring, worker counters, log2 histograms,
 * and a free-list active-request table. The request path never allocates, locks,
 * or blocks: publication is one capacity check then a release store, and every
 * drop increments exactly one bounded loss counter.
 */
#include "flight.h"

#include <stdlib.h>
#include <string.h>

#if defined(__linux__)
#include <sys/random.h>
#endif

#define CACHELINE 64

/* --- id generation -------------------------------------------------------- */
/* A per-worker splitmix64 stream for span/trace ids. Seeded once, off the
 * request path, from the OS CSPRNG when available. Not a cryptographic stream
 * itself; ADR 0021 tracks upgrading to a refilled CSPRNG pool. */
static uint64_t
splitmix64(uint64_t *state)
{
    uint64_t z = (*state += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

/* Stateless splitmix64 finalizer: a deterministic avalanche of one value, used
 * to draw a per-request Detailed-arming sample from the request id. Being a pure
 * function of the id (not the worker rng stream), the same request always makes
 * the same arming decision, and it does not perturb span/trace id generation.
 * The pure oracle mirrors this exactly. */
static uint64_t
mix64(uint64_t x)
{
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static uint64_t
seed_entropy(void)
{
    uint64_t seed = 0;
#if defined(__linux__)
    if (getrandom(&seed, sizeof(seed), 0) == (ssize_t)sizeof(seed) && seed != 0) {
        return seed;
    }
#endif
    /* Fallback: address + a monotonic reading. Never a request-path syscall. */
    seed = (uint64_t)(uintptr_t)&seed ^ 0xD1B54A32D192ED03ULL;
    return seed ? seed : 0x1234567890ABCDEFULL;
}

/* --- strict W3C traceparent parsing --------------------------------------- */
/* "00-<32 hex trace>-<16 hex parent>-<2 hex flags>" (55 bytes). Rejects any
 * malformed field, an unknown/invalid version, and all-zero trace or parent
 * ids, without allocating or reflecting the input. Returns 0 on success. */
static int
hex_nibble(uint8_t c, uint8_t *out)
{
    if (c >= '0' && c <= '9') { *out = (uint8_t)(c - '0'); return 0; }
    if (c >= 'a' && c <= 'f') { *out = (uint8_t)(c - 'a' + 10); return 0; }
    return -1;  /* uppercase and everything else are invalid per the spec */
}

static int
hex_to_u64(const uint8_t *data, int nchars, uint64_t *out)
{
    uint64_t value = 0;
    for (int i = 0; i < nchars; i++) {
        uint8_t nib;
        if (hex_nibble(data[i], &nib) < 0) {
            return -1;
        }
        value = (value << 4) | nib;
    }
    *out = value;
    return 0;
}

int
wreath_nfr_parse_traceparent(const uint8_t *data, Py_ssize_t len, uint64_t *trace_hi,
                             uint64_t *trace_lo, uint64_t *parent_span,
                             uint8_t *sampled)
{
    if (len != 55 || data[2] != '-' || data[35] != '-' || data[52] != '-') {
        return -1;
    }
    uint8_t v0, v1;
    if (hex_nibble(data[0], &v0) < 0 || hex_nibble(data[1], &v1) < 0) {
        return -1;
    }
    uint8_t version = (uint8_t)((v0 << 4) | v1);
    if (version == 0xFF) {  /* forbidden version */
        return -1;
    }
    uint64_t hi, lo, span;
    uint8_t flags_hi, flags_lo;
    if (hex_to_u64(data + 3, 16, &hi) < 0 || hex_to_u64(data + 19, 16, &lo) < 0 ||
        hex_to_u64(data + 36, 16, &span) < 0 ||
        hex_nibble(data[53], &flags_hi) < 0 || hex_nibble(data[54], &flags_lo) < 0) {
        return -1;
    }
    if ((hi == 0 && lo == 0) || span == 0) {  /* all-zero ids are invalid */
        return -1;
    }
    *trace_hi = hi;
    *trace_lo = lo;
    *parent_span = span;
    *sampled = (uint8_t)(((flags_hi << 4) | flags_lo) & 0x01);
    return 0;
}

/* The worker has cache-line-aligned (_Alignas(64)) members, so its allocation
 * must be 64-byte aligned -- PyMem_Calloc only promises 16. Use aligned_alloc
 * for the worker itself; its sub-buffers have natural alignment and stay on
 * PyMem. This is a control-plane allocation, not a request-path one. */
static void *
aligned_zalloc(size_t size)
{
    size_t rounded = (size + CACHELINE - 1) & ~((size_t)CACHELINE - 1);
    void *memory = aligned_alloc(CACHELINE, rounded);
    if (memory != NULL) {
        memset(memory, 0, rounded);
    }
    return memory;
}

/* One active-request slot. `generation` is a seqlock: even = stable, odd = being
 * written; a reader (Stage 3 Inspector) retries while it is odd or changed. */
typedef struct {
    _Atomic uint32_t generation;
    uint64_t request_id;
    uint64_t start_ns;
    uint32_t route_id;
    uint8_t protocol;
    uint8_t in_use;
} wreath_nfr_active_slot;

struct wreath_nfr_worker {
    uint8_t mode;
    int completion_summaries;
    uint32_t worker_id;

    /* request id source (single writer, no atomics needed for the counter). */
    uint64_t next_request_id;

    /* span/trace id generation stream (single writer). */
    uint64_t rng_state;

    /* Detailed-mode arming: a request is armed when a 32-bit draw from the
     * finalizer of its request id is below this threshold. threshold =
     * round(rate * 2^32), so 0 arms none and 2^32 arms all. Only consulted in
     * DETAILED/FORENSIC mode; Pulse never arms. */
    uint64_t detailed_sample_threshold;

    /* Detailed-mode promotion: a completion slower than this (microseconds) is
     * flagged SLOW_PROMOTED; an errored/timed-out one is flagged ERROR_PROMOTED.
     * 0 disables the latency trigger. Promotion flags the completion cell only --
     * phases cannot be recovered retroactively. Consulted in DETAILED/FORENSIC. */
    uint64_t slow_threshold_us;

    /* --- SPSC ring --- */
    uint8_t *ring;            /* ring_records * CELL_SIZE bytes */
    uint32_t ring_records;    /* power of two, or 0 */
    uint32_t ring_mask;
    _Alignas(CACHELINE) _Atomic uint64_t ring_head;  /* writer publishes */
    _Alignas(CACHELINE) _Atomic uint64_t ring_tail;  /* reader consumes */
    _Atomic uint64_t ring_high_water;

    /* --- counters (cache-line separated from the ring indices) --- */
    _Alignas(CACHELINE) _Atomic uint64_t requests;
    _Atomic uint64_t completions;
    wreath_nfr_losses losses;

    /* --- histograms --- */
    uint64_t *histograms;     /* histogram_count * HISTOGRAM_BUCKETS */
    uint32_t histogram_count;

    /* --- active table --- */
    wreath_nfr_active_slot *active;
    uint32_t active_capacity;
    uint32_t *free_stack;
    uint32_t free_top;
    _Atomic uint64_t active_count;

    /* --- phase scratch pool (Detailed mode; armed requests only) ---
     * A free-list of fixed scratch blocks, each holding one request's phase
     * records already laid out as ring-ready 64-byte batch cells, so commit is a
     * straight cell copy. Sized to concurrent *armed* requests (a sample), not to
     * total concurrency, so Off/Pulse and unarmed streams reserve nothing. */
    uint8_t *phase_scratch;     /* phase_capacity * PHASE_BLOCK_BYTES, or NULL */
    uint32_t phase_capacity;    /* number of scratch blocks */
    uint32_t *phase_free_stack;
    uint32_t phase_free_top;
};

/* One scratch block is a whole number of 64-byte batch cells covering the phase
 * budget: BUDGET / RECORDS_PER_BATCH cells. Records are written straight into
 * batch-cell layout so committing is a cell-by-cell ring copy with no repacking. */
#define PHASE_BLOCK_CELLS (WREATH_NFR_PHASE_CELL_BUDGET / WREATH_NFR_PHASE_RECORDS_PER_BATCH)
#define PHASE_BLOCK_BYTES ((size_t)PHASE_BLOCK_CELLS * WREATH_NFR_CELL_SIZE)

static void
note_loss(wreath_nfr_worker *worker, int reason)
{
    if (reason >= 0 && reason < WREATH_NFR_LOSS_REASON_COUNT) {
        atomic_fetch_add_explicit(&worker->losses.reason[reason], 1,
                                  memory_order_relaxed);
    }
}

/* --- lifecycle ------------------------------------------------------------ */

wreath_nfr_worker *
wreath_nfr_worker_new(uint8_t mode, uint32_t worker_id, uint32_t ring_records,
                      uint32_t active_requests, uint32_t histogram_count,
                      int completion_summaries, uint64_t detailed_sample_threshold,
                      uint32_t phase_slots, uint64_t slow_threshold_us)
{
    if (ring_records != 0 && (ring_records & (ring_records - 1)) != 0) {
        PyErr_SetString(PyExc_ValueError, "ring_records must be a power of two");
        return NULL;
    }
    if (histogram_count == 0) {
        histogram_count = 1;  /* always at least the global histogram */
    }
    wreath_nfr_worker *worker = aligned_zalloc(sizeof(*worker));
    if (worker == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    worker->mode = mode;
    worker->completion_summaries = completion_summaries;
    worker->worker_id = worker_id;
    worker->next_request_id = 1;
    worker->rng_state = seed_entropy();
    worker->detailed_sample_threshold = detailed_sample_threshold;
    worker->slow_threshold_us = slow_threshold_us;
    worker->ring_records = ring_records;
    worker->ring_mask = ring_records ? ring_records - 1 : 0;
    worker->histogram_count = histogram_count;
    atomic_init(&worker->ring_head, 0);
    atomic_init(&worker->ring_tail, 0);
    atomic_init(&worker->ring_high_water, 0);
    atomic_init(&worker->requests, 0);
    atomic_init(&worker->completions, 0);
    atomic_init(&worker->active_count, 0);
    for (int i = 0; i < WREATH_NFR_LOSS_REASON_COUNT; i++) {
        atomic_init(&worker->losses.reason[i], 0);
    }

    if (ring_records) {
        worker->ring = PyMem_Calloc((size_t)ring_records, WREATH_NFR_CELL_SIZE);
        if (worker->ring == NULL) {
            goto no_memory;
        }
    }
    worker->histograms =
        PyMem_Calloc((size_t)histogram_count * WREATH_NFR_HISTOGRAM_BUCKETS,
                     sizeof(uint64_t));
    if (worker->histograms == NULL) {
        goto no_memory;
    }
    if (active_requests) {
        worker->active_capacity = active_requests;
        worker->active = PyMem_Calloc(active_requests, sizeof(*worker->active));
        worker->free_stack = PyMem_Calloc(active_requests, sizeof(uint32_t));
        if (worker->active == NULL || worker->free_stack == NULL) {
            goto no_memory;
        }
        for (uint32_t i = 0; i < active_requests; i++) {
            /* newest-first so slot 0 is handed out first. */
            worker->free_stack[i] = active_requests - 1 - i;
            atomic_init(&worker->active[i].generation, 0);
        }
        worker->free_top = active_requests;
    }
    /* The phase scratch pool is only meaningful in a mode that arms phases. */
    if (phase_slots && mode >= WREATH_NFR_MODE_DETAILED) {
        worker->phase_capacity = phase_slots;
        worker->phase_scratch = PyMem_Calloc(phase_slots, PHASE_BLOCK_BYTES);
        worker->phase_free_stack = PyMem_Calloc(phase_slots, sizeof(uint32_t));
        if (worker->phase_scratch == NULL || worker->phase_free_stack == NULL) {
            goto no_memory;
        }
        for (uint32_t i = 0; i < phase_slots; i++) {
            worker->phase_free_stack[i] = phase_slots - 1 - i;
        }
        worker->phase_free_top = phase_slots;
    }
    return worker;

no_memory:
    wreath_nfr_worker_free(worker);
    PyErr_NoMemory();
    return NULL;
}

void
wreath_nfr_worker_free(wreath_nfr_worker *worker)
{
    if (worker == NULL) {
        return;
    }
    PyMem_Free(worker->ring);
    PyMem_Free(worker->histograms);
    PyMem_Free(worker->active);
    PyMem_Free(worker->free_stack);
    PyMem_Free(worker->phase_scratch);
    PyMem_Free(worker->phase_free_stack);
    free(worker);  /* paired with aligned_alloc in wreath_nfr_worker_new */
}

uint8_t
wreath_nfr_worker_mode(const wreath_nfr_worker *worker)
{
    return worker ? worker->mode : WREATH_NFR_MODE_OFF;
}

/* --- active table (writer-owned) ------------------------------------------ */

static int32_t
active_reserve(wreath_nfr_worker *worker, uint64_t request_id, uint64_t start_ns,
               uint8_t protocol)
{
    if (worker->free_top == 0) {
        note_loss(worker, WREATH_NFR_LOSS_ACTIVE_TABLE_FULL);
        return -1;
    }
    uint32_t slot = worker->free_stack[--worker->free_top];
    wreath_nfr_active_slot *entry = &worker->active[slot];
    /* seqlock: odd during the write, even (published) after. */
    uint32_t gen = atomic_load_explicit(&entry->generation, memory_order_relaxed);
    atomic_store_explicit(&entry->generation, gen + 1, memory_order_release);
    entry->request_id = request_id;
    entry->start_ns = start_ns;
    entry->protocol = protocol;
    entry->route_id = 0;
    entry->in_use = 1;
    atomic_store_explicit(&entry->generation, gen + 2, memory_order_release);
    atomic_fetch_add_explicit(&worker->active_count, 1, memory_order_relaxed);
    return (int32_t)slot;
}

static void
active_release(wreath_nfr_worker *worker, int32_t slot)
{
    if (slot < 0 || (uint32_t)slot >= worker->active_capacity) {
        return;
    }
    wreath_nfr_active_slot *entry = &worker->active[slot];
    uint32_t gen = atomic_load_explicit(&entry->generation, memory_order_relaxed);
    atomic_store_explicit(&entry->generation, gen + 1, memory_order_release);
    entry->in_use = 0;
    entry->request_id = 0;
    atomic_store_explicit(&entry->generation, gen + 2, memory_order_release);
    if (worker->free_top < worker->active_capacity) {
        worker->free_stack[worker->free_top++] = (uint32_t)slot;
    }
    atomic_fetch_add_explicit(&worker->active_count, (uint64_t)-1,
                              memory_order_relaxed);
}

/* --- phase scratch pool (writer-owned, armed requests only) --------------- */

/* Reserve a scratch block for an armed request. Returns its index, or -1 when the
 * pool is exhausted (counted as PHASE_SCRATCH_FULL: the request stays armed but
 * records no phases). No atomics: the writer owns the free-list, like the ring. */
static int32_t
phase_reserve(wreath_nfr_worker *worker)
{
    if (worker->phase_free_top == 0) {
        note_loss(worker, WREATH_NFR_LOSS_PHASE_SCRATCH_FULL);
        return -1;
    }
    return (int32_t)worker->phase_free_stack[--worker->phase_free_top];
}

static void
phase_release(wreath_nfr_worker *worker, int32_t slot)
{
    if (slot < 0 || (uint32_t)slot >= worker->phase_capacity) {
        return;
    }
    if (worker->phase_free_top < worker->phase_capacity) {
        worker->phase_free_stack[worker->phase_free_top++] = (uint32_t)slot;
    }
}

/* The batch cell a given record index lands in, within a reserved block. */
static wreath_nfr_phase_batch_cell *
phase_block_batch(wreath_nfr_worker *worker, int32_t slot, uint32_t batch_index)
{
    uint8_t *block = worker->phase_scratch + (size_t)slot * PHASE_BLOCK_BYTES;
    return (wreath_nfr_phase_batch_cell *)(block + (size_t)batch_index * WREATH_NFR_CELL_SIZE);
}

static int ring_publish(wreath_nfr_worker *worker, const void *cell);

/* Commit an armed request's phase batches, then return its scratch block. Phases
 * are published only when the completion cell they belong to made it into the
 * ring, so a phase batch is never an orphan (a dropped completion drops its
 * phases, counted by the ring). A no-op for unarmed requests. */
static void
phase_finish(wreath_nfr_worker *worker, wreath_nfr_context *ctx, int completion_published)
{
    if (ctx->phase_slot < 0) {
        return;
    }
    if (completion_published && ctx->phase_count > 0) {
        uint32_t batches = ((uint32_t)ctx->phase_count + WREATH_NFR_PHASE_RECORDS_PER_BATCH - 1) /
                           WREATH_NFR_PHASE_RECORDS_PER_BATCH;
        for (uint32_t b = 0; b < batches; b++) {
            ring_publish(worker, phase_block_batch(worker, ctx->phase_slot, b));
        }
    }
    phase_release(worker, ctx->phase_slot);
    ctx->phase_slot = -1;
}

/* --- ring (single writer / single reader) --------------------------------- */

/* Publish one 64-byte cell. Returns 1 on success, 0 when the ring is full
 * (loss counted). The writer checks capacity once; a full ring never blocks. */
static int
ring_publish(wreath_nfr_worker *worker, const void *cell)
{
    if (worker->ring_records == 0) {
        note_loss(worker, WREATH_NFR_LOSS_RING_FULL);
        return 0;
    }
    uint64_t head = atomic_load_explicit(&worker->ring_head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&worker->ring_tail, memory_order_acquire);
    uint64_t occupancy = head - tail;
    if (occupancy >= worker->ring_records) {
        note_loss(worker, WREATH_NFR_LOSS_RING_FULL);
        return 0;
    }
    uint32_t index = (uint32_t)(head & worker->ring_mask);
    memcpy(worker->ring + (size_t)index * WREATH_NFR_CELL_SIZE, cell,
           WREATH_NFR_CELL_SIZE);
    atomic_store_explicit(&worker->ring_head, head + 1, memory_order_release);
    uint64_t new_occupancy = occupancy + 1;
    uint64_t hw = atomic_load_explicit(&worker->ring_high_water, memory_order_relaxed);
    if (new_occupancy > hw) {
        atomic_store_explicit(&worker->ring_high_water, new_occupancy,
                              memory_order_relaxed);
    }
    return 1;
}

Py_ssize_t
wreath_nfr_ring_drain(wreath_nfr_worker *worker, uint8_t *out, Py_ssize_t max_cells)
{
    if (worker->ring_records == 0 || max_cells <= 0) {
        return 0;
    }
    uint64_t tail = atomic_load_explicit(&worker->ring_tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&worker->ring_head, memory_order_acquire);
    Py_ssize_t copied = 0;
    while (tail != head && copied < max_cells) {
        uint32_t index = (uint32_t)(tail & worker->ring_mask);
        memcpy(out + (size_t)copied * WREATH_NFR_CELL_SIZE,
               worker->ring + (size_t)index * WREATH_NFR_CELL_SIZE,
               WREATH_NFR_CELL_SIZE);
        tail++;
        copied++;
    }
    atomic_store_explicit(&worker->ring_tail, tail, memory_order_release);
    return copied;
}

/* --- request path --------------------------------------------------------- */

void
wreath_nfr_context_start(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                         uint64_t connection_id, uint8_t protocol, uint64_t start_ns)
{
    if (worker == NULL || worker->mode == WREATH_NFR_MODE_OFF) {
        ctx->mode = WREATH_NFR_MODE_OFF;
        return;
    }
    memset(ctx, 0, sizeof(*ctx));
    ctx->mode = worker->mode;
    ctx->protocol = protocol;
    ctx->connection_id = connection_id;
    ctx->start_ns = start_ns;
    ctx->phase_slot = -1;  /* 0 is a valid slot; unarmed requests hold none */
    ctx->request_id = worker->next_request_id++;
    ctx->span_id = splitmix64(&worker->rng_state);  /* this request's span */
    /* Detailed-mode arming: a cheap, deterministic per-request sample. Pulse
     * never arms, so this branch and its flag are absent from the Pulse cell. An
     * armed request reserves a phase-scratch block (or drops phases + counts the
     * loss if the pool is exhausted). */
    if (worker->mode >= WREATH_NFR_MODE_DETAILED &&
        (mix64(ctx->request_id) & 0xFFFFFFFFULL) < worker->detailed_sample_threshold) {
        ctx->flags |= WREATH_NFR_FLAG_DETAILED_ARMED;
        ctx->phase_slot = phase_reserve(worker);
    }
    atomic_fetch_add_explicit(&worker->requests, 1, memory_order_relaxed);
    ctx->active_slot = active_reserve(worker, ctx->request_id, start_ns, protocol);
    if (ctx->active_slot < 0) {
        ctx->flags |= WREATH_NFR_FLAG_TELEMETRY_LOSS;
    }
}

void
wreath_nfr_context_route(wreath_nfr_context *ctx, uint32_t route_id, uint32_t plan_id)
{
    if (ctx->mode == WREATH_NFR_MODE_OFF) {
        return;
    }
    ctx->route_id = route_id;
    ctx->plan_id = plan_id;
}

void
wreath_nfr_context_phase(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                         uint16_t phase_id, uint16_t dependency_id, uint8_t coverage,
                         uint32_t start_offset_us, uint32_t duration_us)
{
    /* Only an armed request that holds a scratch block records phases. The common
     * (unarmed / Pulse / Off) path is a single predicted branch on phase_slot. */
    if (ctx->phase_slot < 0) {
        return;
    }
    if (ctx->phase_count >= WREATH_NFR_PHASE_CELL_BUDGET) {
        note_loss(worker, WREATH_NFR_LOSS_PHASE_SCRATCH_FULL);
        return;
    }
    uint32_t index = ctx->phase_count;
    uint32_t batch_index = index / WREATH_NFR_PHASE_RECORDS_PER_BATCH;
    uint32_t slot_in_batch = index % WREATH_NFR_PHASE_RECORDS_PER_BATCH;
    wreath_nfr_phase_batch_cell *batch =
        phase_block_batch(worker, ctx->phase_slot, batch_index);
    if (slot_in_batch == 0) {
        /* First record of a batch: zero the whole 64-byte cell so any unused
         * trailing record slots (this request may not fill all three) never leak
         * stale bytes from a prior request that held this recycled scratch block,
         * then lay down the self-identifying header. Commit is then a straight
         * copy and partial batches are clean and reproducible. */
        memset(batch, 0, WREATH_NFR_CELL_SIZE);
        batch->schema_version = WREATH_NFR_SCHEMA_VERSION;
        batch->kind = WREATH_NFR_KIND_PHASE;
        batch->worker_id = (uint8_t)worker->worker_id;
        batch->request_id = ctx->request_id;
    }
    wreath_nfr_phase_cell *record = &batch->records[slot_in_batch];
    record->phase_id = phase_id;
    record->dependency_id = dependency_id;
    record->coverage = coverage;
    record->sequence = (uint8_t)index;
    record->reserved = 0;
    record->start_offset_us = start_offset_us;
    record->duration_us = duration_us;
    batch->count = (uint8_t)(slot_in_batch + 1);
    ctx->phase_count++;
}

void
wreath_nfr_context_propagate(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                             const uint8_t *traceparent, Py_ssize_t len)
{
    if (worker == NULL || ctx->mode == WREATH_NFR_MODE_OFF) {
        return;
    }
    uint64_t hi, lo, parent;
    uint8_t sampled;
    if (wreath_nfr_parse_traceparent(traceparent, len, &hi, &lo, &parent, &sampled) < 0) {
        note_loss(worker, WREATH_NFR_LOSS_PROPAGATION_INVALID);
        return;  /* malformed input is dropped, never reflected */
    }
    ctx->trace_id_hi = hi;
    ctx->trace_id_lo = lo;
    ctx->parent_span_id = parent;
    /* ctx->span_id was generated at start; it is this request's child span. */
    ctx->flags |= WREATH_NFR_FLAG_PROPAGATION_VALID | WREATH_NFR_FLAG_HAS_CORRELATION;
    if (sampled) {
        ctx->flags |= WREATH_NFR_FLAG_SAMPLED;
    }
}

void
wreath_nfr_context_end(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                       uint64_t now_ns, uint32_t status, uint8_t terminal,
                       uint8_t error_class, uint64_t bytes_in, uint64_t bytes_out)
{
    if (worker == NULL || ctx->mode == WREATH_NFR_MODE_OFF) {
        return;
    }
    uint64_t duration_ns = now_ns >= ctx->start_ns ? now_ns - ctx->start_ns : 0;
    uint64_t duration_us = duration_ns / 1000;

    /* Detailed-mode promotion: flag a slow or failed completion as worth
     * attention. This only sets bits on the cell that is emitted anyway (no ring
     * cost, no crossing) and cannot recover phases that were never armed. Pulse
     * leaves the flags clear, keeping its cells byte-identical to Stage 2. */
    if (ctx->mode >= WREATH_NFR_MODE_DETAILED) {
        if (terminal == WREATH_NFR_TERM_ERROR || terminal == WREATH_NFR_TERM_TIMEOUT) {
            ctx->flags |= WREATH_NFR_FLAG_ERROR_PROMOTED;
        }
        if (worker->slow_threshold_us != 0 && duration_us >= worker->slow_threshold_us) {
            ctx->flags |= WREATH_NFR_FLAG_SLOW_PROMOTED;
        }
    }

    atomic_fetch_add_explicit(&worker->completions, 1, memory_order_relaxed);
    int bucket = wreath_nfr_histogram_bucket(duration_us);
    worker->histograms[bucket]++;  /* global histogram, writer-owned */

    if (ctx->active_slot >= 0) {
        active_release(worker, ctx->active_slot);
        ctx->active_slot = -1;
    }

    if (!worker->completion_summaries) {
        /* No completion cell to anchor phases to: drop them, but still return the
         * scratch block so the pool is not leaked. */
        phase_finish(worker, ctx, 0);
        return;
    }
    /* The cell is exactly 64 bytes with no padding (static-asserted in the
     * schema header), so assigning every field -- reserved included -- fully
     * initializes it and skips a per-request memset. */
    wreath_nfr_completion_cell cell;
    cell.schema_version = WREATH_NFR_SCHEMA_VERSION;
    cell.kind = WREATH_NFR_KIND_COMPLETION;
    cell.flags = ctx->flags;
    cell.status = status;
    cell.request_id = ctx->request_id;
    cell.connection_id = ctx->connection_id;
    cell.route_id = ctx->route_id;
    cell.plan_id = ctx->plan_id;
    cell.duration_us = duration_us;
    cell.bytes_in = bytes_in;
    cell.bytes_out = bytes_out;
    cell.protocol = ctx->protocol;
    cell.terminal = terminal;
    cell.error_class = error_class;
    cell.worker_id = (uint8_t)worker->worker_id;
    cell.reserved = 0;  /* the only field not carrying data; must be zeroed */
    int published = ring_publish(worker, &cell);
    /* A paired correlation cell carries the 128-bit trace when propagation was
     * received (never a variable-length record). */
    if (published && (ctx->flags & WREATH_NFR_FLAG_HAS_CORRELATION)) {
        wreath_nfr_correlation_cell corr;
        memset(&corr, 0, sizeof(corr));
        corr.schema_version = WREATH_NFR_SCHEMA_VERSION;
        corr.kind = WREATH_NFR_KIND_CORRELATION;
        corr.flags = ctx->flags;
        corr.request_id = ctx->request_id;
        corr.trace_id_hi = ctx->trace_id_hi;
        corr.trace_id_lo = ctx->trace_id_lo;
        corr.parent_span_id = ctx->parent_span_id;
        corr.span_id = ctx->span_id;
        ring_publish(worker, &corr);  /* a dropped correlation is counted by the ring */
    }
    /* Detailed phase batches follow the completion they belong to (armed only). */
    phase_finish(worker, ctx, published);
}

void
wreath_nfr_context_abandon(wreath_nfr_worker *worker, wreath_nfr_context *ctx)
{
    if (worker == NULL || ctx->mode == WREATH_NFR_MODE_OFF) {
        return;
    }
    if (ctx->active_slot >= 0) {
        active_release(worker, ctx->active_slot);
        ctx->active_slot = -1;
    }
    /* Return an armed request's scratch block without committing anything. */
    phase_finish(worker, ctx, 0);
    ctx->mode = WREATH_NFR_MODE_OFF;  /* idempotent: a later end() is a no-op */
}

/* --- snapshots ------------------------------------------------------------ */

uint64_t
wreath_nfr_counter_requests(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->requests, memory_order_relaxed);
}

uint64_t
wreath_nfr_counter_completions(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->completions, memory_order_relaxed);
}

uint64_t
wreath_nfr_loss(const wreath_nfr_worker *worker, int reason)
{
    if (reason < 0 || reason >= WREATH_NFR_LOSS_REASON_COUNT) {
        return 0;
    }
    return atomic_load_explicit(&worker->losses.reason[reason], memory_order_relaxed);
}

uint64_t
wreath_nfr_ring_occupancy(const wreath_nfr_worker *worker)
{
    uint64_t head = atomic_load_explicit(&worker->ring_head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&worker->ring_tail, memory_order_relaxed);
    return head - tail;
}

uint64_t
wreath_nfr_ring_high_water(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->ring_high_water, memory_order_relaxed);
}

uint64_t
wreath_nfr_active_count(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->active_count, memory_order_relaxed);
}

void
wreath_nfr_histogram_global(const wreath_nfr_worker *worker, uint64_t *out)
{
    memcpy(out, worker->histograms, WREATH_NFR_HISTOGRAM_BUCKETS * sizeof(uint64_t));
}
