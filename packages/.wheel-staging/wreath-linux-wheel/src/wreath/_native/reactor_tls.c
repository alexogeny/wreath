/* Native TLS for the metal tier: OpenSSL in C, no Python in the data path.
 *
 * Why this exists, in one measurement. `EventLoop._start_serving` takes the
 * native io_uring path only when there is no TLS context; a TLS listener fell
 * all the way back to stock asyncio, which meant asyncio's selector accept
 * loop, asyncio's `_SelectorSocketTransport`, and `asyncio.sslproto.SSLProtocol`
 * -- a Python object in the data path for every read and every write. On one
 * machine, one physical core each, handshakes fully amortised:
 *
 *     wreath, plaintext    74,300 req/s      nginx, plaintext   71,400
 *     wreath, TLS          21,300 req/s      nginx, TLS         47,400
 *
 * Plaintext, wreath was 4% ahead. With TLS it was 2.23x behind, and all of that
 * was the fallback: a TLS connection did not merely lose its crypto to Python,
 * it left the metal tier entirely.
 *
 * This module owns only the `SSL_CTX` -- the per-connection `SSL` lives on the
 * transport, because TLS here is a layer *inside* `SocketTransport` rather than
 * a second transport type. That is deliberate: the socket transport already
 * funnels every read through one function and every write through a short
 * egress path, so intercepting those two points reuses its flow control,
 * buffered-protocol dispatch, cork/send-queue egress and lifecycle instead of
 * duplicating two thousand tuned lines that would then drift.
 *
 * Certificate and key arrive as *paths*, not as a Python `ssl.SSLContext`.
 * There is no supported way to borrow an `SSL_CTX *` out of one, and reaching
 * into the object's layout would bind this file to a CPython patch release.
 * `http3_connection.c` already builds its context from paths for the same
 * reason, so this is the tree's existing answer rather than a new one.
 */

#include "reactor_tls.h"

#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include <string.h>


typedef struct {
    PyObject_HEAD
    SSL_CTX *ctx;
    /* ALPN preference list in wire format: each entry one length byte followed
     * by that many name bytes. On a server it is the list to choose *from*
     * when the client offers; on a client it is the list to offer. */
    unsigned char *alpn;
    unsigned int alpn_len;
    int is_client;
    int verify;                 /* client only: check the peer's chain and name */
} MetalTLSContext;

static PyTypeObject MetalTLSContextType;
static PyTypeObject MetalTLSClientContextType;


/* Raise the top of OpenSSL's error queue, and drain the rest.
 *
 * Draining matters: a queue left populated attributes this failure to whatever
 * unrelated call reads it next, which is how a certificate problem surfaces as
 * a handshake error three connections later. */
static void
tls_raise(const char *context)
{
    unsigned long code = ERR_get_error();
    char buffer[256];
    if (code == 0) {
        PyErr_Format(PyExc_OSError, "%s failed", context);
        return;
    }
    ERR_error_string_n(code, buffer, sizeof(buffer));
    PyErr_Format(PyExc_OSError, "%s failed: %s", context, buffer);
    while (ERR_get_error() != 0) {
        /* discard */
    }
}


static int
alpn_select(SSL *Py_UNUSED(ssl), const unsigned char **out, unsigned char *outlen,
            const unsigned char *in, unsigned int inlen, void *arg)
{
    MetalTLSContext *self = (MetalTLSContext *)arg;
    if (self->alpn == NULL || self->alpn_len == 0) {
        return SSL_TLSEXT_ERR_NOACK;
    }
    /* Server preference, not client preference: the order in `alpn` wins.
     * `SSL_select_next_proto` takes the server list second and documents that
     * it prefers the *first* list, so the arguments are the way round they
     * look wrong. */
    if (SSL_select_next_proto((unsigned char **)out, outlen,
                              self->alpn, self->alpn_len,
                              in, inlen) != OPENSSL_NPN_NEGOTIATED) {
        return SSL_TLSEXT_ERR_ALERT_FATAL;
    }
    return SSL_TLSEXT_ERR_OK;
}


/* Encode ("http/1.1", "h2") into the length-prefixed wire form ALPN uses. */
static int
alpn_encode(MetalTLSContext *self, PyObject *names)
{
    if (names == Py_None) {
        return 0;
    }
    PyObject *fast = PySequence_Fast(names, "alpn must be a sequence of str");
    if (fast == NULL) {
        return -1;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    Py_ssize_t total = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
        Py_ssize_t size = 0;
        if (!PyUnicode_Check(item) || PyUnicode_AsUTF8AndSize(item, &size) == NULL) {
            Py_DECREF(fast);
            PyErr_SetString(PyExc_TypeError, "alpn entries must be str");
            return -1;
        }
        if (size < 1 || size > 255) {
            Py_DECREF(fast);
            PyErr_SetString(PyExc_ValueError,
                            "an alpn protocol name must be 1-255 bytes");
            return -1;
        }
        total += size + 1;
    }
    if (total == 0) {
        Py_DECREF(fast);
        return 0;
    }
    unsigned char *wire = PyMem_Malloc((size_t)total);
    if (wire == NULL) {
        Py_DECREF(fast);
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t at = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        Py_ssize_t size = 0;
        const char *text = PyUnicode_AsUTF8AndSize(
            PySequence_Fast_GET_ITEM(fast, i), &size);
        wire[at++] = (unsigned char)size;
        memcpy(wire + at, text, (size_t)size);
        at += size;
    }
    Py_DECREF(fast);
    self->alpn = wire;
    self->alpn_len = (unsigned int)total;
    return 0;
}


