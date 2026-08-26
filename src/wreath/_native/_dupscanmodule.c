/* wreath._native._dupscan: operation-local duplicate-fragment detection.
 *
 * Python owns source discovery, language-aware function boundaries, reporting,
 * and the whole-body AST contract. This module owns the repeated byte work for
 * the optional fragment report: tokenize each supplied body, build rolling
 * token windows, and extend equal windows to one maximal clone. Every table and
 * buffer belongs to one scan call; there is no mutable process-global state.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdint.h>
#include <string.h>


#define DUP_SHAPE 0
#define DUP_ALPHA 1
#define DUP_HASH_BASE UINT64_C(1000003)


typedef struct {
    uint64_t hash;
    Py_ssize_t line;
} DupToken;


typedef struct {
    const char *name;
    Py_ssize_t length;
    uint64_t hash;
    uint32_t canonical;
    unsigned char occupied;
} DupName;


typedef struct {
    DupName *entries;
    size_t capacity;
    size_t used;
    uint32_t next_canonical;
} DupNames;


typedef struct {
    DupToken *tokens;
    uint64_t *prefix;
    Py_ssize_t count;
    Py_ssize_t capacity;
} DupTape;


typedef struct {
    uint64_t hash;
    Py_ssize_t body;
    Py_ssize_t start;
    unsigned char occupied;
} DupWindow;


static uint64_t
dup_hash_bytes(unsigned char tag, const char *data, Py_ssize_t length)
{
    uint64_t value = UINT64_C(1469598103934665603) ^ tag;
    Py_ssize_t index;
    for (index = 0; index < length; index++) {
        value ^= (unsigned char)data[index];
        value *= UINT64_C(1099511628211);
    }
    return value;
}


static uint64_t
dup_hash_number(unsigned char tag, uint64_t number)
{
    uint64_t value = UINT64_C(1469598103934665603) ^ tag;
    int shift;
    for (shift = 0; shift < 64; shift += 8) {
        value ^= (unsigned char)(number >> shift);
        value *= UINT64_C(1099511628211);
    }
    return value;
}


static int
dup_identifier_start(unsigned char value)
{
    return value == '_' || (value >= 'A' && value <= 'Z') ||
           (value >= 'a' && value <= 'z') || value >= 128;
}


static int
dup_identifier_byte(unsigned char value)
{
    return dup_identifier_start(value) || (value >= '0' && value <= '9');
}


static int
dup_keyword(const char *word, Py_ssize_t length, int language)
{
    static const char *const python_keywords[] = {
        "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "case", "class", "continue", "def", "del", "elif", "else",
        "except", "finally", "for", "from", "global", "if", "import", "in",
        "is", "lambda", "match", "nonlocal", "not", "or", "pass", "raise",
        "return", "try", "while", "with", "yield"
    };
    static const char *const native_keywords[] = {
        "_Alignas", "_Alignof", "_Atomic", "_Bool", "_Complex", "_Generic",
        "_Imaginary", "_Noreturn", "_Static_assert", "_Thread_local", "auto",
        "break", "case", "char", "const", "continue", "default", "do", "double",
        "else", "enum", "extern", "float", "for", "goto", "if", "inline", "int",
        "long", "register", "restrict", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while"
    };
    const char *const *keywords = language == 0 ? python_keywords : native_keywords;
    size_t count = language == 0
        ? sizeof(python_keywords) / sizeof(*python_keywords)
        : sizeof(native_keywords) / sizeof(*native_keywords);
    size_t index;
    for (index = 0; index < count; index++) {
        size_t keyword_length = strlen(keywords[index]);
        if ((Py_ssize_t)keyword_length == length &&
            memcmp(word, keywords[index], keyword_length) == 0) {
            return 1;
        }
    }
    return 0;
}


static int
dup_names_resize(DupNames *names, size_t next_capacity)
{
    DupName *next;
    size_t index;
    if (next_capacity > SIZE_MAX / sizeof(*next)) {
        PyErr_NoMemory();
        return -1;
    }
    next = PyMem_Calloc(next_capacity, sizeof(*next));
    if (next == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (index = 0; index < names->capacity; index++) {
        DupName entry = names->entries[index];
        size_t slot;
        if (!entry.occupied) {
            continue;
        }
        slot = (size_t)entry.hash & (next_capacity - 1);
        while (next[slot].occupied) {
            slot = (slot + 1) & (next_capacity - 1);
        }
        next[slot] = entry;
    }
    PyMem_Free(names->entries);
    names->entries = next;
    names->capacity = next_capacity;
    return 0;
}


static int
dup_name_id(
    DupNames *names,
    const char *word,
    Py_ssize_t length,
    uint32_t *canonical)
{
    uint64_t hash = dup_hash_bytes('n', word, length);
    size_t slot;
    if (names->capacity == 0 && dup_names_resize(names, 64) < 0) {
        return -1;
    }
    if (names->used >= names->capacity / 2 - 1) {
        if (names->capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        if (dup_names_resize(names, names->capacity * 2) < 0) {
            return -1;
        }
    }
    slot = (size_t)hash & (names->capacity - 1);
    while (names->entries[slot].occupied) {
        DupName *entry = &names->entries[slot];
        if (entry->hash == hash && entry->length == length &&
            memcmp(entry->name, word, (size_t)length) == 0) {
            *canonical = entry->canonical;
            return 0;
        }
        slot = (slot + 1) & (names->capacity - 1);
    }
    if (names->next_canonical == UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "fragment body has too many distinct names");
        return -1;
    }
    names->entries[slot].name = word;
    names->entries[slot].length = length;
    names->entries[slot].hash = hash;
    names->entries[slot].canonical = names->next_canonical++;
    names->entries[slot].occupied = 1;
    names->used++;
    *canonical = names->entries[slot].canonical;
    return 0;
}


static int
dup_tape_append(DupTape *tape, uint64_t hash, Py_ssize_t line)
{
    if (tape->count == tape->capacity) {
        Py_ssize_t next_capacity;
        DupToken *next;
        if (tape->capacity > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        next_capacity = tape->capacity == 0 ? 64 : tape->capacity * 2;
        if ((size_t)next_capacity > SIZE_MAX / sizeof(*next)) {
            PyErr_NoMemory();
            return -1;
        }
        next = PyMem_Realloc(tape->tokens, (size_t)next_capacity * sizeof(*next));
        if (next == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        tape->tokens = next;
        tape->capacity = next_capacity;
    }
    tape->tokens[tape->count].hash = hash;
    tape->tokens[tape->count].line = line;
    tape->count++;
    return 0;
}


static Py_ssize_t
dup_quoted_end(const char *source, Py_ssize_t length, Py_ssize_t start, int language)
{
    char quote = source[start];
    Py_ssize_t index = start + 1;
    int triple = language == 0 && start + 2 < length &&
                 source[start + 1] == quote && source[start + 2] == quote;
    if (triple) {
        index += 2;
    }
    while (index < length) {
        if (source[index] == '\\') {
            index += index + 1 < length ? 2 : 1;
            continue;
        }
        if (source[index] == quote) {
            if (!triple) {
                return index + 1;
            }
            if (index + 2 < length && source[index + 1] == quote &&
                source[index + 2] == quote) {
                return index + 3;
            }
        }
        index++;
    }
    return length;
}


static int
dup_tokenize(
    const char *source,
    Py_ssize_t length,
    Py_ssize_t base_line,
    int language,
    int mode,
    DupTape *tape)
{
    Py_ssize_t index = 0;
    Py_ssize_t line = base_line;
    DupNames names = {NULL, 0, 0, 0};
    int attribute_follows = 0;

    while (index < length) {
        unsigned char value = (unsigned char)source[index];
        Py_ssize_t start;
        uint64_t hash;

        if (value == ' ' || value == '\t' || value == '\r' || value == '\n' ||
            value == '\f' || value == '\v') {
            if (value == '\n') {
                line++;
            }
            index++;
            continue;
        }
        if ((language == 0 && value == '#') ||
            (language == 1 && value == '/' && index + 1 < length &&
             source[index + 1] == '/')) {
            while (index < length && source[index] != '\n') {
                index++;
            }
            continue;
        }
        if (language == 1 && value == '/' && index + 1 < length &&
            source[index + 1] == '*') {
            index += 2;
            while (index < length) {
                if (source[index] == '\n') {
                    line++;
                }
                if (index + 1 < length && source[index] == '*' &&
                    source[index + 1] == '/') {
                    index += 2;
                    break;
                }
                index++;
            }
            continue;
        }
        if (value == '\'' || value == '"') {
            start = index;
            index = dup_quoted_end(source, length, start, language);
            if (mode == DUP_ALPHA) {
                hash = dup_hash_bytes('L', source + start, index - start);
            }
            else {
                hash = dup_hash_number('L', 0);
            }
            if (dup_tape_append(tape, hash, line) < 0) {
                goto error;
            }
            while (start < index) {
                if (source[start++] == '\n') {
                    line++;
                }
            }
            attribute_follows = 0;
            continue;
        }
        if (dup_identifier_start(value)) {
            uint32_t canonical;
            start = index++;
            while (index < length &&
                   dup_identifier_byte((unsigned char)source[index])) {
                index++;
            }
            if (dup_keyword(source + start, index - start, language)) {
                hash = dup_hash_bytes('K', source + start, index - start);
            }
            else if (mode == DUP_SHAPE) {
                hash = dup_hash_number('I', 0);
            }
            else if (attribute_follows) {
                hash = dup_hash_bytes('A', source + start, index - start);
            }
            else {
                if (dup_name_id(
                        &names, source + start, index - start, &canonical) < 0) {
                    goto error;
                }
                hash = dup_hash_number('N', canonical);
            }
            if (dup_tape_append(tape, hash, line) < 0) {
                goto error;
            }
            attribute_follows = 0;
            continue;
        }
        if (value >= '0' && value <= '9') {
            start = index++;
            while (index < length &&
                   (dup_identifier_byte((unsigned char)source[index]) ||
                    source[index] == '.')) {
                index++;
            }
            hash = mode == DUP_ALPHA
                ? dup_hash_bytes('L', source + start, index - start)
                : dup_hash_number('L', 0);
            if (dup_tape_append(tape, hash, line) < 0) {
                goto error;
            }
            attribute_follows = 0;
            continue;
        }

        start = index++;
        if (index < length) {
            char next = source[index];
            int pair = (value == '-' && next == '>') || value == next ||
                       ((value == '<' || value == '>' || value == '!' || value == '=' ||
                         value == '+' || value == '-' || value == '*' || value == '/' ||
                         value == '%' || value == '&' || value == '|' || value == '^' ||
                         value == ':') && next == '=') ||
                       (value == ':' && next == ':') ||
                       (value == '*' && next == '*') ||
                       (value == '/' && next == '/');
            if (pair) {
                index++;
            }
        }
        hash = dup_hash_bytes('O', source + start, index - start);
        if (dup_tape_append(tape, hash, line) < 0) {
            goto error;
        }
        attribute_follows = source[start] == '.' ||
                            (index - start == 2 && source[start] == '-' &&
                             source[start + 1] == '>');
    }

    PyMem_Free(names.entries);
    return 0;

error:
    PyMem_Free(names.entries);
    return -1;
}


static uint64_t
dup_window_hash(const DupToken *tokens, Py_ssize_t count)
{
    uint64_t hash = 0;
    Py_ssize_t index;
    for (index = 0; index < count; index++) {
        hash = hash * DUP_HASH_BASE + tokens[index].hash;
    }
    return hash;
}


static int
dup_window_equal(
    const DupTape *left,
    Py_ssize_t left_start,
    const DupTape *right,
    Py_ssize_t right_start,
    Py_ssize_t count,
    uint64_t *work)
{
    Py_ssize_t index;
    for (index = 0; index < count; index++) {
        (*work)++;
        if (left->tokens[left_start + index].hash !=
            right->tokens[right_start + index].hash) {
            return 0;
        }
    }
    return 1;
}


static uint64_t
dup_range_hash(const DupTape *tape, Py_ssize_t start, Py_ssize_t count,
               const uint64_t *powers)
{
    return tape->prefix[start + count] - tape->prefix[start] * powers[count];
}


static Py_ssize_t
dup_match_length(
    const DupTape *left,
    Py_ssize_t left_start,
    const DupTape *right,
    Py_ssize_t right_start,
    Py_ssize_t known,
    const uint64_t *powers,
    uint64_t *work)
{
    Py_ssize_t maximum = left->count - left_start;
    Py_ssize_t right_remaining = right->count - right_start;
    Py_ssize_t low = known;
    Py_ssize_t high;
    Py_ssize_t scalar_end;
    if (right_remaining < maximum) {
        maximum = right_remaining;
    }
    scalar_end = known + 16 < maximum ? known + 16 : maximum;
    while (low < scalar_end &&
           left->tokens[left_start + low].hash ==
           right->tokens[right_start + low].hash) {
        (*work)++;
        low++;
    }
    if (low < scalar_end) {
        (*work)++;
    }
    if (low < scalar_end || low == maximum) {
        return low;
    }

    /* Long equal runs are where a scalar extension becomes quadratic on
     * repetitive source.  Prefix images turn each remaining LCP query into a
     * logarithmic number of constant-time range comparisons. */
    high = maximum;
    while (low < high) {
        Py_ssize_t middle = low + (high - low + 1) / 2;
        (*work)++;
        if (dup_range_hash(left, left_start, middle, powers) ==
            dup_range_hash(right, right_start, middle, powers)) {
            low = middle;
        }
        else {
            high = middle - 1;
        }
    }
    return low;
}


