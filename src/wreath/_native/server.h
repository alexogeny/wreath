/* Shared declarations for the wreath._native._server extension.
 *
 * The extension is split into compilation units so HTTP/2 (and later
 * protocols) can be added without growing a single translation unit:
 *   server_common.c  shared globals, awaitables, generic helpers, module init
 *   server_http1.c   HTTP/1.1 connection protocol (Http1Protocol)
 *   server_http2.c   HTTP/2 connection protocol (Http2Protocol)
 *   server_hpack.c   HPACK encoder/decoder for HTTP/2
 *   _servermodule.c  module definition and PyInit
 *
 * This split is behavior-preserving; it must not change HTTP/1.1 semantics.
 */
#ifndef WREATH_NATIVE_SERVER_H
#define WREATH_NATIVE_SERVER_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <math.h>
#include <string.h>

#include "wreathcore.h"

#include "flight.h"

/* The Native Flight Recorder vtable, resolved once at module init from the
 * optional wreath._native._flight capsule. NULL when that extension is absent or
 * telemetry is off, in which case every recorder hook is a not-taken branch. */
extern const WreathFlightCAPI *flight_capi;

/* Resolve the wreath_nfr_worker* a recorder object owns, or NULL for None/error.
 * Used once per connection to attach a borrowed worker to a protocol. */
wreath_nfr_worker *wreath_flight_worker_from(PyObject *recorder);

/* Monotonic clock in nanoseconds for recorder timestamps. */
uint64_t wreath_flight_now_ns(void);

/* A worker-local connection id, assigned once per accepted connection. */
uint64_t wreath_flight_next_connection_id(void);

/* Connection states, mirroring wreath._pure.server. */
enum {
    ST_READING_HEAD = 0,
    ST_READING_FIXED_BODY,
    ST_READING_CHUNK_SIZE,
    ST_READING_CHUNK_DATA,
    ST_READING_CHUNK_TRAILERS,
    ST_REQUEST_RUNNING,
    ST_WS_HANDSHAKE,   /* upgrade dispatched, 101 not yet sent */
    ST_WS_OPEN,        /* WebSocket frames flowing */
    ST_CLOSING
};

/* WebSocket opcodes (RFC 6455). */
enum {
    WS_OP_CONT = 0x0,
    WS_OP_TEXT = 0x1,
    WS_OP_BINARY = 0x2,
    WS_OP_CLOSE = 0x8,
    WS_OP_PING = 0x9,
    WS_OP_PONG = 0xA
};

#define WS_GUID "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

