/* HTTP/1.x request head parser in the picohttpparser style: find the header
 * terminator first, then scan the head with flat byte loops. */
#include "wreathcore.h"

static const uint8_t TOKEN_CHARS[256] = {
    /* RFC 9110 token characters: ALPHA / DIGIT / !#$%&'*+-.^_`|~ */
    ['!'] = 1, ['#'] = 1, ['$'] = 1, ['%'] = 1, ['&'] = 1, ['\''] = 1,
    ['*'] = 1, ['+'] = 1, ['-'] = 1, ['.'] = 1, ['^'] = 1, ['_'] = 1,
    ['`'] = 1, ['|'] = 1, ['~'] = 1,
    ['0'] = 1, ['1'] = 1, ['2'] = 1, ['3'] = 1, ['4'] = 1,
    ['5'] = 1, ['6'] = 1, ['7'] = 1, ['8'] = 1, ['9'] = 1,
    ['A'] = 1, ['B'] = 1, ['C'] = 1, ['D'] = 1, ['E'] = 1, ['F'] = 1,
    ['G'] = 1, ['H'] = 1, ['I'] = 1, ['J'] = 1, ['K'] = 1, ['L'] = 1,
    ['M'] = 1, ['N'] = 1, ['O'] = 1, ['P'] = 1, ['Q'] = 1, ['R'] = 1,
    ['S'] = 1, ['T'] = 1, ['U'] = 1, ['V'] = 1, ['W'] = 1, ['X'] = 1,
    ['Y'] = 1, ['Z'] = 1,
    ['a'] = 1, ['b'] = 1, ['c'] = 1, ['d'] = 1, ['e'] = 1, ['f'] = 1,
    ['g'] = 1, ['h'] = 1, ['i'] = 1, ['j'] = 1, ['k'] = 1, ['l'] = 1,
    ['m'] = 1, ['n'] = 1, ['o'] = 1, ['p'] = 1, ['q'] = 1, ['r'] = 1,
    ['s'] = 1, ['t'] = 1, ['u'] = 1, ['v'] = 1, ['w'] = 1, ['x'] = 1,
    ['y'] = 1, ['z'] = 1,
};

static PyObject *
malformed(const char *reason)
{
    PyErr_SetString(PyExc_ValueError, reason);
    return NULL;
}

/* Common request methods and header names are returned as cached objects so
 * steady-state parsing does not allocate a str per method or a bytes per
 * well-known header name.  Lazily initialized; entries stay for the life of
 * the interpreter. */
/* A string literal paired with its compile-time length, so the per-request
 * match loops below never call strlen on a constant. */
#define WREATH_LSTR(s) {s, (Py_ssize_t)sizeof(s) - 1}

static PyObject *cached_methods[8];
static const struct { const char *text; Py_ssize_t len; } METHOD_NAMES[8] = {
    WREATH_LSTR("GET"), WREATH_LSTR("POST"), WREATH_LSTR("PUT"),
    WREATH_LSTR("DELETE"), WREATH_LSTR("PATCH"), WREATH_LSTR("HEAD"),
    WREATH_LSTR("OPTIONS"), WREATH_LSTR("CONNECT"),
};

static PyObject *
method_object(const uint8_t *data, Py_ssize_t len)
{
    for (int i = 0; i < 8; i++) {
        const char *candidate = METHOD_NAMES[i].text;
        if (METHOD_NAMES[i].len == len && memcmp(candidate, data, len) == 0) {
            if (cached_methods[i] == NULL) {
                cached_methods[i] = PyUnicode_InternFromString(candidate);
                if (cached_methods[i] == NULL) {
                    return NULL;
                }
            }
            return Py_NewRef(cached_methods[i]);
        }
    }
    return PyUnicode_DecodeASCII((const char *)data, len, "strict");
}

