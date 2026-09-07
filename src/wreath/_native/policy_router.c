/* PolicyRouteTable: Wreath's one-pass policy matcher.
 *
 * The idea: inside one (method, segment-count) group, index the routes
 * 0..N-1 in priority order and precompile, per segment position, a bitset per
 * distinct literal value plus a bitset of the routes carrying a parameter
 * there. Matching intersects one mask per position:
 *
 *     survivors &= literal[p][seg] | param[p]
 *
 * A parameter contributes its bit at every position, so nothing folds into
 * every branch and the compiled form stays linear in the route count -- which
 * is the whole point, since the decision tree's parameter folding is what makes
 * it grow super-linearly.
 *
 * Positions are tested strongest-discriminator-first (the survival measure:
 * sum(size^2)/total^2 over the branches a position induces, parameters counted
 * into every branch), and the pass stops as soon as one route survives, which
 * is what keeps it competitive with the tree's early descent exit on small
 * tables. Positions that cannot discriminate at all are dropped at compile time.
 *
 * Access clauses are just more bitsets, so authorization is one more AND rather
 * than a separate pruning pass, and the winner is the lowest set bit -- routes
 * are indexed in (specificity, registration order), so bit order is priority
 * order and tie-breaking is a count-trailing-zeros.
 *
 * Keys live in an open-addressed table indexed by raw bytes, so matching never
 * builds a PyUnicode per segment. Where a position's literals can be told apart
 * by a few byte offsets, the key is those bytes rather than a hash of the whole
 * segment -- see MaskMap.
 */
#include "wreathcore.h"

#include <stddef.h>

#if defined(_MSC_VER)
#include <intrin.h>
#endif

static inline int
route_popcount64(uint64_t value)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_popcountll(value);
#else
    value = value - ((value >> 1) & UINT64_C(0x5555555555555555));
    value = (value & UINT64_C(0x3333333333333333)) +
            ((value >> 2) & UINT64_C(0x3333333333333333));
    value = (value + (value >> 4)) & UINT64_C(0x0f0f0f0f0f0f0f0f);
    return (int)((value * UINT64_C(0x0101010101010101)) >> 56);
#endif
}

static inline int
route_ctz64(uint64_t value)
{
#if defined(_MSC_VER)
    unsigned long index;
    _BitScanForward64(&index, value);
    return (int)index;
#else
    return __builtin_ctzll(value);
#endif
}

#define BRT_MAX_SEGMENTS 64
#define BRT_DISC_OFFS 4

typedef struct {
    char *bytes; /* NULL marks a parameter slot */
    Py_ssize_t len;
} BSeg;

typedef struct {
    BSeg *segs;
    int nseg;
    PyObject *method;      /* owned; groups are per (method, nseg) */
    PyObject *param_names; /* tuple[str, ...] */
    PyObject *handler;
    PyObject *access_clauses;
    /* access_clauses converted to machine words once at add(). NULL when a
     * clause does not fit (or is not an int); those routes keep the PyObject
     * loop, which raises at match time exactly as before. */
    unsigned long long *cmasks;
    Py_ssize_t ncmasks;
    int specificity;
    int order; /* registration order, for a stable tie-break */
} BRoute;

typedef struct {
    char *bytes;       /* NULL for a parameter or wildcard */
    Py_ssize_t len;
    PyObject *name;    /* owned for a named parameter, else NULL */
    int greedy;
} BDynamicSegment;

typedef struct {
    PyObject *method;
    PyObject *handler;
    PyObject *access_clauses;
    unsigned long long *cmasks;
    Py_ssize_t ncmasks;
    BDynamicSegment *path;
    Py_ssize_t npath;
    BDynamicSegment *host;
    Py_ssize_t nhost;
    int host_route;
    int greedy;
} BDynamicRoute;

typedef struct {
    Py_ssize_t *indices;
    Py_ssize_t count;
    Py_ssize_t capacity;
} BDynamicGroup;

/* Open-addressed map: literal bytes -> mask word offset. Avoids building a
 * PyUnicode per segment on the match path, which the dict-keyed tree must do.
 *
 * Two keyings, chosen per position at compile time:
 *
 *   noff == 0  exact. Key is FNV-1a over the whole segment, confirmed by a
 *              memcmp. Reads every byte, then ~60% of lookups read them again.
 *   noff > 0   discriminating bytes. Key is the length plus the up to four byte
 *              offsets that separate this position's literals, compared as one
 *              integer. No full hash, no memcmp. This is the usual case; the
 *              exact keying survives for literals that need more than four
 *              bytes or differ only past BRT_MAX_DISC_OFF.
 *
 * The inexact keying is sound because the scan fully verifies the winning
 * route's literals before accepting it, so the lookup only has to never drop a
 * true match -- it is allowed to admit false positives, which verify rejects.
 * A general hash table could not do this. `keys`/`lens` exist only for the
 * exact keying and are freed on conversion. */
typedef struct {
    char **keys;
    Py_ssize_t *lens;
    uint64_t *hashes;  /* exact: FNV-1a. discriminating: the packed key. */
    Py_ssize_t *slots; /* index into the group's word pool; -1 when empty */
    Py_ssize_t cap;
    Py_ssize_t count;
    /* Byte-wide: offsets are bounded by BRT_MAX_DISC_OFF, and one MaskMap per
     * segment position is walked per match, so the struct's stride is hot. */
    uint8_t off[4]; /* discriminating byte offsets; unused ones repeat off[0] */
    uint8_t noff;   /* 0 selects the exact keying */
} MaskMap;

typedef struct {
    Py_ssize_t nroutes;
    Py_ssize_t nwords;
    int nseg;
    BRoute *owned_routes; /* compact immutable records owned by this group */
    MaskMap *literal;  /* [nseg] */
    uint64_t *pool;    /* literal mask words, nwords each */
    Py_ssize_t pool_len;
    Py_ssize_t pool_cap;
    uint64_t *param;   /* [nseg * nwords] */
    uint64_t *public_mask; /* [nwords] */
    int *order;        /* positions worth testing, strongest first */
    int norder;
} BGroup;

/* Everything one method routes, in one place: its static-path dict and its
 * groups indexed by segment count. The match path resolves the whole struct
 * with one identity compare against the last method seen (methods are interned
 * strings, so the same pointer arrives on every request) or, on a cache miss,
 * one dict lookup on the method string -- no key object is ever built. `built`
 * also remembers (method, nseg) pairs that have no routes, so an unmatched
 * shape is a flag test rather than a rescan of every route. */
typedef struct {
    PyObject *statics; /* owned; the method's path -> entry dict, or NULL */
    BGroup *by_nseg[BRT_MAX_SEGMENTS + 1];
    uint8_t built[BRT_MAX_SEGMENTS + 1]; /* set once tried; NULL means no routes */
} MethodGroups;

/* Probe instrumentation. Compiled in by default: predictable increments on a
 * path already doing a hash, kept because the numbers decide whether a
 * different table design (Swiss-style control bytes, discriminating-byte keys)
 * is worth building. Per table -- not a process-global -- so tables do not
 * pollute each other's numbers and the hottest match path never writes a
 * cache line shared across every table (and, under free threading, every
 * thread). Read and reset through `probe_stats()`. Build with
 * -DWREATH_BRT_PROBES=0 to compile the counters out when pricing them. */
#ifndef WREATH_BRT_PROBES
#define WREATH_BRT_PROBES 1
#endif
#if WREATH_BRT_PROBES
#define BRT_PROBE(stmt) stmt
#else
#define BRT_PROBE(stmt) ((void)0)
#endif
typedef struct {
    uint64_t lookups;
    uint64_t buckets;      /* buckets examined, including the first */
    uint64_t key_compares; /* full memcmp calls actually made */
    uint64_t hits;
    uint64_t misses;
    uint64_t max_probe;
    uint64_t disc_lookups; /* lookups served by the discriminating-byte keying */
    uint64_t verify_routes;/* routes the scan fully verified */
    uint64_t verify_cmps;  /* literal segment compares inside verify */
    uint64_t dynamic_candidates;
} ProbeStats;

typedef struct {
    PyObject_HEAD
    BRoute *routes;
    Py_ssize_t nroutes;    /* mutable registration records still present */
    Py_ssize_t route_count; /* diagnostic count retained after sealing */
    Py_ssize_t routes_cap;
    PyObject *groups;  /* dict: method -> capsule of MethodGroups */
    PyObject *statics; /* dict: method -> dict: path -> (match_result, clauses) */
    /* One-entry method cache. `cached_method` is an owned reference, so a
     * caller's transient string can never dangle here and falsely hit on a
     * recycled address; `cached_mg` stays valid because the groups dict owns
     * the capsule until the dirty flag clears it, which also resets this. */
    PyObject *cached_method;
    MethodGroups *cached_mg;
    PyObject *get_method; /* owned per table; avoids process-global Python state */
    MethodGroups no_groups; /* built-empty unrouted-method sentinel */
    BDynamicRoute *dynamic_routes;
    Py_ssize_t ndynamic;
    Py_ssize_t dynamic_cap;
    PyObject *dynamic_groups[2];
    int dirty;
    int sealed;
    ProbeStats probe_stats; /* zeroed by tp_alloc; reset via probe_stats() */
} PolicyRouteTable;

typedef struct {
    Py_ssize_t offset;
    Py_ssize_t length;
} BParamSlice;

typedef struct {
    PyObject_VAR_HEAD
    PyObject *names;        /* owned tuple[str, ...] */
    PyObject *path;         /* owned Unicode object backing every slice */
    PyObject *cached;       /* one decoded value, or the materialized dict */
    Py_ssize_t cached_index; /* -1 empty; -2 means cached is the dict */
    BParamSlice slices[1];
} BPathParams;

static PyTypeObject BPathParamsType;

static PyObject *
path_params_decode(BPathParams *self, Py_ssize_t index,
                   const char *path, Py_ssize_t path_len)
{
    BParamSlice slice = self->slices[index];
    if (slice.offset < 0 || slice.length < 0 ||
        slice.offset > path_len - slice.length) {
        PyErr_SetString(PyExc_RuntimeError, "invalid lazy path-parameter slice");
        return NULL;
    }
    return PyUnicode_DecodeUTF8(
        path + slice.offset, slice.length, "surrogateescape");
}

static int
path_params_materialize(BPathParams *self)
{
    if (self->cached_index == -2) return 0;
    PyObject *result = PyDict_New();
    if (result == NULL) return -1;
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(self->path, &path_len);
    if (path == NULL) {
        Py_DECREF(result);
        return -1;
    }
    for (Py_ssize_t i = 0; i < Py_SIZE(self); i++) {
        PyObject *value = i == self->cached_index
            ? Py_NewRef(self->cached)
            : path_params_decode(self, i, path, path_len);
        if (value == NULL ||
            PyDict_SetItem(result, PyTuple_GET_ITEM(self->names, i), value) < 0) {
            Py_XDECREF(value);
            Py_DECREF(result);
            return -1;
        }
        Py_DECREF(value);
    }
    Py_XSETREF(self->cached, result);
    self->cached_index = -2;
    return 0;
}

static Py_ssize_t
path_params_length(PyObject *op)
{
    return Py_SIZE((BPathParams *)op);
}

static Py_ssize_t
path_params_find(BPathParams *self, PyObject *key)
{
    Py_hash_t key_hash = PyObject_Hash(key);
    if (key_hash == -1) return -1;
    for (Py_ssize_t i = 0; i < Py_SIZE(self); i++) {
        PyObject *name = PyTuple_GET_ITEM(self->names, i);
        Py_hash_t name_hash = PyObject_Hash(name);
        if (name_hash == -1) return -1;
        if (name_hash != key_hash) continue;
        int equal = PyObject_RichCompareBool(name, key, Py_EQ);
        if (equal < 0) return -1;
        if (equal) return i;
    }
    return -2;
}

static PyObject *
path_params_at(BPathParams *self, Py_ssize_t index)
{
    if (self->cached_index == index) return Py_NewRef(self->cached);
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(self->path, &path_len);
    if (path == NULL) return NULL;
    PyObject *value = path_params_decode(self, index, path, path_len);
    if (value == NULL) return NULL;
    Py_XSETREF(self->cached, Py_NewRef(value));
    self->cached_index = index;
    return value;
}

