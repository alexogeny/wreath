#ifndef WREATH_ACTIVATE_H
#define WREATH_ACTIVATE_H

#include <Python.h>

PyObject *wreath_activate_path(PyObject *, PyObject *);
PyObject *wreath_activate_path_call(PyObject *, PyObject *const *, Py_ssize_t);

#endif
