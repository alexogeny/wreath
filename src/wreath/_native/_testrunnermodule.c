/* wreath._native._testrunner: the minimal native test dispatch loop.
 *
 * Collection, compatibility metadata and rendering belong to Python. This
 * module owns the repeated operation: vectorcall a compiled list of cases,
 * classify their exceptions and measure each body. It has no mutable globals;
 * every reference and counter below belongs to one run() invocation.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <time.h>
#endif

static int
wreath_monotonic_ns(unsigned long long *result)
{
#ifdef _WIN32
    LARGE_INTEGER counter;
    LARGE_INTEGER frequency;
    unsigned long long ticks;
    unsigned long long ticks_per_second;
    if (!QueryPerformanceCounter(&counter) ||
        !QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) {
        PyErr_SetFromWindowsErr(0);
        return -1;
    }
    ticks = (unsigned long long)counter.QuadPart;
    ticks_per_second = (unsigned long long)frequency.QuadPart;
    *result = (ticks / ticks_per_second) * 1000000000ULL +
              ((ticks % ticks_per_second) * 1000000000ULL) / ticks_per_second;
#else
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    *result = (unsigned long long)value.tv_sec * 1000000000ULL +
              (unsigned long long)value.tv_nsec;
#endif
    return 0;
}

static int
append_result(
    PyObject *results,
    PyObject *node_id,
    PyObject *outcome,
    unsigned long long duration_ns,
    PyObject *exception)
{
    PyObject *duration_object = PyLong_FromUnsignedLongLong(duration_ns);
    PyObject *record;

    if (duration_object == NULL) {
        return -1;
    }
    record = PyTuple_New(4);
    if (record == NULL) {
        Py_DECREF(duration_object);
        return -1;
    }
    Py_INCREF(node_id);
    PyTuple_SET_ITEM(record, 0, node_id);
    Py_INCREF(outcome);
    PyTuple_SET_ITEM(record, 1, outcome);
    PyTuple_SET_ITEM(record, 2, duration_object);
    Py_INCREF(exception);
    PyTuple_SET_ITEM(record, 3, exception);
    if (PyList_Append(results, record) < 0) {
        Py_DECREF(record);
        return -1;
    }
    Py_DECREF(record);
    return 0;
}

static PyObject *
testrunner_run(PyObject *Py_UNUSED(module), PyObject *args)
{
    PyObject *cases;
    PyObject *skip_type;
    Py_ssize_t max_failures;
    PyObject *fast_cases = NULL;
    PyObject *results = NULL;
    PyObject *passed_outcome = NULL;
    PyObject *skipped_outcome = NULL;
    PyObject *failed_outcome = NULL;
    PyObject *interrupted_outcome = NULL;
    Py_ssize_t failures = 0;
    Py_ssize_t index;
    Py_ssize_t case_count;
    int is_skip_subclass;
    PyObject *observer = Py_None;

    if (!PyArg_ParseTuple(args, "OOn|O:run", &cases, &skip_type, &max_failures, &observer)) {
        return NULL;
    }
    if (max_failures < 0) {
        PyErr_SetString(PyExc_ValueError, "max_failures must be non-negative");
        return NULL;
    }
    if (!PyType_Check(skip_type)) {
        PyErr_SetString(PyExc_TypeError, "skip_type must be an exception class");
        return NULL;
    }
    is_skip_subclass = PyObject_IsSubclass(skip_type, (PyObject *)PyExc_BaseException);
    if (is_skip_subclass < 0) {
        return NULL;
    }
    if (!is_skip_subclass) {
        PyErr_SetString(PyExc_TypeError, "skip_type must be an exception class");
        return NULL;
    }
    if (observer != Py_None && !PyCallable_Check(observer)) {
        PyErr_SetString(PyExc_TypeError, "observer must be callable or None");
        return NULL;
    }
    fast_cases = PySequence_Fast(cases, "cases must be a sequence");
    if (fast_cases == NULL) {
        return NULL;
    }
    case_count = PySequence_Fast_GET_SIZE(fast_cases);
    results = PyList_New(0);
    passed_outcome = PyUnicode_FromString("passed");
    skipped_outcome = PyUnicode_FromString("skipped");
    failed_outcome = PyUnicode_FromString("failed");
    interrupted_outcome = PyUnicode_FromString("interrupted");
    if (results == NULL || passed_outcome == NULL || skipped_outcome == NULL ||
        failed_outcome == NULL || interrupted_outcome == NULL) {
        Py_DECREF(fast_cases);
        Py_XDECREF(results);
        Py_XDECREF(passed_outcome);
        Py_XDECREF(skipped_outcome);
        Py_XDECREF(failed_outcome);
        Py_XDECREF(interrupted_outcome);
        return NULL;
    }

    for (index = 0; index < case_count; index++) {
        PyObject *case_object = PySequence_Fast_GET_ITEM(fast_cases, index);
        PyObject *fast_case = NULL;
        PyObject *case_sequence = case_object;
        PyObject *node_id;
        PyObject *callable;
        PyObject *call_args;
        PyObject *pre_exception;
        PyObject *result;
        PyObject *exception;
        PyObject *fast_args = NULL;
        PyObject *argument_sequence;
        PyObject *outcome;
        unsigned long long started;
        unsigned long long finished;
        int is_skip;
        PyObject *observed;

        if (!PyTuple_CheckExact(case_object)) {
            fast_case = PySequence_Fast(
                case_object,
                "each case must be (node_id, callable, args, skip_exception)"
            );
            if (fast_case == NULL) {
                goto error;
            }
            case_sequence = fast_case;
        }
        if (PySequence_Fast_GET_SIZE(case_sequence) != 4) {
            Py_XDECREF(fast_case);
            PyErr_Format(
                PyExc_ValueError,
                "case %zd must contain node_id, callable, args, and skip_exception",
                index
            );
            goto error;
        }
        node_id = PySequence_Fast_GET_ITEM(case_sequence, 0);
        callable = PySequence_Fast_GET_ITEM(case_sequence, 1);
        call_args = PySequence_Fast_GET_ITEM(case_sequence, 2);
        pre_exception = PySequence_Fast_GET_ITEM(case_sequence, 3);
        if (!PyUnicode_Check(node_id)) {
            Py_XDECREF(fast_case);
            PyErr_Format(PyExc_TypeError, "case %zd node_id must be str", index);
            goto error;
        }
        if (!PyCallable_Check(callable)) {
            Py_XDECREF(fast_case);
            PyErr_Format(PyExc_TypeError, "case %zd callable is not callable", index);
            goto error;
        }
        argument_sequence = call_args;
        if (!PyTuple_CheckExact(call_args)) {
            fast_args = PySequence_Fast(call_args, "case args must be a sequence");
            if (fast_args == NULL) {
                Py_XDECREF(fast_case);
                goto error;
            }
            argument_sequence = fast_args;
        }

        if (wreath_monotonic_ns(&started) < 0) {
            Py_XDECREF(fast_args);
            Py_XDECREF(fast_case);
            goto error;
        }
        if (observer != Py_None) {
            observed = PyObject_CallFunctionObjArgs(observer, node_id, Py_None, NULL);
            if (observed == NULL) {
                Py_XDECREF(fast_args);
                Py_XDECREF(fast_case);
                goto error;
            }
            Py_DECREF(observed);
        }
        if (pre_exception != Py_None) {
            is_skip = PyObject_IsInstance(pre_exception, skip_type);
            if (is_skip < 0) {
                Py_XDECREF(fast_args);
                Py_XDECREF(fast_case);
                goto error;
            }
            if (!is_skip) {
                Py_XDECREF(fast_args);
                Py_XDECREF(fast_case);
                PyErr_Format(
                    PyExc_TypeError,
                    "case %zd skip_exception must be an instance of skip_type or None",
                    index
                );
                goto error;
            }
            Py_INCREF(pre_exception);
            exception = pre_exception;
            outcome = skipped_outcome;
        }
        else {
            result = PyObject_Vectorcall(
                callable,
                PySequence_Fast_ITEMS(argument_sequence),
                (size_t)PySequence_Fast_GET_SIZE(argument_sequence),
                NULL
            );
            if (result != NULL) {
                Py_DECREF(result);
                Py_INCREF(Py_None);
                exception = Py_None;
                outcome = passed_outcome;
            }
            else {
                int interrupted = PyErr_ExceptionMatches(PyExc_KeyboardInterrupt);
                exception = PyErr_GetRaisedException();
                if (exception == NULL) {
                    Py_XDECREF(fast_args);
                    Py_XDECREF(fast_case);
                    goto error;
                }
                is_skip = PyObject_IsInstance(exception, skip_type);
                if (is_skip < 0) {
                    Py_DECREF(exception);
                    Py_XDECREF(fast_args);
                    Py_XDECREF(fast_case);
                    goto error;
                }
                if (interrupted) {
                    outcome = interrupted_outcome;
                }
                else if (is_skip) {
                    outcome = skipped_outcome;
                }
                else {
                    outcome = failed_outcome;
                    failures++;
                }
            }
        }
        if (wreath_monotonic_ns(&finished) < 0) {
            Py_DECREF(exception);
            Py_XDECREF(fast_args);
            Py_XDECREF(fast_case);
            goto error;
        }
        if (observer != Py_None) {
            observed = PyObject_CallFunctionObjArgs(
                observer, node_id, outcome, NULL
            );
            if (observed == NULL) {
                Py_DECREF(exception);
                Py_XDECREF(fast_args);
                Py_XDECREF(fast_case);
                goto error;
            }
            Py_DECREF(observed);
        }
        if (append_result(
                results,
                node_id,
                outcome,
                finished >= started ? finished - started : 0,
                exception
            ) < 0) {
            Py_DECREF(exception);
            Py_XDECREF(fast_args);
            Py_XDECREF(fast_case);
            goto error;
        }
        Py_DECREF(exception);
        Py_XDECREF(fast_args);
        Py_XDECREF(fast_case);
        if (outcome == interrupted_outcome ||
            (max_failures > 0 && failures >= max_failures)) {
            break;
        }
    }

    Py_DECREF(fast_cases);
    Py_DECREF(passed_outcome);
    Py_DECREF(skipped_outcome);
    Py_DECREF(failed_outcome);
    Py_DECREF(interrupted_outcome);
    return results;

error:
    Py_DECREF(fast_cases);
    Py_DECREF(results);
    Py_DECREF(passed_outcome);
    Py_DECREF(skipped_outcome);
    Py_DECREF(failed_outcome);
    Py_DECREF(interrupted_outcome);
    return NULL;
}

static PyMethodDef testrunner_methods[] = {
    {
        "run",
        testrunner_run,
        METH_VARARGS,
        "run(cases, skip_type, max_failures, observer=None) -> list[tuple]"
    },
    {NULL, NULL, 0, NULL},
};

static PyModuleDef testrunner_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._testrunner",
    .m_doc = "Native vectorcall test dispatch for Wreath.",
    .m_size = 0,
    .m_methods = testrunner_methods,
};

PyMODINIT_FUNC
PyInit__testrunner(void)
{
    return PyModule_Create(&testrunner_module);
}