static PyObject *
path_params_subscript(PyObject *op, PyObject *key)
{
    BPathParams *self = (BPathParams *)op;
    if (self->cached_index == -2) {
        return PyObject_GetItem(self->cached, key);
    }
    Py_ssize_t index = path_params_find(self, key);
    if (index == -1) return NULL;
    if (index == -2) {
        PyErr_SetObject(PyExc_KeyError, key);
        return NULL;
    }
    return path_params_at(self, index);
}

int
wreath_path_param_utf8(PyObject *params, PyObject *key,
                       const char **text, Py_ssize_t *length)
{
    if (!Py_IS_TYPE(params, &BPathParamsType)) return 0;
    BPathParams *self = (BPathParams *)params;
    if (self->cached_index == -2) return 0;
    Py_ssize_t index = path_params_find(self, key);
    if (index == -1) return -1;
    if (index == -2) {
        PyErr_SetObject(PyExc_KeyError, key);
        return -1;
    }
    Py_ssize_t path_length;
    const char *path = PyUnicode_AsUTF8AndSize(self->path, &path_length);
    if (path == NULL) return -1;
    BParamSlice slice = self->slices[index];
    if (slice.offset < 0 || slice.length < 0 ||
        slice.offset > path_length - slice.length) {
        PyErr_SetString(PyExc_RuntimeError, "invalid lazy path-parameter slice");
        return -1;
    }
    *text = path + slice.offset;
    *length = slice.length;
    return 1;
}

static PyObject *
path_params_iter(PyObject *op)
{
    BPathParams *self = (BPathParams *)op;
    if (path_params_materialize(self) < 0) return NULL;
    return PyObject_GetIter(self->cached);
}

static PyObject *
path_params_richcompare(PyObject *left, PyObject *right, int operation)
{
    BPathParams *self = (BPathParams *)left;
    if (path_params_materialize(self) < 0) return NULL;
    if (Py_IS_TYPE(right, &BPathParamsType)) {
        BPathParams *other = (BPathParams *)right;
        if (path_params_materialize(other) < 0) return NULL;
        right = other->cached;
    }
    return PyObject_RichCompare(self->cached, right, operation);
}

static PyObject *
path_params_getattro(PyObject *op, PyObject *name)
{
    BPathParams *self = (BPathParams *)op;
    if (PyUnicode_Check(name) && PyUnicode_GetLength(name) == 3 &&
        PyUnicode_CompareWithASCIIString(name, "get") == 0) {
        return PyObject_GenericGetAttr(op, name);
    }
    if (path_params_materialize(self) < 0) return NULL;
    return PyObject_GetAttr(self->cached, name);
}

static PyObject *
path_params_get(BPathParams *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs < 1 || nargs > 2) {
        PyErr_Format(PyExc_TypeError,
                     "get expected one or two arguments, got %zd", nargs);
        return NULL;
    }
    if (self->cached_index == -2) {
        PyObject *value;
        int found = PyDict_GetItemRef(self->cached, args[0], &value);
        if (found < 0) return NULL;
        return found ? value : Py_NewRef(nargs == 2 ? args[1] : Py_None);
    }
    Py_ssize_t index = path_params_find(self, args[0]);
    if (index == -1) return NULL;
    return index == -2
        ? Py_NewRef(nargs == 2 ? args[1] : Py_None)
        : path_params_at(self, index);
}

static PyObject *
path_params_repr(PyObject *op)
{
    BPathParams *self = (BPathParams *)op;
    if (path_params_materialize(self) < 0) return NULL;
    return PyObject_Repr(self->cached);
}

