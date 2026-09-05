#include "protocol.h"

#include "hydrate.h"

#include "buffer.h"
#include "codec.h"
#include "decode.h"
#include "migration_image.h"
#include "operation.h"
#include "pipeline.h"
#include "plan.h"
#include "slab.h"
#include "tape.h"

#include "../wreath_stream.h"

#include <string.h>

/* `wreath._pgdriver.InterfaceError`, so a refusal here is the same
 * exception a caller catches on a pure build. The pure engine is the reference
 * and a native build that raised something else would make `except
 * InterfaceError` around a query work on one build and not the other. */
static PyObject *exc_interface = NULL;
static PyObject *exc_protocol = NULL;
static PyObject *str_discarded = NULL;
static PyObject *str_set_result = NULL;
static PyObject *str_done = NULL;
static PyObject *str_field_tape = NULL;
static PyObject *str_decoder_plan = NULL;
static PyObject *str_rows = NULL;
static PyObject *str_dest = NULL;
static PyObject *str_mode = NULL;
static PyObject *str_command = NULL;
static PyObject *str_cold = NULL;
static PyObject *str_compile_decoder_plan = NULL;
static PyObject *str_consume_message = NULL;
static PyObject *str_field_tape_type = NULL;
static PyObject *str_parameter_oids = NULL;
static PyObject *str_result_formats = NULL;
static PyObject *str_result_names = NULL;
static PyObject *str_result_oids = NULL;
static PyObject *native_compile_decoder_plan = NULL;
static PyObject *native_field_tape_type = NULL;
static PyObject *pure_consume_message = NULL;

/* Awaitable whose await completes immediately with a stored value, so a
   read_message() call that finds a pending message never suspends and
   allocates no future or coroutine machinery. */
typedef struct {
    PyObject *operation;
    PyObject *tape;
    PyObject *plan;
    PyObject *rows;
    PyObject *dest;
    PyObject *seen;
    long mode;
    int discarded;
    int discarded_sampled;
    int destination_validated;
} WreathPgOperationContext;

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
        PyErr_SetString(exc_interface != NULL ? exc_interface : PyExc_ValueError,
                        "query argument count does not match plan");
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
    else if (strcmp(value, "fetch") == 0 || strcmp(value, "fetch_batch") == 0 ||
             strcmp(value, "fetchrow") == 0 ||
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
        WreathPgPlan *cached_plan = (WreathPgPlan *)plan;
        /* With no arguments the Bind is a function of this plan's statement
         * name and the result format, and the plan is frozen -- so the bytes
         * cannot change and are kept rather than rebuilt. Measured at 2,153
         * instructions a query on the Fortunes shape, which is a parameterless
         * statement run once per request.
         *
         * `execute` asks for text results and every other mode for binary, so
         * there are exactly two, indexed by that. Nothing invalidates them:
         * they die with the plan. */
        int empty = PyTuple_Check(values) && PyTuple_GET_SIZE(values) == 0;
        int slot = execute ? 0 : 1;
        if (empty && cached_plan->packets[slot] != NULL) {
            return Py_NewRef(cached_plan->packets[slot]);
        }
        statement = Py_NewRef(cached_plan->statement_name);
        oids = Py_NewRef(cached_plan->parameter_oids);
        if (statement == NULL || oids == NULL) goto done;
        if (append_bind(&output, statement, values, oids, 1, !execute) < 0 ||
            wreath_pg_buffer_append(&output, EXECUTE_MESSAGE, sizeof(EXECUTE_MESSAGE)) < 0 ||
            wreath_pg_buffer_append(&output, SYNC_MESSAGE, sizeof(SYNC_MESSAGE)) < 0)
            goto done;
        result = wreath_pg_buffer_finish(&output);
        if (result != NULL && empty) {
            cached_plan->packets[slot] = Py_NewRef(result);
        }
        goto done;
    }
    {
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
    PyObject *connection;  /* exact Connection, attached once after startup */
    PyObject *kind_cache[256];  /* connection-owned one-byte control objects */
    /* Fused-egress fast path: when the transport is the native metal
     * transport, writes go through its C API instead of the bound method. */
    const WreathTransportCAPI *transport_capi;
    /* One outstanding fused read offer at a time; guards the offered slab
     * tail against acquisition/retirement races (see stream_* below). */
    int read_offer_live;
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
    /* In-flight operations are protocol-owned C records.  The former shape
       materialised each operation a second time as a six-element Python tuple
       and kept two index-parallel Python lists merely so the native parser
       could unpack them again.  This ring owns the necessary references and
       enum directly; Python is materialised only at the query and result
       boundaries. */
    WreathPgOperationContext *operations;
    Py_ssize_t operations_capacity;
    Py_ssize_t operations_head;
    Py_ssize_t operations_count;
    int head_direct;              /* no Python-consumed control for this head */
    WreathPgSlab *current;
    Py_ssize_t unread_bytes;
    Py_ssize_t slab_allocations;
    Py_ssize_t chained_messages;
    Py_ssize_t direct_data_rows;
    Py_ssize_t direct_record_rows;
    Py_ssize_t direct_model_rows;
    Py_ssize_t direct_completions;
    Py_ssize_t queued_messages;
    Py_ssize_t write_calls;
    Py_ssize_t pause_writing_calls;
    Py_ssize_t resume_writing_calls;
    Py_ssize_t backpressure_waits;
    int write_paused;
    int connection_closed;
} WreathPgBufferedProtocol;

