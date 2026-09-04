/* multipart/form-data parsing for complete (non-streaming) bodies. */
#include "wreathcore.h"

#define multipart_span_tape 64  /* parts between cancellation checks */

static int
multipart_boundary_valid(const uint8_t *value, Py_ssize_t length)
{
    if (length < 1 || length > 70 || value[length - 1] == ' ') return 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        uint8_t byte = value[index];
        if ((byte >= '0' && byte <= '9') ||
            (byte >= 'A' && byte <= 'Z') ||
            (byte >= 'a' && byte <= 'z')) continue;
        switch (byte) {
            case '\'': case '(': case ')': case '+': case '_': case ',':
            case '-': case '.': case '/': case ':': case '=': case '?': case ' ':
                continue;
            default:
                return 0;
        }
    }
    return 1;
}

/* Limits are Py_ssize_t counts of bytes/parts; a negative value means the caller
 * imposes no limit. Every check is written as a subtraction against a known
 * non-negative bound rather than an addition, so no length arithmetic here can
 * overflow. */

/* Parse "Name: value" header lines in [p, headers_end) into a list of
 * (lowercased-name-bytes, value-bytes) tuples. */
static int
multipart_ascii_equal(const uint8_t *value, Py_ssize_t length,
                      const char *expected, Py_ssize_t expected_length)
{
    if (length != expected_length) return 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        uint8_t byte = value[index];
        if (byte >= 'A' && byte <= 'Z') byte = (uint8_t)(byte + ('a' - 'A'));
        if (byte != (uint8_t)expected[index]) return 0;
    }
    return 1;
}

static PyObject *
parse_part_headers(const uint8_t *p, const uint8_t *headers_end,
                   const uint8_t **disposition, Py_ssize_t *disposition_length)
{
    PyObject *headers = PyList_New(0);
    if (headers == NULL) {
        return NULL;
    }
    while (p < headers_end) {
        const uint8_t *eol = wreath_memmem(p, headers_end - p, (const uint8_t *)"\r\n", 2);
        if (eol == NULL) {
            eol = headers_end;
        }
        const uint8_t *colon = memchr(p, ':', (size_t)(eol - p));
        if (colon == NULL || colon == p) {
            PyErr_SetString(PyExc_ValueError, "malformed multipart header line");
            Py_DECREF(headers);
            return NULL;
        }
        /* Allocate uninitialised and fill, rather than copying the source in and
         * lowercasing in place. `PyBytes_FromStringAndSize` with a *non-NULL*
         * source does not always allocate: for a length-1 string it returns the
         * interpreter's immortal single-character singleton, so writing through
         * the result rewrites `b"A"` to `b"a"` for every user of that object in
         * the process, permanently. A one-letter part-header name was enough to
         * do it, and the request still returned 200. Passing NULL always
         * allocates, so this idiom cannot regress into that bug if the length
         * bound ever changes -- unlike a guarded in-place lowercase, which is
         * only correct for as long as the guard holds. */
        Py_ssize_t name_len = colon - p;
        PyObject *name = PyBytes_FromStringAndSize(NULL, name_len);
        if (name == NULL) {
            Py_DECREF(headers);
            return NULL;
        }
        uint8_t *name_buf = (uint8_t *)PyBytes_AS_STRING(name);
        for (Py_ssize_t i = 0; i < name_len; i++) {
            uint8_t c = p[i];
            if (!wreath_ascii_token[c]) {
                PyErr_SetString(PyExc_ValueError,
                                "multipart header name must be an ASCII token");
                Py_DECREF(name);
                Py_DECREF(headers);
                return NULL;
            }
            name_buf[i] = (c >= 'A' && c <= 'Z') ? (uint8_t)(c + ('a' - 'A')) : c;
        }
        const uint8_t *value_start = colon + 1;
        while (value_start < eol && (*value_start == ' ' || *value_start == '\t')) {
            value_start++;
        }
        const uint8_t *value_end = eol;
        while (value_end > value_start &&
               (value_end[-1] == ' ' || value_end[-1] == '\t')) {
            value_end--;
        }
        if (wreath_value_run((const char *)value_start, value_end - value_start) !=
            value_end - value_start) {
            PyErr_SetString(PyExc_ValueError,
                            "multipart header value contains a control octet");
            Py_DECREF(name);
            Py_DECREF(headers);
            return NULL;
        }
        if (*disposition == NULL && multipart_ascii_equal(
                p, name_len, "content-disposition", 19)) {
            *disposition = value_start;
            *disposition_length = value_end - value_start;
        }
        PyObject *value =
            PyBytes_FromStringAndSize((const char *)value_start, value_end - value_start);
        PyObject *pair = wreath_tuple2_from_owned(name, value);
        if (pair == NULL || PyList_Append(headers, pair) < 0) {
            Py_XDECREF(pair);
            Py_DECREF(headers);
            return NULL;
        }
        Py_DECREF(pair);
        p = (eol == headers_end) ? headers_end : eol + 2;
    }
    return headers;
}

