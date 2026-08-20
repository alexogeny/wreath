#include "hmac_sha256.h"
#include "wreathcore.h"

#include "simd.h"

/* ---- JOSE / JWT operations (see design 02-oidc-jwt-sso-auth) --------------
 *
 * This owns the compact-token parsing and verification operations used by the
 * JWT facade:
 *
 *   - base64url decode of the three compact segments (per token, x3);
 *   - splitting "header.payload.signature" and enforcing hard size caps;
 *   - HS256/384/512 verification through the supplied digest function;
 *   - RS256/384/512 and PS256/384/512 encoded-message validation;
 *   - registered-claim validation (exp/nbf/iat/iss/aud/required), which is
 *     integer and string comparison.
 *
 * Hash constructors are operation inputs. The implementation therefore owns
 * no mutable cryptographic process state.
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

static PyObject *jose_hmac_digest_fn = NULL;   /* _hashlib.hmac_digest */
static PyObject *jose_name_digest = NULL;
static PyObject *jose_claim_exp = NULL;
static PyObject *jose_claim_nbf = NULL;
static PyObject *jose_claim_iat = NULL;
static PyObject *jose_claim_iss = NULL;
static PyObject *jose_claim_aud = NULL;
/* The HS256 signing key's block states; see hmac_sha256.h. */
static WreathHmacKey jose_hs256_key = {NULL};

/* Hard ceilings. A JWT header/payload/signature segment is base64url; these cap
 * the *decoded* byte length to keep a hostile token from allocating unbounded or
 * feeding a giant string to the JSON parser. Callers pass their own cap too. */
#define JOSE_ABS_MAX_TOKEN (1 << 20)   /* 1 MiB compact token, absolute */

/* The alphabet table and the decoder itself live in `simd.h`, which picks a
 * width per call: a JWT payload segment is a few hundred characters, where the
 * vector arm is roughly four times the table-driven loop. */

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

/* RSA verification uses CPython's bigint modular exponentiation and hashlib
 * implementations, and owns every PKCS#1/PSS byte loop here.
 * The hash constructor is supplied by the facade, so this operation creates no
 * process-global cache and performs no per-value imports. */
static PyObject *
jose_hash(PyObject *constructor, const unsigned char *data, Py_ssize_t length)
{
    PyObject *input = PyBytes_FromStringAndSize((const char *)data, length);
    PyObject *object;
    PyObject *digest;
    if (input == NULL) return NULL;
    object = PyObject_CallOneArg(constructor, input);
    Py_DECREF(input);
    if (object == NULL) return NULL;
    digest = PyObject_CallMethodNoArgs(object, jose_name_digest);
    Py_DECREF(object);
    return digest;
}

