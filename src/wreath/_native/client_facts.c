/* Native, operation-owned lookup databases for wreath.client_facts.
 *
 * GeoDB expands compact exact ranges once, then narrows each lookup through a
 * top-byte directory before binary search. UserAgentDB owns a fixed hash index
 * and scans each header once. Python objects are created only at the result
 * boundary; neither type owns process-global mutable state.
 */
#include "wreathcore.h"

#include <limits.h>
#include <stdatomic.h>
#include <stdlib.h>

/* Wreath's compact country database --------------------------------------- */

typedef struct {
    uint32_t first;
    uint32_t last;
    char country[2];
} GeoV4Entry;

typedef struct {
    uint64_t first_high;
    uint64_t last_high;
    char country[2];
} GeoV6Entry;

typedef struct {
    PyObject_HEAD
    GeoV4Entry *v4;
    GeoV6Entry *v6;
    uint16_t v4_count;
    uint16_t v6_count;
    uint16_t v4_first[256];
    uint16_t v4_after[256];
    uint16_t v6_first[256];
    uint16_t v6_after[256];
} GeoDBObject;

static const char GEO_BOUNDS_ERROR[] =
    "Wreath GeoIP field extends past the database image";
static const char UA_BOUNDS_ERROR[] =
    "UA field extends past the WUA1 database image";

static int
image_take(Py_ssize_t length, size_t at, size_t count, const char *error)
{
    if (at > (size_t)length || count > (size_t)length - at) {
        PyErr_SetString(PyExc_ValueError, error);
        return -1;
    }
    return 0;
}

static int
geo_varint(const unsigned char *data, Py_ssize_t length, size_t *at,
           uint64_t *value)
{
    uint64_t decoded = 0;
    for (unsigned int shift = 0; shift <= 63; shift += 7) {
        if (image_take(length, *at, 1, GEO_BOUNDS_ERROR) < 0) return -1;
        unsigned char byte = data[(*at)++];
        if (shift == 63 && (byte & 0xfeu) != 0) {
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP varint exceeds uint64");
            return -1;
        }
        decoded |= (uint64_t)(byte & 0x7fu) << shift;
        if ((byte & 0x80u) == 0) {
            *value = decoded;
            return 0;
        }
    }
    PyErr_SetString(PyExc_ValueError, "Wreath GeoIP varint is too long");
    return -1;
}

static void
geo_index_v4(GeoDBObject *self)
{
    size_t first = 0, after = 0;
    for (uint16_t bucket = 0; bucket < 256; bucket++) {
        uint32_t floor = (uint32_t)bucket << 24;
        uint32_t ceiling = floor | UINT32_C(0x00ffffff);
        while (first < self->v4_count && self->v4[first].last < floor) first++;
        if (after < first) after = first;
        while (after < self->v4_count && self->v4[after].first <= ceiling) after++;
        self->v4_first[bucket] = (uint16_t)first;
        self->v4_after[bucket] = (uint16_t)after;
    }
}

static void
geo_index_v6(GeoDBObject *self)
{
    size_t first = 0, after = 0;
    for (uint16_t bucket = 0; bucket < 256; bucket++) {
        uint64_t floor = (uint64_t)bucket << 56;
        uint64_t ceiling = floor | UINT64_C(0x00ffffffffffffff);
        while (first < self->v6_count &&
               self->v6[first].last_high < floor) first++;
        if (after < first) after = first;
        while (after < self->v6_count &&
               self->v6[after].first_high <= ceiling) after++;
        self->v6_first[bucket] = (uint16_t)first;
        self->v6_after[bucket] = (uint16_t)after;
    }
}

