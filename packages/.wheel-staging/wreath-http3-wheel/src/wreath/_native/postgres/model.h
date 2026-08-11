#ifndef WREATH_POSTGRES_MODEL_H
#define WREATH_POSTGRES_MODEL_H

#include <Python.h>
#include <stdint.h>

/* How one column's value is held in a model instance. Fixed-width kinds live
   inline in the instance; everything else holds an owned PyObject *. */
typedef enum {
    WREATH_CELL_OBJECT = 0,
    WREATH_CELL_BOOL,
    WREATH_CELL_INT2,
    WREATH_CELL_INT4,
    WREATH_CELL_INT8,
    WREATH_CELL_FLOAT4,
    WREATH_CELL_FLOAT8,
    WREATH_CELL_DATE,
    WREATH_CELL_TIMESTAMP,
    WREATH_CELL_TIMESTAMPTZ,
    WREATH_CELL_UUID
} WreathPgCellKind;

#define WREATH_FIELD_PRIMARY_KEY 0x1u
#define WREATH_FIELD_NULLABLE 0x2u

typedef struct {
    uint32_t oid;
    uint16_t kind;
    uint16_t flags;
    Py_ssize_t offset; /* byte offset of the cell within the instance */
    Py_ssize_t bit;    /* loaded/null/dirty bit index = declaration position */
} WreathPgModelField;

/* Per-compiled-model layout. Held in the generated storage type's metatype
   data rather than in every instance, and deliberately free of PyObject *
   fields: the fields/pointer_offsets arrays are owned by a capsule in the
   storage type's dict, so the type's own GC frees them and this struct needs
   no traverse, clear, or dealloc of its own. */
typedef struct {
    Py_ssize_t field_count;
    Py_ssize_t relation_count;
    Py_ssize_t bitmap_words;    /* uint64 words per bitmap */
    Py_ssize_t bitmap_offset;   /* loaded[words], then null[words], then dirty[words] */
    Py_ssize_t relation_offset; /* first relationship PyObject * cell */
    Py_ssize_t basicsize;
    WreathPgModelField *fields;
    Py_ssize_t *pointer_offsets; /* every PyObject * cell, for traverse/clear */
    Py_ssize_t pointer_count;
} WreathPgModelLayout;

/* Instance header. Bitmaps and cells follow at layout-computed offsets. */
typedef struct {
    PyObject_HEAD
    PyObject *identity_owner; /* owning Session, or NULL */
    uint64_t state_flags;
} WreathPgModel;

/* The metatype that carries WreathPgModelLayout. wreath.orm's ModelMeta derives from
   it, which is what lets a model class be created by Python while its storage
   base is created here without a metaclass conflict. */
extern PyTypeObject *WreathPgModelTypeType;

int wreath_pg_model_init(PyObject *module);
void wreath_pg_model_fini(void);

/* Layout for an instance of a compiled model, or NULL (no exception set). */
WreathPgModelLayout *wreath_pg_model_layout_of(PyObject *instance);

/* Layout for a compiled model class, or NULL (no exception set). */
WreathPgModelLayout *wreath_pg_model_layout_for_type(PyTypeObject *type);

/* Allocate an empty instance of a compiled model: all bits clear, no cells,
   TRANSIENT, unowned. */
PyObject *wreath_pg_model_alloc(PyTypeObject *type);

#endif
