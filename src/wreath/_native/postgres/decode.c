#include "decode.h"

#include "codec.h"
#include "record.h"
#include "tape.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PG_BOOL 16
#define PG_BYTEA 17
#define PG_INT8 20
#define PG_INT2 21
#define PG_INT4 23
#define PG_TEXT 25
#define PG_FLOAT4 700
#define PG_FLOAT8 701
#define PG_VARCHAR 1043

PyTypeObject *WreathPgDecoderPlanType = NULL;

static PyObject *
decode_bool_binary(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    if (length != 1) {
        PyErr_SetString(PyExc_ValueError, "invalid binary bool");
        return NULL;
    }
    return PyBool_FromLong(data[0] != 0);
}

static PyObject *
decode_bool_text(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    return PyBool_FromLong(length == 1 && data[0] == 't');
}

static PyObject *
decode_integer_text(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    Py_ssize_t index = 0;
    int negative = 0;
    (void)format;
    (void)oid;
    if (length > 0 && (data[0] == '-' || data[0] == '+')) {
        negative = data[0] == '-';
        index = 1;
    }
    if (index < length && length - index <= 18) {
        long long value = 0;
        for (; index < length; index++) {
            unsigned int digit = (unsigned int)data[index] - '0';
            if (digit > 9) goto fallback;
            value = value * 10 + (long long)digit;
        }
        return PyLong_FromLongLong(negative ? -value : value);
    }

fallback:
    {
        PyObject *text = PyUnicode_DecodeASCII((const char *)data, length, "strict");
        PyObject *result;
        if (text == NULL) return NULL;
        result = PyLong_FromUnicodeObject(text, 10);
        Py_DECREF(text);
        return result;
    }
}

static PyObject *
decode_int2_binary(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    if (length != 2) {
        PyErr_SetString(PyExc_ValueError, "invalid binary integer length");
        return NULL;
    }
    return PyLong_FromLong((int16_t)(((uint16_t)data[0] << 8) | data[1]));
}

static PyObject *
decode_int4_binary(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    if (length != 4) {
        PyErr_SetString(PyExc_ValueError, "invalid binary integer length");
        return NULL;
    }
    return PyLong_FromLong((int32_t)(
        ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
        ((uint32_t)data[2] << 8) | data[3]
    ));
}

static PyObject *
decode_int8_binary(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    uint64_t value = 0;
    (void)format;
    (void)oid;
    if (length != 8) {
        PyErr_SetString(PyExc_ValueError, "invalid binary integer length");
        return NULL;
    }
    for (int i = 0; i < 8; i++) value = (value << 8) | data[i];
    return PyLong_FromLongLong((int64_t)value);
}

static PyObject *
decode_float_text(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    if (length > 0 && length < 64) {
        char buffer[64];
        char *end = NULL;
        double number;
        memcpy(buffer, data, (size_t)length);
        buffer[length] = '\0';
        number = PyOS_string_to_double(buffer, &end, NULL);
        if (number == -1.0 && PyErr_Occurred()) {
            if (!PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;
            PyErr_Clear();
        } else if (end == buffer + length) {
            return PyFloat_FromDouble(number);
        }
    }
    {
        PyObject *text = PyUnicode_DecodeASCII((const char *)data, length, "strict");
        PyObject *result;
        if (text == NULL) return NULL;
        result = PyFloat_FromString(text);
        Py_DECREF(text);
        return result;
    }
}

static PyObject *
decode_float_binary(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    if (oid == PG_FLOAT4 && length == 4)
        return PyFloat_FromDouble(PyFloat_Unpack4((const char *)data, 0));
    if (oid == PG_FLOAT8 && length == 8)
        return PyFloat_FromDouble(PyFloat_Unpack8((const char *)data, 0));
    PyErr_SetString(PyExc_ValueError, "invalid binary float length");
    return NULL;
}

static PyObject *
decode_text(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)format;
    (void)oid;
    return PyUnicode_DecodeUTF8((const char *)data, length, "strict");
}

