#ifndef WREATH_POSTGRES_SLAB_H
#define WREATH_POSTGRES_SLAB_H

#include <Python.h>

#define WREATH_PG_SLAB_SIZE (64 * 1024)

typedef struct {
    PyObject_HEAD
    Py_ssize_t read_position;
    Py_ssize_t write_position;
    unsigned char data[WREATH_PG_SLAB_SIZE];
} WreathPgSlab;

extern PyTypeObject *WreathPgSlabType;
WreathPgSlab *wreath_pg_slab_new(void);
PyObject *wreath_pg_slab_writable_view(WreathPgSlab *slab);
PyObject *wreath_pg_slab_view(WreathPgSlab *slab, Py_ssize_t start, Py_ssize_t length);
PyObject *wreath_pg_chained_payload(PyObject *owners, const char *data, Py_ssize_t length);
int wreath_pg_slab_init(PyObject *module);

#endif
