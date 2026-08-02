/* Protocol Buffers wire codec.
 *
 * The byte-for-byte twin of src/wreath/_pure/protobuf.py, which remains the
 * reference implementation and the parity contract; tests/test_protobuf_parity.py
 * asserts the two agree over a corpus, and tests/test_protobuf.py pins both to
 * hand-computed vectors from the encoding specification.
 *
 * Scope matches the pure codec deliberately. This file is bytes-to-values only:
 * it walks a *plan* compiled by wreath/protobuf.py at class creation and never
 * touches a dataclass, so object construction stays in Python and the only
 * thing crossing the boundary is a tuple of ints.
 *
 *   plan row  (number, kind, flags, subplan|None)
 *   values    a list in plan order -- position is identity, no name lookup
 *
 * Decoding choices that must not drift from the pure twin:
 *   - A varint is bounded at ten bytes. This codec reads buffers a peer
 *     controls, and an unbounded continuation run is how a decoder walks off
 *     the end of one.
 *   - Every length prefix is checked against the bytes that actually remain
 *     before anything is allocated or copied.
 *   - Group wire types (3, 4) are refused by name rather than mis-parsed.
 *   - An unknown field is captured verbatim, tag included, and handed back, so
 *     a message survives a round trip through a build with an older
 *     declaration. Dropping them would break the forward compatibility field
 *     numbers exist to provide.
 */
#include "wreathcore.h"

#include <math.h>
#include <string.h>

/* Kind codes. These are the contract with _pure/protobuf.py: the two switch on
 * the same numbering, so a change here is a change there. */
#define PB_KIND_INT32 1
#define PB_KIND_INT64 2
#define PB_KIND_UINT32 3
#define PB_KIND_UINT64 4
#define PB_KIND_SINT32 5
#define PB_KIND_SINT64 6
#define PB_KIND_BOOL 7
#define PB_KIND_ENUM 8
#define PB_KIND_FIXED64 9
#define PB_KIND_SFIXED64 10
#define PB_KIND_DOUBLE 11
#define PB_KIND_FIXED32 12
#define PB_KIND_SFIXED32 13
#define PB_KIND_FLOAT 14
#define PB_KIND_STRING 15
#define PB_KIND_BYTES 16
#define PB_KIND_MESSAGE 17

#define PB_FLAG_REPEATED 1
#define PB_FLAG_PACKED 2
#define PB_FLAG_OPTIONAL 4
#define PB_FLAG_MAP 8

#define PB_WIRE_VARINT 0
#define PB_WIRE_I64 1
#define PB_WIRE_LEN 2
#define PB_WIRE_SGROUP 3
#define PB_WIRE_EGROUP 4
#define PB_WIRE_I32 5

#define PB_MAX_VARINT_BYTES 10

/* Bounds the recursion a hostile message can force. A nested-message chain is
 * the one input that costs stack rather than heap, so it needs its own bound;
 * matches WREATH_JSON_MAX_DEPTH's reasoning. */
#define PB_MAX_DEPTH 100

/* The exception every refusal raises. Held for the process lifetime, exactly as
 * json.c holds the temporal types.
 *
 * Resolved lazily rather than only by protobuf_configure(), because the
 * configure call lives on the path where wreath.protobuf *selects* this module
 * -- and under WREATH_PURE=1 it selects the pure twin instead while a parity
 * test still imports this module directly. Depending on that call would mean
 * the same malformed buffer raised ProtobufDecodeError or a bare ValueError
 * depending on who imported what first, which is precisely the kind of
 * initialization-order difference a parity suite exists to catch. */
static PyObject *pb_decode_error = NULL;

static PyObject *
pb_error_type(void)
{
    if (pb_decode_error == NULL) {
        /* One import per process on an error path, not per value: the result is
         * cached in the static above. */
        PyObject *module = PyImport_ImportModule("wreath._pure.protobuf");
        if (module == NULL) {
            PyErr_Clear();
            return PyExc_ValueError;
        }
        pb_decode_error = PyObject_GetAttrString(module, "ProtobufDecodeError");
        Py_DECREF(module);
        if (pb_decode_error == NULL) {
            PyErr_Clear();
            return PyExc_ValueError;
        }
    }
    return pb_decode_error;
}

static void
pb_err(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    PyObject *message = PyUnicode_FromFormatV(fmt, args);
    va_end(args);
    if (message == NULL) {
        return; /* the formatting failure is already the raised exception */
    }
    PyErr_SetObject(pb_error_type(), message);
    Py_DECREF(message);
}

/* -- writer ---------------------------------------------------------------
 * Appends into a growable PyBytes so finishing a message is a shrinking resize
 * rather than an extra buffer copy -- the same strategy as json.c and msgpack.c.
 */
typedef struct {
    PyObject *bytes;
    char *buf;
    Py_ssize_t len;
    Py_ssize_t cap;
} PbWriter;

