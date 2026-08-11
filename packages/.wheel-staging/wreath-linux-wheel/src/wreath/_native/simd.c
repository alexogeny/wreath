/* Test seam for the dispatched scanners in `simd.h`.
 *
 * A vector arm is only trustworthy if something runs it against the scalar
 * definition on the same bytes. The dispatcher alone cannot provide that: it
 * picks the widest arm the CPU has and the narrower ones are then never
 * executed, so a defect in the SWAR fallback would ship untested to every
 * machine older than this one -- and a defect in AVX2 would ship untested to
 * every machine newer than the developer's.
 *
 * `simd_probe` names an arm explicitly and returns what it found, and
 * `simd_arms` reports which arms this build can actually reach.
 * `tests/test_native_simd.py` crosses the two. Nothing here is public API;
 * `wreath.testing` does not re-export it and no runtime path calls it.
 */
#include "wreathcore.h"

#include "simd.h"

/* simd_arms() -> tuple[str, ...] */
PyObject *
wreath_simd_arms(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    PyObject *arms = PyList_New(0);
    if (arms == NULL) {
        return NULL;
    }
    const char *names[5] = {"scalar", "swar", NULL, NULL, NULL};
#if defined(WREATH_HAVE_SSE2)
    names[2] = "sse2";
#endif
#if defined(WREATH_HAVE_AVX2)
    if (wreath_simd_has_avx2()) {
        names[3] = "avx2";
    }
#endif
#if defined(WREATH_HAVE_NEON)
    /* Baseline on ARMv8, so it is listed whenever it is compiled. */
    names[4] = "neon";
#endif
    for (int i = 0; i < 5; i++) {
        if (names[i] == NULL) {
            continue;
        }
        PyObject *name = PyUnicode_FromString(names[i]);
        if (name == NULL || PyList_Append(arms, name) < 0) {
            Py_XDECREF(name);
            Py_DECREF(arms);
            return NULL;
        }
        Py_DECREF(name);
    }
    PyObject *result = PyList_AsTuple(arms);
    Py_DECREF(arms);
    return result;
}

static int
arm_code(const char *arm)
{
    if (strcmp(arm, "scalar") == 0) {
        return WREATH_ARM_SCALAR;
    }
    if (strcmp(arm, "swar") == 0) {
        return WREATH_ARM_SWAR;
    }
    if (strcmp(arm, "sse2") == 0) {
        return WREATH_ARM_SSE2;
    }
    if (strcmp(arm, "avx2") == 0) {
        return WREATH_ARM_AVX2;
    }
    if (strcmp(arm, "neon") == 0) {
        return WREATH_ARM_NEON;
    }
    return -1;
}

/* Returns -1 and sets nothing when the arm is not reachable in this build. */
static ptrdiff_t
run_kind(const char *kind, int arm, const char *data, ptrdiff_t len, unsigned *seen_high)
{
    if (strcmp(kind, "json") == 0) {
        switch (arm) {
            case WREATH_ARM_SCALAR: return wreath_json_run_scalar(data, len, seen_high);
            case WREATH_ARM_SWAR: return wreath_json_run_swar(data, len, seen_high);
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2: return wreath_json_run_sse2(data, len, seen_high);
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2: return wreath_json_run_avx2(data, len, seen_high);
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON: return wreath_json_run_neon(data, len, seen_high);
#endif
            default: return -1;
        }
    }
    if (strcmp(kind, "html") == 0) {
        switch (arm) {
            case WREATH_ARM_SCALAR: return wreath_html_run_scalar(data, len);
            case WREATH_ARM_SWAR: return wreath_html_run_swar(data, len);
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2: return wreath_html_run_sse2(data, len);
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2: return wreath_html_run_avx2(data, len);
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON: return wreath_html_run_neon(data, len);
#endif
            default: return -1;
        }
    }
    if (strcmp(kind, "value") == 0) {
        switch (arm) {
            case WREATH_ARM_SCALAR: return wreath_value_run_scalar(data, len);
            case WREATH_ARM_SWAR: return wreath_value_run_swar(data, len);
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2: return wreath_value_run_sse2(data, len);
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2: return wreath_value_run_avx2(data, len);
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON: return wreath_value_run_neon(data, len);
#endif
            default: return -1;
        }
    }
    return -2;
}

/* simd_probe(kind, arm, data, key=None) -> int | (int, int) | bytes | None
 *
 * `None` means the named arm is not reachable in this build, which is a skip
 * for the caller and never a pass.
 */
