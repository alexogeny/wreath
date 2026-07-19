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
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/epoll.h>
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
    Py_ssize_t count;           /* live timers */
} TimingWheel;

static PyTypeObject WheelTimerType;
static PyTypeObject TimingWheelType;

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
    t->prev = t->next = NULL;
    t->slot = -1;
    t->wheel->count--;
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
    t->slot = slot;
    t->wheel = w;
    t->prev = NULL;
    t->next = w->slots[slot];
    if (t->next != NULL) {
        t->next->prev = t;
    }
    w->slots[slot] = t;
    w->count++;
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
    t->slot = slot;
    t->wheel = w;
    t->prev = NULL;
    t->next = w->slots[slot];
    if (t->next != NULL) {
        t->next->prev = t;
    }
    w->slots[slot] = t;
    w->count++;
    return Py_NewRef((PyObject *)t);
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
    return due;
}

/* advance_run(now_seconds) -> count of timers fired, each dispatched in C via
 * context.run(callback, *args). No Python round-trip per fired timer. */
static PyObject *
wheel_advance_run(PyObject *op, PyObject *arg)
{
    TimingWheel *w = (TimingWheel *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    int64_t target = (int64_t)((now - w->base) / w->resolution);
    Py_ssize_t fired = 0;
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
                        /* no context: call the callback directly with its args */
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
    return PyLong_FromSsize_t(fired);
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
    Py_ssize_t whead;
    int writing;                /* writer registered */
    int cork;                   /* buffer writes during synchronous request drive */
    Py_ssize_t direct_writelines; /* diagnostic count of sendmsg fast-path writes */
    /* flow control + lifecycle */
    Py_ssize_t high_water, low_water;
    int protocol_paused;
    int reading_paused;
    int closing;
    int conn_lost;
    int eof;
    int protocol_connected;
} SocketTransport;

static PyTypeObject SocketTransportType;

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
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
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
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
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
    Py_ssize_t size = st_wsize(t);
    if (size > 0) {
        const char *p = PyByteArray_AS_STRING(t->wbuf) + t->whead;
        ssize_t n;
        do {
            n = send(t->fd, p, (size_t)size, 0);
        } while (n < 0 && errno == EINTR);
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
            ssize_t n;
            do {
                n = recv(t->fd, buffer, (size_t)capacity, 0);
            } while (n < 0 && errno == EINTR);
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
            ssize_t n;
            do {
                n = recv(t->fd, view.buf, (size_t)view.len, 0);

            } while (n < 0 && errno == EINTR);
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
            ssize_t n;
            do {
                n = recv(t->fd, stackbuf, sizeof(stackbuf), 0);

            } while (n < 0 && errno == EINTR);
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
        ssize_t n;
        do {
            n = send(t->fd, p, (size_t)size, 0);

        } while (n < 0 && errno == EINTR);
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
    Py_ssize_t off = 0;
    if (!t->cork && st_wsize(t) == 0) {
        /* nothing buffered: try to send straight away */
        ssize_t n;
        do {
            n = send(t->fd, view.buf, (size_t)view.len, 0);

        } while (n < 0 && errno == EINTR);
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
            ssize_t sent;
            do {
                sent = sendmsg(t->fd, &msg, 0);
            } while (sent < 0 && errno == EINTR);
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
    if (!t->reading_paused) {
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
    }
    if (st_wsize(t) == 0) {
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
    Py_RETURN_NONE;
}

static PyObject *
st_start_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || t->reading_paused) {
        Py_RETURN_NONE;
    }
    PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
    Py_XDECREF(r);
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
    PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
    Py_XDECREF(r);
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
    PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
    Py_XDECREF(r);
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
    t->writing = 0;
    t->cork = 0;
    t->direct_writelines = 0;
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
    PyDict_SetItemString(t->extra, "socket", sock);
    PyObject *sn = PyObject_CallMethod(sock, "getsockname", NULL);
    if (sn != NULL) {
        PyDict_SetItemString(t->extra, "sockname", sn);
        Py_DECREF(sn);
    } else {
        PyErr_Clear();
    }
    if (PyDict_GetItemString(t->extra, "peername") == NULL) {
        PyObject *pn = PyObject_CallMethod(sock, "getpeername", NULL);
        if (pn != NULL) {
            PyDict_SetItemString(t->extra, "peername", pn);
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
    Py_VISIT(t->sock);
    Py_VISIT(t->protocol);
    Py_VISIT(t->server);
    Py_VISIT(t->extra);
    Py_VISIT(t->wbuf);
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
    Py_CLEAR(t->loop);
    Py_CLEAR(t->sock);
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    Py_CLEAR(t->extra);
    Py_CLEAR(t->wbuf);
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

static PyGetSetDef st_getset[] = {
    {"_fused_http1", st_fused_http1_get, NULL,
     "whether ingress uses the private native HTTP/1 C API", NULL},
    {"_direct_writelines", st_direct_writelines_get, NULL,
     "number of large writelines emitted through sendmsg", NULL},
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
static PyObject *g_s_cancelled;  /* "_cancelled" */
static PyObject *g_s_scheduled;  /* "_scheduled" */
static PyObject *g_s_popleft;    /* "popleft"    */
static PyObject *g_s_append;     /* "append"     */
static PyObject *g_heappop;      /* heapq.heappop */

typedef struct {
    PyObject *reader;       /* callable or NULL */
    PyObject *reader_args;  /* tuple, or NULL for no-arg fast call */
    PyObject *writer;
    PyObject *writer_args;
    uint32_t mask;          /* epoll mask currently registered (0 => not in epoll) */
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
    struct epoll_event *evbuf;
    int evcap;
    double clock_res;       /* loop._clock_resolution */
} ReactorPoller;

static PyTypeObject ReactorPollerType;

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

/* Reconcile epoll registration for `fd` with the desired mask. */
static int
rp_apply(ReactorPoller *p, int fd, uint32_t want)
{
    FdEntry *e = &p->fds[fd];
    if (e->mask == want) {
        return 0;
    }
    struct epoll_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.events = want;
    ev.data.fd = fd;
    int op;
    if (want == 0) {
        op = EPOLL_CTL_DEL;
    } else if (e->mask == 0) {
        op = EPOLL_CTL_ADD;
    } else {
        op = EPOLL_CTL_MOD;
    }
    if (epoll_ctl(p->epfd, op, fd, want == 0 ? NULL : &ev) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
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
    if (rp_apply(p, fd, e->mask | EPOLLIN) < 0) {
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
    if (rp_apply(p, fd, e->mask | EPOLLOUT) < 0) {
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
    if (rp_apply(p, fd, e->mask & ~(uint32_t)EPOLLIN) < 0) {
        return NULL;
    }
    Py_CLEAR(e->reader);
    Py_CLEAR(e->reader_args);
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
    if (rp_apply(p, fd, e->mask & ~(uint32_t)EPOLLOUT) < 0) {
        return NULL;
    }
    Py_CLEAR(e->writer);
    Py_CLEAR(e->writer_args);
    Py_RETURN_TRUE;
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
    /* Swallow into the loop's exception handler (never propagate out of poll). */
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        return;
    }
    PyObject *ctx = PyDict_New();
    if (ctx != NULL) {
        PyDict_SetItemString(ctx, "message",
                             PyUnicode_FromString("Exception in callback"));
        PyDict_SetItemString(ctx, "exception", exc);
        PyObject *hr = PyObject_CallOneArg(p->exc_handler, ctx);
        Py_XDECREF(hr);
        Py_DECREF(ctx);
    }
    if (PyErr_Occurred()) {
        PyErr_WriteUnraisable(p->exc_handler);
    }
    Py_DECREF(exc);
}

static PyObject *
rp_run_once(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ReactorPoller *p = (ReactorPoller *)op;

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
        } else if (PyList_GET_SIZE(p->scheduled) > 0) {
            PyObject *h0 = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
            PyObject *whenobj = PyObject_GetAttr(h0, g_s_when);
            if (whenobj == NULL) {
                return NULL;
            }
            double delay = PyFloat_AsDouble(whenobj) - mono_seconds();
            Py_DECREF(whenobj);
            /* Round the block up (ceil): a positive sub-millisecond delay must
             * never floor to 0, which would turn the poll non-blocking and make
             * the loop busy-spin until the timer expires (a full core burned
             * idle while any recurring timer keeps the schedule non-empty). */
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
        if (block_ms != 0) {
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
        int fd = p->evbuf[i].data.fd;
        uint32_t ev = p->evbuf[i].events;
        if (fd < 0 || fd >= p->fdcap) {
            continue;
        }
        FdEntry *e = &p->fds[fd];
        if ((ev & (EPOLLIN | EPOLLERR | EPOLLHUP)) && e->reader != NULL) {
            PyObject *cb = Py_NewRef(e->reader);
            PyObject *ca = e->reader_args ? Py_NewRef(e->reader_args) : NULL;
            rp_dispatch(p, cb, ca);
            Py_DECREF(cb);
            Py_XDECREF(ca);
        }
        /* Re-load: the reader may have closed the fd / removed the writer. */
        e = &p->fds[fd];
        if ((ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) && e->writer != NULL) {
            PyObject *cb = Py_NewRef(e->writer);
            PyObject *ca = e->writer_args ? Py_NewRef(e->writer_args) : NULL;
            rp_dispatch(p, cb, ca);
            Py_DECREF(cb);
            Py_XDECREF(ca);
        }
    }

    /* --- 4. due timers -> ready ----------------------------------------- */
    double end_time = mono_seconds() + p->clock_res;
    while (PyList_GET_SIZE(p->scheduled) > 0) {
        PyObject *h0 = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
        PyObject *whenobj = PyObject_GetAttr(h0, g_s_when);
        if (whenobj == NULL) {
            return NULL;
        }
        double when = PyFloat_AsDouble(whenobj);
        Py_DECREF(whenobj);
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
        PyObject *cancelled = PyObject_GetAttr(handle, g_s_cancelled);
        if (cancelled == NULL) {
            Py_DECREF(handle);
            return NULL;
        }
        int is_cancelled = PyObject_IsTrue(cancelled);
        Py_DECREF(cancelled);
        if (!is_cancelled) {
            PyObject *rr = PyObject_CallMethodNoArgs(handle, g_s_run);
            Py_XDECREF(rr);
            if (rr == NULL) {
                /* Handle._run swallows callback errors itself; a failure here
                 * is a real loop fault -- surface it. */
                Py_DECREF(handle);
                return NULL;
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
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
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
    PyObject *loop;
    if (!PyArg_ParseTuple(args, "O", &loop)) {
        return -1;
    }
    p->epfd = epoll_create1(EPOLL_CLOEXEC);
    if (p->epfd < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
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
    for (int i = 0; i < p->fdcap; i++) {
        Py_VISIT(p->fds[i].reader);
        Py_VISIT(p->fds[i].reader_args);
        Py_VISIT(p->fds[i].writer);
        Py_VISIT(p->fds[i].writer_args);
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
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
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
    Py_TYPE(op)->tp_free(op);
}

static PyMethodDef rp_methods[] = {
    {"_add_reader", (PyCFunction)(void (*)(void))rp_add_reader, METH_FASTCALL, NULL},
    {"_add_writer", (PyCFunction)(void (*)(void))rp_add_writer, METH_FASTCALL, NULL},
    {"_remove_reader", rp_remove_reader, METH_O, NULL},
    {"_remove_writer", rp_remove_writer, METH_O, NULL},
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
    .tp_init = rp_init,
    .tp_new = PyType_GenericNew,
};

static PyModuleDef reactormodule = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._reactor",
    .m_doc = "Native reactor primitives (timing wheel + shootout stores + transport).",
    .m_size = 0,
};

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
    g_s_cancelled = PyUnicode_InternFromString("_cancelled");
    g_s_scheduled = PyUnicode_InternFromString("_scheduled");
    g_s_popleft = PyUnicode_InternFromString("popleft");
    g_s_append = PyUnicode_InternFromString("append");
    if (!g_s_when || !g_s_run || !g_s_cancelled || !g_s_scheduled ||
        !g_s_popleft || !g_s_append) {
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