static int
pb_grow(PbWriter *w, Py_ssize_t need)
{
    Py_ssize_t cap = w->cap;
    /* Geometric, never additive: additive growth on a per-element append is one
     * of the patterns wreath-native-lint exists to catch. */
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
pb_reserve(PbWriter *w, Py_ssize_t need)
{
    return (w->cap - w->len >= need) ? 0 : pb_grow(w, need);
}

static inline int
pb_byte(PbWriter *w, unsigned char c)
{
    if (pb_reserve(w, 1) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)c;
    return 0;
}

static int
pb_write(PbWriter *w, const char *data, Py_ssize_t n)
{
    if (n == 0) {
        return 0;
    }
    if (pb_reserve(w, n) < 0) {
        return -1;
    }
    memcpy(w->buf + w->len, data, (size_t)n);
    w->len += n;
    return 0;
}

static int
pb_writer_init(PbWriter *w)
{
    w->bytes = PyBytes_FromStringAndSize(NULL, 256);
    if (w->bytes == NULL) {
        return -1;
    }
    w->buf = PyBytes_AS_STRING(w->bytes);
    w->len = 0;
    w->cap = 256;
    return 0;
}

static PyObject *
pb_writer_finish(PbWriter *w)
{
    if (_PyBytes_Resize(&w->bytes, w->len) < 0) {
        return NULL;
    }
    PyObject *out = w->bytes;
    w->bytes = NULL;
    return out;
}

static int
pb_put_varint(PbWriter *w, uint64_t value)
{
    while (value > 0x7FU) {
        if (pb_byte(w, (unsigned char)((value & 0x7FU) | 0x80U)) < 0) {
            return -1;
        }
        value >>= 7;
    }
    return pb_byte(w, (unsigned char)value);
}

static inline int
pb_put_tag(PbWriter *w, uint64_t number, int wire)
{
    return pb_put_varint(w, (number << 3) | (uint64_t)wire);
}

static int
pb_wire_type_for(int kind)
{
    switch (kind) {
    case PB_KIND_INT32:
    case PB_KIND_INT64:
    case PB_KIND_UINT32:
    case PB_KIND_UINT64:
    case PB_KIND_SINT32:
    case PB_KIND_SINT64:
    case PB_KIND_BOOL:
    case PB_KIND_ENUM:
        return PB_WIRE_VARINT;
    case PB_KIND_FIXED64:
    case PB_KIND_SFIXED64:
    case PB_KIND_DOUBLE:
        return PB_WIRE_I64;
    case PB_KIND_FIXED32:
    case PB_KIND_SFIXED32:
    case PB_KIND_FLOAT:
        return PB_WIRE_I32;
    default:
        return PB_WIRE_LEN;
    }
}

static int
pb_is_varint_kind(int kind)
{
    return pb_wire_type_for(kind) == PB_WIRE_VARINT;
}

/* Inclusive bounds per kind, mirroring _BOUNDS in the pure twin. Encoding a
 * value the declared kind cannot hold would silently truncate at the peer. */
static int
pb_bounds(int kind, int64_t *lo, uint64_t *hi, int *is_signed)
{
    switch (kind) {
    case PB_KIND_INT32:
    case PB_KIND_SFIXED32:
    case PB_KIND_SINT32:
    case PB_KIND_ENUM:
        *lo = INT32_MIN;
        *hi = INT32_MAX;
        *is_signed = 1;
        return 1;
    case PB_KIND_INT64:
    case PB_KIND_SFIXED64:
    case PB_KIND_SINT64:
        *lo = INT64_MIN;
        *hi = (uint64_t)INT64_MAX;
        *is_signed = 1;
        return 1;
    case PB_KIND_UINT32:
    case PB_KIND_FIXED32:
        *lo = 0;
        *hi = UINT32_MAX;
        *is_signed = 0;
        return 1;
    case PB_KIND_UINT64:
    case PB_KIND_FIXED64:
        *lo = 0;
        *hi = UINT64_MAX;
        *is_signed = 0;
        return 1;
    default:
        return 0;
    }
}

/* Reads a Python int for `kind`, range-checked. Returns the raw 64-bit two's
 * complement pattern in *out. */
static int
pb_read_int(PyObject *obj, int kind, uint64_t *out)
{
    int64_t lo = 0;
    uint64_t hi = 0;
    int is_signed = 0;
    int bounded = pb_bounds(kind, &lo, &hi, &is_signed);

    int overflow = 0;
    long long as_signed = PyLong_AsLongLongAndOverflow(obj, &overflow);
    if (as_signed == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (overflow == 0) {
        if (bounded) {
            if (as_signed < lo
                || (as_signed >= 0 && (uint64_t)as_signed > hi)) {
                PyErr_Format(PyExc_ValueError,
                             "%lld is out of range for the declared protobuf "
                             "kind",
                             as_signed);
                return -1;
            }
        }
        *out = (uint64_t)as_signed;
        return 0;
    }
    if (overflow < 0) {
        PyErr_SetString(PyExc_ValueError,
                        "value is out of range for the declared protobuf kind");
        return -1;
    }
    /* Positive overflow of int64: only uint64 kinds can hold it. */
    unsigned long long as_unsigned = PyLong_AsUnsignedLongLong(obj);
    if (as_unsigned == (unsigned long long)-1 && PyErr_Occurred()) {
        PyErr_Clear();
        PyErr_SetString(PyExc_ValueError,
                        "value is out of range for the declared protobuf kind");
        return -1;
    }
    if (bounded && (is_signed || (uint64_t)as_unsigned > hi)) {
        PyErr_SetString(PyExc_ValueError,
                        "value is out of range for the declared protobuf kind");
        return -1;
    }
    *out = (uint64_t)as_unsigned;
    return 0;
}

static uint64_t
pb_zigzag(uint64_t raw, int kind)
{
    if (kind == PB_KIND_SINT32) {
        int32_t v = (int32_t)raw;
        return (uint64_t)(uint32_t)((v << 1) ^ (v >> 31));
    }
    int64_t v = (int64_t)raw;
    return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63);
}

static int
pb_put_u32(PbWriter *w, uint32_t v)
{
    char bytes[4];
    bytes[0] = (char)(v & 0xFFU);
    bytes[1] = (char)((v >> 8) & 0xFFU);
    bytes[2] = (char)((v >> 16) & 0xFFU);
    bytes[3] = (char)((v >> 24) & 0xFFU);
    return pb_write(w, bytes, 4);
}

static int
pb_put_u64(PbWriter *w, uint64_t v)
{
    char bytes[8];
    for (int i = 0; i < 8; i++) {
        bytes[i] = (char)((v >> (8 * i)) & 0xFFU);
    }
    return pb_write(w, bytes, 8);
}

/* Appends one scalar body, no tag. Explicit byte assembly rather than a memcpy
 * of the host representation, so the wire stays little-endian on any host. */
static int
pb_put_scalar(PbWriter *w, int kind, PyObject *value)
{
    if (kind == PB_KIND_BOOL) {
        int truth = PyObject_IsTrue(value);
        if (truth < 0) {
            return -1;
        }
        return pb_byte(w, (unsigned char)(truth ? 1 : 0));
    }
    if (pb_is_varint_kind(kind)) {
        uint64_t raw = 0;
        if (pb_read_int(value, kind, &raw) < 0) {
            return -1;
        }
        if (kind == PB_KIND_SINT32 || kind == PB_KIND_SINT64) {
            raw = pb_zigzag(raw, kind);
        }
        return pb_put_varint(w, raw);
    }
    if (kind == PB_KIND_DOUBLE) {
        double d = PyFloat_AsDouble(value);
        if (d == -1.0 && PyErr_Occurred()) {
            return -1;
        }
        uint64_t bits;
        memcpy(&bits, &d, sizeof(bits));
        return pb_put_u64(w, bits);
    }
    if (kind == PB_KIND_FLOAT) {
        double d = PyFloat_AsDouble(value);
        if (d == -1.0 && PyErr_Occurred()) {
            return -1;
        }
        float f = (float)d;
        uint32_t bits;
        memcpy(&bits, &f, sizeof(bits));
        return pb_put_u32(w, bits);
    }
    if (kind == PB_KIND_FIXED64 || kind == PB_KIND_SFIXED64) {
        uint64_t raw = 0;
        if (pb_read_int(value, kind, &raw) < 0) {
            return -1;
        }
        return pb_put_u64(w, raw);
    }
    if (kind == PB_KIND_FIXED32 || kind == PB_KIND_SFIXED32) {
        uint64_t raw = 0;
        if (pb_read_int(value, kind, &raw) < 0) {
            return -1;
        }
        return pb_put_u32(w, (uint32_t)raw);
    }
    PyErr_Format(PyExc_ValueError, "protobuf kind %d is not a scalar", kind);
    return -1;
}

/* proto3 implicit presence: a field holding its type's zero is not written. */
static int
pb_is_default(int kind, PyObject *value)
{
    if (kind == PB_KIND_BOOL) {
        int truth = PyObject_IsTrue(value);
        return truth < 0 ? -1 : (truth == 0);
    }
    if (kind == PB_KIND_STRING) {
        if (!PyUnicode_Check(value)) {
            return 0;
        }
        return PyUnicode_GET_LENGTH(value) == 0;
    }
    if (kind == PB_KIND_BYTES) {
        Py_ssize_t n = PyObject_Length(value);
        return n < 0 ? -1 : (n == 0);
    }
    if (kind == PB_KIND_DOUBLE || kind == PB_KIND_FLOAT) {
        double d = PyFloat_AsDouble(value);
        if (d == -1.0 && PyErr_Occurred()) {
            return -1;
        }
        /* -0.0 is *not* default: its bit pattern differs and round-tripping it
         * as 0.0 would lose the sign, which the pure twin also refuses to do. */
        return (d == 0.0 && !signbit(d));
    }
    /* Everything reaching here is an integer kind (bool, string, bytes and the
     * floats were handled above), and for an int -- including an IntEnum member
     * -- falsy is exactly zero. */
    int truth = PyObject_IsTrue(value);
    return truth < 0 ? -1 : (truth == 0);
}

static int pb_encode_values(PbWriter *w, PyObject *plan, PyObject *values,
                            PyObject *unknown, int depth);

static int
pb_encode_single(PbWriter *w, uint64_t number, int kind, PyObject *value)
{
    if (kind == PB_KIND_STRING) {
        Py_ssize_t size = 0;
        const char *utf8 = PyUnicode_AsUTF8AndSize(value, &size);
        if (utf8 == NULL) {
            return -1;
        }
        if (pb_put_tag(w, number, PB_WIRE_LEN) < 0
            || pb_put_varint(w, (uint64_t)size) < 0
            || pb_write(w, utf8, size) < 0) {
            return -1;
        }
        return 0;
    }
    if (kind == PB_KIND_BYTES) {
        Py_buffer view;
        if (PyObject_GetBuffer(value, &view, PyBUF_SIMPLE) < 0) {
            return -1;
        }
        int failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                     || pb_put_varint(w, (uint64_t)view.len) < 0
                     || pb_write(w, (const char *)view.buf, view.len) < 0;
        PyBuffer_Release(&view);
        return failed ? -1 : 0;
    }
    if (pb_put_tag(w, number, pb_wire_type_for(kind)) < 0) {
        return -1;
    }
    return pb_put_scalar(w, kind, value);
}

/* Encodes a nested message (a `(values, unknown)` pair) as a length-delimited
 * body. The body is built into its own writer because its length has to precede
 * it and is not known until it is complete. */
static int
pb_encode_delimited(PbWriter *w, PyObject *subplan, PyObject *pair, int depth)
{
    if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) {
        PyErr_SetString(PyExc_TypeError,
                        "a nested protobuf message must be a (values, unknown) pair");
        return -1;
    }
    PbWriter body;
    if (pb_writer_init(&body) < 0) {
        return -1;
    }
    if (pb_encode_values(&body, subplan, PyTuple_GET_ITEM(pair, 0),
                         PyTuple_GET_ITEM(pair, 1), depth + 1)
        < 0) {
        Py_XDECREF(body.bytes);
        return -1;
    }
    PyObject *encoded = pb_writer_finish(&body);
    if (encoded == NULL) {
        return -1;
    }
    int failed = pb_put_varint(w, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
                 || pb_write(w, PyBytes_AS_STRING(encoded),
                             PyBytes_GET_SIZE(encoded))
                        < 0;
    Py_DECREF(encoded);
    return failed ? -1 : 0;
}

static int
pb_encode_repeated(PbWriter *w, uint64_t number, int kind, int flags,
                   PyObject *subplan, PyObject *items, int depth)
{
    Py_ssize_t count = PySequence_Size(items);
    if (count < 0) {
        return -1;
    }
    if (count == 0) {
        return 0;
    }
    /* One sequence-protocol call for the whole field, then a contiguous array.
     * `PySequence_GetItem` per element is an out-of-line call through `sq_item`
     * carrying a negative-index fixup and a bounds check; a repeated field is
     * already a list, whose elements are a C array behind that call.
     *
     * `PySequence_Size` above is kept, and runs first, so an input with no
     * length is still refused rather than silently materialised here.
     *
     * The strong reference per item is also kept. Every branch below can
     * re-enter Python -- a nested message reads attributes, a scalar may run
     * `__index__` -- and a callback that cleared the list would otherwise free
     * an item mid-encode. The length is re-read each step for the same reason:
     * a list that shrinks under us must stop the loop rather than read off the
     * end, which is the guarantee `PySequence_GetItem` was providing. */
    PyObject *fast = PySequence_Fast(items, "repeated field must be a sequence");
    if (fast == NULL) {
        return -1;
    }
    int rc = -1;
#define PB_ITEM()                                                             \
    (i < PySequence_Fast_GET_SIZE(fast)                                       \
         ? Py_NewRef(PySequence_Fast_GET_ITEM(fast, i))                       \
         : NULL)

    if (kind == PB_KIND_MESSAGE) {
        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *item = PB_ITEM();
            if (item == NULL) {
                break;
            }
            int failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                         || pb_encode_delimited(w, subplan, item, depth) < 0;
            Py_DECREF(item);
            if (failed) {
                goto done;
            }
        }
        rc = 0;
        goto done;
    }
    if ((flags & PB_FLAG_PACKED) && kind != PB_KIND_STRING
        && kind != PB_KIND_BYTES) {
        PbWriter body;
        if (pb_writer_init(&body) < 0) {
            goto done;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *item = PB_ITEM();
            if (item == NULL) {
                break;
            }
            int failed = pb_put_scalar(&body, kind, item) < 0;
            Py_DECREF(item);
            if (failed) {
                Py_XDECREF(body.bytes);
                goto done;
            }
        }
        PyObject *encoded = pb_writer_finish(&body);
        if (encoded == NULL) {
            goto done;
        }
        int failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                     || pb_put_varint(w, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
                     || pb_write(w, PyBytes_AS_STRING(encoded),
                                 PyBytes_GET_SIZE(encoded))
                            < 0;
        Py_DECREF(encoded);
        rc = failed ? -1 : 0;
        goto done;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *item = PB_ITEM();
        if (item == NULL) {
            break;
        }
        int failed = pb_encode_single(w, number, kind, item) < 0;
        Py_DECREF(item);
        if (failed) {
            goto done;
        }
    }
    rc = 0;
done:
    Py_DECREF(fast);
    return rc;
#undef PB_ITEM
}

/* A map field is repeated messages of {1: key, 2: value}; encoding it in that
 * sugar-free form is what makes a wreath map wire-identical to a declared
 * `map<k, v>`. */
static int
pb_encode_map(PbWriter *w, uint64_t number, PyObject *subplan, PyObject *mapping,
              int depth)
{
    PyObject *key_row = PyTuple_GET_ITEM(subplan, 0);
    PyObject *value_row = PyTuple_GET_ITEM(subplan, 1);
    int key_kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(key_row, 1));
    int value_kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(value_row, 1));
    if (PyErr_Occurred()) {
        return -1;
    }
    PyObject *value_sub = PyTuple_GET_ITEM(value_row, 3);

    PyObject *items = PyMapping_Items(mapping);
    if (items == NULL) {
        return -1;
    }
    Py_ssize_t count = PyList_GET_SIZE(items);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);

        PbWriter entry;
        if (pb_writer_init(&entry) < 0) {
            Py_DECREF(items);
            return -1;
        }
        int failed = 0;
        int key_default = pb_is_default(key_kind, key);
        if (key_default < 0) {
            failed = 1;
        }
        else if (!key_default) {
            failed = pb_encode_single(&entry, 1, key_kind, key) < 0;
        }
        if (!failed) {
            if (value_kind == PB_KIND_MESSAGE) {
                failed = pb_put_tag(&entry, 2, PB_WIRE_LEN) < 0
                         || pb_encode_delimited(&entry, value_sub, value, depth) < 0;
            }
            else {
                int value_default = pb_is_default(value_kind, value);
                if (value_default < 0) {
                    failed = 1;
                }
                else if (!value_default) {
                    failed = pb_encode_single(&entry, 2, value_kind, value) < 0;
                }
            }
        }
        if (failed) {
            Py_XDECREF(entry.bytes);
            Py_DECREF(items);
            return -1;
        }
        PyObject *encoded = pb_writer_finish(&entry);
        if (encoded == NULL) {
            Py_DECREF(items);
            return -1;
        }
        failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                 || pb_put_varint(w, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
                 || pb_write(w, PyBytes_AS_STRING(encoded),
                             PyBytes_GET_SIZE(encoded))
                        < 0;
        Py_DECREF(encoded);
        if (failed) {
            Py_DECREF(items);
            return -1;
        }
    }
    Py_DECREF(items);
    return 0;
}

