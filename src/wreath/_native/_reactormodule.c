/* wreath._native._reactor — native primitives for the reactor event loop.
 *
 * Stage-0 component: a hashed timing wheel for per-connection deadlines.
 *
 * asyncio keeps timers in a binary heap: O(log n) insert and cancel, and it
 * pays a cancellation-compaction pass. A server at high RPS churns two timers
 * per request (keep-alive + request deadline), almost always cancelling them
 * before they fire. A hashed timing wheel makes insert and cancel O(1) with a
 * fixed, tiny memory footprint (one slot array + intrusive nodes), which is
 * exactly the shape of that workload.
 *
 * Design: `slots` buckets at `resolution` seconds each. A timer due in `d`
 * ticks lands in bucket `(cur + d) % slots` carrying `rounds = d / slots`; each
 * time the cursor sweeps a bucket, a node with rounds>0 is decremented,
 * otherwise it fires. Insert/cancel are pointer splices on an intrusive doubly
 * linked list — no reallocation, no heapify, no compaction.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <descrobject.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/epoll.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <linux/io_uring.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#include "server.h"

static const WreathHttp1CAPI *g_http1_capi = NULL;

static void
load_http1_capi(void)
{
    if (g_http1_capi != NULL) {
        return;
    }
    g_http1_capi = PyCapsule_Import(WREATH_HTTP1_CAPI_NAME, 0);
    if (g_http1_capi == NULL) {
        PyErr_Clear();
    }
}

typedef struct WheelTimer {
    PyObject_HEAD
    PyObject *callback;          /* owned; NULL once fired/cancelled */
    PyObject *args;             /* owned tuple, or NULL (loop-timer dispatch) */
    PyObject *context;          /* owned contextvars.Context, or NULL */
    int64_t rounds;             /* full wheel rotations remaining */
    int64_t deadline;           /* absolute wheel tick */
    int slot;                   /* bucket index, or -1 if unlinked */
    struct WheelTimer *prev;    /* intrusive slot list */
    struct WheelTimer *next;
    struct TimingWheel *wheel;  /* borrowed */
} WheelTimer;

typedef struct TimingWheel {
    PyObject_HEAD
    WheelTimer **slots;         /* array of `nslots` bucket heads */
    int nslots;
    double resolution;          /* seconds per tick */
    double base;                /* loop-clock value at construction */
    int64_t cursor;             /* ticks swept so far */
    int64_t next_deadline;      /* earliest absolute tick, INT64_MAX if empty */
    int next_dirty;             /* earliest timer was removed; rescan lazily */
    Py_ssize_t count;           /* live timers */
} TimingWheel;

static PyTypeObject WheelTimerType;
static PyTypeObject TimingWheelType;

static void
wheel_refresh_next(TimingWheel *w)
{
    int64_t next = INT64_MAX;
    for (int slot = 0; slot < w->nslots; slot++) {
        for (WheelTimer *t = w->slots[slot]; t != NULL; t = t->next) {
            if (t->callback != NULL && t->deadline < next) {
                next = t->deadline;
            }
        }
    }
    w->next_deadline = next;
    w->next_dirty = 0;
}

static double
wheel_next_when(TimingWheel *w)
{
    if (w->next_dirty) {
        wheel_refresh_next(w);
    }
    if (w->next_deadline == INT64_MAX) {
        return Py_HUGE_VAL;
    }
    return w->base + (double)w->next_deadline * w->resolution;
}

/* --- WheelTimer ---------------------------------------------------------- */

static void
timer_unlink(WheelTimer *t)
{
    if (t->slot < 0) {
        return;  /* already out of the wheel */
    }
    if (t->prev != NULL) {
        t->prev->next = t->next;
    } else {
        t->wheel->slots[t->slot] = t->next;  /* was the head */
    }
    if (t->next != NULL) {
        t->next->prev = t->prev;
    }
    TimingWheel *w = t->wheel;
    if (t->deadline == w->next_deadline) {
        w->next_dirty = 1;
    }
    t->prev = t->next = NULL;
    t->slot = -1;
    w->count--;
}

static PyObject *
timer_cancel(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WheelTimer *t = (WheelTimer *)op;
    if (t->callback == NULL) {
        Py_RETURN_FALSE;  /* already fired or cancelled */
    }
    timer_unlink(t);
    Py_CLEAR(t->callback);
    /* Drop the wheel's owning reference (parked when scheduled). The caller's
     * handle keeps `t` alive for the duration of this call. */
    Py_DECREF(t);
    Py_RETURN_TRUE;
}

static PyObject *
timer_cancelled(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(((WheelTimer *)op)->callback == NULL);
}

static PyMethodDef timer_methods[] = {
    {"cancel", timer_cancel, METH_NOARGS,
     "Cancel the timer; returns True if it had not yet fired."},
    {"cancelled", timer_cancelled, METH_NOARGS,
     "True if the timer has fired or been cancelled."},
    {NULL, NULL, 0, NULL},
};

static void
timer_dealloc(PyObject *op)
{
    WheelTimer *t = (WheelTimer *)op;
    /* A live node must have been unlinked before its last ref drops; if not
     * (defensive), unlink now so the slot list never dangles. */
    if (t->slot >= 0) {
        timer_unlink(t);
    }
    Py_CLEAR(t->callback);
    Py_CLEAR(t->args);
    Py_CLEAR(t->context);
    Py_TYPE(op)->tp_free(op);
}

static PyTypeObject WheelTimerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.WheelTimer",
    .tp_basicsize = sizeof(WheelTimer),
    .tp_dealloc = timer_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = timer_methods,
};

/* --- TimingWheel --------------------------------------------------------- */

static int
wheel_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    TimingWheel *w = (TimingWheel *)op;
    double resolution = 0.001;   /* 1 ms */
    int nslots = 512;
    double base = 0.0;
    static char *kwlist[] = {"resolution", "slots", "base", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|did", kwlist,
                                     &resolution, &nslots, &base)) {
        return -1;
    }
    if (resolution <= 0.0 || nslots <= 0) {
        PyErr_SetString(PyExc_ValueError, "resolution and slots must be positive");
        return -1;
    }
    w->slots = PyMem_Calloc((size_t)nslots, sizeof(WheelTimer *));
    if (w->slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    w->nslots = nslots;
    w->resolution = resolution;
    w->base = base;
    w->cursor = 0;
    w->next_deadline = INT64_MAX;
    w->next_dirty = 0;
    w->count = 0;
    return 0;
}

static void
wheel_dealloc(PyObject *op)
{
    TimingWheel *w = (TimingWheel *)op;
    if (w->slots != NULL) {
        for (int i = 0; i < w->nslots; i++) {
            WheelTimer *t = w->slots[i];
            while (t != NULL) {
                WheelTimer *nxt = t->next;
                t->slot = -1;
                t->prev = t->next = NULL;
                Py_CLEAR(t->callback);
                Py_DECREF(t);  /* drop the wheel's reference */
                t = nxt;
            }
        }
        PyMem_Free(w->slots);
    }
    Py_TYPE(op)->tp_free(op);
}

/* schedule(delay_seconds, callback) -> WheelTimer */
static PyObject *
wheel_schedule(PyObject *op, PyObject *args)
{
    TimingWheel *w = (TimingWheel *)op;
    double delay;
    PyObject *callback;
    if (!PyArg_ParseTuple(args, "dO", &delay, &callback)) {
        return NULL;
    }
    if (delay < 0.0) {
        delay = 0.0;
    }
    int64_t ticks = (int64_t)(delay / w->resolution);
    if (ticks < 1) {
        ticks = 1;  /* never fire in the bucket currently being swept */
    }
    int64_t deadline = w->cursor + ticks;
    int slot = (int)(deadline % w->nslots);
    int64_t rounds = (ticks - 1) / w->nslots;  /* fire on first arrival at the slot */

    WheelTimer *t = PyObject_New(WheelTimer, &WheelTimerType);
    if (t == NULL) {
        return NULL;
    }
    t->callback = Py_NewRef(callback);
    t->args = NULL;
    t->context = NULL;
    t->rounds = rounds;
    t->deadline = deadline;
    t->slot = slot;
    t->wheel = w;
    t->prev = NULL;
    t->next = w->slots[slot];
    if (t->next != NULL) {
        t->next->prev = t;
    }
    w->slots[slot] = t;
    w->count++;
    if (deadline < w->next_deadline) {
        w->next_deadline = deadline;
        w->next_dirty = 0;
    }
    return Py_NewRef((PyObject *)t);  /* caller holds a ref; wheel holds one */
}

/* schedule_call(delay, callback, args, context) -> WheelTimer.
 *
 * The loop-timer entry point: the timer carries the callback, its argument
 * tuple, and a contextvars.Context, so expiry runs `context.run(callback, *args)`
 * entirely in C -- no per-timer Python TimerHandle and no per-tick Python
 * dispatch loop between the wheel and the event loop. */
static PyObject *
wheel_schedule_call(PyObject *op, PyObject *const *fastargs, Py_ssize_t nargs)
{
    TimingWheel *w = (TimingWheel *)op;
    if (nargs != 4) {
        PyErr_SetString(PyExc_TypeError,
                        "schedule_call(delay, callback, args, context)");
        return NULL;
    }
    double delay = PyFloat_AsDouble(fastargs[0]);
    if (delay == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    if (delay < 0.0) {
        delay = 0.0;
    }
    int64_t ticks = (int64_t)(delay / w->resolution);
    if (ticks < 1) {
        ticks = 1;
    }
    int64_t deadline = w->cursor + ticks;
    int slot = (int)(deadline % w->nslots);
    WheelTimer *t = PyObject_New(WheelTimer, &WheelTimerType);
    if (t == NULL) {
        return NULL;
    }
    t->callback = Py_NewRef(fastargs[1]);
    t->args = Py_NewRef(fastargs[2]);
    /* context=None means "no isolation": dispatch calls the callback directly,
     * skipping context.run and the per-timer copy_context the caller would pay. */
    t->context = (fastargs[3] == Py_None) ? NULL : Py_NewRef(fastargs[3]);
    t->rounds = (ticks - 1) / w->nslots;  /* fire on first arrival at the slot */
    t->deadline = deadline;
    t->slot = slot;
    t->wheel = w;
    t->prev = NULL;
    t->next = w->slots[slot];
    if (t->next != NULL) {
        t->next->prev = t;
    }
    w->slots[slot] = t;
    w->count++;
    if (deadline < w->next_deadline) {
        w->next_deadline = deadline;
        w->next_dirty = 0;
    }
    return Py_NewRef((PyObject *)t);
}

#define WHEEL_CATCHUP_ROTATIONS 4

static int
wheel_deadline_compare(const void *left, const void *right)
{
    const WheelTimer *a = *(WheelTimer *const *)left;
    const WheelTimer *b = *(WheelTimer *const *)right;
    return (a->deadline > b->deadline) - (a->deadline < b->deadline);
}

/* A normal poll advances zero or one ticks. After a machine suspend or process
 * stop, tick-by-tick catch-up turns elapsed wall time into a latency spike. The
 * rare large-jump path scans live nodes once and keeps callback order by sorting
 * their absolute deadlines. It does not mutate the wheel until all allocation
 * that can fail has succeeded. */
static int
wheel_collect_overdue(TimingWheel *w, int64_t target,
                      WheelTimer ***out, Py_ssize_t *out_count)
{
    *out = NULL;
    *out_count = 0;
    if (w->count == 0) {
        return 0;
    }
    WheelTimer **nodes = PyMem_Malloc((size_t)w->count * sizeof(*nodes));
    if (nodes == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t count = 0;
    for (int slot = 0; slot < w->nslots; slot++) {
        for (WheelTimer *t = w->slots[slot]; t != NULL; t = t->next) {
            if (t->deadline <= target) {
                nodes[count++] = t;
            }
        }
    }
    qsort(nodes, (size_t)count, sizeof(*nodes), wheel_deadline_compare);
    *out = nodes;
    *out_count = count;
    return 0;
}

static void
wheel_finish_jump(TimingWheel *w, int64_t target)
{
    w->cursor = target;
    for (int slot = 0; slot < w->nslots; slot++) {
        for (WheelTimer *t = w->slots[slot]; t != NULL; t = t->next) {
            int64_t remaining = t->deadline - target;
            t->rounds = (remaining - 1) / w->nslots;
        }
    }
}

/* advance(now_seconds) -> list of callbacks now due (removed from the wheel) */
static PyObject *
wheel_advance(PyObject *op, PyObject *arg)
{
    TimingWheel *w = (TimingWheel *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    int64_t target = (int64_t)((now - w->base) / w->resolution);
    PyObject *due = PyList_New(0);
    if (due == NULL) {
        return NULL;
    }
    if (target - w->cursor >= (int64_t)w->nslots * WHEEL_CATCHUP_ROTATIONS) {
        WheelTimer **nodes;
        Py_ssize_t count;
        if (wheel_collect_overdue(w, target, &nodes, &count) < 0) {
            Py_DECREF(due);
            return NULL;
        }
        Py_DECREF(due);
        due = PyList_New(count);
        if (due == NULL) {
            PyMem_Free(nodes);
            return NULL;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            WheelTimer *t = nodes[i];
            PyList_SET_ITEM(due, i, Py_NewRef(t->callback));
            timer_unlink(t);
            Py_CLEAR(t->callback);
            Py_DECREF(t);
        }
        PyMem_Free(nodes);
        wheel_finish_jump(w, target);
        if (w->next_dirty) {
            wheel_refresh_next(w);
        }
        return due;
    }
    while (w->cursor < target) {
        w->cursor++;
        int slot = (int)(w->cursor % w->nslots);
        WheelTimer *t = w->slots[slot];
        while (t != NULL) {
            WheelTimer *nxt = t->next;
            if (t->rounds > 0) {
                t->rounds--;
            } else {
                /* fire: unlink, hand the callback to the result list */
                timer_unlink(t);
                if (t->callback != NULL) {
                    if (PyList_Append(due, t->callback) < 0) {
                        Py_DECREF(due);
                        Py_DECREF(t);
                        return NULL;
                    }
                    Py_CLEAR(t->callback);
                }
                Py_DECREF(t);  /* drop the wheel's reference */
            }
            t = nxt;
        }
    }
    if (w->next_dirty) {
        wheel_refresh_next(w);
    }
    return due;
}

/* Dispatch due timers without allocating an argument object. ReactorPoller uses
 * this directly, while advance_run() remains the Python-visible wrapper. */
static Py_ssize_t
wheel_run_due(TimingWheel *w, double now)
{
    int64_t target = (int64_t)((now - w->base) / w->resolution);
    Py_ssize_t fired = 0;
    if (target - w->cursor >= (int64_t)w->nslots * WHEEL_CATCHUP_ROTATIONS) {
        WheelTimer **nodes;
        Py_ssize_t count;
        if (wheel_collect_overdue(w, target, &nodes, &count) < 0) {
            return -1;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            WheelTimer *t = nodes[i];
            timer_unlink(t);
            if (t->callback != NULL) {
                PyObject *result = NULL;
                if (t->context != NULL) {
                    PyObject *run = PyObject_GetAttrString(t->context, "run");
                    if (run != NULL) {
                        Py_ssize_t na = t->args ? PyTuple_GET_SIZE(t->args) : 0;
                        PyObject *call = PyTuple_New(na + 1);
                        if (call != NULL) {
                            PyTuple_SET_ITEM(call, 0, Py_NewRef(t->callback));
                            for (Py_ssize_t j = 0; j < na; j++) {
                                PyTuple_SET_ITEM(call, j + 1,
                                    Py_NewRef(PyTuple_GET_ITEM(t->args, j)));
                            }
                            result = PyObject_CallObject(run, call);
                            Py_DECREF(call);
                        }
                        Py_DECREF(run);
                    }
                } else {
                    result = PyObject_CallObject(t->callback, t->args);
                }
                if (result == NULL) {
                    PyErr_WriteUnraisable(t->callback);
                } else {
                    Py_DECREF(result);
                }
                Py_CLEAR(t->callback);
                fired++;
            }
            Py_DECREF(t);
        }
        PyMem_Free(nodes);
        wheel_finish_jump(w, target);
        if (w->next_dirty) {
            wheel_refresh_next(w);
        }
        return fired;
    }
    while (w->cursor < target) {
        w->cursor++;
        int slot = (int)(w->cursor % w->nslots);
        WheelTimer *t = w->slots[slot];
        while (t != NULL) {
            WheelTimer *nxt = t->next;
            if (t->rounds > 0) {
                t->rounds--;
            } else {
                timer_unlink(t);
                if (t->callback != NULL) {
                    PyObject *result = NULL;
                    if (t->context != NULL) {
                        PyObject *run = PyObject_GetAttrString(t->context, "run");
                        if (run != NULL) {
                            Py_ssize_t na = t->args ? PyTuple_GET_SIZE(t->args) : 0;
                            PyObject *call = PyTuple_New(na + 1);
                            if (call != NULL) {
                                PyTuple_SET_ITEM(call, 0, Py_NewRef(t->callback));
                                for (Py_ssize_t i = 0; i < na; i++) {
                                    PyTuple_SET_ITEM(call, i + 1,
                                        Py_NewRef(PyTuple_GET_ITEM(t->args, i)));
                                }
                                result = PyObject_CallObject(run, call);
                                Py_DECREF(call);
                            }
                            Py_DECREF(run);
                        }
                    } else {
                        result = PyObject_CallObject(t->callback, t->args);
                    }
                    if (result == NULL) {
                        PyErr_WriteUnraisable(t->callback);
                    } else {
                        Py_DECREF(result);
                    }
                    Py_CLEAR(t->callback);
                    fired++;
                }
                Py_DECREF(t);
            }
            t = nxt;
        }
    }
    if (w->next_dirty) {
        wheel_refresh_next(w);
    }
    return fired;
}

/* advance_run(now_seconds) -> count of timers fired, each dispatched in C via
 * context.run(callback, *args). No Python round-trip per fired timer. */
static PyObject *
wheel_advance_run(PyObject *op, PyObject *arg)
{
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    Py_ssize_t fired = wheel_run_due((TimingWheel *)op, now);
    return fired < 0 ? NULL : PyLong_FromSsize_t(fired);
}

static PyObject *
wheel_count(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((TimingWheel *)op)->count);
}

static PyGetSetDef wheel_getset[] = {
    {"count", wheel_count, NULL, "number of live timers", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef wheel_methods[] = {
    {"schedule", wheel_schedule, METH_VARARGS,
     "schedule(delay_seconds, callback) -> WheelTimer"},
    {"schedule_call", (PyCFunction)(void (*)(void))wheel_schedule_call, METH_FASTCALL,
     "schedule_call(delay, callback, args, context) -> WheelTimer"},
    {"advance", wheel_advance, METH_O,
     "advance(now_seconds) -> list of callbacks that became due"},
    {"advance_run", wheel_advance_run, METH_O,
     "advance_run(now_seconds) -> count of timers fired (dispatched in C)"},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject TimingWheelType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.TimingWheel",
    .tp_basicsize = sizeof(TimingWheel),
    .tp_dealloc = wheel_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_init = wheel_init,
    .tp_new = PyType_GenericNew,
    .tp_methods = wheel_methods,
    .tp_getset = wheel_getset,
};

/* ======================================================================== */
/* Native contenders for the timer shootout: heap, hierarchical wheel, FIFO. */
/* A single dumb node (RTimer) is shared; each store owns the unlink logic so  */
/* the node carries no per-kind methods.                                       */
/* ======================================================================== */

typedef struct RTimer {
    PyObject_HEAD
    PyObject *callback;         /* NULL once fired or cancelled */
    struct RTimer *prev, *next; /* intrusive list (fifo / hier) */
    int64_t deadline;           /* absolute ticks (hier) */
    double when;                /* absolute seconds (fifo / heap) */
    int slot;                   /* hier slot, or fifo bucket index */
    int level;                  /* hier level */
    Py_ssize_t seq;             /* heap tie-break */
} RTimer;

static PyTypeObject RTimerType;

static void
rtimer_dealloc(PyObject *op)
{
    Py_CLEAR(((RTimer *)op)->callback);
    Py_TYPE(op)->tp_free(op);
}

static PyTypeObject RTimerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.RTimer",
    .tp_basicsize = sizeof(RTimer),
    .tp_dealloc = rtimer_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};

static RTimer *
rtimer_new(PyObject *cb)
{
    RTimer *t = PyObject_New(RTimer, &RTimerType);
    if (t == NULL) {
        return NULL;
    }
    t->callback = Py_NewRef(cb);
    t->prev = t->next = NULL;
    t->deadline = 0;
    t->when = 0.0;
    t->slot = -1;
    t->level = 0;
    t->seq = 0;
    return t;
}

/* --- HeapStore: binary min-heap + lazy cancel + 50% compaction ----------- */

#define HEAP_MIN_COMPACT 100

typedef struct {
    PyObject_HEAD
    RTimer **arr;
    Py_ssize_t len, cap, cancelled;
    Py_ssize_t seq;
    double now;
} HeapStore;

static PyTypeObject HeapStoreType;

static int
heap_less(RTimer *a, RTimer *b)
{
    if (a->when != b->when) return a->when < b->when;
    return a->seq < b->seq;
}

static int
heap_reserve(HeapStore *s, Py_ssize_t extra)
{
    if (s->len + extra <= s->cap) return 0;
    Py_ssize_t nc = s->cap ? s->cap * 2 : 64;
    while (nc < s->len + extra) nc *= 2;
    RTimer **a = PyMem_Realloc(s->arr, (size_t)nc * sizeof(RTimer *));
    if (a == NULL) { PyErr_NoMemory(); return -1; }
    s->arr = a;
    s->cap = nc;
    return 0;
}

static PyObject *
heap_schedule(PyObject *op, PyObject *args)
{
    HeapStore *s = (HeapStore *)op;
    double delay;
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "dO", &delay, &cb)) return NULL;
    if (heap_reserve(s, 1) < 0) return NULL;
    RTimer *t = rtimer_new(cb);
    if (t == NULL) return NULL;
    t->when = s->now + (delay < 0 ? 0 : delay);
    t->seq = s->seq++;
    Py_ssize_t i = s->len++;
    while (i > 0) {
        Py_ssize_t p = (i - 1) / 2;
        if (heap_less(t, s->arr[p])) { s->arr[i] = s->arr[p]; i = p; }
        else break;
    }
    s->arr[i] = t;
    return Py_NewRef((PyObject *)t);
}

static PyObject *
heap_cancel(PyObject *op, PyObject *node)
{
    HeapStore *s = (HeapStore *)op;
    RTimer *t = (RTimer *)node;
    if (t->callback == NULL) Py_RETURN_FALSE;
    Py_CLEAR(t->callback);           /* lazy: tombstone stays until popped */
    s->cancelled++;
    if (s->len > HEAP_MIN_COMPACT && s->cancelled * 2 > s->len) {
        Py_ssize_t w = 0;
        for (Py_ssize_t r = 0; r < s->len; r++) {
            if (s->arr[r]->callback != NULL) s->arr[w++] = s->arr[r];
            else Py_DECREF(s->arr[r]); /* drop the store's ref to a tombstone */
        }
        s->len = w;
        s->cancelled = 0;
        for (Py_ssize_t i = s->len / 2; i-- > 0; ) {   /* re-heapify */
            RTimer *x = s->arr[i];
            Py_ssize_t j = i, n = s->len;
            for (;;) {
                Py_ssize_t l = 2 * j + 1, rr = 2 * j + 2, m = j;
                RTimer *best = x;
                if (l < n && heap_less(s->arr[l], best)) { m = l; best = s->arr[l]; }
                if (rr < n && heap_less(s->arr[rr], best)) { m = rr; best = s->arr[rr]; }
                if (m == j) break;
                s->arr[j] = s->arr[m];
                j = m;
            }
            s->arr[j] = x;
        }
    }
    Py_RETURN_TRUE;
}

static RTimer *
heap_pop(HeapStore *s)
{
    RTimer *top = s->arr[0];
    RTimer *x = s->arr[--s->len];
    Py_ssize_t i = 0, n = s->len;
    for (;;) {
        Py_ssize_t l = 2 * i + 1, r = 2 * i + 2, m = i;
        RTimer *best = x;
        if (l < n && heap_less(s->arr[l], best)) { m = l; best = s->arr[l]; }
        if (r < n && heap_less(s->arr[r], best)) { m = r; best = s->arr[r]; }
        if (m == i) break;
        s->arr[i] = s->arr[m];
        i = m;
    }
    if (n > 0) s->arr[i] = x;
    return top;  /* transfers the store's ref */
}

static PyObject *
heap_advance(PyObject *op, PyObject *arg)
{
    HeapStore *s = (HeapStore *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) return NULL;
    s->now = now;
    PyObject *due = PyList_New(0);
    if (due == NULL) return NULL;
    while (s->len > 0 && s->arr[0]->when <= now) {
        RTimer *t = heap_pop(s);
        if (t->callback != NULL) {
            if (PyList_Append(due, t->callback) < 0) { Py_DECREF(due); Py_DECREF(t); return NULL; }
            Py_CLEAR(t->callback);
        } else {
            s->cancelled--;
        }
        Py_DECREF(t);
    }
    return due;
}

static PyObject *
heap_count(PyObject *op, void *c) { (void)c;
    HeapStore *s = (HeapStore *)op;
    return PyLong_FromSsize_t(s->len - s->cancelled);
}

static void
heap_dealloc(PyObject *op)
{
    HeapStore *s = (HeapStore *)op;
    for (Py_ssize_t i = 0; i < s->len; i++) Py_DECREF(s->arr[i]);
    PyMem_Free(s->arr);
    Py_TYPE(op)->tp_free(op);
}

static int
heap_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    HeapStore *s = (HeapStore *)op;
    s->arr = NULL; s->len = s->cap = s->cancelled = s->seq = 0; s->now = 0.0;
    return 0;
}

