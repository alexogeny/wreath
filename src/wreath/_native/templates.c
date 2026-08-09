/* wreath._native._core: template tape executor.
 *
 * Executes the flat opcode tape compiled by wreath._pure.templates.compile_tape,
 * producing byte-identical UTF-8 to wreath._pure.templates.render_tape. Parsing
 * and jump resolution stay in Python; only request-time rendering is here.
 */
#include "wreathcore.h"

#include "simd.h"

#include <stdatomic.h>

#define template_render_chunks 256  /* tape instructions between cancellation checks */

/* Owned references installed by template_configure(); the module never imports
 * wreath.templates itself. */
static PyObject *T_Markup = NULL;      /* the Markup type object */
static PyObject *RenderError = NULL;   /* TemplateRenderError */
static const WreathRecordCAPI *record_capi = NULL;

/* Opcodes -- must match wreath._pure.templates. */
#define OP_TEXT 0
#define OP_VAR 1
#define OP_FOR 2
#define OP_ENDFOR 3
#define OP_IF 4
#define OP_JUMP 5
#define OP_ENDIF 6

#define MAX_LOOP_DEPTH 64

typedef struct {
    PyObject *owner;  /* owned bytes object whose writable storage is data */
    char *data;
    Py_ssize_t len;
    Py_ssize_t cap;
    Py_ssize_t max;
    int overflow; /* set when an append would exceed max; no Python error set */
} outbuf;

typedef struct {
    PyObject *iterator;   /* owned, or NULL for an exact list/tuple */
    PyObject *sequence;   /* owned exact list/tuple, or NULL */
    PyObject *current;    /* owned: the loop variable's current value */
    Py_ssize_t body_start;
    Py_ssize_t position;
    Py_ssize_t length;
    int sequence_kind;   /* 1 list, 2 tuple, 3 native RecordBatch */
} loop_frame;

typedef struct {
    PyObject *names;  /* owned for this render; NULL until a Record is seen */
    Py_ssize_t position;
} lookup_cache;

/* One tape instruction with its operands already out of their Python objects.
 *
 * The tape is a tuple of tuples of Python ints, and a loop body is executed
 * once per row: rendering the Fortunes document walked ~80 instructions out of
 * a tape of 11, so the same `PyLong_AsLong` ran seven times over for every
 * opcode, jump target and line number in the body. That showed up as 6.9% of
 * the render's cycles across `PyLong_AsLong` and `PyLong_AsSsize_t`, second
 * only to the renderer itself.
 *
 * Decoding is O(tape) once per Template and execution is O(instructions
 * executed). Every pointer is borrowed from the tape, which the compiled
 * program capsule owns.
 *
 * Unknown opcodes are decoded as themselves with no operands, so an invalid one
 * still raises where it is *reached* rather than where it is decoded. */
typedef struct {
    int op;
    int line;
    Py_ssize_t target;  /* OP_IF else-branch, OP_JUMP destination, OP_FOR end */
    PyObject *first;    /* borrowed: text fragment, lookup path, or loop var */
    PyObject *path;     /* borrowed: OP_FOR's iterable path */
    int binding;        /* lexical loop-frame slot, or -1 for the context */
} decoded;

typedef struct {
    PyObject *tape;       /* owned: decoded operands borrow from it */
    decoded *program;    /* owned */
    Py_ssize_t length;
    _Atomic Py_ssize_t output_hint;
} compiled_template;

#define TEMPLATE_CAPSULE_NAME "wreath.template_program"

#define TAPE_STACK_SLOTS 64  /* tapes at or below this decode without malloc */

