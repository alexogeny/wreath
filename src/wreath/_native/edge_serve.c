/* The reverse proxy's request path, with no Python on it.
 *
 * `wreath.edge.serve()` installs `EdgeProtocol` on a listening socket and hands
 * it a compiled `UpstreamTable` whose connections were opened while the proxy
 * was being configured. From then on a forwarded request never enters Python:
 * bytes arrive in `buffer_updated`, the head is parsed in place, the outbound
 * head is built into a byte buffer, an already-open upstream transport is
 * written to, and the response is relayed back out. No scope, no `Request`, no
 * coroutine, no Task.
 *
 * Why that is the design and not an optimisation. Measured on one machine, 32
 * connections, CPU-microseconds per forwarded request: `wreath.edge` through the
 * ASGI path cost 117, single-threaded haproxy 26, nginx 23 -- while wreath's own
 * server *answered* a request for 13. The gap was never the language or the
 * syscalls (4.6 against haproxy's 4.8); it was the Python orchestration between
 * primitives that were already in C. Moving one leaf of that orchestration into
 * C was tried first and measured nothing (113.2 against 110.1-117.2), which is
 * the result that says the shape has to change rather than the leaves.
 *
 * The connections are pre-warmed because `loop.create_connection` is a
 * coroutine. Reaching for one mid-request pulls asyncio's Task and Future
 * machinery straight back onto the path this file exists to keep clear -- 6.3us
 * for the Task alone. Opening them is Python's half of the job and happens once.
 *
 * What is deliberately not here yet, so nobody reads more into the absence than
 * is there: TLS to the upstream, retrying a failed attempt on a second upstream,
 * protocol upgrades (WebSocket), and HTTP/2 in either direction. Request bodies
 * are buffered under `max_body` rather than streamed, which is the same bound
 * the Python proxy documents. `serve()` refuses at configuration time for the
 * ones that are refusable.
 */

#include "edge.h"
#include "wreath_stream.h"

#include <stdint.h>
#include <string.h>


/* Per-read receive target handed to the transport, matching the server's. */
#define EDGE_RECV_CHUNK 32768

/* Largest request head accepted, and the most fields in one. Over either, 431:
 * a head this size is a client that has lost the plot, and the array below is
 * what keeps parsing a single pass with no allocation. */
#define EDGE_MAX_HEAD 65536
#define EDGE_MAX_HEADERS 128

/* Selection policies, compiled from `UpstreamPool.policy` once. */
#define EDGE_POLICY_EWMA 0
#define EDGE_POLICY_ROUND_ROBIN 1
#define EDGE_POLICY_LEAST_CONNECTIONS 2

/* How much of the old average a new sample leaves behind, and what an upstream's
 * latency is assumed to be before it has served anything. Both mirror
 * `wreath/edge/upstream.py`, which is where the reasoning lives. */
#define EDGE_EWMA_ALPHA 0.2
#define EDGE_COLD_LATENCY 0.050

/* Downstream connection states. */
enum {
    EC_HEAD = 0,        /* accumulating the request head */
    EC_BODY_FIXED,      /* a declared Content-Length still to arrive */
    EC_BODY_SIZE,       /* chunked: the size line */
    EC_BODY_DATA,       /* chunked: chunk octets */
    EC_BODY_DATA_CRLF,  /* chunked: the CRLF after a chunk */
    EC_BODY_TRAILER,    /* chunked: trailer section */
    EC_WAITING,         /* dispatched, or queued for an upstream connection */
    EC_CLOSED
};

/* Upstream connection states. */
enum {
    EU_IDLE = 0,
    EU_HEAD,            /* accumulating the response head */
    EU_FIXED,           /* relaying a declared Content-Length */
    EU_CHUNK_SIZE,
    EU_CHUNK_DATA,
    EU_CHUNK_DATA_CRLF,
    EU_CHUNK_TRAILER,
    EU_UNTIL_CLOSE,     /* no framing: the body ends when the connection does */
    EU_DONE             /* the response is complete and not yet settled */
};


/* --- byte buffer ---------------------------------------------------------- */

/* A grow-only byte buffer with a consumed prefix.
 *
 * `pinned` suspends compaction. Header slices are held as offsets into the
 * buffer while a request body is being read, and moving the bytes under them
 * would invalidate every one -- reallocation is fine, since offsets survive it,
 * but sliding the contents to the front is not.
 */
typedef struct {
    char *data;
    Py_ssize_t len;     /* bytes written */
    Py_ssize_t cap;
    Py_ssize_t start;   /* bytes already consumed from the front */
    int pinned;
} EdgeBuf;


static void
ebuf_free(EdgeBuf *b)
{
    PyMem_Free(b->data);
    b->data = NULL;
    b->len = b->cap = b->start = 0;
    b->pinned = 0;
}


/* Slide the unconsumed tail to the front, moving `cursor` with it.
 *
 * Compaction is explicit rather than folded into `ebuf_reserve` because the
 * parser keeps an offset into this buffer -- the resume point for the CRLFCRLF
 * search -- and sliding the bytes under it without adjusting it would restart
 * the scan somewhere arbitrary. Reallocation needs no such care, which is why
 * only this half is separated out.
 */
static void
ebuf_compact(EdgeBuf *b, Py_ssize_t *cursor)
{
    if (b->pinned || b->start == 0) {
        return;
    }
    Py_ssize_t remaining = b->len - b->start;
    if (remaining > 0) {
        memmove(b->data, b->data + b->start, (size_t)remaining);
    }
    if (cursor != NULL) {
        *cursor = *cursor > b->start ? *cursor - b->start : 0;
    }
    b->len = remaining;
    b->start = 0;
}


