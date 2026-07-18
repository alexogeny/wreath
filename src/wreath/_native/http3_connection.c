/* HTTP/3 (QUIC) endpoint: UDP polling boundary + ngtcp2 transport + TLS.
 *
 * asyncio.DatagramProtocol is used only to receive/send UDP datagrams; packet
 * parsing, ACK/loss state, stream dispatch and TLS stay native (ngtcp2). See
 * ADR 0011. nghttp3/QPACK and the ASGI bridge live in http3_asgi.c.
 */
#include "http3.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <string.h>
#include <time.h>

PyTypeObject *WreathH3StreamType = NULL;
static PyTypeObject *WreathH3EndpointType = NULL;

/* --- time ---------------------------------------------------------------- */
uint64_t
wreath_h3_timestamp(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * NGTCP2_SECONDS + (uint64_t)ts.tv_nsec;
}

/* --- Native Flight Recorder (optional) ----------------------------------- */
/* Resolved once in wreath_h3_ready; NULL keeps every hook a not-taken branch.
 * H3 uses wreath_h3_timestamp() (CLOCK_MONOTONIC ns) for both context start and
 * end, so durations stay internally consistent within the recorder. */
const WreathFlightCAPI *wreath_h3_flight_capi = NULL;
static _Atomic uint64_t wreath_h3_conn_counter = 0;

uint64_t
wreath_h3_next_connection_id(void)
{
    return atomic_fetch_add_explicit(&wreath_h3_conn_counter, 1,
                                     memory_order_relaxed) + 1;
}

/* Resolve the wreath_nfr_worker* a recorder object owns (borrowed), or NULL.
 * Mirrors wreath_flight_worker_from in _server; _http3 is a separate extension
 * and cannot call into _server, so it repeats the tiny capsule attribute walk. */
static wreath_nfr_worker *
wreath_h3_worker_from(PyObject *recorder)
{
    if (recorder == NULL || recorder == Py_None) {
        return NULL;
    }
    PyObject *method = PyObject_GetAttrString(recorder, "worker_capsule");
    if (method == NULL) {
        PyErr_Clear();
        return NULL;
    }
    PyObject *capsule = PyObject_CallNoArgs(method);
    Py_DECREF(method);
    if (capsule == NULL) {
        PyErr_Clear();
        return NULL;
    }
    void *pointer = PyCapsule_GetPointer(capsule, WREATH_FLIGHT_WORKER_CAPSULE);
    Py_DECREF(capsule);
    if (pointer == NULL) {
        PyErr_Clear();
        return NULL;
    }
    return (wreath_nfr_worker *)pointer;
}

/* Length of every connection ID we issue. Short-header (1-RTT) packets do not
 * carry the DCID length, so the decoder must be told this exact value. */
#define WREATH_H3_CIDLEN 18

/* Per-process secret for stateless-reset and Retry tokens (dev-grade). */
static const uint8_t wreath_h3_secret[32] =
    "neo-http3-static-secret-32byte!!";

/* --- address conversion -------------------------------------------------- */

/* Convert a Python (host, port[, flowinfo, scopeid]) tuple into a sockaddr. */
static int
py_addr_to_sockaddr(PyObject *addr, struct sockaddr_storage *ss, socklen_t *len)
{
    const char *host;
    int port;
    if (!PyArg_ParseTuple(addr, "si|ii", &host, &port, &(int){0}, &(int){0})) {
        /* fall back to 2-tuple */
        PyErr_Clear();
        if (!PyArg_ParseTuple(addr, "si", &host, &port)) {
            return -1;
        }
    }
    memset(ss, 0, sizeof(*ss));
    if (strchr(host, ':') != NULL) {
        struct sockaddr_in6 *a = (struct sockaddr_in6 *)ss;
        a->sin6_family = AF_INET6;
        a->sin6_port = htons((uint16_t)port);
        if (inet_pton(AF_INET6, host, &a->sin6_addr) != 1) {
            return -1;
        }
        *len = sizeof(*a);
    } else {
        struct sockaddr_in *a = (struct sockaddr_in *)ss;
        a->sin_family = AF_INET;
        a->sin_port = htons((uint16_t)port);
        if (inet_pton(AF_INET, host, &a->sin_addr) != 1) {
            return -1;
        }
        *len = sizeof(*a);
    }
    return 0;
}

/* Convert a sockaddr into a Python (host, port) tuple. */
static PyObject *
sockaddr_to_py(const struct sockaddr *sa)
{
    char host[INET6_ADDRSTRLEN];
    int port;
    if (sa->sa_family == AF_INET6) {
        const struct sockaddr_in6 *a = (const struct sockaddr_in6 *)sa;
        inet_ntop(AF_INET6, &a->sin6_addr, host, sizeof(host));
        port = ntohs(a->sin6_port);
    } else {
        const struct sockaddr_in *a = (const struct sockaddr_in *)sa;
        inet_ntop(AF_INET, &a->sin_addr, host, sizeof(host));
        port = ntohs(a->sin_port);
    }
    return Py_BuildValue("(si)", host, port);
}

/* --- connection table ---------------------------------------------------- */

static WreathH3Conn *
find_conn(WreathH3Endpoint *ep, const uint8_t *cid, size_t cidlen)
{
    PyObject *key = PyBytes_FromStringAndSize((const char *)cid, (Py_ssize_t)cidlen);
    if (key == NULL) {
        PyErr_Clear();
        return NULL;
    }
    PyObject *cap = PyDict_GetItemWithError(ep->conns, key);
    Py_DECREF(key);
    if (cap == NULL) {
        return NULL;
    }
    return (WreathH3Conn *)PyCapsule_GetPointer(cap, "neoh3conn");
}

