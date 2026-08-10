#include "connection.h"

#include "operation.h"
#include "pipeline.h"
#include "plan.h"
#include "record.h"

static PyObject *pure_module = NULL;
static PyObject *connect_buffered = NULL;
static PyObject *connection_type = NULL;
static PyObject *buffered_protocol_type = NULL;

static PyObject *
postgres_connect(PyObject *module, PyObject *args, PyObject *kwargs)
{
    static char *kwlist[] = {
        "dsn", "statement_cache_size", "statement_cache_bytes", NULL
    };
    PyObject *dsn;
    Py_ssize_t cache_size = 100;
    Py_ssize_t cache_bytes = 4 * 1024 * 1024;
    (void)module;
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "U|nn:connect", kwlist,
            &dsn, &cache_size, &cache_bytes)) {
        return NULL;
    }

    PyObject *call_args = PyTuple_Pack(
        3, dsn, connection_type, buffered_protocol_type);
    if (call_args == NULL) return NULL;
    PyObject *call_kwargs = PyDict_New();
    PyObject *size_obj = PyLong_FromSsize_t(cache_size);
    PyObject *bytes_obj = PyLong_FromSsize_t(cache_bytes);
    if (call_kwargs == NULL || size_obj == NULL || bytes_obj == NULL ||
        PyDict_SetItemString(call_kwargs, "statement_cache_size", size_obj) < 0 ||
        PyDict_SetItemString(call_kwargs, "statement_cache_bytes", bytes_obj) < 0) {
        Py_DECREF(call_args);
        Py_XDECREF(call_kwargs);
        Py_XDECREF(size_obj);
        Py_XDECREF(bytes_obj);
        return NULL;
    }
    Py_DECREF(size_obj);
    Py_DECREF(bytes_obj);
    PyObject *result = PyObject_Call(connect_buffered, call_args, call_kwargs);
    Py_DECREF(call_kwargs);
    Py_DECREF(call_args);
    return result;
}

static PyObject *str_reader_attr = NULL;
static PyObject *str_read_message = NULL;

static PyObject *
connection_receive_message(PyObject *self, PyObject *unused)
{
    PyObject *reader;
    PyObject *result;
    (void)unused;
    reader = PyObject_GetAttr(self, str_reader_attr);
    if (reader == NULL) return NULL;
    result = PyObject_CallMethodNoArgs(reader, str_read_message);
    Py_DECREF(reader);
    return result;
}

static PyMethodDef connection_type_methods[] = {
    {"_receive_message", connection_receive_message, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static PyMethodDef connection_methods[] = {
    {"connect", (PyCFunction)(void (*)(void))postgres_connect,
     METH_VARARGS | METH_KEYWORDS, "Open a native PostgreSQL connection."},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot connection_slots[] = {
    {Py_tp_methods, connection_type_methods},
    {0, NULL}
};

static PyType_Spec connection_spec = {
    .name = "wreath._native._postgres.Connection",
    .basicsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .slots = connection_slots,
};

static int
copy_attribute(PyObject *module, const char *name)
{
    PyObject *value = PyObject_GetAttrString(pure_module, name);
    if (value == NULL) return -1;
    if (PyModule_AddObject(module, name, value) < 0) {
        Py_DECREF(value);
        return -1;
    }
    return 0;
}

static int
set_backend_hook(const char *name, PyObject *value)
{
    return PyObject_SetAttrString(connection_type, name, value);
}

int
wreath_pg_connection_init(PyObject *module)
{
    PyObject *base = NULL;
    PyObject *bases = NULL;
    PyObject *value = NULL;
    static const char *copied[] = {
        "PostgresError", "InterfaceError", "OperationalError", "ProtocolError",
        "PipelineFullError", "_read_message", "_scram_start", "_scram_continue", "_scram_finish", NULL
    };

    str_reader_attr = PyUnicode_InternFromString("_reader");
    str_read_message = PyUnicode_InternFromString("read_message");
    if (str_reader_attr == NULL || str_read_message == NULL) return -1;
    pure_module = PyImport_ImportModule("wreath._pgdriver");
    if (pure_module == NULL) return -1;
    connect_buffered = PyObject_GetAttrString(pure_module, "_connect_buffered");
    buffered_protocol_type = PyObject_GetAttrString(module, "BufferedProtocol");
    base = PyObject_GetAttrString(pure_module, "Connection");
    if (connect_buffered == NULL || buffered_protocol_type == NULL || base == NULL)
        goto error;
    bases = PyTuple_Pack(1, base);
    Py_DECREF(base);
    if (bases == NULL) goto error;
    connection_type = PyType_FromSpecWithBases(&connection_spec, bases);
    Py_DECREF(bases);
    if (connection_type == NULL) goto error;

    if (set_backend_hook("_record_type", (PyObject *)WreathPgRecordType) < 0 ||
        set_backend_hook("_plan_type", (PyObject *)WreathPgPlanType) < 0 ||
        set_backend_hook("_operation_type", WreathPgOperationType) < 0 ||
        set_backend_hook("_batch_decode", Py_True) < 0) goto error;
    value = PyObject_GetAttrString(module, "_FieldTape");
    if (value == NULL || set_backend_hook("_field_tape_type", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_compile_decoder_plan");
    if (value == NULL || set_backend_hook("_compile_decoder_plan", value) < 0)
        goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_decode_field_tape");
    if (value == NULL || set_backend_hook("_decode_tape", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_decode_fetch_extend");
    if (value == NULL || set_backend_hook("_decode_fetch_extend", value) < 0)
        goto error;
    Py_CLEAR(value);
    /* Lets a caller decode straight into its own destination instead of
       Records; the driver treats that destination as opaque. */
    value = PyObject_GetAttrString(module, "_decode_models");
    if (value == NULL || set_backend_hook("_decode_dest", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_decode_value");
    if (value == NULL || set_backend_hook("_decode", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_build_cold_query_packet");
    if (value == NULL || set_backend_hook("_build_cold", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_build_cached_query_packet");
    if (value == NULL || set_backend_hook("_build_cached", value) < 0) goto error;
    Py_CLEAR(value);
    value = PyObject_GetAttrString(module, "_join_pipeline_packets");
    if (value == NULL || set_backend_hook("_join_packets", value) < 0) goto error;
    Py_CLEAR(value);

    for (const char **name = copied; *name != NULL; name++) {
        if (copy_attribute(module, *name) < 0) goto error;
    }
    if (PyModule_AddObjectRef(module, "Connection", connection_type) < 0 ||
        PyModule_AddFunctions(module, connection_methods) < 0) goto error;
    /* After the type exists and after the backend hooks above, because the
       pipeline reads `_plan_type`/`_operation_type`/`_batch_decode` off it. */
    if (wreath_pg_pipeline_init(module, connection_type) < 0) goto error;
    Py_DECREF(connection_type);
    return 0;

error:
    Py_XDECREF(value);
    return -1;
}

void
wreath_pg_connection_fini(void)
{
    wreath_pg_pipeline_fini();
    connection_type = NULL;
    Py_CLEAR(connect_buffered);
    Py_CLEAR(buffered_protocol_type);
    Py_CLEAR(pure_module);
    Py_CLEAR(str_reader_attr);
    Py_CLEAR(str_read_message);
}