static int
ebuf_reserve(EdgeBuf *b, Py_ssize_t extra)
{
    if (b->cap - b->len >= extra) {
        return 0;
    }
    Py_ssize_t needed = b->len + extra;
    Py_ssize_t cap = b->cap ? b->cap : 4096;
    while (cap < needed) {
        cap += cap >> 1;        /* geometric: n appends stay O(n) */
    }
    char *grown = PyMem_Realloc(b->data, (size_t)cap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    b->data = grown;
    b->cap = cap;
    return 0;
}


static int
ebuf_add(EdgeBuf *b, const char *data, Py_ssize_t size)
{
    if (ebuf_reserve(b, size) < 0) {
        return -1;
    }
    memcpy(b->data + b->len, data, (size_t)size);
    b->len += size;
    return 0;
}


#define EBUF_LIT(b, s) ebuf_add((b), (s), (Py_ssize_t)(sizeof(s) - 1))


static int
ebuf_add_ssize(EdgeBuf *b, Py_ssize_t value)
{
    char digits[24];
    int n = 0;
    if (value == 0) {
        digits[n++] = '0';
    }
    while (value > 0) {
        digits[n++] = (char)('0' + (value % 10));
        value /= 10;
    }
    if (ebuf_reserve(b, n) < 0) {
        return -1;
    }
    for (int i = n - 1; i >= 0; i--) {
        b->data[b->len++] = digits[i];
    }
    return 0;
}


static void
ebuf_reset(EdgeBuf *b)
{
    b->len = 0;
    b->start = 0;
    b->pinned = 0;
}


/* --- transport ------------------------------------------------------------ */

static const WreathTransportCAPI *transport_capi = NULL;
static int transport_capi_tried = 0;


static void
load_transport_capi(void)
{
    if (transport_capi_tried) {
        return;
    }
    transport_capi_tried = 1;
    /* native-lint: allow NC004 -- one-time sibling-extension C API resolution */
    transport_capi = PyCapsule_Import(WREATH_TRANSPORT_CAPI_NAME, 0);
    if (transport_capi == NULL) {
        PyErr_Clear();
    }
}


/* Everything either protocol needs to put bytes on a socket. Kept as one struct
 * because the two directions are the same problem and the metal transport's
 * direct write is the whole reason for the `native` flag. */
typedef struct {
    PyObject *transport;
    PyObject *write_fn;
    int native;
    int closing;
} EdgeSink;


static void
sink_bind(EdgeSink *sink, PyObject *transport)
{
    Py_INCREF(transport);
    Py_XSETREF(sink->transport, transport);
    sink->closing = 0;
    sink->native = 0;
    if (strcmp(Py_TYPE(transport)->tp_name,
               "wreath._native._reactor.SocketTransport") == 0) {
        load_transport_capi();
        sink->native = transport_capi != NULL &&
            transport_capi->version == WREATH_TRANSPORT_CAPI_VERSION &&
            transport_capi->check(transport);
    }
    PyObject *write_fn = PyObject_GetAttrString(transport, "write");
    if (write_fn == NULL) {
        PyErr_Clear();
    }
    Py_XSETREF(sink->write_fn, write_fn);
}


static int
sink_write(EdgeSink *sink, const char *data, Py_ssize_t size)
{
    if (sink->transport == NULL || sink->closing || size <= 0) {
        return 0;
    }
    PyObject *payload = PyBytes_FromStringAndSize(data, size);
    if (payload == NULL) {
        return -1;
    }
    int rc = 0;
    if (sink->native && transport_capi != NULL) {
        rc = transport_capi->write(sink->transport, payload);
    }
    else if (sink->write_fn != NULL) {
        PyObject *result = PyObject_CallOneArg(sink->write_fn, payload);
        if (result == NULL) {
            rc = -1;
        }
        else {
            Py_DECREF(result);
        }
    }
    Py_DECREF(payload);
    return rc;
}


static void
sink_close(EdgeSink *sink)
{
    if (sink->transport == NULL || sink->closing) {
        return;
    }
    sink->closing = 1;
    PyObject *result = PyObject_CallMethod(sink->transport, "close", NULL);
    if (result == NULL) {
        PyErr_Clear();
    }
    else {
        Py_DECREF(result);
    }
}


static void
sink_clear(EdgeSink *sink)
{
    Py_CLEAR(sink->transport);
    Py_CLEAR(sink->write_fn);
}


static double
edge_now(void)
{
    PyTime_t now = 0;
    (void)PyTime_MonotonicRaw(&now);
    return (double)now * 1e-9;
}


/* --- types ---------------------------------------------------------------- */

typedef struct EdgeClient EdgeClient;
typedef struct EdgeConn EdgeConn;
typedef struct EdgeTable EdgeTable;


/* One origin, its health, and the connections already open to it. */
typedef struct {
    PyObject *authority;    /* bytes: the outbound `Host` value */
    Py_ssize_t inflight;
    double latency;
    int failures;
    double ejected_until;
    Py_ssize_t total;
    Py_ssize_t open;        /* established connections */
    EdgeConn *free_head;    /* idle connections, intrusive */
    EdgeClient *wait_head;  /* requests waiting for one, FIFO */
    EdgeClient *wait_tail;
    Py_ssize_t waiting;
} EdgeUpstream;


struct EdgeTable {
    PyObject_HEAD
    EdgeUpstream *ups;
    Py_ssize_t count;
    Py_ssize_t cursor;
    int policy;
    int eject_failures;
    double eject_seconds;
    double eject_cap;
    Py_ssize_t max_body;
    PyObject *via;          /* bytes, e.g. b"1.1 wreath" */
    PyObject *scheme;       /* bytes, b"http" or b"https" */
    PyObject *on_lost;      /* called with an upstream index, or None */
    PyObject *live;         /* set of every open EdgeClient and EdgeConn */
    int closing;
};


struct EdgeClient {
    PyObject_HEAD
    EdgeTable *table;
    EdgeSink sink;
    EdgeBuf in;
    EdgeBuf out;            /* the outbound request, head then body */
    EdgeBuf body;           /* a chunked request body, decoded */
    Py_ssize_t offer_offset;
    Py_ssize_t offer_size;
    int exports;
    Py_ssize_t head_scan;   /* resume point for the CRLFCRLF search (NC006) */
    Py_ssize_t head_start;  /* a pinned head's first octet, while chunked */
    Py_ssize_t head_end;    /* offset of the CRLF that ends the head */
    Py_ssize_t body_need;   /* octets still expected */
    int state;
    int close_after;        /* the client asked for `Connection: close` */
    int no_body_expected;   /* HEAD: the response carries no body */
    int keep_alive;
    EdgeConn *conn;         /* the upstream connection serving this request */
    EdgeClient *wait_next;
    Py_ssize_t queued_on;   /* upstream index this is queued against, or -1 */
    char peer[64];
    Py_ssize_t peer_len;
};


struct EdgeConn {
    PyObject_HEAD
    EdgeTable *table;
    Py_ssize_t index;
    EdgeSink sink;
    EdgeBuf in;
    EdgeBuf out;
    Py_ssize_t offer_offset;
    Py_ssize_t offer_size;
    int exports;
    Py_ssize_t head_scan;
    Py_ssize_t body_need;
    int state;
    int upstream_close;     /* the origin asked for `Connection: close` */
    int response_chunked;
    double started;
    EdgeClient *client;     /* the request being served, or NULL */
    EdgeConn *free_next;
    int in_free_list;
    /* Whether this connection currently counts against its upstream's
     * `inflight`. Kept as a flag rather than inferred from `client != NULL`,
     * because a client can disconnect mid-response and leave the connection
     * legitimately busy with nobody to hand the bytes to. */
    int counted;
};


static PyTypeObject *edge_table_type = NULL;
static PyTypeObject *edge_client_type = NULL;
static PyTypeObject *edge_conn_type = NULL;

static int edge_client_drive(EdgeClient *self);
static int edge_conn_drive(EdgeConn *self);
static int edge_dispatch(EdgeClient *self);
static void edge_release_conn(EdgeConn *conn, int reusable);


/* --- upstream selection --------------------------------------------------- */

/* Lower is better: latency times queue depth. Peak-EWMA rather than plain
 * least-connections or plain least-latency, because one upstream with two fast
 * requests in flight should beat one with a single slow one and neither signal
 * alone says that. Mirrors `Upstream.score()`. */
static double
edge_score(const EdgeUpstream *u)
{
    return u->latency * (double)(u->inflight + 1);
}


/* The upstream to send the next request to, or -1 when none qualifies.
 *
 * `need_free` asks for one with an idle connection. The caller tries with it
 * set and then without: preferring an upstream that can be written to *now*
 * keeps a saturated origin from queueing work an idle one would have taken,
 * and falling back keeps selection honest when every origin is busy.
 */
static Py_ssize_t
edge_choose(EdgeTable *table, double now, int need_free)
{
    Py_ssize_t best = -1;
    double best_score = 0.0;
    Py_ssize_t untried = -1;
    Py_ssize_t healthy = 0;
    Py_ssize_t soonest = -1;

    for (Py_ssize_t i = 0; i < table->count; i++) {
        EdgeUpstream *u = &table->ups[i];
        if (u->open == 0) {
            continue;
        }
        if (need_free && u->free_head == NULL) {
            continue;
        }
        if (soonest < 0 || u->ejected_until < table->ups[soonest].ejected_until) {
            soonest = i;
        }
        if (u->ejected_until > now) {
            continue;
        }
        healthy++;
        /* An upstream that has never served anything is chosen before any
         * scoring runs, or it starves permanently: its latency is the cold
         * guess, and the moment a warm upstream measures faster than that guess
         * the cold one stops being selected and so never gets a measurement. */
        if (u->total == 0 && untried < 0) {
            untried = i;
        }
        double score;
        switch (table->policy) {
        case EDGE_POLICY_ROUND_ROBIN:
            score = 0.0;
            break;
        case EDGE_POLICY_LEAST_CONNECTIONS:
            score = (double)u->inflight;
            break;
        default:
            score = edge_score(u);
            break;
        }
        if (best < 0 || score < best_score) {
            best = i;
            best_score = score;
        }
    }
    if (untried >= 0) {
        return untried;
    }
    if (healthy == 0) {
        /* Every upstream ejected is not a failure to answer: refusing while
         * each origin is briefly out turns a recoverable blip into an outage of
         * the proxy's own, and the request declined is the one that would have
         * proved recovery. */
        return soonest;
    }
    if (table->policy == EDGE_POLICY_ROUND_ROBIN && best >= 0) {
        for (Py_ssize_t step = 1; step <= table->count; step++) {
            Py_ssize_t i = (table->cursor + step) % table->count;
            EdgeUpstream *u = &table->ups[i];
            if (u->open == 0 || u->ejected_until > now) {
                continue;
            }
            if (need_free && u->free_head == NULL) {
                continue;
            }
            table->cursor = i;
            return i;
        }
    }
    return best;
}


static void
edge_succeeded(EdgeTable *table, Py_ssize_t index, double elapsed)
{
    EdgeUpstream *u = &table->ups[index];
    u->failures = 0;
    u->ejected_until = 0.0;
    u->total++;
    u->latency += EDGE_EWMA_ALPHA * (elapsed - u->latency);
}


static void
edge_failed(EdgeTable *table, Py_ssize_t index, double now)
{
    EdgeUpstream *u = &table->ups[index];
    u->failures++;
    if (u->failures < table->eject_failures) {
        return;
    }
    int over = u->failures - table->eject_failures;
    double cooldown = table->eject_seconds * (double)(1 << (over > 20 ? 20 : over));
    if (cooldown > table->eject_cap) {
        cooldown = table->eject_cap;
    }
    u->ejected_until = now + cooldown;
}


/* --- error replies -------------------------------------------------------- */

/* One canned reply, then close. Every one of these is a framing decision the
 * request path has already made, so there is nothing to negotiate afterwards
 * and keeping the connection would only invite the next message to be read
 * against a boundary this end no longer trusts. */
static int
edge_refuse_error(EdgeClient *self, int status, const char *error)
{
    const char *line;
    switch (status) {
    case 400: line = "HTTP/1.1 400 Bad Request\r\n"; break;
    case 413: line = "HTTP/1.1 413 Content Too Large\r\n"; break;
    case 431: line = "HTTP/1.1 431 Request Header Fields Too Large\r\n"; break;
    case 501: line = "HTTP/1.1 501 Not Implemented\r\n"; break;
    case 502: line = "HTTP/1.1 502 Bad Gateway\r\n"; break;
    default:  line = "HTTP/1.1 500 Internal Server Error\r\n"; break;
    }
    static const char tail[] =
        "content-length: 0\r\nconnection: close\r\n\r\n";
    EdgeBuf *reply = &self->out;
    ebuf_reset(reply);
    if (ebuf_add(reply, line, (Py_ssize_t)strlen(line)) < 0) {
        return -1;
    }
    if (error != NULL) {
        const char *via = PyBytes_AS_STRING(self->table->via);
        Py_ssize_t via_len = PyBytes_GET_SIZE(self->table->via);
        const char *proxy = memchr(via, ' ', (size_t)via_len);
        Py_ssize_t proxy_len;
        if (proxy == NULL) {
            proxy = via;
            proxy_len = via_len;
        }
        else {
            proxy++;
            proxy_len = via_len - (Py_ssize_t)(proxy - via);
        }
        if (EBUF_LIT(reply, "proxy-status: ") < 0
            || ebuf_add(reply, proxy, proxy_len) < 0
            || EBUF_LIT(reply, ";error=") < 0
            || ebuf_add(reply, error, (Py_ssize_t)strlen(error)) < 0
            || EBUF_LIT(reply, "\r\n") < 0) {
            return -1;
        }
    }
    if (ebuf_add(reply, tail, (Py_ssize_t)(sizeof(tail) - 1)) < 0) {
        return -1;
    }
    self->state = EC_CLOSED;
    if (sink_write(&self->sink, reply->data, reply->len) < 0) {
        return -1;
    }
    sink_close(&self->sink);
    return 0;
}


static int
edge_refuse(EdgeClient *self, int status)
{
    return edge_refuse_error(self, status, NULL);
}


/* --- request head parsing ------------------------------------------------- */

typedef struct {
    Py_ssize_t name;
    Py_ssize_t name_len;
    Py_ssize_t value;
    Py_ssize_t value_len;
} EdgeSlice;


static int
edge_is_space(char c)
{
    return c == ' ' || c == '\t';
}


static int
edge_token_eq(const char *p, Py_ssize_t n, const char *lit, Py_ssize_t lit_n)
{
    return n == lit_n && memcmp(p, lit, (size_t)n) == 0;
}


/* ASCII case-insensitive compare against a lowercase literal. Written out
 * rather than reaching for `strncasecmp`, which is POSIX-only and locale-aware;
 * neither is wanted for a field value on the wire. */
static int
edge_ieq(const char *p, Py_ssize_t n, const char *lit, Py_ssize_t lit_n)
{
    if (n != lit_n) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        char c = p[i];
        if (c >= 'A' && c <= 'Z') {
            c = (char)(c + 32);
        }
        if (c != lit[i]) {
            return 0;
        }
    }
    return 1;
}


/* Is `option` one of the comma-separated tokens in a `Connection` value?
 *
 * `Connection: close` and `Connection: close, x-secret` mean the same thing
 * about the connection, so this cannot be a whole-value compare -- doing that
 * leaves a proxy holding open a socket the client asked it to close, which
 * presents as a hang rather than as a wrong answer.
 */
static int
edge_connection_has(const char *value, Py_ssize_t len,
                    const char *option, Py_ssize_t option_len)
{
    Py_ssize_t i = 0;
    while (i < len) {
        Py_ssize_t start = i;
        while (i < len && value[i] != ',') i++;
        Py_ssize_t end = i;
        if (i < len) i++;
        while (start < end && (unsigned char)value[start] <= ' ') start++;
        while (end > start && (unsigned char)value[end - 1] <= ' ') end--;
        if (edge_ieq(value + start, end - start, option, option_len)) {
            return 1;
        }
    }
    return 0;
}


/* Strict decimal, refusing everything a lenient parser would let through.
 *
 * A `Content-Length` a proxy and an origin can read differently is the whole of
 * request smuggling, so `+5`, ` 5`, `0x5` and `5,5` are all rejected here rather
 * than normalised into something the next hop might disagree about. */
static int
edge_parse_decimal(const char *p, Py_ssize_t n, Py_ssize_t *out)
{
    if (n <= 0 || n > 18) {
        return -1;
    }
    Py_ssize_t value = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        if (p[i] < '0' || p[i] > '9') {
            return -1;
        }
        value = value * 10 + (p[i] - '0');
    }
    *out = value;
    return 0;
}


static int
edge_sf_key_start(char c)
{
    return (c >= 'a' && c <= 'z') || c == '*';
}


static int
edge_sf_key_char(char c)
{
    return edge_sf_key_start(c) || (c >= '0' && c <= '9')
        || c == '_' || c == '-' || c == '.';
}


static int
edge_sf_token_char(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
        || (c >= '0' && c <= '9') || c == '!' || c == '#'
        || c == '$' || c == '%' || c == '&' || c == '\'' || c == '*'
        || c == '+' || c == '-' || c == '.' || c == '^' || c == '_'
        || c == '`' || c == '|' || c == '~' || c == ':' || c == '/';
}


static int
edge_sf_base64(const char *value, Py_ssize_t len)
{
    Py_ssize_t data_len = len;
    int padding = 0;
    while (data_len > 0 && value[data_len - 1] == '=') {
        data_len--;
        padding++;
    }
    if (padding > 2) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < data_len; i++) {
        char c = value[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
              || (c >= '0' && c <= '9') || c == '+' || c == '/')) {
            return 0;
        }
    }
    if (data_len + padding != len || data_len % 4 == 1) {
        return 0;
    }
    if (padding == 0) {
        return 1;
    }
    return len % 4 == 0 && data_len % 4 == 4 - padding;
}