static int
GeoDB_init(GeoDBObject *self, PyObject *args, PyObject *kwargs)
{
    static char *names[] = {"data", NULL};
    PyObject *source;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:GeoDB", names, &source)) {
        return -1;
    }
    if (self->v4 != NULL || self->v6 != NULL) {
        PyErr_SetString(PyExc_RuntimeError, "GeoDB is already initialized");
        return -1;
    }
    Py_buffer view;
    if (PyObject_GetBuffer(source, &view, PyBUF_SIMPLE) < 0) return -1;
    const unsigned char *data = view.buf;
    if (view.len < 9 || view.len > 20000 || memcmp(data, "WGD2", 4) != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "Wreath GeoIP database needs a 9..20000-byte WGD2 image");
        return -1;
    }
    unsigned int countries = data[4];
    uint16_t v4_count = (uint16_t)(data[5] | ((uint16_t)data[6] << 8));
    uint16_t v6_count = (uint16_t)(data[7] | ((uint16_t)data[8] << 8));
    if (countries == 0 || v4_count == 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "Wreath GeoIP database needs countries and IPv4 ranges");
        return -1;
    }
    size_t at = 9;
    size_t country_bytes = (size_t)countries * 2u;
    if (image_take(view.len, at, country_bytes, GEO_BOUNDS_ERROR) < 0) {
        PyBuffer_Release(&view);
        return -1;
    }
    const unsigned char *country_table = data + at;
    for (size_t i = 0; i < country_bytes; i++) {
        unsigned char ch = country_table[i];
        if (ch < 'A' || ch > 'Z') {
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP country codes must be uppercase ASCII");
            return -1;
        }
    }
    for (unsigned int i = 1; i < countries; i++) {
        if (memcmp(country_table + (size_t)(i - 1u) * 2u,
                   country_table + (size_t)i * 2u, 2) >= 0) {
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP country codes must be unique and sorted");
            return -1;
        }
    }
    at += country_bytes;
    size_t remaining = (size_t)view.len - at;
    if ((size_t)v4_count * 3u + (size_t)v6_count * 3u > remaining) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "Wreath GeoIP range counts exceed the database image");
        return -1;
    }
    self->v4 = PyMem_Malloc((size_t)v4_count * sizeof(GeoV4Entry));
    self->v6 = v6_count == 0 ? NULL
        : PyMem_Malloc((size_t)v6_count * sizeof(GeoV6Entry));
    if (self->v4 == NULL || (v6_count != 0 && self->v6 == NULL)) {
        PyMem_Free(self->v4);
        PyMem_Free(self->v6);
        self->v4 = NULL;
        self->v6 = NULL;
        PyBuffer_Release(&view);
        PyErr_NoMemory();
        return -1;
    }

    uint64_t previous_after = 0;
    for (uint16_t i = 0; i < v4_count; i++) {
        uint64_t gap, span;
        if (geo_varint(data, view.len, &at, &gap) < 0 ||
            geo_varint(data, view.len, &at, &span) < 0 ||
            image_take(view.len, at, 1, GEO_BOUNDS_ERROR) < 0) goto fail;
        unsigned int country = data[at++];
        if (gap > UINT32_MAX || span > UINT32_MAX ||
            previous_after + gap > UINT32_MAX ||
            span > UINT32_MAX - (previous_after + gap) || country >= countries) {
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP IPv4 range is invalid");
            goto fail;
        }
        uint32_t first = (uint32_t)(previous_after + gap);
        uint32_t last = first + (uint32_t)span;
        self->v4[i].first = first;
        self->v4[i].last = last;
        memcpy(self->v4[i].country, country_table + country * 2u, 2);
        if (last == UINT32_MAX && i + 1u < v4_count) {
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP IPv4 range follows the address-space end");
            goto fail;
        }
        previous_after = (uint64_t)last + 1u;
    }

    uint64_t previous_last = 0;
    for (uint16_t i = 0; i < v6_count; i++) {
        uint64_t gap, span, first;
        if (geo_varint(data, view.len, &at, &gap) < 0 ||
            geo_varint(data, view.len, &at, &span) < 0 ||
            image_take(view.len, at, 1, GEO_BOUNDS_ERROR) < 0) goto fail;
        unsigned int country = data[at++];
        if (i == 0) {
            first = gap;
        } else {
            if (previous_last == UINT64_MAX ||
                gap > UINT64_MAX - previous_last - 1u) {
                PyErr_SetString(PyExc_ValueError,
                                "Wreath GeoIP IPv6 range gap is invalid");
                goto fail;
            }
            first = previous_last + 1u + gap;
        }
        if (span > UINT64_MAX - first || country >= countries) {
            PyErr_SetString(PyExc_ValueError,
                            "Wreath GeoIP IPv6 range is invalid");
            goto fail;
        }
        self->v6[i].first_high = first;
        self->v6[i].last_high = first + span;
        memcpy(self->v6[i].country, country_table + country * 2u, 2);
        previous_last = first + span;
    }
    if (at != (size_t)view.len) {
        PyErr_SetString(PyExc_ValueError,
                        "Wreath GeoIP database has trailing bytes");
        goto fail;
    }
    self->v4_count = v4_count;
    self->v6_count = v6_count;
    geo_index_v4(self);
    geo_index_v6(self);
    PyBuffer_Release(&view);
    return 0;

fail:
    PyMem_Free(self->v4);
    PyMem_Free(self->v6);
    self->v4 = NULL;
    self->v6 = NULL;
    PyBuffer_Release(&view);
    return -1;
}

