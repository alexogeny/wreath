/* An in-process key/value table: open addressing, SIMD group probing, LRU
 * eviction, and lazy TTL.
 *
 * This is the engine under `wreath.kv`, and through it under every in-memory
 * store in the tree -- sessions, idempotency, the JWKS cache, the response
 * cache. They were all the same three things written out again: a dict, a
 * bound, and a deadline. Written in Python that costs 0.20-0.27us per read,
 * against a 0.05us floor for the bare dict lookup underneath; nearly all of the
 * difference is method-call overhead, `OrderedDict.move_to_end`, and a
 * `monotonic()` reading, none of which pure Python can shed.
 *
 * Layout. One byte of metadata per slot lives in `ctrl`, apart from the
 * entries, so a probe reads 32 bytes of tags rather than 32 entries of 40
 * bytes: the whole group is half a cache line and only the lanes the scan
 * flags are dereferenced. `simd.h` owns the scan and its four arms; the
 * encoding and the reason the group is 32 wide are documented there.
 *
 * Rebuilding, never tombstone-forever. A delete leaves 0xFE so the probe chain
 * that ran through it still terminates in the right place, and the table is
 * rebuilt once tombstones plus entries reach 7/8 of the slots. Rebuilding keeps
 * probe chains exact, which is what stops a long-lived table from degrading
 * into a linear scan -- the same argument `ratelimit.c` makes for its own.
 *
 * The clock is read here rather than passed in. `time.monotonic()` costs about
 * as much from Python as this whole lookup, so a caller that had to pass `now`
 * would pay the thing being optimised away; `PyTime_Monotonic` is the same
 * reading without the round trip. Every method still accepts an explicit `now`,
 * because a test that cannot move the clock cannot test expiry at all.
 *
 * **No internal lock**, exactly as the `BoundedCache` it replaces had none.
 * Every caller is event-loop-local, and `wreath.queue` is the primitive for the
 * cross-thread hand-offs. Documented in `wreath.kv` rather than left implied.
 */
#include "wreathcore.h"

#include "simd.h"

#include <math.h>

#if defined(_MSC_VER)
#include <intrin.h>
static inline int
kv_ctz(uint32_t value)
{
    unsigned long index;
    _BitScanForward(&index, value);
    return (int)index;
}
#else
static inline int
kv_ctz(uint32_t value)
{
    return __builtin_ctz(value);
}
#endif

typedef struct {
    PyObject *key;   /* owned; NULL unless ctrl marks this slot full */
    PyObject *value; /* owned */
    Py_hash_t hash;
    double deadline; /* absolute, in the monotonic clock; HUGE_VAL never expires */
    size_t cost;     /* what the caller says this entry retains, in bytes */
    int32_t prev;    /* toward the most recently used; -1 terminates */
    int32_t next;    /* toward the least recently used; -1 terminates */
} WreathKVSlot;

typedef struct {
    PyObject_HEAD
    uint8_t *ctrl;
    WreathKVSlot *slots;
    size_t slot_count; /* power of two, never below WREATH_CTRL_GROUP */
    size_t slot_mask;
    size_t used;       /* live entries */
    size_t tombstones;
    size_t max_slots;  /* ceiling the table may grow to */
    size_t max_entries;
    int32_t lru_head; /* most recently used */
    int32_t lru_tail; /* least recently used -- the eviction victim */
    double ttl;       /* default lifetime in seconds; HUGE_VAL for none */
    size_t bytes;     /* sum of every live entry's cost */
    size_t max_bytes; /* ceiling on that sum; 0 means unbounded */
    /* Evicted (key, value) pairs the caller has not collected, or NULL when
     * the caller did not ask for them. Only *evictions* land here -- an expiry
     * or an explicit delete does not, because the caller already knows about
     * those and an eviction is the one the table decides on its own. */
    PyObject *evicted;
    /* A caller-supplied time source, or NULL for the C monotonic clock.
     *
     * The default is NULL and that is the fast path: reading the clock in C
     * costs nothing a caller can see, where calling back into Python costs
     * about as much as the lookup it is timing. A table only pays for a clock
     * when a caller injects one, which in practice means a test. */
    PyObject *clock;
    uint64_t hits;
    uint64_t misses;
    uint64_t evictions;
    uint64_t expirations;
} WreathKV;

/* Grow once entries plus tombstones reach 7/8 of the slots. Linear group
 * probing needs a reliable empty lane to stop on, and the margin is what
 * guarantees one exists. */
#define WREATH_KV_FITS(occupied, slots) (((occupied) + 1) * 8 <= (slots) * 7)

static size_t
kv_next_power_of_two(size_t value)
{
    size_t result = WREATH_CTRL_GROUP;
    while (result < value && result < (SIZE_MAX >> 1)) {
        result <<= 1;
    }
    return result;
}

/* Python's hash is the identity for a small int and siphash for a str, so the
 * high bits a tag is cut from are all zero for one and well mixed for the
 * other. Finalising with splitmix64 makes the two behave alike; without it an
 * int-keyed table gives every entry tag 0 and the group scan flags all 32
 * lanes on every probe. */
static inline uint64_t
kv_mix(Py_hash_t hash)
{
    uint64_t value = (uint64_t)hash;
    value ^= value >> 30;
    value *= 0xbf58476d1ce4e5b9ULL;
    value ^= value >> 27;
    value *= 0x94d049bb133111ebULL;
    value ^= value >> 31;
    return value;
}

/* Bits 57..63, so a tag never shares bits with the group index taken from the
 * low end, and never has its high bit set -- which is what keeps a tag scan
 * from ever matching an empty or deleted lane. */
static inline uint8_t
kv_tag(uint64_t mixed)
{
    return (uint8_t)(mixed >> 57);
}

static inline double
kv_clock(void)
{
    PyTime_t now;
    if (PyTime_Monotonic(&now) < 0) {
        /* Documented as failing only when the platform clock is unavailable,
         * which no supported platform does. Reporting 0.0 would make every
         * entry look freshly written; HUGE_VAL expires everything instead, so
         * the degraded mode is a cold cache rather than a stale one. */
        PyErr_Clear();
        return HUGE_VAL;
    }
    return (double)now * 1e-9;
}

/* Resolve the time for one operation, in order of specificity: an explicit
 * `now` argument, then the table's injected clock, then the C monotonic clock.
 * Returns -1 with an exception set on a bad value.
 *
 * The three-step order is what lets one table serve both a caller that threads
 * its own time through every call and a caller that installs a clock once. */
static int
kv_resolve_now(WreathKV *self, PyObject *value, double *out)
{
    double parsed;
    if (value == NULL || value == Py_None) {
        if (self->clock == NULL) {
            *out = kv_clock();
            return 0;
        }
        value = PyObject_CallNoArgs(self->clock);
        if (value == NULL) {
            return -1;
        }
        parsed = PyFloat_AsDouble(value);
        Py_DECREF(value);
        if (parsed == -1.0 && PyErr_Occurred() != NULL) {
            return -1;
        }
        *out = parsed;
        return 0;
    }
    parsed = PyFloat_AsDouble(value);
    if (parsed == -1.0 && PyErr_Occurred() != NULL) {
        return -1;
    }
    *out = parsed;
    return 0;
}

