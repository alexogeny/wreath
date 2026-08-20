/* HTTP/1.x request head parser in the picohttpparser style: find the header
 * terminator first, then scan the head with flat byte loops. */
#include "wreathcore.h"

#include "simd.h"

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

/* A comma-delimited, case-insensitive token list. Request framing consumes
 * this while the parser still has the current header span hot; keeping the
 * definition here avoids a second HeaderBlock traversal in the HTTP/1
 * protocol. */
static int
header_value_has_token(const char *value, Py_ssize_t size,
                       const char *token, Py_ssize_t token_size)
{
    Py_ssize_t start = 0;
    while (start <= size) {
        Py_ssize_t end = start;
        const char *part;
        Py_ssize_t part_size;
        while (end < size && value[end] != ',') end++;
        part = value + start;
        part_size = end - start;
        while (part_size > 0 && (part[0] == ' ' || part[0] == '\t')) {
            part++;
            part_size--;
        }
        while (part_size > 0 &&
               (part[part_size - 1] == ' ' || part[part_size - 1] == '\t')) {
            part_size--;
        }
        if (part_size == token_size &&
            PyOS_strnicmp(part, token, token_size) == 0) {
            return 1;
        }
        if (end == size) break;
        start = end + 1;
    }
    return 0;
}

int
wreath_http_parse_request_parts(
    const uint8_t *data, Py_ssize_t len, Py_ssize_t head_end_off,
    PyObject **method_out, PyObject **target_out, int *minor_out,
    PyObject **headers_out, Py_ssize_t *consumed_out, Py_ssize_t max_headers,
    WreathHttpRequestMeta *request_meta
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
    Py_ssize_t host_count = 0;
    const char *first_length = NULL;
    Py_ssize_t first_length_size = 0;
    int saw_transfer = 0;
    int chunked_count = 0;
    int expect_count = 0;
    int connection_close = 0;
    int connection_keep_alive = 0;
    int connection_upgrade = 0;
    const char *upgrade = NULL;
    Py_ssize_t upgrade_size = 0;

    *method_out = NULL;
    *target_out = NULL;
    *headers_out = NULL;
    if (request_meta != NULL) {
        *request_meta = (WreathHttpRequestMeta){0};
    }
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
    /* One owned copy keeps validated spans alive after the protocol consumes
     * its connection buffer.  Names are lowercased in that private copy and
     * the ASGI list is not built unless Python actually asks for it. */
    headers = wreath_header_block_new_raw(data, *consumed_out);
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
        /* One dispatched scan stops on whatever ends the value: the CR that
         * closes the line, or a byte no value may carry. Which one it was is
         * decided below, exactly as the per-byte loop this replaces did.
         * Values are the long part of a head -- a user-agent or a cookie runs
         * far past a register -- so this is where the width pays. */
        p += wreath_value_run((const char *)p, (ptrdiff_t)(end - p));
        if (p < end && *p != '\r') {
            malformed("invalid header value byte");
            goto error;
        }
        if (end - p < 2 || p[1] != '\n') {
            malformed("malformed header line ending");
            goto error;
        }
        value_end = p;
        while (value_end > value_start &&
               (value_end[-1] == ' ' || value_end[-1] == '\t')) value_end--;
        p += 2;
        /* Folded a byte at a time, and measured to be the right way round.
         *
         * A vector fold of this loop looks obviously worthwhile -- fifteen
         * names a request, no data dependencies -- and is not. Header names are
         * four to seventeen bytes (`host`, `accept`, `user-agent`,
         * `content-length`, `accept-encoding`), and over that range a
         * five-arm SIMD kernel measured *no resolved gain at all* against this
         * loop: 0.102us vs 0.102us at four bytes, 0.107 vs 0.107 at sixteen.
         * A win appears at seventeen bytes and is 0.006us at twenty-seven;
         * the 1.5x only arrives at 128 bytes, which no header name is.
         *
         * Fifteen names at 0.006us would be 0.09us against a 2.6us head parse,
         * most of it inside the noise floor -- so the kernel was written,
         * cross-checked against the byte loop over all 256 values, measured,
         * and then deleted. Recorded here rather than in a commit message
         * because this is where the next person will think of it.
         * (2026-08-01, 11 interleaved rounds against an A/A control.) */
        Py_ssize_t name_offset = name_start - data;
        Py_ssize_t value_offset = value_start - data;
        char *owned = wreath_header_block_raw_data(headers);
        if (owned == NULL) goto error;
        for (Py_ssize_t i = 0; i < name_len; i++) {
            unsigned char c = (unsigned char)owned[name_offset + i];
            if (c >= 'A' && c <= 'Z') {
                owned[name_offset + i] = (char)(c + ('a' - 'A'));
            }
        }
        if (name_len == 4 && memcmp(owned + name_offset, "host", 4) == 0) {
            host_count++;
        }
        if (request_meta != NULL && request_meta->err_status == 0) {
            const char *name = owned + name_offset;
            const char *value = owned + value_offset;
            Py_ssize_t value_size = value_end - value_start;
            if (name_len == 6 && memcmp(name, "expect", 6) == 0) {
                expect_count++;
                if (value_size != 12 ||
                    PyOS_strnicmp(value, "100-continue", 12) != 0) {
                    request_meta->err_status = 417;
                }
                else {
                    request_meta->send_continue = 1;
                }
            }
            if (name_len == 14 && memcmp(name, "content-length", 14) == 0) {
                if (first_length == NULL) {
                    first_length = value;
                    first_length_size = value_size;
                }
                else if (first_length_size != value_size ||
                         memcmp(first_length, value, (size_t)value_size) != 0) {
                    request_meta->err_status = 400;
                }
            }
            else if (name_len == 17 &&
                     memcmp(name, "transfer-encoding", 17) == 0) {
                Py_ssize_t start = 0;
                saw_transfer = 1;
                while (start <= value_size) {
                    Py_ssize_t token_end = start;
                    const char *part;
                    Py_ssize_t part_size;
                    while (token_end < value_size && value[token_end] != ',') {
                        token_end++;
                    }
                    part = value + start;
                    part_size = token_end - start;
                    while (part_size > 0 && (part[0] == ' ' || part[0] == '\t')) {
                        part++;
                        part_size--;
                    }
                    while (part_size > 0 &&
                           (part[part_size - 1] == ' ' ||
                            part[part_size - 1] == '\t')) {
                        part_size--;
                    }
                    if (part_size != 7 ||
                        PyOS_strnicmp(part, "chunked", 7) != 0) {
                        request_meta->err_status = 400;
                        break;
                    }
                    chunked_count++;
                    if (token_end == value_size) break;
                    start = token_end + 1;
                }
            }
            else if (name_len == 10 && memcmp(name, "connection", 10) == 0) {
                connection_close |= header_value_has_token(value, value_size, "close", 5);
                connection_keep_alive |= header_value_has_token(
                    value, value_size, "keep-alive", 10);
                connection_upgrade |= header_value_has_token(
                    value, value_size, "upgrade", 7);
            }
            else if (upgrade == NULL && name_len == 7 &&
                     memcmp(name, "upgrade", 7) == 0) {
                upgrade = value;
                upgrade_size = value_size;
            }
        }
        if (wreath_header_block_append_span(
                headers, name_offset, name_len, value_offset,
                value_end - value_start) < 0) goto error;
    }
    if (request_meta != NULL) {
        request_meta->host_count = host_count;
        if (request_meta->err_status != 0) {
            /* `decide_framing` returned immediately for an inline semantic
             * error, before connection-token post-processing. */
            request_meta->keep_alive = minor_version == 1;
        }
        else {
            request_meta->keep_alive = minor_version == 1
                ? !connection_close : connection_keep_alive;
            if (upgrade != NULL && connection_upgrade) {
                request_meta->upgrade_request = upgrade_size == 9 &&
                    PyOS_strnicmp(upgrade, "websocket", 9) == 0;
            }
            if (expect_count > 1) {
                request_meta->err_status = 417;
            }
            else if (saw_transfer && first_length != NULL) {
                request_meta->err_status = 400;
            }
            else if (saw_transfer) {
                if (chunked_count != 1) {
                    request_meta->err_status = 400;
                }
                else {
                    request_meta->kind = 2;
                }
            }
            else if (first_length != NULL) {
                Py_ssize_t value = 0;
                if (first_length_size == 0) {
                    request_meta->err_status = 400;
                }
                for (Py_ssize_t i = 0;
                     i < first_length_size && request_meta->err_status == 0; i++) {
                    int digit = (unsigned char)first_length[i] - '0';
                    if (digit < 0 || digit > 9 ||
                        value > (PY_SSIZE_T_MAX - digit) / 10) {
                        request_meta->err_status = 400;
                    }
                    else {
                        value = value * 10 + digit;
                    }
                }
                if (request_meta->err_status == 0) {
                    request_meta->kind = 1;
                    request_meta->length = value;
                }
            }
        }
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
        &method, &target, &minor_version, &headers, &consumed, PY_SSIZE_T_MAX,
        NULL
    );
    PyBuffer_Release(&view);
    if (status < 0) return NULL;
    if (status == 0) Py_RETURN_NONE;
    {
        PyObject *materialized = wreath_headers_materialize(headers);
        if (materialized == NULL) {
            Py_DECREF(method);
            Py_DECREF(target);
            Py_DECREF(headers);
            return NULL;
        }
        Py_SETREF(headers, materialized);
    }
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

