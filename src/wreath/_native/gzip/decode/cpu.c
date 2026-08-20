/* Runtime CPU feature probe. Every accelerated arm in this codec is gated on
 * this and has a portable fallback; nothing here is decided at compile time.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Taken from arm 3, which is where it was written and falsified.
 */
#include "cpu.h"

#if (defined(__x86_64__) || defined(__i386__)) && !defined(WREATH_GZIP_PORTABLE)
#include <cpuid.h>

static unsigned probe(void) {
  unsigned f = 0, a, b, c, d;
  unsigned maxleaf = __get_cpuid_max(0, 0);
  if (!maxleaf) return 0;
  if (__get_cpuid(1, &a, &b, &c, &d)) {
    if (c & (1u << 1)) f |= GZ_CPU_PCLMUL;
    if (c & (1u << 19)) f |= GZ_CPU_SSE41;
    /* AVX state must be enabled by the OS (XSAVE + XGETBV bits 1,2) before any
     * ymm instruction is legal. Checking CPUID alone is the classic way to
     * SIGILL inside a VM that masks AVX off. */
    if ((c & (1u << 27)) && (c & (1u << 28))) {
      unsigned lo, hi;
      __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
      (void)hi;
      if ((lo & 0x6) == 0x6) f |= GZ_CPU_YMM;
    }
  }
  if (maxleaf >= 7) {
    __cpuid_count(7, 0, a, b, c, d);
    if (b & (1u << 3)) f |= GZ_CPU_BMI1;
    if (b & (1u << 5)) f |= GZ_CPU_AVX2_RAW;
    if (b & (1u << 8)) f |= GZ_CPU_BMI2;
    if (c & (1u << 10)) f |= GZ_CPU_VPCLMUL_RAW;
  }
  if ((f & GZ_CPU_AVX2_RAW) && (f & GZ_CPU_YMM)) f |= GZ_CPU_AVX2;
  if ((f & GZ_CPU_VPCLMUL_RAW) && (f & GZ_CPU_AVX2)) f |= GZ_CPU_VPCLMUL;
  return f;
}
#else
static unsigned probe(void) { return 0; }
#endif

unsigned wreath_gzip_decoder_cpu_features(void) {
  return probe();
}
