/* Byte-level web codecs: percent decoding, query strings, cookies. */
#include "wreathcore.h"

#include "simd.h"

static inline int
hexval(uint8_t c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/* Decode src into dst (dst must hold at least src_len bytes); returns the
 * decoded length. Invalid %XX sequences are copied through literally, which
 * matches urllib.parse.unquote_to_bytes. */
static Py_ssize_t
decode_into(uint8_t *dst, const uint8_t *src, Py_ssize_t src_len, int plus_as_space)
{
    Py_ssize_t out = 0;
    for (Py_ssize_t i = 0; i < src_len; i++) {
        uint8_t c = src[i];
        if (c == '%' && i + 2 < src_len) {
            int hi = hexval(src[i + 1]);
            int lo = hexval(src[i + 2]);
            if (hi >= 0 && lo >= 0) {
                dst[out++] = (uint8_t)((hi << 4) | lo);
                i += 2;
                continue;
            }
        }
        if (plus_as_space && c == '+') {
            c = ' ';
        }
        dst[out++] = c;
    }
    return out;
}

PyObject *
wreath_percent_decode(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"data", "plus_as_space", NULL};
    Py_buffer data;
    int plus_as_space = 0;
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "y*|p:percent_decode", keywords, &data, &plus_as_space)) {
        return NULL;
    }

    PyObject *result = PyBytes_FromStringAndSize(NULL, data.len);
    if (result == NULL) {
        PyBuffer_Release(&data);
        return NULL;
    }
    Py_ssize_t out = decode_into(
        (uint8_t *)PyBytes_AS_STRING(result), data.buf, data.len, plus_as_space);
    PyBuffer_Release(&data);
    if (_PyBytes_Resize(&result, out) < 0) {
        return NULL;
    }
    return result;
}

/* Decode one percent-encoded component to str (UTF-8, replacement chars). */
static PyObject *
component_to_str(const uint8_t *src, Py_ssize_t len)
{
    if (len == 0) {
        return PyUnicode_New(0, 127);
    }
    /* Nothing to decode is the common case for a query value. Two vectorised
     * `memchr`s establish that and hand the source straight to the decoder,
     * skipping the scratch allocation and the byte loop entirely. */
    if (memchr(src, '%', (size_t)len) == NULL && memchr(src, '+', (size_t)len) == NULL) {
        return PyUnicode_DecodeUTF8((const char *)src, len, "replace");
    }
    uint8_t *scratch = PyMem_Malloc((size_t)len);
    if (scratch == NULL) {
        return PyErr_NoMemory();
    }
    Py_ssize_t out = decode_into(scratch, src, len, 1);
    PyObject *text = PyUnicode_DecodeUTF8((const char *)scratch, out, "replace");
    PyMem_Free(scratch);
    return text;
}

PyObject *
wreath_parse_qs(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer query;
    Py_ssize_t max_fields = 0;
    if (!PyArg_ParseTuple(args, "y*|n:parse_qs", &query, &max_fields)) {
        return NULL;
    }

    PyObject *pairs = PyList_New(0);
    if (pairs == NULL) {
        PyBuffer_Release(&query);
        return NULL;
    }

    const uint8_t *data = query.buf;
    Py_ssize_t len = query.len;
    Py_ssize_t start = 0;
    for (Py_ssize_t i = 0; i <= len; i++) {
        if (i < len && data[i] != '&') {
            continue;
        }
        Py_ssize_t field_len = i - start;
        if (field_len > 0) {
            if (max_fields > 0 && PyList_GET_SIZE(pairs) >= max_fields) {
                PyErr_Format(PyExc_ValueError,
                             "urlencoded data exceeds %zd fields", max_fields);
                Py_DECREF(pairs);
                PyBuffer_Release(&query);
                return NULL;
            }
            const uint8_t *field = data + start;
            Py_ssize_t eq = 0;
            while (eq < field_len && field[eq] != '=') {
                eq++;
            }
            PyObject *key = component_to_str(field, eq);
            PyObject *value = (eq < field_len)
                                  ? component_to_str(field + eq + 1, field_len - eq - 1)
                                  : PyUnicode_New(0, 127);
            PyObject *pair = (key && value) ? PyTuple_Pack(2, key, value) : NULL;
            Py_XDECREF(key);
            Py_XDECREF(value);
            if (pair == NULL || PyList_Append(pairs, pair) < 0) {
                Py_XDECREF(pair);
                Py_DECREF(pairs);
                PyBuffer_Release(&query);
                return NULL;
            }
            Py_DECREF(pair);
        }
        start = i + 1;
    }
    PyBuffer_Release(&query);
    return pairs;
}

