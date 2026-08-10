/* AES-128-GCM on AES-NI and PCLMULQDQ, with a portable Python arm beside it.
 *
 * CPython ships no AES, so `wreath._webpush` writes AES-128-GCM out in Python
 * for RFC 8291 web push. That is correct and it is slow: measured on this repo,
 * 11.8ms for one 4000-byte record, of which the block cipher is 81% and GHASH
 * 31%. A fan-out to 10,000 push subscribers is therefore about two minutes of
 * CPU, spent entirely in interpreter bytecode.
 *
 * Both halves of that have a hardware instruction:
 *
 *   AES-NI       `aesenc`/`aesenclast`/`aeskeygenassist` do a whole AES round
 *                per instruction, so the ten rounds of a block become ten
 *                instructions instead of ten table-driven Python loops.
 *   PCLMULQDQ    `pclmulqdq` is the carry-less multiply GHASH is defined in
 *                terms of, replacing the 128-iteration shift-and-xor loop that
 *                the Python `_gf_mul` runs per 16 bytes.
 *
 * AVX2 is deliberately *not* used. Emulating AES in 256-bit lane arithmetic is
 * strictly worse than the instruction that exists, and the only thing left to
 * widen -- the XOR of the keystream against the plaintext -- is already folded
 * into the same registers the cipher produces, so a wider XOR would need an
 * extra pass over the buffer to do less work per byte than it costs to make.
 *
 * ## What is constant time here, and what is not
 *
 * Everything in this file is. `aesenc` and `pclmulqdq` are fixed-latency
 * instructions with no data-dependent memory access, there is no S-box table to
 * miss in cache, and the tag comparison is a branch-free XOR-accumulate. That
 * is the substantive security difference from the fallback, not a side effect.
 *
 * **The Python arm is not constant time**, and this file does not change that.
 * `wreath._webpush._SBOX` is a byte table indexed by secret state, so it leaks
 * through the cache, and `_gf_mul` branches on the bits of its operand. A
 * machine without AES-NI runs that code, and `wreath._webpush`'s docstring says
 * so in the same words. Do not read "the native path is constant time" as
 * "AES-GCM in Wreath is constant time" -- which one you get depends on the CPU.
 *
 * ## Structure
 *
 * One arm, not four. The portable path is Python, and both are pinned to the
 * NIST SP 800-38D vectors by `tests/test_aesgcm_parity.py`, so there is nothing
 * here for `simd_probe` to cross. Feature detection lives in `simd.h` beside
 * the AVX2 detection it copies; `aesgcm_arms()` reports the result, and
 * `wreath._webpush` binds the Python arm when it comes back empty.
 *
 * GHASH runs as its own pass over the ciphertext rather than folded into the
 * counter-mode loop. It costs a second pass over a buffer that RFC 8291 caps at
 * 4096 bytes -- L1-resident, and unmeasurable next to the ~3us the whole record
 * takes -- and it buys a loop with no `encrypting` branch in its body and no
 * duplicated tail handling.
 */
#include "wreathcore.h"

/* The tag, and the block, are both 16 bytes; naming them separately is how a
 * length check ends up guarding the wrong one. */
#define WREATH_GCM_TAG_BYTES 16
#define WREATH_GCM_BLOCK_BYTES 16
#define WREATH_GCM_NONCE_BYTES 12
#define WREATH_AES128_KEY_BYTES 16

/* Raised when the module is asked for a hardware path the CPU or the build does
 * not have. `wreath._webpush` never reaches it: it asks `aesgcm_arms()` once at
 * import and binds the Python arm when the answer is empty. Defined outside
 * both arms of the guard below so the message cannot drift between them. */
static PyObject *
wreath_aesgcm_unavailable(const char *name)
{
    PyErr_Format(PyExc_NotImplementedError,
                 "%s needs AES-NI and PCLMULQDQ; this build or CPU has neither, "
                 "so wreath._webpush uses its Python implementation instead",
                 name);
    return NULL;
}

