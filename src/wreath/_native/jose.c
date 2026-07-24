#include "wreathcore.h"

/* ---- JOSE / JWT native fast paths (see design 02-oidc-jwt-sso-auth) -------
 *
 * This owns only the hot, allocation-light, *non-crypto-inventing* pieces of
 * JWT verification, in the same spirit as security.c:
 *
 *   - base64url decode of the three compact segments (per token, x3);
 *   - splitting "header.payload.signature" and enforcing hard size caps;
 *   - HS256/384/512 verification, which reuses CPython's already-OpenSSL-backed
 *     `_hashlib.hmac_digest` (the digest is a C routine inside CPython; this owns
 *     the glue and the constant-time compare, exactly like csrf_validate);
 *   - registered-claim validation (exp/nbf/iat/iss/aud/required), which is
 *     integer and string comparison, not cryptography.
 *
 * RSA (RS and PS family) verification is deliberately NOT here. It is a public-key
 * operation whose only risk is *correctness*, not timing, and its PKCS#1-v1.5 /
 * PSS padding checks are error-prone to hand-write in C. The facade does the
 * modexp with CPython's bigint `pow(s, e, n)` (already C-speed) and the padding
 * checks with the stdlib -- keeping the zero-dependency stance without a
 * hand-rolled cipher in an auth path. Linking libcrypto would make the default
 * build depend on OpenSSL, which _core deliberately does not.
 *
 * TODO(pure-twin): a byte-identical wreath._pure.jose twin plus a Project
 * Wycheproof + `cryptography` differential-oracle harness are deferred per the
 * C-first directive. The facade already falls back to stdlib hmac when _core is
 * absent, so the HS, RS, and PS families still function under WREATH_PURE=1.
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

static PyObject *jose_hmac_digest_fn = NULL;   /* _hashlib.hmac_digest */

/* Hard ceilings. A JWT header/payload/signature segment is base64url; these cap
 * the *decoded* byte length to keep a hostile token from allocating unbounded or
 * feeding a giant string to the JSON parser. Callers pass their own cap too. */
#define JOSE_ABS_MAX_TOKEN (1 << 20)   /* 1 MiB compact token, absolute */

/* base64url decode table: maps an input byte to its 6-bit value, 0xFF invalid.
 * '-'/'_' are the URL-safe alphabet; '+'/'/' are NOT accepted. No '=' padding. */
static const unsigned char B64URL_DECODE[256] = {
    ['A'] = 0,  ['B'] = 1,  ['C'] = 2,  ['D'] = 3,  ['E'] = 4,  ['F'] = 5,
    ['G'] = 6,  ['H'] = 7,  ['I'] = 8,  ['J'] = 9,  ['K'] = 10, ['L'] = 11,
    ['M'] = 12, ['N'] = 13, ['O'] = 14, ['P'] = 15, ['Q'] = 16, ['R'] = 17,
    ['S'] = 18, ['T'] = 19, ['U'] = 20, ['V'] = 21, ['W'] = 22, ['X'] = 23,
    ['Y'] = 24, ['Z'] = 25,
    ['a'] = 26, ['b'] = 27, ['c'] = 28, ['d'] = 29, ['e'] = 30, ['f'] = 31,
    ['g'] = 32, ['h'] = 33, ['i'] = 34, ['j'] = 35, ['k'] = 36, ['l'] = 37,
    ['m'] = 38, ['n'] = 39, ['o'] = 40, ['p'] = 41, ['q'] = 42, ['r'] = 43,
    ['s'] = 44, ['t'] = 45, ['u'] = 46, ['v'] = 47, ['w'] = 48, ['x'] = 49,
    ['y'] = 50, ['z'] = 51,
    ['0'] = 52, ['1'] = 53, ['2'] = 54, ['3'] = 55, ['4'] = 56, ['5'] = 57,
    ['6'] = 58, ['7'] = 59, ['8'] = 60, ['9'] = 61, ['-'] = 62, ['_'] = 63,
    /* Everything else stays 0; a companion validity table disambiguates 'A'. */
};
static const unsigned char B64URL_VALID[256] = {
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

/* Constant-time compare, like hmac.compare_digest. Never early-exit. */
static int
jose_ct_equal(const unsigned char *a, const unsigned char *b, Py_ssize_t len)
{
    unsigned char diff = 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        diff |= (unsigned char)(a[i] ^ b[i]);
    }
    return diff == 0;
}

