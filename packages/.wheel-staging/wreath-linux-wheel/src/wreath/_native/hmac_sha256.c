/* See hmac_sha256.h for why this exists and what it measured. */
#include "hmac_sha256.h"

#include <string.h>

/* SHA-256's block, and so HMAC's. A key longer than this is replaced by its own
 * digest; a shorter one is zero-padded up to it. */
#define WREATH_HMAC_BLOCK 64

static PyObject *sha256_ctor = NULL;   /* _hashlib.openssl_sha256 */
static PyObject *str_copy = NULL;
static PyObject *str_update = NULL;
static PyObject *str_digest = NULL;

int
wreath_hmac_sha256_ready(void)
{
    if (sha256_ctor != NULL) {
        return 0;
    }
    PyObject *module = PyImport_ImportModule("_hashlib");
    if (module == NULL) {
        return -1;
    }
    sha256_ctor = PyObject_GetAttrString(module, "openssl_sha256");
    Py_DECREF(module);
    if (sha256_ctor == NULL) {
        return -1;
    }
    str_copy = PyUnicode_InternFromString("copy");
    str_update = PyUnicode_InternFromString("update");
    str_digest = PyUnicode_InternFromString("digest");
    if (str_copy == NULL || str_update == NULL || str_digest == NULL) {
        return -1;
    }
    return 0;
}

/* The prepared key block: the digest of an over-long key, or the key itself
 * zero-padded. Returns 0, or -1 with an exception set. */
static int
prepare_block(PyObject *key, unsigned char block[WREATH_HMAC_BLOCK])
{
    Py_ssize_t length = PyBytes_GET_SIZE(key);
    if (length > WREATH_HMAC_BLOCK) {
        PyObject *hash = PyObject_CallOneArg(sha256_ctor, key);
        if (hash == NULL) {
            return -1;
        }
        PyObject *digest = PyObject_CallMethodNoArgs(hash, str_digest);
        Py_DECREF(hash);
        if (digest == NULL) {
            return -1;
        }
        if (!PyBytes_Check(digest) || PyBytes_GET_SIZE(digest) != 32) {
            Py_DECREF(digest);
            PyErr_SetString(PyExc_RuntimeError, "sha256 digest was not 32 bytes");
            return -1;
        }
        memcpy(block, PyBytes_AS_STRING(digest), 32);
        memset(block + 32, 0, WREATH_HMAC_BLOCK - 32);
        Py_DECREF(digest);
        return 0;
    }
    memcpy(block, PyBytes_AS_STRING(key), (size_t)length);
    memset(block + length, 0, (size_t)(WREATH_HMAC_BLOCK - length));
    return 0;
}

/* A hash object with `block ^ mask` already absorbed. */
static PyObject *
absorb(const unsigned char block[WREATH_HMAC_BLOCK], unsigned char mask)
{
    unsigned char padded[WREATH_HMAC_BLOCK];
    for (int i = 0; i < WREATH_HMAC_BLOCK; i++) {
        padded[i] = block[i] ^ mask;
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)padded,
                                                WREATH_HMAC_BLOCK);
    if (bytes == NULL) {
        return NULL;
    }
    PyObject *hash = PyObject_CallOneArg(sha256_ctor, bytes);
    Py_DECREF(bytes);
    return hash;
}

/* The seed tuple for `key`, rebuilding it when the cache holds another key.
 * Returns a new reference, so a concurrent rebuild cannot free it mid-use. */
static PyObject *
seeds_for(WreathHmacKey *cache, PyObject *key)
{
    PyObject *held = cache->seeds;   /* one read; see the note in the header */
    if (held != NULL) {
        PyObject *cached_key = PyTuple_GET_ITEM(held, 0);
        Py_ssize_t size = PyBytes_GET_SIZE(cached_key);
        if (cached_key == key ||
            (size == PyBytes_GET_SIZE(key) &&
             memcmp(PyBytes_AS_STRING(cached_key), PyBytes_AS_STRING(key),
                    (size_t)size) == 0)) {
            return Py_NewRef(held);
        }
    }

    unsigned char block[WREATH_HMAC_BLOCK];
    if (prepare_block(key, block) < 0) {
        return NULL;
    }
    PyObject *inner = absorb(block, 0x36);
    if (inner == NULL) {
        return NULL;
    }
    PyObject *outer = absorb(block, 0x5c);
    if (outer == NULL) {
        Py_DECREF(inner);
        return NULL;
    }
    PyObject *fresh = PyTuple_Pack(3, key, inner, outer);
    Py_DECREF(inner);
    Py_DECREF(outer);
    if (fresh == NULL) {
        return NULL;
    }
    Py_XSETREF(cache->seeds, Py_NewRef(fresh));
    return fresh;
}

/* `hash.copy()`, then `.update(data)`, then `.digest()`. */
static PyObject *
digest_over(PyObject *seed, PyObject *data)
{
    PyObject *hash = PyObject_CallMethodNoArgs(seed, str_copy);
    if (hash == NULL) {
        return NULL;
    }
    PyObject *ignored = PyObject_CallMethodOneArg(hash, str_update, data);
    if (ignored == NULL) {
        Py_DECREF(hash);
        return NULL;
    }
    Py_DECREF(ignored);
    PyObject *digest = PyObject_CallMethodNoArgs(hash, str_digest);
    Py_DECREF(hash);
    return digest;
}

int
wreath_hmac_sha256(WreathHmacKey *cache, PyObject *key,
                   const char *message, Py_ssize_t message_len,
                   unsigned char out32[32])
{
    if (!PyBytes_Check(key)) {
        PyErr_SetString(PyExc_TypeError, "hmac key must be bytes");
        return -1;
    }
    PyObject *seeds = seeds_for(cache, key);
    if (seeds == NULL) {
        return -1;
    }

    int status = -1;
    PyObject *payload = PyBytes_FromStringAndSize(message, message_len);
    PyObject *inner_digest = NULL;
    PyObject *outer_digest = NULL;
    if (payload == NULL) {
        goto done;
    }
    inner_digest = digest_over(PyTuple_GET_ITEM(seeds, 1), payload);
    if (inner_digest == NULL) {
        goto done;
    }
    outer_digest = digest_over(PyTuple_GET_ITEM(seeds, 2), inner_digest);
    if (outer_digest == NULL) {
        goto done;
    }
    if (!PyBytes_Check(outer_digest) || PyBytes_GET_SIZE(outer_digest) != 32) {
        PyErr_SetString(PyExc_RuntimeError, "sha256 digest was not 32 bytes");
        goto done;
    }
    memcpy(out32, PyBytes_AS_STRING(outer_digest), 32);
    status = 0;

done:
    Py_XDECREF(outer_digest);
    Py_XDECREF(inner_digest);
    Py_XDECREF(payload);
    Py_DECREF(seeds);
    return status;
}
