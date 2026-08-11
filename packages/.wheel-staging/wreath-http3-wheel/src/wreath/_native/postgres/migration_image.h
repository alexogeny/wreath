#ifndef WREATH_POSTGRES_MIGRATION_IMAGE_H
#define WREATH_POSTGRES_MIGRATION_IMAGE_H

#include <Python.h>
#include <stdint.h>

uint64_t wreath_pg_migration_object_id(
    uint32_t kind,
    const unsigned char *schema, Py_ssize_t schema_length,
    const unsigned char *table, Py_ssize_t table_length,
    const unsigned char *name, Py_ssize_t name_length
);
uint32_t wreath_pg_migration_signature(
    const unsigned char *value, Py_ssize_t length
);
int wreath_pg_migration_catalog_check(PyObject *object);
int wreath_pg_migration_catalog_decode(
    PyObject *plan, PyObject *tape, PyObject *destination, Py_ssize_t limit
);
int wreath_pg_migration_image_init(PyObject *module);

#endif
