/* BMI2 + AVX2 decode arm: adds 32-byte match copies for distances >= 32.
 * SPDX-License-Identifier: MPL-2.0 */
#define GZ_ARM_NAME bmi2_avx2
#define GZ_USE_BMI2 1
#define GZ_USE_AVX2 1
#include "inflate_core.h"
