/* Optional HTTP/3 backend boundary for Wreath (ADR 0011).
 *
 * All ngtcp2 / nghttp3 usage is isolated behind this header. Built only when
 * WREATH_BUILD_HTTP3=1; a default install never references it.
 *
 * Ownership:
 *   - Wreath owns ASGI integration, limits, connection/stream lifecycle, timers,
 *     and graceful shutdown.
 *   - ngtcp2 owns QUIC transport, ACK/loss state, and QUIC-TLS.
 *   - nghttp3 owns HTTP/3 framing and QPACK.
 *
 * Files:
 *   http3_connection.c  UDP endpoint (asyncio.DatagramProtocol), QUIC/ngtcp2,
 *                       TLS, connection table, timers, packet I/O.
 *   http3_asgi.c        nghttp3 callbacks, per-stream ASGI bridge, responses.
 */
#ifndef WREATH_NATIVE_HTTP3_H
#define WREATH_NATIVE_HTTP3_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdint.h>
#include <sys/socket.h>

#include <ngtcp2/ngtcp2.h>
#include <ngtcp2/ngtcp2_crypto.h>
#include <ngtcp2/ngtcp2_crypto_ossl.h>
#include <nghttp3/nghttp3.h>
#include <openssl/ssl.h>

#include "flight.h"

struct WreathH3Endpoint;
struct WreathH3Conn;

/* Native Flight Recorder vtable for _http3, resolved once at module init (the
 * same optional capsule _server resolves). NULL leaves every hook a not-taken
 * branch, so a default build pays nothing. Defined in http3_connection.c. */
extern const WreathFlightCAPI *wreath_h3_flight_capi;
uint64_t wreath_h3_next_connection_id(void);

/* Per request stream: ASGI plumbing, mirrors the HTTP/2 stream object. */
typedef struct {
    PyObject_HEAD
    struct WreathH3Conn *conn;     /* borrowed; cleared when the stream closes */
    int64_t stream_id;

    PyObject *loop;             /* owned; outlives `conn` so a receive() after
                                 * detach still resolves instead of crashing */
    PyObject *scope;
    PyObject *task;
    PyObject *receive_callable;
    PyObject *send_callable;
    PyObject *done_callable;

    PyObject *header_list;       /* list[(name,value)] during header assembly */
    PyObject *body_chunks;       /* list[bytes] buffered request body */
    Py_ssize_t body_head;        /* first unconsumed chunk; see the queue rule */
    Py_ssize_t body_received;    /* request payload bytes accepted for this stream */
    PyObject *receive_waiter;    /* Future or NULL */
    int request_ended;
    int disconnected;

    int response_started;
    int response_ended;          /* response submitted to nghttp3 */
    int status;
    PyObject *resp_headers;      /* list[(name,value)] pending for submit */
    /* Response body as immutable segments with stable addresses. nghttp3 is
     * handed pointers into these bytes objects and may reference them until the
     * peer acknowledges them, so a segment is released only once fully acked. */
    PyObject *resp_chunks;       /* list[bytes] */
    Py_ssize_t resp_head;        /* first segment still retained */
    Py_ssize_t resp_read_index;  /* segment currently offered to nghttp3 */
    Py_ssize_t resp_read_offset; /* offset inside that segment */
    uint64_t resp_payload_acked; /* acked payload bytes not yet attributed */
    int resp_eof;                /* response body complete (fin) */

    /* Native Flight Recorder per-stream context (one request). Only touched
     * while telemetry is on; nfr_bytes_out counts response body payload. */
    wreath_nfr_context nfr_ctx;
    int nfr_active;
    uint64_t nfr_bytes_out;
} WreathH3Stream;

/* Per QUIC connection. Plain C struct; owned by a PyCapsule in the endpoint's
 * connection table and freed by that capsule's destructor. */
