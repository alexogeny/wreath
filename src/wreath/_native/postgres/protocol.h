#ifndef WREATH_POSTGRES_PROTOCOL_H
#define WREATH_POSTGRES_PROTOCOL_H

#include <Python.h>

int wreath_pg_protocol_init(PyObject *module);
void wreath_pg_protocol_fini(void);
int wreath_pg_protocol_register_operations(
    PyObject *protocol, PyObject *operations);
int wreath_pg_protocol_register_operation(
    PyObject *protocol, PyObject *operation);
int wreath_pg_protocol_register_operation_parts(
    PyObject *protocol, PyObject *operation, PyObject *tape,
    PyObject *plan, PyObject *rows, PyObject *dest, long mode);

#endif
