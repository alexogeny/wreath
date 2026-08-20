/* Whole-record outbound HTTP replay codec.
 *
 * The Python boundary supplies one exchange or receives one exchange.  Header
 * traversal, redaction, framing, bounds checks, and cursor state remain in this
 * operation-owned C state; no partially encoded header lists cross back into
 * Python.
 */
#include "wreathcore.h"

#include <limits.h>

#define HTTP_V1_HEAD_SIZE 28
#define HTTP_V2_HEAD_SIZE 36
#define HTTP_HEADER_SIZE 8
#define HTTP_HEADERS_REDACTED 1U

typedef struct {
    char *data;
    Py_ssize_t length;
} ForbiddenHeader;

typedef struct {
    ForbiddenHeader *items;
    Py_ssize_t count;
} HeaderPolicy;

typedef struct {
    PyObject *name;
    PyObject *value;
} ReplayHeader;

typedef struct {
    ReplayHeader *items;
    Py_ssize_t count;
    uint64_t wire_size;
    int redacted;
} ReplayHeaders;

typedef enum {
    REPLAY_ATTR_METHOD,
    REPLAY_ATTR_TARGET,
    REPLAY_ATTR_HTTP_VERSION,
    REPLAY_ATTR_REASON,
    REPLAY_ATTR_IDEMPOTENCY_KEY,
    REPLAY_ATTR_REQUEST_BODY,
    REPLAY_ATTR_RESPONSE_BODY,
    REPLAY_ATTR_REQUEST_HEADERS,
    REPLAY_ATTR_RESPONSE_HEADERS,
    REPLAY_ATTR_DEPENDENCY_ID,
    REPLAY_ATTR_RESPONSE_STATUS,
    REPLAY_ATTR_SEQUENCE,
    REPLAY_ATTR_HEADERS_REDACTED,
    REPLAY_ATTR_UPPER,
    REPLAY_ATTR_COUNT
} ReplayAttr;

static PyObject *replay_attrs[REPLAY_ATTR_COUNT];

int
wreath_http_replay_ready(void)
{
    static const char *const names[REPLAY_ATTR_COUNT] = {
        "method", "target", "http_version", "reason", "idempotency_key",
        "request_body", "response_body", "request_headers", "response_headers",
        "dependency_id", "response_status", "sequence", "headers_redacted",
        "upper"
    };
    for (Py_ssize_t i = 0; i < REPLAY_ATTR_COUNT; i++) {
        if (replay_attrs[i] == NULL &&
            (replay_attrs[i] = PyUnicode_InternFromString(names[i])) == NULL)
            return -1;
    }
    return 0;
}

static inline PyObject *
replay_getattr(PyObject *exchange, ReplayAttr attribute)
{
    return PyObject_GetAttr(exchange, replay_attrs[attribute]);
}

static void
header_policy_clear(HeaderPolicy *policy)
{
    for (Py_ssize_t i = 0; i < policy->count; i++)
        PyMem_Free(policy->items[i].data);
    PyMem_Free(policy->items);
    policy->items = NULL;
    policy->count = 0;
}

