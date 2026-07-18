#include "protocol.h"

#include "hydrate.h"

#include "buffer.h"
#include "codec.h"
#include "decode.h"
#include "plan.h"
#include "slab.h"
#include "tape.h"

#include <string.h>

static PyObject *str_discarded = NULL;
static PyObject *str_set_result = NULL;
static PyObject *str_done = NULL;
static PyObject *str_field_tape = NULL;
static PyObject *str_decoder_plan = NULL;
static PyObject *str_rows = NULL;
static PyObject *str_dest = NULL;
static PyObject *str_mode = NULL;
static PyObject *str_command = NULL;
static PyObject *kind_cache[256];

static PyObject *
kind_object(unsigned char kind)
{
    PyObject *cached = kind_cache[kind];
    if (cached == NULL) {
        cached = PyBytes_FromStringAndSize((const char *)&kind, 1);
        kind_cache[kind] = cached;
    }
    return cached;
}

/* Awaitable whose await completes immediately with a stored value, so a
   read_message() call that finds a pending message never suspends and
   allocates no future or coroutine machinery. */
typedef struct {
    PyObject_HEAD
    PyObject *value;
} WreathPgReadyMessage;

static PyTypeObject *ready_message_type = NULL;

static void
ready_message_dealloc(WreathPgReadyMessage *self)
{
    Py_CLEAR(self->value);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
ready_message_await(WreathPgReadyMessage *self)
{
    return Py_NewRef((PyObject *)self);
}

static PyObject *
ready_message_iternext(WreathPgReadyMessage *self)
{
    PyObject *exception;
    if (self->value == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "message awaitable already consumed");
        return NULL;
    }
    exception = PyObject_CallOneArg(PyExc_StopIteration, self->value);
    Py_CLEAR(self->value);
    if (exception == NULL) return NULL;
    PyErr_SetRaisedException(exception);
    return NULL;
}

static PyType_Slot ready_message_slots[] = {
    {Py_tp_dealloc, ready_message_dealloc},
    {Py_tp_iter, ready_message_await},
    {Py_tp_iternext, ready_message_iternext},
    {Py_am_await, ready_message_await},
    {0, NULL},
};

static PyType_Spec ready_message_spec = {
    .name = "wreath._native._postgres._ReadyMessage",
    .basicsize = sizeof(WreathPgReadyMessage),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    .slots = ready_message_slots,
};

/* Steals the reference to value. */
static PyObject *
ready_message_new(PyObject *value)
{
    WreathPgReadyMessage *self;
    self = (WreathPgReadyMessage *)ready_message_type->tp_alloc(ready_message_type, 0);
    if (self == NULL) {
        Py_DECREF(value);
        return NULL;
    }
    self->value = value;
    return (PyObject *)self;
}

static int
append_cstring(WreathPgBuffer *buffer, PyObject *value)
{
    const char *data;
    Py_ssize_t length;
    if (PyUnicode_Check(value)) {
        data = PyUnicode_AsUTF8AndSize(value, &length);
    } else if (PyBytes_Check(value)) {
        data = PyBytes_AS_STRING(value);
        length = PyBytes_GET_SIZE(value);
    } else {
        PyErr_SetString(PyExc_TypeError, "PostgreSQL string must be str or bytes");
        return -1;
    }
    if (data == NULL) return -1;
    if (memchr(data, 0, (size_t)length) != NULL) {
        PyErr_SetString(PyExc_ValueError, "PostgreSQL string contains NUL");
        return -1;
    }
    return wreath_pg_buffer_append(buffer, data, length) < 0 ||
           wreath_pg_buffer_append(buffer, "\0", 1) < 0 ? -1 : 0;
}

/* Complete wire messages with no variable content. */
static const char DESCRIBE_PORTAL_MESSAGE[7] = {'D', 0, 0, 0, 6, 'P', 0};
static const char EXECUTE_MESSAGE[10] = {'E', 0, 0, 0, 9, 0, 0, 0, 0, 0};
static const char SYNC_MESSAGE[5] = {'S', 0, 0, 0, 4};

static int
append_bind(WreathPgBuffer *output, PyObject *statement, PyObject *args,
            PyObject *oids, int binary_parameters, int binary_results)
{
    Py_ssize_t count;
    Py_ssize_t begin;

    if (!PyTuple_Check(args) || !PyTuple_Check(oids)) {
        PyErr_SetString(PyExc_TypeError, "arguments and OIDs must be tuples");
        return -1;
    }
    count = PyTuple_GET_SIZE(args);
    if (count != PyTuple_GET_SIZE(oids) || count > UINT16_MAX) {
        PyErr_SetString(PyExc_ValueError, "query argument count does not match plan");
        return -1;
    }
    begin = wreath_pg_buffer_begin_message(output, 'B');
    if (begin < 0) return -1;
    if (wreath_pg_buffer_append(output, "\0", 1) < 0 ||
        append_cstring(output, statement) < 0) return -1;
    if (binary_parameters && count > 0) {
        if (wreath_pg_buffer_u16(output, (uint16_t)count) < 0) return -1;
        for (Py_ssize_t i = 0; i < count; i++) {
            if (wreath_pg_buffer_u16(output, 1) < 0) return -1;
        }
    } else if (wreath_pg_buffer_u16(output, 0) < 0) return -1;
    if (wreath_pg_buffer_u16(output, (uint16_t)count) < 0) return -1;
    for (Py_ssize_t i = 0; i < count; i++) {
        unsigned long oid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(oids, i));
        if (oid == (unsigned long)-1 && PyErr_Occurred()) return -1;
        if (binary_parameters) {
            if (wreath_pg_encode_binary_into(
                    output, PyTuple_GET_ITEM(args, i), (uint32_t)oid) < 0) return -1;
        } else {
            PyObject *encoded = wreath_pg_encode_text_value(
                PyTuple_GET_ITEM(args, i), (uint32_t)oid
            );
            if (encoded == NULL) return -1;
            if (encoded == Py_None) {
                Py_DECREF(encoded);
                if (wreath_pg_buffer_i32(output, -1) < 0) return -1;
            } else {
                Py_ssize_t length = PyBytes_GET_SIZE(encoded);
                if (length > INT32_MAX ||
                    wreath_pg_buffer_u32(output, (uint32_t)length) < 0 ||
                    wreath_pg_buffer_append(
                        output, PyBytes_AS_STRING(encoded), length) < 0) {
                    Py_DECREF(encoded);
                    return -1;
                }
                Py_DECREF(encoded);
            }
        }
    }
    if (binary_results) {
        if (wreath_pg_buffer_u16(output, 1) < 0 || wreath_pg_buffer_u16(output, 1) < 0)
            return -1;
    } else if (wreath_pg_buffer_u16(output, 0) < 0) return -1;
    return wreath_pg_buffer_end_message(output, begin);
}

