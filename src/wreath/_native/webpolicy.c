/* No `simd.h` here, and that is a decision rather than an oversight.
 *
 * Every scalar loop below walks either one header token or one header list.
 * `wreath_ascii_equal_ci_str` compares against a literal like "gzip" or
 * "no-store" and returns on the first length mismatch; `trim_ows` walks the whitespace at
 * each end, which is nearly always zero or one byte; `parse_quality` reads at
 * most five characters and refuses anything longer. The list walks are over
 * tens of headers, not thousands.
 *
 * So the loop is priced the way AGENTS.md asks: the body is already minimal
 * and the length is single-digit. An arm needs 16 or 32 bytes before its first
 * load pays for the setup, so every one of these would spend more reaching the
 * vector path than it spends finishing scalar. Reach for `simd.h` here only if
 * a caller starts handing this file a buffer rather than a token.
 */

#include "wreathcore.h"

#include <string.h>

#define WREATH_NO_TRANSFORM 1
#define WREATH_NO_STORE 2
#define WREATH_PRIVATE 4
#define WREATH_PUBLIC 8

static void
trim_ows(const char **data, Py_ssize_t *length)
{
    while (*length > 0 && ((*data)[0] == ' ' || (*data)[0] == '\t')) {
        (*data)++;
        (*length)--;
    }
    while (*length > 0 && ((*data)[*length - 1] == ' ' || (*data)[*length - 1] == '\t')) {
        (*length)--;
    }
}

static int
parse_quality(const char *data, Py_ssize_t length)
{
    /* Return thousandths in [0,1000], malformed values are unacceptable. */
    trim_ows(&data, &length);
    if (length == 1 && data[0] == '0') return 0;
    if (length == 1 && data[0] == '1') return 1000;
    if (length < 3 || length > 5 || data[1] != '.' || (data[0] != '0' && data[0] != '1'))
        return 0;
    int value = data[0] == '1' ? 1000 : 0;
    int scale = 100;
    for (Py_ssize_t i = 2; i < length; i++, scale /= 10) {
        if (data[i] < '0' || data[i] > '9') return 0;
        if (data[0] == '1' && data[i] != '0') return 0;
        if (data[0] == '0') value += (data[i] - '0') * scale;
    }
    return value;
}

PyObject *
wreath_select_content_encoding(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    const char *data = (const char *)view.buf;
    Py_ssize_t start = 0;
    /* Qualities are thousandths, and 0 already means "not acceptable", so it
     * doubles as the not-offered sentinel. Only gzip tracks whether it was
     * *named*, because that is what decides whether the wildcard applies to it.
     * Mirrors _pure/webpolicy.py::select_content_encoding exactly; the two are
     * held equal by tests/test_webpolicy_parity.py. */
    int gzip_named = 0, gzip_q = 0, zstd_q = 0, wildcard_q = 0;
    for (Py_ssize_t i = 0; i <= view.len; i++) {
        if (i < view.len && data[i] != ',') continue;
        const char *item = data + start;
        Py_ssize_t item_len = i - start;
        start = i + 1;
        trim_ows(&item, &item_len);
        if (item_len == 0) continue;
        Py_ssize_t semi = 0;
        while (semi < item_len && item[semi] != ';') semi++;
        const char *coding = item;
        Py_ssize_t coding_len = semi;
        trim_ows(&coding, &coding_len);
        int quality = 1000;
        Py_ssize_t parameter = semi;
        while (parameter < item_len) {
            parameter++;
            Py_ssize_t end = parameter;
            while (end < item_len && item[end] != ';') end++;
            const char *part = item + parameter;
            Py_ssize_t part_len = end - parameter;
            trim_ows(&part, &part_len);
            Py_ssize_t equals = 0;
            while (equals < part_len && part[equals] != '=') equals++;
            const char *name = part;
            Py_ssize_t name_len = equals;
            trim_ows(&name, &name_len);
            if (wreath_ascii_equal_ci_str(name, name_len, "q")) {
                quality = equals < part_len
                    ? parse_quality(part + equals + 1, part_len - equals - 1)
                    : 0;
            }
            parameter = end;
        }
        if (wreath_ascii_equal_ci_str(coding, coding_len, "gzip")) {
            gzip_named = 1;
            gzip_q = quality;
        } else if (wreath_ascii_equal_ci_str(coding, coding_len, "zstd")) {
            zstd_q = quality;
        } else if (coding_len == 1 && coding[0] == '*') {
            wildcard_q = quality;
        }
    }
    PyBuffer_Release(&view);
    if (!gzip_named) gzip_q = wildcard_q;
    /* Ties go to zstd; a client that prefers gzip says so with a higher q. */
    if (zstd_q > 0 && zstd_q >= gzip_q)
        return PyUnicode_FromString("zstd");
    if (gzip_q > 0)
        return PyUnicode_FromString("gzip");
    Py_RETURN_NONE;
}

