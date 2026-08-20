/* SPDX-License-Identifier: MPL-2.0 */
#ifndef GZ_CRC32_H
#define GZ_CRC32_H

#include <stddef.h>
#include <stdint.h>

#include "gz.h"

/* State-machine form: no init/final inversion, so it composes. */
uint32_t wreath_gzip_decoder_crc32_raw_bytewise(uint32_t c, const uint8_t *p, size_t n);
uint32_t wreath_gzip_decoder_crc32_raw_slice8(uint32_t c, const uint8_t *p, size_t n);
uint32_t wreath_gzip_decoder_crc32_raw_slice16(uint32_t c, const uint8_t *p, size_t n);
int wreath_gzip_decoder_crc32_pick_arm(void);
int wreath_gzip_decoder_crc32_pick_arm_from_features(unsigned features);
uint32_t wreath_gzip_decoder_crc32_arm(int arm, uint32_t crc, const void *buf, size_t len);

/* Fold constants K(e) = rev64(x^e mod P) for the reflected CRC-32 polynomial:
 * for a fold across D bytes the low half of the register is multiplied by
 * K(8D+63) and the high half by K(8D-1). Harvested from arm 3, where they were
 * derived rather than transplanted; re-verified against zlib in crctest at
 * every length 0..1024 and every misalignment 0..63 in *this* build. */
#define GZ_CRC_K_16_LO 0x65673b4600000000ull
#define GZ_CRC_K_16_HI 0x9ba54c6f00000000ull
#define GZ_CRC_K_32_LO 0x9570d49500000000ull
#define GZ_CRC_K_32_HI 0x01b5fd1d00000000ull
#define GZ_CRC_K_48_LO 0x69ccfc0d00000000ull
#define GZ_CRC_K_48_HI 0x2a28386200000000ull
#define GZ_CRC_K_64_LO 0x653d982200000000ull
#define GZ_CRC_K_64_HI 0xcad38e8f00000000ull
#define GZ_CRC_K_96_LO 0x759fc69d00000000ull
#define GZ_CRC_K_96_HI 0x101a233100000000ull
#define GZ_CRC_K_128_LO 0x7d657a1000000000ull
#define GZ_CRC_K_128_HI 0x7406fa9500000000ull

#endif