static int
parse_result_mode(PyObject *mode, int *execute)
{
    const char *value;
    if (!PyUnicode_Check(mode)) {
        PyErr_SetString(PyExc_ValueError, "unknown PostgreSQL result mode");
        return -1;
    }
    value = PyUnicode_AsUTF8(mode);
    if (value == NULL) return -1;
    if (strcmp(value, "execute") == 0) *execute = 1;
    else if (strcmp(value, "fetch") == 0 || strcmp(value, "fetchrow") == 0 ||
             strcmp(value, "fetchval") == 0) *execute = 0;
    else {
        PyErr_Format(PyExc_ValueError, "unknown PostgreSQL result mode %R", mode);
        return -1;
    }
    return 0;
}

static PyObject *
build_cold(PyObject *module, PyObject *args)
{
    PyObject *statement;
    PyObject *sql;
    PyObject *values;
    PyObject *oids;
    PyObject *mode;
    WreathPgBuffer output = {0};
    Py_ssize_t count;
    Py_ssize_t begin;
    PyObject *result = NULL;
    int execute;
    (void)module;

    if (!PyArg_ParseTuple(args, "OOOOO:_build_cold_query_packet", &statement,
                          &sql, &values, &oids, &mode)) return NULL;
    if (parse_result_mode(mode, &execute) < 0) return NULL;
    if (!PyTuple_Check(values) || PyTuple_GET_SIZE(values) > UINT16_MAX) {
        PyErr_SetString(PyExc_ValueError, "too many query arguments");
        return NULL;
    }
    count = PyTuple_GET_SIZE(values);
    begin = wreath_pg_buffer_begin_message(&output, 'P');
    if (begin < 0 || append_cstring(&output, statement) < 0 ||
        append_cstring(&output, sql) < 0 ||
        wreath_pg_buffer_u16(&output, (uint16_t)count) < 0) goto done;
    for (Py_ssize_t i = 0; i < count; i++) {
        if (wreath_pg_buffer_u32(&output, 0) < 0) goto done;
    }
    if (wreath_pg_buffer_end_message(&output, begin) < 0) goto done;
    begin = wreath_pg_buffer_begin_message(&output, 'D');
    if (begin < 0 || wreath_pg_buffer_append(&output, "S", 1) < 0 ||
        append_cstring(&output, statement) < 0 ||
        wreath_pg_buffer_end_message(&output, begin) < 0) goto done;
    if (append_bind(&output, statement, values, oids, 0, 0) < 0 ||
        (!execute && wreath_pg_buffer_append(
            &output, DESCRIBE_PORTAL_MESSAGE, sizeof(DESCRIBE_PORTAL_MESSAGE)) < 0) ||
        wreath_pg_buffer_append(&output, EXECUTE_MESSAGE, sizeof(EXECUTE_MESSAGE)) < 0 ||
        wreath_pg_buffer_append(&output, SYNC_MESSAGE, sizeof(SYNC_MESSAGE)) < 0)
        goto done;
    result = wreath_pg_buffer_finish(&output);

done:
    wreath_pg_buffer_clear(&output);
    return result;
}

static PyObject *
build_cached(PyObject *module, PyObject *args)
{
    PyObject *plan;
    PyObject *values;
    PyObject *mode;
    PyObject *statement = NULL;
    PyObject *oids = NULL;
    WreathPgBuffer output = {0};
    PyObject *result = NULL;
    int execute;
    (void)module;

    if (!PyArg_ParseTuple(args, "OOO:_build_cached_query_packet", &plan, &values, &mode))
        return NULL;
    if (parse_result_mode(mode, &execute) < 0) return NULL;
    if (WreathPgPlanType != NULL && PyObject_TypeCheck(plan, WreathPgPlanType)) {
        statement = Py_NewRef(((WreathPgPlan *)plan)->statement_name);
        oids = Py_NewRef(((WreathPgPlan *)plan)->parameter_oids);
    } else {
        statement = PyObject_GetAttrString(plan, "statement_name");
        oids = PyObject_GetAttrString(plan, "parameter_oids");
    }
    if (statement == NULL || oids == NULL) goto done;
    if (append_bind(&output, statement, values, oids, 1, !execute) < 0 ||
        wreath_pg_buffer_append(&output, EXECUTE_MESSAGE, sizeof(EXECUTE_MESSAGE)) < 0 ||
        wreath_pg_buffer_append(&output, SYNC_MESSAGE, sizeof(SYNC_MESSAGE)) < 0)
        goto done;
    result = wreath_pg_buffer_finish(&output);

done:
    Py_XDECREF(statement);
    Py_XDECREF(oids);
    wreath_pg_buffer_clear(&output);
    return result;
}

static PyObject *
join_pipeline_packets(PyObject *module, PyObject *packets)
{
    WreathPgBuffer output = {0};
    Py_ssize_t count;
    (void)module;
    if (!PyTuple_Check(packets)) {
        PyErr_SetString(PyExc_TypeError, "pipeline packets must be a tuple");
        return NULL;
    }
    count = PyTuple_GET_SIZE(packets);
    if (count == 1 && PyBytes_Check(PyTuple_GET_ITEM(packets, 0))) {
        return Py_NewRef(PyTuple_GET_ITEM(packets, 0));
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *packet = PyTuple_GET_ITEM(packets, i);
        if (!PyBytes_Check(packet)) {
            wreath_pg_buffer_clear(&output);
            PyErr_SetString(PyExc_TypeError, "pipeline packet must be bytes");
            return NULL;
        }
        if (wreath_pg_buffer_append(
                &output, PyBytes_AS_STRING(packet), PyBytes_GET_SIZE(packet)) < 0) {
            wreath_pg_buffer_clear(&output);
            return NULL;
        }
    }
    return wreath_pg_buffer_finish(&output);
}