typedef struct {
    PyObject_HEAD

    /* Owned references. */
    PyObject *app;
    PyObject *native_app;  /* bound Wreath._wreath_http or NULL for generic ASGI */
    PyObject *loop;
    PyObject *registry;   /* the server's active-protocol set */
    PyObject *config;
    PyObject *transport;  /* set on connection_made, cleared on connection_lost */
    PyObject *transport_write_fn;
    PyObject *transport_writelines_fn;  /* NULL when the transport lacks it */
    int native_transport;               /* direct WreathTransportCAPI available */
    PyObject *server_address;
    PyObject *client_address;
    PyObject *scheme;
    PyObject *receive_callable;
    PyObject *send_callable;
    PyObject *done_callable;
    PyObject *asgi_metadata;
    PyObject *scope_type;
    PyObject *http_version_10;
    PyObject *http_version_11;
    PyObject *root_path;
    /* config._default_response_headers.headers, resolved once per connection so
     * the egress path does not re-run two GetAttrString calls per response. */
    PyObject *default_response_headers;
    PyObject *loop_create_future;
    PyObject *loop_create_task;
    PyObject *loop_call_later;
    PyObject *deadline_callable;

    /* Extracted configuration limits. */
    Py_ssize_t max_request_line;
    Py_ssize_t max_header_count;
    Py_ssize_t max_header_bytes;
    Py_ssize_t max_body_bytes;
    Py_ssize_t max_body_chunks;
    Py_ssize_t read_high_water;
    Py_ssize_t read_high_water_messages;
    Py_ssize_t max_ws_fragments;
    double keep_alive_timeout;
    double request_timeout;

    /* C-owned input buffer with an explicit cursor. */
    char *buf;
    Py_ssize_t buf_len;
    Py_ssize_t buf_cap;
    Py_ssize_t cursor;

    /* asyncio.BufferedProtocol receive-export state.
     *
     * Invariants:
     * - read_offer_offset + read_offer_size <= buf_cap.
     * - read_offer_size > 0 means one transport read offer is outstanding;
     *   only one offer is accepted at a time, and read_offer_offset == buf_len
     *   for its whole lifetime (data_received() refuses to ingest and
     *   do_consume() defers any buf_len reset while an offer or export is
     *   active).
     * - buf_len advances for buffered reads only in buffer_updated().
     * - While read_exports > 0 the exported address range must stay valid:
     *   buf_reserve() must not PyMem_Realloc, do_consume() must not memmove
     *   or reset, and nothing may free self->buf. Compaction is deferred via
     *   compact_pending and applied at the start of the next get_buffer()
     *   once read_exports == 0.
     * - connection_lost() clears the offer but never frees exported memory;
     *   dealloc cannot run with a live export (each export holds a strong
     *   reference to the protocol via Py_buffer.obj) but still guards,
     *   preferring a leak over a dangling pointer.
     */
    Py_ssize_t read_offer_offset;
    Py_ssize_t read_offer_size;
    Py_ssize_t read_exports;
    int compact_pending;

    int state;

    /* Resumable delimiter scan cursors, one per parser state.
     *
     * Each is an offset relative to `cursor` (never an address, so they survive
     * a buffer reallocation) recording how far that state's delimiter search
     * has already looked. Without them, a byte-at-a-time peer makes every
     * arrival rescan the whole buffered prefix. Reset on entering the state,
     * on consume, and on any reset/upgrade/clear path. */
    Py_ssize_t head_terminator_scan;
    Py_ssize_t request_line_scan;
    Py_ssize_t chunk_line_scan;
    Py_ssize_t trailer_terminator_scan;

    /* Request body decoder. */
    Py_ssize_t remaining;        /* fixed-length bytes still expected */
    Py_ssize_t chunk_remaining;  /* bytes left in the current chunk */
    Py_ssize_t body_received;    /* cumulative bytes in this HTTP request */
    Py_ssize_t body_chunks;      /* non-empty chunks in this HTTP request */
    Py_ssize_t queued_bytes;     /* undelivered buffered body bytes */
    int reading_paused;
    int request_more_body;
    int pending_empty_request;
    int disconnected;

    /* ASGI receive plumbing. The queue owns one reference per entry and uses a
     * head index, so taking the front is O(1) without a Python list allocation. */
    PyObject **receive_queue;
    Py_ssize_t receive_queue_cap;
    Py_ssize_t receive_queue_len;
    Py_ssize_t receive_head;   /* first undelivered message */
    Py_ssize_t queued_messages;/* logical queue length, maintained per op */
    PyObject *receive_waiter;  /* Future or NULL */

    /* Write backpressure. */
    int write_paused;
    PyObject *drain_waiter;    /* Future or NULL */

    /* Current application task. */
    PyObject *task;            /* Task or NULL */

    /* Response encoder state. */
    int response_started;
    int response_complete;
    int response_keep_alive;
    int response_chunked;
    int response_suppress_body;
    int head_written;
    int framing_error;
    int resp_status;
    int resp_has_length;                /* application supplied content-length */
    Py_ssize_t resp_content_length;
    Py_ssize_t response_body_sent;

    /* Private immutable-bytes builder. It is populated before exposure to
     * Python, resized to its exact length, and transferred to the transport. */
    PyObject *response_bytes;
    Py_ssize_t response_bytes_len;
    Py_ssize_t response_bytes_cap;
    char response_inline[512];

    /* Reusable framing scratch retained for errors, chunks, and WebSockets.
     * Normal HTTP response heads and coalesced bodies do not use it. */
    char *out_buf;
    Py_ssize_t out_len;
    Py_ssize_t out_cap;

    /* Per-connection request framing. */
    int http11;         /* 1 for HTTP/1.1, 0 for HTTP/1.0 */
    int method_is_head;

    /* Lazy deadline timer.  One pending loop timer per connection at most;
     * per-request deadline changes only update C fields.  The timer fires,
     * compares against the live deadline, and either acts or re-arms. */
    double deadline;            /* absolute monotonic seconds; INFINITY = none */
    int deadline_is_request;    /* 1: request timeout, 0: keep-alive timeout */
    double timer_target;        /* when the pending timer will fire */
    PyObject *timer_handle;     /* TimerHandle or NULL */

    /* WebSocket state (populated when the connection upgrades). */
    int ws_mode;
    int ws_accepted;
    int ws_close_sent;
    int ws_frag_opcode;         /* -1 when no fragmented message in progress */
    Py_ssize_t ws_frag_size;
    /* Fragments in the message being reassembled, empty ones included: bytes
     * alone cannot bound the per-message work when a fragment carries none. */
    Py_ssize_t ws_frag_count;
    /* One accumulator per fragmented message, not one object per fragment: an
     * empty continuation frame allocates nothing, and what is retained is
     * bounded by max_body_bytes rather than by the frame count. */
    PyObject *ws_frag_buffer;   /* bytearray or NULL */
    PyObject *ws_key;           /* bytes: Sec-WebSocket-Key */

    int closing;
    int accepting;
    int want_next;      /* response finished; resume for the next request */

    /* Native Flight Recorder. nfr_worker is a borrowed pointer (the recorder
     * object outlives the connection) or NULL when telemetry is off. */
    wreath_nfr_worker *nfr_worker;
    uint64_t nfr_connection_id;
    wreath_nfr_context nfr_ctx;
    int nfr_active;     /* a context_start is outstanding for the current request */
    /* Per-request wire byte tallies for the completion cell. Accumulated only
     * while telemetry is on (nfr_worker != NULL) and reset each request, so Off
     * pays nothing. bytes_in counts request-body chunks handed to the app;
     * bytes_out counts every byte written to the transport for the response. */
    uint64_t nfr_bytes_in;
    uint64_t nfr_bytes_out;
    /* The WebSocket ASGI scope dict, retained only for a WS session (NULL for
     * HTTP requests, whose scope is the native _RequestContext or a transient
     * dict). Telemetry stamps route/plan attribution into it during dispatch and
     * the completion cell reads it back; owned, GC-tracked, cleared on teardown. */
    PyObject *nfr_ws_scope;
    /* The armed HTTP request's native _RequestContext, retained only when the
     * request was sampled into Detailed. Its bound `_flight_phase` may escape
     * into tasks that outlive the request (ContextVar propagation to dependency
     * seams), so completion severs the context's borrowed recorder pointers
     * before releasing this reference, turning escaped markers into no-ops. */
    PyObject *nfr_http_scope;
} WreathHttpProtocol;