static PyObject *
kind_object(WreathPgBufferedProtocol *self, unsigned char kind)
{
    PyObject *cached = self->kind_cache[kind];
    if (cached == NULL) {
        cached = PyBytes_FromStringAndSize((const char *)&kind, 1);
        self->kind_cache[kind] = cached;
    }
    return cached;
}

static PyTypeObject *buffered_protocol_type = NULL;

static void
operation_context_clear(WreathPgOperationContext *context)
{
    Py_CLEAR(context->operation);
    Py_CLEAR(context->tape);
    Py_CLEAR(context->plan);
    Py_CLEAR(context->rows);
    Py_CLEAR(context->dest);
    Py_CLEAR(context->seen);
    context->mode = 0;
    context->discarded = 0;
    context->discarded_sampled = 0;
    context->destination_validated = 0;
}

static WreathPgOperationContext *
operation_context_at(WreathPgBufferedProtocol *self, Py_ssize_t logical)
{
    Py_ssize_t index = (self->operations_head + logical) %
        self->operations_capacity;
    return &self->operations[index];
}

static WreathPgOperationContext *
operation_context_head(WreathPgBufferedProtocol *self)
{
    return self->operations_count == 0
        ? NULL : operation_context_at(self, 0);
}

static int
operation_context_reserve(WreathPgBufferedProtocol *self, Py_ssize_t extra)
{
    if (extra <= self->operations_capacity - self->operations_count) return 0;
    Py_ssize_t needed = self->operations_count + extra;
    Py_ssize_t capacity = self->operations_capacity == 0
        ? 8 : self->operations_capacity;
    while (capacity < needed) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        capacity *= 2;
    }
    WreathPgOperationContext *contexts = PyMem_Calloc(
        (size_t)capacity, sizeof(*contexts));
    if (contexts == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < self->operations_count; i++)
        contexts[i] = *operation_context_at(self, i);
    PyMem_Free(self->operations);
    self->operations = contexts;
    self->operations_capacity = capacity;
    self->operations_head = 0;
    return 0;
}

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

static int
operations_advance(WreathPgBufferedProtocol *self)
{
    if (self->operations_count == 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "PostgreSQL operation ring is empty");
        return -1;
    }
    operation_context_clear(&self->operations[self->operations_head]);
    self->operations_count--;
    self->operations_head = self->operations_count == 0
        ? 0 : (self->operations_head + 1) % self->operations_capacity;
    self->head_direct = 1;
    return 0;
}

static int
buffered_traverse(WreathPgBufferedProtocol *self, visitproc visit, void *arg)
{
    Py_VISIT(self->transport);
    Py_VISIT(self->transport_write);
    Py_VISIT(self->connection);
    for (Py_ssize_t i = 0; i < 256; i++) Py_VISIT(self->kind_cache[i]);
    Py_VISIT(self->messages);
    Py_VISIT(self->read_waiter);
    Py_VISIT(self->create_future);
    Py_VISIT(self->closed_future);
    Py_VISIT(self->done_future);
    Py_VISIT(self->slabs);
    Py_VISIT(self->spares);
    Py_VISIT(self->retired);
    for (Py_ssize_t i = 0; i < self->operations_count; i++) {
        WreathPgOperationContext *context = operation_context_at(self, i);
        Py_VISIT(context->operation);
        Py_VISIT(context->tape);
        Py_VISIT(context->plan);
        Py_VISIT(context->rows);
        Py_VISIT(context->dest);
        Py_VISIT(context->seen);
    }
    return 0;
}

static void
invalidate_context_cache(WreathPgBufferedProtocol *self)
{
    (void)self;
}

