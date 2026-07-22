/* Native plaintext socket transport. */
typedef struct {
    PyObject_HEAD
    PyObject *loop;
    PyObject *sock;             /* the Python socket (kept alive; closed on lost) */
    PyObject *protocol;
    PyObject *server;           /* AbstractServer, or None */
    PyObject *extra;            /* get_extra_info dict */
    int fd;
    int buffered;               /* protocol is a BufferedProtocol */
    int fused_http1;             /* direct native HTTP/1 buffer C API */
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
    /* Cold diagnostics live after request-hot transport state. */
    Py_ssize_t direct_writelines;
    Py_ssize_t direct_read_dispatches;
    Py_ssize_t direct_protocol_writes;
    Py_ssize_t zero_copy_cork_writes;
} SocketTransport;

static PyTypeObject SocketTransportType;
static int metal_attach_transport(SocketTransport *, PyObject *);
static void metal_detach_transport(SocketTransport *);

static uint64_t
metal_begin_operation(SocketTransport *t, uint16_t kind)
{
    if (t->metal == NULL) {
        return 0;
    }
    if (PyThread_get_thread_ident() != t->metal->owner_thread) {
        t->metal->cross_worker_rejections++;
        errno = EXDEV;
        return 0;
    }
    uint64_t token = metal_slab_allocate(
        &t->metal->operations, t, t->connection_token, kind
    );
    if (token == 0) {
        errno = ENOBUFS;
        return 0;
    }
    t->metal->submissions++;
    return token;
}

static ssize_t
metal_finish_operation(SocketTransport *t, uint64_t token, uint16_t kind,
                       ssize_t raw_result, int saved_errno)
{
    if (t->metal == NULL) {
        errno = saved_errno;
        return raw_result;
    }
    MetalCompletion completion;
    completion.token = token;
    completion.result = raw_result < 0 ? -saved_errno : (int32_t)raw_result;
    completion.kind = kind;
    completion.flags = raw_result == 0 ? METAL_COMPLETION_EOF : 0;
    if (raw_result < 0) {
        completion.flags |= METAL_COMPLETION_ERROR;
    }
    completion.value = 0;
    metal_trace_add(t->metal, METAL_IO_URING, kind, token,
                    completion.result, (uint8_t)completion.flags);

    MetalSlot *operation = metal_slab_validate(&t->metal->operations, token, t);
    MetalSlot *connection = metal_slab_validate(
        &t->metal->connections, t->connection_token, t
    );
    int valid = operation != NULL && connection != NULL &&
                metal_slot_kind(operation) == kind &&
                operation->related == t->connection_token;
    if (operation != NULL) {
        metal_slab_release(&t->metal->operations, token, t);
    }
    t->metal->completions++;
    if (!valid) {
        errno = ECANCELED;
        return -1;
    }
    errno = saved_errno;
    return completion.result < 0 ? -1 : (ssize_t)completion.result;
}

static ssize_t
metal_recv(SocketTransport *t, void *buffer, size_t size)
{
    if (t->metal == NULL) {
        ssize_t result;
        do {
            result = recv(t->fd, buffer, size, 0);
        } while (result < 0 && errno == EINTR);
        return result;
    }
    uint64_t token = metal_begin_operation(t, METAL_OP_RECV);
    if (token == 0) {
        return -1;
    }
    ssize_t result;
    int saved_errno;
    /* Production Metal receive is completion-driven into provided buffers; a
     * synchronous read here would violate that ownership domain. */
    (void)buffer;
    (void)size;
    result = -1;
    saved_errno = EOPNOTSUPP;
    return metal_finish_operation(t, token, METAL_OP_RECV, result, saved_errno);
}

static ssize_t
metal_send(SocketTransport *t, const void *buffer, size_t size)
{
    ssize_t result;
    do {
        result = send(t->fd, buffer, size, MSG_NOSIGNAL);
    } while (result < 0 && errno == EINTR);
    return result;
}

static ssize_t
metal_sendmsg(SocketTransport *t, const struct msghdr *message)
{
    ssize_t result;
    do {
        result = sendmsg(t->fd, message, MSG_NOSIGNAL);
    } while (result < 0 && errno == EINTR);
    return result;
}