static int
edge_sf_lower_hex(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    return -1;
}


static int
edge_sf_display_string(const char *value, Py_ssize_t len)
{
    if (len < 3 || value[0] != '%' || value[1] != '"'
        || value[len - 1] != '"') {
        return 0;
    }
    int continuation = 0;
    unsigned char continuation_min = 0x80;
    unsigned char continuation_max = 0xbf;
    for (Py_ssize_t i = 2; i < len - 1; i++) {
        unsigned char c = (unsigned char)value[i];
        if (c == '%') {
            if (i + 2 >= len - 1) {
                return 0;
            }
            int high = edge_sf_lower_hex(value[i + 1]);
            int low = edge_sf_lower_hex(value[i + 2]);
            if (high < 0 || low < 0) {
                return 0;
            }
            c = (unsigned char)((high << 4) | low);
            i += 2;
        }
        else if (!(c == 0x20 || c == 0x21 || c == 0x23 || c == 0x24
                   || (c >= 0x26 && c <= 0x7e))) {
            return 0;
        }

        if (continuation > 0) {
            if (c < continuation_min || c > continuation_max) {
                return 0;
            }
            continuation--;
            continuation_min = 0x80;
            continuation_max = 0xbf;
        }
        else if (c <= 0x7f) {
            continue;
        }
        else if (c >= 0xc2 && c <= 0xdf) {
            continuation = 1;
        }
        else if (c == 0xe0) {
            continuation = 2;
            continuation_min = 0xa0;
        }
        else if ((c >= 0xe1 && c <= 0xec) || (c >= 0xee && c <= 0xef)) {
            continuation = 2;
        }
        else if (c == 0xed) {
            continuation = 2;
            continuation_max = 0x9f;
        }
        else if (c == 0xf0) {
            continuation = 3;
            continuation_min = 0x90;
        }
        else if (c >= 0xf1 && c <= 0xf3) {
            continuation = 3;
        }
        else if (c == 0xf4) {
            continuation = 3;
            continuation_max = 0x8f;
        }
        else {
            return 0;
        }
    }
    return continuation == 0;
}


static int
edge_sf_parameter_bare(const char *value, Py_ssize_t len)
{
    if (len <= 0) {
        return 0;
    }
    if (value[0] == '?') {
        return len == 2 && (value[1] == '0' || value[1] == '1');
    }
    if (edge_sf_key_start(value[0]) || (value[0] >= 'A' && value[0] <= 'Z')) {
        for (Py_ssize_t i = 1; i < len; i++) {
            if (!edge_sf_token_char(value[i])) {
                return 0;
            }
        }
        return 1;
    }
    if (value[0] == ':') {
        if (len < 2 || value[len - 1] != ':') {
            return 0;
        }
        return edge_sf_base64(value + 1, len - 2);
    }
    Py_ssize_t i = 0;
    int is_date = 0;
    if (value[i] == '@') {
        is_date = 1;
        i++;
        if (i >= len) {
            return 0;
        }
    }
    if (value[i] == '-') {
        i++;
    }
    if (i >= len) {
        return 0;
    }
    Py_ssize_t integer_digits = 0;
    while (i < len && value[i] >= '0' && value[i] <= '9') {
        integer_digits++;
        i++;
    }
    if (integer_digits == 0) {
        return 0;
    }
    if (is_date) {
        return i == len && integer_digits <= 15;
    }
    if (i == len) {
        return integer_digits <= 15;
    }
    if (value[i++] != '.' || integer_digits > 12) {
        return 0;
    }
    Py_ssize_t fraction_digits = 0;
    while (i < len && value[i] >= '0' && value[i] <= '9') {
        fraction_digits++;
        i++;
    }
    return i == len && fraction_digits >= 1 && fraction_digits <= 3;
}


static int
edge_incremental_true(const char *value, Py_ssize_t len)
{
    /* The generic Structured Fields owner is Python and cannot be entered from
     * this request path. Parse only the Boolean Item shape RFC 10036 needs. */
    if (len < 2 || value[0] != '?' || value[1] != '1') {
        return 0;
    }
    Py_ssize_t i = 2;
    while (i < len) {
        if (value[i++] != ';' || i >= len) {
            return 0;
        }
        while (i < len && value[i] == ' ') {
            i++;
        }
        if (i >= len || !edge_sf_key_start(value[i])) {
            return 0;
        }
        i++;
        while (i < len && edge_sf_key_char(value[i])) {
            i++;
        }
        if (i >= len || value[i] == ';') {
            continue;
        }
        if (value[i++] != '=' || i >= len) {
            return 0;
        }
        if (value[i] == '"') {
            i++;
            int closed = 0;
            while (i < len) {
                unsigned char c = (unsigned char)value[i++];
                if (c == '"') {
                    closed = 1;
                    break;
                }
                if (c == '\\') {
                    if (i >= len || (value[i] != '"' && value[i] != '\\')) {
                        return 0;
                    }
                    i++;
                }
                else if (c < 0x20 || c > 0x7e) {
                    return 0;
                }
            }
            if (!closed) {
                return 0;
            }
        }
        else if (value[i] == '%') {
            Py_ssize_t start = i;
            i += 2;
            while (i < len && value[i] != '"') {
                i++;
            }
            if (i >= len
                || !edge_sf_display_string(value + start, i - start + 1)) {
                return 0;
            }
            i++;
        }
        else {
            Py_ssize_t start = i;
            while (i < len && value[i] != ';') {
                i++;
            }
            if (!edge_sf_parameter_bare(value + start, i - start)) {
                return 0;
            }
        }
        if (i < len && value[i] != ';') {
            return 0;
        }
    }
    return 1;
}


/* Locate the CRLFCRLF that ends the head, resuming from the last scan point.
 *
 * Returns 1 with `*end` at the terminating CRLF's offset, 0 for "need more",
 * -1 with `*status` set when the head is already over the bound. */
static int
edge_find_head_end(EdgeClient *self, Py_ssize_t *end, int *status)
{
    const char *base = self->in.data;
    Py_ssize_t limit = self->in.len;
    Py_ssize_t from = self->head_scan;
    if (from < self->in.start) {
        from = self->in.start;
    }
    else if (from > self->in.start + 3) {
        from -= 3;              /* a terminator may straddle the last read */
    }

    Py_ssize_t i = from;
    while (i + 3 < limit) {
        const char *hit = memchr(base + i, '\r', (size_t)(limit - i - 3));
        if (hit == NULL) {
            break;
        }
        Py_ssize_t at = (Py_ssize_t)(hit - base);
        if (base[at + 1] == '\n' && base[at + 2] == '\r' && base[at + 3] == '\n') {
            *end = at;
            return 1;
        }
        i = at + 1;
    }
    self->head_scan = limit;
    if (limit - self->in.start > EDGE_MAX_HEAD) {
        *status = 431;
        return -1;
    }
    return 0;
}


/* Build the outbound request from the parsed slices.
 *
 * The transform is RFC 9110 7.6.1 plus the fields this proxy owns, and it is
 * the same set `wreath_edge_request_headers` applies -- see `edge_headers.c`,
 * which is where the reasoning for each name lives. What differs is only that
 * nothing is materialised: names and values are copied from the read buffer
 * straight into the outbound one.
 */
static int
edge_build_request(EdgeClient *self, EdgeUpstream *up, const EdgeSlice *fields,
                   Py_ssize_t nfields, Py_ssize_t method, Py_ssize_t method_len,
                   Py_ssize_t target, Py_ssize_t target_len,
                   Py_ssize_t host, Py_ssize_t host_len,
                   Py_ssize_t connection, Py_ssize_t connection_len,
                   Py_ssize_t body_len, int has_body)
{
    const char *base = self->in.data;
    EdgeBuf *out = &self->out;
    ebuf_reset(out);

    if (ebuf_add(out, base + method, method_len) < 0
        || EBUF_LIT(out, " ") < 0
        || ebuf_add(out, base + target, target_len) < 0
        || EBUF_LIT(out, " HTTP/1.1\r\nhost: ") < 0) {
        return -1;
    }
    if (ebuf_add(out, PyBytes_AS_STRING(up->authority),
                 PyBytes_GET_SIZE(up->authority)) < 0
        || EBUF_LIT(out, "\r\n") < 0) {
        return -1;
    }
    /* The outbound framing describes what this proxy actually sends. Relaying a
     * claimed length is how a proxy and an origin come to disagree about where
     * one message ends, which is the whole of request smuggling. */
    if (has_body) {
        if (EBUF_LIT(out, "content-length: ") < 0
            || ebuf_add_ssize(out, body_len) < 0
            || EBUF_LIT(out, "\r\n") < 0) {
            return -1;
        }
    }

    for (Py_ssize_t i = 0; i < nfields; i++) {
        const char *name = base + fields[i].name;
        Py_ssize_t name_len = fields[i].name_len;
        if (wreath_edge_is_request_drop(name, name_len)) {
            continue;
        }
        /* A field named by `Connection` is hop-by-hop for this message only, so
         * it cannot be filtered in the parse pass -- the header may arrive after
         * the fields it names. Both oracles get this wrong: haproxy 3.4.3 and
         * nginx 1.30.4 forward such a field. */
        if (connection_len > 0
            && wreath_edge_connection_names(base + connection, connection_len,
                                            name, name_len)) {
            continue;
        }
        if (ebuf_add(out, name, name_len) < 0
            || EBUF_LIT(out, ": ") < 0
            || ebuf_add(out, base + fields[i].value, fields[i].value_len) < 0
            || EBUF_LIT(out, "\r\n") < 0) {
            return -1;
        }
    }

    /* The forwarding record, in both spellings. RFC 7239 `Forwarded` is the
     * standard; the `x-forwarded-*` family is what almost everything actually
     * reads, including wreath's own ProxyHeadersMiddleware -- which is what sits
     * behind this proxy, so emitting only the standard one would mean wreath
     * could not sit behind itself. An inbound value is replaced, never appended
     * to: appending is the classic spoof, where the attacker writes the first
     * element and every parser that reads "the client" reads theirs. */
    const char *scheme = PyBytes_AS_STRING(self->table->scheme);
    Py_ssize_t scheme_len = PyBytes_GET_SIZE(self->table->scheme);
    if (self->peer_len > 0) {
        if (EBUF_LIT(out, "x-forwarded-for: ") < 0
            || ebuf_add(out, self->peer, self->peer_len) < 0
            || EBUF_LIT(out, "\r\n") < 0) {
            return -1;
        }
    }
    if (EBUF_LIT(out, "x-forwarded-proto: ") < 0
        || ebuf_add(out, scheme, scheme_len) < 0
        || EBUF_LIT(out, "\r\n") < 0) {
        return -1;
    }
    if (host_len > 0) {
        if (EBUF_LIT(out, "x-forwarded-host: ") < 0
            || ebuf_add(out, base + host, host_len) < 0
            || EBUF_LIT(out, "\r\n") < 0) {
            return -1;
        }
    }
    if (EBUF_LIT(out, "forwarded: ") < 0) {
        return -1;
    }
    if (self->peer_len > 0) {
        if (EBUF_LIT(out, "for=\"") < 0
            || ebuf_add(out, self->peer, self->peer_len) < 0
            || EBUF_LIT(out, "\"; ") < 0) {
            return -1;
        }
    }
    if (EBUF_LIT(out, "proto=") < 0 || ebuf_add(out, scheme, scheme_len) < 0) {
        return -1;
    }
    if (host_len > 0) {
        if (EBUF_LIT(out, "; host=\"") < 0
            || ebuf_add(out, base + host, host_len) < 0
            || EBUF_LIT(out, "\"") < 0) {
            return -1;
        }
    }
    if (EBUF_LIT(out, "\r\n") < 0) {
        return -1;
    }

    /* `Via` is appended where `x-forwarded-for` is replaced, and the asymmetry
     * is deliberate: `Via` is a loop-detection and topology record whose whole
     * value is the chain, and it is an authorization input nowhere. */
    if (EBUF_LIT(out, "via: ") < 0) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < nfields; i++) {
        if (!edge_token_eq(base + fields[i].name, fields[i].name_len, "via", 3)) {
            continue;
        }
        if (ebuf_add(out, base + fields[i].value, fields[i].value_len) < 0
            || EBUF_LIT(out, ", ") < 0) {
            return -1;
        }
    }
    if (ebuf_add(out, PyBytes_AS_STRING(self->table->via),
                 PyBytes_GET_SIZE(self->table->via)) < 0
        || EBUF_LIT(out, "\r\n\r\n") < 0) {
        return -1;
    }
    return 0;
}


