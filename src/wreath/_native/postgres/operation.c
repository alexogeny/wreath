#include "operation.h"

#include <structmember.h>

/* One query flight.  The accelerated pipeline used to create an empty heap
 * subclass of wreath._pgdriver.Operation and then mutate the Python base's 24
 * slots from C.  That made the operation native in name only: its storage and
 * lifetime were still a Python implementation detail.  This exact C record
 * owns the same references until the future is completed; its member
 * descriptors preserve the internal attribute surface used by the reference
 * protocol and tests. */
typedef struct {
    PyObject_HEAD
    PyObject *args;
    PyObject *cold;
    PyObject *command;
    PyObject *deadline;
    PyObject *decoder_plan;
    PyObject *dest;
    PyObject *discarded;
    PyObject *error;
    PyObject *field_tape;
    PyObject *future;
    PyObject *have_value;
    PyObject *mode;
    PyObject *one_row;
    PyObject *one_value;
    PyObject *packet;
    PyObject *parameter_oids;
    PyObject *plan;
    PyObject *result_formats;
    PyObject *result_names;
    PyObject *result_oids;
    PyObject *rows;
    PyObject *sequence;
    PyObject *sql;
    PyObject *state;
    PyObject *statement_name;
} WreathPgOperation;

PyObject *WreathPgOperationType = NULL;
PyObject *WreathPgOperationQueueType = NULL;

/* A connection-owned FIFO for Operation references.  The Python driver keeps
 * deque as its independent reference implementation; the native Connection
 * substitutes this object at construction.  Its sequence and method surface
 * intentionally matches only what Connection uses, so tests and the remaining
 * Python lifecycle methods observe the same one source of truth while the C
 * pipeline can move references without materialising Python method calls. */
typedef struct {
    PyObject_HEAD
    PyObject **items;
    Py_ssize_t capacity;
    Py_ssize_t head;
    Py_ssize_t size;
} WreathPgOperationQueue;

static int
operation_queue_reserve(WreathPgOperationQueue *self)
{
    if (self->capacity > PY_SSIZE_T_MAX / 2) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t capacity = self->capacity == 0 ? 8 : self->capacity * 2;
    if ((size_t)capacity > SIZE_MAX / sizeof(PyObject *)) {
        PyErr_NoMemory();
        return -1;
    }
    PyObject **items = PyMem_Malloc((size_t)capacity * sizeof(PyObject *));
    if (items == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < self->size; i++) {
        items[i] = self->items[(self->head + i) % self->capacity];
    }
    PyMem_Free(self->items);
    self->items = items;
    self->capacity = capacity;
    self->head = 0;
    return 0;
}

int
wreath_pg_operation_queue_check(PyObject *queue)
{
    return WreathPgOperationQueueType != NULL &&
           Py_IS_TYPE(queue, (PyTypeObject *)WreathPgOperationQueueType);
}

Py_ssize_t
wreath_pg_operation_queue_size(PyObject *queue)
{
    return ((WreathPgOperationQueue *)queue)->size;
}

int
wreath_pg_operation_queue_append(PyObject *queue, PyObject *operation)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)queue;
    if (self->size == self->capacity && operation_queue_reserve(self) < 0) {
        return -1;
    }
    Py_ssize_t index = (self->head + self->size) % self->capacity;
    self->items[index] = Py_NewRef(operation);
    self->size++;
    return 0;
}

PyObject *
wreath_pg_operation_queue_popleft(PyObject *queue)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)queue;
    if (self->size == 0) {
        PyErr_SetString(PyExc_IndexError, "pop from an empty operation queue");
        return NULL;
    }
    PyObject *operation = self->items[self->head];
    self->items[self->head] = NULL;
    self->head = (self->head + 1) % self->capacity;
    self->size--;
    if (self->size == 0) self->head = 0;
    return operation;
}