static int
header_policy_init(HeaderPolicy *policy, PyObject *source)
{
    policy->items = NULL;
    policy->count = 0;
    if (!PyAnySet_Check(source)) {
        PyErr_SetString(PyExc_TypeError,
                        "HTTP replay forbidden headers must be a set");
        return -1;
    }
    PyObject *iterator = PyObject_GetIter(source);
    if (iterator == NULL) return -1;
    Py_ssize_t capacity = PySet_Size(source);
    if (capacity < 0) {
        Py_DECREF(iterator);
        return -1;
    }
    if (capacity != 0) {
        policy->items = PyMem_Calloc((size_t)capacity, sizeof(*policy->items));
        if (policy->items == NULL) {
            Py_DECREF(iterator);
            PyErr_NoMemory();
            return -1;
        }
    }
    PyObject *item;
    while ((item = PyIter_Next(iterator)) != NULL) {
        if (!PyBytes_Check(item)) {
            Py_DECREF(item);
            Py_DECREF(iterator);
            header_policy_clear(policy);
            PyErr_SetString(PyExc_TypeError,
                            "HTTP replay forbidden headers must be bytes");
            return -1;
        }
        Py_ssize_t length = PyBytes_GET_SIZE(item);
        char *copy = PyMem_Malloc((size_t)(length == 0 ? 1 : length));
        if (copy == NULL) {
            Py_DECREF(item);
            Py_DECREF(iterator);
            header_policy_clear(policy);
            PyErr_NoMemory();
            return -1;
        }
        /* complexity: allow CL-LINEAR-IN-LOOP -- disjoint policy names; total copied bytes is linear */
        memcpy(copy, PyBytes_AS_STRING(item), (size_t)length);
        policy->items[policy->count++] = (ForbiddenHeader){copy, length};
        Py_DECREF(item);
    }
    Py_DECREF(iterator);
    if (PyErr_Occurred()) {
        header_policy_clear(policy);
        return -1;
    }
    return 0;
}

static int
header_is_forbidden(const HeaderPolicy *policy, const char *name,
                    Py_ssize_t length)
{
    for (Py_ssize_t i = 0; i < policy->count; i++) {
        if (wreath_ascii_equal_ci(name, length, policy->items[i].data,
                                  policy->items[i].length)) return 1;
    }
    return 0;
}

static PyObject *
replay_bytes(PyObject *value)
{
    if (PyBytes_CheckExact(value)) return Py_NewRef(value);
    return PyObject_CallOneArg((PyObject *)&PyBytes_Type, value);
}

static void
replay_headers_clear(ReplayHeaders *headers)
{
    for (Py_ssize_t i = 0; i < headers->count; i++) {
        Py_XDECREF(headers->items[i].name);
        Py_XDECREF(headers->items[i].value);
    }
    PyMem_Free(headers->items);
    headers->items = NULL;
    headers->count = 0;
}

static int
replay_headers_init(ReplayHeaders *headers, PyObject *source,
                    const HeaderPolicy *policy, PyObject *error_type)
{
    headers->items = NULL;
    headers->count = 0;
    headers->wire_size = 0;
    headers->redacted = 0;
    PyObject *sequence = PySequence_Fast(source,
        "outbound exchange headers must be an iterable of pairs");
    if (sequence == NULL) return -1;
    Py_ssize_t source_count = PySequence_Fast_GET_SIZE(sequence);
    if (source_count != 0) {
        headers->items = PyMem_Calloc(
            (size_t)source_count, sizeof(*headers->items));
        if (headers->items == NULL) {
            Py_DECREF(sequence);
            PyErr_NoMemory();
            return -1;
        }
    }
    for (Py_ssize_t i = 0; i < source_count; i++) {
        PyObject *pair = PySequence_Fast(
            PySequence_Fast_GET_ITEM(sequence, i),
            "outbound exchange header must be a pair");
        if (pair == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(pair) != 2) {
            Py_DECREF(pair);
            PyErr_SetString(PyExc_ValueError,
                            "outbound exchange header must contain two values");
            goto error;
        }
        PyObject *name = replay_bytes(PySequence_Fast_GET_ITEM(pair, 0));
        PyObject *value = name == NULL ? NULL
            : replay_bytes(PySequence_Fast_GET_ITEM(pair, 1));
        Py_DECREF(pair);
        if (value == NULL) {
            Py_XDECREF(name);
            goto error;
        }
        Py_ssize_t name_length = PyBytes_GET_SIZE(name);
        Py_ssize_t value_length = PyBytes_GET_SIZE(value);
        if ((uint64_t)name_length > UINT32_MAX ||
            (uint64_t)value_length > UINT32_MAX) {
            Py_DECREF(name);
            Py_DECREF(value);
            PyErr_SetString(error_type,
                "outbound exchange header exceeds the 4 GiB wire limit");
            goto error;
        }
        if (header_is_forbidden(policy, PyBytes_AS_STRING(name), name_length)) {
            headers->redacted = 1;
            Py_DECREF(name);
            Py_DECREF(value);
            continue;
        }
        uint64_t addition = HTTP_HEADER_SIZE + (uint64_t)name_length +
                            (uint64_t)value_length;
        if (headers->wire_size > UINT64_MAX - addition) {
            Py_DECREF(name);
            Py_DECREF(value);
            PyErr_NoMemory();
            goto error;
        }
        headers->wire_size += addition;
        headers->items[headers->count++] = (ReplayHeader){name, value};
    }
    Py_DECREF(sequence);
    return 0;
error:
    Py_DECREF(sequence);
    replay_headers_clear(headers);
    return -1;
}

