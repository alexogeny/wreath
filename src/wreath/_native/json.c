/* Compact JSON encoder and decoder over UTF-8 byte buffers.
 *
 * Encoder differences from stdlib json.dumps: bytes output, str-only dict keys, and NaN/Infinity
 * rejected. The decoder mirrors stdlib json.loads semantics: NaN/Infinity/
 * -Infinity accepted, lone surrogate escapes allowed, last duplicate key
 * wins, ints of any size. Inputs that stdlib would sniff as UTF-16/32 are
 * delegated to stdlib json so encoding detection stays byte-for-byte
 * compatible.
 */
#include "wreathcore.h"
#include "bytes_writer.h"
#include "ryu/ryu.h"

#include "simd.h"

#include <math.h>

#define WREATH_JSON_MAX_DEPTH 1000
#define json_token_tape 256  /* signal/cancellation boundary in decoded tokens */

/* ------------------------------------------------------------------ */
/* Encoder                                                            */
/* ------------------------------------------------------------------ */

/* The writer appends straight into a growable PyBytes object so finishing a
 * document is a shrinking resize instead of an extra buffer copy. */
static inline int
write_char(WreathBytesWriter *w, char c)
{
    if (wreath_writer_reserve(w, 1) < 0) {
        return -1;
    }
    w->buf[w->len++] = c;
    return 0;
}

static const char HEX[] = "0123456789abcdef";

/* The scan for bytes a JSON string cannot carry unescaped ('"', '\\', and the
 * controls) lives in `simd.h`, which picks a width per call. It used to be an
 * `#if defined(__AVX2__)` arm here, which no build ever defined: nothing in
 * `setup.py` passes `-mavx2`, so the wheel and every local build ran the SWAR
 * fallback and the vector code was never compiled at all. */

int
wreath_json_write_string(WreathBytesWriter *w, PyObject *str)
{
    Py_ssize_t len;
    const char *utf8 = PyUnicode_AsUTF8AndSize(str, &len);
    if (utf8 == NULL) {
        return -1;
    }
    /* The overwhelmingly common string needs no escaping at all, and for that
     * one the whole result -- both quotes and the body -- is known before
     * anything is written. Reserving once and copying once removes three
     * capacity checks and a call per string, which is most of the cost at the
     * lengths a JSON document is actually made of: keys and short values.
     * Escaped strings fall through to the general loop below unchanged. */
    if (wreath_writer_reserve(w, len + 2) < 0) {
        return -1;
    }
    {
        unsigned unused_high = 0;
        Py_ssize_t plain = (Py_ssize_t)wreath_json_run(utf8, (ptrdiff_t)len, &unused_high);
        if (plain == len) {
            char *out = w->buf + w->len;
            out[0] = '"';
            memcpy(out + 1, utf8, (size_t)len);
            out[len + 1] = '"';
            w->len += len + 2;
            return 0;
        }
    }
    if (write_char(w, '"') < 0) {
        return -1;
    }
    Py_ssize_t i = 0;
    while (i < len) {
        Py_ssize_t run_start = i;
        unsigned unused_high = 0;
        i += (Py_ssize_t)wreath_json_run(utf8 + i, (ptrdiff_t)(len - i), &unused_high);
        if (i > run_start && wreath_writer_write(w, utf8 + run_start, i - run_start) < 0) {
            return -1;
        }
        if (i == len) {
            break;
        }
        uint8_t c = (uint8_t)utf8[i++];
        if (wreath_writer_reserve(w, 6) < 0) {
            return -1;
        }
        char *out = w->buf + w->len;
        switch (c) {
            case '"':
                out[0] = '\\';
                out[1] = '"';
                w->len += 2;
                break;
            case '\\':
                out[0] = '\\';
                out[1] = '\\';
                w->len += 2;
                break;
            case '\b':
                out[0] = '\\';
                out[1] = 'b';
                w->len += 2;
                break;
            case '\f':
                out[0] = '\\';
                out[1] = 'f';
                w->len += 2;
                break;
            case '\n':
                out[0] = '\\';
                out[1] = 'n';
                w->len += 2;
                break;
            case '\r':
                out[0] = '\\';
                out[1] = 'r';
                w->len += 2;
                break;
            case '\t':
                out[0] = '\\';
                out[1] = 't';
                w->len += 2;
                break;
            default:
                out[0] = '\\';
                out[1] = 'u';
                out[2] = '0';
                out[3] = '0';
                out[4] = HEX[c >> 4];
                out[5] = HEX[c & 0x0f];
                w->len += 6;
                break;
        }
    }
    return write_char(w, '"');
}

static const char DIGIT_PAIRS[201] =
    "00010203040506070809"
    "10111213141516171819"
    "20212223242526272829"
    "30313233343536373839"
    "40414243444546474849"
    "50515253545556575859"
    "60616263646566676869"
    "70717273747576777879"
    "80818283848586878889"
    "90919293949596979899";

static inline int
write_ll(WreathBytesWriter *w, long long value)
{
    /* Write directly into the reserved buffer tail: itoa emits backwards
     * into a scratch, then one small copy lands it. */
    char tmp[24];
    char *p = tmp + sizeof(tmp);
    int negative = value < 0;
    /* Negate via unsigned so LLONG_MIN stays defined. */
    unsigned long long v =
        negative ? (unsigned long long)(-(value + 1)) + 1 : (unsigned long long)value;
    while (v >= 100) {
        unsigned int idx = (unsigned int)(v % 100) * 2;
        v /= 100;
        *--p = DIGIT_PAIRS[idx + 1];
        *--p = DIGIT_PAIRS[idx];
    }
    if (v >= 10) {
        unsigned int idx = (unsigned int)v * 2;
        *--p = DIGIT_PAIRS[idx + 1];
        *--p = DIGIT_PAIRS[idx];
    }
    else {
        *--p = (char)('0' + v);
    }
    if (negative) {
        *--p = '-';
    }
    return wreath_writer_write(w, p, tmp + sizeof(tmp) - p);
}

static int
write_long(WreathBytesWriter *w, PyObject *obj)
{
    /* Compact ints (single internal digit, the overwhelmingly common case)
     * expose their value without the general conversion machinery. */
    if (PyUnstable_Long_IsCompact((PyLongObject *)obj)) {
        return write_ll(w, (long long)PyUnstable_Long_CompactValue((PyLongObject *)obj));
    }
    int overflow = 0;
    long long value = PyLong_AsLongLongAndOverflow(obj, &overflow);
    if (value == -1 && !overflow && PyErr_Occurred()) {
        return -1;
    }
    if (!overflow) {
        return write_ll(w, value);
    }
    PyObject *text = PyObject_Str(obj);
    if (text == NULL) {
        return -1;
    }
    Py_ssize_t len;
    const char *utf8 = PyUnicode_AsUTF8AndSize(text, &len);
    int rc = (utf8 != NULL) ? wreath_writer_write(w, utf8, len) : -1;
    Py_DECREF(text);
    return rc;
}

static int
write_double(WreathBytesWriter *w, double value)
{
    if (!isfinite(value)) {
        PyErr_SetString(PyExc_ValueError, "JSON values must be finite numbers");
        return -1;
    }
    if (wreath_writer_reserve(w, 25) < 0) {
        return -1;
    }
    int length = wreath_ryu_d2s(value, w->buf + w->len);
    if (length < 0) {
        PyErr_SetString(PyExc_ValueError, "JSON values must be finite numbers");
        return -1;
    }
    w->len += length;
    return 0;
}

/* Set once by `json_configure`; both NULL until then, and the encoder simply
 * raises its ordinary TypeError in that case. */
static PyObject *temporal_types = NULL;  /* tuple of date/time/datetime/timedelta */
static PyObject *format_iso = NULL;      /* wreath.temporal.format_iso */