static void
GeoDB_dealloc(GeoDBObject *self)
{
    PyMem_Free(self->v4);
    PyMem_Free(self->v6);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
GeoDB_lookup(GeoDBObject *self, PyObject *value)
{
    Py_buffer view;
    if (PyObject_GetBuffer(value, &view, PyBUF_SIMPLE) < 0) return NULL;
    const char *country = NULL;
    if (view.len == 4) {
        const unsigned char *raw = view.buf;
        uint32_t address = ((uint32_t)raw[0] << 24) |
                           ((uint32_t)raw[1] << 16) |
                           ((uint32_t)raw[2] << 8) | raw[3];
        unsigned int bucket = raw[0];
        size_t low = self->v4_first[bucket];
        size_t high = self->v4_after[bucket];
        while (low < high) {
            size_t middle = low + (high - low) / 2u;
            if (self->v4[middle].first <= address) low = middle + 1u;
            else high = middle;
        }
        size_t floor = self->v4_first[bucket];
        if (low != floor && address <= self->v4[low - 1u].last) {
            country = self->v4[low - 1u].country;
        }
    } else if (view.len == 16) {
        const unsigned char *raw = view.buf;
        uint64_t address_high = 0;
        for (unsigned int byte = 0; byte < 8; byte++) {
            address_high = (address_high << 8) | raw[byte];
        }
        unsigned int bucket = raw[0];
        size_t low = self->v6_first[bucket];
        size_t high = self->v6_after[bucket];
        while (low < high) {
            size_t middle = low + (high - low) / 2u;
            if (self->v6[middle].first_high <= address_high) low = middle + 1u;
            else high = middle;
        }
        size_t floor = self->v6_first[bucket];
        if (low != floor && address_high <= self->v6[low - 1u].last_high) {
            country = self->v6[low - 1u].country;
        }
    } else {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "Wreath GeoIP lookup needs 4 or 16 packed address bytes");
        return NULL;
    }
    PyBuffer_Release(&view);
    if (country == NULL) Py_RETURN_NONE;
    return PyUnicode_DecodeASCII(country, 2, "strict");
}

static PyMethodDef GeoDB_methods[] = {
    {"lookup", (PyCFunction)GeoDB_lookup, METH_O,
     "lookup(packed_address) -> country | None"},
    {NULL, NULL, 0, NULL}
};

static PyTypeObject GeoDBType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.GeoDB",
    .tp_basicsize = sizeof(GeoDBObject),
    .tp_dealloc = (destructor)GeoDB_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Native-owned compact Wreath country database.",
    .tp_methods = GeoDB_methods,
    .tp_init = (initproc)GeoDB_init,
    .tp_new = PyType_GenericNew,
};

/* User-Agent token database ------------------------------------------------ */

typedef struct {
    char *token;
    Py_ssize_t token_len;
    char *browser;
    Py_ssize_t browser_len;
    char *platform;
    Py_ssize_t platform_len;
    int mobile;
    int bot;
    int priority;
} UAEntry;

typedef struct {
    PyObject_HEAD
    UAEntry *entries;
    Py_ssize_t count;
    int32_t *slots;
    size_t slot_count;
    unsigned char *image;
    _Atomic uint64_t blocked_count;
} UserAgentDBObject;

static PyTypeObject UserAgentDBType;

typedef struct {
    const char *browser;
    Py_ssize_t browser_len;
    const char *platform;
    Py_ssize_t platform_len;
    const char *version;
    Py_ssize_t version_len;
    int mobile;
    int bot;
    int blocked;
    uint16_t rule_id;
} UAClassification;

static uint64_t
ua_hash(const char *data, Py_ssize_t length)
{
    uint64_t value = UINT64_C(1469598103934665603);
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char ch = (unsigned char)data[i];
        if (ch >= 'A' && ch <= 'Z') ch = (unsigned char)(ch + 32);
        value ^= ch;
        value *= UINT64_C(1099511628211);
    }
    return value;
}

