/* Native plaintext socket transport. */

/* Registered stream-fusion capsules, probed in order at protocol-bind time.
 * Resolution reads sys.modules only (no import machinery): by the time a
 * protocol instance reaches bind, its defining module is definitionally
 * imported, and a module that is absent simply has not produced any protocol
 * yet. Resolved pointers are cached for the life of the process. */
typedef struct {
    const char *module_name;   /* sys.modules key; also the _fused_stream id */
    const char *attribute;     /* capsule attribute on the module */
    const char *capsule_name;  /* full dotted capsule name */
    const WreathStreamCAPI *capi;  /* resolved lazily; NULL until seen */
} StreamCapiEntry;

static StreamCapiEntry g_stream_capis[] = {
    {"wreath._native._server", "_HTTP1_C_API", WREATH_HTTP1_CAPI_NAME, NULL},
    {"wreath._native._server", "_HTTP2_C_API", WREATH_HTTP2_CAPI_NAME, NULL},
    {"wreath._native._postgres", "_STREAM_C_API",
     "wreath._native._postgres._STREAM_C_API", NULL},
    {"wreath._native._client", "_STREAM_C_API",
     "wreath._native._client._STREAM_C_API", NULL},
};
#define STREAM_CAPI_COUNT \
    (Py_ssize_t)(sizeof(g_stream_capis) / sizeof(g_stream_capis[0]))

static const WreathStreamCAPI *
stream_capi_resolve(StreamCapiEntry *entry)
{
    if (entry->capi != NULL) {
        return entry->capi;
    }
    PyObject *modules = PyImport_GetModuleDict();  /* borrowed */
    PyObject *module = modules == NULL
        ? NULL : PyDict_GetItemString(modules, entry->module_name);
    if (module == NULL) {
        return NULL;  /* not imported yet; retried on a later bind */
    }
    PyObject *capsule = PyObject_GetAttrString(module, entry->attribute);
    if (capsule == NULL) {
        PyErr_Clear();
        return NULL;
    }
    const WreathStreamCAPI *capi =
        PyCapsule_GetPointer(capsule, entry->capsule_name);
    Py_DECREF(capsule);
    if (capi == NULL) {
        PyErr_Clear();
        return NULL;
    }
    if (capi->version != WREATH_STREAM_CAPI_VERSION) {
        return NULL;
    }
    entry->capi = capi;
    return capi;
}

typedef struct {
    PyObject_HEAD
    PyObject *loop;
    PyObject *sock;             /* the Python socket (kept alive; closed on lost) */
    PyObject *protocol;
    PyObject *server;           /* AbstractServer, or None */
    PyObject *extra;            /* get_extra_info dict */
    int fd;
    int buffered;               /* protocol is a BufferedProtocol */
    const WreathStreamCAPI *fused;  /* stream C API for direct ingress, or NULL */
    const char *fused_module;   /* static module name of the fused capsule */
    /* cached bound methods */
    PyObject *m_add_reader, *m_remove_reader, *m_add_writer, *m_remove_writer;
    PyObject *m_call_soon;
    PyObject *proto_get_buffer, *proto_buffer_updated, *proto_data_received;
    PyObject *read_ready, *write_ready, *conn_lost_cb;  /* our own bound methods */
    /* write buffer: contiguous bytearray with an advancing head */
    PyObject *wbuf;
    PyObject *cork_obj;          /* retained exact bytes; sent directly at flush */
    Py_ssize_t whead;
    int writing;                /* writer registered */
    int cork;                   /* buffer writes during synchronous request drive */
    /* Keep request-hot flow/lifecycle state adjacent to the write state. */
    /* flow control + lifecycle */
    Py_ssize_t high_water, low_water;
    int protocol_paused;
    int reading_paused;
    int closing;
    int conn_lost;
    int eof;
    int protocol_connected;
    PyObject *poller_obj;        /* owns the per-worker MetalRuntime */
    MetalRuntime *metal;         /* borrowed from poller_obj */
    uint64_t connection_token;
    uint64_t uring_receive_token;
    int uring_receive_active;
    int uring_receive_multishot;
    /* Ordered egress of retained immutable payloads. `send_obj` is the payload
     * currently leaving (async SEND in flight when send_op_token != 0, or a
     * synchronous partial), `send_queue` holds the payloads behind it, and the
     * write buffer drains only after every retained payload has left. */
    PyObject *send_queue;        /* PyList FIFO, lazily created */
    Py_ssize_t send_queue_head;
    PyObject *empty_waiter;      /* future resolved once nothing is pending */
    PyObject *send_obj;          /* exact-bytes payload being sent */
    Py_ssize_t send_obj_off;     /* bytes of send_obj already accepted */
    Py_ssize_t send_queued_bytes; /* unsent bytes across send_obj + queue */
    uint64_t send_op_token;      /* live async SEND operation, 0 when idle */
    /* Cold diagnostics live after request-hot transport state. */
    Py_ssize_t direct_writelines;
    Py_ssize_t direct_read_dispatches;
    Py_ssize_t direct_protocol_writes;
    Py_ssize_t zero_copy_cork_writes;
} SocketTransport;

static PyTypeObject SocketTransportType;
static int metal_attach_transport(SocketTransport *, PyObject *);
static void metal_detach_transport(SocketTransport *);
static int rp_start_uring_receive_native(SocketTransport *);
static int rp_stop_uring_receive_native(SocketTransport *);
static void st_pump_egress(SocketTransport *);
static void st_egress_epilogue(SocketTransport *);
static void st_fatal(SocketTransport *, const char *);
static void st_maybe_resume(SocketTransport *);
static int st_call_soon(SocketTransport *, PyObject *, PyObject *);

static ssize_t
metal_recv(SocketTransport *t, void *buffer, size_t size)
{
    if (t->metal != NULL) {
        /* Metal ingress is completion-driven into provided buffers; the poll
         * readiness path must never issue a competing synchronous read. */
        errno = EOPNOTSUPP;
        return -1;
    }
    ssize_t result;
    do {
        /* native-gil-lint: allow NG002 -- t->fd is always O_NONBLOCK: accepted
         * with SOCK_NONBLOCK on the native path, and forced in st_init on the
         * Python-constructed path. A nonblocking recv returns EAGAIN instead of
         * waiting, so there is no blocking region to release the GIL around --
         * and doing so per readiness event would cost two state transitions on
         * the hottest path in the loop. */
        result = recv(t->fd, buffer, size, 0);
    } while (result < 0 && errno == EINTR);
    return result;
}

static ssize_t
metal_send(SocketTransport *t, const void *buffer, size_t size)
{
    ssize_t result;
    do {
        /* native-gil-lint: allow NG002 -- see metal_recv: t->fd is guaranteed
         * O_NONBLOCK, so a full send buffer reports EAGAIN rather than waiting. */
        result = send(t->fd, buffer, size, MSG_NOSIGNAL);
    } while (result < 0 && errno == EINTR);
    return result;
}

static ssize_t
metal_sendmsg(SocketTransport *t, const struct msghdr *message)
{
    ssize_t result;
    do {
        /* native-gil-lint: allow NG002 -- see metal_recv: t->fd is guaranteed
         * O_NONBLOCK, so a full send buffer reports EAGAIN rather than waiting. */
        result = sendmsg(t->fd, message, MSG_NOSIGNAL);
    } while (result < 0 && errno == EINTR);
    return result;
}

static Py_ssize_t st_wsize(SocketTransport *t)
{
    return PyByteArray_GET_SIZE(t->wbuf) - t->whead;
}

static int
st_send_busy(SocketTransport *t)
{
    return t->send_op_token != 0 || t->send_obj != NULL ||
           (t->send_queue != NULL &&
            t->send_queue_head < PyList_GET_SIZE(t->send_queue));
}

static Py_ssize_t
st_pending_write_size(SocketTransport *t)
{
    Py_ssize_t pending = st_wsize(t) + t->send_queued_bytes;
    if (t->cork_obj != NULL) {
        pending += PyBytes_GET_SIZE(t->cork_obj);
    }
    return pending;
}

static PyObject *
st_bound(PyObject *obj, const char *name)
{
    return PyObject_GetAttrString(obj, name);
}

