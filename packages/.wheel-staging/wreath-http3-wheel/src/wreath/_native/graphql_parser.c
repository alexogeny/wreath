/* Bounded GraphQL recursive descent. AST classes remain Python declarations. */
#include "wreathcore.h"

enum { G_PUNCT, G_NAME, G_NUMBER, G_STRING, G_SPREAD, G_EOF };

typedef struct {
    int *kinds;
    PyObject **values;
    Py_ssize_t *starts;
    Py_ssize_t count, at;
    PyObject *source;
    PyObject *config;
    PyObject *limits;
    PyObject *aliases;
    Py_ssize_t complexity, depth;
} GParser;

#define GC(p, i) PyTuple_GET_ITEM((p)->config, (i))
enum { C_ARGUMENT, C_DOCUMENT, C_FIELD, C_FRAGMENT_DEF, C_FRAGMENT_SPREAD,
       C_INLINE_FRAGMENT, C_OPERATION, C_SELECTION_SET, C_VARIABLE,
       C_VARIABLE_DEF, C_ERROR };

static PyObject *g_error(
    GParser *p, const char *message, const char *code, Py_ssize_t position);

static int
g_hex(Py_UCS4 ch)
{
    if (ch >= '0' && ch <= '9') return (int)(ch - '0');
    if (ch >= 'a' && ch <= 'f') return (int)(ch - 'a' + 10);
    if (ch >= 'A' && ch <= 'F') return (int)(ch - 'A' + 10);
    return -1;
}

static PyObject *
g_unescape(GParser *p, PyObject *raw)
{
    Py_ssize_t length = PyUnicode_GET_LENGTH(raw);
    Py_UCS4 *output;
    Py_ssize_t written = 0;
    if ((size_t)length > SIZE_MAX / sizeof(*output) - 1) return PyErr_NoMemory();
    output = PyMem_Malloc(((size_t)length + 1) * sizeof(*output));
    if (output == NULL) return PyErr_NoMemory();
    for (Py_ssize_t i = 0; i < length; i++) {
        Py_UCS4 ch = PyUnicode_READ_CHAR(raw, i);
        if (ch != '\\') { output[written++] = ch; continue; }
        if (++i >= length) { PyMem_Free(output); return g_error(p, "invalid escape '\\\\'", "syntax", 0); }
        ch = PyUnicode_READ_CHAR(raw, i);
        if (ch == 'u' && i + 4 < length) {
            int value = 0;
            int valid = 1;
            for (int digit = 1; digit <= 4; digit++) {
                int nibble = g_hex(PyUnicode_READ_CHAR(raw, i + digit));
                if (nibble < 0) { valid = 0; break; }
                value = (value << 4) | nibble;
            }
            if (valid) { output[written++] = (Py_UCS4)value; i += 4; continue; }
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
        default: {
            PyObject *escape = PyUnicode_FromOrdinal((int)ch);
            PyObject *message = escape == NULL ? NULL :
                PyUnicode_FromFormat("invalid escape %R", escape);
            const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
            PyMem_Free(output); Py_XDECREF(escape);
            if (text != NULL) g_error(p, text, "syntax", 0);
            Py_XDECREF(message); return NULL;
        }
        }
    }
    PyObject *result = PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, output, written);
    PyMem_Free(output); return result;
}

static PyObject *
g_block_string(PyObject *raw)
{
    PyObject *crlf = PyUnicode_FromString("\r\n");
    PyObject *cr = PyUnicode_FromString("\r");
    PyObject *lf = PyUnicode_FromString("\n");
    PyObject *normalized = NULL;
    PyObject *lines = NULL;
    PyObject *stripped = NULL;
    PyObject *result = NULL;
    Py_ssize_t common = PY_SSIZE_T_MAX;
    if (crlf == NULL || cr == NULL || lf == NULL) goto done;
    normalized = PyUnicode_Replace(raw, crlf, lf, -1);
    if (normalized == NULL) goto done;
    Py_SETREF(normalized, PyUnicode_Replace(normalized, cr, lf, -1));
    if (normalized == NULL) goto done;
    lines = PyUnicode_Split(normalized, lf, -1);
    if (lines == NULL) goto done;
    for (Py_ssize_t i = 1; i < PyList_GET_SIZE(lines); i++) {
        PyObject *line = PyList_GET_ITEM(lines, i);
        Py_ssize_t length = PyUnicode_GET_LENGTH(line);
        int nonblank = 0;
        for (Py_ssize_t at = 0; at < length; at++) {
            if (!Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(line, at))) {
                nonblank = 1;
                break;
            }
        }
        if (!nonblank) continue;
        Py_ssize_t indent = 0;
        while (indent < length) {
            Py_UCS4 ch = PyUnicode_READ_CHAR(line, indent);
            if (ch != ' ' && ch != '\t') break;
            indent++;
        }
        if (indent < common) common = indent;
    }
    if (common == PY_SSIZE_T_MAX) common = 0;
    stripped = PyList_New(PyList_GET_SIZE(lines));
    if (stripped == NULL) goto done;
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(lines); i++) {
        PyObject *line = PyList_GET_ITEM(lines, i);
        PyObject *value;
        if (i == 0) {
            Py_ssize_t left = 0, right = PyUnicode_GET_LENGTH(line);
            while (left < right &&
                   Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(line, left))) left++;
            while (right > left &&
                   Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(line, right - 1))) right--;
            value = PyUnicode_Substring(line, left, right);
        }
        else value = PyUnicode_Substring(line, common, PyUnicode_GET_LENGTH(line));
        if (value == NULL) goto done;
        PyList_SET_ITEM(stripped, i, value);
    }
    {
        Py_ssize_t first = 0, last = PyList_GET_SIZE(stripped);
        while (first < last) {
            PyObject *line = PyList_GET_ITEM(stripped, first);
            int nonblank = 0;
            for (Py_ssize_t at = 0; at < PyUnicode_GET_LENGTH(line); at++) {
                if (!Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(line, at))) {
                    nonblank = 1; break;
                }
            }
            if (nonblank) break;
            first++;
        }
        while (last > first) {
            PyObject *line = PyList_GET_ITEM(stripped, last - 1);
            int nonblank = 0;
            for (Py_ssize_t at = 0; at < PyUnicode_GET_LENGTH(line); at++) {
                if (!Py_UNICODE_ISSPACE(PyUnicode_READ_CHAR(line, at))) {
                    nonblank = 1; break;
                }
            }
            if (nonblank) break;
            last--;
        }
        PyObject *kept = PyList_GetSlice(stripped, first, last);
        if (kept != NULL) result = PyUnicode_Join(lf, kept);
        Py_XDECREF(kept);
    }
