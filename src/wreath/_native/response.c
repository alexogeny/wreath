/* Exact HTMLResponse construction for wreath._native._core.
 *
 * The fixed 200/bytes shape is the end of every rendered HTML request: four
 * slot stores and no header object, because Wreath's server derives the two
 * immutable headers at emission.  Keeping those stores in a Python __init__
 * frame cost almost as much as the response object itself.  The Python method
 * remains the oracle and owns every other shape (subclasses, non-200 status,
 * unusual body objects). */
#include "wreathcore.h"

#include <stddef.h>

static PyObject *html_response_type = NULL;
static PyObject *html_response_original_init = NULL;
static PyObject *status_200 = NULL;
static Py_ssize_t response_body_offset;
static Py_ssize_t response_status_offset;
static Py_ssize_t response_background_offset;
static Py_ssize_t response_headers_offset;

#define RESPONSE_SLOT(obj, offset) (*(PyObject **)((char *)(obj) + (offset)))

static int
response_resolve_offset(PyObject *type, const char *name, Py_ssize_t *offset)
{
    PyObject *dict = ((PyTypeObject *)type)->tp_dict;
    PyObject *descriptor = dict == NULL
        ? NULL : PyDict_GetItemString(dict, name);
    if (descriptor == NULL ||
        !PyObject_TypeCheck(descriptor, &PyMemberDescr_Type)) {
        PyErr_Format(
            PyExc_RuntimeError,
            "wreath._native._core: Response has no __slots__ member %s; "
            "the response accelerator must be rebuilt",
            name);
        return -1;
    }
    *offset = ((PyMemberDescrObject *)descriptor)->d_member->offset;
    return 0;
}

static void
response_slot_set(PyObject *self, Py_ssize_t offset, PyObject *value)
{
    PyObject *old = RESPONSE_SLOT(self, offset);
    RESPONSE_SLOT(self, offset) = Py_NewRef(value);
    Py_XDECREF(old);
}

static int
html_response_python_init(PyObject *self, PyObject *args, PyObject *kwargs)
{
    Py_ssize_t count = PyTuple_GET_SIZE(args);
    PyObject *full_args = PyTuple_New(count + 1);
    PyObject *result;
    if (full_args == NULL) return -1;
    PyTuple_SET_ITEM(full_args, 0, Py_NewRef(self));
    for (Py_ssize_t index = 0; index < count; index++) {
        PyTuple_SET_ITEM(
            full_args, index + 1, Py_NewRef(PyTuple_GET_ITEM(args, index)));
    }
    result = PyObject_Call(html_response_original_init, full_args, kwargs);
    Py_DECREF(full_args);
    if (result == NULL) return -1;
    Py_DECREF(result);
    return 0;
}

static int
html_response_init(PyObject *self, PyObject *args, PyObject *kwargs)
{
    PyObject *body;
    PyObject *status = NULL;
    PyObject *background = Py_None;
    PyObject *document;
    int fast_status;
    static char *keywords[] = {"body", "status", "background", NULL};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "O|O$O:HTMLResponse", keywords,
            &body, &status, &background)) return -1;
    fast_status = status == NULL;
    if (status != NULL && PyLong_CheckExact(status)) {
        long code = PyLong_AsLong(status);
        if (code == -1 && PyErr_Occurred()) PyErr_Clear();
        else fast_status = code == 200;
    }
    if ((PyObject *)Py_TYPE(self) != html_response_type || !fast_status ||
        (!PyBytes_Check(body) && !PyUnicode_CheckExact(body))) {
        return html_response_python_init(self, args, kwargs);
    }
    if (PyBytes_Check(body)) {
        document = Py_NewRef(body);
    } else {
        document = PyUnicode_AsUTF8String(body);
        if (document == NULL) return -1;
    }
    response_slot_set(self, response_body_offset, document);
    response_slot_set(self, response_status_offset, status_200);
    response_slot_set(self, response_background_offset, background);
    response_slot_set(self, response_headers_offset, Py_None);
    Py_DECREF(document);
    return 0;
}

PyObject *
wreath_html_response_configure(PyObject *self, PyObject *args)
{
    PyObject *html_type;
    PyObject *response_type;
    PyObject *original;
    (void)self;
    if (!PyArg_ParseTuple(
            args, "O!O!:html_response_configure",
            &PyType_Type, &html_type, &PyType_Type, &response_type)) return NULL;
    original = PyDict_GetItemString(((PyTypeObject *)html_type)->tp_dict, "__init__");
    if (original == NULL ||
        response_resolve_offset(response_type, "body", &response_body_offset) < 0 ||
        response_resolve_offset(response_type, "status", &response_status_offset) < 0 ||
        response_resolve_offset(
            response_type, "background", &response_background_offset) < 0 ||
        response_resolve_offset(response_type, "_headers", &response_headers_offset) < 0) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_RuntimeError, "HTMLResponse.__init__ is missing");
        }
        return NULL;
    }
    if (status_200 == NULL) {
        status_200 = PyLong_FromLong(200);
        if (status_200 == NULL) return NULL;
    }
    Py_XSETREF(html_response_type, Py_NewRef(html_type));
    Py_XSETREF(html_response_original_init, Py_NewRef(original));
    ((PyTypeObject *)html_type)->tp_init = html_response_init;
    PyType_Modified((PyTypeObject *)html_type);
    Py_RETURN_NONE;
}
