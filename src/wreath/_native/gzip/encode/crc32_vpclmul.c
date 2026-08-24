/* CRC-32 folding on 256-bit lanes (VPCLMULQDQ + AVX2) -- encoder instance.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * The body is `gzip/crc32_vpclmul_impl.h`, shared with the decoder.
 */
#include "crc32.h"

#define GZ_CRC_FOLD_FN wreath_gzip_encoder_crc32_vpclmul
#define GZ_CRC_TAIL_FN wreath_gzip_encoder_crc32_raw_slice16

#include "../crc32_vpclmul_impl.h"