/* Private zero-allocation ingress API shared with wreath._native._reactor.
 * The generic capsule shape lives in wreath_stream.h so other native stream
 * protocols (PostgreSQL, the HTTP client) can implement the same seam; HTTP/1
 * remains the first implementer under its historical capsule name. */
#include "wreath_stream.h"

#define WREATH_HTTP1_CAPI_NAME "wreath._native._server._HTTP1_C_API"
#define WREATH_HTTP1_CAPI_VERSION WREATH_STREAM_CAPI_VERSION

typedef WreathStreamCAPI WreathHttp1CAPI;

void wreath_http1_protocol_set_type(PyObject *);
int wreath_http1_protocol_check(PyObject *);
int wreath_http1_acquire_read_buffer(PyObject *, char **, Py_ssize_t *);
int wreath_http1_commit_read(PyObject *, Py_ssize_t);
int wreath_http1_feed_external(PyObject *, const char *, Py_ssize_t);

#define WREATH_HTTP2_CAPI_NAME "wreath._native._server._HTTP2_C_API"
#define WREATH_HTTP2_CAPI_VERSION WREATH_STREAM_CAPI_VERSION

typedef WreathStreamCAPI WreathHttp2CAPI;

int wreath_http2_protocol_check(PyObject *);
int wreath_http2_acquire_read_buffer(PyObject *, char **, Py_ssize_t *);
int wreath_http2_commit_read(PyObject *, Py_ssize_t);
int wreath_http2_feed_external(PyObject *, const char *, Py_ssize_t);


/* --- field validation (defined in server_common.c) ----------------------- */
/* RFC 9110 field-name token octets, one table for every ingress path in this
 * module. The copies used to be per-protocol, and the protocols disagreed:
 * HTTP/1.1 rejected CR, LF, NUL and SP in field names and values while HTTP/2
 * checked names for uppercase only and values not at all, so the same process
 * accepted over h2 exactly the octets it refused over h1. That disagreement is
 * a request-splitting primitive for any downstream that re-serializes the
 * headers to HTTP/1.1, so the rule now lives in one place. */
extern const uint8_t wreath_field_token[256];

/* A non-empty RFC 9110 token, any case. */
int wreath_field_name_valid(const char *data, Py_ssize_t size);

/* A field value with no control octet and no DEL; HTAB is permitted. */
int wreath_field_value_valid(const char *data, Py_ssize_t size);

