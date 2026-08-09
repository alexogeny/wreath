/* Native io_uring event-loop poller. */
/* ======================================================================== *
 *  ReactorPoller — the native run loop core.                               *
 *                                                                          *
 *  Metal's EventLoop rebinds its own _add_reader / _add_writer /          *
 *  _remove_reader / _remove_writer / _run_once to this object's C          *
 *  methods. It owns a tagged io_uring completion domain and an             *
 *  fd-indexed registry of reader/writer callables, so a ready socket       *
 *  dispatches the transport's C _read_ready DIRECTLY: no selector.select   *
 *  wrapper, no _process_events, no per-event Handle allocation, no         *
 *  Handle._run/context.run. This is the layer uvloop has in libuv and the  *
 *  stock asyncio SelectorEventLoop pays for in Python on every iteration.  *
 *                                                                          *
 *  Reader/writer callbacks run WITHOUT a copied contextvars context: the   *
 *  native transport does not read contextvars, and metal is a controlled   *
 *  runtime. call_soon Handles and timers still run through their normal     *
 *  Handle._run (context-correct); only the fd-readiness fast path is bare. *
 * ======================================================================== */

/* cached, interned attribute names + heapq.heappop, set in PyInit */
static PyObject *g_s_when;       /* "_when"      */
static PyObject *g_s_run;        /* "_run"       */
static PyObject *g_s_cancelled;  /* "_cancelled" */
static PyObject *g_s_scheduled;  /* "_scheduled" */
static PyObject *g_s_popleft;    /* "popleft"    */
static PyObject *g_s_append;     /* "append"     */
static PyObject *g_fileno_kwnames; /* vectorcall keyword tuple */
static PyObject *g_heappop;      /* heapq.heappop */
static PyTypeObject *g_handle_type;
static PyTypeObject *g_task_step_type;
static PyMemberDef *g_handle_callback;
static PyMemberDef *g_handle_args;
static PyMemberDef *g_handle_cancelled;
static PyMemberDef *g_handle_context;
static PyMemberDef *g_handle_source_traceback;

typedef struct {
    PyObject *reader;       /* callable or NULL */
    PyObject *reader_args;  /* tuple, or NULL for no-arg fast call */
    PyObject *writer;
    PyObject *writer_args;
    PyObject *accept_callback;
    int native_reader;
    int native_writer;
    int accept_active;
    int accept_multishot;
    int poll_multishot;
    uint32_t mask;          /* POLLIN/POLLOUT mask owned by one ring poll request */
    uint32_t generation;    /* registration incarnation carried in data.u64 */
} FdEntry;

/* ======================================================================== */
/* WreathReadyHandle: the Handle-free call_soon fast path.                  */
/*                                                                          */
/* asyncio's call_soon costs two Python frames (call_soon -> _call_soon)    */
/* plus a Python-class Handle construction per wakeup; every Future         */
/* callback and Task step scheduling pays it. The metal loop rebinds        */
/* loop.call_soon to the poller's C implementation, which enqueues this     */
/* freelisted C handle instead; the run loop recognizes it and runs the     */
/* callback inside its context directly. Legacy asyncio.Handles (e.g. from  */
/* call_soon_threadsafe, which stays on the base implementation for its     */
/* thread safety) continue through the existing path, sharing one FIFO      */
/* deque so cross-source ordering is preserved.                             */
/* ======================================================================== */

typedef struct {
    PyObject_HEAD
    PyObject *callback;
    PyObject *args;      /* tuple, or NULL for a no-argument callback */
    PyObject *context;
    int cancelled;
} WreathReadyHandle;

#define READY_HANDLE_FREELIST_CAP 64
/* A metal loop owns one OS thread. Under free-threading several loops can live
 * in one process, so a process-global freelist lets two allocators pop or push
 * the same Handle concurrently. The entries are empty shells (dealloc clears
 * every PyObject field), making thread-local ownership sufficient there.
 *
 * Keep the ordinary ABI process-local: ELF dynamic TLS lookup measured as the
 * hottest symbol in the saturated Fortunes profile (3.0% of user cycles), and
 * the GIL already serializes access in that build. */
#ifdef Py_GIL_DISABLED
static _Thread_local WreathReadyHandle *
    ready_handle_freelist[READY_HANDLE_FREELIST_CAP];
static _Thread_local int ready_handle_freelist_len = 0;
#else
static WreathReadyHandle *ready_handle_freelist[READY_HANDLE_FREELIST_CAP];
static int ready_handle_freelist_len = 0;
#endif

static PyTypeObject WreathReadyHandleType;

static void
ready_handle_dealloc(PyObject *op)
{
    WreathReadyHandle *handle = (WreathReadyHandle *)op;
    PyObject_GC_UnTrack(op);
    Py_CLEAR(handle->callback);
    Py_CLEAR(handle->args);
    Py_CLEAR(handle->context);
    if (ready_handle_freelist_len < READY_HANDLE_FREELIST_CAP) {
        ready_handle_freelist[ready_handle_freelist_len++] = handle;
    } else {
        PyObject_GC_Del(op);
    }
}

static int
ready_handle_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathReadyHandle *handle = (WreathReadyHandle *)op;
    Py_VISIT(handle->callback);
    Py_VISIT(handle->args);
    Py_VISIT(handle->context);
    return 0;
}

static int
ready_handle_clear_slot(PyObject *op)
{
    WreathReadyHandle *handle = (WreathReadyHandle *)op;
    Py_CLEAR(handle->callback);
    Py_CLEAR(handle->args);
    Py_CLEAR(handle->context);
    return 0;
}

static PyObject *
ready_handle_cancel(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WreathReadyHandle *handle = (WreathReadyHandle *)op;
    handle->cancelled = 1;
    /* Match asyncio.Handle.cancel: release what the callback closed over. */
    Py_CLEAR(handle->callback);
    Py_CLEAR(handle->args);
    Py_RETURN_NONE;
}

static PyObject *
ready_handle_cancelled(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(((WreathReadyHandle *)op)->cancelled);
}

static PyMethodDef ready_handle_methods[] = {
    {"cancel", ready_handle_cancel, METH_NOARGS, NULL},
    {"cancelled", ready_handle_cancelled, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject WreathReadyHandleType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.ReadyHandle",
    .tp_basicsize = sizeof(WreathReadyHandle),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = ready_handle_dealloc,
    .tp_traverse = ready_handle_traverse,
    .tp_clear = ready_handle_clear_slot,
    .tp_methods = ready_handle_methods,
};

static WreathReadyHandle *
ready_handle_new(PyObject *callback, PyObject *const *args, Py_ssize_t nargs,
                 PyObject *context)
{
    WreathReadyHandle *handle;
    if (ready_handle_freelist_len > 0) {
        handle = ready_handle_freelist[--ready_handle_freelist_len];
        _Py_NewReference((PyObject *)handle);
    } else {
        handle = PyObject_GC_New(WreathReadyHandle, &WreathReadyHandleType);
        if (handle == NULL) {
            return NULL;
        }
    }
    handle->callback = Py_NewRef(callback);
    handle->cancelled = 0;
    if (nargs > 0) {
        handle->args = PyTuple_New(nargs);
        if (handle->args == NULL) {
            handle->context = NULL;
            Py_DECREF(handle);
            return NULL;
        }
        for (Py_ssize_t i = 0; i < nargs; i++) {
            PyTuple_SET_ITEM(handle->args, i, Py_NewRef(args[i]));
        }
    } else {
        handle->args = NULL;
    }
    if (context == NULL || context == Py_None) {
        handle->context = PyContext_CopyCurrent();
        if (handle->context == NULL) {
            Py_DECREF(handle);
            return NULL;
        }
    } else {
        handle->context = Py_NewRef(context);
    }
    /* PyObject_GC_New is untracked on the ordinary 3.14 build but may already
     * be tracked by 3.14t. Tracking twice is a fatal runtime assertion. */
    if (!PyObject_GC_IsTracked((PyObject *)handle)) {
        PyObject_GC_Track((PyObject *)handle);
    }
    return handle;
}

typedef struct {
    PyObject_HEAD
    int wake_fd;
    int signal_fd;
    int wake_poll_multishot;
    int signal_poll_multishot;
    PyObject *signal_callback;
    FdEntry *fds;
    int fdcap;
    PyObject *loop;         /* the EventLoop (for call_exception_handler) */
    PyObject *ready;        /* loop._ready (collections.deque) */
    PyObject *ready_popleft; /* cached bound deque method */
    PyObject *ready_append;  /* cached bound deque method */
    PyObject *scheduled;    /* loop._scheduled (heapq list) */
    PyObject *exc_handler;  /* loop.call_exception_handler (bound) */
    PyObject *wheel_obj;    /* TimingWheel or None */
    TimingWheel *wheel;     /* borrowed from wheel_obj */
    double clock_res;       /* loop._clock_resolution */
    int direct_task_steps;  /* bypass Handle._run for C Task step callbacks */
    int closed;             /* poller closed: fast call_soon must reject */
    /* Loop-driven cycle collection. NULL leaves CPython's allocation-triggered
     * collector entirely in charge, which is the behaviour of every tier that
     * does not install one here. */
    PyObject *gc_collect;           /* gc.collect */
    PyObject *gc_young_generation;  /* the cached literal 0 argument */
    uint64_t gc_idle_ns;            /* arrival gap above which a wait is idle */
    uint64_t gc_full_idle_ns;       /* ... and idle enough for a full collection */
    uint64_t gc_min_interval_ns;    /* floor between two loop collections */
    uint64_t gc_last_collect_ns;
    int gc_dirty;                   /* work ran since the last loop collection */
    uint64_t gc_young_collections;
    uint64_t gc_full_collections;
    uint64_t gc_collect_nanoseconds;
    uint64_t generation_wraps;
    MetalRuntime metal;
} ReactorPoller;

static PyTypeObject ReactorPollerType;
static void rp_dispatch(ReactorPoller *, PyObject *, PyObject *);
static void rp_dispatch_native_transport(ReactorPoller *, PyObject *, int);
static void rp_report_callback_error(ReactorPoller *);
static int rp_submit_receive(ReactorPoller *, SocketTransport *);
static uint64_t rp_poll_token(uint32_t, int);

/* Register a live connection with this poller, which OWNS it until it is
 * detached.
 *
 * The owning reference is not bookkeeping: it is the metal equivalent of the
 * one asyncio's selector registration provides. A stock loop keeps an accepted
 * transport alive through the bound `_read_ready` it stored in `_add_reader`;
 * metal drives ingress from an io_uring multishot receive instead and registers
 * no reader, so without this reference an accepted connection is nothing but a
 * transport<->protocol cycle -- reachable from no root, and therefore free for
 * any collection to reap out from under a live socket. Wreath's own `Server`
 * tracks its protocols and so happened to be safe; every other protocol on this
 * loop (`wreath.postgres`, `wreath.http_client`, any third-party
 * `loop.create_server`) was not.
 *
 * A borrowed pointer here is also why the slab could hand `slot->owner` to the
 * completion path at all: that path re-INCREFs for the duration of delivery,
 * which protects the callback but not the connection between callbacks. */
static int
metal_attach_transport(SocketTransport *transport, PyObject *poller_obj)
{
    if (!PyObject_TypeCheck(poller_obj, &ReactorPollerType)) {
        return 0;
    }
    ReactorPoller *poller = (ReactorPoller *)poller_obj;
    uint64_t token = metal_slab_allocate(
        &poller->metal.connections, transport, 0, 0
    );
    if (token == 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "metal connection slab is exhausted");
        return -1;
    }
    transport->poller_obj = Py_NewRef(poller_obj);
    transport->metal = &poller->metal;
    transport->connection_token = token;
    Py_INCREF(transport);  /* released by metal_detach_transport */
    return 0;
}

/* Give the connection back. Idempotent: `st_call_connection_lost` detaches on
 * the ordinary lifecycle, `st_clear` detaches again on teardown, and the poller
 * detaches everything still registered when it closes. */
static void
metal_detach_transport(SocketTransport *transport)
{
    MetalRuntime *runtime = transport->metal;
    uint64_t token = transport->connection_token;
    if (runtime == NULL || token == 0) {
        transport->connection_token = 0;
        return;
    }
    /* Clear the token before releasing: dropping the poller's reference can run
     * this transport's own teardown, which detaches again. */
    transport->connection_token = 0;
    metal_slab_release(&runtime->connections, token, transport);
    Py_DECREF(transport);
}

/* Release every connection this poller still owns. Reached from teardown only:
 * a live loop detaches through the transport's own lifecycle. */
static void
metal_connections_clear(MetalRuntime *runtime)
{
    for (uint32_t index = 0; index < runtime->connections.capacity; index++) {
        MetalSlot *slot = &runtime->connections.slots[index];
        if (!metal_slot_live(slot) || slot->owner == NULL) {
            continue;
        }
        SocketTransport *transport = (SocketTransport *)slot->owner;
        uint64_t token =
            ((uint64_t)metal_slot_generation(slot) << 32) | index;
        /* Detach by hand rather than through metal_detach_transport: the
         * runtime is going away, so the transport must stop pointing at it
         * before its own teardown can try to use it. */
        transport->connection_token = 0;
        transport->metal = NULL;
        metal_slab_release(&runtime->connections, token, transport);
        Py_DECREF(transport);
    }
}

static void
metal_send_operations_clear(MetalRuntime *runtime)
{
    runtime->send_enabled = 0;
    for (uint32_t index = 0; index < runtime->operations.capacity; index++) {
        MetalSlot *operation = &runtime->operations.slots[index];
        if (!metal_slot_live(operation) || operation->owner == NULL) {
            continue;
        }
        uint16_t kind = metal_slot_kind(operation);
        if (kind != METAL_OP_SEND && kind != METAL_OP_RECV) {
            continue;
        }
        SocketTransport *transport = (SocketTransport *)operation->owner;
        uint64_t token =
            ((uint64_t)metal_slot_generation(operation) << 32) | index;
        if (kind == METAL_OP_RECV) {
            transport->uring_receive_active = 0;
            transport->uring_receive_token = 0;
        }
        if (kind == METAL_OP_SEND) {
            /* The ring is shutting down: no CQE will arrive to finish this
             * operation, so the payload can be released here. */
            transport->send_op_token = 0;
            Py_CLEAR(transport->send_obj);
            transport->send_obj_off = 0;
        }
        metal_slab_release(&runtime->operations, token, transport);
        Py_DECREF(transport);
    }
}