static int
pb_encode_values(PbWriter *w, PyObject *plan, PyObject *values, PyObject *unknown,
                 int depth)
{
    if (depth > PB_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError,
                        "protobuf message nesting exceeds the depth limit");
        return -1;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(plan);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *row = PyTuple_GET_ITEM(plan, i);
        uint64_t number = (uint64_t)PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row, 0));
        int kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 1));
        int flags = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 2));
        if (PyErr_Occurred()) {
            return -1;
        }
        PyObject *subplan = PyTuple_GET_ITEM(row, 3);
        PyObject *value = PyList_GET_ITEM(values, i);

        if (value == Py_None) {
            continue;
        }
        if (flags & PB_FLAG_MAP) {
            if (pb_encode_map(w, number, subplan, value, depth) < 0) {
                return -1;
            }
        }
        else if (flags & PB_FLAG_REPEATED) {
            if (pb_encode_repeated(w, number, kind, flags, subplan, value, depth) < 0) {
                return -1;
            }
        }
        else if (kind == PB_KIND_MESSAGE) {
            if (pb_put_tag(w, number, PB_WIRE_LEN) < 0
                || pb_encode_delimited(w, subplan, value, depth) < 0) {
                return -1;
            }
        }
        else {
            int emit = 1;
            if (!(flags & PB_FLAG_OPTIONAL)) {
                int is_default = pb_is_default(kind, value);
                if (is_default < 0) {
                    return -1;
                }
                emit = !is_default;
            }
            if (emit && pb_encode_single(w, number, kind, value) < 0) {
                return -1;
            }
        }
    }
    if (unknown != NULL && unknown != Py_None) {
        Py_buffer view;
        if (PyObject_GetBuffer(unknown, &view, PyBUF_SIMPLE) < 0) {
            return -1;
        }
        int failed = pb_write(w, (const char *)view.buf, view.len) < 0;
        PyBuffer_Release(&view);
        if (failed) {
            return -1;
        }
    }
    return 0;
}