static int
register_cid(WreathH3Endpoint *ep, WreathH3Conn *c, const uint8_t *cid, size_t cidlen)
{
    PyObject *key = PyBytes_FromStringAndSize((const char *)cid, (Py_ssize_t)cidlen);
    if (key == NULL) {
        return -1;
    }
    int rc = PyDict_SetItem(ep->conns, key, c->capsule);
    if (rc == 0) {
        rc = PyList_Append(c->cids, key);  /* track for teardown */
    }
    Py_DECREF(key);
    return rc;
}

static void
unregister_cid(WreathH3Endpoint *ep, const uint8_t *cid, size_t cidlen)
{
    PyObject *key = PyBytes_FromStringAndSize((const char *)cid, (Py_ssize_t)cidlen);
    if (key == NULL) {
        PyErr_Clear();
        return;
    }
    if (PyDict_DelItem(ep->conns, key) < 0) {
        PyErr_Clear();
    }
    Py_DECREF(key);
}

/* --- ngtcp2 callbacks ---------------------------------------------------- */

static ngtcp2_conn *
get_conn_cb(ngtcp2_crypto_conn_ref *ref)
{
    return ((WreathH3Conn *)ref->user_data)->conn;
}

static void
rand_cb(uint8_t *dest, size_t destlen, const ngtcp2_rand_ctx *rand_ctx)
{
    (void)rand_ctx;
    for (size_t i = 0; i < destlen; i++) {
        dest[i] = (uint8_t)(rand() & 0xff);
    }
}

static int
get_new_connection_id_cb(ngtcp2_conn *conn, ngtcp2_cid *cid, uint8_t *token,
                         size_t cidlen, void *user_data)
{
    (void)conn;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    for (size_t i = 0; i < cidlen; i++) {
        cid->data[i] = (uint8_t)(rand() & 0xff);
    }
    cid->datalen = cidlen;
    if (ngtcp2_crypto_generate_stateless_reset_token(
            token, wreath_h3_secret, sizeof(wreath_h3_secret), cid) != 0) {
        return NGTCP2_ERR_CALLBACK_FAILURE;
    }
    if (c->capsule == NULL) {
        /* Called during ngtcp2_conn_server_new before the capsule exists; defer
         * registration -- create_conn registers the scid explicitly. */
        return 0;
    }
    if (register_cid(c->endpoint, c, cid->data, cid->datalen) < 0) {
        PyErr_Clear();
        return NGTCP2_ERR_CALLBACK_FAILURE;
    }
    return 0;
}

static int
remove_connection_id_cb(ngtcp2_conn *conn, const ngtcp2_cid *cid, void *user_data)
{
    (void)conn;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    unregister_cid(c->endpoint, cid->data, cid->datalen);
    return 0;
}

/* declared in http3_asgi.c */
int wreath_h3_recv_stream_data(WreathH3Conn *c, int64_t stream_id, int fin,
                            const uint8_t *data, size_t datalen);
int wreath_h3_stream_close(WreathH3Conn *c, int64_t stream_id, uint64_t app_error);
int wreath_h3_acked_stream_data(WreathH3Conn *c, int64_t stream_id, uint64_t datalen);

static int
recv_stream_data_cb(ngtcp2_conn *conn, uint32_t flags, int64_t stream_id,
                    uint64_t offset, const uint8_t *data, size_t datalen,
                    void *user_data, void *stream_user_data)
{
    (void)offset;
    (void)stream_user_data;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    int fin = (flags & NGTCP2_STREAM_DATA_FLAG_FIN) ? 1 : 0;
    int nconsumed = wreath_h3_recv_stream_data(c, stream_id, fin, data, datalen);
    if (nconsumed < 0) {
        return NGTCP2_ERR_CALLBACK_FAILURE;
    }
    /* Extend by what nghttp3 consumed now; deferred data is credited later via
     * the deferred_consume callback. */
    ngtcp2_conn_extend_max_stream_offset(conn, stream_id, (uint64_t)nconsumed);
    ngtcp2_conn_extend_max_offset(conn, (uint64_t)nconsumed);
    return 0;
}

static int
stream_close_cb(ngtcp2_conn *conn, uint32_t flags, int64_t stream_id,
                uint64_t app_error_code, void *user_data, void *stream_user_data)
{
    (void)flags;
    (void)stream_user_data;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    wreath_h3_stream_close(c, stream_id, app_error_code);
    /* Give the credit back.
     *
     * `initial_max_streams_bidi` is a *budget*, not a concurrency limit: it is
     * the number of streams the peer may ever open, and it only replenishes
     * when the server sends MAX_STREAMS. HTTP/3 opens one bidirectional stream
     * per request, so without this a connection serves exactly
     * `max_concurrent_streams` requests and then stalls forever -- the 101st
     * request on a kept-alive connection never gets a stream. Extending on
     * close is what makes the setting mean "concurrent" at all.
     *
     * Only client-initiated bidirectional streams carry requests. In QUIC's
     * stream-id encoding bit 0 is the initiator (0 = client) and bit 1 is the
     * directionality (0 = bidi), so those are exactly the ids with both low
     * bits clear. Server-initiated and unidirectional streams (control, QPACK)
     * draw on other budgets and must not be credited here. */
    if ((stream_id & 0x03) == 0x00) {
        ngtcp2_conn_extend_max_streams_bidi(conn, 1);
    }
    return 0;
}

static int
acked_stream_data_offset_cb(ngtcp2_conn *conn, int64_t stream_id, uint64_t offset,
                            uint64_t datalen, void *user_data,
                            void *stream_user_data)
{
    (void)conn;
    (void)offset;
    (void)stream_user_data;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    wreath_h3_acked_stream_data(c, stream_id, datalen);
    return 0;
}