/* The table's own idea of "now", for the paths that take no argument. */
static int
kv_now(WreathKV *self, double *out)
{
    return kv_resolve_now(self, NULL, out);
}

/* Resolve a `ttl` argument into an absolute deadline. None means the table's
 * own default. */
static int
kv_resolve_deadline(WreathKV *self, PyObject *value, double now, double *out)
{
    double seconds;
    if (value == NULL || value == Py_None) {
        *out = self->ttl == HUGE_VAL ? HUGE_VAL : now + self->ttl;
        return 0;
    }
    seconds = PyFloat_AsDouble(value);
    if (seconds == -1.0 && PyErr_Occurred() != NULL) {
        return -1;
    }
    if (!(seconds > 0.0)) {
        PyErr_SetString(PyExc_ValueError, "ttl must be positive");
        return -1;
    }
    *out = now + seconds;
    return 0;
}

/* --- the recency list ---------------------------------------------------- */

static void
kv_lru_unlink(WreathKV *self, int32_t index)
{
    WreathKVSlot *slot = &self->slots[index];
    if (slot->prev >= 0) {
        self->slots[slot->prev].next = slot->next;
    } else {
        self->lru_head = slot->next;
    }
    if (slot->next >= 0) {
        self->slots[slot->next].prev = slot->prev;
    } else {
        self->lru_tail = slot->prev;
    }
    slot->prev = -1;
    slot->next = -1;
}

static void
kv_lru_push_front(WreathKV *self, int32_t index)
{
    WreathKVSlot *slot = &self->slots[index];
    slot->prev = -1;
    slot->next = self->lru_head;
    if (self->lru_head >= 0) {
        self->slots[self->lru_head].prev = index;
    }
    self->lru_head = index;
    if (self->lru_tail < 0) {
        self->lru_tail = index;
    }
}

/* Move an entry to the front. Skipped when it is already there, which is the
 * common case for a hot key and saves six pointer writes per hit. */
static inline void
kv_lru_touch(WreathKV *self, int32_t index)
{
    if (self->lru_head == index) {
        return;
    }
    kv_lru_unlink(self, index);
    kv_lru_push_front(self, index);
}

/* --- probing ------------------------------------------------------------- */

/* Locate `key`. On success sets *index to its slot and returns 1; on a miss
 * sets *insert_at to the slot an insert should take and returns 0; returns -1
 * with an exception set if a key comparison raised.
 *
 * The probe walks whole groups. Within a group the tag scan gives a mask of
 * candidate lanes, each of which is confirmed against the stored hash and then
 * the key itself -- the hash gate means the Python comparison runs about once
 * per lookup rather than once per candidate. A group holding an empty lane ends
 * the walk: nothing was ever displaced past it.
 */
static int
kv_probe(WreathKV *self, PyObject *key, uint64_t mixed, Py_hash_t hash,
         size_t *index, size_t *insert_at)
{
    size_t group = (size_t)mixed & self->slot_mask & ~(size_t)(WREATH_CTRL_GROUP - 1);
    uint8_t tag = kv_tag(mixed);
    int have_insert = 0;
    size_t candidate = 0;

    for (;;) {
        const uint8_t *ctrl = self->ctrl + group;
        uint32_t matches = wreath_ctrl_eq(ctrl, tag);
        while (matches != 0) {
            size_t slot = group + (size_t)kv_ctz(matches);
            WreathKVSlot *entry = &self->slots[slot];
            if (entry->hash == hash) {
                int equal = entry->key == key
                                ? 1
                                : PyObject_RichCompareBool(entry->key, key, Py_EQ);
                if (equal < 0) {
                    return -1;
                }
                if (equal) {
                    *index = slot;
                    return 1;
                }
            }
            matches &= matches - 1;
        }
        if (!have_insert) {
            /* The first free lane on the path, tombstone or empty. Taking a
             * tombstone is safe: the chain that ran through it still ends at
             * the same empty lane it always did. */
            uint32_t free_lanes = wreath_ctrl_high(ctrl);
            if (free_lanes != 0) {
                candidate = group + (size_t)kv_ctz(free_lanes);
                have_insert = 1;
            }
        }
        if (wreath_ctrl_eq(ctrl, (uint8_t)WREATH_CTRL_EMPTY) != 0) {
            *insert_at = candidate;
            return 0;
        }
        group = (group + WREATH_CTRL_GROUP) & self->slot_mask;
    }
}

/* Place an entry known to be absent, without re-probing for equality. Used only
 * by the rebuild, where every key came from a table that already held them
 * uniquely. */
static size_t
kv_place(uint8_t *ctrl, WreathKVSlot *slots, size_t mask, uint64_t mixed)
{
    size_t group = (size_t)mixed & mask & ~(size_t)(WREATH_CTRL_GROUP - 1);
    (void)slots;
    for (;;) {
        uint32_t free_lanes = wreath_ctrl_high(ctrl + group);
        if (free_lanes != 0) {
            return group + (size_t)kv_ctz(free_lanes);
        }
        group = (group + WREATH_CTRL_GROUP) & mask;
    }
}

/* Rebuild into a `target`-slot table, dropping tombstones and every entry whose
 * deadline has passed.
 *
 * The recency list is rebuilt with it. Walking the old list head to tail and
 * appending in the same order preserves it exactly, which matters because the
 * order is what eviction reads: rebuilding into insertion order instead would
 * silently make this a FIFO. Returns the number of expired entries dropped, or
 * -1 with an exception set.
 */
static Py_ssize_t
kv_rebuild(WreathKV *self, size_t target, double now)
{
    uint8_t *ctrl = PyMem_Malloc(target);
    WreathKVSlot *slots = PyMem_Calloc(target, sizeof(WreathKVSlot));
    size_t mask = target - 1;
    size_t used = 0;
    size_t bytes = 0;
    Py_ssize_t dropped = 0;
    int32_t head = -1;
    int32_t tail = -1;
    int32_t cursor;

    if (ctrl == NULL || slots == NULL) {
        PyMem_Free(ctrl);
        PyMem_Free(slots);
        PyErr_NoMemory();
        return -1;
    }
    memset(ctrl, (int)WREATH_CTRL_EMPTY, target);

    for (cursor = self->lru_head; cursor >= 0; cursor = self->slots[cursor].next) {
        WreathKVSlot *entry = &self->slots[cursor];
        uint64_t mixed = kv_mix(entry->hash);
        size_t slot;
        if (now >= entry->deadline) {
            Py_CLEAR(entry->key);
            Py_CLEAR(entry->value);
            dropped++;
            continue;
        }
        slot = kv_place(ctrl, slots, mask, mixed);
        ctrl[slot] = kv_tag(mixed);
        slots[slot].key = entry->key;     /* ownership moves into the new table */
        slots[slot].value = entry->value;
        slots[slot].hash = entry->hash;
        slots[slot].deadline = entry->deadline;
        slots[slot].cost = entry->cost;
        slots[slot].prev = tail;
        slots[slot].next = -1;
        if (tail >= 0) {
            slots[tail].next = (int32_t)slot;
        } else {
            head = (int32_t)slot;
        }
        tail = (int32_t)slot;
        used++;
        bytes += entry->cost;
    }

    PyMem_Free(self->ctrl);
    PyMem_Free(self->slots);
    self->ctrl = ctrl;
    self->slots = slots;
    self->slot_count = target;
    self->slot_mask = mask;
    self->used = used;
    self->bytes = bytes;
    self->tombstones = 0;
    self->lru_head = head;
    self->lru_tail = tail;
    return dropped;
}