static int
read_short_attr(PyObject *exchange, ReplayAttr attribute, const char *label,
                uint16_t *out, PyObject *error_type)
{
    PyObject *object = replay_getattr(exchange, attribute);
    if (object == NULL) return -1;
    int overflow = 0;
    long long value = PyLong_AsLongLongAndOverflow(object, &overflow);
    Py_DECREF(object);
    if (overflow || (value == -1 && PyErr_Occurred())) {
        PyErr_Clear();
        PyErr_Format(error_type, "outbound exchange %s exceeds 65535", label);
        return -1;
    }
    if (value < 0 || value > UINT16_MAX) {
        PyErr_Format(error_type, "outbound exchange %s exceeds 65535", label);
        return -1;
    }
    *out = (uint16_t)value;
    return 0;
}

static PyObject *
encoded_text_attr(PyObject *exchange, ReplayAttr attribute, const char *encoding,
                  int uppercase)
{
    PyObject *text = replay_getattr(exchange, attribute);
    if (text == NULL) return NULL;
    if (uppercase) {
        PyObject *upper = PyObject_CallMethodNoArgs(
            text, replay_attrs[REPLAY_ATTR_UPPER]);
        Py_DECREF(text);
        text = upper;
        if (text == NULL) return NULL;
    }
    PyObject *encoded = PyUnicode_AsEncodedString(text, encoding, "strict");
    Py_DECREF(text);
    return encoded;
}

static int
check_short_length(Py_ssize_t length, const char *label, PyObject *error_type)
{
    if ((uint64_t)length <= UINT16_MAX) return 0;
    PyErr_Format(error_type, "outbound exchange %s exceeds 65535", label);
    return -1;
}

static void
write_replay_headers(uint8_t **cursor, const ReplayHeaders *headers)
{
    for (Py_ssize_t i = 0; i < headers->count; i++) {
        PyObject *name = headers->items[i].name;
        PyObject *value = headers->items[i].value;
        uint32_t name_length = (uint32_t)PyBytes_GET_SIZE(name);
        uint32_t value_length = (uint32_t)PyBytes_GET_SIZE(value);
        wreath_store_u32_le(*cursor, name_length);
        wreath_store_u32_le(*cursor + 4, value_length);
        *cursor += HTTP_HEADER_SIZE;
        /* complexity: allow CL-LINEAR-IN-LOOP -- disjoint header fields; total output bytes is linear */
        memcpy(*cursor, PyBytes_AS_STRING(name), name_length);
        *cursor += name_length;
        /* complexity: allow CL-LINEAR-IN-LOOP -- disjoint header fields; total output bytes is linear */
        memcpy(*cursor, PyBytes_AS_STRING(value), value_length);
        *cursor += value_length;
    }
}

