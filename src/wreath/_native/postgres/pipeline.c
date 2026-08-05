/* The connection pipeline state machine, in C.
 *
 * `_pure/postgres.py` owns the reference implementation and the reasoning; this
 * file owns the same state machine for the native tier, and the two are held to
 * identical observable behaviour by `tests/postgres/` running every case against
 * both backends.
 *
 * Why this exists: the native tier accelerated the *codec* -- encode, decode,
 * row hydration -- and left submission, flushing and completion in Python. On a
 * one-row query that split put roughly 4.5% of the round trip's CPU in C and
 * the rest in the interpreter, because a small-result query is mostly
 * orchestration and hardly any bytes. Measured on this machine before this file
 * existed: `fetchrow` of one row cost 18.6us wall of which 15.2us was client
 * CPU, and a profile of the driver alone (no HTTP) attributed 82.8% of cycles
 * to libpython and 4.5% to `_postgres.so`.
 *
 * ## State lives in the Python object, and is read at struct offsets
 *
 * The native `Connection` subclasses the pure one, so every field is a
 * `__slots__` member on the base. This file does not copy that state into a C
 * struct -- it resolves each slot's offset once at import (`resolve_offsets`)
 * and reads it as `*(PyObject **)((char *)self + offset)`.
 *
 * That is deliberate and it is the reason the port can be partial without being
 * wrong. Anything still in Python -- `close`, `listen`, `notifications`,
 * `_Transaction` -- reads and writes the same words this file does, so there is
 * exactly one copy of the connection's state and no synchronisation between a C
 * view and a Python view. A C struct would have been faster to write and would
 * have required moving all 28 remaining methods in one step to stay correct.
 *
 * The offsets are resolved from `PyMemberDescrObject.d_member->offset` on the
 * *base* class, and `resolve_offsets` fails the import if a name is missing
 * rather than defaulting to 0 -- offset 0 is `ob_refcnt`, so a silent miss here
 * would corrupt the object rather than raise.
 */

#include "pipeline.h"

#include "operation.h"
#include "plan.h"

#include <string.h>

/* ------------------------------------------------------------------ *
 * Slot offsets
 * ------------------------------------------------------------------ */

typedef struct {
    Py_ssize_t closed;
    Py_ssize_t write_blocked;
    Py_ssize_t waiting;
    Py_ssize_t waiting_live;
    Py_ssize_t emitted;
    Py_ssize_t completed;
    Py_ssize_t current;
    Py_ssize_t plans;
    Py_ssize_t pending_closes;
    Py_ssize_t sequence;
    Py_ssize_t statement_id;
    Py_ssize_t transaction_status;
    Py_ssize_t transaction_barrier;
    Py_ssize_t flush_handle;
    Py_ssize_t loop;
    Py_ssize_t idle_event;
    Py_ssize_t reader_task;
    Py_ssize_t register_operations;
    Py_ssize_t writer;
    Py_ssize_t write_with_backpressure;
    Py_ssize_t write_count;
    Py_ssize_t background_tasks;
    Py_ssize_t listen_channels;
} ConnectionOffsets;

typedef struct {
    Py_ssize_t sequence;
    Py_ssize_t sql;
    Py_ssize_t args;
    Py_ssize_t mode;
    Py_ssize_t future;
    Py_ssize_t deadline;
    Py_ssize_t decoder_plan;
    Py_ssize_t dest;
    Py_ssize_t field_tape;
    Py_ssize_t state;
    Py_ssize_t plan;
    Py_ssize_t cold;
    Py_ssize_t statement_name;
    Py_ssize_t packet;
    Py_ssize_t parameter_oids;
    Py_ssize_t result_names;
    Py_ssize_t result_oids;
    Py_ssize_t result_formats;
    Py_ssize_t rows;
    Py_ssize_t one_row;
    Py_ssize_t one_value;
    Py_ssize_t have_value;
    Py_ssize_t command;
    Py_ssize_t error;
    Py_ssize_t discarded;
} OperationOffsets;

static ConnectionOffsets conn_off;
static OperationOffsets op_off;

/* Cached module-level objects. */
static PyObject *pure_module = NULL;
static PyObject *exc_interface = NULL;
static PyObject *exc_pipeline_full = NULL;
static PyObject *exc_protocol = NULL;
static PyObject *exc_operational = NULL;
static PyObject *exc_postgres = NULL;
static PyObject *fn_is_transaction_sql = NULL;
static PyObject *fn_infer_oid = NULL;
static PyObject *fn_plan_retained_bytes = NULL;
static PyObject *fn_message = NULL;
static PyObject *fn_cstring = NULL;
static PyObject *fn_parse_parameter_description = NULL;
static PyObject *fn_parse_row_description = NULL;
static PyObject *fn_parse_error = NULL;
static PyObject *fn_data_fields = NULL;
static PyObject *fn_build_cold_query_packet = NULL;
static PyObject *connection_type_ref = NULL;

/* Backend hooks, resolved once.
 *
 * `connection.c` sets these on the type at import and nothing changes them
 * afterwards -- no test patches one, and they are implementation bindings
 * rather than policy. Fetching them per query cost an MRO walk each through
 * `PyObject_GetAttr`, which is the one thing C does *worse* than the bytecode
 * it replaced: CPython 3.14 specialises `self._build_cached` behind an inline
 * cache after a couple of executions, while a generic `PyObject_GetAttr` pays
 * full price every time. That showed up as `PyObject_GetAttr` and
 * `PyObject_VectorcallMethod` in the profile of a port whose entire purpose
 * was to remove interpreter overhead.
 *
 * Guarded by an exact-type compare rather than used unconditionally: a
 * subclass overriding a hook must still get its own, and a pointer comparison
 * is what keeps that correct without giving the cost back. */
static PyObject *hook_operation_type = NULL;
static PyObject *hook_plan_type = NULL;
static PyObject *hook_build_cold = NULL;
static PyObject *hook_build_cached = NULL;
static PyObject *hook_join_packets = NULL;
static PyObject *hook_field_tape_type = NULL;
static int hook_batch_decode = 0;

/* `_eager_flush_idle` is deliberately NOT cached beside the hooks above.
 *
 * Those are implementation bindings -- which codec, which Operation type --
 * fixed at import and meaningless to change. This one is *policy*: "may an
 * idle connection write inside `_submit` instead of waiting for a `call_soon`
 * turn". Caching it froze it for the native type, and the first thing that
 * wanted to turn it off (a probe arm pricing submission without reaching the
 * wire) found it read-only with no way through. A flag a caller is entitled to
 * set is worth one attribute lookup per query. */

/* The hook for `self`, or NULL with the caller falling back to a lookup. */
static PyObject *
cached_hook(PyObject *self, PyObject *cached)
{
    return (PyObject *)Py_TYPE(self) == connection_type_ref ? cached : NULL;
}

/* `PyObject_GetAttr` unless the hook is cached for this exact type. Returns a
   borrowed reference when cached and a new one otherwise, so callers use
   `hook_release` rather than an unconditional Py_DECREF. */
static PyObject *
hook_get(PyObject *self, PyObject *cached, PyObject *name, int *borrowed)
{
    PyObject *hit = cached_hook(self, cached);
    if (hit != NULL) {
        *borrowed = 1;
        return hit;
    }
    *borrowed = 0;
    return PyObject_GetAttr(self, name);
}

static void
hook_release(PyObject *value, int borrowed)
{
    if (!borrowed) Py_XDECREF(value);
}

/* Interned strings for the few attribute names still fetched by name. */
static PyObject *str_state = NULL;
static PyObject *str_cancelled = NULL;
static PyObject *str_emitted = NULL;
static PyObject *str_waiting = NULL;
static PyObject *str_completed = NULL;
static PyObject *str_done = NULL;
static PyObject *str_cancelled_method = NULL;
static PyObject *str_set_result = NULL;
static PyObject *str_set_exception = NULL;
static PyObject *str_create_future = NULL;
static PyObject *str_call_soon = NULL;
static PyObject *str_create_task = NULL;
static PyObject *str_append = NULL;
static PyObject *str_popleft = NULL;
static PyObject *str_clear = NULL;
static PyObject *str_set = NULL;
static PyObject *str_get = NULL;
static PyObject *str_take_evicted = NULL;
static PyObject *str_write = NULL;
static PyObject *str_drain = NULL;
static PyObject *str_flush_method = NULL;
static PyObject *str_read_pipeline = NULL;
static PyObject *str_drain_method = NULL;
static PyObject *str_track_background = NULL;
static PyObject *str_fail_connection = NULL;
static PyObject *str_enqueue_notification = NULL;
static PyObject *str_row_count = NULL;
static PyObject *str_max_queued = NULL;
static PyObject *str_max_emitted = NULL;
static PyObject *str_max_outbound = NULL;
static PyObject *str_eager_flush_idle = NULL;
static PyObject *str_batch_decode = NULL;
static PyObject *str_build_cold = NULL;
static PyObject *str_build_cached = NULL;
static PyObject *str_join_packets = NULL;
static PyObject *str_field_tape_type = NULL;
static PyObject *str_compile_decoder_plan = NULL;
static PyObject *str_decode_tape = NULL;
static PyObject *str_decode_dest = NULL;
static PyObject *str_decode = NULL;
static PyObject *str_record_type = NULL;
static PyObject *str_plan_type = NULL;
static PyObject *str_operation_type = NULL;
static PyObject *str_statement_name = NULL;
static PyObject *str_join = NULL;
static PyObject *str_publish_completed = NULL;
static PyObject *str_binary_results = NULL;

