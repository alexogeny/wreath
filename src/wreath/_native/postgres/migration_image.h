#ifndef WREATH_POSTGRES_MIGRATION_IMAGE_H
#define WREATH_POSTGRES_MIGRATION_IMAGE_H

#include <Python.h>

int wreath_pg_migration_catalog_check(PyObject *object);
int wreath_pg_migration_catalog_decode(
    PyObject *plan, PyObject *tape, PyObject *destination, Py_ssize_t limit
);
int wreath_pg_migration_image_init(PyObject *module);

#endif
