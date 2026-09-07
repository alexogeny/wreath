/* A bounded in-process queue: one ring, one lock, one drop policy.
 *
 * The ring is not what this is for. `collections.deque` is already C and
 * already fast -- a `deque(maxlen=n).append` measures 0.02us, and nothing here
 * beats that. What costs is everything wrapped *around* it: the two copies of
 * this class in the tree (`_logsink.BoundedLogQueue` and
 * `_otlp.BoundedExportQueue`, near-identical) each pay a lock acquire, two
 * counter increments, a length test and a method call per item, and measure
 * 0.17us -- eight times the ring underneath them. Collapsing that bookkeeping
 * into one C call is the whole point; the ring comes along because it has to
 * live somewhere.
 *
 * Loss is counted, never silent. Offering to a full queue returns False and
 * increments `dropped`, which is the only policy compatible with the promise a
 * bounded hand-off makes: bounded memory, bounded latency, and a number an
 * operator can look at. `drop_oldest=True` evicts the front instead, for a
 * consumer that would rather have the newest.
 *
 * Threads. Unlike `wreath.kv` this genuinely crosses them -- the flight
 * recorder's projector thread offers, a writer thread drains -- so the ring is
 * guarded by a `PyMutex`, held only across the pointer arithmetic and never
 * across a call back into Python.
 *
 * The awaitable half. `get()` returns an awaitable that resolves without
 * suspending when an item is already there, the same trick the native server
 * uses to satisfy an ASGI `receive()` without a Future (see `server_common.c`'s
 * note on why there is no `am_send`). Only an actually-empty queue builds a
 * Future, and that path is deliberately Python: parking a waiter, waking it
 * from another thread and surviving cancellation is delicate enough that it
 * belongs where it can be read and tested, not in C for a path that by
 * definition is about to block anyway. C keeps a `waiting` flag so the common
 * offer -- nobody waiting -- costs one integer test rather than a call.
 */
#include "wreathcore.h"

#include <stdint.h>

/* --- the non-suspending awaitable ---------------------------------------- */

typedef struct {
    PyObject_HEAD
    PyObject *value; /* owned; NULL once delivered */
} WreathQueueValue;

static PyObject *
queue_value_await(PyObject *self)
{
    return Py_NewRef(self);
}

/* Delivering through StopIteration is structural to the awaitable protocol
 * rather than a missed optimisation; `server_common.c` records the measurement
 * that established `am_send` is unreachable here. */
static PyObject *
queue_value_next(PyObject *op)
{
    WreathQueueValue *self = (WreathQueueValue *)op;
    PyObject *value = self->value;
    if (value == NULL) {
        PyErr_SetNone(PyExc_StopIteration);
        return NULL;
    }
    self->value = NULL;
    if (PyTuple_Check(value) || PyExceptionInstance_Check(value)) {
        /* Both are ambiguous for PyErr_SetObject, which would unpack a tuple
         * into constructor arguments and re-raise an exception instance as
         * itself. A queue carries whatever the caller put in it, so both are
         * ordinary payloads here and have to be wrapped explicitly. */
        PyObject *stop = PyObject_CallOneArg(PyExc_StopIteration, value);
        Py_DECREF(value);
        if (stop == NULL) {
            return NULL;
        }
        PyErr_SetObject(PyExc_StopIteration, stop);
        Py_DECREF(stop);
        return NULL;
    }
    PyErr_SetObject(PyExc_StopIteration, value);
    Py_DECREF(value);
    return NULL;
}

static void
queue_value_dealloc(PyObject *op)
{
    WreathQueueValue *self = (WreathQueueValue *)op;
    Py_XDECREF(self->value);
    Py_TYPE(op)->tp_free(op);
}

static PyAsyncMethods queue_value_async = {
    .am_await = queue_value_await,
};

static PyTypeObject WreathQueueValueType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core._QueueValue",
    .tp_basicsize = sizeof(WreathQueueValue),
    .tp_dealloc = queue_value_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "An awaitable that resolves to an already-available item.",
    .tp_as_async = &queue_value_async,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = queue_value_next,
};

/* Steals `value`. */
static PyObject *
queue_value_new(PyObject *value)
{
    WreathQueueValue *self = PyObject_New(WreathQueueValue, &WreathQueueValueType);
    if (self == NULL) {
        Py_DECREF(value);
        return NULL;
    }
    self->value = value;
    return (PyObject *)self;
}

/* --- the ring ------------------------------------------------------------ */

typedef struct {
    PyObject_HEAD
    PyObject **items; /* owned refs, capacity slots, head-relative */
    size_t capacity;
    size_t head;
    size_t count;
    PyMutex mutex;
    int drop_oldest;
    int lifo;
    int closed;
    /* Raised by the Python half while a getter is parked. The offer path tests
     * it instead of calling into Python on every item. */
    int waiting;
    uint64_t offered;
    uint64_t dropped;
} WreathQueue;

static PyObject *s_wake = NULL;      /* "_wake": an item arrived */
static PyObject *s_blocked = NULL;   /* "_blocked": build a waiting awaitable */
static PyObject *queue_empty = NULL; /* wreath._queue_protocol.QueueEmpty */
static PyObject *queue_full = NULL;  /* wreath._queue_protocol.QueueFull */

/* The two exception classes are **defined in Python and imported here**, rather
 * than minted with PyErr_NewException on each side. There is one of each, and
 * both this ring and `wreath.queue`'s `RoundRobin` raise it.
 *
 * Two classes named QueueEmpty is not a cosmetic difference: `wreath.queue`
 * re-exports one of them, and `except QueueEmpty` around a ring that raises the
 * other simply does not catch. The facade's own `_blocked` does exactly that,
 * so an arm mismatch turned a wait into an unhandled exception -- found by the
 * suite that runs the awaiting half over *both* rings, and invisible to any
 * test that only exercised the selected one.
 *
 * Resolved on first use rather than at module init. `_core` is imported while
 * `wreath/__init__.py` is still executing, so importing a `wreath.*` submodule
 * from the init function would re-enter a half-built package; by the time a
 * queue can raise, the package is up. One-time resolution into a static
 * follows `wreath_security_ready`, and the value never changes after it. */
