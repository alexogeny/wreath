#ifndef WREATH_REACTOR_INTERNAL_H
#define WREATH_REACTOR_INTERNAL_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

typedef struct TimingWheel TimingWheel;

typedef struct WheelTimer {
    PyObject_HEAD
    PyObject *callback;
    PyObject *args;
    PyObject *context;
    int64_t deadline;
    int slot;
    struct WheelTimer *prev;
    struct WheelTimer *next;
    TimingWheel *wheel;
} WheelTimer;

struct TimingWheel {
    PyObject_HEAD
    WheelTimer **slots;
    int64_t *deadline_tree;
    uint32_t *min_ties;    /* per slot: live nodes tying the slot's tree minimum */
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