/* Bind a method into its cache slot on first use. Connections in the metal
 * fast path never touch most loop/self bound methods (io_uring receive, async
 * send), so binding all of them eagerly in __init__ paid eight attribute
 * lookups and three closure allocations per accepted connection for nothing.
 * Returns a borrowed reference, or NULL with the exception set. */
static PyObject *
st_lazy_bound(PyObject **slot, PyObject *owner, const char *name)
{
    if (*slot == NULL) {
        *slot = PyObject_GetAttrString(owner, name);
    }
    return *slot;
}

static int
st_call_soon(SocketTransport *t, PyObject *fn, PyObject *arg)
{
    PyObject *call_soon = st_lazy_bound(&t->m_call_soon, t->loop, "call_soon");
    if (call_soon == NULL) {
        return -1;
    }
    PyObject *r;
    if (arg == NULL) {
        r = PyObject_CallOneArg(call_soon, fn);
    } else {
        r = PyObject_CallFunctionObjArgs(call_soon, fn, arg, NULL);
    }
    Py_XDECREF(r);
    return r == NULL ? -1 : 0;
}

static int
st_try_start_uring_receive(SocketTransport *t)
{
    if (t->poller_obj == NULL || t->metal == NULL ||
        !t->metal->receive_enabled) {
        return 0;
    }
    /* poller_obj was type-checked when the metal runtime attached, so the
     * poller's C helper is reachable without a Python method call. */
    return rp_start_uring_receive_native(t);
}

static int
st_try_stop_uring_receive(SocketTransport *t)
{
    if (t->poller_obj == NULL || t->metal == NULL ||
        !t->uring_receive_active) {
        return 0;
    }
    int stopped = rp_stop_uring_receive_native(t);
    if (stopped < 0) {
        PyErr_Clear();
        return 1;
    }
    return stopped;
}

static int
st_remove_reader_cb(SocketTransport *t)
{
    PyObject *remove = st_lazy_bound(&t->m_remove_reader, t->loop,
                                     "_remove_reader");
    if (remove == NULL) {
        return -1;
    }
    PyObject *r = PyObject_CallFunction(remove, "i", t->fd);
    Py_XDECREF(r);
    return r == NULL ? -1 : 0;
}

static int
st_remove_writer_cb(SocketTransport *t)
{
    PyObject *remove = st_lazy_bound(&t->m_remove_writer, t->loop,
                                     "_remove_writer");
    if (remove == NULL) {
        return -1;
    }
    PyObject *r = PyObject_CallFunction(remove, "i", t->fd);
    Py_XDECREF(r);
    return r == NULL ? -1 : 0;
}

/* Register the writer callback; returns -1 with the exception set. */
static int
st_register_writer(SocketTransport *t)
{
    if (t->writing) {
        return 0;
    }
    PyObject *ready = st_lazy_bound(&t->write_ready, (PyObject *)t,
                                    "_write_ready");
    PyObject *add = ready != NULL
        ? st_lazy_bound(&t->m_add_writer, t->loop, "_add_writer") : NULL;
    if (add == NULL) {
        return -1;
    }
    PyObject *r = PyObject_CallFunction(add, "iO", t->fd, ready);
    if (r == NULL) {
        return -1;
    }
    Py_DECREF(r);
    t->writing = 1;
    return 0;
}

static int
st_register_reader(SocketTransport *t)
{
    PyObject *ready = st_lazy_bound(&t->read_ready, (PyObject *)t,
                                    "_read_ready");
    PyObject *add = ready != NULL
        ? st_lazy_bound(&t->m_add_reader, t->loop, "_add_reader") : NULL;
    if (add == NULL) {
        return -1;
    }
    PyObject *r = PyObject_CallFunction(add, "iO", t->fd, ready);
    Py_XDECREF(r);
    return r == NULL ? -1 : 0;
}

static int
st_schedule_conn_lost(SocketTransport *t, PyObject *exc)
{
    PyObject *cb = st_lazy_bound(&t->conn_lost_cb, (PyObject *)t,
                                 "_call_connection_lost");
    if (cb == NULL) {
        return -1;
    }
    return st_call_soon(t, cb, exc == NULL ? Py_None : exc);
}

static int
st_async_send_available(SocketTransport *t)
{
    return t->metal != NULL && t->metal->send_enabled &&
           PyThread_get_thread_ident() == t->metal->owner_thread;
}

/* Retain an immutable payload at the tail of the egress queue. */
static int
st_enqueue_send(SocketTransport *t, PyObject *bytes_obj)
{
    if (t->send_queue == NULL) {
        t->send_queue = PyList_New(0);
        if (t->send_queue == NULL) {
            return -1;
        }
    }
    if (PyList_Append(t->send_queue, bytes_obj) < 0) {
        return -1;
    }
    t->send_queued_bytes += PyBytes_GET_SIZE(bytes_obj);
    if (t->metal != NULL &&
        (t->send_op_token != 0 || t->send_obj != NULL)) {
        t->metal->retained_send_enqueues++;
    }
    return 0;
}

static PyObject *
st_dequeue_send(SocketTransport *t)
{
    if (t->send_queue == NULL) {
        return NULL;
    }
    Py_ssize_t size = PyList_GET_SIZE(t->send_queue);
    if (t->send_queue_head >= size) {
        return NULL;
    }
    /* native-lint: allow NC001 -- consumed items are dropped by advancing a
     * head index; the slice delete below runs only on a drained or mostly
     * consumed list, keeping removal amortized O(1). */
    PyObject *item = Py_NewRef(PyList_GET_ITEM(t->send_queue,
                                               t->send_queue_head));
    t->send_queue_head++;
    if (t->send_queue_head >= size ||
        (t->send_queue_head >= 64 && t->send_queue_head * 2 >= size)) {
        if (PyList_SetSlice(t->send_queue, 0, t->send_queue_head, NULL) == 0) {
            t->send_queue_head = 0;
        }
        else {
            PyErr_Clear();  /* keep draining through the head index */
        }
    }
    return item;
}

/* Submit the in-flight payload remainder as one io_uring SEND. The operation
 * retains the transport (released by its CQE) so teardown cannot free the
 * payload while the kernel may still read it. */
static int
st_submit_send_op(SocketTransport *t)
{
    MetalRuntime *metal = t->metal;
    uint64_t token = metal_slab_allocate(
        &metal->operations, t, t->connection_token, METAL_OP_SEND);
    if (token == 0) {
        return -1;  /* slab exhausted: the synchronous fallback takes over */
    }
    const char *base = PyBytes_AS_STRING(t->send_obj);
    Py_ssize_t size = PyBytes_GET_SIZE(t->send_obj) - t->send_obj_off;
    if (metal_uring_queue_send(&metal->uring, t->fd, base + t->send_obj_off,
                               (size_t)size, token) < 0) {
        metal_slab_release(&metal->operations, token, t);
        return -1;
    }
    t->send_op_token = token;
    metal->send_submissions++;
    metal->send_copy_bytes += (uint64_t)size;
    Py_INCREF(t);
    return 0;
}

/* Drive queued egress in order: in-flight payload, retained queue, then the
 * write buffer. Prefers one completion-driven io_uring SEND per payload --
 * submitted here, flushed by the poller's next io_uring_enter, so a drain
 * batch of N responses costs zero send syscalls instead of N. Falls back to
 * synchronous sends when the ring cannot take the operation. Consumes its own
 * errors (st_fatal); leaves no pending exception. */