PyObject *
wreath_is_compressible_content_type(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    const char *data = (const char *)view.buf;
    Py_ssize_t length = 0;
    while (length < view.len && data[length] != ';') length++;
    const char *media = data;
    trim_ows(&media, &length);
    int result = 0;
    if (length >= 5 && wreath_ascii_equal_ci_str(media, 5, "text/")) result = 1;
    if (!result) {
        static const char *exact[] = {
            "application/json", "application/problem+json", "application/javascript",
            "application/xml", "image/svg+xml"
        };
        for (size_t i = 0; i < sizeof(exact) / sizeof(exact[0]); i++) {
            if (wreath_ascii_equal_ci_str(media, length, exact[i])) { result = 1; break; }
        }
    }
    if (!result && length > 12 && wreath_ascii_equal_ci_str(media, 12, "application/")) {
        if ((length >= 5 && wreath_ascii_equal_ci_str(media + length - 5, 5, "+json")) ||
            (length >= 4 && wreath_ascii_equal_ci_str(media + length - 4, 4, "+xml"))) result = 1;
    }
    PyBuffer_Release(&view);
    if (result) Py_RETURN_TRUE;
    Py_RETURN_FALSE;
}

PyObject *
wreath_cache_control_flags(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    const char *data = (const char *)view.buf;
    int flags = 0;
    Py_ssize_t start = 0;
    for (Py_ssize_t i = 0; i <= view.len; i++) {
        if (i < view.len && data[i] != ',') continue;
        const char *part = data + start;
        Py_ssize_t length = i - start;
        start = i + 1;
        trim_ows(&part, &length);
        Py_ssize_t equals = 0;
        while (equals < length && part[equals] != '=') equals++;
        length = equals;
        trim_ows(&part, &length);
        if (wreath_ascii_equal_ci_str(part, length, "no-transform")) flags |= WREATH_NO_TRANSFORM;
        else if (wreath_ascii_equal_ci_str(part, length, "no-store")) flags |= WREATH_NO_STORE;
        else if (wreath_ascii_equal_ci_str(part, length, "private")) flags |= WREATH_PRIVATE;
        else if (wreath_ascii_equal_ci_str(part, length, "public")) flags |= WREATH_PUBLIC;
    }
    PyBuffer_Release(&view);
    return PyLong_FromLong(flags);
}

