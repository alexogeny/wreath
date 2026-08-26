/* wreath._native._lint: operation-local lexical tapes for native-source lints.
 *
 * The linters share two facts about a C translation unit: source with comments
 * and literal contents blanked, and loop nesting at each source line.  Building
 * those facts in separate Python character loops cost more than the rules that
 * consumed them.  This module compiles both images together.  It owns no
 * mutable process-global state; every buffer belongs to one call.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>


enum LexState {
    LEX_CODE = 0,
    LEX_BLOCK,
    LEX_LINE,
    LEX_STRING,
    LEX_CHAR
};


static int
ascii_at(const Py_UCS4 *source, Py_ssize_t length, Py_ssize_t offset,
         const char *word, Py_ssize_t word_length)
{
    Py_ssize_t index;

    if (offset < 0 || word_length > length - offset) {
        return 0;
    }
    for (index = 0; index < word_length; index++) {
        if (source[offset + index] != (unsigned char)word[index]) {
            return 0;
        }
    }
    return 1;
}


static int
loop_open_paren(const Py_UCS4 *source, Py_ssize_t length, Py_ssize_t offset,
                Py_ssize_t *open_paren)
{
    Py_ssize_t cursor;

    if (offset > 0 &&
        (Py_UNICODE_ISALNUM(source[offset - 1]) || source[offset - 1] == '_')) {
        return 0;
    }
    if (ascii_at(source, length, offset, "for", 3)) {
        cursor = offset + 3;
    }
    else if (ascii_at(source, length, offset, "while", 5)) {
        cursor = offset + 5;
    }
    else {
        return 0;
    }
    while (cursor < length && Py_UNICODE_ISSPACE(source[cursor])) {
        cursor++;
    }
    if (cursor >= length || source[cursor] != '(') {
        return 0;
    }
    *open_paren = cursor;
    return 1;
}


static Py_ssize_t
condition_close(const Py_UCS4 *source, Py_ssize_t length, Py_ssize_t open_paren)
{
    Py_ssize_t close = open_paren;
    int balance = 0;

    while (close < length) {
        if (source[close] == '(') {
            balance++;
        }
        else if (source[close] == ')') {
            balance--;
            if (balance == 0) {
                break;
            }
        }
        close++;
    }
    return close;
}


static void
fill_depth(int *depth_at, Py_ssize_t start, Py_ssize_t stop, int depth)
{
    Py_ssize_t offset;

    for (offset = start; offset < stop; offset++) {
        depth_at[offset] = depth;
    }
}


static Py_ssize_t
skip_space(const Py_UCS4 *source, Py_ssize_t length, Py_ssize_t offset,
           int *depth_at, int depth)
{
    while (offset < length && Py_UNICODE_ISSPACE(source[offset])) {
        depth_at[offset] = depth;
        offset++;
    }
    return offset;
}


static int
compile_loop_depth(const Py_UCS4 *source, Py_ssize_t length, int *depth_at)
{
    unsigned char *stack = NULL;
    Py_ssize_t stack_length = 0;
    Py_ssize_t index = 0;
    int current = 0;

    stack = PyMem_New(unsigned char, length + 1);
    if (stack == NULL) {
        PyErr_NoMemory();
        return -1;
    }

    while (index < length) {
        Py_ssize_t open_paren;

        depth_at[index] = current;
        if (loop_open_paren(source, length, index, &open_paren)) {
            Py_ssize_t close = condition_close(source, length, open_paren);
            Py_ssize_t probe;

            fill_depth(depth_at, index, close + 1 < length ? close + 1 : length,
                       current);
            probe = skip_space(source, length, close + 1, depth_at, current);
            if (probe < length && source[probe] == '{') {
                stack[stack_length++] = 1;
                current++;
                depth_at[probe] = current;
                index = probe + 1;
                continue;
            }
            index = close + 1;
            continue;
        }
        if (source[index] == '{') {
            stack[stack_length++] = 0;
        }
        else if (source[index] == '}' && stack_length > 0) {
            stack_length--;
            if (stack[stack_length]) {
                if (current > 0) {
                    current--;
                }
            }
        }
        index++;
    }
    depth_at[length] = current;
    PyMem_Free(stack);
    return 0;
}


static PyObject *
lint_c_tape(PyObject *Py_UNUSED(module), PyObject *value)
{
    PyObject *stripped = NULL;
    PyObject *newline = NULL;
    PyObject *lines = NULL;
    PyObject *depths = NULL;
    PyObject *result = NULL;
    Py_UCS4 *source = NULL;
    int *depth_at = NULL;
    Py_ssize_t length;
    Py_ssize_t index;
    Py_ssize_t line_count = 1;
    Py_ssize_t line_index = 0;
    enum LexState state = LEX_CODE;

    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "c_tape source must be str");
        return NULL;
    }
    length = PyUnicode_GetLength(value);
    if (length < 0) {
        return NULL;
    }
    source = PyUnicode_AsUCS4Copy(value);
    if (source == NULL) {
        return NULL;
    }

    for (index = 0; index < length; index++) {
        Py_UCS4 ch = source[index];
        Py_UCS4 next = index + 1 < length ? source[index + 1] : 0;

        if (ch == '\n') {
            line_count++;
            if (state == LEX_LINE) {
                state = LEX_CODE;
            }
            continue;
        }
        if (state == LEX_CODE) {
            if (ch == '/' && next == '*') {
                source[index] = ' ';
                source[index + 1] = ' ';
                state = LEX_BLOCK;
                index++;
            }
            else if (ch == '/' && next == '/') {
                source[index] = ' ';
                source[index + 1] = ' ';
                state = LEX_LINE;
                index++;
            }
            else if (ch == '"') {
                state = LEX_STRING;
            }
            else if (ch == '\'') {
                state = LEX_CHAR;
            }
            continue;
        }
        if (state == LEX_BLOCK) {
            if (ch == '*' && next == '/') {
                source[index] = ' ';
                source[index + 1] = ' ';
                state = LEX_CODE;
                index++;
            }
            else {
                source[index] = ' ';
            }
            continue;
        }
        if (state == LEX_LINE) {
            source[index] = ' ';
            continue;
        }
        if (ch == '\\') {
            source[index] = ' ';
            if (index + 1 < length && source[index + 1] != '\n') {
                source[index + 1] = ' ';
                index++;
            }
            continue;
        }
        if ((state == LEX_STRING && ch == '"') ||
            (state == LEX_CHAR && ch == '\'')) {
            state = LEX_CODE;
            continue;
        }
        source[index] = ' ';
    }

    depth_at = PyMem_Calloc((size_t)length + 1, sizeof(int));
    if (depth_at == NULL) {
        PyErr_NoMemory();
        goto cleanup;
    }
    if (compile_loop_depth(source, length, depth_at) < 0) {
        goto cleanup;
    }

    stripped = PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, source, length);
    if (stripped == NULL) {
        goto cleanup;
    }
    newline = PyUnicode_FromString("\n");
    if (newline == NULL) {
        goto cleanup;
    }
    lines = PyUnicode_Split(stripped, newline, -1);
    if (lines == NULL) {
        goto cleanup;
    }
    depths = PyList_New(line_count);
    if (depths == NULL) {
        goto cleanup;
    }

    {
        PyObject *depth = PyLong_FromLong(depth_at[0]);
        if (depth == NULL) {
            goto cleanup;
        }
        PyList_SET_ITEM(depths, line_index++, depth);
    }
    for (index = 0; index < length; index++) {
        if (source[index] == '\n') {
            PyObject *depth = PyLong_FromLong(depth_at[index + 1]);
            if (depth == NULL) {
                goto cleanup;
            }
            PyList_SET_ITEM(depths, line_index++, depth);
        }
    }
    result = Py_BuildValue("(iNN)", 1, lines, depths);
    lines = NULL;
    depths = NULL;

cleanup:
    Py_XDECREF(depths);
    Py_XDECREF(lines);
    Py_XDECREF(newline);
    Py_XDECREF(stripped);
    PyMem_Free(depth_at);
    PyMem_Free(source);
    return result;
}


static PyMethodDef lint_methods[] = {
    {"c_tape", lint_c_tape, METH_O,
     "c_tape(source) -> (version, stripped_lines, loop_depths)\n"
     "Compile the shared native-lint lexical tape."},
    {NULL, NULL, 0, NULL}
};


static struct PyModuleDef lint_module = {
    PyModuleDef_HEAD_INIT,
    "_lint",
    "Native C-source lexical tape.",
    0,
    lint_methods,
    NULL,
    NULL,
    NULL,
    NULL
};


PyMODINIT_FUNC
PyInit__lint(void)
{
    return PyModule_Create(&lint_module);
}
