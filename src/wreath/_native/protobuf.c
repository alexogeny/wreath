/* Protocol Buffers wire codec.
 *
 * Held byte-for-byte to the protobuf wire specification:
 * tests/test_protobuf_parity.py writes the encoding rules out -- tag byte,
 * varint, zigzag, little-endian fixed, length prefix -- and asserts against
 * them, and tests/test_protobuf.py pins hand-computed vectors.
 *
 * A plan compiled by wreath/protobuf.py at class creation fixes the wire shape:
 *
 *   plan row  (number, kind, flags, subplan|None)
 *
 * The public entry points consume and produce declared message objects. Decode
 * currently uses the lower-level values tree before constructing those
 * objects; a measured attempt to fuse only those two traversals saved 3%, so
 * the remaining useful boundary is a compiled native descriptor/object shape,
 * not a second decoder beside this authoritative wire parser.
 *
 * Decoding choices the specification fixes:
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
#include "bytes_writer.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

/* Kind codes. These are the contract with wreath/_protobuf_plan.py, which is
 * where the declaration compiler and both codecs read the same numbering from,
 * so a change here is a change there. */
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
#define PB_DESCRIPTOR_NAME "wreath.protobuf.descriptor"

typedef struct {
    uint64_t number;
    int kind;
    int flags;
    int map_key_kind;
    int map_value_kind;
    PyObject *name;
    PyObject *camel;
    PyObject *holder;
    PyObject *subplan;
    PyObject *nested_descriptor;
} PbCompiledField;

typedef struct {
    uint64_t number;
    Py_ssize_t index;
} PbFieldLookup;

typedef struct {
    Py_ssize_t count;
    Py_ssize_t *members;
} PbOneof;

typedef struct {
    Py_ssize_t count;
    PbCompiledField *fields;
    PbFieldLookup *lookup;
    PyObject *names;
    PyObject *unknown_name;
    Py_ssize_t oneof_count;
    PbOneof *oneofs;
} PbDescriptor;

static PbDescriptor *pb_descriptor_from_object(PyObject *object);
static PyObject *pb_camel_name(PyObject *name);
static int pb_encode_compiled_object(WreathBytesWriter *writer,
                                     PyObject *message,
                                     PbDescriptor *descriptor, int depth);

static PyObject *
pb_error_type(void)
{
    PyObject *module = PyImport_ImportModule("wreath._protobuf_plan");
    PyObject *error_type;
    if (module == NULL) {
        PyErr_Clear();
        return Py_NewRef(PyExc_ValueError);
    }
    error_type = PyObject_GetAttrString(module, "ProtobufDecodeError");
    Py_DECREF(module);
    if (error_type == NULL) {
        PyErr_Clear();
        return Py_NewRef(PyExc_ValueError);
    }
    return error_type;
}

static void
pb_err(const char *fmt, ...)
{
    va_list args;
    PyObject *error_type;
    va_start(args, fmt);
    PyObject *message = PyUnicode_FromFormatV(fmt, args);
    va_end(args);
    if (message == NULL) {
        return; /* the formatting failure is already the raised exception */
    }
    error_type = pb_error_type();
    if (error_type != NULL) {
        PyErr_SetObject(error_type, message);
        Py_DECREF(error_type);
    }
    Py_DECREF(message);
}

/* -- writer ---------------------------------------------------------------
 * Appends into a growable PyBytes so finishing a message is a shrinking resize
 * rather than an extra buffer copy -- the same strategy as json.c and msgpack.c.
 */
static inline int
pb_byte(WreathBytesWriter *w, unsigned char c)
{
    if (wreath_writer_reserve(w, 1) < 0) {
        return -1;
    }
    w->buf[w->len++] = (char)c;
    return 0;
}

static int
pb_write(WreathBytesWriter *w, const char *data, Py_ssize_t n)
{
    if (n == 0) {
        return 0;
    }
    if (wreath_writer_reserve(w, n) < 0) {
        return -1;
    }
    memcpy(w->buf + w->len, data, (size_t)n);
    w->len += n;
    return 0;
}

static int
pb_put_varint(WreathBytesWriter *w, uint64_t value)
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
pb_put_tag(WreathBytesWriter *w, uint64_t number, int wire)
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

/* Inclusive bounds per kind, from the declared field widths. Encoding a
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

/* Reserve first, then let the shared store helper write straight into the
 * writer's buffer. Staging into a local `uint8_t bytes[n]` and handing that to
 * pb_write costs twice: the memcpy, and -- because a local array's address is
 * passed out of the frame -- gcc's -fstack-protector-strong heuristic fires on
 * whatever pb_put_scalar inlines this into, adding a %fs:0x28 load, a canary
 * spill and a re-check per call. Encoding 20k packed doubles measured 188.9us
 * with the staging arrays and 156.2us without -- 12 interleaved rounds, fresh
 * process each, min over rounds, against an A/A floor of 0.05%. Decode is
 * untouched (284.0us both ways). */
static int
pb_put_u32(WreathBytesWriter *w, uint32_t v)
{
    if (wreath_writer_reserve(w, 4) < 0) {
        return -1;
    }
    wreath_store_u32_le((uint8_t *)w->buf + w->len, v);
    w->len += 4;
    return 0;
}

static int
pb_put_u64(WreathBytesWriter *w, uint64_t v)
{
    if (wreath_writer_reserve(w, 8) < 0) {
        return -1;
    }
    wreath_store_u64_le((uint8_t *)w->buf + w->len, v);
    w->len += 8;
    return 0;
}

/* Appends one scalar body, no tag. Explicit byte assembly rather than a memcpy
 * of the host representation, so the wire stays little-endian on any host. */
static int
pb_put_scalar(WreathBytesWriter *w, int kind, PyObject *value)
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
         * as 0.0 would lose the sign. */
        return (d == 0.0 && !signbit(d));
    }
    /* Everything reaching here is an integer kind (bool, string, bytes and the
     * floats were handled above), and for an int -- including an IntEnum member
     * -- falsy is exactly zero. */
    int truth = PyObject_IsTrue(value);
    return truth < 0 ? -1 : (truth == 0);
}

static int pb_encode_values(WreathBytesWriter *w, PyObject *plan, PyObject *values,
                            PyObject *unknown, int depth);

