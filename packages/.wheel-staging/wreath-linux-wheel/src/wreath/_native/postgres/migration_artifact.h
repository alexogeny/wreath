#ifndef WREATH_POSTGRES_MIGRATION_ARTIFACT_H
#define WREATH_POSTGRES_MIGRATION_ARTIFACT_H

#include <Python.h>

void wreath_pg_sha256(
    const unsigned char *data, Py_ssize_t length, unsigned char digest[32]
);
int wreath_pg_migration_artifact_init(PyObject *module);

#endif
