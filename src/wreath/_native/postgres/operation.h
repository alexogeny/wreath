#ifndef WREATH_POSTGRES_OPERATION_H
#define WREATH_POSTGRES_OPERATION_H

#include <Python.h>

extern PyObject *WreathPgOperationType;
extern PyObject *WreathPgOperationQueueType;
int wreath_pg_operation_init(PyObject *module);
int wreath_pg_operation_queue_check(PyObject *queue);
Py_ssize_t wreath_pg_operation_queue_size(PyObject *queue);
int wreath_pg_operation_queue_append(PyObject *queue, PyObject *operation);
PyObject *wreath_pg_operation_queue_popleft(PyObject *queue);
PyObject *wreath_pg_operation_queue_getitem(PyObject *queue, Py_ssize_t index);

#endif