static void
st_pump_egress(SocketTransport *t)
{
    if (t->conn_lost || t->send_op_token != 0) {
        return;  /* the completion continues this drain */
    }
    int async_ok = st_async_send_available(t);
    for (;;) {
        if (t->send_obj == NULL) {
            t->send_obj = st_dequeue_send(t);
            t->send_obj_off = 0;
            if (t->send_obj == NULL) {
                break;
            }
        }
        if (async_ok && st_submit_send_op(t) == 0) {
            return;  /* the CQE resumes the pump */
        }
        /* Synchronous fallback: ring or slab full, or metal absent. */
        const char *base = PyBytes_AS_STRING(t->send_obj);
        Py_ssize_t size = PyBytes_GET_SIZE(t->send_obj);
        while (t->send_obj_off < size) {
            ssize_t n = metal_send(t, base + t->send_obj_off,
                                   (size_t)(size - t->send_obj_off));
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    if (st_register_writer(t) < 0) {
                        st_fatal(t, "add_writer failed");
                    }
                    return;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                return;
            }
            t->send_obj_off += n;
            t->send_queued_bytes -= n;
        }
        Py_CLEAR(t->send_obj);
        t->send_obj_off = 0;
    }
    /* The write buffer drains only after every retained payload has left. */
    Py_ssize_t size = st_wsize(t);
    if (size > 0) {
        const char *p = PyByteArray_AS_STRING(t->wbuf) + t->whead;
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                return;
            }
            n = 0;
        }
        t->whead += n;
    }
    st_egress_epilogue(t);
}

/* Shared completion tail: on full drain honour a pending close() or
 * half-close; on a partial synchronous write keep the writer registered so
 * the rest drains. */
/* Complete a pending `_empty_waiter()`, if any. Called from the drain tail and
 * from connection loss, so a waiter cannot outlive the transport it is waiting
 * on -- a sendfile blocked on a future nobody will ever resolve is a hung
 * request, which is worse than the error it replaces. */
static void
st_settle_empty_waiter(SocketTransport *t, PyObject *exc_type, const char *message)
{
    PyObject *waiter = t->empty_waiter;
    if (waiter == NULL) {
        return;
    }
    t->empty_waiter = NULL;
    PyObject *done = PyObject_CallMethod(waiter, "done", NULL);
    int already = done != NULL && PyObject_IsTrue(done);
    Py_XDECREF(done);
    if (done == NULL) {
        PyErr_Clear();
    }
    if (!already) {
        PyObject *result;
        if (exc_type != NULL) {
            PyObject *exc = PyObject_CallFunction(exc_type, "s", message);
            result = exc == NULL
                ? NULL : PyObject_CallMethod(waiter, "set_exception", "O", exc);
            Py_XDECREF(exc);
        }
        else {
            result = PyObject_CallMethod(waiter, "set_result", "O", Py_None);
        }
        Py_XDECREF(result);
        if (result == NULL) {
            PyErr_Clear();
        }
    }
    Py_DECREF(waiter);
}

static void
st_egress_epilogue(SocketTransport *t)
{
    if (st_wsize(t) == 0 && !st_send_busy(t)) {
        if (t->whead != 0 || PyByteArray_GET_SIZE(t->wbuf) != 0) {
            if (PyByteArray_Resize(t->wbuf, 0) == 0) {
                t->whead = 0;
            }
        }
        st_settle_empty_waiter(t, NULL, NULL);
        if (t->writing) {
            if (st_remove_writer_cb(t) < 0) {
                PyErr_Clear();
            }
            t->writing = 0;
        }
        st_maybe_resume(t);
        if (t->closing && !t->conn_lost) {
            t->conn_lost++;
            st_schedule_conn_lost(t, Py_None);
        } else if (t->eof) {
            shutdown(t->fd, SHUT_WR);
        }
        return;
    }
    if (st_wsize(t) > 0 && t->send_op_token == 0 && !t->writing) {
        if (st_register_writer(t) < 0) {
            st_fatal(t, "add_writer failed");
            return;
        }
    }
    st_maybe_resume(t);
}

/* Async SEND completion delivered by the poller's CQ drain. */
static void
st_on_send_complete(SocketTransport *t, int32_t result)
{
    t->send_op_token = 0;
    if (result < 0) {
        Py_CLEAR(t->send_obj);  /* the CQE ends the kernel's read of it */
        t->send_obj_off = 0;
        if (result != -ECANCELED && !t->conn_lost) {
            errno = -result;
            PyErr_SetFromErrno(PyExc_OSError);
            st_fatal(t, "write error");
        }
        return;
    }
    t->send_obj_off += result;
    t->send_queued_bytes -= result;
    if (t->send_queued_bytes < 0) {
        t->send_queued_bytes = 0;  /* an abort already zeroed the accounting */
    }
    if (t->send_obj != NULL &&
        t->send_obj_off >= PyBytes_GET_SIZE(t->send_obj)) {
        Py_CLEAR(t->send_obj);
        t->send_obj_off = 0;
    }
    if (!t->conn_lost) {
        st_pump_egress(t);
    }
}

/* Drop queued payloads on abort. The in-flight payload survives until its
 * CQE arrives: the kernel may still be reading it. */
static void
st_clear_send_queue(SocketTransport *t)
{
    Py_CLEAR(t->send_queue);
    t->send_queue_head = 0;
    t->send_queued_bytes = 0;
    if (t->send_op_token == 0) {
        Py_CLEAR(t->send_obj);
        t->send_obj_off = 0;
    }
}

static void
st_maybe_pause(SocketTransport *t)
{
    if (t->protocol_paused || st_pending_write_size(t) <= t->high_water) {
        return;
    }
    t->protocol_paused = 1;
    PyObject *r = PyObject_CallMethod(t->protocol, "pause_writing", NULL);
    if (r == NULL) {
        PyErr_WriteUnraisable(t->protocol);
    }
    Py_XDECREF(r);
}

static void
st_maybe_resume(SocketTransport *t)
{
    if (!t->protocol_paused || st_pending_write_size(t) > t->low_water) {
        return;
    }
    t->protocol_paused = 0;
    PyObject *r = PyObject_CallMethod(t->protocol, "resume_writing", NULL);
    if (r == NULL) {
        PyErr_WriteUnraisable(t->protocol);
    }
    Py_XDECREF(r);
}

/* Force the connection closed after a fatal error. */
static void
st_force_close(SocketTransport *t, PyObject *exc)
{
    if (t->conn_lost) {
        return;
    }
    Py_CLEAR(t->cork_obj);
    if (t->send_op_token != 0 && t->metal != NULL) {
        /* Best effort: without the cancel a SEND parked on a stalled peer
         * would pin its slot and payload until the poller closes. */
        (void)metal_uring_queue_cancel_raw(
            &t->metal->uring, t->send_op_token | METAL_TOKEN_SEND);
    }
    st_clear_send_queue(t);
    if (st_wsize(t) > 0) {
        if (PyByteArray_Resize(t->wbuf, 0) == 0) {
            t->whead = 0;
        }
        if (t->writing) {
            if (st_remove_writer_cb(t) < 0) {
                PyErr_Clear();
            }
            t->writing = 0;
        }
    }
    if (!t->closing) {
        t->closing = 1;
        if (!st_try_stop_uring_receive(t)) {
            if (st_remove_reader_cb(t) < 0) {
                PyErr_Clear();
            }
        }
    }
    t->conn_lost++;
    st_schedule_conn_lost(t, exc);
}

static void
st_fatal(SocketTransport *t, const char *msg)
{
    PyObject *exc = PyErr_GetRaisedException();
    if (exc == NULL) {
        exc = PyObject_CallFunction(PyExc_OSError, "s", msg);
    }
    st_force_close(t, exc);
    Py_XDECREF(exc);
}

/* --- read path --- */
static PyObject *
st_on_eof(SocketTransport *t)
{
    PyObject *keep = PyObject_CallMethod(t->protocol, "eof_received", NULL);
    if (keep == NULL) {
        st_fatal(t, "eof_received() failed");
        Py_RETURN_NONE;
    }
    int keep_open = PyObject_IsTrue(keep);
    Py_DECREF(keep);
    if (keep_open) {
        if (!st_try_stop_uring_receive(t)) {
            if (st_remove_reader_cb(t) < 0) {
                PyErr_Clear();
            }
        }
    } else {
        PyObject *c = PyObject_CallMethod((PyObject *)t, "close", NULL);
        Py_XDECREF(c);
    }
    Py_RETURN_NONE;
}

/* Flush the corked response. The retained exact-bytes payload moves onto the
 * egress queue unchanged (still zero-copy) and the pump prefers one io_uring
 * SEND per payload: during a CQ drain batch of N responses that submission
 * rides the loop's next io_uring_enter, so the batch costs zero send syscalls
 * instead of N. Leaves no pending exception (st_fatal consumes it). */