static PyObject *cached_header_names[16];
static const struct { const char *text; Py_ssize_t len; } HEADER_NAMES[16] = {
    WREATH_LSTR("host"), WREATH_LSTR("connection"), WREATH_LSTR("accept"),
    WREATH_LSTR("accept-encoding"), WREATH_LSTR("accept-language"),
    WREATH_LSTR("user-agent"), WREATH_LSTR("content-length"),
    WREATH_LSTR("content-type"), WREATH_LSTR("authorization"),
    WREATH_LSTR("cookie"), WREATH_LSTR("cache-control"), WREATH_LSTR("upgrade"),
    WREATH_LSTR("origin"), WREATH_LSTR("referer"), WREATH_LSTR("x-forwarded-for"),
    WREATH_LSTR("x-request-id"),
};

static PyObject *
header_name_object(const uint8_t *lowered, Py_ssize_t len)
{
    for (int i = 0; i < 16; i++) {
        const char *candidate = HEADER_NAMES[i].text;
        if (HEADER_NAMES[i].len == len && memcmp(candidate, lowered, len) == 0) {
            if (cached_header_names[i] == NULL) {
                cached_header_names[i] = PyBytes_FromString(candidate);
                if (cached_header_names[i] == NULL) {
                    return NULL;
                }
            }
            return Py_NewRef(cached_header_names[i]);
        }
    }
    return PyBytes_FromStringAndSize((const char *)lowered, len);
}

/* Header construction is a replaceable parser sink. Keeping allocation outside
 * the request-line/state machine lets the server substitute a lazy raw-header
 * sink without duplicating syntax validation. */
static PyObject *
new_request_header_sink(void)
{
    return PyList_New(0);
}