typedef struct {
    PyObject_HEAD
    PyObject *transport;
    PyObject *transport_write;
    /* Control messages queue through an indexed list plus at most one waiter
       future; DataRow frames never come through here (they stay on the
       slab/tape path). */
    PyObject *messages;
    Py_ssize_t messages_head; /* first unconsumed message; see the queue rule */
    PyObject *read_waiter;
    PyObject *create_future;
    PyObject *closed_future;
    PyObject *done_future;
    PyObject *slabs;
    PyObject *spares;
    /* Slabs consumed while a DataRow memoryview still pins them. A long-lived
       decode can pin many at once, so reclamation scans a bounded slice per
       call and rotates through the list instead of restarting at zero. */
    PyObject *retired;
    Py_ssize_t retired_scan;    /* where the next bounded scan resumes */
    Py_ssize_t retired_scan_steps;  /* entries inspected; test/benchmark only */
    Py_ssize_t retired_reclaims;    /* slabs removed; test/benchmark only */
    Py_ssize_t retired_move_steps;  /* unordered replacements; test/benchmark only */
    /* In-flight pipelined operations, kept in lockstep and drained in order as
       ReadyForQuery arrives. One head index serves both: deleting index 0 per
       completion shifted every operation still in flight, which is quadratic in
       the pipeline depth, and depth is whatever the caller submits at once. */
    PyObject *operations;
    PyObject *operation_contexts;
    Py_ssize_t operations_head;   /* first in-flight operation */
    /* Cache of the head operation context so per-DataRow dispatch avoids
       tuple indexing and attribute lookups; cached_context is a strong
       reference, the tape/plan/rows fields borrow from it. */
    PyObject *cached_context;
    PyObject *cached_tape;
    PyObject *cached_plan;
    PyObject *cached_rows;
    PyObject *cached_dest;
    long cached_mode;
    int cached_discarded;
    int cached_usable;
    WreathPgSlab *current;
    Py_ssize_t slab_allocations;
    Py_ssize_t chained_messages;
    Py_ssize_t direct_data_rows;
    Py_ssize_t queued_messages;
    Py_ssize_t write_calls;
    Py_ssize_t pause_writing_calls;
    Py_ssize_t resume_writing_calls;
    Py_ssize_t backpressure_waits;
    int write_paused;
} WreathPgBufferedProtocol;

static PyTypeObject *buffered_protocol_type = NULL;

/* --- indexed queue ------------------------------------------------------- */
/* An owned list plus a head index. Taking the front is O(1) and the consumed
 * prefix is dropped in one slice, rather than PySequence_DelItem(list, 0)
 * shifting every remaining element on every single take. The logical length is
 * always size - head; the raw list length is never the answer. */

static Py_ssize_t
queue_len(PyObject *list, Py_ssize_t head)
{
    return list == NULL ? 0 : PyList_GET_SIZE(list) - head;
}

/* Take a strong reference to the front item, then advance and maybe compact.
 * The reference is taken before any compaction, so the returned object is never
 * a borrowed pointer into a prefix that is about to be released. */
static PyObject *
queue_pop(PyObject *list, Py_ssize_t *head)
{
    PyObject *item = Py_NewRef(PyList_GET_ITEM(list, *head));
    (*head)++;
    Py_ssize_t size = PyList_GET_SIZE(list);
    if (*head >= size) {
        if (PyList_SetSlice(list, 0, size, NULL) < 0) {
            Py_DECREF(item);
            return NULL;
        }
        *head = 0;
    } else if (*head >= 64 && *head * 2 >= size) {
        if (PyList_SetSlice(list, 0, *head, NULL) < 0) {
            Py_DECREF(item);
            return NULL;
        }
        *head = 0;
    }
    return item;
}

/* Retire the head operation and its context together.
 *
 * The two lists are index-parallel, so one head serves both. Like the control
 * queue, the consumed prefix is dropped in one slice rather than shifting the
 * whole in-flight pipeline on every completion. */
static int
operations_advance(WreathPgBufferedProtocol *self)
{
    self->operations_head++;
    Py_ssize_t size = PyList_GET_SIZE(self->operations);
    if (self->operations_head >= size) {
        if (PyList_SetSlice(self->operations, 0, size, NULL) < 0 ||
            PyList_SetSlice(self->operation_contexts, 0,
                            PyList_GET_SIZE(self->operation_contexts), NULL) < 0) {
            return -1;
        }
        self->operations_head = 0;
    }
    else if (self->operations_head >= 64 && self->operations_head * 2 >= size) {
        Py_ssize_t drop = self->operations_head;
        if (PyList_SetSlice(self->operations, 0, drop, NULL) < 0 ||
            PyList_SetSlice(self->operation_contexts, 0, drop, NULL) < 0) {
            return -1;
        }
        self->operations_head = 0;
    }
    return 0;
}

static int
buffered_traverse(WreathPgBufferedProtocol *self, visitproc visit, void *arg)
{
    Py_VISIT(self->transport);
    Py_VISIT(self->transport_write);
    Py_VISIT(self->messages);
    Py_VISIT(self->read_waiter);
    Py_VISIT(self->create_future);
    Py_VISIT(self->closed_future);
    Py_VISIT(self->done_future);
    Py_VISIT(self->slabs);
    Py_VISIT(self->spares);
    Py_VISIT(self->retired);
    Py_VISIT(self->operations);
    Py_VISIT(self->operation_contexts);
    Py_VISIT(self->cached_context);
    return 0;
}

static void
invalidate_context_cache(WreathPgBufferedProtocol *self)
{
    Py_CLEAR(self->cached_context);
    self->cached_tape = NULL;
    self->cached_plan = NULL;
    self->cached_rows = NULL;
    self->cached_dest = NULL;
    self->cached_usable = 0;
}

static int
buffered_clear(WreathPgBufferedProtocol *self)
{
    Py_CLEAR(self->transport);
    Py_CLEAR(self->transport_write);
    Py_CLEAR(self->messages);
    self->messages_head = 0;
    Py_CLEAR(self->read_waiter);
    Py_CLEAR(self->create_future);
    Py_CLEAR(self->closed_future);
    Py_CLEAR(self->done_future);
    Py_CLEAR(self->slabs);
    Py_CLEAR(self->spares);
    Py_CLEAR(self->retired);
    self->retired_scan = 0;
    Py_CLEAR(self->operations);
    Py_CLEAR(self->operation_contexts);
    self->operations_head = 0;
    invalidate_context_cache(self);
    self->current = NULL;
    return 0;
}