static void
st_flush_cork(SocketTransport *t)
{
    if (t->conn_lost) {
        return;
    }
    if (t->cork_obj != NULL) {
        PyObject *payload = t->cork_obj;
        t->cork_obj = NULL;
        int queued = st_enqueue_send(t, payload);
        Py_DECREF(payload);
        if (queued < 0) {
            st_fatal(t, "write buffer allocation failed");
            return;
        }
    }
    st_pump_egress(t);
}

static int
st_deliver_received(SocketTransport *t, const char *data, Py_ssize_t size)
{
    Py_ssize_t offset = 0;
    if (t->fused != NULL) {
        return t->fused->feed_external(t->protocol, data, size);
    }
    if (t->buffered) {
        while (offset < size) {
            PyObject *requested = PyLong_FromSsize_t(size - offset);
            if (requested == NULL) {
                return -1;
            }
            PyObject *buffer = PyObject_CallOneArg(t->proto_get_buffer, requested);
            Py_DECREF(requested);
            if (buffer == NULL) {
                return -1;
            }
            Py_buffer view;
            if (PyObject_GetBuffer(buffer, &view, PyBUF_WRITABLE) < 0) {
                Py_DECREF(buffer);
                return -1;
            }
            if (view.len <= 0) {
                PyBuffer_Release(&view);
                Py_DECREF(buffer);
                PyErr_SetString(PyExc_BufferError,
                                "get_buffer() returned an empty buffer");
                return -1;
            }
            Py_ssize_t chunk = size - offset;
            if (chunk > view.len) {
                chunk = view.len;
            }
            memcpy(view.buf, data + offset, (size_t)chunk);
            PyBuffer_Release(&view);
            PyObject *written = PyLong_FromSsize_t(chunk);
            PyObject *result = written != NULL
                ? PyObject_CallOneArg(t->proto_buffer_updated, written) : NULL;
            Py_XDECREF(written);
            Py_DECREF(buffer);
            if (result == NULL) {
                return -1;
            }
            Py_DECREF(result);
            offset += chunk;
            if (t->conn_lost) {
                break;
            }
        }
        return 0;
    }
    PyObject *bytes = PyBytes_FromStringAndSize(data, size);
    if (bytes == NULL) {
        return -1;
    }
    PyObject *result = PyObject_CallOneArg(t->proto_data_received, bytes);
    Py_DECREF(bytes);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static PyObject *
st_read_ready(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->conn_lost) {
        Py_RETURN_NONE;
    }
    /* Cork writes for the duration of the synchronous request drive: a burst of
     * small writes (response head + streaming chunks, and any pipelined replies
     * across the drain loop) accumulates in the write buffer and leaves in one
     * send() at `done`, instead of a syscall per write. Inline handlers finish
     * entirely inside this call, so this coalesces without any deferral. */
    t->cork = 1;
    for (int drain = 0; drain < ST_MAX_DRAIN; drain++) {
        if (t->fused != NULL) {
            char *buffer;
            Py_ssize_t capacity;
            if (t->fused->acquire_read_buffer(
                    t->protocol, &buffer, &capacity) < 0) {
                st_fatal(t, "native stream get_buffer failed");
                goto done;
            }
            ssize_t n = metal_recv(t, buffer, (size_t)capacity);
            if (n < 0) {
                int saved_errno = errno;
                if (t->fused->commit_read(t->protocol, 0) < 0) {
                    PyErr_Clear();
                }
                errno = saved_errno;
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                if (t->fused->commit_read(t->protocol, 0) < 0) {
                    st_fatal(t, "native stream commit failed");
                    goto done;
                }
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            if (t->fused->commit_read(t->protocol, n) < 0) {
                st_fatal(t, "native stream commit failed");
                goto done;
            }
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if (n < capacity) {
                goto done;
            }
        } else if (t->buffered) {
            PyObject *minus1 = PyLong_FromLong(-1);
            PyObject *buf = PyObject_CallOneArg(t->proto_get_buffer, minus1);
            Py_DECREF(minus1);
            if (buf == NULL) {
                st_fatal(t, "get_buffer() failed");
                goto done;
            }
            Py_buffer view;
            if (PyObject_GetBuffer(buf, &view, PyBUF_WRITABLE) < 0) {
                Py_DECREF(buf);
                st_fatal(t, "get_buffer() returned a non-writable buffer");
                goto done;
            }
            ssize_t n = metal_recv(t, view.buf, (size_t)view.len);
            Py_ssize_t cap = view.len;
            PyBuffer_Release(&view);
            /* Keep `buf` (the protocol's memoryview) alive across buffer_updated:
             * releasing it clears the protocol's read offer, exactly as asyncio's
             * recv_into path does by holding the buffer local. */
            if (n < 0) {
                Py_DECREF(buf);
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                Py_DECREF(buf);
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            PyObject *nb = PyLong_FromSsize_t(n);
            PyObject *r = PyObject_CallOneArg(t->proto_buffer_updated, nb);
            Py_DECREF(nb);
            Py_DECREF(buf);
            if (r == NULL) {
                st_fatal(t, "buffer_updated() failed");
                goto done;
            }
            Py_DECREF(r);
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if (n < cap) {
                goto done;  /* short read: socket drained */
            }
        } else {
            PyObject *data = PyBytes_FromStringAndSize(NULL, ST_DATA_RECV);
            if (data == NULL) {
                st_fatal(t, "oom");
                goto done;
            }
            ssize_t n = metal_recv(
                t, PyBytes_AS_STRING(data), ST_DATA_RECV);
            if (n < 0) {
                Py_DECREF(data);
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    goto done;
                }
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "read error");
                goto done;
            }
            if (n == 0) {
                Py_DECREF(data);
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            if (_PyBytes_Resize(&data, n) < 0) {
                st_fatal(t, "oom");
                goto done;
            }
            PyObject *r = PyObject_CallOneArg(t->proto_data_received, data);
            Py_DECREF(data);
            if (r == NULL) {
                st_fatal(t, "data_received() failed");
                goto done;
            }
            Py_DECREF(r);
            if (t->conn_lost || t->reading_paused) {
                goto done;
            }
            if (n < ST_DATA_RECV) {
                goto done;
            }
        }
    }
done:
    t->cork = 0;
    st_flush_cork(t);
    Py_RETURN_NONE;
}

/* --- write path --- */
static PyObject *
st_write_ready(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->conn_lost) {
        Py_RETURN_NONE;
    }
    st_pump_egress(t);
    Py_RETURN_NONE;
}