static void
kv_release(WreathKV *self, size_t slot)
{
    WreathKVSlot *entry = &self->slots[slot];
    kv_lru_unlink(self, (int32_t)slot);
    Py_CLEAR(entry->key);
    Py_CLEAR(entry->value);
    entry->deadline = 0.0;
    entry->hash = 0;
    self->bytes -= entry->cost;
    entry->cost = 0;
    self->ctrl[slot] = (uint8_t)WREATH_CTRL_DELETED;
    self->used--;
    self->tombstones++;
}

/* Drop the least recently used entry, recording it first when the caller asked
 * to be told. Returns 0, or -1 with an exception set.
 *
 * The recording is what lets a cache whose entries own something outside the
 * table use one. `wreath._pgdriver` is the case that made it necessary: an
 * evicted prepared plan still exists on the PostgreSQL backend until a
 * `Close ('S')` goes out on the wire, so a table that evicted silently would
 * leak a server-side statement per eviction. `orm/registry` needs none of
 * this and passes `track_evictions=False`, which costs it nothing.
 *
 * The log is unbounded, deliberately: it replaces `_pending_closes`, which was
 * an unbounded list drained on the next operation, and bounding it here would
 * mean *losing* a Close rather than delaying one. A caller that never drains it
 * is the bug, and `take_evicted` is how it drains. */
static int
kv_evict_tail(WreathKV *self)
{
    size_t slot = (size_t)self->lru_tail;
    if (self->evicted != NULL) {
        PyObject *pair = PyTuple_Pack(2, self->slots[slot].key, self->slots[slot].value);
        int appended;
        if (pair == NULL) {
            return -1;
        }
        appended = PyList_Append(self->evicted, pair);
        Py_DECREF(pair);
        if (appended < 0) {
            return -1;
        }
    }
    kv_release(self, slot);
    self->evictions++;
    return 0;
}

/* Bring the table back inside both ceilings after an insert.
 *
 * Runs *after* rather than before, because the entry's own cost is what may
 * have breached the byte budget and its cost is not known until it is in. An
 * entry whose cost alone exceeds the budget is therefore evicted again
 * immediately, leaving the table empty rather than over its bound -- which is
 * exactly what the two hand-written caches this replaces did. */
static int
kv_enforce_budget(WreathKV *self)
{
    while (self->lru_tail >= 0
           && ((self->used > self->max_entries)
               || (self->max_bytes != 0 && self->bytes > self->max_bytes))) {
        if (kv_evict_tail(self) < 0) {
            return -1;
        }
    }
    return 0;
}

/* Guarantee a free slot for one insert. Returns 0, or -1 with an exception. */
static int
kv_ensure_room(WreathKV *self, double now)
{
    size_t occupied = self->used + self->tombstones;
    size_t target;
    Py_ssize_t expired;

    if (WREATH_KV_FITS(occupied, self->slot_count) && self->used < self->max_entries) {
        return 0;
    }
    /* Grow if there is headroom, otherwise rebuild at the same size to clear
     * tombstones and expired entries. Either way one rebuild, and it also
     * reclaims every entry whose deadline has passed -- which is what makes a
     * TTL'd table self-limiting without a sweep thread. */
    target = self->slot_count;
    while (!WREATH_KV_FITS(self->used, target) && target < self->max_slots) {
        target <<= 1;
    }
    expired = kv_rebuild(self, target, now);
    if (expired < 0) {
        return -1;
    }
    /* Counted here and not only inside the rebuild, because the rebuild is also
     * how a table reclaims its dead. Dropping this made `expirations` come up
     * short on nine of thirty randomised trials while every operation result
     * and every other counter matched exactly -- the shape of defect that is
     * invisible until somebody reads the number. `test_kv.py`'s conservation
     * check is what catches it: admissions == evictions + expirations +
     * removals + residents, and nothing else. */
    self->expirations += (uint64_t)expired;
    /* Evict until both bounds hold. The tail is the least recently used, so a
     * key nobody has read since the last eviction goes first. */
    while (self->used >= self->max_entries
           || !WREATH_KV_FITS(self->used + self->tombstones, self->slot_count)) {
        if (self->lru_tail < 0) {
            break;
        }
        if (kv_evict_tail(self) < 0) {
            return -1;
        }
    }
    return 0;
}

/* Look `key` up and drop it if its deadline has passed. Returns 1 with *slot
 * set when the entry is live, 0 when it is absent or expired, -1 on error. */
static int
kv_lookup_live(WreathKV *self, PyObject *key, double now, size_t *slot)
{
    Py_hash_t hash = PyObject_Hash(key);
    uint64_t mixed;
    size_t index;
    size_t insert_at;
    int found;

    if (hash == -1 && PyErr_Occurred() != NULL) {
        return -1;
    }
    mixed = kv_mix(hash);
    found = kv_probe(self, key, mixed, hash, &index, &insert_at);
    if (found < 0) {
        return -1;
    }
    if (!found) {
        return 0;
    }
    if (now >= self->slots[index].deadline) {
        kv_release(self, index);
        self->expirations++;
        return 0;
    }
    *slot = index;
    return 1;
}

/* --- argument binding ----------------------------------------------------- */

/* Bind a vectorcall frame onto `slots`, which the caller pre-fills with
 * defaults. `names` lists every parameter in positional order.
 *
 * This exists because the hot methods are `METH_FASTCALL | METH_KEYWORDS` and
 * not `METH_VARARGS | METH_KEYWORDS`, and that is not a stylistic preference:
 * the VARARGS convention builds an argument *tuple* and a keyword *dict* for
 * every single call. Measured on this table, `wreath.cache.BoundedCache.get`
 * -- one Python method that forwards to `KV.get` with one keyword -- cost
 * 0.51us against the 0.20us the lookup underneath it takes, and it was
 * *slower* than the Python dict-and-deque it replaced. Two objects allocated
 * and freed per lookup is the whole difference.
 *
 * Keyword lookup is a linear scan over at most five short names, which beats a
 * hash for this size and needs no interned-string table to keep in step with
 * the signatures.
 */
