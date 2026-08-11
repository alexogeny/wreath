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

/* A private incremental input buffer. Unlike WreathBytesWriter this never
 * becomes a Python object: one parser owns it, appends received bytes, and
 * advances `start` as complete frames are consumed. Keeping the offset avoids
 * a memmove after every feed; reserve compacts only when the unused prefix is
 * needed for the next append. */
typedef struct {
    char *data;
    Py_ssize_t start;
    Py_ssize_t end;
    Py_ssize_t capacity;
} WreathByteBuffer;

static inline void
wreath_buffer_init(WreathByteBuffer *buffer)
{
    buffer->data = NULL;
    buffer->start = 0;
    buffer->end = 0;
    buffer->capacity = 0;
}

static inline void
wreath_buffer_clear(WreathByteBuffer *buffer)
{
    PyMem_Free(buffer->data);
    wreath_buffer_init(buffer);
}

static inline Py_ssize_t
wreath_buffer_size(const WreathByteBuffer *buffer)
{
    return buffer->end - buffer->start;
}

static inline char *
wreath_buffer_data(const WreathByteBuffer *buffer)
{
    return buffer->data == NULL ? (char *)"" : buffer->data + buffer->start;
}

static inline int
wreath_buffer_reserve(WreathByteBuffer *buffer, Py_ssize_t append)
{
    Py_ssize_t length = wreath_buffer_size(buffer);
    Py_ssize_t needed;
    Py_ssize_t capacity;
    char *grown;
    if (append < 0 || append > PY_SSIZE_T_MAX - length) {
        PyErr_NoMemory();
        return -1;
    }
    if (buffer->capacity - buffer->end >= append) return 0;
    needed = length + append;
    if (buffer->start != 0 && buffer->capacity >= needed) {
        if (length != 0)
            memmove(buffer->data, buffer->data + buffer->start, (size_t)length);
        buffer->start = 0;
        buffer->end = length;
        return 0;
    }
    capacity = buffer->capacity < 256 ? 256 : buffer->capacity;
    while (capacity < needed) {
        Py_ssize_t increment = (capacity >> 1) + 64;
        if (capacity > PY_SSIZE_T_MAX - increment) {
            capacity = needed;
            break;
        }
        capacity += increment;
    }
    grown = PyMem_Realloc(buffer->data, (size_t)capacity);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    buffer->data = grown;
    buffer->capacity = capacity;
    if (buffer->start != 0) {
        if (length != 0)
            memmove(buffer->data, buffer->data + buffer->start, (size_t)length);
        buffer->start = 0;
        buffer->end = length;
    }
    return 0;
}

static inline int
wreath_buffer_append(WreathByteBuffer *buffer, const char *data, Py_ssize_t length)
{
    if (wreath_buffer_reserve(buffer, length) < 0) return -1;
    if (length != 0)
        memcpy(buffer->data + buffer->end, data, (size_t)length);
    buffer->end += length;
    return 0;
}

static inline void
wreath_buffer_consume(WreathByteBuffer *buffer, Py_ssize_t length)
{
    buffer->start += length;
    if (buffer->start == buffer->end) buffer->start = buffer->end = 0;
}

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
    if (need < 0 || w->len > PY_SSIZE_T_MAX - need) {
        PyErr_NoMemory();
        return -1;
    }
    while (cap - w->len < need) {
        Py_ssize_t increment = (cap >> 1) + 64;
        if (cap > PY_SSIZE_T_MAX - increment) {
            cap = w->len + need;
            break;
        }
        cap += increment;
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
