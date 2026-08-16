#include "codec.h"

#include "../simd.h"
#include "../byteorder.h"
#include "../sparse_vector.h"

#include "buffer.h"

#include <datetime.h>
#include <limits.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

/* The nibble table and the decoder live in `simd.h`, which picks a width per
 * call: a `bytea` column arrives as two characters per byte, so the scan is as
 * long as the value is wide. */

PyObject *
wreath_pg_decode_hex_bytea(const unsigned char *data, Py_ssize_t length)
{
    PyObject *result;
    char *out;

    if (length < 0 || (length & 1) != 0) {
        PyErr_SetString(PyExc_ValueError,
                        "bytea hex data has an odd number of digits");
        return NULL;
    }
    if (length == 0) {
        return PyBytes_FromStringAndSize("", 0);
    }
    /* length is even and non-negative, so length / 2 cannot overflow. */
    result = PyBytes_FromStringAndSize(NULL, length / 2);  /* exact, allocated once */
    if (result == NULL) {
        return NULL;
    }
    out = PyBytes_AS_STRING(result);
    /* The odd-length case is already refused above, so -1 here can only mean a
     * byte that is not a hex digit -- the two errors stay distinguishable
     * without the decoder having to report which it was. */
    if (wreath_hex_decode((const char *)data, (ptrdiff_t)length,
                          (unsigned char *)out) < 0) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError,
                        "bytea hex data contains a non-hexadecimal digit");
        return NULL;
    }
    return result;
}

#define PG_BOOL 16
#define PG_BYTEA 17
#define PG_INT8 20
#define PG_INT2 21
#define PG_INT4 23
#define PG_TEXT 25
#define PG_JSON 114
#define PG_FLOAT4 700
#define PG_FLOAT8 701
#define PG_VARCHAR 1043
#define PG_BIT 1560
/* Core `point`, OID 600. Not an extension type -- the catalog allocates it --
   so it is a case here rather than a registration-table lookup. */
#define PG_POINT 600
#define PG_DATE 1082
#define PG_TIMESTAMP 1114
#define PG_TIMESTAMPTZ 1184
#define PG_NUMERIC 1700
#define PG_UUID 2950
#define PG_JSONB 3802

/* `numeric` is base-10000 digit groups plus a sign and a display scale, which
   is what lets it hold values no float can. Both twins land it on `Decimal`;
   `float` was the wrong type, not merely a lossy one, because it collapses
   distinct values onto one. The heavy lifting is delegated to Python's decimal
   module rather than reimplemented here: a second arbitrary-precision decimal
   implementation in C would be a parity liability for no measured gain. */
#define PG_NUMERIC_POS 0x0000
#define PG_NUMERIC_NEG 0x4000
#define PG_NUMERIC_NAN 0xC000
#define PG_NUMERIC_PINF 0xD000
#define PG_NUMERIC_NINF 0xF000

/* One-dimensional array OIDs, and the map back to their element OID. An array
   reuses its element's scalar codec value-by-value, so no per-type array code
   is needed beyond the shared binary framing below. */
#define PG_BOOL_ARRAY 1000
#define PG_BYTEA_ARRAY 1001
#define PG_INT8_ARRAY 1016
#define PG_INT2_ARRAY 1005
#define PG_INT4_ARRAY 1007
#define PG_TEXT_ARRAY 1009
#define PG_JSON_ARRAY 199
#define PG_FLOAT4_ARRAY 1021
#define PG_FLOAT8_ARRAY 1022
#define PG_VARCHAR_ARRAY 1015
#define PG_DATE_ARRAY 1182
#define PG_TIMESTAMP_ARRAY 1115
#define PG_TIMESTAMPTZ_ARRAY 1185
#define PG_NUMERIC_ARRAY 1231
#define PG_UUID_ARRAY 2951
#define PG_JSONB_ARRAY 3807

/* The element OID for an array OID, or 0 when the OID is not a supported array.
 * TODO: `wreath._pgdriver` has no array codec, so `Array(...)` columns need a
 * build with `_postgres`. */
static uint32_t
array_element_oid(uint32_t oid)
{
    switch (oid) {
    case PG_BOOL_ARRAY: return PG_BOOL;
    case PG_BYTEA_ARRAY: return PG_BYTEA;
    case PG_INT8_ARRAY: return PG_INT8;
    case PG_INT2_ARRAY: return PG_INT2;
    case PG_INT4_ARRAY: return PG_INT4;
    case PG_TEXT_ARRAY: return PG_TEXT;
    case PG_JSON_ARRAY: return PG_JSON;
    case PG_FLOAT4_ARRAY: return PG_FLOAT4;
    case PG_FLOAT8_ARRAY: return PG_FLOAT8;
    case PG_VARCHAR_ARRAY: return PG_VARCHAR;
    case PG_DATE_ARRAY: return PG_DATE;
    case PG_TIMESTAMP_ARRAY: return PG_TIMESTAMP;
    case PG_TIMESTAMPTZ_ARRAY: return PG_TIMESTAMPTZ;
    case PG_NUMERIC_ARRAY: return PG_NUMERIC;
    case PG_UUID_ARRAY: return PG_UUID;
    case PG_JSONB_ARRAY: return PG_JSONB;
    default: return 0;
    }
}

/* --- extension types ------------------------------------------------------
 *
 * Every OID above is a compile-time constant and the switches dispatch on it
 * directly. An extension's OIDs cannot be: `CREATE EXTENSION vector` allocates
 * `vector`'s OID from the same sequence as everything else in that database, so
 * it differs between databases and `case PG_VECTOR:` cannot be written.
 *
 * So one bounded table, written at startup by `_register_extension_type` and
 * read-only afterwards. It is consulted from the `default:` arm of each switch
 * rather than ahead of it: every built-in OID is below 5000 and every extension
 * OID is above 16384, so reaching the table only after the built-ins have
 * missed is equivalent, and keeps the scan off the path a bigint takes.
 *
 * The scan is linear because the table holds at most WREATH_PG_EXT_MAX entries;
 * a hash over sixteen elements would cost more than it saved.
 * native-lint: allow NC001 -- bounded by WREATH_PG_EXT_MAX (16) and written
 * only at startup, so this is a fixed-cost lookup, not a growing one. */
#define WREATH_PG_EXT_MAX 16
#define WREATH_PG_EXT_VECTOR 1
#define WREATH_PG_EXT_HALFVEC 2
#define WREATH_PG_EXT_SPARSEVEC 3
#define WREATH_PG_EXT_GEOGRAPHY 4
#define WREATH_PG_EXT_NAME_MAX 63

/* pgvector's SPARSEVEC_MAX_NNZ, matching _sparsevec.py::MAX_SPARSEVEC_NNZ. A
 * decoder bounds the count before it trusts it as a length. */
#define WREATH_PG_SPARSEVEC_MAX_NNZ 16000

/* The largest finite IEEE-754 binary16. A halfvec element beyond it rounds to an
 * infinity, which pgvector refuses on the way in -- so the encode would succeed
 * and the INSERT would fail with a message naming neither the element nor the
 * column. Checked here, matching `_pgdriver._MAX_HALF`. */
#define WREATH_PG_MAX_HALF 65504.0

typedef struct {
    uint32_t oid;
    int kind;
    char name[WREATH_PG_EXT_NAME_MAX + 1];
} WreathPgExtensionType;

static WreathPgExtensionType extension_types[WREATH_PG_EXT_MAX];
static int extension_type_count = 0;

int
wreath_pg_extension_kind(uint32_t oid)
{
    for (int index = 0; index < extension_type_count; index++) {
        if (extension_types[index].oid == oid) return extension_types[index].kind;
    }
    return 0;
}

