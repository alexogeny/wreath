/* The outbound request-header transform for wreath.edge, in C.
 *
 * A reverse proxy is a recipient on one connection and a sender on another, and
 * most of this function is deciding what belongs to the connection it is *not*
 * forwarding on. The rules are RFC 9110 7.6.1 (hop-by-hop, both the fixed list
 * and whatever `Connection` names for this message) plus the fields this proxy
 * writes itself and will not let a client dictate.
 *
 * Why C, when the Python was already a single fused pass: the header build
 * measured at roughly a third of the proxy's per-request cost, and the proxy's
 * own work at 44.4 of 117 CPU-microseconds per forwarded request against
 * single-threaded haproxy's 25.7 for the whole job. `wreath.edge` is native-only
 * by design -- see AGENTS.md -- so this is the implementation, not an
 * accelerator with a Python twin behind it.
 *
 * One pass over the inbound list. Names are matched by length first and then a
 * single memcmp, so the common header pays one comparison against a jump table
 * rather than a set hash.
 */

#include "edge.h"

#include <string.h>


/* Fields dropped from every forwarded request.
 *
 * The union of RFC 9110's hop-by-hop list, the fields this proxy owns
 * (`forwarded`, the `x-forwarded-*` family, `via`), and the two the outbound
 * codec recomputes (`host`, `content-length`). Switching on length first turns
 * fifteen candidate comparisons into at most two.
 *
 * Inbound names are already lowercased by the HTTP/1 parser; this compares
 * verbatim, exactly as the Python it replaces did.
 */
int
wreath_edge_is_request_drop(const char *p, Py_ssize_t n)
{
    switch (n) {
    case 2:  return memcmp(p, "te", 2) == 0;
    case 3:  return memcmp(p, "via", 3) == 0;
    case 4:  return memcmp(p, "host", 4) == 0;
    case 7:  return memcmp(p, "trailer", 7) == 0 || memcmp(p, "upgrade", 7) == 0;
    case 9:  return memcmp(p, "forwarded", 9) == 0;
    case 10: return memcmp(p, "connection", 10) == 0
                 || memcmp(p, "keep-alive", 10) == 0;
    case 14: return memcmp(p, "content-length", 14) == 0;
    case 15: return memcmp(p, "x-forwarded-for", 15) == 0;
    case 16: return memcmp(p, "x-forwarded-host", 16) == 0;
    case 17: return memcmp(p, "transfer-encoding", 17) == 0
                 || memcmp(p, "x-forwarded-proto", 17) == 0;
    case 18: return memcmp(p, "proxy-authenticate", 18) == 0;
    case 19: return memcmp(p, "proxy-authorization", 19) == 0;
    default: return 0;
    }
}


/* The response-direction drop set: the same list without `host` and
 * `content-length`.
 *
 * A response has no `Host`, and its `Content-Length` describes the body being
 * relayed unchanged -- recomputing a framing this proxy did not author is how
 * the two ends come to disagree about where the message ends. This matches
 * `forwardable()` in `wreath/edge/headers.py`, which is what the Python path
 * applies to a response.
 */
int
wreath_edge_is_response_drop(const char *p, Py_ssize_t n)
{
    if (n == 4 || n == 14) {
        return 0;
    }
    return wreath_edge_is_request_drop(p, n);
}


static int
ascii_eq_lower(const char *p, Py_ssize_t n, const char *lit, Py_ssize_t lit_n)
{
    if (n != lit_n) return 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        char c = p[i];
        if (c >= 'A' && c <= 'Z') c = (char)(c + 32);
        if (c != lit[i]) return 0;
    }
    return 1;
}


/* Does `Connection: <value>` name this field for this message?
 *
 * `close` and `keep-alive` are connection *options*, not field names, and are
 * compared case-insensitively. The Python this replaces compared the unlowered
 * token against them while lowering it before use, so `Connection: CLOSE` put a
 * phantom `close` into the drop set -- harmless, because no field is named
 * `close`, but inconsistent with `_connection_named()` in the same module.
 * Resolved here the way that function already did it.
 */