static PyObject *
decode_bytea(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    (void)oid;
    /* Binary format is already raw bytes: copy once. */
    if (format == 1) return PyBytes_FromStringAndSize((const char *)data, length);
    if (length >= 2 && data[0] == '\\' && data[1] == 'x') {
        return wreath_pg_decode_hex_bytea(data + 2, length - 2);
    }
    /* Not hex-format text: preserve the existing pass-through. */
    return PyBytes_FromStringAndSize((const char *)data, length);
}

static PyObject *
decode_fallback(const unsigned char *data, Py_ssize_t length, int format, uint32_t oid)
{
    PyObject *wire = PyBytes_FromStringAndSize((const char *)data, length);
    PyObject *result;
    if (wire == NULL) return NULL;
    result = wreath_pg_decode_value(oid, format, wire);
    Py_DECREF(wire);
    return result;
}

WreathPgRawDecoder
wreath_pg_select_decoder(uint32_t oid, int format)
{
    switch (oid) {
    case PG_BOOL:
        return format == 1 ? decode_bool_binary : decode_bool_text;
    case PG_INT2:
        return format == 1 ? decode_int2_binary : decode_integer_text;
    case PG_INT4:
        return format == 1 ? decode_int4_binary : decode_integer_text;
    case PG_INT8:
        return format == 1 ? decode_int8_binary : decode_integer_text;
    case PG_FLOAT4:
    case PG_FLOAT8:
        return format == 1 ? decode_float_binary : decode_float_text;
    case PG_TEXT:
    case PG_VARCHAR:
        return decode_text;
    case PG_BYTEA:
        return decode_bytea;
    default:
        return decode_fallback;
    }
}

static int
decoder_plan_traverse(WreathPgDecoderPlan *self, visitproc visit, void *arg)
{
    Py_VISIT(self->names);
    Py_VISIT(self->name_index);
    return 0;
}

static int
decoder_plan_clear(WreathPgDecoderPlan *self)
{
    Py_CLEAR(self->names);
    Py_CLEAR(self->name_index);
    return 0;
}

static void
decoder_plan_dealloc(WreathPgDecoderPlan *self)
{
    PyObject_GC_UnTrack(self);
    decoder_plan_clear(self);
    PyMem_Free(self->columns);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

PyObject *
wreath_pg_decoder_plan_new(PyObject *oids, PyObject *formats, PyObject *names)
{
    WreathPgDecoderPlan *self;
    Py_ssize_t count;
    if (!PyTuple_Check(oids) || !PyTuple_Check(formats) || !PyTuple_Check(names)) {
        PyErr_SetString(PyExc_TypeError, "decoder plan fields must be tuples");
        return NULL;
    }
    count = PyTuple_GET_SIZE(oids);
    if (count != PyTuple_GET_SIZE(formats) || count != PyTuple_GET_SIZE(names) ||
        count > UINT16_MAX) {
        PyErr_SetString(PyExc_ValueError, "decoder plan column counts differ");
        return NULL;
    }
    self = (WreathPgDecoderPlan *)WreathPgDecoderPlanType->tp_alloc(
        WreathPgDecoderPlanType, 0
    );
    if (self == NULL) return NULL;
    self->columns = PyMem_Calloc((size_t)count, sizeof(WreathPgColumnDecoder));
    self->name_index = PyDict_New();
    self->names = Py_NewRef(names);
    self->column_count = count;
    if ((self->columns == NULL && count > 0) || self->name_index == NULL) {
        Py_DECREF(self);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        unsigned long oid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(oids, i));
        long format = PyLong_AsLong(PyTuple_GET_ITEM(formats, i));
        PyObject *position;
        if ((oid == (unsigned long)-1 || format == -1) && PyErr_Occurred()) {
            Py_DECREF(self);
            return NULL;
        }
        if (format != 0 && format != 1) {
            Py_DECREF(self);
            PyErr_SetString(PyExc_ValueError, "invalid decoder wire format");
            return NULL;
        }
        self->columns[i].oid = (uint32_t)oid;
        self->columns[i].format = (uint16_t)format;
        self->columns[i].destination = (uint16_t)i;
        self->columns[i].decoder = wreath_pg_select_decoder((uint32_t)oid, (int)format);
        self->decoder_selections++;
        position = PyLong_FromSsize_t(i);
        if (position == NULL || PyDict_SetItem(
                self->name_index, PyTuple_GET_ITEM(names, i), position) < 0) {
            Py_XDECREF(position);
            Py_DECREF(self);
            return NULL;
        }
        Py_DECREF(position);
    }
    return (PyObject *)self;
}