static PyObject *
codec_register_extension_type(PyObject *module, PyObject *args)
{
    const char *name;
    Py_ssize_t name_length;
    unsigned int oid;
    int kind;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "s#Ii:_register_extension_type", &name, &name_length, &oid, &kind))
        return NULL;
    if (kind != WREATH_PG_EXT_VECTOR && kind != WREATH_PG_EXT_HALFVEC &&
        kind != WREATH_PG_EXT_SPARSEVEC && kind != WREATH_PG_EXT_GEOGRAPHY) {
        PyErr_Format(PyExc_ValueError, "unknown extension codec kind %d for '%s'",
                     kind, name);
        return NULL;
    }
    if (oid == 0) {
        PyErr_Format(PyExc_ValueError, "invalid OID %u for extension type '%s'",
                     oid, name);
        return NULL;
    }
    if (name_length < 1 || name_length > WREATH_PG_EXT_NAME_MAX) {
        PyErr_SetString(PyExc_ValueError, "extension type name is out of range");
        return NULL;
    }
    /* Keyed by OID, not by name: one name can legitimately arrive at two OIDs
       (two databases each installing the extension), and what must never happen
       is one OID meaning two wire formats. That is the collision checked here. */
    for (int index = 0; index < extension_type_count; index++) {
        if (extension_types[index].oid != oid) continue;
        if (extension_types[index].kind != kind) {
            PyErr_Format(
                PyExc_ValueError,
                "OID %u is already registered as codec kind %d; re-registering it "
                "as %d for '%s' would decode live rows with the wrong codec",
                oid, extension_types[index].kind, kind, name);
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (extension_type_count >= WREATH_PG_EXT_MAX) {
        PyErr_Format(PyExc_ValueError,
                     "at most %d extension types can be registered",
                     WREATH_PG_EXT_MAX);
        return NULL;
    }
    memcpy(extension_types[extension_type_count].name, name, (size_t)name_length);
    extension_types[extension_type_count].name[name_length] = '\0';
    extension_types[extension_type_count].oid = oid;
    extension_types[extension_type_count].kind = kind;
    extension_type_count++;
    Py_RETURN_NONE;
}

/* Big-endian IEEE-754 binary32 <-> double, without going through
 * PyFloat_Pack4/Unpack4.
 *
 * This is measured, not assumed, and the measurement is the whole reason the
 * code looks like this. `_pgdriver`'s `struct.unpack_from("!1536f", ...)` is
 * one call into `_struct`'s specialised float handler; a C loop calling
 * PyFloat_Unpack4 per element is *slower* than that, because the per-call
 * format dispatch costs more than the arithmetic. Reading the four bytes into a
 * uint32 and memcpy-ing to a float is what actually beats it.
 *
 * memcpy rather than a union or a cast: it is the only spelling that is not
 * strict-aliasing UB, and every compiler here turns it into a register move.
 * Guarded on __STDC_IEC_559__ so a platform without IEEE floats keeps the
 * portable path rather than silently producing different numbers from the twin. */
#if defined(__STDC_IEC_559__) && !defined(WREATH_PG_NO_FAST_FLOAT4)
#define WREATH_PG_FAST_FLOAT4 1
static double
read_be_float4(const unsigned char *raw)
{
    uint32_t bits = ((uint32_t)raw[0] << 24) | ((uint32_t)raw[1] << 16) |
                    ((uint32_t)raw[2] << 8) | (uint32_t)raw[3];
    float value;
    memcpy(&value, &bits, 4);
    return (double)value;
}

static void
write_be_float4(char *out, double number)
{
    float narrowed = (float)number;
    uint32_t bits;
    memcpy(&bits, &narrowed, 4);
    out[0] = (char)(unsigned char)(bits >> 24);
    out[1] = (char)(unsigned char)(bits >> 16);
    out[2] = (char)(unsigned char)(bits >> 8);
    out[3] = (char)(unsigned char)bits;
}
#endif

/* pgvector's binary `vector`: uint16 dim, uint16 unused, then dim big-endian
   float4s. A 1536-dimension embedding is 6148 bytes; the buffer is walked once
   into an exactly sized allocation.
 *
 * What the ablation says. Both implementations imported into one process and
 * driven through `_devtools/measure.py` -- arms interleaved, an A/A control of
 * the native arm at the far end of each round to measure the floor, medians over
 * 11 rounds, 4 independent runs. The workload is the one this codec exists for:
 * a 50-row x 1536-dimension similarity result, 76,800 floats, not a single
 * value. Python 3.14.2, x86-64 Linux.
 *
 *                        native          pure         delta      A/A floor
 *   decode, 50 x 1536    1.74-1.76 ms    2.12-2.24 ms  +373-491 us   4-13 us
 *   encode, one vector   5.22-5.26 us    30.5-31.1 us  +25.3-25.9 us <0.02 us
 *
 * Both are resolved, by 30x to 100x the measured floor, and neither the sign nor
 * the magnitude moved across the four runs. Decode is ~21-28% faster, encode
 * ~5.8x. That is the whole justification for this file and it is met.
 *
 * Two cautions for whoever re-measures. **The decode number here is the
 * conservative one**: it times `wreath_pg_decode_value`, which takes an already
 * boxed bytes object, because that is the only entry point `_pgdriver` has.
 * The real read path installs `wreath_pg_decode_extension` as a column decoder
 * (see `wreath_pg_select_decoder`) and reads the wire buffer directly, skipping
 * a 6 KB copy per row that this measurement charges to both arms. And **decode
 * is the narrower margin for a reason**: `_pgdriver`'s decoder is a single
 * `struct.unpack_from("!1536f")`, one call into `_struct`'s specialised float
 * handler rather than 1536 interpreter round trips, so it is a much better
 * opponent than the usual per-element Python loop.
 *
 * That is also why the float conversion above is spelled by hand. The first
 * version of this code called PyFloat_Pack4/Unpack4 per element and was *slower
 * than `_pgdriver` at both ends*; it measured as no better than a tie, and it
 * was only after the hand-rolled conversion that this file earned its place.
 * Anyone tempted to simplify it back should re-measure first -- and rebuild
 * before believing the result, because an ablation against a stale `.so` is how
 * that earlier reading was produced. */
static PyObject *
encode_vector(PyObject *value)
{
    PyObject *seq;
    PyObject *result = NULL;
    Py_ssize_t count;
    char *out;

    seq = PySequence_Fast(value, "vector codec requires a list or tuple of floats");
    if (seq == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(seq);
    if (count > 0xFFFF) {
        Py_DECREF(seq);
        PyErr_SetString(PyExc_OverflowError,
                        "a vector may hold at most 65535 dimensions");
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, 4 + count * 4);
    if (result == NULL) {
        Py_DECREF(seq);
        return NULL;
    }
    out = PyBytes_AS_STRING(result);
    out[0] = (char)(unsigned char)(count >> 8);
    out[1] = (char)(unsigned char)count;
    out[2] = 0;
    out[3] = 0;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_Fast_GET_ITEM(seq, index);  /* borrowed */
        double number;
        if (PyFloat_CheckExact(item)) {
            number = PyFloat_AS_DOUBLE(item);
        }
        else {
            if (PyBool_Check(item) || (!PyFloat_Check(item) && !PyLong_Check(item))) {
                Py_DECREF(seq);
                Py_DECREF(result);
                PyErr_SetString(PyExc_TypeError,
                                "vector codec requires int or float elements");
                return NULL;
            }
            number = PyFloat_AsDouble(item);
            if (number == -1.0 && PyErr_Occurred()) {
                Py_DECREF(seq);
                Py_DECREF(result);
                return NULL;
            }
        }
#ifdef WREATH_PG_FAST_FLOAT4
        write_be_float4(out + 4 + index * 4, number);
#else
        if (PyFloat_Pack4(number, out + 4 + index * 4, 0) < 0) {
            Py_DECREF(seq);
            Py_DECREF(result);
            return NULL;
        }
#endif
    }
    Py_DECREF(seq);
    return result;
}

static PyObject *
decode_vector(const unsigned char *raw, Py_ssize_t length)
{
    uint32_t count, unused;
    PyObject *list;

    if (length < 4) {
        PyErr_SetString(PyExc_ValueError, "binary vector header is truncated");
        return NULL;
    }
    count = (uint32_t)((raw[0] << 8) | raw[1]);
    unused = (uint32_t)((raw[2] << 8) | raw[3]);
    if (unused != 0) {
        PyErr_Format(PyExc_ValueError, "unsupported binary vector flags %u", unused);
        return NULL;
    }
    if (length != 4 + (Py_ssize_t)count * 4) {
        PyErr_SetString(PyExc_ValueError,
                        "binary vector length does not match its dimension");
        return NULL;
    }
    list = PyList_New((Py_ssize_t)count);
    if (list == NULL) return NULL;
    for (uint32_t index = 0; index < count; index++) {
        const unsigned char *cell = raw + 4 + index * 4;
        PyObject *item;
#ifdef WREATH_PG_FAST_FLOAT4
        item = PyFloat_FromDouble(read_be_float4(cell));
#else
        double number = PyFloat_Unpack4((const char *)cell, 0);
        if (number == -1.0 && PyErr_Occurred()) {
            Py_DECREF(list);
            return NULL;
        }
        item = PyFloat_FromDouble(number);
#endif
        if (item == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, (Py_ssize_t)index, item);
    }
    return list;
}

/* The text form pgvector prints: "[1,2,3]". Only reachable on a cold statement,
   before the plan is cached and results turn binary, but it must land on the
   same Python value as the binary path or a first call and a second call would
   disagree -- the exact failure `orm/introspection.py` documents for the
   catalog. */
static PyObject *
encode_vector_text(PyObject *value)
{
    PyObject *seq;
    PyObject *pieces;
    PyObject *joined = NULL;
    PyObject *result = NULL;
    PyObject *separator;
    Py_ssize_t count;

    seq = PySequence_Fast(value, "vector codec requires a list or tuple of floats");
    if (seq == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(seq);
    pieces = PyList_New(count);
    if (pieces == NULL) {
        Py_DECREF(seq);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_Fast_GET_ITEM(seq, index);
        double number = PyFloat_AsDouble(item);
        PyObject *boxed;
        PyObject *text;
        if (number == -1.0 && PyErr_Occurred()) goto done;
        boxed = PyFloat_FromDouble(number);
        if (boxed == NULL) goto done;
        text = PyObject_Repr(boxed);
        Py_DECREF(boxed);
        if (text == NULL) goto done;
        PyList_SET_ITEM(pieces, index, text);
    }
    separator = PyUnicode_FromString(",");
    if (separator == NULL) goto done;
    joined = PyUnicode_Join(separator, pieces);
    Py_DECREF(separator);
    if (joined == NULL) goto done;
    {
        PyObject *bracketed = PyUnicode_FromFormat("[%U]", joined);
        if (bracketed == NULL) goto done;
        result = PyUnicode_AsEncodedString(bracketed, "ascii", "strict");
        Py_DECREF(bracketed);
    }
done:
    Py_XDECREF(joined);
    Py_DECREF(pieces);
    Py_DECREF(seq);
    return result;
}

static PyObject *
decode_vector_text(const unsigned char *raw, Py_ssize_t length)
{
    PyObject *text, *stripped, *body, *parts, *list;
    PyObject *open_bracket, *close_bracket;
    int bracketed;

    text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
    if (text == NULL) return NULL;
    stripped = PyObject_CallMethod(text, "strip", NULL);
    Py_DECREF(text);
    if (stripped == NULL) return NULL;
    open_bracket = PyUnicode_FromString("[");
    close_bracket = PyUnicode_FromString("]");
    if (open_bracket == NULL || close_bracket == NULL) {
        Py_XDECREF(open_bracket);
        Py_XDECREF(close_bracket);
        Py_DECREF(stripped);
        return NULL;
    }
    bracketed = PyUnicode_Tailmatch(stripped, open_bracket, 0, PY_SSIZE_T_MAX, -1) == 1 &&
                PyUnicode_Tailmatch(stripped, close_bracket, 0, PY_SSIZE_T_MAX, 1) == 1;
    Py_DECREF(open_bracket);
    Py_DECREF(close_bracket);
    if (!bracketed) {
        Py_DECREF(stripped);
        PyErr_SetString(PyExc_ValueError, "text-format vector is not bracketed");
        return NULL;
    }
    body = PySequence_GetSlice(stripped, 1, PyUnicode_GET_LENGTH(stripped) - 1);
    Py_DECREF(stripped);
    if (body == NULL) return NULL;
    Py_SETREF(body, PyObject_CallMethod(body, "strip", NULL));
    if (body == NULL) return NULL;
    if (PyUnicode_GET_LENGTH(body) == 0) {
        Py_DECREF(body);
        return PyList_New(0);
    }
    parts = PyObject_CallMethod(body, "split", "s", ",");
    Py_DECREF(body);
    if (parts == NULL) return NULL;
    list = PyList_New(PyList_GET_SIZE(parts));
    if (list == NULL) {
        Py_DECREF(parts);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(parts); index++) {
        PyObject *number = PyFloat_FromString(PyList_GET_ITEM(parts, index));
        if (number == NULL) {
            Py_DECREF(parts);
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, index, number);
    }
    Py_DECREF(parts);
    return list;
}

/* pgvector's binary `halfvec`: the same uint16 dim / uint16 unused header as
 * `vector`, then dim big-endian float2. PyFloat_Pack2/Unpack2 rather than a
 * hand-rolled conversion: the float4 path is hand-written because it measured
 * faster than PyFloat_Pack4 (see the comment above encode_vector), and that
 * result does not transfer -- binary16 packing needs subnormal and overflow
 * handling that the CPython helper already has, and halfvec exists to halve
 * *storage*, not to be the hot path. Measure before hand-rolling this one too. */
static PyObject *
encode_halfvec(PyObject *value)
{
    PyObject *seq;
    PyObject *result = NULL;
    Py_ssize_t count;
    char *out;

    seq = PySequence_Fast(value, "halfvec codec requires a list or tuple of floats");
    if (seq == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(seq);
    if (count > 0xFFFF) {
        Py_DECREF(seq);
        PyErr_SetString(PyExc_OverflowError,
                        "a halfvec may hold at most 65535 dimensions");
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, 4 + count * 2);
    if (result == NULL) {
        Py_DECREF(seq);
        return NULL;
    }
    out = PyBytes_AS_STRING(result);
    out[0] = (char)(unsigned char)(count >> 8);
    out[1] = (char)(unsigned char)count;
    out[2] = 0;
    out[3] = 0;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_Fast_GET_ITEM(seq, index);  /* borrowed */
        double number;
        if (PyFloat_CheckExact(item)) {
            number = PyFloat_AS_DOUBLE(item);
        }
        else {
            if (PyBool_Check(item) || (!PyFloat_Check(item) && !PyLong_Check(item))) {
                Py_DECREF(seq);
                Py_DECREF(result);
                PyErr_SetString(PyExc_TypeError,
                                "halfvec codec requires int or float elements");
                return NULL;
            }
            number = PyFloat_AsDouble(item);
            if (number == -1.0 && PyErr_Occurred()) {
                Py_DECREF(seq);
                Py_DECREF(result);
                return NULL;
            }
        }
        if (number != number || number > WREATH_PG_MAX_HALF
            || number < -WREATH_PG_MAX_HALF) {
            Py_DECREF(seq);
            Py_DECREF(result);
            /* PyErr_Format supports no float conversion -- `%.1f` raises
             * SystemError at runtime, which the refusal tests caught. The bound is
             * a compile-time constant anyway, so it is written into the literal. */
            PyErr_Format(PyExc_ValueError,
                         "halfvec element %zd is out of binary16 range (+/-65504) or "
                         "not a number; pgvector stores neither NaN nor infinity",
                         index);
            return NULL;
        }
        if (PyFloat_Pack2(number, out + 4 + index * 2, 0) < 0) {
            Py_DECREF(seq);
            Py_DECREF(result);
            return NULL;
        }
    }
    Py_DECREF(seq);
    return result;
}

static PyObject *
decode_halfvec(const unsigned char *raw, Py_ssize_t length)
{
    uint32_t count, unused;
    PyObject *list;

    if (length < 4) {
        PyErr_SetString(PyExc_ValueError, "binary halfvec header is truncated");
        return NULL;
    }
    count = (uint32_t)((raw[0] << 8) | raw[1]);
    unused = (uint32_t)((raw[2] << 8) | raw[3]);
    if (unused != 0) {
        PyErr_Format(PyExc_ValueError, "unsupported binary halfvec flags %u", unused);
        return NULL;
    }
    if (length != 4 + (Py_ssize_t)count * 2) {
        PyErr_SetString(PyExc_ValueError,
                        "binary halfvec length does not match its dimension");
        return NULL;
    }
    list = PyList_New((Py_ssize_t)count);
    if (list == NULL) return NULL;
    for (uint32_t index = 0; index < count; index++) {
        double number = PyFloat_Unpack2((const char *)(raw + 4 + index * 2), 0);
        PyObject *item;
        if (number == -1.0 && PyErr_Occurred()) {
            Py_DECREF(list);
            return NULL;
        }
        item = PyFloat_FromDouble(number);
        if (item == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, (Py_ssize_t)index, item);
    }
    return list;
}

/* `wreath._sparsevec.SparseVector`, resolved in module init beside uuid.UUID and
 * decimal.Decimal. A `sparsevec` is a dimension plus its non-zero elements, and
 * no builtin says both, so unlike `vector` this codec cannot answer in a list.
 * The validation stays in the Python class rather than being restated here: it
 * is declaration-time work on a cold path, and two copies of a bounds check are
 * two chances to disagree about what pgvector accepts. */
static PyObject *sparsevec_type = NULL;

static int
check_sparsevec(PyObject *value)
{
    int is_sparse;
    if (sparsevec_type == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "the SparseVector type is unavailable");
        return -1;
    }
    is_sparse = PyObject_IsInstance(value, sparsevec_type);
    if (is_sparse < 0) return -1;
    if (!is_sparse) {
        PyErr_SetString(
            PyExc_TypeError,
            "sparsevec codec requires a SparseVector; a dict names no dimension "
            "and a list is the dense type. wreath.postgres.SparseVector(dim, "
            "{index: value}) or SparseVector.from_dense([...])");
        return -1;
    }
    return 0;
}

/* pgvector's binary `sparsevec`: int32 dim, nnz, unused, then nnz int32 indices,
 * then nnz big-endian float4 values. The indices on the wire are **0-based** and
 * `SparseVector`'s are 1-based (pgvector's own text form counts from one); this
 * function and `decode_sparsevec` are the only two places the two meet. */
static PyObject *
encode_sparsevec(PyObject *value)
{
    PyObject *capsule = NULL, *result = NULL;
    WreathSparseVector *data;
    unsigned char *out;

    if (check_sparsevec(value) < 0) return NULL;
    capsule = PyObject_GetAttrString(value, "_data");
    if (capsule == NULL) return NULL;
    data = wreath_sparse_vector_get(capsule);
    if (data == NULL) goto done;
    result = PyBytes_FromStringAndSize(NULL, 12 + data->count * 8);
    if (result == NULL) goto done;
    out = (unsigned char *)PyBytes_AS_STRING(result);
    wreath_store_u32_be(out, (uint32_t)data->dimension);
    wreath_store_u32_be(out + 4, (uint32_t)data->count);
    wreath_store_u32_be(out + 8, 0);
    for (Py_ssize_t index = 0; index < data->count; index++) {
        wreath_store_u32_be(
            out + 12 + index * 4, (uint32_t)(data->indices[index] - 1));
#ifdef WREATH_PG_FAST_FLOAT4
        write_be_float4((char *)out + 12 + data->count * 4 + index * 4,
                        data->values[index]);
#else
        if (PyFloat_Pack4(data->values[index],
                          (char *)out + 12 + data->count * 4 + index * 4, 0) < 0) {
            Py_CLEAR(result);
            goto done;
        }
#endif
    }
done:
    Py_XDECREF(capsule);
    return result;
}

static PyObject *
decode_sparsevec(const unsigned char *raw, Py_ssize_t length)
{
    uint32_t dim, count, unused;
    PyObject *mapping, *dim_object, *result;

    if (length < 12) {
        PyErr_SetString(PyExc_ValueError, "binary sparsevec header is truncated");
        return NULL;
    }
    dim = wreath_load_u32_be(raw);
    count = wreath_load_u32_be(raw + 4);
    unused = wreath_load_u32_be(raw + 8);
    if (unused != 0) {
        PyErr_Format(PyExc_ValueError, "unsupported binary sparsevec flags %u", unused);
        return NULL;
    }
    if ((int32_t)dim < 1) {
        PyErr_Format(PyExc_ValueError, "binary sparsevec dimension %d is not positive",
                     (int)(int32_t)dim);
        return NULL;
    }
    if (count > WREATH_PG_SPARSEVEC_MAX_NNZ) {
        PyErr_Format(PyExc_ValueError,
                     "binary sparsevec element count %u is out of range", count);
        return NULL;
    }
    if (length != 12 + (Py_ssize_t)count * 8) {
        PyErr_SetString(PyExc_ValueError,
                        "binary sparsevec length does not match its element count");
        return NULL;
    }
    if (sparsevec_type == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "the SparseVector type is unavailable");
        return NULL;
    }
    mapping = PyDict_New();
    if (mapping == NULL) return NULL;
    for (uint32_t index = 0; index < count; index++) {
        int32_t position = (int32_t)wreath_load_u32_be(raw + 12 + index * 4);
        const unsigned char *cell = raw + 12 + count * 4 + index * 4;
        PyObject *key, *item;
        int stored;
        if (position < 0 || (uint32_t)position >= dim) {
            Py_DECREF(mapping);
            PyErr_Format(PyExc_ValueError,
                         "binary sparsevec index %d is outside 0..%u",
                         (int)position, dim - 1);
            return NULL;
        }
#ifdef WREATH_PG_FAST_FLOAT4
        item = PyFloat_FromDouble(read_be_float4(cell));
#else
        {
            double number = PyFloat_Unpack4((const char *)cell, 0);
            if (number == -1.0 && PyErr_Occurred()) {
                Py_DECREF(mapping);
                return NULL;
            }
            item = PyFloat_FromDouble(number);
        }
#endif
        key = PyLong_FromLong((long)position + 1);
        if (key == NULL || item == NULL) {
            Py_XDECREF(key);
            Py_XDECREF(item);
            Py_DECREF(mapping);
            return NULL;
        }
        stored = PyDict_SetItem(mapping, key, item);
        Py_DECREF(key);
        Py_DECREF(item);
        if (stored < 0) {
            Py_DECREF(mapping);
            return NULL;
        }
    }
    dim_object = PyLong_FromUnsignedLong(dim);
    if (dim_object == NULL) {
        Py_DECREF(mapping);
        return NULL;
    }
    result = PyObject_CallFunctionObjArgs(sparsevec_type, dim_object, mapping, NULL);
    Py_DECREF(dim_object);
    Py_DECREF(mapping);
    return result;
}