/* Parse and validate one request head.
 *
 * Returns 1 when the head is complete and the outbound request is staged in
 * `self->out` (body still to follow, when there is one), 0 for "need more
 * bytes", and -1 when the client has been refused or an exception is set.
 */
static int
edge_parse_request(EdgeClient *self)
{
    Py_ssize_t end = 0;
    int status = 400;
    int found = edge_find_head_end(self, &end, &status);
    if (found <= 0) {
        return found == 0 ? 0 : edge_refuse(self, status);
    }
    self->head_end = end;

    char *base = self->in.data;
    Py_ssize_t p = self->in.start;

    /* Request line. */
    char *eol = memchr(base + p, '\r', (size_t)(end - p + 1));
    if (eol == NULL) {
        return edge_refuse(self, 400);
    }
    Py_ssize_t line_end = (Py_ssize_t)(eol - base);
    char *sp1 = memchr(base + p, ' ', (size_t)(line_end - p));
    if (sp1 == NULL) {
        return edge_refuse(self, 400);
    }
    Py_ssize_t method = p;
    Py_ssize_t method_len = (Py_ssize_t)(sp1 - base) - p;
    Py_ssize_t target = (Py_ssize_t)(sp1 - base) + 1;
    char *sp2 = memchr(base + target, ' ', (size_t)(line_end - target));
    if (sp2 == NULL || method_len == 0) {
        return edge_refuse(self, 400);
    }
    Py_ssize_t target_len = (Py_ssize_t)(sp2 - base) - target;
    Py_ssize_t version = (Py_ssize_t)(sp2 - base) + 1;
    Py_ssize_t version_len = line_end - version;
    if (target_len == 0 || version_len != 8
        || memcmp(base + version, "HTTP/1.", 7) != 0) {
        return edge_refuse(self, 400);
    }
    int minor = base[version + 7] - '0';
    if (minor != 0 && minor != 1) {
        return edge_refuse(self, 400);
    }

    /* Header fields. */
    EdgeSlice fields[EDGE_MAX_HEADERS];
    Py_ssize_t nfields = 0;
    Py_ssize_t host = 0, host_len = 0, hosts = 0;
    Py_ssize_t connection = 0, connection_len = 0;
    Py_ssize_t declared = -1;
    int content_lengths = 0;
    int chunked = 0, transfer_encodings = 0;
    int incrementals = 0, incremental_true = 0;

    p = line_end + 2;
    while (p < end) {
        char *nl = memchr(base + p, '\r', (size_t)(end - p + 1));
        if (nl == NULL || nl[1] != '\n') {
            return edge_refuse(self, 400);
        }
        Py_ssize_t field_end = (Py_ssize_t)(nl - base);
        if (edge_is_space(base[p])) {
            /* Obsolete line folding. RFC 9112 6.3 says a recipient that is not
             * a message/http parser must reject it, and a proxy that unfolds is
             * inventing a value the origin never saw. */
            return edge_refuse(self, 400);
        }
        char *colon = memchr(base + p, ':', (size_t)(field_end - p));
        if (colon == NULL) {
            return edge_refuse(self, 400);
        }
        Py_ssize_t name = p;
        Py_ssize_t name_len = (Py_ssize_t)(colon - base) - p;
        if (name_len == 0 || edge_is_space(base[name + name_len - 1])) {
            /* Whitespace between the field name and the colon: RFC 9112 5.1
             * requires a 400 exactly because the alternatives disagree, which is
             * a desync waiting for two hops that chose differently. */
            return edge_refuse(self, 400);
        }
        Py_ssize_t value = (Py_ssize_t)(colon - base) + 1;
        Py_ssize_t value_end = field_end;
        while (value < value_end && edge_is_space(base[value])) value++;
        while (value_end > value && edge_is_space(base[value_end - 1])) value_end--;
        Py_ssize_t value_len = value_end - value;

        for (Py_ssize_t i = 0; i < name_len; i++) {
            char c = base[name + i];
            if (c >= 'A' && c <= 'Z') {
                base[name + i] = (char)(c + 32);
            }
            else if ((unsigned char)c <= ' ' || c == 0x7f) {
                return edge_refuse(self, 400);
            }
        }

        if (edge_token_eq(base + name, name_len, "host", 4)) {
            host = value;
            host_len = value_len;
            hosts++;
        }
        else if (edge_token_eq(base + name, name_len, "connection", 10)) {
            connection = value;
            connection_len = value_len;
        }
        else if (edge_token_eq(base + name, name_len, "content-length", 14)) {
            content_lengths++;
            Py_ssize_t parsed;
            if (edge_parse_decimal(base + value, value_len, &parsed) < 0) {
                return edge_refuse(self, 400);
            }
            if (declared >= 0 && declared != parsed) {
                return edge_refuse(self, 400);
            }
            declared = parsed;
        }
        else if (edge_token_eq(base + name, name_len, "transfer-encoding", 17)) {
            transfer_encodings++;
            /* `chunked` and nothing else. RFC 9112 6.1 allows a coding list, but
             * a proxy that accepts one it does not implement has agreed to relay
             * a body it cannot find the end of. */
            chunked = edge_ieq(base + value, value_len, "chunked", 7);
            if (!chunked) {
                return edge_refuse(self, 501);
            }
        }
        else if (edge_token_eq(base + name, name_len, "incremental", 11)) {
            incrementals++;
            incremental_true = edge_incremental_true(base + value, value_len);
        }

        if (nfields >= EDGE_MAX_HEADERS) {
            return edge_refuse(self, 431);
        }
        fields[nfields].name = name;
        fields[nfields].name_len = name_len;
        fields[nfields].value = value;
        fields[nfields].value_len = value_len;
        nfields++;
        p = field_end + 2;
    }

    /* The three refusals, stated once. Each is a way for this hop and the next
     * to disagree about where the message ends, and a proxy that resolves the
     * disagreement by picking one reading is a desync vector however fast it is.
     */
    if (hosts > 1) {
        return edge_refuse(self, 400);
    }
    if (hosts == 0 && minor == 1) {
        return edge_refuse(self, 400);
    }
    if (content_lengths > 1) {
        return edge_refuse(self, 400);
    }
    if (transfer_encodings > 0 && content_lengths > 0) {
        return edge_refuse(self, 400);
    }
    if (incrementals == 1 && incremental_true) {
        return edge_refuse_error(self, 501, "incremental_refused");
    }

    self->no_body_expected = edge_token_eq(base + method, method_len, "HEAD", 4);
    self->keep_alive = minor == 1;
    if (edge_connection_has(base + connection, connection_len, "close", 5)) {
        self->keep_alive = 0;
    }
    else if (edge_connection_has(base + connection, connection_len,
                                 "keep-alive", 10)) {
        self->keep_alive = 1;
    }
    self->close_after = !self->keep_alive;

    Py_ssize_t body_len = chunked ? 0 : (declared > 0 ? declared : 0);
    int has_body = chunked || content_lengths > 0;
    if (!chunked && declared > self->table->max_body) {
        return edge_refuse(self, 413);
    }

    /* Chunked arrives without a length, so the head cannot be built until the
     * body has been decoded -- and the slices above are offsets into a buffer
     * that must therefore not be compacted under them. Reallocation is fine;
     * sliding the contents to the front is not. */
    if (chunked) {
        self->head_start = self->in.start;
        self->head_end = end;
        self->in.pinned = 1;
        self->state = EC_BODY_SIZE;
        self->body_need = 0;
        ebuf_reset(&self->body);
        self->in.start = end + 4;
        /* Re-parsed on completion rather than carried: the alternative is
         * copying the whole slice array onto the object for a shape of request
         * a proxy sees rarely, and the second parse is over bytes already hot. */
        return 1;
    }

    EdgeUpstream *up;
    Py_ssize_t index = edge_choose(self->table, edge_now(), 1);
    if (index < 0) {
        index = edge_choose(self->table, edge_now(), 0);
    }
    if (index < 0) {
        return edge_refuse_error(self, 502, "destination_unavailable");
    }
    up = &self->table->ups[index];
    self->queued_on = index;

    if (edge_build_request(self, up, fields, nfields, method, method_len,
                           target, target_len, host, host_len,
                           connection, connection_len, body_len, has_body) < 0) {
        return -1;
    }
    self->in.start = end + 4;
    self->head_scan = self->in.start;
    self->body_need = body_len;
    self->state = body_len > 0 ? EC_BODY_FIXED : EC_WAITING;
    if (self->state == EC_WAITING) {
        return edge_dispatch(self);
    }
    return 1;
}


/* Re-parse a completed chunked request now that its length is known.
 *
 * The head is still at the front of the read buffer (pinned above), so this is
 * the same parse against the same bytes with `body` holding the decoded octets.
 */
static int
edge_finish_chunked(EdgeClient *self)
{
    char *base = self->in.data;
    Py_ssize_t end = self->head_end;
    Py_ssize_t p = self->head_start;

    char *eol = memchr(base + p, '\r', (size_t)(end - p + 1));
    if (eol == NULL) {
        return edge_refuse(self, 400);
    }
    Py_ssize_t line_end = (Py_ssize_t)(eol - base);
    char *sp1 = memchr(base + p, ' ', (size_t)(line_end - p));
    Py_ssize_t method = p;
    Py_ssize_t method_len = (Py_ssize_t)(sp1 - base) - p;
    Py_ssize_t target = (Py_ssize_t)(sp1 - base) + 1;
    char *sp2 = memchr(base + target, ' ', (size_t)(line_end - target));
    Py_ssize_t target_len = (Py_ssize_t)(sp2 - base) - target;

    EdgeSlice fields[EDGE_MAX_HEADERS];
    Py_ssize_t nfields = 0;
    Py_ssize_t host = 0, host_len = 0;
    Py_ssize_t connection = 0, connection_len = 0;

    p = line_end + 2;
    while (p < end && nfields < EDGE_MAX_HEADERS) {
        char *nl = memchr(base + p, '\r', (size_t)(end - p + 1));
        Py_ssize_t field_end = (Py_ssize_t)(nl - base);
        char *colon = memchr(base + p, ':', (size_t)(field_end - p));
        Py_ssize_t name = p;
        Py_ssize_t name_len = (Py_ssize_t)(colon - base) - p;
        Py_ssize_t value = (Py_ssize_t)(colon - base) + 1;
        Py_ssize_t value_end = field_end;
        while (value < value_end && edge_is_space(base[value])) value++;
        while (value_end > value && edge_is_space(base[value_end - 1])) value_end--;
        if (edge_token_eq(base + name, name_len, "host", 4)) {
            host = value;
            host_len = value_end - value;
        }
        else if (edge_token_eq(base + name, name_len, "connection", 10)) {
            connection = value;
            connection_len = value_end - value;
        }
        fields[nfields].name = name;
        fields[nfields].name_len = name_len;
        fields[nfields].value = value;
        fields[nfields].value_len = value_end - value;
        nfields++;
        p = field_end + 2;
    }

    Py_ssize_t index = edge_choose(self->table, edge_now(), 1);
    if (index < 0) {
        index = edge_choose(self->table, edge_now(), 0);
    }
    if (index < 0) {
        return edge_refuse_error(self, 502, "destination_unavailable");
    }
    self->queued_on = index;
    if (edge_build_request(self, &self->table->ups[index], fields, nfields,
                           method, method_len, target, target_len,
                           host, host_len, connection, connection_len,
                           self->body.len, 1) < 0) {
        return -1;
    }
    if (ebuf_add(&self->out, self->body.data, self->body.len) < 0) {
        return -1;
    }
    self->in.pinned = 0;
    self->head_scan = self->in.start;
    ebuf_reset(&self->body);
    self->state = EC_WAITING;
    return edge_dispatch(self);
}


