/* Format-aware BMI2 + AVX2 arm for long-match markup/query streams. A full
 * 32-byte first copy avoids the short-length branch; selected once per block. */
#define GZ_ARM_NAME bmi2_avx2_long
#define GZ_USE_BMI2 1
#define GZ_USE_AVX2 1
#define GZ_COPY_SHORT 0
#include "inflate_core.h"
