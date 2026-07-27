/* MessagePack encoder (serialize only).
 *
 * The byte-for-byte twin of src/wreath/_pure/msgpack.py, which remains the
 * reference implementation and the parity contract; tests/test_negotiation.py
 * pins both to the spec's known-answer vectors.
 *
 * Scope matches the pure encoder deliberately: nil, bool, int, float, str, bin,
 * array, map -- enough for the JSON-shaped data content negotiation serves.
 * Decoding is not needed for response encoding and is absent from both sides.
 *
 * Encoding choices that must not drift from the pure twin:
 *   - float is always float64 (0xCB); no float32 narrowing, because a narrowing
 *     encoder is lossy and the pure side never did it.
 *   - str uses str8 (0xD9) for lengths 32..255 rather than str16.
 *   - bool is tested before int, since Python's bool is an int subclass.
 *   - dict keys go through the same value encoder, so non-str keys are allowed
 *     exactly where the pure encoder allows them.
 */
#include "wreathcore.h"

/* Matches WREATH_JSON_MAX_DEPTH: the recursion bound is a property of the
 * encoder's stack use, not of the format. */
#define WREATH_MSGPACK_MAX_DEPTH 1000

/* Appends into a growable PyBytes so finishing a document is a shrinking
 * resize rather than an extra buffer copy -- the same strategy as json.c. */
typedef struct {
    PyObject *bytes;
    char *buf;
    Py_ssize_t len;
    Py_ssize_t cap;
} MpWriter;

