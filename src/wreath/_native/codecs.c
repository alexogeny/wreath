/* Byte-level web codecs: percent decoding, query strings, cookies. */
#include "wreathcore.h"

#include "simd.h"

#include <limits.h>
#include <math.h>

static Py_ssize_t
zip_find_reverse(const uint8_t *data, Py_ssize_t start, Py_ssize_t end,
                 const char signature[4])
{
    if (end - start < 4) return -1;
    for (Py_ssize_t index = end - 4;; index--) {
        if (memcmp(data + index, signature, 4) == 0) return index;
        if (index == start) break;
    }
    return -1;
}

PyObject *
wreath_zip_entry_count(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *raw;
    Py_ssize_t limit;
    if (!PyArg_ParseTuple(
            args, "O!n:zip_entry_count", &PyBytes_Type, &raw, &limit)) return NULL;
    const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(raw);
    Py_ssize_t length = PyBytes_GET_SIZE(raw);
    Py_ssize_t search_start = length > 65557 ? length - 65557 : 0;
    Py_ssize_t eocd = zip_find_reverse(
        data, search_start, length, "PK\x05\x06");
    if (eocd < 0 || length - eocd < 22) Py_RETURN_NONE;
    uint16_t comment_bytes = wreath_load_u16_le(data + eocd + 20);
    if ((uint64_t)(length - eocd - 22) != (uint64_t)comment_bytes) Py_RETURN_NONE;

    uint64_t directory_size = wreath_load_u32_le(data + eocd + 12);
    Py_ssize_t directory_end = eocd;
    Py_ssize_t locator = eocd - 20;
    if (locator >= 0 && memcmp(data + locator, "PK\x06\x07", 4) == 0) {
        Py_ssize_t zip64 = zip_find_reverse(data, 0, locator, "PK\x06\x06");
        if (zip64 < 0 || locator - zip64 < 56) Py_RETURN_NONE;
        uint64_t record_size = wreath_load_u64_le(data + zip64 + 4);
        if (record_size != (uint64_t)(locator - zip64 - 12)) Py_RETURN_NONE;
        directory_size = wreath_load_u64_le(data + zip64 + 40);
        directory_end = zip64;
    }
    else if (directory_size == UINT32_MAX) Py_RETURN_NONE;

    if (directory_size > (uint64_t)directory_end) Py_RETURN_NONE;
    Py_ssize_t cursor = directory_end - (Py_ssize_t)directory_size;
    Py_ssize_t count = 0;
    while (cursor < directory_end) {
        if (directory_end - cursor < 46 ||
            memcmp(data + cursor, "PK\x01\x02", 4) != 0) Py_RETURN_NONE;
        uint64_t record_size = 46U +
            (uint64_t)wreath_load_u16_le(data + cursor + 28) +
            (uint64_t)wreath_load_u16_le(data + cursor + 30) +
            (uint64_t)wreath_load_u16_le(data + cursor + 32);
        if (record_size > (uint64_t)(directory_end - cursor)) Py_RETURN_NONE;
        cursor += (Py_ssize_t)record_size;
        count++;
        if (count > limit) return PyLong_FromSsize_t(count);
        if ((count & 4095) == 0 && PyErr_CheckSignals() < 0) return NULL;
    }
    return PyLong_FromSsize_t(count);
}

static int
expected_type(const char *expected, PyObject *value)
{
    PyErr_Format(PyExc_TypeError, "expected %s, got %s", expected,
                 Py_TYPE(value)->tp_name);
    return -1;
}

PyObject *
wreath_validate_bit_string(
    PyObject *Py_UNUSED(self), PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "validate_bit_string() takes exactly 2 arguments (%zd given)",
                     nargs);
        return NULL;
    }
    PyObject *value = args[0];
    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "bit string must be str");
        return NULL;
    }
    Py_ssize_t expected = PyLong_AsSsize_t(args[1]);
    if (expected == -1 && PyErr_Occurred() != NULL) return NULL;
    Py_ssize_t length;
    const char *bits = PyUnicode_AsUTF8AndSize(value, &length);
    if (bits == NULL) return NULL;
    Py_ssize_t characters = PyUnicode_GET_LENGTH(value);
    if (characters != expected) {
        PyErr_Format(PyExc_ValueError,
                     "bit(%zd) requires exactly %zd bits, got %zd",
                     expected, expected, characters);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < length; index++) {
        if (bits[index] != '0' && bits[index] != '1') {
            PyErr_SetString(PyExc_ValueError,
                            "a bit string may hold only '0' and '1'");
            return NULL;
        }
    }
    return Py_NewRef(value);
}

PyObject *
wreath_float_sequence(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value;
    Py_ssize_t dimension;
    int half;
    if (!PyArg_ParseTuple(args, "Onp:float_sequence", &value, &dimension, &half))
        return NULL;
    if (!PyList_Check(value) && !PyTuple_Check(value)) {
        expected_type("list or tuple of floats", value);
        return NULL;
    }
    Py_ssize_t length = PyList_Check(value)
        ? PyList_GET_SIZE(value) : PyTuple_GET_SIZE(value);
    if (length != dimension) {
        PyErr_Format(PyExc_ValueError,
                     half ? "halfvec(%zd) requires exactly %zd values, got %zd"
                          : "vector(%zd) requires exactly %zd values, got %zd",
                     dimension, dimension, length);
        return NULL;
    }
    PyObject *out = PyList_New(length);
    if (out == NULL) return NULL;
    PyObject **items = PySequence_Fast_ITEMS(value);
    for (Py_ssize_t i = 0; i < length; i++) {
        PyObject *item = items[i];
        int exact_float = PyFloat_CheckExact(item);
        double number;
        if (PyFloat_Check(item)) {
            number = PyFloat_AS_DOUBLE(item);
        } else {
            if (PyBool_Check(item) || !PyLong_Check(item)) {
                expected_type("float", item);
                Py_DECREF(out);
                return NULL;
            }
            number = PyLong_AsDouble(item);
            if (number == -1.0 && PyErr_Occurred()) {
                Py_DECREF(out);
                return NULL;
            }
        }
        if (!isfinite(number)) {
            PyErr_SetString(PyExc_ValueError, half
                ? "a halfvec element must be finite; pgvector stores neither NaN nor infinity and every distance involving one is undefined"
                : "a vector element must be finite; pgvector stores neither NaN nor infinity and every distance involving one is undefined");
            Py_DECREF(out);
            return NULL;
        }
        if (half && (number < -65504.0 || number > 65504.0)) {
            PyErr_Format(PyExc_ValueError,
                         "halfvec element %R is outside binary16's range of +/-65504.0; it would round to an infinity, which pgvector refuses. Use Vector() for values this large.",
                         item);
            Py_DECREF(out);
            return NULL;
        }
        PyObject *converted = exact_float ? Py_NewRef(item)
                                          : PyFloat_FromDouble(number);
        if (converted == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        PyList_SET_ITEM(out, i, converted);
    }
    return out;
}

static PyObject *
coerce_array_item(PyObject *item, long oid, PyObject *coerce)
{
    const char *expected = NULL;
    if (oid == 16) {
        if (!PyBool_Check(item)) expected = "bool";
    } else if (oid == 20 || oid == 21 || oid == 23) {
        if (!PyLong_CheckExact(item)) expected = oid == 20 ? "int8" : oid == 21 ? "int2" : "int4";
        if (expected == NULL) {
            int overflow = 0;
            long long value = PyLong_AsLongLongAndOverflow(item, &overflow);
            long long low = oid == 21 ? -32768LL : oid == 23 ? -2147483648LL : LLONG_MIN;
            long long high = oid == 21 ? 32767LL : oid == 23 ? 2147483647LL : LLONG_MAX;
            if ((value == -1 && PyErr_Occurred()) || overflow ||
                value < low || value > high) {
                PyErr_Format(PyExc_OverflowError, "value out of range for %s: %R",
                             oid == 20 ? "int8" : oid == 21 ? "int2" : "int4", item);
                return NULL;
            }
        }
    } else if (oid == 700 || oid == 701) {
        if (PyFloat_CheckExact(item)) {
            return PyFloat_FromDouble(PyFloat_AS_DOUBLE(item));
        }
        if (PyBool_Check(item) || (!PyLong_Check(item) && !PyFloat_Check(item))) {
            expected = "float";
        } else {
            double value = PyFloat_Check(item)
                ? PyFloat_AS_DOUBLE(item) : PyFloat_AsDouble(item);
            if (value == -1.0 && PyErr_Occurred()) return NULL;
            return PyFloat_FromDouble(value);
        }
    } else if (oid == 25 || oid == 1043) {
        if (!PyUnicode_Check(item)) expected = "str";
    } else if (oid == 17) {
        if (!PyBytes_Check(item)) expected = "bytes";
    } else {
        return PyObject_CallOneArg(coerce, item);
    }
    if (expected != NULL) {
        expected_type(expected, item);
        return NULL;
    }
    return Py_NewRef(item);
}

PyObject *
wreath_array_coerce(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value, *name, *coerce;
    int nullable;
    long oid;
    if (!PyArg_ParseTuple(args, "OpOlO:array_coerce", &value, &nullable, &name,
                          &oid, &coerce)) return NULL;
    if (!PyList_Check(value) && !PyTuple_Check(value)) {
        expected_type("list or tuple", value);
        return NULL;
    }
    Py_ssize_t length = PySequence_Size(value);
    if (length < 0) return NULL;
    PyObject *out = PyList_New(length);
    if (out == NULL) return NULL;
    PyObject **items = PySequence_Fast_ITEMS(value);
    for (Py_ssize_t i = 0; i < length; i++) {
        PyObject *item = items[i];
        PyObject *converted;
        if (item == Py_None) {
            if (!nullable) {
                PyErr_Format(PyExc_TypeError,
                             "%U[] elements are not nullable; pass nullable_elements=True to allow NULL entries",
                             name);
                Py_DECREF(out);
                return NULL;
            }
            converted = Py_NewRef(Py_None);
        } else {
            converted = coerce_array_item(item, oid, coerce);
            if (converted == NULL) {
                Py_DECREF(out);
                return NULL;
            }
        }
        PyList_SET_ITEM(out, i, converted);
    }
    return out;
}

PyObject *
wreath_map_nullable(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value, *function;
    if (!PyArg_ParseTuple(args, "OO:map_nullable", &value, &function)) return NULL;
    PyObject *items = PySequence_Fast(value, "expected a sequence");
    if (items == NULL) return NULL;
    Py_ssize_t length = PySequence_Fast_GET_SIZE(items);
    PyObject *out = PyList_New(length);
    if (out == NULL) {
        Py_DECREF(items);
        return NULL;
    }
    PyObject **source = PySequence_Fast_ITEMS(items);
    for (Py_ssize_t i = 0; i < length; i++) {
        PyObject *mapped = source[i] == Py_None ? Py_NewRef(Py_None)
                                               : PyObject_CallOneArg(function, source[i]);
        if (mapped == NULL) {
            Py_DECREF(items);
            Py_DECREF(out);
            return NULL;
        }
        PyList_SET_ITEM(out, i, mapped);
    }
    Py_DECREF(items);
    return out;
}

/* The dimension bound and the element mapping, shared by the two sparsevec
 * entry points. On success the caller owns `*mapping_out` and `*keys_out`
 * (keys sorted, so both build the same wire order); on failure both are NULL
 * and an exception is set.
 *
 * One definition because the bound and its message are a pgvector contract:
 * two copies of a range check is two places for the limit to be wrong in. */
static int
sparsevector_dimension(PyObject *dim_obj, long long *dim_out)
{
    if (!PyLong_CheckExact(dim_obj)) {
        PyErr_Format(PyExc_TypeError, "SparseVector dimension must be int, not %s",
                     Py_TYPE(dim_obj)->tp_name);
        return -1;
    }
    long long dim = PyLong_AsLongLong(dim_obj);
    if (dim == -1 && PyErr_Occurred()) return -1;
    if (dim < 1 || dim > 1000000000LL) {
        PyErr_Format(PyExc_ValueError,
                     "SparseVector(%R) is out of range; pgvector allows 1 to 1000000000 dimensions",
                     dim_obj);
        return -1;
    }
    *dim_out = dim;
    return 0;
}

static int
sparsevector_open_mapping(PyObject *dim_obj, PyObject *elements,
                          long long *dim_out, PyObject **mapping_out)
{
    *mapping_out = NULL;
    if (sparsevector_dimension(dim_obj, dim_out) < 0) return -1;
    PyObject *mapping = PyDict_Check(elements)
        ? Py_NewRef(elements)
        : PyObject_CallOneArg((PyObject *)&PyDict_Type, elements);
    if (mapping == NULL) return -1;
    *mapping_out = mapping;
    return 0;
}

static int
sparsevector_open(PyObject *dim_obj, PyObject *elements, long long *dim_out,
                  PyObject **mapping_out, PyObject **keys_out)
{
    *keys_out = NULL;
    if (sparsevector_open_mapping(
            dim_obj, elements, dim_out, mapping_out) < 0) return -1;
    PyObject *mapping = *mapping_out;
    PyObject *keys = PyDict_Keys(mapping);
    if (keys == NULL || PyList_Sort(keys) < 0) {
        Py_XDECREF(keys);
        Py_DECREF(mapping);
        *mapping_out = NULL;
        return -1;
    }
    *keys_out = keys;
    return 0;
}

/* One element: a 1-based index inside the dimension, and a finite int or float
 * value. 0 on success with `*position_out`/`*number_out` filled, -1 with an
 * exception set. What each caller then does with the value differs; which
 * values are legal at all does not, so it is decided once here. */
static int
sparsevector_element_value(PyObject *index, PyObject *item, long long dim,
                           long long *position_out, double *number_out)
{
    if (!PyLong_CheckExact(index)) {
        PyErr_Format(PyExc_TypeError, "sparsevec index %R must be int, not %s",
                     index, Py_TYPE(index)->tp_name);
        return -1;
    }
    long long position = PyLong_AsLongLong(index);
    if ((position == -1 && PyErr_Occurred()) || position < 1 || position > dim) {
        if (!PyErr_Occurred())
            PyErr_Format(PyExc_ValueError,
                         "sparsevec index %R is outside 1..%lld; indices are 1-based, the way pgvector writes them",
                         index, dim);
        return -1;
    }
    if (PyBool_Check(item) || (!PyLong_Check(item) && !PyFloat_Check(item))) {
        PyErr_Format(PyExc_TypeError,
                     "sparsevec value at index %R must be int or float, not %s",
                     index, Py_TYPE(item)->tp_name);
        return -1;
    }
    double number;
    if (PyFloat_Check(item)) number = PyFloat_AS_DOUBLE(item);
    else number = PyLong_AsDouble(item);
    if (number == -1.0 && PyErr_Occurred()) return -1;
    if (!isfinite(number)) {
        PyErr_Format(PyExc_ValueError,
                     "sparsevec value at index %R is %R; pgvector stores neither NaN nor infinity",
                     index, item);
        return -1;
    }
    *position_out = position;
    *number_out = number;
    return 0;
}

static int
sparsevector_element(PyObject *mapping, PyObject *index, long long dim,
                     long long *position_out, double *number_out)
{
    PyObject *item = PyDict_GetItemWithError(mapping, index);
    if (item == NULL) return -1;
    return sparsevector_element_value(
        index, item, dim, position_out, number_out);
}

typedef struct {
    int32_t index;
    double value;
    Py_ssize_t order;
} SparseVectorPair;

static int
sparsevector_pair_compare(const void *left_pointer, const void *right_pointer)
{
    const SparseVectorPair *left = left_pointer;
    const SparseVectorPair *right = right_pointer;
    int index_order = (left->index > right->index) - (left->index < right->index);
    if (index_order != 0) return index_order;
    return (left->order > right->order) - (left->order < right->order);
}

static PyObject *
sparsevector_data_from_pairs(long long dim, SparseVectorPair *pairs,
                             Py_ssize_t source_count, Py_ssize_t max_nnz)
{
    if (source_count <= 64) {
        for (Py_ssize_t source = 1; source < source_count; source++) {
            SparseVectorPair pair = pairs[source];
            Py_ssize_t position = source;
            while (position > 0 &&
                   sparsevector_pair_compare(&pairs[position - 1], &pair) > 0) {
                pairs[position] = pairs[position - 1];
                position--;
            }
            pairs[position] = pair;
        }
    }
    else
        qsort(pairs, (size_t)source_count, sizeof(*pairs), sparsevector_pair_compare);
    Py_ssize_t count = 0;
    for (Py_ssize_t source = 0; source < source_count;) {
        Py_ssize_t next = source + 1;
        while (next < source_count && pairs[next].index == pairs[source].index)
            next++;
        SparseVectorPair pair = pairs[next - 1];
        if (pair.value != 0.0) {
            if (count == max_nnz) {
                PyErr_Format(PyExc_ValueError,
                             "a sparsevec holds at most %zd non-zero elements; this one has more. A value that dense wants `Vector` or `Halfvec`, which store every position and index far better",
                             max_nnz);
                return NULL;
            }
            pairs[count++] = pair;
        }
        source = next;
    }
    WreathSparseVector *data = PyMem_Calloc(1, sizeof(*data));
    if (data == NULL) return PyErr_NoMemory();
    data->dimension = (int32_t)dim;
    data->count = count;
    if (count != 0) {
        size_t index_bytes = (size_t)count * sizeof(*data->indices);
        size_t value_offset = (index_bytes + sizeof(double) - 1) &
                              ~(sizeof(double) - 1);
        if ((size_t)count > (SIZE_MAX - value_offset) / sizeof(*data->values)) {
            PyMem_Free(data);
            return PyErr_NoMemory();
        }
        data->indices = PyMem_Malloc(
            value_offset + (size_t)count * sizeof(*data->values));
        if (data->indices == NULL) {
            PyMem_Free(data);
            return PyErr_NoMemory();
        }
        data->values = (double *)((char *)data->indices + value_offset);
        for (Py_ssize_t sparse = 0; sparse < count; sparse++) {
            data->indices[sparse] = pairs[sparse].index;
            data->values[sparse] = pairs[sparse].value;
        }
    }
    PyObject *capsule = PyCapsule_New(
        data, WREATH_SPARSE_VECTOR_CAPSULE, wreath_sparse_vector_destroy);
    if (capsule == NULL) {
        PyMem_Free(data->indices);
        PyMem_Free(data);
    }
    return capsule;
}

static inline int
sparsevector_pair_from_entry(PyObject *entry, long long dim,
                             Py_ssize_t order, SparseVectorPair *output)
{
    PyObject *pair = PySequence_Fast(
        entry, "sparsevec elements must be index-value pairs");
    if (pair == NULL) return -1;
    if (PySequence_Fast_GET_SIZE(pair) != 2) {
        Py_DECREF(pair);
        PyErr_SetString(PyExc_ValueError,
                        "sparsevec elements must be index-value pairs");
        return -1;
    }
    long long sparse_index;
    double number;
    int invalid = sparsevector_element_value(
        PySequence_Fast_GET_ITEM(pair, 0), PySequence_Fast_GET_ITEM(pair, 1),
        dim, &sparse_index, &number);
    Py_DECREF(pair);
    if (invalid < 0) return -1;
    *output = (SparseVectorPair){(int32_t)sparse_index, number, order};
    return 0;
}

static PyObject *
sparsevector_data_from_sequence(PyObject *dim_obj, PyObject *elements,
                                Py_ssize_t max_nnz)
{
    Py_ssize_t count = PySequence_Size(elements);
    if (count < 0) return NULL;
    if (count > 64) return NULL;
    long long dim;
    if (sparsevector_dimension(dim_obj, &dim) < 0) return NULL;
    PyObject *sequence = PySequence_Fast(
        elements, "sparsevec elements must be index-value pairs");
    if (sequence == NULL) return NULL;
    if ((size_t)count > SIZE_MAX / sizeof(SparseVectorPair)) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    SparseVectorPair *pairs = count != 0
        ? PyMem_Malloc((size_t)count * sizeof(*pairs)) : NULL;
    if (count != 0 && pairs == NULL) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    PyObject **items = PySequence_Fast_ITEMS(sequence);
    for (Py_ssize_t index = 0; index < count; index++) {
        if (sparsevector_pair_from_entry(
                items[index], dim, index, &pairs[index]) < 0) {
            PyErr_Clear();
            PyMem_Free(pairs);
            Py_DECREF(sequence);
            return NULL;
        }
    }
    PyObject *result = sparsevector_data_from_pairs(
        dim, pairs, count, max_nnz);
    PyMem_Free(pairs);
    Py_DECREF(sequence);
    return result;
}

PyObject *
wreath_sparsevector_parts(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *dim_obj, *elements;
    Py_ssize_t max_nnz;
    long long dim;
    PyObject *mapping, *keys;
    if (!PyArg_ParseTuple(args, "OOn:sparsevector_parts", &dim_obj, &elements,
                          &max_nnz)) return NULL;
    if (sparsevector_open(dim_obj, elements, &dim, &mapping, &keys) < 0) return NULL;
    Py_ssize_t count = PyList_GET_SIZE(keys);
    PyObject *indices = PyTuple_New(count);
    PyObject *values = PyTuple_New(count);
    if (indices == NULL || values == NULL) goto sparse_error;
    Py_ssize_t used = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *index = PyList_GET_ITEM(keys, i);
        long long position;
        double number;
        if (sparsevector_element(mapping, index, dim, &position, &number) < 0)
            goto sparse_error;
        if (number == 0.0) continue;
        if (used == max_nnz) {
            PyErr_Format(PyExc_ValueError,
                         "a sparsevec holds at most %zd non-zero elements; this one has more. A value that dense wants `Vector` or `Halfvec`, which store every position and index far better",
                         max_nnz);
            goto sparse_error;
        }
        PyTuple_SET_ITEM(indices, used, Py_NewRef(index));
        PyObject *converted = PyFloat_FromDouble(number);
        if (converted == NULL) goto sparse_error;
        PyTuple_SET_ITEM(values, used, converted);
        used++;
    }
    if (used != count) {
        if (_PyTuple_Resize(&indices, used) < 0 || _PyTuple_Resize(&values, used) < 0)
            goto sparse_error;
    }
    Py_DECREF(keys);
    Py_DECREF(mapping);
    return wreath_tuple2_from_owned(indices, values);

sparse_error:
    Py_XDECREF(indices);
    Py_XDECREF(values);
    Py_DECREF(keys);
    Py_DECREF(mapping);
    return NULL;
}