static PyObject *
normalize_origin(const char *data, Py_ssize_t length)
{
    if (length == 4 && memcmp(data, "null", 4) == 0)
        return PyBytes_FromStringAndSize("null", 4);
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c <= 0x20 || c >= 0x7f || c == ',' || c == '?' || c == '#') return NULL;
    }
    Py_ssize_t scheme_len;
    int default_port;
    if (length >= 7 && wreath_ascii_equal_ci_str(data, 7, "http://")) {
        scheme_len = 4; default_port = 80;
    } else if (length >= 8 && wreath_ascii_equal_ci_str(data, 8, "https://")) {
        scheme_len = 5; default_port = 443;
    } else return NULL;
    Py_ssize_t authority_start = scheme_len + 3;
    Py_ssize_t authority_end = length;
    if (authority_end > authority_start && data[authority_end - 1] == '/') authority_end--;
    for (Py_ssize_t i = authority_start; i < authority_end; i++) {
        if (data[i] == '/' || data[i] == '@') return NULL;
    }
    if (authority_end <= authority_start) return NULL;
    Py_ssize_t host_end = authority_end;
    int port = default_port;
    if (data[authority_start] == '[') {
        Py_ssize_t close = authority_start + 1;
        while (close < authority_end && data[close] != ']') close++;
        if (close == authority_end || close == authority_start + 1) return NULL;
        host_end = close + 1;
        if (host_end < authority_end) {
            if (data[host_end] != ':') return NULL;
        }
    } else {
        Py_ssize_t colon = -1;
        for (Py_ssize_t i = authority_start; i < authority_end; i++) {
            if (data[i] == ':') {
                if (colon >= 0) return NULL;
                colon = i;
            }
        }
        if (colon >= 0) host_end = colon;
    }
    if (host_end == authority_start) return NULL;
    if (host_end < authority_end) {
        Py_ssize_t p = host_end + 1;
        if (p >= authority_end) return NULL;
        port = 0;
        for (; p < authority_end; p++) {
            if (data[p] < '0' || data[p] > '9') return NULL;
            port = port * 10 + (data[p] - '0');
            if (port > 65535) return NULL;
        }
        if (port == 0) return NULL;
    }
    char port_buffer[8];
    int port_length = port == default_port ? 0 : PyOS_snprintf(port_buffer, sizeof(port_buffer), ":%d", port);
    Py_ssize_t output_length = scheme_len + 3 + (host_end - authority_start) + port_length;
    PyObject *result = PyBytes_FromStringAndSize(NULL, output_length);
    if (result == NULL) return NULL;
    char *out = PyBytes_AS_STRING(result);
    /* ASCII by construction, not `<ctype.h>`, which folds by locale: a scheme
     * or host must normalise the same way whatever locale the process began in. */
    for (Py_ssize_t i = 0; i < scheme_len; i++)
        out[i] = (char)wreath_ascii_lower((uint8_t)data[i]);
    memcpy(out + scheme_len, "://", 3);
    Py_ssize_t at = scheme_len + 3;
    for (Py_ssize_t i = authority_start; i < host_end; i++)
        out[at++] = (char)wreath_ascii_lower((uint8_t)data[i]);
    if (port_length > 0) memcpy(out + at, port_buffer, (size_t)port_length);
    return result;
}

PyObject *
wreath_origin_matches(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer origin;
    PyObject *allowed;
    if (!PyArg_ParseTuple(args, "y*O!:origin_matches", &origin, &PyTuple_Type, &allowed))
        return NULL;
    PyObject *normalized = normalize_origin((const char *)origin.buf, origin.len);
    PyBuffer_Release(&origin);
    if (normalized == NULL) {
        if (PyErr_Occurred()) return NULL;
        Py_RETURN_FALSE;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(allowed);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *item = PyTuple_GET_ITEM(allowed, i);
        if (!PyBytes_Check(item)) {
            Py_DECREF(normalized);
            PyErr_SetString(PyExc_TypeError, "allowed origins must be bytes");
            return NULL;
        }
        PyObject *candidate = normalize_origin(PyBytes_AS_STRING(item), PyBytes_GET_SIZE(item));
        if (candidate == NULL) {
            if (PyErr_Occurred()) { Py_DECREF(normalized); return NULL; }
            continue;
        }
        int equal = PyObject_RichCompareBool(normalized, candidate, Py_EQ);
        Py_DECREF(candidate);
        if (equal != 0) {
            Py_DECREF(normalized);
            if (equal < 0) return NULL;
            Py_RETURN_TRUE;
        }
    }
    Py_DECREF(normalized);
    Py_RETURN_FALSE;
}

