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
#include <time.h>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/random.h>
#endif

#define CACHELINE 64

/* --- id generation -------------------------------------------------------- */
/* A per-worker splitmix64 stream for span/trace ids. Seeded once, off the
 * request path, from the OS CSPRNG when available. Not a cryptographic stream
 * itself; `docs/plans/native-flight-recorder-stage-1.md` tracks upgrading to a refilled CSPRNG pool. */
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

/* --- keyed redaction hash ------------------------------------------------- */
/* SipHash-2-4 over a byte string with a 128-bit process-local key. Forensic
 * capture stores this fingerprint for a HASHED field: a stable, non-reversible
 * 64-bit value that correlates repeated occurrences without disclosing the
 * bytes. The key is seeded once from the OS CSPRNG at worker creation. Word
 * loads are little-endian (the whole schema targets LP64 little-endian); the
 * pure oracle mirrors this exactly for byte-identical HASHED slabs under a
 * shared key. */
static inline uint64_t
rotl64(uint64_t x, int b)
{
    return (x << b) | (x >> (64 - b));
}

#define SIPROUND                                                          \
    do {                                                                  \
        v0 += v1; v1 = rotl64(v1, 13); v1 ^= v0; v0 = rotl64(v0, 32);     \
        v2 += v3; v3 = rotl64(v3, 16); v3 ^= v2;                          \
        v0 += v3; v3 = rotl64(v3, 21); v3 ^= v0;                          \
        v2 += v1; v1 = rotl64(v1, 17); v1 ^= v2; v2 = rotl64(v2, 32);     \
    } while (0)

static uint64_t
siphash24(const uint8_t *data, size_t len, uint64_t k0, uint64_t k1)
{
    uint64_t v0 = 0x736f6d6570736575ULL ^ k0;
    uint64_t v1 = 0x646f72616e646f6dULL ^ k1;
    uint64_t v2 = 0x6c7967656e657261ULL ^ k0;
    uint64_t v3 = 0x7465646279746573ULL ^ k1;
    const uint8_t *end = data + (len & ~(size_t)7);
    uint64_t b = (uint64_t)len << 56;
    for (; data != end; data += 8) {
        uint64_t m;
        memcpy(&m, data, 8);  /* little-endian word load on the target ABI */
        v3 ^= m;
        SIPROUND;
        SIPROUND;
        v0 ^= m;
    }
    switch (len & 7) {
        case 7: b |= (uint64_t)data[6] << 48; /* fall through */
        case 6: b |= (uint64_t)data[5] << 40; /* fall through */
        case 5: b |= (uint64_t)data[4] << 32; /* fall through */
        case 4: b |= (uint64_t)data[3] << 24; /* fall through */
        case 3: b |= (uint64_t)data[2] << 16; /* fall through */
        case 2: b |= (uint64_t)data[1] << 8;  /* fall through */
        case 1: b |= (uint64_t)data[0];       /* fall through */
        case 0: break;
    }
    v3 ^= b;
    SIPROUND;
    SIPROUND;
    v0 ^= b;
    v2 ^= 0xff;
    SIPROUND;
    SIPROUND;
    SIPROUND;
    SIPROUND;
    return v0 ^ v1 ^ v2 ^ v3;
}
#undef SIPROUND

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

    /* Clock calibration captured once at creation, so the off-path projector can
     * map each completion's monotonic end offset to Unix time without a per-cell
     * wall stamp. epoch_mono_ns is the CLOCK_MONOTONIC_RAW base that the server's
     * `now_ns` timestamps share; epoch_unix_ns is the wall clock at that instant. */
    uint64_t epoch_mono_ns;
    uint64_t epoch_unix_ns;

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

    /* --- crash forensics: the ring as a file-backed mapping ---
     * When a path is configured the ring lives in a MAP_SHARED file, so its
     * pages are the kernel's and a process that dies badly leaves them
     * readable. `ring_map` is the whole mapping (header page + cells) and
     * `ring` points at the cells inside it; when there is no file, `ring` is
     * PyMem memory and `ring_map` is NULL.
     *
     * `cursor_mirror` always points somewhere writable -- into the mapped
     * header when there is a file, at `cursor_scratch` when there is not -- so
     * publishing mirrors the cursor with an unconditional store and no branch.
     * A branch would look cheaper and be the wrong trade: it puts a test on
     * every publish to save one store to a line that is already hot. */
    void *ring_map;
    size_t ring_map_bytes;
    wreath_nfr_ring_file_cursor *cursor_mirror;
    wreath_nfr_ring_file_cursor cursor_scratch;
    uint64_t *loss_mirror;
    uint64_t loss_scratch[WREATH_NFR_LOSS_REASON_COUNT];

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
    /* Pressure gauges for the Inspector: like ring_high_water, atomics so an
     * out-of-thread reader sees coherent values while the writer owns the pool. */
    _Atomic uint32_t phase_in_use;
    _Atomic uint32_t phase_high_water;

    /* --- forensic capture-slab pool (Forensic mode; armed requests only) ---
     * Preallocated fixed slabs, a writer-owned free stack, and two SPSC index
     * rings: a commit ring (writer -> sink, slabs ready to serialize) and a
     * return ring (sink -> writer, slabs the sink has copied out). The writer
     * reclaims returned slabs onto its free stack before reserving, so the free
     * stack stays single-writer-owned. Redaction happens as bytes are written,
     * so a disallowed field's raw bytes never live here. Off/Pulse/Detailed
     * reserve nothing. */
    uint8_t *capture_pool;          /* capture_capacity * slab_bytes, or NULL */
    uint32_t capture_capacity;      /* number of slabs */
    uint32_t slab_bytes;
    uint32_t *capture_free_stack;   /* writer-owned free list of slab indices */
    uint32_t capture_free_top;
    uint32_t capture_ring_mask;     /* masks the two index rings (pow2 slots) */
    uint32_t *capture_commit_ring;  /* SPSC writer -> sink */
    uint32_t *capture_return_ring;  /* SPSC sink -> writer */
    _Alignas(CACHELINE) _Atomic uint64_t capture_commit_head;
    _Alignas(CACHELINE) _Atomic uint64_t capture_commit_tail;
    _Alignas(CACHELINE) _Atomic uint64_t capture_return_head;
    _Alignas(CACHELINE) _Atomic uint64_t capture_return_tail;
    /* process-local keyed hash for redacted-field fingerprints (SipHash-2-4). */
    uint64_t hash_k0;
    uint64_t hash_k1;
    /* pressure gauges (relaxed atomics, like the ring/phase gauges). */
    _Atomic uint32_t capture_in_use;
    _Atomic uint32_t capture_high_water;
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
        uint64_t total = atomic_fetch_add_explicit(&worker->losses.reason[reason], 1,
                                                   memory_order_relaxed) + 1;
        /* Mirror into the ring file, so a post-mortem knows what it is *not*
         * looking at. Always a store, never a branch, for the same reason the
         * cursor mirror is: `loss_mirror` points at worker-local scratch when
         * no file is mapped. This is the drop path, which was never fast. */
        worker->loss_mirror[reason] = total;
    }
}