int
wreath_bind_args(PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames,
                 const char *const *names, PyObject **slots, Py_ssize_t count,
                 Py_ssize_t required, const char *what)
{
    Py_ssize_t supplied = nargs;

    if (nargs > count) {
        PyErr_Format(PyExc_TypeError, "%s() takes at most %zd positional arguments "
                     "but %zd were given", what, count, nargs);
        return -1;
    }
    for (Py_ssize_t i = 0; i < nargs; i++) {
        slots[i] = args[i];
    }
    if (kwnames != NULL) {
        Py_ssize_t extra = PyTuple_GET_SIZE(kwnames);
        for (Py_ssize_t i = 0; i < extra; i++) {
            PyObject *name = PyTuple_GET_ITEM(kwnames, i);
            Py_ssize_t slot;
            for (slot = 0; slot < count; slot++) {
                int same = PyUnicode_CompareWithASCIIString(name, names[slot]);
                if (same == 0) {
                    break;
                }
                if (same == -1 && PyErr_Occurred() != NULL) {
                    return -1;
                }
            }
            if (slot == count) {
                PyErr_Format(PyExc_TypeError,
                             "%s() got an unexpected keyword argument '%U'", what, name);
                return -1;
            }
            if (slot < nargs) {
                PyErr_Format(PyExc_TypeError,
                             "%s() got multiple values for argument '%s'", what,
                             names[slot]);
                return -1;
            }
            slots[slot] = args[nargs + i];
            if (slot + 1 > supplied) {
                supplied = slot + 1;
            }
        }
    }
    for (Py_ssize_t i = 0; i < required; i++) {
        if (slots[i] == NULL) {
            PyErr_Format(PyExc_TypeError, "%s() missing required argument '%s'", what,
                         names[i]);
            return -1;
        }
    }
    return 0;
}

/* --- methods ------------------------------------------------------------- */

PyDoc_STRVAR(kv_get_doc,
"get(key, default=None, now=None)\n"
"--\n\n"
"The value stored under `key`, or `default` when it is absent or expired.\n"
"A hit moves the key to the front of the recency order.\n\n"
"`now` is positional as well as keyword so a wrapper on an injected clock can\n"
"pass it without a keyword dict; see `wreath_bind_args`.");

static const char *const kv_get_names[] = {"key", "default", "now"};

static PyObject *
kv_get(WreathKV *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[3] = {NULL, Py_None, NULL};
    PyObject *key;
    PyObject *fallback;
    double now;
    size_t slot;
    int live;

    if (wreath_bind_args(args, nargs, kwnames, kv_get_names, slots, 3, 1, "get") < 0) {
        return NULL;
    }
    key = slots[0];
    fallback = slots[1];
    if (kv_resolve_now(self, slots[2], &now) < 0) {
        return NULL;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return NULL;
    }
    if (!live) {
        self->misses++;
        return Py_NewRef(fallback);
    }
    self->hits++;
    kv_lru_touch(self, (int32_t)slot);
    return Py_NewRef(self->slots[slot].value);
}

PyDoc_STRVAR(kv_peek_doc,
"peek(key, default=None, now=None)\n"
"--\n\n"
"The value under `key` **without** counting a hit or a miss, without moving\n"
"it in the recency order, and without dropping it if it has expired.\n\n"
"The read that does not disturb what it is reading: a membership test, a\n"
"diagnostic, an operator asking what is cached. `get` is the one that means\n"
"'I am using this value', and only that one should shape eviction.");

static PyObject *
kv_peek(WreathKV *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[3] = {NULL, Py_None, NULL};
    PyObject *key;
    PyObject *fallback;
    double now;
    Py_hash_t hash;
    size_t index;
    size_t insert_at;
    int found;

    if (wreath_bind_args(args, nargs, kwnames, kv_get_names, slots, 3, 1, "peek") < 0) {
        return NULL;
    }
    key = slots[0];
    fallback = slots[1];
    if (kv_resolve_now(self, slots[2], &now) < 0) {
        return NULL;
    }
    hash = PyObject_Hash(key);
    if (hash == -1 && PyErr_Occurred() != NULL) {
        return NULL;
    }
    /* Deliberately not `kv_lookup_live`, which releases an expired entry and
     * counts the expiry. Peeking must leave the table exactly as it found it. */
    found = kv_probe(self, key, kv_mix(hash), hash, &index, &insert_at);
    if (found < 0) {
        return NULL;
    }
    if (!found || now >= self->slots[index].deadline) {
        return Py_NewRef(fallback);
    }
    return Py_NewRef(self->slots[index].value);
}

PyDoc_STRVAR(kv_set_doc,
"set(key, value, ttl=None, now=None, keep_deadline=False, cost=0)\n"
"--\n\n"
"Store `value` under `key`, evicting the least recently used entry if the\n"
"table is at its ceiling.\n\n"
"`keep_deadline` preserves the deadline a live key already has instead of\n"
"starting a fresh window. That is the rule a claim ledger needs -- a holder\n"
"that keeps writing must not be able to extend its own key indefinitely --\n"
"and it lives here so `wreath.store`'s two backends cannot drift apart on it.\n\n"
"`cost` is what this entry retains in bytes, for a table built with\n"
"`max_bytes`. It is the caller's number because only the caller knows what a\n"
"value really holds -- a plan that references shared registry metadata is not\n"
"charged for it, and no generic sizing function could know that.");

static const char *const kv_set_names[] = {"key", "value", "ttl", "now",
                                          "keep_deadline", "cost"};