static int
queue_exceptions_ready(void)
{
    PyObject *module;
    if (queue_empty != NULL && queue_full != NULL) {
        return 0;
    }
    module = PyImport_ImportModule("wreath._queue_protocol");
    if (module == NULL) {
        return -1;
    }
    queue_empty = PyObject_GetAttrString(module, "QueueEmpty");
    queue_full = PyObject_GetAttrString(module, "QueueFull");
    Py_DECREF(module);
    if (queue_empty == NULL || queue_full == NULL) {
        Py_CLEAR(queue_empty);
        Py_CLEAR(queue_full);
        return -1;
    }
    return 0;
}

/* Append with the ring already locked. Returns 1 when stored, 0 when refused,
 * -1 on allocation failure, and steals nothing. */
static int
queue_push_locked(WreathQueue *self, PyObject *item, PyObject **evicted)
{
    size_t index;
    *evicted = NULL;
    if (self->items == NULL) {
        self->items = PyMem_Calloc(self->capacity, sizeof(PyObject *));
        if (self->items == NULL) {
            PyErr_NoMemory();
            return -1;
        }
    }
    if (self->count == self->capacity) {
        if (!self->drop_oldest) {
            return 0;
        }
        /* Hand the displaced item back for the caller to release outside the
         * lock: a DECREF can run arbitrary __del__ code, and running that under
         * the ring's mutex is how a queue deadlocks against its own producer. */
        *evicted = self->items[self->head];
        self->items[self->head] = NULL;
        self->head = (self->head + 1) % self->capacity;
        self->count--;
    }
    index = (self->head + self->count) % self->capacity;
    self->items[index] = Py_NewRef(item);
    self->count++;
    return 1;
}

/* Take the next item under the queue's discipline: the oldest for a FIFO, the
 * newest for a LIFO. One ring serves both -- a stack is a queue that reads from
 * the end it writes to, and giving it its own type would duplicate the bound,
 * the counters and the lock to change one index. */
static PyObject *
queue_pop_locked(WreathQueue *self)
{
    PyObject *item;
    size_t index;
    if (self->count == 0) {
        return NULL;
    }
    if (self->lifo) {
        index = (self->head + self->count - 1) % self->capacity;
        self->count--;
    } else {
        index = self->head;
        self->head = (self->head + 1) % self->capacity;
        self->count--;
    }
    item = self->items[index];
    self->items[index] = NULL;
    return item; /* reference moves to the caller */
}

/* Tell the Python half a getter may now be satisfiable. Never called with the
 * mutex held. Returns -1 with an exception set. */