static void
buffered_dealloc(WreathPgBufferedProtocol *self)
{
    PyObject_GC_UnTrack(self);
    buffered_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
add_spare(WreathPgBufferedProtocol *self)
{
    WreathPgSlab *slab = wreath_pg_slab_new();
    int result;
    if (slab == NULL) return -1;
    self->slab_allocations++;
    result = PyList_Append(self->spares, (PyObject *)slab);
    Py_DECREF(slab);
    return result;
}

/* Delete an entry without shifting the list suffix. PyList_SetItem steals the
 * new reference and releases the displaced list reference; the temporary
 * INCREF keeps the borrowed final item alive until its old slot is deleted. */
static int
retired_swap_delete(PyObject *list, Py_ssize_t index)
{
    Py_ssize_t final = PyList_GET_SIZE(list) - 1;
    if (index == final) return PySequence_DelItem(list, final);

    PyObject *replacement = PyList_GET_ITEM(list, final);
    Py_INCREF(replacement);
    if (PyList_SetItem(list, index, replacement) < 0) return -1;
    return PySequence_DelItem(list, final);
}

/* Reclaim released slabs, inspecting at most `budget` entries.
 *
 * A pinned prefix must not be rescanned on every receive: with many slabs held
 * by live memoryviews, a full scan makes each cycle cost O(pinned). The cursor
 * rotates, so every entry is still examined eventually, just spread across
 * calls. A negative budget means "scan the whole list once" for the paths that
 * genuinely need an answer now. */
static int
reclaim_retired(WreathPgBufferedProtocol *self, Py_ssize_t budget)
{
    Py_ssize_t size = PyList_GET_SIZE(self->retired);
    if (size == 0) {
        self->retired_scan = 0;
        return 0;
    }
    Py_ssize_t remaining = budget < 0 ? size : (budget < size ? budget : size);
    if (self->retired_scan >= size) {
        self->retired_scan = 0;  /* the list shrank under the cursor */
    }
    while (remaining-- > 0) {
        WreathPgSlab *slab = (WreathPgSlab *)PyList_GET_ITEM(self->retired, self->retired_scan);
        self->retired_scan_steps++;
        if (Py_REFCNT(slab) == 1) {
            slab->read_position = 0;
            slab->write_position = 0;
            if (PyList_GET_SIZE(self->spares) < 4 &&
                PyList_Append(self->spares, (PyObject *)slab) < 0) return -1;
            int moved = self->retired_scan != PyList_GET_SIZE(self->retired) - 1;
            if (retired_swap_delete(self->retired, self->retired_scan) < 0) return -1;
            self->retired_reclaims++;
            self->retired_move_steps += moved;
            /* The final entry moved into this slot: inspect it without advancing. */
            size--;
            if (size == 0) {
                self->retired_scan = 0;
                return 0;
            }
            if (self->retired_scan >= size) {
                self->retired_scan = 0;
            }
        }
        else {
            self->retired_scan++;
            if (self->retired_scan >= size) {
                self->retired_scan = 0;  /* rotate */
            }
        }
    }
    return 0;
}

static int
ensure_spares(WreathPgBufferedProtocol *self)
{
    /* Only look for reclaimable slabs when there is no spare to hand out, and
       stop as soon as two exist rather than walking the whole pinned list. */
    if (PyList_GET_SIZE(self->spares) == 0 &&
        reclaim_retired(self, PyList_GET_SIZE(self->retired)) < 0) {
        return -1;
    }
    while (PyList_GET_SIZE(self->spares) < 2) {
        if (add_spare(self) < 0) return -1;
    }
    return 0;
}

static WreathPgSlab *
acquire_slab(WreathPgBufferedProtocol *self)
{
    WreathPgSlab *slab;
    Py_ssize_t count = PyList_GET_SIZE(self->spares);
    if (count == 0 && add_spare(self) < 0) return NULL;
    count = PyList_GET_SIZE(self->spares);
    slab = (WreathPgSlab *)PyList_GET_ITEM(self->spares, count - 1);
    Py_INCREF(slab);
    if (PySequence_DelItem(self->spares, count - 1) < 0) {
        Py_DECREF(slab);
        return NULL;
    }
    slab->read_position = 0;
    slab->write_position = 0;
    if (PyList_Append(self->slabs, (PyObject *)slab) < 0) {
        Py_DECREF(slab);
        return NULL;
    }
    Py_DECREF(slab);
    self->current = slab;
    if (ensure_spares(self) < 0) return NULL;
    return slab;
}

static PyObject *
buffered_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    WreathPgBufferedProtocol *self;
    PyObject *asyncio_module;
    PyObject *loop;
    (void)args;
    (void)kwargs;

    self = (WreathPgBufferedProtocol *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->slabs = PyList_New(0);
    self->spares = PyList_New(0);
    self->retired = PyList_New(0);
    self->retired_scan = 0;
    self->retired_scan_steps = 0;
    self->retired_reclaims = 0;
    self->retired_move_steps = 0;
    self->operations = PyList_New(0);
    self->operation_contexts = PyList_New(0);
    self->operations_head = 0;
    self->messages = PyList_New(0);
    self->messages_head = 0;
    asyncio_module = PyImport_ImportModule("asyncio");
    loop = asyncio_module == NULL ? NULL : PyObject_CallMethod(asyncio_module, "get_running_loop", NULL);
    self->create_future = loop == NULL ? NULL : PyObject_GetAttrString(loop, "create_future");
    self->closed_future = self->create_future == NULL ? NULL :
        PyObject_CallNoArgs(self->create_future);
    self->done_future = self->create_future == NULL ? NULL :
        PyObject_CallNoArgs(self->create_future);
    Py_XDECREF(loop);
    Py_XDECREF(asyncio_module);
    if (self->slabs == NULL || self->spares == NULL || self->retired == NULL ||
        self->operations == NULL || self->operation_contexts == NULL ||
        self->messages == NULL || self->create_future == NULL ||
        self->closed_future == NULL || self->done_future == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    {
        PyObject *resolved = PyObject_CallMethod(
            self->done_future, "set_result", "O", Py_None
        );
        if (resolved == NULL || ensure_spares(self) < 0) {
            Py_XDECREF(resolved);
            Py_DECREF(self);
            return NULL;
        }
        Py_DECREF(resolved);
    }
    return (PyObject *)self;
}

static Py_ssize_t
available_bytes(WreathPgBufferedProtocol *self)
{
    Py_ssize_t total = 0;
    Py_ssize_t count = PyList_GET_SIZE(self->slabs);
    for (Py_ssize_t i = 0; i < count; i++) {
        WreathPgSlab *slab = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, i);
        total += slab->write_position - slab->read_position;
    }
    return total;
}

static int
peek_bytes(WreathPgBufferedProtocol *self, Py_ssize_t offset, unsigned char *out,
           Py_ssize_t length)
{
    Py_ssize_t count = PyList_GET_SIZE(self->slabs);
    for (Py_ssize_t i = 0; i < count && length > 0; i++) {
        WreathPgSlab *slab = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, i);
        Py_ssize_t available = slab->write_position - slab->read_position;
        Py_ssize_t take;
        if (offset >= available) {
            offset -= available;
            continue;
        }
        take = available - offset;
        if (take > length) take = length;
        memcpy(out, slab->data + slab->read_position + offset, (size_t)take);
        out += take;
        length -= take;
        offset = 0;
    }
    return length == 0 ? 0 : -1;
}