#if defined(WREATH_HAVE_AESGCM)

/* --- AES-128 key schedule ------------------------------------------------ */

/* One step of the AES-128 schedule: rotate-substitute-rcon the previous round
 * key's last word (which `aeskeygenassist` has already computed into lane 3),
 * then chain it back through the four words. */
WREATH_TARGET_AESGCM static inline __m128i
wreath_aes128_key_step(__m128i key, __m128i assist)
{
    assist = _mm_shuffle_epi32(assist, 0xFF);
    key = _mm_xor_si128(key, _mm_slli_si128(key, 4));
    key = _mm_xor_si128(key, _mm_slli_si128(key, 4));
    key = _mm_xor_si128(key, _mm_slli_si128(key, 4));
    return _mm_xor_si128(key, assist);
}

/* Unrolled because `aeskeygenassist`'s round constant is an immediate operand
 * and a loop counter is not one. */
WREATH_TARGET_AESGCM static void
wreath_aes128_expand(const uint8_t *key, __m128i round_keys[11])
{
    round_keys[0] = _mm_loadu_si128((const __m128i *)(const void *)key);
    round_keys[1] = wreath_aes128_key_step(
        round_keys[0], _mm_aeskeygenassist_si128(round_keys[0], 0x01));
    round_keys[2] = wreath_aes128_key_step(
        round_keys[1], _mm_aeskeygenassist_si128(round_keys[1], 0x02));
    round_keys[3] = wreath_aes128_key_step(
        round_keys[2], _mm_aeskeygenassist_si128(round_keys[2], 0x04));
    round_keys[4] = wreath_aes128_key_step(
        round_keys[3], _mm_aeskeygenassist_si128(round_keys[3], 0x08));
    round_keys[5] = wreath_aes128_key_step(
        round_keys[4], _mm_aeskeygenassist_si128(round_keys[4], 0x10));
    round_keys[6] = wreath_aes128_key_step(
        round_keys[5], _mm_aeskeygenassist_si128(round_keys[5], 0x20));
    round_keys[7] = wreath_aes128_key_step(
        round_keys[6], _mm_aeskeygenassist_si128(round_keys[6], 0x40));
    round_keys[8] = wreath_aes128_key_step(
        round_keys[7], _mm_aeskeygenassist_si128(round_keys[7], 0x80));
    round_keys[9] = wreath_aes128_key_step(
        round_keys[8], _mm_aeskeygenassist_si128(round_keys[8], 0x1B));
    round_keys[10] = wreath_aes128_key_step(
        round_keys[9], _mm_aeskeygenassist_si128(round_keys[9], 0x36));
}

WREATH_TARGET_AESGCM static inline __m128i
wreath_aes128_block(const __m128i round_keys[11], __m128i block)
{
    block = _mm_xor_si128(block, round_keys[0]);
    for (int round = 1; round < 10; round++) {
        block = _mm_aesenc_si128(block, round_keys[round]);
    }
    return _mm_aesenclast_si128(block, round_keys[10]);
}

/* Four blocks with the rounds interleaved.
 *
 * `aesenc` has a latency of about four cycles and a throughput of one per
 * cycle, so a serial chain of ten of them idles the unit three cycles in four.
 * Four independent chains fill it. This is the whole reason counter mode is
 * fast on this hardware and it is why the loop below steps 64 bytes: the win is
 * not vector width, it is the dependency chain. */