/* Mode strings, interned so a mode test is a pointer compare. */
static PyObject *mode_execute = NULL;
static PyObject *mode_fetch = NULL;
static PyObject *mode_fetchrow = NULL;
static PyObject *mode_fetchval = NULL;

static PyObject *bytes_empty = NULL;
static PyObject *bytes_idle = NULL;
static PyObject *tuple_empty = NULL;
static PyObject *str_empty = NULL;

#define SLOT(obj, offset) (*(PyObject **)((char *)(obj) + (offset)))

/* Read a slot, or raise if the object never had it assigned. A __slots__
   member that was never set reads as NULL, which is an AttributeError in
   Python and a crash here if dereferenced. */
static PyObject *
slot_get(PyObject *obj, Py_ssize_t offset, const char *name)
{
    PyObject *value = SLOT(obj, offset);
    if (value == NULL) {
        PyErr_Format(PyExc_AttributeError, "%s", name);
        return NULL;
    }
    return value;
}

#define SLOT_BORROW(obj, off, name) slot_get((obj), (off), (name))

static void
slot_set(PyObject *obj, Py_ssize_t offset, PyObject *value)
{
    PyObject *old = SLOT(obj, offset);
    SLOT(obj, offset) = Py_XNewRef(value);
    Py_XDECREF(old);
}

/* Steal `value`'s reference into the slot. */
static void
slot_steal(PyObject *obj, Py_ssize_t offset, PyObject *value)
{
    PyObject *old = SLOT(obj, offset);
    SLOT(obj, offset) = value;
    Py_XDECREF(old);
}

static int
resolve_one(PyObject *type, const char *name, Py_ssize_t *out)
{
    PyObject *dict = ((PyTypeObject *)type)->tp_dict;
    PyObject *descr = dict == NULL ? NULL : PyDict_GetItemString(dict, name);
    if (descr == NULL || !PyObject_TypeCheck(descr, &PyMemberDescr_Type)) {
        PyErr_Format(
            PyExc_RuntimeError,
            "wreath._native._postgres: %s has no __slots__ member %s; the "
            "native pipeline and wreath._pure.postgres are out of step and "
            "the extension must be rebuilt",
            ((PyTypeObject *)type)->tp_name, name
        );
        return -1;
    }
    *out = ((PyMemberDescrObject *)descr)->d_member->offset;
    return 0;
}

#define RESOLVE(type, table, field, name) \
    if (resolve_one((type), (name), &(table).field) < 0) return -1

static int
resolve_offsets(PyObject *connection_base, PyObject *operation_base)
{
    RESOLVE(connection_base, conn_off, closed, "_closed");
    RESOLVE(connection_base, conn_off, write_blocked, "_write_blocked");
    RESOLVE(connection_base, conn_off, waiting, "_waiting");
    RESOLVE(connection_base, conn_off, waiting_live, "_waiting_live");
    RESOLVE(connection_base, conn_off, emitted, "_emitted");
    RESOLVE(connection_base, conn_off, completed, "_completed");
    RESOLVE(connection_base, conn_off, current, "_current");
    RESOLVE(connection_base, conn_off, plans, "_plans");
    RESOLVE(connection_base, conn_off, pending_closes, "_pending_closes");
    RESOLVE(connection_base, conn_off, sequence, "_sequence");
    RESOLVE(connection_base, conn_off, statement_id, "_statement_id");
    RESOLVE(connection_base, conn_off, transaction_status, "_transaction_status");
    RESOLVE(connection_base, conn_off, transaction_barrier, "_transaction_barrier");
    RESOLVE(connection_base, conn_off, flush_handle, "_flush_handle");
    RESOLVE(connection_base, conn_off, loop, "_loop");
    RESOLVE(connection_base, conn_off, idle_event, "_idle_event");
    RESOLVE(connection_base, conn_off, reader_task, "_reader_task");
    RESOLVE(connection_base, conn_off, register_operations, "_register_operations");
    RESOLVE(connection_base, conn_off, writer, "_writer");
    RESOLVE(connection_base, conn_off, write_with_backpressure,
            "_write_with_backpressure");
    RESOLVE(connection_base, conn_off, write_count, "_write_count");
    RESOLVE(connection_base, conn_off, background_tasks, "_background_tasks");
    RESOLVE(connection_base, conn_off, listen_channels, "_listen_channels");

    RESOLVE(operation_base, op_off, sequence, "sequence");
    RESOLVE(operation_base, op_off, sql, "sql");
    RESOLVE(operation_base, op_off, args, "args");
    RESOLVE(operation_base, op_off, mode, "mode");
    RESOLVE(operation_base, op_off, future, "future");
    RESOLVE(operation_base, op_off, deadline, "deadline");
    RESOLVE(operation_base, op_off, decoder_plan, "decoder_plan");
    RESOLVE(operation_base, op_off, dest, "dest");
    RESOLVE(operation_base, op_off, field_tape, "field_tape");
    RESOLVE(operation_base, op_off, state, "state");
    RESOLVE(operation_base, op_off, plan, "plan");
    RESOLVE(operation_base, op_off, cold, "cold");
    RESOLVE(operation_base, op_off, statement_name, "statement_name");
    RESOLVE(operation_base, op_off, packet, "packet");
    RESOLVE(operation_base, op_off, parameter_oids, "parameter_oids");
    RESOLVE(operation_base, op_off, result_names, "result_names");
    RESOLVE(operation_base, op_off, result_oids, "result_oids");
    RESOLVE(operation_base, op_off, result_formats, "result_formats");
    RESOLVE(operation_base, op_off, rows, "rows");
    RESOLVE(operation_base, op_off, one_row, "one_row");
    RESOLVE(operation_base, op_off, one_value, "one_value");
    RESOLVE(operation_base, op_off, have_value, "have_value");
    RESOLVE(operation_base, op_off, command, "command");
    RESOLVE(operation_base, op_off, error, "error");
    RESOLVE(operation_base, op_off, discarded, "discarded");
    return 0;
}

/* ------------------------------------------------------------------ *
 * Small helpers over the slots
 * ------------------------------------------------------------------ */

static int
slot_is_true(PyObject *obj, Py_ssize_t offset)
{
    PyObject *value = SLOT(obj, offset);
    if (value == NULL) return 0;
    return PyObject_IsTrue(value);
}

static Py_ssize_t
slot_as_ssize(PyObject *obj, Py_ssize_t offset, const char *name)
{
    PyObject *value = SLOT_BORROW(obj, offset, name);
    if (value == NULL) return -1;
    return PyLong_AsSsize_t(value);
}

static int
slot_set_ssize(PyObject *obj, Py_ssize_t offset, Py_ssize_t value)
{
    PyObject *boxed = PyLong_FromSsize_t(value);
    if (boxed == NULL) return -1;
    slot_steal(obj, offset, boxed);
    return 0;
}

/* A class-level tunable (`max_queued_operations` and friends). */
static Py_ssize_t
class_bound(PyObject *self, PyObject *name)
{
    PyObject *value = PyObject_GetAttr(self, name);
    Py_ssize_t result;
    if (value == NULL) return -1;
    result = PyLong_AsSsize_t(value);
    Py_DECREF(value);
    return result;
}

/* Deliberately *not* memoised, though it was tried and measured.
 *
 * `_submit` and `_flush` read four of these per query. Caching them against
 * `tp_version_tag` -- CPython's own inline-cache trick, and correct under
 * `monkeypatch.setattr(type(conn), ...)` because the tag is bumped -- was worth
 * 313 instructions on `execute` and 441 on `fetchrow`, against a within-session
 * spread of about 260. Roughly 100 instructions per lookup, not the MRO walk
 * the change assumed: CPython's type attribute cache already makes a
 * class-level `PyObject_GetAttr` cheap.
 *
 * Forty lines of cache machinery for 0.6%, at the noise floor, is not a trade
 * this file should make. Left here so the next person does not re-derive it. */

static PyObject *
call_method0(PyObject *obj, PyObject *name)
{
    return PyObject_CallMethodNoArgs(obj, name);
}

static int
future_is_cancelled(PyObject *future)
{
    PyObject *result = call_method0(future, str_cancelled_method);
    int truth;
    if (result == NULL) return -1;
    truth = PyObject_IsTrue(result);
    Py_DECREF(result);
    return truth;
}

static int
future_is_done(PyObject *future)
{
    PyObject *result = call_method0(future, str_done);
    int truth;
    if (result == NULL) return -1;
    truth = PyObject_IsTrue(result);
    Py_DECREF(result);
    return truth;
}

/* ------------------------------------------------------------------ *
 * Transaction-control detection
 * ------------------------------------------------------------------ */

/* `_submit` asks this of every statement, and the Python original
 *
 *     first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
 *     return first in {"BEGIN", "START", ...}
 *
 * allocates a stripped string, a split list and an uppercased string to answer
 * "does this start with BEGIN". Measured at ~4,010 instructions per query --
 * a third of everything left in the submission path after the future, the
 * Operation and the packet were accounted for.
 *
 * This is the same question answered by a byte scan with no allocation, and it
 * declines anything non-ASCII rather than guessing. That fallback is not
 * defensive padding: `str.lstrip()` strips U+00A0 and U+3000, and `.upper()`
 * maps fullwidth letters, so a byte scan that tried to handle them would give a
 * different answer from the reference. `tests/postgres/test_transaction_sql_parity.py`
 * holds the two to the same corpus, non-ASCII included.
 */

static const char *const TRANSACTION_KEYWORDS[] = {
    "BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", NULL
};

static int
ascii_space(char c)
{
    /* Exactly the set `str.lstrip()` removes from an ASCII string. */
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
           c == '\f' || c == '\v';
}