int
wreath_json_write_value(WreathBytesWriter *w, PyObject *obj, int depth)
{
    if (obj == Py_None) {
        return wreath_writer_write(w, "null", 4);
    }
    if (obj == Py_True) {
        return wreath_writer_write(w, "true", 4);
    }
    if (obj == Py_False) {
        return wreath_writer_write(w, "false", 5);
    }

    PyTypeObject *type = Py_TYPE(obj);
    if (type == &PyUnicode_Type || PyUnicode_Check(obj)) {
        return wreath_json_write_string(w, obj);
    }
    if (type == &PyLong_Type || PyLong_Check(obj)) {
        return write_long(w, obj);
    }
    if (type == &PyFloat_Type || PyFloat_Check(obj)) {
        return write_double(w, PyFloat_AS_DOUBLE(obj));
    }

    if (depth >= WREATH_JSON_MAX_DEPTH) {
        PyErr_SetString(PyExc_ValueError, "JSON structure is too deeply nested");
        return -1;
    }

    if (type == &PyDict_Type || PyDict_Check(obj)) {
        if (write_char(w, '{') < 0) {
            return -1;
        }
        Py_ssize_t pos = 0;
        PyObject *key, *value;
        int first = 1;
        while (PyDict_Next(obj, &pos, &key, &value)) {
            if (!PyUnicode_Check(key)) {
                PyErr_Format(PyExc_TypeError, "JSON object keys must be str, got %.100s",
                             Py_TYPE(key)->tp_name);
                return -1;
            }
            if (!first && write_char(w, ',') < 0) {
                return -1;
            }
            first = 0;
            if (wreath_json_write_string(w, key) < 0 || write_char(w, ':') < 0 ||
                wreath_json_write_value(w, value, depth + 1) < 0) {
                return -1;
            }
        }
        return write_char(w, '}');
    }

    if (type == &PyList_Type || PyList_Check(obj)) {
        if (write_char(w, '[') < 0) {
            return -1;
        }
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(obj); i++) {
            if (i > 0 && write_char(w, ',') < 0) {
                return -1;
            }
            if (wreath_json_write_value(w, PyList_GET_ITEM(obj, i), depth + 1) < 0) {
                return -1;
            }
        }
        return write_char(w, ']');
    }

    if (type == &PyTuple_Type || PyTuple_Check(obj)) {
        if (write_char(w, '[') < 0) {
            return -1;
        }
        Py_ssize_t size = PyTuple_GET_SIZE(obj);
        for (Py_ssize_t i = 0; i < size; i++) {
            if (i > 0 && write_char(w, ',') < 0) {
                return -1;
            }
            if (wreath_json_write_value(w, PyTuple_GET_ITEM(obj, i), depth + 1) < 0) {
                return -1;
            }
        }
        return write_char(w, ']');
    }

    if (PyCapsule_IsValid(obj, "wreath.graphql.projection")) {
        return wreath_graphql_write_projection(w, obj, depth);
    }

    /* Temporal values are rendered inline rather than by rewriting the document.
     *
     * `wreath._json.dumps` used to encode, catch TypeError, rebuild the whole
     * payload through `temporal.jsonable`, and encode again. The retry itself is
     * cheap -- the failed attempt aborts on the first temporal value in ~0.5us --
     * but the rebuild is not: it reconstructs every dict and list in the
     * document in Python. Measured 2026-07-27 on 1000 ORM-shaped rows carrying
     * one timestamp each: 91us with no timestamps, 1550us with them, of which
     * the rebuild was 1422us (92%). One `created_at` column made encoding 17x
     * slower.
     *
     * Formatting stays in Python deliberately. `format_iso` is the single seam
     * every surface renders through, and `datetime.isoformat` is already C, so
     * reimplementing it here would risk divergence for no measured gain -- see
     * the note on `wreath.temporal.format_iso`. Calling it keeps the bytes
     * identical by construction; what this removes is the walk, not the format.
     */
    if (temporal_types != NULL && format_iso != NULL) {
        int is_temporal = PyObject_IsInstance(obj, temporal_types);
        if (is_temporal < 0) {
            return -1;
        }
        if (is_temporal) {
            PyObject *text = PyObject_CallOneArg(format_iso, obj);
            if (text == NULL) {
                return -1;
            }
            if (!PyUnicode_Check(text)) {
                Py_DECREF(text);
                PyErr_SetString(PyExc_TypeError,
                                "format_iso must return str");
                return -1;
            }
            int rc = wreath_json_write_string(w, text);
            Py_DECREF(text);
            return rc;
        }
    }

    /* An explicitly JSON-aware result crosses into its declared wire shape at
     * the encoder boundary.  This used to fail the first native pass, rebuild
     * the complete result recursively in Python through temporal.jsonable,
     * then encode that second graph.  Looking the hook up on the type preserves
     * the public contract (and deliberately ignores instance __getattr__), but
     * keeps the recursive walk in this request-owned writer.  The hook is the
     * materialization boundary: its returned Python value is consumed
     * immediately and never cached or mutated here.  Temporal values stay
     * above this lookup because they are common leaves and already have a
     * resolved formatter; asking every datetime type for a missing hook added
     * one class lookup per bucket. */
    PyObject *hook = NULL;
    int has_hook = PyObject_GetOptionalAttrString(
        (PyObject *)type, "__jsonable__", &hook);
    if (has_hook < 0) {
        return -1;
    }
    if (has_hook) {
        PyObject *materialized = PyObject_CallOneArg(hook, obj);
        Py_DECREF(hook);
        if (materialized == NULL) {
            return -1;
        }
        if (materialized == obj) {
            Py_DECREF(materialized);
            PyErr_Format(
                PyExc_TypeError,
                "object of type %.200s returned itself from __jsonable__",
                type->tp_name);
            return -1;
        }
        int rc = wreath_json_write_value(w, materialized, depth + 1);
        Py_DECREF(materialized);
        return rc;
    }

    PyErr_Format(PyExc_TypeError, "object of type %.100s is not JSON serializable",
                 type->tp_name);
    return -1;
}

/* json_configure(temporal_types, format_iso) -> None

   Installed once at import by `wreath._json`. Kept as a configure hook rather
   than an import from C so this file never reaches back into Python packages,
   matching `template_configure` and `orm_shape_configure`. */
PyObject *
wreath_json_configure(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *types;
    PyObject *formatter;

    if (!PyArg_ParseTuple(args, "OO:json_configure", &types, &formatter)) {
        return NULL;
    }
    Py_XSETREF(temporal_types, Py_NewRef(types));
    Py_XSETREF(format_iso, Py_NewRef(formatter));
    Py_RETURN_NONE;
}

PyObject *
wreath_json_dumps(PyObject *Py_UNUSED(self), PyObject *obj)
{
    WreathBytesWriter w;
    if (wreath_writer_init(&w, 256) < 0) {
        return NULL;
    }
    if (wreath_json_write_value(&w, obj, 0) < 0) {
        Py_XDECREF(w.bytes);
        return NULL;
    }
    return wreath_writer_finish(&w);
}

/* ------------------------------------------------------------------ */
/* Decoder                                                            */
/* ------------------------------------------------------------------ */

#define WREATH_KEY_CACHE_SIZE 512
#define WREATH_KEY_CACHE_MAX_LEN 48

typedef struct {
    const char *start;
    const char *cur;
    const char *end;
    Py_ssize_t tokens;
    PyObject *key_cache[WREATH_KEY_CACHE_SIZE];
} Parser;

static PyObject *
decode_error(Parser *p, const char *at, const char *msg)
{
    PyErr_Format(PyExc_ValueError, "%s: char %zd", msg, (Py_ssize_t)(at - p->start));
    return NULL;
}

static inline void
skip_ws(Parser *p)
{
    const char *cur = p->cur;
    while (cur < p->end &&
           (*cur == ' ' || *cur == '\t' || *cur == '\n' || *cur == '\r')) {
        cur++;
    }
    p->cur = cur;
}

/* Repeated object keys resolve to shared str objects through a small
 * parser-local direct map. Similar objects within one document avoid repeated
 * decoding without allowing unrelated clients to evict one another's keys. */

/* p->cur points just past the opening quote on entry, just past the closing
 * quote on success. */
