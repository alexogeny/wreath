/* Direct model hydration: field tape straight into fixed-size model cells.

   The Record path decodes every field into a Python object and packs those into
   a Record, which the ORM would then copy into a model. This path skips both:
   binary scalars are read out of the slab into unboxed inline cells, so a row of
   integers and timestamps allocates only the model itself. Only variable-width
   payloads (text, bytea, JSON) still allocate, because the Python object is the
   value.

   Record decoding is untouched -- fetch()/fetchrow() keep their exact behavior;
   this is an additional destination, selected by the ORM session. */

#include "hydrate.h"

#define hydrate_batch_budget 128  /* rows between cancellation checks */

#include "codec.h"
#include "decode.h"
#include "migration_image.h"
#include "model.h"
#include "tape.h"

#include <stdint.h>
#include <string.h>

/* One result column, resolved to the cell it fills. */
typedef struct {
    uint32_t oid;
    uint16_t kind; /* WreathPgCellKind of the target cell */
    Py_ssize_t offset;
    Py_ssize_t bit;
} WreathPgHydrateColumn;

typedef struct {
    PyObject_HEAD
    PyTypeObject *model_type;
    PyObject *identity_spec; /* ModelSpec, the first half of an identity key */
    WreathPgHydrateColumn *columns;
    Py_ssize_t column_count;
    Py_ssize_t *key_positions; /* result positions of the primary-key columns */
    Py_ssize_t key_count;
    Py_ssize_t bitmap_offset;
    Py_ssize_t bitmap_words;
    /* Test hooks, following the existing native PostgreSQL counter convention. */
    Py_ssize_t allocated;
    Py_ssize_t reused;
} WreathPgHydratePlan;

PyTypeObject *WreathPgHydratePlanType = NULL;

static PyObject *mapping_error = NULL;

#define BITMAP_LOADED 0
#define BITMAP_NULL 1

typedef struct {
    PyObject *instance;
    WreathPgModelLayout *layout;
    unsigned char *state;
    int restored;
} WreathPgHydrateSnapshot;

typedef struct {
    PyObject *created;
    WreathPgHydrateSnapshot *snapshots;
    Py_ssize_t snapshot_count;
    Py_ssize_t snapshot_capacity;
} WreathPgHydrateJournal;

static int
hydrate_journal_init(WreathPgHydrateJournal *journal)
{
    memset(journal, 0, sizeof(*journal));
    journal->created = PyList_New(0);
    return journal->created == NULL ? -1 : 0;
}

static void
hydrate_snapshot_release(WreathPgHydrateSnapshot *snapshot)
{
    if (!snapshot->restored) {
        for (Py_ssize_t i = 0; i < snapshot->layout->pointer_count; i++) {
            Py_ssize_t relative = snapshot->layout->pointer_offsets[i] -
                snapshot->layout->bitmap_offset;
            PyObject *value;
            memcpy(&value, snapshot->state + relative, sizeof(value));
            Py_XDECREF(value);
        }
    }
    Py_DECREF(snapshot->instance);
    PyMem_Free(snapshot->state);
}

static void
hydrate_journal_clear(WreathPgHydrateJournal *journal)
{
    for (Py_ssize_t i = 0; i < journal->snapshot_count; i++)
        hydrate_snapshot_release(&journal->snapshots[i]);
    PyMem_Free(journal->snapshots);
    Py_CLEAR(journal->created);
}