static inline int
urlencode_safe(uint8_t byte)
{
    return (byte >= 'a' && byte <= 'z') ||
           (byte >= 'A' && byte <= 'Z') ||
           (byte >= '0' && byte <= '9') ||
           byte == '_' || byte == '.' || byte == '-' || byte == '~';
}

static int
write_urlencoded(WreathBytesWriter *writer, PyObject *text)
{
    Py_ssize_t length;
    const uint8_t *data = (const uint8_t *)PyUnicode_AsUTF8AndSize(text, &length);
    static const char hex[] = "0123456789ABCDEF";
    if (data == NULL) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        uint8_t byte = data[index];
        if (urlencode_safe(byte)) {
            if (wreath_writer_byte(writer, (char)byte) < 0) return -1;
        }
        else if (byte == ' ') {
            if (wreath_writer_byte(writer, '+') < 0) return -1;
        }
        else {
            char escaped[3] = {'%', hex[byte >> 4], hex[byte & 15]};
            if (wreath_writer_write(writer, escaped, 3) < 0) return -1;
        }
    }
    return 0;
}

PyObject *
wreath_cache_key_selected(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *method, *path, *declared;
    Py_buffer query;
    if (!PyArg_ParseTuple(
            args, "UUy*O!:cache_key_selected", &method, &path, &query,
            &PyTuple_Type, &declared)) return NULL;

    PyObject *values = PyDict_New();
    if (values == NULL) {
        PyBuffer_Release(&query);
        return NULL;
    }
    const uint8_t *data = query.buf;
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index <= query.len; index++) {
        if (index < query.len && data[index] != '&') continue;
        Py_ssize_t field_length = index - start;
        if (field_length != 0) {
            const uint8_t *field = data + start;
            Py_ssize_t equals = 0;
            while (equals < field_length && field[equals] != '=') equals++;
            PyObject *key = component_to_str(field, equals);
            PyObject *value = equals < field_length
                ? component_to_str(field + equals + 1, field_length - equals - 1)
                : PyUnicode_New(0, 127);
            int failed = key == NULL || value == NULL;
            if (!failed) {
                failed = PyDict_SetDefaultRef(values, key, value, NULL) < 0;
            }
            Py_XDECREF(key);
            Py_XDECREF(value);
            if (failed) {
                Py_DECREF(values);
                PyBuffer_Release(&query);
                return NULL;
            }
        }
        start = index + 1;
    }
    PyBuffer_Release(&query);

    WreathBytesWriter writer = {0};
    Py_ssize_t method_length, path_length;
    const char *method_data = PyUnicode_AsUTF8AndSize(method, &method_length);
    const char *path_data = method_data != NULL
        ? PyUnicode_AsUTF8AndSize(path, &path_length) : NULL;
    if (path_data == NULL ||
        wreath_writer_init(&writer, method_length + path_length + 64) < 0 ||
        wreath_writer_write(&writer, method_data, method_length) < 0 ||
        wreath_writer_byte(&writer, ' ') < 0 ||
        wreath_writer_write(&writer, path_data, path_length) < 0) {
        Py_XDECREF(writer.bytes);
        Py_DECREF(values);
        return NULL;
    }

    int first = 1;
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(declared); index++) {
        PyObject *name = PyTuple_GET_ITEM(declared, index);
        PyObject *value = PyDict_GetItemWithError(values, name);
        if (value == NULL) {
            if (PyErr_Occurred()) goto error;
            continue;
        }
        if (wreath_writer_byte(&writer, first ? '?' : '&') < 0 ||
            write_urlencoded(&writer, name) < 0 ||
            wreath_writer_byte(&writer, '=') < 0 ||
            write_urlencoded(&writer, value) < 0) goto error;
        first = 0;
    }
    Py_DECREF(values);
    PyObject *encoded = wreath_writer_finish(&writer);
    if (encoded == NULL) return NULL;
    PyObject *result = PyUnicode_DecodeUTF8(
        PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded), "strict");
    Py_DECREF(encoded);
    return result;

error:
    Py_DECREF(values);
    Py_XDECREF(writer.bytes);
    return NULL;
}