static int
validate_header_pair(PyObject *pair)
{
    if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2 ||
        !PyBytes_Check(PyTuple_GET_ITEM(pair, 0)) ||
        !PyBytes_Check(PyTuple_GET_ITEM(pair, 1))) {
        PyErr_SetString(PyExc_TypeError, "response headers must be two-item bytes tuples");
        return -1;
    }
    return 0;
}

static int
validate_headers(PyObject *headers)
{
    if (!PyList_Check(headers)) {
        PyErr_SetString(PyExc_TypeError, "response headers must be a list");
        return -1;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (validate_header_pair(pair) < 0) return -1;
    }
    return 0;
}

static int
header_keys_equal_ci(PyObject *left, PyObject *right)
{
    return wreath_ascii_equal_ci(PyBytes_AS_STRING(left), PyBytes_GET_SIZE(left),
                                 PyBytes_AS_STRING(right), PyBytes_GET_SIZE(right));
}

static int
header_name_is(PyObject *pair, const char *name)
{
    PyObject *key = PyTuple_GET_ITEM(pair, 0);
    return wreath_ascii_equal_ci_str(PyBytes_AS_STRING(key), PyBytes_GET_SIZE(key), name);
}

static PyObject *
ascii_lower_bytes(PyObject *value)
{
    Py_ssize_t length = PyBytes_GET_SIZE(value);
    PyObject *result = PyBytes_FromStringAndSize(NULL, length);
    if (result == NULL) return NULL;
    const unsigned char *src = (const unsigned char *)PyBytes_AS_STRING(value);
    unsigned char *dst = (unsigned char *)PyBytes_AS_STRING(result);
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char c = src[i];
        dst[i] = c >= 'A' && c <= 'Z' ? (unsigned char)(c + ('a' - 'A')) : c;
    }
    return result;
}

PyObject *
wreath_append_missing_headers(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers, *additions;
    if (!PyArg_ParseTuple(args, "OO:append_missing_headers", &headers, &additions)) return NULL;
    if (validate_headers(headers) < 0) return NULL;
    PyObject *items = PySequence_Fast(additions, "header additions must be a sequence");
    if (items == NULL) return NULL;
    Py_ssize_t addition_count = PySequence_Fast_GET_SIZE(items);
    for (Py_ssize_t i = 0; i < addition_count; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
        if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2 ||
            !PyBytes_Check(PyTuple_GET_ITEM(pair, 0)) ||
            !PyBytes_Check(PyTuple_GET_ITEM(pair, 1))) {
            Py_DECREF(items);
            PyErr_SetString(PyExc_TypeError, "header additions must be two-item bytes tuples");
            return NULL;
        }
    }

    /* The usual security-policy path is tiny. Avoid a set until the explicit
     * comparison count would exceed 256; division avoids product overflow. */
    Py_ssize_t header_count = PyList_GET_SIZE(headers);
    int use_set = addition_count > 0 && header_count > 256 / addition_count;
    PyObject *names = NULL;
    if (use_set) {
        names = PySet_New(NULL);
        if (names == NULL) goto error;
        for (Py_ssize_t i = 0; i < header_count; i++) {
            PyObject *normalized = ascii_lower_bytes(
                PyTuple_GET_ITEM(PyList_GET_ITEM(headers, i), 0));
            if (normalized == NULL) goto error;
            int added = PySet_Add(names, normalized);
            Py_DECREF(normalized);
            if (added < 0) goto error;
        }
    }

    for (Py_ssize_t i = 0; i < addition_count; i++) {
        PyObject *addition = PySequence_Fast_GET_ITEM(items, i);
        PyObject *name = PyTuple_GET_ITEM(addition, 0);
        int exists = 0;
        PyObject *normalized = NULL;
        if (names != NULL) {
            normalized = ascii_lower_bytes(name);
            if (normalized == NULL) goto error;
            exists = PySet_Contains(names, normalized);
            if (exists < 0) { Py_DECREF(normalized); goto error; }
        }
        else {
            for (Py_ssize_t j = 0; j < PyList_GET_SIZE(headers); j++) {
                PyObject *current = PyList_GET_ITEM(headers, j);
                if (header_keys_equal_ci(name, PyTuple_GET_ITEM(current, 0))) {
                    exists = 1;
                    break;
                }
            }
        }
        if (!exists) {
            if (names != NULL && PySet_Add(names, normalized) < 0) {
                Py_DECREF(normalized);
                goto error;
            }
            if (PyList_Append(headers, addition) < 0) {
                Py_XDECREF(normalized);
                goto error;
            }
        }
        Py_XDECREF(normalized);
    }
    Py_XDECREF(names);
    Py_DECREF(items);
    Py_RETURN_NONE;

