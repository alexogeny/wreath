/* SCIM filter parsing and evaluation (RFC 7644 section 3.4.2.2). */
#include "wreathcore.h"

#include <stdarg.h>

#define SCIM_MAX_LENGTH 2048
#define SCIM_MAX_DEPTH 16

enum { SCIM_WORD = 256, SCIM_STRING };

typedef struct {
    int kind;
    PyObject *value;
} ScimToken;

typedef struct {
    ScimToken *tokens;
    Py_ssize_t count;
    Py_ssize_t at;
    PyObject *types;
} ScimParser;

#define SCIM_COMPARE(p) PyTuple_GET_ITEM((p)->types, 0)
#define SCIM_LOGICAL(p) PyTuple_GET_ITEM((p)->types, 1)
#define SCIM_NEGATE(p) PyTuple_GET_ITEM((p)->types, 2)
#define SCIM_GROUP(p) PyTuple_GET_ITEM((p)->types, 3)
#define SCIM_VALUE_PATH(p) PyTuple_GET_ITEM((p)->types, 4)
#define SCIM_ERROR(p) PyTuple_GET_ITEM((p)->types, 5)
#define SCIM_MAPPING(types) PyTuple_GET_ITEM((types), 6)

static int
scim_raise(PyObject *error_type, const char *format, ...)
{
    va_list args;
    PyObject *message;
    va_start(args, format);
    message = PyUnicode_FromFormatV(format, args);
    va_end(args);
    if (message == NULL) return -1;
    PyErr_SetObject(error_type, message);
    Py_DECREF(message);
    return -1;
}

static int
scim_word_char(Py_UCS4 ch)
{
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
           (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == ':' ||
           ch == '$' || ch == '-' || ch == '+';
}

static int
scim_hex(Py_UCS4 ch)
{
    if (ch >= '0' && ch <= '9') return (int)(ch - '0');
    if (ch >= 'a' && ch <= 'f') return (int)(ch - 'a' + 10);
    if (ch >= 'A' && ch <= 'F') return (int)(ch - 'A' + 10);
    return -1;
}

static PyObject *
scim_string(PyObject *source, Py_ssize_t *position, PyObject *error_type)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(source);
    Py_ssize_t index = *position + 1;
    Py_UCS4 *output = PyMem_Malloc((size_t)(length - *position) * sizeof(*output));
    Py_ssize_t written = 0;

    if (output == NULL) return PyErr_NoMemory();
    while (index < length) {
        Py_UCS4 ch = PyUnicode_READ_CHAR(source, index);
        if (ch == '"') {
            PyObject *result = PyUnicode_FromKindAndData(
                PyUnicode_4BYTE_KIND, output, written);
            PyMem_Free(output);
            *position = index + 1;
            return result;
        }
        if (ch != '\\') {
            output[written++] = ch;
            index++;
            continue;
        }
        if (index + 1 >= length) {
            PyMem_Free(output);
            scim_raise(error_type, "filter ends inside a string escape");
            return NULL;
        }
        ch = PyUnicode_READ_CHAR(source, index + 1);
        if (ch == 'u') {
            int value = 0;
            if (index + 6 > length) {
                PyMem_Free(output);
                scim_raise(error_type, "filter has a truncated \\u escape");
                return NULL;
            }
            for (Py_ssize_t digit = index + 2; digit < index + 6; digit++) {
                int nibble = scim_hex(PyUnicode_READ_CHAR(source, digit));
                if (nibble < 0) {
                    PyObject *bad = PyUnicode_Substring(source, index + 2, index + 6);
                    PyMem_Free(output);
                    if (bad == NULL) return NULL;
                    scim_raise(error_type,
                               "filter has an invalid \\u escape: \\u%U", bad);
                    Py_DECREF(bad);
                    return NULL;
                }
                value = (value << 4) | nibble;
            }
            output[written++] = (Py_UCS4)value;
            index += 6;
            continue;
        }
        switch (ch) {
        case '"': output[written++] = '"'; break;
        case '\\': output[written++] = '\\'; break;
        case '/': output[written++] = '/'; break;
        case 'b': output[written++] = '\b'; break;
        case 'f': output[written++] = '\f'; break;
        case 'n': output[written++] = '\n'; break;
        case 'r': output[written++] = '\r'; break;
        case 't': output[written++] = '\t'; break;
        default:
            PyMem_Free(output);
            scim_raise(error_type,
                       "filter has an unknown string escape: \\%c", (int)ch);
            return NULL;
        }
        index += 2;
    }
    PyMem_Free(output);
    scim_raise(error_type, "filter has an unterminated string");
    return NULL;
}

