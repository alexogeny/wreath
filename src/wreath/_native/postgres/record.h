#ifndef WREATH_POSTGRES_RECORD_H
#define WREATH_POSTGRES_RECORD_H

#include <Python.h>
#include "../record_api.h"

extern PyTypeObject *WreathPgRecordType;
extern PyTypeObject *WreathPgRecordBatchType;
PyObject *wreath_pg_record_create(PyObject *names, PyObject *index, PyObject *values);
PyObject *wreath_pg_record_alloc(PyObject *names, PyObject *index, Py_ssize_t count);
void wreath_pg_record_set_value(PyObject *record, Py_ssize_t position, PyObject *value);
PyObject *wreath_pg_record_batch_new(void);
int wreath_pg_record_batch_append(PyObject *batch, PyObject *value);
int wreath_pg_record_batch_check(PyObject *batch);
Py_ssize_t wreath_pg_record_batch_size(PyObject *batch);
void wreath_pg_record_batch_truncate(PyObject *batch, Py_ssize_t size);
int wreath_pg_record_batch_prepare(PyObject *batch, PyObject *names,
                                   PyObject *index, Py_ssize_t columns,
                                   Py_ssize_t rows, Py_ssize_t *start);
void wreath_pg_record_batch_set_value(PyObject *batch, Py_ssize_t row,
                                      Py_ssize_t column, PyObject *value);
void wreath_pg_record_batch_commit(PyObject *batch, Py_ssize_t size);
int wreath_pg_record_init(PyObject *module);
void wreath_pg_record_fini(void);

#endif