error:
    Py_XDECREF(names);
    Py_DECREF(items);
    return NULL;
}

PyObject *
wreath_find_response_header(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers;
    Py_buffer name;
    if (!PyArg_ParseTuple(args, "Oy*:find_response_header", &headers, &name)) return NULL;
    if (validate_headers(headers) < 0) { PyBuffer_Release(&name); return NULL; }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        if (PyBytes_GET_SIZE(key) == name.len) {
            int equal = 1;
            const char *a = PyBytes_AS_STRING(key), *b = (const char *)name.buf;
            for (Py_ssize_t j = 0; j < name.len; j++) {
                unsigned char ca = (unsigned char)a[j], cb = (unsigned char)b[j];
                if (ca >= 'A' && ca <= 'Z') ca += 'a' - 'A';
                if (cb >= 'A' && cb <= 'Z') cb += 'a' - 'A';
                if (ca != cb) { equal = 0; break; }
            }
            if (equal) {
                PyObject *result = Py_NewRef(PyTuple_GET_ITEM(pair, 1));
                PyBuffer_Release(&name);
                return result;
            }
        }
    }
    PyBuffer_Release(&name);
    Py_RETURN_NONE;
}

static PyObject *
lower_trimmed_bytes(const char *data, Py_ssize_t length)
{
    trim_ows(&data, &length);
    PyObject *result = PyBytes_FromStringAndSize(NULL, length);
    if (result == NULL) return NULL;
    char *out = PyBytes_AS_STRING(result);
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char c = (unsigned char)data[i];
        out[i] = (char)(c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c);
    }
    return result;
}

PyObject *
wreath_append_vary(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers;
    Py_buffer token;
    if (!PyArg_ParseTuple(args, "Oy*:append_vary", &headers, &token)) return NULL;
    if (validate_headers(headers) < 0) { PyBuffer_Release(&token); return NULL; }
    PyObject *target = lower_trimmed_bytes((const char *)token.buf, token.len);
    PyBuffer_Release(&token);
    if (target == NULL) return NULL;
    if (PyBytes_GET_SIZE(target) == 0) {
        Py_DECREF(target);
        PyErr_SetString(PyExc_ValueError, "Vary token must not be empty");
        return NULL;
    }
    PyObject *tokens = PyList_New(0), *seen = PySet_New(NULL);
    if (tokens == NULL || seen == NULL) { Py_XDECREF(tokens); Py_XDECREF(seen); Py_DECREF(target); return NULL; }
    Py_ssize_t first = -1;
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (!header_name_is(pair, "vary")) continue;
        if (first < 0) first = i;
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        const char *data = PyBytes_AS_STRING(value);
        Py_ssize_t length = PyBytes_GET_SIZE(value), start = 0;
        for (Py_ssize_t j = 0; j <= length; j++) {
            if (j < length && data[j] != ',') continue;
            PyObject *part = lower_trimmed_bytes(data + start, j - start);
            start = j + 1;
            if (part == NULL) goto error;
            if (PyBytes_GET_SIZE(part) > 0) {
                int contains = PySet_Contains(seen, part);
                if (contains < 0) { Py_DECREF(part); goto error; }
                if (!contains && (PySet_Add(seen, part) < 0 || PyList_Append(tokens, part) < 0)) {
                    Py_DECREF(part); goto error;
                }
            }
            Py_DECREF(part);
        }
    }
    PyObject *star_token = PyBytes_FromStringAndSize("*", 1);
    if (star_token == NULL) goto error;
    int star = PySet_Contains(seen, star_token);
    Py_DECREF(star_token);
    if (star < 0) goto error;
    if (!star) {
        int contains = PySet_Contains(seen, target);
        if (contains < 0) goto error;
        if (!contains && PyList_Append(tokens, target) < 0) goto error;
    }
    PyObject *merged;
    if (star) merged = PyBytes_FromStringAndSize("*", 1);
    else {
        PyObject *separator = PyBytes_FromStringAndSize(", ", 2);
        merged = separator ? PyObject_CallMethod(separator, "join", "O", tokens) : NULL;
        Py_XDECREF(separator);
    }
    if (merged == NULL) goto error;
    PyObject *vary_name = PyBytes_FromString("vary");
    PyObject *pair = vary_name ? PyTuple_Pack(2, vary_name, merged) : NULL;
    Py_XDECREF(vary_name);
    Py_DECREF(merged);
    if (pair == NULL) goto error;
    if (first < 0) {
        if (PyList_Append(headers, pair) < 0) { Py_DECREF(pair); goto error; }
    } else {
        if (PyList_SetItem(headers, first, pair) < 0) { Py_DECREF(pair); goto error; }
        for (Py_ssize_t i = PyList_GET_SIZE(headers) - 1; i > first; i--)
            if (header_name_is(PyList_GET_ITEM(headers, i), "vary") && PySequence_DelItem(headers, i) < 0)
                goto error;
    }
    Py_DECREF(tokens); Py_DECREF(seen); Py_DECREF(target);
    Py_RETURN_NONE;