static PyMethodDef heap_methods[] = {
    {"schedule", heap_schedule, METH_VARARGS, NULL},
    {"cancel", heap_cancel, METH_O, NULL},
    {"advance", heap_advance, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};
static PyGetSetDef heap_getset[] = {
    {"count", heap_count, NULL, NULL, NULL}, {NULL, NULL, NULL, NULL, NULL},
};
static PyTypeObject HeapStoreType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.HeapStore",
    .tp_basicsize = sizeof(HeapStore),
    .tp_dealloc = heap_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_init = heap_init,
    .tp_new = PyType_GenericNew,
    .tp_methods = heap_methods,
    .tp_getset = heap_getset,
};

/* --- HierStore: hierarchical cascading wheel (Varghese-Lauck) ------------ */

#define HS_S 64
#define HS_SHIFT 6
#define HS_LEVELS 4

static const int64_t HS_SPAN[HS_LEVELS] = {64, 4096, 262144, 16777216};

typedef struct {
    PyObject_HEAD
    RTimer *levels[HS_LEVELS][HS_S];
    int64_t cursor;
    Py_ssize_t count;
    double res;
} HierStore;

static PyTypeObject HierStoreType;

static void
hier_link(HierStore *s, RTimer *t)
{
    int64_t rem = t->deadline - s->cursor;
    if (rem < 0) rem = 0;
    int lvl = HS_LEVELS - 1;
    for (int i = 0; i < HS_LEVELS; i++) {
        if (rem < HS_SPAN[i]) { lvl = i; break; }
    }
    int slot = (int)((t->deadline >> (lvl * HS_SHIFT)) & (HS_S - 1));
    t->level = lvl;
    t->slot = slot;
    t->prev = NULL;
    t->next = s->levels[lvl][slot];
    if (t->next != NULL) t->next->prev = t;
    s->levels[lvl][slot] = t;
}

static int
hier_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    HierStore *s = (HierStore *)op;
    double res = 0.001;
    static char *kw[] = {"resolution", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|d", kw, &res)) return -1;
    memset(s->levels, 0, sizeof(s->levels));
    s->cursor = 0; s->count = 0; s->res = res > 0 ? res : 0.001;
    return 0;
}

static PyObject *
hier_schedule(PyObject *op, PyObject *args)
{
    HierStore *s = (HierStore *)op;
    double delay;
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "dO", &delay, &cb)) return NULL;
    int64_t ticks = (int64_t)((delay < 0 ? 0 : delay) / s->res);
    if (ticks < 1) ticks = 1;
    RTimer *t = rtimer_new(cb);
    if (t == NULL) return NULL;
    t->deadline = s->cursor + ticks;
    hier_link(s, t);
    s->count++;
    return Py_NewRef((PyObject *)t);
}

static PyObject *
hier_cancel(PyObject *op, PyObject *node)
{
    HierStore *s = (HierStore *)op;
    RTimer *t = (RTimer *)node;
    if (t->callback == NULL) Py_RETURN_FALSE;
    Py_CLEAR(t->callback);
    if (t->prev != NULL) t->prev->next = t->next;
    else s->levels[t->level][t->slot] = t->next;
    if (t->next != NULL) t->next->prev = t->prev;
    t->prev = t->next = NULL;
    s->count--;
    Py_DECREF(t);  /* drop the store's ref */
    Py_RETURN_TRUE;
}

static PyObject *
hier_advance(PyObject *op, PyObject *arg)
{
    HierStore *s = (HierStore *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) return NULL;
    int64_t target = (int64_t)(now / s->res);
    PyObject *due = PyList_New(0);
    if (due == NULL) return NULL;
    while (s->cursor < target) {
        s->cursor++;
        if ((s->cursor & (HS_S - 1)) == 0) {
            for (int lvl = 1; lvl < HS_LEVELS; lvl++) {
                int idx = (int)((s->cursor >> (lvl * HS_SHIFT)) & (HS_S - 1));
                RTimer *node = s->levels[lvl][idx];
                s->levels[lvl][idx] = NULL;
                while (node != NULL) {
                    RTimer *nxt = node->next;
                    node->prev = node->next = NULL;
                    hier_link(s, node);  /* re-file closer (cascade) */
                    node = nxt;
                }
                if (idx != 0) break;
            }
        }
        int slot = (int)(s->cursor & (HS_S - 1));
        RTimer *node = s->levels[0][slot];
        s->levels[0][slot] = NULL;
        while (node != NULL) {
            RTimer *nxt = node->next;
            if (PyList_Append(due, node->callback) < 0) { Py_DECREF(due); return NULL; }
            Py_CLEAR(node->callback);
            s->count--;
            Py_DECREF(node);  /* drop the store's ref */
            node = nxt;
        }
    }
    return due;
}

static PyObject *
hier_count(PyObject *op, void *c) { (void)c;
    return PyLong_FromSsize_t(((HierStore *)op)->count);
}

static void
hier_dealloc(PyObject *op)
{
    HierStore *s = (HierStore *)op;
    for (int lvl = 0; lvl < HS_LEVELS; lvl++) {
        for (int slot = 0; slot < HS_S; slot++) {
            RTimer *node = s->levels[lvl][slot];
            while (node != NULL) {
                RTimer *nxt = node->next;
                node->prev = node->next = NULL;
                Py_DECREF(node);
                node = nxt;
            }
        }
    }
    Py_TYPE(op)->tp_free(op);
}

static PyMethodDef hier_methods[] = {
    {"schedule", hier_schedule, METH_VARARGS, NULL},
    {"cancel", hier_cancel, METH_O, NULL},
    {"advance", hier_advance, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};
static PyGetSetDef hier_getset[] = {
    {"count", hier_count, NULL, NULL, NULL}, {NULL, NULL, NULL, NULL, NULL},
};
static PyTypeObject HierStoreType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.HierStore",
    .tp_basicsize = sizeof(HierStore),
    .tp_dealloc = hier_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_init = hier_init,
    .tp_new = PyType_GenericNew,
    .tp_methods = hier_methods,
    .tp_getset = hier_getset,
};

/* --- FifoStore: one FIFO list per fixed duration ------------------------- */

typedef struct { RTimer *head, *tail; } FBucket;

typedef struct {
    PyObject_HEAD
    PyObject *keymap;       /* dict {int key: int bucket index} */
    FBucket *buckets;
    Py_ssize_t nb, cap, count;
    double now, q;
} FifoStore;

static PyTypeObject FifoStoreType;

static int
fifo_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    FifoStore *s = (FifoStore *)op;
    double q = 0.001;
    static char *kw[] = {"quantum", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|d", kw, &q)) return -1;
    s->keymap = PyDict_New();
    if (s->keymap == NULL) return -1;
    s->buckets = NULL; s->nb = s->cap = s->count = 0;
    s->now = 0.0; s->q = q > 0 ? q : 0.001;
    return 0;
}

static Py_ssize_t
fifo_bucket_for(FifoStore *s, int64_t key)
{
    PyObject *k = PyLong_FromLongLong((long long)key);
    if (k == NULL) return -1;
    PyObject *v = PyDict_GetItemWithError(s->keymap, k);
    if (v != NULL) {
        Py_ssize_t idx = PyLong_AsSsize_t(v);
        Py_DECREF(k);
        if (idx == -1 && PyErr_Occurred()) {
            return -1;
        }
        return idx;
    }
    if (PyErr_Occurred()) { Py_DECREF(k); return -1; }
    if (s->nb == s->cap) {
        Py_ssize_t nc = s->cap ? s->cap * 2 : 8;
        FBucket *b = PyMem_Realloc(s->buckets, (size_t)nc * sizeof(FBucket));
        if (b == NULL) { Py_DECREF(k); PyErr_NoMemory(); return -1; }
        s->buckets = b; s->cap = nc;
    }
    Py_ssize_t idx = s->nb++;
    s->buckets[idx].head = s->buckets[idx].tail = NULL;
    PyObject *iv = PyLong_FromSsize_t(idx);
    if (iv == NULL || PyDict_SetItem(s->keymap, k, iv) < 0) {
        Py_XDECREF(iv); Py_DECREF(k); s->nb--; return -1;
    }
    Py_DECREF(iv); Py_DECREF(k);
    return idx;
}

static PyObject *
fifo_schedule(PyObject *op, PyObject *args)
{
    FifoStore *s = (FifoStore *)op;
    double delay;
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "dO", &delay, &cb)) return NULL;
    if (delay < 0) delay = 0;
    Py_ssize_t idx = fifo_bucket_for(s, (int64_t)(delay / s->q));
    if (idx < 0) return NULL;
    RTimer *t = rtimer_new(cb);
    if (t == NULL) return NULL;
    t->when = s->now + delay;
    t->slot = (int)idx;
    FBucket *b = &s->buckets[idx];
    t->prev = b->tail;
    t->next = NULL;
    if (b->tail != NULL) b->tail->next = t;
    else b->head = t;
    b->tail = t;
    s->count++;
    return Py_NewRef((PyObject *)t);
}

static PyObject *
fifo_cancel(PyObject *op, PyObject *node)
{
    FifoStore *s = (FifoStore *)op;
    RTimer *t = (RTimer *)node;
    if (t->callback == NULL) Py_RETURN_FALSE;
    Py_CLEAR(t->callback);
    FBucket *b = &s->buckets[t->slot];
    if (t->prev != NULL) t->prev->next = t->next;
    else b->head = t->next;
    if (t->next != NULL) t->next->prev = t->prev;
    else b->tail = t->prev;
    t->prev = t->next = NULL;
    s->count--;
    Py_DECREF(t);
    Py_RETURN_TRUE;
}

static PyObject *
fifo_advance(PyObject *op, PyObject *arg)
{
    FifoStore *s = (FifoStore *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) return NULL;
    s->now = now;
    PyObject *due = PyList_New(0);
    if (due == NULL) return NULL;
    for (Py_ssize_t i = 0; i < s->nb; i++) {
        FBucket *b = &s->buckets[i];
        RTimer *node = b->head;
        while (node != NULL && node->when <= now) {
            RTimer *nxt = node->next;
            if (PyList_Append(due, node->callback) < 0) { Py_DECREF(due); return NULL; }
            Py_CLEAR(node->callback);
            s->count--;
            Py_DECREF(node);
            node = nxt;
        }
        b->head = node;
        if (node != NULL) node->prev = NULL;
        else b->tail = NULL;
    }
    return due;
}

static PyObject *
fifo_count(PyObject *op, void *c) { (void)c;
    return PyLong_FromSsize_t(((FifoStore *)op)->count);
}

static void
fifo_dealloc(PyObject *op)
{
    FifoStore *s = (FifoStore *)op;
    for (Py_ssize_t i = 0; i < s->nb; i++) {
        RTimer *node = s->buckets[i].head;
        while (node != NULL) {
            RTimer *nxt = node->next;
            node->prev = node->next = NULL;
            Py_DECREF(node);
            node = nxt;
        }
    }
    PyMem_Free(s->buckets);
    Py_CLEAR(s->keymap);
    Py_TYPE(op)->tp_free(op);
}

static PyMethodDef fifo_methods[] = {
    {"schedule", fifo_schedule, METH_VARARGS, NULL},
    {"cancel", fifo_cancel, METH_O, NULL},
    {"advance", fifo_advance, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};
static PyGetSetDef fifo_getset[] = {
    {"count", fifo_count, NULL, NULL, NULL}, {NULL, NULL, NULL, NULL, NULL},
};
static PyTypeObject FifoStoreType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.FifoStore",
    .tp_basicsize = sizeof(FifoStore),
    .tp_dealloc = fifo_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_init = fifo_init,
    .tp_new = PyType_GenericNew,
    .tp_methods = fifo_methods,
    .tp_getset = fifo_getset,
};