done:
    Py_XDECREF(crlf); Py_XDECREF(cr); Py_XDECREF(lf); Py_XDECREF(normalized);
    Py_XDECREF(lines); Py_XDECREF(stripped); return result;
}

static PyObject *
g_error(GParser *p, const char *message, const char *code, Py_ssize_t position)
{
    PyObject *text = PyUnicode_FromString(message);
    PyObject *args;
    PyObject *kwargs;
    PyObject *code_obj;
    PyObject *position_obj;
    PyObject *error;
    if (text == NULL) return NULL;
    args = PyTuple_Pack(1, text);
    kwargs = PyDict_New();
    code_obj = PyUnicode_FromString(code);
    position_obj = PyLong_FromSsize_t(position);
    Py_DECREF(text);
    if (args == NULL || kwargs == NULL || code_obj == NULL || position_obj == NULL) {
        Py_XDECREF(args); Py_XDECREF(kwargs); Py_XDECREF(code_obj);
        Py_XDECREF(position_obj);
        return NULL;
    }
    if (PyDict_SetItemString(kwargs, "code", code_obj) < 0 ||
        PyDict_SetItemString(kwargs, "position", position_obj) < 0) {
        Py_DECREF(args); Py_DECREF(kwargs); Py_DECREF(code_obj); Py_DECREF(position_obj);
        return NULL;
    }
    Py_DECREF(code_obj); Py_DECREF(position_obj);
    error = PyObject_Call(GC(p, C_ERROR), args, kwargs);
    Py_DECREF(args); Py_DECREF(kwargs);
    if (error == NULL) return NULL;
    PyErr_SetObject(GC(p, C_ERROR), error);
    Py_DECREF(error);
    return NULL;
}

static PyObject *
g_error_format(GParser *p, const char *code, Py_ssize_t position,
               const char *format, PyObject *value)
{
    PyObject *message = PyUnicode_FromFormat(format, value);
    PyObject *result;
    const char *utf8;
    if (message == NULL) return NULL;
    utf8 = PyUnicode_AsUTF8(message);
    if (utf8 == NULL) { Py_DECREF(message); return NULL; }
    result = g_error(p, utf8, code, position);
    Py_DECREF(message);
    return result;
}

static Py_ssize_t
g_position(GParser *p)
{
    if (p->count == 0) return 0;
    return p->starts[p->at < p->count ? p->at : p->count - 1];
}

static int
g_head_in(Py_UCS4 ch, const char *ascii)
{
    if (ch > 127) return 0;
    return strchr(ascii, (int)ch) != NULL;
}

