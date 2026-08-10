/* Shared declarations for wreath._native._edge.
 *
 * `wreath.edge` has no Python path by design (AGENTS.md): a fallback here would
 * degrade silently. The module has two halves that share the RFC 9110 7.6.1
 * rules and nothing else -- `edge_headers.c`, which transforms a header list for
 * the Python `ReverseProxy`, and `edge_serve.c`, which is the proxy that never
 * builds one.
 */
#ifndef WREATH_EDGE_H
#define WREATH_EDGE_H

#include "wreathcore.h"

/* Fields dropped from every forwarded *request*: RFC 9110's hop-by-hop list,
 * the fields this proxy owns and will not let a client dictate, and the two the
 * outbound framing recomputes (`host`, `content-length`). Names must already be
 * lowercased. */
int wreath_edge_is_request_drop(const char *name, Py_ssize_t len);

/* The same, for a *response*: hop-by-hop and proxy-owned, but `content-length`
 * survives -- the body being relayed is the body the upstream declared, and
 * recomputing a length this proxy did not change is how the two ends come to
 * disagree about where the message ends. */
int wreath_edge_is_response_drop(const char *name, Py_ssize_t len);

/* Does `Connection: <value>` name this field for this message? `close` and
 * `keep-alive` are connection options rather than field names and never match.
 */
int wreath_edge_connection_names(const char *conn, Py_ssize_t conn_len,
                                 const char *name, Py_ssize_t name_len);

PyObject *wreath_edge_request_headers(PyObject *module, PyObject *const *args,
                                      Py_ssize_t nargs, PyObject *kwnames);

/* Register `UpstreamTable`, `EdgeProtocol` and `UpstreamConnection`. */
int wreath_edge_serve_ready(PyObject *module);

#endif