static PyObject *
parse_string_ex(Parser *p, int as_key)
{
    const char *start = p->cur;
    const char *cur = start;
    const char *end = p->end;
    uint64_t high = 0;  /* accumulates every scanned byte: ASCII iff no 0x80 bit */

    {
        /* One dispatched scan to the first byte this fast path cannot carry:
         * '"', '\\', or a control. `seen_high` reports whether anything it
         * passed over was non-ASCII, which decides between a one-byte str and
         * a UTF-8 decode below. */
        unsigned seen_high = 0;
        cur += wreath_json_run(cur, (ptrdiff_t)(end - cur), &seen_high);
        if (seen_high) {
            high = WREATH_SWAR_HIGH;
        }
    }
    while (cur < end) {
        uint8_t c = (uint8_t)*cur;
        if (c == '"') {
            Py_ssize_t n = cur - start;
            int ascii = (high & WREATH_SWAR_HIGH) == 0;
            PyObject *s;
            if (as_key && n > 0 && n <= WREATH_KEY_CACHE_MAX_LEN) {
                uint64_t h = 1469598103934665603ULL;
                for (Py_ssize_t k = 0; k < n; k++) {
                    h ^= (uint8_t)start[k];
                    h *= 1099511628211ULL;
                }
                size_t slot = (size_t)(h & (WREATH_KEY_CACHE_SIZE - 1));
                PyObject *entry = p->key_cache[slot];
                if (entry != NULL) {
                    Py_ssize_t entry_len;
                    const char *entry_data = PyUnicode_AsUTF8AndSize(entry, &entry_len);
                    if (entry_data == NULL) {
                        PyErr_Clear();
                    }
                    else if (entry_len == n && memcmp(entry_data, start, (size_t)n) == 0) {
                        p->cur = cur + 1;
                        return Py_NewRef(entry);
                    }
                }
                s = ascii ? PyUnicode_New(n, 127) : PyUnicode_DecodeUTF8(start, n, NULL);
                if (s == NULL) {
                    return NULL;
                }
                if (ascii) {
                    memcpy(PyUnicode_1BYTE_DATA(s), start, (size_t)n);
                }
                p->cur = cur + 1;
                Py_XSETREF(p->key_cache[slot], Py_NewRef(s));
                return s;
            }
            if (ascii) {
                s = PyUnicode_New(n, 127);
                if (s == NULL) {
                    return NULL;
                }
                memcpy(PyUnicode_1BYTE_DATA(s), start, (size_t)n);
            }
            else {
                s = PyUnicode_DecodeUTF8(start, n, NULL);
            }
            if (s != NULL) {
                p->cur = cur + 1;
            }
            return s;
        }
        if (c == '\\') {
            break;
        }
        if (c < 0x20) {
            return decode_error(p, cur, "Invalid control character in string");
        }
        high |= c;
        cur++;
    }
    if (cur >= end) {
        return decode_error(p, start - 1, "Unterminated string");
    }

    /* Escapes present: assemble through a unicode writer. */
    PyUnicodeWriter *w = PyUnicodeWriter_Create(cur - start + 8);
    if (w == NULL) {
        return NULL;
    }
    if (cur > start && PyUnicodeWriter_WriteUTF8(w, start, cur - start) < 0) {
        goto fail;
    }
    while (cur < end) {
        uint8_t c = (uint8_t)*cur;
        if (c == '"') {
            p->cur = cur + 1;
            return PyUnicodeWriter_Finish(w);
        }
        if (c < 0x20) {
            decode_error(p, cur, "Invalid control character in string");
            goto fail;
        }
        if (c != '\\') {
            const char *run = cur;
            unsigned unused_high = 0;
            cur += wreath_json_run(cur, (ptrdiff_t)(end - cur), &unused_high);
            if (PyUnicodeWriter_WriteUTF8(w, run, cur - run) < 0) {
                goto fail;
            }
            continue;
        }
        cur++;
        if (cur >= end) {
            decode_error(p, cur - 1, "Unterminated string");
            goto fail;
        }
        Py_UCS4 ch;
        switch (*cur) {
            case '"': ch = '"'; cur++; break;
            case '\\': ch = '\\'; cur++; break;
            case '/': ch = '/'; cur++; break;
            case 'b': ch = '\b'; cur++; break;
            case 'f': ch = '\f'; cur++; break;
            case 'n': ch = '\n'; cur++; break;
            case 'r': ch = '\r'; cur++; break;
            case 't': ch = '\t'; cur++; break;
            case 'u': {
                if (end - cur < 5) {
                    decode_error(p, cur - 1, "Invalid \\uXXXX escape");
                    goto fail;
                }
                unsigned int u = 0;
                for (int k = 1; k <= 4; k++) {
                    uint8_t h = (uint8_t)cur[k];
                    unsigned int digit;
                    if (h >= '0' && h <= '9') {
                        digit = h - '0';
                    }
                    else if ((h | 0x20) >= 'a' && (h | 0x20) <= 'f') {
                        digit = (h | 0x20) - 'a' + 10;
                    }
                    else {
                        decode_error(p, cur - 1, "Invalid \\uXXXX escape");
                        goto fail;
                    }
                    u = (u << 4) | digit;
                }
                cur += 5;
                ch = u;
                /* Combine a surrogate pair; a lone surrogate is kept as-is,
                 * matching stdlib json. */
                if (u >= 0xD800 && u <= 0xDBFF && end - cur >= 6 &&
                    cur[0] == '\\' && cur[1] == 'u') {
                    unsigned int lo = 0;
                    int valid = 1;
                    for (int k = 2; k <= 5; k++) {
                        uint8_t h = (uint8_t)cur[k];
                        unsigned int digit;
                        if (h >= '0' && h <= '9') {
                            digit = h - '0';
                        }
                        else if ((h | 0x20) >= 'a' && (h | 0x20) <= 'f') {
                            digit = (h | 0x20) - 'a' + 10;
                        }
                        else {
                            valid = 0;
                            break;
                        }
                        lo = (lo << 4) | digit;
                    }
                    if (valid && lo >= 0xDC00 && lo <= 0xDFFF) {
                        ch = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00);
                        cur += 6;
                    }
                }
                break;
            }
            default:
                decode_error(p, cur - 1, "Invalid \\escape");
                goto fail;
        }
        if (PyUnicodeWriter_WriteChar(w, ch) < 0) {
            goto fail;
        }
    }
    decode_error(p, start - 1, "Unterminated string");

fail:
    PyUnicodeWriter_Discard(w);
    return NULL;
}

static inline PyObject *
parse_string(Parser *p)
{
    return parse_string_ex(p, 0);
}

/* Number grammar identical to stdlib json's NUMBER_RE: an invalid fraction
 * or exponent is not consumed, so "1." parses as 1 and the '.' is left for
 * the caller to reject, exactly like stdlib. */
static PyObject *
parse_number(Parser *p)
{
    const char *start = p->cur;
    const char *cur = start;
    const char *end = p->end;
    int negative = 0;

    if (cur < end && *cur == '-') {
        negative = 1;
        cur++;
    }
    if (cur >= end || *cur < '0' || *cur > '9') {
        return decode_error(p, start, "Expecting value");
    }
    const char *digits = cur;
    if (*cur == '0') {
        cur++; /* leading zeros are not part of the number */
    }
    else {
        while (cur < end && *cur >= '0' && *cur <= '9') {
            cur++;
        }
    }
    const char *int_end = cur;
    int is_float = 0;
    if (cur < end && *cur == '.') {
        const char *f = cur + 1;
        if (f < end && *f >= '0' && *f <= '9') {
            is_float = 1;
            cur = f;
            while (cur < end && *cur >= '0' && *cur <= '9') {
                cur++;
            }
        }
    }
    if (cur < end && (*cur == 'e' || *cur == 'E')) {
        const char *e = cur + 1;
        if (e < end && (*e == '+' || *e == '-')) {
            e++;
        }
        if (e < end && *e >= '0' && *e <= '9') {
            is_float = 1;
            cur = e;
            while (cur < end && *cur >= '0' && *cur <= '9') {
                cur++;
            }
        }
    }
    p->cur = cur;

    if (!is_float) {
        Py_ssize_t ndigits = int_end - digits;
        if (ndigits <= 18) {
            long long value = 0;
            for (const char *d = digits; d < int_end; d++) {
                value = value * 10 + (*d - '0');
            }
            return PyLong_FromLongLong(negative ? -value : value);
        }
        cur = int_end;
        p->cur = int_end;
    }

    Py_ssize_t token_len = cur - start;
    char stack_buf[64];
    char *buf = stack_buf;
    if (token_len + 1 > (Py_ssize_t)sizeof(stack_buf)) {
        buf = PyMem_Malloc((size_t)token_len + 1);
        if (buf == NULL) {
            return PyErr_NoMemory();
        }
    }
    memcpy(buf, start, (size_t)token_len);
    buf[token_len] = '\0';

    PyObject *result;
    if (is_float) {
        double value = PyOS_string_to_double(buf, NULL, NULL);
        result = (value == -1.0 && PyErr_Occurred()) ? NULL : PyFloat_FromDouble(value);
    }
    else {
        result = PyLong_FromString(buf, NULL, 10);
    }
    if (buf != stack_buf) {
        PyMem_Free(buf);
    }
    return result;
}

static PyObject *parse_value(Parser *p);