static int
mp_grow(MpWriter *w, Py_ssize_t need)
{
    Py_ssize_t cap = w->cap;
    /* Geometric, never additive: additive growth on a per-element append is
     * one of the patterns wreath-native-lint exists to catch. */
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
mp_reserve(MpWriter *w, Py_ssize_t need)
{
    return (w->cap - w->len >= need) ? 0 : mp_grow(w, need);
}

static inline int
mp_byte(MpWriter *w, unsigned char c)
{
    if (mp_reserve(w, 1) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)c;
    return 0;
}

static inline int
mp_write(MpWriter *w, const char *data, Py_ssize_t len)
{
    if (mp_reserve(w, len) < 0) {
        return -1;
    }
    memcpy(w->buf + w->len, data, (size_t)len);
    w->len += len;
    return 0;
}

/* Big-endian stores. MessagePack is network byte order throughout. */
static inline int
mp_tag_u8(MpWriter *w, unsigned char tag, unsigned char value)
{
    if (mp_reserve(w, 2) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)tag;
    w->buf[w->len++] = (char)value;
    return 0;
}

static inline int
mp_tag_u16(MpWriter *w, unsigned char tag, uint16_t value)
{
    if (mp_reserve(w, 3) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)tag;
    w->buf[w->len++] = (char)(value >> 8);
    w->buf[w->len++] = (char)(value & 0xFF);
    return 0;
}

static inline int
mp_tag_u32(MpWriter *w, unsigned char tag, uint32_t value)
{
    if (mp_reserve(w, 5) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)tag;
    w->buf[w->len++] = (char)(value >> 24);
    w->buf[w->len++] = (char)((value >> 16) & 0xFF);
    w->buf[w->len++] = (char)((value >> 8) & 0xFF);
    w->buf[w->len++] = (char)(value & 0xFF);
    return 0;
}

static inline int
mp_tag_u64(MpWriter *w, unsigned char tag, uint64_t value)
{
    if (mp_reserve(w, 9) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)tag;
    for (int shift = 56; shift >= 0; shift -= 8) {
        w->buf[w->len++] = (char)((value >> shift) & 0xFF);
    }
    return 0;
}

static int
mp_encode_int(MpWriter *w, PyObject *obj)
{
    int overflow = 0;
    long long signed_value = PyLong_AsLongLongAndOverflow(obj, &overflow);
    if (signed_value == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (overflow > 0) {
        /* Above int64: still valid if it fits uint64, matching the pure
         * encoder's 0 <= n <= 0xFFFFFFFFFFFFFFFF branch. */
        unsigned long long unsigned_value = PyLong_AsUnsignedLongLong(obj);
        if (unsigned_value == (unsigned long long)-1 && PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_SetString(PyExc_ValueError,
                            "integer out of range for MessagePack");
            return -1;
        }
        return mp_tag_u64(w, 0xCF, (uint64_t)unsigned_value);
    }
    if (overflow < 0) {
        PyErr_SetString(PyExc_ValueError, "integer out of range for MessagePack");
        return -1;
    }

    if (signed_value >= 0) {
        if (signed_value <= 0x7F) {
            return mp_byte(w, (unsigned char)signed_value);
        }
        if (signed_value <= 0xFF) {
            return mp_tag_u8(w, 0xCC, (unsigned char)signed_value);
        }
        if (signed_value <= 0xFFFF) {
            return mp_tag_u16(w, 0xCD, (uint16_t)signed_value);
        }
        if (signed_value <= 0xFFFFFFFFLL) {
            return mp_tag_u32(w, 0xCE, (uint32_t)signed_value);
        }
        return mp_tag_u64(w, 0xCF, (uint64_t)signed_value);
    }
    if (signed_value >= -0x20) {
        return mp_byte(w, (unsigned char)(signed_value & 0xFF));
    }
    if (signed_value >= -0x80) {
        return mp_tag_u8(w, 0xD0, (unsigned char)(signed_value & 0xFF));
    }
    if (signed_value >= -0x8000) {
        return mp_tag_u16(w, 0xD1, (uint16_t)signed_value);
    }
    if (signed_value >= -0x80000000LL) {
        return mp_tag_u32(w, 0xD2, (uint32_t)signed_value);
    }
    return mp_tag_u64(w, 0xD3, (uint64_t)signed_value);
}

static int
mp_encode_str(MpWriter *w, PyObject *obj)
{
    Py_ssize_t size = 0;
    const char *data = PyUnicode_AsUTF8AndSize(obj, &size);
    if (data == NULL) {
        return -1;
    }
    if (size <= 0x1F) {
        if (mp_byte(w, (unsigned char)(0xA0 | size)) < 0) {
            return -1;
        }
    }
    else if (size <= 0xFF) {
        if (mp_tag_u8(w, 0xD9, (unsigned char)size) < 0) {
            return -1;
        }
    }
    else if (size <= 0xFFFF) {
        if (mp_tag_u16(w, 0xDA, (uint16_t)size) < 0) {
            return -1;
        }
    }
    else if (size <= 0xFFFFFFFFLL) {
        if (mp_tag_u32(w, 0xDB, (uint32_t)size) < 0) {
            return -1;
        }
    }
    else {
        PyErr_SetString(PyExc_ValueError, "string too long for MessagePack");
        return -1;
    }
    return mp_write(w, data, size);
}

static int
mp_encode_bin(MpWriter *w, const char *data, Py_ssize_t size)
{
    if (size <= 0xFF) {
        if (mp_tag_u8(w, 0xC4, (unsigned char)size) < 0) {
            return -1;
        }
    }
    else if (size <= 0xFFFF) {
        if (mp_tag_u16(w, 0xC5, (uint16_t)size) < 0) {
            return -1;
        }
    }
    else if (size <= 0xFFFFFFFFLL) {
        if (mp_tag_u32(w, 0xC6, (uint32_t)size) < 0) {
            return -1;
        }
    }
    else {
        PyErr_SetString(PyExc_ValueError, "bytes too long for MessagePack");
        return -1;
    }
    return mp_write(w, data, size);
}

static int
mp_encode_header(MpWriter *w, Py_ssize_t n, unsigned char fix, unsigned char tag16,
                 unsigned char tag32, const char *what)
{
    if (n <= 0xF) {
        return mp_byte(w, (unsigned char)(fix | n));
    }
    if (n <= 0xFFFF) {
        return mp_tag_u16(w, tag16, (uint16_t)n);
    }
    if (n <= 0xFFFFFFFFLL) {
        return mp_tag_u32(w, tag32, (uint32_t)n);
    }
    PyErr_Format(PyExc_ValueError, "%s too long for MessagePack", what);
    return -1;
}

static int mp_encode_value(MpWriter *w, PyObject *obj, int depth);

static int
mp_encode_value(MpWriter *w, PyObject *obj, int depth)
{
    if (obj == Py_None) {
        return mp_byte(w, 0xC0);
    }
    /* Before the int test: Python's bool is an int subclass, and the pure
     * encoder orders these the same way for the same reason. */
    if (obj == Py_True) {
        return mp_byte(w, 0xC3);
    }
    if (obj == Py_False) {
        return mp_byte(w, 0xC2);
    }
    if (PyLong_Check(obj)) {
        return mp_encode_int(w, obj);
    }
    if (PyFloat_Check(obj)) {
        /* Always float64, matching the pure encoder. Narrowing to float32 for
         * values that happen to fit would silently change what a client
         * receives. */
        double value = PyFloat_AS_DOUBLE(obj);
        uint64_t bits;
        memcpy(&bits, &value, sizeof(bits));
        return mp_tag_u64(w, 0xCB, bits);
    }
    if (PyUnicode_Check(obj)) {
        return mp_encode_str(w, obj);
    }
    if (PyBytes_Check(obj)) {
        return mp_encode_bin(w, PyBytes_AS_STRING(obj), PyBytes_GET_SIZE(obj));
    }

    if (depth >= WREATH_MSGPACK_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError, "MessagePack document nests too deeply");
        return -1;
    }

    if (PyList_Check(obj)) {
        Py_ssize_t n = PyList_GET_SIZE(obj);
        if (mp_encode_header(w, n, 0x90, 0xDC, 0xDD, "array") < 0) {
            return -1;
        }
        for (Py_ssize_t i = 0; i < n; i++) {
            if (mp_encode_value(w, PyList_GET_ITEM(obj, i), depth + 1) < 0) {
                return -1;
            }
        }
        return 0;
    }
    if (PyTuple_Check(obj)) {
        Py_ssize_t n = PyTuple_GET_SIZE(obj);
        if (mp_encode_header(w, n, 0x90, 0xDC, 0xDD, "array") < 0) {
            return -1;
        }
        for (Py_ssize_t i = 0; i < n; i++) {
            if (mp_encode_value(w, PyTuple_GET_ITEM(obj, i), depth + 1) < 0) {
                return -1;
            }
        }
        return 0;
    }
    if (PyDict_Check(obj)) {
        Py_ssize_t n = PyDict_GET_SIZE(obj);
        if (mp_encode_header(w, n, 0x80, 0xDE, 0xDF, "map") < 0) {
            return -1;
        }
        Py_ssize_t pos = 0;
        PyObject *key = NULL;
        PyObject *value = NULL;
        /* PyDict_Next borrows; nothing here can run arbitrary Python code that
         * would mutate the dict, because every encoder branch above operates on
         * exact built-in types. */
        while (PyDict_Next(obj, &pos, &key, &value)) {
            if (mp_encode_value(w, key, depth + 1) < 0 ||
                mp_encode_value(w, value, depth + 1) < 0) {
                return -1;
            }
        }
        return 0;
    }
    if (PyByteArray_Check(obj)) {
        return mp_encode_bin(w, PyByteArray_AS_STRING(obj),
                             PyByteArray_GET_SIZE(obj));
    }
    if (PyMemoryView_Check(obj)) {
        Py_buffer *view = PyMemoryView_GET_BUFFER(obj);
        if (!PyBuffer_IsContiguous(view, 'C')) {
            PyErr_SetString(PyExc_TypeError,
                            "cannot msgpack-encode a non-contiguous memoryview");
            return -1;
        }
        return mp_encode_bin(w, (const char *)view->buf, view->len);
    }

    PyErr_Format(PyExc_TypeError, "cannot msgpack-encode %s",
                 Py_TYPE(obj)->tp_name);
    return -1;
}

PyObject *
wreath_msgpack_dumps(PyObject *Py_UNUSED(self), PyObject *obj)
{
    MpWriter w;
    w.cap = 256;
    w.len = 0;
    w.bytes = PyBytes_FromStringAndSize(NULL, w.cap);
    if (w.bytes == NULL) {
        return NULL;
    }
    w.buf = PyBytes_AS_STRING(w.bytes);
    if (mp_encode_value(&w, obj, 0) < 0) {
        Py_XDECREF(w.bytes);
        return NULL;
    }
    if (_PyBytes_Resize(&w.bytes, w.len) < 0) {
        return NULL;
    }
    return w.bytes;
}