PyObject *
wreath_http_exchange_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *exchange, *forbidden, *error_type;
    if (!PyArg_ParseTuple(args, "OOO:http_exchange_encode",
                          &exchange, &forbidden, &error_type)) return NULL;
    HeaderPolicy policy;
    ReplayHeaders request_headers = {0}, response_headers = {0};
    PyObject *method = NULL, *target = NULL, *version = NULL, *reason = NULL;
    PyObject *idempotency = NULL, *request_body = NULL, *response_body = NULL;
    PyObject *request_source = NULL, *response_source = NULL;
    if (header_policy_init(&policy, forbidden) < 0) return NULL;

    method = encoded_text_attr(exchange, REPLAY_ATTR_METHOD, "ascii", 1);
    target = method == NULL ? NULL
        : encoded_text_attr(exchange, REPLAY_ATTR_TARGET, "ascii", 0);
    version = target == NULL ? NULL
        : encoded_text_attr(exchange, REPLAY_ATTR_HTTP_VERSION, "ascii", 0);
    PyObject *object = version == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_REASON);
    reason = object == NULL ? NULL : replay_bytes(object);
    Py_XDECREF(object);
    object = reason == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_IDEMPOTENCY_KEY);
    if (object != NULL) {
        if (object == Py_None) idempotency = PyBytes_FromStringAndSize(NULL, 0);
        else idempotency = PyUnicode_AsEncodedString(object, "utf-8", "strict");
        Py_DECREF(object);
    }
    object = idempotency == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_REQUEST_BODY);
    request_body = object == NULL ? NULL : replay_bytes(object);
    Py_XDECREF(object);
    object = request_body == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_RESPONSE_BODY);
    response_body = object == NULL ? NULL : replay_bytes(object);
    Py_XDECREF(object);
    request_source = response_body == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_REQUEST_HEADERS);
    response_source = request_source == NULL ? NULL
        : replay_getattr(exchange, REPLAY_ATTR_RESPONSE_HEADERS);
    if (response_source == NULL ||
        replay_headers_init(&request_headers, request_source, &policy, error_type) < 0 ||
        replay_headers_init(&response_headers, response_source, &policy, error_type) < 0)
        goto error;

    uint16_t dependency_id, status;
    if (read_short_attr(exchange, REPLAY_ATTR_DEPENDENCY_ID, "dependency id",
                        &dependency_id, error_type) < 0 ||
        read_short_attr(exchange, REPLAY_ATTR_RESPONSE_STATUS, "response status",
                        &status, error_type) < 0 ||
        check_short_length(PyBytes_GET_SIZE(method), "method", error_type) < 0 ||
        check_short_length(PyBytes_GET_SIZE(target), "target", error_type) < 0 ||
        check_short_length(PyBytes_GET_SIZE(version), "HTTP version", error_type) < 0 ||
        check_short_length(PyBytes_GET_SIZE(reason), "reason", error_type) < 0 ||
        check_short_length(PyBytes_GET_SIZE(idempotency), "idempotency key", error_type) < 0)
        goto error;
    uint64_t header_count = (uint64_t)request_headers.count +
                            (uint64_t)response_headers.count;
    if (header_count > UINT16_MAX) {
        PyErr_SetString(error_type,
                        "outbound exchange header count exceeds 65535");
        goto error;
    }
    uint64_t request_length = (uint64_t)PyBytes_GET_SIZE(request_body);
    uint64_t response_length = (uint64_t)PyBytes_GET_SIZE(response_body);
    if (request_length > UINT32_MAX || response_length > UINT32_MAX) {
        PyErr_SetString(error_type,
                        "outbound exchange body exceeds the 4 GiB wire limit");
        goto error;
    }
    object = replay_getattr(exchange, REPLAY_ATTR_SEQUENCE);
    if (object == NULL) goto error;
    uint64_t sequence = PyLong_AsUnsignedLongLong(object);
    Py_DECREF(object);
    if (sequence == UINT64_MAX && PyErr_Occurred()) {
        PyErr_Clear();
        PyErr_SetString(error_type, "outbound exchange sequence exceeds uint64");
        goto error;
    }
    object = replay_getattr(exchange, REPLAY_ATTR_HEADERS_REDACTED);
    if (object == NULL) goto error;
    int already_redacted = PyObject_IsTrue(object);
    Py_DECREF(object);
    if (already_redacted < 0) goto error;
    uint32_t flags = already_redacted || request_headers.redacted ||
                     response_headers.redacted ? HTTP_HEADERS_REDACTED : 0;

    uint64_t total = HTTP_V2_HEAD_SIZE + (uint64_t)PyBytes_GET_SIZE(method) +
        (uint64_t)PyBytes_GET_SIZE(target) + (uint64_t)PyBytes_GET_SIZE(version) +
        (uint64_t)PyBytes_GET_SIZE(reason) + (uint64_t)PyBytes_GET_SIZE(idempotency) +
        request_headers.wire_size + HTTP_HEADER_SIZE + response_headers.wire_size +
        request_length + response_length;
    if (total > PY_SSIZE_T_MAX) {
        PyErr_NoMemory();
        goto error;
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)total);
    if (result == NULL) goto error;
    uint8_t *cursor = (uint8_t *)PyBytes_AS_STRING(result);
    memcpy(cursor, "WHX2", 4);
    wreath_store_u64_le(cursor + 4, sequence);
    wreath_store_u16_le(cursor + 12, dependency_id);
    wreath_store_u16_le(cursor + 14, status);
    wreath_store_u16_le(cursor + 16, (uint16_t)PyBytes_GET_SIZE(method));
    wreath_store_u16_le(cursor + 18, (uint16_t)PyBytes_GET_SIZE(target));
    wreath_store_u16_le(cursor + 20, (uint16_t)PyBytes_GET_SIZE(version));
    wreath_store_u16_le(cursor + 22, (uint16_t)PyBytes_GET_SIZE(reason));
    wreath_store_u16_le(cursor + 24, (uint16_t)PyBytes_GET_SIZE(idempotency));
    wreath_store_u16_le(cursor + 26, (uint16_t)request_headers.count);
    wreath_store_u32_le(cursor + 28, (uint32_t)request_length);
    wreath_store_u32_le(cursor + 32, (uint32_t)response_length);
    cursor += HTTP_V2_HEAD_SIZE;