PyObject *
wreath_sparsevector_data(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *dim_obj, *elements;
    Py_ssize_t max_nnz;
    long long dim;
    PyObject *mapping;
    if (!PyArg_ParseTuple(args, "OOn:sparsevector_data", &dim_obj, &elements,
                          &max_nnz)) return NULL;
    if (PyList_CheckExact(elements) || PyTuple_CheckExact(elements)) {
        PyObject *fast = sparsevector_data_from_sequence(
            dim_obj, elements, max_nnz);
        if (fast != NULL || PyErr_Occurred()) return fast;
    }
    if (sparsevector_open_mapping(
            dim_obj, elements, &dim, &mapping) < 0) return NULL;
    Py_ssize_t source_count = PyDict_GET_SIZE(mapping);
    if ((size_t)source_count > SIZE_MAX / sizeof(SparseVectorPair)) {
        Py_DECREF(mapping);
        return PyErr_NoMemory();
    }
    SparseVectorPair *pairs = source_count != 0
        ? PyMem_Malloc((size_t)source_count * sizeof(*pairs)) : NULL;
    if (source_count != 0 && pairs == NULL) {
        Py_DECREF(mapping);
        return PyErr_NoMemory();
    }
    Py_ssize_t count = 0;
    Py_ssize_t position = 0;
    PyObject *index = NULL;
    PyObject *item = NULL;
    while (PyDict_Next(mapping, &position, &index, &item)) {
        long long sparse_index;
        double number;
        if (sparsevector_element_value(
                index, item, dim, &sparse_index, &number) < 0)
            goto sparse_data_error;
        if (number == 0.0) continue;
        if (count == max_nnz) {
            PyErr_Format(PyExc_ValueError,
                         "a sparsevec holds at most %zd non-zero elements; this one has more. A value that dense wants `Vector` or `Halfvec`, which store every position and index far better",
                         max_nnz);
            goto sparse_data_error;
        }
        pairs[count] = (SparseVectorPair){(int32_t)sparse_index, number, count};
        count++;
    }
    PyObject *capsule = sparsevector_data_from_pairs(dim, pairs, count, max_nnz);
    PyMem_Free(pairs);
    Py_DECREF(mapping);
    return capsule;

sparse_data_error:
    PyMem_Free(pairs);
    Py_DECREF(mapping);
    return NULL;
}

PyObject *
wreath_sparsevector_dim(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    return PyLong_FromLong(data->dimension);
}

PyObject *
wreath_sparsevector_len(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    return PyLong_FromSsize_t(data->count);
}

PyObject *
wreath_sparsevector_indices(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    PyObject *result = PyTuple_New(data->count);
    if (result == NULL) return NULL;
    for (Py_ssize_t i = 0; i < data->count; i++) {
        PyObject *item = PyLong_FromLong(data->indices[i]);
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, i, item);
    }
    return result;
}

PyObject *
wreath_sparsevector_values(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    PyObject *result = PyTuple_New(data->count);
    if (result == NULL) return NULL;
    for (Py_ssize_t i = 0; i < data->count; i++) {
        PyObject *item = PyFloat_FromDouble(data->values[i]);
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, i, item);
    }
    return result;
}

PyObject *
wreath_sparsevector_dict(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    PyObject *result = PyDict_New();
    if (result == NULL) return NULL;
    for (Py_ssize_t i = 0; i < data->count; i++) {
        PyObject *key = PyLong_FromLong(data->indices[i]);
        PyObject *value = PyFloat_FromDouble(data->values[i]);
        if (key == NULL || value == NULL || PyDict_SetItem(result, key, value) < 0) {
            Py_XDECREF(key);
            Py_XDECREF(value);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(key);
        Py_DECREF(value);
    }
    return result;
}

PyObject *
wreath_sparsevector_equal(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *left_capsule, *right_capsule;
    if (!PyArg_ParseTuple(args, "OO:sparsevector_equal",
                          &left_capsule, &right_capsule)) return NULL;
    WreathSparseVector *left = wreath_sparse_vector_get(left_capsule);
    if (left == NULL) return NULL;
    WreathSparseVector *right = wreath_sparse_vector_get(right_capsule);
    if (right == NULL) return NULL;
    if (left->dimension != right->dimension || left->count != right->count)
        Py_RETURN_FALSE;
    for (Py_ssize_t i = 0; i < left->count; i++) {
        if (left->indices[i] != right->indices[i] ||
            left->values[i] != right->values[i]) Py_RETURN_FALSE;
    }
    Py_RETURN_TRUE;
}

PyObject *
wreath_sparsevector_hash(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) return NULL;
    uint64_t hash = UINT64_C(1469598103934665603);
    hash = (hash ^ (uint32_t)data->dimension) * UINT64_C(1099511628211);
    for (Py_ssize_t index = 0; index < data->count; index++) {
        uint64_t bits;
        memcpy(&bits, &data->values[index], sizeof(bits));
        hash = (hash ^ (uint32_t)data->indices[index]) * UINT64_C(1099511628211);
        hash = (hash ^ bits) * UINT64_C(1099511628211);
    }
    Py_hash_t result = (Py_hash_t)(hash ^ (hash >> 32));
    if (result == -1) result = -2;
    return PyLong_FromSsize_t(result);
}

static inline int
hexval(uint8_t c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/* Decode src into dst (dst must hold at least src_len bytes); returns the
 * decoded length. Invalid %XX sequences are copied through literally, which
 * matches urllib.parse.unquote_to_bytes. */
static Py_ssize_t
decode_into(uint8_t *dst, const uint8_t *src, Py_ssize_t src_len, int plus_as_space)
{
    Py_ssize_t out = 0;
    for (Py_ssize_t i = 0; i < src_len; i++) {
        uint8_t c = src[i];
        if (c == '%' && i + 2 < src_len) {
            int hi = hexval(src[i + 1]);
            int lo = hexval(src[i + 2]);
            if (hi >= 0 && lo >= 0) {
                dst[out++] = (uint8_t)((hi << 4) | lo);
                i += 2;
                continue;
            }
        }
        if (plus_as_space && c == '+') {
            c = ' ';
        }
        dst[out++] = c;
    }
    return out;
}

PyObject *
wreath_percent_decode(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"data", "plus_as_space", NULL};
    Py_buffer data;
    int plus_as_space = 0;
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "y*|p:percent_decode", keywords, &data, &plus_as_space)) {
        return NULL;
    }

    PyObject *result = PyBytes_FromStringAndSize(NULL, data.len);
    if (result == NULL) {
        PyBuffer_Release(&data);
        return NULL;
    }
    Py_ssize_t out = decode_into(
        (uint8_t *)PyBytes_AS_STRING(result), data.buf, data.len, plus_as_space);
    PyBuffer_Release(&data);
    if (_PyBytes_Resize(&result, out) < 0) {
        return NULL;
    }
    return result;
}

/* Decode one percent-encoded component to str (UTF-8, replacement chars). */
static PyObject *
component_to_str(const uint8_t *src, Py_ssize_t len)
{
    if (len == 0) {
        return PyUnicode_New(0, 127);
    }
    /* Nothing to decode is the common case for a query value. Two vectorised
     * `memchr`s establish that and hand the source straight to the decoder,
     * skipping the scratch allocation and the byte loop entirely. */
    if (memchr(src, '%', (size_t)len) == NULL && memchr(src, '+', (size_t)len) == NULL) {
        return PyUnicode_DecodeUTF8((const char *)src, len, "replace");
    }
    uint8_t *scratch = PyMem_Malloc((size_t)len);
    if (scratch == NULL) {
        return PyErr_NoMemory();
    }
    Py_ssize_t out = decode_into(scratch, src, len, 1);
    PyObject *text = PyUnicode_DecodeUTF8((const char *)scratch, out, "replace");
    PyMem_Free(scratch);
    return text;
}

PyObject *
wreath_parse_qs(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer query;
    Py_ssize_t max_fields = 0;
    if (!PyArg_ParseTuple(args, "y*|n:parse_qs", &query, &max_fields)) {
        return NULL;
    }

    const uint8_t *data = query.buf;
    Py_ssize_t len = query.len;
    Py_ssize_t start = 0;
    PyObject *pairs = PyList_New(0);
    if (pairs == NULL) {
        PyBuffer_Release(&query);
        return NULL;
    }
    for (Py_ssize_t i = 0; i <= len; i++) {
        if (i < len && data[i] != '&') {
            continue;
        }
        Py_ssize_t field_len = i - start;
        if (field_len > 0) {
            if (max_fields > 0 && PyList_GET_SIZE(pairs) >= max_fields) {
                PyErr_Format(PyExc_ValueError,
                             "urlencoded data exceeds %zd fields", max_fields);
                Py_DECREF(pairs);
                PyBuffer_Release(&query);
                return NULL;
            }
            const uint8_t *field = data + start;
            Py_ssize_t eq = 0;
            while (eq < field_len && field[eq] != '=') {
                eq++;
            }
            PyObject *key = component_to_str(field, eq);
            PyObject *value = (eq < field_len)
                                  ? component_to_str(field + eq + 1, field_len - eq - 1)
                                  : PyUnicode_New(0, 127);
            PyObject *pair = wreath_tuple2_from_owned(key, value);
            if (pair == NULL || PyList_Append(pairs, pair) < 0) {
                Py_XDECREF(pair);
                Py_DECREF(pairs);
                PyBuffer_Release(&query);
                return NULL;
            }
            Py_DECREF(pair);
        }
        start = i + 1;
    }
    PyBuffer_Release(&query);
    return pairs;
}

