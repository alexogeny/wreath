/* First-class HTTP policy executor for Wreath's native server.
 *
 * This is not a middleware interpreter. The descriptor has one fixed schema,
 * execution order is encoded here, and per-request state is a C struct owned by
 * the protocol/stream. Python sees a Request only after ingress has completed.
 */
#include "server_policy.h"

#include <math.h>
#include <string.h>
#include <time.h>

#if defined(__linux__)
#include <errno.h>
#include <sys/random.h>
#endif


static PyObject *
program_item(WreathPolicyProgram *program, Py_ssize_t index)
{
    if (program == NULL || program->descriptor == NULL) return NULL;
    PyObject *value = PyTuple_GET_ITEM(program->descriptor, index);
    return value == Py_None ? NULL : value;
}


static uint64_t
policy_now_ns(void)
{
    PyTime_t now = 0;
    (void)PyTime_MonotonicRaw(&now);
    return (uint64_t)now;
}


static int
ascii_equal_ci(const char *left, Py_ssize_t left_size,
               const char *right, Py_ssize_t right_size)
{
    if (left_size != right_size) return 0;
    for (Py_ssize_t i = 0; i < left_size; i++) {
        unsigned char a = (unsigned char)left[i];
        unsigned char b = (unsigned char)right[i];
        if (a >= 'A' && a <= 'Z') a = (unsigned char)(a + ('a' - 'A'));
        if (b >= 'A' && b <= 'Z') b = (unsigned char)(b + ('a' - 'A'));
        if (a != b) return 0;
    }
    return 1;
}


static int
bytes_equal_ci(PyObject *left, PyObject *right)
{
    return PyBytes_Check(left) && PyBytes_Check(right) &&
        ascii_equal_ci(PyBytes_AS_STRING(left), PyBytes_GET_SIZE(left),
                       PyBytes_AS_STRING(right), PyBytes_GET_SIZE(right));
}


static int
bytes_equal_literal(PyObject *value, const char *literal, Py_ssize_t size)
{
    return PyBytes_Check(value) && PyBytes_GET_SIZE(value) == size &&
        memcmp(PyBytes_AS_STRING(value), literal, (size_t)size) == 0;
}


static PyObject *
find_header(PyObject *headers, const char *name, Py_ssize_t name_size,
            Py_ssize_t *count)
{
    PyObject *found = NULL;
    Py_ssize_t matches = 0;
    Py_ssize_t size = PyList_GET_SIZE(headers);
    for (Py_ssize_t i = 0; i < size; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *candidate = PyTuple_GET_ITEM(pair, 0);
        if (PyBytes_GET_SIZE(candidate) == name_size &&
            memcmp(PyBytes_AS_STRING(candidate), name, (size_t)name_size) == 0) {
            if (found == NULL) found = PyTuple_GET_ITEM(pair, 1);
            matches++;
        }
    }
    if (count != NULL) *count = matches;
    return found; /* borrowed */
}


static PyObject *
find_named_header(PyObject *headers, PyObject *name)
{
    return find_header(headers, PyBytes_AS_STRING(name), PyBytes_GET_SIZE(name), NULL);
}


static int
response_header_index(PyObject *headers, PyObject *name)
{
    Py_ssize_t size = PyList_GET_SIZE(headers);
    for (Py_ssize_t i = 0; i < size; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *candidate = PyTuple_GET_ITEM(pair, 0);
        if (bytes_equal_ci(candidate, name)) return (int)i;
    }
    return -1;
}


static int
append_pair(PyObject *headers, PyObject *name, PyObject *value)
{
    PyObject *pair = PyTuple_Pack(2, name, value);
    if (pair == NULL) return -1;
    int result = PyList_Append(headers, pair);
    Py_DECREF(pair);
    return result;
}


static int
append_literal(PyObject *headers, const char *name, const char *value)
{
    PyObject *name_obj = PyBytes_FromString(name);
    PyObject *value_obj = name_obj != NULL ? PyBytes_FromString(value) : NULL;
    int result = value_obj != NULL ? append_pair(headers, name_obj, value_obj) : -1;
    Py_XDECREF(name_obj);
    Py_XDECREF(value_obj);
    return result;
}


static int
replace_header(PyObject *headers, PyObject *name, PyObject *value)
{
    int index = response_header_index(headers, name);
    PyObject *pair = PyTuple_Pack(2, name, value);
    if (pair == NULL) return -1;
    int result;
    if (index >= 0) {
        result = PyList_SetItem(headers, index, pair); /* steals */
    }
    else {
        result = PyList_Append(headers, pair);
        Py_DECREF(pair);
    }
    return result;
}