static int
pb_encode_single(WreathBytesWriter *w, uint64_t number, int kind, PyObject *value)
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
pb_encode_delimited(WreathBytesWriter *w, PyObject *subplan, PyObject *pair, int depth)
{
    if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) {
        PyErr_SetString(PyExc_TypeError,
                        "a nested protobuf message must be a (values, unknown) pair");
        return -1;
    }
    WreathBytesWriter body;
    if (wreath_writer_init(&body, 256) < 0) {
        return -1;
    }
    if (pb_encode_values(&body, subplan, PyTuple_GET_ITEM(pair, 0),
                         PyTuple_GET_ITEM(pair, 1), depth + 1)
        < 0) {
        Py_XDECREF(body.bytes);
        return -1;
    }
    PyObject *encoded = wreath_writer_finish(&body);
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
pb_encode_repeated(WreathBytesWriter *w, uint64_t number, int kind, int flags,
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
        WreathBytesWriter body;
        if (wreath_writer_init(&body, 256) < 0) {
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
        PyObject *encoded = wreath_writer_finish(&body);
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
pb_encode_map(WreathBytesWriter *w, uint64_t number, PyObject *subplan, PyObject *mapping,
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

        WreathBytesWriter entry;
        if (wreath_writer_init(&entry, 256) < 0) {
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
        PyObject *encoded = wreath_writer_finish(&entry);
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
pb_encode_values(WreathBytesWriter *w, PyObject *plan, PyObject *values, PyObject *unknown,
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

static int pb_encode_object(WreathBytesWriter *w, PyObject *message, int depth);

static int
pb_encode_object_delimited(WreathBytesWriter *w, PyObject *message, int depth)
{
    WreathBytesWriter body;
    PyObject *encoded;
    int failed;

    if (wreath_writer_init(&body, 256) < 0) return -1;
    if (pb_encode_object(&body, message, depth + 1) < 0) {
        Py_XDECREF(body.bytes);
        return -1;
    }
    encoded = wreath_writer_finish(&body);
    if (encoded == NULL) return -1;
    failed = pb_put_varint(w, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
             || pb_write(w, PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded)) < 0;
    Py_DECREF(encoded);
    return failed ? -1 : 0;
}

static int
pb_encode_object_map(WreathBytesWriter *w, uint64_t number, PyObject *subplan,
                     PyObject *mapping, int depth)
{
    PyObject *key_row = PyTuple_GET_ITEM(subplan, 0);
    PyObject *value_row = PyTuple_GET_ITEM(subplan, 1);
    int key_kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(key_row, 1));
    int value_kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(value_row, 1));
    PyObject *items;

    if (PyErr_Occurred()) return -1;
    items = PyMapping_Items(mapping);
    if (items == NULL) return -1;
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(items); i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        WreathBytesWriter entry;
        PyObject *encoded;
        int failed = 0;
        int is_default;

        if (wreath_writer_init(&entry, 256) < 0) {
            Py_DECREF(items);
            return -1;
        }
        is_default = pb_is_default(key_kind, key);
        if (is_default < 0) failed = 1;
        else if (!is_default) failed = pb_encode_single(&entry, 1, key_kind, key) < 0;
        if (!failed && value_kind == PB_KIND_MESSAGE) {
            failed = pb_put_tag(&entry, 2, PB_WIRE_LEN) < 0
                     || pb_encode_object_delimited(&entry, value, depth) < 0;
        }
        else if (!failed) {
            is_default = pb_is_default(value_kind, value);
            if (is_default < 0) failed = 1;
            else if (!is_default) {
                failed = pb_encode_single(&entry, 2, value_kind, value) < 0;
            }
        }
        if (failed) {
            Py_XDECREF(entry.bytes);
            Py_DECREF(items);
            return -1;
        }
        encoded = wreath_writer_finish(&entry);
        if (encoded == NULL) {
            Py_DECREF(items);
            return -1;
        }
        failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                 || pb_put_varint(w, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
                 || pb_write(w, PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded)) < 0;
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
pb_encode_object_repeated(WreathBytesWriter *w, uint64_t number, int kind, int flags,
                          PyObject *subplan, PyObject *items, int depth)
{
    PyObject *fast;
    Py_ssize_t count;

    if (kind != PB_KIND_MESSAGE) {
        return pb_encode_repeated(w, number, kind, flags, subplan, items, depth);
    }
    count = PySequence_Size(items);
    if (count < 0) return -1;
    fast = PySequence_Fast(items, "repeated field must be a sequence");
    if (fast == NULL) return -1;
    for (Py_ssize_t i = 0; i < count && i < PySequence_Fast_GET_SIZE(fast); i++) {
        PyObject *item = Py_NewRef(PySequence_Fast_GET_ITEM(fast, i));
        int failed = pb_put_tag(w, number, PB_WIRE_LEN) < 0
                     || pb_encode_object_delimited(w, item, depth) < 0;
        Py_DECREF(item);
        if (failed) {
            Py_DECREF(fast);
            return -1;
        }
    }
    Py_DECREF(fast);
    return 0;
}

static int
pb_encode_compiled_object(WreathBytesWriter *w, PyObject *message,
                          PbDescriptor *descriptor, int depth)
{
    PyObject *unknown = NULL;

    if (depth > PB_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError,
                        "protobuf message nesting exceeds the depth limit");
        return -1;
    }
    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        PbCompiledField *field = &descriptor->fields[index];
        PyObject *value = PyObject_GetAttr(message, field->name);
        int failed = 0;
        if (value == NULL) return -1;
        if (value == Py_None) {
            Py_DECREF(value);
            continue;
        }
        if (field->flags & PB_FLAG_MAP) {
            failed = pb_encode_object_map(
                w, field->number, field->subplan, value, depth) < 0;
        }
        else if (field->flags & PB_FLAG_REPEATED) {
            failed = pb_encode_object_repeated(
                w, field->number, field->kind, field->flags,
                field->subplan, value, depth) < 0;
        }
        else if (field->kind == PB_KIND_MESSAGE) {
            failed = pb_put_tag(w, field->number, PB_WIRE_LEN) < 0 ||
                     pb_encode_object_delimited(w, value, depth) < 0;
        }
        else {
            int emit = 1;
            if (!(field->flags & PB_FLAG_OPTIONAL)) {
                int is_default = pb_is_default(field->kind, value);
                if (is_default < 0) failed = 1;
                else emit = !is_default;
            }
            if (!failed && emit) {
                failed = pb_encode_single(
                    w, field->number, field->kind, value) < 0;
            }
        }
        Py_DECREF(value);
        if (failed) return -1;
    }
    unknown = PyObject_GetAttrString(message, "__wreath_protobuf_unknown__");
    if (unknown == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return -1;
        PyErr_Clear();
        return 0;
    }
    {
        Py_buffer view;
        int failed;
        if (PyObject_GetBuffer(unknown, &view, PyBUF_SIMPLE) < 0) {
            Py_DECREF(unknown);
            return -1;
        }
        failed = pb_write(w, (const char *)view.buf, view.len) < 0;
        PyBuffer_Release(&view);
        Py_DECREF(unknown);
        return failed ? -1 : 0;
    }
}

static int
pb_encode_object(WreathBytesWriter *w, PyObject *message, int depth)
{
    PyObject *descriptor_object = PyObject_GetAttrString(
        (PyObject *)Py_TYPE(message), "__wreath_protobuf_descriptor__");
    PbDescriptor *descriptor;
    int result;
    if (descriptor_object == NULL) return -1;
    descriptor = pb_descriptor_from_object(descriptor_object);
    if (descriptor == NULL) {
        Py_DECREF(descriptor_object);
        return -1;
    }
    result = pb_encode_compiled_object(w, message, descriptor, depth);
    Py_DECREF(descriptor_object);
    return result;
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
        uint64_t bits = wreath_load_u64_le(chunk);
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
    uint32_t bits = wreath_load_u32_le(chunk);
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
static PyObject *pb_decode_compiled_message(PyObject *cls,
                                            PbDescriptor *descriptor,
                                            const uint8_t *data,
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

static void
pb_compiled_values_clear(PyObject **values, Py_ssize_t count)
{
    if (values == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++)
        Py_XDECREF(values[index]);
    PyMem_Free(values);
}

static PyObject **
pb_compiled_defaults(PbDescriptor *descriptor)
{
    PyObject **values = PyMem_Calloc(
        (size_t)(descriptor->count == 0 ? 1 : descriptor->count),
        sizeof(*values));
    if (values == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        PbCompiledField *field = &descriptor->fields[index];
        PyObject *item;
        if (field->flags & PB_FLAG_MAP)
            item = PyDict_New();
        else if (field->flags & PB_FLAG_REPEATED)
            item = PyList_New(0);
        else if ((field->flags & PB_FLAG_OPTIONAL) ||
                 field->kind == PB_KIND_MESSAGE)
            item = Py_NewRef(Py_None);
        else if (field->kind == PB_KIND_STRING)
            item = PyUnicode_FromStringAndSize("", 0);
        else if (field->kind == PB_KIND_BYTES)
            item = PyBytes_FromStringAndSize(NULL, 0);
        else if (field->kind == PB_KIND_BOOL)
            item = Py_NewRef(Py_False);
        else if (field->kind == PB_KIND_DOUBLE || field->kind == PB_KIND_FLOAT)
            item = PyFloat_FromDouble(0.0);
        else
            item = PyLong_FromLong(0);
        if (item == NULL) {
            pb_compiled_values_clear(values, descriptor->count);
            return NULL;
        }
        values[index] = item;
    }
    return values;
}

static int
pb_compiled_store(PyObject **values, Py_ssize_t index, int flags,
                  PyObject *item)
{
    if (item == NULL) return -1;
    if (flags & PB_FLAG_REPEATED) {
        int failed = PyList_Append(values[index], item) < 0;
        Py_DECREF(item);
        return failed ? -1 : 0;
    }
    Py_SETREF(values[index], item);
    return 0;
}

static PyObject *
pb_declared_default(int kind)
{
    if (kind == PB_KIND_MESSAGE) return Py_NewRef(Py_None);
    if (kind == PB_KIND_STRING) return PyUnicode_FromStringAndSize("", 0);
    if (kind == PB_KIND_BYTES) return PyBytes_FromStringAndSize(NULL, 0);
    if (kind == PB_KIND_BOOL) return Py_NewRef(Py_False);
    if (kind == PB_KIND_DOUBLE || kind == PB_KIND_FLOAT)
        return PyFloat_FromDouble(0.0);
    return PyLong_FromLong(0);
}

static PyObject *
pb_read_declared_value(PbReader *reader, int kind)
{
    if (kind == PB_KIND_STRING || kind == PB_KIND_BYTES) {
        const uint8_t *body;
        Py_ssize_t body_len;
        PyObject *result;
        if (pb_read_len(reader, &body, &body_len) < 0) return NULL;
        if (kind == PB_KIND_BYTES)
            return PyBytes_FromStringAndSize((const char *)body, body_len);
        result = PyUnicode_DecodeUTF8((const char *)body, body_len, NULL);
        if (result == NULL) {
            PyErr_Clear();
            pb_err("a string field carried bytes that are not UTF-8");
        }
        return result;
    }
    return pb_read_scalar(reader, kind);
}

static int
pb_read_compiled_map(PbReader *reader, PbCompiledField *field,
                     PyObject *mapping, int depth)
{
    const uint8_t *body;
    Py_ssize_t body_len;
    PbReader entry;
    PyObject *key = NULL;
    PyObject *value = NULL;
    int result = -1;

    if (pb_read_len(reader, &body, &body_len) < 0) return -1;
    entry.data = body;
    entry.len = body_len;
    entry.pos = 0;
    key = pb_declared_default(field->map_key_kind);
    value = pb_declared_default(field->map_value_kind);
    if (key == NULL || value == NULL) goto done;
    while (entry.pos < entry.len) {
        Py_ssize_t tag_start = entry.pos;
        uint64_t tag;
        uint64_t number;
        int wire;
        PyObject *item = NULL;
        if (pb_get_varint(&entry, &tag) < 0) goto done;
        number = tag >> 3;
        wire = (int)(tag & 7U);
        if (number == 0) {
            pb_err("field number 0 at offset %zd", tag_start);
            goto done;
        }
        if (wire == PB_WIRE_SGROUP || wire == PB_WIRE_EGROUP) {
            pb_err("group wire type %d at offset %zd: groups are deprecated and "
                   "this codec refuses them rather than guessing",
                   wire, tag_start);
            goto done;
        }
        if (wire > PB_WIRE_I32) {
            pb_err("unknown wire type %d at offset %zd", wire, tag_start);
            goto done;
        }
        if (number == 1) {
            item = pb_read_declared_value(&entry, field->map_key_kind);
            if (item == NULL) goto done;
            Py_SETREF(key, item);
        }
        else if (number == 2 && field->map_value_kind == PB_KIND_MESSAGE) {
            const uint8_t *nested_body;
            Py_ssize_t nested_length;
            PbDescriptor *nested;
            if (pb_read_len(&entry, &nested_body, &nested_length) < 0) goto done;
            nested = pb_descriptor_from_object(field->nested_descriptor);
            if (nested == NULL) goto done;
            item = pb_decode_compiled_message(
                field->holder, nested, nested_body, nested_length, depth + 1);
            if (item == NULL) goto done;
            Py_SETREF(value, item);
        }
        else if (number == 2) {
            item = pb_read_declared_value(&entry, field->map_value_kind);
            if (item == NULL) goto done;
            Py_SETREF(value, item);
        }
        else if (pb_skip(&entry, wire) < 0) {
            goto done;
        }
    }
    result = PyDict_SetItem(mapping, key, value) < 0 ? -1 : 0;
done:
    Py_XDECREF(value);
    Py_XDECREF(key);
    return result;
}

static int
pb_read_compiled_field(PbReader *reader, PbCompiledField *field,
                       PyObject **values, Py_ssize_t index, int wire, int depth)
{
    if (field->flags & PB_FLAG_MAP) {
        return pb_read_compiled_map(reader, field, values[index], depth);
    }
    if (field->kind == PB_KIND_MESSAGE) {
        const uint8_t *body;
        Py_ssize_t body_len;
        PbDescriptor *nested;
        PyObject *item;
        if (pb_read_len(reader, &body, &body_len) < 0) return -1;
        nested = pb_descriptor_from_object(field->nested_descriptor);
        if (nested == NULL) return -1;
        item = pb_decode_compiled_message(
            field->holder, nested, body, body_len, depth + 1);
        return pb_compiled_store(values, index, field->flags, item);
    }
    if (field->kind == PB_KIND_STRING || field->kind == PB_KIND_BYTES) {
        const uint8_t *body;
        Py_ssize_t body_len;
        PyObject *item;
        if (pb_read_len(reader, &body, &body_len) < 0) return -1;
        if (field->kind == PB_KIND_STRING) {
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
        return pb_compiled_store(values, index, field->flags, item);
    }
    if ((field->flags & PB_FLAG_REPEATED) && wire == PB_WIRE_LEN) {
        const uint8_t *body;
        Py_ssize_t body_len;
        PbReader inner;
        if (pb_read_len(reader, &body, &body_len) < 0) return -1;
        inner.data = body;
        inner.len = body_len;
        inner.pos = 0;
        while (inner.pos < inner.len) {
            PyObject *item = pb_read_scalar(&inner, field->kind);
            if (item == NULL) return -1;
            if (PyList_Append(values[index], item) < 0) {
                Py_DECREF(item);
                return -1;
            }
            Py_DECREF(item);
        }
        return 0;
    }
    return pb_compiled_store(
        values, index, field->flags, pb_read_scalar(reader, field->kind));
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
    WreathBytesWriter unknown;
    if (wreath_writer_init(&unknown, 256) < 0) {
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

    PyObject *unknown_bytes = wreath_writer_finish(&unknown);
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

static PyObject *
pb_build_enum(PyObject *holder, PyObject *value, int tolerate_unknown)
{
    PyObject *converted = PyObject_CallOneArg(holder, value);
    if (converted != NULL) return converted;
    if (tolerate_unknown && PyErr_ExceptionMatches(PyExc_ValueError)) {
        PyErr_Clear();
        return Py_NewRef(value);
    }
    return NULL;
}

static PyObject *
pb_build_enums(PyObject *holder, PyObject *values)
{
    Py_ssize_t count = PyList_GET_SIZE(values);
    PyObject *result = PyList_New(count);
    if (result == NULL) return NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *value = PyList_GET_ITEM(values, i);
        PyObject *built = pb_build_enum(holder, value, 0);
        if (built == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, i, built);
    }
    return result;
}

static Py_ssize_t
pb_compiled_find(PbDescriptor *descriptor, uint64_t number)
{
    Py_ssize_t low = 0, high = descriptor->count;
    if (descriptor->count <= 8) {
        for (Py_ssize_t index = 0; index < descriptor->count; index++) {
            if (descriptor->fields[index].number == number) return index;
        }
        return -1;
    }
    while (low < high) {
        Py_ssize_t middle = low + ((high - low) >> 1);
        uint64_t candidate = descriptor->lookup[middle].number;
        if (candidate < number) low = middle + 1;
        else high = middle;
    }
    return low < descriptor->count && descriptor->lookup[low].number == number
        ? descriptor->lookup[low].index : -1;
}

static int
pb_compiled_convert(PbDescriptor *descriptor, PyObject **values)
{
    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        PbCompiledField *field = &descriptor->fields[index];
        PyObject *converted;
        if ((field->flags & PB_FLAG_REPEATED) &&
            field->kind == PB_KIND_ENUM) {
            converted = pb_build_enums(field->holder, values[index]);
        }
        else if (field->kind == PB_KIND_ENUM && values[index] != Py_None) {
            converted = pb_build_enum(field->holder, values[index], 1);
        }
        else {
            continue;
        }
        if (converted == NULL) return -1;
        Py_SETREF(values[index], converted);
    }
    return 0;
}

static int
pb_compiled_apply_oneofs(PbDescriptor *descriptor, PyObject **values)
{
    for (Py_ssize_t group_index = 0;
         group_index < descriptor->oneof_count; group_index++) {
        PbOneof *group = &descriptor->oneofs[group_index];
        Py_ssize_t last = -1;
        for (Py_ssize_t member_index = 0;
             member_index < group->count; member_index++) {
            Py_ssize_t index = group->members[member_index];
            if (values[index] != Py_None) last = index;
        }
        if (last >= 0) {
            for (Py_ssize_t member_index = 0;
                 member_index < group->count; member_index++) {
                Py_ssize_t index = group->members[member_index];
                if (index != last && values[index] != Py_None)
                    Py_SETREF(values[index], Py_NewRef(Py_None));
            }
        }
    }
    return 0;
}

static PyObject *
pb_decode_compiled_message(PyObject *cls, PbDescriptor *descriptor,
                           const uint8_t *data, Py_ssize_t len, int depth)
{
    PyObject **values = NULL;
    PyObject *unknown_bytes = NULL;
    PyObject *built = NULL;
    WreathBytesWriter unknown;
    PbReader reader = {data, len, 0};

    if (depth > PB_MAX_DEPTH) {
        pb_err("protobuf message nesting exceeds the depth limit");
        return NULL;
    }
    values = pb_compiled_defaults(descriptor);
    if (values == NULL) return NULL;
    if (wreath_writer_init(&unknown, 256) < 0) goto error;
    while (reader.pos < reader.len) {
        Py_ssize_t tag_start = reader.pos;
        uint64_t tag;
        uint64_t number;
        int wire;
        Py_ssize_t index;
        if (pb_get_varint(&reader, &tag) < 0) goto writer_error;
        number = tag >> 3;
        wire = (int)(tag & 0x07U);
        if (number == 0) {
            pb_err("field number 0 at offset %zd", tag_start);
            goto writer_error;
        }
        if (wire == PB_WIRE_SGROUP || wire == PB_WIRE_EGROUP) {
            pb_err("group wire type %d at offset %zd: groups are deprecated and "
                   "this codec refuses them rather than guessing",
                   wire, tag_start);
            goto writer_error;
        }
        if (wire > PB_WIRE_I32) {
            pb_err("unknown wire type %d at offset %zd", wire, tag_start);
            goto writer_error;
        }
        index = pb_compiled_find(descriptor, number);
        if (index < 0) {
            if (pb_skip(&reader, wire) < 0 ||
                pb_write(&unknown, (const char *)(data + tag_start),
                         reader.pos - tag_start) < 0) goto writer_error;
            continue;
        }
        if (pb_read_compiled_field(
                &reader, &descriptor->fields[index], values, index, wire,
                depth) < 0) goto writer_error;
    }
    unknown_bytes = wreath_writer_finish(&unknown);
    if (unknown_bytes == NULL) goto error;
    if (pb_compiled_convert(descriptor, values) < 0 ||
        pb_compiled_apply_oneofs(descriptor, values) < 0) goto error;
    built = PyObject_Vectorcall(cls, values, 0, descriptor->names);
    if (built == NULL) goto error;
    {
        if (PyObject_GenericSetAttr(
                built, descriptor->unknown_name, unknown_bytes) < 0) goto error;
    }
    pb_compiled_values_clear(values, descriptor->count);
    Py_DECREF(unknown_bytes);
    return built;

writer_error:
    Py_XDECREF(unknown.bytes);
error:
    Py_CLEAR(built);
    Py_XDECREF(unknown_bytes);
    pb_compiled_values_clear(values, descriptor->count);
    return NULL;
}

static void
pb_descriptor_free(PbDescriptor *descriptor)
{
    if (descriptor == NULL) return;
    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        PbCompiledField *field = &descriptor->fields[index];
        Py_XDECREF(field->name);
        Py_XDECREF(field->camel);
        Py_XDECREF(field->holder);
        Py_XDECREF(field->subplan);
        Py_XDECREF(field->nested_descriptor);
    }
    PyMem_Free(descriptor->fields);
    PyMem_Free(descriptor->lookup);
    if (descriptor->oneofs != NULL) {
        for (Py_ssize_t index = 0; index < descriptor->oneof_count; index++)
            PyMem_Free(descriptor->oneofs[index].members);
    }
    PyMem_Free(descriptor->oneofs);
    Py_XDECREF(descriptor->names);
    Py_XDECREF(descriptor->unknown_name);
    PyMem_Free(descriptor);
}

static int
pb_lookup_compare(const void *left, const void *right)
{
    const PbFieldLookup *a = left;
    const PbFieldLookup *b = right;
    return a->number < b->number ? -1 : a->number != b->number;
}

static void
pb_descriptor_destructor(PyObject *capsule)
{
    PbDescriptor *descriptor = PyCapsule_GetPointer(capsule, PB_DESCRIPTOR_NAME);
    if (descriptor == NULL) {
        PyErr_Clear();
        return;
    }
    pb_descriptor_free(descriptor);
}

static PbDescriptor *
pb_descriptor_from_object(PyObject *object)
{
    return PyCapsule_GetPointer(object, PB_DESCRIPTOR_NAME);
}

PyObject *
wreath_protobuf_compile(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan;
    PyObject *names;
    PyObject *holders;
    PyObject *oneofs;
    PbDescriptor *descriptor = NULL;
    PyObject *capsule = NULL;

    if (!PyArg_ParseTuple(args, "O!O!O!O!:protobuf_compile",
                          &PyTuple_Type, &plan,
                          &PyTuple_Type, &names,
                          &PyTuple_Type, &holders,
                          &PyDict_Type, &oneofs)) return NULL;
    if (PyTuple_GET_SIZE(plan) != PyTuple_GET_SIZE(names) ||
        PyTuple_GET_SIZE(plan) != PyTuple_GET_SIZE(holders)) {
        PyErr_SetString(PyExc_ValueError,
                        "protobuf plan, names, and holders differ in length");
        return NULL;
    }
    descriptor = PyMem_Calloc(1, sizeof(*descriptor));
    if (descriptor == NULL) return PyErr_NoMemory();
    descriptor->count = PyTuple_GET_SIZE(plan);
    descriptor->fields = PyMem_Calloc(
        (size_t)(descriptor->count == 0 ? 1 : descriptor->count),
        sizeof(*descriptor->fields));
    descriptor->lookup = PyMem_Calloc(
        (size_t)(descriptor->count == 0 ? 1 : descriptor->count),
        sizeof(*descriptor->lookup));
    descriptor->unknown_name = PyUnicode_FromString(
        "__wreath_protobuf_unknown__");
    if (descriptor->fields == NULL || descriptor->lookup == NULL ||
        descriptor->unknown_name == NULL) {
        pb_descriptor_free(descriptor);
        return PyErr_NoMemory();
    }
    descriptor->names = Py_NewRef(names);

    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        PyObject *row = PyTuple_GET_ITEM(plan, index);
        PbCompiledField *field = &descriptor->fields[index];
        PyObject *holder = PyTuple_GET_ITEM(holders, index);
        if (!PyTuple_Check(row) || PyTuple_GET_SIZE(row) != 4) {
            PyErr_Format(PyExc_TypeError,
                         "protobuf plan row %zd must have four items", index);
            goto error;
        }
        field->number = PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row, 0));
        field->kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 1));
        field->flags = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 2));
        if (PyErr_Occurred()) goto error;
        descriptor->lookup[index].number = field->number;
        descriptor->lookup[index].index = index;
        field->name = Py_NewRef(PyTuple_GET_ITEM(names, index));
        field->camel = pb_camel_name(field->name);
        if (field->camel == NULL) goto error;
        field->holder = Py_NewRef(holder);
        field->subplan = Py_NewRef(PyTuple_GET_ITEM(row, 3));
        field->map_key_kind = -1;
        field->map_value_kind = -1;
        if (field->flags & PB_FLAG_MAP) {
            PyObject *subplan = field->subplan;
            PyObject *value_row;
            if (!PyTuple_Check(subplan) || PyTuple_GET_SIZE(subplan) != 2) {
                PyErr_Format(PyExc_TypeError,
                             "protobuf map plan at field %zd must have two rows",
                             index);
                goto error;
            }
            field->map_key_kind = (int)PyLong_AsLong(
                PyTuple_GET_ITEM(PyTuple_GET_ITEM(subplan, 0), 1));
            value_row = PyTuple_GET_ITEM(subplan, 1);
            field->map_value_kind = (int)PyLong_AsLong(
                PyTuple_GET_ITEM(value_row, 1));
            if ((field->map_key_kind < 0 || field->map_value_kind < 0) &&
                PyErr_Occurred()) goto error;
        }
        if ((field->kind == PB_KIND_MESSAGE && !(field->flags & PB_FLAG_MAP)) ||
            field->map_value_kind == PB_KIND_MESSAGE) {
            if (holder == Py_None) {
                PyErr_Format(PyExc_TypeError,
                             "protobuf message field %zd has no holder", index);
                goto error;
            }
            field->nested_descriptor = PyObject_GetAttrString(
                holder, "__wreath_protobuf_descriptor__");
            if (field->nested_descriptor == NULL) goto error;
            if (pb_descriptor_from_object(field->nested_descriptor) == NULL)
                goto error;
        }
    }
    for (Py_ssize_t index = 0; index < descriptor->count; index++) {
        for (Py_ssize_t prior = 0; prior < index; prior++) {
            int equal = PyObject_RichCompareBool(
                descriptor->fields[prior].camel,
                descriptor->fields[index].camel, Py_EQ);
            if (equal < 0) goto error;
            if (equal) {
                PyErr_Format(
                    PyExc_ValueError,
                    "protobuf fields %R and %R share OTLP/JSON name %R",
                    descriptor->fields[prior].name,
                    descriptor->fields[index].name,
                    descriptor->fields[index].camel);
                goto error;
            }
        }
    }
    qsort(descriptor->lookup, (size_t)descriptor->count,
          sizeof(*descriptor->lookup), pb_lookup_compare);
    for (Py_ssize_t index = 1; index < descriptor->count; index++) {
        if (descriptor->lookup[index - 1].number ==
            descriptor->lookup[index].number) {
            PyErr_Format(
                PyExc_ValueError,
                "protobuf field number %llu is declared more than once",
                (unsigned long long)descriptor->lookup[index].number);
            goto error;
        }
    }
    descriptor->oneof_count = PyDict_GET_SIZE(oneofs);
    descriptor->oneofs = PyMem_Calloc(
        (size_t)(descriptor->oneof_count == 0 ? 1 : descriptor->oneof_count),
        sizeof(*descriptor->oneofs));
    if (descriptor->oneofs == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    {
        Py_ssize_t position = 0, group_index = 0;
        PyObject *group_name, *members;
        while (PyDict_Next(oneofs, &position, &group_name, &members)) {
            PbOneof *group = &descriptor->oneofs[group_index++];
            if (!PyList_Check(members)) {
                PyErr_Format(
                    PyExc_TypeError,
                    "protobuf oneof %R members must be a list of field indices",
                    group_name);
                goto error;
            }
            group->count = PyList_GET_SIZE(members);
            group->members = PyMem_Malloc(
                (size_t)(group->count == 0 ? 1 : group->count) *
                sizeof(*group->members));
            if (group->members == NULL) {
                PyErr_NoMemory();
                goto error;
            }
            for (Py_ssize_t member = 0; member < group->count; member++) {
                Py_ssize_t field_index = PyLong_AsSsize_t(
                    PyList_GET_ITEM(members, member));
                if (field_index == -1 && PyErr_Occurred()) goto error;
                if (field_index < 0 || field_index >= descriptor->count) {
                    PyErr_Format(
                        PyExc_ValueError,
                        "protobuf oneof %R field index %zd is outside 0..%zd",
                        group_name, field_index, descriptor->count - 1);
                    goto error;
                }
                group->members[member] = field_index;
            }
        }
    }
    capsule = PyCapsule_New(descriptor, PB_DESCRIPTOR_NAME,
                            pb_descriptor_destructor);
    if (capsule == NULL) goto error;
    return capsule;

error:
    pb_descriptor_free(descriptor);
    return NULL;
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
    WreathBytesWriter w;
    if (wreath_writer_init(&w, 256) < 0) {
        return NULL;
    }
    if (pb_encode_values(&w, plan, values, unknown, 0) < 0) {
        Py_XDECREF(w.bytes);
        return NULL;
    }
    return wreath_writer_finish(&w);
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

PyObject *
wreath_protobuf_encode_message(PyObject *Py_UNUSED(self), PyObject *message)
{
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 256) < 0) return NULL;
    if (pb_encode_object(&writer, message, 0) < 0) {
        Py_XDECREF(writer.bytes);
        return NULL;
    }
    return wreath_writer_finish(&writer);
}