enum { QUERY_STR = 0, QUERY_INT = 1, QUERY_FLOAT = 2, QUERY_BOOL = 3 };

static int
query_append_error(PyObject **errors, PyObject *alias, PyObject *message,
                   const char *kind)
{
    if (*errors == Py_None) *errors = NULL;
    if (*errors == NULL) {
        *errors = PyList_New(0);
        if (*errors == NULL) return -1;
    }
    PyObject *source = PyUnicode_FromString("query");
    PyObject *loc = source == NULL ? NULL : PyList_New(2);
    PyObject *type = PyUnicode_FromString(kind);
    PyObject *error = NULL;
    int result = -1;
    if (loc != NULL) {
        PyList_SET_ITEM(loc, 0, source); /* steals */
        PyList_SET_ITEM(loc, 1, Py_NewRef(alias));
    }
    else {
        Py_XDECREF(source);
    }
    if (loc == NULL || type == NULL || message == NULL) goto done;
    error = _PyDict_NewPresized(3);
    if (error == NULL ||
        PyDict_SetItemString(error, "loc", loc) < 0 ||
        PyDict_SetItemString(error, "msg", message) < 0 ||
        PyDict_SetItemString(error, "type", type) < 0 ||
        PyList_Append(*errors, error) < 0) goto done;
    result = 0;
done:
    Py_XDECREF(error);
    Py_XDECREF(type);
    Py_XDECREF(loc);
    Py_XDECREF(message);
    return result;
}

static int
query_append_raw_error(PyObject **errors, PyObject *alias, PyObject *raw,
                       const char *format, const char *kind)
{
    return query_append_error(
        errors, alias, PyUnicode_FromFormat(format, raw), kind);
}

static PyObject *
query_convert_bool(PyObject *raw)
{
    PyObject *lower = PyObject_CallMethod(raw, "lower", NULL);
    if (lower == NULL) return NULL;
    int truth = PyUnicode_EqualToUTF8(lower, "1") ||
                PyUnicode_EqualToUTF8(lower, "true") ||
                PyUnicode_EqualToUTF8(lower, "yes") ||
                PyUnicode_EqualToUTF8(lower, "on");
    int falsehood = PyUnicode_EqualToUTF8(lower, "0") ||
                    PyUnicode_EqualToUTF8(lower, "false") ||
                    PyUnicode_EqualToUTF8(lower, "no") ||
                    PyUnicode_EqualToUTF8(lower, "off");
    Py_DECREF(lower);
    if (truth) return Py_NewRef(Py_True);
    if (falsehood) return Py_NewRef(Py_False);
    return NULL;
}

static int
query_scan_values(const uint8_t *data, Py_ssize_t len, PyObject *lookup,
                  PyObject **raw_values, Py_ssize_t raw_count)
{
    Py_ssize_t start = 0;
    for (Py_ssize_t i = 0; i <= len; i++) {
        if (i < len && data[i] != '&') continue;
        Py_ssize_t field_len = i - start;
        if (field_len > 0) {
            const uint8_t *field = data + start;
            Py_ssize_t eq = 0;
            while (eq < field_len && field[eq] != '=') eq++;
            PyObject *key = component_to_str(field, eq);
            if (key == NULL) return -1;
            PyObject *positions = PyDict_GetItemWithError(lookup, key);
            Py_DECREF(key);
            if (positions == NULL) {
                if (PyErr_Occurred()) return -1;
            }
            else {
                if (!PyTuple_CheckExact(positions)) {
                    PyErr_SetString(PyExc_TypeError,
                                    "invalid compiled query binding positions");
                    return -1;
                }
                Py_ssize_t count = PyTuple_GET_SIZE(positions);
                int wanted = 0;
                for (Py_ssize_t j = 0; j < count; j++) {
                    Py_ssize_t index = PyLong_AsSsize_t(PyTuple_GET_ITEM(positions, j));
                    if (index < 0 && PyErr_Occurred()) return -1;
                    if (index < 0 || index >= raw_count) {
                        PyErr_SetString(PyExc_RuntimeError,
                                        "query binding position is out of range");
                        return -1;
                    }
                    if (raw_values[index] == NULL) wanted = 1;
                }
                if (wanted) {
                    PyObject *value = eq < field_len
                        ? component_to_str(field + eq + 1, field_len - eq - 1)
                        : PyUnicode_New(0, 127);
                    if (value == NULL) return -1;
                    for (Py_ssize_t j = 0; j < count; j++) {
                        Py_ssize_t index = PyLong_AsSsize_t(PyTuple_GET_ITEM(positions, j));
                        if (index < 0 && PyErr_Occurred()) {
                            Py_DECREF(value);
                            return -1;
                        }
                        if (index < 0 || index >= raw_count) {
                            Py_DECREF(value);
                            PyErr_SetString(PyExc_RuntimeError,
                                            "query binding position is out of range");
                            return -1;
                        }
                        if (raw_values[index] == NULL) {
                            raw_values[index] = Py_NewRef(value);
                        }
                    }
                    Py_DECREF(value);
                }
            }
        }
        start = i + 1;
    }
    return 0;
}

static int
query_bind_entry(PyObject *entry, PyObject *raw, PyObject *kwargs,
                 PyObject **errors)
{
    if (!PyTuple_CheckExact(entry) || PyTuple_GET_SIZE(entry) != 8) {
        PyErr_SetString(PyExc_TypeError, "invalid compiled query binding entry");
        return -1;
    }
    PyObject *name = PyTuple_GET_ITEM(entry, 0);
    PyObject *alias = PyTuple_GET_ITEM(entry, 1);
    int opcode = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 2));
    if (opcode < 0 && PyErr_Occurred()) return -1;
    if (raw == Py_None) {
        int required = PyObject_IsTrue(PyTuple_GET_ITEM(entry, 3));
        if (required < 0) return -1;
        if (required) {
            PyObject *message = PyUnicode_FromString("parameter is required");
            return query_append_error(errors, alias, message, "missing");
        }
        return PyDict_SetItem(kwargs, name, PyTuple_GET_ITEM(entry, 4));
    }

    PyObject *value = NULL;
    if (opcode == QUERY_STR) value = Py_NewRef(raw);
    else if (opcode == QUERY_INT) {
        value = PyLong_FromUnicodeObject(raw, 10);
        if (value == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return query_append_raw_error(
                errors, alias, raw, "%R is not an integer", "int");
        }
    }
    else if (opcode == QUERY_FLOAT) {
        value = PyFloat_FromString(raw);
        if (value == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return query_append_raw_error(
                errors, alias, raw, "%R is not a number", "float");
        }
    }
    else if (opcode == QUERY_BOOL) {
        value = query_convert_bool(raw);
        if (value == NULL && !PyErr_Occurred()) {
            return query_append_raw_error(
                errors, alias, raw, "%R is not a boolean", "bool");
        }
    }
    else {
        PyErr_SetString(PyExc_RuntimeError, "invalid query binding opcode");
        return -1;
    }
    if (value == NULL) return -1;

    PyObject *minimum = PyTuple_GET_ITEM(entry, 5);
    PyObject *maximum = PyTuple_GET_ITEM(entry, 6);
    int clamp = PyObject_IsTrue(PyTuple_GET_ITEM(entry, 7));
    if (clamp < 0) {
        Py_DECREF(value);
        return -1;
    }
    if (minimum != Py_None) {
        int below = PyObject_RichCompareBool(value, minimum, Py_LT);
        if (below < 0) {
            Py_DECREF(value);
            return -1;
        }
        if (below) {
            if (clamp) Py_SETREF(value, Py_NewRef(minimum));
            else {
                Py_DECREF(value);
                return query_append_error(
                    errors, alias,
                    PyUnicode_FromFormat("value must be >= %S", minimum),
                    "minimum");
            }
        }
    }
    if (maximum != Py_None) {
        int above = PyObject_RichCompareBool(value, maximum, Py_GT);
        if (above < 0) {
            Py_DECREF(value);
            return -1;
        }
        if (above) {
            if (clamp) Py_SETREF(value, Py_NewRef(maximum));
            else {
                Py_DECREF(value);
                return query_append_error(
                    errors, alias,
                    PyUnicode_FromFormat("value must be <= %S", maximum),
                    "maximum");
            }
        }
    }
    int inserted = PyDict_SetItem(kwargs, name, value);
    Py_DECREF(value);
    return inserted;
}

PyObject *
wreath_bind_query_into(PyObject *Py_UNUSED(self), PyObject *const *args,
                       Py_ssize_t nargs)
{
    if (nargs != 4) {
        PyErr_Format(PyExc_TypeError,
                     "bind_query_into expected 4 arguments, got %zd", nargs);
        return NULL;
    }
    Py_buffer query;
    if (PyObject_GetBuffer(args[0], &query, PyBUF_SIMPLE) < 0) return NULL;
    PyObject *compiled = args[1];
    PyObject *kwargs = args[2];
    PyObject *errors = args[3] == Py_None ? NULL : Py_NewRef(args[3]);
    if (!PyTuple_CheckExact(compiled) || PyTuple_GET_SIZE(compiled) != 2 ||
        !PyDict_CheckExact(kwargs) ||
        (errors != NULL && !PyList_CheckExact(errors))) {
        PyBuffer_Release(&query);
        Py_XDECREF(errors);
        PyErr_SetString(PyExc_TypeError, "invalid compiled query binding plan");
        return NULL;
    }
    PyObject *lookup = PyTuple_GET_ITEM(compiled, 0);
    PyObject *entries = PyTuple_GET_ITEM(compiled, 1);
    if (!PyDict_CheckExact(lookup) || !PyTuple_CheckExact(entries)) {
        PyBuffer_Release(&query);
        Py_XDECREF(errors);
        PyErr_SetString(PyExc_TypeError, "invalid compiled query binding plan");
        return NULL;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(entries);
    PyObject *local_values[8] = {NULL};
    PyObject **raw_values = local_values;
    if (count > 8) {
        raw_values = PyMem_Calloc((size_t)count, sizeof(*raw_values));
        if (raw_values == NULL) {
            PyErr_NoMemory();
            goto fail;
        }
    }
    if (query_scan_values(query.buf, query.len, lookup, raw_values, count) < 0) goto fail;
    for (Py_ssize_t i = 0; i < count; i++) {
        if (query_bind_entry(PyTuple_GET_ITEM(entries, i),
                             raw_values[i] == NULL ? Py_None : raw_values[i],
                             kwargs, &errors) < 0) {
            goto fail;
        }
    }
    for (Py_ssize_t i = 0; i < count; i++) Py_XDECREF(raw_values[i]);
    if (raw_values != local_values) PyMem_Free(raw_values);
    PyBuffer_Release(&query);
    return errors == NULL ? Py_NewRef(Py_None) : errors;
fail:
    if (raw_values != NULL) {
        for (Py_ssize_t i = 0; i < count; i++) Py_XDECREF(raw_values[i]);
        if (raw_values != local_values) PyMem_Free(raw_values);
    }
    Py_XDECREF(errors);
    PyBuffer_Release(&query);
    return NULL;
}

static int
component_equals_ascii(const uint8_t *src, Py_ssize_t len, const char *wanted)
{
    Py_ssize_t out = 0;
    for (Py_ssize_t index = 0; index < len; index++) {
        uint8_t value = src[index];
        if (value == '%' && index + 2 < len) {
            int hi = hexval(src[index + 1]);
            int lo = hexval(src[index + 2]);
            if (hi >= 0 && lo >= 0) {
                value = (uint8_t)((hi << 4) | lo);
                index += 2;
            }
        }
        else if (value == '+') value = ' ';
        if (wanted[out] == '\0' || value != (uint8_t)wanted[out]) return 0;
        out++;
    }
    return wanted[out] == '\0';
}

static long
page_bounded_value(PyObject *text, long fallback, long ceiling)
{
    if (text == NULL) return fallback;
    PyObject *number = PyLong_FromUnicodeObject(text, 10);
    if (number == NULL) {
        PyErr_Clear();
        return fallback;
    }
    long value = PyLong_AsLong(number);
    Py_DECREF(number);
    if (value == -1 && PyErr_Occurred()) {
        PyErr_Clear();
        return fallback;
    }
    if (value < 1) return 1;
    return value > ceiling ? ceiling : value;
}

static PyObject *
page_sort_tuple(PyObject *text)
{
    if (text == NULL) return PyTuple_New(0);
    Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    Py_ssize_t count = 0;
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index <= length; index++) {
        if (index < length && PyUnicode_READ_CHAR(text, index) != ',') continue;
        Py_ssize_t left = start;
        Py_ssize_t right = index;
        while (left < right && Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(text, left)))
            left++;
        while (right > left && Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(text, right - 1)))
            right--;
        if (right > left) count++;
        start = index + 1;
    }
    PyObject *result = PyTuple_New(count);
    if (result == NULL) return NULL;
    Py_ssize_t out = 0;
    start = 0;
    for (Py_ssize_t index = 0; index <= length; index++) {
        if (index < length && PyUnicode_READ_CHAR(text, index) != ',') continue;
        Py_ssize_t left = start;
        Py_ssize_t right = index;
        while (left < right && Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(text, left)))
            left++;
        while (right > left && Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(text, right - 1)))
            right--;
        if (right > left) {
            PyObject *token = PyUnicode_Substring(text, left, right);
            if (token == NULL) {
                Py_DECREF(result);
                return NULL;
            }
            PyTuple_SET_ITEM(result, out++, token);
        }
        start = index + 1;
    }
    return result;
}

PyObject *
wreath_page_params(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer query;
    long default_size;
    long max_page;
    long max_size;
    if (!PyArg_ParseTuple(args, "y*lll:page_params", &query, &default_size,
                          &max_page, &max_size)) return NULL;
    PyObject *page_text = NULL;
    PyObject *size_text = NULL;
    PyObject *sort_text = NULL;
    const uint8_t *data = query.buf;
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index <= query.len; index++) {
        if (index < query.len && data[index] != '&') continue;
        Py_ssize_t field_len = index - start;
        if (field_len > 0) {
            const uint8_t *field = data + start;
            Py_ssize_t equal = 0;
            while (equal < field_len && field[equal] != '=') equal++;
            const uint8_t *value = equal < field_len ? field + equal + 1 : field + equal;
            Py_ssize_t value_len = equal < field_len ? field_len - equal - 1 : 0;
            PyObject **target = NULL;
            if (page_text == NULL && component_equals_ascii(field, equal, "page"))
                target = &page_text;
            else if (size_text == NULL && component_equals_ascii(field, equal, "size"))
                target = &size_text;
            else if (sort_text == NULL && component_equals_ascii(field, equal, "sort"))
                target = &sort_text;
            if (target != NULL) {
                *target = component_to_str(value, value_len);
                if (*target == NULL) goto error;
            }
        }
        start = index + 1;
    }
    long page = page_bounded_value(page_text, 1, max_page);
    long size = page_bounded_value(size_text, default_size, max_size);
    PyObject *sort = page_sort_tuple(sort_text);
    if (sort == NULL) goto error;
    PyObject *result = Py_BuildValue("llN", page, size, sort);
    Py_XDECREF(page_text);
    Py_XDECREF(size_text);
    Py_XDECREF(sort_text);
    PyBuffer_Release(&query);
    return result;
