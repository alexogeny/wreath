/* AES-128-GCM for RFC 8291 web push.
 *
 * The scalar implementation is the architecture-independent definition. On
 * x86, per-call feature detection selects the AES-NI/PCLMULQDQ implementation:
 *
 *   AES-NI       `aesenc`/`aesenclast`/`aeskeygenassist` do a whole AES round
 *                per instruction.
 *   PCLMULQDQ    `pclmulqdq` is the carry-less multiply GHASH is defined in
 *                terms of.
 *
 * AVX2 is deliberately *not* used. Emulating AES in 256-bit lane arithmetic is
 * strictly worse than the instruction that exists, and the only thing left to
 * widen -- the XOR of the keystream against the plaintext -- is already folded
 * into the same registers the cipher produces, so a wider XOR would need an
 * extra pass over the buffer to do less work per byte than it costs to make.
 *
 * ## What is constant time here, and what is not
 *
 * The AES-NI/PCLMULQDQ implementation is constant-time. Those are fixed-latency
 * instructions with no data-dependent memory access, and the tag comparison is
 * a branch-free XOR-accumulate.
 *
 * **The scalar arm is not constant time.** Its S-box is indexed by secret
 * state, so it leaks through the cache. It exists for CPUs without AES
 * instructions. Do not read "the hardware path is constant
 * time" as "AES-GCM in Wreath is constant time" -- which arm runs still
 * depends on the CPU.
 *
 * ## Structure
 *
 * Both instruction paths are pinned independently to NIST SP 800-38D vectors
 * and OpenSSL outputs in `tests/test_aesgcm_parity.py`. Feature detection lives
 * in `simd.h`; `aesgcm_arms()` reports the selected path.
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

/* --- scalar AES-128 ---------------------------------------------------- */

static const uint8_t wreath_aes_sbox[256] = {
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
};

static const uint8_t wreath_aes_rcon[10] = {
    0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36,
};

static uint8_t
wreath_aes_xtime(uint8_t value)
{
    return (uint8_t)((value << 1) ^ (0x1b & (uint8_t)-(int)(value >> 7)));
}

static void
wreath_aes128_expand_scalar(const uint8_t key[16], uint8_t round_keys[176])
{
    memcpy(round_keys, key, 16);
    for (int word = 4; word < 44; word++) {
        uint8_t temp[4];
        memcpy(temp, round_keys + (word - 1) * 4, 4);
        if ((word & 3) == 0) {
            uint8_t first = temp[0];
            temp[0] = (uint8_t)(wreath_aes_sbox[temp[1]] ^ wreath_aes_rcon[word / 4 - 1]);
            temp[1] = wreath_aes_sbox[temp[2]];
            temp[2] = wreath_aes_sbox[temp[3]];
            temp[3] = wreath_aes_sbox[first];
        }
        for (int byte = 0; byte < 4; byte++) {
            round_keys[word * 4 + byte] =
                (uint8_t)(round_keys[(word - 4) * 4 + byte] ^ temp[byte]);
        }
    }
}

static void
wreath_aes128_block_scalar(const uint8_t round_keys[176],
                             const uint8_t input[16], uint8_t output[16])
{
    uint8_t state[16];
    for (int i = 0; i < 16; i++) state[i] = (uint8_t)(input[i] ^ round_keys[i]);
    for (int round = 1; round <= 10; round++) {
        uint8_t shifted[16];
        for (int i = 0; i < 16; i++) {
            shifted[i] = wreath_aes_sbox[state[(i + (i & 3) * 4) & 15]];
        }
        if (round != 10) {
            for (int column = 0; column < 4; column++) {
                uint8_t *a = shifted + column * 4;
                uint8_t total = (uint8_t)(a[0] ^ a[1] ^ a[2] ^ a[3]);
                uint8_t first = a[0];
                a[0] ^= (uint8_t)(total ^ wreath_aes_xtime((uint8_t)(a[0] ^ a[1])));
                a[1] ^= (uint8_t)(total ^ wreath_aes_xtime((uint8_t)(a[1] ^ a[2])));
                a[2] ^= (uint8_t)(total ^ wreath_aes_xtime((uint8_t)(a[2] ^ a[3])));
                a[3] ^= (uint8_t)(total ^ wreath_aes_xtime((uint8_t)(a[3] ^ first)));
            }
        }
        for (int i = 0; i < 16; i++) {
            state[i] = (uint8_t)(shifted[i] ^ round_keys[round * 16 + i]);
        }
    }
    memcpy(output, state, 16);
}

