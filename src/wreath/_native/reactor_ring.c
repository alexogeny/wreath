/* io_uring ownership, operation slabs, and submission primitives. */
#define ST_MAX_DRAIN 8          /* recvs per readable event before yielding */
#define ST_DATA_RECV 262144     /* recv size for non-buffered protocols */
#define ST_CORK_MAX 262144      /* flush corked writes once they reach this size */

/* Metal-only operation/completion substrate. io_uring retains each operation
 * handle until its generation-validated CQE arrives. Slots are per poller/worker,
 * bounded, and generation-validated before state-machine delivery. */
#define METAL_CONNECTION_CAPACITY_DEFAULT 4096
#define METAL_OPERATION_CAPACITY_DEFAULT 4096
#define METAL_RECV_BUFFER_COUNT_DEFAULT 16
#define METAL_CAPACITY_MIN 16
#define METAL_CONNECTION_CAPACITY_MAX (1U << 20)
#define METAL_OPERATION_CAPACITY_MAX (1U << 21)
#define METAL_RECV_BUFFER_COUNT_MAX (1U << 15)
#define METAL_RECV_BUFFER_SIZE 16384
#define METAL_RECV_BUFFER_GROUP 1
#define METAL_TRACE_CAPACITY 256
#define METAL_TOKEN_TAG_SHIFT 62
#define METAL_TOKEN_PAYLOAD_MASK ((1ULL << METAL_TOKEN_TAG_SHIFT) - 1)
#define METAL_TOKEN_ACCEPT 0ULL
#define METAL_TOKEN_RECEIVE (1ULL << METAL_TOKEN_TAG_SHIFT)
#define METAL_TOKEN_SEND (2ULL << METAL_TOKEN_TAG_SHIFT)
#define METAL_TOKEN_CONTROL (3ULL << METAL_TOKEN_TAG_SHIFT)
#define METAL_RECEIVE_DIRECT_FLAG (1ULL << 61)
#define METAL_WAKE_TOKEN (METAL_TOKEN_CONTROL | 1ULL)
#define METAL_CANCEL_TOKEN (METAL_TOKEN_CONTROL | 2ULL)
#define METAL_SIGNAL_TOKEN (METAL_TOKEN_CONTROL | 3ULL)
#define METAL_POLL_TOKEN_FLAG (1ULL << 61)
#define METAL_POLL_GENERATION_LIMIT (1U << 29)
#define METAL_GENERATION_LIMIT (1U << 29)
#define METAL_SLOT_KIND_SHIFT 29
#define METAL_SLOT_GENERATION_MASK (METAL_GENERATION_LIMIT - 1)
#define METAL_SEND_NOTIFICATIONS_MASK 0xffffU
#define METAL_SEND_COMPLETE (1U << 16)
#define METAL_SEND_ERRNO_SHIFT 17
#define METAL_SEND_ERRNO_MAX 0x7fffU
#define METAL_SLOT_NONE UINT32_MAX

enum {
    METAL_OP_RECV = 1,
    METAL_OP_SEND = 2,
    METAL_OP_ACCEPT = 3,
    METAL_OP_CANCEL = 4,
    METAL_OP_BUFFER = 5,
    METAL_OP_TIMEOUT = 6,
};
enum {
    METAL_IO_URING = 1,
};
enum {
    METAL_COMPLETION_EOF = 0x1,
    METAL_COMPLETION_ERROR = 0x2,
};

typedef struct {
    uint64_t token;
    int32_t result;
    uint16_t kind;
    uint16_t flags;
    uint32_t value;
} MetalCompletion;

typedef struct {
    void *owner;
    uint64_t related;
    uint32_t generation_kind;
    uint32_t next_free;
} MetalSlot;

_Static_assert(sizeof(MetalSlot) == 24,
               "generational slab slot must remain three machine words");

static uint32_t
metal_slot_generation(const MetalSlot *slot)
{
    return slot->generation_kind & METAL_SLOT_GENERATION_MASK;
}

static uint16_t
metal_slot_kind(const MetalSlot *slot)
{
    uint16_t encoded =
        (uint16_t)(slot->generation_kind >> METAL_SLOT_KIND_SHIFT);
    return encoded == 0 ? UINT16_MAX : (uint16_t)(encoded - 1);
}

