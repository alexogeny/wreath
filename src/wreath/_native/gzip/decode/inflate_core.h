/* The hot inflate symbol loop, compiled once per ISA arm.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Two independent ideas meet here and are kept separately attributable:
 *
 *   - arm 1's control flow. The loop does no per-symbol bounds checking; it
 *     earns that by proving once, per budget window, that enough input and
 *     output remain for every iteration in the window. Inside, refills read a
 *     whole 64-bit word unconditionally, literals store unconditionally, and a
 *     match copy may overrun the match end by up to 31 bytes.
 *   - arm 3's instruction-set specialisation. The whole function is selected
 *     once, at block entry, never per symbol: a BMI2 build differs only in how
 *     field extraction is spelled, so an ablation between the two arms measures
 *     the instruction set and nothing else.
 *
 * Each arm compiles two loops -- with and without the fused multi-symbol root
 * table -- because that choice, too, has to be made once per block.
 */
#include "infl.h"

#ifndef GZ_ARM_NAME
#error "define GZ_ARM_NAME before including inflate_core.h"
#endif
#define GZ_CAT2(a, b) a##b
#define GZ_CAT(a, b) GZ_CAT2(a, b)
#define GZFN(name) GZ_CAT(name, GZ_ARM_NAME)

#if GZ_USE_BMI2 || GZ_USE_AVX2
#include <immintrin.h>
#endif

#if GZ_USE_BMI2
/* bzhi takes the width directly, so there is no mask to materialise, and the
 * variable shifts become shrx, which does not need the count in cl. */
#define GZ_LOW(v, n) _bzhi_u64((uint64_t)(v), (unsigned)(n))
#define GZ_ROOT(v, root, mask) ((size_t)_bzhi_u64((uint64_t)(v), (root)))
#else
#define GZ_LOW(v, n) ((uint64_t)((uint32_t)(v) & ((1u << (n)) - 1u)))
#define GZ_ROOT(v, root, mask) ((size_t)((v) & (mask)))
#endif

#if GZ_DIRTY_BITCOUNT
#define GZ_CNT ((uint8_t)cnt)
#define GZ_REFILL()                      \
  do {                                   \
    bb |= wreath_gzip_decoder_ld64(ip) << GZ_CNT;         \
    ip += (63u - (unsigned)GZ_CNT) >> 3; \
    cnt |= 56;                           \
  } while (0)
#define GZ_DROP(ent)       \
  do {                     \
    bb >>= (uint8_t)(ent); \
    cnt -= (ent);          \
  } while (0)
#else
#define GZ_CNT cnt
#define GZ_REFILL()           \
  do {                        \
    bb |= wreath_gzip_decoder_ld64(ip) << cnt; \
    ip += (63 - cnt) >> 3;    \
    cnt |= 56;                \
  } while (0)
#define GZ_DROP(ent)              \
  do {                            \
    unsigned n_ = GZ_E_BITS(ent); \
    bb >>= n_;                    \
    cnt -= n_;                    \
  } while (0)
#endif

/* One step of the literal chain: a table load, a sign test, a byte store out of
 * %ah and the bit drop. `tail` is what happens once the literal is emitted --
 * either another step, or `continue` for the innermost one. */
#if GZ_COMPACT_LITROOT
#define GZ_LITSTEP(tail)                                                   \
  e = rt[GZ_ROOT(bb, lroot, lmask)];                                       \
  if (__builtin_expect((e & GZ_CR_ESCAPE) == 0, 1)) {                      \
    *op++ = GZ_E_LITBYTE(e);                                               \
    GZ_DROP(e);                                                            \
    tail                                                                   \
  } else {                                                                 \
    e = lt[GZ_ROOT(bb, lroot, lmask)];                                     \
  }
#else
#define GZ_LITSTEP(tail)               \
  e = lt[GZ_ROOT(bb, lroot, lmask)];   \
  if (GZ_IS_LIT(e)) {                  \
    *op++ = GZ_E_LITBYTE(e);           \
    GZ_DROP(e);                        \
    tail                               \
  }
#endif

/* A refill leaves at least 56 bits buffered. A length code is at most 15 bits
 * (root plus subtable index) and its extra bits at most 5, so a fall-through to
 * the match path may be preceded by at most 3 root-width literals; a fourth may
 * be taken only if it loops or refills. Violating either underflows `cnt`,
 * which is unsigned. Chains past 4 refill between groups of four, which is what
 * GZ_IN_STEP's "at most two refills" has to be widened for -- see infl.h. */