static int
g_tokenize(GParser *p, PyObject *source)
{
    PyObject *max_steps_object = PyObject_GetAttrString(p->limits, "max_steps");
    Py_ssize_t max_steps = max_steps_object == NULL ? -1 :
        PyLong_AsSsize_t(max_steps_object);
    Py_ssize_t length = PyUnicode_GET_LENGTH(source);
    Py_ssize_t position = 0, capacity;
    Py_XDECREF(max_steps_object);
    if (max_steps < 0 && PyErr_Occurred()) return -1;
    if ((size_t)length > SIZE_MAX / sizeof(*p->values) ||
        (size_t)length > SIZE_MAX / sizeof(*p->starts) ||
        (size_t)length > SIZE_MAX / sizeof(*p->kinds)) {
        PyErr_NoMemory();
        return -1;
    }
    capacity = length == 0 ? 1 : length;
    p->kinds = PyMem_Malloc((size_t)capacity * sizeof(*p->kinds));
    p->values = PyMem_Calloc((size_t)capacity, sizeof(*p->values));
    p->starts = PyMem_Malloc((size_t)capacity * sizeof(*p->starts));
    if (p->kinds == NULL || p->values == NULL || p->starts == NULL) {
        PyErr_NoMemory(); return -1;
    }
    p->source = source;
    while (position < length) {
        Py_UCS4 head = PyUnicode_READ_CHAR(source, position);
        Py_ssize_t start = position;
        Py_ssize_t end;
        if (g_head_in(head, " \t\r\n\f\v,") || head == 0xfeff) {
            position++;
            continue;
        }
        if (head == '#') {
            do position++;
            while (position < length &&
                   PyUnicode_READ_CHAR(source, position) != '\r' &&
                   PyUnicode_READ_CHAR(source, position) != '\n');
            continue;
        }
        if (p->count >= max_steps) {
            PyObject *message = PyUnicode_FromFormat(
                "document exceeded the %zd-token parse budget", max_steps);
            const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
            if (text != NULL) g_error(p, text, "steps", start);
            Py_XDECREF(message); return -1;
        }
        p->starts[p->count] = start;
        if ((head >= 'A' && head <= 'Z') || (head >= 'a' && head <= 'z') || head == '_') {
            position++;
            while (position < length) {
                Py_UCS4 ch = PyUnicode_READ_CHAR(source, position);
                if (!((ch >= 'A' && ch <= 'Z') ||
                      (ch >= 'a' && ch <= 'z') ||
                      (ch >= '0' && ch <= '9') || ch == '_')) break;
                position++;
            }
            p->kinds[p->count] = G_NAME;
            p->values[p->count] = PyUnicode_Substring(source, start, position);
        }
        else if (g_head_in(head, "{}()[]:=$@!|&")) {
            position++;
            p->kinds[p->count] = G_PUNCT;
        }
        else if (head == '-' || (head >= '0' && head <= '9')) {
            if (head == '-') {
                position++;
                if (position >= length ||
                    PyUnicode_READ_CHAR(source, position) < '0' ||
                    PyUnicode_READ_CHAR(source, position) > '9') {
                    g_error(p, "expected a number after '-'", "syntax", start);
                    return -1;
                }
            }
            while (position < length &&
                   PyUnicode_READ_CHAR(source, position) >= '0' &&
                   PyUnicode_READ_CHAR(source, position) <= '9') position++;
            if (position < length && PyUnicode_READ_CHAR(source, position) == '.' &&
                position + 1 < length &&
                PyUnicode_READ_CHAR(source, position + 1) >= '0' &&
                PyUnicode_READ_CHAR(source, position + 1) <= '9') {
                position += 2;
                while (position < length &&
                       PyUnicode_READ_CHAR(source, position) >= '0' &&
                       PyUnicode_READ_CHAR(source, position) <= '9') position++;
            }
            if (position < length &&
                (PyUnicode_READ_CHAR(source, position) == 'e' ||
                 PyUnicode_READ_CHAR(source, position) == 'E')) {
                Py_ssize_t exponent = position++;
                if (position < length &&
                    (PyUnicode_READ_CHAR(source, position) == '+' ||
                     PyUnicode_READ_CHAR(source, position) == '-')) position++;
                if (position >= length ||
                    PyUnicode_READ_CHAR(source, position) < '0' ||
                    PyUnicode_READ_CHAR(source, position) > '9') position = exponent;
                else {
                    do position++;
                    while (position < length &&
                           PyUnicode_READ_CHAR(source, position) >= '0' &&
                           PyUnicode_READ_CHAR(source, position) <= '9');
                }
            }
            end = position;
            PyObject *token = PyUnicode_Substring(source, start, end);
            if (token == NULL) return -1;
            if (end == start + 1 && head == '-') {
                g_error(p, "expected a number after '-'", "syntax", start);
                Py_DECREF(token); return -1;
            }
            p->kinds[p->count] = G_NUMBER;
            if (PyUnicode_FindChar(token, '.', 0, end - start, 1) >= 0 ||
                PyUnicode_FindChar(token, 'e', 0, end - start, 1) >= 0 ||
                PyUnicode_FindChar(token, 'E', 0, end - start, 1) >= 0)
                p->values[p->count] = PyFloat_FromString(token);
            else p->values[p->count] = PyLong_FromUnicodeObject(token, 10);
            Py_DECREF(token);
        }
        else if (head == '"') {
            int block = position + 2 < length &&
                PyUnicode_READ_CHAR(source, position + 1) == '"' &&
                PyUnicode_READ_CHAR(source, position + 2) == '"';
            p->kinds[p->count] = G_STRING;
            if (block) {
                position += 3;
                while (position + 2 < length &&
                       !(PyUnicode_READ_CHAR(source, position) == '"' &&
                         PyUnicode_READ_CHAR(source, position + 1) == '"' &&
                         PyUnicode_READ_CHAR(source, position + 2) == '"'))
                    position++;
                if (position + 2 >= length) {
                    g_error(p, "unterminated string", "syntax", start);
                    return -1;
                }
                PyObject *raw = PyUnicode_Substring(source, start + 3, position);
                position += 3;
                p->values[p->count] = raw == NULL ? NULL : g_block_string(raw);
                Py_XDECREF(raw);
            }
            else {
                position++;
                while (position < length &&
                       PyUnicode_READ_CHAR(source, position) != '"') {
                    Py_UCS4 ch = PyUnicode_READ_CHAR(source, position);
                    if (ch == '\r' || ch == '\n') break;
                    if (ch == '\\') {
                        if (position + 1 >= length ||
                            PyUnicode_READ_CHAR(source, position + 1) == '\r' ||
                            PyUnicode_READ_CHAR(source, position + 1) == '\n') break;
                        position += 2;
                    }
                    else position++;
                }
                if (position >= length ||
                    PyUnicode_READ_CHAR(source, position) != '"') {
                    g_error(p, "unterminated string", "syntax", start);
                    return -1;
                }
                PyObject *raw = PyUnicode_Substring(source, start + 1, position);
                position++;
                p->values[p->count] = raw == NULL ? NULL : g_unescape(p, raw);
                Py_XDECREF(raw);
            }
        }
        else if (head == '.' && position + 2 < length &&
                 PyUnicode_READ_CHAR(source, position + 1) == '.' &&
                 PyUnicode_READ_CHAR(source, position + 2) == '.') {
            position += 3;
            p->kinds[p->count] = G_SPREAD;
        }
        else {
            PyObject *character = PyUnicode_FromOrdinal((int)head);
            if (character != NULL)
                g_error_format(p, "syntax", start,
                               "unexpected character %R", character);
            Py_XDECREF(character);
            return -1;
        }
        if ((p->kinds[p->count] == G_NAME ||
             p->kinds[p->count] == G_NUMBER ||
             p->kinds[p->count] == G_STRING) &&
            p->values[p->count] == NULL) return -1;
        p->count++;
    }
    return 0;
}