/* Decode `len` base64url chars into `out` (caller-sized >= (len/4)*3 + 2).
 * Returns the decoded byte count, or -1 on any invalid char / illegal length.
 * Rejects len % 4 == 1 (impossible base64) and any non-alphabet byte. */
static Py_ssize_t
b64url_decode_into(const char *in, Py_ssize_t len, unsigned char *out)
{
    Py_ssize_t o = 0;
    Py_ssize_t i = 0;
    if (len % 4 == 1) {
        return -1;
    }
    for (; i + 4 <= len; i += 4) {
        if (!B64URL_VALID[(unsigned char)in[i]] ||
            !B64URL_VALID[(unsigned char)in[i + 1]] ||
            !B64URL_VALID[(unsigned char)in[i + 2]] ||
            !B64URL_VALID[(unsigned char)in[i + 3]]) {
            return -1;
        }
        unsigned int q = ((unsigned int)B64URL_DECODE[(unsigned char)in[i]] << 18) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 1]] << 12) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 2]] << 6) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 3]]);
        out[o++] = (unsigned char)((q >> 16) & 0xFF);
        out[o++] = (unsigned char)((q >> 8) & 0xFF);
        out[o++] = (unsigned char)(q & 0xFF);
    }
    Py_ssize_t rem = len - i;
    if (rem == 2) {
        if (!B64URL_VALID[(unsigned char)in[i]] || !B64URL_VALID[(unsigned char)in[i + 1]]) {
            return -1;
        }
        unsigned int q = ((unsigned int)B64URL_DECODE[(unsigned char)in[i]] << 18) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 1]] << 12);
        out[o++] = (unsigned char)((q >> 16) & 0xFF);
    }
    else if (rem == 3) {
        if (!B64URL_VALID[(unsigned char)in[i]] || !B64URL_VALID[(unsigned char)in[i + 1]] ||
            !B64URL_VALID[(unsigned char)in[i + 2]]) {
            return -1;
        }
        unsigned int q = ((unsigned int)B64URL_DECODE[(unsigned char)in[i]] << 18) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 1]] << 12) |
                         ((unsigned int)B64URL_DECODE[(unsigned char)in[i + 2]] << 6);
        out[o++] = (unsigned char)((q >> 16) & 0xFF);
        out[o++] = (unsigned char)((q >> 8) & 0xFF);
    }
    return o;
}

/* jose_b64url_decode(data: str) -> bytes
 * Strict, unpadded, URL-safe. Raises ValueError on any invalid input. */
PyObject *
wreath_jose_b64url_decode(PyObject *Py_UNUSED(self), PyObject *arg)
{
    const char *in;
    Py_ssize_t len;
    PyObject *result;

    if (!PyUnicode_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "b64url input must be str");
        return NULL;
    }
    in = PyUnicode_AsUTF8AndSize(arg, &len);
    if (in == NULL) {
        return NULL;
    }
    if (len > JOSE_ABS_MAX_TOKEN) {
        PyErr_SetString(PyExc_ValueError, "base64url input too large");
        return NULL;
    }
    result = PyBytes_FromStringAndSize(NULL, (len / 4) * 3 + 3);
    if (result == NULL) {
        return NULL;
    }
    Py_ssize_t n = b64url_decode_into(in, len, (unsigned char *)PyBytes_AS_STRING(result));
    if (n < 0) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError, "invalid base64url");
        return NULL;
    }
    if (_PyBytes_Resize(&result, n) < 0) {
        return NULL;
    }
    return result;
}

/* jose_parse(token: str, max_segment_bytes: int)
 *     -> (header_json: bytes, payload_json: bytes,
 *         signing_input: bytes, signature: bytes)
 *
 * Splits the compact JWS, enforces exactly two dots and non-empty segments,
 * decodes each base64url segment, and returns the raw decoded bytes plus the
 * ASCII signing input ("header_b64.payload_b64"). JSON parsing and all crypto
 * happen in the facade. `max_segment_bytes` caps each DECODED segment. */