error:
    Py_DECREF(tokens); Py_DECREF(seen); Py_DECREF(target);
    return NULL;
}

PyObject *
wreath_replace_content_length(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers, *length_obj;
    if (!PyArg_ParseTuple(args, "OO:replace_content_length", &headers, &length_obj)) return NULL;
    if (validate_headers(headers) < 0) return NULL;
    Py_ssize_t length = 0;
    if (length_obj != Py_None) {
        length = PyLong_AsSsize_t(length_obj);
        if (length < 0) {
            if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "content length must be non-negative");
            return NULL;
        }
    }
    for (Py_ssize_t i = PyList_GET_SIZE(headers) - 1; i >= 0; i--)
        if (header_name_is(PyList_GET_ITEM(headers, i), "content-length") &&
            PySequence_DelItem(headers, i) < 0) return NULL;
    if (length_obj != Py_None) {
        PyObject *name = PyBytes_FromString("content-length");
        PyObject *value = PyBytes_FromFormat("%zd", length);
        PyObject *pair = name && value ? PyTuple_Pack(2, name, value) : NULL;
        Py_XDECREF(name); Py_XDECREF(value);
        if (pair == NULL) return NULL;
        int result = PyList_Append(headers, pair);
        Py_DECREF(pair);
        if (result < 0) return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
header_pair(PyObject *name, PyObject *value)
{
    if (!PyBytes_Check(name) || !PyBytes_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "header name and value must be bytes");
        return NULL;
    }
    return PyTuple_Pack(2, name, value);
}