/* Percent-decode a request path into the `str` an ASGI scope's `path` must
 * carry, then require strict UTF-8. Returns a new reference, or NULL; `*bad` is
 * set when the path itself is at fault (an encoded separator, or bytes that are
 * not UTF-8) rather than the interpreter. Shared, because HTTP/2 built its
 * scope from the raw `:path` instead: the same URL routed to a different place
 * depending on which protocol carried it, and `%2F` was refused over h1 and
 * accepted over h2. */
PyObject *wreath_decode_request_path(const char *data, Py_ssize_t size, int *bad);


/* --- shared module globals (defined in server_common.c) ------------------ */
/* Borrowed from the live module; cleared by server_module_free(). */
extern PyObject *disconnect_error;  /* module-private _Disconnect */
extern PyObject *immediate_none;  /* stateless completed awaitable */


/* Cached callables for the per-request hot path. */
extern PyObject *task_add_done_callback;  /* unbound Task.add_done_callback */
extern PyObject *task_exception_fn;  /* unbound Task.exception */
extern PyObject *resume_started_coroutine;  /* Python continuation trampoline */

/* Interned key/value constants so hot dict operations skip per-call string
 * creation and hashing. */
extern PyObject *s_type;
extern PyObject *s_body;
extern PyObject *s_more_body;
extern PyObject *s_status;
extern PyObject *s_headers;
extern PyObject *s_http_request;  /* "http.request" */
extern PyObject *s_http_disconnect;  /* "http.disconnect" */
extern PyObject *s_resp_start;  /* "http.response.start" */
extern PyObject *s_resp_body;  /* "http.response.body" */
extern PyObject *s_wreath_response;  /* "wreath.response" one-shot message */
extern PyObject *k_extensions;
extern PyObject *extensions_dict;  /* {"wreath.response": {}} shared */

/* WebSocket constants and cold-path helpers. */
extern const WreathCoreCAPI *core_capi;  /* C-level parsers from _core */
extern PyObject *sha1_fn;  /* hashlib.sha1 */
extern PyObject *b64encode_fn;  /* base64.b64encode */
extern PyObject *s_websocket;  /* "websocket" scope type */
extern PyObject *s_ws_scheme;  /* "ws" */
extern PyObject *s_wss_scheme;  /* "wss" */
extern PyObject *s_ws_connect;  /* "websocket.connect" */
extern PyObject *s_ws_receive;  /* "websocket.receive" */
extern PyObject *s_ws_disconnect;  /* "websocket.disconnect" */
extern PyObject *s_ws_send_msg;  /* "websocket.send" */
extern PyObject *s_ws_accept_msg;  /* "websocket.accept" */
extern PyObject *s_ws_close_msg;  /* "websocket.close" */
extern PyObject *s_text;
extern PyObject *s_bytes;
extern PyObject *s_code;
extern PyObject *s_reason;
extern PyObject *s_subprotocol;
extern PyObject *k_subprotocols;
extern PyObject *header_host;  /* b"host" */
extern PyObject *k_asgi;
extern PyObject *k_http_version;
extern PyObject *k_method;
extern PyObject *k_scheme;
extern PyObject *k_path;
extern PyObject *k_raw_path;
extern PyObject *k_query_string;
extern PyObject *k_server;
extern PyObject *k_client;
extern PyObject *k_root_path;

extern PyTypeObject ImmediateAwaitableType;
extern PyTypeObject ValueAwaitableType;

int wreath_request_context_ready(PyObject *module);
int wreath_request_context_check(PyObject *object);
/* Seed a dict scope's `_wreath_flight` slot with the recorder's request id, for
 * the protocols that dispatch without a request-context object (HTTP/2, HTTP/3,
 * WebSocket). See the definition in server_request.c for why the id can share
 * the slot Python later overwrites with route attribution. */
int wreath_request_scope_seed_flight(PyObject *scope,
                                     const wreath_nfr_context *nfr_ctx);
PyObject *wreath_request_context_new(
    PyObject *type, PyObject *asgi, PyObject *http_version, PyObject *method,
    PyObject *scheme, PyObject *path, PyObject *raw_path, PyObject *query_string,
    PyObject *headers, PyObject *server, PyObject *client, PyObject *root_path
);
void wreath_request_context_set_flight(PyObject *object, wreath_nfr_context *nfr_ctx,
                                       wreath_nfr_worker *nfr_worker);
