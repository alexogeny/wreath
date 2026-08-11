#ifndef WREATH_POSTGRES_PROTOCOL_H
#define WREATH_POSTGRES_PROTOCOL_H

#include <Python.h>

int wreath_pg_protocol_init(PyObject *module);
void wreath_pg_protocol_fini(void);

#endif