/* GCM's right-shift definition, expressed over bytes so the result is
 * independent of host endianness. The masks keep the multiply's control flow
 * independent of the input bits; the AES S-box remains the scalar arm's
 * stated cache-timing limitation. */
static void
wreath_gcm_mul_scalar(const uint8_t left[16], const uint8_t right[16],
                        uint8_t output[16])
{
    uint8_t z[16] = {0};
    uint8_t v[16];
    memcpy(v, right, 16);
    for (int bit = 0; bit < 128; bit++) {
        uint8_t selected = (uint8_t)-(int)((left[bit >> 3] >> (7 - (bit & 7))) & 1);
        for (int i = 0; i < 16; i++) z[i] ^= (uint8_t)(v[i] & selected);
        uint8_t reduction = (uint8_t)-(int)(v[15] & 1);
        uint8_t carry = 0;
        for (int i = 0; i < 16; i++) {
            uint8_t next = (uint8_t)(v[i] & 1);
            v[i] = (uint8_t)((v[i] >> 1) | (carry << 7));
            carry = next;
        }
        v[0] ^= (uint8_t)(0xe1 & reduction);
    }
    memcpy(output, z, 16);
}

static void
wreath_gcm_ghash_scalar(uint8_t state[16], const uint8_t h[16],
                          const uint8_t *data, Py_ssize_t length)
{
    for (Py_ssize_t offset = 0; offset < length; offset += 16) {
        uint8_t block[16] = {0};
        uint8_t product[16];
        Py_ssize_t width = length - offset < 16 ? length - offset : 16;
        memcpy(block, data + offset, (size_t)width);
        for (int i = 0; i < 16; i++) block[i] ^= state[i];
        wreath_gcm_mul_scalar(block, h, product);
        memcpy(state, product, 16);
    }
}

static void
wreath_gcm_counter_scalar(const uint8_t nonce[12], uint32_t counter,
                            uint8_t block[16])
{
    memcpy(block, nonce, 12);
    block[12] = (uint8_t)(counter >> 24);
    block[13] = (uint8_t)(counter >> 16);
    block[14] = (uint8_t)(counter >> 8);
    block[15] = (uint8_t)counter;
}

static void
wreath_aesgcm_transform_scalar(const uint8_t *key, const uint8_t *nonce,
                                 const uint8_t *src, uint8_t *dst, Py_ssize_t length,
                                 const uint8_t *aad, Py_ssize_t aad_length,
                                 const uint8_t *cipher, uint8_t tag[16])
{
    uint8_t round_keys[176];
    uint8_t h[16];
    uint8_t zero[16] = {0};
    uint8_t state[16] = {0};
    uint32_t counter = 2;
    wreath_aes128_expand_scalar(key, round_keys);
    wreath_aes128_block_scalar(round_keys, zero, h);
    for (Py_ssize_t offset = 0; offset < length; offset += 16, counter++) {
        uint8_t counter_block[16];
        uint8_t stream[16];
        Py_ssize_t width = length - offset < 16 ? length - offset : 16;
        wreath_gcm_counter_scalar(nonce, counter, counter_block);
        wreath_aes128_block_scalar(round_keys, counter_block, stream);
        for (Py_ssize_t i = 0; i < width; i++) dst[offset + i] = src[offset + i] ^ stream[i];
    }
    wreath_gcm_ghash_scalar(state, h, aad, aad_length);
    wreath_gcm_ghash_scalar(state, h, cipher, length);
    {
        uint8_t lengths[16];
        uint8_t product[16];
        uint64_t aad_bits = (uint64_t)aad_length * 8u;
        uint64_t cipher_bits = (uint64_t)length * 8u;
        for (int i = 0; i < 8; i++) {
            lengths[i] = (uint8_t)(aad_bits >> (56 - 8 * i));
            lengths[8 + i] = (uint8_t)(cipher_bits >> (56 - 8 * i));
            lengths[i] ^= state[i];
            lengths[8 + i] ^= state[8 + i];
        }
        wreath_gcm_mul_scalar(lengths, h, product);
        memcpy(state, product, 16);
    }
    {
        uint8_t j0[16];
        uint8_t mask[16];
        wreath_gcm_counter_scalar(nonce, 1, j0);
        wreath_aes128_block_scalar(round_keys, j0, mask);
        for (int i = 0; i < 16; i++) tag[i] = state[i] ^ mask[i];
    }
}

