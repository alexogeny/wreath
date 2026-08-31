/* RFC 8941 fields used by HTTP Message Signatures. */
#include "wreathcore.h"
#include "bytes_writer.h"

#include <ctype.h>
#include <string.h>

typedef struct {
    const char *text;
    Py_ssize_t length;
    Py_ssize_t index;
    PyObject *error_type;
    Py_ssize_t max_components;
} SignatureParser;

typedef struct {
    PyObject *name;
    PyObject *params;
    PyObject *identifier;
} SignaturePlanComponent;

typedef struct {
    SignaturePlanComponent *components;
    Py_ssize_t component_count;
    PyObject *params;
    PyObject *raw_signature;
    PyObject *covered_order;
    PyObject *serialized_params;
} SignaturePlan;

typedef struct {
    PyObject *key;
    SignaturePlanComponent *components;
    Py_ssize_t component_count;
    PyObject *params;
} SignatureInputEntry;

typedef struct {
    PyObject *key;
    PyObject *value;
} SignatureValueEntry;

#define SIGNATURE_PLAN_CAPSULE "wreath.signature.plan"

static PyObject *
signature_error(SignatureParser *parser, const char *message)
{
    PyErr_SetString(parser->error_type, message);
    return NULL;
}

static void
signature_skip_ws(SignatureParser *parser)
{
    while (parser->index < parser->length &&
           (parser->text[parser->index] == ' ' || parser->text[parser->index] == '\t'))
        parser->index++;
}

static int
signature_token_char(unsigned char value)
{
    return isalnum(value) || strchr("_-.:%*/@", value) != NULL;
}

static PyObject *
signature_parse_token(SignatureParser *parser)
{
    Py_ssize_t start = parser->index;
    while (parser->index < parser->length &&
           signature_token_char((unsigned char)parser->text[parser->index]))
        parser->index++;
    if (parser->index == start)
        return signature_error(parser, "structured field: expected a token");
    return PyUnicode_DecodeUTF8(parser->text + start, parser->index - start, "strict");
}

static PyObject *
signature_parse_string_value(SignatureParser *parser)
{
    Py_ssize_t written = 0;
    char *output;
    PyObject *value;
    if (parser->index >= parser->length || parser->text[parser->index] != '"')
        return signature_error(parser, "structured field: expected a quoted string");
    parser->index++;
    output = PyMem_Malloc((size_t)(parser->length - parser->index + 1));
    if (output == NULL) return PyErr_NoMemory();
    while (parser->index < parser->length) {
        unsigned char current = (unsigned char)parser->text[parser->index++];
        if (current == '\\') {
            if (parser->index >= parser->length ||
                (parser->text[parser->index] != '"' &&
                 parser->text[parser->index] != '\\')) {
                PyMem_Free(output);
                return signature_error(parser, "structured field: bad escape");
            }
            output[written++] = parser->text[parser->index++];
            continue;
        }
        if (current == '"') {
            value = PyUnicode_DecodeASCII(output, written, "strict");
            PyMem_Free(output);
            return value;
        }
        if (current < 0x20 || current > 0x7e) {
            PyMem_Free(output);
            return signature_error(parser,
                                   "structured field: bad character in string");
        }
        output[written++] = (char)current;
    }
    PyMem_Free(output);
    return signature_error(parser, "structured field: unterminated string");
}

static int
signature_b64_value(unsigned char value)
{
    if (value >= 'A' && value <= 'Z') return value - 'A';
    if (value >= 'a' && value <= 'z') return value - 'a' + 26;
    if (value >= '0' && value <= '9') return value - '0' + 52;
    if (value == '+') return 62;
    if (value == '/') return 63;
    return -1;
}

static PyObject *
signature_decode_base64(SignatureParser *parser, const char *input, Py_ssize_t length)
{
    Py_ssize_t padding = 0, output_length, written = 0;
    PyObject *result;
    unsigned char *output;
    if (length == 0 || (length & 3) != 0)
        return signature_error(parser, "structured field: bad base64");
    if (input[length - 1] == '=') padding++;
    if (input[length - 2] == '=') padding++;
    output_length = length / 4 * 3 - padding;
    result = PyBytes_FromStringAndSize(NULL, output_length);
    if (result == NULL) return NULL;
    output = (unsigned char *)PyBytes_AS_STRING(result);
    for (Py_ssize_t index = 0; index < length; index += 4) {
        int a = signature_b64_value((unsigned char)input[index]);
        int b = signature_b64_value((unsigned char)input[index + 1]);
        int c = input[index + 2] == '=' ? 0 :
                signature_b64_value((unsigned char)input[index + 2]);
        int d = input[index + 3] == '=' ? 0 :
                signature_b64_value((unsigned char)input[index + 3]);
        int last = index + 4 == length;
        if (a < 0 || b < 0 || c < 0 || d < 0 ||
            (!last && (input[index + 2] == '=' || input[index + 3] == '=')) ||
            (input[index + 2] == '=' && input[index + 3] != '=') ||
            (input[index + 2] == '=' && (b & 15) != 0) ||
            (input[index + 3] == '=' && input[index + 2] != '=' && (c & 3) != 0)) {
            Py_DECREF(result);
            return signature_error(parser, "structured field: bad base64");
        }
        if (written < output_length)
            output[written++] = (unsigned char)((a << 2) | (b >> 4));
        if (written < output_length)
            output[written++] = (unsigned char)((b << 4) | (c >> 2));
        if (written < output_length)
            output[written++] = (unsigned char)((c << 6) | d);
    }
    return result;
}