PyObject *
wreath_simd_probe(PyObject *Py_UNUSED(self), PyObject *args)
{
    const char *kind;
    const char *arm;
    Py_buffer data;
    Py_buffer key = {NULL, NULL};
    if (!PyArg_ParseTuple(args, "ssy*|y*:simd_probe", &kind, &arm, &data, &key)) {
        return NULL;
    }

    int code = arm_code(arm);
    if (code < 0) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        PyErr_Format(PyExc_ValueError, "unknown arm %s", arm);
        return NULL;
    }

    if (strcmp(kind, "xor") == 0) {
        if (key.buf == NULL || key.len != 4) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            PyErr_SetString(PyExc_ValueError, "xor needs a 4-byte key");
            return NULL;
        }
        PyObject *out = PyBytes_FromStringAndSize(NULL, data.len);
        if (out == NULL) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            return NULL;
        }
        uint8_t *dst = (uint8_t *)PyBytes_AS_STRING(out);
        const uint8_t *src = (const uint8_t *)data.buf;
        const uint8_t *k = (const uint8_t *)key.buf;
        switch (code) {
            case WREATH_ARM_SCALAR: wreath_xor_mask_scalar(dst, src, data.len, k); break;
            case WREATH_ARM_SWAR: wreath_xor_mask_swar(dst, src, data.len, k); break;
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2: wreath_xor_mask_sse2(dst, src, data.len, k); break;
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2: wreath_xor_mask_avx2(dst, src, data.len, k); break;
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON: wreath_xor_mask_neon(dst, src, data.len, k); break;
#endif
            default:
                Py_DECREF(out);
                out = Py_NewRef(Py_None);
                break;
        }
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        return out;
    }

    if (strcmp(kind, "hex") == 0) {
        PyObject *out = PyBytes_FromStringAndSize(NULL, data.len / 2);
        if (out == NULL) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            return NULL;
        }
        unsigned char *dst = (unsigned char *)PyBytes_AS_STRING(out);
        const char *src = (const char *)data.buf;
        ptrdiff_t decoded;
        switch (code) {
            case WREATH_ARM_SCALAR:
            case WREATH_ARM_SWAR:
                decoded = wreath_hex_decode_scalar(src, data.len, dst);
                break;
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2:
                decoded = wreath_hex_decode_avx2(src, data.len, dst);
                break;
#endif
            default:
                PyBuffer_Release(&data);
                PyBuffer_Release(&key);
                Py_DECREF(out);
                Py_RETURN_NONE;
        }
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        if (decoded < 0) {
            Py_DECREF(out);
            Py_RETURN_FALSE;
        }
        return out;
    }

    if (strcmp(kind, "b64enc") == 0) {
        Py_ssize_t room = ((data.len + 2) / 3) * 4;
        PyObject *out = PyBytes_FromStringAndSize(NULL, room);
        if (out == NULL) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            return NULL;
        }
        char *dst = PyBytes_AS_STRING(out);
        const unsigned char *src = (const unsigned char *)data.buf;
        ptrdiff_t written;
        switch (code) {
            case WREATH_ARM_SCALAR:
            case WREATH_ARM_SWAR:
                written = wreath_b64_encode_scalar(src, data.len, dst, 0, 1);
                break;
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2:
                written = wreath_b64_encode_avx2(src, data.len, dst, 0, 1);
                break;
#endif
            default:
                PyBuffer_Release(&data);
                PyBuffer_Release(&key);
                Py_DECREF(out);
                Py_RETURN_NONE;
        }
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        if (_PyBytes_Resize(&out, (Py_ssize_t)written) < 0) {
            return NULL;
        }
        return out;
    }

    if (strcmp(kind, "b64") == 0) {
        /* (len/4)*3 + 2 is what every caller sizes its buffer to; allocating
         * exactly that here means an arm that writes past it corrupts this
         * object rather than going unnoticed. */
        Py_ssize_t room = (data.len / 4) * 3 + 2;
        PyObject *out = PyBytes_FromStringAndSize(NULL, room);
        if (out == NULL) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            return NULL;
        }
        unsigned char *dst = (unsigned char *)PyBytes_AS_STRING(out);
        const char *src = (const char *)data.buf;
        ptrdiff_t decoded;
        switch (code) {
            case WREATH_ARM_SCALAR:
            case WREATH_ARM_SWAR:
                decoded = wreath_b64url_decode_scalar(src, data.len, dst);
                break;
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2:
                decoded = wreath_b64url_decode_avx2(src, data.len, dst);
                break;
#endif
            default:
                PyBuffer_Release(&data);
                PyBuffer_Release(&key);
                Py_DECREF(out);
                Py_RETURN_NONE;
        }
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        if (decoded < 0) {
            Py_DECREF(out);
            Py_RETURN_FALSE;  /* rejected, which is an answer and not an error */
        }
        if (_PyBytes_Resize(&out, (Py_ssize_t)decoded) < 0) {
            return NULL;
        }
        return out;
    }

    /* Substring search. `data` is the haystack, `key` the needle; the answer is
     * the offset of the first match or -1. Every arm must agree exactly --
     * an arm that reported a *later* match than the scalar one would still look
     * like "a match" to a caller and would silently split a multipart body in
     * the wrong place. */
    if (strcmp(kind, "find") == 0) {
        const uint8_t *found;
        Py_ssize_t offset;
        if (key.buf == NULL) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            PyErr_SetString(PyExc_ValueError, "find needs a needle");
            return NULL;
        }
        const uint8_t *hay = (const uint8_t *)data.buf;
        const uint8_t *needle = (const uint8_t *)key.buf;
        switch (code) {
            case WREATH_ARM_SCALAR:
            case WREATH_ARM_SWAR:
                /* No SWAR arm: a word-at-a-time substring search is the
                 * scalar one with extra steps, and mapping it here keeps the
                 * probe's arm list uniform rather than inventing a gap. */
                found = wreath_find_scalar(hay, data.len, needle, key.len);
                break;
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2:
                found = wreath_find_sse2(hay, data.len, needle, key.len);
                break;
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2:
                found = wreath_find_avx2(hay, data.len, needle, key.len);
                break;
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON:
                found = wreath_find_neon(hay, data.len, needle, key.len);
                break;
#endif
            default:
                PyBuffer_Release(&data);
                PyBuffer_Release(&key);
                Py_RETURN_NONE;
        }
        offset = found == NULL ? -1 : (Py_ssize_t)(found - hay);
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        return PyLong_FromSsize_t(offset);
    }

    /* The incumbent, so the two can be timed as arms of one interleaved run.
     * `wreath_memmem` is glibc's `memmem` where there is one, and the whole
     * question for the kernel above is whether it beats that -- a question no
     * amount of reasoning about vector widths settles, because glibc's is a
     * good scalar algorithm rather than a naive one. Ignores `arm`. */
    if (strcmp(kind, "memmem") == 0) {
        const uint8_t *hay = (const uint8_t *)data.buf;
        const uint8_t *found =
            key.buf == NULL
                ? NULL
                : wreath_memmem(hay, data.len, (const uint8_t *)key.buf, key.len);
        Py_ssize_t offset = found == NULL ? -1 : (Py_ssize_t)(found - hay);
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        return PyLong_FromSsize_t(offset);
    }

    /* Hash-table control-byte groups. `data` is one WREATH_CTRL_GROUP-byte
     * group; a one-byte `key` asks which lanes equal it, and no key asks which
     * lanes are free. Both answers are a lane mask, so both cross against the
     * scalar arm the same way. */
    if (strcmp(kind, "ctrl") == 0) {
        uint32_t mask;
        int want_eq = key.buf != NULL;
        uint8_t needle = want_eq ? *(const uint8_t *)key.buf : 0;
        const uint8_t *group = (const uint8_t *)data.buf;
        if (data.len != WREATH_CTRL_GROUP || (want_eq && key.len != 1)) {
            PyBuffer_Release(&data);
            PyBuffer_Release(&key);
            PyErr_Format(PyExc_ValueError,
                         "ctrl needs a %d-byte group and an optional 1-byte needle",
                         WREATH_CTRL_GROUP);
            return NULL;
        }
        switch (code) {
            case WREATH_ARM_SCALAR:
                mask = want_eq ? wreath_ctrl_eq_scalar(group, needle)
                               : wreath_ctrl_high_scalar(group);
                break;
            case WREATH_ARM_SWAR:
                mask = want_eq ? wreath_ctrl_eq_swar(group, needle)
                               : wreath_ctrl_high_swar(group);
                break;
#if defined(WREATH_HAVE_SSE2)
            case WREATH_ARM_SSE2:
                mask = want_eq ? wreath_ctrl_eq_sse2(group, needle)
                               : wreath_ctrl_high_sse2(group);
                break;
#endif
#if defined(WREATH_HAVE_AVX2)
            case WREATH_ARM_AVX2:
                mask = want_eq ? wreath_ctrl_eq_avx2(group, needle)
                               : wreath_ctrl_high_avx2(group);
                break;
#endif
#if defined(WREATH_HAVE_NEON)
            case WREATH_ARM_NEON:
                mask = want_eq ? wreath_ctrl_eq_neon(group, needle)
                               : wreath_ctrl_high_neon(group);
                break;
#endif
            default:
                PyBuffer_Release(&data);
                PyBuffer_Release(&key);
                Py_RETURN_NONE;
        }
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        return PyLong_FromUnsignedLong((unsigned long)mask);
    }

    unsigned seen_high = 0;
    ptrdiff_t run = run_kind(kind, code, (const char *)data.buf, data.len, &seen_high);
    Py_ssize_t length = data.len;
    PyBuffer_Release(&data);
    PyBuffer_Release(&key);
    (void)length;

    if (run == -2) {
        PyErr_Format(PyExc_ValueError, "unknown kind %s", kind);
        return NULL;
    }
    if (run == -1) {
        Py_RETURN_NONE;
    }
    if (strcmp(kind, "json") == 0) {
        return Py_BuildValue("(nk)", (Py_ssize_t)run, (unsigned long)seen_high);
    }
    return PyLong_FromSsize_t((Py_ssize_t)run);
}