error:
    Py_XDECREF(page_text);
    Py_XDECREF(size_text);
    Py_XDECREF(sort_text);
    PyBuffer_Release(&query);
    return NULL;
}

/* Parse an application/x-www-form-urlencoded body directly into FormData's
 * two final mappings.  The generic parse_qs API above deliberately returns
 * ordered pairs; Request.form does not need that representation, and used to
 * allocate a list plus one 2-tuple per field only to walk and discard them in
 * Python.  This scan owns no Python declaration and retains no state: decoded
 * strings are materialized only as members of the public result containers. */
PyObject *
wreath_parse_form_urlencoded(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer query;
    Py_ssize_t max_fields;
    PyObject *error_type;
    PyObject *fields = NULL;
    PyObject *every = NULL;
    PyObject *result = NULL;
    Py_ssize_t field_count = 0;

    if (!PyArg_ParseTuple(args, "y*nO:parse_form_urlencoded",
                          &query, &max_fields, &error_type)) {
        return NULL;
    }
    fields = PyDict_New();
    every = PyDict_New();
    if (fields == NULL || every == NULL) goto done;

    const uint8_t *data = query.buf;
    Py_ssize_t len = query.len;
    Py_ssize_t start = 0;
    for (Py_ssize_t i = 0; i <= len; i++) {
        if (i < len && data[i] != '&') continue;
        Py_ssize_t field_len = i - start;
        if (field_len > 0) {
            PyObject *key = NULL;
            PyObject *value = NULL;
            PyObject *values = NULL;
            PyObject *inserted_values = NULL;
            PyObject *first_value = NULL;
            Py_ssize_t eq = 0;

            if (max_fields > 0 && field_count >= max_fields) {
                PyErr_Format(error_type,
                             "urlencoded form exceeds %zd fields", max_fields);
                goto done;
            }
            field_count++;
            while (eq < field_len && data[start + eq] != '=') eq++;
            key = component_to_str(data + start, eq);
            value = eq < field_len
                ? component_to_str(data + start + eq + 1, field_len - eq - 1)
                : PyUnicode_New(0, 127);
            if (key == NULL || value == NULL) {
                Py_XDECREF(key);
                Py_XDECREF(value);
                goto done;
            }

            if (PyDict_SetDefaultRef(fields, key, value, &first_value) < 0) {
                Py_DECREF(key);
                Py_DECREF(value);
                goto done;
            }
            Py_DECREF(first_value);
            values = PyDict_GetItemWithError(every, key);
            if (values == NULL) {
                if (PyErr_Occurred()) {
                    Py_DECREF(key);
                    Py_DECREF(value);
                    goto done;
                }
                inserted_values = PyList_New(0);
                if (inserted_values == NULL ||
                    PyDict_SetItem(every, key, inserted_values) < 0) {
                    Py_XDECREF(inserted_values);
                    Py_DECREF(key);
                    Py_DECREF(value);
                    goto done;
                }
                values = inserted_values;
            }
            if (PyList_Append(values, value) < 0) {
                Py_XDECREF(inserted_values);
                Py_DECREF(key);
                Py_DECREF(value);
                goto done;
            }
            Py_XDECREF(inserted_values);
            Py_DECREF(key);
            Py_DECREF(value);
        }
        start = i + 1;
    }
    result = wreath_tuple2_from_owned(fields, every);
    fields = NULL;
    every = NULL;

done:
    Py_XDECREF(fields);
    Py_XDECREF(every);
    PyBuffer_Release(&query);
    return result;
}

static inline int
urlencode_safe(uint8_t byte)
{
    return (byte >= 'a' && byte <= 'z') ||
           (byte >= 'A' && byte <= 'Z') ||
           (byte >= '0' && byte <= '9') ||
           byte == '_' || byte == '.' || byte == '-' || byte == '~';
}

static int
write_urlencoded(WreathBytesWriter *writer, PyObject *text)
{
    Py_ssize_t length;
    const uint8_t *data = (const uint8_t *)PyUnicode_AsUTF8AndSize(text, &length);
    static const char hex[] = "0123456789ABCDEF";
    if (data == NULL) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        uint8_t byte = data[index];
        if (urlencode_safe(byte)) {
            if (wreath_writer_byte(writer, (char)byte) < 0) return -1;
        }
        else if (byte == ' ') {
            if (wreath_writer_byte(writer, '+') < 0) return -1;
        }
        else {
            char escaped[3] = {'%', hex[byte >> 4], hex[byte & 15]};
            if (wreath_writer_write(writer, escaped, 3) < 0) return -1;
        }
    }
    return 0;
}

PyObject *
wreath_cache_key_selected(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *method, *path, *declared;
    Py_buffer query;
    if (!PyArg_ParseTuple(
            args, "UUy*O!:cache_key_selected", &method, &path, &query,
            &PyTuple_Type, &declared)) return NULL;

    PyObject *values = PyDict_New();
    if (values == NULL) {
        PyBuffer_Release(&query);
        return NULL;
    }
    const uint8_t *data = query.buf;
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index <= query.len; index++) {
        if (index < query.len && data[index] != '&') continue;
        Py_ssize_t field_length = index - start;
        if (field_length != 0) {
            const uint8_t *field = data + start;
            Py_ssize_t equals = 0;
            while (equals < field_length && field[equals] != '=') equals++;
            PyObject *key = component_to_str(field, equals);
            PyObject *value = equals < field_length
                ? component_to_str(field + equals + 1, field_length - equals - 1)
                : PyUnicode_New(0, 127);
            int failed = key == NULL || value == NULL;
            if (!failed) {
                failed = PyDict_SetDefaultRef(values, key, value, NULL) < 0;
            }
            Py_XDECREF(key);
            Py_XDECREF(value);
            if (failed) {
                Py_DECREF(values);
                PyBuffer_Release(&query);
                return NULL;
            }
        }
        start = index + 1;
    }
    PyBuffer_Release(&query);

    WreathBytesWriter writer = {0};
    Py_ssize_t method_length, path_length;
    const char *method_data = PyUnicode_AsUTF8AndSize(method, &method_length);
    const char *path_data = method_data != NULL
        ? PyUnicode_AsUTF8AndSize(path, &path_length) : NULL;
    if (path_data == NULL ||
        wreath_writer_init(&writer, method_length + path_length + 64) < 0 ||
        wreath_writer_write(&writer, method_data, method_length) < 0 ||
        wreath_writer_byte(&writer, ' ') < 0 ||
        wreath_writer_write(&writer, path_data, path_length) < 0) {
        Py_XDECREF(writer.bytes);
        Py_DECREF(values);
        return NULL;
    }

    int first = 1;
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(declared); index++) {
        PyObject *name = PyTuple_GET_ITEM(declared, index);
        PyObject *value = PyDict_GetItemWithError(values, name);
        if (value == NULL) {
            if (PyErr_Occurred()) goto error;
            continue;
        }
        if (wreath_writer_byte(&writer, first ? '?' : '&') < 0 ||
            write_urlencoded(&writer, name) < 0 ||
            wreath_writer_byte(&writer, '=') < 0 ||
            write_urlencoded(&writer, value) < 0) goto error;
        first = 0;
    }
    Py_DECREF(values);
    PyObject *encoded = wreath_writer_finish(&writer);
    if (encoded == NULL) return NULL;
    PyObject *result = PyUnicode_DecodeUTF8(
        PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded), "strict");
    Py_DECREF(encoded);
    return result;

error:
    Py_DECREF(values);
    Py_XDECREF(writer.bytes);
    return NULL;
}

PyObject *
wreath_parse_cookie_data_raw(const uint8_t *data, Py_ssize_t len)
{
    PyObject *cookies = PyDict_New();
    if (cookies == NULL) return NULL;
    Py_ssize_t start = 0;
    while (start <= len) {
        /* Both delimiter searches go through `memchr`: a cookie header is one
         * of the longest a browser sends, and the C library's scan is
         * vectorised where a byte-at-a-time loop is not. */
        const uint8_t *sep = start < len
            ? memchr(data + start, ';', (size_t)(len - start)) : NULL;
        Py_ssize_t i = sep == NULL ? len : (Py_ssize_t)(sep - data);
        Py_ssize_t lo = start;
        Py_ssize_t hi = i;
        start = i + 1;
        while (lo < hi && (data[lo] == ' ' || data[lo] == '\t')) {
            lo++;
        }
        while (hi > lo && (data[hi - 1] == ' ' || data[hi - 1] == '\t')) {
            hi--;
        }
        if (lo >= hi) {
            continue;
        }
        const uint8_t *split = memchr(data + lo, '=', (size_t)(hi - lo));
        if (split == NULL) {
            continue; /* no '=': ignore the fragment */
        }
        Py_ssize_t eq = (Py_ssize_t)(split - data);
        /* Trim the inner edges too, not just the ones facing the ';'. RFC
         * 6265bis 5.8.3 strips WSP from both halves of a cookie-pair; trimming
         * only the fragment left the space glued to the name, so `" a = 1 "`
         * yielded `"a "` and a lookup of `"a"` found nothing. The outer trim
         * above already took the name's leading and the value's trailing run,
         * so only the two edges facing the '=' are left. */
        Py_ssize_t name_hi = eq;
        while (name_hi > lo && (data[name_hi - 1] == ' ' || data[name_hi - 1] == '\t')) {
            name_hi--;
        }
        if (name_hi == lo) {
            continue; /* no name */
        }
        Py_ssize_t value_lo = eq + 1;
        while (value_lo < hi && (data[value_lo] == ' ' || data[value_lo] == '\t')) {
            value_lo++;
        }
        PyObject *name =
            PyUnicode_DecodeLatin1((const char *)data + lo, name_hi - lo, NULL);
        PyObject *value =
            PyUnicode_DecodeLatin1((const char *)data + value_lo, hi - value_lo, NULL);
        int failed = (name == NULL || value == NULL);
        /* First value wins for duplicate cookie names. `SetDefaultRef` says
         * exactly that in one hash and one probe, where `Contains` followed by
         * `SetItem` paid for both twice. */
        if (!failed) {
            failed = PyDict_SetDefaultRef(cookies, name, value, NULL) < 0;
        }
        Py_XDECREF(name);
        Py_XDECREF(value);
        if (failed) {
            Py_DECREF(cookies);
            return NULL;
        }
    }
    return cookies;
}

PyObject *
wreath_parse_cookies(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer header;
    if (!PyArg_ParseTuple(args, "y*:parse_cookies", &header)) return NULL;
    PyObject *result = wreath_parse_cookie_data_raw(header.buf, header.len);
    PyBuffer_Release(&header);
    return result;
}

PyObject *
wreath_parse_cookie_headers(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers_object, *error_type;
    Py_ssize_t limit;
    if (!PyArg_ParseTuple(args, "OnO:parse_cookie_headers", &headers_object,
                          &limit, &error_type)) return NULL;
    PyObject *headers = PySequence_Fast(
        headers_object, "request headers must be a sequence");
    if (headers == NULL) return NULL;
    PyObject **items = PySequence_Fast_ITEMS(headers);
    Py_ssize_t count = PySequence_Fast_GET_SIZE(headers);
    Py_ssize_t total = 0, cookie_count = 0;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *pair = items[index];
        PyObject *name = NULL, *value = NULL;
        if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
            name = PyTuple_GET_ITEM(pair, 0); value = PyTuple_GET_ITEM(pair, 1);
        } else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
            name = PyList_GET_ITEM(pair, 0); value = PyList_GET_ITEM(pair, 1);
        }
        if (name == NULL || !PyBytes_Check(name) || !PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError,
                            "request headers must be (bytes, bytes) pairs");
            Py_DECREF(headers);
            return NULL;
        }
        if (PyBytes_GET_SIZE(name) != 6 ||
            memcmp(PyBytes_AS_STRING(name), "cookie", 6) != 0) continue;
        Py_ssize_t value_length = PyBytes_GET_SIZE(value);
        if (value_length > PY_SSIZE_T_MAX - total - (cookie_count ? 2 : 0)) {
            Py_DECREF(headers); PyErr_NoMemory(); return NULL;
        }
        total += value_length + (cookie_count ? 2 : 0);
        cookie_count++;
        if (total > limit) {
            PyErr_Format(error_type, "Cookie headers are %zd bytes; the limit is %zd",
                         total, limit);
            Py_DECREF(headers);
            return NULL;
        }
    }
    if (cookie_count == 0) {
        Py_DECREF(headers);
        return PyDict_New();
    }
    uint8_t *joined = PyMem_Malloc((size_t)total);
    if (joined == NULL) {
        Py_DECREF(headers); PyErr_NoMemory(); return NULL;
    }
    Py_ssize_t used = 0;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *pair = items[index];
        PyObject *name = PyTuple_Check(pair) ? PyTuple_GET_ITEM(pair, 0)
                                             : PyList_GET_ITEM(pair, 0);
        if (PyBytes_GET_SIZE(name) != 6 ||
            memcmp(PyBytes_AS_STRING(name), "cookie", 6) != 0) continue;
        PyObject *value = PyTuple_Check(pair) ? PyTuple_GET_ITEM(pair, 1)
                                              : PyList_GET_ITEM(pair, 1);
        Py_ssize_t value_length = PyBytes_GET_SIZE(value);
        if (used) { joined[used++] = ';'; joined[used++] = ' '; }
        memcpy(joined + used, PyBytes_AS_STRING(value), (size_t)value_length);
        used += value_length;
    }
    PyObject *result = wreath_parse_cookie_data_raw(joined, used);
    PyMem_Free(joined);
    Py_DECREF(headers);
    return result;
}

/* b64encode(data, urlsafe=False, pad=True) -> str
 *
 * `base64.b64encode` runs a scalar table loop at roughly 0.5 bytes/ns and hands
 * back `bytes` that every caller here immediately decodes to `str`. This
 * encodes at the widest width the CPU has and builds the ASCII string
 * directly, so the intermediate object never exists.
 */
PyObject *
wreath_b64encode(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"data", "urlsafe", "pad", NULL};
    Py_buffer data;
    int urlsafe = 0;
    int pad = 1;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "y*|pp:b64encode", keywords,
                                     &data, &urlsafe, &pad)) {
        return NULL;
    }
    Py_ssize_t room = ((data.len + 2) / 3) * 4;
    PyObject *result = PyUnicode_New(room, 127);
    if (result == NULL) {
        PyBuffer_Release(&data);
        return NULL;
    }
    ptrdiff_t written = wreath_b64_encode(
        (const unsigned char *)data.buf, (ptrdiff_t)data.len,
        (char *)PyUnicode_1BYTE_DATA(result), urlsafe, pad);
    PyBuffer_Release(&data);
    if ((Py_ssize_t)written == room) {
        return result;
    }
    /* Unpadded output is shorter than the padded bound; PyUnicode has no
     * resize for a finished object, so the exact-length copy is made here. */
    PyObject *exact = PyUnicode_FromKindAndData(
        PyUnicode_1BYTE_KIND, PyUnicode_1BYTE_DATA(result), (Py_ssize_t)written);
    Py_DECREF(result);
    return exact;
}

static PyObject *
latin1_bytes(PyObject *value)
{
    return PyUnicode_AsEncodedString(value, "latin-1", "strict");
}