static int
is_transaction_sql(PyObject *sql)
{
    const char *data;
    Py_ssize_t length;
    Py_ssize_t start = 0;
    Py_ssize_t end;

    if (!PyUnicode_Check(sql)) return 0;
    if (!PyUnicode_IS_ASCII(sql)) {
        /* Hand it to the reference; see the note above. */
        PyObject *verdict = PyObject_CallOneArg(fn_is_transaction_sql, sql);
        int truth;
        if (verdict == NULL) return -1;
        truth = PyObject_IsTrue(verdict);
        Py_DECREF(verdict);
        return truth;
    }

    data = (const char *)PyUnicode_1BYTE_DATA(sql);
    length = PyUnicode_GET_LENGTH(sql);
    while (start < length && ascii_space(data[start])) start++;
    if (start == length) return 0;           /* whitespace only -> "" */
    end = start;
    while (end < length && !ascii_space(data[end])) end++;

    for (const char *const *keyword = TRANSACTION_KEYWORDS; *keyword; keyword++) {
        Py_ssize_t width = (Py_ssize_t)strlen(*keyword);
        Py_ssize_t index;
        if (end - start != width) continue;
        for (index = 0; index < width; index++) {
            char c = data[start + index];
            if (c >= 'a' && c <= 'z') c = (char)(c - 32);
            if (c != (*keyword)[index]) break;
        }
        if (index == width) return 1;
    }
    return 0;
}

static PyObject *
pipeline_is_transaction_sql(PyObject *module, PyObject *sql)
{
    int verdict;
    (void)module;
    verdict = is_transaction_sql(sql);
    if (verdict < 0) return NULL;
    return PyBool_FromLong(verdict);
}

static PyMethodDef pipeline_module_methods[] = {
    {"_is_transaction_sql", pipeline_is_transaction_sql, METH_O,
     "Whether this SQL is transaction control, as `_submit` decides it."},
    {NULL, NULL, 0, NULL}
};

/* ------------------------------------------------------------------ *
 * Operation.__init__
 * ------------------------------------------------------------------ */

/* The pure `Operation.__init__` assigns 24 `__slots__`, and one runs per query.
 * The native type is a heap subclass with `basicsize = 0`, so it inherited that
 * Python constructor and paid a frame plus 24 STORE_ATTR for every operation
 * the driver created. The slots and their order are the reference's
 * (`_pure/postgres.py:1372`); this writes the same values at the offsets
 * `resolve_offsets` already found.
 *
 * `rows` is `[]` for a fetch and `None` otherwise, which is the one field whose
 * value depends on the mode -- everything else is a constant. */
static int
operation_init(PyObject *self, PyObject *args, PyObject *kwargs)
{
    PyObject *sequence, *sql, *argv, *mode, *future, *deadline;
    static char *kwlist[] = {
        "sequence", "sql", "args", "mode", "future", "deadline", NULL
    };
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "OOOOOO:Operation", kwlist,
            &sequence, &sql, &argv, &mode, &future, &deadline)) {
        return -1;
    }

    slot_set(self, op_off.sequence, sequence);
    slot_set(self, op_off.sql, sql);
    slot_set(self, op_off.args, argv);
    slot_set(self, op_off.mode, mode);
    slot_set(self, op_off.future, future);
    slot_set(self, op_off.deadline, deadline);
    slot_set(self, op_off.decoder_plan, Py_None);
    slot_set(self, op_off.dest, Py_None);
    slot_set(self, op_off.field_tape, Py_None);
    slot_set(self, op_off.state, str_waiting);
    slot_set(self, op_off.plan, Py_None);
    slot_set(self, op_off.cold, Py_True);
    slot_set(self, op_off.statement_name, bytes_empty);
    slot_set(self, op_off.packet, bytes_empty);
    slot_set(self, op_off.parameter_oids, tuple_empty);
    slot_set(self, op_off.result_names, tuple_empty);
    slot_set(self, op_off.result_oids, tuple_empty);
    slot_set(self, op_off.result_formats, tuple_empty);
    slot_set(self, op_off.one_row, Py_None);
    slot_set(self, op_off.one_value, Py_None);
    slot_set(self, op_off.have_value, Py_False);
    slot_set(self, op_off.command, str_empty);
    slot_set(self, op_off.error, Py_None);
    slot_set(self, op_off.discarded, Py_False);

    if (mode == mode_fetch) {
        PyObject *rows = PyList_New(0);
        if (rows == NULL) return -1;
        slot_steal(self, op_off.rows, rows);
    } else {
        slot_set(self, op_off.rows, Py_None);
    }
    return 0;
}

/* ------------------------------------------------------------------ *
 * _closes_prefix
 * ------------------------------------------------------------------ */

static PyObject *
build_closes_prefix(PyObject *self)
{
    PyObject *pending = SLOT_BORROW(self, conn_off.pending_closes, "_pending_closes");
    PyObject *emitted;
    PyObject *in_flight = NULL;
    PyObject *closeable = NULL;
    PyObject *retained = NULL;
    PyObject *joined = NULL;
    Py_ssize_t count;

    if (pending == NULL) return NULL;
    count = PyList_Size(pending);
    if (count < 0) return NULL;
    if (count == 0) return Py_NewRef(bytes_empty);

    emitted = SLOT_BORROW(self, conn_off.emitted, "_emitted");
    if (emitted == NULL) return NULL;

    /* Statement names an in-flight operation still refers to must not be
       closed underneath it; they stay pending for a later operation. */
    in_flight = PySet_New(NULL);
    if (in_flight == NULL) return NULL;
    {
        PyObject *iterator = PyObject_GetIter(emitted);
        PyObject *item;
        if (iterator == NULL) goto error;
        while ((item = PyIter_Next(iterator)) != NULL) {
            PyObject *name = SLOT(item, op_off.statement_name);
            if (name != NULL && PySet_Add(in_flight, name) < 0) {
                Py_DECREF(item);
                Py_DECREF(iterator);
                goto error;
            }
            Py_DECREF(item);
        }
        Py_DECREF(iterator);
        if (PyErr_Occurred()) goto error;
    }

    closeable = PyList_New(0);
    retained = PyList_New(0);
    if (closeable == NULL || retained == NULL) goto error;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *name = PyList_GET_ITEM(pending, i);
        int held = PySet_Contains(in_flight, name);
        if (held < 0) goto error;
        if (PyList_Append(held ? retained : closeable, name) < 0) goto error;
    }
    if (PyList_GET_SIZE(closeable) == 0) {
        Py_DECREF(in_flight);
        Py_DECREF(closeable);
        Py_DECREF(retained);
        return Py_NewRef(bytes_empty);
    }
    slot_set(self, conn_off.pending_closes, retained);

    /* b"".join(_message(b"C", b"S" + _cstring(name)) for name in closeable) */
    {
        PyObject *parts = PyList_New(0);
        if (parts == NULL) goto error;
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(closeable); i++) {
            PyObject *name = PyList_GET_ITEM(closeable, i);
            PyObject *cstring = PyObject_CallOneArg(fn_cstring, name);
            PyObject *body = NULL;
            PyObject *message = NULL;
            PyObject *prefix = PyBytes_FromStringAndSize("S", 1);
            if (cstring == NULL || prefix == NULL) {
                Py_XDECREF(cstring);
                Py_XDECREF(prefix);
                Py_DECREF(parts);
                goto error;
            }
            body = PySequence_Concat(prefix, cstring);
            Py_DECREF(prefix);
            Py_DECREF(cstring);
            if (body == NULL) { Py_DECREF(parts); goto error; }
            {
                PyObject *kind = PyBytes_FromStringAndSize("C", 1);
                if (kind == NULL) {
                    Py_DECREF(body);
                    Py_DECREF(parts);
                    goto error;
                }
                message = PyObject_CallFunctionObjArgs(fn_message, kind, body, NULL);
                Py_DECREF(kind);
            }
            Py_DECREF(body);
            if (message == NULL || PyList_Append(parts, message) < 0) {
                Py_XDECREF(message);
                Py_DECREF(parts);
                goto error;
            }
            Py_DECREF(message);
        }
        joined = PyObject_CallMethodOneArg(bytes_empty, str_join, parts);
        Py_DECREF(parts);
    }
    Py_DECREF(in_flight);
    Py_DECREF(closeable);
    return joined;

error:
    Py_XDECREF(in_flight);
    Py_XDECREF(closeable);
    Py_XDECREF(retained);
    return NULL;
}

static PyObject *
connection_closes_prefix(PyObject *self, PyObject *unused)
{
    (void)unused;
    return build_closes_prefix(self);
}

/* ------------------------------------------------------------------ *
 * _publish_completed / _finish_operation
 * ------------------------------------------------------------------ */

static int
publish_completed(PyObject *self)
{
    PyObject *completed = SLOT_BORROW(self, conn_off.completed, "_completed");
    if (completed == NULL) return -1;

    for (;;) {
        PyObject *operation;
        PyObject *future;
        PyObject *error;
        PyObject *mode;
        PyObject *value;
        PyObject *result;
        int done;

        if (PyObject_Size(completed) == 0) break;
        operation = call_method0(completed, str_popleft);
        if (operation == NULL) return -1;

        future = SLOT(operation, op_off.future);
        if (future == NULL) { Py_DECREF(operation); return -1; }
        done = future_is_done(future);
        if (done < 0) { Py_DECREF(operation); return -1; }
        if (done || slot_is_true(operation, op_off.discarded)) {
            Py_DECREF(operation);
            continue;
        }

        error = SLOT(operation, op_off.error);
        if (error != NULL && error != Py_None) {
            result = PyObject_CallMethodOneArg(future, str_set_exception, error);
            Py_DECREF(operation);
            if (result == NULL) return -1;
            Py_DECREF(result);
            continue;
        }

        mode = SLOT(operation, op_off.mode);
        if (mode == mode_execute) {
            value = SLOT(operation, op_off.command);
        } else if (mode == mode_fetch) {
            value = SLOT(operation, op_off.rows);
        } else if (mode == mode_fetchrow) {
            value = SLOT(operation, op_off.one_row);
        } else {
            value = SLOT(operation, op_off.one_value);
        }
        if (value == NULL) value = Py_None;
        result = PyObject_CallMethodOneArg(future, str_set_result, value);
        Py_DECREF(operation);
        if (result == NULL) return -1;
        Py_DECREF(result);
    }
    return 0;
}

