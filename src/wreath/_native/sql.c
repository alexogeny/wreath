/* PostgreSQL placeholder rewriting for composed SQL statements. */
#include "wreathcore.h"


typedef struct {
    Py_UCS4 *data;
    Py_ssize_t length;
    Py_ssize_t capacity;
} UnicodeBuffer;


static int
unicode_reserve(UnicodeBuffer *buffer, Py_ssize_t extra)
{
    Py_ssize_t needed;
    Py_ssize_t capacity;
    Py_UCS4 *grown;

    if (extra > PY_SSIZE_T_MAX - buffer->length) {
        PyErr_NoMemory();
        return -1;
    }
    needed = buffer->length + extra;
    if (needed <= buffer->capacity) return 0;
    capacity = buffer->capacity > 0 ? buffer->capacity : 64;
    while (capacity < needed) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            capacity = needed;
            break;
        }
        capacity *= 2;
    }
    grown = PyMem_Realloc(buffer->data, (size_t)capacity * sizeof(*grown));
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    buffer->data = grown;
    buffer->capacity = capacity;
    return 0;
}


static int
unicode_append_object(UnicodeBuffer *buffer, PyObject *text)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    if (unicode_reserve(buffer, length) < 0) return -1;
    for (Py_ssize_t i = 0; i < length; i++) {
        buffer->data[buffer->length++] = PyUnicode_READ_CHAR(text, i);
    }
    return 0;
}


static int
unicode_append_ssize(UnicodeBuffer *buffer, Py_ssize_t value)
{
    char reversed[sizeof(Py_ssize_t) * 3 + 1];
    size_t magnitude;
    Py_ssize_t count = 0;

    if (value < 0) {
        magnitude = (size_t)(-(value + 1)) + 1;
    }
    else {
        magnitude = (size_t)value;
    }
    do {
        reversed[count++] = (char)('0' + magnitude % 10);
        magnitude /= 10;
    } while (magnitude != 0);
    if (unicode_reserve(buffer, count + (value < 0)) < 0) return -1;
    if (value < 0) buffer->data[buffer->length++] = '-';
    while (count > 0) {
        buffer->data[buffer->length++] = (Py_UCS4)reversed[--count];
    }
    return 0;
}


static int
unicode_append_renumbered_slow(UnicodeBuffer *buffer, PyObject *text,
                               Py_ssize_t start, Py_ssize_t end,
                               Py_ssize_t offset)
{
    PyObject *digits = PyUnicode_Substring(text, start, end);
    PyObject *number = NULL;
    PyObject *shift = NULL;
    PyObject *renumbered = NULL;
    int result = -1;

    if (digits == NULL) return -1;
    number = PyLong_FromUnicodeObject(digits, 10);
    Py_DECREF(digits);
    if (number == NULL) return -1;
    shift = PyLong_FromSsize_t(offset);
    if (shift == NULL) goto done;
    renumbered = PyNumber_Add(number, shift);
    if (renumbered == NULL) goto done;
    digits = PyObject_Str(renumbered);
    if (digits == NULL) goto done;
    result = unicode_append_object(buffer, digits);
    Py_DECREF(digits);

done:
    Py_XDECREF(renumbered);
    Py_XDECREF(shift);
    Py_DECREF(number);
    return result;
}


static int
unicode_append_renumbered(UnicodeBuffer *buffer, PyObject *text,
                          Py_ssize_t start, Py_ssize_t end,
                          Py_ssize_t offset)
{
    Py_ssize_t number = 0;

    for (Py_ssize_t index = start; index < end; index++) {
        Py_UCS4 ch = PyUnicode_READ_CHAR(text, index);
        if (ch < '0' || ch > '9') {
            return unicode_append_renumbered_slow(
                buffer, text, start, end, offset);
        }
        Py_ssize_t digit = (Py_ssize_t)(ch - '0');
        if (number > (PY_SSIZE_T_MAX - digit) / 10) {
            return unicode_append_renumbered_slow(
                buffer, text, start, end, offset);
        }
        number = number * 10 + digit;
    }
    if ((offset > 0 && number > PY_SSIZE_T_MAX - offset) ||
        (offset < 0 && number < PY_SSIZE_T_MIN - offset)) {
        return unicode_append_renumbered_slow(
            buffer, text, start, end, offset);
    }
    return unicode_append_ssize(buffer, number + offset);
}


PyObject *
wreath_sql_renumber(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *text;
    Py_ssize_t offset;
    Py_ssize_t length;
    UnicodeBuffer output = {NULL, 0, 0};

    if (!PyArg_ParseTuple(args, "Un:sql_renumber", &text, &offset)) return NULL;
    if (offset == 0) return Py_NewRef(text);
    length = PyUnicode_GET_LENGTH(text);
    if (unicode_reserve(&output, length) < 0) return NULL;

    for (Py_ssize_t index = 0; index < length;) {
        Py_UCS4 ch = PyUnicode_READ_CHAR(text, index);
        Py_ssize_t end;

        if (ch != '$' || index + 1 >= length ||
                !Py_UNICODE_ISDIGIT(PyUnicode_READ_CHAR(text, index + 1))) {
            output.data[output.length++] = ch;
            index++;
            continue;
        }
        end = index + 2;
        while (end < length && Py_UNICODE_ISDIGIT(PyUnicode_READ_CHAR(text, end))) {
            end++;
        }
        if (unicode_reserve(&output, 1) < 0) {
            goto error;
        }
        output.data[output.length++] = '$';
        if (unicode_append_renumbered(
                &output, text, index + 1, end, offset) < 0) goto error;
        index = end;
    }

    text = PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, output.data, output.length);
    PyMem_Free(output.data);
    return text;

error:
    PyMem_Free(output.data);
    return NULL;
}