static PyObject *
signature_parse_item(SignatureParser *parser)
{
    unsigned char current;
    if (parser->index >= parser->length)
        return signature_error(parser, "structured field: expected an item");
    current = (unsigned char)parser->text[parser->index];
    if (current == '"') return signature_parse_string_value(parser);
    if (current == ':') {
        Py_ssize_t start = ++parser->index;
        PyObject *value;
        while (parser->index < parser->length && parser->text[parser->index] != ':')
            parser->index++;
        if (parser->index >= parser->length)
            return signature_error(parser,
                                   "structured field: unterminated byte sequence");
        value = signature_decode_base64(parser, parser->text + start,
                                        parser->index - start);
        parser->index++;
        return value;
    }
    if (current == '?') {
        if (parser->index + 1 >= parser->length ||
            (parser->text[parser->index + 1] != '0' &&
             parser->text[parser->index + 1] != '1'))
            return signature_error(parser, "structured field: bad boolean");
        current = (unsigned char)parser->text[parser->index + 1];
        parser->index += 2;
        return PyBool_FromLong(current == '1');
    }
    if (current == '-' || isdigit(current)) {
        Py_ssize_t start = parser->index++;
        int decimal = 0;
        PyObject *raw, *value;
        while (parser->index < parser->length) {
            current = (unsigned char)parser->text[parser->index];
            if (isdigit(current)) parser->index++;
            else if (current == '.') { decimal = 1; parser->index++; }
            else break;
        }
        raw = PyUnicode_DecodeASCII(parser->text + start,
                                    parser->index - start, "strict");
        if (raw == NULL) return NULL;
        value = decimal ? PyFloat_FromString(raw) :
                PyLong_FromUnicodeObject(raw, 10);
        Py_DECREF(raw);
        if (value == NULL) {
            PyErr_Clear();
            return signature_error(parser, "structured field: bad number");
        }
        return value;
    }
    return signature_parse_token(parser);
}

static PyObject *
signature_parse_params(SignatureParser *parser)
{
    PyObject *params = PyDict_New();
    if (params == NULL) return NULL;
    while (parser->index < parser->length && parser->text[parser->index] == ';') {
        PyObject *key, *value;
        parser->index++;
        signature_skip_ws(parser);
        key = signature_parse_token(parser);
        if (key == NULL) { Py_DECREF(params); return NULL; }
        if (parser->index < parser->length && parser->text[parser->index] == '=') {
            parser->index++;
            value = signature_parse_item(parser);
        }
        else value = Py_NewRef(Py_True);
        if (value == NULL || PyDict_SetItem(params, key, value) < 0) {
            Py_XDECREF(value); Py_DECREF(key); Py_DECREF(params); return NULL;
        }
        Py_DECREF(value); Py_DECREF(key);
        signature_skip_ws(parser);
    }
    return params;
}

static PyObject *
signature_parse_inner(SignatureParser *parser)
{
    PyObject *items;
    if (parser->index >= parser->length || parser->text[parser->index] != '(')
        return signature_error(parser, "structured field: expected an inner list");
    parser->index++;
    signature_skip_ws(parser);
    items = PyList_New(0);
    if (items == NULL) return NULL;
    while (parser->index < parser->length && parser->text[parser->index] != ')') {
        PyObject *value, *params, *pair;
        if (PyList_GET_SIZE(items) >= parser->max_components) {
            Py_DECREF(items);
            return signature_error(parser, "signature covers too many components");
        }
        value = signature_parse_string_value(parser);
        if (value == NULL) { Py_DECREF(items); return NULL; }
        params = signature_parse_params(parser);
        if (params == NULL) { Py_DECREF(value); Py_DECREF(items); return NULL; }
        pair = wreath_tuple2_from_owned(value, params);
        if (pair == NULL || PyList_Append(items, pair) < 0) {
            Py_XDECREF(pair); Py_DECREF(items); return NULL;
        }
        Py_DECREF(pair);
        signature_skip_ws(parser);
    }
    if (parser->index >= parser->length) {
        Py_DECREF(items);
        return signature_error(parser, "structured field: unterminated inner list");
    }
    parser->index++;
    PyObject *result = PyList_AsTuple(items);
    Py_DECREF(items);
    return result;
}

PyObject *
wreath_signature_parse_dictionary(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *text_object, *error_type, *output;
    int inner_list;
    Py_ssize_t max_header, max_components;
    SignatureParser parser;
    if (!PyArg_ParseTuple(args, "OpOnn:signature_parse_dictionary", &text_object,
                          &inner_list, &error_type, &max_header, &max_components))
        return NULL;
    parser.text = PyUnicode_AsUTF8AndSize(text_object, &parser.length);
    if (parser.text == NULL) return NULL;
    parser.index = 0;
    parser.error_type = error_type;
    parser.max_components = max_components;
    if (parser.length > max_header)
        return signature_error(&parser, "signature header is too large");
    output = PyDict_New();
    if (output == NULL) return NULL;
    signature_skip_ws(&parser);
    while (parser.index < parser.length) {
        PyObject *key = signature_parse_token(&parser);
        PyObject *value = NULL, *params = NULL, *entry = NULL;
        if (key == NULL) goto error;
        if (parser.index < parser.length && parser.text[parser.index] == '=') {
            parser.index++;
            value = inner_list ? signature_parse_inner(&parser) :
                    signature_parse_item(&parser);
            if (value == NULL) goto item_error;
            params = signature_parse_params(&parser);
        }
        else {
            params = signature_parse_params(&parser);
            value = inner_list ? PyTuple_New(0) : Py_NewRef(Py_True);
        }
        if (params == NULL || value == NULL) goto item_error;
        entry = wreath_tuple2_from_owned(value, params);
        value = NULL;
        params = NULL;
        if (entry == NULL || PyDict_SetItem(output, key, entry) < 0) goto item_error;
        Py_DECREF(entry); Py_DECREF(key);
        signature_skip_ws(&parser);
        if (parser.index < parser.length) {
            if (parser.text[parser.index] != ',') {
                Py_DECREF(output);
                return signature_error(&parser, "structured field: expected a comma");
            }
            parser.index++;
            signature_skip_ws(&parser);
            if (parser.index >= parser.length) {
                Py_DECREF(output);
                return signature_error(&parser, "structured field: trailing comma");
            }
        }
        continue;
item_error:
        Py_XDECREF(entry); Py_XDECREF(params); Py_XDECREF(value); Py_DECREF(key);
error:
        Py_DECREF(output);
        return NULL;
    }
    return output;
}

