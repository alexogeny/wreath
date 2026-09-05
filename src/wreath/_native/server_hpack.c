/* HPACK (RFC 7541) header compression for HTTP/2.
 *
 * Self-contained: a dynamic table, a canonical Huffman decode tree built once at
 * module load, integer/string primitives with overflow guards, and a literal
 * encoder for response headers. All protocol-error paths return -1 with an HTTP/2
 * error code in *h2_error and no Python exception set; only genuine allocation
 * failures set a Python exception.
 */
#include "server.h"

typedef struct {
    uint32_t code;
    uint8_t nbits;
} WreathHuffCode;

#include "server_hpack_huffman.h"

/* --- static table (RFC 7541 Appendix A) ---------------------------------- */
static const char *const STATIC_NAMES[] = {
    ":authority", ":method", ":method", ":path", ":path", ":scheme", ":scheme",
    ":status", ":status", ":status", ":status", ":status", ":status", ":status",
    "accept-charset", "accept-encoding", "accept-language", "accept-ranges",
    "accept", "access-control-allow-origin", "age", "allow", "authorization",
    "cache-control", "content-disposition", "content-encoding",
    "content-language", "content-length", "content-location", "content-range",
    "content-type", "cookie", "date", "etag", "expect", "expires", "from",
    "host", "if-match", "if-modified-since", "if-none-match", "if-range",
    "if-unmodified-since", "last-modified", "link", "location", "max-forwards",
    "proxy-authenticate", "proxy-authorization", "range", "referer", "refresh",
    "retry-after", "server", "set-cookie", "strict-transport-security",
    "transfer-encoding", "user-agent", "vary", "via", "www-authenticate",
};
static const char *const STATIC_VALUES[] = {
    "", "GET", "POST", "/", "/index.html", "http", "https", "200", "204", "206",
    "304", "400", "404", "500", "", "gzip, deflate", "", "", "", "", "", "", "",
    "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
    "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
};
#define STATIC_TABLE_LEN 61

/* --- Huffman byte-transition table (built once) ------------------------- */
typedef struct {
    int sym;    /* symbol at a leaf, or -1 for an internal node */
    int child[2];
} HuffNode;

static HuffNode *huff_tree = NULL;
static uint32_t *huff_transitions = NULL;
static uint8_t *huff_final_valid = NULL;
static int huff_state_count = 0;

/* The static table (RFC 7541 Appendix A) is a compile-time constant, but an
 * indexed reference to it appears in almost every request's header block. These
 * cache one PyBytes per name/value so resolve_index hands out a new reference
 * instead of re-encoding the same literal on every occurrence. */
static PyObject *static_name_objects[STATIC_TABLE_LEN];
static PyObject *static_value_objects[STATIC_TABLE_LEN];

static void
free_static_table(void)
{
    for (int i = 0; i < STATIC_TABLE_LEN; i++) {
        Py_CLEAR(static_name_objects[i]);
        Py_CLEAR(static_value_objects[i]);
    }
}

static int
build_static_table(void)
{
    for (int i = 0; i < STATIC_TABLE_LEN; i++) {
        /* native-lint: allow NC007 -- one-time build of the constant-header cache. */
        static_name_objects[i] = PyBytes_FromString(STATIC_NAMES[i]);
        /* native-lint: allow NC007 -- one-time build of the constant-header cache. */
        static_value_objects[i] = PyBytes_FromString(STATIC_VALUES[i]);
        if (static_name_objects[i] == NULL || static_value_objects[i] == NULL) {
            free_static_table();
            return -1;
        }
    }
    return 0;
}

void
wreath_hpack_free_huffman(void)
{
    free_static_table();
    PyMem_Free(huff_final_valid);
    PyMem_Free(huff_transitions);
    PyMem_Free(huff_tree);
    huff_final_valid = NULL;
    huff_transitions = NULL;
    huff_tree = NULL;
    huff_state_count = 0;
}

