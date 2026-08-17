#ifndef WREATH_POSTGRES_DECODE_H
#define WREATH_POSTGRES_DECODE_H

#include <Python.h>
#include <stdint.h>

#include "tape.h"

/* Where decoded rows land. Records are the driver's own result type; models are
   hydrated straight into fixed-size cells with no Record in between. */
typedef enum {
    WREATH_PG_DEST_RECORD = 0,
    WREATH_PG_DEST_MODEL = 1
} WreathPgDestinationKind;

/* Decodes one field's wire bytes into a Python object. */
typedef PyObject *(*WreathPgRawDecoder)(
    const unsigned char *data, Py_ssize_t length, int format, uint32_t oid
);

typedef struct {
    uint32_t oid;
    uint16_t format;
    uint16_t destination;
    WreathPgRawDecoder decoder;
} WreathPgColumnDecoder;

/* Compiled once per result shape and reused for every row of it. */
typedef struct {
    PyObject_HEAD
    Py_ssize_t column_count;
    Py_ssize_t decoder_selections;
    WreathPgColumnDecoder *columns;
    PyObject *names;
    PyObject *name_index;
} WreathPgDecoderPlan;

extern PyTypeObject *WreathPgDecoderPlanType;
PyObject *wreath_pg_decoder_plan_new(PyObject *oids, PyObject *formats, PyObject *names);
int wreath_pg_decode_fetch_extend(PyObject *plan_object, PyObject *tape_object,
                               Py_ssize_t limit, PyObject *dest);
int wreath_pg_decode_datarow_batch(PyObject *plan_object, PyObject *batch,
                                  const unsigned char *data,
                                  Py_ssize_t length);
PyObject *wreath_pg_decode_fetchval(PyObject *plan_object,
                                   PyObject *tape_object);
int wreath_pg_decode_init(PyObject *module);

/* Pick the decoder for an OID/format pair. Shared so alternative destinations
   reuse one codec table rather than growing a second one. */
WreathPgRawDecoder wreath_pg_select_decoder(uint32_t oid, int format);

/* Acquire buffers for the slabs the first `rows` rows reference. Owner entries
   are deduplicated by the tape, so each slab is acquired exactly once. */
Py_buffer *wreath_pg_acquire_owner_buffers(WreathPgFieldTape *tape, Py_ssize_t rows,
                                        Py_ssize_t *owner_limit_out);
void wreath_pg_release_owner_buffers(Py_buffer *buffers, Py_ssize_t owner_limit);

#endif