PyObject *
wreath_signature_parse_string(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *text_object, *error_type, *value;
    Py_ssize_t index;
    SignatureParser parser;
    if (!PyArg_ParseTuple(args, "OnO:signature_parse_string", &text_object,
                          &index, &error_type)) return NULL;
    parser.text = PyUnicode_AsUTF8AndSize(text_object, &parser.length);
    if (parser.text == NULL) return NULL;
    parser.index = index;
    parser.error_type = error_type;
    parser.max_components = PY_SSIZE_T_MAX;
    value = signature_parse_string_value(&parser);
    if (value == NULL) return NULL;
    return wreath_tuple2_from_owned(value, PyLong_FromSsize_t(parser.index));
}

static int
signature_raise(PyObject *error_type, const char *message)
{
    PyErr_SetString(error_type, message);
    return -1;
}

static int
signature_write_unicode(WreathBytesWriter *writer, PyObject *value)
{
    Py_ssize_t length;
    const char *data = PyUnicode_AsUTF8AndSize(value, &length);
    if (data == NULL) return -1;
    return wreath_writer_write(writer, data, length);
}

static int
signature_write_quoted(WreathBytesWriter *writer, PyObject *value)
{
    Py_ssize_t length;
    const char *data = PyUnicode_AsUTF8AndSize(value, &length);
    Py_ssize_t start = 0;
    if (data == NULL || wreath_writer_byte(writer, '"') < 0) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        if (data[index] != '\\' && data[index] != '"') continue;
        if (wreath_writer_write(writer, data + start, index - start) < 0 ||
            wreath_writer_byte(writer, '\\') < 0) return -1;
        start = index;
    }
    return wreath_writer_write(writer, data + start, length - start) < 0 ? -1 :
        wreath_writer_byte(writer, '"');
}

static int
signature_write_base64(WreathBytesWriter *writer, PyObject *value)
{
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const unsigned char *data = (const unsigned char *)PyBytes_AS_STRING(value);
    Py_ssize_t length = PyBytes_GET_SIZE(value);
    Py_ssize_t index = 0;
    if (wreath_writer_byte(writer, ':') < 0) return -1;
    while (index + 3 <= length) {
        unsigned int bits = ((unsigned int)data[index] << 16) |
                            ((unsigned int)data[index + 1] << 8) |
                            data[index + 2];
        char encoded[4] = {
            alphabet[(bits >> 18) & 63], alphabet[(bits >> 12) & 63],
            alphabet[(bits >> 6) & 63], alphabet[bits & 63],
        };
        if (wreath_writer_write(writer, encoded, 4) < 0) return -1;
        index += 3;
    }
    if (index < length) {
        unsigned int bits = (unsigned int)data[index] << 16;
        char encoded[4];
        if (index + 1 < length) bits |= (unsigned int)data[index + 1] << 8;
        encoded[0] = alphabet[(bits >> 18) & 63];
        encoded[1] = alphabet[(bits >> 12) & 63];
        encoded[2] = index + 1 < length ? alphabet[(bits >> 6) & 63] : '=';
        encoded[3] = '=';
        if (wreath_writer_write(writer, encoded, 4) < 0) return -1;
    }
    return wreath_writer_byte(writer, ':');
}

static int
signature_write_bare(WreathBytesWriter *writer, PyObject *value,
                     PyObject *error_type)
{
    if (value == Py_True) return wreath_writer_write(writer, "?1", 2);
    if (value == Py_False) return wreath_writer_write(writer, "?0", 2);
    if (PyUnicode_Check(value)) return signature_write_quoted(writer, value);
    if (PyBytes_Check(value)) return signature_write_base64(writer, value);
    if (PyLong_Check(value)) {
        PyObject *text = PyObject_Str(value);
        int result = text == NULL ? -1 : signature_write_unicode(writer, text);
        Py_XDECREF(text);
        return result;
    }
    {
        PyObject *name = PyObject_GetAttrString((PyObject *)Py_TYPE(value), "__name__");
        PyObject *message = name == NULL ? NULL : PyUnicode_FromFormat(
            "cannot serialize parameter of type %U", name);
        Py_XDECREF(name);
        if (message == NULL) return -1;
        PyErr_SetObject(error_type, message);
        Py_DECREF(message);
        return -1;
    }
}

static PyObject *
signature_serialize_params(PyObject *params, PyObject *error_type)
{
    WreathBytesWriter writer = {0};
    if (wreath_writer_init(&writer, 64) < 0) return NULL;
    if (PyDict_CheckExact(params)) {
        Py_ssize_t position = 0;
        PyObject *key, *value;
        while (PyDict_Next(params, &position, &key, &value)) {
            PyObject *key_text = PyUnicode_CheckExact(key) ?
                Py_NewRef(key) : PyObject_Str(key);
            if (key_text == NULL || wreath_writer_byte(&writer, ';') < 0 ||
                signature_write_unicode(&writer, key_text) < 0 ||
                (value != Py_True &&
                 (wreath_writer_byte(&writer, '=') < 0 ||
                  signature_write_bare(&writer, value, error_type) < 0))) {
                Py_XDECREF(key_text);
                goto error;
            }
            Py_DECREF(key_text);
        }
    }
    else {
        PyObject *items = PyMapping_Items(params);
        PyObject *sequence;
        if (items == NULL) goto error;
        sequence = PySequence_Fast(items, "component parameters must be a mapping");
        Py_DECREF(items);
        if (sequence == NULL) goto error;
        for (Py_ssize_t index = 0;
             index < PySequence_Fast_GET_SIZE(sequence); index++) {
            PyObject *pair = PySequence_Fast(
                PySequence_Fast_GET_ITEM(sequence, index),
                "mapping items must be pairs");
            PyObject *key_text = NULL;
            PyObject *value;
            if (pair == NULL) { Py_DECREF(sequence); goto error; }
            if (PySequence_Fast_GET_SIZE(pair) != 2) {
                Py_DECREF(pair);
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_ValueError, "mapping items must be pairs");
                goto error;
            }
            key_text = PyObject_Str(PySequence_Fast_GET_ITEM(pair, 0));
            value = PySequence_Fast_GET_ITEM(pair, 1);
            if (key_text == NULL || wreath_writer_byte(&writer, ';') < 0 ||
                signature_write_unicode(&writer, key_text) < 0 ||
                (value != Py_True &&
                 (wreath_writer_byte(&writer, '=') < 0 ||
                  signature_write_bare(&writer, value, error_type) < 0))) {
                Py_XDECREF(key_text);
                Py_DECREF(pair);
                Py_DECREF(sequence);
                goto error;
            }
            Py_DECREF(key_text);
            Py_DECREF(pair);
        }
        Py_DECREF(sequence);
    }
    return wreath_writer_finish(&writer);