static int
append_missing(PyObject *headers, PyObject *additions)
{
    Py_ssize_t size = PyTuple_GET_SIZE(additions);
    for (Py_ssize_t i = 0; i < size; i++) {
        PyObject *pair = PyTuple_GET_ITEM(additions, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        if (response_header_index(headers, name) < 0 &&
            PyList_Append(headers, pair) < 0) {
            return -1;
        }
    }
    return 0;
}


static int
append_vary(PyObject *headers, const char *token, Py_ssize_t token_size)
{
    PyObject *vary_name = PyBytes_FromStringAndSize("vary", 4);
    if (vary_name == NULL) return -1;
    int index = response_header_index(headers, vary_name);
    if (index < 0) {
        PyObject *value = PyBytes_FromStringAndSize(token, token_size);
        int result = value != NULL ? append_pair(headers, vary_name, value) : -1;
        Py_XDECREF(value);
        Py_DECREF(vary_name);
        return result;
    }
    PyObject *pair = PyList_GET_ITEM(headers, index);
    PyObject *old = PyTuple_GET_ITEM(pair, 1);
    const char *data = PyBytes_AS_STRING(old);
    Py_ssize_t size = PyBytes_GET_SIZE(old);
    Py_ssize_t start = 0;
    while (start <= size) {
        Py_ssize_t end = start;
        while (end < size && data[end] != ',') end++;
        const char *part = data + start;
        Py_ssize_t part_size = end - start;
        while (part_size > 0 && (*part == ' ' || *part == '\t')) {
            part++;
            part_size--;
        }
        while (part_size > 0 &&
               (part[part_size - 1] == ' ' || part[part_size - 1] == '\t')) {
            part_size--;
        }
        if (ascii_equal_ci(part, part_size, token, token_size)) {
            Py_DECREF(vary_name);
            return 0;
        }
        if (end == size) break;
        start = end + 1;
    }
    PyObject *merged = PyBytes_FromStringAndSize(NULL, size + 2 + token_size);
    if (merged == NULL) {
        Py_DECREF(vary_name);
        return -1;
    }
    char *out = PyBytes_AS_STRING(merged);
    memcpy(out, data, (size_t)size);
    memcpy(out + size, ", ", 2);
    memcpy(out + size + 2, token, (size_t)token_size);
    int result = replace_header(headers, vary_name, merged);
    Py_DECREF(vary_name);
    Py_DECREF(merged);
    return result;
}


static int
request_id_valid(PyObject *value, Py_ssize_t maximum)
{
    if (!PyBytes_Check(value)) return 0;
    Py_ssize_t size = PyBytes_GET_SIZE(value);
    if (size < 1 || size > maximum) return 0;
    const unsigned char *data = (const unsigned char *)PyBytes_AS_STRING(value);
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = data[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.')) {
            return 0;
        }
    }
    return 1;
}


static PyObject *
random_hex(PyObject *fallback)
{
    static const char digits[] = "0123456789abcdef";
    unsigned char raw[16];
    char encoded[32];
    int ready = 0;
#if defined(__linux__)
    Py_ssize_t filled = 0;
    while (filled < (Py_ssize_t)sizeof(raw)) {
        ssize_t got = getrandom(raw + filled, sizeof(raw) - (size_t)filled, 0);
        if (got < 0) {
            if (errno == EINTR) continue;
            break;
        }
        filled += got;
    }
    ready = filled == (Py_ssize_t)sizeof(raw);
#endif
    if (!ready) {
        PyObject *size = PyLong_FromLong(16);
        PyObject *drawn = size != NULL
            ? PyObject_CallOneArg(fallback, size) : NULL;
        Py_XDECREF(size);
        if (drawn == NULL) return NULL;
        if (!PyBytes_Check(drawn) || PyBytes_GET_SIZE(drawn) != 16) {
            Py_DECREF(drawn);
            PyErr_SetString(PyExc_RuntimeError, "os.urandom returned the wrong size");
            return NULL;
        }
        memcpy(raw, PyBytes_AS_STRING(drawn), sizeof(raw));
        Py_DECREF(drawn);
    }
    for (int i = 0; i < 16; i++) {
        encoded[i * 2] = digits[raw[i] >> 4];
        encoded[i * 2 + 1] = digits[raw[i] & 15];
    }
    return PyBytes_FromStringAndSize(encoded, 32);
}


static int
reply_body(WreathPolicyReply *reply, int status, const char *content_type,
           const char *body, Py_ssize_t body_size)
{
    reply->status = status;
    reply->headers = PyList_New(0);
    reply->body = PyBytes_FromStringAndSize(body, body_size);
    if (reply->headers == NULL || reply->body == NULL) return -1;
    if (append_literal(reply->headers, "content-type", content_type) < 0) return -1;
    if (status != 204) {
        char length[32];
        int written = PyOS_snprintf(length, sizeof(length), "%zd", body_size);
        if (written < 0 || append_literal(reply->headers, "content-length", length) < 0) {
            return -1;
        }
    }
    return 1;
}


static int
reply_problem(WreathPolicyReply *reply, int status)
{
    if (status == 400) {
        static const char body[] =
            "{\"type\":\"about:blank\",\"title\":\"Bad Request\",\"status\":400,"
            "\"detail\":\"Invalid Host header\"}";
        return reply_body(reply, 400, "application/problem+json", body,
                          (Py_ssize_t)sizeof(body) - 1);
    }
    if (status == 403) {
        static const char body[] =
            "{\"type\":\"about:blank\",\"title\":\"Forbidden\",\"status\":403,"
            "\"detail\":\"CSRF validation failed\"}";
        return reply_body(reply, 403, "application/problem+json", body,
                          (Py_ssize_t)sizeof(body) - 1);
    }
    static const char body[] =
        "{\"type\":\"about:blank\",\"title\":\"Too Many Requests\",\"status\":429,"
        "\"detail\":\"Rate limit exceeded\"}";
    return reply_body(reply, 429, "application/problem+json", body,
                      (Py_ssize_t)sizeof(body) - 1);
}


int
wreath_policy_program_load(WreathPolicyProgram *program, PyObject *app)
{
    program->descriptor = PyObject_GetAttrString(app, "_wreath_policy");
    if (program->descriptor == NULL) {
        if (PyErr_ExceptionMatches(PyExc_AttributeError)) {
            PyErr_Clear();
            return 0;
        }
        return -1;
    }
    if (program->descriptor == Py_None) {
        Py_CLEAR(program->descriptor);
        return 0;
    }
    if (!PyTuple_CheckExact(program->descriptor) ||
        PyTuple_GET_SIZE(program->descriptor) != WREATH_POLICY_SIZE) {
        PyErr_SetString(PyExc_RuntimeError, "invalid native HTTP policy descriptor");
        return -1;
    }
    PyObject *tag = PyTuple_GET_ITEM(program->descriptor, WREATH_POLICY_TAG);
    if (!PyUnicode_Check(tag) ||
        PyUnicode_CompareWithASCIIString(tag, "wreath.http-policy.v1") != 0) {
        PyErr_SetString(PyExc_RuntimeError, "unsupported native HTTP policy descriptor");
        return -1;
    }
    return 0;
}