static int
decode_tape(PyObject *tape, Py_ssize_t n, decoded *out)
{
    PyObject *scope_vars[MAX_LOOP_DEPTH];
    int scope_depth = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *instr = PyTuple_GET_ITEM(tape, i);
        decoded *slot = &out[i];
        slot->line = 0;
        slot->target = 0;
        slot->first = NULL;
        slot->path = NULL;
        slot->binding = -1;
        long op = PyLong_AsLong(PyTuple_GET_ITEM(instr, 0));
        if (op == -1 && PyErr_Occurred()) {
            return -1;
        }
        slot->op = (int)op;
        switch (op) {
        case OP_TEXT:
            slot->first = PyTuple_GET_ITEM(instr, 1);
            break;
        case OP_VAR:
            slot->first = PyTuple_GET_ITEM(instr, 1);
            slot->line = (int)PyLong_AsLong(PyTuple_GET_ITEM(instr, 2));
            break;
        case OP_IF:
            slot->first = PyTuple_GET_ITEM(instr, 1);
            slot->target = PyLong_AsSsize_t(PyTuple_GET_ITEM(instr, 2));
            slot->line = (int)PyLong_AsLong(PyTuple_GET_ITEM(instr, 3));
            break;
        case OP_JUMP:
            slot->target = PyLong_AsSsize_t(PyTuple_GET_ITEM(instr, 1));
            break;
        case OP_FOR:
            slot->first = PyTuple_GET_ITEM(instr, 1);
            slot->path = PyTuple_GET_ITEM(instr, 2);
            slot->target = PyLong_AsSsize_t(PyTuple_GET_ITEM(instr, 3));
            slot->line = (int)PyLong_AsLong(PyTuple_GET_ITEM(instr, 4));
            break;
        default:
            break;  /* ENDIF and ENDFOR carry no operands; unknown ops none. */
        }
        if (PyErr_Occurred()) {
            return -1;
        }

        PyObject *lookup = NULL;
        if (op == OP_VAR || op == OP_IF) {
            lookup = slot->first;
        } else if (op == OP_FOR) {
            /* The iterable is evaluated outside the new loop variable's
             * scope: `{% for x in x.children %}` sees the outer x. */
            lookup = slot->path;
        }
        if (lookup != NULL && PyTuple_GET_SIZE(lookup) > 0) {
            PyObject *root = PyTuple_GET_ITEM(lookup, 0);
            for (int depth = scope_depth - 1; depth >= 0; depth--) {
                int equal = PyObject_RichCompareBool(root, scope_vars[depth], Py_EQ);
                if (equal < 0) {
                    return -1;
                }
                if (equal) {
                    slot->binding = depth;
                    break;
                }
            }
        }
        if (op == OP_FOR && scope_depth < MAX_LOOP_DEPTH) {
            scope_vars[scope_depth++] = slot->first;
        } else if (op == OP_ENDFOR && scope_depth > 0) {
            scope_depth--;
        }
    }
    return 0;
}

static int
outbuf_reserve(outbuf *b, Py_ssize_t extra)
{
    if (b->len + extra <= b->cap) {
        return 0;
    }
    Py_ssize_t newcap = b->cap ? b->cap : 256;
    while (newcap < b->len + extra) {
        newcap *= 2;
    }
    if (b->owner == NULL) {
        b->owner = PyBytes_FromStringAndSize(NULL, newcap);
        if (b->owner == NULL) {
            return -1;
        }
    } else if (_PyBytes_Resize(&b->owner, newcap) < 0) {
        b->data = NULL;
        b->cap = 0;
        return -1;
    }
    b->data = PyBytes_AS_STRING(b->owner);
    b->cap = newcap;
    return 0;
}

static int
outbuf_append(outbuf *b, const char *src, Py_ssize_t n)
{
    if (b->len + n > b->max) {
        b->overflow = 1;
        return -1;
    }
    if (outbuf_reserve(b, n) < 0) {
        return -1;
    }
    memcpy(b->data + b->len, src, (size_t)n);
    b->len += n;
    return 0;
}

/* Append UTF-8 bytes, replacing the five HTML-significant ASCII characters.
 * Those bytes never occur inside a multibyte UTF-8 sequence, so scanning by
 * byte is safe. */