static int
handshake_completed_cb(ngtcp2_conn *conn, void *user_data)
{
    (void)conn;
    WreathH3Conn *c = (WreathH3Conn *)user_data;
    c->handshake_done = 1;
    int rc = wreath_h3_setup_httpconn(c);
    if (rc < 0) {
        return NGTCP2_ERR_CALLBACK_FAILURE;
    }
    return 0;
}

static void
fill_callbacks(ngtcp2_callbacks *cb)
{
    memset(cb, 0, sizeof(*cb));
    cb->recv_client_initial = ngtcp2_crypto_recv_client_initial_cb;
    cb->recv_crypto_data = ngtcp2_crypto_recv_crypto_data_cb;
    cb->encrypt = ngtcp2_crypto_encrypt_cb;
    cb->decrypt = ngtcp2_crypto_decrypt_cb;
    cb->hp_mask = ngtcp2_crypto_hp_mask_cb;
    cb->update_key = ngtcp2_crypto_update_key_cb;
    cb->delete_crypto_aead_ctx = ngtcp2_crypto_delete_crypto_aead_ctx_cb;
    cb->delete_crypto_cipher_ctx = ngtcp2_crypto_delete_crypto_cipher_ctx_cb;
    cb->get_path_challenge_data = ngtcp2_crypto_get_path_challenge_data_cb;
    cb->version_negotiation = ngtcp2_crypto_version_negotiation_cb;
    cb->handshake_completed = handshake_completed_cb;
    cb->recv_stream_data = recv_stream_data_cb;
    cb->stream_close = stream_close_cb;
    cb->acked_stream_data_offset = acked_stream_data_offset_cb;
    cb->rand = rand_cb;
    cb->get_new_connection_id = get_new_connection_id_cb;
    cb->remove_connection_id = remove_connection_id_cb;
}

static int send_datagram(WreathH3Endpoint *ep, const struct sockaddr *sa,
                         socklen_t salen, const uint8_t *data, size_t datalen);

/* Send a Retry packet for address validation (RFC 9000 s8.1). The client will
 * resend its Initial carrying the token, which we verify before creating a
 * connection -- this bounds amplification before the peer address is validated. */
static int
send_retry(WreathH3Endpoint *ep, const ngtcp2_pkt_hd *hd,
           const struct sockaddr *remote, socklen_t remotelen)
{
    ngtcp2_cid retry_scid;
    retry_scid.datalen = WREATH_H3_CIDLEN;
    for (size_t i = 0; i < retry_scid.datalen; i++) {
        retry_scid.data[i] = (uint8_t)(rand() & 0xff);
    }
    uint8_t token[NGTCP2_CRYPTO_MAX_RETRY_TOKENLEN2];
    ngtcp2_ssize tokenlen = ngtcp2_crypto_generate_retry_token2(
        token, wreath_h3_secret, sizeof(wreath_h3_secret), hd->version,
        (const ngtcp2_sockaddr *)remote, remotelen, &retry_scid, &hd->dcid,
        wreath_h3_timestamp());
    if (tokenlen < 0) {
        return -1;
    }
    uint8_t buf[256];
    ngtcp2_ssize n = ngtcp2_crypto_write_retry(
        buf, sizeof(buf), hd->version, &hd->scid, &retry_scid, &hd->dcid,
        token, (size_t)tokenlen);
    if (n < 0) {
        return -1;
    }
    return send_datagram(ep, remote, remotelen, buf, (size_t)n);
}

/* --- connection lifecycle ------------------------------------------------ */

/* Release native resources (safe to call once). The WreathH3Conn struct itself is
 * freed later by reap_conns() so that callers still holding `c` can read
 * c->closed without a use-after-free. */
static void
release_native(WreathH3Conn *c)
{
    if (c->h3 != NULL) {
        nghttp3_conn_del(c->h3);
        c->h3 = NULL;
    }
    if (c->conn != NULL) {
        ngtcp2_conn_del(c->conn);
        c->conn = NULL;
    }
    if (c->ossl_ctx != NULL) {
        ngtcp2_crypto_ossl_ctx_del(c->ossl_ctx);
        c->ossl_ctx = NULL;
    }
    if (c->ssl != NULL) {
        SSL_free(c->ssl);
        c->ssl = NULL;
    }
    Py_CLEAR(c->streams);
}

/* Free every connection struct queued for reaping. Called at the end of the
 * top-level datagram / timer entry points, once no caller still holds `c`. */
static void
reap_conns(WreathH3Endpoint *ep)
{
    for (Py_ssize_t i = 0; i < ep->reap_len; i++) {
        Py_CLEAR(ep->reap[i]->cids);
        PyMem_Free(ep->reap[i]);
    }
    ep->reap_len = 0;
}

/* Tear a connection down: disconnect streams, remove its routing CIDs (dropping
 * the capsule), release native state, and queue the struct for reaping. */