int
wreath_hpack_build_huffman(void)
{
    int cap = 2;
    wreath_hpack_free_huffman();
    for (int i = 0; i < 257; i++) cap += WREATH_HUFF[i].nbits;
    huff_tree = PyMem_Malloc(sizeof(HuffNode) * (size_t)cap);
    if (huff_tree == NULL) goto no_memory;
    huff_tree[0] = (HuffNode){.sym = -1, .child = {-1, -1}};
    int used = 1;
    for (int sym = 0; sym < 257; sym++) {
        uint32_t code = WREATH_HUFF[sym].code;
        int node = 0;
        for (int b = WREATH_HUFF[sym].nbits - 1; b >= 0; b--) {
            int bit = (code >> b) & 1;
            int next = huff_tree[node].child[bit];
            if (next == -1) {
                next = used++;
                huff_tree[next] = (HuffNode){.sym = -1, .child = {-1, -1}};
                huff_tree[node].child[bit] = next;
            }
            node = next;
        }
        huff_tree[node].sym = sym;
    }
    huff_state_count = used;
    if (used > 1024) {
        PyErr_SetString(PyExc_RuntimeError,
                        "HPACK Huffman decoder has more than 1024 states");
        wreath_hpack_free_huffman();
        return -1;
    }
    if ((size_t)used > SIZE_MAX / (256 * sizeof(*huff_transitions))) goto no_memory;
    huff_transitions = PyMem_Calloc(
        (size_t)used * 256, sizeof(*huff_transitions));
    huff_final_valid = PyMem_Calloc((size_t)used, sizeof(uint8_t));
    if (huff_transitions == NULL || huff_final_valid == NULL) goto no_memory;

    for (int state = 0; state < used; state++) {
        if (huff_tree[state].sym >= 0) continue;
        for (int byte = 0; byte < 256; byte++) {
            int node = state;
            uint8_t output[2] = {0, 0};
            uint8_t output_count = 0;
            int invalid = 0;
            for (int b = 7; b >= 0; b--) {
                node = huff_tree[node].child[(byte >> b) & 1];
                if (node < 0) { invalid = 1; break; }
                int sym = huff_tree[node].sym;
                if (sym >= 0) {
                    if (sym == 256 || output_count == 2) {
                        invalid = 1;
                        break;
                    }
                    output[output_count++] = (uint8_t)sym;
                    node = 0;
                }
            }
            uint32_t packed = node < 0 ? 0U : (uint32_t)node;
            packed |= (uint32_t)output[0] << 10;
            packed |= (uint32_t)output[1] << 18;
            packed |= (uint32_t)output_count << 26;
            packed |= (uint32_t)invalid << 28;
            huff_transitions[(size_t)state * 256 + byte] = packed;
        }
    }

    /* RFC 7541 permits only zero padding or a prefix of EOS: 1..7 one bits. */
    huff_final_valid[0] = 1;
    int node = 0;
    for (int depth = 1; depth < 8; depth++) {
        node = huff_tree[node].child[1];
        if (node < 0 || huff_tree[node].sym >= 0) break;
        huff_final_valid[node] = 1;
    }
    PyMem_Free(huff_tree);
    huff_tree = NULL;
    if (build_static_table() < 0) {
        wreath_hpack_free_huffman();
        return -1;
    }
    return 0;

no_memory:
    wreath_hpack_free_huffman();
    PyErr_NoMemory();
    return -1;
}

/* Decode with one precomputed transition per compressed byte. The shortest
 * HPACK code is five bits, so each byte emits at most two bytes and len*2+8 is
 * sufficient; the arithmetic is checked before allocation. */
static PyObject *
huffman_decode(const uint8_t *data, Py_ssize_t len, int *err)
{
    *err = 0;
    if (len > (PY_SSIZE_T_MAX - 8) / 2) {
        PyErr_NoMemory();
        return NULL;
    }
    Py_ssize_t out_cap = len * 2 + 8;
    uint8_t *out = PyMem_Malloc((size_t)out_cap);
    if (out == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    Py_ssize_t out_len = 0;
    int state = 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        uint32_t transition =
            huff_transitions[(size_t)state * 256 + data[i]];
        Py_ssize_t output_count = (Py_ssize_t)((transition >> 26) & 3U);
        if ((transition & (1U << 28)) != 0 ||
            out_len > out_cap - output_count) {
            *err = 1;
            PyMem_Free(out);
            return NULL;
        }
        if (output_count != 0) {
            out[out_len++] = (uint8_t)((transition >> 10) & 0xffU);
            if (output_count == 2)
                out[out_len++] = (uint8_t)((transition >> 18) & 0xffU);
        }
        state = (int)(transition & 0x3ffU);
    }
    if (state < 0 || state >= huff_state_count || !huff_final_valid[state]) {
        *err = 1;
        PyMem_Free(out);
        return NULL;
    }
    PyObject *result = PyBytes_FromStringAndSize((const char *)out, out_len);
    PyMem_Free(out);
    return result;
}

/* --- integer/string primitives (RFC 7541 s5) ----------------------------- */