static int http_ascii_equals(
    const uint8_t *data, Py_ssize_t len, const char *literal);
static int http_is_ows(uint8_t c);

void
wreath_http_response_head_clear(WreathHttpResponseHead *head)
{
    Py_CLEAR(head->headers);
    Py_CLEAR(head->reason);
}

int
wreath_http_parse_response_parts(
    const uint8_t *data, Py_ssize_t size, PyObject *method,
    WreathHttpResponseHead *head)
{
    const uint8_t *head_end;
    const uint8_t *p;
    const uint8_t *end;
    const uint8_t *reason_start;
    const uint8_t *reason_end;
    const uint8_t *first_length = NULL;
    Py_ssize_t first_length_size = 0;
    Py_ssize_t header_count = 0;
    Py_ssize_t header_index = 0;
    Py_ssize_t transfer_count = 0;
    Py_ssize_t chunked_count = 0;
    int last_is_chunked = 0;
    int length_conflict = 0;
    int saw_close = 0;
    int saw_keep_alive = 0;

    memset(head, 0, sizeof(*head));
    head->content_length = -1;
    head_end = wreath_memmem(data, size, (const uint8_t *)"\r\n\r\n", 4);
    if (head_end == NULL) {
        return 0;
    }
    head->consumed = (head_end - data) + 4;
    p = data;
    end = head_end + 2;
    if (end - p < 14 || memcmp(p, "HTTP/1.", 7) != 0 ||
        (p[7] != '0' && p[7] != '1') || p[8] != ' ' ||
        p[9] < '0' || p[9] > '9' || p[10] < '0' || p[10] > '9' ||
        p[11] < '0' || p[11] > '9' || (p[12] != ' ' && p[12] != '\r')) {
        malformed("malformed response status line");
        goto error;
    }
    head->minor = p[7] - '0';
    head->status = (p[9] - '0') * 100 + (p[10] - '0') * 10 + (p[11] - '0');
    if (head->status < 100) {
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

    /* Count line endings before materialization so the request path allocates
     * its final header tuple once.  The validating pass below still owns every
     * syntax decision; this pass only determines the tuple size. */
    for (const uint8_t *cursor = p; cursor < end; cursor++) {
        if (*cursor == '\r' && cursor + 1 < end && cursor[1] == '\n') {
            header_count++;
            cursor++;
        }
    }
    head->headers = PyTuple_New(header_count);
    if (head->headers == NULL) goto error;
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
        /* One dispatched scan stops on whatever ends the value: the CR that
         * closes the line, or a byte no value may carry. Which one it was is
         * decided below, exactly as the per-byte loop this replaces did.
         * Values are the long part of a head -- a user-agent or a cookie runs
         * far past a register -- so this is where the width pays. */
        p += wreath_value_run((const char *)p, (ptrdiff_t)(end - p));
        if (p < end && *p != '\r') {
            malformed("invalid header value byte");
            goto error;
        }
        if (end - p < 2 || p[1] != '\n') {
            malformed("malformed header line ending");
            goto error;
        }
        value_end = p;
        while (value_end > value_start &&
               (value_end[-1] == ' ' || value_end[-1] == '\t')) value_end--;
        p += 2;

        if (name_len == (Py_ssize_t)(sizeof("content-length") - 1) &&
            http_ascii_equals(name_start, name_len, "content-length")) {
            Py_ssize_t value_size = value_end - value_start;
            if (first_length == NULL) {
                first_length = value_start;
                first_length_size = value_size;
            } else if (value_size != first_length_size ||
                       memcmp(first_length, value_start,
                              (size_t)value_size) != 0) {
                length_conflict = 1;
            }
        } else if (
            name_len == (Py_ssize_t)(sizeof("transfer-encoding") - 1) &&
            http_ascii_equals(name_start, name_len, "transfer-encoding")) {
            const uint8_t *cursor = value_start;
            for (;;) {
                const uint8_t *separator = cursor;
                const uint8_t *token_end;
                while (separator < value_end && *separator != ',') separator++;
                token_end = separator;
                while (cursor < token_end && http_is_ows(*cursor)) cursor++;
                while (token_end > cursor && http_is_ows(token_end[-1])) token_end--;
                last_is_chunked = http_ascii_equals(
                    cursor, (Py_ssize_t)(token_end - cursor), "chunked");
                chunked_count += last_is_chunked;
                transfer_count++;
                if (separator == value_end) break;
                cursor = separator + 1;
            }
        } else if (
            name_len == (Py_ssize_t)(sizeof("connection") - 1) &&
            http_ascii_equals(name_start, name_len, "connection")) {
            const uint8_t *cursor = value_start;
            for (;;) {
                const uint8_t *separator = cursor;
                const uint8_t *token_end;
                while (separator < value_end && *separator != ',') separator++;
                token_end = separator;
                while (cursor < token_end && http_is_ows(*cursor)) cursor++;
                while (token_end > cursor && http_is_ows(token_end[-1])) token_end--;
                saw_close |= http_ascii_equals(
                    cursor, (Py_ssize_t)(token_end - cursor), "close");
                saw_keep_alive |= http_ascii_equals(
                    cursor, (Py_ssize_t)(token_end - cursor), "keep-alive");
                if (separator == value_end) break;
                cursor = separator + 1;
            }
        }

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
        pair = wreath_tuple2_from_owned(name, value);
        if (pair == NULL) goto error;
        if (header_index >= header_count) {
            Py_DECREF(pair);
            malformed("malformed response header block");
            goto error;
        }
        PyTuple_SET_ITEM(head->headers, header_index++, pair);
    }
    if (header_index != header_count) {
        malformed("malformed response header block");
        goto error;
    }
    head->reason = PyBytes_FromStringAndSize(
        (const char *)reason_start, reason_end - reason_start
    );
    if (head->reason == NULL) goto error;

    /* The public head parser historically validates only head syntax.  The
     * fused transaction supplies its method and asks this same pass to decide
     * framing and reuse; a NULL method retains the standalone boundary. */
    if (method == NULL) return 1;

    int no_body = head->status == 204 || head->status == 304;
    if (!no_body && PyUnicode_Check(method)) {
        int comparison = PyUnicode_CompareWithASCIIString(method, "HEAD");
        if (comparison == -1 && PyErr_Occurred()) goto error;
        no_body = comparison == 0;
    }
    if (no_body) {
        head->framing = 0;
        head->content_length = 0;
    } else if (transfer_count && first_length != NULL) {
        PyErr_SetString(
            PyExc_ValueError,
            "response has conflicting transfer-encoding and content-length");
        goto error;
    } else if (transfer_count) {
        if (!last_is_chunked || chunked_count != 1) {
            PyErr_SetString(PyExc_ValueError,
                            "unsupported response transfer-encoding");
            goto error;
        }
        head->framing = 1;
    } else if (first_length != NULL) {
        if (length_conflict) {
            PyErr_SetString(PyExc_ValueError,
                            "response has conflicting content-length values");
            goto error;
        }
        if (first_length_size == 0) {
            PyErr_SetString(PyExc_ValueError, "invalid response content-length");
            goto error;
        }
        Py_ssize_t length = 0;
        for (Py_ssize_t i = 0; i < first_length_size; i++) {
            int digit = first_length[i] - '0';
            if (digit < 0 || digit > 9) {
                PyErr_SetString(PyExc_ValueError,
                                "invalid response content-length");
                goto error;
            }
            if (length > (PY_SSIZE_T_MAX - digit) / 10) {
                PyErr_SetString(PyExc_OverflowError,
                                "Python int too large to convert to C ssize_t");
                goto error;
            }
            length = length * 10 + digit;
        }
        head->framing = 2;
        head->content_length = length;
    } else {
        head->framing = 3;
    }
    head->reusable = head->framing != 3 &&
        !saw_close && (head->minor != 0 || saw_keep_alive);
    return 1;

error:
    wreath_http_response_head_clear(head);
    return -1;
}

