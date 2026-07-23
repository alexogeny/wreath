/* Canonical packed migration images and deterministic linear merge diff. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "decode.h"
#include "migration_artifact.h"
#include "migration_image.h"
#include "tape.h"

#define IMAGE_HEADER_SIZE 16
#define IMAGE_RECORD_SIZE 24
#define TAPE_HEADER_SIZE 12
#define TAPE_RECORD_SIZE 24
#define IMAGE_VERSION 1
#define OP_ADD 1
#define OP_DROP 2
#define OP_ALTER 3

typedef struct {
    PyObject_HEAD
    unsigned char *records;
    unsigned char *descriptor;
    Py_ssize_t count;
    Py_ssize_t capacity;
    Py_ssize_t descriptor_length;
    Py_ssize_t descriptor_capacity;
    int finished;
    int needs_sort;
    int named_only;
} WreathMigrationCatalog;

static PyTypeObject *WreathMigrationCatalogType;

typedef struct {
    Py_buffer view;
    const unsigned char *records;
    uint32_t count;
} WreathMigrationImage;

static uint16_t
read_u16_le(const unsigned char *value)
{
    return (uint16_t)(((uint16_t)value[0]) | ((uint16_t)value[1] << 8));
}

static uint32_t
read_u32_le(const unsigned char *value)
{
    return ((uint32_t)value[0]) |
           ((uint32_t)value[1] << 8) |
           ((uint32_t)value[2] << 16) |
           ((uint32_t)value[3] << 24);
}

static uint64_t
read_u64_le(const unsigned char *value)
{
    return ((uint64_t)read_u32_le(value)) |
           ((uint64_t)read_u32_le(value + 4) << 32);
}

static void
write_u16_le(unsigned char *target, uint16_t value)
{
    target[0] = (unsigned char)value;
    target[1] = (unsigned char)(value >> 8);
}

static void
write_u32_le(unsigned char *target, uint32_t value)
{
    target[0] = (unsigned char)value;
    target[1] = (unsigned char)(value >> 8);
    target[2] = (unsigned char)(value >> 16);
    target[3] = (unsigned char)(value >> 24);
}

static void
write_u64_le(unsigned char *target, uint64_t value)
{
    write_u32_le(target, (uint32_t)value);
    write_u32_le(target + 4, (uint32_t)(value >> 32));
}

static int
record_compare(const unsigned char *left, const unsigned char *right)
{
    const uint32_t left_kind = read_u32_le(left + 16);
    const uint32_t right_kind = read_u32_le(right + 16);
    const uint64_t left_id = read_u64_le(left);
    const uint64_t right_id = read_u64_le(right);

    if (left_kind != right_kind) {
        return left_kind < right_kind ? -1 : 1;
    }
    if (left_id == right_id) {
        return 0;
    }
    return left_id < right_id ? -1 : 1;
}

static int
image_open(PyObject *object, const char *name, WreathMigrationImage *image)
{
    Py_ssize_t expected;
    uint32_t index;

    memset(image, 0, sizeof(*image));
    if (PyObject_GetBuffer(object, &image->view, PyBUF_CONTIG_RO) < 0) {
        return -1;
    }
    if (!image->view.readonly) {
        PyErr_Format(PyExc_TypeError, "%s migration image must be read-only", name);
        goto error;
    }
    if (image->view.len < IMAGE_HEADER_SIZE) {
        PyErr_Format(PyExc_ValueError, "%s migration image is truncated", name);
        goto error;
    }
    if (memcmp(image->view.buf, "WMI1", 4) != 0 ||
        read_u32_le((const unsigned char *)image->view.buf + 4) != IMAGE_VERSION ||
        read_u32_le((const unsigned char *)image->view.buf + 8) != IMAGE_RECORD_SIZE) {
        PyErr_Format(
            PyExc_ValueError,
            "invalid %s WMI1 image: expected WMI1 magic, format 1, and 24-byte records",
            name);
        goto error;
    }
    image->count = read_u32_le((const unsigned char *)image->view.buf + 12);
    if (image->count > (uint32_t)((PY_SSIZE_T_MAX - IMAGE_HEADER_SIZE) / IMAGE_RECORD_SIZE)) {
        PyErr_Format(PyExc_ValueError, "%s migration image is too large", name);
        goto error;
    }
    expected = IMAGE_HEADER_SIZE + (Py_ssize_t)image->count * IMAGE_RECORD_SIZE;
    if (image->view.len != expected) {
        PyErr_Format(PyExc_ValueError, "%s migration image has an invalid byte length", name);
        goto error;
    }
    image->records = (const unsigned char *)image->view.buf + IMAGE_HEADER_SIZE;
    for (index = 1; index < image->count; index++) {
        const unsigned char *previous = image->records + (index - 1) * IMAGE_RECORD_SIZE;
        const unsigned char *current = image->records + index * IMAGE_RECORD_SIZE;
        if (record_compare(previous, current) >= 0) {
            PyErr_Format(
                PyExc_ValueError,
                "%s migration image records must be canonical and unique",
                name);
            goto error;
        }
    }
    return 0;

error:
    PyBuffer_Release(&image->view);
    memset(image, 0, sizeof(*image));
    return -1;
}

static uint32_t
diff_count(const WreathMigrationImage *desired, const WreathMigrationImage *actual)
{
    uint32_t left = 0;
    uint32_t right = 0;
    uint32_t count = 0;

    while (left < desired->count && right < actual->count) {
        const unsigned char *wanted = desired->records + left * IMAGE_RECORD_SIZE;
        const unsigned char *found = actual->records + right * IMAGE_RECORD_SIZE;
        const int compared = record_compare(wanted, found);
        if (compared < 0) {
            left++;
            count++;
        }
        else if (compared > 0) {
            right++;
            count++;
        }
        else {
            if (read_u32_le(wanted + 20) != read_u32_le(found + 20) ||
                read_u64_le(wanted + 8) != read_u64_le(found + 8)) {
                count++;
            }
            left++;
            right++;
        }
    }
    return count + (desired->count - left) + (actual->count - right);
}

static void
write_operation(
    unsigned char *target,
    uint32_t operation,
    const unsigned char *record,
    uint32_t before,
    uint32_t after)
{
    write_u32_le(target, operation);
    write_u32_le(target + 4, read_u32_le(record + 16));
    write_u64_le(target + 8, read_u64_le(record));
    write_u32_le(target + 16, before);
    write_u32_le(target + 20, after);
}

static PyObject *
migration_diff_images(PyObject *self, PyObject *args)
{
    PyObject *desired_object;
    PyObject *actual_object;
    WreathMigrationImage desired;
    WreathMigrationImage actual;
    PyObject *result = NULL;
    unsigned char *output;
    uint32_t operation_count;
    uint32_t left = 0;
    uint32_t right = 0;
    uint32_t written = 0;

    (void)self;
    if (!PyArg_ParseTuple(
            args, "OO:_migration_diff_images", &desired_object, &actual_object)) {
        return NULL;
    }
    if (image_open(desired_object, "desired", &desired) < 0) {
        return NULL;
    }
    if (image_open(actual_object, "actual", &actual) < 0) {
        PyBuffer_Release(&desired.view);
        return NULL;
    }
    operation_count = diff_count(&desired, &actual);
    if (operation_count > (uint32_t)((PY_SSIZE_T_MAX - TAPE_HEADER_SIZE) / TAPE_RECORD_SIZE)) {
        PyErr_SetString(PyExc_OverflowError, "migration operation tape is too large");
        goto done;
    }
    result = PyBytes_FromStringAndSize(
        NULL,
        TAPE_HEADER_SIZE + (Py_ssize_t)operation_count * TAPE_RECORD_SIZE);
    if (result == NULL) {
        goto done;
    }
    output = (unsigned char *)PyBytes_AS_STRING(result);
    memcpy(output, "WMO1", 4);
    write_u32_le(output + 4, IMAGE_VERSION);
    write_u32_le(output + 8, operation_count);
    output += TAPE_HEADER_SIZE;

    while (left < desired.count && right < actual.count) {
        const unsigned char *wanted = desired.records + left * IMAGE_RECORD_SIZE;
        const unsigned char *found = actual.records + right * IMAGE_RECORD_SIZE;
        const int compared = record_compare(wanted, found);
        if (compared < 0) {
            write_operation(output + written++ * TAPE_RECORD_SIZE, OP_ADD, wanted, 0,
                            read_u32_le(wanted + 20));
            left++;
        }
        else if (compared > 0) {
            write_operation(output + written++ * TAPE_RECORD_SIZE, OP_DROP, found,
                            read_u32_le(found + 20), 0);
            right++;
        }
        else {
            const uint32_t before = read_u32_le(found + 20);
            const uint32_t after = read_u32_le(wanted + 20);
            if (before != after || read_u64_le(wanted + 8) != read_u64_le(found + 8)) {
                write_operation(output + written++ * TAPE_RECORD_SIZE, OP_ALTER, wanted,
                                before, after);
            }
            left++;
            right++;
        }
    }
    while (left < desired.count) {
        const unsigned char *wanted = desired.records + left++ * IMAGE_RECORD_SIZE;
        write_operation(output + written++ * TAPE_RECORD_SIZE, OP_ADD, wanted, 0,
                        read_u32_le(wanted + 20));
    }
    while (right < actual.count) {
        const unsigned char *found = actual.records + right++ * IMAGE_RECORD_SIZE;
        write_operation(output + written++ * TAPE_RECORD_SIZE, OP_DROP, found,
                        read_u32_le(found + 20), 0);
    }

 done:
    PyBuffer_Release(&actual.view);
    PyBuffer_Release(&desired.view);
    return result;
}

static uint32_t
read_u32_be(const unsigned char *value)
{
    return ((uint32_t)value[0] << 24) |
           ((uint32_t)value[1] << 16) |
           ((uint32_t)value[2] << 8) |
           (uint32_t)value[3];
}

static uint64_t
read_u64_be(const unsigned char *value)
{
    return ((uint64_t)read_u32_be(value) << 32) |
           (uint64_t)read_u32_be(value + 4);
}

static void
migration_catalog_dealloc(PyObject *object)
{
    WreathMigrationCatalog *self = (WreathMigrationCatalog *)object;
    PyMem_Free(self->records);
    PyMem_Free(self->descriptor);
    Py_TYPE(object)->tp_free(object);
}

static int
migration_catalog_reserve(WreathMigrationCatalog *self, Py_ssize_t required)
{
    Py_ssize_t capacity;
    unsigned char *records;
    if (required <= self->capacity) return 0;
    capacity = self->capacity == 0 ? 256 : self->capacity;
    while (capacity < required) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            PyErr_SetString(PyExc_OverflowError, "migration catalog is too large");
            return -1;
        }
        capacity *= 2;
    }
    if (capacity > PY_SSIZE_T_MAX / IMAGE_RECORD_SIZE) {
        PyErr_SetString(PyExc_OverflowError, "migration catalog is too large");
        return -1;
    }
    records = PyMem_Realloc(self->records, (size_t)capacity * IMAGE_RECORD_SIZE);
    if (records == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->records = records;
    self->capacity = capacity;
    return 0;
}

static int
migration_catalog_descriptor_reserve(
    WreathMigrationCatalog *self, Py_ssize_t required)
{
    Py_ssize_t capacity;
    unsigned char *descriptor;
    if (required <= self->descriptor_capacity) return 0;
    capacity = self->descriptor_capacity == 0 ? 4096 : self->descriptor_capacity;
    while (capacity < required) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            PyErr_SetString(PyExc_OverflowError, "migration descriptor is too large");
            return -1;
        }
        capacity *= 2;
    }
    descriptor = PyMem_Realloc(self->descriptor, (size_t)capacity);
    if (descriptor == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->descriptor = descriptor;
    self->descriptor_capacity = capacity;
    return 0;
}

static const unsigned char *
catalog_field_bytes(
    WreathPgFieldTape *tape,
    WreathPgFieldRef *field,
    Py_buffer *buffers,
    Py_ssize_t expected_length,
    Py_ssize_t *length_out)
{
    Py_ssize_t slot;
    if (field->length < 0 ||
        (expected_length >= 0 && field->length != expected_length) ||
        (Py_ssize_t)field->slab_index < tape->owner_head) {
        PyErr_SetString(PyExc_ValueError, "catalog field has an invalid binary value");
        return NULL;
    }
    slot = (Py_ssize_t)field->slab_index - tape->owner_head;
    if ((uint64_t)field->offset + (uint64_t)field->length >
        (uint64_t)buffers[slot].len) {
        PyErr_Format(
            PyExc_RuntimeError,
            "catalog field range exceeds its slab: offset %u plus length %d is greater than %zd bytes",
            field->offset, field->length, buffers[slot].len);
        return NULL;
    }
    *length_out = field->length;
    return (const unsigned char *)buffers[slot].buf + field->offset;
}

static const unsigned char *
catalog_field_data(
    WreathPgFieldTape *tape,
    WreathPgFieldRef *field,
    Py_buffer *buffers,
    Py_ssize_t expected_length)
{
    Py_ssize_t ignored;
    return catalog_field_bytes(
        tape, field, buffers, expected_length, &ignored);
}

static uint64_t
catalog_hash_part(uint64_t hash, const unsigned char *value, Py_ssize_t length)
{
    for (Py_ssize_t index = 0; index < length; index++) {
        hash ^= value[index];
        hash *= UINT64_C(1099511628211);
    }
    hash ^= 0xffU;
    return hash * UINT64_C(1099511628211);
}

uint64_t
wreath_pg_migration_object_id(
    uint32_t kind,
    const unsigned char *schema, Py_ssize_t schema_length,
    const unsigned char *table, Py_ssize_t table_length,
    const unsigned char *name, Py_ssize_t name_length)
{
    uint64_t hash = UINT64_C(14695981039346656037);
    unsigned char kind_bytes[4];
    write_u32_le(kind_bytes, kind);
    hash = catalog_hash_part(hash, kind_bytes, 4);
    hash = catalog_hash_part(hash, schema, schema_length);
    hash = catalog_hash_part(hash, table, table_length);
    return catalog_hash_part(hash, name, name_length);
}

int
wreath_pg_migration_catalog_check(PyObject *object)
{
    return WreathMigrationCatalogType != NULL &&
           PyObject_TypeCheck(object, WreathMigrationCatalogType);
}

static int
catalog_decode_numeric_row(
    WreathMigrationCatalog *catalog,
    WreathPgFieldTape *tape,
    Py_buffer *buffers,
    Py_ssize_t row)
{
    const unsigned char *object_id = catalog_field_data(
        tape, wreath_pg_tape_ref(tape, row, 0), buffers, 8);
    const unsigned char *parent_id = catalog_field_data(
        tape, wreath_pg_tape_ref(tape, row, 1), buffers, 8);
    const unsigned char *kind = catalog_field_data(
        tape, wreath_pg_tape_ref(tape, row, 2), buffers, 4);
    const unsigned char *signature = catalog_field_data(
        tape, wreath_pg_tape_ref(tape, row, 3), buffers, 4);
    unsigned char *record;
    if (object_id == NULL || parent_id == NULL || kind == NULL || signature == NULL)
        return -1;
    record = catalog->records + catalog->count * IMAGE_RECORD_SIZE;
    write_u64_le(record, read_u64_be(object_id));
    write_u64_le(record + 8, read_u64_be(parent_id));
    write_u32_le(record + 16, read_u32_be(kind));
    write_u32_le(record + 20, read_u32_be(signature));
    if (catalog->count > 0 &&
        record_compare(record - IMAGE_RECORD_SIZE, record) >= 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "catalog rows must be ordered canonically and have unique object IDs");
        return -1;
    }
    catalog->count++;
    return 0;
}

uint32_t
wreath_pg_migration_signature(const unsigned char *value, Py_ssize_t length)
{
    uint32_t hash = UINT32_C(2166136261);
    for (Py_ssize_t index = 0; index < length; index++) {
        hash ^= value[index];
        hash *= UINT32_C(16777619);
    }
    return hash;
}

static int
catalog_append_descriptor(
    WreathMigrationCatalog *catalog,
    const unsigned char *schema, Py_ssize_t schema_length,
    const unsigned char *table, Py_ssize_t table_length,
    const unsigned char *name, Py_ssize_t name_length,
    uint32_t kind,
    const unsigned char *signature, Py_ssize_t signature_length)
{
    Py_ssize_t required;
    unsigned char *target;
    if (schema_length > UINT16_MAX || table_length > UINT16_MAX ||
        name_length > UINT16_MAX || signature_length > UINT16_MAX) {
        PyErr_SetString(PyExc_ValueError, "catalog descriptor value exceeds 65535 bytes");
        return -1;
    }
    if (schema_length > PY_SSIZE_T_MAX - table_length - name_length - signature_length - 12 ||
        catalog->descriptor_length > PY_SSIZE_T_MAX -
            (12 + schema_length + table_length + name_length + signature_length)) {
        PyErr_SetString(PyExc_OverflowError, "migration descriptor is too large");
        return -1;
    }
    required = catalog->descriptor_length + 12 + schema_length + table_length +
        name_length + signature_length;
    if (migration_catalog_descriptor_reserve(catalog, required) < 0) return -1;
    target = catalog->descriptor + catalog->descriptor_length;
    write_u16_le(target, (uint16_t)schema_length);
    write_u16_le(target + 2, (uint16_t)table_length);
    write_u16_le(target + 4, (uint16_t)name_length);
    write_u16_le(target + 6, (uint16_t)signature_length);
    write_u32_le(target + 8, kind);
    target += 12;
    memcpy(target, schema, (size_t)schema_length);
    target += schema_length;
    memcpy(target, table, (size_t)table_length);
    target += table_length;
    memcpy(target, name, (size_t)name_length);
    target += name_length;
    memcpy(target, signature, (size_t)signature_length);
    catalog->descriptor_length = required;
    return 0;
}

static int
catalog_decode_named_row(
    WreathMigrationCatalog *catalog,
    WreathPgFieldTape *tape,
    Py_buffer *buffers,
    Py_ssize_t row,
    int signature_text)
{
    Py_ssize_t schema_length, table_length, name_length;
    const unsigned char *schema = catalog_field_bytes(
        tape, wreath_pg_tape_ref(tape, row, 0), buffers, -1, &schema_length);
    const unsigned char *table = catalog_field_bytes(
        tape, wreath_pg_tape_ref(tape, row, 1), buffers, -1, &table_length);
    const unsigned char *name = catalog_field_bytes(
        tape, wreath_pg_tape_ref(tape, row, 2), buffers, -1, &name_length);
    const unsigned char *kind_data = catalog_field_data(
        tape, wreath_pg_tape_ref(tape, row, 3), buffers, 4);
    Py_ssize_t signature_length;
    const unsigned char *signature_data = catalog_field_bytes(
        tape, wreath_pg_tape_ref(tape, row, 4), buffers,
        signature_text ? -1 : 4, &signature_length);
    static const unsigned char empty[1] = {0};
    uint32_t kind;
    uint32_t signature;
    uint64_t table_id;
    uint64_t object_id;
    unsigned char *record;
    if (schema == NULL || table == NULL || name == NULL ||
        kind_data == NULL || signature_data == NULL) return -1;
    kind = read_u32_be(kind_data);
    table_id = wreath_pg_migration_object_id(
        1, schema, schema_length, table, table_length, empty, 0);
    object_id = kind == 1 ? table_id : wreath_pg_migration_object_id(
        kind, schema, schema_length, table, table_length, name, name_length);
    signature = signature_text
        ? wreath_pg_migration_signature(signature_data, signature_length)
        : read_u32_be(signature_data);
    if (catalog_append_descriptor(
            catalog, schema, schema_length, table, table_length,
            name, name_length, kind, signature_data, signature_length) < 0) return -1;
    record = catalog->records + catalog->count * IMAGE_RECORD_SIZE;
    write_u64_le(record, object_id);
    write_u64_le(record + 8, kind == 1 ? 0 : table_id);
    write_u32_le(record + 16, kind);
    write_u32_le(record + 20, signature);
    catalog->count++;
    catalog->needs_sort = 1;
    return 0;
}

int
wreath_pg_migration_catalog_decode(
    PyObject *plan_object,
    PyObject *tape_object,
    PyObject *destination,
    Py_ssize_t limit)
{
    WreathPgDecoderPlan *plan = (WreathPgDecoderPlan *)plan_object;
    WreathPgFieldTape *tape = (WreathPgFieldTape *)tape_object;
    WreathMigrationCatalog *catalog = (WreathMigrationCatalog *)destination;
    Py_ssize_t rows;
    Py_ssize_t owner_limit = 0;
    Py_ssize_t original_count;
    Py_ssize_t original_descriptor_length;
    Py_buffer *buffers;
    int result = -1;
    int named_mode;
    int signature_text;

    if (!PyObject_TypeCheck(plan_object, WreathPgDecoderPlanType) ||
        !PyObject_TypeCheck(tape_object, WreathPgFieldTapeType) ||
        !wreath_pg_migration_catalog_check(destination) || limit <= 0) {
        PyErr_SetString(PyExc_ValueError, "invalid migration catalog decode request");
        return -1;
    }
    named_mode = tape->stored_columns == 5 && plan->column_count >= 5 &&
        plan->columns[0].oid == 25 && plan->columns[1].oid == 25 &&
        plan->columns[2].oid == 25 && plan->columns[3].oid == 23 &&
        (plan->columns[4].oid == 23 || plan->columns[4].oid == 25);
    signature_text = named_mode && plan->columns[4].oid == 25;
    if (!named_mode && !(tape->stored_columns == 4 && plan->column_count >= 4 &&
        plan->columns[0].oid == 20 && plan->columns[1].oid == 20 &&
        plan->columns[2].oid == 23 && plan->columns[3].oid == 23)) {
        PyErr_SetString(
            PyExc_ValueError,
            "catalog destination requires named or numeric migration rows");
        return -1;
    }
    for (Py_ssize_t column = 0; column < tape->stored_columns; column++) {
        if (plan->columns[column].format != 1) {
            PyErr_SetString(PyExc_ValueError, "catalog destination requires binary rows");
            return -1;
        }
    }
    if (catalog->finished) {
        PyErr_SetString(PyExc_RuntimeError, "migration catalog is already finished");
        return -1;
    }
    rows = tape->row_count < limit ? tape->row_count : limit;
    if (rows == 0) return 0;
    if (rows > PY_SSIZE_T_MAX - catalog->count ||
        migration_catalog_reserve(catalog, catalog->count + rows) < 0) return -1;
    buffers = wreath_pg_acquire_owner_buffers(tape, rows, &owner_limit);
    if (buffers == NULL) return -1;
    original_count = catalog->count;
    original_descriptor_length = catalog->descriptor_length;
    if (!named_mode) catalog->named_only = 0;

    for (Py_ssize_t row = 0; row < rows; row++) {
        int decoded = named_mode
            ? catalog_decode_named_row(
                catalog, tape, buffers, row, signature_text)
            : catalog_decode_numeric_row(catalog, tape, buffers, row);
        if (decoded < 0) goto done;
    }
    if (wreath_pg_tape_consume(tape, rows) < 0) goto done;
    result = 0;

done:
    if (result < 0) {
        catalog->count = original_count;
        catalog->descriptor_length = original_descriptor_length;
    }
    wreath_pg_release_owner_buffers(buffers, owner_limit);
    return result;
}

static int
catalog_record_qsort_compare(const void *left, const void *right)
{
    return record_compare(
        (const unsigned char *)left,
        (const unsigned char *)right);
}

static PyObject *
migration_catalog_finish(PyObject *object, PyObject *ignored)
{
    WreathMigrationCatalog *self = (WreathMigrationCatalog *)object;
    PyObject *image;
    unsigned char *data;
    Py_ssize_t length;
    (void)ignored;
    if (self->finished) {
        PyErr_SetString(PyExc_RuntimeError, "migration catalog is already finished");
        return NULL;
    }
    if (self->count > (PY_SSIZE_T_MAX - IMAGE_HEADER_SIZE) / IMAGE_RECORD_SIZE ||
        self->count > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "migration catalog is too large");
        return NULL;
    }
    if (self->needs_sort && self->count > 1) {
        qsort(
            self->records,
            (size_t)self->count,
            IMAGE_RECORD_SIZE,
            catalog_record_qsort_compare);
        for (Py_ssize_t index = 1; index < self->count; index++) {
            if (record_compare(
                    self->records + (index - 1) * IMAGE_RECORD_SIZE,
                    self->records + index * IMAGE_RECORD_SIZE) == 0) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "catalog object ID collision or duplicate object");
                return NULL;
            }
        }
    }
    length = IMAGE_HEADER_SIZE + self->count * IMAGE_RECORD_SIZE;
    image = PyBytes_FromStringAndSize(NULL, length);
    if (image == NULL) return NULL;
    data = (unsigned char *)PyBytes_AS_STRING(image);
    memcpy(data, "WMI1", 4);
    write_u32_le(data + 4, IMAGE_VERSION);
    write_u32_le(data + 8, IMAGE_RECORD_SIZE);
    write_u32_le(data + 12, (uint32_t)self->count);
    if (self->count > 0)
        memcpy(data + IMAGE_HEADER_SIZE, self->records,
               (size_t)self->count * IMAGE_RECORD_SIZE);
    self->finished = 1;
    PyMem_Free(self->records);
    self->records = NULL;
    self->capacity = 0;
    return image;
}

static PyObject *
migration_catalog_descriptor(PyObject *object, PyObject *ignored)
{
    WreathMigrationCatalog *self = (WreathMigrationCatalog *)object;
    PyObject *result;
    unsigned char *data;
    Py_ssize_t length;
    (void)ignored;
    if (!self->named_only) {
        PyErr_SetString(
            PyExc_RuntimeError, "numeric migration catalogs have no named descriptor");
        return NULL;
    }
    if (self->count > UINT32_MAX ||
        self->descriptor_length > PY_SSIZE_T_MAX - 12) {
        PyErr_SetString(PyExc_OverflowError, "migration descriptor is too large");
        return NULL;
    }
    length = 12 + self->descriptor_length;
    result = PyBytes_FromStringAndSize(NULL, length);
    if (result == NULL) return NULL;
    data = (unsigned char *)PyBytes_AS_STRING(result);
    memcpy(data, "WMD1", 4);
    write_u32_le(data + 4, 1);
    write_u32_le(data + 8, (uint32_t)self->count);
    if (self->descriptor_length > 0)
        memcpy(data + 12, self->descriptor, (size_t)self->descriptor_length);
    return result;
}

static PyMethodDef migration_catalog_methods[] = {
    {"descriptor", migration_catalog_descriptor, METH_NOARGS,
     PyDoc_STR("Return the bounded WMD1 names and signatures for decoded rows.")},
    {"finish", migration_catalog_finish, METH_NOARGS,
     PyDoc_STR("Freeze directly decoded catalog rows into one WMI1 image.")},
    {NULL, NULL, 0, NULL},
};

static PyType_Slot migration_catalog_slots[] = {
    {Py_tp_dealloc, migration_catalog_dealloc},
    {Py_tp_methods, migration_catalog_methods},
    {0, NULL},
};

static PyType_Spec migration_catalog_spec = {
    .name = "wreath._native._postgres._MigrationCatalog",
    .basicsize = sizeof(WreathMigrationCatalog),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = migration_catalog_slots,
};

static PyObject *
migration_catalog_builder(PyObject *module, PyObject *ignored)
{
    PyObject *object;
    (void)module;
    (void)ignored;
    object = PyObject_CallNoArgs((PyObject *)WreathMigrationCatalogType);
    if (object != NULL) ((WreathMigrationCatalog *)object)->named_only = 1;
    return object;
}

static PyObject *
migration_decode_catalog(PyObject *module, PyObject *args)
{
    PyObject *plan;
    PyObject *tape;
    PyObject *destination;
    Py_ssize_t limit;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "OOOn:_migration_decode_catalog",
            &plan, &tape, &destination, &limit)) return NULL;
    if (wreath_pg_migration_catalog_decode(plan, tape, destination, limit) < 0)
        return NULL;
    Py_RETURN_NONE;
}

static PyObject *
migration_compile_desired(PyObject *module, PyObject *args)
{
    const unsigned char *descriptor;
    Py_ssize_t length;
    uint32_t count;
    Py_ssize_t offset = 12;
    unsigned char *records = NULL;
    PyObject *image = NULL;
    unsigned char *output;
    (void)module;

    if (!PyArg_ParseTuple(
            args, "y#:_migration_compile_desired", &descriptor, &length)) return NULL;
    if (length < 12 || memcmp(descriptor, "WMD1", 4) != 0 ||
        read_u32_le(descriptor + 4) != 1) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid desired WMD1 descriptor: expected WMD1 magic, format 1, and a 12-byte header");
        return NULL;
    }
    count = read_u32_le(descriptor + 8);
    if (count > (uint32_t)((PY_SSIZE_T_MAX - IMAGE_HEADER_SIZE) / IMAGE_RECORD_SIZE)) {
        PyErr_SetString(PyExc_OverflowError, "desired descriptor is too large");
        return NULL;
    }
    records = PyMem_Malloc((size_t)(count > 0 ? count : 1) * IMAGE_RECORD_SIZE);
    if (records == NULL) return PyErr_NoMemory();
    for (uint32_t index = 0; index < count; index++) {
        uint16_t schema_length, table_length, name_length, signature_length;
        uint32_t kind;
        Py_ssize_t payload_length;
        const unsigned char *schema;
        const unsigned char *table;
        const unsigned char *name;
        const unsigned char *signature;
        uint64_t table_id;
        unsigned char *record;
        if (length - offset < 12) {
            PyErr_SetString(PyExc_ValueError, "desired descriptor is truncated");
            goto done;
        }
        schema_length = read_u16_le(descriptor + offset);
        table_length = read_u16_le(descriptor + offset + 2);
        name_length = read_u16_le(descriptor + offset + 4);
        signature_length = read_u16_le(descriptor + offset + 6);
        kind = read_u32_le(descriptor + offset + 8);
        offset += 12;
        payload_length = (Py_ssize_t)schema_length + table_length +
            name_length + signature_length;
        if (schema_length == 0 || table_length == 0 || payload_length > length - offset) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid desired WMD1 record %u: schema/table names must be nonempty and its %zd-byte payload must fit in %zd remaining bytes",
                index, payload_length, length - offset);
            goto done;
        }
        schema = descriptor + offset;
        table = schema + schema_length;
        name = table + table_length;
        signature = name + name_length;
        offset += payload_length;
        table_id = wreath_pg_migration_object_id(
            1, schema, schema_length, table, table_length,
            (const unsigned char *)"", 0);
        record = records + (Py_ssize_t)index * IMAGE_RECORD_SIZE;
        write_u64_le(
            record,
            kind == 1 ? table_id : wreath_pg_migration_object_id(
                kind, schema, schema_length, table, table_length, name, name_length));
        write_u64_le(record + 8, kind == 1 ? 0 : table_id);
        write_u32_le(record + 16, kind);
        write_u32_le(record + 20, wreath_pg_migration_signature(signature, signature_length));
    }
    if (offset != length) {
        PyErr_SetString(PyExc_ValueError, "desired descriptor has trailing bytes");
        goto done;
    }
    if (count > 1) {
        qsort(records, count, IMAGE_RECORD_SIZE, catalog_record_qsort_compare);
        for (uint32_t index = 1; index < count; index++) {
            if (record_compare(
                    records + (Py_ssize_t)(index - 1) * IMAGE_RECORD_SIZE,
                    records + (Py_ssize_t)index * IMAGE_RECORD_SIZE) == 0) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "desired object ID collision or duplicate object");
                goto done;
            }
        }
    }
    image = PyBytes_FromStringAndSize(
        NULL, IMAGE_HEADER_SIZE + (Py_ssize_t)count * IMAGE_RECORD_SIZE);
    if (image == NULL) goto done;
    output = (unsigned char *)PyBytes_AS_STRING(image);
    memcpy(output, "WMI1", 4);
    write_u32_le(output + 4, IMAGE_VERSION);
    write_u32_le(output + 8, IMAGE_RECORD_SIZE);
    write_u32_le(output + 12, count);
    if (count > 0)
        memcpy(output + IMAGE_HEADER_SIZE, records, (size_t)count * IMAGE_RECORD_SIZE);

done:
    PyMem_Free(records);
    return image;
}

typedef struct {
    const unsigned char *schema;
    const unsigned char *table;
    const unsigned char *name;
    const unsigned char *signature;
    uint16_t schema_length;
    uint16_t table_length;
    uint16_t name_length;
    uint16_t signature_length;
    uint32_t kind;
    uint32_t signature_hash;
    uint64_t object_id;
} WreathNamedMigrationRecord;

static int
named_record_compare(const void *left_object, const void *right_object)
{
    const WreathNamedMigrationRecord *left = left_object;
    const WreathNamedMigrationRecord *right = right_object;
    if (left->kind != right->kind) return left->kind < right->kind ? -1 : 1;
    if (left->object_id == right->object_id) return 0;
    return left->object_id < right->object_id ? -1 : 1;
}

static int
open_named_descriptor(
    const unsigned char *data,
    Py_ssize_t length,
    const char *label,
    WreathNamedMigrationRecord **records_out,
    uint32_t *count_out)
{
    uint32_t count;
    Py_ssize_t offset = 12;
    WreathNamedMigrationRecord *records;
    if (length < 12 || memcmp(data, "WMD1", 4) != 0 || read_u32_le(data + 4) != 1) {
        PyErr_Format(
            PyExc_ValueError,
            "invalid %s WMD1 descriptor: expected WMD1 magic, format 1, and a 12-byte header",
            label);
        return -1;
    }
    count = read_u32_le(data + 8);
    if (count > (uint32_t)(PY_SSIZE_T_MAX / sizeof(*records))) {
        PyErr_Format(PyExc_OverflowError, "%s descriptor is too large", label);
        return -1;
    }
    records = PyMem_Malloc((size_t)(count > 0 ? count : 1) * sizeof(*records));
    if (records == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (uint32_t index = 0; index < count; index++) {
        WreathNamedMigrationRecord *record = records + index;
        Py_ssize_t payload_length;
        uint64_t table_id;
        if (length - offset < 12) {
            PyErr_Format(PyExc_ValueError, "%s descriptor is truncated", label);
            goto error;
        }
        record->schema_length = read_u16_le(data + offset);
        record->table_length = read_u16_le(data + offset + 2);
        record->name_length = read_u16_le(data + offset + 4);
        record->signature_length = read_u16_le(data + offset + 6);
        record->kind = read_u32_le(data + offset + 8);
        offset += 12;
        payload_length = (Py_ssize_t)record->schema_length + record->table_length +
            record->name_length + record->signature_length;
        if (record->schema_length == 0 || record->table_length == 0 ||
            payload_length > length - offset) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid %s WMD1 record %u: schema/table names must be nonempty and its %zd-byte payload must fit in %zd remaining bytes",
                label, index, payload_length, length - offset);
            goto error;
        }
        record->schema = data + offset;
        record->table = record->schema + record->schema_length;
        record->name = record->table + record->table_length;
        record->signature = record->name + record->name_length;
        offset += payload_length;
        table_id = wreath_pg_migration_object_id(
            1, record->schema, record->schema_length,
            record->table, record->table_length, (const unsigned char *)"", 0);
        record->object_id = record->kind == 1 ? table_id : wreath_pg_migration_object_id(
            record->kind, record->schema, record->schema_length,
            record->table, record->table_length, record->name, record->name_length);
        record->signature_hash = wreath_pg_migration_signature(
            record->signature, record->signature_length);
    }
    if (offset != length) {
        PyErr_Format(PyExc_ValueError, "%s descriptor has trailing bytes", label);
        goto error;
    }
    if (count > 1) {
        qsort(records, count, sizeof(*records), named_record_compare);
        for (uint32_t index = 1; index < count; index++) {
            if (named_record_compare(records + index - 1, records + index) == 0) {
                PyErr_Format(
                    PyExc_ValueError, "%s descriptor has duplicate objects", label);
                goto error;
            }
        }
    }
    *records_out = records;
    *count_out = count;
    return 0;
error:
    PyMem_Free(records);
    return -1;
}

static Py_ssize_t
named_operation_size(
    const WreathNamedMigrationRecord *record,
    uint16_t before_length,
    uint16_t after_length)
{
    return 20 + (Py_ssize_t)record->schema_length + record->table_length +
        record->name_length + before_length + after_length;
}

static unsigned char *
write_named_operation(
    unsigned char *target,
    uint32_t operation,
    const WreathNamedMigrationRecord *record,
    const unsigned char *before, uint16_t before_length,
    const unsigned char *after, uint16_t after_length)
{
    write_u32_le(target, operation);
    write_u32_le(target + 4, record->kind);
    write_u16_le(target + 8, record->schema_length);
    write_u16_le(target + 10, record->table_length);
    write_u16_le(target + 12, record->name_length);
    write_u16_le(target + 14, before_length);
    write_u16_le(target + 16, after_length);
    write_u16_le(target + 18, 0);
    target += 20;
    memcpy(target, record->schema, record->schema_length);
    target += record->schema_length;
    memcpy(target, record->table, record->table_length);
    target += record->table_length;
    memcpy(target, record->name, record->name_length);
    target += record->name_length;
    memcpy(target, before, before_length);
    target += before_length;
    memcpy(target, after, after_length);
    return target + after_length;
}

static PyObject *
migration_plan_descriptors(PyObject *module, PyObject *args)
{
    const unsigned char *desired_data, *actual_data;
    Py_ssize_t desired_length, actual_length, output_length = 12;
    WreathNamedMigrationRecord *desired = NULL, *actual = NULL;
    uint32_t desired_count, actual_count, left = 0, right = 0, operation_count = 0;
    PyObject *result = NULL;
    unsigned char *output, *cursor;
    (void)module;
    if (!PyArg_ParseTuple(args, "y#y#:_migration_plan_descriptors",
            &desired_data, &desired_length, &actual_data, &actual_length)) return NULL;
    if (open_named_descriptor(
            desired_data, desired_length, "desired", &desired, &desired_count) < 0 ||
        open_named_descriptor(
            actual_data, actual_length, "actual", &actual, &actual_count) < 0) goto done;
    while (left < desired_count || right < actual_count) {
        const WreathNamedMigrationRecord *record;
        uint16_t before_length = 0, after_length = 0;
        int compared = left < desired_count && right < actual_count
            ? named_record_compare(desired + left, actual + right)
            : (left < desired_count ? -1 : 1);
        if (compared < 0) {
            record = desired + left++;
            after_length = record->signature_length;
        }
        else if (compared > 0) {
            record = actual + right++;
            before_length = record->signature_length;
        }
        else {
            record = desired + left;
            if (desired[left].signature_hash == actual[right].signature_hash) {
                left++;
                right++;
                continue;
            }
            before_length = actual[right].signature_length;
            after_length = desired[left].signature_length;
            left++;
            right++;
        }
        if (output_length > PY_SSIZE_T_MAX -
                named_operation_size(record, before_length, after_length)) {
            PyErr_SetString(PyExc_OverflowError, "named migration plan is too large");
            goto done;
        }
        output_length += named_operation_size(record, before_length, after_length);
        operation_count++;
    }
    result = PyBytes_FromStringAndSize(NULL, output_length);
    if (result == NULL) goto done;
    output = (unsigned char *)PyBytes_AS_STRING(result);
    memcpy(output, "WMP1", 4);
    write_u32_le(output + 4, 1);
    write_u32_le(output + 8, operation_count);
    cursor = output + 12;
    left = right = 0;
    while (left < desired_count || right < actual_count) {
        const WreathNamedMigrationRecord *record;
        const unsigned char *before = (const unsigned char *)"";
        const unsigned char *after = (const unsigned char *)"";
        uint16_t before_length = 0, after_length = 0;
        uint32_t operation;
        int compared = left < desired_count && right < actual_count
            ? named_record_compare(desired + left, actual + right)
            : (left < desired_count ? -1 : 1);
        if (compared < 0) {
            record = desired + left++;
            operation = OP_ADD;
            after = record->signature;
            after_length = record->signature_length;
        }
        else if (compared > 0) {
            record = actual + right++;
            operation = OP_DROP;
            before = record->signature;
            before_length = record->signature_length;
        }
        else {
            record = desired + left;
            if (desired[left].signature_hash == actual[right].signature_hash) {
                left++;
                right++;
                continue;
            }
            operation = OP_ALTER;
            before = actual[right].signature;
            before_length = actual[right].signature_length;
            after = desired[left].signature;
            after_length = desired[left].signature_length;
            left++;
            right++;
        }
        cursor = write_named_operation(
            cursor, operation, record, before, before_length, after, after_length);
    }

done:
    PyMem_Free(desired);
    PyMem_Free(actual);
    return result;
}

static PyObject *
migration_image_fingerprint(PyObject *module, PyObject *arg)
{
    WreathMigrationImage image;
    unsigned char digest[32];
    PyObject *result;
    (void)module;
    if (image_open(arg, "schema", &image) < 0) return NULL;
    wreath_pg_sha256(
        (const unsigned char *)image.view.buf, image.view.len, digest);
    PyBuffer_Release(&image.view);
    result = PyBytes_FromStringAndSize((const char *)digest, 32);
    return result;
}

static PyMethodDef migration_image_methods[] = {
    {
        "_migration_plan_descriptors",
        migration_plan_descriptors,
        METH_VARARGS,
        PyDoc_STR("Build one deterministic named WMP1 operation plan in metal."),
    },
    {
        "_migration_image_fingerprint",
        migration_image_fingerprint,
        METH_O,
        PyDoc_STR("Hash one validated canonical WMI1 image with SHA-256."),
    },
    {
        "_migration_compile_desired",
        migration_compile_desired,
        METH_VARARGS,
        PyDoc_STR("Compile one bounded WMD1 descriptor into a canonical WMI1 image."),
    },
    {
        "_migration_catalog_builder",
        migration_catalog_builder,
        METH_NOARGS,
        PyDoc_STR("Create a direct native pg_catalog decode destination."),
    },
    {
        "_migration_decode_catalog",
        migration_decode_catalog,
        METH_VARARGS,
        PyDoc_STR("Decode a field tape directly into a migration catalog image."),
    },
    {
        "_migration_diff_images",
        migration_diff_images,
        METH_VARARGS,
        PyDoc_STR("Diff two canonical packed migration images."),
    },
    {NULL, NULL, 0, NULL},
};

int
wreath_pg_migration_image_init(PyObject *module)
{
    WreathMigrationCatalogType = (PyTypeObject *)PyType_FromSpec(&migration_catalog_spec);
    if (WreathMigrationCatalogType == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "_MigrationCatalog", (PyObject *)WreathMigrationCatalogType) < 0 ||
        PyModule_AddFunctions(module, migration_image_methods) < 0) {
        Py_CLEAR(WreathMigrationCatalogType);
        return -1;
    }
    return 0;
}
