/* CRC-32 folding on 256-bit lanes (VPCLMULQDQ + AVX2) -- the kernel body.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Included once by `decode/crc32_vpclmul.c` and once by
 * `encode/crc32_vpclmul.c`; see `crc32_pclmul_impl.h` for why the body is
 * shared rather than transcribed.
 *
 * Own translation unit, -mavx2 -mvpclmulqdq, reached only through the runtime
 * probe. VPCLMULQDQ applies the carry-less multiply independently to each
 * 128-bit lane, so a ymm is two folds with one instruction: the same constants
 * work unchanged, and the two lanes are combined at the end by folding lane 0
 * forward the 16 bytes that separate them.
 *
 * The includer defines these and includes its own `crc32.h` first; see
 * `crc32_pclmul_impl.h` for why that include cannot live here:
 *   GZ_CRC_FOLD_FN  the exported `uint32_t (uint32_t, const void *, size_t)`
 *   GZ_CRC_TAIL_FN  that side's `crc32_raw_slice16`
 */
#if !defined(GZ_CRC_FOLD_FN) || !defined(GZ_CRC_TAIL_FN)
#error "crc32_vpclmul_impl.h needs GZ_CRC_FOLD_FN and GZ_CRC_TAIL_FN"
#endif
#ifndef GZ_CRC32_H
#error "crc32_vpclmul_impl.h needs its side's crc32.h included first"
#endif

#include <immintrin.h>
#include <string.h>

#define K2(lo, hi) _mm_set_epi64x((long long)(hi), (long long)(lo))

static inline __m256i fold256(__m256i x, __m256i k) {
  return _mm256_xor_si256(_mm256_clmulepi64_epi128(x, k, 0x00),
                          _mm256_clmulepi64_epi128(x, k, 0x11));
}

static inline __m128i fold128(__m128i x, __m128i k) {
  return _mm_xor_si128(_mm_clmulepi64_si128(x, k, 0x00),
                       _mm_clmulepi64_si128(x, k, 0x11));
}

static inline __m256i ld256(const uint8_t *p) {
  return _mm256_loadu_si256((const __m256i *)(const void *)p);
}

uint32_t GZ_CRC_FOLD_FN(uint32_t crc, const void *buf, size_t len) {
  const uint8_t *p = (const uint8_t *)buf;
  uint32_t c = ~crc;

  if (len >= 128) {
    const __m256i k128 =
        _mm256_broadcastsi128_si256(K2(GZ_CRC_K_128_LO, GZ_CRC_K_128_HI));
    __m256i y0 = _mm256_xor_si256(
        ld256(p), _mm256_castsi128_si256(_mm_cvtsi32_si128((int)c)));
    __m256i y1 = ld256(p + 32), y2 = ld256(p + 64), y3 = ld256(p + 96);
    p += 128;
    len -= 128;
    while (len >= 128) {
      y0 = _mm256_xor_si256(fold256(y0, k128), ld256(p));
      y1 = _mm256_xor_si256(fold256(y1, k128), ld256(p + 32));
      y2 = _mm256_xor_si256(fold256(y2, k128), ld256(p + 64));
      y3 = _mm256_xor_si256(fold256(y3, k128), ld256(p + 96));
      p += 128;
      len -= 128;
    }
    /* Stream i sits 32*(3-i) bytes ahead of stream 3. */
    __m256i acc = y3;
    acc = _mm256_xor_si256(
        acc, fold256(y0, _mm256_broadcastsi128_si256(
                             K2(GZ_CRC_K_96_LO, GZ_CRC_K_96_HI))));
    acc = _mm256_xor_si256(
        acc, fold256(y1, _mm256_broadcastsi128_si256(
                             K2(GZ_CRC_K_64_LO, GZ_CRC_K_64_HI))));
    acc = _mm256_xor_si256(
        acc, fold256(y2, _mm256_broadcastsi128_si256(
                             K2(GZ_CRC_K_32_LO, GZ_CRC_K_32_HI))));

    const __m128i k16 = K2(GZ_CRC_K_16_LO, GZ_CRC_K_16_HI);
    /* Lane 0 holds the 16 bytes preceding lane 1. */
    __m128i x = _mm_xor_si128(fold128(_mm256_castsi256_si128(acc), k16),
                              _mm256_extracti128_si256(acc, 1));
    while (len >= 16) {
      x = _mm_xor_si128(fold128(x, k16),
                        _mm_loadu_si128((const __m128i *)(const void *)p));
      p += 16;
      len -= 16;
    }
    uint8_t tmp[16];
    _mm_storeu_si128((__m128i *)(void *)tmp, x);
    c = GZ_CRC_TAIL_FN(0, tmp, 16);
    _mm256_zeroupper();
  }
  c = GZ_CRC_TAIL_FN(c, p, len);
  return ~c;
}