PyObject *
wreath_protobuf_decode_message(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *cls;
    PyObject *descriptor_object;
    PbDescriptor *descriptor;
    PyObject *result;
    Py_buffer view;

    if (!PyArg_ParseTuple(args, "Oy*:protobuf_decode_message", &cls, &view)) return NULL;
    descriptor_object = PyObject_GetAttrString(
        cls, "__wreath_protobuf_descriptor__");
    if (descriptor_object == NULL) {
        PyBuffer_Release(&view);
        return NULL;
    }
    descriptor = pb_descriptor_from_object(descriptor_object);
    if (descriptor == NULL) {
        Py_DECREF(descriptor_object);
        PyBuffer_Release(&view);
        return NULL;
    }
    result = pb_decode_compiled_message(
        cls, descriptor, (const uint8_t *)view.buf, view.len, 0);
    PyBuffer_Release(&view);
    Py_DECREF(descriptor_object);
    return result;
}

/* -- OTLP JSON projection ------------------------------------------------- */

static PyObject *
pb_camel_name(PyObject *name)
{
    Py_ssize_t length;
    const char *source = PyUnicode_AsUTF8AndSize(name, &length);
    PyObject *result;
    char *target;
    Py_ssize_t written = 0;
    int upper = 0;
    if (source == NULL) return NULL;
    result = PyUnicode_New(length, 127);
    if (result == NULL) return NULL;
    target = (char *)PyUnicode_1BYTE_DATA(result);
    for (Py_ssize_t index = 0; index < length; index++) {
        unsigned char value = (unsigned char)source[index];
        if (value == '_') { upper = 1; continue; }
        if (upper && value >= 'a' && value <= 'z') value = (unsigned char)(value - 32);
        target[written++] = (char)value;
        upper = 0;
    }
    if (written != length && PyUnicode_Resize(&result, written) < 0) return NULL;
    return result;
}

static int
pb_hex_nibble(unsigned char value)
{
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}