static int
cookie_field_valid(PyObject *value, const char *field, int separator)
{
    PyObject *encoded = latin1_bytes(value);
    if (encoded == NULL) return -1;
    const unsigned char *data = (const unsigned char *)PyBytes_AS_STRING(encoded);
    Py_ssize_t length = PyBytes_GET_SIZE(encoded);
    for (Py_ssize_t i = 0; i < length; i++) {
        if (data[i] < 0x20 || data[i] == 0x7f) {
            PyErr_Format(PyExc_ValueError,
                         "cookie %s contains a control character", field);
            Py_DECREF(encoded);
            return -1;
        }
    }
    if (separator && memchr(data, ';', (size_t)length) != NULL) {
        PyErr_Format(PyExc_ValueError,
                     "cookie %s contains an attribute separator", field);
        Py_DECREF(encoded);
        return -1;
    }
    Py_DECREF(encoded);
    return 0;
}

static int
cookie_write_object(WreathBytesWriter *writer, PyObject *value)
{
    PyObject *encoded = latin1_bytes(value);
    if (encoded == NULL) return -1;
    int result = wreath_writer_write(writer, PyBytes_AS_STRING(encoded),
                                     PyBytes_GET_SIZE(encoded));
    Py_DECREF(encoded);
    return result;
}

static int
cookie_write_part(WreathBytesWriter *writer, const char *prefix, PyObject *value)
{
    return wreath_writer_write(writer, "; ", 2) < 0 ||
           wreath_writer_write(writer, prefix, (Py_ssize_t)strlen(prefix)) < 0 ||
           cookie_write_object(writer, value) < 0 ? -1 : 0;
}

PyObject *
wreath_cookie_header(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *name, *value, *max_age, *expires, *path, *domain, *secure_obj,
             *httponly_obj, *samesite;
    if (!PyArg_UnpackTuple(args, "cookie_header", 9, 9, &name, &value, &max_age,
                           &expires, &path, &domain, &secure_obj, &httponly_obj,
                           &samesite)) return NULL;
    int secure = PyObject_IsTrue(secure_obj);
    int httponly = PyObject_IsTrue(httponly_obj);
    if (secure < 0 || httponly < 0) return NULL;
    PyObject *site = NULL;
    if (samesite != Py_None) {
        site = PyObject_CallMethod(samesite, "lower", NULL);
        if (site == NULL) return NULL;
        int strict = PyUnicode_CompareWithASCIIString(site, "strict") == 0;
        int lax = PyUnicode_CompareWithASCIIString(site, "lax") == 0;
        int none = PyUnicode_CompareWithASCIIString(site, "none") == 0;
        if (!strict && !lax && !none) {
            PyErr_Format(PyExc_ValueError,
                         "samesite must be 'strict', 'lax', or 'none', got %R",
                         site);
            Py_DECREF(site);
            return NULL;
        }
        if (none && !secure) {
            PyErr_SetString(PyExc_ValueError,
                            "SameSite=None cookies must be Secure (RFC 6265bis 5.4.7); pass secure=True");
            Py_DECREF(site);
            return NULL;
        }
    }
    if (cookie_field_valid(name, "name", 1) < 0 ||
        cookie_field_valid(value, "value", 1) < 0 ||
        cookie_field_valid(path, "path", 1) < 0 ||
        (domain != Py_None && cookie_field_valid(domain, "domain", 1) < 0) ||
        (expires != Py_None && cookie_field_valid(expires, "expires", 1) < 0)) {
        Py_XDECREF(site);
        return NULL;
    }
    if (max_age != Py_None && !PyLong_CheckExact(max_age)) {
        PyErr_Format(PyExc_TypeError, "max_age must be an int, not %.200s",
                     Py_TYPE(max_age)->tp_name);
        Py_XDECREF(site);
        return NULL;
    }
    Py_ssize_t name_len;
    const char *name_data = PyUnicode_AsUTF8AndSize(name, &name_len);
    if (name_data == NULL) {
        Py_XDECREF(site);
        return NULL;
    }
    int secure_prefix = name_len >= 9 && memcmp(name_data, "__Secure-", 9) == 0;
    int host_prefix = name_len >= 7 && memcmp(name_data, "__Host-", 7) == 0;
    if (secure_prefix && !secure) {
        PyErr_SetString(PyExc_ValueError,
                        "__Secure- cookies must be Secure; pass secure=True");
        Py_XDECREF(site);
        return NULL;
    }
    int path_is_root = PyUnicode_CompareWithASCIIString(path, "/") == 0;
    if (host_prefix && (!secure || !path_is_root || domain != Py_None)) {
        PyErr_SetString(PyExc_ValueError,
                        "__Host- cookies must be Secure, have Path=/, and set no Domain (RFC 6265bis 4.1.3)");
        Py_XDECREF(site);
        return NULL;
    }
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 128) < 0) {
        Py_XDECREF(site);
        return NULL;
    }
    if (cookie_write_object(&writer, name) < 0 ||
        wreath_writer_byte(&writer, '=') < 0 ||
        cookie_write_object(&writer, value) < 0) goto cookie_error;
    if (max_age != Py_None) {
        PyObject *text = PyObject_Str(max_age);
        if (text == NULL) goto cookie_error;
        int failed = cookie_write_part(&writer, "Max-Age=", text);
        Py_DECREF(text);
        if (failed < 0) goto cookie_error;
    }
    if (expires != Py_None && cookie_write_part(&writer, "Expires=", expires) < 0)
        goto cookie_error;
    int has_path = PyObject_IsTrue(path);
    if (has_path < 0) goto cookie_error;
    if (has_path && cookie_write_part(&writer, "Path=", path) < 0) goto cookie_error;
    if (domain != Py_None && cookie_write_part(&writer, "Domain=", domain) < 0)
        goto cookie_error;
    if (secure && wreath_writer_write(&writer, "; Secure", 8) < 0) goto cookie_error;
    if (httponly && wreath_writer_write(&writer, "; HttpOnly", 10) < 0) goto cookie_error;
    if (site != NULL) {
        const char *spelling = PyUnicode_CompareWithASCIIString(site, "strict") == 0
            ? "Strict" : PyUnicode_CompareWithASCIIString(site, "lax") == 0
            ? "Lax" : "None";
        if (wreath_writer_write(&writer, "; SameSite=", 11) < 0 ||
            wreath_writer_write(&writer, spelling, (Py_ssize_t)strlen(spelling)) < 0)
            goto cookie_error;
    }
    Py_XDECREF(site);
    return wreath_writer_finish(&writer);

cookie_error:
    Py_XDECREF(site);
    Py_XDECREF(writer.bytes);
    return NULL;
}

PyObject *
wreath_log_batch(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *rows, *names, *after, *cursor_type, *record_type, *batch_type;
    if (!PyArg_ParseTuple(args, "OOOOOO:log_batch", &rows, &names, &after,
                          &cursor_type, &record_type, &batch_type)) return NULL;
    PyObject *row_items = PySequence_Fast(rows, "rows must be a sequence");
    PyObject *name_items = PySequence_Fast(names, "names must be a sequence");
    if (row_items == NULL || name_items == NULL) {
        Py_XDECREF(row_items);
        Py_XDECREF(name_items);
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(row_items);
    Py_ssize_t name_count = PySequence_Fast_GET_SIZE(name_items);
    PyObject *records = PyTuple_New(count);
    PyObject *cursor = Py_NewRef(after);
    if (records == NULL) goto log_error;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *row = PySequence_Fast(PySequence_Fast_GET_ITEM(row_items, i),
                                        "a log row must be a sequence");
        if (row == NULL) goto log_error;
        if (PySequence_Fast_GET_SIZE(row) < name_count + 3) {
            PyErr_SetString(PyExc_IndexError, "log row has fewer values than its declaration");
            Py_DECREF(row);
            goto log_error;
        }
        PyObject *xid = PyNumber_Long(PySequence_Fast_GET_ITEM(row, 0));
        PyObject *seq = PyNumber_Long(PySequence_Fast_GET_ITEM(row, 1));
        PyObject *next_cursor = xid != NULL && seq != NULL
            ? PyObject_CallFunctionObjArgs(cursor_type, xid, seq, NULL) : NULL;
        Py_XDECREF(xid);
        Py_XDECREF(seq);
        if (next_cursor == NULL) {
            Py_DECREF(row);
            goto log_error;
        }
        PyObject *values = PyDict_New();
        if (values == NULL) {
            Py_DECREF(next_cursor);
            Py_DECREF(row);
            goto log_error;
        }
        for (Py_ssize_t j = 0; j < name_count; j++) {
            if (PyDict_SetItem(values, PySequence_Fast_GET_ITEM(name_items, j),
                               PySequence_Fast_GET_ITEM(row, j + 3)) < 0) {
                Py_DECREF(values);
                Py_DECREF(next_cursor);
                Py_DECREF(row);
                goto log_error;
            }
        }
        PyObject *record = PyObject_CallFunctionObjArgs(
            record_type, next_cursor, PySequence_Fast_GET_ITEM(row, 2), values, NULL);
        Py_DECREF(values);
        Py_DECREF(row);
        if (record == NULL) {
            Py_DECREF(next_cursor);
            goto log_error;
        }
        Py_SETREF(cursor, next_cursor);
        PyTuple_SET_ITEM(records, i, record);
    }
    PyObject *batch = PyObject_CallFunctionObjArgs(batch_type, records, cursor, NULL);
    Py_DECREF(records);
    Py_DECREF(cursor);
    Py_DECREF(row_items);
    Py_DECREF(name_items);
    return batch;

log_error:
    Py_XDECREF(records);
    Py_XDECREF(cursor);
    Py_DECREF(row_items);
    Py_DECREF(name_items);
    return NULL;
}

static int
temporary_object_name(PyObject *name)
{
    Py_ssize_t length;
    const char *data = PyUnicode_AsUTF8AndSize(name, &length);
    if (data == NULL) return -1;
    if (length < 19 || data[0] != '.' || data[length - 17] != '.' ||
        memcmp(data + length - 4, ".tmp", 4) != 0) return 0;
    for (Py_ssize_t i = length - 16; i < length - 4; i++) {
        if (!((data[i] >= '0' && data[i] <= '9') ||
              (data[i] >= 'a' && data[i] <= 'f'))) return 0;
    }
    return 1;
}

static PyObject *
call_noargs_attr(PyObject *object, const char *name)
{
    PyObject *callable = PyObject_GetAttrString(object, name);
    if (callable == NULL) return NULL;
    PyObject *result = PyObject_CallNoArgs(callable);
    Py_DECREF(callable);
    return result;
}

static PyObject *
call_onearg_attr(PyObject *object, const char *name, PyObject *arg)
{
    PyObject *callable = PyObject_GetAttrString(object, name);
    if (callable == NULL) return NULL;
    PyObject *result = PyObject_CallOneArg(callable, arg);
    Py_DECREF(callable);
    return result;
}

static int
walk_directory(PyObject *path, PyObject *relative, PyObject *scandir,
               PyObject *join, PyObject *keys)
{
    PyObject *scan = PyObject_CallOneArg(scandir, path);
    if (scan == NULL) return -1;
    PyObject *iterator = PyObject_GetIter(scan);
    Py_DECREF(scan);
    if (iterator == NULL) return -1;
    PyObject *entry;
    while ((entry = PyIter_Next(iterator)) != NULL) {
        PyObject *name = PyObject_GetAttrString(entry, "name");
        PyObject *full = name == NULL ? NULL
            : PyObject_CallFunctionObjArgs(join, path, name, NULL);
        PyObject *symlink_obj = full == NULL ? NULL
            : call_noargs_attr(entry, "is_symlink");
        int symlink = symlink_obj == NULL ? -1 : PyObject_IsTrue(symlink_obj);
        Py_XDECREF(symlink_obj);
        if (symlink < 0 || full == NULL) {
            Py_XDECREF(name);
            Py_XDECREF(full);
            Py_DECREF(entry);
            Py_DECREF(iterator);
            return -1;
        }
        if (!symlink) {
            PyObject *directory_obj = call_noargs_attr(entry, "is_dir");
            int directory = directory_obj == NULL ? -1 : PyObject_IsTrue(directory_obj);
            Py_XDECREF(directory_obj);
            if (directory < 0) {
                Py_DECREF(name);
                Py_DECREF(full);
                Py_DECREF(entry);
                Py_DECREF(iterator);
                return -1;
            }
            PyObject *key = PyUnicode_GET_LENGTH(relative) == 0
                ? Py_NewRef(name) : PyUnicode_FromFormat("%U/%U", relative, name);
            int status = 0;
            if (key != NULL) {
                if (directory) {
                    status = walk_directory(full, key, scandir, join, keys);
                } else {
                    int temporary = temporary_object_name(name);
                    status = temporary < 0 ? -1
                                           : !temporary ? PyList_Append(keys, key) : 0;
                }
            }
            if (key == NULL || status < 0) {
                Py_XDECREF(key);
                Py_DECREF(name);
                Py_DECREF(full);
                Py_DECREF(entry);
                Py_DECREF(iterator);
                return -1;
            }
            Py_DECREF(key);
        }
        Py_DECREF(name);
        Py_DECREF(full);
        Py_DECREF(entry);
    }
    int failed = PyErr_Occurred() ? -1 : 0;
    Py_DECREF(iterator);
    return failed;
}

PyObject *
wreath_local_walk(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *root, *scandir, *join;
    if (!PyArg_ParseTuple(args, "OOO:local_walk", &root, &scandir, &join))
        return NULL;
    PyObject *keys = PyList_New(0);
    PyObject *relative = PyUnicode_New(0, 127);
    if (keys == NULL || relative == NULL ||
        walk_directory(root, relative, scandir, join, keys) < 0 ||
        PyList_Sort(keys) < 0) {
        Py_XDECREF(keys);
        Py_XDECREF(relative);
        return NULL;
    }
    Py_DECREF(relative);
    return keys;
}

typedef struct {
    uint64_t minutes;
    uint32_t hours;
    uint32_t days;
    uint16_t months;
    uint8_t weekdays;
    uint8_t dom_restricted;
    uint8_t dow_restricted;
} RecurrencePlan;

#define RECURRENCE_PLAN_CAPSULE_NAME "wreath.recurrence_plan"

static void
recurrence_plan_destroy(PyObject *capsule)
{
    RecurrencePlan *plan = PyCapsule_GetPointer(
        capsule, RECURRENCE_PLAN_CAPSULE_NAME);
    if (plan == NULL) {
        PyErr_WriteUnraisable(capsule);
        return;
    }
    PyMem_Free(plan);
}

static int
recurrence_mask(PyObject *source, int low, int high, uint64_t *mask_out,
                const char *name)
{
    PyObject *items = PySequence_Fast(source, name);
    if (items == NULL) return -1;
    uint64_t mask = 0;
    PyObject **values = PySequence_Fast_ITEMS(items);
    Py_ssize_t count = PySequence_Fast_GET_SIZE(items);
    for (Py_ssize_t index = 0; index < count; index++) {
        long value = PyLong_AsLong(values[index]);
        if ((value == -1 && PyErr_Occurred()) || value < low || value > high) {
            if (!PyErr_Occurred()) PyErr_Format(
                PyExc_ValueError, "%s recurrence value %ld is outside %d..%d",
                name, value, low, high);
            Py_DECREF(items);
            return -1;
        }
        mask |= UINT64_C(1) << (unsigned int)value;
    }
    Py_DECREF(items);
    *mask_out = mask;
    return 0;
}

