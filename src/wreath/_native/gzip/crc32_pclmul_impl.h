/* CRC-32 by carry-less multiply folding (PCLMULQDQ) -- the kernel body.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Included once by `decode/crc32_pclmul.c` and once by `encode/crc32_pclmul.c`,
 * each of which defines the two names below before including it. The encoder
 * and decoder keep separate symbol namespaces and separate table sets, but the
 * fold itself is the same algebra over the same constants, and it used to exist
 * as two files that differed only in the prefix on those names. A correction to
 * the fold -- a constant, a tail case, the finish step -- had to be made twice
 * or it was only half made.
 *
 * Compiled as its own translation unit with -mpclmul -msse4.1; reached only
 * through the runtime probe in cpu.c. The algebra, and the proof that these
 * constants reproduce zlib's crc32 bit-for-bit at every length, live in
 * tools/derive_crc_constants.py.
 *
 * Four independent 16-byte streams, so the loop is bound by clmul throughput
 * rather than by its ~7-cycle latency: one dependent fold per stream per
 * iteration, four iterations of the chain in flight.
 *
 * The includer defines these and includes its own `crc32.h` first -- that
 * header is per-side (the two carry different fold-constant sets and different
 * arm enums), and a quoted include from here would resolve against `gzip/`,
 * which has no `crc32.h` at all:
 *   GZ_CRC_FOLD_FN  the exported `uint32_t (uint32_t, const void *, size_t)`
 *   GZ_CRC_TAIL_FN  that side's `crc32_raw_slice16`, used for the sub-16-byte
 *                   tail and for the finish step
 */
#if !defined(GZ_CRC_FOLD_FN) || !defined(GZ_CRC_TAIL_FN)
#error "crc32_pclmul_impl.h needs GZ_CRC_FOLD_FN and GZ_CRC_TAIL_FN"
#endif
#ifndef GZ_CRC32_H
#error "crc32_pclmul_impl.h needs its side's crc32.h included first"
#endif

#include <immintrin.h>
#include <string.h>

#define K2(lo, hi) _mm_set_epi64x((long long)(hi), (long long)(lo))

static inline __m128i fold1(__m128i x, __m128i k) {
  return _mm_xor_si128(_mm_clmulepi64_si128(x, k, 0x00),
                       _mm_clmulepi64_si128(x, k, 0x11));
}

static inline __m128i ld(const uint8_t *p) {
  return _mm_loadu_si128((const __m128i *)(const void *)p);
}

uint32_t GZ_CRC_FOLD_FN(uint32_t crc, const void *buf, size_t len) {
  const uint8_t *p = (const uint8_t *)buf;
  uint32_t c = ~crc;

  if (len >= 64) {
    const __m128i k64 = K2(GZ_CRC_K_64_LO, GZ_CRC_K_64_HI);
    __m128i x0 = _mm_xor_si128(ld(p), _mm_cvtsi32_si128((int)c));
    __m128i x1 = ld(p + 16), x2 = ld(p + 32), x3 = ld(p + 48);
    p += 64;
    len -= 64;
    while (len >= 64) {
      x0 = _mm_xor_si128(fold1(x0, k64), ld(p));
      x1 = _mm_xor_si128(fold1(x1, k64), ld(p + 16));
      x2 = _mm_xor_si128(fold1(x2, k64), ld(p + 32));
      x3 = _mm_xor_si128(fold1(x3, k64), ld(p + 48));
      p += 64;
      len -= 64;
    }
    /* Stream i sits 16*(3-i) bytes ahead of stream 3. */
    __m128i acc = x3;
    acc = _mm_xor_si128(acc, fold1(x0, K2(GZ_CRC_K_48_LO, GZ_CRC_K_48_HI)));
    acc = _mm_xor_si128(acc, fold1(x1, K2(GZ_CRC_K_32_LO, GZ_CRC_K_32_HI)));
    acc = _mm_xor_si128(acc, fold1(x2, K2(GZ_CRC_K_16_LO, GZ_CRC_K_16_HI)));

    const __m128i k16 = K2(GZ_CRC_K_16_LO, GZ_CRC_K_16_HI);
    while (len >= 16) {
      acc = _mm_xor_si128(fold1(acc, k16), ld(p));
      p += 16;
      len -= 16;
    }
    /* Finish: by the register invariant, running the table CRC over the 16
     * accumulator bytes from state 0 *is* the state. No Barrett step, and one
     * fewer place to be subtly wrong; it runs once per call. */
    uint8_t tmp[16];
    _mm_storeu_si128((__m128i *)(void *)tmp, acc);
    c = GZ_CRC_TAIL_FN(0, tmp, 16);
  }
  c = GZ_CRC_TAIL_FN(c, p, len);
  return ~c;
}