static double mono_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* Grow the fd registry so index `fd` is valid, zeroing new slots. */
static int
rp_ensure_fd(ReactorPoller *p, int fd)
{
    if (fd < 0) {
        PyErr_SetString(PyExc_ValueError, "negative file descriptor");
        return -1;
    }
    if (fd < p->fdcap) {
        return 0;
    }
    int newcap = p->fdcap ? p->fdcap : 64;
    while (newcap <= fd) {
        newcap *= 2;
    }
    FdEntry *grown = PyMem_Realloc(p->fds, (size_t)newcap * sizeof(FdEntry));
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    memset(grown + p->fdcap, 0, (size_t)(newcap - p->fdcap) * sizeof(FdEntry));
    p->fds = grown;
    p->fdcap = newcap;
    return 0;
}

/* Reconcile one registration and publish a new {generation, fd} token. `force`
 * refreshes data.u64 when a callback changes without changing the readiness
 * mask, invalidating CQEs already published by the previous poll request. */
static uint64_t
rp_next_listener_token(ReactorPoller *p, int fd)
{
    FdEntry *entry = &p->fds[fd];
    uint32_t generation = entry->generation + 1;
    if (generation == 0) {
        generation = 1;
        p->generation_wraps++;
    }
    entry->generation = generation;
    return ((uint64_t)generation << 32) | (uint32_t)fd;
}

static int
rp_submit_accept(ReactorPoller *p, int fd)
{
    FdEntry *entry = &p->fds[fd];
    uint64_t token = ((uint64_t)entry->generation << 32) | (uint32_t)fd;
    if (metal_uring_queue_accept(&p->metal.uring, fd, token,
                                 entry->accept_multishot) < 0) {
        return -1;
    }
    p->metal.accept_submissions++;
    return 0;
}