static int
decode_integer(const uint8_t *data, Py_ssize_t len, Py_ssize_t *pos,
               int prefix_bits, uint64_t *out)
{
    if (*pos >= len) {
        return -1;
    }
    uint32_t max_prefix = (1u << prefix_bits) - 1u;
    uint64_t value = data[*pos] & max_prefix;
    (*pos)++;
    if (value < max_prefix) {
        *out = value;
        return 0;
    }
    unsigned shift = 0;
    for (;;) {
        if (*pos >= len) {
            return -1;
        }
        uint8_t byte = data[*pos];
        (*pos)++;
        value += (uint64_t)(byte & 0x7F) << shift;
        if (value > 0x7FFFFFFFULL) {
            return -1;
        }
        if (!(byte & 0x80)) {
            break;
        }
        shift += 7;
        if (shift > 28) {
            return -1;
        }
    }
    *out = value;
    return 0;
}

static PyObject *
decode_string(const uint8_t *data, Py_ssize_t len, Py_ssize_t *pos, int *err)
{
    *err = 0;
    if (*pos >= len) {
        *err = 1;
        return NULL;
    }
    int huffman = (data[*pos] & 0x80) != 0;
    uint64_t slen;
    if (decode_integer(data, len, pos, 7, &slen) < 0) {
        *err = 1;
        return NULL;
    }
    if (slen > (uint64_t)(len - *pos)) {
        *err = 1;
        return NULL;
    }
    const uint8_t *raw = data + *pos;
    *pos += (Py_ssize_t)slen;
    if (huffman) {
        return huffman_decode(raw, (Py_ssize_t)slen, err);
    }
    return PyBytes_FromStringAndSize((const char *)raw, (Py_ssize_t)slen);
}

/* --- dynamic table ------------------------------------------------------- */

int
wreath_hpack_table_init(WreathHpackTable *t, size_t hard_max)
{
    t->entries = NULL;
    t->cap = 0;
    t->count = 0;
    t->head = 0;
    t->size = 0;
    t->cur_max = hard_max;
    t->hard_max = hard_max;
    return 0;
}

/* Index of the k-th newest entry (k=0 newest) in the ring. */
static Py_ssize_t
ring_index(const WreathHpackTable *t, Py_ssize_t k)
{
    Py_ssize_t idx = (t->head - k) % t->cap;
    if (idx < 0) {
        idx += t->cap;
    }
    return idx;
}

void
wreath_hpack_table_clear(WreathHpackTable *t)
{
    if (t->entries != NULL) {
        for (Py_ssize_t i = 0; i < t->count; i++) {
            Py_ssize_t idx = ring_index(t, i);
            Py_CLEAR(t->entries[idx].name);
            Py_CLEAR(t->entries[idx].value);
        }
        PyMem_Free(t->entries);
        t->entries = NULL;
    }
    t->cap = t->count = t->head = 0;
    t->size = 0;
}

static void table_evict_to(WreathHpackTable *t, size_t target);
static void table_shrink_capacity(WreathHpackTable *t);

void
wreath_hpack_table_set_hard_max(WreathHpackTable *t, size_t hard_max)
{
    t->hard_max = hard_max;
    if (t->cur_max > hard_max) {
        t->cur_max = hard_max;
    }
    table_evict_to(t, t->cur_max);
    if ((hard_max == 0 && t->cap > 0) ||
        (t->cap > 16 && t->count <= t->cap / 4)) {
        table_shrink_capacity(t);
    }
}

static void
table_evict_to(WreathHpackTable *t, size_t target)
{
    while (t->count > 0 && t->size > target) {
        Py_ssize_t oldest = ring_index(t, t->count - 1);
        t->size -= t->entries[oldest].size;
        Py_CLEAR(t->entries[oldest].name);
        Py_CLEAR(t->entries[oldest].value);
        t->count--;
    }
}

static void
table_shrink_capacity(WreathHpackTable *t)
{
    Py_ssize_t target = t->cur_max == 0 ? 0 : 16;
    while (target < t->count) target *= 2;
    if (target >= t->cap) return;
    if (target == 0) {
        PyMem_Free(t->entries);
        t->entries = NULL;
        t->cap = t->head = 0;
        return;
    }
    WreathHpackEntry *fresh = PyMem_Calloc(
        (size_t)target, sizeof(WreathHpackEntry));
    if (fresh == NULL) return;  /* reclamation is best effort */
    for (Py_ssize_t k = 0; k < t->count; k++) {
        fresh[t->count - 1 - k] = t->entries[ring_index(t, k)];
    }
    PyMem_Free(t->entries);
    t->entries = fresh;
    t->cap = target;
    t->head = t->count == 0 ? 0 : t->count - 1;
}