/* ======================================================================== */
/* SocketTransport: a native asyncio Transport for plaintext TCP.             */
/*                                                                            */
/* App-facing behaviour matches asyncio's _SelectorSocketTransport exactly    */
/* (connection_made/get_buffer/buffer_updated/data_received/eof_received/     */
/* connection_lost, flow control, pause/resume). Under the hood it uses       */
/* direct recv/send syscalls, a single contiguous offset write buffer (O(1)   */
/* size, one send -- no chunk list / sendmsg), cached bound protocol methods, */
/* and a bounded eager read-drain so a burst costs fewer epoll wakeups.       */
/* ======================================================================== */

#define ST_MAX_DRAIN 8          /* recvs per readable event before yielding */
#define ST_DATA_RECV 262144     /* recv size for non-buffered protocols */
#define ST_CORK_MAX 262144      /* flush corked writes once they reach this size */

/* Metal-only operation/completion substrate. The epoll adapter completes
 * readiness-triggered socket operations synchronously today; io_uring can retain
 * the same operation handle until its CQE arrives. Slots are per poller/worker,
 * bounded, and generation-validated before state-machine delivery. */
#define METAL_CONNECTION_CAPACITY 4096
#define METAL_OPERATION_CAPACITY 4096
#define METAL_RECV_BUFFER_COUNT 16
#define METAL_RECV_BUFFER_SIZE 16384
#define METAL_RECV_BUFFER_GROUP 1
#define METAL_SLOT_NONE UINT32_MAX

enum {
    METAL_OP_RECV = 1,
    METAL_OP_SEND = 2,
};
enum {
    METAL_IO_EPOLL = 0,
    METAL_IO_URING = 1,
};
enum {
    METAL_COMPLETION_EOF = 0x1,
    METAL_COMPLETION_ERROR = 0x2,
};

typedef struct {
    uint64_t token;
    int32_t result;
    uint16_t kind;
    uint16_t flags;
    uint32_t value;
} MetalCompletion;

typedef struct {
    void *owner;
    uint64_t related;
    uint32_t generation;
    uint32_t next_free;
    uint16_t kind;
    uint8_t live;
} MetalSlot;

typedef struct {
    MetalSlot *slots;
    uint32_t capacity;
    uint32_t free_head;
    uint32_t occupancy;
    uint32_t high_water;
    uint64_t exhaustions;
    uint64_t generation_wraps;
    uint64_t stale;
} MetalSlab;

typedef struct {
    int fd;
    void *sq_ring;
    void *cq_ring;
    struct io_uring_sqe *sqes;
    size_t sq_ring_size;
    size_t cq_ring_size;
    size_t sqes_size;
    unsigned *sq_head;
    unsigned *sq_tail;
    unsigned *sq_mask;
    unsigned *sq_entries;
    unsigned *sq_array;
    unsigned *cq_head;
    unsigned *cq_tail;
    unsigned *cq_mask;
    struct io_uring_cqe *cqes;
} MetalUring;

typedef struct {
    struct io_uring_buf_ring *ring;
    char *data;
    size_t ring_size;
    size_t data_size;
    uint16_t tail;
    uint16_t entries;
    uint16_t mask;
    uint16_t group_id;
    uint32_t buffer_size;
    int registered;
} MetalProvidedBuffers;

typedef struct {
    MetalSlab connections;
    MetalSlab operations;
    MetalUring uring;
    MetalUring listener_uring;
    MetalUring receive_uring;
    MetalProvidedBuffers receive_buffers;
    uint64_t submissions;
    uint64_t accept_submissions;
    uint64_t accept_completions;
    uint64_t accept_stale;
    uint64_t accept_errors;
    uint64_t accept_multishot_fallbacks;
    uint64_t receive_submissions;
    uint64_t receive_completions;
    uint64_t receive_stale;
    uint64_t receive_errors;
    uint64_t receive_multishot_fallbacks;
    uint64_t provided_buffer_recycles;
    int receive_enabled;
    int receive_setup_errno;
    uint64_t completions;
    uint64_t cross_worker_rejections;
    unsigned long owner_thread;
    uint32_t worker_id;
    uint8_t io_backend;
} MetalRuntime;

static void
metal_uring_clear(MetalUring *ring)
{
    if (ring->sqes != MAP_FAILED && ring->sqes != NULL) {
        munmap(ring->sqes, ring->sqes_size);
    }
    if (ring->cq_ring != MAP_FAILED && ring->cq_ring != NULL &&
        ring->cq_ring != ring->sq_ring) {
        munmap(ring->cq_ring, ring->cq_ring_size);
    }
    if (ring->sq_ring != MAP_FAILED && ring->sq_ring != NULL) {
        munmap(ring->sq_ring, ring->sq_ring_size);
    }
    if (ring->fd >= 0) {
        close(ring->fd);
    }
    memset(ring, 0, sizeof(*ring));
    ring->fd = -1;
}

static int
metal_uring_init(MetalUring *ring, unsigned entries)
{
    memset(ring, 0, sizeof(*ring));
    ring->fd = -1;
    struct io_uring_params params;
    memset(&params, 0, sizeof(params));
    int fd = (int)syscall(SYS_io_uring_setup, entries, &params);
    if (fd < 0) {
        return -1;
    }
    ring->fd = fd;
    ring->sq_ring_size = params.sq_off.array +
                         params.sq_entries * sizeof(unsigned);
    ring->cq_ring_size = params.cq_off.cqes +
                         params.cq_entries * sizeof(struct io_uring_cqe);
    if (params.features & IORING_FEAT_SINGLE_MMAP) {
        if (ring->cq_ring_size > ring->sq_ring_size) {
            ring->sq_ring_size = ring->cq_ring_size;
        }
        ring->cq_ring_size = ring->sq_ring_size;
    }
    ring->sq_ring = mmap(NULL, ring->sq_ring_size, PROT_READ | PROT_WRITE,
                         MAP_SHARED, fd, IORING_OFF_SQ_RING);
    if (ring->sq_ring == MAP_FAILED) {
        metal_uring_clear(ring);
        return -1;
    }
    if (params.features & IORING_FEAT_SINGLE_MMAP) {
        ring->cq_ring = ring->sq_ring;
    } else {
        ring->cq_ring = mmap(NULL, ring->cq_ring_size, PROT_READ | PROT_WRITE,
                             MAP_SHARED, fd, IORING_OFF_CQ_RING);
        if (ring->cq_ring == MAP_FAILED) {
            metal_uring_clear(ring);
            return -1;
        }
    }
    ring->sqes_size = params.sq_entries * sizeof(struct io_uring_sqe);
    ring->sqes = mmap(NULL, ring->sqes_size, PROT_READ | PROT_WRITE,
                      MAP_SHARED, fd, IORING_OFF_SQES);
    if (ring->sqes == MAP_FAILED) {
        metal_uring_clear(ring);
        return -1;
    }
    char *sq = (char *)ring->sq_ring;
    char *cq = (char *)ring->cq_ring;
    ring->sq_head = (unsigned *)(sq + params.sq_off.head);
    ring->sq_tail = (unsigned *)(sq + params.sq_off.tail);
    ring->sq_mask = (unsigned *)(sq + params.sq_off.ring_mask);
    ring->sq_entries = (unsigned *)(sq + params.sq_off.ring_entries);
    ring->sq_array = (unsigned *)(sq + params.sq_off.array);
    ring->cq_head = (unsigned *)(cq + params.cq_off.head);
    ring->cq_tail = (unsigned *)(cq + params.cq_off.tail);
    ring->cq_mask = (unsigned *)(cq + params.cq_off.ring_mask);
    ring->cqes = (struct io_uring_cqe *)(cq + params.cq_off.cqes);
    return 0;
}

static void
metal_provided_buffers_clear(MetalUring *uring, MetalProvidedBuffers *pool)
{
    if (pool->registered && uring->fd >= 0) {
        struct io_uring_buf_reg registration;
        memset(&registration, 0, sizeof(registration));
        registration.bgid = pool->group_id;
        syscall(SYS_io_uring_register, uring->fd,
                IORING_UNREGISTER_PBUF_RING, &registration, 1);
    }
    if (pool->ring != NULL && pool->ring != MAP_FAILED) {
        munmap(pool->ring, pool->ring_size);
    }
    if (pool->data != NULL && pool->data != MAP_FAILED) {
        munmap(pool->data, pool->data_size);
    }
    memset(pool, 0, sizeof(*pool));
}

static void
metal_provided_buffer_recycle(MetalProvidedBuffers *pool, uint16_t buffer_id)
{
    uint16_t index = pool->tail & pool->mask;
    struct io_uring_buf *buffer = &pool->ring->bufs[index];
    buffer->addr = (uint64_t)(uintptr_t)(
        pool->data + (size_t)buffer_id * pool->buffer_size);
    buffer->len = pool->buffer_size;
    buffer->bid = buffer_id;
    pool->tail++;
    __atomic_store_n(&pool->ring->tail, pool->tail, __ATOMIC_RELEASE);
}

static int
metal_provided_buffers_init(MetalUring *uring, MetalProvidedBuffers *pool,
                            uint16_t entries, uint32_t buffer_size,
                            uint16_t group_id)
{
    memset(pool, 0, sizeof(*pool));
    if (entries == 0 || (entries & (entries - 1)) != 0) {
        errno = EINVAL;
        return -1;
    }
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) {
        page_size = 4096;
    }
    size_t raw_ring_size = (size_t)entries * sizeof(struct io_uring_buf);
    pool->ring_size = (raw_ring_size + (size_t)page_size - 1) &
                      ~((size_t)page_size - 1);
    pool->data_size = (size_t)entries * buffer_size;
    pool->ring = mmap(NULL, pool->ring_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool->ring == MAP_FAILED) {
        return -1;
    }
    pool->data = mmap(NULL, pool->data_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool->data == MAP_FAILED) {
        int saved_errno = errno;
        munmap(pool->ring, pool->ring_size);
        memset(pool, 0, sizeof(*pool));
        errno = saved_errno;
        return -1;
    }
    pool->entries = entries;
    pool->mask = entries - 1;
    pool->group_id = group_id;
    pool->buffer_size = buffer_size;

    struct io_uring_buf_reg registration;
    memset(&registration, 0, sizeof(registration));
    registration.ring_addr = (uint64_t)(uintptr_t)pool->ring;
    registration.ring_entries = entries;
    registration.bgid = group_id;
    if (syscall(SYS_io_uring_register, uring->fd,
                IORING_REGISTER_PBUF_RING, &registration, 1) < 0) {
        int saved_errno = errno;
        metal_provided_buffers_clear(uring, pool);
        errno = saved_errno;
        return -1;
    }
    pool->registered = 1;
    for (uint16_t buffer_id = 0; buffer_id < entries; buffer_id++) {
        metal_provided_buffer_recycle(pool, buffer_id);
    }
    return 0;
}

static int
metal_uring_submit(MetalUring *ring, uint8_t opcode, int fd,
                   const void *address, uint32_t length, uint64_t token,
                   uint64_t *completed_token, int *operation_result)
{
    unsigned head = __atomic_load_n(ring->sq_head, __ATOMIC_ACQUIRE);
    unsigned tail = __atomic_load_n(ring->sq_tail, __ATOMIC_RELAXED);
    if (tail - head >= *ring->sq_entries) {
        errno = EBUSY;
        return -1;
    }
    unsigned index = tail & *ring->sq_mask;
    struct io_uring_sqe *sqe = &ring->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode = opcode;
    sqe->fd = fd;
    sqe->addr = (uint64_t)(uintptr_t)address;
    sqe->len = length;
    sqe->user_data = token;
    ring->sq_array[index] = index;
    __atomic_store_n(ring->sq_tail, tail + 1, __ATOMIC_RELEASE);

    int entered;
    do {
        entered = (int)syscall(SYS_io_uring_enter, ring->fd, 1, 1,
                               IORING_ENTER_GETEVENTS, NULL, 0);
    } while (entered < 0 && errno == EINTR);
    if (entered < 0) {
        return -1;
    }
    unsigned cq_head = __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
    unsigned cq_tail = __atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE);
    if (cq_head == cq_tail) {
        errno = EIO;
        return -1;
    }
    struct io_uring_cqe *cqe = &ring->cqes[cq_head & *ring->cq_mask];
    *operation_result = cqe->res;
    *completed_token = cqe->user_data;
    __atomic_store_n(ring->cq_head, cq_head + 1, __ATOMIC_RELEASE);
    return 0;
}

static int
metal_uring_queue_accept(MetalUring *ring, int fd, uint64_t token,
                         int multishot)
{
    unsigned head = __atomic_load_n(ring->sq_head, __ATOMIC_ACQUIRE);
    unsigned tail = __atomic_load_n(ring->sq_tail, __ATOMIC_RELAXED);
    if (tail - head >= *ring->sq_entries) {
        errno = EBUSY;
        return -1;
    }
    unsigned index = tail & *ring->sq_mask;
    struct io_uring_sqe *sqe = &ring->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode = IORING_OP_ACCEPT;
    sqe->fd = fd;
    sqe->accept_flags = SOCK_NONBLOCK | SOCK_CLOEXEC;
#ifdef IORING_ACCEPT_MULTISHOT
    if (multishot) {
        sqe->ioprio |= IORING_ACCEPT_MULTISHOT;
    }
#else
    (void)multishot;
#endif
    sqe->user_data = token;
    ring->sq_array[index] = index;
    __atomic_store_n(ring->sq_tail, tail + 1, __ATOMIC_RELEASE);

    int entered;
    do {
        entered = (int)syscall(SYS_io_uring_enter, ring->fd, 1, 0, 0, NULL, 0);
    } while (entered < 0 && errno == EINTR);
    return entered < 0 ? -1 : 0;
}

static int
metal_uring_queue_receive(MetalUring *ring, int fd, uint64_t token,
                          uint16_t buffer_group, uint32_t buffer_size,
                          int multishot)
{
    unsigned head = __atomic_load_n(ring->sq_head, __ATOMIC_ACQUIRE);
    unsigned tail = __atomic_load_n(ring->sq_tail, __ATOMIC_RELAXED);
    if (tail - head >= *ring->sq_entries) {
        errno = EBUSY;
        return -1;
    }
    unsigned index = tail & *ring->sq_mask;
    struct io_uring_sqe *sqe = &ring->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode = IORING_OP_RECV;
    sqe->fd = fd;
    sqe->len = multishot ? 0 : buffer_size;
    sqe->flags = IOSQE_BUFFER_SELECT;
    sqe->buf_group = buffer_group;
#ifdef IORING_RECV_MULTISHOT
    if (multishot) {
        sqe->ioprio |= IORING_RECV_MULTISHOT;
    }
#else
    (void)multishot;
#endif
    sqe->user_data = token;
    ring->sq_array[index] = index;
    __atomic_store_n(ring->sq_tail, tail + 1, __ATOMIC_RELEASE);

    int entered;
    do {
        entered = (int)syscall(SYS_io_uring_enter, ring->fd, 1, 0, 0, NULL, 0);
    } while (entered < 0 && errno == EINTR);
    return entered < 0 ? -1 : 0;
}

static int
metal_uring_queue_cancel(MetalUring *ring, uint64_t target_token)
{
    unsigned head = __atomic_load_n(ring->sq_head, __ATOMIC_ACQUIRE);
    unsigned tail = __atomic_load_n(ring->sq_tail, __ATOMIC_RELAXED);
    if (tail - head >= *ring->sq_entries) {
        errno = EBUSY;
        return -1;
    }
    unsigned index = tail & *ring->sq_mask;
    struct io_uring_sqe *sqe = &ring->sqes[index];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode = IORING_OP_ASYNC_CANCEL;
    sqe->addr = target_token;
    sqe->user_data = 0;
    ring->sq_array[index] = index;
    __atomic_store_n(ring->sq_tail, tail + 1, __ATOMIC_RELEASE);
    int entered;
    do {
        entered = (int)syscall(SYS_io_uring_enter, ring->fd, 1, 0, 0, NULL, 0);
    } while (entered < 0 && errno == EINTR);
    return entered < 0 ? -1 : 0;
}