static Py_ssize_t st_wsize(SocketTransport *t)
{
    return PyByteArray_GET_SIZE(t->wbuf) - t->whead;
}

static Py_ssize_t
st_pending_write_size(SocketTransport *t)
{
    return st_wsize(t);
}

static PyObject *
st_bound(PyObject *obj, const char *name)
{
    return PyObject_GetAttrString(obj, name);
}

static int
st_call_soon(SocketTransport *t, PyObject *fn, PyObject *arg)
{
    PyObject *r;
    if (arg == NULL) {
        r = PyObject_CallOneArg(t->m_call_soon, fn);
    } else {
        r = PyObject_CallFunctionObjArgs(t->m_call_soon, fn, arg, NULL);
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
    PyObject *result = PyObject_CallMethod(
        t->poller_obj, "_start_uring_receive", "O", (PyObject *)t);
    if (result == NULL) {
        return -1;
    }
    int started = PyObject_IsTrue(result);
    Py_DECREF(result);
    return started;
}

static int
st_try_stop_uring_receive(SocketTransport *t)
{
    if (t->poller_obj == NULL || !t->uring_receive_active) {
        return 0;
    }
    PyObject *result = PyObject_CallMethod(
        t->poller_obj, "_stop_uring_receive", "O", (PyObject *)t);
    if (result == NULL) {
        PyErr_Clear();
        return 1;
    }
    int stopped = PyObject_IsTrue(result);
    Py_DECREF(result);
    return stopped;
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
    if (st_wsize(t) > 0) {
        if (PyByteArray_Resize(t->wbuf, 0) == 0) {
            t->whead = 0;
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
    }
    if (!t->closing) {
        t->closing = 1;
        if (!st_try_stop_uring_receive(t)) {
            PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
            Py_XDECREF(r);
        }
    }
    t->conn_lost++;
    st_call_soon(t, t->conn_lost_cb, exc == NULL ? Py_None : exc);
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
            PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
            Py_XDECREF(r);
        }
    } else {
        PyObject *c = PyObject_CallMethod((PyObject *)t, "close", NULL);
        Py_XDECREF(c);
    }
    Py_RETURN_NONE;
}

/* Flush the corked write buffer in a single send(); register the writer for any
 * remainder. Called after the synchronous request drive so a burst of small
 * writes (response head + streaming chunks) collapses into one syscall instead
 * of one per write. Leaves no pending exception (st_fatal consumes it). */
