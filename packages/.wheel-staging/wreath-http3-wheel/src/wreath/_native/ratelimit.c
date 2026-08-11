/* In-process token-bucket rate limiting.
 *
 * Buckets live in an open-addressed table sized in powers of two and probed
 * linearly, so the common request touches one cache line and allocates nothing.
 * Refills are lazy: a bucket stores its token count and the timestamp it was
 * last touched, and the tokens owed since then are computed on read. No timer,
 * no sweep thread.
 *
 * Memory is bounded by max_entries. The bound is free of accuracy loss because
 * a bucket that has refilled to capacity is indistinguishable from one that was
 * never created -- both admit a full burst -- so compaction simply drops them.
 * A spraying attacker creates buckets that go idle immediately and are reclaimed
 * on the next compaction; only genuinely rate-limited keys occupy the table.
 *
 * Deletion never tombstones. Compaction rebuilds the table, which keeps probe
 * chains exact and stops a long-lived limiter from degrading into a linear scan.
 */
#include "wreathcore.h"

typedef struct {
    PyObject *key; /* owned str; NULL marks an empty slot */
    Py_hash_t hash;
    double tokens;
    double updated;
} WreathBucket;

typedef struct {
    PyObject_HEAD
    WreathBucket *slots;
    size_t mask;        /* slot count - 1; slot count is a power of two */
    size_t used;
    size_t max_slots;   /* ceiling on the slot count */
    size_t max_entries; /* hard ceiling on tracked keys */
    double capacity;    /* burst size, in tokens */
    double rate;        /* tokens replenished per second */
} WreathTokenBucket;

/* Keep the table at or below 3/4 load: linear probing degrades sharply past
 * that, and the check must stay cheap enough to run on every insert. */
#define WREATH_FITS(used, slots) (((used) + 1) * 4 <= (slots) * 3)

/* Room for one more key: both under the load factor and under the configured
 * ceiling. The ceiling is checked separately because the table rounds up to a
 * power of two, which would otherwise admit ~1.5x the requested max_entries. */
#define WREATH_HAS_ROOM(self) \
    (WREATH_FITS((self)->used, (self)->mask + 1) && (self)->used < (self)->max_entries)

static size_t
next_power_of_two(size_t value)
{
    size_t result = 8;
    while (result < value && result < (SIZE_MAX >> 1)) {
        result <<= 1;
    }
    return result;
}

/* Credit the tokens owed since the bucket was last touched.
 *
 * A non-monotonic clock (NTP step, a caller passing wall time) can hand us a
 * `now` behind `updated`. Refusing to move backwards keeps the bucket from
 * gaining tokens it did not earn and from pinning `updated` in the future. */
static void
bucket_refill(WreathTokenBucket *self, WreathBucket *slot, double now)
{
    double elapsed = now - slot->updated;
    double tokens;
    if (elapsed <= 0.0) {
        return;
    }
    tokens = slot->tokens + elapsed * self->rate;
    slot->tokens = tokens > self->capacity ? self->capacity : tokens;
    slot->updated = now;
}

/* Locate `key`, or the slot it would occupy. Sets *found and returns the index,
 * or -1 with an exception set. Terminates because the load factor stays < 1. */
static Py_ssize_t
bucket_find(WreathTokenBucket *self, PyObject *key, Py_hash_t hash, int *found)
{
    size_t index = (size_t)hash & self->mask;
    for (;;) {
        WreathBucket *slot = &self->slots[index];
        if (slot->key == NULL) {
            *found = 0;
            return (Py_ssize_t)index;
        }
        if (slot->hash == hash) {
            /* Gated on the 64-bit hash, so the object comparison runs about
             * once per lookup rather than once per probe. */
            int equal = PyObject_RichCompareBool(slot->key, key, Py_EQ);
            if (equal < 0) {
                return -1;
            }
            if (equal) {
                *found = 1;
                return (Py_ssize_t)index;
            }
        }
        index = (index + 1) & self->mask;
    }
}

/* Rebuild into a `target`-slot table, dropping refilled-to-capacity buckets and
 * the bucket at `drop` (-1 to keep them all). Rebuilding rather than shifting
 * keeps probe chains exact without tombstones. */