static void
close_conn(WreathH3Endpoint *ep, WreathH3Conn *c)
{
    if (c->closed) {
        return;
    }
    c->closed = 1;
    if (c->streams != NULL) {
        PyObject *values = PyDict_Values(c->streams);
        if (values != NULL) {
            for (Py_ssize_t i = 0; i < PyList_GET_SIZE(values); i++) {
                wreath_h3_stream_disconnect((WreathH3Stream *)PyList_GET_ITEM(values, i));
            }
            Py_DECREF(values);
        }
    }
    /* remove every routing CID (drops the last capsule reference) */
    if (c->cids != NULL) {
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(c->cids); i++) {
            PyObject *key = PyList_GET_ITEM(c->cids, i);
            if (PyDict_DelItem(ep->conns, key) < 0) {
                PyErr_Clear();
            }
        }
    }
    release_native(c);
    /* queue for deferred free */
    if (ep->reap_len == ep->reap_cap) {
        Py_ssize_t new_cap = ep->reap_cap ? ep->reap_cap * 2 : 8;
        WreathH3Conn **grown = PyMem_Realloc(ep->reap, sizeof(WreathH3Conn *) * (size_t)new_cap);
        if (grown == NULL) {
            /* out of memory: leak the struct rather than risk a double free */
            PyErr_Clear();
            return;
        }
        ep->reap = grown;
        ep->reap_cap = new_cap;
    }
    ep->reap[ep->reap_len++] = c;
}

/* Create a new server connection for a validated Initial packet. `odcid` is the
 * client's original destination CID (recovered from the Retry token); `hd->dcid`
 * is the Retry SCID we issued. */
static WreathH3Conn *
create_conn(WreathH3Endpoint *ep, const ngtcp2_pkt_hd *hd, const ngtcp2_path *path,
            const ngtcp2_cid *odcid)
{
    WreathH3Conn *c = PyMem_Malloc(sizeof(WreathH3Conn));
    if (c == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    memset(c, 0, sizeof(*c));
    c->endpoint = ep;
    c->nfr_connection_id = ep->nfr_worker != NULL ? wreath_h3_next_connection_id() : 0;
    c->streams = PyDict_New();
    c->cids = PyList_New(0);
    if (c->streams == NULL || c->cids == NULL) {
        Py_XDECREF(c->streams);
        Py_XDECREF(c->cids);
        PyMem_Free(c);
        return NULL;
    }

    /* our source CID */
    ngtcp2_cid scid;
    scid.datalen = WREATH_H3_CIDLEN;
    for (size_t i = 0; i < scid.datalen; i++) {
        scid.data[i] = (uint8_t)(rand() & 0xff);
    }
    memcpy(c->scid, scid.data, scid.datalen);
    c->scidlen = scid.datalen;

    ngtcp2_settings settings;
    ngtcp2_settings_default(&settings);
    settings.initial_ts = wreath_h3_timestamp();
    /* Tell ngtcp2 the peer address was validated by our Retry token, so it does
     * not ask for another Retry (read_pkt would otherwise return ERR_RETRY). */
    settings.token = hd->token;
    settings.tokenlen = hd->tokenlen;
    settings.token_type = NGTCP2_TOKEN_TYPE_RETRY;

    ngtcp2_transport_params params;
    ngtcp2_transport_params_default(&params);
    params.initial_max_stream_data_bidi_local = (uint64_t)ep->initial_stream_window;
    params.initial_max_stream_data_bidi_remote = (uint64_t)ep->initial_stream_window;
    params.initial_max_stream_data_uni = (uint64_t)ep->initial_stream_window;
    params.initial_max_data = (uint64_t)ep->initial_connection_window;
    params.initial_max_streams_bidi = (uint64_t)ep->max_concurrent_streams;
    params.initial_max_streams_uni = 3;
    params.max_idle_timeout = 30 * NGTCP2_SECONDS;
    /* We always issue a Retry, so the original DCID comes from the token and the
     * Retry SCID is the DCID of this (second) Initial. */
    params.original_dcid = *odcid;
    params.original_dcid_present = 1;
    params.retry_scid = hd->dcid;
    params.retry_scid_present = 1;

    ngtcp2_callbacks callbacks;
    fill_callbacks(&callbacks);

    int rc = ngtcp2_conn_server_new(&c->conn, &hd->scid, &scid, path, hd->version,
                                    &callbacks, &settings, &params, NULL, c);
    if (rc != 0) {
        PyErr_Format(PyExc_RuntimeError, "ngtcp2_conn_server_new: %s",
                     ngtcp2_strerror(rc));
        Py_DECREF(c->streams);
        PyMem_Free(c);
        return NULL;
    }

    /* TLS setup (OpenSSL 3.5+ QUIC via ngtcp2_crypto_ossl). */
    c->ssl = SSL_new(ep->ssl_ctx);
    if (c->ssl == NULL) {
        goto fail;
    }
    c->conn_ref.get_conn = get_conn_cb;
    c->conn_ref.user_data = c;
    SSL_set_app_data(c->ssl, &c->conn_ref);
    SSL_set_accept_state(c->ssl);
    if (ngtcp2_crypto_ossl_configure_server_session(c->ssl) != 0) {
        goto fail;
    }
    if (ngtcp2_crypto_ossl_ctx_new(&c->ossl_ctx, c->ssl) != 0) {
        goto fail;
    }
    ngtcp2_conn_set_tls_native_handle(c->conn, c->ossl_ctx);

    memcpy(&c->remote_addr, path->remote.addr, path->remote.addrlen);
    c->remote_addrlen = path->remote.addrlen;
    memcpy(&c->local_addr, path->local.addr, path->local.addrlen);
    c->local_addrlen = path->local.addrlen;

    /* Capsule holds a borrowed pointer only (no destructor); the connection
     * struct is freed by reap_conns(). It stays alive while >=1 CID is dict-registered. */
    c->capsule = PyCapsule_New(c, "neoh3conn", NULL);
    if (c->capsule == NULL) {
        goto fail;
    }
    if (register_cid(ep, c, c->scid, c->scidlen) < 0) {
        Py_DECREF(c->capsule);
        goto fail;
    }
    Py_DECREF(c->capsule);  /* dict now owns it */
    return c;

fail:
    if (c->conn) ngtcp2_conn_del(c->conn);
    if (c->ossl_ctx) ngtcp2_crypto_ossl_ctx_del(c->ossl_ctx);
    if (c->ssl) SSL_free(c->ssl);
    Py_DECREF(c->streams);
    Py_DECREF(c->cids);
    PyMem_Free(c);
    if (!PyErr_Occurred()) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP/3 connection setup failed");
    }
    return NULL;
}