static void
path_params_dealloc(BPathParams *self)
{
    Py_XDECREF(self->names);
    Py_XDECREF(self->path);
    Py_XDECREF(self->cached);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyMappingMethods path_params_mapping = {
    .mp_length = path_params_length,
    .mp_subscript = path_params_subscript,
};

static PyMethodDef path_params_methods[] = {
    {"get", (PyCFunction)(void (*)(void))path_params_get, METH_FASTCALL,
     "Return the value for key, or default when it is absent."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject BPathParamsType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core._PathParams",
    .tp_basicsize = offsetof(BPathParams, slices),
    .tp_itemsize = sizeof(BParamSlice),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    .tp_doc = "Lazy read-only path-parameter mapping.",
    .tp_dealloc = (destructor)path_params_dealloc,
    .tp_as_mapping = &path_params_mapping,
    .tp_iter = path_params_iter,
    .tp_richcompare = path_params_richcompare,
    .tp_getattro = path_params_getattro,
    .tp_methods = path_params_methods,
    .tp_repr = path_params_repr,
    .tp_hash = PyObject_HashNotImplemented,
};

/* ------------------------------------------------------------------ */

static uint64_t
hash_bytes(const char *p, Py_ssize_t n)
{
    uint64_t h = 1469598103934665603ULL; /* FNV-1a */
    for (Py_ssize_t i = 0; i < n; i++) {
        h ^= (unsigned char)p[i];
        h *= 1099511628211ULL;
    }
    return h ? h : 1;
}

/* The packed discriminating key. Must be a pure function of the segment bytes
 * and nothing else: that is what makes a false positive merely slow (verify
 * rejects it) rather than wrong. Offsets past the end read as 0, so a short
 * segment is still keyed, never out of bounds. Length is folded in because it
 * is already known and separates most literals for free. */
static inline uint64_t
disc_byte(const MaskMap *m, int i, const char *s, Py_ssize_t len)
{
    Py_ssize_t o = m->off[i];
    return (uint64_t)(unsigned char)(o < len ? s[o] : 0);
}

/* Two widths, because reading bytes a position does not need is not free: most
 * literals separate on one byte, and paying four reads for them gave back more
 * than half the win on word-like vocabularies. Build and lookup both route
 * through disc_key, so the width can never disagree between them. */
static inline uint64_t
disc_key(const MaskMap *m, const char *s, Py_ssize_t len)
{
    /* Length is folded in above the bytes, so literals of different lengths
     * cannot collide on their bytes alone. */
    if (m->noff <= 2) {
        return ((uint64_t)(len & 0xffff) << 16)
               | (disc_byte(m, 0, s, len) << 8)
               | disc_byte(m, 1, s, len);
    }
    return ((uint64_t)(len & 0xffff) << 32)
           | (disc_byte(m, 0, s, len) << 24)
           | (disc_byte(m, 1, s, len) << 16)
           | (disc_byte(m, 2, s, len) << 8)
           | disc_byte(m, 3, s, len);
}

/* Packed keys are dense and their low bits carry only the segment bytes, so the
 * bucket comes from a multiply-and-fold rather than from `key & mask`, which
 * would ignore the length entirely. */
static inline uint64_t
disc_mix(uint64_t k)
{
    k *= 0x9E3779B97F4A7C15ULL;
    return k ^ (k >> 29);
}

static int
maskmap_init(MaskMap *m, Py_ssize_t cap)
{
    m->keys = PyMem_Calloc((size_t)cap, sizeof(char *));
    m->lens = PyMem_Calloc((size_t)cap, sizeof(Py_ssize_t));
    m->hashes = PyMem_Calloc((size_t)cap, sizeof(uint64_t));
    m->slots = PyMem_Malloc((size_t)cap * sizeof(Py_ssize_t));
    if (m->keys == NULL || m->lens == NULL || m->hashes == NULL || m->slots == NULL) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < cap; i++) m->slots[i] = -1;
    m->cap = cap;
    m->count = 0;
    m->noff = 0; /* exact until separating offsets are found */
    memset(m->off, 0, sizeof(m->off));
    return 0;
}

static void
maskmap_clear(MaskMap *m)
{
    if (m->keys != NULL) {
        for (Py_ssize_t i = 0; i < m->cap; i++) PyMem_Free(m->keys[i]);
    }
    PyMem_Free(m->keys);
    PyMem_Free(m->lens);
    PyMem_Free(m->hashes);
    PyMem_Free(m->slots);
    memset(m, 0, sizeof(*m));
}

static int
maskmap_grow(MaskMap *m)
{
    if (m->cap > PY_SSIZE_T_MAX / (2 * (Py_ssize_t)sizeof(uint64_t))) return -1;
    MaskMap grown = {0};
    Py_ssize_t cap = m->cap ? m->cap * 2 : 8;
    if (maskmap_init(&grown, cap) < 0) {
        maskmap_clear(&grown);
        return -1;
    }
    Py_ssize_t mask = cap - 1;
    for (Py_ssize_t old = 0; old < m->cap; old++) {
        if (m->slots[old] < 0) continue;
        Py_ssize_t slot = (Py_ssize_t)(m->hashes[old] & (uint64_t)mask);
        while (grown.slots[slot] >= 0) slot = (slot + 1) & mask;
        grown.keys[slot] = m->keys[old];
        grown.lens[slot] = m->lens[old];
        grown.hashes[slot] = m->hashes[old];
        grown.slots[slot] = m->slots[old];
    }
    grown.count = m->count;
    PyMem_Free(m->keys);
    PyMem_Free(m->lens);
    PyMem_Free(m->hashes);
    PyMem_Free(m->slots);
    *m = grown;
    return 0;
}

/* Returns the slot index for `key`, inserting `fresh_slot` when absent. */
static Py_ssize_t
maskmap_intern(MaskMap *m, const char *key, Py_ssize_t len, Py_ssize_t fresh_slot,
               int *inserted)
{
    if (m->cap == 0 && maskmap_grow(m) < 0) {
        *inserted = -1;
        return -1;
    }
    uint64_t h = hash_bytes(key, len);
    Py_ssize_t mask = m->cap - 1;
    Py_ssize_t i = (Py_ssize_t)(h & (uint64_t)mask);
    for (;;) {
        if (m->slots[i] < 0) {
            if (m->count >= m->cap / 2) {
                if (maskmap_grow(m) < 0) { *inserted = -1; return -1; }
                mask = m->cap - 1;
                i = (Py_ssize_t)(h & (uint64_t)mask);
                while (m->slots[i] >= 0) i = (i + 1) & mask;
            }
            char *copy = PyMem_Malloc((size_t)len ? (size_t)len : 1);
            if (copy == NULL) { *inserted = -1; return -1; }
            memcpy(copy, key, (size_t)len);
            m->keys[i] = copy;
            m->lens[i] = len;
            m->hashes[i] = h;
            m->slots[i] = fresh_slot;
            m->count++;
            *inserted = 1;
            return fresh_slot;
        }
        if (m->hashes[i] == h && m->lens[i] == len &&
            memcmp(m->keys[i], key, (size_t)len) == 0) {
            *inserted = 0;
            return m->slots[i];
        }
        i = (i + 1) & mask;
    }
}

#define BRT_MAX_DISC_OFF 24

/* Offsets at or past noff repeat off[0], so the key reads a byte it has already
 * read rather than branching on how many offsets are in use. */
static void
disc_fill(MaskMap *m)
{
    for (int i = m->noff; i < BRT_DISC_OFFS; i++) m->off[i] = m->off[0];
}

/* How many of the `d` distinct literals get distinct packed keys under `m`'s
 * current offsets? `d` means they are fully separated. `seen` is scratch of
 * power-of-two capacity `scap`. */
static Py_ssize_t
disc_classes(const MaskMap *m, char *const *keys, const Py_ssize_t *lens,
             Py_ssize_t d, uint64_t *seen, Py_ssize_t scap)
{
    memset(seen, 0, (size_t)scap * sizeof(uint64_t));
    Py_ssize_t smask = scap - 1, classes = 0;
    for (Py_ssize_t e = 0; e < d; e++) {
        uint64_t k = disc_key(m, keys[e], lens[e]);
        Py_ssize_t i = (Py_ssize_t)(disc_mix(k) & (uint64_t)smask);
        for (;;) {
            if (seen[i] == 0) { seen[i] = k + 1; classes++; break; } /* 0: empty */
            if (seen[i] == k + 1) break; /* two literals share one key */
            i = (i + 1) & smask;
        }
    }
    return classes;
}

/* Pick byte offsets separating every literal at this position, fewest first.
 * Returns 1 with noff/off set, 0 when no set of BRT_DISC_OFFS offsets within
 * BRT_MAX_DISC_OFF separates them and the exact keying has to stay.
 *
 * Greedy: repeatedly add the offset that splits the most literals apart. The
 * optimal subset would be exponential to find and this is a compile-time
 * heuristic over a couple of dozen literals; a miss costs the exact fallback,
 * not correctness.
 *
 * Separation is demanded rather than merely preferred. Inexact keys stay sound
 * -- verify rejects the false positives -- but they would put extra routes
 * through verify, which is the cost this is trying to remove. Falling back to
 * the exact keying bounds the worst case at the current behaviour. */
static int
disc_choose(MaskMap *m, char *const *keys, const Py_ssize_t *lens, Py_ssize_t d,
            uint64_t *seen, Py_ssize_t scap)
{
    Py_ssize_t maxlen = 1;
    for (Py_ssize_t e = 0; e < d; e++) {
        if (lens[e] > maxlen) maxlen = lens[e];
    }
    if (maxlen > BRT_MAX_DISC_OFF) maxlen = BRT_MAX_DISC_OFF;
    memset(m->off, 0, sizeof(m->off));
    Py_ssize_t best = 0;
    for (int step = 0; step < BRT_DISC_OFFS; step++) {
        m->noff = (uint8_t)(step + 1);
        int pick = -1;
        Py_ssize_t pick_classes = best;
        for (Py_ssize_t o = 0; o < maxlen; o++) {
            m->off[step] = (uint8_t)o; /* maxlen <= BRT_MAX_DISC_OFF */
            disc_fill(m);
            Py_ssize_t classes = disc_classes(m, keys, lens, d, seen, scap);
            if (classes > pick_classes) {
                pick_classes = classes;
                pick = (int)o;
            }
        }
        if (pick < 0) break; /* no remaining offset splits anything further */
        m->off[step] = (uint8_t)pick;
        disc_fill(m);
        best = pick_classes;
        if (best == d) return 1;
    }
    m->noff = 0;
    memset(m->off, 0, sizeof(m->off));
    return 0;
}

/* Rebuild the table under the chosen offsets, mapping each literal to the pool
 * slot it already owns. 1 converted, 0 refused, -1 out of memory. */
static int
maskmap_rekey(MaskMap *m, char **keys, Py_ssize_t *lens, Py_ssize_t *slots,
              Py_ssize_t d)
{
    uint64_t *hashes = PyMem_Calloc((size_t)m->cap, sizeof(uint64_t));
    Py_ssize_t *fresh = PyMem_Malloc((size_t)m->cap * sizeof(Py_ssize_t));
    if (hashes == NULL || fresh == NULL) {
        PyMem_Free(hashes);
        PyMem_Free(fresh);
        return -1;
    }
    for (Py_ssize_t i = 0; i < m->cap; i++) fresh[i] = -1;
    Py_ssize_t mask = m->cap - 1;
    for (Py_ssize_t e = 0; e < d; e++) {
        uint64_t k = disc_key(m, keys[e], lens[e]);
        Py_ssize_t i = (Py_ssize_t)(disc_mix(k) & (uint64_t)mask);
        while (fresh[i] >= 0) {
            if (hashes[i] == k) {
                /* disc_choose proved the keys distinct. Colliding here would
                 * merge two literals onto one slot and silently drop a route's
                 * bit, so refuse the conversion rather than trust the proof. */
                PyMem_Free(hashes);
                PyMem_Free(fresh);
                return 0;
            }
            i = (i + 1) & mask;
        }
        hashes[i] = k;
        fresh[i] = slots[e];
    }
    for (Py_ssize_t i = 0; i < m->cap; i++) PyMem_Free(m->keys[i]);
    PyMem_Free(m->keys);
    PyMem_Free(m->lens);
    PyMem_Free(m->hashes);
    PyMem_Free(m->slots);
    m->keys = NULL; /* the literals live on in the routes; verify reads them */
    m->lens = NULL;
    m->hashes = hashes;
    m->slots = fresh;
    return 1;
}

/* Convert one built exact map to discriminating-byte keying where possible.
 * 1 converted, 0 kept exact, -1 out of memory. */
static int
maskmap_try_disc(MaskMap *m)
{
    if (m->count == 0) return 0;
    Py_ssize_t d = m->count;
    Py_ssize_t scap = 8;
    while (scap < d * 2) scap *= 2;
    char **keys = PyMem_Malloc((size_t)d * sizeof(char *));
    Py_ssize_t *lens = PyMem_Malloc((size_t)d * sizeof(Py_ssize_t));
    Py_ssize_t *slots = PyMem_Malloc((size_t)d * sizeof(Py_ssize_t));
    uint64_t *seen = PyMem_Malloc((size_t)scap * sizeof(uint64_t));
    Py_ssize_t n = 0;
    int rc = -1;
    if (keys == NULL || lens == NULL || slots == NULL || seen == NULL) goto done;
    for (Py_ssize_t i = 0; i < m->cap; i++) {
        if (m->slots[i] < 0) continue;
        keys[n] = m->keys[i];
        lens[n] = m->lens[i];
        slots[n] = m->slots[i];
        n++;
    }
    rc = disc_choose(m, keys, lens, n, seen, scap)
             ? maskmap_rekey(m, keys, lens, slots, n)
             : 0;
done:
    if (rc != 1) {
        m->noff = 0; /* any refusal leaves the exact table and must key it so */
        memset(m->off, 0, sizeof(m->off));
    }
    PyMem_Free(keys);
    PyMem_Free(lens);
    PyMem_Free(slots);
    PyMem_Free(seen);
    return rc;
}

static inline void
probe_record(ProbeStats *ps, uint64_t examined, int hit)
{
#if WREATH_BRT_PROBES
    ps->buckets += examined;
    if (hit) ps->hits++;
    else ps->misses++;
    if (examined > ps->max_probe) ps->max_probe = examined;
#else
    (void)ps;
    (void)examined;
    (void)hit;
#endif
}

/* Exact keying: hash every byte, then confirm with a memcmp. */
static inline Py_ssize_t
maskmap_get_exact(const MaskMap *m, const char *key, Py_ssize_t len,
                  ProbeStats *ps)
{
    uint64_t h = hash_bytes(key, len);
    Py_ssize_t mask = m->cap - 1;
    Py_ssize_t i = (Py_ssize_t)(h & (uint64_t)mask);
    uint64_t examined = 0;
    for (;;) {
        examined++;
        if (m->slots[i] < 0) { probe_record(ps, examined, 0); return -1; }
        if (m->hashes[i] == h && m->lens[i] == len) {
            BRT_PROBE(ps->key_compares++);
            if (memcmp(m->keys[i], key, (size_t)len) == 0) {
                probe_record(ps, examined, 1);
                return m->slots[i];
            }
        }
        i = (i + 1) & mask;
    }
}

/* Discriminating-byte keying: read length and one or two bytes, compare as one
 * integer. A hit is not proof the segment equals the literal -- verify decides
 * that -- it is only proof that no true match was dropped. */
static inline Py_ssize_t
maskmap_get_disc(const MaskMap *m, const char *key, Py_ssize_t len,
                 ProbeStats *ps)
{
    uint64_t k = disc_key(m, key, len);
    Py_ssize_t mask = m->cap - 1;
    Py_ssize_t i = (Py_ssize_t)(disc_mix(k) & (uint64_t)mask);
    uint64_t examined = 0;
    BRT_PROBE(ps->disc_lookups++);
    for (;;) {
        examined++;
        if (m->slots[i] < 0) { probe_record(ps, examined, 0); return -1; }
        if (m->hashes[i] == k) { probe_record(ps, examined, 1); return m->slots[i]; }
        i = (i + 1) & mask;
    }
}

static Py_ssize_t
maskmap_get(const MaskMap *m, const char *key, Py_ssize_t len, ProbeStats *ps)
{
    if (m->cap == 0) return -1;
    BRT_PROBE(ps->lookups++);
    if (m->noff > 0) return maskmap_get_disc(m, key, len, ps);
    return maskmap_get_exact(m, key, len, ps);
}

static void
broute_clear(BRoute *route)
{
    PyMem_Free(route->segs);
    PyMem_Free(route->cmasks);
    Py_XDECREF(route->method);
    Py_XDECREF(route->param_names);
    Py_XDECREF(route->handler);
    Py_XDECREF(route->access_clauses);
    memset(route, 0, sizeof(*route));
}

static int
broute_clone(BRoute *target, const BRoute *source)
{
    *target = *source;
    target->segs = NULL;
    target->cmasks = NULL;
    target->method = Py_XNewRef(source->method);
    target->param_names = Py_XNewRef(source->param_names);
    target->handler = Py_XNewRef(source->handler);
    target->access_clauses = Py_XNewRef(source->access_clauses);

    Py_ssize_t bytes_needed = 0;
    for (int i = 0; i < source->nseg; i++) bytes_needed += source->segs[i].len;
    target->segs = PyMem_Malloc(
        (size_t)source->nseg * sizeof(BSeg) + (size_t)bytes_needed);
    if (target->segs == NULL) goto fail;
    char *blob = (char *)(target->segs + source->nseg);
    for (int i = 0; i < source->nseg; i++) {
        target->segs[i].len = source->segs[i].len;
        if (source->segs[i].bytes == NULL) {
            target->segs[i].bytes = NULL;
        }
        else {
            memcpy(blob, source->segs[i].bytes, (size_t)source->segs[i].len);
            target->segs[i].bytes = blob;
            blob += source->segs[i].len;
        }
    }
    if (source->ncmasks > 0 && source->cmasks != NULL) {
        target->cmasks = PyMem_Malloc(
            (size_t)source->ncmasks * sizeof(unsigned long long));
        if (target->cmasks == NULL) goto fail;
        memcpy(target->cmasks, source->cmasks,
               (size_t)source->ncmasks * sizeof(unsigned long long));
    }
    return 0;

fail:
    broute_clear(target);
    return -1;
}

static void
bgroup_free(BGroup *g)
{
    if (g == NULL) return;
    if (g->literal != NULL) {
        for (int p = 0; p < g->nseg; p++) maskmap_clear(&g->literal[p]);
        PyMem_Free(g->literal);
    }
    PyMem_Free(g->pool);
    PyMem_Free(g->param);
    PyMem_Free(g->public_mask);
    PyMem_Free(g->order);
    if (g->owned_routes != NULL) {
        for (Py_ssize_t i = 0; i < g->nroutes; i++) broute_clear(&g->owned_routes[i]);
        PyMem_Free(g->owned_routes);
    }
    PyMem_Free(g);
}

static void
methodgroups_capsule_free(PyObject *capsule)
{
    MethodGroups *mg =
        (MethodGroups *)PyCapsule_GetPointer(capsule, "brt.methodgroups");
    if (mg == NULL) return;
    Py_XDECREF(mg->statics);
    for (int i = 0; i <= BRT_MAX_SEGMENTS; i++) bgroup_free(mg->by_nseg[i]);
    PyMem_Free(mg);
}

static Py_ssize_t
pool_alloc(BGroup *g)
{
    if (g->pool_len + g->nwords > g->pool_cap) {
        Py_ssize_t cap = g->pool_cap ? g->pool_cap * 2 : g->nwords * 8;
        while (cap < g->pool_len + g->nwords) cap *= 2;
        uint64_t *grown = PyMem_Realloc(g->pool, (size_t)cap * sizeof(uint64_t));
        if (grown == NULL) return -1;
        memset(grown + g->pool_cap, 0,
               (size_t)(cap - g->pool_cap) * sizeof(uint64_t));
        g->pool = grown;
        g->pool_cap = cap;
    }
    Py_ssize_t at = g->pool_len;
    g->pool_len += g->nwords;
    return at;
}

static int
route_priority_cmp(const void *a, const void *b)
{
    const BRoute *x = *(const BRoute **)a;
    const BRoute *y = *(const BRoute **)b;
    if (x->specificity != y->specificity) return y->specificity - x->specificity;
    return x->order - y->order;
}

/* Survival of position p: sum(size^2)/total^2 with parameters folded into every
 * branch. Lower discriminates more. Returns 1.0 when the position cannot
 * narrow anything. */
static double
position_survival(BGroup *g, int p)
{
    MaskMap *m = &g->literal[p];
    if (m->count == 0) return 1.0;
    Py_ssize_t params = 0;
    for (Py_ssize_t w = 0; w < g->nwords; w++) {
        params += route_popcount64(g->param[(Py_ssize_t)p * g->nwords + w]);
    }
    double total = 0.0, sq = 0.0;
    for (Py_ssize_t i = 0; i < m->cap; i++) {
        if (m->slots[i] < 0) continue;
        Py_ssize_t n = 0;
        for (Py_ssize_t w = 0; w < g->nwords; w++) {
            n += route_popcount64(g->pool[m->slots[i] + w]);
        }
        double size = (double)(n + params);
        total += size;
        sq += size * size;
    }
    if (params) {
        total += (double)params;
        sq += (double)params * (double)params;
    }
    if (total <= 0.0) return 1.0;
    return sq / (total * total);
}

static int
bgroup_plan(BGroup *g)
{
    g->order = PyMem_Malloc((size_t)g->nseg * sizeof(int));
    if (g->order == NULL) return -1;
    double score[BRT_MAX_SEGMENTS];
    int n = 0;
    for (int p = 0; p < g->nseg; p++) {
        double s = position_survival(g, p);
        if (s >= 1.0) {
            /* An all-param position accepts any segment: probing it is a true
             * no-op. A position with literals scoring 1.0 (every route shares
             * one literal, e.g. a common '/api' prefix segment) narrows
             * nothing among hits but rejects the whole group on a miss --
             * keep it, ordered last, so a matched request usually early-exits
             * before paying its probe while a request missing only there is
             * filtered instead of falling through to per-route verification. */
            if (g->literal[p].count == 0) continue;
            s = 1.0;
        }
        score[n] = s;
        g->order[n] = p;
        n++;
    }
    /* Insertion sort: nseg is tiny and this runs once per group at compile. */
    for (int i = 1; i < n; i++) {
        double s = score[i];
        int p = g->order[i];
        int j = i - 1;
        while (j >= 0 && score[j] > s) {
            score[j + 1] = score[j];
            g->order[j + 1] = g->order[j];
            j--;
        }
        score[j + 1] = s;
        g->order[j + 1] = p;
    }
    g->norder = n;
    return 0;
}

static BGroup *
bgroup_build(BRoute **cands, Py_ssize_t ncand, int nseg)
{
    BGroup *g = PyMem_Calloc(1, sizeof(BGroup));
    if (g == NULL) return NULL;
    g->nseg = nseg;
    g->nroutes = ncand;
    g->nwords = (ncand + 63) / 64;

    g->owned_routes = PyMem_Calloc((size_t)ncand, sizeof(BRoute));
    if (g->owned_routes == NULL) goto fail;
    qsort(cands, (size_t)ncand, sizeof(BRoute *), route_priority_cmp);
    for (Py_ssize_t i = 0; i < ncand; i++) {
        if (broute_clone(&g->owned_routes[i], cands[i]) < 0) goto fail;
    }

    g->literal = PyMem_Calloc((size_t)nseg, sizeof(MaskMap));
    g->param = PyMem_Calloc((size_t)nseg * (size_t)g->nwords, sizeof(uint64_t));
    g->public_mask = PyMem_Calloc((size_t)g->nwords, sizeof(uint64_t));
    if (g->literal == NULL || g->param == NULL || g->public_mask == NULL) goto fail;
    for (Py_ssize_t i = 0; i < ncand; i++) {
        BRoute *r = &g->owned_routes[i];
        Py_ssize_t word = i / 64;
        uint64_t bit = 1ULL << (i % 64);
        for (int p = 0; p < nseg; p++) {
            if (r->segs[p].bytes == NULL) {
                g->param[(Py_ssize_t)p * g->nwords + word] |= bit;
                continue;
            }
            Py_ssize_t fresh = pool_alloc(g);
            if (fresh < 0) goto fail;
            int inserted = 0;
            Py_ssize_t slot = maskmap_intern(&g->literal[p], r->segs[p].bytes,
                                             r->segs[p].len, fresh, &inserted);
            if (inserted < 0) goto fail;
            if (!inserted) g->pool_len -= g->nwords; /* value already present */
            g->pool[slot + word] |= bit;
        }
        for (Py_ssize_t c = 0; c < PyTuple_GET_SIZE(r->access_clauses); c++) {
            PyObject *clause = PyTuple_GET_ITEM(r->access_clauses, c);
            int is_zero = PyObject_Not(clause);
            if (is_zero < 0) goto fail;
            if (is_zero) {
                g->public_mask[word] |= bit;
                break;
            }
        }
    }
    /* Rekey each position on its discriminating bytes now that the literals are
     * known. Survival is unaffected: separation is exact, so the branches a
     * position induces are the same ones. */
    for (int p = 0; p < nseg; p++) {
        if (maskmap_try_disc(&g->literal[p]) < 0) { PyErr_NoMemory(); goto fail; }
    }
    if (bgroup_plan(g) < 0) goto fail;
    return g;

fail:
    /* Most failures here are allocation failures that did not raise; a bare
     * NULL would read as "no group" and turn OOM into a silent 404. */
    if (!PyErr_Occurred()) PyErr_NoMemory();
    bgroup_free(g);
    return NULL;
}

/* ------------------------------------------------------------------ */

static Py_ssize_t
brt_scan(const char *path, Py_ssize_t len, BSeg *out)
{
    const char *p = path + 1;
    const char *end = path + len;
    const char *start = p;
    Py_ssize_t count = 0;
    for (;; p++) {
        if (p == end || *p == '/') {
            if (count == BRT_MAX_SEGMENTS) return -1;
            out[count].bytes = (char *)start;
            out[count].len = p - start;
            count++;
            if (p == end) return count;
            start = p + 1;
        }
    }
}

static void
dynamic_route_clear(BDynamicRoute *route)
{
    for (Py_ssize_t i = 0; i < route->npath; i++) {
        PyMem_Free(route->path[i].bytes);
        Py_XDECREF(route->path[i].name);
    }
    for (Py_ssize_t i = 0; i < route->nhost; i++) {
        PyMem_Free(route->host[i].bytes);
        Py_XDECREF(route->host[i].name);
    }
    PyMem_Free(route->path);
    PyMem_Free(route->host);
    PyMem_Free(route->cmasks);
    Py_XDECREF(route->method);
    Py_XDECREF(route->handler);
    Py_XDECREF(route->access_clauses);
    memset(route, 0, sizeof(*route));
}

static void
dynamic_group_capsule_free(PyObject *capsule)
{
    BDynamicGroup *group = PyCapsule_GetPointer(capsule, "brt.dynamicgroup");
    if (group == NULL) {
        PyErr_Clear();
        return;
    }
    PyMem_Free(group->indices);
    PyMem_Free(group);
}

static BDynamicGroup *
dynamic_group_lookup(PolicyRouteTable *self, int host_route, PyObject *method)
{
    PyObject *capsule = PyDict_GetItemWithError(
        self->dynamic_groups[host_route], method);
    if (capsule == NULL) return NULL;
    return PyCapsule_GetPointer(capsule, "brt.dynamicgroup");
}

static int
dynamic_groups_build(PolicyRouteTable *self)
{
    PyDict_Clear(self->dynamic_groups[0]);
    PyDict_Clear(self->dynamic_groups[1]);
    for (Py_ssize_t index = 0; index < self->ndynamic; index++) {
        BDynamicRoute *route = &self->dynamic_routes[index];
        PyObject *groups = self->dynamic_groups[route->host_route];
        PyObject *capsule = PyDict_GetItemWithError(groups, route->method);
        BDynamicGroup *group;
        if (capsule != NULL) {
            group = PyCapsule_GetPointer(capsule, "brt.dynamicgroup");
            if (group == NULL) return -1;
        } else {
            if (PyErr_Occurred()) return -1;
            group = PyMem_Calloc(1, sizeof(*group));
            if (group == NULL) {
                PyErr_NoMemory();
                return -1;
            }
            capsule = PyCapsule_New(
                group, "brt.dynamicgroup", dynamic_group_capsule_free);
            if (capsule == NULL || PyDict_SetItem(groups, route->method, capsule) < 0) {
                Py_XDECREF(capsule);
                if (capsule == NULL) {
                    PyMem_Free(group);
                }
                return -1;
            }
            Py_DECREF(capsule);
        }
        if (group->count == group->capacity) {
            if (group->capacity > PY_SSIZE_T_MAX / 2) {
                PyErr_NoMemory();
                return -1;
            }
            Py_ssize_t capacity = group->capacity == 0 ? 8 : group->capacity * 2;
            if ((size_t)capacity > SIZE_MAX / sizeof(*group->indices)) {
                PyErr_NoMemory();
                return -1;
            }
            Py_ssize_t *indices = PyMem_Realloc(
                group->indices, (size_t)capacity * sizeof(*indices));
            if (indices == NULL) {
                PyErr_NoMemory();
                return -1;
            }
            group->indices = indices;
            group->capacity = capacity;
        }
        group->indices[group->count++] = index;
    }
    return 0;
}

static int
dynamic_segment_compile(BDynamicSegment *target, const char *text,
                        Py_ssize_t len, int host)
{
    memset(target, 0, sizeof(*target));
    if (host && len == 1 && text[0] == '*') return 0;
    if (len >= 2 && text[0] == '{' && text[len - 1] == '}') {
        Py_ssize_t name_len = len - 2;
        if (!host && name_len > 5 &&
            memcmp(text + 1 + name_len - 5, ":path", 5) == 0) {
            target->greedy = 1;
            name_len -= 5;
        }
        target->name = PyUnicode_DecodeUTF8(text + 1, name_len, "strict");
        return target->name == NULL ? -1 : 0;
    }
    target->bytes = PyMem_Malloc((size_t)(len == 0 ? 1 : len));
    if (target->bytes == NULL) return -1;
    memcpy(target->bytes, text, (size_t)len);
    target->len = len;
    return 0;
}

static int
dynamic_path_compile(BDynamicRoute *route, const char *path, Py_ssize_t len)
{
    BSeg raw[BRT_MAX_SEGMENTS];
    Py_ssize_t count = brt_scan(path, len, raw);
    if (count < 0) {
        PyErr_SetString(PyExc_ValueError, "dynamic route path has too many segments");
        return -1;
    }
    route->path = PyMem_Calloc((size_t)count, sizeof(*route->path));
    if (route->path == NULL) return -1;
    route->npath = count;
    for (Py_ssize_t i = 0; i < count; i++) {
        if (dynamic_segment_compile(
                &route->path[i], raw[i].bytes, raw[i].len, 0) < 0) return -1;
        if (route->path[i].greedy) {
            if (i != count - 1) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "a dynamic {name:path} placeholder must be the final segment");
                return -1;
            }
            route->greedy = 1;
        }
    }
    return 0;
}