PyObject *
wreath_recurrence_plan(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *minutes, *hours, *days, *months, *weekdays;
    int dom_restricted, dow_restricted;
    if (!PyArg_ParseTuple(
            args, "OOOOOpp:recurrence_plan", &minutes, &hours, &days,
            &months, &weekdays, &dom_restricted, &dow_restricted)) return NULL;
    RecurrencePlan *plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) return PyErr_NoMemory();
    uint64_t hours_mask, days_mask, months_mask, weekdays_mask;
    if (recurrence_mask(minutes, 0, 59, &plan->minutes, "minute") < 0 ||
        recurrence_mask(hours, 0, 23, &hours_mask, "hour") < 0 ||
        recurrence_mask(days, 1, 31, &days_mask, "day") < 0 ||
        recurrence_mask(months, 1, 12, &months_mask, "month") < 0 ||
        recurrence_mask(weekdays, 0, 6, &weekdays_mask, "weekday") < 0) {
        PyMem_Free(plan);
        return NULL;
    }
    plan->hours = (uint32_t)hours_mask;
    plan->days = (uint32_t)days_mask;
    plan->months = (uint16_t)months_mask;
    plan->weekdays = (uint8_t)weekdays_mask;
    plan->dom_restricted = (uint8_t)dom_restricted;
    plan->dow_restricted = (uint8_t)dow_restricted;
    return PyCapsule_New(
        plan, RECURRENCE_PLAN_CAPSULE_NAME, recurrence_plan_destroy);
}

static int
recurrence_leap(int year)
{
    return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
}

static int
recurrence_month_days(int year, int month)
{
    static const unsigned char lengths[] = {
        0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    };
    return month == 2 && recurrence_leap(year) ? 29 : lengths[month];
}

static int64_t
recurrence_ordinal(int year, int month, int day)
{
    int64_t previous = year - 1;
    int64_t total = previous * 365 + previous / 4 - previous / 100 + previous / 400;
    static const unsigned short before_month[] = {
        0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334,
    };
    total += before_month[month] + day - 1;
    if (month > 2 && recurrence_leap(year)) total++;
    return total;
}

static void
recurrence_advance_day(int *year, int *month, int *day)
{
    (*day)++;
    if (*day <= recurrence_month_days(*year, *month)) return;
    *day = 1;
    (*month)++;
    if (*month <= 12) return;
    *month = 1;
    (*year)++;
}

static int
recurrence_datetime_part(PyObject *value, const char *name, int *out)
{
    PyObject *part = PyObject_GetAttrString(value, name);
    if (part == NULL) return -1;
    long number = PyLong_AsLong(part);
    Py_DECREF(part);
    if (number == -1 && PyErr_Occurred()) return -1;
    *out = (int)number;
    return 0;
}

PyObject *
wreath_recurrence_next(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *start, *plan_object, *tz, *from_wall_clock, *wall_clock, *utc,
             *datetime_type;
    Py_ssize_t search_days;
    if (!PyArg_ParseTuple(
            args, "OOOOOOOn:recurrence_next", &start, &plan_object, &tz,
            &from_wall_clock, &wall_clock, &utc, &datetime_type, &search_days))
        return NULL;
    RecurrencePlan *plan = PyCapsule_GetPointer(
        plan_object, RECURRENCE_PLAN_CAPSULE_NAME);
    if (plan == NULL) return NULL;
    int is_datetime = PyObject_IsInstance(start, datetime_type);
    if (is_datetime < 0) return NULL;
    if (!is_datetime) {
        PyErr_SetString(PyExc_TypeError, "recurrence start must be a datetime");
        return NULL;
    }
    int year, month, day, start_hour, start_minute;
    if (recurrence_datetime_part(start, "year", &year) < 0 ||
        recurrence_datetime_part(start, "month", &month) < 0 ||
        recurrence_datetime_part(start, "day", &day) < 0 ||
        recurrence_datetime_part(start, "hour", &start_hour) < 0 ||
        recurrence_datetime_part(start, "minute", &start_minute) < 0) return NULL;
    int python_weekday = (int)(recurrence_ordinal(year, month, day) % 7);
    for (Py_ssize_t offset = 0; offset < search_days; offset++) {
        int month_ok = (plan->months & (UINT16_C(1) << month)) != 0;
        int dom_ok = (plan->days & (UINT32_C(1) << day)) != 0;
        int cron_weekday = (python_weekday + 1) % 7;
        int dow_ok = (plan->weekdays & (UINT8_C(1) << cron_weekday)) != 0;
        int day_ok = plan->dom_restricted && plan->dow_restricted
            ? dom_ok || dow_ok : dom_ok && dow_ok;
        if (month_ok && day_ok) {
            for (int hour = 0; hour < 24; hour++) {
                if ((plan->hours & (UINT32_C(1) << hour)) == 0) continue;
                for (int minute = 0; minute < 60; minute++) {
                    if ((plan->minutes & (UINT64_C(1) << minute)) == 0) continue;
                    if (offset == 0 &&
                        (hour < start_hour ||
                         (hour == start_hour && minute <= start_minute))) continue;
                    PyObject *local = PyObject_CallFunction(
                        datetime_type, "iiiii", year, month, day, hour, minute);
                    if (local == NULL) goto recurrence_error;
                    PyObject *candidate = PyObject_CallFunctionObjArgs(
                        from_wall_clock, local, tz, NULL);
                    PyObject *normalized = candidate == NULL ? NULL
                        : call_onearg_attr(candidate, "astimezone", utc);
                    PyObject *readback = normalized == NULL ? NULL
                        : PyObject_CallFunctionObjArgs(
                            wall_clock, normalized, tz, NULL);
                    Py_XDECREF(normalized);
                    if (readback == NULL) {
                        Py_XDECREF(candidate);
                        Py_DECREF(local);
                        goto recurrence_error;
                    }
                    int exists = PyObject_RichCompareBool(readback, local, Py_EQ);
                    Py_DECREF(readback);
                    Py_DECREF(local);
                    if (exists < 0) {
                        Py_DECREF(candidate);
                        goto recurrence_error;
                    }
                    if (exists) return candidate;
                    Py_DECREF(candidate);
                }
            }
        }
        recurrence_advance_day(&year, &month, &day);
        python_weekday = (python_weekday + 1) % 7;
    }
    Py_RETURN_NONE;

recurrence_error:
    return NULL;
}

static int
writer_u32le(WreathBytesWriter *writer, unsigned long value)
{
    unsigned char data[4] = {
        (unsigned char)value, (unsigned char)(value >> 8),
        (unsigned char)(value >> 16), (unsigned char)(value >> 24)
    };
    return wreath_writer_write(writer, (const char *)data, 4);
}

static int
writer_u64le(WreathBytesWriter *writer, unsigned long long value)
{
    unsigned char data[8];
    for (int i = 0; i < 8; i++) data[i] = (unsigned char)(value >> (i * 8));
    return wreath_writer_write(writer, (const char *)data, 8);
}

static int
writer_text(WreathBytesWriter *writer, PyObject *text)
{
    PyObject *encoded = PyUnicode_AsUTF8String(text);
    if (encoded == NULL) return -1;
    Py_ssize_t length = PyBytes_GET_SIZE(encoded);
    if ((size_t)length > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError,
                        "attempt recording field is too long to encode");
        Py_DECREF(encoded);
        return -1;
    }
    int failed = writer_u32le(writer, (unsigned long)length) < 0 ||
        wreath_writer_write(writer, PyBytes_AS_STRING(encoded), length) < 0;
    Py_DECREF(encoded);
    return failed ? -1 : 0;
}

static PyObject *
attribute(PyObject *object, const char *name)
{
    return PyObject_GetAttrString(object, name);
}

PyObject *
wreath_attempt_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *job_id_obj, *fence_obj, *attempt_obj, *max_attempts_obj,
             *argument_count_obj, *boundaries, *texts, *arguments;
    if (!PyArg_ParseTuple(args, "OOOOOOOO:attempt_encode", &job_id_obj, &fence_obj,
                          &attempt_obj, &max_attempts_obj, &argument_count_obj,
                          &boundaries, &texts, &arguments)) return NULL;
    long long job_id = PyLong_AsLongLong(job_id_obj);
    long long fence = PyLong_AsLongLong(fence_obj);
    unsigned long attempt = PyLong_AsUnsignedLong(attempt_obj);
    unsigned long max_attempts = PyLong_AsUnsignedLong(max_attempts_obj);
    unsigned long argument_count = PyLong_AsUnsignedLong(argument_count_obj);
    if (PyErr_Occurred() || attempt > UINT32_MAX || max_attempts > UINT32_MAX ||
        argument_count > UINT32_MAX) return NULL;
    PyObject *boundary_items = PySequence_Fast(boundaries,
                                               "boundaries must be a sequence");
    PyObject *text_items = PySequence_Fast(texts, "texts must be a sequence");
    PyObject *argument_items = PySequence_Fast(arguments,
                                               "arguments must be a sequence");
    if (boundary_items == NULL || text_items == NULL || argument_items == NULL) {
        Py_XDECREF(boundary_items);
        Py_XDECREF(text_items);
        Py_XDECREF(argument_items);
        return NULL;
    }
    Py_ssize_t boundary_count = PySequence_Fast_GET_SIZE(boundary_items);
    if (boundary_count > UINT32_MAX || PySequence_Fast_GET_SIZE(text_items) != 8)
        goto attempt_shape_error;
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 512) < 0) goto attempt_error;
    if (wreath_writer_write(&writer, "ATT1\x01\0\0\0", 8) < 0 ||
        writer_u32le(&writer, 0) < 0 ||
        writer_u64le(&writer, (unsigned long long)job_id) < 0 ||
        writer_u64le(&writer, (unsigned long long)fence) < 0 ||
        writer_u32le(&writer, attempt) < 0 ||
        writer_u32le(&writer, max_attempts) < 0 ||
        writer_u32le(&writer, argument_count) < 0 ||
        writer_u32le(&writer, (unsigned long)boundary_count) < 0) goto encode_error;
    for (Py_ssize_t i = 0; i < 8; i++) {
        if (writer_text(&writer, PySequence_Fast_GET_ITEM(text_items, i)) < 0)
            goto encode_error;
    }
    for (Py_ssize_t i = 0; i < boundary_count; i++) {
        PyObject *event = PySequence_Fast_GET_ITEM(boundary_items, i);
        PyObject *seam_obj = attribute(event, "seam");
        PyObject *coordinate_obj = attribute(event, "coordinate");
        PyObject *target = attribute(event, "target");
        PyObject *error_type = attribute(event, "error_type");
        if (seam_obj == NULL || coordinate_obj == NULL || target == NULL ||
            error_type == NULL) {
            Py_XDECREF(seam_obj); Py_XDECREF(coordinate_obj);
            Py_XDECREF(target); Py_XDECREF(error_type);
            goto encode_error;
        }
        unsigned long seam = PyLong_AsUnsignedLong(seam_obj);
        long coordinate = PyLong_AsLong(coordinate_obj);
        Py_DECREF(seam_obj);
        Py_DECREF(coordinate_obj);
        if (PyErr_Occurred() || seam > 255 || coordinate < INT32_MIN ||
            coordinate > INT32_MAX || wreath_writer_byte(&writer, (char)seam) < 0 ||
            writer_u32le(&writer, (unsigned long)(uint32_t)coordinate) < 0 ||
            writer_text(&writer, target) < 0 || writer_text(&writer, error_type) < 0) {
            Py_DECREF(target);
            Py_DECREF(error_type);
            goto encode_error;
        }
        Py_DECREF(target);
        Py_DECREF(error_type);
    }
    Py_ssize_t argument_fields = PySequence_Fast_GET_SIZE(argument_items);
    if (argument_fields != 0) {
        if (argument_fields > UINT32_MAX ||
            writer_u32le(&writer, (unsigned long)argument_fields) < 0)
            goto encode_error;
        for (Py_ssize_t i = 0; i < argument_fields; i++) {
            PyObject *pair = PySequence_Fast(PySequence_Fast_GET_ITEM(argument_items, i),
                                             "an argument must be a pair");
            if (pair == NULL) goto encode_error;
            if (PySequence_Fast_GET_SIZE(pair) != 2) {
                PyErr_SetString(PyExc_ValueError, "an argument must be a pair");
                Py_DECREF(pair);
                goto encode_error;
            }
            int failed = writer_text(&writer, PySequence_Fast_GET_ITEM(pair, 0)) < 0 ||
                         writer_text(&writer, PySequence_Fast_GET_ITEM(pair, 1)) < 0;
            Py_DECREF(pair);
            if (failed) goto encode_error;
        }
    }
    if (writer.len > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "attempt recording is too long to encode");
        goto encode_error;
    }
    uint32_t total = (uint32_t)writer.len;
    writer.buf[8] = (char)total;
    writer.buf[9] = (char)(total >> 8);
    writer.buf[10] = (char)(total >> 16);
    writer.buf[11] = (char)(total >> 24);
    Py_DECREF(boundary_items);
    Py_DECREF(text_items);
    Py_DECREF(argument_items);
    return wreath_writer_finish(&writer);

attempt_shape_error:
    PyErr_SetString(PyExc_ValueError, "attempt record fields have an invalid shape");
attempt_error:
    Py_DECREF(boundary_items);
    Py_DECREF(text_items);
    Py_DECREF(argument_items);
    return NULL;
encode_error:
    Py_XDECREF(writer.bytes);
    goto attempt_error;
}

static uint32_t
read_u32le(const unsigned char *data)
{
    return (uint32_t)data[0] | (uint32_t)data[1] << 8 |
           (uint32_t)data[2] << 16 | (uint32_t)data[3] << 24;
}

static uint64_t
read_u64le(const unsigned char *data)
{
    uint64_t value = 0;
    for (int i = 7; i >= 0; i--) value = (value << 8) | data[i];
    return value;
}

static PyObject *
read_attempt_text(const unsigned char *data, Py_ssize_t length, Py_ssize_t *offset,
                  PyObject *error_type)
{
    if (*offset > length - 4) {
        PyErr_SetString(error_type,
                        "attempt recording is truncated inside a text field");
        return NULL;
    }
    uint32_t size = read_u32le(data + *offset);
    *offset += 4;
    if ((uint64_t)size > (uint64_t)(length - *offset)) {
        PyErr_Format(error_type,
                     "attempt recording is truncated: a field declares %u bytes and only %zd remain",
                     size, length - *offset);
        return NULL;
    }
    PyObject *text = PyUnicode_DecodeUTF8((const char *)data + *offset, size, "strict");
    *offset += size;
    return text;
}

