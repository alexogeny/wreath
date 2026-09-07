/* First-class HTTP policy executor for Wreath's native server.
 *
 * This is not a middleware interpreter. The descriptor has one fixed schema,
 * execution order is encoded here, and per-request state is a C struct owned by
 * the protocol/stream. Python sees a Request only after ingress has completed.
 */
#include "server_policy.h"
#include "wreathcore.h"
#include "compression_select.h"

#include <math.h>
#include <string.h>
#include <time.h>

#if defined(__linux__)
#include <errno.h>
#include <sys/random.h>
#endif

static PyObject *policy_name_contains;
static PyObject *policy_name_forwarded_client;
static PyObject *policy_name_throttled;
static PyObject *policy_scheme_http;
static PyObject *policy_scheme_https;
static PyObject *policy_header_host;

int
wreath_policy_ready(void)
{
    if (policy_name_contains == NULL &&
        (policy_name_contains = PyUnicode_InternFromString("contains")) == NULL)
        return -1;
    if (policy_name_forwarded_client == NULL &&
        (policy_name_forwarded_client =
            PyUnicode_InternFromString("forwarded_client")) == NULL)
        return -1;
    if (policy_name_throttled == NULL &&
        (policy_name_throttled = PyUnicode_InternFromString("throttled")) == NULL)
        return -1;
    if (policy_scheme_http == NULL &&
        (policy_scheme_http = PyUnicode_InternFromString("http")) == NULL)
        return -1;
    if (policy_scheme_https == NULL &&
        (policy_scheme_https = PyUnicode_InternFromString("https")) == NULL)
        return -1;
    if (policy_header_host == NULL &&
        (policy_header_host = PyBytes_FromString("host")) == NULL)
        return -1;
    return 0;
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
    /* ASGI's canonical path supplies lower-case names.  Let libc handle that
     * overwhelmingly common exact-byte case in wide words, while retaining
     * the case-insensitive fallback for manually constructed responses. */
    if (left == right || memcmp(left, right, (size_t)left_size) == 0) return 1;
    for (Py_ssize_t i = 0; i < left_size; i++) {
        unsigned char a = (unsigned char)left[i];
        unsigned char b = (unsigned char)right[i];
        if (a >= 'A' && a <= 'Z') a = (unsigned char)(a + ('a' - 'A'));
        if (b >= 'A' && b <= 'Z') b = (unsigned char)(b + ('a' - 'A'));
        if (a != b) return 0;
    }
    return 1;
}

static void
trim_ows(const char **data, Py_ssize_t *size)
{
    while (*size > 0 && (**data == ' ' || **data == '\t')) {
        (*data)++;
        (*size)--;
    }
    while (*size > 0 && ((*data)[*size - 1] == ' ' ||
                         (*data)[*size - 1] == '\t')) (*size)--;
}

static int
bytes_equal_ci(PyObject *left, PyObject *right)
{
    if (!PyBytes_Check(left) || !PyBytes_Check(right)) return 0;
    return left == right ||
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
    Py_ssize_t first;
    Py_ssize_t matches;
    if (wreath_headers_find(
            headers, name, name_size, &first,
            count == NULL ? NULL : &matches) < 0) return NULL;
    if (count != NULL) *count = matches;
    return first < 0 ? NULL : wreath_headers_value_borrowed(headers, first);
}

static int
find_header_view(PyObject *headers, const char *name, Py_ssize_t name_size,
                 const char **value, Py_ssize_t *value_size,
                 Py_ssize_t *count)
{
    Py_ssize_t first;
    Py_ssize_t matches;
    if (wreath_headers_find(
            headers, name, name_size, &first,
            count == NULL ? NULL : &matches) < 0) return -1;
    if (count != NULL) *count = matches;
    if (first < 0) return 0;
    const char *found_name;
    Py_ssize_t found_name_size;
    if (wreath_headers_view(
            headers, first, &found_name, &found_name_size,
            value, value_size) < 0) return -1;
    return 1;
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
response_index_literal(PyObject *headers, const char *name, Py_ssize_t size)
{
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *candidate = PyTuple_GET_ITEM(pair, 0);
        if (PyBytes_Check(candidate) &&
            ascii_equal_ci(PyBytes_AS_STRING(candidate), PyBytes_GET_SIZE(candidate),
                           name, size)) return (int)i;
    }
    return -1;
}