static int
dynamic_host_compile(BDynamicRoute *route, const char *host, Py_ssize_t len)
{
    Py_ssize_t count = 1;
    for (Py_ssize_t i = 0; i < len; i++) count += host[i] == '.';
    route->host = PyMem_Calloc((size_t)count, sizeof(*route->host));
    if (route->host == NULL) return -1;
    route->nhost = count;
    Py_ssize_t index = 0;
    const char *start = host;
    for (const char *cursor = host;; cursor++) {
        if (cursor == host + len || *cursor == '.') {
            if (dynamic_segment_compile(
                    &route->host[index++], start, cursor - start, 1) < 0) return -1;
            if (cursor == host + len) break;
            start = cursor + 1;
        }
    }
    return 0;
}

static int
dynamic_compile_masks(BDynamicRoute *route)
{
    Py_ssize_t count = PyTuple_GET_SIZE(route->access_clauses);
    if (count == 0) return 0;
    route->cmasks = PyMem_Malloc((size_t)count * sizeof(*route->cmasks));
    if (route->cmasks == NULL) return -1;
    for (Py_ssize_t index = 0; index < count; index++) {
        route->cmasks[index] = PyLong_AsUnsignedLongLong(
            PyTuple_GET_ITEM(route->access_clauses, index));
        if (route->cmasks[index] == (unsigned long long)-1 && PyErr_Occurred()) {
            if (!PyErr_ExceptionMatches(PyExc_OverflowError)) return -1;
            PyErr_Clear();
            PyMem_Free(route->cmasks);
            route->cmasks = NULL;
            route->ncmasks = 0;
            return 0;
        }
    }
    route->ncmasks = count;
    return 0;
}

static int
validate_access_clauses(PyObject *clauses)
{
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(clauses); index++) {
        PyObject *clause = PyTuple_GET_ITEM(clauses, index);
        int sign = 0;
        if (!PyLong_CheckExact(clause) ||
            PyLong_GetSign(clause, &sign) < 0 || sign < 0) {
            if (PyErr_Occurred()) return -1;
            PyErr_Format(
                PyExc_ValueError,
                "access_clauses[%zd] must be a non-negative int",
                index);
            return -1;
        }
    }
    return 0;
}