static const uint8_t *
multipart_trim_start(const uint8_t *start, const uint8_t *end)
{
    while (start < end && (*start == ' ' || *start == '\t')) start++;
    return start;
}

static const uint8_t *
multipart_trim_end(const uint8_t *start, const uint8_t *end)
{
    while (end > start && (end[-1] == ' ' || end[-1] == '\t')) end--;
    return end;
}

static PyObject *
multipart_disposition_param(const uint8_t *value, Py_ssize_t length,
                            const char *parameter, Py_ssize_t parameter_length)
{
    if (value == NULL) return Py_NewRef(Py_None);
    const uint8_t *end = value + length;
    for (const uint8_t *fragment = value; fragment <= end;) {
        const uint8_t *fragment_end = fragment;
        int quoted = 0;
        int escaped = 0;
        while (fragment_end < end) {
            uint8_t byte = *fragment_end;
            if (escaped) escaped = 0;
            else if (quoted && byte == '\\') escaped = 1;
            else if (byte == '"') quoted = !quoted;
            else if (byte == ';' && !quoted) break;
            fragment_end++;
        }
        const uint8_t *start = multipart_trim_start(fragment, fragment_end);
        const uint8_t *trimmed_end = multipart_trim_end(start, fragment_end);
        const uint8_t *equals = memchr(start, '=', (size_t)(trimmed_end - start));
        if (equals != NULL) {
            const uint8_t *key_end = multipart_trim_end(start, equals);
            const uint8_t *raw_start = multipart_trim_start(equals + 1, trimmed_end);
            const uint8_t *raw_end = multipart_trim_end(raw_start, trimmed_end);
            if (multipart_ascii_equal(
                    start, key_end - start, parameter, parameter_length)) {
                if (raw_start < raw_end && raw_start[0] == '"') {
                    if (raw_end - raw_start < 2 || raw_end[-1] != '"' || quoted) {
                        PyErr_Format(PyExc_ValueError,
                                     "malformed multipart %s parameter", parameter);
                        return NULL;
                    }
                    raw_start++;
                    raw_end--;
                    Py_ssize_t raw_length = raw_end - raw_start;
                    uint8_t *unescaped = raw_length != 0
                        ? PyMem_Malloc((size_t)raw_length) : NULL;
                    if (raw_length != 0 && unescaped == NULL) return PyErr_NoMemory();
                    Py_ssize_t decoded_length = 0;
                    for (Py_ssize_t index = 0; index < raw_length; index++) {
                        if (raw_start[index] == '\\' && index + 1 < raw_length) index++;
                        unescaped[decoded_length++] = raw_start[index];
                    }
                    PyObject *decoded = PyUnicode_DecodeUTF8(
                        raw_length != 0 ? (const char *)unescaped : "",
                        decoded_length, "replace");
                    PyMem_Free(unescaped);
                    return decoded;
                }
                return PyUnicode_DecodeUTF8(
                    (const char *)raw_start, raw_end - raw_start, "replace");
            }
        }
        if (fragment_end == end) break;
        fragment = fragment_end + 1;
    }
    return Py_NewRef(Py_None);
}

