#ifndef WREATH_POSTGRES_CONNECTION_H
#define WREATH_POSTGRES_CONNECTION_H
#include <Python.h>
int wreath_pg_connection_init(PyObject *module);
void wreath_pg_connection_fini(void);
#endif