#define COPY_REPLAY_BYTES(value) do { \
    Py_ssize_t copy_length = PyBytes_GET_SIZE(value); \
    memcpy(cursor, PyBytes_AS_STRING(value), (size_t)copy_length); \
    cursor += copy_length; \
} while (0)
    COPY_REPLAY_BYTES(method);
    COPY_REPLAY_BYTES(target);
    COPY_REPLAY_BYTES(version);
    COPY_REPLAY_BYTES(reason);
    COPY_REPLAY_BYTES(idempotency);
    write_replay_headers(&cursor, &request_headers);
    wreath_store_u32_le(cursor, (uint32_t)response_headers.count);
    wreath_store_u32_le(cursor + 4, flags);
    cursor += HTTP_HEADER_SIZE;
    write_replay_headers(&cursor, &response_headers);
    COPY_REPLAY_BYTES(request_body);
    COPY_REPLAY_BYTES(response_body);
#undef COPY_REPLAY_BYTES

    header_policy_clear(&policy);
    replay_headers_clear(&request_headers);
    replay_headers_clear(&response_headers);
    Py_DECREF(method); Py_DECREF(target); Py_DECREF(version); Py_DECREF(reason);
    Py_DECREF(idempotency); Py_DECREF(request_body); Py_DECREF(response_body);
    Py_DECREF(request_source); Py_DECREF(response_source);
    return result;
error:
    header_policy_clear(&policy);
    replay_headers_clear(&request_headers);
    replay_headers_clear(&response_headers);
    Py_XDECREF(method); Py_XDECREF(target); Py_XDECREF(version); Py_XDECREF(reason);
    Py_XDECREF(idempotency); Py_XDECREF(request_body); Py_XDECREF(response_body);
    Py_XDECREF(request_source); Py_XDECREF(response_source);
    return NULL;
}

