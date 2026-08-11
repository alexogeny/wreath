/* Fixed-cell projection for WFR1 event chunks.
 *
 * Container framing stays in `_recording_format.py`: one EVNT chunk means one
 * CRC call and the rest of that reader costs about 0.35M instructions.  The
 * expensive part was crossing the interpreter 4,096 times to slice one bytes
 * object per cell (9.66M instructions in the measured 256 KiB recording).
 * This operation owns only its result tuple and makes the same bytes objects in
 * one native loop.  No view of the input escapes the call.
 */
#include "wreathcore.h"


PyObject *
wreath_recording_event_cells(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *payload;
    PyObject *error_type;
    Py_ssize_t cell_size;
    int version;
    Py_ssize_t length;
    Py_ssize_t count;
    const char *source;
    PyObject *result;

    if (!PyArg_ParseTuple(args, "OniO:recording_event_cells",
                          &payload, &cell_size, &version, &error_type)) {
        return NULL;
    }
    if (!PyBytes_Check(payload)) {
        PyErr_Format(PyExc_TypeError,
                     "recording event payload must be bytes, not %.200s",
                     Py_TYPE(payload)->tp_name);
        return NULL;
    }
    if (cell_size <= 0) {
        PyErr_Format(PyExc_ValueError,
                     "recording event cell size must be positive, not %zd",
                     cell_size);
        return NULL;
    }
    if (version < 0 || version > UINT8_MAX) {
        PyErr_Format(PyExc_ValueError,
                     "recording event schema version must fit in one byte, not %d",
                     version);
        return NULL;
    }
    if (!PyExceptionClass_Check(error_type)) {
        PyErr_SetString(PyExc_TypeError,
                        "recording event error_type must be an exception class");
        return NULL;
    }

    length = PyBytes_GET_SIZE(payload);
    if (length % cell_size != 0) {
        Py_RETURN_NONE;
    }
    count = length / cell_size;
    result = PyTuple_New(count);
    if (result == NULL) {
        return NULL;
    }
    source = PyBytes_AS_STRING(payload);
    for (Py_ssize_t index = 0; index < count; index++) {
        const char *cell = source + index * cell_size;
        PyObject *item;
        if ((uint8_t)cell[0] != (uint8_t)version) {
            Py_DECREF(result);
            PyErr_SetString(error_type,
                            "event cell has an unsupported schema version");
            return NULL;
        }
        item = PyBytes_FromStringAndSize(cell, cell_size);
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, item);
    }
    return result;
}
