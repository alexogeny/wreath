#ifndef WREATH_POSTGRES_RECORD_H
#define WREATH_POSTGRES_RECORD_H

#include <Python.h>

extern PyTypeObject *WreathPgRecordType;
PyObject *wreath_pg_record_create(PyObject *names, PyObject *index, PyObject *values);
PyObject *wreath_pg_record_alloc(PyObject *names, PyObject *index, Py_ssize_t count);
void wreath_pg_record_set_value(PyObject *record, Py_ssize_t position, PyObject *value);
int wreath_pg_record_init(PyObject *module);
void wreath_pg_record_fini(void);

#endif
