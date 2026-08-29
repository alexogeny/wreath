#ifndef WREATH_HEADER_BLOCK_H
#define WREATH_HEADER_BLOCK_H

#include <Python.h>
#include <stdint.h>

/* A validated request-head sink. HTTP/1 stores offsets into one owned byte
 * block; compressed protocols may store owned name/value objects. Neither
 * representation allocates the ASGI list or its pair tuples until Python asks
 * for it. Once materialized, every reader uses the cached mutable list so ASGI
 * mutation semantics remain exact. */
PyObject *wreath_header_block_new_raw(const uint8_t *data, Py_ssize_t size);
PyObject *wreath_header_block_new_objects(Py_ssize_t capacity);
void wreath_header_block_freelist_enable(void);
void wreath_header_block_freelist_fini(void);
Py_ssize_t wreath_header_block_storage_allocations(void);
char *wreath_header_block_raw_data(PyObject *block);
int wreath_header_block_append_span(
    PyObject *block, Py_ssize_t name_offset, Py_ssize_t name_size,
    Py_ssize_t value_offset, Py_ssize_t value_size);
int wreath_header_block_append_objects(
    PyObject *block, PyObject *name, PyObject *value);

int wreath_headers_is_block(PyObject *headers);
Py_ssize_t wreath_headers_count(PyObject *headers);
int wreath_headers_view(
    PyObject *headers, Py_ssize_t index,
    const char **name, Py_ssize_t *name_size,
    const char **value, Py_ssize_t *value_size);
PyObject *wreath_headers_name_object(PyObject *headers, Py_ssize_t index);
PyObject *wreath_headers_value_object(PyObject *headers, Py_ssize_t index);
PyObject *wreath_headers_value_borrowed(PyObject *headers, Py_ssize_t index);
PyObject *wreath_headers_materialize(PyObject *headers);
int wreath_headers_find(
    PyObject *headers, const char *name, Py_ssize_t name_size,
    Py_ssize_t *first, Py_ssize_t *matches);
int wreath_headers_find_name(
    PyObject *headers, PyObject *name, Py_ssize_t *first,
    Py_ssize_t *matches);
Py_ssize_t wreath_headers_unique_count(PyObject *headers);
int wreath_headers_set_first(PyObject *headers, PyObject *name, PyObject *value);
int wreath_headers_remove_all(PyObject *headers, PyObject *name);

#endif