static int
buffered_clear(WreathPgBufferedProtocol *self)
{
    Py_CLEAR(self->transport);
    Py_CLEAR(self->transport_write);
    Py_CLEAR(self->connection);
    for (Py_ssize_t i = 0; i < 256; i++) Py_CLEAR(self->kind_cache[i]);
    self->transport_capi = NULL;
    self->read_offer_live = 0;
    Py_CLEAR(self->messages);
    self->messages_head = 0;
    Py_CLEAR(self->read_waiter);
    Py_CLEAR(self->create_future);
    Py_CLEAR(self->closed_future);
    Py_CLEAR(self->done_future);
    Py_CLEAR(self->slabs);
    self->unread_bytes = 0;
    Py_CLEAR(self->spares);
    Py_CLEAR(self->retired);
    self->retired_scan = 0;
    for (Py_ssize_t i = 0; i < self->operations_count; i++)
        operation_context_clear(operation_context_at(self, i));
    PyMem_Free(self->operations);
    self->operations = NULL;
    self->operations_capacity = 0;
    self->operations_count = 0;
    self->operations_head = 0;
    self->head_direct = 1;
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
trim_idle_spares(WreathPgBufferedProtocol *self)
{
    Py_ssize_t size = PyList_GET_SIZE(self->spares);
    if (size <= 2) return 0;
    return PyList_SetSlice(self->spares, 2, size, NULL);
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
    self->operations = NULL;
    self->operations_capacity = 0;
    self->operations_head = 0;
    self->operations_count = 0;
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
    return self->unread_bytes;
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
            self->unread_bytes -= length;
            return 0;
        }
        length -= available;
        if (self->current == slab) self->current = NULL;
        if (Py_REFCNT(slab) == 1 && PyList_GET_SIZE(self->spares) < 4) {
            slab->read_position = 0;
            slab->write_position = 0;
            /* Reset changes unread storage even if the spare append fails. */
            self->unread_bytes -= available;
            available = 0;
            if (PyList_Append(self->spares, (PyObject *)slab) < 0) return -1;
        } else if (Py_REFCNT(slab) > 1) {
            if (PyList_Append(self->retired, (PyObject *)slab) < 0) return -1;
        }
        /* native-lint: allow NC001 -- self->slabs holds only the slabs backing
           currently-unparsed bytes, so this list is a handful of entries; it is
           not a queue that grows with traffic. */
        if (PySequence_DelItem(self->slabs, 0) < 0) return -1;
        self->unread_bytes -= available;
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

static uint16_t
protocol_read_u16(const unsigned char *data)
{
    return (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static uint32_t
protocol_read_u32(const unsigned char *data)
{
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
           ((uint32_t)data[2] << 8) | data[3];
}

static int
cold_native_operation(WreathPgBufferedProtocol *self,
                      WreathPgOperationContext **context_out)
{
    WreathPgOperationContext *context = operation_context_head(self);
    PyObject *hook;
    PyObject *cold;
    int is_cold;
    (void)self;
    if (context == NULL || WreathPgOperationType == NULL ||
        !Py_IS_TYPE(context->operation, (PyTypeObject *)WreathPgOperationType)) return 0;
    cold = PyObject_GetAttr(context->operation, str_cold);
    if (cold == NULL) return -1;
    is_cold = cold == Py_True;
    Py_DECREF(cold);
    if (!is_cold) return 0;
    if (self->connection != NULL) {
        hook = PyObject_GetAttr(self->connection, str_compile_decoder_plan);
        if (hook == NULL) return -1;
        is_cold = hook == native_compile_decoder_plan;
        Py_DECREF(hook);
        if (!is_cold) return 0;
        hook = PyObject_GetAttr(self->connection, str_field_tape_type);
        if (hook == NULL) return -1;
        is_cold = hook == native_field_tape_type;
        Py_DECREF(hook);
        if (!is_cold) return 0;
        hook = PyObject_GetAttr(self->connection, str_consume_message);
        if (hook == NULL) return -1;
        is_cold = PyMethod_Check(hook) &&
            PyMethod_GET_FUNCTION(hook) == pure_consume_message;
        Py_DECREF(hook);
        if (!is_cold) return 0;
    }
    *context_out = context;
    return 1;
}

static int
install_parameter_description(WreathPgOperationContext *context,
                              const unsigned char *data, Py_ssize_t length)
{
    PyObject *oids;
    uint16_t count;
    if (length < 2) {
        PyErr_SetString(exc_protocol, "truncated ParameterDescription");
        return -1;
    }
    count = protocol_read_u16(data);
    if (length != 2 + (Py_ssize_t)count * 4) {
        PyErr_SetString(exc_protocol, "invalid ParameterDescription length");
        return -1;
    }
    oids = PyTuple_New(count);
    if (oids == NULL) return -1;
    for (uint16_t i = 0; i < count; i++) {
        PyObject *oid = PyLong_FromUnsignedLong(protocol_read_u32(data + 2 + i * 4));
        if (oid == NULL) {
            Py_DECREF(oids);
            return -1;
        }
        PyTuple_SET_ITEM(oids, i, oid);
    }
    if (PyObject_SetAttr(context->operation, str_parameter_oids, oids) < 0) {
        Py_DECREF(oids);
        return -1;
    }
    Py_DECREF(oids);
    return 0;
}

static int
install_row_description(WreathPgOperationContext *context,
                        const unsigned char *data, Py_ssize_t length)
{
    PyObject *names = NULL;
    PyObject *oids = NULL;
    PyObject *formats = NULL;
    PyObject *plan = NULL;
    PyObject *tape = NULL;
    Py_ssize_t offset = 2;
    uint16_t count;
    int result = -1;

    if (length < 2) {
        PyErr_SetString(exc_protocol, "truncated RowDescription");
        return -1;
    }
    count = protocol_read_u16(data);
    names = PyTuple_New(count);
    oids = PyTuple_New(count);
    formats = PyTuple_New(count);
    if (names == NULL || oids == NULL || formats == NULL) goto done;
    for (uint16_t i = 0; i < count; i++) {
        const unsigned char *end;
        Py_ssize_t remaining;
        Py_ssize_t name_length;
        PyObject *name;
        PyObject *oid;
        PyObject *format;
        if (offset > length) {
            PyErr_SetString(exc_protocol, "truncated RowDescription field");
            goto done;
        }
        remaining = length - offset;
        end = memchr(data + offset, 0, (size_t)remaining);
        if (end == NULL || data + length - end < 19) {
            PyErr_SetString(exc_protocol, "truncated RowDescription field");
            goto done;
        }
        name_length = end - (data + offset);
        name = PyUnicode_DecodeUTF8((const char *)data + offset, name_length, "strict");
        if (name == NULL) goto done;
        offset += name_length + 1;
        oid = PyLong_FromUnsignedLong(protocol_read_u32(data + offset + 6));
        format = PyLong_FromLong((int16_t)protocol_read_u16(data + offset + 16));
        if (oid == NULL || format == NULL) {
            Py_DECREF(name);
            Py_XDECREF(oid);
            Py_XDECREF(format);
            goto done;
        }
        PyTuple_SET_ITEM(names, i, name);
        PyTuple_SET_ITEM(oids, i, oid);
        PyTuple_SET_ITEM(formats, i, format);
        offset += 18;
    }
    if (offset != length) {
        PyErr_SetString(exc_protocol, "invalid RowDescription length");
        goto done;
    }
    if (count > 0) {
        PyObject *width;
        plan = wreath_pg_decoder_plan_new(oids, formats, names);
        if (plan == NULL) goto done;
        width = PyLong_FromUnsignedLong(count);
        if (width != NULL)
            tape = PyObject_CallOneArg((PyObject *)WreathPgFieldTapeType, width);
        Py_XDECREF(width);
        if (tape == NULL) goto done;
    }
    if (PyObject_SetAttr(context->operation, str_result_names, names) < 0 ||
        PyObject_SetAttr(context->operation, str_result_oids, oids) < 0 ||
        PyObject_SetAttr(context->operation, str_result_formats, formats) < 0)
        goto done;
    if (count > 0 &&
        (PyObject_SetAttr(context->operation, str_decoder_plan, plan) < 0 ||
         PyObject_SetAttr(context->operation, str_field_tape, tape) < 0))
        goto done;
    if (count > 0) {
        Py_SETREF(context->plan, Py_NewRef(plan));
        Py_SETREF(context->tape, Py_NewRef(tape));
    }
    result = 0;

done:
    Py_XDECREF(tape);
    Py_XDECREF(plan);
    Py_XDECREF(formats);
    Py_XDECREF(oids);
    Py_XDECREF(names);
    return result;
}

static int
direct_data_row(WreathPgBufferedProtocol *self, Py_ssize_t payload_length,
                WreathPgSlab *slab, Py_ssize_t absolute_offset)
{
    WreathPgOperationContext *context;
    WreathPgFieldTape *tape;
    unsigned int selected;

    context = operation_context_head(self);
    if (context == NULL) return 0;
    if (!context->discarded_sampled) {
        PyObject *discarded = PyObject_GetAttr(
            context->operation, str_discarded);
        if (discarded == NULL) return -1;
        context->discarded = discarded == Py_True;
        context->discarded_sampled = 1;
        Py_DECREF(discarded);
    }
    /* The discarded flag is sampled once per operation: rows accepted after
       a late cancellation are still dropped by the driver, which re-checks
       the flag before decoding batches and publishing results. */
    if (context->discarded) return 1;
    if (context->tape == Py_None || context->plan == Py_None ||
        !PyObject_TypeCheck(context->tape, WreathPgFieldTapeType) ||
        !PyObject_TypeCheck(context->plan, WreathPgDecoderPlanType)) return 0;
    if (context->mode == 0) return 1;
    if (context->mode == 4) {
        int decoded;
        if (slab == NULL)
            slab = locate_contiguous(self, 5, payload_length, &absolute_offset);
        if (slab != NULL) {
            decoded = wreath_pg_decode_datarow_batch(
                context->plan, context->rows,
                slab->data + absolute_offset, payload_length);
        } else {
            PyObject *payload = payload_object(self, 5, payload_length, 0);
            if (payload == NULL) return -1;
            decoded = wreath_pg_decode_datarow_batch(
                context->plan, context->rows,
                (const unsigned char *)PyBytes_AS_STRING(payload),
                PyBytes_GET_SIZE(payload));
            Py_DECREF(payload);
        }
        return decoded < 0 ? -1 : 1;
    }
    if (context->mode == 1 && context->dest == Py_None) {
        int decoded;
        if (slab == NULL)
            slab = locate_contiguous(self, 5, payload_length, &absolute_offset);
        if (slab != NULL) {
            decoded = wreath_pg_decode_datarow_record(
                context->plan, context->rows,
                slab->data + absolute_offset, payload_length);
        } else {
            PyObject *payload = payload_object(self, 5, payload_length, 0);
            if (payload == NULL) return -1;
            decoded = wreath_pg_decode_datarow_record(
                context->plan, context->rows,
                (const unsigned char *)PyBytes_AS_STRING(payload),
                PyBytes_GET_SIZE(payload));
            Py_DECREF(payload);
        }
        if (decoded < 0) return -1;
        self->direct_record_rows++;
        return 1;
    }
    tape = (WreathPgFieldTape *)context->tape;
    if ((context->mode == 2 || context->mode == 3) && tape->row_count > 0)
        return 1;
    selected = context->mode == 3 ? 1 : tape->source_columns;
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
    if (context->mode == 1 && tape->row_count >= 256) {
        if (context->dest != Py_None) {
            if (wreath_pg_migration_catalog_check(context->dest)) {
                if (wreath_pg_migration_catalog_decode(
                        context->plan, context->tape,
                        context->dest, 256) < 0) return -1;
            } else if (!PyTuple_Check(context->dest) ||
                       PyTuple_GET_SIZE(context->dest) != 3) {
                PyErr_SetString(
                    PyExc_TypeError,
                    "a decode destination is a migration catalog or "
                    "(plan, identity_map, owner)");
                return -1;
            }
        } else if (wreath_pg_decode_fetch_extend(
                       context->plan, context->tape, 256,
                       context->rows) < 0) {
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
        if ((kind_byte == 't' || kind_byte == 'T') &&
            self->operations_count > 0) {
            WreathPgOperationContext *context = NULL;
            int cold = cold_native_operation(self, &context);
            if (cold < 0) return -1;
            if (cold) {
                const unsigned char *description_data;
                Py_ssize_t description_length = (Py_ssize_t)length - 4;
                PyObject *description = NULL;
                int installed;
                if (contiguous) {
                    description_data = first->data + first->read_position + 5;
                } else {
                    description = payload_object(
                        self, 5, description_length, 0);
                    if (description == NULL) return -1;
                    description_data =
                        (const unsigned char *)PyBytes_AS_STRING(description);
                }
                if (kind_byte == 't') {
                    installed = install_parameter_description(
                        context, description_data, description_length);
                } else {
                    installed = install_row_description(
                        context, description_data, description_length);
                }
                Py_XDECREF(description);
                if (installed < 0) return -1;
                if (consume_bytes(self, wire_length) < 0) return -1;
                continue;
            }
        }
        /* CommandComplete, for every result mode rather than only `execute`.
         *
         * `_consume_message`'s `C` branch does exactly what the block below
         * does -- assign `operation.command` -- so queueing the message to
         * Python bought nothing and cost a crossing: one reader resumption and
         * one `_consume_message` call. It was measurable, because a
         * row-returning query surfaced two messages to Python where an
         * `execute` surfaced one (`queued_messages` 2.000 against 1.002), and a
         * row-returning query is every query the Fortunes board issues.
         *
         * The mode is still read, because it is what the surrounding code uses
         * to tell a result set from a bare command, and it stays in the context
         * for `direct_data_row` above. It just no longer gates this. */
        if (kind_byte == 'C' && self->operations_count > 0) {
            WreathPgOperationContext *context = operation_context_head(self);
            {
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
                        context->operation, str_command, command) < 0) {
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
        if (kind_byte == 'Z' && length == 5 && self->connection != NULL &&
            self->head_direct && self->operations_count > 0) {
            WreathPgOperationContext *context = operation_context_head(self);
            char status;
            PyObject *status_payload = NULL;
            int completed;
            if (contiguous) {
                status = (char)first->data[first->read_position + 5];
            }
            else {
                status_payload = payload_object(self, 5, 1, 0);
                if (status_payload == NULL) return -1;
                status = PyBytes_AS_STRING(status_payload)[0];
            }
            completed = wreath_pg_pipeline_complete_cached(
                self->connection, context->operation,
                context->tape, context->plan, status);
            Py_XDECREF(status_payload);
            if (completed < 0) return -1;
            if (completed) {
                self->direct_completions++;
                if (consume_bytes(self, wire_length) < 0) return -1;
                invalidate_context_cache(self);
                if (operations_advance(self) < 0) return -1;
                if (self->operations_count == 0 && trim_idle_spares(self) < 0)
                    return -1;
                continue;
            }
        }
        kind = kind_object(self, kind_byte);
        payload = payload_object(self, 5, length - 4, kind_byte == 'D');
        if (kind == NULL || payload == NULL) {
            Py_XDECREF(payload);
            return -1;
        }
        item = PyTuple_Pack(2, kind, payload);
        Py_DECREF(payload);
        if (item == NULL) return -1;
        if (kind_byte != 'A' && kind_byte != 'N' && kind_byte != 'S')
            self->head_direct = 0;
        if (deliver_message(self, item) < 0) {
            Py_DECREF(item);
            return -1;
        }
        self->queued_messages++;
        Py_DECREF(item);
        if (consume_bytes(self, wire_length) < 0) return -1;
        if (kind_byte == 'Z' && self->operations_count > 0) {
            invalidate_context_cache(self);
            if (operations_advance(self) < 0) return -1;
            if (self->operations_count == 0 && trim_idle_spares(self) < 0) {
                return -1;
            }
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
    self->unread_bytes += count;
    if (parse_messages(self) < 0) return NULL;
    Py_RETURN_NONE;
}

/* --- fused stream ingress (WreathStreamCAPI) ----------------------------- */
/* The metal transport delivers wire bytes here with no Python calling
 * convention: no memoryview, no boxed byte counts, no method dispatch per
 * socket read. The copy into the slab remains -- records and DataRow windows
 * retain slab memory beyond the callback, so ingress cannot borrow transient
 * transport buffers the way the HTTP/1 parser does.
 *
 * Offer contract: at most one outstanding offer; the offered region is the
 * current slab's writable tail, which is stable between acquire and commit
 * because slabs are fixed allocations and retirement only touches the
 * `retired` list (never `current`). `read_offer_live` asserts the contract. */

static int
stream_check(PyObject *protocol)
{
    return buffered_protocol_type != NULL &&
           PyObject_TypeCheck(protocol, buffered_protocol_type);
}

static int
stream_acquire_read_buffer(PyObject *protocol, char **buffer,
                           Py_ssize_t *capacity)
{
    if (!stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native pg BufferedProtocol");
        return -1;
    }
    WreathPgBufferedProtocol *self = (WreathPgBufferedProtocol *)protocol;
    if (self->read_offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "acquire_read_buffer() while a read offer is live");
        return -1;
    }
    if (reclaim_retired(self, 8) < 0) return -1;
    if (self->current == NULL ||
        self->current->write_position == WREATH_PG_SLAB_SIZE) {
        if (acquire_slab(self) == NULL) return -1;
    }
    self->read_offer_live = 1;
    *buffer = (char *)self->current->data + self->current->write_position;
    *capacity = WREATH_PG_SLAB_SIZE - self->current->write_position;
    return 0;
}

static int
stream_commit_read(PyObject *protocol, Py_ssize_t nbytes)
{
    if (!stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native pg BufferedProtocol");
        return -1;
    }
    WreathPgBufferedProtocol *self = (WreathPgBufferedProtocol *)protocol;
    if (!self->read_offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "commit_read() without a live read offer");
        return -1;
    }
    self->read_offer_live = 0;
    if (nbytes < 0 || self->current == NULL ||
        nbytes > WREATH_PG_SLAB_SIZE - self->current->write_position) {
        PyErr_SetString(PyExc_ValueError, "invalid fused receive count");
        return -1;
    }
    self->current->write_position += nbytes;
    self->unread_bytes += nbytes;
    if (nbytes == 0) return 0;  /* abandoned offer: nothing to parse */
    return parse_messages(self);
}

static int
stream_feed_external(PyObject *protocol, const char *data, Py_ssize_t size)
{
    if (!stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native pg BufferedProtocol");
        return -1;
    }
    if (size < 0) {
        PyErr_SetString(PyExc_ValueError, "negative external read size");
        return -1;
    }
    WreathPgBufferedProtocol *self = (WreathPgBufferedProtocol *)protocol;
    if (self->read_offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "external read while a read offer is live");
        return -1;
    }
    /* Copy slab-by-slab, parsing after each fill exactly as the buffered
     * path parses after each buffer_updated(); messages spanning slab
     * boundaries take the existing chained-payload path. */
    while (size > 0) {
        if (self->current == NULL ||
            self->current->write_position == WREATH_PG_SLAB_SIZE) {
            if (reclaim_retired(self, 8) < 0) return -1;
            if (acquire_slab(self) == NULL) return -1;
        }
        Py_ssize_t space = WREATH_PG_SLAB_SIZE - self->current->write_position;
        Py_ssize_t chunk = size < space ? size : space;
        memcpy(self->current->data + self->current->write_position,
               data, (size_t)chunk);
        self->current->write_position += chunk;
        self->unread_bytes += chunk;
        data += chunk;
        size -= chunk;
        if (parse_messages(self) < 0) return -1;
    }
    return 0;
}

static const WreathStreamCAPI stream_capi = {
    WREATH_STREAM_CAPI_VERSION,
    stream_check,
    stream_acquire_read_buffer,
    stream_commit_read,
    stream_feed_external,
};

static PyObject *
buffered_connection_made(WreathPgBufferedProtocol *self, PyObject *transport)
{
    PyObject *write = PyObject_GetAttrString(transport, "write");
    if (write == NULL) return NULL;
    Py_XSETREF(self->transport, Py_NewRef(transport));
    Py_XSETREF(self->transport_write, write);
    self->connection_closed = 0;
    const WreathTransportCAPI *capi = wreath_transport_capi_resolve();
    self->transport_capi =
        capi != NULL && capi->check(transport) ? capi : NULL;
    Py_RETURN_NONE;
}

static PyObject *
buffered_connection_lost(WreathPgBufferedProtocol *self, PyObject *error)
{
    PyObject *done;
    PyObject *result;
    (void)error;
    self->connection_closed = 1;
    self->write_paused = 0;
    if (self->read_waiter != NULL) {
        done = PyObject_CallMethodNoArgs(self->read_waiter, str_done);
        if (done == NULL) return NULL;
        if (done == Py_False) {
            PyObject *exception;
            Py_DECREF(done);
            exception = PyObject_CallFunction(
                PyExc_ConnectionError, "s", "PostgreSQL transport closed"
            );
            if (exception == NULL) return NULL;
            result = PyObject_CallMethod(
                self->read_waiter, "set_exception", "O", exception
            );
            Py_DECREF(exception);
            if (result == NULL) return NULL;
            Py_DECREF(result);
        } else {
            Py_DECREF(done);
        }
    }
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

static int
operation_context_append_parts(WreathPgBufferedProtocol *self,
                               PyObject *operation, PyObject *tape,
                               PyObject *plan, PyObject *rows,
                               PyObject *dest, long mode_code)
{
    if (operation_context_reserve(self, 1) < 0) return -1;
    WreathPgOperationContext *context = operation_context_at(
        self, self->operations_count);
    context->operation = Py_NewRef(operation);
    context->tape = Py_NewRef(tape);
    context->plan = Py_NewRef(plan);
    context->rows = Py_NewRef(rows);
    context->dest = Py_NewRef(dest);
    context->seen = Py_NewRef(Py_None);
    context->mode = mode_code;
    context->discarded = 0;
    context->discarded_sampled = 0;
    context->destination_validated = 0;
    if (self->operations_count == 0) self->head_direct = 1;
    self->operations_count++;
    return 0;
}

static int
operation_context_append(WreathPgBufferedProtocol *self, PyObject *operation)
{
    PyObject *tape = PyObject_GetAttr(operation, str_field_tape);
    PyObject *plan = PyObject_GetAttr(operation, str_decoder_plan);
    PyObject *rows = PyObject_GetAttr(operation, str_rows);
    PyObject *mode_object = PyObject_GetAttr(operation, str_mode);
    PyObject *dest = PyObject_GetAttr(operation, str_dest);
    const char *mode;
    long mode_code;
    if (tape == NULL || plan == NULL || rows == NULL || mode_object == NULL ||
        dest == NULL) goto error;
    mode = PyUnicode_AsUTF8(mode_object);
    if (mode == NULL) goto error;
    if (strcmp(mode, "execute") == 0) mode_code = 0;
    else if (strcmp(mode, "fetch") == 0) mode_code = 1;
    else if (strcmp(mode, "fetch_batch") == 0) mode_code = 4;
    else if (strcmp(mode, "fetchrow") == 0) mode_code = 2;
    else if (strcmp(mode, "fetchval") == 0) mode_code = 3;
    else {
        PyErr_SetString(PyExc_ValueError, "unknown PostgreSQL result mode");
        goto error;
    }
    if (operation_context_append_parts(
            self, operation, tape, plan, rows, dest, mode_code) < 0) goto error;
    Py_DECREF(mode_object);
    Py_DECREF(dest);
    Py_DECREF(rows);
    Py_DECREF(plan);
    Py_DECREF(tape);
    return 0;

error:
    Py_XDECREF(mode_object);
    Py_XDECREF(dest);
    Py_XDECREF(rows);
    Py_XDECREF(plan);
    Py_XDECREF(tape);
    return -1;
}

static int
register_operation_sequence(WreathPgBufferedProtocol *self,
                            PyObject *operations)
{
    PyObject *fast = PySequence_Fast(
        operations, "pipeline operations must be a sequence");
    if (fast == NULL) return -1;
    Py_ssize_t original_count = self->operations_count;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    PyObject **items = PySequence_Fast_ITEMS(fast);
    for (Py_ssize_t i = 0; i < count; i++) {
        if (operation_context_append(self, items[i]) < 0) {
            while (self->operations_count > original_count) {
                Py_ssize_t tail = (self->operations_head +
                    self->operations_count - 1) % self->operations_capacity;
                operation_context_clear(&self->operations[tail]);
                self->operations_count--;
            }
            Py_DECREF(fast);
            return -1;
        }
    }
    Py_DECREF(fast);
    return 0;
}

int
wreath_pg_protocol_register_operations(PyObject *protocol,
                                       PyObject *operations)
{
    if (buffered_protocol_type == NULL ||
        !Py_IS_TYPE(protocol, buffered_protocol_type)) return 0;
    if (register_operation_sequence(
            (WreathPgBufferedProtocol *)protocol, operations) < 0) return -1;
    return 1;
}

int
wreath_pg_protocol_register_operation(PyObject *protocol,
                                      PyObject *operation)
{
    if (buffered_protocol_type == NULL ||
        !Py_IS_TYPE(protocol, buffered_protocol_type)) return 0;
    if (operation_context_append(
            (WreathPgBufferedProtocol *)protocol, operation) < 0) return -1;
    return 1;
}

int
wreath_pg_protocol_register_operation_parts(
    PyObject *protocol, PyObject *operation, PyObject *tape,
    PyObject *plan, PyObject *rows, PyObject *dest, long mode)
{
    if (buffered_protocol_type == NULL ||
        !Py_IS_TYPE(protocol, buffered_protocol_type)) return 0;
    if (mode < 0 || mode > 4) {
        PyErr_SetString(PyExc_ValueError, "unknown PostgreSQL result mode");
        return -1;
    }
    if (operation_context_append_parts(
            (WreathPgBufferedProtocol *)protocol, operation,
            tape, plan, rows, dest, mode) < 0) return -1;
    return 1;
}

static PyObject *
buffered_register_operations(WreathPgBufferedProtocol *self, PyObject *operations)
{
    if (!PyTuple_Check(operations)) {
        PyErr_SetString(PyExc_TypeError, "pipeline operations must be a tuple");
        return NULL;
    }
    if (register_operation_sequence(self, operations) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
buffered_attach_connection(WreathPgBufferedProtocol *self, PyObject *connection)
{
    if (self->connection != NULL && self->connection != connection) {
        PyErr_SetString(PyExc_RuntimeError,
                        "PostgreSQL protocol is already attached to a connection");
        return NULL;
    }
    Py_XSETREF(self->connection, Py_NewRef(connection));
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
    if (self->connection_closed) {
        PyErr_SetString(PyExc_ConnectionError, "PostgreSQL transport closed");
        return NULL;
    }
    self->read_waiter = PyObject_CallNoArgs(self->create_future);
    if (self->read_waiter == NULL) return NULL;
    return Py_NewRef(self->read_waiter);
}

static PyObject *
buffered_write(WreathPgBufferedProtocol *self, PyObject *data)
{
    PyObject *result;
    if (self->connection_closed) {
        PyErr_SetString(PyExc_ConnectionError, "PostgreSQL transport closed");
        return NULL;
    }
    if (self->transport_write == NULL) {
        PyErr_SetString(PyExc_ConnectionError, "transport is not connected");
        return NULL;
    }
    if (self->transport_capi != NULL && self->transport != NULL) {
        /* Fused egress: no bound-method dispatch; exact-bytes payloads enter
         * the metal transport's retained send queue zero-copy. */
        if (self->transport_capi->write(self->transport, data) < 0) {
            return NULL;
        }
        self->write_calls++;
        Py_RETURN_NONE;
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
        "{s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n,s:n}",
        "unread_bytes", self->unread_bytes,
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
        "direct_record_rows", self->direct_record_rows,
        "direct_model_rows", self->direct_model_rows,
        "direct_completions", self->direct_completions,
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
    {"attach_connection", (PyCFunction)buffered_attach_connection, METH_O, NULL},
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
    {
        PyObject *pure = PyImport_ImportModule("wreath._pgdriver");
        PyObject *connection;
        if (pure == NULL) return -1;
        exc_interface = PyObject_GetAttrString(pure, "InterfaceError");
        exc_protocol = PyObject_GetAttrString(pure, "ProtocolError");
        connection = PyObject_GetAttrString(pure, "Connection");
        if (connection != NULL)
            pure_consume_message = PyObject_GetAttrString(
                connection, "_consume_message");
        Py_XDECREF(connection);
        Py_DECREF(pure);
        if (exc_interface == NULL || exc_protocol == NULL ||
            pure_consume_message == NULL) return -1;
    }
    native_compile_decoder_plan = PyObject_GetAttrString(
        module, "_compile_decoder_plan");
    native_field_tape_type = PyObject_GetAttrString(module, "_FieldTape");
    if (native_compile_decoder_plan == NULL || native_field_tape_type == NULL)
        return -1;
    str_discarded = PyUnicode_InternFromString("discarded");
    str_set_result = PyUnicode_InternFromString("set_result");
    str_done = PyUnicode_InternFromString("done");
    str_field_tape = PyUnicode_InternFromString("field_tape");
    str_decoder_plan = PyUnicode_InternFromString("decoder_plan");
    str_rows = PyUnicode_InternFromString("rows");
    str_dest = PyUnicode_InternFromString("dest");
    str_mode = PyUnicode_InternFromString("mode");
    str_command = PyUnicode_InternFromString("command");
    str_cold = PyUnicode_InternFromString("cold");
    str_compile_decoder_plan = PyUnicode_InternFromString("_compile_decoder_plan");
    str_consume_message = PyUnicode_InternFromString("_consume_message");
    str_field_tape_type = PyUnicode_InternFromString("_field_tape_type");
    str_parameter_oids = PyUnicode_InternFromString("parameter_oids");
    str_result_formats = PyUnicode_InternFromString("result_formats");
    str_result_names = PyUnicode_InternFromString("result_names");
    str_result_oids = PyUnicode_InternFromString("result_oids");
    ready_message_type = (PyTypeObject *)PyType_FromSpec(&ready_message_spec);
    if (str_discarded == NULL || str_set_result == NULL || str_done == NULL ||
        str_field_tape == NULL || str_decoder_plan == NULL || str_rows == NULL ||
        str_dest == NULL ||
        str_mode == NULL || str_command == NULL || str_cold == NULL ||
        str_compile_decoder_plan == NULL || str_consume_message == NULL ||
        str_field_tape_type == NULL ||
        str_parameter_oids == NULL || str_result_formats == NULL ||
        str_result_names == NULL || str_result_oids == NULL ||
        ready_message_type == NULL) {
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
    PyObject *stream_capsule = PyCapsule_New(
        (void *)&stream_capi, "wreath._native._postgres._STREAM_C_API", NULL);
    if (stream_capsule == NULL ||
        PyModule_AddObject(module, "_STREAM_C_API", stream_capsule) < 0) {
        Py_XDECREF(stream_capsule);
        return -1;
    }
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
    Py_CLEAR(str_cold);
    Py_CLEAR(str_compile_decoder_plan);
    Py_CLEAR(str_consume_message);
    Py_CLEAR(str_field_tape_type);
    Py_CLEAR(str_parameter_oids);
    Py_CLEAR(str_result_formats);
    Py_CLEAR(str_result_names);
    Py_CLEAR(str_result_oids);
    Py_CLEAR(native_compile_decoder_plan);
    Py_CLEAR(native_field_tape_type);
    Py_CLEAR(pure_consume_message);
    Py_CLEAR(exc_protocol);
    Py_CLEAR(ready_message_type);
    buffered_protocol_type = NULL;
}
