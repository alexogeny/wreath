/* Baseline decode arm: portable C, no ISA extensions.
 * SPDX-License-Identifier: MPL-2.0 */
#define GZ_ARM_NAME scalar
#define GZ_USE_BMI2 0
#define GZ_USE_AVX2 0
#include "inflate_core.h"