static void
scim_tokens_clear(ScimToken *tokens, Py_ssize_t count)
{
    if (tokens == NULL) return;
    for (Py_ssize_t i = 0; i < count; i++) Py_XDECREF(tokens[i].value);
    PyMem_Free(tokens);
}

static int
scim_tokenize(ScimParser *parser, PyObject *source)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(source);
    Py_ssize_t position = 0;
    parser->tokens = PyMem_Calloc((size_t)(length + 1), sizeof(*parser->tokens));
    if (parser->tokens == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    while (position < length) {
        Py_UCS4 ch = PyUnicode_READ_CHAR(source, position);
        ScimToken *token;
        if (ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n') {
            position++;
            continue;
        }
        token = &parser->tokens[parser->count];
        if (ch == '(' || ch == ')' || ch == '[' || ch == ']') {
            token->kind = (int)ch;
            token->value = PyUnicode_FromOrdinal((int)ch);
            position++;
        }
        else if (ch == '"') {
            token->kind = SCIM_STRING;
            token->value = scim_string(source, &position, SCIM_ERROR(parser));
        }
        else if (scim_word_char(ch)) {
            Py_ssize_t start = position++;
            while (position < length &&
                   scim_word_char(PyUnicode_READ_CHAR(source, position))) position++;
            token->kind = SCIM_WORD;
            token->value = PyUnicode_Substring(source, start, position);
        }
        else {
            PyObject *unexpected = PyUnicode_FromOrdinal((int)ch);
            if (unexpected == NULL) return -1;
            scim_raise(SCIM_ERROR(parser),
                       "filter contains an unexpected character: %R",
                       unexpected);
            Py_DECREF(unexpected);
            return -1;
        }
        if (token->value == NULL) return -1;
        parser->count++;
    }
    return 0;
}

static ScimToken *
scim_peek(ScimParser *parser)
{
    return parser->at < parser->count ? &parser->tokens[parser->at] : NULL;
}

static ScimToken *
scim_take(ScimParser *parser)
{
    ScimToken *token = scim_peek(parser);
    if (token == NULL) {
        scim_raise(SCIM_ERROR(parser), "filter ends where more was expected");
        return NULL;
    }
    parser->at++;
    return token;
}

static ScimToken *
scim_expect(ScimParser *parser, int kind)
{
    ScimToken *token = scim_take(parser);
    if (token == NULL) return NULL;
    if (token->kind != kind) {
        PyObject *expected = PyUnicode_FromOrdinal(kind);
        if (expected == NULL) return NULL;
        scim_raise(SCIM_ERROR(parser), "filter expected %R and found %R",
                   expected, token->value);
        Py_DECREF(expected);
        return NULL;
    }
    return token;
}

static int
scim_keyword(ScimParser *parser, const char *word)
{
    ScimToken *token = scim_peek(parser);
    return token != NULL && token->kind == SCIM_WORD &&
           PyUnicode_CompareWithASCIIString(token->value, word) == 0;
}

static PyObject *scim_disjunction(ScimParser *parser, int depth);

static PyObject *
scim_attribute(PyObject *word)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(word);
    Py_ssize_t start = 0;
    for (Py_ssize_t i = 0; i < length; i++) {
        if (PyUnicode_READ_CHAR(word, i) == ':') start = i + 1;
    }
    PyObject *tail = PyUnicode_Substring(word, start, length);
    PyObject *lower;
    if (tail == NULL) return NULL;
    lower = PyObject_CallMethod(tail, "lower", NULL);
    Py_DECREF(tail);
    return lower;
}

