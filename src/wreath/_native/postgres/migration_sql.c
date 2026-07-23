/* Deterministic SQL statement tapes derived from native named migration plans. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "buffer.h"
#include "migration_image.h"
#include "migration_sql.h"

#define SQL_FLAG_DESTRUCTIVE 1U
#define SQL_FLAG_MANUAL 2U
#define OP_ADD 1U
#define OP_DROP 2U
#define OP_ALTER 3U
#define KIND_TABLE 1U
#define KIND_COLUMN 2U
#define KIND_CONSTRAINT 3U
#define KIND_INDEX 4U


typedef struct {
    uint32_t action;
    uint32_t kind;
    const unsigned char *schema;
    const unsigned char *table;
    const unsigned char *name;
    const unsigned char *before;
    const unsigned char *after;
    uint16_t schema_length;
    uint16_t table_length;
    uint16_t name_length;
    uint16_t before_length;
    uint16_t after_length;
    uint32_t ordinal;
} WreathSqlOperation;


typedef struct {
    const unsigned char *data;
    Py_ssize_t length;
} WreathSqlPart;


static uint16_t
read_u16_le(const unsigned char *value)
{
    return (uint16_t)((uint16_t)value[0] | ((uint16_t)value[1] << 8));
}


static uint32_t
read_u32_le(const unsigned char *value)
{
    return (uint32_t)value[0] |
           ((uint32_t)value[1] << 8) |
           ((uint32_t)value[2] << 16) |
           ((uint32_t)value[3] << 24);
}


static int
append_u32_le(WreathPgBuffer *buffer, uint32_t value)
{
    unsigned char bytes[4] = {
        (unsigned char)value,
        (unsigned char)(value >> 8),
        (unsigned char)(value >> 16),
        (unsigned char)(value >> 24),
    };
    return wreath_pg_buffer_append(buffer, bytes, 4);
}


static int
append_literal(WreathPgBuffer *buffer, const char *value)
{
    return wreath_pg_buffer_append(buffer, value, (Py_ssize_t)strlen(value));
}


static int
append_identifier(
    WreathPgBuffer *buffer, const unsigned char *value, Py_ssize_t length)
{
    if (length == 0 || memchr(value, 0, (size_t)length) != NULL) {
        PyErr_SetString(PyExc_ValueError, "migration identifier is empty or contains NUL");
        return -1;
    }
    if (append_literal(buffer, "\"") < 0) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        if (value[index] == '"' && append_literal(buffer, "\"") < 0) return -1;
        if (wreath_pg_buffer_append(buffer, value + index, 1) < 0) return -1;
    }
    return append_literal(buffer, "\"");
}


static int
append_qualified(WreathPgBuffer *buffer, const WreathSqlOperation *operation)
{
    return append_identifier(buffer, operation->schema, operation->schema_length) < 0 ||
           append_literal(buffer, ".") < 0 ||
           append_identifier(buffer, operation->table, operation->table_length) < 0
        ? -1 : 0;
}


static uint32_t
operation_rank(const WreathSqlOperation *operation)
{
    if (operation->action == OP_DROP && operation->kind == KIND_CONSTRAINT) return 0;
    if (operation->action == OP_DROP && operation->kind == KIND_COLUMN) return 1;
    if (operation->action == OP_DROP && operation->kind == KIND_TABLE) return 2;
    if (operation->action == OP_ADD && operation->kind == KIND_TABLE) return 3;
    if (operation->action == OP_ADD && operation->kind == KIND_COLUMN) return 4;
    if (operation->action == OP_ALTER && operation->kind == KIND_COLUMN) return 5;
    if (operation->action == OP_ADD && operation->kind == KIND_CONSTRAINT) return 6;
    if (operation->action == OP_ADD && operation->kind == KIND_INDEX) return 7;
    return 99;
}


static uint64_t
operation_object_id(const WreathSqlOperation *operation)
{
    return wreath_pg_migration_object_id(
        operation->kind,
        operation->schema, operation->schema_length,
        operation->table, operation->table_length,
        operation->name, operation->name_length);
}


static int
operation_canonical_compare(const void *left_object, const void *right_object)
{
    const WreathSqlOperation *left = left_object;
    const WreathSqlOperation *right = right_object;
    const uint64_t left_id = operation_object_id(left);
    const uint64_t right_id = operation_object_id(right);
    if (left->kind != right->kind) return left->kind < right->kind ? -1 : 1;
    if (left_id == right_id) return 0;
    return left_id < right_id ? -1 : 1;
}


static int
operation_compare(const void *left_object, const void *right_object)
{
    const WreathSqlOperation *left = left_object;
    const WreathSqlOperation *right = right_object;
    const uint32_t left_rank = operation_rank(left);
    const uint32_t right_rank = operation_rank(right);
    if (left_rank != right_rank) return left_rank < right_rank ? -1 : 1;
    if (left->ordinal == right->ordinal) return 0;
    return left->ordinal < right->ordinal ? -1 : 1;
}


static int
parse_plan(
    const unsigned char *data,
    Py_ssize_t length,
    WreathSqlOperation **operations_out,
    uint32_t *count_out)
{
    Py_ssize_t offset = 12;
    uint32_t count;
    WreathSqlOperation *operations;
    if (length < 12 || memcmp(data, "WMP1", 4) != 0 || read_u32_le(data + 4) != 1) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid WMP1 named plan: expected WMP1 magic, format 1, and a 12-byte header");
        return -1;
    }
    count = read_u32_le(data + 8);
    if (count > (uint32_t)(PY_SSIZE_T_MAX / sizeof(*operations))) {
        PyErr_SetString(PyExc_OverflowError, "named migration plan is too large");
        return -1;
    }
    operations = PyMem_Malloc((size_t)(count > 0 ? count : 1) * sizeof(*operations));
    if (operations == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (uint32_t index = 0; index < count; index++) {
        WreathSqlOperation *operation = operations + index;
        Py_ssize_t payload_length;
        if (length - offset < 20) {
            PyErr_SetString(PyExc_ValueError, "named migration plan is truncated");
            goto error;
        }
        operation->action = read_u32_le(data + offset);
        operation->kind = read_u32_le(data + offset + 4);
        operation->schema_length = read_u16_le(data + offset + 8);
        operation->table_length = read_u16_le(data + offset + 10);
        operation->name_length = read_u16_le(data + offset + 12);
        operation->before_length = read_u16_le(data + offset + 14);
        operation->after_length = read_u16_le(data + offset + 16);
        operation->ordinal = index;
        if (read_u16_le(data + offset + 18) != 0 ||
            operation->action < OP_ADD || operation->action > OP_ALTER ||
            operation->kind < KIND_TABLE || operation->kind > 4U) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid WMP1 operation %u: action must be add (1), drop (2), or alter (3); object kind must be table (1) through index (4); reserved flags must be zero",
                index);
            goto error;
        }
        offset += 20;
        payload_length = (Py_ssize_t)operation->schema_length + operation->table_length +
            operation->name_length + operation->before_length + operation->after_length;
        if (operation->schema_length == 0 || operation->table_length == 0 ||
            payload_length > length - offset) {
            PyErr_Format(
                PyExc_ValueError,
                "invalid WMP1 operation %u: schema/table names must be nonempty and its %zd-byte payload must fit in the remaining %zd bytes",
                index, payload_length, length - offset);
            goto error;
        }
        operation->schema = data + offset;
        operation->table = operation->schema + operation->schema_length;
        operation->name = operation->table + operation->table_length;
        operation->before = operation->name + operation->name_length;
        operation->after = operation->before + operation->before_length;
        offset += payload_length;
    }
    if (offset != length) {
        PyErr_SetString(PyExc_ValueError, "named migration plan has trailing bytes");
        goto error;
    }
    if (count > 1) qsort(operations, count, sizeof(*operations), operation_compare);
    *operations_out = operations;
    *count_out = count;
    return 0;
error:
    PyMem_Free(operations);
    return -1;
}


static int
split_value(
    const unsigned char *data,
    Py_ssize_t length,
    unsigned char separator,
    WreathSqlPart *parts,
    Py_ssize_t expected)
{
    Py_ssize_t start = 0;
    Py_ssize_t count = 0;
    for (Py_ssize_t index = 0; index <= length; index++) {
        if (index == length || data[index] == separator) {
            if (count >= expected) return -1;
            parts[count].data = data + start;
            parts[count].length = index - start;
            count++;
            start = index + 1;
        }
    }
    return count == expected ? 0 : -1;
}


static int
part_equals(const WreathSqlPart *part, const char *value)
{
    const Py_ssize_t length = (Py_ssize_t)strlen(value);
    return part->length == length && memcmp(part->data, value, (size_t)length) == 0;
}


static int
parse_oid(const WreathSqlPart *part, uint32_t *oid_out)
{
    uint32_t value = 0;
    if (part->length == 0) return -1;
    for (Py_ssize_t index = 0; index < part->length; index++) {
        const unsigned char digit = part->data[index];
        if (digit < '0' || digit > '9' || value > (UINT32_MAX - (digit - '0')) / 10)
            return -1;
        value = value * 10 + (digit - '0');
    }
    *oid_out = value;
    return 0;
}


static const char *
sql_type_for_oid(uint32_t oid)
{
    switch (oid) {
        case 16: return "boolean";
        case 17: return "bytea";
        case 20: return "bigint";
        case 21: return "smallint";
        case 23: return "integer";
        case 25: return "text";
        case 700: return "real";
        case 701: return "double precision";
        case 1082: return "date";
        case 1114: return "timestamp without time zone";
        case 1184: return "timestamp with time zone";
        case 1700: return "numeric";
        case 2950: return "uuid";
        case 3802: return "jsonb";
        default: return NULL;
    }
}


static int
render_column_definition(
    WreathPgBuffer *statement,
    const unsigned char *signature,
    Py_ssize_t signature_length)
{
    WreathSqlPart parts[7];
    uint32_t oid;
    const char *type;
    if (split_value(signature, signature_length, 0x1f, parts, 7) < 0 ||
        !part_equals(parts, "column") || parse_oid(parts + 1, &oid) < 0)
        return 1;
    type = sql_type_for_oid(oid);
    if (type == NULL || parts[5].length != 0) return 1;
    if (append_literal(statement, type) < 0) return -1;
    if (part_equals(parts + 4, "a")) {
        if (append_literal(statement, " generated always as identity") < 0) return -1;
    }
    else if (part_equals(parts + 4, "d")) {
        if (append_literal(statement, " generated by default as identity") < 0) return -1;
    }
    else if (parts[4].length != 0) return 1;
    if (parts[6].length != 0) {
        if (append_literal(statement, " default ") < 0 ||
            wreath_pg_buffer_append(statement, parts[6].data, parts[6].length) < 0)
            return -1;
    }
    if (part_equals(parts + 3, "1")) {
        if (append_literal(statement, " not null") < 0) return -1;
    }
    else if (!part_equals(parts + 3, "0")) return 1;
    return 0;
}


static int
append_alter_column_prefix(
    WreathPgBuffer *statement, const WreathSqlOperation *operation)
{
    return append_literal(statement, "alter table ") < 0 ||
           append_qualified(statement, operation) < 0 ||
           append_literal(statement, " alter column ") < 0 ||
           append_identifier(statement, operation->name, operation->name_length) < 0
        ? -1 : 0;
}


static int
render_column_change(
    WreathPgBuffer *statement, const WreathSqlOperation *operation)
{
    WreathSqlPart before[7];
    WreathSqlPart after[7];
    uint32_t before_oid, after_oid;
    int wrote = 0;
    if (split_value(
            operation->before, operation->before_length, 0x1f, before, 7) < 0 ||
        split_value(
            operation->after, operation->after_length, 0x1f, after, 7) < 0 ||
        !part_equals(before, "column") || !part_equals(after, "column") ||
        parse_oid(before + 1, &before_oid) < 0 ||
        parse_oid(after + 1, &after_oid) < 0 ||
        before[4].length != 0 || after[4].length != 0 ||
        before[5].length != 0 || after[5].length != 0 ||
        (!part_equals(before + 3, "0") && !part_equals(before + 3, "1")) ||
        (!part_equals(after + 3, "0") && !part_equals(after + 3, "1"))) return 1;
    if (before_oid != after_oid) {
        const char *type = sql_type_for_oid(after_oid);
        if (type == NULL || append_alter_column_prefix(statement, operation) < 0 ||
            append_literal(statement, " type ") < 0 ||
            append_literal(statement, type) < 0 || append_literal(statement, ";") < 0)
            return type == NULL ? 1 : -1;
        wrote = 1;
    }
    if (before[6].length != after[6].length ||
        memcmp(before[6].data, after[6].data, (size_t)before[6].length) != 0) {
        if (wrote && append_literal(statement, " ") < 0) return -1;
        if (append_alter_column_prefix(statement, operation) < 0) return -1;
        if (after[6].length == 0) {
            if (append_literal(statement, " drop default;") < 0) return -1;
        }
        else if (append_literal(statement, " set default ") < 0 ||
                 wreath_pg_buffer_append(
                     statement, after[6].data, after[6].length) < 0 ||
                 append_literal(statement, ";") < 0) return -1;
        wrote = 1;
    }
    if (part_equals(before + 3, "1") != part_equals(after + 3, "1")) {
        if ((wrote && append_literal(statement, " ") < 0) ||
            append_alter_column_prefix(statement, operation) < 0 ||
            append_literal(
                statement,
                part_equals(after + 3, "1") ? " set not null;" : " drop not null;") < 0)
            return -1;
        wrote = 1;
    }
    return wrote ? 0 : 1;
}


static int
append_identifier_list(WreathPgBuffer *buffer, const WreathSqlPart *part)
{
    Py_ssize_t start = 0;
    if (part->length == 0) return -1;
    for (Py_ssize_t index = 0; index <= part->length; index++) {
        if (index == part->length || part->data[index] == ',') {
            if (index == start ||
                (start > 0 && append_literal(buffer, ", ") < 0) ||
                append_identifier(buffer, part->data + start, index - start) < 0)
                return -1;
            start = index + 1;
        }
    }
    return 0;
}


static int
render_index(WreathPgBuffer *statement, const WreathSqlOperation *operation)
{
    WreathSqlPart parts[2];
    if (split_value(
            operation->name, operation->name_length, ':', parts, 2) < 0 ||
        !part_equals(parts, "i")) return 1;
    if (append_literal(statement, "create index on ") < 0 ||
        append_qualified(statement, operation) < 0 ||
        append_literal(statement, " (") < 0 ||
        append_identifier_list(statement, parts + 1) < 0 ||
        append_literal(statement, ");") < 0) return -1;
    return 0;
}


static int
render_constraint(WreathPgBuffer *statement, const WreathSqlOperation *operation)
{
    WreathSqlPart parts[5];
    if (split_value(
            operation->name, operation->name_length, ':', parts, 5) < 0) return 1;
    if (append_literal(statement, "alter table ") < 0 ||
        append_qualified(statement, operation) < 0) return -1;
    if (part_equals(parts, "p")) {
        if (append_literal(statement, " add primary key (") < 0 ||
            append_identifier_list(statement, parts + 1) < 0 ||
            append_literal(statement, ");") < 0) return -1;
        return 0;
    }
    if (part_equals(parts, "u")) {
        if (append_literal(statement, " add unique (") < 0 ||
            append_identifier_list(statement, parts + 1) < 0 ||
            append_literal(statement, ");") < 0) return -1;
        return 0;
    }
    if (part_equals(parts, "f")) {
        if (append_literal(statement, " add foreign key (") < 0 ||
            append_identifier_list(statement, parts + 1) < 0 ||
            append_literal(statement, ") references ") < 0 ||
            append_identifier(statement, parts[2].data, parts[2].length) < 0 ||
            append_literal(statement, ".") < 0 ||
            append_identifier(statement, parts[3].data, parts[3].length) < 0 ||
            append_literal(statement, " (") < 0 ||
            append_identifier_list(statement, parts + 4) < 0 ||
            append_literal(statement, ");") < 0) return -1;
        return 0;
    }
    return 1;
}


static int
render_operation(
    const WreathSqlOperation *operation,
    WreathPgBuffer *statement,
    uint32_t *flags_out)
{
    int rendered;
    *flags_out = operation->action == OP_DROP ? SQL_FLAG_DESTRUCTIVE : 0;
    if (operation->action == OP_ADD && operation->kind == KIND_TABLE) {
        if (append_literal(statement, "create table ") < 0 ||
            append_qualified(statement, operation) < 0 ||
            append_literal(statement, " ();") < 0) return -1;
        return 0;
    }
    if (operation->action == OP_DROP && operation->kind == KIND_TABLE) {
        if (append_literal(statement, "drop table ") < 0 ||
            append_qualified(statement, operation) < 0 ||
            append_literal(statement, ";") < 0) return -1;
        return 0;
    }
    if (operation->action == OP_ADD && operation->kind == KIND_COLUMN) {
        if (append_literal(statement, "alter table ") < 0 ||
            append_qualified(statement, operation) < 0 ||
            append_literal(statement, " add column ") < 0 ||
            append_identifier(statement, operation->name, operation->name_length) < 0 ||
            append_literal(statement, " ") < 0) return -1;
        rendered = render_column_definition(
            statement, operation->after, operation->after_length);
        if (rendered != 0) return rendered;
        return append_literal(statement, ";");
    }
    if (operation->action == OP_DROP && operation->kind == KIND_COLUMN) {
        if (append_literal(statement, "alter table ") < 0 ||
            append_qualified(statement, operation) < 0 ||
            append_literal(statement, " drop column ") < 0 ||
            append_identifier(statement, operation->name, operation->name_length) < 0 ||
            append_literal(statement, ";") < 0) return -1;
        return 0;
    }
    if (operation->action == OP_ALTER && operation->kind == KIND_COLUMN)
        return render_column_change(statement, operation);
    if (operation->action == OP_ADD && operation->kind == KIND_CONSTRAINT)
        return render_constraint(statement, operation);
    if (operation->action == OP_ADD && operation->kind == KIND_INDEX)
        return render_index(statement, operation);
    *flags_out |= SQL_FLAG_MANUAL;
    return 1;
}


PyObject *
wreath_pg_migration_operations_from_plan(
    const unsigned char *plan, Py_ssize_t plan_length)
{
    WreathSqlOperation *operations = NULL;
    uint32_t count;
    PyObject *result = NULL;
    unsigned char *output;
    if (parse_plan(plan, plan_length, &operations, &count) < 0) return NULL;
    if (count > 1) {
        qsort(operations, count, sizeof(*operations), operation_canonical_compare);
        for (uint32_t index = 1; index < count; index++) {
            if (operation_canonical_compare(
                    operations + index - 1, operations + index) == 0) {
                PyErr_Format(
                    PyExc_ValueError,
                    "invalid WMP1 plan: operations %u and %u target the same object",
                    index, index + 1);
                goto done;
            }
        }
    }
    if (count > (uint32_t)((PY_SSIZE_T_MAX - 12) / 24)) {
        PyErr_SetString(PyExc_OverflowError, "operation tape is too large");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, 12 + (Py_ssize_t)count * 24);
    if (result == NULL) goto done;
    output = (unsigned char *)PyBytes_AS_STRING(result);
    memcpy(output, "WMO1", 4);
    output[4] = 1;
    output[5] = output[6] = output[7] = 0;
    output[8] = (unsigned char)count;
    output[9] = (unsigned char)(count >> 8);
    output[10] = (unsigned char)(count >> 16);
    output[11] = (unsigned char)(count >> 24);
    for (uint32_t index = 0; index < count; index++) {
        const WreathSqlOperation *operation = operations + index;
        unsigned char *record = output + 12 + (Py_ssize_t)index * 24;
        const uint64_t object_id = operation_object_id(operation);
        const uint32_t before = operation->before_length == 0 ? 0 :
            wreath_pg_migration_signature(operation->before, operation->before_length);
        const uint32_t after = operation->after_length == 0 ? 0 :
            wreath_pg_migration_signature(operation->after, operation->after_length);
        record[0] = (unsigned char)operation->action;
        record[1] = (unsigned char)(operation->action >> 8);
        record[2] = (unsigned char)(operation->action >> 16);
        record[3] = (unsigned char)(operation->action >> 24);
        record[4] = (unsigned char)operation->kind;
        record[5] = (unsigned char)(operation->kind >> 8);
        record[6] = (unsigned char)(operation->kind >> 16);
        record[7] = (unsigned char)(operation->kind >> 24);
        for (uint32_t byte = 0; byte < 8; byte++)
            record[8 + byte] = (unsigned char)(object_id >> (byte * 8));
        record[16] = (unsigned char)before;
        record[17] = (unsigned char)(before >> 8);
        record[18] = (unsigned char)(before >> 16);
        record[19] = (unsigned char)(before >> 24);
        record[20] = (unsigned char)after;
        record[21] = (unsigned char)(after >> 8);
        record[22] = (unsigned char)(after >> 16);
        record[23] = (unsigned char)(after >> 24);
    }
done:
    PyMem_Free(operations);
    return result;
}


PyObject *
wreath_pg_migration_render_sql(
    const unsigned char *plan, Py_ssize_t plan_length)
{
    WreathSqlOperation *operations = NULL;
    uint32_t count;
    WreathPgBuffer output = {0};
    PyObject *result = NULL;
    if (parse_plan(plan, plan_length, &operations, &count) < 0) return NULL;
    if (wreath_pg_buffer_append(&output, "WMS1", 4) < 0 ||
        append_u32_le(&output, 1) < 0 || append_u32_le(&output, count) < 0) goto done;
    for (uint32_t index = 0; index < count; index++) {
        WreathPgBuffer statement = {0};
        uint32_t flags = 0;
        int rendered = render_operation(operations + index, &statement, &flags);
        if (rendered < 0) {
            wreath_pg_buffer_clear(&statement);
            goto done;
        }
        if (rendered > 0) {
            flags |= SQL_FLAG_MANUAL;
            wreath_pg_buffer_clear(&statement);
        }
        if (statement.length > UINT32_MAX ||
            append_u32_le(&output, flags) < 0 ||
            append_u32_le(&output, (uint32_t)statement.length) < 0 ||
            wreath_pg_buffer_append(&output, statement.data, statement.length) < 0) {
            wreath_pg_buffer_clear(&statement);
            goto done;
        }
        wreath_pg_buffer_clear(&statement);
    }
    result = wreath_pg_buffer_finish(&output);

done:
    PyMem_Free(operations);
    wreath_pg_buffer_clear(&output);
    return result;
}


static PyObject *
migration_operations_from_plan(PyObject *module, PyObject *args)
{
    const unsigned char *plan;
    Py_ssize_t plan_length;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "y#:_migration_operations_from_plan", &plan, &plan_length)) return NULL;
    return wreath_pg_migration_operations_from_plan(plan, plan_length);
}


static PyObject *
migration_render_sql(PyObject *module, PyObject *args)
{
    const unsigned char *plan;
    Py_ssize_t plan_length;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "y#:_migration_render_sql", &plan, &plan_length)) return NULL;
    return wreath_pg_migration_render_sql(plan, plan_length);
}


static PyMethodDef migration_sql_methods[] = {
    {"_migration_operations_from_plan", migration_operations_from_plan, METH_VARARGS,
     PyDoc_STR("Derive the authoritative WMO1 operation tape from WMP1 in metal.")},
    {"_migration_render_sql", migration_render_sql, METH_VARARGS,
     PyDoc_STR("Derive one deterministic bounded WMS1 SQL statement tape from WMP1.")},
    {NULL, NULL, 0, NULL},
};


int
wreath_pg_migration_sql_init(PyObject *module)
{
    return PyModule_AddFunctions(module, migration_sql_methods);
}