error:
    Py_XDECREF(writer.bytes);
    return NULL;
}

static PyObject *
signature_identifier(PyObject *name, PyObject *serialized_params)
{
    WreathBytesWriter writer = {0};
    if (wreath_writer_init(&writer, 64) < 0) return NULL;
    if (signature_write_quoted(&writer, name) < 0 ||
        wreath_writer_write(
            &writer, PyBytes_AS_STRING(serialized_params),
            PyBytes_GET_SIZE(serialized_params)) < 0) {
        Py_XDECREF(writer.bytes);
        return NULL;
    }
    return wreath_writer_finish(&writer);
}

static int
signature_component_params(PyObject *params, PyObject *error_type)
{
    PyObject *req = NULL;
    if (PyDict_CheckExact(params)) {
        Py_ssize_t probe = 0;
        PyObject *probe_key, *probe_value;
        while (PyDict_Next(params, &probe, &probe_key, &probe_value)) {
            if (!PyUnicode_CheckExact(probe_key)) goto generic_mapping;
        }
        Py_ssize_t position = 0;
        PyObject *key, *value;
        while (PyDict_Next(params, &position, &key, &value)) {
            int is_name = PyUnicode_CheckExact(key) &&
                PyUnicode_EqualToUTF8(key, "name");
            int is_req = PyUnicode_CheckExact(key) &&
                PyUnicode_EqualToUTF8(key, "req");
            if (!is_name && !is_req) {
                PyObject *representation = PyObject_Repr(key);
                PyObject *message;
                if (representation == NULL) return -1;
                message = PyUnicode_FromFormat(
                    "unsupported component parameter %U", representation);
                Py_DECREF(representation);
                if (message == NULL) return -1;
                PyErr_SetObject(error_type, message);
                Py_DECREF(message);
                return -1;
            }
            if (is_req) req = value;
        }
        if (req == NULL) return 0;
        int truth = PyObject_IsTrue(req);
        if (truth < 0) return -1;
        return truth ? signature_raise(
            error_type, "request-response binding is not supported") : 0;
    }
generic_mapping:
    PyObject *name_key = PyUnicode_FromString("name");
    PyObject *req_key = PyUnicode_FromString("req");
    PyObject *keys = NULL, *sequence = NULL;
    if (name_key == NULL || req_key == NULL) goto error;
    keys = PyMapping_Keys(params);
    if (keys == NULL) goto error;
    sequence = PySequence_Fast(keys, "component parameters must be a mapping");
    Py_DECREF(keys);
    keys = NULL;
    if (sequence == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(sequence); index++) {
        PyObject *key = PySequence_Fast_GET_ITEM(sequence, index);
        int is_name = PyObject_RichCompareBool(key, name_key, Py_EQ);
        int is_req = PyObject_RichCompareBool(key, req_key, Py_EQ);
        if (is_name < 0 || is_req < 0) goto error;
        if (!is_name && !is_req) {
            PyObject *representation = PyObject_Repr(key);
            PyObject *message;
            if (representation == NULL) goto error;
            message = PyUnicode_FromFormat("unsupported component parameter %U",
                                           representation);
            Py_DECREF(representation);
            if (message == NULL) goto error;
            PyErr_SetObject(error_type, message);
            Py_DECREF(message);
            goto error;
        }
    }
    Py_DECREF(sequence);
    sequence = NULL;
    {
        int found = PyMapping_GetOptionalItem(params, req_key, &req);
        int truth;
        if (found < 0) goto error;
        if (!found) {
            Py_DECREF(req_key);
            Py_DECREF(name_key);
            return 0;
        }
        truth = PyObject_IsTrue(req);
        Py_DECREF(req);
        if (truth < 0) goto error;
        if (truth) {
            signature_raise(error_type,
                            "request-response binding is not supported");
            goto error;
        }
    }
    Py_DECREF(req_key);
    Py_DECREF(name_key);
    return 0;
error:
    Py_XDECREF(sequence);
    Py_XDECREF(keys);
    Py_XDECREF(req_key);
    Py_XDECREF(name_key);
    return -1;
}

static void
signature_components_clear(SignaturePlanComponent *components, Py_ssize_t count)
{
    if (components == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_XDECREF(components[index].identifier);
        Py_XDECREF(components[index].params);
        Py_XDECREF(components[index].name);
    }
    PyMem_Free(components);
}

static void
signature_plan_clear(SignaturePlan *plan)
{
    if (plan == NULL) return;
    signature_components_clear(plan->components, plan->component_count);
    Py_XDECREF(plan->serialized_params);
    Py_XDECREF(plan->covered_order);
    Py_XDECREF(plan->raw_signature);
    Py_XDECREF(plan->params);
    PyMem_Free(plan);
}