static PyObject *
scim_value(ScimParser *parser)
{
    ScimToken *token = scim_take(parser);
    PyObject *number;
    if (token == NULL) return NULL;
    if (token->kind == SCIM_STRING) return Py_NewRef(token->value);
    if (token->kind != SCIM_WORD) {
        scim_raise(SCIM_ERROR(parser), "filter expected a value and found %R",
                   token->value);
        return NULL;
    }
    if (PyUnicode_CompareWithASCIIString(token->value, "true") == 0)
        return Py_NewRef(Py_True);
    if (PyUnicode_CompareWithASCIIString(token->value, "false") == 0)
        return Py_NewRef(Py_False);
    if (PyUnicode_CompareWithASCIIString(token->value, "null") == 0)
        return Py_NewRef(Py_None);
    number = PyLong_FromUnicodeObject(token->value, 10);
    if (number != NULL) return number;
    PyErr_Clear();
    number = PyFloat_FromString(token->value);
    if (number != NULL) return number;
    PyErr_Clear();
    scim_raise(SCIM_ERROR(parser),
               "filter has an unquoted value: %R; strings must be quoted",
               token->value);
    return NULL;
}

static PyObject *
scim_attribute_expression(ScimParser *parser, int depth)
{
    ScimToken *token = scim_take(parser);
    PyObject *path;
    ScimToken *operator_token;
    PyObject *operator;
    PyObject *value;
    PyObject *node;
    if (token == NULL) return NULL;
    if (token->kind != SCIM_WORD) {
        scim_raise(SCIM_ERROR(parser),
                   "filter expected an attribute and found %R", token->value);
        return NULL;
    }
    path = scim_attribute(token->value);
    if (path == NULL) return NULL;
    if (PyUnicode_GET_LENGTH(path) == 0) {
        scim_raise(SCIM_ERROR(parser),
                   "filter has an empty attribute name in %R", token->value);
        Py_DECREF(path);
        return NULL;
    }
    token = scim_peek(parser);
    if (token != NULL && token->kind == '[') {
        parser->at++;
        value = scim_disjunction(parser, depth + 1);
        if (value == NULL || scim_expect(parser, ']') == NULL) {
            Py_XDECREF(value);
            Py_DECREF(path);
            return NULL;
        }
        node = PyObject_CallFunctionObjArgs(SCIM_VALUE_PATH(parser), path, value, NULL);
        Py_DECREF(path);
        Py_DECREF(value);
        return node;
    }
    operator_token = scim_take(parser);
    if (operator_token == NULL) {
        Py_DECREF(path);
        return NULL;
    }
    if (operator_token->kind != SCIM_WORD) {
        scim_raise(SCIM_ERROR(parser),
                   "filter expected an operator and found %R", operator_token->value);
        Py_DECREF(path);
        return NULL;
    }
    operator = PyObject_CallMethod(operator_token->value, "lower", NULL);
    if (operator == NULL) {
        Py_DECREF(path);
        return NULL;
    }
    if (PyUnicode_CompareWithASCIIString(operator, "pr") == 0) {
        node = PyObject_CallFunctionObjArgs(SCIM_COMPARE(parser), path, operator, NULL);
        Py_DECREF(path);
        Py_DECREF(operator);
        return node;
    }
    if (PyUnicode_CompareWithASCIIString(operator, "eq") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "ne") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "co") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "sw") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "ew") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "gt") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "ge") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "lt") != 0 &&
        PyUnicode_CompareWithASCIIString(operator, "le") != 0) {
        scim_raise(SCIM_ERROR(parser),
                   "filter has an unknown operator: %R", operator);
        Py_DECREF(path);
        Py_DECREF(operator);
        return NULL;
    }
    value = scim_value(parser);
    if (value == NULL) {
        Py_DECREF(path);
        Py_DECREF(operator);
        return NULL;
    }
    node = PyObject_CallFunctionObjArgs(SCIM_COMPARE(parser), path, operator, value, NULL);
    Py_DECREF(path);
    Py_DECREF(operator);
    Py_DECREF(value);
    return node;
}