/* -- reader --------------------------------------------------------------- */

typedef struct {
    const uint8_t *data;
    Py_ssize_t len;
    Py_ssize_t pos;
} PbReader;

static int
pb_get_varint(PbReader *r, uint64_t *out)
{
    uint64_t result = 0;
    unsigned shift = 0;
    Py_ssize_t start = r->pos;
    for (;;) {
        if (r->pos >= r->len) {
            pb_err("buffer ends inside a varint that began at offset %zd", start);
            return -1;
        }
        if (r->pos - start >= PB_MAX_VARINT_BYTES) {
            pb_err("varint at offset %zd exceeds %d bytes", start,
                   PB_MAX_VARINT_BYTES);
            return -1;
        }
        uint8_t byte = r->data[r->pos++];
        result |= (uint64_t)(byte & 0x7FU) << shift;
        if (!(byte & 0x80U)) {
            *out = result;
            return 0;
        }
        shift += 7;
    }
}

static int
pb_take(PbReader *r, Py_ssize_t count, const char *what, const uint8_t **out)
{
    if (count < 0 || count > r->len - r->pos) {
        pb_err("%s at offset %zd needs %zd bytes, but only %zd remain", what,
               r->pos, count, r->len - r->pos);
        return -1;
    }
    *out = r->data + r->pos;
    r->pos += count;
    return 0;
}