static char *
ua_copy_text(PyObject *value, const char *field, Py_ssize_t *length)
{
    if (value == Py_None) {
        if (length != NULL) *length = 0;
        return NULL;
    }
    if (!PyUnicode_Check(value)) {
        PyErr_Format(PyExc_TypeError, "UA database %s must be str or None", field);
        return NULL;
    }
    Py_ssize_t size;
    const char *text = PyUnicode_AsUTF8AndSize(value, &size);
    if (text == NULL) return NULL;
    if (size <= 0 || size > 128) {
        PyErr_Format(PyExc_ValueError, "UA database %s must be 1..128 UTF-8 bytes", field);
        return NULL;
    }
    char *copy = PyMem_Malloc((size_t)size + 1u);
    if (copy == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    memcpy(copy, text, (size_t)size);
    copy[size] = '\0';
    if (length != NULL) *length = size;
    return copy;
}

static void
ua_entries_free(UserAgentDBObject *self)
{
    if (self->entries != NULL && self->image == NULL) {
        for (Py_ssize_t i = 0; i < self->count; i++) {
            PyMem_Free(self->entries[i].token);
            PyMem_Free(self->entries[i].browser);
            PyMem_Free(self->entries[i].platform);
        }
    }
    PyMem_Free(self->entries);
    PyMem_Free(self->slots);
    PyMem_Free(self->image);
    self->entries = NULL;
    self->slots = NULL;
    self->image = NULL;
    self->count = 0;
}

static int
ua_build_slots(UserAgentDBObject *self)
{
    size_t slots = 1;
    while (slots < (size_t)self->count * 2u) slots <<= 1;
    self->slots = PyMem_Malloc(slots * sizeof(int32_t));
    if (self->slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->slot_count = slots;
    for (size_t i = 0; i < slots; i++) self->slots[i] = -1;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        UAEntry *entry = &self->entries[i];
        size_t slot = (size_t)ua_hash(entry->token, entry->token_len) & (slots - 1u);
        while (self->slots[slot] != -1) {
            UAEntry *other = &self->entries[self->slots[slot]];
            if (other->token_len == entry->token_len &&
                memcmp(other->token, entry->token, (size_t)entry->token_len) == 0) {
                PyErr_Format(PyExc_ValueError,
                             "duplicate UA database token %.*s",
                             (int)entry->token_len, entry->token);
                return -1;
            }
            slot = (slot + 1u) & (slots - 1u);
        }
        self->slots[slot] = (int32_t)i;
    }
    return 0;
}

static int
ua_binary_init(UserAgentDBObject *self, PyObject *source)
{
    Py_buffer view;
    if (PyObject_GetBuffer(source, &view, PyBUF_SIMPLE) < 0) return -1;
    const unsigned char *input = view.buf;
    Py_ssize_t image_len = view.len;
    if (view.len < 7 || view.len > 5000 || memcmp(input, "WUA1", 4) != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "UA binary database needs a 7..5000-byte WUA1 image");
        return -1;
    }
    unsigned int string_count = input[4];
    uint16_t count = (uint16_t)(input[5] | ((uint16_t)input[6] << 8));
    if (string_count == 0 || count == 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "UA binary database needs strings and entries");
        return -1;
    }
    if ((size_t)string_count * 2u + (size_t)count * 5u >
        (size_t)image_len - 7u) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "UA string and entry counts exceed the WUA1 image");
        return -1;
    }
    unsigned char *image = PyMem_Malloc((size_t)image_len);
    UAEntry *entries = PyMem_Calloc((size_t)count, sizeof(UAEntry));
    if (image == NULL || entries == NULL) {
        PyMem_Free(image);
        PyMem_Free(entries);
        PyBuffer_Release(&view);
        PyErr_NoMemory();
        return -1;
    }
    memcpy(image, input, (size_t)image_len);
    PyBuffer_Release(&view);
    self->image = image;
    self->entries = entries;
    self->count = count;
    const char *strings[255];
    Py_ssize_t string_lengths[255];
    size_t at = 7;
    for (unsigned int i = 0; i < string_count; i++) {
        if (image_take(image_len, at, 1, UA_BOUNDS_ERROR) < 0) goto fail;
        size_t length = image[at++];
        if (length == 0) {
            PyErr_SetString(PyExc_ValueError,
                            "UA binary strings must contain 1..255 bytes");
            goto fail;
        }
        if (image_take(image_len, at, length, UA_BOUNDS_ERROR) < 0) goto fail;
        PyObject *checked = PyUnicode_DecodeUTF8(
            (const char *)image + at, (Py_ssize_t)length, "strict");
        if (checked == NULL) goto fail;
        Py_DECREF(checked);
        strings[i] = (const char *)image + at;
        string_lengths[i] = (Py_ssize_t)length;
        at += length;
    }
    for (uint16_t i = 0; i < count; i++) {
        if (image_take(image_len, at, 1, UA_BOUNDS_ERROR) < 0) goto fail;
        size_t token_len = image[at++];
        if (token_len == 0 || token_len > 128) {
            PyErr_SetString(PyExc_ValueError,
                            "UA binary token must contain 1..128 bytes");
            goto fail;
        }
        if (image_take(image_len, at, token_len + 4u, UA_BOUNDS_ERROR) < 0)
            goto fail;
        entries[i].token = (char *)image + at;
        entries[i].token_len = (Py_ssize_t)token_len;
        for (size_t j = 0; j < token_len; j++) {
            unsigned char ch = image[at + j];
            if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') ||
                  ch == '_' || ch == '-' || ch == '.')) {
                PyErr_SetString(PyExc_ValueError,
                                "UA binary token must be lowercase ASCII");
                goto fail;
            }
        }
        at += token_len;
        unsigned int browser = image[at++];
        unsigned int platform = image[at++];
        unsigned int flags = image[at++];
        entries[i].priority = image[at++];
        if ((browser != 255 && browser >= string_count) ||
            (platform != 255 && platform >= string_count) ||
            (flags & ~7u) != 0 || (flags & 3u) == 3u) {
            PyErr_SetString(PyExc_ValueError,
                            "UA binary entry has an invalid index or flags");
            goto fail;
        }
        if (browser != 255) {
            entries[i].browser = (char *)strings[browser];
            entries[i].browser_len = string_lengths[browser];
        }
        if (platform != 255) {
            entries[i].platform = (char *)strings[platform];
            entries[i].platform_len = string_lengths[platform];
        }
        entries[i].mobile = (flags & 3u) == 0 ? -1 : (int)(flags & 3u) - 1;
        entries[i].bot = (flags & 4u) != 0;
    }
    if (at != (size_t)image_len) {
        PyErr_SetString(PyExc_ValueError,
                        "UA binary database has trailing bytes");
        goto fail;
    }
    if (ua_build_slots(self) < 0) goto fail;
    return 0;