static int
replay_take(const uint8_t *data, Py_ssize_t length, Py_ssize_t *offset,
            uint64_t take, const char *label, const uint8_t **out,
            PyObject *error_type)
{
    if (take > (uint64_t)(length - *offset)) {
        PyErr_Format(error_type, "outbound exchange %s is truncated", label);
        return -1;
    }
    *out = data + *offset;
    *offset += (Py_ssize_t)take;
    return 0;
}

static PyObject *
decode_replay_text(const uint8_t *data, Py_ssize_t length, const char *encoding,
                   PyObject *error_type)
{
    PyObject *text = strcmp(encoding, "ascii") == 0
        ? PyUnicode_DecodeASCII((const char *)data, length, "strict")
        : PyUnicode_DecodeUTF8((const char *)data, length, "strict");
    if (text == NULL && PyErr_ExceptionMatches(PyExc_UnicodeError)) {
        PyErr_Clear();
        PyErr_SetString(error_type,
                        "outbound exchange text has the wrong encoding");
    }
    return text;
}

static PyObject *
decode_replay_headers(const uint8_t *data, Py_ssize_t length,
                      Py_ssize_t *offset, uint32_t count,
                      PyObject *error_type)
{
    Py_ssize_t scan = *offset;
    for (uint32_t i = 0; i < count; i++) {
        if (length - scan < HTTP_HEADER_SIZE) {
            PyErr_SetString(error_type,
                            "outbound exchange header length is truncated");
            return NULL;
        }
        uint32_t name_length = wreath_load_u32_le(data + scan);
        uint32_t value_length = wreath_load_u32_le(data + scan + 4);
        scan += HTTP_HEADER_SIZE;
        uint64_t pair_length = (uint64_t)name_length + value_length;
        if (pair_length > (uint64_t)(length - scan)) {
            PyErr_SetString(error_type,
                            "outbound exchange header is truncated");
            return NULL;
        }
        scan += (Py_ssize_t)pair_length;
        if ((i & 4095U) == 4095U && PyErr_CheckSignals() < 0) return NULL;
    }
    PyObject *headers = PyTuple_New((Py_ssize_t)count);
    if (headers == NULL) return NULL;
    for (uint32_t i = 0; i < count; i++) {
        if (length - *offset < HTTP_HEADER_SIZE) {
            PyErr_SetString(error_type,
                            "outbound exchange header length is truncated");
            goto error;
        }
        uint32_t name_length = wreath_load_u32_le(data + *offset);
        uint32_t value_length = wreath_load_u32_le(data + *offset + 4);
        *offset += HTTP_HEADER_SIZE;
        uint64_t pair_length = (uint64_t)name_length + value_length;
        if (pair_length > (uint64_t)(length - *offset)) {
            PyErr_SetString(error_type, "outbound exchange header is truncated");
            goto error;
        }
        PyObject *name = PyBytes_FromStringAndSize(
            (const char *)data + *offset, name_length);
        PyObject *value = name == NULL ? NULL : PyBytes_FromStringAndSize(
            (const char *)data + *offset + name_length, value_length);
        PyObject *pair = wreath_tuple2_from_owned(name, value);
        if (pair == NULL) goto error;
        PyTuple_SET_ITEM(headers, i, pair);
        *offset += (Py_ssize_t)pair_length;
    }
    return headers;
error:
    Py_DECREF(headers);
    return NULL;
}

static int
dict_set_owned(PyObject *mapping, const char *key, PyObject *value)
{
    if (value == NULL) return -1;
    int result = PyDict_SetItemString(mapping, key, value);
    Py_DECREF(value);
    return result;
}

