#include "wreathcore.h"

/* ---- signed double-submit CSRF tokens (see ADR 0018) --------------------
 *
 * Token: "v1.<issued>.<nonce>.<signature>", where nonce and signature are each
 * 32 bytes in unpadded URL-safe base64 (43 characters).
 *
 * The HMAC itself is *not* reimplemented here. `hmac.digest(..., "sha256")` is
 * already a C routine inside CPython, and it measured at ~2.5us of CSRF's ~11us
 * per request; the rest was Python glue -- an f-string, `.encode()`, and two
 * `_b64encode` frames each running b64encode/rstrip/decode. So this owns the
 * glue and calls the existing primitive once for the digest. Linking libcrypto
 * would make the default build depend on OpenSSL, which `_core` deliberately
 * does not, and hand-rolling SHA-256 in an auth path buys ~1us for a much worse
 * trade.
 */

#include <errno.h>
#include <stdlib.h>

#if defined(__linux__)
#include <sys/random.h>
#define WREATH_HAVE_GETRANDOM 1
#endif

static PyObject *hmac_digest_fn = NULL;   /* _hashlib.hmac_digest */
static PyObject *sha256_name = NULL;      /* "sha256" */
static PyObject *urandom_fn = NULL;       /* os.urandom */
static PyObject *nonce_size = NULL;       /* 32 */

static const char B64URL[65] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/* Unpadded URL-safe base64 of exactly 32 bytes -> 43 characters. */
static void
b64url_32(const unsigned char *in, char *out)
{
    int i = 0, o = 0;
    for (; i + 3 <= 32; i += 3) {
        unsigned int triple = ((unsigned int)in[i] << 16) |
                              ((unsigned int)in[i + 1] << 8) | in[i + 2];
        out[o++] = B64URL[(triple >> 18) & 0x3F];
        out[o++] = B64URL[(triple >> 12) & 0x3F];
        out[o++] = B64URL[(triple >> 6) & 0x3F];
        out[o++] = B64URL[triple & 0x3F];
    }
    /* 32 = 3*10 + 2: the tail encodes two bytes as three characters, and the
     * padding '=' that base64 would add is stripped, exactly as the Python
     * `_b64encode` helper does. */
    {
        unsigned int rest = wreath_load_u16_be((const uint8_t *)(in + 30));
        out[o++] = B64URL[(rest >> 10) & 0x3F];
        out[o++] = B64URL[(rest >> 4) & 0x3F];
        out[o++] = B64URL[(rest << 2) & 0x3F];
    }
}

/* [A-Za-z0-9_-], matching _TOKEN_COMPONENT in the Python twin. */
static const unsigned char B64URL_CHAR[256] = {
    ['A'] = 1, ['B'] = 1, ['C'] = 1, ['D'] = 1, ['E'] = 1, ['F'] = 1, ['G'] = 1,
    ['H'] = 1, ['I'] = 1, ['J'] = 1, ['K'] = 1, ['L'] = 1, ['M'] = 1, ['N'] = 1,
    ['O'] = 1, ['P'] = 1, ['Q'] = 1, ['R'] = 1, ['S'] = 1, ['T'] = 1, ['U'] = 1,
    ['V'] = 1, ['W'] = 1, ['X'] = 1, ['Y'] = 1, ['Z'] = 1,
    ['a'] = 1, ['b'] = 1, ['c'] = 1, ['d'] = 1, ['e'] = 1, ['f'] = 1, ['g'] = 1,
    ['h'] = 1, ['i'] = 1, ['j'] = 1, ['k'] = 1, ['l'] = 1, ['m'] = 1, ['n'] = 1,
    ['o'] = 1, ['p'] = 1, ['q'] = 1, ['r'] = 1, ['s'] = 1, ['t'] = 1, ['u'] = 1,
    ['v'] = 1, ['w'] = 1, ['x'] = 1, ['y'] = 1, ['z'] = 1,
    ['0'] = 1, ['1'] = 1, ['2'] = 1, ['3'] = 1, ['4'] = 1, ['5'] = 1, ['6'] = 1,
    ['7'] = 1, ['8'] = 1, ['9'] = 1, ['-'] = 1, ['_'] = 1,
};

static int
component_valid(const char *data, Py_ssize_t len)
{
    if (len != 43) return 0;
    for (Py_ssize_t i = 0; i < 43; i++) {
        if (!B64URL_CHAR[(unsigned char)data[i]]) return 0;
    }
    return 1;
}