PyObject *
wreath_attempt_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer payload;
    PyObject *error_type, *boundary_type, *record_type;
    if (!PyArg_ParseTuple(args, "y*OOO:attempt_decode", &payload, &error_type,
                          &boundary_type, &record_type)) return NULL;
    const unsigned char *data = payload.buf;
    Py_ssize_t length = payload.len;
    if (length < 12) {
        PyErr_Format(error_type,
                     "attempt recording is truncated: %zd bytes is shorter than its 12-byte header",
                     length);
        goto decode_error;
    }
    if (memcmp(data, "ATT1", 4) != 0) {
        PyObject *magic = PyBytes_FromStringAndSize((const char *)data, 4);
        if (magic != NULL) PyErr_Format(error_type,
                                       "not an attempt recording: bad record magic %R", magic);
        Py_XDECREF(magic);
        goto decode_error;
    }
    if (data[4] != 1) {
        PyErr_Format(error_type, "unsupported attempt record version %u", data[4]);
        goto decode_error;
    }
    if (data[5] & 1) {
        PyErr_SetString(error_type,
                        "attempt recording is chunked across records; it is refused rather than joined, because a partially assembled attempt reports fewer boundaries than the attempt crossed and reads as complete");
        goto decode_error;
    }
    uint32_t total = read_u32le(data + 8);
    if ((uint64_t)total != (uint64_t)length) {
        PyErr_Format(error_type,
                     "attempt recording is truncated: it declares %u bytes and holds %zd",
                     total, length);
        goto decode_error;
    }
    if (length < 44) {
        PyErr_SetString(error_type, "attempt recording is truncated inside its fixed fields");
        goto decode_error;
    }
    int64_t job_id = (int64_t)read_u64le(data + 12);
    int64_t fence = (int64_t)read_u64le(data + 20);
    uint32_t attempt = read_u32le(data + 28);
    uint32_t max_attempts = read_u32le(data + 32);
    uint32_t argument_count = read_u32le(data + 36);
    uint32_t boundary_count = read_u32le(data + 40);
    Py_ssize_t offset = 44;
    PyObject *texts[8] = {0};
    for (int i = 0; i < 8; i++) {
        texts[i] = read_attempt_text(data, length, &offset, error_type);
        if (texts[i] == NULL) goto decode_objects_error;
    }
    PyObject *boundaries = PyTuple_New(boundary_count);
    if (boundaries == NULL) goto decode_objects_error;
    for (uint32_t i = 0; i < boundary_count; i++) {
        if (offset > length - 5) {
            PyErr_SetString(error_type,
                            "attempt recording is truncated inside a boundary");
            Py_DECREF(boundaries);
            goto decode_objects_error;
        }
        PyObject *seam = PyLong_FromUnsignedLong(data[offset]);
        int32_t coordinate_value = (int32_t)read_u32le(data + offset + 1);
        PyObject *coordinate = PyLong_FromLong(coordinate_value);
        offset += 5;
        PyObject *target = read_attempt_text(data, length, &offset, error_type);
        PyObject *failure = target == NULL ? NULL
            : read_attempt_text(data, length, &offset, error_type);
        PyObject *event = seam != NULL && coordinate != NULL && target != NULL && failure != NULL
            ? PyObject_CallFunctionObjArgs(boundary_type, seam, target, coordinate,
                                           failure, NULL) : NULL;
        Py_XDECREF(seam); Py_XDECREF(coordinate); Py_XDECREF(target); Py_XDECREF(failure);
        if (event == NULL) {
            Py_DECREF(boundaries);
            goto decode_objects_error;
        }
        PyTuple_SET_ITEM(boundaries, i, event);
    }
    PyObject *argument_values = PyTuple_New(0);
    if (argument_values == NULL) {
        Py_DECREF(boundaries);
        goto decode_objects_error;
    }
    if (offset < length) {
        if (offset > length - 4) {
            PyErr_SetString(error_type,
                            "attempt recording is truncated inside its argument count");
            Py_DECREF(boundaries); Py_DECREF(argument_values);
            goto decode_objects_error;
        }
        uint32_t argument_fields = read_u32le(data + offset);
        offset += 4;
        Py_SETREF(argument_values, PyTuple_New(argument_fields));
        if (argument_values == NULL) {
            Py_DECREF(boundaries);
            goto decode_objects_error;
        }
        for (uint32_t i = 0; i < argument_fields; i++) {
            PyObject *name = read_attempt_text(data, length, &offset, error_type);
            PyObject *captured = name == NULL ? NULL
                : read_attempt_text(data, length, &offset, error_type);
            PyObject *pair = wreath_tuple2_from_owned(name, captured);
            if (pair == NULL) {
                Py_DECREF(boundaries); Py_DECREF(argument_values);
                goto decode_objects_error;
            }
            PyTuple_SET_ITEM(argument_values, i, pair);
        }
    }
    PyObject *values[15] = {
        PyLong_FromLongLong(job_id), Py_NewRef(texts[0]), Py_NewRef(texts[1]),
        PyLong_FromUnsignedLong(attempt), PyLong_FromUnsignedLong(max_attempts),
        Py_NewRef(texts[2]), Py_NewRef(texts[3]), PyLong_FromLongLong(fence),
        Py_NewRef(texts[4]), boundaries, Py_NewRef(texts[5]), Py_NewRef(texts[6]),
        Py_NewRef(texts[7]), PyLong_FromUnsignedLong(argument_count), argument_values
    };
    PyObject *record = NULL;
    for (int i = 0; i < 15; i++) {
        if (values[i] == NULL) goto record_values_error;
    }
    record = PyObject_Vectorcall(record_type, values, 15, NULL);
record_values_error:
    for (int i = 0; i < 15; i++) Py_XDECREF(values[i]);
    for (int i = 0; i < 8; i++) Py_DECREF(texts[i]);
    PyBuffer_Release(&payload);
    return record;

decode_objects_error:
    for (int i = 0; i < 8; i++) Py_XDECREF(texts[i]);
decode_error:
    PyBuffer_Release(&payload);
    return NULL;
}

typedef struct {
    PyObject *name;
    uint32_t crc;
    uint64_t size;
    uint64_t offset;
} ZipEntry;

typedef struct {
    PyObject_HEAD
    PyObject *crc32;
    ZipEntry *entries;
    Py_ssize_t count;
    Py_ssize_t capacity;
    uint64_t offset;
    int open;
} ZipBuilder;

static int
writer_u16le(WreathBytesWriter *writer, unsigned int value)
{
    unsigned char data[2] = {(unsigned char)value, (unsigned char)(value >> 8)};
    return wreath_writer_write(writer, (const char *)data, 2);
}

static int
zip_reserve(ZipBuilder *self)
{
    if (self->count < self->capacity) return 0;
    Py_ssize_t capacity = self->capacity == 0 ? 16 : self->capacity * 2;
    if (capacity < self->capacity ||
        (size_t)capacity > SIZE_MAX / sizeof(ZipEntry)) {
        PyErr_NoMemory();
        return -1;
    }
    ZipEntry *entries = PyMem_Realloc(self->entries,
                                      (size_t)capacity * sizeof(ZipEntry));
    if (entries == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->entries = entries;
    self->capacity = capacity;
    return 0;
}

static int
zip_init(ZipBuilder *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"crc32", NULL};
    PyObject *crc32;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:ZipBuilder", keywords,
                                     &crc32)) return -1;
    if (!PyCallable_Check(crc32)) {
        PyErr_SetString(PyExc_TypeError, "crc32 must be callable");
        return -1;
    }
    self->crc32 = Py_NewRef(crc32);
    return 0;
}

static int
zip_traverse(ZipBuilder *self, visitproc visit, void *arg)
{
    Py_VISIT(self->crc32);
    for (Py_ssize_t i = 0; i < self->count; i++) Py_VISIT(self->entries[i].name);
    return 0;
}

static int
zip_clear(ZipBuilder *self)
{
    Py_CLEAR(self->crc32);
    for (Py_ssize_t i = 0; i < self->count; i++) Py_CLEAR(self->entries[i].name);
    return 0;
}

static void
zip_dealloc(ZipBuilder *self)
{
    PyObject_GC_UnTrack(self);
    zip_clear(self);
    PyMem_Free(self->entries);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
zip_begin(ZipBuilder *self, PyObject *name)
{
    if (!PyBytes_Check(name)) {
        PyErr_SetString(PyExc_TypeError, "zip entry name must be bytes");
        return NULL;
    }
    if (self->open) {
        PyErr_SetString(PyExc_RuntimeError, "the preceding zip entry is still open");
        return NULL;
    }
    if (PyBytes_GET_SIZE(name) > UINT16_MAX || zip_reserve(self) < 0) return NULL;
    ZipEntry *entry = &self->entries[self->count++];
    entry->name = Py_NewRef(name);
    entry->crc = 0;
    entry->size = 0;
    entry->offset = self->offset;
    self->open = 1;
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 30 + PyBytes_GET_SIZE(name)) < 0) {
        Py_CLEAR(entry->name);
        self->count--;
        self->open = 0;
        return NULL;
    }
    if (writer_u32le(&writer, 0x04034b50) < 0 || writer_u16le(&writer, 45) < 0 ||
        writer_u16le(&writer, 0x0808) < 0 || writer_u16le(&writer, 0) < 0 ||
        writer_u16le(&writer, 0) < 0 || writer_u16le(&writer, 0x21) < 0 ||
        writer_u32le(&writer, 0) < 0 || writer_u32le(&writer, 0) < 0 ||
        writer_u32le(&writer, 0) < 0 ||
        writer_u16le(&writer, (unsigned int)PyBytes_GET_SIZE(name)) < 0 ||
        writer_u16le(&writer, 0) < 0 ||
        wreath_writer_write(&writer, PyBytes_AS_STRING(name), PyBytes_GET_SIZE(name)) < 0) {
        Py_XDECREF(writer.bytes);
        Py_CLEAR(entry->name);
        self->count--;
        self->open = 0;
        return NULL;
    }
    PyObject *header = wreath_writer_finish(&writer);
    if (header != NULL) self->offset += (uint64_t)PyBytes_GET_SIZE(header);
    return header;
}

static PyObject *
zip_feed(ZipBuilder *self, PyObject *chunk)
{
    if (!self->open) {
        PyErr_SetString(PyExc_RuntimeError, "no zip entry is open");
        return NULL;
    }
    Py_ssize_t length = PyObject_Length(chunk);
    if (length < 0) return NULL;
    ZipEntry *entry = &self->entries[self->count - 1];
    PyObject *crc = PyLong_FromUnsignedLong(entry->crc);
    PyObject *updated = crc == NULL ? NULL
        : PyObject_CallFunctionObjArgs(self->crc32, chunk, crc, NULL);
    Py_XDECREF(crc);
    if (updated == NULL) return NULL;
    unsigned long result = PyLong_AsUnsignedLong(updated);
    Py_DECREF(updated);
    if (PyErr_Occurred()) return NULL;
    entry->crc = (uint32_t)result;
    entry->size += (uint64_t)length;
    self->offset += (uint64_t)length;
    Py_RETURN_NONE;
}

static PyObject *
zip_end(ZipBuilder *self, PyObject *Py_UNUSED(ignored))
{
    if (!self->open) {
        PyErr_SetString(PyExc_RuntimeError, "no zip entry is open");
        return NULL;
    }
    ZipEntry *entry = &self->entries[self->count - 1];
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 24) < 0) return NULL;
    if (writer_u32le(&writer, 0x08074b50) < 0 ||
        writer_u32le(&writer, entry->crc) < 0 ||
        writer_u64le(&writer, entry->size) < 0 ||
        writer_u64le(&writer, entry->size) < 0) {
        Py_XDECREF(writer.bytes);
        return NULL;
    }
    self->open = 0;
    self->offset += 24;
    return wreath_writer_finish(&writer);
}

static PyObject *
zip_finish(ZipBuilder *self, PyObject *Py_UNUSED(ignored))
{
    if (self->open) {
        PyErr_SetString(PyExc_RuntimeError, "the final zip entry is still open");
        return NULL;
    }
    uint64_t cd_start = self->offset;
    Py_ssize_t room = 0;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        Py_ssize_t name_length = PyBytes_GET_SIZE(self->entries[i].name);
        if (name_length > PY_SSIZE_T_MAX - 74 - room) {
            PyErr_NoMemory();
            return NULL;
        }
        room += 74 + name_length;
    }
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, room) < 0) return NULL;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        ZipEntry *entry = &self->entries[i];
        Py_ssize_t name_length = PyBytes_GET_SIZE(entry->name);
        if (writer_u32le(&writer, 0x02014b50) < 0 ||
            writer_u16le(&writer, 45) < 0 || writer_u16le(&writer, 45) < 0 ||
            writer_u16le(&writer, 0x0808) < 0 || writer_u16le(&writer, 0) < 0 ||
            writer_u16le(&writer, 0) < 0 || writer_u16le(&writer, 0x21) < 0 ||
            writer_u32le(&writer, entry->crc) < 0 ||
            writer_u32le(&writer, UINT32_MAX) < 0 ||
            writer_u32le(&writer, UINT32_MAX) < 0 ||
            writer_u16le(&writer, (unsigned int)name_length) < 0 ||
            writer_u16le(&writer, 28) < 0 || writer_u16le(&writer, 0) < 0 ||
            writer_u16le(&writer, 0) < 0 || writer_u16le(&writer, 0) < 0 ||
            writer_u32le(&writer, 0) < 0 || writer_u32le(&writer, UINT32_MAX) < 0 ||
            wreath_writer_write(&writer, PyBytes_AS_STRING(entry->name), name_length) < 0 ||
            writer_u16le(&writer, 0x0001) < 0 || writer_u16le(&writer, 24) < 0 ||
            writer_u64le(&writer, entry->size) < 0 ||
            writer_u64le(&writer, entry->size) < 0 ||
            writer_u64le(&writer, entry->offset) < 0) {
            Py_XDECREF(writer.bytes);
            return NULL;
        }
    }
    PyObject *directory = wreath_writer_finish(&writer);
    if (directory == NULL) return NULL;
    uint64_t cd_size = (uint64_t)PyBytes_GET_SIZE(directory);
    uint64_t count = (uint64_t)self->count;
    uint64_t z64_offset = cd_start + cd_size;
    unsigned int count16 = count > UINT16_MAX ? UINT16_MAX : (unsigned int)count;
    unsigned long cd_size32 = cd_size > UINT32_MAX ? UINT32_MAX : (unsigned long)cd_size;
    unsigned long cd_start32 = cd_start > UINT32_MAX ? UINT32_MAX : (unsigned long)cd_start;
    WreathBytesWriter z64 = {0}, locator = {0}, eocd = {0};
    if (wreath_writer_init(&z64, 56) < 0 || wreath_writer_init(&locator, 20) < 0 ||
        wreath_writer_init(&eocd, 22) < 0) {
        Py_DECREF(directory);
        Py_XDECREF(z64.bytes); Py_XDECREF(locator.bytes); Py_XDECREF(eocd.bytes);
        return NULL;
    }
    if (writer_u32le(&z64, 0x06064b50) < 0 || writer_u64le(&z64, 44) < 0 ||
        writer_u16le(&z64, 45) < 0 || writer_u16le(&z64, 45) < 0 ||
        writer_u32le(&z64, 0) < 0 || writer_u32le(&z64, 0) < 0 ||
        writer_u64le(&z64, count) < 0 || writer_u64le(&z64, count) < 0 ||
        writer_u64le(&z64, cd_size) < 0 || writer_u64le(&z64, cd_start) < 0 ||
        writer_u32le(&locator, 0x07064b50) < 0 || writer_u32le(&locator, 0) < 0 ||
        writer_u64le(&locator, z64_offset) < 0 || writer_u32le(&locator, 1) < 0 ||
        writer_u32le(&eocd, 0x06054b50) < 0 || writer_u16le(&eocd, 0) < 0 ||
        writer_u16le(&eocd, 0) < 0 || writer_u16le(&eocd, count16) < 0 ||
        writer_u16le(&eocd, count16) < 0 || writer_u32le(&eocd, cd_size32) < 0 ||
        writer_u32le(&eocd, cd_start32) < 0 || writer_u16le(&eocd, 0) < 0) {
        Py_DECREF(directory);
        Py_XDECREF(z64.bytes); Py_XDECREF(locator.bytes); Py_XDECREF(eocd.bytes);
        return NULL;
    }
    PyObject *z64_bytes = wreath_writer_finish(&z64);
    PyObject *locator_bytes = wreath_writer_finish(&locator);
    PyObject *eocd_bytes = wreath_writer_finish(&eocd);
    PyObject *result = z64_bytes != NULL && locator_bytes != NULL && eocd_bytes != NULL
        ? PyTuple_Pack(4, directory, z64_bytes, locator_bytes, eocd_bytes) : NULL;
    Py_DECREF(directory);
    Py_XDECREF(z64_bytes); Py_XDECREF(locator_bytes); Py_XDECREF(eocd_bytes);
    return result;
}