static PyObject *
pb_otlp_scalar(int kind, PyObject *value)
{
    if (kind == PB_KIND_BYTES && PyUnicode_Check(value)) {
        Py_ssize_t length;
        const char *hex = PyUnicode_AsUTF8AndSize(value, &length);
        PyObject *result;
        unsigned char *output;
        if (hex == NULL) return NULL;
        if (length & 1) {
            PyErr_SetString(PyExc_ValueError, "OTLP hexadecimal id has odd length");
            return NULL;
        }
        result = PyBytes_FromStringAndSize(NULL, length / 2);
        if (result == NULL) return NULL;
        output = (unsigned char *)PyBytes_AS_STRING(result);
        for (Py_ssize_t index = 0; index < length; index += 2) {
            int high = pb_hex_nibble((unsigned char)hex[index]);
            int low = pb_hex_nibble((unsigned char)hex[index + 1]);
            if (high < 0 || low < 0) {
                Py_DECREF(result);
                PyErr_SetString(PyExc_ValueError, "OTLP id is not hexadecimal");
                return NULL;
            }
            output[index / 2] = (unsigned char)((high << 4) | low);
        }
        return result;
    }
    if (kind == PB_KIND_BYTES) return PyBytes_FromObject(value);
    if (kind == PB_KIND_INT64 || kind == PB_KIND_UINT64 ||
        kind == PB_KIND_SINT64 || kind == PB_KIND_FIXED64 ||
        kind == PB_KIND_SFIXED64) return PyNumber_Long(value);
    return Py_NewRef(value);
}

static PyObject *pb_otlp_from_json(PyObject *cls, PyObject *data, int depth);

static int
pb_otlp_name_matches(PyObject *snake, PyObject *camel)
{
    Py_ssize_t snake_length, camel_length;
    const char *source = PyUnicode_AsUTF8AndSize(snake, &snake_length);
    const char *wanted = PyUnicode_AsUTF8AndSize(camel, &camel_length);
    Py_ssize_t at = 0;
    int upper = 0;
    if (source == NULL || wanted == NULL) return -1;
    for (Py_ssize_t index = 0; index < snake_length; index++) {
        unsigned char value = (unsigned char)source[index];
        if (value == '_') {
            upper = 1;
            continue;
        }
        if (upper && value >= 'a' && value <= 'z') value -= 32;
        if (at >= camel_length || (unsigned char)wanted[at] != value) return 0;
        upper = 0;
        at++;
    }
    return at == camel_length;
}

static int pb_encode_otlp_json(WreathBytesWriter *writer, PyObject *cls,
                               PyObject *data, int depth);

static int
pb_encode_otlp_delimited(WreathBytesWriter *writer, PyObject *cls,
                         PyObject *data, int depth)
{
    WreathBytesWriter body;
    PyObject *encoded;
    int failed;
    if (wreath_writer_init(&body, 256) < 0) return -1;
    if (pb_encode_otlp_json(&body, cls, data, depth + 1) < 0) {
        Py_XDECREF(body.bytes);
        return -1;
    }
    encoded = wreath_writer_finish(&body);
    if (encoded == NULL) return -1;
    failed = pb_put_varint(writer, (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
             || pb_write(writer, PyBytes_AS_STRING(encoded),
                         PyBytes_GET_SIZE(encoded)) < 0;
    Py_DECREF(encoded);
    return failed ? -1 : 0;
}

static int
pb_encode_otlp_value(WreathBytesWriter *writer, uint64_t number, int kind,
                     int flags, PyObject *holder, PyObject *value, int depth)
{
    if (flags & PB_FLAG_MAP) {
        PyErr_SetString(PyExc_TypeError, "OTLP declarations do not contain map fields");
        return -1;
    }
    if (flags & PB_FLAG_REPEATED) {
        PyObject *items = PySequence_Fast(
            value, "repeated OTLP field must be a sequence");
        Py_ssize_t count;
        if (items == NULL) return -1;
        count = PySequence_Fast_GET_SIZE(items);
        if (count == 0) {
            Py_DECREF(items);
            return 0;
        }
        if (kind == PB_KIND_MESSAGE) {
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *item = PySequence_Fast_GET_ITEM(items, index);
                if (pb_put_tag(writer, number, PB_WIRE_LEN) < 0
                    || pb_encode_otlp_delimited(writer, holder, item, depth) < 0) {
                    Py_DECREF(items);
                    return -1;
                }
            }
            Py_DECREF(items);
            return 0;
        }
        if ((flags & PB_FLAG_PACKED) && kind != PB_KIND_STRING
            && kind != PB_KIND_BYTES) {
            WreathBytesWriter body;
            PyObject *encoded;
            int failed;
            if (wreath_writer_init(&body, 256) < 0) {
                Py_DECREF(items);
                return -1;
            }
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *converted = pb_otlp_scalar(
                    kind, PySequence_Fast_GET_ITEM(items, index));
                if (converted == NULL || pb_put_scalar(&body, kind, converted) < 0) {
                    Py_XDECREF(converted);
                    Py_XDECREF(body.bytes);
                    Py_DECREF(items);
                    return -1;
                }
                Py_DECREF(converted);
            }
            encoded = wreath_writer_finish(&body);
            if (encoded == NULL) {
                Py_DECREF(items);
                return -1;
            }
            failed = pb_put_tag(writer, number, PB_WIRE_LEN) < 0
                     || pb_put_varint(writer,
                                      (uint64_t)PyBytes_GET_SIZE(encoded)) < 0
                     || pb_write(writer, PyBytes_AS_STRING(encoded),
                                 PyBytes_GET_SIZE(encoded)) < 0;
            Py_DECREF(encoded);
            Py_DECREF(items);
            return failed ? -1 : 0;
        }
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *converted = pb_otlp_scalar(
                kind, PySequence_Fast_GET_ITEM(items, index));
            if (converted == NULL
                || pb_encode_single(writer, number, kind, converted) < 0) {
                Py_XDECREF(converted);
                Py_DECREF(items);
                return -1;
            }
            Py_DECREF(converted);
        }
        Py_DECREF(items);
        return 0;
    }
    if (kind == PB_KIND_MESSAGE) {
        if (pb_put_tag(writer, number, PB_WIRE_LEN) < 0
            || pb_encode_otlp_delimited(writer, holder, value, depth) < 0) return -1;
        return 0;
    }
    PyObject *converted = pb_otlp_scalar(kind, value);
    int failed = 0;
    if (converted == NULL) return -1;
    if (!(flags & PB_FLAG_OPTIONAL)) {
        int is_default = pb_is_default(kind, converted);
        if (is_default < 0) failed = 1;
        else if (is_default) {
            Py_DECREF(converted);
            return 0;
        }
    }
    if (!failed) failed = pb_encode_single(writer, number, kind, converted) < 0;
    Py_DECREF(converted);
    return failed ? -1 : 0;
}

static int
pb_encode_otlp_json(WreathBytesWriter *writer, PyObject *cls,
                    PyObject *data, int depth)
{
    PyObject *definition;
    PyObject *plan;
    PyObject *names;
    PyObject *holders;
    Py_ssize_t position = 0;
    PyObject *key;
    PyObject *value;
    if (depth > PB_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError, "OTLP message nesting exceeds 100 levels");
        return -1;
    }
    if (!PyDict_Check(data)) {
        PyErr_SetString(PyExc_TypeError, "OTLP message value must be a dict");
        return -1;
    }
    definition = PyObject_GetAttrString(cls, "__wreath_protobuf_plan__");
    if (definition == NULL) return -1;
    if (!PyTuple_Check(definition) || PyTuple_GET_SIZE(definition) != 4) {
        Py_DECREF(definition);
        PyErr_SetString(PyExc_TypeError, "message class has no compiled protobuf plan");
        return -1;
    }
    plan = PyTuple_GET_ITEM(definition, 0);
    names = PyTuple_GET_ITEM(definition, 1);
    holders = PyTuple_GET_ITEM(definition, 2);
    while (PyDict_Next(data, &position, &key, &value)) {
        Py_ssize_t found = -1;
        if (PyUnicode_Check(key)) {
            for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(names); index++) {
                int matches = pb_otlp_name_matches(PyTuple_GET_ITEM(names, index), key);
                if (matches < 0) {
                    Py_DECREF(definition);
                    return -1;
                }
                if (matches) {
                    found = index;
                    break;
                }
            }
        }
        if (found < 0) {
            PyObject *name = PyObject_GetAttrString(cls, "__name__");
            if (name != NULL) {
                PyErr_Format(PyExc_ValueError,
                             "%U has no field for OTLP/JSON key %R", name, key);
                Py_DECREF(name);
            }
            Py_DECREF(definition);
            return -1;
        }
        PyObject *row = PyTuple_GET_ITEM(plan, found);
        uint64_t number = PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row, 0));
        int kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 1));
        int flags = (int)PyLong_AsLong(PyTuple_GET_ITEM(row, 2));
        if (PyErr_Occurred()
            || pb_encode_otlp_value(writer, number, kind, flags,
                                    PyTuple_GET_ITEM(holders, found), value,
                                    depth) < 0) {
            Py_DECREF(definition);
            return -1;
        }
    }
    Py_DECREF(definition);
    return 0;
}

static PyObject *
pb_otlp_value(int kind, int flags, PyObject *holder, PyObject *value, int depth)
{
    if (flags & PB_FLAG_REPEATED) {
        PyObject *items = PySequence_Fast(value, "repeated OTLP field must be a sequence");
        PyObject *result;
        if (items == NULL) return NULL;
        result = PyList_New(PySequence_Fast_GET_SIZE(items));
        if (result == NULL) { Py_DECREF(items); return NULL; }
        for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(items); index++) {
            PyObject *item = PySequence_Fast_GET_ITEM(items, index);
            PyObject *converted = kind == PB_KIND_MESSAGE
                ? pb_otlp_from_json(holder, item, depth + 1)
                : pb_otlp_scalar(kind, item);
            if (converted == NULL) {
                Py_DECREF(result);
                Py_DECREF(items);
                return NULL;
            }
            PyList_SET_ITEM(result, index, converted);
        }
        Py_DECREF(items);
        return result;
    }
    if (kind == PB_KIND_MESSAGE) return pb_otlp_from_json(holder, value, depth + 1);
    return pb_otlp_scalar(kind, value);
}

static PyObject *
pb_otlp_from_json(PyObject *cls, PyObject *data, int depth)
{
    PyObject *descriptor_object = NULL, *keywords = NULL, *result = NULL;
    PyObject **arguments = NULL;
    PbDescriptor *descriptor;
    Py_ssize_t position = 0;
    Py_ssize_t used = 0;
    PyObject *key, *value;
    if (depth > PB_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError, "OTLP message nesting exceeds 100 levels");
        return NULL;
    }
    if (!PyDict_Check(data)) {
        PyErr_SetString(PyExc_TypeError, "OTLP message value must be a dict");
        return NULL;
    }
    descriptor_object = PyObject_GetAttrString(
        cls, "__wreath_protobuf_descriptor__");
    if (descriptor_object == NULL) return NULL;
    descriptor = pb_descriptor_from_object(descriptor_object);
    if (descriptor == NULL) goto done;
    if (PyDict_GET_SIZE(data) > descriptor->count) {
        PyObject *name = PyObject_GetAttrString(cls, "__name__");
        if (name != NULL) {
            PyErr_Format(
                PyExc_ValueError,
                "%U received more OTLP/JSON keys than it declares fields", name);
            Py_DECREF(name);
        }
        goto done;
    }
    keywords = PyTuple_New(PyDict_GET_SIZE(data));
    arguments = PyMem_Calloc(
        (size_t)(PyDict_GET_SIZE(data) == 0 ? 1 : PyDict_GET_SIZE(data)),
        sizeof(*arguments));
    if (keywords == NULL || arguments == NULL) {
        if (arguments == NULL) PyErr_NoMemory();
        goto done;
    }
    while (PyDict_Next(data, &position, &key, &value)) {
        Py_ssize_t index = -1;
        PyObject *converted;
        for (Py_ssize_t candidate = 0;
             candidate < descriptor->count; candidate++) {
            int equal = PyObject_RichCompareBool(
                key, descriptor->fields[candidate].camel, Py_EQ);
            if (equal < 0) goto done;
            if (equal) {
                index = candidate;
                break;
            }
        }
        if (index < 0) {
            PyObject *name = PyObject_GetAttrString(cls, "__name__");
            if (name != NULL) {
                PyErr_Format(PyExc_ValueError,
                             "%U has no field for OTLP/JSON key %R", name, key);
                Py_DECREF(name);
            }
            goto done;
        }
        converted = pb_otlp_value(
            descriptor->fields[index].kind, descriptor->fields[index].flags,
            descriptor->fields[index].holder, value, depth);
        if (converted == NULL) goto done;
        arguments[used] = converted;
        PyTuple_SET_ITEM(
            keywords, used, Py_NewRef(descriptor->fields[index].name));
        used++;
    }
    result = PyObject_Vectorcall(cls, arguments, 0, keywords);
done:
    for (Py_ssize_t index = 0; index < used; index++)
        Py_XDECREF(arguments == NULL ? NULL : arguments[index]);
    PyMem_Free(arguments);
    Py_XDECREF(keywords);
    Py_XDECREF(descriptor_object);
    return result;
}