int
wreath_http_parse_request_parts(
    const uint8_t *data, Py_ssize_t len, Py_ssize_t head_end_off,
    PyObject **method_out, PyObject **target_out, int *minor_out,
    PyObject **headers_out, Py_ssize_t *consumed_out, Py_ssize_t max_headers
)
{
    /* The caller has already located the CRLFCRLF that ends the head (the
     * driver needs it to know the head is complete), so it is passed in as an
     * offset rather than scanned for a second time here. A negative offset
     * means "head not yet complete". */
    const uint8_t *head_end;
    const uint8_t *p;
    const uint8_t *end;
    const uint8_t *method_start;
    const uint8_t *target_start;
    Py_ssize_t method_len;
    Py_ssize_t target_len;
    PyObject *headers = NULL;
    PyObject *method = NULL;
    PyObject *target = NULL;
    int minor_version;
    Py_ssize_t header_count = 0;

    *method_out = NULL;
    *target_out = NULL;
    *headers_out = NULL;
    if (head_end_off < 0 || head_end_off + 4 > len) return 0;
    head_end = data + head_end_off;
    *consumed_out = head_end_off + 4;
    p = data;
    end = head_end + 2;

    method_start = p;
    while (p < end && TOKEN_CHARS[*p]) p++;
    method_len = p - method_start;
    if (method_len == 0 || p >= end || *p != ' ') {
        malformed("malformed request line");
        goto error;
    }
    p++;
    target_start = p;
    while (p < end && *p > ' ' && *p != 0x7f) p++;
    target_len = p - target_start;
    if (target_len == 0 || p >= end || *p != ' ') {
        malformed("malformed request target");
        goto error;
    }
    p++;
    if (end - p < 10 || memcmp(p, "HTTP/1.", 7) != 0 ||
        (p[7] != '0' && p[7] != '1') || p[8] != '\r' || p[9] != '\n') {
        malformed("malformed HTTP version");
        goto error;
    }
    minor_version = p[7] - '0';
    p += 10;
    headers = new_request_header_sink();
    if (headers == NULL) goto error;
    while (p < end) {
        if (++header_count > max_headers) {
            Py_DECREF(headers);
            return -2;
        }
        const uint8_t *name_start;
        const uint8_t *value_start;
        const uint8_t *value_end;
        Py_ssize_t name_len;
        PyObject *name;
        PyObject *value;
        PyObject *pair;
        if (*p == ' ' || *p == '\t') {
            malformed("obsolete header line folding is not supported");
            goto error;
        }
        name_start = p;
        while (p < end && TOKEN_CHARS[*p]) p++;
        name_len = p - name_start;
        if (name_len == 0 || p >= end || *p != ':') {
            malformed("malformed header name");
            goto error;
        }
        p++;
        while (p < end && (*p == ' ' || *p == '\t')) p++;
        value_start = p;
        while (p < end && *p != '\r') {
            uint8_t c = *p;
            if (c != '\t' && (c < 0x20 || c == 0x7f)) {
                malformed("invalid header value byte");
                goto error;
            }
            p++;
        }
        if (end - p < 2 || p[1] != '\n') {
            malformed("malformed header line ending");
            goto error;
        }
        value_end = p;
        while (value_end > value_start &&
               (value_end[-1] == ' ' || value_end[-1] == '\t')) value_end--;
        p += 2;
        if (name_len <= 64) {
            uint8_t lowered[64];
            for (Py_ssize_t i = 0; i < name_len; i++) {
                uint8_t c = name_start[i];
                lowered[i] = c >= 'A' && c <= 'Z'
                    ? (uint8_t)(c + ('a' - 'A')) : c;
            }
            name = header_name_object(lowered, name_len);
        } else {
            /* Allocate uninitialised and fill. Copying the source in and
             * lowercasing in place is only safe while `name_len > 64` holds:
             * `PyBytes_FromStringAndSize` with a non-NULL source returns the
             * interpreter's immortal singleton for a length-1 string, and
             * writing through it corrupts `b"A"` process-wide. That is a live
             * defect elsewhere in this tree, reached through multipart. Passing
             * NULL always allocates, so this branch stays correct on its own
             * terms rather than on the guard above it. */
            name = PyBytes_FromStringAndSize(NULL, name_len);
            if (name != NULL) {
                uint8_t *name_buf = (uint8_t *)PyBytes_AS_STRING(name);
                for (Py_ssize_t i = 0; i < name_len; i++) {
                    uint8_t c = name_start[i];
                    name_buf[i] = (c >= 'A' && c <= 'Z')
                        ? (uint8_t)(c + ('a' - 'A')) : c;
                }
            }
        }
        if (name == NULL) goto error;
        value = PyBytes_FromStringAndSize(
            (const char *)value_start, value_end - value_start
        );
        pair = value != NULL ? PyTuple_Pack(2, name, value) : NULL;
        Py_DECREF(name);
        Py_XDECREF(value);
        if (pair == NULL || PyList_Append(headers, pair) < 0) {
            Py_XDECREF(pair);
            goto error;
        }
        Py_DECREF(pair);
    }
    method = method_object(method_start, method_len);
    target = method != NULL
        ? PyBytes_FromStringAndSize((const char *)target_start, target_len) : NULL;
    if (target == NULL) goto error;
    *method_out = method;
    *target_out = target;
    *minor_out = minor_version;
    *headers_out = headers;
    return 1;

error:
    Py_XDECREF(method);
    Py_XDECREF(target);
    Py_XDECREF(headers);
    return -1;
}

PyObject *
wreath_http_parse_request(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    PyObject *method = NULL;
    PyObject *target = NULL;
    PyObject *headers = NULL;
    PyObject *result;
    PyObject *minor;
    PyObject *consumed_obj;
    Py_ssize_t consumed = 0;
    int minor_version = 0;
    int status;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    /* Standalone entry point (not the server hot path): locate the head
     * terminator once here and hand its offset to the shared parser. */
    const uint8_t *terminator = wreath_memmem(
        view.buf, view.len, (const uint8_t *)"\r\n\r\n", 4
    );
    status = wreath_http_parse_request_parts(
        view.buf, view.len,
        terminator != NULL ? terminator - (const uint8_t *)view.buf : -1,
        &method, &target, &minor_version, &headers, &consumed, PY_SSIZE_T_MAX
    );
    PyBuffer_Release(&view);
    if (status < 0) return NULL;
    if (status == 0) Py_RETURN_NONE;
    minor = PyLong_FromLong(minor_version);
    consumed_obj = minor != NULL ? PyLong_FromSsize_t(consumed) : NULL;
    result = consumed_obj != NULL ? PyTuple_New(5) : NULL;
    if (result == NULL) {
        Py_DECREF(method);
        Py_DECREF(target);
        Py_DECREF(headers);
        Py_XDECREF(minor);
        Py_XDECREF(consumed_obj);
        return NULL;
    }
    PyTuple_SET_ITEM(result, 0, method);
    PyTuple_SET_ITEM(result, 1, target);
    PyTuple_SET_ITEM(result, 2, minor);
    PyTuple_SET_ITEM(result, 3, headers);
    PyTuple_SET_ITEM(result, 4, consumed_obj);
    return result;
}