static PyObject *
brt_new(PyTypeObject *type, PyObject *Py_UNUSED(a), PyObject *Py_UNUSED(k))
{
    PolicyRouteTable *self = (PolicyRouteTable *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    memset(self->no_groups.built, 1, sizeof(self->no_groups.built));
    self->groups = PyDict_New();
    self->statics = PyDict_New();
    self->dynamic_groups[0] = PyDict_New();
    self->dynamic_groups[1] = PyDict_New();
    self->get_method = PyUnicode_InternFromString("GET");
    if (self->groups == NULL || self->statics == NULL ||
        self->dynamic_groups[0] == NULL || self->dynamic_groups[1] == NULL ||
        self->get_method == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    self->dirty = 1;
    self->sealed = 0;
    return (PyObject *)self;
}

static void
brt_dealloc(PolicyRouteTable *self)
{
    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        broute_clear(&self->routes[i]);
    }
    PyMem_Free(self->routes);
    for (Py_ssize_t i = 0; i < self->ndynamic; i++) {
        dynamic_route_clear(&self->dynamic_routes[i]);
    }
    PyMem_Free(self->dynamic_routes);
    Py_XDECREF(self->dynamic_groups[0]);
    Py_XDECREF(self->dynamic_groups[1]);
    Py_XDECREF(self->cached_method);
    Py_XDECREF(self->get_method);
    Py_XDECREF(self->groups);
    Py_XDECREF(self->statics);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
brt_add(PolicyRouteTable *self, PyObject *args)
{
    if (self->sealed) {
        PyErr_SetString(PyExc_RuntimeError, "compiled route tables are immutable");
        return NULL;
    }
    PyObject *path_obj, *method_obj, *handler, *clauses = NULL;
    if (!PyArg_ParseTuple(args, "UUO|O:add", &path_obj, &method_obj, &handler,
                          &clauses)) {
        return NULL;
    }
    if (clauses == NULL || clauses == Py_None) {
        PyObject *zero = PyLong_FromLong(0);
        clauses = zero == NULL ? NULL : PyTuple_Pack(1, zero);
        Py_XDECREF(zero);
        if (clauses == NULL) return NULL;
    }
    else if (!PyTuple_CheckExact(clauses)) {
        /* Everything downstream reads this with PyTuple_GET_ITEM. */
        PyErr_SetString(PyExc_TypeError, "access_clauses must be a tuple");
        return NULL;
    }
    else {
        Py_INCREF(clauses);
    }
    if (validate_access_clauses(clauses) < 0) {
        Py_DECREF(clauses);
        return NULL;
    }
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL || path_len == 0 || path[0] != '/') {
        Py_DECREF(clauses);
        if (path != NULL) {
            PyErr_SetString(PyExc_ValueError, "route paths must begin with '/'");
        }
        return NULL;
    }

    if (memchr(path, '{', (size_t)path_len) == NULL) {
        /* Static: same hash fast path the tree keeps, untouched by the bitset. */
        PyObject *by_path = PyDict_GetItemWithError(self->statics, method_obj);
        if (by_path == NULL) {
            if (PyErr_Occurred()) { Py_DECREF(clauses); return NULL; }
            by_path = PyDict_New();
            if (by_path == NULL ||
                PyDict_SetItem(self->statics, method_obj, by_path) < 0) {
                Py_XDECREF(by_path); Py_DECREF(clauses); return NULL;
            }
            Py_DECREF(by_path);
        }
        int exists = PyDict_Contains(by_path, path_obj);
        if (exists < 0) { Py_DECREF(clauses); return NULL; }
        if (exists) {
            Py_DECREF(clauses);
            PyErr_Format(PyExc_ValueError, "duplicate route: %U %U", method_obj, path_obj);
            return NULL;
        }
        PyObject *mr = PyTuple_Pack(2, handler, Py_None);
        PyObject *entry = mr ? PyTuple_Pack(2, mr, clauses) : NULL;
        Py_XDECREF(mr);
        Py_DECREF(clauses);
        if (entry == NULL) return NULL;
        int rc = PyDict_SetItem(by_path, path_obj, entry);
        Py_DECREF(entry);
        if (rc < 0) return NULL;
        /* A compiled MethodGroups caches this method's by_path dict (possibly
         * as NULL, for a method that had no statics yet), so a static add must
         * invalidate the compiled groups like a parameter add does. */
        self->dirty = 1;
        Py_RETURN_NONE;
    }

    BSeg raw[BRT_MAX_SEGMENTS];
    Py_ssize_t nseg = brt_scan(path, path_len, raw);
    if (nseg < 0) {
        Py_DECREF(clauses);
        PyErr_SetString(PyExc_ValueError, "route path has too many segments");
        return NULL;
    }

    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        BRoute *other = &self->routes[i];
        if (other->nseg != nseg) continue;
        int same_method = PyObject_RichCompareBool(other->method, method_obj, Py_EQ);
        if (same_method < 0) { Py_DECREF(clauses); return NULL; }
        if (!same_method) continue;
        /* Same shape is the same route: parameter names do not distinguish. */
        int same = 1;
        for (Py_ssize_t k = 0; k < nseg && same; k++) {
            const char *seg = raw[k].bytes;
            Py_ssize_t len = raw[k].len;
            int is_param = (len >= 2 && seg[0] == '{' && seg[len - 1] == '}');
            int other_param = (other->segs[k].bytes == NULL);
            if (is_param != other_param) same = 0;
            else if (!is_param &&
                     (other->segs[k].len != len ||
                      memcmp(other->segs[k].bytes, seg, (size_t)len) != 0)) same = 0;
        }
        if (same) {
            Py_DECREF(clauses);
            /* Same literal/parameter shape is the same route: parameter names
             * do not distinguish it. Keep the established wording --
             * "duplicate" is the static-path case. */
            PyErr_Format(PyExc_ValueError, "conflicting route: %U %U",
                         method_obj, path_obj);
            return NULL;
        }
    }

    if (self->nroutes == self->routes_cap) {
        Py_ssize_t cap = self->routes_cap ? self->routes_cap * 2 : 16;
        BRoute *grown = PyMem_Realloc(self->routes, (size_t)cap * sizeof(BRoute));
        if (grown == NULL) { Py_DECREF(clauses); return PyErr_NoMemory(); }
        self->routes = grown;
        self->routes_cap = cap;
    }
    BRoute *r = &self->routes[self->nroutes];
    memset(r, 0, sizeof(*r));
    r->nseg = (int)nseg;
    r->order = (int)self->nroutes;
    r->method = Py_NewRef(method_obj);
    r->access_clauses = clauses; /* owned */
    r->handler = Py_NewRef(handler);

    /* One block holds the segment bytes so each route is a single free. */
    Py_ssize_t bytes_needed = 0;
    for (Py_ssize_t i = 0; i < nseg; i++) bytes_needed += raw[i].len;
    r->segs = PyMem_Malloc((size_t)nseg * sizeof(BSeg) + (size_t)bytes_needed);
    if (r->segs == NULL) {
        Py_DECREF(clauses); Py_DECREF(r->handler);
        return PyErr_NoMemory();
    }
    char *blob = (char *)(r->segs + nseg);
    PyObject *names = PyList_New(0);
    if (names == NULL) { PyMem_Free(r->segs); Py_DECREF(clauses);
                         Py_DECREF(r->handler); return NULL; }
    for (Py_ssize_t i = 0; i < nseg; i++) {
        const char *seg = raw[i].bytes;
        Py_ssize_t len = raw[i].len;
        int is_param = (len >= 2 && seg[0] == '{' && seg[len - 1] == '}');
        if (is_param) {
            PyObject *name = PyUnicode_DecodeUTF8(seg + 1, len - 2, NULL);
            if (name == NULL || PyList_Append(names, name) < 0) {
                Py_XDECREF(name); Py_DECREF(names); PyMem_Free(r->segs);
                Py_DECREF(clauses); Py_DECREF(r->handler);
                return NULL;
            }
            Py_DECREF(name);
            r->segs[i].bytes = NULL;
            r->segs[i].len = 0;
        }
        else {
            memcpy(blob, seg, (size_t)len);
            r->segs[i].bytes = blob;
            r->segs[i].len = len;
            blob += len;
            r->specificity++;
        }
    }
    r->param_names = PyList_AsTuple(names);
    Py_DECREF(names);
    if (r->param_names == NULL) {
        PyMem_Free(r->segs); Py_DECREF(clauses); Py_DECREF(r->handler);
        return NULL;
    }
    /* Convert ordinary clauses to machine words so the common match path never
     * touches PyLong arithmetic. A wider clause leaves cmasks NULL and uses the
     * arbitrary-precision native fallback. */
    Py_ssize_t ncl = PyTuple_GET_SIZE(clauses);
    if (ncl > 0) {
        r->cmasks = PyMem_Malloc((size_t)ncl * sizeof(unsigned long long));
        if (r->cmasks != NULL) {
            for (Py_ssize_t c = 0; c < ncl; c++) {
                unsigned long long cl =
                    PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(clauses, c));
                if (cl == (unsigned long long)-1 && PyErr_Occurred()) {
                    PyErr_Clear();
                    PyMem_Free(r->cmasks);
                    r->cmasks = NULL;
                    break;
                }
                r->cmasks[c] = cl;
            }
        }
        if (r->cmasks != NULL) r->ncmasks = ncl;
    }
    self->nroutes++;
    self->route_count++;
    self->dirty = 1;
    Py_RETURN_NONE;
}

static PyObject *
brt_add_dynamic(PolicyRouteTable *self, PyObject *args)
{
    if (self->sealed) {
        PyErr_SetString(PyExc_RuntimeError, "compiled route tables are immutable");
        return NULL;
    }
    PyObject *path_obj, *method_obj, *host_obj, *handler, *clauses = NULL;
    if (!PyArg_ParseTuple(
            args, "UUOO|O:add_dynamic", &path_obj, &method_obj, &host_obj,
            &handler, &clauses)) return NULL;
    if (host_obj != Py_None && !PyUnicode_Check(host_obj)) {
        PyErr_SetString(PyExc_TypeError, "dynamic route host must be str or None");
        return NULL;
    }
    if (clauses == NULL || clauses == Py_None) {
        PyObject *zero = PyLong_FromLong(0);
        clauses = zero == NULL ? NULL : PyTuple_Pack(1, zero);
        Py_XDECREF(zero);
        if (clauses == NULL) return NULL;
    }
    else if (!PyTuple_CheckExact(clauses)) {
        PyErr_SetString(PyExc_TypeError, "access_clauses must be a tuple");
        return NULL;
    }
    else {
        Py_INCREF(clauses);
    }
    if (validate_access_clauses(clauses) < 0) {
        Py_DECREF(clauses);
        return NULL;
    }

    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) {
        Py_DECREF(clauses);
        return NULL;
    }
    if (path_len == 0 || path[0] != '/') {
        Py_DECREF(clauses);
        PyErr_SetString(PyExc_ValueError, "route paths must begin with '/'");
        return NULL;
    }
    if (self->ndynamic == self->dynamic_cap) {
        Py_ssize_t capacity = self->dynamic_cap ? self->dynamic_cap * 2 : 8;
        BDynamicRoute *routes = PyMem_Realloc(
            self->dynamic_routes, (size_t)capacity * sizeof(*routes));
        if (routes == NULL) {
            Py_DECREF(clauses);
            return PyErr_NoMemory();
        }
        self->dynamic_routes = routes;
        self->dynamic_cap = capacity;
    }
    BDynamicRoute *route = &self->dynamic_routes[self->ndynamic];
    memset(route, 0, sizeof(*route));
    route->method = Py_NewRef(method_obj);
    route->handler = Py_NewRef(handler);
    route->access_clauses = clauses;
    route->host_route = host_obj != Py_None;
    if (dynamic_path_compile(route, path, path_len) < 0) goto error;
    if (route->host_route) {
        Py_ssize_t host_len;
        const char *host = PyUnicode_AsUTF8AndSize(host_obj, &host_len);
        if (host == NULL || dynamic_host_compile(route, host, host_len) < 0) goto error;
    }
    if (!route->host_route && !route->greedy) {
        PyErr_SetString(
            PyExc_ValueError,
            "add_dynamic requires a host pattern or final {name:path} placeholder");
        goto error;
    }
    if (dynamic_compile_masks(route) < 0) goto error;
    self->ndynamic++;
    self->route_count++;
    Py_RETURN_NONE;