static int
metal_slot_live(const MetalSlot *slot)
{
    return (slot->generation_kind >> METAL_SLOT_KIND_SHIFT) != 0;
}

typedef struct {
    MetalSlot *slots;
    uint32_t capacity;
    uint32_t free_head;
    uint32_t occupancy;
    uint32_t high_water;
    uint64_t exhaustions;
    uint64_t generation_wraps;
    uint64_t stale;
} MetalSlab;

typedef struct {
    int fd;
    int enter_fd;
    unsigned enter_flags;
    int registered_ring_fd;
    void *sq_ring;
    void *cq_ring;
    struct io_uring_sqe *sqes;
    size_t sq_ring_size;
    size_t cq_ring_size;
    size_t sqes_size;
    unsigned *sq_head;
    unsigned *sq_tail;
    unsigned *sq_mask;
    unsigned *sq_entries;
    unsigned *sq_array;
    unsigned *cq_head;
    unsigned *cq_tail;
    unsigned *cq_mask;
    struct io_uring_cqe *cqes;
    unsigned sq_mask_value;
    unsigned sq_entries_value;
    unsigned cq_mask_value;
    unsigned cached_sq_tail;
    unsigned published_sq_tail;
    unsigned cached_cq_head;
    unsigned pending_submissions;
    uint64_t sq_tail_publications;
    uint64_t cq_head_publications;
    uint64_t enter_calls;
    int diagnostics;
    uint32_t features;
    uint32_t setup_flags;
} MetalUring;

typedef struct {
    struct io_uring_buf_ring *ring;
    char *data;
    size_t ring_size;
    size_t data_size;
    uint16_t tail;
    uint16_t entries;
    uint16_t mask;
    uint16_t group_id;
    uint32_t buffer_size;
    uint64_t *tokens;
    uint8_t *in_ring;
    MetalSlab *descriptors;
    int registered;
} MetalProvidedBuffers;

typedef struct {
    uint64_t sequence;
    uint64_t token;
    int32_t result;
    uint16_t kind;
    uint8_t backend;
    uint8_t flags;
} MetalTraceEntry;

typedef struct {
    MetalSlab connections;
    MetalSlab operations;
    MetalSlab buffer_descriptors;
    MetalUring uring;
    MetalProvidedBuffers receive_buffers;
    int diagnostics;
    uint64_t submissions;
    uint64_t accept_submissions;
    uint64_t accept_completions;
    uint64_t accept_native_activations;
    uint64_t accept_stale;
    uint64_t accept_errors;
    uint64_t accept_multishot_fallbacks;
    uint64_t wake_requests;
    uint64_t wake_submissions;
    uint64_t wake_completions;
    uint64_t wake_writes;
    uint64_t wake_coalesced;
    int wake_pending;
    uint64_t signal_submissions;
    uint64_t signal_completions;
    uint64_t poll_submissions;
    uint64_t poll_completions;
    uint64_t poll_cancellations;
    uint64_t poll_stale;
    uint64_t poll_multishot_fallbacks;
    uint64_t readiness_callbacks;
    uint64_t receive_submissions;
    uint64_t receive_completions;
    uint64_t receive_stale;
    uint64_t receive_errors;
    uint64_t receive_multishot_fallbacks;
    uint64_t direct_receive_completions;
    uint64_t direct_receive_bytes;
    uint64_t provided_buffer_recycles;
    uint64_t provided_buffer_exhaustions;
    uint64_t send_submissions;
    uint64_t send_completions;
    uint64_t send_stale;
    uint64_t send_errors;
    uint64_t send_zc_notifications;
    uint64_t send_zc_copied;
    uint64_t send_zc_bytes;
    uint64_t send_copy_bytes;
    uint64_t retained_send_enqueues;
    uint64_t submission_batches;
    uint64_t submitted_sqes;
    uint64_t blocking_enters;
    uint64_t uring_waits;
    uint64_t uring_timeouts;
    uint64_t spin_attempts;
    uint64_t spin_hits;
    uint64_t spin_misses;
    uint64_t spin_nanoseconds;
    uint64_t arrival_samples;
    double arrival_ewma_ns;
    double arrival_deviation_ns;
    MetalTraceEntry *trace;
    uint64_t trace_sequence;
    uint32_t trace_count;
    int adaptive_polling;
    int poll_multishot_enabled;
    int receive_enabled;
    int receive_setup_errno;
    int send_enabled;
    int send_setup_errno;
    uint64_t completions;
    uint64_t cross_worker_rejections;
    unsigned long owner_thread;
    uint32_t worker_id;
} MetalRuntime;