PyObject *
wreath_http_exchange_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *raw, *record_type, *error_type;
    if (!PyArg_ParseTuple(args, "O!OO:http_exchange_decode", &PyBytes_Type, &raw,
                          &record_type, &error_type)) return NULL;
    const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(raw);
    Py_ssize_t length = PyBytes_GET_SIZE(raw);
    if (length < 4) {
        PyErr_SetString(error_type,
                        "outbound exchange is shorter than its header");
        return NULL;
    }
    uint64_t sequence;
    uint16_t dependency_id, status, method_length, target_length;
    uint16_t version_length, reason_length, idempotency_length;
    uint32_t request_header_count, request_body_length, response_body_length;
    Py_ssize_t offset;
    if (memcmp(data, "WHX2", 4) == 0) {
        if (length < HTTP_V2_HEAD_SIZE) {
            PyErr_SetString(error_type,
                            "outbound exchange is shorter than its header");
            return NULL;
        }
        sequence = wreath_load_u64_le(data + 4);
        dependency_id = wreath_load_u16_le(data + 12);
        status = wreath_load_u16_le(data + 14);
        method_length = wreath_load_u16_le(data + 16);
        target_length = wreath_load_u16_le(data + 18);
        version_length = wreath_load_u16_le(data + 20);
        reason_length = wreath_load_u16_le(data + 22);
        idempotency_length = wreath_load_u16_le(data + 24);
        request_header_count = wreath_load_u16_le(data + 26);
        request_body_length = wreath_load_u32_le(data + 28);
        response_body_length = wreath_load_u32_le(data + 32);
        offset = HTTP_V2_HEAD_SIZE;
    } else if (memcmp(data, "WHX1", 4) == 0) {
        if (length < HTTP_V1_HEAD_SIZE) {
            PyErr_SetString(error_type,
                            "outbound exchange is shorter than its header");
            return NULL;
        }
        sequence = 0;
        dependency_id = wreath_load_u16_le(data + 4);
        status = wreath_load_u16_le(data + 6);
        method_length = wreath_load_u16_le(data + 8);
        target_length = wreath_load_u16_le(data + 10);
        version_length = wreath_load_u16_le(data + 12);
        reason_length = wreath_load_u16_le(data + 14);
        idempotency_length = wreath_load_u16_le(data + 16);
        request_header_count = wreath_load_u16_le(data + 18);
        request_body_length = wreath_load_u32_le(data + 20);
        response_body_length = wreath_load_u32_le(data + 24);
        offset = HTTP_V1_HEAD_SIZE;
    } else {
        PyObject *magic = PyBytes_FromStringAndSize((const char *)data, 4);
        if (magic != NULL)
            PyErr_Format(error_type,
                "bad outbound exchange magic %R; expected WHX1 or WHX2", magic);
        Py_XDECREF(magic);
        return NULL;
    }

    const uint8_t *method_data, *target_data, *version_data, *reason_data;
    const uint8_t *idempotency_data;
    if (replay_take(data, length, &offset, method_length, "method",
                    &method_data, error_type) < 0 ||
        replay_take(data, length, &offset, target_length, "target",
                    &target_data, error_type) < 0 ||
        replay_take(data, length, &offset, version_length, "HTTP version",
                    &version_data, error_type) < 0 ||
        replay_take(data, length, &offset, reason_length, "reason",
                    &reason_data, error_type) < 0 ||
        replay_take(data, length, &offset, idempotency_length, "idempotency key",
                    &idempotency_data, error_type) < 0) return NULL;
    PyObject *method = decode_replay_text(
        method_data, method_length, "ascii", error_type);
    PyObject *target = method == NULL ? NULL : decode_replay_text(
        target_data, target_length, "ascii", error_type);
    PyObject *version = target == NULL ? NULL : decode_replay_text(
        version_data, version_length, "ascii", error_type);
    PyObject *idempotency = NULL;
    if (version != NULL) {
        idempotency = idempotency_length == 0 ? Py_NewRef(Py_None)
            : decode_replay_text(idempotency_data, idempotency_length,
                                 "utf-8", error_type);
    }
    if (idempotency == NULL) goto error_text;
    PyObject *request_headers = decode_replay_headers(
        data, length, &offset, request_header_count, error_type);
    if (request_headers == NULL) goto error_text;
    if (length - offset < HTTP_HEADER_SIZE) {
        PyErr_SetString(error_type,
            "outbound exchange response-header count is truncated");
        Py_DECREF(request_headers);
        goto error_text;
    }
    uint32_t response_header_count = wreath_load_u32_le(data + offset);
    uint32_t flags = wreath_load_u32_le(data + offset + 4);
    offset += HTTP_HEADER_SIZE;
    uint32_t unknown_flags = flags & ~HTTP_HEADERS_REDACTED;
    if (unknown_flags != 0) {
        PyErr_Format(error_type,
            "outbound exchange has unknown flags 0x%x", unknown_flags);
        Py_DECREF(request_headers);
        goto error_text;
    }
    PyObject *response_headers = decode_replay_headers(
        data, length, &offset, response_header_count, error_type);
    if (response_headers == NULL) {
        Py_DECREF(request_headers);
        goto error_text;
    }
    const uint8_t *request_body_data, *response_body_data;
    if ((uint64_t)request_body_length + response_body_length >
        (uint64_t)(length - offset)) {
        PyErr_SetString(error_type, "outbound exchange body is truncated");
        Py_DECREF(request_headers); Py_DECREF(response_headers);
        goto error_text;
    }
    request_body_data = data + offset;
    offset += request_body_length;
    response_body_data = data + offset;
    offset += response_body_length;
    if (offset != length) {
        PyErr_SetString(error_type, "outbound exchange has trailing bytes");
        Py_DECREF(request_headers); Py_DECREF(response_headers);
        goto error_text;
    }

    PyObject *kwargs = PyDict_New();
    if (kwargs == NULL) {
        Py_DECREF(request_headers); Py_DECREF(response_headers);
        goto error_text;
    }
    if (PyDict_SetItemString(kwargs, "request_headers", request_headers) < 0 ||
        PyDict_SetItemString(kwargs, "response_headers", response_headers) < 0) {
        Py_DECREF(request_headers); Py_DECREF(response_headers);
        Py_DECREF(kwargs);
        goto error_text;
    }
    Py_DECREF(request_headers);
    Py_DECREF(response_headers);
    if (dict_set_owned(kwargs, "dependency_id", PyLong_FromUnsignedLong(dependency_id)) < 0 ||
        dict_set_owned(kwargs, "method", Py_NewRef(method)) < 0 ||
        dict_set_owned(kwargs, "target", Py_NewRef(target)) < 0 ||
        dict_set_owned(kwargs, "request_body", PyBytes_FromStringAndSize(
            (const char *)request_body_data, request_body_length)) < 0 ||
        dict_set_owned(kwargs, "idempotency_key", Py_NewRef(idempotency)) < 0 ||
        dict_set_owned(kwargs, "response_status", PyLong_FromUnsignedLong(status)) < 0 ||
        dict_set_owned(kwargs, "response_body", PyBytes_FromStringAndSize(
            (const char *)response_body_data, response_body_length)) < 0 ||
        dict_set_owned(kwargs, "http_version", Py_NewRef(version)) < 0 ||
        dict_set_owned(kwargs, "reason", PyBytes_FromStringAndSize(
            (const char *)reason_data, reason_length)) < 0 ||
        dict_set_owned(kwargs, "headers_redacted", PyBool_FromLong(
            (flags & HTTP_HEADERS_REDACTED) != 0)) < 0 ||
        dict_set_owned(kwargs, "sequence", PyLong_FromUnsignedLongLong(sequence)) < 0) {
        Py_DECREF(kwargs);
        goto error_text;
    }
    PyObject *result = PyObject_VectorcallDict(record_type, NULL, 0, kwargs);
    Py_DECREF(kwargs);
    Py_DECREF(method); Py_DECREF(target); Py_DECREF(version); Py_DECREF(idempotency);
    return result;
error_text:
    Py_XDECREF(method); Py_XDECREF(target); Py_XDECREF(version);
    Py_XDECREF(idempotency);
    return NULL;
}