static PyObject *
rp_add_uring_listener(PyObject *op, PyObject *args)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd;
    PyObject *callback;
    if (!PyArg_ParseTuple(args, "iO:_add_uring_listener", &fd, &callback)) {
        return NULL;
    }
    /* (protocol_factory, server, socket_factory, family, type, proto). The
     * last three are the listener's, and an accepted connection inherits all
     * three -- see `rp_activate_accepted_connection` for why carrying them
     * costs two fewer syscalls per connection than letting `socket()` ask. */
    int native_spec = PyTuple_CheckExact(callback) &&
                      PyTuple_GET_SIZE(callback) == 7 &&
                      PyCallable_Check(PyTuple_GET_ITEM(callback, 0)) &&
                      PyCallable_Check(PyTuple_GET_ITEM(callback, 2)) &&
                      PyLong_Check(PyTuple_GET_ITEM(callback, 3)) &&
                      PyLong_Check(PyTuple_GET_ITEM(callback, 4)) &&
                      PyLong_Check(PyTuple_GET_ITEM(callback, 5));
    if (!native_spec && !PyCallable_Check(callback)) {
        PyErr_SetString(
            PyExc_TypeError,
            "accept target must be callable or a native server specification");
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    FdEntry *entry = &p->fds[fd];
    if (entry->accept_active) {
        PyErr_SetString(PyExc_RuntimeError, "listener is already registered");
        return NULL;
    }
    rp_next_listener_token(p, fd);
    entry->accept_callback = Py_NewRef(callback);
    entry->accept_active = 1;
    entry->accept_multishot = 1;
    if (rp_submit_accept(p, fd) < 0) {
        entry->accept_active = 0;
        Py_CLEAR(entry->accept_callback);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_remove_uring_listener(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd < 0 || fd >= p->fdcap || !p->fds[fd].accept_active) {
        Py_RETURN_FALSE;
    }
    FdEntry *entry = &p->fds[fd];
    entry->accept_active = 0;
    entry->accept_multishot = 0;
    rp_next_listener_token(p, fd);
    Py_CLEAR(entry->accept_callback);
    Py_RETURN_TRUE;
}

/* C fast paths for the transport: poller_obj is type-checked when the metal
 * runtime attaches, so pause/resume/close need no Python method dispatch.
 * Return 1 started/stopped, 0 not applicable, -1 with the exception set. */
static int
rp_start_uring_receive_native(SocketTransport *transport)
{
    ReactorPoller *p = (ReactorPoller *)transport->poller_obj;
    if (p == NULL || !p->metal.receive_enabled ||
        transport->metal != &p->metal) {
        return 0;
    }
    if (transport->uring_receive_active) {
        return 1;
    }
    transport->uring_receive_active = 1;
    transport->uring_receive_multishot = 1;
    if (rp_submit_receive(p, transport) < 0) {
        transport->uring_receive_active = 0;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    return 1;
}

static int
rp_stop_uring_receive_native(SocketTransport *transport)
{
    ReactorPoller *p = (ReactorPoller *)transport->poller_obj;
    if (p == NULL || !transport->uring_receive_active ||
        transport->metal != &p->metal) {
        return 0;
    }
    uint64_t receive_token = transport->uring_receive_token != 0
        ? transport->uring_receive_token : transport->connection_token;
    if (metal_uring_queue_cancel(&p->metal.uring, receive_token) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    transport->uring_receive_active = 0;
    return 1;
}

static PyObject *
rp_start_uring_receive(PyObject *op, PyObject *transport_obj)
{
    if (!PyObject_TypeCheck(transport_obj, &SocketTransportType)) {
        PyErr_SetString(PyExc_TypeError, "expected SocketTransport");
        return NULL;
    }
    SocketTransport *transport = (SocketTransport *)transport_obj;
    if (transport->poller_obj != op) {
        Py_RETURN_FALSE;
    }
    int started = rp_start_uring_receive_native(transport);
    if (started < 0) {
        return NULL;
    }
    return PyBool_FromLong(started);
}

static PyObject *
rp_stop_uring_receive(PyObject *op, PyObject *transport_obj)
{
    if (!PyObject_TypeCheck(transport_obj, &SocketTransportType)) {
        PyErr_SetString(PyExc_TypeError, "expected SocketTransport");
        return NULL;
    }
    SocketTransport *transport = (SocketTransport *)transport_obj;
    if (transport->poller_obj != op) {
        Py_RETURN_FALSE;
    }
    int stopped = rp_stop_uring_receive_native(transport);
    if (stopped < 0) {
        return NULL;
    }
    return PyBool_FromLong(stopped);
}

static int
rp_submit_receive(ReactorPoller *p, SocketTransport *transport)
{
    /* A persistent provided-buffer receive removes one SQE publication and
     * io_uring_enter from every keep-alive request. Copying the small HTTP head
     * into the parser is cheaper than rearming a direct single-shot receive. */
    if (metal_uring_queue_receive(
            &p->metal.uring, transport->fd,
            transport->connection_token, p->metal.receive_buffers.group_id,
            p->metal.receive_buffers.buffer_size,
            transport->uring_receive_multishot) < 0) {
        return -1;
    }
    transport->uring_receive_token = transport->connection_token;
    p->metal.receive_submissions++;
    return 0;
}

static int
rp_drain_receive_completions(ReactorPoller *p, unsigned budget,
                             unsigned *drained_out)
{
    MetalUring *ring = &p->metal.uring;
    MetalProvidedBuffers *buffers = &p->metal.receive_buffers;
    *drained_out = 0;
    while (*drained_out < budget) {
        unsigned head = ring->cached_cq_head;
        struct io_uring_cqe cqe = ring->cqes[head & ring->cq_mask_value];
        uint64_t tag = cqe.user_data & ~METAL_TOKEN_PAYLOAD_MASK;
        if (cqe.user_data != METAL_CANCEL_TOKEN &&
            tag != METAL_TOKEN_RECEIVE) {
            break;
        }
        ring->cached_cq_head = head + 1;
        (*drained_out)++;
        if (cqe.user_data == METAL_CANCEL_TOKEN) {
            uint8_t flags = cqe.res < 0 ? METAL_COMPLETION_ERROR : 0;
            metal_trace_add(&p->metal, METAL_IO_URING, METAL_OP_CANCEL,
                            cqe.user_data, cqe.res, flags);
            continue;
        }
        uint64_t owner_token = cqe.user_data & METAL_TOKEN_PAYLOAD_MASK;
        if (p->metal.diagnostics) {
            p->metal.receive_completions++;
        }
        uint8_t trace_flags = cqe.res == 0 ? METAL_COMPLETION_EOF : 0;
        if (cqe.res < 0) {
            trace_flags |= METAL_COMPLETION_ERROR;
        }
        metal_trace_add(&p->metal, METAL_IO_URING, METAL_OP_RECV,
                        owner_token, cqe.res, trace_flags);

        int has_buffer = (cqe.flags & IORING_CQE_F_BUFFER) != 0;
        uint16_t buffer_id = (uint16_t)(cqe.flags >> IORING_CQE_BUFFER_SHIFT);
        if (has_buffer && !metal_provided_buffer_claim(buffers, buffer_id)) {
            p->metal.receive_errors++;
            continue;
        }

        uint32_t index = (uint32_t)owner_token;
        uint32_t generation = (uint32_t)(owner_token >> 32);
        MetalSlot *slot = index < p->metal.connections.capacity
            ? &p->metal.connections.slots[index] : NULL;
        if (slot == NULL || !metal_slot_live(slot) ||
            metal_slot_generation(slot) != generation) {
            p->metal.receive_stale++;
            if (has_buffer && buffer_id < buffers->entries) {
                if (metal_provided_buffer_recycle(buffers, buffer_id) < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return -1;
                }
                if (p->metal.diagnostics) {
                    p->metal.provided_buffer_recycles++;
                }
            }
            continue;
        }

        SocketTransport *transport = (SocketTransport *)slot->owner;
        Py_INCREF(transport);
        int has_more = (cqe.flags & IORING_CQE_F_MORE) != 0;
        if (cqe.res > 0 && has_buffer && buffer_id < buffers->entries) {
            if (!transport->conn_lost && !transport->closing) {
                transport->cork = 1;
                const char *data = buffers->data +
                                   (size_t)buffer_id * buffers->buffer_size;
                if (st_deliver_received(transport, data, cqe.res) < 0) {
                    st_fatal(transport, "io_uring receive delivery failed");
                }
                transport->cork = 0;
                st_flush_cork(transport);
            }
        } else if (cqe.res == 0) {
            transport->uring_receive_active = 0;
            if (!transport->conn_lost && !transport->closing) {
                PyObject *eof_result = st_on_eof(transport);
                Py_XDECREF(eof_result);
            }
        } else if (cqe.res > 0) {
            p->metal.receive_errors++;
            if (!transport->conn_lost && !transport->closing) {
                PyErr_SetString(PyExc_OSError,
                                "io_uring receive completed without a buffer");
                st_fatal(transport, "io_uring receive failed");
            }
        } else if (cqe.res == -ENOBUFS) {
            p->metal.provided_buffer_exhaustions++;
        } else if (cqe.res != -ECANCELED) {
            p->metal.receive_errors++;
            if (!transport->conn_lost && !transport->closing) {
                errno = -cqe.res;
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(transport, "io_uring receive failed");
            }
        }

        if (has_buffer && buffer_id < buffers->entries) {
            if (metal_provided_buffer_recycle(buffers, buffer_id) < 0) {
                Py_DECREF(transport);
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
            if (p->metal.diagnostics) {
                p->metal.provided_buffer_recycles++;
            }
        }
        if (!has_more && transport->uring_receive_active &&
            !transport->reading_paused && !transport->closing &&
            !transport->conn_lost) {
            if (rp_submit_receive(p, transport) < 0) {
                Py_DECREF(transport);
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
        }
        Py_DECREF(transport);
    }
    return 0;
}

/* Consume the consecutive run of async SEND CQEs at the CQ head. Each
 * completion releases its operation slot, settles the transport's in-flight
 * payload, and re-pumps that transport's egress queue. */
static int
rp_drain_send_completions(ReactorPoller *p, unsigned budget,
                          unsigned *drained_out)
{
    MetalUring *ring = &p->metal.uring;
    *drained_out = 0;
    while (*drained_out < budget) {
        unsigned head = ring->cached_cq_head;
        struct io_uring_cqe cqe = ring->cqes[head & ring->cq_mask_value];
        if ((cqe.user_data & ~METAL_TOKEN_PAYLOAD_MASK) != METAL_TOKEN_SEND) {
            break;
        }
        ring->cached_cq_head = head + 1;
        (*drained_out)++;
        p->metal.send_completions++;
        uint64_t token = cqe.user_data & METAL_TOKEN_PAYLOAD_MASK;
        uint8_t trace_flags = cqe.res < 0 ? METAL_COMPLETION_ERROR : 0;
        metal_trace_add(&p->metal, METAL_IO_URING, METAL_OP_SEND,
                        token, cqe.res, trace_flags);
        uint32_t index = (uint32_t)token;
        uint32_t generation = (uint32_t)(token >> 32);
        MetalSlot *slot = index < p->metal.operations.capacity
            ? &p->metal.operations.slots[index] : NULL;
        if (slot == NULL || !metal_slot_live(slot) ||
            metal_slot_generation(slot) != generation ||
            metal_slot_kind(slot) != METAL_OP_SEND) {
            p->metal.send_stale++;
            continue;
        }
        SocketTransport *transport = (SocketTransport *)slot->owner;
        metal_slab_release(&p->metal.operations, token, transport);
        st_on_send_complete(transport, cqe.res);
        Py_DECREF(transport);  /* the operation's reference */
    }
    return 0;
}

static int
rp_activate_accepted_connection(ReactorPoller *p, FdEntry *entry, int fd)
{
    PyObject *spec = entry->accept_callback;
    PyObject *protocol_factory = PyTuple_GET_ITEM(spec, 0);
    PyObject *server = PyTuple_GET_ITEM(spec, 1);
    PyObject *socket_factory = PyTuple_GET_ITEM(spec, 2);
    PyObject *fd_object = PyLong_FromLong(fd);
    PyObject *sock = NULL;
    PyObject *protocol = NULL;
    PyObject *transport = NULL;
    if (fd_object == NULL) {
        goto error;
    }
    /* Positional family/type/proto, carried from the listener, and not an
     * optimization of style. `socket(fileno=fd)` alone leaves all three at -1,
     * and CPython then asks the kernel for each: `getsockopt(SO_TYPE)` and
     * `getsockopt(SO_PROTOCOL)` on top of the `getsockname` it always does.
     * An accepted connection inherits all three from its listener, so those
     * two syscalls answer a question already answered at bind time. Measured
     * at 4.21us -> 2.29us per accepted connection. */
    PyObject *call_args[] = {
        PyTuple_GET_ITEM(spec, 3),  /* family */
        PyTuple_GET_ITEM(spec, 4),  /* type */
        PyTuple_GET_ITEM(spec, 5),  /* proto */
        fd_object,
    };
    sock = PyObject_Vectorcall(
        socket_factory, call_args, 3, g_fileno_kwnames);
    if (sock == NULL) {
        goto error;
    }
    /* No setblocking(False) round trip: io_uring accepted with SOCK_NONBLOCK,
     * so the descriptor is already non-blocking and asyncio's per-connection
     * call would only re-set what accept4 set. The invariant lives on the
     * descriptor, not on this object -- `metal_recv`/`metal_send` work the raw
     * fd, and `sock.gettimeout()` is None here exactly as it is on the path
     * that asks the kernel for the type. */
    protocol = PyObject_CallNoArgs(protocol_factory);
    if (protocol == NULL) {
        goto error;
    }
    /* The seventh element is the listener's native TLS context, or None.
     * A TLS connection is constructed without `inline_activate`: the protocol
     * is not told about its transport until the handshake completes, which the
     * transport drives itself. */
    PyObject *tls = PyTuple_GET_ITEM(spec, 6);
    transport = PyObject_CallFunctionObjArgs(
        (PyObject *)&SocketTransportType, p->loop, sock, protocol,
        Py_None, Py_None, server, tls == Py_None ? Py_True : Py_False,
        fd_object, tls, NULL);
    if (transport == NULL) {
        goto error;
    }
    Py_DECREF(transport);
    Py_DECREF(protocol);
    Py_DECREF(sock);
    Py_DECREF(fd_object);
    p->metal.accept_native_activations++;
    return 0;

error:
    Py_XDECREF(transport);
    Py_XDECREF(protocol);
    if (sock == NULL) {
        close(fd);
    }
    Py_XDECREF(sock);
    Py_XDECREF(fd_object);
    rp_report_callback_error(p);
    return 0;
}

static int
rp_drain_accept_completions(ReactorPoller *p, unsigned budget,
                            unsigned *drained_out)
{
    MetalUring *ring = &p->metal.uring;
    *drained_out = 0;
    while (*drained_out < budget) {
        unsigned head = ring->cached_cq_head;
        struct io_uring_cqe cqe = ring->cqes[head & ring->cq_mask_value];
        if (cqe.user_data != METAL_WAKE_TOKEN &&
            cqe.user_data != METAL_SIGNAL_TOKEN &&
            (cqe.user_data & ~METAL_TOKEN_PAYLOAD_MASK) != METAL_TOKEN_ACCEPT) {
            break;  /* another class: the dispatcher re-routes */
        }
        ring->cached_cq_head = head + 1;
        (*drained_out)++;
        if (cqe.user_data == METAL_WAKE_TOKEN) {
            /* Multishot keeps the wake poll armed across wakes: no rearm SQE
             * per cross-thread wake. Downgrade once if the kernel rejects it. */
            if (cqe.res == -EINVAL && p->wake_poll_multishot) {
                p->wake_poll_multishot = 0;
                p->metal.poll_multishot_fallbacks++;
                if (metal_uring_queue_poll(
                        ring, p->wake_fd, POLLIN, METAL_WAKE_TOKEN, 0) < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return -1;
                }
                p->metal.wake_submissions++;
                continue;
            }
            uint64_t value;
            /* native-gil-lint: allow NG002 -- wake_fd is an eventfd created with
             * EFD_NONBLOCK (see rp_init), so this read cannot block: it either
             * consumes the counter or fails EAGAIN. Releasing the GIL for it
             * would cost two state transitions per wakeup on the poll path. */
            while (read(p->wake_fd, &value, sizeof(value)) < 0 &&
                   errno == EINTR) {
                /* Retry only interrupted reads; EAGAIN means fully drained. */
            }
            __atomic_store_n(
                &p->metal.wake_pending, 0, __ATOMIC_RELEASE);
            p->metal.wake_completions++;
            if (!(cqe.flags & IORING_CQE_F_MORE)) {
                if (metal_uring_queue_poll(
                        ring, p->wake_fd, POLLIN, METAL_WAKE_TOKEN,
                        p->wake_poll_multishot) < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return -1;
                }
                p->metal.wake_submissions++;
            }
            continue;
        }
        if (cqe.user_data == METAL_SIGNAL_TOKEN) {
            if (cqe.res == -EINVAL && p->signal_poll_multishot) {
                p->signal_poll_multishot = 0;
                p->metal.poll_multishot_fallbacks++;
                if (metal_uring_queue_poll(
                        ring, p->signal_fd, POLLIN, METAL_SIGNAL_TOKEN,
                        0) < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return -1;
                }
                p->metal.signal_submissions++;
                continue;
            }
            p->metal.signal_completions++;
            if (p->signal_callback == NULL) {
                PyErr_SetString(PyExc_RuntimeError,
                                "signal completion has no callback");
                return -1;
            }
            PyObject *result = PyObject_CallNoArgs(p->signal_callback);
            if (result == NULL) {
                return -1;
            }
            Py_DECREF(result);
            if (!(cqe.flags & IORING_CQE_F_MORE)) {
                if (metal_uring_queue_poll(
                        ring, p->signal_fd, POLLIN, METAL_SIGNAL_TOKEN,
                        p->signal_poll_multishot) < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return -1;
                }
                p->metal.signal_submissions++;
            }
            continue;
        }
        p->metal.accept_completions++;
        uint8_t trace_flags = cqe.res < 0 ? METAL_COMPLETION_ERROR : 0;
        metal_trace_add(&p->metal, METAL_IO_URING, METAL_OP_ACCEPT,
                        cqe.user_data, cqe.res, trace_flags);

        int fd = (int)(uint32_t)cqe.user_data;
        uint32_t generation = (uint32_t)(cqe.user_data >> 32);
        if (fd < 0 || fd >= p->fdcap ||
            p->fds[fd].generation != generation ||
            !p->fds[fd].accept_active) {
            p->metal.accept_stale++;
            if (cqe.res >= 0) {
                close(cqe.res);
            }
            continue;
        }
        FdEntry *entry = &p->fds[fd];
        int has_more = (cqe.flags & IORING_CQE_F_MORE) != 0;
        if (cqe.res == -EINVAL && entry->accept_multishot) {
            entry->accept_multishot = 0;
            p->metal.accept_multishot_fallbacks++;
        } else if (cqe.res >= 0) {
            if (PyTuple_CheckExact(entry->accept_callback)) {
                if (rp_activate_accepted_connection(p, entry, cqe.res) < 0) {
                    return -1;
                }
            } else {
                PyObject *accepted_fd = PyLong_FromLong(cqe.res);
                PyObject *callback = Py_NewRef(entry->accept_callback);
                if (accepted_fd == NULL || callback == NULL) {
                    Py_XDECREF(accepted_fd);
                    Py_XDECREF(callback);
                    close(cqe.res);
                    return -1;
                }
                PyObject *result = PyObject_CallOneArg(callback, accepted_fd);
                Py_DECREF(callback);
                Py_DECREF(accepted_fd);
                if (result == NULL) {
                    close(cqe.res);
                    return -1;
                }
                Py_DECREF(result);
            }
        } else if (cqe.res != -ECANCELED) {
            p->metal.accept_errors++;
        }

        entry = &p->fds[fd];
        if (entry->generation == generation && entry->accept_active && !has_more) {
            if (rp_submit_accept(p, fd) < 0) {
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
        }
    }
    return 0;
}

static int
rp_drain_poll_completions(ReactorPoller *p, unsigned budget,
                          unsigned *drained_out)
{
    MetalUring *ring = &p->metal.uring;
    *drained_out = 0;
    while (*drained_out < budget) {
        unsigned head = ring->cached_cq_head;
        struct io_uring_cqe cqe = ring->cqes[head & ring->cq_mask_value];
        if ((cqe.user_data & METAL_POLL_TOKEN_FLAG) == 0 ||
            (cqe.user_data & ~METAL_TOKEN_PAYLOAD_MASK) !=
                METAL_TOKEN_CONTROL) {
            break;  /* another class: the dispatcher re-routes */
        }
        ring->cached_cq_head = head + 1;
        (*drained_out)++;
        p->metal.poll_completions++;
        int fd = (int)(uint32_t)cqe.user_data;
        uint32_t generation =
            (uint32_t)((cqe.user_data >> 32) &
                       (METAL_POLL_GENERATION_LIMIT - 1));
        if (fd < 0 || fd >= p->fdcap ||
            p->fds[fd].generation != generation ||
            p->fds[fd].mask == 0) {
            p->metal.poll_stale++;
            continue;
        }
        FdEntry *entry = &p->fds[fd];
        if (cqe.res == -EINVAL && entry->poll_multishot) {
            p->metal.poll_multishot_enabled = 0;
            p->metal.poll_multishot_fallbacks++;
            entry->poll_multishot = 0;
            if (metal_uring_queue_poll(
                    ring, fd, entry->mask,
                    rp_poll_token(generation, fd), 0) < 0) {
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
            p->metal.poll_submissions++;
            continue;
        }
        if (cqe.res == -ECANCELED || cqe.res == -ENOENT) {
            continue;
        }
        if (cqe.res < 0) {
            entry->mask = 0;
            errno = -cqe.res;
            PyErr_SetFromErrno(PyExc_OSError);
            return -1;
        }
        int has_more = (cqe.flags & IORING_CQE_F_MORE) != 0;
        uint32_t events = (uint32_t)cqe.res;
        if ((events & (POLLIN | POLLERR | POLLHUP | POLLNVAL)) &&
            entry->reader != NULL) {
            PyObject *callback = Py_NewRef(entry->reader);
            PyObject *callback_args = entry->reader_args
                ? Py_NewRef(entry->reader_args) : NULL;
            p->metal.readiness_callbacks++;
            if (entry->native_reader) {
                rp_dispatch_native_transport(p, callback, 1);
            } else {
                rp_dispatch(p, callback, callback_args);
            }
            Py_XDECREF(callback_args);
            Py_DECREF(callback);
        }
        entry = &p->fds[fd];
        if (entry->generation != generation) {
            p->metal.poll_stale++;
            continue;
        }
        if ((events & (POLLOUT | POLLERR | POLLHUP | POLLNVAL)) &&
            entry->writer != NULL) {
            PyObject *callback = Py_NewRef(entry->writer);
            PyObject *callback_args = entry->writer_args
                ? Py_NewRef(entry->writer_args) : NULL;
            p->metal.readiness_callbacks++;
            if (entry->native_writer) {
                rp_dispatch_native_transport(p, callback, 0);
            } else {
                rp_dispatch(p, callback, callback_args);
            }
            Py_XDECREF(callback_args);
            Py_DECREF(callback);
        }
        entry = &p->fds[fd];
        if (entry->generation == generation && entry->mask != 0 &&
            !has_more) {
            if (metal_uring_queue_poll(
                    ring, fd, entry->mask,
                    rp_poll_token(generation, fd), entry->poll_multishot) < 0) {
                PyErr_SetFromErrno(PyExc_OSError);
                return -1;
            }
            p->metal.poll_submissions++;
        }
    }
    return 0;
}

static int
rp_drain_completions(ReactorPoller *p, unsigned budget)
{
    MetalUring *ring = &p->metal.uring;
    unsigned head = __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
    unsigned tail = __atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE);
    unsigned available = tail - head;
    if (available > budget) {
        available = budget;
    }
    if (available > 0) {
        /* Something ran this turn, so there is plausibly garbage to reclaim.
         * The flag keeps an idle loop from re-collecting a heap it already
         * cleaned on every wakeup. */
        p->gc_dirty = 1;
    }
    ring->cached_cq_head = head;
    int status = 0;
    unsigned drained = 0;
    while (drained < available) {
        uint64_t token = ring->cqes[
            ring->cached_cq_head & ring->cq_mask_value].user_data;
        uint64_t tag = token & ~METAL_TOKEN_PAYLOAD_MASK;
        if (tag == METAL_TOKEN_RECEIVE || token == METAL_CANCEL_TOKEN) {
            /* Consume the consecutive receive run in one pass. The old dispatcher
             * first scanned every token to find the run and then reread every CQE
             * to process it, doubling completion-ring traffic on the hot path. */
            unsigned batch = 0;
            status = rp_drain_receive_completions(
                p, available - drained, &batch);
            drained += batch;
        } else if (tag == METAL_TOKEN_SEND) {
            unsigned batch = 0;
            status = rp_drain_send_completions(
                p, available - drained, &batch);
            drained += batch;
        } else if ((token & METAL_POLL_TOKEN_FLAG) != 0 &&
                   tag == METAL_TOKEN_CONTROL) {
            unsigned batch = 0;
            status = rp_drain_poll_completions(p, available - drained, &batch);
            drained += batch;
        } else if (tag == METAL_TOKEN_ACCEPT || token == METAL_WAKE_TOKEN ||
                   token == METAL_SIGNAL_TOKEN) {
            unsigned batch = 0;
            status = rp_drain_accept_completions(
                p, available - drained, &batch);
            drained += batch;
        } else {
            ring->cached_cq_head++;
            PyErr_SetString(PyExc_RuntimeError,
                            "unknown io_uring completion token class");
            status = -1;
        }
        if (status < 0) {
            break;
        }
    }
    /* Recycled descriptors become visible as one release publication per CQ
     * batch instead of one cache-line handoff per received packet. */
    metal_provided_buffers_publish(&p->metal.receive_buffers);
    __atomic_store_n(
        ring->cq_head, ring->cached_cq_head, __ATOMIC_RELEASE);
    if (ring->diagnostics && ring->cached_cq_head != head) {
        ring->cq_head_publications++;
    }
    return status;
}

static uint64_t
rp_poll_token(uint32_t generation, int fd)
{
    return METAL_TOKEN_CONTROL | METAL_POLL_TOKEN_FLAG |
           ((uint64_t)generation << 32) | (uint32_t)fd;
}

/* May this registration be armed multishot -- that is, does every callback it
 * is about to carry drain the descriptor before returning?
 *
 * A multishot poll reports once per readiness *edge*. `add_reader`'s contract
 * is the opposite: level-triggered, the callback invoked again for as long as
 * the fd stays readable. Stock asyncio readers are written to that contract and
 * consume exactly one message per call -- `_SelectorDatagramTransport`
 * `recvfrom`s once -- so a burst arriving in a single wakeup delivers its head
 * and strands the tail in the socket queue with no edge left to fetch it. That
 * is not a slow path; the data is never read at all until some later packet
 * happens to re-arm the edge, and then it arrives one behind forever.
 *
 * `st_read_ready` is the exception this asks about: it loops to EAGAIN in C, so
 * an edge is all it needs and it keeps the cheaper registration. Everything
 * else -- any Python callback, ours or a third party's -- gets a one-shot poll
 * that the completion path re-arms, which is level-triggered by construction:
 * arming a poll on a descriptor that is still readable completes immediately.
 *
 * Found by HTTP/3, which is the only protocol here that answers one datagram
 * per callback through asyncio's transport rather than through ours. It failed
 * on the metal tier and nowhere else, and it failed silently: the server was
 * up, replying to packet one, and invisible from packet two on. */
static int
rp_entry_drains(const FdEntry *entry, uint32_t want)
{
    if ((want & POLLIN) && entry->reader != NULL && !entry->native_reader) {
        return 0;
    }
    if ((want & POLLOUT) && entry->writer != NULL && !entry->native_writer) {
        return 0;
    }
    return 1;
}

static int
rp_apply(ReactorPoller *p, int fd, uint32_t want, int force)
{
    FdEntry *entry = &p->fds[fd];
    if (entry->mask == want && !force) {
        return 0;
    }
    if (entry->mask != 0) {
        uint64_t old_token = rp_poll_token(entry->generation, fd);
        if (metal_uring_queue_cancel_raw(&p->metal.uring, old_token) < 0) {
            PyErr_SetFromErrno(PyExc_OSError);
            return -1;
        }
        p->metal.poll_cancellations++;
    }
    uint32_t generation = entry->generation + 1;
    if (generation >= METAL_POLL_GENERATION_LIMIT) {
        generation = 1;
        p->generation_wraps++;
    }
    int multishot = p->metal.poll_multishot_enabled && rp_entry_drains(entry, want);
    if (want != 0) {
        uint64_t token = rp_poll_token(generation, fd);
        if (metal_uring_queue_poll(
                &p->metal.uring, fd, want, token, multishot) < 0) {
            PyErr_SetFromErrno(PyExc_OSError);
            return -1;
        }
        p->metal.poll_submissions++;
    }
    entry->generation = generation;
    entry->mask = want;
    entry->poll_multishot = want != 0 && multishot;
    return 0;
}

/* args==NULL means "no positional args" (bound methods with a captured self). */
static PyObject *
rp_pack_args(PyObject *const *args, Py_ssize_t n)
{
    if (n == 0) {
        return NULL;
    }
    PyObject *tuple = PyTuple_New(n);
    if (tuple == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyTuple_SET_ITEM(tuple, i, Py_NewRef(args[i]));
    }
    return tuple;
}

static PyObject *
rp_add_reader(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (nargs < 2) {
        PyErr_SetString(PyExc_TypeError, "_add_reader(fd, callback, *args)");
        return NULL;
    }
    int fd = (int)PyLong_AsLong(args[0]);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    PyObject *cb_args = rp_pack_args(args + 2, nargs - 2);
    if (nargs - 2 > 0 && cb_args == NULL) {
        return NULL;
    }
    FdEntry *e = &p->fds[fd];
    Py_XSETREF(e->reader, Py_NewRef(args[1]));
    Py_XSETREF(e->reader_args, cb_args);
    e->native_reader = PyCFunction_Check(args[1]) &&
        PyCFunction_GET_SELF(args[1]) != NULL &&
        PyObject_TypeCheck(PyCFunction_GET_SELF(args[1]), &SocketTransportType) &&
        PyCFunction_GET_FUNCTION(args[1]) == (PyCFunction)st_read_ready;
    if (rp_apply(p, fd, e->mask | POLLIN, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_add_writer(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (nargs < 2) {
        PyErr_SetString(PyExc_TypeError, "_add_writer(fd, callback, *args)");
        return NULL;
    }
    int fd = (int)PyLong_AsLong(args[0]);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (rp_ensure_fd(p, fd) < 0) {
        return NULL;
    }
    PyObject *cb_args = rp_pack_args(args + 2, nargs - 2);
    if (nargs - 2 > 0 && cb_args == NULL) {
        return NULL;
    }
    FdEntry *e = &p->fds[fd];
    Py_XSETREF(e->writer, Py_NewRef(args[1]));
    Py_XSETREF(e->writer_args, cb_args);
    e->native_writer = PyCFunction_Check(args[1]) &&
        PyCFunction_GET_SELF(args[1]) != NULL &&
        PyObject_TypeCheck(PyCFunction_GET_SELF(args[1]), &SocketTransportType) &&
        PyCFunction_GET_FUNCTION(args[1]) == (PyCFunction)st_write_ready;
    if (rp_apply(p, fd, e->mask | POLLOUT, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
rp_remove_reader(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd < 0 || fd >= p->fdcap || p->fds[fd].reader == NULL) {
        Py_RETURN_FALSE;
    }
    FdEntry *e = &p->fds[fd];
    if (rp_apply(p, fd, e->mask & ~(uint32_t)POLLIN, 0) < 0) {
        return NULL;
    }
    Py_CLEAR(e->reader);
    Py_CLEAR(e->reader_args);
    e->native_reader = 0;
    Py_RETURN_TRUE;
}

static PyObject *
rp_remove_writer(PyObject *op, PyObject *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd = (int)PyLong_AsLong(arg);
    if (fd < 0 && PyErr_Occurred()) {
        return NULL;
    }
    if (fd < 0 || fd >= p->fdcap || p->fds[fd].writer == NULL) {
        Py_RETURN_FALSE;
    }
    FdEntry *e = &p->fds[fd];
    if (rp_apply(p, fd, e->mask & ~(uint32_t)POLLOUT, 0) < 0) {
        return NULL;
    }
    Py_CLEAR(e->writer);
    Py_CLEAR(e->writer_args);
    e->native_writer = 0;
    Py_RETURN_TRUE;
}

static void
rp_report_callback_error(ReactorPoller *p)
{
    /* Swallow into the loop's exception handler (never propagate out of poll). */
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        return;
    }
    PyObject *ctx = PyDict_New();
    PyObject *message = PyUnicode_FromString("Exception in callback");
    if (ctx != NULL && message != NULL &&
        PyDict_SetItemString(ctx, "message", message) == 0 &&
        PyDict_SetItemString(ctx, "exception", exc) == 0) {
        PyObject *hr = PyObject_CallOneArg(p->exc_handler, ctx);
        Py_XDECREF(hr);
    }
    Py_XDECREF(message);
    Py_XDECREF(ctx);
    if (PyErr_Occurred()) {
        PyErr_WriteUnraisable(p->exc_handler);
    }
    Py_DECREF(exc);
}

/* Call a readiness callback; on error route to loop.call_exception_handler,
 * matching asyncio's Handle._run so one bad callback cannot kill the loop. */
static void
rp_dispatch(ReactorPoller *p, PyObject *cb, PyObject *cb_args)
{
    PyObject *r = (cb_args == NULL) ? PyObject_CallNoArgs(cb)
                                    : PyObject_Call(cb, cb_args, NULL);
    if (r != NULL) {
        Py_DECREF(r);
        return;
    }
    rp_report_callback_error(p);
}

static void
rp_dispatch_native_transport(ReactorPoller *p, PyObject *cb, int reader)
{
    PyObject *transport = PyCFunction_GET_SELF(cb);
    PyObject *r;
    if (reader) {
        ((SocketTransport *)transport)->direct_read_dispatches++;
        r = st_read_ready(transport, NULL);
    } else {
        r = st_write_ready(transport, NULL);
    }
    if (r != NULL) {
        Py_DECREF(r);
    } else {
        rp_report_callback_error(p);
    }
}

static int
rp_report_task_step_error(ReactorPoller *p, PyObject *handle, PyObject *callback)
{
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "task step failed without an exception");
        return -1;
    }
    if (PyErr_GivenExceptionMatches(exc, PyExc_SystemExit) ||
            PyErr_GivenExceptionMatches(exc, PyExc_KeyboardInterrupt)) {
        PyErr_SetRaisedException(exc);
        return -1;
    }

    PyObject *message = PyUnicode_FromFormat("Exception in callback %R", callback);
    PyObject *context = message != NULL ? PyDict_New() : NULL;
    if (context == NULL ||
            PyDict_SetItemString(context, "message", message) < 0 ||
            PyDict_SetItemString(context, "exception", exc) < 0 ||
            PyDict_SetItemString(context, "handle", handle) < 0) {
        Py_XDECREF(context);
        Py_XDECREF(message);
        PyErr_SetRaisedException(exc);
        return -1;
    }
    Py_DECREF(message);

    PyObject *source = PyMember_GetOne(
        (const char *)handle, g_handle_source_traceback);
    if (source == NULL) {
        PyErr_Clear();
    } else {
        if (source != Py_None &&
                PyDict_SetItemString(context, "source_traceback", source) < 0) {
            Py_DECREF(source);
            Py_DECREF(context);
            PyErr_SetRaisedException(exc);
            return -1;
        }
        Py_DECREF(source);
    }
    Py_DECREF(exc);
    PyObject *result = PyObject_CallOneArg(p->exc_handler, context);
    Py_DECREF(context);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static PyObject *
rp_handle_object_member(PyObject *handle, const PyMemberDef *member)
{
    PyObject *value = *(PyObject **)((char *)handle + member->offset);
    if (value == NULL) {
        PyErr_Format(PyExc_AttributeError, "Handle.%s is unset", member->name);
        return NULL;
    }
    return Py_NewRef(value);
}

/* asyncio's C Task schedules no-argument TaskStepMethWrapper callbacks. Going
 * through Handle._run adds a Python frame around every suspension/resume. For
 * that exact CPython 3.14 callback shape, enter the captured Context and invoke
 * the C task step directly. Every other Handle retains asyncio's own _run. */
static int
rp_run_task_step(ReactorPoller *p, PyObject *handle)
{
    if (!Py_IS_TYPE(handle, g_handle_type)) {
        return 0;
    }
    PyObject *callback = rp_handle_object_member(handle, g_handle_callback);
    if (callback == NULL) {
        return -1;
    }
    if (g_task_step_type == NULL) {
        if (strcmp(Py_TYPE(callback)->tp_name,
                   "_asyncio.TaskStepMethWrapper") != 0) {
            Py_DECREF(callback);
            return 0;
        }
        g_task_step_type = (PyTypeObject *)Py_NewRef((PyObject *)Py_TYPE(callback));
    } else if (!Py_IS_TYPE(callback, g_task_step_type)) {
        Py_DECREF(callback);
        return 0;
    }
    PyObject *args = rp_handle_object_member(handle, g_handle_args);
    if (args == NULL) {
        Py_DECREF(callback);
        return -1;
    }
    if (!PyTuple_CheckExact(args) || PyTuple_GET_SIZE(args) != 0) {
        Py_DECREF(args);
        Py_DECREF(callback);
        return 0;
    }
    PyObject *context = rp_handle_object_member(handle, g_handle_context);
    if (context == NULL) {
        Py_DECREF(args);
        Py_DECREF(callback);
        return -1;
    }

    PyObject *result = PyObject_CallMethodOneArg(context, g_s_context_run, callback);
    int status;
    if (result != NULL) {
        Py_DECREF(result);
        status = 1;
    } else {
        status = rp_report_task_step_error(p, handle, callback) < 0 ? -1 : 1;
    }
    Py_DECREF(context);
    Py_DECREF(args);
    Py_DECREF(callback);
    return status;
}

static int
rp_flush_async_submissions(ReactorPoller *p)
{
    MetalUring *ring = &p->metal.uring;
    if (ring->pending_submissions == 0) {
        return 0;
    }
    int submitted = metal_uring_flush_submissions(ring);
    if (submitted < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    if (p->metal.diagnostics && submitted > 0) {
        p->metal.submission_batches++;
        p->metal.submitted_sqes += (uint64_t)submitted;
    }
    return 0;
}

static int
rp_has_completion(ReactorPoller *p)
{
    MetalUring *ring = &p->metal.uring;
    return __atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE) !=
           __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED);
}

/* The Handle-free call_soon: enqueue a freelisted C handle onto the shared
 * ready deque. Replaces asyncio's two Python frames + Handle construction
 * per scheduled callback (every Future callback, every Task wakeup). The
 * loop rebinds `loop.call_soon` here; call_soon_threadsafe stays on the
 * base implementation and its asyncio.Handles interleave in the same FIFO. */
static PyObject *
rp_call_soon(PyObject *op, PyObject *const *args, Py_ssize_t nargs,
             PyObject *kwnames)
{
    ReactorPoller *p = (ReactorPoller *)op;
    if (p->closed) {
        PyErr_SetString(PyExc_RuntimeError, "Event loop is closed");
        return NULL;
    }
    PyObject *context = NULL;
    if (kwnames != NULL) {
        Py_ssize_t nkw = PyTuple_GET_SIZE(kwnames);
        for (Py_ssize_t i = 0; i < nkw; i++) {
            PyObject *name = PyTuple_GET_ITEM(kwnames, i);
            if (PyUnicode_CompareWithASCIIString(name, "context") == 0) {
                context = args[nargs + i];
            } else {
                PyErr_Format(PyExc_TypeError,
                             "call_soon() got an unexpected keyword argument "
                             "%R", name);
                return NULL;
            }
        }
    }
    if (nargs < 1) {
        PyErr_SetString(PyExc_TypeError,
                        "call_soon() missing required argument: 'callback'");
        return NULL;
    }
    if (!PyCallable_Check(args[0])) {
        PyErr_Format(PyExc_TypeError,
                     "a callable object was expected by call_soon(), got %R",
                     args[0]);
        return NULL;
    }
    WreathReadyHandle *handle = ready_handle_new(
        args[0], args + 1, nargs - 1, context);
    if (handle == NULL) {
        return NULL;
    }
    PyObject *appended = PyObject_CallOneArg(
        p->ready_append, (PyObject *)handle);
    if (appended == NULL) {
        Py_DECREF(handle);
        return NULL;
    }
    Py_DECREF(appended);
    return (PyObject *)handle;
}

/* Run one fast handle: callback inside its context, straight from C. Returns
 * -1 only for exceptions that must stop the loop (SystemExit/KeyboardInterrupt
 * or a failing exception handler); callback errors route to the loop's
 * exception handler exactly like Handle._run. */
static int
rp_run_ready_handle(ReactorPoller *p, WreathReadyHandle *handle)
{
    if (handle->cancelled) {
        return 0;
    }
    PyObject *result;
    if (handle->args == NULL) {
        result = PyObject_CallMethodOneArg(
            handle->context, g_s_context_run, handle->callback);
    } else {
        Py_ssize_t extra = PyTuple_GET_SIZE(handle->args);
        PyObject *stack_small[8];
        PyObject **stack = stack_small;
        if (extra + 2 > 8) {
            stack = PyMem_Malloc((size_t)(extra + 2) * sizeof(PyObject *));
            if (stack == NULL) {
                PyErr_NoMemory();
                return -1;
            }
        }
        stack[0] = handle->context;
        stack[1] = handle->callback;
        for (Py_ssize_t i = 0; i < extra; i++) {
            stack[2 + i] = PyTuple_GET_ITEM(handle->args, i);
        }
        result = PyObject_VectorcallMethod(
            g_s_context_run, stack, (size_t)(extra + 2), NULL);
        if (stack != stack_small) {
            PyMem_Free(stack);
        }
    }
    if (result != NULL) {
        Py_DECREF(result);
        return 0;
    }
    if (PyErr_ExceptionMatches(PyExc_SystemExit) ||
        PyErr_ExceptionMatches(PyExc_KeyboardInterrupt)) {
        return -1;
    }
    rp_report_callback_error(p);
    return 0;
}

/* --- adaptive spin-then-block ------------------------------------------- */
/* An idle probe predicts the next completion's arrival from an EWMA of past
 * empty-CQ-to-arrival gaps. When arrivals are predicted imminent, a bounded
 * busy-poll skips the blocking io_uring_enter's sleep/wake round trip -- the
 * dominant fixed cost per request on a saturated keep-alive loop. */
#define METAL_SPIN_MIN_SAMPLES 8
#define METAL_SPIN_PREDICT_NS 100000.0   /* spin only under this EWMA */
#define METAL_SPIN_BUDGET_MIN_NS 2000
#define METAL_SPIN_BUDGET_MAX_NS 50000

static void
rp_record_arrival(MetalRuntime *metal, uint64_t gap_ns)
{
    double gap = (double)gap_ns;
    if (metal->arrival_samples == 0) {
        metal->arrival_ewma_ns = gap;
        metal->arrival_deviation_ns = 0.0;
    } else {
        double err = gap - metal->arrival_ewma_ns;
        /* Fast attack toward shorter gaps (load onset), slow release toward
         * longer ones (going idle): mispredicting idle costs one bounded
         * spin, mispredicting load costs latency on every request. */
        metal->arrival_ewma_ns += err * (err < 0.0 ? 0.25 : 0.125);
        metal->arrival_deviation_ns +=
            (fabs(err) - metal->arrival_deviation_ns) * 0.125;
    }
    metal->arrival_samples++;
}

static int
rp_spin_predicted(const MetalRuntime *metal)
{
    return metal->arrival_samples >= METAL_SPIN_MIN_SAMPLES &&
           metal->arrival_ewma_ns < METAL_SPIN_PREDICT_NS;
}

/* Busy-poll for a completion within an EWMA-derived budget. The ring runs
 * with DEFER_TASKRUN, so completions materialize only through io_uring_enter:
 * each probe is a zero-timeout GETEVENTS enter (~submit-free syscall, no
 * sleep, no wakeup IPI) interleaved with pause loops. Returns 1 on arrival,
 * 0 on budget exhaustion, -1 with the exception set. */
static int
rp_spin_for_completions(ReactorPoller *p, uint64_t *spin_ns_out)
{
    MetalRuntime *metal = &p->metal;
    MetalUring *ring = &metal->uring;
    double predicted = metal->arrival_ewma_ns +
                       2.0 * metal->arrival_deviation_ns;
    uint64_t budget = (uint64_t)predicted;
    if (budget < METAL_SPIN_BUDGET_MIN_NS) {
        budget = METAL_SPIN_BUDGET_MIN_NS;
    } else if (budget > METAL_SPIN_BUDGET_MAX_NS) {
        budget = METAL_SPIN_BUDGET_MAX_NS;
    }
    uint64_t start = metal_now_ns();
    uint64_t deadline = start + budget;
    metal->spin_attempts++;
    int hit = 0;
    int failed_errno = 0;
    Py_BEGIN_ALLOW_THREADS
    for (;;) {
        unsigned to_submit = ring->pending_submissions;
        metal_uring_publish_submissions(ring);
        if (ring->diagnostics) {
            ring->enter_calls++;
        }
        int entered = (int)syscall(
            SYS_io_uring_enter, ring->enter_fd, to_submit, 0,
            IORING_ENTER_GETEVENTS | ring->enter_flags, NULL, 0);
        if (entered >= 0 && to_submit != 0) {
            ring->pending_submissions -=
                (unsigned)entered <= ring->pending_submissions
                    ? (unsigned)entered : ring->pending_submissions;
        } else if (entered < 0 && errno != EINTR) {
            failed_errno = errno;
            break;
        }
        if (__atomic_load_n(ring->cq_tail, __ATOMIC_ACQUIRE) !=
            __atomic_load_n(ring->cq_head, __ATOMIC_RELAXED)) {
            hit = 1;
            break;
        }
        if (metal_now_ns() >= deadline) {
            break;
        }
        for (int i = 0; i < 32; i++) {
            metal_cpu_relax();
        }
    }
    Py_END_ALLOW_THREADS
    uint64_t elapsed = metal_now_ns() - start;
    metal->spin_nanoseconds += elapsed;
    *spin_ns_out = elapsed;
    if (failed_errno != 0) {
        errno = failed_errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    if (hit) {
        metal->spin_hits++;
        rp_record_arrival(metal, elapsed);
    } else {
        metal->spin_misses++;
    }
    return hit;
}

/* --- loop-driven cycle collection --------------------------------------- */
/* CPython triggers the cycle collector off an allocation counter, so on a
 * request-serving loop it fires wherever the Nth container allocation happens
 * to land -- which is inside a request batch. That cost is invisible in a
 * throughput average and is exactly what a p99 is made of. The loop knows one
 * thing the allocator cannot: when it is about to stop working. Collecting
 * there costs no request anything.
 *
 * "About to block" is NOT the same as "block_ms is large". A saturated loop
 * still computes a multi-second keep-alive deadline and then returns from the
 * enter immediately, so gating on block_ms would collect on every batch under
 * full load -- the precise opposite of the intent. The gate is the arrival
 * EWMA, the same estimator the adaptive spin reads: it measures how long this
 * loop actually waits between completions. A saturated loop therefore never
 * collects here and falls back to the (raised) automatic threshold, which is
 * why the Python side raises that threshold modestly rather than disabling the
 * collector outright.
 *
 * Returns the collection's cost in nanoseconds, 0 when none ran, or -1 with the
 * exception set. */
static int64_t
rp_collect_idle(ReactorPoller *p, int block_ms)
{
    if (p->gc_collect == NULL || !p->gc_dirty || block_ms == 0) {
        return 0;
    }
    const MetalRuntime *metal = &p->metal;
    if (metal->arrival_samples < METAL_SPIN_MIN_SAMPLES ||
        metal->arrival_ewma_ns < (double)p->gc_idle_ns) {
        return 0;
    }
    /* One request wakes the loop several times -- a receive completion, a send
     * completion, a ready-queue turn -- and each one is genuinely "work ran".
     * Without a floor a lightly loaded server therefore collects several times
     * per request, every time into a young generation it just emptied. The
     * floor bounds that churn without weakening the guarantee: the gap between
     * two collections is idle time either way. */
    uint64_t started = metal_now_ns();
    if (started - p->gc_last_collect_ns < p->gc_min_interval_ns) {
        return 0;
    }
    int full = metal->arrival_ewma_ns >= (double)p->gc_full_idle_ns;
    PyObject *result = full
        ? PyObject_CallNoArgs(p->gc_collect)
        : PyObject_CallOneArg(p->gc_collect, p->gc_young_generation);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    uint64_t finished = metal_now_ns();
    uint64_t elapsed = finished - started;
    p->gc_last_collect_ns = finished;
    p->gc_collect_nanoseconds += elapsed;
    if (full) {
        p->gc_full_collections++;
    } else {
        p->gc_young_collections++;
    }
    p->gc_dirty = 0;
    return (int64_t)elapsed;
}

static PyObject *
rp_run_once(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ReactorPoller *p = (ReactorPoller *)op;
    double loop_now = mono_seconds();
    if (p->wheel != NULL && p->wheel->count > 0 &&
            wreath_wheel_run_due(p->wheel, loop_now) < 0) {
        return NULL;
    }
    int blocked = 0;

    /* --- 1. poll -------------------------------------------------------- */
    /* Non-blocking probe with the GIL held: a saturated server returns work on
     * this call, so the hot path never computes a timeout, never reads the
     * ready deque length, and never touches the timer heap. Only when the probe
     * finds nothing do we compute how long to block and block with the GIL
     * released, so executor threads and signals are never starved. */
    if (rp_has_completion(p) && rp_drain_completions(p, 64) < 0) {
        return NULL;
    }
    {
        Py_ssize_t nready = PyObject_Length(p->ready);
        if (nready < 0) {
            return NULL;
        }
        int block_ms = -1;
        if (nready > 0 || rp_has_completion(p)) {
            block_ms = 0;
        } else {
            double delay = Py_HUGE_VAL;
            if (PyList_GET_SIZE(p->scheduled) > 0) {
                /* Named apart from the `head` borrowed in the due-timer loop
                 * below: that one is taken after this function releases the GIL,
                 * and sharing a name would read -- to a person or to
                 * wreath-native-gil-lint, which is line-based and cannot see C
                 * block scope -- as one borrow carried across the release. */
                PyObject *soonest = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
                PyObject *whenobj = PyObject_GetAttr(soonest, g_s_when);
                if (whenobj == NULL) {
                    return NULL;
                }
                delay = PyFloat_AsDouble(whenobj) - loop_now;
                Py_DECREF(whenobj);
            }
            if (p->wheel != NULL && p->wheel->count > 0) {
                double wheel_delay = wreath_wheel_next_when(p->wheel) - loop_now;
                if (wheel_delay < delay) {
                    delay = wheel_delay;
                }
            }
            if (!isinf(delay)) {
                if (delay <= 0.0) {
                    block_ms = 0;
                } else {
                    double ms = delay * 1000.0;
                    if (ms >= 2147483646.0) {
                        block_ms = 2147483646;
                    } else {
                        block_ms = (int)ms;
                        if ((double)block_ms < ms) {
                            block_ms += 1;
                        }
                    }
                }
            }
        }
        int64_t gc_ns = rp_collect_idle(p, block_ms);
        if (gc_ns < 0) {
            return NULL;
        }
        if (gc_ns > 0) {
            blocked = 1;  /* time advanced; re-read the clock below */
            /* Spend the collection out of the wait rather than on top of it: a
             * deadline computed before the collection must not slip by its
             * cost. */
            if (block_ms > 0) {
                int spent_ms = (int)(gc_ns / 1000000);
                block_ms = spent_ms >= block_ms ? 0 : block_ms - spent_ms;
            }
            /* A finalizer can schedule work. Never sleep on top of it. */
            Py_ssize_t rescheduled = PyObject_Length(p->ready);
            if (rescheduled < 0) {
                return NULL;
            }
            if (rescheduled > 0) {
                block_ms = 0;
            }
        }
        if (block_ms != 0) {
            uint64_t spin_ns = 0;
            int spun = 0;
            if (p->metal.adaptive_polling && rp_spin_predicted(&p->metal)) {
                spun = rp_spin_for_completions(p, &spin_ns);
                if (spun < 0) {
                    return NULL;
                }
                blocked = 1;  /* time advanced; re-read the clock below */
            }
            if (spun == 1) {
                if (rp_drain_completions(p, 64) < 0) {
                    return NULL;
                }
            } else {
                int wait_result;
                unsigned pending_before = p->metal.uring.pending_submissions;
                if (p->metal.diagnostics) {
                    p->metal.blocking_enters++;
                    p->metal.uring_waits++;
                }
                blocked = 1;
                uint64_t wait_start = metal_now_ns();
                Py_BEGIN_ALLOW_THREADS
                wait_result = metal_uring_wait(&p->metal.uring, block_ms);
                Py_END_ALLOW_THREADS
                unsigned submitted = pending_before -
                    p->metal.uring.pending_submissions;
                if (p->metal.diagnostics && submitted > 0) {
                    p->metal.submission_batches++;
                    p->metal.submitted_sqes += submitted;
                }
                if (wait_result < 0) {
                    PyErr_SetFromErrno(PyExc_OSError);
                    return NULL;
                }
                if (rp_has_completion(p)) {
                    /* Sample empty-CQ-to-arrival (spin time included) so the
                     * predictor tracks the live arrival cadence. */
                    rp_record_arrival(
                        &p->metal,
                        (metal_now_ns() - wait_start) + spin_ns);
                    if (rp_drain_completions(p, 64) < 0) {
                        return NULL;
                    }
                } else {
                    if (p->metal.diagnostics) {
                        p->metal.uring_timeouts++;
                    }
                    if (p->metal.trace != NULL) {
                        metal_trace_add(&p->metal, METAL_IO_URING,
                                        METAL_OP_TIMEOUT, 0, 0, 0);
                    }
                }
            }
        } else if (rp_flush_async_submissions(p) < 0) {
            return NULL;
        }
    }

    /* Keep CQ-generated receive rearms poller-local until ready callbacks have
     * run, so response sends and rearms publish in one SQ batch below. */

    /* The wheel is driven by the poll deadline itself: no recurring bridge
     * timer, no idle 1 kHz wakeup, and no Python frame between poll and expiry. */
    if (p->wheel != NULL && p->wheel->count > 0) {
        if (blocked) {
            loop_now = mono_seconds();
        }
        if (wreath_wheel_run_due(p->wheel, loop_now) < 0) {
            return NULL;
        }
    }

    /* --- 4. due timers -> ready ----------------------------------------- */
    double end_time = loop_now + p->clock_res;
    while (PyList_GET_SIZE(p->scheduled) > 0) {
        PyObject *head = PyList_GET_ITEM(p->scheduled, 0);  /* borrowed */
        PyObject *whenobj = PyObject_GetAttr(head, g_s_when);
        if (whenobj == NULL) {
            return NULL;
        }
        double when = PyFloat_AsDouble(whenobj);
        Py_DECREF(whenobj);
        if (when == -1.0 && PyErr_Occurred()) {
            return NULL;
        }
        if (when >= end_time) {
            break;
        }
        PyObject *handle = PyObject_CallOneArg(g_heappop, p->scheduled);
        if (handle == NULL) {
            return NULL;
        }
        if (PyObject_SetAttr(handle, g_s_scheduled, Py_False) < 0) {
            Py_DECREF(handle);
            return NULL;
        }
        PyObject *ar = PyObject_CallOneArg(p->ready_append, handle);
        Py_DECREF(handle);
        if (ar == NULL) {
            return NULL;
        }
        Py_DECREF(ar);
    }

    /* --- 5. drain the ready queue (call_soon + timers, context-correct) -- */
    Py_ssize_t ntodo = PyObject_Length(p->ready);
    if (ntodo < 0) {
        return NULL;
    }
    if (ntodo > 0) {
        p->gc_dirty = 1;
    }
    for (Py_ssize_t i = 0; i < ntodo; i++) {
        PyObject *handle = PyObject_CallNoArgs(p->ready_popleft);
        if (handle == NULL) {
            return NULL;
        }
        if (Py_IS_TYPE(handle, &WreathReadyHandleType)) {
            int status = rp_run_ready_handle(p, (WreathReadyHandle *)handle);
            Py_DECREF(handle);
            if (status < 0) {
                return NULL;
            }
            continue;
        }
        int is_cancelled;
        if (p->direct_task_steps && Py_IS_TYPE(handle, g_handle_type)) {
            PyObject *cancelled = *(PyObject **)(
                (char *)handle + g_handle_cancelled->offset);
            is_cancelled = cancelled == Py_True;
        } else {
            PyObject *cancelled = PyObject_GetAttr(handle, g_s_cancelled);
            if (cancelled == NULL) {
                Py_DECREF(handle);
                return NULL;
            }
            is_cancelled = PyObject_IsTrue(cancelled);
            Py_DECREF(cancelled);
        }
        if (!is_cancelled) {
            int fast = p->direct_task_steps ? rp_run_task_step(p, handle) : 0;
            if (fast < 0) {
                Py_DECREF(handle);
                return NULL;
            }
            if (fast == 0) {
                PyObject *rr = PyObject_CallMethodNoArgs(handle, g_s_run);
                Py_XDECREF(rr);
                if (rr == NULL) {
                    /* Handle._run swallows callback errors itself; a failure here
                     * is a real loop fault -- surface it. */
                    Py_DECREF(handle);
                    return NULL;
                }
            }
        }
        Py_DECREF(handle);
    }
    /* If the ready queue drained, the next run_once publishes pending SQEs in
     * the same io_uring_enter that waits for their completions. */
    Py_RETURN_NONE;
}

static PyObject *
rp_set_signal_reader(PyObject *op, PyObject *args)
{
    ReactorPoller *p = (ReactorPoller *)op;
    int fd;
    PyObject *callback;
    if (!PyArg_ParseTuple(args, "iO:_set_signal_reader", &fd, &callback)) {
        return NULL;
    }
    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "signal reader must be callable");
        return NULL;
    }
    if (p->signal_fd >= 0) {
        PyErr_SetString(PyExc_RuntimeError, "signal reader is already installed");
        return NULL;
    }
    if (metal_uring_queue_poll(
            &p->metal.uring, fd, POLLIN, METAL_SIGNAL_TOKEN,
            p->signal_poll_multishot) < 0) {
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    p->signal_fd = fd;
    p->signal_callback = Py_NewRef(callback);
    p->metal.signal_submissions++;
    Py_RETURN_NONE;
}

/* Hand the poller the collector it drives from its idle gaps, or None to give
 * the heap back to CPython's automatic trigger. Both thresholds are arrival-gap
 * seconds; see rp_collect_idle for why the gate is the arrival estimator rather
 * than the computed block deadline. */
static PyObject *
rp_set_gc_collector(PyObject *op, PyObject *args)
{
    ReactorPoller *p = (ReactorPoller *)op;
    PyObject *collect;
    double idle_seconds;
    double full_idle_seconds;
    double min_interval_seconds;
    if (!PyArg_ParseTuple(args, "Oddd:_set_gc_collector", &collect,
                          &idle_seconds, &full_idle_seconds,
                          &min_interval_seconds)) {
        return NULL;
    }
    if (collect == Py_None) {
        Py_CLEAR(p->gc_collect);
        p->gc_dirty = 0;
        Py_RETURN_NONE;
    }
    if (!PyCallable_Check(collect)) {
        PyErr_SetString(PyExc_TypeError, "gc collector must be callable");
        return NULL;
    }
    if (!(idle_seconds > 0.0) || !(full_idle_seconds >= idle_seconds)) {
        PyErr_SetString(
            PyExc_ValueError,
            "idle threshold must be positive and no greater than the "
            "full-collection threshold");
        return NULL;
    }
    if (!(min_interval_seconds >= 0.0)) {
        PyErr_SetString(PyExc_ValueError,
                        "minimum collection interval must not be negative");
        return NULL;
    }
    if (p->gc_young_generation == NULL) {
        p->gc_young_generation = PyLong_FromLong(0);
        if (p->gc_young_generation == NULL) {
            return NULL;
        }
    }
    p->gc_idle_ns = (uint64_t)(idle_seconds * 1e9);
    p->gc_full_idle_ns = (uint64_t)(full_idle_seconds * 1e9);
    p->gc_min_interval_ns = (uint64_t)(min_interval_seconds * 1e9);
    /* Not 0: the floor is a "time since" comparison, and starting from zero
     * would read as "collected at the epoch" and let the first gap through
     * before the loop has run anything. */
    p->gc_last_collect_ns = metal_now_ns();
    Py_XSETREF(p->gc_collect, Py_NewRef(collect));
    p->gc_dirty = 0;
    Py_RETURN_NONE;
}

static PyObject *
rp_wake(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ReactorPoller *p = (ReactorPoller *)op;
    p->metal.wake_requests++;
    if (__atomic_exchange_n(
            &p->metal.wake_pending, 1, __ATOMIC_ACQ_REL) != 0) {
        p->metal.wake_coalesced++;
        Py_RETURN_NONE;
    }
    uint64_t value = 1;
    ssize_t written;
    do {
        /* native-gil-lint: allow NG002 -- wake_fd is an eventfd created with
         * EFD_NONBLOCK (see rp_init); an 8-byte counter increment cannot block,
         * and the EAGAIN case below is the "already saturated" path, not a
         * would-block wait. This is the call_soon_threadsafe/signal wakeup, so
         * it must stay safe to issue while holding the GIL briefly. */
        written = write(p->wake_fd, &value, sizeof(value));
    } while (written < 0 && errno == EINTR);
    if (written < 0 && errno != EAGAIN) {
        __atomic_store_n(&p->metal.wake_pending, 0, __ATOMIC_RELEASE);
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    p->metal.wake_writes++;
    Py_RETURN_NONE;
}

static PyObject *
rp_close(PyObject *op, PyObject *Py_UNUSED(i))
{
    ReactorPoller *p = (ReactorPoller *)op;
    p->closed = 1;
    /* A closed poller drives nothing; the counters stay readable for a caller
     * inspecting the run it just finished. */
    Py_CLEAR(p->gc_collect);
    if (p->wake_fd >= 0) {
        close(p->wake_fd);
        p->wake_fd = -1;
    }
    metal_send_operations_clear(&p->metal);
    /* Before the ring and the slabs go: a connection still registered here
     * holds this poller's reference, and the ring it would be driven by is
     * about to stop existing. */
    metal_connections_clear(&p->metal);
    metal_provided_buffers_clear(&p->metal.uring,
                                 &p->metal.receive_buffers);
    metal_uring_clear(&p->metal.uring);
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
            Py_CLEAR(p->fds[i].accept_callback);
        }
        PyMem_Free(p->fds);
        p->fds = NULL;
        p->fdcap = 0;
    }
    Py_RETURN_NONE;
}

static int
metal_env_capacity(const char *name, uint32_t default_value,
                   uint32_t maximum, int require_power_of_two,
                   uint32_t *result)
{
    const char *text = getenv(name);
    if (text == NULL || text[0] == '\0') {
        *result = default_value;
        return 0;
    }
    char *end = NULL;
    errno = 0;
    unsigned long long parsed = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' ||
        parsed < METAL_CAPACITY_MIN || parsed > maximum ||
        (require_power_of_two && (parsed & (parsed - 1)) != 0)) {
        PyErr_Format(PyExc_ValueError,
                     "%s must be an integer from %u through %u%s",
                     name, METAL_CAPACITY_MIN, maximum,
                     require_power_of_two ? " and a power of two" : "");
        return -1;
    }
    *result = (uint32_t)parsed;
    return 0;
}

static int
rp_init(PyObject *op, PyObject *args, PyObject *Py_UNUSED(kwds))
{
    ReactorPoller *p = (ReactorPoller *)op;
    p->wake_fd = -1;
    p->signal_fd = -1;
    /* The ring's "no descriptor" sentinel belongs here, beside the other two,
     * not after the capacity checks below. Every early return between this
     * point and `metal_uring_init` still leaves the struct for `rp_dealloc`,
     * and `metal_uring_clear` closes anything `>= 0` -- so a zero here is
     * indistinguishable from a valid stdin and gets closed. */
    p->metal.uring.fd = -1;
    p->metal.uring.enter_fd = -1;
    PyObject *loop, *wheel = Py_None;
    int direct_task_steps = 1;
    unsigned int worker_id = 0;
    unsigned int io_backend = METAL_IO_URING;
    unsigned int adaptive_polling = 1;
    unsigned int diagnostics = 1;
    uint32_t connection_capacity;
    uint32_t operation_capacity;
    uint32_t receive_buffer_count;
    if (!PyArg_ParseTuple(args, "O|OpIIII", &loop, &wheel, &direct_task_steps,
                          &worker_id, &io_backend, &adaptive_polling,
                          &diagnostics)) {
        return -1;
    }
    /* The argument remains in the private constructor ABI so unsupported values
     * fail explicitly, but a live ReactorPoller is always io_uring-backed. */
    if (io_backend != METAL_IO_URING) {
        PyErr_SetString(PyExc_ValueError, "unknown metal I/O backend");
        return -1;
    }
    if (metal_env_capacity(
            "WREATH_METAL_CONNECTION_CAPACITY",
            METAL_CONNECTION_CAPACITY_DEFAULT,
            METAL_CONNECTION_CAPACITY_MAX, 0, &connection_capacity) < 0 ||
        metal_env_capacity(
            "WREATH_METAL_OPERATION_CAPACITY",
            METAL_OPERATION_CAPACITY_DEFAULT,
            METAL_OPERATION_CAPACITY_MAX, 0, &operation_capacity) < 0 ||
        metal_env_capacity(
            "WREATH_METAL_RECV_BUFFERS", METAL_RECV_BUFFER_COUNT_DEFAULT,
            METAL_RECV_BUFFER_COUNT_MAX, 1, &receive_buffer_count) < 0) {
        return -1;
    }
    p->direct_task_steps = direct_task_steps;
    p->closed = 0;
    /* The loop installs a collector explicitly (_set_gc_collector) or not at
     * all; a freshly initialised poller leaves the heap to CPython. */
    Py_CLEAR(p->gc_collect);
    Py_CLEAR(p->gc_young_generation);
    p->gc_idle_ns = 0;
    p->gc_full_idle_ns = 0;
    p->gc_min_interval_ns = 0;
    p->gc_last_collect_ns = 0;
    p->gc_dirty = 0;
    p->gc_young_collections = 0;
    p->gc_full_collections = 0;
    p->gc_collect_nanoseconds = 0;
    p->generation_wraps = 0;
    memset(&p->metal, 0, sizeof(p->metal));
    /* Restore the sentinel the memset just wiped, before the trace allocation
     * and the three slab inits below can return early. */
    p->metal.uring.fd = -1;
    p->metal.uring.enter_fd = -1;
    const char *trace_mode = getenv("WREATH_METAL_TRACE");
    if (trace_mode != NULL && strcmp(trace_mode, "0") != 0) {
        if (strcmp(trace_mode, "1") != 0) {
            PyErr_SetString(PyExc_ValueError,
                            "WREATH_METAL_TRACE must be '0' or '1'");
            return -1;
        }
        p->metal.trace = PyMem_Calloc(
            METAL_TRACE_CAPACITY, sizeof(MetalTraceEntry));
        if (p->metal.trace == NULL) {
            PyErr_NoMemory();
            return -1;
        }
    }
    if (metal_slab_init(&p->metal.connections, connection_capacity) < 0) {
        return -1;
    }
    if (metal_slab_init(&p->metal.operations, operation_capacity) < 0) {
        metal_slab_clear(&p->metal.connections);
        return -1;
    }
    if (metal_slab_init(&p->metal.buffer_descriptors,
                        receive_buffer_count) < 0) {
        metal_slab_clear(&p->metal.operations);
        metal_slab_clear(&p->metal.connections);
        return -1;
    }
    p->metal.submissions = 0;
    p->metal.completions = 0;
    p->metal.cross_worker_rejections = 0;
    p->metal.owner_thread = PyThread_get_thread_ident();
    p->metal.worker_id = worker_id;
    p->metal.adaptive_polling = adaptive_polling != 0;
    p->metal.diagnostics = diagnostics != 0;
    p->wake_poll_multishot = 0;
    p->signal_poll_multishot = 0;
#ifdef IORING_POLL_ADD_MULTI
    p->metal.poll_multishot_enabled = 1;
    p->wake_poll_multishot = 1;
    p->signal_poll_multishot = 1;
#endif
    p->metal.uring.fd = -1;
    if (metal_uring_init(&p->metal.uring, 512) < 0) {
        int saved_errno = errno;
        metal_slab_clear(&p->metal.operations);
        metal_slab_clear(&p->metal.connections);
        errno = saved_errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    p->metal.uring.diagnostics = p->metal.diagnostics;
#ifdef IORING_FEAT_EXT_ARG
    if ((p->metal.uring.features & IORING_FEAT_EXT_ARG) == 0) {
        errno = EOPNOTSUPP;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
#else
    errno = EOPNOTSUPP;
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
#endif
    p->wake_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (p->wake_fd < 0 ||
        metal_uring_queue_poll(
            &p->metal.uring, p->wake_fd, POLLIN, METAL_WAKE_TOKEN,
            p->wake_poll_multishot) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    p->metal.wake_submissions++;
    if (metal_provided_buffers_init(
            &p->metal.uring, &p->metal.receive_buffers,
            &p->metal.buffer_descriptors, (uint16_t)receive_buffer_count,
            METAL_RECV_BUFFER_SIZE, METAL_RECV_BUFFER_GROUP) < 0) {
        p->metal.receive_setup_errno = errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    p->metal.receive_enabled = 1;
    /* Async sends ride the loop's next io_uring_enter -- during a CQ drain
     * batch of N responses that is zero send syscalls instead of N, and the
     * submission side of the enter executes the send before any sleep, so
     * response latency does not move. WREATH_METAL_ASYNC_SEND=0 restores the
     * synchronous path for differential measurement. */
    const char *async_send_mode = getenv("WREATH_METAL_ASYNC_SEND");
    if (async_send_mode != NULL && strcmp(async_send_mode, "0") != 0 &&
        strcmp(async_send_mode, "1") != 0) {
        PyErr_SetString(PyExc_ValueError,
                        "WREATH_METAL_ASYNC_SEND must be '0' or '1'");
        return -1;
    }
    p->metal.send_enabled =
        async_send_mode == NULL || strcmp(async_send_mode, "0") != 0;
    p->fds = NULL;
    p->fdcap = 0;
    p->loop = Py_NewRef(loop);
    p->wheel_obj = Py_NewRef(wheel);
    p->wheel = PyObject_TypeCheck(wheel, &TimingWheelType)
                   ? (TimingWheel *)wheel : NULL;
    p->ready = PyObject_GetAttrString(loop, "_ready");
    p->ready_popleft = p->ready != NULL
        ? PyObject_GetAttr(p->ready, g_s_popleft) : NULL;
    p->ready_append = p->ready != NULL
        ? PyObject_GetAttr(p->ready, g_s_append) : NULL;
    p->scheduled = PyObject_GetAttrString(loop, "_scheduled");
    p->exc_handler = PyObject_GetAttrString(loop, "call_exception_handler");
    if (p->ready == NULL || p->ready_popleft == NULL ||
        p->ready_append == NULL || p->scheduled == NULL ||
        p->exc_handler == NULL) {
        return -1;
    }
    if (!PyList_Check(p->scheduled)) {
        PyErr_SetString(PyExc_TypeError, "loop._scheduled must be a list");
        return -1;
    }
    p->clock_res = 1e-9;
    PyObject *cr = PyObject_GetAttrString(loop, "_clock_resolution");
    if (cr != NULL) {
        p->clock_res = PyFloat_AsDouble(cr);
        Py_DECREF(cr);
        if (p->clock_res < 0 && PyErr_Occurred()) {
            PyErr_Clear();
            p->clock_res = 1e-9;
        }
    } else {
        PyErr_Clear();
    }
    return 0;
}

static int
rp_traverse(PyObject *op, visitproc visit, void *arg)
{
    ReactorPoller *p = (ReactorPoller *)op;
    Py_VISIT(p->loop);
    Py_VISIT(p->ready);
    Py_VISIT(p->ready_popleft);
    Py_VISIT(p->ready_append);
    Py_VISIT(p->scheduled);
    Py_VISIT(p->exc_handler);
    Py_VISIT(p->wheel_obj);
    Py_VISIT(p->signal_callback);
    Py_VISIT(p->gc_collect);
    Py_VISIT(p->gc_young_generation);
    /* The connections this poller owns. Visiting them is what keeps the
     * poller<->transport reference from turning a dead loop with live
     * connections into an uncollectable cycle. */
    for (uint32_t i = 0; i < p->metal.connections.capacity; i++) {
        MetalSlot *slot = &p->metal.connections.slots[i];
        if (metal_slot_live(slot) && slot->owner != NULL) {
            Py_VISIT((PyObject *)slot->owner);
        }
    }
    for (int i = 0; i < p->fdcap; i++) {
        Py_VISIT(p->fds[i].reader);
        Py_VISIT(p->fds[i].reader_args);
        Py_VISIT(p->fds[i].writer);
        Py_VISIT(p->fds[i].writer_args);
        Py_VISIT(p->fds[i].accept_callback);
    }
    return 0;
}

static int
rp_clear(PyObject *op)
{
    ReactorPoller *p = (ReactorPoller *)op;
    Py_CLEAR(p->loop);
    Py_CLEAR(p->ready);
    Py_CLEAR(p->ready_popleft);
    Py_CLEAR(p->ready_append);
    Py_CLEAR(p->scheduled);
    Py_CLEAR(p->exc_handler);
    Py_CLEAR(p->wheel_obj);
    Py_CLEAR(p->signal_callback);
    Py_CLEAR(p->gc_collect);
    Py_CLEAR(p->gc_young_generation);
    if (p->metal.connections.slots != NULL) {
        metal_connections_clear(&p->metal);
    }
    p->signal_fd = -1;
    p->wheel = NULL;
    if (p->fds != NULL) {
        for (int i = 0; i < p->fdcap; i++) {
            Py_CLEAR(p->fds[i].reader);
            Py_CLEAR(p->fds[i].reader_args);
            Py_CLEAR(p->fds[i].writer);
            Py_CLEAR(p->fds[i].writer_args);
            Py_CLEAR(p->fds[i].accept_callback);
        }
    }
    return 0;
}

static void
rp_dealloc(PyObject *op)
{
    ReactorPoller *p = (ReactorPoller *)op;
    PyObject_GC_UnTrack(op);
    if (p->wake_fd >= 0) {
        close(p->wake_fd);
        p->wake_fd = -1;
    }
    rp_clear(op);
    if (p->fds != NULL) {
        PyMem_Free(p->fds);
        p->fds = NULL;
    }
    metal_send_operations_clear(&p->metal);
    metal_provided_buffers_clear(&p->metal.uring,
                                 &p->metal.receive_buffers);
    metal_uring_clear(&p->metal.uring);
    metal_slab_clear(&p->metal.buffer_descriptors);
    metal_slab_clear(&p->metal.operations);
    metal_slab_clear(&p->metal.connections);
    PyMem_Free(p->metal.trace);
    p->metal.trace = NULL;
    Py_TYPE(op)->tp_free(op);
}

static uint64_t
metal_uring_mapped_bytes(const MetalUring *ring)
{
    if (ring->fd < 0) {
        return 0;
    }
    uint64_t total = ring->sqes_size + ring->sq_ring_size;
    if (ring->cq_ring != ring->sq_ring) {
        total += ring->cq_ring_size;
    }
    return total;
}

static PyObject *
rp_get_native_mapped_bytes(PyObject *op, void *closure)
{
    (void)closure;
    ReactorPoller *p = (ReactorPoller *)op;
    uint64_t total = metal_uring_mapped_bytes(&p->metal.uring) +
                     p->metal.receive_buffers.ring_size +
                     p->metal.receive_buffers.data_size;
    return PyLong_FromUnsignedLongLong(total);
}

static PyObject *
rp_get_native_heap_bytes(PyObject *op, void *closure)
{
    (void)closure;
    ReactorPoller *p = (ReactorPoller *)op;
    uint64_t total =
        (uint64_t)p->metal.connections.capacity * sizeof(MetalSlot) +
        (uint64_t)p->metal.operations.capacity * sizeof(MetalSlot) +
        (uint64_t)p->metal.buffer_descriptors.capacity * sizeof(MetalSlot) +
        (uint64_t)p->metal.receive_buffers.entries * sizeof(uint64_t) +
        (uint64_t)p->fdcap * sizeof(FdEntry);
    if (p->metal.trace != NULL) {
        total += METAL_TRACE_CAPACITY * sizeof(MetalTraceEntry);
    }
    return PyLong_FromUnsignedLongLong(total);
}

static PyObject *
rp_get_native_ring_count(PyObject *op, void *closure)
{
    (void)closure;
    ReactorPoller *p = (ReactorPoller *)op;
    unsigned count = p->metal.uring.fd >= 0;
    return PyLong_FromUnsignedLong(count);
}

static PyObject *
rp_get_registered_ring_fd(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(
        ((ReactorPoller *)op)->metal.uring.registered_ring_fd);
}

static PyObject *
rp_get_epoll_active(PyObject *op, void *closure)
{
    (void)closure;
    (void)op;
    Py_RETURN_FALSE;
}

static PyObject *
rp_get_stale_events(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.poll_stale);
}

static PyObject *
rp_get_generation_wraps(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(((ReactorPoller *)op)->generation_wraps);
}

static PyObject *
rp_get_accept_submissions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_submissions);
}

static PyObject *
rp_get_accept_completions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_completions);
}

static PyObject *
rp_get_accept_native_activations(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_native_activations);
}

static PyObject *
rp_get_accept_multishot_fallbacks(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.accept_multishot_fallbacks);
}

static PyObject *
rp_get_receive_enabled(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(((ReactorPoller *)op)->metal.receive_enabled);
}

static PyObject *
rp_get_receive_submissions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_submissions);
}

static PyObject *
rp_get_receive_completions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_completions);
}