static PyObject *
parse_object(Parser *p)
{
    PyObject *dict = PyDict_New();
    if (dict == NULL) {
        return NULL;
    }
    skip_ws(p);
    if (p->cur < p->end && *p->cur == '}') {
        p->cur++;
        return dict;
    }
    for (;;) {
        skip_ws(p);
        if (p->cur >= p->end || *p->cur != '"') {
            decode_error(p, p->cur, "Expecting property name enclosed in double quotes");
            goto fail;
        }
        p->cur++;
        PyObject *key = parse_string_ex(p, 1);
        if (key == NULL) {
            goto fail;
        }
        skip_ws(p);
        if (p->cur >= p->end || *p->cur != ':') {
            Py_DECREF(key);
            decode_error(p, p->cur, "Expecting ':' delimiter");
            goto fail;
        }
        p->cur++;
        PyObject *value = parse_value(p);
        if (value == NULL) {
            Py_DECREF(key);
            goto fail;
        }
        int rc = PyDict_SetItem(dict, key, value);
        Py_DECREF(key);
        Py_DECREF(value);
        if (rc < 0) {
            goto fail;
        }
        skip_ws(p);
        if (p->cur < p->end && *p->cur == ',') {
            p->cur++;
            continue;
        }
        if (p->cur < p->end && *p->cur == '}') {
            p->cur++;
            return dict;
        }
        decode_error(p, p->cur, "Expecting ',' delimiter");
        goto fail;
    }

fail:
    Py_DECREF(dict);
    return NULL;
}

static PyObject *
parse_array(Parser *p)
{
    PyObject *list = PyList_New(0);
    if (list == NULL) {
        return NULL;
    }
    skip_ws(p);
    if (p->cur < p->end && *p->cur == ']') {
        p->cur++;
        return list;
    }
    for (;;) {
        PyObject *value = parse_value(p);
        if (value == NULL) {
            goto fail;
        }
        int rc = PyList_Append(list, value);
        Py_DECREF(value);
        if (rc < 0) {
            goto fail;
        }
        skip_ws(p);
        if (p->cur < p->end && *p->cur == ',') {
            p->cur++;
            continue;
        }
        if (p->cur < p->end && *p->cur == ']') {
            p->cur++;
            return list;
        }
        decode_error(p, p->cur, "Expecting ',' delimiter");
        goto fail;
    }

fail:
    Py_DECREF(list);
    return NULL;
}

static PyObject *
parse_value(Parser *p)
{
    if (++p->tokens % json_token_tape == 0 && PyErr_CheckSignals() < 0) {
        return NULL;
    }
    skip_ws(p);
    if (p->cur >= p->end) {
        return decode_error(p, p->cur, "Expecting value");
    }
    char c = *p->cur;
    switch (c) {
        case '"':
            p->cur++;
            return parse_string(p);
        case '{': {
            if (Py_EnterRecursiveCall(" while decoding a JSON document")) {
                return NULL;
            }
            p->cur++;
            PyObject *result = parse_object(p);
            Py_LeaveRecursiveCall();
            return result;
        }
        case '[': {
            if (Py_EnterRecursiveCall(" while decoding a JSON document")) {
                return NULL;
            }
            p->cur++;
            PyObject *result = parse_array(p);
            Py_LeaveRecursiveCall();
            return result;
        }
        case 't':
            if (p->end - p->cur >= 4 && memcmp(p->cur, "true", 4) == 0) {
                p->cur += 4;
                Py_RETURN_TRUE;
            }
            return decode_error(p, p->cur, "Expecting value");
        case 'f':
            if (p->end - p->cur >= 5 && memcmp(p->cur, "false", 5) == 0) {
                p->cur += 5;
                Py_RETURN_FALSE;
            }
            return decode_error(p, p->cur, "Expecting value");
        case 'n':
            if (p->end - p->cur >= 4 && memcmp(p->cur, "null", 4) == 0) {
                p->cur += 4;
                Py_RETURN_NONE;
            }
            return decode_error(p, p->cur, "Expecting value");
        /* stdlib json accepts these non-standard constants by default. */
        case 'N':
            if (p->end - p->cur >= 3 && memcmp(p->cur, "NaN", 3) == 0) {
                p->cur += 3;
                return PyFloat_FromDouble(fabs(Py_NAN));
            }
            return decode_error(p, p->cur, "Expecting value");
        case 'I':
            if (p->end - p->cur >= 8 && memcmp(p->cur, "Infinity", 8) == 0) {
                p->cur += 8;
                return PyFloat_FromDouble(Py_HUGE_VAL);
            }
            return decode_error(p, p->cur, "Expecting value");
        case '-':
            if (p->end - p->cur >= 9 && memcmp(p->cur + 1, "Infinity", 8) == 0) {
                p->cur += 9;
                return PyFloat_FromDouble(-Py_HUGE_VAL);
            }
            return parse_number(p);
        default:
            if (c >= '0' && c <= '9') {
                return parse_number(p);
            }
            return decode_error(p, p->cur, "Expecting value");
    }
}

static void
parser_clear(Parser *p)
{
    for (size_t i = 0; i < WREATH_KEY_CACHE_SIZE; i++) {
        Py_XDECREF(p->key_cache[i]);
    }
}

static PyObject *
parse_document(const char *data, Py_ssize_t len)
{
    Parser p = {0};
    p.start = data;
    p.cur = data;
    p.end = data + len;
    PyObject *value = parse_value(&p);
    if (value == NULL) {
        parser_clear(&p);
        return NULL;
    }
    skip_ws(&p);
    if (p.cur != p.end) {
        Py_DECREF(value);
        value = decode_error(&p, p.cur, "Extra data");
    }
    parser_clear(&p);
    return value;
}

/* Parse the common successful typed-body shape without first materialising the
 * top-level dict.  Field values still use the canonical JSON parser, then run
 * through their startup-compiled child plans in declaration order before the
 * dataclass is constructed.  Anything outside that exact success path returns
 * NotImplemented: the caller replays the bytes through the full decoder and
 * validator, which owns every refusal and uncommon plan shape. */
static PyObject *
parse_dataclass_document(const char *data, Py_ssize_t len, PyObject *plan,
                         PyObject *loc_seq)
{
    enum { OP_DATACLASS = 9 };
    PyObject *source = wreath_validation_plan_source(plan);
    if (!PyTuple_Check(source) || PyTuple_GET_SIZE(source) != 6) {
        return Py_NewRef(Py_NotImplemented);
    }
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(source, 0));
    if (opcode == -1 && PyErr_Occurred()) return NULL;
    if (opcode != OP_DATACLASS) return Py_NewRef(Py_NotImplemented);

    PyObject *fields = PyTuple_GET_ITEM(source, 2);
    PyObject *field_indices = PyTuple_GET_ITEM(source, 5);
    if (!PyTuple_Check(fields) || !PyDict_Check(field_indices)) {
        return Py_NewRef(Py_NotImplemented);
    }
    Py_ssize_t field_count = PyTuple_GET_SIZE(fields);
    PyObject **values = field_count == 0 ? NULL
        : PyMem_Calloc((size_t)field_count, sizeof(*values));
    if (field_count != 0 && values == NULL) {
        PyMem_Free(values);
        return PyErr_NoMemory();
    }

    Parser p = {0};
    p.start = data;
    p.cur = data;
    p.end = data + len;
    if (++p.tokens % json_token_tape == 0 && PyErr_CheckSignals() < 0) {
        PyMem_Free(values);
        return NULL;
    }
    PyObject *loc = NULL;
    PyObject *errors = NULL;
    PyObject *instance = NULL;
    int fallback = 0;
    Py_ssize_t present_count = 0;

    skip_ws(&p);
    if (p.cur >= p.end || *p.cur != '{') {
        fallback = 1;
        goto done;
    }
    p.cur++;
    skip_ws(&p);
    if (p.cur < p.end && *p.cur == '}') {
        p.cur++;
        goto parsed;
    }
    for (;;) {
        skip_ws(&p);
        if (p.cur >= p.end || *p.cur != '"') {
            fallback = 1;
            goto done;
        }
        p.cur++;
        PyObject *key = parse_string_ex(&p, 1);
        if (key == NULL) {
            if (PyErr_ExceptionMatches(PyExc_ValueError)) {
                PyErr_Clear();
                fallback = 1;
            }
            goto done;
        }
        skip_ws(&p);
        if (p.cur >= p.end || *p.cur != ':') {
            Py_DECREF(key);
            fallback = 1;
            goto done;
        }
        p.cur++;

        PyObject *field_index_value = PyDict_GetItemWithError(field_indices, key);
        Py_DECREF(key);
        if (field_index_value == NULL) {
            if (PyErr_Occurred()) goto done;
            fallback = 1;
            goto done;
        }
        Py_ssize_t field_index = PyLong_AsSsize_t(field_index_value);
        if (field_index < 0 && PyErr_Occurred()) goto done;
        if (field_index >= field_count || values[field_index] != NULL) {
            fallback = 1;
            goto done;
        }
        PyObject *value = parse_value(&p);
        if (value == NULL) {
            if (PyErr_ExceptionMatches(PyExc_ValueError)) {
                PyErr_Clear();
                fallback = 1;
            }
            goto done;
        }
        values[field_index] = value;
        present_count++;

        skip_ws(&p);
        if (p.cur < p.end && *p.cur == ',') {
            p.cur++;
            continue;
        }
        if (p.cur < p.end && *p.cur == '}') {
            p.cur++;
            break;
        }
        fallback = 1;
        goto done;
    }

