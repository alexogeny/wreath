#ifndef WREATH_REACTOR_INTERNAL_H
#define WREATH_REACTOR_INTERNAL_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

/* The reactor's ablation axes, kept here because they are a considered
 * decomposition and re-deriving one costs more than recording it:
 *
 *     deadline_lanes, connection_core, buffer_ladder, completion_batch,
 *     send_chain, fixed_files, adaptive_policy, uring_timeout
 *
 * They named the isolated research tier that twice built and was twice removed
 * -- `_exp`, whose transport ablation was reverted after measurement, and
 * `forge`, which measured io_uring linked timeouts against this wheel and found
 * them ~12x more expensive per deadline on the dominant scheduled-then-cancelled
 * path (a linked timeout doubles the SQEs and doubles the completions). That
 * result is recorded; the tier is not, because a tier is a place to answer a
 * question and the question was answered.
 *
 * This is a list of axes worth ablating, not a build input. Both removed tiers
 * left a builder behind naming sources that did not exist, so if one of these
 * ever becomes real, give it a file before it gets a name in `setup.py`. */

typedef struct TimingWheel TimingWheel;

/* A node in one slot's pairing heap.
 *
 * `child` is the first child, `next` the next sibling, and `prev` is the parent
 * when this node is a first child and the previous sibling otherwise -- the two
 * cases are told apart by `prev->child == node`. A root has `prev` and `next`
 * NULL.
 *
 * The third pointer is what buys arbitrary removal in O(log n). Cancelling a
 * timer splices a node out of the middle of the heap, and without a way back up
 * the structure that costs a walk. Cancellation is the dominant operation on a
 * request deadline, so it is the one the layout is chosen for. */
typedef struct WheelTimer {
    PyObject_HEAD
    PyObject *callback;
    PyObject *args;
    PyObject *context;
    int64_t deadline;
    int slot;
    struct WheelTimer *prev;
    struct WheelTimer *next;
    struct WheelTimer *child;
    TimingWheel *wheel;
} WheelTimer;

struct TimingWheel {
    PyObject_HEAD
    WheelTimer **slots;    /* per slot: the root of that slot's pairing heap */
    int64_t *deadline_tree;
    int nslots;
    int tree_base;
    int slot_mask;
    double resolution;
    double inverse_resolution;
    double base;
    int64_t cursor;
    int64_t next_deadline;
    Py_ssize_t count;
    uint64_t slot_rescans;
    uint64_t tree_node_updates;
};

extern PyTypeObject TimingWheelType;

double wreath_wheel_next_when(TimingWheel *wheel);
Py_ssize_t wreath_wheel_run_due(TimingWheel *wheel, double now);
int wreath_reactor_timers_ready(void);
int wreath_reactor_timers_add(PyObject *module);

#endif