static int
metal_slab_init(MetalSlab *slab, uint32_t capacity)
{
    slab->slots = PyMem_Calloc(capacity, sizeof(MetalSlot));
    if (slab->slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    slab->capacity = capacity;
    slab->free_head = 0;
    slab->occupancy = 0;
    slab->high_water = 0;
    slab->exhaustions = 0;
    slab->generation_wraps = 0;
    slab->stale = 0;
    for (uint32_t i = 0; i < capacity; i++) {
        slab->slots[i].generation = 1;
        slab->slots[i].next_free = i + 1 < capacity ? i + 1 : METAL_SLOT_NONE;
    }
    return 0;
}

static void
metal_slab_clear(MetalSlab *slab)
{
    PyMem_Free(slab->slots);
    memset(slab, 0, sizeof(*slab));
    slab->free_head = METAL_SLOT_NONE;
}

static uint64_t
metal_slab_allocate(MetalSlab *slab, void *owner, uint64_t related, uint16_t kind)
{
    uint32_t index = slab->free_head;
    if (index == METAL_SLOT_NONE) {
        slab->exhaustions++;
        return 0;
    }
    MetalSlot *slot = &slab->slots[index];
    slab->free_head = slot->next_free;
    slot->owner = owner;
    slot->related = related;
    slot->kind = kind;
    slot->live = 1;
    slab->occupancy++;
    if (slab->occupancy > slab->high_water) {
        slab->high_water = slab->occupancy;
    }
    return ((uint64_t)slot->generation << 32) | index;
}

static MetalSlot *
metal_slab_validate(MetalSlab *slab, uint64_t token, void *owner)
{
    uint32_t index = (uint32_t)token;
    uint32_t generation = (uint32_t)(token >> 32);
    if (index >= slab->capacity) {
        slab->stale++;
        return NULL;
    }
    MetalSlot *slot = &slab->slots[index];
    if (!slot->live || slot->generation != generation || slot->owner != owner) {
        slab->stale++;
        return NULL;
    }
    return slot;
}

static void
metal_slab_release(MetalSlab *slab, uint64_t token, void *owner)
{
    MetalSlot *slot = metal_slab_validate(slab, token, owner);
    if (slot == NULL) {
        return;
    }
    uint32_t index = (uint32_t)token;
    slot->owner = NULL;
    slot->related = 0;
    slot->kind = 0;
    slot->live = 0;
    slot->generation++;
    if (slot->generation == 0) {
        slot->generation = 1;
        slab->generation_wraps++;
    }
    slot->next_free = slab->free_head;
    slab->free_head = index;
    slab->occupancy--;
}

typedef struct {
    PyObject_HEAD
    PyObject *loop;
    PyObject *sock;             /* the Python socket (kept alive; closed on lost) */
    PyObject *protocol;
    PyObject *server;           /* AbstractServer, or None */
    PyObject *extra;            /* get_extra_info dict */
    int fd;
    int buffered;               /* protocol is a BufferedProtocol */
    int fused_http1;             /* direct native HTTP/1 buffer C API */
    /* cached bound methods */
    PyObject *m_add_reader, *m_remove_reader, *m_add_writer, *m_remove_writer;
    PyObject *m_call_soon;
    PyObject *proto_get_buffer, *proto_buffer_updated, *proto_data_received;
    PyObject *read_ready, *write_ready, *conn_lost_cb;  /* our own bound methods */
    /* write buffer: contiguous bytearray with an advancing head */
    PyObject *wbuf;
    PyObject *cork_obj;          /* retained exact bytes; sent directly at flush */
    Py_ssize_t whead;
    int writing;                /* writer registered */
    int cork;                   /* buffer writes during synchronous request drive */
    Py_ssize_t direct_writelines; /* diagnostic count of sendmsg fast-path writes */
    Py_ssize_t direct_read_dispatches; /* poller called st_read_ready directly */
    Py_ssize_t direct_protocol_writes; /* server called transport C API directly */
    Py_ssize_t zero_copy_cork_writes; /* exact bytes retained instead of copied */
    /* flow control + lifecycle */
    Py_ssize_t high_water, low_water;
    int protocol_paused;
    int reading_paused;
    int closing;
    int conn_lost;
    int eof;
    int protocol_connected;
    PyObject *poller_obj;        /* owns the per-worker MetalRuntime */
    MetalRuntime *metal;         /* borrowed from poller_obj */
    uint64_t connection_token;
    int uring_receive_active;
    int uring_receive_multishot;
} SocketTransport;

static PyTypeObject SocketTransportType;
static int metal_attach_transport(SocketTransport *, PyObject *);
static void metal_detach_transport(SocketTransport *);

static uint64_t
metal_begin_operation(SocketTransport *t, uint16_t kind)
{
    if (t->metal == NULL) {
        return 0;
    }
    if (PyThread_get_thread_ident() != t->metal->owner_thread) {
        t->metal->cross_worker_rejections++;
        errno = EXDEV;
        return 0;
    }
    uint64_t token = metal_slab_allocate(
        &t->metal->operations, t, t->connection_token, kind
    );
    if (token == 0) {
        errno = ENOBUFS;
        return 0;
    }
    t->metal->submissions++;
    return token;
}

static ssize_t
metal_finish_operation(SocketTransport *t, uint64_t token, uint16_t kind,
                       ssize_t raw_result, int saved_errno)
{
    if (t->metal == NULL) {
        errno = saved_errno;
        return raw_result;
    }
    MetalCompletion completion;
    completion.token = token;
    completion.result = raw_result < 0 ? -saved_errno : (int32_t)raw_result;
    completion.kind = kind;
    completion.flags = raw_result == 0 ? METAL_COMPLETION_EOF : 0;
    if (raw_result < 0) {
        completion.flags |= METAL_COMPLETION_ERROR;
    }
    completion.value = 0;

    MetalSlot *operation = metal_slab_validate(&t->metal->operations, token, t);
    MetalSlot *connection = metal_slab_validate(
        &t->metal->connections, t->connection_token, t
    );
    int valid = operation != NULL && connection != NULL &&
                operation->kind == kind &&
                operation->related == t->connection_token;
    if (operation != NULL) {
        metal_slab_release(&t->metal->operations, token, t);
    }
    t->metal->completions++;
    if (!valid) {
        errno = ECANCELED;
        return -1;
    }
    errno = saved_errno;
    return completion.result < 0 ? -1 : (ssize_t)completion.result;
}

static ssize_t
metal_recv(SocketTransport *t, void *buffer, size_t size)
{
    if (t->metal == NULL) {
        ssize_t result;
        do {
            result = recv(t->fd, buffer, size, 0);
        } while (result < 0 && errno == EINTR);
        return result;
    }
    uint64_t token = metal_begin_operation(t, METAL_OP_RECV);
    if (token == 0) {
        return -1;
    }
    size_t request = size > INT32_MAX ? INT32_MAX : size;
    ssize_t result;
    int saved_errno;
    if (t->metal->io_backend == METAL_IO_URING) {
        uint64_t completed_token = 0;
        int operation_result = 0;
        if (metal_uring_submit(&t->metal->uring, IORING_OP_RECV, t->fd,
                               buffer, (uint32_t)request, token,
                               &completed_token, &operation_result) < 0) {
            result = -1;
            saved_errno = errno;
        } else if (completed_token != token) {
            t->metal->operations.stale++;
            result = -1;
            saved_errno = ECANCELED;
        } else {
            result = operation_result < 0 ? -1 : operation_result;
            saved_errno = operation_result < 0 ? -operation_result : 0;
        }
    } else {
        do {
            result = recv(t->fd, buffer, request, 0);
        } while (result < 0 && errno == EINTR);
        saved_errno = result < 0 ? errno : 0;
    }
    return metal_finish_operation(t, token, METAL_OP_RECV, result, saved_errno);
}

static ssize_t
metal_send(SocketTransport *t, const void *buffer, size_t size)
{
    if (t->metal == NULL) {
        ssize_t result;
        do {
            result = send(t->fd, buffer, size, 0);
        } while (result < 0 && errno == EINTR);
        return result;
    }
    uint64_t token = metal_begin_operation(t, METAL_OP_SEND);
    if (token == 0) {
        return -1;
    }
    size_t request = size > INT32_MAX ? INT32_MAX : size;
    ssize_t result;
    int saved_errno;
    if (t->metal->io_backend == METAL_IO_URING) {
        uint64_t completed_token = 0;
        int operation_result = 0;
        if (metal_uring_submit(&t->metal->uring, IORING_OP_SEND, t->fd,
                               buffer, (uint32_t)request, token,
                               &completed_token, &operation_result) < 0) {
            result = -1;
            saved_errno = errno;
        } else if (completed_token != token) {
            t->metal->operations.stale++;
            result = -1;
            saved_errno = ECANCELED;
        } else {
            result = operation_result < 0 ? -1 : operation_result;
            saved_errno = operation_result < 0 ? -operation_result : 0;
        }
    } else {
        do {
            result = send(t->fd, buffer, request, 0);
        } while (result < 0 && errno == EINTR);
        saved_errno = result < 0 ? errno : 0;
    }
    return metal_finish_operation(t, token, METAL_OP_SEND, result, saved_errno);
}

static ssize_t
metal_sendmsg(SocketTransport *t, const struct msghdr *message)
{
    if (t->metal == NULL) {
        ssize_t result;
        do {
            result = sendmsg(t->fd, message, 0);
        } while (result < 0 && errno == EINTR);
        return result;
    }
    uint64_t token = metal_begin_operation(t, METAL_OP_SEND);
    if (token == 0) {
        return -1;
    }
    ssize_t result;
    int saved_errno;
    if (t->metal->io_backend == METAL_IO_URING) {
        uint64_t completed_token = 0;
        int operation_result = 0;
        if (metal_uring_submit(&t->metal->uring, IORING_OP_SENDMSG, t->fd,
                               message, 1, token, &completed_token,
                               &operation_result) < 0) {
            result = -1;
            saved_errno = errno;
        } else if (completed_token != token) {
            t->metal->operations.stale++;
            result = -1;
            saved_errno = ECANCELED;
        } else {
            result = operation_result < 0 ? -1 : operation_result;
            saved_errno = operation_result < 0 ? -operation_result : 0;
        }
    } else {
        do {
            result = sendmsg(t->fd, message, 0);
        } while (result < 0 && errno == EINTR);
        saved_errno = result < 0 ? errno : 0;
    }
    return metal_finish_operation(t, token, METAL_OP_SEND, result, saved_errno);
}

static Py_ssize_t st_wsize(SocketTransport *t)
{
    return PyByteArray_GET_SIZE(t->wbuf) - t->whead;
}

static PyObject *
st_bound(PyObject *obj, const char *name)
{
    return PyObject_GetAttrString(obj, name);
}

static int
st_call_soon(SocketTransport *t, PyObject *fn, PyObject *arg)
{
    PyObject *r;
    if (arg == NULL) {
        r = PyObject_CallOneArg(t->m_call_soon, fn);
    } else {
        r = PyObject_CallFunctionObjArgs(t->m_call_soon, fn, arg, NULL);
    }
    Py_XDECREF(r);
    return r == NULL ? -1 : 0;
}

static int
st_try_start_uring_receive(SocketTransport *t)
{
    if (t->poller_obj == NULL || t->metal == NULL ||
        !t->metal->receive_enabled) {
        return 0;
    }
    PyObject *result = PyObject_CallMethod(
        t->poller_obj, "_start_uring_receive", "O", (PyObject *)t);
    if (result == NULL) {
        return -1;
    }
    int started = PyObject_IsTrue(result);
    Py_DECREF(result);
    return started;
}

static int
st_try_stop_uring_receive(SocketTransport *t)
{
    if (t->poller_obj == NULL || !t->uring_receive_active) {
        return 0;
    }
    PyObject *result = PyObject_CallMethod(
        t->poller_obj, "_stop_uring_receive", "O", (PyObject *)t);
    if (result == NULL) {
        PyErr_Clear();
        return 1;
    }
    int stopped = PyObject_IsTrue(result);
    Py_DECREF(result);
    return stopped;
}

static void
st_maybe_pause(SocketTransport *t)
{
    if (t->protocol_paused || st_wsize(t) <= t->high_water) {
        return;
    }
    t->protocol_paused = 1;
    PyObject *r = PyObject_CallMethod(t->protocol, "pause_writing", NULL);
    if (r == NULL) {
        PyErr_WriteUnraisable(t->protocol);
    }
    Py_XDECREF(r);
}

static void
st_maybe_resume(SocketTransport *t)
{
    if (!t->protocol_paused || st_wsize(t) > t->low_water) {
        return;
    }
    t->protocol_paused = 0;
    PyObject *r = PyObject_CallMethod(t->protocol, "resume_writing", NULL);
    if (r == NULL) {
        PyErr_WriteUnraisable(t->protocol);
    }
    Py_XDECREF(r);
}

/* Force the connection closed after a fatal error. */
static void
st_force_close(SocketTransport *t, PyObject *exc)
{
    if (t->conn_lost) {
        return;
    }
    Py_CLEAR(t->cork_obj);
    if (st_wsize(t) > 0) {
        if (PyByteArray_Resize(t->wbuf, 0) == 0) {
            t->whead = 0;
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
    }
    if (!t->closing) {
        t->closing = 1;
        if (!st_try_stop_uring_receive(t)) {
            PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
            Py_XDECREF(r);
        }
    }
    t->conn_lost++;
    st_call_soon(t, t->conn_lost_cb, exc == NULL ? Py_None : exc);
}

static void
st_fatal(SocketTransport *t, const char *msg)
{
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        exc = PyObject_CallFunction(PyExc_OSError, "s", msg);
    }
    st_force_close(t, exc);
    Py_XDECREF(exc);
}

/* --- read path --- */
static PyObject *
st_on_eof(SocketTransport *t)
{
    PyObject *keep = PyObject_CallMethod(t->protocol, "eof_received", NULL);
    if (keep == NULL) {
        st_fatal(t, "eof_received() failed");
        Py_RETURN_NONE;
    }
    int keep_open = PyObject_IsTrue(keep);
    Py_DECREF(keep);
    if (keep_open) {
        if (!st_try_stop_uring_receive(t)) {
            PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
            Py_XDECREF(r);
        }
    } else {
        PyObject *c = PyObject_CallMethod((PyObject *)t, "close", NULL);
        Py_XDECREF(c);
    }
    Py_RETURN_NONE;
}

/* Flush the corked write buffer in a single send(); register the writer for any
 * remainder. Called after the synchronous request drive so a burst of small
 * writes (response head + streaming chunks) collapses into one syscall instead
 * of one per write. Leaves no pending exception (st_fatal consumes it). */
static void
st_flush_cork(SocketTransport *t)
{
    if (t->conn_lost) {
        return;
    }
    int pending_partial = 0;
    if (t->cork_obj != NULL) {
        const char *p = PyBytes_AS_STRING(t->cork_obj);
        Py_ssize_t size = PyBytes_GET_SIZE(t->cork_obj);
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                return;
            }
            n = 0;
        }
        if (n < size) {
            Py_ssize_t remaining = size - n;
            if (PyByteArray_Resize(t->wbuf, remaining) < 0) {
                st_fatal(t, "write buffer allocation failed");
                return;
            }
            memcpy(PyByteArray_AS_STRING(t->wbuf), p + n, (size_t)remaining);
            t->whead = 0;
            pending_partial = 1;
        }
        Py_CLEAR(t->cork_obj);
    }
    Py_ssize_t size = st_wsize(t);
    if (size > 0 && !pending_partial) {
        const char *p = PyByteArray_AS_STRING(t->wbuf) + t->whead;
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                return;
            }
            n = 0;
        }
        t->whead += n;
    }
    /* Same completion as st_write_ready: on full drain honour a pending close()
     * (schedule connection_lost -- a corked response followed by close() must
     * still terminate the connection) or half-close; on a partial write register
     * the writer so the rest drains. */
    if (st_wsize(t) == 0) {
        if (PyByteArray_Resize(t->wbuf, 0) == 0) {
            t->whead = 0;
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_maybe_resume(t);
        if (t->closing && !t->conn_lost) {
            t->conn_lost++;
            st_call_soon(t, t->conn_lost_cb, Py_None);
        } else if (t->eof) {
            shutdown(t->fd, SHUT_WR);
        }
    } else if (!t->writing) {
        PyObject *r = PyObject_CallFunction(t->m_add_writer, "iO", t->fd, t->write_ready);
        if (r == NULL) {
            st_fatal(t, "add_writer failed");
            return;
        }
        Py_DECREF(r);
        t->writing = 1;
    }
}

static int
st_deliver_received(SocketTransport *t, const char *data, Py_ssize_t size)
{
    Py_ssize_t offset = 0;
    if (t->fused_http1) {
        while (offset < size) {
            char *target;
            Py_ssize_t capacity;
            if (g_http1_capi->acquire_read_buffer(
                    t->protocol, &target, &capacity) < 0) {
                return -1;
            }
            if (capacity <= 0) {
                PyErr_SetString(PyExc_BufferError,
                                "native HTTP/1 returned an empty read buffer");
                return -1;
            }
            Py_ssize_t chunk = size - offset;
            if (chunk > capacity) {
                chunk = capacity;
            }
            memcpy(target, data + offset, (size_t)chunk);
            if (g_http1_capi->commit_read(t->protocol, chunk) < 0) {
                return -1;
            }
            offset += chunk;
            if (t->conn_lost) {
                break;
            }
        }
        return 0;
    }
    if (t->buffered) {
        while (offset < size) {
            PyObject *requested = PyLong_FromSsize_t(size - offset);
            if (requested == NULL) {
                return -1;
            }
            PyObject *buffer = PyObject_CallOneArg(t->proto_get_buffer, requested);
            Py_DECREF(requested);
            if (buffer == NULL) {
                return -1;
            }
            Py_buffer view;
            if (PyObject_GetBuffer(buffer, &view, PyBUF_WRITABLE) < 0) {
                Py_DECREF(buffer);
                return -1;
            }
            if (view.len <= 0) {
                PyBuffer_Release(&view);
                Py_DECREF(buffer);
                PyErr_SetString(PyExc_BufferError,
                                "get_buffer() returned an empty buffer");
                return -1;
            }
            Py_ssize_t chunk = size - offset;
            if (chunk > view.len) {
                chunk = view.len;
            }
            memcpy(view.buf, data + offset, (size_t)chunk);
            PyBuffer_Release(&view);
            PyObject *written = PyLong_FromSsize_t(chunk);
            PyObject *result = written != NULL
                ? PyObject_CallOneArg(t->proto_buffer_updated, written) : NULL;
            Py_XDECREF(written);
            Py_DECREF(buffer);
            if (result == NULL) {
                return -1;
            }
            Py_DECREF(result);
            offset += chunk;
            if (t->conn_lost) {
                break;
            }
        }
        return 0;
    }
    PyObject *bytes = PyBytes_FromStringAndSize(data, size);
    if (bytes == NULL) {
        return -1;
    }
    PyObject *result = PyObject_CallOneArg(t->proto_data_received, bytes);
    Py_DECREF(bytes);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static PyObject *
st_read_ready(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->conn_lost) {
        Py_RETURN_NONE;
    }
    /* Cork writes for the duration of the synchronous request drive: a burst of
     * small writes (response head + streaming chunks, and any pipelined replies
     * across the drain loop) accumulates in the write buffer and leaves in one
     * send() at `done`, instead of a syscall per write. Inline handlers finish
     * entirely inside this call, so this coalesces without any deferral. */
    t->cork = 1;
    for (int drain = 0; drain < ST_MAX_DRAIN; drain++) {
        if (t->fused_http1) {
            char *buffer;
            Py_ssize_t capacity;
            if (g_http1_capi->acquire_read_buffer(
                    t->protocol, &buffer, &capacity) < 0) {
                st_fatal(t, "native HTTP/1 get_buffer failed");
                goto done;
            }
            ssize_t n = metal_recv(t, buffer, (size_t)capacity);
            if (n < 0) {
                int saved_errno = errno;
                if (g_http1_capi->commit_read(t->protocol, 0) < 0) {
                    PyErr_Clear();
                }
                errno = saved_errno;
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                if (g_http1_capi->commit_read(t->protocol, 0) < 0) {
                    st_fatal(t, "native HTTP/1 commit failed");
                    goto done;
                }
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            if (g_http1_capi->commit_read(t->protocol, n) < 0) {
                st_fatal(t, "native HTTP/1 commit failed");
                goto done;
            }
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if (n < capacity) {
                goto done;
            }
        } else if (t->buffered) {
            PyObject *minus1 = PyLong_FromLong(-1);
            PyObject *buf = PyObject_CallOneArg(t->proto_get_buffer, minus1);
            Py_DECREF(minus1);
            if (buf == NULL) {
                st_fatal(t, "get_buffer() failed");
                goto done;
            }
            Py_buffer view;
            if (PyObject_GetBuffer(buf, &view, PyBUF_WRITABLE) < 0) {
                Py_DECREF(buf);
                st_fatal(t, "get_buffer() returned a non-writable buffer");
                goto done;
            }
            ssize_t n = metal_recv(t, view.buf, (size_t)view.len);
            Py_ssize_t cap = view.len;
            PyBuffer_Release(&view);
            /* Keep `buf` (the protocol's memoryview) alive across buffer_updated:
             * releasing it clears the protocol's read offer, exactly as asyncio's
             * recv_into path does by holding the buffer local. */
            if (n < 0) {
                Py_DECREF(buf);
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                Py_DECREF(buf);
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            PyObject *nb = PyLong_FromSsize_t(n);
            PyObject *r = PyObject_CallOneArg(t->proto_buffer_updated, nb);
            Py_DECREF(nb);
            Py_DECREF(buf);
            if (r == NULL) {
                st_fatal(t, "buffer_updated() failed");
                goto done;
            }
            Py_DECREF(r);
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if (n < cap) {
                goto done;  /* short read: socket drained */
            }
        } else {
            char stackbuf[ST_DATA_RECV];
            ssize_t n = metal_recv(t, stackbuf, sizeof(stackbuf));
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            PyObject *data = PyBytes_FromStringAndSize(stackbuf, n);
            if (data == NULL) {
                st_fatal(t, "oom");
                goto done;
            }
            PyObject *r = PyObject_CallOneArg(t->proto_data_received, data);
            Py_DECREF(data);
            if (r == NULL) {
                st_fatal(t, "data_received() failed");
                goto done;
            }
            Py_DECREF(r);
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if ((size_t)n < sizeof(stackbuf)) {
                goto done;
            }
        }
    }
done:
    t->cork = 0;
    st_flush_cork(t);
    Py_RETURN_NONE;
}

/* --- write path --- */
static PyObject *
st_write_ready(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->conn_lost) {
        Py_RETURN_NONE;
    }
    Py_ssize_t size = st_wsize(t);
    if (size > 0) {
        const char *p = PyByteArray_AS_STRING(t->wbuf) + t->whead;
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
            }
            Py_RETURN_NONE;
        }
        t->whead += n;
    }
    if (st_wsize(t) == 0) {
        if (PyByteArray_Resize(t->wbuf, 0) == 0) {
            t->whead = 0;
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_maybe_resume(t);
        if (t->closing) {
            t->conn_lost++;
            st_call_soon(t, t->conn_lost_cb, Py_None);
        } else if (t->eof) {
            shutdown(t->fd, SHUT_WR);
        }
    } else {
        st_maybe_resume(t);
    }
    Py_RETURN_NONE;
}