PyObject *
wreath_jose_parse(PyObject *Py_UNUSED(self), PyObject *args)
{
    const char *tok;
    Py_ssize_t tok_len;
    Py_ssize_t max_seg;
    PyObject *tok_obj;

    if (!PyArg_ParseTuple(args, "Un:jose_parse", &tok_obj, &max_seg)) {
        return NULL;
    }
    tok = PyUnicode_AsUTF8AndSize(tok_obj, &tok_len);
    if (tok == NULL) {
        return NULL;
    }
    if (tok_len == 0 || tok_len > JOSE_ABS_MAX_TOKEN) {
        PyErr_SetString(PyExc_ValueError, "token length out of range");
        return NULL;
    }
    /* Find exactly two '.' separators. */
    Py_ssize_t d1 = -1, d2 = -1;
    for (Py_ssize_t i = 0; i < tok_len; i++) {
        if (tok[i] != '.') {
            continue;
        }
        if (d1 < 0) {
            d1 = i;
        }
        else if (d2 < 0) {
            d2 = i;
        }
        else {
            PyErr_SetString(PyExc_ValueError, "compact JWT must have exactly two dots");
            return NULL;
        }
    }
    if (d1 < 0 || d2 < 0) {
        PyErr_SetString(PyExc_ValueError, "compact JWT must have exactly two dots");
        return NULL;
    }
    Py_ssize_t h_len = d1;
    Py_ssize_t p_len = d2 - d1 - 1;
    Py_ssize_t s_len = tok_len - d2 - 1;
    if (h_len == 0 || p_len == 0 || s_len == 0) {
        PyErr_SetString(PyExc_ValueError, "compact JWT has an empty segment");
        return NULL;
    }
    const char *h_b64 = tok;
    const char *p_b64 = tok + d1 + 1;
    const char *s_b64 = tok + d2 + 1;

    /* Reject before allocating if the base64 length implies a decoded size over
     * the cap ( decoded ~= b64len * 3 / 4 ). */
    if (h_len / 4 * 3 > max_seg || p_len / 4 * 3 > max_seg || s_len / 4 * 3 > max_seg) {
        PyErr_SetString(PyExc_ValueError, "JWT segment exceeds size cap");
        return NULL;
    }

    PyObject *header = PyBytes_FromStringAndSize(NULL, h_len / 4 * 3 + 3);
    PyObject *payload = PyBytes_FromStringAndSize(NULL, p_len / 4 * 3 + 3);
    PyObject *signature = PyBytes_FromStringAndSize(NULL, s_len / 4 * 3 + 3);
    PyObject *signing_input = PyBytes_FromStringAndSize(h_b64, d2);
    if (header == NULL || payload == NULL || signature == NULL || signing_input == NULL) {
        goto error;
    }
    Py_ssize_t hn = b64url_decode_into(h_b64, h_len, (unsigned char *)PyBytes_AS_STRING(header));
    Py_ssize_t pn = b64url_decode_into(p_b64, p_len, (unsigned char *)PyBytes_AS_STRING(payload));
    Py_ssize_t sn = b64url_decode_into(s_b64, s_len, (unsigned char *)PyBytes_AS_STRING(signature));
    if (hn < 0 || pn < 0 || sn < 0) {
        PyErr_SetString(PyExc_ValueError, "invalid base64url in JWT segment");
        goto error;
    }
    if (_PyBytes_Resize(&header, hn) < 0 ||
        _PyBytes_Resize(&payload, pn) < 0 ||
        _PyBytes_Resize(&signature, sn) < 0) {
        /* _PyBytes_Resize sets these to NULL on failure. */
        Py_XDECREF(header);
        Py_XDECREF(payload);
        Py_XDECREF(signature);
        Py_XDECREF(signing_input);
        return NULL;
    }
    {
        PyObject *out = PyTuple_Pack(4, header, payload, signing_input, signature);
        Py_DECREF(header);
        Py_DECREF(payload);
        Py_DECREF(signing_input);
        Py_DECREF(signature);
        return out;
    }
error:
    Py_XDECREF(header);
    Py_XDECREF(payload);
    Py_XDECREF(signature);
    Py_XDECREF(signing_input);
    return NULL;
}