static PyObject *
tls_context_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"certfile", "keyfile", "password", "alpn", NULL};
    const char *certfile = NULL, *keyfile = NULL, *password = NULL;
    PyObject *alpn = Py_None;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "ss|zO:TLSContext", kwlist,
                                     &certfile, &keyfile, &password, &alpn)) {
        return NULL;
    }

    MetalTLSContext *self = (MetalTLSContext *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->is_client = 0;
    self->ctx = SSL_CTX_new(TLS_server_method());
    if (self->ctx == NULL) {
        Py_DECREF(self);
        tls_raise("SSL_CTX_new");
        return NULL;
    }
    /* TLS 1.2 floor. Everything below it is either broken or on its way there,
     * and a proxy that will negotiate one is a proxy that can be made to. */
    SSL_CTX_set_min_proto_version(self->ctx, TLS1_2_VERSION);
    /* PARTIAL_WRITE lets `SSL_write` report a short count instead of insisting
     * on all-or-nothing, which is what the transport's egress loop expects.
     * MOVING_WRITE_BUFFER allows the retry after WANT_WRITE to come from a
     * different address -- the write buffer is a bytearray that may have been
     * reallocated in between. Without it OpenSSL refuses the retry. */
    SSL_CTX_set_mode(self->ctx,
                     SSL_MODE_ENABLE_PARTIAL_WRITE
                     | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
    if (password != NULL) {
        SSL_CTX_set_default_passwd_cb_userdata(self->ctx, (void *)password);
    }
    /* Loaded here, at configuration time, rather than deferred to the first
     * handshake. A listener that binds and then fails every connection is the
     * shape this tree refuses everywhere else, and OpenSSL is perfectly happy
     * to defer the error if asked. */
    if (SSL_CTX_use_certificate_chain_file(self->ctx, certfile) != 1) {
        Py_DECREF(self);
        tls_raise("loading the certificate chain");
        return NULL;
    }
    if (SSL_CTX_use_PrivateKey_file(self->ctx, keyfile, SSL_FILETYPE_PEM) != 1) {
        Py_DECREF(self);
        tls_raise("loading the private key");
        return NULL;
    }
    if (SSL_CTX_check_private_key(self->ctx) != 1) {
        Py_DECREF(self);
        tls_raise("the private key does not match the certificate");
        return NULL;
    }
    if (alpn_encode(self, alpn) < 0) {
        Py_DECREF(self);
        return NULL;
    }
    if (self->alpn != NULL) {
        SSL_CTX_set_alpn_select_cb(self->ctx, alpn_select, self);
    }
    return (PyObject *)self;
}


/* The outbound half. Verification defaults on, and the default is the point:
 * a TLS client that skips the trust check is faster than one that does not and
 * looks identical until it matters. `SSL_set1_host` at connect time does the
 * name check inside OpenSSL rather than leaving it to a caller who might not. */
static PyObject *
tls_client_context_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"cafile", "capath", "verify", "alpn", NULL};
    const char *cafile = NULL, *capath = NULL;
    int verify = 1;
    PyObject *alpn = Py_None;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|zzpO:TLSClientContext",
                                     kwlist, &cafile, &capath, &verify, &alpn)) {
        return NULL;
    }

    MetalTLSContext *self = (MetalTLSContext *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->is_client = 1;
    self->verify = verify;
    self->ctx = SSL_CTX_new(TLS_client_method());
    if (self->ctx == NULL) {
        Py_DECREF(self);
        tls_raise("SSL_CTX_new");
        return NULL;
    }
    SSL_CTX_set_min_proto_version(self->ctx, TLS1_2_VERSION);
    SSL_CTX_set_mode(self->ctx,
                     SSL_MODE_ENABLE_PARTIAL_WRITE
                     | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);
    if (verify) {
        SSL_CTX_set_verify(self->ctx, SSL_VERIFY_PEER, NULL);
        if (cafile != NULL || capath != NULL) {
            if (SSL_CTX_load_verify_locations(self->ctx, cafile, capath) != 1) {
                Py_DECREF(self);
                tls_raise("loading the trust store");
                return NULL;
            }
        }
        else if (SSL_CTX_set_default_verify_paths(self->ctx) != 1) {
            Py_DECREF(self);
            tls_raise("loading the system trust store");
            return NULL;
        }
    }
    else {
        SSL_CTX_set_verify(self->ctx, SSL_VERIFY_NONE, NULL);
    }
    if (alpn_encode(self, alpn) < 0) {
        Py_DECREF(self);
        return NULL;
    }
    if (self->alpn != NULL
        && SSL_CTX_set_alpn_protos(self->ctx, self->alpn, self->alpn_len) != 0) {
        Py_DECREF(self);
        tls_raise("setting the ALPN list");
        return NULL;
    }
    return (PyObject *)self;
}


