/* Native Flight Recorder (NFR) fixed wire schema — C mirror.
 *
 * This header is the C-side twin of src/wreath/_flight_schema.py and MUST agree
 * with it byte-for-byte: same cell size, field offsets, enum values, and flag
 * bits. tests/test_flight_schema.py parses this header and asserts parity with
 * the Python constants, and the _Static_assert lines below fail the build if a
 * struct ever drifts from its 64-byte budget.
 *
 * No recorder, ring, or runtime code lives here. flight.h includes this header.
 *
 * All fields are little-endian. The field order is chosen so the natural C
 * layout on LP64 little-endian targets already matches the packed Python
 * struct with no padding; the static asserts guard that assumption.
 */
#ifndef WREATH_FLIGHT_SCHEMA_H
#define WREATH_FLIGHT_SCHEMA_H

#include <stdint.h>

#define WREATH_NFR_SCHEMA_VERSION 1
#define WREATH_NFR_METADATA_VERSION 1

#define WREATH_NFR_CELL_SIZE 64
#define WREATH_NFR_PHASE_CELL_SIZE 16
#define WREATH_NFR_PHASE_CELL_BUDGET 12
#define WREATH_NFR_PHASE_RECORDS_PER_BATCH 3
#define WREATH_NFR_HISTOGRAM_BUCKETS 64
#define WREATH_NFR_IMAGE_HASH_BYTES 16

/* Forensic capture (Stage 5). Slabs are self-identifying blocks off the ring. */
#define WREATH_NFR_CAPTURE_HASH_BYTES 8
#define WREATH_NFR_CAPTURE_FIELD_ALIGN 4
#define WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE 24
#define WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE 12

/* Reserved: 0 always means none/unknown in every metadata table. */
#define WREATH_NFR_ID_NONE 0

/* EventKind (the `kind` byte of a 64-byte cell). */
enum {
    WREATH_NFR_KIND_INVALID = 0,
    WREATH_NFR_KIND_COMPLETION = 1,
    WREATH_NFR_KIND_CORRELATION = 2,
    WREATH_NFR_KIND_PHASE = 3,
    WREATH_NFR_KIND_CONTROL = 4,
    WREATH_NFR_KIND_CAPTURE = 5,
    WREATH_NFR_KIND_LOG = 6,
    WREATH_NFR_KIND_CLIENT_FACTS = 7
};

/* CaptureFieldClass: the boundary a captured field came from. */
enum {
    WREATH_NFR_CAP_CLASS_UNKNOWN = 0,
    WREATH_NFR_CAP_CLASS_REQUEST_HEADER = 1,
    WREATH_NFR_CAP_CLASS_RESPONSE_HEADER = 2,
    WREATH_NFR_CAP_CLASS_REQUEST_BODY = 3,
    WREATH_NFR_CAP_CLASS_RESPONSE_BODY = 4,
    WREATH_NFR_CAP_CLASS_QUERY_PARAM = 5,
    WREATH_NFR_CAP_CLASS_DB_PARAM = 6,
    WREATH_NFR_CAP_CLASS_DB_ROW = 7,
    WREATH_NFR_CAP_CLASS_OUTBOUND_REQUEST = 8,
    WREATH_NFR_CAP_CLASS_OUTBOUND_RESPONSE = 9,
    WREATH_NFR_CAP_CLASS_OUTBOUND_HTTP_EXCHANGE = 10
};

/* CaptureDisposition: how a field is reduced before it enters a slab. */
enum {
    WREATH_NFR_CAP_RAW = 0,     /* verbatim bytes, bounded by the slab       */
    WREATH_NFR_CAP_HASHED = 1,  /* 8-byte keyed hash, never the bytes        */
    WREATH_NFR_CAP_MASKED = 2,  /* constant mask; only the length is kept    */
    WREATH_NFR_CAP_LENGTH = 3   /* length only (bytes dropped)               */
};

/* Mode. Off performs zero request-path work. */
enum {
    WREATH_NFR_MODE_OFF = 0,
    WREATH_NFR_MODE_PULSE = 1,
    WREATH_NFR_MODE_DETAILED = 2,
    WREATH_NFR_MODE_FORENSIC = 3
};