static PyObject *
payload_object(WreathPgBufferedProtocol *self, Py_ssize_t offset,
               Py_ssize_t length, int retain_view)
{
    Py_ssize_t count = PyList_GET_SIZE(self->slabs);
    for (Py_ssize_t i = 0; i < count; i++) {
        WreathPgSlab *slab = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, i);
        Py_ssize_t available = slab->write_position - slab->read_position;
        if (offset >= available) {
            offset -= available;
            continue;
        }
        if (length <= available - offset) {
            PyObject *view = wreath_pg_slab_view(
                slab, slab->read_position + offset, length
            );
            PyObject *result;
            if (view == NULL || retain_view) return view;
            result = PyBytes_FromObject(view);
            Py_DECREF(view);
            return result;
        }
        break;
    }
    self->chained_messages++;
    {
        PyObject *result = PyBytes_FromStringAndSize(NULL, length);
        PyObject *owners;
        PyObject *view;
        if (result == NULL) return NULL;
        if (peek_bytes(self, offset, (unsigned char *)PyBytes_AS_STRING(result), length) < 0) {
            Py_DECREF(result);
            PyErr_SetString(PyExc_RuntimeError, "incomplete chained PostgreSQL message");
            return NULL;
        }
        if (!retain_view) return result;
        owners = PyList_AsTuple(self->slabs);
        if (owners == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        view = wreath_pg_chained_payload(
            owners, PyBytes_AS_STRING(result), PyBytes_GET_SIZE(result)
        );
        Py_DECREF(owners);
        Py_DECREF(result);
        return view;
    }
}

static int
consume_bytes(WreathPgBufferedProtocol *self, Py_ssize_t length)
{
    while (length > 0) {
        WreathPgSlab *slab;
        Py_ssize_t available;
        if (PyList_GET_SIZE(self->slabs) == 0) return -1;
        slab = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, 0);
        available = slab->write_position - slab->read_position;
        if (length < available) {
            slab->read_position += length;
            return 0;
        }
        length -= available;
        if (self->current == slab) self->current = NULL;
        if (Py_REFCNT(slab) == 1 && PyList_GET_SIZE(self->spares) < 4) {
            slab->read_position = 0;
            slab->write_position = 0;
            if (PyList_Append(self->spares, (PyObject *)slab) < 0) return -1;
        } else if (Py_REFCNT(slab) > 1) {
            if (PyList_Append(self->retired, (PyObject *)slab) < 0) return -1;
        }
        /* native-lint: allow NC001 -- self->slabs holds only the slabs backing
           currently-unparsed bytes, so this list is a handful of entries; it is
           not a queue that grows with traffic. */
        if (PySequence_DelItem(self->slabs, 0) < 0) return -1;
    }
    return 0;
}

/* Locate the slab whose unread window contains [offset, offset + length)
   contiguously; NULL (without an exception) when the range spans slabs. */
static WreathPgSlab *
locate_contiguous(WreathPgBufferedProtocol *self, Py_ssize_t offset,
                  Py_ssize_t length, Py_ssize_t *absolute_offset)
{
    Py_ssize_t count = PyList_GET_SIZE(self->slabs);
    for (Py_ssize_t i = 0; i < count; i++) {
        WreathPgSlab *slab = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, i);
        Py_ssize_t available = slab->write_position - slab->read_position;
        if (offset >= available) {
            offset -= available;
            continue;
        }
        if (length <= available - offset) {
            *absolute_offset = slab->read_position + offset;
            return slab;
        }
        return NULL;
    }
    return NULL;
}

static int
direct_data_row(WreathPgBufferedProtocol *self, Py_ssize_t payload_length,
                WreathPgSlab *slab, Py_ssize_t absolute_offset)
{
    PyObject *context;
    WreathPgFieldTape *tape;
    unsigned int selected;

    if (queue_len(self->operation_contexts, self->operations_head) == 0) return 0;
    context = PyList_GET_ITEM(self->operation_contexts, self->operations_head);
    if (context != self->cached_context) {
        PyObject *operation = PyTuple_GET_ITEM(context, 0);
        PyObject *tape_object = PyTuple_GET_ITEM(context, 1);
        PyObject *plan_object = PyTuple_GET_ITEM(context, 2);
        PyObject *discarded;
        long mode = PyLong_AsLong(PyTuple_GET_ITEM(context, 4));
        if (mode == -1 && PyErr_Occurred()) return -1;
        discarded = PyObject_GetAttr(operation, str_discarded);
        if (discarded == NULL) return -1;
        invalidate_context_cache(self);
        self->cached_context = Py_NewRef(context);
        self->cached_mode = mode;
        self->cached_discarded = discarded == Py_True;
        Py_DECREF(discarded);
        self->cached_tape = tape_object;
        self->cached_plan = plan_object;
        self->cached_rows = PyTuple_GET_ITEM(context, 3);
        self->cached_dest = PyTuple_GET_ITEM(context, 5);
        self->cached_usable = tape_object != Py_None && plan_object != Py_None &&
            PyObject_TypeCheck(tape_object, WreathPgFieldTapeType) &&
            PyObject_TypeCheck(plan_object, WreathPgDecoderPlanType);
    }
    /* The discarded flag is sampled once per operation: rows accepted after
       a late cancellation are still dropped by the driver, which re-checks
       the flag before decoding batches and publishing results. */
    if (self->cached_discarded) return 1;
    if (!self->cached_usable) return 0;
    if (self->cached_mode == 0) return 1;
    tape = (WreathPgFieldTape *)self->cached_tape;
    if ((self->cached_mode == 2 || self->cached_mode == 3) && tape->row_count > 0)
        return 1;
    selected = self->cached_mode == 3 ? 1 : tape->source_columns;
    if (slab == NULL)
        slab = locate_contiguous(self, 5, payload_length, &absolute_offset);
    if (slab != NULL) {
        if (wreath_pg_tape_append_raw(
                tape, (PyObject *)slab, slab->data + absolute_offset,
                payload_length, absolute_offset, selected) < 0) return -1;
    } else {
        PyObject *payload = payload_object(self, 5, payload_length, 1);
        int appended;
        if (payload == NULL) return -1;
        appended = wreath_pg_tape_append_payload(tape, payload, selected);
        Py_DECREF(payload);
        if (appended < 0) return -1;
    }
    if (self->cached_mode == 1 && tape->row_count >= 256) {
        if (self->cached_dest != NULL && self->cached_dest != Py_None) {
            if (!PyTuple_Check(self->cached_dest) ||
                PyTuple_GET_SIZE(self->cached_dest) != 3) {
                PyErr_SetString(PyExc_TypeError,
                                "a decode destination is (plan, identity_map, owner)");
                return -1;
            }
            if (wreath_pg_hydrate_models(
                    self->cached_plan, self->cached_tape,
                    PyTuple_GET_ITEM(self->cached_dest, 0), 256, self->cached_rows,
                    PyTuple_GET_ITEM(self->cached_dest, 1),
                    PyTuple_GET_ITEM(self->cached_dest, 2)) < 0) return -1;
        } else if (wreath_pg_decode_fetch_extend(
                       self->cached_plan, self->cached_tape, 256,
                       self->cached_rows) < 0) {
            return -1;
        }
    }
    return 1;
}

