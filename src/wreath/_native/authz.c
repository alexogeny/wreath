/* Authorization result normalization for the optional native core. */
#include "wreathcore.h"

static int
add_capabilities(PyObject **mask, PyObject *capabilities, PyObject *values,
                 const char *prefix)
{
    PyObject *iterator = PyObject_GetIter(values);
    if (iterator == NULL) {
        return -1;
    }
    PyObject *value;
    while ((value = PyIter_Next(iterator)) != NULL) {
        if (!PyUnicode_Check(value)) {
            Py_DECREF(value);
            Py_DECREF(iterator);
            PyErr_SetString(PyExc_TypeError, "identity capabilities must be strings");
            return -1;
        }
        PyObject *key = PyUnicode_FromFormat("%s:%U", prefix, value);
        Py_DECREF(value);
        if (key == NULL) {
            Py_DECREF(iterator);
            return -1;
        }
        PyObject *capability = PyDict_GetItemWithError(capabilities, key);
        Py_DECREF(key);
        if (capability != NULL) {
            PyObject *combined = PyNumber_Or(*mask, capability);
            if (combined == NULL) {
                Py_DECREF(iterator);
                return -1;
            }
            Py_SETREF(*mask, combined);
        } else if (PyErr_Occurred()) {
            Py_DECREF(iterator);
            return -1;
        }
    }
    Py_DECREF(iterator);
    return PyErr_Occurred() ? -1 : 0;
}

PyObject *
wreath_build_capability_mask(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capabilities, *roles, *permissions;
    if (!PyArg_ParseTuple(args, "O!OO:build_capability_mask", &PyDict_Type,
                          &capabilities, &roles, &permissions)) {
        return NULL;
    }
    PyObject *authenticated = PyDict_GetItemString(capabilities, "authenticated");
    if (authenticated == NULL) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_KeyError, "authenticated");
        }
        return NULL;
    }
    PyObject *mask = Py_NewRef(authenticated);
    if (add_capabilities(&mask, capabilities, roles, "role") < 0 ||
        add_capabilities(&mask, capabilities, permissions, "permission") < 0) {
        Py_DECREF(mask);
        return NULL;
    }
    return mask;
}

PyObject *
wreath_normalize_authorization_decision(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *result, *decision_type;
    if (!PyArg_ParseTuple(args, "OO:normalize_authorization_decision",
                          &result, &decision_type)) {
        return NULL;
    }

    int is_instance = PyObject_IsInstance(result, decision_type);
    if (is_instance < 0) {
        return NULL;
    }
    if (is_instance) {
        return Py_NewRef(result);
    }

    PyObject *reason = PyUnicode_InternFromString("cedar");
    if (reason == NULL) {
        return NULL;
    }
    if (PyBool_Check(result)) {
        PyObject *normalized = PyObject_CallFunctionObjArgs(
            decision_type, result, reason, NULL);
        Py_DECREF(reason);
        return normalized;
    }

    PyObject *allowed_attr = PyObject_GetAttrString(result, "allowed");
    if (allowed_attr == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) {
            Py_DECREF(reason);
            return NULL;
        }
        PyErr_Clear();
        allowed_attr = Py_NewRef(Py_False);
    }
    int allowed = PyObject_IsTrue(allowed_attr);
    Py_DECREF(allowed_attr);
    if (allowed < 0) {
        Py_DECREF(reason);
        return NULL;
    }
    PyObject *allowed_obj = PyBool_FromLong(allowed);

    PyObject *raw_diagnostics = PyObject_GetAttrString(result, "diagnostics");
    if (raw_diagnostics == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) {
            Py_DECREF(allowed_obj);
            Py_DECREF(reason);
            return NULL;
        }
        PyErr_Clear();
        raw_diagnostics = PyTuple_New(0);
    }
    PyObject *items = raw_diagnostics ? PySequence_Fast(
        raw_diagnostics, "authorization diagnostics must be iterable") : NULL;
    Py_XDECREF(raw_diagnostics);
    if (items == NULL) {
        Py_DECREF(allowed_obj);
        Py_DECREF(reason);
        return NULL;
    }

    Py_ssize_t count = PySequence_Fast_GET_SIZE(items);
    PyObject *diagnostics = PyTuple_New(count);
    if (diagnostics == NULL) {
        Py_DECREF(items);
        Py_DECREF(allowed_obj);
        Py_DECREF(reason);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *text = PyObject_Str(PySequence_Fast_GET_ITEM(items, i));
        if (text == NULL) {
            Py_DECREF(diagnostics);
            Py_DECREF(items);
            Py_DECREF(allowed_obj);
            Py_DECREF(reason);
            return NULL;
        }
        PyTuple_SET_ITEM(diagnostics, i, text);
    }
    Py_DECREF(items);

    PyObject *normalized = PyObject_CallFunctionObjArgs(
        decision_type, allowed_obj, reason, diagnostics, NULL);
    Py_DECREF(diagnostics);
    Py_DECREF(allowed_obj);
    Py_DECREF(reason);
    return normalized;
}