PyObject *
wreath_pg_operation_queue_getitem(PyObject *queue, Py_ssize_t index)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)queue;
    if (index < 0) index += self->size;
    if (index < 0 || index >= self->size) {
        PyErr_SetString(PyExc_IndexError, "operation queue index out of range");
        return NULL;
    }
    return Py_NewRef(self->items[(self->head + index) % self->capacity]);
}

static PyObject *
operation_queue_append_method(PyObject *self, PyObject *operation)
{
    if (wreath_pg_operation_queue_append(self, operation) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
operation_queue_popleft_method(PyObject *self, PyObject *unused)
{
    (void)unused;
    return wreath_pg_operation_queue_popleft(self);
}

static PyObject *
operation_queue_clear_method(PyObject *op, PyObject *unused)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)op;
    (void)unused;
    while (self->size > 0) {
        PyObject *operation = wreath_pg_operation_queue_popleft(op);
        if (operation == NULL) return NULL;
        Py_DECREF(operation);
    }
    Py_RETURN_NONE;
}

static Py_ssize_t
operation_queue_length(PyObject *op)
{
    return ((WreathPgOperationQueue *)op)->size;
}

static PyObject *
operation_queue_item(PyObject *op, Py_ssize_t index)
{
    return wreath_pg_operation_queue_getitem(op, index);
}

static int
operation_queue_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)op;
    Py_VISIT(Py_TYPE(op));
    for (Py_ssize_t i = 0; i < self->size; i++) {
        Py_VISIT(self->items[(self->head + i) % self->capacity]);
    }
    return 0;
}

static int
operation_queue_clear(PyObject *op)
{
    WreathPgOperationQueue *self = (WreathPgOperationQueue *)op;
    while (self->size > 0) {
        PyObject *operation = wreath_pg_operation_queue_popleft(op);
        if (operation == NULL) {
            PyErr_Clear();
            break;
        }
        Py_DECREF(operation);
    }
    return 0;
}

static void
operation_queue_dealloc(PyObject *op)
{
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    operation_queue_clear(op);
    PyMem_Free(((WreathPgOperationQueue *)op)->items);
    type->tp_free(op);
    Py_DECREF(type);
}

static PyObject *
operation_queue_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    if ((args != NULL && PyTuple_GET_SIZE(args) != 0) ||
        (kwargs != NULL && PyDict_GET_SIZE(kwargs) != 0)) {
        PyErr_SetString(PyExc_TypeError, "OperationQueue takes no arguments");
        return NULL;
    }
    WreathPgOperationQueue *self =
        (WreathPgOperationQueue *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->items = NULL;
    self->capacity = 0;
    self->head = 0;
    self->size = 0;
    return (PyObject *)self;
}