/* Hand a control message to the waiting reader, or park it until one
   arrives. Borrows item. */
static int
deliver_message(WreathPgBufferedProtocol *self, PyObject *item)
{
    if (self->read_waiter != NULL) {
        PyObject *waiter = self->read_waiter;
        PyObject *done;
        self->read_waiter = NULL;
        done = PyObject_CallMethodNoArgs(waiter, str_done);
        if (done == NULL) {
            Py_DECREF(waiter);
            return -1;
        }
        if (done == Py_False) {
            PyObject *set = PyObject_CallMethodOneArg(waiter, str_set_result, item);
            Py_DECREF(done);
            Py_DECREF(waiter);
            if (set == NULL) return -1;
            Py_DECREF(set);
            return 0;
        }
        /* The reader was cancelled; keep the message for the next call. */
        Py_DECREF(done);
        Py_DECREF(waiter);
    }
    return PyList_Append(self->messages, item);
}

static int
parse_messages(WreathPgBufferedProtocol *self)
{
    unsigned char header[5];
    for (;;) {
        WreathPgSlab *first = NULL;
        Py_ssize_t first_available = 0;
        Py_ssize_t total = -1;
        const unsigned char *head;
        uint32_t length;
        Py_ssize_t wire_length;
        unsigned char kind_byte;
        int contiguous;
        PyObject *kind;
        PyObject *payload;
        PyObject *item;

        /* Ordinary messages live entirely inside the first slab, so the
           header is read with pointer arithmetic; the multi-slab peek only
           runs when a message straddles a slab boundary. */
        if (PyList_GET_SIZE(self->slabs) > 0) {
            first = (WreathPgSlab *)PyList_GET_ITEM(self->slabs, 0);
            first_available = first->write_position - first->read_position;
        }
        if (first_available >= 5) {
            head = first->data + first->read_position;
        } else {
            total = available_bytes(self);
            if (total < 5) return 0;
            if (peek_bytes(self, 0, header, 5) < 0) return -1;
            head = header;
        }
        kind_byte = head[0];
        length = ((uint32_t)head[1] << 24) | ((uint32_t)head[2] << 16) |
                 ((uint32_t)head[3] << 8) | head[4];
        if (length < 4 || length > 64U * 1024U * 1024U) {
            PyErr_SetString(PyExc_ValueError, "invalid PostgreSQL message length");
            return -1;
        }
        wire_length = (Py_ssize_t)length + 1;
        contiguous = first_available >= wire_length;
        if (!contiguous) {
            if (total < 0) total = available_bytes(self);
            if (total < wire_length) return 0;
        }
        if (kind_byte == '1' || kind_byte == '2' || kind_byte == 'n' ||
            kind_byte == 's') {
            if (consume_bytes(self, wire_length) < 0) return -1;
            continue;
        }
        if (kind_byte == 'C' &&
            queue_len(self->operation_contexts, self->operations_head) > 0) {
            PyObject *context =
                PyList_GET_ITEM(self->operation_contexts, self->operations_head);
            long mode = PyLong_AsLong(PyTuple_GET_ITEM(context, 4));
            if (mode == -1 && PyErr_Occurred()) return -1;
            if (mode == 0) {
                PyObject *payload_object_value = payload_object(
                    self, 5, (Py_ssize_t)length - 4, 0
                );
                PyObject *command;
                Py_ssize_t command_length;
                if (payload_object_value == NULL) return -1;
                command_length = PyBytes_GET_SIZE(payload_object_value);
                while (command_length > 0 &&
                       PyBytes_AS_STRING(payload_object_value)[command_length - 1] == '\0')
                    command_length--;
                command = PyUnicode_DecodeUTF8(
                    PyBytes_AS_STRING(payload_object_value), command_length, "replace"
                );
                Py_DECREF(payload_object_value);
                if (command == NULL || PyObject_SetAttr(
                        PyTuple_GET_ITEM(context, 0), str_command, command) < 0) {
                    Py_XDECREF(command);
                    return -1;
                }
                Py_DECREF(command);
                if (consume_bytes(self, wire_length) < 0) return -1;
                continue;
            }
        }
        if (kind_byte == 'D') {
            int handled = direct_data_row(
                self, (Py_ssize_t)length - 4,
                contiguous ? first : NULL,
                contiguous ? first->read_position + 5 : 0
            );
            if (handled < 0) return -1;
            if (handled) {
                self->direct_data_rows++;
                if (consume_bytes(self, wire_length) < 0) return -1;
                continue;
            }
        }
        kind = kind_object(kind_byte);
        payload = payload_object(self, 5, length - 4, kind_byte == 'D');
        if (kind == NULL || payload == NULL) {
            Py_XDECREF(payload);
            return -1;
        }
        item = PyTuple_Pack(2, kind, payload);
        Py_DECREF(payload);
        if (item == NULL) return -1;
        if (deliver_message(self, item) < 0) {
            Py_DECREF(item);
            return -1;
        }
        self->queued_messages++;
        Py_DECREF(item);
        if (consume_bytes(self, wire_length) < 0) return -1;
        if (kind_byte == 'Z' &&
            queue_len(self->operations, self->operations_head) > 0) {
            invalidate_context_cache(self);
            if (operations_advance(self) < 0) return -1;
        }
    }
}

static PyObject *
buffered_get_buffer(WreathPgBufferedProtocol *self, PyObject *sizehint)
{
    (void)sizehint;
    /* A small fixed budget: this runs on every socket read, so it must not
       scale with the number of pinned slabs. The cursor rotates, so pinned
       entries are re-examined on later reads. */
    if (reclaim_retired(self, 8) < 0) return NULL;
    if (self->current == NULL || self->current->write_position == WREATH_PG_SLAB_SIZE) {
        if (acquire_slab(self) == NULL) return NULL;
    }
    return wreath_pg_slab_writable_view(self->current);
}

