/* HMAC-SHA256 with the key's block states absorbed once instead of per call.
 *
 * HMAC is `H((K^opad) || H((K^ipad) || message))`. The two key blocks depend
 * only on the key, and every key this framework signs with -- a CSRF secret, a
 * JWT signing key -- is fixed for the life of the process. `hmac.digest` cannot
 * know that, so it pads the key, XORs both masks and compresses both blocks on
 * every single call: two of the four SHA-256 compressions in every signature,
 * recomputed from the same input to the same answer.
 *
 * Absorbing each key block once and copying the resulting hash state per call
 * removes both. This is HMAC's own definition rather than an approximation, so
 * the digest is identical; `tests/test_csrf_native_parity.py` and
 * `tests/test_jose_native_parity.py` hold it to `hmac.digest` differentially,
 * including the branches this introduces (a key longer than the block, which
 * HMAC replaces with its digest, and a shorter one, which it zero-pads).
 *
 * SHA-256 itself is *not* reimplemented. `_hashlib`'s is OpenSSL's, with the
 * CPU's SHA extensions where it has them, and a scalar C version measured
 * slower than the call it would have replaced. The saving here is arithmetic
 * that was being repeated, not a faster hash.
 *
 * Measured A/B, 9 alternating rounds against an A/A control that resolved
 * 1.000x +/- 20ns:
 *
 *     csrf_sign        1377ns -> 888ns   1.55x
 *     csrf_new_token   1470ns -> 969ns   1.52x
 *     csrf_validate    1529ns -> 1018ns  1.50x
 *
 * On process-global state: these are and derived from key material,
 * which the note above `fill_random` refuses for pre-drawn *random* bytes. The
 * trade differs. Cached randomness is unused key material that exists only
 * because it was kept; these states are a pure function of a secret the caller
 * already holds for the process lifetime, so keeping them exposes nothing that
 * was not already resident.
 */
#ifndef WREATH_HMAC_SHA256_H
#define WREATH_HMAC_SHA256_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>

/* One cache line: a `(key, sha256(key^ipad), sha256(key^opad))` tuple, or NULL
 * before the first use. Each caller owns one, so two subsystems signing with
 * different keys do not evict each other.
 *
 * Held as a single tuple rather than three fields so that a reader cannot pair
 * one key's inner state with another key's outer state: the pointer is read
 * once into a local and a rebuild swaps in a whole new tuple. That matters only
 * under free-threading, where two applications with different keys could
 * otherwise interleave a rebuild.
 */
typedef struct {
    PyObject *seeds;
} WreathHmacKey;

/* Resolve `_hashlib.openssl_sha256` and the method names. Idempotent; call from
 * each module's ready hook. Returns 0, or -1 with an exception set. */
int wreath_hmac_sha256_ready(void);

/* HMAC-SHA256(key, message) -> out32, reusing `cache` when it already holds
 * this key. Returns 0, or -1 with an exception set. */
int wreath_hmac_sha256(WreathHmacKey *cache, PyObject *key,
                       const char *message, Py_ssize_t message_len,
                       unsigned char out32[32]);

#endif /* WREATH_HMAC_SHA256_H */