static PyObject *
pb_signed_object(int kind, uint64_t raw)
{
    switch (kind) {
    case PB_KIND_SINT32:
    case PB_KIND_SINT64:
        return PyLong_FromLongLong((long long)((raw >> 1) ^ (~(raw & 1) + 1)));
    case PB_KIND_INT32:
    case PB_KIND_ENUM: {
        /* Sign-extended to 64 bits on the wire; narrow it back to 32. */
        uint32_t narrowed = (uint32_t)raw;
        return PyLong_FromLong((long)(int32_t)narrowed);
    }
    case PB_KIND_INT64:
        return PyLong_FromLongLong((long long)raw);
    case PB_KIND_UINT32:
        return PyLong_FromUnsignedLong((unsigned long)(uint32_t)raw);
    default:
        return PyLong_FromUnsignedLongLong((unsigned long long)raw);
    }
}

static PyObject *
pb_read_scalar(PbReader *r, int kind)
{
    if (pb_is_varint_kind(kind)) {
        uint64_t raw = 0;
        if (pb_get_varint(r, &raw) < 0) {
            return NULL;
        }
        if (kind == PB_KIND_BOOL) {
            return PyBool_FromLong(raw != 0);
        }
        return pb_signed_object(kind, raw);
    }
    if (pb_wire_type_for(kind) == PB_WIRE_I64) {
        const uint8_t *chunk = NULL;
        if (pb_take(r, 8, "a 64-bit field", &chunk) < 0) {
            return NULL;
        }
        uint64_t bits = 0;
        for (int i = 0; i < 8; i++) {
            bits |= (uint64_t)chunk[i] << (8 * i);
        }
        if (kind == PB_KIND_DOUBLE) {
            double d;
            memcpy(&d, &bits, sizeof(d));
            return PyFloat_FromDouble(d);
        }
        if (kind == PB_KIND_FIXED64) {
            return PyLong_FromUnsignedLongLong((unsigned long long)bits);
        }
        return PyLong_FromLongLong((long long)bits);
    }
    const uint8_t *chunk = NULL;
    if (pb_take(r, 4, "a 32-bit field", &chunk) < 0) {
        return NULL;
    }
    uint32_t bits = 0;
    for (int i = 0; i < 4; i++) {
        bits |= (uint32_t)chunk[i] << (8 * i);
    }
    if (kind == PB_KIND_FLOAT) {
        float f;
        memcpy(&f, &bits, sizeof(f));
        return PyFloat_FromDouble((double)f);
    }
    if (kind == PB_KIND_FIXED32) {
        return PyLong_FromUnsignedLong((unsigned long)bits);
    }
    return PyLong_FromLong((long)(int32_t)bits);
}