static uint64_t metal_slab_allocate(
    MetalSlab *, void *, uint64_t, uint16_t);
static MetalSlot *metal_slab_validate(MetalSlab *, uint64_t, void *);
static void metal_slab_release(MetalSlab *, uint64_t, void *);

static void
metal_trace_add(MetalRuntime *runtime, uint8_t backend, uint16_t kind,
                uint64_t token, int32_t result, uint8_t flags)
{
    if (runtime->trace == NULL) {
        return;
    }
    uint64_t sequence = ++runtime->trace_sequence;
    uint32_t index = (uint32_t)((sequence - 1) % METAL_TRACE_CAPACITY);
    MetalTraceEntry *entry = &runtime->trace[index];
    entry->sequence = sequence;
    entry->token = token;
    entry->result = result;
    entry->kind = kind;
    entry->backend = backend;
    entry->flags = flags;
    if (runtime->trace_count < METAL_TRACE_CAPACITY) {
        runtime->trace_count++;
    }
}

static void
metal_uring_clear(MetalUring *ring)
{
    if (ring->sqes != MAP_FAILED && ring->sqes != NULL) {
        munmap(ring->sqes, ring->sqes_size);
    }
    if (ring->cq_ring != MAP_FAILED && ring->cq_ring != NULL &&
        ring->cq_ring != ring->sq_ring) {
        munmap(ring->cq_ring, ring->cq_ring_size);
    }
    if (ring->sq_ring != MAP_FAILED && ring->sq_ring != NULL) {
        munmap(ring->sq_ring, ring->sq_ring_size);
    }
    if (ring->fd >= 0) {
        if (ring->registered_ring_fd) {
            struct io_uring_rsrc_update update;
            memset(&update, 0, sizeof(update));
            update.offset = (uint32_t)ring->enter_fd;
            syscall(SYS_io_uring_register, ring->fd,
                    IORING_UNREGISTER_RING_FDS, &update, 1);
        }
        close(ring->fd);
    }
    memset(ring, 0, sizeof(*ring));
    ring->fd = -1;
    ring->enter_fd = -1;
}