static PyObject *
compile_decoder_plan(PyObject *module, PyObject *args)
{
    PyObject *oids;
    PyObject *formats;
    PyObject *names;
    (void)module;
    if (!PyArg_ParseTuple(
            args, "OOO:_compile_decoder_plan", &oids, &formats, &names)) return NULL;
    return wreath_pg_decoder_plan_new(oids, formats, names);
}

static PyObject *
plan_column_count(WreathPgDecoderPlan *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->column_count);
}

static PyObject *
plan_selection_count(WreathPgDecoderPlan *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->decoder_selections);
}

static PyGetSetDef plan_getset[] = {
    {"column_count", (getter)plan_column_count, NULL, NULL, NULL},
    {"decoder_selections", (getter)plan_selection_count, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

static PyType_Slot plan_slots[] = {
    {Py_tp_dealloc, decoder_plan_dealloc},
    {Py_tp_traverse, decoder_plan_traverse},
    {Py_tp_clear, decoder_plan_clear},
    {Py_tp_getset, plan_getset},
    {0, NULL}
};

static PyType_Spec plan_spec = {
    .name = "wreath._native._postgres._DecoderPlan",
    .basicsize = sizeof(WreathPgDecoderPlan),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = plan_slots
};

static PyObject *
decode_ref(WreathPgDecoderPlan *plan, WreathPgFieldTape *tape,
           Py_ssize_t row, Py_ssize_t column, Py_buffer *buffers)
{
    WreathPgFieldRef *field = wreath_pg_tape_ref(tape, row, column);
    WreathPgColumnDecoder *decoder = &plan->columns[column];
    const unsigned char *data;
    Py_ssize_t slot;
    if (field->length == -1) return Py_NewRef(Py_None);
    /* `buffers` covers the live owner window, so a stored index is offset by
     * the tape's owner head. Anything below it names a consumed owner. */
    if ((Py_ssize_t)field->slab_index < tape->owner_head ||
        field->slab_index >= (uint32_t)PyList_GET_SIZE(tape->owners)) {
        PyErr_SetString(PyExc_RuntimeError, "field tape owner index is invalid");
        return NULL;
    }
    slot = (Py_ssize_t)field->slab_index - tape->owner_head;
    if ((uint64_t)field->offset + (uint64_t)field->length >
        (uint64_t)buffers[slot].len) {
        PyErr_SetString(PyExc_RuntimeError, "field tape range is invalid");
        return NULL;
    }
    data = (const unsigned char *)buffers[slot].buf + field->offset;
    return decoder->decoder(data, field->length, decoder->format, decoder->oid);
}

Py_buffer *
wreath_pg_acquire_owner_buffers(WreathPgFieldTape *tape, Py_ssize_t rows,
                             Py_ssize_t *owner_limit_out)
{
    Py_ssize_t refs_span = rows * tape->stored_columns;
    /* Owner indexes are monotonic, so the last field of the last row names the
     * highest owner these rows touch. Only the live window [owner_head, limit)
     * is acquired: consumed owners may still be physically present. */
    Py_ssize_t owner_limit = refs_span > 0
        ? (Py_ssize_t)wreath_pg_tape_ref(tape, rows - 1, tape->stored_columns - 1)
              ->slab_index + 1
        : tape->owner_head;
    Py_ssize_t count = owner_limit - tape->owner_head;
    Py_buffer *buffers;
    if (owner_limit > PyList_GET_SIZE(tape->owners) || count < 0) {
        PyErr_SetString(PyExc_RuntimeError, "field tape owner index is invalid");
        return NULL;
    }
    buffers = PyMem_Calloc((size_t)(count > 0 ? count : 1), sizeof(Py_buffer));
    if (buffers == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        if (PyObject_GetBuffer(
                PyList_GET_ITEM(tape->owners, tape->owner_head + i), &buffers[i],
                PyBUF_CONTIG_RO) < 0) {
            for (Py_ssize_t j = 0; j < i; j++) PyBuffer_Release(&buffers[j]);
            PyMem_Free(buffers);
            return NULL;
        }
    }
    *owner_limit_out = count;
    return buffers;
}

void
wreath_pg_release_owner_buffers(Py_buffer *buffers, Py_ssize_t owner_limit)
{
    for (Py_ssize_t i = 0; i < owner_limit; i++) {
        if (buffers[i].obj != NULL) PyBuffer_Release(&buffers[i]);
    }
    PyMem_Free(buffers);
}

/* Decode `rows` rows into private records, then append them to `dest`.
   Records only become visible once fully populated, so a decoder that runs
   Python code (UUID construction, codec fallbacks) can never observe or
   disturb a half-built row. */
static int
fetch_into(WreathPgDecoderPlan *plan, WreathPgFieldTape *tape, Py_ssize_t rows,
           Py_buffer *buffers, PyObject *dest)
{
    Py_ssize_t columns = tape->stored_columns;
    PyObject **records;
    int failed = -1;

    records = PyMem_Calloc((size_t)(rows > 0 ? rows : 1), sizeof(PyObject *));
    if (records == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t row = 0; row < rows; row++) {
        records[row] = wreath_pg_record_alloc(plan->names, plan->name_index, columns);
        if (records[row] == NULL) goto done;
    }
    for (Py_ssize_t column = 0; column < columns; column++) {
        WreathPgColumnDecoder *decoder = &plan->columns[column];
        for (Py_ssize_t row = 0; row < rows; row++) {
            WreathPgFieldRef *field = wreath_pg_tape_ref(tape, row, column);
            PyObject *value;
            if (field->length == -1) {
                value = Py_NewRef(Py_None);
            } else {
                /* buffers[] covers the live owner window; see decode_ref. */
                const unsigned char *data =
                    (const unsigned char *)
                        buffers[(Py_ssize_t)field->slab_index - tape->owner_head].buf
                    + field->offset;
                value = decoder->decoder(
                    data, field->length, decoder->format, decoder->oid
                );
            }
            if (value == NULL) goto done;
            wreath_pg_record_set_value(records[row], decoder->destination, value);
        }
    }
    {
        Py_ssize_t appended = 0;
        for (; appended < rows; appended++) {
            if (PyList_Append(dest, records[appended]) < 0) break;
        }
        if (appended < rows) {
            PyList_SetSlice(
                dest, PyList_GET_SIZE(dest) - appended, PyList_GET_SIZE(dest), NULL
            );
            goto done;
        }
    }
    if (wreath_pg_tape_consume(tape, rows) < 0) {
        Py_ssize_t end = PyList_GET_SIZE(dest);
        PyList_SetSlice(dest, end - rows, end, NULL);
        goto done;
    }
    failed = 0;

done:
    for (Py_ssize_t row = 0; row < rows; row++) Py_XDECREF(records[row]);
    PyMem_Free(records);
    return failed;
}

int
wreath_pg_decode_fetch_extend(PyObject *plan_object, PyObject *tape_object,
                           Py_ssize_t limit, PyObject *dest)
{
    WreathPgDecoderPlan *plan = (WreathPgDecoderPlan *)plan_object;
    WreathPgFieldTape *tape = (WreathPgFieldTape *)tape_object;
    Py_ssize_t rows;
    Py_ssize_t owner_limit = 0;
    Py_buffer *buffers;
    int result;

    if (!PyObject_TypeCheck(plan_object, WreathPgDecoderPlanType) ||
        !PyObject_TypeCheck(tape_object, WreathPgFieldTapeType) ||
        !PyList_Check(dest) || limit <= 0 ||
        tape->stored_columns > plan->column_count) {
        PyErr_SetString(PyExc_ValueError, "invalid field tape decode request");
        return -1;
    }
    rows = tape->row_count < limit ? tape->row_count : limit;
    if (rows == 0) return 0;
    buffers = wreath_pg_acquire_owner_buffers(tape, rows, &owner_limit);
    if (buffers == NULL) return -1;
    result = fetch_into(plan, tape, rows, buffers, dest);
    wreath_pg_release_owner_buffers(buffers, owner_limit);
    return result;
}

static PyObject *
decode_field_tape(PyObject *module, PyObject *args)
{
    WreathPgDecoderPlan *plan;
    WreathPgFieldTape *tape;
    const char *mode;
    Py_ssize_t limit;
    Py_ssize_t rows;
    Py_ssize_t owner_limit = 0;
    Py_buffer *buffers = NULL;
    PyObject *result = NULL;
    (void)module;

    if (!PyArg_ParseTuple(
            args, "O!O!sn:_decode_field_tape",
            WreathPgDecoderPlanType, &plan,
            WreathPgFieldTapeType, &tape,
            &mode, &limit)) return NULL;
    if (limit <= 0 || tape->stored_columns > plan->column_count) {
        PyErr_SetString(PyExc_ValueError, "invalid field tape decode request");
        return NULL;
    }
    rows = tape->row_count < limit ? tape->row_count : limit;
    buffers = wreath_pg_acquire_owner_buffers(tape, rows, &owner_limit);
    if (buffers == NULL) return NULL;

    if (strcmp(mode, "fetchval") == 0) {
        result = rows == 0 ? Py_NewRef(Py_None) : decode_ref(plan, tape, 0, 0, buffers);
        if (result != NULL && wreath_pg_tape_consume(tape, tape->row_count) < 0)
            Py_CLEAR(result);
        goto done;
    }
    if (strcmp(mode, "fetchrow") == 0) {
        if (rows == 0) {
            result = Py_NewRef(Py_None);
            goto done;
        }
        result = wreath_pg_record_alloc(
            plan->names, plan->name_index, tape->stored_columns
        );
        if (result == NULL) goto done;
        for (Py_ssize_t column = 0; column < tape->stored_columns; column++) {
            PyObject *value = decode_ref(plan, tape, 0, column, buffers);
            if (value == NULL) {
                Py_CLEAR(result);
                goto done;
            }
            wreath_pg_record_set_value(result, column, value);
        }
        if (wreath_pg_tape_consume(tape, tape->row_count) < 0) Py_CLEAR(result);
        goto done;
    }
    if (strcmp(mode, "fetch") != 0) {
        PyErr_SetString(PyExc_ValueError, "unsupported field tape result mode");
        goto done;
    }

    result = PyList_New(0);
    if (result != NULL && fetch_into(plan, tape, rows, buffers, result) < 0)
        Py_CLEAR(result);

done:
    wreath_pg_release_owner_buffers(buffers, owner_limit);
    return result;
}

static PyMethodDef decode_methods[] = {
    {"_compile_decoder_plan", compile_decoder_plan, METH_VARARGS, NULL},
    {"_decode_field_tape", decode_field_tape, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL}
};

int
wreath_pg_decode_init(PyObject *module)
{
    int result;
    WreathPgDecoderPlanType = (PyTypeObject *)PyType_FromSpec(&plan_spec);
    if (WreathPgDecoderPlanType == NULL) return -1;
    result = PyModule_AddObjectRef(
        module, "_DecoderPlan", (PyObject *)WreathPgDecoderPlanType
    );
    if (result == 0) result = PyModule_AddFunctions(module, decode_methods);
    if (result == 0) Py_DECREF(WreathPgDecoderPlanType);
    return result;
}
