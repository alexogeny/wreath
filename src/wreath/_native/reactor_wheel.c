/* wreath._native._reactor — native primitives for the reactor event loop.
 *
 * Stage-0 component: a slotted deadline index for per-connection deadlines.
 *
 * asyncio keeps timers in one binary heap: O(log n) insert and cancel over
 * *every* timer in the process, plus a cancellation-compaction pass. A server
 * at high RPS churns two timers per request (keep-alive + request deadline) and
 * almost always cancels them before they fire, so that global heap is both the
 * hot path and the wrong shape for it.
 *
 * Design, in three parts. Each is answering a different question, and reading
 * any one of them as the whole structure will mislead you:
 *
 * 1. `slots` buckets at `resolution` seconds each. A deadline maps to exactly
 *    one bucket by `deadline & slot_mask`, so a timer never migrates and there
 *    is no rounds counter and no overflow list. **This is not a Varghese-Lauck
 *    hashed wheel** -- nothing here ticks per bucket or decrements a rounds
 *    field, and an earlier version of this comment said otherwise and misled
 *    two separate readers into reasoning about a structure that is not here.
 *
 * 2. A tournament tree over the per-slot minima, so "when is the next deadline
 *    anywhere" is `deadline_tree[1]` in O(1) and the cursor **jumps** straight
 *    to the next due tick. The wheel is therefore already tickless: an idle
 *    advance is O(1) in elapsed ticks however long the gap, which is what the
 *    tree was added for -- sweeping every intervening bucket was the original
 *    cost and this replaced it.
 *
 * 3. Each slot is an intrusive **pairing heap** ordered by deadline, not a
 *    list. That is what keeps the structure honest when deadlines collide.
 *    Slots are a hash, so any deadlines congruent modulo `nslots` share one --
 *    reachable from a periodic workload whose period is a multiple of
 *    `nslots * resolution`, or a client opening connections on that cadence.
 *    With an unordered chain, both "which nodes are due" and "what is this
 *    slot's new minimum" needed a full walk, and each was quadratic in the
 *    chain: 4000 colliding deadlines cost 3549 ns each to cancel and 6658 ns
 *    each to fire, against 34 ns and 43 ns spread. The heap makes insert O(1),
 *    cancel and fire O(log c) in the chain, and leaves the arrangement
 *    penalty at the constant factor rather than the exponent.
 *
 * Nothing reallocates: the heap is intrusive, the slot array and tournament
 * tree are sized once at construction, and a node costs one pointer more than
 * a doubly linked list node. Cancel is the dominant operation, so that pointer
 * -- which is what makes arbitrary removal O(log c) rather than a walk -- is
 * bought for the operation that actually happens.
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
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <linux/io_uring.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>

#include "reactor_internal.h"

static PyTypeObject WheelTimerType;
PyTypeObject TimingWheelType;
static PyObject *g_s_context_run;

/* --- the per-slot pairing heap ------------------------------------------- */

/* Link two heaps so the smaller deadline wins. Ties keep `a` on top, which
 * makes a same-deadline cohort behave like a stack -- the previous chain was
 * head-inserted and drained the same way, so a batch of identical deadlines
 * still fires in the order it did before. */
static WheelTimer *
heap_meld(WheelTimer *a, WheelTimer *b)
{
    if (a == NULL) {
        return b;
    }
    if (b == NULL) {
        return a;
    }
    if (b->deadline < a->deadline) {
        WheelTimer *swap = a;
        a = b;
        b = swap;
    }
    b->next = a->child;
    if (a->child != NULL) {
        a->child->prev = b;
    }
    b->prev = a;
    a->child = b;
    return a;
}

/* The two-pass merge that gives a pairing heap its amortized O(log n) delete:
 * meld adjacent siblings left to right, then fold the results right to left.
 *
 * Iterative rather than recursive on purpose. The sibling list is as long as
 * the slot's chain, and the arrangement this whole structure exists to survive
 * is thousands of deadlines in one slot -- recursion there would trade a
 * quadratic for a stack overflow. */