void
wreath_policy_program_clear(WreathPolicyProgram *program)
{
    Py_CLEAR(program->descriptor);
}


void
wreath_policy_state_init(WreathPolicyState *state)
{
    memset(state, 0, sizeof(*state));
}


void
wreath_policy_state_clear(WreathPolicyState *state)
{
    Py_CLEAR(state->client);
    Py_CLEAR(state->scheme);
    Py_CLEAR(state->origin);
    Py_CLEAR(state->request_id);
    Py_CLEAR(state->csrf_token);
    memset(state, 0, sizeof(*state));
}


void
wreath_policy_reply_clear(WreathPolicyReply *reply)
{
    Py_CLEAR(reply->headers);
    Py_CLEAR(reply->body);
    reply->status = 0;
}


/* Return 1 allowed, 0 refused. Patterns were normalized at construction. */
static int
trusted_host_allowed(PyObject *patterns, PyObject *value)
{
    if (!PyBytes_Check(value)) return 0;
    const char *data = PyBytes_AS_STRING(value);
    Py_ssize_t size = PyBytes_GET_SIZE(value);
    Py_ssize_t host_size = size;
    if (size == 0) return 0;
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c <= 0x20 || c >= 0x7f || c == '@' || c == '/' || c == '\\') return 0;
    }
    if (data[0] == '[') {
        Py_ssize_t close = 1;
        while (close < size && data[close] != ']') close++;
        if (close == size || close == 1) return 0;
        host_size = close + 1;
        if (host_size < size) {
            if (data[host_size] != ':' || host_size + 1 == size) return 0;
            for (Py_ssize_t i = host_size + 1; i < size; i++) {
                if (data[i] < '0' || data[i] > '9') return 0;
            }
        }
    }
    else {
        Py_ssize_t colon = -1;
        for (Py_ssize_t i = 0; i < size; i++) {
            if (data[i] == ':') {
                if (colon >= 0) return 0;
                colon = i;
            }
        }
        if (colon >= 0) {
            if (colon == 0 || colon + 1 == size) return 0;
            for (Py_ssize_t i = colon + 1; i < size; i++) {
                if (data[i] < '0' || data[i] > '9') return 0;
            }
            host_size = colon;
        }
    }
    if (host_size == 0) return 0;
    Py_ssize_t count = PyTuple_GET_SIZE(patterns);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pattern = PyTuple_GET_ITEM(patterns, i);
        const char *pd = PyBytes_AS_STRING(pattern);
        Py_ssize_t ps = PyBytes_GET_SIZE(pattern);
        if (ps == 1 && pd[0] == '*') return 1;
        if (ps > 2 && pd[0] == '*' && pd[1] == '.') {
            Py_ssize_t suffix = ps - 1;
            if (host_size > suffix &&
                ascii_equal_ci(data + host_size - suffix, suffix, pd + 1, suffix)) {
                return 1;
            }
        }
        else if (ascii_equal_ci(data, host_size, pd, ps)) {
            return 1;
        }
    }
    return 0;
}


static int
origin_allowed(PyObject *cors, PyObject *origin)
{
    if (PyTuple_GET_ITEM(cors, 0) == Py_True) return 1;
    PyObject *origins = PyTuple_GET_ITEM(cors, 1);
    int contains = PySet_Contains(origins, origin);
    if (contains != 0) return contains;
    PyObject *lowered = PyObject_CallMethod(origin, "lower", NULL);
    if (lowered == NULL) return -1;
    contains = PySet_Contains(origins, lowered);
    Py_DECREF(lowered);
    return contains;
}


static int
method_allowed(PyObject *methods, PyObject *method)
{
    PyObject *iterator = PyObject_GetIter(methods);
    if (iterator == NULL) return -1;
    PyObject *candidate;
    while ((candidate = PyIter_Next(iterator)) != NULL) {
        int equal = bytes_equal_ci(candidate, method);
        Py_DECREF(candidate);
        if (equal) {
            Py_DECREF(iterator);
            return 1;
        }
    }
    Py_DECREF(iterator);
    return PyErr_Occurred() ? -1 : 0;
}


static int
cors_preflight(WreathPolicyState *state, PyObject *cors, PyObject *method,
               PyObject *headers, WreathPolicyReply *reply)
{
    PyObject *origin = find_header(headers, "origin", 6, NULL);
    if (origin != NULL) state->origin = Py_NewRef(origin);
    if (PyUnicode_CompareWithASCIIString(method, "OPTIONS") != 0) return 0;
    PyObject *requested = find_header(
        headers, "access-control-request-method", 29, NULL);
    if (origin == NULL || requested == NULL) return 0;
    int allowed_origin = origin_allowed(cors, origin);
    if (allowed_origin < 0) return -1;
    int allowed_method = method_allowed(PyTuple_GET_ITEM(cors, 2), requested);
    if (allowed_method < 0) return -1;
    if (!allowed_method || !allowed_origin) {
        const char *body = allowed_method ? "disallowed origin" : "disallowed method";
        if (reply_body(reply, 403, "text/plain", body, 17) < 0 ||
            append_literal(reply->headers, "vary", "origin") < 0) return -1;
        return 1;
    }
    if (reply_body(reply, 204, "application/octet-stream", "", 0) < 0) return -1;
    PyObject *allow_name = PyBytes_FromString("access-control-allow-origin");
    PyObject *allow_value = PyTuple_GET_ITEM(cors, 0) == Py_True
        ? PyBytes_FromString("*") : Py_NewRef(origin);
    if (allow_name == NULL || allow_value == NULL ||
        append_pair(reply->headers, allow_name, allow_value) < 0) {
        Py_XDECREF(allow_name);
        Py_XDECREF(allow_value);
        return -1;
    }
    Py_DECREF(allow_name);
    Py_DECREF(allow_value);
    PyObject *preflight = PyTuple_GET_ITEM(cors, 3);
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(preflight); i++) {
        if (PyList_Append(reply->headers, PyTuple_GET_ITEM(preflight, i)) < 0) return -1;
    }
    if (PyTuple_GET_ITEM(cors, 0) != Py_True &&
        append_literal(reply->headers, "vary", "origin") < 0) return -1;
    return 1;
}