/* Compares in time independent of where the first difference is, like
 * hmac.compare_digest. Never early-exit here. */
static int
constant_time_equal(const char *a, const char *b, Py_ssize_t len)
{
    unsigned char diff = 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        diff |= (unsigned char)(a[i] ^ b[i]);
    }
    return diff == 0;
}

/* signature = b64url(HMAC-SHA256(secret, "v1.<issued>.<nonce>")) */
static int
sign_message(PyObject *secret, const char *message, Py_ssize_t message_len, char *out43)
{
    PyObject *payload = PyBytes_FromStringAndSize(message, message_len);
    PyObject *digest;
    PyObject *args[3];

    if (payload == NULL) return -1;
    args[0] = secret;
    args[1] = payload;
    args[2] = sha256_name;
    digest = PyObject_Vectorcall(hmac_digest_fn, args, 3, NULL);
    Py_DECREF(payload);
    if (digest == NULL) return -1;
    if (!PyBytes_Check(digest) || PyBytes_GET_SIZE(digest) != 32) {
        Py_DECREF(digest);
        PyErr_SetString(PyExc_RuntimeError, "hmac_digest did not return 32 bytes");
        return -1;
    }
    b64url_32((const unsigned char *)PyBytes_AS_STRING(digest), out43);
    Py_DECREF(digest);
    return 0;
}

/* csrf_sign(secret: bytes, issued: int, nonce: str) -> str */
PyObject *
wreath_csrf_sign(PyObject *self, PyObject *args)
{
    PyObject *secret;
    long long issued;
    const char *nonce;
    Py_ssize_t nonce_len;
    char message[80];
    char signature[43];
    int message_len;
    (void)self;

    if (!PyArg_ParseTuple(args, "SLs#:csrf_sign", &secret, &issued, &nonce, &nonce_len)) {
        return NULL;
    }
    if (nonce_len != 43) {
        PyErr_SetString(PyExc_ValueError, "csrf nonce must be 43 characters");
        return NULL;
    }
    message_len = snprintf(message, sizeof(message), "v1.%lld.%.*s",
                           issued, (int)nonce_len, nonce);
    if (message_len < 0 || (size_t)message_len >= sizeof(message)) {
        PyErr_SetString(PyExc_ValueError, "csrf issued timestamp out of range");
        return NULL;
    }
    if (sign_message(secret, message, message_len, signature) < 0) {
        return NULL;
    }
    {
        char token[128];
        int token_len = snprintf(token, sizeof(token), "%.*s.%.*s",
                                 message_len, message, 43, signature);
        if (token_len < 0 || (size_t)token_len >= sizeof(token)) {
            PyErr_SetString(PyExc_ValueError, "csrf token too long");
            return NULL;
        }
        return PyUnicode_DecodeASCII(token, token_len, "strict");
    }
}

/* Fill `out` with `len` cryptographically secure bytes.
 *
 * `getrandom(2)` rather than a call back into `os.urandom`, which was measured
 * at 2639ns for 32 bytes against 135ns here -- a 19.6x difference that is not
 * call overhead. `os.urandom` performs the syscall every time; glibc's
 * `getrandom` reaches the same kernel generator through the vDSO, so the common
 * case costs no syscall at all. The entropy is identical: this is the source
 * CPython's own `os.urandom` draws from on Linux, with the same blocking-until-
 * seeded semantics.
 *
 * Anything that is not Linux, and any failure at all, falls back to
 * `os.urandom`. A CSRF nonce is not a place to improvise when the fast path is
 * unavailable, and no cached buffer of pre-drawn bytes is kept: that would be
 * process-global mutable state (ADR 0007) holding unused key material.
 */
static int
fill_random(unsigned char *out, Py_ssize_t len)
{
#if defined(WREATH_HAVE_GETRANDOM)
    Py_ssize_t filled = 0;
    while (filled < len) {
        ssize_t got = getrandom(out + filled, (size_t)(len - filled), 0);
        if (got < 0) {
            if (errno == EINTR) {
                if (PyErr_CheckSignals() < 0) {
                    return -1;
                }
                continue;
            }
            break;  /* ENOSYS on an old kernel, or anything else: fall back */
        }
        filled += got;
    }
    if (filled == len) {
        return 0;
    }
#endif
    {
        PyObject *size = PyLong_FromSsize_t(len);
        PyObject *drawn = size == NULL
            ? NULL : PyObject_CallOneArg(urandom_fn, size);
        Py_XDECREF(size);
        if (drawn == NULL) {
            return -1;
        }
        if (!PyBytes_Check(drawn) || PyBytes_GET_SIZE(drawn) != len) {
            Py_DECREF(drawn);
            PyErr_SetString(PyExc_RuntimeError, "os.urandom returned the wrong size");
            return -1;
        }
        memcpy(out, PyBytes_AS_STRING(drawn), (size_t)len);
        Py_DECREF(drawn);
        return 0;
    }
}