/* pgvector's text `sparsevec`: `{1:1.5,3:3.5}/5`. Each value is rendered by
 * `repr()` rather than by a C format, because `_pgdriver` renders it that way
 * and the two must agree byte for byte -- `%g` and `repr` disagree about how
 * many digits a float4 deserves. */
static PyObject *
encode_sparsevec_text(PyObject *value)
{
    PyObject *dim_object = NULL, *indices = NULL, *values = NULL;
    PyObject *pieces = NULL, *comma = NULL, *body = NULL, *text = NULL;
    PyObject *result = NULL;
    Py_ssize_t count;

    if (check_sparsevec(value) < 0) return NULL;
    dim_object = PyObject_GetAttrString(value, "dim");
    indices = PyObject_GetAttrString(value, "indices");
    values = PyObject_GetAttrString(value, "values");
    if (dim_object == NULL || indices == NULL || values == NULL) goto done;
    count = PySequence_Size(indices);
    if (count < 0) goto done;
    pieces = PyList_New(count);
    if (pieces == NULL) goto done;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *position = PySequence_GetItem(indices, index);
        PyObject *number = PySequence_GetItem(values, index);
        PyObject *piece = NULL;
        if (position != NULL && number != NULL) {
            piece = PyUnicode_FromFormat("%S:%R", position, number);
        }
        Py_XDECREF(position);
        Py_XDECREF(number);
        if (piece == NULL) goto done;
        PyList_SET_ITEM(pieces, index, piece);
    }
    comma = PyUnicode_FromString(",");
    if (comma == NULL) goto done;
    body = PyUnicode_Join(comma, pieces);
    if (body == NULL) goto done;
    text = PyUnicode_FromFormat("{%U}/%S", body, dim_object);
    if (text == NULL) goto done;
    result = PyUnicode_AsEncodedString(text, "ascii", "strict");
done:
    Py_XDECREF(dim_object);
    Py_XDECREF(indices);
    Py_XDECREF(values);
    Py_XDECREF(pieces);
    Py_XDECREF(comma);
    Py_XDECREF(body);
    Py_XDECREF(text);
    return result;
}

static PyObject *
decode_sparsevec_text(const unsigned char *raw, Py_ssize_t length)
{
    PyObject *text = NULL, *split = NULL, *body = NULL, *dim_object = NULL;
    PyObject *inner = NULL, *parts = NULL, *mapping = NULL, *result = NULL;
    Py_ssize_t size;

    text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
    if (text == NULL) return NULL;
    Py_SETREF(text, PyObject_CallMethod(text, "strip", NULL));
    if (text == NULL) return NULL;
    /* rpartition, so a `/` inside the braces could never be mistaken for the
       one that introduces the dimension. */
    split = PyObject_CallMethod(text, "rpartition", "s", "/");
    if (split == NULL || PyTuple_GET_SIZE(split) != 3) goto malformed;
    body = Py_NewRef(PyTuple_GET_ITEM(split, 0));
    dim_object = Py_NewRef(PyTuple_GET_ITEM(split, 2));
    if (PyUnicode_GET_LENGTH(PyTuple_GET_ITEM(split, 1)) == 0) goto malformed;
    size = PyUnicode_GET_LENGTH(body);
    if (size < 2 || PyUnicode_READ_CHAR(body, 0) != '{' ||
        PyUnicode_READ_CHAR(body, size - 1) != '}') {
        goto malformed;
    }
    inner = PySequence_GetSlice(body, 1, size - 1);
    if (inner == NULL) goto done;
    Py_SETREF(inner, PyObject_CallMethod(inner, "strip", NULL));
    if (inner == NULL) goto done;
    Py_SETREF(dim_object, PyNumber_Long(dim_object));
    if (dim_object == NULL) goto done;
    mapping = PyDict_New();
    if (mapping == NULL) goto done;
    if (PyUnicode_GET_LENGTH(inner) != 0) {
        parts = PyObject_CallMethod(inner, "split", "s", ",");
        if (parts == NULL) goto done;
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(parts); index++) {
            PyObject *piece = PyList_GET_ITEM(parts, index);  /* borrowed */
            Py_ssize_t size = PyUnicode_GET_LENGTH(piece);
            /* Split at the colon by index rather than by calling `partition`:
               a method call inside a loop re-resolves the attribute by name
               every iteration (NC005), and a find plus two slices is what the
               method would have done anyway. */
            Py_ssize_t colon = PyUnicode_FindChar(piece, ':', 0, size, 1);
            PyObject *key = NULL, *item = NULL, *number = NULL;
            int stored;
            if (colon == -2) goto done;
            if (colon < 0) goto malformed;
            key = PySequence_GetSlice(piece, 0, colon);
            number = PySequence_GetSlice(piece, colon + 1, size);
            if (key != NULL) Py_SETREF(key, PyNumber_Long(key));
            if (number != NULL) item = PyFloat_FromString(number);
            Py_XDECREF(number);
            if (key == NULL || item == NULL) {
                Py_XDECREF(key);
                Py_XDECREF(item);
                goto done;
            }
            stored = PyDict_SetItem(mapping, key, item);
            Py_DECREF(key);
            Py_DECREF(item);
            if (stored < 0) goto done;
        }
    }
    if (sparsevec_type == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "the SparseVector type is unavailable");
        goto done;
    }
    result = PyObject_CallFunctionObjArgs(sparsevec_type, dim_object, mapping, NULL);
    goto done;
malformed:
    PyErr_SetString(PyExc_ValueError,
                    "text-format sparsevec is not '{index:value,...}/dim'");
done:
    Py_XDECREF(text);
    Py_XDECREF(split);
    Py_XDECREF(body);
    Py_XDECREF(dim_object);
    Py_XDECREF(inner);
    Py_XDECREF(parts);
    Py_XDECREF(mapping);
    return result;
}

/* PostgreSQL's binary `point`: two big-endian float8, x then y -- longitude
 * then latitude, the order PostGIS and GeoJSON use and the opposite of the one
 * people say aloud. The value arriving here is the `(x,y)` text literal that
 * `Point.to_wire` produced, because one `to_wire` has to serve both the text
 * and the binary parameter paths; this parses it rather than the column type
 * carrying two representations. Kept byte-for-byte equal to `_pgdriver`'s
 * `_encode_point` by tests/orm/test_geospatial_codec_parity.py. */