static PyObject *
kv_set(WreathKV *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[6] = {NULL, NULL, NULL, NULL, NULL, NULL};
    PyObject *key;
    PyObject *value;
    int keep_deadline = 0;
    size_t cost = 0;
    double now;
    double deadline;
    Py_hash_t hash;
    uint64_t mixed;
    size_t index;
    size_t insert_at;
    int found;

    if (wreath_bind_args(args, nargs, kwnames, kv_set_names, slots, 6, 2, "set") < 0) {
        return NULL;
    }
    key = slots[0];
    value = slots[1];
    if (slots[5] != NULL) {
        Py_ssize_t parsed = PyNumber_AsSsize_t(slots[5], PyExc_OverflowError);
        if (parsed == -1 && PyErr_Occurred() != NULL) {
            return NULL;
        }
        if (parsed < 0) {
            PyErr_SetString(PyExc_ValueError, "cost cannot be negative");
            return NULL;
        }
        cost = (size_t)parsed;
    }
    if (slots[4] != NULL) {
        keep_deadline = PyObject_IsTrue(slots[4]);
        if (keep_deadline < 0) {
            return NULL;
        }
    }
    if (kv_resolve_now(self, slots[3], &now) < 0) {
        return NULL;
    }
    if (kv_resolve_deadline(self, slots[2], now, &deadline) < 0) {
        return NULL;
    }
    hash = PyObject_Hash(key);
    if (hash == -1 && PyErr_Occurred() != NULL) {
        return NULL;
    }
    mixed = kv_mix(hash);
    found = kv_probe(self, key, mixed, hash, &index, &insert_at);
    if (found < 0) {
        return NULL;
    }
    if (found) {
        WreathKVSlot *entry = &self->slots[index];
        if (now >= entry->deadline) {
            /* Expired in place: the window restarts even under keep_deadline,
             * because there is no live window left to keep. */
            self->expirations++;
        } else if (keep_deadline) {
            deadline = entry->deadline;
        }
        Py_SETREF(entry->value, Py_NewRef(value));
        entry->deadline = deadline;
        self->bytes = self->bytes - entry->cost + cost;
        entry->cost = cost;
        kv_lru_touch(self, (int32_t)index);
        if (kv_enforce_budget(self) < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (kv_ensure_room(self, now) < 0) {
        return NULL;
    }
    /* The rebuild inside kv_ensure_room invalidates every slot index, so the
     * insertion point has to be found again. Re-probing rather than trying to
     * translate the old index is not a missed optimisation: a rebuild is rare,
     * and a stale index here would write an entry over a live one. */
    found = kv_probe(self, key, mixed, hash, &index, &insert_at);
    if (found < 0) {
        return NULL;
    }
    if (found) {
        /* Only reachable if a key comparison has side effects that mutated the
         * table. Treat it as an update rather than corrupting the invariants. */
        WreathKVSlot *entry = &self->slots[index];
        Py_SETREF(entry->value, Py_NewRef(value));
        entry->deadline = deadline;
        self->bytes = self->bytes - entry->cost + cost;
        entry->cost = cost;
        kv_lru_touch(self, (int32_t)index);
        if (kv_enforce_budget(self) < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (self->ctrl[insert_at] == (uint8_t)WREATH_CTRL_DELETED) {
        self->tombstones--;
    }
    self->ctrl[insert_at] = kv_tag(mixed);
    self->slots[insert_at].key = Py_NewRef(key);
    self->slots[insert_at].value = Py_NewRef(value);
    self->slots[insert_at].hash = hash;
    self->slots[insert_at].deadline = deadline;
    self->slots[insert_at].cost = cost;
    self->slots[insert_at].prev = -1;
    self->slots[insert_at].next = -1;
    self->used++;
    self->bytes += cost;
    kv_lru_push_front(self, (int32_t)insert_at);
    if (kv_enforce_budget(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

PyDoc_STRVAR(kv_claim_doc,
"claim(key, value=None, ttl=None, now=None)\n"
"--\n\n"
"Store `value` under `key` only if nothing live is there, and report whether\n"
"this call was the one that took it.\n\n"
"There is no await between the lookup and the write, so no other task on this\n"
"loop can interleave -- the in-process counterpart of the single\n"
"`INSERT ... ON CONFLICT` statement a shared store claims with.");

static const char *const kv_claim_names[] = {"key", "value", "ttl", "now"};

static PyObject *
kv_claim(WreathKV *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[4] = {NULL, Py_None, NULL, NULL};
    PyObject *key;
    PyObject *value;
    double now;
    double deadline;
    size_t slot;
    int live;
    Py_hash_t hash;
    uint64_t mixed;
    size_t index;
    size_t insert_at;
    int found;

    if (wreath_bind_args(args, nargs, kwnames, kv_claim_names, slots, 4, 1, "claim") < 0) {
        return NULL;
    }
    key = slots[0];
    value = slots[1];
    if (kv_resolve_now(self, slots[3], &now) < 0) {
        return NULL;
    }
    if (kv_resolve_deadline(self, slots[2], now, &deadline) < 0) {
        return NULL;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return NULL;
    }
    if (live) {
        Py_RETURN_FALSE;
    }
    hash = PyObject_Hash(key);
    if (hash == -1 && PyErr_Occurred() != NULL) {
        return NULL;
    }
    mixed = kv_mix(hash);
    if (kv_ensure_room(self, now) < 0) {
        return NULL;
    }
    found = kv_probe(self, key, mixed, hash, &index, &insert_at);
    if (found < 0) {
        return NULL;
    }
    if (found) {
        WreathKVSlot *entry = &self->slots[index];
        Py_SETREF(entry->value, Py_NewRef(value));
        entry->deadline = deadline;
        kv_lru_touch(self, (int32_t)index);
        Py_RETURN_TRUE;
    }
    if (self->ctrl[insert_at] == (uint8_t)WREATH_CTRL_DELETED) {
        self->tombstones--;
    }
    self->ctrl[insert_at] = kv_tag(mixed);
    self->slots[insert_at].key = Py_NewRef(key);
    self->slots[insert_at].value = Py_NewRef(value);
    self->slots[insert_at].hash = hash;
    self->slots[insert_at].deadline = deadline;
    self->slots[insert_at].cost = 0;
    self->slots[insert_at].prev = -1;
    self->slots[insert_at].next = -1;
    self->used++;
    kv_lru_push_front(self, (int32_t)insert_at);
    Py_RETURN_TRUE;
}

PyDoc_STRVAR(kv_delete_doc,
"delete(key)\n"
"--\n\n"
"Drop `key`, reporting whether it was there. An expired entry counts as absent.");

static PyObject *
kv_delete(WreathKV *self, PyObject *key)
{
    size_t slot;
    double now;
    int live;
    if (kv_now(self, &now) < 0) {
        return NULL;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return NULL;
    }
    if (!live) {
        Py_RETURN_FALSE;
    }
    kv_release(self, slot);
    Py_RETURN_TRUE;
}

PyDoc_STRVAR(kv_pop_doc,
"pop(key, default=None, *, now=None)\n"
"--\n\n"
"Remove `key` and return its value, or `default` when it is absent or expired.");

static PyObject *
kv_pop(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"key", "default", "now", NULL};
    PyObject *key;
    PyObject *fallback = Py_None;
    PyObject *now_arg = NULL;
    PyObject *value;
    double now;
    size_t slot;
    int live;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|O$O:pop", keywords, &key,
                                     &fallback, &now_arg)) {
        return NULL;
    }
    if (kv_resolve_now(self, now_arg, &now) < 0) {
        return NULL;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return NULL;
    }
    if (!live) {
        self->misses++;
        return Py_NewRef(fallback);
    }
    self->hits++;
    /* Owned before the release, which clears the slot's own reference. */
    value = Py_NewRef(self->slots[slot].value);
    kv_release(self, slot);
    return value;
}

PyDoc_STRVAR(kv_touch_doc,
"touch(key, *, ttl=None, now=None)\n"
"--\n\n"
"Start a fresh window for a live `key` and move it to the front of the\n"
"recency order. Reports whether there was anything live to touch.");

static PyObject *
kv_touch(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"key", "ttl", "now", NULL};
    PyObject *key;
    PyObject *ttl_arg = NULL;
    PyObject *now_arg = NULL;
    double now;
    double deadline;
    size_t slot;
    int live;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|$OO:touch", keywords, &key,
                                     &ttl_arg, &now_arg)) {
        return NULL;
    }
    if (kv_resolve_now(self, now_arg, &now) < 0) {
        return NULL;
    }
    if (kv_resolve_deadline(self, ttl_arg, now, &deadline) < 0) {
        return NULL;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return NULL;
    }
    if (!live) {
        Py_RETURN_FALSE;
    }
    self->slots[slot].deadline = deadline;
    kv_lru_touch(self, (int32_t)slot);
    Py_RETURN_TRUE;
}

PyDoc_STRVAR(kv_purge_doc,
"purge(*, now=None)\n"
"--\n\n"
"Drop every entry whose deadline has passed, returning how many went.\n\n"
"Nothing calls this for you. Expiry is lazy -- on read, and during the rebuild\n"
"an insert triggers -- so a table that is being used never needs it; a table\n"
"that has gone quiet holding large values does.");

static PyObject *
kv_purge(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"now", NULL};
    PyObject *now_arg = NULL;
    double now;
    Py_ssize_t dropped;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|$O:purge", keywords, &now_arg)) {
        return NULL;
    }
    if (kv_resolve_now(self, now_arg, &now) < 0) {
        return NULL;
    }
    dropped = kv_rebuild(self, self->slot_count, now);
    if (dropped < 0) {
        return NULL;
    }
    self->expirations += (uint64_t)dropped;
    return PyLong_FromSsize_t(dropped);
}

