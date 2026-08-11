/* Native TLS for the metal tier. See reactor_tls.c for why this exists.
 *
 * The split is deliberate: this file owns the `SSL_CTX` (one per listener,
 * built at configuration time from certificate and key paths), while the
 * per-connection `SSL` lives on `SocketTransport`, because TLS is a layer
 * inside that transport rather than a second transport type.
 */
#ifndef WREATH_REACTOR_TLS_H
#define WREATH_REACTOR_TLS_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <openssl/ssl.h>

/* Is this the native `TLSContext`? Used to tell a metal TLS listener from a
 * Python `ssl.SSLContext`, which still takes the asyncio fallback. */
int wreath_tls_context_check(PyObject *context);

/* An `SSL` bound to `fd` and ready to handshake: accept state for a server
 * context, connect state for a client one, with SNI and the certificate name
 * check set from `server_hostname` (NULL on a server). Returns NULL with a
 * Python exception set. */
SSL *wreath_tls_new(PyObject *context, int fd, const char *server_hostname);

int wreath_tls_register(PyObject *module);

#endif
