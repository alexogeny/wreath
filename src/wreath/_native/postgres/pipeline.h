#ifndef WREATH_POSTGRES_PIPELINE_H
#define WREATH_POSTGRES_PIPELINE_H

#include <Python.h>

/* Methods grafted onto the native Connection type; see pipeline.c. */
PyMethodDef *wreath_pg_pipeline_methods(void);
int wreath_pg_pipeline_init(PyObject *module, PyObject *connection_type);
void wreath_pg_pipeline_fini(void);
int wreath_pg_pipeline_complete_fetchval(PyObject *connection,
                                         PyObject *operation,
                                         PyObject *tape,
                                         PyObject *plan,
                                         char transaction_status);

#endif