error:
    if (!PyErr_Occurred()) PyErr_NoMemory();
    dynamic_route_clear(route);
    return NULL;
}

/* Resolve one method to its MethodGroups. Returns the table-owned empty group for a method
 * nothing routes, NULL only on error. The compiled groups are keyed by method
 * alone and indexed by nseg, so the hot path allocates nothing -- the old
 * (method, nseg) tuple key was the single biggest per-match cost on small
 * tables -- and the one-entry cache makes the steady state a pointer compare. */
static MethodGroups *
brt_method_groups(PolicyRouteTable *self, PyObject *method)
{
    if (self->dirty) {
        /* add() after matching: drop every compiled group, mirroring the pure
         * table, which clears on add. Rebuilt lazily below. */
        PyDict_Clear(self->groups);
        Py_CLEAR(self->cached_method);
        self->cached_mg = NULL;
        self->dirty = 0;
    }
    if (method == self->cached_method) return self->cached_mg;
    PyObject *capsule = PyDict_GetItemWithError(self->groups, method);
    MethodGroups *mg;
    if (capsule != NULL) {
        mg = (MethodGroups *)PyCapsule_GetPointer(capsule, "brt.methodgroups");
        if (mg == NULL) return NULL;
    }
    else {
        if (PyErr_Occurred()) return NULL;
        /* Only methods that actually route earn an entry; giving one to every
         * probed method would let request traffic grow the dict forever. */
        PyObject *by_path = PyDict_GetItemWithError(self->statics, method);
        if (by_path == NULL && PyErr_Occurred()) return NULL;
        int routed = (by_path != NULL);
        for (Py_ssize_t i = 0; !routed && i < self->nroutes; i++) {
            routed = PyObject_RichCompareBool(self->routes[i].method, method, Py_EQ);
            if (routed < 0) return NULL;
        }
        if (!routed) return &self->no_groups;
        mg = PyMem_Calloc(1, sizeof(MethodGroups));
        if (mg == NULL) { PyErr_NoMemory(); return NULL; }
        mg->statics = Py_XNewRef(by_path);
        capsule = PyCapsule_New(mg, "brt.methodgroups", methodgroups_capsule_free);
        if (capsule == NULL) { Py_XDECREF(mg->statics); PyMem_Free(mg); return NULL; }
        int rc = PyDict_SetItem(self->groups, method, capsule);
        Py_DECREF(capsule);
        if (rc < 0) return NULL;
    }
    Py_XSETREF(self->cached_method, Py_NewRef(method));
    self->cached_mg = mg;
    return mg;
}

/* Build the group for one (method, nseg) into `mg`. Returns NULL with no
 * exception set when no routes have that shape; the NULL is cached. */
static BGroup *
brt_group_build(PolicyRouteTable *self, MethodGroups *mg, PyObject *method,
                int nseg)
{
    BRoute **cands = PyMem_Malloc((size_t)(self->nroutes ? self->nroutes : 1)
                                  * sizeof(BRoute *));
    if (cands == NULL) { PyErr_NoMemory(); return NULL; }
    Py_ssize_t n = 0;
    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        /* A group is one (method, segment count). */
        if (self->routes[i].nseg != nseg) continue;
        int same = PyObject_RichCompareBool(self->routes[i].method, method, Py_EQ);
        if (same < 0) { PyMem_Free(cands); return NULL; }
        if (same) cands[n++] = &self->routes[i];
    }
    BGroup *g = NULL;
    if (n > 0) {
        g = bgroup_build(cands, n, nseg);
        if (g == NULL) { PyMem_Free(cands); return NULL; }
    }
    PyMem_Free(cands);
    mg->by_nseg[nseg] = g; /* NULL is cached too: no routes have this shape */
    mg->built[nseg] = 1;
    return g;
}


/* (code, payload) without Py_BuildValue's format parsing. Steals `payload`. */
static PyObject *
classification_pair(long code, PyObject *payload)
{
    PyObject *code_obj = PyLong_FromLong(code);
    if (code_obj == NULL) { Py_DECREF(payload); return NULL; }
    PyObject *pair = PyTuple_New(2);
    if (pair == NULL) { Py_DECREF(code_obj); Py_DECREF(payload); return NULL; }
    PyTuple_SET_ITEM(pair, 0, code_obj);
    PyTuple_SET_ITEM(pair, 1, payload);
    return pair;
}

/* Is `r` reachable by a caller holding `caller`? Clauses are disjunctive; a
 * zero clause admits everyone. */
static int
brt_compile_groups(PolicyRouteTable *self)
{
    if (!self->dirty) return 0;
    if (dynamic_groups_build(self) < 0) return -1;
    PyDict_Clear(self->groups);
    Py_CLEAR(self->cached_method);
    self->cached_mg = NULL;
    self->dirty = 0;

    PyObject *method;
    PyObject *unused;
    Py_ssize_t pos = 0;
    while (PyDict_Next(self->statics, &pos, &method, &unused)) {
        if (brt_method_groups(self, method) == NULL) return -1;
    }
    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        BRoute *route = &self->routes[i];
        MethodGroups *mg = brt_method_groups(self, route->method);
        if (mg == NULL) return -1;
        if (!mg->built[route->nseg] &&
            brt_group_build(self, mg, route->method, route->nseg) == NULL &&
            PyErr_Occurred()) {
            return -1;
        }
    }
    pos = 0;
    while (PyDict_Next(self->groups, &pos, &method, &unused)) {
        MethodGroups *mg = (MethodGroups *)PyCapsule_GetPointer(
            unused, "brt.methodgroups");
        if (mg == NULL) return -1;
        memset(mg->built, 1, sizeof(mg->built));
    }

    /* Compiled groups own their route records and static dictionaries. Release
     * registration-only storage so the request lifetime retains one shape. */
    PyDict_Clear(self->statics);
    for (Py_ssize_t i = 0; i < self->nroutes; i++) broute_clear(&self->routes[i]);
    PyMem_Free(self->routes);
    self->routes = NULL;
    self->nroutes = 0;
    self->routes_cap = 0;
    self->sealed = 1;
    return 0;
}

static PyObject *
brt_compile(PolicyRouteTable *self, PyObject *Py_UNUSED(unused))
{
    if (brt_compile_groups(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static int
clause_eligible(PyObject *clause, unsigned long long caller,
                PyObject *wide_caller)
{
    if (wide_caller != NULL) {
        PyObject *intersection = PyNumber_And(clause, wide_caller);
        if (intersection == NULL) return -1;
        int equal = PyObject_RichCompareBool(intersection, clause, Py_EQ);
        Py_DECREF(intersection);
        return equal;
    }
    unsigned long long word = PyLong_AsUnsignedLongLong(clause);
    if (word == (unsigned long long)-1 && PyErr_Occurred()) {
        if (!PyErr_ExceptionMatches(PyExc_OverflowError)) return -1;
        PyErr_Clear();
        return 0;
    }
    return (word & ~caller) == 0;
}

static int
route_eligible(BRoute *r, unsigned long long caller, PyObject *wide_caller)
{
    if (wide_caller == NULL && r->cmasks != NULL) {
        for (Py_ssize_t c = 0; c < r->ncmasks; c++) {
            if ((r->cmasks[c] & ~caller) == 0) return 1;
        }
        return 0;
    }
    PyObject *clauses = r->access_clauses;
    for (Py_ssize_t c = 0; c < PyTuple_GET_SIZE(clauses); c++) {
        int eligible = clause_eligible(
            PyTuple_GET_ITEM(clauses, c), caller, wide_caller);
        if (eligible != 0) return eligible;
    }
    return 0;
}

static int ticket_append(PyObject **ticket_out, PyObject *entry);

static int
dynamic_route_eligible(BDynamicRoute *route, unsigned long long caller,
                       PyObject *wide_caller)
{
    if (wide_caller == NULL && route->cmasks != NULL) {
        for (Py_ssize_t index = 0; index < route->ncmasks; index++) {
            if ((route->cmasks[index] & ~caller) == 0) return 1;
        }
        return 0;
    }
    for (Py_ssize_t index = 0;
         index < PyTuple_GET_SIZE(route->access_clauses); index++) {
        int eligible = clause_eligible(
            PyTuple_GET_ITEM(route->access_clauses, index), caller, wide_caller);
        if (eligible != 0) return eligible;
    }
    return 0;
}

static Py_ssize_t
dynamic_host_scan(const char *host, Py_ssize_t len, BSeg *out)
{
    const char *start = host;
    Py_ssize_t count = 0;
    for (const char *cursor = host;; cursor++) {
        if (cursor == host + len || *cursor == '.') {
            if (count == BRT_MAX_SEGMENTS) return -1;
            out[count].bytes = (char *)start;
            out[count].len = cursor - start;
            count++;
            if (cursor == host + len) return count;
            start = cursor + 1;
        }
    }
}

static int
dynamic_segments_match(const BDynamicSegment *pattern, Py_ssize_t count,
                       const BSeg *actual)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        if (pattern[index].bytes == NULL) {
            if (!pattern[index].greedy && actual[index].len == 0) return 0;
            continue;
        }
        if (pattern[index].len != actual[index].len ||
            memcmp(pattern[index].bytes, actual[index].bytes,
                   (size_t)actual[index].len) != 0) return 0;
    }
    return 1;
}

static PyObject *
dynamic_build_match(BDynamicRoute *route, PyObject *path_obj,
                    const BSeg *path_segments, const BSeg *host_segments)
{
    PyObject *params = PyDict_New();
    if (params == NULL) return NULL;
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) goto error;
    for (Py_ssize_t index = 0; index < route->npath; index++) {
        PyObject *name = route->path[index].name;
        if (name == NULL) continue;
        const char *value = path_segments[index].bytes;
        Py_ssize_t length = route->path[index].greedy
            ? path + path_len - value
            : path_segments[index].len;
        PyObject *text = PyUnicode_DecodeUTF8(value, length, "surrogateescape");
        if (text == NULL || PyDict_SetItem(params, name, text) < 0) {
            Py_XDECREF(text);
            goto error;
        }
        Py_DECREF(text);
    }
    if (route->host_route) {
        for (Py_ssize_t index = 0; index < route->nhost; index++) {
            PyObject *name = route->host[index].name;
            if (name == NULL) continue;
            PyObject *text = PyUnicode_DecodeUTF8(
                host_segments[index].bytes, host_segments[index].len,
                "surrogateescape");
            if (text == NULL || PyDict_SetItem(params, name, text) < 0) {
                Py_XDECREF(text);
                goto error;
            }
            Py_DECREF(text);
        }
    }
    PyObject *result = PyTuple_New(2);
    if (result == NULL) goto error;
    PyTuple_SET_ITEM(result, 0, Py_NewRef(route->handler));
    PyTuple_SET_ITEM(result, 1, params);
    return result;

error:
    Py_DECREF(params);
    return NULL;
}

/* Ordered native fallback for the two shapes the grouped bitset deliberately
 * does not model: host predicates and a final greedy path segment.  A match is
 * materialized only after every literal fact has succeeded. */