static int g_kind(GParser *p) { return p->at < p->count ? p->kinds[p->at] : G_EOF; }
static int g_end(GParser *p) { return p->at >= p->count; }
static int
g_peek(GParser *p, char ch)
{
    return p->at < p->count && p->kinds[p->at] == G_PUNCT &&
           PyUnicode_READ_CHAR(p->source, p->starts[p->at]) == (Py_UCS4)ch;
}
static int
g_expect(GParser *p, char ch)
{
    if (!g_peek(p, ch)) {
        char message[] = "expected '?'";
        message[10] = ch;
        g_error(p, message, "syntax", g_position(p));
        return -1;
    }
    p->at++; return 0;
}
static int g_maybe(GParser *p, char ch) { if (g_peek(p, ch)) { p->at++; return 1; } return 0; }
static PyObject *
g_name(GParser *p)
{
    if (g_kind(p) != G_NAME) return g_error(p, "expected a name", "syntax", g_position(p));
    return Py_NewRef(p->values[p->at++]);
}

static PyObject *g_value(GParser *p);
static PyObject *g_selection_set(GParser *p, Py_ssize_t depth);

static PyObject *
g_value(GParser *p)
{
    int kind = g_kind(p);
    if (kind == G_NUMBER || kind == G_STRING) return Py_NewRef(p->values[p->at++]);
    if (g_maybe(p, '$')) {
        PyObject *name = g_name(p);
        PyObject *node = name == NULL ? NULL : PyObject_CallOneArg(GC(p, C_VARIABLE), name);
        Py_XDECREF(name); return node;
    }
    if (g_maybe(p, '[')) {
        PyObject *items = PyList_New(0);
        if (items == NULL) return NULL;
        while (!g_peek(p, ']')) {
            PyObject *item;
            if (g_end(p)) { Py_DECREF(items); return g_error(p, "unterminated list value", "syntax", g_position(p)); }
            item = g_value(p);
            if (item == NULL || PyList_Append(items, item) < 0) { Py_XDECREF(item); Py_DECREF(items); return NULL; }
            Py_DECREF(item);
        }
        p->at++; return items;
    }
    if (g_maybe(p, '{')) {
        PyObject *entries = PyDict_New();
        if (entries == NULL) return NULL;
        while (!g_peek(p, '}')) {
            PyObject *name, *value;
            if (g_end(p)) { Py_DECREF(entries); return g_error(p, "unterminated object value", "syntax", g_position(p)); }
            name = g_name(p);
            if (name == NULL || g_expect(p, ':') < 0) { Py_XDECREF(name); Py_DECREF(entries); return NULL; }
            value = g_value(p);
            if (value == NULL || PyDict_SetItem(entries, name, value) < 0) {
                Py_XDECREF(value); Py_DECREF(name); Py_DECREF(entries); return NULL;
            }
            Py_DECREF(name); Py_DECREF(value);
        }
        p->at++; return entries;
    }
    PyObject *name = g_name(p);
    if (name == NULL) return NULL;
    if (PyUnicode_CompareWithASCIIString(name, "true") == 0) { Py_DECREF(name); return Py_NewRef(Py_True); }
    if (PyUnicode_CompareWithASCIIString(name, "false") == 0) { Py_DECREF(name); return Py_NewRef(Py_False); }
    if (PyUnicode_CompareWithASCIIString(name, "null") == 0) { Py_DECREF(name); return Py_NewRef(Py_None); }
    return name;
}

static PyObject *
g_arguments(GParser *p)
{
    if (!g_maybe(p, '(')) return PyTuple_New(0);
    PyObject *items = PyList_New(0);
    if (items == NULL) return NULL;
    while (!g_peek(p, ')')) {
        PyObject *name, *value, *node;
        if (g_end(p)) { Py_DECREF(items); return g_error(p, "unterminated argument list", "syntax", g_position(p)); }
        name = g_name(p);
        if (name == NULL || g_expect(p, ':') < 0) { Py_XDECREF(name); Py_DECREF(items); return NULL; }
        value = g_value(p);
        node = value == NULL ? NULL : PyObject_CallFunctionObjArgs(GC(p, C_ARGUMENT), name, value, NULL);
        Py_DECREF(name); Py_XDECREF(value);
        if (node == NULL || PyList_Append(items, node) < 0) { Py_XDECREF(node); Py_DECREF(items); return NULL; }
        Py_DECREF(node);
    }
    p->at++;
    PyObject *result = PyList_AsTuple(items); Py_DECREF(items); return result;
}