static int
pb_skip(PbReader *r, int wire)
{
    const uint8_t *ignored = NULL;
    if (wire == PB_WIRE_VARINT) {
        uint64_t raw = 0;
        return pb_get_varint(r, &raw);
    }
    if (wire == PB_WIRE_I64) {
        return pb_take(r, 8, "an unknown 64-bit field", &ignored);
    }
    if (wire == PB_WIRE_I32) {
        return pb_take(r, 4, "an unknown 32-bit field", &ignored);
    }
    uint64_t length = 0;
    if (pb_get_varint(r, &length) < 0) {
        return -1;
    }
    if (length > (uint64_t)PY_SSIZE_T_MAX) {
        pb_err("an unknown length-delimited field declares an impossible length");
        return -1;
    }
    return pb_take(r, (Py_ssize_t)length, "an unknown length-delimited field",
                   &ignored);
}

static PyObject *pb_decode_values(PyObject *plan, const uint8_t *data,
                                  Py_ssize_t len, int depth);

static PyObject *
pb_defaults(PyObject *plan)
{
    Py_ssize_t count = PyTuple_GET_SIZE(plan);
    PyObject *values = PyList_New(count);
    if (values == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *row = PyTuple_GET_ITEM(plan, i);
        int kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 1));
        int flags = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 2));
        if (PyErr_Occurred()) {
            Py_DECREF(values);
            return NULL;
        }
        PyObject *item = NULL;
        if (flags & PB_FLAG_MAP) {
            item = PyDict_New();
        }
        else if (flags & PB_FLAG_REPEATED) {
            item = PyList_New(0);
        }
        else if ((flags & PB_FLAG_OPTIONAL) || kind == PB_KIND_MESSAGE) {
            item = Py_NewRef(Py_None);
        }
        else if (kind == PB_KIND_STRING) {
            item = PyUnicode_FromStringAndSize("", 0);
        }
        else if (kind == PB_KIND_BYTES) {
            item = PyBytes_FromStringAndSize("", 0);
        }
        else if (kind == PB_KIND_BOOL) {
            item = Py_NewRef(Py_False);
        }
        else if (kind == PB_KIND_DOUBLE || kind == PB_KIND_FLOAT) {
            item = PyFloat_FromDouble(0.0);
        }
        else {
            item = PyLong_FromLong(0);
        }
        if (item == NULL) {
            Py_DECREF(values);
            return NULL;
        }
        PyList_SET_ITEM(values, i, item);
    }
    return values;
}