static PyObject *
dynamic_dispatch(PolicyRouteTable *self, PyObject *method_obj,
                 PyObject *path_obj, PyObject *host_obj, int host_routes,
                 unsigned long long caller, PyObject *wide_caller,
                 PyObject **ticket_out,
                 int *found_public)
{
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) return NULL;
    BSeg path_segments[BRT_MAX_SEGMENTS];
    Py_ssize_t npath = brt_scan(path, path_len, path_segments);
    if (npath < 0) Py_RETURN_NONE;

    const char *host = NULL;
    Py_ssize_t host_len = 0;
    BSeg host_segments[BRT_MAX_SEGMENTS];
    Py_ssize_t nhost = 0;
    if (host_routes) {
        host = PyUnicode_AsUTF8AndSize(host_obj, &host_len);
        if (host == NULL) return NULL;
        nhost = dynamic_host_scan(host, host_len, host_segments);
        if (nhost < 0) Py_RETURN_NONE;
    }

    BDynamicGroup *method_group = dynamic_group_lookup(
        self, host_routes, method_obj);
    if (method_group == NULL && PyErr_Occurred()) return NULL;
    BDynamicGroup *get_group = NULL;
    if (PyUnicode_CompareWithASCIIString(method_obj, "HEAD") == 0) {
        get_group = dynamic_group_lookup(self, host_routes, self->get_method);
        if (get_group == NULL && PyErr_Occurred()) return NULL;
    }
    Py_ssize_t method_pos = 0;
    Py_ssize_t get_pos = 0;
    while ((method_group != NULL && method_pos < method_group->count) ||
           (get_group != NULL && get_pos < get_group->count)) {
        Py_ssize_t method_index = method_group != NULL && method_pos < method_group->count
            ? method_group->indices[method_pos] : PY_SSIZE_T_MAX;
        Py_ssize_t get_index = get_group != NULL && get_pos < get_group->count
            ? get_group->indices[get_pos] : PY_SSIZE_T_MAX;
        Py_ssize_t index;
        if (method_index < get_index) {
            index = method_index;
            method_pos++;
        } else {
            index = get_index;
            get_pos++;
        }
        BRT_PROBE(self->probe_stats.dynamic_candidates++);
        BDynamicRoute *route = &self->dynamic_routes[index];
        if (route->greedy ? npath < route->npath : npath != route->npath) continue;
        Py_ssize_t fixed_path = route->greedy ? route->npath - 1 : route->npath;
        if (!dynamic_segments_match(route->path, fixed_path, path_segments)) continue;
        if (host_routes &&
            (nhost != route->nhost ||
             !dynamic_segments_match(route->host, route->nhost, host_segments))) continue;
        PyObject *match = dynamic_build_match(
            route, path_obj, path_segments, host_segments);
        if (match == NULL) return NULL;
        int eligible = dynamic_route_eligible(route, caller, wide_caller);
        if (eligible < 0) {
            Py_DECREF(match);
            return NULL;
        }
        if (eligible) {
            if (found_public != NULL) *found_public = 1;
            return match;
        }
        if (ticket_out != NULL) {
            PyObject *entry = PyTuple_Pack(2, match, route->access_clauses);
            Py_DECREF(match);
            if (entry == NULL || ticket_append(ticket_out, entry) < 0) {
                Py_XDECREF(entry);
                return NULL;
            }
            Py_DECREF(entry);
        }
        else {
            Py_DECREF(match);
        }
        Py_RETURN_NONE; /* first dynamic route owns this fact conjunction */
    }
    Py_RETURN_NONE;
}

/* Keep the common one-candidate ticket as the entry itself.  A list appears
 * only when two protected routes genuinely compete for the same path; wrapping
 * every single entry in list -> tuple machinery was allocation by convention,
 * not information the resolver needed. */
static int
ticket_append(PyObject **ticket_out, PyObject *entry)
{
    if (*ticket_out == NULL) {
        *ticket_out = Py_NewRef(entry);
        return 0;
    }
    if (!PyList_CheckExact(*ticket_out)) {
        PyObject *items = PyList_New(1);
        if (items == NULL) return -1;
        PyList_SET_ITEM(items, 0, *ticket_out); /* steal the former owned ref */
        *ticket_out = items;
    }
    return PyList_Append(*ticket_out, entry);
}

static PyObject *
path_params_new(BRoute *route, BSeg *segments, PyObject *path)
{
    Py_ssize_t count = PyTuple_GET_SIZE(route->param_names);
    if (count == 0) return Py_NewRef(Py_None);
    BPathParams *params = PyObject_NewVar(
        BPathParams, &BPathParamsType, count);
    if (params == NULL) return NULL;
    params->names = Py_NewRef(route->param_names);
    params->path = Py_NewRef(path);
    params->cached = NULL;
    params->cached_index = -1;
    Py_ssize_t path_len;
    const char *base = PyUnicode_AsUTF8AndSize(path, &path_len);
    if (base == NULL) {
        Py_DECREF(params);
        return NULL;
    }
    (void)path_len;
    Py_ssize_t at = 0;
    for (int p = 0; p < route->nseg; p++) {
        if (route->segs[p].bytes != NULL) continue;
        params->slices[at].offset = segments[p].bytes - base;
        params->slices[at].length = segments[p].len;
        at++;
    }
    return (PyObject *)params;
}

/* (handler, params | None) for a route already known to match `segs`. */
static PyObject *
build_match(BRoute *r, BSeg *segs, PyObject *path)
{
    PyObject *params = path_params_new(r, segs, path);
    if (params == NULL) return NULL;
    PyObject *result = PyTuple_New(2);
    if (result == NULL) { Py_DECREF(params); return NULL; }
    PyTuple_SET_ITEM(result, 0, Py_NewRef(r->handler));
    PyTuple_SET_ITEM(result, 1, params); /* steals the reference */
    return result;
}

/* `ticket_out`, when non-NULL, points at a list slot that is filled lazily on
 * the first unreachable candidate: public traffic never allocates a ticket. */
static PyObject *
brt_match_impl(PolicyRouteTable *self, PyObject *method_obj,
               PyObject *path_obj, unsigned long long caller,
               PyObject *wide_caller, PyObject **ticket_out,
               int *found_public)
{
    if (brt_compile_groups(self) < 0) return NULL;
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) return NULL;

    MethodGroups *mg = brt_method_groups(self, method_obj);
    if (mg == NULL) return NULL;

    /* Static hash path first: a fully-literal route is more specific than any
     * parameter route, so a reachable hit wins outright and costs one hash of
     * the whole path instead of one per segment. Literal-vs-parameter
     * precedence is known at compile time, which is what makes this sound. */
    if (mg->statics != NULL) {
        PyObject *entry = PyDict_GetItemWithError(mg->statics, path_obj);
        if (entry != NULL) {
            PyObject *clauses = PyTuple_GET_ITEM(entry, 1);
            int eligible = 0;
            for (Py_ssize_t c = 0; c < PyTuple_GET_SIZE(clauses); c++) {
                PyObject *cl_obj = PyTuple_GET_ITEM(clauses, c);
                eligible = clause_eligible(cl_obj, caller, wide_caller);
                if (eligible < 0) return NULL;
                if (eligible) break;
            }
            if (eligible) {
                if (found_public != NULL) *found_public = 1;
                return Py_NewRef(PyTuple_GET_ITEM(entry, 0));
            }
            if (ticket_out != NULL && ticket_append(ticket_out, entry) < 0) {
                return NULL;
            }
            /* Present but unreachable: fall through to the parameter group. A
             * static route this caller cannot reach must not shadow a
             * parameter route it can -- pruning ineligible branches
             * rather than exposing them. */
        }
        if (PyErr_Occurred()) return NULL;
    }

    if (path_len == 0 || path[0] != '/') Py_RETURN_NONE;
    BSeg segs[BRT_MAX_SEGMENTS];
    Py_ssize_t nseg = brt_scan(path, path_len, segs);
    if (nseg < 0) Py_RETURN_NONE;

    BGroup *g = mg->by_nseg[nseg];
    if (g == NULL) {
        if (PyErr_Occurred()) return NULL;
        Py_RETURN_NONE;
    }

    /* One pass: intersect a mask per discriminating position, strongest first,
     * stopping as soon as at most one route can still match. The buffer is one
     * word per 64 routes in the group, not per segment, so groups past the
     * stack capacity spill to the heap rather than off the stack. */
    uint64_t stack_words[64];
    uint64_t *heap_words = NULL;
    uint64_t *survivors = stack_words;
    Py_ssize_t nw = g->nwords;
    if ((size_t)nw > sizeof(stack_words) / sizeof(stack_words[0])) {
        heap_words = PyMem_Malloc((size_t)nw * sizeof(uint64_t));
        if (heap_words == NULL) return PyErr_NoMemory();
        survivors = heap_words;
    }
    PyObject *out = NULL;
    for (Py_ssize_t w = 0; w < nw; w++) survivors[w] = ~0ULL;
    if (g->nroutes % 64) {
        survivors[nw - 1] = (1ULL << (g->nroutes % 64)) - 1;
    }
    for (int k = 0; k < g->norder; k++) {
        int p = g->order[k];
        Py_ssize_t slot = maskmap_get(&g->literal[p], segs[p].bytes, segs[p].len,
                                      &self->probe_stats);
        const uint64_t *pm = &g->param[(Py_ssize_t)p * nw];
        int live = 0;
        for (Py_ssize_t w = 0; w < nw; w++) {
            uint64_t m = (slot >= 0 ? g->pool[slot + w] : 0ULL) | pm[w];
            survivors[w] &= m;
            live += route_popcount64(survivors[w]);
        }
        if (live <= 1) break;
    }

    /* Bits are in priority order (specificity, then registration order), so the
     * first candidate that both matches the path and is reachable wins.
     * Verification comes before the access check: the intersection can leave a
     * route that matched every tested position but not an untested one, and a
     * route that does not match must never reach a ticket. */
    for (Py_ssize_t w = 0; w < nw; w++) {
        uint64_t bits = survivors[w];
        while (bits) {
            Py_ssize_t i = w * 64 + route_ctz64(bits);
            bits &= bits - 1;
            BRoute *r = &g->owned_routes[i];
            int matches = 1;
            BRT_PROBE(self->probe_stats.verify_routes++);
            for (int p = 0; p < r->nseg; p++) {
                if (r->segs[p].bytes == NULL) continue;
                BRT_PROBE(self->probe_stats.verify_cmps++);
                if (r->segs[p].len != segs[p].len ||
                    memcmp(r->segs[p].bytes, segs[p].bytes,
                           (size_t)segs[p].len) != 0) {
                    matches = 0;
                    break;
                }
            }
            if (!matches) continue;
            int eligible = route_eligible(r, caller, wide_caller);
            if (eligible < 0) goto done;
            if (eligible) {
                if (found_public != NULL) *found_public = 1;
                out = build_match(r, segs, path_obj);
                goto done;
            }
            if (ticket_out != NULL) {
                PyObject *mr = build_match(r, segs, path_obj);
                if (mr == NULL) goto done;
                PyObject *entry = PyTuple_Pack(2, mr, r->access_clauses);
                Py_DECREF(mr);
                if (entry == NULL || ticket_append(ticket_out, entry) < 0) {
                    Py_XDECREF(entry);
                    goto done;
                }
                Py_DECREF(entry);
            }
        }
    }
    out = Py_NewRef(Py_None);
done:
    PyMem_Free(heap_words);
    return out;
}


/* HEAD falls back to GET, matching Wreath's routing contract. */
static PyObject *
brt_dispatch(PolicyRouteTable *self, PyObject *method_obj, PyObject *path_obj,
             unsigned long long caller, PyObject *wide_caller,
             PyObject **ticket_out, int *found_public)
{
    PyObject *r = brt_match_impl(self, method_obj, path_obj, caller, wide_caller,
                                 ticket_out, found_public);
    if (r == NULL || r != Py_None) return r;
    if (PyUnicode_CompareWithASCIIString(method_obj, "HEAD") != 0) return r;
    Py_DECREF(r);
    return brt_match_impl(self, self->get_method, path_obj, caller, wide_caller,
                          ticket_out, found_public);
}

static int
brt_fastcall_arity(const char *name, Py_ssize_t nargs, Py_ssize_t minimum,
                   Py_ssize_t maximum)
{
    if (nargs >= minimum && nargs <= maximum) return 0;
    PyErr_Format(PyExc_TypeError, "%s expected %zd to %zd arguments, got %zd",
                 name, minimum, maximum, nargs);
    return -1;
}

static int
brt_require_unicode(PyObject *value, const char *name, const char *argument)
{
    if (PyUnicode_Check(value)) return 0;
    PyErr_Format(PyExc_TypeError, "%s %s must be str", name, argument);
    return -1;
}

static int
brt_caller_mask(PyObject *value, unsigned long long *word,
                PyObject **wide)
{
    int sign = 0;
    if (!PyLong_CheckExact(value) ||
        PyLong_GetSign(value, &sign) < 0 || sign < 0) {
        if (PyErr_Occurred()) return -1;
        PyErr_SetString(PyExc_ValueError,
                        "caller capability mask must be a non-negative integer");
        return -1;
    }
    *word = PyLong_AsUnsignedLongLong(value);
    if (*word == (unsigned long long)-1 && PyErr_Occurred()) {
        if (!PyErr_ExceptionMatches(PyExc_OverflowError)) return -1;
        PyErr_Clear();
        *word = 0;
        *wide = value; /* borrowed for the duration of this native call */
    }
    else {
        *wide = NULL;
    }
    return 0;
}