static PyObject *
encode_point(PyObject *value)
{
    const char *text;
    Py_ssize_t length;
    char buffer[64];
    char *comma;
    char *end;
    double x;
    double y;
    PyObject *result;

    if (PyUnicode_Check(value)) {
        text = PyUnicode_AsUTF8AndSize(value, &length);
        if (text == NULL) return NULL;
    } else if (PyBytes_Check(value)) {
        text = PyBytes_AS_STRING(value);
        length = PyBytes_GET_SIZE(value);
    } else {
        PyErr_SetString(PyExc_TypeError, "point codec requires the '(x,y)' literal");
        return NULL;
    }
    if (length < 5 || length >= (Py_ssize_t)sizeof(buffer) || text[0] != '(' ||
        text[length - 1] != ')') {
        PyErr_SetString(PyExc_TypeError, "point codec requires the '(x,y)' literal");
        return NULL;
    }
    memcpy(buffer, text + 1, (size_t)(length - 2));
    buffer[length - 2] = '\0';
    comma = strchr(buffer, ',');
    if (comma == NULL || strchr(comma + 1, ',') != NULL) {
        PyErr_SetString(PyExc_TypeError, "point codec requires the '(x,y)' literal");
        return NULL;
    }
    *comma = '\0';
    errno = 0;
    x = strtod(buffer, &end);
    if (end == buffer || *end != '\0' || errno != 0) {
        PyErr_SetString(PyExc_TypeError, "point codec requires the '(x,y)' literal");
        return NULL;
    }
    errno = 0;
    y = strtod(comma + 1, &end);
    if (end == comma + 1 || *end != '\0' || errno != 0) {
        PyErr_SetString(PyExc_TypeError, "point codec requires the '(x,y)' literal");
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, 16);
    if (result == NULL) return NULL;
    if (PyFloat_Pack8(x, PyBytes_AS_STRING(result), 0) < 0 ||
        PyFloat_Pack8(y, PyBytes_AS_STRING(result) + 8, 0) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    return result;
}

/* PostGIS `geography`: EWKB bytes, arriving here as the hex spelling
 * `Geography.to_wire` produced. PostGIS reads that hex on the text parameter
 * path and the un-hexed bytes on the binary one, so one `to_wire` serves both
 * and this only has to reverse the hex -- deliberately without a second opinion
 * about what the geometry may be, which is the server's to hold. Kept
 * byte-for-byte equal to `_pgdriver`'s `_encode_geography` by
 * tests/orm/test_geospatial_codec_parity.py. */
static int
hex_nibble(unsigned char digit)
{
    if (digit >= '0' && digit <= '9') return digit - '0';
    if (digit >= 'a' && digit <= 'f') return digit - 'a' + 10;
    if (digit >= 'A' && digit <= 'F') return digit - 'A' + 10;
    return -1;
}

static PyObject *
encode_geography(PyObject *value)
{
    const char *text;
    Py_ssize_t length;
    PyObject *result;
    char *out;

    if (PyUnicode_Check(value)) {
        text = PyUnicode_AsUTF8AndSize(value, &length);
        if (text == NULL) return NULL;
    } else if (PyBytes_Check(value)) {
        text = PyBytes_AS_STRING(value);
        length = PyBytes_GET_SIZE(value);
    } else {
        PyErr_SetString(PyExc_TypeError, "geography codec requires EWKB hex");
        return NULL;
    }
    if (length % 2 != 0) {
        PyErr_SetString(PyExc_TypeError, "geography codec requires EWKB hex");
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, length / 2);
    if (result == NULL) return NULL;
    out = PyBytes_AS_STRING(result);
    for (Py_ssize_t index = 0; index < length; index += 2) {
        int high = hex_nibble((unsigned char)text[index]);
        int low = hex_nibble((unsigned char)text[index + 1]);
        if (high < 0 || low < 0) {
            Py_DECREF(result);
            PyErr_SetString(PyExc_TypeError, "geography codec requires EWKB hex");
            return NULL;
        }
        out[index / 2] = (char)((high << 4) | low);
    }
    return result;
}

/* PostgreSQL's binary `bit`: int32 bit count, then the bits MSB-first, the final
 * byte padded on the right with zeros. A 3-bit '101' is one byte 0b10100000, not
 * 0b00000101 -- reversed, it is a value the server accepts and pgvector's hamming
 * distance then reads as a different signature entirely. */
static PyObject *
encode_bit(PyObject *value)
{
    const char *bits;
    Py_ssize_t length, width;
    PyObject *result;
    unsigned char *out;

    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "bit codec requires a str of '0' and '1'");
        return NULL;
    }
    bits = PyUnicode_AsUTF8AndSize(value, &length);
    if (bits == NULL) return NULL;
    width = (length + 7) / 8;
    result = PyBytes_FromStringAndSize(NULL, 4 + width);
    if (result == NULL) return NULL;
    out = (unsigned char *)PyBytes_AS_STRING(result);
    wreath_store_u32_be(out, (uint32_t)length);
    memset(out + 4, 0, (size_t)width);
    for (Py_ssize_t index = 0; index < length; index++) {
        if (bits[index] == '1') {
            out[4 + (index >> 3)] |= (unsigned char)(0x80u >> (index & 7));
        }
        else if (bits[index] != '0') {
            Py_DECREF(result);
            PyErr_SetString(PyExc_ValueError,
                            "a bit string may hold only '0' and '1'");
            return NULL;
        }
    }
    return result;
}

static PyObject *
decode_bit(const unsigned char *raw, Py_ssize_t length)
{
    int32_t bits;
    Py_ssize_t width;
    int padding;
    PyObject *result;
    Py_UCS1 *out;

    if (length < 4) {
        PyErr_SetString(PyExc_ValueError, "binary bit header is truncated");
        return NULL;
    }
    bits = (int32_t)wreath_load_u32_be(raw);
    if (bits < 0) {
        PyErr_Format(PyExc_ValueError, "binary bit length %d is negative", (int)bits);
        return NULL;
    }
    width = ((Py_ssize_t)bits + 7) / 8;
    if (length != 4 + width) {
        PyErr_SetString(PyExc_ValueError,
                        "binary bit length does not match its payload");
        return NULL;
    }
    padding = (int)(width * 8 - bits);
    if (padding != 0 && (raw[4 + width - 1] & ((1u << padding) - 1u)) != 0) {
        PyErr_SetString(PyExc_ValueError, "binary bit has non-zero padding bits");
        return NULL;
    }
    result = PyUnicode_New((Py_ssize_t)bits, 127);
    if (result == NULL) return NULL;
    out = PyUnicode_1BYTE_DATA(result);
    for (Py_ssize_t index = 0; index < bits; index++) {
        out[index] = (raw[4 + (index >> 3)] & (0x80u >> (index & 7))) ? '1' : '0';
    }
    return result;
}

/* The text form of `bit` is the string itself, so both directions only have to
   establish that it is one -- and that it holds nothing but '0' and '1', which
   the binary encoder would otherwise be the first to notice. */
static PyObject *
check_bit_string(PyObject *text)
{
    const char *bits;
    Py_ssize_t length;

    if (!PyUnicode_Check(text)) {
        PyErr_SetString(PyExc_TypeError, "bit codec requires a str of '0' and '1'");
        return NULL;
    }
    bits = PyUnicode_AsUTF8AndSize(text, &length);
    if (bits == NULL) return NULL;
    for (Py_ssize_t index = 0; index < length; index++) {
        if (bits[index] != '0' && bits[index] != '1') {
            PyErr_SetString(PyExc_ValueError,
                            "a bit string may hold only '0' and '1'");
            return NULL;
        }
    }
    return Py_NewRef(text);
}

PyObject *
wreath_pg_decode_extension(
    const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    int kind = wreath_pg_extension_kind(oid);
    if (kind == WREATH_PG_EXT_SPARSEVEC) {
        if (format == 1) return decode_sparsevec(data, length);
        if (format == 0) return decode_sparsevec_text(data, length);
        PyErr_Format(PyExc_ValueError, "invalid field format code %d", format);
        return NULL;
    }
    if (kind == WREATH_PG_EXT_GEOGRAPHY) {
        /* EWKB, arriving as raw bytes in binary and as its hex spelling in
         * text. Handed back unread in both cases, which is what the fallback
         * decoder would have produced -- the arm exists because `decoder_for`
         * routes *every* registered extension OID here, so a kind with no case
         * raises rather than falling through. `Geography.from_wire` is the one
         * place a geography is interpreted, and reading it twice is how the
         * two readings drift apart. */
        if (format == 0 || format == 1) {
            return PyBytes_FromStringAndSize((const char *)data, length);
        }
        PyErr_Format(PyExc_ValueError, "invalid field format code %d", format);
        return NULL;
    }
    if (kind != WREATH_PG_EXT_VECTOR && kind != WREATH_PG_EXT_HALFVEC) {
        PyErr_Format(PyExc_ValueError, "no decoder for extension OID %u", oid);
        return NULL;
    }
    /* The text form is byte-identical for both -- "[1,2,3]" -- so only the
     * binary framing (float4 vs float2) dispatches on the kind. */
    if (format == 1) {
        return kind == WREATH_PG_EXT_HALFVEC
            ? decode_halfvec(data, length)
            : decode_vector(data, length);
    }
    if (format == 0) return decode_vector_text(data, length);
    PyErr_Format(PyExc_ValueError, "invalid field format code %d", format);
    return NULL;
}

static void
write_be32(unsigned char *out, uint32_t value)
{
    out[0] = (unsigned char)(value >> 24);
    out[1] = (unsigned char)(value >> 16);
    out[2] = (unsigned char)(value >> 8);
    out[3] = (unsigned char)value;
}

/* Mutually recursive with the scalar encoder/decoder (an array frames its
   elements through them), so both are forward-declared here. */
static PyObject *encode_binary_array(PyObject *value, uint32_t element_oid);
static PyObject *decode_binary_array(const unsigned char *raw, Py_ssize_t length);

/* PostgreSQL counts date/timestamp binary values from 2000-01-01 and reserves
   the integer extremes for the infinities datetime cannot represent. */
#define PG_EPOCH_UNIX_DAYS 10957
#define MICROS_PER_DAY 86400000000LL
#define PG_TIMESTAMP_INFINITY INT64_MAX
#define PG_TIMESTAMP_NEG_INFINITY INT64_MIN
#define PG_DATE_INFINITY INT32_MAX
#define PG_DATE_NEG_INFINITY INT32_MIN

static PyObject *uuid_type = NULL;
static PyObject *decimal_type = NULL;
static PyObject *str_as_tuple = NULL;
static PyObject *utc_timezone = NULL;
static PyObject *date_fromisoformat = NULL;
static PyObject *datetime_fromisoformat = NULL;
static PyObject *str_isoformat = NULL;
static PyObject *str_utcoffset = NULL;
static PyObject *str_space = NULL;

/* Days between the civil date and 1970-01-01 (Howard Hinnant's algorithm). */
static int64_t
days_from_civil(int64_t year, unsigned month, unsigned day)
{
    int64_t era;
    unsigned year_of_era, day_of_year, day_of_era;
    year -= month <= 2;
    era = (year >= 0 ? year : year - 399) / 400;
    year_of_era = (unsigned)(year - era * 400);
    day_of_year = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    return era * 146097 + (int64_t)day_of_era - 719468;
}

static void
civil_from_days(int64_t days, int *year, unsigned *month, unsigned *day)
{
    int64_t era, year_value;
    unsigned day_of_era, year_of_era, day_of_year, month_position;
    days += 719468;
    era = (days >= 0 ? days : days - 146096) / 146097;
    day_of_era = (unsigned)(days - era * 146097);
    year_of_era = (day_of_era - day_of_era / 1460 + day_of_era / 36524 -
                   day_of_era / 146096) / 365;
    year_value = (int64_t)year_of_era + era * 400;
    day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    month_position = (5 * day_of_year + 2) / 153;
    *day = day_of_year - (153 * month_position + 2) / 5 + 1;
    *month = month_position + (month_position < 10 ? 3 : -9);
    *year = (int)(year_value + (*month <= 2));
}

int
wreath_pg_check_exact_date(PyObject *value)
{
    if (!PyDate_CheckExact(value)) {
        PyErr_SetString(PyExc_TypeError, "date codec requires date");
        return -1;
    }
    return 0;
}

#define check_exact_date wreath_pg_check_exact_date
#define check_timestamp wreath_pg_check_timestamp

int
wreath_pg_check_timestamp(PyObject *value, int aware)
{
    PyObject *tzinfo;
    if (!PyDateTime_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "timestamp codec requires datetime");
        return -1;
    }
    tzinfo = PyDateTime_DATE_GET_TZINFO(value);
    if (aware && tzinfo == Py_None) {
        PyErr_SetString(PyExc_TypeError, "timestamptz codec requires an aware datetime");
        return -1;
    }
    if (!aware && tzinfo != Py_None) {
        PyErr_SetString(PyExc_TypeError, "timestamp codec requires a naive datetime");
        return -1;
    }
    return 0;
}

/* Days since 2000-01-01 for a date, matching the binary date encoding. */
int
wreath_pg_date_days(PyObject *value, int64_t *out)
{
    if (wreath_pg_check_exact_date(value) < 0) return -1;
    *out = days_from_civil(PyDateTime_GET_YEAR(value),
                           (unsigned)PyDateTime_GET_MONTH(value),
                           (unsigned)PyDateTime_GET_DAY(value)) - PG_EPOCH_UNIX_DAYS;
    if (*out <= PG_DATE_NEG_INFINITY || *out >= PG_DATE_INFINITY) {
        PyErr_SetString(PyExc_OverflowError, "date out of range");
        return -1;
    }
    return 0;
}

/* UUID(bytes=...) once per decoded value.
 *
 * The temporary argument containers here were measured, not assumed: replacing
 * them with a cached-kwnames vectorcall changed nothing (903 ns/value either
 * way, 9 trials). Calling a Python class goes through type_call -> tp_new/
 * tp_init regardless of how the arguments arrive, and `uuid.UUID(bytes=...)`
 * costs 692 ns/value on its own -- 77% of the total. The boundary is not the
 * cost here, so this stays in its simpler, obvious form. */