static int
wreath_aesgcm_tags_equal_scalar(const uint8_t *left, const uint8_t *right)
{
    uint8_t difference = 0;
    for (int i = 0; i < WREATH_GCM_TAG_BYTES; i++) {
        difference |= (uint8_t)(left[i] ^ right[i]);
    }
    return difference == 0;
}

static PyObject *
wreath_aesgcm_dispatch_scalar(PyObject *args, int encrypting)
{
    const char *key, *nonce, *data, *aad;
    Py_ssize_t key_len, nonce_len, data_len, aad_len;
    Py_ssize_t out_len;
    PyObject *out;
    uint8_t tag[WREATH_GCM_TAG_BYTES];
    if (!PyArg_ParseTuple(args, "y#y#y#y#", &key, &key_len, &nonce, &nonce_len,
                          &data, &data_len, &aad, &aad_len)) return NULL;
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
    if (out_len > PY_SSIZE_T_MAX - WREATH_GCM_TAG_BYTES) {
        PyErr_SetString(PyExc_OverflowError, "AES-GCM message length overflows");
        return NULL;
    }
    out = PyBytes_FromStringAndSize(
        NULL, encrypting ? out_len + WREATH_GCM_TAG_BYTES : out_len);
    if (out == NULL) return NULL;
    {
        uint8_t *buffer = (uint8_t *)PyBytes_AS_STRING(out);
        const uint8_t *source = (const uint8_t *)data;
        const uint8_t *cipher = encrypting ? buffer : source;
        wreath_aesgcm_transform_scalar(
            (const uint8_t *)key, (const uint8_t *)nonce, source, buffer, out_len,
            (const uint8_t *)aad, aad_len, cipher, tag);
        if (encrypting) {
            memcpy(buffer + out_len, tag, WREATH_GCM_TAG_BYTES);
            return out;
        }
        if (!wreath_aesgcm_tags_equal_scalar(
                tag, (const uint8_t *)data + out_len)) {
            Py_DECREF(out);
            Py_RETURN_NONE;
        }
    }
    return out;
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
 * attacker how many leading bytes were right. */
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

    (void)name;
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
            /* None lets the facade own the public refusal vocabulary. */
            Py_DECREF(out);
            Py_RETURN_NONE;
        }
    }
    return out;
}
#endif /* WREATH_HAVE_AESGCM */

PyObject *
wreath_aesgcm_arms(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    if (wreath_simd_has_aesgcm()) return Py_BuildValue("(ss)", "aesni", "pclmul");
    return Py_BuildValue("(s)", "scalar");
}

PyObject *
wreath_aes128gcm_encrypt(PyObject *Py_UNUSED(self), PyObject *args)
{
#if defined(WREATH_HAVE_AESGCM)
    if (wreath_simd_has_aesgcm())
        return wreath_aesgcm_dispatch(args, "aes128gcm_encrypt", 1);
#endif
    return wreath_aesgcm_dispatch_scalar(args, 1);
}

PyObject *
wreath_aes128gcm_encrypt_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    return wreath_aesgcm_dispatch_scalar(args, 1);
}

PyObject *
wreath_aes128gcm_decrypt_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    return wreath_aesgcm_dispatch_scalar(args, 0);
}

PyObject *
wreath_aes128gcm_decrypt(PyObject *Py_UNUSED(self), PyObject *args)
{
#if defined(WREATH_HAVE_AESGCM)
    if (wreath_simd_has_aesgcm())
        return wreath_aesgcm_dispatch(args, "aes128gcm_decrypt", 0);
#endif
    return wreath_aesgcm_dispatch_scalar(args, 0);
}