/* --- dispatch ------------------------------------------------------------- */

/* Hand the staged request to an open upstream connection, or queue it.
 *
 * There is nothing to await here, which is the point: `self->out` is a byte
 * buffer and `conn->sink` is a transport that was connected while the proxy was
 * being configured. When no connection is free the client is parked on the
 * upstream's FIFO and picked up by whichever connection finishes next.
 */
static int
edge_dispatch(EdgeClient *self)
{
    EdgeTable *table = self->table;
    Py_ssize_t index = self->queued_on;
    EdgeUpstream *up = &table->ups[index];

    EdgeConn *conn = up->free_head;
    if (conn == NULL) {
        if (up->wait_tail != NULL) {
            up->wait_tail->wait_next = self;
        }
        else {
            up->wait_head = self;
        }
        up->wait_tail = self;
        self->wait_next = NULL;
        up->waiting++;
        Py_INCREF(self);
        return 1;
    }
    up->free_head = conn->free_next;
    conn->free_next = NULL;
    conn->in_free_list = 0;

    Py_INCREF(self);
    Py_XSETREF(conn->client, self);
    Py_INCREF(conn);
    Py_XSETREF(self->conn, conn);
    conn->state = EU_HEAD;
    conn->started = edge_now();
    conn->head_scan = conn->in.start;
    conn->counted = 1;
    up->inflight++;
    if (sink_write(&conn->sink, self->out.data, self->out.len) < 0) {
        return -1;
    }
    ebuf_reset(&self->out);
    return 1;
}


/* The connection has finished a response (or died). Give it back, or hand it
 * straight to whoever is queued for this upstream. */
static void
edge_release_conn(EdgeConn *conn, int reusable)
{
    EdgeTable *table = conn->table;
    EdgeUpstream *up = &table->ups[conn->index];
    EdgeClient *client = conn->client;

    if (conn->counted) {
        conn->counted = 0;
        up->inflight--;
    }
    if (client != NULL) {
        Py_CLEAR(client->conn);
        conn->client = NULL;
        Py_DECREF(client);
    }
    conn->state = EU_IDLE;
    if (!reusable || table->closing) {
        return;
    }

    EdgeClient *waiter = up->wait_head;
    if (waiter != NULL) {
        up->wait_head = waiter->wait_next;
        if (up->wait_head == NULL) {
            up->wait_tail = NULL;
        }
        waiter->wait_next = NULL;
        up->waiting--;
        Py_INCREF(waiter);
        Py_XSETREF(conn->client, waiter);
        Py_INCREF(conn);
        Py_XSETREF(waiter->conn, conn);
        conn->state = EU_HEAD;
        conn->started = edge_now();
        conn->head_scan = conn->in.start;
        conn->counted = 1;
        up->inflight++;
        if (sink_write(&conn->sink, waiter->out.data, waiter->out.len) < 0) {
            PyErr_WriteUnraisable((PyObject *)conn);
        }
        ebuf_reset(&waiter->out);
        Py_DECREF(waiter);      /* the queue's reference */
        return;
    }
    conn->free_next = up->free_head;
    up->free_head = conn;
    conn->in_free_list = 1;
}


/* --- downstream protocol -------------------------------------------------- */

static int
edge_client_drive(EdgeClient *self)
{
    for (;;) {
        Py_ssize_t avail = self->in.len - self->in.start;
        switch (self->state) {
        case EC_HEAD: {
            if (avail <= 0) {
                return 0;
            }
            int rc = edge_parse_request(self);
            if (rc <= 0) {
                return rc;
            }
            break;
        }
        case EC_BODY_FIXED: {
            if (avail <= 0) {
                return 0;
            }
            Py_ssize_t take = avail < self->body_need ? avail : self->body_need;
            if (ebuf_add(&self->out, self->in.data + self->in.start, take) < 0) {
                return -1;
            }
            self->in.start += take;
            self->body_need -= take;
            if (self->body_need > 0) {
                return 0;
            }
            self->head_scan = self->in.start;
            self->state = EC_WAITING;
            if (edge_dispatch(self) < 0) {
                return -1;
            }
            break;
        }
        case EC_BODY_SIZE: {
            char *base = self->in.data;
            char *nl = avail > 0
                ? memchr(base + self->in.start, '\n', (size_t)avail) : NULL;
            if (nl == NULL) {
                return 0;
            }
            Py_ssize_t line_len = (Py_ssize_t)(nl - (base + self->in.start));
            Py_ssize_t size = 0;
            Py_ssize_t i = 0;
            const char *p = base + self->in.start;
            for (; i < line_len; i++) {
                char c = p[i];
                int digit;
                if (c >= '0' && c <= '9') digit = c - '0';
                else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
                else if (c >= 'A' && c <= 'F') digit = c - 'A' + 10;
                else break;
                if (size > (PY_SSIZE_T_MAX >> 8)) {
                    return edge_refuse(self, 400);
                }
                size = size * 16 + digit;
            }
            if (i == 0) {
                return edge_refuse(self, 400);
            }
            self->in.start += line_len + 1;
            if (size == 0) {
                self->state = EC_BODY_TRAILER;
                break;
            }
            if (self->body.len + size > self->table->max_body) {
                return edge_refuse(self, 413);
            }
            self->body_need = size;
            self->state = EC_BODY_DATA;
            break;
        }
        case EC_BODY_DATA: {
            if (avail <= 0) {
                return 0;
            }
            Py_ssize_t take = avail < self->body_need ? avail : self->body_need;
            if (ebuf_add(&self->body, self->in.data + self->in.start, take) < 0) {
                return -1;
            }
            self->in.start += take;
            self->body_need -= take;
            if (self->body_need == 0) {
                self->state = EC_BODY_DATA_CRLF;
            }
            break;
        }
        case EC_BODY_DATA_CRLF: {
            if (avail < 2) {
                return 0;
            }
            self->in.start += 2;
            self->state = EC_BODY_SIZE;
            break;
        }
        case EC_BODY_TRAILER: {
            char *base = self->in.data;
            char *nl = avail > 0
                ? memchr(base + self->in.start, '\n', (size_t)avail) : NULL;
            if (nl == NULL) {
                return 0;
            }
            Py_ssize_t line_len = (Py_ssize_t)(nl - (base + self->in.start));
            int blank = line_len == 0
                || (line_len == 1 && base[self->in.start] == '\r');
            self->in.start += line_len + 1;
            if (blank) {
                return edge_finish_chunked(self) < 0 ? -1 : 0;
            }
            break;
        }
        default:
            /* EC_WAITING: a pipelined request stays in the buffer until the
             * response in flight has been written, because HTTP/1.1 responses
             * must leave in the order their requests arrived. */
            return 0;
        }
    }
}


static void
edge_client_unqueue(EdgeClient *self)
{
    if (self->queued_on < 0 || self->table == NULL) {
        return;
    }
    EdgeUpstream *up = &self->table->ups[self->queued_on];
    EdgeClient **link = &up->wait_head;
    EdgeClient *prev = NULL;
    while (*link != NULL) {
        if (*link == self) {
            *link = self->wait_next;
            if (up->wait_tail == self) {
                up->wait_tail = prev;
            }
            self->wait_next = NULL;
            up->waiting--;
            Py_DECREF(self);
            return;
        }
        prev = *link;
        link = &(*link)->wait_next;
    }
}