static PyObject *
response_value(PyObject *headers, const char *name, Py_ssize_t size)
{
    int index = response_index_literal(headers, name, size);
    return index < 0 ? NULL : PyTuple_GET_ITEM(PyList_GET_ITEM(headers, index), 1);
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
append_literal_value(PyObject *headers, const char *name, Py_ssize_t name_size,
                     PyObject *value)
{
    PyObject *name_obj = PyBytes_FromStringAndSize(name, name_size);
    if (name_obj == NULL) return -1;
    int result = append_pair(headers, name_obj, value);
    Py_DECREF(name_obj);
    return result;
}


static int
set_literal_value(PyObject *headers, Py_ssize_t index,
                  const char *name, Py_ssize_t name_size, PyObject *value)
{
    PyObject *name_obj = PyBytes_FromStringAndSize(name, name_size);
    PyObject *pair = name_obj != NULL ? PyTuple_Pack(2, name_obj, value) : NULL;
    Py_XDECREF(name_obj);
    if (pair == NULL) return -1;
    return PyList_SetItem(headers, index, pair); /* steals */
}


static int
timing_value_is_only_metric(PyObject *value, PyObject *metric)
{
    if (!PyBytes_Check(value) || !PyBytes_Check(metric)) return 0;
    const char *data = PyBytes_AS_STRING(value);
    Py_ssize_t size = PyBytes_GET_SIZE(value);
    trim_ows(&data, &size);
    if (memchr(data, ',', (size_t)size) != NULL) return 0;
    const char *semi = memchr(data, ';', (size_t)size);
    Py_ssize_t name_size = semi == NULL ? size : (Py_ssize_t)(semi - data);
    trim_ows(&data, &name_size);
    return ascii_equal_ci(
        data, name_size, PyBytes_AS_STRING(metric), PyBytes_GET_SIZE(metric));
}


static int
replace_header(PyObject *headers, PyObject *name, PyObject *value)
{
    PyObject *mutable = wreath_headers_materialize(headers);
    if (mutable == NULL) return -1;
    int index = response_header_index(mutable, name);
    PyObject *pair = PyTuple_Pack(2, name, value);
    if (pair == NULL) {
        Py_DECREF(mutable);
        return -1;
    }
    int result;
    if (index >= 0) {
        result = PyList_SetItem(mutable, index, pair); /* steals */
    }
    else {
        result = PyList_Append(mutable, pair);
        Py_DECREF(pair);
    }
    Py_DECREF(mutable);
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
append_vary_tokens(PyObject *headers, const char *const *tokens,
                   const Py_ssize_t *token_sizes, Py_ssize_t token_count)
{
    PyObject *vary_name = PyBytes_FromStringAndSize("vary", 4);
    if (vary_name == NULL) return -1;
    int index = response_header_index(headers, vary_name);
    if (index < 0) {
        Py_ssize_t value_size = token_count > 0 ? (token_count - 1) * 2 : 0;
        for (Py_ssize_t i = 0; i < token_count; i++) {
            if (token_sizes[i] > PY_SSIZE_T_MAX - value_size) {
                Py_DECREF(vary_name);
                return PyErr_NoMemory(), -1;
            }
            value_size += token_sizes[i];
        }
        PyObject *value = PyBytes_FromStringAndSize(NULL, value_size);
        if (value != NULL) {
            char *out = PyBytes_AS_STRING(value);
            for (Py_ssize_t i = 0; i < token_count; i++) {
                if (i != 0) {
                    memcpy(out, ", ", 2);
                    out += 2;
                }
                memcpy(out, tokens[i], (size_t)token_sizes[i]);
                out += token_sizes[i];
            }
        }
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
    unsigned char present[2] = {0, 0};
    int has_value = 0;
    int wildcard = 0;
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
        if (part_size > 0) {
            has_value = 1;
            if (part_size == 1 && *part == '*') wildcard = 1;
            for (Py_ssize_t i = 0; i < token_count; i++) {
                if (ascii_equal_ci(part, part_size, tokens[i], token_sizes[i]))
                    present[i] = 1;
            }
        }
        if (end == size) break;
        start = end + 1;
    }
    if (wildcard) {
        PyObject *star = PyBytes_FromStringAndSize("*", 1);
        int result = star != NULL ? replace_header(headers, vary_name, star) : -1;
        Py_XDECREF(star);
        Py_DECREF(vary_name);
        return result;
    }
    Py_ssize_t missing = 0;
    Py_ssize_t added_size = 0;
    for (Py_ssize_t i = 0; i < token_count; i++) {
        if (present[i]) continue;
        if (token_sizes[i] > PY_SSIZE_T_MAX - added_size) {
            Py_DECREF(vary_name);
            return PyErr_NoMemory(), -1;
        }
        added_size += token_sizes[i];
        missing++;
    }
    if (missing == 0) {
        Py_DECREF(vary_name);
        return 0;
    }
    Py_ssize_t separators = (missing - 1 + has_value) * 2;
    if (size > PY_SSIZE_T_MAX - added_size - separators) {
        Py_DECREF(vary_name);
        return PyErr_NoMemory(), -1;
    }
    Py_ssize_t prefix_size = has_value ? size : 0;
    PyObject *merged = PyBytes_FromStringAndSize(
        NULL, prefix_size + separators + added_size);
    if (merged == NULL) {
        Py_DECREF(vary_name);
        return -1;
    }
    char *out = PyBytes_AS_STRING(merged);
    if (has_value) {
        memcpy(out, data, (size_t)size);
        out += size;
    }
    int needs_separator = has_value;
    for (Py_ssize_t i = 0; i < token_count; i++) {
        if (present[i]) continue;
        if (needs_separator) {
            memcpy(out, ", ", 2);
            out += 2;
        }
        memcpy(out, tokens[i], (size_t)token_sizes[i]);
        out += token_sizes[i];
        needs_separator = 1;
    }
    int result = replace_header(headers, vary_name, merged);
    Py_DECREF(vary_name);
    Py_DECREF(merged);
    return result;
}


static int
append_vary(PyObject *headers, const char *token, Py_ssize_t token_size)
{
    const char *tokens[1] = {token};
    Py_ssize_t token_sizes[1] = {token_size};
    return append_vary_tokens(headers, tokens, token_sizes, 1);
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
    reply->flight_flags = status >= 400 ? WREATH_NFR_FLAG_POLICY_REFUSED : 0;
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


static int
ai_scraping_config_valid(const WreathCoreCAPI *core, PyObject *config)
{
    if (!PyTuple_CheckExact(config) || PyTuple_GET_SIZE(config) != 2 ||
        !core->user_agent_database_check(PyTuple_GET_ITEM(config, 0)) ||
        !PyBytes_CheckExact(PyTuple_GET_ITEM(config, 1))) {
        PyErr_SetString(PyExc_RuntimeError,
                        "invalid native AI scraping policy descriptor");
        return 0;
    }
    PyObject *table = PyTuple_GET_ITEM(config, 1);
    Py_ssize_t size = PyBytes_GET_SIZE(table);
    const unsigned char *data = (const unsigned char *)PyBytes_AS_STRING(table);
    if (size == 0 || (size & 1) != 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native AI scraping rule table must contain uint16 ids");
        return 0;
    }
    uint16_t previous = 0;
    for (Py_ssize_t at = 0; at < size; at += 2) {
        uint16_t current = (uint16_t)(data[at] | ((uint16_t)data[at + 1] << 8));
        if (current == 0 || current <= previous) {
            PyErr_SetString(PyExc_RuntimeError,
                            "native AI scraping rule ids must be nonzero and sorted");
            return 0;
        }
        previous = current;
    }
    return 1;
}

static int
compression_config_valid(PyObject *config)
{
    if (!PyTuple_CheckExact(config) || PyTuple_GET_SIZE(config) != 9 ||
        !PyLong_Check(PyTuple_GET_ITEM(config, 0)) ||
        !PyLong_Check(PyTuple_GET_ITEM(config, 1)) ||
        !PyLong_Check(PyTuple_GET_ITEM(config, 2)) ||
        (PyTuple_GET_ITEM(config, 3) != Py_True &&
         PyTuple_GET_ITEM(config, 3) != Py_False) ||
        !PyCallable_Check(PyTuple_GET_ITEM(config, 4)) ||
        !PyCapsule_CheckExact(PyTuple_GET_ITEM(config, 5)) ||
        !PyTuple_CheckExact(PyTuple_GET_ITEM(config, 6)) ||
        PyTuple_GET_SIZE(PyTuple_GET_ITEM(config, 6)) != 7 ||
        !PyCallable_Check(PyTuple_GET_ITEM(config, 7)) ||
        !PyTuple_CheckExact(PyTuple_GET_ITEM(config, 8)) ||
        PyTuple_GET_SIZE(PyTuple_GET_ITEM(config, 8)) != 7) goto invalid;

    PyObject *dictionaries = PyTuple_GET_ITEM(config, 6);
    PyObject *fragments = PyTuple_GET_ITEM(config, 8);
    for (Py_ssize_t i = 0; i < 7; i++) {
        PyObject *dictionary = PyTuple_GET_ITEM(dictionaries, i);
        if (dictionary != Py_None &&
            (!PyTuple_CheckExact(dictionary) || PyTuple_GET_SIZE(dictionary) != 4 ||
             !PyBytes_CheckExact(PyTuple_GET_ITEM(dictionary, 0)) ||
             !PyBytes_CheckExact(PyTuple_GET_ITEM(dictionary, 1)) ||
             PyBytes_GET_SIZE(PyTuple_GET_ITEM(dictionary, 1)) != 32 ||
             (PyTuple_GET_ITEM(dictionary, 3) != Py_None &&
              !PyCapsule_CheckExact(PyTuple_GET_ITEM(dictionary, 3))))) goto invalid;
        PyObject *fragment = PyTuple_GET_ITEM(fragments, i);
        if (fragment != Py_None &&
            (!PyTuple_CheckExact(fragment) || PyTuple_GET_SIZE(fragment) != 5 ||
             !PyLong_Check(PyTuple_GET_ITEM(fragment, 0)) ||
             !PyLong_Check(PyTuple_GET_ITEM(fragment, 1)) ||
             !PyBytes_CheckExact(PyTuple_GET_ITEM(fragment, 2)) ||
             !PyBytes_CheckExact(PyTuple_GET_ITEM(fragment, 3)) ||
             !PyLong_Check(PyTuple_GET_ITEM(fragment, 4)))) goto invalid;
    }
    return 1;

invalid:
    PyErr_SetString(PyExc_RuntimeError,
                    "invalid native compression policy descriptor");
    return 0;
}

static int
maintenance_config_valid(PyObject *config)
{
    if (!PyTuple_CheckExact(config) || PyTuple_GET_SIZE(config) != 4 ||
        !PyFrozenSet_Check(PyTuple_GET_ITEM(config, 0)) ||
        !PyCallable_Check(PyTuple_GET_ITEM(config, 1)) ||
        !PyTuple_CheckExact(PyTuple_GET_ITEM(config, 2)) ||
        !PyBytes_CheckExact(PyTuple_GET_ITEM(config, 3))) {
        PyErr_SetString(PyExc_RuntimeError,
                        "invalid native maintenance policy descriptor");
        return 0;
    }
    return 1;
}

static int
run_maintenance(PyObject *config, PyObject *path, WreathPolicyReply *reply)
{
    int exempt = PySet_Contains(PyTuple_GET_ITEM(config, 0), path);
    if (exempt < 0) return -1;
    if (exempt) return 0;
    PyObject *allowed = PyObject_CallNoArgs(PyTuple_GET_ITEM(config, 1));
    if (allowed == NULL) return -1;
    int admit = PyObject_IsTrue(allowed);
    Py_DECREF(allowed);
    if (admit < 0) return -1;
    if (admit) return 0;
    reply->status = 503;
    reply->headers = PySequence_List(PyTuple_GET_ITEM(config, 2));
    reply->body = Py_NewRef(PyTuple_GET_ITEM(config, 3));
    if (reply->headers == NULL) {
        Py_CLEAR(reply->body);
        return -1;
    }
    reply->flight_flags |= WREATH_NFR_FLAG_POLICY_REFUSED;
    return 1;
}


static int
run_ai_scraping(const WreathCoreCAPI *core, PyObject *config,
                 PyObject *method, PyObject *path, PyObject *headers,
                 WreathPolicyReply *reply)
{
    if (PyUnicode_CompareWithASCIIString(path, "/robots.txt") == 0 &&
        (PyUnicode_CompareWithASCIIString(method, "GET") == 0 ||
         PyUnicode_CompareWithASCIIString(method, "HEAD") == 0)) return 0;
    const char *user_agent;
    Py_ssize_t user_agent_size;
    int found = find_header_view(
        headers, "user-agent", 10, &user_agent, &user_agent_size, NULL);
    if (found <= 0) return found;
    int blocked = 0;
    if (core->user_agent_blocked_raw(
            PyTuple_GET_ITEM(config, 0), user_agent, user_agent_size,
            PyTuple_GET_ITEM(config, 1), &blocked) < 0) return -1;
    if (!blocked) return 0;
    static const char body[] =
        "{\"type\":\"about:blank\",\"title\":\"Forbidden\",\"status\":403,"
        "\"detail\":\"AI scraper traffic is disabled by default\"}";
    int result = reply_body(reply, 403, "application/problem+json", body,
                            (Py_ssize_t)sizeof(body) - 1);
    if (result > 0) {
        reply->flight_flags |= WREATH_NFR_FLAG_AI_SCRAPING_REFUSED;
    }
    return result;
}


int
wreath_policy_program_load(WreathPolicyProgram *program, PyObject *app)
{
    memset(program, 0, sizeof(*program));
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
        PyUnicode_CompareWithASCIIString(tag, "wreath.http-policy.v5") != 0) {
        PyErr_SetString(PyExc_RuntimeError, "unsupported native HTTP policy descriptor");
        return -1;
    }
    PyObject **lowered[] = {
        &program->proxy,
        &program->trusted_host,
        &program->ai_scraping,
        &program->rate,
        &program->request_id,
        &program->timing,
        &program->cors,
        &program->csrf,
        &program->security,
        &program->websocket_origin,
        &program->cache,
        &program->compression,
        &program->maintenance,
    };
    for (Py_ssize_t index = 1; index < WREATH_POLICY_SIZE; index++) {
        PyObject *value = PyTuple_GET_ITEM(program->descriptor, index);
        *lowered[index - 1] = value == Py_None ? NULL : value;
    }
    if (program->ai_scraping != NULL || program->compression != NULL ||
        program->csrf != NULL || program->timing != NULL) {
        program->core = (const WreathCoreCAPI *)PyCapsule_Import(
            WREATH_CORE_CAPI_NAME, 0);
        if (program->core == NULL) return -1;
        if (program->ai_scraping != NULL &&
            !ai_scraping_config_valid(
                program->core, program->ai_scraping)) return -1;
        if (program->compression != NULL &&
            !compression_config_valid(program->compression)) return -1;
    }
    if (program->maintenance != NULL &&
        !maintenance_config_valid(program->maintenance)) return -1;
    program->response_transform = (unsigned char)(
        program->cache != NULL || program->compression != NULL);
    return 0;
}


void
wreath_policy_program_clear(WreathPolicyProgram *program)
{
    Py_CLEAR(program->descriptor);
    memset(program, 0, sizeof(*program));
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
    Py_CLEAR(state->available_dictionary);
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
    reply->flight_flags = 0;
}


void
wreath_policy_record_completion(const WreathFlightCAPI *flight,
                                wreath_nfr_worker *worker,
                                uint64_t connection_id, uint8_t protocol,
                                uint64_t start_ns, uint64_t end_ns,
                                PyObject *headers,
                                const WreathPolicyReply *reply,
                                uint64_t bytes_in, uint64_t bytes_out)
{
    if (flight == NULL || worker == NULL) return;
    wreath_nfr_context context;
    flight->context_start(worker, &context, connection_id, protocol, start_ns);
    if (context.mode == WREATH_NFR_MODE_OFF) return;
    context.flags |= reply->flight_flags;
    PyObject *traceparent = find_header(headers, "traceparent", 11, NULL);
    if (traceparent != NULL && PyBytes_CheckExact(traceparent)) {
        flight->context_propagate(
            worker, &context, (const uint8_t *)PyBytes_AS_STRING(traceparent),
            PyBytes_GET_SIZE(traceparent));
    }
    flight->context_end(worker, &context, end_ns, (uint32_t)reply->status,
                        WREATH_NFR_TERM_OK, 0, bytes_in, bytes_out);
}


/* Return 1 allowed, 0 refused. Patterns were normalized at construction. */
static int
trusted_host_allowed_data(PyObject *patterns, const char *data, Py_ssize_t size)
{
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
cors_token_char(unsigned char value)
{
    return (value >= '0' && value <= '9') ||
        (value >= 'A' && value <= 'Z') ||
        (value >= 'a' && value <= 'z') ||
        value == '!' || value == '#' || value == '$' || value == '%' ||
        value == '&' || value == '\'' || value == '*' || value == '+' ||
        value == '-' || value == '.' || value == '^' || value == '_' ||
        value == '`' || value == '|' || value == '~';
}


static int
cors_headers_allowed(PyObject *allowed, int allow_all, PyObject *requested)
{
    if (requested == NULL) return 1;
    if (!PyBytes_Check(requested)) return 0;
    const char *data = PyBytes_AS_STRING(requested);
    Py_ssize_t length = PyBytes_GET_SIZE(requested);
    Py_ssize_t start = 0;
    while (start <= length) {
        Py_ssize_t end = start;
        while (end < length && data[end] != ',') end++;
        Py_ssize_t next = end;
        Py_ssize_t token_start = start;
        while (token_start < end && (data[token_start] == ' ' || data[token_start] == '\t'))
            token_start++;
        while (end > token_start && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
        if (token_start == end) return 0;
        for (Py_ssize_t i = token_start; i < end; i++) {
            if (!cors_token_char((unsigned char)data[i])) return 0;
        }
        if (!allow_all) {
            Py_ssize_t token_size = end - token_start;
            PyObject *token = PyBytes_FromStringAndSize(NULL, token_size);
            if (token == NULL) return -1;
            char *normalized = PyBytes_AS_STRING(token);
            for (Py_ssize_t i = 0; i < token_size; i++) {
                normalized[i] = (char)wreath_ascii_lower(
                    (uint8_t)data[token_start + i]);
            }
            int found = PySet_Contains(allowed, token);
            Py_DECREF(token);
            if (found <= 0) return found;
        }
        if (next == length) return 1;
        start = next + 1;
    }
    return 0;
}


static int
cors_preflight(WreathPolicyState *state, PyObject *cors, PyObject *method,
               PyObject *headers, WreathPolicyReply *reply)
{
    Py_ssize_t origin_count = 0;
    PyObject *origin = find_header(headers, "origin", 6, &origin_count);
    if (origin_count == 1) state->origin = Py_NewRef(origin);
    if (PyUnicode_CompareWithASCIIString(method, "OPTIONS") != 0) return 0;
    Py_ssize_t requested_count = 0;
    PyObject *requested = find_header(
        headers, "access-control-request-method", 29, &requested_count);
    if (origin_count > 1 || requested_count > 1) {
        Py_CLEAR(state->origin);
        if (reply_body(reply, 400, "text/plain", "duplicate CORS header", 21) < 0)
            return -1;
        return 1;
    }
    if (origin == NULL || requested == NULL) return 0;
    Py_ssize_t requested_headers_count = 0;
    PyObject *requested_headers = find_header(
        headers, "access-control-request-headers", 30, &requested_headers_count);
    if (requested_headers_count > 1) {
        Py_CLEAR(state->origin);
        if (reply_body(reply, 400, "text/plain", "duplicate CORS header", 21) < 0)
            return -1;
        return 1;
    }
    int allowed_headers = cors_headers_allowed(
        PyTuple_GET_ITEM(cors, 5), PyTuple_GET_ITEM(cors, 6) == Py_True,
        requested_headers);
    if (allowed_headers < 0) return -1;
    if (!allowed_headers) {
        Py_CLEAR(state->origin);
        if (reply_body(reply, 403, "text/plain", "disallowed header", 17) < 0 ||
            append_literal(reply->headers, "vary", "origin") < 0) return -1;
        return 1;
    }
    int allowed_origin = origin_allowed(cors, origin);
    if (allowed_origin < 0) return -1;
    int allowed_method = method_allowed(PyTuple_GET_ITEM(cors, 2), requested);
    if (allowed_method < 0) return -1;
    if (!allowed_method || !allowed_origin) {
        Py_CLEAR(state->origin);
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
    PyObject *old = PyObject_GetAttr(owner, policy_name_throttled);
    PyObject *one = old != NULL ? PyLong_FromLong(1) : NULL;
    PyObject *next = one != NULL ? PyNumber_Add(old, one) : NULL;
    Py_XDECREF(old);
    Py_XDECREF(one);
    if (next == NULL || PyObject_SetAttr(owner, policy_name_throttled, next) < 0) {
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
        ? PyObject_CallMethodOneArg(networks, policy_name_contains, address) : NULL;
    Py_XDECREF(address);
    if (contains == NULL) return -1;
    int trusted = PyObject_IsTrue(contains);
    Py_DECREF(contains);
    if (trusted <= 0) return trusted;

    Py_ssize_t forwarded_count = 0;
    PyObject *forwarded = find_header(
        headers, "x-forwarded-for", 15, &forwarded_count);
    if (forwarded != NULL && forwarded_count == 1) {
        PyObject *client = PyObject_CallMethodOneArg(
            networks, policy_name_forwarded_client, forwarded);
        if (client == NULL) return -1;
        if (client != Py_None) {
            PyObject *effective = wreath_tuple2_from_owned(
                client, Py_NewRef(Py_None));
            if (effective == NULL) return -1;
            Py_SETREF(state->client, effective);
        }
        else {
            Py_DECREF(client);
        }
    }

    if (PyTuple_GET_ITEM(proxy, 1) == Py_True) {
        const char *data = NULL;
        Py_ssize_t size = 0;
        Py_ssize_t proto_count = 0;
        int found = find_header_view(
            headers, "x-forwarded-proto", 17, &data, &size, &proto_count);
        if (found < 0) return -1;
        if (found && proto_count == 1) {
            if (memchr(data, ',', (size_t)size) != NULL) goto proxy_host;
            Py_ssize_t end = 0;
            while (end < size && data[end] != ',') end++;
            Py_ssize_t start = 0;
            while (start < end && (data[start] == ' ' || data[start] == '\t')) start++;
            while (end > start && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
            if (ascii_equal_ci(data + start, end - start, "http", 4) ||
                ascii_equal_ci(data + start, end - start, "https", 5)) {
                PyObject *scheme = Py_NewRef(
                    end - start == 4 ? policy_scheme_http : policy_scheme_https);
                Py_SETREF(state->scheme, scheme);
            }
        }
    }

proxy_host:
    if (PyTuple_GET_ITEM(proxy, 2) == Py_True) {
        const char *data = NULL;
        Py_ssize_t size = 0;
        Py_ssize_t host_count = 0;
        int found = find_header_view(
            headers, "x-forwarded-host", 16, &data, &size, &host_count);
        if (found < 0) return -1;
        if (found && host_count == 1) {
            if (memchr(data, ',', (size_t)size) != NULL) return 0;
            Py_ssize_t end = 0;
            while (end < size && data[end] != ',') end++;
            Py_ssize_t start = 0;
            while (start < end && (data[start] == ' ' || data[start] == '\t')) start++;
            while (end > start && (data[end - 1] == ' ' || data[end - 1] == '\t')) end--;
            if (end > start) {
                PyObject *value = PyBytes_FromStringAndSize(data + start, end - start);
                int result = value != NULL
                    ? replace_header(headers, policy_header_host, value) : -1;
                Py_XDECREF(value);
                if (result < 0) return -1;
            }
        }
    }
    return 0;
}


static PyObject *
cookie_value(PyObject *headers, PyObject *wanted, int *ambiguous)
{
    if (ambiguous != NULL) *ambiguous = 0;
    PyObject *found = NULL;
    const char *wanted_data = PyBytes_AS_STRING(wanted);
    Py_ssize_t wanted_size = PyBytes_GET_SIZE(wanted);
    Py_ssize_t count = wreath_headers_count(headers);
    if (count < 0) return NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        const char *name;
        const char *data;
        Py_ssize_t name_size;
        Py_ssize_t size;
        if (wreath_headers_view(headers, i, &name, &name_size,
                                &data, &size) < 0) {
            Py_XDECREF(found);
            return NULL;
        }
        if (name_size != 6 || memcmp(name, "cookie", 6) != 0) continue;
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
                if (ambiguous == NULL) {
                    return PyUnicode_DecodeLatin1(
                        data + value_start, end - value_start, NULL);
                }
                if (found != NULL) {
                    Py_DECREF(found);
                    *ambiguous = 1;
                    return NULL;
                }
                found = PyUnicode_DecodeLatin1(
                    data + value_start, end - value_start, NULL);
                if (found == NULL) return NULL;
            }
            if (separator == NULL) break;
            start = separator_at + 1;
        }
    }
    return found;
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


typedef struct {
    PyObject *host;
    PyObject *origin;
    PyObject *referer;
    PyObject *site;
    PyObject *submitted;
    Py_ssize_t origin_count;
    Py_ssize_t referer_count;
    Py_ssize_t site_count;
    Py_ssize_t submitted_count;
} CsrfRequestHeaders;


static int
csrf_record_header(PyObject *headers, Py_ssize_t index,
                   PyObject **first, Py_ssize_t *matches)
{
    (*matches)++;
    if (*first != NULL) return 0;
    *first = wreath_headers_value_borrowed(headers, index);
    return *first == NULL ? -1 : 0;
}


static int
csrf_request_headers(PyObject *headers, PyObject *submitted_name,
                     CsrfRequestHeaders *found)
{
    memset(found, 0, sizeof(*found));
    const char *submitted_data = PyBytes_AS_STRING(submitted_name);
    Py_ssize_t submitted_size = PyBytes_GET_SIZE(submitted_name);
    Py_ssize_t count = wreath_headers_count(headers);
    if (count < 0) return -1;
    for (Py_ssize_t i = 0; i < count; i++) {
        const char *name;
        const char *value;
        Py_ssize_t name_size;
        Py_ssize_t value_size;
        if (wreath_headers_view(headers, i, &name, &name_size,
                                &value, &value_size) < 0) return -1;
        if (ascii_equal_ci(name, name_size, "host", 4)) {
            if (found->host == NULL) {
                found->host = wreath_headers_value_borrowed(headers, i);
                if (found->host == NULL) return -1;
            }
        }
        if (ascii_equal_ci(name, name_size, "origin", 6)) {
            if (csrf_record_header(headers, i, &found->origin,
                                   &found->origin_count) < 0) return -1;
        }
        if (ascii_equal_ci(name, name_size, "referer", 7)) {
            if (csrf_record_header(headers, i, &found->referer,
                                   &found->referer_count) < 0) return -1;
        }
        if (ascii_equal_ci(name, name_size, "sec-fetch-site", 14)) {
            if (csrf_record_header(headers, i, &found->site,
                                   &found->site_count) < 0) return -1;
        }
        if (ascii_equal_ci(name, name_size, submitted_data, submitted_size)) {
            if (csrf_record_header(headers, i, &found->submitted,
                                   &found->submitted_count) < 0) return -1;
        }
    }
    return 0;
}


static int
csrf_has_duplicate_security_header(const CsrfRequestHeaders *headers)
{
    return headers->origin_count > 1 || headers->referer_count > 1 ||
        headers->site_count > 1 ||
        headers->submitted_count > 1;
}


static int
csrf_origin_valid(WreathPolicyState *state, PyObject *csrf,
                  const CsrfRequestHeaders *headers)
{
    PyObject *host = headers->host;
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
    PyObject *candidate = headers->origin;
    PyObject *owned_candidate = NULL;
    if (candidate == NULL) {
        PyObject *referer = headers->referer;
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
    CsrfRequestHeaders request_headers;
    if (csrf_request_headers(
            headers, PyTuple_GET_ITEM(csrf, 3), &request_headers) < 0) return -1;
    if (csrf_has_duplicate_security_header(&request_headers)) {
        return reply_problem(reply, 403);
    }
    int safe = PyUnicode_CompareWithASCIIString(method, "GET") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "HEAD") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "OPTIONS") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "QUERY") == 0;
    PyObject *site = request_headers.site;
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
    int ambiguous_cookie = 0;
    PyObject *cookie = cookie_value(
        headers, PyTuple_GET_ITEM(csrf, 1), safe ? NULL : &ambiguous_cookie);
    if (cookie == NULL && PyErr_Occurred()) return -1;
    if (!safe && ambiguous_cookie) return reply_problem(reply, 403);
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
    PyObject *submitted = request_headers.submitted;
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
    int origin_valid = valid ? csrf_origin_valid(state, csrf, &request_headers) : 0;
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
                      PyObject *method, PyObject *path, PyObject *scheme,
                      PyObject *client, PyObject *headers,
                      WreathPolicyReply *reply)
{
    wreath_policy_state_clear(state);
    memset(reply, 0, sizeof(*reply));
    if (program->descriptor == NULL) return 0;
    state->native = 1;
    state->method_is_head = (unsigned char)(
        PyUnicode_CompareWithASCIIString(method, "HEAD") == 0);
    state->client = Py_NewRef(client);
    state->scheme = Py_NewRef(scheme);
    if (program->compression != NULL) {
        PyObject *compression = program->compression;
        const char *accepted = NULL;
        Py_ssize_t accepted_size = 0;
        int accepted_found = find_header_view(
            headers, "accept-encoding", 15, &accepted, &accepted_size, NULL);
        if (accepted_found < 0) return -1;
        int fallback = 0;
        int allow_dcz = PyTuple_CheckExact(compression) &&
            PyTuple_GET_SIZE(compression) >= 9 &&
            PyTuple_CheckExact(PyTuple_GET_ITEM(compression, 6));
        int coding = wreath_select_compression_data(
            accepted, accepted_size, allow_dcz, &fallback);
        if (coding < 0) return -1;
        state->compression_coding = (unsigned char)coding;
        state->compression_fallback = (unsigned char)fallback;
        if (state->compression_coding == 3) {
            Py_ssize_t count = 0;
            PyObject *available = find_header(
                headers, "available-dictionary", 20, &count);
            if (count == 1 && available != NULL) {
                state->available_dictionary = Py_NewRef(available);
            }
        }
    }
    if (program->security != NULL) {
        state->completed |= WREATH_POLICY_DONE_SECURITY;
    }

    PyObject *proxy = program->proxy;
    if (proxy != NULL) {
        if (run_proxy(state, proxy, headers) < 0) return -1;
        state->completed |= WREATH_POLICY_DONE_PROXY;
    }

    PyObject *trusted = program->trusted_host;
    if (trusted != NULL) {
        const char *host = NULL;
        Py_ssize_t host_size = 0;
        Py_ssize_t count = 0;
        int found = find_header_view(
            headers, "host", 4, &host, &host_size, &count);
        if (found < 0) return -1;
        state->completed |= WREATH_POLICY_DONE_TRUSTED_HOST;
        if (count != 1 || !trusted_host_allowed_data(trusted, host, host_size)) {
            return reply_problem(reply, 400);
        }
    }

    PyObject *maintenance = program->maintenance;
    if (maintenance != NULL) {
        int result = run_maintenance(maintenance, path, reply);
        if (result != 0) return result;
    }

    PyObject *ai_scraping = program->ai_scraping;
    if (ai_scraping != NULL) {
        int result = run_ai_scraping(
            program->core, ai_scraping, method, path, headers, reply);
        if (result != 0) return result;
    }

    PyObject *rate = program->rate;
    if (rate != NULL) {
        int result = run_rate(state, rate, reply);
        state->completed |= WREATH_POLICY_DONE_RATE;
        if (result != 0) return result;
    }

    PyObject *request_id = program->request_id;
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

    PyObject *timing = program->timing;
    if (timing != NULL) {
        state->started_ns = policy_now_ns();
        state->completed |= WREATH_POLICY_DONE_TIMING;
    }

    PyObject *cors = program->cors;
    if (cors != NULL) {
        int result = cors_preflight(state, cors, method, headers, reply);
        state->completed |= WREATH_POLICY_DONE_CORS;
        if (result != 0) return result;
    }

    PyObject *csrf = program->csrf;
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
    PyObject *config = program->websocket_origin;
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

static int
compressible_type(PyObject *value)
{
    if (value == NULL || !PyBytes_Check(value)) return 0;
    const char *media = PyBytes_AS_STRING(value);
    Py_ssize_t size = 0, total = PyBytes_GET_SIZE(value);
    while (size < total && media[size] != ';') size++;
    trim_ows(&media, &size);
    if (size >= 5 && ascii_equal_ci(media, 5, "text/", 5)) return 1;
    static const char *exact[] = {
        "application/json", "application/problem+json", "application/javascript",
        "application/xml", "image/svg+xml"
    };
    for (size_t i = 0; i < sizeof(exact) / sizeof(exact[0]); i++) {
        Py_ssize_t exact_size = (Py_ssize_t)strlen(exact[i]);
        if (ascii_equal_ci(media, size, exact[i], exact_size)) return 1;
    }
    return size > 12 && ascii_equal_ci(media, 12, "application/", 12) &&
        ((size >= 5 && ascii_equal_ci(media + size - 5, 5, "+json", 5)) ||
         (size >= 4 && ascii_equal_ci(media + size - 4, 4, "+xml", 4)));
}

static int
has_cache_token(PyObject *value, const char *token, Py_ssize_t token_size)
{
    if (value == NULL || !PyBytes_Check(value)) return 0;
    const char *data = PyBytes_AS_STRING(value);
    Py_ssize_t size = PyBytes_GET_SIZE(value), start = 0;
    for (Py_ssize_t i = 0; i <= size; i++) {
        if (i < size && data[i] != ',') continue;
        const char *part = data + start;
        Py_ssize_t part_size = i - start;
        start = i + 1;
        trim_ows(&part, &part_size);
        Py_ssize_t equals = 0;
        while (equals < part_size && part[equals] != '=') equals++;
        part_size = equals;
        trim_ows(&part, &part_size);
        if (ascii_equal_ci(part, part_size, token, token_size)) return 1;
    }
    return 0;
}

static int
replace_literal_header(PyObject *headers, const char *name, Py_ssize_t name_size,
                       PyObject *value)
{
    PyObject *name_obj = PyBytes_FromStringAndSize(name, name_size);
    if (name_obj == NULL) return -1;
    int result = replace_header(headers, name_obj, value);
    Py_DECREF(name_obj);
    return result;
}

static PyObject *
encoded_etag(PyObject *etag, int coding)
{
    if (etag == NULL) return NULL;
    if (!PyBytes_Check(etag)) return Py_NewRef(Py_None);
    const char *data = PyBytes_AS_STRING(etag);
    Py_ssize_t size = PyBytes_GET_SIZE(etag), prefix = 0;
    if (size >= 2 && data[0] == 'W' && data[1] == '/') prefix = 2;
    if (size - prefix < 2 || data[prefix] != '"' || data[size - 1] != '"')
        return Py_NewRef(Py_None);
    const char *suffix = coding == 3 ? "--dcz" : (coding == 2 ? "--zstd" : "--gzip");
    Py_ssize_t suffix_size = coding == 3 ? 5 : 6;
    PyObject *result = PyBytes_FromStringAndSize(NULL, size + suffix_size);
    if (result == NULL) return NULL;
    char *out = PyBytes_AS_STRING(result);
    memcpy(out, data, (size_t)(size - 1));
    memcpy(out + size - 1, suffix, (size_t)suffix_size);
    out[size - 1 + suffix_size] = '"';
    return result;
}

static PyObject *
matching_dcz_dictionary(WreathPolicyProgram *program, PyObject *compression,
                        PyObject *available, PyObject *content_type)
{
    if (available == NULL || !PyBytes_Check(available) ||
        !PyTuple_CheckExact(compression) || PyTuple_GET_SIZE(compression) < 9)
        return NULL;
    const char *token = PyBytes_AS_STRING(available);
    Py_ssize_t token_size = PyBytes_GET_SIZE(available);
    trim_ows(&token, &token_size);
    PyObject *table = PyTuple_GET_ITEM(compression, 6);
    if (!PyTuple_CheckExact(table) || PyTuple_GET_SIZE(table) != 7) return NULL;
    int format;
    if (program->core->gzip_format(content_type, &format) < 0) return NULL;
    PyObject *entry = PyTuple_GET_ITEM(table, format);
    if (entry == Py_None || !PyTuple_CheckExact(entry) ||
        PyTuple_GET_SIZE(entry) != 4) return NULL;
    PyObject *expected = PyTuple_GET_ITEM(entry, 0);
    return PyBytes_Check(expected) && PyBytes_GET_SIZE(expected) == token_size &&
        memcmp(PyBytes_AS_STRING(expected), token, (size_t)token_size) == 0
        ? entry : NULL;
}

static PyObject *
compress_dcz(PyObject *compressor, PyObject *dictionary, PyObject *body,
             PyObject *level)
{
    PyObject *arguments[3] = {dictionary, body, level};
    PyObject *payload = PyObject_Vectorcall(compressor, arguments, 3, NULL);
    if (payload == NULL || PyBytes_CheckExact(payload)) return payload;
    Py_DECREF(payload);
    PyErr_SetString(PyExc_RuntimeError,
                    "native DCZ compressor did not return bytes");
    return NULL;
}

int
wreath_policy_response(WreathPolicyProgram *program, WreathPolicyState *state,
                       PyObject *status_obj, PyObject *headers, PyObject **body,
                       int authenticated)
{
    if (program->descriptor == NULL || !state->native) return 0;
    if (!PyList_Check(headers) || body == NULL || !PyBytes_Check(*body)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native response policy requires list headers and bytes body");
        return -1;
    }
    PyObject *cache = program->cache;
    if (cache != NULL && response_index_literal(headers, "cache-control", 13) < 0) {
        PyObject *value = PyTuple_GET_ITEM(cache, 0);
        if (PyTuple_GET_ITEM(cache, 1) == Py_True &&
            response_index_literal(headers, "set-cookie", 10) >= 0) {
            if (append_literal(
                    headers, "cache-control", "private, no-store") < 0) return -1;
        }
        else {
            PyObject *name = PyBytes_FromString("cache-control");
            int result = name != NULL ? append_pair(headers, name, value) : -1;
            Py_XDECREF(name);
            if (result < 0) return -1;
        }
    }
    PyObject *compression = program->compression;
    if (compression == NULL || state->compression_coding == 0 || state->method_is_head ||
        (authenticated && PyTuple_GET_ITEM(compression, 3) != Py_True)) return 0;
    long status = PyLong_AsLong(status_obj);
    if ((status == -1 && PyErr_Occurred()) || status == 204 || status == 304 ||
        status == 206) return PyErr_Occurred() ? -1 : 0;
    Py_ssize_t minimum = PyLong_AsSsize_t(PyTuple_GET_ITEM(compression, 0));
    if (minimum == -1 && PyErr_Occurred()) return -1;
    Py_ssize_t body_size = PyObject_Length(*body);
    if (body_size < 0) return -1;
    if (body_size < minimum) return 0;
    PyObject *content_type = response_value(headers, "content-type", 12);
    if (response_index_literal(headers, "content-encoding", 16) >= 0 ||
        response_index_literal(headers, "content-range", 13) >= 0 ||
        response_index_literal(headers, "content-digest", 14) >= 0 ||
        response_index_literal(headers, "repr-digest", 11) >= 0 ||
        !compressible_type(content_type) ||
        has_cache_token(response_value(headers, "cache-control", 13),
                        "no-transform", 12)) return 0;
    int coding = state->compression_coding;
    PyObject *dcz_dictionary = NULL;
    if (coding == 3) {
        int secure = state->scheme != NULL &&
            PyUnicode_Compare(state->scheme, policy_scheme_https) == 0;
        if (secure) {
            dcz_dictionary = matching_dcz_dictionary(
                program, compression, state->available_dictionary, content_type);
        }
        if (dcz_dictionary == NULL) coding = state->compression_fallback;
        if (coding == 0) return 0;
    }
    PyObject *etag = response_value(headers, "etag", 4);
    PyObject *new_etag = encoded_etag(etag, coding);
    if (new_etag == NULL && etag != NULL) return -1;
    if (new_etag == Py_None) {
        Py_DECREF(new_etag);
        return 0;
    }
    PyObject *level = PyTuple_GET_ITEM(
        compression, coding == 2 || coding == 3 ? 2 : 1);
    PyObject *compressed;
    if (coding == 3) {
        compressed = compress_dcz(
            PyTuple_GET_ITEM(compression, 7),
            dcz_dictionary, *body, level);
    }
    else if (coding == 2) {
        PyObject *compressor = PyTuple_GET_ITEM(compression, 4);
        PyObject *arguments[2] = {*body, level};
        compressed = PyObject_Vectorcall(compressor, arguments, 2, NULL);
    }
    else {
        long gzip_level = PyLong_AsLong(level);
        if (gzip_level == -1 && PyErr_Occurred()) {
            Py_XDECREF(new_etag);
            return -1;
        }
        compressed = program->core->gzip_fragment_compress(
            PyTuple_GET_ITEM(compression, 5), *body, (int)gzip_level,
            content_type, PyTuple_GET_ITEM(compression, 8));
    }
    if (compressed == NULL) {
        Py_XDECREF(new_etag);
        return -1;
    }
    Py_SETREF(*body, compressed);
    PyObject *length = PyBytes_FromFormat("%zd", PyBytes_GET_SIZE(*body));
    if (length == NULL || replace_literal_header(
            headers, "content-length", 14, length) < 0) {
        Py_XDECREF(length);
        Py_XDECREF(new_etag);
        return -1;
    }
    Py_DECREF(length);
    const char *coding_name = coding == 3 ? "dcz" : (coding == 2 ? "zstd" : "gzip");
    int vary_result;
    if (coding == 3) {
        const char *vary_tokens[2] = {"accept-encoding", "available-dictionary"};
        Py_ssize_t vary_sizes[2] = {15, 20};
        vary_result = append_vary_tokens(headers, vary_tokens, vary_sizes, 2);
    }
    else
        vary_result = append_vary(headers, "accept-encoding", 15);
    if (append_literal(headers, "content-encoding", coding_name) < 0 ||
        vary_result < 0 ||
        (new_etag != NULL && replace_literal_header(headers, "etag", 4, new_etag) < 0)) {
        Py_XDECREF(new_etag);
        return -1;
    }
    Py_XDECREF(new_etag);
    return 0;
}


int
wreath_policy_egress(WreathPolicyProgram *program, WreathPolicyState *state,
                     PyObject *headers)
{
    if (program->descriptor == NULL || !state->native) return 0;
    const uint32_t egress = WREATH_POLICY_DONE_REQUEST_ID |
        WREATH_POLICY_DONE_TIMING | WREATH_POLICY_DONE_CORS |
        WREATH_POLICY_DONE_CSRF | WREATH_POLICY_DONE_SECURITY;
    if ((state->completed & egress) == 0) return 0;
    if (!PyList_Check(headers)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native HTTP policy requires a mutable response header list");
        return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_SECURITY) {
        PyObject *security = program->security;
        int https = PyUnicode_CompareWithASCIIString(state->scheme, "https") == 0;
        if (append_missing(headers, PyTuple_GET_ITEM(security, https ? 1 : 0)) < 0) return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_CSRF) {
        PyObject *csrf = program->csrf;
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
            int result = program->core->replace_cookie(
                headers, PyTuple_GET_ITEM(csrf, 2), cookie);
            Py_DECREF(cookie);
            if (result < 0) return -1;
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
        if (cors_egress(state, program->cors, headers) < 0) return -1;
    }
    if (state->completed & WREATH_POLICY_DONE_TIMING) {
        PyObject *timing = program->timing;
        state->elapsed_ns = policy_now_ns() - state->started_ns;
        if (PyTuple_GET_ITEM(timing, 1) == Py_True) {
            PyObject *metric = PyTuple_GET_ITEM(timing, 0);
            char buffer[128];
            int written = PyOS_snprintf(
                buffer, sizeof(buffer), "%.*s;dur=%.3f",
                (int)PyBytes_GET_SIZE(metric), PyBytes_AS_STRING(metric),
                (double)state->elapsed_ns / 1000000.0);
            PyObject *value = written >= 0 && written < (int)sizeof(buffer)
                ? PyBytes_FromStringAndSize(buffer, written) : NULL;
            int timing_result = -1;
            if (value != NULL) {
                int timing_index = response_index_literal(
                    headers, "server-timing", 13);
                if (timing_index < 0) {
                    timing_result = append_literal_value(
                        headers, "server-timing", 13, value);
                }
                else {
                    int unique = 1;
                    for (Py_ssize_t i = timing_index + 1;
                         i < PyList_GET_SIZE(headers); i++) {
                        PyObject *pair = PyList_GET_ITEM(headers, i);
                        PyObject *name = PyTuple_GET_ITEM(pair, 0);
                        if (PyBytes_Check(name) && ascii_equal_ci(
                                PyBytes_AS_STRING(name), PyBytes_GET_SIZE(name),
                                "server-timing", 13)) {
                            unique = 0;
                            break;
                        }
                    }
                    PyObject *old = PyTuple_GET_ITEM(
                        PyList_GET_ITEM(headers, timing_index), 1);
                    timing_result = unique && timing_value_is_only_metric(old, metric)
                        ? set_literal_value(
                            headers, timing_index, "server-timing", 13, value)
                        : program->core->replace_server_timing(
                            headers, metric, value);
                }
            }
            if (timing_result < 0) {
                Py_XDECREF(value);
                return -1;
            }
            Py_DECREF(value);
        }
    }
    if (state->completed & WREATH_POLICY_DONE_REQUEST_ID) {
        PyObject *config = program->request_id;
        if (PyTuple_GET_ITEM(config, 2) == Py_True &&
            replace_header(headers, PyTuple_GET_ITEM(config, 0), state->request_id) < 0) {
            return -1;
        }
    }
    return 0;
}