static PyObject *
st_write(PyObject *op, PyObject *data)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_buffer view;
    if (PyObject_GetBuffer(data, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (view.len == 0 || t->conn_lost || t->eof) {
        if (t->conn_lost) {
            t->conn_lost++;
        }
        PyBuffer_Release(&view);
        Py_RETURN_NONE;
    }
    if (t->cork && t->cork_obj == NULL && st_wsize(t) == 0 &&
        PyBytes_CheckExact(data)) {
        t->cork_obj = Py_NewRef(data);
        t->zero_copy_cork_writes++;
        PyBuffer_Release(&view);
        Py_RETURN_NONE;
    }
    if (t->cork_obj != NULL) {
        Py_ssize_t pending = PyBytes_GET_SIZE(t->cork_obj);
        if (PyByteArray_Resize(t->wbuf, pending) < 0) {
            PyBuffer_Release(&view);
            return NULL;
        }
        memcpy(PyByteArray_AS_STRING(t->wbuf),
               PyBytes_AS_STRING(t->cork_obj), (size_t)pending);
        Py_CLEAR(t->cork_obj);
        t->whead = 0;
    }
    Py_ssize_t off = 0;
    if (!t->cork && st_wsize(t) == 0) {
        /* nothing buffered: try to send straight away */
        ssize_t n = metal_send(t, view.buf, (size_t)view.len);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyBuffer_Release(&view);
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                Py_RETURN_NONE;
            }
            n = 0;
        }
        off = n;
        if (off == view.len) {
            PyBuffer_Release(&view);
            Py_RETURN_NONE;  /* fully sent, nothing buffered */
        }
        if (!t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_add_writer, "iO", t->fd, t->write_ready);
            if (r == NULL) {
                PyBuffer_Release(&view);
                st_fatal(t, "add_writer failed");
                Py_RETURN_NONE;
            }
            Py_DECREF(r);
            t->writing = 1;
        }
    }
    /* Reclaim the dead prefix only once it has grown to at least the live size.
     * Compacting on every write is O(n) per append -- quadratic for a stream of
     * writes under sustained backpressure. Gating on whead >= live bounds the
     * wasted space to 2x the live bytes and makes compaction amortized O(1). */
    if (t->whead > 0 && t->whead >= st_wsize(t)) {
        Py_ssize_t live = st_wsize(t);
        memmove(PyByteArray_AS_STRING(t->wbuf),
                PyByteArray_AS_STRING(t->wbuf) + t->whead, (size_t)live);
        if (PyByteArray_Resize(t->wbuf, live) == 0) {
            t->whead = 0;
        }
    }
    Py_ssize_t old = PyByteArray_GET_SIZE(t->wbuf);
    Py_ssize_t add = view.len - off;
    if (PyByteArray_Resize(t->wbuf, old + add) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    memcpy(PyByteArray_AS_STRING(t->wbuf) + old, (const char *)view.buf + off, (size_t)add);
    PyBuffer_Release(&view);
    /* Bound corked memory: a large inline burst flushes mid-way instead of
     * accumulating the whole response before the single post-drive send(). */
    if (t->cork && st_wsize(t) >= ST_CORK_MAX) {
        st_flush_cork(t);
    }
    st_maybe_pause(t);
    Py_RETURN_NONE;
}

static PyObject *
st_writelines(PyObject *op, PyObject *seq)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *parts = PySequence_Fast(seq, "writelines() needs an iterable");
    if (parts == NULL) {
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(parts);

    /* Large response head+body pairs can leave directly as one writev-shaped
     * syscall. This avoids copying an immutable response body into bytearray.
     * Keep the vector stack-bounded; arbitrary iterables use the normal path. */
    if (count >= 2 && count <= 16 && st_wsize(t) == 0 && !t->conn_lost && !t->eof) {
        struct iovec iov[16];
        Py_buffer views[16];
        Py_ssize_t acquired = 0;
        Py_ssize_t total = 0;
        for (; acquired < count; acquired++) {
            PyObject *item = PySequence_Fast_GET_ITEM(parts, acquired);
            if (PyObject_GetBuffer(item, &views[acquired], PyBUF_SIMPLE) < 0) {
                for (Py_ssize_t i = 0; i < acquired; i++) {
                    PyBuffer_Release(&views[i]);
                }
                Py_DECREF(parts);
                return NULL;
            }
            iov[acquired].iov_base = views[acquired].buf;
            iov[acquired].iov_len = (size_t)views[acquired].len;
            total += views[acquired].len;
        }
        if (total >= 16384) {
            struct msghdr msg;
            memset(&msg, 0, sizeof(msg));
            msg.msg_iov = iov;
            msg.msg_iovlen = (size_t)count;
            ssize_t sent = metal_sendmsg(t, &msg);
            if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                sent = 0;
            } else if (sent < 0) {
                for (Py_ssize_t i = 0; i < acquired; i++) {
                    PyBuffer_Release(&views[i]);
                }
                Py_DECREF(parts);
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                Py_RETURN_NONE;
            }
            t->direct_writelines++;
            Py_ssize_t remaining = total - sent;
            if (remaining > 0) {
                if (PyByteArray_Resize(t->wbuf, remaining) < 0) {
                    for (Py_ssize_t i = 0; i < acquired; i++) {
                        PyBuffer_Release(&views[i]);
                    }
                    Py_DECREF(parts);
                    return NULL;
                }
                char *dst = PyByteArray_AS_STRING(t->wbuf);
                Py_ssize_t skip = sent;
                for (Py_ssize_t i = 0; i < count; i++) {
                    Py_ssize_t part = views[i].len;
                    if (skip >= part) {
                        skip -= part;
                        continue;
                    }
                    Py_ssize_t copy = part - skip;
                    memcpy(dst, (char *)views[i].buf + skip, (size_t)copy);
                    dst += copy;
                    skip = 0;
                }
                if (!t->writing) {
                    PyObject *r = PyObject_CallFunction(
                        t->m_add_writer, "iO", t->fd, t->write_ready);
                    if (r == NULL) {
                        for (Py_ssize_t i = 0; i < acquired; i++) {
                            PyBuffer_Release(&views[i]);
                        }
                        Py_DECREF(parts);
                        st_fatal(t, "add_writer failed");
                        Py_RETURN_NONE;
                    }
                    Py_DECREF(r);
                    t->writing = 1;
                }
                st_maybe_pause(t);
            }
            for (Py_ssize_t i = 0; i < acquired; i++) {
                PyBuffer_Release(&views[i]);
            }
            Py_DECREF(parts);
            Py_RETURN_NONE;
        }
        for (Py_ssize_t i = 0; i < acquired; i++) {
            PyBuffer_Release(&views[i]);
        }
    }

    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *r = st_write(op, PySequence_Fast_GET_ITEM(parts, i));
        if (r == NULL) {
            Py_DECREF(parts);
            return NULL;
        }
        Py_DECREF(r);
    }
    Py_DECREF(parts);
    Py_RETURN_NONE;
}

static PyObject *
st_close(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing) {
        Py_RETURN_NONE;
    }
    t->closing = 1;
    if (!t->reading_paused && !st_try_stop_uring_receive(t)) {
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
    }
    if (st_wsize(t) == 0 && t->cork_obj == NULL) {
        t->conn_lost++;
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_call_soon(t, t->conn_lost_cb, Py_None);
    }
    Py_RETURN_NONE;
}

static PyObject *
st_abort(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    st_force_close((SocketTransport *)op, NULL);
    Py_RETURN_NONE;
}

static PyObject *
st_call_connection_lost(PyObject *op, PyObject *exc)
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->protocol_connected) {
        PyObject *e = (exc == Py_None) ? Py_None : exc;
        PyObject *r = PyObject_CallMethod(t->protocol, "connection_lost", "O", e);
        if (r == NULL) {
            PyErr_WriteUnraisable(t->protocol);
        }
        Py_XDECREF(r);
    }
    if (t->sock != NULL) {
        PyObject *r = PyObject_CallMethod(t->sock, "close", NULL);
        Py_XDECREF(r);
    }
    if (t->server != NULL && t->server != Py_None) {
        PyObject *r = PyObject_CallMethod(t->server, "_detach", "O", op);
        if (r == NULL) {
            PyErr_Clear();  /* older/newer servers may not expose _detach */
        }
        Py_XDECREF(r);
    }
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    metal_detach_transport(t);
    Py_RETURN_NONE;
}

static PyObject *
st_start_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || t->reading_paused) {
        Py_RETURN_NONE;
    }
    int uring_started = st_try_start_uring_receive(t);
    if (uring_started < 0) {
        st_fatal(t, "starting io_uring receive failed");
        Py_RETURN_NONE;
    }
    if (!uring_started) {
        PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

static PyObject *
st_pause_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || t->reading_paused) {
        Py_RETURN_NONE;
    }
    t->reading_paused = 1;
    if (!st_try_stop_uring_receive(t)) {
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

static PyObject *
st_resume_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || !t->reading_paused) {
        Py_RETURN_NONE;
    }
    t->reading_paused = 0;
    int uring_started = st_try_start_uring_receive(t);
    if (uring_started < 0) {
        st_fatal(t, "resuming io_uring receive failed");
    } else if (!uring_started) {
        PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

static PyObject *
st_get_extra_info(PyObject *op, PyObject *args)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *name, *dflt = Py_None;
    if (!PyArg_ParseTuple(args, "O|O", &name, &dflt)) {
        return NULL;
    }
    PyObject *v = PyDict_GetItemWithError(t->extra, name);
    if (v != NULL) {
        return Py_NewRef(v);
    }
    if (PyErr_Occurred()) {
        return NULL;
    }
    return Py_NewRef(dflt);
}

static PyObject *
st_is_closing(PyObject *op, PyObject *Py_UNUSED(i))
{
    return PyBool_FromLong(((SocketTransport *)op)->closing);
}

static PyObject *
st_is_reading(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyBool_FromLong(!t->closing && !t->reading_paused);
}

static PyObject *
st_get_protocol(PyObject *op, PyObject *Py_UNUSED(i))
{
    PyObject *p = ((SocketTransport *)op)->protocol;
    return Py_NewRef(p ? p : Py_None);
}

static void st_bind_protocol_methods(SocketTransport *t);

static PyObject *
st_set_protocol(PyObject *op, PyObject *proto)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_XSETREF(t->protocol, Py_NewRef(proto));
    st_bind_protocol_methods(t);
    Py_RETURN_NONE;
}

static PyObject *
st_get_write_buffer_size(PyObject *op, PyObject *Py_UNUSED(i))
{
    return PyLong_FromSsize_t(st_wsize((SocketTransport *)op));
}

static PyObject *
st_get_write_buffer_limits(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    return Py_BuildValue("nn", t->low_water, t->high_water);
}

static PyObject *
st_set_write_buffer_limits(PyObject *op, PyObject *args, PyObject *kwds)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *high = Py_None, *low = Py_None;
    static char *kw[] = {"high", "low", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|OO", kw, &high, &low)) {
        return NULL;
    }
    Py_ssize_t h = (high == Py_None) ? -1 : PyLong_AsSsize_t(high);
    Py_ssize_t lo = (low == Py_None) ? -1 : PyLong_AsSsize_t(low);
    if (h < 0) {
        h = (lo >= 0) ? lo * 4 : 65536;
    }
    if (lo < 0) {
        lo = h / 4;
    }
    if (lo > h) {
        PyErr_SetString(PyExc_ValueError, "high must be >= low");
        return NULL;
    }
    t->high_water = h;
    t->low_water = lo;
    st_maybe_pause(t);
    Py_RETURN_NONE;
}

static PyObject *
st_can_write_eof(PyObject *op, PyObject *Py_UNUSED(i))
{
    Py_RETURN_TRUE;
}

static PyObject *
st_write_eof(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->eof || t->conn_lost) {
        Py_RETURN_NONE;
    }
    t->eof = 1;
    if (st_wsize(t) == 0) {
        shutdown(t->fd, SHUT_WR);
    }
    Py_RETURN_NONE;
}

static PyMethodDef st_methods[] = {
    {"write", st_write, METH_O, NULL},
    {"writelines", st_writelines, METH_O, NULL},
    {"close", st_close, METH_NOARGS, NULL},
    {"abort", st_abort, METH_NOARGS, NULL},
    {"get_extra_info", st_get_extra_info, METH_VARARGS, NULL},
    {"is_closing", st_is_closing, METH_NOARGS, NULL},
    {"is_reading", st_is_reading, METH_NOARGS, NULL},
    {"pause_reading", st_pause_reading, METH_NOARGS, NULL},
    {"resume_reading", st_resume_reading, METH_NOARGS, NULL},
    {"get_protocol", st_get_protocol, METH_NOARGS, NULL},
    {"set_protocol", st_set_protocol, METH_O, NULL},
    {"get_write_buffer_size", st_get_write_buffer_size, METH_NOARGS, NULL},
    {"get_write_buffer_limits", st_get_write_buffer_limits, METH_NOARGS, NULL},
    {"set_write_buffer_limits", (PyCFunction)(void (*)(void))st_set_write_buffer_limits,
     METH_VARARGS | METH_KEYWORDS, NULL},
    {"can_write_eof", st_can_write_eof, METH_NOARGS, NULL},
    {"write_eof", st_write_eof, METH_NOARGS, NULL},
    /* internal callbacks (scheduled onto the loop) */
    {"_read_ready", st_read_ready, METH_NOARGS, NULL},
    {"_write_ready", st_write_ready, METH_NOARGS, NULL},
    {"_start_reading", st_start_reading, METH_NOARGS, NULL},
    {"_call_connection_lost", st_call_connection_lost, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static void
st_bind_protocol_methods(SocketTransport *t)
{
    Py_CLEAR(t->proto_get_buffer);
    Py_CLEAR(t->proto_buffer_updated);
    Py_CLEAR(t->proto_data_received);
    load_http1_capi();
    t->fused_http1 = g_http1_capi != NULL &&
                     g_http1_capi->version == WREATH_HTTP1_CAPI_VERSION &&
                     g_http1_capi->check(t->protocol);
    if (t->fused_http1) {
        t->buffered = 1;
        return;
    }
    PyObject *bp = PyObject_GetAttrString(
        PyImport_AddModule("asyncio.protocols"), "BufferedProtocol");
    t->buffered = bp != NULL && PyObject_IsInstance(t->protocol, bp) == 1;
    Py_XDECREF(bp);
    if (PyErr_Occurred()) {
        PyErr_Clear();
        t->buffered = 0;
    }
    if (t->buffered) {
        t->proto_get_buffer = st_bound(t->protocol, "get_buffer");
        t->proto_buffer_updated = st_bound(t->protocol, "buffer_updated");
    } else {
        t->proto_data_received = st_bound(t->protocol, "data_received");
    }
}

static int
st_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *loop, *sock, *protocol, *waiter = Py_None, *extra = Py_None, *server = Py_None;
    static char *kw[] = {"loop", "sock", "protocol", "waiter", "extra", "server", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OOO|OOO", kw, &loop, &sock,
                                     &protocol, &waiter, &extra, &server)) {
        return -1;
    }
    t->loop = Py_NewRef(loop);
    t->sock = Py_NewRef(sock);
    t->protocol = Py_NewRef(protocol);
    t->poller_obj = NULL;
    t->metal = NULL;
    t->connection_token = 0;
    t->uring_receive_active = 0;
    t->uring_receive_multishot = 0;
    PyObject *poller_obj = PyObject_GetAttrString(loop, "_poller");
    if (poller_obj == NULL) {
        PyErr_Clear();
    } else {
        if (poller_obj != Py_None && metal_attach_transport(t, poller_obj) < 0) {
            Py_DECREF(poller_obj);
            return -1;
        }
        Py_DECREF(poller_obj);
    }
    t->server = Py_NewRef(server);
    t->extra = (extra == Py_None) ? PyDict_New() : PyDict_Copy(extra);
    if (t->extra == NULL) {
        return -1;
    }
    PyObject *fdobj = PyObject_CallMethod(sock, "fileno", NULL);
    if (fdobj == NULL) {
        return -1;
    }
    t->fd = (int)PyLong_AsLong(fdobj);
    Py_DECREF(fdobj);
    if (t->fd < 0 && PyErr_Occurred()) {
        return -1;
    }
    /* TCP_NODELAY (best effort) */
    int one = 1;
    setsockopt(t->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    t->m_add_reader = st_bound(loop, "_add_reader");
    t->m_remove_reader = st_bound(loop, "_remove_reader");
    t->m_add_writer = st_bound(loop, "_add_writer");
    t->m_remove_writer = st_bound(loop, "_remove_writer");
    t->m_call_soon = st_bound(loop, "call_soon");
    t->read_ready = st_bound(op, "_read_ready");
    t->write_ready = st_bound(op, "_write_ready");
    t->conn_lost_cb = st_bound(op, "_call_connection_lost");
    if (!t->m_add_reader || !t->m_remove_reader || !t->m_add_writer ||
        !t->m_remove_writer || !t->m_call_soon || !t->read_ready ||
        !t->write_ready || !t->conn_lost_cb) {
        return -1;
    }

    t->wbuf = PyByteArray_FromStringAndSize("", 0);
    if (t->wbuf == NULL) {
        return -1;
    }
    t->whead = 0;
    t->cork_obj = NULL;
    t->writing = 0;
    t->cork = 0;
    t->direct_writelines = 0;
    t->direct_read_dispatches = 0;
    t->direct_protocol_writes = 0;
    t->zero_copy_cork_writes = 0;
    t->high_water = 65536;
    t->low_water = 16384;
    t->protocol_paused = 0;
    t->reading_paused = 0;
    t->closing = 0;
    t->conn_lost = 0;
    t->eof = 0;
    t->protocol_connected = 1;

    st_bind_protocol_methods(t);

    /* populate get_extra_info like asyncio does */
    if (PyDict_SetItemString(t->extra, "socket", sock) < 0) {
        return -1;
    }
    PyObject *sn = PyObject_CallMethod(sock, "getsockname", NULL);
    if (sn != NULL) {
        if (PyDict_SetItemString(t->extra, "sockname", sn) < 0) {
            Py_DECREF(sn);
            return -1;
        }
        Py_DECREF(sn);
    } else {
        PyErr_Clear();
    }
    if (PyDict_GetItemString(t->extra, "peername") == NULL) {
        PyObject *pn = PyObject_CallMethod(sock, "getpeername", NULL);
        if (pn != NULL) {
            if (PyDict_SetItemString(t->extra, "peername", pn) < 0) {
                Py_DECREF(pn);
                return -1;
            }
            Py_DECREF(pn);
        } else {
            PyErr_Clear();
        }
    }

    /* register with the server so shutdown accounting can close us */
    if (t->server != NULL && t->server != Py_None) {
        PyObject *r = PyObject_CallMethod(t->server, "_attach", "O", op);
        if (r == NULL) {
            PyErr_Clear();
        } else {
            Py_DECREF(r);
        }
    }

    /* schedule connection_made, then start reading, then wake the waiter */
    PyObject *cm = st_bound(protocol, "connection_made");
    if (cm != NULL) {
        st_call_soon(t, cm, op);
        Py_DECREF(cm);
    }
    PyObject *sr = st_bound(op, "_start_reading");
    if (sr != NULL) {
        st_call_soon(t, sr, NULL);
        Py_DECREF(sr);
    }
    if (waiter != Py_None) {
        PyObject *setres = PyObject_GetAttrString(
            PyImport_AddModule("asyncio.futures"), "_set_result_unless_cancelled");
        if (setres != NULL) {
            PyObject *r = PyObject_CallFunctionObjArgs(t->m_call_soon, setres, waiter, Py_None, NULL);
            Py_XDECREF(r);
            Py_DECREF(setres);
        }
    }
    return 0;
}

static int
st_traverse(PyObject *op, visitproc visit, void *arg)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_VISIT(t->loop);
    Py_VISIT(t->poller_obj);
    Py_VISIT(t->sock);
    Py_VISIT(t->protocol);
    Py_VISIT(t->server);
    Py_VISIT(t->extra);
    Py_VISIT(t->wbuf);
    Py_VISIT(t->cork_obj);
    /* The bound methods below close a reference cycle back to the transport
     * (read_ready/write_ready/conn_lost_cb are bound to self); GC must see them
     * or a closed connection is never collected. */
    Py_VISIT(t->m_add_reader);
    Py_VISIT(t->m_remove_reader);
    Py_VISIT(t->m_add_writer);
    Py_VISIT(t->m_remove_writer);
    Py_VISIT(t->m_call_soon);
    Py_VISIT(t->proto_get_buffer);
    Py_VISIT(t->proto_buffer_updated);
    Py_VISIT(t->proto_data_received);
    Py_VISIT(t->read_ready);
    Py_VISIT(t->write_ready);
    Py_VISIT(t->conn_lost_cb);
    return 0;
}