PyObject *
wreath_replace_response_header(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers, *name, *value;
    if (!PyArg_ParseTuple(args, "OOO:replace_response_header", &headers, &name, &value))
        return NULL;
    PyObject *replacement = header_pair(name, value);
    if (replacement == NULL) return NULL;
    if (!PyList_Check(headers)) {
        Py_DECREF(replacement);
        PyErr_SetString(PyExc_TypeError, "response headers must be a list");
        return NULL;
    }
    Py_ssize_t count = PyList_GET_SIZE(headers), first = -1;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (validate_header_pair(pair) < 0) { Py_DECREF(replacement); return NULL; }
        if (first < 0 && header_keys_equal_ci(name, PyTuple_GET_ITEM(pair, 0))) {
            first = i;
        }
    }
    if (first < 0) {
        int result = PyList_Append(headers, replacement);
        Py_DECREF(replacement);
        if (result < 0) return NULL;
        Py_RETURN_NONE;
    }
    PyObject *rebuilt = PyList_New(0);
    if (rebuilt == NULL) { Py_DECREF(replacement); return NULL; }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (!header_keys_equal_ci(name, PyTuple_GET_ITEM(pair, 0)) &&
            PyList_Append(rebuilt, pair) < 0) goto error;
    }
    if (PyList_Append(rebuilt, replacement) < 0 ||
        PyList_SetSlice(headers, 0, count, rebuilt) < 0) goto error;
    Py_DECREF(rebuilt);
    Py_DECREF(replacement);
    Py_RETURN_NONE;
error:
    Py_DECREF(rebuilt);
    Py_DECREF(replacement);
    return NULL;
}

PyObject *
wreath_replace_cookie(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers, *prefix, *value;
    if (!PyArg_ParseTuple(args, "OOO:replace_cookie", &headers, &prefix, &value))
        return NULL;
    if (!PyBytes_Check(prefix) || !PyBytes_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "cookie prefix and value must be bytes");
        return NULL;
    }
    if (!PyList_Check(headers)) {
        PyErr_SetString(PyExc_TypeError, "response headers must be a list");
        return NULL;
    }
    Py_ssize_t count = PyList_GET_SIZE(headers);
    Py_ssize_t prefix_length = PyBytes_GET_SIZE(prefix);
    const char *prefix_data = PyBytes_AS_STRING(prefix);
    int found = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (validate_header_pair(pair) < 0) return NULL;
        PyObject *current = PyTuple_GET_ITEM(pair, 1);
        if (!found && header_name_is(pair, "set-cookie") &&
            PyBytes_GET_SIZE(current) >= prefix_length &&
            memcmp(PyBytes_AS_STRING(current), prefix_data, (size_t)prefix_length) == 0) {
            found = 1;
        }
    }
    PyObject *name = PyBytes_FromString("set-cookie");
    PyObject *replacement = name ? header_pair(name, value) : NULL;
    Py_XDECREF(name);
    if (replacement == NULL) return NULL;
    if (!found) {
        int result = PyList_Append(headers, replacement);
        Py_DECREF(replacement);
        if (result < 0) return NULL;
        Py_RETURN_NONE;
    }
    PyObject *rebuilt = PyList_New(0);
    if (rebuilt == NULL) { Py_DECREF(replacement); return NULL; }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *current = PyTuple_GET_ITEM(pair, 1);
        int matches = header_name_is(pair, "set-cookie") &&
            PyBytes_GET_SIZE(current) >= prefix_length &&
            memcmp(PyBytes_AS_STRING(current), prefix_data, (size_t)prefix_length) == 0;
        if (!matches && PyList_Append(rebuilt, pair) < 0) goto error;
    }
    if (PyList_Append(rebuilt, replacement) < 0 ||
        PyList_SetSlice(headers, 0, count, rebuilt) < 0) goto error;
    Py_DECREF(rebuilt);
    Py_DECREF(replacement);
    Py_RETURN_NONE;
error:
    Py_DECREF(rebuilt);
    Py_DECREF(replacement);
    return NULL;
}

static int
timing_metric_matches(const char *data, Py_ssize_t length, PyObject *metric)
{
    const char *semi = memchr(data, ';', (size_t)length);
    if (semi != NULL) length = (Py_ssize_t)(semi - data);
    trim_ows(&data, &length);
    Py_ssize_t target_length = PyBytes_GET_SIZE(metric);
    if (length != target_length) return 0;
    const unsigned char *target =
        (const unsigned char *)PyBytes_AS_STRING(metric);
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char left = (unsigned char)data[i], right = target[i];
        if (left >= 'A' && left <= 'Z') left += 'a' - 'A';
        if (right >= 'A' && right <= 'Z') right += 'a' - 'A';
        if (left != right) return 0;
    }
    return 1;
}