static int
append_escaped(outbuf *b, const char *s, Py_ssize_t n)
{
    Py_ssize_t i = 0;
    while (i < n) {
        /* Interpolated text is overwhelmingly free of the five: find the next
         * one a register at a time and copy everything before it in one go,
         * rather than deciding per byte. */
        Py_ssize_t start = i;
        const char *rep;
        Py_ssize_t rlen;
        i += (Py_ssize_t)wreath_html_run(s + i, (ptrdiff_t)(n - i));
        if (i > start && outbuf_append(b, s + start, i - start) < 0) {
            return -1;
        }
        if (i == n) {
            break;
        }
        switch (s[i]) {
        case '&': rep = "&amp;"; rlen = 5; break;
        case '<': rep = "&lt;"; rlen = 4; break;
        case '>': rep = "&gt;"; rlen = 4; break;
        case '"': rep = "&#34;"; rlen = 5; break;
        default: rep = "&#39;"; rlen = 5; break;
        }
        /* Entities are four or five bytes and there is one per special, so
         * dense text pays this far more often than it pays the scan. Copy the
         * fixed width directly instead of calling out to a general append that
         * re-derives the bound and dispatches to `memcpy` for five bytes. */
        if (b->len + rlen > b->max) {
            b->overflow = 1;
            return -1;
        }
        if (b->len + 5 > b->cap && outbuf_reserve(b, 5) < 0) {
            return -1;
        }
        memcpy(b->data + b->len, rep, 5);
        b->len += rlen;
        i++;
    }
    return 0;
}

static void
raise_render(int line, PyObject *message)
{
    if (message == NULL) {
        return;
    }
    PyObject *kwargs = Py_BuildValue("{s:i}", "line", line);
    PyObject *args = PyTuple_Pack(1, message);
    if (kwargs != NULL && args != NULL) {
        PyObject *exc = PyObject_Call(RenderError, args, kwargs);
        if (exc != NULL) {
            PyErr_SetObject(RenderError, exc);
            Py_DECREF(exc);
        }
    }
    Py_XDECREF(args);
    Py_XDECREF(kwargs);
    Py_DECREF(message);
}

/* Resolve a dotted lookup path against ``context``; returns a new reference or
 * NULL with a Python error set. Mirrors wreath._pure.templates._lookup. */
