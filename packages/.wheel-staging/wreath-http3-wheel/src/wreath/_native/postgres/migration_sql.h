#ifndef WREATH_POSTGRES_MIGRATION_SQL_H
#define WREATH_POSTGRES_MIGRATION_SQL_H

#include <Python.h>

PyObject *wreath_pg_migration_operations_from_plan(
    const unsigned char *plan, Py_ssize_t plan_length
);
PyObject *wreath_pg_migration_render_sql(
    const unsigned char *plan, Py_ssize_t plan_length
);
int wreath_pg_migration_sql_init(PyObject *module);

#endif