static int
queue_wake(PyObject *self)
{
    PyObject *result = PyObject_CallMethodNoArgs(self, s_wake);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

PyDoc_STRVAR(queue_offer_doc,
"offer(item)\n"
"--\n\n"
"Enqueue `item`, reporting whether it was kept.\n\n"
"A full queue with the default policy refuses the item and counts a drop; with\n"
"`drop_oldest=True` it evicts the front and keeps this one, which still counts\n"
"a drop because something was lost either way.");

static PyObject *
queue_offer(WreathQueue *self, PyObject *item)
{
    PyObject *evicted;
    int stored;
    int waiting;

    PyMutex_Lock(&self->mutex);
    if (self->closed) {
        PyMutex_Unlock(&self->mutex);
        PyErr_SetString(PyExc_RuntimeError, "queue is closed");
        return NULL;
    }
    stored = queue_push_locked(self, item, &evicted);
    if (stored < 0) {
        PyMutex_Unlock(&self->mutex);
        return NULL;
    }
    self->offered++;
    if (!stored || evicted != NULL) {
        self->dropped++;
    }
    waiting = self->waiting;
    PyMutex_Unlock(&self->mutex);

    Py_XDECREF(evicted);
    if (stored && waiting && queue_wake((PyObject *)self) < 0) {
        return NULL;
    }
    return PyBool_FromLong(stored);
}

PyDoc_STRVAR(queue_put_nowait_doc,
"put_nowait(item)\n"
"--\n\n"
"Enqueue `item`, raising `QueueFull` rather than dropping it.\n\n"
"For a producer that must not lose work and would rather be told. `offer` is\n"
"the one to use where the queue exists to bound memory.");

static PyObject *
queue_put_nowait(WreathQueue *self, PyObject *item)
{
    PyObject *evicted;
    int stored;
    int waiting;

    PyMutex_Lock(&self->mutex);
    if (self->closed) {
        PyMutex_Unlock(&self->mutex);
        PyErr_SetString(PyExc_RuntimeError, "queue is closed");
        return NULL;
    }
    if (self->count == self->capacity && !self->drop_oldest) {
        PyMutex_Unlock(&self->mutex);
        if (queue_exceptions_ready() < 0) {
            return NULL;
        }
        PyErr_SetString(queue_full, "queue is full");
        return NULL;
    }
    stored = queue_push_locked(self, item, &evicted);
    if (stored < 0) {
        PyMutex_Unlock(&self->mutex);
        return NULL;
    }
    self->offered++;
    if (evicted != NULL) {
        self->dropped++;
    }
    waiting = self->waiting;
    PyMutex_Unlock(&self->mutex);

    Py_XDECREF(evicted);
    if (stored && waiting && queue_wake((PyObject *)self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

PyDoc_STRVAR(queue_get_nowait_doc,
"get_nowait()\n"
"--\n\n"
"The oldest item, or `QueueEmpty` when there is none.");

static PyObject *
queue_get_nowait(WreathQueue *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *item;

    PyMutex_Lock(&self->mutex);
    item = queue_pop_locked(self);
    PyMutex_Unlock(&self->mutex);

    if (item == NULL) {
        if (queue_exceptions_ready() < 0) {
            return NULL;
        }
        PyErr_SetString(queue_empty, "queue is empty");
        return NULL;
    }
    return item;
}

PyDoc_STRVAR(queue_get_doc,
"get()\n"
"--\n\n"
"An awaitable for the oldest item, waiting for one if the queue is empty.\n\n"
"When an item is already there the awaitable resolves without suspending the\n"
"calling coroutine -- no Future is built and the event loop is not re-entered.\n"
"Only a genuinely empty queue parks a waiter.");

static PyObject *
queue_get(WreathQueue *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *item;

    PyMutex_Lock(&self->mutex);
    item = queue_pop_locked(self);
    PyMutex_Unlock(&self->mutex);

    if (item != NULL) {
        return queue_value_new(item); /* steals the reference */
    }
    /* Empty: hand off to the Python half, which owns the Future, the waiter
     * list and cancellation. It returns its own awaitable. */
    return PyObject_CallMethodNoArgs((PyObject *)self, s_blocked);
}

PyDoc_STRVAR(queue_peek_doc,
"peek(default=None)\n"
"--\n\n"
"The next item under this queue's discipline without removing it, or\n"
"`default` when the queue is empty.\n\n"
"The read that does not disturb what it is reading, which is what `peek`\n"
"means on `wreath.kv` and on `PriorityQueue` too.");

static const char *const queue_peek_names[] = {"default"};

static PyObject *
queue_peek(WreathQueue *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[1] = {Py_None};
    PyObject *item = NULL;

    if (wreath_bind_args(args, nargs, kwnames, queue_peek_names, slots, 1, 0, "peek") < 0) {
        return NULL;
    }
    PyMutex_Lock(&self->mutex);
    if (self->count > 0) {
        size_t offset = self->lifo ? self->count - 1 : 0;
        item = Py_NewRef(self->items[(self->head + offset) % self->capacity]);
    }
    PyMutex_Unlock(&self->mutex);
    return item != NULL ? item : Py_NewRef(slots[0]);
}

PyDoc_STRVAR(queue_drain_doc,
"drain(limit=None)\n"
"--\n\n"
"Remove and return up to `limit` items, oldest first, or everything when\n"
"`limit` is None.\n\n"
"One call rather than a pop per item: the list is built with the ring locked\n"
"once, which is what a drainer on its own thread wants.");

static PyObject *
queue_drain_upto(WreathQueue *self, Py_ssize_t limit)
{
    PyObject *out;
    Py_ssize_t taken;
    Py_ssize_t filled;

    /* Sized, then allocated, then filled -- and the allocation happens outside
     * the lock on purpose. `PyList_New` can trigger a GC pass, a GC pass runs
     * finalizers, and a finalizer that touches this queue would deadlock
     * against a mutex that is not reentrant. Reading `count` first can
     * over-size the list if a concurrent drain took items in between, so the
     * fill loop stops at whatever is actually there and the list is trimmed. */
    PyMutex_Lock(&self->mutex);
    taken = (Py_ssize_t)self->count;
    PyMutex_Unlock(&self->mutex);
    if (taken > limit) {
        taken = limit;
    }
    out = PyList_New(taken);
    if (out == NULL) {
        return NULL;
    }

    PyMutex_Lock(&self->mutex);
    for (filled = 0; filled < taken; filled++) {
        PyObject *item = queue_pop_locked(self);
        if (item == NULL) {
            break;
        }
        PyList_SET_ITEM(out, filled, item);
    }
    PyMutex_Unlock(&self->mutex);

    if (filled < taken && PyList_SetSlice(out, filled, taken, NULL) < 0) {
        Py_DECREF(out);
        return NULL;
    }
    return out;
}

static PyObject *
queue_drain(WreathQueue *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"limit", NULL};
    PyObject *limit_arg = Py_None;
    Py_ssize_t limit;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|O:drain", keywords, &limit_arg)) {
        return NULL;
    }
    if (limit_arg == Py_None) {
        limit = PY_SSIZE_T_MAX;
    } else {
        limit = PyNumber_AsSsize_t(limit_arg, PyExc_OverflowError);
        if (limit == -1 && PyErr_Occurred() != NULL) {
            return NULL;
        }
        if (limit < 0) {
            limit = 0;
        }
    }
    return queue_drain_upto(self, limit);
}

PyDoc_STRVAR(queue_snapshot_doc,
"snapshot()\n"
"--\n\n"
"The queued items, oldest first, **without** removing them.\n\n"
"For asking what a queue is holding -- a diagnostic, a test, an operator\n"
"wanting to see the backlog -- where `drain` would answer the question by\n"
"destroying it. The list is a copy taken under the ring's lock, so it is a\n"
"consistent moment rather than a view that shifts while it is read.");

static PyObject *
queue_snapshot(WreathQueue *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *out;
    Py_ssize_t held;
    Py_ssize_t filled;

    /* Sized, allocated, then filled -- and allocated outside the lock, for the
     * reason `queue_drain_upto` gives: a GC pass triggered by the allocation
     * runs finalizers, and a finalizer touching this queue would deadlock on a
     * mutex that is not reentrant. */
    PyMutex_Lock(&self->mutex);
    held = (Py_ssize_t)self->count;
    PyMutex_Unlock(&self->mutex);

    out = PyList_New(held);
    if (out == NULL) {
        return NULL;
    }

    PyMutex_Lock(&self->mutex);
    for (filled = 0; filled < held && filled < (Py_ssize_t)self->count; filled++) {
        /* In the order `get` would hand them back, which for a LIFO is the
         * reverse of the ring. A snapshot that disagreed with the discipline
         * would be a debugging aid that lies about what happens next. */
        size_t offset = self->lifo ? self->count - 1 - (size_t)filled : (size_t)filled;
        size_t index = (self->head + offset) % self->capacity;
        PyList_SET_ITEM(out, filled, Py_NewRef(self->items[index]));
    }
    PyMutex_Unlock(&self->mutex);

    if (filled < held && PyList_SetSlice(out, filled, held, NULL) < 0) {
        Py_DECREF(out);
        return NULL;
    }
    return out;
}

PyDoc_STRVAR(queue_clear_doc,
"clear()\n"
"--\n\nDrop every queued item, returning how many went.");

static PyObject *
queue_clear(WreathQueue *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *discarded = queue_drain_upto(self, PY_SSIZE_T_MAX);
    Py_ssize_t size;
    if (discarded == NULL) {
        return NULL;
    }
    size = PyList_GET_SIZE(discarded);
    /* The items go with it: this is the one place a drop is deliberate rather
     * than counted, because the caller asked for it by name. */
    Py_DECREF(discarded);
    return PyLong_FromSsize_t(size);
}

PyDoc_STRVAR(queue_close_doc,
"close()\n"
"--\n\n"
"Refuse further items. Draining what is already queued still works, so a\n"
"consumer can finish the backlog after the producer has stopped.");

static PyObject *
queue_close(WreathQueue *self, PyObject *Py_UNUSED(ignored))
{
    int waiting;
    PyMutex_Lock(&self->mutex);
    self->closed = 1;
    waiting = self->waiting;
    PyMutex_Unlock(&self->mutex);
    if (waiting && queue_wake((PyObject *)self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* --- type plumbing ------------------------------------------------------- */

static Py_ssize_t
queue_length(WreathQueue *self)
{
    Py_ssize_t size;
    PyMutex_Lock(&self->mutex);
    size = (Py_ssize_t)self->count;
    PyMutex_Unlock(&self->mutex);
    return size;
}

/* Declared here because `tp_init` reuses them to reset a re-initialised
 * instance, and both are defined with the rest of the type plumbing below. The
 * same use-before-declaration shape cost this tree an aarch64 build; see
 * `AGENTS.md`. */
static int queue_tp_clear(WreathQueue *self);

/* Allocation only; the arguments are `tp_init`'s business.
 *
 * Split for the same reason `kv.c` splits them, and to settle the same
 * inconsistency: a type that configures itself in `tp_new` forces every
 * subclass to override `__new__` and to never call `super().__init__`, which
 * is a rule nothing enforces and nobody remembers. `wreath.queue.Queue` is a
 * subclass, so it used to carry exactly that constraint while `wreath.kv.KV`
 * next door did not. One family, one construction protocol. */
static PyObject *
queue_new(PyTypeObject *type, PyObject *Py_UNUSED(args), PyObject *Py_UNUSED(kwargs))
{
    WreathQueue *self = (WreathQueue *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->capacity = 1;
    return (PyObject *)self;
}

static int
queue_init(WreathQueue *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"capacity", "drop_oldest", "lifo", NULL};
    Py_ssize_t capacity = 4096;
    int drop_oldest = 0;
    int lifo = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|n$pp:Queue", keywords, &capacity,
                                     &drop_oldest, &lifo)) {
        return -1;
    }
    if (capacity < 1) {
        PyErr_SetString(PyExc_ValueError, "capacity must be positive");
        return -1;
    }
    if ((size_t)capacity > (size_t)PY_SSIZE_T_MAX / sizeof(PyObject *)) {
        PyErr_NoMemory();
        return -1;
    }
    /* Re-initialising drops whatever was queued: the ring is being resized, and
     * carrying items across a capacity change would silently truncate. */
    (void)queue_tp_clear(self);
    PyMem_Free(self->items);
    self->items = NULL;
    self->capacity = (size_t)capacity;
    self->head = 0;
    self->count = 0;
    self->drop_oldest = drop_oldest;
    self->lifo = lifo;
    self->closed = 0;
    self->waiting = 0;
    self->offered = 0;
    self->dropped = 0;
    return 0;
}

static int
queue_traverse(WreathQueue *self, visitproc visit, void *arg)
{
    if (self->items == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->count; i++) {
        Py_VISIT(self->items[(self->head + i) % self->capacity]);
    }
    return 0;
}

static int
queue_tp_clear(WreathQueue *self)
{
    if (self->items == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->capacity; i++) {
        Py_CLEAR(self->items[i]);
    }
    self->head = 0;
    self->count = 0;
    return 0;
}

static void
queue_dealloc(WreathQueue *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    (void)queue_tp_clear(self);
    PyMem_Free(self->items);
    self->items = NULL;
    type->tp_free((PyObject *)self);
}

static PyObject *
queue_get_capacity(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSize_t(self->capacity);
}

static PyObject *
queue_get_offered(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->offered);
}

static PyObject *
queue_get_dropped(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->dropped);
}

static PyObject *
queue_get_closed(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->closed);
}