static int
dup_append_match(
    PyObject *results,
    Py_ssize_t left_body,
    Py_ssize_t left_start_line,
    Py_ssize_t left_end_line,
    Py_ssize_t right_body,
    Py_ssize_t right_start_line,
    Py_ssize_t right_end_line,
    Py_ssize_t tokens)
{
    PyObject *record = Py_BuildValue(
        "(nnnnnnn)",
        left_body,
        left_start_line,
        left_end_line,
        right_body,
        right_start_line,
        right_end_line,
        tokens
    );
    int appended;
    if (record == NULL) {
        return -1;
    }
    appended = PyList_Append(results, record);
    Py_DECREF(record);
    return appended;
}


static PyObject *
dupscan_scan(PyObject *Py_UNUSED(module), PyObject *args)
{
    PyObject *body_objects;
    Py_ssize_t min_lines;
    Py_ssize_t min_tokens;
    int mode = DUP_SHAPE;
    int report_work = 0;
    PyObject *fast_bodies = NULL;
    DupTape *tapes = NULL;
    Py_ssize_t body_count;
    Py_ssize_t body_index;
    size_t total_windows = 0;
    size_t wanted_capacity;
    size_t table_capacity = 1;
    DupWindow *windows = NULL;
    uint64_t *powers = NULL;
    Py_ssize_t maximum_tokens = 0;
    PyObject *results = NULL;
    PyObject *answer;
    uint64_t work = 0;

    if (!PyArg_ParseTuple(
            args, "Onn|ip:scan", &body_objects, &min_lines, &min_tokens,
            &mode, &report_work)) {
        return NULL;
    }
    if (min_lines < 1) {
        PyErr_SetString(PyExc_ValueError, "fragment min_lines must be at least 1");
        return NULL;
    }
    if (min_tokens < 1) {
        PyErr_SetString(PyExc_ValueError, "fragment min_tokens must be at least 1");
        return NULL;
    }
    if (mode != DUP_SHAPE && mode != DUP_ALPHA) {
        PyErr_SetString(PyExc_ValueError, "fragment normalization must be 0 (shape) or 1 (alpha)");
        return NULL;
    }
    fast_bodies = PySequence_Fast(body_objects, "fragment bodies must be a sequence");
    if (fast_bodies == NULL) {
        return NULL;
    }
    body_count = PySequence_Fast_GET_SIZE(fast_bodies);
    tapes = PyMem_Calloc((size_t)body_count, sizeof(*tapes));
    if (tapes == NULL && body_count > 0) {
        Py_DECREF(fast_bodies);
        PyErr_NoMemory();
        return NULL;
    }

    for (body_index = 0; body_index < body_count; body_index++) {
        PyObject *body = PySequence_Fast_GET_ITEM(fast_bodies, body_index);
        PyObject *fast_body = PySequence_Fast(
            body, "each fragment body must be (source, start_line, language)"
        );
        PyObject *source_object;
        Py_ssize_t start_line;
        Py_ssize_t language;
        char *source;
        Py_ssize_t source_length;
        if (fast_body == NULL) {
            goto error;
        }
        if (PySequence_Fast_GET_SIZE(fast_body) != 3) {
            Py_DECREF(fast_body);
            PyErr_Format(
                PyExc_ValueError,
                "fragment body %zd must contain source, start_line, and language",
                body_index
            );
            goto error;
        }
        source_object = PySequence_Fast_GET_ITEM(fast_body, 0);
        if (!PyBytes_Check(source_object)) {
            Py_DECREF(fast_body);
            PyErr_Format(PyExc_TypeError, "fragment body %zd source must be bytes", body_index);
            goto error;
        }
        start_line = PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(fast_body, 1));
        if (start_line == -1 && PyErr_Occurred()) {
            Py_DECREF(fast_body);
            goto error;
        }
        language = PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(fast_body, 2));
        if (language == -1 && PyErr_Occurred()) {
            Py_DECREF(fast_body);
            goto error;
        }
        if (start_line < 1) {
            Py_DECREF(fast_body);
            PyErr_Format(PyExc_ValueError, "fragment body %zd start_line must be at least 1", body_index);
            goto error;
        }
        if (language != 0 && language != 1) {
            Py_DECREF(fast_body);
            PyErr_Format(PyExc_ValueError, "fragment body %zd language must be 0 (python) or 1 (native)", body_index);
            goto error;
        }
        source = PyBytes_AS_STRING(source_object);
        source_length = PyBytes_GET_SIZE(source_object);
        if (dup_tokenize(
                source, source_length, start_line, (int)language, mode,
                &tapes[body_index]) < 0) {
            Py_DECREF(fast_body);
            goto error;
        }
        Py_DECREF(fast_body);
        work += (uint64_t)tapes[body_index].count;
        if (tapes[body_index].count > maximum_tokens) {
            maximum_tokens = tapes[body_index].count;
        }
        if (tapes[body_index].count >= min_tokens) {
            size_t count = (size_t)(tapes[body_index].count - min_tokens + 1);
            if (count > SIZE_MAX - total_windows) {
                PyErr_NoMemory();
                goto error;
            }
            total_windows += count;
        }
    }

    if (total_windows > (SIZE_MAX - 1) / 2) {
        PyErr_NoMemory();
        goto error;
    }
    wanted_capacity = total_windows * 2 + 1;
    while (table_capacity < wanted_capacity) {
        if (table_capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            goto error;
        }
        table_capacity *= 2;
    }
    if (table_capacity > SIZE_MAX / sizeof(*windows)) {
        PyErr_NoMemory();
        goto error;
    }
    if ((size_t)maximum_tokens + 1 > SIZE_MAX / sizeof(*powers)) {
        PyErr_NoMemory();
        goto error;
    }
    powers = PyMem_Malloc(((size_t)maximum_tokens + 1) * sizeof(*powers));
    windows = PyMem_Calloc(table_capacity, sizeof(*windows));
    results = PyList_New(0);
    if (powers == NULL || windows == NULL || results == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    powers[0] = 1;
    for (body_index = 0; body_index < maximum_tokens; body_index++) {
        powers[body_index + 1] = powers[body_index] * DUP_HASH_BASE;
    }
    for (body_index = 0; body_index < body_count; body_index++) {
        DupTape *tape = &tapes[body_index];
        Py_ssize_t token_index;
        if ((size_t)tape->count + 1 > SIZE_MAX / sizeof(*tape->prefix)) {
            PyErr_NoMemory();
            goto error;
        }
        tape->prefix = PyMem_Malloc(
            ((size_t)tape->count + 1) * sizeof(*tape->prefix)
        );
        if (tape->prefix == NULL) {
            PyErr_NoMemory();
            goto error;
        }
        tape->prefix[0] = 0;
        for (token_index = 0; token_index < tape->count; token_index++) {
            tape->prefix[token_index + 1] =
                tape->prefix[token_index] * DUP_HASH_BASE + tape->tokens[token_index].hash;
        }
    }

    for (body_index = 0; body_index < body_count; body_index++) {
        DupTape *current = &tapes[body_index];
        Py_ssize_t start;
        Py_ssize_t window_count;
        uint64_t rolling;
        uint64_t window_power;
        if (current->count < min_tokens) {
            continue;
        }
        window_count = current->count - min_tokens + 1;
        window_power = powers[min_tokens - 1];
        rolling = dup_window_hash(current->tokens, min_tokens);
        for (start = 0; start < window_count; start++) {
            size_t slot;
            DupWindow *stored;
            if (start > 0) {
                rolling = (rolling - current->tokens[start - 1].hash * window_power) *
                          DUP_HASH_BASE + current->tokens[start + min_tokens - 1].hash;
            }
            work++;
            slot = (size_t)rolling & (table_capacity - 1);
            while (windows[slot].occupied && windows[slot].hash != rolling) {
                slot = (slot + 1) & (table_capacity - 1);
            }
            stored = &windows[slot];
            if (stored->occupied) {
                DupTape *left = &tapes[stored->body];
                Py_ssize_t left_start = stored->start;
                if (dup_window_equal(
                        left, left_start, current, start, min_tokens, &work) &&
                    !(stored->body == body_index &&
                      start < left_start + min_tokens && left_start < start + min_tokens)) {
                    Py_ssize_t matched;
                    int has_equal_previous = left_start > 0 && start > 0 &&
                        left->tokens[left_start - 1].hash == current->tokens[start - 1].hash;
                    if (!has_equal_previous) {
                        Py_ssize_t left_end;
                        Py_ssize_t right_end;
                        Py_ssize_t left_lines;
                        Py_ssize_t right_lines;
                        matched = dup_match_length(
                            left, left_start, current, start, min_tokens, powers, &work
                        );
                        left_end = left_start + matched - 1;
                        right_end = start + matched - 1;
                        left_lines = left->tokens[left_end].line -
                                     left->tokens[left_start].line + 1;
                        right_lines = current->tokens[right_end].line -
                                      current->tokens[start].line + 1;
                        if (left_lines >= min_lines && right_lines >= min_lines &&
                            !(left_start == 0 && matched == left->count && start == 0 &&
                              matched == current->count) &&
                            !(stored->body == body_index && start <= left_end)) {
                            if (dup_append_match(
                                    results,
                                    stored->body,
                                    left->tokens[left_start].line,
                                    left->tokens[left_end].line,
                                    body_index,
                                    current->tokens[start].line,
                                    current->tokens[right_end].line,
                                    matched) < 0) {
                                goto error;
                            }
                        }
                    }
                }
            }
            else {
                stored->hash = rolling;
                stored->body = body_index;
                stored->start = start;
                stored->occupied = 1;
            }
        }
    }

    for (body_index = 0; body_index < body_count; body_index++) {
        PyMem_Free(tapes[body_index].tokens);
        PyMem_Free(tapes[body_index].prefix);
    }
    PyMem_Free(tapes);
    PyMem_Free(windows);
    PyMem_Free(powers);
    Py_DECREF(fast_bodies);
    if (!report_work) {
        return results;
    }
    answer = Py_BuildValue("(NK)", results, work);
    return answer;

error:
    if (tapes != NULL) {
        for (body_index = 0; body_index < body_count; body_index++) {
            PyMem_Free(tapes[body_index].tokens);
            PyMem_Free(tapes[body_index].prefix);
        }
    }
    PyMem_Free(tapes);
    PyMem_Free(windows);
    PyMem_Free(powers);
    Py_XDECREF(results);
    Py_XDECREF(fast_bodies);
    return NULL;
}


static PyMethodDef dupscan_methods[] = {
    {
        "scan",
        dupscan_scan,
        METH_VARARGS,
        "scan(bodies, min_lines, min_tokens, normalization=0, report_work=False)"
    },
    {NULL, NULL, 0, NULL}
};


static PyModuleDef dupscan_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._dupscan",
    .m_doc = "Native token-window duplicate-fragment detection for Wreath.",
    .m_size = 0,
    .m_methods = dupscan_methods,
};


PyMODINIT_FUNC
PyInit__dupscan(void)
{
    return PyModule_Create(&dupscan_module);
}
