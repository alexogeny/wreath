#ifndef WREATH_POSTGRES_CODEC_H
#define WREATH_POSTGRES_CODEC_H

#include <Python.h>
#include <stdint.h>

#include "buffer.h"

PyObject *wreath_pg_encode_text_value(PyObject *value, uint32_t oid);
PyObject *wreath_pg_encode_binary_value(PyObject *value, uint32_t oid);
int wreath_pg_encode_binary_into(WreathPgBuffer *output, PyObject *value, uint32_t oid);
PyObject *wreath_pg_decode_value(uint32_t oid, int format, PyObject *data);
/* Decode PostgreSQL hex-format `bytea` text, excluding the leading "\x"
 * marker. Returns an exact bytes object, or NULL with ValueError set on an odd
 * length or a non-hex byte. */
PyObject *wreath_pg_decode_hex_bytea(const unsigned char *data, Py_ssize_t length);
int wreath_pg_codec_init(PyObject *module);
void wreath_pg_codec_fini(void);

/* Temporal conversions shared with the model cell storage, so a value the ORM
   accepts into a cell is by construction a value this codec can bind. */
int wreath_pg_check_exact_date(PyObject *value);
int wreath_pg_check_timestamp(PyObject *value, int aware);
int wreath_pg_date_days(PyObject *value, int64_t *out);
int wreath_pg_timestamp_micros(PyObject *value, int aware, int64_t *out);
PyObject *wreath_pg_date_from_days(int64_t days);
PyObject *wreath_pg_timestamp_from_micros(int64_t micros, int aware);
PyObject *wreath_pg_uuid_from_bytes(const unsigned char *data);
int wreath_pg_uuid_bytes(PyObject *value, unsigned char *out);

#endif