/* --- lifecycle ------------------------------------------------------------ */

/* Map the ring from `path`, creating and sizing the file.
 *
 * Owner-only (0600) like the WFR1 recording, and for the same reason: a ring
 * file holds whatever the application logged, which is application data. The
 * file is truncated to exactly one header page plus the cell area, so a stale
 * file from a differently-sized run cannot leave a tail of cells behind that a
 * decoder would read as this run's.
 *
 * On failure it sets a Python exception and returns -1. Failing loudly is right
 * here: the caller asked for a forensic ring by configuring a path, and a
 * server that silently ran without one would produce no file to notice.
 */
static int
map_ring_file(wreath_nfr_worker *worker, const char *path, uint32_t ring_records)
{
    size_t bytes = (size_t)WREATH_NFR_RING_FILE_HEADER_BYTES
                   + (size_t)ring_records * WREATH_NFR_CELL_SIZE;
    int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
        return -1;
    }
    if (ftruncate(fd, (off_t)bytes) != 0) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
        close(fd);
        return -1;
    }
    void *map = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    /* The mapping keeps the pages alive on its own; the descriptor has done its
     * job. Holding it open would only be one more thing to leak on teardown. */
    close(fd);
    if (map == MAP_FAILED) {
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
        return -1;
    }
    memset(map, 0, WREATH_NFR_RING_FILE_HEADER_BYTES);
    wreath_nfr_ring_file_header *header = (wreath_nfr_ring_file_header *)map;
    memcpy(header->magic, WREATH_NFR_RING_FILE_MAGIC, 4);
    header->container_version = WREATH_NFR_RING_FILE_VERSION;
    header->schema_version = WREATH_NFR_SCHEMA_VERSION;
    header->flags = 0;
    header->ring_records = ring_records;
    header->cell_size = WREATH_NFR_CELL_SIZE;
    header->worker_id = worker->worker_id;
    header->reserved = 0;
    header->epoch_mono_ns = worker->epoch_mono_ns;
    header->epoch_unix_ns = worker->epoch_unix_ns;
    header->pid = (uint64_t)getpid();
    header->reserved2 = 0;
    {
        struct timespec wall;
        clock_gettime(CLOCK_REALTIME, &wall);
        header->created_unix_nano =
            (uint64_t)wall.tv_sec * 1000000000ULL + (uint64_t)wall.tv_nsec;
    }
    worker->ring_map = map;
    worker->ring_map_bytes = bytes;
    worker->cursor_mirror = (wreath_nfr_ring_file_cursor *)((uint8_t *)map
                            + WREATH_NFR_RING_FILE_CURSOR_OFFSET);
    worker->loss_mirror =
        (uint64_t *)((uint8_t *)map + WREATH_NFR_RING_FILE_LOSS_OFFSET);
    /* Carry over anything already counted: a worker can drop before its ring
     * file exists only if creation were reordered, but copying is one memcpy
     * and it removes the question. */
    for (int i = 0; i < WREATH_NFR_LOSS_REASON_COUNT; i++) {
        worker->loss_mirror[i] = worker->loss_scratch[i];
    }
    worker->ring = (uint8_t *)map + WREATH_NFR_RING_FILE_HEADER_BYTES;
    return 0;
}