static int
jose_mgf1(PyObject *constructor, const unsigned char *seed, Py_ssize_t seed_length,
          unsigned char *output, Py_ssize_t output_length, Py_ssize_t hash_length)
{
    unsigned char *input = PyMem_Malloc((size_t)seed_length + 4);
    Py_ssize_t written = 0;
    uint32_t counter = 0;
    if (input == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    memcpy(input, seed, (size_t)seed_length);
    while (written < output_length) {
        PyObject *digest;
        Py_ssize_t width;
        input[seed_length] = (unsigned char)(counter >> 24);
        input[seed_length + 1] = (unsigned char)(counter >> 16);
        input[seed_length + 2] = (unsigned char)(counter >> 8);
        input[seed_length + 3] = (unsigned char)counter;
        digest = jose_hash(constructor, input, seed_length + 4);
        if (digest == NULL) {
            PyMem_Free(input);
            return -1;
        }
        if (!PyBytes_Check(digest) || PyBytes_GET_SIZE(digest) != hash_length) {
            Py_DECREF(digest);
            PyMem_Free(input);
            PyErr_SetString(PyExc_TypeError, "RSA hash constructor returned the wrong digest size");
            return -1;
        }
        width = output_length - written < hash_length
                    ? output_length - written : hash_length;
        memcpy(output + written, PyBytes_AS_STRING(digest), (size_t)width);
        written += width;
        counter++;
        Py_DECREF(digest);
    }
    PyMem_Free(input);
    return 0;
}

PyObject *
wreath_jose_verify_rsa(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *modulus, *exponent, *constructor, *digest_object, *signature_object;
    Py_ssize_t key_bytes;
    int pss;
    const unsigned char *signature, *digest;
    Py_ssize_t signature_length, digest_length;
    PyObject *encoded_integer = NULL, *message_integer = NULL, *encoded = NULL;
    int answer = 0;
    static const unsigned char sha256_info[] = {
        0x30,0x31,0x30,0x0d,0x06,0x09,0x60,0x86,0x48,0x01,0x65,0x03,0x04,0x02,0x01,
        0x05,0x00,0x04,0x20,
    };
    static const unsigned char sha384_info[] = {
        0x30,0x41,0x30,0x0d,0x06,0x09,0x60,0x86,0x48,0x01,0x65,0x03,0x04,0x02,0x02,
        0x05,0x00,0x04,0x30,
    };
    static const unsigned char sha512_info[] = {
        0x30,0x51,0x30,0x0d,0x06,0x09,0x60,0x86,0x48,0x01,0x65,0x03,0x04,0x02,0x03,
        0x05,0x00,0x04,0x40,
    };
    if (!PyArg_ParseTuple(args, "OOnOOOp:jose_verify_rsa", &modulus, &exponent,
                          &key_bytes, &constructor, &digest_object,
                          &signature_object, &pss)) return NULL;
    if (!PyLong_Check(modulus) || !PyLong_Check(exponent) || key_bytes <= 0 ||
        !PyCallable_Check(constructor) ||
        PyBytes_AsStringAndSize(digest_object, (char **)&digest, &digest_length) < 0 ||
        PyBytes_AsStringAndSize(signature_object, (char **)&signature,
                                &signature_length) < 0) return NULL;
    if (signature_length != key_bytes) Py_RETURN_FALSE;
    encoded_integer = PyLong_FromUnsignedNativeBytes(
        signature, (size_t)signature_length,
        Py_ASNATIVEBYTES_BIG_ENDIAN | Py_ASNATIVEBYTES_UNSIGNED_BUFFER);
    if (encoded_integer == NULL) goto error;
    {
        int outside = PyObject_RichCompareBool(encoded_integer, modulus, Py_GE);
        if (outside < 0) goto error;
        if (outside) goto done;
    }
    message_integer = PyNumber_Power(encoded_integer, exponent, modulus);
    if (message_integer == NULL) goto error;
    if (!pss) {
        const unsigned char *prefix;
        Py_ssize_t prefix_length;
        Py_ssize_t padding;
        if (digest_length == 32) { prefix = sha256_info; prefix_length = sizeof(sha256_info); }
        else if (digest_length == 48) { prefix = sha384_info; prefix_length = sizeof(sha384_info); }
        else if (digest_length == 64) { prefix = sha512_info; prefix_length = sizeof(sha512_info); }
        else {
            PyErr_SetString(PyExc_ValueError, "RSA verification needs SHA-256, SHA-384, or SHA-512");
            goto error;
        }
        if (key_bytes < prefix_length + digest_length + 11) goto done;
        encoded = PyBytes_FromStringAndSize(NULL, key_bytes);
        if (encoded == NULL) goto error;
        {
            Py_ssize_t encoded_size = PyLong_AsNativeBytes(
                message_integer, PyBytes_AS_STRING(encoded), key_bytes,
                Py_ASNATIVEBYTES_BIG_ENDIAN | Py_ASNATIVEBYTES_UNSIGNED_BUFFER);
            if (encoded_size == -1 && PyErr_Occurred()) goto error;
            if (encoded_size > key_bytes) goto done;
        }
        padding = key_bytes - prefix_length - digest_length - 3;
        const unsigned char *raw = (const unsigned char *)PyBytes_AS_STRING(encoded);
        if (raw[0] != 0 || raw[1] != 1 || raw[padding + 2] != 0) goto done;
        for (Py_ssize_t i = 0; i < padding; i++) if (raw[i + 2] != 0xff) goto done;
        if (!jose_ct_equal(raw + padding + 3, prefix, prefix_length)) goto done;
        answer = jose_ct_equal(raw + padding + 3 + prefix_length, digest, digest_length);
    }
    else {
        PyObject *bits_object = PyObject_CallMethod(modulus, "bit_length", NULL);
        Py_ssize_t bits, em_length, db_length, padding;
        unsigned char *raw, *mask;
        PyObject *prime = NULL;
        if (bits_object == NULL) goto error;
        bits = PyLong_AsSsize_t(bits_object) - 1;
        Py_DECREF(bits_object);
        if (bits < 0 || (bits == -1 && PyErr_Occurred())) goto error;
        em_length = (bits + 7) / 8;
        if (em_length < digest_length * 2 + 2) goto done;
        encoded = PyBytes_FromStringAndSize(NULL, em_length);
        if (encoded == NULL) goto error;
        {
            Py_ssize_t encoded_size = PyLong_AsNativeBytes(
                message_integer, PyBytes_AS_STRING(encoded), em_length,
                Py_ASNATIVEBYTES_BIG_ENDIAN | Py_ASNATIVEBYTES_UNSIGNED_BUFFER);
            if (encoded_size == -1 && PyErr_Occurred()) goto error;
            if (encoded_size > em_length) goto done;
        }
        raw = (unsigned char *)PyBytes_AS_STRING(encoded);
        if (raw[em_length - 1] != 0xbc) goto done;
        db_length = em_length - digest_length - 1;
        {
            int top_bits = (int)(8 * em_length - bits);
            if (top_bits && (raw[0] & (unsigned char)(0xff << (8 - top_bits)))) goto done;
        }
        mask = PyMem_Malloc((size_t)db_length);
        if (mask == NULL) { PyErr_NoMemory(); goto error; }
        if (jose_mgf1(constructor, raw + db_length, digest_length,
                      mask, db_length, digest_length) < 0) {
            PyMem_Free(mask);
            goto error;
        }
        for (Py_ssize_t i = 0; i < db_length; i++) raw[i] ^= mask[i];
        PyMem_Free(mask);
        {
            int top_bits = (int)(8 * em_length - bits);
            if (top_bits) raw[0] &= (unsigned char)(0xff >> top_bits);
        }
        padding = em_length - digest_length * 2 - 2;
        for (Py_ssize_t i = 0; i < padding; i++) if (raw[i] != 0) goto done;
        if (raw[padding] != 1) goto done;
        {
            Py_ssize_t prime_length = 8 + digest_length * 2;
            unsigned char *prime_raw;
            prime = PyBytes_FromStringAndSize(NULL, prime_length);
            if (prime == NULL) goto error;
            prime_raw = (unsigned char *)PyBytes_AS_STRING(prime);
            memset(prime_raw, 0, 8);
            memcpy(prime_raw + 8, digest, (size_t)digest_length);
            memcpy(prime_raw + 8 + digest_length, raw + db_length - digest_length,
                   (size_t)digest_length);
            PyObject *hashed = jose_hash(constructor, prime_raw, prime_length);
            Py_DECREF(prime);
            if (hashed == NULL) goto error;
            answer = PyBytes_Check(hashed) && PyBytes_GET_SIZE(hashed) == digest_length &&
                jose_ct_equal((const unsigned char *)PyBytes_AS_STRING(hashed),
                              raw + db_length, digest_length);
            Py_DECREF(hashed);
        }
    }
done:
    Py_XDECREF(encoded);
    Py_XDECREF(message_integer);
    Py_XDECREF(encoded_integer);
    return PyBool_FromLong(answer);
error:
    Py_XDECREF(encoded);
    Py_XDECREF(message_integer);
    Py_XDECREF(encoded_integer);
    return NULL;
}

/* Decode `len` base64url chars into `out` (caller-sized >= (len/4)*3 + 2).
 * Returns the decoded byte count, or -1 on any invalid char / illegal length. */
static Py_ssize_t
b64url_decode_into(const char *in, Py_ssize_t len, unsigned char *out)
{
    return (Py_ssize_t)wreath_b64url_decode(in, (ptrdiff_t)len, out);
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
    /* Wording is shared with `_auth/jwt.py`, deliberately: the two are one
       contract, and an error message that names which code parsed the token is
       a difference callers can trip over. An *empty* token is not
       handled here -- it falls through to the separator count below and is
       answered "must have exactly two dots", which is both the more accurate
       diagnosis and what the Python branch already said. */
    if (tok_len > JOSE_ABS_MAX_TOKEN) {
        PyErr_SetString(PyExc_ValueError, "compact JWT exceeds maximum size");
        return NULL;
    }
    /* Find exactly two '.' separators.
     *
     * One byte at a time, where three `wreath_memmem(..., ".", 1)` calls would
     * each reach glibc's vectorised `memchr`. Left scalar deliberately, and
     * this is the one of these scans whose length is caller-supplied: a real
     * compact JWT is a few hundred to a couple of thousand bytes, and
     * `JOSE_ABS_MAX_TOKEN` (1 MiB) is the absolute refusal above. Two things
     * make the rewrite not worth it even so. The byte count is the same either
     * way -- proving there is no *third* dot means reading to the end of the
     * token whichever primitive does it, so there is no early exit to win --
     * and at the realistic length the whole scan is under a microsecond
     * against a signature verification measured in milliseconds. Revisit only
     * if something starts handing this megabyte tokens, which the cap above
     * says is a refusal rather than a workload. */
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
        /* Covers both causes -- a character outside the alphabet, and a length
           that is not a valid unpadded base64 length -- because both are the
           same instruction to the caller. Shared verbatim with `_auth/jwt.py`. */
        PyErr_SetString(PyExc_ValueError, "a compact JWT segment must be unpadded base64url");
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
    /* HS256 is the overwhelming majority of JWTs in this framework, and it is
     * the one digest whose key schedule is cached (`hmac_sha256.h`: SHA-384 and
     * SHA-512 use a 128-byte HMAC block, which that helper does not model). The
     * other two keep the general path -- same digest, same comparison, one
     * `hmac.digest` call. Nothing degrades silently: both arms answer from the
     * same definition, and `tests/test_jose_native_parity.py` holds them to
     * `hmac.digest` for every algorithm. */
    unsigned char cached[32];
    if (PyUnicode_CompareWithASCIIString(digestmod, "sha256") == 0) {
        if (wreath_hmac_sha256(&jose_hs256_key, key,
                               PyBytes_AS_STRING(signing_input),
                               PyBytes_GET_SIZE(signing_input), cached) < 0) {
            return NULL;
        }
        digest = PyBytes_FromStringAndSize((const char *)cached, 32);
        if (digest == NULL) {
            return NULL;
        }
    }
    else {
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
claim_as_ll(PyObject *claims, PyObject *name, long long *out, int *present)
{
    PyObject *value = PyDict_GetItemWithError(claims, name);  /* borrowed */
    if (value == NULL && PyErr_Occurred()) return -1;
    *present = 0;
    if (value == NULL) {
        return 0;
    }
    /* `PyBool_Check` first: `bool` subclasses `int`, so `PyLong_Check` is true
     * for `True`/`False` and a boolean would read through as the timestamp 1 or
     * 0. RFC 7519 section 2 makes a NumericDate a JSON *number*, so a boolean
     * is malformed -- and saying so matters even though both values happen to
     * land on "expired" today, because that is one comparison change away from
     * reading `true` as a far-future date. */
    if (PyBool_Check(value) || !PyLong_Check(value)) {
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
    if (claim_as_ll(claims, jose_claim_exp, &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && now - leeway >= value) {
        return PyLong_FromLong(JOSE_CLAIMS_EXPIRED);
    }
    /* nbf: reject when nbf > now + leeway. */
    if (claim_as_ll(claims, jose_claim_nbf, &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && value > now + leeway) {
        return PyLong_FromLong(JOSE_CLAIMS_NOT_YET);
    }
    /* iat: reject when iat is implausibly in the future. */
    if (claim_as_ll(claims, jose_claim_iat, &value, &present) < 0) {
        return PyLong_FromLong(JOSE_CLAIMS_MALFORMED);
    }
    if (present && value > now + leeway) {
        return PyLong_FromLong(JOSE_CLAIMS_IAT_FUTURE);
    }

    /* iss: exact string match when an issuer is pinned. */
    if (issuer != Py_None) {
        PyObject *claim_iss = PyDict_GetItemWithError(claims, jose_claim_iss);
        if (claim_iss == NULL && PyErr_Occurred()) return NULL;
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
        PyObject *claim_aud = PyDict_GetItemWithError(claims, jose_claim_aud);
        if (claim_aud == NULL && PyErr_Occurred()) return NULL;
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
    jose_name_digest = PyUnicode_InternFromString("digest");
    jose_claim_exp = PyUnicode_InternFromString("exp");
    jose_claim_nbf = PyUnicode_InternFromString("nbf");
    jose_claim_iat = PyUnicode_InternFromString("iat");
    jose_claim_iss = PyUnicode_InternFromString("iss");
    jose_claim_aud = PyUnicode_InternFromString("aud");
    if (jose_name_digest == NULL || jose_claim_exp == NULL ||
        jose_claim_nbf == NULL || jose_claim_iat == NULL ||
        jose_claim_iss == NULL || jose_claim_aud == NULL ||
        wreath_hmac_sha256_ready() < 0) {
        return -1;
    }
    return 0;
}