/* Maps a field number to its plan index. Linear over the plan because a message
 * has a handful of fields and the scan stays in cache; a dict would cost more to
 * build per message than it saves. */
static Py_ssize_t
pb_find(PyObject *plan, uint64_t number)
{
    Py_ssize_t count = PyTuple_GET_SIZE(plan);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *row = PyTuple_GET_ITEM(plan, i);
        unsigned long long candidate =
            PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row, 0));
        if (candidate == (unsigned long long)number) {
            return i;
        }
    }
    PyErr_Clear();
    return -1;
}

static int
pb_read_len(PbReader *r, const uint8_t **out, Py_ssize_t *out_len)
{
    uint64_t length = 0;
    if (pb_get_varint(r, &length) < 0) {
        return -1;
    }
    if (length > (uint64_t)PY_SSIZE_T_MAX) {
        pb_err("a length-delimited field declares an impossible length");
        return -1;
    }
    if (pb_take(r, (Py_ssize_t)length, "a length-delimited field", out) < 0) {
        return -1;
    }
    *out_len = (Py_ssize_t)length;
    return 0;
}

static int
pb_store(PyObject *values, Py_ssize_t index, int flags, PyObject *item)
{
    if (item == NULL) {
        return -1;
    }
    if (flags & PB_FLAG_REPEATED) {
        int failed = PyList_Append(PyList_GET_ITEM(values, index), item) < 0;
        Py_DECREF(item);
        return failed ? -1 : 0;
    }
    /* Steals `item`, replacing the default this slot was seeded with. The status
     * is checked rather than assumed: an out-of-range index would otherwise
     * return success with an IndexError already set. */
    if (PyList_SetItem(values, index, item) < 0) {
        return -1;
    }
    return 0;
}

static int
pb_read_field(PbReader *r, PyObject *row, PyObject *values, Py_ssize_t index,
              int wire, int depth)
{
    int kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 1));
    int flags = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 2));
    if (PyErr_Occurred()) {
        return -1;
    }
    PyObject *subplan = PyTuple_GET_ITEM(row, 3);

    if (flags & PB_FLAG_MAP) {
        const uint8_t *body = NULL;
        Py_ssize_t body_len = 0;
        if (pb_read_len(r, &body, &body_len) < 0) {
            return -1;
        }
        PyObject *pair = pb_decode_values(subplan, body, body_len, depth + 1);
        if (pair == NULL) {
            return -1;
        }
        PyObject *entries = PyTuple_GET_ITEM(pair, 0);
        int failed = PyDict_SetItem(PyList_GET_ITEM(values, index),
                                    PyList_GET_ITEM(entries, 0),
                                    PyList_GET_ITEM(entries, 1))
                     < 0;
        Py_DECREF(pair);
        return failed ? -1 : 0;
    }
    if (kind == PB_KIND_MESSAGE) {
        const uint8_t *body = NULL;
        Py_ssize_t body_len = 0;
        if (pb_read_len(r, &body, &body_len) < 0) {
            return -1;
        }
        PyObject *nested = pb_decode_values(subplan, body, body_len, depth + 1);
        return pb_store(values, index, flags, nested);
    }
    if (kind == PB_KIND_STRING || kind == PB_KIND_BYTES) {
        const uint8_t *body = NULL;
        Py_ssize_t body_len = 0;
        if (pb_read_len(r, &body, &body_len) < 0) {
            return -1;
        }
        PyObject *item = NULL;
        if (kind == PB_KIND_STRING) {
            item = PyUnicode_DecodeUTF8((const char *)body, body_len, NULL);
            if (item == NULL) {
                PyErr_Clear();
                pb_err("a string field carried bytes that are not UTF-8");
                return -1;
            }
        }
        else {
            item = PyBytes_FromStringAndSize((const char *)body, body_len);
        }
        return pb_store(values, index, flags, item);
    }
    if ((flags & PB_FLAG_REPEATED) && wire == PB_WIRE_LEN) {
        /* A packed body, whatever this build declared: proto3 requires a parser
         * to accept both representations of a repeated scalar. */
        const uint8_t *body = NULL;
        Py_ssize_t body_len = 0;
        if (pb_read_len(r, &body, &body_len) < 0) {
            return -1;
        }
        PbReader inner = {body, body_len, 0};
        PyObject *target = PyList_GET_ITEM(values, index);
        while (inner.pos < inner.len) {
            PyObject *item = pb_read_scalar(&inner, kind);
            if (item == NULL) {
                return -1;
            }
            int failed = PyList_Append(target, item) < 0;
            Py_DECREF(item);
            if (failed) {
                return -1;
            }
        }
        return 0;
    }
    return pb_store(values, index, flags, pb_read_scalar(r, kind));
}