parsed:
    skip_ws(&p);
    if (p.cur != p.end) {
        fallback = 1;
        goto done;
    }
    for (Py_ssize_t index = 0; index < field_count; index++) {
        PyObject *field = PyTuple_GET_ITEM(fields, index);
        if (values[index] == NULL) {
            long required = PyLong_AsLong(PyTuple_GET_ITEM(field, 3));
            if (required == -1 && PyErr_Occurred()) goto done;
            if (required) {
                fallback = 1;
                goto done;
            }
        }
    }

    loc = PySequence_List(loc_seq);
    errors = PyList_New(0);
    if (loc == NULL || errors == NULL) goto done;
    long steps = WREATH_VALIDATE_MAX_STEPS - 1; /* the dataclass node */
    for (Py_ssize_t index = 0; index < field_count; index++) {
        if (values[index] == NULL) continue;
        PyObject *field = PyTuple_GET_ITEM(fields, index);
        PyObject *wire_name = PyTuple_GET_ITEM(field, 1);
        if (PyList_Append(loc, wire_name) < 0) goto done;
        PyObject *validated = PyCapsule_CheckExact(plan)
            ? wreath_validate_plan_field(
                plan, index, values[index], loc, errors, &steps)
            : wreath_validate_node(
                PyTuple_GET_ITEM(field, 2), values[index], loc, errors, &steps);
        if (PyList_SetSlice(
                loc, PyList_GET_SIZE(loc) - 1, PyList_GET_SIZE(loc), NULL) < 0) {
            Py_XDECREF(validated);
            goto done;
        }
        if (validated == NULL) goto done;
        Py_SETREF(values[index], validated);
        if (PyList_GET_SIZE(errors) != 0 || steps < 0) {
            fallback = 1;
            goto done;
        }
    }

    PyObject *cls = PyTuple_GET_ITEM(source, 1);
    int positional = PyObject_IsTrue(PyTuple_GET_ITEM(source, 4));
    if (positional < 0) goto done;
    if (positional && present_count == field_count) {
        instance = PyObject_Vectorcall(cls, values, (size_t)field_count, NULL);
    }
    else {
        PyObject *kwargs = PyDict_New();
        if (kwargs == NULL) goto done;
        for (Py_ssize_t index = 0; index < field_count; index++) {
            if (values[index] == NULL) continue;
            PyObject *name = PyTuple_GET_ITEM(PyTuple_GET_ITEM(fields, index), 0);
            if (PyDict_SetItem(kwargs, name, values[index]) < 0) {
                Py_DECREF(kwargs);
                goto done;
            }
        }
        instance = PyObject_VectorcallDict(cls, NULL, 0, kwargs);
        Py_DECREF(kwargs);
    }

done:
    parser_clear(&p);
    for (Py_ssize_t index = 0; index < field_count; index++) {
        Py_XDECREF(values[index]);
    }
    PyMem_Free(values);
    Py_XDECREF(loc);
    if (fallback) {
        Py_XDECREF(errors);
        Py_XDECREF(instance);
        if (PyErr_Occurred()) PyErr_Clear();
        return Py_NewRef(Py_NotImplemented);
    }
    if (instance == NULL) {
        Py_XDECREF(errors);
        return NULL;
    }
    return wreath_tuple2_from_owned(instance, errors);
}

PyObject *
wreath_json_loads_validation(PyObject *arg, PyObject *plan, PyObject *loc_seq)
{
    if (!PyBytes_Check(arg) && !PyByteArray_Check(arg)) {
        return Py_NewRef(Py_NotImplemented);
    }
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    const char *data = view.buf;
    Py_ssize_t len = view.len;
    PyObject *result;
    if ((len >= 3 && memcmp(data, "\xef\xbb\xbf", 3) == 0) ||
        (len >= 2 && (data[0] == '\0' || data[1] == '\0' ||
                      (uint8_t)data[0] == 0xFF || (uint8_t)data[0] == 0xFE))) {
        result = Py_NewRef(Py_NotImplemented);
    }
    else {
        if (Py_EnterRecursiveCall(" while decoding a JSON document")) {
            result = NULL;
        }
        else {
            result = parse_dataclass_document(data, len, plan, loc_seq);
            Py_LeaveRecursiveCall();
        }
    }
    PyBuffer_Release(&view);
    return result;
}

/* Fall back to stdlib json.loads for inputs whose encoding detection we do
 * not reimplement (UTF-16/32 payloads and strs that cannot encode to UTF-8).
 */
static PyObject *
stdlib_loads(PyObject *arg)
{
    static PyObject *loads = NULL;
    if (loads == NULL) {
        PyObject *module = PyImport_ImportModule("json");
        if (module == NULL) {
            return NULL;
        }
        loads = PyObject_GetAttrString(module, "loads");
        Py_DECREF(module);
        if (loads == NULL) {
            return NULL;
        }
    }
    return PyObject_CallOneArg(loads, arg);
}

PyObject *
wreath_json_loads(PyObject *Py_UNUSED(self), PyObject *arg)
{
    if (PyUnicode_Check(arg)) {
        Py_ssize_t len;
        const char *data = PyUnicode_AsUTF8AndSize(arg, &len);
        if (data == NULL) {
            /* Lone surrogates in the source text: stdlib handles them. */
            PyErr_Clear();
            return stdlib_loads(arg);
        }
        return parse_document(data, len);
    }

    if (!PyBytes_Check(arg) && !PyByteArray_Check(arg)) {
        PyErr_Format(PyExc_TypeError,
                     "the JSON object must be str, bytes or bytearray, not %.100s",
                     Py_TYPE(arg)->tp_name);
        return NULL;
    }

    /* Pin the buffer: a bytearray cannot be resized while exported. */
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    const char *data = view.buf;
    Py_ssize_t len = view.len;
    PyObject *result;

    if (len >= 3 && memcmp(data, "\xef\xbb\xbf", 3) == 0) {
        /* UTF-8 BOM: stdlib detects utf-8-sig for bytes input. */
        data += 3;
        len -= 3;
        result = parse_document(data, len);
    }
    else if (len >= 2 && (data[0] == '\0' || data[1] == '\0' ||
                          (uint8_t)data[0] == 0xFF || (uint8_t)data[0] == 0xFE)) {
        /* Looks like UTF-16/32 (BOM or embedded NUL in the first bytes);
         * defer to stdlib's encoding detection. Valid UTF-8 JSON can never
         * start this way. */
        result = stdlib_loads(arg);
    }
    else {
        result = parse_document(data, len);
    }

    PyBuffer_Release(&view);
    return result;
}

typedef struct {
    Py_ssize_t remaining;
    PyObject *error_type;
    PyObject *fullmatch;
    PyObject *search;
    PyObject *pattern_error;
    PyObject *nothing;
} JSONPathEval;

enum {
    JP_NAME,
    JP_INDEX,
    JP_SLICE,
    JP_WILDCARD,
    JP_FILTER
};

enum {
    JP_LITERAL,
    JP_QUERY,
    JP_FUNCTION,
    JP_COMPARE,
    JP_NOT,
    JP_LOGICAL
};

enum {
    JP_LENGTH,
    JP_COUNT,
    JP_VALUE,
    JP_MATCH,
    JP_SEARCH
};

static PyObject *
jp_refusal(JSONPathEval *eval, const char *message)
{
    PyErr_SetString(eval->error_type, message);
    return NULL;
}