PyObject *
wreath_protobuf_otlp_from_json(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *cls, *data;
    if (!PyArg_ParseTuple(args, "OO:protobuf_otlp_from_json", &cls, &data)) return NULL;
    return pb_otlp_from_json(cls, data, 0);
}

PyObject *
wreath_protobuf_encode_otlp_json(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *cls;
    PyObject *data;
    WreathBytesWriter writer;
    if (!PyArg_ParseTuple(args, "OO:protobuf_encode_otlp_json", &cls, &data)) {
        return NULL;
    }
    if (wreath_writer_init(&writer, 1024) < 0) return NULL;
    if (pb_encode_otlp_json(&writer, cls, data, 0) < 0) {
        Py_XDECREF(writer.bytes);
        return NULL;
    }
    return wreath_writer_finish(&writer);
}

/* -- direct OTLP metric writer -------------------------------------------
 *
 * The public OTLP/JSON builder remains the readable mapping definition and
 * JSON transport surface.  The protobuf transport does not need to allocate
 * that dictionary tree: this writer emits the same declared wire shape from a
 * ProjectorSnapshot in one owned buffer.
 */

typedef struct {
    Py_ssize_t length_at;
    Py_ssize_t body_at;
} OtlpFrame;

typedef struct {
    PyObject *route_id;
    PyObject *route_path;
    PyObject *buckets;
    uint64_t route_raw;
    uint64_t count_raw;
    uint64_t errors_raw;
    double duration_sum;
    double duration_max;
} OtlpMetric;

static int
otlp_begin(WreathBytesWriter *writer, uint64_t field, OtlpFrame *frame)
{
    if (pb_put_tag(writer, field, PB_WIRE_LEN) < 0 ||
        wreath_writer_reserve(writer, 1) < 0) return -1;
    frame->length_at = writer->len;
    writer->buf[writer->len++] = 0;
    frame->body_at = writer->len;
    return 0;
}

static int
otlp_end(WreathBytesWriter *writer, const OtlpFrame *frame)
{
    uint64_t length = (uint64_t)(writer->len - frame->body_at);
    unsigned char encoded[10];
    Py_ssize_t count = 0;
    do {
        encoded[count] = (unsigned char)(length & 0x7fU);
        length >>= 7;
        if (length != 0) encoded[count] |= 0x80U;
        count++;
    } while (length != 0);
    if (count > 1) {
        Py_ssize_t body_length = writer->len - frame->body_at;
        if (wreath_writer_reserve(writer, count - 1) < 0) return -1;
        memmove(writer->buf + frame->body_at + count - 1,
                writer->buf + frame->body_at, (size_t)body_length);
        writer->len += count - 1;
    }
    memcpy(writer->buf + frame->length_at, encoded, (size_t)count);
    return 0;
}

static int
otlp_string(WreathBytesWriter *writer, uint64_t field,
            const char *text, Py_ssize_t length)
{
    if (length == 0) return 0;
    return pb_put_tag(writer, field, PB_WIRE_LEN) < 0 ||
           pb_put_varint(writer, (uint64_t)length) < 0 ||
           pb_write(writer, text, length) < 0 ? -1 : 0;
}

static int
otlp_unicode(WreathBytesWriter *writer, uint64_t field, PyObject *text)
{
    Py_ssize_t length;
    const char *data = PyUnicode_AsUTF8AndSize(text, &length);
    if (data == NULL) return -1;
    return pb_put_tag(writer, field, PB_WIRE_LEN) < 0 ||
           pb_put_varint(writer, (uint64_t)length) < 0 ||
           pb_write(writer, data, length) < 0 ? -1 : 0;
}

static int
otlp_fixed64(WreathBytesWriter *writer, uint64_t field, uint64_t value,
             int optional)
{
    if (!optional && value == 0) return 0;
    return pb_put_tag(writer, field, PB_WIRE_I64) < 0 ||
           pb_put_u64(writer, value) < 0 ? -1 : 0;
}

static int
otlp_double(WreathBytesWriter *writer, uint64_t field, double value)
{
    uint64_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return otlp_fixed64(writer, field, bits, 1);
}

static int
otlp_attribute_string(WreathBytesWriter *writer, uint64_t field,
                      const char *key, Py_ssize_t key_length, PyObject *value)
{
    OtlpFrame attribute, any;
    if (otlp_begin(writer, field, &attribute) < 0 ||
        otlp_string(writer, 1, key, key_length) < 0 ||
        otlp_begin(writer, 2, &any) < 0 ||
        otlp_unicode(writer, 1, value) < 0 ||
        otlp_end(writer, &any) < 0 ||
        otlp_end(writer, &attribute) < 0) return -1;
    return 0;
}

static int
otlp_attribute_text(WreathBytesWriter *writer, uint64_t field,
                    const char *key, const char *value)
{
    OtlpFrame attribute, any;
    if (otlp_begin(writer, field, &attribute) < 0 ||
        otlp_string(writer, 1, key, (Py_ssize_t)strlen(key)) < 0 ||
        otlp_begin(writer, 2, &any) < 0 ||
        otlp_string(writer, 1, value, (Py_ssize_t)strlen(value)) < 0 ||
        otlp_end(writer, &any) < 0 ||
        otlp_end(writer, &attribute) < 0) return -1;
    return 0;
}

static int
otlp_attribute_int(WreathBytesWriter *writer, uint64_t field,
                   const char *key, uint64_t raw)
{
    OtlpFrame attribute, any;
    if (otlp_begin(writer, field, &attribute) < 0 ||
        otlp_string(writer, 1, key, (Py_ssize_t)strlen(key)) < 0 ||
        otlp_begin(writer, 2, &any) < 0 ||
        pb_put_tag(writer, 3, PB_WIRE_VARINT) < 0 ||
        pb_put_varint(writer, raw) < 0 ||
        otlp_end(writer, &any) < 0 ||
        otlp_end(writer, &attribute) < 0) return -1;
    return 0;
}

static int
otlp_resource(WreathBytesWriter *writer, PyObject *attributes)
{
    PyObject *service = NULL;
    OtlpFrame resource;
    if (otlp_begin(writer, 1, &resource) < 0) return -1;
    if (attributes != Py_None) {
        if (PyMapping_GetOptionalItemString(
                attributes, "service.name", &service) < 0) return -1;
    }
    if (service != NULL) {
        if (otlp_attribute_string(
                writer, 1, "service.name", 12, service) < 0) {
            Py_DECREF(service);
            return -1;
        }
        Py_DECREF(service);
    } else if (otlp_attribute_text(
                   writer, 1, "service.name", "wreath") < 0) return -1;
    if (attributes != Py_None) {
        PyObject *items = PyMapping_Items(attributes);
        if (items == NULL) return -1;
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(items); index++) {
            PyObject *pair = PyList_GET_ITEM(items, index);
            PyObject *key = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            Py_ssize_t key_length;
            const char *key_data;
            int is_service = PyUnicode_Check(key) &&
                PyUnicode_CompareWithASCIIString(key, "service.name") == 0;
            if (is_service) continue;
            key_data = PyUnicode_AsUTF8AndSize(key, &key_length);
            if (key_data == NULL || otlp_attribute_string(
                    writer, 1, key_data, key_length, value) < 0) {
                Py_DECREF(items);
                return -1;
            }
        }
        Py_DECREF(items);
    }
    return otlp_end(writer, &resource);
}

static PyObject *
otlp_route_paths(PyObject *image)
{
    PyObject *result = PyDict_New();
    PyObject *rows_object = NULL, *rows = NULL;
    if (result == NULL || image == Py_None) return result;
    rows_object = PyObject_GetAttrString(image, "routes");
    if (rows_object == NULL) goto error;
    rows = PySequence_Fast(rows_object, "metadata routes must be a sequence");
    Py_DECREF(rows_object);
    if (rows == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(rows); index++) {
        PyObject *row = PySequence_Fast_GET_ITEM(rows, index);
        PyObject *route_id = PyObject_GetAttrString(row, "route_id");
        PyObject *path = PyObject_GetAttrString(row, "path");
        if (route_id == NULL || path == NULL ||
            PyDict_SetItem(result, route_id, path) < 0) {
            Py_XDECREF(path);
            Py_XDECREF(route_id);
            goto error;
        }
        Py_DECREF(path);
        Py_DECREF(route_id);
    }
    Py_DECREF(rows);
    return result;
error:
    Py_XDECREF(rows);
    Py_DECREF(result);
    return NULL;
}

static void
otlp_metrics_clear(OtlpMetric *metrics, Py_ssize_t count)
{
    if (metrics == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_XDECREF(metrics[index].buckets);
        Py_XDECREF(metrics[index].route_path);
        Py_XDECREF(metrics[index].route_id);
    }
    PyMem_Free(metrics);
}

static int
otlp_point_attributes(WreathBytesWriter *writer, uint64_t field,
                      const OtlpMetric *metric, int error)
{
    if (otlp_attribute_int(
            writer, field, "wreath.route_id", metric->route_raw) < 0) return -1;
    if (metric->route_path != NULL && otlp_attribute_string(
            writer, field, "http.route", 10, metric->route_path) < 0) return -1;
    if (error && otlp_attribute_text(
            writer, field, "wreath.outcome", "error") < 0) return -1;
    return 0;
}

static int
otlp_number_point(WreathBytesWriter *writer, const OtlpMetric *metric,
                  uint64_t start_ns, uint64_t now_ns, int error)
{
    OtlpFrame point;
    uint64_t value = error ? metric->errors_raw : metric->count_raw;
    if (otlp_begin(writer, 1, &point) < 0 ||
        otlp_fixed64(writer, 2, start_ns, 0) < 0 ||
        otlp_fixed64(writer, 3, now_ns, 0) < 0 ||
        otlp_point_attributes(writer, 7, metric, error) < 0 ||
        otlp_fixed64(writer, 6, value, 1) < 0 ||
        otlp_end(writer, &point) < 0) return -1;
    return 0;
}

static int
otlp_histogram_point(WreathBytesWriter *writer, const OtlpMetric *metric,
                     uint64_t start_ns, uint64_t now_ns)
{
    OtlpFrame point, positive, packed;
    Py_ssize_t first = -1, last = -1;
    if (otlp_begin(writer, 1, &point) < 0 ||
        otlp_fixed64(writer, 2, start_ns, 0) < 0 ||
        otlp_fixed64(writer, 3, now_ns, 0) < 0 ||
        otlp_point_attributes(writer, 1, metric, 0) < 0) return -1;
    for (Py_ssize_t index = 0;
         index < PySequence_Fast_GET_SIZE(metric->buckets); index++) {
        int truth = PyObject_IsTrue(PySequence_Fast_GET_ITEM(metric->buckets, index));
        if (truth < 0) return -1;
        if (truth) {
            if (first < 0) first = index;
            last = index;
        }
    }
    if (otlp_fixed64(writer, 4, metric->count_raw, 0) < 0 ||
        otlp_double(writer, 5, metric->duration_sum) < 0 ||
        otlp_double(writer, 13, metric->duration_max) < 0 ||
        otlp_begin(writer, 8, &positive) < 0) return -1;
    if (first > 0 && (pb_put_tag(writer, 1, PB_WIRE_VARINT) < 0 ||
                      pb_put_varint(writer, pb_zigzag((uint64_t)first,
                                                     PB_KIND_SINT32)) < 0)) return -1;
    if (first >= 0) {
        if (otlp_begin(writer, 2, &packed) < 0) return -1;
        for (Py_ssize_t index = first; index <= last; index++) {
            uint64_t value;
            if (pb_read_int(PySequence_Fast_GET_ITEM(metric->buckets, index),
                            PB_KIND_UINT64, &value) < 0 ||
                pb_put_varint(writer, value) < 0) return -1;
        }
        if (otlp_end(writer, &packed) < 0) return -1;
    }
    if (otlp_end(writer, &positive) < 0 || otlp_end(writer, &point) < 0) return -1;
    return 0;
}

