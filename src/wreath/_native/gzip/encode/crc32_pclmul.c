/* CRC-32 by carry-less multiply folding (PCLMULQDQ) -- encoder instance.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * The body is `gzip/crc32_pclmul_impl.h`, shared with the decoder. Naming the
 * two symbols here and including it is the whole file: the fold is the same
 * algebra either way, and keeping one copy means a correction to it cannot
 * land on one side only.
 */
#include "crc32.h"

#define GZ_CRC_FOLD_FN wreath_gzip_encoder_crc32_pclmul
#define GZ_CRC_TAIL_FN wreath_gzip_encoder_crc32_raw_slice16

#include "../crc32_pclmul_impl.h"