/* Insert (name, value) as the newest entry (steals a reference to each). */
static int
table_add(WreathHpackTable *t, PyObject *name, PyObject *value)
{
    size_t entry_size = (size_t)PyBytes_GET_SIZE(name)
                        + (size_t)PyBytes_GET_SIZE(value) + 32;
    if (entry_size > t->cur_max) {
        table_evict_to(t, 0);  /* RFC 7541 s4.4: clears the whole table */
        Py_DECREF(name);
        Py_DECREF(value);
        return 0;
    }
    table_evict_to(t, t->cur_max - entry_size);
    if (t->count == t->cap) {
        Py_ssize_t new_cap = t->cap == 0 ? 16 : t->cap * 2;
        WreathHpackEntry *grown = PyMem_Malloc(sizeof(WreathHpackEntry) * (size_t)new_cap);
        if (grown == NULL) {
            Py_DECREF(name);
            Py_DECREF(value);
            PyErr_NoMemory();
            return -1;
        }
        /* Compact newest-first into [0..count); newest ends at index count-1. */
        for (Py_ssize_t i = 0; i < t->count; i++) {
            grown[t->count - 1 - i] = t->entries[ring_index(t, i)];
        }
        PyMem_Free(t->entries);
        t->entries = grown;
        t->cap = new_cap;
        t->head = t->count - 1;
    }
    t->head = (t->count == 0) ? 0 : (t->head + 1) % t->cap;
    t->entries[t->head].name = name;
    t->entries[t->head].value = value;
    t->entries[t->head].size = entry_size;
    t->size += entry_size;
    t->count++;
    return 0;
}

static int
table_get(WreathHpackTable *t, Py_ssize_t k, PyObject **name, PyObject **value)
{
    if (k < 0 || k >= t->count) {
        return -1;
    }
    Py_ssize_t idx = ring_index(t, k);
    *name = t->entries[idx].name;
    *value = t->entries[idx].value;
    return 0;
}

/* --- main decode --------------------------------------------------------- */

/* Resolve an index to (name, value) new refs. When `value` is NULL, only the
 * name is produced. Returns 0 ok, -1 invalid index, -2 python exception. */
static int
resolve_index(WreathHpackTable *t, uint64_t index, PyObject **name, PyObject **value)
{
    if (index == 0) {
        return -1;
    }
    if (index <= STATIC_TABLE_LEN) {
        /* Constant, cached at build time: hand out a reference, never re-encode. */
        *name = Py_NewRef(static_name_objects[index - 1]);
        if (value != NULL) {
            *value = Py_NewRef(static_value_objects[index - 1]);
        }
        return 0;
    }
    PyObject *dn, *dv;
    if (table_get(t, (Py_ssize_t)(index - STATIC_TABLE_LEN - 1), &dn, &dv) < 0) {
        return -1;
    }
    *name = Py_NewRef(dn);
    if (value != NULL) {
        *value = Py_NewRef(dv);
    }
    return 0;
}