/* random_hex(size: int) -> str
 *
 * `os.urandom(n).hex()` in one call and no syscall. `os.urandom` performs a
 * real `getrandom` syscall every time it is asked; glibc routes the same
 * request through the vDSO, which is why `fill_random` above exists and why
 * CSRF token minting stopped costing 2.6us. Correlation identifiers wanted
 * exactly the same thing: `RequestIDMiddleware` runs on every request of every
 * application that installs it, and spent almost all of its time here.
 *
 * Bounded to 64 bytes because an identifier is not key material and a stack
 * buffer keeps this allocation-free; nothing in the framework asks for more.
 */
PyObject *
wreath_random_hex(PyObject *self, PyObject *args)
{
    static const char digits[] = "0123456789abcdef";
    unsigned char raw[64];
    char out[128];
    Py_ssize_t size;
    Py_ssize_t i;
    (void)self;

    if (!PyArg_ParseTuple(args, "n:random_hex", &size)) {
        return NULL;
    }
    if (size < 1 || size > (Py_ssize_t)sizeof(raw)) {
        PyErr_SetString(PyExc_ValueError, "random_hex size must be 1..64");
        return NULL;
    }
    if (fill_random(raw, size) < 0) {
        return NULL;
    }
    for (i = 0; i < size; i++) {
        out[i * 2] = digits[raw[i] >> 4];
        out[i * 2 + 1] = digits[raw[i] & 0x0F];
    }
    return PyUnicode_DecodeASCII(out, size * 2, "strict");
}

/* csrf_new_token(secret: bytes, issued: int) -> str */
PyObject *
wreath_csrf_new_token(PyObject *self, PyObject *args)
{
    PyObject *secret;
    long long issued;
    unsigned char seed[32];
    char nonce[43];
    char message[80];
    char signature[43];
    int message_len;
    (void)self;

    if (!PyArg_ParseTuple(args, "SL:csrf_new_token", &secret, &issued)) {
        return NULL;
    }
    if (fill_random(seed, 32) < 0) {
        return NULL;
    }
    b64url_32(seed, nonce);
    message_len = snprintf(message, sizeof(message), "v1.%lld.%.*s", issued, 43, nonce);
    if (message_len < 0 || (size_t)message_len >= sizeof(message)) {
        PyErr_SetString(PyExc_ValueError, "csrf issued timestamp out of range");
        return NULL;
    }
    if (sign_message(secret, message, message_len, signature) < 0) {
        return NULL;
    }
    {
        char token[128];
        int token_len = snprintf(token, sizeof(token), "%.*s.%.*s",
                                 message_len, message, 43, signature);
        if (token_len < 0 || (size_t)token_len >= sizeof(token)) {
            PyErr_SetString(PyExc_ValueError, "csrf token too long");
            return NULL;
        }
        return PyUnicode_DecodeASCII(token, token_len, "strict");
    }
}

/* csrf_validate(secret: bytes, token: str, now: int, max_age: int)
 *     -> (valid: bool, issued: int) */