/* Protocol. */
enum {
    WREATH_NFR_PROTO_UNKNOWN = 0,
    WREATH_NFR_PROTO_HTTP1 = 1,
    WREATH_NFR_PROTO_HTTP2 = 2,
    WREATH_NFR_PROTO_HTTP3 = 3,
    WREATH_NFR_PROTO_WEBSOCKET = 4
};

/* TerminalStatus. */
enum {
    WREATH_NFR_TERM_OK = 0,
    WREATH_NFR_TERM_ERROR = 1,
    WREATH_NFR_TERM_CANCELLED = 2,
    WREATH_NFR_TERM_DISCONNECTED = 3,
    WREATH_NFR_TERM_TIMEOUT = 4,
    WREATH_NFR_TERM_PROTOCOL_ERROR = 5
};

/* LossReason. Every dropped item increments exactly one bounded counter. */
enum {
    WREATH_NFR_LOSS_RING_FULL = 0,
    WREATH_NFR_LOSS_ACTIVE_TABLE_FULL = 1,
    WREATH_NFR_LOSS_PHASE_SCRATCH_FULL = 2,
    WREATH_NFR_LOSS_CAPTURE_POOL_FULL = 3,
    WREATH_NFR_LOSS_EXPORT_QUEUE_FULL = 4,
    WREATH_NFR_LOSS_ENTROPY_EXHAUSTED = 5,
    WREATH_NFR_LOSS_PROPAGATION_INVALID = 6,
    WREATH_NFR_LOSS_BODY_TRUNCATED = 7,
    WREATH_NFR_LOSS_LOG_SCRATCH_FULL = 8,
    WREATH_NFR_LOSS_LOG_ARGS_TRUNCATED = 9,
    WREATH_NFR_LOSS_LOG_SITE_TABLE_FULL = 10,
    WREATH_NFR_LOSS_LOG_SAMPLED = 11,
    WREATH_NFR_LOSS_LOG_OFF_LOOP = 12,
    WREATH_NFR_LOSS_REASON_COUNT = 13
};

/* Completion-cell flag bits. */
#define WREATH_NFR_FLAG_SAMPLED (1u << 0)
#define WREATH_NFR_FLAG_DETAILED_ARMED (1u << 1)
#define WREATH_NFR_FLAG_FORENSIC_ARMED (1u << 2)
#define WREATH_NFR_FLAG_ERROR_PROMOTED (1u << 3)
#define WREATH_NFR_FLAG_SLOW_PROMOTED (1u << 4)
#define WREATH_NFR_FLAG_PROPAGATION_VALID (1u << 5)
#define WREATH_NFR_FLAG_BODY_TRUNCATED (1u << 6)
#define WREATH_NFR_FLAG_TELEMETRY_LOSS (1u << 7)
#define WREATH_NFR_FLAG_HAS_CORRELATION (1u << 8)
#define WREATH_NFR_FLAG_HAS_CLIENT_FACTS (1u << 9)
#define WREATH_NFR_FLAG_POLICY_REFUSED (1u << 10)
#define WREATH_NFR_FLAG_AI_SCRAPING_REFUSED (1u << 11)

/* Client-facts-cell flags. */
#define WREATH_NFR_CLIENT_UA_KNOWN (1u << 0)
#define WREATH_NFR_CLIENT_BOT_CLAIMED (1u << 1)
#define WREATH_NFR_CLIENT_AGENT_VERIFIED (1u << 2)
#define WREATH_NFR_CLIENT_MOBILE_KNOWN (1u << 3)
#define WREATH_NFR_CLIENT_MOBILE (1u << 4)
#define WREATH_NFR_CLIENT_IP_KNOWN (1u << 5)
#define WREATH_NFR_CLIENT_IP_FORWARDED (1u << 6)
#define WREATH_NFR_CLIENT_GEO_KNOWN (1u << 7)
#define WREATH_NFR_CLIENT_IPV6 (1u << 8)

/* A completion/event cell. Mirrors CompletionCell in _flight_schema.py. */
typedef struct {
    uint8_t schema_version; /* offset 0  */
    uint8_t kind;           /* offset 1  */
    uint16_t flags;         /* offset 2  */
    uint32_t status;        /* offset 4  */
    uint64_t request_id;    /* offset 8  */
    uint64_t connection_id; /* offset 16 */
    uint32_t route_id;      /* offset 24 */
    uint32_t plan_id;       /* offset 28 */
    uint64_t duration_us;   /* offset 32 */
    uint64_t bytes_in;      /* offset 40 */
    uint64_t bytes_out;     /* offset 48 */
    uint8_t protocol;       /* offset 56 */
    uint8_t terminal;       /* offset 57 */
    uint8_t error_class;    /* offset 58 */
    uint8_t worker_id;      /* offset 59 */
    uint32_t end_offset_ms; /* offset 60: monotonic end, ms from the worker epoch */
} wreath_nfr_completion_cell;