static PyObject *
queue_get_drop_oldest(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->drop_oldest);
}

static PyObject *
queue_get_lifo(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->lifo);
}

static PyObject *
queue_get_waiting(WreathQueue *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->waiting);
}

static int
queue_set_waiting(WreathQueue *self, PyObject *value, void *Py_UNUSED(closure))
{
    int truth;
    if (value == NULL) {
        PyErr_SetString(PyExc_AttributeError, "waiting cannot be deleted");
        return -1;
    }
    truth = PyObject_IsTrue(value);
    if (truth < 0) {
        return -1;
    }
    PyMutex_Lock(&self->mutex);
    self->waiting = truth;
    PyMutex_Unlock(&self->mutex);
    return 0;
}

static PyGetSetDef queue_getset[] = {
    {"capacity", (getter)queue_get_capacity, NULL, "Items held before dropping.", NULL},
    {"offered", (getter)queue_get_offered, NULL, "Items ever offered.", NULL},
    {"dropped", (getter)queue_get_dropped, NULL, "Items lost to a full queue.", NULL},
    {"closed", (getter)queue_get_closed, NULL, "Whether further items are refused.", NULL},
    {"drop_oldest", (getter)queue_get_drop_oldest, NULL,
     "Whether a full queue evicts the front rather than refusing.", NULL},
    {"lifo", (getter)queue_get_lifo, NULL,
     "Whether the newest item is taken first rather than the oldest.", NULL},
    {"waiting", (getter)queue_get_waiting, (setter)queue_set_waiting,
     "Whether a getter is parked; set by the Python half.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef queue_methods[] = {
    {"offer", (PyCFunction)queue_offer, METH_O, queue_offer_doc},
    {"put_nowait", (PyCFunction)queue_put_nowait, METH_O, queue_put_nowait_doc},
    {"get_nowait", (PyCFunction)queue_get_nowait, METH_NOARGS, queue_get_nowait_doc},
    {"get", (PyCFunction)queue_get, METH_NOARGS, queue_get_doc},
    {"drain", (PyCFunction)(void (*)(void))queue_drain, METH_VARARGS | METH_KEYWORDS,
     queue_drain_doc},
    {"peek", (PyCFunction)(void (*)(void))queue_peek, METH_FASTCALL | METH_KEYWORDS,
     queue_peek_doc},
    {"snapshot", (PyCFunction)queue_snapshot, METH_NOARGS, queue_snapshot_doc},
    {"clear", (PyCFunction)queue_clear, METH_NOARGS, queue_clear_doc},
    {"close", (PyCFunction)queue_close, METH_NOARGS, queue_close_doc},
    {NULL, NULL, 0, NULL},
};

static PySequenceMethods queue_as_sequence = {
    .sq_length = (lenfunc)queue_length,
};

PyDoc_STRVAR(queue_doc,
"Queue(capacity=4096, *, drop_oldest=False, lifo=False)\n"
"--\n\n"
"A bounded ring with counted loss, safe to offer to from any thread.\n\n"
"Subclassed by `wreath.queue.Queue`, which supplies the `_wake` the awaitable\n"
"half calls.");

static PyTypeObject WreathQueueType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.Queue",
    .tp_basicsize = sizeof(WreathQueue),
    .tp_dealloc = (destructor)queue_dealloc,
    .tp_as_sequence = &queue_as_sequence,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_BASETYPE,
    .tp_doc = queue_doc,
    .tp_traverse = (traverseproc)queue_traverse,
    .tp_clear = (inquiry)queue_tp_clear,
    .tp_methods = queue_methods,
    .tp_getset = queue_getset,
    .tp_init = (initproc)queue_init,
    .tp_new = queue_new,
    .tp_free = PyObject_GC_Del,
};