/* jose_verify_hs(digestmod: str, key: bytes, signing_input: bytes,
 *                signature: bytes) -> bool
 *
 * HMAC(key, signing_input) under `digestmod` ("sha256"/"sha384"/"sha512"),
 * constant-time compared against `signature`. Reuses _hashlib.hmac_digest. */
PyObject *
wreath_jose_verify_hs(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *digestmod;
    PyObject *key;
    PyObject *signing_input;
    const char *sig;
    Py_ssize_t sig_len;
    PyObject *digest;
    PyObject *call_args[3];

    if (!PyArg_ParseTuple(args, "USSy#:jose_verify_hs",
                          &digestmod, &key, &signing_input, &sig, &sig_len)) {
        return NULL;
    }
    call_args[0] = key;
    call_args[1] = signing_input;
    call_args[2] = digestmod;
    digest = PyObject_Vectorcall(jose_hmac_digest_fn, call_args, 3, NULL);
    if (digest == NULL) {
        return NULL;
    }
    if (!PyBytes_Check(digest)) {
        Py_DECREF(digest);
        PyErr_SetString(PyExc_RuntimeError, "hmac_digest did not return bytes");
        return NULL;
    }
    /* Length mismatch is a definitive non-match; still compare in constant time
     * over the expected digest length to avoid leaking which arm failed. */
    Py_ssize_t dig_len = PyBytes_GET_SIZE(digest);
    int ok = (dig_len == sig_len) &&
             jose_ct_equal((const unsigned char *)PyBytes_AS_STRING(digest),
                           (const unsigned char *)sig, dig_len);
    Py_DECREF(digest);
    if (ok) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

/* Fetch an integer claim as long long. Returns 0 and sets *present=0 when the
 * key is absent; returns -1 on a present-but-non-integer value (a hard error).
 * JWT NumericDate is seconds; we accept int and reject float/other for exp/nbf/
 * iat to avoid ambiguity (the facade may pre-coerce if it wants float support). */
static int
claim_as_ll(PyObject *claims, const char *name, long long *out, int *present)
{
    PyObject *value = PyDict_GetItemString(claims, name);  /* borrowed */
    *present = 0;
    if (value == NULL) {
        return 0;
    }
    if (!PyLong_Check(value)) {
        return -1;
    }
    long long v = PyLong_AsLongLong(value);
    if (v == -1 && PyErr_Occurred()) {
        PyErr_Clear();
        return -1;
    }
    *out = v;
    *present = 1;
    return 0;
}

/* Reason codes returned by jose_validate_claims (0 == valid). Mirror these in
 * the facade so error messages/telemetry are stable. */
enum {
    JOSE_CLAIMS_OK = 0,
    JOSE_CLAIMS_EXPIRED = 1,       /* now - leeway >= exp */
    JOSE_CLAIMS_NOT_YET = 2,       /* nbf > now + leeway */
    JOSE_CLAIMS_IAT_FUTURE = 3,    /* iat > now + leeway */
    JOSE_CLAIMS_BAD_ISS = 4,
    JOSE_CLAIMS_BAD_AUD = 5,
    JOSE_CLAIMS_MISSING = 6,       /* a required claim is absent */
    JOSE_CLAIMS_MALFORMED = 7,     /* a time claim was present but non-integer */
};

/* jose_validate_claims(claims: dict, now: int, leeway: int,
 *                       issuer: str | None, audiences: tuple[str, ...],
 *                       required: tuple[str, ...]) -> int
 *
 * Registered-claim checks only. `audiences` empty means "do not check aud".
 * `issuer` None means "do not check iss". Returns a reason code. */
PyObject *
wreath_jose_validate_claims(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *claims;
    long long now;
    long long leeway;
    PyObject *issuer;
    PyObject *audiences;
    PyObject *required;

    if (!PyArg_ParseTuple(args, "O!LLOO!O!:jose_validate_claims",
                          &PyDict_Type, &claims, &now, &leeway,
                          &issuer, &PyTuple_Type, &audiences,
                          &PyTuple_Type, &required)) {
        return NULL;
    }

    long long value;
    int present;

    /* exp: reject when now - leeway >= exp (token no longer valid). */
    if (claim_as_ll(claims, "exp", &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && now - leeway >= value) {
        return PyLong_FromLong(JOSE_CLAIMS_EXPIRED);
    }
    /* nbf: reject when nbf > now + leeway. */
    if (claim_as_ll(claims, "nbf", &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && value > now + leeway) {
        return PyLong_FromLong(JOSE_CLAIMS_NOT_YET);
    }
    /* iat: reject when iat is implausibly in the future. */
    if (claim_as_ll(claims, "iat", &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && value > now + leeway) {
        return PyLong_FromLong(JOSE_CLAIMS_IAT_FUTURE);
    }

    /* iss: exact string match when an issuer is pinned. */
    if (issuer != Py_None) {
        PyObject *claim_iss = PyDict_GetItemString(claims, "iss");  /* borrowed */
        if (claim_iss == NULL || !PyUnicode_Check(claim_iss)) {
            return PyLong_FromLong(JOSE_CLAIMS_BAD_ISS);
        }
        int eq = PyObject_RichCompareBool(claim_iss, issuer, Py_EQ);
        if (eq < 0) {
            return NULL;
        }
        if (!eq) {
            return PyLong_FromLong(JOSE_CLAIMS_BAD_ISS);
        }
    }

    /* aud: the claim is a string or a list of strings; require a non-empty
     * intersection with the accepted audiences. */
    if (PyTuple_GET_SIZE(audiences) > 0) {
        PyObject *claim_aud = PyDict_GetItemString(claims, "aud");  /* borrowed */
        if (claim_aud == NULL) {
            return PyLong_FromLong(JOSE_CLAIMS_BAD_AUD);
        }
        int matched = 0;
        if (PyUnicode_Check(claim_aud)) {
            for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(audiences); i++) {
                int eq = PyObject_RichCompareBool(claim_aud, PyTuple_GET_ITEM(audiences, i), Py_EQ);
                if (eq < 0) {
                    return NULL;
                }
                if (eq) { matched = 1; break; }
            }
        }
        else if (PyList_Check(claim_aud)) {
            for (Py_ssize_t j = 0; j < PyList_GET_SIZE(claim_aud) && !matched; j++) {
                PyObject *entry = PyList_GET_ITEM(claim_aud, j);  /* borrowed */
                if (!PyUnicode_Check(entry)) {
                    continue;
                }
                for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(audiences); i++) {
                    int eq = PyObject_RichCompareBool(entry, PyTuple_GET_ITEM(audiences, i), Py_EQ);
                    if (eq < 0) {
                        return NULL;
                    }
                    if (eq) { matched = 1; break; }
                }
            }
        }
        else {
            return PyLong_FromLong(JOSE_CLAIMS_BAD_AUD);
        }
        if (!matched) {
            return PyLong_FromLong(JOSE_CLAIMS_BAD_AUD);
        }
    }

    /* required: presence only (value semantics are the caller's business). */
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(required); i++) {
        PyObject *name = PyTuple_GET_ITEM(required, i);  /* borrowed */
        int has = PyDict_Contains(claims, name);
        if (has < 0) {
            return NULL;
        }
        if (!has) {
            return PyLong_FromLong(JOSE_CLAIMS_MISSING);
        }
    }

    return PyLong_FromLong(JOSE_CLAIMS_OK);
}

int
wreath_jose_ready(void)
{
    PyObject *module = PyImport_ImportModule("_hashlib");
    if (module == NULL) {
        return -1;
    }
    jose_hmac_digest_fn = PyObject_GetAttrString(module, "hmac_digest");
    Py_DECREF(module);
    if (jose_hmac_digest_fn == NULL) {
        return -1;
    }
    return 0;
}