PyObject *
wreath_protobuf_encode_otlp_metrics(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *snapshot, *image, *start_object, *now_object, *attributes;
    PyObject *routes_object = NULL, *routes = NULL, *paths = NULL;
    OtlpMetric *metrics = NULL;
    Py_ssize_t count = 0;
    uint64_t start_ns, now_ns;
    WreathBytesWriter writer = {0};
    OtlpFrame request, scope_metrics, scope, metric, sum, histogram;
    if (!PyArg_ParseTuple(args, "OOOOO:protobuf_encode_otlp_metrics",
                          &snapshot, &image, &start_object, &now_object,
                          &attributes)) return NULL;
    if (pb_read_int(start_object, PB_KIND_FIXED64, &start_ns) < 0 ||
        pb_read_int(now_object, PB_KIND_FIXED64, &now_ns) < 0) return NULL;
    routes_object = PyObject_GetAttrString(snapshot, "routes");
    if (routes_object == NULL) return NULL;
    routes = PySequence_Fast(routes_object, "snapshot routes must be a sequence");
    Py_DECREF(routes_object);
    if (routes == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(routes);
    if (count == 0) {
        Py_DECREF(routes);
        return PyBytes_FromStringAndSize(NULL, 0);
    }
    paths = otlp_route_paths(image);
    metrics = PyMem_Calloc((size_t)count, sizeof(*metrics));
    if (paths == NULL || metrics == NULL) {
        if (metrics == NULL) PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *route = PySequence_Fast_GET_ITEM(routes, index);
        PyObject *count_object = NULL, *errors_object = NULL;
        PyObject *sum_object = NULL, *max_object = NULL, *buckets_object = NULL;
        metrics[index].route_id = PyObject_GetAttrString(route, "route_id");
        count_object = PyObject_GetAttrString(route, "count");
        errors_object = PyObject_GetAttrString(route, "errors");
        sum_object = PyObject_GetAttrString(route, "duration_us_sum");
        max_object = PyObject_GetAttrString(route, "duration_us_max");
        buckets_object = PyObject_GetAttrString(route, "buckets");
        if (metrics[index].route_id == NULL || count_object == NULL ||
            errors_object == NULL || sum_object == NULL || max_object == NULL ||
            buckets_object == NULL ||
            pb_read_int(metrics[index].route_id, PB_KIND_INT64,
                        &metrics[index].route_raw) < 0 ||
            pb_read_int(count_object, PB_KIND_SFIXED64,
                        &metrics[index].count_raw) < 0 ||
            pb_read_int(count_object, PB_KIND_FIXED64,
                        &metrics[index].count_raw) < 0 ||
            pb_read_int(errors_object, PB_KIND_SFIXED64,
                        &metrics[index].errors_raw) < 0) {
            Py_XDECREF(buckets_object); Py_XDECREF(max_object);
            Py_XDECREF(sum_object); Py_XDECREF(errors_object);
            Py_XDECREF(count_object); goto error;
        }
        metrics[index].duration_sum = PyFloat_AsDouble(sum_object);
        metrics[index].duration_max = PyFloat_AsDouble(max_object);
        Py_DECREF(max_object); Py_DECREF(sum_object);
        Py_DECREF(errors_object); Py_DECREF(count_object);
        if (PyErr_Occurred()) { Py_DECREF(buckets_object); goto error; }
        metrics[index].buckets = PySequence_Fast(
            buckets_object, "route buckets must be a sequence");
        Py_DECREF(buckets_object);
        if (metrics[index].buckets == NULL) goto error;
        {
            PyObject *path;
            if (PyDict_GetItemRef(paths, metrics[index].route_id, &path) < 0) goto error;
            metrics[index].route_path = path;
        }
    }
    if (count > (PY_SSIZE_T_MAX / 256)) {
        PyErr_SetString(PyExc_OverflowError, "too many OTLP metric routes");
        goto error;
    }
    if (wreath_writer_init(&writer, count > 32 ? count * 256 : 8192) < 0 ||
        otlp_begin(&writer, 1, &request) < 0 ||
        otlp_resource(&writer, attributes) < 0 ||
        otlp_begin(&writer, 2, &scope_metrics) < 0 ||
        otlp_begin(&writer, 1, &scope) < 0 ||
        otlp_string(&writer, 1, "wreath.flight", 13) < 0 ||
        otlp_end(&writer, &scope) < 0 ||
        otlp_begin(&writer, 2, &metric) < 0 ||
        otlp_string(&writer, 1, "http.server.request.count", 25) < 0 ||
        otlp_string(&writer, 3, "{request}", 9) < 0 ||
        otlp_begin(&writer, 7, &sum) < 0 ||
        pb_put_tag(&writer, 2, PB_WIRE_VARINT) < 0 || pb_put_varint(&writer, 2) < 0 ||
        pb_put_tag(&writer, 3, PB_WIRE_VARINT) < 0 || pb_put_varint(&writer, 1) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < count; index++) {
        if (otlp_number_point(&writer, &metrics[index], start_ns, now_ns, 0) < 0 ||
            otlp_number_point(&writer, &metrics[index], start_ns, now_ns, 1) < 0)
            goto error;
    }
    if (otlp_end(&writer, &sum) < 0 || otlp_end(&writer, &metric) < 0 ||
        otlp_begin(&writer, 2, &metric) < 0 ||
        otlp_string(&writer, 1, "http.server.request.duration", 28) < 0 ||
        otlp_string(&writer, 3, "us", 2) < 0 ||
        otlp_begin(&writer, 10, &histogram) < 0 ||
        pb_put_tag(&writer, 2, PB_WIRE_VARINT) < 0 || pb_put_varint(&writer, 2) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < count; index++)
        if (otlp_histogram_point(
                &writer, &metrics[index], start_ns, now_ns) < 0) goto error;
    if (otlp_end(&writer, &histogram) < 0 || otlp_end(&writer, &metric) < 0 ||
        otlp_end(&writer, &scope_metrics) < 0 || otlp_end(&writer, &request) < 0)
        goto error;
    otlp_metrics_clear(metrics, count);
    Py_DECREF(paths);
    Py_DECREF(routes);
    return wreath_writer_finish(&writer);
error:
    Py_XDECREF(writer.bytes);
    otlp_metrics_clear(metrics, count);
    Py_XDECREF(paths);
    Py_XDECREF(routes);
    return NULL;
}

static uint64_t
otlp_mix64(uint64_t value)
{
    value = (value ^ (value >> 30)) * UINT64_C(0xBF58476D1CE4E5B9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94D049BB133111EB);
    return value ^ (value >> 31);
}

static int
otlp_bytes(WreathBytesWriter *writer, uint64_t field,
           const uint8_t *data, Py_ssize_t length)
{
    return pb_put_tag(writer, field, PB_WIRE_LEN) < 0 ||
           pb_put_varint(writer, (uint64_t)length) < 0 ||
           pb_write(writer, (const char *)data, length) < 0 ? -1 : 0;
}

static int
otlp_long_bytes(WreathBytesWriter *writer, uint64_t field,
                PyObject *number, Py_ssize_t length)
{
    uint8_t data[16];
    Py_ssize_t required = PyLong_AsNativeBytes(
        number, data, length,
        Py_ASNATIVEBYTES_BIG_ENDIAN | Py_ASNATIVEBYTES_UNSIGNED_BUFFER);
    if (required < 0 && PyErr_Occurred() != NULL) return -1;
    if (required < 0) {
        PyErr_SetString(PyExc_SystemError,
                        "integer byte conversion failed without an exception");
        return -1;
    }
    return otlp_bytes(writer, field, data, length);
}

static int
otlp_u64_bytes(WreathBytesWriter *writer, uint64_t field, uint64_t value)
{
    uint8_t data[8];
    for (int index = 7; index >= 0; index--) {
        data[index] = (uint8_t)value;
        value >>= 8;
    }
    return otlp_bytes(writer, field, data, 8);
}

static PyObject *
otlp_enum_lower(PyObject *value)
{
    PyObject *name = PyObject_GetAttrString(value, "name");
    PyObject *lower;
    if (name == NULL) return NULL;
    lower = PyObject_CallMethod(name, "lower", NULL);
    Py_DECREF(name);
    return lower;
}

static int
otlp_trace_maps(PyObject *image, PyObject **routes_out, PyObject **names_out)
{
    PyObject *routes = PyDict_New();
    PyObject *names = PyDict_New();
    PyObject *rows_object = NULL, *rows = NULL;
    if (routes == NULL || names == NULL) goto error;
    if (image == Py_None) {
        *routes_out = routes;
        *names_out = names;
        return 0;
    }
    rows_object = PyObject_GetAttrString(image, "routes");
    if (rows_object == NULL) goto error;
    rows = PySequence_Fast(rows_object, "metadata routes must be a sequence");
    Py_CLEAR(rows_object);
    if (rows == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(rows); index++) {
        PyObject *row = PySequence_Fast_GET_ITEM(rows, index);
        PyObject *route_id = PyObject_GetAttrString(row, "route_id");
        if (route_id == NULL || PyDict_SetItem(routes, route_id, row) < 0) {
            Py_XDECREF(route_id);
            goto error;
        }
        Py_DECREF(route_id);
    }
    Py_CLEAR(rows);
    {
        const char *tables[] = {"databases", "clients", "dependencies"};
        for (int table = 0; table < 3; table++) {
            rows_object = PyObject_GetAttrString(image, tables[table]);
            if (rows_object == NULL) goto error;
            rows = PySequence_Fast(rows_object, "metadata name table must be a sequence");
            Py_CLEAR(rows_object);
            if (rows == NULL) goto error;
            for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(rows); index++) {
                PyObject *row = PySequence_Fast_GET_ITEM(rows, index);
                PyObject *entry_id = PyObject_GetAttrString(row, "entry_id");
                int contains;
                if (entry_id == NULL) goto error;
                contains = PyDict_Contains(names, entry_id);
                if (contains < 0) { Py_DECREF(entry_id); goto error; }
                if (!contains) {
                    PyObject *name = PyObject_GetAttrString(row, "name");
                    if (name == NULL || PyDict_SetItem(names, entry_id, name) < 0) {
                        Py_XDECREF(name); Py_DECREF(entry_id); goto error;
                    }
                    Py_DECREF(name);
                }
                Py_DECREF(entry_id);
            }
            Py_CLEAR(rows);
        }
    }
    *routes_out = routes;
    *names_out = names;
    return 0;
error:
    Py_XDECREF(rows);
    Py_XDECREF(rows_object);
    Py_XDECREF(names);
    Py_XDECREF(routes);
    return -1;
}

static int
otlp_attr_raw(WreathBytesWriter *writer, uint64_t field,
              const char *key, PyObject *value)
{
    uint64_t raw;
    return pb_read_int(value, PB_KIND_INT64, &raw) < 0
        ? -1 : otlp_attribute_int(writer, field, key, raw);
}

