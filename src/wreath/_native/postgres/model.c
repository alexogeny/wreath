/* Fixed-size native storage for wreath.orm models.

   Each compiled model gets one generated heap type whose tp_basicsize is fixed
   for that model: scalar columns live in unboxed inline cells, so hydrating an
   int8 column writes eight bytes instead of allocating a PyLong. Variable-width
   payloads (text, bytea, JSON) are separately allocated PyObject * cells, so
   "fixed size" describes the model struct and explicitly not the total retained
   memory.

   Validation on assignment deliberately calls the column's Python validator
   (Column.validate: the type's coercion with any business rules fused into it)
   rather than reimplementing the checks here. That keeps one implementation of
   the rules shared with the reference backend, and costs a Python call only
   on assignment -- never on hydration, which writes cells directly. */

#include "model.h"

#include "codec.h"

#include <stdint.h>
#include <string.h>

#define PG_BOOL 16
#define PG_BYTEA 17
#define PG_INT8 20
#define PG_INT2 21
#define PG_INT4 23
#define PG_TEXT 25
#define PG_JSON 114
#define PG_FLOAT4 700
#define PG_FLOAT8 701
#define PG_VARCHAR 1043
#define PG_DATE 1082
#define PG_TIMESTAMP 1114
#define PG_TIMESTAMPTZ 1184
#define PG_UUID 2950
#define PG_JSONB 3802

/* Object states, mirroring wreath.orm.model. */
#define STATE_TRANSIENT 0
#define STATE_PERSISTENT 1
#define STATE_DELETED 2
#define STATE_DETACHED 3

PyTypeObject *WreathPgModelTypeType = NULL;
static PyTypeObject *WreathPgColumnDescriptorType = NULL;
static PyTypeObject *WreathPgRelationDescriptorType = NULL;

/* wreath.orm imports wreath.postgres, so importing the ORM's exceptions during module
   init would be circular. Python injects them once instead. */
static PyObject *error_unloaded_attribute = NULL;
static PyObject *error_unloaded_relationship = NULL;
static PyObject *error_declaration = NULL;

/* -- cell kinds ------------------------------------------------------------ */

static uint16_t
cell_kind_for_oid(uint32_t oid)
{
    switch (oid) {
    case PG_BOOL: return WREATH_CELL_BOOL;
    case PG_INT2: return WREATH_CELL_INT2;
    case PG_INT4: return WREATH_CELL_INT4;
    case PG_INT8: return WREATH_CELL_INT8;
    case PG_FLOAT4: return WREATH_CELL_FLOAT4;
    case PG_FLOAT8: return WREATH_CELL_FLOAT8;
    case PG_DATE: return WREATH_CELL_DATE;
    case PG_TIMESTAMP: return WREATH_CELL_TIMESTAMP;
    case PG_TIMESTAMPTZ: return WREATH_CELL_TIMESTAMPTZ;
    case PG_UUID: return WREATH_CELL_UUID;
    default:
        /* text, varchar, bytea, json, jsonb, and anything a later codec adds */
        return WREATH_CELL_OBJECT;
    }
}

static Py_ssize_t
cell_size(uint16_t kind)
{
    switch (kind) {
    case WREATH_CELL_BOOL: return 1;
    case WREATH_CELL_INT2: return 2;
    case WREATH_CELL_INT4: return 4;
    case WREATH_CELL_FLOAT4: return 4;
    case WREATH_CELL_DATE: return 4;
    case WREATH_CELL_INT8: return 8;
    case WREATH_CELL_FLOAT8: return 8;
    case WREATH_CELL_TIMESTAMP: return 8;
    case WREATH_CELL_TIMESTAMPTZ: return 8;
    case WREATH_CELL_UUID: return 16;
    default: return (Py_ssize_t)sizeof(PyObject *);
    }
}

static Py_ssize_t
cell_align(uint16_t kind)
{
    switch (kind) {
    case WREATH_CELL_BOOL: return 1;
    case WREATH_CELL_UUID: return 1; /* an opaque 16-byte blob */
    case WREATH_CELL_INT2: return 2;
    case WREATH_CELL_INT4: return 4;
    case WREATH_CELL_FLOAT4: return 4;
    case WREATH_CELL_DATE: return 4;
    default: return 8;
    }
}

static Py_ssize_t
align_up(Py_ssize_t value, Py_ssize_t alignment)
{
    return (value + alignment - 1) / alignment * alignment;
}

/* -- bitmaps --------------------------------------------------------------- */

static uint64_t *
bitmap_words(PyObject *instance, const WreathPgModelLayout *layout, int which)
{
    char *base = (char *)instance + layout->bitmap_offset;
    return (uint64_t *)(base + (Py_ssize_t)which * layout->bitmap_words * 8);
}

static int
bit_get(PyObject *instance, const WreathPgModelLayout *layout, int which, Py_ssize_t bit)
{
    uint64_t *words = bitmap_words(instance, layout, which);
    return (words[bit >> 6] >> (bit & 63)) & 1u;
}

static void
bit_set(PyObject *instance, const WreathPgModelLayout *layout, int which, Py_ssize_t bit,
        int value)
{
    uint64_t *words = bitmap_words(instance, layout, which);
    uint64_t mask = (uint64_t)1 << (bit & 63);
    if (value) {
        words[bit >> 6] |= mask;
    } else {
        words[bit >> 6] &= ~mask;
    }
}

#define BITMAP_LOADED 0
#define BITMAP_NULL 1
#define BITMAP_DIRTY 2

/* -- layout lookup --------------------------------------------------------- */

WreathPgModelLayout *
wreath_pg_model_layout_for_type(PyTypeObject *type)
{
    PyTypeObject *base;
    if (Py_IS_TYPE(type, WreathPgModelTypeType)) {
        return PyObject_GetTypeData((PyObject *)type, WreathPgModelTypeType);
    }
    /* A model class is created by Python's ModelMeta, so its own metatype is
       ModelMeta rather than the metatype here. Its solid base is the storage
       type generated by _compile_model_layout, which is what carries the
       layout; nothing is stored per instance. */
    base = type->tp_base;
    if (base != NULL && Py_IS_TYPE(base, WreathPgModelTypeType)) {
        return PyObject_GetTypeData((PyObject *)base, WreathPgModelTypeType);
    }
    return NULL;
}

WreathPgModelLayout *
wreath_pg_model_layout_of(PyObject *instance)
{
    return wreath_pg_model_layout_for_type(Py_TYPE(instance));
}

PyObject *
wreath_pg_model_alloc(PyTypeObject *type)
{
    /* tp_alloc zero-fills, which is exactly the empty state. */
    return type->tp_alloc(type, 0);
}

static WreathPgModelLayout *
require_layout(PyObject *instance)
{
    WreathPgModelLayout *layout = wreath_pg_model_layout_of(instance);
    if (layout == NULL) {
        PyErr_Format(PyExc_TypeError,
                     "%.200s is not a compiled model storage type",
                     Py_TYPE(instance)->tp_name);
    }
    return layout;
}

/* -- cell load and store --------------------------------------------------- */