static PyObject *
scim_unary(ScimParser *parser, int depth)
{
    ScimToken *token;
    PyObject *operand;
    PyObject *node;
    if (depth > SCIM_MAX_DEPTH) {
        scim_raise(SCIM_ERROR(parser),
                   "filter nests deeper than %d levels; simplify it or send several requests",
                   SCIM_MAX_DEPTH);
        return NULL;
    }
    if (scim_keyword(parser, "not")) {
        parser->at++;
        if (scim_expect(parser, '(') == NULL) return NULL;
        operand = scim_disjunction(parser, depth + 1);
        if (operand == NULL || scim_expect(parser, ')') == NULL) {
            Py_XDECREF(operand);
            return NULL;
        }
        node = PyObject_CallOneArg(SCIM_NEGATE(parser), operand);
        Py_DECREF(operand);
        return node;
    }
    token = scim_peek(parser);
    if (token != NULL && token->kind == '(') {
        parser->at++;
        operand = scim_disjunction(parser, depth + 1);
        if (operand == NULL || scim_expect(parser, ')') == NULL) {
            Py_XDECREF(operand);
            return NULL;
        }
        node = PyObject_CallOneArg(SCIM_GROUP(parser), operand);
        Py_DECREF(operand);
        return node;
    }
    return scim_attribute_expression(parser, depth);
}

static PyObject *
scim_conjunction(ScimParser *parser, int depth)
{
    PyObject *node = scim_unary(parser, depth);
    if (node == NULL) return NULL;
    while (scim_keyword(parser, "and")) {
        PyObject *right;
        PyObject *combined;
        PyObject *op;
        parser->at++;
        right = scim_unary(parser, depth);
        op = PyUnicode_FromString("and");
        if (right == NULL || op == NULL) {
            Py_XDECREF(right);
            Py_XDECREF(op);
            Py_DECREF(node);
            return NULL;
        }
        combined = PyObject_CallFunctionObjArgs(SCIM_LOGICAL(parser), op, node, right, NULL);
        Py_DECREF(op);
        Py_DECREF(node);
        Py_DECREF(right);
        if (combined == NULL) return NULL;
        node = combined;
    }
    return node;
}

static PyObject *
scim_disjunction(ScimParser *parser, int depth)
{
    PyObject *node = scim_conjunction(parser, depth);
    if (node == NULL) return NULL;
    while (scim_keyword(parser, "or")) {
        PyObject *right;
        PyObject *combined;
        PyObject *op;
        parser->at++;
        right = scim_conjunction(parser, depth);
        op = PyUnicode_FromString("or");
        if (right == NULL || op == NULL) {
            Py_XDECREF(right);
            Py_XDECREF(op);
            Py_DECREF(node);
            return NULL;
        }
        combined = PyObject_CallFunctionObjArgs(SCIM_LOGICAL(parser), op, node, right, NULL);
        Py_DECREF(op);
        Py_DECREF(node);
        Py_DECREF(right);
        if (combined == NULL) return NULL;
        node = combined;
    }
    return node;
}

static int
scim_unheld(PyObject *types, PyObject *attributes, PyObject *base)
{
    PyObject *names = PySequence_List(attributes);
    PyObject *separator = PyUnicode_FromString(", ");
    PyObject *held = NULL;
    int result = -1;
    if (names == NULL || separator == NULL || PyList_Sort(names) < 0) goto done;
    held = PyUnicode_Join(separator, names);
    if (held == NULL) goto done;
    result = scim_raise(
        PyTuple_GET_ITEM(types, 5),
        "this provider does not hold an attribute named %R; it holds %U",
        base, held);
done:
    Py_XDECREF(names);
    Py_XDECREF(separator);
    Py_XDECREF(held);
    return result;
}