static void
signature_plan_capsule_destructor(PyObject *capsule)
{
    SignaturePlan *plan = PyCapsule_GetPointer(capsule, SIGNATURE_PLAN_CAPSULE);
    if (plan != NULL) signature_plan_clear(plan);
    else PyErr_Clear();
}

static SignaturePlan *
signature_plan_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, SIGNATURE_PLAN_CAPSULE);
}

static int
signature_parse_plan_inner(SignatureParser *parser,
                           SignaturePlanComponent **components_out,
                           Py_ssize_t *count_out)
{
    SignaturePlanComponent *components = NULL;
    Py_ssize_t count = 0;
    if (parser->index >= parser->length || parser->text[parser->index] != '(') {
        signature_error(parser, "structured field: expected an inner list");
        return -1;
    }
    parser->index++;
    signature_skip_ws(parser);
    if (parser->max_components > 0) {
        components = PyMem_Calloc((size_t)parser->max_components,
                                  sizeof(*components));
        if (components == NULL) {
            PyErr_NoMemory();
            return -1;
        }
    }
    while (parser->index < parser->length && parser->text[parser->index] != ')') {
        PyObject *name, *params;
        if (count >= parser->max_components) {
            signature_components_clear(components, count);
            signature_error(parser, "signature covers too many components");
            return -1;
        }
        name = signature_parse_string_value(parser);
        if (name == NULL) {
            signature_components_clear(components, count);
            return -1;
        }
        params = signature_parse_params(parser);
        if (params == NULL) {
            Py_DECREF(name);
            signature_components_clear(components, count);
            return -1;
        }
        components[count].name = name;
        components[count].params = params;
        count++;
        signature_skip_ws(parser);
    }
    if (parser->index >= parser->length) {
        signature_components_clear(components, count);
        signature_error(parser, "structured field: unterminated inner list");
        return -1;
    }
    parser->index++;
    *components_out = components;
    *count_out = count;
    return 0;
}

static int
signature_input_entry_index(SignatureInputEntry *entries, Py_ssize_t count,
                            PyObject *key)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        int equal = PyObject_RichCompareBool(entries[index].key, key, Py_EQ);
        if (equal != 0) return equal < 0 ? -2 : (int)index;
    }
    return -1;
}

static int
signature_value_entry_index(SignatureValueEntry *entries, Py_ssize_t count,
                            PyObject *key)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        int equal = PyObject_RichCompareBool(entries[index].key, key, Py_EQ);
        if (equal != 0) return equal < 0 ? -2 : (int)index;
    }
    return -1;
}

static void
signature_input_entries_clear(SignatureInputEntry *entries, Py_ssize_t count)
{
    if (entries == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        signature_components_clear(entries[index].components,
                                   entries[index].component_count);
        Py_XDECREF(entries[index].params);
        Py_XDECREF(entries[index].key);
    }
    PyMem_Free(entries);
}

static void
signature_value_entries_clear(SignatureValueEntry *entries, Py_ssize_t count)
{
    if (entries == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_XDECREF(entries[index].value);
        Py_XDECREF(entries[index].key);
    }
    PyMem_Free(entries);
}

static int
signature_parse_input_entries(SignatureParser *parser,
                              SignatureInputEntry **entries_out,
                              Py_ssize_t *count_out)
{
    SignatureInputEntry *entries = NULL;
    Py_ssize_t count = 0, capacity = 0;
    signature_skip_ws(parser);
    while (parser->index < parser->length) {
        SignaturePlanComponent *components = NULL;
        Py_ssize_t component_count = 0;
        PyObject *key = signature_parse_token(parser);
        PyObject *params = NULL;
        int existing;
        if (key == NULL) goto error;
        if (parser->index < parser->length && parser->text[parser->index] == '=') {
            parser->index++;
            if (signature_parse_plan_inner(parser, &components,
                                           &component_count) < 0) {
                Py_DECREF(key);
                goto error;
            }
            params = signature_parse_params(parser);
        }
        else params = signature_parse_params(parser);
        if (params == NULL) {
            Py_DECREF(key);
            signature_components_clear(components, component_count);
            goto error;
        }
        existing = signature_input_entry_index(entries, count, key);
        if (existing == -2) {
            Py_DECREF(params);
            Py_DECREF(key);
            signature_components_clear(components, component_count);
            goto error;
        }
        if (existing >= 0) {
            signature_components_clear(entries[existing].components,
                                       entries[existing].component_count);
            Py_DECREF(entries[existing].params);
            Py_DECREF(key);
            entries[existing].components = components;
            entries[existing].component_count = component_count;
            entries[existing].params = params;
        }
        else {
            if (count == capacity) {
                Py_ssize_t next = capacity == 0 ? 4 : capacity * 2;
                SignatureInputEntry *grown = PyMem_Realloc(
                    entries, (size_t)next * sizeof(*entries));
                if (grown == NULL) {
                    Py_DECREF(params);
                    Py_DECREF(key);
                    signature_components_clear(components, component_count);
                    PyErr_NoMemory();
                    goto error;
                }
                entries = grown;
                capacity = next;
            }
            entries[count++] = (SignatureInputEntry){
                .key = key,
                .components = components,
                .component_count = component_count,
                .params = params,
            };
        }
        signature_skip_ws(parser);
        if (parser->index < parser->length) {
            if (parser->text[parser->index] != ',') {
                signature_error(parser, "structured field: expected a comma");
                goto error;
            }
            parser->index++;
            signature_skip_ws(parser);
            if (parser->index >= parser->length) {
                signature_error(parser, "structured field: trailing comma");
                goto error;
            }
        }
    }
    *entries_out = entries;
    *count_out = count;
    return 0;
error:
    signature_input_entries_clear(entries, count);
    return -1;
}

