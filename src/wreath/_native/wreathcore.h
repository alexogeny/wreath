/* Shared declarations for the wreath._native._core extension. */
#ifndef WREATH_CORE_H
#define WREATH_CORE_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdint.h>
#include <string.h>

/* authz.c */
PyObject *wreath_build_capability_mask(PyObject *self, PyObject *args);
PyObject *wreath_normalize_authorization_decision(PyObject *self, PyObject *args);

/* env.c */
PyObject *wreath_parse_dotenv(PyObject *self, PyObject *arg);
PyObject *wreath_read_osenv(PyObject *self, PyObject *ignored);

/* security.c */
PyObject *wreath_host_allowed(PyObject *self, PyObject *args);
PyObject *wreath_csrf_sign(PyObject *self, PyObject *args);
PyObject *wreath_csrf_new_token(PyObject *self, PyObject *args);
PyObject *wreath_csrf_validate(PyObject *self, PyObject *args);
/* Resolves _hashlib.hmac_digest once; returns -1 on failure. */
int wreath_security_ready(void);

/* observability.c */
PyObject *wreath_request_id_valid(PyObject *self, PyObject *args);
PyObject *wreath_format_server_timing(PyObject *self, PyObject *args);

/* proxy.c: adds the TrustedNetworks type; returns -1 on failure. */
int wreath_register_proxy(PyObject *module);

/* ratelimit.c: adds the TokenBucket type; returns -1 on failure. */
int wreath_register_ratelimit(PyObject *module);

/* headers.c */
PyObject *wreath_find_header(PyObject *self, PyObject *args);
PyObject *wreath_build_header_map(PyObject *self, PyObject *args);

/* validate.c */
PyObject *wreath_run_validation(PyObject *self, PyObject *args);

/* orm_shape.c */
PyObject *wreath_orm_shape(PyObject *self, PyObject *args);
PyObject *wreath_orm_shape_configure(PyObject *self, PyObject *args);
PyObject *wreath_orm_collect_values(PyObject *self, PyObject *args);