WREATH_TARGET_AESGCM static inline void
wreath_aes128_block4(const __m128i round_keys[11], __m128i blocks[4])
{
    __m128i b0 = _mm_xor_si128(blocks[0], round_keys[0]);
    __m128i b1 = _mm_xor_si128(blocks[1], round_keys[0]);
    __m128i b2 = _mm_xor_si128(blocks[2], round_keys[0]);
    __m128i b3 = _mm_xor_si128(blocks[3], round_keys[0]);
    for (int round = 1; round < 10; round++) {
        b0 = _mm_aesenc_si128(b0, round_keys[round]);
        b1 = _mm_aesenc_si128(b1, round_keys[round]);
        b2 = _mm_aesenc_si128(b2, round_keys[round]);
        b3 = _mm_aesenc_si128(b3, round_keys[round]);
    }
    blocks[0] = _mm_aesenclast_si128(b0, round_keys[10]);
    blocks[1] = _mm_aesenclast_si128(b1, round_keys[10]);
    blocks[2] = _mm_aesenclast_si128(b2, round_keys[10]);
    blocks[3] = _mm_aesenclast_si128(b3, round_keys[10]);
}

/* --- GHASH --------------------------------------------------------------- */

/* GCM numbers the bits of a block the opposite way round from the way a
 * register does: the first bit of the first byte is the coefficient of x^0. A
 * whole-register byte reversal maps that onto the ordinary polynomial basis,
 * where bit k of the register is the coefficient of x^(127-k) -- which is the
 * form `pclmulqdq` and the reduction below are written for. Everything between
 * the load and the store stays reversed; only the boundary swaps. */
WREATH_TARGET_AESGCM static inline __m128i
wreath_gcm_bswap_mask(void)
{
    return _mm_setr_epi8(15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0);
}

/* Carry-less multiply in GF(2^128) with GCM's reduction polynomial
 * x^128 + x^7 + x^2 + x + 1, on byte-reversed operands.
 *
 * Four `pclmulqdq`s give the 255-bit product; the middle two are folded
 * together and split across the halves. The product is then shifted left one
 * bit -- the reversed representation costs exactly that one bit -- and reduced
 * in two shift-and-xor stages, which is Intel's published sequence for this
 * polynomial rather than anything invented here. The whole thing is checked
 * against `wreath._webpush._gf_mul`, a plain 128-iteration definition, over
 * every ciphertext length in `tests/test_aesgcm_parity.py`. */
WREATH_TARGET_AESGCM static inline __m128i
wreath_gcm_gfmul(__m128i a, __m128i b)
{
    __m128i lo = _mm_clmulepi64_si128(a, b, 0x00);
    __m128i mid_a = _mm_clmulepi64_si128(a, b, 0x10);
    __m128i mid_b = _mm_clmulepi64_si128(a, b, 0x01);
    __m128i hi = _mm_clmulepi64_si128(a, b, 0x11);
    __m128i carry;
    __m128i shift_lo;
    __m128i shift_hi;
    __m128i spill;
    __m128i fold;
    __m128i tail;

    mid_a = _mm_xor_si128(mid_a, mid_b);
    lo = _mm_xor_si128(lo, _mm_slli_si128(mid_a, 8));
    hi = _mm_xor_si128(hi, _mm_srli_si128(mid_a, 8));

    /* <<1 over the whole 256-bit value: shift each dword and carry the bit that
     * left it into the next one up. */
    shift_lo = _mm_srli_epi32(lo, 31);
    shift_hi = _mm_srli_epi32(hi, 31);
    lo = _mm_slli_epi32(lo, 1);
    hi = _mm_slli_epi32(hi, 1);
    spill = _mm_srli_si128(shift_lo, 12);
    shift_hi = _mm_slli_si128(shift_hi, 4);
    shift_lo = _mm_slli_si128(shift_lo, 4);
    lo = _mm_or_si128(lo, shift_lo);
    hi = _mm_or_si128(_mm_or_si128(hi, shift_hi), spill);

    /* First reduction stage: the three taps of the polynomial, folded into the
     * low half and carried out of it. */
    fold = _mm_xor_si128(_mm_xor_si128(_mm_slli_epi32(lo, 31), _mm_slli_epi32(lo, 30)),
                         _mm_slli_epi32(lo, 25));
    carry = _mm_srli_si128(fold, 4);
    lo = _mm_xor_si128(lo, _mm_slli_si128(fold, 12));

    /* Second stage: the same taps on the way back down, plus the carry. */
    tail = _mm_xor_si128(_mm_xor_si128(_mm_srli_epi32(lo, 1), _mm_srli_epi32(lo, 2)),
                         _mm_srli_epi32(lo, 7));
    tail = _mm_xor_si128(tail, carry);
    return _mm_xor_si128(hi, _mm_xor_si128(lo, tail));
}