static int
multipart_disposition_is_form_data(const uint8_t *value, Py_ssize_t length)
{
    if (value == NULL) return 1;
    const uint8_t *end = value + length;
    const uint8_t *separator = memchr(value, ';', (size_t)length);
    if (separator == NULL) separator = end;
    const uint8_t *start = multipart_trim_start(value, separator);
    const uint8_t *trimmed_end = multipart_trim_end(start, separator);
    return multipart_ascii_equal(start, trimmed_end - start, "form-data", 9);
}

static PyObject *
multipart_materialize_part(PyObject *part_type, PyObject *headers,
                           const uint8_t *content_start, Py_ssize_t content_length,
                           const uint8_t *disposition, Py_ssize_t disposition_length)
{
    if (!multipart_disposition_is_form_data(disposition, disposition_length)) {
        PyErr_SetString(PyExc_ValueError,
                        "multipart Content-Disposition must be form-data");
        return NULL;
    }
    PyObject *values[4] = {NULL};
    values[0] = multipart_disposition_param(disposition, disposition_length, "name", 4);
    values[1] = values[0] != NULL
        ? multipart_disposition_param(
            disposition, disposition_length, "filename", 8) : NULL;
    values[2] = values[1] != NULL ? Py_NewRef(headers) : NULL;
    values[3] = values[2] != NULL
        ? PyBytes_FromStringAndSize(NULL, content_length) : NULL;
    if (values[3] != NULL && content_length != 0) {
        memcpy(PyBytes_AS_STRING(values[3]), content_start, (size_t)content_length);
    }
    PyObject *part = values[3] != NULL
        ? PyObject_Vectorcall(part_type, values, 4, NULL) : NULL;
    for (Py_ssize_t index = 0; index < 4; index++) Py_XDECREF(values[index]);
    return part;
}

PyObject *
wreath_multipart_part_info(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer block;
    if (PyObject_GetBuffer(arg, &block, PyBUF_SIMPLE) < 0) return NULL;
    const uint8_t *disposition = NULL;
    Py_ssize_t disposition_length = 0;
    const uint8_t *start = block.buf;
    PyObject *headers = parse_part_headers(
        start, start + block.len, &disposition, &disposition_length);
    if (headers != NULL &&
        !multipart_disposition_is_form_data(disposition, disposition_length)) {
        Py_CLEAR(headers);
        PyErr_SetString(PyExc_ValueError,
                        "multipart Content-Disposition must be form-data");
    }
    PyObject *name = headers != NULL
        ? multipart_disposition_param(disposition, disposition_length, "name", 4)
        : NULL;
    PyObject *filename = name != NULL
        ? multipart_disposition_param(disposition, disposition_length, "filename", 8)
        : NULL;
    PyObject *result = filename != NULL
        ? PyTuple_Pack(3, headers, name, filename) : NULL;
    Py_XDECREF(filename);
    Py_XDECREF(name);
    Py_XDECREF(headers);
    PyBuffer_Release(&block);
    return result;
}