fail:
    ua_entries_free(self);
    return -1;
}

static int
UserAgentDB_init(UserAgentDBObject *self, PyObject *args, PyObject *kwargs)
{
    static char *names[] = {"entries", NULL};
    PyObject *source;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:UserAgentDB", names, &source)) return -1;
    if (self->entries != NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "UserAgentDB is already initialized");
        return -1;
    }
    atomic_init(&self->blocked_count, 0);
    if (PyObject_CheckBuffer(source)) return ua_binary_init(self, source);
    PyObject *rows = PySequence_Fast(source, "UA database entries must be a sequence");
    if (rows == NULL) return -1;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(rows);
    if (count <= 0 || count > 65535) {
        Py_DECREF(rows);
        PyErr_SetString(PyExc_ValueError, "UA database needs 1..65535 entries");
        return -1;
    }
    UAEntry *entries = PyMem_Calloc((size_t)count, sizeof(UAEntry));
    if (entries == NULL) {
        Py_DECREF(rows);
        PyErr_NoMemory();
        return -1;
    }
    self->entries = entries;
    self->count = count;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *row = PySequence_Fast(PySequence_Fast_GET_ITEM(rows, i),
                                        "each UA database entry must be a sequence");
        if (row == NULL) goto fail;
        if (PySequence_Fast_GET_SIZE(row) != 6) {
            Py_DECREF(row);
            PyErr_SetString(PyExc_ValueError,
                            "each UA database entry must be (token, browser, platform, mobile, bot, priority)");
            goto fail;
        }
        PyObject **values = PySequence_Fast_ITEMS(row);
        entries[i].token = ua_copy_text(values[0], "token", &entries[i].token_len);
        entries[i].browser = ua_copy_text(
            values[1], "browser", &entries[i].browser_len);
        if (entries[i].token == NULL || (values[1] != Py_None && entries[i].browser == NULL)) {
            Py_DECREF(row);
            goto fail;
        }
        entries[i].platform = ua_copy_text(
            values[2], "platform", &entries[i].platform_len);
        if (values[2] != Py_None && entries[i].platform == NULL) {
            Py_DECREF(row);
            goto fail;
        }
        long mobile = PyLong_AsLong(values[3]);
        int bot = PyObject_IsTrue(values[4]);
        long priority = PyLong_AsLong(values[5]);
        Py_DECREF(row);
        if ((mobile == -1 && PyErr_Occurred()) || bot < 0 ||
            (priority == -1 && PyErr_Occurred())) goto fail;
        if (mobile < -1 || mobile > 1) {
            PyErr_SetString(PyExc_ValueError, "UA database mobile must be -1, 0, or 1");
            goto fail;
        }
        if (priority < INT_MIN || priority > INT_MAX) {
            PyErr_SetString(PyExc_ValueError, "UA database priority is outside C int range");
            goto fail;
        }
        entries[i].mobile = (int)mobile;
        entries[i].bot = bot;
        entries[i].priority = (int)priority;
        for (Py_ssize_t j = 0; j < entries[i].token_len; j++) {
            unsigned char ch = (unsigned char)entries[i].token[j];
            if (ch >= 'A' && ch <= 'Z') entries[i].token[j] = (char)(ch + 32);
            else if (!((ch >= 'a' && ch <= 'z') ||
                       (ch >= '0' && ch <= '9') || ch == '_' ||
                       ch == '-' || ch == '.')) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "UA database token must contain only product-token characters");
                goto fail;
            }
        }
    }
    Py_DECREF(rows);
    if (ua_build_slots(self) < 0) goto fail_no_rows;
    return 0;

