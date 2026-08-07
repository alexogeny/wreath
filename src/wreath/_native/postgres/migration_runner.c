/* One-statement transactional DDL blocks from authoritative WMS1 tapes. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#include "../byteorder.h"
#include <stdio.h>
#include <string.h>

#include "buffer.h"
#include "migration_runner.h"

#define SQL_FLAG_DESTRUCTIVE 1U
#define SQL_FLAG_MANUAL 2U



static int
append_literal(WreathPgBuffer *buffer, const char *value)
{
    return wreath_pg_buffer_append(buffer, value, (Py_ssize_t)strlen(value));
}


static int
contains_bytes(
    const unsigned char *haystack,
    Py_ssize_t haystack_length,
    const char *needle,
    Py_ssize_t needle_length)
{
    if (needle_length == 0 || needle_length > haystack_length) return 0;
    for (Py_ssize_t index = 0; index <= haystack_length - needle_length; index++) {
        if (memcmp(haystack + index, needle, (size_t)needle_length) == 0) return 1;
    }
    return 0;
}


static int
select_delimiter(
    const unsigned char *tape,
    Py_ssize_t length,
    char delimiter[48],
    Py_ssize_t *delimiter_length)
{
    for (uint32_t suffix = 0; suffix < 1000000U; suffix++) {
        const int written = snprintf(
            delimiter, 48, "$wreath_migration_%u$", suffix);
        if (written < 0 || written >= 48) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "could not construct a bounded PostgreSQL migration delimiter");
            return -1;
        }
        if (!contains_bytes(tape, length, delimiter, written)) {
            *delimiter_length = written;
            return 0;
        }
    }
    PyErr_SetString(
        PyExc_ValueError,
        "cannot render migration DDL: every bounded dollar-quote delimiter appears in the SQL tape");
    return -1;
}


static PyObject *
migration_build_ddl_block(PyObject *module, PyObject *args)
{
    const unsigned char *tape;
    Py_ssize_t length, offset = 12;
    int allow_destructive;
    uint32_t count;
    char delimiter[48];
    Py_ssize_t delimiter_length;
    WreathPgBuffer output = {0};
    PyObject *result = NULL;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "y#p:_migration_build_ddl_block",
            &tape, &length, &allow_destructive)) return NULL;
    if (length < 12) {
        PyErr_Format(
            PyExc_ValueError,
            "invalid WMS1 SQL tape: truncated header (%zd bytes; need at least 12)",
            length);
        return NULL;
    }
    if (memcmp(tape, "WMS1", 4) != 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid SQL tape: expected WMS1 magic in the first four bytes");
        return NULL;
    }
    if (wreath_load_u32_le(tape + 4) != 1) {
        PyErr_Format(
            PyExc_ValueError,
            "invalid WMS1 SQL tape: format field is %u; expected 1",
            wreath_load_u32_le(tape + 4));
        return NULL;
    }
    count = wreath_load_u32_le(tape + 8);
    if (select_delimiter(tape, length, delimiter, &delimiter_length) < 0) return NULL;
    if (append_literal(&output, "DO ") < 0 ||
        wreath_pg_buffer_append(&output, delimiter, delimiter_length) < 0 ||
        append_literal(&output, "\nBEGIN\n") < 0) goto done;
    for (uint32_t index = 0; index < count; index++) {
        uint32_t flags, sql_length;
        if (length - offset < 8) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid WMS1 SQL tape: statement %u lacks its 8-byte header",
                index);
            goto done;
        }
        flags = wreath_load_u32_le(tape + offset);
        sql_length = wreath_load_u32_le(tape + offset + 4);
        offset += 8;
        if ((flags & ~3U) != 0 || sql_length > (uint32_t)(length - offset)) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid WMS1 SQL tape: statement %u has flags %u or length %u outside the remaining tape",
                index, flags, sql_length);
            goto done;
        }
        if ((flags & SQL_FLAG_MANUAL) != 0) {
            PyErr_Format(
                PyExc_ValueError,
                "cannot apply migration: operation %u is marked MANUAL and has no authoritative SQL",
                index + 1);
            goto done;
        }
        if ((flags & SQL_FLAG_DESTRUCTIVE) != 0 && !allow_destructive) {
            PyErr_Format(
                PyExc_PermissionError,
                "cannot apply migration: operation %u is destructive; pass explicit destructive approval",
                index + 1);
            goto done;
        }
        if (sql_length == 0) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid WMS1 SQL tape: executable operation %u has an empty SQL statement",
                index + 1);
            goto done;
        }
        {
            char marker[48];
            const int marker_length = snprintf(
                marker, sizeof(marker), "  -- Wreath operation %u\n  ", index + 1);
            if (marker_length < 0 || marker_length >= (int)sizeof(marker)) {
                PyErr_Format(
                    PyExc_RuntimeError,
                    "could not label Wreath migration operation %u in the DDL block",
                    index + 1);
                goto done;
            }
            if (wreath_pg_buffer_append(&output, marker, marker_length) < 0 ||
                wreath_pg_buffer_append(&output, tape + offset, sql_length) < 0 ||
                append_literal(&output, "\n") < 0) goto done;
        }
        offset += sql_length;
    }
    if (offset != length) {
        PyErr_Format(
            PyExc_ValueError,
            "invalid WMS1 SQL tape: %zd trailing bytes remain after %u statements",
            length - offset, count);
        goto done;
    }
    if (append_literal(&output, "END\n") < 0 ||
        wreath_pg_buffer_append(&output, delimiter, delimiter_length) < 0 ||
        append_literal(&output, ";") < 0) goto done;
    result = PyUnicode_DecodeUTF8(output.data, output.length, "strict");

done:
    wreath_pg_buffer_clear(&output);
    return result;
}


static PyMethodDef migration_runner_methods[] = {
    {"_migration_build_ddl_block", migration_build_ddl_block, METH_VARARGS,
     PyDoc_STR("Build one authoritative PostgreSQL DDL block from WMS1.")},
    {NULL, NULL, 0, NULL},
};


int
wreath_pg_migration_runner_init(PyObject *module)
{
    return PyModule_AddFunctions(module, migration_runner_methods);
}