PyObject *
wreath_multipart_parse(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwds)
{
    Py_buffer body, boundary;
    Py_ssize_t max_parts = -1;
    Py_ssize_t max_part_header_bytes = -1;
    Py_ssize_t max_part_bytes = -1;
    PyObject *part_factory = Py_None;
    PyObject *part_type = Py_None;
    static char *kwlist[] = {"body", "boundary", "max_parts",
                             "max_part_header_bytes", "max_part_bytes",
                             "part_factory", "part_type", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*y*|nnnOO:multipart_parse", kwlist,
                                     &body, &boundary, &max_parts,
                                     &max_part_header_bytes, &max_part_bytes,
                                     &part_factory, &part_type)) {
        return NULL;
    }
    if (part_factory != Py_None && part_type != Py_None) {
        PyBuffer_Release(&body);
        PyBuffer_Release(&boundary);
        PyErr_SetString(
            PyExc_ValueError, "part_factory and part_type are mutually exclusive");
        return NULL;
    }
    if (!multipart_boundary_valid(boundary.buf, boundary.len)) {
        PyBuffer_Release(&body);
        PyBuffer_Release(&boundary);
        PyErr_SetString(PyExc_ValueError, "boundary must be 1-70 permitted bytes");
        return NULL;
    }

    /* delimiter = CRLF "--" boundary; the first delimiter may omit the CRLF. */
    Py_ssize_t blen = boundary.len;
    uint8_t *delim = PyMem_Malloc((size_t)(blen + 4));
    if (delim == NULL) {
        PyBuffer_Release(&body);
        PyBuffer_Release(&boundary);
        return PyErr_NoMemory();
    }
    delim[0] = '\r';
    delim[1] = '\n';
    delim[2] = '-';
    delim[3] = '-';
    memcpy(delim + 4, boundary.buf, (size_t)blen);
    Py_ssize_t delim_len = blen + 4;

    const uint8_t *data = body.buf;
    Py_ssize_t len = body.len;
    PyObject *parts = NULL;
    PyObject *body_view = NULL;

    const uint8_t *pos;
    if (len >= delim_len - 2 && memcmp(data, delim + 2, (size_t)(delim_len - 2)) == 0) {
        pos = data + (delim_len - 2);
    }
    else {
        const uint8_t *first = wreath_memmem(data, len, delim, delim_len);
        if (first == NULL) {
            PyErr_SetString(PyExc_ValueError, "multipart boundary not found");
            goto done;
        }
        pos = first + delim_len;
    }

    parts = PyList_New(0);
    body_view = part_type == Py_None ? PyMemoryView_FromObject(body.obj) : NULL;
    if (parts == NULL || (part_type == Py_None && body_view == NULL)) {
        Py_CLEAR(parts);
        goto done;
    }

    const uint8_t *end = data + len;
    Py_ssize_t part_count = 0;
    for (;;) {
        /* After a boundary: "--" closes the stream; otherwise expect CRLF
         * (optionally preceded by linear whitespace padding). */
        if (end - pos >= 2 && pos[0] == '-' && pos[1] == '-') {
            const uint8_t *close_end = pos + 2;
            while (close_end < end && (*close_end == ' ' || *close_end == '\t')) {
                close_end++;
            }
            if (close_end != end &&
                (end - close_end < 2 || close_end[0] != '\r' || close_end[1] != '\n')) {
                PyErr_SetString(PyExc_ValueError,
                                "malformed multipart closing boundary");
                goto fail;
            }
            break;
        }
        while (pos < end && (*pos == ' ' || *pos == '\t')) {
            pos++;
        }
        if (end - pos < 2 || pos[0] != '\r' || pos[1] != '\n') {
            PyErr_SetString(PyExc_ValueError, "malformed multipart boundary line");
            goto fail;
        }
        pos += 2;

        /* An immediate CRLF means an empty header block; checking this first
         * keeps a \r\n\r\n inside the part content from being misread as the
         * header terminator. */
        const uint8_t *headers_end;
        const uint8_t *body_start;
        if (end - pos >= 2 && pos[0] == '\r' && pos[1] == '\n') {
            headers_end = pos;
            body_start = pos + 2;
        }
        else {
            headers_end = wreath_memmem(pos, end - pos, (const uint8_t *)"\r\n\r\n", 4);
            if (headers_end == NULL) {
                PyErr_SetString(PyExc_ValueError, "unterminated multipart headers");
                goto fail;
            }
            body_start = headers_end + 4;
        }

        const uint8_t *next = body_start;
        for (;;) {
            next = wreath_memmem(next, end - next, delim, delim_len);
            if (next == NULL) break;
            const uint8_t *suffix = next + delim_len;
            if ((end - suffix >= 2 && suffix[0] == '\r' && suffix[1] == '\n') ||
                (end - suffix >= 2 && suffix[0] == '-' && suffix[1] == '-') ||
                (suffix < end && (*suffix == ' ' || *suffix == '\t'))) break;
            next++;
        }
        if (next == NULL) {
            PyErr_SetString(PyExc_ValueError, "unterminated multipart part");
            goto fail;
        }

        if (max_parts >= 0 && part_count >= max_parts) {
            PyErr_Format(PyExc_ValueError,
                         "multipart form has more than %zd parts", max_parts);
            goto fail;
        }
        if (max_part_header_bytes >= 0 &&
            headers_end - pos > max_part_header_bytes) {
            PyErr_Format(PyExc_ValueError,
                         "multipart part headers exceed %zd bytes",
                         max_part_header_bytes);
            goto fail;
        }
        /* Refuse before building the payload view, so an over-budget part is
         * never retained even once. */
        if (max_part_bytes >= 0 && next - body_start > max_part_bytes) {
            PyErr_Format(PyExc_ValueError,
                         "multipart part exceeds %zd bytes", max_part_bytes);
            goto fail;
        }
        part_count++;
        if (part_count % multipart_span_tape == 0 && PyErr_CheckSignals() < 0) {
            goto fail;
        }

        const uint8_t *disposition = NULL;
        Py_ssize_t disposition_length = 0;
        PyObject *headers = parse_part_headers(
            pos, headers_end, &disposition, &disposition_length);
        if (headers == NULL) {
            goto fail;
        }
        PyObject *content = part_type == Py_None
            ? PySequence_GetSlice(body_view, body_start - data, next - data) : NULL;
        PyObject *part = NULL;
        if (part_type != Py_None) {
            part = multipart_materialize_part(
                part_type, headers, body_start, next - body_start,
                disposition, disposition_length);
        }
        else if (content != NULL) {
            if (part_factory == Py_None) {
                part = wreath_tuple2_from_owned(headers, content);
                headers = NULL;
                content = NULL;
            }
            else {
                part = PyObject_CallFunctionObjArgs(part_factory, headers, content, NULL);
            }
        }
        Py_XDECREF(headers);
        Py_XDECREF(content);
        if (part == NULL || PyList_Append(parts, part) < 0) {
            Py_XDECREF(part);
            goto fail;
        }
        Py_DECREF(part);
        pos = next + delim_len;
    }
    goto done;