static int
jp_visit(JSONPathEval *eval)
{
    eval->remaining--;
    if (eval->remaining >= 0) {
        return 0;
    }
    PyErr_SetString(eval->error_type, "JSONPath evaluation exceeded its node visit limit");
    return -1;
}

static int
jp_code(JSONPathEval *eval, PyObject *operation, Py_ssize_t minimum_size, long *code)
{
    if (!PyTuple_Check(operation) || PyTuple_GET_SIZE(operation) < minimum_size) {
        jp_refusal(eval, "compiled JSONPath operation has the wrong shape");
        return -1;
    }
    *code = PyLong_AsLong(PyTuple_GET_ITEM(operation, 0));
    if (*code == -1 && PyErr_Occurred()) {
        PyErr_Clear();
        jp_refusal(eval, "compiled JSONPath operation code must be an integer");
        return -1;
    }
    return 0;
}

static PyObject *
jp_tokens_append(JSONPathEval *eval, PyObject *tokens, PyObject *token)
{
    if (!PyTuple_Check(tokens)) {
        return jp_refusal(eval, "compiled JSONPath node path is not a tuple");
    }
    Py_ssize_t size = PyTuple_GET_SIZE(tokens);
    PyObject *result = PyTuple_New(size + 1);
    if (result == NULL) {
        return NULL;
    }
    for (Py_ssize_t index = 0; index < size; index++) {
        PyTuple_SET_ITEM(result, index, Py_NewRef(PyTuple_GET_ITEM(tokens, index)));
    }
    PyTuple_SET_ITEM(result, size, Py_NewRef(token));
    return result;
}

static PyObject *
jp_node(JSONPathEval *eval, PyObject *value, PyObject *tokens, PyObject *token)
{
    PyObject *next_tokens = jp_tokens_append(eval, tokens, token);
    if (next_tokens == NULL) {
        return NULL;
    }
    PyObject *node = PyTuple_Pack(2, value, next_tokens);
    Py_DECREF(next_tokens);
    return node;
}

static int
jp_append_child(JSONPathEval *eval, PyObject *result, PyObject *node,
                PyObject *value, PyObject *token, int count_visit)
{
    if (!PyTuple_Check(node) || PyTuple_GET_SIZE(node) != 2) {
        jp_refusal(eval, "compiled JSONPath node has the wrong shape");
        return -1;
    }
    if (count_visit && jp_visit(eval) < 0) {
        return -1;
    }
    PyObject *child = jp_node(eval, value, PyTuple_GET_ITEM(node, 1), token);
    if (child == NULL) {
        return -1;
    }
    int rc = PyList_Append(result, child);
    Py_DECREF(child);
    return rc;
}

static PyObject *
jp_children(JSONPathEval *eval, PyObject *node, int count_visits)
{
    if (!PyTuple_Check(node) || PyTuple_GET_SIZE(node) != 2) {
        return jp_refusal(eval, "compiled JSONPath node has the wrong shape");
    }
    PyObject *value = PyTuple_GET_ITEM(node, 0);
    PyObject *result = PyList_New(0);
    if (result == NULL) {
        return NULL;
    }
    if (PyDict_Check(value)) {
        Py_ssize_t position = 0;
        PyObject *key;
        PyObject *child_value;
        while (PyDict_Next(value, &position, &key, &child_value)) {
            if (!PyUnicode_Check(key)) {
                Py_DECREF(result);
                return jp_refusal(eval, "JSONPath input objects must have string member names");
            }
            if (jp_append_child(eval, result, node, child_value, key, count_visits) < 0) {
                Py_DECREF(result);
                return NULL;
            }
        }
    }
    else if (PyList_Check(value)) {
        Py_ssize_t size = PyList_GET_SIZE(value);
        for (Py_ssize_t index = 0; index < size; index++) {
            PyObject *token = PyLong_FromSsize_t(index);
            if (token == NULL) {
                Py_DECREF(result);
                return NULL;
            }
            int rc = jp_append_child(eval, result, node, PyList_GET_ITEM(value, index),
                                     token, count_visits);
            Py_DECREF(token);
            if (rc < 0) {
                Py_DECREF(result);
                return NULL;
            }
        }
    }
    return result;
}

static PyObject *
jp_descendants(JSONPathEval *eval, PyObject *node)
{
    PyObject *found = PyList_New(0);
    PyObject *pending = PyList_New(1);
    if (found == NULL || pending == NULL) {
        Py_XDECREF(found);
        Py_XDECREF(pending);
        return NULL;
    }
    PyList_SET_ITEM(pending, 0, Py_NewRef(node));
    while (PyList_GET_SIZE(pending)) {
        Py_ssize_t last = PyList_GET_SIZE(pending) - 1;
        PyObject *current = Py_NewRef(PyList_GET_ITEM(pending, last));
        if (PySequence_DelItem(pending, last) < 0 || PyList_Append(found, current) < 0) {
            Py_DECREF(current);
            Py_DECREF(found);
            Py_DECREF(pending);
            return NULL;
        }
        PyObject *children = jp_children(eval, current, 1);
        Py_DECREF(current);
        if (children == NULL) {
            Py_DECREF(found);
            Py_DECREF(pending);
            return NULL;
        }
        for (Py_ssize_t index = PyList_GET_SIZE(children); index > 0; index--) {
            if (PyList_Append(pending, PyList_GET_ITEM(children, index - 1)) < 0) {
                Py_DECREF(children);
                Py_DECREF(found);
                Py_DECREF(pending);
                return NULL;
            }
        }
        Py_DECREF(children);
    }
    Py_DECREF(pending);
    return found;
}

static PyObject *jp_run(JSONPathEval *eval, PyObject *segments, PyObject *nodes,
                        PyObject *root);
static PyObject *jp_evaluate(JSONPathEval *eval, PyObject *expression,
                             PyObject *root, PyObject *current);

static int
jp_truth(JSONPathEval *eval, PyObject *value)
{
    if (value == eval->nothing) {
        return 0;
    }
    return PyObject_IsTrue(value);
}

static PyObject *
jp_query_nodes(JSONPathEval *eval, PyObject *query, PyObject *root, PyObject *current)
{
    long code;
    if (jp_code(eval, query, 4, &code) < 0 || code != JP_QUERY) {
        return jp_refusal(eval, "compiled JSONPath query has the wrong shape");
    }
    PyObject *start = PyObject_IsTrue(PyTuple_GET_ITEM(query, 1))
                          ? root
                          : current;
    if (start == NULL || PyErr_Occurred()) {
        return NULL;
    }
    PyObject *nodes = PyList_New(1);
    if (nodes == NULL) {
        return NULL;
    }
    PyList_SET_ITEM(nodes, 0, Py_NewRef(start));
    PyObject *result = jp_run(eval, PyTuple_GET_ITEM(query, 2), nodes, root);
    Py_DECREF(nodes);
    return result;
}