static int
scim_check_attributes(PyObject *node, PyObject *attributes, PyObject *types, int nested)
{
    int instance;
    PyObject *path;
    PyObject *base;
    if (attributes == Py_None) return 0;
    instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 0));
    if (instance < 0) return -1;
    if (instance) {
        if (nested) return 0;
        path = PyObject_GetAttrString(node, "path");
        if (path == NULL) return -1;
        Py_ssize_t dot = PyUnicode_FindChar(path, '.', 0,
                                            PyUnicode_GET_LENGTH(path), 1);
        base = dot < 0 ? Py_NewRef(path) : PyUnicode_Substring(path, 0, dot);
        Py_DECREF(path);
        if (base == NULL) return -1;
        int held = PySet_Contains(attributes, base);
        if (held == 0) scim_unheld(types, attributes, base);
        Py_DECREF(base);
        return held == 1 ? 0 : -1;
    }
    instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 4));
    if (instance < 0) return -1;
    if (instance) {
        path = PyObject_GetAttrString(node, "path");
        if (path == NULL) return -1;
        int held = PySet_Contains(attributes, path);
        if (held == 0) scim_unheld(types, attributes, path);
        Py_DECREF(path);
        return held == 1 ? 0 : -1;
    }
    instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 1));
    if (instance < 0) return -1;
    if (instance) {
        PyObject *left = PyObject_GetAttrString(node, "left");
        PyObject *right = PyObject_GetAttrString(node, "right");
        int failed = left == NULL || right == NULL ||
                     scim_check_attributes(left, attributes, types, nested) < 0 ||
                     scim_check_attributes(right, attributes, types, nested) < 0;
        Py_XDECREF(left);
        Py_XDECREF(right);
        return failed ? -1 : 0;
    }
    for (int type_index = 2; type_index <= 3; type_index++) {
        instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, type_index));
        if (instance < 0) return -1;
        if (instance) {
            PyObject *operand = PyObject_GetAttrString(node, "operand");
            int result = operand == NULL ? -1 :
                scim_check_attributes(operand, attributes, types, nested);
            Py_XDECREF(operand);
            return result;
        }
    }
    return 0;
}

PyObject *
wreath_scim_parse(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *source;
    PyObject *attributes;
    PyObject *types;
    ScimParser parser = {NULL, 0, 0, NULL};
    PyObject *node = NULL;
    if (!PyArg_ParseTuple(args, "UOO:scim_parse", &source, &attributes, &types))
        return NULL;
    parser.types = types;
    if (PyUnicode_GET_LENGTH(source) > SCIM_MAX_LENGTH) {
        scim_raise(SCIM_ERROR(&parser),
                   "filter is longer than %d characters (%zd)",
                   SCIM_MAX_LENGTH, PyUnicode_GET_LENGTH(source));
        return NULL;
    }
    if (scim_tokenize(&parser, source) < 0) goto done;
    if (parser.count == 0) {
        scim_raise(SCIM_ERROR(&parser), "filter is empty");
        goto done;
    }
    node = scim_disjunction(&parser, 0);
    if (node == NULL) goto done;
    if (parser.at != parser.count) {
        scim_raise(SCIM_ERROR(&parser),
                   "filter has trailing input beginning at %R",
                   parser.tokens[parser.at].value);
        Py_CLEAR(node);
        goto done;
    }
    if (scim_check_attributes(node, attributes, types, 0) < 0) Py_CLEAR(node);

done:
    scim_tokens_clear(parser.tokens, parser.count);
    return node;
}

