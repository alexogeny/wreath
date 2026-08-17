/* Execute a startup-compiled path-only scalar activation plan in one C call. */
#include "activate.h"

/* The framework materializes its public Request at the handler-activation
 * boundary.  Its lazy fields intentionally remain unassigned; everything
 * below merely performs the exact slot initialization Request.__init__ would
 * otherwise execute as a Python frame.  Layout state belongs to the capsule
 * created by request_layout(), not to this extension process-wide. */
typedef struct {
    PyTypeObject *type;
    PyObject *client_source;
    Py_ssize_t scope;
    Py_ssize_t context;
    Py_ssize_t receive;
    Py_ssize_t client_source_slot;
    Py_ssize_t app;
    Py_ssize_t header_map;
    Py_ssize_t header_scanned;
    Py_ssize_t path_params;
    Py_ssize_t policy_mask;
    Py_ssize_t identity;
    Py_ssize_t route_outcome;
    Py_ssize_t state;
    Py_ssize_t limits;
} RequestLayout;

#define REQUEST_LAYOUT_CAPSULE "wreath._core.RequestLayout.v1"
#define REQUEST_SLOT(object, offset) \
    (*(PyObject **)((char *)(object) + (offset)))

static int
request_slot_offset(PyTypeObject *type, const char *name, Py_ssize_t *out)
{
    PyObject *descr = type->tp_dict == NULL
        ? NULL : PyDict_GetItemString(type->tp_dict, name);
    if (descr == NULL || !PyObject_TypeCheck(descr, &PyMemberDescr_Type)) {
        PyErr_Format(
            PyExc_RuntimeError,
            "%s has no __slots__ member %s; Request and the native activation "
            "kernel are out of step and the extension must be rebuilt",
            type->tp_name, name);
        return -1;
    }
    *out = ((PyMemberDescrObject *)descr)->d_member->offset;
    return 0;
}

static void
request_layout_free(PyObject *capsule)
{
    RequestLayout *layout = PyCapsule_GetPointer(
        capsule, REQUEST_LAYOUT_CAPSULE);
    if (layout == NULL) {
        PyErr_Clear();
        return;
    }
    Py_DECREF(layout->type);
    Py_DECREF(layout->client_source);
    PyMem_Free(layout);
}

PyObject *
wreath_request_layout(PyObject *Py_UNUSED(module), PyObject *type_object)
{
    if (!PyType_Check(type_object)) {
        PyErr_SetString(PyExc_TypeError, "request_layout expects a Request type");
        return NULL;
    }
    PyTypeObject *type = (PyTypeObject *)type_object;
    RequestLayout *layout = PyMem_Calloc(1, sizeof(*layout));
    if (layout == NULL) return PyErr_NoMemory();
    layout->type = (PyTypeObject *)Py_NewRef(type_object);
    layout->client_source = PyUnicode_FromString("socket");
#define REQUEST_OFFSET(field, name) \
    request_slot_offset(type, (name), &layout->field) < 0
    if (layout->client_source == NULL ||
        REQUEST_OFFSET(scope, "_scope") ||
        REQUEST_OFFSET(context, "_context") ||
        REQUEST_OFFSET(receive, "_receive") ||
        REQUEST_OFFSET(client_source_slot, "_client_source") ||
        REQUEST_OFFSET(app, "_app") ||
        REQUEST_OFFSET(header_map, "_header_map") ||
        REQUEST_OFFSET(header_scanned, "_header_scanned") ||
        REQUEST_OFFSET(path_params, "_path_params") ||
        REQUEST_OFFSET(policy_mask, "_policy_mask") ||
        REQUEST_OFFSET(identity, "_identity") ||
        REQUEST_OFFSET(route_outcome, "_route_outcome") ||
        REQUEST_OFFSET(state, "_state") ||
        REQUEST_OFFSET(limits, "_limits")) {
        Py_DECREF(layout->type);
        Py_XDECREF(layout->client_source);
        PyMem_Free(layout);
        return NULL;
    }
#undef REQUEST_OFFSET
    return PyCapsule_New(
        layout, REQUEST_LAYOUT_CAPSULE, request_layout_free);
}

static void
request_slot_init(PyObject *request, Py_ssize_t offset, PyObject *value)
{
    REQUEST_SLOT(request, offset) = Py_NewRef(value);
}

PyObject *
wreath_request_new(PyObject *Py_UNUSED(module), PyObject *const *args,
                   Py_ssize_t nargs)
{
    if (nargs != 6 && nargs != 7) {
        PyErr_Format(PyExc_TypeError,
                     "request_new expected 6 or 7 arguments, got %zd", nargs);
        return NULL;
    }
    RequestLayout *layout = PyCapsule_GetPointer(
        args[0], REQUEST_LAYOUT_CAPSULE);
    if (layout == NULL) return NULL;
    PyObject *request = layout->type->tp_alloc(layout->type, 0);
    if (request == NULL) return NULL;
    if (PyDict_Check(args[1])) {
        request_slot_init(request, layout->scope, args[1]);
        request_slot_init(request, layout->context, Py_None);
    }
    else {
        request_slot_init(request, layout->scope, Py_None);
        request_slot_init(request, layout->context, args[1]);
    }
    request_slot_init(request, layout->receive, args[2]);
    request_slot_init(request, layout->client_source_slot,
                      layout->client_source);
    request_slot_init(request, layout->app, args[5]);
    request_slot_init(request, layout->header_map, Py_None);
    request_slot_init(request, layout->header_scanned, Py_False);
    request_slot_init(request, layout->path_params, args[3]);
    request_slot_init(
        request, layout->policy_mask,
        Py_GetConstantBorrowed(Py_CONSTANT_ZERO));
    request_slot_init(
        request, layout->identity, nargs == 7 ? args[6] : Py_None);
    request_slot_init(request, layout->route_outcome, Py_None);
    request_slot_init(request, layout->state, Py_None);
    request_slot_init(request, layout->limits, args[4]);
    return request;
}

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