typedef struct WreathH3Conn {
    struct WreathH3Endpoint *endpoint;   /* borrowed */
    ngtcp2_conn *conn;
    nghttp3_conn *h3;
    SSL *ssl;
    ngtcp2_crypto_ossl_ctx *ossl_ctx;
    ngtcp2_crypto_conn_ref conn_ref;
    struct sockaddr_storage remote_addr;
    socklen_t remote_addrlen;
    struct sockaddr_storage local_addr;
    socklen_t local_addrlen;
    uint8_t scid[NGTCP2_MAX_CIDLEN];
    size_t scidlen;
    PyObject *streams;                /* dict {int64 stream_id: WreathH3Stream} */
    PyObject *cids;                   /* list[bytes]: every routing CID */
    PyObject *capsule;                /* borrowed self-capsule (no destructor) */
    uint64_t nfr_connection_id;       /* 0 when telemetry is off */
    int handshake_done;
    int closed;
    int draining;
} WreathH3Conn;

/* The UDP datagram endpoint (asyncio.DatagramProtocol). */
typedef struct WreathH3Endpoint {
    PyObject_HEAD
    PyObject *app;
    PyObject *config;
    PyObject *loop;
    PyObject *registry;
    PyObject *transport;              /* DatagramTransport */
    PyObject *transport_sendto;       /* bound transport.sendto */
    PyObject *loop_create_future;
    PyObject *loop_create_task;
    PyObject *loop_call_at;
    PyObject *timer_handle;           /* TimerHandle or NULL */
    double timer_target;              /* loop.time() the timer will fire, or -1 */
    SSL_CTX *ssl_ctx;
    PyObject *conns;                  /* dict {bytes(cid): capsule(WreathH3Conn*)} */
    struct sockaddr_storage local_addr_store;
    socklen_t local_addrlen_store;
    WreathH3Conn **reap;                 /* connections to free after the callback */
    Py_ssize_t reap_len;
    Py_ssize_t reap_cap;
    int accepting;
    int active_requests;

    /* limits (from ServerConfig) */
    Py_ssize_t max_concurrent_streams;
    Py_ssize_t max_body_bytes;
    Py_ssize_t max_header_list_bytes;
    Py_ssize_t initial_stream_window;
    Py_ssize_t initial_connection_window;
    Py_ssize_t qpack_table_bytes;
    Py_ssize_t qpack_blocked_streams;

    /* Native Flight Recorder: borrowed worker (or NULL when telemetry is off). */
    wreath_nfr_worker *nfr_worker;
} WreathH3Endpoint;

/* --- shared between the two units ----------------------------------------- */

/* connection.c */
int wreath_h3_ready(PyObject *module);          /* register endpoint + stream types */
uint64_t wreath_h3_timestamp(void);             /* monotonic ns for ngtcp2 */
int wreath_h3_flush(WreathH3Conn *c);              /* drive writes for one connection */
extern PyTypeObject *WreathH3StreamType;

/* asgi.c: nghttp3 wiring + ASGI */
int wreath_h3_setup_httpconn(WreathH3Conn *c);     /* create nghttp3_conn + bind streams */
int wreath_h3_init_message_keys(void);          /* intern the ASGI message keys once */
void wreath_h3_stream_disconnect(WreathH3Stream *s);
int wreath_h3_alpn_select_cb(SSL *ssl, const unsigned char **out, unsigned char *outlen,
                          const unsigned char *in, unsigned int inlen, void *arg);
nghttp3_ssize wreath_h3_writev(WreathH3Conn *c, int64_t *stream_id, int *fin,
                            nghttp3_vec *vec, size_t veccnt);
int wreath_h3_recv_stream_data(WreathH3Conn *c, int64_t stream_id, int fin,
                            const uint8_t *data, size_t datalen);
int wreath_h3_stream_close(WreathH3Conn *c, int64_t stream_id, uint64_t app_error);
int wreath_h3_acked_stream_data(WreathH3Conn *c, int64_t stream_id, uint64_t datalen);
extern PyType_Spec wreath_h3_stream_spec;

#endif /* WREATH_NATIVE_HTTP3_H */