static PyObject *
jp_operand(JSONPathEval *eval, PyObject *expression, PyObject *root, PyObject *current)
{
    long code;
    if (jp_code(eval, expression, 2, &code) < 0) {
        return NULL;
    }
    if (code == JP_LITERAL) {
        return Py_NewRef(PyTuple_GET_ITEM(expression, 1));
    }
    if (code == JP_QUERY) {
        PyObject *nodes = jp_query_nodes(eval, expression, root, current);
        if (nodes == NULL) {
            return NULL;
        }
        int singular = PyObject_IsTrue(PyTuple_GET_ITEM(expression, 3));
        if (singular < 0) {
            Py_DECREF(nodes);
            return NULL;
        }
        if (!singular) {
            return nodes;
        }
        PyObject *result = PyList_GET_SIZE(nodes) == 1
                               ? Py_NewRef(PyTuple_GET_ITEM(PyList_GET_ITEM(nodes, 0), 0))
                               : Py_NewRef(eval->nothing);
        Py_DECREF(nodes);
        return result;
    }
    if (code != JP_FUNCTION || PyTuple_GET_SIZE(expression) != 3) {
        return jp_evaluate(eval, expression, root, current);
    }
    long function = PyLong_AsLong(PyTuple_GET_ITEM(expression, 1));
    PyObject *arguments = PyTuple_GET_ITEM(expression, 2);
    if ((function == -1 && PyErr_Occurred()) || !PyTuple_Check(arguments)) {
        PyErr_Clear();
        return jp_refusal(eval, "compiled JSONPath function has the wrong shape");
    }
    if (function == JP_COUNT || function == JP_VALUE) {
        if (PyTuple_GET_SIZE(arguments) != 1) {
            return jp_refusal(eval, "compiled JSONPath function has the wrong arity");
        }
        PyObject *nodes = jp_query_nodes(
            eval, PyTuple_GET_ITEM(arguments, 0), root, current);
        if (nodes == NULL) {
            return NULL;
        }
        if (function == JP_COUNT) {
            PyObject *result = PyLong_FromSsize_t(PyList_GET_SIZE(nodes));
            Py_DECREF(nodes);
            return result;
        }
        PyObject *result = PyList_GET_SIZE(nodes) == 1
                               ? Py_NewRef(PyTuple_GET_ITEM(PyList_GET_ITEM(nodes, 0), 0))
                               : Py_NewRef(eval->nothing);
        Py_DECREF(nodes);
        return result;
    }
    if (function == JP_LENGTH) {
        if (PyTuple_GET_SIZE(arguments) != 1) {
            return jp_refusal(eval, "compiled JSONPath length function has the wrong arity");
        }
        PyObject *value = jp_operand(eval, PyTuple_GET_ITEM(arguments, 0), root, current);
        if (value == NULL) {
            return NULL;
        }
        if (!PyUnicode_Check(value) && !PyList_Check(value) && !PyDict_Check(value)) {
            Py_DECREF(value);
            return Py_NewRef(eval->nothing);
        }
        Py_ssize_t size = PyObject_Length(value);
        Py_DECREF(value);
        return size < 0 ? NULL : PyLong_FromSsize_t(size);
    }
    if ((function == JP_MATCH || function == JP_SEARCH) &&
        PyTuple_GET_SIZE(arguments) == 2) {
        PyObject *value = jp_operand(eval, PyTuple_GET_ITEM(arguments, 0), root, current);
        PyObject *pattern = value == NULL
                                ? NULL
                                : jp_operand(eval, PyTuple_GET_ITEM(arguments, 1), root, current);
        if (value == NULL || pattern == NULL) {
            Py_XDECREF(value);
            Py_XDECREF(pattern);
            return NULL;
        }
        if (!PyUnicode_Check(value) || !PyUnicode_Check(pattern)) {
            Py_DECREF(value);
            Py_DECREF(pattern);
            return Py_NewRef(Py_False);
        }
        PyObject *callable = function == JP_MATCH ? eval->fullmatch : eval->search;
        PyObject *matched = PyObject_CallFunctionObjArgs(callable, pattern, value, NULL);
        Py_DECREF(value);
        Py_DECREF(pattern);
        if (matched == NULL) {
            int invalid_pattern = PyErr_ExceptionMatches(eval->pattern_error);
            if (!invalid_pattern) {
                return NULL;
            }
            PyErr_Clear();
            return Py_NewRef(Py_False);
        }
        PyObject *result = Py_NewRef(matched == Py_None ? Py_False : Py_True);
        Py_DECREF(matched);
        return result;
    }
    return jp_refusal(eval, "compiled JSONPath function is not supported");
}

static int
jp_number(PyObject *value)
{
    return value != Py_True && value != Py_False &&
           (PyLong_Check(value) || PyFloat_Check(value));
}

static int
jp_equal(JSONPathEval *eval, PyObject *left, PyObject *right)
{
    if (left == eval->nothing || right == eval->nothing) {
        return left == right;
    }
    if (jp_number(left)) {
        return jp_number(right) ? PyObject_RichCompareBool(left, right, Py_EQ) : 0;
    }
    if (Py_TYPE(left) != Py_TYPE(right)) {
        return 0;
    }
    return PyObject_RichCompareBool(left, right, Py_EQ);
}

static int
jp_less(PyObject *left, PyObject *right)
{
    if ((PyUnicode_Check(left) && PyUnicode_Check(right)) ||
        (jp_number(left) && jp_number(right))) {
        return PyObject_RichCompareBool(left, right, Py_LT);
    }
    return 0;
}

static PyObject *
jp_evaluate(JSONPathEval *eval, PyObject *expression, PyObject *root, PyObject *current)
{
    long code;
    if (jp_code(eval, expression, 2, &code) < 0) {
        return NULL;
    }
    if (code == JP_LOGICAL) {
        if (PyTuple_GET_SIZE(expression) != 4) {
            return jp_refusal(eval, "compiled JSONPath logical expression has the wrong shape");
        }
        PyObject *left = jp_evaluate(eval, PyTuple_GET_ITEM(expression, 2), root, current);
        if (left == NULL) {
            return NULL;
        }
        int left_truth = jp_truth(eval, left);
        Py_DECREF(left);
        if (left_truth < 0) {
            return NULL;
        }
        int conjunction = PyObject_IsTrue(PyTuple_GET_ITEM(expression, 1));
        if (conjunction < 0) {
            return NULL;
        }
        if ((conjunction && !left_truth) || (!conjunction && left_truth)) {
            return PyBool_FromLong(left_truth);
        }
        PyObject *right = jp_evaluate(eval, PyTuple_GET_ITEM(expression, 3), root, current);
        if (right == NULL) {
            return NULL;
        }
        int right_truth = jp_truth(eval, right);
        Py_DECREF(right);
        return right_truth < 0 ? NULL : PyBool_FromLong(right_truth);
    }
    if (code == JP_NOT) {
        PyObject *value = jp_evaluate(eval, PyTuple_GET_ITEM(expression, 1), root, current);
        if (value == NULL) {
            return NULL;
        }
        int truth = jp_truth(eval, value);
        Py_DECREF(value);
        return truth < 0 ? NULL : PyBool_FromLong(!truth);
    }
    if (code == JP_COMPARE) {
        if (PyTuple_GET_SIZE(expression) != 4) {
            return jp_refusal(eval, "compiled JSONPath comparison has the wrong shape");
        }
        long comparison = PyLong_AsLong(PyTuple_GET_ITEM(expression, 1));
        if (comparison == -1 && PyErr_Occurred()) {
            return NULL;
        }
        PyObject *left = jp_operand(eval, PyTuple_GET_ITEM(expression, 2), root, current);
        PyObject *right = left == NULL
                              ? NULL
                              : jp_operand(eval, PyTuple_GET_ITEM(expression, 3), root, current);
        if (left == NULL || right == NULL) {
            Py_XDECREF(left);
            Py_XDECREF(right);
            return NULL;
        }
        int equal = jp_equal(eval, left, right);
        if (equal < 0) {
            Py_DECREF(left);
            Py_DECREF(right);
            return NULL;
        }
        int less = 0;
        int reverse_less = 0;
        if (comparison >= 2) {
            less = jp_less(left, right);
            reverse_less = less < 0 ? -1 : jp_less(right, left);
        }
        Py_DECREF(left);
        Py_DECREF(right);
        if (less < 0 || reverse_less < 0) {
            return NULL;
        }
        int result;
        switch (comparison) {
            case 0: result = equal; break;
            case 1: result = !equal; break;
            case 2: result = less || equal; break;
            case 3: result = reverse_less || equal; break;
            case 4: result = less; break;
            case 5: result = reverse_less; break;
            default:
                return jp_refusal(eval, "compiled JSONPath comparison is not supported");
        }
        return PyBool_FromLong(result);
    }
    if (code == JP_QUERY) {
        PyObject *nodes = jp_query_nodes(eval, expression, root, current);
        if (nodes == NULL) {
            return NULL;
        }
        PyObject *result = PyBool_FromLong(PyList_GET_SIZE(nodes) != 0);
        Py_DECREF(nodes);
        return result;
    }
    PyObject *value = jp_operand(eval, expression, root, current);
    if (value == NULL) {
        return NULL;
    }
    int truth = jp_truth(eval, value);
    Py_DECREF(value);
    return truth < 0 ? NULL : PyBool_FromLong(truth);
}