PyObject *
wreath_parse_cookies(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer header;
    if (!PyArg_ParseTuple(args, "y*:parse_cookies", &header)) {
        return NULL;
    }

    PyObject *cookies = PyDict_New();
    if (cookies == NULL) {
        PyBuffer_Release(&header);
        return NULL;
    }

    const uint8_t *data = header.buf;
    Py_ssize_t len = header.len;
    Py_ssize_t start = 0;
    while (start <= len) {
        /* Both delimiter searches go through `memchr`: a cookie header is one
         * of the longest a browser sends, and the C library's scan is
         * vectorised where a byte-at-a-time loop is not. */
        const uint8_t *sep = start < len
            ? memchr(data + start, ';', (size_t)(len - start)) : NULL;
        Py_ssize_t i = sep == NULL ? len : (Py_ssize_t)(sep - data);
        Py_ssize_t lo = start;
        Py_ssize_t hi = i;
        start = i + 1;
        while (lo < hi && (data[lo] == ' ' || data[lo] == '\t')) {
            lo++;
        }
        while (hi > lo && (data[hi - 1] == ' ' || data[hi - 1] == '\t')) {
            hi--;
        }
        if (lo >= hi) {
            continue;
        }
        const uint8_t *split = memchr(data + lo, '=', (size_t)(hi - lo));
        if (split == NULL) {
            continue; /* no '=': ignore the fragment */
        }
        Py_ssize_t eq = (Py_ssize_t)(split - data);
        /* Trim the inner edges too, not just the ones facing the ';'. RFC
         * 6265bis 5.8.3 strips WSP from both halves of a cookie-pair; trimming
         * only the fragment left the space glued to the name, so `" a = 1 "`
         * yielded `"a "` and a lookup of `"a"` found nothing. The outer trim
         * above already took the name's leading and the value's trailing run,
         * so only the two edges facing the '=' are left. */
        Py_ssize_t name_hi = eq;
        while (name_hi > lo && (data[name_hi - 1] == ' ' || data[name_hi - 1] == '\t')) {
            name_hi--;
        }
        if (name_hi == lo) {
            continue; /* no name */
        }
        Py_ssize_t value_lo = eq + 1;
        while (value_lo < hi && (data[value_lo] == ' ' || data[value_lo] == '\t')) {
            value_lo++;
        }
        PyObject *name =
            PyUnicode_DecodeLatin1((const char *)data + lo, name_hi - lo, NULL);
        PyObject *value =
            PyUnicode_DecodeLatin1((const char *)data + value_lo, hi - value_lo, NULL);
        int failed = (name == NULL || value == NULL);
        /* First value wins for duplicate cookie names. `SetDefaultRef` says
         * exactly that in one hash and one probe, where `Contains` followed by
         * `SetItem` paid for both twice. */
        if (!failed) {
            failed = PyDict_SetDefaultRef(cookies, name, value, NULL) < 0;
        }
        Py_XDECREF(name);
        Py_XDECREF(value);
        if (failed) {
            Py_DECREF(cookies);
            PyBuffer_Release(&header);
            return NULL;
        }
    }
    PyBuffer_Release(&header);
    return cookies;
}

/* b64encode(data, urlsafe=False, pad=True) -> str
 *
 * `base64.b64encode` runs a scalar table loop at roughly 0.5 bytes/ns and hands
 * back `bytes` that every caller here immediately decodes to `str`. This
 * encodes at the widest width the CPU has and builds the ASCII string
 * directly, so the intermediate object never exists.
 */
PyObject *
wreath_b64encode(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"data", "urlsafe", "pad", NULL};
    Py_buffer data;
    int urlsafe = 0;
    int pad = 1;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "y*|pp:b64encode", keywords,
                                     &data, &urlsafe, &pad)) {
        return NULL;
    }
    Py_ssize_t room = ((data.len + 2) / 3) * 4;
    PyObject *result = PyUnicode_New(room, 127);
    if (result == NULL) {
        PyBuffer_Release(&data);
        return NULL;
    }
    ptrdiff_t written = wreath_b64_encode(
        (const unsigned char *)data.buf, (ptrdiff_t)data.len,
        (char *)PyUnicode_1BYTE_DATA(result), urlsafe, pad);
    PyBuffer_Release(&data);
    if ((Py_ssize_t)written == room) {
        return result;
    }
    /* Unpadded output is shorter than the padded bound; PyUnicode has no
     * resize for a finished object, so the exact-length copy is made here. */
    PyObject *exact = PyUnicode_FromKindAndData(
        PyUnicode_1BYTE_KIND, PyUnicode_1BYTE_DATA(result), (Py_ssize_t)written);
    Py_DECREF(result);
    return exact;
}