static int
run_rate(WreathPolicyState *state, PyObject *rate, WreathPolicyReply *reply)
{
    PyObject *key;
    if (state->client == NULL || state->client == Py_None ||
        !PyTuple_Check(state->client) || PyTuple_GET_SIZE(state->client) < 1) {
        key = PyUnicode_FromString("\0unkeyed");
    }
    else {
        key = PyObject_Str(PyTuple_GET_ITEM(state->client, 0));
    }
    if (key == NULL) return -1;
    PyObject *bucket = PyTuple_GET_ITEM(rate, 0);
    double cost = PyFloat_AsDouble(PyTuple_GET_ITEM(rate, 1));
    if (cost == -1.0 && PyErr_Occurred()) {
        Py_DECREF(key);
        return -1;
    }
    double now = (double)policy_now_ns() / 1000000000.0;
    PyObject *result = PyObject_CallMethod(bucket, "acquire", "Odd", key, now, cost);
    Py_DECREF(key);
    if (result == NULL) return -1;
    double retry = PyFloat_AsDouble(result);
    Py_DECREF(result);
    if (retry == -1.0 && PyErr_Occurred()) return -1;
    if (retry <= 0.0) return 0;
    if (reply_problem(reply, 429) < 0) return -1;
    long seconds = (long)ceil(retry);
    if (seconds < 1) seconds = 1;
    char buffer[32];
    PyOS_snprintf(buffer, sizeof(buffer), "%ld", seconds);
    if (append_literal(reply->headers, "retry-after", buffer) < 0) return -1;
    PyObject *policy_headers = PyTuple_GET_ITEM(rate, 2);
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(policy_headers); i++) {
        if (PyList_Append(reply->headers, PyTuple_GET_ITEM(policy_headers, i)) < 0) return -1;
    }
    if (append_literal(reply->headers, "x-ratelimit-remaining", "0") < 0) return -1;
    PyObject *owner = PyTuple_GET_ITEM(rate, 3);
    PyObject *old = PyObject_GetAttrString(owner, "throttled");
    PyObject *one = old != NULL ? PyLong_FromLong(1) : NULL;
    PyObject *next = one != NULL ? PyNumber_Add(old, one) : NULL;
    Py_XDECREF(old);
    Py_XDECREF(one);
    if (next == NULL || PyObject_SetAttrString(owner, "throttled", next) < 0) {
        Py_XDECREF(next);
        return -1;
    }
    Py_DECREF(next);
    return 1;
}