static int
metal_uring_init(MetalUring *ring, unsigned entries)
{
    memset(ring, 0, sizeof(*ring));
    ring->fd = -1;
    ring->enter_fd = -1;
    struct io_uring_params params;
    memset(&params, 0, sizeof(params));
#ifdef IORING_SETUP_SINGLE_ISSUER
    params.flags |= IORING_SETUP_SINGLE_ISSUER;
#endif
#ifdef IORING_SETUP_COOP_TASKRUN
    params.flags |= IORING_SETUP_COOP_TASKRUN;
#endif
#ifdef IORING_SETUP_DEFER_TASKRUN
    params.flags |= IORING_SETUP_DEFER_TASKRUN;
#endif
    int fd = (int)syscall(SYS_io_uring_setup, entries, &params);
    if (fd < 0 && errno == EINVAL && params.flags != 0) {
        memset(&params, 0, sizeof(params));
        fd = (int)syscall(SYS_io_uring_setup, entries, &params);
    }
    if (fd < 0) {
        return -1;
    }
    ring->fd = fd;
    ring->enter_fd = fd;
    /* Registering the ring itself avoids fdget/fdput in every io_uring_enter.
     * The ring is single-owner, so its registered index is stable for life. */
    struct io_uring_rsrc_update ring_update;
    memset(&ring_update, 0, sizeof(ring_update));
    ring_update.offset = UINT32_MAX;
    ring_update.data = (uint64_t)fd;
    int ring_registration = (int)syscall(
        SYS_io_uring_register, fd, IORING_REGISTER_RING_FDS,
        &ring_update, 1);
    if (ring_registration == 1) {
        ring->enter_fd = (int)ring_update.offset;
        ring->enter_flags = IORING_ENTER_REGISTERED_RING;
        ring->registered_ring_fd = 1;
    }
    ring->sq_ring_size = params.sq_off.array +
                         params.sq_entries * sizeof(unsigned);
    ring->cq_ring_size = params.cq_off.cqes +
                         params.cq_entries * sizeof(struct io_uring_cqe);
    if (params.features & IORING_FEAT_SINGLE_MMAP) {
        if (ring->cq_ring_size > ring->sq_ring_size) {
            ring->sq_ring_size = ring->cq_ring_size;
        }
        ring->cq_ring_size = ring->sq_ring_size;
    }
    ring->sq_ring = mmap(NULL, ring->sq_ring_size, PROT_READ | PROT_WRITE,
                         MAP_SHARED, fd, IORING_OFF_SQ_RING);
    if (ring->sq_ring == MAP_FAILED) {
        metal_uring_clear(ring);
        return -1;
    }
    if (params.features & IORING_FEAT_SINGLE_MMAP) {
        ring->cq_ring = ring->sq_ring;
    } else {
        ring->cq_ring = mmap(NULL, ring->cq_ring_size, PROT_READ | PROT_WRITE,
                             MAP_SHARED, fd, IORING_OFF_CQ_RING);
        if (ring->cq_ring == MAP_FAILED) {
            metal_uring_clear(ring);
            return -1;
        }
    }
    ring->sqes_size = params.sq_entries * sizeof(struct io_uring_sqe);
    ring->sqes = mmap(NULL, ring->sqes_size, PROT_READ | PROT_WRITE,
                      MAP_SHARED, fd, IORING_OFF_SQES);
    if (ring->sqes == MAP_FAILED) {
        metal_uring_clear(ring);
        return -1;
    }
    char *sq = (char *)ring->sq_ring;
    char *cq = (char *)ring->cq_ring;
    ring->sq_head = (unsigned *)(sq + params.sq_off.head);
    ring->sq_tail = (unsigned *)(sq + params.sq_off.tail);
    ring->sq_mask = (unsigned *)(sq + params.sq_off.ring_mask);
    ring->sq_entries = (unsigned *)(sq + params.sq_off.ring_entries);
    ring->sq_array = (unsigned *)(sq + params.sq_off.array);
    ring->cq_head = (unsigned *)(cq + params.cq_off.head);
    ring->cq_tail = (unsigned *)(cq + params.cq_off.tail);
    ring->cq_mask = (unsigned *)(cq + params.cq_off.ring_mask);
    ring->cqes = (struct io_uring_cqe *)(cq + params.cq_off.cqes);
    /* These ring geometry values are immutable after setup. Keep private copies
     * beside the cached heads/tails instead of rereading shared mmap metadata in
     * every SQ/CQ iteration. */
    ring->sq_mask_value = *ring->sq_mask;
    ring->sq_entries_value = *ring->sq_entries;
    ring->cq_mask_value = *ring->cq_mask;
    ring->cached_sq_tail = __atomic_load_n(ring->sq_tail, __ATOMIC_RELAXED);
    ring->published_sq_tail = ring->cached_sq_tail;
    ring->cached_cq_head = __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
    ring->features = params.features;
    ring->setup_flags = params.flags;
    return 0;
}

#include "reactor_buffers.c"

static struct io_uring_sqe *
metal_uring_get_sqe(MetalUring *ring, unsigned *index_out)
{
    unsigned head = __atomic_load_n(ring->sq_head, __ATOMIC_ACQUIRE);
    unsigned tail = ring->cached_sq_tail;
    if (tail - head >= ring->sq_entries_value) {
        errno = EBUSY;
        return NULL;
    }
    unsigned index = tail & ring->sq_mask_value;
    struct io_uring_sqe *sqe = &ring->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    ring->sq_array[index] = index;
    ring->cached_sq_tail = tail + 1;
    ring->pending_submissions++;
    *index_out = index;
    return sqe;
}

static int
metal_uring_queue_accept(MetalUring *ring, int fd, uint64_t token,
                         int multishot)
{
    unsigned index;
    struct io_uring_sqe *sqe = metal_uring_get_sqe(ring, &index);
    if (sqe == NULL) {
        return -1;
    }
    sqe->opcode = IORING_OP_ACCEPT;
    sqe->fd = fd;
    sqe->accept_flags = SOCK_NONBLOCK | SOCK_CLOEXEC;
#ifdef IORING_ACCEPT_MULTISHOT
    if (multishot) {
        sqe->ioprio |= IORING_ACCEPT_MULTISHOT;
    }
#else
    (void)multishot;
#endif
    sqe->user_data = token | METAL_TOKEN_ACCEPT;
    return 0;
}