PyObject *
wreath_pg_uuid_from_bytes(const unsigned char *data)
{
    PyObject *uuid_bytes = PyBytes_FromStringAndSize((const char *)data, 16);
    PyObject *empty_args;
    PyObject *keywords;
    PyObject *result;
    if (uuid_bytes == NULL) return NULL;
    empty_args = PyTuple_New(0);
    keywords = Py_BuildValue("{s:O}", "bytes", uuid_bytes);
    if (empty_args == NULL || keywords == NULL) {
        Py_DECREF(uuid_bytes);
        Py_XDECREF(empty_args);
        Py_XDECREF(keywords);
        return NULL;
    }
    /* native-boundary-lint: allow NB002 -- measured: replacing these containers
       with a cached-kwnames vectorcall changed nothing; uuid.UUID.__init__ is
       77% of this call's cost. See the note above the function. */
    result = PyObject_Call(uuid_type, empty_args, keywords);
    Py_DECREF(uuid_bytes);
    Py_DECREF(empty_args);
    Py_DECREF(keywords);
    return result;
}

int
wreath_pg_uuid_bytes(PyObject *value, unsigned char *out)
{
    PyObject *attr;
    if (uuid_type == NULL || !PyObject_IsInstance(value, uuid_type)) {
        PyErr_SetString(PyExc_TypeError, "uuid codec requires UUID");
        return -1;
    }
    attr = PyObject_GetAttrString(value, "bytes");
    if (attr == NULL) return -1;
    if (!PyBytes_Check(attr) || PyBytes_GET_SIZE(attr) != 16) {
        Py_DECREF(attr);
        PyErr_SetString(PyExc_TypeError, "invalid UUID bytes");
        return -1;
    }
    memcpy(out, PyBytes_AS_STRING(attr), 16);
    Py_DECREF(attr);
    return 0;
}

static PyObject *
json_utf8(PyObject *value)
{
    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "json codec requires str");
        return NULL;
    }
    return PyUnicode_AsEncodedString(value, "utf-8", "strict");
}

/* Microseconds since 2000-01-01, normalized to UTC for aware values. */
#define timestamp_micros wreath_pg_timestamp_micros
#define date_from_days wreath_pg_date_from_days
#define timestamp_from_micros wreath_pg_timestamp_from_micros

int
wreath_pg_timestamp_micros(PyObject *value, int aware, int64_t *out)
{
    int64_t micros = (days_from_civil(PyDateTime_GET_YEAR(value),
                                      (unsigned)PyDateTime_GET_MONTH(value),
                                      (unsigned)PyDateTime_GET_DAY(value)) -
                      PG_EPOCH_UNIX_DAYS) * MICROS_PER_DAY;
    micros += (int64_t)PyDateTime_DATE_GET_HOUR(value) * 3600000000LL;
    micros += (int64_t)PyDateTime_DATE_GET_MINUTE(value) * 60000000LL;
    micros += (int64_t)PyDateTime_DATE_GET_SECOND(value) * 1000000LL;
    micros += PyDateTime_DATE_GET_MICROSECOND(value);
    if (aware) {
        PyObject *offset = PyObject_CallMethodNoArgs(value, str_utcoffset);
        if (offset == NULL) return -1;
        if (offset != Py_None) {
            if (!PyDelta_Check(offset)) {
                Py_DECREF(offset);
                PyErr_SetString(PyExc_TypeError, "utcoffset() must return a timedelta");
                return -1;
            }
            micros -= (int64_t)PyDateTime_DELTA_GET_DAYS(offset) * MICROS_PER_DAY +
                      (int64_t)PyDateTime_DELTA_GET_SECONDS(offset) * 1000000LL +
                      PyDateTime_DELTA_GET_MICROSECONDS(offset);
        }
        Py_DECREF(offset);
    }
    *out = micros;
    return 0;
}

PyObject *
wreath_pg_date_from_days(int64_t days)
{
    int year;
    unsigned month, day;
    if (days == PG_DATE_INFINITY || days == PG_DATE_NEG_INFINITY) {
        PyErr_SetString(PyExc_ValueError, "date infinity is not representable");
        return NULL;
    }
    civil_from_days(days + PG_EPOCH_UNIX_DAYS, &year, &month, &day);
    if (year < 1 || year > 9999) {
        PyErr_SetString(PyExc_ValueError, "date is outside the supported range");
        return NULL;
    }
    return PyDate_FromDate(year, (int)month, (int)day);
}

PyObject *
wreath_pg_timestamp_from_micros(int64_t micros, int aware)
{
    int64_t days, remainder;
    int year;
    unsigned month, day;
    if (micros == PG_TIMESTAMP_INFINITY || micros == PG_TIMESTAMP_NEG_INFINITY) {
        PyErr_SetString(PyExc_ValueError, "timestamp infinity is not representable");
        return NULL;
    }
    days = micros / MICROS_PER_DAY;
    remainder = micros % MICROS_PER_DAY;
    if (remainder < 0) {
        remainder += MICROS_PER_DAY;
        days -= 1;
    }
    civil_from_days(days + PG_EPOCH_UNIX_DAYS, &year, &month, &day);
    if (year < 1 || year > 9999) {
        PyErr_SetString(PyExc_ValueError, "timestamp is outside the supported range");
        return NULL;
    }
    return PyDateTimeAPI->DateTime_FromDateAndTime(
        year, (int)month, (int)day,
        (int)(remainder / 3600000000LL),
        (int)(remainder / 60000000LL % 60),
        (int)(remainder / 1000000LL % 60),
        (int)(remainder % 1000000LL),
        aware ? utc_timezone : Py_None,
        PyDateTimeAPI->DateTimeType
    );
}

static PyObject *
utf8_from_object(PyObject *value)
{
    if (!PyUnicode_Check(value)) {
        PyErr_Format(PyExc_TypeError, "text codec requires str, not %.200s",
                     Py_TYPE(value)->tp_name);
        return NULL;
    }
    return PyUnicode_AsEncodedString(value, "utf-8", "strict");
}

static PyObject *
ascii_string(PyObject *value)
{
    PyObject *text = PyObject_Str(value);
    PyObject *result;
    if (text == NULL) {
        return NULL;
    }
    result = PyUnicode_AsEncodedString(text, "ascii", "strict");
    Py_DECREF(text);
    return result;
}

/* Text-format date/time output goes through isoformat() to preserve PostgreSQL's
   accepted wire spelling for every supported temporal value. */
static PyObject *
isoformat_ascii(PyObject *value, int with_separator)
{
    PyObject *text = with_separator
        ? PyObject_CallMethodOneArg(value, str_isoformat, str_space)
        : PyObject_CallMethodNoArgs(value, str_isoformat);
    PyObject *result;
    if (text == NULL) return NULL;
    result = PyUnicode_AsEncodedString(text, "ascii", "strict");
    Py_DECREF(text);
    return result;
}

/* Coerce to `Decimal`, refusing `float`. A column is declared `numeric` exactly
   because binary floating point cannot hold its values, so accepting a float
   would reintroduce the collapse the type exists to prevent. Returns a new
   reference, or NULL with an exception set. */
static PyObject *
as_decimal(PyObject *value)
{
    if (decimal_type != NULL && PyObject_TypeCheck(value, (PyTypeObject *)decimal_type)) {
        return Py_NewRef(value);
    }
    if (PyLong_CheckExact(value)) {
        return PyObject_CallOneArg(decimal_type, value);
    }
    PyErr_Format(PyExc_TypeError,
                 "numeric codec requires Decimal or int, not %s; "
                 "a float cannot hold a numeric exactly",
                 Py_TYPE(value)->tp_name);
    return NULL;
}

/* Pack a Decimal into PostgreSQL's base-10000 numeric wire form:
   ndigits, weight, sign, dscale, then ndigits groups most significant first.
   Decimal.as_tuple() is the input boundary and the result bytes are the output
   boundary. Between them the coefficient stays in an unboxed C digit buffer:
   rebuilding it as a Python bigint made both the per-digit multiply and the
   repeated base-10000 division quadratic in precision. */