int wreath_request_context_set_armed(PyObject *object);
void wreath_request_context_sever(PyObject *object);

/* --- shared helpers (server_common.c) ------------------------------------ */
PyObject *completed_none(void);
PyObject *completed_value(PyObject *value);
Py_ssize_t find_sub(const char *hay, Py_ssize_t n, const char *needle, Py_ssize_t m);
Py_ssize_t find_sub_from(
    const char *hay, Py_ssize_t hay_len, const char *needle, Py_ssize_t needle_len,
    Py_ssize_t *scan_from
);
const char *reason_phrase(int status, Py_ssize_t *size);
int append_raw(PyObject *buffer, const char *data, Py_ssize_t size);
int append_decimal(PyObject *buffer, Py_ssize_t value);
int init_cached_constants(void);
void server_module_free(void *module);

/* --- HTTP/1.1 protocol (server_http1.c) ---------------------------------- */
extern PyType_Spec http_protocol_spec;

/* ======================================================================== */
/* HTTP/2 (RFC 9113) and HPACK (RFC 7541)                                   */
/* ======================================================================== */

#include <stdint.h>

/* RFC 9113 error codes (s7). */
enum {
    H2_NO_ERROR = 0x0,
    H2_PROTOCOL_ERROR = 0x1,
    H2_INTERNAL_ERROR = 0x2,
    H2_FLOW_CONTROL_ERROR = 0x3,
    H2_SETTINGS_TIMEOUT = 0x4,
    H2_STREAM_CLOSED = 0x5,
    H2_FRAME_SIZE_ERROR = 0x6,
    H2_REFUSED_STREAM = 0x7,
    H2_CANCEL = 0x8,
    H2_COMPRESSION_ERROR = 0x9,
    H2_CONNECT_ERROR = 0xA,
    H2_ENHANCE_YOUR_CALM = 0xB,
    H2_INADEQUATE_SECURITY = 0xC,
    H2_HTTP_1_1_REQUIRED = 0xD
};

/* --- HPACK (server_hpack.c) ---------------------------------------------- */

/* One dynamic-table entry; owns interned-ish bytes objects. */
typedef struct {
    PyObject *name;   /* bytes */
    PyObject *value;  /* bytes */
    size_t size;      /* len(name)+len(value)+32 (RFC 7541 s4.1) */
} WreathHpackEntry;

/* HPACK dynamic table used by both decoder and (optionally) encoder. It is a
 * simple growable ring of most-recent-first entries, bounded by cur_max. */
typedef struct {
    WreathHpackEntry *entries;  /* ring buffer */
    Py_ssize_t cap;          /* allocated slots */
    Py_ssize_t count;        /* live entries */
    Py_ssize_t head;         /* index of newest entry */
    size_t size;             /* current byte size */
    size_t cur_max;          /* current max (after size updates) */
    size_t hard_max;         /* SETTINGS_HEADER_TABLE_SIZE limit */
} WreathHpackTable;

int wreath_hpack_build_huffman(void);  /* build the shared decode tree once */
void wreath_hpack_free_huffman(void);

int wreath_hpack_table_init(WreathHpackTable *t, size_t hard_max);
void wreath_hpack_table_clear(WreathHpackTable *t);
void wreath_hpack_table_set_hard_max(WreathHpackTable *t, size_t hard_max);

/* Decode a complete header block into out_list as (name_bytes, value_bytes)
 * tuples. Returns 0 on success. On failure returns -1 and sets *h2_error to the
 * connection error code (typically H2_COMPRESSION_ERROR). Does not set a Python
 * exception for protocol errors; a -1 with *h2_error == 0 means a real Python
 * error is set (e.g. MemoryError). */
int wreath_hpack_decode(WreathHpackTable *t, const uint8_t *data, Py_ssize_t len,
                        Py_ssize_t max_header_count,
                        Py_ssize_t max_header_list,
                        PyObject *out_list, int *h2_error);

/* Append one response header to `out` (a bytearray) as an HPACK literal without
 * indexing (new name), Huffman-encoded when it is not larger. Returns 0/-1. */
int wreath_hpack_encode_literal(PyObject *out, const uint8_t *name, Py_ssize_t nlen,
                             const uint8_t *value, Py_ssize_t vlen);

/* --- HTTP/2 protocol (server_http2.c) ------------------------------------ */
extern PyType_Spec http2_protocol_spec;
int wreath_http2_ready(PyObject *module);  /* build types + register Http2Protocol */

#endif /* WREATH_NATIVE_SERVER_H */