static int
otlp_server_span(WreathBytesWriter *writer, PyObject *trace,
                 PyObject *routes, PyObject **trace_id_out,
                 PyObject **span_id_out, uint64_t *span_raw_out,
                 uint64_t *parent_start_out)
{
    PyObject *effective = NULL, *route_id = NULL, *plan_id = NULL;
    PyObject *terminal = NULL, *terminal_text = NULL, *protocol_object = NULL;
    PyObject *bytes_in = NULL, *bytes_out = NULL, *status = NULL;
    PyObject *error_class = NULL, *parent_span = NULL, *duration = NULL;
    PyObject *observed = NULL, *route = NULL, *name = NULL;
    PyObject *method = NULL, *path = NULL, *failure_object = NULL;
    OtlpFrame span, status_message;
    uint64_t end, duration_us, start, span_raw, parent_raw = 0;
    long protocol, status_code, error_code;
    int failure;
    effective = PyObject_GetAttrString(trace, "effective_ids");
    route_id = PyObject_GetAttrString(trace, "route_id");
    plan_id = PyObject_GetAttrString(trace, "plan_id");
    terminal = PyObject_GetAttrString(trace, "terminal");
    protocol_object = PyObject_GetAttrString(trace, "protocol");
    bytes_in = PyObject_GetAttrString(trace, "bytes_in");
    bytes_out = PyObject_GetAttrString(trace, "bytes_out");
    status = PyObject_GetAttrString(trace, "status");
    error_class = PyObject_GetAttrString(trace, "error_class");
    parent_span = PyObject_GetAttrString(trace, "parent_span_id");
    duration = PyObject_GetAttrString(trace, "duration_us");
    observed = PyObject_GetAttrString(trace, "observed_unix_nano");
    failure_object = PyObject_GetAttrString(trace, "is_failure");
    if (effective == NULL || !PyTuple_Check(effective) ||
        PyTuple_GET_SIZE(effective) != 2 || route_id == NULL || plan_id == NULL ||
        terminal == NULL || protocol_object == NULL || bytes_in == NULL ||
        bytes_out == NULL || status == NULL || error_class == NULL ||
        parent_span == NULL || duration == NULL || observed == NULL ||
        failure_object == NULL) goto error;
    *trace_id_out = Py_NewRef(PyTuple_GET_ITEM(effective, 0));
    *span_id_out = Py_NewRef(PyTuple_GET_ITEM(effective, 1));
    if (pb_read_int(*span_id_out, PB_KIND_UINT64, &span_raw) < 0 ||
        pb_read_int(parent_span, PB_KIND_UINT64, &parent_raw) < 0 ||
        pb_read_int(duration, PB_KIND_UINT64, &duration_us) < 0 ||
        pb_read_int(observed, PB_KIND_FIXED64, &end) < 0) goto error;
    if (duration_us > end / 1000U) {
        PyErr_SetString(PyExc_ValueError, "trace start timestamp is negative");
        goto error;
    }
    start = end - duration_us * 1000U;
    protocol = PyLong_AsLong(protocol_object);
    status_code = PyLong_AsLong(status);
    error_code = PyLong_AsLong(error_class);
    failure = PyObject_IsTrue(failure_object);
    terminal_text = otlp_enum_lower(terminal);
    if (PyErr_Occurred() || failure < 0 || terminal_text == NULL) goto error;
    if (PyDict_GetItemRef(routes, route_id, &route) < 0) goto error;
    if (route != NULL) {
        method = PyObject_GetAttrString(route, "method");
        path = PyObject_GetAttrString(route, "path");
        if (method == NULL || path == NULL) goto error;
        name = PyUnicode_FromFormat("%U %U", method, path);
    } else {
        name = PyUnicode_FromString(protocol == 4 ? "WEBSOCKET" : "HTTP");
    }
    if (name == NULL || otlp_begin(writer, 2, &span) < 0 ||
        otlp_long_bytes(writer, 1, *trace_id_out, 16) < 0 ||
        otlp_long_bytes(writer, 2, *span_id_out, 8) < 0 ||
        otlp_unicode(writer, 5, name) < 0 ||
        pb_put_tag(writer, 6, PB_WIRE_VARINT) < 0 || pb_put_varint(writer, 2) < 0 ||
        otlp_fixed64(writer, 7, start, 0) < 0 ||
        otlp_fixed64(writer, 8, end, 0) < 0 ||
        otlp_attr_raw(writer, 9, "wreath.route_id", route_id) < 0 ||
        otlp_attr_raw(writer, 9, "wreath.plan_id", plan_id) < 0 ||
        otlp_attribute_string(writer, 9, "wreath.terminal", 15, terminal_text) < 0 ||
        otlp_attr_raw(writer, 9, "http.request.body.size", bytes_in) < 0 ||
        otlp_attr_raw(writer, 9, "http.response.body.size", bytes_out) < 0)
        goto error;
    if (protocol == 4) {
        if (otlp_attribute_text(
                writer, 9, "network.protocol.name", "websocket") < 0) goto error;
    } else {
        const char *version = protocol == 1 ? "1.1" : protocol == 2 ? "2" :
                              protocol == 3 ? "3" : NULL;
        if (otlp_attribute_text(writer, 9, "network.protocol.name", "http") < 0 ||
            (version != NULL && otlp_attribute_text(
                writer, 9, "network.protocol.version", version) < 0)) goto error;
    }
    if (status_code != 0 && otlp_attr_raw(
            writer, 9, "http.response.status_code", status) < 0) goto error;
    if (route != NULL && (otlp_attribute_string(
            writer, 9, "http.request.method", 19, method) < 0 ||
        otlp_attribute_string(writer, 9, "http.route", 10, path) < 0)) goto error;
    if (error_code != 0) {
        PyObject *error_text = PyUnicode_FromFormat("class:%ld", error_code);
        int failed = error_text == NULL || otlp_attribute_string(
            writer, 9, "error.type", 10, error_text) < 0;
        Py_XDECREF(error_text);
        if (failed) goto error;
    }
    if (parent_raw != 0 && otlp_long_bytes(writer, 4, parent_span, 8) < 0) goto error;
    if (otlp_begin(writer, 15, &status_message) < 0) goto error;
    if (failure && (pb_put_tag(writer, 3, PB_WIRE_VARINT) < 0 ||
                    pb_put_varint(writer, 2) < 0 ||
                    otlp_unicode(writer, 2, terminal_text) < 0)) goto error;
    if (otlp_end(writer, &status_message) < 0 || otlp_end(writer, &span) < 0) goto error;
    *span_raw_out = span_raw;
    *parent_start_out = start;
    Py_DECREF(failure_object); Py_DECREF(observed);
    Py_DECREF(duration); Py_DECREF(parent_span); Py_DECREF(error_class);
    Py_DECREF(status); Py_DECREF(bytes_out); Py_DECREF(bytes_in);
    Py_DECREF(protocol_object); Py_DECREF(terminal_text); Py_DECREF(terminal);
    Py_DECREF(plan_id); Py_DECREF(route_id); Py_DECREF(effective);
    Py_XDECREF(path); Py_XDECREF(method); Py_XDECREF(name); Py_XDECREF(route);
    return 0;
error:
    Py_XDECREF(*span_id_out); *span_id_out = NULL;
    Py_XDECREF(*trace_id_out); *trace_id_out = NULL;
    Py_XDECREF(failure_object); Py_XDECREF(observed);
    Py_XDECREF(duration); Py_XDECREF(parent_span); Py_XDECREF(error_class);
    Py_XDECREF(status); Py_XDECREF(bytes_out); Py_XDECREF(bytes_in);
    Py_XDECREF(protocol_object); Py_XDECREF(terminal_text); Py_XDECREF(terminal);
    Py_XDECREF(plan_id); Py_XDECREF(route_id); Py_XDECREF(effective);
    Py_XDECREF(path); Py_XDECREF(method); Py_XDECREF(name); Py_XDECREF(route);
    return -1;
}

static int
otlp_phase_span(WreathBytesWriter *writer, PyObject *phase,
                PyObject *trace_id, uint64_t parent_span,
                uint64_t parent_start, PyObject *dependency_names)
{
    PyObject *phase_id = NULL, *phase_text = NULL, *coverage = NULL;
    PyObject *coverage_text = NULL, *dependency = NULL, *sequence = NULL;
    PyObject *start_offset = NULL, *duration = NULL, *dependency_name = NULL;
    OtlpFrame span;
    uint64_t dependency_raw, sequence_raw, offset_raw, duration_raw;
    uint64_t start, end, child;
    long phase_kind;
    phase_id = PyObject_GetAttrString(phase, "phase_id");
    coverage = PyObject_GetAttrString(phase, "coverage");
    dependency = PyObject_GetAttrString(phase, "dependency_id");
    sequence = PyObject_GetAttrString(phase, "sequence");
    start_offset = PyObject_GetAttrString(phase, "start_offset_us");
    duration = PyObject_GetAttrString(phase, "duration_us");
    if (phase_id == NULL || coverage == NULL || dependency == NULL || sequence == NULL ||
        start_offset == NULL || duration == NULL ||
        pb_read_int(dependency, PB_KIND_UINT64, &dependency_raw) < 0 ||
        pb_read_int(sequence, PB_KIND_UINT64, &sequence_raw) < 0 ||
        pb_read_int(start_offset, PB_KIND_UINT64, &offset_raw) < 0 ||
        pb_read_int(duration, PB_KIND_UINT64, &duration_raw) < 0) goto error;
    if (offset_raw > (UINT64_MAX - parent_start) / 1000U) {
        PyErr_SetString(PyExc_ValueError, "phase start timestamp overflows fixed64");
        goto error;
    }
    start = parent_start + offset_raw * 1000U;
    if (duration_raw > (UINT64_MAX - start) / 1000U) {
        PyErr_SetString(PyExc_ValueError, "phase end timestamp overflows fixed64");
        goto error;
    }
    end = start + duration_raw * 1000U;
    phase_kind = PyLong_AsLong(phase_id);
    phase_text = otlp_enum_lower(phase_id);
    coverage_text = otlp_enum_lower(coverage);
    if (PyErr_Occurred() || phase_text == NULL || coverage_text == NULL) goto error;
    child = otlp_mix64(parent_span ^
        (UINT64_C(0x9E3779B97F4A7C15) * (sequence_raw + 1U)));
    if (child == 0) child = 1;
    if (dependency_raw != 0 &&
        PyDict_GetItemRef(dependency_names, dependency, &dependency_name) < 0) goto error;
    if (otlp_begin(writer, 2, &span) < 0 ||
        otlp_long_bytes(writer, 1, trace_id, 16) < 0 ||
        otlp_u64_bytes(writer, 2, child) < 0 ||
        otlp_u64_bytes(writer, 4, parent_span) < 0 ||
        otlp_unicode(writer, 5, phase_text) < 0 ||
        pb_put_tag(writer, 6, PB_WIRE_VARINT) < 0 ||
        pb_put_varint(writer, phase_kind >= 7 && phase_kind <= 10 ? 3 : 1) < 0 ||
        otlp_fixed64(writer, 7, start, 0) < 0 ||
        otlp_fixed64(writer, 8, end, 0) < 0 ||
        otlp_attribute_string(writer, 9, "wreath.phase", 12, phase_text) < 0 ||
        otlp_attribute_string(writer, 9, "wreath.coverage", 15, coverage_text) < 0)
        goto error;
    if (dependency_raw != 0 && (otlp_attribute_int(
            writer, 9, "wreath.dependency_id", dependency_raw) < 0 ||
        (dependency_name != NULL && otlp_attribute_string(
            writer, 9, "wreath.dependency", 17, dependency_name) < 0))) goto error;
    if (otlp_end(writer, &span) < 0) goto error;
    Py_XDECREF(dependency_name); Py_DECREF(duration); Py_DECREF(start_offset);
    Py_DECREF(sequence); Py_DECREF(dependency); Py_DECREF(coverage_text);
    Py_DECREF(coverage); Py_DECREF(phase_text); Py_DECREF(phase_id);
    return 0;
error:
    Py_XDECREF(dependency_name); Py_XDECREF(duration); Py_XDECREF(start_offset);
    Py_XDECREF(sequence); Py_XDECREF(dependency); Py_XDECREF(coverage_text);
    Py_XDECREF(coverage); Py_XDECREF(phase_text); Py_XDECREF(phase_id);
    return -1;
}

PyObject *
wreath_protobuf_encode_otlp_traces(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *traces_object, *image, *attributes;
    PyObject *traces = NULL, *routes = NULL, *dependency_names = NULL;
    WreathBytesWriter writer = {0};
    OtlpFrame resource_spans, scope_spans, scope;
    Py_ssize_t count;
    if (!PyArg_ParseTuple(args, "OOO:protobuf_encode_otlp_traces",
                          &traces_object, &image, &attributes)) return NULL;
    traces = PySequence_Fast(traces_object, "traces must be a sequence");
    if (traces == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(traces);
    if (count == 0) { Py_DECREF(traces); return PyBytes_FromStringAndSize(NULL, 0); }
    if (otlp_trace_maps(image, &routes, &dependency_names) < 0) goto error;
    if (count > PY_SSIZE_T_MAX / 1024) {
        PyErr_SetString(PyExc_OverflowError, "too many OTLP traces");
        goto error;
    }
    if (wreath_writer_init(&writer, count > 8 ? count * 1024 : 8192) < 0 ||
        otlp_begin(&writer, 1, &resource_spans) < 0 ||
        otlp_resource(&writer, attributes) < 0 ||
        otlp_begin(&writer, 2, &scope_spans) < 0 ||
        otlp_begin(&writer, 1, &scope) < 0 ||
        otlp_string(&writer, 1, "wreath.flight", 13) < 0 ||
        otlp_end(&writer, &scope) < 0) goto error;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *trace = PySequence_Fast_GET_ITEM(traces, index);
        PyObject *trace_id = NULL, *span_id = NULL;
        PyObject *phases_object = NULL, *phases = NULL;
        uint64_t span_raw, parent_start;
        if (otlp_server_span(&writer, trace, routes, &trace_id, &span_id,
                             &span_raw, &parent_start) < 0) goto error;
        phases_object = PyObject_GetAttrString(trace, "phases");
        if (phases_object == NULL) {
            Py_DECREF(span_id); Py_DECREF(trace_id); goto error;
        }
        phases = PySequence_Fast(phases_object, "trace phases must be a sequence");
        Py_DECREF(phases_object);
        if (phases == NULL) { Py_DECREF(span_id); Py_DECREF(trace_id); goto error; }
        for (Py_ssize_t phase = 0; phase < PySequence_Fast_GET_SIZE(phases); phase++) {
            if (otlp_phase_span(&writer, PySequence_Fast_GET_ITEM(phases, phase),
                                trace_id, span_raw, parent_start,
                                dependency_names) < 0) {
                Py_DECREF(phases); Py_DECREF(span_id); Py_DECREF(trace_id); goto error;
            }
        }
        Py_DECREF(phases); Py_DECREF(span_id); Py_DECREF(trace_id);
    }
    if (otlp_end(&writer, &scope_spans) < 0 ||
        otlp_end(&writer, &resource_spans) < 0) goto error;
    Py_DECREF(dependency_names); Py_DECREF(routes); Py_DECREF(traces);
    return wreath_writer_finish(&writer);
error:
    Py_XDECREF(writer.bytes);
    Py_XDECREF(dependency_names); Py_XDECREF(routes); Py_XDECREF(traces);
    return NULL;
}

static PyObject *
otlp_log_display(PyObject *argument, int known)
{
    PyObject *type = PyObject_GetAttrString(argument, "type");
    long kind;
    if (type == NULL) return NULL;
    kind = PyLong_AsLong(type);
    Py_DECREF(type);
    if (kind == -1 && PyErr_Occurred()) return NULL;
    if (kind == 4) return PyObject_GetAttrString(argument, "text_value");
    if (kind == 2) return PyObject_GetAttrString(argument, "number");
    if (kind == 3) return PyObject_GetAttrString(argument, "fraction");
    if (kind == 1) {
        PyObject *number = PyObject_GetAttrString(argument, "number");
        int truth;
        if (number == NULL) return NULL;
        truth = PyObject_IsTrue(number);
        Py_DECREF(number);
        return truth < 0 ? NULL : Py_NewRef(truth ? Py_True : Py_False);
    }
    if (kind == 5 || kind == 6) {
        PyObject *number = PyObject_GetAttrString(argument, "number");
        PyObject *text;
        unsigned long long value;
        if (number == NULL) return NULL;
        value = PyLong_AsUnsignedLongLong(number);
        Py_DECREF(number);
        if (PyErr_Occurred()) return NULL;
        text = kind == 5
            ? PyUnicode_FromFormat("#%016llx", value)
            : PyUnicode_FromFormat("<%llu bytes>", value);
        return text;
    }
    return known ? PyUnicode_FromString("?") : Py_NewRef(Py_None);
}

