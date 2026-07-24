#include "codec.h"

#include "buffer.h"

#include <datetime.h>
#include <limits.h>
#include <string.h>

/* Nibble value per ASCII byte; 0xFF marks a non-hex byte. Decoding `bytea`
 * through this table replaces a per-field `binascii` import and method call. */
static const unsigned char wreath_pg_hex_nibble[256] = {
    ['0'] = 0, ['1'] = 1, ['2'] = 2, ['3'] = 3, ['4'] = 4,
    ['5'] = 5, ['6'] = 6, ['7'] = 7, ['8'] = 8, ['9'] = 9,
    ['a'] = 10, ['b'] = 11, ['c'] = 12, ['d'] = 13, ['e'] = 14, ['f'] = 15,
    ['A'] = 10, ['B'] = 11, ['C'] = 12, ['D'] = 13, ['E'] = 14, ['F'] = 15,
    /* Every other byte stays 0, so a lone table lookup cannot distinguish '0'
     * from an invalid byte; wreath_pg_hex_valid below carries that distinction. */
};

static const unsigned char wreath_pg_hex_valid[256] = {
    ['0'] = 1, ['1'] = 1, ['2'] = 1, ['3'] = 1, ['4'] = 1,
    ['5'] = 1, ['6'] = 1, ['7'] = 1, ['8'] = 1, ['9'] = 1,
    ['a'] = 1, ['b'] = 1, ['c'] = 1, ['d'] = 1, ['e'] = 1, ['f'] = 1,
    ['A'] = 1, ['B'] = 1, ['C'] = 1, ['D'] = 1, ['E'] = 1, ['F'] = 1,
};

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
    for (Py_ssize_t i = 0; i < length; i += 2) {
        unsigned char hi = data[i];
        unsigned char lo = data[i + 1];
        if (!wreath_pg_hex_valid[hi] || !wreath_pg_hex_valid[lo]) {
            /* Report the first invalid byte, matching binascii's strictness. */
            Py_DECREF(result);
            PyErr_SetString(PyExc_ValueError,
                            "bytea hex data contains a non-hexadecimal digit");
            return NULL;
        }
        out[i / 2] = (char)((wreath_pg_hex_nibble[hi] << 4) | wreath_pg_hex_nibble[lo]);
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
#define PG_DATE 1082
#define PG_TIMESTAMP 1114
#define PG_TIMESTAMPTZ 1184
#define PG_UUID 2950
#define PG_JSONB 3802

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
#define PG_UUID_ARRAY 2951
#define PG_JSONB_ARRAY 3807

/* The element OID for an array OID, or 0 when the OID is not a supported array.
 * TODO(pure-twin): the pure backend (_pure/postgres.py) has no array codec yet,
 * so Array(...) columns require the native build until that twin is added. */
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
    case PG_UUID_ARRAY: return PG_UUID;
    case PG_JSONB_ARRAY: return PG_JSONB;
    default: return 0;
    }
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

/* Text-format date/time output goes through isoformat() so the native and
   reference backends emit byte-identical parameters. */
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
    case PG_JSON:
    case PG_JSONB:
        return json_utf8(value);
    default:
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
    default: {
        uint32_t element_oid = array_element_oid(oid);
        if (element_oid != 0) {
            return encode_binary_array(value, element_oid);
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
        case PG_JSON:
        case PG_JSONB:
            return PyUnicode_DecodeUTF8((const char *)raw, length, "strict");
        default:
            if (array_element_oid(oid) != 0) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "text-format array decoding is not supported; request binary results");
                return NULL;
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
    if (array_element_oid(oid) != 0) {
        return decode_binary_array(raw, length);
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
    Py_CLEAR(utc_timezone);
    Py_CLEAR(date_fromisoformat);
    Py_CLEAR(datetime_fromisoformat);
    Py_CLEAR(str_isoformat);
    Py_CLEAR(str_utcoffset);
    Py_CLEAR(str_space);
}
