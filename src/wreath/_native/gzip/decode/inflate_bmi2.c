/* BMI2 decode arm: bzhi for field extraction, shrx for the variable shifts.
 * Built -mbmi -mbmi2 as its own translation unit; reached only through the
 * runtime probe, and selected once at block entry.
 * SPDX-License-Identifier: MPL-2.0 */
#define GZ_ARM_NAME bmi2
#define GZ_USE_BMI2 1
#define GZ_USE_AVX2 0
#include "inflate_core.h"