static PyObject *
encode_numeric(PyObject *value)
{
    PyObject *number = NULL, *parts = NULL, *digits = NULL, *exponent = NULL;
    PyObject *result = NULL;
    unsigned char *coefficient = NULL;
    unsigned short *groups = NULL;
    long sign_flag = 0, dscale = 0, exp_value = 0;
    Py_ssize_t count, appended, padding, total_digits, group_count;
    Py_ssize_t fraction_groups, lead, tail, ndigits, weight, i;
    unsigned char header[8];
    char *out;

    number = as_decimal(value);
    if (number == NULL) return NULL;
    parts = PyObject_CallMethodNoArgs(number, str_as_tuple);
    if (parts == NULL || !PyTuple_Check(parts) || PyTuple_GET_SIZE(parts) != 3) {
        if (parts != NULL) PyErr_SetString(PyExc_TypeError, "invalid Decimal tuple");
        goto done;
    }
    sign_flag = PyLong_AsLong(PyTuple_GET_ITEM(parts, 0));
    if (sign_flag == -1 && PyErr_Occurred()) goto done;
    digits = Py_NewRef(PyTuple_GET_ITEM(parts, 1));
    exponent = Py_NewRef(PyTuple_GET_ITEM(parts, 2));

    /* A non-integer exponent is Decimal's marker for NaN ('n'/'N') or
       infinity ('F'); PostgreSQL 14+ carries both as reserved sign words. */
    if (!PyLong_Check(exponent)) {
        unsigned int marker = PG_NUMERIC_NAN;
        if (PyUnicode_Check(exponent) && PyUnicode_CompareWithASCIIString(exponent, "F") == 0) {
            marker = sign_flag ? PG_NUMERIC_NINF : PG_NUMERIC_PINF;
        }
        memset(header, 0, sizeof header);
        header[4] = (unsigned char)(marker >> 8);
        header[5] = (unsigned char)marker;
        result = PyBytes_FromStringAndSize((const char *)header, 8);
        goto done;
    }
    exp_value = PyLong_AsLong(exponent);
    if (exp_value == -1 && PyErr_Occurred()) goto done;
    if (!PyTuple_Check(digits)) {
        PyErr_SetString(PyExc_TypeError, "invalid Decimal digits");
        goto done;
    }
    count = PyTuple_GET_SIZE(digits);
    coefficient = PyMem_Malloc((size_t)(count > 0 ? count : 1));
    if (coefficient == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    for (i = 0; i < count; i++) {
        long digit = PyLong_AsLong(PyTuple_GET_ITEM(digits, i));
        if ((digit == -1 && PyErr_Occurred()) || digit < 0 || digit > 9) {
            if (!PyErr_Occurred())
                PyErr_Format(PyExc_ValueError,
                             "invalid Decimal digit %ld at position %zd", digit, i);
            goto done;
        }
        coefficient[i] = (unsigned char)digit;
    }

    appended = 0;
    if (exp_value > 0) {
        if ((unsigned long)exp_value > (unsigned long)PY_SSIZE_T_MAX) {
            PyErr_SetString(PyExc_ValueError,
                            "numeric exponent exceeds the PostgreSQL wire format");
            goto done;
        }
        appended = (Py_ssize_t)exp_value;
        exp_value = 0;
    }
    else if (exp_value == LONG_MIN) {
        PyErr_SetString(PyExc_ValueError,
                        "numeric exponent exceeds the PostgreSQL wire format");
        goto done;
    }
    dscale = -exp_value;
    if (dscale > 0xFFFF) {
        PyErr_Format(PyExc_ValueError,
                     "numeric display scale %ld exceeds PostgreSQL's 65535 limit",
                     dscale);
        goto done;
    }
    padding = (Py_ssize_t)((4 - (dscale & 3)) & 3);
    if (count > PY_SSIZE_T_MAX - appended - padding) {
        PyErr_NoMemory();
        goto done;
    }
    total_digits = count + appended + padding;
    group_count = total_digits / 4 + (total_digits % 4 != 0);
    if (group_count > 32767) {
        PyErr_Format(PyExc_ValueError,
                     "numeric needs %zd base-10000 groups; PostgreSQL permits 32767",
                     group_count);
        goto done;
    }
    groups = PyMem_Calloc((size_t)(group_count > 0 ? group_count : 1),
                          sizeof(*groups));
    if (groups == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    {
        static const unsigned short places[] = {1000, 100, 10, 1};
        Py_ssize_t prefix = group_count * 4 - total_digits;
        for (i = 0; i < count; i++) {
            Py_ssize_t position = prefix + i;
            groups[position / 4] = (unsigned short)(groups[position / 4] +
                coefficient[i] * places[position & 3]);
        }
    }
    fraction_groups = (Py_ssize_t)(dscale + padding) / 4;
    lead = 0;
    while (lead < group_count && groups[lead] == 0) lead++;
    tail = group_count;
    while (tail > lead && groups[tail - 1] == 0) tail--;
    ndigits = tail - lead;
    weight = ndigits == 0 ? 0 : group_count - 1 - fraction_groups - lead;
    if (weight < -32768 || weight > 32767) {
        PyErr_Format(PyExc_ValueError,
                     "numeric weight %zd exceeds PostgreSQL's signed 16-bit limit",
                     weight);
        goto done;
    }
    if (ndigits == 0) sign_flag = 0;

    result = PyBytes_FromStringAndSize(NULL, 8 + ndigits * 2);
    if (result == NULL) goto done;
    out = PyBytes_AS_STRING(result);
    out[0] = (char)((ndigits >> 8) & 0xFF); out[1] = (char)(ndigits & 0xFF);
    out[2] = (char)((weight >> 8) & 0xFF); out[3] = (char)(weight & 0xFF);
    {
        unsigned int sign_word = sign_flag ? PG_NUMERIC_NEG : PG_NUMERIC_POS;
        out[4] = (char)(sign_word >> 8); out[5] = (char)sign_word;
    }
    out[6] = (char)((dscale >> 8) & 0xFF); out[7] = (char)(dscale & 0xFF);
    for (i = 0; i < ndigits; i++) {
        unsigned short group = groups[lead + i];
        out[8 + i * 2] = (char)(group >> 8);
        out[9 + i * 2] = (char)(group & 0xFF);
    }

done:
    Py_XDECREF(number); Py_XDECREF(parts); Py_XDECREF(digits); Py_XDECREF(exponent);
    PyMem_Free(coefficient); PyMem_Free(groups);
    return result;
}

/* Unpack binary numeric into an exact Decimal, built from a digit string so the
   decimal context can never round it. */
static PyObject *
decode_numeric(const unsigned char *raw, Py_ssize_t length)
{
    int ndigits, weight;
    unsigned int sign, dscale;
    PyObject *unscaled = NULL, *base = NULL, *text = NULL, *result = NULL;
    long exponent;
    int i;

    if (length < 8) {
        PyErr_SetString(PyExc_ValueError, "invalid binary numeric header");
        return NULL;
    }
    ndigits = (int)(int16_t)((raw[0] << 8) | raw[1]);
    weight = (int)(int16_t)((raw[2] << 8) | raw[3]);
    sign = (unsigned int)((raw[4] << 8) | raw[5]);
    dscale = (unsigned int)((raw[6] << 8) | raw[7]);

    if (sign == PG_NUMERIC_NAN) return PyObject_CallFunction(decimal_type, "s", "NaN");
    if (sign == PG_NUMERIC_PINF) return PyObject_CallFunction(decimal_type, "s", "Infinity");
    if (sign == PG_NUMERIC_NINF) return PyObject_CallFunction(decimal_type, "s", "-Infinity");
    if (sign != PG_NUMERIC_POS && sign != PG_NUMERIC_NEG) {
        PyErr_Format(PyExc_ValueError, "invalid numeric sign 0x%04X", sign);
        return NULL;
    }
    if (ndigits < 0 || length != 8 + (Py_ssize_t)ndigits * 2) {
        PyErr_SetString(PyExc_ValueError, "invalid binary numeric length");
        return NULL;
    }

    unscaled = PyLong_FromLong(0);
    base = PyLong_FromLong(10000);
    if (unscaled == NULL || base == NULL) goto done;
    for (i = 0; i < ndigits; i++) {
        int group = (int)(int16_t)((raw[8 + i * 2] << 8) | raw[9 + i * 2]);
        PyObject *scaled, *addend;
        if (group < 0 || group >= 10000) {
            PyErr_SetString(PyExc_ValueError, "invalid numeric digit group");
            goto done;
        }
        scaled = PyNumber_Multiply(unscaled, base);
        if (scaled == NULL) goto done;
        addend = PyLong_FromLong(group);
        if (addend == NULL) { Py_DECREF(scaled); goto done; }
        Py_SETREF(unscaled, PyNumber_Add(scaled, addend));
        Py_DECREF(scaled);
        Py_DECREF(addend);
        if (unscaled == NULL) goto done;
    }
    exponent = 4L * (weight - ndigits + 1);

    /* Move onto the advertised display scale. Growing is always safe; shrinking
       only ever drops PostgreSQL's zero group padding, and the remainder guard
       means a significant digit is never discarded. */
    {
        PyObject *ten = PyLong_FromLong(10);
        if (ten == NULL) goto done;
        while (exponent > -(long)dscale) {
            Py_SETREF(unscaled, PyNumber_Multiply(unscaled, ten));
            if (unscaled == NULL) { Py_DECREF(ten); goto done; }
            exponent--;
        }
        while (exponent < -(long)dscale) {
            PyObject *pair = PyNumber_Divmod(unscaled, ten);
            int divisible;
            if (pair == NULL) { Py_DECREF(ten); goto done; }
            divisible = !PyObject_IsTrue(PyTuple_GET_ITEM(pair, 1));
            if (!divisible) { Py_DECREF(pair); break; }
            Py_SETREF(unscaled, Py_NewRef(PyTuple_GET_ITEM(pair, 0)));
            Py_DECREF(pair);
            exponent++;
        }
        Py_DECREF(ten);
    }

    text = PyUnicode_FromFormat(
        "%s%SE%ld", sign == PG_NUMERIC_NEG ? "-" : "", unscaled, exponent
    );
    if (text == NULL) goto done;
    result = PyObject_CallOneArg(decimal_type, text);

done:
    Py_XDECREF(unscaled); Py_XDECREF(base); Py_XDECREF(text);
    return result;
}

PyObject *
wreath_pg_encode_text_value(PyObject *value, uint32_t oid)
{
    PyObject *result;
    char *output;
    const unsigned char *input;
    Py_ssize_t length;
    static const char hex[] = "0123456789abcdef";

    if (value == Py_None) {
        return Py_NewRef(Py_None);
    }
    switch (oid) {
    case PG_BOOL:
        if (!PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bool codec requires bool");
            return NULL;
        }
        return PyBytes_FromStringAndSize(value == Py_True ? "t" : "f", 1);
    case PG_INT2:
    case PG_INT4:
    case PG_INT8:
        if (!PyLong_CheckExact(value)) {
            PyErr_SetString(PyExc_TypeError, "integer codec requires int");
            return NULL;
        }
        return ascii_string(value);
    case PG_FLOAT4:
    case PG_FLOAT8:
        if ((!PyFloat_Check(value) && !PyLong_CheckExact(value)) || PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "float codec requires int or float");
            return NULL;
        }
        return ascii_string(value);
    case PG_TEXT:
    case PG_VARCHAR:
        return utf8_from_object(value);
    case PG_BYTEA:
        if (!PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bytea codec requires bytes");
            return NULL;
        }
        length = PyBytes_GET_SIZE(value);
        if (length > (PY_SSIZE_T_MAX - 2) / 2) {
            return PyErr_NoMemory();
        }
        result = PyBytes_FromStringAndSize(NULL, 2 + length * 2);
        if (result == NULL) {
            return NULL;
        }
        output = PyBytes_AS_STRING(result);
        output[0] = '\\';
        output[1] = 'x';
        input = (const unsigned char *)PyBytes_AS_STRING(value);
        for (Py_ssize_t i = 0; i < length; i++) {
            output[2 + i * 2] = hex[input[i] >> 4];
            output[3 + i * 2] = hex[input[i] & 15];
        }
        return result;
    case PG_UUID:
        if (uuid_type == NULL || !PyObject_IsInstance(value, uuid_type)) {
            PyErr_SetString(PyExc_TypeError, "uuid codec requires UUID");
            return NULL;
        }
        return ascii_string(value);
    case PG_DATE:
        if (check_exact_date(value) < 0) return NULL;
        return isoformat_ascii(value, 0);
    case PG_TIMESTAMP:
    case PG_TIMESTAMPTZ:
        if (check_timestamp(value, oid == PG_TIMESTAMPTZ) < 0) return NULL;
        return isoformat_ascii(value, 1);
    case PG_NUMERIC: {
        PyObject *number = as_decimal(value);
        PyObject *encoded;
        if (number == NULL) return NULL;
        encoded = ascii_string(number);
        Py_DECREF(number);
        return encoded;
    }
    case PG_JSON:
    case PG_JSONB:
        return json_utf8(value);
    case PG_BIT: {
        PyObject *checked = check_bit_string(value);
        PyObject *encoded;
        if (checked == NULL) return NULL;
        encoded = PyUnicode_AsEncodedString(checked, "ascii", "strict");
        Py_DECREF(checked);
        return encoded;
    }
    default:
        {
            /* `vector` and `halfvec` both print "[1,2,3]", so one text encoder
             * serves them; `sparsevec` prints "{1:1.5}/5" and needs its own. */
            int kind = wreath_pg_extension_kind(oid);
            if (kind == WREATH_PG_EXT_VECTOR || kind == WREATH_PG_EXT_HALFVEC) {
                return encode_vector_text(value);
            }
            if (kind == WREATH_PG_EXT_SPARSEVEC) {
                return encode_sparsevec_text(value);
            }
        }
        return ascii_string(value);
    }
}

static PyObject *
pack_integer(PyObject *value, int bytes)
{
    long long number;
    unsigned long long encoded;
    PyObject *result;
    unsigned char *out;

    if (!PyLong_CheckExact(value)) {
        PyErr_SetString(PyExc_TypeError, "integer codec requires int");
        return NULL;
    }
    number = PyLong_AsLongLong(value);
    if (number == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if ((bytes == 2 && (number < INT16_MIN || number > INT16_MAX)) ||
        (bytes == 4 && (number < INT32_MIN || number > INT32_MAX))) {
        PyErr_Format(PyExc_OverflowError, "int%d out of range", bytes * 8);
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, bytes);
    if (result == NULL) {
        return NULL;
    }
    encoded = (unsigned long long)number;
    out = (unsigned char *)PyBytes_AS_STRING(result);
    for (int i = bytes - 1; i >= 0; i--) {
        out[i] = (unsigned char)encoded;
        encoded >>= 8;
    }
    return result;
}

PyObject *
wreath_pg_encode_binary_value(PyObject *value, uint32_t oid)
{
    PyObject *result;
    PyObject *bytes_attr;
    double number;

    if (value == Py_None) {
        return Py_NewRef(Py_None);
    }
    switch (oid) {
    case PG_BOOL:
        if (!PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bool codec requires bool");
            return NULL;
        }
        return PyBytes_FromStringAndSize(value == Py_True ? "\1" : "\0", 1);
    case PG_INT2:
        return pack_integer(value, 2);
    case PG_INT4:
        return pack_integer(value, 4);
    case PG_INT8:
        return pack_integer(value, 8);
    case PG_FLOAT4:
    case PG_FLOAT8:
        if ((!PyFloat_Check(value) && !PyLong_CheckExact(value)) || PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "float codec requires int or float");
            return NULL;
        }
        number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) {
            return NULL;
        }
        result = PyBytes_FromStringAndSize(NULL, oid == PG_FLOAT4 ? 4 : 8);
        if (result == NULL) {
            return NULL;
        }
        if ((oid == PG_FLOAT4 ?
             PyFloat_Pack4(number, PyBytes_AS_STRING(result), 0) :
             PyFloat_Pack8(number, PyBytes_AS_STRING(result), 0)) < 0) {
            Py_DECREF(result);
            return NULL;
        }
        return result;
    case PG_TEXT:
    case PG_VARCHAR:
        return utf8_from_object(value);
    case PG_BYTEA:
        if (!PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bytea codec requires bytes");
            return NULL;
        }
        return Py_NewRef(value);
    case PG_UUID:
        if (uuid_type == NULL || !PyObject_IsInstance(value, uuid_type)) {
            PyErr_SetString(PyExc_TypeError, "uuid codec requires UUID");
            return NULL;
        }
        bytes_attr = PyObject_GetAttrString(value, "bytes");
        if (bytes_attr == NULL || !PyBytes_Check(bytes_attr)) {
            Py_XDECREF(bytes_attr);
            PyErr_SetString(PyExc_TypeError, "invalid UUID bytes");
            return NULL;
        }
        return bytes_attr;
    case PG_DATE: {
        int64_t days;
        unsigned char out[4];
        uint32_t encoded;
        if (check_exact_date(value) < 0) return NULL;
        days = days_from_civil(PyDateTime_GET_YEAR(value),
                               (unsigned)PyDateTime_GET_MONTH(value),
                               (unsigned)PyDateTime_GET_DAY(value)) - PG_EPOCH_UNIX_DAYS;
        if (days <= PG_DATE_NEG_INFINITY || days >= PG_DATE_INFINITY) {
            PyErr_SetString(PyExc_OverflowError, "date out of range");
            return NULL;
        }
        encoded = (uint32_t)(int32_t)days;
        for (int i = 3; i >= 0; i--) {
            out[i] = (unsigned char)encoded;
            encoded >>= 8;
        }
        return PyBytes_FromStringAndSize((const char *)out, 4);
    }
    case PG_TIMESTAMP:
    case PG_TIMESTAMPTZ: {
        int64_t micros;
        unsigned char out[8];
        uint64_t encoded;
        if (check_timestamp(value, oid == PG_TIMESTAMPTZ) < 0) return NULL;
        if (timestamp_micros(value, oid == PG_TIMESTAMPTZ, &micros) < 0) return NULL;
        encoded = (uint64_t)micros;
        for (int i = 7; i >= 0; i--) {
            out[i] = (unsigned char)encoded;
            encoded >>= 8;
        }
        return PyBytes_FromStringAndSize((const char *)out, 8);
    }
    case PG_NUMERIC:
        return encode_numeric(value);
    case PG_JSON:
        return json_utf8(value);
    case PG_JSONB: {
        PyObject *payload = json_utf8(value);
        PyObject *versioned;
        if (payload == NULL) return NULL;
        versioned = PyBytes_FromStringAndSize(NULL, 1 + PyBytes_GET_SIZE(payload));
        if (versioned == NULL) {
            Py_DECREF(payload);
            return NULL;
        }
        PyBytes_AS_STRING(versioned)[0] = 1;
        memcpy(PyBytes_AS_STRING(versioned) + 1, PyBytes_AS_STRING(payload),
               (size_t)PyBytes_GET_SIZE(payload));
        Py_DECREF(payload);
        return versioned;
    }
    case PG_BIT:
        return encode_bit(value);
    case PG_POINT:
        return encode_point(value);
    default: {
        uint32_t element_oid = array_element_oid(oid);
        if (element_oid != 0) {
            return encode_binary_array(value, element_oid);
        }
        {
            int kind = wreath_pg_extension_kind(oid);
            if (kind == WREATH_PG_EXT_VECTOR) return encode_vector(value);
            if (kind == WREATH_PG_EXT_HALFVEC) return encode_halfvec(value);
            if (kind == WREATH_PG_EXT_SPARSEVEC) return encode_sparsevec(value);
            if (kind == WREATH_PG_EXT_GEOGRAPHY) return encode_geography(value);
        }
        PyErr_Format(PyExc_TypeError, "no binary encoder for PostgreSQL OID %u", oid);
        return NULL;
    }
    }
}