/* codecs.c */
PyObject *wreath_percent_decode(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *wreath_parse_qs(PyObject *self, PyObject *args);
PyObject *wreath_parse_cookies(PyObject *self, PyObject *args);

/* ws.c */
PyObject *wreath_ws_mask(PyObject *self, PyObject *args);
PyObject *wreath_ws_parse_frame(PyObject *self, PyObject *args);
PyObject *wreath_ws_build_frame(PyObject *self, PyObject *args, PyObject *kwargs);

/* WebSocket frame header, filled by the C-level parser shared through the
 * WREATH_CORE_CAPI capsule so sibling extensions (the native server) can parse
 * frames without a Python call per frame. */
typedef struct {
    int fin;
    int opcode;
    int masked;
    Py_ssize_t header_len;    /* bytes before the payload, mask key included */
    Py_ssize_t payload_len;
    const uint8_t *mask_key;  /* points into the input; NULL when unmasked */
} WreathWsFrameHeader;

typedef struct {
    /* Returns 0 with *out filled when a complete header is available,
     * 1 when more bytes are needed, -1 on a protocol error (reserved bits,
     * oversized length).  Never touches the Python error indicator. */
    int (*ws_parse_header)(const uint8_t *buf, Py_ssize_t len, WreathWsFrameHeader *out);
    /* XOR (un)mask src into dst with the 4-byte key. dst may equal src. */
    void (*ws_unmask)(uint8_t *dst, const uint8_t *src, Py_ssize_t len,
                      const uint8_t *key);
} WreathCoreCAPI;

#define WREATH_CORE_CAPI_NAME "wreath._native._core._C_API"

int wreath_ws_parse_header_raw(const uint8_t *buf, Py_ssize_t len, WreathWsFrameHeader *out);
void wreath_ws_unmask_raw(uint8_t *dst, const uint8_t *src, Py_ssize_t len,
                       const uint8_t *key);

/* multipart.c */
PyObject *wreath_multipart_parse(PyObject *self, PyObject *args, PyObject *kwds);

/* json.c */
PyObject *wreath_json_dumps(PyObject *self, PyObject *arg);
PyObject *wreath_json_loads(PyObject *self, PyObject *arg);

/* templates.c */
PyObject *wreath_template_render(PyObject *self, PyObject *args);
PyObject *wreath_template_configure(PyObject *self, PyObject *args);

/* http.c */
int wreath_http_parse_request_parts(
    const uint8_t *data, Py_ssize_t len, Py_ssize_t head_end_off,
    PyObject **method, PyObject **target, int *minor_version,
    PyObject **headers, Py_ssize_t *consumed
);
PyObject *wreath_http_parse_request(PyObject *self, PyObject *args);
PyObject *wreath_http_parse_response(PyObject *self, PyObject *args);
PyObject *wreath_http_serialize_request(PyObject *self, PyObject *args);

/* webpolicy.c */
PyObject *wreath_select_content_encoding(PyObject *self, PyObject *arg);
PyObject *wreath_is_compressible_content_type(PyObject *self, PyObject *arg);
PyObject *wreath_cache_control_flags(PyObject *self, PyObject *arg);
PyObject *wreath_origin_matches(PyObject *self, PyObject *args);
PyObject *wreath_append_missing_headers(PyObject *self, PyObject *args);
PyObject *wreath_append_vary(PyObject *self, PyObject *args);
PyObject *wreath_replace_content_length(PyObject *self, PyObject *args);
PyObject *wreath_find_response_header(PyObject *self, PyObject *args);
int wreath_register_webpolicy(PyObject *module);

/* router.c: adds the RouteTable type to the module; returns -1 on failure. */
int wreath_register_router(PyObject *module);

/* dtrouter.c: adds the DecisionRouteTable type; returns -1 on failure. */
int wreath_register_dtrouter(PyObject *module);
int wreath_register_dtbitset(PyObject *module);

/* Substring search.
 *
 * Delegates to libc memmem (two-way algorithm, linear worst case) where it
 * exists — this matters for attacker-controlled inputs like multipart
 * boundaries. CPython's pyconfig.h defines _GNU_SOURCE, so glibc declares
 * memmem.
 *
 * Define WREATH_FORCE_PORTABLE_MEMMEM to select the portable path on a platform
 * that would otherwise use libc. That is a test/diagnostic switch only: it
 * deliberately does not participate in the release platform detection below,
 * because a result produced by glibc's memmem says nothing about the fallback.
 */
#if !defined(WREATH_FORCE_PORTABLE_MEMMEM) && \
    (defined(__GLIBC__) || defined(__APPLE__) || defined(__FreeBSD__) || \
     defined(__OpenBSD__) || defined(__NetBSD__) || \
     (defined(__linux__) && !defined(__ANDROID__)))
#define WREATH_MEMMEM_USES_LIBC 1
#else
#define WREATH_MEMMEM_USES_LIBC 0
#endif

#if !WREATH_MEMMEM_USES_LIBC
/* Maximal suffix of `needle` under a byte ordering (`reverse` flips it).
 *
 * Returns the index of the maximal suffix (-1 when it is the whole string) and
 * stores its period. Half of the Crochemore-Perrin critical factorization. */
static inline Py_ssize_t
wreath_max_suffix(const uint8_t *needle, Py_ssize_t len, Py_ssize_t *period, int reverse)
{
    Py_ssize_t ms = -1, j = 0, k = 1, p = 1;
    while (j + k < len) {
        uint8_t a = needle[j + k];
        uint8_t b = needle[ms + k];   /* ms == -1 reads needle[k - 1] */
        int a_before_b = reverse ? (a > b) : (a < b);
        int b_before_a = reverse ? (a < b) : (a > b);
        if (a_before_b) {
            j += k;
            k = 1;
            p = j - ms;
        }
        else if (b_before_a) {
            ms = j;
            j = ms + 1;
            k = p = 1;
        }
        else if (k != p) {          /* a == b, still inside the period */
            k++;
        }
        else {
            j += p;
            k = 1;
        }
    }
    *period = p;
    return ms;
}

/* Critical factorization: the position splitting the needle into halves whose
 * local period is the needle's period. Returns the first index of the right
 * half and stores that period. */
static inline Py_ssize_t
wreath_critical_factorization(const uint8_t *needle, Py_ssize_t len, Py_ssize_t *period)
{
    Py_ssize_t forward_period, reverse_period;
    Py_ssize_t forward = wreath_max_suffix(needle, len, &forward_period, 0);
    Py_ssize_t reverse = wreath_max_suffix(needle, len, &reverse_period, 1);
    if (reverse < forward) {        /* keep the longer left half */
        *period = forward_period;
        return forward + 1;
    }
    *period = reverse_period;
    return reverse + 1;
}

/* Two-way substring search (Crochemore-Perrin): linear worst case with no
 * allocation and no shift table, which suits the short needles this sees (a
 * multipart delimiter is at most 74 bytes). The naive memchr-accelerated scan
 * this replaces was O(hay * needle) on repetitive input with overlapping
 * prefixes -- exactly what an attacker-supplied boundary can produce. */
static inline const uint8_t *
wreath_two_way(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle,
            Py_ssize_t needle_len)
{
    Py_ssize_t period;
    Py_ssize_t suffix = wreath_critical_factorization(needle, needle_len, &period);
    Py_ssize_t j = 0;

    if (period + suffix <= needle_len &&
        memcmp(needle, needle + period, (size_t)suffix) == 0) {
        /* The needle is periodic: a mismatch can only advance by the period, so
         * remember the prefix already known to match and never rescan it. */
        Py_ssize_t memory = 0;
        while (j <= hay_len - needle_len) {
            Py_ssize_t i = suffix > memory ? suffix : memory;
            while (i < needle_len && needle[i] == hay[i + j]) {
                i++;
            }
            if (i < needle_len) {
                j += i - suffix + 1;
                memory = 0;
                continue;
            }
            i = suffix - 1;
            while (i >= memory && needle[i] == hay[i + j]) {
                i--;
            }
            if (i < memory) {
                return hay + j;
            }
            j += period;
            memory = needle_len - period;
        }
        return NULL;
    }

    /* Aperiodic: a mismatch may advance past the whole left or right half. */
    Py_ssize_t shift = (suffix > needle_len - suffix ? suffix : needle_len - suffix) + 1;
    while (j <= hay_len - needle_len) {
        Py_ssize_t i = suffix;
        while (i < needle_len && needle[i] == hay[i + j]) {
            i++;
        }
        if (i < needle_len) {
            j += i - suffix + 1;
            continue;
        }
        i = suffix - 1;
        while (i >= 0 && needle[i] == hay[i + j]) {
            i--;
        }
        if (i < 0) {
            return hay + j;
        }
        j += shift;
    }
    return NULL;
}
#endif /* !WREATH_MEMMEM_USES_LIBC */

static inline const uint8_t *
wreath_memmem(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle, Py_ssize_t needle_len)
{
    if (needle_len <= 0 || hay_len < needle_len) {
        return NULL;
    }
#if WREATH_MEMMEM_USES_LIBC
    return memmem(hay, (size_t)hay_len, needle, (size_t)needle_len);
#else
    if (needle_len == 1) {
        return (const uint8_t *)memchr(hay, needle[0], (size_t)hay_len);
    }
    return wreath_two_way(hay, hay_len, needle, needle_len);
#endif
}

#endif /* WREATH_CORE_H */