fail:
    Py_DECREF(rows);
fail_no_rows:
    ua_entries_free(self);
    return -1;
}

static void
UserAgentDB_dealloc(UserAgentDBObject *self)
{
    ua_entries_free(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static UAEntry *
ua_find(UserAgentDBObject *self, const char *token, Py_ssize_t length)
{
    size_t slot = (size_t)ua_hash(token, length) & (self->slot_count - 1u);
    for (;;) {
        int32_t index = self->slots[slot];
        if (index < 0) return NULL;
        UAEntry *entry = &self->entries[index];
        if (entry->token_len == length) {
            Py_ssize_t i;
            for (i = 0; i < length; i++) {
                unsigned char ch = (unsigned char)token[i];
                if (ch >= 'A' && ch <= 'Z') ch = (unsigned char)(ch + 32);
                if (ch != (unsigned char)entry->token[i]) break;
            }
            if (i == length) return entry;
        }
        slot = (slot + 1u) & (self->slot_count - 1u);
    }
}

static int
ua_token_char(unsigned char ch)
{
    return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
           (ch >= '0' && ch <= '9') || ch == '_' || ch == '-' || ch == '.';
}

static PyObject *
ua_optional_text(const char *value, Py_ssize_t length)
{
    if (value == NULL) Py_RETURN_NONE;
    return PyUnicode_DecodeUTF8(value, length, "strict");
}

static int
ua_rule_table_contains(const unsigned char *data, Py_ssize_t count,
                       uint16_t rule_id)
{
    Py_ssize_t low = 0;
    Py_ssize_t high = count;
    while (low < high) {
        Py_ssize_t middle = low + (high - low) / 2;
        Py_ssize_t at = middle * 2;
        uint16_t candidate = (uint16_t)(data[at] |
                                        ((uint16_t)data[at + 1] << 8));
        if (candidate < rule_id) low = middle + 1;
        else high = middle;
    }
    if (low >= count) return 0;
    Py_ssize_t at = low * 2;
    uint16_t candidate = (uint16_t)(data[at] |
                                    ((uint16_t)data[at + 1] << 8));
    return candidate == rule_id;
}

static int
ua_classify_data(UserAgentDBObject *self, const char *data, Py_ssize_t size,
                 UAClassification *out, const unsigned char *blocked_ids,
                 Py_ssize_t blocked_count)
{
    if (size > 8192) return 1;
    memset(out, 0, sizeof(*out));
    out->mobile = -1;
    int browser_priority = INT_MIN, platform_priority = INT_MIN;
    int mobile_priority = INT_MIN;
    int rule_priority = INT_MIN;
    Py_ssize_t at = 0;
    while (at < size) {
        while (at < size && !ua_token_char((unsigned char)data[at])) at++;
        Py_ssize_t start = at;
        while (at < size && ua_token_char((unsigned char)data[at])) at++;
        Py_ssize_t length = at - start;
        if (length == 0 || length > 128) continue;
        UAEntry *entry = ua_find(self, data + start, length);
        if (entry == NULL) {
            for (Py_ssize_t split = 1; split < length; split++) {
                if (data[start + split] == '-') {
                    entry = ua_find(self, data + start, split);
                    if (entry != NULL) break;
                }
            }
        }
        if (entry == NULL) continue;
        uint16_t rule_id = (uint16_t)((entry - self->entries) + 1);
        if (blocked_ids != NULL &&
            ua_rule_table_contains(blocked_ids, blocked_count, rule_id)) {
            out->blocked = 1;
        }
        if (entry->priority > rule_priority) {
            rule_priority = entry->priority;
            out->rule_id = rule_id;
        }
        if (entry->browser != NULL && entry->priority > browser_priority) {
            out->browser = entry->browser;
            out->browser_len = entry->browser_len;
            browser_priority = entry->priority;
            out->version = NULL;
            out->version_len = 0;
            if (at < size && data[at] == '/') {
                Py_ssize_t vstart = ++at;
                while (at < size &&
                       (((unsigned char)data[at] >= '0' && (unsigned char)data[at] <= '9') ||
                        data[at] == '.')) at++;
                if (at > vstart) {
                    out->version = data + vstart;
                    out->version_len = at - vstart;
                }
            }
        }
        if (entry->platform != NULL && entry->priority > platform_priority) {
            out->platform = entry->platform;
            out->platform_len = entry->platform_len;
            platform_priority = entry->priority;
        }
        if (entry->mobile >= 0 && entry->priority > mobile_priority) {
            out->mobile = entry->mobile;
            mobile_priority = entry->priority;
        }
        if (entry->bot) out->bot = 1;
    }
    return 0;
}

static int
ua_classify(UserAgentDBObject *self, PyObject *value, UAClassification *out,
            const unsigned char *blocked_ids, Py_ssize_t blocked_count)
{
    Py_buffer view;
    if (PyObject_GetBuffer(value, &view, PyBUF_SIMPLE) < 0) return -1;
    int result = ua_classify_data(
        self, view.buf, view.len, out, blocked_ids, blocked_count);
    PyBuffer_Release(&view);
    return result;
}

static PyObject *
UserAgentDB_classify(UserAgentDBObject *self, PyObject *value)
{
    UAClassification found;
    int classified = ua_classify(self, value, &found, NULL, 0);
    if (classified < 0) return NULL;
    if (classified > 0) {
        PyErr_SetString(PyExc_ValueError, "User-Agent exceeds 8192 bytes");
        return NULL;
    }
    PyObject *browser_obj = ua_optional_text(found.browser, found.browser_len);
    PyObject *version_obj = found.version == NULL ? Py_NewRef(Py_None)
        : PyUnicode_DecodeASCII(found.version, found.version_len, "strict");
    PyObject *platform_obj = ua_optional_text(found.platform, found.platform_len);
    PyObject *mobile_obj = found.mobile < 0
        ? Py_NewRef(Py_None) : PyBool_FromLong(found.mobile);
    PyObject *bot_obj = PyBool_FromLong(found.bot);
    PyObject *rule_obj = PyLong_FromUnsignedLong(found.rule_id);
    if (browser_obj == NULL || version_obj == NULL || platform_obj == NULL ||
        mobile_obj == NULL || bot_obj == NULL || rule_obj == NULL) {
        Py_XDECREF(browser_obj); Py_XDECREF(version_obj); Py_XDECREF(platform_obj);
        Py_XDECREF(mobile_obj); Py_XDECREF(bot_obj); Py_XDECREF(rule_obj);
        return NULL;
    }
    PyObject *out = PyTuple_Pack(6, browser_obj, version_obj, platform_obj,
                                 mobile_obj, bot_obj, rule_obj);
    Py_DECREF(browser_obj); Py_DECREF(version_obj); Py_DECREF(platform_obj);
    Py_DECREF(mobile_obj); Py_DECREF(bot_obj); Py_DECREF(rule_obj);
    return out;
}

static int
user_agent_blocked_data(PyObject *database, const char *data, Py_ssize_t size,
                        PyObject *table, int *blocked)
{
    if (!Py_IS_TYPE(database, &UserAgentDBType)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native AI scraping policy needs a UserAgentDB");
        return -1;
    }
    if (!PyBytes_CheckExact(table) || (PyBytes_GET_SIZE(table) & 1) != 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native AI scraping policy needs packed uint16 rule ids");
        return -1;
    }
    UAClassification found;
    int classified = ua_classify_data(
        (UserAgentDBObject *)database, data, size, &found,
        (const unsigned char *)PyBytes_AS_STRING(table),
        PyBytes_GET_SIZE(table) / 2);
    if (classified < 0) return -1;
    *blocked = classified == 0 && found.blocked;
    if (*blocked) {
        atomic_fetch_add_explicit(
            &((UserAgentDBObject *)database)->blocked_count, 1,
            memory_order_relaxed);
    }
    return 0;
}

static int
user_agent_blocked(PyObject *database, PyObject *value, PyObject *table,
                   int *blocked)
{
    Py_buffer view;
    if (PyObject_GetBuffer(value, &view, PyBUF_SIMPLE) < 0) return -1;
    int result = user_agent_blocked_data(
        database, view.buf, view.len, table, blocked);
    PyBuffer_Release(&view);
    return result;
}

static PyObject *
UserAgentDB_blocked(UserAgentDBObject *self, PyObject *args)
{
    PyObject *value;
    PyObject *table;
    if (!PyArg_ParseTuple(args, "OO:blocked", &value, &table)) return NULL;
    int blocked;
    if (user_agent_blocked((PyObject *)self, value, table, &blocked) < 0) {
        return NULL;
    }
    return PyBool_FromLong(blocked);
}

static PyObject *
UserAgentDB_blocked_headers(UserAgentDBObject *self, PyObject *args)
{
    PyObject *headers;
    PyObject *table;
    if (!PyArg_ParseTuple(args, "OO:blocked_headers", &headers, &table)) return NULL;
    if (!PyBytes_CheckExact(table) || (PyBytes_GET_SIZE(table) & 1) != 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native AI scraping policy needs packed uint16 rule ids");
        return NULL;
    }
    PyObject *seq = PySequence_Fast(headers, "headers must be a sequence");
    if (seq == NULL) return NULL;
    PyObject *value = NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(seq);
    PyObject **items = PySequence_Fast_ITEMS(seq);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = items[i];
        PyObject *name;
        if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
            name = PyTuple_GET_ITEM(pair, 0);
            value = PyTuple_GET_ITEM(pair, 1);
        }
        else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
            name = PyList_GET_ITEM(pair, 0);
            value = PyList_GET_ITEM(pair, 1);
        }
        else {
            PyErr_SetString(PyExc_TypeError,
                            "header entries must be two-item tuples");
            Py_DECREF(seq);
            return NULL;
        }
        if (!PyBytes_Check(name) || !PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError,
                            "header names and values must be bytes");
            Py_DECREF(seq);
            return NULL;
        }
        if (PyBytes_GET_SIZE(name) == 10 &&
            memcmp(PyBytes_AS_STRING(name), "user-agent", 10) == 0) {
            break;
        }
        value = NULL;
    }
    if (value == NULL) {
        Py_DECREF(seq);
        Py_RETURN_FALSE;
    }
    int blocked;
    int result = user_agent_blocked((PyObject *)self, value, table, &blocked);
    Py_DECREF(seq);
    if (result < 0) return NULL;
    return PyBool_FromLong(blocked);
}

