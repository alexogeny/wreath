/* multipart/form-data parsing for complete (non-streaming) bodies. */
#include "wreathcore.h"

#define multipart_span_tape 64  /* parts between cancellation checks */

/* Limits are Py_ssize_t counts of bytes/parts; a negative value means the caller
 * imposes no limit. Every check is written as a subtraction against a known
 * non-negative bound rather than an addition, so no length arithmetic here can
 * overflow. */

/* Parse "Name: value" header lines in [p, headers_end) into a list of
 * (lowercased-name-bytes, value-bytes) tuples. */
static PyObject *
parse_part_headers(const uint8_t *p, const uint8_t *headers_end)
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
        PyObject *name = PyBytes_FromStringAndSize((const char *)p, colon - p);
        if (name == NULL) {
            Py_DECREF(headers);
            return NULL;
        }
        uint8_t *name_buf = (uint8_t *)PyBytes_AS_STRING(name);
        for (Py_ssize_t i = 0; i < PyBytes_GET_SIZE(name); i++) {
            if (name_buf[i] >= 'A' && name_buf[i] <= 'Z') {
                name_buf[i] += 'a' - 'A';
            }
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
        PyObject *value =
            PyBytes_FromStringAndSize((const char *)value_start, value_end - value_start);
        PyObject *pair = (value != NULL) ? PyTuple_Pack(2, name, value) : NULL;
        Py_DECREF(name);
        Py_XDECREF(value);
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

PyObject *
wreath_multipart_parse(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwds)
{
    Py_buffer body, boundary;
    Py_ssize_t max_parts = -1;
    Py_ssize_t max_part_header_bytes = -1;
    Py_ssize_t max_part_bytes = -1;
    PyObject *part_factory = Py_None;
    static char *kwlist[] = {"body", "boundary", "max_parts",
                             "max_part_header_bytes", "max_part_bytes",
                             "part_factory", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*y*|nnnO:multipart_parse", kwlist,
                                     &body, &boundary, &max_parts,
                                     &max_part_header_bytes, &max_part_bytes,
                                     &part_factory)) {
        return NULL;
    }
    if (boundary.len < 1 || boundary.len > 70) {
        PyBuffer_Release(&body);
        PyBuffer_Release(&boundary);
        PyErr_SetString(PyExc_ValueError, "boundary must be 1-70 bytes");
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
    body_view = PyMemoryView_FromObject(body.obj);
    if (parts == NULL || body_view == NULL) {
        Py_CLEAR(parts);
        goto done;
    }

    const uint8_t *end = data + len;
    Py_ssize_t part_count = 0;
    for (;;) {
        /* After a boundary: "--" closes the stream; otherwise expect CRLF
         * (optionally preceded by linear whitespace padding). */
        if (end - pos >= 2 && pos[0] == '-' && pos[1] == '-') {
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

        const uint8_t *next = wreath_memmem(body_start, end - body_start, delim, delim_len);
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

        PyObject *headers = parse_part_headers(pos, headers_end);
        if (headers == NULL) {
            goto fail;
        }
        PyObject *content = PySequence_GetSlice(
            body_view, body_start - data, next - data);
        PyObject *part = NULL;
        if (content != NULL) {
            part = part_factory == Py_None
                ? PyTuple_Pack(2, headers, content)
                : PyObject_CallFunctionObjArgs(part_factory, headers, content, NULL);
        }
        Py_DECREF(headers);
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
