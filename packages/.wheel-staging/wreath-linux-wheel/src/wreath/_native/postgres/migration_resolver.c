/* Packed Wreath-metal migration readiness classification. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#include "migration_resolver.h"

#define WREATH_MIGRATION_ROW_SIZE 32
#define WREATH_HISTORY_UNKNOWN 0
#define WREATH_HISTORY_VERIFIED 1
#define WREATH_HISTORY_AMBIGUOUS 2
#define WREATH_HISTORY_BLOCKED 3

static uint32_t
read_u32_le(const unsigned char *value)
{
    return ((uint32_t)value[0]) |
           ((uint32_t)value[1] << 8) |
           ((uint32_t)value[2] << 16) |
           ((uint32_t)value[3] << 24);
}

static uint64_t
read_u64_le(const unsigned char *value)
{
    return ((uint64_t)read_u32_le(value)) |
           ((uint64_t)read_u32_le(value + 4) << 32);
}

static PyObject *
migration_resolve_managed(PyObject *self, PyObject *args)
{
    PyObject *snapshot;
    Py_buffer view = {0};
    unsigned long long target_migration;
    unsigned long long target_checksum;
    unsigned int directory_generation;
    uint64_t current = 0;
    uint64_t apply = 0;
    uint64_t verify = 0;
    uint64_t ambiguous = 0;
    uint64_t blocked = 0;
    Py_ssize_t offset;

    (void)self;
    if (!PyArg_ParseTuple(
            args,
            "OKKI:_migration_resolve_managed",
            &snapshot,
            &target_migration,
            &target_checksum,
            &directory_generation)) {
        return NULL;
    }
    if (PyObject_GetBuffer(snapshot, &view, PyBUF_CONTIG_RO) < 0) {
        return NULL;
    }
    if (view.len % WREATH_MIGRATION_ROW_SIZE != 0) {
        PyBuffer_Release(&view);
        PyErr_Format(
            PyExc_ValueError,
            "managed migration snapshot length must be a multiple of %d bytes",
            WREATH_MIGRATION_ROW_SIZE);
        return NULL;
    }

    for (offset = 0; offset < view.len; offset += WREATH_MIGRATION_ROW_SIZE) {
        const unsigned char *row = (const unsigned char *)view.buf + offset;
        const uint64_t migration = read_u64_le(row + 8);
        const uint64_t checksum = read_u64_le(row + 16);
        const uint32_t generation = read_u32_le(row + 24);
        const unsigned char status = row[28];

        if (status == WREATH_HISTORY_AMBIGUOUS) {
            ambiguous++;
        }
        else if (status == WREATH_HISTORY_BLOCKED) {
            blocked++;
        }
        else if (status != WREATH_HISTORY_VERIFIED ||
                 generation != directory_generation) {
            verify++;
        }
        else if (migration == (uint64_t)target_migration &&
                 checksum == (uint64_t)target_checksum) {
            current++;
        }
        else {
            apply++;
        }
    }
    PyBuffer_Release(&view);
    return Py_BuildValue(
        "(KKKKK)",
        (unsigned long long)current,
        (unsigned long long)apply,
        (unsigned long long)verify,
        (unsigned long long)ambiguous,
        (unsigned long long)blocked);
}

static PyMethodDef migration_resolver_methods[] = {
    {
        "_migration_resolve_managed",
        migration_resolve_managed,
        METH_VARARGS,
        PyDoc_STR("Classify a packed managed-fleet history snapshot."),
    },
    {NULL, NULL, 0, NULL},
};

int
wreath_pg_migration_resolver_init(PyObject *module)
{
    return PyModule_AddFunctions(module, migration_resolver_methods);
}