static PyObject *
st_write(PyObject *op, PyObject *data)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_buffer view;
    if (PyObject_GetBuffer(data, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (view.len == 0 || t->conn_lost || t->eof) {
        if (t->conn_lost) {
            t->conn_lost++;
        }
        PyBuffer_Release(&view);
        Py_RETURN_NONE;
    }
    if (t->cork && t->cork_obj == NULL && st_wsize(t) == 0 &&
        PyBytes_CheckExact(data)) {
        t->cork_obj = Py_NewRef(data);
        if (t->metal == NULL || t->metal->diagnostics) {
            t->zero_copy_cork_writes++;
        }
        PyBuffer_Release(&view);
        Py_RETURN_NONE;
    }
    if (t->cork_obj != NULL) {
        Py_ssize_t pending = PyBytes_GET_SIZE(t->cork_obj);
        if (PyByteArray_Resize(t->wbuf, pending) < 0) {
            PyBuffer_Release(&view);
            return NULL;
        }
        memcpy(PyByteArray_AS_STRING(t->wbuf),
               PyBytes_AS_STRING(t->cork_obj), (size_t)pending);
        Py_CLEAR(t->cork_obj);
        t->whead = 0;
    }
    /* Immediate async path: an exact-bytes payload with nothing buffered in
     * the write buffer rides the egress queue zero-copy. This holds whether
     * the queue is idle (submit now) or busy (retain behind the in-flight
     * send) -- the queue preserves order and the CQE-driven pump drains it. */
    if (!t->cork && st_wsize(t) == 0 && PyBytes_CheckExact(data) &&
        st_async_send_available(t)) {
        PyBuffer_Release(&view);
        if (st_enqueue_send(t, data) < 0) {
            return NULL;
        }
        st_pump_egress(t);
        st_maybe_pause(t);
        Py_RETURN_NONE;
    }
    Py_ssize_t off = 0;
    if (!t->cork && st_wsize(t) == 0 && !st_send_busy(t)) {
        ssize_t n = metal_send(t, view.buf, (size_t)view.len);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyBuffer_Release(&view);
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                Py_RETURN_NONE;
            }
            n = 0;
        }
        off = n;
        if (off == view.len) {
            PyBuffer_Release(&view);
            st_maybe_pause(t);
            Py_RETURN_NONE;  /* submitted or fully sent */
        }
        if (st_register_writer(t) < 0) {
            PyBuffer_Release(&view);
            st_fatal(t, "add_writer failed");
            Py_RETURN_NONE;
        }
    }
    /* Reclaim the dead prefix only once it has grown to at least the live size.
     * Compacting on every write is O(n) per append -- quadratic for a stream of
     * writes under sustained backpressure. Gating on whead >= live bounds the
     * wasted space to 2x the live bytes and makes compaction amortized O(1). */
    if (t->whead > 0 && t->whead >= st_wsize(t)) {
        Py_ssize_t live = st_wsize(t);
        memmove(PyByteArray_AS_STRING(t->wbuf),
                PyByteArray_AS_STRING(t->wbuf) + t->whead, (size_t)live);
        if (PyByteArray_Resize(t->wbuf, live) == 0) {
            t->whead = 0;
        }
    }
    Py_ssize_t old = PyByteArray_GET_SIZE(t->wbuf);
    Py_ssize_t add = view.len - off;
    if (PyByteArray_Resize(t->wbuf, old + add) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    memcpy(PyByteArray_AS_STRING(t->wbuf) + old, (const char *)view.buf + off, (size_t)add);
    PyBuffer_Release(&view);
    /* Bound corked memory: a large inline burst flushes mid-way instead of
     * accumulating the whole response before the single post-drive send(). */
    if (t->cork && st_wsize(t) >= ST_CORK_MAX) {
        st_flush_cork(t);
    }
    st_maybe_pause(t);
    Py_RETURN_NONE;
}

static PyObject *
st_writelines(PyObject *op, PyObject *seq)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *parts = PySequence_Fast(seq, "writelines() needs an iterable");
    if (parts == NULL) {
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(parts);

    /* Large response head+body pairs can leave directly as one writev-shaped
     * syscall. This avoids copying an immutable response body into bytearray.
     * Keep the vector stack-bounded; arbitrary iterables use the normal path.
     * A busy egress queue or staged cork payload must drain first: bypassing
     * them here would reorder bytes on the wire. */
    if (count >= 2 && count <= 16 && st_wsize(t) == 0 && !t->conn_lost &&
        !t->eof && !st_send_busy(t) && t->cork_obj == NULL) {
        struct iovec iov[16];
        Py_buffer views[16];
        Py_ssize_t acquired = 0;
        Py_ssize_t total = 0;
        for (; acquired < count; acquired++) {
            PyObject *item = PySequence_Fast_GET_ITEM(parts, acquired);
            if (PyObject_GetBuffer(item, &views[acquired], PyBUF_SIMPLE) < 0) {
                for (Py_ssize_t i = 0; i < acquired; i++) {
                    PyBuffer_Release(&views[i]);
                }
                Py_DECREF(parts);
                return NULL;
            }
            iov[acquired].iov_base = views[acquired].buf;
            iov[acquired].iov_len = (size_t)views[acquired].len;
            total += views[acquired].len;
        }
        if (total >= 16384) {
            struct msghdr msg;
            memset(&msg, 0, sizeof(msg));
            msg.msg_iov = iov;
            msg.msg_iovlen = (size_t)count;
            ssize_t sent = metal_sendmsg(t, &msg);
            if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                sent = 0;
            } else if (sent < 0) {
                for (Py_ssize_t i = 0; i < acquired; i++) {
                    PyBuffer_Release(&views[i]);
                }
                Py_DECREF(parts);
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                Py_RETURN_NONE;
            }
            if (t->metal == NULL || t->metal->diagnostics) {
                t->direct_writelines++;
            }
            Py_ssize_t remaining = total - sent;
            if (remaining > 0) {
                if (PyByteArray_Resize(t->wbuf, remaining) < 0) {
                    for (Py_ssize_t i = 0; i < acquired; i++) {
                        PyBuffer_Release(&views[i]);
                    }
                    Py_DECREF(parts);
                    return NULL;
                }
                char *dst = PyByteArray_AS_STRING(t->wbuf);
                Py_ssize_t skip = sent;
                for (Py_ssize_t i = 0; i < count; i++) {
                    Py_ssize_t part = views[i].len;
                    if (skip >= part) {
                        skip -= part;
                        continue;
                    }
                    Py_ssize_t copy = part - skip;
                    memcpy(dst, (char *)views[i].buf + skip, (size_t)copy);
                    dst += copy;
                    skip = 0;
                }
                if (st_register_writer(t) < 0) {
                    for (Py_ssize_t i = 0; i < acquired; i++) {
                        PyBuffer_Release(&views[i]);
                    }
                    Py_DECREF(parts);
                    st_fatal(t, "add_writer failed");
                    Py_RETURN_NONE;
                }
                st_maybe_pause(t);
            }
            st_maybe_pause(t);
            for (Py_ssize_t i = 0; i < acquired; i++) {
                PyBuffer_Release(&views[i]);
            }
            Py_DECREF(parts);
            Py_RETURN_NONE;
        }
        for (Py_ssize_t i = 0; i < acquired; i++) {
            PyBuffer_Release(&views[i]);
        }
    }

    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *r = st_write(op, PySequence_Fast_GET_ITEM(parts, i));
        if (r == NULL) {
            Py_DECREF(parts);
            return NULL;
        }
        Py_DECREF(r);
    }
    Py_DECREF(parts);
    Py_RETURN_NONE;
}

static PyObject *
st_close(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing) {
        Py_RETURN_NONE;
    }
    t->closing = 1;
    if (!t->reading_paused && !st_try_stop_uring_receive(t)) {
        if (st_remove_reader_cb(t) < 0) {
            PyErr_Clear();
        }
    }
    if (st_pending_write_size(t) == 0) {
        t->conn_lost++;
        if (t->writing) {
            if (st_remove_writer_cb(t) < 0) {
                PyErr_Clear();
            }
            t->writing = 0;
        }
        st_schedule_conn_lost(t, Py_None);
    }
    /* Otherwise the egress pump's drained epilogue delivers connection_lost
     * once the queue, in-flight send, and write buffer have all left. */
    Py_RETURN_NONE;
}

static PyObject *
st_abort(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    st_force_close((SocketTransport *)op, NULL);
    Py_RETURN_NONE;
}

static PyObject *
st_call_connection_lost(PyObject *op, PyObject *exc)
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->protocol_connected) {
        PyObject *e = (exc == Py_None) ? Py_None : exc;
        PyObject *r = PyObject_CallMethod(t->protocol, "connection_lost", "O", e);
        if (r == NULL) {
            PyErr_WriteUnraisable(t->protocol);
        }
        Py_XDECREF(r);
    }
    if (t->sock != NULL) {
        PyObject *r = PyObject_CallMethod(t->sock, "close", NULL);
        Py_XDECREF(r);
    }
    if (t->server != NULL && t->server != Py_None) {
        PyObject *r = PyObject_CallMethod(t->server, "_detach", "O", op);
        if (r == NULL) {
            PyErr_Clear();  /* older/newer servers may not expose _detach */
        }
        Py_XDECREF(r);
    }
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    st_settle_empty_waiter(t, PyExc_ConnectionResetError,
                           "connection lost while waiting for the write buffer to drain");
    st_clear_send_queue(t);
    metal_detach_transport(t);
    Py_RETURN_NONE;
}

/* _empty_waiter() -> awaitable resolved once nothing is left to send.
 *
 * The caller is `EventLoop.sendfile`, which must not put file bytes on the
 * wire in front of a response head that is still queued. Corked writes are
 * flushed first: the cork exists to coalesce a synchronous request drive into
 * one send, and a waiter that did not flush it would wait for bytes that are
 * deliberately being held back -- which is a hang, not a slow path.
 */