static WheelTimer *
heap_merge_pairs(WheelTimer *first)
{
    WheelTimer *stack = NULL;
    while (first != NULL) {
        WheelTimer *a = first;
        WheelTimer *b = a->next;
        if (b == NULL) {
            a->prev = NULL;
            a->next = stack;   /* `next` doubles as the pass-one stack link */
            stack = a;
            break;
        }
        first = b->next;
        a->prev = a->next = NULL;
        b->prev = b->next = NULL;
        WheelTimer *merged = heap_meld(a, b);
        merged->next = stack;
        stack = merged;
    }
    WheelTimer *result = NULL;
    while (stack != NULL) {
        WheelTimer *nxt = stack->next;
        stack->next = NULL;
        result = heap_meld(result, stack);
        stack = nxt;
    }
    if (result != NULL) {
        result->prev = result->next = NULL;
    }
    return result;
}

/* Remove `node` from the heap rooted at `root` and return the new root.
 *
 * `node == root` is the pop the fire loop takes; anything else is a cancel,
 * which is why the parent pointer exists -- the node is spliced out of its
 * sibling list in O(1) and only its own children are re-merged. */
static WheelTimer *
heap_delete(WheelTimer *root, WheelTimer *node)
{
    if (node != root) {
        if (node->prev->child == node) {
            node->prev->child = node->next;
        } else {
            node->prev->next = node->next;
        }
        if (node->next != NULL) {
            node->next->prev = node->prev;
        }
        node->prev = node->next = NULL;
    }
    WheelTimer *orphans = heap_merge_pairs(node->child);
    node->child = NULL;
    return node == root ? orphans : heap_meld(root, orphans);
}

/* Publish a slot's current minimum into the tournament tree.
 *
 * The minimum is the heap root, read in O(1) -- the walk this replaces was the
 * quadratic. The early return matters: most operations leave a slot's minimum
 * where it was, and skipping the sift keeps the tree cost off that path.
 *
 * `slot_rescans` counts the times a slot's published minimum actually moved.
 * It used to count chain walks; there are none now, and a counter that always
 * reads `k` regardless of arrangement was exactly why this defect was invisible
 * to the probe that watched it. */
static void
wheel_publish_slot_minimum(TimingWheel *w, int slot)
{
    WheelTimer *root = w->slots[slot];
    int64_t minimum = root != NULL ? root->deadline : INT64_MAX;
    int node = w->tree_base + slot;
    if (w->deadline_tree[node] == minimum) {
        return;
    }
    w->slot_rescans++;
    w->deadline_tree[node] = minimum;
    w->tree_node_updates++;
    while (node > 1) {
        node >>= 1;
        int64_t left = w->deadline_tree[node << 1];
        int64_t right = w->deadline_tree[(node << 1) | 1];
        w->deadline_tree[node] = left < right ? left : right;
        w->tree_node_updates++;
    }
    w->next_deadline = w->deadline_tree[1];
}

/* Push an already-initialised node into its slot and republish the minimum.
 * O(1) meld plus, only when this node is the new slot minimum, one O(log
 * nslots) sift. */
static void
wheel_insert(TimingWheel *w, WheelTimer *t, int slot)
{
    t->prev = t->next = t->child = NULL;
    w->slots[slot] = heap_meld(w->slots[slot], t);
    w->count++;
    wheel_publish_slot_minimum(w, slot);
}

/* Drop the wheel's reference to every node in one slot, for teardown.
 *
 * The traversal threads its own stack through the sibling links rather than
 * recursing, because a slot's heap is as deep as its chain is long and the
 * arrangement worth surviving puts thousands of nodes in one slot. Each child
 * list is walked exactly once to find its tail, so the whole release is linear
 * in the node count. */
static void
heap_release(WheelTimer *root)
{
    WheelTimer *stack = root;
    while (stack != NULL) {
        WheelTimer *node = stack;
        stack = node->next;
        WheelTimer *child = node->child;
        if (child != NULL) {
            WheelTimer *tail = child;
            while (tail->next != NULL) {
                tail = tail->next;
            }
            tail->next = stack;
            stack = child;
        }
        node->slot = -1;
        node->prev = node->next = node->child = NULL;
        Py_CLEAR(node->callback);
        Py_DECREF(node);  /* drop the wheel's reference */
    }
}