static int
metal_uring_queue_poll(MetalUring *ring, int fd, uint32_t events,
                       uint64_t token, int multishot)
{
    unsigned index;
    struct io_uring_sqe *sqe = metal_uring_get_sqe(ring, &index);
    if (sqe == NULL) {
        return -1;
    }
    sqe->opcode = IORING_OP_POLL_ADD;
    sqe->fd = fd;
    sqe->poll_events = events;
#ifdef IORING_POLL_ADD_MULTI
    if (multishot) {
        sqe->len |= IORING_POLL_ADD_MULTI;
    }
#else
    (void)multishot;
#endif
    sqe->user_data = token;
    return 0;
}

static int
metal_uring_queue_cancel_raw(MetalUring *ring, uint64_t target_token)
{
    unsigned index;
    struct io_uring_sqe *sqe = metal_uring_get_sqe(ring, &index);
    if (sqe == NULL) {
        return -1;
    }
    sqe->opcode = IORING_OP_ASYNC_CANCEL;
    sqe->addr = target_token;
    sqe->user_data = METAL_CANCEL_TOKEN;
    return 0;
}

static int
metal_uring_queue_receive(MetalUring *ring, int fd, uint64_t token,
                          uint16_t buffer_group, uint32_t buffer_size,
                          int multishot)
{
    unsigned index;
    struct io_uring_sqe *sqe = metal_uring_get_sqe(ring, &index);
    if (sqe == NULL) {
        return -1;
    }
    sqe->opcode = IORING_OP_RECV;
    sqe->fd = fd;
    sqe->len = multishot ? 0 : buffer_size;
    sqe->flags = IOSQE_BUFFER_SELECT;
    sqe->buf_group = buffer_group;
#ifdef IORING_RECV_MULTISHOT
    if (multishot) {
        sqe->ioprio |= IORING_RECV_MULTISHOT;
    }
#else
    (void)multishot;
#endif
    sqe->user_data = token | METAL_TOKEN_RECEIVE;
    return 0;
}

static int
metal_uring_queue_cancel(MetalUring *ring, uint64_t target_token)
{
    unsigned index;
    struct io_uring_sqe *sqe = metal_uring_get_sqe(ring, &index);
    if (sqe == NULL) {
        return -1;
    }
    sqe->opcode = IORING_OP_ASYNC_CANCEL;
    sqe->addr = target_token | METAL_TOKEN_RECEIVE;
    sqe->user_data = METAL_CANCEL_TOKEN;
    return 0;
}

static void
metal_uring_publish_submissions(MetalUring *ring)
{
    if (ring->published_sq_tail != ring->cached_sq_tail) {
        __atomic_store_n(
            ring->sq_tail, ring->cached_sq_tail, __ATOMIC_RELEASE);
        ring->published_sq_tail = ring->cached_sq_tail;
        if (ring->diagnostics) {
            ring->sq_tail_publications++;
        }
    }
}

static int
metal_uring_flush_submissions(MetalUring *ring)
{
    if (ring->pending_submissions == 0) {
        return 0;
    }
    unsigned requested = ring->pending_submissions;
    metal_uring_publish_submissions(ring);
    int entered;
    do {
        if (ring->diagnostics) {
            ring->enter_calls++;
        }
        entered = (int)syscall(SYS_io_uring_enter, ring->enter_fd, requested,
                               0, ring->enter_flags, NULL, 0);
    } while (entered < 0 && errno == EINTR);
    if (entered < 0) {
        return -1;
    }
    if ((unsigned)entered > ring->pending_submissions) {
        errno = EIO;
        return -1;
    }
    ring->pending_submissions -= (unsigned)entered;
    return entered;
}

