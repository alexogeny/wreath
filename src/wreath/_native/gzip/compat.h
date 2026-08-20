/* Compiler portability for the baseline gzip translation units.
 * SPDX-License-Identifier: MPL-2.0
 */
#ifndef WREATH_GZIP_COMPAT_H
#define WREATH_GZIP_COMPAT_H

#if defined(_MSC_VER)
#include <intrin.h>

#define __attribute__(value)
#define __builtin_expect(value, expected) (value)
#define __builtin_prefetch(address, write, locality) ((void)(address))

static __forceinline unsigned
wreath_gzip_ctz32(unsigned long value)
{
    unsigned long index;
    _BitScanForward(&index, value);
    return (unsigned)index;
}

static __forceinline unsigned
wreath_gzip_ctz64(unsigned __int64 value)
{
    unsigned long index;
    _BitScanForward64(&index, value);
    return (unsigned)index;
}

static __forceinline unsigned
wreath_gzip_clz32(unsigned long value)
{
    unsigned long index;
    _BitScanReverse(&index, value);
    return 31u - (unsigned)index;
}

static __forceinline unsigned
wreath_gzip_popcount64(unsigned __int64 value)
{
    value -= (value >> 1) & 0x5555555555555555ull;
    value = (value & 0x3333333333333333ull) +
            ((value >> 2) & 0x3333333333333333ull);
    value = (value + (value >> 4)) & 0x0f0f0f0f0f0f0f0full;
    return (unsigned)((value * 0x0101010101010101ull) >> 56);
}

#define __builtin_ctz(value) wreath_gzip_ctz32(value)
#define __builtin_ctzll(value) wreath_gzip_ctz64(value)
#define __builtin_clz(value) wreath_gzip_clz32(value)
#define __builtin_popcountll(value) wreath_gzip_popcount64(value)

/* The packed sequence is six bytes under GCC. MSVC uses the explicit reserved
 * field and an eight-byte record instead of relying on compiler attributes. */
#define GZ_SEQUENCE_PACKED 0
#endif

#endif