static PyObject *
scim_values(PyObject *resource, PyObject *path, PyObject *types)
{
    PyObject *separator = PyUnicode_FromString(".");
    PyObject *steps;
    PyObject *current;
    if (separator == NULL) return NULL;
    steps = PyUnicode_Split(path, separator, -1);
    Py_DECREF(separator);
    if (steps == NULL) return NULL;
    current = PyList_New(1);
    if (current == NULL) {
        Py_DECREF(steps);
        return NULL;
    }
    PyList_SET_ITEM(current, 0, Py_NewRef(resource));
    for (Py_ssize_t step_index = 0; step_index < PyList_GET_SIZE(steps); step_index++) {
        PyObject *wanted = Py_NewRef(PyList_GET_ITEM(steps, step_index));
        PyObject *found = PyList_New(0);
        if (wanted == NULL || found == NULL) {
            Py_XDECREF(wanted);
            Py_XDECREF(found);
            Py_DECREF(current);
            Py_DECREF(steps);
            return NULL;
        }
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(current); i++) {
            PyObject *item = PyList_GET_ITEM(current, i);
            int mapping = PyObject_IsInstance(item, SCIM_MAPPING(types));
            PyObject *pairs;
            if (mapping < 0) goto values_error;
            if (!mapping) continue;
            pairs = PyMapping_Items(item);
            if (pairs == NULL) goto values_error;
            PyObject *fast = PySequence_Fast(pairs, "mapping items must be a sequence");
            Py_DECREF(pairs);
            if (fast == NULL) goto values_error;
            for (Py_ssize_t pair_index = 0;
                 pair_index < PySequence_Fast_GET_SIZE(fast); pair_index++) {
                PyObject *pair = PySequence_Fast_GET_ITEM(fast, pair_index);
                PyObject *key = PySequence_GetItem(pair, 0);
                PyObject *value = PySequence_GetItem(pair, 1);
                int equal = 0;
                if (key == NULL || value == NULL) {
                    Py_XDECREF(key);
                    Py_XDECREF(value);
                    Py_DECREF(fast);
                    goto values_error;
                }
                if (PyUnicode_Check(key) &&
                    PyUnicode_GET_LENGTH(key) == PyUnicode_GET_LENGTH(wanted)) {
                    equal = 1;
                    for (Py_ssize_t at = 0; at < PyUnicode_GET_LENGTH(key); at++) {
                        if (Py_UNICODE_TOLOWER(PyUnicode_READ_CHAR(key, at)) !=
                            Py_UNICODE_TOLOWER(PyUnicode_READ_CHAR(wanted, at))) {
                            equal = 0;
                            break;
                        }
                    }
                }
                Py_DECREF(key);
                if (equal < 0) {
                    Py_DECREF(value);
                    Py_DECREF(fast);
                    goto values_error;
                }
                if (equal) {
                    if (PyList_Check(value)) {
                        if (PyList_SetSlice(found, PyList_GET_SIZE(found),
                                            PyList_GET_SIZE(found), value) < 0) {
                            Py_DECREF(value);
                            Py_DECREF(fast);
                            goto values_error;
                        }
                    }
                    else if (PyList_Append(found, value) < 0) {
                        Py_DECREF(value);
                        Py_DECREF(fast);
                        goto values_error;
                    }
                }
                Py_DECREF(value);
            }
            Py_DECREF(fast);
        }
        Py_DECREF(wanted);
        Py_DECREF(current);
        current = found;
        continue;

values_error:
        Py_DECREF(wanted);
        Py_DECREF(found);
        Py_DECREF(current);
        Py_DECREF(steps);
        return NULL;
    }
    Py_DECREF(steps);
    return current;
}

PyObject *
wreath_scim_values_at(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *resource;
    PyObject *path;
    PyObject *types;
    if (!PyArg_ParseTuple(args, "OUO:scim_values_at", &resource, &path, &types))
        return NULL;
    return scim_values(resource, path, types);
}

static int
scim_equal(PyObject *value, PyObject *wanted)
{
    if (PyUnicode_Check(value) && PyUnicode_Check(wanted)) {
        PyObject *left = PyObject_CallMethod(value, "lower", NULL);
        PyObject *right = PyObject_CallMethod(wanted, "lower", NULL);
        int equal;
        if (left == NULL || right == NULL) {
            Py_XDECREF(left);
            Py_XDECREF(right);
            return -1;
        }
        equal = PyObject_RichCompareBool(left, right, Py_EQ);
        Py_DECREF(left);
        Py_DECREF(right);
        return equal;
    }
    if (PyBool_Check(value) || PyBool_Check(wanted)) return value == wanted;
    return PyObject_RichCompareBool(value, wanted, Py_EQ);
}