static int
bucket_rebuild(WreathTokenBucket *self, size_t target, double now, Py_ssize_t drop)
{
    WreathBucket *old = self->slots;
    size_t old_slots = self->mask + 1;
    size_t new_mask = target - 1;
    size_t used = 0;
    WreathBucket *fresh = PyMem_Calloc(target, sizeof(WreathBucket));

    if (fresh == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (size_t i = 0; i < old_slots; i++) {
        WreathBucket *slot = &old[i];
        size_t index;
        if (slot->key == NULL) {
            continue;
        }
        bucket_refill(self, slot, now);
        if ((Py_ssize_t)i == drop || slot->tokens >= self->capacity) {
            Py_CLEAR(slot->key);
            continue;
        }
        index = (size_t)slot->hash & new_mask;
        while (fresh[index].key != NULL) {
            index = (index + 1) & new_mask;
        }
        fresh[index] = *slot; /* moves the key reference into the new table */
        used++;
    }
    PyMem_Free(old);
    self->slots = fresh;
    self->mask = new_mask;
    self->used = used;
    return 0;
}

/* Guarantee a free slot for one insert. Returns 0, or -1 with an exception.
 *
 * One O(slots) scan decides the whole strategy so the saturated path pays a
 * single rebuild, not two. Previously, at the ceiling with every bucket still
 * limiting, this ran a no-op reclaiming rebuild (which frees nothing), found
 * the table still full, scanned again for the fullest, then rebuilt a second
 * time -- ~3x O(slots) and two allocations per new key under a key flood.
 * Now the same scan refills, counts reclaimable (refilled-to-capacity) buckets
 * and tracks the fullest, so exactly one rebuild runs: grow, reclaim, or evict.
 */
static int
bucket_ensure_room(WreathTokenBucket *self, double now)
{
    size_t slots = self->mask + 1;
    size_t survivors = 0;      /* buckets a compaction would keep */
    size_t reclaimable = 0;    /* buckets refilled back to capacity (droppable) */
    size_t fullest = 0;
    double most_tokens = -1.0;
    int found_any = 0;
    size_t target;

    if (WREATH_HAS_ROOM(self)) {
        return 0;
    }
    /* The refill here is not wasted work: the rebuild below sees the same `now`
     * and skips it. */
    for (size_t i = 0; i < slots; i++) {
        WreathBucket *slot = &self->slots[i];
        if (slot->key == NULL) {
            continue;
        }
        bucket_refill(self, slot, now);
        if (slot->tokens < self->capacity) {
            survivors++;
        } else {
            reclaimable++;
        }
        if (slot->tokens > most_tokens) {
            most_tokens = slot->tokens;
            fullest = i;
            found_any = 1;
        }
    }
    target = slots;
    while (!WREATH_FITS(survivors, target) && target < self->max_slots) {
        target <<= 1;
    }
    /* Skip a rebuild that provably reclaims nothing: at the ceiling (no growth)
     * with no bucket refilled to capacity, a keep-all rebuild is a pure copy
     * that leaves the table just as full. Evict the fullest directly -- it is
     * the closest to idle, so its owner loses the least by starting over, and
     * the table stays inside its configured bound. This is the saturated
     * key-flood path; collapsing it to one rebuild is the whole point. */
    if (target == slots && reclaimable == 0) {
        if (!found_any) {
            return 0;
        }
        return bucket_rebuild(self, slots, now, (Py_ssize_t)fullest);
    }
    /* Grow and/or drop reclaimable buckets. */
    if (bucket_rebuild(self, target, now, -1) < 0) {
        return -1;
    }
    if (WREATH_HAS_ROOM(self)) {
        return 0;
    }
    /* Growth capped out and survivors alone exceed the load factor: evict the
     * fullest survivor. The pre-rebuild scan's `fullest` may have been dropped
     * as reclaimable, so re-select among what remains. */
    most_tokens = -1.0;
    found_any = 0;
    for (size_t i = 0; i <= self->mask; i++) {
        if (self->slots[i].key != NULL && self->slots[i].tokens > most_tokens) {
            most_tokens = self->slots[i].tokens;
            fullest = i;
            found_any = 1;
        }
    }
    if (!found_any) {
        return 0;
    }
    return bucket_rebuild(self, self->mask + 1, now, (Py_ssize_t)fullest);
}

static PyObject *
token_bucket_acquire(WreathTokenBucket *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"key", "now", "cost", NULL};
    PyObject *key;
    double now;
    double cost = 1.0;
    double retry_after = 0.0;
    Py_hash_t hash;
    int failed = 0;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "Ud|d:acquire", keywords, &key, &now, &cost)) {
        return NULL;
    }
    if (cost <= 0.0) {
        PyErr_SetString(PyExc_ValueError, "cost must be positive");
        return NULL;
    }
    if (cost > self->capacity) {
        PyErr_SetString(PyExc_ValueError, "cost exceeds the bucket capacity");
        return NULL;
    }
    hash = PyObject_Hash(key);
    if (hash == -1) {
        return NULL;
    }

    /* The table is mutable shared state, so a free-threaded build must not let
     * two requests probe and insert concurrently. This compiles away entirely
     * on a GIL build. */
    Py_BEGIN_CRITICAL_SECTION(self);
    int found = 0;
    Py_ssize_t index = bucket_find(self, key, hash, &found);
    if (index < 0) {
        failed = 1;
    }
    else if (found) {
        WreathBucket *slot = &self->slots[index];
        bucket_refill(self, slot, now);
        if (slot->tokens >= cost) {
            slot->tokens -= cost;
        }
        else {
            retry_after = (cost - slot->tokens) / self->rate;
        }
    }
    else if (bucket_ensure_room(self, now) < 0) {
        failed = 1;
    }
    else {
        /* ensure_room may have rebuilt the table, so the earlier index is stale. */
        index = bucket_find(self, key, hash, &found);
        if (index < 0) {
            failed = 1;
        }
        else {
            WreathBucket *slot = &self->slots[index];
            slot->key = Py_NewRef(key);
            slot->hash = hash;
            slot->tokens = self->capacity - cost;
            slot->updated = now;
            self->used++;
        }
    }
    Py_END_CRITICAL_SECTION();

    if (failed) {
        return NULL;
    }
    return PyFloat_FromDouble(retry_after);
}