/* A correlation cell. Mirrors CorrelationCell in _flight_schema.py. */
typedef struct {
    uint8_t schema_version;  /* offset 0  */
    uint8_t kind;            /* offset 1  */
    uint16_t flags;          /* offset 2  */
    uint32_t reserved0;      /* offset 4  */
    uint64_t request_id;     /* offset 8  */
    uint64_t trace_id_hi;    /* offset 16 */
    uint64_t trace_id_lo;    /* offset 24 */
    uint64_t parent_span_id; /* offset 32 */
    uint64_t span_id;        /* offset 40 */
    uint8_t reserved1[16];   /* offset 48 */
} wreath_nfr_correlation_cell;

/* A compact client classification. Raw address, User-Agent, and verified-agent
 * identity deliberately do not cross this boundary. */
typedef struct {
    uint8_t schema_version;       /* offset 0  */
    uint8_t kind;                 /* offset 1  */
    uint16_t flags;               /* offset 2  (WREATH_NFR_CLIENT_*) */
    uint16_t user_agent_rule_id;  /* offset 4  (0 = none) */
    uint8_t country[2];           /* offset 6  (uppercase ISO, or NULs) */
    uint64_t request_id;          /* offset 8  */
    uint8_t reserved[48];         /* offset 16 */
} wreath_nfr_client_facts_cell;

/* A phase (detail) record. 16 bytes; only armed requests write it. Mirrors
 * PhaseRecord in _flight_schema.py. */
typedef struct {
    uint16_t phase_id;        /* offset 0  (PhaseKind)                 */
    uint16_t dependency_id;   /* offset 2  (metadata id, truncated)    */
    uint8_t coverage;         /* offset 4  (PhaseCoverage)             */
    uint8_t sequence;         /* offset 5  (order within the request)  */
    uint16_t reserved;        /* offset 6                              */
    uint32_t start_offset_us; /* offset 8  (from request start)        */
    uint32_t duration_us;     /* offset 12                             */
} wreath_nfr_phase_cell;

/* A 64-byte ring cell carrying up to three phase records for one request.
 * Mirrors PhaseBatchCell in _flight_schema.py. The header shares the leading
 * (schema_version, kind) bytes with every other ring cell so a reader dispatches
 * on `kind`; request_id makes the batch self-identifying. */
typedef struct {
    uint8_t schema_version; /* offset 0  */
    uint8_t kind;           /* offset 1  (WREATH_NFR_KIND_PHASE)          */
    uint8_t count;          /* offset 2  (1..WREATH_NFR_PHASE_RECORDS_PER_BATCH) */
    uint8_t worker_id;      /* offset 3  */
    uint32_t reserved;      /* offset 4  */
    uint64_t request_id;    /* offset 8  */
    wreath_nfr_phase_cell records[WREATH_NFR_PHASE_RECORDS_PER_BATCH]; /* 16,32,48 */
} wreath_nfr_phase_batch_cell;

/* --- log records ---------------------------------------------------------- */

/* Bytes of a log cell given over to packed arguments, and the retained argument
 * count. Mirrors LOG_INLINE_ARG_BYTES / LOG_MAX_ARGS in _flight_schema.py. */
#define WREATH_NFR_LOG_INLINE_ARG_BYTES 32
#define WREATH_NFR_LOG_MAX_ARGS 8

/* Log-cell flag bits. A separate namespace from WREATH_NFR_FLAG_*: these
 * describe one record, not the request it belongs to. */
#define WREATH_NFR_LOG_FLAG_PROMOTED (1u << 0)
#define WREATH_NFR_LOG_FLAG_TRUNCATED (1u << 1)
#define WREATH_NFR_LOG_FLAG_REDACTED (1u << 2)
#define WREATH_NFR_LOG_FLAG_OFF_LOOP (1u << 3)
#define WREATH_NFR_LOG_FLAG_EVENT_FIELDS (1u << 4)