static PyObject *
rp_get_receive_multishot_fallbacks(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.receive_multishot_fallbacks);
}

static PyObject *
rp_get_provided_buffer_recycles(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.provided_buffer_recycles);
}

static PyObject *
rp_get_provided_buffer_count(PyObject *op, void *closure)
{
    (void)closure;
    ReactorPoller *p = (ReactorPoller *)op;
    return PyLong_FromUnsignedLong(
        p->metal.receive_enabled ? p->metal.receive_buffers.entries : 0);
}

static PyObject *
rp_get_send_enabled(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(((ReactorPoller *)op)->metal.send_enabled);
}

static PyObject *
rp_get_send_submissions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.send_submissions);
}

static PyObject *
rp_get_send_completions(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.send_completions);
}

static PyObject *
rp_get_sq_tail_publications(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.uring.sq_tail_publications);
}

static PyObject *
rp_get_cq_head_publications(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.uring.cq_head_publications);
}

static PyObject *
rp_get_enter_calls(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->metal.uring.enter_calls);
}

#define RP_METAL_U64_GETTER(name, field) \
    static PyObject *name(PyObject *op, void *closure) \
    { \
        (void)closure; \
        return PyLong_FromUnsignedLongLong( \
            ((ReactorPoller *)op)->metal.field); \
    }