static int
st_clear(PyObject *op)
{
    SocketTransport *t = (SocketTransport *)op;
    metal_detach_transport(t);
    t->metal = NULL;
    Py_CLEAR(t->poller_obj);
    Py_CLEAR(t->loop);
    Py_CLEAR(t->sock);
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    Py_CLEAR(t->extra);
    Py_CLEAR(t->wbuf);
    Py_CLEAR(t->cork_obj);
    Py_CLEAR(t->m_add_reader);
    Py_CLEAR(t->m_remove_reader);
    Py_CLEAR(t->m_add_writer);
    Py_CLEAR(t->m_remove_writer);
    Py_CLEAR(t->m_call_soon);
    Py_CLEAR(t->proto_get_buffer);
    Py_CLEAR(t->proto_buffer_updated);
    Py_CLEAR(t->proto_data_received);
    Py_CLEAR(t->read_ready);
    Py_CLEAR(t->write_ready);
    Py_CLEAR(t->conn_lost_cb);
    return 0;
}

static void
st_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    st_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
st_fused_http1_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(((SocketTransport *)op)->fused_http1);
}

static PyObject *
st_direct_writelines_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_writelines);
}

static int
transport_capi_check(PyObject *op)
{
    return PyObject_TypeCheck(op, &SocketTransportType);
}

static int
transport_capi_write(PyObject *op, PyObject *data)
{
    PyObject *result = st_write(op, data);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    ((SocketTransport *)op)->direct_protocol_writes++;
    return 0;
}

static int
transport_capi_writelines(PyObject *op, PyObject *parts)
{
    PyObject *result = st_writelines(op, parts);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    ((SocketTransport *)op)->direct_protocol_writes++;
    return 0;
}

static PyObject *
st_direct_read_dispatches_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_read_dispatches);
}

static PyObject *
st_direct_protocol_writes_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_protocol_writes);
}

static PyObject *
st_zero_copy_cork_writes_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->zero_copy_cork_writes);
}

static PyObject *
st_metal_connection_token_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((SocketTransport *)op)->connection_token);
}

static PyObject *
st_metal_submissions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(t->metal != NULL ? t->metal->submissions : 0);
}

static PyObject *
st_metal_completions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(t->metal != NULL ? t->metal->completions : 0);
}

static PyObject *
st_metal_operation_high_water_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLong(
        t->metal != NULL ? t->metal->operations.high_water : 0
    );
}

static PyObject *
st_metal_operation_exhaustions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL ? t->metal->operations.exhaustions : 0
    );
}

static PyObject *
st_metal_worker_id_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLong(t->metal != NULL ? t->metal->worker_id : 0);
}

static PyObject *
st_metal_cross_worker_rejections_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL ? t->metal->cross_worker_rejections : 0
    );
}

static PyObject *
st_metal_io_backend_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    const char *name = t->metal != NULL &&
                       t->metal->io_backend == METAL_IO_URING
                       ? "io_uring" : "epoll";
    return PyUnicode_FromString(name);
}

static PyGetSetDef st_getset[] = {
    {"_fused_http1", st_fused_http1_get, NULL,
     "whether ingress uses the private native HTTP/1 C API", NULL},
    {"_direct_writelines", st_direct_writelines_get, NULL,
     "number of large writelines emitted through sendmsg", NULL},
    {"_direct_read_dispatches", st_direct_read_dispatches_get, NULL,
     "number of readiness callbacks dispatched as direct C calls", NULL},
    {"_direct_protocol_writes", st_direct_protocol_writes_get, NULL,
     "number of protocol writes entering through the transport C API", NULL},
    {"_zero_copy_cork_writes", st_zero_copy_cork_writes_get, NULL,
     "number of immutable writes retained for direct post-drive send", NULL},
    {"_metal_connection_token", st_metal_connection_token_get, NULL,
     "generation-validated metal connection handle", NULL},
    {"_metal_submissions", st_metal_submissions_get, NULL,
     "socket operations submitted through the metal backend", NULL},
    {"_metal_completions", st_metal_completions_get, NULL,
     "normalized metal completions consumed", NULL},
    {"_metal_operation_high_water", st_metal_operation_high_water_get, NULL,
     "operation slab high-water occupancy", NULL},
    {"_metal_operation_exhaustions", st_metal_operation_exhaustions_get, NULL,
     "operation submissions rejected by bounded slab exhaustion", NULL},
    {"_metal_worker_id", st_metal_worker_id_get, NULL,
     "owner worker for this metal connection", NULL},
    {"_metal_cross_worker_rejections", st_metal_cross_worker_rejections_get, NULL,
     "operations rejected outside the owner thread", NULL},
    {"_metal_io_backend", st_metal_io_backend_get, NULL,
     "metal socket operation backend", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject SocketTransportType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.SocketTransport",
    .tp_basicsize = sizeof(SocketTransport),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = st_dealloc,
    .tp_traverse = st_traverse,
    .tp_clear = st_clear,
    .tp_methods = st_methods,
    .tp_getset = st_getset,
    .tp_init = st_init,
    .tp_new = PyType_GenericNew,
};

/* ======================================================================== *
 *  ReactorPoller — the native run loop core.                               *
 *                                                                          *
 *  Metal's EventLoop rebinds its own _add_reader / _add_writer /          *
 *  _remove_reader / _remove_writer / _run_once to this object's C          *
 *  methods. It owns an epoll fd and an                                     *
 *  fd-indexed registry of reader/writer callables, so a readable socket    *
 *  dispatches the transport's C _read_ready DIRECTLY: no selector.select   *
 *  wrapper, no _process_events, no per-event Handle allocation, no         *
 *  Handle._run/context.run. This is the layer uvloop has in libuv and the  *
 *  stock asyncio SelectorEventLoop pays for in Python on every iteration.  *
 *                                                                          *
 *  Reader/writer callbacks run WITHOUT a copied contextvars context: the   *
 *  native transport does not read contextvars, and metal is a controlled   *
 *  runtime. call_soon Handles and timers still run through their normal     *
 *  Handle._run (context-correct); only the fd-readiness fast path is bare. *
 * ======================================================================== */

/* cached, interned attribute names + heapq.heappop, set in PyInit */
static PyObject *g_s_when;       /* "_when"      */
static PyObject *g_s_run;        /* "_run"       */
static PyObject *g_s_context_run; /* "run"        */
static PyObject *g_s_cancelled;  /* "_cancelled" */
static PyObject *g_s_scheduled;  /* "_scheduled" */
static PyObject *g_s_popleft;    /* "popleft"    */
static PyObject *g_s_append;     /* "append"     */
static PyObject *g_heappop;      /* heapq.heappop */
static PyTypeObject *g_handle_type;
static PyMemberDef *g_handle_callback;
static PyMemberDef *g_handle_args;
static PyMemberDef *g_handle_cancelled;
static PyMemberDef *g_handle_context;
static PyMemberDef *g_handle_source_traceback;

typedef struct {
    PyObject *reader;       /* callable or NULL */
    PyObject *reader_args;  /* tuple, or NULL for no-arg fast call */
    PyObject *writer;
    PyObject *writer_args;
    PyObject *accept_callback;
    int native_reader;
    int native_writer;
    int accept_active;
    int accept_multishot;
    uint32_t mask;          /* epoll mask currently registered (0 => not in epoll) */
    uint32_t generation;    /* registration incarnation carried in data.u64 */
} FdEntry;

typedef struct {
    PyObject_HEAD
    int epfd;
    FdEntry *fds;
    int fdcap;
    PyObject *loop;         /* the EventLoop (for call_exception_handler) */
    PyObject *ready;        /* loop._ready (collections.deque) */
    PyObject *scheduled;    /* loop._scheduled (heapq list) */
    PyObject *exc_handler;  /* loop.call_exception_handler (bound) */
    PyObject *wheel_obj;    /* TimingWheel or None */
    TimingWheel *wheel;     /* borrowed from wheel_obj */
    struct epoll_event *evbuf;
    int evcap;
    double clock_res;       /* loop._clock_resolution */
    int direct_task_steps;  /* bypass Handle._run for C Task step callbacks */
    uint64_t stale_events;
    uint64_t generation_wraps;
    MetalRuntime metal;
} ReactorPoller;

static PyTypeObject ReactorPollerType;

static int
metal_attach_transport(SocketTransport *transport, PyObject *poller_obj)
{
    if (!PyObject_TypeCheck(poller_obj, &ReactorPollerType)) {
        return 0;
    }
    ReactorPoller *poller = (ReactorPoller *)poller_obj;
    uint64_t token = metal_slab_allocate(
        &poller->metal.connections, transport, 0, 0
    );
    if (token == 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "metal connection slab is exhausted");
        return -1;
    }
    transport->poller_obj = Py_NewRef(poller_obj);
    transport->metal = &poller->metal;
    transport->connection_token = token;
    return 0;
}

static void
metal_detach_transport(SocketTransport *transport)
{
    if (transport->metal != NULL && transport->connection_token != 0) {
        metal_slab_release(&transport->metal->connections,
                           transport->connection_token, transport);
    }
    transport->connection_token = 0;
}

static double mono_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* Grow the fd registry so index `fd` is valid, zeroing new slots. */
static int
rp_ensure_fd(ReactorPoller *p, int fd)
{
    if (fd < 0) {
        PyErr_SetString(PyExc_ValueError, "negative file descriptor");
        return -1;
    }
    if (fd < p->fdcap) {
        return 0;
    }
    int newcap = p->fdcap ? p->fdcap : 64;
    while (newcap <= fd) {
        newcap *= 2;
    }
    FdEntry *grown = PyMem_Realloc(p->fds, (size_t)newcap * sizeof(FdEntry));
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    memset(grown + p->fdcap, 0, (size_t)(newcap - p->fdcap) * sizeof(FdEntry));
    p->fds = grown;
    p->fdcap = newcap;
    return 0;
}

/* Reconcile one registration and publish a new {generation, fd} token. `force`
 * refreshes data.u64 when a callback changes without changing the readiness
 * mask, invalidating events already returned in the current epoll batch. */
static uint64_t
rp_next_listener_token(ReactorPoller *p, int fd)
{
    FdEntry *entry = &p->fds[fd];
    uint32_t generation = entry->generation + 1;
    if (generation == 0) {
        generation = 1;
        p->generation_wraps++;
    }
    entry->generation = generation;
    return ((uint64_t)generation << 32) | (uint32_t)fd;
}

static int
rp_submit_accept(ReactorPoller *p, int fd)
{
    FdEntry *entry = &p->fds[fd];
    uint64_t token = ((uint64_t)entry->generation << 32) | (uint32_t)fd;
    if (metal_uring_queue_accept(&p->metal.listener_uring, fd, token,
                                 entry->accept_multishot) < 0) {
        return -1;
    }
    p->metal.accept_submissions++;
    return 0;
}

static PyObject *
rp_add_uring_listener(PyObject *op, PyObject *args)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd;
    PyObject *callback;
    if (!PyArg_ParseTuple(args, "iO:_add_uring_listener", &fd, &callback)) {
        return NULL;
    }
    if (p->metal.io_backend != METAL_IO_URING) {
        PyErr_SetString(PyExc_RuntimeError,
                        "io_uring listener requires the io_uring backend");
        return NULL;
    }
    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "accept callback must be callable");
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    FdEntry *entry = &p->fds[fd];
    if (entry->accept_active) {
        PyErr_SetString(PyExc_RuntimeError, "listener is already registered");
        return NULL;
    }
    rp_next_listener_token(p, fd);
    entry->accept_callback = Py_NewRef(callback);
    entry->accept_active = 1;
    entry->accept_multishot = 1;
    if (rp_submit_accept(p, fd) < 0) {
        entry->accept_active = 0;
        Py_CLEAR(entry->accept_callback);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_remove_uring_listener(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd < 0 || fd >= p->fdcap || !p->fds[fd].accept_active) {
        Py_RETURN_FALSE;
    }
    FdEntry *entry = &p->fds[fd];
    entry->accept_active = 0;
    entry->accept_multishot = 0;
    rp_next_listener_token(p, fd);
    Py_CLEAR(entry->accept_callback);
    Py_RETURN_TRUE;
}

static PyObject *
rp_start_uring_receive(PyObject *op, PyObject *transport_obj)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (!PyObject_TypeCheck(transport_obj, &SocketTransportType)) {
        PyErr_SetString(PyExc_TypeError, "expected SocketTransport");
        return NULL;
    }
    SocketTransport *transport = (SocketTransport *)transport_obj;
    if (!p->metal.receive_enabled || transport->metal != &p->metal) {
        Py_RETURN_FALSE;
    }
    if (transport->uring_receive_active) {
        Py_RETURN_TRUE;
    }
    transport->uring_receive_active = 1;
    transport->uring_receive_multishot = 1;
    if (metal_uring_queue_receive(
            &p->metal.receive_uring, transport->fd,
            transport->connection_token, p->metal.receive_buffers.group_id,
            p->metal.receive_buffers.buffer_size, 1) < 0) {
        transport->uring_receive_active = 0;
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    p->metal.receive_submissions++;
    Py_RETURN_TRUE;
}

static PyObject *
rp_stop_uring_receive(PyObject *op, PyObject *transport_obj)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (!PyObject_TypeCheck(transport_obj, &SocketTransportType)) {
        PyErr_SetString(PyExc_TypeError, "expected SocketTransport");
        return NULL;
    }
    SocketTransport *transport = (SocketTransport *)transport_obj;
    if (!transport->uring_receive_active || transport->metal != &p->metal) {
        Py_RETURN_FALSE;
    }
    if (metal_uring_queue_cancel(&p->metal.receive_uring,
                                 transport->connection_token) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    transport->uring_receive_active = 0;
    Py_RETURN_TRUE;
}

static int
rp_submit_receive(ReactorPoller *p, SocketTransport *transport)
{
    if (metal_uring_queue_receive(
            &p->metal.receive_uring, transport->fd,
            transport->connection_token, p->metal.receive_buffers.group_id,
            p->metal.receive_buffers.buffer_size,
            transport->uring_receive_multishot) < 0) {
        return -1;
    }
    p->metal.receive_submissions++;
    return 0;
}