/* --- packet write loop --------------------------------------------------- */

static int
send_datagram(WreathH3Endpoint *ep, const struct sockaddr *sa, socklen_t salen,
              const uint8_t *data, size_t datalen)
{
    (void)salen;
    if (ep->transport_sendto == NULL) {
        return 0;
    }
    PyObject *payload = PyBytes_FromStringAndSize((const char *)data, (Py_ssize_t)datalen);
    if (payload == NULL) {
        return -1;
    }
    PyObject *addr = sockaddr_to_py(sa);
    if (addr == NULL) {
        Py_DECREF(payload);
        return -1;
    }
    PyObject *args[2] = {payload, addr};
    PyObject *r = PyObject_Vectorcall(ep->transport_sendto, args, 2, NULL);
    Py_DECREF(payload);
    Py_DECREF(addr);
    if (r == NULL) {
        return -1;
    }
    Py_DECREF(r);
    return 0;
}

int
wreath_h3_flush(WreathH3Conn *c)
{
    WreathH3Endpoint *ep = c->endpoint;
    uint8_t buf[1452];
    ngtcp2_path_storage ps;
    ngtcp2_path_storage_zero(&ps);
    ngtcp2_pkt_info pi;
    uint64_t ts = wreath_h3_timestamp();

    for (;;) {
        int64_t stream_id = -1;
        int fin = 0;
        nghttp3_vec vec[16];
        nghttp3_ssize sveccnt = 0;
        if (c->h3 != NULL) {
            sveccnt = wreath_h3_writev(c, &stream_id, &fin, vec, 16);
            if (sveccnt < 0) {
                return -1;
            }
        }
        uint32_t flags = NGTCP2_WRITE_STREAM_FLAG_NONE;
        if (fin) {
            flags |= NGTCP2_WRITE_STREAM_FLAG_FIN;
        }
        ngtcp2_ssize ndatalen = 0;
        ngtcp2_ssize nwrite = ngtcp2_conn_writev_stream(
            c->conn, &ps.path, &pi, buf, sizeof(buf), &ndatalen, flags,
            stream_id, (const ngtcp2_vec *)vec, (size_t)sveccnt, ts);
        if (nwrite < 0) {
            if (nwrite == NGTCP2_ERR_STREAM_DATA_BLOCKED ||
                nwrite == NGTCP2_ERR_STREAM_SHUT_WR) {
                /* let ngtcp2 drain non-stream frames next iteration */
                if (c->h3 != NULL && stream_id >= 0) {
                    nghttp3_conn_block_stream(c->h3, stream_id);
                }
                continue;
            }
            if (nwrite == NGTCP2_ERR_WRITE_MORE) {
                if (c->h3 != NULL && ndatalen >= 0) {
                    nghttp3_conn_add_write_offset(c->h3, stream_id, (size_t)ndatalen);
                }
                continue;
            }
            /* fatal */
            close_conn(ep, c);
            return 0;
        }
        if (ndatalen >= 0 && c->h3 != NULL && stream_id >= 0) {
            nghttp3_conn_add_write_offset(c->h3, stream_id, (size_t)ndatalen);
        }
        if (nwrite == 0) {
            break;  /* nothing more to send right now */
        }
        if (send_datagram(ep, (const struct sockaddr *)ps.path.remote.addr,
                          ps.path.remote.addrlen, buf, (size_t)nwrite) < 0) {
            return -1;
        }
    }
    return 0;
}

/* --- timer --------------------------------------------------------------- */

static PyObject *endpoint_on_timer(PyObject *op, PyObject *ignored);

static void
rearm_timer(WreathH3Endpoint *ep)
{
    /* Find the nearest expiry across all connections. */
    uint64_t min_expiry = UINT64_MAX;
    PyObject *values = PyDict_Values(ep->conns);
    if (values != NULL) {
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(values); i++) {
            WreathH3Conn *c = (WreathH3Conn *)PyCapsule_GetPointer(
                PyList_GET_ITEM(values, i), "neoh3conn");
            if (c == NULL || c->conn == NULL) {
                continue;
            }
            uint64_t e = ngtcp2_conn_get_expiry(c->conn);
            if (e < min_expiry) {
                min_expiry = e;
            }
        }
        Py_DECREF(values);
    }
    if (min_expiry == UINT64_MAX) {
        return;
    }
    uint64_t now = wreath_h3_timestamp();
    double delay = (min_expiry <= now) ? 0.0
                   : (double)(min_expiry - now) / (double)NGTCP2_SECONDS;
    /* schedule loop.call_later(delay, self._on_timer) */
    PyObject *cb = PyObject_GetAttrString((PyObject *)ep, "_on_timer");
    if (cb == NULL) {
        PyErr_Clear();
        return;
    }
    PyObject *loop_call_later = PyObject_GetAttrString(ep->loop, "call_later");
    if (loop_call_later == NULL) {
        Py_DECREF(cb);
        PyErr_Clear();
        return;
    }
    PyObject *handle = PyObject_CallFunction(loop_call_later, "dO", delay, cb);
    Py_DECREF(cb);
    Py_DECREF(loop_call_later);
    if (handle == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XSETREF(ep->timer_handle, handle);
}