int
wreath_edge_connection_names(const char *conn, Py_ssize_t conn_len,
                             const char *name, Py_ssize_t name_len)
{
    Py_ssize_t i = 0;
    while (i < conn_len) {
        Py_ssize_t start = i;
        while (i < conn_len && conn[i] != ',') i++;
        Py_ssize_t end = i;
        if (i < conn_len) i++;                      /* step over the comma */

        while (start < end && (unsigned char)conn[start] <= ' ') start++;
        while (end > start && (unsigned char)conn[end - 1] <= ' ') end--;
        Py_ssize_t token_len = end - start;
        if (token_len == 0) continue;

        const char *token = conn + start;
        if (ascii_eq_lower(token, token_len, "close", 5)) continue;
        if (ascii_eq_lower(token, token_len, "keep-alive", 10)) continue;
        if (ascii_eq_lower(token, token_len, name, name_len)) return 1;
    }
    return 0;
}


/* Append (name, value) to the outbound list. Steals nothing. */
static int
append_pair(PyObject *out, const char *name, Py_ssize_t name_len, PyObject *value)
{
    PyObject *key = PyBytes_FromStringAndSize(name, name_len);
    if (key == NULL) return -1;
    PyObject *pair = PyTuple_Pack(2, key, value);
    Py_DECREF(key);
    if (pair == NULL) return -1;
    int rc = PyList_Append(out, pair);
    Py_DECREF(pair);
    return rc;
}


/* Join a list of bytes with `sep`, through the public API.
 *
 * Everything here is built from sizes rather than `PyBytes_FromFormat("%s")`:
 * that stops at the first NUL, and this function's entire purpose is producing
 * a header a proxy will put on the wire. A value carrying an embedded NUL must
 * come out whole -- and wrong in a way a parser rejects -- rather than silently
 * truncated into something that reframes the message.
 */
static PyObject *
join_bytes(const char *sep, Py_ssize_t sep_len, PyObject *parts)
{
    PyObject *separator = PyBytes_FromStringAndSize(sep, sep_len);
    if (separator == NULL) return NULL;
    PyObject *joined = PyObject_CallMethod(separator, "join", "O", parts);
    Py_DECREF(separator);
    return joined;
}


static int
append_literal(PyObject *parts, const char *text, Py_ssize_t len)
{
    PyObject *chunk = PyBytes_FromStringAndSize(text, len);
    if (chunk == NULL) return -1;
    int rc = PyList_Append(parts, chunk);
    Py_DECREF(chunk);
    return rc;
}


/* Read one (name, value) pair, refusing anything that is not two bytes objects.
 *
 * Refusing rather than coercing is the safe answer for this function
 * specifically: its whole job is deciding what not to send, and a `str` name
 * would match no drop rule and be forwarded. A silent pass-through here is a
 * header leak.
 */
static int
unpack_pair(PyObject *pair, PyObject **name, PyObject **value)
{
    PyObject *fast = PySequence_Fast(pair, "each header must be a (name, value) pair");
    if (fast == NULL) return -1;
    if (PySequence_Fast_GET_SIZE(fast) != 2) {
        Py_DECREF(fast);
        PyErr_SetString(PyExc_ValueError,
                        "each header must be a (name, value) pair");
        return -1;
    }
    PyObject *n = PySequence_Fast_GET_ITEM(fast, 0);
    PyObject *v = PySequence_Fast_GET_ITEM(fast, 1);
    if (!PyBytes_Check(n) || !PyBytes_Check(v)) {
        Py_DECREF(fast);
        PyErr_SetString(PyExc_TypeError,
                        "header names and values must be bytes");
        return -1;
    }
    *name = Py_NewRef(n);
    *value = Py_NewRef(v);
    Py_DECREF(fast);
    return 0;
}


