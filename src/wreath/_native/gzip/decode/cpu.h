/* SPDX-License-Identifier: MPL-2.0 */
#ifndef GZ_CPU_H
#define GZ_CPU_H

/* Raw CPUID bits are kept separate from the usable feature bits: a feature is
 * only usable when the OS has enabled the register state it needs. */
#define GZ_CPU_PCLMUL (1u << 0)
#define GZ_CPU_SSE41 (1u << 1)
#define GZ_CPU_BMI1 (1u << 2)
#define GZ_CPU_BMI2 (1u << 3)
#define GZ_CPU_YMM (1u << 4) /* XGETBV says ymm state is live */
#define GZ_CPU_AVX2_RAW (1u << 5)
#define GZ_CPU_VPCLMUL_RAW (1u << 6)
#define GZ_CPU_AVX2 (1u << 7)
#define GZ_CPU_VPCLMUL (1u << 8)

unsigned wreath_gzip_decoder_cpu_features(void);

#endif