static PyObject *
brt_match(PolicyRouteTable *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (brt_fastcall_arity("match", nargs, 2, 3) < 0 ||
        brt_require_unicode(args[0], "match", "method") < 0 ||
        brt_require_unicode(args[1], "match", "path") < 0) {
        return NULL;
    }
    PyObject *method_obj = args[0];
    PyObject *path_obj = args[1];
    PyObject *caller_obj = nargs == 3 ? args[2] : NULL;
    unsigned long long caller = 0;
    PyObject *wide_caller = NULL;
    if (caller_obj != NULL) {
        if (brt_caller_mask(caller_obj, &caller, &wide_caller) < 0) return NULL;
    }
    PyObject *result = brt_dispatch(
        self, method_obj, path_obj, caller, wide_caller, NULL, NULL);
    if (result == NULL || result != Py_None) return result;
    Py_DECREF(result);
    return dynamic_dispatch(
        self, method_obj, path_obj, Py_None, 0, caller, wide_caller, NULL, NULL);
}

static PyObject *
classification_finish(PyObject *result, PyObject *ticket, int found_public)
{
    if (found_public) {
        Py_XDECREF(ticket);
        return classification_pair(1, result);
    }
    Py_DECREF(result);
    if (ticket == NULL) {
        return classification_pair(0, Py_NewRef(Py_None));
    }
    if (PyList_CheckExact(ticket)) {
        PyObject *as_tuple = PyList_AsTuple(ticket);
        Py_DECREF(ticket);
        if (as_tuple == NULL) return NULL;
        ticket = as_tuple;
    }
    return classification_pair(2, ticket);
}

/* (0, None) | (1, match) | (2, ticket): a reachable-by-anyone route answers 1
 * outright; otherwise every candidate that matched the path is returned as a
 * ticket for resolve() to settle once the caller is known. */
static PyObject *
brt_classify(PolicyRouteTable *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (brt_fastcall_arity("classify", nargs, 2, 2) < 0 ||
        brt_require_unicode(args[0], "classify", "method") < 0 ||
        brt_require_unicode(args[1], "classify", "path") < 0) {
        return NULL;
    }
    PyObject *method_obj = args[0];
    PyObject *path_obj = args[1];
    PyObject *ticket = NULL; /* created lazily on the first protected candidate */
    int found_public = 0;
    PyObject *r = brt_dispatch(
        self, method_obj, path_obj, 0, NULL, &ticket, &found_public);
    if (r == NULL) { Py_XDECREF(ticket); return NULL; }
    if (!found_public && ticket == NULL) {
        Py_DECREF(r);
        r = dynamic_dispatch(
            self, method_obj, path_obj, Py_None, 0, 0, NULL, &ticket,
            &found_public);
        if (r == NULL) { Py_XDECREF(ticket); return NULL; }
    }
    return classification_finish(r, ticket, found_public);
}

static PyObject *
brt_classify_request(PolicyRouteTable *self, PyObject *const *args,
                     Py_ssize_t nargs)
{
    if (brt_fastcall_arity("classify_request", nargs, 3, 3) < 0 ||
        brt_require_unicode(args[0], "classify_request", "method") < 0 ||
        brt_require_unicode(args[1], "classify_request", "path") < 0 ||
        brt_require_unicode(args[2], "classify_request", "host") < 0) {
        return NULL;
    }
    PyObject *ticket = NULL;
    int found_public = 0;
    PyObject *result = dynamic_dispatch(
        self, args[0], args[1], args[2], 1, 0, NULL, &ticket, &found_public);
    if (result == NULL) return NULL;
    if (found_public || ticket != NULL) {
        return classification_finish(result, ticket, found_public);
    }
    Py_DECREF(result);
    result = brt_dispatch(
        self, args[0], args[1], 0, NULL, &ticket, &found_public);
    if (result == NULL) return NULL;
    if (found_public || ticket != NULL) {
        return classification_finish(result, ticket, found_public);
    }
    Py_DECREF(result);
    result = dynamic_dispatch(
        self, args[0], args[1], Py_None, 0, 0, NULL, &ticket, &found_public);
    if (result == NULL) return NULL;
    return classification_finish(result, ticket, found_public);
}

static int
brt_ticket_is_single(PyObject *ticket)
{
    if (PyTuple_GET_SIZE(ticket) != 2) return 0;
    PyObject *clauses = PyTuple_GET_ITEM(ticket, 1);
    if (!PyTuple_CheckExact(clauses)) return 0;
    return PyTuple_GET_SIZE(clauses) == 0 ||
           PyLong_CheckExact(PyTuple_GET_ITEM(clauses, 0));
}

static PyObject *
brt_resolve_entry(PyObject *entry, unsigned long long caller,
                  PyObject *wide_caller)
{
    PyObject *clauses = PyTuple_GET_ITEM(entry, 1);
    for (Py_ssize_t c = 0; c < PyTuple_GET_SIZE(clauses); c++) {
        int eligible = clause_eligible(
            PyTuple_GET_ITEM(clauses, c), caller, wide_caller);
        if (eligible < 0) return NULL;
        if (eligible) {
            return Py_NewRef(PyTuple_GET_ITEM(entry, 0));
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
brt_resolve_mask(PyObject *ticket, unsigned long long caller,
                 PyObject *wide_caller)
{
    if (brt_ticket_is_single(ticket)) {
        return brt_resolve_entry(ticket, caller, wide_caller);
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(ticket); i++) {
        PyObject *resolved =
            brt_resolve_entry(
                PyTuple_GET_ITEM(ticket, i), caller, wide_caller);
        if (resolved == NULL || resolved != Py_None) return resolved;
        Py_DECREF(resolved);
    }
    Py_RETURN_NONE;
}

static PyObject *
brt_resolve(PolicyRouteTable *Py_UNUSED(self), PyObject *const *args,
            Py_ssize_t nargs)
{
    if (brt_fastcall_arity("resolve", nargs, 2, 2) < 0) return NULL;
    PyObject *ticket = args[0];
    PyObject *caller_obj = args[1];
    if (!PyTuple_Check(ticket)) {
        PyErr_SetString(PyExc_TypeError, "resolve ticket must be tuple");
        return NULL;
    }
    unsigned long long caller;
    PyObject *wide_caller;
    if (brt_caller_mask(caller_obj, &caller, &wide_caller) < 0) return NULL;
    return brt_resolve_mask(ticket, caller, wide_caller);
}

static PyObject *
brt_resolve_identity(PolicyRouteTable *Py_UNUSED(self), PyObject *const *args,
                     Py_ssize_t nargs)
{
    if (brt_fastcall_arity("resolve_identity", nargs, 4, 4) < 0) return NULL;
    if (!PyTuple_Check(args[0])) {
        PyErr_SetString(PyExc_TypeError, "resolve ticket must be tuple");
        return NULL;
    }
    unsigned long long caller = 0;
    PyObject *wide_caller = NULL;
    PyObject *owned_mask = NULL;
    if (wreath_compiled_capability_word(
            args[1], args[2], args[3], &caller) < 0) {
        if (!PyErr_ExceptionMatches(PyExc_OverflowError)) return NULL;
        PyErr_Clear();
        owned_mask = wreath_compiled_capability_mask(args[1], args[2], args[3]);
        if (owned_mask == NULL ||
            brt_caller_mask(owned_mask, &caller, &wide_caller) < 0) {
            Py_XDECREF(owned_mask);
            return NULL;
        }
    }
    PyObject *resolved = brt_resolve_mask(args[0], caller, wide_caller);
    Py_XDECREF(owned_mask);
    return resolved;
}

/* Compatibility probe: never exposes protected tickets. */
static PyObject *
brt_probe(PolicyRouteTable *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (brt_fastcall_arity("probe", nargs, 3, 3) < 0 ||
        brt_require_unicode(args[0], "probe", "method") < 0 ||
        brt_require_unicode(args[1], "probe", "path") < 0) {
        return NULL;
    }
    PyObject *classify_args[2] = {args[0], args[1]};
    PyObject *pair = brt_classify(self, classify_args, 2);
    if (pair == NULL) return NULL;
    long code = PyLong_AsLong(PyTuple_GET_ITEM(pair, 0));
    if (code == -1 && PyErr_Occurred()) { Py_DECREF(pair); return NULL; }
    if (code == 1) return pair;
    Py_DECREF(pair);
    return classification_pair(code, Py_NewRef(Py_None));
}

static PyObject *
brt_stats(PolicyRouteTable *self, PyObject *Py_UNUSED(a))
{
    Py_ssize_t words = 0, groups = 0, keys = 0;
    PyObject *k, *v;
    Py_ssize_t pos = 0;
    while (PyDict_Next(self->groups, &pos, &k, &v)) {
        MethodGroups *mg =
            (MethodGroups *)PyCapsule_GetPointer(v, "brt.methodgroups");
        if (mg == NULL) return NULL;
        for (int i = 0; i <= BRT_MAX_SEGMENTS; i++) {
            BGroup *g = mg->by_nseg[i];
            if (g == NULL) continue;
            groups++;
            words += g->pool_len + (Py_ssize_t)g->nseg * g->nwords + g->nwords;
            for (int p = 0; p < g->nseg; p++) keys += g->literal[p].count;
        }
    }
    return Py_BuildValue("{s:n,s:n,s:n,s:n}", "groups", groups, "mask_words",
                         words, "literal_keys", keys, "routes", self->route_count);
}

static PyObject *
brt_probe_stats_get(PolicyRouteTable *self, PyObject *Py_UNUSED(a))
{
    ProbeStats *s = &self->probe_stats;
    PyObject *d = Py_BuildValue(
        "{s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K}",
        "lookups", s->lookups, "buckets_examined", s->buckets,
        "key_compares", s->key_compares, "hits", s->hits,
        "misses", s->misses, "max_probe", s->max_probe,
        "disc_lookups", s->disc_lookups, "verify_routes", s->verify_routes,
        "verify_cmps", s->verify_cmps,
        "dynamic_candidates", s->dynamic_candidates);
    memset(s, 0, sizeof(*s));
    return d;
}

static PyMethodDef brt_methods[] = {
    {"add", (PyCFunction)brt_add, METH_VARARGS,
     "add(path, method, handler, access_clauses=(0,))"},
    {"add_dynamic", (PyCFunction)brt_add_dynamic, METH_VARARGS,
     "add_dynamic(path, method, host, handler, access_clauses=(0,))"},
    {"compile", (PyCFunction)brt_compile, METH_NOARGS,
     "compile() -> None; eagerly build every route group"},
    {"match", (PyCFunction)(void (*)(void))brt_match, METH_FASTCALL,
     "match(method, path, caller_mask=0) -> (handler, params | None) | None"},
    {"classify", (PyCFunction)(void (*)(void))brt_classify, METH_FASTCALL,
     "classify(method, path) -> (classification, public_match | ticket | None)"},
    {"classify_request",
     (PyCFunction)(void (*)(void))brt_classify_request, METH_FASTCALL,
     "classify_request(method, path, host) -> classification and continuation"},
    {"resolve", (PyCFunction)(void (*)(void))brt_resolve, METH_FASTCALL,
     "resolve(ticket, caller_mask) -> (handler, params | None) | None"},
    {"resolve_identity",
     (PyCFunction)(void (*)(void))brt_resolve_identity, METH_FASTCALL,
     "resolve_identity(ticket, descriptor, roles, permissions) -> match | None"},
    {"probe", (PyCFunction)(void (*)(void))brt_probe, METH_FASTCALL,
     "probe(method, path, all_capability_mask) -> (classification, match | None)"},
    {"stats", (PyCFunction)brt_stats, METH_NOARGS,
     "stats() -> compiled size, for measurement"},
    {"probe_stats", (PyCFunction)brt_probe_stats_get, METH_NOARGS,
     "probe_stats() -> this table's hash probe counters, and reset them"},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject PolicyRouteTableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.PolicyRouteTable",
    .tp_basicsize = sizeof(PolicyRouteTable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "One-pass route and capability policy table.",
    .tp_new = brt_new,
    .tp_dealloc = (destructor)brt_dealloc,
    .tp_methods = brt_methods,
};

int
wreath_register_policy_router(PyObject *module)
{
    if (PyType_Ready(&BPathParamsType) < 0) return -1;
    if (PyType_Ready(&PolicyRouteTableType) < 0) return -1;
    return PyModule_AddObjectRef(module, "PolicyRouteTable",
                                 (PyObject *)&PolicyRouteTableType);
}