static int
scim_compare(PyObject *value, PyObject *op, PyObject *wanted)
{
    if (PyUnicode_CompareWithASCIIString(op, "eq") == 0)
        return scim_equal(value, wanted);
    if (PyUnicode_CompareWithASCIIString(op, "ne") == 0) {
        int equal = scim_equal(value, wanted);
        return equal < 0 ? -1 : !equal;
    }
    if (PyUnicode_CompareWithASCIIString(op, "co") == 0 ||
        PyUnicode_CompareWithASCIIString(op, "sw") == 0 ||
        PyUnicode_CompareWithASCIIString(op, "ew") == 0) {
        PyObject *left;
        PyObject *right;
        int result;
        if (!PyUnicode_Check(value) || !PyUnicode_Check(wanted)) return 0;
        left = PyObject_CallMethod(value, "lower", NULL);
        right = PyObject_CallMethod(wanted, "lower", NULL);
        if (left == NULL || right == NULL) {
            Py_XDECREF(left);
            Py_XDECREF(right);
            return -1;
        }
        if (PyUnicode_CompareWithASCIIString(op, "co") == 0) {
            result = PyUnicode_Find(left, right, 0, PyUnicode_GET_LENGTH(left), 1) >= 0;
        }
        else {
            int direction = PyUnicode_CompareWithASCIIString(op, "sw") == 0 ? -1 : 1;
            result = PyUnicode_Tailmatch(left, right, 0,
                                         PyUnicode_GET_LENGTH(left), direction);
        }
        Py_DECREF(left);
        Py_DECREF(right);
        return result;
    }
    int comparable = (PyUnicode_Check(value) && PyUnicode_Check(wanted)) ||
        (((PyLong_Check(value) || PyFloat_Check(value)) && !PyBool_Check(value)) &&
         ((PyLong_Check(wanted) || PyFloat_Check(wanted)) && !PyBool_Check(wanted)));
    if (!comparable) return 0;
    PyObject *left = value;
    PyObject *right = wanted;
    if (PyUnicode_Check(value)) {
        left = PyObject_CallMethod(value, "lower", NULL);
        right = PyObject_CallMethod(wanted, "lower", NULL);
        if (left == NULL || right == NULL) {
            Py_XDECREF(left);
            Py_XDECREF(right);
            return -1;
        }
    }
    int relation = PyUnicode_CompareWithASCIIString(op, "gt") == 0 ? Py_GT :
                   PyUnicode_CompareWithASCIIString(op, "ge") == 0 ? Py_GE :
                   PyUnicode_CompareWithASCIIString(op, "lt") == 0 ? Py_LT : Py_LE;
    int result = PyObject_RichCompareBool(left, right, relation);
    if (left != value) {
        Py_DECREF(left);
        Py_DECREF(right);
    }
    return result;
}

static int scim_matches_node(PyObject *node, PyObject *resource, PyObject *types);

static int
scim_matches_values(PyObject *node, PyObject *resource, PyObject *types)
{
    PyObject *path = PyObject_GetAttrString(node, "path");
    PyObject *op = PyObject_GetAttrString(node, "op");
    PyObject *wanted = NULL;
    PyObject *values;
    int result = 0;
    if (path == NULL || op == NULL) goto error;
    values = scim_values(resource, path, types);
    if (values == NULL) goto error;
    if (PyUnicode_CompareWithASCIIString(op, "pr") != 0) {
        wanted = PyObject_GetAttrString(node, "value");
        if (wanted == NULL) {
            Py_DECREF(values);
            goto error;
        }
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(values); i++) {
        PyObject *value = PyList_GET_ITEM(values, i);
        if (wanted == NULL) {
            result = value != Py_None;
            if (result && (PyUnicode_Check(value) || PyList_Check(value) ||
                           PyDict_Check(value) || PyTuple_Check(value))) {
                Py_ssize_t size = PyObject_Length(value);
                if (size < 0) result = -1;
                else result = size > 0;
            }
        }
        else result = scim_compare(value, op, wanted);
        if (result != 0) break;
    }
    Py_DECREF(values);
    Py_XDECREF(wanted);
    Py_DECREF(path);
    Py_DECREF(op);
    return result;

error:
    Py_XDECREF(path);
    Py_XDECREF(op);
    Py_XDECREF(wanted);
    return -1;
}