/* Encode a Python list/tuple as one binary PostgreSQL array. Elements are the
   element type's own wire values (Array.to_wire has already mapped element
   to_wire over the sequence), so each frames through the scalar encoder. */
static PyObject *
encode_binary_array(PyObject *value, uint32_t element_oid)
{
    PyObject *seq;
    PyObject *result = NULL;
    PyObject **payloads = NULL;
    Py_ssize_t count = 0;
    Py_ssize_t total;
    int has_null = 0;
    unsigned char *out;
    Py_ssize_t offset;

    seq = PySequence_Fast(value, "array codec requires a list or tuple");
    if (seq == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(seq);
    if (count > 0) {
        payloads = PyMem_Calloc((size_t)count, sizeof(PyObject *));
        if (payloads == NULL) {
            Py_DECREF(seq);
            return PyErr_NoMemory();
        }
    }

    /* header: ndim(4) + has_null(4) + element_oid(4); one dimension block(8)
       when non-empty; then per element len(4) + payload. */
    total = 12 + (count > 0 ? 8 : 0);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_Fast_GET_ITEM(seq, index);  /* borrowed */
        PyObject *encoded;
        if (item == Py_None) {
            has_null = 1;
            payloads[index] = Py_NewRef(Py_None);
            total += 4;
            continue;
        }
        encoded = wreath_pg_encode_binary_value(item, element_oid);
        if (encoded == NULL) goto cleanup;
        if (encoded == Py_None) {
            has_null = 1;
            payloads[index] = encoded;
            total += 4;
            continue;
        }
        if (!PyBytes_Check(encoded)) {
            Py_DECREF(encoded);
            PyErr_SetString(PyExc_TypeError, "array element encoder did not return bytes");
            goto cleanup;
        }
        if (PyBytes_GET_SIZE(encoded) > INT32_MAX ||
            total > PY_SSIZE_T_MAX - 4 - PyBytes_GET_SIZE(encoded)) {
            Py_DECREF(encoded);
            PyErr_SetString(PyExc_OverflowError, "array parameter too large");
            goto cleanup;
        }
        payloads[index] = encoded;
        total += 4 + PyBytes_GET_SIZE(encoded);
    }

    result = PyBytes_FromStringAndSize(NULL, total);
    if (result == NULL) goto cleanup;
    out = (unsigned char *)PyBytes_AS_STRING(result);
    write_be32(out, count > 0 ? 1u : 0u);       /* ndim */
    write_be32(out + 4, has_null ? 1u : 0u);    /* has-null flags */
    write_be32(out + 8, element_oid);           /* element oid */
    offset = 12;
    if (count > 0) {
        write_be32(out + offset, (uint32_t)count);  /* dimension length */
        write_be32(out + offset + 4, 1u);           /* lower bound */
        offset += 8;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *payload = payloads[index];
        Py_ssize_t size;
        if (payload == Py_None) {
            write_be32(out + offset, (uint32_t)(int32_t)-1);
            offset += 4;
            continue;
        }
        size = PyBytes_GET_SIZE(payload);
        write_be32(out + offset, (uint32_t)size);
        memcpy(out + offset + 4, PyBytes_AS_STRING(payload), (size_t)size);
        offset += 4 + size;
    }

cleanup:
    if (payloads != NULL) {
        for (Py_ssize_t index = 0; index < count; index++) Py_XDECREF(payloads[index]);
        PyMem_Free(payloads);
    }
    Py_DECREF(seq);
    return result;
}

/* Append one Bind parameter (length prefix plus binary payload) directly to
   the outgoing buffer, avoiding an intermediate bytes object for the common
   scalar types. Falls back to the boxed encoder for everything else. */
int
wreath_pg_encode_binary_into(WreathPgBuffer *output, PyObject *value, uint32_t oid)
{
    if (value == Py_None) return wreath_pg_buffer_i32(output, -1);
    switch (oid) {
    case PG_BOOL:
        if (!PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bool codec requires bool");
            return -1;
        }
        return wreath_pg_buffer_u32(output, 1) < 0 ||
               wreath_pg_buffer_append(output, value == Py_True ? "\1" : "\0", 1) < 0
               ? -1 : 0;
    case PG_INT2:
    case PG_INT4:
    case PG_INT8: {
        int bytes = oid == PG_INT2 ? 2 : oid == PG_INT4 ? 4 : 8;
        long long number;
        unsigned long long encoded;
        unsigned char out[8];
        if (!PyLong_CheckExact(value)) {
            PyErr_SetString(PyExc_TypeError, "integer codec requires int");
            return -1;
        }
        number = PyLong_AsLongLong(value);
        if (number == -1 && PyErr_Occurred()) return -1;
        if ((bytes == 2 && (number < INT16_MIN || number > INT16_MAX)) ||
            (bytes == 4 && (number < INT32_MIN || number > INT32_MAX))) {
            PyErr_Format(PyExc_OverflowError, "int%d out of range", bytes * 8);
            return -1;
        }
        encoded = (unsigned long long)number;
        for (int i = bytes - 1; i >= 0; i--) {
            out[i] = (unsigned char)encoded;
            encoded >>= 8;
        }
        return wreath_pg_buffer_u32(output, (uint32_t)bytes) < 0 ||
               wreath_pg_buffer_append(output, out, bytes) < 0 ? -1 : 0;
    }
    case PG_FLOAT4:
    case PG_FLOAT8: {
        double number;
        char out[8];
        int bytes = oid == PG_FLOAT4 ? 4 : 8;
        if ((!PyFloat_Check(value) && !PyLong_CheckExact(value)) || PyBool_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "float codec requires int or float");
            return -1;
        }
        number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) return -1;
        if ((oid == PG_FLOAT4 ? PyFloat_Pack4(number, out, 0)
                              : PyFloat_Pack8(number, out, 0)) < 0) return -1;
        return wreath_pg_buffer_u32(output, (uint32_t)bytes) < 0 ||
               wreath_pg_buffer_append(output, out, bytes) < 0 ? -1 : 0;
    }
    case PG_TEXT:
    case PG_VARCHAR: {
        const char *data;
        Py_ssize_t length;
        if (!PyUnicode_Check(value)) {
            PyErr_Format(PyExc_TypeError, "text codec requires str, not %.200s",
                         Py_TYPE(value)->tp_name);
            return -1;
        }
        data = PyUnicode_AsUTF8AndSize(value, &length);
        if (data == NULL) return -1;
        if (length > INT32_MAX) {
            PyErr_SetString(PyExc_OverflowError, "text parameter too large");
            return -1;
        }
        return wreath_pg_buffer_u32(output, (uint32_t)length) < 0 ||
               wreath_pg_buffer_append(output, data, length) < 0 ? -1 : 0;
    }
    case PG_BYTEA:
        if (!PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError, "bytea codec requires bytes");
            return -1;
        }
        if (PyBytes_GET_SIZE(value) > INT32_MAX) {
            PyErr_SetString(PyExc_OverflowError, "bytea parameter too large");
            return -1;
        }
        return wreath_pg_buffer_u32(output, (uint32_t)PyBytes_GET_SIZE(value)) < 0 ||
               wreath_pg_buffer_append(
                   output, PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value)) < 0
               ? -1 : 0;
    default: {
        PyObject *encoded = wreath_pg_encode_binary_value(value, oid);
        int result;
        if (encoded == NULL) return -1;
        if (encoded == Py_None) {
            Py_DECREF(encoded);
            return wreath_pg_buffer_i32(output, -1);
        }
        if (PyBytes_GET_SIZE(encoded) > INT32_MAX) {
            Py_DECREF(encoded);
            PyErr_SetString(PyExc_OverflowError, "parameter too large");
            return -1;
        }
        result = wreath_pg_buffer_u32(output, (uint32_t)PyBytes_GET_SIZE(encoded)) < 0 ||
                 wreath_pg_buffer_append(
                     output, PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded)) < 0
                 ? -1 : 0;
        Py_DECREF(encoded);
        return result;
    }
    }
}

static unsigned long long
read_unsigned(const unsigned char *data, Py_ssize_t length)
{
    unsigned long long value = 0;
    for (Py_ssize_t i = 0; i < length; i++) {
        value = (value << 8) | data[i];
    }
    return value;
}