static PyObject *
endpoint_on_timer(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    uint64_t now = wreath_h3_timestamp();
    PyObject *values = PyDict_Values(ep->conns);
    if (values != NULL) {
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(values); i++) {
            WreathH3Conn *c = (WreathH3Conn *)PyCapsule_GetPointer(
                PyList_GET_ITEM(values, i), "neoh3conn");
            if (c == NULL || c->conn == NULL || c->closed) {
                continue;
            }
            if (ngtcp2_conn_get_expiry(c->conn) <= now) {
                int rc = ngtcp2_conn_handle_expiry(c->conn, now);
                if (rc != 0) {
                    close_conn(ep, c);
                    continue;
                }
                if (wreath_h3_flush(c) < 0) {
                    Py_DECREF(values);
                    return NULL;
                }
            }
        }
        Py_DECREF(values);
    }
    reap_conns(ep);
    rearm_timer(ep);
    Py_RETURN_NONE;
}

/* --- datagram reception -------------------------------------------------- */

static PyObject *
endpoint_datagram_received(PyObject *op, PyObject *args)
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    PyObject *data;
    PyObject *addr;
    if (!PyArg_ParseTuple(args, "OO", &data, &addr)) {
        return NULL;
    }
    char *pkt;
    Py_ssize_t pktlen;
    if (PyBytes_AsStringAndSize(data, &pkt, &pktlen) < 0) {
        return NULL;
    }
    struct sockaddr_storage remote;
    socklen_t remotelen;
    if (py_addr_to_sockaddr(addr, &remote, &remotelen) < 0) {
        PyErr_Clear();
        Py_RETURN_NONE;  /* ignore undecodable source address */
    }

    ngtcp2_version_cid vc;
    int rc = ngtcp2_pkt_decode_version_cid(&vc, (const uint8_t *)pkt,
                                           (size_t)pktlen, WREATH_H3_CIDLEN);
    if (rc == NGTCP2_ERR_VERSION_NEGOTIATION) {
        Py_RETURN_NONE;  /* v1-only: drop unsupported versions (no ASGI) */
    }
    if (rc != 0) {
        Py_RETURN_NONE;  /* malformed packet: never reaches ASGI */
    }

    WreathH3Conn *c = find_conn(ep, vc.dcid, vc.dcidlen);
    ngtcp2_path path;
    memset(&path, 0, sizeof(path));
    path.local.addr = (ngtcp2_sockaddr *)&ep->local_addr_store;
    path.local.addrlen = ep->local_addrlen_store;
    path.remote.addr = (ngtcp2_sockaddr *)&remote;
    path.remote.addrlen = remotelen;

    if (c == NULL) {
        if (!ep->accepting) {
            Py_RETURN_NONE;
        }
        ngtcp2_pkt_hd hd;
        if (ngtcp2_accept(&hd, (const uint8_t *)pkt, (size_t)pktlen) != 0) {
            Py_RETURN_NONE;  /* not a valid Initial: drop */
        }
        /* Anti-amplification: require a validated address via Retry before
         * creating any connection state (RFC 9000 s8.1). */
        if (hd.tokenlen == 0) {
            send_retry(ep, &hd, (const struct sockaddr *)&remote, remotelen);
            Py_RETURN_NONE;  /* client resends its Initial with the token */
        }
        ngtcp2_cid odcid;
        if (ngtcp2_crypto_verify_retry_token2(
                &odcid, hd.token, hd.tokenlen, wreath_h3_secret,
                sizeof(wreath_h3_secret), hd.version,
                (const ngtcp2_sockaddr *)&remote, remotelen, &hd.dcid,
                10 * NGTCP2_SECONDS, wreath_h3_timestamp()) != 0) {
            Py_RETURN_NONE;  /* invalid/expired token: drop */
        }
        c = create_conn(ep, &hd, &path, &odcid);
        if (c == NULL) {
            return NULL;
        }
    }

    ngtcp2_pkt_info pi;
    memset(&pi, 0, sizeof(pi));
    rc = ngtcp2_conn_read_pkt(c->conn, &path, &pi, (const uint8_t *)pkt,
                              (size_t)pktlen, wreath_h3_timestamp());
    if (rc != 0) {
        /* Fatal transport error: tear the connection down (no ASGI leak). */
        close_conn(ep, c);
        Py_RETURN_NONE;
    }
    if (!c->closed && wreath_h3_flush(c) < 0) {
        return NULL;
    }
    if (!c->closed &&
        (ngtcp2_conn_in_closing_period(c->conn) ||
         ngtcp2_conn_in_draining_period(c->conn))) {
        close_conn(ep, c);
    }
    reap_conns(ep);
    rearm_timer(ep);
    Py_RETURN_NONE;
}

/* --- endpoint asyncio.DatagramProtocol interface ------------------------- */

