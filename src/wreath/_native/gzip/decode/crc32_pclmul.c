/* CRC-32 by carry-less multiply folding (PCLMULQDQ) -- decoder instance.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Harvested from arm 3, and now shared with the encoder rather than kept as a
 * second transcription of it: the body is `gzip/crc32_pclmul_impl.h`.
 */
#include "crc32.h"

#define GZ_CRC_FOLD_FN wreath_gzip_decoder_crc32_pclmul
#define GZ_CRC_TAIL_FN wreath_gzip_decoder_crc32_raw_slice16

#include "../crc32_pclmul_impl.h"