PyDoc_STRVAR(kv_take_evicted_doc,
"take_evicted()\n"
"--\n\n"
"The `(key, value)` pairs evicted since the last call, and clears the record.\n\n"
"Empty unless the table was built with `track_evictions=True`. Only evictions\n"
"appear: an expiry or an explicit delete does not, because the caller already\n"
"knows about those, and an eviction is the one the table decided on its own.\n\n"
"For a cache whose entries own something outside the table -- a prepared\n"
"statement on a database backend, a file handle -- where evicting silently\n"
"would leak it.");

static PyObject *
kv_take_evicted(WreathKV *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *taken;
    PyObject *fresh;
    if (self->evicted == NULL) {
        return PyList_New(0);
    }
    fresh = PyList_New(0);
    if (fresh == NULL) {
        return NULL;
    }
    /* Swapped rather than copied-and-cleared: the caller gets the list itself,
     * so a large batch costs no second allocation and the table cannot be left
     * holding entries it has already handed over. */
    taken = self->evicted;
    self->evicted = fresh;
    return taken;
}

PyDoc_STRVAR(kv_clear_doc,
"clear()\n"
"--\n\n"
"Drop every entry, live or expired, and report how many went.\n\n"
"Counters are left alone: they describe the table's history, and a test that\n"
"clears between cases still wants them. The count is returned because\n"
"`wreath.queue`'s `clear` returns one, and one family should not have two\n"
"answers to what `clear` gives back.");

static PyObject *
kv_clear(WreathKV *self, PyObject *Py_UNUSED(ignored))
{
    Py_ssize_t held = (Py_ssize_t)self->used;
    for (size_t i = 0; i < self->slot_count; i++) {
        if ((self->ctrl[i] & 0x80u) == 0) {
            Py_CLEAR(self->slots[i].key);
            Py_CLEAR(self->slots[i].value);
        }
        self->ctrl[i] = (uint8_t)WREATH_CTRL_EMPTY;
        self->slots[i].prev = -1;
        self->slots[i].next = -1;
    }
    self->used = 0;
    self->bytes = 0;
    self->tombstones = 0;
    self->lru_head = -1;
    self->lru_tail = -1;
    if (self->evicted != NULL && PyList_SetSlice(self->evicted, 0, PY_SSIZE_T_MAX, NULL) < 0) {
        return NULL;
    }
    return PyLong_FromSsize_t(held);
}

/* Build a list of the live entries, most recently used first. `what` selects
 * keys (0), values (1) or two-tuples (2). */
static PyObject *
kv_collect(WreathKV *self, double now, int what)
{
    PyObject *out = PyList_New(0);
    int32_t cursor;

    if (out == NULL) {
        return NULL;
    }
    for (cursor = self->lru_head; cursor >= 0; cursor = self->slots[cursor].next) {
        WreathKVSlot *entry = &self->slots[cursor];
        PyObject *item;
        if (now >= entry->deadline) {
            continue;
        }
        if (what == 0) {
            item = Py_NewRef(entry->key);
        } else if (what == 1) {
            item = Py_NewRef(entry->value);
        } else {
            item = PyTuple_Pack(2, entry->key, entry->value);
        }
        if (item == NULL || PyList_Append(out, item) < 0) {
            Py_XDECREF(item);
            Py_DECREF(out);
            return NULL;
        }
        Py_DECREF(item);
    }
    return out;
}

static PyObject *
kv_view(WreathKV *self, PyObject *args, PyObject *kwargs, int what, const char *name)
{
    static char *keywords[] = {"now", NULL};
    PyObject *now_arg = NULL;
    double now;
    char format[16];

    PyOS_snprintf(format, sizeof(format), "|$O:%s", name);
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, format, keywords, &now_arg)) {
        return NULL;
    }
    if (kv_resolve_now(self, now_arg, &now) < 0) {
        return NULL;
    }
    return kv_collect(self, now, what);
}

PyDoc_STRVAR(kv_keys_doc,
"keys(*, now=None)\n--\n\nLive keys, most recently used first.");

static PyObject *
kv_keys(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    return kv_view(self, args, kwargs, 0, "keys");
}

PyDoc_STRVAR(kv_values_doc,
"values(*, now=None)\n--\n\nLive values, most recently used first.");

static PyObject *
kv_values(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    return kv_view(self, args, kwargs, 1, "values");
}

PyDoc_STRVAR(kv_items_doc,
"items(*, now=None)\n--\n\nLive (key, value) pairs, most recently used first.");

PyDoc_STRVAR(kv_snapshot_doc,
"snapshot(*, now=None)\n"
"--\n\n"
"The live entries as a plain dict.\n\n"
"A copy, and deliberately not a view: a caller counting or inspecting should\n"
"not have the order disturbed by the recency bookkeeping a `get` performs.");

static PyObject *
kv_snapshot(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    PyObject *pairs = kv_view(self, args, kwargs, 2, "snapshot");
    PyObject *out;
    if (pairs == NULL) {
        return NULL;
    }
    out = PyDict_New();
    if (out == NULL) {
        Py_DECREF(pairs);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(pairs); i++) {
        PyObject *pair = PyList_GET_ITEM(pairs, i);
        if (PyDict_SetItem(out, PyTuple_GET_ITEM(pair, 0),
                           PyTuple_GET_ITEM(pair, 1)) < 0) {
            Py_DECREF(pairs);
            Py_DECREF(out);
            return NULL;
        }
    }
    Py_DECREF(pairs);
    return out;
}