/* OpenTelemetry SeverityNumber, at the base of each band. */
enum {
    WREATH_NFR_SEVERITY_TRACE = 1,
    WREATH_NFR_SEVERITY_DEBUG = 5,
    WREATH_NFR_SEVERITY_INFO = 9,
    WREATH_NFR_SEVERITY_WARN = 13,
    WREATH_NFR_SEVERITY_ERROR = 17,
    WREATH_NFR_SEVERITY_FATAL = 21
};

/* The type tag leading each packed argument. Arguments stay self-describing
 * even though the interned call site already declares their types: one byte
 * apiece buys a decode that validates a stale or torn record instead of
 * trusting it. Mirrors LogArgType in _flight_schema.py. */
enum {
    WREATH_NFR_LOG_ARG_NONE = 0,   /* no payload                              */
    WREATH_NFR_LOG_ARG_BOOL = 1,   /* uint8_t 0/1                             */
    WREATH_NFR_LOG_ARG_INT = 2,    /* int64_t                                 */
    WREATH_NFR_LOG_ARG_FLOAT = 3,  /* double (IEEE 754 binary64)              */
    WREATH_NFR_LOG_ARG_STR = 4,    /* uint8_t length, then UTF-8 bytes        */
    WREATH_NFR_LOG_ARG_HASH = 5,   /* uint64_t keyed SipHash; never the bytes */
    WREATH_NFR_LOG_ARG_LENGTH = 6  /* uint32_t original length, bytes dropped */
};

/* The declared type of one call-site field, as the emitter sees it. A site's
 * fields are flattened at registration into a spec blob -- one byte per field,
 * `(type << 4) | disposition` -- so the native emitter walks a byte string
 * beside the argument tuple instead of reading Python objects to decide how to
 * pack. That flattening is what makes the packing branch on a small integer
 * rather than on `isinstance`. Mirrors LOG_SPEC_* in _flight_schema.py; the
 * disposition nibble is CaptureDisposition, unchanged. */
enum {
    WREATH_NFR_LOG_SPEC_NONE = 0,  /* the value must be None                   */
    WREATH_NFR_LOG_SPEC_BOOL = 1,
    WREATH_NFR_LOG_SPEC_INT = 2,
    WREATH_NFR_LOG_SPEC_FLOAT = 3,
    WREATH_NFR_LOG_SPEC_STR = 4,
    WREATH_NFR_LOG_SPEC_BYTES = 5
};
#define WREATH_NFR_LOG_SPEC_TYPE(byte) ((uint8_t)((byte) >> 4))
#define WREATH_NFR_LOG_SPEC_DISPOSITION(byte) ((uint8_t)((byte) & 0x0F))

/* Redaction dispositions, mirroring CaptureDisposition in _flight_schema.py.
 * RAW writes the value; HASHED writes a keyed fingerprint; MASKED and LENGTH
 * both keep only how long it was. */
enum {
    WREATH_NFR_CAPTURE_RAW = 0,
    WREATH_NFR_CAPTURE_HASHED = 1,
    WREATH_NFR_CAPTURE_MASKED = 2,
    WREATH_NFR_CAPTURE_LENGTH = 3
};

/* A 64-byte ring cell carrying one application log record. Mirrors LogCell in
 * _flight_schema.py. Deliberately carries no trace or span id: the projector
 * joins a record to its trace by request_id, exactly as it joins a phase batch,
 * and log records outnumber completions by one to two orders of magnitude. */
typedef struct {
    uint8_t schema_version;  /* offset 0                                      */
    uint8_t kind;            /* offset 1  (WREATH_NFR_KIND_LOG)               */
    uint16_t flags;          /* offset 2  (WREATH_NFR_LOG_FLAG_*)             */
    uint32_t site_id;        /* offset 4  (interned call site; 0 = none)      */
    uint64_t request_id;     /* offset 8  (0 = not request-scoped)            */
    uint32_t offset_ms;      /* offset 16 (from the worker clock epoch)       */
    uint32_t dropped_siblings; /* offset 20 (limiter drops since the last)    */
    uint8_t severity;        /* offset 24 (OTel SeverityNumber)               */
    uint8_t worker_id;       /* offset 25                                     */
    uint8_t arg_count;       /* offset 26                                     */
    uint8_t arg_bytes;       /* offset 27 (<= WREATH_NFR_LOG_INLINE_ARG_BYTES)*/
    uint32_t reserved;       /* offset 28                                     */
    uint8_t args[WREATH_NFR_LOG_INLINE_ARG_BYTES]; /* offset 32               */
} wreath_nfr_log_cell;

