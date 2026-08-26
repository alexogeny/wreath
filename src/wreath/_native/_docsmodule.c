/* wreath._native._docs: operation-local kernels for the documentation build.
 *
 * Markdown semantics and site policy remain in Python. These functions own the
 * repeated byte work after rendering: turn generated HTML into visible prose
 * and compile a section's searchable word tape. No mutable process-global
 * state is used; every buffer and index belongs to one call.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdlib.h>
#include <string.h>


typedef struct {
    char *data;
    Py_ssize_t length;
} Word;


#define WDT_NONBLANK 128
#define WDT_FENCE 1
#define WDT_HEADING 2
#define WDT_THEMATIC 4
#define WDT_ADMONITION 8
#define WDT_TAB 16
#define WDT_QUOTE 32
#define WDT_LIST 64


static Py_ssize_t
find_bytes(
    const char *data,
    Py_ssize_t length,
    Py_ssize_t offset,
    const char *needle,
    Py_ssize_t needle_length)
{
    Py_ssize_t index;

    if (needle_length == 0) {
        return offset;
    }
    for (index = offset; index <= length - needle_length; index++) {
        if (data[index] == needle[0] &&
            memcmp(data + index, needle, (size_t)needle_length) == 0) {
            return index;
        }
    }
    return -1;
}


static int
unicode_starts_with_ascii(
    int kind,
    const void *data,
    Py_ssize_t length,
    Py_ssize_t offset,
    const char *needle,
    Py_ssize_t needle_length)
{
    Py_ssize_t index;

    if (offset < 0 || needle_length > length - offset) {
        return 0;
    }
    for (index = 0; index < needle_length; index++) {
        if (PyUnicode_READ(kind, data, offset + index) !=
            (unsigned char)needle[index]) {
            return 0;
        }
    }
    return 1;
}


static Py_ssize_t
unicode_find_ascii(
    int kind,
    const void *data,
    Py_ssize_t length,
    Py_ssize_t offset,
    const char *needle,
    Py_ssize_t needle_length)
{
    Py_ssize_t index;

    if (needle_length == 0) {
        return offset;
    }
    for (index = offset; index <= length - needle_length; index++) {
        if (unicode_starts_with_ascii(
                kind, data, length, index, needle, needle_length)) {
            return index;
        }
    }
    return -1;
}


static PyObject *
docs_visible_prose(PyObject *Py_UNUSED(module), PyObject *value)
{
    int kind;
    const void *source;
    Py_ssize_t source_length;
    Py_ssize_t index = 0;
    Py_ssize_t intermediate_used = 0;
    Py_ssize_t used = 0;
    Py_UCS4 *intermediate;
    Py_UCS4 *output;
    PyObject *result;
    int pending_space = 0;

    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "visible_prose html must be str");
        return NULL;
    }
    kind = PyUnicode_KIND(value);
    source = PyUnicode_DATA(value);
    source_length = PyUnicode_GET_LENGTH(value);
    intermediate = PyMem_Malloc((size_t)source_length * sizeof(*intermediate));
    output = PyMem_Malloc((size_t)source_length * sizeof(*output));
    if ((intermediate == NULL || output == NULL) && source_length > 0) {
        PyMem_Free(intermediate);
        PyMem_Free(output);
        PyErr_NoMemory();
        return NULL;
    }

    while (index < source_length &&
           Py_UNICODE_ISSPACE(PyUnicode_READ(kind, source, index))) {
        index++;
    }
    if (index + 3 < source_length &&
        PyUnicode_READ(kind, source, index) == '<' &&
        PyUnicode_READ(kind, source, index + 1) == 'h' &&
        PyUnicode_READ(kind, source, index + 2) >= '1' &&
        PyUnicode_READ(kind, source, index + 2) <= '6' &&
        (PyUnicode_READ(kind, source, index + 3) == '>' ||
         Py_UNICODE_ISSPACE(PyUnicode_READ(kind, source, index + 3)))) {
        Py_ssize_t closing = unicode_find_ascii(
            kind, source, source_length, index + 4, "</h", 3);
        if (closing >= 0) {
            Py_ssize_t end = unicode_find_ascii(
                kind, source, source_length, closing + 3, ">", 1);
            if (end >= 0) {
                index = end + 1;
            }
        }
    }

    while (index < source_length) {
        if (unicode_starts_with_ascii(
                kind, source, source_length, index,
                "<a class=\"anchor\"", 17)) {
            Py_ssize_t end = unicode_find_ascii(
                kind, source, source_length, index + 17, "</a>", 4);
            if (end >= 0) {
                index = end + 4;
                continue;
            }
        }
        if (unicode_starts_with_ascii(
                kind, source, source_length, index,
                "<ul class=\"plate-names\"", 23)) {
            Py_ssize_t end = unicode_find_ascii(
                kind, source, source_length, index + 23, "</ul>", 5);
            if (end >= 0) {
                intermediate[intermediate_used++] = (Py_UCS4)' ';
                index = end + 5;
                continue;
            }
        }
        intermediate[intermediate_used++] = PyUnicode_READ(kind, source, index++);
    }

    index = 0;
    while (index < intermediate_used) {
        if (intermediate[index] == '<') {
            Py_ssize_t end = index + 1;
            while (end < intermediate_used && intermediate[end] != '>') {
                end++;
            }
            if (end < intermediate_used) {
                pending_space = used > 0;
                index = end + 1;
                continue;
            }
        }
        if (Py_UNICODE_ISSPACE(intermediate[index])) {
            pending_space = used > 0;
            index++;
            continue;
        }
        if (pending_space) {
            output[used++] = (Py_UCS4)' ';
            pending_space = 0;
        }
        output[used++] = intermediate[index++];
    }
    while (used > 0 && output[used - 1] == ' ') {
        used--;
    }
    result = PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, output, used);
    PyMem_Free(intermediate);
    PyMem_Free(output);
    return result;
}


static unsigned char
block_flags(PyObject *line)
{
    int kind = PyUnicode_KIND(line);
    const void *data = PyUnicode_DATA(line);
    Py_ssize_t length = PyUnicode_GET_LENGTH(line);
    Py_ssize_t stripped = 0;
    Py_UCS4 first;
    Py_UCS4 stripped_first;
    unsigned char flags = WDT_NONBLANK;

    while (stripped < length &&
           Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, stripped))) {
        stripped++;
    }
    if (stripped == length) {
        return 0;
    }
    first = PyUnicode_READ(kind, data, 0);
    stripped_first = PyUnicode_READ(kind, data, stripped);
    if (first == '`' || first == '~') {
        flags |= WDT_FENCE;
    }
    if (first == '#') {
        flags |= WDT_HEADING;
    }
    if (stripped_first == '-' || stripped_first == '*' || stripped_first == '_') {
        flags |= WDT_THEMATIC;
    }
    if (first == '!' || first == '?') {
        flags |= WDT_ADMONITION;
    }
    if (first == '=') {
        flags |= WDT_TAB;
    }
    if (first == '>') {
        flags |= WDT_QUOTE;
    }
    if (stripped_first == '-' || stripped_first == '*' ||
        stripped_first == '+' ||
        (stripped_first >= '0' && stripped_first <= '9')) {
        flags |= WDT_LIST;
    }
    return flags;
}


static PyObject *
docs_block_tape(PyObject *Py_UNUSED(module), PyObject *value)
{
    int kind;
    const void *source;
    Py_ssize_t source_length;
    Py_ssize_t line_count = 1;
    Py_ssize_t index;
    Py_ssize_t start;
    Py_ssize_t line_index = 0;
    PyObject *lines = NULL;
    PyObject *flags_object = NULL;
    PyObject *version = NULL;
    PyObject *result = NULL;
    char *flags;

    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "block_tape source must be str");
        return NULL;
    }
    kind = PyUnicode_KIND(value);
    source = PyUnicode_DATA(value);
    source_length = PyUnicode_GET_LENGTH(value);
    for (index = 0; index < source_length; index++) {
        Py_UCS4 character = PyUnicode_READ(kind, source, index);
        if (character == '\r') {
            line_count++;
            if (index + 1 < source_length &&
                PyUnicode_READ(kind, source, index + 1) == '\n') {
                index++;
            }
        }
        else if (character == '\n') {
            line_count++;
        }
    }
    lines = PyList_New(line_count);
    flags_object = PyBytes_FromStringAndSize(NULL, line_count);
    version = PyLong_FromLong(1);
    if (lines == NULL || flags_object == NULL || version == NULL) {
        goto cleanup;
    }
    flags = PyBytes_AS_STRING(flags_object);
    start = 0;
    index = 0;
    while (index <= source_length) {
        Py_ssize_t end = index;
        PyObject *line;
        while (end < source_length) {
            Py_UCS4 character = PyUnicode_READ(kind, source, end);
            if (character == '\r' || character == '\n') {
                break;
            }
            end++;
        }
        line = PyUnicode_Substring(value, start, end);
        if (line == NULL) {
            goto cleanup;
        }
        PyList_SET_ITEM(lines, line_index, line);
        flags[line_index++] = (char)block_flags(line);
        if (end == source_length) {
            break;
        }
        index = end + 1;
        if (PyUnicode_READ(kind, source, end) == '\r' &&
            index < source_length && PyUnicode_READ(kind, source, index) == '\n') {
            index++;
        }
        start = index;
    }
    result = PyTuple_Pack(3, version, lines, flags_object);

cleanup:
    Py_XDECREF(version);
    Py_XDECREF(flags_object);
    Py_XDECREF(lines);
    return result;
}


static int
fence_line(
    PyObject *line,
    Py_UCS4 *marker,
    Py_ssize_t *marker_length,
    Py_ssize_t *info_start,
    Py_ssize_t *info_end)
{
    int kind = PyUnicode_KIND(line);
    const void *data = PyUnicode_DATA(line);
    Py_ssize_t length = PyUnicode_GET_LENGTH(line);
    Py_ssize_t index;
    Py_UCS4 first;

    if (length < 3) {
        return 0;
    }
    first = PyUnicode_READ(kind, data, 0);
    if (first != '`' && first != '~') {
        return 0;
    }
    index = 1;
    while (index < length && PyUnicode_READ(kind, data, index) == first) {
        index++;
    }
    if (index < 3) {
        return 0;
    }
    *marker = first;
    *marker_length = index;
    while (index < length && Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, index))) {
        index++;
    }
    *info_start = index;
    *info_end = length;
    while (*info_end > index &&
           Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, *info_end - 1))) {
        (*info_end)--;
    }
    return 1;
}


static int
python_info(PyObject *line, Py_ssize_t start, Py_ssize_t end)
{
    int kind = PyUnicode_KIND(line);
    const void *data = PyUnicode_DATA(line);
    Py_ssize_t stop = start;
    Py_ssize_t length;

    while (stop < end && !Py_UNICODE_ISSPACE(PyUnicode_READ(kind, data, stop))) {
        stop++;
    }
    length = stop - start;
    return (length == 2 &&
            PyUnicode_READ(kind, data, start) == 'p' &&
            PyUnicode_READ(kind, data, start + 1) == 'y') ||
           (length == 6 &&
            unicode_starts_with_ascii(kind, data, end, start, "python", 6)) ||
           (length == 7 &&
            unicode_starts_with_ascii(kind, data, end, start, "python3", 7));
}


static PyObject *
docs_python_blocks(PyObject *module, PyObject *value)
{
    PyObject *tape = NULL;
    PyObject *lines;
    PyObject *blocks = NULL;
    PyObject *separator = NULL;
    Py_ssize_t index = 0;
    Py_ssize_t line_count;

    tape = docs_block_tape(module, value);
    if (tape == NULL) {
        return NULL;
    }
    lines = PyTuple_GET_ITEM(tape, 1);
    line_count = PyList_GET_SIZE(lines);
    blocks = PyList_New(0);
    separator = PyUnicode_FromString("\n");
    if (blocks == NULL || separator == NULL) {
        goto error;
    }
    while (index < line_count) {
        PyObject *line = PyList_GET_ITEM(lines, index);
        Py_UCS4 marker;
        Py_ssize_t marker_length;
        Py_ssize_t info_start;
        Py_ssize_t info_end;
        Py_ssize_t opening = index;
        Py_ssize_t closing;
        int is_python;

        if (!fence_line(
                line, &marker, &marker_length, &info_start, &info_end)) {
            index++;
            continue;
        }
        is_python = python_info(line, info_start, info_end);
        closing = index + 1;
        while (closing < line_count) {
            PyObject *candidate = PyList_GET_ITEM(lines, closing);
            Py_UCS4 candidate_marker;
            Py_ssize_t candidate_length;
            Py_ssize_t candidate_info_start;
            Py_ssize_t candidate_info_end;
            if (fence_line(
                    candidate,
                    &candidate_marker,
                    &candidate_length,
                    &candidate_info_start,
                    &candidate_info_end) &&
                candidate_marker == marker &&
                candidate_length >= marker_length &&
                candidate_info_start == candidate_info_end) {
                break;
            }
            closing++;
        }
        if (is_python) {
            PyObject *info = PyUnicode_Substring(line, info_start, info_end);
            PyObject *body_lines = PyList_GetSlice(lines, opening + 1, closing);
            PyObject *body = NULL;
            PyObject *line_number = NULL;
            PyObject *record = NULL;
            if (info == NULL || body_lines == NULL) {
                Py_XDECREF(info);
                Py_XDECREF(body_lines);
                goto error;
            }
            body = PyUnicode_Join(separator, body_lines);
            Py_DECREF(body_lines);
            line_number = PyLong_FromSsize_t(opening + 1);
            if (body == NULL || line_number == NULL) {
                Py_DECREF(info);
                Py_XDECREF(body);
                Py_XDECREF(line_number);
                goto error;
            }
            record = PyTuple_Pack(3, info, body, line_number);
            Py_DECREF(info);
            Py_DECREF(body);
            Py_DECREF(line_number);
            if (record == NULL || PyList_Append(blocks, record) < 0) {
                Py_XDECREF(record);
                goto error;
            }
            Py_DECREF(record);
        }
        index = closing + 1;
    }
    Py_DECREF(separator);
    Py_DECREF(tape);
    return blocks;

error:
    Py_XDECREF(separator);
    Py_XDECREF(blocks);
    Py_DECREF(tape);
    return NULL;
}


static int
word_compare_raw(const char *left, Py_ssize_t left_length,
                 const char *right, Py_ssize_t right_length)
{
    Py_ssize_t shared = left_length < right_length ? left_length : right_length;
    int compared = memcmp(left, right, (size_t)shared);
    if (compared != 0) {
        return compared;
    }
    return (left_length > right_length) - (left_length < right_length);
}


static int
word_compare(const void *left_value, const void *right_value)
{
    const Word *left = left_value;
    const Word *right = right_value;
    return word_compare_raw(left->data, left->length, right->data, right->length);
}


static int
is_stopword(PyObject *stopwords, const char *word, Py_ssize_t length)
{
    Py_ssize_t low = 0;
    Py_ssize_t high = PyTuple_GET_SIZE(stopwords);

    while (low < high) {
        Py_ssize_t middle = low + (high - low) / 2;
        PyObject *candidate_object = PyTuple_GET_ITEM(stopwords, middle);
        Py_ssize_t candidate_length;
        const char *candidate = PyUnicode_AsUTF8AndSize(
            candidate_object, &candidate_length);
        if (candidate == NULL) {
            return -1;
        }
        int compared = word_compare_raw(word, length, candidate, candidate_length);
        if (compared == 0) {
            return 1;
        }
        if (compared < 0) {
            high = middle;
        }
        else {
            low = middle + 1;
        }
    }
    return 0;
}


static int
allowed_word_byte(unsigned char value)
{
    return (value >= 'a' && value <= 'z') ||
           (value >= '0' && value <= '9') || value == '_' || value == '.';
}


static int
word_start_byte(unsigned char value)
{
    return allowed_word_byte(value) && value != '.';
}


static Py_ssize_t
stem_word(char *word, Py_ssize_t length)
{
    if (length <= 4) {
        return length;
    }
    if (length >= 3 && memcmp(word + length - 3, "ies", 3) == 0) {
        word[length - 3] = 'y';
        return length - 2;
    }
    if (length >= 4 &&
        (memcmp(word + length - 4, "ches", 4) == 0 ||
         memcmp(word + length - 4, "shes", 4) == 0 ||
         memcmp(word + length - 4, "sses", 4) == 0)) {
        return length - 2;
    }
    if (length >= 3 &&
        (memcmp(word + length - 3, "xes", 3) == 0 ||
         memcmp(word + length - 3, "zes", 3) == 0)) {
        return length - 2;
    }
    if (length >= 2 && memcmp(word + length - 2, "es", 2) == 0) {
        return length - 1;
    }
    if (word[length - 1] == 's' &&
        !(length >= 2 && word[length - 2] == 's')) {
        return length - 1;
    }
    return length;
}


static int
contains_bytes(
    const char *haystack,
    Py_ssize_t haystack_length,
    const char *needle,
    Py_ssize_t needle_length)
{
    return find_bytes(haystack, haystack_length, 0, needle, needle_length) >= 0;
}


static PyObject *
docs_word_set(PyObject *Py_UNUSED(module), PyObject *args)
{
    PyObject *text_object;
    PyObject *covered_object;
    PyObject *stopwords;
    const char *text_source;
    const char *covered_source;
    Py_ssize_t text_length;
    Py_ssize_t covered_length;
    char *text = NULL;
    char *covered = NULL;
    Word *words = NULL;
    Py_ssize_t word_count = 0;
    Py_ssize_t word_capacity = 0;
    Py_ssize_t index;
    PyObject *storage = NULL;
    PyObject *result = NULL;
    char *output;
    Py_ssize_t used = 0;

    if (!PyArg_ParseTuple(
            args, "UUO!:word_set",
            &text_object, &covered_object, &PyTuple_Type, &stopwords)) {
        return NULL;
    }
    text_source = PyUnicode_AsUTF8AndSize(text_object, &text_length);
    covered_source = PyUnicode_AsUTF8AndSize(covered_object, &covered_length);
    if (text_source == NULL || covered_source == NULL) {
        return NULL;
    }
    text = PyMem_Malloc((size_t)text_length + 1);
    covered = PyMem_Malloc((size_t)covered_length + 1);
    if (text == NULL || covered == NULL) {
        PyErr_NoMemory();
        goto cleanup;
    }
    for (index = 0; index < text_length; index++) {
        unsigned char value = (unsigned char)text_source[index];
        text[index] = value >= 'A' && value <= 'Z' ? (char)(value + ('a' - 'A')) : (char)value;
    }
    text[text_length] = '\0';
    for (index = 0; index < covered_length; index++) {
        unsigned char value = (unsigned char)covered_source[index];
        covered[index] = value >= 'A' && value <= 'Z' ? (char)(value + ('a' - 'A')) : (char)value;
    }
    covered[covered_length] = '\0';

    index = 0;
    while (index < text_length) {
        Py_ssize_t start;
        Py_ssize_t length;
        if (!word_start_byte((unsigned char)text[index])) {
            index++;
            continue;
        }
        start = index++;
        while (index < text_length && allowed_word_byte((unsigned char)text[index])) {
            index++;
        }
        length = index - start;
        if (length < 3) {
            continue;
        }
        {
            int stopped = is_stopword(stopwords, text + start, length);
            if (stopped < 0) {
                goto cleanup;
            }
            if (stopped) {
                continue;
            }
        }
        length = stem_word(text + start, length);
        if (word_count == word_capacity) {
            Py_ssize_t next_capacity = word_capacity == 0 ? 32 : word_capacity * 2;
            Word *next = PyMem_Realloc(words, (size_t)next_capacity * sizeof(*words));
            if (next == NULL) {
                PyErr_NoMemory();
                goto cleanup;
            }
            words = next;
            word_capacity = next_capacity;
        }
        words[word_count].data = text + start;
        words[word_count].length = length;
        word_count++;
    }
    qsort(words, (size_t)word_count, sizeof(*words), word_compare);
    storage = PyBytes_FromStringAndSize(NULL, text_length);
    if (storage == NULL) {
        goto cleanup;
    }
    output = PyBytes_AS_STRING(storage);
    for (index = 0; index < word_count; index++) {
        Word *word = &words[index];
        if (index > 0 && word_compare(word, &words[index - 1]) == 0) {
            continue;
        }
        if (contains_bytes(covered, covered_length, word->data, word->length)) {
            continue;
        }
        if (used > 0) {
            output[used++] = ' ';
        }
        memcpy(output + used, word->data, (size_t)word->length);
        used += word->length;
    }
    result = PyUnicode_DecodeUTF8(output, used, "strict");

cleanup:
    Py_XDECREF(storage);
    PyMem_Free(words);
    PyMem_Free(covered);
    PyMem_Free(text);
    return result;
}


static PyMethodDef docs_methods[] = {
    {"block_tape", docs_block_tape, METH_O,
     "block_tape(source) -> (version, lines, flags)\nCompile a WDT1 block scan."},
    {"python_blocks", docs_python_blocks, METH_O,
     "python_blocks(source) -> list[(info, body, line)]\nExtract WDT1 Python fences."},
    {"visible_prose", docs_visible_prose, METH_O,
     "visible_prose(html) -> str\nExtract visible section prose in one native pass."},
    {"word_set", docs_word_set, METH_VARARGS,
     "word_set(text, covered, stopwords) -> str\nCompile the sorted WDT1 search-word tape."},
    {NULL, NULL, 0, NULL}
};


static struct PyModuleDef docs_module = {
    PyModuleDef_HEAD_INIT,
    "_docs",
    "Native documentation-build kernels.",
    0,
    docs_methods,
    NULL,
    NULL,
    NULL,
    NULL
};


PyMODINIT_FUNC
PyInit__docs(void)
{
    return PyModule_Create(&docs_module);
}