static PyObject *
kv_items(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    return kv_view(self, args, kwargs, 2, "items");
}

/* --- type plumbing ------------------------------------------------------- */

/* Live entries, not occupied slots. An expired entry is one this table refuses
 * to return, so counting it would make the answer a measure of debris rather
 * than of what is held -- the same correction `MemoryStore.__len__` had to make
 * in Python. */
static Py_ssize_t
kv_count_at(WreathKV *self, double now)
{
    Py_ssize_t live = 0;
    for (int32_t cursor = self->lru_head; cursor >= 0; cursor = self->slots[cursor].next) {
        if (now < self->slots[cursor].deadline) {
            live++;
        }
    }
    return live;
}

static Py_ssize_t
kv_length(WreathKV *self)
{
    double now;
    if (kv_now(self, &now) < 0) {
        /* `mp_length` cannot report an error, and a clock that raises is a
         * caller's bug rather than the table's. Answer against the C clock and
         * leave the exception for the next call that can propagate it. */
        PyErr_Clear();
        now = kv_clock();
    }
    return kv_count_at(self, now);
}

PyDoc_STRVAR(kv_count_doc,
"count(*, now=None)\n"
"--\n\n"
"Live entries as of `now`. `len(table)` is this against the real clock.\n\n"
"It takes a time because `len()` cannot, and a caller with an injected clock\n"
"needs to ask the question at *its* time: a store built on a test clock is\n"
"entirely expired as far as the real one is concerned, so `len()` on it\n"
"answers 0 for every entry. That is not hypothetical -- collapsing this into\n"
"`len()` alone silently broke `MemoryStore`'s count for exactly that reason.");

static PyObject *
kv_count(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"now", NULL};
    PyObject *now_arg = NULL;
    double now;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|$O:count", keywords, &now_arg)) {
        return NULL;
    }
    if (kv_resolve_now(self, now_arg, &now) < 0) {
        return NULL;
    }
    return PyLong_FromSsize_t(kv_count_at(self, now));
}

static int
kv_contains(WreathKV *self, PyObject *key)
{
    size_t slot;
    double now;
    int live;
    if (kv_now(self, &now) < 0) {
        return -1;
    }
    live = kv_lookup_live(self, key, now, &slot);
    if (live < 0) {
        return -1;
    }
    return live;
}

/* Allocation only; every argument is `tp_init`'s business.
 *
 * Splitting them is what makes this type subclass like an ordinary Python
 * class, and that is the point rather than a detail: `wreath.cache.BoundedCache`
 * and `wreath.store.MemoryStore` are both subclasses now, and a type whose
 * `tp_new` did the configuring forces every subclass to override `__new__` and
 * to never call `super().__init__` -- a rule nobody remembers and nothing
 * enforces. `wreath.queue.Queue` still carries exactly that constraint, with a
 * comment saying so.
 *
 * The table is left in a usable default state here so that a subclass which
 * forgets to chain `__init__` gets an empty 1024-entry table rather than a
 * NULL dereference. */
static PyObject *
kv_new(PyTypeObject *type, PyObject *Py_UNUSED(args), PyObject *Py_UNUSED(kwargs))
{
    WreathKV *self = (WreathKV *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->slot_count = WREATH_CTRL_GROUP;
    self->slot_mask = WREATH_CTRL_GROUP - 1;
    self->max_entries = 1024;
    self->max_slots = kv_next_power_of_two((1024 * 8 + 6) / 7);
    self->ttl = HUGE_VAL;
    self->lru_head = -1;
    self->lru_tail = -1;
    self->ctrl = PyMem_Malloc(self->slot_count);
    self->slots = PyMem_Calloc(self->slot_count, sizeof(WreathKVSlot));
    if (self->ctrl == NULL || self->slots == NULL) {
        Py_DECREF(self);
        PyErr_NoMemory();
        return NULL;
    }
    memset(self->ctrl, (int)WREATH_CTRL_EMPTY, self->slot_count);
    for (size_t i = 0; i < self->slot_count; i++) {
        self->slots[i].prev = -1;
        self->slots[i].next = -1;
    }
    return (PyObject *)self;
}

static int
kv_init(WreathKV *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"max_entries", "ttl", "max_bytes", "track_evictions",
                               "clock", NULL};
    Py_ssize_t max_entries = 1024;
    PyObject *ttl_arg = Py_None;
    Py_ssize_t max_bytes = 0;
    int track_evictions = 0;
    PyObject *clock = Py_None;
    double ttl = HUGE_VAL;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|nO$npO:KV", keywords, &max_entries,
                                     &ttl_arg, &max_bytes, &track_evictions, &clock)) {
        return -1;
    }
    if (max_entries < 1) {
        PyErr_SetString(PyExc_ValueError, "max_entries must be positive");
        return -1;
    }
    if (max_bytes < 0) {
        PyErr_SetString(PyExc_ValueError, "max_bytes must be positive or 0 for unbounded");
        return -1;
    }
    if (clock != Py_None && !PyCallable_Check(clock)) {
        PyErr_SetString(PyExc_TypeError, "clock must be callable or None");
        return -1;
    }
    if (ttl_arg != Py_None) {
        ttl = PyFloat_AsDouble(ttl_arg);
        if (ttl == -1.0 && PyErr_Occurred() != NULL) {
            return -1;
        }
        if (!(ttl > 0.0)) {
            PyErr_SetString(PyExc_ValueError, "ttl must be positive or None");
            return -1;
        }
    }
    /* Re-initialising an already-populated table starts it over rather than
     * leaving entries sized against the previous bounds. */
    if (self->used != 0 || self->tombstones != 0) {
        PyObject *discard = kv_clear(self, NULL);
        if (discard == NULL) {
            return -1;
        }
        Py_DECREF(discard);
    }
    /* Sized for the ceiling but allocated at the floor: a table declared to
     * hold a million keys and given four should cost four keys' worth of
     * memory, so it starts at one group and doubles. */
    self->max_slots = kv_next_power_of_two(((size_t)max_entries * 8 + 6) / 7);
    self->max_entries = (size_t)max_entries;
    self->max_bytes = (size_t)max_bytes;
    self->ttl = ttl;
    Py_XSETREF(self->clock, clock == Py_None ? NULL : Py_NewRef(clock));
    Py_CLEAR(self->evicted);
    if (track_evictions) {
        self->evicted = PyList_New(0);
        if (self->evicted == NULL) {
            return -1;
        }
    }
    return 0;
}

static int
kv_traverse(WreathKV *self, visitproc visit, void *arg)
{
    Py_VISIT(self->evicted);
    Py_VISIT(self->clock);
    if (self->slots == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->slot_count; i++) {
        Py_VISIT(self->slots[i].key);
        Py_VISIT(self->slots[i].value);
    }
    return 0;
}

