#include "operation.h"

PyObject *WreathPgOperationType = NULL;

static PyType_Slot operation_slots[] = {
    {0, NULL}
};

static PyType_Spec operation_spec = {
    .name = "wreath._native._postgres.Operation",
    .basicsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .slots = operation_slots,
};

int
wreath_pg_operation_init(PyObject *module)
{
    PyObject *pure = PyImport_ImportModule("wreath._pure.postgres");
    PyObject *base;
    PyObject *bases;
    int result;
    if (pure == NULL) return -1;
    base = PyObject_GetAttrString(pure, "Operation");
    Py_DECREF(pure);
    if (base == NULL) return -1;
    bases = PyTuple_Pack(1, base);
    Py_DECREF(base);
    if (bases == NULL) return -1;
    WreathPgOperationType = PyType_FromSpecWithBases(&operation_spec, bases);
    Py_DECREF(bases);
    if (WreathPgOperationType == NULL) return -1;
    result = PyModule_AddObjectRef(module, "Operation", WreathPgOperationType);
    if (result == 0) Py_DECREF(WreathPgOperationType);
    return result;
}