int
wreath_hpack_decode(WreathHpackTable *t, const uint8_t *data, Py_ssize_t len,
                 Py_ssize_t max_header_count, Py_ssize_t max_header_list,
                 PyObject *out_list, int *h2_error)
{
    *h2_error = 0;
    Py_ssize_t pos = 0;
    Py_ssize_t header_count = 0;
    Py_ssize_t header_size = 0;
    int saw_header = 0;
    while (pos < len) {
        uint8_t first = data[pos];
        PyObject *name = NULL;
        PyObject *value = NULL;
        if (first & 0x80) {
            uint64_t index;
            if (decode_integer(data, len, &pos, 7, &index) < 0) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            int r = resolve_index(t, index, &name, &value);
            if (r == -2) {
                return -1;
            }
            if (r < 0) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
        } else if (first & 0x40) {
            uint64_t index;
            if (decode_integer(data, len, &pos, 6, &index) < 0) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            int err = 0;
            if (index == 0) {
                name = decode_string(data, len, &pos, &err);
                if (name == NULL) {
                    if (err) *h2_error = H2_COMPRESSION_ERROR;
                    return -1;
                }
            } else {
                int r = resolve_index(t, index, &name, NULL);
                if (r == -2) return -1;
                if (r < 0) { *h2_error = H2_COMPRESSION_ERROR; return -1; }
            }
            value = decode_string(data, len, &pos, &err);
            if (value == NULL) {
                Py_XDECREF(name);
                if (err) *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            if (table_add(t, Py_NewRef(name), Py_NewRef(value)) < 0) {
                Py_DECREF(name);
                Py_DECREF(value);
                return -1;
            }
        } else if (first & 0x20) {
            if (saw_header) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            uint64_t new_size;
            if (decode_integer(data, len, &pos, 5, &new_size) < 0) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            if (new_size > t->hard_max) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            t->cur_max = (size_t)new_size;
            table_evict_to(t, t->cur_max);
            continue;
        } else {
            uint64_t index;
            if (decode_integer(data, len, &pos, 4, &index) < 0) {
                *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
            int err = 0;
            if (index == 0) {
                name = decode_string(data, len, &pos, &err);
                if (name == NULL) {
                    if (err) *h2_error = H2_COMPRESSION_ERROR;
                    return -1;
                }
            } else {
                int r = resolve_index(t, index, &name, NULL);
                if (r == -2) return -1;
                if (r < 0) { *h2_error = H2_COMPRESSION_ERROR; return -1; }
            }
            value = decode_string(data, len, &pos, &err);
            if (value == NULL) {
                Py_XDECREF(name);
                if (err) *h2_error = H2_COMPRESSION_ERROR;
                return -1;
            }
        }
        saw_header = 1;
        Py_ssize_t name_size = PyBytes_GET_SIZE(name);
        Py_ssize_t value_size = PyBytes_GET_SIZE(value);
        if ((max_header_count > 0 && header_count >= max_header_count) ||
            name_size > PY_SSIZE_T_MAX - value_size - 32 ||
            (max_header_list > 0 &&
             header_size > max_header_list - (name_size + value_size + 32))) {
            Py_DECREF(name);
            Py_DECREF(value);
            *h2_error = H2_ENHANCE_YOUR_CALM;
            return -1;
        }
        header_count++;
        header_size += name_size + value_size + 32;
        int rc = wreath_headers_is_block(out_list)
            ? wreath_header_block_append_objects(out_list, name, value)
            : -1;
        Py_DECREF(name);
        Py_DECREF(value);
        if (rc < 0) {
            return -1;
        }
    }
    return 0;
}

/* --- response encoder ---------------------------------------------------- */

static int
huffman_encoded_len(const uint8_t *data, Py_ssize_t len)
{
    uint64_t bits = 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        bits += WREATH_HUFF[data[i]].nbits;
    }
    return (int)((bits + 7) / 8);
}

static int
append_hpack_integer(PyObject *out, uint64_t value, int prefix_bits, uint8_t flags)
{
    uint8_t buf[16];
    Py_ssize_t n = 0;
    uint32_t max_prefix = (1u << prefix_bits) - 1u;
    if (value < max_prefix) {
        buf[n++] = (uint8_t)(flags | value);
    } else {
        buf[n++] = (uint8_t)(flags | max_prefix);
        value -= max_prefix;
        while (value >= 128) {
            buf[n++] = (uint8_t)((value & 0x7F) | 0x80);
            value >>= 7;
        }
        buf[n++] = (uint8_t)value;
    }
    return append_raw(out, (const char *)buf, n);
}

static int
append_hpack_string(PyObject *out, const uint8_t *data, Py_ssize_t len)
{
    int hlen = huffman_encoded_len(data, len);
    if (hlen < len) {
        if (append_hpack_integer(out, (uint64_t)hlen, 7, 0x80) < 0) {
            return -1;
        }
        uint8_t *tmp = PyMem_Malloc((size_t)hlen + 1);
        if (tmp == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        uint64_t bits = 0;
        int nbits = 0;
        Py_ssize_t o = 0;
        for (Py_ssize_t i = 0; i < len; i++) {
            WreathHuffCode c = WREATH_HUFF[data[i]];
            bits = (bits << c.nbits) | c.code;
            nbits += c.nbits;
            while (nbits >= 8) {
                nbits -= 8;
                tmp[o++] = (uint8_t)((bits >> nbits) & 0xFF);
            }
        }
        if (nbits > 0) {
            tmp[o++] = (uint8_t)((bits << (8 - nbits)) | ((1u << (8 - nbits)) - 1));
        }
        int rc = append_raw(out, (const char *)tmp, o);
        PyMem_Free(tmp);
        return rc;
    }
    if (append_hpack_integer(out, (uint64_t)len, 7, 0x00) < 0) {
        return -1;
    }
    return append_raw(out, (const char *)data, len);
}

int
wreath_hpack_encode_literal(PyObject *out, const uint8_t *name, Py_ssize_t nlen,
                         const uint8_t *value, Py_ssize_t vlen)
{
    uint8_t zero = 0x00;  /* literal without indexing, new name */
    if (append_raw(out, (const char *)&zero, 1) < 0) {
        return -1;
    }
    if (append_hpack_string(out, name, nlen) < 0) {
        return -1;
    }
    return append_hpack_string(out, value, vlen);
}