/* --- the priority heap ---------------------------------------------------- */
/*
 * A bounded binary min-heap: lowest priority number comes out first, and ties
 * come out in the order they went in.
 *
 * Stability is not decoration. Without it, two items at the same priority come
 * out in whatever order the sift happened to leave them, which makes a
 * priority queue non-deterministic for the *common* case -- most items share a
 * priority -- and makes a test that pins ordering flaky rather than wrong. One
 * monotonically increasing sequence number per push, compared after the
 * priority, costs eight bytes an entry and removes the whole class of problem.
 *
 * The payload is never compared. Ordering reads the priority and the sequence
 * only, so an item that does not define `<` is still queueable and a comparison
 * cannot run arbitrary Python in the middle of a sift -- which, under the
 * ring's mutex, is how this would deadlock.
 *
 * `drop_lowest` scans the leaves to find the worst entry, which is O(n/2) and
 * documented rather than hidden: a min-heap knows its best element in constant
 * time and its worst nowhere. That is the honest cost of "a high-priority item
 * should displace a queued low-priority one", and it is paid only when the
 * queue is already full.
 */

typedef struct {
    double priority;
    uint64_t sequence;
    PyObject *item; /* owned */
} WreathHeapEntry;

typedef struct {
    PyObject_HEAD
    WreathHeapEntry *entries;
    size_t capacity;
    size_t count;
    uint64_t sequence;
    PyMutex mutex;
    int drop_lowest;
    int closed;
    int waiting;
    uint64_t offered;
    uint64_t dropped;
} WreathHeap;

/* Strictly-before, with the sequence breaking a priority tie. */
static inline int
heap_before(const WreathHeapEntry *a, const WreathHeapEntry *b)
{
    if (a->priority < b->priority) {
        return 1;
    }
    if (a->priority > b->priority) {
        return 0;
    }
    return a->sequence < b->sequence;
}

static int
heap_entry_compare(const void *left_pointer, const void *right_pointer)
{
    const WreathHeapEntry *left = left_pointer;
    const WreathHeapEntry *right = right_pointer;
    if (left->priority < right->priority) return -1;
    if (left->priority > right->priority) return 1;
    if (left->sequence < right->sequence) return -1;
    return left->sequence != right->sequence;
}

static void
heap_sift_up(WreathHeap *self, size_t index)
{
    WreathHeapEntry moving = self->entries[index];
    while (index > 0) {
        size_t parent = (index - 1) / 2;
        if (!heap_before(&moving, &self->entries[parent])) {
            break;
        }
        self->entries[index] = self->entries[parent];
        index = parent;
    }
    self->entries[index] = moving;
}

static void
heap_sift_down(WreathHeap *self, size_t index)
{
    WreathHeapEntry moving = self->entries[index];
    for (;;) {
        size_t child = index * 2 + 1;
        if (child >= self->count) {
            break;
        }
        if (child + 1 < self->count
            && heap_before(&self->entries[child + 1], &self->entries[child])) {
            child++;
        }
        if (!heap_before(&self->entries[child], &moving)) {
            break;
        }
        self->entries[index] = self->entries[child];
        index = child;
    }
    self->entries[index] = moving;
}

/* The worst entry, which in a min-heap is always a leaf. Leaves start at
 * count/2, so this looks at half the entries and never at an interior node. */
static size_t
heap_worst(WreathHeap *self)
{
    size_t worst = self->count / 2;
    for (size_t i = worst + 1; i < self->count; i++) {
        if (heap_before(&self->entries[worst], &self->entries[i])) {
            worst = i;
        }
    }
    return worst;
}

/* Remove `index`, returning its item. The last entry takes its place and is
 * sifted in whichever direction it needs -- up as well as down, because the
 * hole can be anywhere when `drop_lowest` removes a leaf. */
static PyObject *
heap_remove_locked(WreathHeap *self, size_t index)
{
    PyObject *item = self->entries[index].item;
    self->count--;
    if (index != self->count) {
        self->entries[index] = self->entries[self->count];
        heap_sift_down(self, index);
        heap_sift_up(self, index);
    }
    self->entries[self->count].item = NULL;
    return item;
}