PyObject *
wreath_http_parse_response(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    WreathHttpResponseHead head;
    PyObject *headers_list = NULL;
    PyObject *minor = NULL;
    PyObject *status = NULL;
    PyObject *consumed = NULL;
    PyObject *result = NULL;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    int parsed = wreath_http_parse_response_parts(
        view.buf, view.len, NULL, &head);
    PyBuffer_Release(&view);
    if (parsed < 0) return NULL;
    if (parsed == 0) Py_RETURN_NONE;
    headers_list = PySequence_List(head.headers);
    minor = headers_list != NULL ? PyLong_FromLong(head.minor) : NULL;
    status = minor != NULL ? PyLong_FromLong(head.status) : NULL;
    consumed = status != NULL ? PyLong_FromSsize_t(head.consumed) : NULL;
    result = consumed != NULL ? PyTuple_New(5) : NULL;
    if (result != NULL) {
        PyTuple_SET_ITEM(result, 0, minor);
        PyTuple_SET_ITEM(result, 1, status);
        PyTuple_SET_ITEM(result, 2, Py_NewRef(head.reason));
        PyTuple_SET_ITEM(result, 3, headers_list);
        PyTuple_SET_ITEM(result, 4, consumed);
    } else {
        Py_XDECREF(headers_list);
        Py_XDECREF(minor);
        Py_XDECREF(status);
        Py_XDECREF(consumed);
    }
    wreath_http_response_head_clear(&head);
    return result;
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
http_is_ows(uint8_t c)
{
    /* bytes.strip()'s ASCII whitespace set, matching the reference codec. */
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

static PyObject *framing_modes[4];

static PyObject *
http_framing_result(int mode, PyObject *length)
{
    static const char *names[4] = {"none", "chunked", "length", "close"};
    PyObject *result;
    if (length == NULL) return NULL;
    if (framing_modes[mode] == NULL) {
        framing_modes[mode] = PyUnicode_InternFromString(names[mode]);
        if (framing_modes[mode] == NULL) {
            Py_DECREF(length);
            return NULL;
        }
    }
    result = PyTuple_New(2);
    if (result == NULL) {
        Py_DECREF(length);
        return NULL;
    }
    PyTuple_SET_ITEM(result, 0, Py_NewRef(framing_modes[mode]));
    PyTuple_SET_ITEM(result, 1, length);
    return result;
}

PyObject *
wreath_http_response_framing(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *method;
    PyObject *headers_obj;
    PyObject *headers = NULL;
    PyObject *first_length = NULL;
    PyObject *parsed_length = NULL;
    long status;
    Py_ssize_t transfer_count = 0;
    Py_ssize_t chunked_count = 0;
    int last_is_chunked = 0;
    int length_conflict = 0;

    if (!PyArg_ParseTuple(args, "OlO:http_response_framing",
                          &method, &status, &headers_obj)) return NULL;
    if (PyUnicode_Check(method)) {
        int comparison = PyUnicode_CompareWithASCIIString(method, "HEAD");
        if (comparison == -1 && PyErr_Occurred()) return NULL;
        if (comparison == 0) return http_framing_result(0, PyLong_FromLong(0));
    }
    if (status == 204 || status == 304) {
        return http_framing_result(0, PyLong_FromLong(0));
    }
    headers = PySequence_Fast(headers_obj, "headers must be a sequence of pairs");
    if (headers == NULL) return NULL;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(headers); index++) {
        PyObject *pair_obj = PySequence_Fast_GET_ITEM(headers, index);
        PyObject *pair = PySequence_Fast(pair_obj, "header must be a pair");
        PyObject *name;
        PyObject *value;
        const uint8_t *data;
        Py_ssize_t len;
        if (pair == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(pair) != 2) {
            Py_DECREF(pair);
            PyErr_SetString(PyExc_ValueError, "header must be a pair");
            goto error;
        }
        name = PySequence_Fast_GET_ITEM(pair, 0);
        value = PySequence_Fast_GET_ITEM(pair, 1);
        if (!PyBytes_Check(name)) {
            Py_DECREF(pair);
            continue;
        }
        data = (const uint8_t *)PyBytes_AS_STRING(name);
        len = PyBytes_GET_SIZE(name);
        if (len == (Py_ssize_t)(sizeof("content-length") - 1) &&
            memcmp(data, "content-length", sizeof("content-length") - 1) == 0) {
            if (!PyBytes_Check(value)) {
                Py_DECREF(pair);
                PyErr_SetString(PyExc_TypeError, "content-length must be bytes");
                goto error;
            }
            if (first_length == NULL) {
                first_length = Py_NewRef(value);
            } else {
                int equal = PyObject_RichCompareBool(first_length, value, Py_EQ);
                if (equal < 0) {
                    Py_DECREF(pair);
                    goto error;
                }
                if (!equal) length_conflict = 1;
            }
        } else if (len == (Py_ssize_t)(sizeof("transfer-encoding") - 1) &&
                   memcmp(data, "transfer-encoding",
                          sizeof("transfer-encoding") - 1) == 0) {
            const uint8_t *cursor;
            const uint8_t *end;
            if (!PyBytes_Check(value)) {
                Py_DECREF(pair);
                PyErr_SetString(PyExc_TypeError, "transfer-encoding must be bytes");
                goto error;
            }
            cursor = (const uint8_t *)PyBytes_AS_STRING(value);
            end = cursor + PyBytes_GET_SIZE(value);
            for (;;) {
                const uint8_t *separator = cursor;
                const uint8_t *token_end;
                const uint8_t *start;
                while (separator < end && *separator != ',') separator++;
                token_end = separator;
                start = cursor;
                while (start < token_end && http_is_ows(*start)) start++;
                while (token_end > start && http_is_ows(token_end[-1])) token_end--;
                last_is_chunked = http_ascii_equals(
                    start, (Py_ssize_t)(token_end - start), "chunked");
                chunked_count += last_is_chunked;
                transfer_count++;
                if (separator == end) break;
                cursor = separator + 1;
            }
        }
        Py_DECREF(pair);
    }
    if (transfer_count && first_length != NULL) {
        PyErr_SetString(PyExc_ValueError,
                        "response has conflicting transfer-encoding and content-length");
        goto error;
    }
    if (transfer_count) {
        Py_DECREF(headers);
        Py_XDECREF(first_length);
        if (!last_is_chunked || chunked_count != 1) {
            PyErr_SetString(PyExc_ValueError, "unsupported response transfer-encoding");
            return NULL;
        }
        return http_framing_result(1, PyLong_FromLong(-1));
    }
    if (first_length != NULL) {
        const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(first_length);
        Py_ssize_t len = PyBytes_GET_SIZE(first_length);
        if (length_conflict) {
            PyErr_SetString(PyExc_ValueError,
                            "response has conflicting content-length values");
            goto error;
        }
        if (len == 0) {
            PyErr_SetString(PyExc_ValueError, "invalid response content-length");
            goto error;
        }
        for (Py_ssize_t i = 0; i < len; i++) {
            if (data[i] < '0' || data[i] > '9') {
                PyErr_SetString(PyExc_ValueError, "invalid response content-length");
                goto error;
            }
        }
        parsed_length = PyLong_FromString(PyBytes_AS_STRING(first_length), NULL, 10);
        if (parsed_length == NULL) goto error;
        Py_DECREF(headers);
        Py_DECREF(first_length);
        return http_framing_result(2, parsed_length);
    }
    Py_DECREF(headers);
    return http_framing_result(3, PyLong_FromLong(-1));

error:
    Py_XDECREF(headers);
    Py_XDECREF(first_length);
    Py_XDECREF(parsed_length);
    return NULL;
}

PyObject *
wreath_http_response_keeps_alive(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers_obj;
    PyObject *headers;
    int minor;
    int framed;
    int saw_close = 0;
    int saw_keep_alive = 0;

    if (!PyArg_ParseTuple(args, "iOp:http_response_keeps_alive",
                          &minor, &headers_obj, &framed)) return NULL;
    if (!framed) Py_RETURN_FALSE;
    headers = PySequence_Fast(headers_obj, "headers must be a sequence of pairs");
    if (headers == NULL) return NULL;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(headers); index++) {
        PyObject *pair_obj = PySequence_Fast_GET_ITEM(headers, index);
        PyObject *pair = PySequence_Fast(pair_obj, "header must be a pair");
        PyObject *name;
        PyObject *value;
        if (pair == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(pair) != 2) {
            Py_DECREF(pair);
            PyErr_SetString(PyExc_ValueError, "header must be a pair");
            goto error;
        }
        name = PySequence_Fast_GET_ITEM(pair, 0);
        value = PySequence_Fast_GET_ITEM(pair, 1);
        if (PyBytes_Check(name) &&
            PyBytes_GET_SIZE(name) == (Py_ssize_t)(sizeof("connection") - 1) &&
            memcmp(PyBytes_AS_STRING(name), "connection", sizeof("connection") - 1) == 0) {
            const uint8_t *cursor;
            const uint8_t *end;
            if (!PyBytes_Check(value)) {
                Py_DECREF(pair);
                PyErr_SetString(PyExc_TypeError, "connection header must be bytes");
                goto error;
            }
            cursor = (const uint8_t *)PyBytes_AS_STRING(value);
            end = cursor + PyBytes_GET_SIZE(value);
            for (;;) {
                const uint8_t *separator = cursor;
                const uint8_t *token_end;
                while (separator < end && *separator != ',') separator++;
                token_end = separator;
                while (cursor < token_end && http_is_ows(*cursor)) cursor++;
                while (token_end > cursor && http_is_ows(token_end[-1])) token_end--;
                saw_close |= http_ascii_equals(
                    cursor, (Py_ssize_t)(token_end - cursor), "close");
                saw_keep_alive |= http_ascii_equals(
                    cursor, (Py_ssize_t)(token_end - cursor), "keep-alive");
                if (separator == end) break;
                cursor = separator + 1;
            }
        }
        Py_DECREF(pair);
    }
    Py_DECREF(headers);
    if (saw_close || (minor == 0 && !saw_keep_alive)) Py_RETURN_FALSE;
    Py_RETURN_TRUE;

error:
    Py_DECREF(headers);
    return NULL;
}

