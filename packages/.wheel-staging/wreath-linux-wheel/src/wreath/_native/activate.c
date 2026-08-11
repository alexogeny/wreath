/* Execute a startup-compiled path-only scalar activation plan in one C call. */
#include "activate.h"

enum { ACTIVATE_STR = 0, ACTIVATE_INT = 1, ACTIVATE_FLOAT = 2, ACTIVATE_BOOL = 3 };

static int
append_error(PyObject *errors, PyObject *alias, PyObject *raw,
             const char *message, const char *kind)
{
    PyObject *source = PyUnicode_FromString("path");
    PyObject *loc = source != NULL ? PyList_New(2) : NULL;
    PyObject *msg = PyUnicode_FromFormat(message, raw);
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
    if (loc == NULL || msg == NULL || type == NULL) goto done;
    error = _PyDict_NewPresized(3);
    if (error == NULL) goto done;
    if (PyDict_SetItemString(error, "loc", loc) < 0 ||
        PyDict_SetItemString(error, "msg", msg) < 0 ||
        PyDict_SetItemString(error, "type", type) < 0 ||
        PyList_Append(errors, error) < 0) goto done;
    result = 0;
done:
    Py_XDECREF(error);
    Py_XDECREF(type);
    Py_XDECREF(msg);
    Py_XDECREF(loc);
    return result;
}

static int
append_activation_error(PyObject **errors, PyObject *alias, PyObject *raw,
                        const char *message, const char *kind)
{
    if (*errors == NULL) {
        *errors = PyList_New(0);
        if (*errors == NULL) return -1;
    }
    return append_error(*errors, alias, raw, message, kind);
}

static PyObject *
convert_bool(PyObject *raw)
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
activate_path_value(PyObject *params, PyObject *entry,
                    PyObject **errors, PyObject **value_out)
{
    PyObject *alias = PyTuple_GET_ITEM(entry, 1);
    int opcode = (int)PyLong_AsLong(PyTuple_GET_ITEM(entry, 2));
    if (opcode == -1 && PyErr_Occurred()) return -1;
    PyObject *raw = PyObject_GetItem(params, alias);
    if (raw == NULL) return -1;
    PyObject *value = NULL;
    if (opcode == ACTIVATE_STR) value = Py_NewRef(raw);
    else if (opcode == ACTIVATE_INT) {
        value = PyLong_FromUnicodeObject(raw, 10);
        if (value == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            if (append_activation_error(errors, alias, raw,
                                        "%R is not an integer", "int") < 0) {
                Py_DECREF(raw);
                return -1;
            }
            Py_DECREF(raw);
            *value_out = NULL;
            return 0;
        }
    }
    else if (opcode == ACTIVATE_FLOAT) {
        value = PyFloat_FromString(raw);
        if (value == NULL && PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            if (append_activation_error(errors, alias, raw,
                                        "%R is not a number", "float") < 0) {
                Py_DECREF(raw);
                return -1;
            }
            Py_DECREF(raw);
            *value_out = NULL;
            return 0;
        }
    }
    else if (opcode == ACTIVATE_BOOL) {
        value = convert_bool(raw);
        if (value == NULL && !PyErr_Occurred()) {
            if (append_activation_error(errors, alias, raw,
                                        "%R is not a boolean", "bool") < 0) {
                Py_DECREF(raw);
                return -1;
            }
            Py_DECREF(raw);
            *value_out = NULL;
            return 0;
        }
    }
    else {
        Py_DECREF(raw);
        PyErr_SetString(PyExc_RuntimeError, "invalid path activation opcode");
        return -1;
    }
    Py_DECREF(raw);
    if (value == NULL) return -1;
    *value_out = value;
    return 0;
}

static int
activate_path_into(PyObject *params, PyObject *plan,
                   PyObject *kwargs, PyObject **errors)
{
    Py_ssize_t count = PyTuple_GET_SIZE(plan);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *entry = PyTuple_GET_ITEM(plan, i);
        PyObject *value = NULL;
        if (activate_path_value(params, entry, errors, &value) < 0) return -1;
        if (value == NULL) continue;
        PyObject *name = PyTuple_GET_ITEM(entry, 0);
        int inserted = PyDict_SetItem(kwargs, name, value);
        Py_DECREF(value);
        if (inserted < 0) return -1;
    }
    return 0;
}

PyObject *
wreath_activate_path(PyObject *Py_UNUSED(module), PyObject *args)
{
    PyObject *params;
    PyObject *plan;
    if (!PyArg_ParseTuple(args, "OO!:activate_path", &params,
                          &PyTuple_Type, &plan)) return NULL;
    PyObject *kwargs = _PyDict_NewPresized(PyTuple_GET_SIZE(plan));
    PyObject *errors = PyList_New(0);
    if (kwargs == NULL || errors == NULL ||
        activate_path_into(params, plan, kwargs, &errors) < 0) {
        Py_XDECREF(kwargs);
        Py_XDECREF(errors);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(2, kwargs, errors);
    Py_DECREF(kwargs);
    Py_DECREF(errors);
    return result;
}

static PyObject *
raise_activation_errors(PyObject *error_type, PyObject *errors)
{
    PyObject *exception = PyObject_CallOneArg(error_type, errors);
    if (exception != NULL) {
        PyErr_SetObject(error_type, exception);
        Py_DECREF(exception);
    }
    return NULL;
}

PyObject *
wreath_activate_path_call(PyObject *Py_UNUSED(module), PyObject *const *args,
                          Py_ssize_t nargs)
{
    if (nargs != 4) {
        PyErr_Format(PyExc_TypeError,
                     "activate_path_call expected 4 arguments, got %zd", nargs);
        return NULL;
    }
    PyObject *handler = args[0];
    PyObject *request = args[1];
    PyObject *compiled = args[2];
    PyObject *error_type = args[3];
    PyObject *plan = PyTuple_GET_ITEM(compiled, 0);
    PyObject *keyword_names = PyTuple_GET_ITEM(compiled, 1);
    PyObject *params = PyObject_GetAttrString(request, "path_params");
    if (params == NULL) return NULL;
    Py_ssize_t count = PyTuple_GET_SIZE(plan);
    PyObject *local_stack[9] = {NULL};
    PyObject **call_stack = local_stack;
    if (count > 8) {
        call_stack = PyMem_Calloc((size_t)count + 1, sizeof(PyObject *));
        if (call_stack == NULL) {
            Py_DECREF(params);
            return PyErr_NoMemory();
        }
    }
    call_stack[0] = request;
    PyObject *errors = NULL;
    int failed = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        if (activate_path_value(params, PyTuple_GET_ITEM(plan, i),
                                &errors, &call_stack[i + 1]) < 0) {
            failed = 1;
            break;
        }
    }
    Py_DECREF(params);
    PyObject *result = NULL;
    if (failed) {
        Py_XDECREF(errors);
    }
    else if (errors != NULL) {
        result = raise_activation_errors(error_type, errors);
        Py_DECREF(errors);
    }
    else {
        result = PyObject_Vectorcall(handler, call_stack, 1, keyword_names);
    }
    for (Py_ssize_t i = 0; i < count; i++) Py_XDECREF(call_stack[i + 1]);
    if (call_stack != local_stack) PyMem_Free(call_stack);
    return result;
}