static PyMethodDef zip_methods[] = {
    {"begin", (PyCFunction)zip_begin, METH_O, "Begin one archive entry."},
    {"feed", (PyCFunction)zip_feed, METH_O, "Account for one content chunk."},
    {"end", (PyCFunction)zip_end, METH_NOARGS, "Finish the current entry."},
    {"finish", (PyCFunction)zip_finish, METH_NOARGS, "Emit the central directory."},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot zip_slots[] = {
    {Py_tp_new, PyType_GenericNew},
    {Py_tp_init, zip_init},
    {Py_tp_dealloc, zip_dealloc},
    {Py_tp_traverse, zip_traverse},
    {Py_tp_clear, zip_clear},
    {Py_tp_methods, zip_methods},
    {0, NULL}
};

static PyType_Spec zip_spec = {
    .name = "wreath._native._core.ZipBuilder",
    .basicsize = sizeof(ZipBuilder),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = zip_slots,
};

int
wreath_register_zip_builder(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&zip_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "ZipBuilder", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}

static PyObject *
aws_encode(PyObject *text, int slash)
{
    Py_ssize_t length;
    const unsigned char *data = (const unsigned char *)PyUnicode_AsUTF8AndSize(
        text, &length);
    if (data == NULL) return NULL;
    size_t encoded_length = 0;
    for (Py_ssize_t i = 0; i < length; i++) {
        size_t width = urlencode_safe(data[i]) || (slash && data[i] == '/')
            ? 1 : 3;
        if (width > (size_t)PY_SSIZE_T_MAX - encoded_length)
            return PyErr_NoMemory();
        encoded_length += width;
    }
    PyObject *result = PyUnicode_New((Py_ssize_t)encoded_length, 127);
    if (result == NULL) return NULL;
    char *output = (char *)PyUnicode_1BYTE_DATA(result);
    static const char hex[] = "0123456789ABCDEF";
    size_t written = 0;
    for (Py_ssize_t i = 0; i < length; i++) {
        unsigned char byte = data[i];
        if (urlencode_safe(byte) || (slash && byte == '/')) {
            output[written++] = (char)byte;
        } else {
            output[written++] = '%';
            output[written++] = hex[byte >> 4];
            output[written++] = hex[byte & 15];
        }
    }
    return result;
}

static PyObject *
canonical_ascii_header_key(PyObject *key)
{
    const Py_UCS1 *data = PyUnicode_1BYTE_DATA(key);
    Py_ssize_t left = 0;
    Py_ssize_t right = PyUnicode_GET_LENGTH(key);
    while (left < right && Py_UNICODE_ISSPACE(data[left])) left++;
    while (right > left && Py_UNICODE_ISSPACE(data[right - 1])) right--;
    PyObject *result = PyUnicode_New(right - left, 127);
    if (result == NULL) return NULL;
    Py_UCS1 *output = PyUnicode_1BYTE_DATA(result);
    for (Py_ssize_t i = left; i < right; i++) {
        Py_UCS1 character = data[i];
        output[i - left] = character >= 'A' && character <= 'Z'
            ? (Py_UCS1)(character + ('a' - 'A')) : character;
    }
    return result;
}

static PyObject *
canonical_header_key(PyObject *key)
{
    if (PyUnicode_CheckExact(key) && PyUnicode_IS_ASCII(key))
        return canonical_ascii_header_key(key);
    PyObject *lower = call_noargs_attr(key, "lower");
    if (lower == NULL) return NULL;
    PyObject *result = call_noargs_attr(lower, "strip");
    Py_DECREF(lower);
    return result;
}

static PyObject *
canonical_ascii_header_value(PyObject *text)
{
    const Py_UCS1 *data = PyUnicode_1BYTE_DATA(text);
    Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    Py_ssize_t output_length = 0;
    Py_ssize_t word_count = 0;
    Py_ssize_t index = 0;
    while (index < length) {
        while (index < length && Py_UNICODE_ISSPACE(data[index])) index++;
        Py_ssize_t start = index;
        while (index < length && !Py_UNICODE_ISSPACE(data[index])) index++;
        if (index != start) {
            output_length += index - start + (word_count != 0);
            word_count++;
        }
    }
    PyObject *result = PyUnicode_New(output_length, 127);
    if (result == NULL) return NULL;
    Py_UCS1 *output = PyUnicode_1BYTE_DATA(result);
    Py_ssize_t written = 0;
    word_count = 0;
    index = 0;
    while (index < length) {
        while (index < length && Py_UNICODE_ISSPACE(data[index])) index++;
        Py_ssize_t start = index;
        while (index < length && !Py_UNICODE_ISSPACE(data[index])) index++;
        if (index == start) continue;
        if (word_count != 0) output[written++] = ' ';
        memcpy(output + written, data + start, (size_t)(index - start));
        written += index - start;
        word_count++;
    }
    return result;
}

static PyObject *
canonical_header_value(PyObject *value, PyObject *space)
{
    PyObject *text = PyObject_Str(value);
    if (text == NULL) return NULL;
    if (PyUnicode_CheckExact(text) && PyUnicode_IS_ASCII(text)) {
        PyObject *result = canonical_ascii_header_value(text);
        Py_DECREF(text);
        return result;
    }
    PyObject *stripped = call_noargs_attr(text, "strip");
    Py_DECREF(text);
    if (stripped == NULL) return NULL;
    PyObject *words = call_noargs_attr(stripped, "split");
    Py_DECREF(stripped);
    if (words == NULL) return NULL;
    PyObject *result = PyUnicode_Join(space, words);
    Py_DECREF(words);
    return result;
}

static PyObject *
canonical_header_items(PyObject *headers)
{
    PyObject *source = PyMapping_Items(headers);
    if (source == NULL) return NULL;
    Py_ssize_t count = PyList_GET_SIZE(source);
    PyObject *items = PyList_New(count);
    PyObject *space = PyUnicode_FromString(" ");
    if (items == NULL || space == NULL) {
        Py_XDECREF(items); Py_XDECREF(space); Py_DECREF(source);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(source, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        PyObject *clean_key = canonical_header_key(key);
        PyObject *clean_value = clean_key == NULL ? NULL
            : canonical_header_value(value, space);
        PyObject *normalized = wreath_tuple2_from_owned(
            clean_key, clean_value);
        if (normalized == NULL) {
            Py_DECREF(space); Py_DECREF(items); Py_DECREF(source);
            return NULL;
        }
        PyList_SET_ITEM(items, i, normalized);
    }
    Py_DECREF(space);
    Py_DECREF(source);
    if (PyList_Sort(items) < 0) {
        Py_DECREF(items);
        return NULL;
    }
    return items;
}

static int
sigv4_add_length(Py_ssize_t *total, Py_ssize_t addition)
{
    if (addition > PY_SSIZE_T_MAX - *total) {
        PyErr_NoMemory();
        return -1;
    }
    *total += addition;
    return 0;
}

static int
sigv4_add_unicode_length(Py_ssize_t *total, Py_UCS4 *maxchar,
                         PyObject *value)
{
    if (!PyUnicode_Check(value)) {
        PyUnicode_AsUTF8AndSize(value, NULL);
        return -1;
    }
    if (sigv4_add_length(total, PyUnicode_GET_LENGTH(value)) < 0) return -1;
    Py_UCS4 value_maxchar = PyUnicode_MAX_CHAR_VALUE(value);
    if (value_maxchar > *maxchar) *maxchar = value_maxchar;
    return 0;
}

static int
sigv4_copy_unicode(PyObject *output, Py_ssize_t *written, PyObject *source)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(source);
    if (PyUnicode_CopyCharacters(output, *written, source, 0, length) < 0)
        return -1;
    *written += length;
    return 0;
}

static int
sigv4_write_character(PyObject *output, Py_ssize_t *written, Py_UCS4 character)
{
    if (PyUnicode_WriteChar(output, *written, character) < 0) return -1;
    (*written)++;
    return 0;
}

static PyObject *
render_headers(PyObject *items)
{
    Py_ssize_t count = PyList_GET_SIZE(items);
    Py_ssize_t canonical_length = 0;
    Py_ssize_t signed_length = count > 0 ? count - 1 : 0;
    Py_UCS4 canonical_maxchar = 127;
    Py_UCS4 signed_maxchar = 127;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        if (sigv4_add_unicode_length(
                &canonical_length, &canonical_maxchar, key) < 0 ||
            sigv4_add_unicode_length(
                &canonical_length, &canonical_maxchar, value) < 0 ||
            sigv4_add_length(&canonical_length, 2) < 0 ||
            sigv4_add_unicode_length(
                &signed_length, &signed_maxchar, key) < 0)
            return NULL;
    }
    PyObject *canonical = PyUnicode_New(canonical_length, canonical_maxchar);
    PyObject *signed_names = PyUnicode_New(signed_length, signed_maxchar);
    if (canonical == NULL || signed_names == NULL) {
        Py_XDECREF(canonical);
        Py_XDECREF(signed_names);
        return NULL;
    }
    Py_ssize_t canonical_written = 0;
    Py_ssize_t signed_written = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        PyObject *key = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        if (sigv4_copy_unicode(canonical, &canonical_written, key) < 0 ||
            sigv4_write_character(canonical, &canonical_written, ':') < 0 ||
            sigv4_copy_unicode(canonical, &canonical_written, value) < 0 ||
            sigv4_write_character(canonical, &canonical_written, '\n') < 0 ||
            (i != 0 && sigv4_write_character(
                signed_names, &signed_written, ';') < 0) ||
            sigv4_copy_unicode(signed_names, &signed_written, key) < 0) {
            Py_DECREF(canonical);
            Py_DECREF(signed_names);
            return NULL;
        }
    }
    if (canonical_written != canonical_length ||
        signed_written != signed_length) {
        Py_DECREF(canonical);
        Py_DECREF(signed_names);
        PyErr_SetString(PyExc_RuntimeError,
                        "SigV4 header size changed while writing");
        return NULL;
    }
    return wreath_tuple2_from_owned(canonical, signed_names);
}

PyObject *
wreath_sigv4_headers(PyObject *Py_UNUSED(self), PyObject *headers)
{
    PyObject *items = canonical_header_items(headers);
    if (items == NULL) return NULL;
    PyObject *result = render_headers(items);
    Py_DECREF(items);
    return result;
}

PyObject *
wreath_sigv4_canonical(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *method, *path, *params, *headers, *payload_hash;
    if (!PyArg_ParseTuple(args, "OOOOO:sigv4_canonical", &method, &path, &params,
                          &headers, &payload_hash)) return NULL;
    PyObject *header_items = canonical_header_items(headers);
    PyObject *rendered = header_items == NULL ? NULL : render_headers(header_items);
    Py_XDECREF(header_items);
    if (rendered == NULL) return NULL;
    PyObject *canonical_headers = PyTuple_GET_ITEM(rendered, 0);
    PyObject *signed_headers = PyTuple_GET_ITEM(rendered, 1);
    PyObject *param_source = PySequence_List(params);
    if (param_source == NULL) {
        Py_DECREF(rendered);
        return NULL;
    }
    Py_ssize_t param_count = PyList_GET_SIZE(param_source);
    PyObject *encoded_params = PyList_New(param_count);
    if (encoded_params == NULL) {
        Py_DECREF(param_source); Py_DECREF(rendered);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < param_count; i++) {
        PyObject *pair = PyList_GET_ITEM(param_source, i);
        PyObject *key = PySequence_GetItem(pair, 0);
        PyObject *value = key == NULL ? NULL : PySequence_GetItem(pair, 1);
        PyObject *encoded_key = key == NULL ? NULL : aws_encode(key, 0);
        PyObject *encoded_value = value == NULL ? NULL : aws_encode(value, 0);
        Py_XDECREF(key); Py_XDECREF(value);
        PyObject *encoded_pair = wreath_tuple2_from_owned(
            encoded_key, encoded_value);
        if (encoded_pair == NULL) {
            Py_DECREF(encoded_params); Py_DECREF(param_source); Py_DECREF(rendered);
            return NULL;
        }
        PyList_SET_ITEM(encoded_params, i, encoded_pair);
    }
    Py_DECREF(param_source);
    if (PyList_Sort(encoded_params) < 0) {
        Py_DECREF(encoded_params); Py_DECREF(rendered);
        return NULL;
    }
    PyObject *upper = PyObject_CallMethod(method, "upper", NULL);
    PyObject *encoded_path = upper == NULL ? NULL : aws_encode(path, 1);
    if (encoded_path != NULL && PyUnicode_GET_LENGTH(encoded_path) == 0) {
        Py_SETREF(encoded_path, PyUnicode_FromString("/"));
    }
    if (encoded_path == NULL) goto canonical_error;
    Py_ssize_t canonical_length = 0;
    Py_UCS4 canonical_maxchar = 127;
    if (sigv4_add_unicode_length(
            &canonical_length, &canonical_maxchar, upper) < 0 ||
        sigv4_add_unicode_length(
            &canonical_length, &canonical_maxchar, encoded_path) < 0 ||
        sigv4_add_unicode_length(
            &canonical_length, &canonical_maxchar, canonical_headers) < 0 ||
        sigv4_add_unicode_length(
            &canonical_length, &canonical_maxchar, signed_headers) < 0 ||
        sigv4_add_unicode_length(
            &canonical_length, &canonical_maxchar, payload_hash) < 0 ||
        sigv4_add_length(&canonical_length, 5) < 0)
        goto canonical_error;
    for (Py_ssize_t i = 0; i < param_count; i++) {
        PyObject *pair = PyList_GET_ITEM(encoded_params, i);
        if (sigv4_add_unicode_length(
                &canonical_length, &canonical_maxchar,
                PyTuple_GET_ITEM(pair, 0)) < 0 ||
            sigv4_add_unicode_length(
                &canonical_length, &canonical_maxchar,
                PyTuple_GET_ITEM(pair, 1)) < 0 ||
            sigv4_add_length(&canonical_length, i != 0 ? 2 : 1) < 0)
            goto canonical_error;
    }
    PyObject *canonical = PyUnicode_New(canonical_length, canonical_maxchar);
    if (canonical == NULL) goto canonical_error;
    Py_ssize_t written = 0;
    if (sigv4_copy_unicode(canonical, &written, upper) < 0 ||
        sigv4_write_character(canonical, &written, '\n') < 0 ||
        sigv4_copy_unicode(canonical, &written, encoded_path) < 0 ||
        sigv4_write_character(canonical, &written, '\n') < 0)
        goto canonical_write_error;
    for (Py_ssize_t i = 0; i < param_count; i++) {
        PyObject *pair = PyList_GET_ITEM(encoded_params, i);
        if ((i != 0 && sigv4_write_character(
                canonical, &written, '&') < 0) ||
            sigv4_copy_unicode(
                canonical, &written, PyTuple_GET_ITEM(pair, 0)) < 0 ||
            sigv4_write_character(canonical, &written, '=') < 0 ||
            sigv4_copy_unicode(
                canonical, &written, PyTuple_GET_ITEM(pair, 1)) < 0)
            goto canonical_write_error;
    }
    if (sigv4_write_character(canonical, &written, '\n') < 0 ||
        sigv4_copy_unicode(canonical, &written, canonical_headers) < 0 ||
        sigv4_write_character(canonical, &written, '\n') < 0 ||
        sigv4_copy_unicode(canonical, &written, signed_headers) < 0 ||
        sigv4_write_character(canonical, &written, '\n') < 0 ||
        sigv4_copy_unicode(canonical, &written, payload_hash) < 0)
        goto canonical_write_error;
    if (written != canonical_length) {
        PyErr_SetString(PyExc_RuntimeError,
                        "SigV4 canonical request size changed while writing");
        goto canonical_write_error;
    }
    PyObject *result = wreath_tuple2_from_owned(
        canonical, Py_NewRef(signed_headers));
    Py_DECREF(upper); Py_DECREF(encoded_path); Py_DECREF(encoded_params); Py_DECREF(rendered);
    return result;
canonical_write_error:
    Py_DECREF(canonical);
canonical_error:
    Py_XDECREF(upper); Py_XDECREF(encoded_path); Py_DECREF(encoded_params); Py_DECREF(rendered);
    return NULL;
}
