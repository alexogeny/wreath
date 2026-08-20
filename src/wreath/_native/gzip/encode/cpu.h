/* SPDX-License-Identifier: MPL-2.0 */
#ifndef GZ_CPU_H
#define GZ_CPU_H

enum {
  GZ_CPU_PCLMUL = 1u << 0,
  GZ_CPU_SSE41 = 1u << 1,
  GZ_CPU_AESNI = 1u << 2,
  GZ_CPU_YMM = 1u << 3,        /* OS has enabled ymm state */
  GZ_CPU_AVX2_RAW = 1u << 4,   /* CPUID says AVX2 */
  GZ_CPU_BMI1 = 1u << 5,
  GZ_CPU_BMI2 = 1u << 6,
  GZ_CPU_VPCLMUL_RAW = 1u << 7,
  GZ_CPU_AVX2 = 1u << 8,       /* AVX2 and usable */
  GZ_CPU_VPCLMUL = 1u << 9,    /* VPCLMULQDQ and usable */
};

unsigned wreath_gzip_encoder_cpu_features(void);

#endif