static int
signature_parse_value_entries(SignatureParser *parser,
                              SignatureValueEntry **entries_out,
                              Py_ssize_t *count_out)
{
    SignatureValueEntry *entries = NULL;
    Py_ssize_t count = 0, capacity = 0;
    signature_skip_ws(parser);
    while (parser->index < parser->length) {
        PyObject *key = signature_parse_token(parser);
        PyObject *value = NULL, *params = NULL;
        int existing;
        if (key == NULL) goto error;
        if (parser->index < parser->length && parser->text[parser->index] == '=') {
            parser->index++;
            value = signature_parse_item(parser);
            if (value != NULL) params = signature_parse_params(parser);
        }
        else {
            params = signature_parse_params(parser);
            value = Py_NewRef(Py_True);
        }
        if (params == NULL) {
            Py_XDECREF(value);
            Py_DECREF(key);
            goto error;
        }
        Py_XDECREF(params);
        if (value == NULL) {
            Py_DECREF(key);
            goto error;
        }
        existing = signature_value_entry_index(entries, count, key);
        if (existing == -2) {
            Py_DECREF(value);
            Py_DECREF(key);
            goto error;
        }
        if (existing >= 0) {
            Py_DECREF(entries[existing].value);
            Py_DECREF(key);
            entries[existing].value = value;
        }
        else {
            if (count == capacity) {
                Py_ssize_t next = capacity == 0 ? 4 : capacity * 2;
                SignatureValueEntry *grown = PyMem_Realloc(
                    entries, (size_t)next * sizeof(*entries));
                if (grown == NULL) {
                    Py_DECREF(value);
                    Py_DECREF(key);
                    PyErr_NoMemory();
                    goto error;
                }
                entries = grown;
                capacity = next;
            }
            entries[count++] = (SignatureValueEntry){.key = key, .value = value};
        }
        signature_skip_ws(parser);
        if (parser->index < parser->length) {
            if (parser->text[parser->index] != ',') {
                signature_error(parser, "structured field: expected a comma");
                goto error;
            }
            parser->index++;
            signature_skip_ws(parser);
            if (parser->index >= parser->length) {
                signature_error(parser, "structured field: trailing comma");
                goto error;
            }
        }
    }
    *entries_out = entries;
    *count_out = count;
    return 0;
error:
    signature_value_entries_clear(entries, count);
    return -1;
}

static int
signature_component_is_lowercase(PyObject *name)
{
    Py_ssize_t length;
    const char *text = PyUnicode_AsUTF8AndSize(name, &length);
    if (text == NULL) return -1;
    for (Py_ssize_t index = 0; index < length; index++)
        if (text[index] >= 'A' && text[index] <= 'Z') return 0;
    return 1;
}

PyObject *
wreath_signature_compile_pair(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *input_text, *signature_text, *error_type;
    Py_ssize_t max_header, max_components;
    SignatureParser input_parser, value_parser;
    SignatureInputEntry *inputs = NULL;
    SignatureValueEntry *values = NULL;
    Py_ssize_t input_count = 0, value_count = 0;
    SignaturePlan *plan = NULL;
    PyObject *seen = NULL, *capsule = NULL;
    Py_ssize_t chosen = -1, signature_index = -1;
    if (!PyArg_ParseTuple(args, "OOOnn:signature_compile_pair", &input_text,
                          &signature_text, &error_type, &max_header,
                          &max_components)) return NULL;
    input_parser.text = PyUnicode_AsUTF8AndSize(input_text, &input_parser.length);
    if (input_parser.text == NULL) return NULL;
    value_parser.text = PyUnicode_AsUTF8AndSize(signature_text,
                                               &value_parser.length);
    if (value_parser.text == NULL) return NULL;
    input_parser.index = value_parser.index = 0;
    input_parser.error_type = value_parser.error_type = error_type;
    input_parser.max_components = value_parser.max_components = max_components;
    if (input_parser.length > max_header || value_parser.length > max_header)
        return signature_error(&input_parser, "signature header is too large");
    if (signature_parse_input_entries(&input_parser, &inputs, &input_count) < 0 ||
        signature_parse_value_entries(&value_parser, &values, &value_count) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < input_count && chosen < 0; index++) {
        int match = signature_value_entry_index(values, value_count,
                                                inputs[index].key);
        if (match == -2) goto error;
        if (match >= 0) {
            chosen = index;
            signature_index = match;
        }
    }
    if (chosen < 0) {
        signature_raise(error_type,
                        "no label appears in both signature headers");
        goto error;
    }
    if (inputs[chosen].component_count == 0) {
        signature_raise(error_type, "signature covers no components");
        goto error;
    }
    plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    plan->components = inputs[chosen].components;
    plan->component_count = inputs[chosen].component_count;
    plan->params = inputs[chosen].params;
    inputs[chosen].components = NULL;
    inputs[chosen].component_count = 0;
    inputs[chosen].params = NULL;
    plan->raw_signature = Py_NewRef(values[signature_index].value);
    plan->covered_order = PyTuple_New(plan->component_count);
    seen = PySet_New(NULL);
    if (plan->covered_order == NULL || seen == NULL) goto error;
    for (Py_ssize_t index = 0; index < plan->component_count; index++) {
        SignaturePlanComponent *component = &plan->components[index];
        PyObject *serialized;
        int lower = signature_component_is_lowercase(component->name);
        if (lower < 0) goto error;
        if (!lower) {
            signature_raise(error_type,
                            "component identifiers must be lowercase");
            goto error;
        }
        if (signature_component_params(component->params, error_type) < 0)
            goto error;
        serialized = signature_serialize_params(component->params, error_type);
        if (serialized == NULL) goto error;
        component->identifier = signature_identifier(component->name, serialized);
        Py_DECREF(serialized);
        if (component->identifier == NULL) goto error;
        int duplicate = PySet_Contains(seen, component->identifier);
        if (duplicate < 0) goto error;
        if (duplicate) {
            PyObject *representation = PyObject_Repr(component->name);
            PyObject *message = representation == NULL ? NULL :
                PyUnicode_FromFormat("component %U is covered twice",
                                     representation);
            Py_XDECREF(representation);
            if (message == NULL) goto error;
            PyErr_SetObject(error_type, message);
            Py_DECREF(message);
            goto error;
        }
        if (PySet_Add(seen, component->identifier) < 0) goto error;
        PyTuple_SET_ITEM(plan->covered_order, index,
                         Py_NewRef(component->name));
    }
    plan->serialized_params = signature_serialize_params(plan->params, error_type);
    if (plan->serialized_params == NULL) goto error;
    capsule = PyCapsule_New(plan, SIGNATURE_PLAN_CAPSULE,
                            signature_plan_capsule_destructor);
    if (capsule == NULL) goto error;
    plan = NULL;
error:
    Py_XDECREF(seen);
    signature_plan_clear(plan);
    signature_input_entries_clear(inputs, input_count);
    signature_value_entries_clear(values, value_count);
    return capsule;
}