PyObject *
wreath_replace_server_timing(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers, *metric, *value;
    if (!PyArg_ParseTuple(args, "OOO:replace_server_timing", &headers, &metric, &value))
        return NULL;
    if (!PyBytes_Check(metric) || !PyBytes_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "timing metric and value must be bytes");
        return NULL;
    }
    if (!PyList_Check(headers)) {
        PyErr_SetString(PyExc_TypeError, "response headers must be a list");
        return NULL;
    }
    Py_ssize_t count = PyList_GET_SIZE(headers);
    int found = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (validate_header_pair(pair) < 0) return NULL;
        if (!found && header_name_is(pair, "server-timing")) {
            found = 1;
        }
    }
    PyObject *name = PyBytes_FromString("server-timing");
    PyObject *replacement = name ? header_pair(name, value) : NULL;
    Py_XDECREF(name);
    if (replacement == NULL) return NULL;
    if (!found) {
        int result = PyList_Append(headers, replacement);
        Py_DECREF(replacement);
        if (result < 0) return NULL;
        Py_RETURN_NONE;
    }
    PyObject *rebuilt = PyList_New(0), *retained = PyList_New(0);
    if (rebuilt == NULL || retained == NULL) {
        Py_XDECREF(rebuilt); Py_XDECREF(retained); Py_DECREF(replacement);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        if (!header_name_is(pair, "server-timing")) {
            if (PyList_Append(rebuilt, pair) < 0) goto timing_error;
            continue;
        }
        PyObject *current = PyTuple_GET_ITEM(pair, 1);
        const char *data = PyBytes_AS_STRING(current);
        Py_ssize_t length = PyBytes_GET_SIZE(current), start = 0;
        for (Py_ssize_t j = 0; j <= length; j++) {
            if (j < length && data[j] != ',') continue;
            const char *entry = data + start;
            Py_ssize_t entry_length = j - start;
            start = j + 1;
            trim_ows(&entry, &entry_length);
            if (entry_length == 0 || timing_metric_matches(entry, entry_length, metric))
                continue;
            PyObject *part = PyBytes_FromStringAndSize(entry, entry_length);
            if (part == NULL || PyList_Append(retained, part) < 0) {
                Py_XDECREF(part);
                goto timing_error;
            }
            Py_DECREF(part);
        }
    }
    if (PyList_Append(retained, value) < 0) goto timing_error;
    PyObject *separator = PyBytes_FromStringAndSize(", ", 2);
    PyObject *merged = separator ? PyObject_CallMethod(separator, "join", "O", retained) : NULL;
    Py_XDECREF(separator);
    if (merged == NULL) goto timing_error;
    PyObject *merged_name = PyBytes_FromString("server-timing");
    PyObject *merged_pair = merged_name ? header_pair(merged_name, merged) : NULL;
    Py_XDECREF(merged_name); Py_DECREF(merged);
    if (merged_pair == NULL) goto timing_error;
    int append_result = PyList_Append(rebuilt, merged_pair);
    Py_DECREF(merged_pair);
    if (append_result < 0 || PyList_SetSlice(headers, 0, count, rebuilt) < 0)
        goto timing_error;
    Py_DECREF(rebuilt); Py_DECREF(retained); Py_DECREF(replacement);
    Py_RETURN_NONE;
timing_error:
    Py_DECREF(rebuilt); Py_DECREF(retained); Py_DECREF(replacement);
    return NULL;
}

int
wreath_register_webpolicy(PyObject *module)
{
    return PyModule_AddIntConstant(module, "NO_TRANSFORM", WREATH_NO_TRANSFORM) < 0 ||
           PyModule_AddIntConstant(module, "NO_STORE", WREATH_NO_STORE) < 0 ||
           PyModule_AddIntConstant(module, "PRIVATE", WREATH_PRIVATE) < 0 ||
           PyModule_AddIntConstant(module, "PUBLIC", WREATH_PUBLIC) < 0 ? -1 : 0;
}