static int
wheel_slot(const TimingWheel *w, int64_t deadline)
{
    return w->slot_mask >= 0
        ? (int)(deadline & w->slot_mask)
        : (int)(deadline % w->nslots);
}

double
wreath_wheel_next_when(TimingWheel *w)
{
    if (w->next_deadline == INT64_MAX) {
        return Py_HUGE_VAL;
    }
    return w->base + (double)w->next_deadline * w->resolution;
}

/* --- WheelTimer ---------------------------------------------------------- */

/* Remove a timer from its slot heap, whether it is the root the fire loop is
 * popping or a node in the middle that a caller cancelled. Both are O(log c)
 * amortized in the slot's chain length, and the arrangement of the deadlines
 * around it does not enter into the cost. */
static void
timer_unlink(WheelTimer *t)
{
    if (t->slot < 0) {
        return;  /* already out of the wheel */
    }
    TimingWheel *w = t->wheel;
    int slot = t->slot;
    w->slots[slot] = heap_delete(w->slots[slot], t);
    t->prev = t->next = t->child = NULL;
    t->slot = -1;
    w->count--;
    wheel_publish_slot_minimum(w, slot);
}

static PyObject *
timer_cancel(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WheelTimer *t = (WheelTimer *)op;
    if (t->callback == NULL) {
        Py_RETURN_FALSE;  /* already fired or cancelled */
    }
    int linked = t->slot >= 0;
    timer_unlink(t);
    Py_CLEAR(t->callback);
    /* Drop the wheel's owning reference (parked when scheduled). The caller's
     * handle keeps `t` alive for the duration of this call. A node that is
     * unlinked but still has a callback sits on run_due's pending-dispatch
     * list, which owns that reference and will drop it after dispatch --
     * clearing the callback above already made the dispatch a no-op. */
    if (linked) {
        Py_DECREF(t);
    }
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
    int tree_base = 1;
    while (tree_base < nslots) {
        tree_base <<= 1;
    }
    w->deadline_tree = PyMem_Malloc(
        (size_t)(tree_base * 2) * sizeof(int64_t));
    if (w->deadline_tree == NULL) {
        PyMem_Free(w->slots);
        w->slots = NULL;
        PyErr_NoMemory();
        return -1;
    }
    for (int node = 0; node < tree_base * 2; node++) {
        w->deadline_tree[node] = INT64_MAX;
    }
    w->nslots = nslots;
    w->tree_base = tree_base;
    w->slot_mask = (nslots & (nslots - 1)) == 0 ? nslots - 1 : -1;
    w->resolution = resolution;
    w->inverse_resolution = 1.0 / resolution;
    w->base = base;
    w->cursor = 0;
    w->next_deadline = INT64_MAX;
    w->count = 0;
    return 0;
}

static void
wheel_dealloc(PyObject *op)
{
    TimingWheel *w = (TimingWheel *)op;
    if (w->slots != NULL) {
        for (int i = 0; i < w->nslots; i++) {
            heap_release(w->slots[i]);
            w->slots[i] = NULL;
        }
        PyMem_Free(w->slots);
    }
    PyMem_Free(w->deadline_tree);
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
    int64_t ticks = (int64_t)(delay * w->inverse_resolution);
    if (ticks < 1) {
        ticks = 1;  /* never fire in the bucket currently being swept */
    }
    int64_t deadline = w->cursor + ticks;
    int slot = wheel_slot(w, deadline);

    WheelTimer *t = PyObject_New(WheelTimer, &WheelTimerType);
    if (t == NULL) {
        return NULL;
    }
    t->callback = Py_NewRef(callback);
    t->args = NULL;
    t->context = NULL;
    t->deadline = deadline;
    t->slot = slot;
    t->wheel = w;
    wheel_insert(w, t, slot);
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
    int64_t ticks = (int64_t)(delay * w->inverse_resolution);
    if (ticks < 1) {
        ticks = 1;
    }
    int64_t deadline = w->cursor + ticks;
    int slot = wheel_slot(w, deadline);
    WheelTimer *t = PyObject_New(WheelTimer, &WheelTimerType);
    if (t == NULL) {
        return NULL;
    }
    t->callback = Py_NewRef(fastargs[1]);
    t->args = Py_NewRef(fastargs[2]);
    /* context=None means "no isolation": dispatch calls the callback directly,
     * skipping context.run and the per-timer copy_context the caller would pay. */
    t->context = (fastargs[3] == Py_None) ? NULL : Py_NewRef(fastargs[3]);
    t->deadline = deadline;
    t->slot = slot;
    t->wheel = w;
    wheel_insert(w, t, slot);
    return Py_NewRef((PyObject *)t);
}