static int
kv_tp_clear(WreathKV *self)
{
    Py_CLEAR(self->evicted);
    Py_CLEAR(self->clock);
    if (self->slots == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->slot_count; i++) {
        Py_CLEAR(self->slots[i].key);
        Py_CLEAR(self->slots[i].value);
        if (self->ctrl != NULL) {
            self->ctrl[i] = (uint8_t)WREATH_CTRL_EMPTY;
        }
    }
    self->used = 0;
    self->bytes = 0;
    self->tombstones = 0;
    self->lru_head = -1;
    self->lru_tail = -1;
    return 0;
}

static void
kv_dealloc(WreathKV *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    (void)kv_tp_clear(self);
    PyMem_Free(self->ctrl);
    PyMem_Free(self->slots);
    self->ctrl = NULL;
    self->slots = NULL;
    type->tp_free((PyObject *)self);
}

static PyObject *
kv_get_hits(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->hits);
}

static PyObject *
kv_get_misses(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->misses);
}

static PyObject *
kv_get_evictions(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->evictions);
}

static PyObject *
kv_get_expirations(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->expirations);
}

static PyObject *
kv_get_slots(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSize_t(self->slot_count);
}

static PyObject *
kv_get_max_entries(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSize_t(self->max_entries);
}

static PyObject *
kv_get_ttl(WreathKV *self, void *Py_UNUSED(closure))
{
    if (self->ttl == HUGE_VAL) {
        Py_RETURN_NONE;
    }
    return PyFloat_FromDouble(self->ttl);
}

static PyObject *
kv_get_bytes(WreathKV *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSize_t(self->bytes);
}

static PyObject *
kv_get_max_bytes(WreathKV *self, void *Py_UNUSED(closure))
{
    if (self->max_bytes == 0) {
        Py_RETURN_NONE;
    }
    return PyLong_FromSize_t(self->max_bytes);
}

static PyObject *
kv_get_clock(WreathKV *self, void *Py_UNUSED(closure))
{
    return Py_NewRef(self->clock == NULL ? Py_None : self->clock);
}

static PyGetSetDef kv_getset[] = {
    {"bytes", (getter)kv_get_bytes, NULL,
     "What the live entries say they retain, summed.", NULL},
    {"max_bytes", (getter)kv_get_max_bytes, NULL,
     "The byte ceiling, or None when only the entry count is bounded.", NULL},
    {"hits", (getter)kv_get_hits, NULL, "Reads that found a live entry.", NULL},
    {"misses", (getter)kv_get_misses, NULL, "Reads that found nothing live.", NULL},
    {"evictions", (getter)kv_get_evictions, NULL,
     "Entries dropped to stay inside max_entries.", NULL},
    {"expirations", (getter)kv_get_expirations, NULL,
     "Entries dropped because their deadline had passed.", NULL},
    {"slots", (getter)kv_get_slots, NULL, "Slots currently allocated.", NULL},
    {"max_entries", (getter)kv_get_max_entries, NULL, "The configured ceiling.", NULL},
    {"ttl", (getter)kv_get_ttl, NULL, "The default lifetime, or None.", NULL},
    {"clock", (getter)kv_get_clock, NULL,
     "The injected time source, or None for the built-in monotonic clock.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef kv_methods[] = {
    {"get", (PyCFunction)(void (*)(void))kv_get, METH_FASTCALL | METH_KEYWORDS,
     kv_get_doc},
    {"peek", (PyCFunction)(void (*)(void))kv_peek, METH_FASTCALL | METH_KEYWORDS,
     kv_peek_doc},
    {"set", (PyCFunction)(void (*)(void))kv_set, METH_FASTCALL | METH_KEYWORDS,
     kv_set_doc},
    {"claim", (PyCFunction)(void (*)(void))kv_claim, METH_FASTCALL | METH_KEYWORDS,
     kv_claim_doc},
    {"delete", (PyCFunction)kv_delete, METH_O, kv_delete_doc},
    {"pop", (PyCFunction)(void (*)(void))kv_pop, METH_VARARGS | METH_KEYWORDS, kv_pop_doc},
    {"touch", (PyCFunction)(void (*)(void))kv_touch, METH_VARARGS | METH_KEYWORDS,
     kv_touch_doc},
    {"purge", (PyCFunction)(void (*)(void))kv_purge, METH_VARARGS | METH_KEYWORDS,
     kv_purge_doc},
    {"count", (PyCFunction)(void (*)(void))kv_count, METH_VARARGS | METH_KEYWORDS,
     kv_count_doc},
    {"take_evicted", (PyCFunction)kv_take_evicted, METH_NOARGS, kv_take_evicted_doc},
    {"clear", (PyCFunction)kv_clear, METH_NOARGS, kv_clear_doc},
    {"keys", (PyCFunction)(void (*)(void))kv_keys, METH_VARARGS | METH_KEYWORDS,
     kv_keys_doc},
    {"values", (PyCFunction)(void (*)(void))kv_values, METH_VARARGS | METH_KEYWORDS,
     kv_values_doc},
    {"items", (PyCFunction)(void (*)(void))kv_items, METH_VARARGS | METH_KEYWORDS,
     kv_items_doc},
    {"snapshot", (PyCFunction)(void (*)(void))kv_snapshot, METH_VARARGS | METH_KEYWORDS,
     kv_snapshot_doc},
    {NULL, NULL, 0, NULL},
};

static PySequenceMethods kv_as_sequence = {
    .sq_contains = (objobjproc)kv_contains,
};

static PyMappingMethods kv_as_mapping = {
    .mp_length = (lenfunc)kv_length,
};

PyDoc_STRVAR(kv_doc,
"KV(max_entries=1024, ttl=None)\n"
"--\n\n"
"A bounded key/value table with LRU eviction and lazy TTL expiry.\n\n"
"Not synchronised: it is built for one event loop, the way the pure\n"
"`BoundedCache` it replaces was. Use `wreath.queue` to hand work between\n"
"threads.");

static PyTypeObject WreathKVType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.KV",
    .tp_basicsize = sizeof(WreathKV),
    .tp_dealloc = (destructor)kv_dealloc,
    .tp_as_sequence = &kv_as_sequence,
    .tp_as_mapping = &kv_as_mapping,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_BASETYPE,
    .tp_doc = kv_doc,
    .tp_traverse = (traverseproc)kv_traverse,
    .tp_clear = (inquiry)kv_tp_clear,
    .tp_methods = kv_methods,
    .tp_getset = kv_getset,
    .tp_init = (initproc)kv_init,
    .tp_new = kv_new,
    .tp_free = PyObject_GC_Del,
};

int
wreath_register_kv(PyObject *module)
{
    if (PyType_Ready(&WreathKVType) < 0) {
        return -1;
    }
    if (PyModule_AddObjectRef(module, "KV", (PyObject *)&WreathKVType) < 0) {
        return -1;
    }
    return 0;
}