static PyObject *
pb_decode_values(PyObject *plan, const uint8_t *data, Py_ssize_t len, int depth)
{
    if (depth > PB_MAX_DEPTH) {
        pb_err("protobuf message nesting exceeds the depth limit");
        return NULL;
    }
    PyObject *values = pb_defaults(plan);
    if (values == NULL) {
        return NULL;
    }
    PbWriter unknown;
    if (pb_writer_init(&unknown) < 0) {
        Py_DECREF(values);
        return NULL;
    }

    PbReader r = {data, len, 0};
    while (r.pos < r.len) {
        Py_ssize_t tag_start = r.pos;
        uint64_t tag = 0;
        if (pb_get_varint(&r, &tag) < 0) {
            goto error;
        }
        uint64_t number = tag >> 3;
        int wire = (int)(tag & 0x07U);
        if (number == 0) {
            pb_err("field number 0 at offset %zd", tag_start);
            goto error;
        }
        if (wire == PB_WIRE_SGROUP || wire == PB_WIRE_EGROUP) {
            pb_err("group wire type %d at offset %zd: groups are deprecated and "
                   "this codec refuses them rather than guessing",
                   wire, tag_start);
            goto error;
        }
        if (wire > PB_WIRE_I32) {
            pb_err("unknown wire type %d at offset %zd", wire, tag_start);
            goto error;
        }
        Py_ssize_t index = pb_find(plan, number);
        if (index < 0) {
            if (pb_skip(&r, wire) < 0) {
                goto error;
            }
            if (pb_write(&unknown, (const char *)(data + tag_start),
                         r.pos - tag_start)
                < 0) {
                goto error;
            }
            continue;
        }
        if (pb_read_field(&r, PyTuple_GET_ITEM(plan, index), values, index, wire,
                          depth)
            < 0) {
            goto error;
        }
    }

    PyObject *unknown_bytes = pb_writer_finish(&unknown);
    if (unknown_bytes == NULL) {
        Py_DECREF(values);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(2, values, unknown_bytes);
    Py_DECREF(values);
    Py_DECREF(unknown_bytes);
    return result;

error:
    Py_XDECREF(unknown.bytes);
    Py_DECREF(values);
    return NULL;
}

/* -- module surface ------------------------------------------------------- */

PyObject *
wreath_protobuf_configure(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_XSETREF(pb_decode_error, Py_NewRef(arg));
    Py_RETURN_NONE;
}

PyObject *
wreath_protobuf_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan = NULL;
    PyObject *values = NULL;
    PyObject *unknown = NULL;
    if (!PyArg_ParseTuple(args, "O!O!|O", &PyTuple_Type, &plan, &PyList_Type,
                          &values, &unknown)) {
        return NULL;
    }
    if (PyTuple_GET_SIZE(plan) != PyList_GET_SIZE(values)) {
        PyErr_SetString(PyExc_ValueError,
                        "protobuf plan and value list differ in length");
        return NULL;
    }
    PbWriter w;
    if (pb_writer_init(&w) < 0) {
        return NULL;
    }
    if (pb_encode_values(&w, plan, values, unknown, 0) < 0) {
        Py_XDECREF(w.bytes);
        return NULL;
    }
    return pb_writer_finish(&w);
}

PyObject *
wreath_protobuf_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan = NULL;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "O!y*", &PyTuple_Type, &plan, &view)) {
        return NULL;
    }
    PyObject *result =
        pb_decode_values(plan, (const uint8_t *)view.buf, view.len, 0);
    PyBuffer_Release(&view);
    return result;
}