static int
g_directives(GParser *p)
{
    while (g_maybe(p, '@')) {
        PyObject *name = g_name(p);
        PyObject *args;
        if (name == NULL) return -1;
        Py_DECREF(name);
        args = g_arguments(p);
        if (args == NULL) return -1;
        Py_DECREF(args);
    }
    return 0;
}

static PyObject *
g_selection(GParser *p, Py_ssize_t depth)
{
    if (g_kind(p) == G_SPREAD) {
        PyObject *name;
        PyObject *node;
        p->at++;
        if (g_peek(p, '{')) {
            if (g_directives(p) < 0) return NULL;
            PyObject *set = g_selection_set(p, depth + 1);
            if (set == NULL) return NULL;
            node = PyObject_CallFunctionObjArgs(GC(p, C_INLINE_FRAGMENT), Py_None, set, NULL);
            Py_DECREF(set); return node;
        }
        name = g_name(p);
        if (name == NULL) return NULL;
        if (PyUnicode_CompareWithASCIIString(name, "on") == 0) {
            PyObject *condition = g_name(p);
            PyObject *set;
            Py_DECREF(name);
            if (condition == NULL || g_directives(p) < 0) { Py_XDECREF(condition); return NULL; }
            set = g_selection_set(p, depth + 1);
            if (set == NULL) { Py_DECREF(condition); return NULL; }
            node = PyObject_CallFunctionObjArgs(GC(p, C_INLINE_FRAGMENT), condition, set, NULL);
            Py_DECREF(condition); Py_DECREF(set); return node;
        }
        if (g_directives(p) < 0) { Py_DECREF(name); return NULL; }
        node = PyObject_CallOneArg(GC(p, C_FRAGMENT_SPREAD), name);
        Py_DECREF(name); return node;
    }
    PyObject *name = g_name(p);
    PyObject *key;
    PyObject *arguments;
    PyObject *set = NULL;
    PyObject *node;
    if (name == NULL) return NULL;
    key = Py_NewRef(name);
    if (g_maybe(p, ':')) {
        PyObject *field = g_name(p);
        if (field == NULL) { Py_DECREF(name); Py_DECREF(key); return NULL; }
        Py_SETREF(name, field);
    }
    p->complexity++;
    PyObject *limit_obj = PyObject_GetAttrString(p->limits, "max_complexity");
    Py_ssize_t limit = limit_obj == NULL ? -1 : PyLong_AsSsize_t(limit_obj);
    Py_XDECREF(limit_obj);
    if (limit < 0 && PyErr_Occurred()) { Py_DECREF(name); Py_DECREF(key); return NULL; }
    if (p->complexity > limit) {
        PyObject *message = PyUnicode_FromFormat("document selects more than %zd fields", limit);
        const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
        if (text != NULL) g_error(p, text, "complexity", g_position(p));
        Py_XDECREF(message); Py_DECREF(name); Py_DECREF(key); return NULL;
    }
    if (PyObject_RichCompareBool(key, name, Py_NE) == 1) {
        PyObject *old = PyDict_GetItemWithError(p->aliases, name);
        long count = old == NULL ? 1 : PyLong_AsLong(old) + 1;
        PyObject *count_obj = PyLong_FromLong(count);
        PyObject *alias_limit_obj = PyObject_GetAttrString(p->limits, "max_aliases");
        Py_ssize_t alias_limit = alias_limit_obj == NULL ? -1 : PyLong_AsSsize_t(alias_limit_obj);
        Py_XDECREF(alias_limit_obj);
        if (count_obj == NULL || (alias_limit < 0 && PyErr_Occurred()) ||
            PyDict_SetItem(p->aliases, name, count_obj) < 0) {
            Py_XDECREF(count_obj); Py_DECREF(name); Py_DECREF(key); return NULL;
        }
        Py_DECREF(count_obj);
        if (count > alias_limit) {
            PyObject *message = PyUnicode_FromFormat(
                "field %R is aliased more than %zd times", name, alias_limit);
            const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
            if (text != NULL) g_error(p, text, "aliases", g_position(p));
            Py_XDECREF(message); Py_DECREF(name); Py_DECREF(key); return NULL;
        }
    }
    arguments = g_arguments(p);
    if (arguments == NULL || g_directives(p) < 0) {
        Py_XDECREF(arguments); Py_DECREF(name); Py_DECREF(key); return NULL;
    }
    if (g_peek(p, '{')) set = g_selection_set(p, depth + 1);
    if (g_peek(p, '{') || (set == NULL && PyErr_Occurred())) {
        Py_DECREF(arguments); Py_DECREF(name); Py_DECREF(key); Py_XDECREF(set); return NULL;
    }
    node = PyObject_CallFunctionObjArgs(
        GC(p, C_FIELD), name, key, arguments, set == NULL ? Py_None : set, NULL);
    Py_DECREF(name); Py_DECREF(key); Py_DECREF(arguments); Py_XDECREF(set);
    return node;
}

