/* wreath._native._reactor — native primitives for the reactor event loop.
 *
 * Stage-0 component: a hashed timing wheel for per-connection deadlines.
 *
 * asyncio keeps timers in a binary heap: O(log n) insert and cancel, and it
 * pays a cancellation-compaction pass. A server at high RPS churns two timers
 * per request (keep-alive + request deadline), almost always cancelling them
 * before they fire. A hashed timing wheel makes insert and cancel O(1) with a
 * fixed, tiny memory footprint (one slot array + intrusive nodes), which is
 * exactly the shape of that workload.
 *
 * Design: `slots` buckets at `resolution` seconds each. A timer due in `d`
 * ticks lands in bucket `(cur + d) % slots` carrying its absolute deadline; an
 * interval tree over the buckets tracks each slot's minimum deadline (plus how
 * many nodes tie it), so the drain loop jumps the cursor from due tick to due
 * tick without touching parked long timers, and a batch of same-deadline
 * fires/cancels pays one chain rescan total. Insert/cancel are pointer splices
 * on an intrusive doubly linked list — no reallocation, no heapify, no
 * compaction.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <descrobject.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <linux/io_uring.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>

#include "server.h"
#include "reactor_internal.h"

static PyObject *g_buffered_protocol;
static PyObject *g_s_context_run;

/* ======================================================================== */
/* SocketTransport: a native asyncio Transport for plaintext TCP.             */
/*                                                                            */
/* App-facing behaviour matches asyncio's _SelectorSocketTransport exactly    */
/* (connection_made/get_buffer/buffer_updated/data_received/eof_received/     */
/* connection_lost, flow control, pause/resume). Under the hood it uses       */
/* direct recv/send syscalls, a single contiguous offset write buffer (O(1)   */
/* size, one send -- no chunk list / sendmsg), cached bound protocol methods, */
/* and a bounded eager read-drain so a burst costs fewer readiness CQEs.     */
/* ======================================================================== */

#include "reactor_tls.h"

#include "reactor_ring.c"
#include "reactor_transport.c"
#include "reactor_poller.c"

static PyModuleDef reactormodule = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._reactor",
    .m_doc = "Native reactor primitives (timing wheel + shootout stores + transport).",
    .m_size = 0,
};

static PyMemberDef *
handle_member(PyObject *handle_type, const char *name)
{
    PyObject *descriptor = PyObject_GetAttrString(handle_type, name);
    if (descriptor == NULL) {
        return NULL;
    }
    if (!Py_IS_TYPE(descriptor, &PyMemberDescr_Type) ||
            PyDescr_TYPE(descriptor) != (PyTypeObject *)handle_type) {
        PyErr_Format(PyExc_RuntimeError, "asyncio.Handle.%s is not a member descriptor", name);
        Py_DECREF(descriptor);
        return NULL;
    }
    PyMemberDef *member = ((PyMemberDescrObject *)descriptor)->d_member;
    Py_DECREF(descriptor);
    return member;
}