PyObject *
wreath_edge_request_headers(PyObject *Py_UNUSED(module), PyObject *const *args,
                            Py_ssize_t nargs, PyObject *kwnames)
{
    static const char *kwlist[] = {"client", "scheme", "via"};
    PyObject *inbound = NULL, *client = NULL, *scheme = NULL, *via = NULL;

    if (nargs != 1) {
        PyErr_SetString(PyExc_TypeError,
                        "request_headers() takes exactly one positional argument");
        return NULL;
    }
    inbound = args[0];

    Py_ssize_t nkw = kwnames == NULL ? 0 : PyTuple_GET_SIZE(kwnames);
    for (Py_ssize_t i = 0; i < nkw; i++) {
        PyObject *key = PyTuple_GET_ITEM(kwnames, i);
        PyObject *val = args[nargs + i];
        int matched = 0;
        for (size_t k = 0; k < sizeof(kwlist) / sizeof(kwlist[0]); k++) {
            if (PyUnicode_CompareWithASCIIString(key, kwlist[k]) == 0) {
                if (k == 0) client = val;
                else if (k == 1) scheme = val;
                else via = val;
                matched = 1;
                break;
            }
        }
        if (!matched) {
            PyErr_Format(PyExc_TypeError,
                         "request_headers() got an unexpected keyword argument %R",
                         key);
            return NULL;
        }
    }
    if (scheme == NULL || via == NULL || client == NULL) {
        PyErr_SetString(PyExc_TypeError,
                        "request_headers() requires client, scheme and via");
        return NULL;
    }
    if (!PyBytes_Check(scheme) || !PyBytes_Check(via)) {
        PyErr_SetString(PyExc_TypeError, "scheme and via must be bytes");
        return NULL;
    }

    PyObject *client_bytes = NULL;      /* the peer, latin-1 encoded, or NULL */
    if (client != Py_None) {
        if (!PyUnicode_Check(client)) {
            PyErr_SetString(PyExc_TypeError, "client must be str or None");
            return NULL;
        }
        client_bytes = PyUnicode_AsLatin1String(client);
        if (client_bytes == NULL) return NULL;
    }

    PyObject *fast = PySequence_Fast(inbound, "inbound headers must be a sequence");
    if (fast == NULL) {
        Py_XDECREF(client_bytes);
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);

    PyObject *out = PyList_New(0);
    PyObject *host = NULL;              /* the inbound Host value, or NULL */
    PyObject *connection = NULL;        /* the inbound Connection value, or NULL */
    PyObject *chain = NULL;             /* inbound Via values, in order */
    if (out == NULL) goto error;
    chain = PyList_New(0);
    if (chain == NULL) goto error;

    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *name = NULL, *value = NULL;
        if (unpack_pair(PySequence_Fast_GET_ITEM(fast, i), &name, &value) < 0) {
            goto error;
        }
        const char *np = PyBytes_AS_STRING(name);
        Py_ssize_t nn = PyBytes_GET_SIZE(name);

        if (nn == 4 && memcmp(np, "host", 4) == 0) {
            Py_XSETREF(host, value);
            Py_DECREF(name);
            continue;
        }
        if (nn == 10 && memcmp(np, "connection", 10) == 0) {
            Py_XSETREF(connection, value);
            Py_DECREF(name);
            continue;
        }
        if (nn == 3 && memcmp(np, "via", 3) == 0) {
            int rc = PyList_Append(chain, value);
            Py_DECREF(name);
            Py_DECREF(value);
            if (rc < 0) goto error;
            continue;
        }
        if (wreath_edge_is_request_drop(np, nn)) {
            Py_DECREF(name);
            Py_DECREF(value);
            continue;
        }
        PyObject *pair = PyTuple_Pack(2, name, value);
        Py_DECREF(name);
        Py_DECREF(value);
        if (pair == NULL) goto error;
        int rc = PyList_Append(out, pair);
        Py_DECREF(pair);
        if (rc < 0) goto error;
    }

    /* A field named by `Connection` is hop-by-hop for this message only, so it
     * cannot be filtered in the pass above -- the header may arrive after the
     * fields it names. Both oracles get this wrong: haproxy 3.4.3 and nginx
     * 1.30.4 forward such a field. See tests/test_edge_forwarding_contract.py. */
    if (connection != NULL) {
        /* Built fresh rather than compacted in place. The in-place version of
         * this loop overwrote a slot without releasing its occupant and then
         * dropped a reference the list still held -- a use-after-free that
         * surfaced as a duplicated header and a vanished one. It runs only when
         * the request carries `Connection` at all, so there is nothing to buy
         * by being clever here. */
        const char *cp = PyBytes_AS_STRING(connection);
        Py_ssize_t cn = PyBytes_GET_SIZE(connection);
        PyObject *kept = PyList_New(0);
        if (kept == NULL) goto error;
        Py_ssize_t out_len = PyList_GET_SIZE(out);
        for (Py_ssize_t i = 0; i < out_len; i++) {
            PyObject *pair = PyList_GET_ITEM(out, i);       /* borrowed */
            PyObject *name = PyTuple_GET_ITEM(pair, 0);     /* borrowed */
            if (wreath_edge_connection_names(cp, cn, PyBytes_AS_STRING(name),
                                             PyBytes_GET_SIZE(name))) {
                continue;
            }
            if (PyList_Append(kept, pair) < 0) {
                Py_DECREF(kept);
                goto error;
            }
        }
        Py_SETREF(out, kept);
    }

    /* The forwarding record, in both spellings. RFC 7239 `Forwarded` is the
     * standard; the `x-forwarded-*` family is what almost everything actually
     * reads -- including wreath's own ProxyHeadersMiddleware, which is what sits
     * behind this proxy, so emitting only the standard one would mean wreath
     * could not sit behind itself. */
    PyObject *parts = PyList_New(0);
    if (parts == NULL) goto error;
    if (client_bytes != NULL) {
        if (append_pair(out, "x-forwarded-for", 15, client_bytes) < 0) {
            Py_DECREF(parts);
            goto error;
        }
        if (append_literal(parts, "for=\"", 5) < 0
            || PyList_Append(parts, client_bytes) < 0
            || append_literal(parts, "\"; ", 3) < 0) {
            Py_DECREF(parts);
            goto error;
        }
    }
    if (append_literal(parts, "proto=", 6) < 0 || PyList_Append(parts, scheme) < 0) {
        Py_DECREF(parts);
        goto error;
    }
    if (append_pair(out, "x-forwarded-proto", 17, scheme) < 0) {
        Py_DECREF(parts);
        goto error;
    }
    if (host != NULL) {
        if (append_pair(out, "x-forwarded-host", 16, host) < 0
            || append_literal(parts, "; host=\"", 8) < 0
            || PyList_Append(parts, host) < 0
            || append_literal(parts, "\"", 1) < 0) {
            Py_DECREF(parts);
            goto error;
        }
    }
    PyObject *forwarded = join_bytes("", 0, parts);
    Py_DECREF(parts);
    if (forwarded == NULL) goto error;
    int rc = append_pair(out, "forwarded", 9, forwarded);
    Py_DECREF(forwarded);
    if (rc < 0) goto error;

    /* `Via` is appended where `x-forwarded-for` is replaced, and the asymmetry
     * is deliberate: `Via` is a loop-detection and topology record whose whole
     * value is the chain, and it is an authorization input nowhere. */
    PyObject *via_value = NULL;
    if (PyList_GET_SIZE(chain) > 0) {
        if (PyList_Append(chain, via) < 0) goto error;
        via_value = join_bytes(", ", 2, chain);
        if (via_value == NULL) goto error;
    }
    else {
        via_value = Py_NewRef(via);
    }
    rc = append_pair(out, "via", 3, via_value);
    Py_DECREF(via_value);
    if (rc < 0) goto error;

    Py_DECREF(fast);
    Py_XDECREF(client_bytes);
    Py_XDECREF(host);
    Py_XDECREF(connection);
    Py_DECREF(chain);
    return out;

error:
    Py_XDECREF(fast);
    Py_XDECREF(client_bytes);
    Py_XDECREF(out);
    Py_XDECREF(host);
    Py_XDECREF(connection);
    Py_XDECREF(chain);
    return NULL;
}