static int
finish_operation(PyObject *self, PyObject *operation)
{
    PyObject *sql = SLOT(operation, op_off.sql);
    PyObject *error = SLOT(operation, op_off.error);
    PyObject *completed;

    slot_set(operation, op_off.state, str_completed);

    if (slot_is_true(operation, op_off.cold) &&
        (error == NULL || error == Py_None)) {
        int borrowed;
        PyObject *plan_type = hook_get(self, hook_plan_type, str_plan_type, &borrowed);
        PyObject *plan;
        PyObject *cost;
        PyObject *evicted;
        if (plan_type == NULL) return -1;
        plan = PyObject_CallFunctionObjArgs(
            plan_type,
            SLOT(operation, op_off.statement_name),
            SLOT(operation, op_off.parameter_oids),
            SLOT(operation, op_off.result_oids),
            SLOT(operation, op_off.result_names),
            NULL
        );
        hook_release(plan_type, borrowed);
        if (plan == NULL) return -1;

        cost = PyObject_CallFunctionObjArgs(fn_plan_retained_bytes, sql, plan, NULL);
        if (cost == NULL) { Py_DECREF(plan); return -1; }
        {
            PyObject *plans = SLOT_BORROW(self, conn_off.plans, "_plans");
            PyObject *kwargs = PyDict_New();
            PyObject *args = NULL;
            PyObject *outcome = NULL;
            PyObject *setter = NULL;
            if (plans == NULL || kwargs == NULL ||
                PyDict_SetItemString(kwargs, "cost", cost) < 0) {
                Py_XDECREF(kwargs);
                Py_DECREF(cost);
                Py_DECREF(plan);
                return -1;
            }
            args = PyTuple_Pack(2, sql, plan);
            setter = args == NULL ? NULL : PyObject_GetAttr(plans, str_set);
            if (setter != NULL) outcome = PyObject_Call(setter, args, kwargs);
            Py_XDECREF(setter);
            Py_XDECREF(args);
            Py_DECREF(kwargs);
            Py_DECREF(cost);
            Py_DECREF(plan);
            if (outcome == NULL) return -1;
            Py_DECREF(outcome);
        }

        /* Whatever that displaced has to be closed on the wire. */
        {
            PyObject *plans = SLOT(self, conn_off.plans);
            PyObject *pending = SLOT(self, conn_off.pending_closes);
            PyObject *iterator;
            PyObject *item;
            evicted = call_method0(plans, str_take_evicted);
            if (evicted == NULL) return -1;
            iterator = PyObject_GetIter(evicted);
            Py_DECREF(evicted);
            if (iterator == NULL) return -1;
            while ((item = PyIter_Next(iterator)) != NULL) {
                PyObject *entry = PySequence_GetItem(item, 1);
                PyObject *name = entry == NULL
                    ? NULL : PyObject_GetAttr(entry, str_statement_name);
                Py_XDECREF(entry);
                Py_DECREF(item);
                if (name == NULL || PyList_Append(pending, name) < 0) {
                    Py_XDECREF(name);
                    Py_DECREF(iterator);
                    return -1;
                }
                Py_DECREF(name);
            }
            Py_DECREF(iterator);
            if (PyErr_Occurred()) return -1;
        }
    }

    if (slot_is_true(self, conn_off.transaction_barrier)) {
        int truth = is_transaction_sql(sql);
        if (truth < 0) return -1;
        if (truth) slot_set(self, conn_off.transaction_barrier, Py_False);
    }

    completed = SLOT_BORROW(self, conn_off.completed, "_completed");
    if (completed == NULL) return -1;
    {
        PyObject *result = PyObject_CallMethodOneArg(completed, str_append, operation);
        if (result == NULL) return -1;
        Py_DECREF(result);
    }
    /* Through the attribute rather than calling `publish_completed` directly.
       The reference calls `self._publish_completed()`, and
       `test_a_reader_defect_fails_every_caller_wherever_it_is_raised` injects a
       fault at exactly this seam -- a direct C call would fuse the two points
       and silently remove one of the four the test walks. One attribute lookup
       per completed operation against a ~15us round trip; the frames this file
       exists to remove were in `_submit`, not here. */
    {
        PyObject *method = PyObject_GetAttr(self, str_publish_completed);
        PyObject *outcome;
        if (method == NULL) return -1;
        outcome = PyObject_CallNoArgs(method);
        Py_DECREF(method);
        if (outcome == NULL) return -1;
        Py_DECREF(outcome);
    }
    return 0;
}