static PyObject *
endpoint_connection_made(PyObject *op, PyObject *transport)
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    ep->transport = Py_NewRef(transport);
    ep->transport_sendto = PyObject_GetAttrString(transport, "sendto");
    if (ep->transport_sendto == NULL) {
        return NULL;
    }
    /* cache local sockname for path.local */
    PyObject *get_extra = PyObject_GetAttrString(transport, "get_extra_info");
    if (get_extra != NULL) {
        PyObject *sockname = PyObject_CallFunction(get_extra, "s", "sockname");
        Py_DECREF(get_extra);
        if (sockname != NULL && sockname != Py_None) {
            struct sockaddr_storage ss;
            socklen_t len;
            if (py_addr_to_sockaddr(sockname, &ss, &len) == 0) {
                memcpy(&ep->local_addr_store, &ss, sizeof(ss));
                ep->local_addrlen_store = len;
            } else {
                PyErr_Clear();
            }
        }
        Py_XDECREF(sockname);
    } else {
        PyErr_Clear();
    }
    if (ep->registry != NULL && PySet_Add(ep->registry, op) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
endpoint_error_received(PyObject *op, PyObject *exc)
{
    (void)op;
    (void)exc;
    Py_RETURN_NONE;  /* transient ICMP errors: ignore */
}

/* Close every live connection and free them immediately. */
static void
close_all_conns(WreathH3Endpoint *ep)
{
    if (ep->conns == NULL) {
        return;
    }
    for (;;) {
        PyObject *values = PyDict_Values(ep->conns);
        if (values == NULL || PyList_GET_SIZE(values) == 0) {
            Py_XDECREF(values);
            break;
        }
        /* close_conn removes the connection's CID keys as it goes */
        WreathH3Conn *c = (WreathH3Conn *)PyCapsule_GetPointer(
            PyList_GET_ITEM(values, 0), "neoh3conn");
        Py_DECREF(values);
        if (c == NULL) {
            PyErr_Clear();
            break;
        }
        close_conn(ep, c);
    }
    reap_conns(ep);
}

static PyObject *
endpoint_connection_lost(PyObject *op, PyObject *Py_UNUSED(exc))
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    close_all_conns(ep);
    if (ep->registry != NULL && PySet_Discard(ep->registry, op) < 0) {
        PyErr_Clear();
    }
    Py_CLEAR(ep->transport);
    Py_CLEAR(ep->transport_sendto);
    Py_RETURN_NONE;
}

static PyObject *
endpoint_stop_accepting(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ((WreathH3Endpoint *)op)->accepting = 0;
    Py_RETURN_NONE;
}

