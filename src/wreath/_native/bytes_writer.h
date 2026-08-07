/* Append into a growable `PyBytes`, then hand it back with one shrinking resize.
 *
 * Four encoders wrote this out independently -- `json.c`'s `Writer`,
 * `msgpack.c`'s `MpWriter`, `protobuf.c`'s `PbWriter` and `sse.c`'s `SseWriter`
 * -- and `wreath-dup-scan` grouped the four `*_grow` bodies as byte-identical
 * once it learned to read C. Their comments had already noticed ("the same
 * strategy as json.c and msgpack.c"); nothing had collapsed them.
 *
 * The strategy each of them describes: appending straight into the object that
 * will be returned makes finishing a document a shrink rather than a second
 * buffer and a copy. Growth is geometric and never additive, which is one of
 * the patterns `wreath-native-lint` exists to catch (NC004).
 *
 * Every function here is `static inline` in a header rather than a compiled
 * symbol, so each encoder still inlines its own appends exactly as it did when
 * it owned the code -- this is a de-duplication, and it is not allowed to cost
 * the hot path a call.
 */
#ifndef WREATH_BYTES_WRITER_H
#define WREATH_BYTES_WRITER_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <string.h>

typedef struct {
    PyObject *bytes;
    char *buf;
    Py_ssize_t len;
    Py_ssize_t cap;
} WreathBytesWriter;

/* Start with `capacity` bytes of room. Returns -1 with an exception set. */
static inline int
wreath_writer_init(WreathBytesWriter *w, Py_ssize_t capacity)
{
    w->bytes = PyBytes_FromStringAndSize(NULL, capacity);
    if (w->bytes == NULL) {
        return -1;
    }
    w->buf = PyBytes_AS_STRING(w->bytes);
    w->len = 0;
    w->cap = capacity;
    return 0;
}

/* The slow half of `reserve`, kept out of line of the append so the common
 * case is a compare and a fallthrough. */
static inline int
wreath_writer_grow(WreathBytesWriter *w, Py_ssize_t need)
{
    Py_ssize_t cap = w->cap;
    while (cap - w->len < need) {
        cap += (cap >> 1) + 64;
    }
    if (_PyBytes_Resize(&w->bytes, cap) < 0) {
        return -1; /* w->bytes is cleared by _PyBytes_Resize */
    }
    w->buf = PyBytes_AS_STRING(w->bytes);
    w->cap = cap;
    return 0;
}

static inline int
wreath_writer_reserve(WreathBytesWriter *w, Py_ssize_t need)
{
    return (w->cap - w->len >= need) ? 0 : wreath_writer_grow(w, need);
}

static inline int
wreath_writer_write(WreathBytesWriter *w, const char *data, Py_ssize_t len)
{
    if (wreath_writer_reserve(w, len) < 0) {
        return -1;
    }
    memcpy(w->buf + w->len, data, (size_t)len);
    w->len += len;
    return 0;
}

static inline int
wreath_writer_byte(WreathBytesWriter *w, char c)
{
    if (wreath_writer_reserve(w, 1) < 0) {
        return -1;
    }
    w->buf[w->len++] = c;
    return 0;
}

/* The finished object, shrunk to what was written. Steals the writer's
 * reference either way; on failure `w->bytes` is already cleared. */
static inline PyObject *
wreath_writer_finish(WreathBytesWriter *w)
{
    if (_PyBytes_Resize(&w->bytes, w->len) < 0) {
        return NULL;
    }
    PyObject *result = w->bytes;
    w->bytes = NULL;
    w->buf = NULL;
    w->len = w->cap = 0;
    return result;
}

#endif /* WREATH_BYTES_WRITER_H */