PyObject *
wreath_signature_plan_facts(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    SignaturePlan *plan = signature_plan_get(capsule);
    if (plan == NULL) return NULL;
    return PyTuple_Pack(3, plan->params, plan->raw_signature,
                        plan->covered_order);
}

PyObject *
wreath_signature_plan_base(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *message, *capsule, *error_type, *derived;
    SignaturePlan *plan;
    PyObject *space = NULL, *split_name = NULL, *header = NULL, *result = NULL;
    WreathBytesWriter writer = {0};
    if (!PyArg_ParseTuple(args, "OOOO:signature_plan_base", &message, &capsule,
                          &error_type, &derived)) return NULL;
    plan = signature_plan_get(capsule);
    if (plan == NULL) return NULL;
    space = PyUnicode_FromString(" ");
    split_name = PyUnicode_FromString("split");
    header = PyObject_GetAttrString(message, "header");
    if (space == NULL || split_name == NULL || header == NULL ||
        wreath_writer_init(&writer, 512) < 0) goto error;
    for (Py_ssize_t index = 0; index < plan->component_count; index++) {
        SignaturePlanComponent *component = &plan->components[index];
        PyObject *value = NULL;
        Py_ssize_t name_length;
        const char *name = PyUnicode_AsUTF8AndSize(component->name, &name_length);
        if (name == NULL) goto error;
        if (name_length > 0 && name[0] == '@')
            value = PyObject_CallFunctionObjArgs(derived, component->name,
                                                 component->params, message, NULL);
        else {
            PyObject *raw = PyObject_CallOneArg(header, component->name);
            PyObject *parts;
            if (raw == NULL) goto error;
            if (raw == Py_None) {
                PyObject *representation = PyObject_Repr(component->name);
                PyObject *error_message;
                Py_DECREF(raw);
                if (representation == NULL) goto error;
                error_message = PyUnicode_FromFormat(
                    "covered header %U is not present", representation);
                Py_DECREF(representation);
                if (error_message == NULL) goto error;
                PyErr_SetObject(error_type, error_message);
                Py_DECREF(error_message);
                goto error;
            }
            parts = PyObject_CallMethodNoArgs(raw, split_name);
            Py_DECREF(raw);
            if (parts == NULL) goto error;
            value = PyUnicode_Join(space, parts);
            Py_DECREF(parts);
        }
        if (value == NULL ||
            wreath_writer_write(&writer, PyBytes_AS_STRING(component->identifier),
                                PyBytes_GET_SIZE(component->identifier)) < 0 ||
            wreath_writer_write(&writer, ": ", 2) < 0 ||
            signature_write_unicode(&writer, value) < 0 ||
            wreath_writer_byte(&writer, '\n') < 0) {
            Py_XDECREF(value);
            goto error;
        }
        Py_DECREF(value);
    }
    if (wreath_writer_write(&writer, "\"@signature-params\": (", 22) < 0)
        goto error;
    for (Py_ssize_t index = 0; index < plan->component_count; index++) {
        PyObject *identifier = plan->components[index].identifier;
        if ((index != 0 && wreath_writer_byte(&writer, ' ') < 0) ||
            wreath_writer_write(&writer, PyBytes_AS_STRING(identifier),
                                PyBytes_GET_SIZE(identifier)) < 0)
            goto error;
    }
    if (wreath_writer_byte(&writer, ')') < 0 ||
        wreath_writer_write(&writer, PyBytes_AS_STRING(plan->serialized_params),
                            PyBytes_GET_SIZE(plan->serialized_params)) < 0)
        goto error;
    result = wreath_writer_finish(&writer);
error:
    Py_XDECREF(writer.bytes);
    Py_XDECREF(header);
    Py_XDECREF(split_name);
    Py_XDECREF(space);
    return result;
}