static PyObject *
connection_finish_operation(PyObject *self, PyObject *operation)
{
    if (finish_operation(self, operation) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
connection_publish_completed(PyObject *self, PyObject *unused)
{
    (void)unused;
    if (publish_completed(self) < 0) return NULL;
    Py_RETURN_NONE;
}

/* ------------------------------------------------------------------ *
 * _flush
 * ------------------------------------------------------------------ */

static int
schedule_flush(PyObject *self)
{
    PyObject *loop = SLOT_BORROW(self, conn_off.loop, "_loop");
    PyObject *bound;
    PyObject *handle;
    if (loop == NULL) return -1;
    bound = PyObject_GetAttr(self, str_flush_method);
    if (bound == NULL) return -1;
    handle = PyObject_CallMethodOneArg(loop, str_call_soon, bound);
    Py_DECREF(bound);
    if (handle == NULL) return -1;
    slot_steal(self, conn_off.flush_handle, handle);
    return 0;
}

static int
do_flush(PyObject *self)
{
    PyObject *waiting;
    PyObject *emitted;
    PyObject *packets = NULL;
    PyObject *operations = NULL;
    Py_ssize_t available;
    Py_ssize_t batch_size = 0;
    Py_ssize_t emitted_now = 0;
    Py_ssize_t waiting_live;
    Py_ssize_t max_emitted;
    Py_ssize_t max_batch;
    int failed = 0;

    slot_set(self, conn_off.flush_handle, Py_None);

    if (slot_is_true(self, conn_off.closed) ||
        slot_is_true(self, conn_off.write_blocked)) return 0;
    waiting_live = slot_as_ssize(self, conn_off.waiting_live, "_waiting_live");
    if (waiting_live < 0) return PyErr_Occurred() ? -1 : 0;
    if (waiting_live == 0) return 0;

    max_emitted = class_bound(self, str_max_emitted);
    max_batch = class_bound(self, str_max_outbound);
    if (max_emitted < 0 || max_batch < 0) return -1;

    waiting = SLOT_BORROW(self, conn_off.waiting, "_waiting");
    emitted = SLOT_BORROW(self, conn_off.emitted, "_emitted");
    if (waiting == NULL || emitted == NULL) return -1;

    available = max_emitted - PyObject_Size(emitted);
    if (available <= 0) return PyErr_Occurred() ? -1 : 0;

    packets = PyList_New(0);
    operations = PyList_New(0);
    if (packets == NULL || operations == NULL) goto error;

    while (PyObject_Size(waiting) > 0 && emitted_now < available) {
        PyObject *operation = PySequence_GetItem(waiting, 0);
        PyObject *state;
        PyObject *packet;
        Py_ssize_t packet_size;
        int cancelled;

        if (operation == NULL) goto error;
        state = SLOT(operation, op_off.state);
        /* Tombstones left by `_cancel_operation`. Drained here rather than
           removed there, and skipped before the batch-size test so a cancelled
           operation's packet cannot end a batch it will never join. */
        if (state == str_cancelled) {
            PyObject *dropped = call_method0(waiting, str_popleft);
            Py_DECREF(operation);
            if (dropped == NULL) goto error;
            Py_DECREF(dropped);
            continue;
        }
        packet = SLOT(operation, op_off.packet);
        packet_size = packet == NULL ? 0 : PyBytes_Size(packet);
        if (packet_size < 0) { Py_DECREF(operation); goto error; }
        if (PyList_GET_SIZE(packets) > 0 && batch_size + packet_size > max_batch) {
            Py_DECREF(operation);
            break;
        }
        {
            PyObject *dropped = call_method0(waiting, str_popleft);
            if (dropped == NULL) { Py_DECREF(operation); goto error; }
            Py_DECREF(dropped);
        }
        waiting_live -= 1;
        if (slot_set_ssize(self, conn_off.waiting_live, waiting_live) < 0) {
            Py_DECREF(operation);
            goto error;
        }
        cancelled = future_is_cancelled(SLOT(operation, op_off.future));
        if (cancelled < 0) { Py_DECREF(operation); goto error; }
        if (cancelled) {
            slot_set(operation, op_off.state, str_cancelled);
            Py_DECREF(operation);
            continue;
        }
        if (PyList_Append(packets, packet) < 0 ||
            PyList_Append(operations, operation) < 0) {
            Py_DECREF(operation);
            goto error;
        }
        batch_size += packet_size;
        slot_set(operation, op_off.state, str_emitted);
        {
            PyObject *result = PyObject_CallMethodOneArg(emitted, str_append, operation);
            Py_DECREF(operation);
            if (result == NULL) goto error;
            Py_DECREF(result);
        }
        emitted_now += 1;
    }

    if (PyList_GET_SIZE(packets) > 0) {
        /* Once per flight, not once per operation. `_idle_event` is edge state
           -- "is the pipeline empty" -- so clearing it k times to emit a batch
           of k did the same work k times, and only two tests ever wait on it. */
        PyObject *idle = SLOT(self, conn_off.idle_event);
        PyObject *payload = NULL;
        PyObject *pending = NULL;
        PyObject *hook = SLOT(self, conn_off.register_operations);
        PyObject *backpressure = SLOT(self, conn_off.write_with_backpressure);

        if (idle != NULL) {
            PyObject *cleared = call_method0(idle, str_clear);
            if (cleared == NULL) goto error;
            Py_DECREF(cleared);
        }
        if (hook != NULL && hook != Py_None) {
            PyObject *tuple = PyList_AsTuple(operations);
            PyObject *result = tuple == NULL
                ? NULL : PyObject_CallOneArg(hook, tuple);
            Py_XDECREF(tuple);
            if (result == NULL) goto write_error;
            Py_DECREF(result);
        }
        if (PyList_GET_SIZE(packets) == 1) {
            payload = Py_NewRef(PyList_GET_ITEM(packets, 0));
        } else {
            int borrowed;
            PyObject *joiner = hook_get(
                self, hook_join_packets, str_join_packets, &borrowed);
            PyObject *tuple = PyList_AsTuple(packets);
            payload = (joiner == NULL || tuple == NULL)
                ? NULL : PyObject_CallOneArg(joiner, tuple);
            hook_release(joiner, borrowed);
            Py_XDECREF(tuple);
            if (payload == NULL) goto write_error;
        }
        if (backpressure == NULL || backpressure == Py_None) {
            PyObject *writer = SLOT(self, conn_off.writer);
            PyObject *written = writer == NULL
                ? NULL : PyObject_CallMethodOneArg(writer, str_write, payload);
            if (written == NULL) { Py_DECREF(payload); goto write_error; }
            Py_DECREF(written);
            pending = call_method0(writer, str_drain);
        } else {
            pending = PyObject_CallOneArg(backpressure, payload);
        }
        Py_DECREF(payload);
        if (pending == NULL) goto write_error;

        {
            Py_ssize_t count = slot_as_ssize(self, conn_off.write_count, "_write_count");
            if (count < 0 && PyErr_Occurred()) { Py_DECREF(pending); goto error; }
            if (slot_set_ssize(self, conn_off.write_count, count + 1) < 0) {
                Py_DECREF(pending);
                goto error;
            }
        }

        if (pending != Py_None) {
            int settled = 0;
            if (PyObject_IsInstance(pending, (PyObject *)&PyBaseObject_Type) >= 0) {
                /* A Future that has already resolved needs no drain task. */
                PyObject *done_attr = PyObject_GetAttr(pending, str_done);
                if (done_attr != NULL) {
                    PyObject *outcome = PyObject_CallNoArgs(done_attr);
                    Py_DECREF(done_attr);
                    if (outcome != NULL) {
                        settled = PyObject_IsTrue(outcome);
                        Py_DECREF(outcome);
                    } else {
                        PyErr_Clear();
                    }
                } else {
                    PyErr_Clear();
                }
            }
            if (!settled) {
                PyObject *loop = SLOT(self, conn_off.loop);
                PyObject *drain_method = PyObject_GetAttr(self, str_drain_method);
                PyObject *coroutine = drain_method == NULL
                    ? NULL : PyObject_CallOneArg(drain_method, pending);
                PyObject *task = NULL;
                Py_XDECREF(drain_method);
                if (coroutine != NULL) {
                    task = PyObject_CallMethodOneArg(loop, str_create_task, coroutine);
                    Py_DECREF(coroutine);
                }
                if (task == NULL) { Py_DECREF(pending); goto error; }
                slot_set(self, conn_off.write_blocked, Py_True);
                {
                    PyObject *tracked = PyObject_CallMethodOneArg(
                        self, str_track_background, task);
                    Py_DECREF(task);
                    if (tracked == NULL) { Py_DECREF(pending); goto error; }
                    Py_DECREF(tracked);
                }
            }
        }
        Py_DECREF(pending);

        {
            /* One question again. The reader waits on the socket now, so
               there is nothing to wake: this flight's write is what makes the
               server answer, and the answer is what wakes the read. See the
               loop comment in `_read_pipeline`. */
            PyObject *reader = SLOT(self, conn_off.reader_task);
            if (reader == NULL || reader == Py_None) {
                PyObject *loop = SLOT(self, conn_off.loop);
                PyObject *method = PyObject_GetAttr(self, str_read_pipeline);
                PyObject *coroutine = method == NULL
                    ? NULL : PyObject_CallNoArgs(method);
                PyObject *task = NULL;
                Py_XDECREF(method);
                if (coroutine != NULL) {
                    task = PyObject_CallMethodOneArg(loop, str_create_task, coroutine);
                    Py_DECREF(coroutine);
                }
                if (task == NULL) goto error;
                slot_steal(self, conn_off.reader_task, task);
            }
        }
    }

    if (waiting_live > 0 && PyObject_Size(emitted) < max_emitted) {
        if (schedule_flush(self) < 0) goto error;
    }

    Py_DECREF(packets);
    Py_DECREF(operations);
    return failed;

write_error:
    /* An OSError from the transport is a lost connection, and every caller has
       to be failed rather than left waiting on a write that never happened. */
    if (PyErr_ExceptionMatches(PyExc_OSError)) {
        PyObject *type, *value, *traceback;
        PyObject *failure;
        PyErr_Fetch(&type, &value, &traceback);
        PyErr_NormalizeException(&type, &value, &traceback);
        failure = PyObject_CallFunction(
            exc_operational, "s", "PostgreSQL connection lost");
        if (failure != NULL) {
            PyObject *handled = PyObject_CallMethodObjArgs(
                self, str_fail_connection, failure,
                value == NULL ? Py_None : value, NULL);
            Py_DECREF(failure);
            Py_XDECREF(handled);
        }
        Py_XDECREF(type);
        Py_XDECREF(value);
        Py_XDECREF(traceback);
        Py_XDECREF(packets);
        Py_XDECREF(operations);
        if (PyErr_Occurred()) return -1;
        return 0;
    }
error:
    Py_XDECREF(packets);
    Py_XDECREF(operations);
    return -1;
}

static PyObject *
connection_flush(PyObject *self, PyObject *unused)
{
    (void)unused;
    if (do_flush(self) < 0) return NULL;
    Py_RETURN_NONE;
}

/* ------------------------------------------------------------------ *
 * _submit
 * ------------------------------------------------------------------ */

/* Awaitable returned by `_submit`: delegates to the operation's future and
   turns a cancellation into `_cancel_operation` on the way through.
 *
 * In the Python reference `_submit` is an `async def` whose body ends in
 * `await future` inside a `try/except CancelledError`. Reproducing that as a
 * coroutine object would reintroduce the frame this file exists to remove, so
 * the delegation is written out: `__await__` hands back an iterator over the
 * future's own await, and `throw` intercepts the cancellation. */
/* Submission is deferred to the first await, not done when `fetch()` returns.
 *
 * That is not an implementation detail, it is the contract. `async def fetch`
 * returned a coroutine that had not run yet, so
 *
 *     task = asyncio.create_task(connection.fetchval(sql))
 *
 * put every refusal -- pipeline full, closed connection, transaction barrier --
 * inside the task, where the caller awaits it. Submitting eagerly in `fetch()`
 * moves those raises to the `create_task(...)` line, out of the task and into
 * whatever frame happened to build the awaitable. `test_queue_limit_raises_
 * pipeline_full` caught exactly that, and it was right to: the operation is not
 * ordered against its neighbours until it is submitted either, so an eager
 * `_submit` also reorders a pipeline against the order the caller awaited in.
 *
 * So the awaitable carries the arguments and calls `submit()` on its first
 * step. `await connection.fetch(...)` is byte-for-byte the same sequence it was
 * as a coroutine; what is gone is the coroutine object and its frame. */
typedef struct {
    PyObject_HEAD
    PyObject *connection;
    PyObject *mode;
    PyObject *sql;
    PyObject *args;
    PyObject *dest;
    PyObject *operation;  /* NULL until the first step submits */
    PyObject *iterator;   /* future.__await__() */
    int started;
} SubmitAwait;

static PyTypeObject *submit_await_type = NULL;

static int cancel_operation(PyObject *self, PyObject *operation);

static void
submit_await_dealloc(SubmitAwait *self)
{
    PyTypeObject *type = Py_TYPE(self);
    Py_CLEAR(self->connection);
    Py_CLEAR(self->mode);
    Py_CLEAR(self->sql);
    Py_CLEAR(self->args);
    Py_CLEAR(self->dest);
    Py_CLEAR(self->operation);
    Py_CLEAR(self->iterator);
    type->tp_free((PyObject *)self);
    Py_DECREF(type);
}

static int
submit_await_traverse(SubmitAwait *self, visitproc visit, void *arg)
{
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->connection);
    Py_VISIT(self->mode);
    Py_VISIT(self->sql);
    Py_VISIT(self->args);
    Py_VISIT(self->dest);
    Py_VISIT(self->operation);
    Py_VISIT(self->iterator);
    return 0;
}

static PyObject *submit(PyObject *self, PyObject *mode, PyObject *sql,
                       PyObject *args, PyObject *dest);

static int
submit_await_ensure_iterator(SubmitAwait *self)
{
    PyObject *future;
    unaryfunc await;
    if (self->iterator != NULL) return 0;
    if (self->started) {
        PyErr_SetString(PyExc_RuntimeError, "operation awaitable already consumed");
        return -1;
    }
    self->started = 1;
    /* The submission itself, on the first step -- see the type comment. */
    self->operation = submit(
        self->connection, self->mode, self->sql, self->args, self->dest);
    Py_CLEAR(self->mode);
    Py_CLEAR(self->sql);
    Py_CLEAR(self->args);
    Py_CLEAR(self->dest);
    if (self->operation == NULL) return -1;
    future = SLOT(self->operation, op_off.future);
    if (future == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "operation has no future");
        return -1;
    }
    await = Py_TYPE(future)->tp_as_async ? Py_TYPE(future)->tp_as_async->am_await : NULL;
    if (await == NULL) {
        PyErr_SetString(PyExc_TypeError, "operation future is not awaitable");
        return -1;
    }
    self->iterator = await(future);
    return self->iterator == NULL ? -1 : 0;
}