static PyObject *
st_empty_waiter(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->empty_waiter != NULL) {
        PyErr_SetString(PyExc_RuntimeError, "empty waiter is already set");
        return NULL;
    }
    if (t->cork) {
        st_flush_cork(t);
        if (PyErr_Occurred()) {
            return NULL;
        }
    }
    PyObject *future = PyObject_CallMethod(t->loop, "create_future", NULL);
    if (future == NULL) {
        return NULL;
    }
    if (t->conn_lost) {
        PyObject *exc = PyObject_CallFunction(
            PyExc_ConnectionResetError, "s", "connection lost");
        PyObject *set = exc == NULL
            ? NULL : PyObject_CallMethod(future, "set_exception", "O", exc);
        Py_XDECREF(exc);
        Py_XDECREF(set);
        if (set == NULL) {
            Py_DECREF(future);
            return NULL;
        }
        return future;
    }
    if (st_pending_write_size(t) == 0 && !st_send_busy(t)) {
        PyObject *set = PyObject_CallMethod(future, "set_result", "O", Py_None);
        if (set == NULL) {
            Py_DECREF(future);
            return NULL;
        }
        Py_DECREF(set);
        return future;
    }
    t->empty_waiter = Py_NewRef(future);
    return future;
}

static PyObject *
st_start_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || t->reading_paused) {
        Py_RETURN_NONE;
    }
    int uring_started = st_try_start_uring_receive(t);
    if (uring_started < 0) {
        st_fatal(t, "starting io_uring receive failed");
        Py_RETURN_NONE;
    }
    if (!uring_started && st_register_reader(t) < 0) {
        PyErr_Clear();
    }
    Py_RETURN_NONE;
}

static PyObject *
st_pause_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || t->reading_paused) {
        Py_RETURN_NONE;
    }
    t->reading_paused = 1;
    if (!st_try_stop_uring_receive(t)) {
        if (st_remove_reader_cb(t) < 0) {
            PyErr_Clear();
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
st_resume_reading(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->closing || !t->reading_paused) {
        Py_RETURN_NONE;
    }
    t->reading_paused = 0;
    int uring_started = st_try_start_uring_receive(t);
    if (uring_started < 0) {
        st_fatal(t, "resuming io_uring receive failed");
    } else if (!uring_started && st_register_reader(t) < 0) {
        PyErr_Clear();
    }
    Py_RETURN_NONE;
}

/* Natively accepted connections defer the getsockname/getpeername syscalls
 * and tuple conversions out of the accept hot path; the first request for
 * either computes and caches it here (cold: at most once per connection). */
static PyObject *
st_lazy_extra_address(SocketTransport *t, PyObject *name)
{
    const char *method = NULL;
    if (!PyUnicode_Check(name) || t->sock == NULL || t->conn_lost) {
        return NULL;
    }
    if (PyUnicode_CompareWithASCIIString(name, "sockname") == 0) {
        method = "getsockname";
    } else if (PyUnicode_CompareWithASCIIString(name, "peername") == 0) {
        method = "getpeername";
    }
    if (method == NULL) {
        return NULL;
    }
    PyObject *value = PyObject_CallMethod(t->sock, method, NULL);
    if (value == NULL) {
        /* Contract: NULL from this helper means "no value", never "error" --
         * st_get_extra_info returns its caller's default on NULL and never
         * inspects PyErr_Occurred(), matching asyncio's get_extra_info(name,
         * default), which reports a missing key by returning the default rather
         * than raising. A closed or half-torn-down socket makes getsockname
         * fail routinely, so leaving that exception set would surface later as a
         * spurious error at an unrelated call site. */
        PyErr_Clear();
        /* native-error-lint: allow NE003 -- documented NULL-means-absent contract, see above */
        return NULL;
    }
    if (PyDict_SetItem(t->extra, name, value) < 0) {
        PyErr_Clear();
    }
    return value;
}

static PyObject *
st_get_extra_info(PyObject *op, PyObject *args)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *name, *dflt = Py_None;
    if (!PyArg_ParseTuple(args, "O|O", &name, &dflt)) {
        return NULL;
    }
    PyObject *v = PyDict_GetItemWithError(t->extra, name);
    if (v != NULL) {
        return Py_NewRef(v);
    }
    if (PyErr_Occurred()) {
        return NULL;
    }
    PyObject *lazy = st_lazy_extra_address(t, name);
    if (lazy != NULL) {
        return lazy;
    }
    return Py_NewRef(dflt);
}

static PyObject *
st_is_closing(PyObject *op, PyObject *Py_UNUSED(i))
{
    return PyBool_FromLong(((SocketTransport *)op)->closing);
}

static PyObject *
st_is_reading(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyBool_FromLong(!t->closing && !t->reading_paused);
}

static PyObject *
st_get_protocol(PyObject *op, PyObject *Py_UNUSED(i))
{
    PyObject *p = ((SocketTransport *)op)->protocol;
    return Py_NewRef(p ? p : Py_None);
}

static void st_bind_protocol_methods(SocketTransport *t);

static PyObject *
st_set_protocol(PyObject *op, PyObject *proto)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_XSETREF(t->protocol, Py_NewRef(proto));
    st_bind_protocol_methods(t);
    Py_RETURN_NONE;
}

static PyObject *
st_get_write_buffer_size(PyObject *op, PyObject *Py_UNUSED(i))
{
    return PyLong_FromSsize_t(st_pending_write_size((SocketTransport *)op));
}

static PyObject *
st_get_write_buffer_limits(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    return Py_BuildValue("nn", t->low_water, t->high_water);
}

static PyObject *
st_set_write_buffer_limits(PyObject *op, PyObject *args, PyObject *kwds)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *high = Py_None, *low = Py_None;
    static char *kw[] = {"high", "low", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|OO", kw, &high, &low)) {
        return NULL;
    }
    Py_ssize_t h = (high == Py_None) ? -1 : PyLong_AsSsize_t(high);
    Py_ssize_t lo = (low == Py_None) ? -1 : PyLong_AsSsize_t(low);
    if (h < 0) {
        h = (lo >= 0) ? lo * 4 : 65536;
    }
    if (lo < 0) {
        lo = h / 4;
    }
    if (lo > h) {
        PyErr_SetString(PyExc_ValueError, "high must be >= low");
        return NULL;
    }
    t->high_water = h;
    t->low_water = lo;
    st_maybe_pause(t);
    Py_RETURN_NONE;
}

static PyObject *
st_can_write_eof(PyObject *op, PyObject *Py_UNUSED(i))
{
    Py_RETURN_TRUE;
}

static PyObject *
st_write_eof(PyObject *op, PyObject *Py_UNUSED(i))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->eof || t->conn_lost) {
        Py_RETURN_NONE;
    }
    t->eof = 1;
    if (st_pending_write_size(t) == 0) {
        shutdown(t->fd, SHUT_WR);
    }
    Py_RETURN_NONE;
}