static int
run_proxy(WreathPolicyState *state, PyObject *proxy, PyObject *headers)
{
    if (state->client == NULL || state->client == Py_None ||
        !PyTuple_Check(state->client) || PyTuple_GET_SIZE(state->client) < 1) {
        return 0;
    }
    PyObject *networks = PyTuple_GET_ITEM(proxy, 0);
    PyObject *address = PyObject_Str(PyTuple_GET_ITEM(state->client, 0));
    PyObject *contains = address != NULL
        ? PyObject_CallMethod(networks, "contains", "O", address) : NULL;
    Py_XDECREF(address);
    if (contains == NULL) return -1;
    int trusted = PyObject_IsTrue(contains);
    Py_DECREF(contains);
    if (trusted <= 0) return trusted;

    PyObject *forwarded = find_header(headers, "x-forwarded-for", 15, NULL);
    if (forwarded != NULL) {
        PyObject *client = PyObject_CallMethod(
            networks, "forwarded_client", "O", forwarded);
        if (client == NULL) return -1;
        if (client != Py_None) {
            PyObject *effective = PyTuple_Pack(2, client, Py_None);
            Py_DECREF(client);
            if (effective == NULL) return -1;
            Py_SETREF(state->client, effective);
        }
        else {
            Py_DECREF(client);
        }
    }

    if (PyTuple_GET_ITEM(proxy, 1) == Py_True) {
        PyObject *proto = find_header(headers, "x-forwarded-proto", 17, NULL);
        if (proto != NULL) {
            const char *data = PyBytes_AS_STRING(proto);
            Py_ssize_t size = PyBytes_GET_SIZE(proto);
            Py_ssize_t end = 0;
            while (end < size && data[end] != ',') end++;
            Py_ssize_t start = 0;
            while (start < end && (data[start] == ' ' || data[start] == '\t')) start++;
            while (end > start && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
            if (ascii_equal_ci(data + start, end - start, "http", 4) ||
                ascii_equal_ci(data + start, end - start, "https", 5)) {
                PyObject *scheme = PyUnicode_FromString(
                    end - start == 4 ? "http" : "https");
                if (scheme == NULL) return -1;
                Py_SETREF(state->scheme, scheme);
            }
        }
    }

    if (PyTuple_GET_ITEM(proxy, 2) == Py_True) {
        PyObject *forwarded_host = find_header(
            headers, "x-forwarded-host", 16, NULL);
        if (forwarded_host != NULL) {
            const char *data = PyBytes_AS_STRING(forwarded_host);
            Py_ssize_t size = PyBytes_GET_SIZE(forwarded_host);
            Py_ssize_t end = 0;
            while (end < size && data[end] != ',') end++;
            Py_ssize_t start = 0;
            while (start < end && (data[start] == ' ' || data[start] == '\t')) start++;
            while (end > start && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
            if (end > start) {
                PyObject *name = PyBytes_FromString("host");
                PyObject *value = PyBytes_FromStringAndSize(data + start, end - start);
                int result = name != NULL && value != NULL
                    ? replace_header(headers, name, value) : -1;
                Py_XDECREF(name);
                Py_XDECREF(value);
                if (result < 0) return -1;
            }
        }
    }
    return 0;
}


static PyObject *
cookie_value(PyObject *headers, PyObject *wanted)
{
    const char *wanted_data = PyBytes_AS_STRING(wanted);
    Py_ssize_t wanted_size = PyBytes_GET_SIZE(wanted);
    Py_ssize_t count = PyList_GET_SIZE(headers);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        if (PyBytes_GET_SIZE(name) != 6 ||
            memcmp(PyBytes_AS_STRING(name), "cookie", 6) != 0) continue;
        PyObject *line = PyTuple_GET_ITEM(pair, 1);
        const char *data = PyBytes_AS_STRING(line);
        Py_ssize_t size = PyBytes_GET_SIZE(line);
        Py_ssize_t start = 0;
        while (start <= size) {
            const char *separator = start < size
                ? memchr(data + start, ';', (size_t)(size - start)) : NULL;
            Py_ssize_t separator_at = separator != NULL ? separator - data : size;
            Py_ssize_t end = separator_at;
            Py_ssize_t low = start;
            while (low < end && (data[low] == ' ' || data[low] == '\t')) low++;
            while (end > low && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
            const char *equals = low < end
                ? memchr(data + low, '=', (size_t)(end - low)) : NULL;
            if (equals != NULL && equals - (data + low) == wanted_size &&
                memcmp(data + low, wanted_data, (size_t)wanted_size) == 0) {
                Py_ssize_t value_start = (equals - data) + 1;
                return PyUnicode_DecodeLatin1(
                    data + value_start, end - value_start, NULL);
            }
            if (separator == NULL) break;
            start = separator_at + 1;
        }
    }
    return NULL;
}


static int
csrf_validate_token(PyObject *csrf, PyObject *token, long long now,
                    int *valid, long long *issued)
{
    PyObject *result = PyObject_CallFunction(
        PyTuple_GET_ITEM(csrf, 12), "OOLL", PyTuple_GET_ITEM(csrf, 0), token,
        now, PyLong_AsLongLong(PyTuple_GET_ITEM(csrf, 4)));
    if (result == NULL) return -1;
    *valid = PyObject_IsTrue(PyTuple_GET_ITEM(result, 0));
    *issued = PyLong_AsLongLong(PyTuple_GET_ITEM(result, 1));
    Py_DECREF(result);
    return (*valid < 0 || (*issued == -1 && PyErr_Occurred())) ? -1 : 0;
}


static PyObject *
csrf_new_token(PyObject *csrf, long long now)
{
    return PyObject_CallFunction(
        PyTuple_GET_ITEM(csrf, 11), "OL", PyTuple_GET_ITEM(csrf, 0), now);
}


static int
csrf_origin_valid(WreathPolicyState *state, PyObject *csrf, PyObject *headers)
{
    PyObject *host = find_header(headers, "host", 4, NULL);
    if (host == NULL || state->scheme == NULL) return 0;
    PyObject *trusted_hosts = PyTuple_GET_ITEM(csrf, 8);
    if (PyTuple_GET_SIZE(trusted_hosts) > 0) {
        int allowed = 0;
        for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(trusted_hosts); i++) {
            if (bytes_equal_ci(host, PyTuple_GET_ITEM(trusted_hosts, i))) {
                allowed = 1;
                break;
            }
        }
        if (!allowed) return 0;
    }
    Py_ssize_t scheme_size;
    const char *scheme = PyUnicode_AsUTF8AndSize(state->scheme, &scheme_size);
    if (scheme == NULL ||
        !(ascii_equal_ci(scheme, scheme_size, "http", 4) ||
          ascii_equal_ci(scheme, scheme_size, "https", 5))) return 0;
    PyObject *expected = PyBytes_FromStringAndSize(
        NULL, scheme_size + 3 + PyBytes_GET_SIZE(host));
    if (expected == NULL) return -1;
    char *expected_data = PyBytes_AS_STRING(expected);
    for (Py_ssize_t i = 0; i < scheme_size; i++) {
        unsigned char c = (unsigned char)scheme[i];
        expected_data[i] = (char)(c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c);
    }
    memcpy(expected_data + scheme_size, "://", 3);
    memcpy(expected_data + scheme_size + 3, PyBytes_AS_STRING(host),
           (size_t)PyBytes_GET_SIZE(host));
    PyObject *trusted = PyTuple_GET_ITEM(csrf, 7);
    PyObject *allowed = PyTuple_New(PyTuple_GET_SIZE(trusted) + 1);
    if (allowed == NULL) {
        Py_DECREF(expected);
        return -1;
    }
    PyTuple_SET_ITEM(allowed, 0, expected); /* steals */
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(trusted); i++) {
        PyTuple_SET_ITEM(allowed, i + 1, Py_NewRef(PyTuple_GET_ITEM(trusted, i)));
    }
    PyObject *candidate = find_header(headers, "origin", 6, NULL);
    PyObject *owned_candidate = NULL;
    if (candidate == NULL) {
        PyObject *referer = find_header(headers, "referer", 7, NULL);
        if (referer != NULL) {
            const char *data = PyBytes_AS_STRING(referer);
            Py_ssize_t size = PyBytes_GET_SIZE(referer);
            const char *marker = memchr(data, ':', (size_t)size);
            if (marker != NULL && marker + 2 < data + size &&
                marker[1] == '/' && marker[2] == '/') {
                const char *end = marker + 3;
                while (end < data + size && *end != '/' && *end != '?' && *end != '#') end++;
                owned_candidate = PyBytes_FromStringAndSize(data, end - data);
                candidate = owned_candidate;
            }
        }
    }
    if (candidate == NULL) {
        Py_DECREF(allowed);
        return PyTuple_GET_ITEM(csrf, 9) == Py_True;
    }
    PyObject *matched = PyObject_CallFunctionObjArgs(
        PyTuple_GET_ITEM(csrf, 13), candidate, allowed, NULL);
    Py_XDECREF(owned_candidate);
    Py_DECREF(allowed);
    if (matched == NULL) return -1;
    int result = PyObject_IsTrue(matched);
    Py_DECREF(matched);
    return result;
}