/* Accumulate `len` bytes into `state`, zero-padding a trailing partial block --
 * which is what GCM specifies for both the AAD and the ciphertext. */
WREATH_TARGET_AESGCM static __m128i
wreath_gcm_ghash(__m128i state, __m128i h, const uint8_t *data, Py_ssize_t len)
{
    const __m128i bswap = wreath_gcm_bswap_mask();
    Py_ssize_t offset = 0;
    for (; len - offset >= WREATH_GCM_BLOCK_BYTES; offset += WREATH_GCM_BLOCK_BYTES) {
        __m128i block = _mm_shuffle_epi8(
            _mm_loadu_si128((const __m128i *)(const void *)(data + offset)), bswap);
        state = wreath_gcm_gfmul(_mm_xor_si128(state, block), h);
    }
    if (offset < len) {
        uint8_t padded[WREATH_GCM_BLOCK_BYTES] = {0};
        memcpy(padded, data + offset, (size_t)(len - offset));
        __m128i block = _mm_shuffle_epi8(
            _mm_loadu_si128((const __m128i *)(const void *)padded), bswap);
        state = wreath_gcm_gfmul(_mm_xor_si128(state, block), h);
    }
    return state;
}

/* --- counter mode -------------------------------------------------------- */

/* J0 for a 96-bit nonce is `nonce || 0x00000001` (SP 800-38D 7.1), and every
 * subsequent block increments the low 32 bits. Built here rather than kept as a
 * live register because the increment is defined on the big-endian integer and
 * SSE2 has no 128-bit add; the store-and-load costs a couple of cycles against
 * the ten `aesenc`s that follow it. */
WREATH_TARGET_AESGCM static inline __m128i
wreath_gcm_counter_block(const uint8_t *nonce, uint32_t counter)
{
    uint8_t block[WREATH_GCM_BLOCK_BYTES];
    memcpy(block, nonce, WREATH_GCM_NONCE_BYTES);
    block[12] = (uint8_t)(counter >> 24);
    block[13] = (uint8_t)(counter >> 16);
    block[14] = (uint8_t)(counter >> 8);
    block[15] = (uint8_t)counter;
    return _mm_loadu_si128((const __m128i *)(const void *)block);
}

/* XOR `len` bytes of `src` with the AES-CTR keystream from counter 2 onwards,
 * into `dst`. `dst` and `src` may be the same buffer but must not overlap
 * partially; the callers here always pass distinct allocations. */
WREATH_TARGET_AESGCM static void
wreath_gcm_ctr(const __m128i round_keys[11], const uint8_t *nonce,
               const uint8_t *src, uint8_t *dst, Py_ssize_t len)
{
    Py_ssize_t offset = 0;
    /* Counter 1 is J0, which is reserved for the tag; the data starts at 2. */
    uint32_t counter = 2;

    for (; len - offset >= 4 * WREATH_GCM_BLOCK_BYTES;
         offset += 4 * WREATH_GCM_BLOCK_BYTES) {
        __m128i blocks[4];
        for (int i = 0; i < 4; i++) {
            blocks[i] = wreath_gcm_counter_block(nonce, counter + (uint32_t)i);
        }
        counter += 4;
        wreath_aes128_block4(round_keys, blocks);
        for (int i = 0; i < 4; i++) {
            Py_ssize_t at = offset + i * WREATH_GCM_BLOCK_BYTES;
            __m128i data = _mm_loadu_si128((const __m128i *)(const void *)(src + at));
            _mm_storeu_si128((__m128i *)(void *)(dst + at),
                             _mm_xor_si128(data, blocks[i]));
        }
    }
    for (; len - offset >= WREATH_GCM_BLOCK_BYTES; offset += WREATH_GCM_BLOCK_BYTES) {
        __m128i stream = wreath_aes128_block(
            round_keys, wreath_gcm_counter_block(nonce, counter));
        __m128i data = _mm_loadu_si128((const __m128i *)(const void *)(src + offset));
        counter++;
        _mm_storeu_si128((__m128i *)(void *)(dst + offset), _mm_xor_si128(data, stream));
    }
    if (offset < len) {
        uint8_t tail[WREATH_GCM_BLOCK_BYTES] = {0};
        size_t rest = (size_t)(len - offset);
        __m128i stream = wreath_aes128_block(
            round_keys, wreath_gcm_counter_block(nonce, counter));
        memcpy(tail, src + offset, rest);
        _mm_storeu_si128((__m128i *)(void *)tail,
                         _mm_xor_si128(_mm_loadu_si128((const __m128i *)(const void *)tail),
                                       stream));
        memcpy(dst + offset, tail, rest);
    }
}