static PyObject *
UserAgentDB_lookup(UserAgentDBObject *self, PyObject *value)
{
    PyObject *classified = UserAgentDB_classify(self, value);
    PyObject *result;
    if (classified == NULL) return NULL;
    result = PyTuple_GetSlice(classified, 0, 5);
    Py_DECREF(classified);
    return result;
}

static PyObject *
UserAgentDB_blocked_count(UserAgentDBObject *self, PyObject *Py_UNUSED(ignored))
{
    uint64_t count = atomic_load_explicit(&self->blocked_count, memory_order_relaxed);
    return PyLong_FromUnsignedLongLong((unsigned long long)count);
}

static PyMethodDef UserAgentDB_methods[] = {
    {"lookup", (PyCFunction)UserAgentDB_lookup, METH_O,
     "lookup(user_agent_bytes) -> (browser, version, platform, mobile, bot)"},
    {"classify", (PyCFunction)UserAgentDB_classify, METH_O,
     "classify(user_agent_bytes) -> (..., bot, stable_rule_id)"},
    {"blocked", (PyCFunction)UserAgentDB_blocked, METH_VARARGS,
     "blocked(user_agent_bytes, packed_rule_ids) -> bool"},
    {"blocked_headers", (PyCFunction)UserAgentDB_blocked_headers, METH_VARARGS,
     "blocked_headers(headers, packed_rule_ids) -> bool"},
    {"blocked_count", (PyCFunction)UserAgentDB_blocked_count, METH_NOARGS,
     "blocked_count() -> cumulative blocked lookups"},
    {NULL, NULL, 0, NULL}
};