static PyObject *
lookup_path(PyObject *context, PyObject *path, int line,
            const loop_frame *frames, int binding, lookup_cache *cache)
{
    Py_ssize_t count = PyTuple_GET_SIZE(path);
    Py_ssize_t start = 0;
    PyObject *current = context; /* borrowed until first hop */
    const loop_frame *bound_frame = NULL;
    if (binding >= 0) {
        bound_frame = &frames[binding];
        if (bound_frame->sequence_kind == 3 && count == 1) {
            PyObject *row = record_capi->batch_get_borrowed(
                bound_frame->sequence, bound_frame->position);
            return row == NULL ? NULL : Py_NewRef(row);
        }
        current = bound_frame->current;
        start = 1;  /* the frame already resolved the root loop variable */
    }
    PyObject *owned = NULL;
    for (Py_ssize_t i = start; i < count; i++) {
        PyObject *segment = PyTuple_GET_ITEM(path, i);
        PyObject *found = NULL;
        int record_missed = 0;
        if (bound_frame != NULL && bound_frame->sequence_kind == 3 &&
            i == start) {
            PyObject *borrowed = NULL;
            int batch_result = record_capi->batch_get_value(
                bound_frame->sequence, bound_frame->position, segment,
                &cache->names, &cache->position, &borrowed);
            if (batch_result < 0) return NULL;
            if (batch_result == 1) {
                found = Py_NewRef(borrowed);
            } else if (batch_result == 2) {
                record_missed = 1;
            } else {
                current = record_capi->batch_get_borrowed(
                    bound_frame->sequence, bound_frame->position);
                if (current == NULL) return NULL;
            }
        }
        /* Fast path: most contexts and nested nodes are dicts. A direct hash
         * lookup skips the subscript protocol and, on a miss, avoids raising
         * and clearing a KeyError before the attribute fallback. Semantics
         * stay identical to the generic path (miss falls through to getattr). */
        if (found == NULL && !record_missed && record_capi != NULL &&
            record_capi->version == WREATH_RECORD_CAPI_VERSION) {
            PyObject *borrowed = NULL;
            int record_result = record_capi->get_borrowed(
                current, segment, &cache->names, &cache->position, &borrowed);
            if (record_result < 0) {
                Py_XDECREF(owned);
                return NULL;
            }
            if (record_result == 1) {
                found = Py_NewRef(borrowed);
            } else if (record_result == 2) {
                record_missed = 1;
            }
        }
        if (found != NULL) {
            /* resolved by the exact native Record bridge */
        } else if (!record_missed && PyDict_Check(current)) {
            PyObject *borrowed = PyDict_GetItemWithError(current, segment);
            if (borrowed != NULL) {
                found = Py_NewRef(borrowed);
            } else if (PyErr_Occurred()) {
                Py_XDECREF(owned);
                return NULL;
            }
        } else if (!record_missed) {
            found = PyObject_GetItem(current, segment);
            if (found == NULL) {
                if (PyErr_ExceptionMatches(PyExc_KeyError) ||
                    PyErr_ExceptionMatches(PyExc_TypeError) ||
                    PyErr_ExceptionMatches(PyExc_IndexError)) {
                    PyErr_Clear();
                } else {
                    Py_XDECREF(owned);
                    return NULL;
                }
            }
        }
        if (found == NULL) {
            /* Subscript missed; mirror the pure lookup's attribute fallback. */
            found = PyObject_GetAttr(current, segment);
            if (found == NULL) {
                if (PyErr_ExceptionMatches(PyExc_AttributeError)) {
                    PyErr_Clear();
                    PyObject *message;
                    if (i == 0) {
                        message = PyUnicode_FromFormat("%R is undefined", segment);
                    } else {
                        PyObject *joiner = PyUnicode_FromString(".");
                        PyObject *joined =
                            joiner ? PyUnicode_Join(joiner, path) : NULL;
                        Py_XDECREF(joiner);
                        message = joined ? PyUnicode_FromFormat(
                                               "cannot resolve %R at %R",
                                               joined, segment)
                                         : NULL;
                        Py_XDECREF(joined);
                    }
                    raise_render(line, message);
                }
                Py_XDECREF(owned);
                return NULL;
            }
        }
        Py_XDECREF(owned);
        owned = found;
        current = found;
    }
    return owned != NULL ? owned : Py_NewRef(current);
}