static PyObject *
endpoint_shutdown(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    ep->accepting = 0;
    close_all_conns(ep);
    if (ep->transport != NULL) {
        PyObject *r = PyObject_CallMethod(ep->transport, "close", NULL);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

/* How many requests are still in flight across every connection here.

   One endpoint serves every connection on the UDP socket, so unlike a TCP
   protocol its existence says nothing about whether there is work left to
   drain; a graceful shutdown has to ask. It counts open *streams* rather than
   connections on purpose: a QUIC connection outlives the client that made it
   (nothing is reaped until it times out), so an idle connection would keep a
   drain waiting for a response that will never come. An open stream is a
   request that has not been answered yet, which is exactly what draining is
   for.

   Connections are keyed by every routing CID, so one may be counted more than
   once. The drain only asks whether this is zero, and a connection with no
   open stream contributes zero however many CIDs point at it. */
static PyObject *
endpoint_get_active_requests(PyObject *op, void *closure)
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    Py_ssize_t pos = 0;
    Py_ssize_t total = 0;
    PyObject *key, *cap;
    (void)closure;

    if (ep->conns == NULL) {
        return PyLong_FromLong(0);
    }
    while (PyDict_Next(ep->conns, &pos, &key, &cap)) {
        WreathH3Conn *c = (WreathH3Conn *)PyCapsule_GetPointer(cap, "neoh3conn");
        if (c == NULL) {
            PyErr_Clear();
            continue;
        }
        if (c->streams != NULL) {
            total += PyDict_Size(c->streams);
        }
    }
    return PyLong_FromSsize_t(total);
}

static PyGetSetDef endpoint_getset[] = {
    {"active_requests", endpoint_get_active_requests, NULL,
     PyDoc_STR("Requests still in flight across this endpoint's connections."), NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef endpoint_methods[] = {
    {"connection_made", endpoint_connection_made, METH_O, NULL},
    {"datagram_received", endpoint_datagram_received, METH_VARARGS, NULL},
    {"error_received", endpoint_error_received, METH_O, NULL},
    {"connection_lost", endpoint_connection_lost, METH_O, NULL},
    {"stop_accepting", endpoint_stop_accepting, METH_NOARGS, NULL},
    {"shutdown", endpoint_shutdown, METH_NOARGS, NULL},
    {"_on_timer", endpoint_on_timer, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static int
read_ssize(PyObject *config, const char *name, Py_ssize_t *out)
{
    PyObject *v = PyObject_GetAttrString(config, name);
    if (v == NULL) {
        return -1;
    }
    Py_ssize_t val = PyLong_AsSsize_t(v);
    Py_DECREF(v);
    if (val == -1 && PyErr_Occurred()) {
        return -1;
    }
    *out = val;
    return 0;
}

static int
endpoint_init(PyObject *op, PyObject *args, PyObject *Py_UNUSED(kwargs))
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    PyObject *app, *config, *loop, *registry;
    PyObject *recorder = NULL;
    const char *certfile, *keyfile, *password;
    if (!PyArg_ParseTuple(args, "OOOOssz|O", &app, &config, &loop, &registry,
                          &certfile, &keyfile, &password, &recorder)) {
        return -1;
    }
    ep->nfr_worker = wreath_h3_flight_capi != NULL
                         ? wreath_h3_worker_from(recorder)
                         : NULL;
    ep->app = Py_NewRef(app);
    ep->config = Py_NewRef(config);
    ep->loop = Py_NewRef(loop);
    ep->registry = Py_NewRef(registry);
    ep->conns = PyDict_New();
    if (ep->conns == NULL) {
        return -1;
    }
    ep->accepting = 1;
    ep->active_requests = 0;
    ep->reap = NULL;
    ep->reap_len = 0;
    ep->reap_cap = 0;
    ep->local_addrlen_store = 0;
    if (read_ssize(config, "max_concurrent_streams", &ep->max_concurrent_streams) < 0 ||
        read_ssize(config, "max_body_bytes", &ep->max_body_bytes) < 0 ||
        read_ssize(config, "max_header_list_bytes", &ep->max_header_list_bytes) < 0 ||
        read_ssize(config, "initial_stream_window", &ep->initial_stream_window) < 0 ||
        read_ssize(config, "initial_connection_window", &ep->initial_connection_window) < 0 ||
        read_ssize(config, "qpack_table_bytes", &ep->qpack_table_bytes) < 0 ||
        read_ssize(config, "qpack_blocked_streams", &ep->qpack_blocked_streams) < 0) {
        return -1;
    }

    /* Build the QUIC-capable SSL_CTX (OpenSSL 3.5+). */
    ep->ssl_ctx = SSL_CTX_new(TLS_server_method());
    if (ep->ssl_ctx == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "SSL_CTX_new failed");
        return -1;
    }
    SSL_CTX_set_min_proto_version(ep->ssl_ctx, TLS1_3_VERSION);
    SSL_CTX_set_max_proto_version(ep->ssl_ctx, TLS1_3_VERSION);
    if (SSL_CTX_use_certificate_chain_file(ep->ssl_ctx, certfile) != 1 ||
        SSL_CTX_use_PrivateKey_file(ep->ssl_ctx, keyfile, SSL_FILETYPE_PEM) != 1) {
        PyErr_SetString(PyExc_RuntimeError, "failed to load TLS certificate/key");
        return -1;
    }
    SSL_CTX_set_alpn_select_cb(ep->ssl_ctx, wreath_h3_alpn_select_cb, NULL);
    return 0;
}

static int
endpoint_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    Py_VISIT(ep->app);
    Py_VISIT(ep->config);
    Py_VISIT(ep->loop);
    Py_VISIT(ep->registry);
    Py_VISIT(ep->transport);
    Py_VISIT(ep->transport_sendto);
    Py_VISIT(ep->timer_handle);
    Py_VISIT(ep->conns);
    return 0;
}

static int
endpoint_clear(PyObject *op)
{
    WreathH3Endpoint *ep = (WreathH3Endpoint *)op;
    close_all_conns(ep);
    if (ep->reap != NULL) {
        PyMem_Free(ep->reap);
        ep->reap = NULL;
        ep->reap_cap = 0;
    }
    Py_CLEAR(ep->app);
    Py_CLEAR(ep->config);
    Py_CLEAR(ep->loop);
    Py_CLEAR(ep->registry);
    Py_CLEAR(ep->transport);
    Py_CLEAR(ep->transport_sendto);
    Py_CLEAR(ep->timer_handle);
    Py_CLEAR(ep->conns);
    if (ep->ssl_ctx != NULL) {
        SSL_CTX_free(ep->ssl_ctx);
        ep->ssl_ctx = NULL;
    }
    return 0;
}

static void
endpoint_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    endpoint_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
endpoint_new(PyTypeObject *type, PyObject *Py_UNUSED(a), PyObject *Py_UNUSED(k))
{
    return type->tp_alloc(type, 0);
}

static PyType_Slot endpoint_slots[] = {
    {Py_tp_new, endpoint_new},
    {Py_tp_init, endpoint_init},
    {Py_tp_dealloc, endpoint_dealloc},
    {Py_tp_traverse, endpoint_traverse},
    {Py_tp_clear, endpoint_clear},
    {Py_tp_methods, endpoint_methods},
    {Py_tp_getset, endpoint_getset},
    {0, NULL},
};

static PyType_Spec endpoint_spec = {
    .name = "wreath._native._http3.DatagramEndpoint",
    .basicsize = sizeof(WreathH3Endpoint),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    .slots = endpoint_slots,
};

/* stream type spec lives in http3_asgi.c */
extern PyType_Spec wreath_h3_stream_spec;

int
wreath_h3_ready(PyObject *module)
{
    if (ngtcp2_crypto_ossl_init() != 0) {
        PyErr_SetString(PyExc_RuntimeError, "ngtcp2_crypto_ossl_init failed");
        return -1;
    }
    if (wreath_h3_init_message_keys() < 0) {
        return -1;
    }
    /* Resolve the optional Flight Recorder vtable, exactly as _server does. A
     * missing/incompatible extension just leaves the hooks disabled. */
    {
        PyObject *flight_module = PyImport_ImportModule("wreath._native._flight");
        if (flight_module == NULL) {
            PyErr_Clear();
        } else {
            Py_DECREF(flight_module);
            wreath_h3_flight_capi =
                (const WreathFlightCAPI *)PyCapsule_Import(WREATH_FLIGHT_CAPI_NAME, 0);
            if (wreath_h3_flight_capi == NULL) {
                PyErr_Clear();
            } else if (wreath_h3_flight_capi->version != WREATH_FLIGHT_CAPI_VERSION) {
                wreath_h3_flight_capi = NULL;
            }
        }
    }
    PyObject *stream_type = PyType_FromSpec(&wreath_h3_stream_spec);
    if (stream_type == NULL) {
        return -1;
    }
    WreathH3StreamType = (PyTypeObject *)stream_type;
    PyObject *endpoint_type = PyType_FromSpec(&endpoint_spec);
    if (endpoint_type == NULL) {
        return -1;
    }
    WreathH3EndpointType = (PyTypeObject *)endpoint_type;
    if (PyModule_AddObjectRef(module, "DatagramEndpoint", endpoint_type) < 0) {
        Py_DECREF(endpoint_type);
        return -1;
    }
    Py_DECREF(endpoint_type);
    return 0;
}