static PyMethodDef operation_queue_methods[] = {
    {"append", operation_queue_append_method, METH_O, NULL},
    {"popleft", operation_queue_popleft_method, METH_NOARGS, NULL},
    {"clear", operation_queue_clear_method, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyType_Slot operation_queue_slots[] = {
    {Py_tp_new, operation_queue_new},
    {Py_tp_dealloc, operation_queue_dealloc},
    {Py_tp_traverse, operation_queue_traverse},
    {Py_tp_clear, operation_queue_clear},
    {Py_tp_methods, operation_queue_methods},
    {Py_sq_length, operation_queue_length},
    {Py_sq_item, operation_queue_item},
    {0, NULL},
};

static PyType_Spec operation_queue_spec = {
    .name = "wreath._native._postgres._OperationQueue",
    .basicsize = sizeof(WreathPgOperationQueue),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = operation_queue_slots,
};

#define OP_VISIT(name) Py_VISIT(self->name)
#define OP_CLEAR(name) Py_CLEAR(self->name)
#define OP_MEMBER(name) {#name, T_OBJECT_EX, offsetof(WreathPgOperation, name), 0, NULL}

static int
operation_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathPgOperation *self = (WreathPgOperation *)op;
    Py_VISIT(Py_TYPE(op));
    OP_VISIT(args); OP_VISIT(cold); OP_VISIT(command); OP_VISIT(deadline);
    OP_VISIT(decoder_plan); OP_VISIT(dest); OP_VISIT(discarded);
    OP_VISIT(error); OP_VISIT(field_tape); OP_VISIT(future);
    OP_VISIT(have_value); OP_VISIT(mode); OP_VISIT(one_row);
    OP_VISIT(one_value); OP_VISIT(packet); OP_VISIT(parameter_oids);
    OP_VISIT(plan); OP_VISIT(result_formats); OP_VISIT(result_names);
    OP_VISIT(result_oids); OP_VISIT(rows); OP_VISIT(sequence);
    OP_VISIT(sql); OP_VISIT(state); OP_VISIT(statement_name);
    return 0;
}

static int
operation_clear(PyObject *op)
{
    WreathPgOperation *self = (WreathPgOperation *)op;
    OP_CLEAR(args); OP_CLEAR(cold); OP_CLEAR(command); OP_CLEAR(deadline);
    OP_CLEAR(decoder_plan); OP_CLEAR(dest); OP_CLEAR(discarded);
    OP_CLEAR(error); OP_CLEAR(field_tape); OP_CLEAR(future);
    OP_CLEAR(have_value); OP_CLEAR(mode); OP_CLEAR(one_row);
    OP_CLEAR(one_value); OP_CLEAR(packet); OP_CLEAR(parameter_oids);
    OP_CLEAR(plan); OP_CLEAR(result_formats); OP_CLEAR(result_names);
    OP_CLEAR(result_oids); OP_CLEAR(rows); OP_CLEAR(sequence);
    OP_CLEAR(sql); OP_CLEAR(state); OP_CLEAR(statement_name);
    return 0;
}

static void
operation_dealloc(PyObject *op)
{
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    operation_clear(op);
    type->tp_free(op);
    Py_DECREF(type);
}

static PyMemberDef operation_members[] = {
    OP_MEMBER(args), OP_MEMBER(cold), OP_MEMBER(command), OP_MEMBER(deadline),
    OP_MEMBER(decoder_plan), OP_MEMBER(dest), OP_MEMBER(discarded),
    OP_MEMBER(error), OP_MEMBER(field_tape), OP_MEMBER(future),
    OP_MEMBER(have_value), OP_MEMBER(mode), OP_MEMBER(one_row),
    OP_MEMBER(one_value), OP_MEMBER(packet), OP_MEMBER(parameter_oids),
    OP_MEMBER(plan), OP_MEMBER(result_formats), OP_MEMBER(result_names),
    OP_MEMBER(result_oids), OP_MEMBER(rows), OP_MEMBER(sequence),
    OP_MEMBER(sql), OP_MEMBER(state), OP_MEMBER(statement_name),
    {NULL, 0, 0, 0, NULL},
};

static PyType_Slot operation_slots[] = {
    {Py_tp_new, PyType_GenericNew},
    {Py_tp_dealloc, operation_dealloc},
    {Py_tp_traverse, operation_traverse},
    {Py_tp_clear, operation_clear},
    {Py_tp_members, operation_members},
    {0, NULL},
};

static PyType_Spec operation_spec = {
    .name = "wreath._native._postgres.Operation",
    .basicsize = sizeof(WreathPgOperation),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    .slots = operation_slots,
};

int
wreath_pg_operation_init(PyObject *module)
{
    WreathPgOperationQueueType = PyType_FromSpec(&operation_queue_spec);
    if (WreathPgOperationQueueType == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "_OperationQueue", WreathPgOperationQueueType) < 0) return -1;
    Py_DECREF(WreathPgOperationQueueType);
    WreathPgOperationType = PyType_FromSpec(&operation_spec);
    if (WreathPgOperationType == NULL) return -1;
    int result = PyModule_AddObjectRef(module, "Operation", WreathPgOperationType);
    if (result == 0) Py_DECREF(WreathPgOperationType);
    return result;
}