RP_METAL_U64_GETTER(rp_get_submission_batches, submission_batches)
RP_METAL_U64_GETTER(rp_get_submitted_sqes, submitted_sqes)
RP_METAL_U64_GETTER(rp_get_blocking_enters, blocking_enters)
RP_METAL_U64_GETTER(rp_get_uring_waits, uring_waits)
RP_METAL_U64_GETTER(rp_get_uring_timeouts, uring_timeouts)
RP_METAL_U64_GETTER(rp_get_poll_submissions, poll_submissions)
RP_METAL_U64_GETTER(rp_get_poll_completions, poll_completions)
RP_METAL_U64_GETTER(rp_get_poll_cancellations, poll_cancellations)
RP_METAL_U64_GETTER(rp_get_poll_stale, poll_stale)
RP_METAL_U64_GETTER(rp_get_poll_multishot_fallbacks,
                    poll_multishot_fallbacks)
RP_METAL_U64_GETTER(rp_get_readiness_callbacks, readiness_callbacks)
RP_METAL_U64_GETTER(rp_get_spin_attempts, spin_attempts)
RP_METAL_U64_GETTER(rp_get_spin_hits, spin_hits)
RP_METAL_U64_GETTER(rp_get_spin_misses, spin_misses)
RP_METAL_U64_GETTER(rp_get_spin_nanoseconds, spin_nanoseconds)
RP_METAL_U64_GETTER(rp_get_arrival_samples, arrival_samples)
RP_METAL_U64_GETTER(rp_get_provided_buffer_exhaustions,
                    provided_buffer_exhaustions)