static int
emit_value(outbuf *b, PyObject *value)
{
    if (Py_TYPE(value) == (PyTypeObject *)T_Markup) {
        Py_ssize_t n;
        const char *s = PyUnicode_AsUTF8AndSize(value, &n);
        if (s == NULL) {
            return -1;
        }
        return outbuf_append(b, s, n);
    }
    if (PyUnicode_CheckExact(value)) {
        Py_ssize_t n;
        const char *s = PyUnicode_AsUTF8AndSize(value, &n);
        if (s == NULL) {
            return -1;
        }
        return append_escaped(b, s, n);
    }
    /* An exact `int` is decimal digits, and no digit is one of the five
     * characters escaping replaces -- so `str()` would allocate a `str`, format
     * into it, hand back its UTF-8, be scanned for specials that cannot be
     * there, and be freed again. Formatting straight into the output buffer
     * skips all five.
     *
     * Exact, not `PyLong_Check`: the contract is `str(value)`, and `bool` and
     * every `int` subclass may answer that differently -- `str(True)` is
     * "True", not "1". A value wider than a C long falls through to the
     * general path, which is where arbitrary precision belongs. */
    if (PyLong_CheckExact(value)) {
        int overflowed = 0;
        long number = PyLong_AsLongAndOverflow(value, &overflowed);
        if (number == -1 && PyErr_Occurred()) {
            return -1;  /* -1 is also a legal value; the sentinel needs both */
        }
        if (!overflowed) {
            /* Write backwards from the end.  libc's general printf parser is
             * considerable machinery for one base-ten signed integer, and
             * Fortunes exercises this once per row.  Negating LONG_MIN is
             * undefined, so form the magnitude through the -(n + 1) identity. */
            char digits[3 * sizeof(long) + 3];
            char *end = digits + sizeof(digits);
            char *cursor = end;
            unsigned long magnitude = number < 0
                ? (unsigned long)(-(number + 1)) + 1UL
                : (unsigned long)number;
            do {
                *--cursor = (char)('0' + magnitude % 10UL);
                magnitude /= 10UL;
            } while (magnitude != 0);
            if (number < 0) {
                *--cursor = '-';
            }
            return outbuf_append(b, cursor, (Py_ssize_t)(end - cursor));
        }
    }
    PyObject *text = PyObject_Str(value);
    if (text == NULL) {
        return -1;
    }
    Py_ssize_t n;
    const char *s = PyUnicode_AsUTF8AndSize(text, &n);
    if (s == NULL) {
        Py_DECREF(text);
        return -1;
    }
    int rc = append_escaped(b, s, n);
    Py_DECREF(text);
    return rc;
}