static PyObject *
token_bucket_clear(WreathTokenBucket *self, PyObject *Py_UNUSED(ignored))
{
    Py_BEGIN_CRITICAL_SECTION(self);
    for (size_t i = 0; i <= self->mask; i++) {
        Py_CLEAR(self->slots[i].key);
    }
    self->used = 0;
    Py_END_CRITICAL_SECTION();
    Py_RETURN_NONE;
}

static PyObject *
token_bucket_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"capacity", "rate", "max_entries", NULL};
    double capacity;
    double rate;
    Py_ssize_t max_entries = 10000;
    WreathTokenBucket *self;
    size_t initial;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "dd|n:TokenBucket", keywords, &capacity, &rate, &max_entries)) {
        return NULL;
    }
    if (!(capacity > 0.0)) {
        PyErr_SetString(PyExc_ValueError, "capacity must be positive");
        return NULL;
    }
    if (!(rate > 0.0)) {
        PyErr_SetString(PyExc_ValueError, "rate must be positive");
        return NULL;
    }
    if (max_entries < 1) {
        PyErr_SetString(PyExc_ValueError, "max_entries must be at least 1");
        return NULL;
    }
    self = (WreathTokenBucket *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    /* Size the ceiling so max_entries live keys still fit under the load
     * factor, then start small and grow into it. */
    self->max_entries = (size_t)max_entries;
    self->max_slots = next_power_of_two((size_t)max_entries * 4 / 3 + 1);
    initial = self->max_slots < 64 ? self->max_slots : 64;
    self->slots = PyMem_Calloc(initial, sizeof(WreathBucket));
    if (self->slots == NULL) {
        Py_DECREF(self);
        return PyErr_NoMemory();
    }
    self->mask = initial - 1;
    self->used = 0;
    self->capacity = capacity;
    self->rate = rate;
    return (PyObject *)self;
}

static void
token_bucket_dealloc(WreathTokenBucket *self)
{
    if (self->slots != NULL) {
        for (size_t i = 0; i <= self->mask; i++) {
            Py_CLEAR(self->slots[i].key);
        }
        PyMem_Free(self->slots);
    }
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
token_bucket_get_tracked(WreathTokenBucket *self, void *closure)
{
    (void)closure;
    return PyLong_FromSize_t(self->used);
}

static PyObject *
token_bucket_get_slots(WreathTokenBucket *self, void *closure)
{
    (void)closure;
    return PyLong_FromSize_t(self->mask + 1);
}

static PyMethodDef token_bucket_methods[] = {
    {"acquire", (PyCFunction)(void (*)(void))token_bucket_acquire,
     METH_VARARGS | METH_KEYWORDS,
     "acquire(key, now, cost=1.0) -> float\n"
     "Consume `cost` tokens for `key`. Returns 0.0 when allowed, otherwise the\n"
     "seconds until enough tokens have refilled."},
    {"clear", (PyCFunction)token_bucket_clear, METH_NOARGS,
     "clear() -> None\nForget every tracked key."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef token_bucket_getset[] = {
    {"tracked", (getter)token_bucket_get_tracked, NULL, "keys currently tracked", NULL},
    {"slots", (getter)token_bucket_get_slots, NULL, "allocated table slots", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TokenBucketType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.TokenBucket",
    .tp_doc = "Bounded in-process token-bucket table.",
    .tp_basicsize = sizeof(WreathTokenBucket),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = token_bucket_new,
    .tp_dealloc = (destructor)token_bucket_dealloc,
    .tp_methods = token_bucket_methods,
    .tp_getset = token_bucket_getset,
};

int
wreath_register_ratelimit(PyObject *module)
{
    if (PyType_Ready(&TokenBucketType) < 0) {
        return -1;
    }
    return PyModule_AddObjectRef(module, "TokenBucket", (PyObject *)&TokenBucketType);
}
