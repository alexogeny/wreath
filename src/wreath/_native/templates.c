/* wreath._native._core: template tape executor.
 *
 * Executes the flat opcode tape compiled by wreath._pure.templates.compile_tape,
 * producing byte-identical UTF-8 to wreath._pure.templates.render_tape. Parsing
 * and jump resolution stay in Python; only request-time rendering is here.
 */
#include "wreathcore.h"

#include "simd.h"

#define template_render_chunks 256  /* tape instructions between cancellation checks */

/* Owned references installed by template_configure(); the module never imports
 * wreath.templates itself. */
static PyObject *T_Markup = NULL;      /* the Markup type object */
static PyObject *RenderError = NULL;   /* TemplateRenderError */

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
    char *data;
    Py_ssize_t len;
    Py_ssize_t cap;
    Py_ssize_t max;
    int overflow; /* set when an append would exceed max; no Python error set */
} outbuf;

typedef struct {
    PyObject *iterator;   /* owned */
    PyObject *var;        /* borrowed from the tape */
    Py_ssize_t body_start;
    int had_old;
    PyObject *old_value;  /* owned, or NULL when there was no prior binding */
} loop_frame;

/* One tape instruction with its operands already out of their Python objects.
 *
 * The tape is a tuple of tuples of Python ints, and a loop body is executed
 * once per row: rendering the Fortunes document walked ~80 instructions out of
 * a tape of 11, so the same `PyLong_AsLong` ran seven times over for every
 * opcode, jump target and line number in the body. That showed up as 6.9% of
 * the render's cycles across `PyLong_AsLong` and `PyLong_AsSsize_t`, second
 * only to the renderer itself.
 *
 * Decoding is O(tape) once per render and execution is O(instructions
 * executed), so this pays for any template with a loop in it and costs a tape
 * walk for one without. Every pointer is borrowed from the tape, which the
 * caller holds for the whole render.
 *
 * Unknown opcodes are decoded as themselves with no operands, so an invalid one
 * still raises where it is *reached* rather than where it is decoded. */
typedef struct {
    int op;
    int line;
    Py_ssize_t target;  /* OP_IF else-branch, OP_JUMP destination, OP_FOR end */
    PyObject *first;    /* borrowed: text fragment, lookup path, or loop var */
    PyObject *path;     /* borrowed: OP_FOR's iterable path */
} decoded;

#define TAPE_STACK_SLOTS 64  /* tapes at or below this decode without malloc */

static int
decode_tape(PyObject *tape, Py_ssize_t n, decoded *out)
{
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *instr = PyTuple_GET_ITEM(tape, i);
        decoded *slot = &out[i];
        slot->line = 0;
        slot->target = 0;
        slot->first = NULL;
        slot->path = NULL;
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
    char *grown = PyMem_Realloc(b->data, (size_t)newcap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    b->data = grown;
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
lookup_path(PyObject *context, PyObject *path, int line)
{
    Py_ssize_t count = PyTuple_GET_SIZE(path);
    PyObject *current = context; /* borrowed until first hop */
    PyObject *owned = NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *segment = PyTuple_GET_ITEM(path, i);
        PyObject *found = NULL;
        /* Fast path: most contexts and nested nodes are dicts. A direct hash
         * lookup skips the subscript protocol and, on a miss, avoids raising
         * and clearing a KeyError before the attribute fallback. Semantics
         * stay identical to the generic path (miss falls through to getattr). */
        if (PyDict_Check(current)) {
            PyObject *borrowed = PyDict_GetItemWithError(current, segment);
            if (borrowed != NULL) {
                found = Py_NewRef(borrowed);
            } else if (PyErr_Occurred()) {
                Py_XDECREF(owned);
                return NULL;
            }
        } else {
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
    return owned;
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
            char digits[24];
            int written = snprintf(digits, sizeof(digits), "%ld", number);
            if (written > 0 && written < (int)sizeof(digits)) {
                return outbuf_append(b, digits, written);
            }
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

    PyObject *local = PyDict_Copy(context);
    if (local == NULL) {
        if (program != stack_program) {
            PyMem_Free(program);
        }
        return NULL;
    }

    outbuf buf = {NULL, 0, 0, max_output, 0};
    loop_frame frames[MAX_LOOP_DEPTH];
    Py_ssize_t depth = 0;
    Py_ssize_t ip = 0;
    int failed = 0;

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
            PyObject *value = lookup_path(local, instr->first, instr->line);
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
            PyObject *value = lookup_path(local, instr->first, instr->line);
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
            PyObject *var = instr->first;
            int line = instr->line;
            PyObject *iterable = lookup_path(local, instr->path, line);
            if (iterable == NULL) {
                failed = 1;
                break;
            }
            PyObject *iterator = PyObject_GetIter(iterable);
            Py_DECREF(iterable);
            if (iterator == NULL) {
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
            PyObject *item = PyIter_Next(iterator);
            if (item == NULL) {
                Py_DECREF(iterator);
                if (PyErr_Occurred()) {
                    failed = 1;
                    break;
                }
                ip = instr->target;
                continue;
            }
            if (depth >= MAX_LOOP_DEPTH) {
                Py_DECREF(item);
                Py_DECREF(iterator);
                raise_render(0, PyUnicode_FromString(
                                    "template loop nesting too deep"));
                failed = 1;
                break;
            }
            PyObject *old = PyDict_GetItemWithError(local, var);
            if (old == NULL && PyErr_Occurred()) {
                Py_DECREF(item);
                Py_DECREF(iterator);
                failed = 1;
                break;
            }
            frames[depth].iterator = iterator;
            frames[depth].var = var;
            frames[depth].body_start = ip + 1;
            frames[depth].had_old = old != NULL;
            frames[depth].old_value = old;
            Py_XINCREF(old);
            depth++;
            int rc = PyDict_SetItem(local, var, item);
            Py_DECREF(item);
            if (rc < 0) {
                failed = 1;
                break;
            }
            ip++;
        } else if (op == OP_ENDFOR) {
            loop_frame *frame = &frames[depth - 1];
            PyObject *item = PyIter_Next(frame->iterator);
            if (item == NULL) {
                if (PyErr_Occurred()) {
                    failed = 1;
                    break;
                }
                if (frame->had_old) {
                    if (PyDict_SetItem(local, frame->var, frame->old_value) < 0) {
                        failed = 1;
                        break;
                    }
                } else if (PyDict_DelItem(local, frame->var) < 0) {
                    PyErr_Clear();
                }
                Py_XDECREF(frame->old_value);
                Py_DECREF(frame->iterator);
                depth--;
                ip++;
            } else {
                int rc = PyDict_SetItem(local, frame->var, item);
                Py_DECREF(item);
                if (rc < 0) {
                    failed = 1;
                    break;
                }
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
        Py_XDECREF(frames[depth].old_value);
        Py_DECREF(frames[depth].iterator);
    }
    Py_DECREF(local);
    if (program != stack_program) {
        PyMem_Free(program);
    }

    if (failed) {
        PyMem_Free(buf.data);
        return NULL;
    }
    PyObject *result = PyBytes_FromStringAndSize(buf.data, buf.len);
    PyMem_Free(buf.data);
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