RP_METAL_U64_GETTER(rp_get_direct_receive_completions,
                    direct_receive_completions)
RP_METAL_U64_GETTER(rp_get_direct_receive_bytes, direct_receive_bytes)
RP_METAL_U64_GETTER(rp_get_wake_requests, wake_requests)
RP_METAL_U64_GETTER(rp_get_wake_submissions, wake_submissions)
RP_METAL_U64_GETTER(rp_get_wake_completions, wake_completions)
RP_METAL_U64_GETTER(rp_get_wake_writes, wake_writes)
RP_METAL_U64_GETTER(rp_get_wake_coalesced, wake_coalesced)
RP_METAL_U64_GETTER(rp_get_signal_submissions, signal_submissions)
RP_METAL_U64_GETTER(rp_get_signal_completions, signal_completions)
RP_METAL_U64_GETTER(rp_get_send_zc_notifications, send_zc_notifications)
RP_METAL_U64_GETTER(rp_get_send_zc_copied, send_zc_copied)
RP_METAL_U64_GETTER(rp_get_send_zc_bytes, send_zc_bytes)
RP_METAL_U64_GETTER(rp_get_send_copy_bytes, send_copy_bytes)
RP_METAL_U64_GETTER(rp_get_retained_send_enqueues, retained_send_enqueues)