PyObject *
wreath_csrf_validate(PyObject *self, PyObject *args)
{
    PyObject *secret;
    const char *token;
    Py_ssize_t token_len;
    long long now;
    long long max_age;
    const char *cursor;
    const char *parts[4];
    Py_ssize_t lengths[4];
    int count = 0;
    long long issued;
    char *end;
    char number[24];
    char message[80];
    char signature[43];
    int message_len;
    (void)self;

    if (!PyArg_ParseTuple(args, "Ss#LL:csrf_validate", &secret, &token, &token_len,
                          &now, &max_age)) {
        return NULL;
    }
    /* Exactly four dot-separated fields, like token.split(".") in the twin. */
    cursor = token;
    parts[0] = cursor;
    for (Py_ssize_t i = 0; i < token_len; i++) {
        if (token[i] != '.') continue;
        if (count == 3) return Py_BuildValue("(OL)", Py_False, 0LL);
        lengths[count] = (token + i) - parts[count];
        count++;
        parts[count] = token + i + 1;
    }
    if (count != 3) return Py_BuildValue("(OL)", Py_False, 0LL);
    lengths[3] = (token + token_len) - parts[3];

    if (lengths[0] != 2 || memcmp(parts[0], "v1", 2) != 0) {
        return Py_BuildValue("(OL)", Py_False, 0LL);
    }
    /* int(parts[1]) semantics, minus the exceptions: reject anything strtoll
     * would not consume whole, and anything too long to be a timestamp. */
    if (lengths[1] == 0 || (size_t)lengths[1] >= sizeof(number)) {
        return Py_BuildValue("(OL)", Py_False, 0LL);
    }
    memcpy(number, parts[1], (size_t)lengths[1]);
    number[lengths[1]] = '\0';
    errno = 0;
    issued = strtoll(number, &end, 10);
    if (*end != '\0' || errno != 0) {
        return Py_BuildValue("(OL)", Py_False, 0LL);
    }
    if (!component_valid(parts[2], lengths[2]) || !component_valid(parts[3], lengths[3])) {
        return Py_BuildValue("(OL)", Py_False, 0LL);
    }
    /* Expiry is reported with the issued time, so the caller can decide to
     * renew rather than reject -- matching the Python twin's contract. */
    if (issued > now + 60 || now - issued > max_age) {
        return Py_BuildValue("(OL)", Py_False, issued);
    }
    message_len = snprintf(message, sizeof(message), "v1.%lld.%.*s",
                           issued, (int)lengths[2], parts[2]);
    if (message_len < 0 || (size_t)message_len >= sizeof(message)) {
        return Py_BuildValue("(OL)", Py_False, issued);
    }
    if (sign_message(secret, message, message_len, signature) < 0) {
        return NULL;
    }
    return Py_BuildValue(
        "(OL)",
        constant_time_equal(signature, parts[3], 43) ? Py_True : Py_False,
        issued
    );
}

int
wreath_security_ready(void)
{
    PyObject *module = PyImport_ImportModule("_hashlib");
    if (module == NULL) return -1;
    hmac_digest_fn = PyObject_GetAttrString(module, "hmac_digest");
    Py_DECREF(module);
    if (hmac_digest_fn == NULL) return -1;
    sha256_name = PyUnicode_InternFromString("sha256");
    if (sha256_name == NULL) return -1;

    module = PyImport_ImportModule("os");
    if (module == NULL) return -1;
    urandom_fn = PyObject_GetAttrString(module, "urandom");
    Py_DECREF(module);
    if (urandom_fn == NULL) return -1;
    nonce_size = PyLong_FromLong(32);
    if (nonce_size == NULL) return -1;
    return 0;
}

PyObject *
wreath_host_allowed(PyObject *self, PyObject *args)
{
    PyObject *host_obj;
    PyObject *patterns;
    const char *host;
    Py_ssize_t host_len;
    Py_ssize_t i;
    (void)self;

    if (!PyArg_ParseTuple(args, "UO!:host_allowed", &host_obj, &PyTuple_Type, &patterns)) {
        return NULL;
    }
    host = PyUnicode_AsUTF8AndSize(host_obj, &host_len);
    if (host == NULL) {
        return NULL;
    }
    for (i = 0; i < PyTuple_GET_SIZE(patterns); i++) {
        PyObject *pattern_obj = PyTuple_GET_ITEM(patterns, i);
        const char *pattern;
        Py_ssize_t pattern_len;
        if (!PyUnicode_Check(pattern_obj)) {
            PyErr_SetString(PyExc_TypeError, "trusted-host patterns must be strings");
            return NULL;
        }
        pattern = PyUnicode_AsUTF8AndSize(pattern_obj, &pattern_len);
        if (pattern == NULL) {
            return NULL;
        }
        if (pattern_len == 1 && pattern[0] == '*') {
            Py_RETURN_TRUE;
        }
        if (pattern_len == host_len && memcmp(pattern, host, (size_t)host_len) == 0) {
            Py_RETURN_TRUE;
        }
        if (pattern_len > 2 && pattern[0] == '*' && pattern[1] == '.') {
            Py_ssize_t suffix_len = pattern_len - 1;
            if (host_len > suffix_len &&
                memcmp(host + host_len - suffix_len, pattern + 1, (size_t)suffix_len) == 0) {
                Py_RETURN_TRUE;
            }
        }
    }
    Py_RETURN_FALSE;
}