static PyMethodDef st_methods[] = {
    {"write", st_write, METH_O, NULL},
    {"writelines", st_writelines, METH_O, NULL},
    {"close", st_close, METH_NOARGS, NULL},
    {"abort", st_abort, METH_NOARGS, NULL},
    {"get_extra_info", st_get_extra_info, METH_VARARGS, NULL},
    {"is_closing", st_is_closing, METH_NOARGS, NULL},
    {"is_reading", st_is_reading, METH_NOARGS, NULL},
    {"pause_reading", st_pause_reading, METH_NOARGS, NULL},
    {"resume_reading", st_resume_reading, METH_NOARGS, NULL},
    {"get_protocol", st_get_protocol, METH_NOARGS, NULL},
    {"set_protocol", st_set_protocol, METH_O, NULL},
    {"get_write_buffer_size", st_get_write_buffer_size, METH_NOARGS, NULL},
    {"get_write_buffer_limits", st_get_write_buffer_limits, METH_NOARGS, NULL},
    {"set_write_buffer_limits", (PyCFunction)(void (*)(void))st_set_write_buffer_limits,
     METH_VARARGS | METH_KEYWORDS, NULL},
    {"can_write_eof", st_can_write_eof, METH_NOARGS, NULL},
    {"write_eof", st_write_eof, METH_NOARGS, NULL},
    /* internal callbacks (scheduled onto the loop) */
    {"_empty_waiter", st_empty_waiter, METH_NOARGS, NULL},
    {"_read_ready", st_read_ready, METH_NOARGS, NULL},
    {"_write_ready", st_write_ready, METH_NOARGS, NULL},
    {"_start_reading", st_start_reading, METH_NOARGS, NULL},
    {"_call_connection_lost", st_call_connection_lost, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static void
st_bind_protocol_methods(SocketTransport *t)
{
    Py_CLEAR(t->proto_get_buffer);
    Py_CLEAR(t->proto_buffer_updated);
    Py_CLEAR(t->proto_data_received);
    t->fused = NULL;
    t->fused_module = NULL;
    for (Py_ssize_t i = 0; i < STREAM_CAPI_COUNT; i++) {
        const WreathStreamCAPI *capi = stream_capi_resolve(&g_stream_capis[i]);
        if (capi != NULL && capi->check(t->protocol)) {
            t->fused = capi;
            t->fused_module = g_stream_capis[i].module_name;
            break;
        }
    }
    if (t->fused != NULL) {
        t->buffered = 1;
        return;
    }
    t->buffered = g_buffered_protocol != NULL &&
                  PyObject_IsInstance(t->protocol, g_buffered_protocol) == 1;
    if (PyErr_Occurred()) {
        PyErr_Clear();
        t->buffered = 0;
    }
    if (t->buffered) {
        t->proto_get_buffer = st_bound(t->protocol, "get_buffer");
        t->proto_buffer_updated = st_bound(t->protocol, "buffer_updated");
    } else {
        t->proto_data_received = st_bound(t->protocol, "data_received");
    }
}

static int
st_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    SocketTransport *t = (SocketTransport *)op;
    PyObject *loop, *sock, *protocol, *waiter = Py_None, *extra = Py_None, *server = Py_None;
    int inline_activate = 0;
    int known_fd = -1;
    static char *kw[] = {
        "loop", "sock", "protocol", "waiter", "extra", "server",
        "inline_activate", "fd", NULL
    };
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OOO|OOOpi", kw, &loop, &sock,
                                     &protocol, &waiter, &extra, &server,
                                     &inline_activate, &known_fd)) {
        return -1;
    }
    t->loop = Py_NewRef(loop);
    t->sock = Py_NewRef(sock);
    t->protocol = Py_NewRef(protocol);
    t->poller_obj = NULL;
    t->metal = NULL;
    t->connection_token = 0;
    t->uring_receive_active = 0;
    t->uring_receive_multishot = 0;
    PyObject *poller_obj = PyObject_GetAttrString(loop, "_poller");
    if (poller_obj == NULL) {
        PyErr_Clear();
    } else {
        if (poller_obj != Py_None && metal_attach_transport(t, poller_obj) < 0) {
            Py_DECREF(poller_obj);
            return -1;
        }
        Py_DECREF(poller_obj);
    }
    t->server = Py_NewRef(server);
    t->extra = (extra == Py_None) ? PyDict_New() : PyDict_Copy(extra);
    if (t->extra == NULL) {
        goto error;
    }
    if (known_fd >= 0) {
        /* The native accept path already holds the fd; skip the fileno()
         * Python round trip per accepted connection. */
        t->fd = known_fd;
    } else {
        PyObject *fdobj = PyObject_CallMethod(sock, "fileno", NULL);
        if (fdobj == NULL) {
            goto error;
        }
        t->fd = (int)PyLong_AsLong(fdobj);
        Py_DECREF(fdobj);
        if (t->fd < 0 && PyErr_Occurred()) {
            goto error;
        }
        /* The accept path above gets O_NONBLOCK from accept4(SOCK_NONBLOCK); a
         * socket handed in from Python has only asyncio's convention behind it,
         * and metal_recv/metal_send issue recv/send while holding the GIL, so a
         * blocking descriptor here would stall the entire loop instead of
         * returning EAGAIN. Make the invariant true rather than assumed -- once,
         * off the accept hot path, which already pays nothing for it. */
        int flags = fcntl(t->fd, F_GETFL, 0);
        if (flags >= 0 && !(flags & O_NONBLOCK)) {
            fcntl(t->fd, F_SETFL, flags | O_NONBLOCK);
        }
    }
    /* TCP_NODELAY (best effort) */
    int one = 1;
    setsockopt(t->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    /* Loop and self bound methods bind lazily on first use (st_lazy_bound):
     * the metal fast path -- io_uring receive, async send -- touches none of
     * them, so eager binding taxed every accepted connection. */
    t->m_add_reader = NULL;
    t->m_remove_reader = NULL;
    t->m_add_writer = NULL;
    t->m_remove_writer = NULL;
    t->m_call_soon = NULL;
    t->read_ready = NULL;
    t->write_ready = NULL;
    t->conn_lost_cb = NULL;

    t->wbuf = PyByteArray_FromStringAndSize("", 0);
    if (t->wbuf == NULL) {
        goto error;
    }
    t->whead = 0;
    t->cork_obj = NULL;
    t->writing = 0;
    t->cork = 0;
    t->send_queue = NULL;
    t->send_queue_head = 0;
    t->send_obj = NULL;
    t->send_obj_off = 0;
    t->send_queued_bytes = 0;
    t->send_op_token = 0;
    t->direct_writelines = 0;
    t->direct_read_dispatches = 0;
    t->direct_protocol_writes = 0;
    t->zero_copy_cork_writes = 0;
    t->high_water = 65536;
    t->low_water = 16384;
    t->protocol_paused = 0;
    t->reading_paused = 0;
    t->closing = 0;
    t->conn_lost = 0;
    t->eof = 0;
    t->protocol_connected = 1;

    st_bind_protocol_methods(t);

    /* populate get_extra_info like asyncio does */
    if (PyDict_SetItemString(t->extra, "socket", sock) < 0) {
        goto error;
    }
    /* Native accepts (inline_activate) defer sockname/peername: two syscalls
     * plus tuple conversions per connection that most protocols never read.
     * st_get_extra_info computes and caches them on first request. */
    if (!inline_activate) {
        PyObject *sn = PyObject_CallMethod(sock, "getsockname", NULL);
        if (sn != NULL) {
            if (PyDict_SetItemString(t->extra, "sockname", sn) < 0) {
                Py_DECREF(sn);
                goto error;
            }
            Py_DECREF(sn);
        } else {
            PyErr_Clear();
        }
        if (PyDict_GetItemString(t->extra, "peername") == NULL) {
            PyObject *pn = PyObject_CallMethod(sock, "getpeername", NULL);
            if (pn != NULL) {
                if (PyDict_SetItemString(t->extra, "peername", pn) < 0) {
                    Py_DECREF(pn);
                    goto error;
                }
                Py_DECREF(pn);
            } else {
                PyErr_Clear();
            }
        }
    }

    /* register with the server so shutdown accounting can close us */
    if (t->server != NULL && t->server != Py_None) {
        PyObject *r = PyObject_CallMethod(t->server, "_attach", "O", op);
        if (r == NULL) {
            PyErr_Clear();
        } else {
            Py_DECREF(r);
        }
    }

    /* Accepted plaintext metal connections activate synchronously after every
     * transport field and server attachment is valid. Other construction paths
     * retain asyncio's scheduled connection_made/start-reading ordering. */
    PyObject *cm = st_bound(protocol, "connection_made");
    if (inline_activate) {
        if (cm != NULL) {
            PyObject *connected = PyObject_CallOneArg(cm, op);
            Py_DECREF(cm);
            if (connected == NULL) {
                goto error;
            }
            Py_DECREF(connected);
        }
        PyObject *started = st_start_reading(op, NULL);
        if (started == NULL) {
            goto error;
        }
        Py_DECREF(started);
    } else {
        if (cm != NULL) {
            st_call_soon(t, cm, op);
            Py_DECREF(cm);
        }
        PyObject *sr = st_bound(op, "_start_reading");
        if (sr != NULL) {
            st_call_soon(t, sr, NULL);
            Py_DECREF(sr);
        }
    }
    if (waiter != Py_None) {
        PyObject *setres = PyObject_GetAttrString(
            PyImport_AddModule("asyncio.futures"), "_set_result_unless_cancelled");
        PyObject *call_soon = setres != NULL
            ? st_lazy_bound(&t->m_call_soon, t->loop, "call_soon") : NULL;
        if (call_soon != NULL) {
            PyObject *r = PyObject_CallFunctionObjArgs(
                call_soon, setres, waiter, Py_None, NULL);
            Py_XDECREF(r);
        }
        Py_XDECREF(setres);
        if (PyErr_Occurred()) {
            PyErr_Clear();
        }
    }
    return 0;

error:
    /* A half-built transport must not stay registered: the poller owns a
     * reference to everything in its connection slab, and would both leak
     * this one and keep delivering completions to it. */
    metal_detach_transport(t);
    return -1;
}