#define GZ_LITGRP_MAX 4
_Static_assert((GZ_LITGRP_MAX - 1) * GZ_LIT_ROOT + 20 <= 56,
               "literal chain can fall through with fewer bits than a match needs");
_Static_assert(GZ_LITGRP_MAX * GZ_LIT_ROOT <= 56,
               "literal chain can consume more bits than one refill guarantees");
/* And after the length code the distance code needs 15 + 13 = 28. */
_Static_assert(GZ_DIST_REFILL >= 28, "distance refill threshold is below 15 + 13");

#define GZ_LITSTEP1(t) GZ_LITSTEP(t)
#define GZ_LITSTEP2(t) GZ_LITSTEP(GZ_LITSTEP(t))
#define GZ_LITSTEP3(t) GZ_LITSTEP(GZ_LITSTEP(GZ_LITSTEP(t)))
#define GZ_LITSTEP4(t) GZ_LITSTEP(GZ_LITSTEP(GZ_LITSTEP(GZ_LITSTEP(t))))

#if GZ_LIT_CHAIN == 1
#define GZ_LITCHAIN GZ_LITSTEP1(continue;)
#elif GZ_LIT_CHAIN == 2
#define GZ_LITCHAIN GZ_LITSTEP2(continue;)
#elif GZ_LIT_CHAIN == 3
#define GZ_LITCHAIN GZ_LITSTEP3(continue;)
#elif GZ_LIT_CHAIN == 4
#define GZ_LITCHAIN GZ_LITSTEP4(continue;)
#elif GZ_LIT_CHAIN == 6
#define GZ_LITCHAIN GZ_LITSTEP4(GZ_REFILL(); GZ_LITSTEP2(continue;))
#elif GZ_LIT_CHAIN == 8
#define GZ_LITCHAIN GZ_LITSTEP4(GZ_REFILL(); GZ_LITSTEP4(continue;))
#elif GZ_LIT_CHAIN == 12
#define GZ_LITCHAIN \
  GZ_LITSTEP4(GZ_REFILL(); GZ_LITSTEP4(GZ_REFILL(); GZ_LITSTEP4(continue;)))
#else
#error "GZ_LIT_CHAIN must be 1..4, 6, 8 or 12; see the bit budget above"
#endif

/* One fused window: up to four literals as a single 32-bit store. The store is
 * always four bytes wide whatever the run length -- the extra bytes land in
 * proved slack and are overwritten by the next window. */
#define GZ_EMIT(f)                          \
  do {                                      \
    wreath_gzip_decoder_st32(op, (uint32_t)((f) >> 32));      \
    op += (uint32_t)(((f) >> 8) & 0xF);      \
    bb >>= (uint8_t)(f);                     \
    cnt -= GZ_DIRTY_BITCOUNT ? (uint32_t)(f) : (uint8_t)(f); \
  } while (0)

/* Width of the match-copy head: the widest store the arm can issue. 0 disables
 * it and restores the two-8-byte-store head that was here before, which is the
 * ablation. An override is capped at what the arm has, so one -D sweeps all
 * three arms at once. */
#if GZ_USE_AVX2
#define GZ_COPY_HEAD_MAX 32
#else
#define GZ_COPY_HEAD_MAX 16
#endif
#ifndef GZ_COPY_HEAD
#define GZ_COPY_HEAD GZ_COPY_HEAD_MAX
#endif
#if GZ_COPY_HEAD > GZ_COPY_HEAD_MAX
#undef GZ_COPY_HEAD
#define GZ_COPY_HEAD GZ_COPY_HEAD_MAX
#endif

/* Matches at or below this length take a narrower store than GZ_COPY_HEAD. 0
 * disables the split. Must not exceed GZ_COPY_HEAD, which is what makes the
 * narrower store legal at the same distance guard. */
#ifndef GZ_COPY_SHORT
#if GZ_COPY_HEAD == 32
#define GZ_COPY_SHORT 16
#else
#define GZ_COPY_SHORT 0
#endif
#endif
#if GZ_COPY_SHORT > GZ_COPY_HEAD
#error "GZ_COPY_SHORT must not exceed GZ_COPY_HEAD"
#endif

/* 32 is an AVX2 intrinsic; 16 is written as a fixed-size memcpy so the portable
 * arm gets whatever the host has rather than an x86 instruction. */
#if GZ_COPY_HEAD == 32
#define GZ_WIDE(d, s)                                       \
  _mm256_storeu_si256((__m256i *)(void *)(d),               \
                      _mm256_loadu_si256((const __m256i *)(const void *)(s)))