static PyObject *
render_program(const decoded *program, Py_ssize_t n, PyObject *context,
               Py_ssize_t max_output, _Atomic Py_ssize_t *output_hint)
{
    outbuf buf = {NULL, NULL, 0, 0, max_output, 0};
    if (output_hint != NULL && max_output > 0) {
        Py_ssize_t initial = atomic_load_explicit(output_hint, memory_order_relaxed);
        if (initial > max_output) {
            initial = max_output;
        }
        if (initial > 0 && outbuf_reserve(&buf, initial) < 0) {
            return NULL;
        }
    }
    loop_frame frames[MAX_LOOP_DEPTH];
    Py_ssize_t depth = 0;
    Py_ssize_t ip = 0;
    int failed = 0;
    lookup_cache stack_caches[TAPE_STACK_SLOTS];
    lookup_cache *caches = stack_caches;
    if (n > TAPE_STACK_SLOTS) {
        caches = PyMem_Calloc((size_t)n, sizeof(lookup_cache));
        if (caches == NULL) {
            Py_XDECREF(buf.owner);
            return PyErr_NoMemory();
        }
    } else {
        memset(caches, 0, (size_t)n * sizeof(lookup_cache));
    }
    for (Py_ssize_t cache_index = 0; cache_index < n; cache_index++) {
        caches[cache_index].position = -1;
    }

    while (ip < n) {
        if (ip > 0 && ip % template_render_chunks == 0 && PyErr_CheckSignals() < 0) {
            failed = 1;
            break;
        }
        const decoded *instr = &program[ip];
        int op = instr->op;
        if (op == OP_TEXT) {
            PyObject *fragment = instr->first;
            if (outbuf_append(&buf, PyBytes_AS_STRING(fragment),
                              PyBytes_GET_SIZE(fragment)) < 0) {
                goto overflow_or_error;
            }
            ip++;
        } else if (op == OP_VAR) {
            PyObject *value = lookup_path(context, instr->first, instr->line,
                                          frames, instr->binding, &caches[ip]);
            if (value == NULL) {
                failed = 1;
                break;
            }
            int rc = emit_value(&buf, value);
            Py_DECREF(value);
            if (rc < 0) {
                goto overflow_or_error;
            }
            ip++;
        } else if (op == OP_IF) {
            PyObject *value = lookup_path(context, instr->first, instr->line,
                                          frames, instr->binding, &caches[ip]);
            if (value == NULL) {
                failed = 1;
                break;
            }
            int truth = PyObject_IsTrue(value);
            Py_DECREF(value);
            if (truth < 0) {
                failed = 1;
                break;
            }
            ip = truth ? ip + 1 : instr->target;
        } else if (op == OP_JUMP) {
            ip = instr->target;
        } else if (op == OP_ENDIF) {
            ip++;
        } else if (op == OP_FOR) {
            int line = instr->line;
            PyObject *iterable = lookup_path(context, instr->path, line,
                                             frames, instr->binding, &caches[ip]);
            if (iterable == NULL) {
                failed = 1;
                break;
            }
            PyObject *iterator = NULL;
            PyObject *sequence = NULL;
            PyObject *item = NULL;
            Py_ssize_t length = 0;
            int sequence_kind = 0;
            if (PyList_CheckExact(iterable)) {
                sequence = iterable;
                sequence_kind = 1;
                length = PyList_GET_SIZE(sequence);
                if (length > 0) {
                    item = Py_NewRef(PyList_GET_ITEM(sequence, 0));
                }
            } else if (PyTuple_CheckExact(iterable)) {
                sequence = iterable;
                sequence_kind = 2;
                length = PyTuple_GET_SIZE(sequence);
                if (length > 0) {
                    item = Py_NewRef(PyTuple_GET_ITEM(sequence, 0));
                }
            } else if (record_capi != NULL &&
                       record_capi->batch_check(iterable)) {
                sequence = iterable;
                sequence_kind = 3;
                length = record_capi->batch_size(sequence);
                if (length > 0) {
                    item = Py_NewRef(Py_None);
                }
            } else {
                iterator = PyObject_GetIter(iterable);
                Py_DECREF(iterable);
                iterable = NULL;
                if (iterator != NULL) {
                    item = PyIter_Next(iterator);
                }
            }
            if (sequence == NULL && iterator == NULL) {
                if (PyErr_ExceptionMatches(PyExc_TypeError)) {
                    PyErr_Clear();
                    PyObject *joiner = PyUnicode_FromString(".");
                    PyObject *joined =
                        joiner ? PyUnicode_Join(joiner, instr->path) : NULL;
                    Py_XDECREF(joiner);
                    raise_render(line, joined ? PyUnicode_FromFormat(
                                                    "%R is not iterable", joined)
                                              : NULL);
                    Py_XDECREF(joined);
                }
                failed = 1;
                break;
            }
            if (item == NULL) {
                Py_XDECREF(iterator);
                Py_XDECREF(sequence);
                if (PyErr_Occurred()) {
                    failed = 1;
                    break;
                }
                ip = instr->target;
                continue;
            }
            if (depth >= MAX_LOOP_DEPTH) {
                Py_DECREF(item);
                Py_XDECREF(iterator);
                Py_XDECREF(sequence);
                raise_render(0, PyUnicode_FromString(
                                    "template loop nesting too deep"));
                failed = 1;
                break;
            }
            frames[depth].iterator = iterator;
            frames[depth].sequence = sequence;
            frames[depth].current = item;
            frames[depth].body_start = ip + 1;
            frames[depth].position = 0;
            frames[depth].length = length;
            frames[depth].sequence_kind = sequence_kind;
            depth++;
            ip++;
        } else if (op == OP_ENDFOR) {
            loop_frame *frame = &frames[depth - 1];
            PyObject *item;
            if (frame->sequence != NULL) {
                frame->position++;
                Py_ssize_t sequence_length;
                if (frame->sequence_kind == 1) {
                    sequence_length = PyList_GET_SIZE(frame->sequence);
                } else if (frame->sequence_kind == 3) {
                    sequence_length = record_capi->batch_size(frame->sequence);
                } else {
                    sequence_length = frame->length;
                }
                if (frame->position < sequence_length) {
                    PyObject *borrowed;
                    if (frame->sequence_kind == 1) {
                        borrowed = PyList_GET_ITEM(
                            frame->sequence, frame->position);
                    } else if (frame->sequence_kind == 2) {
                        borrowed = PyTuple_GET_ITEM(
                            frame->sequence, frame->position);
                    } else {
                        borrowed = Py_None;
                    }
                    item = Py_NewRef(borrowed);
                } else {
                    item = NULL;
                }
            } else {
                item = PyIter_Next(frame->iterator);
            }
            if (item == NULL) {
                if (PyErr_Occurred()) {
                    failed = 1;
                    break;
                }
                Py_DECREF(frame->current);
                Py_XDECREF(frame->iterator);
                Py_XDECREF(frame->sequence);
                depth--;
                ip++;
            } else {
                Py_SETREF(frame->current, item);
                ip = frame->body_start;
            }
        } else {
            raise_render(0, PyUnicode_FromFormat("invalid opcode %ld", op));
            failed = 1;
            break;
        }
        continue;

    overflow_or_error:
        if (buf.overflow && !PyErr_Occurred()) {
            raise_render(0, PyUnicode_FromString("template output too large"));
        }
        failed = 1;
        break;
    }

    /* Unwind any open loop frames (only non-empty on the error path). */
    while (depth > 0) {
        depth--;
        Py_DECREF(frames[depth].current);
        Py_XDECREF(frames[depth].iterator);
        Py_XDECREF(frames[depth].sequence);
    }

    for (Py_ssize_t cache_index = 0; cache_index < n; cache_index++) {
        Py_XDECREF(caches[cache_index].names);
    }
    if (caches != stack_caches) {
        PyMem_Free(caches);
    }

    if (failed) {
        Py_XDECREF(buf.owner);
        return NULL;
    }
    if (output_hint != NULL) {
        atomic_store_explicit(output_hint, buf.len, memory_order_relaxed);
    }
    if (buf.owner == NULL) {
        return PyBytes_FromStringAndSize("", 0);
    }
    if (buf.len != buf.cap && _PyBytes_Resize(&buf.owner, buf.len) < 0) {
        return NULL;
    }
    return buf.owner;
}