static int
load_handle_layout(void)
{
    /* CPython 3.14-specific but offset-independent: resolve Handle's slot
     * descriptors once, then use PyMember_GetOne in the ready-queue hot path. */
    /* native-lint: allow NC004 -- called once by module init, never per callback */
    PyObject *events = PyImport_ImportModule("asyncio.events");
    if (events == NULL) {
        return -1;
    }
    PyObject *handle = PyObject_GetAttrString(events, "Handle");
    Py_DECREF(events);
    if (handle == NULL) {
        return -1;
    }
    if (!PyType_Check(handle)) {
        PyErr_SetString(PyExc_RuntimeError, "asyncio.events.Handle is not a type");
        Py_DECREF(handle);
        return -1;
    }
    g_handle_type = (PyTypeObject *)handle;
    g_handle_callback = handle_member(handle, "_callback");
    g_handle_args = handle_member(handle, "_args");
    g_handle_cancelled = handle_member(handle, "_cancelled");
    g_handle_context = handle_member(handle, "_context");
    g_handle_source_traceback = handle_member(handle, "_source_traceback");
    if (g_handle_callback == NULL || g_handle_args == NULL ||
            g_handle_cancelled == NULL || g_handle_context == NULL ||
            g_handle_source_traceback == NULL) {
        return -1;
    }
    /* CPython's non-limited structmember T_OBJECT remains numeric 6 while the
     * public 3.14 spelling only exposes Py_T_OBJECT_EX. */
    int callback_object = g_handle_callback->type == Py_T_OBJECT_EX ||
                          g_handle_callback->type == 6;
    int args_object = g_handle_args->type == Py_T_OBJECT_EX ||
                      g_handle_args->type == 6;
    int context_object = g_handle_context->type == Py_T_OBJECT_EX ||
                         g_handle_context->type == 6;
    if (!callback_object || !args_object || !context_object ||
        g_handle_cancelled->type != Py_T_OBJECT_EX) {
        PyErr_Format(
            PyExc_RuntimeError,
            "asyncio.Handle layout callback=%d args=%d context=%d cancelled=%d",
            g_handle_callback->type, g_handle_args->type,
            g_handle_context->type, g_handle_cancelled->type);
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------------ *
 * Loop fast path: create_future / get_debug
 * ------------------------------------------------------------------ */

/* `EventLoop` inherits both of these from `asyncio.base_events` as Python
 * functions, and between them they cost two interpreter frames on every future
 * this loop creates -- `create_future` itself, plus the `get_debug` that
 * `asyncio.Future.__init__` calls on its loop before deciding whether to
 * capture a source traceback.
 *
 * That is charged per future, and a PostgreSQL query creates two (the
 * operation's, and the protocol's read waiter). Grafting C equivalents onto the
 * heap type removes both frames without changing what either returns:
 * `get_debug` still reports `self._debug`, so `loop.set_debug(True)` keeps
 * working and a debug loop still gets its tracebacks.
 *
 * Deliberately *not* a custom Future type, which was the tempting next step:
 * CPython's C `Task` has a `Future_CheckExact` fast path, and anything else
 * falls back to fetching `_asyncio_future_blocking` and calling
 * `add_done_callback` through the abstract protocol. A cheaper future that
 * lands on the slow path is not cheaper. */

static PyObject *g_future_type = NULL;
static PyObject *g_s_debug_attr = NULL;
static PyObject *g_loop_kwnames = NULL;

static PyObject *
loop_get_debug(PyObject *self, PyObject *unused)
{
    PyObject *debug;
    int truth;
    (void)unused;
    debug = PyObject_GetAttr(self, g_s_debug_attr);
    if (debug == NULL) return NULL;
    truth = PyObject_IsTrue(debug);
    Py_DECREF(debug);
    if (truth < 0) return NULL;
    return PyBool_FromLong(truth);
}

static PyObject *
loop_create_future(PyObject *self, PyObject *unused)
{
    PyObject *args[1] = {self};
    (void)unused;
    /* `Future(loop=self)`, by vectorcall so the keyword costs no dict. */
    return PyObject_Vectorcall(g_future_type, args, 0, g_loop_kwnames);
}

static PyMethodDef loop_fastpath_methods[] = {
    {"create_future", loop_create_future, METH_NOARGS, NULL},
    {"get_debug", loop_get_debug, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

/* Graft the two onto a caller-supplied heap type. Called from `reactor.py`
   once, at import, with `EventLoop`. */
static PyObject *
install_loop_fastpath(PyObject *module, PyObject *type)
{
    (void)module;
    if (!PyType_Check(type)) {
        PyErr_SetString(PyExc_TypeError, "a loop type is required");
        return NULL;
    }
    for (PyMethodDef *def = loop_fastpath_methods; def->ml_name != NULL; def++) {
        PyObject *descriptor = PyDescr_NewMethod((PyTypeObject *)type, def);
        if (descriptor == NULL) return NULL;
        if (PyObject_SetAttrString(type, def->ml_name, descriptor) < 0) {
            Py_DECREF(descriptor);
            return NULL;
        }
        Py_DECREF(descriptor);
    }
    PyType_Modified((PyTypeObject *)type);
    Py_RETURN_NONE;
}

static PyMethodDef reactor_module_methods[] = {
    {"_install_loop_fastpath", install_loop_fastpath, METH_O,
     "Graft the C create_future/get_debug onto a loop type."},
    {NULL, NULL, 0, NULL}
};

static int
loop_fastpath_ready(void)
{
    /* native-lint: allow NC004 -- one-time module initialization */
    PyObject *futures = PyImport_ImportModule("asyncio.futures");
    PyObject *loop_name;
    if (futures == NULL) return -1;
    g_future_type = PyObject_GetAttrString(futures, "Future");
    Py_DECREF(futures);
    if (g_future_type == NULL) return -1;
    g_s_debug_attr = PyUnicode_InternFromString("_debug");
    loop_name = PyUnicode_InternFromString("loop");
    if (g_s_debug_attr == NULL || loop_name == NULL) {
        Py_XDECREF(loop_name);
        return -1;
    }
    g_loop_kwnames = PyTuple_Pack(1, loop_name);
    Py_DECREF(loop_name);
    return g_loop_kwnames == NULL ? -1 : 0;
}

PyMODINIT_FUNC
PyInit__reactor(void)
{
    if (wreath_reactor_timers_ready() < 0 ||
        PyType_Ready(&SocketTransportType) < 0 ||
        PyType_Ready(&ReactorPollerType) < 0 ||
        PyType_Ready(&WreathReadyHandleType) < 0) {
        return NULL;
    }
    /* intern the attribute names the run loop touches every iteration */
    g_s_when = PyUnicode_InternFromString("_when");
    g_s_run = PyUnicode_InternFromString("_run");
    g_s_context_run = PyUnicode_InternFromString("run");
    g_s_cancelled = PyUnicode_InternFromString("_cancelled");
    g_s_scheduled = PyUnicode_InternFromString("_scheduled");
    g_s_popleft = PyUnicode_InternFromString("popleft");
    g_s_append = PyUnicode_InternFromString("append");
    PyObject *fileno = PyUnicode_InternFromString("fileno");
    if (fileno != NULL) {
        g_fileno_kwnames = PyTuple_Pack(1, fileno);
        Py_DECREF(fileno);
    }
    if (!g_s_when || !g_s_run || !g_s_context_run || !g_s_cancelled ||
            !g_s_scheduled || !g_s_popleft || !g_s_append ||
            !g_fileno_kwnames) {
        return NULL;
    }
    /* native-lint: allow NC004 -- one-time module initialization */
    PyObject *protocols = PyImport_ImportModule("asyncio.protocols");
    if (protocols == NULL) {
        return NULL;
    }
    g_buffered_protocol = PyObject_GetAttrString(protocols, "BufferedProtocol");
    Py_DECREF(protocols);
    if (g_buffered_protocol == NULL) {
        return NULL;
    }
    if (load_handle_layout() < 0 || loop_fastpath_ready() < 0) {
        return NULL;
    }
    /* native-lint: allow NC004 -- one-time module-init lookup, never per value */
    PyObject *heapq = PyImport_ImportModule("heapq");
    if (heapq == NULL) {
        return NULL;
    }
    g_heappop = PyObject_GetAttrString(heapq, "heappop");
    Py_DECREF(heapq);
    if (g_heappop == NULL) {
        return NULL;
    }
    PyObject *m = PyModule_Create(&reactormodule);
    if (m == NULL) {
        return NULL;
    }
    PyObject *transport_capsule = PyCapsule_New(
        &transport_capi, WREATH_TRANSPORT_CAPI_NAME, NULL);
    if (transport_capsule == NULL ||
        PyModule_AddObject(m, "_TRANSPORT_C_API", transport_capsule) < 0) {
        Py_XDECREF(transport_capsule);
        Py_DECREF(m);
        return NULL;
    }
    if (PyModule_AddFunctions(m, reactor_module_methods) < 0 ||
        wreath_reactor_timers_add(m) < 0 ||
        PyModule_AddObjectRef(m, "SocketTransport", (PyObject *)&SocketTransportType) < 0 ||
        PyModule_AddObjectRef(m, "ReactorPoller", (PyObject *)&ReactorPollerType) < 0 ||
        wreath_tls_register(m) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