static PyObject *
g_selection_set(GParser *p, Py_ssize_t depth)
{
    PyObject *limit_obj = PyObject_GetAttrString(p->limits, "max_depth");
    Py_ssize_t limit = limit_obj == NULL ? -1 : PyLong_AsSsize_t(limit_obj);
    PyObject *items;
    PyObject *tuple;
    PyObject *result;
    Py_XDECREF(limit_obj);
    if (limit < 0 && PyErr_Occurred()) return NULL;
    if (depth > limit) {
        PyObject *message = PyUnicode_FromFormat(
            "selection nesting exceeds the maximum depth of %zd", limit);
        const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
        if (text != NULL) g_error(p, text, "depth", g_position(p));
        Py_XDECREF(message); return NULL;
    }
    if (depth > p->depth) p->depth = depth;
    if (g_expect(p, '{') < 0) return NULL;
    items = PyList_New(0);
    if (items == NULL) return NULL;
    while (!g_peek(p, '}')) {
        PyObject *selection;
        if (g_end(p)) { Py_DECREF(items); return g_error(p, "unterminated selection set", "syntax", g_position(p)); }
        selection = g_selection(p, depth);
        if (selection == NULL || PyList_Append(items, selection) < 0) {
            Py_XDECREF(selection); Py_DECREF(items); return NULL;
        }
        Py_DECREF(selection);
    }
    p->at++;
    if (PyList_GET_SIZE(items) == 0) {
        Py_DECREF(items); return g_error(p, "a selection set cannot be empty", "syntax", g_position(p));
    }
    tuple = PyList_AsTuple(items); Py_DECREF(items);
    if (tuple == NULL) return NULL;
    result = PyObject_CallOneArg(GC(p, C_SELECTION_SET), tuple);
    Py_DECREF(tuple); return result;
}

static PyObject *
g_variables(GParser *p)
{
    if (!g_maybe(p, '(')) return PyTuple_New(0);
    PyObject *items = PyList_New(0);
    if (items == NULL) return NULL;
    while (!g_peek(p, ')')) {
        PyObject *name, *type, *def = NULL, *node, *flags[3];
        int is_list, inner_non_null, non_null, has_default = 0;
        if (g_end(p)) { Py_DECREF(items); return g_error(p, "unterminated variable definitions", "syntax", g_position(p)); }
        if (g_expect(p, '$') < 0) { Py_DECREF(items); return NULL; }
        name = g_name(p);
        if (name == NULL || g_expect(p, ':') < 0) { Py_XDECREF(name); Py_DECREF(items); return NULL; }
        is_list = g_maybe(p, '[');
        type = g_name(p);
        inner_non_null = g_maybe(p, '!');
        if (type == NULL || (is_list && g_expect(p, ']') < 0)) {
            Py_DECREF(name); Py_XDECREF(type); Py_DECREF(items); return NULL;
        }
        non_null = g_maybe(p, '!') || (inner_non_null && !is_list);
        if (g_maybe(p, '=')) { def = g_value(p); has_default = 1; }
        else def = Py_NewRef(Py_None);
        flags[0] = PyBool_FromLong(non_null); flags[1] = PyBool_FromLong(is_list);
        flags[2] = PyBool_FromLong(has_default);
        if (def == NULL || flags[0] == NULL || flags[1] == NULL || flags[2] == NULL) {
            Py_DECREF(name); Py_DECREF(type); Py_XDECREF(def);
            Py_XDECREF(flags[0]); Py_XDECREF(flags[1]); Py_XDECREF(flags[2]);
            Py_DECREF(items); return NULL;
        }
        node = PyObject_CallFunctionObjArgs(GC(p, C_VARIABLE_DEF), name, type,
            flags[0], flags[1], def, flags[2], NULL);
        Py_DECREF(name); Py_DECREF(type); Py_DECREF(def);
        Py_DECREF(flags[0]); Py_DECREF(flags[1]); Py_DECREF(flags[2]);
        if (node == NULL || PyList_Append(items, node) < 0) {
            Py_XDECREF(node); Py_DECREF(items); return NULL;
        }
        Py_DECREF(node);
    }
    p->at++;
    PyObject *tuple = PyList_AsTuple(items); Py_DECREF(items); return tuple;
}

static PyObject *
g_operation(GParser *p)
{
    PyObject *kind = PyUnicode_FromString("query");
    PyObject *name = Py_NewRef(Py_None);
    PyObject *variables, *set, *node;
    if (kind == NULL || name == NULL) { Py_XDECREF(kind); Py_XDECREF(name); return NULL; }
    if (!g_peek(p, '{')) {
        Py_SETREF(kind, g_name(p));
        if (kind == NULL) { Py_DECREF(name); return NULL; }
        if (PyUnicode_CompareWithASCIIString(kind, "query") != 0 &&
            PyUnicode_CompareWithASCIIString(kind, "mutation") != 0) {
            PyObject *message = PyUnicode_FromFormat(
                "unsupported operation %R; only query and mutation are served", kind);
            const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
            if (text != NULL) g_error(p, text, "syntax", g_position(p));
            Py_XDECREF(message); Py_DECREF(kind); Py_DECREF(name); return NULL;
        }
        if (!g_peek(p, '(') && !g_peek(p, '{')) Py_SETREF(name, g_name(p));
        if (name == NULL) { Py_DECREF(kind); return NULL; }
    }
    variables = g_variables(p);
    if (variables == NULL || g_directives(p) < 0) {
        Py_XDECREF(variables); Py_DECREF(kind); Py_DECREF(name); return NULL;
    }
    set = g_selection_set(p, 1);
    if (set == NULL) { Py_DECREF(variables); Py_DECREF(kind); Py_DECREF(name); return NULL; }
    node = PyObject_CallFunctionObjArgs(GC(p, C_OPERATION), kind, name, variables, set, NULL);
    Py_DECREF(kind); Py_DECREF(name); Py_DECREF(variables); Py_DECREF(set);
    return node;
}