static int
increment_counter(PyObject *owner, const char *name)
{
    PyObject *old = PyObject_GetAttrString(owner, name);
    PyObject *one = old != NULL ? PyLong_FromLong(1) : NULL;
    PyObject *next = one != NULL ? PyNumber_Add(old, one) : NULL;
    Py_XDECREF(old);
    Py_XDECREF(one);
    if (next == NULL || PyObject_SetAttrString(owner, name, next) < 0) {
        Py_XDECREF(next);
        return -1;
    }
    Py_DECREF(next);
    return 0;
}


static int
run_csrf(WreathPolicyState *state, PyObject *csrf, PyObject *method,
         PyObject *headers, WreathPolicyReply *reply)
{
    state->csrf_config = csrf;
    int safe = PyUnicode_CompareWithASCIIString(method, "GET") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "HEAD") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "OPTIONS") == 0;
    PyObject *site = find_header(headers, "sec-fetch-site", 14, NULL);
    if (site != NULL) {
        if (safe || bytes_equal_literal(site, "same-origin", 11) ||
            bytes_equal_literal(site, "none", 4)) {
            state->csrf_minter = 1;
            return 0;
        }
        if (increment_counter(PyTuple_GET_ITEM(csrf, 10),
                              "cross_site_refusals") < 0) return -1;
        return reply_problem(reply, 403);
    }
    long long now = (long long)time(NULL);
    PyObject *cookie = cookie_value(headers, PyTuple_GET_ITEM(csrf, 1));
    if (safe) {
        int valid = 0;
        long long issued = 0;
        if (cookie != NULL && csrf_validate_token(
                csrf, cookie, now, &valid, &issued) < 0) {
            Py_DECREF(cookie);
            return -1;
        }
        long long max_age = PyLong_AsLongLong(PyTuple_GET_ITEM(csrf, 4));
        if (max_age == -1 && PyErr_Occurred()) {
            Py_XDECREF(cookie);
            return -1;
        }
        int renew = !valid || now - issued >= max_age * 3 / 4;
        state->csrf_token = renew ? csrf_new_token(csrf, now) : cookie;
        if (renew) Py_XDECREF(cookie);
        state->csrf_issue = (unsigned char)renew;
        return state->csrf_token != NULL ? 0 : -1;
    }
    PyObject *submitted = find_named_header(headers, PyTuple_GET_ITEM(csrf, 3));
    int valid = 0;
    long long issued = now;
    if (cookie != NULL && submitted != NULL) {
        Py_ssize_t token_size;
        const char *token = PyUnicode_AsUTF8AndSize(cookie, &token_size);
        if (token == NULL) {
            Py_DECREF(cookie);
            return -1;
        }
        if (token_size == PyBytes_GET_SIZE(submitted)) {
            unsigned char difference = 0;
            const char *other = PyBytes_AS_STRING(submitted);
            for (Py_ssize_t i = 0; i < token_size; i++) {
                difference |= (unsigned char)(token[i] ^ other[i]);
            }
            if (difference == 0 &&
                csrf_validate_token(csrf, cookie, now, &valid, &issued) < 0) {
                Py_DECREF(cookie);
                return -1;
            }
        }
    }
    int origin_valid = valid ? csrf_origin_valid(state, csrf, headers) : 0;
    if (origin_valid < 0) {
        Py_XDECREF(cookie);
        return -1;
    }
    if (!valid || !origin_valid) {
        Py_XDECREF(cookie);
        return reply_problem(reply, 403);
    }
    long long max_age = PyLong_AsLongLong(PyTuple_GET_ITEM(csrf, 4));
    if (max_age == -1 && PyErr_Occurred()) {
        Py_DECREF(cookie);
        return -1;
    }
    int renew = now - issued >= max_age * 3 / 4;
    state->csrf_token = renew ? csrf_new_token(csrf, now) : cookie;
    if (renew) Py_DECREF(cookie);
    state->csrf_issue = (unsigned char)renew;
    return state->csrf_token != NULL ? 0 : -1;
}