static int
jp_select(JSONPathEval *eval, PyObject *selector, PyObject *node,
          PyObject *root, PyObject *selected)
{
    long code;
    if (jp_code(eval, selector, 1, &code) < 0 ||
        !PyTuple_Check(node) || PyTuple_GET_SIZE(node) != 2) {
        return -1;
    }
    PyObject *value = PyTuple_GET_ITEM(node, 0);
    if (code == JP_NAME) {
        if (PyTuple_GET_SIZE(selector) != 2 ||
            !PyUnicode_Check(PyTuple_GET_ITEM(selector, 1))) {
            jp_refusal(eval, "compiled JSONPath name selector has the wrong shape");
            return -1;
        }
        if (!PyDict_Check(value)) {
            return 0;
        }
        PyObject *key = PyTuple_GET_ITEM(selector, 1);
        PyObject *member = PyDict_GetItemWithError(value, key);
        if (member == NULL) {
            return PyErr_Occurred() ? -1 : 0;
        }
        return jp_append_child(eval, selected, node, member, key, 1);
    }
    if (code == JP_INDEX) {
        if (PyTuple_GET_SIZE(selector) != 2 || !PyList_Check(value)) {
            return 0;
        }
        long long raw = PyLong_AsLongLong(PyTuple_GET_ITEM(selector, 1));
        if (raw == -1 && PyErr_Occurred()) {
            PyErr_Clear();
            return 0;
        }
        Py_ssize_t size = PyList_GET_SIZE(value);
        long long index = raw < 0 ? (long long)size + raw : raw;
        if (index < 0 || index >= size) {
            return 0;
        }
        PyObject *token = PyLong_FromLongLong(index);
        if (token == NULL) {
            return -1;
        }
        int rc = jp_append_child(eval, selected, node, PyList_GET_ITEM(value, (Py_ssize_t)index),
                                 token, 1);
        Py_DECREF(token);
        return rc;
    }
    if (code == JP_SLICE) {
        if (PyTuple_GET_SIZE(selector) != 4 || !PyList_Check(value)) {
            return 0;
        }
        PyObject *step_object = PyTuple_GET_ITEM(selector, 3);
        if (step_object != Py_None) {
            long long step_value = PyLong_AsLongLong(step_object);
            if (step_value == -1 && PyErr_Occurred()) {
                return -1;
            }
            if (step_value == 0) {
                return 0;
            }
        }
        PyObject *slice = PySlice_New(PyTuple_GET_ITEM(selector, 1),
                                      PyTuple_GET_ITEM(selector, 2), step_object);
        if (slice == NULL) {
            return -1;
        }
        Py_ssize_t start;
        Py_ssize_t stop;
        Py_ssize_t step;
        Py_ssize_t length;
        int rc = PySlice_GetIndicesEx(slice, PyList_GET_SIZE(value),
                                     &start, &stop, &step, &length);
        Py_DECREF(slice);
        if (rc < 0) {
            return -1;
        }
        Py_ssize_t index = start;
        for (Py_ssize_t count = 0; count < length; count++, index += step) {
            PyObject *token = PyLong_FromSsize_t(index);
            if (token == NULL) {
                return -1;
            }
            rc = jp_append_child(eval, selected, node, PyList_GET_ITEM(value, index),
                                 token, 1);
            Py_DECREF(token);
            if (rc < 0) {
                return -1;
            }
        }
        return 0;
    }
    if (code == JP_WILDCARD) {
        PyObject *children = jp_children(eval, node, 1);
        if (children == NULL) {
            return -1;
        }
        Py_ssize_t size = PyList_GET_SIZE(children);
        for (Py_ssize_t index = 0; index < size; index++) {
            if (PyList_Append(selected, PyList_GET_ITEM(children, index)) < 0) {
                Py_DECREF(children);
                return -1;
            }
        }
        Py_DECREF(children);
        return 0;
    }
    if (code == JP_FILTER && PyTuple_GET_SIZE(selector) == 2) {
        PyObject *children = jp_children(eval, node, 1);
        if (children == NULL) {
            return -1;
        }
        Py_ssize_t size = PyList_GET_SIZE(children);
        for (Py_ssize_t index = 0; index < size; index++) {
            PyObject *child = PyList_GET_ITEM(children, index);
            PyObject *answer = jp_evaluate(
                eval, PyTuple_GET_ITEM(selector, 1), root, child);
            if (answer == NULL) {
                Py_DECREF(children);
                return -1;
            }
            int truth = jp_truth(eval, answer);
            Py_DECREF(answer);
            if (truth < 0 || (truth && PyList_Append(selected, child) < 0)) {
                Py_DECREF(children);
                return -1;
            }
        }
        Py_DECREF(children);
        return 0;
    }
    jp_refusal(eval, "compiled JSONPath selector is not supported");
    return -1;
}

static PyObject *
jp_run(JSONPathEval *eval, PyObject *segments, PyObject *nodes, PyObject *root)
{
    if (!PyTuple_Check(segments) || !PyList_Check(nodes)) {
        return jp_refusal(eval, "compiled JSONPath program has the wrong shape");
    }
    PyObject *current = Py_NewRef(nodes);
    Py_ssize_t segment_count = PyTuple_GET_SIZE(segments);
    for (Py_ssize_t segment_index = 0; segment_index < segment_count; segment_index++) {
        PyObject *segment = PyTuple_GET_ITEM(segments, segment_index);
        if (!PyTuple_Check(segment) || PyTuple_GET_SIZE(segment) != 2 ||
            !PyTuple_Check(PyTuple_GET_ITEM(segment, 1))) {
            Py_DECREF(current);
            return jp_refusal(eval, "compiled JSONPath segment has the wrong shape");
        }
        int descendant = PyObject_IsTrue(PyTuple_GET_ITEM(segment, 0));
        if (descendant < 0) {
            Py_DECREF(current);
            return NULL;
        }
        PyObject *sources;
        if (!descendant) {
            sources = Py_NewRef(current);
        }
        else {
            sources = PyList_New(0);
            if (sources == NULL) {
                Py_DECREF(current);
                return NULL;
            }
            Py_ssize_t current_size = PyList_GET_SIZE(current);
            for (Py_ssize_t index = 0; index < current_size; index++) {
                PyObject *found = jp_descendants(eval, PyList_GET_ITEM(current, index));
                if (found == NULL) {
                    Py_DECREF(sources);
                    Py_DECREF(current);
                    return NULL;
                }
                for (Py_ssize_t item = 0; item < PyList_GET_SIZE(found); item++) {
                    if (PyList_Append(sources, PyList_GET_ITEM(found, item)) < 0) {
                        Py_DECREF(found);
                        Py_DECREF(sources);
                        Py_DECREF(current);
                        return NULL;
                    }
                }
                Py_DECREF(found);
            }
        }
        PyObject *selected = PyList_New(0);
        if (selected == NULL) {
            Py_DECREF(sources);
            Py_DECREF(current);
            return NULL;
        }
        PyObject *selectors = PyTuple_GET_ITEM(segment, 1);
        for (Py_ssize_t source_index = 0; source_index < PyList_GET_SIZE(sources);
             source_index++) {
            for (Py_ssize_t selector_index = 0;
                 selector_index < PyTuple_GET_SIZE(selectors); selector_index++) {
                if (jp_select(eval, PyTuple_GET_ITEM(selectors, selector_index),
                              PyList_GET_ITEM(sources, source_index), root, selected) < 0) {
                    Py_DECREF(selected);
                    Py_DECREF(sources);
                    Py_DECREF(current);
                    return NULL;
                }
            }
        }
        Py_DECREF(sources);
        Py_DECREF(current);
        current = selected;
    }
    return current;
}

PyObject *
wreath_jsonpath_find(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *program;
    PyObject *value;
    Py_ssize_t max_visits;
    JSONPathEval eval;
    if (!PyArg_ParseTuple(args, "OOnOOOO:jsonpath_find", &program, &value,
                          &max_visits, &eval.error_type, &eval.fullmatch,
                          &eval.search, &eval.pattern_error)) {
        return NULL;
    }
    if (max_visits <= 0 || !PyExceptionClass_Check(eval.error_type) ||
        !PyCallable_Check(eval.fullmatch) || !PyCallable_Check(eval.search) ||
        !PyExceptionClass_Check(eval.pattern_error)) {
        PyErr_SetString(PyExc_TypeError, "jsonpath_find received an invalid evaluator argument");
        return NULL;
    }
    eval.remaining = max_visits;
    eval.nothing = PyObject_CallNoArgs((PyObject *)&PyBaseObject_Type);
    if (eval.nothing == NULL) {
        return NULL;
    }
    PyObject *tokens = PyTuple_New(0);
    PyObject *root = tokens == NULL ? NULL : PyTuple_Pack(2, value, tokens);
    Py_XDECREF(tokens);
    PyObject *nodes = root == NULL ? NULL : PyList_New(1);
    if (nodes != NULL) {
        PyList_SET_ITEM(nodes, 0, Py_NewRef(root));
    }
    PyObject *result = nodes == NULL ? NULL : jp_run(&eval, program, nodes, root);
    Py_XDECREF(nodes);
    Py_XDECREF(root);
    Py_DECREF(eval.nothing);
    return result;
}