static PyObject *
edge_client_connection_made(EdgeClient *self, PyObject *transport)
{
    sink_bind(&self->sink, transport);
    PyObject *peer = PyObject_CallMethod(transport, "get_extra_info", "s", "peername");
    if (peer == NULL) {
        PyErr_Clear();
    }
    else {
        if (PyTuple_Check(peer) && PyTuple_GET_SIZE(peer) >= 1) {
            PyObject *host = PyTuple_GET_ITEM(peer, 0);
            if (PyUnicode_Check(host)) {
                Py_ssize_t size = 0;
                const char *utf8 = PyUnicode_AsUTF8AndSize(host, &size);
                if (utf8 != NULL && size < (Py_ssize_t)sizeof(self->peer)) {
                    memcpy(self->peer, utf8, (size_t)size);
                    self->peer_len = size;
                }
                else {
                    PyErr_Clear();
                }
            }
        }
        Py_DECREF(peer);
    }
    if (PySet_Add(self->table->live, (PyObject *)self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
edge_client_connection_lost(EdgeClient *self, PyObject *Py_UNUSED(exc))
{
    self->sink.closing = 1;
    self->state = EC_CLOSED;
    edge_client_unqueue(self);
    self->queued_on = -1;
    if (self->conn != NULL) {
        /* The response still on the wire has nowhere to go, but the connection
         * carrying it stays useful once it has been drained -- so it is left to
         * finish rather than torn down. It keeps its `inflight` count until it
         * does: it is still occupied, and releasing the count here would offer
         * a busy origin to the selector as an idle one. */
        Py_CLEAR(self->conn->client);
        Py_CLEAR(self->conn);
    }
    if (self->table != NULL
        && PySet_Discard(self->table->live, (PyObject *)self) < 0) {
        PyErr_Clear();
    }
    sink_clear(&self->sink);
    Py_RETURN_NONE;
}


static int
edge_client_prepare_read(EdgeClient *self, Py_ssize_t sizehint,
                         char **buffer, Py_ssize_t *capacity)
{
    Py_ssize_t target = (sizehint > 0 && sizehint < EDGE_RECV_CHUNK)
        ? sizehint : EDGE_RECV_CHUNK;
    ebuf_compact(&self->in, &self->head_scan);
    if (ebuf_reserve(&self->in, target) < 0) {
        return -1;
    }
    self->offer_offset = self->in.len;
    self->offer_size = self->in.cap - self->in.len;
    *buffer = self->in.data + self->offer_offset;
    *capacity = self->offer_size;
    return 0;
}


static inline int
edge_getbuffer(PyObject *op, Py_buffer *view, int flags, EdgeBuf *input,
               Py_ssize_t offer_offset, Py_ssize_t offer_size, int *exports)
{
    if (offer_size <= 0) {
        PyErr_SetString(PyExc_BufferError, "no active receive offer");
        view->obj = NULL;
        return -1;
    }
    if (PyBuffer_FillInfo(view, op, input->data + offer_offset,
                          offer_size, 0, flags) < 0) {
        return -1;
    }
    (*exports)++;
    return 0;
}


static int
edge_client_getbuffer(PyObject *op, Py_buffer *view, int flags)
{
    EdgeClient *self = (EdgeClient *)op;
    return edge_getbuffer(op, view, flags, &self->in, self->offer_offset,
                          self->offer_size, &self->exports);
}


static void
edge_client_releasebuffer(PyObject *op, Py_buffer *Py_UNUSED(view))
{
    EdgeClient *self = (EdgeClient *)op;
    if (--self->exports <= 0) {
        self->exports = 0;
        self->offer_size = 0;
        self->offer_offset = 0;
    }
}


static PyObject *
edge_client_get_buffer(EdgeClient *self, PyObject *arg)
{
    Py_ssize_t sizehint = PyLong_AsSsize_t(arg);
    char *buffer;
    Py_ssize_t capacity;
    if (sizehint == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (edge_client_prepare_read(self, sizehint, &buffer, &capacity) < 0) {
        return NULL;
    }
    PyObject *view = PyMemoryView_FromObject((PyObject *)self);
    if (view == NULL) {
        self->offer_size = 0;
        self->offer_offset = 0;
    }
    return view;
}


static PyObject *
edge_client_buffer_updated(EdgeClient *self, PyObject *arg)
{
    Py_ssize_t nbytes = PyLong_AsSsize_t(arg);
    if (nbytes == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (nbytes < 0 || nbytes > self->offer_size) {
        self->offer_size = 0;
        PyErr_SetString(PyExc_ValueError, "buffer_updated() count out of range");
        return NULL;
    }
    self->in.len = self->offer_offset + nbytes;
    self->offer_size = 0;
    self->offer_offset = 0;
    if (nbytes == 0 || self->state == EC_CLOSED) {
        Py_RETURN_NONE;
    }
    if (edge_client_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* Kept for delegating transports and direct test harnesses; production asyncio
 * socket transports take the buffered pair above and never copy. */
static PyObject *
edge_client_data_received(EdgeClient *self, PyObject *data)
{
    Py_buffer view;
    if (self->state == EC_CLOSED) {
        Py_RETURN_NONE;
    }
    if (PyObject_GetBuffer(data, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    ebuf_compact(&self->in, &self->head_scan);
    if (ebuf_add(&self->in, (const char *)view.buf, view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    PyBuffer_Release(&view);
    if (edge_client_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
edge_client_eof_received(EdgeClient *Py_UNUSED(self), PyObject *Py_UNUSED(a))
{
    Py_RETURN_FALSE;
}


static PyObject *
edge_client_noop(EdgeClient *Py_UNUSED(self), PyObject *Py_UNUSED(a))
{
    Py_RETURN_NONE;
}


static PyObject *
edge_client_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *table;
    static char *kwlist[] = {"table", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O!", kwlist,
                                     edge_table_type, &table)) {
        return NULL;
    }
    EdgeClient *self = (EdgeClient *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->table = (EdgeTable *)Py_NewRef(table);
    self->state = EC_HEAD;
    self->queued_on = -1;
    self->keep_alive = 1;
    return (PyObject *)self;
}


static int
edge_client_traverse(PyObject *op, visitproc visit, void *arg)
{
    EdgeClient *self = (EdgeClient *)op;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->table);
    Py_VISIT(self->conn);
    Py_VISIT(self->sink.transport);
    Py_VISIT(self->sink.write_fn);
    return 0;
}


static int
edge_client_clear(PyObject *op)
{
    EdgeClient *self = (EdgeClient *)op;
    Py_CLEAR(self->table);
    Py_CLEAR(self->conn);
    sink_clear(&self->sink);
    return 0;
}


static void
edge_client_dealloc(PyObject *op)
{
    EdgeClient *self = (EdgeClient *)op;
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    edge_client_clear(op);
    ebuf_free(&self->in);
    ebuf_free(&self->out);
    ebuf_free(&self->body);
    type->tp_free(op);
    Py_DECREF(type);
}


static PyMethodDef edge_client_methods[] = {
    {"connection_made", (PyCFunction)edge_client_connection_made, METH_O, NULL},
    {"connection_lost", (PyCFunction)edge_client_connection_lost, METH_O, NULL},
    {"data_received", (PyCFunction)edge_client_data_received, METH_O, NULL},
    {"get_buffer", (PyCFunction)edge_client_get_buffer, METH_O, NULL},
    {"buffer_updated", (PyCFunction)edge_client_buffer_updated, METH_O, NULL},
    {"eof_received", (PyCFunction)edge_client_eof_received, METH_NOARGS, NULL},
    {"pause_writing", (PyCFunction)edge_client_noop, METH_NOARGS, NULL},
    {"resume_writing", (PyCFunction)edge_client_noop, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};


static PyType_Slot edge_client_slots[] = {
    {Py_tp_doc, "The listening side of the native proxy: one client connection."},
    {Py_tp_new, edge_client_new},
    {Py_tp_dealloc, edge_client_dealloc},
    {Py_tp_traverse, edge_client_traverse},
    {Py_tp_clear, edge_client_clear},
    {Py_tp_methods, edge_client_methods},
    {Py_bf_getbuffer, edge_client_getbuffer},
    {Py_bf_releasebuffer, edge_client_releasebuffer},
    {0, NULL},
};


static PyType_Spec edge_client_spec = {
    .name = "wreath._native._edge.EdgeProtocol",
    .basicsize = sizeof(EdgeClient),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = edge_client_slots,
};


/* --- upstream protocol ---------------------------------------------------- */

/* Rewrite one response head and put it on the client's socket.
 *
 * Returns 1 when the head is complete, 0 for "need more", -1 on error. */
static int
edge_relay_response_head(EdgeConn *self)
{
    char *base = self->in.data;
    Py_ssize_t limit = self->in.len;
    Py_ssize_t from = self->head_scan;
    if (from < self->in.start) {
        from = self->in.start;
    }
    else if (from > self->in.start + 3) {
        from -= 3;
    }
    Py_ssize_t end = -1;
    Py_ssize_t i = from;
    while (i + 3 < limit) {
        const char *hit = memchr(base + i, '\r', (size_t)(limit - i - 3));
        if (hit == NULL) {
            break;
        }
        Py_ssize_t at = (Py_ssize_t)(hit - base);
        if (base[at + 1] == '\n' && base[at + 2] == '\r' && base[at + 3] == '\n') {
            end = at;
            break;
        }
        i = at + 1;
    }
    if (end < 0) {
        self->head_scan = limit;
        if (limit - self->in.start > EDGE_MAX_HEAD) {
            return -2;
        }
        return 0;
    }

    Py_ssize_t p = self->in.start;
    char *eol = memchr(base + p, '\r', (size_t)(end - p + 1));
    if (eol == NULL) {
        return -2;
    }
    Py_ssize_t line_end = (Py_ssize_t)(eol - base);
    /* `HTTP/1.x SP status [SP reason]`. The version is rewritten because this
     * hop speaks 1.1 downstream whatever the origin answered in. */
    char *sp1 = memchr(base + p, ' ', (size_t)(line_end - p));
    if (sp1 == NULL) {
        return -2;
    }
    Py_ssize_t rest = (Py_ssize_t)(sp1 - base) + 1;
    if (line_end - rest < 3) {
        return -2;
    }
    Py_ssize_t code = 0;
    if (edge_parse_decimal(base + rest, 3, &code) < 0) {
        return -2;
    }

    EdgeBuf *out = &self->out;
    ebuf_reset(out);
    if (EBUF_LIT(out, "HTTP/1.1 ") < 0
        || ebuf_add(out, base + rest, line_end - rest) < 0
        || EBUF_LIT(out, "\r\n") < 0) {
        return -1;
    }

    Py_ssize_t declared = -1;
    int chunked = 0;
    int upstream_close = 0;
    Py_ssize_t connection = 0, connection_len = 0;

    /* Two passes, because `Connection` may name a field that appeared before
     * it. The first records framing and the connection options; the second
     * emits. */
    p = line_end + 2;
    while (p < end) {
        char *nl = memchr(base + p, '\r', (size_t)(end - p + 1));
        if (nl == NULL) {
            return -2;
        }
        Py_ssize_t field_end = (Py_ssize_t)(nl - base);
        char *colon = memchr(base + p, ':', (size_t)(field_end - p));
        if (colon == NULL) {
            return -2;
        }
        Py_ssize_t name = p;
        Py_ssize_t name_len = (Py_ssize_t)(colon - base) - p;
        Py_ssize_t value = (Py_ssize_t)(colon - base) + 1;
        Py_ssize_t value_end = field_end;
        while (value < value_end && edge_is_space(base[value])) value++;
        while (value_end > value && edge_is_space(base[value_end - 1])) value_end--;
        for (Py_ssize_t k = 0; k < name_len; k++) {
            char c = base[name + k];
            if (c >= 'A' && c <= 'Z') {
                base[name + k] = (char)(c + 32);
            }
        }
        if (edge_token_eq(base + name, name_len, "content-length", 14)) {
            if (edge_parse_decimal(base + value, value_end - value, &declared) < 0) {
                return -2;
            }
        }
        else if (edge_token_eq(base + name, name_len, "transfer-encoding", 17)) {
            chunked = edge_ieq(base + value, value_end - value, "chunked", 7);
            if (!chunked) {
                return -2;
            }
        }
        else if (edge_token_eq(base + name, name_len, "connection", 10)) {
            connection = value;
            connection_len = value_end - value;
            upstream_close = edge_connection_has(base + connection,
                                                 connection_len, "close", 5);
        }
        p = field_end + 2;
    }

    p = line_end + 2;
    while (p < end) {
        char *nl = memchr(base + p, '\r', (size_t)(end - p + 1));
        Py_ssize_t field_end = (Py_ssize_t)(nl - base);
        char *colon = memchr(base + p, ':', (size_t)(field_end - p));
        Py_ssize_t name = p;
        Py_ssize_t name_len = (Py_ssize_t)(colon - base) - p;
        Py_ssize_t value = (Py_ssize_t)(colon - base) + 1;
        Py_ssize_t value_end = field_end;
        while (value < value_end && edge_is_space(base[value])) value++;
        while (value_end > value && edge_is_space(base[value_end - 1])) value_end--;
        p = field_end + 2;
        if (wreath_edge_is_response_drop(base + name, name_len)) {
            continue;
        }
        if (connection_len > 0
            && wreath_edge_connection_names(base + connection, connection_len,
                                            base + name, name_len)) {
            continue;
        }
        if (ebuf_add(out, base + name, name_len) < 0
            || EBUF_LIT(out, ": ") < 0
            || ebuf_add(out, base + value, value_end - value) < 0
            || EBUF_LIT(out, "\r\n") < 0) {
            return -1;
        }
    }

    /* A 1xx, 204 or 304 carries no body whatever it declares, and neither does
     * any response to HEAD. Relaying one as though it did would leave this end
     * waiting for octets the origin will never send. */
    EdgeClient *client = self->client;
    int bodyless = code < 200 || code == 204 || code == 304
        || (client != NULL && client->no_body_expected);

    self->upstream_close = upstream_close;
    self->response_chunked = 0;
    if (bodyless) {
        self->state = EU_DONE;
        self->body_need = 0;
    }
    else if (chunked) {
        if (EBUF_LIT(out, "transfer-encoding: chunked\r\n") < 0) {
            return -1;
        }
        self->response_chunked = 1;
        self->state = EU_CHUNK_SIZE;
    }
    else if (declared >= 0) {
        self->body_need = declared;
        self->state = declared > 0 ? EU_FIXED : EU_DONE;
    }
    else {
        /* No framing at all: the body ends when the connection does, which
         * means this connection cannot be reused and the client's cannot stay
         * open either -- the length is the close. */
        self->state = EU_UNTIL_CLOSE;
        self->upstream_close = 1;
        if (client != NULL) {
            client->close_after = 1;
        }
    }
    if (client != NULL && client->close_after) {
        if (EBUF_LIT(out, "connection: close\r\n") < 0) {
            return -1;
        }
    }
    if (EBUF_LIT(out, "\r\n") < 0) {
        return -1;
    }
    self->in.start = end + 4;
    self->head_scan = self->in.start;
    if (client != NULL && sink_write(&client->sink, out->data, out->len) < 0) {
        return -1;
    }
    ebuf_reset(out);
    return 1;
}


/* The response is complete. Account for it, settle both connections, and let
 * the client parse whatever it pipelined behind this request. */
static int
edge_conn_complete(EdgeConn *self)
{
    EdgeTable *table = self->table;
    EdgeClient *client = self->client;
    edge_succeeded(table, self->index, edge_now() - self->started);

    int reusable = !self->upstream_close;
    EdgeClient *held = client;
    if (held != NULL) {
        Py_INCREF(held);
    }
    edge_release_conn(self, reusable);
    if (!reusable) {
        sink_close(&self->sink);
    }
    if (held == NULL) {
        return 0;
    }
    int rc = 0;
    if (held->close_after) {
        held->state = EC_CLOSED;
        sink_close(&held->sink);
    }
    else if (held->state == EC_WAITING) {
        held->state = EC_HEAD;
        held->head_scan = held->in.start;
        rc = edge_client_drive(held);
    }
    Py_DECREF(held);
    return rc < 0 ? -1 : 0;
}


static int
edge_conn_drive(EdgeConn *self)
{
    for (;;) {
        Py_ssize_t avail = self->in.len - self->in.start;
        EdgeClient *client = self->client;
        switch (self->state) {
        case EU_HEAD: {
            if (avail <= 0) {
                return 0;
            }
            int rc = edge_relay_response_head(self);
            if (rc == 0) {
                return 0;
            }
            if (rc == -2) {
                /* An unparseable head is the origin's fault, and the client is
                 * owed an answer rather than a hang. */
                if (client != NULL) {
                    edge_refuse_error(client, 502, "http_protocol_error");
                }
                edge_failed(self->table, self->index, edge_now());
                edge_release_conn(self, 0);
                sink_close(&self->sink);
                return 0;
            }
            if (rc < 0) {
                return -1;
            }
            break;
        }
        case EU_DONE: {
            if (edge_conn_complete(self) < 0) {
                return -1;
            }
            /* `complete` may have handed this connection the next queued
             * request, in which case any bytes still buffered belong to it. */
            if (self->state == EU_IDLE) {
                return 0;
            }
            break;
        }
        case EU_FIXED: {
            if (avail <= 0) {
                return 0;
            }
            Py_ssize_t take = avail < self->body_need ? avail : self->body_need;
            if (client != NULL
                && sink_write(&client->sink, self->in.data + self->in.start,
                              take) < 0) {
                return -1;
            }
            self->in.start += take;
            self->body_need -= take;
            if (self->body_need > 0) {
                return 0;
            }
            self->head_scan = self->in.start;
            self->state = EU_DONE;
            break;
        }
        case EU_UNTIL_CLOSE: {
            if (avail <= 0) {
                return 0;
            }
            if (client != NULL
                && sink_write(&client->sink, self->in.data + self->in.start,
                              avail) < 0) {
                return -1;
            }
            self->in.start += avail;
            return 0;
        }
        case EU_CHUNK_SIZE: {
            char *base = self->in.data;
            char *nl = avail > 0
                ? memchr(base + self->in.start, '\n', (size_t)avail) : NULL;
            if (nl == NULL) {
                return 0;
            }
            Py_ssize_t line_len = (Py_ssize_t)(nl - (base + self->in.start)) + 1;
            Py_ssize_t size = 0;
            Py_ssize_t i = 0;
            const char *p = base + self->in.start;
            for (; i < line_len; i++) {
                char c = p[i];
                int digit;
                if (c >= '0' && c <= '9') digit = c - '0';
                else if (c >= 'a' && c <= 'f') digit = c - 'a' + 10;
                else if (c >= 'A' && c <= 'F') digit = c - 'A' + 10;
                else break;
                if (size > (PY_SSIZE_T_MAX >> 8)) {
                    i = 0;
                    break;
                }
                size = size * 16 + digit;
            }
            if (i == 0) {
                /* Not a chunk size. The framing is unrecoverable from here --
                 * this end no longer knows where the body ends -- so the only
                 * safe move is to stop reading it. */
                edge_release_conn(self, 0);
                sink_close(&self->sink);
                return 0;
            }
            /* Chunk framing is relayed verbatim, sizes and all: re-encoding a
             * body this proxy did not change would be inventing a message. */
            if (client != NULL
                && sink_write(&client->sink, p, line_len) < 0) {
                return -1;
            }
            self->in.start += line_len;
            self->body_need = size;
            self->state = size == 0 ? EU_CHUNK_TRAILER : EU_CHUNK_DATA;
            break;
        }
        case EU_CHUNK_DATA: {
            if (avail <= 0) {
                return 0;
            }
            Py_ssize_t take = avail < self->body_need ? avail : self->body_need;
            if (client != NULL
                && sink_write(&client->sink, self->in.data + self->in.start,
                              take) < 0) {
                return -1;
            }
            self->in.start += take;
            self->body_need -= take;
            if (self->body_need == 0) {
                self->state = EU_CHUNK_DATA_CRLF;
            }
            break;
        }
        case EU_CHUNK_DATA_CRLF: {
            if (avail < 2) {
                return 0;
            }
            if (client != NULL
                && sink_write(&client->sink, self->in.data + self->in.start, 2) < 0) {
                return -1;
            }
            self->in.start += 2;
            self->state = EU_CHUNK_SIZE;
            break;
        }
        case EU_CHUNK_TRAILER: {
            char *base = self->in.data;
            char *nl = avail > 0
                ? memchr(base + self->in.start, '\n', (size_t)avail) : NULL;
            if (nl == NULL) {
                return 0;
            }
            Py_ssize_t line_len = (Py_ssize_t)(nl - (base + self->in.start)) + 1;
            int blank = line_len <= 2;
            if (client != NULL
                && sink_write(&client->sink, base + self->in.start, line_len) < 0) {
                return -1;
            }
            self->in.start += line_len;
            if (blank) {
                self->head_scan = self->in.start;
                self->state = EU_DONE;
            }
            break;
        }
        default:
            return 0;
        }
    }
}


static PyObject *
edge_conn_connection_made(EdgeConn *self, PyObject *transport)
{
    sink_bind(&self->sink, transport);
    EdgeUpstream *up = &self->table->ups[self->index];
    up->open++;
    self->state = EU_IDLE;
    if (PySet_Add(self->table->live, (PyObject *)self) < 0) {
        return NULL;
    }
    self->free_next = up->free_head;
    up->free_head = self;
    self->in_free_list = 1;
    /* A connection arriving while requests are queued serves one immediately:
     * this is also the path a reconnect lands on. */
    if (up->wait_head != NULL) {
        up->free_head = self->free_next;
        self->free_next = NULL;
        self->in_free_list = 0;
        edge_release_conn(self, 1);
    }
    Py_RETURN_NONE;
}


static void
edge_conn_unfree(EdgeConn *self)
{
    if (!self->in_free_list) {
        return;
    }
    EdgeUpstream *up = &self->table->ups[self->index];
    EdgeConn **link = &up->free_head;
    while (*link != NULL) {
        if (*link == self) {
            *link = self->free_next;
            break;
        }
        link = &(*link)->free_next;
    }
    self->free_next = NULL;
    self->in_free_list = 0;
}


static PyObject *
edge_conn_connection_lost(EdgeConn *self, PyObject *Py_UNUSED(exc))
{
    EdgeTable *table = self->table;
    self->sink.closing = 1;
    edge_conn_unfree(self);
    if (table != NULL) {
        EdgeUpstream *up = &table->ups[self->index];
        if (up->open > 0) {
            up->open--;
        }
        if (self->state == EU_UNTIL_CLOSE) {
            /* Close *was* the framing. The message is complete. */
            EdgeClient *client = self->client;
            if (client != NULL) {
                client->close_after = 1;
            }
            if (edge_conn_complete(self) < 0) {
                PyErr_WriteUnraisable((PyObject *)self);
            }
        }
        else if (self->state != EU_IDLE) {
            EdgeClient *client = self->client;
            if (client != NULL && self->state == EU_HEAD) {
                if (edge_refuse_error(client, 502, "connection_terminated") < 0) {
                    PyErr_WriteUnraisable((PyObject *)self);
                }
            }
            else if (client != NULL) {
                client->state = EC_CLOSED;
                sink_close(&client->sink);
            }
            edge_failed(table, self->index, edge_now());
            edge_release_conn(self, 0);
        }
        if (PySet_Discard(table->live, (PyObject *)self) < 0) {
            PyErr_Clear();
        }
        if (!table->closing && table->on_lost != Py_None) {
            PyObject *index = PyLong_FromSsize_t(self->index);
            if (index == NULL) {
                PyErr_Clear();
            }
            else {
                PyObject *result = PyObject_CallOneArg(table->on_lost, index);
                Py_DECREF(index);
                if (result == NULL) {
                    PyErr_WriteUnraisable(table->on_lost);
                }
                else {
                    Py_DECREF(result);
                }
            }
        }
    }
    sink_clear(&self->sink);
    Py_RETURN_NONE;
}


static int
edge_conn_getbuffer(PyObject *op, Py_buffer *view, int flags)
{
    EdgeConn *self = (EdgeConn *)op;
    return edge_getbuffer(op, view, flags, &self->in, self->offer_offset,
                          self->offer_size, &self->exports);
}


static void
edge_conn_releasebuffer(PyObject *op, Py_buffer *Py_UNUSED(view))
{
    EdgeConn *self = (EdgeConn *)op;
    if (--self->exports <= 0) {
        self->exports = 0;
        self->offer_size = 0;
        self->offer_offset = 0;
    }
}


static PyObject *
edge_conn_get_buffer(EdgeConn *self, PyObject *arg)
{
    Py_ssize_t sizehint = PyLong_AsSsize_t(arg);
    if (sizehint == -1 && PyErr_Occurred()) {
        return NULL;
    }
    Py_ssize_t target = (sizehint > 0 && sizehint < EDGE_RECV_CHUNK)
        ? sizehint : EDGE_RECV_CHUNK;
    ebuf_compact(&self->in, &self->head_scan);
    if (ebuf_reserve(&self->in, target) < 0) {
        return NULL;
    }
    self->offer_offset = self->in.len;
    self->offer_size = self->in.cap - self->in.len;
    PyObject *view = PyMemoryView_FromObject((PyObject *)self);
    if (view == NULL) {
        self->offer_size = 0;
        self->offer_offset = 0;
    }
    return view;
}


static PyObject *
edge_conn_buffer_updated(EdgeConn *self, PyObject *arg)
{
    Py_ssize_t nbytes = PyLong_AsSsize_t(arg);
    if (nbytes == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (nbytes < 0 || nbytes > self->offer_size) {
        self->offer_size = 0;
        PyErr_SetString(PyExc_ValueError, "buffer_updated() count out of range");
        return NULL;
    }
    self->in.len = self->offer_offset + nbytes;
    self->offer_size = 0;
    self->offer_offset = 0;
    if (nbytes == 0) {
        Py_RETURN_NONE;
    }
    if (edge_conn_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
edge_conn_data_received(EdgeConn *self, PyObject *data)
{
    Py_buffer view;
    if (PyObject_GetBuffer(data, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    ebuf_compact(&self->in, &self->head_scan);
    if (ebuf_add(&self->in, (const char *)view.buf, view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    PyBuffer_Release(&view);
    if (edge_conn_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
edge_conn_eof_received(EdgeConn *Py_UNUSED(self), PyObject *Py_UNUSED(a))
{
    Py_RETURN_FALSE;
}


static PyObject *
edge_conn_noop(EdgeConn *Py_UNUSED(self), PyObject *Py_UNUSED(a))
{
    Py_RETURN_NONE;
}


static PyObject *
edge_conn_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *table;
    Py_ssize_t index;
    static char *kwlist[] = {"table", "index", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O!n", kwlist,
                                     edge_table_type, &table, &index)) {
        return NULL;
    }
    if (index < 0 || index >= ((EdgeTable *)table)->count) {
        PyErr_SetString(PyExc_IndexError, "upstream index out of range");
        return NULL;
    }
    EdgeConn *self = (EdgeConn *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->table = (EdgeTable *)Py_NewRef(table);
    self->index = index;
    self->state = EU_IDLE;
    return (PyObject *)self;
}


static int
edge_conn_traverse(PyObject *op, visitproc visit, void *arg)
{
    EdgeConn *self = (EdgeConn *)op;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->table);
    Py_VISIT(self->client);
    Py_VISIT(self->sink.transport);
    Py_VISIT(self->sink.write_fn);
    return 0;
}


static int
edge_conn_clear(PyObject *op)
{
    EdgeConn *self = (EdgeConn *)op;
    Py_CLEAR(self->table);
    Py_CLEAR(self->client);
    sink_clear(&self->sink);
    return 0;
}


static void
edge_conn_dealloc(PyObject *op)
{
    EdgeConn *self = (EdgeConn *)op;
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    edge_conn_clear(op);
    ebuf_free(&self->in);
    ebuf_free(&self->out);
    type->tp_free(op);
    Py_DECREF(type);
}


static PyMethodDef edge_conn_methods[] = {
    {"connection_made", (PyCFunction)edge_conn_connection_made, METH_O, NULL},
    {"connection_lost", (PyCFunction)edge_conn_connection_lost, METH_O, NULL},
    {"data_received", (PyCFunction)edge_conn_data_received, METH_O, NULL},
    {"get_buffer", (PyCFunction)edge_conn_get_buffer, METH_O, NULL},
    {"buffer_updated", (PyCFunction)edge_conn_buffer_updated, METH_O, NULL},
    {"eof_received", (PyCFunction)edge_conn_eof_received, METH_NOARGS, NULL},
    {"pause_writing", (PyCFunction)edge_conn_noop, METH_NOARGS, NULL},
    {"resume_writing", (PyCFunction)edge_conn_noop, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};


static PyType_Slot edge_conn_slots[] = {
    {Py_tp_doc, "One pre-warmed connection to an origin."},
    {Py_tp_new, edge_conn_new},
    {Py_tp_dealloc, edge_conn_dealloc},
    {Py_tp_traverse, edge_conn_traverse},
    {Py_tp_clear, edge_conn_clear},
    {Py_tp_methods, edge_conn_methods},
    {Py_bf_getbuffer, edge_conn_getbuffer},
    {Py_bf_releasebuffer, edge_conn_releasebuffer},
    {0, NULL},
};


static PyType_Spec edge_conn_spec = {
    .name = "wreath._native._edge.UpstreamConnection",
    .basicsize = sizeof(EdgeConn),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = edge_conn_slots,
};


/* --- the table ------------------------------------------------------------ */

static PyObject *
edge_table_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *entries, *via, *scheme, *on_lost = Py_None;
    const char *policy = "ewma";
    int eject_failures = 3;
    double eject_seconds = 5.0, eject_cap = 60.0;
    Py_ssize_t max_body = 8 * 1024 * 1024;
    static char *kwlist[] = {
        "entries", "via", "scheme", "policy", "eject_failures",
        "eject_seconds", "eject_cap", "max_body", "on_lost", NULL,
    };
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "OSS|siddnO", kwlist, &entries, &via, &scheme,
            &policy, &eject_failures, &eject_seconds, &eject_cap, &max_body,
            &on_lost)) {
        return NULL;
    }
    int policy_id;
    if (strcmp(policy, "ewma") == 0) {
        policy_id = EDGE_POLICY_EWMA;
    }
    else if (strcmp(policy, "round-robin") == 0) {
        policy_id = EDGE_POLICY_ROUND_ROBIN;
    }
    else if (strcmp(policy, "least-connections") == 0) {
        policy_id = EDGE_POLICY_LEAST_CONNECTIONS;
    }
    else {
        PyErr_Format(PyExc_ValueError, "unknown policy: %s", policy);
        return NULL;
    }

    PyObject *fast = PySequence_Fast(entries, "entries must be a sequence");
    if (fast == NULL) {
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    if (count == 0) {
        Py_DECREF(fast);
        PyErr_SetString(PyExc_ValueError,
                        "an upstream table needs at least one upstream");
        return NULL;
    }

    EdgeTable *self = (EdgeTable *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_DECREF(fast);
        return NULL;
    }
    self->ups = PyMem_Calloc((size_t)count, sizeof(EdgeUpstream));
    if (self->ups == NULL) {
        Py_DECREF(fast);
        Py_DECREF(self);
        return PyErr_NoMemory();
    }
    self->count = count;
    self->policy = policy_id;
    self->eject_failures = eject_failures;
    self->eject_seconds = eject_seconds;
    self->eject_cap = eject_cap;
    self->max_body = max_body;
    self->cursor = 0;
    self->via = Py_NewRef(via);
    self->scheme = Py_NewRef(scheme);
    self->on_lost = Py_NewRef(on_lost);
    self->live = PySet_New(NULL);
    if (self->live == NULL) {
        Py_DECREF(fast);
        Py_DECREF(self);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *authority = PySequence_Fast_GET_ITEM(fast, i);
        if (!PyBytes_Check(authority)) {
            Py_DECREF(fast);
            Py_DECREF(self);
            PyErr_SetString(PyExc_TypeError,
                            "each entry must be the upstream authority, as bytes");
            return NULL;
        }
        self->ups[i].authority = Py_NewRef(authority);
        self->ups[i].latency = EDGE_COLD_LATENCY;
    }
    Py_DECREF(fast);
    return (PyObject *)self;
}


static PyObject *
edge_table_connections(EdgeTable *self, PyObject *Py_UNUSED(a))
{
    Py_ssize_t total = 0;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        total += self->ups[i].open;
    }
    return PyLong_FromSsize_t(total);
}


static PyObject *
edge_table_stats(EdgeTable *self, PyObject *Py_UNUSED(a))
{
    double now = edge_now();
    Py_ssize_t healthy = 0, inflight = 0, requests = 0, open = 0, waiting = 0;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        EdgeUpstream *u = &self->ups[i];
        healthy += u->ejected_until <= now ? 1 : 0;
        inflight += u->inflight;
        requests += u->total;
        open += u->open;
        waiting += u->waiting;
    }
    return Py_BuildValue(
        "{s:n,s:n,s:n,s:n,s:n,s:n}",
        "upstreams", self->count, "healthy", healthy, "inflight", inflight,
        "requests", requests, "connections", open, "waiting", waiting);
}


static PyObject *
edge_table_close(EdgeTable *self, PyObject *Py_UNUSED(a))
{
    self->closing = 1;
    PyObject *live = PySequence_List(self->live);
    if (live == NULL) {
        return NULL;
    }
    Py_ssize_t n = PyList_GET_SIZE(live);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyList_GET_ITEM(live, i);
        if (Py_IS_TYPE(item, edge_conn_type)) {
            sink_close(&((EdgeConn *)item)->sink);
        }
        else {
            sink_close(&((EdgeClient *)item)->sink);
        }
    }
    Py_DECREF(live);
    Py_RETURN_NONE;
}


static int
edge_table_traverse(PyObject *op, visitproc visit, void *arg)
{
    EdgeTable *self = (EdgeTable *)op;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->live);
    Py_VISIT(self->on_lost);
    return 0;
}


static int
edge_table_clear(PyObject *op)
{
    EdgeTable *self = (EdgeTable *)op;
    Py_CLEAR(self->live);
    Py_CLEAR(self->on_lost);
    return 0;
}


static void
edge_table_dealloc(PyObject *op)
{
    EdgeTable *self = (EdgeTable *)op;
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    edge_table_clear(op);
    Py_CLEAR(self->via);
    Py_CLEAR(self->scheme);
    if (self->ups != NULL) {
        for (Py_ssize_t i = 0; i < self->count; i++) {
            Py_CLEAR(self->ups[i].authority);
        }
        PyMem_Free(self->ups);
        self->ups = NULL;
    }
    type->tp_free(op);
    Py_DECREF(type);
}


static PyMethodDef edge_table_methods[] = {
    {"connections", (PyCFunction)edge_table_connections, METH_NOARGS,
     "connections() -> int: upstream connections currently established."},
    {"stats", (PyCFunction)edge_table_stats, METH_NOARGS,
     "stats() -> dict[str, int]: live counters."},
    {"close", (PyCFunction)edge_table_close, METH_NOARGS,
     "close() -> None: close every client and upstream connection."},
    {NULL, NULL, 0, NULL},
};


static PyType_Slot edge_table_slots[] = {
    {Py_tp_doc, "The compiled upstream table: origins, health, and open sockets."},
    {Py_tp_new, edge_table_new},
    {Py_tp_dealloc, edge_table_dealloc},
    {Py_tp_traverse, edge_table_traverse},
    {Py_tp_clear, edge_table_clear},
    {Py_tp_methods, edge_table_methods},
    {0, NULL},
};


static PyType_Spec edge_table_spec = {
    .name = "wreath._native._edge.UpstreamTable",
    .basicsize = sizeof(EdgeTable),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = edge_table_slots,
};


/* --- registration --------------------------------------------------------- */

/* Both protocols must genuinely inherit `asyncio.BufferedProtocol`: asyncio
 * selects the zero-copy receive path with an isinstance() check, not by method
 * presence. */
static PyObject *
make_buffered_type(PyType_Spec *spec)
{
    PyObject *protocols = PyImport_ImportModule("asyncio.protocols");
    if (protocols == NULL) {
        return NULL;
    }
    PyObject *base = PyObject_GetAttrString(protocols, "BufferedProtocol");
    Py_DECREF(protocols);
    if (base == NULL) {
        return NULL;
    }
    PyObject *bases = PyTuple_Pack(1, base);
    Py_DECREF(base);
    if (bases == NULL) {
        return NULL;
    }
    PyObject *type = PyType_FromSpecWithBases(spec, bases);
    Py_DECREF(bases);
    return type;
}


int
wreath_edge_serve_ready(PyObject *module)
{
    PyObject *table = PyType_FromSpec(&edge_table_spec);
    if (table == NULL) {
        return -1;
    }
    edge_table_type = (PyTypeObject *)table;
    if (PyModule_AddObjectRef(module, "UpstreamTable", table) < 0) {
        Py_DECREF(table);
        return -1;
    }
    Py_DECREF(table);

    PyObject *client = make_buffered_type(&edge_client_spec);
    if (client == NULL) {
        return -1;
    }
    edge_client_type = (PyTypeObject *)client;
    if (PyModule_AddObjectRef(module, "EdgeProtocol", client) < 0) {
        Py_DECREF(client);
        return -1;
    }
    Py_DECREF(client);

    PyObject *conn = make_buffered_type(&edge_conn_spec);
    if (conn == NULL) {
        return -1;
    }
    edge_conn_type = (PyTypeObject *)conn;
    if (PyModule_AddObjectRef(module, "UpstreamConnection", conn) < 0) {
        Py_DECREF(conn);
        return -1;
    }
    Py_DECREF(conn);
    return 0;
}
