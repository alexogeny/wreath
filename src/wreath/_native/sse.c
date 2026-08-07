/* Server-Sent Event framing.
 *
 * The byte-for-byte twin of `_sse_frame_fields` in src/wreath/response.py,
 * which remains the reference implementation and the parity contract;
 * tests/test_sse_frame_parity.py asserts the two agree.
 *
 * Why this is in C at all: the pure version normalises line endings with
 * `value.replace("\r\n", "\n").replace("\r", "\n").split("\n")`, which copies
 * the whole payload twice and then allocates a list of substrings, before an
 * f-string per line, a join, and an encode -- about five passes over the data
 * and O(lines) allocations. That is structural rather than constant-factor, and
 * it is paid once per event *per subscriber*, so a fan-out stream multiplies it.
 * This walks the payload once, straight into the output buffer.
 *
 * The caller has already resolved the event's shape and coerced its fields to
 * str; only the framing lives here. `name` and `ident` are refused rather than
 * normalised when they contain CR or LF, matching `_sse_single_line`: a newline
 * there would let caller text append arbitrary frames to the stream.
 */
#include "wreathcore.h"
#include "bytes_writer.h"

/* Emit `text` as one `field: segment` line per line of text, normalising CRLF
 * and bare CR to LF on the fly. An empty segment emits `field:` with no space,
 * matching the pure encoder. Each line is terminated with LF here; the caller
 * appends the final blank line.
 *
 * CR and LF cannot occur inside a multi-byte UTF-8 sequence, so scanning bytes
 * is safe on UTF-8 input. */
static int
sse_emit_lines(WreathBytesWriter *w, const char *field, Py_ssize_t field_len,
               const char *text, Py_ssize_t text_len, int *emitted)
{
    Py_ssize_t start = 0;
    Py_ssize_t i = 0;

    for (;;) {
        int at_end = (i >= text_len);
        char c = at_end ? '\0' : text[i];
        if (!at_end && c != '\n' && c != '\r') {
            i++;
            continue;
        }
        Py_ssize_t seg_len = i - start;
        if (wreath_writer_write(w, field, field_len) < 0) {
            return -1;
        }
        if (seg_len > 0) {
            if (wreath_writer_write(w, ": ", 2) < 0 ||
                wreath_writer_write(w, text + start, seg_len) < 0) {
                return -1;
            }
        }
        else if (wreath_writer_write(w, ":", 1) < 0) {
            return -1;
        }
        if (wreath_writer_write(w, "\n", 1) < 0) {
            return -1;
        }
        *emitted = 1;
        if (at_end) {
            return 0;
        }
        /* CRLF counts as one break, matching the replace() chain. */
        if (c == '\r' && i + 1 < text_len && text[i + 1] == '\n') {
            i++;
        }
        i++;
        start = i;
    }
}

/* Optional str argument -> UTF-8 view, or NULL when the argument is None. */
static const char *
sse_optional_utf8(PyObject *obj, const char *what, Py_ssize_t *size)
{
    if (obj == Py_None) {
        *size = 0;
        return NULL;
    }
    if (!PyUnicode_Check(obj)) {
        PyErr_Format(PyExc_TypeError, "SSE %s must be str or None, got %s", what,
                     Py_TYPE(obj)->tp_name);
        return NULL;
    }
    const char *data = PyUnicode_AsUTF8AndSize(obj, size);
    if (data == NULL) {
        *size = 0;
    }
    return data;
}

/* A single-line field: refused rather than normalised if it holds CR or LF. */
static int
sse_emit_single(WreathBytesWriter *w, const char *field, Py_ssize_t field_len,
                PyObject *value, int *emitted)
{
    Py_ssize_t size = 0;
    const char *data = sse_optional_utf8(value, field, &size);
    if (data == NULL) {
        return PyErr_Occurred() ? -1 : 0;
    }
    if (memchr(data, '\r', (size_t)size) != NULL ||
        memchr(data, '\n', (size_t)size) != NULL) {
        PyErr_Format(PyExc_ValueError, "SSE %s must not contain a newline", field);
        return -1;
    }
    if (wreath_writer_write(w, field, field_len) < 0 || wreath_writer_write(w, ": ", 2) < 0 ||
        wreath_writer_write(w, data, size) < 0 || wreath_writer_write(w, "\n", 1) < 0) {
        return -1;
    }
    *emitted = 1;
    return 0;
}

/* sse_frame(comment, name, ident, retry, data) -> bytes */
PyObject *
wreath_sse_frame(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *comment;
    PyObject *name;
    PyObject *ident;
    PyObject *retry;
    PyObject *data;
    WreathBytesWriter w;
    int emitted = 0;

    if (!PyArg_ParseTuple(args, "OOOOO:sse_frame", &comment, &name, &ident, &retry,
                          &data)) {
        return NULL;
    }

    if (wreath_writer_init(&w, 128) < 0) {
        return NULL;
    }

    if (comment != Py_None) {
        Py_ssize_t size = 0;
        const char *text = sse_optional_utf8(comment, "comment", &size);
        if (text == NULL) {
            goto error;
        }
        /* Field name is empty for a comment: the line reads ": text". */
        if (sse_emit_lines(&w, "", 0, text, size, &emitted) < 0) {
            goto error;
        }
    }
    if (sse_emit_single(&w, "event", 5, name, &emitted) < 0) {
        goto error;
    }
    if (sse_emit_single(&w, "id", 2, ident, &emitted) < 0) {
        goto error;
    }
    if (retry != Py_None) {
        PyObject *text = PyObject_Str(retry);
        if (text == NULL) {
            goto error;
        }
        Py_ssize_t size = 0;
        const char *chars = PyUnicode_AsUTF8AndSize(text, &size);
        int failed = chars == NULL || wreath_writer_write(&w, "retry: ", 7) < 0 ||
                     wreath_writer_write(&w, chars, size) < 0 || wreath_writer_write(&w, "\n", 1) < 0;
        Py_DECREF(text);
        if (failed) {
            goto error;
        }
        emitted = 1;
    }
    if (data != Py_None) {
        Py_ssize_t size = 0;
        const char *text = sse_optional_utf8(data, "data", &size);
        if (text == NULL) {
            goto error;
        }
        if (sse_emit_lines(&w, "data", 4, text, size, &emitted) < 0) {
            goto error;
        }
    }
    if (!emitted) {
        /* A bare keep-alive comment. */
        if (wreath_writer_write(&w, ":\n", 2) < 0) {
            goto error;
        }
    }
    /* The blank line that terminates the event. */
    if (wreath_writer_write(&w, "\n", 1) < 0) {
        goto error;
    }
    return wreath_writer_finish(&w);

error:
    Py_XDECREF(w.bytes);
    return NULL;
}