static void
g_clear(GParser *p)
{
    for (Py_ssize_t i = 0; i < p->count; i++) Py_XDECREF(p->values[i]);
    PyMem_Free(p->kinds); PyMem_Free(p->values); PyMem_Free(p->starts);
    Py_XDECREF(p->aliases);
}

static int
g_collect_spreads(GParser *p, PyObject *selection_set, PyObject *found)
{
    PyObject *selections = PyObject_GetAttrString(selection_set, "selections");
    PyObject *fast;
    if (selections == NULL) return -1;
    fast = PySequence_Fast(selections, "selections must be a sequence");
    Py_DECREF(selections);
    if (fast == NULL) return -1;
    for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(fast); i++) {
        PyObject *selection = PySequence_Fast_GET_ITEM(fast, i);
        int spread = PyObject_IsInstance(selection, GC(p, C_FRAGMENT_SPREAD));
        if (spread < 0) { Py_DECREF(fast); return -1; }
        if (spread) {
            PyObject *name = PyObject_GetAttrString(selection, "name");
            if (name == NULL || PyList_Append(found, name) < 0) {
                Py_XDECREF(name); Py_DECREF(fast); return -1;
            }
            Py_DECREF(name);
            continue;
        }
        PyObject *child = PyObject_GetAttrString(selection, "selection_set");
        if (child == NULL) { Py_DECREF(fast); return -1; }
        if (child != Py_None && g_collect_spreads(p, child, found) < 0) {
            Py_DECREF(child); Py_DECREF(fast); return -1;
        }
        Py_DECREF(child);
    }
    Py_DECREF(fast);
    return 0;
}

static int
g_visit_fragment(GParser *p, PyObject *fragments, PyObject *state,
                 PyObject *name, PyObject *path)
{
    PyObject *marker = PyDict_GetItemWithError(state, name);
    long status = marker == NULL ? -1 : PyLong_AsLong(marker);
    if (marker == NULL && PyErr_Occurred()) return -1;
    if (marker != NULL && status == 1) return 0;
    if (marker != NULL && status == 0) {
        Py_ssize_t start = 0;
        PyObject *cycle;
        PyObject *arrow;
        PyObject *joined;
        PyObject *message;
        const char *text;
        while (start < PyList_GET_SIZE(path)) {
            int equal = PyObject_RichCompareBool(
                PyList_GET_ITEM(path, start), name, Py_EQ);
            if (equal < 0) return -1;
            if (equal) break;
            start++;
        }
        cycle = PyList_GetSlice(path, start, PyList_GET_SIZE(path));
        arrow = PyUnicode_FromString(" -> ");
        if (cycle == NULL || arrow == NULL || PyList_Append(cycle, name) < 0) {
            Py_XDECREF(cycle); Py_XDECREF(arrow); return -1;
        }
        joined = PyUnicode_Join(arrow, cycle);
        Py_DECREF(arrow); Py_DECREF(cycle);
        if (joined == NULL) return -1;
        message = PyUnicode_FromFormat("fragment cycle: %U", joined);
        Py_DECREF(joined);
        if (message == NULL) return -1;
        text = PyUnicode_AsUTF8(message);
        if (text != NULL) g_error(p, text, "fragment_cycle", 0);
        Py_DECREF(message);
        return -1;
    }
    PyObject *definition = PyDict_GetItemWithError(fragments, name);
    PyObject *zero;
    PyObject *next_path;
    PyObject *found;
    PyObject *set;
    if (definition == NULL) return PyErr_Occurred() ? -1 : 0;
    zero = PyLong_FromLong(0);
    next_path = PyList_GetSlice(path, 0, PyList_GET_SIZE(path));
    found = PyList_New(0);
    set = PyObject_GetAttrString(definition, "selection_set");
    if (zero == NULL || next_path == NULL || found == NULL || set == NULL ||
        PyDict_SetItem(state, name, zero) < 0 ||
        PyList_Append(next_path, name) < 0 ||
        g_collect_spreads(p, set, found) < 0) {
        Py_XDECREF(zero); Py_XDECREF(next_path); Py_XDECREF(found); Py_XDECREF(set);
        return -1;
    }
    Py_DECREF(zero); Py_DECREF(set);
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(found); i++) {
        if (g_visit_fragment(p, fragments, state,
                             PyList_GET_ITEM(found, i), next_path) < 0) {
            Py_DECREF(next_path); Py_DECREF(found); return -1;
        }
    }
    Py_DECREF(next_path); Py_DECREF(found);
    PyObject *one = PyLong_FromLong(1);
    int failed;
    if (one == NULL) return -1;
    failed = PyDict_SetItem(state, name, one) < 0;
    Py_DECREF(one);
    return failed ? -1 : 0;
}