static int
rp_drain_receive_completions(ReactorPoller *p, unsigned budget)
{
    MetalUring *ring = &p->metal.receive_uring;
    MetalProvidedBuffers *buffers = &p->metal.receive_buffers;
    unsigned drained = 0;
    while (drained < budget) {
        unsigned head = __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
        unsigned tail = __atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE);
        if (head == tail) {
            break;
        }
        struct io_uring_cqe cqe = ring->cqes[head & *ring->cq_mask];
        __atomic_store_n(ring->cq_head, head + 1, __ATOMIC_RELEASE);
        drained++;
        if (cqe.user_data == 0) {
            continue;  /* completion for an explicit cancellation SQE */
        }
        p->metal.receive_completions++;
        int has_buffer = (cqe.flags & IORING_CQE_F_BUFFER) != 0;
        uint16_t buffer_id = (uint16_t)(cqe.flags >> IORING_CQE_BUFFER_SHIFT);
        uint32_t index = (uint32_t)cqe.user_data;
        uint32_t generation = (uint32_t)(cqe.user_data >> 32);
        MetalSlot *slot = index < p->metal.connections.capacity
            ? &p->metal.connections.slots[index] : NULL;
        if (slot == NULL || !slot->live || slot->generation != generation) {
            p->metal.receive_stale++;
            if (has_buffer && buffer_id < buffers->entries) {
                metal_provided_buffer_recycle(buffers, buffer_id);
                p->metal.provided_buffer_recycles++;
            }
            continue;
        }
        SocketTransport *transport = (SocketTransport *)slot->owner;
        Py_INCREF(transport);
        int has_more = (cqe.flags & IORING_CQE_F_MORE) != 0;
        if (cqe.res == -EINVAL && transport->uring_receive_multishot) {
            transport->uring_receive_multishot = 0;
            p->metal.receive_multishot_fallbacks++;
        } else if (cqe.res > 0 && has_buffer && buffer_id < buffers->entries) {
            if (!transport->conn_lost && !transport->closing) {
                transport->cork = 1;
                const char *data = buffers->data +
                                   (size_t)buffer_id * buffers->buffer_size;
                if (st_deliver_received(transport, data, cqe.res) < 0) {
                    st_fatal(transport, "io_uring receive delivery failed");
                }
                transport->cork = 0;
                st_flush_cork(transport);
            }
        } else if (cqe.res == 0) {
            transport->uring_receive_active = 0;
            if (!transport->conn_lost && !transport->closing) {
                PyObject *eof_result = st_on_eof(transport);
                Py_XDECREF(eof_result);
            }
        } else if (cqe.res > 0) {
            p->metal.receive_errors++;
            if (!transport->conn_lost && !transport->closing) {
                PyErr_SetString(PyExc_OSError,
                                "io_uring receive completed without a buffer");
                st_fatal(transport, "io_uring receive failed");
            }
        } else if (cqe.res != -ECANCELED && cqe.res != -ENOBUFS) {
            p->metal.receive_errors++;
            if (!transport->conn_lost && !transport->closing) {
                errno = -cqe.res;
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(transport, "io_uring receive failed");
            }
        }
        if (has_buffer && buffer_id < buffers->entries) {
            metal_provided_buffer_recycle(buffers, buffer_id);
            p->metal.provided_buffer_recycles++;
        }
        if (!has_more && transport->uring_receive_active &&
            !transport->reading_paused && !transport->closing &&
            !transport->conn_lost) {
            if (rp_submit_receive(p, transport) < 0) {
                Py_DECREF(transport);
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
        }
        Py_DECREF(transport);
    }
    return 0;
}

static int
rp_drain_accept_completions(ReactorPoller *p, unsigned budget)
{
    MetalUring *ring = &p->metal.listener_uring;
    unsigned drained = 0;
    while (drained < budget) {
        unsigned head = __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
        unsigned tail = __atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE);
        if (head == tail) {
            break;
        }
        struct io_uring_cqe cqe = ring->cqes[head & *ring->cq_mask];
        __atomic_store_n(ring->cq_head, head + 1, __ATOMIC_RELEASE);
        drained++;
        p->metal.accept_completions++;

        int fd = (int)(uint32_t)cqe.user_data;
        uint32_t generation = (uint32_t)(cqe.user_data >> 32);
        if (fd < 0 || fd >= p->fdcap ||
            p->fds[fd].generation != generation ||
            !p->fds[fd].accept_active) {
            p->metal.accept_stale++;
            if (cqe.res >= 0) {
                close(cqe.res);
            }
            continue;
        }
        FdEntry *entry = &p->fds[fd];
        int has_more = (cqe.flags & IORING_CQE_F_MORE) != 0;
        if (cqe.res == -EINVAL && entry->accept_multishot) {
            entry->accept_multishot = 0;
            p->metal.accept_multishot_fallbacks++;
        } else if (cqe.res >= 0) {
            PyObject *accepted_fd = PyLong_FromLong(cqe.res);
            PyObject *callback = Py_NewRef(entry->accept_callback);
            if (accepted_fd == NULL || callback == NULL) {
                Py_XDECREF(accepted_fd);
                Py_XDECREF(callback);
                close(cqe.res);
                return -1;
            }
            PyObject *result = PyObject_CallOneArg(callback, accepted_fd);
            Py_DECREF(callback);
            Py_DECREF(accepted_fd);
            if (result == NULL) {
                close(cqe.res);
                return -1;
            }
            Py_DECREF(result);
        } else if (cqe.res != -ECANCELED) {
            p->metal.accept_errors++;
        }

        entry = &p->fds[fd];
        if (entry->generation == generation && entry->accept_active && !has_more) {
            if (rp_submit_accept(p, fd) < 0) {
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
        }
    }
    return 0;
}

static int
rp_apply(ReactorPoller *p, int fd, uint32_t want, int force)
{
    FdEntry *e = &p->fds[fd];
    if (e->mask == want && !force) {
        return 0;
    }
    uint32_t generation = e->generation + 1;
    if (generation == 0) {
        generation = 1;
        p->generation_wraps++;
    }
    struct epoll_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.events = want;
    ev.data.u64 = ((uint64_t)generation << 32) | (uint32_t)fd;
    int ctl_op;
    if (want == 0) {
        ctl_op = EPOLL_CTL_DEL;
    } else if (e->mask == 0) {
        ctl_op = EPOLL_CTL_ADD;
    } else {
        ctl_op = EPOLL_CTL_MOD;
    }
    if (epoll_ctl(p->epfd, ctl_op, fd, want == 0 ? NULL : &ev) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    e->generation = generation;
    e->mask = want;
    return 0;
}

/* args==NULL means "no positional args" (bound methods with a captured self). */
static PyObject *
rp_pack_args(PyObject *const *args, Py_ssize_t n)
{
    if (n == 0) {
        return NULL;
    }
    PyObject *tuple = PyTuple_New(n);
    if (tuple == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyTuple_SET_ITEM(tuple, i, Py_NewRef(args[i]));
    }
    return tuple;
}

static PyObject *
rp_add_reader(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (nargs < 2) {
        PyErr_SetString(PyExc_TypeError, "_add_reader(fd, callback, *args)");
        return NULL;
    }
    int fd = (int)PyLong_AsLong(args[0]);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    PyObject *cb_args = rp_pack_args(args + 2, nargs - 2);
    if (nargs - 2 > 0 && cb_args == NULL) {
        return NULL;
    }
    FdEntry *e = &p->fds[fd];
    Py_XSETREF(e->reader, Py_NewRef(args[1]));
    Py_XSETREF(e->reader_args, cb_args);
    e->native_reader = PyCFunction_Check(args[1]) &&
        PyCFunction_GET_SELF(args[1]) != NULL &&
        PyObject_TypeCheck(PyCFunction_GET_SELF(args[1]), &SocketTransportType) &&
        PyCFunction_GET_FUNCTION(args[1]) == (PyCFunction)st_read_ready;
    if (rp_apply(p, fd, e->mask | EPOLLIN, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_add_writer(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (nargs < 2) {
        PyErr_SetString(PyExc_TypeError, "_add_writer(fd, callback, *args)");
        return NULL;
    }
    int fd = (int)PyLong_AsLong(args[0]);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    PyObject *cb_args = rp_pack_args(args + 2, nargs - 2);
    if (nargs - 2 > 0 && cb_args == NULL) {
        return NULL;
    }
    FdEntry *e = &p->fds[fd];
    Py_XSETREF(e->writer, Py_NewRef(args[1]));
    Py_XSETREF(e->writer_args, cb_args);
    e->native_writer = PyCFunction_Check(args[1]) &&
        PyCFunction_GET_SELF(args[1]) != NULL &&
        PyObject_TypeCheck(PyCFunction_GET_SELF(args[1]), &SocketTransportType) &&
        PyCFunction_GET_FUNCTION(args[1]) == (PyCFunction)st_write_ready;
    if (rp_apply(p, fd, e->mask | EPOLLOUT, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_remove_reader(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd >= p->fdcap || p->fds[fd].reader == NULL) {
        Py_RETURN_FALSE;
    }
    FdEntry *e = &p->fds[fd];
    if (rp_apply(p, fd, e->mask & ~(uint32_t)EPOLLIN, 0) < 0) {
        return NULL;
    }
    Py_CLEAR(e->reader);
    Py_CLEAR(e->reader_args);
    e->native_reader = 0;
    Py_RETURN_TRUE;
}

static PyObject *
rp_remove_writer(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd >= p->fdcap || p->fds[fd].writer == NULL) {
        Py_RETURN_FALSE;
    }
    FdEntry *e = &p->fds[fd];
    if (rp_apply(p, fd, e->mask & ~(uint32_t)EPOLLOUT, 0) < 0) {
        return NULL;
    }
    Py_CLEAR(e->writer);
    Py_CLEAR(e->writer_args);
    e->native_writer = 0;
    Py_RETURN_TRUE;
}

static void
rp_report_callback_error(ReactorPoller *p)
{
    /* Swallow into the loop's exception handler (never propagate out of poll). */
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        return;
    }
    PyObject *ctx = PyDict_New();
    PyObject *message = PyUnicode_FromString("Exception in callback");
    if (ctx != NULL && message != NULL &&
        PyDict_SetItemString(ctx, "message", message) == 0 &&
        PyDict_SetItemString(ctx, "exception", exc) == 0) {
        PyObject *hr = PyObject_CallOneArg(p->exc_handler, ctx);
        Py_XDECREF(hr);
    }
    Py_XDECREF(message);
    Py_XDECREF(ctx);
    if (PyErr_Occurred()) {
        PyErr_WriteUnraisable(p->exc_handler);
    }
    Py_DECREF(exc);
}

/* Call a readiness callback; on error route to loop.call_exception_handler,
 * matching asyncio's Handle._run so one bad callback cannot kill the loop. */
static void
rp_dispatch(ReactorPoller *p, PyObject *cb, PyObject *cb_args)
{
    PyObject *r = (cb_args == NULL) ? PyObject_CallNoArgs(cb)
                                    : PyObject_Call(cb, cb_args, NULL);
    if (r != NULL) {
        Py_DECREF(r);
        return;
    }
    rp_report_callback_error(p);
}

static void
rp_dispatch_native_transport(ReactorPoller *p, PyObject *cb, int reader)
{
    PyObject *transport = PyCFunction_GET_SELF(cb);
    PyObject *r;
    if (reader) {
        ((SocketTransport *)transport)->direct_read_dispatches++;
        r = st_read_ready(transport, NULL);
    } else {
        r = st_write_ready(transport, NULL);
    }
    if (r != NULL) {
        Py_DECREF(r);
    } else {
        rp_report_callback_error(p);
    }
}

static int
rp_report_task_step_error(ReactorPoller *p, PyObject *handle, PyObject *callback)
{
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "task step failed without an exception");
        return -1;
    }
    if (PyErr_GivenExceptionMatches(exc, PyExc_SystemExit) ||
            PyErr_GivenExceptionMatches(exc, PyExc_KeyboardInterrupt)) {
        PyErr_SetRaisedException(exc);
        return -1;
    }

    PyObject *message = PyUnicode_FromFormat("Exception in callback %R", callback);
    PyObject *context = message != NULL ? PyDict_New() : NULL;
    if (context == NULL ||
            PyDict_SetItemString(context, "message", message) < 0 ||
            PyDict_SetItemString(context, "exception", exc) < 0 ||
            PyDict_SetItemString(context, "handle", handle) < 0) {
        Py_XDECREF(context);
        Py_XDECREF(message);
        PyErr_SetRaisedException(exc);
        return -1;
    }
    Py_DECREF(message);

    PyObject *source = PyMember_GetOne(
        (const char *)handle, g_handle_source_traceback);
    if (source == NULL) {
        PyErr_Clear();
    } else {
        if (source != Py_None &&
                PyDict_SetItemString(context, "source_traceback", source) < 0) {
            Py_DECREF(source);
            Py_DECREF(context);
            PyErr_SetRaisedException(exc);
            return -1;
        }
        Py_DECREF(source);
    }
    Py_DECREF(exc);
    PyObject *result = PyObject_CallOneArg(p->exc_handler, context);
    Py_DECREF(context);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

/* asyncio's C Task schedules no-argument TaskStepMethWrapper callbacks. Going
 * through Handle._run adds a Python frame around every suspension/resume. For
 * that exact CPython 3.14 callback shape, enter the captured Context and invoke
 * the C task step directly. Every other Handle retains asyncio's own _run. */
static int
rp_run_task_step(ReactorPoller *p, PyObject *handle)
{
    if (!Py_IS_TYPE(handle, g_handle_type)) {
        return 0;
    }
    PyObject *callback = PyMember_GetOne((const char *)handle, g_handle_callback);
    if (callback == NULL) {
        return -1;
    }
    if (strcmp(Py_TYPE(callback)->tp_name, "_asyncio.TaskStepMethWrapper") != 0) {
        Py_DECREF(callback);
        return 0;
    }
    PyObject *args = PyMember_GetOne((const char *)handle, g_handle_args);
    if (args == NULL) {
        Py_DECREF(callback);
        return -1;
    }
    if (!PyTuple_CheckExact(args) || PyTuple_GET_SIZE(args) != 0) {
        Py_DECREF(args);
        Py_DECREF(callback);
        return 0;
    }
    PyObject *context = PyMember_GetOne((const char *)handle, g_handle_context);
    if (context == NULL) {
        Py_DECREF(args);
        Py_DECREF(callback);
        return -1;
    }

    PyObject *result = PyObject_CallMethodOneArg(context, g_s_context_run, callback);
    int status;
    if (result != NULL) {
        Py_DECREF(result);
        status = 1;
    } else {
        status = rp_report_task_step_error(p, handle, callback) < 0 ? -1 : 1;
    }
    Py_DECREF(context);
    Py_DECREF(args);
    Py_DECREF(callback);
    return status;
}

static PyObject *
rp_run_once(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ReactorPoller *p = (ReactorPoller *)op;
    double loop_now = mono_seconds();
    if (p->wheel != NULL && p->wheel->count > 0 &&
            wheel_run_due(p->wheel, loop_now) < 0) {
        return NULL;
    }
    int blocked = 0;

    /* --- 1. poll -------------------------------------------------------- */
    /* Non-blocking probe with the GIL held: a saturated server returns work on
     * this call, so the hot path never computes a timeout, never reads the
     * ready deque length, and never touches the timer heap. Only when the probe
     * finds nothing do we compute how long to block and block with the GIL
     * released, so executor threads and signals are never starved. */
    int n = epoll_wait(p->epfd, p->evbuf, p->evcap, 0);
    if (n == 0) {
        Py_ssize_t nready = PyObject_Length(p->ready);
        if (nready < 0) {
            return NULL;
        }
        int block_ms = -1;
        if (nready > 0) {
            block_ms = 0;
        } else {
            double delay = Py_HUGE_VAL;
            if (PyList_GET_SIZE(p->scheduled) > 0) {
                PyObject *h0 = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
                PyObject *whenobj = PyObject_GetAttr(h0, g_s_when);
                if (whenobj == NULL) {
                    return NULL;
                }
                delay = PyFloat_AsDouble(whenobj) - loop_now;
                Py_DECREF(whenobj);
            }
            if (p->wheel != NULL && p->wheel->count > 0) {
                double wheel_delay = wheel_next_when(p->wheel) - loop_now;
                if (wheel_delay < delay) {
                    delay = wheel_delay;
                }
            }
            if (!isinf(delay)) {
                if (delay <= 0.0) {
                    block_ms = 0;
                } else {
                    double ms = delay * 1000.0;
                    if (ms >= 2147483646.0) {
                        block_ms = 2147483646;
                    } else {
                        block_ms = (int)ms;
                        if ((double)block_ms < ms) {
                            block_ms += 1;
                        }
                    }
                }
            }
        }
        if (block_ms != 0) {
            blocked = 1;
            Py_BEGIN_ALLOW_THREADS
            n = epoll_wait(p->epfd, p->evbuf, p->evcap, block_ms);
            Py_END_ALLOW_THREADS
        }
    }
    if (n < 0) {
        if (errno == EINTR) {
            n = 0;  /* a signal ran; re-enter next iteration */
        } else {
            PyErr_SetFromErrno(PyExc_OSError);
            return NULL;
        }
    }

    /* --- 3. dispatch readiness directly in C ---------------------------- */
    for (int i = 0; i < n; i++) {
        uint64_t token = p->evbuf[i].data.u64;
        if (token == UINT64_MAX) {
            if (rp_drain_accept_completions(p, 64) < 0) {
                return NULL;
            }
            continue;
        }
        if (token == UINT64_MAX - 1) {
            if (rp_drain_receive_completions(p, 64) < 0) {
                return NULL;
            }
            continue;
        }
        int fd = (int)(uint32_t)token;
        uint32_t generation = (uint32_t)(token >> 32);
        uint32_t ev = p->evbuf[i].events;
        if (fd < 0 || fd >= p->fdcap ||
            p->fds[fd].generation != generation) {
            p->stale_events++;
            continue;
        }
        FdEntry *e = &p->fds[fd];
        if ((ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) && e->reader != NULL) {
            PyObject *cb = Py_NewRef(e->reader);
            if (e->native_reader) {
                rp_dispatch_native_transport(p, cb, 1);
            } else {
                PyObject *ca = e->reader_args ? Py_NewRef(e->reader_args) : NULL;
                rp_dispatch(p, cb, ca);
                Py_XDECREF(ca);
            }
            Py_DECREF(cb);
        }
        /* Re-load and revalidate: the reader may have closed/reused the fd or
         * replaced its writer while this same epoll record was dispatching. */
        e = &p->fds[fd];
        if (e->generation != generation) {
            p->stale_events++;
            continue;
        }
        if ((ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) && e->writer != NULL) {
            PyObject *cb = Py_NewRef(e->writer);
            if (e->native_writer) {
                rp_dispatch_native_transport(p, cb, 0);
            } else {
                PyObject *ca = e->writer_args ? Py_NewRef(e->writer_args) : NULL;
                rp_dispatch(p, cb, ca);
                Py_XDECREF(ca);
            }
            Py_DECREF(cb);
        }
    }

    /* The wheel is driven by the poll deadline itself: no recurring bridge
     * timer, no idle 1 kHz wakeup, and no Python frame between poll and expiry. */
    if (p->wheel != NULL && p->wheel->count > 0) {
        if (blocked) {
            loop_now = mono_seconds();
        }
        if (wheel_run_due(p->wheel, loop_now) < 0) {
            return NULL;
        }
    }

    /* --- 4. due timers -> ready ----------------------------------------- */
    double end_time = loop_now + p->clock_res;
    while (PyList_GET_SIZE(p->scheduled) > 0) {
        PyObject *h0 = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
        PyObject *whenobj = PyObject_GetAttr(h0, g_s_when);
        if (whenobj == NULL) {
            return NULL;
        }
        double when = PyFloat_AsDouble(whenobj);
        Py_DECREF(whenobj);
        if (when == -1.0 && PyErr_Occurred()) {
            return NULL;
        }
        if (when >= end_time) {
            break;
        }
        PyObject *handle = PyObject_CallOneArg(g_heappop, p->scheduled);
        if (handle == NULL) {
            return NULL;
        }
        if (PyObject_SetAttr(handle, g_s_scheduled, Py_False) < 0) {
            Py_DECREF(handle);
            return NULL;
        }
        PyObject *ar = PyObject_CallMethodOneArg(p->ready, g_s_append, handle);
        Py_DECREF(handle);
        if (ar == NULL) {
            return NULL;
        }
        Py_DECREF(ar);
    }

    /* --- 5. drain the ready queue (call_soon + timers, context-correct) -- */
    Py_ssize_t ntodo = PyObject_Length(p->ready);
    if (ntodo < 0) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < ntodo; i++) {
        PyObject *handle = PyObject_CallMethodNoArgs(p->ready, g_s_popleft);
        if (handle == NULL) {
            return NULL;
        }
        PyObject *cancelled = p->direct_task_steps && Py_IS_TYPE(handle, g_handle_type)
            ? PyMember_GetOne((const char *)handle, g_handle_cancelled)
            : PyObject_GetAttr(handle, g_s_cancelled);
        if (cancelled == NULL) {
            Py_DECREF(handle);
            return NULL;
        }
        int is_cancelled = PyObject_IsTrue(cancelled);
        Py_DECREF(cancelled);
        if (!is_cancelled) {
            int fast = p->direct_task_steps ? rp_run_task_step(p, handle) : 0;
            if (fast < 0) {
                Py_DECREF(handle);
                return NULL;
            }
            if (fast == 0) {
                PyObject *rr = PyObject_CallMethodNoArgs(handle, g_s_run);
                Py_XDECREF(rr);
                if (rr == NULL) {
                    /* Handle._run swallows callback errors itself; a failure here
                     * is a real loop fault -- surface it. */
                    Py_DECREF(handle);
                    return NULL;
                }
            }
        }
        Py_DECREF(handle);
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_close(PyObject *op, PyObject *Py_UNUSED(i))
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (p->epfd >= 0) {
        close(p->epfd);
        p->epfd = -1;
    }
    metal_uring_clear(&p->metal.receive_uring);
    metal_provided_buffers_clear(&p->metal.receive_uring,
                                 &p->metal.receive_buffers);
    metal_uring_clear(&p->metal.listener_uring);
    metal_uring_clear(&p->metal.uring);
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
            Py_CLEAR(p->fds[i].accept_callback);
        }
        PyMem_Free(p->fds);
        p->fds = NULL;
        p->fdcap = 0;
    }
    Py_RETURN_NONE;
}

static int
rp_init(PyObject *op, PyObject *args, PyObject *Py_UNUSED(kwds))
{
    ReactorPoller *p = (ReactorPoller *)op;
    PyObject *loop, *wheel = Py_None;
    int direct_task_steps = 1;
    unsigned int worker_id = 0;
    unsigned int io_backend = METAL_IO_EPOLL;
    if (!PyArg_ParseTuple(args, "O|OpII", &loop, &wheel, &direct_task_steps,
                          &worker_id, &io_backend)) {
        return -1;
    }
    p->direct_task_steps = direct_task_steps;
    p->stale_events = 0;
    p->generation_wraps = 0;
    memset(&p->metal, 0, sizeof(p->metal));
    if (metal_slab_init(&p->metal.connections, METAL_CONNECTION_CAPACITY) < 0) {
        return -1;
    }
    if (metal_slab_init(&p->metal.operations, METAL_OPERATION_CAPACITY) < 0) {
        metal_slab_clear(&p->metal.connections);
        return -1;
    }
    p->metal.submissions = 0;
    p->metal.completions = 0;
    p->metal.cross_worker_rejections = 0;
    p->metal.owner_thread = PyThread_get_thread_ident();
    p->metal.worker_id = worker_id;
    p->metal.io_backend = (uint8_t)io_backend;
    p->metal.uring.fd = -1;
    p->metal.listener_uring.fd = -1;
    p->metal.receive_uring.fd = -1;
    if (io_backend > METAL_IO_URING) {
        metal_slab_clear(&p->metal.operations);
        metal_slab_clear(&p->metal.connections);
        PyErr_SetString(PyExc_ValueError, "unknown metal I/O backend");
        return -1;
    }
    if (io_backend == METAL_IO_URING &&
        metal_uring_init(&p->metal.uring, 64) < 0) {
        int saved_errno = errno;
        metal_slab_clear(&p->metal.operations);
        metal_slab_clear(&p->metal.connections);
        errno = saved_errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    if (io_backend == METAL_IO_URING &&
        metal_uring_init(&p->metal.listener_uring, 64) < 0) {
        int saved_errno = errno;
        metal_uring_clear(&p->metal.uring);
        metal_slab_clear(&p->metal.operations);
        metal_slab_clear(&p->metal.connections);
        errno = saved_errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    if (io_backend == METAL_IO_URING) {
        if (metal_uring_init(&p->metal.receive_uring, 256) < 0) {
            p->metal.receive_setup_errno = errno;
        } else if (metal_provided_buffers_init(
                       &p->metal.receive_uring, &p->metal.receive_buffers,
                       METAL_RECV_BUFFER_COUNT, METAL_RECV_BUFFER_SIZE,
                       METAL_RECV_BUFFER_GROUP) < 0) {
            p->metal.receive_setup_errno = errno;
            metal_uring_clear(&p->metal.receive_uring);
        } else {
            p->metal.receive_enabled = 1;
        }
    }
    p->epfd = epoll_create1(EPOLL_CLOEXEC);
    if (p->epfd < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    if (io_backend == METAL_IO_URING) {
        struct epoll_event ring_event;
        memset(&ring_event, 0, sizeof(ring_event));
        ring_event.events = EPOLLIN;
        ring_event.data.u64 = UINT64_MAX;
        if (epoll_ctl(p->epfd, EPOLL_CTL_ADD, p->metal.listener_uring.fd,
                      &ring_event) < 0) {
            PyErr_SetFromErrno(PyExc_OSError);
            return -1;
        }
        if (p->metal.receive_enabled) {
            ring_event.data.u64 = UINT64_MAX - 1;
            if (epoll_ctl(p->epfd, EPOLL_CTL_ADD, p->metal.receive_uring.fd,
                          &ring_event) < 0) {
                p->metal.receive_setup_errno = errno;
                p->metal.receive_enabled = 0;
                metal_provided_buffers_clear(&p->metal.receive_uring,
                                             &p->metal.receive_buffers);
                metal_uring_clear(&p->metal.receive_uring);
            }
        }
    }
    p->evcap = 1024;
    p->evbuf = PyMem_Malloc((size_t)p->evcap * sizeof(struct epoll_event));
    if (p->evbuf == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    p->fds = NULL;
    p->fdcap = 0;
    p->loop = Py_NewRef(loop);
    p->wheel_obj = Py_NewRef(wheel);
    p->wheel = PyObject_TypeCheck(wheel, &TimingWheelType)
                   ? (TimingWheel *)wheel : NULL;
    p->ready = PyObject_GetAttrString(loop, "_ready");
    p->scheduled = PyObject_GetAttrString(loop, "_scheduled");
    p->exc_handler = PyObject_GetAttrString(loop, "call_exception_handler");
    if (p->ready == NULL || p->scheduled == NULL || p->exc_handler == NULL) {
        return -1;
    }
    if (!PyList_Check(p->scheduled)) {
        PyErr_SetString(PyExc_TypeError, "loop._scheduled must be a list");
        return -1;
    }
    p->clock_res = 1e-9;
    PyObject *cr = PyObject_GetAttrString(loop, "_clock_resolution");
    if (cr != NULL) {
        p->clock_res = PyFloat_AsDouble(cr);
        Py_DECREF(cr);
        if (p->clock_res < 0 && PyErr_Occurred()) {
            PyErr_Clear();
            p->clock_res = 1e-9;
        }
    } else {
        PyErr_Clear();
    }
    return 0;
}

static int
rp_traverse(PyObject *op, visitproc visit, void *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    Py_VISIT(p->loop);
    Py_VISIT(p->ready);
    Py_VISIT(p->scheduled);
    Py_VISIT(p->exc_handler);
    Py_VISIT(p->wheel_obj);
    for (int i = 0; i < p->fdcap; i++) {
        Py_VISIT(p->fds[i].reader);
        Py_VISIT(p->fds[i].reader_args);
        Py_VISIT(p->fds[i].writer);
        Py_VISIT(p->fds[i].writer_args);
        Py_VISIT(p->fds[i].accept_callback);
    }
    return 0;
}

static int
rp_clear(PyObject *op)
{
    ReactorPoller *p = (ReactorPoller *)op;
    Py_CLEAR(p->loop);
    Py_CLEAR(p->ready);
    Py_CLEAR(p->scheduled);
    Py_CLEAR(p->exc_handler);
    Py_CLEAR(p->wheel_obj);
    p->wheel = NULL;
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
            Py_CLEAR(p->fds[i].accept_callback);
        }
    }
    return 0;
}

static void
rp_dealloc(PyObject *op)
{
    ReactorPoller *p = (ReactorPoller *)op;
    PyObject_GC_UnTrack(op);
    if (p->epfd >= 0) {
        close(p->epfd);
        p->epfd = -1;
    }
    if (p->evbuf != NULL) {
        PyMem_Free(p->evbuf);
        p->evbuf = NULL;
    }
    rp_clear(op);
    if (p->fds != NULL) {
        PyMem_Free(p->fds);
        p->fds = NULL;
    }
    metal_uring_clear(&p->metal.receive_uring);
    metal_provided_buffers_clear(&p->metal.receive_uring,
                                 &p->metal.receive_buffers);
    metal_uring_clear(&p->metal.listener_uring);
    metal_uring_clear(&p->metal.uring);
    metal_slab_clear(&p->metal.operations);
    metal_slab_clear(&p->metal.connections);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
rp_get_stale_events(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(((ReactorPoller *)op)->stale_events);
}

static PyObject *
rp_get_generation_wraps(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(((ReactorPoller *)op)->generation_wraps);
}

static PyObject *
rp_get_accept_submissions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_submissions);
}

static PyObject *
rp_get_accept_completions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_completions);
}