/* Timers carry absolute tick deadlines and the interval tree tracks each
 * slot's minimum, so both drain loops jump the cursor straight from due tick
 * to due tick: an advance over an idle gap -- one quiet poll or a machine
 * suspend alike -- is O(1) in elapsed ticks and never touches parked timers.
 * A deadline maps to exactly one slot, so draining ticks in tree-minimum
 * order yields callbacks in exact deadline order with no catch-up path. */

/* advance(now_seconds) -> list of callbacks now due (removed from the wheel) */
static PyObject *
wheel_advance(PyObject *op, PyObject *arg)
{
    TimingWheel *w = (TimingWheel *)op;
    double now = PyFloat_AsDouble(arg);
    if (now == -1.0 && PyErr_Occurred()) {
        return NULL;
    }
    int64_t target = (int64_t)(
        (now - w->base) * w->inverse_resolution);
    PyObject *due = PyList_New(0);
    if (due == NULL) {
        return NULL;
    }
    while (w->cursor < target) {
        int64_t next = w->deadline_tree[1];
        if (next > target) {
            w->cursor = target;
            break;
        }
        /* next > cursor by construction (due nodes never outlive their tick);
         * the +1 arm only guards termination against a corrupted tree. */
        w->cursor = next > w->cursor ? next : w->cursor + 1;
        int slot = wheel_slot(w, w->cursor);
        /* Pop while the slot's minimum is due. The heap root *is* the earliest
         * deadline, so a slot holding a thousand later deadlines costs nothing
         * to skip -- which is the whole difference from the chain this
         * replaced, where every sweep walked every node to find the few due. */
        WheelTimer *t;
        while ((t = w->slots[slot]) != NULL && t->deadline <= w->cursor) {
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
    }
    return due;
}

static void
wheel_dispatch_callback(WheelTimer *timer)
{
    PyObject *result;
    if (timer->context != NULL) {
        PyObject *run = PyObject_GetAttr(timer->context, g_s_context_run);
        if (run == NULL) {
            PyErr_WriteUnraisable(timer->callback);
            return;
        }
        Py_ssize_t count = timer->args
            ? PyTuple_GET_SIZE(timer->args) : 0;
        PyObject *local_args[9];
        PyObject **call_args = local_args;
        if (count > 8) {
            call_args = PyMem_Malloc(
                (size_t)(count + 1) * sizeof(PyObject *));
            if (call_args == NULL) {
                Py_DECREF(run);
                PyErr_NoMemory();
                PyErr_WriteUnraisable(timer->callback);
                return;
            }
        }
        call_args[0] = timer->callback;
        for (Py_ssize_t index = 0; index < count; index++) {
            call_args[index + 1] = PyTuple_GET_ITEM(timer->args, index);
        }
        result = PyObject_Vectorcall(
            run, call_args, (size_t)count + 1, NULL);
        if (call_args != local_args) {
            PyMem_Free(call_args);
        }
        Py_DECREF(run);
    } else if (timer->args != NULL) {
        result = PyObject_Call(timer->callback, timer->args, NULL);
    } else {
        result = PyObject_CallNoArgs(timer->callback);
    }
    if (result == NULL) {
        PyErr_WriteUnraisable(timer->callback);
    } else {
        Py_DECREF(result);
    }
}

/* Dispatch due timers without allocating an argument object. ReactorPoller uses
 * this directly, while advance_run() remains the Python-visible wrapper.
 *
 * Due nodes are unlinked from the slot chain first and dispatched after the
 * walk, so a callback that schedules into or cancels out of this slot mutates
 * a chain the walk no longer holds pointers into. While a node waits on the
 * pending list it is recognizable by slot < 0 with callback != NULL;
 * timer_cancel leaves the wheel's reference with the list for that state and
 * clearing the callback already makes the dispatch a no-op. */
Py_ssize_t
wreath_wheel_run_due(TimingWheel *w, double now)
{
    int64_t target = (int64_t)(
        (now - w->base) * w->inverse_resolution);
    Py_ssize_t fired = 0;
    while (w->cursor < target) {
        int64_t next = w->deadline_tree[1];
        if (next > target) {
            w->cursor = target;
            break;
        }
        w->cursor = next > w->cursor ? next : w->cursor + 1;
        int slot = wheel_slot(w, w->cursor);
        WheelTimer *pending = NULL;
        WheelTimer **pending_tail = &pending;
        /* Drain the slot's due prefix. Popping the root repeatedly yields the
         * due nodes in deadline order and never touches the ones that are not
         * due, so a crowded slot costs the same as an empty one plus the work
         * actually dispatched. */
        WheelTimer *t;
        while ((t = w->slots[slot]) != NULL && t->deadline <= w->cursor) {
            timer_unlink(t);       /* clears t->next */
            *pending_tail = t;     /* reuse the link as the dispatch list */
            pending_tail = &t->next;
        }
        while (pending != NULL) {
            WheelTimer *nxt = pending->next;
            pending->next = NULL;
            if (pending->callback != NULL) {
                wheel_dispatch_callback(pending);
                Py_CLEAR(pending->callback);
                fired++;
            }
            Py_DECREF(pending);
            pending = nxt;
        }
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
    Py_ssize_t fired = wreath_wheel_run_due((TimingWheel *)op, now);
    return fired < 0 ? NULL : PyLong_FromSsize_t(fired);
}

static PyObject *
wheel_count(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((TimingWheel *)op)->count);
}

static PyObject *
wheel_slot_rescans(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((TimingWheel *)op)->slot_rescans);
}