#undef RP_METAL_U64_GETTER

#define RP_BUFFER_SLAB_GETTER(name, field) \
    static PyObject *name(PyObject *op, void *closure) \
    { \
        (void)closure; \
        return PyLong_FromUnsignedLongLong( \
            ((ReactorPoller *)op)->metal.buffer_descriptors.field); \
    }

RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_capacity, capacity)
RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_occupancy, occupancy)
RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_high_water, high_water)
RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_exhaustions, exhaustions)
RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_stale, stale)
RP_BUFFER_SLAB_GETTER(rp_get_buffer_descriptor_generation_wraps,
                      generation_wraps)

#undef RP_BUFFER_SLAB_GETTER

static PyObject *
rp_get_connection_capacity(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLong(
        ((ReactorPoller *)op)->metal.connections.capacity);
}

static PyObject *
rp_get_operation_capacity(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLong(
        ((ReactorPoller *)op)->metal.operations.capacity);
}

static PyObject *
rp_get_poll_multishot_enabled(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(
        ((ReactorPoller *)op)->metal.poll_multishot_enabled);
}

static PyObject *
rp_get_adaptive_polling(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(((ReactorPoller *)op)->metal.adaptive_polling);
}

static PyObject *
rp_get_arrival_ewma_ns(PyObject *op, void *closure)
{
    (void)closure;
    return PyFloat_FromDouble(((ReactorPoller *)op)->metal.arrival_ewma_ns);
}

static PyObject *
rp_get_arrival_deviation_ns(PyObject *op, void *closure)
{
    (void)closure;
    return PyFloat_FromDouble(
        ((ReactorPoller *)op)->metal.arrival_deviation_ns);
}

static PyObject *
rp_get_gc_loop_driven(PyObject *op, void *closure)
{
    (void)closure;
    return PyBool_FromLong(((ReactorPoller *)op)->gc_collect != NULL);
}