PyObject *
wreath_signature_base(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *message, *components, *params, *error_type;
    PyObject *derived;
    Py_ssize_t max_components;
    PyObject *sequence = NULL, *covered = NULL, *seen = NULL;
    PyObject *at = NULL;
    PyObject *space = NULL;
    PyObject *lower_name = NULL, *split_name = NULL, *header = NULL;
    PyObject *result = NULL;
    WreathBytesWriter writer = {0};
    if (!PyArg_ParseTuple(args, "OOOOOn:signature_base", &message, &components,
                          &params, &error_type, &derived,
                          &max_components)) return NULL;
    sequence = PySequence_Fast(components, "components must be a sequence");
    if (sequence == NULL) goto error;
    if (PySequence_Fast_GET_SIZE(sequence) == 0) {
        signature_raise(error_type, "signature covers no components");
        goto error;
    }
    if (PySequence_Fast_GET_SIZE(sequence) > max_components) {
        signature_raise(error_type, "signature covers too many components");
        goto error;
    }
    covered = PyList_New(PySequence_Fast_GET_SIZE(sequence));
    seen = PySet_New(NULL);
    at = PyUnicode_FromString("@");
    space = PyUnicode_FromString(" ");
    lower_name = PyUnicode_FromString("lower");
    split_name = PyUnicode_FromString("split");
    header = PyObject_GetAttrString(message, "header");
    if (covered == NULL || seen == NULL || at == NULL || space == NULL ||
        lower_name == NULL || split_name == NULL || header == NULL) goto error;
    if (wreath_writer_init(&writer, 512) < 0) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(sequence); index++) {
        PyObject *pair = PySequence_Fast(PySequence_Fast_GET_ITEM(sequence, index),
                                         "component must be a pair");
        PyObject *raw_name, *component_params, *name = NULL, *serialized_params = NULL;
        PyObject *key = NULL, *identifier = NULL, *value = NULL;
        int equal, duplicate, derived_name;
        if (pair == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(pair) != 2) {
            Py_DECREF(pair);
            PyErr_SetString(PyExc_ValueError, "component must be a pair");
            goto error;
        }
        raw_name = PySequence_Fast_GET_ITEM(pair, 0);
        component_params = PySequence_Fast_GET_ITEM(pair, 1);
        name = PyObject_CallMethodNoArgs(raw_name, lower_name);
        if (name == NULL) { Py_DECREF(pair); goto error; }
        equal = PyObject_RichCompareBool(name, raw_name, Py_EQ);
        if (equal < 0) goto item_error;
        if (!equal) {
            signature_raise(error_type, "component identifiers must be lowercase");
            goto item_error;
        }
        if (signature_component_params(component_params, error_type) < 0)
            goto item_error;
        serialized_params = signature_serialize_params(component_params, error_type);
        if (serialized_params == NULL) goto item_error;
        key = wreath_tuple2_from_owned(
            Py_NewRef(name), Py_NewRef(serialized_params));
        if (key == NULL) goto item_error;
        duplicate = PySet_Contains(seen, key);
        if (duplicate < 0) goto item_error;
        if (duplicate) {
            PyObject *representation = PyObject_Repr(name);
            PyObject *error_message;
            if (representation == NULL) goto item_error;
            error_message = PyUnicode_FromFormat("component %U is covered twice",
                                                 representation);
            Py_DECREF(representation);
            if (error_message == NULL) goto item_error;
            PyErr_SetObject(error_type, error_message);
            Py_DECREF(error_message);
            goto item_error;
        }
        if (PySet_Add(seen, key) < 0) goto item_error;
        derived_name = PyUnicode_Tailmatch(name, at, 0, 1, 1);
        if (derived_name < 0) goto item_error;
        if (derived_name)
            value = PyObject_CallFunctionObjArgs(derived, name, component_params,
                                                 message, NULL);
        else {
            PyObject *raw = PyObject_CallOneArg(header, name);
            PyObject *parts;
            if (raw == NULL) goto item_error;
            if (raw == Py_None) {
                PyObject *representation = PyObject_Repr(name);
                PyObject *error_message;
                Py_DECREF(raw);
                if (representation == NULL) goto item_error;
                error_message = PyUnicode_FromFormat(
                    "covered header %U is not present", representation);
                Py_DECREF(representation);
                if (error_message == NULL) goto item_error;
                PyErr_SetObject(error_type, error_message);
                Py_DECREF(error_message);
                goto item_error;
            }
            parts = PyObject_CallMethodNoArgs(raw, split_name);
            Py_DECREF(raw);
            if (parts == NULL) goto item_error;
            value = PyUnicode_Join(space, parts);
            Py_DECREF(parts);
        }
        if (value == NULL) goto item_error;
        identifier = signature_identifier(name, serialized_params);
        if (identifier == NULL) goto item_error;
        if (wreath_writer_write(
                &writer, PyBytes_AS_STRING(identifier),
                PyBytes_GET_SIZE(identifier)) < 0 ||
            wreath_writer_write(&writer, ": ", 2) < 0 ||
            signature_write_unicode(&writer, value) < 0 ||
            wreath_writer_byte(&writer, '\n') < 0) goto item_error;
        PyList_SET_ITEM(covered, index, identifier);
        identifier = NULL;
        Py_DECREF(value); Py_DECREF(key);
        Py_DECREF(serialized_params); Py_DECREF(name); Py_DECREF(pair);
        continue;
item_error:
        Py_XDECREF(value); Py_XDECREF(identifier); Py_XDECREF(key);
        Py_XDECREF(serialized_params); Py_XDECREF(name); Py_DECREF(pair);
        goto error;
    }
    {
        PyObject *serialized = signature_serialize_params(params, error_type);
        if (serialized == NULL ||
            wreath_writer_write(&writer, "\"@signature-params\": (", 22) < 0) {
            Py_XDECREF(serialized); goto error;
        }
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(covered); index++) {
            PyObject *identifier = PyList_GET_ITEM(covered, index);
            if ((index != 0 && wreath_writer_byte(&writer, ' ') < 0) ||
                wreath_writer_write(
                    &writer, PyBytes_AS_STRING(identifier),
                    PyBytes_GET_SIZE(identifier)) < 0) {
                Py_DECREF(serialized);
                goto error;
            }
        }
        if (wreath_writer_byte(&writer, ')') < 0 ||
            wreath_writer_write(
                &writer, PyBytes_AS_STRING(serialized),
                PyBytes_GET_SIZE(serialized)) < 0) {
            Py_DECREF(serialized); goto error;
        }
        Py_DECREF(serialized);
        result = wreath_writer_finish(&writer);
    }
error:
    Py_XDECREF(writer.bytes);
    Py_XDECREF(header); Py_XDECREF(split_name); Py_XDECREF(lower_name);
    Py_XDECREF(space); Py_XDECREF(at);
    Py_XDECREF(seen); Py_XDECREF(covered); Py_XDECREF(sequence);
    return result;
}