static PyObject *
rp_get_accept_multishot_fallbacks(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_multishot_fallbacks);
}

static PyObject *
rp_get_receive_enabled(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(((ReactorPoller *)op)->metal.receive_enabled);
}

static PyObject *
rp_get_receive_submissions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_submissions);
}

static PyObject *
rp_get_receive_completions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_completions);
}

static PyObject *
rp_get_receive_multishot_fallbacks(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_multishot_fallbacks);
}

static PyObject *
rp_get_provided_buffer_recycles(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.provided_buffer_recycles);
}

static PyObject *
rp_get_provided_buffer_count(PyObject *op, void *closure)
{
    (void)closure;
    ReactorPoller *p = (ReactorPoller *)op;
    return PyLong_FromUnsignedLong(
        p->metal.receive_enabled ? p->metal.receive_buffers.entries : 0);
}

static PyGetSetDef rp_getset[] = {
    {"stale_events", rp_get_stale_events, NULL,
     PyDoc_STR("Epoll records rejected after registration replacement."), NULL},
    {"generation_wraps", rp_get_generation_wraps, NULL,
     PyDoc_STR("32-bit registration generation wraps."), NULL},
    {"accept_submissions", rp_get_accept_submissions, NULL,
     PyDoc_STR("io_uring accept SQEs submitted."), NULL},
    {"accept_completions", rp_get_accept_completions, NULL,
     PyDoc_STR("io_uring accept CQEs drained."), NULL},
    {"accept_multishot_fallbacks", rp_get_accept_multishot_fallbacks, NULL,
     PyDoc_STR("Listeners downgraded to ordinary accept SQEs."), NULL},
    {"receive_enabled", rp_get_receive_enabled, NULL,
     PyDoc_STR("Whether provided-buffer receive is active."), NULL},
    {"receive_submissions", rp_get_receive_submissions, NULL,
     PyDoc_STR("Receive SQEs submitted."), NULL},
    {"receive_completions", rp_get_receive_completions, NULL,
     PyDoc_STR("Receive CQEs drained."), NULL},
    {"receive_multishot_fallbacks", rp_get_receive_multishot_fallbacks, NULL,
     PyDoc_STR("Receivers downgraded to one-shot SQEs."), NULL},
    {"provided_buffer_recycles", rp_get_provided_buffer_recycles, NULL,
     PyDoc_STR("Consumed provided buffers returned to the ring."), NULL},
    {"provided_buffer_count", rp_get_provided_buffer_count, NULL,
     PyDoc_STR("Registered receive buffer count."), NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef rp_methods[] = {
    {"_add_reader", (PyCFunction)(void (*)(void))rp_add_reader, METH_FASTCALL, NULL},
    {"_add_writer", (PyCFunction)(void (*)(void))rp_add_writer, METH_FASTCALL, NULL},
    {"_remove_reader", rp_remove_reader, METH_O, NULL},
    {"_remove_writer", rp_remove_writer, METH_O, NULL},
    {"_add_uring_listener", rp_add_uring_listener, METH_VARARGS, NULL},
    {"_remove_uring_listener", rp_remove_uring_listener, METH_O, NULL},
    {"_start_uring_receive", rp_start_uring_receive, METH_O, NULL},
    {"_stop_uring_receive", rp_stop_uring_receive, METH_O, NULL},
    {"_run_once", rp_run_once, METH_NOARGS, NULL},
    {"close", rp_close, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ReactorPollerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.ReactorPoller",
    .tp_basicsize = sizeof(ReactorPoller),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = rp_dealloc,
    .tp_traverse = rp_traverse,
    .tp_clear = rp_clear,
    .tp_methods = rp_methods,
    .tp_getset = rp_getset,
    .tp_init = rp_init,
    .tp_new = PyType_GenericNew,
};

static WreathTransportCAPI transport_capi = {
    WREATH_TRANSPORT_CAPI_VERSION,
    transport_capi_check,
    transport_capi_write,
    transport_capi_writelines,
};

static PyModuleDef reactormodule = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._reactor",
    .m_doc = "Native reactor primitives (timing wheel + shootout stores + transport).",
    .m_size = 0,
};

static PyMemberDef *
handle_member(PyObject *handle_type, const char *name)
{
    PyObject *descriptor = PyObject_GetAttrString(handle_type, name);
    if (descriptor == NULL) {
        return NULL;
    }
    if (!Py_IS_TYPE(descriptor, &PyMemberDescr_Type) ||
            PyDescr_TYPE(descriptor) != (PyTypeObject *)handle_type) {
        PyErr_Format(PyExc_RuntimeError, "asyncio.Handle.%s is not a member descriptor", name);
        Py_DECREF(descriptor);
        return NULL;
    }
    PyMemberDef *member = ((PyMemberDescrObject *)descriptor)->d_member;
    Py_DECREF(descriptor);
    return member;
}

static int
load_handle_layout(void)
{
    /* CPython 3.14-specific but offset-independent: resolve Handle's slot
     * descriptors once, then use PyMember_GetOne in the ready-queue hot path. */
    /* native-lint: allow NC004 -- called once by module init, never per callback */
    PyObject *events = PyImport_ImportModule("asyncio.events");
    if (events == NULL) {
        return -1;
    }
    PyObject *handle = PyObject_GetAttrString(events, "Handle");
    Py_DECREF(events);
    if (handle == NULL) {
        return -1;
    }
    if (!PyType_Check(handle)) {
        PyErr_SetString(PyExc_RuntimeError, "asyncio.events.Handle is not a type");
        Py_DECREF(handle);
        return -1;
    }
    g_handle_type = (PyTypeObject *)handle;
    g_handle_callback = handle_member(handle, "_callback");
    g_handle_args = handle_member(handle, "_args");
    g_handle_cancelled = handle_member(handle, "_cancelled");
    g_handle_context = handle_member(handle, "_context");
    g_handle_source_traceback = handle_member(handle, "_source_traceback");
    if (g_handle_callback == NULL || g_handle_args == NULL ||
            g_handle_cancelled == NULL || g_handle_context == NULL ||
            g_handle_source_traceback == NULL) {
        return -1;
    }
    return 0;
}

PyMODINIT_FUNC
PyInit__reactor(void)
{
    if (PyType_Ready(&WheelTimerType) < 0 || PyType_Ready(&TimingWheelType) < 0 ||
        PyType_Ready(&RTimerType) < 0 || PyType_Ready(&HeapStoreType) < 0 ||
        PyType_Ready(&HierStoreType) < 0 || PyType_Ready(&FifoStoreType) < 0 ||
        PyType_Ready(&SocketTransportType) < 0 || PyType_Ready(&ReactorPollerType) < 0) {
        return NULL;
    }
    /* intern the attribute names the run loop touches every iteration */
    g_s_when = PyUnicode_InternFromString("_when");
    g_s_run = PyUnicode_InternFromString("_run");
    g_s_context_run = PyUnicode_InternFromString("run");
    g_s_cancelled = PyUnicode_InternFromString("_cancelled");
    g_s_scheduled = PyUnicode_InternFromString("_scheduled");
    g_s_popleft = PyUnicode_InternFromString("popleft");
    g_s_append = PyUnicode_InternFromString("append");
    if (!g_s_when || !g_s_run || !g_s_context_run || !g_s_cancelled ||
            !g_s_scheduled || !g_s_popleft || !g_s_append) {
        return NULL;
    }
    if (load_handle_layout() < 0) {
        return NULL;
    }
    /* native-lint: allow NC004 -- one-time module-init lookup, never per value */
    PyObject *heapq = PyImport_ImportModule("heapq");
    if (heapq == NULL) {
        return NULL;
    }
    g_heappop = PyObject_GetAttrString(heapq, "heappop");
    Py_DECREF(heapq);
    if (g_heappop == NULL) {
        return NULL;
    }
    PyObject *m = PyModule_Create(&reactormodule);
    if (m == NULL) {
        return NULL;
    }
    PyObject *transport_capsule = PyCapsule_New(
        &transport_capi, WREATH_TRANSPORT_CAPI_NAME, NULL);
    if (transport_capsule == NULL ||
        PyModule_AddObject(m, "_TRANSPORT_C_API", transport_capsule) < 0) {
        Py_XDECREF(transport_capsule);
        Py_DECREF(m);
        return NULL;
    }
    if (PyModule_AddObjectRef(m, "TimingWheel", (PyObject *)&TimingWheelType) < 0 ||
        PyModule_AddObjectRef(m, "WheelTimer", (PyObject *)&WheelTimerType) < 0 ||
        PyModule_AddObjectRef(m, "HeapStore", (PyObject *)&HeapStoreType) < 0 ||
        PyModule_AddObjectRef(m, "HierStore", (PyObject *)&HierStoreType) < 0 ||
        PyModule_AddObjectRef(m, "FifoStore", (PyObject *)&FifoStoreType) < 0 ||
        PyModule_AddObjectRef(m, "SocketTransport", (PyObject *)&SocketTransportType) < 0 ||
        PyModule_AddObjectRef(m, "ReactorPoller", (PyObject *)&ReactorPollerType) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