fail:
    Py_CLEAR(parts);
done:
    Py_XDECREF(body_view);
    PyMem_Free(delim);
    PyBuffer_Release(&body);
    PyBuffer_Release(&boundary);
    return parts;
}

enum {
    MP_PREAMBLE,
    MP_BOUNDARY,
    MP_HEADERS,
    MP_BODY,
    MP_CLOSE,
    MP_EPILOGUE,
};

typedef struct {
    PyObject_HEAD
    WreathByteBuffer buffer;
    PyObject *opening;
    PyObject *delimiter;
    PyObject *limit_error;
    Py_ssize_t max_parts;
    Py_ssize_t max_header_bytes;
    Py_ssize_t max_part_bytes;
    Py_ssize_t part_count;
    Py_ssize_t part_size;
    int state;
    int closed;
    int preamble_start;
} WreathMultipartStream;

static int
multipart_stream_init(PyObject *object, PyObject *args, PyObject *kwargs)
{
    WreathMultipartStream *self = (WreathMultipartStream *)object;
    Py_buffer boundary;
    PyObject *error_type;
    static char *keywords[] = {
        "boundary", "max_parts", "max_part_header_bytes", "max_part_bytes",
        "limit_error", NULL,
    };
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "y*nnnO:MultipartStreamParser", keywords,
            &boundary, &self->max_parts, &self->max_header_bytes,
            &self->max_part_bytes, &error_type)) return -1;
    if (!multipart_boundary_valid(boundary.buf, boundary.len)) {
        PyBuffer_Release(&boundary);
        PyErr_SetString(PyExc_ValueError, "boundary must be 1-70 permitted bytes");
        return -1;
    }
    self->opening = PyBytes_FromStringAndSize(NULL, boundary.len + 2);
    self->delimiter = PyBytes_FromStringAndSize(NULL, boundary.len + 4);
    if (self->opening == NULL || self->delimiter == NULL) {
        PyBuffer_Release(&boundary);
        return -1;
    }
    memcpy(PyBytes_AS_STRING(self->opening), "--", 2);
    memcpy(PyBytes_AS_STRING(self->opening) + 2, boundary.buf, (size_t)boundary.len);
    memcpy(PyBytes_AS_STRING(self->delimiter), "\r\n--", 4);
    memcpy(PyBytes_AS_STRING(self->delimiter) + 4, boundary.buf, (size_t)boundary.len);
    PyBuffer_Release(&boundary);
    wreath_buffer_init(&self->buffer);
    self->limit_error = Py_NewRef(error_type);
    self->state = MP_PREAMBLE;
    self->preamble_start = 1;
    return 0;
}