static int
hydrate_journal_snapshot(WreathPgHydrateJournal *journal, PyObject *instance)
{
    WreathPgModelLayout *layout = wreath_pg_model_layout_of(instance);
    WreathPgHydrateSnapshot *snapshot;
    Py_ssize_t state_size;
    if (layout == NULL) {
        PyErr_SetString(PyExc_TypeError, "model hydration target has no native layout");
        return -1;
    }
    if (journal->snapshot_count == journal->snapshot_capacity) {
        Py_ssize_t capacity = journal->snapshot_capacity == 0
            ? 8 : journal->snapshot_capacity * 2;
        WreathPgHydrateSnapshot *resized = PyMem_Realloc(
            journal->snapshots, (size_t)capacity * sizeof(*resized));
        if (resized == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        journal->snapshots = resized;
        journal->snapshot_capacity = capacity;
    }
    state_size = layout->basicsize - layout->bitmap_offset;
    snapshot = &journal->snapshots[journal->snapshot_count];
    snapshot->state = PyMem_Malloc((size_t)state_size);
    if (snapshot->state == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    memcpy(snapshot->state, (char *)instance + layout->bitmap_offset,
           (size_t)state_size);
    for (Py_ssize_t i = 0; i < layout->pointer_count; i++) {
        Py_ssize_t relative = layout->pointer_offsets[i] - layout->bitmap_offset;
        PyObject *value;
        memcpy(&value, snapshot->state + relative, sizeof(value));
        Py_XINCREF(value);
    }
    snapshot->instance = Py_NewRef(instance);
    snapshot->layout = layout;
    snapshot->restored = 0;
    journal->snapshot_count++;
    return 0;
}

static void
hydrate_journal_rollback(WreathPgHydrateJournal *journal, PyObject *identity_map)
{
    PyObject *raised = PyErr_GetRaisedException();
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(journal->created); i++) {
        if (PyDict_DelItem(identity_map, PyList_GET_ITEM(journal->created, i)) < 0)
            PyErr_Clear();
    }
    for (Py_ssize_t i = 0; i < journal->snapshot_count; i++) {
        WreathPgHydrateSnapshot *snapshot = &journal->snapshots[i];
        WreathPgModelLayout *layout = snapshot->layout;
        for (Py_ssize_t pointer = 0; pointer < layout->pointer_count; pointer++) {
            PyObject **slot = (PyObject **)(
                (char *)snapshot->instance + layout->pointer_offsets[pointer]);
            Py_CLEAR(*slot);
        }
        memcpy((char *)snapshot->instance + layout->bitmap_offset,
               snapshot->state,
               (size_t)(layout->basicsize - layout->bitmap_offset));
        snapshot->restored = 1;
    }
    PyErr_SetRaisedException(raised);
}

static uint64_t *
bitmap_words_at(PyObject *instance, const WreathPgHydratePlan *plan, int which)
{
    char *base = (char *)instance + plan->bitmap_offset;
    return (uint64_t *)(base + (Py_ssize_t)which * plan->bitmap_words * 8);
}

static void
bit_set(PyObject *instance, const WreathPgHydratePlan *plan, int which, Py_ssize_t bit,
        int value)
{
    uint64_t *words = bitmap_words_at(instance, plan, which);
    uint64_t mask = (uint64_t)1 << (bit & 63);
    if (value) {
        words[bit >> 6] |= mask;
    } else {
        words[bit >> 6] &= ~mask;
    }
}

static int
bit_get(PyObject *instance, const WreathPgHydratePlan *plan, int which, Py_ssize_t bit)
{
    uint64_t *words = bitmap_words_at(instance, plan, which);
    return (words[bit >> 6] >> (bit & 63)) & 1u;
}

static int64_t
read_be(const unsigned char *data, Py_ssize_t length)
{
    uint64_t value = 0;
    for (Py_ssize_t i = 0; i < length; i++) value = (value << 8) | data[i];
    if (length < 8 && (data[0] & 0x80)) value |= ~0ULL << (length * 8);
    return (int64_t)value;
}

/* Write one binary field straight into its inline cell. Returns 1 when the
   value was stored, 0 when this OID/format/length combination is not proven and
   the caller should fall back to the boxed decoder, -1 on error. */
static int
store_binary_inline(char *cell, const WreathPgHydrateColumn *column, int format,
                    const unsigned char *data, Py_ssize_t length)
{
    /* The same statement arrives as text on its first execution and binary once
       the plan is cached, so the format is read per decode rather than assumed. */
    if (format != 1) return 0;
    switch (column->kind) {
    case WREATH_CELL_BOOL:
        if (length != 1) goto bad_length;
        *(uint8_t *)cell = data[0] != 0;
        return 1;
    case WREATH_CELL_INT2: {
        int16_t value;
        if (length != 2) goto bad_length;
        value = (int16_t)read_be(data, 2);
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_INT4: {
        int32_t value;
        if (length != 4) goto bad_length;
        value = (int32_t)read_be(data, 4);
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_INT8: {
        int64_t value;
        if (length != 8) goto bad_length;
        value = read_be(data, 8);
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_FLOAT4: {
        float value;
        if (length != 4) goto bad_length;
        value = (float)PyFloat_Unpack4((const char *)data, 0);
        if (value == -1.0f && PyErr_Occurred()) return -1;
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_FLOAT8: {
        double value;
        if (length != 8) goto bad_length;
        value = PyFloat_Unpack8((const char *)data, 0);
        if (value == -1.0 && PyErr_Occurred()) return -1;
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_DATE: {
        int32_t value;
        if (length != 4) goto bad_length;
        value = (int32_t)read_be(data, 4);
        if (value == INT32_MAX || value == INT32_MIN) {
            PyErr_SetString(PyExc_ValueError, "date infinity is not representable");
            return -1;
        }
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_TIMESTAMP:
    case WREATH_CELL_TIMESTAMPTZ: {
        int64_t value;
        if (length != 8) goto bad_length;
        value = read_be(data, 8);
        if (value == INT64_MAX || value == INT64_MIN) {
            PyErr_SetString(PyExc_ValueError, "timestamp infinity is not representable");
            return -1;
        }
        memcpy(cell, &value, sizeof(value));
        return 1;
    }
    case WREATH_CELL_UUID:
        if (length != 16) goto bad_length;
        memcpy(cell, data, 16);
        return 1;
    default:
        /* Text, bytea, JSON, and every fallback codec produce Python objects. */
        return 0;
    }

bad_length:
    PyErr_Format(PyExc_ValueError,
                 "invalid binary length %zd for PostgreSQL OID %u", length,
                 column->oid);
    return -1;
}

/* Store an already-decoded Python object into a cell whose representation the
   binary fast path did not prove. */
static int
store_boxed(char *cell, const WreathPgHydrateColumn *column, PyObject *value)
{
    switch (column->kind) {
    case WREATH_CELL_OBJECT: {
        PyObject **slot = (PyObject **)cell;
        Py_XSETREF(*slot, Py_NewRef(value));
        return 0;
    }
    case WREATH_CELL_BOOL:
        if (!PyBool_Check(value)) goto mismatch;
        *(uint8_t *)cell = value == Py_True;
        return 0;
    case WREATH_CELL_INT2:
    case WREATH_CELL_INT4:
    case WREATH_CELL_INT8: {
        long long number = PyLong_AsLongLong(value);
        if (number == -1 && PyErr_Occurred()) return -1;
        if (column->kind == WREATH_CELL_INT2) {
            int16_t stored = (int16_t)number;
            memcpy(cell, &stored, sizeof(stored));
        } else if (column->kind == WREATH_CELL_INT4) {
            int32_t stored = (int32_t)number;
            memcpy(cell, &stored, sizeof(stored));
        } else {
            memcpy(cell, &number, sizeof(int64_t));
        }
        return 0;
    }
    case WREATH_CELL_FLOAT4: {
        float stored = (float)PyFloat_AsDouble(value);
        if (stored == -1.0f && PyErr_Occurred()) return -1;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_FLOAT8: {
        double stored = PyFloat_AsDouble(value);
        if (stored == -1.0 && PyErr_Occurred()) return -1;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_DATE: {
        int64_t days;
        int32_t stored;
        if (wreath_pg_date_days(value, &days) < 0) return -1;
        stored = (int32_t)days;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_TIMESTAMP:
    case WREATH_CELL_TIMESTAMPTZ: {
        int aware = column->kind == WREATH_CELL_TIMESTAMPTZ;
        int64_t stored;
        if (wreath_pg_check_timestamp(value, aware) < 0) return -1;
        if (wreath_pg_timestamp_micros(value, aware, &stored) < 0) return -1;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_UUID: {
        unsigned char stored[16];
        if (wreath_pg_uuid_bytes(value, stored) < 0) return -1;
        memcpy(cell, stored, sizeof(stored));
        return 0;
    }
    default:
        goto mismatch;
    }

mismatch:
    PyErr_Format(PyExc_TypeError,
                 "PostgreSQL OID %u decoded to %.200s, which does not fit its column",
                 column->oid, Py_TYPE(value)->tp_name);
    return -1;
}

/* -- plan ------------------------------------------------------------------ */

static int
hydrate_plan_traverse(PyObject *self, visitproc visit, void *arg)
{
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)self;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(plan->model_type);
    Py_VISIT(plan->identity_spec);
    return 0;
}

static int
hydrate_plan_clear(PyObject *self)
{
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)self;
    Py_CLEAR(plan->model_type);
    Py_CLEAR(plan->identity_spec);
    return 0;
}

static void
hydrate_plan_dealloc(PyObject *self)
{
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)self;
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    hydrate_plan_clear(self);
    PyMem_Free(plan->columns);
    PyMem_Free(plan->key_positions);
    type->tp_free(self);
    Py_DECREF(type);
}

static PyObject *
plan_counters(PyObject *self, void *closure)
{
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)self;
    (void)closure;
    return Py_BuildValue("{s:n,s:n}", "allocated", plan->allocated, "reused",
                         plan->reused);
}

static PyGetSetDef hydrate_plan_getset[] = {
    {"counters", plan_counters, NULL, "test-only allocation counters", NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

static PyType_Slot hydrate_plan_slots[] = {
    {Py_tp_traverse, hydrate_plan_traverse},
    {Py_tp_clear, hydrate_plan_clear},
    {Py_tp_dealloc, hydrate_plan_dealloc},
    {Py_tp_getset, hydrate_plan_getset},
    {0, NULL}
};

static PyType_Spec hydrate_plan_spec = {
    .name = "wreath._native._postgres._HydratePlan",
    .basicsize = sizeof(WreathPgHydratePlan),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = hydrate_plan_slots,
};

/* _compile_hydrate_plan(model_type, spec, targets, formats)

   `targets` maps each result position to a column index on the model; `formats`
   gives the wire format each position will arrive in. Both come from the SQL
   Wreath generated, so the mapping is positional and needs no name lookup per row. */
static PyObject *
compile_hydrate_plan(PyObject *module, PyObject *args)
{
    PyObject *model_type;
    PyObject *spec;
    PyObject *targets;
    WreathPgHydratePlan *plan;
    WreathPgModelLayout *layout;
    Py_ssize_t count;
    Py_ssize_t key_count = 0;
    (void)module;

    if (!PyArg_ParseTuple(args, "OOO:_compile_hydrate_plan", &model_type, &spec,
                          &targets)) {
        return NULL;
    }
    if (!PyType_Check(model_type)) {
        PyErr_SetString(PyExc_TypeError, "expected a model class");
        return NULL;
    }
    layout = wreath_pg_model_layout_for_type((PyTypeObject *)model_type);
    if (layout == NULL) {
        PyErr_SetString(PyExc_TypeError, "model has no compiled storage layout");
        return NULL;
    }
    if (!PyTuple_Check(targets)) {
        PyErr_SetString(PyExc_TypeError, "targets must be a tuple");
        return NULL;
    }
    count = PyTuple_GET_SIZE(targets);

    plan = (WreathPgHydratePlan *)WreathPgHydratePlanType->tp_alloc(WreathPgHydratePlanType, 0);
    if (plan == NULL) return NULL;
    plan->model_type = (PyTypeObject *)Py_NewRef(model_type);
    plan->identity_spec = Py_NewRef(spec);
    plan->column_count = count;
    plan->bitmap_offset = layout->bitmap_offset;
    plan->bitmap_words = layout->bitmap_words;
    plan->columns = PyMem_Calloc((size_t)(count > 0 ? count : 1),
                                 sizeof(WreathPgHydrateColumn));
    plan->key_positions = PyMem_Calloc((size_t)(count > 0 ? count : 1),
                                       sizeof(Py_ssize_t));
    if (plan->columns == NULL || plan->key_positions == NULL) {
        Py_DECREF(plan);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        Py_ssize_t target = PyLong_AsSsize_t(PyTuple_GET_ITEM(targets, i));
        WreathPgModelField *field;
        if (target == -1 && PyErr_Occurred()) {
            Py_DECREF(plan);
            return NULL;
        }
        if (target < 0 || target >= layout->field_count) {
            Py_DECREF(plan);
            PyErr_Format(PyExc_IndexError, "column index %zd is out of range", target);
            return NULL;
        }
        field = &layout->fields[target];
        plan->columns[i].oid = field->oid;
        plan->columns[i].kind = field->kind;
        plan->columns[i].offset = field->offset;
        plan->columns[i].bit = field->bit;
        if (field->flags & WREATH_FIELD_PRIMARY_KEY) {
            plan->key_positions[key_count++] = i;
        }
    }
    plan->key_count = key_count;
    if (key_count == 0) {
        Py_DECREF(plan);
        PyErr_SetString(
            mapping_error == NULL ? PyExc_ValueError : mapping_error,
            "a model result must select its primary key"
        );
        return NULL;
    }
    /* tp_alloc already tracked it; tracking again trips a GC assertion. */
    return (PyObject *)plan;
}

/* -- hydration ------------------------------------------------------------- */

/* Check the result really is the shape the plan was compiled for, before any
   row is decoded. */
static int
validate_result(const WreathPgDecoderPlan *decoder, const WreathPgHydratePlan *plan)
{
    if (decoder->column_count != plan->column_count) {
        PyErr_Format(mapping_error == NULL ? PyExc_ValueError : mapping_error,
                     "result has %zd column(s) but %.200s expects %zd",
                     decoder->column_count, plan->model_type->tp_name,
                     plan->column_count);
        return -1;
    }
    for (Py_ssize_t i = 0; i < plan->column_count; i++) {
        if (decoder->columns[i].oid != plan->columns[i].oid) {
            PyErr_Format(mapping_error == NULL ? PyExc_ValueError : mapping_error,
                         "result column %zd has OID %u, but %.200s declares OID %u",
                         i, decoder->columns[i].oid, plan->model_type->tp_name,
                         plan->columns[i].oid);
            return -1;
        }
    }
    return 0;
}

int
wreath_pg_hydrate_validate(PyObject *decoder_plan, PyObject *hydrate_plan)
{
    if (!PyObject_TypeCheck(decoder_plan, WreathPgDecoderPlanType) ||
        !PyObject_TypeCheck(hydrate_plan, WreathPgHydratePlanType)) {
        PyErr_SetString(PyExc_ValueError, "invalid model hydration plan");
        return -1;
    }
    return validate_result(
        (WreathPgDecoderPlan *)decoder_plan,
        (WreathPgHydratePlan *)hydrate_plan);
}

static const unsigned char *
field_data(WreathPgFieldTape *tape, Py_buffer *buffers, Py_ssize_t row,
           Py_ssize_t column, Py_ssize_t *length)
{
    WreathPgFieldRef *ref = wreath_pg_tape_ref(tape, row, column);
    /* buffers[] covers the live owner window, so a stored index is offset by
     * the tape's owner head (see decode_ref in decode.c). */
    Py_ssize_t slot = (Py_ssize_t)ref->slab_index - tape->owner_head;
    if (ref->length == -1) {
        *length = -1;
        return NULL;
    }
    if (slot < 0 ||
        (uint64_t)ref->offset + (uint64_t)ref->length >
        (uint64_t)buffers[slot].len) {
        PyErr_SetString(PyExc_RuntimeError, "field tape range is invalid");
        *length = -2;
        return NULL;
    }
    *length = ref->length;
    return (const unsigned char *)buffers[slot].buf + ref->offset;
}

/* Build the identity key for one row without allocating the model first. */
static PyObject *
row_identity(WreathPgHydratePlan *plan, WreathPgDecoderPlan *decoder, WreathPgFieldTape *tape,
             Py_buffer *buffers, Py_ssize_t row, int *has_null)
{
    PyObject *key = PyTuple_New(plan->key_count);
    if (key == NULL) return NULL;
    *has_null = 0;
    for (Py_ssize_t i = 0; i < plan->key_count; i++) {
        Py_ssize_t position = plan->key_positions[i];
        WreathPgColumnDecoder *column = &decoder->columns[position];
        Py_ssize_t length;
        const unsigned char *data = field_data(tape, buffers, row, position, &length);
        PyObject *value;
        if (length == -2) {
            Py_DECREF(key);
            return NULL;
        }
        if (length == -1) {
            /* A null key component: this row identifies no object. */
            *has_null = 1;
            Py_DECREF(key);
            return NULL;
        }
        value = column->decoder(data, length, column->format, column->oid);
        if (value == NULL) {
            Py_DECREF(key);
            return NULL;
        }
        PyTuple_SET_ITEM(key, i, value);
    }
    return key;
}

static int
hydrate_row(WreathPgHydratePlan *plan, WreathPgDecoderPlan *decoder, WreathPgFieldTape *tape,
            Py_buffer *buffers, Py_ssize_t row, PyObject *dest, PyObject *identity_map,
            PyObject *owner, PyObject *seen, WreathPgHydrateJournal *journal)
{
    PyObject *key = NULL;
    PyObject *identity = NULL;
    PyObject *instance = NULL;
    PyObject *raised = NULL;
    int has_null = 0;
    int fresh = 0;
    int identity_added = 0;
    int seen_before;
    int seen_added = 0;
    int failed = -1;

    key = row_identity(plan, decoder, tape, buffers, row, &has_null);
    if (key == NULL) {
        /* A null primary key is a row that maps to no object, not an error. */
        return has_null ? 0 : -1;
    }
    identity = PyTuple_Pack(2, plan->identity_spec, key);
    if (identity == NULL) goto done;
    seen_before = PySet_Contains(seen, identity);
    if (seen_before < 0) goto done;

    if (PyDict_GetItemRef(identity_map, identity, &instance) < 0) goto done;
    if (instance == NULL) {
        instance = wreath_pg_model_alloc(plan->model_type);
        if (instance == NULL) goto done;
        fresh = 1;
        plan->allocated++;
    } else {
        plan->reused++;
        if (!seen_before && hydrate_journal_snapshot(journal, instance) < 0)
            goto done;
    }

    for (Py_ssize_t i = 0; i < plan->column_count; i++) {
        WreathPgHydrateColumn *column = &plan->columns[i];
        Py_ssize_t length;
        const unsigned char *data;
        char *cell = (char *)instance + column->offset;
        int stored;

        /* A field the session already changed is its pending write; a row must
           not silently revert it. */
        if (!fresh && bit_get(instance, plan, 2, column->bit)) continue;

        data = field_data(tape, buffers, row, i, &length);
        if (length == -2) goto done;
        if (length == -1) {
            if (column->kind == WREATH_CELL_OBJECT) {
                PyObject **slot = (PyObject **)cell;
                Py_CLEAR(*slot);
            }
            bit_set(instance, plan, BITMAP_NULL, column->bit, 1);
            bit_set(instance, plan, BITMAP_LOADED, column->bit, 1);
            continue;
        }
        stored = store_binary_inline(cell, column, decoder->columns[i].format,
                                     data, length);
        if (stored < 0) goto done;
        if (stored == 0) {
            WreathPgColumnDecoder *source = &decoder->columns[i];
            PyObject *value = source->decoder(data, length, source->format, source->oid);
            if (value == NULL) goto done;
            if (store_boxed(cell, column, value) < 0) {
                Py_DECREF(value);
                goto done;
            }
            Py_DECREF(value);
        }
        /* Bits are set only once the cell really holds the value. */
        bit_set(instance, plan, BITMAP_NULL, column->bit, 0);
        bit_set(instance, plan, BITMAP_LOADED, column->bit, 1);
    }

    if (fresh) {
        WreathPgModel *model = (WreathPgModel *)instance;
        model->state_flags = 1; /* PERSISTENT */
        Py_XSETREF(model->identity_owner, Py_NewRef(owner));
        if (PyDict_SetItem(identity_map, identity, instance) < 0) goto done;
        identity_added = 1;
    }
    if (!seen_before) {
        if (PySet_Add(seen, identity) < 0) goto done;
        seen_added = 1;
        if (PyList_Append(dest, instance) < 0) goto done;
        if (fresh && PyList_Append(journal->created, identity) < 0) goto done;
    }
    failed = 0;

done:
    if (failed) raised = PyErr_GetRaisedException();
    if (failed && identity_added && PyDict_DelItem(identity_map, identity) < 0)
        PyErr_Clear();
    if (failed && fresh && instance != NULL) {
        /* Leave nothing partially visible: the provisional object never entered
           the identity map, and dropping the last reference releases whatever
           cells were filled before the failure. */
        Py_CLEAR(instance);
    }
    if (failed && seen_added && PySet_Discard(seen, identity) < 0)
        PyErr_Clear();
    if (failed) PyErr_SetRaisedException(raised);
    Py_XDECREF(instance);
    Py_XDECREF(identity);
    Py_XDECREF(key);
    return failed;
}

typedef struct {
    const unsigned char *data;
    Py_ssize_t length;
} WreathPgRawField;

static uint16_t
raw_u16(const unsigned char *data)
{
    return (uint16_t)((uint16_t)data[0] << 8 | data[1]);
}

static uint32_t
raw_u32(const unsigned char *data)
{
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
           ((uint32_t)data[2] << 8) | data[3];
}

static PyObject *
raw_row_identity(WreathPgHydratePlan *plan, WreathPgDecoderPlan *decoder,
                 WreathPgRawField *fields, int *has_null)
{
    PyObject *key = PyTuple_New(plan->key_count);
    if (key == NULL) return NULL;
    *has_null = 0;
    for (Py_ssize_t i = 0; i < plan->key_count; i++) {
        Py_ssize_t position = plan->key_positions[i];
        WreathPgColumnDecoder *column = &decoder->columns[position];
        WreathPgRawField *field = &fields[position];
        PyObject *value;
        if (field->length == -1) {
            *has_null = 1;
            Py_DECREF(key);
            return NULL;
        }
        value = column->decoder(
            field->data, field->length, column->format, column->oid);
        if (value == NULL) {
            Py_DECREF(key);
            return NULL;
        }
        PyTuple_SET_ITEM(key, i, value);
    }
    return key;
}

static int
hydrate_raw_row(WreathPgHydratePlan *plan, WreathPgDecoderPlan *decoder,
                WreathPgRawField *fields, PyObject *dest,
                PyObject *identity_map, PyObject *owner, PyObject *seen)
{
    PyObject *key = NULL;
    PyObject *identity = NULL;
    PyObject *instance = NULL;
    PyObject *raised = NULL;
    int has_null = 0;
    int fresh = 0;
    int identity_added = 0;
    int seen_before;
    int seen_added = 0;
    int failed = -1;

    key = raw_row_identity(plan, decoder, fields, &has_null);
    if (key == NULL) return has_null ? 0 : -1;
    identity = PyTuple_Pack(2, plan->identity_spec, key);
    if (identity == NULL) goto done;
    seen_before = PySet_Contains(seen, identity);
    if (seen_before < 0) goto done;
    if (PyDict_GetItemRef(identity_map, identity, &instance) < 0) goto done;
    if (instance == NULL) {
        instance = wreath_pg_model_alloc(plan->model_type);
        if (instance == NULL) goto done;
        fresh = 1;
        plan->allocated++;
    } else {
        plan->reused++;
    }

    for (Py_ssize_t i = 0; i < plan->column_count; i++) {
        WreathPgHydrateColumn *column = &plan->columns[i];
        WreathPgRawField *field = &fields[i];
        char *cell = (char *)instance + column->offset;
        int stored;
        if (!fresh && bit_get(instance, plan, 2, column->bit)) continue;
        if (field->length == -1) {
            if (column->kind == WREATH_CELL_OBJECT) {
                PyObject **slot = (PyObject **)cell;
                Py_CLEAR(*slot);
            }
            bit_set(instance, plan, BITMAP_NULL, column->bit, 1);
            bit_set(instance, plan, BITMAP_LOADED, column->bit, 1);
            continue;
        }
        stored = store_binary_inline(
            cell, column, decoder->columns[i].format,
            field->data, field->length);
        if (stored < 0) goto done;
        if (stored == 0) {
            WreathPgColumnDecoder *source = &decoder->columns[i];
            PyObject *value = source->decoder(
                field->data, field->length, source->format, source->oid);
            if (value == NULL) goto done;
            if (store_boxed(cell, column, value) < 0) {
                Py_DECREF(value);
                goto done;
            }
            Py_DECREF(value);
        }
        bit_set(instance, plan, BITMAP_NULL, column->bit, 0);
        bit_set(instance, plan, BITMAP_LOADED, column->bit, 1);
    }

    if (fresh) {
        WreathPgModel *model = (WreathPgModel *)instance;
        model->state_flags = 1;
        Py_XSETREF(model->identity_owner, Py_NewRef(owner));
        if (PyDict_SetItem(identity_map, identity, instance) < 0) goto done;
        identity_added = 1;
    }
    if (!seen_before) {
        if (PySet_Add(seen, identity) < 0) goto done;
        seen_added = 1;
        if (PyList_Append(dest, instance) < 0) goto done;
    }
    failed = 0;

done:
    if (failed) raised = PyErr_GetRaisedException();
    if (failed && identity_added && PyDict_DelItem(identity_map, identity) < 0)
        PyErr_Clear();
    if (failed && fresh && instance != NULL) Py_CLEAR(instance);
    if (failed && seen_added && PySet_Discard(seen, identity) < 0)
        PyErr_Clear();
    if (failed) PyErr_SetRaisedException(raised);
    Py_XDECREF(instance);
    Py_XDECREF(identity);
    Py_XDECREF(key);
    return failed;
}

int
wreath_pg_hydrate_datarow(PyObject *decoder_plan, PyObject *hydrate_plan,
                          PyObject *dest, PyObject *identity_map,
                          PyObject *owner, PyObject *seen,
                          const unsigned char *data, Py_ssize_t length)
{
    WreathPgDecoderPlan *decoder = (WreathPgDecoderPlan *)decoder_plan;
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)hydrate_plan;
    WreathPgRawField stack_fields[32];
    WreathPgRawField *fields = stack_fields;
    Py_ssize_t offset = 2;
    int result;

    if (!PyObject_TypeCheck(decoder_plan, WreathPgDecoderPlanType) ||
        !PyObject_TypeCheck(hydrate_plan, WreathPgHydratePlanType) ||
        !PyList_Check(dest) || !PyDict_Check(identity_map) ||
        !PySet_Check(seen) || data == NULL || length < 2) {
        PyErr_SetString(PyExc_ValueError, "invalid direct model hydration request");
        return -1;
    }
    if ((Py_ssize_t)raw_u16(data) != decoder->column_count) {
        PyErr_SetString(PyExc_ValueError, "DataRow column count does not match plan");
        return -1;
    }
    if (decoder->column_count > 32) {
        fields = PyMem_Malloc(
            (size_t)decoder->column_count * sizeof(WreathPgRawField));
        if (fields == NULL) {
            PyErr_NoMemory();
            return -1;
        }
    }
    for (Py_ssize_t column = 0; column < decoder->column_count; column++) {
        int32_t field_length;
        if (offset > length - 4) {
            PyErr_SetString(PyExc_ValueError, "truncated DataRow field length");
            goto error;
        }
        field_length = (int32_t)raw_u32(data + offset);
        offset += 4;
        if (field_length < -1 ||
            (field_length >= 0 && offset > length - field_length)) {
            PyErr_SetString(PyExc_ValueError, "invalid DataRow field length");
            goto error;
        }
        fields[column].length = field_length;
        fields[column].data = field_length < 0 ? NULL : data + offset;
        if (field_length >= 0) offset += field_length;
    }
    if (offset != length) {
        PyErr_SetString(PyExc_ValueError, "invalid DataRow length");
        goto error;
    }
    result = hydrate_raw_row(
        plan, decoder, fields, dest, identity_map, owner, seen);
    if (fields != stack_fields) PyMem_Free(fields);
    return result;

error:
    if (fields != stack_fields) PyMem_Free(fields);
    return -1;
}

int
wreath_pg_hydrate_models(PyObject *decoder_plan, PyObject *tape_object,
                      PyObject *hydrate_plan, Py_ssize_t limit, PyObject *dest,
                      PyObject *identity_map, PyObject *owner)
{
    WreathPgDecoderPlan *decoder = (WreathPgDecoderPlan *)decoder_plan;
    WreathPgFieldTape *tape = (WreathPgFieldTape *)tape_object;
    WreathPgHydratePlan *plan = (WreathPgHydratePlan *)hydrate_plan;
    Py_ssize_t rows;
    Py_ssize_t owner_limit = 0;
    Py_buffer *buffers;
    Py_ssize_t start;
    PyObject *seen = NULL;
    WreathPgHydrateJournal journal;
    int result = 0;

    if (!PyObject_TypeCheck(decoder_plan, WreathPgDecoderPlanType) ||
        !PyObject_TypeCheck(tape_object, WreathPgFieldTapeType) ||
        !PyObject_TypeCheck(hydrate_plan, WreathPgHydratePlanType) ||
        !PyList_Check(dest) || !PyDict_Check(identity_map) || limit <= 0) {
        PyErr_SetString(PyExc_ValueError, "invalid model hydration request");
        return -1;
    }
    if (validate_result(decoder, plan) < 0) return -1;

    rows = tape->row_count < limit ? tape->row_count : limit;
    if (rows == 0) return 0;
    seen = PySet_New(NULL);
    if (seen == NULL) return -1;
    if (hydrate_journal_init(&journal) < 0) {
        Py_DECREF(seen);
        return -1;
    }
    buffers = wreath_pg_acquire_owner_buffers(tape, rows, &owner_limit);
    if (buffers == NULL) {
        hydrate_journal_clear(&journal);
        Py_DECREF(seen);
        return -1;
    }

    start = PyList_GET_SIZE(dest);
    for (Py_ssize_t row = 0; row < rows; row++) {
        if (row > 0 && row % hydrate_batch_budget == 0 && PyErr_CheckSignals() < 0) {
            result = -1;
            break;
        }
        if (hydrate_row(plan, decoder, tape, buffers, row, dest, identity_map, owner,
                        seen, &journal) < 0) {
            result = -1;
            break;
        }
    }
    if (result == 0 && wreath_pg_tape_consume(tape, rows) < 0) result = -1;
    if (result < 0) {
        PyList_SetSlice(dest, start, PyList_GET_SIZE(dest), NULL);
        hydrate_journal_rollback(&journal, identity_map);
    }
    wreath_pg_release_owner_buffers(buffers, owner_limit);
    hydrate_journal_clear(&journal);
    Py_DECREF(seen);
    return result;
}

/* _decode_models(decoder_plan, tape, destination, limit, rows)

   `destination` is the opaque object the driver was handed: the ORM packs its
   hydrate plan, identity map, and owning session into it, so the reference
   driver never learns what a model is. */
static PyObject *
decode_models(PyObject *module, PyObject *args)
{
    PyObject *decoder_plan;
    PyObject *tape;
    PyObject *destination;
    PyObject *rows;
    PyObject *plan;
    PyObject *identity_map;
    PyObject *owner;
    Py_ssize_t limit;
    (void)module;

    if (!PyArg_ParseTuple(args, "OOOnO:_decode_models", &decoder_plan, &tape,
                          &destination, &limit, &rows)) {
        return NULL;
    }
    if (wreath_pg_migration_catalog_check(destination)) {
        if (wreath_pg_migration_catalog_decode(
                decoder_plan, tape, destination, limit) < 0) return NULL;
        Py_RETURN_NONE;
    }
    if (!PyTuple_Check(destination) || PyTuple_GET_SIZE(destination) != 3) {
        PyErr_SetString(
            PyExc_TypeError,
            "a decode destination is a migration catalog or "
            "(hydrate_plan, identity_map, session)");
        return NULL;
    }
    plan = PyTuple_GET_ITEM(destination, 0);
    identity_map = PyTuple_GET_ITEM(destination, 1);
    owner = PyTuple_GET_ITEM(destination, 2);
    if (wreath_pg_hydrate_models(decoder_plan, tape, plan, limit, rows, identity_map,
                              owner) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
configure_hydrate_errors(PyObject *module, PyObject *arg)
{
    (void)module;
    Py_XSETREF(mapping_error, Py_NewRef(arg));
    Py_RETURN_NONE;
}

static PyMethodDef hydrate_methods[] = {
    {"_compile_hydrate_plan", compile_hydrate_plan, METH_VARARGS, NULL},
    {"_decode_models", decode_models, METH_VARARGS, NULL},
    {"_configure_hydrate_errors", configure_hydrate_errors, METH_O, NULL},
    {NULL, NULL, 0, NULL}
};

int
wreath_pg_hydrate_init(PyObject *module)
{
    WreathPgHydratePlanType = (PyTypeObject *)PyType_FromSpec(&hydrate_plan_spec);
    if (WreathPgHydratePlanType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "_HydratePlan",
                              (PyObject *)WreathPgHydratePlanType) < 0) {
        return -1;
    }
    Py_DECREF(WreathPgHydratePlanType);
    return PyModule_AddFunctions(module, hydrate_methods);
}

void
wreath_pg_hydrate_fini(void)
{
    Py_CLEAR(mapping_error);
}