int
wreath_policy_ingress(WreathPolicyProgram *program, WreathPolicyState *state,
                      PyObject *method, PyObject *scheme, PyObject *client,
                      PyObject *headers, WreathPolicyReply *reply)
{
    wreath_policy_state_clear(state);
    memset(reply, 0, sizeof(*reply));
    if (program->descriptor == NULL) return 0;
    state->native = 1;
    state->client = Py_NewRef(client);
    state->scheme = Py_NewRef(scheme);
    if (program_item(program, WREATH_POLICY_SECURITY) != NULL) {
        state->completed |= WREATH_POLICY_DONE_SECURITY;
    }

    PyObject *proxy = program_item(program, WREATH_POLICY_PROXY);
    if (proxy != NULL) {
        if (run_proxy(state, proxy, headers) < 0) return -1;
        state->completed |= WREATH_POLICY_DONE_PROXY;
    }

    PyObject *trusted = program_item(program, WREATH_POLICY_TRUSTED_HOST);
    if (trusted != NULL) {
        Py_ssize_t count = 0;
        PyObject *host = find_header(headers, "host", 4, &count);
        state->completed |= WREATH_POLICY_DONE_TRUSTED_HOST;
        if (count != 1 || !trusted_host_allowed(trusted, host)) {
            return reply_problem(reply, 400);
        }
    }

    PyObject *rate = program_item(program, WREATH_POLICY_RATE);
    if (rate != NULL) {
        int result = run_rate(state, rate, reply);
        state->completed |= WREATH_POLICY_DONE_RATE;
        if (result != 0) return result;
    }

    PyObject *request_id = program_item(program, WREATH_POLICY_REQUEST_ID);
    if (request_id != NULL) {
        PyObject *name = PyTuple_GET_ITEM(request_id, 0);
        int trust = PyTuple_GET_ITEM(request_id, 1) == Py_True;
        Py_ssize_t maximum = PyLong_AsSsize_t(PyTuple_GET_ITEM(request_id, 3));
        if (maximum == -1 && PyErr_Occurred()) return -1;
        PyObject *inbound = trust ? find_named_header(headers, name) : NULL;
        state->request_id = inbound != NULL && request_id_valid(inbound, maximum)
            ? Py_NewRef(inbound) : random_hex(PyTuple_GET_ITEM(request_id, 4));
        if (state->request_id == NULL) return -1;
        state->completed |= WREATH_POLICY_DONE_REQUEST_ID;
    }

    PyObject *timing = program_item(program, WREATH_POLICY_TIMING);
    if (timing != NULL) {
        state->started_ns = policy_now_ns();
        state->completed |= WREATH_POLICY_DONE_TIMING;
    }

    PyObject *cors = program_item(program, WREATH_POLICY_CORS);
    if (cors != NULL) {
        int result = cors_preflight(state, cors, method, headers, reply);
        state->completed |= WREATH_POLICY_DONE_CORS;
        if (result != 0) return result;
    }

    PyObject *csrf = program_item(program, WREATH_POLICY_CSRF);
    if (csrf != NULL) {
        int result = run_csrf(state, csrf, method, headers, reply);
        state->completed |= WREATH_POLICY_DONE_CSRF;
        if (result != 0) return result;
    }
    return 0;
}


PyObject *
wreath_policy_csrf_token(WreathPolicyState *state)
{
    if (state == NULL || state->csrf_config == NULL) Py_RETURN_NONE;
    if (state->csrf_token == NULL && state->csrf_minter) {
        state->csrf_token = csrf_new_token(
            state->csrf_config, (long long)time(NULL));
        if (state->csrf_token == NULL) return NULL;
        state->csrf_issue = 1;
    }
    if (state->csrf_token == NULL) Py_RETURN_NONE;
    return Py_NewRef(state->csrf_token);
}


int
wreath_policy_websocket_origin(WreathPolicyProgram *program, PyObject *headers,
                               WreathPolicyReply *reply)
{
    PyObject *config = program_item(
        program, WREATH_POLICY_WEBSOCKET_ORIGIN);
    if (config == NULL) return 0;
    Py_ssize_t count = 0;
    PyObject *origin = find_header(headers, "origin", 6, &count);
    int allowed = 0;
    if (count == 1) {
        PyObject *matched = PyObject_CallFunctionObjArgs(
            PyTuple_GET_ITEM(config, 1), origin,
            PyTuple_GET_ITEM(config, 0), NULL);
        if (matched == NULL) return -1;
        allowed = PyObject_IsTrue(matched);
        Py_DECREF(matched);
        if (allowed < 0) return -1;
    }
    if (allowed) return 0;
    static const char body[] =
        "{\"type\":\"about:blank\",\"title\":\"Forbidden\",\"status\":403,"
        "\"detail\":\"WebSocket origin is not allowed\"}";
    return reply_body(reply, 403, "application/problem+json", body,
                      (Py_ssize_t)sizeof(body) - 1);
}


static int
cors_egress(WreathPolicyState *state, PyObject *cors, PyObject *headers)
{
    if (state->origin == NULL) return 0;
    int allowed = origin_allowed(cors, state->origin);
    if (allowed < 0) return -1;
    int wildcard = PyTuple_GET_ITEM(cors, 0) == Py_True;
    if (allowed) {
        PyObject *name = PyBytes_FromString("access-control-allow-origin");
        if (name == NULL) return -1;
        if (response_header_index(headers, name) < 0) {
            PyObject *value = wildcard ? PyBytes_FromString("*") : Py_NewRef(state->origin);
            if (value == NULL || append_pair(headers, name, value) < 0) {
                Py_XDECREF(value);
                Py_DECREF(name);
                return -1;
            }
            Py_DECREF(value);
            PyObject *simple = PyTuple_GET_ITEM(cors, 4);
            for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(simple); i++) {
                if (PyList_Append(headers, PyTuple_GET_ITEM(simple, i)) < 0) {
                    Py_DECREF(name);
                    return -1;
                }
            }
        }
        Py_DECREF(name);
    }
    if (!wildcard && append_vary(headers, "origin", 6) < 0) return -1;
    return 0;
}