static int
st_traverse(PyObject *op, visitproc visit, void *arg)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_VISIT(t->loop);
    Py_VISIT(t->poller_obj);
    Py_VISIT(t->empty_waiter);
    Py_VISIT(t->sock);
    Py_VISIT(t->protocol);
    Py_VISIT(t->server);
    Py_VISIT(t->extra);
    Py_VISIT(t->wbuf);
    Py_VISIT(t->cork_obj);
    Py_VISIT(t->send_queue);
    Py_VISIT(t->send_obj);
    /* The bound methods below close a reference cycle back to the transport
     * (read_ready/write_ready/conn_lost_cb are bound to self); GC must see them
     * or a closed connection is never collected. */
    Py_VISIT(t->m_add_reader);
    Py_VISIT(t->m_remove_reader);
    Py_VISIT(t->m_add_writer);
    Py_VISIT(t->m_remove_writer);
    Py_VISIT(t->m_call_soon);
    Py_VISIT(t->proto_get_buffer);
    Py_VISIT(t->proto_buffer_updated);
    Py_VISIT(t->proto_data_received);
    Py_VISIT(t->read_ready);
    Py_VISIT(t->write_ready);
    Py_VISIT(t->conn_lost_cb);
    return 0;
}

static int
st_clear(PyObject *op)
{
    SocketTransport *t = (SocketTransport *)op;
    metal_detach_transport(t);
    t->metal = NULL;
    Py_CLEAR(t->empty_waiter);
    Py_CLEAR(t->poller_obj);
    Py_CLEAR(t->loop);
    Py_CLEAR(t->sock);
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    Py_CLEAR(t->extra);
    Py_CLEAR(t->wbuf);
    Py_CLEAR(t->cork_obj);
    Py_CLEAR(t->send_queue);
    Py_CLEAR(t->send_obj);
    t->send_queue_head = 0;
    t->send_obj_off = 0;
    t->send_queued_bytes = 0;
    Py_CLEAR(t->m_add_reader);
    Py_CLEAR(t->m_remove_reader);
    Py_CLEAR(t->m_add_writer);
    Py_CLEAR(t->m_remove_writer);
    Py_CLEAR(t->m_call_soon);
    Py_CLEAR(t->proto_get_buffer);
    Py_CLEAR(t->proto_buffer_updated);
    Py_CLEAR(t->proto_data_received);
    Py_CLEAR(t->read_ready);
    Py_CLEAR(t->write_ready);
    Py_CLEAR(t->conn_lost_cb);
    return 0;
}

static void
st_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    st_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
st_fused_http1_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyBool_FromLong(
        t->fused != NULL && t->fused == g_stream_capis[0].capi);
}

static PyObject *
st_fused_stream_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    if (t->fused_module == NULL) {
        Py_RETURN_NONE;
    }
    return PyUnicode_FromString(t->fused_module);
}

static PyObject *
st_direct_writelines_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_writelines);
}

static int
transport_capi_check(PyObject *op)
{
    return PyObject_TypeCheck(op, &SocketTransportType);
}

static int
transport_capi_write(PyObject *op, PyObject *data)
{
    PyObject *result = st_write(op, data);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    SocketTransport *transport = (SocketTransport *)op;
    if (transport->metal == NULL || transport->metal->diagnostics) {
        transport->direct_protocol_writes++;
    }
    return 0;
}

static int
transport_capi_writelines(PyObject *op, PyObject *parts)
{
    PyObject *result = st_writelines(op, parts);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    SocketTransport *transport = (SocketTransport *)op;
    if (transport->metal == NULL || transport->metal->diagnostics) {
        transport->direct_protocol_writes++;
    }
    return 0;
}

static PyObject *
st_direct_read_dispatches_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_read_dispatches);
}

static PyObject *
st_direct_protocol_writes_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->direct_protocol_writes);
}

static PyObject *
st_zero_copy_cork_writes_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((SocketTransport *)op)->zero_copy_cork_writes);
}

static PyObject *
st_metal_connection_token_get(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((SocketTransport *)op)->connection_token);
}

static PyObject *
st_metal_submissions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL
            ? t->metal->submissions + t->metal->send_submissions : 0);
}

static PyObject *
st_metal_completions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL
            ? t->metal->completions + t->metal->send_completions : 0);
}

static PyObject *
st_metal_operation_high_water_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLong(
        t->metal != NULL ? t->metal->operations.high_water : 0
    );
}

static PyObject *
st_metal_operation_exhaustions_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL ? t->metal->operations.exhaustions : 0
    );
}

static PyObject *
st_metal_worker_id_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLong(t->metal != NULL ? t->metal->worker_id : 0);
}

static PyObject *
st_metal_cross_worker_rejections_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyLong_FromUnsignedLongLong(
        t->metal != NULL ? t->metal->cross_worker_rejections : 0
    );
}

static PyObject *
st_metal_io_backend_get(PyObject *op, void *Py_UNUSED(closure))
{
    SocketTransport *t = (SocketTransport *)op;
    return PyUnicode_FromString(
        t->metal != NULL ? "io_uring" : "synchronous");
}

static PyGetSetDef st_getset[] = {
    {"_fused_http1", st_fused_http1_get, NULL,
     "whether ingress uses the private native HTTP/1 C API", NULL},
    {"_fused_stream", st_fused_stream_get, NULL,
     "module name of the fused stream C API, or None", NULL},
    {"_direct_writelines", st_direct_writelines_get, NULL,
     "number of large writelines emitted through sendmsg", NULL},
    {"_direct_read_dispatches", st_direct_read_dispatches_get, NULL,
     "number of readiness callbacks dispatched as direct C calls", NULL},
    {"_direct_protocol_writes", st_direct_protocol_writes_get, NULL,
     "number of protocol writes entering through the transport C API", NULL},
    {"_zero_copy_cork_writes", st_zero_copy_cork_writes_get, NULL,
     "number of immutable writes retained for direct post-drive send", NULL},
    {"_metal_connection_token", st_metal_connection_token_get, NULL,
     "generation-validated metal connection handle", NULL},
    {"_metal_submissions", st_metal_submissions_get, NULL,
     "socket operations submitted through the metal backend", NULL},
    {"_metal_completions", st_metal_completions_get, NULL,
     "normalized metal completions consumed", NULL},
    {"_metal_operation_high_water", st_metal_operation_high_water_get, NULL,
     "operation slab high-water occupancy", NULL},
    {"_metal_operation_exhaustions", st_metal_operation_exhaustions_get, NULL,
     "operation submissions rejected by bounded slab exhaustion", NULL},
    {"_metal_worker_id", st_metal_worker_id_get, NULL,
     "owner worker for this metal connection", NULL},
    {"_metal_cross_worker_rejections", st_metal_cross_worker_rejections_get, NULL,
     "operations rejected outside the owner thread", NULL},
    {"_metal_io_backend", st_metal_io_backend_get, NULL,
     "metal socket operation backend", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject SocketTransportType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._reactor.SocketTransport",
    .tp_basicsize = sizeof(SocketTransport),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_dealloc = st_dealloc,
    .tp_traverse = st_traverse,
    .tp_clear = st_clear,
    .tp_methods = st_methods,
    .tp_getset = st_getset,
    .tp_init = st_init,
    .tp_new = PyType_GenericNew,
};