static int
g_reject_cycles(GParser *p, PyObject *fragments)
{
    PyObject *state = PyDict_New();
    PyObject *names = PyDict_Keys(fragments);
    PyObject *path = PyList_New(0);
    if (state == NULL || names == NULL || path == NULL) {
        Py_XDECREF(state); Py_XDECREF(names); Py_XDECREF(path); return -1;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(names); i++) {
        if (g_visit_fragment(p, fragments, state,
                             PyList_GET_ITEM(names, i), path) < 0) {
            Py_DECREF(state); Py_DECREF(names); Py_DECREF(path); return -1;
        }
    }
    Py_DECREF(state); Py_DECREF(names); Py_DECREF(path);
    return 0;
}

PyObject *
wreath_graphql_parse(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *source;
    PyObject *limits;
    PyObject *config;
    PyObject *operations = NULL;
    PyObject *fragments = NULL;
    PyObject *result = NULL;
    GParser parser = {0};
    if (!PyArg_ParseTuple(args, "UOO:graphql_parse", &source, &limits, &config))
        return NULL;
    parser.config = config;
    parser.limits = limits;
    PyObject *size_obj = PyObject_GetAttrString(limits, "max_document_bytes");
    Py_ssize_t maximum = size_obj == NULL ? -1 : PyLong_AsSsize_t(size_obj);
    Py_XDECREF(size_obj);
    if (maximum < 0 && PyErr_Occurred()) return NULL;
    if (PyUnicode_GET_LENGTH(source) > maximum) {
        PyObject *message = PyUnicode_FromFormat(
            "document is longer than %zd characters", maximum);
        const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
        if (text != NULL) g_error(&parser, text, "document_size", 0);
        Py_XDECREF(message); return NULL;
    }
    parser.aliases = PyDict_New();
    if (parser.aliases == NULL || g_tokenize(&parser, source) < 0) goto done;
    operations = PyList_New(0);
    fragments = PyDict_New();
    if (operations == NULL || fragments == NULL) goto done;
    while (!g_end(&parser)) {
        if (g_peek(&parser, '{')) {
            PyObject *operation = g_operation(&parser);
            if (operation == NULL || PyList_Append(operations, operation) < 0) {
                Py_XDECREF(operation); goto done;
            }
            Py_DECREF(operation); continue;
        }
        Py_ssize_t checkpoint = parser.at;
        PyObject *word = g_name(&parser);
        if (word == NULL) goto done;
        if (PyUnicode_CompareWithASCIIString(word, "fragment") == 0) {
            PyObject *name = g_name(&parser);
            PyObject *on = g_name(&parser);
            PyObject *condition = NULL;
            PyObject *set = NULL;
            PyObject *definition = NULL;
            Py_DECREF(word);
            if (name == NULL || on == NULL) { Py_XDECREF(name); Py_XDECREF(on); goto done; }
            if (PyUnicode_CompareWithASCIIString(on, "on") != 0) {
                Py_DECREF(name); Py_DECREF(on);
                g_error(&parser, "a fragment needs an `on` type condition",
                        "syntax", g_position(&parser)); goto done;
            }
            Py_DECREF(on);
            condition = g_name(&parser);
            if (condition == NULL || g_directives(&parser) < 0) {
                Py_DECREF(name); Py_XDECREF(condition); goto done;
            }
            if (PyDict_Contains(fragments, name) == 1) {
                PyObject *message = PyUnicode_FromFormat("fragment %R is defined twice", name);
                const char *text = message == NULL ? NULL : PyUnicode_AsUTF8(message);
                if (text != NULL) g_error(&parser, text, "syntax", g_position(&parser));
                Py_XDECREF(message); Py_DECREF(name); Py_DECREF(condition); goto done;
            }
            set = g_selection_set(&parser, 1);
            if (set != NULL) definition = PyObject_CallFunctionObjArgs(
                GC(&parser, C_FRAGMENT_DEF), name, condition, set, NULL);
            if (definition == NULL || PyDict_SetItem(fragments, name, definition) < 0) {
                Py_XDECREF(definition); Py_XDECREF(set);
                Py_DECREF(name); Py_DECREF(condition); goto done;
            }
            Py_DECREF(definition); Py_DECREF(set); Py_DECREF(name); Py_DECREF(condition);
            continue;
        }
        Py_DECREF(word);
        parser.at = checkpoint;
        PyObject *operation = g_operation(&parser);
        if (operation == NULL || PyList_Append(operations, operation) < 0) {
            Py_XDECREF(operation); goto done;
        }
        Py_DECREF(operation);
    }
    if (PyList_GET_SIZE(operations) == 0) {
        g_error(&parser, "document defines no operation", "syntax", g_position(&parser));
        goto done;
    }
    {
        PyObject *ops_tuple;
        PyObject *depth;
        PyObject *complexity;
        if (g_reject_cycles(&parser, fragments) < 0) goto done;
        ops_tuple = PyList_AsTuple(operations);
        depth = PyLong_FromSsize_t(parser.depth);
        complexity = PyLong_FromSsize_t(parser.complexity);
        if (ops_tuple != NULL && depth != NULL && complexity != NULL)
            result = PyObject_CallFunctionObjArgs(GC(&parser, C_DOCUMENT), ops_tuple,
                                                  fragments, depth, complexity, NULL);
        Py_XDECREF(ops_tuple); Py_XDECREF(depth); Py_XDECREF(complexity);
    }

done:
    Py_XDECREF(operations); Py_XDECREF(fragments); g_clear(&parser);
    return result;
}
