#include "buffer.h"

#include <limits.h>
#include <string.h>

void
wreath_pg_buffer_clear(WreathPgBuffer *buffer)
{
    PyMem_Free(buffer->data);
    buffer->data = NULL;
    buffer->length = 0;
    buffer->capacity = 0;
}

int
wreath_pg_buffer_reserve(WreathPgBuffer *buffer, Py_ssize_t additional)
{
    Py_ssize_t required;
    Py_ssize_t capacity;
    char *resized;

    if (additional < 0 || buffer->length > PY_SSIZE_T_MAX - additional) {
        PyErr_NoMemory();
        return -1;
    }
    required = buffer->length + additional;
    if (required <= buffer->capacity) {
        return 0;
    }
    capacity = buffer->capacity > 0 ? buffer->capacity : 256;
    while (capacity < required) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            capacity = required;
            break;
        }
        capacity *= 2;
    }
    resized = PyMem_Realloc(buffer->data, (size_t)capacity);
    if (resized == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    buffer->data = resized;
    buffer->capacity = capacity;
    return 0;
}

int
wreath_pg_buffer_append(WreathPgBuffer *buffer, const void *data, Py_ssize_t length)
{
    if (wreath_pg_buffer_reserve(buffer, length) < 0) {
        return -1;
    }
    if (length > 0) {
        memcpy(buffer->data + buffer->length, data, (size_t)length);
        buffer->length += length;
    }
    return 0;
}

int
wreath_pg_buffer_u16(WreathPgBuffer *buffer, uint16_t value)
{
    unsigned char data[2] = {
        (unsigned char)(value >> 8),
        (unsigned char)value
    };
    return wreath_pg_buffer_append(buffer, data, 2);
}

int
wreath_pg_buffer_u32(WreathPgBuffer *buffer, uint32_t value)
{
    unsigned char data[4] = {
        (unsigned char)(value >> 24),
        (unsigned char)(value >> 16),
        (unsigned char)(value >> 8),
        (unsigned char)value
    };
    return wreath_pg_buffer_append(buffer, data, 4);
}

int
wreath_pg_buffer_i32(WreathPgBuffer *buffer, int32_t value)
{
    return wreath_pg_buffer_u32(buffer, (uint32_t)value);
}

Py_ssize_t
wreath_pg_buffer_begin_message(WreathPgBuffer *buffer, char type)
{
    static const char placeholder[4] = {0, 0, 0, 0};
    if (wreath_pg_buffer_append(buffer, &type, 1) < 0 ||
        wreath_pg_buffer_append(buffer, placeholder, 4) < 0) return -1;
    return buffer->length - 4;
}

int
wreath_pg_buffer_end_message(WreathPgBuffer *buffer, Py_ssize_t length_position)
{
    Py_ssize_t length = buffer->length - length_position;
    unsigned char *out;
    if (length < 4 || length > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "PostgreSQL message too large");
        return -1;
    }
    out = (unsigned char *)buffer->data + length_position;
    out[0] = (unsigned char)((uint32_t)length >> 24);
    out[1] = (unsigned char)((uint32_t)length >> 16);
    out[2] = (unsigned char)((uint32_t)length >> 8);
    out[3] = (unsigned char)length;
    return 0;
}

PyObject *
wreath_pg_buffer_finish(WreathPgBuffer *buffer)
{
    PyObject *result = PyBytes_FromStringAndSize(buffer->data, buffer->length);
    wreath_pg_buffer_clear(buffer);
    return result;
}

int
wreath_pg_buffer_init(PyObject *module)
{
    (void)module;
    return 0;
}