static PyObject *
cell_load(const char *cell, const WreathPgModelField *field)
{
    switch (field->kind) {
    case WREATH_CELL_BOOL:
        return PyBool_FromLong(*(const uint8_t *)cell);
    case WREATH_CELL_INT2: {
        int16_t value;
        memcpy(&value, cell, sizeof(value));
        return PyLong_FromLong(value);
    }
    case WREATH_CELL_INT4: {
        int32_t value;
        memcpy(&value, cell, sizeof(value));
        return PyLong_FromLong(value);
    }
    case WREATH_CELL_INT8: {
        int64_t value;
        memcpy(&value, cell, sizeof(value));
        return PyLong_FromLongLong(value);
    }
    case WREATH_CELL_FLOAT4: {
        float value;
        memcpy(&value, cell, sizeof(value));
        return PyFloat_FromDouble((double)value);
    }
    case WREATH_CELL_FLOAT8: {
        double value;
        memcpy(&value, cell, sizeof(value));
        return PyFloat_FromDouble(value);
    }
    case WREATH_CELL_DATE: {
        int32_t value;
        memcpy(&value, cell, sizeof(value));
        return wreath_pg_date_from_days(value);
    }
    case WREATH_CELL_TIMESTAMP:
    case WREATH_CELL_TIMESTAMPTZ: {
        int64_t value;
        memcpy(&value, cell, sizeof(value));
        return wreath_pg_timestamp_from_micros(
            value, field->kind == WREATH_CELL_TIMESTAMPTZ
        );
    }
    case WREATH_CELL_UUID:
        return wreath_pg_uuid_from_bytes((const unsigned char *)cell);
    default: {
        PyObject *value = *(PyObject *const *)cell;
        return Py_NewRef(value == NULL ? Py_None : value);
    }
    }
}

/* Write an already-coerced value into a cell. `changed` reports whether the
   stored value differs from what was there, which is only meaningful when the
   cell already held a loaded, non-null value. */