static void
st_flush_cork(SocketTransport *t)
{
    if (t->conn_lost) {
        return;
    }
    int pending_partial = 0;
    if (t->cork_obj != NULL) {
        const char *p = PyBytes_AS_STRING(t->cork_obj);
        Py_ssize_t size = PyBytes_GET_SIZE(t->cork_obj);
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
                return;
            }
            n = 0;
        }
        if (n < size) {
            Py_ssize_t remaining = size - n;
            if (PyByteArray_Resize(t->wbuf, remaining) < 0) {
                st_fatal(t, "write buffer allocation failed");
                return;
            }
            memcpy(PyByteArray_AS_STRING(t->wbuf), p + n, (size_t)remaining);
            t->whead = 0;
            pending_partial = 1;
        }
        Py_CLEAR(t->cork_obj);
    }
    Py_ssize_t size = st_wsize(t);
    if (size > 0 && !pending_partial) {
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
    /* Same completion as st_write_ready: on full drain honour a pending close()
     * (schedule connection_lost -- a corked response followed by close() must
     * still terminate the connection) or half-close; on a partial write register
     * the writer so the rest drains. */
    if (st_wsize(t) == 0) {
        if (t->whead != 0 || PyByteArray_GET_SIZE(t->wbuf) != 0) {
            if (PyByteArray_Resize(t->wbuf, 0) == 0) {
                t->whead = 0;
            }
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_maybe_resume(t);
        if (t->closing && !t->conn_lost) {
            t->conn_lost++;
            st_call_soon(t, t->conn_lost_cb, Py_None);
        } else if (t->eof) {
            shutdown(t->fd, SHUT_WR);
        }
    } else if (!t->writing) {
        PyObject *r = PyObject_CallFunction(t->m_add_writer, "iO", t->fd, t->write_ready);
        if (r == NULL) {
            st_fatal(t, "add_writer failed");
            return;
        }
        Py_DECREF(r);
        t->writing = 1;
    }
}

static int
st_deliver_received(SocketTransport *t, const char *data, Py_ssize_t size)
{
    Py_ssize_t offset = 0;
    if (t->fused_http1) {
        return g_http1_capi->feed_external(t->protocol, data, size);
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
        if (t->fused_http1) {
            char *buffer;
            Py_ssize_t capacity;
            if (g_http1_capi->acquire_read_buffer(
                    t->protocol, &buffer, &capacity) < 0) {
                st_fatal(t, "native HTTP/1 get_buffer failed");
                goto done;
            }
            ssize_t n = metal_recv(t, buffer, (size_t)capacity);
            if (n < 0) {
                int saved_errno = errno;
                if (g_http1_capi->commit_read(t->protocol, 0) < 0) {
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
                if (g_http1_capi->commit_read(t->protocol, 0) < 0) {
                    st_fatal(t, "native HTTP/1 commit failed");
                    goto done;
                }
                t->cork = 0;
                st_flush_cork(t);
                return st_on_eof(t);
            }
            if (g_http1_capi->commit_read(t->protocol, n) < 0) {
                st_fatal(t, "native HTTP/1 commit failed");
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
    Py_ssize_t size = st_wsize(t);
    if (size > 0) {
        const char *p = PyByteArray_AS_STRING(t->wbuf) + t->whead;
        ssize_t n = metal_send(t, p, (size_t)size);
        if (n < 0) {
            if (!(errno == EAGAIN || errno == EWOULDBLOCK)) {
                PyErr_SetFromErrno(PyExc_OSError);
                st_fatal(t, "write error");
            }
            Py_RETURN_NONE;
        }
        t->whead += n;
    }
    if (st_wsize(t) == 0) {
        if (t->whead != 0 || PyByteArray_GET_SIZE(t->wbuf) != 0) {
            if (PyByteArray_Resize(t->wbuf, 0) == 0) {
                t->whead = 0;
            }
        }
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_maybe_resume(t);
        if (t->closing) {
            t->conn_lost++;
            st_call_soon(t, t->conn_lost_cb, Py_None);
        } else if (t->eof) {
            shutdown(t->fd, SHUT_WR);
        }
    } else {
        st_maybe_resume(t);
    }
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
    Py_ssize_t off = 0;
    if (!t->cork && st_wsize(t) == 0) {
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
        if (!t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_add_writer, "iO", t->fd, t->write_ready);
            if (r == NULL) {
                PyBuffer_Release(&view);
                st_fatal(t, "add_writer failed");
                Py_RETURN_NONE;
            }
            Py_DECREF(r);
            t->writing = 1;
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
     * Keep the vector stack-bounded; arbitrary iterables use the normal path. */
    if (count >= 2 && count <= 16 && st_wsize(t) == 0 && !t->conn_lost && !t->eof) {
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
                if (!t->writing) {
                    PyObject *r = PyObject_CallFunction(
                        t->m_add_writer, "iO", t->fd, t->write_ready);
                    if (r == NULL) {
                        for (Py_ssize_t i = 0; i < acquired; i++) {
                            PyBuffer_Release(&views[i]);
                        }
                        Py_DECREF(parts);
                        st_fatal(t, "add_writer failed");
                        Py_RETURN_NONE;
                    }
                    Py_DECREF(r);
                    t->writing = 1;
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
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
    }
    if (st_wsize(t) == 0 && t->cork_obj == NULL) {
        t->conn_lost++;
        if (t->writing) {
            PyObject *r = PyObject_CallFunction(t->m_remove_writer, "i", t->fd);
            Py_XDECREF(r);
            t->writing = 0;
        }
        st_call_soon(t, t->conn_lost_cb, Py_None);
    }
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
    metal_detach_transport(t);
    Py_RETURN_NONE;
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
    if (!uring_started) {
        PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
        Py_XDECREF(r);
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
        PyObject *r = PyObject_CallFunction(t->m_remove_reader, "i", t->fd);
        Py_XDECREF(r);
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
    } else if (!uring_started) {
        PyObject *r = PyObject_CallFunction(t->m_add_reader, "iO", t->fd, t->read_ready);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
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
    if (st_wsize(t) == 0) {
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
    load_http1_capi();
    t->fused_http1 = g_http1_capi != NULL &&
                     g_http1_capi->version == WREATH_HTTP1_CAPI_VERSION &&
                     g_http1_capi->check(t->protocol);
    if (t->fused_http1) {
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
    static char *kw[] = {
        "loop", "sock", "protocol", "waiter", "extra", "server",
        "inline_activate", NULL
    };
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OOO|OOOp", kw, &loop, &sock,
                                     &protocol, &waiter, &extra, &server,
                                     &inline_activate)) {
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
        return -1;
    }
    PyObject *fdobj = PyObject_CallMethod(sock, "fileno", NULL);
    if (fdobj == NULL) {
        return -1;
    }
    t->fd = (int)PyLong_AsLong(fdobj);
    Py_DECREF(fdobj);
    if (t->fd < 0 && PyErr_Occurred()) {
        return -1;
    }
    /* TCP_NODELAY (best effort) */
    int one = 1;
    setsockopt(t->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    t->m_add_reader = st_bound(loop, "_add_reader");
    t->m_remove_reader = st_bound(loop, "_remove_reader");
    t->m_add_writer = st_bound(loop, "_add_writer");
    t->m_remove_writer = st_bound(loop, "_remove_writer");
    t->m_call_soon = st_bound(loop, "call_soon");
    t->read_ready = st_bound(op, "_read_ready");
    t->write_ready = st_bound(op, "_write_ready");
    t->conn_lost_cb = st_bound(op, "_call_connection_lost");
    if (!t->m_add_reader || !t->m_remove_reader || !t->m_add_writer ||
        !t->m_remove_writer || !t->m_call_soon || !t->read_ready ||
        !t->write_ready || !t->conn_lost_cb) {
        return -1;
    }

    t->wbuf = PyByteArray_FromStringAndSize("", 0);
    if (t->wbuf == NULL) {
        return -1;
    }
    t->whead = 0;
    t->cork_obj = NULL;
    t->writing = 0;
    t->cork = 0;
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
        return -1;
    }
    PyObject *sn = PyObject_CallMethod(sock, "getsockname", NULL);
    if (sn != NULL) {
        if (PyDict_SetItemString(t->extra, "sockname", sn) < 0) {
            Py_DECREF(sn);
            return -1;
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
                return -1;
            }
            Py_DECREF(pn);
        } else {
            PyErr_Clear();
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
                return -1;
            }
            Py_DECREF(connected);
        }
        PyObject *started = st_start_reading(op, NULL);
        if (started == NULL) {
            return -1;
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
        if (setres != NULL) {
            PyObject *r = PyObject_CallFunctionObjArgs(t->m_call_soon, setres, waiter, Py_None, NULL);
            Py_XDECREF(r);
            Py_DECREF(setres);
        }
    }
    return 0;
}

static int
st_traverse(PyObject *op, visitproc visit, void *arg)
{
    SocketTransport *t = (SocketTransport *)op;
    Py_VISIT(t->loop);
    Py_VISIT(t->poller_obj);
    Py_VISIT(t->sock);
    Py_VISIT(t->protocol);
    Py_VISIT(t->server);
    Py_VISIT(t->extra);
    Py_VISIT(t->wbuf);
    Py_VISIT(t->cork_obj);
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
    Py_CLEAR(t->poller_obj);
    Py_CLEAR(t->loop);
    Py_CLEAR(t->sock);
    Py_CLEAR(t->protocol);
    Py_CLEAR(t->server);
    Py_CLEAR(t->extra);
    Py_CLEAR(t->wbuf);
    Py_CLEAR(t->cork_obj);
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
    return PyBool_FromLong(((SocketTransport *)op)->fused_http1);
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