/* --- the whole operation ------------------------------------------------- */

/* Transforms `in_len` bytes and writes the 16-byte tag over `cipher`, which is
 * whichever of the two buffers holds ciphertext: `dst` when encrypting, `src`
 * when decrypting. Verification is the caller's, so that both directions build
 * the tag by the identical route. */
WREATH_TARGET_AESGCM static void
wreath_aesgcm_transform(const uint8_t *key, const uint8_t *nonce,
                        const uint8_t *src, uint8_t *dst, Py_ssize_t in_len,
                        const uint8_t *aad, Py_ssize_t aad_len,
                        const uint8_t *cipher, uint8_t *tag)
{
    const __m128i bswap = wreath_gcm_bswap_mask();
    __m128i round_keys[11];
    __m128i h;
    __m128i state;
    __m128i lengths;
    uint8_t length_block[WREATH_GCM_BLOCK_BYTES];
    uint64_t aad_bits = (uint64_t)aad_len * 8u;
    uint64_t cipher_bits = (uint64_t)in_len * 8u;

    wreath_aes128_expand(key, round_keys);
    h = _mm_shuffle_epi8(wreath_aes128_block(round_keys, _mm_setzero_si128()), bswap);

    wreath_gcm_ctr(round_keys, nonce, src, dst, in_len);

    state = _mm_setzero_si128();
    state = wreath_gcm_ghash(state, h, aad, aad_len);
    state = wreath_gcm_ghash(state, h, cipher, in_len);

    for (int i = 0; i < 8; i++) {
        length_block[i] = (uint8_t)(aad_bits >> (56 - 8 * i));
        length_block[8 + i] = (uint8_t)(cipher_bits >> (56 - 8 * i));
    }
    lengths = _mm_shuffle_epi8(
        _mm_loadu_si128((const __m128i *)(const void *)length_block), bswap);
    state = wreath_gcm_gfmul(_mm_xor_si128(state, lengths), h);

    _mm_storeu_si128(
        (__m128i *)(void *)tag,
        _mm_xor_si128(
            _mm_shuffle_epi8(state, bswap),
            wreath_aes128_block(round_keys, wreath_gcm_counter_block(nonce, 1))));
}

/* Branch-free equality, because a tag comparison that returns early tells an
 * attacker how many leading bytes were right, and forging a tag one byte at a
 * time is a very different problem from forging it all at once. The Python arm
 * reaches the same property through `hmac.compare_digest`. */
static int
wreath_aesgcm_tags_equal(const uint8_t *a, const uint8_t *b)
{
    uint8_t difference = 0;
    for (int i = 0; i < WREATH_GCM_TAG_BYTES; i++) {
        difference = (uint8_t)(difference | (uint8_t)(a[i] ^ b[i]));
    }
    return difference == 0;
}