wreath_nfr_worker *
wreath_nfr_worker_new(uint8_t mode, uint32_t worker_id, uint32_t ring_records,
                      uint32_t active_requests, uint32_t histogram_count,
                      int completion_summaries, uint64_t detailed_sample_threshold,
                      uint32_t phase_slots, uint64_t slow_threshold_us,
                      uint32_t capture_slabs, uint32_t slab_bytes,
                      uint64_t hash_key0, uint64_t hash_key1,
                      const char *ring_path)
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
    {
        /* CLOCK_MONOTONIC_RAW is the base PyTime_MonotonicRaw uses on Linux, so
         * this pairs with the server's `now_ns`; CLOCK_REALTIME is the wall pair. */
        struct timespec mono, wall;
        clock_gettime(CLOCK_MONOTONIC_RAW, &mono);
        clock_gettime(CLOCK_REALTIME, &wall);
        worker->epoch_mono_ns = (uint64_t)mono.tv_sec * 1000000000ULL + (uint64_t)mono.tv_nsec;
        worker->epoch_unix_ns = (uint64_t)wall.tv_sec * 1000000000ULL + (uint64_t)wall.tv_nsec;
    }
    worker->detailed_sample_threshold = detailed_sample_threshold;
    worker->slow_threshold_us = slow_threshold_us;
    worker->ring_records = ring_records;
    worker->ring_mask = ring_records ? ring_records - 1 : 0;
    worker->histogram_count = histogram_count;
    atomic_init(&worker->ring_head, 0);
    atomic_init(&worker->ring_tail, 0);
    atomic_init(&worker->ring_high_water, 0);
    atomic_init(&worker->phase_in_use, 0);
    atomic_init(&worker->phase_high_water, 0);
    atomic_init(&worker->capture_commit_head, 0);
    atomic_init(&worker->capture_commit_tail, 0);
    atomic_init(&worker->capture_return_head, 0);
    atomic_init(&worker->capture_return_tail, 0);
    atomic_init(&worker->capture_in_use, 0);
    atomic_init(&worker->capture_high_water, 0);
    atomic_init(&worker->requests, 0);
    atomic_init(&worker->completions, 0);
    atomic_init(&worker->active_count, 0);
    for (int i = 0; i < WREATH_NFR_LOSS_REASON_COUNT; i++) {
        atomic_init(&worker->losses.reason[i], 0);
    }

    /* Always somewhere writable, so publish mirrors the cursor with a store and
     * no branch. `map_ring_file` repoints both into the mapped header. */
    worker->cursor_mirror = &worker->cursor_scratch;
    worker->loss_mirror = worker->loss_scratch;
    if (ring_records) {
        if (ring_path != NULL) {
            if (map_ring_file(worker, ring_path, ring_records) != 0) {
                /* The exception is already set: a configured forensic ring that
                 * cannot be opened is a startup failure, not a silent downgrade
                 * to a ring nobody can read after the crash. */
                wreath_nfr_worker_free(worker);
                return NULL;
            }
        } else {
            worker->ring = PyMem_Calloc((size_t)ring_records, WREATH_NFR_CELL_SIZE);
            if (worker->ring == NULL) {
                goto no_memory;
            }
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
    /* The capture pool is only meaningful in Forensic mode. */
    if (capture_slabs != 0 && slab_bytes >= WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE &&
        mode >= WREATH_NFR_MODE_FORENSIC) {
        worker->capture_capacity = capture_slabs;
        worker->slab_bytes = slab_bytes;
        /* Index rings sized to a power of two strictly greater than the slab
         * count, so a ring can hold every slab and never reports full. */
        uint32_t slots = 1;
        while (slots <= capture_slabs) {
            slots <<= 1;
        }
        worker->capture_ring_mask = slots - 1;
        worker->capture_pool = PyMem_Calloc(capture_slabs, slab_bytes);
        worker->capture_free_stack = PyMem_Calloc(capture_slabs, sizeof(uint32_t));
        worker->capture_commit_ring = PyMem_Calloc(slots, sizeof(uint32_t));
        worker->capture_return_ring = PyMem_Calloc(slots, sizeof(uint32_t));
        if (worker->capture_pool == NULL || worker->capture_free_stack == NULL ||
            worker->capture_commit_ring == NULL || worker->capture_return_ring == NULL) {
            goto no_memory;
        }
        for (uint32_t i = 0; i < capture_slabs; i++) {
            worker->capture_free_stack[i] = capture_slabs - 1 - i;
        }
        worker->capture_free_top = capture_slabs;
    }
    /* A shared explicit key makes HASHED slabs reproducible for differential
     * tests; the default (0, 0) draws a process-local key from the CSPRNG. */
    if (hash_key0 == 0 && hash_key1 == 0) {
        worker->hash_k0 = seed_entropy();
        worker->hash_k1 = seed_entropy();
    } else {
        worker->hash_k0 = hash_key0;
        worker->hash_k1 = hash_key1;
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
    if (worker->ring_map != NULL) {
        /* The one place anything is msync'd. MAP_SHARED already survives the
         * process without it -- the kernel owns the pages -- so this is for the
         * other failure: a machine that goes away before writeback. On a clean
         * close it costs nothing anyone notices and makes the file coherent. */
        msync(worker->ring_map, worker->ring_map_bytes, MS_SYNC);
        munmap(worker->ring_map, worker->ring_map_bytes);
        worker->ring_map = NULL;
        worker->ring = NULL;  /* it pointed into the mapping, not to PyMem */
        /* Both mirrors pointed into the mapping that has just gone. Repoint
         * them at the scratch they started on: teardown is not instantaneous,
         * and a note_loss racing the unmap must not write to a dead page. */
        worker->cursor_mirror = &worker->cursor_scratch;
        worker->loss_mirror = worker->loss_scratch;
    }
    PyMem_Free(worker->ring);
    PyMem_Free(worker->histograms);
    PyMem_Free(worker->active);
    PyMem_Free(worker->free_stack);
    PyMem_Free(worker->phase_scratch);
    PyMem_Free(worker->phase_free_stack);
    PyMem_Free(worker->capture_pool);
    PyMem_Free(worker->capture_free_stack);
    PyMem_Free(worker->capture_commit_ring);
    PyMem_Free(worker->capture_return_ring);
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
    int32_t slot = (int32_t)worker->phase_free_stack[--worker->phase_free_top];
    uint32_t in_use = worker->phase_capacity - worker->phase_free_top;
    atomic_store_explicit(&worker->phase_in_use, in_use, memory_order_relaxed);
    uint32_t hw = atomic_load_explicit(&worker->phase_high_water,
                                       memory_order_relaxed);
    if (in_use > hw) {
        atomic_store_explicit(&worker->phase_high_water, in_use,
                              memory_order_relaxed);
    }
    return slot;
}

static void
phase_release(wreath_nfr_worker *worker, int32_t slot)
{
    if (slot < 0 || (uint32_t)slot >= worker->phase_capacity) {
        return;
    }
    if (worker->phase_free_top < worker->phase_capacity) {
        worker->phase_free_stack[worker->phase_free_top++] = (uint32_t)slot;
        atomic_store_explicit(&worker->phase_in_use,
                              worker->phase_capacity - worker->phase_free_top,
                              memory_order_relaxed);
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

/* --- forensic capture-slab pool (writer-owned free-list + SPSC index rings) */

/* Push/pop a uint32 slab index through one SPSC index ring. Occupancy is
 * head - tail; the ring holds mask+1 slots, sized above the slab count so a push
 * never fails in practice. The producer release-stores head; the consumer
 * acquire-loads it, so the slab bytes a producer wrote are visible after a pop. */
static int
index_ring_push(uint32_t *ring, uint32_t mask, _Atomic uint64_t *head,
                _Atomic uint64_t *tail, uint32_t value)
{
    uint64_t h = atomic_load_explicit(head, memory_order_relaxed);
    uint64_t t = atomic_load_explicit(tail, memory_order_acquire);
    if (h - t > mask) {
        return 0;  /* full */
    }
    ring[h & mask] = value;
    atomic_store_explicit(head, h + 1, memory_order_release);
    return 1;
}

static int
index_ring_pop(uint32_t *ring, uint32_t mask, _Atomic uint64_t *head,
               _Atomic uint64_t *tail, uint32_t *out)
{
    uint64_t t = atomic_load_explicit(tail, memory_order_relaxed);
    uint64_t h = atomic_load_explicit(head, memory_order_acquire);
    if (t == h) {
        return 0;  /* empty */
    }
    *out = ring[t & mask];
    atomic_store_explicit(tail, t + 1, memory_order_release);
    return 1;
}

static uint8_t *
capture_slab_ptr(wreath_nfr_worker *worker, int32_t slot)
{
    return worker->capture_pool + (size_t)slot * worker->slab_bytes;
}

static void
capture_gauge(wreath_nfr_worker *worker)
{
    uint32_t in_use = worker->capture_capacity - worker->capture_free_top;
    atomic_store_explicit(&worker->capture_in_use, in_use, memory_order_relaxed);
    uint32_t hw = atomic_load_explicit(&worker->capture_high_water,
                                       memory_order_relaxed);
    if (in_use > hw) {
        atomic_store_explicit(&worker->capture_high_water, in_use,
                              memory_order_relaxed);
    }
}

/* Drain slabs the sink has returned back onto the writer-owned free stack. Run
 * by the writer just before it reserves, so the free stack stays single-writer. */
static void
capture_reclaim(wreath_nfr_worker *worker)
{
    uint32_t slot;
    while (index_ring_pop(worker->capture_return_ring, worker->capture_ring_mask,
                          &worker->capture_return_head, &worker->capture_return_tail,
                          &slot)) {
        if (worker->capture_free_top < worker->capture_capacity) {
            worker->capture_free_stack[worker->capture_free_top++] = slot;
        }
    }
    capture_gauge(worker);
}

/* Reserve a slab for an armed request's first captured field. Returns its index
 * or -1 (CAPTURE_POOL_FULL). Lays down the self-identifying slab header. */
static int32_t
capture_reserve(wreath_nfr_worker *worker, wreath_nfr_context *ctx)
{
    capture_reclaim(worker);
    if (worker->capture_free_top == 0) {
        note_loss(worker, WREATH_NFR_LOSS_CAPTURE_POOL_FULL);
        return -1;
    }
    int32_t slot = (int32_t)worker->capture_free_stack[--worker->capture_free_top];
    capture_gauge(worker);
    wreath_nfr_capture_slab_header *hdr =
        (wreath_nfr_capture_slab_header *)capture_slab_ptr(worker, slot);
    memset(hdr, 0, sizeof(*hdr));
    hdr->schema_version = WREATH_NFR_SCHEMA_VERSION;
    hdr->kind = WREATH_NFR_KIND_CAPTURE;
    hdr->worker_id = (uint8_t)worker->worker_id;
    hdr->request_id = ctx->request_id;
    ctx->capture_used = WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE;
    return slot;
}

/* Return a slab straight to the writer-owned free stack (nothing to commit). */
static void
capture_release(wreath_nfr_worker *worker, int32_t slot)
{
    if (slot < 0 || (uint32_t)slot >= worker->capture_capacity) {
        return;
    }
    if (worker->capture_free_top < worker->capture_capacity) {
        worker->capture_free_stack[worker->capture_free_top++] = (uint32_t)slot;
        capture_gauge(worker);
    }
}

/* Commit an armed request's slab behind a published completion (like phases), or
 * release it when there is no completion / nothing was captured. */
static void
capture_finish(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
               int completion_published)
{
    if (ctx->capture_slot < 0) {
        return;
    }
    wreath_nfr_capture_slab_header *hdr =
        (wreath_nfr_capture_slab_header *)capture_slab_ptr(worker, ctx->capture_slot);
    if (completion_published && hdr->field_count > 0) {
        hdr->used_bytes = ctx->capture_used;
        if (!index_ring_push(worker->capture_commit_ring, worker->capture_ring_mask,
                             &worker->capture_commit_head, &worker->capture_commit_tail,
                             (uint32_t)ctx->capture_slot)) {
            /* Rings are sized to hold every slab, so this is unreachable; count it
             * and reclaim rather than leak the slab if the invariant ever breaks. */
            note_loss(worker, WREATH_NFR_LOSS_CAPTURE_POOL_FULL);
            capture_release(worker, ctx->capture_slot);
        }
    } else {
        capture_release(worker, ctx->capture_slot);
    }
    ctx->capture_slot = -1;
    ctx->capture_used = 0;
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
    /* Mirror the cursor for a decoder that will only ever see the file. Plain,
     * because nothing in this process reads it and the ordering that matters --
     * cell before head -- is the release store above. Unconditional: the mirror
     * points at worker-local scratch when no file is mapped, so an unmapped
     * ring pays a store to a hot line rather than a branch on every publish. */
    worker->cursor_mirror->head = head + 1;
    uint64_t new_occupancy = occupancy + 1;
    uint64_t hw = atomic_load_explicit(&worker->ring_high_water, memory_order_relaxed);
    if (new_occupancy > hw) {
        atomic_store_explicit(&worker->ring_high_water, new_occupancy,
                              memory_order_relaxed);
    }
    return 1;
}

/* The log emitter's publish seam. The emitter packs a KIND_LOG cell and hands
 * it here so records ride the same single-writer ring, the same one capacity
 * check and release store, and the same RING_FULL accounting as a completion.
 * The native emitter replaced the packing above this line, not this call --
 * which is what the design promised the seam would survive. */
int
wreath_nfr_publish_cell(wreath_nfr_worker *worker, const void *cell)
{
    return ring_publish(worker, cell);
}

/* The keyed redaction fingerprint, for callers outside this translation unit.
 *
 * The log emitter hashes with the *site registry's* key rather than the
 * worker's, because the Python packer uses that key and the two must agree
 * byte for byte -- a fingerprint that differed between the C and Python
 * halves of one process would break correlation within a single recording. So
 * the key travels in rather than being read off the worker. */
uint64_t
wreath_nfr_fingerprint(const void *data, size_t len, uint64_t k0, uint64_t k1)
{
    return siphash24((const uint8_t *)data, len, k0, k1);
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
    /* The reader's half of the mirror. Off the request path, so its cost is
     * nobody's concern; without it a decoder cannot tell how far behind the
     * projector was when the process died, which is often the whole story. */
    worker->cursor_mirror->tail = tail;
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
    ctx->phase_slot = -1;    /* 0 is a valid slot; unarmed requests hold none */
    ctx->capture_slot = -1;  /* a slab is reserved lazily on the first capture */
    ctx->request_id = worker->next_request_id++;
    ctx->span_id = splitmix64(&worker->rng_state);  /* this request's span */
    /* Detailed-mode arming: a cheap, deterministic per-request sample. Pulse
     * never arms, so this branch and its flag are absent from the Pulse cell. An
     * armed request reserves a phase-scratch block (or drops phases + counts the
     * loss if the pool is exhausted). In Forensic mode the same sample also arms
     * payload capture (a nested subset of the phase-armed set), gating the
     * deny-by-default capture path; the slab itself is reserved lazily. */
    if (worker->mode >= WREATH_NFR_MODE_DETAILED &&
        (mix64(ctx->request_id) & 0xFFFFFFFFULL) < worker->detailed_sample_threshold) {
        ctx->flags |= WREATH_NFR_FLAG_DETAILED_ARMED;
        if (worker->mode >= WREATH_NFR_MODE_FORENSIC) {
            ctx->flags |= WREATH_NFR_FLAG_FORENSIC_ARMED;
        }
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
wreath_nfr_context_capture(wreath_nfr_worker *worker, wreath_nfr_context *ctx,
                           uint16_t field_class, uint16_t descriptor_id,
                           uint8_t disposition, const uint8_t *data, Py_ssize_t len,
                           uint32_t max_bytes)
{
    /* Deny-by-default: only a Forensic-armed request captures. Every other path
     * (Off/Pulse/Detailed/unarmed) is a single predicted branch on the flag. */
    if (!(ctx->flags & WREATH_NFR_FLAG_FORENSIC_ARMED) ||
        worker->capture_capacity == 0) {
        return;
    }
    if (ctx->capture_slot < 0) {
        ctx->capture_slot = capture_reserve(worker, ctx);
        if (ctx->capture_slot < 0) {
            return;  /* pool exhausted: loss already counted */
        }
    }
    if (len < 0) {
        len = 0;
    }
    uint64_t original_length = (uint64_t)len;

    /* Decide the retained bytes by disposition, redacting *before* anything
     * enters the slab. RAW keeps a bounded prefix; HASHED keeps only a keyed
     * fingerprint; MASKED/LENGTH keep no bytes at all. */
    uint32_t stored = 0;
    uint8_t hash_buf[WREATH_NFR_CAPTURE_HASH_BYTES];
    const uint8_t *payload = NULL;
    if (disposition == WREATH_NFR_CAP_RAW) {
        stored = original_length > 0xFFFFu ? 0xFFFFu : (uint32_t)original_length;
        /* Policy byte cap (max_bytes): a longer body keeps only a prefix and is
         * marked truncated below, its true original length preserved. */
        if (max_bytes != 0 && stored > max_bytes) {
            stored = max_bytes;
        }
        payload = data;
    } else if (disposition == WREATH_NFR_CAP_HASHED) {
        uint64_t h = siphash24(data, (size_t)len, worker->hash_k0, worker->hash_k1);
        memcpy(hash_buf, &h, sizeof(h));
        stored = WREATH_NFR_CAPTURE_HASH_BYTES;
        payload = hash_buf;
    }

    uint8_t *slab = capture_slab_ptr(worker, ctx->capture_slot);
    uint32_t used = ctx->capture_used;
    /* Not even a field header fits: drop the field whole (CAPTURE_POOL_FULL). */
    if (used + WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE > worker->slab_bytes) {
        note_loss(worker, WREATH_NFR_LOSS_CAPTURE_POOL_FULL);
        return;
    }
    uint32_t room = worker->slab_bytes - used - WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE;
    uint32_t padded = (stored + (WREATH_NFR_CAPTURE_FIELD_ALIGN - 1)) &
                      ~(uint32_t)(WREATH_NFR_CAPTURE_FIELD_ALIGN - 1);
    if (padded > room) {
        if (disposition == WREATH_NFR_CAP_RAW) {
            /* Clip a raw body to the largest aligned chunk that fits. */
            stored = room & ~(uint32_t)(WREATH_NFR_CAPTURE_FIELD_ALIGN - 1);
            padded = stored;
        } else {
            /* A fixed-size hash that will not fit is dropped, not clipped. */
            note_loss(worker, WREATH_NFR_LOSS_CAPTURE_POOL_FULL);
            return;
        }
    }

    wreath_nfr_capture_field field;
    field.field_class = field_class;
    field.descriptor_id = descriptor_id;
    field.disposition = disposition;
    field.reserved = 0;
    field.stored_length = (uint16_t)stored;
    field.original_length =
        original_length > 0xFFFFFFFFULL ? 0xFFFFFFFFu : (uint32_t)original_length;
    memcpy(slab + used, &field, sizeof(field));
    used += WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE;
    if (stored > 0 && payload != NULL) {
        memcpy(slab + used, payload, stored);
    }
    /* Zero the alignment pad so a recycled slab never leaks stale bytes and the
     * serialized slab is reproducible. */
    if (padded > stored) {
        memset(slab + used + stored, 0, padded - stored);
    }
    used += padded;
    ctx->capture_used = used;

    wreath_nfr_capture_slab_header *hdr = (wreath_nfr_capture_slab_header *)slab;
    hdr->field_count++;
    /* A raw field that kept fewer bytes than it had was truncated. */
    if (disposition == WREATH_NFR_CAP_RAW && stored < original_length) {
        hdr->flags |= (uint8_t)WREATH_NFR_FLAG_BODY_TRUNCATED;
        ctx->flags |= WREATH_NFR_FLAG_BODY_TRUNCATED;
        note_loss(worker, WREATH_NFR_LOSS_BODY_TRUNCATED);
    }
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
        /* No completion cell to anchor phases/slab to: drop them, but still
         * return the scratch block and capture slab so the pools are not leaked. */
        phase_finish(worker, ctx, 0);
        capture_finish(worker, ctx, 0);
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
    /* Monotonic end instant as ms from the worker's epoch, so the off-path
     * projector maps it to Unix time via the worker's calibration -- no per-cell
     * wall stamp needed. Clamp at u32 (~49 days of uptime) rather than wrap. */
    {
        uint64_t end_ms = now_ns >= worker->epoch_mono_ns
            ? (now_ns - worker->epoch_mono_ns) / 1000000ULL
            : 0;  /* a synthetic clock before the epoch clamps to 0, like the oracle */
        cell.end_offset_ms = end_ms > 0xFFFFFFFFULL ? 0xFFFFFFFFU : (uint32_t)end_ms;
    }
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
    if (published && (ctx->flags & WREATH_NFR_FLAG_HAS_CLIENT_FACTS)) {
        wreath_nfr_client_facts_cell facts;
        memset(&facts, 0, sizeof(facts));
        facts.schema_version = WREATH_NFR_SCHEMA_VERSION;
        facts.kind = WREATH_NFR_KIND_CLIENT_FACTS;
        facts.flags = ctx->client_flags;
        facts.user_agent_rule_id = ctx->user_agent_rule_id;
        facts.country[0] = ctx->client_country[0];
        facts.country[1] = ctx->client_country[1];
        facts.request_id = ctx->request_id;
        ring_publish(worker, &facts);
    }
    /* Detailed phase batches and the Forensic capture slab follow the completion
     * they belong to (armed only); a dropped completion drops both. */
    phase_finish(worker, ctx, published);
    capture_finish(worker, ctx, published);
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
    /* Return an armed request's scratch block and capture slab without
     * committing anything. */
    phase_finish(worker, ctx, 0);
    capture_finish(worker, ctx, 0);
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

/* Phase-scratch pressure gauges for the Inspector (Stage 3 slice 3b). Zero for
 * a worker whose mode reserves no pool (Off/Pulse). */
uint64_t
wreath_nfr_phase_capacity(const wreath_nfr_worker *worker)
{
    return worker->phase_capacity;
}

uint64_t
wreath_nfr_phase_in_use(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->phase_in_use, memory_order_relaxed);
}

uint64_t
wreath_nfr_phase_high_water(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->phase_high_water, memory_order_relaxed);
}

uint64_t
wreath_nfr_active_capacity(const wreath_nfr_worker *worker)
{
    return worker->active_capacity;
}

uint32_t
wreath_nfr_active_snapshot(const wreath_nfr_worker *worker,
                           wreath_nfr_active_entry *out, uint32_t max)
{
    uint32_t written = 0;
    for (uint32_t i = 0; i < worker->active_capacity && written < max; i++) {
        const wreath_nfr_active_slot *entry = &worker->active[i];
        /* Seqlock read: retry while the writer is mid-update (odd) or the
         * generation moved under us; give up on a slot after a few spins so a
         * reader can never be pinned by writer traffic. */
        for (int attempt = 0; attempt < 4; attempt++) {
            uint32_t before = atomic_load_explicit(&entry->generation,
                                                   memory_order_acquire);
            if (before & 1u) {
                continue;
            }
            wreath_nfr_active_entry row = {
                .request_id = entry->request_id,
                .start_ns = entry->start_ns,
                .route_id = entry->route_id,
                .protocol = entry->protocol,
            };
            int in_use = entry->in_use;
            uint32_t after = atomic_load_explicit(&entry->generation,
                                                  memory_order_acquire);
            if (after != before) {
                continue;
            }
            if (in_use) {
                out[written++] = row;
            }
            break;
        }
    }
    return written;
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

/* --- forensic capture drain / gauges (reader side) ------------------------ */

Py_ssize_t
wreath_nfr_capture_drain(wreath_nfr_worker *worker, uint8_t *out, uint32_t *lengths,
                         Py_ssize_t max_slabs)
{
    if (worker->capture_capacity == 0 || max_slabs <= 0) {
        return 0;
    }
    Py_ssize_t copied = 0;
    uint32_t slot;
    while (copied < max_slabs &&
           index_ring_pop(worker->capture_commit_ring, worker->capture_ring_mask,
                          &worker->capture_commit_head, &worker->capture_commit_tail,
                          &slot)) {
        uint8_t *slab = capture_slab_ptr(worker, slot);
        const wreath_nfr_capture_slab_header *hdr =
            (const wreath_nfr_capture_slab_header *)slab;
        uint32_t used = hdr->used_bytes;
        if (used > worker->slab_bytes) {
            used = worker->slab_bytes;  /* defensive clamp against a bad header */
        }
        memcpy(out + (size_t)copied * worker->slab_bytes, slab, used);
        lengths[copied] = used;
        copied++;
        /* Hand the slab back to the writer (SPSC sink -> writer); the writer
         * reclaims it onto the free stack on its next reserve. */
        index_ring_push(worker->capture_return_ring, worker->capture_ring_mask,
                        &worker->capture_return_head, &worker->capture_return_tail,
                        slot);
    }
    return copied;
}

uint64_t
wreath_nfr_worker_epoch_mono_ns(const wreath_nfr_worker *worker)
{
    return worker->epoch_mono_ns;
}

uint64_t
wreath_nfr_worker_epoch_unix_ns(const wreath_nfr_worker *worker)
{
    return worker->epoch_unix_ns;
}

uint64_t
wreath_nfr_capture_capacity(const wreath_nfr_worker *worker)
{
    return worker->capture_capacity;
}

uint64_t
wreath_nfr_capture_slab_bytes(const wreath_nfr_worker *worker)
{
    return worker->slab_bytes;
}

uint64_t
wreath_nfr_capture_in_use(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->capture_in_use, memory_order_relaxed);
}

uint64_t
wreath_nfr_capture_high_water(const wreath_nfr_worker *worker)
{
    return atomic_load_explicit(&worker->capture_high_water, memory_order_relaxed);
}

uint64_t
wreath_nfr_capture_committed(const wreath_nfr_worker *worker)
{
    uint64_t head = atomic_load_explicit(&worker->capture_commit_head,
                                         memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&worker->capture_commit_tail,
                                         memory_order_relaxed);
    return head - tail;
}