static void
tls_context_dealloc(PyObject *op)
{
    MetalTLSContext *self = (MetalTLSContext *)op;
    if (self->ctx != NULL) {
        SSL_CTX_free(self->ctx);
        self->ctx = NULL;
    }
    PyMem_Free(self->alpn);
    self->alpn = NULL;
    Py_TYPE(op)->tp_free(op);
}


static PyTypeObject MetalTLSContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.TLSContext",
    .tp_doc = "A server TLS context built in C from certificate and key paths.",
    .tp_basicsize = sizeof(MetalTLSContext),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = tls_context_new,
    .tp_dealloc = tls_context_dealloc,
};


static PyTypeObject MetalTLSClientContextType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.TLSClientContext",
    .tp_doc = "An outbound TLS context built in C, verifying by default.",
    .tp_basicsize = sizeof(MetalTLSContext),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = tls_client_context_new,
    .tp_dealloc = tls_context_dealloc,
};


int
wreath_tls_context_check(PyObject *op)
{
    return op != NULL && (PyObject_TypeCheck(op, &MetalTLSContextType)
                          || PyObject_TypeCheck(op, &MetalTLSClientContextType));
}


SSL *
wreath_tls_new(PyObject *op, int fd, const char *server_hostname)
{
    if (!wreath_tls_context_check(op)) {
        PyErr_SetString(PyExc_TypeError, "expected a native TLS context");
        return NULL;
    }
    MetalTLSContext *self = (MetalTLSContext *)op;
    SSL *ssl = SSL_new(self->ctx);
    if (ssl == NULL) {
        tls_raise("SSL_new");
        return NULL;
    }
    if (SSL_set_fd(ssl, fd) != 1) {
        SSL_free(ssl);
        tls_raise("SSL_set_fd");
        return NULL;
    }
    if (!self->is_client) {
        SSL_set_accept_state(ssl);
        return ssl;
    }
    if (server_hostname != NULL && *server_hostname != '\0') {
        /* An IP literal is checked against the certificate's iPAddress SAN, not
         * its DNS names, and is never sent as SNI -- RFC 6066 forbids it, and
         * `SSL_set1_host` would look for a DNS entry that cannot be there. This
         * matters more here than it looks: an upstream pool is usually written
         * as addresses, so treating one as a host name refuses every
         * correctly-issued certificate it could have been given. */
        X509_VERIFY_PARAM *param = SSL_get0_param(ssl);
        int is_ip = param != NULL
            && X509_VERIFY_PARAM_set1_ip_asc(param, server_hostname) == 1;
        if (!is_ip) {
            /* SNI, so the peer can pick a certificate, and -- separately -- the
             * name the certificate is checked against. Setting only the first
             * is the classic half-done client: it reaches the right virtual
             * host and then accepts a certificate for any name at all. */
            if (SSL_set_tlsext_host_name(ssl, server_hostname) != 1) {
                SSL_free(ssl);
                tls_raise("setting the SNI host name");
                return NULL;
            }
            if (self->verify) {
                SSL_set_hostflags(ssl, X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS);
                if (SSL_set1_host(ssl, server_hostname) != 1) {
                    SSL_free(ssl);
                    tls_raise("setting the certificate host name");
                    return NULL;
                }
            }
        }
        else if (!self->verify) {
            /* Recorded above regardless, so clear it when trust is off; a
             * stale IP constraint would otherwise reject on a path the caller
             * asked not to check at all. */
            X509_VERIFY_PARAM_set1_ip_asc(param, NULL);
        }
    }
    SSL_set_connect_state(ssl);
    return ssl;
}


int
wreath_tls_register(PyObject *module)
{
    if (PyType_Ready(&MetalTLSContextType) < 0
        || PyType_Ready(&MetalTLSClientContextType) < 0) {
        return -1;
    }
    if (PyModule_AddObjectRef(module, "TLSContext",
                              (PyObject *)&MetalTLSContextType) < 0) {
        return -1;
    }
    return PyModule_AddObjectRef(module, "TLSClientContext",
                                 (PyObject *)&MetalTLSClientContextType);
}