static int
metal_uring_wait(MetalUring *ring, int timeout_ms)
{
    unsigned requested = ring->pending_submissions;
    metal_uring_publish_submissions(ring);
    int entered;
    if (timeout_ms < 0) {
        do {
            if (ring->diagnostics) {
                ring->enter_calls++;
            }
            entered = (int)syscall(
                SYS_io_uring_enter, ring->enter_fd, requested, 1,
                IORING_ENTER_GETEVENTS | ring->enter_flags, NULL, 0);
        } while (entered < 0 && errno == EINTR);
        if (entered >= 0) {
            if ((unsigned)entered > requested) {
                errno = EIO;
                return -1;
            }
            ring->pending_submissions -= (unsigned)entered;
        }
        return entered;
    }
#ifdef IORING_ENTER_EXT_ARG
    struct __kernel_timespec timeout = {
        .tv_sec = timeout_ms / 1000,
        .tv_nsec = (timeout_ms % 1000) * 1000000L,
    };
    struct io_uring_getevents_arg argument;
    memset(&argument, 0, sizeof(argument));
    argument.ts = (uint64_t)(uintptr_t)&timeout;
    do {
        if (ring->diagnostics) {
            ring->enter_calls++;
        }
        entered = (int)syscall(
            SYS_io_uring_enter, ring->enter_fd, requested, 1,
            IORING_ENTER_GETEVENTS | IORING_ENTER_EXT_ARG |
                ring->enter_flags,
            &argument, sizeof(argument));
    } while (entered < 0 && errno == EINTR);
    if (entered < 0 && errno == ETIME) {
        return 0;
    }
    if (entered >= 0) {
        if ((unsigned)entered > requested) {
            errno = EIO;
            return -1;
        }
        ring->pending_submissions -= (unsigned)entered;
    }
    return entered;
#else
    (void)ring;
    (void)timeout_ms;
    errno = EOPNOTSUPP;
    return -1;
#endif
}

static int
metal_slab_init(MetalSlab *slab, uint32_t capacity)
{
    slab->slots = PyMem_Calloc(capacity, sizeof(MetalSlot));
    if (slab->slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    slab->capacity = capacity;
    slab->free_head = 0;
    slab->occupancy = 0;
    slab->high_water = 0;
    slab->exhaustions = 0;
    slab->generation_wraps = 0;
    slab->stale = 0;
    for (uint32_t i = 0; i < capacity; i++) {
        slab->slots[i].generation_kind = 1;
        slab->slots[i].next_free = i + 1 < capacity ? i + 1 : METAL_SLOT_NONE;
    }
    return 0;
}

static void
metal_slab_clear(MetalSlab *slab)
{
    PyMem_Free(slab->slots);
    memset(slab, 0, sizeof(*slab));
    slab->free_head = METAL_SLOT_NONE;
}

static uint64_t
metal_slab_allocate(MetalSlab *slab, void *owner, uint64_t related, uint16_t kind)
{
    uint32_t index = slab->free_head;
    if (index == METAL_SLOT_NONE) {
        slab->exhaustions++;
        return 0;
    }
    MetalSlot *slot = &slab->slots[index];
    slab->free_head = slot->next_free;
    slot->owner = owner;
    slot->related = related;
    slot->generation_kind = metal_slot_generation(slot) |
                            ((uint32_t)(kind + 1) << METAL_SLOT_KIND_SHIFT);
    slab->occupancy++;
    if (slab->occupancy > slab->high_water) {
        slab->high_water = slab->occupancy;
    }
    return ((uint64_t)metal_slot_generation(slot) << 32) | index;
}

static MetalSlot *
metal_slab_validate(MetalSlab *slab, uint64_t token, void *owner)
{
    uint32_t index = (uint32_t)token;
    uint32_t generation = (uint32_t)(token >> 32);
    if (index >= slab->capacity) {
        slab->stale++;
        return NULL;
    }
    MetalSlot *slot = &slab->slots[index];
    if (!metal_slot_live(slot) || metal_slot_generation(slot) != generation ||
        slot->owner != owner) {
        slab->stale++;
        return NULL;
    }
    return slot;
}

static void
metal_slab_release(MetalSlab *slab, uint64_t token, void *owner)
{
    MetalSlot *slot = metal_slab_validate(slab, token, owner);
    if (slot == NULL) {
        return;
    }
    uint32_t index = (uint32_t)token;
    slot->owner = NULL;
    slot->related = 0;
    uint32_t generation = metal_slot_generation(slot) + 1;
    if (generation >= METAL_GENERATION_LIMIT) {
        generation = 1;
        slab->generation_wraps++;
    }
    slot->generation_kind = generation;
    slot->next_free = slab->free_head;
    slab->free_head = index;
    slab->occupancy--;
}