static int
scim_matches_node(PyObject *node, PyObject *resource, PyObject *types)
{
    int instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 0));
    if (instance < 0) return -1;
    if (instance) return scim_matches_values(node, resource, types);
    instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 4));
    if (instance < 0) return -1;
    if (instance) {
        PyObject *path = PyObject_GetAttrString(node, "path");
        PyObject *predicate = PyObject_GetAttrString(node, "predicate");
        PyObject *values;
        int result = 0;
        if (path == NULL || predicate == NULL) {
            Py_XDECREF(path);
            Py_XDECREF(predicate);
            return -1;
        }
        values = scim_values(resource, path, types);
        Py_DECREF(path);
        if (values == NULL) {
            Py_DECREF(predicate);
            return -1;
        }
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(values); i++) {
            result = scim_matches_node(
                predicate, PyList_GET_ITEM(values, i), types);
            if (result != 0) break;
        }
        Py_DECREF(values);
        Py_DECREF(predicate);
        return result;
    }
    instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, 1));
    if (instance < 0) return -1;
    if (instance) {
        PyObject *op = PyObject_GetAttrString(node, "op");
        PyObject *left = PyObject_GetAttrString(node, "left");
        PyObject *right = PyObject_GetAttrString(node, "right");
        int result;
        if (op == NULL || left == NULL || right == NULL) {
            Py_XDECREF(op); Py_XDECREF(left); Py_XDECREF(right);
            return -1;
        }
        result = scim_matches_node(left, resource, types);
        if (result >= 0) {
            if (PyUnicode_CompareWithASCIIString(op, "and") == 0) {
                if (result) result = scim_matches_node(right, resource, types);
            }
            else if (!result) result = scim_matches_node(right, resource, types);
        }
        Py_DECREF(op); Py_DECREF(left); Py_DECREF(right);
        return result;
    }
    for (int type_index = 2; type_index <= 3; type_index++) {
        instance = PyObject_IsInstance(node, PyTuple_GET_ITEM(types, type_index));
        if (instance < 0) return -1;
        if (instance) {
            PyObject *operand = PyObject_GetAttrString(node, "operand");
            int result = operand == NULL ? -1 :
                scim_matches_node(operand, resource, types);
            Py_XDECREF(operand);
            return type_index == 2 && result >= 0 ? !result : result;
        }
    }
    scim_raise(PyTuple_GET_ITEM(types, 5),
               "filter contains a node this evaluator does not know");
    return -1;
}

PyObject *
wreath_scim_matches(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *node;
    PyObject *resource;
    PyObject *types;
    int result;
    if (!PyArg_ParseTuple(args, "OOO:scim_matches", &node, &resource, &types))
        return NULL;
    result = scim_matches_node(node, resource, types);
    if (result < 0) return NULL;
    return PyBool_FromLong(result);
}

PyObject *
wreath_scim_filter(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *node, *resources_object, *types;
    PyObject *resources = NULL, *selected = NULL;
    int invert = 0;
    if (!PyArg_ParseTuple(args, "OOOp:scim_filter", &node, &resources_object,
                          &types, &invert)) return NULL;
    resources = PySequence_Fast(resources_object,
                                "SCIM resources must be a sequence");
    selected = PyList_New(0);
    if (resources == NULL || selected == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(resources); index++) {
        PyObject *resource = PySequence_Fast_GET_ITEM(resources, index);
        int matched = scim_matches_node(node, resource, types);
        if (matched < 0) goto error;
        if ((matched != 0) != (invert != 0) && PyList_Append(selected, resource) < 0)
            goto error;
    }
    Py_DECREF(resources);
    return selected;
error:
    Py_XDECREF(selected);
    Py_XDECREF(resources);
    return NULL;
}