static int
cell_store(char *cell, const WreathPgModelField *field, PyObject *value, int *changed)
{
    switch (field->kind) {
    case WREATH_CELL_BOOL: {
        uint8_t stored = (uint8_t)(value == Py_True);
        if (!PyBool_Check(value)) goto coercion_bug;
        *changed = *(const uint8_t *)cell != stored;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_INT2:
    case WREATH_CELL_INT4:
    case WREATH_CELL_INT8: {
        long long number = PyLong_AsLongLong(value);
        if (number == -1 && PyErr_Occurred()) return -1;
        if (field->kind == WREATH_CELL_INT2) {
            int16_t stored = (int16_t)number, previous;
            memcpy(&previous, cell, sizeof(previous));
            *changed = previous != stored;
            memcpy(cell, &stored, sizeof(stored));
        } else if (field->kind == WREATH_CELL_INT4) {
            int32_t stored = (int32_t)number, previous;
            memcpy(&previous, cell, sizeof(previous));
            *changed = previous != stored;
            memcpy(cell, &stored, sizeof(stored));
        } else {
            int64_t stored = (int64_t)number, previous;
            memcpy(&previous, cell, sizeof(previous));
            *changed = previous != stored;
            memcpy(cell, &stored, sizeof(stored));
        }
        return 0;
    }
    case WREATH_CELL_FLOAT4:
    case WREATH_CELL_FLOAT8: {
        double number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) return -1;
        if (field->kind == WREATH_CELL_FLOAT4) {
            float stored = (float)number, previous;
            memcpy(&previous, cell, sizeof(previous));
            /* Compare as numbers, not bytes, so NaN counts as a change and
               -0.0 does not, exactly as Python's == would decide. */
            *changed = !(previous == stored);
            memcpy(cell, &stored, sizeof(stored));
        } else {
            double previous;
            memcpy(&previous, cell, sizeof(previous));
            *changed = !(previous == number);
            memcpy(cell, &number, sizeof(number));
        }
        return 0;
    }
    case WREATH_CELL_DATE: {
        int64_t days;
        int32_t stored, previous;
        if (wreath_pg_date_days(value, &days) < 0) return -1;
        stored = (int32_t)days;
        memcpy(&previous, cell, sizeof(previous));
        *changed = previous != stored;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_TIMESTAMP:
    case WREATH_CELL_TIMESTAMPTZ: {
        int aware = field->kind == WREATH_CELL_TIMESTAMPTZ;
        int64_t stored, previous;
        if (wreath_pg_check_timestamp(value, aware) < 0) return -1;
        if (wreath_pg_timestamp_micros(value, aware, &stored) < 0) return -1;
        memcpy(&previous, cell, sizeof(previous));
        *changed = previous != stored;
        memcpy(cell, &stored, sizeof(stored));
        return 0;
    }
    case WREATH_CELL_UUID: {
        unsigned char stored[16];
        if (wreath_pg_uuid_bytes(value, stored) < 0) return -1;
        *changed = memcmp(cell, stored, sizeof(stored)) != 0;
        memcpy(cell, stored, sizeof(stored));
        return 0;
    }
    default: {
        PyObject **slot = (PyObject **)cell;
        PyObject *previous = *slot;
        *changed = 1;
        if (previous != NULL) {
            int equal = previous == value ? 1 : PyObject_RichCompareBool(previous, value, Py_EQ);
            if (equal < 0) {
                /* A comparison that raises is not evidence of equality; the
                   reference backend treats it as a change too. */
                PyErr_Clear();
                equal = 0;
            }
            *changed = !equal;
        }
        *slot = Py_NewRef(value);
        Py_XDECREF(previous);
        return 0;
    }
    }

coercion_bug:
    PyErr_Format(PyExc_TypeError, "column coercion produced %.200s for an inline cell",
                 Py_TYPE(value)->tp_name);
    return -1;
}

static void
cell_release(char *cell, const WreathPgModelField *field)
{
    if (field->kind == WREATH_CELL_OBJECT) {
        PyObject **slot = (PyObject **)cell;
        Py_CLEAR(*slot);
    }
}

/* -- descriptors ----------------------------------------------------------- */

typedef struct {
    PyObject_HEAD
    PyObject *column;     /* wreath.orm.fields.Column, for metadata and errors */
    PyObject *expression; /* its ColumnExpr, returned on class access */
    PyObject *validate;   /* Column.validate: the type's coercion with the
                             column's business rules fused into it. It is
                             PgType.coerce itself when the column has no rules,
                             so a plain column costs exactly what it did. */
    PyObject *name;       /* python_name */
    WreathPgModelField field;
    Py_ssize_t bitmap_offset;
    Py_ssize_t bitmap_words;
} WreathPgColumnDescriptor;

typedef struct {
    PyObject_HEAD
    PyObject *relationship;
    PyObject *expression;
    PyObject *name;
    Py_ssize_t offset;
} WreathPgRelationDescriptor;

static void
descriptor_layout(const WreathPgColumnDescriptor *self, WreathPgModelLayout *out)
{
    /* Enough of a layout for the bitmap helpers, without a type lookup. */
    out->bitmap_offset = self->bitmap_offset;
    out->bitmap_words = self->bitmap_words;
}

static PyObject *
raise_unloaded_attribute(PyObject *instance, PyObject *name)
{
    PyErr_Format(
        error_unloaded_attribute == NULL ? PyExc_AttributeError : error_unloaded_attribute,
        "%.200s.%U was not loaded; select it or reload the object",
        Py_TYPE(instance)->tp_name, name
    );
    return NULL;
}

static PyObject *
column_load(PyObject *instance, const WreathPgColumnDescriptor *descriptor)
{
    WreathPgModelLayout view;
    descriptor_layout(descriptor, &view);
    if (!bit_get(instance, &view, BITMAP_LOADED, descriptor->field.bit)) {
        return raise_unloaded_attribute(instance, descriptor->name);
    }
    if (bit_get(instance, &view, BITMAP_NULL, descriptor->field.bit)) {
        Py_RETURN_NONE;
    }
    return cell_load((const char *)instance + descriptor->field.offset, &descriptor->field);
}

static int
column_store(PyObject *instance, const WreathPgColumnDescriptor *descriptor, PyObject *value)
{
    WreathPgModelLayout view;
    WreathPgModel *model = (WreathPgModel *)instance;
    Py_ssize_t bit = descriptor->field.bit;
    char *cell = (char *)instance + descriptor->field.offset;
    int changed;
    int loaded;

    descriptor_layout(descriptor, &view);
    if ((descriptor->field.flags & WREATH_FIELD_PRIMARY_KEY) &&
        model->state_flags == STATE_PERSISTENT) {
        PyErr_Format(
            error_declaration == NULL ? PyExc_AttributeError : error_declaration,
            "cannot change primary key %.200s.%U on a persistent object",
            Py_TYPE(instance)->tp_name, descriptor->name
        );
        return -1;
    }
    loaded = bit_get(instance, &view, BITMAP_LOADED, bit);
    if (value == Py_None) {
        if (!(descriptor->field.flags & WREATH_FIELD_NULLABLE)) {
            PyErr_Format(PyExc_ValueError, "%.200s.%U is not nullable",
                         Py_TYPE(instance)->tp_name, descriptor->name);
            return -1;
        }
        changed = !loaded || !bit_get(instance, &view, BITMAP_NULL, bit);
        cell_release(cell, &descriptor->field);
        bit_set(instance, &view, BITMAP_NULL, bit, 1);
    } else {
        PyObject *coerced = PyObject_CallOneArg(descriptor->validate, value);
        int was_null;
        if (coerced == NULL) return -1;
        was_null = bit_get(instance, &view, BITMAP_NULL, bit);
        if (cell_store(cell, &descriptor->field, coerced, &changed) < 0) {
            Py_DECREF(coerced);
            return -1;
        }
        Py_DECREF(coerced);
        if (!loaded || was_null) changed = 1;
        bit_set(instance, &view, BITMAP_NULL, bit, 0);
    }
    bit_set(instance, &view, BITMAP_LOADED, bit, 1);
    if (changed && (model->state_flags == STATE_PERSISTENT ||
                    model->state_flags == STATE_DELETED)) {
        bit_set(instance, &view, BITMAP_DIRTY, bit, 1);
    }
    return 0;
}

static PyObject *
column_descr_get(PyObject *self, PyObject *obj, PyObject *type)
{
    WreathPgColumnDescriptor *descriptor = (WreathPgColumnDescriptor *)self;
    (void)type;
    if (obj == NULL || obj == Py_None) {
        return Py_NewRef(descriptor->expression);
    }
    return column_load(obj, descriptor);
}

static int
column_descr_set(PyObject *self, PyObject *obj, PyObject *value)
{
    WreathPgColumnDescriptor *descriptor = (WreathPgColumnDescriptor *)self;
    if (value == NULL) {
        PyErr_Format(PyExc_AttributeError, "cannot delete mapped column %U",
                     descriptor->name);
        return -1;
    }
    return column_store(obj, descriptor, value);
}

static int
column_descr_traverse(PyObject *self, visitproc visit, void *arg)
{
    WreathPgColumnDescriptor *descriptor = (WreathPgColumnDescriptor *)self;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(descriptor->column);
    Py_VISIT(descriptor->expression);
    Py_VISIT(descriptor->validate);
    return 0;
}

static int
column_descr_clear(PyObject *self)
{
    WreathPgColumnDescriptor *descriptor = (WreathPgColumnDescriptor *)self;
    Py_CLEAR(descriptor->column);
    Py_CLEAR(descriptor->expression);
    Py_CLEAR(descriptor->validate);
    Py_CLEAR(descriptor->name);
    return 0;
}

static void
column_descr_dealloc(PyObject *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    column_descr_clear(self);
    type->tp_free(self);
    Py_DECREF(type);
}

static PyObject *
column_descr_repr(PyObject *self)
{
    WreathPgColumnDescriptor *descriptor = (WreathPgColumnDescriptor *)self;
    return PyUnicode_FromFormat("<native column %U>", descriptor->name);
}

static PyObject *
column_descr_column(PyObject *self, void *closure)
{
    (void)closure;
    return Py_NewRef(((WreathPgColumnDescriptor *)self)->column);
}

static PyGetSetDef column_descr_getset[] = {
    {"column", column_descr_column, NULL, "the declared Column", NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

static PyType_Slot column_descr_slots[] = {
    {Py_tp_descr_get, column_descr_get},
    {Py_tp_descr_set, column_descr_set},
    {Py_tp_traverse, column_descr_traverse},
    {Py_tp_clear, column_descr_clear},
    {Py_tp_dealloc, column_descr_dealloc},
    {Py_tp_repr, column_descr_repr},
    {Py_tp_getset, column_descr_getset},
    {0, NULL}
};

static PyType_Spec column_descr_spec = {
    .name = "wreath._native._postgres._ColumnDescriptor",
    .basicsize = sizeof(WreathPgColumnDescriptor),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = column_descr_slots,
};

static PyObject *
relation_descr_get(PyObject *self, PyObject *obj, PyObject *type)
{
    WreathPgRelationDescriptor *descriptor = (WreathPgRelationDescriptor *)self;
    PyObject *value;
    (void)type;
    if (obj == NULL || obj == Py_None) {
        return Py_NewRef(descriptor->expression);
    }
    value = *(PyObject **)((char *)obj + descriptor->offset);
    if (value == NULL) {
        PyErr_Format(
            error_unloaded_relationship == NULL ? PyExc_AttributeError
                                                : error_unloaded_relationship,
            "%.200s.%U was not loaded; include it in the query or call "
            "await session.load(obj, %.200s.%U)",
            Py_TYPE(obj)->tp_name, descriptor->name, Py_TYPE(obj)->tp_name,
            descriptor->name
        );
        return NULL;
    }
    return Py_NewRef(value);
}

static int
relation_descr_set(PyObject *self, PyObject *obj, PyObject *value)
{
    WreathPgRelationDescriptor *descriptor = (WreathPgRelationDescriptor *)self;
    PyObject **slot = (PyObject **)((char *)obj + descriptor->offset);
    if (value == NULL) {
        PyErr_Format(PyExc_AttributeError, "cannot delete relationship %U",
                     descriptor->name);
        return -1;
    }
    Py_XSETREF(*slot, Py_NewRef(value));
    return 0;
}

static int
relation_descr_traverse(PyObject *self, visitproc visit, void *arg)
{
    WreathPgRelationDescriptor *descriptor = (WreathPgRelationDescriptor *)self;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(descriptor->relationship);
    Py_VISIT(descriptor->expression);
    return 0;
}

static int
relation_descr_clear(PyObject *self)
{
    WreathPgRelationDescriptor *descriptor = (WreathPgRelationDescriptor *)self;
    Py_CLEAR(descriptor->relationship);
    Py_CLEAR(descriptor->expression);
    Py_CLEAR(descriptor->name);
    return 0;
}

static void
relation_descr_dealloc(PyObject *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    relation_descr_clear(self);
    type->tp_free(self);
    Py_DECREF(type);
}

static PyType_Slot relation_descr_slots[] = {
    {Py_tp_descr_get, relation_descr_get},
    {Py_tp_descr_set, relation_descr_set},
    {Py_tp_traverse, relation_descr_traverse},
    {Py_tp_clear, relation_descr_clear},
    {Py_tp_dealloc, relation_descr_dealloc},
    {0, NULL}
};

static PyType_Spec relation_descr_spec = {
    .name = "wreath._native._postgres._RelationDescriptor",
    .basicsize = sizeof(WreathPgRelationDescriptor),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = relation_descr_slots,
};

/* -- the generated storage type -------------------------------------------- */

static int
storage_traverse(PyObject *self, visitproc visit, void *arg)
{
    WreathPgModelLayout *layout = wreath_pg_model_layout_of(self);
    WreathPgModel *model = (WreathPgModel *)self;
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(model->identity_owner);
    if (layout == NULL) return 0;
    for (Py_ssize_t i = 0; i < layout->pointer_count; i++) {
        PyObject *value = *(PyObject **)((char *)self + layout->pointer_offsets[i]);
        Py_VISIT(value);
    }
    return 0;
}

static int
storage_clear(PyObject *self)
{
    WreathPgModelLayout *layout = wreath_pg_model_layout_of(self);
    WreathPgModel *model = (WreathPgModel *)self;
    Py_CLEAR(model->identity_owner);
    if (layout == NULL) return 0;
    for (Py_ssize_t i = 0; i < layout->pointer_count; i++) {
        PyObject **slot = (PyObject **)((char *)self + layout->pointer_offsets[i]);
        Py_CLEAR(*slot);
    }
    return 0;
}

static void
storage_dealloc(PyObject *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    /* Weakrefs must be cleared before the cells they may resurrect. */
    if (type->tp_weaklistoffset) PyObject_ClearWeakRefs(self);
    storage_clear(self);
    type->tp_free(self);
    /* The instance held a reference to its heap type. */
    Py_DECREF(type);
}

static PyObject *
storage_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    (void)args;
    (void)kwargs;
    /* tp_alloc zero-fills, which is exactly the empty state: no bits set, no
       cells, TRANSIENT, and no owner. */
    return type->tp_alloc(type, 0);
}

/* -- storage protocol ------------------------------------------------------ */

static const WreathPgModelField *
field_at(PyObject *self, WreathPgModelLayout **layout_out, Py_ssize_t index)
{
    WreathPgModelLayout *layout = require_layout(self);
    if (layout == NULL) return NULL;
    if (index < 0 || index >= layout->field_count) {
        PyErr_Format(PyExc_IndexError, "column index %zd is out of range", index);
        return NULL;
    }
    *layout_out = layout;
    return &layout->fields[index];
}

static PyObject *
column_name_at(PyObject *self, Py_ssize_t index)
{
    /* Only the error paths need a name, so this stays off the hot path. */
    PyObject *columns = PyObject_GetAttrString((PyObject *)Py_TYPE(self), "__wreath_columns__");
    PyObject *name = NULL;
    if (columns == NULL) {
        PyErr_Clear();
        return PyUnicode_FromFormat("column %zd", index);
    }
    if (PyTuple_Check(columns) && index < PyTuple_GET_SIZE(columns)) {
        name = PyObject_GetAttrString(PyTuple_GET_ITEM(columns, index), "python_name");
    }
    Py_DECREF(columns);
    if (name == NULL) {
        PyErr_Clear();
        return PyUnicode_FromFormat("column %zd", index);
    }
    return name;
}

static PyObject *
storage_orm_new(PyObject *cls, PyObject *unused)
{
    PyTypeObject *type = (PyTypeObject *)cls;
    (void)unused;
    if (!PyType_Check(cls)) {
        PyErr_SetString(PyExc_TypeError, "_orm_new() must be called on a model class");
        return NULL;
    }
    return type->tp_alloc(type, 0);
}

static PyObject *
storage_orm_get(PyObject *self, PyObject *arg)
{
    WreathPgModelLayout *layout;
    Py_ssize_t index = PyLong_AsSsize_t(arg);
    const WreathPgModelField *field;
    if (index == -1 && PyErr_Occurred()) return NULL;
    field = field_at(self, &layout, index);
    if (field == NULL) return NULL;
    if (!bit_get(self, layout, BITMAP_LOADED, field->bit)) {
        PyObject *name = column_name_at(self, index);
        if (name == NULL) return NULL;
        raise_unloaded_attribute(self, name);
        Py_DECREF(name);
        return NULL;
    }
    if (bit_get(self, layout, BITMAP_NULL, field->bit)) Py_RETURN_NONE;
    return cell_load((const char *)self + field->offset, field);
}

/* Record a value read from the database: no coercion, no dirty bit.

   METH_FASTCALL, not METH_VARARGS: this is called once per column for every row
   hydrated and every request body validated, and packing an argument tuple and
   boxing the index for each of those calls cost more than the write itself. */
static PyObject *
storage_orm_set_loaded(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    WreathPgModelLayout *layout;
    Py_ssize_t index;
    PyObject *value;
    const WreathPgModelField *field;
    int changed;

    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_orm_set_loaded() takes exactly 2 arguments (%zd given)", nargs);
        return NULL;
    }
    index = PyLong_AsSsize_t(args[0]);
    if (index == -1 && PyErr_Occurred()) return NULL;
    value = args[1];
    field = field_at(self, &layout, index);
    if (field == NULL) return NULL;
    if (value == Py_None) {
        cell_release((char *)self + field->offset, field);
        bit_set(self, layout, BITMAP_NULL, field->bit, 1);
    } else {
        if (cell_store((char *)self + field->offset, field, value, &changed) < 0) {
            return NULL;
        }
        bit_set(self, layout, BITMAP_NULL, field->bit, 0);
    }
    bit_set(self, layout, BITMAP_LOADED, field->bit, 1);
    Py_RETURN_NONE;
}

static PyObject *
storage_orm_set(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    WreathPgModelLayout *layout;
    Py_ssize_t index;
    PyObject *value;
    const WreathPgModelField *field;
    PyObject *descriptor;
    PyObject *name;
    PyObject *dict;
    int result;

    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_orm_set() takes exactly 2 arguments (%zd given)", nargs);
        return NULL;
    }
    index = PyLong_AsSsize_t(args[0]);
    if (index == -1 && PyErr_Occurred()) return NULL;
    value = args[1];
    field = field_at(self, &layout, index);
    if (field == NULL) return NULL;
    /* Route through the descriptor so assignment has one implementation. It
       must come from the type's dict: getattr would invoke the descriptor and
       hand back the column's SQL expression instead. */
    name = column_name_at(self, index);
    if (name == NULL) return NULL;
    dict = PyType_GetDict(Py_TYPE(self));
    if (dict == NULL) {
        Py_DECREF(name);
        return NULL;
    }
    if (PyDict_GetItemRef(dict, name, &descriptor) < 0) {
        Py_DECREF(dict);
        Py_DECREF(name);
        return NULL;
    }
    Py_DECREF(dict);
    Py_DECREF(name);
    if (descriptor == NULL || !Py_IS_TYPE(descriptor, WreathPgColumnDescriptorType)) {
        Py_XDECREF(descriptor);
        PyErr_Format(PyExc_TypeError, "%.200s column %zd is not a native column",
                     Py_TYPE(self)->tp_name, index);
        return NULL;
    }
    result = column_store(self, (WreathPgColumnDescriptor *)descriptor, value);
    Py_DECREF(descriptor);
    if (result < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
bit_query(PyObject *self, PyObject *arg, int which)
{
    WreathPgModelLayout *layout;
    Py_ssize_t index = PyLong_AsSsize_t(arg);
    const WreathPgModelField *field;
    if (index == -1 && PyErr_Occurred()) return NULL;
    field = field_at(self, &layout, index);
    if (field == NULL) return NULL;
    return PyBool_FromLong(bit_get(self, layout, which, field->bit));
}

static PyObject *
storage_orm_is_loaded(PyObject *self, PyObject *arg)
{
    return bit_query(self, arg, BITMAP_LOADED);
}

static PyObject *
storage_orm_is_null(PyObject *self, PyObject *arg)
{
    return bit_query(self, arg, BITMAP_NULL);
}

static PyObject *
storage_orm_is_dirty(PyObject *self, PyObject *arg)
{
    return bit_query(self, arg, BITMAP_DIRTY);
}

static PyObject *
storage_orm_has_changes(PyObject *self, PyObject *unused)
{
    WreathPgModelLayout *layout = require_layout(self);
    uint64_t *words;
    (void)unused;
    if (layout == NULL) return NULL;
    words = bitmap_words(self, layout, BITMAP_DIRTY);
    for (Py_ssize_t i = 0; i < layout->bitmap_words; i++) {
        if (words[i]) Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

static PyObject *
storage_orm_clear_dirty(PyObject *self, PyObject *unused)
{
    WreathPgModelLayout *layout = require_layout(self);
    (void)unused;
    if (layout == NULL) return NULL;
    memset(bitmap_words(self, layout, BITMAP_DIRTY), 0,
           (size_t)layout->bitmap_words * 8);
    Py_RETURN_NONE;
}

static PyObject **
relation_slot(PyObject *self, Py_ssize_t index, WreathPgModelLayout **layout_out)
{
    WreathPgModelLayout *layout = require_layout(self);
    if (layout == NULL) return NULL;
    if (index < 0 || index >= layout->relation_count) {
        PyErr_Format(PyExc_IndexError, "relationship index %zd is out of range", index);
        return NULL;
    }
    if (layout_out) *layout_out = layout;
    return (PyObject **)((char *)self + layout->relation_offset +
                         index * (Py_ssize_t)sizeof(PyObject *));
}

static PyObject *
storage_orm_get_relation(PyObject *self, PyObject *arg)
{
    Py_ssize_t index = PyLong_AsSsize_t(arg);
    PyObject **slot;
    if (index == -1 && PyErr_Occurred()) return NULL;
    slot = relation_slot(self, index, NULL);
    if (slot == NULL) return NULL;
    if (*slot == NULL) {
        PyObject *name = PyUnicode_FromFormat("relationship %zd", index);
        PyErr_Format(
            error_unloaded_relationship == NULL ? PyExc_AttributeError
                                                : error_unloaded_relationship,
            "%.200s.%U was not loaded; include it in the query or call "
            "await session.load(...)",
            Py_TYPE(self)->tp_name, name
        );
        Py_XDECREF(name);
        return NULL;
    }
    return Py_NewRef(*slot);
}

static PyObject *
storage_orm_set_relation(PyObject *self, PyObject *args)
{
    Py_ssize_t index;
    PyObject *value;
    PyObject **slot;
    if (!PyArg_ParseTuple(args, "nO:_orm_set_relation", &index, &value)) return NULL;
    slot = relation_slot(self, index, NULL);
    if (slot == NULL) return NULL;
    Py_XSETREF(*slot, Py_NewRef(value));
    Py_RETURN_NONE;
}

static PyObject *
storage_orm_relation_loaded(PyObject *self, PyObject *arg)
{
    Py_ssize_t index = PyLong_AsSsize_t(arg);
    PyObject **slot;
    if (index == -1 && PyErr_Occurred()) return NULL;
    slot = relation_slot(self, index, NULL);
    if (slot == NULL) return NULL;
    return PyBool_FromLong(*slot != NULL);
}

static PyObject *
storage_get_state(PyObject *self, void *closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(((WreathPgModel *)self)->state_flags);
}

static int
storage_set_state(PyObject *self, PyObject *value, void *closure)
{
    unsigned long long state;
    (void)closure;
    if (value == NULL) {
        PyErr_SetString(PyExc_AttributeError, "cannot delete _orm_state");
        return -1;
    }
    state = PyLong_AsUnsignedLongLong(value);
    if (state == (unsigned long long)-1 && PyErr_Occurred()) return -1;
    ((WreathPgModel *)self)->state_flags = (uint64_t)state;
    return 0;
}

static PyObject *
storage_get_owner(PyObject *self, void *closure)
{
    WreathPgModel *model = (WreathPgModel *)self;
    (void)closure;
    return Py_NewRef(model->identity_owner == NULL ? Py_None : model->identity_owner);
}

static int
storage_set_owner(PyObject *self, PyObject *value, void *closure)
{
    WreathPgModel *model = (WreathPgModel *)self;
    (void)closure;
    if (value == NULL || value == Py_None) {
        Py_CLEAR(model->identity_owner);
        return 0;
    }
    Py_XSETREF(model->identity_owner, Py_NewRef(value));
    return 0;
}

/* A test-only description of the compiled layout. Reports sizes and offsets,
   never addresses. */
static PyObject *
storage_layout(PyObject *cls, void *closure)
{
    PyTypeObject *type = (PyTypeObject *)cls;
    WreathPgModelLayout *layout;
    PyObject *fields;
    PyObject *pointers;
    PyObject *result;
    (void)closure;

    if (Py_IS_TYPE(type, WreathPgModelTypeType)) {
        layout = PyObject_GetTypeData(cls, WreathPgModelTypeType);
    } else if (type->tp_base != NULL && Py_IS_TYPE(type->tp_base, WreathPgModelTypeType)) {
        layout = PyObject_GetTypeData((PyObject *)type->tp_base, WreathPgModelTypeType);
    } else {
        PyErr_SetString(PyExc_TypeError, "not a compiled model");
        return NULL;
    }
    fields = PyList_New(0);
    pointers = PyList_New(0);
    if (fields == NULL || pointers == NULL) {
        Py_XDECREF(fields);
        Py_XDECREF(pointers);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < layout->field_count; i++) {
        WreathPgModelField *field = &layout->fields[i];
        PyObject *item = Py_BuildValue(
            "{s:I,s:i,s:n,s:n,s:i}",
            "oid", field->oid, "kind", (int)field->kind,
            "offset", field->offset, "bit", field->bit,
            "size", (int)cell_size(field->kind)
        );
        if (item == NULL || PyList_Append(fields, item) < 0) {
            Py_XDECREF(item);
            Py_DECREF(fields);
            Py_DECREF(pointers);
            return NULL;
        }
        Py_DECREF(item);
    }
    for (Py_ssize_t i = 0; i < layout->pointer_count; i++) {
        PyObject *item = PyLong_FromSsize_t(layout->pointer_offsets[i]);
        if (item == NULL || PyList_Append(pointers, item) < 0) {
            Py_XDECREF(item);
            Py_DECREF(fields);
            Py_DECREF(pointers);
            return NULL;
        }
        Py_DECREF(item);
    }
    result = Py_BuildValue(
        "{s:n,s:n,s:n,s:n,s:n,s:n,s:N,s:N}",
        "basicsize", (Py_ssize_t)type->tp_basicsize,
        "storage_basicsize", layout->basicsize,
        "field_count", layout->field_count,
        "relation_count", layout->relation_count,
        "bitmap_offset", layout->bitmap_offset,
        "bitmap_words", layout->bitmap_words,
        "fields", fields,
        "pointer_offsets", pointers
    );
    return result;
}

static PyMethodDef storage_methods[] = {
    {"_orm_new", storage_orm_new, METH_NOARGS | METH_CLASS, NULL},
    {"_orm_get", storage_orm_get, METH_O, NULL},
    {"_orm_set", (PyCFunction)(void(*)(void))storage_orm_set, METH_FASTCALL, NULL},
    {"_orm_set_loaded", (PyCFunction)(void(*)(void))storage_orm_set_loaded, METH_FASTCALL, NULL},
    {"_orm_is_loaded", storage_orm_is_loaded, METH_O, NULL},
    {"_orm_is_null", storage_orm_is_null, METH_O, NULL},
    {"_orm_is_dirty", storage_orm_is_dirty, METH_O, NULL},
    {"_orm_has_changes", storage_orm_has_changes, METH_NOARGS, NULL},
    {"_orm_clear_dirty", storage_orm_clear_dirty, METH_NOARGS, NULL},
    {"_orm_get_relation", storage_orm_get_relation, METH_O, NULL},
    {"_orm_set_relation", storage_orm_set_relation, METH_VARARGS, NULL},
    {"_orm_relation_loaded", storage_orm_relation_loaded, METH_O, NULL},
    {NULL, NULL, 0, NULL}
};

static PyGetSetDef storage_getset[] = {
    {"_orm_state", storage_get_state, storage_set_state, NULL, NULL},
    {"_orm_owner", storage_get_owner, storage_set_owner, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

static PyType_Slot storage_slots[] = {
    {Py_tp_new, storage_new},
    {Py_tp_dealloc, storage_dealloc},
    {Py_tp_traverse, storage_traverse},
    {Py_tp_clear, storage_clear},
    {Py_tp_methods, storage_methods},
    {Py_tp_getset, storage_getset},
    {0, NULL}
};

/* -- layout compilation ---------------------------------------------------- */

/* Order fixed-width cells by decreasing alignment so padding stays minimal and
   every cell is naturally aligned. Ties keep declaration order. */
static int
compare_fields(const void *left, const void *right)
{
    const WreathPgModelField *a = *(const WreathPgModelField **)left;
    const WreathPgModelField *b = *(const WreathPgModelField **)right;
    Py_ssize_t left_align = cell_align(a->kind);
    Py_ssize_t right_align = cell_align(b->kind);
    if (left_align != right_align) return left_align > right_align ? -1 : 1;
    return a->bit < b->bit ? -1 : a->bit > b->bit ? 1 : 0;
}

#define MAX_MODEL_FIELDS 4096

static PyObject *
compile_model_layout(PyObject *module, PyObject *args)
{
    PyObject *columns;
    Py_ssize_t relation_count;
    PyObject *storage_type = NULL;
    WreathPgModelField *fields = NULL;
    Py_ssize_t *pointer_offsets = NULL;
    WreathPgModelField **ordered = NULL;
    WreathPgModelLayout *layout;
    Py_ssize_t count;
    Py_ssize_t offset;
    Py_ssize_t pointer_count = 0;
    Py_ssize_t pointer_index = 0;
    PyType_Spec spec;

    if (!PyArg_ParseTuple(args, "On:_compile_model_layout", &columns, &relation_count)) {
        return NULL;
    }
    if (!PyTuple_Check(columns)) {
        PyErr_SetString(PyExc_TypeError, "columns must be a tuple");
        return NULL;
    }
    count = PyTuple_GET_SIZE(columns);
    if (count > MAX_MODEL_FIELDS || relation_count < 0 ||
        relation_count > MAX_MODEL_FIELDS) {
        PyErr_Format(PyExc_ValueError,
                     "a model is limited to %d columns and relationships",
                     MAX_MODEL_FIELDS);
        return NULL;
    }

    fields = PyMem_Calloc((size_t)(count > 0 ? count : 1), sizeof(WreathPgModelField));
    ordered = PyMem_Calloc((size_t)(count > 0 ? count : 1), sizeof(WreathPgModelField *));
    if (fields == NULL || ordered == NULL) {
        PyErr_NoMemory();
        goto error;
    }

    for (Py_ssize_t i = 0; i < count; i++) {
        unsigned int oid;
        int primary_key, nullable;
        if (!PyArg_ParseTuple(PyTuple_GET_ITEM(columns, i), "Ipp", &oid, &primary_key,
                              &nullable)) {
            goto error;
        }
        fields[i].oid = (uint32_t)oid;
        fields[i].kind = cell_kind_for_oid((uint32_t)oid);
        fields[i].bit = i;
        fields[i].flags =
            (uint16_t)((primary_key ? WREATH_FIELD_PRIMARY_KEY : 0) |
                       (nullable ? WREATH_FIELD_NULLABLE : 0));
        ordered[i] = &fields[i];
        if (fields[i].kind == WREATH_CELL_OBJECT) pointer_count++;
    }

    /* Header, then bitmaps, then object cells and relationships, then the
       fixed-width cells ordered by decreasing alignment. */
    offset = (Py_ssize_t)sizeof(WreathPgModel);
    offset = align_up(offset, 8);

    Py_ssize_t bitmap_offset = offset;
    Py_ssize_t words = (count + 63) / 64;
    if (words > (PY_SSIZE_T_MAX / 8 - offset) / 3) {
        PyErr_SetString(PyExc_OverflowError, "model bitmaps overflow the object size");
        goto error;
    }
    offset += words * 8 * 3;

    Py_ssize_t total_pointers = pointer_count + relation_count;
    if (total_pointers > (PY_SSIZE_T_MAX - offset) / (Py_ssize_t)sizeof(PyObject *)) {
        PyErr_SetString(PyExc_OverflowError, "model pointer cells overflow the object size");
        goto error;
    }
    pointer_offsets = PyMem_Calloc(
        (size_t)(total_pointers > 0 ? total_pointers : 1), sizeof(Py_ssize_t)
    );
    if (pointer_offsets == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        if (fields[i].kind != WREATH_CELL_OBJECT) continue;
        fields[i].offset = offset;
        pointer_offsets[pointer_index++] = offset;
        offset += (Py_ssize_t)sizeof(PyObject *);
    }
    Py_ssize_t relation_offset = offset;
    for (Py_ssize_t i = 0; i < relation_count; i++) {
        pointer_offsets[pointer_index++] = offset;
        offset += (Py_ssize_t)sizeof(PyObject *);
    }

    qsort(ordered, (size_t)count, sizeof(WreathPgModelField *), compare_fields);
    for (Py_ssize_t i = 0; i < count; i++) {
        WreathPgModelField *field = ordered[i];
        Py_ssize_t size, alignment;
        if (field->kind == WREATH_CELL_OBJECT) continue;
        size = cell_size(field->kind);
        alignment = cell_align(field->kind);
        offset = align_up(offset, alignment);
        if (size > PY_SSIZE_T_MAX - offset) {
            PyErr_SetString(PyExc_OverflowError, "model cells overflow the object size");
            goto error;
        }
        field->offset = offset;
        offset += size;
    }
    offset = align_up(offset, 8);
    if (offset > INT_MAX) {
        PyErr_SetString(PyExc_OverflowError, "model exceeds the maximum object size");
        goto error;
    }

    memset(&spec, 0, sizeof(spec));
    spec.name = "wreath._native._postgres._ModelStorage";
    spec.basicsize = (int)offset;
    /* Managed weakrefs keep parity with the reference storage, which lists
       __weakref__ in its __slots__. */
    spec.flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC |
                 Py_TPFLAGS_MANAGED_WEAKREF;
    spec.slots = storage_slots;
    storage_type = PyType_FromMetaclass(
        WreathPgModelTypeType, module, &spec, (PyObject *)&PyBaseObject_Type
    );
    if (storage_type == NULL) goto error;

    /* The type owns the arrays from here on, and frees them in its dealloc. */
    layout = PyObject_GetTypeData(storage_type, WreathPgModelTypeType);
    layout->field_count = count;
    layout->relation_count = relation_count;
    layout->bitmap_words = words;
    layout->bitmap_offset = bitmap_offset;
    layout->relation_offset = relation_offset;
    layout->basicsize = offset;
    layout->fields = fields;
    layout->pointer_offsets = pointer_offsets;
    layout->pointer_count = total_pointers;
    PyMem_Free(ordered);
    return storage_type;

error:
    PyMem_Free(fields);
    PyMem_Free(pointer_offsets);
    PyMem_Free(ordered);
    return NULL;
}

/* -- descriptor construction ----------------------------------------------- */

static PyObject *
make_column_descriptor(PyObject *module, PyObject *args)
{
    PyObject *storage_type;
    PyObject *column;
    Py_ssize_t index;
    WreathPgColumnDescriptor *descriptor;
    WreathPgModelLayout *layout;
    (void)module;

    if (!PyArg_ParseTuple(args, "OnO:_make_column_descriptor", &storage_type, &index,
                          &column)) {
        return NULL;
    }
    if (!PyType_Check(storage_type) ||
        !Py_IS_TYPE((PyTypeObject *)storage_type, WreathPgModelTypeType)) {
        PyErr_SetString(PyExc_TypeError, "expected a compiled model storage type");
        return NULL;
    }
    layout = PyObject_GetTypeData(storage_type, WreathPgModelTypeType);
    if (index < 0 || index >= layout->field_count) {
        PyErr_SetString(PyExc_IndexError, "column index is out of range");
        return NULL;
    }
    descriptor = PyObject_GC_New(WreathPgColumnDescriptor, WreathPgColumnDescriptorType);
    if (descriptor == NULL) return NULL;
    Py_INCREF(WreathPgColumnDescriptorType);
    descriptor->column = Py_NewRef(column);
    descriptor->expression = NULL;
    descriptor->validate = NULL;
    descriptor->name = NULL;
    descriptor->field = layout->fields[index];
    descriptor->bitmap_offset = layout->bitmap_offset;
    descriptor->bitmap_words = layout->bitmap_words;

    descriptor->expression = PyObject_GetAttrString(column, "expression");
    descriptor->name = PyObject_GetAttrString(column, "python_name");
    /* Column.validate, not PgType.coerce: the column has already fused its type
       with its business rules, and taking coerce here would silently skip them
       on every native assignment while the reference backend still enforced
       them. Python compiles this before the descriptor is made. */
    descriptor->validate = PyObject_GetAttrString(column, "validate");
    if (descriptor->expression == NULL || descriptor->name == NULL ||
        descriptor->validate == NULL) {
        Py_DECREF(descriptor);
        return NULL;
    }
    if (!PyObject_GC_IsTracked((PyObject *)descriptor)) {
        PyObject_GC_Track(descriptor);
    }
    return (PyObject *)descriptor;
}

static PyObject *
make_relation_descriptor(PyObject *module, PyObject *args)
{
    PyObject *storage_type;
    PyObject *relationship;
    Py_ssize_t index;
    WreathPgRelationDescriptor *descriptor;
    WreathPgModelLayout *layout;
    (void)module;

    if (!PyArg_ParseTuple(args, "OnO:_make_relation_descriptor", &storage_type, &index,
                          &relationship)) {
        return NULL;
    }
    if (!PyType_Check(storage_type) ||
        !Py_IS_TYPE((PyTypeObject *)storage_type, WreathPgModelTypeType)) {
        PyErr_SetString(PyExc_TypeError, "expected a compiled model storage type");
        return NULL;
    }
    layout = PyObject_GetTypeData(storage_type, WreathPgModelTypeType);
    if (index < 0 || index >= layout->relation_count) {
        PyErr_SetString(PyExc_IndexError, "relationship index is out of range");
        return NULL;
    }
    descriptor = PyObject_GC_New(WreathPgRelationDescriptor, WreathPgRelationDescriptorType);
    if (descriptor == NULL) return NULL;
    Py_INCREF(WreathPgRelationDescriptorType);
    descriptor->relationship = Py_NewRef(relationship);
    descriptor->expression = PyObject_GetAttrString(relationship, "expression");
    descriptor->name = PyObject_GetAttrString(relationship, "python_name");
    descriptor->offset =
        layout->relation_offset + index * (Py_ssize_t)sizeof(PyObject *);
    if (descriptor->expression == NULL || descriptor->name == NULL) {
        Py_DECREF(descriptor);
        return NULL;
    }
    if (!PyObject_GC_IsTracked((PyObject *)descriptor)) {
        PyObject_GC_Track(descriptor);
    }
    return (PyObject *)descriptor;
}

static PyObject *
configure_model_errors(PyObject *module, PyObject *args)
{
    PyObject *unloaded_attribute;
    PyObject *unloaded_relationship;
    PyObject *declaration;
    (void)module;
    if (!PyArg_ParseTuple(args, "OOO:_configure_model_errors", &unloaded_attribute,
                          &unloaded_relationship, &declaration)) {
        return NULL;
    }
    Py_XSETREF(error_unloaded_attribute, Py_NewRef(unloaded_attribute));
    Py_XSETREF(error_unloaded_relationship, Py_NewRef(unloaded_relationship));
    Py_XSETREF(error_declaration, Py_NewRef(declaration));
    Py_RETURN_NONE;
}

/* Record the OID an extension type turned out to have, on one already-compiled
   field.
 *
 * Every other OID in a layout is a compile-time constant and is baked in when
 * the model class is defined. An extension type's is not: `CREATE EXTENSION`
 * assigns it, so at class-definition time it is 0, and the hydrate plan built
 * from this layout would then refuse every result row for that column -- the
 * result really does carry the live OID, and 0 really does not match it.
 *
 * Startup calls this once per such column, after resolution and before any
 * query. It changes *only* the recorded OID: the cell kind, offset, size, and
 * the instance's basicsize are all unchanged, which is checked here rather than
 * assumed, because a rebind that moved a cell would corrupt every instance
 * already allocated against the old layout. Both the unresolved OID (0) and
 * every extension OID land on WREATH_CELL_OBJECT, so the check passes for the
 * intended use and fails for anything else. */
static PyObject *
rebind_field_oid(PyObject *module, PyObject *args)
{
    PyObject *storage_type;
    Py_ssize_t index;
    unsigned int oid;
    WreathPgModelLayout *layout;
    WreathPgModelField *field;
    (void)module;

    if (!PyArg_ParseTuple(args, "OnI:_rebind_field_oid", &storage_type, &index, &oid))
        return NULL;
    if (!PyType_Check(storage_type)) {
        PyErr_SetString(PyExc_TypeError, "expected a model storage class");
        return NULL;
    }
    layout = wreath_pg_model_layout_for_type((PyTypeObject *)storage_type);
    if (layout == NULL) {
        PyErr_SetString(PyExc_TypeError, "model has no native storage layout");
        return NULL;
    }
    if (index < 0 || index >= layout->field_count) {
        PyErr_Format(PyExc_IndexError, "column index %zd is out of range", index);
        return NULL;
    }
    field = &layout->fields[index];
    if (cell_kind_for_oid(oid) != field->kind) {
        PyErr_Format(
            PyExc_ValueError,
            "OID %u would need cell kind %u but column %zd was compiled as kind %u; "
            "a rebind may only record a resolved OID, never change a cell's shape",
            oid, (unsigned)cell_kind_for_oid(oid), index, (unsigned)field->kind);
        return NULL;
    }
    field->oid = oid;
    Py_RETURN_NONE;
}

/* These references pin wreath.orm.errors, and through it the ORM package, so they
   must be dropped when the module goes away. */
void
wreath_pg_model_fini(void)
{
    Py_CLEAR(error_unloaded_attribute);
    Py_CLEAR(error_unloaded_relationship);
    Py_CLEAR(error_declaration);
}

static PyMethodDef model_methods[] = {
    {"_compile_model_layout", compile_model_layout, METH_VARARGS, NULL},
    {"_make_column_descriptor", make_column_descriptor, METH_VARARGS, NULL},
    {"_make_relation_descriptor", make_relation_descriptor, METH_VARARGS, NULL},
    {"_configure_model_errors", configure_model_errors, METH_VARARGS, NULL},
    {"_rebind_field_oid", rebind_field_oid, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL}
};

/* The layout's arrays are owned here rather than by anything in the type's
   dict: GC may clear a type's dict while instances of it are still alive, and
   traverse would then read freed memory. A type cannot be deallocated until
   every instance is gone, because each instance holds a reference to it, so
   freeing here is the one point where no instance can still need the arrays. */
static void
model_type_dealloc(PyObject *self)
{
    WreathPgModelLayout *layout = PyObject_GetTypeData(self, WreathPgModelTypeType);
    PyMem_Free(layout->fields);
    PyMem_Free(layout->pointer_offsets);
    layout->fields = NULL;
    layout->pointer_offsets = NULL;
    layout->field_count = 0;
    layout->pointer_count = 0;
    PyType_Type.tp_dealloc(self);
}

static PyType_Slot model_type_slots[] = {
    {Py_tp_dealloc, model_type_dealloc},
    {0, NULL}
};

/* The metatype carries the layout as data after PyHeapTypeObject. It defines no
   tp_new on purpose: PyType_FromMetaclass rejects a metaclass whose tp_new is
   not type_new. */
static PyType_Spec model_type_spec = {
    .name = "wreath._native._postgres._ModelType",
    .basicsize = -(int)sizeof(WreathPgModelLayout),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_ITEMS_AT_END,
    .slots = model_type_slots,
};

static PyGetSetDef model_type_getset[] = {
    {"__layout__", storage_layout, NULL, "test-only layout summary", NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

int
wreath_pg_model_init(PyObject *module)
{
    WreathPgModelTypeType = (PyTypeObject *)PyType_FromSpecWithBases(
        &model_type_spec, (PyObject *)&PyType_Type
    );
    if (WreathPgModelTypeType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "_ModelType", (PyObject *)WreathPgModelTypeType) < 0) {
        return -1;
    }
    Py_DECREF(WreathPgModelTypeType);

    WreathPgColumnDescriptorType = (PyTypeObject *)PyType_FromSpec(&column_descr_spec);
    if (WreathPgColumnDescriptorType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "_ColumnDescriptor",
                              (PyObject *)WreathPgColumnDescriptorType) < 0) {
        return -1;
    }
    Py_DECREF(WreathPgColumnDescriptorType);

    WreathPgRelationDescriptorType = (PyTypeObject *)PyType_FromSpec(&relation_descr_spec);
    if (WreathPgRelationDescriptorType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "_RelationDescriptor",
                              (PyObject *)WreathPgRelationDescriptorType) < 0) {
        return -1;
    }
    Py_DECREF(WreathPgRelationDescriptorType);

    if (PyModule_AddFunctions(module, model_methods) < 0) return -1;

    /* __layout__ is exposed on the metatype so both the storage base and the
       model class created from it answer it. */
    {
        PyObject *descriptor = PyDescr_NewGetSet(WreathPgModelTypeType, model_type_getset);
        int result;
        if (descriptor == NULL) return -1;
        result = PyObject_SetAttrString((PyObject *)WreathPgModelTypeType, "__layout__",
                                        descriptor);
        Py_DECREF(descriptor);
        if (result < 0) return -1;
    }
    return 0;
}