int
wreath_policy_egress(WreathPolicyProgram *program, WreathPolicyState *state,
                     PyObject *headers)
{
    if (program->descriptor == NULL || !state->native) return 0;
    if (!PyList_Check(headers)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native HTTP policy requires a mutable response header list");
        return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_SECURITY) {
        PyObject *security = program_item(program, WREATH_POLICY_SECURITY);
        int https = PyUnicode_CompareWithASCIIString(state->scheme, "https") == 0;
        if (append_missing(headers, PyTuple_GET_ITEM(security, https ? 1 : 0)) < 0) return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_CSRF) {
        PyObject *csrf = program_item(program, WREATH_POLICY_CSRF);
        if (state->csrf_minter && append_vary(headers, "sec-fetch-site", 14) < 0) {
            return -1;
        }
        if (state->csrf_issue && state->csrf_token != NULL) {
            Py_ssize_t token_size;
            const char *token = PyUnicode_AsUTF8AndSize(
                state->csrf_token, &token_size);
            if (token == NULL) return -1;
            PyObject *same_site = PyTuple_GET_ITEM(csrf, 6);
            const char *same_data = PyBytes_AS_STRING(same_site);
            Py_ssize_t same_size = PyBytes_GET_SIZE(same_site);
            char max_age[32];
            int max_written = PyOS_snprintf(
                max_age, sizeof(max_age), "%lld",
                PyLong_AsLongLong(PyTuple_GET_ITEM(csrf, 4)));
            if (max_written < 0 || PyErr_Occurred()) return -1;
            PyObject *name = PyTuple_GET_ITEM(csrf, 1);
            Py_ssize_t total = PyBytes_GET_SIZE(name) + 1 + token_size +
                18 + max_written + 11 + same_size +
                (PyTuple_GET_ITEM(csrf, 5) == Py_True ? 8 : 0);
            PyObject *cookie = PyBytes_FromStringAndSize(NULL, total);
            if (cookie == NULL) return -1;
            char *out = PyBytes_AS_STRING(cookie);
            Py_ssize_t at = 0;
#define CSRF_COPY(source, length) do { \
    memcpy(out + at, (source), (size_t)(length)); at += (length); \
} while (0)
            CSRF_COPY(PyBytes_AS_STRING(name), PyBytes_GET_SIZE(name));
            CSRF_COPY("=", 1);
            CSRF_COPY(token, token_size);
            CSRF_COPY("; Path=/; Max-Age=", 18);
            CSRF_COPY(max_age, max_written);
            CSRF_COPY("; SameSite=", 11);
            for (Py_ssize_t i = 0; i < same_size; i++) {
                unsigned char c = (unsigned char)same_data[i];
                out[at++] = (char)(i == 0 && c >= 'a' && c <= 'z'
                    ? c - ('a' - 'A') : c);
            }
            if (PyTuple_GET_ITEM(csrf, 5) == Py_True) {
                CSRF_COPY("; Secure", 8);
            }
#undef CSRF_COPY
            if (at != total) {
                Py_DECREF(cookie);
                PyErr_SetString(PyExc_RuntimeError, "CSRF cookie size invariant failed");
                return -1;
            }
            PyObject *result = PyObject_CallFunctionObjArgs(
                PyTuple_GET_ITEM(csrf, 14), headers,
                PyTuple_GET_ITEM(csrf, 2), cookie, NULL);
            Py_DECREF(cookie);
            if (result == NULL) return -1;
            Py_DECREF(result);
            PyObject *cache_name = PyBytes_FromString("cache-control");
            PyObject *cache_value = PyBytes_FromString("private, no-store");
            int cache_result = cache_name != NULL && cache_value != NULL
                ? replace_header(headers, cache_name, cache_value) : -1;
            Py_XDECREF(cache_name);
            Py_XDECREF(cache_value);
            if (cache_result < 0) return -1;
        }
    }
    if (state->completed & WREATH_POLICY_DONE_CORS) {
        if (cors_egress(state, program_item(program, WREATH_POLICY_CORS), headers) < 0) return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_TIMING) {
        PyObject *timing = program_item(program, WREATH_POLICY_TIMING);
        state->elapsed_ns = policy_now_ns() - state->started_ns;
        if (PyTuple_GET_ITEM(timing, 1) == Py_True) {
            PyObject *name = PyBytes_FromString("server-timing");
            PyObject *metric = PyTuple_GET_ITEM(timing, 0);
            char buffer[128];
            int written = PyOS_snprintf(
                buffer, sizeof(buffer), "%.*s;dur=%.3f",
                (int)PyBytes_GET_SIZE(metric), PyBytes_AS_STRING(metric),
                (double)state->elapsed_ns / 1000000.0);
            PyObject *value = written >= 0 && written < (int)sizeof(buffer)
                ? PyBytes_FromStringAndSize(buffer, written) : NULL;
            if (name == NULL || value == NULL || replace_header(headers, name, value) < 0) {
                Py_XDECREF(name);
                Py_XDECREF(value);
                return -1;
            }
            Py_DECREF(name);
            Py_DECREF(value);
        }
    }
    if (state->completed & WREATH_POLICY_DONE_REQUEST_ID) {
        PyObject *config = program_item(program, WREATH_POLICY_REQUEST_ID);
        if (PyTuple_GET_ITEM(config, 2) == Py_True &&
            replace_header(headers, PyTuple_GET_ITEM(config, 0), state->request_id) < 0) {
            return -1;
        }
    }
    return 0;
}