static PyTypeObject UserAgentDBType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.UserAgentDB",
    .tp_basicsize = sizeof(UserAgentDBObject),
    .tp_dealloc = (destructor)UserAgentDB_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Native-owned single-scan User-Agent token database.",
    .tp_methods = UserAgentDB_methods,
    .tp_init = (initproc)UserAgentDB_init,
    .tp_new = PyType_GenericNew,
};

int
wreath_user_agent_blocked(PyObject *database, PyObject *value, PyObject *table,
                          int *blocked)
{
    return user_agent_blocked(database, value, table, blocked);
}

int
wreath_user_agent_blocked_raw(PyObject *database, const char *data,
                              Py_ssize_t size, PyObject *table, int *blocked)
{
    return user_agent_blocked_data(database, data, size, table, blocked);
}

int
wreath_user_agent_database_check(PyObject *database)
{
    return Py_IS_TYPE(database, &UserAgentDBType);
}

int
wreath_register_client_facts(PyObject *module)
{
    if (PyType_Ready(&GeoDBType) < 0 ||
        PyType_Ready(&UserAgentDBType) < 0) return -1;
    if (PyModule_AddObjectRef(module, "GeoDB", (PyObject *)&GeoDBType) < 0 ||
        PyModule_AddObjectRef(module, "UserAgentDB", (PyObject *)&UserAgentDBType) < 0) {
        return -1;
    }
    return 0;
}
