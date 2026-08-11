#ifndef WREATH_POSTGRES_TAPE_H
#define WREATH_POSTGRES_TAPE_H

#include <Python.h>
#include <stdint.h>

typedef struct {
    uint32_t slab_index;
    uint32_t offset;
    int32_t length;
} WreathPgFieldRef;

/* Field tape: a flat array of field refs plus the slab objects owning the bytes
 * they point into.
 *
 * Consumption advances logical cursors rather than shifting the arrays. A
 * result drained one row at a time would otherwise memmove every surviving ref
 * and rebase every owner index on each call, which is quadratic in the row
 * count. Physical storage is reclaimed by occasional compaction instead.
 *
 * Layout invariants:
 *   - logical ref (row, column) lives at refs[ref_head + row * stored_columns + column]
 *     (use wreath_pg_tape_ref; no caller may assume refs[0] is logical row zero);
 *   - ref_count and row_count are LIVE counts; physical use is ref_head + ref_count;
 *   - WreathPgFieldRef.slab_index is a physical index into `owners`, rebased once
 *     whenever owner compaction moves the physical base;
 *   - owners[0 .. owner_head) are consumed and unreferenced, awaiting compaction.
 */
typedef struct {
    PyObject_HEAD
    uint32_t source_columns;
    uint32_t stored_columns;
    Py_ssize_t row_count;    /* live rows */
    Py_ssize_t ref_count;    /* live refs == row_count * stored_columns */
    Py_ssize_t ref_head;     /* physical index of logical ref zero */
    Py_ssize_t ref_capacity;
    WreathPgFieldRef *refs;
    PyObject *owners;
    Py_ssize_t owner_head;   /* physical index of the first live owner */
} WreathPgFieldTape;

/* The one supported way to reach a logical field. */
static inline WreathPgFieldRef *
wreath_pg_tape_ref(WreathPgFieldTape *tape, Py_ssize_t row, Py_ssize_t column)
{
    return &tape->refs[tape->ref_head + row * (Py_ssize_t)tape->stored_columns + column];
}

/* Resolve a ref's stored owner index to its slab object. */
static inline PyObject *
wreath_pg_tape_owner(WreathPgFieldTape *tape, uint32_t stored_owner_index)
{
    return PyList_GET_ITEM(tape->owners, (Py_ssize_t)stored_owner_index);
}

extern PyTypeObject *WreathPgFieldTapeType;
int wreath_pg_tape_append_payload(
    WreathPgFieldTape *tape, PyObject *payload, unsigned int selected
);
int wreath_pg_tape_append_raw(
    WreathPgFieldTape *tape, PyObject *owner, const unsigned char *data,
    Py_ssize_t length, Py_ssize_t base_offset, unsigned int selected
);
int wreath_pg_tape_consume(WreathPgFieldTape *tape, Py_ssize_t rows);
int wreath_pg_tape_init(PyObject *module);

#endif