PyObject *
wreath_http_parse_response(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    const uint8_t *data;
    const uint8_t *head_end;
    const uint8_t *p;
    const uint8_t *end;
    const uint8_t *reason_start;
    const uint8_t *reason_end;
    PyObject *headers = NULL;
    PyObject *reason = NULL;
    PyObject *result = NULL;
    PyObject *minor_obj = NULL;
    PyObject *status_obj = NULL;
    PyObject *consumed_obj = NULL;
    Py_ssize_t consumed;
    int minor;
    int status;

    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    data = view.buf;
    head_end = wreath_memmem(data, view.len, (const uint8_t *)"\r\n\r\n", 4);
    if (head_end == NULL) {
        PyBuffer_Release(&view);
        Py_RETURN_NONE;
    }
    consumed = (head_end - data) + 4;
    p = data;
    end = head_end + 2;
    if (end - p < 14 || memcmp(p, "HTTP/1.", 7) != 0 ||
        (p[7] != '0' && p[7] != '1') || p[8] != ' ' ||
        p[9] < '0' || p[9] > '9' || p[10] < '0' || p[10] > '9' ||
        p[11] < '0' || p[11] > '9' || (p[12] != ' ' && p[12] != '\r')) {
        malformed("malformed response status line");
        goto error;
    }
    minor = p[7] - '0';
    status = (p[9] - '0') * 100 + (p[10] - '0') * 10 + (p[11] - '0');
    if (status < 100) {
        malformed("malformed response status");
        goto error;
    }
    if (p[12] == ' ') {
        reason_start = p + 13;
        p = reason_start;
        while (p < end && *p != '\r') {
            uint8_t c = *p;
            if (c != '\t' && (c < 0x20 || c == 0x7f)) {
                malformed("invalid response reason");
                goto error;
            }
            p++;
        }
        reason_end = p;
    } else {
        reason_start = p + 12;
        reason_end = reason_start;
        p = reason_start;
    }
    if (end - p < 2 || p[0] != '\r' || p[1] != '\n') {
        malformed("malformed response status line");
        goto error;
    }
    p += 2;
    headers = PyList_New(0);
    if (headers == NULL) goto error;
    while (p < end) {
        const uint8_t *name_start;
        const uint8_t *value_start;
        const uint8_t *value_end;
        Py_ssize_t name_len;
        PyObject *name;
        PyObject *value;
        PyObject *pair;
        if (*p == ' ' || *p == '\t') {
            malformed("obsolete header line folding is not supported");
            goto error;
        }
        name_start = p;
        while (p < end && TOKEN_CHARS[*p]) p++;
        name_len = p - name_start;
        if (name_len == 0 || p >= end || *p != ':') {
            malformed("malformed header name");
            goto error;
        }
        p++;
        while (p < end && (*p == ' ' || *p == '\t')) p++;
        value_start = p;
        while (p < end && *p != '\r') {
            uint8_t c = *p;
            if (c != '\t' && (c < 0x20 || c == 0x7f)) {
                malformed("invalid header value byte");
                goto error;
            }
            p++;
        }
        if (end - p < 2 || p[1] != '\n') {
            malformed("malformed header line ending");
            goto error;
        }
        value_end = p;
        while (value_end > value_start &&
               (value_end[-1] == ' ' || value_end[-1] == '\t')) value_end--;
        p += 2;
        if (name_len <= 64) {
            uint8_t lowered[64];
            for (Py_ssize_t i = 0; i < name_len; i++) {
                uint8_t c = name_start[i];
                lowered[i] = c >= 'A' && c <= 'Z'
                    ? (uint8_t)(c + ('a' - 'A')) : c;
            }
            name = header_name_object(lowered, name_len);
        } else {
            /* Allocate uninitialised and fill. Copying the source in and
             * lowercasing in place is only safe while `name_len > 64` holds:
             * `PyBytes_FromStringAndSize` with a non-NULL source returns the
             * interpreter's immortal singleton for a length-1 string, and
             * writing through it corrupts `b"A"` process-wide. That is a live
             * defect elsewhere in this tree, reached through multipart. Passing
             * NULL always allocates, so this branch stays correct on its own
             * terms rather than on the guard above it. */
            name = PyBytes_FromStringAndSize(NULL, name_len);
            if (name != NULL) {
                uint8_t *name_buf = (uint8_t *)PyBytes_AS_STRING(name);
                for (Py_ssize_t i = 0; i < name_len; i++) {
                    uint8_t c = name_start[i];
                    name_buf[i] = (c >= 'A' && c <= 'Z')
                        ? (uint8_t)(c + ('a' - 'A')) : c;
                }
            }
        }
        if (name == NULL) goto error;
        value = PyBytes_FromStringAndSize(
            (const char *)value_start, value_end - value_start
        );
        pair = value != NULL ? PyTuple_Pack(2, name, value) : NULL;
        Py_DECREF(name);
        Py_XDECREF(value);
        if (pair == NULL || PyList_Append(headers, pair) < 0) {
            Py_XDECREF(pair);
            goto error;
        }
        Py_DECREF(pair);
    }
    reason = PyBytes_FromStringAndSize(
        (const char *)reason_start, reason_end - reason_start
    );
    minor_obj = reason != NULL ? PyLong_FromLong(minor) : NULL;
    status_obj = minor_obj != NULL ? PyLong_FromLong(status) : NULL;
    consumed_obj = status_obj != NULL ? PyLong_FromSsize_t(consumed) : NULL;
    result = consumed_obj != NULL ? PyTuple_New(5) : NULL;
    if (result == NULL) goto error;
    PyTuple_SET_ITEM(result, 0, minor_obj);
    PyTuple_SET_ITEM(result, 1, status_obj);
    PyTuple_SET_ITEM(result, 2, reason);
    PyTuple_SET_ITEM(result, 3, headers);
    PyTuple_SET_ITEM(result, 4, consumed_obj);
    PyBuffer_Release(&view);
    return result;

error:
    PyBuffer_Release(&view);
    Py_XDECREF(headers);
    Py_XDECREF(reason);
    Py_XDECREF(minor_obj);
    Py_XDECREF(status_obj);
    Py_XDECREF(consumed_obj);
    Py_XDECREF(result);
    return NULL;
}

