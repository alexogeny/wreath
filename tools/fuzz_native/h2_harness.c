#include "harness.h"

typedef struct {
    PyObject *drive_h2;
} H2State;

static const char state_key[] = "wreath.fuzz.h2";

static void
destroy_state(PyObject *capsule)
{
    H2State *state = PyCapsule_GetPointer(capsule, state_key);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XDECREF(state->drive_h2);
    PyMem_Free(state);
}

static const char driver_source[] =
    "import asyncio\n"
    "from wreath.server import ServerConfig\n"
    "_loop = asyncio.new_event_loop()\n"
    "_config = ServerConfig(\n"
    "    protocols=('h2',),\n"
    "    max_body_bytes=65536,\n"
    "    max_body_chunks=128,\n"
    "    max_header_list_bytes=16384,\n"
    "    max_header_count=128,\n"
    ")\n"
    "_registry = set()\n"
    "class _Transport:\n"
    "    closed = False\n"
    "    def write(self, data):\n"
    "        pass\n"
    "    def writelines(self, chunks):\n"
    "        pass\n"
    "    def close(self):\n"
    "        self.closed = True\n"
    "    def abort(self):\n"
    "        self.closed = True\n"
    "    def is_closing(self):\n"
    "        return self.closed\n"
    "    def pause_reading(self):\n"
    "        pass\n"
    "    def resume_reading(self):\n"
    "        pass\n"
    "    def get_extra_info(self, name, default=None):\n"
    "        if name == 'sockname':\n"
    "            return ('127.0.0.1', 8000)\n"
    "        if name == 'peername':\n"
    "            return ('127.0.0.1', 50000)\n"
    "        return default\n"
    "async def _app(scope, receive, send):\n"
    "    await send({'type': 'http.response.start', 'status': 204, 'headers': []})\n"
    "    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})\n"
    "def _drive_h2(data):\n"
    "    protocol = _protocol(_app, _config, _loop, _registry)\n"
    "    protocol.connection_made(_Transport())\n"
    "    protocol.data_received(b'PRI * HTTP/2.0\\r\\n\\r\\nSM\\r\\n\\r\\n' + data)\n"
    "    protocol.connection_lost(None)\n"
    "    _loop.run_until_complete(asyncio.sleep(0))\n";

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    PyObject *server;
    PyObject *protocol;
    PyObject *globals;
    PyObject *executed;
    H2State *state;
    (void)argc;
    (void)argv;

    Py_Initialize();
    state = wreath_fuzz_allocate_state(sizeof(*state));
    server = PyImport_ImportModule("wreath._native._server");
    if (server == NULL) wreath_fuzz_abort_python();
    protocol = PyObject_GetAttrString(server, "Http2Protocol");
    Py_DECREF(server);
    if (protocol == NULL) wreath_fuzz_abort_python();
    globals = PyDict_New();
    if (globals == NULL ||
        PyDict_SetItemString(globals, "__builtins__", PyEval_GetBuiltins()) < 0 ||
        PyDict_SetItemString(globals, "_protocol", protocol) < 0) {
        Py_XDECREF(globals);
        Py_DECREF(protocol);
        wreath_fuzz_abort_python();
    }
    Py_DECREF(protocol);
    executed = PyRun_String(driver_source, Py_file_input, globals, globals);
    if (executed == NULL) {
        Py_DECREF(globals);
        wreath_fuzz_abort_python();
    }
    Py_DECREF(executed);
    state->drive_h2 = PyDict_GetItemString(globals, "_drive_h2");
    Py_XINCREF(state->drive_h2);
    Py_DECREF(globals);
    if (state->drive_h2 == NULL) wreath_fuzz_abort_python();
    wreath_fuzz_store_state(state_key, state, destroy_state);
    return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *result;
    H2State *state;
    if (size > 65536) return 0;
    state = wreath_fuzz_get_state(state_key);
    result = PyObject_CallFunction(
        state->drive_h2, "y#", (const char *)data, (Py_ssize_t)size);
    if (result == NULL) wreath_fuzz_abort_python();
    Py_DECREF(result);
    return 0;
}