/* `asyncio.CancelledError`, resolved at init. Routing it to
   `_cancel_operation` and re-raising is the `except asyncio.CancelledError`
   arm of the reference implementation, and it is what turns an awaiter's
   cancellation into a tombstone or a CancelRequest rather than an operation
   nobody is left waiting for. */
static PyObject *exc_cancelled_error = NULL;

static PyObject *
submit_await_step(SubmitAwait *self, PyObject *sent)
{
    PyObject *result;
    if (submit_await_ensure_iterator(self) < 0) return NULL;
    if (sent == NULL || sent == Py_None) {
        result = Py_TYPE(self->iterator)->tp_iternext(self->iterator);
    } else {
        result = PyObject_CallMethod(self->iterator, "send", "O", sent);
    }
    if (result == NULL && self->connection != NULL && self->operation != NULL &&
        PyErr_ExceptionMatches(exc_cancelled_error)) {
        PyObject *type, *value, *traceback;
        PyErr_Fetch(&type, &value, &traceback);
        if (cancel_operation(self->connection, self->operation) < 0) {
            Py_XDECREF(type);
            Py_XDECREF(value);
            Py_XDECREF(traceback);
            return NULL;
        }
        PyErr_Restore(type, value, traceback);
    }
    return result;
}

static PyObject *
submit_await_iternext(SubmitAwait *self)
{
    return submit_await_step(self, NULL);
}

static PyObject *
submit_await_await(SubmitAwait *self)
{
    return Py_NewRef((PyObject *)self);
}

static PyObject *
submit_await_send(SubmitAwait *self, PyObject *value)
{
    return submit_await_step(self, value);
}

static PyObject *
submit_await_throw(SubmitAwait *self, PyObject *args)
{
    PyObject *result;
    if (submit_await_ensure_iterator(self) < 0) return NULL;
    result = PyObject_CallMethod(self->iterator, "throw", "O", args);
    if (result == NULL && self->connection != NULL && self->operation != NULL &&
        PyErr_ExceptionMatches(exc_cancelled_error)) {
        PyObject *type, *value, *traceback;
        PyErr_Fetch(&type, &value, &traceback);
        if (cancel_operation(self->connection, self->operation) < 0) {
            Py_XDECREF(type);
            Py_XDECREF(value);
            Py_XDECREF(traceback);
            return NULL;
        }
        PyErr_Restore(type, value, traceback);
    }
    return result;
}

static PyObject *
submit_await_close(SubmitAwait *self, PyObject *unused)
{
    (void)unused;
    if (self->iterator != NULL) {
        PyObject *result = PyObject_CallMethod(self->iterator, "close", NULL);
        if (result == NULL) {
            if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return NULL;
            PyErr_Clear();
        } else {
            Py_DECREF(result);
        }
    }
    Py_RETURN_NONE;
}