PyObject *
wreath_http_parse_chunk_size(PyObject *Py_UNUSED(self), PyObject *arg)
{
    const uint8_t *line;
    Py_ssize_t length;
    Py_ssize_t digits;
    char number[1025];

    if (!PyBytes_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "response chunk line must be bytes");
        return NULL;
    }
    line = (const uint8_t *)PyBytes_AS_STRING(arg);
    length = PyBytes_GET_SIZE(arg);
    if (length > 1024) {
        PyErr_SetString(PyExc_ValueError, "response chunk line exceeds limit");
        return NULL;
    }
    if (length < 3 || line[length - 2] != '\r' || line[length - 1] != '\n') {
        PyErr_SetString(PyExc_ValueError, "invalid response chunk size");
        return NULL;
    }

    digits = 0;
    while (digits < length - 2 && line[digits] != ';') {
        uint8_t byte = line[digits];
        if (!((byte >= '0' && byte <= '9') ||
              (byte >= 'a' && byte <= 'f') ||
              (byte >= 'A' && byte <= 'F'))) {
            PyErr_SetString(PyExc_ValueError, "invalid response chunk size");
            return NULL;
        }
        digits++;
    }
    if (digits == 0) {
        PyErr_SetString(PyExc_ValueError, "invalid response chunk size");
        return NULL;
    }

    memcpy(number, line, (size_t)digits);
    number[digits] = '\0';
    return PyLong_FromString(number, NULL, 16);
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
    /* Each header's name and value buffer, captured once. The output size is
     * computed from these exact buffers and the copy reads them again, so a
     * header object cannot report one length while being sized and a larger
     * one while being written -- which is how the two-pass version overflowed
     * its allocation. Small requests never touch the heap for this. */
    Py_buffer inline_views[32];
    Py_buffer *views = inline_views;
    Py_ssize_t view_count = 0;
    Py_ssize_t header_count = 0;

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
        /* A per-byte table lookup rather than one of `simd.h`'s arms. No arm
         * expresses this predicate: `wreath_value_run` and its SWAR twin test
         * for control bytes and DEL, and token-set membership is a different
         * question that a SWAR word cannot answer without the table anyway.
         * The length settles it regardless -- a method is 3-7 bytes, so it
         * never fills the eight-byte word a SWAR step needs, and
         * `wreath_value_run` would itself fall through to its scalar loop
         * below 16. Scalar by decision. */
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
        /* Close to `simd.h`'s `wreath_value_run_swar`, which tests
         * `wreath_swar_lt(word, 0x20) | wreath_swar_eq(word, 0x7f)` -- but not
         * the same predicate: this refuses SPACE (`<= 0x20`) because a space
         * in a request target would split the request line, and the value arm
         * permits HTAB. Reusing it would mean re-checking every stop by hand.
         * Not worth it at this length: an outgoing request target is 30-80
         * bytes, one instruction per byte, and `wreath_value_run` takes its
         * own scalar path below 16 bytes. Scalar by decision, not by
         * oversight. */
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
    /* This one *is* `simd.h`'s predicate exactly: `wreath_is_value_stop` is
     * `(c < 0x20 && c != '\t') || c == 0x7f`, so
     * `wreath_value_run(host.buf, host.len) == host.len` would answer it. Left
     * scalar anyway, and the reason is the length rather than the shape: a
     * host header is 10-40 bytes, `wreath_value_run` dispatches to its own
     * scalar loop under 16 of them, and above that the saving is single-digit
     * nanoseconds on a scan that runs once per outgoing request. A hand-rolled
     * loop beside a primitive is usually an oversight; this one is a decision,
     * and this is the number behind it. */
    for (Py_ssize_t i = 0; i < host.len; i++) {
        uint8_t c = ((const uint8_t *)host.buf)[i];
        if (c != '\t' && (c < 0x20 || c == 0x7f)) {
            PyErr_SetString(PyExc_ValueError, "invalid host");
            goto error;
        }
    }
    headers = PySequence_Fast(headers_obj, "headers must be an iterable of pairs");
    if (headers == NULL) goto error;
    header_count = PySequence_Fast_GET_SIZE(headers);
    if (header_count > (Py_ssize_t)(sizeof(inline_views) / sizeof(inline_views[0])) / 2) {
        if (header_count > PY_SSIZE_T_MAX / (Py_ssize_t)(2 * sizeof(Py_buffer))) {
            PyErr_NoMemory();
            goto error;
        }
        views = PyMem_Malloc((size_t)header_count * 2 * sizeof(Py_buffer));
        if (views == NULL) {
            PyErr_NoMemory();
            goto error;
        }
    }
    if (http_add_size(&total, PyBytes_GET_SIZE(method)) < 0 ||
        http_add_size(&total, 1 + target.len +
            (Py_ssize_t)(sizeof(" HTTP/1.1\r\nhost: ") - 1) + host.len + 2) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < header_count; index++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(headers, index);  /* borrowed */
        Py_buffer *name = &views[view_count];
        Py_buffer *value = &views[view_count + 1];
        PyObject *name_obj, *value_obj;
        int invalid = 0;
        /* A header pair holds exactly two items, so read those two rather than
         * materializing the sequence: a pair object whose __len__ reports 2
         * while its __getitem__ never raises IndexError would otherwise make
         * PySequence_Fast allocate until the process is killed. */
        Py_ssize_t pair_size = PySequence_Size(pair);
        if (pair_size < 0) {
            PyErr_Clear();
            PyErr_SetString(PyExc_TypeError, "header must be a pair");
            goto error;
        }
        if (pair_size != 2) {
            PyErr_SetString(PyExc_ValueError, "header must be a pair");
            goto error;
        }
        name_obj = PySequence_GetItem(pair, 0);
        if (name_obj == NULL) goto error;
        value_obj = PySequence_GetItem(pair, 1);
        if (value_obj == NULL) {
            Py_DECREF(name_obj);
            goto error;
        }
        int got = PyObject_GetBuffer(name_obj, name, PyBUF_SIMPLE);
        if (got == 0 && PyObject_GetBuffer(value_obj, value, PyBUF_SIMPLE) < 0) {
            PyBuffer_Release(name);
            got = -1;
        }
        /* Each view holds its own reference to the exporter, so releasing the
         * item references here cannot free the bytes underneath. */
        Py_DECREF(name_obj);
        Py_DECREF(value_obj);
        if (got < 0) goto error;
        view_count += 2;
        if (name->len == 0) invalid = 1;
        for (Py_ssize_t i = 0; i < name->len && !invalid; i++)
            if (!TOKEN_CHARS[((const uint8_t *)name->buf)[i]]) invalid = 1;
        if (invalid) PyErr_SetString(PyExc_ValueError, "invalid header name");
        for (Py_ssize_t i = 0; i < value->len && !invalid; i++) {
            uint8_t c = ((const uint8_t *)value->buf)[i];
            if (c != '\t' && (c < 0x20 || c == 0x7f)) {
                PyErr_SetString(PyExc_ValueError, "invalid header value");
                invalid = 1;
            }
        }
        if (!invalid && http_ascii_equals(name->buf, name->len, "host")) {
            PyErr_SetString(PyExc_ValueError, "host header is owned by the client");
            invalid = 1;
        }
        if (!invalid && http_ascii_equals(name->buf, name->len, "content-length")) {
            PyErr_SetString(PyExc_ValueError, "content-length is owned by the client");
            invalid = 1;
        }
        if (!invalid && http_ascii_equals(name->buf, name->len, "transfer-encoding")) {
            PyErr_SetString(PyExc_ValueError, "transfer-encoding requires streaming mode");
            invalid = 1;
        }
        if (!invalid && http_add_size(&total, name->len + 2 + value->len + 2) < 0)
            invalid = 1;
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
    for (Py_ssize_t index = 0; index < view_count; index += 2) {
        const Py_buffer *name = &views[index];
        const Py_buffer *value = &views[index + 1];
        for (Py_ssize_t i = 0; i < name->len; i++) {
            uint8_t c = ((const uint8_t *)name->buf)[i];
            *write++ = c >= 'A' && c <= 'Z' ? (char)(c + ('a' - 'A')) : (char)c;
        }
        HTTP_COPY(": ", 2);
        HTTP_COPY(value->buf, value->len);
        HTTP_COPY("\r\n", 2);
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
    for (Py_ssize_t i = 0; i < view_count; i++) PyBuffer_Release(&views[i]);
    if (views != inline_views) PyMem_Free(views);
    PyBuffer_Release(&target);
    PyBuffer_Release(&host);
    PyBuffer_Release(&body);
    return output;

error:
    Py_XDECREF(method);
    Py_XDECREF(headers);
    Py_XDECREF(output);
    for (Py_ssize_t i = 0; i < view_count; i++) PyBuffer_Release(&views[i]);
    if (views != inline_views) PyMem_Free(views);
    PyBuffer_Release(&target);
    PyBuffer_Release(&host);
    PyBuffer_Release(&body);
    return NULL;
}