#elif GZ_COPY_HEAD
#define GZ_WIDE(d, s) memcpy((d), (s), GZ_COPY_HEAD)
#endif

/* Only ever called with GZ_OUT_SLACK bytes proved free, so the stores are
 * allowed to run past op + len. */
static inline void GZFN(wreath_gzip_decoder_copy_)(uint8_t *op, uint32_t dist, uint32_t len) {
  uint8_t *end = op + len;
  const uint8_t *src = op - dist;

#if GZ_COPY_HEAD
  /* One unconditional GZ_COPY_HEAD-byte store, which finishes most matches on
   * its own: arm 3 instrumented fewer than 0.5% of matches at distance under
   * 32, and the mean match is far shorter than 32 bytes. The narrower head that
   * was here costs two loads, two stores and a length test on every one of
   * those. `dist >= GZ_COPY_HEAD` is what makes the wide store correct -- the
   * source cannot overlap the bytes being written -- and the store may run
   * GZ_COPY_HEAD-1 bytes past the match end, which is the same overshoot the
   * tail loop already has and which GZ_OUT_SLACK already covers. */
  if (__builtin_expect(dist >= GZ_COPY_HEAD, 1)) {
#if GZ_COPY_SHORT
    /* A match no longer than this is finished by a narrower store. The wide
     * store would also finish it in one instruction, but it reads and writes
     * cache lines the match never needed, which is the whole of the difference
     * between the two on the miss-per-kB axis. */
    if (__builtin_expect(len <= GZ_COPY_SHORT, 1)) {
      memcpy(op, src, GZ_COPY_SHORT);
      return;
    }
#endif
    GZ_WIDE(op, src);
    if (__builtin_expect(len > GZ_COPY_HEAD, 0)) {
      op += GZ_COPY_HEAD;
      src += GZ_COPY_HEAD;
      do {
        GZ_WIDE(op, src);
        op += GZ_COPY_HEAD;
        src += GZ_COPY_HEAD;
      } while (op < end);
    }
    return;
  }
#endif

  /* Distances too short for the wide head: two 8-byte stores, which are correct
   * from distance 8 up because the second reads what the first wrote. Arm 1
   * measured one store and four stores both worse than two. */
  if (__builtin_expect(dist >= 8, 1)) {
    wreath_gzip_decoder_st64(op, wreath_gzip_decoder_ld64(src));
    wreath_gzip_decoder_st64(op + 8, wreath_gzip_decoder_ld64(src + 8));
    if (len > 16) {
      op += 16;
      src += 16;
#if GZ_USE_AVX2 && !GZ_COPY_HEAD
      if (dist >= 32) {
        do {
          _mm256_storeu_si256((__m256i *)(void *)op,
                              _mm256_loadu_si256((const __m256i *)(const void *)src));
          op += 32;
          src += 32;
        } while (op < end);
        return;
      }
#endif
      do {
        wreath_gzip_decoder_st64(op, wreath_gzip_decoder_ld64(src));
        op += 8;
        src += 8;
      } while (op < end);
    }
    return;
  }

  /* Short distance: build the period-`dist` pattern once in a register and
   * store it whole, advancing by the largest multiple of `dist` that fits in
   * eight bytes. Fewer than 0.5% of matches reach here on any corpus body, so
   * this path is written for clarity rather than for speed. */
  {
    uint64_t pat;
    uint32_t step;
    if (dist == 1) {
      pat = (uint64_t)src[0] * 0x0101010101010101ull;
      step = 8;
    } else {
      uint64_t unit = 0;
      for (uint32_t i = 0; i < dist; i++) unit |= (uint64_t)src[i] << (8 * i);
      pat = 0;
      for (uint32_t f = 0; f < 64; f += 8 * dist) pat |= unit << f;
      step = (8 / dist) * dist;
    }
    do {
      wreath_gzip_decoder_st64(op, pat);
      op += step;
    } while (op < end);
  }
}

#define GZ_FUSED 0
#define GZ_LOOPNAME wreath_gzip_decoder_inflate_fast_
#include "inflate_loop.h"
#undef GZ_LOOPNAME
#undef GZ_FUSED

#define GZ_FUSED 1
#define GZ_LOOPNAME wreath_gzip_decoder_inflate_fused_
#include "inflate_loop.h"
#undef GZ_LOOPNAME
#undef GZ_FUSED

#undef GZ_REFILL
#undef GZ_DROP
#undef GZ_EMIT
#undef GZ_CNT
#undef GZ_LOW
#undef GZ_ROOT
