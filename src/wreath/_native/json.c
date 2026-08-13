/* Compact JSON encoder and decoder over UTF-8 byte buffers.
 *
 * Encoder differences from stdlib json.dumps (documented in
 * docs/native/json.md): bytes output, str-only dict keys, and NaN/Infinity
 * rejected. The decoder mirrors stdlib json.loads semantics: NaN/Infinity/
 * -Infinity accepted, lone surrogate escapes allowed, last duplicate key
 * wins, ints of any size. Inputs that stdlib would sniff as UTF-16/32 are
 * delegated to stdlib json so encoding detection stays byte-for-byte
 * compatible.
 */
#include "wreathcore.h"
#include "bytes_writer.h"

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
    char *text = PyOS_double_to_string(value, 'r', 0, Py_DTSF_ADD_DOT_0, NULL);
    if (text == NULL) {
        return -1;
    }
    int rc = wreath_writer_write(w, text, (Py_ssize_t)strlen(text));
    PyMem_Free(text);
    return rc;
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