/* --- the ring file (crash forensics) -------------------------------------- */

/* Given a path, the recorder maps its ring from a file with MAP_SHARED rather
 * than allocating it, so the pages belong to the kernel and outlive the process
 * writing to them. A SIGSEGV, a SIGKILL or an abort() therefore leaves the last
 * records -- the ones a post-mortem is about -- readable on disk.
 *
 * This is not durability. MAP_SHARED survives the *process*; it does not
 * survive a machine losing power unless the pages were written back first.
 * Shutdown msyncs, nothing else does, and no doc claims otherwise.
 *
 * Mirrors the ring-file section of _flight_schema.py. The header is one page so
 * the cells that follow start page-aligned, and so the two moving cursors never
 * share a page with a cell. */
#define WREATH_NFR_RING_FILE_MAGIC "WFRR"
#define WREATH_NFR_RING_FILE_VERSION 1
#define WREATH_NFR_RING_FILE_HEADER_BYTES 4096
#define WREATH_NFR_RING_FILE_CURSOR_OFFSET 64

/* Mirrored loss counters, one uint64_t per LossReason, in enum order. A crash
 * file without them is one you cannot draw a conclusion from: "the last thing
 * it served was /orders" means something else when the ring was also full four
 * thousand times and the real last thing never reached the file. */
#define WREATH_NFR_RING_FILE_LOSS_OFFSET 128

/* Fixed provenance, written once when the mapping is made. A decoder reads the
 * geometry and the clock calibration from here because the process that could
 * have answered is, by assumption, gone. */
typedef struct {
    char magic[4];             /* offset 0  (WREATH_NFR_RING_FILE_MAGIC)      */
    uint8_t container_version; /* offset 4                                    */
    uint8_t schema_version;    /* offset 5                                    */
    uint16_t flags;            /* offset 6  (reserved)                        */
    uint32_t ring_records;     /* offset 8  (power of two)                    */
    uint32_t cell_size;        /* offset 12 (WREATH_NFR_CELL_SIZE)            */
    uint32_t worker_id;        /* offset 16                                   */
    uint32_t reserved;         /* offset 20                                   */
    uint64_t epoch_mono_ns;    /* offset 24 (origin of a cell's offset_ms)    */
    uint64_t epoch_unix_ns;    /* offset 32 (its wall-clock pair)             */
    uint64_t created_unix_nano;/* offset 40                                   */
    uint64_t pid;              /* offset 48 (whose crash this was)            */
    uint64_t reserved2;        /* offset 56                                   */
} wreath_nfr_ring_file_header;

/* The two cursors, on their own cache line: the fixed fields above are written
 * once, these move on every publish and every drain. */
typedef struct {
    uint64_t head; /* offset 64 -- the writer's publish cursor */
    uint64_t tail; /* offset 72 -- the reader's consume cursor */
} wreath_nfr_ring_file_cursor;

/* A capture-slab header. Mirrors CaptureSlab's header in _flight_schema.py.
 * A slab holds one armed Forensic request's retained fields; used_bytes covers
 * this header plus every field record, so the sink copies exactly that much. */
typedef struct {
    uint64_t request_id;    /* offset 0  (self-identifying)                */
    uint32_t used_bytes;    /* offset 8  (header + all field records)      */
    uint16_t field_count;   /* offset 12                                   */
    uint8_t schema_version; /* offset 14                                   */
    uint8_t kind;           /* offset 15 (WREATH_NFR_KIND_CAPTURE)         */
    uint8_t worker_id;      /* offset 16                                   */
    uint8_t flags;          /* offset 17 (FLAG_BODY_TRUNCATED bit 6)       */
    uint16_t reserved;      /* offset 18                                   */
    uint32_t reserved2;     /* offset 20                                   */
} wreath_nfr_capture_slab_header;

/* A capture-field header. Mirrors CaptureField's header in _flight_schema.py.
 * Followed by stored_length payload bytes, padded up to CAPTURE_FIELD_ALIGN so
 * the next record header stays naturally aligned. */