static PyObject *
wreath_aesgcm_dispatch(PyObject *args, const char *name, int encrypting)
{
    const char *key = NULL;
    const char *nonce = NULL;
    const char *data = NULL;
    const char *aad = NULL;
    Py_ssize_t key_len = 0;
    Py_ssize_t nonce_len = 0;
    Py_ssize_t data_len = 0;
    Py_ssize_t aad_len = 0;
    Py_ssize_t out_len;
    PyObject *out;
    uint8_t tag[WREATH_GCM_TAG_BYTES];

    if (!wreath_simd_has_aesgcm()) {
        return wreath_aesgcm_unavailable(name);
    }
    if (!PyArg_ParseTuple(args, "y#y#y#y#", &key, &key_len, &nonce, &nonce_len,
                          &data, &data_len, &aad, &aad_len)) {
        return NULL;
    }
    if (key_len != WREATH_AES128_KEY_BYTES) {
        PyErr_SetString(PyExc_ValueError, "AES-128 takes a 16-byte key");
        return NULL;
    }
    if (nonce_len != WREATH_GCM_NONCE_BYTES) {
        PyErr_SetString(PyExc_ValueError, "this GCM profile takes a 96-bit nonce");
        return NULL;
    }
    if (!encrypting && data_len < WREATH_GCM_TAG_BYTES) {
        PyErr_SetString(PyExc_ValueError,
                        "an AES-GCM message is at least its 16-byte tag");
        return NULL;
    }
    out_len = encrypting ? data_len : data_len - WREATH_GCM_TAG_BYTES;
    /* The facade bounds the plaintext long before this; the check is here so
     * that the one piece of length arithmetic in this file cannot overflow
     * whatever a future caller passes. */
    if (out_len > PY_SSIZE_T_MAX - WREATH_GCM_TAG_BYTES) {
        PyErr_SetString(PyExc_OverflowError, "AES-GCM message length overflows");
        return NULL;
    }
    out = PyBytes_FromStringAndSize(NULL, encrypting ? out_len + WREATH_GCM_TAG_BYTES
                                                     : out_len);
    if (out == NULL) {
        return NULL;
    }
    {
        uint8_t *buffer = (uint8_t *)PyBytes_AS_STRING(out);
        const uint8_t *source = (const uint8_t *)data;
        const uint8_t *cipher = encrypting ? buffer : source;
        wreath_aesgcm_transform((const uint8_t *)key, (const uint8_t *)nonce, source,
                                buffer, out_len, (const uint8_t *)aad, aad_len,
                                cipher, tag);
        if (encrypting) {
            memcpy(buffer + out_len, tag, WREATH_GCM_TAG_BYTES);
            return out;
        }
        if (!wreath_aesgcm_tags_equal(tag, (const uint8_t *)data + out_len)) {
            /* None rather than an exception: the facade owns the message, so
             * the Python and hardware paths refuse in exactly the same words. */
            Py_DECREF(out);
            Py_RETURN_NONE;
        }
    }
    return out;
}
#else
/* No AES-NI in this build -- a non-x86 target, or a compiler without
 * per-function target selection. Nothing above was compiled, `aesgcm_arms()`
 * reports nothing, and `wreath._webpush` binds its Python arm. */
static PyObject *
wreath_aesgcm_dispatch(PyObject *args, const char *name, int encrypting)
{
    (void)args;
    (void)encrypting;
    return wreath_aesgcm_unavailable(name);
}
#endif /* WREATH_HAVE_AESGCM */

PyObject *
wreath_aesgcm_arms(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    if (!wreath_simd_has_aesgcm()) {
        return PyTuple_New(0);
    }
    return Py_BuildValue("(ss)", "aesni", "pclmul");
}

PyObject *
wreath_aes128gcm_encrypt(PyObject *Py_UNUSED(self), PyObject *args)
{
    return wreath_aesgcm_dispatch(args, "aes128gcm_encrypt", 1);
}

PyObject *
wreath_aes128gcm_decrypt(PyObject *Py_UNUSED(self), PyObject *args)
{
    return wreath_aesgcm_dispatch(args, "aes128gcm_decrypt", 0);
}