static PyObject *
rp_get_gc_young_collections(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->gc_young_collections);
}

static PyObject *
rp_get_gc_full_collections(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->gc_full_collections);
}

static PyObject *
rp_get_gc_collect_nanoseconds(PyObject *op, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(
        ((ReactorPoller *)op)->gc_collect_nanoseconds);
}

static PyObject *
rp_completion_trace(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ReactorPoller *p = (ReactorPoller *)op;
    PyObject *rows = PyList_New(p->metal.trace_count);
    if (rows == NULL) {
        return NULL;
    }
    uint64_t first = p->metal.trace_sequence - p->metal.trace_count + 1;
    for (uint32_t offset = 0; offset < p->metal.trace_count; offset++) {
        uint64_t sequence = first + offset;
        MetalTraceEntry *entry = &p->metal.trace[
            (uint32_t)((sequence - 1) % METAL_TRACE_CAPACITY)];
        PyObject *row = Py_BuildValue(
            "(KKiiii)", entry->sequence, entry->token, entry->result,
            (int)entry->kind, (int)entry->backend, (int)entry->flags);
        if (row == NULL) {
            Py_DECREF(rows);
            return NULL;
        }
        PyList_SET_ITEM(rows, offset, row);
    }
    return rows;
}

static PyGetSetDef rp_getset[] = {
    {"native_mapped_bytes", rp_get_native_mapped_bytes, NULL,
     PyDoc_STR("Metal-owned mmap capacity in bytes."), NULL},
    {"native_heap_bytes", rp_get_native_heap_bytes, NULL,
     PyDoc_STR("Metal-owned fixed heap capacity in bytes."), NULL},
    {"native_ring_count", rp_get_native_ring_count, NULL,
     PyDoc_STR("Live io_uring instances owned by this worker."), NULL},
    {"registered_ring_fd", rp_get_registered_ring_fd, NULL,
     PyDoc_STR("Whether io_uring_enter uses a registered ring index."), NULL},
    {"epoll_active", rp_get_epoll_active, NULL,
     PyDoc_STR("Always false; metal owns no epoll instance."), NULL},
    {"stale_events", rp_get_stale_events, NULL,
     PyDoc_STR("Readiness records rejected after registration replacement."), NULL},
    {"generation_wraps", rp_get_generation_wraps, NULL,
     PyDoc_STR("32-bit registration generation wraps."), NULL},
    {"accept_submissions", rp_get_accept_submissions, NULL,
     PyDoc_STR("io_uring accept SQEs submitted."), NULL},
    {"accept_completions", rp_get_accept_completions, NULL,
     PyDoc_STR("io_uring accept CQEs drained."), NULL},
    {"accept_native_activations", rp_get_accept_native_activations, NULL,
     PyDoc_STR("Accepted connections activated directly from the CQ."), NULL},
    {"accept_multishot_fallbacks", rp_get_accept_multishot_fallbacks, NULL,
     PyDoc_STR("Listeners downgraded to ordinary accept SQEs."), NULL},
    {"receive_enabled", rp_get_receive_enabled, NULL,
     PyDoc_STR("Whether provided-buffer receive is active."), NULL},
    {"receive_submissions", rp_get_receive_submissions, NULL,
     PyDoc_STR("Receive SQEs submitted."), NULL},
    {"receive_completions", rp_get_receive_completions, NULL,
     PyDoc_STR("Receive CQEs drained."), NULL},
    {"receive_multishot_fallbacks", rp_get_receive_multishot_fallbacks, NULL,
     PyDoc_STR("Receivers downgraded to one-shot SQEs."), NULL},
    {"provided_buffer_recycles", rp_get_provided_buffer_recycles, NULL,
     PyDoc_STR("Consumed provided buffers returned to the ring."), NULL},
    {"provided_buffer_exhaustions", rp_get_provided_buffer_exhaustions, NULL,
     PyDoc_STR("Receive epochs stopped by provided-buffer exhaustion."), NULL},
    {"direct_receive_completions", rp_get_direct_receive_completions, NULL,
     PyDoc_STR("HTTP/1 receives committed directly into parser storage."), NULL},
    {"direct_receive_bytes", rp_get_direct_receive_bytes, NULL,
     PyDoc_STR("Bytes received directly into HTTP/1 parser storage."), NULL},
    {"provided_buffer_count", rp_get_provided_buffer_count, NULL,
     PyDoc_STR("Registered receive buffer count."), NULL},
    {"connection_capacity", rp_get_connection_capacity, NULL,
     PyDoc_STR("Configured per-worker connection slab capacity."), NULL},
    {"operation_capacity", rp_get_operation_capacity, NULL,
     PyDoc_STR("Configured per-worker operation slab capacity."), NULL},
    {"buffer_descriptor_capacity", rp_get_buffer_descriptor_capacity, NULL,
     PyDoc_STR("Configured provided-buffer descriptor capacity."), NULL},
    {"buffer_descriptor_occupancy", rp_get_buffer_descriptor_occupancy, NULL,
     PyDoc_STR("Live generational provided-buffer descriptors."), NULL},
    {"buffer_descriptor_high_water", rp_get_buffer_descriptor_high_water, NULL,
     PyDoc_STR("Provided-buffer descriptor high-water occupancy."), NULL},
    {"buffer_descriptor_exhaustions", rp_get_buffer_descriptor_exhaustions,
     NULL, PyDoc_STR("Provided-buffer descriptor slab exhaustions."), NULL},
    {"buffer_descriptor_stale", rp_get_buffer_descriptor_stale, NULL,
     PyDoc_STR("Rejected stale provided-buffer descriptor tokens."), NULL},
    {"buffer_descriptor_generation_wraps",
     rp_get_buffer_descriptor_generation_wraps, NULL,
     PyDoc_STR("Provided-buffer descriptor generation wraps."), NULL},
    {"send_enabled", rp_get_send_enabled, NULL,
     PyDoc_STR("Whether asynchronous send completion is active."), NULL},
    {"send_submissions", rp_get_send_submissions, NULL,
     PyDoc_STR("Send SQEs submitted, including partial retries."), NULL},
    {"send_completions", rp_get_send_completions, NULL,
     PyDoc_STR("Send CQEs drained."), NULL},
    {"send_zc_notifications", rp_get_send_zc_notifications, NULL,
     PyDoc_STR("Zero-copy ownership notification CQEs."), NULL},
    {"send_zc_copied", rp_get_send_zc_copied, NULL,
     PyDoc_STR("SEND_ZC completions requiring no notification."), NULL},
    {"send_zc_bytes", rp_get_send_zc_bytes, NULL,
     PyDoc_STR("Bytes submitted through SEND_ZC."), NULL},
    {"send_copy_bytes", rp_get_send_copy_bytes, NULL,
     PyDoc_STR("Bytes submitted through ordinary async send."), NULL},
    {"retained_send_enqueues", rp_get_retained_send_enqueues, NULL,
     PyDoc_STR("Immutable payloads retained behind an active send."), NULL},
    {"submission_batches", rp_get_submission_batches, NULL,
     PyDoc_STR("io_uring submission-enter batches."), NULL},
    {"submitted_sqes", rp_get_submitted_sqes, NULL,
     PyDoc_STR("SQEs accepted by io_uring_enter."), NULL},
    {"sq_tail_publications", rp_get_sq_tail_publications, NULL,
     PyDoc_STR("Batched SQ tail release publications."), NULL},
    {"cq_head_publications", rp_get_cq_head_publications, NULL,
     PyDoc_STR("Batched CQ head release publications."), NULL},
    {"enter_calls", rp_get_enter_calls, NULL,
     PyDoc_STR("io_uring_enter syscalls, including interrupted retries."), NULL},
    {"adaptive_polling", rp_get_adaptive_polling, NULL,
     PyDoc_STR("Whether adaptive spin-then-block is enabled."), NULL},
    {"blocking_enters", rp_get_blocking_enters, NULL,
     PyDoc_STR("Blocking native waits after an empty probe."), NULL},
    {"uring_waits", rp_get_uring_waits, NULL,
     PyDoc_STR("Blocking io_uring_enter waits."), NULL},
    {"uring_timeouts", rp_get_uring_timeouts, NULL,
     PyDoc_STR("io_uring waits completed by their deadline."), NULL},
    {"poll_submissions", rp_get_poll_submissions, NULL,
     PyDoc_STR("Generic readiness poll SQEs submitted."), NULL},
    {"poll_completions", rp_get_poll_completions, NULL,
     PyDoc_STR("Generic readiness poll CQEs drained."), NULL},
    {"poll_cancellations", rp_get_poll_cancellations, NULL,
     PyDoc_STR("Generic readiness poll cancellation SQEs submitted."), NULL},
    {"poll_stale", rp_get_poll_stale, NULL,
     PyDoc_STR("Stale generic readiness CQEs rejected."), NULL},
    {"poll_multishot_enabled", rp_get_poll_multishot_enabled, NULL,
     PyDoc_STR("Whether generic readiness uses multishot poll."), NULL},
    {"poll_multishot_fallbacks", rp_get_poll_multishot_fallbacks, NULL,
     PyDoc_STR("Multishot polls downgraded after kernel rejection."), NULL},
    {"readiness_callbacks", rp_get_readiness_callbacks, NULL,
     PyDoc_STR("Generic readiness callbacks dispatched directly from the CQ."), NULL},
    {"wake_requests", rp_get_wake_requests, NULL,
     PyDoc_STR("Cross-thread native wake requests."), NULL},
    {"wake_submissions", rp_get_wake_submissions, NULL,
     PyDoc_STR("io_uring wake-poll submissions."), NULL},
    {"wake_completions", rp_get_wake_completions, NULL,
     PyDoc_STR("io_uring wake-poll completions."), NULL},
    {"wake_writes", rp_get_wake_writes, NULL,
     PyDoc_STR("eventfd writes after wake coalescing."), NULL},
    {"wake_coalesced", rp_get_wake_coalesced, NULL,
     PyDoc_STR("Wake requests covered by an already-pending eventfd signal."), NULL},
    {"signal_submissions", rp_get_signal_submissions, NULL,
     PyDoc_STR("io_uring signal-socket poll submissions."), NULL},
    {"signal_completions", rp_get_signal_completions, NULL,
     PyDoc_STR("io_uring signal-socket poll completions."), NULL},
    {"spin_attempts", rp_get_spin_attempts, NULL,
     PyDoc_STR("Adaptive userspace spin epochs."), NULL},
    {"spin_hits", rp_get_spin_hits, NULL,
     PyDoc_STR("Spin epochs observing a CQ arrival."), NULL},
    {"spin_misses", rp_get_spin_misses, NULL,
     PyDoc_STR("Spin epochs falling through to blocking."), NULL},
    {"spin_nanoseconds", rp_get_spin_nanoseconds, NULL,
     PyDoc_STR("Total measured userspace spin time."), NULL},
    {"arrival_samples", rp_get_arrival_samples, NULL,
     PyDoc_STR("Empty-CQ-to-arrival samples."), NULL},
    {"arrival_ewma_ns", rp_get_arrival_ewma_ns, NULL,
     PyDoc_STR("EWMA empty-CQ-to-arrival nanoseconds."), NULL},
    {"arrival_deviation_ns", rp_get_arrival_deviation_ns, NULL,
     PyDoc_STR("EWMA absolute arrival-gap deviation."), NULL},
    {"gc_loop_driven", rp_get_gc_loop_driven, NULL,
     PyDoc_STR("Whether the loop drives cycle collection from its idle gaps."),
     NULL},
    {"gc_young_collections", rp_get_gc_young_collections, NULL,
     PyDoc_STR("Young-generation collections run in an idle gap."), NULL},
    {"gc_full_collections", rp_get_gc_full_collections, NULL,
     PyDoc_STR("Full collections run in an idle gap."), NULL},
    {"gc_collect_nanoseconds", rp_get_gc_collect_nanoseconds, NULL,
     PyDoc_STR("Total measured time spent in loop-driven collection."), NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef rp_methods[] = {
    {"_add_reader", (PyCFunction)(void (*)(void))rp_add_reader, METH_FASTCALL, NULL},
    {"_add_writer", (PyCFunction)(void (*)(void))rp_add_writer, METH_FASTCALL, NULL},
    {"_remove_reader", rp_remove_reader, METH_O, NULL},
    {"_remove_writer", rp_remove_writer, METH_O, NULL},
    {"_add_uring_listener", rp_add_uring_listener, METH_VARARGS, NULL},
    {"_remove_uring_listener", rp_remove_uring_listener, METH_O, NULL},
    {"_start_uring_receive", rp_start_uring_receive, METH_O, NULL},
    {"_stop_uring_receive", rp_stop_uring_receive, METH_O, NULL},
    {"_run_once", rp_run_once, METH_NOARGS, NULL},
    {"_call_soon", (PyCFunction)(void (*)(void))rp_call_soon,
     METH_FASTCALL | METH_KEYWORDS, NULL},
    {"_set_signal_reader", rp_set_signal_reader, METH_VARARGS, NULL},
    {"_set_gc_collector", rp_set_gc_collector, METH_VARARGS, NULL},
    {"_wake", rp_wake, METH_NOARGS, NULL},
    {"completion_trace", rp_completion_trace, METH_NOARGS, NULL},
    {"close", rp_close, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

/* `PyType_GenericAlloc` zero-fills the object, and zero is a valid descriptor.
 * A poller built with `__new__` and never initialised would therefore reach
 * `rp_dealloc` carrying `metal.uring.fd == 0` and close the process's stdin.
 * Stamping the sentinels here makes "no descriptor" mean -1 from the moment the
 * object exists, so `rp_init` is the only thing that can install a real one. */
static PyObject *
rp_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    PyObject *self = PyType_GenericNew(type, args, kwds);
    if (self == NULL) {
        return NULL;
    }
    ReactorPoller *p = (ReactorPoller *)self;
    p->wake_fd = -1;
    p->signal_fd = -1;
    p->metal.uring.fd = -1;
    p->metal.uring.enter_fd = -1;
    return self;
}

static PyTypeObject ReactorPollerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.ReactorPoller",
    .tp_basicsize = sizeof(ReactorPoller),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = rp_dealloc,
    .tp_traverse = rp_traverse,
    .tp_clear = rp_clear,
    .tp_methods = rp_methods,
    .tp_getset = rp_getset,
    .tp_init = rp_init,
    .tp_new = rp_new,
};

static WreathTransportCAPI transport_capi = {
    WREATH_TRANSPORT_CAPI_VERSION,
    transport_capi_check,
    transport_capi_write,
    transport_capi_writelines,
};