static void
compiled_template_destroy(PyObject *capsule)
{
    compiled_template *compiled =
        PyCapsule_GetPointer(capsule, TEMPLATE_CAPSULE_NAME);
    if (compiled == NULL) {
        PyErr_WriteUnraisable(capsule);
        return;
    }
    Py_DECREF(compiled->tape);
    PyMem_Free(compiled->program);
    PyMem_Free(compiled);
}

/* template_compile(tape) -> opaque program
 *
 * The Python parser has already resolved control-flow targets. Decode its
 * tuple tape once with the Template instead of re-reading every integer and
 * operand on every request. The capsule owns the tape because each decoded
 * instruction deliberately borrows its immutable operands from it. */
PyObject *
wreath_template_compile(PyObject *self, PyObject *tape)
{
    (void)self;
    if (!PyTuple_Check(tape)) {
        PyErr_SetString(PyExc_TypeError, "tape must be a tuple");
        return NULL;
    }
    Py_ssize_t n = PyTuple_GET_SIZE(tape);
    compiled_template *compiled = PyMem_Malloc(sizeof(compiled_template));
    if (compiled == NULL) {
        return PyErr_NoMemory();
    }
    compiled->tape = Py_NewRef(tape);
    compiled->program = NULL;
    compiled->length = n;
    atomic_init(&compiled->output_hint, 256);
    if (n > 0) {
        compiled->program = PyMem_Malloc((size_t)n * sizeof(decoded));
        if (compiled->program == NULL) {
            Py_DECREF(compiled->tape);
            PyMem_Free(compiled);
            return PyErr_NoMemory();
        }
        if (decode_tape(tape, n, compiled->program) < 0) {
            Py_DECREF(compiled->tape);
            PyMem_Free(compiled->program);
            PyMem_Free(compiled);
            return NULL;
        }
    }
    PyObject *capsule = PyCapsule_New(compiled, TEMPLATE_CAPSULE_NAME,
                                      compiled_template_destroy);
    if (capsule == NULL) {
        Py_DECREF(compiled->tape);
        PyMem_Free(compiled->program);
        PyMem_Free(compiled);
    }
    return capsule;
}