PyDoc_STRVAR(heap_offer_doc,
"offer(item, priority=0.0)\n"
"--\n\n"
"Enqueue `item` at `priority`, reporting whether it was kept.\n\n"
"Lower numbers come out first, and items at the same priority come out in the\n"
"order they went in. A full queue refuses and counts a drop; with\n"
"`drop_lowest=True` it displaces the worst queued item instead, and refuses\n"
"only when the new item is itself the worst.");

static const char *const heap_offer_names[] = {"item", "priority"};

static PyObject *
heap_offer(WreathHeap *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    PyObject *slots[2] = {NULL, NULL};
    PyObject *displaced = NULL;
    PyObject *item;
    double priority = 0.0;
    int stored = 0;
    int waiting;

    if (wreath_bind_args(args, nargs, kwnames, heap_offer_names, slots, 2, 1, "offer") < 0) {
        return NULL;
    }
    item = slots[0];
    if (slots[1] != NULL) {
        priority = PyFloat_AsDouble(slots[1]);
        if (priority == -1.0 && PyErr_Occurred() != NULL) {
            return NULL;
        }
        if (priority != priority) {
            PyErr_SetString(PyExc_ValueError,
                            "priority must be a number, not NaN: NaN compares false "
                            "against everything, which would leave the heap unordered");
            return NULL;
        }
    }

    PyMutex_Lock(&self->mutex);
    if (self->closed) {
        PyMutex_Unlock(&self->mutex);
        PyErr_SetString(PyExc_RuntimeError, "queue is closed");
        return NULL;
    }
    if (self->entries == NULL) {
        self->entries = PyMem_Calloc(self->capacity, sizeof(WreathHeapEntry));
        if (self->entries == NULL) {
            PyMutex_Unlock(&self->mutex);
            return PyErr_NoMemory();
        }
    }
    self->offered++;
    if (self->count == self->capacity) {
        self->dropped++;
        if (self->drop_lowest && self->count > 0) {
            size_t worst = heap_worst(self);
            WreathHeapEntry candidate = {priority, self->sequence, item};
            if (heap_before(&candidate, &self->entries[worst])) {
                displaced = heap_remove_locked(self, worst);
                stored = 1;
            }
        }
    } else {
        stored = 1;
    }
    if (stored) {
        self->entries[self->count].priority = priority;
        self->entries[self->count].sequence = self->sequence++;
        self->entries[self->count].item = Py_NewRef(item);
        self->count++;
        heap_sift_up(self, self->count - 1);
    }
    waiting = self->waiting;
    PyMutex_Unlock(&self->mutex);

    /* Released outside the lock: a __del__ can run arbitrary Python, and running
     * it under the heap's mutex is how this deadlocks against its own producer. */
    Py_XDECREF(displaced);
    if (stored && waiting && queue_wake((PyObject *)self) < 0) {
        return NULL;
    }
    return PyBool_FromLong(stored);
}

PyDoc_STRVAR(heap_put_nowait_doc,
"put_nowait(item, priority=0.0)\n"
"--\n\n"
"Enqueue `item`, raising `QueueFull` rather than dropping anything.\n\n"
"The other posture, and the one a priority queue often wants: losing an\n"
"urgent item silently is worse than being told the queue is full. Counts no\n"
"drop, because nothing was dropped.");

static PyObject *
heap_put_nowait(WreathHeap *self, PyObject *const *args, Py_ssize_t nargs,
                PyObject *kwnames)
{
    PyObject *kept;
    int full;

    PyMutex_Lock(&self->mutex);
    /* Under `drop_lowest` a full heap still accepts an item better than its
     * worst, so "full" here is only the case where nothing can be admitted;
     * `offer` below settles which. */
    full = self->count == self->capacity && !self->drop_lowest;
    PyMutex_Unlock(&self->mutex);
    if (full) {
        if (queue_exceptions_ready() < 0) {
            return NULL;
        }
        PyErr_SetString(queue_full, "queue is full");
        return NULL;
    }
    kept = heap_offer(self, args, nargs, kwnames);
    if (kept == NULL) {
        return NULL;
    }
    if (kept == Py_False) {
        Py_DECREF(kept);
        if (queue_exceptions_ready() < 0) {
            return NULL;
        }
        PyErr_SetString(queue_full, "queue is full");
        return NULL;
    }
    Py_DECREF(kept);
    Py_RETURN_NONE;
}

PyDoc_STRVAR(heap_get_nowait_doc,
"get_nowait()\n--\n\nThe best item, or `QueueEmpty` when there is none.");

static PyObject *
heap_get_nowait(WreathHeap *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *item = NULL;

    PyMutex_Lock(&self->mutex);
    if (self->count > 0) {
        item = heap_remove_locked(self, 0);
    }
    PyMutex_Unlock(&self->mutex);

    if (item == NULL) {
        if (queue_exceptions_ready() < 0) {
            return NULL;
        }
        PyErr_SetString(queue_empty, "queue is empty");
        return NULL;
    }
    return item;
}

PyDoc_STRVAR(heap_get_doc,
"get()\n"
"--\n\n"
"An awaitable for the best item, waiting for one if the queue is empty.\n\n"
"Resolves without suspending when an item is already there, exactly as\n"
"`Queue.get` does.");

static PyObject *
heap_get(WreathHeap *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *item = NULL;

    PyMutex_Lock(&self->mutex);
    if (self->count > 0) {
        item = heap_remove_locked(self, 0);
    }
    PyMutex_Unlock(&self->mutex);

    if (item != NULL) {
        return queue_value_new(item);
    }
    return PyObject_CallMethodNoArgs((PyObject *)self, s_blocked);
}

PyDoc_STRVAR(heap_peek_doc,
"peek(default=None)\n"
"--\n\n"
"The best item without removing it, or `default` when the queue is empty.");

static PyObject *
heap_peek(WreathHeap *self, PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    static const char *const names[] = {"default"};
    PyObject *slots[1] = {Py_None};
    PyObject *item;

    if (wreath_bind_args(args, nargs, kwnames, names, slots, 1, 0, "peek") < 0) {
        return NULL;
    }
    PyMutex_Lock(&self->mutex);
    item = self->count > 0 ? Py_NewRef(self->entries[0].item) : NULL;
    PyMutex_Unlock(&self->mutex);
    return item != NULL ? item : Py_NewRef(slots[0]);
}