PyObject *
wreath_pg_decode_value(uint32_t oid, int format, PyObject *data)
{
    const unsigned char *raw;
    Py_ssize_t length;
    long long signed_value;
    unsigned long long value;
    double number;

    if (data == Py_None) {
        return Py_NewRef(Py_None);
    }
    if (PyBytes_Check(data)) {
        raw = (const unsigned char *)PyBytes_AS_STRING(data);
        length = PyBytes_GET_SIZE(data);
    } else if (PyMemoryView_Check(data) && PyMemoryView_GET_BUFFER(data)->ndim == 1 &&
               PyBuffer_IsContiguous(PyMemoryView_GET_BUFFER(data), 'C')) {
        raw = (const unsigned char *)PyMemoryView_GET_BUFFER(data)->buf;
        length = PyMemoryView_GET_BUFFER(data)->len;
    } else {
        PyErr_SetString(PyExc_TypeError, "field data must be contiguous bytes-like data or None");
        return NULL;
    }
    if (format == 0) {
        switch (oid) {
        case PG_BOOL:
            return PyBool_FromLong(length == 1 && raw[0] == 't');
        case PG_INT2:
        case PG_INT4:
        case PG_INT8: {
            PyObject *text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
            PyObject *result;
            if (text == NULL) return NULL;
            result = PyLong_FromUnicodeObject(text, 10);
            Py_DECREF(text);
            return result;
        }
        case PG_FLOAT4:
        case PG_FLOAT8: {
            PyObject *text = PyUnicode_DecodeASCII((char *)raw, length, "strict");
            PyObject *result;
            if (text == NULL) return NULL;
            result = PyFloat_FromString(text);
            Py_DECREF(text);
            return result;
        }
        case PG_TEXT:
        case PG_VARCHAR:
            return PyUnicode_DecodeUTF8((char *)raw, length, "strict");
        case PG_BYTEA:
            if (length >= 2 && raw[0] == '\\' && raw[1] == 'x') {
                return wreath_pg_decode_hex_bytea(raw + 2, length - 2);
            }
            /* Not hex-format text: hand back the wire bytes unchanged. */
            return Py_NewRef(data);
        case PG_UUID:
            return PyObject_CallFunction(uuid_type, "s#", raw, length);
        case PG_DATE:
        case PG_TIMESTAMP:
        case PG_TIMESTAMPTZ: {
            PyObject *text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
            PyObject *result;
            if (text == NULL) return NULL;
            result = PyObject_CallOneArg(
                oid == PG_DATE ? date_fromisoformat : datetime_fromisoformat, text
            );
            Py_DECREF(text);
            return result;
        }
        case PG_NUMERIC: {
            PyObject *text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
            PyObject *decoded;
            if (text == NULL) return NULL;
            decoded = PyObject_CallOneArg(decimal_type, text);
            Py_DECREF(text);
            return decoded;
        }
        case PG_JSON:
        case PG_JSONB:
            return PyUnicode_DecodeUTF8((const char *)raw, length, "strict");
        case PG_BIT: {
            PyObject *text = PyUnicode_DecodeASCII((const char *)raw, length, "strict");
            PyObject *checked;
            if (text == NULL) return NULL;
            checked = check_bit_string(text);
            Py_DECREF(text);
            return checked;
        }
        default:
            if (array_element_oid(oid) != 0) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "text-format array decoding is not supported; request binary results");
                return NULL;
            }
            {
                /* `vector` and `halfvec` both print "[1,2,3]", so one text
                 * decoder serves them; `sparsevec` prints "{1:1.5}/5". */
                int kind = wreath_pg_extension_kind(oid);
                if (kind == WREATH_PG_EXT_VECTOR || kind == WREATH_PG_EXT_HALFVEC) {
                    return decode_vector_text(raw, length);
                }
                if (kind == WREATH_PG_EXT_SPARSEVEC) {
                    return decode_sparsevec_text(raw, length);
                }
            }
            return Py_NewRef(data);
        }
    }
    if (format != 1) {
        PyErr_Format(PyExc_ValueError, "invalid field format code %d", format);
        return NULL;
    }
    if (oid == PG_BOOL) {
        if (length != 1) {
            PyErr_SetString(PyExc_ValueError, "invalid binary bool");
            return NULL;
        }
        return PyBool_FromLong(raw[0] != 0);
    }
    if ((oid == PG_INT2 && length == 2) ||
        (oid == PG_INT4 && length == 4) ||
        (oid == PG_INT8 && length == 8)) {
        value = read_unsigned(raw, length);
        if (length < 8 && (raw[0] & 0x80)) {
            value |= ~0ULL << (length * 8);
        }
        signed_value = (long long)value;
        return PyLong_FromLongLong(signed_value);
    }
    if (oid == PG_FLOAT4 && length == 4) {
        number = PyFloat_Unpack4((const char *)raw, 0);
        return PyFloat_FromDouble(number);
    }
    if (oid == PG_FLOAT8 && length == 8) {
        number = PyFloat_Unpack8((const char *)raw, 0);
        return PyFloat_FromDouble(number);
    }
    if (oid == PG_TEXT || oid == PG_VARCHAR) {
        return PyUnicode_DecodeUTF8((const char *)raw, length, "strict");
    }
    if (oid == PG_BYTEA) {
        return PyBytes_FromStringAndSize((const char *)raw, length);
    }
    if (oid == PG_UUID) {
        PyObject *uuid_bytes;
        PyObject *empty_args;
        PyObject *keywords;
        PyObject *result;
        if (length != 16) {
            PyErr_SetString(PyExc_ValueError, "invalid binary uuid");
            return NULL;
        }
        uuid_bytes = PyBytes_FromStringAndSize((const char *)raw, length);
        empty_args = PyTuple_New(0);
        keywords = uuid_bytes == NULL ? NULL : Py_BuildValue("{s:O}", "bytes", uuid_bytes);
        if (uuid_bytes == NULL || empty_args == NULL || keywords == NULL) {
            Py_XDECREF(uuid_bytes);
            Py_XDECREF(empty_args);
            Py_XDECREF(keywords);
            return NULL;
        }
        result = PyObject_Call(uuid_type, empty_args, keywords);
        Py_DECREF(uuid_bytes);
        Py_DECREF(empty_args);
        Py_DECREF(keywords);
        return result;
    }
    if (oid == PG_DATE) {
        if (length != 4) {
            PyErr_SetString(PyExc_ValueError, "invalid binary date length");
            return NULL;
        }
        value = read_unsigned(raw, 4);
        if (raw[0] & 0x80) value |= ~0ULL << 32;
        return date_from_days((int64_t)value);
    }
    if (oid == PG_TIMESTAMP || oid == PG_TIMESTAMPTZ) {
        if (length != 8) {
            PyErr_Format(PyExc_ValueError, "invalid binary value length for OID %u", oid);
            return NULL;
        }
        value = read_unsigned(raw, 8);
        return timestamp_from_micros((int64_t)value, oid == PG_TIMESTAMPTZ);
    }
    if (oid == PG_NUMERIC) {
        return decode_numeric(raw, length);
    }
    if (oid == PG_JSON) {
        return PyUnicode_DecodeUTF8((const char *)raw, length, "strict");
    }
    if (oid == PG_JSONB) {
        if (length < 1 || raw[0] != 1) {
            PyErr_SetString(PyExc_ValueError, "unsupported jsonb wire version");
            return NULL;
        }
        return PyUnicode_DecodeUTF8((const char *)raw + 1, length - 1, "strict");
    }
    if (oid == PG_BIT) {
        return decode_bit(raw, length);
    }
    if (array_element_oid(oid) != 0) {
        return decode_binary_array(raw, length);
    }
    {
        int kind = wreath_pg_extension_kind(oid);
        if (kind == WREATH_PG_EXT_VECTOR) return decode_vector(raw, length);
        if (kind == WREATH_PG_EXT_HALFVEC) return decode_halfvec(raw, length);
        if (kind == WREATH_PG_EXT_SPARSEVEC) return decode_sparsevec(raw, length);
    }
    return Py_NewRef(data);
}

/* Decode one binary PostgreSQL array (format 1). Only 0- and 1-dimensional
   arrays are supported; each element is decoded through the scalar decoder for
   the wire's element OID, and NULL elements (length -1) become Py_None. The
   returned list is the element types' wire values; Array.from_wire finishes
   them (e.g. jsonb text -> object). */
static PyObject *
decode_binary_array(const unsigned char *raw, Py_ssize_t length)
{
    uint32_t ndim, element_oid;
    int64_t dim_length;
    PyObject *list;
    Py_ssize_t offset;

    if (length < 12) {
        PyErr_SetString(PyExc_ValueError, "binary array header is truncated");
        return NULL;
    }
    ndim = (uint32_t)read_unsigned(raw, 4);
    /* raw + 4 is the has-null flag word; element lengths carry NULLs directly. */
    element_oid = (uint32_t)read_unsigned(raw + 8, 4);
    if (ndim == 0) {
        return PyList_New(0);
    }
    if (ndim != 1) {
        PyErr_SetString(PyExc_ValueError, "only one-dimensional arrays are supported");
        return NULL;
    }
    if (length < 20) {
        PyErr_SetString(PyExc_ValueError, "binary array dimension is truncated");
        return NULL;
    }
    dim_length = (int64_t)(int32_t)(uint32_t)read_unsigned(raw + 12, 4);
    /* raw + 16 is the lower bound, which the element order already encodes. */
    if (dim_length < 0) {
        PyErr_SetString(PyExc_ValueError, "invalid array dimension length");
        return NULL;
    }
    list = PyList_New((Py_ssize_t)dim_length);
    if (list == NULL) return NULL;
    offset = 20;
    for (int64_t index = 0; index < dim_length; index++) {
        int64_t element_length;
        PyObject *chunk;
        PyObject *element;
        if (length - offset < 4) {
            Py_DECREF(list);
            PyErr_SetString(PyExc_ValueError, "binary array element length is truncated");
            return NULL;
        }
        element_length = (int64_t)(int32_t)(uint32_t)read_unsigned(raw + offset, 4);
        offset += 4;
        if (element_length == -1) {
            PyList_SET_ITEM(list, (Py_ssize_t)index, Py_NewRef(Py_None));
            continue;
        }
        if (element_length < 0 || length - offset < element_length) {
            Py_DECREF(list);
            PyErr_SetString(PyExc_ValueError, "binary array element is truncated");
            return NULL;
        }
        chunk = PyBytes_FromStringAndSize(
            (const char *)raw + offset, (Py_ssize_t)element_length);
        if (chunk == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        element = wreath_pg_decode_value(element_oid, 1, chunk);
        Py_DECREF(chunk);
        if (element == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, (Py_ssize_t)index, element);
        offset += element_length;
    }
    if (offset != length) {
        Py_DECREF(list);
        PyErr_SetString(PyExc_ValueError, "binary array has trailing bytes");
        return NULL;
    }
    return list;
}

static PyObject *
codec_encode_text(PyObject *module, PyObject *args)
{
    PyObject *value;
    unsigned int oid;
    (void)module;
    if (!PyArg_ParseTuple(args, "OI:_encode_text", &value, &oid)) return NULL;
    return wreath_pg_encode_text_value(value, oid);
}

static PyObject *
codec_encode_binary(PyObject *module, PyObject *args)
{
    PyObject *value;
    unsigned int oid;
    (void)module;
    if (!PyArg_ParseTuple(args, "OI:_encode_binary", &value, &oid)) return NULL;
    return wreath_pg_encode_binary_value(value, oid);
}

static PyObject *
codec_decode(PyObject *module, PyObject *args)
{
    PyObject *data;
    unsigned int oid;
    int format;
    (void)module;
    if (!PyArg_ParseTuple(args, "IiO:_decode_value", &oid, &format, &data)) return NULL;
    return wreath_pg_decode_value(oid, format, data);
}

static PyMethodDef codec_methods[] = {
    {"_encode_text", codec_encode_text, METH_VARARGS, NULL},
    {"_encode_binary", codec_encode_binary, METH_VARARGS, NULL},
    {"_decode_value", codec_decode, METH_VARARGS, NULL},
    {"_register_extension_type", codec_register_extension_type, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL}
};

int
wreath_pg_codec_init(PyObject *module)
{
    PyObject *uuid_module;
    PyObject *datetime_module = NULL;
    PyObject *date_type = NULL;
    PyObject *datetime_type = NULL;
    PyObject *timezone_type = NULL;

    PyDateTime_IMPORT;
    if (PyDateTimeAPI == NULL) return -1;

    uuid_module = PyImport_ImportModule("uuid");
    if (uuid_module == NULL) return -1;
    uuid_type = PyObject_GetAttrString(uuid_module, "UUID");
    Py_DECREF(uuid_module);
    if (uuid_type == NULL) return -1;

    {
        PyObject *decimal_module = PyImport_ImportModule("decimal");
        if (decimal_module == NULL) return -1;
        decimal_type = PyObject_GetAttrString(decimal_module, "Decimal");
        Py_DECREF(decimal_module);
        if (decimal_type == NULL) return -1;
        str_as_tuple = PyUnicode_InternFromString("as_tuple");
        if (str_as_tuple == NULL) return -1;
    }

    {
        /* `wreath._sparsevec` imports nothing from wreath, so resolving it here
           -- while `wreath.postgres` is still executing its own module body to
           select this backend -- cannot close an import cycle. It is a separate
           module for exactly that reason. */
        PyObject *sparsevec_module = PyImport_ImportModule("wreath._sparsevec");
        if (sparsevec_module == NULL) return -1;
        sparsevec_type = PyObject_GetAttrString(sparsevec_module, "SparseVector");
        Py_DECREF(sparsevec_module);
        if (sparsevec_type == NULL) return -1;
    }

    datetime_module = PyImport_ImportModule("datetime");
    if (datetime_module == NULL) return -1;
    date_type = PyObject_GetAttrString(datetime_module, "date");
    datetime_type = PyObject_GetAttrString(datetime_module, "datetime");
    timezone_type = PyObject_GetAttrString(datetime_module, "timezone");
    Py_DECREF(datetime_module);
    if (date_type == NULL || datetime_type == NULL || timezone_type == NULL) {
        Py_XDECREF(date_type);
        Py_XDECREF(datetime_type);
        Py_XDECREF(timezone_type);
        return -1;
    }
    date_fromisoformat = PyObject_GetAttrString(date_type, "fromisoformat");
    datetime_fromisoformat = PyObject_GetAttrString(datetime_type, "fromisoformat");
    utc_timezone = PyObject_GetAttrString(timezone_type, "utc");
    Py_DECREF(date_type);
    Py_DECREF(datetime_type);
    Py_DECREF(timezone_type);
    str_isoformat = PyUnicode_InternFromString("isoformat");
    str_utcoffset = PyUnicode_InternFromString("utcoffset");
    str_space = PyUnicode_FromString(" ");
    if (date_fromisoformat == NULL || datetime_fromisoformat == NULL ||
        utc_timezone == NULL || str_isoformat == NULL || str_utcoffset == NULL ||
        str_space == NULL) {
        return -1;
    }
    return PyModule_AddFunctions(module, codec_methods);
}

void
wreath_pg_codec_fini(void)
{
    Py_CLEAR(uuid_type);
    Py_CLEAR(decimal_type);
    Py_CLEAR(sparsevec_type);
    Py_CLEAR(str_as_tuple);
    Py_CLEAR(utc_timezone);
    Py_CLEAR(date_fromisoformat);
    Py_CLEAR(datetime_fromisoformat);
    Py_CLEAR(str_isoformat);
    Py_CLEAR(str_utcoffset);
    Py_CLEAR(str_space);
}
