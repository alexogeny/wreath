#ifndef WREATH_POSTGRES_BUFFER_H
#define WREATH_POSTGRES_BUFFER_H

#include <Python.h>
#include <stdint.h>

typedef struct {
    char *data;
    Py_ssize_t length;
    Py_ssize_t capacity;
} WreathPgBuffer;

void wreath_pg_buffer_clear(WreathPgBuffer *buffer);
int wreath_pg_buffer_reserve(WreathPgBuffer *buffer, Py_ssize_t additional);
int wreath_pg_buffer_append(WreathPgBuffer *buffer, const void *data, Py_ssize_t length);
int wreath_pg_buffer_u16(WreathPgBuffer *buffer, uint16_t value);
int wreath_pg_buffer_u32(WreathPgBuffer *buffer, uint32_t value);
int wreath_pg_buffer_i32(WreathPgBuffer *buffer, int32_t value);
Py_ssize_t wreath_pg_buffer_begin_message(WreathPgBuffer *buffer, char type);
int wreath_pg_buffer_end_message(WreathPgBuffer *buffer, Py_ssize_t length_position);
PyObject *wreath_pg_buffer_finish(WreathPgBuffer *buffer);
int wreath_pg_buffer_init(PyObject *module);

#endif