/* template_render_compiled(program, context, max_output) -> bytes */
PyObject *
wreath_template_render_compiled(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *capsule;
    PyObject *context;
    Py_ssize_t max_output;
    if (!PyArg_ParseTuple(args, "OOn", &capsule, &context, &max_output)) {
        return NULL;
    }
    if (RenderError == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "templates not configured");
        return NULL;
    }
    if (!PyDict_Check(context)) {
        PyErr_SetString(PyExc_TypeError, "context must be a dict");
        return NULL;
    }
    compiled_template *compiled =
        PyCapsule_GetPointer(capsule, TEMPLATE_CAPSULE_NAME);
    if (compiled == NULL) {
        return NULL;
    }
    return render_program(compiled->program, compiled->length, context,
                          max_output, &compiled->output_hint);
}

/* template_render(tape, context, max_output) -> bytes */
PyObject *
wreath_template_render(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *tape;
    PyObject *context;
    Py_ssize_t max_output;
    if (!PyArg_ParseTuple(args, "OOn", &tape, &context, &max_output)) {
        return NULL;
    }
    if (RenderError == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "templates not configured");
        return NULL;
    }
    if (!PyTuple_Check(tape)) {
        PyErr_SetString(PyExc_TypeError, "tape must be a tuple");
        return NULL;
    }
    if (!PyDict_Check(context)) {
        PyErr_SetString(PyExc_TypeError, "context must be a dict");
        return NULL;
    }

    Py_ssize_t n = PyTuple_GET_SIZE(tape);
    decoded stack_program[TAPE_STACK_SLOTS];
    decoded *program = stack_program;
    if (n > TAPE_STACK_SLOTS) {
        program = PyMem_Malloc((size_t)n * sizeof(decoded));
        if (program == NULL) {
            return PyErr_NoMemory();
        }
    }
    if (decode_tape(tape, n, program) < 0) {
        if (program != stack_program) {
            PyMem_Free(program);
        }
        return NULL;
    }
    PyObject *result = render_program(program, n, context, max_output, NULL);
    if (program != stack_program) {
        PyMem_Free(program);
    }
    return result;
}

/* template_configure(markup_type, render_error_type) -> None */
PyObject *
wreath_template_configure(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *markup;
    PyObject *render_error;
    if (!PyArg_ParseTuple(args, "OO", &markup, &render_error)) {
        return NULL;
    }
    Py_XSETREF(T_Markup, Py_NewRef(markup));
    Py_XSETREF(RenderError, Py_NewRef(render_error));
    Py_RETURN_NONE;
}

PyObject *
wreath_template_record_configure(PyObject *self, PyObject *capsule)
{
    (void)self;
    const WreathRecordCAPI *candidate = PyCapsule_GetPointer(
        capsule, WREATH_RECORD_CAPI_NAME);
    if (candidate == NULL) {
        return NULL;
    }
    if (candidate->version != WREATH_RECORD_CAPI_VERSION ||
        candidate->get_borrowed == NULL || candidate->batch_check == NULL ||
        candidate->batch_size == NULL || candidate->batch_get_borrowed == NULL ||
        candidate->batch_get_value == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "incompatible Wreath Record C API");
        return NULL;
    }
    record_capi = candidate;
    Py_RETURN_NONE;
}