static PyObject *
wheel_tree_node_updates(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((TimingWheel *)op)->tree_node_updates);
}

static PyGetSetDef wheel_getset[] = {
    {"count", wheel_count, NULL, "number of live timers", NULL},
    {"slot_rescans", wheel_slot_rescans, NULL,
     "timer buckets rescanned after removal of their minimum", NULL},
    {"tree_node_updates", wheel_tree_node_updates, NULL,
     "segment-tree nodes updated", NULL},
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

PyTypeObject TimingWheelType = {
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


int
wreath_reactor_timers_ready(void)
{
    if (g_s_context_run == NULL) {
        g_s_context_run = PyUnicode_InternFromString("run");
        if (g_s_context_run == NULL) {
            return -1;
        }
    }
    return PyType_Ready(&WheelTimerType) < 0 ||
           PyType_Ready(&TimingWheelType) < 0 ||
           PyType_Ready(&RTimerType) < 0 ||
           PyType_Ready(&HeapStoreType) < 0 ||
           PyType_Ready(&HierStoreType) < 0 ||
           PyType_Ready(&FifoStoreType) < 0
        ? -1 : 0;
}

int
wreath_reactor_timers_add(PyObject *module)
{
    return PyModule_AddObjectRef(module, "TimingWheel", (PyObject *)&TimingWheelType) < 0 ||
           PyModule_AddObjectRef(module, "WheelTimer", (PyObject *)&WheelTimerType) < 0 ||
           PyModule_AddObjectRef(module, "HeapStore", (PyObject *)&HeapStoreType) < 0 ||
           PyModule_AddObjectRef(module, "HierStore", (PyObject *)&HierStoreType) < 0 ||
           PyModule_AddObjectRef(module, "FifoStore", (PyObject *)&FifoStoreType) < 0
        ? -1 : 0;
}