static int
http_ascii_equals(const uint8_t *data, Py_ssize_t len, const char *literal)
{
    Py_ssize_t literal_len = (Py_ssize_t)strlen(literal);
    if (len != literal_len) return 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        uint8_t c = data[i];
        if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        if (c != (uint8_t)literal[i]) return 0;
    }
    return 1;
}

static int
http_add_size(Py_ssize_t *total, Py_ssize_t amount)
{
    if (amount < 0 || *total > PY_SSIZE_T_MAX - amount) {
        PyErr_SetString(PyExc_OverflowError, "serialized HTTP request is too large");
        return -1;
    }
    *total += amount;
    return 0;
}

PyObject *
wreath_http_serialize_request(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *method_obj;
    PyObject *target_obj;
    PyObject *host_obj;
    PyObject *headers_obj;
    PyObject *body_obj;
    PyObject *method = NULL;
    PyObject *headers = NULL;
    PyObject *output = NULL;
    Py_buffer target = {0};
    Py_buffer host = {0};
    Py_buffer body = {0};
    Py_ssize_t total = 0;
    char length_buf[32];
    Py_ssize_t length_len = 0;
    char *write;

    if (!PyArg_ParseTuple(
            args, "OOOOO:http_serialize_request", &method_obj, &target_obj,
            &host_obj, &headers_obj, &body_obj)) return NULL;
    method = PyUnicode_AsASCIIString(method_obj);
    if (method == NULL) goto error;
    if (PyObject_GetBuffer(target_obj, &target, PyBUF_SIMPLE) < 0) goto error;
    if (PyObject_GetBuffer(host_obj, &host, PyBUF_SIMPLE) < 0) goto error;
    if (PyObject_GetBuffer(body_obj, &body, PyBUF_SIMPLE) < 0) goto error;
    {
        const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(method);
        Py_ssize_t len = PyBytes_GET_SIZE(method);
        if (len == 0) {
            PyErr_SetString(PyExc_ValueError, "invalid HTTP method");
            goto error;
        }
        for (Py_ssize_t i = 0; i < len; i++) {
            if (!TOKEN_CHARS[data[i]]) {
                PyErr_SetString(PyExc_ValueError, "invalid HTTP method");
                goto error;
            }
        }
    }
    {
        const uint8_t *data = target.buf;
        if (target.len == 0 ||
            (data[0] != '/' && !(target.len == 1 && data[0] == '*'))) {
            PyErr_SetString(PyExc_ValueError, "invalid request target");
            goto error;
        }
        for (Py_ssize_t i = 0; i < target.len; i++) {
            if (data[i] <= 0x20 || data[i] == 0x7f) {
                PyErr_SetString(PyExc_ValueError, "invalid request target");
                goto error;
            }
        }
    }
    if (host.len == 0) {
        PyErr_SetString(PyExc_ValueError, "invalid host");
        goto error;
    }
    for (Py_ssize_t i = 0; i < host.len; i++) {
        uint8_t c = ((const uint8_t *)host.buf)[i];
        if (c != '\t' && (c < 0x20 || c == 0x7f)) {
            PyErr_SetString(PyExc_ValueError, "invalid host");
            goto error;
        }
    }
    headers = PySequence_Fast(headers_obj, "headers must be an iterable of pairs");
    if (headers == NULL) goto error;
    if (http_add_size(&total, PyBytes_GET_SIZE(method)) < 0 ||
        http_add_size(&total, 1 + target.len +
            (Py_ssize_t)(sizeof(" HTTP/1.1\r\nhost: ") - 1) + host.len + 2) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(headers); index++) {
        PyObject *pair = PySequence_Fast(
            PySequence_Fast_GET_ITEM(headers, index), "header must be a pair"
        );
        Py_buffer name = {0};
        Py_buffer value = {0};
        int invalid = 0;
        if (pair == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(pair) != 2 ||
            PyObject_GetBuffer(PySequence_Fast_GET_ITEM(pair, 0), &name, PyBUF_SIMPLE) < 0 ||
            PyObject_GetBuffer(PySequence_Fast_GET_ITEM(pair, 1), &value, PyBUF_SIMPLE) < 0) {
            PyBuffer_Release(&name);
            PyBuffer_Release(&value);
            Py_DECREF(pair);
            goto error;
        }
        if (name.len == 0) invalid = 1;
        for (Py_ssize_t i = 0; i < name.len && !invalid; i++)
            if (!TOKEN_CHARS[((const uint8_t *)name.buf)[i]]) invalid = 1;
        if (invalid) PyErr_SetString(PyExc_ValueError, "invalid header name");
        for (Py_ssize_t i = 0; i < value.len && !invalid; i++) {
            uint8_t c = ((const uint8_t *)value.buf)[i];
            if (c != '\t' && (c < 0x20 || c == 0x7f)) {
                PyErr_SetString(PyExc_ValueError, "invalid header value");
                invalid = 1;
            }
        }
        if (!invalid && http_ascii_equals(name.buf, name.len, "host")) {
            PyErr_SetString(PyExc_ValueError, "host header is owned by the client");
            invalid = 1;
        }
        if (!invalid && http_ascii_equals(name.buf, name.len, "content-length")) {
            PyErr_SetString(PyExc_ValueError, "content-length is owned by the client");
            invalid = 1;
        }
        if (!invalid && http_ascii_equals(name.buf, name.len, "transfer-encoding")) {
            PyErr_SetString(PyExc_ValueError, "transfer-encoding requires streaming mode");
            invalid = 1;
        }
        if (!invalid && http_add_size(&total, name.len + 2 + value.len + 2) < 0)
            invalid = 1;
        PyBuffer_Release(&name);
        PyBuffer_Release(&value);
        Py_DECREF(pair);
        if (invalid) goto error;
    }
    if (body.len > 0) {
        length_len = PyOS_snprintf(length_buf, sizeof(length_buf), "%zd", body.len);
        if (length_len <= 0 ||
            http_add_size(&total,
                (Py_ssize_t)(sizeof("content-length: ") - 1) + length_len + 2) < 0)
            goto error;
    }
    if (http_add_size(&total, 2 + body.len) < 0) goto error;
    output = PyBytes_FromStringAndSize(NULL, total);
    if (output == NULL) goto error;
    write = PyBytes_AS_STRING(output);
#define HTTP_COPY(source, size) do { memcpy(write, (source), (size)); write += (size); } while (0)
    HTTP_COPY(PyBytes_AS_STRING(method), PyBytes_GET_SIZE(method));
    *write++ = ' ';
    HTTP_COPY(target.buf, target.len);
    HTTP_COPY(" HTTP/1.1\r\nhost: ", sizeof(" HTTP/1.1\r\nhost: ") - 1);
    HTTP_COPY(host.buf, host.len);
    HTTP_COPY("\r\n", 2);
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(headers); index++) {
        PyObject *pair = PySequence_Fast(PySequence_Fast_GET_ITEM(headers, index), "header pair");
        Py_buffer name = {0};
        Py_buffer value = {0};
        if (pair == NULL ||
            PyObject_GetBuffer(PySequence_Fast_GET_ITEM(pair, 0), &name, PyBUF_SIMPLE) < 0 ||
            PyObject_GetBuffer(PySequence_Fast_GET_ITEM(pair, 1), &value, PyBUF_SIMPLE) < 0) {
            Py_XDECREF(pair);
            PyBuffer_Release(&name);
            PyBuffer_Release(&value);
            goto error;
        }
        for (Py_ssize_t i = 0; i < name.len; i++) {
            uint8_t c = ((const uint8_t *)name.buf)[i];
            *write++ = c >= 'A' && c <= 'Z' ? (char)(c + ('a' - 'A')) : (char)c;
        }
        HTTP_COPY(": ", 2);
        HTTP_COPY(value.buf, value.len);
        HTTP_COPY("\r\n", 2);
        PyBuffer_Release(&name);
        PyBuffer_Release(&value);
        Py_DECREF(pair);
    }
    if (body.len > 0) {
        HTTP_COPY("content-length: ", sizeof("content-length: ") - 1);
        HTTP_COPY(length_buf, length_len);
        HTTP_COPY("\r\n", 2);
    }
    HTTP_COPY("\r\n", 2);
    HTTP_COPY(body.buf, body.len);
#undef HTTP_COPY
    Py_DECREF(method);
    Py_DECREF(headers);
    PyBuffer_Release(&target);
    PyBuffer_Release(&host);
    PyBuffer_Release(&body);
    return output;

error:
    Py_XDECREF(method);
    Py_XDECREF(headers);
    Py_XDECREF(output);
    PyBuffer_Release(&target);
    PyBuffer_Release(&host);
    PyBuffer_Release(&body);
    return NULL;
}