static int
otlp_attribute_object(WreathBytesWriter *writer, uint64_t field,
                      PyObject *key, PyObject *value)
{
    Py_ssize_t key_length;
    const char *key_data = PyUnicode_AsUTF8AndSize(key, &key_length);
    OtlpFrame attribute, any;
    if (key_data == NULL || otlp_begin(writer, field, &attribute) < 0 ||
        otlp_string(writer, 1, key_data, key_length) < 0 ||
        otlp_begin(writer, 2, &any) < 0) return -1;
    if (PyBool_Check(value)) {
        if (pb_put_tag(writer, 2, PB_WIRE_VARINT) < 0 ||
            pb_put_varint(writer, value == Py_True) < 0) return -1;
    } else if (PyLong_Check(value)) {
        uint64_t raw;
        if (pb_read_int(value, PB_KIND_INT64, &raw) < 0 ||
            pb_put_tag(writer, 3, PB_WIRE_VARINT) < 0 ||
            pb_put_varint(writer, raw) < 0) return -1;
    } else if (PyFloat_Check(value)) {
        if (otlp_double(writer, 4, PyFloat_AS_DOUBLE(value)) < 0) return -1;
    } else {
        PyObject *text = PyObject_Str(value);
        int failed = text == NULL || otlp_unicode(writer, 1, text) < 0;
        Py_XDECREF(text);
        if (failed) return -1;
    }
    return otlp_end(writer, &any) < 0 || otlp_end(writer, &attribute) < 0 ? -1 : 0;
}

static PyObject *
otlp_log_render(PyObject *site, PyObject *values)
{
    PyObject *template = PyObject_GetAttrString(site, "template");
    PyObject *method = NULL, *empty = NULL, *rendered = NULL;
    if (template == NULL) return NULL;
    method = PyObject_GetAttrString(template, "format");
    empty = PyTuple_New(0);
    if (method != NULL && empty != NULL)
        rendered = PyObject_Call(method, empty, values);
    Py_XDECREF(empty);
    Py_XDECREF(method);
    Py_DECREF(template);
    if (rendered != NULL) return rendered;
    if (!PyErr_ExceptionMatches(PyExc_IndexError) &&
        !PyErr_ExceptionMatches(PyExc_KeyError) &&
        !PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;
    PyErr_Clear();
    {
        PyObject *event_name = PyObject_GetAttrString(site, "event_name");
        if (event_name == NULL) return NULL;
        rendered = PyUnicode_FromFormat("%U %R", event_name, values);
        Py_DECREF(event_name);
    }
    return rendered;
}

static int
otlp_log_record(WreathBytesWriter *writer, PyObject *record, PyObject *sites)
{
    PyObject *cell = NULL, *site_id = NULL, *severity = NULL, *args_object = NULL;
    PyObject *arguments = NULL, *site = NULL, *fields_object = NULL, *fields = NULL;
    PyObject *values = NULL, *rendered = NULL, *observed = NULL;
    PyObject *trace_id = NULL, *span_id = NULL, *route_id = NULL;
    PyObject *dropped = NULL, *event_name = NULL;
    OtlpFrame log, body;
    Py_ssize_t site_index, argument_count, field_count = 0;
    uint64_t stamp, route_raw, dropped_raw;
    long severity_number;
    int trace_truth, span_truth, correlated;
    cell = PyObject_GetAttrString(record, "cell");
    observed = PyObject_GetAttrString(record, "observed_unix_nano");
    trace_id = PyObject_GetAttrString(record, "trace_id");
    span_id = PyObject_GetAttrString(record, "span_id");
    route_id = PyObject_GetAttrString(record, "route_id");
    if (cell == NULL || observed == NULL || trace_id == NULL ||
        span_id == NULL || route_id == NULL ||
        pb_read_int(observed, PB_KIND_FIXED64, &stamp) < 0 ||
        pb_read_int(route_id, PB_KIND_INT64, &route_raw) < 0) goto error;
    trace_truth = PyObject_IsTrue(trace_id);
    span_truth = PyObject_IsTrue(span_id);
    if (trace_truth < 0 || span_truth < 0) goto error;
    site_id = PyObject_GetAttrString(cell, "site_id");
    severity = PyObject_GetAttrString(cell, "severity");
    args_object = PyObject_GetAttrString(cell, "args");
    dropped = PyObject_GetAttrString(cell, "dropped_siblings");
    if (site_id == NULL || severity == NULL || args_object == NULL || dropped == NULL ||
        pb_read_int(dropped, PB_KIND_INT64, &dropped_raw) < 0) goto error;
    site_index = PyLong_AsSsize_t(site_id);
    severity_number = PyLong_AsLong(severity);
    if (PyErr_Occurred() || severity_number < INT32_MIN || severity_number > INT32_MAX) {
        if (!PyErr_Occurred()) PyErr_SetString(
            PyExc_ValueError, "log severity is outside int32");
        goto error;
    }
    if (site_index >= 1 && site_index <= PyList_GET_SIZE(sites))
        site = Py_NewRef(PyList_GET_ITEM(sites, site_index - 1));
    arguments = PySequence_Fast(args_object, "log arguments must be a sequence");
    if (arguments == NULL) goto error;
    argument_count = PySequence_Fast_GET_SIZE(arguments);
    if (site != NULL) {
        fields_object = PyObject_GetAttrString(site, "fields");
        if (fields_object == NULL) goto error;
        fields = PySequence_Fast(fields_object, "log fields must be a sequence");
        if (fields == NULL) goto error;
        field_count = PySequence_Fast_GET_SIZE(fields);
        event_name = PyObject_GetAttrString(site, "event_name");
        if (event_name == NULL) goto error;
    }
    values = PyDict_New();
    if (values == NULL) goto error;
    Py_ssize_t displayed = site == NULL || argument_count < field_count
        ? argument_count : field_count;
    for (Py_ssize_t index = 0; index < displayed; index++) {
        PyObject *key, *value;
        int known = site != NULL && index < field_count;
        if (known) {
            key = PyObject_GetAttrString(PySequence_Fast_GET_ITEM(fields, index), "name");
        } else {
            key = PyUnicode_FromFormat("arg%zd", index);
        }
        value = otlp_log_display(PySequence_Fast_GET_ITEM(arguments, index), known);
        if (key == NULL || value == NULL || PyDict_SetItem(values, key, value) < 0) {
            Py_XDECREF(value); Py_XDECREF(key); goto error;
        }
        Py_DECREF(value); Py_DECREF(key);
    }
    if (site != NULL) {
        for (Py_ssize_t index = 0; index < field_count; index++) {
            PyObject *key = PyObject_GetAttrString(
                PySequence_Fast_GET_ITEM(fields, index), "name");
            int contains;
            if (key == NULL) goto error;
            contains = PyDict_Contains(values, key);
            if (contains < 0) {
                Py_DECREF(key);
                goto error;
            }
            if (!contains) {
                PyObject *missing = PyUnicode_FromString("?");
                int failed = missing == NULL || PyDict_SetItem(values, key, missing) < 0;
                Py_XDECREF(missing);
                if (failed) { Py_DECREF(key); goto error; }
            }
            Py_DECREF(key);
        }
        rendered = otlp_log_render(site, values);
    } else {
        rendered = PyUnicode_FromFormat("<unknown log site %zd>", site_index);
    }
    if (rendered == NULL) goto error;
    correlated = trace_truth || span_truth;
    {
        long band = (severity_number - 1) / 4;
        const char *severity_texts[] = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"};
        const char *severity_text;
        if (severity_number <= 0) band = 0;
        if (band > 5) band = 5;
        severity_text = severity_texts[band];
        if (otlp_begin(writer, 2, &log) < 0 ||
            otlp_fixed64(writer, 1, stamp, 0) < 0 ||
            otlp_fixed64(writer, 11, stamp, 0) < 0 ||
            (severity_number != 0 && (pb_put_tag(writer, 2, PB_WIRE_VARINT) < 0 ||
                                      pb_put_varint(writer, (uint64_t)severity_number) < 0)) ||
            otlp_string(writer, 3, severity_text, (Py_ssize_t)strlen(severity_text)) < 0 ||
            otlp_begin(writer, 5, &body) < 0 ||
            otlp_unicode(writer, 1, rendered) < 0 || otlp_end(writer, &body) < 0)
            goto error;
    }
    if (event_name != NULL) {
        Py_ssize_t event_length;
        const char *event_data = PyUnicode_AsUTF8AndSize(event_name, &event_length);
        if (event_data == NULL || otlp_string(
                writer, 12, event_data, event_length) < 0) goto error;
    }
    if (correlated && (otlp_long_bytes(writer, 9, trace_id, 16) < 0 ||
                       otlp_long_bytes(writer, 10, span_id, 8) < 0)) goto error;
    {
        Py_ssize_t position = 0;
        PyObject *key, *value;
        while (PyDict_Next(values, &position, &key, &value))
            if (otlp_attribute_object(writer, 6, key, value) < 0) goto error;
    }
    if (dropped_raw != 0 && otlp_attribute_int(
            writer, 6, "wreath.dropped_siblings", dropped_raw) < 0) goto error;
    if (route_raw != 0 && otlp_attribute_int(
            writer, 6, "wreath.route_id", route_raw) < 0) goto error;
    if (otlp_end(writer, &log) < 0) goto error;
    Py_XDECREF(event_name); Py_DECREF(rendered); Py_DECREF(values);
    Py_XDECREF(fields); Py_XDECREF(fields_object); Py_XDECREF(site);
    Py_DECREF(arguments); Py_DECREF(dropped); Py_DECREF(args_object);
    Py_DECREF(severity); Py_DECREF(site_id); Py_DECREF(route_id);
    Py_DECREF(span_id); Py_DECREF(trace_id); Py_DECREF(observed); Py_DECREF(cell);
    return 0;
error:
    Py_XDECREF(event_name); Py_XDECREF(rendered); Py_XDECREF(values);
    Py_XDECREF(fields); Py_XDECREF(fields_object); Py_XDECREF(site);
    Py_XDECREF(arguments); Py_XDECREF(dropped); Py_XDECREF(args_object);
    Py_XDECREF(severity); Py_XDECREF(site_id); Py_XDECREF(route_id);
    Py_XDECREF(span_id); Py_XDECREF(trace_id); Py_XDECREF(observed); Py_XDECREF(cell);
    return -1;
}

PyObject *
wreath_protobuf_encode_otlp_logs(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *records_object, *registry, *attributes;
    PyObject *records = NULL, *sites = NULL;
    WreathBytesWriter writer = {0};
    OtlpFrame resource_logs, scope_logs, scope;
    Py_ssize_t count;
    if (!PyArg_ParseTuple(args, "OOO:protobuf_encode_otlp_logs",
                          &records_object, &registry, &attributes)) return NULL;
    records = PySequence_Fast(records_object, "log records must be a sequence");
    if (records == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(records);
    if (count == 0) { Py_DECREF(records); return PyBytes_FromStringAndSize(NULL, 0); }
    sites = PyObject_GetAttrString(registry, "_by_id");
    if (sites == NULL || !PyList_Check(sites)) {
        if (sites != NULL) PyErr_SetString(
            PyExc_TypeError, "log registry must own a site list");
        goto error;
    }
    if (count > PY_SSIZE_T_MAX / 512) {
        PyErr_SetString(PyExc_OverflowError, "too many OTLP log records");
        goto error;
    }
    if (wreath_writer_init(&writer, count > 16 ? count * 512 : 8192) < 0 ||
        otlp_begin(&writer, 1, &resource_logs) < 0 ||
        otlp_resource(&writer, attributes) < 0 ||
        otlp_begin(&writer, 2, &scope_logs) < 0 ||
        otlp_begin(&writer, 1, &scope) < 0 ||
        otlp_string(&writer, 1, "wreath.flight", 13) < 0 ||
        otlp_end(&writer, &scope) < 0) goto error;
    for (Py_ssize_t index = 0; index < count; index++)
        if (otlp_log_record(
                &writer, PySequence_Fast_GET_ITEM(records, index), sites) < 0) goto error;
    if (otlp_end(&writer, &scope_logs) < 0 ||
        otlp_end(&writer, &resource_logs) < 0) goto error;
    Py_DECREF(sites); Py_DECREF(records);
    return wreath_writer_finish(&writer);
error:
    Py_XDECREF(writer.bytes); Py_XDECREF(sites); Py_XDECREF(records);
    return NULL;
}