static int
multipart_stream_traverse(PyObject *object, visitproc visit, void *arg)
{
    WreathMultipartStream *self = (WreathMultipartStream *)object;
    Py_VISIT(self->opening);
    Py_VISIT(self->delimiter);
    Py_VISIT(self->limit_error);
    return 0;
}

static int
multipart_stream_clear(PyObject *object)
{
    WreathMultipartStream *self = (WreathMultipartStream *)object;
    wreath_buffer_clear(&self->buffer);
    Py_CLEAR(self->opening);
    Py_CLEAR(self->delimiter);
    Py_CLEAR(self->limit_error);
    return 0;
}

static void
multipart_stream_dealloc(PyObject *object)
{
    PyObject_GC_UnTrack(object);
    multipart_stream_clear(object);
    Py_TYPE(object)->tp_free(object);
}

static int
multipart_event(PyObject *events, int kind, const uint8_t *data, Py_ssize_t length)
{
    PyObject *payload = data == NULL ? Py_NewRef(Py_None) :
        PyBytes_FromStringAndSize((const char *)data, length);
    PyObject *kind_object;
    PyObject *event;
    int failed;
    if (payload == NULL) return -1;
    kind_object = PyLong_FromLong(kind);
    event = kind_object == NULL ? NULL : PyTuple_Pack(2, kind_object, payload);
    Py_XDECREF(kind_object);
    Py_DECREF(payload);
    if (event == NULL) return -1;
    failed = PyList_Append(events, event) < 0;
    Py_DECREF(event);
    return failed ? -1 : 0;
}

static int
multipart_limit(WreathMultipartStream *self, const char *format, Py_ssize_t value)
{
    PyObject *message = PyUnicode_FromFormat(format, value);
    if (message == NULL) return -1;
    PyErr_SetObject(self->limit_error, message);
    Py_DECREF(message);
    return -1;
}

static int
multipart_part_data(WreathMultipartStream *self, PyObject *events,
                    const uint8_t *data, Py_ssize_t length)
{
    if (length == 0) return 0;
    if (length > self->max_part_bytes - self->part_size) {
        return multipart_limit(self,
            "multipart part exceeds %zd bytes", self->max_part_bytes);
    }
    self->part_size += length;
    return multipart_event(events, 1, data, length);
}