static PyMethodDef submit_await_methods[] = {
    {"send", (PyCFunction)submit_await_send, METH_O, NULL},
    {"throw", (PyCFunction)submit_await_throw, METH_VARARGS, NULL},
    {"close", (PyCFunction)submit_await_close, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot submit_await_slots[] = {
    {Py_tp_dealloc, submit_await_dealloc},
    {Py_tp_traverse, submit_await_traverse},
    {Py_am_await, submit_await_await},
    {Py_tp_iter, PyObject_SelfIter},
    {Py_tp_iternext, submit_await_iternext},
    {Py_tp_methods, submit_await_methods},
    {0, NULL}
};

static PyType_Spec submit_await_spec = {
    .name = "wreath._native._postgres._SubmitAwait",
    .basicsize = sizeof(SubmitAwait),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = submit_await_slots,
};

/* ------------------------------------------------------------------ *
 * _cancel_operation
 * ------------------------------------------------------------------ */

static int
cancel_operation(PyObject *self, PyObject *operation)
{
    PyObject *method = PyObject_GetAttrString(self, "_cancel_operation");
    PyObject *result;
    if (method == NULL) return -1;
    result = PyObject_CallOneArg(method, operation);
    Py_DECREF(method);
    if (result == NULL) return -1;
    Py_DECREF(result);
    return 0;
}

/* ------------------------------------------------------------------ *
 * The submit path
 * ------------------------------------------------------------------ */

static PyObject *
submit(PyObject *self, PyObject *mode, PyObject *sql, PyObject *args, PyObject *dest)
{
    PyObject *waiting;
    PyObject *emitted;
    PyObject *future = NULL;
    PyObject *operation = NULL;
    PyObject *plan = NULL;
    PyObject *packet = NULL;
    PyObject *closes = NULL;
    Py_ssize_t outstanding;
    Py_ssize_t waiting_live;
    Py_ssize_t sequence;
    Py_ssize_t max_queued;
    Py_ssize_t max_batch;
    int transaction_sql;
    int batch_decode;

    if (slot_is_true(self, conn_off.closed)) {
        PyErr_SetString(exc_interface, "connection is closed");
        return NULL;
    }
    if (!PyUnicode_Check(sql) || PyUnicode_GET_LENGTH(sql) == 0) {
        PyErr_SetString(exc_interface, "SQL must be a non-empty string");
        return NULL;
    }

    waiting_live = slot_as_ssize(self, conn_off.waiting_live, "_waiting_live");
    if (waiting_live < 0 && PyErr_Occurred()) return NULL;
    emitted = SLOT_BORROW(self, conn_off.emitted, "_emitted");
    if (emitted == NULL) return NULL;
    outstanding = waiting_live + PyObject_Size(emitted);
    if (outstanding < 0 && PyErr_Occurred()) return NULL;

    transaction_sql = is_transaction_sql(sql);
    if (transaction_sql < 0) return NULL;
    {
        PyObject *status = SLOT(self, conn_off.transaction_status);
        int in_transaction = status == NULL
            ? 0 : PyObject_RichCompareBool(status, bytes_idle, Py_NE);
        if (in_transaction < 0) return NULL;
        if ((in_transaction || slot_is_true(self, conn_off.transaction_barrier)) &&
            outstanding) {
            PyErr_SetString(
                exc_interface, "explicit transactions reject concurrent operations");
            return NULL;
        }
    }
    if (transaction_sql && outstanding) {
        PyErr_SetString(
            exc_interface, "transaction control cannot enter an active pipeline");
        return NULL;
    }
    max_queued = class_bound(self, str_max_queued);
    if (max_queued < 0) return NULL;
    if (outstanding >= max_queued) {
        PyErr_SetString(exc_pipeline_full, "PostgreSQL pipeline is full");
        return NULL;
    }

    sequence = slot_as_ssize(self, conn_off.sequence, "_sequence");
    if (sequence < 0 && PyErr_Occurred()) return NULL;
    sequence += 1;
    if (slot_set_ssize(self, conn_off.sequence, sequence) < 0) return NULL;

    {
        PyObject *loop = SLOT_BORROW(self, conn_off.loop, "_loop");
        if (loop == NULL) return NULL;
        future = call_method0(loop, str_create_future);
        if (future == NULL) return NULL;
    }
    {
        int borrowed;
        PyObject *operation_type = hook_get(
            self, hook_operation_type, str_operation_type, &borrowed);
        PyObject *sequence_object = PyLong_FromSsize_t(sequence);
        if (operation_type == NULL || sequence_object == NULL) {
            hook_release(operation_type, borrowed);
            Py_XDECREF(sequence_object);
            Py_DECREF(future);
            return NULL;
        }
        operation = PyObject_CallFunctionObjArgs(
            operation_type, sequence_object, sql, args, mode, future, Py_None, NULL);
        hook_release(operation_type, borrowed);
        Py_DECREF(sequence_object);
        Py_DECREF(future);
        if (operation == NULL) return NULL;
    }
    if (dest != NULL && dest != Py_None) slot_set(operation, op_off.dest, dest);

    if ((PyObject *)Py_TYPE(self) == connection_type_ref) {
        batch_decode = hook_batch_decode;
    } else {
        PyObject *flag = PyObject_GetAttr(self, str_batch_decode);
        if (flag == NULL) goto error;
        batch_decode = PyObject_IsTrue(flag);
        Py_DECREF(flag);
        if (batch_decode < 0) goto error;
    }

    /* Plan lookup; a hit is also the recency update. */
    {
        PyObject *plans = SLOT(self, conn_off.plans);
        PyObject *getter = plans == NULL ? NULL : PyObject_GetAttr(plans, str_get);
        if (getter == NULL) goto error;
        plan = PyObject_CallOneArg(getter, sql);
        Py_DECREF(getter);
        if (plan == NULL) goto error;
    }
    slot_set(operation, op_off.plan, plan);
    slot_set(operation, op_off.cold, plan == Py_None ? Py_True : Py_False);

    if (plan == Py_None) {
        Py_ssize_t statement_id = slot_as_ssize(
            self, conn_off.statement_id, "_statement_id");
        PyObject *name;
        PyObject *oids;
        PyObject *builder;
        if (statement_id < 0 && PyErr_Occurred()) goto error;
        statement_id += 1;
        if (slot_set_ssize(self, conn_off.statement_id, statement_id) < 0) goto error;
        name = PyUnicode_FromFormat("wreath_%zd", statement_id);
        if (name == NULL) goto error;
        {
            PyObject *encoded = PyUnicode_AsASCIIString(name);
            Py_DECREF(name);
            if (encoded == NULL) goto error;
            slot_steal(operation, op_off.statement_name, encoded);
        }
        {
            Py_ssize_t count = PyTuple_Check(args) ? PyTuple_GET_SIZE(args) : 0;
            oids = PyTuple_New(count);
            if (oids == NULL) goto error;
            for (Py_ssize_t i = 0; i < count; i++) {
                PyObject *oid = PyObject_CallOneArg(
                    fn_infer_oid, PyTuple_GET_ITEM(args, i));
                if (oid == NULL) { Py_DECREF(oids); goto error; }
                PyTuple_SET_ITEM(oids, i, oid);
            }
        }
        slot_steal(operation, op_off.parameter_oids, oids);
        if (dest == NULL || dest == Py_None) {
            int borrowed;
            builder = hook_get(self, hook_build_cold, str_build_cold, &borrowed);
            if (builder == NULL) goto error;
            packet = PyObject_CallFunctionObjArgs(
                builder,
                SLOT(operation, op_off.statement_name),
                sql, args,
                SLOT(operation, op_off.parameter_oids),
                mode, NULL
            );
            hook_release(builder, borrowed);
        } else {
            /* A catalog destination decodes binary only, and a cold operation
               otherwise binds text -- so the *first* catalog read on any
               connection failed, and failed as a hang. Deliberately the Python
               builder even when the C one is bound, matching the reference:
               the accelerated `_build_cold` takes five positional arguments and
               adding a sixth means moving both twins under the byte-for-byte
               parity contract to serve a path that runs once per connection,
               off the hot path, only for migration reads. */
            PyObject *call_args = PyTuple_Pack(
                5, SLOT(operation, op_off.statement_name), sql, args,
                SLOT(operation, op_off.parameter_oids), mode);
            PyObject *call_kwargs = PyDict_New();
            if (call_args == NULL || call_kwargs == NULL ||
                PyDict_SetItem(call_kwargs, str_binary_results, Py_True) < 0) {
                Py_XDECREF(call_args);
                Py_XDECREF(call_kwargs);
                goto error;
            }
            packet = PyObject_Call(fn_build_cold_query_packet, call_args, call_kwargs);
            Py_DECREF(call_args);
            Py_DECREF(call_kwargs);
        }
        if (packet == NULL) goto error;
    } else {
        PyObject *builder;
        slot_set(operation, op_off.statement_name,
                 ((WreathPgPlan *)plan)->statement_name);
        slot_set(operation, op_off.parameter_oids,
                 ((WreathPgPlan *)plan)->parameter_oids);
        slot_set(operation, op_off.result_names,
                 ((WreathPgPlan *)plan)->result_names);
        slot_set(operation, op_off.result_oids,
                 ((WreathPgPlan *)plan)->result_oids);
        if (batch_decode) {
            PyObject *result_oids = ((WreathPgPlan *)plan)->result_oids;
            slot_set(operation, op_off.decoder_plan,
                     ((WreathPgPlan *)plan)->decoder_plan);
            if (result_oids != NULL && PyObject_Size(result_oids) > 0) {
                int borrowed;
                PyObject *tape_type = hook_get(
                    self, hook_field_tape_type, str_field_tape_type, &borrowed);
                PyObject *width = PyLong_FromSsize_t(PyObject_Size(result_oids));
                PyObject *tape = NULL;
                if (tape_type != NULL && width != NULL)
                    tape = PyObject_CallOneArg(tape_type, width);
                hook_release(tape_type, borrowed);
                Py_XDECREF(width);
                if (tape == NULL) goto error;
                slot_steal(operation, op_off.field_tape, tape);
            }
        } else {
            PyObject *formats;
            Py_ssize_t count = PyObject_Size(((WreathPgPlan *)plan)->result_oids);
            if (count < 0) goto error;
            formats = PyTuple_New(count);
            if (formats == NULL) goto error;
            for (Py_ssize_t i = 0; i < count; i++)
                PyTuple_SET_ITEM(formats, i, PyLong_FromLong(1));
            slot_steal(operation, op_off.result_formats, formats);
        }
        {
            int borrowed;
            builder = hook_get(self, hook_build_cached, str_build_cached, &borrowed);
            if (builder == NULL) goto error;
            packet = PyObject_CallFunctionObjArgs(builder, plan, args, mode, NULL);
            hook_release(builder, borrowed);
        }
        if (packet == NULL) goto error;
    }

    closes = build_closes_prefix(self);
    if (closes == NULL) goto error;
    if (PyBytes_GET_SIZE(closes) > 0) {
        /* Retire evicted server statements within this operation's Sync, so the
           Close is ordered and acknowledged rather than injected loose. */
        PyObject *combined = PySequence_Concat(closes, packet);
        if (combined == NULL) goto error;
        Py_DECREF(packet);
        packet = combined;
    }
    Py_CLEAR(closes);
    slot_set(operation, op_off.packet, packet);

    max_batch = class_bound(self, str_max_outbound);
    if (max_batch < 0) goto error;
    if (PyBytes_GET_SIZE(packet) > max_batch) {
        PyErr_SetString(exc_pipeline_full, "operation exceeds maximum outbound batch");
        goto error;
    }
    Py_CLEAR(packet);

    if (transaction_sql) slot_set(self, conn_off.transaction_barrier, Py_True);

    waiting = SLOT_BORROW(self, conn_off.waiting, "_waiting");
    if (waiting == NULL) goto error;
    {
        PyObject *appended = PyObject_CallMethodOneArg(waiting, str_append, operation);
        if (appended == NULL) goto error;
        Py_DECREF(appended);
    }
    if (slot_set_ssize(self, conn_off.waiting_live, waiting_live + 1) < 0) goto error;

    /* The eager path: an otherwise idle connection writes now rather than after
       a call_soon turn, which is the whole latency win on a single query. */
    {
        int eager;
        PyObject *handle = SLOT(self, conn_off.flush_handle);
        PyObject *flag = PyObject_GetAttr(self, str_eager_flush_idle);
        if (flag == NULL) goto error;
        eager = PyObject_IsTrue(flag);
        Py_DECREF(flag);
        if (eager < 0) goto error;
        eager = eager && waiting_live == 0 && PyObject_Size(emitted) == 0 &&
                (handle == NULL || handle == Py_None);
        if (eager) {
            if (do_flush(self) < 0) goto error;
        } else if (handle == NULL || handle == Py_None) {
            if (schedule_flush(self) < 0) goto error;
        }
    }

    return operation;

error:
    Py_XDECREF(closes);
    Py_XDECREF(packet);
    Py_XDECREF(operation);
    return NULL;
}

/* Package a submission for a later await. Allocation only -- nothing touches
   the connection's state until the first step, which is what keeps a refusal
   inside the caller's task rather than at the call site. */
static PyObject *
submit_later(PyObject *self, PyObject *mode, PyObject *sql, PyObject *args,
             PyObject *dest)
{
    SubmitAwait *awaitable = PyObject_GC_New(SubmitAwait, submit_await_type);
    if (awaitable == NULL) return NULL;
    Py_INCREF(submit_await_type);
    awaitable->connection = Py_NewRef(self);
    awaitable->mode = Py_NewRef(mode);
    awaitable->sql = Py_NewRef(sql);
    awaitable->args = Py_NewRef(args);
    awaitable->dest = Py_NewRef(dest);
    awaitable->operation = NULL;
    awaitable->iterator = NULL;
    awaitable->started = 0;
    PyObject_GC_Track(awaitable);
    return (PyObject *)awaitable;
}

static PyObject *
connection_submit(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    PyObject *dest = nargs > 3 ? args[3] : Py_None;
    if (nargs < 3 || nargs > 4) {
        PyErr_SetString(PyExc_TypeError, "_submit expects (mode, sql, args[, dest])");
        return NULL;
    }
    return submit_later(self, args[0], args[1], args[2], dest);
}

/* The four public entry points. Each is one C call rather than a Python frame
   that forwards to `_submit`; on a one-row query that frame was measurable. */
static PyObject *
connection_query(PyObject *self, PyObject *const *args, Py_ssize_t nargs,
                 PyObject *mode)
{
    PyObject *argv;
    PyObject *result;
    if (nargs < 1) {
        PyErr_SetString(PyExc_TypeError, "a SQL statement is required");
        return NULL;
    }
    argv = PyTuple_New(nargs - 1);
    if (argv == NULL) return NULL;
    for (Py_ssize_t i = 1; i < nargs; i++)
        PyTuple_SET_ITEM(argv, i - 1, Py_NewRef(args[i]));
    result = submit_later(self, mode, args[0], argv, Py_None);
    Py_DECREF(argv);
    return result;
}

static PyObject *
connection_execute(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    return connection_query(self, args, nargs, mode_execute);
}

static PyObject *
connection_fetch(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    return connection_query(self, args, nargs, mode_fetch);
}

static PyObject *
connection_fetchrow(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    return connection_query(self, args, nargs, mode_fetchrow);
}

static PyObject *
connection_fetchval(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    return connection_query(self, args, nargs, mode_fetchval);
}

static PyObject *
connection_fetch_into(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 3) {
        PyErr_SetString(PyExc_TypeError, "_fetch_into expects (sql, args, dest)");
        return NULL;
    }
    return submit_later(self, mode_fetch, args[0], args[1], args[2]);
}

/* ------------------------------------------------------------------ *
 * Registration
 * ------------------------------------------------------------------ */

static PyMethodDef pipeline_methods[] = {
    {"_submit", (PyCFunction)(void (*)(void))connection_submit, METH_FASTCALL, NULL},
    {"_flush", connection_flush, METH_NOARGS, NULL},
    {"_closes_prefix", connection_closes_prefix, METH_NOARGS, NULL},
    {"_finish_operation", connection_finish_operation, METH_O, NULL},
    {"_publish_completed", connection_publish_completed, METH_NOARGS, NULL},
    {"execute", (PyCFunction)(void (*)(void))connection_execute, METH_FASTCALL, NULL},
    {"fetch", (PyCFunction)(void (*)(void))connection_fetch, METH_FASTCALL, NULL},
    {"fetchrow", (PyCFunction)(void (*)(void))connection_fetchrow, METH_FASTCALL, NULL},
    {"fetchval", (PyCFunction)(void (*)(void))connection_fetchval, METH_FASTCALL, NULL},
    {"_fetch_into", (PyCFunction)(void (*)(void))connection_fetch_into,
     METH_FASTCALL, NULL},
    {NULL, NULL, 0, NULL}
};

PyMethodDef *
wreath_pg_pipeline_methods(void)
{
    return pipeline_methods;
}

static PyObject *
intern(const char *text)
{
    return PyUnicode_InternFromString(text);
}

int
wreath_pg_pipeline_init(PyObject *module, PyObject *connection_type)
{
    PyObject *operation_base = NULL;
    PyObject *connection_base = NULL;
    PyObject *asyncio_module = NULL;

    connection_type_ref = connection_type;
    pure_module = PyImport_ImportModule("wreath._pure.postgres");
    if (pure_module == NULL) return -1;
    asyncio_module = PyImport_ImportModule("asyncio");
    if (asyncio_module == NULL) return -1;
    exc_cancelled_error = PyObject_GetAttrString(asyncio_module, "CancelledError");
    Py_DECREF(asyncio_module);
    if (exc_cancelled_error == NULL) return -1;

    connection_base = PyObject_GetAttrString(pure_module, "Connection");
    operation_base = PyObject_GetAttrString(pure_module, "Operation");
    if (connection_base == NULL || operation_base == NULL) goto error;
    if (resolve_offsets(connection_base, operation_base) < 0) goto error;
    Py_CLEAR(connection_base);
    Py_CLEAR(operation_base);

#define GRAB(target, name) \
    do { (target) = PyObject_GetAttrString(pure_module, (name)); \
         if ((target) == NULL) goto error; } while (0)
    GRAB(exc_interface, "InterfaceError");
    GRAB(exc_pipeline_full, "PipelineFullError");
    GRAB(exc_protocol, "ProtocolError");
    GRAB(exc_operational, "OperationalError");
    GRAB(exc_postgres, "PostgresError");
    GRAB(fn_is_transaction_sql, "_is_transaction_sql");
    GRAB(fn_infer_oid, "_infer_oid");
    GRAB(fn_plan_retained_bytes, "_plan_retained_bytes");
    GRAB(fn_message, "_message");
    GRAB(fn_cstring, "_cstring");
    GRAB(fn_parse_parameter_description, "_parse_parameter_description");
    GRAB(fn_parse_row_description, "_parse_row_description");
    GRAB(fn_parse_error, "_parse_error");
    GRAB(fn_data_fields, "_data_fields");
    GRAB(fn_build_cold_query_packet, "_build_cold_query_packet");
#undef GRAB

#define INTERN(target, text) \
    do { (target) = intern(text); if ((target) == NULL) goto error; } while (0)
    INTERN(str_state, "state");
    INTERN(str_cancelled, "cancelled");
    INTERN(str_emitted, "emitted");
    INTERN(str_waiting, "waiting");
    INTERN(str_completed, "completed");
    INTERN(str_done, "done");
    INTERN(str_cancelled_method, "cancelled");
    INTERN(str_set_result, "set_result");
    INTERN(str_set_exception, "set_exception");
    INTERN(str_create_future, "create_future");
    INTERN(str_call_soon, "call_soon");
    INTERN(str_create_task, "create_task");
    INTERN(str_append, "append");
    INTERN(str_popleft, "popleft");
    INTERN(str_clear, "clear");
    INTERN(str_set, "set");
    INTERN(str_get, "get");
    INTERN(str_take_evicted, "take_evicted");
    INTERN(str_write, "write");
    INTERN(str_drain, "drain");
    INTERN(str_flush_method, "_flush");
    INTERN(str_read_pipeline, "_read_pipeline");
    INTERN(str_drain_method, "_drain");
    INTERN(str_track_background, "_track_background");
    INTERN(str_fail_connection, "_fail_connection");
    INTERN(str_enqueue_notification, "_enqueue_notification");
    INTERN(str_row_count, "row_count");
    INTERN(str_max_queued, "max_queued_operations");
    INTERN(str_max_emitted, "max_emitted_operations");
    INTERN(str_max_outbound, "max_outbound_batch");
    INTERN(str_eager_flush_idle, "_eager_flush_idle");
    INTERN(str_batch_decode, "_batch_decode");
    INTERN(str_build_cold, "_build_cold");
    INTERN(str_build_cached, "_build_cached");
    INTERN(str_join_packets, "_join_packets");
    INTERN(str_field_tape_type, "_field_tape_type");
    INTERN(str_compile_decoder_plan, "_compile_decoder_plan");
    INTERN(str_decode_tape, "_decode_tape");
    INTERN(str_decode_dest, "_decode_dest");
    INTERN(str_decode, "_decode");
    INTERN(str_record_type, "_record_type");
    INTERN(str_plan_type, "_plan_type");
    INTERN(str_operation_type, "_operation_type");
    INTERN(str_statement_name, "statement_name");
    INTERN(str_join, "join");
    INTERN(str_publish_completed, "_publish_completed");
    INTERN(str_binary_results, "binary_results");
    INTERN(mode_execute, "execute");
    INTERN(mode_fetch, "fetch");
    INTERN(mode_fetchrow, "fetchrow");
    INTERN(mode_fetchval, "fetchval");
    INTERN(str_empty, "");
#undef INTERN

    bytes_empty = PyBytes_FromStringAndSize("", 0);
    bytes_idle = PyBytes_FromStringAndSize("I", 1);
    tuple_empty = PyTuple_New(0);
    if (bytes_empty == NULL || bytes_idle == NULL || tuple_empty == NULL) goto error;

    submit_await_type = (PyTypeObject *)PyType_FromSpec(&submit_await_spec);
    if (submit_await_type == NULL) goto error;
    if (PyModule_AddObjectRef(module, "_SubmitAwait",
                              (PyObject *)submit_await_type) < 0) goto error;

    /* Graft the pipeline onto the already-created Connection type.
     *
     * Not folded into `connection_spec`'s method table because these overrides
     * have to land on the *subclass* after `PyType_FromSpecWithBases` has
     * inherited the Python originals -- and because a descriptor set here is
     * visible to `resolve_offsets` having already run, so a mismatch between
     * this file and `_pure/postgres.py` fails the import rather than producing
     * a Connection whose C half reads different words from its Python half. */
    for (PyMethodDef *def = pipeline_methods; def->ml_name != NULL; def++) {
        PyObject *descriptor = PyDescr_NewMethod(
            (PyTypeObject *)connection_type, def);
        if (descriptor == NULL) goto error;
        if (PyObject_SetAttrString(connection_type, def->ml_name, descriptor) < 0) {
            Py_DECREF(descriptor);
            goto error;
        }
        Py_DECREF(descriptor);
    }
    if (PyModule_AddFunctions(module, pipeline_module_methods) < 0) goto error;
    PyType_Modified((PyTypeObject *)connection_type);

    /* Same graft for `Operation.__init__`; `operation.c` builds the type, this
       supplies the constructor once the slot offsets are known. */
    ((PyTypeObject *)WreathPgOperationType)->tp_init = operation_init;
    PyType_Modified((PyTypeObject *)WreathPgOperationType);

    /* After the graft, so the lookups see the finished type. */
#define HOOK(target, name) \
    do { (target) = PyObject_GetAttrString(connection_type, (name)); \
         if ((target) == NULL) goto error; } while (0)
    HOOK(hook_operation_type, "_operation_type");
    HOOK(hook_plan_type, "_plan_type");
    HOOK(hook_build_cold, "_build_cold");
    HOOK(hook_build_cached, "_build_cached");
    HOOK(hook_join_packets, "_join_packets");
    HOOK(hook_field_tape_type, "_field_tape_type");
#undef HOOK
    {
        PyObject *flag = PyObject_GetAttrString(connection_type, "_batch_decode");
        if (flag == NULL) goto error;
        hook_batch_decode = PyObject_IsTrue(flag);
        Py_DECREF(flag);
        if (hook_batch_decode < 0) goto error;
    }
    return 0;

error:
    Py_XDECREF(connection_base);
    Py_XDECREF(operation_base);
    return -1;
}

void
wreath_pg_pipeline_fini(void)
{
    Py_CLEAR(pure_module);
    Py_CLEAR(exc_cancelled_error);
}