static PyObject *
buffered_buffer_updated(WreathPgBufferedProtocol *self, PyObject *count_object)
{
    Py_ssize_t count = PyLong_AsSsize_t(count_object);
    if (count == -1 && PyErr_Occurred()) return NULL;
    if (self->current == NULL || count < 0 ||
        count > WREATH_PG_SLAB_SIZE - self->current->write_position) {
        PyErr_SetString(PyExc_ValueError, "invalid buffered receive count");
        return NULL;
    }
    self->current->write_position += count;
    if (parse_messages(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
buffered_connection_made(WreathPgBufferedProtocol *self, PyObject *transport)
{
    PyObject *write = PyObject_GetAttrString(transport, "write");
    if (write == NULL) return NULL;
    Py_XSETREF(self->transport, Py_NewRef(transport));
    Py_XSETREF(self->transport_write, write);
    Py_RETURN_NONE;
}

static PyObject *
buffered_connection_lost(WreathPgBufferedProtocol *self, PyObject *error)
{
    PyObject *done;
    PyObject *result;
    (void)error;
    self->write_paused = 0;
    done = PyObject_CallMethodNoArgs(self->done_future, str_done);
    if (done == NULL) return NULL;
    if (done == Py_False) {
        Py_DECREF(done);
        result = PyObject_CallMethodOneArg(self->done_future, str_set_result, Py_None);
        if (result == NULL) return NULL;
        Py_DECREF(result);
    } else {
        Py_DECREF(done);
    }
    done = PyObject_CallMethod(self->closed_future, "done", NULL);
    if (done == NULL) return NULL;
    if (done == Py_False) {
        Py_DECREF(done);
        result = PyObject_CallMethod(self->closed_future, "set_result", "O", Py_None);
        if (result == NULL) return NULL;
        Py_DECREF(result);
    } else {
        Py_DECREF(done);
    }
    Py_RETURN_NONE;
}

static PyObject *
buffered_register_operations(WreathPgBufferedProtocol *self, PyObject *operations)
{
    Py_ssize_t count;
    if (!PyTuple_Check(operations)) {
        PyErr_SetString(PyExc_TypeError, "pipeline operations must be a tuple");
        return NULL;
    }
    count = PyTuple_GET_SIZE(operations);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *operation = PyTuple_GET_ITEM(operations, i);
        PyObject *tape = PyObject_GetAttr(operation, str_field_tape);
        PyObject *plan = PyObject_GetAttr(operation, str_decoder_plan);
        PyObject *rows = PyObject_GetAttr(operation, str_rows);
        PyObject *mode_object = PyObject_GetAttr(operation, str_mode);
        PyObject *dest = PyObject_GetAttr(operation, str_dest);
        PyObject *mode_number = NULL;
        PyObject *context = NULL;
        const char *mode;
        long mode_code;
        if (tape == NULL || plan == NULL || rows == NULL || mode_object == NULL ||
            dest == NULL)
            goto context_error;
        mode = PyUnicode_AsUTF8(mode_object);
        if (mode == NULL) goto context_error;
        if (strcmp(mode, "execute") == 0) mode_code = 0;
        else if (strcmp(mode, "fetch") == 0) mode_code = 1;
        else if (strcmp(mode, "fetchrow") == 0) mode_code = 2;
        else if (strcmp(mode, "fetchval") == 0) mode_code = 3;
        else {
            PyErr_SetString(PyExc_ValueError, "unknown PostgreSQL result mode");
            goto context_error;
        }
        mode_number = PyLong_FromLong(mode_code);
        if (mode_number == NULL) goto context_error;
        context = PyTuple_Pack(6, operation, tape, plan, rows, mode_number, dest);
        if (context == NULL || PyList_Append(self->operations, operation) < 0 ||
            PyList_Append(self->operation_contexts, context) < 0) {
            if (PyList_GET_SIZE(self->operations) >
                PyList_GET_SIZE(self->operation_contexts))
                PySequence_DelItem(
                    self->operations, PyList_GET_SIZE(self->operations) - 1
                );
            goto context_error;
        }
        Py_DECREF(context);
        Py_DECREF(mode_number);
        Py_DECREF(mode_object);
        Py_DECREF(dest);
        Py_DECREF(rows);
        Py_DECREF(plan);
        Py_DECREF(tape);
        continue;

context_error:
        Py_XDECREF(context);
        Py_XDECREF(mode_number);
        Py_XDECREF(mode_object);
        Py_XDECREF(dest);
        Py_XDECREF(rows);
        Py_XDECREF(plan);
        Py_XDECREF(tape);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
buffered_read_message(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    if (queue_len(self->messages, self->messages_head) > 0) {
        PyObject *item = queue_pop(self->messages, &self->messages_head);
        if (item == NULL) {
            return NULL;
        }
        return ready_message_new(item);
    }
    if (self->read_waiter != NULL) {
        /* A previous reader may have been cancelled while waiting. */
        PyObject *done = PyObject_CallMethodNoArgs(self->read_waiter, str_done);
        if (done == NULL) return NULL;
        if (done == Py_False) {
            Py_DECREF(done);
            PyErr_SetString(
                PyExc_RuntimeError, "read_message is already awaited elsewhere"
            );
            return NULL;
        }
        Py_DECREF(done);
        Py_CLEAR(self->read_waiter);
    }
    self->read_waiter = PyObject_CallNoArgs(self->create_future);
    if (self->read_waiter == NULL) return NULL;
    return Py_NewRef(self->read_waiter);
}

static PyObject *
buffered_write(WreathPgBufferedProtocol *self, PyObject *data)
{
    PyObject *result;
    if (self->transport_write == NULL) {
        PyErr_SetString(PyExc_ConnectionError, "transport is not connected");
        return NULL;
    }
    result = PyObject_CallOneArg(self->transport_write, data);
    if (result != NULL) self->write_calls++;
    return result;
}

static PyObject *
buffered_write_with_backpressure(WreathPgBufferedProtocol *self, PyObject *data)
{
    PyObject *result = buffered_write(self, data);
    if (result == NULL) return NULL;
    Py_DECREF(result);
    if (!self->write_paused) Py_RETURN_NONE;
    self->backpressure_waits++;
    return Py_NewRef(self->done_future);
}

static PyObject *
buffered_pause_writing(WreathPgBufferedProtocol *self, PyObject *unused)
{
    PyObject *future;
    (void)unused;
    if (self->write_paused) Py_RETURN_NONE;
    future = PyObject_CallNoArgs(self->create_future);
    if (future == NULL) return NULL;
    Py_SETREF(self->done_future, future);
    self->write_paused = 1;
    self->pause_writing_calls++;
    Py_RETURN_NONE;
}

static PyObject *
buffered_resume_writing(WreathPgBufferedProtocol *self, PyObject *unused)
{
    PyObject *result;
    (void)unused;
    if (!self->write_paused) Py_RETURN_NONE;
    self->write_paused = 0;
    self->resume_writing_calls++;
    result = PyObject_CallMethodOneArg(self->done_future, str_set_result, Py_None);
    if (result == NULL) return NULL;
    Py_DECREF(result);
    Py_RETURN_NONE;
}

static PyObject *
buffered_drain(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    return Py_NewRef(self->done_future);
}

static PyObject *
buffered_close(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    if (self->transport == NULL) Py_RETURN_NONE;
    return PyObject_CallMethod(self->transport, "close", NULL);
}

static PyObject *
buffered_wait_closed(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    return Py_NewRef(self->closed_future);
}

static PyObject *
buffered_is_closing(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    if (self->transport == NULL) Py_RETURN_TRUE;
    return PyObject_CallMethod(self->transport, "is_closing", NULL);
}

static PyObject *
buffered_stats(WreathPgBufferedProtocol *self, PyObject *unused)
{
    (void)unused;
    return Py_BuildValue(
        "{s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n}",
        "slab_allocations", self->slab_allocations,
        "active_slabs", PyList_GET_SIZE(self->slabs),
        "idle_slabs", PyList_GET_SIZE(self->spares),
        "retired_slabs", PyList_GET_SIZE(self->retired),
        /* Cumulative retired entries inspected. Exposed so tests can assert the
           per-receive scan budget without timing anything. */
        "retired_scan_steps", self->retired_scan_steps,
        "retired_reclaims", self->retired_reclaims,
        "retired_move_steps", self->retired_move_steps,
        "chained_messages", self->chained_messages,
        "direct_data_rows", self->direct_data_rows,
        "queued_messages", self->queued_messages,
        "write_calls", self->write_calls,
        "pause_writing_calls", self->pause_writing_calls,
        "resume_writing_calls", self->resume_writing_calls,
        "backpressure_waits", self->backpressure_waits
    );
}

static PyMethodDef buffered_methods[] = {
    {"get_buffer", (PyCFunction)buffered_get_buffer, METH_O, NULL},
    {"buffer_updated", (PyCFunction)buffered_buffer_updated, METH_O, NULL},
    {"connection_made", (PyCFunction)buffered_connection_made, METH_O, NULL},
    {"connection_lost", (PyCFunction)buffered_connection_lost, METH_O, NULL},
    {"register_operations", (PyCFunction)buffered_register_operations, METH_O, NULL},
    {"read_message", (PyCFunction)buffered_read_message, METH_NOARGS, NULL},
    {"write", (PyCFunction)buffered_write, METH_O, NULL},
    {"write_with_backpressure", (PyCFunction)buffered_write_with_backpressure, METH_O, NULL},
    {"pause_writing", (PyCFunction)buffered_pause_writing, METH_NOARGS, NULL},
    {"resume_writing", (PyCFunction)buffered_resume_writing, METH_NOARGS, NULL},
    {"drain", (PyCFunction)buffered_drain, METH_NOARGS, NULL},
    {"close", (PyCFunction)buffered_close, METH_NOARGS, NULL},
    {"wait_closed", (PyCFunction)buffered_wait_closed, METH_NOARGS, NULL},
    {"is_closing", (PyCFunction)buffered_is_closing, METH_NOARGS, NULL},
    {"_receive_stats", (PyCFunction)buffered_stats, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot buffered_slots[] = {
    {Py_tp_new, buffered_new},
    {Py_tp_dealloc, buffered_dealloc},
    {Py_tp_traverse, buffered_traverse},
    {Py_tp_clear, buffered_clear},
    {Py_tp_methods, buffered_methods},
    {0, NULL},
};

static PyType_Spec buffered_spec = {
    .name = "wreath._native._postgres.BufferedProtocol",
    .basicsize = sizeof(WreathPgBufferedProtocol),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = buffered_slots,
};

static PyMethodDef protocol_methods[] = {
    {"_build_cold_query_packet", build_cold, METH_VARARGS, NULL},
    {"_build_cached_query_packet", build_cached, METH_VARARGS, NULL},
    {"_join_pipeline_packets", join_pipeline_packets, METH_O, NULL},
    {NULL, NULL, 0, NULL}
};

int
wreath_pg_protocol_init(PyObject *module)
{
    PyObject *asyncio_module = PyImport_ImportModule("asyncio");
    PyObject *base;
    PyObject *bases;
    if (asyncio_module == NULL) return -1;
    base = PyObject_GetAttrString(asyncio_module, "BufferedProtocol");
    Py_DECREF(asyncio_module);
    if (base == NULL) return -1;
    bases = PyTuple_Pack(1, base);
    Py_DECREF(base);
    if (bases == NULL) return -1;
    str_discarded = PyUnicode_InternFromString("discarded");
    str_set_result = PyUnicode_InternFromString("set_result");
    str_done = PyUnicode_InternFromString("done");
    str_field_tape = PyUnicode_InternFromString("field_tape");
    str_decoder_plan = PyUnicode_InternFromString("decoder_plan");
    str_rows = PyUnicode_InternFromString("rows");
    str_dest = PyUnicode_InternFromString("dest");
    str_mode = PyUnicode_InternFromString("mode");
    str_command = PyUnicode_InternFromString("command");
    ready_message_type = (PyTypeObject *)PyType_FromSpec(&ready_message_spec);
    if (str_discarded == NULL || str_set_result == NULL || str_done == NULL ||
        str_field_tape == NULL || str_decoder_plan == NULL || str_rows == NULL ||
        str_dest == NULL ||
        str_mode == NULL || str_command == NULL || ready_message_type == NULL) {
        Py_DECREF(bases);
        return -1;
    }
    buffered_protocol_type = (PyTypeObject *)PyType_FromSpecWithBases(
        &buffered_spec, bases
    );
    Py_DECREF(bases);
    if (buffered_protocol_type == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "BufferedProtocol", (PyObject *)buffered_protocol_type) < 0 ||
        PyModule_AddFunctions(module, protocol_methods) < 0) return -1;
    Py_DECREF(buffered_protocol_type);
    return 0;
}

void
wreath_pg_protocol_fini(void)
{
    Py_CLEAR(str_discarded);
    Py_CLEAR(str_set_result);
    Py_CLEAR(str_done);
    Py_CLEAR(str_field_tape);
    Py_CLEAR(str_decoder_plan);
    Py_CLEAR(str_rows);
    Py_CLEAR(str_dest);
    Py_CLEAR(str_mode);
    Py_CLEAR(str_command);
    Py_CLEAR(ready_message_type);
    for (int i = 0; i < 256; i++) Py_CLEAR(kind_cache[i]);
    buffered_protocol_type = NULL;
}