static PyObject *
multipart_stream_feed(PyObject *object, PyObject *chunk)
{
    WreathMultipartStream *self = (WreathMultipartStream *)object;
    Py_buffer view;
    Py_ssize_t total;
    Py_ssize_t cursor = 0;
    PyObject *events;
    if (self->closed) return PyList_New(0);
    if (PyObject_GetBuffer(chunk, &view, PyBUF_SIMPLE) < 0) return NULL;
    if (wreath_buffer_append(&self->buffer, view.buf, view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    PyBuffer_Release(&view);
    total = wreath_buffer_size(&self->buffer);
    events = PyList_New(0);
    if (events == NULL) return NULL;

    for (;;) {
        const uint8_t *data =
            (const uint8_t *)wreath_buffer_data(&self->buffer) + cursor;
        Py_ssize_t available = total - cursor;
        if (self->state == MP_PREAMBLE) {
            const uint8_t *opening = (const uint8_t *)PyBytes_AS_STRING(self->opening);
            Py_ssize_t opening_len = PyBytes_GET_SIZE(self->opening);
            const uint8_t *delimiter =
                (const uint8_t *)PyBytes_AS_STRING(self->delimiter);
            Py_ssize_t delimiter_len = PyBytes_GET_SIZE(self->delimiter);
            const uint8_t *found = NULL;
            Py_ssize_t matched_length = delimiter_len;
            if (self->preamble_start && available >= opening_len &&
                memcmp(data, opening, (size_t)opening_len) == 0) {
                found = data;
                matched_length = opening_len;
            }
            else if (self->preamble_start && available < opening_len &&
                     memcmp(data, opening, (size_t)available) == 0) {
                break;
            }
            else {
                found = wreath_memmem(data, available, delimiter, delimiter_len);
            }
            if (found == NULL) {
                Py_ssize_t safe = available - delimiter_len + 1;
                if (safe > 0) {
                    cursor += safe;
                    self->preamble_start = 0;
                }
                break;
            }
            cursor += (found - data) + matched_length;
            self->preamble_start = 0;
            self->state = MP_BOUNDARY;
        }
        else if (self->state == MP_BOUNDARY) {
            Py_ssize_t whitespace = 0;
            if (available >= 2 && data[0] == '-' && data[1] == '-') {
                cursor += 2;
                self->state = MP_CLOSE;
                continue;
            }
            while (whitespace < available &&
                   (data[whitespace] == ' ' || data[whitespace] == '\t')) whitespace++;
            if (available < whitespace + 2) break;
            if (data[whitespace] != '\r' || data[whitespace + 1] != '\n') {
                PyErr_SetString(PyExc_ValueError,
                                "malformed multipart boundary line");
                goto error;
            }
            cursor += whitespace + 2;
            self->state = MP_HEADERS;
        }
        else if (self->state == MP_HEADERS) {
            const uint8_t *end;
            Py_ssize_t header_length;
            Py_ssize_t consumed;
            if (available >= 2 && data[0] == '\r' && data[1] == '\n') {
                header_length = 0;
                consumed = 2;
            }
            else {
                end = wreath_memmem(data, available,
                                    (const uint8_t *)"\r\n\r\n", 4);
                if (end == NULL) {
                    if (available > self->max_header_bytes + 3) {
                        multipart_limit(self,
                            "multipart part headers exceed %zd bytes",
                            self->max_header_bytes);
                        goto error;
                    }
                    break;
                }
                header_length = end - data;
                consumed = header_length + 4;
            }
            if (header_length > self->max_header_bytes) {
                multipart_limit(self,
                    "multipart part headers exceed %zd bytes",
                    self->max_header_bytes);
                goto error;
            }
            if (self->part_count >= self->max_parts) {
                multipart_limit(self,
                    "multipart form has more than %zd parts", self->max_parts);
                goto error;
            }
            self->part_count++;
            self->part_size = 0;
            if (multipart_event(events, 0, data, header_length) < 0) goto error;
            cursor += consumed;
            self->state = MP_BODY;
        }
        else if (self->state == MP_BODY) {
            const uint8_t *delimiter =
                (const uint8_t *)PyBytes_AS_STRING(self->delimiter);
            Py_ssize_t delimiter_len = PyBytes_GET_SIZE(self->delimiter);
            const uint8_t *end = data + available;
            const uint8_t *found = data;
            for (;;) {
                found = wreath_memmem(found, end - found, delimiter, delimiter_len);
                if (found == NULL) break;
                const uint8_t *suffix = found + delimiter_len;
                Py_ssize_t suffix_length = end - suffix;
                if (suffix_length == 0 ||
                    (suffix_length == 1 && (suffix[0] == '-' || suffix[0] == '\r')) ||
                    (suffix_length >= 2 && suffix[0] == '\r' && suffix[1] == '\n') ||
                    (suffix_length >= 2 && suffix[0] == '-' && suffix[1] == '-') ||
                    (suffix_length >= 1 && (suffix[0] == ' ' || suffix[0] == '\t'))) {
                    break;
                }
                Py_ssize_t rejected = (found - data) + 1;
                if (multipart_part_data(self, events, data, rejected) < 0) goto error;
                cursor += rejected;
                data += rejected;
                available -= rejected;
                found = data;
            }
            if (found != NULL) {
                Py_ssize_t body_length = found - data;
                Py_ssize_t suffix_length = end - (found + delimiter_len);
                if (suffix_length == 0 ||
                    (suffix_length == 1 &&
                     (found[delimiter_len] == '-' || found[delimiter_len] == '\r'))) {
                    if (multipart_part_data(self, events, data, body_length) < 0) goto error;
                    cursor += body_length;
                    break;
                }
                if (multipart_part_data(self, events, data, body_length) < 0 ||
                    multipart_event(events, 2, NULL, 0) < 0) goto error;
                cursor += body_length + delimiter_len;
                self->state = MP_BOUNDARY;
                continue;
            }
            Py_ssize_t safe = available - delimiter_len + 1;
            if (safe > 0) {
                if (multipart_part_data(self, events, data, safe) < 0) goto error;
                cursor += safe;
            }
            break;
        }
        else if (self->state == MP_CLOSE) {
            Py_ssize_t whitespace = 0;
            while (whitespace < available &&
                   (data[whitespace] == ' ' || data[whitespace] == '\t')) whitespace++;
            if (whitespace == available) break;
            if (available - whitespace < 2 && data[whitespace] == '\r') break;
            if (available - whitespace < 2 || data[whitespace] != '\r' ||
                data[whitespace + 1] != '\n') {
                PyErr_SetString(PyExc_ValueError,
                                "malformed multipart closing boundary");
                goto error;
            }
            self->closed = 1;
            self->state = MP_EPILOGUE;
            cursor = total;
            break;
        }
        else break;
    }
    if (cursor != 0) wreath_buffer_consume(&self->buffer, cursor);
    return events;

error:
    Py_DECREF(events);
    return NULL;
}

static PyObject *
multipart_stream_finish(PyObject *object, PyObject *Py_UNUSED(ignored))
{
    WreathMultipartStream *self = (WreathMultipartStream *)object;
    if (self->state == MP_CLOSE) {
        const uint8_t *data = (const uint8_t *)wreath_buffer_data(&self->buffer);
        Py_ssize_t length = wreath_buffer_size(&self->buffer);
        Py_ssize_t index = 0;
        while (index < length && (data[index] == ' ' || data[index] == '\t')) index++;
        if (index == length) {
            self->closed = 1;
            self->state = MP_EPILOGUE;
        }
    }
    if (!self->closed) {
        PyErr_SetString(PyExc_ValueError, "unterminated multipart part");
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyMethodDef multipart_stream_methods[] = {
    {"feed", multipart_stream_feed, METH_O, "feed(chunk) -> list[event]"},
    {"finish", multipart_stream_finish, METH_NOARGS, "finish() -> None"},
    {NULL, NULL, 0, NULL},
};

static PyType_Slot multipart_stream_slots[] = {
    {Py_tp_init, multipart_stream_init},
    {Py_tp_dealloc, multipart_stream_dealloc},
    {Py_tp_traverse, multipart_stream_traverse},
    {Py_tp_clear, multipart_stream_clear},
    {Py_tp_methods, multipart_stream_methods},
    {Py_tp_new, PyType_GenericNew},
    {0, NULL},
};

static PyType_Spec multipart_stream_spec = {
    .name = "wreath._native._core.MultipartStreamParser",
    .basicsize = sizeof(WreathMultipartStream),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = multipart_stream_slots,
};

int
wreath_register_multipart(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&multipart_stream_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "MultipartStreamParser", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}