/* Take up to `limit` items in priority order. Repeated extraction rather than a
 * sort of the backing array: the array is heap-ordered, not sorted, so reading
 * it in place would hand back an order no caller asked for. */
static PyObject *
heap_drain_upto(WreathHeap *self, Py_ssize_t limit)
{
    PyObject *out;
    Py_ssize_t held;
    Py_ssize_t filled;

    PyMutex_Lock(&self->mutex);
    held = (Py_ssize_t)self->count;
    PyMutex_Unlock(&self->mutex);
    if (held > limit) {
        held = limit;
    }
    out = PyList_New(held);
    if (out == NULL) {
        return NULL;
    }

    PyMutex_Lock(&self->mutex);
    for (filled = 0; filled < held && self->count > 0; filled++) {
        PyList_SET_ITEM(out, filled, heap_remove_locked(self, 0));
    }
    PyMutex_Unlock(&self->mutex);

    if (filled < held && PyList_SetSlice(out, filled, held, NULL) < 0) {
        Py_DECREF(out);
        return NULL;
    }
    return out;
}

PyDoc_STRVAR(heap_drain_doc,
"drain(limit=None)\n"
"--\n\nRemove and return up to `limit` items, best first.");

static PyObject *
heap_drain(WreathHeap *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"limit", NULL};
    PyObject *limit_arg = Py_None;
    Py_ssize_t limit;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|O:drain", keywords, &limit_arg)) {
        return NULL;
    }
    if (limit_arg == Py_None) {
        limit = PY_SSIZE_T_MAX;
    } else {
        limit = PyNumber_AsSsize_t(limit_arg, PyExc_OverflowError);
        if (limit == -1 && PyErr_Occurred() != NULL) {
            return NULL;
        }
        if (limit < 0) {
            limit = 0;
        }
    }
    return heap_drain_upto(self, limit);
}

PyDoc_STRVAR(heap_snapshot_doc,
"snapshot()\n"
"--\n\n"
"The queued `(priority, item)` pairs in the order `get` would return them,\n"
"**without** removing them.\n\n"
"Built by copying the heap and draining the copy, so it costs a sort rather\n"
"than a walk -- the backing array is heap-ordered, and reading it directly\n"
"would report an order no caller will ever see.");

static PyObject *
heap_snapshot(WreathHeap *self, PyObject *Py_UNUSED(ignored))
{
    WreathHeapEntry *copy;
    PyObject *out;
    size_t held;
    size_t taken = 0;

    PyMutex_Lock(&self->mutex);
    held = self->count;
    copy = held > 0 ? PyMem_Malloc(held * sizeof(WreathHeapEntry)) : NULL;
    if (held > 0 && copy == NULL) {
        PyMutex_Unlock(&self->mutex);
        PyErr_NoMemory();
        return NULL;
    }
    for (size_t i = 0; i < held; i++) {
        copy[i] = self->entries[i];
        Py_INCREF(copy[i].item);
    }
    PyMutex_Unlock(&self->mutex);

    out = PyList_New((Py_ssize_t)held);
    if (out == NULL) {
        for (size_t i = 0; i < held; i++) {
            Py_DECREF(copy[i].item);
        }
        PyMem_Free(copy);
        return NULL;
    }
    /* Sequence is the stable tie-breaker, so qsort's own stability is
     * irrelevant and the copied heap reaches get order in O(n log n).  The
     * former repeated minimum selection scanned n + (n-1) + ... entries. */
    qsort(copy, held, sizeof(*copy), heap_entry_compare);
    while (taken < held) {
        PyObject *pair;
        pair = Py_BuildValue("(dO)", copy[taken].priority, copy[taken].item);
        if (pair == NULL) {
            for (size_t i = taken; i < held; i++) {
                Py_DECREF(copy[i].item);
            }
            PyMem_Free(copy);
            Py_DECREF(out);
            return NULL;
        }
        PyList_SET_ITEM(out, (Py_ssize_t)taken, pair);
        Py_DECREF(copy[taken].item);
        taken++;
    }
    PyMem_Free(copy);
    return out;
}

PyDoc_STRVAR(heap_clear_doc,
"clear()\n--\n\nDrop every queued item, returning how many went.");

static PyObject *
heap_clear(WreathHeap *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *discarded = heap_drain_upto(self, PY_SSIZE_T_MAX);
    Py_ssize_t size;
    if (discarded == NULL) {
        return NULL;
    }
    size = PyList_GET_SIZE(discarded);
    Py_DECREF(discarded);
    return PyLong_FromSsize_t(size);
}

PyDoc_STRVAR(heap_close_doc,
"close()\n--\n\nRefuse further items; draining the backlog still works.");