typedef struct {
    uint16_t field_class;      /* offset 0  (CaptureFieldClass)            */
    uint16_t descriptor_id;    /* offset 2  (compiled metadata id)         */
    uint8_t disposition;       /* offset 4  (CaptureDisposition)           */
    uint8_t reserved;          /* offset 5                                 */
    uint16_t stored_length;    /* offset 6  (payload bytes in the slab)    */
    uint32_t original_length;  /* offset 8  (true length before redaction) */
} wreath_nfr_capture_field;

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(wreath_nfr_completion_cell) == WREATH_NFR_CELL_SIZE,
               "completion cell must be 64 bytes");
_Static_assert(sizeof(wreath_nfr_correlation_cell) == WREATH_NFR_CELL_SIZE,
               "correlation cell must be 64 bytes");
_Static_assert(sizeof(wreath_nfr_client_facts_cell) == WREATH_NFR_CELL_SIZE,
               "client-facts cell must be 64 bytes");
_Static_assert(sizeof(wreath_nfr_phase_cell) == WREATH_NFR_PHASE_CELL_SIZE,
               "phase cell must be 16 bytes");
_Static_assert(sizeof(wreath_nfr_phase_batch_cell) == WREATH_NFR_CELL_SIZE,
               "phase batch cell must be 64 bytes");
_Static_assert(WREATH_NFR_PHASE_CELL_BUDGET % WREATH_NFR_PHASE_RECORDS_PER_BATCH == 0,
               "phase budget must be a whole number of batches");
_Static_assert(sizeof(wreath_nfr_capture_slab_header) == WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE,
               "capture slab header must be 24 bytes");
_Static_assert(sizeof(wreath_nfr_capture_field) == WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE,
               "capture field header must be 12 bytes");
_Static_assert(sizeof(wreath_nfr_log_cell) == WREATH_NFR_CELL_SIZE,
               "log cell must be 64 bytes");
_Static_assert(sizeof(wreath_nfr_ring_file_header)
                   == WREATH_NFR_RING_FILE_CURSOR_OFFSET,
               "the ring file header must fill the bytes before the cursors");
_Static_assert(sizeof(wreath_nfr_ring_file_cursor) == 16,
               "the ring file cursor pair must be two u64s");
_Static_assert(WREATH_NFR_RING_FILE_HEADER_BYTES % WREATH_NFR_CELL_SIZE == 0,
               "the ring file header must be a whole number of cells, so the "
               "cell area stays aligned");
_Static_assert(WREATH_NFR_RING_FILE_LOSS_OFFSET
                   + WREATH_NFR_LOSS_REASON_COUNT * 8
                   <= WREATH_NFR_RING_FILE_HEADER_BYTES,
               "the mirrored loss counters must fit the ring file header page");
_Static_assert(WREATH_NFR_RING_FILE_LOSS_OFFSET
                   >= WREATH_NFR_RING_FILE_CURSOR_OFFSET
                          + (int)sizeof(wreath_nfr_ring_file_cursor),
               "the loss mirror must not overlap the cursor pair");
_Static_assert(WREATH_NFR_LOG_INLINE_ARG_BYTES == WREATH_NFR_CELL_SIZE - 32,
               "the log cell's inline argument area must fill the cell");
#endif

/* Histogram bucket for a microsecond duration: log2, clamped to a valid bin.
 * Mirrors histogram_bucket() in _flight_schema.py. Branchless past the <=1 guard:
 * floor(log2(x)) == 63 - clz(x) for x >= 2, one instruction on any modern ISA,
 * instead of a per-completion shift loop. HISTOGRAM_BUCKETS is 64 so a 64-bit
 * clz result already lands in range and needs no further clamp. */
static inline int
wreath_nfr_histogram_bucket(uint64_t duration_us)
{
    if (duration_us <= 1) {
        return 0;
    }
#if defined(__GNUC__) || defined(__clang__)
    return 63 - __builtin_clzll(duration_us);
#else
    int bucket = WREATH_NFR_HISTOGRAM_BUCKETS - 1;
    while (bucket > 0 && (duration_us >> bucket) == 0) {
        bucket--;
    }
    return bucket;
#endif
}

#endif /* WREATH_FLIGHT_SCHEMA_H */
