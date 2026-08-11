/* Shared bounded deadline scheduler.
 *
 * A binary min-heap owns strong references to waiters and gives native users a
 * common O(log n) deadline queue. Capacity is fixed at construction so overload
 * is explicit instead of growing an unbounded Python list under pressure.
 */
#include "wreathcore.h"

typedef struct {
    PyObject_HEAD
    PyObject **waiters;
    double *deadlines;
    Py_ssize_t size;
    Py_ssize_t capacity;
} WreathScheduler;

static void
scheduler_swap(WreathScheduler *self, Py_ssize_t a, Py_ssize_t b)
{
    PyObject *waiter = self->waiters[a];
    double deadline = self->deadlines[a];
    self->waiters[a] = self->waiters[b];
    self->deadlines[a] = self->deadlines[b];
    self->waiters[b] = waiter;
    self->deadlines[b] = deadline;
}

static PyObject *
scheduler_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    Py_ssize_t capacity = 1024;
    WreathScheduler *self;
    static char *names[] = {"capacity", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|n", names, &capacity)) return NULL;
    if (capacity <= 0) {
        PyErr_SetString(PyExc_ValueError, "capacity must be positive");
        return NULL;
    }
    self = (WreathScheduler *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->waiters = PyMem_Calloc((size_t)capacity, sizeof(PyObject *));
    self->deadlines = PyMem_Malloc((size_t)capacity * sizeof(double));
    self->size = 0;
    self->capacity = capacity;
    if (self->waiters == NULL || self->deadlines == NULL) {
        PyMem_Free(self->waiters);
        PyMem_Free(self->deadlines);
        self->waiters = NULL;
        self->deadlines = NULL;
        Py_DECREF(self);
        return PyErr_NoMemory();
    }
    return (PyObject *)self;
}

static int
scheduler_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathScheduler *self = (WreathScheduler *)op;
    for (Py_ssize_t i = 0; i < self->size; i++) Py_VISIT(self->waiters[i]);
    return 0;
}

static int
scheduler_clear(PyObject *op)
{
    WreathScheduler *self = (WreathScheduler *)op;
    for (Py_ssize_t i = 0; i < self->size; i++) Py_CLEAR(self->waiters[i]);
    self->size = 0;
    return 0;
}

static void
scheduler_dealloc(PyObject *op)
{
    WreathScheduler *self = (WreathScheduler *)op;
    PyObject_GC_UnTrack(op);
    scheduler_clear(op);
    PyMem_Free(self->waiters);
    PyMem_Free(self->deadlines);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
scheduler_schedule(WreathScheduler *self, PyObject *args)
{
    double deadline;
    PyObject *waiter;
    Py_ssize_t child;
    if (!PyArg_ParseTuple(args, "dO", &deadline, &waiter)) return NULL;
    if (self->size == self->capacity) {
        PyErr_SetString(PyExc_OverflowError, "native scheduler capacity exceeded");
        return NULL;
    }
    child = self->size++;
    self->deadlines[child] = deadline;
    self->waiters[child] = Py_NewRef(waiter);
    while (child > 0) {
        Py_ssize_t parent = (child - 1) / 2;
        if (self->deadlines[parent] <= deadline) break;
        scheduler_swap(self, parent, child);
        child = parent;
    }
    Py_RETURN_NONE;
}

static PyObject *
scheduler_pop_due(WreathScheduler *self, PyObject *args)
{
    double now;
    Py_ssize_t limit = 64;
    PyObject *result;
    if (!PyArg_ParseTuple(args, "d|n", &now, &limit)) return NULL;
    if (limit < 0) {
        PyErr_SetString(PyExc_ValueError, "limit must be non-negative");
        return NULL;
    }
    result = PyList_New(0);
    if (result == NULL) return NULL;
    while (self->size && limit-- && self->deadlines[0] <= now) {
        PyObject *waiter = self->waiters[0];
        Py_ssize_t parent = 0;
        self->size--;
        if (PyList_Append(result, waiter) < 0) {
            Py_DECREF(waiter);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(waiter);
        if (self->size == 0) break;
        self->waiters[0] = self->waiters[self->size];
        self->deadlines[0] = self->deadlines[self->size];
        while (1) {
            Py_ssize_t left = parent * 2 + 1;
            Py_ssize_t right = left + 1;
            Py_ssize_t child;
            if (left >= self->size) break;
            child = right < self->size && self->deadlines[right] < self->deadlines[left]
                ? right : left;
            if (self->deadlines[parent] <= self->deadlines[child]) break;
            scheduler_swap(self, parent, child);
            parent = child;
        }
    }
    return result;
}

static PyObject *scheduler_length(WreathScheduler *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->size);
}

static PyMethodDef scheduler_methods[] = {
    {"schedule", (PyCFunction)scheduler_schedule, METH_VARARGS, NULL},
    {"pop_due", (PyCFunction)scheduler_pop_due, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};
static PyGetSetDef scheduler_getset[] = {
    {"size", (getter)scheduler_length, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};
static PyType_Slot scheduler_slots[] = {
    {Py_tp_new, scheduler_new}, {Py_tp_dealloc, scheduler_dealloc},
    {Py_tp_traverse, scheduler_traverse}, {Py_tp_clear, scheduler_clear},
    {Py_tp_methods, scheduler_methods}, {Py_tp_getset, scheduler_getset},
    {0, NULL},
};
static PyType_Spec scheduler_spec = {
    .name = "wreath._native._core.NativeScheduler",
    .basicsize = sizeof(WreathScheduler),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = scheduler_slots,
};

int
wreath_register_scheduler(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&scheduler_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "NativeScheduler", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}