static PyObject *
heap_close(WreathHeap *self, PyObject *Py_UNUSED(ignored))
{
    int waiting;
    PyMutex_Lock(&self->mutex);
    self->closed = 1;
    waiting = self->waiting;
    PyMutex_Unlock(&self->mutex);
    if (waiting && queue_wake((PyObject *)self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static Py_ssize_t
heap_length(WreathHeap *self)
{
    Py_ssize_t size;
    PyMutex_Lock(&self->mutex);
    size = (Py_ssize_t)self->count;
    PyMutex_Unlock(&self->mutex);
    return size;
}

/* Same reason as `queue_tp_clear` above; `WreathHeap` is not declared until
 * this section, so the declaration lives here rather than beside that one. */
static int heap_tp_clear(WreathHeap *self);

static PyObject *
heap_new(PyTypeObject *type, PyObject *Py_UNUSED(args), PyObject *Py_UNUSED(kwargs))
{
    WreathHeap *self = (WreathHeap *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->capacity = 1;
    return (PyObject *)self;
}

static int
heap_init(WreathHeap *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"capacity", "drop_lowest", NULL};
    Py_ssize_t capacity = 4096;
    int drop_lowest = 0;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|n$p:PriorityQueue", keywords,
                                     &capacity, &drop_lowest)) {
        return -1;
    }
    if (capacity < 1) {
        PyErr_SetString(PyExc_ValueError, "capacity must be positive");
        return -1;
    }
    if ((size_t)capacity > (size_t)PY_SSIZE_T_MAX / sizeof(WreathHeapEntry)) {
        PyErr_NoMemory();
        return -1;
    }
    (void)heap_tp_clear(self);
    PyMem_Free(self->entries);
    self->entries = NULL;
    self->capacity = (size_t)capacity;
    self->count = 0;
    self->sequence = 0;
    self->drop_lowest = drop_lowest;
    self->closed = 0;
    self->waiting = 0;
    self->offered = 0;
    self->dropped = 0;
    return 0;
}

static int
heap_traverse(WreathHeap *self, visitproc visit, void *arg)
{
    if (self->entries == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->count; i++) {
        Py_VISIT(self->entries[i].item);
    }
    return 0;
}

static int
heap_tp_clear(WreathHeap *self)
{
    if (self->entries == NULL) {
        return 0;
    }
    for (size_t i = 0; i < self->capacity; i++) {
        Py_CLEAR(self->entries[i].item);
    }
    self->count = 0;
    return 0;
}

static void
heap_dealloc(WreathHeap *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    (void)heap_tp_clear(self);
    PyMem_Free(self->entries);
    self->entries = NULL;
    type->tp_free((PyObject *)self);
}

static PyObject *
heap_get_capacity(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSize_t(self->capacity);
}

static PyObject *
heap_get_offered(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->offered);
}

static PyObject *
heap_get_dropped(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->dropped);
}

static PyObject *
heap_get_closed(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->closed);
}

static PyObject *
heap_get_drop_lowest(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->drop_lowest);
}

static PyObject *
heap_get_waiting(WreathHeap *self, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(self->waiting);
}

static int
heap_set_waiting(WreathHeap *self, PyObject *value, void *Py_UNUSED(closure))
{
    int truth;
    if (value == NULL) {
        PyErr_SetString(PyExc_AttributeError, "waiting cannot be deleted");
        return -1;
    }
    truth = PyObject_IsTrue(value);
    if (truth < 0) {
        return -1;
    }
    PyMutex_Lock(&self->mutex);
    self->waiting = truth;
    PyMutex_Unlock(&self->mutex);
    return 0;
}

static PyGetSetDef heap_getset[] = {
    {"capacity", (getter)heap_get_capacity, NULL, "Items held before dropping.", NULL},
    {"offered", (getter)heap_get_offered, NULL, "Items ever offered.", NULL},
    {"dropped", (getter)heap_get_dropped, NULL, "Items lost to a full queue.", NULL},
    {"closed", (getter)heap_get_closed, NULL, "Whether further items are refused.", NULL},
    {"drop_lowest", (getter)heap_get_drop_lowest, NULL,
     "Whether a full queue displaces its worst item rather than refusing.", NULL},
    {"waiting", (getter)heap_get_waiting, (setter)heap_set_waiting,
     "Whether a getter is parked; set by the Python half.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef heap_methods[] = {
    {"offer", (PyCFunction)(void (*)(void))heap_offer, METH_FASTCALL | METH_KEYWORDS,
     heap_offer_doc},
    {"put_nowait", (PyCFunction)(void (*)(void))heap_put_nowait,
     METH_FASTCALL | METH_KEYWORDS, heap_put_nowait_doc},
    {"get_nowait", (PyCFunction)heap_get_nowait, METH_NOARGS, heap_get_nowait_doc},
    {"get", (PyCFunction)heap_get, METH_NOARGS, heap_get_doc},
    {"peek", (PyCFunction)(void (*)(void))heap_peek, METH_FASTCALL | METH_KEYWORDS,
     heap_peek_doc},
    {"drain", (PyCFunction)(void (*)(void))heap_drain, METH_VARARGS | METH_KEYWORDS,
     heap_drain_doc},
    {"snapshot", (PyCFunction)heap_snapshot, METH_NOARGS, heap_snapshot_doc},
    {"clear", (PyCFunction)heap_clear, METH_NOARGS, heap_clear_doc},
    {"close", (PyCFunction)heap_close, METH_NOARGS, heap_close_doc},
    {NULL, NULL, 0, NULL},
};

static PySequenceMethods heap_as_sequence = {
    .sq_length = (lenfunc)heap_length,
};

PyDoc_STRVAR(heap_doc,
"PriorityQueue(capacity=4096, *, drop_lowest=False)\n"
"--\n\n"
"A bounded priority queue: lowest number first, insertion order within a\n"
"priority, counted loss when full.");

static PyTypeObject WreathHeapType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.PriorityQueue",
    .tp_basicsize = sizeof(WreathHeap),
    .tp_dealloc = (destructor)heap_dealloc,
    .tp_as_sequence = &heap_as_sequence,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_BASETYPE,
    .tp_doc = heap_doc,
    .tp_traverse = (traverseproc)heap_traverse,
    .tp_clear = (inquiry)heap_tp_clear,
    .tp_methods = heap_methods,
    .tp_getset = heap_getset,
    .tp_init = (initproc)heap_init,
    .tp_new = heap_new,
    .tp_free = PyObject_GC_Del,
};

int
wreath_register_queue(PyObject *module)
{
    if (s_wake == NULL) {
        s_wake = PyUnicode_InternFromString("_wake");
        if (s_wake == NULL) {
            return -1;
        }
    }
    if (s_blocked == NULL) {
        s_blocked = PyUnicode_InternFromString("_blocked");
        if (s_blocked == NULL) {
            return -1;
        }
    }
    if (PyType_Ready(&WreathQueueValueType) < 0 || PyType_Ready(&WreathQueueType) < 0) {
        return -1;
    }
    if (PyType_Ready(&WreathHeapType) < 0) {
        return -1;
    }
    if (PyModule_AddObjectRef(module, "Queue", (PyObject *)&WreathQueueType) < 0
        || PyModule_AddObjectRef(module, "PriorityQueue", (PyObject *)&WreathHeapType) < 0) {
        return -1;
    }
    return 0;
}
